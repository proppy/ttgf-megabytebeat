#!/usr/bin/env python3
"""4-bit Ramp ADC workflow: builds passive `r2r_ladder_4bit_sum` via `dac.py` + `src/adc_ramp.x`.

External `clkinv_4` drivers and `clkbuf_1` comparator live in `src/sc_adc_ramp_1ch_4b.v` (and are
instantiated in `src/adc_ramp.spice` around the LVS-verified `r2r_ladder_4bit_sum` passive ladder).
Run standalone (`python src/adc.py`) or import `build_adc`/`check_adc`.
"""

from __future__ import annotations

import argparse
import io
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request

import kfactory as kf
import xls

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
XLS_VERSION = "v0.0.0-11174-g32dc47058"


def find_or_fetch_xls_bin_dir() -> Path:
    """Locate XLS CLI binaries supporting impl-style procs (caching release tarball if needed)."""
    env_dir = os.environ.get("XLS_BIN_DIR")
    if env_dir and (Path(env_dir) / "ir_converter_main").exists():
        return Path(env_dir)
    in_path = shutil.which("ir_converter_main")
    if in_path:
        return Path(in_path).parent

    cache_dir = Path.home() / ".cache/xls" / f"xls-{XLS_VERSION}-linux-x64"
    if not (cache_dir / "ir_converter_main").exists():
        cache_dir.parent.mkdir(parents=True, exist_ok=True)
        url = (
            f"https://github.com/google/xls/releases/download/{XLS_VERSION}/"
            f"xls-{XLS_VERSION}-linux-x64.tar.gz"
        )
        print(f"[xls] Fetching XLS {XLS_VERSION} -> {cache_dir} ...")
        with urllib.request.urlopen(url) as resp:
            data = resp.read()
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
            tf.extractall(cache_dir.parent)
    return cache_dir


def compile_adc_xls(dslx_path: Path = SRC_DIR / "adc_ramp.x") -> tuple[str, str]:
    """Compile `src/adc_ramp.x` -> (IR, Verilog) via XLS CLI (`flop_outputs=false`)."""
    xls_bin = find_or_fetch_xls_bin_dir()
    ir = subprocess.run(
        [
            str(xls_bin / "ir_converter_main"),
            f"--dslx_path={dslx_path.parent}",
            "--top=adc_ramp",
            str(dslx_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout

    with tempfile.TemporaryDirectory() as tmp:
        ir_path = Path(tmp) / "adc_ramp.ir"
        opt_ir_path = Path(tmp) / "adc_ramp.opt.ir"
        ir_path.write_text(ir)
        opt_ir = subprocess.run(
            [str(xls_bin / "opt_main"), str(ir_path)],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        opt_ir_path.write_text(opt_ir)
        verilog = subprocess.run(
            [
                str(xls_bin / "codegen_main"),
                "--generator=pipeline",
                "--pipeline_stages=1",
                "--delay_model=unit",
                "--module_name=adc_ramp",
                "--reset=reset",
                "--reset_active_low=false",
                "--reset_asynchronous=false",
                "--reset_data_path=true",
                "--use_system_verilog=false",
                "--flop_inputs=false",
                "--flop_outputs=false",
                str(opt_ir_path),
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    return ir, verilog


def _check_adc_xls(dslx_path: Path, ir: str) -> None:
    xls_bin = find_or_fetch_xls_bin_dir()
    subprocess.run(
        [
            str(xls_bin / "interpreter_main"),
            "--compare=jit",
            f"--dslx_path={dslx_path.parent}",
            str(dslx_path),
        ],
        check=True,
    )
    fn_only_ir = ir.split("\ntop proc ")[0] + "\n"
    pkg_fn = xls.parse_ir_package(fn_only_ir)
    jit = pkg_fn.get_function("__adc_ramp__adc_ramp__step").to_jit()
    targets = [2, 5, 8, 11]
    st_val = xls.parse_typed_value(
        "(bits[4]:0, bits[4]:0, [bits[4]:0, bits[4]:0, bits[4]:0, bits[4]:0])"
    )
    emitted_word = None
    for cyc in range(16):
        cmp_mask = sum((1 << i) for i in range(4) if cyc < targets[i])
        res = jit.run([st_val, xls.Value.make_ubits(4, cmp_mask)])
        st_val = res.get_element(0)
        if res.get_element(2).get_bits().to_uint64() == 1:
            emitted_word = res.get_element(3).get_bits().to_uint64()
    expected = sum(targets[i] << (i * 4) for i in range(4))
    assert emitted_word == expected, f"JIT mismatch: got {emitted_word:#x}, expected {expected:#x}"
    print(
        f"  [ADC check] XLS proc --compare=jit + LLVM JIT verified 4x4b in-place crossing "
        f"latch ({targets} -> {emitted_word:#x})."
    )


def build_adc(
    l_1r: float = L_1R, l_2r: float = L_2R, w: float = W_RES
) -> tuple[kf.DSchematic, kf.DKCell, list[str], str, str]:
    """Build passive 4-bit summing R-2R ladder + compile `src/adc_ramp.x` -> (IR, Verilog)."""
    s, cell, pins = dac.build_r2r(
        bits=BITS, l_1r=l_1r, l_2r=l_2r, w=w, sum_res=True, name=CELL_NAME
    )
    ramp_ir, ramp_v = compile_adc_xls(SRC_DIR / "adc_ramp.x")
    return s, cell, pins, ramp_ir, ramp_v


def check_adc(
    adc: kf.DKCell, gds: Path, spice: Path, pins: list[str], ramp_ir: str, out_dir: Path
) -> None:
    _check_adc_xls(SRC_DIR / "adc_ramp.x", ramp_ir)
    dac.check_r2r(adc, gds, spice, pins, out_dir)

    for corner, rcorner in (("typical", "res_typical"), ("ff", "res_ff"), ("ss", "res_ss")):
        (trips,) = analog.run_ngspice(SRC_DIR / "adc_ramp.spice", spice, corner, rcorner)
        _, dnl, inl = analog.check_linearity(trips, f"ADC {corner}")
        print(
            f"  [ADC check | {corner:7s}] {len(trips) + 1}-step ngspice OK: "
            f"Vtrip={trips[0]:.3f}V..{trips[-1]:.3f}V, max|DNL|={dnl:.4f} LSB, max|INL|={inl:.4f} LSB"
        )


def main() -> None:
    ap = argparse.ArgumentParser(description="Build & verify 4-bit Ramp ADC sub-block.")
    ap.add_argument("--out-dir", type=Path, default=REPO_ROOT / "gen/adc")
    ap.add_argument("--render", action="store_true", help="Render GDS preview PNG via KLayout")
    args = ap.parse_args()

    s, adc, pins, ramp_ir, ramp_v = build_adc()
    gds, _, _, spice = dac.emit_r2r(s, adc, pins, args.out_dir)
    (args.out_dir / "adc_ramp.v").write_text(ramp_v)
    check_adc(adc, gds, spice, pins, ramp_ir, args.out_dir)
    if args.render:
        png = analog.render_gds(gds, args.out_dir / f"{adc.name}.png", width=1600, height=900)
        print(f"[adc] Rendered {png}")
    print(
        f"{adc.name}: {adc.info['width']:.2f} x {adc.info['height']:.2f} um "
        f"({adc.info['area']:.2f} um^2)"
    )


if __name__ == "__main__":
    main()
