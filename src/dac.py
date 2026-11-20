#!/usr/bin/env python3
"""R-2R DAC sub-block: gdsfactory layout, collateral emission, DRC/LVS & ngspice check.

Checked-in static views live under `src/sc_r2r_dac_8bit.{lib,v}` and `src/dac_dc.spice`.
Can be run standalone (`python src/dac.py --check`) or imported by `src/chip.py`.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from pathlib import Path
import subprocess

import gdsfactory as gf
from gf180mcu.layers import LAYER

import analog

SRC_DIR = Path(__file__).resolve().parent
REPO_ROOT = SRC_DIR.parent
DAC_CANONICAL_PINS = ["VDD", "VSS", *[f"d[{b}]" for b in range(7, -1, -1)], "out"]


@dataclass
class Dac:
    cell_name: str
    bits: int
    width_um: float
    area_um2: float
    component: gf.Component = field(repr=False)
    lef: str = field(repr=False)
    spice: str = field(repr=False)
    lib: Path
    verilog: Path


@gf.cell
def build_dac_cell(
    bits: int = 8, driver: str = "clkbuf_12", l_1r: float = 10.00, l_2r: float = 9.81
) -> gf.Component:
    """Build 1-row (3.92um) CLASS CORE N-bit R_on-compensated R-2R DAC cell."""
    c = gf.Component()
    tiel = analog.std_cell("tiel")
    drv = analog.std_cell(driver)
    tie = analog.std_cell("filltie")
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
    analog.m2_bridge(
        c,
        r_tl.ports["ZN"].dx,
        r_tl.ports["ZN"].dy,
        r_cbt.ports["I"].dx,
        r_cbt.ports["I"].dy,
        y_route=0.84,
    )

    bay_term = analog.r2r_res_bay((l_2r, l_2r))
    r_bt = c.add_ref(bay_term)
    r_bt.dmove((x, 0))
    x = round(x + bay_term.info["width"], 3)
    f0_r0, f0_r1 = r_bt.ports["f0_r0"].dx, r_bt.ports["f0_r1"].dx
    f1_r0, n_prev_x = r_bt.ports["f1_r0"].dx, r_bt.ports["f1_r1"].dx
    analog.m1_strap_y(c, f0_r0, analog.Y_RES, y_drv)
    analog.m2_bridge(c, r_cbt.ports["Z"].dx, y_drv, f0_r0, y_drv, y_route=y_drv)
    analog.m1_hbar(c, f0_r1, f1_r0, analog.Y_RES)
    analog.m1_strap_y(c, n_prev_x, analog.Y_RES, analog.Y_M2_SPINE)

    for b in range(bits):
        c.add_ref(tie).dmove((x, 0))
        x = round(x + 1.12, 3)
        x_cb = x
        rcb = c.add_ref(drv)
        rcb.dmove((x, 0))
        x = round(x + drv_w, 3)
        analog.add_signal_pin(
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
        bay = analog.r2r_res_bay(lengths)
        rb = c.add_ref(bay)
        rb.dmove((x, 0))
        x = round(x + bay.info["width"], 3)
        p0_0, p0_1 = rb.ports["f0_r0"].dx, rb.ports["f0_r1"].dx
        p1_0, p1_1 = rb.ports["f1_r0"].dx, rb.ports["f1_r1"].dx

        analog.m1_strap_y(c, p0_0, analog.Y_RES, y_drv)
        analog.m2_bridge(c, rcb.ports["Z"].dx, y_drv, p0_0, y_drv, y_route=y_drv)
        analog.m1_hbar(c, p0_1, p1_0, analog.Y_RES)
        analog.m1_strap_y(c, p1_1, analog.Y_RES, analog.Y_M2_SPINE)
        analog.m2_bridge(c, n_prev_x, analog.Y_M2_SPINE, p1_1, analog.Y_M2_SPINE)

        if b + 1 < bits:
            p2_0, p2_1 = rb.ports["f2_r0"].dx, rb.ports["f2_r1"].dx
            analog.m1_hbar(c, p1_1, p2_0, analog.Y_RES)
            analog.m1_strap_y(c, p2_1, analog.Y_RES, analog.Y_M2_SPINE)
            n_prev_x = p2_1
        else:
            analog.m1_strap_y(c, p1_1, analog.Y_RES, 2.75)
            analog.add_signal_pin(
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


def _run_dac_dc(
    subckt_path: Path, cell_name: str, corner: str, rcorner: str, sim_dir: Path
) -> list[float]:
    sim_dir.mkdir(parents=True, exist_ok=True)
    wrapped = sim_dir / f"{cell_name}_ngspice.cir"
    wrapped.write_text(analog.ngspice_subckt_wrapper(subckt_path, cell_name, DAC_CANONICAL_PINS))
    (sim_dir / "env.spice").write_text(analog.ngspice_env(corner, rcorner, includes=[wrapped]))
    subprocess.run(
        ["ngspice", "-b", str(SRC_DIR / "dac_dc.spice")],
        cwd=sim_dir,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    dat_file = sim_dir / "dac_dc.dat"
    return [float(p[1]) for line in dat_file.read_text().splitlines() if (p := line.split())]


def _check_dac_ngspice(
    spice_path: Path, cir_path: Path, cell_name: str, bits: int, out_dir: Path
) -> None:
    ncodes = 1 << bits
    typ_extracted: list[float] | None = None
    for corner, rcorner in [("typical", "res_typical"), ("ff", "res_ff"), ("ss", "res_ss")]:
        samples = _run_dac_dc(cir_path, cell_name, corner, rcorner, out_dir / f"sim_{corner}")
        if corner == "typical":
            typ_extracted = samples
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

    assert typ_extracted is not None
    typ_inferred = _run_dac_dc(
        spice_path, cell_name, "typical", "res_typical", out_dir / "sim_inferred_typical"
    )
    lsb = (typ_extracted[-1] - typ_extracted[0]) / (ncodes - 1)
    max_delta_lsb = max(abs(a - b) / lsb for a, b in zip(typ_inferred, typ_extracted))
    assert max_delta_lsb < 0.1, (
        f"DAC inferred vs extracted mismatch: {max_delta_lsb:.4f} LSB >= 0.1 LSB"
    )
    print(
        f"  [DAC check | inf/ext] {ncodes}-code inferred vs KLayout-extracted OK: "
        f"max|dV|={max_delta_lsb * lsb * 1e3:.3f} mV ({max_delta_lsb:.4f} LSB)"
    )


def build_dac(
    bits: int = 8,
    driver: str = "clkbuf_12",
    l_1r: float = 10.00,
    l_2r: float = 9.81,
) -> Dac:
    if bits != 8:
        raise ValueError("Checked-in src/sc_r2r_dac_8bit.{lib,v} targets 8-bit DAC.")
    cell_name = "sc_r2r_dac_8bit"
    cell = build_dac_cell(bits=bits, driver=driver, l_1r=l_1r, l_2r=l_2r)
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

    return Dac(
        cell_name=cell_name,
        bits=bits,
        width_um=w,
        area_um2=area,
        component=named,
        lef=analog.export_lef(named, cell_name),
        spice=analog.infer_spice(cell, cell_name),
        lib=SRC_DIR / f"{cell_name}.lib",
        verilog=SRC_DIR / f"{cell_name}.v",
    )


def check_dac(dac: Dac, gds: Path, lef: Path, spice: Path, out_dir: Path) -> None:
    analog.check_liberty_and_lef(lef, dac.lib, dac.cell_name, expected_area=dac.area_um2)
    print("  [DAC check] OpenROAD readLef + readLiberty & area match OK.")
    abut_gds = out_dir / f"{dac.cell_name}_abutted.gds"
    analog.surround_with_neighbors(dac.component).write_gds(abut_gds)
    analog.run_klayout_drc(abut_gds, out_dir / "drc")
    print("  [DAC check] KLayout DRC (4-sided abutted) clean.")
    cir = analog.run_klayout_lvs(gds, spice, out_dir / "lvs", dac.cell_name, ["--top_lvl_pins"])
    print("  [DAC check] KLayout LVS (Component.get_netlist() vs GDS) matched.")
    _check_dac_ngspice(spice, cir, dac.cell_name, dac.bits, out_dir)


def main() -> None:
    ap = argparse.ArgumentParser(description="Build & verify sc_r2r_dac_8bit sub-block.")
    ap.add_argument("--bits", type=int, default=8)
    ap.add_argument("--driver", default="clkbuf_12")
    ap.add_argument("--l-1r", type=float, default=10.00)
    ap.add_argument("--l-2r", type=float, default=9.81)
    ap.add_argument("--out-dir", type=Path, default=REPO_ROOT / "gen/dac")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()

    dac = build_dac(args.bits, args.driver, args.l_1r, args.l_2r)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    gds = args.out_dir / f"{dac.cell_name}.gds"
    lef = args.out_dir / f"{dac.cell_name}.lef"
    spice = args.out_dir / f"{dac.cell_name}.spice"
    dac.component.write_gds(gds)
    lef.write_text(dac.lef)
    spice.write_text(dac.spice)
    if args.check:
        check_dac(dac, gds, lef, spice, args.out_dir)
    print(dac)


if __name__ == "__main__":
    main()
