#!/usr/bin/env python3
"""Programmatic LibreLane placement & config generator for Tiny Tapeout and ISHI-KAI.

Derives passive `r2r_ladder_4bit_sum` (ADC) and `r2r_ladder_8bit` (DAC) macro anchors plus
disjoint, tapcell-free `clkbuf_20` DAC driver placements directly from XLS-generated Verilog and
target pin/floorplan geometry, eliminating hand-maintained coordinates from `src/config.json` and
`ishikai/config.yaml`.
"""

from __future__ import annotations

import argparse
import copy
from dataclasses import dataclass
import json
import math
import os
import re
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src"
ISHIKAI_DIR = REPO_ROOT / "ishikai"

# Shared GF180MCU 7T 5V0 analog macro & placement constants
SITE_W = 0.56
ROW_H = 3.92
ENDCAP_W = 1.12  # gf180mcu_fd_sc_mcu7t5v0__endcap (2 sites)
TAP_W = 1.12  # gf180mcu_fd_sc_mcu7t5v0__filltie (2 sites)
TAP_PITCH = 39.20  # 70 sites
CLKBUF20_W = 34.72  # gf180mcu_fd_sc_mcu7t5v0__clkbuf_20 (62 sites)

ADC_CELL_NAME = "r2r_ladder_4bit_sum"
ADC_MACRO_W = 28.00
ADC_MACRO_H = 15.12
ADC_SUM_IN_PIN_X = 25.36
ADC_SUM_IN_PIN_Y = 13.16

DAC_CELL_NAME = "r2r_ladder_8bit"
DAC_MACRO_W = 70.00
DAC_MACRO_H = 26.32
DAC_IN_PIN_X = 2.62
DAC_OUT_PIN_X = 45.20
DAC_OUT_PIN_Y = 24.36

FP_MACRO_HORIZONTAL_HALO = 4.8
FP_MACRO_VERTICAL_HALO = 4.0

DLY_EXCLUDED_CELLS = [
    f"gf180mcu_fd_sc_mcu7t5v0__dly{grp}_{drv}" for grp in "abcd" for drv in (1, 2, 4)
]

# Metal3 horizontal pin track pitch (GF180MCU 7T)
M3_PITCH = 0.56


@dataclass(frozen=True)
class _AnalogMacroSpec:
    cell: str
    path: str
    ua_idx: int
    w: float
    h: float
    pin_x: float
    pin_y: float


def _snap_up(val: float, grid: float) -> float:
    return round(math.ceil((val - 1e-9) / grid) * grid, 2)


def _snap_down(val: float, grid: float) -> float:
    return round(math.floor((val + 1e-9) / grid) * grid, 2)


def _snap_nearest(val: float, grid: float) -> float:
    return round(round(val / grid) * grid, 2)


def _xls_inst_key(name: str) -> list[int | str]:
    norm = re.sub(r"__(\d+)_inst$", r"[\1]_inst", name)
    norm = re.sub(r"(?<!\])_inst$", "[0]_inst", norm)
    return [int(tok) if tok.isdigit() else tok for tok in re.split(r"(\d+)", norm)]


def find_adc_macro_instances(verilog_path: Path, core_inst: str) -> list[str]:
    """Discover XLS #[extern_verilog] `sc_adc_ramp_1ch_4b` instance paths in channel order."""
    matches = re.findall(
        r"\bsc_adc_ramp_1ch_4b\s+(\w+)\s*\(", verilog_path.read_text(encoding="utf-8")
    )
    return [f"{core_inst}.{inst}.u_r2r" for inst in sorted(matches, key=_xls_inst_key)]


def find_dac_macro_instances(verilog_path: Path, core_inst: str) -> list[str]:
    """Discover XLS #[extern_verilog] `sc_r2r_dac_8bit` instance paths in natural channel order."""
    matches = re.findall(
        r"\bsc_r2r_dac_8bit\s+(\w+)\s*\(", verilog_path.read_text(encoding="utf-8")
    )
    if not matches:
        raise RuntimeError(f"No sc_r2r_dac_8bit instances found in {verilog_path}")
    return [f"{core_inst}.{inst}.u_r2r" for inst in sorted(matches, key=_xls_inst_key)]


def _build_macro_specs(verilog_path: Path, core_inst: str) -> list[_AnalogMacroSpec]:
    """Return all passive ADC (`ua[0..N_adc-1]`) + DAC (`ua[N_adc..N_total-1]`) macro specs."""
    adc_paths = find_adc_macro_instances(verilog_path, core_inst=core_inst)
    dac_paths = find_dac_macro_instances(verilog_path, core_inst=core_inst)
    specs: list[_AnalogMacroSpec] = []
    for idx, path in enumerate(adc_paths):
        specs.append(
            _AnalogMacroSpec(
                cell=ADC_CELL_NAME,
                path=path,
                ua_idx=idx,
                w=ADC_MACRO_W,
                h=ADC_MACRO_H,
                pin_x=ADC_SUM_IN_PIN_X,
                pin_y=ADC_SUM_IN_PIN_Y,
            )
        )
    offset = len(adc_paths)
    for idx, path in enumerate(dac_paths):
        specs.append(
            _AnalogMacroSpec(
                cell=DAC_CELL_NAME,
                path=path,
                ua_idx=offset + idx,
                w=DAC_MACRO_W,
                h=DAC_MACRO_H,
                pin_x=DAC_OUT_PIN_X,
                pin_y=DAC_OUT_PIN_Y,
            )
        )
    return specs


def _plan_dac_driver_placements(
    dac_paths: list[str],
    dac_coords: list[tuple[float, float]],
    rows: list[tuple[float, bool, float]],
    core_x0: float,
    core_x1: float,
    macro_obstacles: list[tuple[float, float, float, float]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Place all `clkbuf_20` DAC drivers into disjoint, tapcell-free bays near each macro's pins."""
    obstacles = macro_obstacles or []

    bays: list[tuple[float, float, float, bool]] = []
    seen_rows: set[float] = set()
    for row_y, is_fs, tap0 in rows:
        ry = round(row_y, 2)
        if ry in seen_rows:
            continue
        seen_rows.add(ry)
        # Collect tapcell and macro-obstacle X intervals blocking this row
        blocked: list[tuple[float, float]] = []
        tx = tap0
        while tx + TAP_W <= core_x1 + 1e-6:
            if tx >= core_x0 - 1e-6:
                blocked.append((round(tx, 2), round(tx + TAP_W, 2)))
            tx = round(tx + TAP_PITCH, 2)
        for ox0, oy0, ox1, oy1 in obstacles:
            if ry < oy1 - 1e-6 and ry + ROW_H > oy0 + 1e-6:
                blocked.append(
                    (max(core_x0, _snap_down(ox0, SITE_W)), min(core_x1, _snap_up(ox1, SITE_W)))
                )

        # Merge blocked intervals and emit free bays wide enough for clkbuf_20
        blocked.sort()
        merged: list[tuple[float, float]] = []
        for a, b in blocked:
            if a >= b:
                continue
            if merged and a <= merged[-1][1] + 1e-6:
                merged[-1] = (merged[-1][0], max(merged[-1][1], b))
            else:
                merged.append((a, b))

        cursor = core_x0
        for a, b in merged:
            bx0, bx1 = round(cursor, 2), round(min(a, core_x1), 2)
            if bx1 - bx0 >= CLKBUF20_W - 1e-6:
                bays.append((ry, bx0, bx1, is_fs))
            cursor = max(cursor, b)
        if core_x1 - cursor >= CLKBUF20_W - 1e-6:
            bays.append((ry, round(cursor, 2), round(core_x1, 2), is_fs))

    pin_specs = [
        *[(f"gen_drv[{b}].u_buf", 4.76 + 2.80 * b) for b in range(7, -1, -1)],
        ("u_term", 1.96),
    ]
    prefixes = [p.removesuffix(".u_r2r") for p in dac_paths]

    def _best_in_bay(
        bx0: float, bx1: float, row_y: float, is_fs: bool, px: float, py: float
    ) -> tuple[float, float, float, str] | None:
        best = None
        n_steps = int(round((bx1 - CLKBUF20_W - bx0) / SITE_W))
        orients = (("FS", False), ("S", True)) if is_fs else (("N", False), ("FN", True))
        for k in range(n_steps + 1):
            x = round(bx0 + k * SITE_W, 2)
            if x + CLKBUF20_W > bx1 + 1e-6:
                continue
            for orient, flip_x in orients:
                zx0 = x + (CLKBUF20_W - 33.13) if flip_x else x + 12.74
                zx1 = x + (CLKBUF20_W - 12.74) if flip_x else x + 33.13
                dx = 0.0 if zx0 <= px <= zx1 else min(abs(px - zx0), abs(px - zx1))
                z_err = abs(0.5 * (zx0 + zx1) - px)
                dy = abs((row_y + 1.96) - py)
                cand = (round(dx + dy, 4), round(z_err, 4), x, orient)
                if best is None or cand < best:
                    best = cand
        return best

    used_bays: set[int] = set()
    placed: dict[tuple[int, str], tuple[str, float, float, str]] = {}
    for pin_suffix, y_off in pin_specs:
        for ch, (prefix, (mx, my)) in enumerate(zip(prefixes, dac_coords, strict=True)):
            px = mx + DAC_IN_PIN_X
            py = my + y_off
            best = None
            for bi, (row_y, bx0, bx1, is_fs) in enumerate(bays):
                if bi in used_bays:
                    continue
                # Keep drivers close to their own macro (especially vertically stacked ladders)
                if row_y < my + DAC_MACRO_H or row_y > my + DAC_MACRO_H + 55.0:
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

    ordered: dict[str, dict[str, Any]] = {}
    for ch in range(len(prefixes)):
        for pin_suffix, _ in pin_specs:
            inst, x, row_y, orient = placed[(ch, pin_suffix)]
            ordered[inst] = {"location": [x, row_y], "orientation": orient}
    return ordered


def parse_tt_def_template(def_path: Path) -> dict[str, Any]:
    """Parse DBU, die area, ROW grid, and `ua[k]` pin X coordinates from `tt_analog_2x2.def`."""
    txt = def_path.read_text(encoding="utf-8")
    m_dbu = re.search(r"UNITS\s+DISTANCE\s+MICRONS\s+(\d+)", txt)
    if not m_dbu:
        raise RuntimeError(f"UNITS DISTANCE MICRONS not found in {def_path}")
    dbu = int(m_dbu.group(1))

    m_die = re.search(r"DIEAREA\s+\(\s*(-?\d+)\s+(-?\d+)\s*\)\s+\(\s*(-?\d+)\s+(-?\d+)\s*\)", txt)
    if not m_die:
        raise RuntimeError(f"DIEAREA not found in {def_path}")
    die_area = [int(v) / dbu for v in m_die.groups()]

    m_row = re.search(
        r"ROW\s+ROW_0\s+\S+\s+(-?\d+)\s+(-?\d+)\s+(\S+)\s+DO\s+(\d+)\s+BY\s+1\s+STEP\s+(\d+)\s+0",
        txt,
    )
    if not m_row:
        raise RuntimeError(f"ROW_0 not found in {def_path}")
    row_x0 = int(m_row.group(1)) / dbu
    row_y0 = int(m_row.group(2)) / dbu
    row0_is_fs = m_row.group(3) in ("FS", "S")
    row_sites = int(m_row.group(4))
    site_w = int(m_row.group(5)) / dbu
    row_x1 = round(row_x0 + row_sites * site_w, 2)

    ua_pins: dict[int, tuple[float, float]] = {}
    for m_pin in re.finditer(
        r"-\s+ua\[(\d+)\]\s+\+.*?\+\s+PLACED\s+\(\s*(-?\d+)\s+(-?\d+)\s*\)",
        txt,
        re.S,
    ):
        idx = int(m_pin.group(1))
        ua_pins[idx] = (int(m_pin.group(2)) / dbu, int(m_pin.group(3)) / dbu)

    return {
        "dbu": dbu,
        "die_area": die_area,
        "row_x0": row_x0,
        "row_x1": row_x1,
        "row_y0": row_y0,
        "row0_is_fs": row0_is_fs,
        "ua_pins": ua_pins,
    }


def parse_ishikai_pin_order(pin_order_path: Path, die_h: float) -> dict[int, tuple[str, float]]:
    """Compute expected `(side, y_um)` for every `ua[k]` pin from `ishikai/pin_order.cfg`."""
    sides: dict[str, list[str | int]] = {}
    cur_side: str | None = None
    for raw in pin_order_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        m_side = re.match(r"^#\s*([NEWS])\b", line)
        if m_side:
            cur_side = m_side.group(1)
            sides.setdefault(cur_side, [])
            continue
        if line.startswith("#") or line.startswith("@"):
            continue
        if cur_side is None:
            continue
        m_virt = re.match(r"^\$\s*(\d+)$", line)
        if m_virt:
            sides[cur_side].append(int(m_virt.group(1)))
        else:
            sides[cur_side].append(line)

    n_tracks = int(math.floor(die_h / M3_PITCH))
    ua_positions: dict[int, tuple[str, float]] = {}
    for side in ("W", "E"):
        entries = sides.get(side, [])
        if not entries:
            continue
        total_slots = sum(item if isinstance(item, int) else 1 for item in entries)
        tracks_per_pin = n_tracks // total_slots
        used_tracks = tracks_per_pin * (total_slots - 1) + 1
        cur_track = (n_tracks - used_tracks) // 2
        for item in entries:
            if isinstance(item, int):
                cur_track += tracks_per_pin * item
                continue
            m_ua = re.fullmatch(r"ua\\\[(\d+)\\\]", item) or re.fullmatch(r"ua\[(\d+)\]", item)
            if m_ua:
                ch = int(m_ua.group(1))
                ua_positions[ch] = (side, round(cur_track * M3_PITCH, 2))
            cur_track += tracks_per_pin
    return ua_positions


def plan_tinytapeout_placements(
    verilog_path: Path,
    def_template_path: Path,
    core_inst: str = "u_core",
) -> tuple[dict[str, dict[str, dict[str, Any]]], dict[str, dict[str, Any]]]:
    """Compute Tiny Tapeout 2x2 bottom-row ADC+DAC R-2R macro & clkbuf_20 driver placements."""
    specs = _build_macro_specs(verilog_path, core_inst=core_inst)
    tmpl = parse_tt_def_template(def_template_path)
    ua_pins = tmpl["ua_pins"]
    for spec in specs:
        if spec.ua_idx not in ua_pins:
            raise RuntimeError(f"ua[{spec.ua_idx}] pin missing from {def_template_path}")

    # Zero-stranded-site gap between adjacent macros (10.08 - 2*4.8 = 0.48 < 0.56)
    macro_gap = _snap_up(2.0 * FP_MACRO_HORIZONTAL_HALO, SITE_W)  # 10.08 um
    macro_y = round(tmpl["row_y0"] + ROW_H, 2)  # 7.84 um (ROW_1, clears bottom ua pins)

    # Order macros left-to-right matching ua[0..N-1] physical X order
    ltr_specs = sorted(specs, key=lambda s: ua_pins[s.ua_idx][0])
    rel_xs: list[float] = []
    cur_x = 0.0
    for spec in ltr_specs:
        rel_xs.append(round(cur_x, 2))
        cur_x = round(cur_x + spec.w + macro_gap, 2)
    total_w = round(rel_xs[-1] + ltr_specs[-1].w, 2)

    # Center macro analog pins on ua[k] pins, clamped clear of leftmost VPWR/VGND stripe pair
    min_x = 35.84
    max_right = _snap_down(tmpl["row_x1"] - ENDCAP_W - FP_MACRO_HORIZONTAL_HALO, SITE_W)
    desired_left = sum(
        ua_pins[spec.ua_idx][0] - (rx + spec.pin_x)
        for spec, rx in zip(ltr_specs, rel_xs, strict=True)
    ) / len(ltr_specs)
    left_x = max(min_x, _snap_nearest(desired_left, SITE_W))
    if left_x + total_w > max_right + 1e-6:
        left_x = _snap_down(max_right - total_w, SITE_W)

    coords_by_path: dict[str, tuple[float, float]] = {}
    for spec, rx in zip(ltr_specs, rel_xs, strict=True):
        coords_by_path[spec.path] = (round(left_x + rx, 2), macro_y)

    macros_by_cell: dict[str, dict[str, dict[str, Any]]] = {}
    for spec in specs:
        mx, my = coords_by_path[spec.path]
        macros_by_cell.setdefault(spec.cell, {})[spec.path] = {
            "location": [mx, my],
            "orientation": "N",
        }

    # Skip macro-boundary row (y = macro_y + 31.36 = 39.20, double-density taps); use 8 normal rows
    first_drv_y = _snap_up(macro_y + DAC_MACRO_H + FP_MACRO_VERTICAL_HALO, ROW_H) + ROW_H
    row_x0 = tmpl["row_x0"]
    core_x0 = round(row_x0 + ENDCAP_W, 2)
    core_x1 = round(tmpl["row_x1"] - ENDCAP_W, 2)
    rows: list[tuple[float, bool, float]] = []
    for r in range(8):
        ry = round(first_drv_y + r * ROW_H, 2)
        row_idx = int(round((ry - tmpl["row_y0"]) / ROW_H))
        is_fs = (row_idx % 2 == 1) ^ tmpl["row0_is_fs"]
        tap0 = round(row_x0 + (TAP_PITCH if is_fs else TAP_PITCH / 2.0), 2)
        rows.append((ry, is_fs, tap0))

    obstacles = [
        (
            coords_by_path[spec.path][0] - FP_MACRO_HORIZONTAL_HALO,
            coords_by_path[spec.path][1] - FP_MACRO_VERTICAL_HALO,
            coords_by_path[spec.path][0] + spec.w + FP_MACRO_HORIZONTAL_HALO,
            coords_by_path[spec.path][1] + spec.h + FP_MACRO_VERTICAL_HALO,
        )
        for spec in specs
    ]
    dac_specs = [spec for spec in specs if spec.cell == DAC_CELL_NAME]
    drv_placements = _plan_dac_driver_placements(
        [spec.path for spec in dac_specs],
        [coords_by_path[spec.path] for spec in dac_specs],
        rows=rows,
        core_x0=core_x0,
        core_x1=core_x1,
        macro_obstacles=obstacles,
    )
    return macros_by_cell, drv_placements


def plan_ishikai_placements(
    verilog_path: Path,
    base_cfg: dict[str, Any],
    pin_order_path: Path,
    core_inst: str = "u_core",
) -> tuple[dict[str, dict[str, dict[str, Any]]], dict[str, dict[str, Any]]]:
    """Compute ISHI-KAI 960x960 macro ADC+DAC R-2R ladder & clkbuf_20 driver placements."""
    specs = _build_macro_specs(verilog_path, core_inst=core_inst)

    die_area = base_cfg["DIE_AREA"]
    core_area = base_cfg["CORE_AREA"]
    die_h = float(die_area[3] - die_area[1])
    ua_pins = parse_ishikai_pin_order(pin_order_path, die_h=die_h)
    for spec in specs:
        if spec.ua_idx not in ua_pins:
            raise RuntimeError(f"ua[{spec.ua_idx}] not found on #W or #E in {pin_order_path}")

    # Exact OpenROAD core row grid (mirrors IFP-0028 snapping of CORE_AREA)
    row_x0 = _snap_up(float(core_area[0]), SITE_W)  # 30.24
    row_y0 = _snap_up(float(core_area[1]), ROW_H)  # 31.36 (ROW_0, N)
    row_x1 = _snap_down(float(core_area[2]), SITE_W)  # 929.60
    row_y1 = _snap_down(float(core_area[3]), ROW_H)  # 929.04 (top of ROW_228)
    core_x0 = round(row_x0 + ENDCAP_W, 2)  # 31.36
    core_x1 = round(row_x1 - ENDCAP_W, 2)  # 928.48

    # Vertical placement constraints:
    # - snap macro Y to even ROW_H (2 * 3.92 = 7.84 um) for uniform row parity above every ladder
    # - keep >= 10 rows above bottom core edge and >= 7 clean rows above top DAC (or 4 above ADC)
    min_my = _snap_up(row_y0 + 10 * ROW_H, 2 * ROW_H)  # 70.56
    max_dac_my = _snap_down(row_y1 - 2 * ROW_H - 14 * ROW_H, 2 * ROW_H)  # 862.40
    max_adc_my = _snap_down(row_y1 - 2 * ROW_H - 8 * ROW_H, 2 * ROW_H)  # 885.92

    coords_by_path: dict[str, tuple[float, float]] = {}
    for side in ("W", "E"):
        side_specs = sorted(
            (s for s in specs if ua_pins[s.ua_idx][0] == side),
            key=lambda s: ua_pins[s.ua_idx][1],
        )
        if not side_specs:
            continue
        max_ys = [max_dac_my if s.cell == DAC_CELL_NAME else max_adc_my for s in side_specs]
        pitches = [
            round((20 if s.cell == DAC_CELL_NAME else 10) * ROW_H, 2) for s in side_specs[:-1]
        ]
        ys = [
            max(min_my, min(max_y, _snap_nearest(ua_pins[s.ua_idx][1] - s.pin_y, 2 * ROW_H)))
            for s, max_y in zip(side_specs, max_ys, strict=True)
        ]
        # Forward pass: enforce minimum vertical pitch going upward
        for i in range(1, len(ys)):
            if ys[i] < ys[i - 1] + pitches[i - 1] - 1e-6:
                ys[i] = round(ys[i - 1] + pitches[i - 1], 2)
        # Backward pass: if top ladder exceeded its ceiling, pull the stack down cleanly
        if ys[-1] > max_ys[-1] + 1e-6:
            ys[-1] = max_ys[-1]
            for i in range(len(ys) - 2, -1, -1):
                if ys[i] > ys[i + 1] - pitches[i] + 1e-6:
                    ys[i] = round(ys[i + 1] - pitches[i], 2)
        if ys[0] < min_my - 1e-6:
            raise RuntimeError(f"Cannot pack {len(side_specs)} ladders on side {side}: {ys}")
        for s, my in zip(side_specs, ys, strict=True):
            mx = round(row_x0 + 30.24, 2) if side == "W" else round(row_x1 - s.w - 38.64, 2)
            coords_by_path[s.path] = (mx, my)

    macros_by_cell: dict[str, dict[str, dict[str, Any]]] = {}
    for spec in specs:
        mx, my = coords_by_path[spec.path]
        macros_by_cell.setdefault(spec.cell, {})[spec.path] = {
            "location": [mx, my],
            "orientation": "N",
        }

    # Build tapcell-free driver row list: 6 normal rows directly above each DAC ladder's boundary
    # row (skipping the macro-boundary row at my + 31.36 where OpenROAD inserts double-density taps).
    dac_specs = [spec for spec in specs if spec.cell == DAC_CELL_NAME]
    rows: list[tuple[float, bool, float]] = []
    top_boundary_y = round(row_y1 - ROW_H, 2)  # ROW_228 (925.12, double-density taps)
    for spec in dac_specs:
        _, my = coords_by_path[spec.path]
        first_drv_y = _snap_up(my + DAC_MACRO_H + FP_MACRO_VERTICAL_HALO, ROW_H) + ROW_H
        for r in range(6):
            ry = round(first_drv_y + r * ROW_H, 2)
            if ry >= top_boundary_y - 1e-6:
                continue
            row_idx = int(round((ry - row_y0) / ROW_H))
            is_fs = row_idx % 2 == 1
            tap0 = round(row_x0 + (TAP_PITCH if is_fs else TAP_PITCH / 2.0), 2)
            rows.append((ry, is_fs, tap0))

    obstacles = [
        (
            coords_by_path[spec.path][0] - FP_MACRO_HORIZONTAL_HALO,
            coords_by_path[spec.path][1] - FP_MACRO_VERTICAL_HALO,
            coords_by_path[spec.path][0] + spec.w + FP_MACRO_HORIZONTAL_HALO,
            coords_by_path[spec.path][1] + spec.h + FP_MACRO_VERTICAL_HALO,
        )
        for spec in specs
    ]
    drv_placements = _plan_dac_driver_placements(
        [spec.path for spec in dac_specs],
        [coords_by_path[spec.path] for spec in dac_specs],
        rows=rows,
        core_x0=core_x0,
        core_x1=core_x1,
        macro_obstacles=obstacles,
    )
    return macros_by_cell, drv_placements


def _rewrite_dir_refs(obj: Any, src_dir: Path, dst_dir: Path) -> Any:
    """Recursively rewrite `dir::...` paths from `src_dir` to be relative to `dst_dir`."""
    if isinstance(obj, str) and obj.startswith("dir::"):
        target = (src_dir / obj[len("dir::") :]).resolve()
        return f"dir::{os.path.relpath(target, dst_dir.resolve())}"
    if isinstance(obj, list):
        return [_rewrite_dir_refs(v, src_dir, dst_dir) for v in obj]
    if isinstance(obj, dict):
        return {k: _rewrite_dir_refs(v, src_dir, dst_dir) for k, v in obj.items()}
    return obj


def generate_config(
    target: str,
    verilog_path: Path,
    out_path: Path,
    base_config_path: Path | None = None,
    def_template_path: Path | None = None,
    pin_order_path: Path | None = None,
    core_inst: str = "u_core",
) -> Path:
    """Merge base config with programmatically generated macro & clkbuf_20 driver placements."""
    out_path = out_path.resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if target == "tinytapeout":
        base_path = (base_config_path or (SRC_DIR / "config.json")).resolve()
        def_path = (def_template_path or (REPO_ROOT / "gen/tt_analog_2x2.def")).resolve()
        # Strip JSON comment keys ("//") while keeping all real config values cleanly
        base_cfg = json.loads(
            base_path.read_text(encoding="utf-8"),
            object_pairs_hook=lambda pairs: {k: v for k, v in pairs if k != "//"},
        )
        cfg = _rewrite_dir_refs(base_cfg, base_path.parent, out_path.parent)
        macros_by_cell, drv_placements = plan_tinytapeout_placements(
            verilog_path=verilog_path,
            def_template_path=def_path,
            core_inst=core_inst,
        )
    elif target == "ishikai":
        base_path = (base_config_path or (ISHIKAI_DIR / "config.yaml")).resolve()
        pin_cfg = (pin_order_path or (ISHIKAI_DIR / "pin_order.cfg")).resolve()
        base_cfg = yaml.safe_load(base_path.read_text(encoding="utf-8"))
        cfg = _rewrite_dir_refs(copy.deepcopy(base_cfg), base_path.parent, out_path.parent)
        macros_by_cell, drv_placements = plan_ishikai_placements(
            verilog_path=verilog_path,
            base_cfg=base_cfg,
            pin_order_path=pin_cfg,
            core_inst=core_inst,
        )
    else:
        raise ValueError(f"Unsupported target: {target}")

    # Inject shared GF180 analog constants and programmatic placements
    cfg["FP_MACRO_HORIZONTAL_HALO"] = FP_MACRO_HORIZONTAL_HALO
    cfg["FP_MACRO_VERTICAL_HALO"] = FP_MACRO_VERTICAL_HALO
    cfg["IGNORE_DISCONNECTED_MODULES"] = [
        cell for cell in (DAC_CELL_NAME, ADC_CELL_NAME) if cell in macros_by_cell
    ]
    cfg["EXTRA_EXCLUDED_CELLS"] = list(DLY_EXCLUDED_CELLS)
    macros_cfg = cfg.setdefault("MACROS", {})
    for cell_name, insts in macros_by_cell.items():
        macros_cfg.setdefault(cell_name, {})["instances"] = insts
    cfg["MANUAL_GLOBAL_PLACEMENTS"] = drv_placements

    if out_path.suffix == ".json":
        out_path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    else:
        out_path.write_text(
            yaml.safe_dump(cfg, sort_keys=False, default_flow_style=False),
            encoding="utf-8",
        )

    counts = ", ".join(f"{len(insts)}x {cell}" for cell, insts in macros_by_cell.items())
    print(
        f"[chip] Generated {target} config -> {out_path} "
        f"({counts}, {len(drv_placements)}x clkbuf_20 drivers)"
    )
    for cell_name, insts in macros_by_cell.items():
        for name, info in insts.items():
            print(
                f"  macro [{cell_name}] {name}: loc={info['location']} orient={info['orientation']}"
            )
    return out_path


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--target", choices=("tinytapeout", "ishikai"), required=True)
    ap.add_argument(
        "--verilog",
        type=Path,
        default=None,
        help="Path to XLS-generated megabytebeat.{sv,v}",
    )
    ap.add_argument("--base-config", type=Path, default=None)
    ap.add_argument("--def-template", type=Path, default=None)
    ap.add_argument("--pin-order", type=Path, default=None)
    ap.add_argument("--core-inst", type=str, default="u_core")
    ap.add_argument(
        "--adc-out-dir",
        type=Path,
        default=REPO_ROOT / "gen/adc",
        help="Output directory for passive r2r_ladder_4bit_sum macro build (default: gen/adc)",
    )
    ap.add_argument(
        "--dac-out-dir",
        type=Path,
        default=REPO_ROOT / "gen/dac",
        help="Output directory for passive r2r_ladder_8bit macro build (default: gen/dac)",
    )
    ap.add_argument(
        "--skip-macros",
        "--skip-dac",
        dest="skip_macros",
        action="store_true",
        help="Skip building/verifying passive ADC/DAC macros and only emit LibreLane config",
    )
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    if args.target == "tinytapeout":
        verilog = args.verilog or (SRC_DIR / "megabytebeat.sv")
        out = args.out or (REPO_ROOT / "gen/tt_config.json")
    else:
        verilog = args.verilog or (SRC_DIR / "megabytebeat.v")
        out = args.out or (REPO_ROOT / "gen/ishikai_config.yaml")

    if not args.skip_macros:
        if find_adc_macro_instances(verilog, core_inst=args.core_inst):
            import adc

            adc.build_and_verify(out_dir=args.adc_out_dir)
        import dac

        dac.build_and_verify(out_dir=args.dac_out_dir)

    generate_config(
        target=args.target,
        verilog_path=verilog,
        out_path=out,
        base_config_path=args.base_config,
        def_template_path=args.def_template,
        pin_order_path=args.pin_order,
        core_inst=args.core_inst,
    )


if __name__ == "__main__":
    main()
