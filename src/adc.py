#!/usr/bin/env python3
"""4-bit Ramp ADC passive analog macro: builds & verifies `r2r_ladder_4bit_sum` via `dac.py`.

External `clkinv_4` drivers and `clkbuf_1` comparator live in `src/sc_adc_ramp_1ch_4b.v`
(instantiated directly from `src/adc_ramp.x` via XLS `#[extern_verilog]` and modeled at
transistor level in `src/adc_ramp.spice`).
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import os
from pathlib import Path

import kfactory as kf

import analog
import dac
import tools

SRC_DIR = Path(__file__).resolve().parent
REPO_ROOT = SRC_DIR.parent
CHANNELS = 4
BITS = 4
CELL_NAME = "r2r_ladder_4bit_sum"
W_RES = 1.00  # um; ppolyf_u_1k_6p0 finger width
L_1R = 6.00  # um; 1R unit finger (~6.3 kOhm)
L_2R = 5.42  # um; 2 * L_2R + clkinv_4 R_on ~= 2 * L_1R
CANONICAL_PINS = ["VSS", "term", *[f"in[{b}]" for b in range(BITS - 1, -1, -1)], "out", "sum_in"]

# (corner, rcorner, vdd_V, temp_C, max_dnl_lsb, max_inl_lsb)
PVT_CORNERS: tuple[tuple[str, str, float, float, float, float], ...] = (
    ("typical", "res_typical", 5.0, 27.0, 0.25, 0.50),
    ("ff", "res_ff", 5.0, 27.0, 0.25, 0.50),
    ("ss", "res_ss", 5.0, 27.0, 0.25, 0.50),
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
    def ok(self) -> bool:
        return (
            len(self.trips) == self.expected_steps
            and self.linearity.monotonic
            and self.linearity.max_dnl < self.max_dnl_limit
            and self.linearity.max_inl < self.max_inl_limit
        )


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
    args = ap.parse_args()

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
