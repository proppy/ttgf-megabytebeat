#!/usr/bin/env python3
"""Closed-Loop Eurorack Mixed-Signal Co-Simulation (ngspice d_cosim + Verilator vlnggen).

Orchestrates checked-in `src/cosim_digital_core.v` and `src/eurorack_cosim.spice`
against the generated XLS RTL and LVS-proven converter subcircuits, verifying 7 quantitative
mixed-signal and Eurorack assertions.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
import re
import shutil
import subprocess

import analog
import gf180

SRC_DIR = Path(__file__).resolve().parent
REPO_ROOT = SRC_DIR.parent


@dataclass
class CosimMetrics:
    reset_midscale_v: float
    sweep1_codes: list[int]
    sweep2_codes: list[int]
    sweep3_codes: list[int]
    bpm_pad_min_v: float
    bpm_pad_max_v: float
    bpm_esd_max_ma: float
    bpm_rising_edges: int
    max_dac_err_lsb: float
    dac_settle_99_us: float
    max_rail_droop_mv: float


def _build_verilator_cosim_so(
    cosim_dir: Path,
    core_v: Path,
) -> tuple[Path, list[str], list[tuple[str, int, int]]]:
    """Combine checked-in cosim_digital_core.v with fused XLS RTL and compile via ngspice vlnggen."""
    combined_v = cosim_dir / "cosim_digital_core.v"
    combined_v.write_text(
        "\n\n".join(
            [
                "`define SIM 1",
                (SRC_DIR / "cosim_digital_core.v").read_text(),
                core_v.read_text(),
                (SRC_DIR / "sc_adc_ramp_1ch_4b.v").read_text(),
                (SRC_DIR / "r2r_ladder_4bit_sum.v").read_text(),
            ]
        )
    )
    obj_dir = cosim_dir / "cosim_digital_core_obj_dir"
    if obj_dir.exists():
        shutil.rmtree(obj_dir)
    subprocess.run(
        ["ngspice", "vlnggen", "cosim_digital_core.v"],
        cwd=cosim_dir,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    in_ports = [
        m.group(1)
        for line in (obj_dir / "inputs.h").read_text().splitlines()
        if (m := re.match(r"VL_DATA\(\d+,(\w+),0,0\)", line))
    ]
    out_ports = [
        (m.group(1), int(m.group(2)), int(m.group(3)))
        for line in (obj_dir / "outputs.h").read_text().splitlines()
        if (m := re.match(r"VL_DATA\(\d+,(\w+),(\d+),(\d+)\)", line))
    ]
    return cosim_dir / "cosim_digital_core.so", in_ports, out_ports


def _write_d_cosim_inst(
    out_path: Path,
    so_path: Path,
    in_ports: list[str],
    out_ports: list[tuple[str, int, int]],
) -> None:
    """Emit the 6-line XSPICE adc_bridge / d_cosim / dac_bridge wiring matching Verilator's headers."""
    a_in_map = {
        "clk": "clk_core",
        "rst_n": "rst_n_core",
        "bpm_in": "bpm_core",
        "cmp_0": "cmp_a_0",
        "cmp_1": "cmp_a_1",
        "cmp_2": "cmp_a_2",
        "cmp_3": "cmp_a_3",
    }
    in_analog_vec = " ".join(a_in_map[p] for p in in_ports)
    in_dig_vec = " ".join(f"d_in_{p}" for p in in_ports)

    out_dig_tokens = []
    out_ana_tokens = []
    for pname, msb, lsb in out_ports:
        for bit in range(msb, lsb - 1, -1):
            suffix = f"_{bit}" if msb > lsb else ""
            out_dig_tokens.append(f"d_out_{pname}{suffix}")
            out_ana_tokens.append(f"a_{pname}{suffix}")

    out_path.write_text(
        "\n".join(
            [
                f"abridge_in [{in_analog_vec}] [{in_dig_vec}] adc_core_b",
                ".model adc_core_b adc_bridge in_low=1.8 in_high=3.2",
                f"adut [{in_dig_vec}] [{' '.join(out_dig_tokens)}] null vlt_chip",
                f'.model vlt_chip d_cosim simulation="{so_path.resolve()}"',
                f"abridge_out [{' '.join(out_dig_tokens)}] [{' '.join(out_ana_tokens)}] dac_core_b",
                ".model dac_core_b dac_bridge out_low=0.0 out_high=5.0 t_rise=0.8n t_fall=0.8n",
                "",
            ]
        )
    )


def run_eurorack_cosim(
    gen_dir: Path = REPO_ROOT / "gen",
    tracks: int = 5,
    cv_r_src_ohm: float = 100.0,
) -> CosimMetrics:
    """Run deterministic 68-clock Eurorack mixed-signal transient co-simulation and assert all 7 checks."""
    if tracks != 5:
        raise ValueError("Checked-in src/eurorack_cosim.spice targets 5 tracks.")
    cosim_dir = gen_dir / "cosim"
    cosim_dir.mkdir(parents=True, exist_ok=True)

    print("[cosim 1/3] Compiling fused XLS digital RTL to Verilator d_cosim shared library ...")
    so_path, in_ports, out_ports = _build_verilator_cosim_so(
        cosim_dir,
        core_v=gen_dir / "xls/megabytebeat_core.v",
    )
    adc_cir = cosim_dir / "r2r_ladder_4bit_sum_ngspice.cir"
    adc_cir.write_text(analog.spice_to_ngspice(gen_dir / "adc/r2r_ladder_4bit_sum.spice"))
    dac_cir = cosim_dir / "r2r_ladder_8bit_ngspice.cir"
    dac_cir.write_text(analog.spice_to_ngspice(gen_dir / "dac/r2r_ladder_8bit.spice"))
    env_text = analog.ngspice_env(
        corner="typical",
        rcorner="res_typical",
        includes=[gf180.IO_SPICE, adc_cir, dac_cir],
        extra_libs=["moscap_typical", "diode_typical"],
    )
    (cosim_dir / "env.spice").write_text(env_text + f".param cv_r_src = {cv_r_src_ohm}\n")
    _write_d_cosim_inst(cosim_dir / "d_cosim_inst.spice", so_path, in_ports, out_ports)

    print(
        "[cosim 2/3] Running ngspice-42 + Verilator closed-loop transient simulation (0..2.08 ms) ..."
    )
    subprocess.run(
        ["ngspice", "-b", str(SRC_DIR / "eurorack_cosim.spice")],
        cwd=cosim_dir,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    print("[cosim 3/3] Evaluating 7 deterministic mixed-signal & Eurorack assertions ...")
    return _evaluate_cosim_assertions(cosim_dir / "eurorack_cosim.dat", tracks)


def _evaluate_cosim_assertions(dat_path: Path, tracks: int) -> CosimMetrics:
    rows = [
        [float(x) for x in line.split()]
        for line in dat_path.read_text().splitlines()
        if line.strip()
    ]

    def sig(r: list[float], idx: int) -> float:
        return r[2 * idx + 1]

    def row_at(t_us: float) -> list[float]:
        return min(rows, key=lambda r: abs(r[0] * 1e6 - t_us))

    def decode_abcd(r: list[float]) -> list[int]:
        out = []
        for ch in range(4):
            val = 0
            for bi in range(4):
                val = (val << 1) | (1 if sig(r, 6 + ch * 4 + bi) > 2.5 else 0)
            out.append(val)
        return out

    # Expected Eurorack divider gain: R_load / (R_dac_out + R_prot + R_load) = 100k / (10.46k + 1k + 100k)
    euro_gain = 100_000.0 / (10_460.0 + 1_000.0 + 100_000.0)
    euro_lsb_v = (4.9803 * euro_gain) / 255.0

    # Check 1: Reset Mid-Scale Bias (code 128 -> ~2.243V at Eurorack jack, click-free startup)
    r_rst = row_at(45.0)
    reset_v = sig(r_rst, 22)
    exp_mid_v = (128.0 / 255.0) * 4.9803 * euro_gain
    assert abs(reset_v - exp_mid_v) < 0.05, (
        f"Check 1 FAILED: Reset mid-scale {reset_v:.4f}V != {exp_mid_v:.4f}V"
    )
    print(
        f"  [PASS 1/7] Reset Mid-Scale Bias: V(euro_out) = {reset_v:.4f}V "
        f"(target {exp_mid_v:.4f}V, click-free)"
    )

    # Check 2: Closed-Loop ADC Sweep 1 & Sweep 2 Alignment (0 LSB drift across sweep wraparound)
    sw1 = decode_abcd(row_at(650.0))
    sw2 = decode_abcd(row_at(1070.0))
    assert sw1 == [5, 1, 2, 0] and sw2 == [5, 1, 2, 0], (
        f"Check 2 FAILED: Sweep1={sw1}, Sweep2={sw2} (expected [5, 1, 2, 0] with 0 LSB skew)"
    )
    print(f"  [PASS 2/7] Closed-Loop ADC Sweep 1 & Sweep 2 Alignment: {sw1} -> {sw2} (0 LSB skew)")

    # Check 3: Dynamic CV Step & Zero Channel-to-Channel Crosstalk
    sw3 = decode_abcd(row_at(2050.0))
    assert sw3[0] == sw1[0] == 5, (
        f"Check 3 FAILED: Ch0 crosstalk! Expected 5, got {sw3[0]} after Ch1..3 stepped"
    )
    assert sw3[1] in (9, 10) and sw3[2] == 4 and sw3[3] == 15, (
        f"Check 3 FAILED: Dynamic step tracking wrong: {sw3}"
    )
    print(
        f"  [PASS 3/7] Dynamic CV Step & Zero Crosstalk: Ch0 stayed {sw3[0]} "
        f"while Ch1..3 stepped -> {sw3[1:]}"
    )

    # Check 4: Bipolar -5V..+8V Eurorack Clamp & Schmitt-Trigger Hysteresis
    pad_bpm_vals = [sig(r, 0) for r in rows]
    bpm_core_vals = [sig(r, 1) for r in rows]
    esd_ma_vals = [abs(sig(r, 2)) * 1e3 for r in rows]
    bpm_pad_min, bpm_pad_max = min(pad_bpm_vals), max(pad_bpm_vals)
    bpm_esd_max_ma = max(esd_ma_vals)
    bpm_rising = sum(
        1 for i in range(1, len(bpm_core_vals)) if bpm_core_vals[i - 1] < 2.5 <= bpm_core_vals[i]
    )
    assert -0.8 <= bpm_pad_min and bpm_pad_max <= 5.85, (
        f"Check 4a FAILED: Pad ESD clamp out of bounds [{bpm_pad_min:.2f}V, {bpm_pad_max:.2f}V]"
    )
    assert bpm_esd_max_ma < 1.0, (
        f"Check 4b FAILED: ESD clamp current {bpm_esd_max_ma:.2f}mA > 1.0mA"
    )
    assert bpm_rising == 2, (
        f"Check 4c FAILED: Schmitt pad chattered! Expected 2 clean rising edges, got {bpm_rising}"
    )
    print(
        f"  [PASS 4/7] Eurorack -5V..+8V Clamp & Schmitt Immunity: "
        f"pad=[{bpm_pad_min:.2f}V..{bpm_pad_max:.2f}V], "
        f"I_esd={bpm_esd_max_ma:.2f}mA, edges={bpm_rising}"
    )

    # Check 5: End-to-End Bytebeat Analog Waveform vs Python Reference across sample_tick frames
    tick_times_us = [
        rows[i][0] * 1e6
        for i in range(1, len(rows))
        if rows[i][0] * 1e6 > 60.0 and sig(rows[i - 1], 5) < 2.5 <= sig(rows[i], 5)
    ]
    assert len(tick_times_us) >= 12, f"Expected >=12 sample_tick pulses, got {len(tick_times_us)}"

    def eval_tracks_py(t_idx: int, a: int, b: int, c: int, d: int) -> list[int]:
        ab = (b << 4) | a
        cd = (d << 4) | c
        ab_nz = 1 if ab == 0 else ab
        cd2 = cd + 2
        cond0 = (t_idx >> a) & b
        p0 = t_idx >> c
        p1 = ((~t_idx & 0xFFFFFFFF) + 1) >> d
        return [
            (t_idx * (ab & (t_idx >> cd))) & 0xFF,
            (t_idx | (t_idx % ab_nz) | (t_idx % cd2)) & 0xFF,
            (p0 if cond0 != 0 else p1) & 0xFF,
            (t_idx * ((t_idx >> a) | (t_idx >> b)) & cd) & 0xFF,
            (((t_idx * a) & (t_idx >> b)) | ((t_idx * c) & (t_idx >> d))) & 0xFF,
        ]

    max_err_lsb = 0.0
    worst_info = ""
    for sample_t, t_tick in enumerate(tick_times_us[:-2]):
        t_measure = t_tick + 85.0
        if t_measure > 2060.0:
            break
        r_m = row_at(t_measure)
        a, b, c, d = decode_abcd(row_at(t_tick + 5.0))
        exp_codes = eval_tracks_py(sample_t, a, b, c, d)
        for k in range(tracks):
            v_meas = sig(r_m, 22 + k)
            v_exp = (exp_codes[k] / 255.0) * 4.9803 * euro_gain
            err_lsb = abs(v_meas - v_exp) / euro_lsb_v
            if err_lsb > max_err_lsb:
                max_err_lsb = err_lsb
                worst_info = (
                    f"sample_t={sample_t} t_tick={t_tick:.1f}us k={k} "
                    f"abcd=({a},{b},{c},{d}) exp={exp_codes[k]} "
                    f"v_meas={v_meas:.4f}V v_exp={v_exp:.4f}V"
                )

    assert max_err_lsb < 1.0, (
        f"Check 5 FAILED: End-to-end bytebeat analog error {max_err_lsb:.3f} LSB >= 1.0 LSB "
        f"({worst_info})"
    )
    print(
        f"  [PASS 5/7] End-to-End Bytebeat Analog Waveform Accuracy "
        f"({len(tick_times_us) - 2} frames x {tracks} ch): max error = {max_err_lsb:.3f} LSB"
    )

    # Check 6: DAC 99% analog settling time & rail-overshoot check into 220pF Eurorack cable
    all_dac_v = [sig(r, 22 + k) for r in rows for k in range(tracks)]
    min_dac_v, max_dac_v = min(all_dac_v), max(all_dac_v)
    assert -0.05 <= min_dac_v and max_dac_v <= 5.05 * euro_gain + 0.05, (
        "Check 6a FAILED: DAC overshoot!"
    )
    best_step_dv = 0.0
    settle_99_us = 0.0
    for k in range(tracks):
        for t_tick in tick_times_us[:-2]:
            r_pre = row_at(t_tick - 2.0)
            r_post = row_at(t_tick + 55.0)
            v0, v1 = sig(r_pre, 22 + k), sig(r_post, 22 + k)
            if abs(v1 - v0) > best_step_dv:
                best_step_dv = abs(v1 - v0)
                sub = [r for r in rows if (t_tick - 2.0) <= r[0] * 1e6 <= (t_tick + 55.0)]
                t_start = next(
                    (r[0] * 1e6 for r in sub if abs(sig(r, 22 + k) - v0) > 0.02 * best_step_dv),
                    t_tick,
                )
                t_end = next(
                    (
                        r[0] * 1e6
                        for r in sub
                        if r[0] * 1e6 >= t_start and abs(sig(r, 22 + k) - v1) <= 0.01 * best_step_dv
                    ),
                    t_tick + 55.0,
                )
                settle_99_us = t_end - t_start
    assert best_step_dv > 3.5 and settle_99_us < 20.0, (
        f"Check 6b FAILED: Cable settling {settle_99_us:.2f}us (dv={best_step_dv:.2f}V)"
    )
    print(
        f"  [PASS 6/7] Eurorack 220pF Cable Settling & Glitch Check: "
        f"99% settle = {settle_99_us:.2f} us (step={best_step_dv:.2f}V), no overshoot"
    )

    # Check 7: Shared VDD/VSS Rail Bounce under simultaneous multi-channel switching
    rail_droops_mv = [abs(5.0 - (sig(r, 3) - sig(r, 4))) * 1e3 for r in rows if r[0] * 1e6 > 10.0]
    max_droop_mv = max(rail_droops_mv)
    assert max_droop_mv < 50.0, f"Check 7 FAILED: Shared VDD/VSS droop {max_droop_mv:.2f}mV >= 50mV"
    print(
        f"  [PASS 7/7] Shared VDD/VSS Supply Bounce Immunity (0.15-ohm bondwires): "
        f"peak droop = {max_droop_mv:.2f} mV (< 50 mV)"
    )

    return CosimMetrics(
        reset_midscale_v=reset_v,
        sweep1_codes=sw1,
        sweep2_codes=sw2,
        sweep3_codes=sw3,
        bpm_pad_min_v=bpm_pad_min,
        bpm_pad_max_v=bpm_pad_max,
        bpm_esd_max_ma=bpm_esd_max_ma,
        bpm_rising_edges=bpm_rising,
        max_dac_err_lsb=max_err_lsb,
        dac_settle_99_us=settle_99_us,
        max_rail_droop_mv=max_droop_mv,
    )


if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="Run closed-loop Eurorack mixed-signal ngspice d_cosim + Verilator check."
    )
    ap.add_argument("--gen-dir", type=Path, default=REPO_ROOT / "gen")
    ap.add_argument("--tracks", type=int, default=5)
    ap.add_argument(
        "--cv-r-src",
        type=float,
        default=100.0,
        help="External Eurorack CV source impedance (100=buffered op-amp, 1000=passive 1k)",
    )
    args = ap.parse_args()
    print(run_eurorack_cosim(gen_dir=args.gen_dir, tracks=args.tracks, cv_r_src_ohm=args.cv_r_src))
