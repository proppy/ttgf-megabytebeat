#!/usr/bin/env python3
"""4-bit Ramp ADC passive analog macro: builds & verifies `r2r_ladder_4bit_sum` via `dac.py`.

External `clkinv_12` drivers and `aoi21_1` + `inv_1` comparator live in `src/sc_adc_ramp_1ch_4b.v`
(instantiated directly from `src/adc_ramp.x` via XLS `#[extern_verilog]` and modeled at
transistor level in `src/adc_ramp.spice` and `cocotbext-ams` `AdcAmsBridge`).
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import os
from pathlib import Path
import re
import tempfile

try:
    import kfactory as kf
except ImportError:  # pragma: no cover - layout build only needed for GDS generation
    kf = None  # type: ignore[assignment]

import analog
import dac
import gf180
import tools

SRC_DIR = Path(__file__).resolve().parent
REPO_ROOT = SRC_DIR.parent
CHANNELS = 4
BITS = 4
CELL_NAME = "r2r_ladder_4bit_sum"
AMS_SUBCKT = "sc_adc_ramp_1ch_4b_ams"
DRIVER_CELL = "gf180mcu_fd_sc_mcu7t5v0__clkinv_12"
TIEL_CELL = "gf180mcu_fd_sc_mcu7t5v0__tiel"
CMP_CELL = "gf180mcu_fd_sc_mcu7t5v0__aoi21_1"
POST_INV_CELL = "gf180mcu_fd_sc_mcu7t5v0__inv_1"
TT_MUX_R_OHM = 0  # Keep 1R summing ratio exact (matching adc_ramp.spice); SPEF in_rc adds route R
TT_PAD_C_PF = 10
AMS_INIT_COMMANDS: tuple[str, ...] = ("set ngbehavior=hsa", "set ng_nomodcheck")
W_RES = 1.00  # um; ppolyf_u_1k_6p0 finger width
L_1R = 6.00  # um; 1R unit finger (~6.3 kOhm)
L_2R = 5.82  # um; 2 * L_2R + clkinv_12 R_on ~= 2 * L_1R across 3.0-5.5 V PVT
CANONICAL_PINS = ["VSS", "term", *[f"in[{b}]" for b in range(BITS - 1, -1, -1)], "out", "sum_in"]

_DC_SIM = None  # retain libngspice ctypes callback closures until next ngSpice_Init

# (corner, rcorner, vdd_V, temp_C, max_dnl_lsb, max_inl_lsb)
PVT_CORNERS: tuple[tuple[str, str, float, float, float, float], ...] = (
    # TTGF26c 3.3 V +/- 10% PVT (correlated, skewed, and uncorrelated MOS/res cross-corners)
    ("typical", "res_typical", 3.3, 27.0, 0.25, 0.50),
    ("ss", "res_ss", 3.0, 125.0, 0.25, 0.50),
    ("ff", "res_ff", 3.6, -40.0, 0.25, 0.50),
    ("sf", "res_typical", 3.3, 27.0, 0.25, 0.50),
    ("fs", "res_typical", 3.3, 27.0, 0.25, 0.50),
    ("ss", "res_ff", 3.0, 125.0, 0.50, 0.50),
    ("ff", "res_ss", 3.6, -40.0, 0.25, 0.50),
    # wafer.space 5.0 V +/- 10% PVT
    ("typical", "res_typical", 5.0, 27.0, 0.25, 0.50),
    ("ss", "res_ss", 4.5, 125.0, 0.25, 0.50),
    ("ff", "res_ff", 5.5, -40.0, 0.25, 0.50),
)


@dataclass(frozen=True)
class AdcCornerResult:
    """Structured result of one 16-step ngspice ramp-ADC comparator sweep."""

    corner: str
    rcorner: str
    vdd: float
    temp: float
    trips: tuple[float, ...]
    linearity: analog.LinearityResult
    max_dnl_limit: float
    max_inl_limit: float

    @property
    def expected_steps(self) -> int:
        return (1 << BITS) - 1

    @property
    def trips_in_range(self) -> bool:
        if self.corner not in ("typical", "ss", "ff"):
            return True
        return bool(self.trips) and 0.0 < self.trips[0] < self.trips[-1] < self.vdd

    @property
    def ok(self) -> bool:
        return (
            len(self.trips) == self.expected_steps
            and self.trips_in_range
            and self.linearity.monotonic
            and self.linearity.max_dnl < self.max_dnl_limit
            and self.linearity.max_inl < self.max_inl_limit
        )


@dataclass(frozen=True)
class AdcChannelParasitics:
    """Post-PnR SPEF routing R/C for one ADC channel: `ua[ch]` sum_in, `vsum`, and `clkinv_12` nets."""

    in_rc: tuple[float, float] = (0.0, 0.0)
    vsum_rc: tuple[float, float] = (0.0, 0.0)
    term_rc: tuple[float, float] = (0.0, 0.0)
    bit_rc: tuple[tuple[float, float], ...] = ()
    in_net: str | None = None

    @property
    def drv_res_ohms(self) -> list[float]:
        return [self.term_rc[0], *(r for r, _ in self.bit_rc)]


@dataclass(frozen=True)
class AdcSpefCheckResult:
    """Structured result of post-PnR ADC SPEF parasitics verification."""

    spef_path: Path
    channels: dict[int, AdcChannelParasitics]
    violations: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.violations


def build_adc(
    l_1r: float = L_1R, l_2r: float = L_2R, w: float = W_RES
) -> tuple[kf.DSchematic, kf.DKCell, list[str]]:
    """Build passive 4-bit summing R-2R ladder for the ramp ADC."""
    return dac.build_r2r(bits=BITS, l_1r=l_1r, l_2r=l_2r, w=w, sum_res=True, name=CELL_NAME)


def find_spice(explicit: Path | None = None, prefer_pex: bool = True) -> Path | None:
    """Locate the generated passive `r2r_ladder_4bit_sum` SPICE netlist (preferring Magic PEX)."""
    candidates: list[Path] = []
    if explicit is not None:
        candidates.append(Path(explicit))
    env_path = os.environ.get("ADC_R2R_SPICE")
    if env_path:
        candidates.append(Path(env_path))
    suffixes = (".pex.spice", ".spice") if prefer_pex else (".spice", ".pex.spice")
    for base in (REPO_ROOT / "gen/adc", REPO_ROOT / "gen_tt/adc"):
        for suf in suffixes:
            candidates.append(base / f"{CELL_NAME}{suf}")
    for path in candidates:
        if path.is_file():
            return path
    return None


def check_adc_parasitics(
    spef_path: Path,
    channels: int = CHANNELS,
    bits: int = BITS,
    max_in_r_ohm: float = 500.0,
    max_in_c_pf: float = 5.0,
    max_vsum_r_ohm: float = 200.0,
    max_vsum_c_pf: float = 2.0,
    max_drv_r_ohm: float = 200.0,
    max_drv_c_pf: float = 5.0,
    in_nets: tuple[str, ...] | None = None,
) -> AdcSpefCheckResult:
    """Verify post-PnR `ua[0..3]`/`adc_in`, `vsum`, and `clkinv_12` `in_drv`/`term_drv` SPEF R/C."""
    violations: list[str] = []
    if in_nets is not None:
        if len(in_nets) != channels:
            return AdcSpefCheckResult(
                spef_path=spef_path,
                channels={},
                violations=(f"in_nets length {len(in_nets)} != channels {channels}",),
            )
        candidate_in_nets = set(in_nets)
        prefixes_to_check: tuple[str, ...] = ()
    else:
        prefixes_to_check = ("ua", "adc_in")
        candidate_in_nets = {f"{pfx}[{ch}]" for pfx in prefixes_to_check for ch in range(channels)}
    net_re = re.compile(r"^(.+)\.(in_drv\[(\d+)\]|term_drv|vsum)$")

    def _want(net: str) -> bool:
        return net in candidate_in_nets or net_re.match(net) is not None

    parsed = analog.parse_spef_net_rc(spef_path, predicate=_want)

    if in_nets is not None:
        resolved_in_nets = list(in_nets)
    else:
        resolved_prefix = next(
            (
                pfx
                for pfx in prefixes_to_check
                if all(f"{pfx}[{ch}]" in parsed for ch in range(channels))
            ),
            prefixes_to_check[0],
        )
        resolved_in_nets = [f"{resolved_prefix}[{ch}]" for ch in range(channels)]
    missing_in = [name for name in resolved_in_nets if name not in parsed]
    if missing_in:
        violations.append(f"{spef_path}: missing analog ADC input net(s) in SPEF: {missing_in}")
        return AdcSpefCheckResult(spef_path=spef_path, channels={}, violations=tuple(violations))

    by_prefix: dict[str, dict[str, tuple[float, float]]] = {}
    for net, rc in parsed.items():
        m = net_re.match(net)
        if m:
            prefix, pin_name = m.group(1), m.group(2)
            by_prefix.setdefault(prefix, {})[pin_name] = rc

    adc_prefixes = sorted(
        (
            prefix
            for prefix, pins_map in by_prefix.items()
            if sum(1 for k in pins_map if k.startswith("in_drv[")) == bits
        ),
        key=dac._natural_key,
    )
    if len(adc_prefixes) != channels:
        violations.append(
            f"{spef_path}: expected {channels} {bits}-bit ADC driver instances in SPEF, "
            f"found {len(adc_prefixes)}: {adc_prefixes}"
        )
        return AdcSpefCheckResult(spef_path=spef_path, channels={}, violations=tuple(violations))

    by_ch: dict[int, AdcChannelParasitics] = {}
    for ch, (prefix, in_name) in enumerate(zip(adc_prefixes, resolved_in_nets, strict=True)):
        in_r, in_c = parsed[in_name]
        if in_r >= max_in_r_ohm:
            violations.append(
                f"{spef_path}: {in_name} routing R={in_r:.2f} Ohm >= {max_in_r_ohm:.1f} Ohm"
            )
        if in_c >= max_in_c_pf:
            violations.append(
                f"{spef_path}: {in_name} routing C={in_c:.4f} pF >= {max_in_c_pf:.2f} pF"
            )
        pins_map = by_prefix[prefix]
        if "term_drv" not in pins_map:
            violations.append(f"{spef_path}: missing {prefix}.term_drv in SPEF")
            continue
        if "vsum" not in pins_map:
            violations.append(f"{spef_path}: missing {prefix}.vsum in SPEF")
            continue
        term_rc = pins_map["term_drv"]
        vsum_rc = pins_map["vsum"]
        if vsum_rc[0] >= max_vsum_r_ohm:
            violations.append(
                f"{spef_path}: adc ch{ch} vsum R={vsum_rc[0]:.2f} Ohm >= {max_vsum_r_ohm:.1f} Ohm"
            )
        if vsum_rc[1] >= max_vsum_c_pf:
            violations.append(
                f"{spef_path}: adc ch{ch} vsum C={vsum_rc[1]:.4f} pF >= {max_vsum_c_pf:.2f} pF"
            )
        bit_rc_list: list[tuple[float, float]] = []
        missing_bits = False
        for b in range(bits):
            key = f"in_drv[{b}]"
            if key not in pins_map:
                violations.append(f"{spef_path}: missing {prefix}.{key} in SPEF")
                missing_bits = True
            else:
                bit_rc_list.append(pins_map[key])
        if missing_bits:
            continue
        for pin_name, (dr, dc) in [("term_drv", term_rc)] + [
            (f"in_drv[{b}]", bit_rc_list[b]) for b in range(bits)
        ]:
            if dr >= max_drv_r_ohm:
                violations.append(
                    f"{spef_path}: adc ch{ch} {pin_name} R={dr:.2f} Ohm >= {max_drv_r_ohm:.1f} Ohm"
                )
            if dc >= max_drv_c_pf:
                violations.append(
                    f"{spef_path}: adc ch{ch} {pin_name} C={dc:.4f} pF >= {max_drv_c_pf:.2f} pF"
                )
        by_ch[ch] = AdcChannelParasitics(
            in_rc=(in_r, in_c),
            vsum_rc=vsum_rc,
            term_rc=term_rc,
            bit_rc=tuple(bit_rc_list),
            in_net=in_name,
        )
    return AdcSpefCheckResult(spef_path=spef_path, channels=by_ch, violations=tuple(violations))


def format_parasitics_summary(parasitics: dict[int, AdcChannelParasitics]) -> str:
    """Format a compact one-line summary of per-channel ADC SPEF parasitics."""
    parts: list[str] = []
    for ch, p in sorted(parasitics.items()):
        label = p.in_net or f"ua[{ch}]"
        in_r, in_c = p.in_rc
        vs_r, vs_c = p.vsum_rc
        if p.bit_rc:
            rs = p.drv_res_ohms
            parts.append(
                f"{label}={in_r:.1f}Ohm/{in_c:.3f}pF "
                f"(vsum={vs_r:.1f}Ohm/{vs_c:.3f}pF, drv={min(rs):.1f}..{max(rs):.1f}Ohm)"
            )
        else:
            parts.append(f"{label}={in_r:.1f}Ohm/{in_c:.3f}pF")
    return ", ".join(parts)


def _subckt_lines(
    bits: int,
    subckt: str,
    cell_name: str,
    in_rc: tuple[float, float] = (0.0, 0.0),
    vsum_rc: tuple[float, float] = (0.0, 0.0),
    term_rc: tuple[float, float] = (0.0, 0.0),
    bit_rc: tuple[tuple[float, float], ...] = (),
) -> list[str]:
    """Emit AMS `.subckt`: `term in[0..3]` (already inverted `u_r2r` pins) -> `clkinv_12` -> R-2R -> `aoi21_1`+`inv_1`."""
    in_pins = " ".join(f"in{b}" for b in range(bits))
    lines = [
        f".subckt {subckt} term {in_pins} cmp adc_in vsum vdd vss",
        # Linear inversion restores polarity before real BSIM3v3 clkinv_12 drivers (u_r2r.in = ~step).
        "E_PRE_TERM term_inv_s vdd term vss -1.0",
        "R_PRE_TERM term_inv_s term_inv 100",
        f"X_TERM term_inv drv_term vdd vdd vss vss {DRIVER_CELL}",
        *[f"E_PRE_B{b} in_inv_s{b} vdd in{b} vss -1.0" for b in range(bits)],
        *[f"R_PRE_B{b} in_inv_s{b} in_inv{b} 100" for b in range(bits)],
        *[f"X_B{b} in_inv{b} drv{b} vdd vdd vss vss {DRIVER_CELL}" for b in range(bits)],
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

    # Pad capacitance + optional analog mux / post-PnR top-level route on sum_in.
    in_r, in_c = in_rc
    sum_in_node = "adc_in"
    if TT_MUX_R_OHM > 0:
        lines.append(f"R_MUX adc_in ua_pad {TT_MUX_R_OHM}")
        sum_in_node = "ua_pad"
    if TT_PAD_C_PF > 0:
        lines.append(f"C_PAD {sum_in_node} vss {TT_PAD_C_PF}p")
    if in_r > 0.0:
        lines.append(f"R_IN_ROUTE {sum_in_node} sum_in_int {in_r:.4f}")
        sum_in_node = "sum_in_int"
    if in_c > 0.0:
        lines.append(f"C_IN_ROUTE {sum_in_node} vss {in_c:.6f}p")

    r2r_pins_msb = " ".join(r2r_in_nodes[b] for b in range(bits - 1, -1, -1))
    lines.append(f"XR2R vss {term_node} {r2r_pins_msb} vsum {sum_in_node} {cell_name}")

    vsum_r, vsum_c = vsum_rc
    cmp_in_node = "vsum"
    if vsum_r > 0.0:
        lines.append(f"R_VSUM vsum vsum_cmp {vsum_r:.4f}")
        cmp_in_node = "vsum_cmp"
    if vsum_c > 0.0:
        lines.append(f"C_VSUM {cmp_in_node} vss {vsum_c:.6f}p")

    lines += [
        f"X_TIEL cmp_lo vdd vdd vss vss {TIEL_CELL}",
        f"X_CMP {cmp_in_node} {cmp_in_node} cmp_lo cmp_raw vdd vdd vss vss {CMP_CELL}",
        f"X_POST cmp_raw cmp vdd vdd vss vss {POST_INV_CELL}",
        f".ends {subckt}",
    ]
    return lines


def export_ams_spice(
    r2r_ng_spice: Path,
    bits: int = BITS,
    subckt: str = AMS_SUBCKT,
    cell_name: str = CELL_NAME,
    route_rc: dict[int, AdcChannelParasitics] | None = None,
) -> str:
    """Return ngspice `.subckt` wrapper: `clkinv_12` drivers + `r2r_ladder_4bit_sum` + `aoi21_1`+`inv_1`."""
    lines = [
        f"* cocotbext-ams ADC wrapper: 5x clkinv_12 + {cell_name} + aoi21_1 + inv_1 + route/mux RC",
        f".include {r2r_ng_spice.resolve()}",
        "",
        *_subckt_lines(bits=bits, subckt=subckt, cell_name=cell_name),
    ]
    if route_rc:
        for ch, p in sorted(route_rc.items()):
            lines.append("")
            lines.extend(
                _subckt_lines(
                    bits=bits,
                    subckt=f"{subckt}_ch{ch}",
                    cell_name=cell_name,
                    in_rc=p.in_rc,
                    vsum_rc=p.vsum_rc,
                    term_rc=p.term_rc,
                    bit_rc=p.bit_rc,
                )
            )
    lines.append("")
    return "\n".join(lines)


def prepare_ams_spice(
    out_dir: Path,
    r2r_spice: Path,
    route_rc: dict[int, AdcChannelParasitics] | None = None,
    tech=gf180,
) -> Path:
    """Emit ngspice-ready `r2r_ladder_4bit_sum` + `sc_adc_ramp_1ch_4b_ams` wrapper in `out_dir`."""
    out_dir.mkdir(parents=True, exist_ok=True)
    r2r_ng = out_dir / f"{CELL_NAME}_ngspice.cir"
    r2r_ng.write_text(analog.spice_to_ngspice(r2r_spice, tech=tech))
    wrapper = out_dir / f"{AMS_SUBCKT}.spice"
    wrapper.write_text(export_ams_spice(r2r_ng, route_rc=route_rc))
    return wrapper


def trip_sweep_netlist(
    wrapper_spice: Path,
    vdd: float = 3.3,
    corner: str = "typical",
    rcorner: str = "res_typical",
    temp: float | None = None,
    subckt: str = AMS_SUBCKT,
    pdk_root: Path | None = None,
    tech=gf180,
) -> list[str]:
    """Return ngspice deck lines for a 15-step DC comparator trip-point sweep via `libngspice0`."""
    n_steps = (1 << BITS) - 1
    inst_lines: list[str] = []
    for step in range(n_steps):
        inv_code = ((1 << BITS) - 1) ^ step
        bits_str = " ".join("vhi" if (inv_code >> b) & 1 else "vlo" for b in range(BITS))
        inst_lines.append(
            f"XADC_{step} vlo {bits_str} cmp_{step} adc_in vsum_{step} vdd vss {subckt}"
        )
    save_line = ".save v(adc_in) " + " ".join(f"v(cmp_{k})" for k in range(n_steps))
    return [
        "* 15-step ADC comparator DC trip-point sweep via libngspice0",
        *analog.ngspice_pdk_lines(
            corner, rcorner, includes=[wrapper_spice], pdk_root=pdk_root, temp=temp, tech=tech
        ),
        f"VDD vdd 0 {vdd}",
        "VSS vss 0 0.0",
        f"VHI vhi 0 {vdd}",
        "VLO vlo 0 0.0",
        "VADC adc_in 0 0.0",
        *inst_lines,
        save_line,
        ".options klu reltol=1e-3 vntol=1e-4 abstol=1e-9",
        ".end",
    ]


def measure_trip_points(
    r2r_spice: Path,
    vdd: float = 3.3,
    corner: str = "typical",
    rcorner: str = "res_typical",
    temp: float | None = None,
    route_rc: dict[int, AdcChannelParasitics] | None = None,
    channel: int | None = None,
    pdk_root: Path | None = None,
    tech=gf180,
) -> list[float]:
    """Measure the 15 BSIM3v3 + R-2R (+ SPEF RC) comparator trip voltages in-process via `libngspice0`."""
    global _DC_SIM
    from cocotbext.ams._ngspice import NgspiceInterface

    n_steps = (1 << BITS) - 1
    subckt = (
        f"{AMS_SUBCKT}_ch{channel}"
        if (channel is not None and route_rc and channel in route_rc)
        else AMS_SUBCKT
    )
    with tempfile.TemporaryDirectory(prefix="adc_trip_") as tmp:
        wrapper = prepare_ams_spice(Path(tmp), r2r_spice, route_rc=route_rc, tech=tech)
        lines = trip_sweep_netlist(
            wrapper,
            vdd=vdd,
            corner=corner,
            rcorner=rcorner,
            temp=temp,
            subckt=subckt,
            pdk_root=pdk_root,
            tech=tech,
        )
        sim = NgspiceInterface()
        _DC_SIM = sim  # retain callback closures until next ngSpice_Init
        for cmd in AMS_INIT_COMMANDS:
            sim.command(cmd)
        sim.load_circuit(lines)
        sim.command(f"dc vadc 0.0 {vdd:.3f} 0.015")

        vin_vec = sim._lib.ngGet_Vec_Info(b"adc_in")
        if not vin_vec:
            raise RuntimeError("ngspice ADC trip sweep did not produce adc_in vector")
        vin_vi = vin_vec.contents
        n_pts = vin_vi.v_length
        vin = [float(vin_vi.v_realdata[i]) for i in range(n_pts)]
        vmid = 0.5 * vdd

        trips: list[float] = []
        for k in range(n_steps):
            cvec = sim._lib.ngGet_Vec_Info(f"cmp_{k}".encode())
            if not cvec:
                raise RuntimeError(f"ngspice ADC trip sweep did not produce cmp_{k} vector")
            cvi = cvec.contents
            if cvi.v_length != n_pts:
                raise RuntimeError(
                    f"ngspice ADC trip sweep vector length mismatch: cmp_{k}={cvi.v_length} != {n_pts}"
                )
            vcmp = [float(cvi.v_realdata[i]) for i in range(n_pts)]
            vtrip: float | None = None
            for i in range(n_pts - 1):
                if vcmp[i] <= vmid <= vcmp[i + 1] and vcmp[i + 1] > vcmp[i]:
                    frac = (vmid - vcmp[i]) / (vcmp[i + 1] - vcmp[i])
                    vtrip = vin[i] + frac * (vin[i + 1] - vin[i])
                    break
            if vtrip is None:
                raise RuntimeError(
                    f"ADC step {k} comparator never crossed {vmid:.2f} V "
                    f"(range {min(vcmp):.3f}..{max(vcmp):.3f} V)"
                )
            trips.append(vtrip)
        return trips


def ams_block(
    name: str,
    wrapper_spice: Path,
    handle,
    initial_vin: float = 0.0,
    vdd: float = 3.3,
    corner: str = "typical",
    rcorner: str = "res_typical",
    temp: float | None = None,
    edge_time_ns: float = 5.0,
    tran_step: str = "2u",
    subckt: str = AMS_SUBCKT,
    pdk_root: Path | None = None,
    tech=gf180,
):
    """Build a `cocotbext-ams` `AnalogBlock` binding a 4-bit `u_r2r` handle to `sc_adc_ramp_1ch_4b_ams`."""
    from cocotbext.ams import AnalogBlock, DigitalPin

    return AnalogBlock(
        name=name,
        spice_file=wrapper_spice,
        subcircuit=subckt,
        digital_pins={
            "term": DigitalPin(direction="input", width=1, vdd=vdd, vss=0.0),
            "in": DigitalPin(direction="input", width=BITS, vdd=vdd, vss=0.0),
            "cmp": DigitalPin(direction="output", width=1, vdd=vdd, vss=0.0),
        },
        analog_inputs={"adc_in": initial_vin},
        analog_outputs=["vsum"],
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


def simulate_adc_corners(
    spice: Path,
    corners: tuple[tuple[str, str, float, float, float, float], ...] = PVT_CORNERS,
) -> list[AdcCornerResult]:
    """Run parallel multi-corner 16-step `ngspice` comparator sweeps and return results."""

    def _run_corner(spec: tuple[str, str, float, float, float, float]) -> AdcCornerResult:
        corner, rcorner, vdd, temp, max_dnl, max_inl = spec
        (trips,) = analog.run_ngspice(
            SRC_DIR / "adc_ramp.spice", spice, corner, rcorner, vdd=vdd, temp=temp
        )
        return AdcCornerResult(
            corner=corner,
            rcorner=rcorner,
            vdd=vdd,
            temp=temp,
            trips=tuple(trips),
            linearity=analog.measure_linearity(trips),
            max_dnl_limit=max_dnl,
            max_inl_limit=max_inl,
        )

    with ThreadPoolExecutor(max_workers=min(len(corners), os.cpu_count() or 4)) as ex:
        return list(ex.map(_run_corner, corners))


def extract_adc_pex(
    gds: Path,
    out_dir: Path,
    pins: list[str] | None = None,
) -> analog.PexResult:
    """Extract polygon-level Magic PEX (`r2r_ladder_4bit_sum.pex.spice`) from `gds`."""
    pex_spice = out_dir / f"{CELL_NAME}.pex.spice"
    return analog.run_magic_pex(gds, CELL_NAME, pins or CANONICAL_PINS, pex_spice, out_dir / "pex")


def _report_adc_corners(results: list[AdcCornerResult], label_prefix: str) -> None:
    for r in results:
        label = f"{label_prefix} {r.corner}/{r.rcorner} {r.vdd:.1f}V {r.temp:.0f}C"
        assert len(r.trips) == r.expected_steps, (
            f"{label}: measured {len(r.trips)}/{r.expected_steps} trip points!"
        )
        if r.corner in ("typical", "ss", "ff"):
            assert 0.0 < r.trips[0] < r.trips[-1] < r.vdd, (
                f"{label}: trip points [{r.trips[0]:.3f}..{r.trips[-1]:.3f}]V "
                f"outside (0, {r.vdd:.1f}V)!"
            )
        assert r.linearity.monotonic, f"{label}: non-monotonic!"
        assert r.linearity.max_dnl < r.max_dnl_limit and r.linearity.max_inl < r.max_inl_limit, (
            f"{label}: linearity failed "
            f"(max|DNL|={r.linearity.max_dnl:.4f} >= {r.max_dnl_limit}, "
            f"max|INL|={r.linearity.max_inl:.4f} >= {r.max_inl_limit})"
        )
        print(
            f"  [{label_prefix} | {r.corner:7s}/{r.rcorner:11s} {r.vdd:.1f}V {r.temp:4.0f}C] "
            f"{len(r.trips) + 1}-step ngspice OK: "
            f"Vtrip={r.trips[0]:.3f}V..{r.trips[-1]:.3f}V, "
            f"max|DNL|={r.linearity.max_dnl:.4f} LSB, max|INL|={r.linearity.max_inl:.4f} LSB",
            flush=True,
        )


def main() -> None:
    ap = argparse.ArgumentParser(description="Build & verify 4-bit Ramp ADC passive macro.")
    ap.add_argument("--out-dir", type=Path, default=REPO_ROOT / "gen/adc")
    ap.add_argument(
        "--check-spef",
        type=Path,
        default=None,
        help="Verify post-PnR ua[0..3]/adc_in, vsum, and in_drv/term_drv routing parasitics in SPEF",
    )
    ap.add_argument(
        "--channels",
        type=int,
        default=CHANNELS,
        help=f"Number of ADC channels expected by --check-spef (default: {CHANNELS})",
    )
    args = ap.parse_args()

    if args.check_spef is not None:
        spef_files = (
            [args.check_spef]
            if args.check_spef.is_file()
            else sorted(args.check_spef.rglob("*.spef"))
        )
        assert spef_files, f"No SPEF files found under {args.check_spef}"
        for spef in spef_files:
            res = check_adc_parasitics(spef, channels=args.channels)
            assert res.ok, "; ".join(res.violations)
            print(f"  [ADC SPEF check | {spef.name}] OK: {format_parasitics_summary(res.channels)}")
        return

    tool_checks = tools.check_native_tools(("klayout", "magic"))
    bad_tools = [c for c in tool_checks if not c.ok]
    assert not bad_tools, (
        "Native tool version mismatch vs LibreLane "
        f"{tools.LIBRELANE_VERSION}: "
        + ", ".join(f"{c.name}={c.actual} (expected {c.expected})" for c in bad_tools)
    )

    s, adc, pins = build_adc()
    gds, _, _, spice = analog.emit_macro(s, adc, pins, args.out_dir)

    drc = analog.run_klayout_drc(gds, args.out_dir / "drc")
    assert drc.clean, f"KLayout DRC failed on {gds}:\n{drc.log}"
    print(f"  [Analog check | {adc.name}] KLayout DRC clean.")

    lvs = analog.run_klayout_lvs(gds, spice, args.out_dir / "lvs", adc.name, pins=pins)
    assert lvs.matched, f"KLayout LVS mismatch on {adc.name}:\n{lvs.log}"
    assert lvs.pins_match, (
        f"LVS extracted pin mismatch on {adc.name}: "
        f"extracted={sorted(lvs.ext_pins)} vs expected={sorted(lvs.expected_pins)}"
    )
    print(f"  [Analog check | {adc.name}] KLayout LVS (component-graph schematic vs GDS) matched.")

    _report_adc_corners(simulate_adc_corners(spice), label_prefix="ADC check")

    pex = extract_adc_pex(gds, args.out_dir, pins=pins)
    assert pex.ok, (
        f"Magic PEX extraction on {adc.name} produced n_res={pex.n_res}, n_cap={pex.n_cap}"
    )
    print(
        f"  [Magic PEX | {adc.name}] Extracted {pex.spice_path} "
        f"(R={pex.n_res}, C={pex.n_cap}, bleed={pex.n_float_bleed})"
    )
    _report_adc_corners(simulate_adc_corners(pex.spice_path), label_prefix="ADC PEX check")

    png = analog.render_gds(gds, args.out_dir / f"{adc.name}.png", width=1600, height=900)
    print(f"[adc] Rendered {png}")
    print(
        f"{adc.name}: {adc.info['width']:.2f} x {adc.info['height']:.2f} um "
        f"({adc.info['area']:.2f} um^2)"
    )


if __name__ == "__main__":
    main()
