# Copyright 2025-2026 Google LLC.
# SPDX-License-Identifier: Apache-2.0
"""End-to-end cocotb verification for mixed-signal `tt_um_proppy_megabytebeat`.

Uses `analog_model.R2RAdcModel` / `analog_model.R2RDacModel` and live `libngspice0`
`AdcAmsBridge` / `DacAmsBridge` / `MixedSignalAmsBridge` (`cocotbext-ams`) to verify the
full analog-in -> 4x4b ramp ADC -> 5-track bytebeat -> 5x PWM & 2x 8-bit R-2R DAC datapath
bit-for-bit on both RTL and Gate-Level netlists.
"""

import os
from pathlib import Path
import sys

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge, Timer

from analog_model import (
    AdcAmsBridge,
    DacAmsBridge,
    MixedSignalAmsBridge,
    R2RAdcModel,
    R2RDacModel,
)

SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import analog  # noqa: E402

CLOCK_PERIOD_NS = 488  # ~2.048 MHz (= 64 * 32 kHz core tick = 256 * 8 kHz PWM)
PWM_PERIOD = 256
PWM_PINS = (0, 1, 2, 3, 7)
ADC_IN_NETS = ("ua[0]", "ua[1]", "ua[2]", "ua[3]")
DAC_OUT_NETS = ("ua[4]", "ua[5]")
SIM_SUFFIX = "glsim" if os.environ.get("GATES") == "yes" else "sim"
SIM_NAME = os.environ.get("SIM", "verilator")


def eval_tracks_py(t: int, a: int, b: int, c: int, d: int) -> list[int]:
    ab = (b << 4) | a
    cd = (d << 4) | c
    ab_nz = 1 if ab == 0 else ab
    return [
        (t * (ab & (t >> cd))) & 0xFF,
        (t | (t % ab_nz) | (t % (cd + 2))) & 0xFF,
        ((t >> c) if ((t >> a) & b) != 0 else (((~t & 0xFFFFFFFF) + 1) >> d)) & 0xFF,
        ((t * ((t >> a) | (t >> b))) & cd) & 0xFF,
        (((t * a) & (t >> b)) | ((t * c) & (t >> d))) & 0xFF,
    ]


_ADC_TRIPS: dict[tuple[float, str, str, float | None, str, bool], list[float]] = {}


def _get_adc_trip_points(ams: AdcAmsBridge) -> list[float]:
    key = (
        ams.vdd,
        ams.corner,
        ams.rcorner,
        ams.temp,
        str(ams.r2r_spice),
        ams.route_rc is not None,
    )
    if key not in _ADC_TRIPS:
        trips = ams.measure_trip_points()
        assert all(trips[k + 1] > trips[k] for k in range(len(trips) - 1)), (
            f"libngspice0 15-step ADC trip curve is non-monotonic for {key}: {trips}"
        )
        _ADC_TRIPS[key] = trips
    return _ADC_TRIPS[key]


async def init_dut(dut, dac0_sel: int = 0, dac1_sel: int = 1):
    adc = R2RAdcModel(dut, drive_handle=dut.ua_in, expected_channels=4, vdd=3.3)
    dac = R2RDacModel(dut, expected_channels=2, vdd=3.3)
    clock = Clock(dut.clk, CLOCK_PERIOD_NS, unit="ns", impl="gpi")
    cocotb.start_soon(clock.start())
    dut.ena.value = 1
    dut.rst_n.value = 0
    dut.ui_in.value = 0
    dut.uio_in.value = ((dac1_sel & 0x7) << 3) | (dac0_sel & 0x7)
    dut.ua_in.value = 0
    await ClockCycles(dut.clk, 4)
    return adc, dac


@cocotb.test()
async def test_reset_and_standby_power_gating(dut):
    """Verify rst_n=0 and combinational ena=0 clamp all 4 ADC and 2 DAC ladders to 0 V."""
    adc, dac = await init_dut(dut, dac0_sel=2, dac1_sel=1)

    # During reset: all 4 ADC ladders (in=~4'hF=0) and both DAC ladders (in=0) clamped to 0 V.
    assert int(dut.uio_oe.value) == 0x00
    assert int(dut.uio_out.value) == 0x00
    assert adc.ladder_codes() == [0] * 4
    assert dac.codes() == [0, 0] and dac.voltages() == [0.0, 0.0]
    assert int(dut.ua.value[4]) == 0 and int(dut.ua.value[5]) == 0

    # Release reset and drive CV inputs so the synth produces non-zero DAC codes.
    adc.set_codes([10, 2, 10, 0])
    adc.start()
    dut.rst_n.value = 1
    await ClockCycles(dut.clk, 3 * PWM_PERIOD + 128)
    assert any(c != 0 for c in dac.codes()), f"Expected active DAC output, got {dac.codes()}"

    # Deselecting project (ena=0) combinationally clamps both DACs and all 4 ADCs without a clock edge.
    dut.ena.value = 0
    await Timer(10, unit="ns")
    assert adc.ladder_codes() == [0] * 4
    assert dac.codes() == [0, 0] and dac.voltages() == [0.0, 0.0]
    assert int(dut.ua.value[4]) == 0 and int(dut.ua.value[5]) == 0
    dut.ena.value = 1
    adc.stop()


@cocotb.test()
async def test_end_to_end_mixed_signal(dut):
    """Bit-exact end-to-end check: live 4x4b ADC -> 5x PWM + 2x 8-bit R-2R DAC."""
    dac0_sel, dac1_sel = 2, 1  # untitleddroid on DAC0, fractaltrees on DAC1
    adc_py, dac = await init_dut(dut, dac0_sel=dac0_sel, dac1_sel=dac1_sel)
    adc_ams = AdcAmsBridge.create(
        dut, drive_handle=dut.ua_in, expected_channels=4, vdd=3.3, in_nets=ADC_IN_NETS
    )
    dac_ams = DacAmsBridge.create(dut, expected_channels=2, vdd=3.3, out_nets=DAC_OUT_NETS)
    mixed_ams = (
        MixedSignalAmsBridge(dut, adc_ams, dac_ams)
        if (adc_ams is not None and dac_ams is not None)
        else None
    )
    analog_vcd = Path(__file__).resolve().parent / "output" / f"analog_{SIM_SUFFIX}_{SIM_NAME}.vcd"

    dac_luts: list[list[float]] | None = None
    adc_trips: list[float] | None = None
    if mixed_ams is not None and dac_ams is not None and adc_ams is not None:
        adc_trips = _get_adc_trip_points(adc_ams)
        dac_luts = dac_ams.measure_dc_luts()
        for ch, lut in enumerate(dac_luts):
            assert all(lut[k + 1] > lut[k] for k in range(255)), (
                f"libngspice0 256-code DC LUT is non-monotonic on ch{ch}"
            )
        # Spot-check worst-case 3.0V ss/res_ff 125C monotonicity & INL with PEX + post-PnR SPEF.
        ams_wc = DacAmsBridge.create(
            dut,
            expected_channels=2,
            vdd=3.0,
            corner="ss",
            rcorner="res_ff",
            temp=125.0,
            route_rc=dac_ams.route_rc,
        )
        assert ams_wc is not None
        wc_luts = ams_wc.measure_dc_luts()
        wc_lsb = 3.0 / 256.0
        wc_max_err = 0.0
        for ch, lut in enumerate(wc_luts):
            assert all(lut[k + 1] > lut[k] for k in range(255)), (
                f"ss/res_ff 3.0V 125C 256-code DC LUT is non-monotonic on ch{ch}"
            )
            ch_err = max(abs(lut[k] - k * wc_lsb) / wc_lsb for k in range(256))
            wc_max_err = max(wc_max_err, ch_err)
            assert ch_err < 0.95, f"ss/res_ff 3.0V 125C ch{ch} max err {ch_err:.4f} LSB >= 0.95"
        dut._log.info(
            "DAC AMS worst-case PVT DC check (ss/res_ff @ 3.0V 125C, 2 ch, ladder=%s): "
            "monotonic, max|V_lut - V_ideal|=%.4f LSB",
            dac_ams.r2r_spice.name,
            wc_max_err,
        )

    def _drive_cv(codes: list[int]) -> None:
        if mixed_ams is not None and adc_ams is not None and adc_trips is not None:
            adc_ams.set_codes(codes, trips=adc_trips)
        else:
            adc_py.set_codes(codes)

    _drive_cv([5, 1, 3, 0])
    if mixed_ams is not None:
        await mixed_ams.start(duration_ns=2_500_000.0, analog_vcd=analog_vcd)
    else:
        adc_py.start()

    try:
        dut.rst_n.value = 1
        await RisingEdge(dut.clk)

        # Frame 0 (cyc 0..255): reset State.pcm = 0; tick 3 latches t=0 (0x0a2a) at end of frame 0.
        # Frames 1..3: t=0,1,2 with init params (10,2,10,0).
        # Tick 15 (end of frame 3) latches CV1=(5,1,3,0) -> frames 4..7: t=3..6 with (5,1,3,0).
        # At start of frame 4, switch analog CV to CV2=(10,2,10,0) & switch DAC track selectors to (0,4).
        # Tick 31 (end of frame 7) latches CV2=(10,2,10,0) -> frames 8..11: t=7..10 with (10,2,10,0).
        max_err_lsb = 0.0
        max_tran_vs_lut_lsb = 0.0
        for frame in range(12):
            if frame == 4:
                _drive_cv([10, 2, 10, 0])
                dac0_sel, dac1_sel = 0, 4  # the42melody on DAC0, sierpinskiharmony on DAC1
                dut.uio_in.value = ((dac1_sel & 0x7) << 3) | (dac0_sel & 0x7)

            pwm_ones = [0] * 8
            dac_mid = None
            ua_mid = None
            v_spice_mid = None
            for cyc in range(PWM_PERIOD):
                await RisingEdge(dut.clk)
                uo = int(dut.uo_out.value)
                for ch in range(8):
                    pwm_ones[ch] += (uo >> ch) & 1
                if cyc == PWM_PERIOD // 2:
                    dac_mid = dac.codes()
                    ua_mid = [int(dut.ua.value[4]), int(dut.ua.value[5])]
                    if mixed_ams is not None and dac_ams is not None:
                        v_spice_mid = dac_ams.voltages()

            assert pwm_ones[4] == 0 and pwm_ones[5] == 0 and pwm_ones[6] == 0
            if frame == 0:
                exp_tracks = [0, 0, 0, 0, 0]
            else:
                t_idx = frame - 1
                if frame <= 3:
                    params = (10, 2, 10, 0)
                elif frame <= 7:
                    params = (5, 1, 3, 0)
                else:
                    params = (10, 2, 10, 0)
                exp_tracks = eval_tracks_py(t_idx, *params)

            got_pwm = [pwm_ones[p] for p in PWM_PINS]
            assert got_pwm == exp_tracks, (
                f"Frame {frame} PWM mismatch: got {got_pwm}, exp {exp_tracks}"
            )

            exp_dacs = [exp_tracks[dac0_sel], exp_tracks[dac1_sel]]
            assert dac_mid == exp_dacs, (
                f"Frame {frame} DAC 8-bit mismatch: got {dac_mid}, exp {exp_dacs}"
            )
            assert ua_mid == [(c >> 7) & 1 for c in exp_dacs]
            if v_spice_mid is not None and dac_luts is not None and dac_ams is not None:
                lsb = dac_ams.vdd / 256.0
                for ch, (code, v_got) in enumerate(zip(exp_dacs, v_spice_mid, strict=True)):
                    v_lut = dac_luts[ch][code]
                    tran_err = abs(v_got - v_lut) / lsb
                    max_tran_vs_lut_lsb = max(max_tran_vs_lut_lsb, tran_err)
                    assert tran_err < 0.05, (
                        f"Frame {frame} ch{ch} code={code}: "
                        f"V_tran={v_got:.5f}V vs DC LUT={v_lut:.5f}V ({tran_err:.4f} LSB)"
                    )
                    v_ideal = code * lsb
                    err_lsb = abs(v_got - v_ideal) / lsb
                    max_err_lsb = max(max_err_lsb, err_lsb)
                    assert err_lsb < 0.5, (
                        f"Frame {frame} ch{ch} code={code}: "
                        f"V_spice={v_got:.4f}V vs ideal={v_ideal:.4f}V ({err_lsb:.4f} LSB >= 0.5)"
                    )

        if mixed_ams is not None and dac_ams is not None:
            dut.ena.value = 0
            await Timer(25, unit="us")
            standby_v = dac_ams.voltages()
            max_standby_mv = max(abs(v) for v in standby_v) * 1e3
            assert max_standby_mv < 1.0, f"Standby V_spice={standby_v} exceeds 1 mV"
            dut._log.info(
                f"Mixed-signal ngspice AMS co-sim (4x ADC + {dac_ams.channels}x DAC @ {dac_ams.vdd:.1f}V): "
                f"max|V_tran - V_lut|={max_tran_vs_lut_lsb:.4f} LSB, "
                f"max|V_spice - V_ideal|={max_err_lsb:.4f} LSB, standby_max={max_standby_mv:.3f} mV"
            )
            dut.ena.value = 1
    finally:
        if mixed_ams is not None:
            await mixed_ams.stop()
        else:
            adc_py.stop()

    if mixed_ams is not None:
        assert analog_vcd.is_file() and analog_vcd.stat().st_size > 0, (
            f"Expected non-empty analog VCD at {analog_vcd}"
        )


@cocotb.test()
@cocotb.parametrize(
    (
        ("corner", "rcorner", "vdd", "temp", "max_dnl", "max_inl"),
        [
            ("typical", "res_typical", 3.3, 27.0, 0.25, 0.50),
            ("ss", "res_ff", 3.0, 125.0, 0.50, 0.50),
            ("typical", "res_typical", 5.0, 27.0, 0.25, 0.50),
        ],
    )
)
async def test_adc_ams_pvt(
    dut,
    corner: str,
    rcorner: str,
    vdd: float,
    temp: float,
    max_dnl: float,
    max_inl: float,
):
    """Verify BSIM3v3 + LVS/PEX 4x ramp ADC trip linearity & live transient quantization across PVT."""
    adc_ams = AdcAmsBridge.create(
        dut,
        drive_handle=dut.ua_in,
        expected_channels=4,
        vdd=vdd,
        corner=corner,
        rcorner=rcorner,
        temp=temp,
        in_nets=ADC_IN_NETS,
    )
    if adc_ams is None:
        dut._log.info("Skipping test_adc_ams_pvt (ngspice AMS co-sim unavailable)")
        return

    _, dac = await init_dut(dut, dac0_sel=2, dac1_sel=1)

    trips = _get_adc_trip_points(adc_ams)
    assert 0.0 < trips[0] < trips[-1] < vdd, (
        f"[{corner}/{rcorner} {vdd}V {temp}C] trip points [{trips[0]:.3f}..{trips[-1]:.3f}]V "
        f"outside (0, {vdd:.1f}V)"
    )
    lin = analog.measure_linearity(trips)
    assert lin.monotonic and lin.max_dnl < max_dnl and lin.max_inl < max_inl, (
        f"[{corner}/{rcorner} {vdd}V {temp}C] ADC linearity failed: "
        f"DNL={lin.max_dnl:.4f} (limit {max_dnl}), INL={lin.max_inl:.4f} (limit {max_inl})"
    )

    # Sweep 1 exercises boundary rails (0 V -> 0, VDD -> 15) + mid-scale [7, 10].
    # Sweep 2 swaps rails and exercises wraparound reset with [15, 0, 12, 3].
    vin1 = [
        0.0,
        vdd,
        AdcAmsBridge.code_voltage(7, trips, vdd),
        AdcAmsBridge.code_voltage(10, trips, vdd),
    ]
    exp_codes1 = tuple(AdcAmsBridge.predict_code(v, trips) for v in vin1)
    assert exp_codes1 == (0, 15, 7, 10), f"Unexpected Sweep 1 predicted codes: {exp_codes1}"

    vin2 = [
        vdd,
        0.0,
        AdcAmsBridge.code_voltage(12, trips, vdd),
        AdcAmsBridge.code_voltage(3, trips, vdd),
    ]
    exp_codes2 = tuple(AdcAmsBridge.predict_code(v, trips) for v in vin2)
    assert exp_codes2 == (15, 0, 12, 3), f"Unexpected Sweep 2 predicted codes: {exp_codes2}"

    adc_ams.set_voltages(vin1)
    await adc_ams.start(duration_ns=1_600_000.0, analog_vcd=None)
    try:
        dut.rst_n.value = 1
        await RisingEdge(dut.clk)

        # Wait through Frames 0..3 (Sweep 1), switch to Sweep 2 at Frame 4, verify Frame 4 (t=3) & Frame 8 (t=7).
        for frame in range(9):
            if frame == 4:
                adc_ams.set_voltages(vin2)
                vsums = adc_ams.vsum_voltages()
                assert all(-0.1 <= vs <= vdd + 0.1 for vs in vsums), (
                    f"Out-of-range vsum voltages: {vsums}"
                )

            pwm_ones = [0] * 8
            dac_mid = None
            for cyc in range(PWM_PERIOD):
                await RisingEdge(dut.clk)
                if frame in (4, 8):
                    uo = int(dut.uo_out.value)
                    for ch in range(8):
                        pwm_ones[ch] += (uo >> ch) & 1
                    if cyc == PWM_PERIOD // 2:
                        dac_mid = dac.codes()

            if frame == 4:
                exp_f4 = eval_tracks_py(3, *exp_codes1)
                got_f4 = [pwm_ones[p] for p in PWM_PINS]
                assert got_f4 == exp_f4, (
                    f"[{corner}/{rcorner} {vdd}V {temp}C] Sweep 1 Frame 4 PWM mismatch: "
                    f"got {got_f4}, expected {exp_f4} (codes={exp_codes1})"
                )
                assert dac_mid == [exp_f4[2], exp_f4[1]]
            elif frame == 8:
                exp_f8 = eval_tracks_py(7, *exp_codes2)
                got_f8 = [pwm_ones[p] for p in PWM_PINS]
                assert got_f8 == exp_f8, (
                    f"[{corner}/{rcorner} {vdd}V {temp}C] Sweep 2 Frame 8 PWM mismatch: "
                    f"got {got_f8}, expected {exp_f8} (codes={exp_codes2})"
                )
                assert dac_mid == [exp_f8[2], exp_f8[1]]
    finally:
        await adc_ams.stop()

    dut._log.info(
        f"ADC AMS PVT check ({corner}/{rcorner} @ {vdd:.1f}V {temp:.0f}C, ladder={adc_ams.r2r_spice.name}): "
        f"Vtrip={trips[0]:.3f}..{trips[-1]:.3f}V, max|DNL|={lin.max_dnl:.4f} LSB, "
        f"max|INL|={lin.max_inl:.4f} LSB, live sweeps {exp_codes1} & {exp_codes2} bit-exact"
    )


@cocotb.test()
async def test_bpm_tap_tempo_lock(dut):
    """Verify ui_in[0] BPM pulse stretching, glitch rejection, and 120 -> 480 BPM lock on DACs."""
    dac0_sel, dac1_sel = 1, 2  # fractaltrees & untitleddroid
    adc, dac = await init_dut(dut, dac0_sel=dac0_sel, dac1_sel=dac1_sel)
    adc.set_codes([5, 1, 3, 0])
    adc.start()

    dut.rst_n.value = 1
    await RisingEdge(dut.clk)

    # Golden model matching adc_ramp + bpm_sync_step + megabytebeat
    prev_bpm = False
    tick_cnt = 0
    period_m = 4096
    acc = 0
    t_synth = 0
    active_params = (10, 2, 10, 0)
    held_pcm = [0] * 5

    # Short 4-clock pulses on ui_in[0] test both the 2-FF synchronizer and /64 pulse stretcher:
    #   tick 0    -> initial rising edge (tick_cnt=0 < 1024, period_m stays 4096)
    #   tick 2    -> glitch edge (tick_cnt=2 < 1024, rejected; period_m stays 4096)
    #   tick 1026 -> 1024 ticks after tick 2 -> re-locks to period_m=1024 (480 BPM = 1 tick/sample)
    pulse_ticks = {0, 2, 1026}
    total_ticks = 1043
    relock_dac_codes: list[list[int]] = []

    try:
        for tick in range(total_ticks):
            pulse = tick in pulse_ticks
            if pulse:
                dut.ui_in.value = 1
                await ClockCycles(dut.clk, 4)
                dut.ui_in.value = 0
                await ClockCycles(dut.clk, 28)
            else:
                await ClockCycles(dut.clk, 32)

            # Sample both 8-bit R-2R DACs mid-tick and verify bit-exact match
            if tick in range(3, 20) or tick >= 1027:
                exp_dacs = [held_pcm[dac0_sel], held_pcm[dac1_sel]]
                got_dacs = dac.codes()
                assert got_dacs == exp_dacs, (
                    f"BPM sync DAC mismatch at tick={tick} period_m={period_m}: "
                    f"got {got_dacs}, expected {exp_dacs}"
                )
                if tick >= 1027:
                    relock_dac_codes.append(got_dacs)

            await ClockCycles(dut.clk, 32)

            # Advance golden state at tick_en (clock 63 of this tick)
            adc_vld = (tick % 16) == 15
            if adc_vld:
                active_params = (5, 1, 3, 0)
            rising = pulse and not prev_bpm
            if rising and tick_cnt >= 1024:
                period_m = tick_cnt
            tick_cnt = 1 if rising else min(tick_cnt + 1, 65535)
            prev_bpm = pulse
            next_acc = acc + 1024
            sample_tick = next_acc >= period_m
            acc = (next_acc - period_m) if sample_tick else next_acc
            if sample_tick:
                held_pcm = eval_tracks_py(t_synth, *active_params)
                t_synth += 1
    finally:
        adc.stop()

    assert period_m == 1024
    # At 480 BPM (1 sample per tick), consecutive ticks must produce changing DAC outputs
    assert len({tuple(c) for c in relock_dac_codes}) > 4
