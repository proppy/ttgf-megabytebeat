#!/usr/bin/env python3
"""Top-level mixed-signal chip orchestrator.

Builds pure-passive R-2R analog macros (`dac`, `adc`), compiles the fused XLS DSLX core
(`src/megabytebeat.x` instantiating 4x `sc_adc_ramp_1ch_4b` + 5x `sc_r2r_dac_8bit` via
`#[extern_verilog]`), assembles stock LibreLane configs, and runs cocotb (`analog_model`) + `cosim`.
"""

from __future__ import annotations

import argparse
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.request
from types import SimpleNamespace

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge

import analog
import gf180

SRC_DIR = Path(__file__).resolve().parent
REPO_ROOT = SRC_DIR.parent
TEST_DIR = REPO_ROOT / "test"
XLS_VERSION = "v0.0.0-11174-g32dc47058"
RSZ_DONT_TOUCH_RX = (
    r".*(u_tiel|u_tieh|u_term|u_buf|u_inv|u_cmp|u_r2r"
    r"|term_tie|pre_drv|term_drv|in_drv|vsum|cmp_raw|dac_out|adc_in|^ua\[).*"
)


def _eval_tracks_py(t: int, a: int, b: int, c: int, d: int) -> list[int]:
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


@cocotb.test()
async def test_megabytebeat_chip_core(dut) -> None:
    """End-to-end cocotb test using Python R-2R analog models (runs identically on RTL & GL)."""
    if str(TEST_DIR) not in sys.path:
        sys.path.insert(0, str(TEST_DIR))
    from analog_model import R2RAdcModel, R2RDacModel

    adc_model = R2RAdcModel(dut, drive_handle=dut.adc_in, expected_channels=4, vdd=5.0)
    dac_model = R2RDacModel(dut, expected_channels=5, vdd=5.0)

    cocotb.start_soon(Clock(dut.clk, 100, unit="ns").start())
    dut.rst_n.value = 0
    dut.bpm_in.value = 0
    dut.adc_in.value = 0
    for _ in range(4):
        await RisingEdge(dut.clk)

    # Reset standby power-gating: all 4 ADC ladders and 5 DAC ladders clamped to 0 V.
    assert adc_model.ladder_codes() == [0] * 4
    assert dac_model.codes() == [0] * 5

    # Drive 4 analog CV voltages (a=5, b=1, c=3, d=0) via event-driven R2RAdcModel.
    adc_model.set_codes([5, 1, 3, 0])
    adc_model.start()
    dut.rst_n.value = 1

    # Verify exact 5-track 8-bit DAC codes across 2 sweeps (32 clocks = 8 samples at 120 BPM).
    # First 3 samples (t=0..2) use init params 0x0a2a; tick 15 latches [5,1,3,0] for t>=3.
    # Each sample latches into ChipState.pcm on the rising edge of ticks 3,7,11,15,19,23,27,31.
    t_idx = 0
    for cyc in range(36):
        await RisingEdge(dut.clk)
        if cyc >= 4 and (cyc - 4) % 4 == 0 and t_idx < 8:
            params = (5, 1, 3, 0) if t_idx >= 3 else (10, 2, 10, 0)
            exp = _eval_tracks_py(t_idx, *params)
            got = dac_model.codes()
            assert got == exp, f"t={t_idx} DAC mismatch: got {got}, expected {exp}"
            t_idx += 1
    assert t_idx == 8

    # Run extended closed-loop sweep with live CV change + BPM pulse and check full 8-bit activity.
    adc_model.set_codes([10, 2, 10, 0])
    unique_codes = [set() for _ in range(5)]
    for cyc in range(600):
        dut.bpm_in.value = 1 if cyc in (100, 1200) else 0
        await RisingEdge(dut.clk)
        for ch, code in enumerate(dac_model.codes()):
            unique_codes[ch].add(code)

    adc_model.stop()
    dut._log.info(f"5x 8-bit DAC unique codes seen: {[len(s) for s in unique_codes]}")
    assert all(len(s) >= 4 for s in unique_codes), f"Low DAC activity: {unique_codes}"


def find_or_fetch_xls_bin_dir() -> Path:
    """Locate XLS CLI binaries supporting impl-style procs (caching release tarball if needed)."""
    env_dir = os.environ.get("XLS_BIN_DIR")
    if env_dir and (Path(env_dir) / "ir_converter_main").exists():
        return Path(env_dir)
    in_path = shutil.which("ir_converter_main")
    if in_path:
        return Path(in_path).parent

    cache_dir = Path.home() / ".cache/xls" / f"xls-{XLS_VERSION}-linux-x64"
    if not (cache_dir / "ir_converter_main").exists():
        cache_dir.parent.mkdir(parents=True, exist_ok=True)
        url = (
            f"https://github.com/google/xls/releases/download/{XLS_VERSION}/"
            f"xls-{XLS_VERSION}-linux-x64.tar.gz"
        )
        print(f"[xls] Fetching XLS {XLS_VERSION} -> {cache_dir} ...")
        with urllib.request.urlopen(url) as resp:
            data = resp.read()
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
            tf.extractall(cache_dir.parent)
    return cache_dir


def compile_xls_core(
    dslx_path: Path,
    top: str,
    module_name: str,
    out_dir: Path,
    check: bool = True,
) -> Path:
    """Compile fused DSLX proc (with #[extern_verilog] ADC FFI) -> Verilog via XLS CLI."""
    out_dir.mkdir(parents=True, exist_ok=True)
    xls_bin = find_or_fetch_xls_bin_dir()

    if check:
        subprocess.run(
            [
                str(xls_bin / "interpreter_main"),
                "--compare=jit",
                f"--dslx_path={dslx_path.parent}",
                str(dslx_path),
            ],
            check=True,
        )

    ir_path = out_dir / f"{module_name}.ir"
    opt_ir_path = out_dir / f"{module_name}.opt.ir"
    v_path = out_dir / f"{module_name}.v"

    ir_path.write_text(
        subprocess.run(
            [
                str(xls_bin / "ir_converter_main"),
                f"--dslx_path={dslx_path.parent}",
                f"--top={top}",
                str(dslx_path),
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    )
    opt_ir_path.write_text(
        subprocess.run(
            [str(xls_bin / "opt_main"), str(ir_path)],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    )
    v_path.write_text(
        subprocess.run(
            [
                str(xls_bin / "codegen_main"),
                "--generator=pipeline",
                "--pipeline_stages=1",
                "--delay_model=unit",
                f"--module_name={module_name}",
                "--reset=reset",
                "--reset_active_low=false",
                "--reset_asynchronous=false",
                "--reset_data_path=true",
                "--use_system_verilog=false",
                "--flop_inputs=false",
                "--flop_outputs=false",
                str(opt_ir_path),
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    )
    return v_path


def _find_macro_instances(core_v: Path, wrapper_module: str, core_inst: str) -> list[str]:
    """Discover XLS #[extern_verilog] analog macro instance paths in natural channel order."""
    matches = re.findall(rf"\b{re.escape(wrapper_module)}\s+(\w+)\s*\(", core_v.read_text())

    def _key(name: str) -> list[int | str]:
        norm = re.sub(r"__(\d+)_inst$", r"[\1]_inst", name)
        norm = re.sub(r"(?<!\])_inst$", "[0]_inst", norm)
        return [int(tok) if tok.isdigit() else tok for tok in re.split(r"(\d+)", norm)]

    return [f"{core_inst}.{inst}.u_r2r" for inst in sorted(matches, key=_key)]


def find_adc_macro_instances(core_v: Path, core_inst: str) -> list[str]:
    """Discover XLS #[extern_verilog] sc_adc_ramp_1ch_4b instance paths in channel order (0..3)."""
    return _find_macro_instances(core_v, "sc_adc_ramp_1ch_4b", core_inst)


def find_dac_macro_instances(core_v: Path, core_inst: str) -> list[str]:
    """Discover XLS #[extern_verilog] sc_r2r_dac_8bit instance paths in channel order."""
    return _find_macro_instances(core_v, "sc_r2r_dac_8bit", core_inst)


def _rel_dir_ref(path: Path, base_dir: Path) -> str:
    return f"dir::{os.path.relpath(path.resolve(), base_dir.resolve())}"


def emit_librelane_overlays(
    target: str,
    adc_col,
    dac_col,
    core_v: Path,
    tracks: int,
    out_dir: Path,
) -> Path:
    chip_core_v = SRC_DIR / "megabytebeat_chip_core.v"
    v_files = [
        _rel_dir_ref(chip_core_v, out_dir),
        _rel_dir_ref(core_v, out_dir),
        _rel_dir_ref(adc_col.wrapper_verilog, out_dir),
        _rel_dir_ref(dac_col.wrapper_verilog, out_dir),
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
        assert adc_col.channels + tracks <= 6, (
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
    core_v: Path,
    tracks: int,
    gl_netlist: Path | None = None,
) -> None:
    """Run in-file @cocotb.test() via cocotb_tools.runner (Icarus Verilog) on RTL and GL."""
    from cocotb_tools.runner import get_runner

    rtl_sources = [
        chip_core_v,
        core_v,
        adc_col.wrapper_verilog,
        dac_col.wrapper_verilog,
        adc_col.macro_verilog,
        dac_col.macro_verilog,
    ]
    runner = get_runner("icarus")
    runner.build(
        sources=rtl_sources,
        hdl_toplevel="megabytebeat_chip_core",
        parameters={"TRACKS": tracks},
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
    core_v: Path,
    tracks: int,
    dockerized: bool = False,
) -> Path:
    """Emit stock LibreLane Classic config for `megabytebeat_chip_core` and invoke CLI."""
    ll_cfg = out_dir / "librelane_core_config.json"
    v_files = [
        _rel_dir_ref(chip_core_v, out_dir),
        _rel_dir_ref(core_v, out_dir),
        _rel_dir_ref(adc_col.wrapper_verilog, out_dir),
        _rel_dir_ref(dac_col.wrapper_verilog, out_dir),
    ]
    die_w = max(560, 180 + tracks * 56 + 40)
    die_h = 360
    adc_paths = find_adc_macro_instances(core_v, core_inst="u_core")
    adc_insts = {
        path: {"location": [round(18.0 + ch * 38.0, 2), 310.0], "orientation": "N"}
        for ch, path in enumerate(adc_paths)
    }
    dac_paths = find_dac_macro_instances(core_v, core_inst="u_core")[:tracks]
    dac_insts = {
        path: {"location": [round(180.0 + t * 56.0, 2), 298.8], "orientation": "N"}
        for t, path in enumerate(dac_paths)
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
    out_dir: Path = REPO_ROOT / "gen",
    check: bool = False,
    sim: bool = False,
    cosim: bool = False,
    run_ll: bool = False,
    dockerized: bool = False,
    render: bool = False,
) -> None:
    import adc
    import dac

    if tracks is None:
        tracks = 2 if target == "tinytapeout" else 5
    out_dir.mkdir(parents=True, exist_ok=True)

    dac_dir = out_dir / "dac"
    dac_s, dac_cell, dac_pins = dac.build_dac()
    dac_gds, dac_lef, dac_lib, dac_spice = analog.emit_macro(dac_s, dac_cell, dac_pins, dac_dir)
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
    adc_s, adc_cell, adc_pins = adc.build_adc()
    adc_gds, adc_lef, adc_lib, adc_spice = analog.emit_macro(adc_s, adc_cell, adc_pins, adc_dir)
    if check:
        adc.check_adc(adc_cell, adc_gds, adc_spice, adc_pins, adc_dir)
    adc_col = SimpleNamespace(
        cell_name=adc_cell.name,
        channels=adc_channels,
        bits=adc_bits,
        gds=adc_gds,
        lef=adc_lef,
        lib=adc_lib,
        macro_verilog=SRC_DIR / f"{adc_cell.name}.v",
        wrapper_verilog=SRC_DIR / "sc_adc_ramp_1ch_4b.v",
        spice=adc_spice,
    )

    core_v = compile_xls_core(
        dslx_path=SRC_DIR / "megabytebeat.x",
        top="megabytebeat_core",
        module_name="megabytebeat_core",
        out_dir=out_dir / "xls",
        check=True,
    )

    chip_core_v = emit_librelane_overlays(target, adc_col, dac_col, core_v, tracks, out_dir)
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
            core_v,
            tracks,
            dockerized=dockerized,
        )

    if sim:
        run_rtl_and_gl_sim(out_dir, chip_core_v, adc_col, dac_col, core_v, tracks, gl_netlist=gl_nl)

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
    ap.add_argument("--out-dir", type=Path, default=REPO_ROOT / "gen")
    ap.add_argument(
        "--check",
        action="store_true",
        help="Run DRC, LVS, and multi-corner ngspice checks on ADC & DAC macros",
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
        out_dir=args.out_dir,
        check=args.check,
        sim=args.sim,
        cosim=args.cosim,
        run_ll=args.librelane,
        dockerized=args.dockerized,
        render=args.render,
    )
