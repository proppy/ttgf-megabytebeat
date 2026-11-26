#!/usr/bin/env python3
"""Parametric pure-passive N-bit R-2R ladder & 8-bit DAC workflow.

Builds a pure-passive N-bit poly-resistor R-2R ladder from single `horiz_hres_6p0` resistor
finger primitives (optionally with a top 1R summing branch for `adc.py`), emits collateral via
`analog.emit_macro`, and verifies KLayout DRC/LVS plus multi-corner 256-code `ngspice` linearity
with external `clkbuf_20` drivers (`src/dac_dc.spice`) across TTGF26c (3.3 V) and wafer.space (5.0 V).
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import math
import os
from pathlib import Path
import re
import tempfile

try:
    import kfactory as kf
    from kfactory.schematic import Placement
except ImportError:  # pragma: no cover - layout build only needed for GDS generation
    kf = None  # type: ignore[assignment]
    Placement = None  # type: ignore[assignment]

import analog
import gf180
import tools

SRC_DIR = Path(__file__).resolve().parent
REPO_ROOT = SRC_DIR.parent
BITS = 8
CELL_NAME = "r2r_ladder_8bit"
AMS_SUBCKT = "sc_r2r_dac_8bit"
DRIVER_CELL = "gf180mcu_fd_sc_mcu7t5v0__clkbuf_20"
TT_MUX_R_OHM = 500
TT_PAD_C_PF = 10
AMS_INIT_COMMANDS: tuple[str, ...] = ("set ngbehavior=hsa", "set ng_nomodcheck")
W_RES = 1.00  # um; ppolyf_u_1k_6p0 finger width
L_1R = 20.00  # um; 1R unit finger (~20.5 kOhm) for dual 3.3 V (TTGF26c) + 5.0 V (wafer.space)
L_2R = 19.80  # um; 2 * L_2R + clkbuf_20 R_on ~= 2 * L_1R

_DC_SIM = None  # retain libngspice ctypes callback closures until next ngSpice_Init

# (corner, rcorner, vdd_V, temp_C, max_dnl_lsb, max_inl_lsb)
PVT_CORNERS: tuple[tuple[str, str, float, float, float, float], ...] = (
    # TTGF26c 3.3 V +/- 10% PVT (correlated, skewed, and uncorrelated MOS/res cross-corners)
    ("typical", "res_typical", 3.3, 27.0, 0.25, 0.50),
    ("ss", "res_ss", 3.0, 125.0, 0.50, 0.50),
    ("ff", "res_ff", 3.6, -40.0, 0.50, 0.50),
    ("sf", "res_typical", 3.3, 27.0, 0.25, 0.50),
    ("fs", "res_typical", 3.3, 27.0, 0.25, 0.50),
    ("ss", "res_ff", 3.0, 125.0, 0.95, 0.95),
    ("ff", "res_ss", 3.6, -40.0, 0.75, 0.50),
    # wafer.space 5.0 V +/- 10% PVT
    ("typical", "res_typical", 5.0, 27.0, 0.50, 0.50),
    ("ss", "res_ss", 4.5, 125.0, 0.25, 0.50),
    ("ff", "res_ff", 5.5, -40.0, 0.55, 0.50),
)


def _m1_strap_y(c: kf.DKCell, x: float, y0: float, y1: float, w: float = 0.34, tech=gf180) -> None:
    hw = w / 2
    analog.box(c, x - hw, min(y0, y1) - hw, x + hw, max(y0, y1) + hw, tech.LAYER.metal1)


def _m1_hbar(c: kf.DKCell, x0: float, x1: float, y: float, w: float = 0.34, tech=gf180) -> None:
    hw = w / 2
    analog.box(c, min(x0, x1) - hw, y - hw, max(x0, x1) + hw, y + hw, tech.LAYER.metal1)


def route_m1_strap(c: kf.DKCell, start_ports: list[kf.DPort], end_ports: list[kf.DPort]) -> None:
    """Horizontal Metal1 series strap connecting adjacent resistor fingers in a row."""
    for p1, p2 in zip(start_ports, end_ports):
        _m1_hbar(c, round(p1.dx, 3), round(p2.dx, 3), round(p1.dy, 3))


def route_m1_spine(c: kf.DKCell, start_ports: list[kf.DPort], end_ports: list[kf.DPort]) -> None:
    """Schematic routing strategy: pure Metal1 Z-route across resistor rows (zero Via1)."""
    for p1, p2 in zip(start_ports, end_ports):
        x1, y1 = round(p1.dx, 3), round(p1.dy, 3)
        x2, y2 = round(p2.dx, 3), round(p2.dy, 3)
        y_mid = analog.snap_grid((y1 + y2) / 2)
        _m1_strap_y(c, x1, y1, y_mid)
        _m1_hbar(c, x1, x2, y_mid)
        _m1_strap_y(c, x2, y_mid, y2)


def _row_finger_xs(
    lengths: tuple[float, ...], x_margin: float, tech=gf180
) -> tuple[list[float], float]:
    """Return `(finger_center_xs, row_x1)` for a guarded horizontal strip starting at `x_margin`."""
    cur_x = round(x_margin + tech.HRES_GUARD_X + 0.20, 3)
    centers: list[float] = []
    for i, l_val in enumerate(lengths):
        half_span = round(l_val / 2 + tech.HRES_HEAD_X, 3)
        cx = round(cur_x + half_span, 3)
        centers.append(cx)
        cur_x = round(cx + half_span + (tech.HRES_GAP_X if i + 1 < len(lengths) else 0.0), 3)
    row_x1 = round(cur_x + 0.20 + tech.HRES_GUARD_X, 3)
    return centers, row_x1


def _add_finger_row(
    s: kf.DSchematic,
    prefix: str,
    lengths: tuple[float, ...],
    w: float,
    x_margin: float,
    y: float,
    tech=gf180,
) -> list:
    centers, _ = _row_finger_xs(lengths, x_margin, tech=tech)
    refs = []
    for k, (cx, l_val) in enumerate(zip(centers, lengths)):
        inst = s.create_inst(
            f"{prefix}_{k}",
            tech.RES_CELL,
            settings={"w": w, "length": l_val},
            placement=Placement(x=cx, y=y),
        )
        if k > 0:
            s.add_route(f"{prefix}_s{k}", [refs[-1]["r1"]], [inst["r0"]], "m1_strap")
        refs.append(inst)
    return refs


def r2r_schematic(
    name: str,
    bits: int,
    l_1r: float,
    l_2r: float,
    w: float = W_RES,
    sum_res: bool = False,
    y_pitch: float = 2.80,
    x_margin: float = 0.84,
    y_margin: float = 1.96,
    tech=gf180,
) -> tuple[kf.DSchematic, list[tuple[float, float, float]]]:
    """Pure-passive N-bit R-2R ladder schematic built from single resistor finger primitives.

    Row 0:     term  --l_2r--l_2r-- (out) -> Row 1's tap
    Row b+1:   in[b] --l_2r--l_2r-- (tap) --l_1r-- (out) -> next tap [or sum_in on top]
    """
    s = kf.DSchematic(name=name)
    rows_meta: list[tuple[float, float, float]] = []

    term_lengths = (l_2r, l_2r)
    _, term_x1 = _row_finger_xs(term_lengths, x_margin, tech=tech)
    rows_meta.append((x_margin, term_x1, y_margin))
    term_refs = _add_finger_row(s, "term", term_lengths, w, x_margin, y_margin, tech=tech)
    s.add_port("term", port=term_refs[0]["r0"])
    prev = term_refs[-1]["r1"]

    for b in range(bits):
        y = round(y_margin + (b + 1) * y_pitch, 3)
        lengths = (l_2r, l_2r, l_1r) if (b + 1 < bits or sum_res) else (l_2r, l_2r)
        _, row_x1 = _row_finger_xs(lengths, x_margin, tech=tech)
        rows_meta.append((x_margin, row_x1, y))
        refs = _add_finger_row(s, f"r2r_{b}", lengths, w, x_margin, y, tech=tech)
        s.add_port(f"in[{b}]", port=refs[0]["r0"])
        s.add_route(f"spine_{b}", [prev], [refs[1]["r1"]], "m1_spine")
        if b + 1 < bits:
            prev = refs[-1]["r1"]
        else:
            s.add_port("out", port=refs[1]["r1"])
            if sum_res:
                s.add_port("sum_in", port=refs[-1]["r1"])

    return s, rows_meta


def build_r2r(
    bits: int = BITS,
    l_1r: float = L_1R,
    l_2r: float = L_2R,
    w: float = W_RES,
    sum_res: bool = False,
    name: str | None = None,
    y_pitch: float = 2.80,
    x_margin: float = 0.84,
    y_margin: float = 1.96,
    tech=gf180,
) -> tuple[kf.DSchematic, kf.DKCell, list[str]]:
    """Realize a pure-passive R-2R `DKCell` with Metal2 pins and DRC-safe outer dualgate margins."""
    cell_name = name or f"r2r_ladder_{bits}bit{'_sum' if sum_res else ''}"
    s, rows_meta = r2r_schematic(
        name=cell_name,
        bits=bits,
        l_1r=l_1r,
        l_2r=l_2r,
        w=w,
        sum_res=sum_res,
        y_pitch=y_pitch,
        x_margin=x_margin,
        y_margin=y_margin,
        tech=tech,
    )
    cell = s.create_cell(
        kf.DKCell,
        routing_strategies={"m1_strap": route_m1_strap, "m1_spine": route_m1_spine},
    )
    for x0, x1, y in rows_meta:
        tech.add_hres_row_guard(cell, x0, x1, y, w=w)
    max_x1 = max(x1 for _, x1, _ in rows_meta)
    width = round(math.ceil((max_x1 + x_margin) / tech.SITE_W) * tech.SITE_W, 3)
    height = round(2 * y_margin + bits * y_pitch, 3)
    analog.box(cell, 0.0, 0.0, width, height, tech.LAYER.pr_bndry)
    for p in cell.ports:
        analog.add_m2_pin(
            cell,
            p,
            "OUTPUT" if p.name == "out" else "INPUT",
            analog=(p.name in ("out", "sum_in")),
            tech=tech,
        )
    cell.info["width"] = width
    cell.info["height"] = height
    cell.info["area"] = width * height
    cell.flatten()
    pins = [
        "VSS",
        "term",
        *[f"in[{b}]" for b in range(bits - 1, -1, -1)],
        "out",
        *(["sum_in"] if sum_res else []),
    ]
    return s, cell, pins


def build_dac(
    l_1r: float = L_1R, l_2r: float = L_2R, w: float = W_RES
) -> tuple[kf.DSchematic, kf.DKCell, list[str]]:
    """Build passive 8-bit R-2R ladder schematic, flattened layout, and canonical LVS pin list."""
    return build_r2r(bits=BITS, l_1r=l_1r, l_2r=l_2r, w=w, name=CELL_NAME)


@dataclass(frozen=True)
class DacCornerResult:
    """Structured result of one 256-code ngspice PVT corner simulation."""

    corner: str
    rcorner: str
    vdd: float
    temp: float
    v_min: float
    v_max: float
    linearity: analog.LinearityResult
    max_dnl_limit: float
    max_inl_limit: float
    i_off_na: float
    max_i_off_na: float = 10.0

    @property
    def ok(self) -> bool:
        return (
            self.linearity.monotonic
            and self.linearity.max_dnl < self.max_dnl_limit
            and self.linearity.max_inl < self.max_inl_limit
            and self.i_off_na < self.max_i_off_na
        )


def simulate_dac_corners(
    spice: Path,
    corners: tuple[tuple[str, str, float, float, float, float], ...] = PVT_CORNERS,
) -> list[DacCornerResult]:
    """Run parallel multi-corner 256-code `ngspice` DC sweeps on `spice` and return results."""

    def _run_corner(spec: tuple[str, str, float, float, float, float]) -> DacCornerResult:
        corner, rcorner, vdd, temp, max_dnl, max_inl = spec
        _, samples, currents = analog.run_ngspice(
            SRC_DIR / "dac_dc.spice", spice, corner, rcorner, vdd=vdd, temp=temp
        )
        return DacCornerResult(
            corner=corner,
            rcorner=rcorner,
            vdd=vdd,
            temp=temp,
            v_min=samples[0],
            v_max=samples[-1],
            linearity=analog.measure_linearity(samples),
            max_dnl_limit=max_dnl,
            max_inl_limit=max_inl,
            i_off_na=abs(currents[0]) * 1e9,
        )

    with ThreadPoolExecutor(max_workers=min(len(corners), os.cpu_count() or 4)) as ex:
        return list(ex.map(_run_corner, corners))


def extract_dac_pex(
    gds: Path,
    out_dir: Path,
    cell_name: str = CELL_NAME,
    bits: int = BITS,
    pins: list[str] | None = None,
) -> analog.PexResult:
    """Extract polygon-level Magic PEX (`<cell>.pex.spice`) from `gds`."""
    canonical_pins = pins or [
        "VSS",
        "term",
        *[f"in[{b}]" for b in range(bits - 1, -1, -1)],
        "out",
    ]
    pex_spice = out_dir / f"{cell_name}.pex.spice"
    return analog.run_magic_pex(gds, cell_name, canonical_pins, pex_spice, out_dir / "pex")


def ideal_voltage(code: int, vdd: float = 3.3, bits: int = BITS) -> float:
    """Return ideal unloaded R-2R DAC output voltage `vdd * code / 2**bits`."""
    return vdd * code / (1 << bits)


def find_spice(explicit: Path | None = None, prefer_pex: bool = True) -> Path | None:
    """Locate the generated passive `r2r_ladder_8bit` SPICE netlist (preferring Magic `.pex.spice`)."""
    candidates: list[Path] = []
    if explicit is not None:
        candidates.append(Path(explicit))
    env_path = os.environ.get("R2R_SPICE")
    if env_path:
        candidates.append(Path(env_path))
    suffixes = (".pex.spice", ".spice") if prefer_pex else (".spice", ".pex.spice")
    for base in (REPO_ROOT / "gen/dac", REPO_ROOT / "gen_tt/dac"):
        for suf in suffixes:
            candidates.append(base / f"{CELL_NAME}{suf}")
    for path in candidates:
        if path.is_file():
            return path
    return None


def find_spef(explicit: Path | None = None) -> Path | None:
    """Locate the post-PnR OpenRCX SPEF file (preferring worst-case `max`, then `nom`)."""
    candidates: list[Path] = []
    if explicit is not None:
        p = Path(explicit)
        if p.is_file():
            return p
        if p.is_dir():
            for corner in ("max", "nom", "min"):
                candidates.extend(sorted((p / corner).glob("*.spef")))
            candidates.extend(sorted(p.rglob("*.spef")))
    env_path = os.environ.get("SPEF_FILE")
    if env_path:
        p = Path(env_path)
        if p.is_file():
            return p
        if p.is_dir():
            for corner in ("max", "nom", "min"):
                candidates.extend(sorted((p / corner).glob("*.spef")))
            candidates.extend(sorted(p.rglob("*.spef")))
    spef_dir = REPO_ROOT / "gen/final/spef"
    for corner in ("max", "nom", "min"):
        candidates.extend(sorted((spef_dir / corner).glob("*.spef")))
    candidates.extend(sorted(spef_dir.rglob("*.spef")))
    for path in candidates:
        if path.is_file():
            return path
    return None


@dataclass(frozen=True)
class ChannelParasitics:
    """Post-PnR SPEF routing R/C for one DAC channel: output net + `clkbuf_20` driver nets."""

    out_rc: tuple[float, float] = (0.0, 0.0)
    term_rc: tuple[float, float] = (0.0, 0.0)
    bit_rc: tuple[tuple[float, float], ...] = ()
    out_net: str = ""

    @property
    def drv_res_ohms(self) -> list[float]:
        return [self.term_rc[0], *(r for r, _ in self.bit_rc)]


@dataclass(frozen=True)
class SpefCheckResult:
    """Structured result of parsing and validating DAC routing parasitics in a SPEF file."""

    spef_path: Path
    channels: dict[int, ChannelParasitics]
    violations: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.violations


def _natural_key(text: str) -> list[int | str]:
    norm = re.sub(r"__(\d+)_inst(?=\.|$)", r"[\1]_inst", text)
    norm = re.sub(r"(?<!\])_inst(?=\.|$)", "[0]_inst", norm)
    return [int(tok) if tok.isdigit() else tok for tok in re.split(r"(\d+)", norm)]


def check_dac_parasitics(
    spef_path: Path,
    channels: int = 5,
    bits: int = BITS,
    max_out_r_ohm: float = 500.0,
    max_out_c_pf: float = 5.0,
    max_drv_r_ohm: float = 200.0,
    max_drv_c_pf: float = 5.0,
    out_prefix: str | None = None,
    out_nets: tuple[str, ...] | list[str] | None = None,
) -> SpefCheckResult:
    """Parse and validate post-PnR `ua`/`dac_out` and `in_drv[0..bits-1]`/`term_drv` SPEF R/C."""
    if out_nets is not None:
        if len(out_nets) != channels:
            raise ValueError(f"Expected {channels} out_nets, got {len(out_nets)}: {out_nets}")
        candidate_groups: tuple[tuple[str, ...], ...] = (tuple(out_nets),)
    else:
        prefixes_to_check = (out_prefix,) if out_prefix is not None else ("ua", "dac_out")
        candidate_groups = tuple(
            tuple(f"{pfx}[{ch}]" for ch in range(channels)) for pfx in prefixes_to_check
        )
    all_out_nets = {n for grp in candidate_groups for n in grp}
    drv_re = re.compile(r"^(.+)\.(in_drv\[(\d+)\]|term_drv)$")

    def _want(net: str) -> bool:
        return net in all_out_nets or drv_re.match(net) is not None

    parsed = analog.parse_spef_net_rc(spef_path, predicate=_want)
    violations: list[str] = []

    resolved_out_nets = next(
        (grp for grp in candidate_groups if all(n in parsed for n in grp)),
        candidate_groups[0],
    )
    missing_out = [n for n in resolved_out_nets if n not in parsed]
    if missing_out:
        violations.append(f"{spef_path}: missing analog output net(s) in SPEF: {missing_out}")

    by_prefix: dict[str, dict[str, tuple[float, float]]] = {}
    for net, rc in parsed.items():
        m = drv_re.match(net)
        if m:
            prefix, pin_name = m.group(1), m.group(2)
            by_prefix.setdefault(prefix, {})[pin_name] = rc

    # Distinguish 8-bit DAC instances from 4-bit ADC instances that also define in_drv/term_drv.
    dac_prefixes = sorted(
        (
            prefix
            for prefix, pins_map in by_prefix.items()
            if sum(1 for k in pins_map if k.startswith("in_drv[")) == bits
        ),
        key=_natural_key,
    )
    if len(dac_prefixes) != channels:
        violations.append(
            f"{spef_path}: expected {channels} {bits}-bit DAC driver instances in SPEF, "
            f"found {len(dac_prefixes)}: {dac_prefixes}"
        )

    by_ch: dict[int, ChannelParasitics] = {}
    if not missing_out and len(dac_prefixes) == channels:
        for ch, (prefix, out_name) in enumerate(zip(dac_prefixes, resolved_out_nets, strict=True)):
            out_r, out_c = parsed[out_name]
            if out_r >= max_out_r_ohm:
                violations.append(
                    f"{spef_path}: {out_name} routing R={out_r:.2f} Ohm >= {max_out_r_ohm:.1f} Ohm"
                )
            if out_c >= max_out_c_pf:
                violations.append(
                    f"{spef_path}: {out_name} routing C={out_c:.4f} pF >= {max_out_c_pf:.2f} pF"
                )
            pins_map = by_prefix[prefix]
            if "term_drv" not in pins_map:
                violations.append(f"{spef_path}: missing {prefix}.term_drv in SPEF")
                continue
            term_rc = pins_map["term_drv"]
            bit_rc_list: list[tuple[float, float]] = []
            missing_bit = False
            for b in range(bits):
                key = f"in_drv[{b}]"
                if key not in pins_map:
                    violations.append(f"{spef_path}: missing {prefix}.{key} in SPEF")
                    missing_bit = True
                else:
                    bit_rc_list.append(pins_map[key])
            if missing_bit:
                continue
            for pin_name, (dr, dc) in [("term_drv", term_rc)] + [
                (f"in_drv[{b}]", bit_rc_list[b]) for b in range(bits)
            ]:
                if dr >= max_drv_r_ohm:
                    violations.append(
                        f"{spef_path}: ch{ch} {pin_name} R={dr:.2f} Ohm >= {max_drv_r_ohm:.1f} Ohm"
                    )
                if dc >= max_drv_c_pf:
                    violations.append(
                        f"{spef_path}: ch{ch} {pin_name} C={dc:.4f} pF >= {max_drv_c_pf:.2f} pF"
                    )
            by_ch[ch] = ChannelParasitics(
                out_rc=(out_r, out_c),
                term_rc=term_rc,
                bit_rc=tuple(bit_rc_list),
                out_net=out_name,
            )
    return SpefCheckResult(spef_path=spef_path, channels=by_ch, violations=tuple(violations))


def format_parasitics_summary(parasitics: dict[int, ChannelParasitics]) -> str:
    """Format a compact one-line summary of per-channel output & driver SPEF parasitics."""
    parts: list[str] = []
    for ch, p in sorted(parasitics.items()):
        out_r, out_c = p.out_rc
        label = p.out_net or f"ua[{ch}]"
        if p.bit_rc:
            rs = p.drv_res_ohms
            in7_r = p.bit_rc[-1][0]
            parts.append(
                f"{label}={out_r:.1f}Ohm/{out_c:.3f}pF "
                f"(drv={min(rs):.1f}..{max(rs):.1f}Ohm, in7={in7_r:.1f}Ohm)"
            )
        else:
            parts.append(f"{label}={out_r:.1f}Ohm/{out_c:.3f}pF")
    return ", ".join(parts)


def _coerce_parasitics(
    route_rc: dict[int, tuple[float, float] | ChannelParasitics] | None,
) -> dict[int, ChannelParasitics] | None:
    if not route_rc:
        return None
    out: dict[int, ChannelParasitics] = {}
    for ch, val in route_rc.items():
        if isinstance(val, ChannelParasitics):
            out[ch] = val
        else:
            out[ch] = ChannelParasitics(out_rc=val)
    return out


def _subckt_lines(
    bits: int,
    subckt: str,
    cell_name: str,
    route_r_ohm: float = 0.0,
    route_c_pf: float = 0.0,
    term_rc: tuple[float, float] = (0.0, 0.0),
    bit_rc: tuple[tuple[float, float], ...] = (),
) -> list[str]:
    in_pins = " ".join(f"in{b}" for b in range(bits))
    lines = [
        f".subckt {subckt} term {in_pins} out vdd vss",
        f"X_TERM term drv_term vdd vdd vss vss {DRIVER_CELL}",
        *[f"X_B{b} in{b} drv{b} vdd vdd vss vss {DRIVER_CELL}" for b in range(bits)],
    ]

    term_r, term_c = term_rc
    term_node = "drv_term"
    if term_r > 0.0 or term_c > 0.0:
        if term_r > 0.0:
            lines.append(f"R_DRV_TERM drv_term r2r_term {term_r:.4f}")
            term_node = "r2r_term"
        if term_c > 0.0:
            lines.append(f"C_DRV_TERM {term_node} vss {term_c:.6f}p")

    r2r_in_nodes: list[str] = []
    for b in range(bits):
        br, bc = bit_rc[b] if b < len(bit_rc) else (0.0, 0.0)
        node = f"drv{b}"
        if br > 0.0 or bc > 0.0:
            if br > 0.0:
                lines.append(f"R_DRV{b} drv{b} r2r_in{b} {br:.4f}")
                node = f"r2r_in{b}"
            if bc > 0.0:
                lines.append(f"C_DRV{b} {node} vss {bc:.6f}p")
        r2r_in_nodes.append(node)

    r2r_pins_msb = " ".join(r2r_in_nodes[b] for b in range(bits - 1, -1, -1))
    lines.append(f"XR2R vss {term_node} {r2r_pins_msb} dac_int {cell_name}")

    if route_r_ohm > 0.0 or route_c_pf > 0.0:
        lines.append(
            f"* Extracted post-PnR top-level route (R={route_r_ohm:.2f} Ohm, C={route_c_pf:.4f} pF)"
        )
        src_node = "dac_int"
        if route_r_ohm > 0.0:
            lines.append(f"R_ROUTE dac_int ua_pin {route_r_ohm:.4f}")
            src_node = "ua_pin"
        if route_c_pf > 0.0:
            lines.append(f"C_ROUTE {src_node} vss {route_c_pf:.6f}p")
        lines += [
            "* Tiny Tapeout analog mux (~500 Ohm) + pad/board capacitance (~10 pF)",
            f"R_MUX {src_node} out {TT_MUX_R_OHM}",
            f"C_PAD out vss {TT_PAD_C_PF}p",
        ]
    else:
        lines += [
            "* Tiny Tapeout analog mux (~500 Ohm) + pad/board capacitance (~10 pF), tau ~ 215 ns",
            f"R_MUX dac_int out {TT_MUX_R_OHM}",
            f"C_PAD out vss {TT_PAD_C_PF}p",
        ]
    lines.append(f".ends {subckt}")
    return lines


def export_ams_spice(
    r2r_ng_spice: Path,
    bits: int = BITS,
    subckt: str = AMS_SUBCKT,
    cell_name: str = CELL_NAME,
    route_r_ohm: float = 0.0,
    route_c_pf: float = 0.0,
    route_rc: dict[int, tuple[float, float] | ChannelParasitics] | None = None,
) -> str:
    """Return ngspice `.subckt` wrapper: `bits+1` `clkbuf_20` drivers + R-2R ladder + route/mux RC."""
    parasitics = _coerce_parasitics(route_rc)
    lines = [
        f"* cocotbext-ams wrapper: {bits + 1}x clkbuf_20 + {cell_name} + post-PnR route & TT mux RC",
        f".include {r2r_ng_spice.resolve()}",
        "",
        *_subckt_lines(
            bits=bits,
            subckt=subckt,
            cell_name=cell_name,
            route_r_ohm=route_r_ohm,
            route_c_pf=route_c_pf,
        ),
    ]
    if parasitics:
        for ch, p in sorted(parasitics.items()):
            lines.append("")
            lines.extend(
                _subckt_lines(
                    bits=bits,
                    subckt=f"{subckt}_ch{ch}",
                    cell_name=cell_name,
                    route_r_ohm=p.out_rc[0],
                    route_c_pf=p.out_rc[1],
                    term_rc=p.term_rc,
                    bit_rc=p.bit_rc,
                )
            )
    lines.append("")
    return "\n".join(lines)


def prepare_ams_spice(
    out_dir: Path,
    r2r_spice: Path,
    route_rc: dict[int, tuple[float, float] | ChannelParasitics] | None = None,
    tech=gf180,
) -> Path:
    """Emit ngspice-ready `r2r_ladder_8bit` + `sc_r2r_dac_8bit` wrapper in `out_dir`."""
    out_dir.mkdir(parents=True, exist_ok=True)
    r2r_ng = out_dir / f"{CELL_NAME}_ngspice.cir"
    r2r_ng.write_text(analog.spice_to_ngspice(r2r_spice, tech=tech))
    wrapper = out_dir / f"{AMS_SUBCKT}_ams.spice"
    parasitics = _coerce_parasitics(route_rc)
    max_r = max((p.out_rc[0] for p in parasitics.values()), default=0.0) if parasitics else 0.0
    max_c = max((p.out_rc[1] for p in parasitics.values()), default=0.0) if parasitics else 0.0
    wrapper.write_text(
        export_ams_spice(r2r_ng, route_r_ohm=max_r, route_c_pf=max_c, route_rc=parasitics)
    )
    return wrapper


def dc_lut_netlist(
    wrapper_spice: Path,
    vdd: float = 3.3,
    corner: str = "typical",
    rcorner: str = "res_typical",
    temp: float | None = None,
    subckts: list[str] | None = None,
    pdk_root: Path | None = None,
    tech=gf180,
) -> list[str]:
    """Return ngspice deck lines for a 256-code DC transfer sweep of one or more DAC subcircuits."""
    bit_pins = " ".join(f"d{b}" for b in range(BITS))
    targets = subckts or [AMS_SUBCKT]
    if len(targets) == 1:
        inst_lines = [f"XDAC term {bit_pins} dac_out vdd vss {targets[0]}"]
        save_line = ".save v(dac_out)"
    else:
        inst_lines = [
            f"XDAC{ch} term {bit_pins} dac_out{ch} vdd vss {sc}" for ch, sc in enumerate(targets)
        ]
        save_line = ".save " + " ".join(f"v(dac_out{ch})" for ch in range(len(targets)))
    return [
        "* 256-code DC LUT sweep via libngspice0",
        *analog.ngspice_pdk_lines(
            corner, rcorner, includes=[wrapper_spice], pdk_root=pdk_root, temp=temp, tech=tech
        ),
        f"VDD vdd 0 {vdd}",
        "VSS vss 0 0.0",
        "VCODE vcode 0 0.0",
        "VTERM term 0 0.0",
        *[
            f"B_D{b} d{b} 0 V = "
            f"(floor(floor(v(vcode)+0.5)/{1 << b}) - "
            f"2*floor(floor(v(vcode)+0.5)/{1 << (b + 1)})) > 0.5 ? v(vdd) : 0.0"
            for b in range(BITS)
        ],
        *inst_lines,
        save_line,
        ".options klu",
        ".end",
    ]


def measure_dc_luts(
    r2r_spice: Path,
    channels: int = 5,
    vdd: float = 3.3,
    corner: str = "typical",
    rcorner: str = "res_typical",
    temp: float | None = None,
    route_rc: dict[int, tuple[float, float] | ChannelParasitics] | None = None,
    pdk_root: Path | None = None,
    tech=gf180,
) -> list[list[float]]:
    """Measure per-channel 256-code BSIM3v3 + R-2R (+ SPEF RC) DC curves in one libngspice0 sweep."""
    global _DC_SIM
    from cocotbext.ams._ngspice import NgspiceInterface

    n_codes = 1 << BITS
    parasitics = _coerce_parasitics(route_rc)
    has_per_ch_drv = bool(
        parasitics and any(p.bit_rc or p.term_rc[0] > 0 for p in parasitics.values())
    )
    if has_per_ch_drv and parasitics is not None:
        subckts = [
            f"{AMS_SUBCKT}_ch{ch}" if ch in parasitics else AMS_SUBCKT for ch in range(channels)
        ]
    else:
        subckts = [AMS_SUBCKT]

    with tempfile.TemporaryDirectory(prefix="dac_dc_lut_") as tmp:
        wrapper = prepare_ams_spice(Path(tmp), r2r_spice, route_rc=parasitics, tech=tech)
        lines = dc_lut_netlist(
            wrapper,
            vdd=vdd,
            corner=corner,
            rcorner=rcorner,
            temp=temp,
            subckts=subckts,
            pdk_root=pdk_root,
            tech=tech,
        )
        sim = NgspiceInterface()
        _DC_SIM = sim  # retain callback closures until next ngSpice_Init
        for cmd in AMS_INIT_COMMANDS:
            sim.command(cmd)
        sim.load_circuit(lines)
        sim.command(f"dc vcode 0 {n_codes - 1} 1")
        if len(subckts) == 1:
            vec = sim._lib.ngGet_Vec_Info(b"dac_out")
            if not vec:
                raise RuntimeError("ngspice DC LUT sweep did not produce dac_out vector")
            vi = vec.contents
            if vi.v_length != n_codes:
                raise RuntimeError(f"Expected {n_codes} DC LUT points, got {vi.v_length}")
            shared = [float(vi.v_realdata[i]) for i in range(n_codes)]
            return [list(shared) for _ in range(channels)]
        luts: list[list[float]] = []
        for ch in range(len(subckts)):
            vec = sim._lib.ngGet_Vec_Info(f"dac_out{ch}".encode())
            if not vec:
                raise RuntimeError(f"ngspice DC LUT sweep did not produce dac_out{ch} vector")
            vi = vec.contents
            if vi.v_length != n_codes:
                raise RuntimeError(
                    f"Expected {n_codes} DC LUT points for ch{ch}, got {vi.v_length}"
                )
            luts.append([float(vi.v_realdata[i]) for i in range(n_codes)])
        return luts


def measure_dc_lut(
    r2r_spice: Path,
    vdd: float = 3.3,
    corner: str = "typical",
    rcorner: str = "res_typical",
    temp: float | None = None,
    route_rc: dict[int, tuple[float, float] | ChannelParasitics] | None = None,
    pdk_root: Path | None = None,
    tech=gf180,
) -> list[float]:
    """Measure the 256-code BSIM3v3 + R-2R DC transfer curve in-process via `libngspice0`."""
    return measure_dc_luts(
        r2r_spice,
        channels=1,
        vdd=vdd,
        corner=corner,
        rcorner=rcorner,
        temp=temp,
        route_rc=route_rc,
        pdk_root=pdk_root,
        tech=tech,
    )[0]


def ams_block(
    name: str,
    wrapper_spice: Path,
    handle,
    vdd: float = 3.3,
    corner: str = "typical",
    rcorner: str = "res_typical",
    temp: float | None = None,
    edge_time_ns: float = 5.0,
    tran_step: str = "5u",
    subckt: str = AMS_SUBCKT,
    pdk_root: Path | None = None,
    tech=gf180,
):
    """Build a `cocotbext-ams` `AnalogBlock` binding a `u_r2r` handle to `sc_r2r_dac_8bit`."""
    from cocotbext.ams import AnalogBlock, DigitalPin

    return AnalogBlock(
        name=name,
        spice_file=wrapper_spice,
        subcircuit=subckt,
        digital_pins={
            "term": DigitalPin(direction="input", width=1, vdd=vdd, vss=0.0),
            "in": DigitalPin(direction="input", width=BITS, vdd=vdd, vss=0.0),
        },
        analog_outputs=["out"],
        vdd=vdd,
        vss=0.0,
        tran_step=tran_step,
        extra_lines=[
            *analog.ngspice_pdk_lines(corner, rcorner, pdk_root=pdk_root, temp=temp, tech=tech),
            ".options klu method=gear",
        ],
        init_commands=list(AMS_INIT_COMMANDS),
        edge_time_ns=edge_time_ns,
        handle=handle,
    )


def _report_dac_corners(results: list[DacCornerResult], label_prefix: str) -> None:
    for r in results:
        label = f"{label_prefix} {r.corner}/{r.rcorner} {r.vdd:.1f}V {r.temp:.0f}C"
        assert r.linearity.monotonic, f"{label}: non-monotonic!"
        assert r.linearity.max_dnl < r.max_dnl_limit and r.linearity.max_inl < r.max_inl_limit, (
            f"{label}: linearity failed "
            f"(max|DNL|={r.linearity.max_dnl:.4f} >= {r.max_dnl_limit}, "
            f"max|INL|={r.linearity.max_inl:.4f} >= {r.max_inl_limit})"
        )
        assert r.i_off_na < r.max_i_off_na, (
            f"{label}: code=0 standby I(VDD)={r.i_off_na:.2f} nA >= {r.max_i_off_na} nA"
        )
        print(
            f"  [{label_prefix} | {r.corner:7s}/{r.rcorner:11s} {r.vdd:.1f}V {r.temp:4.0f}C] "
            f"256-code ngspice OK: Vout={r.v_min * 1e3:.2f}mV..{r.v_max:.4f}V, "
            f"max|DNL|={r.linearity.max_dnl:.4f} LSB, max|INL|={r.linearity.max_inl:.4f} LSB, "
            f"I_off={r.i_off_na:.2f} nA"
        )


def main() -> None:
    ap = argparse.ArgumentParser(description="Build & verify passive N-bit R-2R ladder / DAC.")
    ap.add_argument("--width", type=int, default=BITS, help="Ladder bit width (default: 8)")
    ap.add_argument("--l-1r", type=float, default=L_1R)
    ap.add_argument("--l-2r", type=float, default=L_2R)
    ap.add_argument("--w-res", type=float, default=W_RES)
    ap.add_argument("--sum-res", action="store_true", help="Add output 1R summing branch (ADC)")
    ap.add_argument("--name", type=str, default=None)
    ap.add_argument("--out-dir", type=Path, default=None)
    ap.add_argument(
        "--check-spef",
        type=Path,
        default=None,
        help="Verify post-PnR ua/dac_out and in_drv/term_drv routing parasitics in SPEF and exit",
    )
    ap.add_argument(
        "--channels",
        type=int,
        default=5,
        help="Number of DAC channels expected by --check-spef (default: 5)",
    )
    ap.add_argument(
        "--out-nets",
        type=str,
        default=None,
        help="Comma-separated analog output net names for --check-spef (e.g. ua[4],ua[5])",
    )
    args = ap.parse_args()

    if args.check_spef is not None:
        spef_files = (
            [args.check_spef]
            if args.check_spef.is_file()
            else sorted(args.check_spef.rglob("*.spef"))
        )
        assert spef_files, f"No SPEF files found under {args.check_spef}"
        out_nets = (
            [n.strip() for n in args.out_nets.split(",") if n.strip()] if args.out_nets else None
        )
        channels = len(out_nets) if out_nets is not None else args.channels
        for spef in spef_files:
            res = check_dac_parasitics(spef, channels=channels, out_nets=out_nets)
            assert res.ok, "; ".join(res.violations)
            print(f"  [SPEF check | {spef.name}] OK: {format_parasitics_summary(res.channels)}")
        return

    tool_checks = tools.check_native_tools(("klayout", "magic"))
    bad_tools = [c for c in tool_checks if not c.ok]
    assert not bad_tools, (
        "Native tool version mismatch vs LibreLane "
        f"{tools.LIBRELANE_VERSION}: "
        + ", ".join(f"{c.name}={c.actual} (expected {c.expected})" for c in bad_tools)
    )

    out_dir = args.out_dir or (
        REPO_ROOT / "gen/dac" if args.width == BITS and not args.sum_res else REPO_ROOT / "gen/r2r"
    )
    s, cell, pins = build_r2r(
        bits=args.width,
        l_1r=args.l_1r,
        l_2r=args.l_2r,
        w=args.w_res,
        sum_res=args.sum_res,
        name=args.name,
    )
    gds, _, _, spice = analog.emit_macro(s, cell, pins, out_dir)

    drc = analog.run_klayout_drc(gds, out_dir / "drc")
    assert drc.clean, f"KLayout DRC failed on {gds}:\n{drc.log}"
    print(f"  [Analog check | {cell.name}] KLayout DRC clean.")

    lvs = analog.run_klayout_lvs(gds, spice, out_dir / "lvs", cell.name, pins=pins)
    assert lvs.matched, f"KLayout LVS mismatch on {cell.name}:\n{lvs.log}"
    assert lvs.pins_match, (
        f"LVS extracted pin mismatch on {cell.name}: "
        f"extracted={sorted(lvs.ext_pins)} vs expected={sorted(lvs.expected_pins)}"
    )
    print(f"  [Analog check | {cell.name}] KLayout LVS (component-graph schematic vs GDS) matched.")

    is_8bit_dac = args.width == BITS and not args.sum_res
    if is_8bit_dac:
        _report_dac_corners(simulate_dac_corners(spice), label_prefix="DAC check")

    pex = extract_dac_pex(gds, out_dir, cell_name=cell.name, bits=args.width, pins=pins)
    assert pex.ok, (
        f"Magic PEX extraction on {cell.name} produced n_res={pex.n_res}, n_cap={pex.n_cap}"
    )
    print(
        f"  [Magic PEX | {cell.name}] Extracted {pex.spice_path} "
        f"(R={pex.n_res}, C={pex.n_cap}, bleed={pex.n_float_bleed})"
    )
    if is_8bit_dac:
        _report_dac_corners(simulate_dac_corners(pex.spice_path), label_prefix="DAC PEX check")

    png = analog.render_gds(gds, out_dir / f"{cell.name}.png", width=1800, height=1200)
    print(f"[dac] Rendered {png}")
    print(
        f"{cell.name}: {cell.info['width']:.2f} x {cell.info['height']:.2f} um "
        f"({cell.info['area']:.2f} um^2)"
    )


if __name__ == "__main__":
    main()
