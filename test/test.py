# Copyright 2025-2026 Google LLC.
# SPDX-License-Identifier: Apache-2.0
"""End-to-end cocotb verification for mixed-signal `tt_um_proppy_megabytebeat`.

Uses `analog_model.R2RAdcModel` and `analog_model.R2RDacModel` (cocotb's Python analog-model
pattern) to verify the full analog-in -> 4x4b ramp ADC -> 5-track bytebeat -> 5x PWM & 2x
8-bit R-2R DAC datapath bit-for-bit on both RTL and Gate-Level netlists.
"""

import os
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge, Timer

from analog_model import DacAmsBridge, R2RAdcModel, R2RDacModel

CLOCK_PERIOD_NS = 488  # ~2.048 MHz (= 64 * 32 kHz core tick = 256 * 8 kHz PWM)
PWM_PERIOD = 256
PWM_PINS = (0, 1, 2, 3, 7)
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
    """Bit-exact end-to-end check: analog CV -> 4x4b ADC -> 5x PWM + 2x 8-bit R-2R DAC."""
    dac0_sel, dac1_sel = 2, 1  # untitleddroid on DAC0, fractaltrees on DAC1
    adc, dac = await init_dut(dut, dac0_sel=dac0_sel, dac1_sel=dac1_sel)
    ams = DacAmsBridge.create(dut, expected_channels=2, vdd=3.3, out_nets=("ua[4]", "ua[5]"))
    analog_vcd = Path(__file__).resolve().parent / "output" / f"analog_{SIM_SUFFIX}_{SIM_NAME}.vcd"

    dac_luts: list[list[float]] | None = None
    if ams is not None:
        dac_luts = ams.measure_dc_luts()
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
            route_rc=ams.route_rc,
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
            ams.r2r_spice.name,
            wc_max_err,
        )
        await ams.start(duration_ns=2_500_000.0, analog_vcd=analog_vcd)

    try:
        # Sweep 1 analog CV voltages: mid-bin voltages for (a=5, b=1, c=3, d=0).
        adc.set_codes([5, 1, 3, 0])
        adc.start()
        dut.rst_n.value = 1
        await RisingEdge(dut.clk)

        # Frame 0 (cyc 0..255): reset SynthState.pcm = 0; tick 3 latches t=0 (0x0a2a) at end of frame 0.
        # Frames 1..3: t=0,1,2 with init params (10,2,10,0).
        # Tick 15 (end of frame 3) latches CV1=(5,1,3,0) -> frames 4..7: t=3..6 with (5,1,3,0).
        # At start of frame 4, switch analog CV to CV2=(10,2,10,0) & switch DAC track selectors to (0,4).
        # Tick 31 (end of frame 7) latches CV2=(10,2,10,0) -> frames 8..11: t=7..10 with (10,2,10,0).
        max_err_lsb = 0.0
        max_tran_vs_lut_lsb = 0.0
        for frame in range(12):
            if frame == 4:
                adc.set_codes([10, 2, 10, 0])
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
                    if ams is not None:
                        v_spice_mid = ams.voltages()

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
            if v_spice_mid is not None and dac_luts is not None:
                lsb = ams.vdd / 256.0
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

        if ams is not None:
            dut.ena.value = 0
            await Timer(25, unit="us")
            standby_v = ams.voltages()
            max_standby_mv = max(abs(v) for v in standby_v) * 1e3
            assert max_standby_mv < 1.0, f"Standby V_spice={standby_v} exceeds 1 mV"
            dut._log.info(
                f"ngspice AMS co-sim ({ams.channels} ch @ {ams.vdd:.1f}V): "
                f"max|V_tran - V_lut|={max_tran_vs_lut_lsb:.4f} LSB, "
                f"max|V_spice - V_ideal|={max_err_lsb:.4f} LSB, standby_max={max_standby_mv:.3f} mV"
            )
            dut.ena.value = 1
    finally:
        adc.stop()
        if ams is not None:
            await ams.stop()

    if ams is not None:
        assert analog_vcd.is_file() and analog_vcd.stat().st_size > 0, (
            f"Expected non-empty analog VCD at {analog_vcd}"
        )
