#!/usr/bin/env python3
"""Top-level mixed-signal chip orchestrator.

Imports sub-blocks (`dac`, `adc`, `bpm`, `bytebeat`) and pairs checked-in RTL
under `src/` with generated passive R-2R converter collateral for stock LibreLane on
wafer.space and Tiny Tapeout.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge

import analog
import gf180

SRC_DIR = Path(__file__).resolve().parent
REPO_ROOT = SRC_DIR.parent
RSZ_DONT_TOUCH_RX = (
    r".*(u_tiel|u_tieh|u_term|u_buf|u_inv|u_cmp|u_r2r"
    r"|term_tie|pre_drv|term_drv|in_drv|vsum|cmp_raw|dac_out|adc_in|^ua\[).*"
)


@cocotb.test()
async def test_megabytebeat_chip_core(dut) -> None:
    """In-file cocotb testbench (runs identically on RTL and synthesized 7t5v0 GL netlist)."""
    cocotb.start_soon(Clock(dut.clk, 100, unit="ns").start())
    dut.rst_n.value = 0
    dut.bpm_in.value = 0
    dut.adc_in.value = 0
    for _ in range(4):
        await RisingEdge(dut.clk)
    dut.rst_n.value = 1

    # Drive 4 distinct virtual analog knob voltages on adc_in[3:0]: (a=10, b=2, c=10, d=0)
    targets = [10, 2, 10, 0]
    transitions = 0
    prev_dac = None

    for cyc in range(4500):
        step_phase = cyc % 16
        adc_mask = sum((1 << ch) for ch, thr in enumerate(targets) if step_phase < thr)
        dut.adc_in.value = adc_mask
        dut.bpm_in.value = 1 if cyc in (1200, 2400) else 0
        await RisingEdge(dut.clk)

        if cyc > 32:
            cur = int(dut.dac_out.value)
            if prev_dac is not None and cur != prev_dac:
                transitions += 1
            prev_dac = cur

    dut._log.info(
        f"Completed 4500 clocks via cocotb: dac_out MSB transitions={transitions}, "
        f"final={prev_dac:#b}"
    )
    assert transitions >= 4, f"Expected audio waveform activity on dac_out, got {transitions}"


def _rel_dir_ref(path: Path, base_dir: Path) -> str:
    return f"dir::{os.path.relpath(path.resolve(), base_dir.resolve())}"


def emit_librelane_overlays(
    target: str,
    adc_col,
    dac_col,
    bpm_col,
    bb_col,
    out_dir: Path,
) -> Path:
    chip_core_v = SRC_DIR / "megabytebeat_chip_core.v"
    v_files = [
        _rel_dir_ref(chip_core_v, out_dir),
        _rel_dir_ref(adc_col.subsystem_verilog, out_dir),
        _rel_dir_ref(adc_col.wrapper_verilog, out_dir),
        _rel_dir_ref(dac_col.wrapper_verilog, out_dir),
        _rel_dir_ref(adc_col.ramp_verilog, out_dir),
        _rel_dir_ref(bpm_col.verilog, out_dir),
        _rel_dir_ref(bb_col.verilog, out_dir),
    ]
    extra_lefs = [_rel_dir_ref(adc_col.lef, out_dir), _rel_dir_ref(dac_col.lef, out_dir)]
    extra_gds = [_rel_dir_ref(adc_col.gds, out_dir), _rel_dir_ref(dac_col.gds, out_dir)]
    extra_libs = [_rel_dir_ref(adc_col.lib, out_dir), _rel_dir_ref(dac_col.lib, out_dir)]
    extra_v = [
        _rel_dir_ref(adc_col.macro_verilog, out_dir),
        _rel_dir_ref(dac_col.macro_verilog, out_dir),
    ]
    extra_spice = [_rel_dir_ref(adc_col.spice, out_dir), _rel_dir_ref(dac_col.spice, out_dir)]

    if target == "tinytapeout":
        assert adc_col.channels + bb_col.tracks <= 6, (
            "Tiny Tapeout analog template supports max 6 ua[5:0] pins!"
        )
        (out_dir / "tt_config_extra.json").write_text(
            json.dumps(
                {
                    "VERILOG_FILES": [
                        _rel_dir_ref(SRC_DIR / "tt_um_proppy_megabytebeat.v", out_dir),
                        *v_files,
                    ],
                    "EXTRA_LEFS": extra_lefs,
                    "EXTRA_GDS": extra_gds,
                    "EXTRA_LIBS": extra_libs,
                    "EXTRA_VERILOG_MODELS": extra_v,
                    "EXTRA_SPICE_MODELS": extra_spice,
                    "RSZ_DONT_TOUCH_RX": RSZ_DONT_TOUCH_RX,
                },
                indent=2,
            )
            + "\n"
        )
    else:
        (out_dir / "waferspace_config.yaml").write_text(
            "\n".join(
                [
                    "# Stock LibreLane overlay for wafer-space/gf180mcu-project-template",
                    "VERILOG_FILES:",
                    *[f"  - {p}" for p in v_files],
                    "EXTRA_LEFS:",
                    *[f"  - {p}" for p in extra_lefs],
                    "EXTRA_GDS:",
                    *[f"  - {p}" for p in extra_gds],
                    "EXTRA_LIBS:",
                    *[f"  - {p}" for p in extra_libs],
                    "EXTRA_VERILOG_MODELS:",
                    *[f"  - {p}" for p in extra_v],
                    "EXTRA_SPICE_MODELS:",
                    *[f"  - {p}" for p in extra_spice],
                    f"RSZ_DONT_TOUCH_RX: '{RSZ_DONT_TOUCH_RX}'",
                    "DESIGN_REPAIR_BUFFER_INPUT_PORTS: false",
                    "DESIGN_REPAIR_BUFFER_OUTPUT_PORTS: false",
                    "CLOCK_PERIOD: 30517.5",
                    "",
                ]
            )
        )

    return chip_core_v


def run_rtl_and_gl_sim(
    out_dir: Path,
    chip_core_v: Path,
    adc_col,
    dac_col,
    bpm_col,
    bb_col,
    gl_netlist: Path | None = None,
) -> None:
    """Run in-file @cocotb.test() via cocotb_tools.runner (Icarus Verilog) on RTL and GL."""
    from cocotb_tools.runner import get_runner

    rtl_sources = [
        chip_core_v,
        adc_col.subsystem_verilog,
        adc_col.ramp_verilog,
        bpm_col.verilog,
        bb_col.verilog,
        adc_col.wrapper_verilog,
        dac_col.wrapper_verilog,
    ]
    runner = get_runner("icarus")
    runner.build(
        sources=rtl_sources,
        hdl_toplevel="megabytebeat_chip_core",
        parameters={"TRACKS": bb_col.tracks},
        defines={"SIM": 1},
        timescale=("1ns", "1ps"),
        build_dir=out_dir / "sim_build_rtl",
        always=True,
    )
    res_xml = runner.test(
        hdl_toplevel="megabytebeat_chip_core",
        test_module="chip",
        build_dir=out_dir / "sim_build_rtl",
        test_dir=SRC_DIR,
    )
    assert "<failure" not in Path(res_xml).read_text(), "RTL cocotb test failed!"

    if gl_netlist is not None and gl_netlist.exists():
        sc_v = gf180.SC_DIR / "verilog/gf180mcu_fd_sc_mcu7t5v0.v"
        prim_v = gf180.SC_DIR / "verilog/primitives.v"
        runner_gl = get_runner("icarus")
        runner_gl.build(
            sources=[
                gl_netlist,
                adc_col.macro_verilog,
                dac_col.macro_verilog,
                prim_v,
                sc_v,
            ],
            hdl_toplevel="megabytebeat_chip_core",
            defines={"SIM": 1, "FUNCTIONAL": 1, "UNIT_DELAY": "#1"},
            timescale=("1ns", "1ps"),
            build_dir=out_dir / "sim_build_gl",
            always=True,
        )
        res_gl_xml = runner_gl.test(
            hdl_toplevel="megabytebeat_chip_core",
            test_module="chip",
            build_dir=out_dir / "sim_build_gl",
            test_dir=SRC_DIR,
        )
        assert "<failure" not in Path(res_gl_xml).read_text(), "GL cocotb test failed!"


def run_stock_librelane(
    out_dir: Path,
    chip_core_v: Path,
    adc_col,
    dac_col,
    bpm_col,
    bb_col,
    dockerized: bool = False,
) -> Path:
    """Emit standalone LibreLane Classic config for megabytebeat_chip_core and invoke CLI."""
    ll_cfg = out_dir / "librelane_core_config.json"
    v_files = [
        _rel_dir_ref(chip_core_v, out_dir),
        _rel_dir_ref(adc_col.subsystem_verilog, out_dir),
        _rel_dir_ref(adc_col.wrapper_verilog, out_dir),
        _rel_dir_ref(dac_col.wrapper_verilog, out_dir),
        _rel_dir_ref(adc_col.ramp_verilog, out_dir),
        _rel_dir_ref(bpm_col.verilog, out_dir),
        _rel_dir_ref(bb_col.verilog, out_dir),
    ]
    die_w = max(560, 180 + bb_col.tracks * 56 + 40)
    die_h = 360
    adc_insts = {
        f"u_adc_sub.gen_adc_ch[{ch}].u_adc_ch.u_r2r": {
            "location": [round(18.0 + ch * 38.0, 2), 310.0],
            "orientation": "N",
        }
        for ch in range(adc_col.channels)
    }
    dac_insts = {
        f"gen_dac[{t}].u_dac.u_r2r": {
            "location": [round(180.0 + t * 56.0, 2), 298.8],
            "orientation": "N",
        }
        for t in range(bb_col.tracks)
    }
    cfg = {
        "DESIGN_NAME": "megabytebeat_chip_core",
        "VERILOG_FILES": v_files,
        "MACROS": {
            adc_col.cell_name: {
                "gds": [_rel_dir_ref(adc_col.gds, out_dir)],
                "lef": [_rel_dir_ref(adc_col.lef, out_dir)],
                "vh": [_rel_dir_ref(adc_col.macro_verilog, out_dir)],
                "lib": {"*": [_rel_dir_ref(adc_col.lib, out_dir)]},
                "spice": [_rel_dir_ref(adc_col.spice, out_dir)],
                "instances": adc_insts,
            },
            dac_col.cell_name: {
                "gds": [_rel_dir_ref(dac_col.gds, out_dir)],
                "lef": [_rel_dir_ref(dac_col.lef, out_dir)],
                "vh": [_rel_dir_ref(dac_col.macro_verilog, out_dir)],
                "lib": {"*": [_rel_dir_ref(dac_col.lib, out_dir)]},
                "spice": [_rel_dir_ref(dac_col.spice, out_dir)],
                "instances": dac_insts,
            },
        },
        "FP_MACRO_HORIZONTAL_HALO": 4.0,
        "FP_MACRO_VERTICAL_HALO": 4.0,
        "CLOCK_PORT": "clk",
        "CLOCK_PERIOD": 30517.5,
        "FP_SIZING": "absolute",
        "DIE_AREA": [0, 0, die_w, die_h],
        "PL_TARGET_DENSITY_PCT": 65,
        "RSZ_DONT_TOUCH_RX": RSZ_DONT_TOUCH_RX,
        "RUN_HEURISTIC_DIODE_INSERTION": False,
        "DESIGN_REPAIR_BUFFER_INPUT_PORTS": False,
        "DESIGN_REPAIR_BUFFER_OUTPUT_PORTS": False,
        "GRT_ALLOW_CONGESTION": True,
    }
    ll_cfg.write_text(json.dumps(cfg, indent=2) + "\n")
    ll_bin = shutil.which("librelane") or str(Path(sys.executable).parent / "librelane")
    use_docker = dockerized or (shutil.which("openroad") is None)
    cmd = [ll_bin]
    if use_docker:
        cmd += [
            "-m",
            str(gf180.PDK_ROOT),
            "-m",
            str(REPO_ROOT),
            "--docker-no-tty",
            "--dockerized",
        ]
    cmd += [
        "--pdk",
        "gf180mcuD",
        "--pdk-root",
        str(gf180.PDK_ROOT),
        "--manual-pdk",
        "--scl",
        "gf180mcu_fd_sc_mcu7t5v0",
        "--save-views-to",
        str(out_dir / "final"),
        str(ll_cfg),
    ]
    print(f"[librelane] Running: {' '.join(cmd)}")
    subprocess.run(cmd, check=True)
    return (
        next((out_dir / "final/pnl").glob("*.pnl.v"))
        if (out_dir / "final/pnl").exists()
        else next((out_dir / "final/nl").glob("*.nl.v"))
    )


def build_chip(
    target: str = "waferspace",
    tracks: int | None = None,
    adc_channels: int = 4,
    adc_bits: int = 4,
    dac_bits: int = 8,
    step_samples: int = 1024,
    out_dir: Path = REPO_ROOT / "gen",
    check: bool = False,
    sim: bool = False,
    cosim: bool = False,
    run_ll: bool = False,
    dockerized: bool = False,
    render: bool = False,
) -> None:
    import adc
    from bpm import build_bpm
    from bytebeat import build_bytebeat
    import dac

    if tracks is None:
        tracks = 2 if target == "tinytapeout" else 5
    out_dir.mkdir(parents=True, exist_ok=True)

    dac_dir = out_dir / "dac"
    dac_s, dac_cell, dac_pins = dac.build_dac()
    dac_gds, dac_lef, dac_lib, dac_spice = dac.emit_r2r(dac_s, dac_cell, dac_pins, dac_dir)
    if check:
        dac.check_dac(dac_cell, dac_gds, dac_spice, dac_pins, dac_dir)
    dac_col = SimpleNamespace(
        cell_name=dac_cell.name,
        bits=dac_bits,
        gds=dac_gds,
        lef=dac_lef,
        lib=dac_lib,
        macro_verilog=SRC_DIR / f"{dac_cell.name}.v",
        wrapper_verilog=SRC_DIR / "sc_r2r_dac_8bit.v",
        spice=dac_spice,
    )

    adc_dir = out_dir / "adc"
    adc_s, adc_cell, adc_pins, ramp_ir, ramp_v = adc.build_adc()
    adc_gds, adc_lef, adc_lib, adc_spice = dac.emit_r2r(adc_s, adc_cell, adc_pins, adc_dir)
    adc_ramp_v = adc_dir / "adc_ramp.v"
    adc_ramp_v.write_text(ramp_v)
    if check:
        adc.check_adc(adc_cell, adc_gds, adc_spice, adc_pins, ramp_ir, adc_dir)
    adc_col = SimpleNamespace(
        cell_name=adc_cell.name,
        channels=adc_channels,
        bits=adc_bits,
        gds=adc_gds,
        lef=adc_lef,
        lib=adc_lib,
        macro_verilog=SRC_DIR / f"{adc_cell.name}.v",
        wrapper_verilog=SRC_DIR / "sc_adc_ramp_1ch_4b.v",
        ramp_verilog=adc_ramp_v,
        subsystem_verilog=SRC_DIR / "adc_subsystem.v",
        spice=adc_spice,
    )

    bpm_col = build_bpm(
        step_samples=step_samples,
        param_width=adc_channels * adc_bits,
        out_dir=out_dir / "bpm",
        check=check,
    )
    bb_col = build_bytebeat(
        tracks=tracks,
        param_bits=adc_bits,
        sample_bits=dac_bits,
        out_dir=out_dir / "bytebeat",
        check=check,
    )

    chip_core_v = emit_librelane_overlays(target, adc_col, dac_col, bpm_col, bb_col, out_dir)
    print(
        f"[chip] Assembled {target} collateral "
        f"({adc_channels}x{adc_bits}b ADC + {tracks}x{dac_bits}b DAC) -> {out_dir}"
    )

    gl_nl: Path | None = None
    if run_ll:
        gl_nl = run_stock_librelane(
            out_dir,
            chip_core_v,
            adc_col,
            dac_col,
            bpm_col,
            bb_col,
            dockerized=dockerized,
        )

    if sim:
        run_rtl_and_gl_sim(
            out_dir, chip_core_v, adc_col, dac_col, bpm_col, bb_col, gl_netlist=gl_nl
        )

    if cosim:
        from cosim import run_eurorack_cosim

        run_eurorack_cosim(gen_dir=out_dir, tracks=tracks)

    if render:
        dac_png = analog.render_gds(
            dac_col.gds, dac_dir / f"{dac_col.cell_name}.png", width=1800, height=1200
        )
        adc_png = analog.render_gds(
            adc_col.gds, adc_dir / f"{adc_col.cell_name}.png", width=1600, height=900
        )
        print(f"[chip] Rendered {dac_png} and {adc_png}")
        chip_gds = out_dir / "megabytebeat_chip_core.gds"
        if not chip_gds.exists() and (out_dir / "final/gds").exists():
            chip_gds = next((out_dir / "final/gds").glob("*.gds"), chip_gds)
        if chip_gds.exists():
            chip_png = analog.render_gds(
                chip_gds, out_dir / "megabytebeat_chip_core.png", width=2400, height=960
            )
            print(f"[chip] Rendered {chip_png}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="Top-level mixed-signal chip generator & verification runner."
    )
    ap.add_argument("--target", choices=["waferspace", "tinytapeout"], default="waferspace")
    ap.add_argument(
        "--tracks",
        type=int,
        default=None,
        help="Number of analog output tracks (default: 5 for waferspace, 2 for tinytapeout)",
    )
    ap.add_argument("--adc-channels", type=int, default=4)
    ap.add_argument("--adc-bits", type=int, default=4)
    ap.add_argument("--dac-bits", type=int, default=8)
    ap.add_argument("--step-samples", type=int, default=1024)
    ap.add_argument("--out-dir", type=Path, default=REPO_ROOT / "gen")
    ap.add_argument(
        "--check",
        action="store_true",
        help="Run DRC, LVS, ngspice, and XLS LLVM JIT checks on every sub-block",
    )
    ap.add_argument(
        "--sim",
        action="store_true",
        help="Run full-chip Verilog RTL simulation (and GL simulation if netlist built)",
    )
    ap.add_argument(
        "--cosim",
        action="store_true",
        help="Run closed-loop Eurorack mixed-signal ngspice d_cosim + Verilator check",
    )
    ap.add_argument(
        "--librelane",
        action="store_true",
        help="Run stock LibreLane hardening flow end-to-end (native Nix or --dockerized)",
    )
    ap.add_argument("--dockerized", action="store_true", help="Force Dockerized LibreLane run")
    ap.add_argument(
        "--render",
        action="store_true",
        help="Render KLayout GDS preview PNGs for DAC, ADC, and chip core (if built)",
    )
    args = ap.parse_args()
    build_chip(
        target=args.target,
        tracks=args.tracks,
        adc_channels=args.adc_channels,
        adc_bits=args.adc_bits,
        dac_bits=args.dac_bits,
        step_samples=args.step_samples,
        out_dir=args.out_dir,
        check=args.check,
        sim=args.sim,
        cosim=args.cosim,
        run_ll=args.librelane,
        dockerized=args.dockerized,
        render=args.render,
    )
