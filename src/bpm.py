#!/usr/bin/env python3
"""Bresenham BPM / Tap-Tempo Sync build & JIT verification orchestrator (src/bpm_sync.x)."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path

import xls

SRC_DIR = Path(__file__).resolve().parent
REPO_ROOT = SRC_DIR.parent


@dataclass
class Bpm:
    module_name: str
    step_samples: int
    param_width: int
    dslx: Path
    verilog: Path


def build_bpm(
    step_samples: int = 1024,
    counter_bits: int = 16,
    param_width: int = 16,
    default_period: int = 4096,
    out_dir: Path = REPO_ROOT / "gen/bpm",
    check: bool = False,
) -> Bpm:
    out_dir.mkdir(parents=True, exist_ok=True)
    dslx_path = SRC_DIR / "bpm_sync.x"
    dslx_src = dslx_path.read_text()

    ir = xls.convert_dslx_to_ir(
        dslx_src, str(dslx_path), "bpm_sync", additional_search_paths=[str(SRC_DIR)]
    )
    if check:
        pkg_fn = xls.parse_ir_package(ir)
        jit = pkg_fn.get_function(xls.mangle_dslx_name("bpm_sync", "bpm_sync_step")).to_jit()
        st = xls.parse_typed_value(
            f"(bits[1]:0, bits[{counter_bits}]:0, bits[{counter_bits}]:{default_period}, "
            f"bits[{counter_bits}]:0, bits[{param_width}]:0x1234)"
        )
        ticks = 0
        for _ in range(default_period):
            res = jit.run(
                [
                    st,
                    xls.Value.make_ubits(1, 0),
                    xls.Value.make_ubits(param_width, 0),
                    xls.Value.make_ubits(1, 0),
                ]
            )
            st = res.get_element(0)
            ticks += res.get_element(1).get_bits().to_uint64()
        assert ticks == step_samples, (
            f"Expected {step_samples} ticks over {default_period}, got {ticks}"
        )

        new_period = step_samples * 2
        for cyc in range(new_period + 1):
            bpm_hi = 1 if cyc in (0, new_period) else 0
            res = jit.run(
                [
                    st,
                    xls.Value.make_ubits(1, bpm_hi),
                    xls.Value.make_ubits(param_width, 0x5678),
                    xls.Value.make_ubits(1, 1),
                ]
            )
            st = res.get_element(0)
        relock_ticks = 0
        for _ in range(new_period):
            res = jit.run(
                [
                    st,
                    xls.Value.make_ubits(1, 0),
                    xls.Value.make_ubits(param_width, 0),
                    xls.Value.make_ubits(1, 0),
                ]
            )
            st = res.get_element(0)
            relock_ticks += res.get_element(1).get_bits().to_uint64()
        assert relock_ticks == step_samples, (
            f"Expected {step_samples} ticks after re-lock to {new_period}, got {relock_ticks}"
        )
        print(
            f"  [BPM check] XLS LLVM JIT verified {ticks}/{default_period} default and "
            f"{relock_ticks}/{new_period} re-locked Bresenham ticks."
        )

    proc_name = [line.split()[1].split("(")[0] for line in ir.splitlines() if "proc " in line][0]
    opt_ir = xls.optimize_ir(ir, top=proc_name)
    res = xls.parse_ir_package(opt_ir).schedule_and_codegen(
        with_delay_model=True,
        generator="pipeline",
        pipeline_stages=1,
        delay_model="unit",
        module_name="bpm_sync",
        reset="reset",
        reset_active_low=False,
        reset_asynchronous=False,
        reset_data_path=True,
        use_system_verilog=False,
        streaming_channel_data_suffix="",
        streaming_channel_valid_suffix="_vld",
        streaming_channel_ready_suffix="_rdy",
        flop_inputs=False,
        flop_outputs=True,
    )
    v_path = out_dir / "bpm_sync.v"
    v_path.write_text(res.get_verilog_text())
    return Bpm(
        module_name="bpm_sync",
        step_samples=step_samples,
        param_width=param_width,
        dslx=dslx_path,
        verilog=v_path,
    )


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Build & verify src/bpm_sync.x Bresenham BPM block.")
    ap.add_argument("--step-samples", type=int, default=1024)
    ap.add_argument("--counter-bits", type=int, default=16)
    ap.add_argument("--param-width", type=int, default=16)
    ap.add_argument("--default-period", type=int, default=4096)
    ap.add_argument("--out-dir", type=Path, default=REPO_ROOT / "gen/bpm")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    print(
        build_bpm(
            args.step_samples,
            args.counter_bits,
            args.param_width,
            args.default_period,
            args.out_dir,
            args.check,
        )
    )
