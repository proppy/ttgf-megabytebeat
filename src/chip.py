#!/usr/bin/env python3
"""Top-level mixed-signal chip orchestrator.

Builds pure-passive R-2R analog macros (`dac`, `adc`), compiles the fused XLS DSLX core
(`src/megabytebeat.x` instantiating 4x `sc_adc_ramp_1ch_4b` + 2x/5x `sc_r2r_dac_8bit` via
`#[extern_verilog]`), assembles stock LibreLane configs (wafer.space and Tiny Tapeout TTGF26c 2x2),
and runs cocotb (`analog_model`) + `cosim` verification.
"""

from __future__ import annotations

import argparse
import io
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
from types import SimpleNamespace

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge

import analog
import gf180
import tools

SRC_DIR = Path(__file__).resolve().parent
REPO_ROOT = SRC_DIR.parent
TEST_DIR = REPO_ROOT / "test"
XLS_REPO = "proppy/xls"
XLS_VERSION = "v0.0.0-11264-g628b096fe"
_VLT_VER = tools.NATIVE_MIN_TOOL_VERSIONS["verilator"]
VERILATOR_VERSION = f"v{_VLT_VER}"
MIN_VERILATOR_VERSION = tuple(int(x) for x in _VLT_VER.split("."))
RSZ_DONT_TOUCH_RX = (
    r".*(u_tiel|u_tieh|u_term|u_buf|u_inv|u_cmp|u_r2r"
    r"|term_tie|pre_drv|term_drv|in_drv|vsum|cmp_lo|cmp_raw|dac_out|dac_bus|adc_in|^ua\[).*"
)
# Exclude GF180 delay cells so OpenROAD buffer_ports / repair_design use compact buf_* instead of dlyb_1.
DLY_EXCLUDED_CELLS = [
    f"gf180mcu_fd_sc_mcu7t5v0__dly{grp}_{drv}" for grp in "abcd" for drv in (1, 2, 4)
]


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
    dac_model = R2RDacModel(dut, expected_channels=None, vdd=5.0)
    tracks = dac_model.channels
    assert tracks in (2, 5), f"Unexpected DAC channel count: {tracks}"

    for pwr in ("VPWR", "VDD"):
        if hasattr(dut, pwr):
            getattr(dut, pwr).value = 1
    for gnd in ("VGND", "VSS"):
        if hasattr(dut, gnd):
            getattr(dut, gnd).value = 0

    cocotb.start_soon(Clock(dut.clk, 2000, unit="ns").start())
    dut.rst_n.value = 0
    dut.bpm_in.value = 0
    dut.adc_in.value = 0
    for _ in range(4):
        await RisingEdge(dut.clk)

    # Reset standby power-gating: all 4 ADC ladders and all DAC ladders clamped to 0 V.
    assert adc_model.ladder_codes() == [0] * 4
    assert dac_model.codes() == [0] * tracks

    # Drive 4 analog CV voltages (a=5, b=1, c=3, d=0) via event-driven R2RAdcModel.
    adc_model.set_codes([5, 1, 3, 0])
    adc_model.start()
    dut.rst_n.value = 1

    # Verify exact 8-bit DAC codes across 2 sweeps (32 clocks = 8 samples at 120 BPM).
    # First 3 samples (t=0..2) use init params 0x0a2a; tick 15 latches [5,1,3,0] for t>=3.
    # Each sample latches into ChipState.pcm on the rising edge of ticks 3,7,11,15,19,23,27,31.
    t_idx = 0
    for cyc in range(36):
        await RisingEdge(dut.clk)
        if cyc >= 4 and (cyc - 4) % 4 == 0 and t_idx < 8:
            params = (5, 1, 3, 0) if t_idx >= 3 else (10, 2, 10, 0)
            exp = _eval_tracks_py(t_idx, *params)[:tracks]
            got = dac_model.codes()
            assert got == exp, f"t={t_idx} DAC mismatch: got {got}, expected {exp}"
            t_idx += 1
    assert t_idx == 8

    # Run extended closed-loop sweep with live CV change + BPM pulse and check full 8-bit activity.
    adc_model.set_codes([10, 2, 10, 0])
    unique_codes = [set() for _ in range(tracks)]
    for cyc in range(600):
        dut.bpm_in.value = 1 if cyc in (100, 1200) else 0
        await RisingEdge(dut.clk)
        for ch, code in enumerate(dac_model.codes()):
            unique_codes[ch].add(code)

    adc_model.stop()
    dut._log.info(f"{tracks}x 8-bit DAC unique codes seen: {[len(s) for s in unique_codes]}")
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
            f"https://github.com/{XLS_REPO}/releases/download/{XLS_VERSION}/"
            f"xls-{XLS_VERSION}-linux-x64.tar.gz"
        )
        print(f"[xls] Fetching XLS {XLS_VERSION} ({XLS_REPO}) -> {cache_dir} ...")
        with urllib.request.urlopen(url) as resp:
            data = resp.read()
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
            tf.extractall(cache_dir.parent)
    return cache_dir


def _parse_verilator_version(bin_path: str | Path) -> tuple[int, int] | None:
    try:
        out = subprocess.check_output([str(bin_path), "--version"], text=True)
    except (OSError, subprocess.CalledProcessError):
        return None
    m = re.search(r"Verilator\s+(\d+)\.(\d+)", out)
    return (int(m.group(1)), int(m.group(2))) if m else None


def ensure_modern_verilator() -> Path:
    """Ensure Verilator >= 5.054 is on PATH/VERILATOR_ROOT (auto-caching prebuilt zip if needed)."""
    existing = shutil.which("verilator")
    if existing:
        ver = _parse_verilator_version(existing)
        if ver is not None and ver >= MIN_VERILATOR_VERSION:
            return Path(existing).resolve().parent

    cache_dir = Path.home() / ".cache/verilator" / VERILATOR_VERSION
    vlt_bin = cache_dir / "bin/verilator"
    if not vlt_bin.exists():
        asset = (
            "verilator-macos-15.zip" if sys.platform == "darwin" else "verilator-ubuntu-24.04.zip"
        )
        url = (
            f"https://github.com/veryl-lang/verilator-package/releases/download/"
            f"{VERILATOR_VERSION}/{asset}"
        )
        print(f"[verilator] Fetching Verilator {VERILATOR_VERSION} ({asset}) -> {cache_dir} ...")
        cache_dir.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(url) as resp:
            data = resp.read()
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for info in zf.infolist():
                target = Path(zf.extract(info, cache_dir))
                mode = info.external_attr >> 16
                if mode:
                    target.chmod(mode)
    vlt_mk = cache_dir / "include/verilated.mk"
    if vlt_mk.exists():
        mk_txt = vlt_mk.read_text()
        if shutil.which("mold") is None:
            mk_txt = mk_txt.replace("-fuse-ld=mold", "")
        if shutil.which("ccache") is None:
            mk_txt = re.sub(r"(?m)^OBJCACHE\s*\?=.*ccache.*$", "OBJCACHE ?=", mk_txt)
        vlt_mk.write_text(mk_txt)

    os.environ["VERILATOR_ROOT"] = str(cache_dir)
    bin_dir = str(cache_dir / "bin")
    if bin_dir not in os.environ.get("PATH", "").split(os.pathsep):
        os.environ["PATH"] = f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"
    return cache_dir / "bin"


def compile_xls_core(
    dslx_path: Path,
    top: str,
    module_name: str,
    out_dir: Path,
    check: bool = True,
    delay_model: str = "gf180mcu_5v0",
) -> Path:
    """Compile fused DSLX proc (with #[extern_verilog] ADC/DAC FFI) -> SystemVerilog via XLS CLI."""
    out_dir.mkdir(parents=True, exist_ok=True)
    xls_bin = find_or_fetch_xls_bin_dir()
    stdlib_dir = xls_bin / "xls/dslx/stdlib"
    stdlib_args = [f"--dslx_stdlib_path={stdlib_dir}"] if stdlib_dir.is_dir() else []

    if check:
        subprocess.run(
            [
                str(xls_bin / "interpreter_main"),
                "--compare=jit",
                *stdlib_args,
                f"--dslx_path={dslx_path.parent}",
                str(dslx_path),
            ],
            check=True,
        )

    ir_path = out_dir / f"{module_name}.ir"
    opt_ir_path = out_dir / f"{module_name}.opt.ir"
    sv_path = out_dir / f"{module_name}.sv"

    ir_path.write_text(
        subprocess.run(
            [
                str(xls_bin / "ir_converter_main"),
                *stdlib_args,
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
            [
                str(xls_bin / "opt_main"),
                f"--delay_model={delay_model}",
                f"--area_model={delay_model}",
                str(ir_path),
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    )
    sv_path.write_text(
        subprocess.run(
            [
                str(xls_bin / "codegen_main"),
                f"--delay_model={delay_model}",
                "--clock_period_ps=476837",
                "--ffi_fallback_delay_ps=500",
                f"--module_name={module_name}",
                "--reset=reset",
                "--use_system_verilog=true",
                str(opt_ir_path),
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    )
    return sv_path


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
        tt_sdc = _rel_dir_ref(SRC_DIR / "tt_um_proppy_megabytebeat.sdc", out_dir)
        (out_dir / "tt_config_extra.json").write_text(
            json.dumps(
                {
                    "USE_SLANG": True,
                    "VERILOG_FILES": [
                        _rel_dir_ref(SRC_DIR / "tt_um_proppy_megabytebeat.v", out_dir),
                        _rel_dir_ref(SRC_DIR / "pwm_audio.v", out_dir),
                        _rel_dir_ref(core_v, out_dir),
                        _rel_dir_ref(adc_col.wrapper_verilog, out_dir),
                        _rel_dir_ref(dac_col.wrapper_verilog, out_dir),
                    ],
                    "EXTRA_LEFS": extra_lefs,
                    "EXTRA_GDS": extra_gds,
                    "EXTRA_LIBS": extra_libs,
                    "EXTRA_VERILOG_MODELS": extra_v,
                    "EXTRA_SPICE_MODELS": extra_spice,
                    "PNR_SDC_FILE": tt_sdc,
                    "SIGNOFF_SDC_FILE": tt_sdc,
                    "IGNORE_DISCONNECTED_MODULES": [adc_col.cell_name, dac_col.cell_name],
                    "EXTRA_EXCLUDED_CELLS": DLY_EXCLUDED_CELLS,
                    "RSZ_DONT_TOUCH_RX": RSZ_DONT_TOUCH_RX,
                },
                indent=2,
            )
            + "\n"
        )
    else:
        ws_sdc = _rel_dir_ref(SRC_DIR / "megabytebeat_chip_core.sdc", out_dir)
        (out_dir / "waferspace_config.yaml").write_text(
            "\n".join(
                [
                    "# Stock LibreLane overlay for wafer-space/gf180mcu-project-template",
                    "USE_SLANG: true",
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
                    f"PNR_SDC_FILE: {ws_sdc}",
                    f"SIGNOFF_SDC_FILE: {ws_sdc}",
                    "IGNORE_DISCONNECTED_MODULES:",
                    f"  - {adc_col.cell_name}",
                    f"  - {dac_col.cell_name}",
                    "EXTRA_EXCLUDED_CELLS:",
                    *[f"  - {c}" for c in DLY_EXCLUDED_CELLS],
                    f"RSZ_DONT_TOUCH_RX: '{RSZ_DONT_TOUCH_RX}'",
                    "RUN_HEURISTIC_DIODE_INSERTION: false",
                    "DESIGN_REPAIR_BUFFER_INPUT_PORTS: true",
                    "DESIGN_REPAIR_BUFFER_OUTPUT_PORTS: true",
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
    target: str = "waferspace",
    gl_netlist: Path | None = None,
    sim_tool: str | None = None,
) -> None:
    """Run cocotb RTL (and optional GL) verification via cocotb_tools.runner (default: Verilator)."""
    from cocotb_tools.runner import VerilatorControlFile, get_runner

    sim_name = (sim_tool or os.environ.get("SIM") or "verilator").lower()
    is_verilator = sim_name == "verilator"
    if is_verilator:
        ensure_modern_verilator()
    vlt_cfg = [VerilatorControlFile(TEST_DIR / "verilator.vlt")] if is_verilator else []
    vlt_args = ["--no-public-flat-rw", "--no-timing"] if is_verilator else []

    if target == "tinytapeout":
        rtl_sources = [
            *vlt_cfg,
            TEST_DIR / "tb.v",
            SRC_DIR / "tt_um_proppy_megabytebeat.v",
            SRC_DIR / "pwm_audio.v",
            core_v,
            adc_col.wrapper_verilog,
            dac_col.wrapper_verilog,
            adc_col.macro_verilog,
            dac_col.macro_verilog,
        ]
        hdl_top = "tb"
        build_params: dict[str, int] = {}
        test_mod = "test"
        rtl_build_dir = out_dir / "sim_build_rtl"
        rtl_test_dir = TEST_DIR
    else:
        rtl_sources = [
            *vlt_cfg,
            chip_core_v,
            core_v,
            adc_col.wrapper_verilog,
            dac_col.wrapper_verilog,
            adc_col.macro_verilog,
            dac_col.macro_verilog,
        ]
        hdl_top = "megabytebeat_chip_core"
        build_params = {"TRACKS": tracks}
        test_mod = "chip"
        rtl_build_dir = out_dir / "sim_build_rtl"
        rtl_test_dir = SRC_DIR

    runner = get_runner(sim_name)
    runner.build(
        sources=rtl_sources,
        hdl_toplevel=hdl_top,
        parameters=build_params,
        defines={"SIM": 1},
        build_args=vlt_args,
        timescale=("1ns", "1ps"),
        build_dir=rtl_build_dir,
        always=True,
    )
    rtl_env = {"R2R_SPICE": str(dac_col.pex_spice.resolve()), "SIM": sim_name}
    res_xml = runner.test(
        hdl_toplevel=hdl_top,
        test_module=test_mod,
        build_dir=rtl_build_dir,
        test_dir=rtl_test_dir,
        results_xml=rtl_build_dir / "results.xml",
        extra_env=rtl_env,
    )
    assert "<failure" not in Path(res_xml).read_text(), "RTL cocotb test failed!"

    if gl_netlist is not None and gl_netlist.exists():
        sc_v = gf180.SC_DIR / "verilog/gf180mcu_fd_sc_mcu7t5v0.v"
        prim_v = gf180.SC_DIR / "verilog/primitives.v"
        gl_test_dir = TEST_DIR if target == "tinytapeout" else SRC_DIR

        def _run_gl(tool_name: str, build_subdir: str, test_filter: str | None = None) -> None:
            tool_is_vlt = tool_name == "verilator"
            tool_cfg = [VerilatorControlFile(TEST_DIR / "verilator.vlt")] if tool_is_vlt else []
            tool_args = ["--no-public-flat-rw", "--no-timing"] if tool_is_vlt else []
            gl_sources = [
                *tool_cfg,
                *([TEST_DIR / "tb.v"] if target == "tinytapeout" else []),
                gl_netlist,
                adc_col.macro_verilog,
                dac_col.macro_verilog,
                prim_v,
                sc_v,
            ]
            gl_defines: dict[str, int | str] = {"SIM": 1, "FUNCTIONAL": 1}
            if not tool_is_vlt:
                gl_defines["UNIT_DELAY"] = "#1"
            if target == "tinytapeout":
                gl_defines["GL_TEST"] = 1
            if target == "tinytapeout" or gl_netlist.name.endswith(".pnl.v"):
                gl_defines["USE_POWER_PINS"] = 1
            gl_build_dir = out_dir / build_subdir
            runner_gl = get_runner(tool_name)
            prev_makeflags = os.environ.get("MAKEFLAGS")
            try:
                if tool_is_vlt:
                    os.environ["MAKEFLAGS"] = (
                        f"{prev_makeflags or ''} OPT_FAST=-O1 OPT_GLOBAL=-O1".strip()
                    )
                runner_gl.build(
                    sources=gl_sources,
                    hdl_toplevel=hdl_top,
                    defines=gl_defines,
                    build_args=tool_args,
                    timescale=("1ns", "1ps"),
                    build_dir=gl_build_dir,
                    always=True,
                )
            finally:
                if tool_is_vlt:
                    if prev_makeflags is None:
                        os.environ.pop("MAKEFLAGS", None)
                    else:
                        os.environ["MAKEFLAGS"] = prev_makeflags
            gl_env = {
                "R2R_SPICE": str(dac_col.pex_spice.resolve()),
                "GATES": "yes",
                "SIM": tool_name,
            }
            if (out_dir / "final/spef").is_dir():
                gl_env["SPEF_FILE"] = str((out_dir / "final/spef").resolve())
            res_gl_xml = runner_gl.test(
                hdl_toplevel=hdl_top,
                test_module=test_mod,
                build_dir=gl_build_dir,
                test_dir=gl_test_dir,
                results_xml=gl_build_dir / "results.xml",
                test_filter=test_filter,
                extra_env=gl_env,
            )
            assert "<failure" not in Path(res_gl_xml).read_text(), (
                f"GL cocotb test ({tool_name}) failed!"
            )

        _run_gl(sim_name, "sim_build_gl")
        # Companion 4-state Icarus GL pass catches reset/power-pin X-propagation missed by 2-state Verilator.
        if sim_name == "verilator" and shutil.which("iverilog"):
            icarus_filter = (
                "test_reset_and_standby_power_gating|test_end_to_end_mixed_signal"
                if target == "tinytapeout"
                else None
            )
            _run_gl("icarus", "sim_build_gl_icarus", test_filter=icarus_filter)


def _ensure_tt_def_template(out_dir: Path, tt_def_template: Path | None = None) -> Path:
    """Return path to official TinyTapeout GF180MCU 2x2 analog DEF template."""
    if tt_def_template is not None:
        return tt_def_template.resolve()
    local_tt = REPO_ROOT / "tt/tech/gf180mcuD/def/analog/tt_analog_2x2.def"
    if local_tt.exists():
        return local_tt
    cached = out_dir / "tt_analog_2x2.def"
    if not cached.exists():
        url = (
            "https://raw.githubusercontent.com/TinyTapeout/tt-support-tools/main/"
            "tech/gf180mcuD/def/analog/tt_analog_2x2.def"
        )
        urllib.request.urlretrieve(url, cached)
    return cached


def verify_hardened_views(
    out_dir: Path,
    target: str,
    tracks: int,
    adc_channels: int = 4,
    forbid_dly_cells: bool = True,
    run_klayout_drc: bool = True,
) -> None:
    """Audit post-PnR netlist, LEF PDN, metrics.json, and (for waferspace) full-chip KLayout DRC."""
    final_dir = out_dir / "final"
    nl_candidates = (
        list((final_dir / "pnl").glob("*.pnl.v"))
        + list((final_dir / "nl").glob("*.nl.v"))
        + list(out_dir.glob("*.v"))
    )
    assert nl_candidates, f"No post-PnR netlist found under {out_dir}"
    nl_path = nl_candidates[0]
    nl_txt = re.sub(r"//.*", "", nl_path.read_text())

    if target == "tinytapeout":
        adc_ports = {f"ua[{i}]" for i in range(adc_channels)}
        dac_ports = {f"ua[{adc_channels + i}]" for i in range(tracks)}
    else:
        adc_ports = {f"adc_in[{i}]" for i in range(adc_channels)}
        dac_ports = {f"dac_out[{i}]" for i in range(tracks)}
    all_ana_ports = adc_ports | dac_ports

    # Ensure no continuous `assign` statement aliases or drives any analog port.
    for m in re.finditer(r"^\s*assign\s+([^;]+);", nl_txt, re.M):
        stmt = m.group(1)
        for p in all_ana_ports:
            assert p not in stmt, f"Unexpected `assign` touching analog port {p}: assign {stmt};"

    analog_conns: dict[str, list[tuple[str, str, str]]] = {p: [] for p in all_ana_ports}
    adc_macros = 0
    dac_macros = 0
    dac_drivers = 0
    adc_drivers = 0
    adc_cmps = 0
    dly_cells = 0
    total_cells = 0

    inst_re = re.compile(
        r"^\s*([A-Za-z_]\w*)\s+(\\\S+|[A-Za-z_]\w*)\s*\((.*?)\);\s*$",
        re.M | re.S,
    )
    pin_re = re.compile(r"\.(\w+)\s*\(([^)]*)\)")
    dly_re = re.compile(r"gf180mcu_fd_sc_mcu7t5v0__dly[abcd]_[124]")

    for m in inst_re.finditer(nl_txt):
        cell_type, inst_name, port_body = m.group(1), m.group(2), m.group(3)
        if cell_type == "module" or not port_body.lstrip().startswith("."):
            continue
        total_cells += 1
        if cell_type == "r2r_ladder_4bit_sum":
            adc_macros += 1
        elif cell_type == "r2r_ladder_8bit":
            dac_macros += 1
        if inst_name.endswith(".u_buf") or (
            "__dac__" in inst_name and inst_name.endswith(".u_term")
        ):
            assert cell_type == "gf180mcu_fd_sc_mcu7t5v0__clkbuf_20", (
                f"DAC driver {inst_name} resized to {cell_type} (expected clkbuf_20)!"
            )
            dac_drivers += 1
        if inst_name.endswith(".u_inv") or (
            "__adc_ramp__" in inst_name and inst_name.endswith(".u_term")
        ):
            assert cell_type == "gf180mcu_fd_sc_mcu7t5v0__clkinv_12", (
                f"ADC driver {inst_name} resized to {cell_type} (expected clkinv_12)!"
            )
            adc_drivers += 1
        if inst_name.endswith(".u_cmp"):
            assert cell_type == "gf180mcu_fd_sc_mcu7t5v0__aoi21_1", (
                f"ADC comparator {inst_name} resized to {cell_type} (expected aoi21_1)!"
            )
            adc_cmps += 1
        if dly_re.fullmatch(cell_type):
            dly_cells += 1

        for pm in pin_re.finditer(port_body):
            pin_name, expr = pm.group(1), pm.group(2).strip()
            if expr.startswith("{") and expr.endswith("}"):
                expr = expr[1:-1]
            for tok in expr.split(","):
                net = tok.strip()
                if net in analog_conns:
                    analog_conns[net].append((cell_type, inst_name, pin_name))

    assert adc_macros == adc_channels, f"Expected {adc_channels} ADC macros, found {adc_macros}"
    assert dac_macros == tracks, f"Expected {tracks} DAC macros, found {dac_macros}"
    assert dac_drivers == 9 * tracks, (
        f"Expected {9 * tracks} DAC clkbuf_20 drivers, found {dac_drivers}"
    )
    assert adc_drivers == 5 * adc_channels, (
        f"Expected {5 * adc_channels} ADC clkinv_12 drivers, found {adc_drivers}"
    )
    assert adc_cmps == adc_channels, (
        f"Expected {adc_channels} ADC aoi21_1 comparators, found {adc_cmps}"
    )
    if forbid_dly_cells:
        assert dly_cells == 0, f"Found {dly_cells} excluded GF180 delay cells in {nl_path.name}!"

    for p in sorted(adc_ports):
        conns = analog_conns[p]
        assert (
            len(conns) == 1 and conns[0][0] == "r2r_ladder_4bit_sum" and conns[0][2] == "sum_in"
        ), f"Analog ADC port {p} is buffered or misconnected: {conns}"
    for p in sorted(dac_ports):
        conns = analog_conns[p]
        assert len(conns) == 1 and conns[0][0] == "r2r_ladder_8bit" and conns[0][2] == "out", (
            f"Analog DAC port {p} is buffered or misconnected: {conns}"
        )

    # Tiny Tapeout LEF PDN check: zero short Metal4 PDN rescue stubs (all stripes span full height).
    lef_candidates = list((final_dir / "lef").glob("*.lef")) + list(out_dir.glob("*.lef"))
    if target == "tinytapeout" and lef_candidates:
        lef_txt = lef_candidates[0].read_text()
        for pin in ("VPWR", "VGND"):
            pm = re.search(rf"PIN\s+{pin}\b(.*?)END\s+{pin}\b", lef_txt, re.S)
            assert pm is not None, f"Missing {pin} pin in {lef_candidates[0].name}"
            rects = re.findall(r"RECT\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)", pm.group(1))
            stubs = [r for r in rects if float(r[3]) <= 315.36]
            assert len(rects) >= 16 and not stubs, (
                f"{pin} LEF PDN check failed: {len(rects)} rects, {len(stubs)} short stubs ({stubs})"
            )

    # LibreLane final/metrics.json signoff gate.
    metrics_path = final_dir / "metrics.json"
    if metrics_path.exists():
        metrics = json.loads(metrics_path.read_text())
        for key in (
            "design__lvs_error__count",
            "magic__drc_error__count",
            "route__drc_errors",
            "design__xor_difference__count",
        ):
            assert key in metrics, f"Missing required signoff metric {key} in {metrics_path}"
            assert metrics[key] == 0, f"Signoff metric {key}={metrics[key]} (expected 0)!"
        if "klayout__drc_error__count" in metrics:
            assert metrics["klayout__drc_error__count"] == 0, (
                f"KLayout DRC violations: {metrics['klayout__drc_error__count']}"
            )
        sta_vios = {
            k: v
            for k, v in metrics.items()
            if k.startswith(("timing__setup_vio__count", "timing__hold_vio__count")) and v != 0
        }
        assert not sta_vios, f"Non-zero post-PnR STA timing violations: {sta_vios}"

    # Full-chip GF180MCU KLayout DRC on waferspace (TT runs KLayout DRC in precheck).
    gds_candidates = list((final_dir / "gds").glob("*.gds")) + list(out_dir.glob("*.gds"))
    if target == "waferspace" and run_klayout_drc and gds_candidates and shutil.which("klayout"):
        chip_drc = analog.run_klayout_drc(
            gds_candidates[0],
            out_dir / "klayout_drc",
            extra_args=("--run_mode=deep", f"--thr={os.cpu_count() or 4}"),
        )
        assert chip_drc.clean, f"KLayout DRC failed on {gds_candidates[0]}:\n{chip_drc.log}"
        print(f"[verify] {target} full-chip KLayout DRC clean ({gds_candidates[0].name}).")

    print(
        f"[verify] {target} hardened views OK: {total_cells} cells, "
        f"{adc_macros}x ADC + {dac_macros}x DAC macros, {adc_drivers}x clkinv_12, "
        f"{dac_drivers}x clkbuf_20, {adc_cmps}x aoi21_1, 0 analog buffers, {dly_cells} dly cells."
    )


def _plan_dac_driver_placements(
    dac_paths: list[str],
    dac_xs: list[float] | tuple[float, ...],
    macro_y: float,
    rows: list[tuple[float, bool, float]],
    core_x0: float,
    core_x1: float,
) -> dict[str, dict]:
    """Place all `clkbuf_20` DAC drivers into disjoint, tapcell-free bays near each macro's pins."""
    cell_w = 34.72  # gf180mcu_fd_sc_mcu7t5v0__clkbuf_20 width (62 sites)
    site_w = 0.56
    tap_w = 1.12  # filltie width (2 sites)
    tap_pitch = 39.20

    bays: list[tuple[float, float, float, bool]] = []
    for row_y, is_fs, tap0 in rows:
        taps: list[tuple[float, float]] = []
        tx = tap0
        while tx + tap_w <= core_x1 + 1e-6:
            if tx >= core_x0 - 1e-6:
                taps.append((round(tx, 2), round(tx + tap_w, 2)))
            tx = round(tx + tap_pitch, 2)
        edges = [core_x0]
        for a, b in taps:
            edges.extend([a, b])
        edges.append(core_x1)
        for i in range(0, len(edges), 2):
            bx0, bx1 = round(edges[i], 2), round(edges[i + 1], 2)
            if bx1 - bx0 >= cell_w - 1e-6:
                bays.append((round(row_y, 2), bx0, bx1, is_fs))

    pins_order = [
        *[(f"gen_drv[{b}].u_buf", macro_y + 4.76 + 2.80 * b) for b in range(7, -1, -1)],
        ("u_term", macro_y + 1.96),
    ]
    prefixes = [p.removesuffix(".u_r2r") for p in dac_paths]

    def _best_in_bay(
        bx0: float, bx1: float, row_y: float, is_fs: bool, px: float, py: float
    ) -> tuple[float, float, float, str] | None:
        best = None
        n_steps = int(round((bx1 - cell_w - bx0) / site_w))
        orients = (("FS", False), ("S", True)) if is_fs else (("N", False), ("FN", True))
        for k in range(n_steps + 1):
            x = round(bx0 + k * site_w, 2)
            if x + cell_w > bx1 + 1e-6:
                continue
            for orient, flip_x in orients:
                zx0 = x + (cell_w - 33.13) if flip_x else x + 12.74
                zx1 = x + (cell_w - 12.74) if flip_x else x + 33.13
                dx = 0.0 if zx0 <= px <= zx1 else min(abs(px - zx0), abs(px - zx1))
                z_err = abs(0.5 * (zx0 + zx1) - px)
                dy = abs((row_y + 1.96) - py)
                cand = (round(dx + dy, 4), round(z_err, 4), x, orient)
                if best is None or cand < best:
                    best = cand
        return best

    used_bays: set[int] = set()
    placed: dict[tuple[int, str], tuple[str, float, float, str]] = {}
    for pin_suffix, py in pins_order:
        for ch, (prefix, mx) in enumerate(zip(prefixes, dac_xs, strict=True)):
            px = mx + 2.62
            best = None
            for bi, (row_y, bx0, bx1, is_fs) in enumerate(bays):
                if bi in used_bays:
                    continue
                pos = _best_in_bay(bx0, bx1, row_y, is_fs, px, py)
                if pos is None:
                    continue
                cand = (pos[0], pos[1], row_y, bi, pos)
                if best is None or cand < best:
                    best = cand
            assert best is not None, f"No tapcell-free bay for {prefix}.{pin_suffix}"
            _, _, row_y, bi, (_, _, x, orient) = best
            used_bays.add(bi)
            placed[(ch, pin_suffix)] = (f"{prefix}.{pin_suffix}", x, row_y, orient)

    ordered: dict[str, dict] = {}
    for ch in range(len(prefixes)):
        for pin_suffix, _ in pins_order:
            inst, x, row_y, orient = placed[(ch, pin_suffix)]
            ordered[inst] = {"location": [x, row_y], "orientation": orient}
    return ordered


def run_stock_librelane(
    out_dir: Path,
    chip_core_v: Path,
    adc_col,
    dac_col,
    core_v: Path,
    tracks: int,
    target: str = "waferspace",
    dockerized: bool = False,
    tt_def_template: Path | None = None,
) -> Path:
    """Emit stock LibreLane Classic config (wafer.space or TTGF26c 2x2) and invoke CLI."""
    adc_paths = find_adc_macro_instances(core_v, core_inst="u_core")
    dac_paths = find_dac_macro_instances(core_v, core_inst="u_core")
    if target == "tinytapeout":
        ll_cfg = out_dir / "librelane_tt_config.json"
        def_tmpl = _ensure_tt_def_template(out_dir, tt_def_template)
        v_files = [
            _rel_dir_ref(SRC_DIR / "tt_um_proppy_megabytebeat.v", out_dir),
            _rel_dir_ref(SRC_DIR / "pwm_audio.v", out_dir),
            _rel_dir_ref(core_v, out_dir),
            _rel_dir_ref(adc_col.wrapper_verilog, out_dir),
            _rel_dir_ref(dac_col.wrapper_verilog, out_dir),
        ]
        # Single row of pure-passive macros at y=7.84 directly above ua[5:0] (x=108.9..327.3 um).
        # Uniform 10.08 um edge-to-edge gaps with 4.8 um horizontal halo leave 0.48 um < 0.56 um
        # (zero stdcell sites between macros) so pdngen never drops short rescue Metal4 stubs,
        # while DAC1 at x=35.84 clears the leftmost VPWR/VGND full stripe pair (18.88/22.18 um).
        adc_xs = (310.24, 272.16, 234.08, 196.00)
        adc_insts = {
            path: {"location": [x, 7.84], "orientation": "N"}
            for path, x in zip(adc_paths, adc_xs, strict=True)
        }
        dac_xs = (115.92, 35.84)
        dac_insts = {
            path: {"location": [x, 7.84], "orientation": "N"}
            for path, x in zip(dac_paths, dac_xs, strict=True)
        }
        # Skip row y=39.20 (double-density macro-boundary tapcells); place all 2x9 clkbuf_20
        # drivers into staggered 38.08 um tapcell-free bays directly above DAC0/DAC1.
        tt_drv_rows = [
            (round(43.12 + 3.92 * r, 2), r % 2 == 1, 22.96 if r % 2 == 0 else 42.56)
            for r in range(8)
        ]
        drv_placements = _plan_dac_driver_placements(
            dac_paths, dac_xs, macro_y=7.84, rows=tt_drv_rows, core_x0=4.48, core_x1=706.72
        )
        tt_sdc = _rel_dir_ref(SRC_DIR / "tt_um_proppy_megabytebeat.sdc", out_dir)
        cfg: dict = {
            "DESIGN_NAME": "tt_um_proppy_megabytebeat",
            "USE_SLANG": True,
            "VERILOG_FILES": v_files,
            "PNR_SDC_FILE": tt_sdc,
            "SIGNOFF_SDC_FILE": tt_sdc,
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
            "MANUAL_GLOBAL_PLACEMENTS": drv_placements,
            "FP_DEF_TEMPLATE": _rel_dir_ref(def_tmpl, out_dir),
            "DIE_AREA": [0, 0, 711.20, 325.36],
            "FP_SIZING": "absolute",
            "VDD_PIN": "VPWR",
            "GND_PIN": "VGND",
            "DEFAULT_CORNER": "nom_tt_025C_3v30",
            "STA_CORNERS": [
                "nom_tt_025C_3v30",
                "nom_ss_125C_3v00",
                "nom_ff_n40C_3v60",
                "min_tt_025C_3v30",
                "min_ss_125C_3v00",
                "min_ff_n40C_3v60",
                "max_tt_025C_3v30",
                "max_ss_125C_3v00",
                "max_ff_n40C_3v60",
            ],
            "LIB": {
                "*_tt_025C_3v30": [
                    "pdk_dir::libs.ref/gf180mcu_fd_sc_mcu7t5v0/lib/gf180mcu_fd_sc_mcu7t5v0__tt_025C_3v30.lib"
                ],
                "*_ss_125C_3v00": [
                    "pdk_dir::libs.ref/gf180mcu_fd_sc_mcu7t5v0/lib/gf180mcu_fd_sc_mcu7t5v0__ss_125C_3v00.lib"
                ],
                "*_ff_n40C_3v60": [
                    "pdk_dir::libs.ref/gf180mcu_fd_sc_mcu7t5v0/lib/gf180mcu_fd_sc_mcu7t5v0__ff_n40C_3v60.lib"
                ],
            },
            "RT_MAX_LAYER": "Metal4",
            "PDN_MULTILAYER": False,
            "PDN_VPITCH": 38.87,
            "FP_MACRO_HORIZONTAL_HALO": 4.8,
            "FP_MACRO_VERTICAL_HALO": 4.0,
            "TOP_MARGIN_MULT": 1,
            "BOTTOM_MARGIN_MULT": 1,
            "LEFT_MARGIN_MULT": 6,
            "RIGHT_MARGIN_MULT": 6,
            "SYNTH_STRATEGY": "AREA 0",
            "MAX_FANOUT_CONSTRAINT": 16,
            "CLOCK_PORT": "clk",
            "CLOCK_PERIOD": 476.8,
            "PL_TARGET_DENSITY_PCT": 65,
            "EXTRA_EXCLUDED_CELLS": DLY_EXCLUDED_CELLS,
            "RSZ_DONT_TOUCH_RX": RSZ_DONT_TOUCH_RX,
            "RUN_HEURISTIC_DIODE_INSERTION": False,
            "DESIGN_REPAIR_BUFFER_INPUT_PORTS": True,
            "DESIGN_REPAIR_BUFFER_OUTPUT_PORTS": True,
            "GRT_ALLOW_CONGESTION": True,
            "MAGIC_DEF_LABELS": False,
            "MAGIC_WRITE_LEF_PINONLY": True,
            "RUN_KLAYOUT_XOR": True,
            "RUN_KLAYOUT_DRC": False,
            "IGNORE_DISCONNECTED_MODULES": [adc_col.cell_name, dac_col.cell_name],
        }
    else:
        ll_cfg = out_dir / "librelane_core_config.json"
        v_files = [
            _rel_dir_ref(chip_core_v, out_dir),
            _rel_dir_ref(core_v, out_dir),
            _rel_dir_ref(adc_col.wrapper_verilog, out_dir),
            _rel_dir_ref(dac_col.wrapper_verilog, out_dir),
        ]
        # Grid-snapped (0.56 um X / 3.92 um Y) top-edge macro row with uniform 10.08 um gaps
        # and 4.8 um horizontal halo (0.48 um < 0.56 um => zero stranded sites between macros),
        # flush below core_y1 = 360.64 - 4 * 3.92 = 344.96 um.
        adc_xs = [round(35.84 + ch * 38.08, 2) for ch in range(len(adc_paths))]
        dac_x0 = round(adc_xs[-1] + 28.00 + 10.08, 2)
        dac_xs = [round(dac_x0 + t * 80.08, 2) for t in range(tracks)]
        die_w = round(math.ceil((dac_xs[-1] + 70.00 + 42.0) / 0.56) * 0.56, 2)
        die_h = 360.64
        adc_insts = {
            path: {"location": [x, 325.36], "orientation": "N"}
            for path, x in zip(adc_paths, adc_xs, strict=True)
        }
        dac_insts = {
            path: {"location": [x, 313.60], "orientation": "N"}
            for path, x in zip(dac_paths[:tracks], dac_xs, strict=True)
        }
        # Skip row y=301.84 (macro-boundary tapcells); place all tracks*9 clkbuf_20 drivers into
        # staggered 38.08 um tapcell-free bays directly below the top-edge DAC macro row.
        ws_drv_rows = [
            (round(297.92 - 3.92 * r, 2), r % 2 == 1, 26.32 if r % 2 == 0 else 45.92)
            for r in range(8)
        ]
        drv_placements = _plan_dac_driver_placements(
            dac_paths[:tracks],
            dac_xs,
            macro_y=313.60,
            rows=ws_drv_rows,
            core_x0=7.84,
            core_x1=round(die_w - 7.84, 2),
        )
        ws_sdc = _rel_dir_ref(SRC_DIR / "megabytebeat_chip_core.sdc", out_dir)
        cfg = {
            "DESIGN_NAME": "megabytebeat_chip_core",
            "USE_SLANG": True,
            "VERILOG_FILES": v_files,
            "PNR_SDC_FILE": ws_sdc,
            "SIGNOFF_SDC_FILE": ws_sdc,
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
            "MANUAL_GLOBAL_PLACEMENTS": drv_placements,
            "FP_MACRO_HORIZONTAL_HALO": 4.8,
            "FP_MACRO_VERTICAL_HALO": 4.0,
            "CLOCK_PORT": "clk",
            "CLOCK_PERIOD": 30517.5,
            "FP_SIZING": "absolute",
            "DIE_AREA": [0, 0, die_w, die_h],
            "PL_TARGET_DENSITY_PCT": 65,
            "EXTRA_EXCLUDED_CELLS": DLY_EXCLUDED_CELLS,
            "RSZ_DONT_TOUCH_RX": RSZ_DONT_TOUCH_RX,
            "RUN_HEURISTIC_DIODE_INSERTION": False,
            "DESIGN_REPAIR_BUFFER_INPUT_PORTS": True,
            "DESIGN_REPAIR_BUFFER_OUTPUT_PORTS": True,
            "GRT_ALLOW_CONGESTION": True,
            "RUN_KLAYOUT_XOR": True,
            "RUN_KLAYOUT_DRC": False,
            "IGNORE_DISCONNECTED_MODULES": [adc_col.cell_name, dac_col.cell_name],
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
    verify_hardened_views(out_dir, target=target, tracks=tracks, adc_channels=adc_col.channels)
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
    sim: bool = False,
    sim_tool: str | None = None,
    cosim: bool = False,
    spef_path: Path | None = None,
    run_ll: bool = False,
    dockerized: bool = False,
    tt_def_template: Path | None = None,
) -> None:
    import adc
    import dac

    tool_checks = tools.check_native_tools(("klayout", "magic"))
    bad_tools = [c for c in tool_checks if not c.ok]
    assert not bad_tools, (
        f"Native tool version mismatch vs LibreLane {tools.LIBRELANE_VERSION}: "
        + ", ".join(f"{c.name}={c.actual} (expected {c.expected})" for c in bad_tools)
    )

    if tracks is None:
        tracks = 2 if target == "tinytapeout" else 5
    out_dir.mkdir(parents=True, exist_ok=True)

    dac_dir = out_dir / "dac"
    dac_s, dac_cell, dac_pins = dac.build_dac()
    dac_gds, dac_lef, dac_lib, dac_spice = analog.emit_macro(dac_s, dac_cell, dac_pins, dac_dir)
    dac_drc = analog.run_klayout_drc(dac_gds, dac_dir / "drc")
    assert dac_drc.clean, f"KLayout DRC failed on {dac_gds}:\n{dac_drc.log}"
    print(f"  [Analog check | {dac_cell.name}] KLayout DRC clean.")
    dac_lvs = analog.run_klayout_lvs(dac_gds, dac_spice, dac_dir / "lvs", dac_cell.name, dac_pins)
    assert dac_lvs.ok, f"KLayout LVS failed on {dac_cell.name}:\n{dac_lvs.log}"
    print(f"  [Analog check | {dac_cell.name}] KLayout LVS matched.")
    dac._report_dac_corners(dac.simulate_dac_corners(dac_spice), label_prefix="DAC check")
    dac_pex = dac.extract_dac_pex(
        dac_gds, dac_dir, cell_name=dac_cell.name, bits=dac_bits, pins=dac_pins
    )
    assert dac_pex.ok, f"Magic PEX failed on {dac_cell.name}: {dac_pex}"
    print(
        f"  [Magic PEX | {dac_cell.name}] Extracted {dac_pex.spice_path} "
        f"(R={dac_pex.n_res}, C={dac_pex.n_cap}, bleed={dac_pex.n_float_bleed})"
    )
    dac._report_dac_corners(
        dac.simulate_dac_corners(dac_pex.spice_path), label_prefix="DAC PEX check"
    )
    dac_col = SimpleNamespace(
        cell_name=dac_cell.name,
        bits=dac_bits,
        gds=dac_gds,
        lef=dac_lef,
        lib=dac_lib,
        macro_verilog=SRC_DIR / f"{dac_cell.name}.v",
        wrapper_verilog=SRC_DIR / "sc_r2r_dac_8bit.v",
        spice=dac_spice,
        pex_spice=dac_pex.spice_path,
    )

    adc_dir = out_dir / "adc"
    adc_s, adc_cell, adc_pins = adc.build_adc()
    adc_gds, adc_lef, adc_lib, adc_spice = analog.emit_macro(adc_s, adc_cell, adc_pins, adc_dir)
    adc_drc = analog.run_klayout_drc(adc_gds, adc_dir / "drc")
    assert adc_drc.clean, f"KLayout DRC failed on {adc_gds}:\n{adc_drc.log}"
    print(f"  [Analog check | {adc_cell.name}] KLayout DRC clean.")
    adc_lvs = analog.run_klayout_lvs(adc_gds, adc_spice, adc_dir / "lvs", adc_cell.name, adc_pins)
    assert adc_lvs.ok, f"KLayout LVS failed on {adc_cell.name}:\n{adc_lvs.log}"
    print(f"  [Analog check | {adc_cell.name}] KLayout LVS matched.")
    adc._report_adc_corners(adc.simulate_adc_corners(adc_spice), label_prefix="ADC check")
    adc_pex = adc.extract_adc_pex(adc_gds, adc_dir, pins=adc_pins)
    assert adc_pex.ok, f"Magic PEX failed on {adc_cell.name}: {adc_pex}"
    print(
        f"  [Magic PEX | {adc_cell.name}] Extracted {adc_pex.spice_path} "
        f"(R={adc_pex.n_res}, C={adc_pex.n_cap}, bleed={adc_pex.n_float_bleed})"
    )
    adc._report_adc_corners(
        adc.simulate_adc_corners(adc_pex.spice_path), label_prefix="ADC PEX check"
    )
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
        pex_spice=adc_pex.spice_path,
    )

    core_top = "MegaBytebeat" if target == "tinytapeout" else "megabytebeat_core"
    core_module = "megabytebeat" if target == "tinytapeout" else "megabytebeat_core"
    core_v = compile_xls_core(
        dslx_path=SRC_DIR / "megabytebeat.x",
        top=core_top,
        module_name=core_module,
        out_dir=out_dir / "xls",
        check=True,
        delay_model="gf180mcu_3v3" if target == "tinytapeout" else "gf180mcu_5v0",
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
            target=target,
            dockerized=dockerized,
            tt_def_template=tt_def_template,
        )
        spef_dir = out_dir / "final/spef"
        if spef_dir.is_dir():
            in_nets = tuple(
                f"ua[{ch}]" if target == "tinytapeout" else f"adc_in[{ch}]"
                for ch in range(adc_channels)
            )
            out_nets = (
                [f"ua[{adc_channels + ch}]" for ch in range(tracks)]
                if target == "tinytapeout"
                else [f"dac_out[{ch}]" for ch in range(tracks)]
            )
            for spef in sorted(spef_dir.rglob("*.spef")):
                adc_spef_res = adc.check_adc_parasitics(
                    spef, channels=adc_channels, in_nets=in_nets
                )
                assert adc_spef_res.ok, "; ".join(adc_spef_res.violations)
                print(
                    f"  [ADC SPEF check | {spef.name}] OK: "
                    f"{adc.format_parasitics_summary(adc_spef_res.channels)}"
                )
                spef_res = dac.check_dac_parasitics(spef, channels=tracks, out_nets=out_nets)
                assert spef_res.ok, "; ".join(spef_res.violations)
                print(
                    f"  [SPEF check | {spef.name}] OK: "
                    f"{dac.format_parasitics_summary(spef_res.channels)}"
                )

    if sim:
        run_rtl_and_gl_sim(
            out_dir,
            chip_core_v,
            adc_col,
            dac_col,
            core_v,
            tracks,
            target=target,
            gl_netlist=gl_nl,
            sim_tool=sim_tool,
        )

    if cosim:
        ensure_modern_verilator()
        from cosim import run_eurorack_cosim

        run_eurorack_cosim(gen_dir=out_dir, tracks=tracks, spef_path=spef_path)

    dac_png = analog.render_gds(
        dac_col.gds, dac_dir / f"{dac_col.cell_name}.png", width=1800, height=1200
    )
    adc_png = analog.render_gds(
        adc_col.gds, adc_dir / f"{adc_col.cell_name}.png", width=1600, height=900
    )
    print(f"[chip] Rendered {dac_png} and {adc_png}")
    chip_name = "tt_um_proppy_megabytebeat" if target == "tinytapeout" else "megabytebeat_chip_core"
    chip_gds = out_dir / f"{chip_name}.gds"
    if not chip_gds.exists() and (out_dir / "final/gds").exists():
        chip_gds = next((out_dir / "final/gds").glob("*.gds"), chip_gds)
    if chip_gds.exists():
        chip_png = analog.render_gds(chip_gds, out_dir / f"{chip_name}.png", width=2400, height=960)
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
        "--sim",
        action="store_true",
        help="Run full-chip SystemVerilog RTL simulation (and GL simulation if netlist built)",
    )
    ap.add_argument(
        "--sim-tool",
        choices=["verilator", "icarus"],
        default=None,
        help="cocotb HDL simulator (default: $SIM or verilator)",
    )
    ap.add_argument(
        "--cosim",
        action="store_true",
        help="Run closed-loop Eurorack mixed-signal ngspice d_cosim + Verilator check",
    )
    ap.add_argument(
        "--spef",
        type=Path,
        default=None,
        help="Post-PnR SPEF file or directory to inject into --cosim (default: <out-dir>/final/spef)",
    )
    ap.add_argument(
        "--librelane",
        action="store_true",
        help="Run stock LibreLane hardening flow end-to-end (native Nix or --dockerized)",
    )
    ap.add_argument("--dockerized", action="store_true", help="Force Dockerized LibreLane run")
    ap.add_argument(
        "--tt-def-template",
        type=Path,
        default=None,
        help="Path to TinyTapeout analog DEF template (default: auto-fetch tt_analog_2x2.def)",
    )
    args = ap.parse_args()
    build_chip(
        target=args.target,
        tracks=args.tracks,
        adc_channels=args.adc_channels,
        adc_bits=args.adc_bits,
        dac_bits=args.dac_bits,
        out_dir=args.out_dir,
        sim=args.sim,
        sim_tool=args.sim_tool,
        cosim=args.cosim,
        spef_path=args.spef,
        run_ll=args.librelane,
        dockerized=args.dockerized,
        tt_def_template=args.tt_def_template,
    )
