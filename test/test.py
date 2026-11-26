# Copyright 2025 Google LLC.
# SPDX-License-Identifier: Apache-2.0

import array
import difflib
import filecmp
import math
import os
from pathlib import Path
import pprint
import wave

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge, Timer

from analog_model import DacAmsBridge, R2RDacModel


SAMPLE_RATE = 8000
PWM_PERIOD = 256
FREQ = SAMPLE_RATE * PWM_PERIOD
PERIOD_PS = 488280
SIM_SUFFIX = "glsim" if os.environ.get("GATES") == "yes" else "sim"
SIM_NAME = os.environ.get("SIM", "verilator")


def the42melody(t: int, a: int, b: int, c: int, d: int) -> int:
    ab = (b << 4) | a
    cd = (d << 4) | c
    return (t * (ab & (t >> cd))) & 0xFF


def fractaltrees(t: int, a: int, b: int, c: int, d: int) -> int:
    ab = (b << 4) | a
    ab_nz = 1 if ab == 0 else ab
    cd = (d << 4) | c
    return (t | (t % ab_nz) | (t % (cd + 2))) & 0xFF


def untitleddroid(t: int, a: int, b: int, c: int, d: int) -> int:
    cond = (t >> a) & b
    s = (t >> c) if cond != 0 else (((~t + 1) & 0xFFFFFFFF) >> d)
    return s & 0xFF


def atunetoshare(t: int, a: int, b: int, c: int, d: int) -> int:
    cd = (d << 4) | c
    return (t * ((t >> a) | (t >> b)) & cd) & 0xFF


def sierpinskiharmony(t: int, a: int, b: int, c: int, d: int) -> int:
    return (((t * a) & (t >> b)) | ((t * c) & (t >> d))) & 0xFF


def silence(t: int, a: int, b: int, c: int, d: int) -> int:
    return 0


FORMULAS = [
    the42melody,
    fractaltrees,
    untitleddroid,
    atunetoshare,
    silence,
    silence,
    silence,
    sierpinskiharmony,
]


def golden_sample(num_samples: int, a: int, b: int, c: int, d: int):
    for t in range(num_samples):
        yield [fn(t, a, b, c, d) for fn in FORMULAS]


def golden_pwm(num_samples: int, a: int, b: int, c: int, d: int):
    for s in golden_sample(num_samples, a, b, c, d):
        # sum/or (concat) individual pwm bit signal into a bus
        yield [
            sum((count < s[ch]) << ch for ch in range(len(FORMULAS))) for count in range(PWM_PERIOD)
        ]


def write_wav(path: Path, samples: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(1)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(samples)


def write_wav_16bit(path: Path, voltages: list[float], vdd: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pcm = array.array(
        "h", (max(-32768, min(32767, round((v / vdd - 0.5) * 65536.0))) for v in voltages)
    )
    if array.array("h", [1]).tobytes() != b"\x01\x00":
        pcm.byteswap()
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(pcm.tobytes())


DAC_TRACKS = (0, 1, 2, 3, 7)


async def collect_pwm(dut, num_samples: int):
    for _ in range(num_samples):
        frame = []
        for _ in range(PWM_PERIOD):
            await RisingEdge(dut.clk)
            assert dut.uo_out.value.is_resolvable
            frame.append(int(dut.uo_out.value))
        yield frame


async def collect_sample(dut, num_samples: int):
    async for frame in collect_pwm(dut, num_samples):
        # count high cycles over each 256-clock period to recover the PWM duty cycle (8-bit PCM sample)
        yield [sum((val >> ch) & 1 for val in frame) for ch in range(len(FORMULAS))]


async def _reset_dut(dut, a: int = 0x5, b: int = 0x1, c: int = 0x3, d: int = 0x0) -> None:
    """Start the 2.048 MHz clock, drive `(a, b, c, d)` params, and hold `rst_n` low for 3 cycles."""
    clock = Clock(dut.clk, PERIOD_PS, unit="ps", impl="gpi")
    cocotb.start_soon(clock.start())
    dut.ena.value = 1
    dut.rst_n.value = 0
    dut.ui_in.value = (b << 4) | a
    dut.uio_in.value = (d << 4) | c
    # wait 3 cycles so rst_n propagates past t=0 and non-blocking/unit-delay assignments take effect
    await ClockCycles(dut.clk, 3)


_DAC_DC_LUTS: dict[tuple[float, str, str, float | None, str, bool], list[list[float]]] = {}


def _get_dac_dc_luts(ams: DacAmsBridge) -> list[list[float]]:
    key = (
        ams.vdd,
        ams.corner,
        ams.rcorner,
        ams.temp,
        str(ams.r2r_spice),
        ams.route_rc is not None,
    )
    if key not in _DAC_DC_LUTS:
        luts = ams.measure_dc_luts()
        for ch, lut in enumerate(luts):
            assert all(lut[k + 1] > lut[k] for k in range(255)), (
                f"libngspice0 256-code DC LUT is non-monotonic on ch{ch} for {key}"
            )
        _DAC_DC_LUTS[key] = luts
    return _DAC_DC_LUTS[key]


async def _toggle_params_mid_frame(dut, params: tuple[int, int, int, int]) -> None:
    """Update `ui_in`/`uio_in` at quarter-frame (clock 64) to verify glitch-free registered handoff."""
    await Timer((PWM_PERIOD // 4) * PERIOD_PS - PERIOD_PS // 2, unit="ps")
    await RisingEdge(dut.clk)
    pa, pb, pc, pd = params
    dut.ui_in.value = (pb << 4) | pa
    dut.uio_in.value = (pd << 4) | pc


async def _wait_frames(dut, num_frames: int) -> None:
    if num_frames > 0:
        await Timer(num_frames * PWM_PERIOD * PERIOD_PS - PERIOD_PS // 2, unit="ps")
        await RisingEdge(dut.clk)


async def collect_dac(dut, dac: R2RDacModel, num_samples: int):
    """Collect full-track 8-bit R-2R DAC codes and `ua[4:0]` MSBs once per PWM frame."""
    half = PWM_PERIOD // 2
    for _ in range(num_samples):
        await Timer(half * PERIOD_PS - PERIOD_PS // 2, unit="ps")
        await RisingEdge(dut.clk)
        yield (
            dac.codes(),
            [int(dut.ua.value[idx]) for idx in range(len(DAC_TRACKS))],
        )
        await Timer((PWM_PERIOD - half) * PERIOD_PS - PERIOD_PS // 2, unit="ps")
        await RisingEdge(dut.clk)


async def collect_dac_ams(dut, ams: DacAmsBridge, num_samples: int):
    """Collect continuous live `libngspice0` DAC voltages once per PWM frame."""
    half = PWM_PERIOD // 2
    for _ in range(num_samples):
        await Timer(half * PERIOD_PS - PERIOD_PS // 2, unit="ps")
        await RisingEdge(dut.clk)
        yield ams.voltages()
        await Timer((PWM_PERIOD - half) * PERIOD_PS - PERIOD_PS // 2, unit="ps")
        await RisingEdge(dut.clk)


async def _collect(agen):
    return [item async for item in agen]


@cocotb.test()
async def test_bytebeat(dut):
    dut._log.info("Start")
    dac = R2RDacModel(dut, expected_channels=len(DAC_TRACKS), vdd=3.3)
    ams = DacAmsBridge.create(dut, expected_channels=len(DAC_TRACKS), vdd=3.3)

    await _reset_dut(dut, 0x5, 0x1, 0x3, 0x0)
    assert dut.uio_oe.value == 0
    assert dut.uio_out.value == 0
    assert dac.codes() == [0] * 5 and dac.voltages() == [0.0] * 5
    assert all(int(dut.ua.value[ch]) == 0 for ch in range(5))

    analog_vcd = Path(__file__).resolve().parent / "output" / f"analog_{SIM_SUFFIX}_{SIM_NAME}.vcd"
    if ams is not None:
        await ams.start(duration_ns=5_000_000.0, analog_vcd=analog_vcd)

    def _check_frame(
        f_idx: int,
        active_params: tuple[int, int, int, int],
        frame: list[int],
        got_codes: list[int],
        got_ua: list[int],
        got_v_spice: list[float] | None,
    ) -> float:
        ea, eb, ec, ed = active_params
        exp_sample = [fn(f_idx, ea, eb, ec, ed) for fn in FORMULAS]
        exp_frame = [
            sum((count < exp_sample[ch]) << ch for ch in range(len(FORMULAS)))
            for count in range(PWM_PERIOD)
        ]
        assert frame == exp_frame, "\n" + "\n".join(
            difflib.unified_diff(
                pprint.pformat(exp_frame).splitlines(),
                pprint.pformat(frame).splitlines(),
                fromfile="golden",
                tofile=SIM_SUFFIX,
                lineterm="",
            )
        )
        pwm_codes = [sum((val >> ch) & 1 for val in frame) for ch in DAC_TRACKS]
        exp_codes = [exp_sample[ch] for ch in DAC_TRACKS]
        assert got_codes == pwm_codes == exp_codes, (
            f"Frame {f_idx} DAC vs PWM vs ref mismatch: "
            f"dac={got_codes}, pwm={pwm_codes}, ref={exp_codes}"
        )
        exp_ua = [(c >> 7) & 1 for c in exp_codes]
        assert got_ua == exp_ua, (
            f"Frame {f_idx} DAC ua[4:0] MSB mismatch: got {got_ua}, expected {exp_ua}"
        )
        worst = 0.0
        if got_v_spice is not None and ams is not None:
            lsb = ams.vdd / 256.0
            for ch, (code, v_got) in enumerate(zip(exp_codes, got_v_spice, strict=True)):
                v_ideal = code * lsb
                err_lsb = abs(v_got - v_ideal) / lsb
                worst = max(worst, err_lsb)
                assert err_lsb < 0.5, (
                    f"Frame {f_idx} ch{ch} code={code}: "
                    f"V_spice={v_got:.4f}V vs ideal={v_ideal:.4f}V ({err_lsb:.4f} LSB >= 0.5)"
                )
        return worst

    try:
        dut.rst_n.value = 1  # take out of reset
        # advance to the first rising edge where rst_n == 1 is sampled (start of the first 256-clock PWM period)
        await ClockCycles(dut.clk, 1)

        # Sweep default params, then toggle ui_in/uio_in *mid-frame* (clock 64 of segment's 1st
        # frame) across edge cases:
        #   - (0, 0, 0, 0): exercises fractaltrees' ab==0 divide-by-zero guard
        #   - (f, f, f, f): max nibble values (fractaltrees original params)
        # Because `pcm` is registered in `megabytebeat`, mid-frame toggling must NOT disturb the
        # in-flight 256-clock PWM waveform or live clock-128 DAC voltage; the new params take
        # effect cleanly on the following frame.
        param_schedule = [
            (16, (0x5, 0x1, 0x3, 0x0)),
            (4, (0x0, 0x0, 0x0, 0x0)),
            (4, (0xF, 0xF, 0xF, 0xF)),
        ]
        max_err_lsb = 0.0
        frame_idx = 0
        latched_params = param_schedule[0][1]
        for seg_idx, (seg_samples, seg_params) in enumerate(param_schedule):
            toggle_task = (
                cocotb.start_soon(_toggle_params_mid_frame(dut, seg_params))
                if seg_idx > 0
                else None
            )
            pwm_task = cocotb.start_soon(_collect(collect_pwm(dut, seg_samples)))
            dac_task = cocotb.start_soon(_collect(collect_dac(dut, dac, seg_samples)))
            ams_task = (
                cocotb.start_soon(_collect(collect_dac_ams(dut, ams, seg_samples)))
                if ams is not None
                else None
            )
            if toggle_task is not None:
                await toggle_task
            pwm_frames = await pwm_task
            dac_frames = await dac_task
            ams_frames = await ams_task if ams_task is not None else [None] * seg_samples

            for local_idx, (frame, (got_codes, got_ua), got_v_spice) in enumerate(
                zip(pwm_frames, dac_frames, ams_frames, strict=True)
            ):
                active_params = latched_params if (seg_idx > 0 and local_idx == 0) else seg_params
                max_err_lsb = max(
                    max_err_lsb,
                    _check_frame(frame_idx, active_params, frame, got_codes, got_ua, got_v_spice),
                )
                frame_idx += 1
            latched_params = seg_params

        # ena=0 -> 1 live standby clamp and recovery within the same active non-zero frame.
        active_codes = dac.codes()
        assert any(c > 0 for c in active_codes), (
            f"Expected non-zero active DAC codes: {active_codes}"
        )
        dut.ena.value = 0
        await ClockCycles(dut.clk, 1)
        assert dac.codes() == [0] * 5 and dac.voltages() == [0.0] * 5
        assert all(int(dut.ua.value[ch]) == 0 for ch in range(5))
        max_standby_mv = 0.0
        if ams is not None:
            await Timer(25, unit="us")
            standby_v = ams.voltages()
            max_standby_mv = max(abs(v) for v in standby_v) * 1e3
            assert max_standby_mv < 1.0, f"Standby V_spice={standby_v} exceeds 1 mV"

        dut.ena.value = 1
        await ClockCycles(dut.clk, 1)
        assert dac.codes() == active_codes, (
            f"ena=1 recovery DAC codes mismatch: got {dac.codes()}, expected {active_codes}"
        )
        assert [int(dut.ua.value[ch]) for ch in range(5)] == [(c >> 7) & 1 for c in active_codes]
        max_ena_recover_lsb = 0.0
        if ams is not None:
            await Timer(25, unit="us")
            lsb = ams.vdd / 256.0
            for ch, (code, v_got) in enumerate(zip(active_codes, ams.voltages(), strict=True)):
                err_lsb = abs(v_got - code * lsb) / lsb
                max_ena_recover_lsb = max(max_ena_recover_lsb, err_lsb)
                max_err_lsb = max(max_err_lsb, err_lsb)
                assert err_lsb < 0.5, (
                    f"ena=1 recovery ch{ch} code={code}: "
                    f"V_spice={v_got:.4f}V vs ideal={code * lsb:.4f}V ({err_lsb:.4f} LSB >= 0.5)"
                )

        # Mid-stream rst_n=0 clamps all 5 DACs to <1 mV and restarts cleanly at t=0 on rst_n=1.
        restart_params = (0x5, 0x1, 0x3, 0x0)
        ra, rb, rc, rd = restart_params
        dut.rst_n.value = 0
        dut.ui_in.value = (rb << 4) | ra
        dut.uio_in.value = (rd << 4) | rc
        await ClockCycles(dut.clk, 3)
        assert dac.codes() == [0] * 5 and dac.voltages() == [0.0] * 5
        assert all(int(dut.ua.value[ch]) == 0 for ch in range(5))
        max_reset_mv = 0.0
        if ams is not None:
            await Timer(25, unit="us")
            reset_v = ams.voltages()
            max_reset_mv = max(abs(v) for v in reset_v) * 1e3
            assert max_reset_mv < 1.0, f"Mid-stream rst_n=0 V_spice={reset_v} exceeds 1 mV"

        dut.rst_n.value = 1
        await ClockCycles(dut.clk, 1)
        restart_samples = 4
        pwm_task = cocotb.start_soon(_collect(collect_pwm(dut, restart_samples)))
        dac_task = cocotb.start_soon(_collect(collect_dac(dut, dac, restart_samples)))
        ams_task = (
            cocotb.start_soon(_collect(collect_dac_ams(dut, ams, restart_samples)))
            if ams is not None
            else None
        )
        pwm_frames = await pwm_task
        dac_frames = await dac_task
        ams_frames = await ams_task if ams_task is not None else [None] * restart_samples
        for t_idx, (frame, (got_codes, got_ua), got_v_spice) in enumerate(
            zip(pwm_frames, dac_frames, ams_frames, strict=True)
        ):
            max_err_lsb = max(
                max_err_lsb,
                _check_frame(t_idx, restart_params, frame, got_codes, got_ua, got_v_spice),
            )

        if ams is not None:
            dut._log.info(
                f"ngspice AMS co-sim ({ams.channels} ch @ {ams.vdd:.1f}V, "
                f"{frame_idx}+{restart_samples} frames): "
                f"max|V_spice - V_ideal|={max_err_lsb:.4f} LSB, "
                f"ena_recover={max_ena_recover_lsb:.4f} LSB, "
                f"standby_max={max_standby_mv:.3f} mV, reset_max={max_reset_mv:.3f} mV"
            )
    finally:
        if ams is not None:
            await ams.stop()

    if ams is not None:
        assert analog_vcd.is_file() and analog_vcd.stat().st_size > 0, (
            f"Expected non-empty analog VCD at {analog_vcd}"
        )


@cocotb.test()
@cocotb.parametrize((("start", "end"), [(0, 16), (120, 136), (240, 256)]))
async def test_dac_ams_dc_vs_tran(dut, start: int, end: int):
    """Verify BSIM3v3 + LVS R-2R 256-code DC LUT matches live transient `libngspice0` co-sim."""
    dac = R2RDacModel(dut, expected_channels=len(DAC_TRACKS), vdd=3.3)
    ams = DacAmsBridge.create(dut, expected_channels=len(DAC_TRACKS), vdd=3.3)
    if ams is None:
        dut._log.info("Skipping test_dac_ams_dc_vs_tran (ngspice AMS co-sim unavailable)")
        return

    await _reset_dut(dut, 0x5, 0x1, 0x3, 0x0)
    dac_luts = _get_dac_dc_luts(ams)

    dut.rst_n.value = 1
    await ClockCycles(dut.clk, 1)
    await _wait_frames(dut, start)

    count = end - start
    win_ns = (count + 4) * PWM_PERIOD * PERIOD_PS / 1000.0
    await ams.start(duration_ns=win_ns, analog_vcd=None)
    try:
        dac_task = cocotb.start_soon(_collect(collect_dac(dut, dac, count)))
        ams_task = cocotb.start_soon(_collect(collect_dac_ams(dut, ams, count)))
        dac_frames = await dac_task
        ams_frames = await ams_task
    finally:
        await ams.stop()

    lsb = ams.vdd / 256.0
    max_tran_vs_lut_lsb = 0.0
    max_tran_err_lsb = 0.0
    for offset, ((codes, _), v_spice) in enumerate(zip(dac_frames, ams_frames, strict=True)):
        frame_idx = start + offset
        for dac_ch, track_ch in enumerate(DAC_TRACKS):
            code = codes[dac_ch]
            v_lut = dac_luts[dac_ch][code]
            v_live = v_spice[dac_ch]
            tran_err = abs(v_live - v_lut) / lsb
            max_tran_vs_lut_lsb = max(max_tran_vs_lut_lsb, tran_err)
            assert tran_err < 0.05, (
                f"{FORMULAS[track_ch].__name__} frame {frame_idx} code={code}: "
                f"live V_tran={v_live:.5f}V vs DC LUT={v_lut:.5f}V ({tran_err:.4f} LSB >= 0.05)"
            )
            ideal_err = abs(v_live - code * lsb) / lsb
            max_tran_err_lsb = max(max_tran_err_lsb, ideal_err)
            assert ideal_err < 0.5, (
                f"{FORMULAS[track_ch].__name__} frame {frame_idx} code={code}: "
                f"live V_tran={v_live:.5f}V vs ideal={code * lsb:.5f}V ({ideal_err:.4f} LSB >= 0.5)"
            )

    dut._log.info(
        f"DAC AMS DC vs tran check (frames {start}:{end}): "
        f"max|V_tran - V_lut|={max_tran_vs_lut_lsb:.4f} LSB, "
        f"max|V_tran - V_ideal|={max_tran_err_lsb:.4f} LSB"
    )


@cocotb.test()
@cocotb.parametrize(
    (
        ("corner", "rcorner", "vdd", "temp", "max_ideal_lsb"),
        [
            ("ss", "res_ff", 3.0, 125.0, 0.95),
            ("typical", "res_typical", 5.0, 27.0, 0.50),
        ],
    )
)
async def test_dac_ams_pvt(
    dut,
    corner: str,
    rcorner: str,
    vdd: float,
    temp: float,
    max_ideal_lsb: float,
):
    """Spot-check worst-case PVT (3.0V ss/res_ff 125C) and dual-voltage (5.0V) transient settling."""
    dac = R2RDacModel(dut, expected_channels=len(DAC_TRACKS), vdd=vdd)
    ams = DacAmsBridge.create(
        dut,
        expected_channels=len(DAC_TRACKS),
        vdd=vdd,
        corner=corner,
        rcorner=rcorner,
        temp=temp,
    )
    if ams is None:
        dut._log.info("Skipping test_dac_ams_pvt (ngspice AMS co-sim unavailable)")
        return

    await _reset_dut(dut, 0x5, 0x1, 0x3, 0x0)
    dac_luts = _get_dac_dc_luts(ams)

    start, end = 124, 132  # Crosses 127 -> 128 MSB carry at t=128 and 128 -> 127 at t=129
    dut.rst_n.value = 1
    await ClockCycles(dut.clk, 1)
    await _wait_frames(dut, start)

    count = end - start
    win_ns = (count + 4) * PWM_PERIOD * PERIOD_PS / 1000.0
    await ams.start(duration_ns=win_ns, analog_vcd=None)
    try:
        dac_task = cocotb.start_soon(_collect(collect_dac(dut, dac, count)))
        ams_task = cocotb.start_soon(_collect(collect_dac_ams(dut, ams, count)))
        dac_frames = await dac_task
        ams_frames = await ams_task
    finally:
        await ams.stop()

    seen_codes = [c for codes, _ in dac_frames for c in codes]
    assert 127 in seen_codes and 128 in seen_codes, (
        f"Expected MSB carry codes 127 & 128 in window {start}:{end}, saw {sorted(set(seen_codes))}"
    )

    lsb = ams.vdd / 256.0
    max_tran_vs_lut_lsb = 0.0
    max_tran_err_lsb = 0.0
    for offset, ((codes, _), v_spice) in enumerate(zip(dac_frames, ams_frames, strict=True)):
        frame_idx = start + offset
        for dac_ch, track_ch in enumerate(DAC_TRACKS):
            code = codes[dac_ch]
            v_lut = dac_luts[dac_ch][code]
            v_live = v_spice[dac_ch]
            tran_err = abs(v_live - v_lut) / lsb
            max_tran_vs_lut_lsb = max(max_tran_vs_lut_lsb, tran_err)
            assert tran_err < 0.05, (
                f"[{corner}/{rcorner} {vdd}V {temp}C] {FORMULAS[track_ch].__name__} "
                f"frame {frame_idx} code={code}: "
                f"live V_tran={v_live:.5f}V vs DC LUT={v_lut:.5f}V ({tran_err:.4f} LSB >= 0.05)"
            )
            ideal_err = abs(v_live - code * lsb) / lsb
            max_tran_err_lsb = max(max_tran_err_lsb, ideal_err)
            assert ideal_err < max_ideal_lsb, (
                f"[{corner}/{rcorner} {vdd}V {temp}C] {FORMULAS[track_ch].__name__} "
                f"frame {frame_idx} code={code}: "
                f"live V_tran={v_live:.5f}V vs ideal={code * lsb:.5f}V "
                f"({ideal_err:.4f} LSB >= {max_ideal_lsb})"
            )

    dut._log.info(
        f"DAC AMS PVT check ({corner}/{rcorner} @ {vdd:.1f}V {temp:.0f}C, frames {start}:{end}): "
        f"max|V_tran - V_lut|={max_tran_vs_lut_lsb:.4f} LSB, "
        f"max|V_tran - V_ideal|={max_tran_err_lsb:.4f} LSB"
    )


@cocotb.test()
async def test_wav(dut):
    dac = R2RDacModel(dut, expected_channels=len(DAC_TRACKS), vdd=3.3)
    ams = DacAmsBridge.create(dut, expected_channels=len(DAC_TRACKS), vdd=3.3)
    a, b, c, d = 0x5, 0x1, 0x3, 0x0
    await _reset_dut(dut, a, b, c, d)

    default_samples = 256 if os.environ.get("GATES") == "yes" else 8 * SAMPLE_RATE
    num_samples = int(os.environ.get("NUM_SAMPLES", str(default_samples)))
    dac_luts = _get_dac_dc_luts(ams) if ams is not None else None

    dut.rst_n.value = 1  # take out of reset
    # advance to the first rising edge where rst_n == 1 is sampled (start of the first 256-clock PWM period)
    await ClockCycles(dut.clk, 1)

    pwm_task = cocotb.start_soon(_collect(collect_sample(dut, num_samples)))
    dac_task = cocotb.start_soon(_collect(collect_dac(dut, dac, num_samples)))
    samples = await pwm_task
    dac_frames = await dac_task

    expected = list(golden_sample(num_samples, a, b, c, d))

    out_dir = Path(__file__).resolve().parent / "output"
    max_ams_err_lsb = 0.0
    sum_sq_err_lsb = 0.0
    total_ams_pts = 0
    for dac_ch, track_ch in enumerate(DAC_TRACKS):
        fn = FORMULAS[track_ch]
        write_wav(
            out_dir / f"{fn.__name__}_{SIM_SUFFIX}.wav",
            bytes([s[track_ch] for s in samples]),
        )
        write_wav(
            out_dir / f"{fn.__name__}_{SIM_SUFFIX}_dac.wav",
            bytes([codes[dac_ch] for codes, _ in dac_frames]),
        )
        write_wav(
            out_dir / f"{fn.__name__}_golden.wav",
            bytes([s[track_ch] for s in expected]),
        )
        if ams is not None and dac_luts is not None:
            lsb = ams.vdd / 256.0
            v_track: list[float] = []
            for codes, _ in dac_frames:
                code = codes[dac_ch]
                v_val = dac_luts[dac_ch][code]
                v_track.append(v_val)
                err = abs(v_val - code * lsb) / lsb
                max_ams_err_lsb = max(max_ams_err_lsb, err)
                sum_sq_err_lsb += err * err
                total_ams_pts += 1

            write_wav(
                out_dir / f"{fn.__name__}_{SIM_SUFFIX}_ams.wav",
                bytes([max(0, min(255, round(v / lsb))) for v in v_track]),
            )
            write_wav_16bit(
                out_dir / f"{fn.__name__}_{SIM_SUFFIX}_ams_16bit.wav",
                v_track,
                vdd=ams.vdd,
            )

    for track_ch in DAC_TRACKS:
        fn = FORMULAS[track_ch]
        pwm_wav = out_dir / f"{fn.__name__}_{SIM_SUFFIX}.wav"
        dac_wav = out_dir / f"{fn.__name__}_{SIM_SUFFIX}_dac.wav"
        golden_wav = out_dir / f"{fn.__name__}_golden.wav"
        assert filecmp.cmp(dac_wav, pwm_wav, shallow=False), (
            f"{dac_wav.name} differs from {pwm_wav.name}"
        )
        assert filecmp.cmp(pwm_wav, golden_wav, shallow=False), (
            f"{pwm_wav.name} differs from {golden_wav.name}"
        )
        if ams is not None:
            ams_wav = out_dir / f"{fn.__name__}_{SIM_SUFFIX}_ams.wav"
            assert filecmp.cmp(ams_wav, golden_wav, shallow=False), (
                f"{ams_wav.name} differs from {golden_wav.name}"
            )

    if ams is not None and total_ams_pts > 0:
        rms_err_lsb = math.sqrt(sum_sq_err_lsb / total_ams_pts)
        dut._log.info(
            f"AMS WAV check ({num_samples} samples = {num_samples / SAMPLE_RATE:.3f}s, SPICE DC LUT): "
            f"max|V_ams - V_ideal|={max_ams_err_lsb:.4f} LSB, rms={rms_err_lsb:.4f} LSB"
        )
