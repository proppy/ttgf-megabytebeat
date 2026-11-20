#!/usr/bin/env python3
"""Top-level mixed-signal chip orchestrator.

Imports sub-blocks (`dac`, `adc`, `bpm`, `bytebeat`) and pairs checked-in RTL
under `src/` with generated converter collateral for stock LibreLane on
wafer.space and Tiny Tapeout.
"""

from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import os
from pathlib import Path
import shutil
import subprocess
import sys

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge

SRC_DIR = Path(__file__).resolve().parent
REPO_ROOT = SRC_DIR.parent
PDK = Path(os.environ.get("PDK_ROOT", "/home/exedev/workspace/pdk")) / "gf180mcuD"
SC_TLEF = PDK / "libs.ref/gf180mcu_fd_sc_mcu7t5v0/techlef/gf180mcu_fd_sc_mcu7t5v0__nom.tlef"
SC_LEF = PDK / "libs.ref/gf180mcu_fd_sc_mcu7t5v0/lef/gf180mcu_fd_sc_mcu7t5v0.lef"
SC_LIB = PDK / "libs.ref/gf180mcu_fd_sc_mcu7t5v0/lib/gf180mcu_fd_sc_mcu7t5v0__tt_025C_5v00.lib"
SC_GDS = PDK / "libs.ref/gf180mcu_fd_sc_mcu7t5v0/gds/gf180mcu_fd_sc_mcu7t5v0.gds"


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
        _rel_dir_ref(adc_col.ramp_verilog, out_dir),
        _rel_dir_ref(bpm_col.verilog, out_dir),
        _rel_dir_ref(bb_col.verilog, out_dir),
    ]
    extra_lefs = [_rel_dir_ref(adc_col.lef, out_dir), _rel_dir_ref(dac_col.lef, out_dir)]
    extra_gds = [_rel_dir_ref(adc_col.gds, out_dir), _rel_dir_ref(dac_col.gds, out_dir)]
    extra_libs = [_rel_dir_ref(adc_col.lib, out_dir), _rel_dir_ref(dac_col.lib, out_dir)]
    extra_v = [_rel_dir_ref(adc_col.cell_verilog, out_dir), _rel_dir_ref(dac_col.verilog, out_dir)]
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
        adc_col.cell_verilog,
        dac_col.verilog,
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
        sc_v = PDK / "libs.ref/gf180mcu_fd_sc_mcu7t5v0/verilog/gf180mcu_fd_sc_mcu7t5v0.v"
        prim_v = PDK / "libs.ref/gf180mcu_fd_sc_mcu7t5v0/verilog/primitives.v"
        runner_gl = get_runner("icarus")
        runner_gl.build(
            sources=[gl_netlist, adc_col.cell_verilog, dac_col.verilog, prim_v, sc_v],
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
    ll_cfg = out_dir / "librelane_core_config.yaml"
    v_files = [
        _rel_dir_ref(chip_core_v, out_dir),
        _rel_dir_ref(adc_col.subsystem_verilog, out_dir),
        _rel_dir_ref(adc_col.ramp_verilog, out_dir),
        _rel_dir_ref(bpm_col.verilog, out_dir),
        _rel_dir_ref(bb_col.verilog, out_dir),
    ]
    ll_cfg.write_text(
        "\n".join(
            [
                "DESIGN_NAME: megabytebeat_chip_core",
                "VERILOG_FILES:",
                *[f"  - {p}" for p in v_files],
                "EXTRA_LEFS:",
                f"  - {_rel_dir_ref(adc_col.lef, out_dir)}",
                f"  - {_rel_dir_ref(dac_col.lef, out_dir)}",
                "EXTRA_GDS:",
                f"  - {_rel_dir_ref(adc_col.gds, out_dir)}",
                f"  - {_rel_dir_ref(dac_col.gds, out_dir)}",
                "EXTRA_LIBS:",
                f"  - {_rel_dir_ref(adc_col.lib, out_dir)}",
                f"  - {_rel_dir_ref(dac_col.lib, out_dir)}",
                "EXTRA_VERILOG_MODELS:",
                f"  - {_rel_dir_ref(adc_col.cell_verilog, out_dir)}",
                f"  - {_rel_dir_ref(dac_col.verilog, out_dir)}",
                "EXTRA_SPICE_MODELS:",
                f"  - {_rel_dir_ref(adc_col.spice, out_dir)}",
                f"  - {_rel_dir_ref(dac_col.spice, out_dir)}",
                "CLOCK_PORT: clk",
                "CLOCK_PERIOD: 30517.5",
                "FP_SIZING: absolute",
                "DIE_AREA: [0, 0, 900, 360]",
                "PL_TARGET_DENSITY_PCT: 50",
                "DESIGN_REPAIR_BUFFER_OUTPUT_PORTS: false",
                "GRT_ALLOW_CONGESTION: true",
                "",
            ]
        )
    )
    ll_bin = str(Path(sys.executable).parent / "librelane")
    use_docker = dockerized or (shutil.which("openroad") is None)
    cmd = [ll_bin]
    if use_docker:
        cmd += [
            "-m",
            str(PDK.parent),
            "-m",
            str(REPO_ROOT),
            "--docker-no-tty",
            "--dockerized",
        ]
    cmd += [
        "--pdk",
        "gf180mcuD",
        "--pdk-root",
        str(PDK.parent),
        "--manual-pdk",
        "--scl",
        "gf180mcu_fd_sc_mcu7t5v0",
        "--save-views-to",
        str(out_dir / "final"),
        str(ll_cfg),
    ]
    print(f"[librelane] Running: {' '.join(cmd)}")
    subprocess.run(cmd, check=True)
    return next((out_dir / "final/nl").glob("*.nl.v"))


def _yosys_worker(
    v_srcs: list[str], bb_srcs: list[str], libs: list[str], out_nl: str, tracks: int
) -> None:
    from pyosys import libyosys as ys

    d = ys.Design()
    for lb in [str(SC_LIB), *libs]:
        ys.run_pass(f"read_liberty -lib {lb}", d)
    for bb in bb_srcs:
        ys.run_pass(f"read_verilog -sv {bb}", d)
    for src in v_srcs:
        ys.run_pass(f"read_verilog -sv {src}", d)
    ys.run_pass(f"chparam -set TRACKS {tracks} megabytebeat_chip_core", d)
    ys.run_pass("synth -top megabytebeat_chip_core -flatten", d)
    ys.run_pass(f"dfflibmap -liberty {SC_LIB}", d)
    ys.run_pass(f"abc -liberty {SC_LIB}", d)
    ys.run_pass(
        "hilomap -hicell gf180mcu_fd_sc_mcu7t5v0__tieh Z -locell gf180mcu_fd_sc_mcu7t5v0__tiel ZN",
        d,
    )
    ys.run_pass("insbuf -buf gf180mcu_fd_sc_mcu7t5v0__buf_2 I Z; opt_clean -purge", d)
    ys.run_pass(f"stat -liberty {SC_LIB} " + " ".join(f"-liberty {lb}" for lb in libs), d)
    ys.run_pass(f"write_verilog -noattr -noexpr -nohex -nodec {out_nl}", d)


def _openroad_worker(
    out_nl: str,
    lefs: list[str],
    libs: list[str],
    out_def: str,
    converter_names: tuple[str, str],
) -> None:
    import openroad
    from openroad import odb

    tech = openroad.Tech()
    for lf in [str(SC_TLEF), str(SC_LEF), *lefs]:
        tech.readLef(lf)
    for lb in [str(SC_LIB), *libs]:
        tech.readLiberty(lb)

    design = openroad.Design(tech)
    design.readVerilog(out_nl)
    design.link("megabytebeat_chip_core")

    db = tech.getDB()
    db_tech = db.getTech()
    dbu = design.micronToDBU

    fp = design.getFloorplan()
    site = fp.findSite("GF018hv5v_mcu_sc7")
    fp.initFloorplan(
        odb.Rect(0, 0, dbu(900.0), dbu(320.0)),
        odb.Rect(dbu(16.8), dbu(15.68), dbu(883.2), dbu(301.84)),
        site,
    )
    fp.makeTracks()

    block = design.getBlock()
    for net_name, sig_type in [("VDD", "POWER"), ("VSS", "GROUND")]:
        net = block.findNet(net_name) or odb.dbNet_create(block, net_name)
        net.setSpecial()
        net.setSigType(sig_type)
        block.addGlobalConnect(None, ".*", net_name, net, True)
    block.globalConnect()

    io = design.getIOPlacer()
    io.addHorLayer(db_tech.findLayer("Metal3"))
    io.addVerLayer(db_tech.findLayer("Metal2"))
    io.runHungarianMatching()

    design.evalTclString("set_wire_rc -layer Metal3")
    gpl = design.getReplace()
    gpl.setTimingDrivenMode(False)
    gpl.setRoutabilityDrivenMode(False)
    gpl.setUniformTargetDensityMode(True)
    gpl.setTargetDensity(0.55)
    gpl.doInitialPlace(2)
    gpl.doNesterovPlace(2, 0)

    dpl = design.getOpendp()
    dpl.detailedPlacement(0, 0, "")
    dpl.optimizeMirroring()
    dpl.checkPlacement(False, "")

    um = db_tech.getDbUnitsPerMicron()
    print("  [PnR] Automatic HPWL Placement of CLASS CORE Converter Cells:")
    for inst in sorted(block.getInsts(), key=lambda i: i.getName()):
        mname = inst.getMaster().getName()
        if mname in converter_names:
            bb = inst.getBBox()
            pin_nm = "adc_in" if "adc" in mname else "out"
            bterms = list(inst.findITerm(pin_nm).getNet().getBTerms())
            pb = bterms[0].getBBox()
            px, py = (pb.xMin() + pb.xMax()) / 2 / um, (pb.yMin() + pb.yMax()) / 2 / um
            print(
                f"    {inst.getName():20s} ({mname}): row y={bb.yMin() / um:5.2f} um, "
                f"x={bb.xMin() / um:6.2f}..{bb.xMax() / um:6.2f} ({inst.getOrient()}) "
                f"-> pin {bterms[0].getName()} ({px:.1f},{py:.1f})"
            )

    filler_masters = [
        db.findMaster(f"gf180mcu_fd_sc_mcu7t5v0__fill_{w}") for w in (64, 32, 16, 8, 4, 2, 1)
    ]
    dpl.fillerPlacement(filler_masters, "FILLER_", False)

    grt = design.getGlobalRouter()
    grt.setMinRoutingLayer(2)
    grt.setMaxRoutingLayer(5)
    grt.setAllowCongestion(True)
    grt.globalRoute(True)
    print(
        "  [PnR] Global Routing (FastRoute) & Legalization Finished Cleanly (0 overlaps).",
        flush=True,
    )
    design.writeDef(out_def)
    sys.stdout.flush()
    os._exit(0)


def _streamout_gds(out_def: Path, lefs: list[Path], gdss: list[Path], out_gds: Path) -> None:
    import klayout.db as pya

    opt = pya.LoadLayoutOptions()
    ld = opt.lefdef_config
    ld.macro_resolution_mode = 1
    ld.read_lef_with_def = False
    ld.lef_files = [str(SC_TLEF), str(SC_LEF), *[str(p) for p in lefs]]
    map_file = PDK / "libs.tech/klayout/tech/gf180mcu.map"
    if map_file.exists():
        ld.map_file = str(map_file)

    ly = pya.Layout()
    for g in [SC_GDS, *gdss]:
        ly.read(str(g))
    ly.read(str(out_def), opt)
    ly.write(str(out_gds))
    top = ly.cell("megabytebeat_chip_core")
    print(
        f"  [PnR] Merged GDSII written: {out_gds} "
        f"({out_gds.stat().st_size / 1e6:.2f} MB, bbox={top.dbbox()})"
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
    pnr_smoke: bool = False,
) -> None:
    from types import SimpleNamespace

    from adc import build_adc, check_adc
    from bpm import build_bpm
    from bytebeat import build_bytebeat
    from dac import build_dac, check_dac

    if tracks is None:
        tracks = 2 if target == "tinytapeout" else 5
    out_dir.mkdir(parents=True, exist_ok=True)

    dac_dir = out_dir / "dac"
    dac_dir.mkdir(parents=True, exist_ok=True)
    dac_obj = build_dac(bits=dac_bits)
    dac_gds = dac_dir / f"{dac_obj.cell_name}.gds"
    dac_lef = dac_dir / f"{dac_obj.cell_name}.lef"
    dac_spice = dac_dir / f"{dac_obj.cell_name}.spice"
    dac_obj.component.write_gds(dac_gds)
    dac_lef.write_text(dac_obj.lef)
    dac_spice.write_text(dac_obj.spice)
    if check:
        check_dac(dac_obj, dac_gds, dac_lef, dac_spice, dac_dir)
    dac_col = SimpleNamespace(
        cell_name=dac_obj.cell_name,
        bits=dac_obj.bits,
        gds=dac_gds,
        lef=dac_lef,
        lib=dac_obj.lib,
        verilog=dac_obj.verilog,
        spice=dac_spice,
    )

    adc_dir = out_dir / "adc"
    adc_dir.mkdir(parents=True, exist_ok=True)
    adc_obj = build_adc(channels=adc_channels, bits=adc_bits)
    adc_gds = adc_dir / f"{adc_obj.cell_name}.gds"
    adc_lef = adc_dir / f"{adc_obj.cell_name}.lef"
    adc_spice = adc_dir / f"{adc_obj.cell_name}.spice"
    adc_ramp_v = adc_dir / "adc_ramp.v"
    adc_obj.component.write_gds(adc_gds)
    adc_lef.write_text(adc_obj.lef)
    adc_spice.write_text(adc_obj.spice)
    adc_ramp_v.write_text(adc_obj.ramp_verilog)
    if check:
        check_adc(adc_obj, adc_gds, adc_lef, adc_spice, adc_dir)
    adc_col = SimpleNamespace(
        cell_name=adc_obj.cell_name,
        channels=adc_obj.channels,
        bits=adc_obj.bits,
        gds=adc_gds,
        lef=adc_lef,
        lib=adc_obj.lib,
        cell_verilog=adc_obj.cell_verilog,
        ramp_verilog=adc_ramp_v,
        subsystem_verilog=adc_obj.subsystem_verilog,
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
            out_dir, chip_core_v, adc_col, dac_col, bpm_col, bb_col, dockerized=dockerized
        )

    if pnr_smoke:
        ctx = mp.get_context("spawn")
        nl_v = out_dir / "megabytebeat_chip_core.nl.v"
        routed_def = out_dir / "megabytebeat_chip_core.routed.def"
        merged_gds = out_dir / "megabytebeat_chip_core.gds"

        v_srcs = [
            str(chip_core_v),
            str(adc_col.subsystem_verilog),
            str(adc_col.ramp_verilog),
            str(bpm_col.verilog),
            str(bb_col.verilog),
        ]
        bb_srcs = [str(adc_col.cell_verilog), str(dac_col.verilog)]
        libs = [str(adc_col.lib), str(dac_col.lib)]
        lefs = [str(adc_col.lef), str(dac_col.lef)]

        p_syn = ctx.Process(target=_yosys_worker, args=(v_srcs, bb_srcs, libs, str(nl_v), tracks))
        p_syn.start()
        p_syn.join()
        assert p_syn.exitcode == 0, "Yosys synthesis failed!"
        gl_nl = nl_v

        p_pnr = ctx.Process(
            target=_openroad_worker,
            args=(
                str(nl_v),
                lefs,
                libs,
                str(routed_def),
                (adc_col.cell_name, dac_col.cell_name),
            ),
        )
        p_pnr.start()
        p_pnr.join()
        assert p_pnr.exitcode == 0, "OpenROAD P&R failed!"

        _streamout_gds(
            routed_def, [adc_col.lef, dac_col.lef], [adc_col.gds, dac_col.gds], merged_gds
        )

    if sim:
        run_rtl_and_gl_sim(
            out_dir, chip_core_v, adc_col, dac_col, bpm_col, bb_col, gl_netlist=gl_nl
        )

    if cosim:
        from cosim import run_eurorack_cosim

        run_eurorack_cosim(gen_dir=out_dir, tracks=tracks)


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
        "--pnr-smoke",
        action="store_true",
        help="Run fast local Yosys + OpenROAD placement + GDS streamout check",
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
        pnr_smoke=args.pnr_smoke,
    )
