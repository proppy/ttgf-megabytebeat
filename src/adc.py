#!/usr/bin/env python3
"""4-bit Ramp ADC passive analog macro: builds & verifies `r2r_ladder_4bit_sum` via `dac.py`.

External `clkinv_4` drivers and `clkbuf_1` comparator live in `src/sc_adc_ramp_1ch_4b.v`
(instantiated directly from `src/adc_ramp.x` via XLS `#[extern_verilog]` and modeled at
transistor level in `src/adc_ramp.spice`).
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import kfactory as kf

import analog
import dac

SRC_DIR = Path(__file__).resolve().parent
REPO_ROOT = SRC_DIR.parent
CHANNELS = 4
BITS = 4
CELL_NAME = "r2r_ladder_4bit_sum"
W_RES = 1.00  # um; ppolyf_u_1k_6p0 finger width
L_1R = 6.00  # um; 1R unit finger (~6.3 kOhm)
L_2R = 5.42  # um; 2 * L_2R + clkinv_4 R_on ~= 2 * L_1R
CANONICAL_PINS = ["VSS", "term", *[f"in[{b}]" for b in range(BITS - 1, -1, -1)], "out", "sum_in"]


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


def check_adc_ngspice(spice: Path, label_prefix: str = "ADC check") -> None:
    """Run multi-corner 16-step `ngspice` comparator trip-point linearity checks on `spice`."""
    for corner, rcorner in (("typical", "res_typical"), ("ff", "res_ff"), ("ss", "res_ss")):
        (trips,) = analog.run_ngspice(SRC_DIR / "adc_ramp.spice", spice, corner, rcorner)
        _, dnl, inl = analog.check_linearity(trips, f"{label_prefix} {corner}")
        print(
            f"  [{label_prefix} | {corner:7s}] {len(trips) + 1}-step ngspice OK: "
            f"Vtrip={trips[0]:.3f}V..{trips[-1]:.3f}V, max|DNL|={dnl:.4f} LSB, max|INL|={inl:.4f} LSB"
        )


def check_adc(adc: kf.DKCell, gds: Path, spice: Path, pins: list[str], out_dir: Path) -> None:
    analog.check_macro(adc, gds, spice, pins, out_dir)
    check_adc_ngspice(spice, label_prefix="ADC check")


def extract_adc_pex(
    gds: Path,
    out_dir: Path,
    pins: list[str] | None = None,
    check_ngspice: bool = True,
) -> Path:
    """Extract polygon-level Magic PEX (`r2r_ladder_4bit_sum.pex.spice`) and verify trip points."""
    pex_spice = out_dir / f"{CELL_NAME}.pex.spice"
    analog.run_magic_pex(gds, CELL_NAME, pins or CANONICAL_PINS, pex_spice, out_dir / "pex")
    print(f"  [Magic PEX | {CELL_NAME}] Extracted {pex_spice}")
    if check_ngspice:
        check_adc_ngspice(pex_spice, label_prefix="ADC PEX check")
    return pex_spice


def main() -> None:
    ap = argparse.ArgumentParser(description="Build & verify 4-bit Ramp ADC passive macro.")
    ap.add_argument("--out-dir", type=Path, default=REPO_ROOT / "gen/adc")
    ap.add_argument(
        "--no-check", action="store_true", help="Emit macro collateral without DRC/LVS/ngspice"
    )
    ap.add_argument("--render", action="store_true", help="Render GDS preview PNG via KLayout")
    ap.add_argument(
        "--pex",
        action="store_true",
        help="Also run Magic polygon RCX extraction (`r2r_ladder_4bit_sum.pex.spice`) and verify",
    )
    ap.add_argument(
        "--pex-only",
        action="store_true",
        help="Run Magic PEX extraction + ngspice check on an existing `<out-dir>/r2r_ladder_4bit_sum.gds`",
    )
    args = ap.parse_args()

    if args.pex_only:
        gds = args.out_dir / f"{CELL_NAME}.gds"
        assert gds.is_file(), f"Missing GDS for --pex-only: {gds}"
        extract_adc_pex(gds, args.out_dir, check_ngspice=not args.no_check)
        return

    s, adc, pins = build_adc()
    gds, _, _, spice = analog.emit_macro(s, adc, pins, args.out_dir)
    if not args.no_check:
        check_adc(adc, gds, spice, pins, args.out_dir)
    if args.pex:
        extract_adc_pex(gds, args.out_dir, pins=pins, check_ngspice=not args.no_check)
    if args.render:
        png = analog.render_gds(gds, args.out_dir / f"{adc.name}.png", width=1600, height=900)
        print(f"[adc] Rendered {png}")
    print(
        f"{adc.name}: {adc.info['width']:.2f} x {adc.info['height']:.2f} um "
        f"({adc.info['area']:.2f} um^2)"
    )


if __name__ == "__main__":
    main()
