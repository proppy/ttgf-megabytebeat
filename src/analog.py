# Copyright 2026 Google LLC.
# SPDX-License-Identifier: Apache-2.0
"""Reusable analog layout, LEF/Liberty/SPICE export, KLayout DRC/LVS, ngspice, and rendering helpers."""

from __future__ import annotations

from collections.abc import Iterable
import ctypes
import ctypes.util
from dataclasses import dataclass
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

try:
    import kfactory as kf
    from kfactory import kdb
    from kfactory.netlist import NetlistPort
    from kfactory.schematic import PortRef
    from networkx.utils import UnionFind
except ImportError:  # pragma: no cover - layout/netlist export only needed for GDS generation
    kf = kdb = NetlistPort = PortRef = UnionFind = None  # type: ignore[assignment]

import gf180

Rect = tuple[float, float, float, float]


@dataclass(frozen=True)
class DrcResult:
    """Structured outcome of a KLayout DRC run."""

    clean: bool
    gds_path: Path
    log: str


@dataclass(frozen=True)
class LvsResult:
    """Structured outcome of a KLayout LVS run and extracted pin-set verification."""

    matched: bool
    topcell: str
    ext_pins: tuple[str, ...]
    expected_pins: tuple[str, ...]
    log: str

    @property
    def pins_match(self) -> bool:
        return set(self.ext_pins) == set(self.expected_pins)

    @property
    def ok(self) -> bool:
        return self.matched and self.pins_match


@dataclass(frozen=True)
class PexResult:
    """Structured outcome of a Magic polygon parasitic extraction."""

    spice_path: Path
    cell_name: str
    n_res: int
    n_cap: int
    n_float_bleed: int

    @property
    def ok(self) -> bool:
        return self.n_res > 0 and self.n_cap > 0


@dataclass(frozen=True)
class LinearityResult:
    """Measured monotonicity, LSB step, worst-case |DNL|, and worst-case |INL|."""

    lsb: float
    monotonic: bool
    max_dnl: float
    max_inl: float


def snap_grid(val: float, grid: float = gf180.GRID) -> float:
    """Snap a micron coordinate to the manufacturing `grid`."""
    return round(round(val / grid) * grid, 3)


def box(c: kf.DKCell, x0: float, y0: float, x1: float, y1: float, layer: int) -> None:
    """Insert a rounded micron rectangle into `c` on `layer`."""
    c.shapes(layer).insert(kdb.DBox(round(x0, 3), round(y0, 3), round(x1, 3), round(y1, 3)))


def add_label(c: kf.DKCell, text: str, x: float, y: float, layer: int) -> None:
    """Insert a text label into `c` on `layer` at `(x, y)`."""
    c.shapes(layer).insert(kdb.DText(text, kdb.DTrans(round(x, 3), round(y, 3))))


def add_m2_pin(
    c: kf.DKCell,
    port: kf.DPort,
    direction: str,
    analog: bool = False,
    tech=gf180,
) -> None:
    """Drop a Via1 + 0.56 x 0.80 um Metal2 pin pad on `port` and attach LEF/Liberty/LVS metadata."""
    cx, cy = round(port.dx, 3), round(port.dy, 3)
    box(c, cx - 0.13, cy - 0.13, cx + 0.13, cy + 0.13, tech.LAYER.via1)
    box(c, cx - 0.20, cy - 0.20, cx + 0.20, cy + 0.20, tech.LAYER.metal1)
    rect = (round(cx - 0.28, 3), round(cy - 0.40, 3), round(cx + 0.28, 3), round(cy + 0.40, 3))
    box(c, *rect, tech.LAYER.metal2)
    port.info["rect"] = rect
    port.info["direction"] = direction
    port.info["analog"] = analog
    port.info["layer"] = "Metal2"
    add_label(c, port.name, cx, cy, tech.LAYER.metal2_label)
    # GF180MCU KLayout LVS binds metal2_label to disconnected metal2_ncap; also label M1 under Via1.
    add_label(c, port.name, cx, cy, tech.LAYER.metal1_label)


def export_lef(c: kf.DKCell, tech=gf180) -> str:
    """Return OpenROAD CLASS BLOCK LEF abstract with Metal2 signal pins and M1-M2 obstructions."""
    layer = tech.LAYER
    cell_name = c.name
    total_w = float(c.info["width"])
    total_h = float(c.info["height"])
    dbu = c.kcl.layout.dbu
    kcell = c.kdb_cell

    def _layer_reg(lyr: int) -> kdb.Region:
        return kdb.Region(kcell.begin_shapes_rec(lyr)).merged()

    pin_m2 = kdb.Region()
    for p in c.ports:
        pin_m2.insert(kdb.DBox(*p.info["rect"]).to_itype(dbu))
    obs_m2 = _layer_reg(layer.metal2).not_interacting(pin_m2)

    lines = [
        "VERSION 5.7 ;",
        '  DIVIDERCHAR "/" ;',
        '  BUSBITCHARS "[]" ;',
        f"MACRO {cell_name}",
        "  CLASS BLOCK ;",
        f"  FOREIGN {cell_name} 0.000 0.000 ;",
        "  ORIGIN 0.000 0.000 ;",
        f"  SIZE {total_w:.3f} BY {total_h:.3f} ;",
        "  SYMMETRY X Y ;",
    ]
    for p in c.ports:
        x0, y0, x1, y1 = p.info["rect"]
        lines += [
            f"  PIN {p.name}",
            f"    DIRECTION {p.info['direction']} ;",
            "    USE SIGNAL ;",
            "    PORT",
            f"      LAYER {p.info.get('layer', 'Metal2')} ;",
            f"        RECT {x0:.3f} {y0:.3f} {x1:.3f} {y1:.3f} ;",
            "    END",
            f"  END {p.name}",
        ]
    lines.append("  OBS")
    for lyr_label, reg in (
        ("Metal1", _layer_reg(layer.metal1)),
        ("Via1", _layer_reg(layer.via1)),
        ("Metal2", obs_m2),
    ):
        if not reg.is_empty():
            lines.append(f"      LAYER {lyr_label} ;")
            for poly in reg.each():
                for trap in poly.decompose_trapezoids(kdb.Polygon.TD_htrapezoids):
                    b = trap.bbox().to_dtype(dbu)
                    lines.append(
                        f"        RECT {b.left:.3f} {b.bottom:.3f} {b.right:.3f} {b.top:.3f} ;"
                    )
    lines += ["  END", f"END {cell_name}", "END LIBRARY", ""]
    return "\n".join(lines)


def export_lib(c: kf.DKCell) -> str:
    """Return a minimal OpenSTA/Yosys Liberty stub derived automatically from `c.ports`."""
    cell_name = c.name
    area = float(c.info["area"])
    entries: list[tuple[str, str, dict]] = []  # (kind, name, meta)
    buses: dict[str, dict] = {}
    for p in c.ports:
        m = re.fullmatch(r"(.+)\[(\d+)\]", p.name)
        direction = str(p.info["direction"]).lower()
        is_analog = bool(p.info.get("analog", False))
        cap = 0.0 if direction == "output" else 0.003
        if m:
            bname, idx = m.group(1), int(m.group(2))
            if bname not in buses:
                buses[bname] = {
                    "indices": [],
                    "direction": direction,
                    "analog": is_analog,
                    "cap": cap,
                }
                entries.append(("bus", bname, buses[bname]))
            buses[bname]["indices"].append(idx)
        else:
            entries.append(
                ("pin", p.name, {"direction": direction, "analog": is_analog, "cap": cap})
            )

    lines = [
        f"library({cell_name}_lib) {{",
        "  delay_model : table_lookup;",
        '  time_unit : "1ns";',
        '  voltage_unit : "1V";',
        '  current_unit : "1mA";',
        "  capacitive_load_unit(1, pf);",
        '  pulling_resistance_unit : "1ohm";',
        '  leakage_power_unit : "1uW";',
        "  input_threshold_pct_fall : 50;",
        "  input_threshold_pct_rise : 50;",
        "  output_threshold_pct_fall : 50;",
        "  output_threshold_pct_rise : 50;",
        "  slew_derate_from_library : 0.5;",
        "  slew_lower_threshold_pct_fall : 30;",
        "  slew_lower_threshold_pct_rise : 30;",
        "  slew_upper_threshold_pct_fall : 70;",
        "  slew_upper_threshold_pct_rise : 70;",
        "  default_fanout_load : 1.0;",
        "",
    ]
    for bname, meta in buses.items():
        hi, lo = max(meta["indices"]), min(meta["indices"])
        lines += [
            f"  type(bus_{bname}) {{",
            "    base_type : array;",
            "    data_type : bit;",
            f"    bit_width : {hi - lo + 1};",
            f"    bit_from : {hi};",
            f"    bit_to : {lo};",
            "    downto : true;",
            "  }",
            "",
        ]
    lines += [
        f"  cell({cell_name}) {{",
        f"    area : {area:.2f};",
        "    dont_touch : true;",
        "    dont_use : true;",
    ]
    for kind, name, meta in entries:
        if kind == "bus":
            lines.append(f"    bus({name}) {{")
            lines.append(f"      bus_type : bus_{name};")
        else:
            lines.append(f"    pin({name}) {{")
        lines.append(f"      direction : {meta['direction']};")
        if meta["analog"]:
            lines.append("      is_analog : true;")
        lines.append(f"      capacitance : {meta['cap']};")
        lines.append("    }")
    lines += ["  }", "}", ""]
    return "\n".join(lines)


def export_spice(nl, cell_name: str, pins: list[str], tech=gf180) -> str:
    """Compile a kfactory `DSchematic.netlist()` of primitive resistor fingers into a `.SUBCKT`."""
    uf: UnionFind[str] = UnionFind()

    def _key(ep) -> str:
        if isinstance(ep, NetlistPort):
            return f"PIN:{ep.name}"
        if isinstance(ep, PortRef):
            return f"{ep.instance}:{ep.port}"
        raise TypeError(f"Unsupported net endpoint: {ep!r}")

    for net in nl.nets:
        keys = [_key(ep) for ep in net.root]
        for k in keys[1:]:
            uf.union(keys[0], k)

    root_to_name = {uf[f"PIN:{port.name}"]: port.name for port in nl.ports}
    next_id = 0

    def _net(node: str) -> str:
        nonlocal next_id
        r = uf[node]
        if r not in root_to_name:
            next_id += 1
            root_to_name[r] = f"n_{next_id}"
        return root_to_name[r]

    lines = [
        f"* Component-graph netlist for {cell_name}",
        f".SUBCKT {cell_name} {' '.join(pins)}",
    ]
    for iname, inst in nl.instances.items():
        if inst.component == tech.RES_CELL:
            w = float(inst.settings["w"])
            length = float(inst.settings["length"])
            lines.append(
                f"RR_{iname} {_net(f'{iname}:r0')} {_net(f'{iname}:r1')} VSS "
                f"{tech.RES_MODEL} W={w}e-6 L={length}e-6"
            )
    lines += [f".ENDS {cell_name}", ""]
    return "\n".join(lines)


def emit_macro(
    s: kf.DSchematic, cell: kf.DKCell, pins: list[str], out_dir: Path, tech=gf180
) -> tuple[Path, Path, Path, Path]:
    """Write GDS, LEF, Liberty, and LVS/ngspice SPICE netlist for a passive analog macro."""
    out_dir.mkdir(parents=True, exist_ok=True)
    gds = out_dir / f"{cell.name}.gds"
    lef = out_dir / f"{cell.name}.lef"
    lib = out_dir / f"{cell.name}.lib"
    spice = out_dir / f"{cell.name}.spice"
    cell.write(gds, set_meta_data=False)
    lef.write_text(export_lef(cell, tech=tech))
    lib.write_text(export_lib(cell))
    spice.write_text(export_spice(s.netlist(), cell.name, pins=pins, tech=tech))
    return gds, lef, lib, spice


def run_klayout_drc(
    gds_path: Path, run_dir: Path, tech=gf180, extra_args: tuple[str, ...] = ()
) -> DrcResult:
    """Run PDK KLayout DRC script on `gds_path` and return a structured `DrcResult`."""
    res = subprocess.run(
        [
            sys.executable,
            str(tech.DRC_SCRIPT),
            f"--path={gds_path}",
            f"--run_dir={run_dir}",
            *tech.DRC_ARGS,
            *extra_args,
        ],
        capture_output=True,
        text=True,
    )
    log = res.stdout + res.stderr
    return DrcResult(
        clean=res.returncode == 0 and "GDS has no DRC violations" in log,
        gds_path=gds_path,
        log=log,
    )


def run_klayout_lvs(
    gds_path: Path, spice_path: Path, run_dir: Path, topcell: str, pins: list[str], tech=gf180
) -> LvsResult:
    """Run PDK KLayout LVS script and return structured `LvsResult` with extracted pin names."""
    res = subprocess.run(
        [
            sys.executable,
            str(tech.LVS_SCRIPT),
            f"--layout={gds_path}",
            f"--netlist={spice_path}",
            f"--topcell={topcell}",
            f"--run_dir={run_dir}",
            *tech.LVS_ARGS,
        ],
        capture_output=True,
        text=True,
    )
    log = res.stdout + res.stderr
    matched = res.returncode == 0 and "Congratulations! Netlists match." in log
    ext_pins: tuple[str, ...] = ()
    cir = run_dir / f"{gds_path.stem}.cir"
    if cir.is_file():
        raw = re.sub(r"\n\+\s*", " ", cir.read_text())
        if m := re.search(r"^\.SUBCKT\s+\S+\s+(.*)$", raw, re.M):
            ext_pins = tuple(m.group(1).split())
    return LvsResult(
        matched=matched,
        topcell=topcell,
        ext_pins=ext_pins,
        expected_pins=tuple(pins),
        log=log,
    )


def spice_to_ngspice(spice_path: Path, tech=gf180) -> str:
    """Translate poly resistors in an LVS-verified passive `.spice` subckt to ngspice X-calls."""
    res_pat = re.escape(tech.RES_MODEL)
    return re.sub(
        rf"^RR_(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+({res_pat})\s+W=(\S+)\s+L=(\S+)",
        r"XRR_\1 \2 \3 \4 \5 r_width=\6 r_length=\7",
        spice_path.read_text(),
        flags=re.M,
    )


def normalize_magic_pex_spice(
    raw_spice: str, cell_name: str, pins: list[str]
) -> tuple[str, int, int, int]:
    """Normalize Magic `-f ngspice` PEX subcircuit to canonical `pins` order with DC-safe caps."""
    txt = re.sub(r"\bVSUBS\b", "VSS", raw_spice)
    subckt_hdr = f".subckt {cell_name} {' '.join(pins)}"
    txt, n_hdr = re.subn(
        rf"^\.subckt\s+{re.escape(cell_name)}\b.*$",
        subckt_hdr,
        txt,
        count=1,
        flags=re.M | re.I,
    )
    if n_hdr != 1:
        raise ValueError(f"Magic PEX output missing `.subckt {cell_name}` header")

    dc_nodes = set(pins)
    cap_nodes: set[str] = set()
    n_res = 0
    n_cap = 0
    n_float_bleed = 0
    lines: list[str] = []
    for raw in txt.splitlines():
        s = raw.strip()
        if re.match(r"^C\S+\s+\S+\s+\S+\s+0(?:\.0+)?\s*$", s, flags=re.I):
            continue
        clean = re.sub(r"\s*\$\s*\*\*FLOATING.*$", "", raw)
        parts = clean.split()
        if parts:
            head = parts[0].upper()
            if head.startswith(("X", "R")) and len(parts) >= 3:
                dc_nodes.update(parts[1:3])
                n_res += 1
            elif head.startswith("C") and len(parts) >= 4:
                cap_nodes.update(parts[1:3])
                n_cap += 1
        if s.lower().startswith(".ends"):
            # Floating dummy-poly endcaps have only parasitic caps; add 1 T-Ohm bleed for clean DC op.
            for idx, node in enumerate(sorted(cap_nodes - dc_nodes)):
                lines.append(f"R_FLT{idx} {node} VSS 1T")
                n_float_bleed += 1
        lines.append(clean)

    return "\n".join(lines) + "\n", n_res, n_cap, n_float_bleed


def run_magic_pex(
    gds_path: Path,
    cell_name: str,
    pins: list[str],
    out_spice: Path,
    run_dir: Path,
    cthresh_ff: float = 0.001,
    pdk_root: Path | None = None,
    tech=gf180,
) -> PexResult:
    """Extract polygon-level parasitic C + coupling PEX netlist from `gds_path` via native Magic."""
    gds_abs = gds_path.resolve()
    out_abs = out_spice.resolve()
    run_abs = run_dir.resolve()
    run_abs.mkdir(parents=True, exist_ok=True)
    out_abs.parent.mkdir(parents=True, exist_ok=True)

    root = Path(pdk_root) if pdk_root is not None else tech.PDK_ROOT
    magicrc = root / "gf180mcuD/libs.tech/magic/gf180mcuD.magicrc"
    if not magicrc.is_file():
        raise FileNotFoundError(f"Missing GF180MCU Magic rcfile: {magicrc}")

    magic_bin = shutil.which("magic")
    if magic_bin is None:
        raise FileNotFoundError("magic binary not found on PATH")

    raw_path = run_abs / f"{cell_name}.raw.pex.spice"
    tcl_path = run_abs / "extract_pex.tcl"
    tcl_path.write_text(
        "\n".join(
            [
                "drc off",
                "crashbackups disable",
                "locking disable",
                f"gds read {gds_abs}",
                f"load {cell_name}",
                "select top cell",
                "extract style ngspice()",
                "extract do local",
                "extract do capacitance",
                "extract do coupling",
                "extract do adjust",
                "extract do unique",
                "extract all",
                "ext2spice lvs",
                f"ext2spice cthresh {cthresh_ff}",
                f"ext2spice -f ngspice -o {raw_path} {cell_name}.ext",
                "quit -noprompt",
                "",
            ]
        )
    )

    cmd = [magic_bin, "-dnull", "-noconsole", "-rcfile", str(magicrc), str(tcl_path)]
    env = {**os.environ, "PDK_ROOT": str(root), "PDK": "gf180mcuD"}
    res = subprocess.run(cmd, cwd=run_abs, env=env, capture_output=True, text=True)
    if res.returncode != 0 or not raw_path.is_file():
        raise RuntimeError(f"Magic PEX failed on {gds_abs}:\n{res.stdout}\n{res.stderr}")

    norm_spice, n_res, n_cap, n_float_bleed = normalize_magic_pex_spice(
        raw_path.read_text(), cell_name, pins
    )
    out_abs.write_text(norm_spice)
    return PexResult(
        spice_path=out_abs,
        cell_name=cell_name,
        n_res=n_res,
        n_cap=n_cap,
        n_float_bleed=n_float_bleed,
    )


def has_libngspice() -> bool:
    """Return True if the `libngspice` shared library can be loaded in-process."""
    if ctypes.util.find_library("ngspice"):
        return True
    for candidate in ("libngspice.so.0", "libngspice.so"):
        try:
            ctypes.CDLL(candidate)
            return True
        except OSError:
            continue
    return False


def ngspice_pdk_lines(
    corner: str,
    rcorner: str,
    includes: tuple[Path, ...] | list[Path] = (),
    extra_libs: tuple[str, ...] | list[str] = (),
    pdk_root: Path | None = None,
    temp: float | None = None,
    tech=gf180,
) -> list[str]:
    """Return PDK corner + subcircuit `.include`/`.lib` directives for ngspice."""
    ng_design, ng_lib, sc_spice = tech.ngspice_pdk_files(pdk_root)
    lines: list[str] = []
    if temp is not None:
        lines.append(f".temp {temp}")
    lines += [
        f".include {ng_design}",
        f".lib {ng_lib} {corner}",
        f".lib {ng_lib} {rcorner}",
        *[f".lib {ng_lib} {lib}" for lib in extra_libs],
        f".include {sc_spice}",
        *[f".include {p.resolve()}" for p in includes],
    ]
    return lines


def ngspice_env(
    corner: str,
    rcorner: str,
    includes: list[Path],
    extra_libs: tuple[str, ...] | list[str] = (),
    vdd: float | None = None,
    temp: float | None = None,
    pdk_root: Path | None = None,
    tech=gf180,
) -> str:
    """Return PDK corner + subcircuit `.include` header for static `.spice` testbenches."""
    lines = []
    if vdd is not None:
        lines.append(f".param vdd = {vdd}")
    lines += ngspice_pdk_lines(
        corner,
        rcorner,
        includes=includes,
        extra_libs=extra_libs,
        pdk_root=pdk_root,
        temp=temp,
        tech=tech,
    )
    return "\n".join(lines) + "\n"


def _unescape_spef_name(name: str) -> str:
    return re.sub(r"\\(.)", r"\1", name.lstrip("\\"))


def parse_spef_net_rc(
    spef_path: Path,
    net_names: Iterable[str] | None = None,
    predicate=None,
) -> dict[str, tuple[float, float]]:
    """Extract total `(r_ohm, c_pf)` for matching nets from an IEEE 1481 SPEF file."""
    wanted = {_unescape_spef_name(n) for n in net_names} if net_names is not None else None
    if wanted is None and predicate is None:
        raise ValueError("Specify net_names or predicate")
    c_scale_pf = 1.0  # default 1 PF
    r_scale_ohm = 1.0  # default 1 OHM
    c_units = {"F": 1e12, "UF": 1e6, "NF": 1e3, "PF": 1.0, "FF": 1e-3}
    r_units = {"OHM": 1.0, "KOHM": 1e3, "MOHM": 1e6}
    name_map: dict[str, str] = {}
    results: dict[str, tuple[float, float]] = {}

    section: str | None = None
    cur_net: str | None = None
    cur_cap_pf = 0.0
    cur_res_ohm = 0.0

    with spef_path.open(encoding="utf-8", errors="replace") as fh:
        for raw_line in fh:
            line = raw_line.strip()
            if not line or line.startswith("//"):
                continue
            if line.startswith("*C_UNIT"):
                parts = line.split()
                c_scale_pf = float(parts[1]) * c_units[parts[2].upper()]
                continue
            if line.startswith("*R_UNIT"):
                parts = line.split()
                r_scale_ohm = float(parts[1]) * r_units[parts[2].upper()]
                continue
            if line == "*NAME_MAP":
                section = "NAME_MAP"
                continue
            if line.startswith("*D_NET "):
                parts = line.split()
                raw_net = name_map.get(parts[1], parts[1])
                norm_net = _unescape_spef_name(raw_net)
                matched = (wanted is not None and norm_net in wanted) or (
                    predicate is not None and predicate(norm_net)
                )
                if matched:
                    cur_net = norm_net
                    cur_cap_pf = float(parts[2]) * c_scale_pf
                    cur_res_ohm = 0.0
                    section = "D_NET"
                else:
                    cur_net = None
                    section = None
                continue
            if line == "*END":
                if cur_net is not None:
                    results[cur_net] = (cur_res_ohm, cur_cap_pf)
                    cur_net = None
                section = None
                continue
            if section == "NAME_MAP":
                if line.startswith("*PORTS") or line.startswith("*D_NET"):
                    section = None
                else:
                    parts = line.split(None, 1)
                    if len(parts) == 2 and parts[0].startswith("*"):
                        name_map[parts[0]] = parts[1].strip()
                continue
            if cur_net is not None:
                if line in ("*CONN", "*CAP", "*RES"):
                    section = line
                elif section == "*RES":
                    parts = line.split()
                    if len(parts) >= 4:
                        cur_res_ohm += float(parts[3]) * r_scale_ohm

    return results


def measure_linearity(samples: list[float]) -> LinearityResult:
    """Measure monotonicity, LSB step, max |DNL|, and max |INL| on a converter transfer curve."""
    n = len(samples)
    if n < 2:
        raise ValueError(f"Expected >= 2 samples for linearity measurement, got {n}")
    lsb = (samples[-1] - samples[0]) / (n - 1)
    monotonic = all(samples[k + 1] > samples[k] for k in range(n - 1))
    if lsb == 0.0:
        return LinearityResult(
            lsb=0.0, monotonic=monotonic, max_dnl=float("inf"), max_inl=float("inf")
        )
    dnls = [(samples[k + 1] - samples[k]) / lsb - 1.0 for k in range(n - 1)]
    inls = [(samples[k] - (samples[0] + k * lsb)) / lsb for k in range(n)]
    return LinearityResult(
        lsb=lsb,
        monotonic=monotonic,
        max_dnl=max(map(abs, dnls)),
        max_inl=max(map(abs, inls)),
    )


def run_ngspice(
    tb_spice: Path,
    spice_path: Path,
    corner: str,
    rcorner: str,
    vdd: float = 5.0,
    temp: float = 27.0,
    tech=gf180,
) -> tuple[list[float], ...]:
    """Run `tb_spice` against `spice_path` in a temp dir and return numeric columns from `out.dat`."""
    with tempfile.TemporaryDirectory() as tmp:
        sim_dir = Path(tmp)
        subckt = sim_dir / f"{spice_path.stem}_ngspice.cir"
        subckt.write_text(spice_to_ngspice(spice_path, tech=tech))
        (sim_dir / "env.spice").write_text(
            ngspice_env(corner, rcorner, includes=[subckt], vdd=vdd, temp=temp, tech=tech)
        )
        res = subprocess.run(
            ["ngspice", "-b", str(tb_spice.resolve())],
            cwd=sim_dir,
            capture_output=True,
            text=True,
        )
        if res.returncode != 0:
            raise RuntimeError(f"ngspice failed on {tb_spice}:\n{res.stdout}\n{res.stderr}")
        rows = [
            [float(x) for x in line.split()]
            for line in (sim_dir / "out.dat").read_text().splitlines()
            if line.split()
        ]
        return tuple(list(col) for col in zip(*rows))


def render_gds(
    gds_path: Path,
    png_path: Path,
    width: int = 1600,
    height: int = 1200,
    margin_um: float = 2.0,
    lyp_path: Path = gf180.LYP_PATH,
) -> Path:
    """Render a GDSII layout to PNG using KLayout LayoutView and PDK layer properties."""
    import klayout.lay as lay

    ly = kdb.Layout()
    ly.read(str(gds_path))
    top = max(ly.top_cells(), key=lambda c: c.dbbox().area())

    lv = lay.LayoutView()
    lv.load_layout(str(gds_path), False)
    lv.active_cellview().cell_name = top.name
    if lyp_path.exists():
        lv.load_layer_props(str(lyp_path))
    lv.max_hier()
    bb = top.dbbox()
    lv.zoom_box(
        kdb.DBox(
            bb.left - margin_um,
            bb.bottom - margin_um,
            bb.right + margin_um,
            bb.top + margin_um,
        )
    )
    png_path.parent.mkdir(parents=True, exist_ok=True)
    lv.save_image(str(png_path), width, height)
    return png_path
