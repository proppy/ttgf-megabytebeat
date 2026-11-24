#!/usr/bin/env python3
"""4-bit Ramp ADC passive analog macro: builds & verifies `r2r_ladder_4bit_sum` via `dac.py`.

External `clkinv_4` drivers and `clkbuf_1` comparator live in `src/sc_adc_ramp_1ch_4b.v`
(instantiated directly from `src/adc_ramp.x` via XLS `#[extern_verilog]` and modeled at
transistor level in `src/adc_ramp.spice`).
"""

from __future__ import annotations

import argparse
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


def build_adc(
    l_1r: float = L_1R, l_2r: float = L_2R, w: float = W_RES
) -> tuple[kf.DSchematic, kf.DKCell, list[str]]:
    """Build passive 4-bit summing R-2R ladder for the ramp ADC."""
    return dac.build_r2r(bits=BITS, l_1r=l_1r, l_2r=l_2r, w=w, sum_res=True, name=CELL_NAME)


def check_adc(adc: kf.DKCell, gds: Path, spice: Path, pins: list[str], out_dir: Path) -> None:
    analog.check_macro(adc, gds, spice, pins, out_dir)
    for corner, rcorner in (("typical", "res_typical"), ("ff", "res_ff"), ("ss", "res_ss")):
        (trips,) = analog.run_ngspice(SRC_DIR / "adc_ramp.spice", spice, corner, rcorner)
        _, dnl, inl = analog.check_linearity(trips, f"ADC {corner}")
        print(
            f"  [ADC check | {corner:7s}] {len(trips) + 1}-step ngspice OK: "
            f"Vtrip={trips[0]:.3f}V..{trips[-1]:.3f}V, max|DNL|={dnl:.4f} LSB, max|INL|={inl:.4f} LSB"
        )


def main() -> None:
    ap = argparse.ArgumentParser(description="Build & verify 4-bit Ramp ADC passive macro.")
    ap.add_argument("--out-dir", type=Path, default=REPO_ROOT / "gen/adc")
    ap.add_argument("--render", action="store_true", help="Render GDS preview PNG via KLayout")
    args = ap.parse_args()

    s, adc, pins = build_adc()
    gds, _, _, spice = analog.emit_macro(s, adc, pins, args.out_dir)
    check_adc(adc, gds, spice, pins, args.out_dir)
    if args.render:
        png = analog.render_gds(gds, args.out_dir / f"{adc.name}.png", width=1600, height=900)
        print(f"[adc] Rendered {png}")
    print(
        f"{adc.name}: {adc.info['width']:.2f} x {adc.info['height']:.2f} um "
        f"({adc.info['area']:.2f} um^2)"
    )


if __name__ == "__main__":
    main()
