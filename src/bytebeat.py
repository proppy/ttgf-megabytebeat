#!/usr/bin/env python3
"""Multi-Track Bytebeat Synthesizer sub-block (compiles checked-in src/megabytebeat.x).

Uses XLS CLI (`v0.0.0-11174-g32dc47058`) for impl-style proc compilation & `--compare=jit`
proc verification, and `xls-python` LLVM JIT to cross-check `megabytebeat::samples` against
the Python golden reference.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
import subprocess

import xls

import adc

SRC_DIR = Path(__file__).resolve().parent
REPO_ROOT = SRC_DIR.parent
DEFAULT_DSLX = SRC_DIR / "megabytebeat.x"


@dataclass
class Bytebeat:
    module_name: str
    tracks: int
    sample_bits: int
    param_bits: int
    dslx: Path
    verilog: Path


def _golden_tracks(t: int, a: int, b: int, c: int, d: int) -> list[int]:
    ab = (b << 4) | a
    cd = (d << 4) | c
    ab_nz = 1 if ab == 0 else ab
    return [
        (t * (ab & (t >> cd))) & 0xFF,
        (t | (t % ab_nz) | (t % (cd + 2))) & 0xFF,
        ((t >> c) if ((t >> a) & b) != 0 else (((~t & 0xFFFFFFFF) + 1) >> d)) & 0xFF,
        ((t * ((t >> a) | (t >> b))) & cd) & 0xFF,
        (((t * a) & (t >> b)) | ((t * c) & (t >> d))) & 0xFF,
    ]


def build_bytebeat(
    tracks: int = 5,
    param_bits: int = 4,
    sample_bits: int = 8,
    dslx_path: Path = DEFAULT_DSLX,
    out_dir: Path = REPO_ROOT / "gen/bytebeat",
    check: bool = False,
) -> Bytebeat:
    if param_bits != 4 or sample_bits != 8 or not (1 <= tracks <= 5):
        raise ValueError(
            "Checked-in src/megabytebeat.x supports 1..5 tracks with 4-bit params and 8-bit PCM."
        )
    out_dir.mkdir(parents=True, exist_ok=True)
    xls_bin = adc.find_or_fetch_xls_bin_dir()

    ir_path = out_dir / "megabytebeat.ir"
    opt_ir_path = out_dir / "megabytebeat.opt.ir"
    v_path = out_dir / "megabytebeat.v"

    ir_proc = subprocess.run(
        [
            str(xls_bin / "ir_converter_main"),
            f"--dslx_path={dslx_path.parent}",
            "--top=megabytebeat",
            str(dslx_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    ir = ir_proc.stdout
    ir_path.write_text(ir)

    if check:
        subprocess.run(
            [
                str(xls_bin / "interpreter_main"),
                "--compare=jit",
                f"--dslx_path={dslx_path.parent}",
                str(dslx_path),
            ],
            check=True,
        )
        # Cross-check megabytebeat::samples directly via xls-python LLVM JIT.
        fn_only_ir = ir.split("\ntop proc ")[0] + "\n"
        pkg_fn = xls.parse_ir_package(fn_only_ir)
        jit = pkg_fn.get_function("__megabytebeat__megabytebeat__samples").to_jit()
        a, b, c, d = 0xA, 0x2, 0xA, 0x0
        params_val = xls.Value.make_ubits(16, (d << 12) | (c << 8) | (b << 4) | a)
        for t_in in (0, 1024, 2048, 3073, 8191):
            arr_val = jit.run([xls.Value.make_ubits(32, t_in), params_val])
            expected = _golden_tracks(t_in, a, b, c, d)
            for idx in range(5):
                got = arr_val.get_element(idx).get_bits().to_uint64()
                assert got == expected[idx], (
                    f"Track {idx} mismatch at t={t_in}: got {got}, expected {expected[idx]}"
                )
        print(
            "  [Bytebeat check] XLS proc --compare=jit + LLVM JIT verified all 5 tracks "
            "of src/megabytebeat.x against Python golden reference."
        )

    opt_proc = subprocess.run(
        [str(xls_bin / "opt_main"), str(ir_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    opt_ir_path.write_text(opt_proc.stdout)

    cg_proc = subprocess.run(
        [
            str(xls_bin / "codegen_main"),
            "--generator=pipeline",
            "--pipeline_stages=1",
            "--delay_model=unit",
            "--module_name=megabytebeat",
            "--reset=reset",
            "--reset_active_low=false",
            "--reset_asynchronous=false",
            "--reset_data_path=true",
            "--use_system_verilog=false",
            "--flop_inputs=false",
            "--flop_outputs=true",
            str(opt_ir_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    v_path.write_text(cg_proc.stdout)
    return Bytebeat(
        module_name="megabytebeat",
        tracks=tracks,
        sample_bits=sample_bits,
        param_bits=param_bits,
        dslx=dslx_path,
        verilog=v_path,
    )


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Build & verify src/megabytebeat.x sub-block.")
    ap.add_argument("--tracks", type=int, default=5)
    ap.add_argument("--param-bits", type=int, default=4)
    ap.add_argument("--sample-bits", type=int, default=8)
    ap.add_argument("--dslx", type=Path, default=DEFAULT_DSLX)
    ap.add_argument("--out-dir", type=Path, default=REPO_ROOT / "gen/bytebeat")
    args = ap.parse_args()
    print(
        build_bytebeat(
            tracks=args.tracks,
            param_bits=args.param_bits,
            sample_bits=args.sample_bits,
            dslx_path=args.dslx,
            out_dir=args.out_dir,
            check=True,
        )
    )
