#!/usr/bin/env python3
"""4-bit Ramp ADC build & collateral orchestrator (gdsfactory cell + XLS src/adc_ramp.x).

Builds physical collateral for `sc_adc_ramp_1ch_4b` (GDS, LEF, Liberty, SPICE, blackbox stub),
compiles checked-in `src/adc_ramp.x` -> `gen/adc/adc_ramp.v` via XLS C API (`flop_outputs=False`),
and runs XLS JIT, OpenSTA Liberty, KLayout DRC/LVS, and multi-corner ngspice checks.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
import re
import subprocess

import gdsfactory as gf
from gf180mcu.layers import LAYER
import xls

import analog

SRC_DIR = Path(__file__).resolve().parent
REPO_ROOT = SRC_DIR.parent
ADC_CANONICAL_PINS = ["VDD", "VSS", *[f"step[{b}]" for b in range(3, -1, -1)], "adc_in", "cmp"]


@dataclass
class Adc:
    cell_name: str
    channels: int
    bits: int
    width_um: float
    area_um2: float
    gds: Path
    lef: Path
    lib: Path
    cell_verilog: Path
    ramp_verilog: Path
    subsystem_verilog: Path
    spice: Path


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


def _compile_and_check_adc_xls(dslx_path: Path, out_dir: Path, check: bool) -> Path:
    dslx_src = dslx_path.read_text()
    ir = xls.convert_dslx_to_ir(
        dslx_src, str(dslx_path), "adc_ramp", additional_search_paths=[str(SRC_DIR)]
    )

    if check:
        pkg_fn = xls.parse_ir_package(ir)
        step_fn = pkg_fn.get_function(xls.mangle_dslx_name("adc_ramp", "adc_ramp_step"))
        jit = step_fn.to_jit()
        targets = [2, 5, 8, 11]
        st_val = xls.parse_typed_value(
            "(bits[4]:0, bits[4]:0, bits[4]:0, bits[4]:0, bits[4]:0, bits[4]:0)"
        )
        emitted_word = None
        for cyc in range(16):
            cmp_mask = sum((1 << i) for i in range(4) if cyc < targets[i])
            res = jit.run([st_val, xls.Value.make_ubits(4, cmp_mask)])
            st_val = res.get_element(0)
            if res.get_element(2).get_bits().to_uint64() == 1:
                emitted_word = res.get_element(3).get_bits().to_uint64()
        expected = sum(targets[i] << (i * 4) for i in range(4))
        assert emitted_word == expected, (
            f"JIT mismatch: got {emitted_word:#x}, expected {expected:#x}"
        )
        print(
            f"  [ADC check] XLS LLVM JIT verified 4x4b in-place crossing latch ({targets} -> {emitted_word:#x})."
        )

    proc_name = [line.split()[1].split("(")[0] for line in ir.splitlines() if "proc " in line][0]
    opt_ir = xls.optimize_ir(ir, top=proc_name)
    res = xls.parse_ir_package(opt_ir).schedule_and_codegen(
        with_delay_model=True,
        generator="pipeline",
        pipeline_stages=1,
        delay_model="unit",
        module_name="adc_ramp",
        reset="reset",
        reset_active_low=False,
        reset_asynchronous=False,
        reset_data_path=True,
        use_system_verilog=False,
        streaming_channel_data_suffix="",
        streaming_channel_valid_suffix="_vld",
        streaming_channel_ready_suffix="_rdy",
        flop_inputs=False,
        flop_outputs=False,
    )
    ramp_v = out_dir / "adc_ramp.v"
    ramp_v.write_text(res.get_verilog_text())
    return ramp_v


def _check_adc_ngspice(cir_path: Path, cell_name: str, bits: int, out_dir: Path) -> None:
    nsteps = (1 << bits) - 1
    wrapped_cir = out_dir / f"{cell_name}_ngspice.cir"
    wrapped_cir.write_text(analog.ngspice_subckt_wrapper(cir_path, cell_name, ADC_CANONICAL_PINS))
    for corner, rcorner in [("typical", "res_typical"), ("ff", "res_ff"), ("ss", "res_ss")]:
        cdir = out_dir / f"sim_{corner}"
        cdir.mkdir(parents=True, exist_ok=True)
        (cdir / "env.spice").write_text(analog.ngspice_env(corner, rcorner, includes=[wrapped_cir]))
        out = subprocess.check_output(
            ["ngspice", "-b", str(SRC_DIR / "adc_ramp.spice")],
            cwd=cdir,
            stderr=subprocess.STDOUT,
            text=True,
        )
        trips = [
            float(re.search(rf"^vtrip_{s}\s*=\s*([0-9.eE+-]+)", out, re.M).group(1))
            for s in range(nsteps)
        ]
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


def build_adc(
    channels: int = 4,
    bits: int = 4,
    driver: str = "clkinv_4",
    l_1r: float = 6.00,
    l_2r: float = 5.42,
    out_dir: Path = REPO_ROOT / "gen/adc",
    check: bool = False,
) -> Adc:
    if channels != 4 or bits != 4:
        raise ValueError(
            "Checked-in src/adc_ramp.x & src/sc_adc_ramp_1ch_4b.{lib,v} target 4ch x 4-bit ADC."
        )
    out_dir.mkdir(parents=True, exist_ok=True)
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

    gds = out_dir / f"{cell_name}.gds"
    lef = out_dir / f"{cell_name}.lef"
    lib = SRC_DIR / f"{cell_name}.lib"
    cell_v = SRC_DIR / f"{cell_name}.v"
    subsys_v = SRC_DIR / "adc_subsystem.v"
    spice = out_dir / f"{cell_name}.spice"

    named.write_gds(gds)
    lef.write_text(analog.export_lef(named, cell_name))
    spice.write_text(analog.infer_spice(cell, cell_name))
    ramp_v = _compile_and_check_adc_xls(SRC_DIR / "adc_ramp.x", out_dir, check)

    if check:
        analog.check_liberty_and_lef(lef, lib, cell_name, expected_area=area)
        print("  [ADC check] OpenROAD readLef + readLiberty & area match OK.")
        abut_gds = out_dir / f"{cell_name}_abutted.gds"
        analog.surround_with_neighbors(named).write_gds(abut_gds)
        analog.run_klayout_drc(abut_gds, out_dir / "drc")
        print("  [ADC check] KLayout DRC (4-sided abutted) clean.")
        analog.run_klayout_lvs(gds, spice, out_dir / "lvs_cmp", cell_name, ["--schematic_simplify"])
        print("  [ADC check] KLayout LVS (Component.get_netlist() vs GDS) matched.")
        cir = analog.run_klayout_lvs(
            gds, spice, out_dir / "lvs_unmerged", cell_name, ["--top_lvl_pins", "--net_only"]
        )
        _check_adc_ngspice(cir, cell_name, bits, out_dir)

    return Adc(
        cell_name=cell_name,
        channels=channels,
        bits=bits,
        width_um=w,
        area_um2=area,
        gds=gds,
        lef=lef,
        lib=lib,
        cell_verilog=cell_v,
        ramp_verilog=ramp_v,
        subsystem_verilog=subsys_v,
        spice=spice,
    )


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Build & verify 4-bit Ramp ADC subsystem.")
    ap.add_argument("--channels", type=int, default=4)
    ap.add_argument("--bits", type=int, default=4)
    ap.add_argument("--driver", default="clkinv_4")
    ap.add_argument("--l-1r", type=float, default=6.00)
    ap.add_argument("--l-2r", type=float, default=5.42)
    ap.add_argument("--out-dir", type=Path, default=REPO_ROOT / "gen/adc")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    print(
        build_adc(
            channels=args.channels,
            bits=args.bits,
            driver=args.driver,
            l_1r=args.l_1r,
            l_2r=args.l_2r,
            out_dir=args.out_dir,
            check=args.check,
        )
    )
