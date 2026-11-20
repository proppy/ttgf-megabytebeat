#!/usr/bin/env python3
"""4-bit Ramp ADC build & collateral orchestrator (gdsfactory cell + XLS src/adc_ramp.x).

Builds physical collateral for `sc_adc_ramp_1ch_4b` (GDS, LEF, Liberty, SPICE, blackbox stub),
compiles checked-in `src/adc_ramp.x` -> `gen/adc/adc_ramp.v` via XLS C API (`flop_outputs=False`),
and runs XLS JIT, OpenSTA Liberty, KLayout DRC/LVS, and multi-corner ngspice checks.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import io
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request

import gdsfactory as gf
from gf180mcu.layers import LAYER
import xls

import analog

SRC_DIR = Path(__file__).resolve().parent
REPO_ROOT = SRC_DIR.parent
ADC_CANONICAL_PINS = ["VDD", "VSS", *[f"step[{b}]" for b in range(3, -1, -1)], "adc_in", "cmp"]
XLS_VERSION = "v0.0.0-11174-g32dc47058"


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


@dataclass
class Adc:
    cell_name: str
    channels: int
    bits: int
    width_um: float
    area_um2: float
    component: gf.Component = field(repr=False)
    lef: str = field(repr=False)
    spice: str = field(repr=False)
    ramp_ir: str = field(repr=False)
    ramp_verilog: str = field(repr=False)
    lib: Path
    cell_verilog: Path
    subsystem_verilog: Path


@gf.cell
def build_adc_cell(
    bits: int = 4, driver: str = "clkinv_4", l_1r: float = 6.00, l_2r: float = 5.42
) -> gf.Component:
    """Build 1-row (3.92um) CLASS CORE resistive-summing ramp ADC channel cell."""
    c = gf.Component()
    tieh = analog.std_cell("tieh")
    inv = analog.std_cell(driver)
    tie = analog.std_cell("filltie")
    cb1 = analog.std_cell("clkbuf_1")
    inv_w = float(inv.info["width"])

    x = 0.0
    c.add_ref(tie).dmove((x, 0))
    x = round(x + 1.12, 3)
    r_th = c.add_ref(tieh)
    r_th.dmove((x, 0))
    x = round(x + 2.24, 3)
    r_invt = c.add_ref(inv)
    r_invt.dmove((x, 0))
    x = round(x + inv_w, 3)
    analog.m2_bridge(
        c, r_th.ports["Z"].dx, r_th.ports["Z"].dy, r_invt.ports["I"].dx, r_invt.ports["I"].dy
    )

    bay_term = analog.r2r_res_bay((l_2r, l_2r))
    r_bt = c.add_ref(bay_term)
    r_bt.dmove((x, 0))
    x = round(x + bay_term.info["width"], 3)
    f0_r0, f0_r1 = r_bt.ports["f0_r0"].dx, r_bt.ports["f0_r1"].dx
    f1_r0, n_prev_x = r_bt.ports["f1_r0"].dx, r_bt.ports["f1_r1"].dx
    analog.m1_strap_y(c, f0_r0, analog.Y_RES, analog.Y_M2_DRV)
    analog.m2_bridge(
        c,
        r_invt.ports["ZN"].dx,
        r_invt.ports["ZN"].dy,
        f0_r0,
        analog.Y_M2_DRV,
        y_route=analog.Y_M2_DRV,
    )
    analog.m1_hbar(c, f0_r1, f1_r0, analog.Y_RES)
    analog.m1_strap_y(c, n_prev_x, analog.Y_RES, analog.Y_M2_SPINE)

    for b in range(bits):
        c.add_ref(tie).dmove((x, 0))
        x = round(x + 1.12, 3)
        x_inv = x
        ri = c.add_ref(inv)
        ri.dmove((x, 0))
        x = round(x + inv_w, 3)
        analog.add_signal_pin(
            c,
            f"step[{b}]",
            "INPUT",
            x_inv + 0.70,
            1.765,
            x_inv + 2.65,
            2.150,
            target_port=ri.ports["I"],
        )

        bay = analog.r2r_res_bay((l_2r, l_2r, l_1r))
        rb = c.add_ref(bay)
        rb.dmove((x, 0))
        x = round(x + bay.info["width"], 3)
        p0_0, p0_1 = rb.ports["f0_r0"].dx, rb.ports["f0_r1"].dx
        p1_0, p1_1 = rb.ports["f1_r0"].dx, rb.ports["f1_r1"].dx
        p2_0, p2_1 = rb.ports["f2_r0"].dx, rb.ports["f2_r1"].dx

        analog.m1_strap_y(c, p0_0, analog.Y_RES, analog.Y_M2_DRV)
        analog.m2_bridge(
            c,
            ri.ports["ZN"].dx,
            ri.ports["ZN"].dy,
            p0_0,
            analog.Y_M2_DRV,
            y_route=analog.Y_M2_DRV,
        )
        analog.m1_hbar(c, p0_1, p1_0, analog.Y_RES)
        analog.m1_hbar(c, p1_1, p2_0, analog.Y_RES)
        analog.m1_strap_y(c, p1_1, analog.Y_RES, analog.Y_M2_SPINE)
        analog.m2_bridge(c, n_prev_x, analog.Y_M2_SPINE, p1_1, analog.Y_M2_SPINE)

        if b + 1 < bits:
            n_prev_x = p2_1
            analog.m1_strap_y(c, n_prev_x, analog.Y_RES, analog.Y_M2_SPINE)
        else:
            vsum_x, adc_x = p2_0, p2_1
            s_adc = analog.m1_strap_y(c, adc_x, analog.Y_RES, 3.00, w=0.44)
            analog.add_signal_pin(
                c,
                "adc_in",
                "INPUT",
                adc_x - 0.22,
                2.35,
                adc_x + 0.22,
                3.15,
                target_port=s_adc.ports["e2"],
            )

    c.add_ref(tie).dmove((x, 0))
    x = round(x + 1.12, 3)
    x_cb = x
    rcb = c.add_ref(cb1)
    rcb.dmove((x, 0))
    x = round(x + 3.36, 3)
    c.add_ref(tie).dmove((x, 0))
    x = round(x + 1.12, 3)

    analog.m1_strap_y(c, vsum_x, analog.Y_RES, rcb.ports["I"].dy)
    analog.m2_bridge(c, vsum_x, rcb.ports["I"].dy, rcb.ports["I"].dx, rcb.ports["I"].dy)
    analog.add_signal_pin(
        c, "cmp", "OUTPUT", x_cb + 2.565, 1.605, x_cb + 2.930, 3.320, target_port=rcb.ports["Z"]
    )

    c.add_label("VDD", position=(0.56, 3.92), layer=LAYER.metal1_label)
    c.add_label("VSS", position=(0.56, 0.00), layer=LAYER.metal1_label)
    c.info["width"] = x
    return c


def _compile_adc_xls(dslx_path: Path) -> tuple[str, str]:
    xls_bin = find_or_fetch_xls_bin_dir()
    ir = subprocess.run(
        [
            str(xls_bin / "ir_converter_main"),
            f"--dslx_path={dslx_path.parent}",
            "--top=adc_ramp",
            str(dslx_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout

    with tempfile.TemporaryDirectory() as tmp:
        ir_path = Path(tmp) / "adc_ramp.ir"
        opt_ir_path = Path(tmp) / "adc_ramp.opt.ir"
        ir_path.write_text(ir)
        opt_ir = subprocess.run(
            [str(xls_bin / "opt_main"), str(ir_path)],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        opt_ir_path.write_text(opt_ir)
        verilog = subprocess.run(
            [
                str(xls_bin / "codegen_main"),
                "--generator=pipeline",
                "--pipeline_stages=1",
                "--delay_model=unit",
                "--module_name=adc_ramp",
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
    return ir, verilog


def _check_adc_xls(dslx_path: Path, ir: str) -> None:
    xls_bin = find_or_fetch_xls_bin_dir()
    subprocess.run(
        [
            str(xls_bin / "interpreter_main"),
            "--compare=jit",
            f"--dslx_path={dslx_path.parent}",
            str(dslx_path),
        ],
        check=True,
    )
    fn_only_ir = ir.split("\ntop proc ")[0] + "\n"
    pkg_fn = xls.parse_ir_package(fn_only_ir)
    step_fn = pkg_fn.get_function("__adc_ramp__adc_ramp__step")
    jit = step_fn.to_jit()
    targets = [2, 5, 8, 11]
    st_val = xls.parse_typed_value(
        "(bits[4]:0, bits[4]:0, [bits[4]:0, bits[4]:0, bits[4]:0, bits[4]:0])"
    )
    emitted_word = None
    for cyc in range(16):
        cmp_mask = sum((1 << i) for i in range(4) if cyc < targets[i])
        res = jit.run([st_val, xls.Value.make_ubits(4, cmp_mask)])
        st_val = res.get_element(0)
        if res.get_element(2).get_bits().to_uint64() == 1:
            emitted_word = res.get_element(3).get_bits().to_uint64()
    expected = sum(targets[i] << (i * 4) for i in range(4))
    assert emitted_word == expected, f"JIT mismatch: got {emitted_word:#x}, expected {expected:#x}"
    print(
        f"  [ADC check] XLS proc --compare=jit + LLVM JIT verified 4x4b in-place crossing "
        f"latch ({targets} -> {emitted_word:#x})."
    )


def _run_adc_dc(
    subckt_path: Path, cell_name: str, nsteps: int, corner: str, rcorner: str, sim_dir: Path
) -> list[float]:
    sim_dir.mkdir(parents=True, exist_ok=True)
    wrapped = sim_dir / f"{cell_name}_ngspice.cir"
    wrapped.write_text(analog.ngspice_subckt_wrapper(subckt_path, cell_name, ADC_CANONICAL_PINS))
    (sim_dir / "env.spice").write_text(analog.ngspice_env(corner, rcorner, includes=[wrapped]))
    out = subprocess.check_output(
        ["ngspice", "-b", str(SRC_DIR / "adc_ramp.spice")],
        cwd=sim_dir,
        stderr=subprocess.STDOUT,
        text=True,
    )
    return [
        float(re.search(rf"^vtrip_{s}\s*=\s*([0-9.eE+-]+)", out, re.M).group(1))
        for s in range(nsteps)
    ]


def _check_adc_ngspice(
    spice_path: Path, cir_path: Path, cell_name: str, bits: int, out_dir: Path
) -> None:
    nsteps = (1 << bits) - 1
    typ_extracted: list[float] | None = None
    for corner, rcorner in [("typical", "res_typical"), ("ff", "res_ff"), ("ss", "res_ss")]:
        trips = _run_adc_dc(cir_path, cell_name, nsteps, corner, rcorner, out_dir / f"sim_{corner}")
        if corner == "typical":
            typ_extracted = trips
        alsb = (trips[-1] - trips[0]) / (nsteps - 1)
        adnls = [(trips[s + 1] - trips[s]) / alsb - 1.0 for s in range(nsteps - 1)]
        ainls = [(trips[s] - (trips[0] + s * alsb)) / alsb for s in range(nsteps)]
        assert all(trips[s + 1] > trips[s] for s in range(nsteps - 1)), (
            f"ADC non-monotonic in {corner}!"
        )
        assert max(map(abs, adnls)) < 0.25 and max(map(abs, ainls)) < 0.50, (
            f"ADC linearity failed in {corner}"
        )
        print(
            f"  [ADC check | {corner:7s}] {nsteps + 1}-step ngspice OK: "
            f"Vtrip={trips[0]:.3f}V..{trips[-1]:.3f}V, max|DNL|={max(map(abs, adnls)):.4f} LSB, "
            f"max|INL|={max(map(abs, ainls)):.4f} LSB"
        )

    assert typ_extracted is not None
    typ_inferred = _run_adc_dc(
        spice_path, cell_name, nsteps, "typical", "res_typical", out_dir / "sim_inferred_typical"
    )
    alsb = (typ_extracted[-1] - typ_extracted[0]) / (nsteps - 1)
    max_delta_lsb = max(abs(a - b) / alsb for a, b in zip(typ_inferred, typ_extracted))
    assert max_delta_lsb < 0.1, (
        f"ADC inferred vs extracted mismatch: {max_delta_lsb:.4f} LSB >= 0.1 LSB"
    )
    print(
        f"  [ADC check | inf/ext] {nsteps}-step inferred vs KLayout-extracted OK: "
        f"max|dV|={max_delta_lsb * alsb * 1e3:.3f} mV ({max_delta_lsb:.4f} LSB)"
    )


def build_adc(
    channels: int = 4,
    bits: int = 4,
    driver: str = "clkinv_4",
    l_1r: float = 6.00,
    l_2r: float = 5.42,
) -> Adc:
    if channels != 4 or bits != 4:
        raise ValueError(
            "Checked-in src/adc_ramp.x & src/sc_adc_ramp_1ch_4b.{lib,v} target 4ch x 4-bit ADC."
        )
    cell_name = "sc_adc_ramp_1ch_4b"
    cell = build_adc_cell(bits=bits, driver=driver, l_1r=l_1r, l_2r=l_2r)
    w = float(cell.info["width"])
    area = w * analog.ROW_H

    named = gf.Component(cell_name)
    named.add_ref(cell)
    named.flatten()
    for p in cell.ports:
        np = named.add_port(name=p.name, port=p)
        np.info["direction"] = p.info["direction"]
        np.info["rect"] = p.info["rect"]
    named.info["width"] = w

    ramp_ir, ramp_verilog = _compile_adc_xls(SRC_DIR / "adc_ramp.x")
    return Adc(
        cell_name=cell_name,
        channels=channels,
        bits=bits,
        width_um=w,
        area_um2=area,
        component=named,
        lef=analog.export_lef(named, cell_name),
        spice=analog.infer_spice(cell, cell_name),
        ramp_ir=ramp_ir,
        ramp_verilog=ramp_verilog,
        lib=SRC_DIR / f"{cell_name}.lib",
        cell_verilog=SRC_DIR / f"{cell_name}.v",
        subsystem_verilog=SRC_DIR / "adc_subsystem.v",
    )


def check_adc(adc: Adc, gds: Path, lef: Path, spice: Path, out_dir: Path) -> None:
    _check_adc_xls(SRC_DIR / "adc_ramp.x", adc.ramp_ir)
    analog.check_liberty_and_lef(lef, adc.lib, adc.cell_name, expected_area=adc.area_um2)
    print("  [ADC check] OpenROAD readLef + readLiberty & area match OK.")
    abut_gds = out_dir / f"{adc.cell_name}_abutted.gds"
    analog.surround_with_neighbors(adc.component).write_gds(abut_gds)
    analog.run_klayout_drc(abut_gds, out_dir / "drc")
    print("  [ADC check] KLayout DRC (4-sided abutted) clean.")
    analog.run_klayout_lvs(gds, spice, out_dir / "lvs_cmp", adc.cell_name, ["--schematic_simplify"])
    print("  [ADC check] KLayout LVS (Component.get_netlist() vs GDS) matched.")
    cir = analog.run_klayout_lvs(
        gds, spice, out_dir / "lvs_unmerged", adc.cell_name, ["--top_lvl_pins", "--net_only"]
    )
    _check_adc_ngspice(spice, cir, adc.cell_name, adc.bits, out_dir)


def main() -> None:
    ap = argparse.ArgumentParser(description="Build & verify 4-bit Ramp ADC subsystem.")
    ap.add_argument("--channels", type=int, default=4)
    ap.add_argument("--bits", type=int, default=4)
    ap.add_argument("--driver", default="clkinv_4")
    ap.add_argument("--l-1r", type=float, default=6.00)
    ap.add_argument("--l-2r", type=float, default=5.42)
    ap.add_argument("--out-dir", type=Path, default=REPO_ROOT / "gen/adc")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()

    adc = build_adc(args.channels, args.bits, args.driver, args.l_1r, args.l_2r)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    gds = args.out_dir / f"{adc.cell_name}.gds"
    lef = args.out_dir / f"{adc.cell_name}.lef"
    spice = args.out_dir / f"{adc.cell_name}.spice"
    ramp_v = args.out_dir / "adc_ramp.v"
    adc.component.write_gds(gds)
    lef.write_text(adc.lef)
    spice.write_text(adc.spice)
    ramp_v.write_text(adc.ramp_verilog)
    if args.check:
        check_adc(adc, gds, lef, spice, args.out_dir)
    print(adc)


if __name__ == "__main__":
    main()
