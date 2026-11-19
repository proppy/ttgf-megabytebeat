#!/usr/bin/env python3
"""R-2R DAC sub-block: gdsfactory layout, collateral emission, DRC/LVS & ngspice check.

Checked-in static views live under `src/sc_r2r_dac_8bit.{lib,v}` and `src/dac_dc.spice`.
Can be run standalone (`python chip/dac.py --check`) or imported by `chip/chip.py`.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
import subprocess

import gdsfactory as gf
from gf180mcu.layers import LAYER

from r2r import (
    ROW_H,
    Y_M2_SPINE,
    Y_RES,
    add_signal_pin,
    check_liberty_and_lef,
    export_lef,
    infer_spice_from_component,
    m1_hbar,
    m1_strap_y,
    m2_bridge,
    r2r_res_bay,
    run_klayout_drc,
    run_klayout_lvs,
    std_cell,
    surround_with_neighbors,
    write_ngspice_env,
    write_ngspice_subckt_wrapper,
)

BASE_DIR = Path(__file__).resolve().parent
SRC_DIR = BASE_DIR.parent / "src"
DAC_CANONICAL_PINS = ["VDD", "VSS", *[f"d[{b}]" for b in range(7, -1, -1)], "out"]


@dataclass
class DacCollateral:
    cell_name: str
    bits: int
    width_um: float
    area_um2: float
    gds: Path
    lef: Path
    lib: Path
    verilog: Path
    spice: Path


@gf.cell
def build_dac_cell(
    bits: int = 8, driver: str = "clkbuf_12", l_1r: float = 10.00, l_2r: float = 9.81
) -> gf.Component:
    """Build 1-row (3.92um) CLASS CORE N-bit R_on-compensated R-2R DAC cell."""
    c = gf.Component()
    tiel = std_cell("tiel")
    drv = std_cell(driver)
    tie = std_cell("filltie")
    drv_w = float(drv.info["width"])
    y_drv = round(drv.ports["Z"].dy, 3)

    x = 0.0
    c.add_ref(tie).dmove((x, 0))
    x = round(x + 1.12, 3)
    r_tl = c.add_ref(tiel)
    r_tl.dmove((x, 0))
    x = round(x + 2.24, 3)
    r_cbt = c.add_ref(drv)
    r_cbt.dmove((x, 0))
    x = round(x + drv_w, 3)
    m2_bridge(
        c,
        r_tl.ports["ZN"].dx,
        r_tl.ports["ZN"].dy,
        r_cbt.ports["I"].dx,
        r_cbt.ports["I"].dy,
        y_route=0.84,
    )

    bay_term = r2r_res_bay((l_2r, l_2r))
    r_bt = c.add_ref(bay_term)
    r_bt.dmove((x, 0))
    x = round(x + bay_term.info["width"], 3)
    f0_r0, f0_r1 = r_bt.ports["f0_r0"].dx, r_bt.ports["f0_r1"].dx
    f1_r0, n_prev_x = r_bt.ports["f1_r0"].dx, r_bt.ports["f1_r1"].dx
    m1_strap_y(c, f0_r0, Y_RES, y_drv)
    m2_bridge(c, r_cbt.ports["Z"].dx, y_drv, f0_r0, y_drv, y_route=y_drv)
    m1_hbar(c, f0_r1, f1_r0, Y_RES)
    m1_strap_y(c, n_prev_x, Y_RES, Y_M2_SPINE)

    for b in range(bits):
        c.add_ref(tie).dmove((x, 0))
        x = round(x + 1.12, 3)
        x_cb = x
        rcb = c.add_ref(drv)
        rcb.dmove((x, 0))
        x = round(x + drv_w, 3)
        add_signal_pin(
            c,
            f"d[{b}]",
            "INPUT",
            x_cb + 1.00,
            1.765,
            x_cb + 4.00,
            2.150,
            target_port=rcb.ports["I"],
        )

        lengths = (l_2r, l_2r, l_1r) if b + 1 < bits else (l_2r, l_2r)
        bay = r2r_res_bay(lengths)
        rb = c.add_ref(bay)
        rb.dmove((x, 0))
        x = round(x + bay.info["width"], 3)
        p0_0, p0_1 = rb.ports["f0_r0"].dx, rb.ports["f0_r1"].dx
        p1_0, p1_1 = rb.ports["f1_r0"].dx, rb.ports["f1_r1"].dx

        m1_strap_y(c, p0_0, Y_RES, y_drv)
        m2_bridge(c, rcb.ports["Z"].dx, y_drv, p0_0, y_drv, y_route=y_drv)
        m1_hbar(c, p0_1, p1_0, Y_RES)
        m1_strap_y(c, p1_1, Y_RES, Y_M2_SPINE)
        m2_bridge(c, n_prev_x, Y_M2_SPINE, p1_1, Y_M2_SPINE)

        if b + 1 < bits:
            p2_0, p2_1 = rb.ports["f2_r0"].dx, rb.ports["f2_r1"].dx
            m1_hbar(c, p1_1, p2_0, Y_RES)
            m1_strap_y(c, p2_1, Y_RES, Y_M2_SPINE)
            n_prev_x = p2_1
        else:
            m1_strap_y(c, p1_1, Y_RES, 2.75)
            add_signal_pin(
                c,
                "out",
                "OUTPUT",
                p1_1 - 0.14,
                2.35,
                p1_1 + 0.14,
                3.15,
                target_port=rb.ports["f1_r1"],
            )

    c.add_ref(tie).dmove((x, 0))
    x = round(x + 1.12, 3)
    c.add_label("VDD", position=(0.56, 3.92), layer=LAYER.metal1_label)
    c.add_label("VSS", position=(0.56, 0.00), layer=LAYER.metal1_label)
    c.info["width"] = x
    return c


def _check_dac_ngspice(cir_path: Path, cell_name: str, bits: int, out_dir: Path) -> None:
    ncodes = 1 << bits
    wrapped_cir = write_ngspice_subckt_wrapper(
        cir_path, cell_name, DAC_CANONICAL_PINS, out_dir / f"{cell_name}_ngspice.cir"
    )
    for corner, rcorner in [("typical", "res_typical"), ("ff", "res_ff"), ("ss", "res_ss")]:
        cdir = out_dir / f"sim_{corner}"
        cdir.mkdir(parents=True, exist_ok=True)
        write_ngspice_env(cdir / "env.spice", corner, rcorner, includes=[wrapped_cir])
        subprocess.run(
            ["ngspice", "-b", str(SRC_DIR / "dac_dc.spice")],
            cwd=cdir,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        dat_file = cdir / "dac_dc.dat"
        samples = [float(p[1]) for line in dat_file.read_text().splitlines() if (p := line.split())]
        lsb = (samples[-1] - samples[0]) / (ncodes - 1)
        dnls = [(samples[k + 1] - samples[k]) / lsb - 1.0 for k in range(ncodes - 1)]
        inls = [(samples[k] - (samples[0] + k * lsb)) / lsb for k in range(ncodes)]
        assert all(samples[k + 1] > samples[k] for k in range(ncodes - 1)), (
            f"DAC non-monotonic in {corner}!"
        )
        assert max(map(abs, dnls)) < 0.25 and max(map(abs, inls)) < 0.50, (
            f"DAC linearity failed in {corner}"
        )
        print(
            f"  [DAC check | {corner:7s}] {ncodes}-code ngspice OK: "
            f"max|DNL|={max(map(abs, dnls)):.4f} LSB, max|INL|={max(map(abs, inls)):.4f} LSB"
        )


def build_dac(
    bits: int = 8,
    driver: str = "clkbuf_12",
    l_1r: float = 10.00,
    l_2r: float = 9.81,
    out_dir: Path = BASE_DIR / "gen/dac",
    check: bool = False,
) -> DacCollateral:
    if bits != 8:
        raise ValueError("Checked-in src/sc_r2r_dac_8bit.{lib,v} targets 8-bit DAC.")
    out_dir.mkdir(parents=True, exist_ok=True)
    cell_name = "sc_r2r_dac_8bit"
    cell = build_dac_cell(bits=bits, driver=driver, l_1r=l_1r, l_2r=l_2r)
    w = float(cell.info["width"])
    area = w * ROW_H

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
    spice = out_dir / f"{cell_name}.spice"
    lib = SRC_DIR / f"{cell_name}.lib"
    verilog = SRC_DIR / f"{cell_name}.v"

    named.write_gds(gds)
    export_lef(named, cell_name, lef)
    infer_spice_from_component(cell, cell_name, spice)
    print(f"[dac] Emitted {cell_name}: {w:.2f} x {ROW_H} um ({area:.1f} um^2) -> {out_dir}")

    if check:
        check_liberty_and_lef(lef, lib, cell_name, expected_area=area)
        print("  [DAC check] OpenROAD readLef + readLiberty & area match OK.")
        abut_gds = out_dir / f"{cell_name}_abutted.gds"
        surround_with_neighbors(named).write_gds(abut_gds)
        run_klayout_drc(abut_gds, out_dir / "drc")
        print("  [DAC check] KLayout DRC (4-sided abutted) clean.")
        cir = run_klayout_lvs(gds, spice, out_dir / "lvs", cell_name, ["--top_lvl_pins"])
        print("  [DAC check] KLayout LVS (Component.get_netlist() vs GDS) matched.")
        _check_dac_ngspice(cir, cell_name, bits, out_dir)

    return DacCollateral(
        cell_name=cell_name,
        bits=bits,
        width_um=w,
        area_um2=area,
        gds=gds,
        lef=lef,
        lib=lib,
        verilog=verilog,
        spice=spice,
    )


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Build & verify sc_r2r_dac_8bit sub-block.")
    ap.add_argument("--bits", type=int, default=8)
    ap.add_argument("--driver", default="clkbuf_12")
    ap.add_argument("--l-1r", type=float, default=10.00)
    ap.add_argument("--l-2r", type=float, default=9.81)
    ap.add_argument("--out-dir", type=Path, default=BASE_DIR / "gen/dac")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    build_dac(args.bits, args.driver, args.l_1r, args.l_2r, args.out_dir, args.check)
