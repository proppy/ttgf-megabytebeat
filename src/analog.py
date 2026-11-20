"""GF180MCU gdsfactory analog layout, collateral export, DRC/LVS, and ngspice helpers."""

from __future__ import annotations

import math
import os
from pathlib import Path
import re
import subprocess
import sys

import gdsfactory as gf
from gdsfactory.get_netlist import PortCenterMatcher
import gf180mcu  # noqa: F401 - activates gf180mcu PDK
from gf180mcu.layers import LAYER
import klayout.db as pya

PDK_ROOT = Path(os.environ.get("PDK_ROOT", "/home/exedev/workspace/pdk"))
GF180_DIR = PDK_ROOT / "gf180mcuD"
SC_DIR = GF180_DIR / "libs.ref/gf180mcu_fd_sc_mcu7t5v0"
SC_GDS = SC_DIR / "gds/gf180mcu_fd_sc_mcu7t5v0.gds"
SC_CDL = SC_DIR / "cdl/gf180mcu_fd_sc_mcu7t5v0.cdl"
SC_SPICE = SC_DIR / "spice/gf180mcu_fd_sc_mcu7t5v0.spice"
SC_TLEF = SC_DIR / "techlef/gf180mcu_fd_sc_mcu7t5v0__nom.tlef"
SC_LEF = SC_DIR / "lef/gf180mcu_fd_sc_mcu7t5v0.lef"
SC_LIB = SC_DIR / "lib/gf180mcu_fd_sc_mcu7t5v0__tt_025C_5v00.lib"
IO_SPICE = GF180_DIR / "libs.ref/gf180mcu_fd_io/spice/gf180mcu_fd_io.spice"
DRC_SCRIPT = GF180_DIR / "libs.tech/klayout/drc/run_drc.py"
LVS_SCRIPT = GF180_DIR / "libs.tech/klayout/lvs/run_lvs.py"
NG_MODELS = GF180_DIR / "libs.tech/ngspice"

SITE_W = 0.56
ROW_H = 3.92
Y_RES = 1.14
Y_M2_DRV = 2.45
Y_M2_SPINE = 3.15

_SAFE_PIN_CENTERS: dict[str, dict[str, tuple[float, float]]] = {
    "tieh": {"Z": (1.40, 2.85)},
    "tiel": {"ZN": (1.41, 1.09)},
    "clkinv_4": {"I": (0.90, 1.955), "ZN": (3.645, 2.45)},
    "clkinv_8": {"I": (1.20, 1.955), "ZN": (3.62, 2.55)},
    "clkbuf_8": {"I": (1.50, 1.95), "Z": (9.80, 1.95)},
    "clkbuf_12": {"I": (2.00, 2.03), "Z": (14.28, 2.00)},
    "clkbuf_16": {"I": (2.00, 1.95), "Z": (18.435, 2.00)},
    "clkbuf_1": {"I": (1.15, 1.945), "Z": (2.75, 2.45)},
}


@gf.cell
def std_cell(name: str) -> gf.Component:
    """Import foundry 7t5v0 cell, attach DRC-safe electrical Ports, and strip internal labels."""
    raw = gf.import_gds(SC_GDS, cellname=f"gf180mcu_fd_sc_mcu7t5v0__{name}")
    out = gf.Component()
    out.add_ref(raw)
    out.flatten()
    for pname, center in _SAFE_PIN_CENTERS.get(name, {}).items():
        out.add_port(
            pname,
            center=center,
            width=0.28,
            orientation=0,
            layer=LAYER.metal1,
            port_type="electrical",
        )
    ly = out.kcl.layout
    for lp in [(34, 10), (21, 10), (204, 10), (30, 10), (22, 10)]:
        li = ly.find_layer(*lp)
        if li is not None:
            out.kdb_cell.shapes(li).clear()
    li_pr = ly.find_layer(0, 0)
    out.info["width"] = round(out.kdb_cell.bbox_per_layer(li_pr).width() * ly.dbu, 3)
    return out


@gf.cell
def via_m1_m2() -> gf.Component:
    c = gf.Component()
    c.add_polygon([(-0.13, -0.13), (0.13, -0.13), (0.13, 0.13), (-0.13, 0.13)], layer=LAYER.via1)
    for lyr in (LAYER.metal1, LAYER.metal2):
        c.add_polygon([(-0.20, -0.20), (0.20, -0.20), (0.20, 0.20), (-0.20, 0.20)], layer=lyr)
    c.add_port(
        "e1", center=(0, 0), width=0.40, orientation=90, layer=LAYER.metal1, port_type="electrical"
    )
    c.add_port(
        "e2", center=(0, 0), width=0.40, orientation=0, layer=LAYER.metal2, port_type="electrical"
    )
    return c


@gf.cell
def horiz_hres_6p0(w: float = 1.00, length: float = 10.00) -> gf.Component:
    """Bare horizontal ppolyf_u_1k_6p0 unit finger with r0/r1 electrical Ports."""
    c = gf.Component()
    hl, hw = round(length / 2, 3), round(w / 2, 3)
    poly_end = round(hl + 0.61, 3)
    c.add_polygon([(-hl, -hw), (hl, -hw), (hl, hw), (-hl, hw)], layer=LAYER.res_mk)
    c.add_polygon(
        [(-poly_end, -hw), (poly_end, -hw), (poly_end, hw), (-poly_end, hw)], layer=LAYER.poly2
    )
    c.add_polygon(
        [
            (-(hl + 0.10), -(hw + 0.28)),
            (hl + 0.10, -(hw + 0.28)),
            (hl + 0.10, hw + 0.28),
            (-(hl + 0.10), hw + 0.28),
        ],
        layer=LAYER.sab,
    )
    c.add_polygon(
        [
            (-(poly_end + 0.20), -(hw + 0.20)),
            (-hl, -(hw + 0.20)),
            (-hl, hw + 0.20),
            (-(poly_end + 0.20), hw + 0.20),
        ],
        layer=LAYER.pplus,
    )
    c.add_polygon(
        [
            (hl, -(hw + 0.20)),
            (poly_end + 0.20, -(hw + 0.20)),
            (poly_end + 0.20, hw + 0.20),
            (hl, hw + 0.20),
        ],
        layer=LAYER.pplus,
    )
    for sign, pname, ori in [(-1, "r0", 180), (1, "r1", 0)]:
        cx = round(sign * (hl + 0.43), 3)
        c.add_polygon(
            [(cx - 0.11, -0.11), (cx + 0.11, -0.11), (cx + 0.11, 0.11), (cx - 0.11, 0.11)],
            layer=LAYER.contact,
        )
        c.add_polygon(
            [(cx - 0.17, -0.30), (cx + 0.17, -0.30), (cx + 0.17, 0.30), (cx - 0.17, 0.30)],
            layer=LAYER.metal1,
        )
        c.add_port(
            pname,
            center=(cx, 0.0),
            width=0.34,
            orientation=ori,
            layer=LAYER.metal1,
            port_type="electrical",
        )
    return c


@gf.cell
def r2r_res_bay(lengths: tuple[float, ...], w: float = 1.00) -> gf.Component:
    """1-row (3.92um) standard-cell bay in LVPWELL (bulk=VSS in both Magic and KLayout)."""
    c = gf.Component()
    fill1 = std_cell("fill_1")
    pad_left, pad_right = 2.20, 2.20
    cur_x = pad_left
    centers: list[float] = []
    for i, l_val in enumerate(lengths):
        half_span = round(l_val / 2 + 0.81, 3)
        cx = round(cur_x + half_span, 3)
        centers.append(cx)
        cur_x = round(cx + half_span + (0.50 if i + 1 < len(lengths) else 0.0), 3)

    raw_w = cur_x + pad_right
    sites = math.ceil(raw_w / SITE_W)
    bay_w = round(sites * SITE_W, 3)
    shift = round(((bay_w - raw_w) / 2) / 0.005) * 0.005
    centers = [round(cx + shift, 3) for cx in centers]

    for s in (0, 1, sites - 2, sites - 1):
        rf = c.add_ref(fill1)
        rf.dmove((round(s * SITE_W, 3), 0))

    c.add_polygon([(0, 0), (bay_w, 0), (bay_w, ROW_H), (0, ROW_H)], layer=LAYER.pr_bndry)
    c.add_polygon(
        [(-0.28, -0.28), (bay_w + 0.28, -0.28), (bay_w + 0.28, 4.20), (-0.28, 4.20)],
        layer=LAYER.dualgate,
    )
    c.add_polygon(
        [(-0.43, 1.76), (bay_w + 0.43, 1.76), (bay_w + 0.43, 4.35), (-0.43, 4.35)],
        layer=LAYER.nwell,
    )
    c.add_polygon(
        [(-0.43, -0.43), (bay_w + 0.43, -0.43), (bay_w + 0.43, 1.76), (-0.43, 1.76)],
        layer=LAYER.lvpwell,
    )
    c.add_polygon([(0, -0.30), (bay_w, -0.30), (bay_w, 0.30), (0, 0.30)], layer=LAYER.metal1)
    c.add_polygon([(0, 3.62), (bay_w, 3.62), (bay_w, 4.22), (0, 4.22)], layer=LAYER.metal1)
    c.add_polygon(
        [(-0.20, 2.50), (bay_w + 0.20, 2.50), (bay_w + 0.20, 4.12), (-0.20, 4.12)],
        layer=LAYER.pplus,
    )

    hw = round(w / 2, 3)
    res_x0 = round(centers[0] - (lengths[0] / 2 + 1.01), 3)
    res_x1 = round(centers[-1] + (lengths[-1] / 2 + 1.01), 3)
    c.add_polygon(
        [
            (res_x0, Y_RES - (hw + 0.40)),
            (res_x1, Y_RES - (hw + 0.40)),
            (res_x1, Y_RES + (hw + 0.40)),
            (res_x0, Y_RES + (hw + 0.40)),
        ],
        layer=LAYER.resistor,
    )
    for dx0 in (round(res_x0 - 0.75, 3), round(res_x1 + 0.35, 3)):
        c.add_polygon(
            [
                (dx0, Y_RES - hw),
                (dx0 + 0.40, Y_RES - hw),
                (dx0 + 0.40, Y_RES + hw),
                (dx0, Y_RES + hw),
            ],
            layer=LAYER.poly2,
        )

    for idx, (cx, l_val) in enumerate(zip(centers, lengths)):
        ref = c.add_ref(horiz_hres_6p0(w=w, length=l_val))
        ref.dmove((cx, Y_RES))
        c.add_port(f"f{idx}_r0", port=ref.ports["r0"])
        c.add_port(f"f{idx}_r1", port=ref.ports["r1"])

    c.info["width"] = bay_w
    return c


@gf.cell
def _wire_m1_v(dy: float = 1.0, w: float = 0.34) -> gf.Component:
    c = gf.Component()
    ylo, yhi = round(min(0.0, dy) - 0.15, 3), round(max(0.0, dy) + 0.15, 3)
    hw = round(w / 2, 3)
    c.add_polygon([(-hw, ylo), (hw, ylo), (hw, yhi), (-hw, yhi)], layer=LAYER.metal1)
    c.add_port(
        "e1", center=(0, 0), width=0.28, orientation=270, layer=LAYER.metal1, port_type="electrical"
    )
    c.add_port(
        "e2",
        center=(0, round(dy, 3)),
        width=0.28,
        orientation=90,
        layer=LAYER.metal1,
        port_type="electrical",
    )
    return c


@gf.cell
def _wire_m1_h(dx: float = 1.0, w: float = 0.34) -> gf.Component:
    c = gf.Component()
    xlo, xhi = round(min(0.0, dx) - 0.15, 3), round(max(0.0, dx) + 0.15, 3)
    hw = round(w / 2, 3)
    c.add_polygon([(xlo, -hw), (xhi, -hw), (xhi, hw), (xlo, hw)], layer=LAYER.metal1)
    c.add_port(
        "e1", center=(0, 0), width=0.28, orientation=180, layer=LAYER.metal1, port_type="electrical"
    )
    c.add_port(
        "e2",
        center=(round(dx, 3), 0),
        width=0.28,
        orientation=0,
        layer=LAYER.metal1,
        port_type="electrical",
    )
    return c


@gf.cell
def _wire_m2_bridge(
    dx: float = 1.0, dy: float = 0.0, dr: float = 0.0, w: float = 0.30
) -> gf.Component:
    c = gf.Component()
    v = via_m1_m2()
    c.add_ref(v)
    v1 = c.add_ref(v)
    v1.dmove((round(dx, 3), round(dy, 3)))
    hw = round(w / 2, 3)
    xlo, xhi = min(0.0, dx), max(0.0, dx)
    c.add_polygon(
        [
            (round(xlo - 0.15, 3), round(dr - hw, 3)),
            (round(xhi + 0.15, 3), round(dr - hw, 3)),
            (round(xhi + 0.15, 3), round(dr + hw, 3)),
            (round(xlo - 0.15, 3), round(dr + hw, 3)),
        ],
        layer=LAYER.metal2,
    )
    for xv, yv in [(0.0, 0.0), (dx, dy)]:
        if abs(dr - yv) > 1e-3:
            ylo, yhi = min(dr, yv), max(dr, yv)
            c.add_polygon(
                [
                    (round(xv - hw, 3), round(ylo - 0.15, 3)),
                    (round(xv + hw, 3), round(ylo - 0.15, 3)),
                    (round(xv + hw, 3), round(yhi + 0.15, 3)),
                    (round(xv - hw, 3), round(yhi + 0.15, 3)),
                ],
                layer=LAYER.metal2,
            )
    c.add_port(
        "e1", center=(0, 0), width=0.28, orientation=180, layer=LAYER.metal1, port_type="electrical"
    )
    c.add_port(
        "e2",
        center=(round(dx, 3), round(dy, 3)),
        width=0.28,
        orientation=0,
        layer=LAYER.metal1,
        port_type="electrical",
    )
    return c


def m1_strap_y(c: gf.Component, x: float, y0: float, y1: float, w: float = 0.34):
    ref = c.add_ref(_wire_m1_v(dy=round(y1 - y0, 3), w=w))
    ref.dmove((round(x, 3), round(y0, 3)))
    return ref


def m1_hbar(c: gf.Component, x0: float, x1: float, y: float, w: float = 0.34):
    ref = c.add_ref(_wire_m1_h(dx=round(x1 - x0, 3), w=w))
    ref.dmove((round(x0, 3), round(y, 3)))
    return ref


def m2_bridge(
    c: gf.Component,
    x0: float,
    y0: float,
    x1: float,
    y1: float,
    w: float = 0.30,
    y_route: float | None = None,
):
    yr = y0 if y_route is None else y_route
    ref = c.add_ref(
        _wire_m2_bridge(dx=round(x1 - x0, 3), dy=round(y1 - y0, 3), dr=round(yr - y0, 3), w=w)
    )
    ref.dmove((round(x0, 3), round(y0, 3)))
    return ref


def add_signal_pin(
    c: gf.Component,
    name: str,
    direction: str,
    x0: float,
    y0: float,
    x1: float,
    y1: float,
    target_port=None,
) -> None:
    if target_port is not None:
        p = c.add_port(name=name, port=target_port)
    else:
        cx, cy = round((x0 + x1) / 2, 3), round((y0 + y1) / 2, 3)
        p = c.add_port(
            name=name,
            center=(cx, cy),
            width=max(0.24, round((x1 - x0) / 0.01) * 0.01),
            orientation=90,
            layer=LAYER.metal1,
            port_type="electrical",
        )
    p.info["direction"] = direction
    p.info["rect"] = (round(x0, 3), round(y0, 3), round(x1, 3), round(y1, 3))
    c.add_label(
        name, position=(round((x0 + x1) / 2, 3), round((y0 + y1) / 2, 3)), layer=LAYER.metal1_label
    )


def extract_cdl_subckts(names: list[str]) -> str:
    cdl = SC_CDL.read_text()
    blocks = []
    for nm in names:
        full = f"gf180mcu_fd_sc_mcu7t5v0__{nm}"
        m = re.search(rf"^\.SUBCKT {full}\b.*?^\.ENDS", cdl, re.M | re.S)
        if not m:
            raise ValueError(f"Missing {full} in CDL")
        blocks.append(m.group(0))
    return "\n\n".join(blocks)


def infer_spice(c: gf.Component, subckt_name: str) -> str:
    """Return LVS reference SPICE netlist inferred from gdsfactory Component.get_netlist()."""
    nl = c.get_netlist(
        recursive=True,
        on_multi_connect="ignore",
        on_dangling_port="ignore",
        port_matcher=PortCenterMatcher(tolerance_dbu=5),
    )
    parent: dict[str, str] = {}

    def find_root(x: str) -> str:
        while x in parent:
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find_root(a), find_root(b)
        if ra != rb:
            parent[rb] = ra

    leaf_devs: list[tuple[str, str, dict]] = []

    def walk(cell_key: str, prefix: str) -> None:
        cell_nl = nl[cell_key]
        for net in cell_nl.get("nets", ()):
            union(f"{prefix}{net['p1']}", f"{prefix}{net['p2']}")
        for ext_p, inner_target in cell_nl.get("ports", {}).items():
            lhs = f"{prefix[:-1]},{ext_p}" if prefix else f"TOP,{ext_p}"
            union(lhs, f"{prefix}{inner_target}")
        for iname, idata in cell_nl.get("instances", {}).items():
            comp_name = idata["component"]
            sub_path = f"{prefix}{iname}"
            settings = idata.get("settings", {})
            if comp_name.startswith(("_wire_", "via_m1_m2")):
                union(f"{sub_path},e1", f"{sub_path},e2")
            elif comp_name.startswith("horiz_hres_6p0"):
                leaf_devs.append((sub_path, "hres", settings))
            elif comp_name.startswith("std_cell"):
                sc_type = settings.get("name", "")
                if sc_type not in ("fill_1", "filltie"):
                    leaf_devs.append((sub_path, f"sc:{sc_type}", settings))
            elif comp_name in nl:
                walk(comp_name, f"{sub_path}/")

    walk(next(iter(nl.keys())), "")

    pin_order = [p.name for p in c.ports] + ["VDD", "VSS"]
    root_to_name: dict[str, str] = {}
    for p in c.ports:
        root_to_name[find_root(f"TOP,{p.name}")] = p.name
    net_counter = 0

    def net_name(node: str) -> str:
        nonlocal net_counter
        r = find_root(node)
        if r not in root_to_name:
            net_counter += 1
            root_to_name[r] = f"n_auto_{net_counter}"
        return root_to_name[r]

    used_sc = sorted({kind.split(":")[1] for _, kind, _ in leaf_devs if kind.startswith("sc:")})
    lines = [
        "* Auto-inferred from gdsfactory Component.get_netlist(recursive=True)",
        extract_cdl_subckts(used_sc),
        f".SUBCKT {subckt_name} {' '.join(pin_order)}",
    ]
    for idx, (path, kind, settings) in enumerate(leaf_devs):
        if kind == "hres":
            w, res_l = settings.get("w", 1.0), settings.get("length", 10.0)
            lines.append(
                f"RR_{idx} {net_name(path + ',r0')} {net_name(path + ',r1')} VSS "
                f"ppolyf_u_1k_6p0 W={w}e-6 L={res_l}e-6"
            )
        elif kind.startswith("sc:"):
            sc = kind.split(":")[1]
            if sc == "tieh":
                lines.append(
                    f"X_{idx} {net_name(path + ',Z')} VDD VDD VSS VSS gf180mcu_fd_sc_mcu7t5v0__tieh"
                )
            elif sc == "tiel":
                lines.append(
                    f"X_{idx} {net_name(path + ',ZN')} VDD VDD VSS VSS gf180mcu_fd_sc_mcu7t5v0__tiel"
                )
            elif sc.startswith("clkinv"):
                lines.append(
                    f"X_{idx} {net_name(path + ',I')} {net_name(path + ',ZN')} "
                    f"VDD VDD VSS VSS gf180mcu_fd_sc_mcu7t5v0__{sc}"
                )
            elif sc.startswith("clkbuf"):
                lines.append(
                    f"X_{idx} {net_name(path + ',I')} {net_name(path + ',Z')} "
                    f"VDD VDD VSS VSS gf180mcu_fd_sc_mcu7t5v0__{sc}"
                )
    lines += [f".ENDS {subckt_name}", ""]
    return "\n".join(lines)


def export_lef(c: gf.Component, cell_name: str) -> str:
    """Return OpenROAD CLASS CORE ; SITE GF018hv5v_mcu_sc7 LEF abstract text."""
    total_w = float(c.info["width"])
    ly = c.kcl.layout
    dbu = ly.dbu
    kcell = c.kdb_cell

    li_m1 = ly.find_layer(34, 0)
    li_v1 = ly.find_layer(35, 0)
    li_m2 = ly.find_layer(36, 0)

    reg_m1 = pya.Region(kcell.begin_shapes_rec(li_m1)).merged()
    pin_m1_reg = pya.Region()
    pin_m1_reg.insert(pya.DBox(0, -0.30, total_w, 0.30).to_itype(dbu))
    pin_m1_reg.insert(pya.DBox(0, 3.62, total_w, 4.22).to_itype(dbu))
    for p in c.ports:
        x0, y0, x1, y1 = p.info["rect"]
        pin_m1_reg.insert(pya.DBox(x0, y0, x1, y1).to_itype(dbu))

    obs_m1 = reg_m1 - pin_m1_reg
    obs_v1 = (
        pya.Region(kcell.begin_shapes_rec(li_v1)).merged() if li_v1 is not None else pya.Region()
    )
    obs_m2 = (
        pya.Region(kcell.begin_shapes_rec(li_m2)).merged() if li_m2 is not None else pya.Region()
    )

    lines = [
        "VERSION 5.7 ;",
        "  NOWIREEXTENSIONATPIN ON ;",
        '  DIVIDERCHAR "/" ;',
        '  BUSBITCHARS "[]" ;',
        f"MACRO {cell_name}",
        "  CLASS CORE ;",
        f"  FOREIGN {cell_name} 0.000 0.000 ;",
        "  ORIGIN 0.000 0.000 ;",
        f"  SIZE {total_w:.3f} BY {ROW_H:.3f} ;",
        "  SYMMETRY X Y ;",
        "  SITE GF018hv5v_mcu_sc7 ;",
    ]
    for p in c.ports:
        x0, y0, x1, y1 = p.info["rect"]
        lines += [
            f"  PIN {p.name}",
            f"    DIRECTION {p.info['direction']} ;",
            "    USE SIGNAL ;",
            "    PORT",
            "      LAYER Metal1 ;",
            f"        RECT {x0:.3f} {y0:.3f} {x1:.3f} {y1:.3f} ;",
            "    END",
            f"  END {p.name}",
        ]
    lines += [
        "  PIN VDD",
        "    DIRECTION INOUT ;",
        "    USE POWER ;",
        "    SHAPE ABUTMENT ;",
        "    PORT",
        "      LAYER Metal1 ;",
        f"        RECT 0.000 3.620 {total_w:.3f} 4.220 ;",
        "    END",
        "  END VDD",
        "  PIN VSS",
        "    DIRECTION INOUT ;",
        "    USE GROUND ;",
        "    SHAPE ABUTMENT ;",
        "    PORT",
        "      LAYER Metal1 ;",
        f"        RECT 0.000 -0.300 {total_w:.3f} 0.300 ;",
        "    END",
        "  END VSS",
        "  PIN VNW",
        "    DIRECTION INOUT ;",
        "    USE POWER ;",
        "    PORT",
        "      LAYER Nwell ;",
        f"        RECT -0.430 1.760 {total_w + 0.43:.3f} 4.350 ;",
        "    END",
        "  END VNW",
        "  PIN VPW",
        "    DIRECTION INOUT ;",
        "    USE GROUND ;",
        "    PORT",
        "      LAYER Pwell ;",
        f"        RECT -0.430 -0.430 {total_w + 0.43:.3f} 1.760 ;",
        "    END",
        "  END VPW",
        "  OBS",
    ]
    for lyr_label, reg in [("Metal1", obs_m1), ("Via1", obs_v1), ("Metal2", obs_m2)]:
        if not reg.is_empty():
            lines.append(f"      LAYER {lyr_label} ;")
            for poly in reg.each():
                for trap in poly.decompose_trapezoids(pya.Polygon.TD_htrapezoids):
                    b = trap.bbox().to_dtype(dbu)
                    lines.append(
                        f"        RECT {b.left:.3f} {b.bottom:.3f} {b.right:.3f} {b.top:.3f} ;"
                    )
    lines += ["  END", f"END {cell_name}", "END LIBRARY", ""]
    return "\n".join(lines)


def surround_with_neighbors(cell: gf.Component) -> gf.Component:
    """Surround a 1-row CLASS CORE cell on all 4 sides with standard cells for abutted DRC."""
    total_w = float(cell.info["width"])
    top = gf.Component(f"{cell.name}_abutted")
    top.add_ref(cell)
    cb8 = std_cell("clkbuf_8")
    tie = std_cell("filltie")
    fill1 = std_cell("fill_1")
    total_sites = round(total_w / SITE_W)
    for y_nb in (7.84, 0.00):
        s = -2
        while s < total_sites + 2:
            if s + 28 <= total_sites + 2:
                rt = top.add_ref(tie)
                rt.dmirror_y()
                rt.dmove((round(s * SITE_W, 3), y_nb))
                rc = top.add_ref(cb8)
                rc.dmirror_y()
                rc.dmove((round((s + 2) * SITE_W, 3), y_nb))
                s += 28
            else:
                rf = top.add_ref(fill1)
                rf.dmirror_y()
                rf.dmove((round(s * SITE_W, 3), y_nb))
                s += 1
    rl = top.add_ref(tie)
    rl.dmove((-1.12, 0))
    rr = top.add_ref(tie)
    rr.dmove((total_w, 0))
    return top


def run_klayout_drc(gds_path: Path, run_dir: Path) -> None:
    res = subprocess.run(
        [
            sys.executable,
            str(DRC_SCRIPT),
            f"--path={gds_path}",
            f"--run_dir={run_dir}",
            "--variant=D",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "GDS has no DRC violations" in (res.stdout + res.stderr), f"DRC failed on {gds_path}"


def run_klayout_lvs(
    gds_path: Path, spice_path: Path, run_dir: Path, topcell: str, extra_flags: list[str]
) -> Path:
    res = subprocess.run(
        [
            sys.executable,
            str(LVS_SCRIPT),
            f"--layout={gds_path}",
            f"--netlist={spice_path}",
            f"--topcell={topcell}",
            f"--run_dir={run_dir}",
            "--variant=D",
            "--lvs_sub=VSS",
            *extra_flags,
        ],
        capture_output=True,
        text=True,
    )
    if "--net_only" not in extra_flags:
        assert res.returncode == 0 and "Congratulations! Netlists match." in (
            res.stdout + res.stderr
        ), f"LVS mismatch on {topcell}:\n{res.stdout}\n{res.stderr}"
    return run_dir / f"{gds_path.stem}.cir"


def klayout_cir_to_ngspice(cir_path: Path) -> tuple[list[str], str]:
    raw = re.sub(r"\n\+\s*", " ", cir_path.read_text())
    pins = re.search(r"^\.SUBCKT\s+\S+\s+(.*)$", raw, re.M).group(1).split()
    conv = re.sub(
        r"^R(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+\S+\s+(ppolyf_u\S*)\s+L=(\S+)\s+W=(\S+)",
        r"XR\1 \2 \3 \4 \5 r_length=\6 r_width=\7",
        raw,
        flags=re.M,
    )
    conv = re.sub(r"^M(\S+)", r"XM\1", conv, flags=re.M)
    conv = (
        conv.replace("pfet_05v0", "pfet_06v0")
        .replace("nfet_05v0", "nfet_06v0")
        .replace("\\$", "n_")
    )
    return pins, conv


def ngspice_subckt_wrapper(src_path: Path, cell_name: str, canonical_pins: list[str]) -> str:
    """Convert KLayout .cir or LVS .spice to ngspice sm141064 syntax with fixed canonical pin order."""
    raw = src_path.read_text()
    if src_path.suffix == ".cir":
        pins, conv = klayout_cir_to_ngspice(src_path)
    else:
        top_m = re.search(r"(^\.SUBCKT\s+(?!gf180mcu_)\S+\s+.*?^\.ENDS\s+\S+)", raw, re.M | re.S)
        conv = top_m.group(1)
        pins = re.search(r"^\.SUBCKT\s+\S+\s+(.*)$", conv, re.M).group(1).split()
        conv = re.sub(
            r"^RR_(\d+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(ppolyf_u\S*)\s+W=(\S+)\s+L=(\S+)",
            r"XRR_\1 \2 \3 \4 \5 r_width=\6 r_length=\7",
            conv,
            flags=re.M,
        )
    assert set(pins) == set(canonical_pins), (
        f"Subckt pin mismatch for {cell_name}: "
        f"extracted={sorted(pins)} vs expected={sorted(canonical_pins)}"
    )
    raw_name = f"{cell_name}_raw"
    conv = re.sub(r"^(\.SUBCKT\s+)\S+", rf"\g<1>{raw_name}", conv, count=1, flags=re.M)
    conv = re.sub(r"^(\.ENDS)(?:\s+\S+)?$", rf"\g<1> {raw_name}", conv, flags=re.M)
    return (
        f"{conv}\n\n"
        f".SUBCKT {cell_name} {' '.join(canonical_pins)}\n"
        f"XRAW {' '.join(pins)} {raw_name}\n"
        f".ENDS {cell_name}\n"
    )


def ngspice_env(
    corner: str = "typical",
    rcorner: str = "res_typical",
    includes: list[Path] | None = None,
    extra_libs: list[str] | None = None,
) -> str:
    """Return minimal PDK corner + subcircuit .include header for static .spice testbenches."""
    lines = [
        f".include {NG_MODELS}/design.ngspice",
        f".lib {NG_MODELS}/sm141064.ngspice {corner}",
        f".lib {NG_MODELS}/sm141064.ngspice {rcorner}",
        *[f".lib {NG_MODELS}/sm141064.ngspice {lib}" for lib in (extra_libs or [])],
        f".include {SC_SPICE}",
        *[f".include {p.resolve()}" for p in (includes or [])],
    ]
    return "\n".join(lines) + "\n"


def check_liberty_and_lef(
    lef_path: Path, lib_path: Path, cell_name: str, expected_area: float
) -> None:
    """Validate LEF + Liberty in OpenROAD/OpenSTA and verify Liberty area matches layout."""
    import openroad

    t = openroad.Tech()
    t.readLef(str(SC_TLEF))
    t.readLef(str(lef_path))
    t.readLiberty(str(lib_path))
    m = re.search(r"area\s*:\s*([0-9.]+)\s*;", lib_path.read_text())
    assert m is not None, f"Missing area attribute in {lib_path}"
    lib_area = float(m.group(1))
    assert abs(lib_area - expected_area) < 0.05, (
        f"{cell_name} Liberty area {lib_area:.2f} != layout area {expected_area:.2f} um^2"
    )
