#!/usr/bin/env python3
"""Stage and validate a drop-in megabytebeat_macro bundle for ishi-kai/wafer_space_RUN3_sample."""

from __future__ import annotations

import argparse
import json
import re
import shutil
from pathlib import Path

REQUIRED_SRC_FILES = (
    "adc_ramp.x",
    "bytebeat.x",
    "dac.x",
    "megabytebeat.x",
    "megabytebeat.ir",
    "megabytebeat.opt.ir",
    "megabytebeat.v",
    "r2r_ladder_4bit_sum.v",
    "r2r_ladder_8bit.v",
    "sc_adc_ramp_1ch_4b.v",
    "sc_r2r_dac_8bit.v",
)

REQUIRED_MACRO_FILES = (
    "adc/r2r_ladder_4bit_sum.gds",
    "adc/r2r_ladder_4bit_sum.lef",
    "adc/r2r_ladder_4bit_sum.lib",
    "adc/r2r_ladder_4bit_sum.spice",
    "adc/r2r_ladder_4bit_sum.pex.spice",
    "dac/r2r_ladder_8bit.gds",
    "dac/r2r_ladder_8bit.lef",
    "dac/r2r_ladder_8bit.lib",
    "dac/r2r_ladder_8bit.spice",
    "dac/r2r_ladder_8bit.pex.spice",
)

FINAL_VIEW_ENTRIES = (
    "def",
    "gds",
    "lef",
    "lib",
    "nl",
    "pnl",
    "sdc",
    "spice",
    "vh",
    "metrics.csv",
    "metrics.json",
)

REQUIRED_LIB_CORNERS = (
    "nom_tt_025C_5v00",
    "nom_ss_125C_4v50",
    "nom_ff_n40C_5v50",
    "min_tt_025C_5v00",
    "min_ss_125C_4v50",
    "min_ff_n40C_5v50",
    "max_tt_025C_5v00",
    "max_ss_125C_4v50",
    "max_ff_n40C_5v50",
)

ZERO_METRIC_GATES = (
    "flow__errors__count",
    "route__drc_errors",
    "design__xor_difference__count",
    "magic__drc_error__count",
    "klayout__drc_error__count",
    "design__lvs_error__count",
)

ANTENNA_METRIC_KEYS = (
    "route__antenna_violation__count",
    "route__antenna_violation_count",
)


def _get_antenna_violations(metrics: dict) -> int | None:
    for key in ANTENNA_METRIC_KEYS:
        if key in metrics and metrics[key] is not None:
            return metrics[key]
    return None


def rewrite_config_for_ishikai(config_text: str) -> str:
    """Rewrite dir:: paths (from either ishikai/ or gen/) for chip/macros/megabytebeat/."""
    rewritten = config_text.replace("dir::../src/", "dir::src/")
    rewritten = rewritten.replace(
        "dir::../ishikai/megabytebeat_macro.sv", "dir::src/megabytebeat_macro.sv"
    )
    rewritten = rewritten.replace("dir::../ishikai/", "dir::")
    rewritten = rewritten.replace("dir::../gen/", "dir::macros/")
    rewritten = rewritten.replace("dir::adc/", "dir::macros/adc/")
    rewritten = rewritten.replace("dir::dac/", "dir::macros/dac/")
    rewritten = rewritten.replace("dir::megabytebeat_macro.sv", "dir::src/megabytebeat_macro.sv")
    return rewritten


def stage_bundle(
    repo_root: Path,
    final_dir: Path,
    out_dir: Path,
    config_path: Path | None = None,
) -> dict:
    """Copy sources, analog sub-macros, self-contained config, pin order, SDC, and final views."""
    src_dir = repo_root / "src"
    gen_dir = repo_root / "gen"
    ishikai_dir = repo_root / "ishikai"

    if out_dir.exists():
        shutil.rmtree(out_dir)
    bundle_src = out_dir / "src"
    bundle_macros = out_dir / "macros"
    bundle_final = out_dir / "final"
    bundle_src.mkdir(parents=True)
    bundle_macros.mkdir(parents=True)
    bundle_final.mkdir(parents=True)

    for name in REQUIRED_SRC_FILES:
        path = src_dir / name
        if not path.is_file():
            raise FileNotFoundError(f"Missing required source file: {path}")
        shutil.copy2(path, bundle_src / name)

    for rel in REQUIRED_MACRO_FILES:
        path = gen_dir / rel
        if not path.is_file():
            raise FileNotFoundError(f"Missing required analog macro file: {path}")
        dst = bundle_macros / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dst)

    shutil.copy2(ishikai_dir / "megabytebeat_macro.sv", bundle_src / "megabytebeat_macro.sv")
    shutil.copy2(ishikai_dir / "megabytebeat_macro.sdc", out_dir / "megabytebeat_macro.sdc")
    shutil.copy2(ishikai_dir / "pdn_cfg.tcl", out_dir / "pdn_cfg.tcl")
    shutil.copy2(ishikai_dir / "pin_order.cfg", out_dir / "pin_order.cfg")

    resolved_cfg = config_path if config_path is not None else (ishikai_dir / "config.yaml")
    if not resolved_cfg.is_file():
        raise FileNotFoundError(f"Missing LibreLane config file: {resolved_cfg}")
    config_text = resolved_cfg.read_text(encoding="utf-8")
    (out_dir / "config.yaml").write_text(
        rewrite_config_for_ishikai(config_text),
        encoding="utf-8",
    )

    for entry in FINAL_VIEW_ENTRIES:
        src_entry = final_dir / entry
        if not src_entry.exists():
            raise FileNotFoundError(f"Missing required LibreLane final view: {src_entry}")
        dst_entry = bundle_final / entry
        if src_entry.is_dir():
            shutil.copytree(src_entry, dst_entry)
        else:
            shutil.copy2(src_entry, dst_entry)

    return validate_bundle(out_dir)


def validate_bundle(bundle_dir: Path) -> dict:
    """Verify all views consumed by ISHI-KAI macros_gnss.yaml and check signoff metrics."""
    final_dir = bundle_dir / "final"
    required_files = [
        bundle_dir / "config.yaml",
        final_dir / "def" / "megabytebeat_macro.def",
        final_dir / "gds" / "megabytebeat_macro.gds",
        final_dir / "lef" / "megabytebeat_macro.lef",
        final_dir / "nl" / "megabytebeat_macro.nl.v",
        final_dir / "pnl" / "megabytebeat_macro.pnl.v",
        final_dir / "sdc" / "megabytebeat_macro.sdc",
        final_dir / "spice" / "megabytebeat_macro.spice",
        final_dir / "vh" / "megabytebeat_macro.vh",
        final_dir / "metrics.json",
    ]
    for corner in REQUIRED_LIB_CORNERS:
        required_files.append(final_dir / "lib" / corner / f"megabytebeat_macro__{corner}.lib")

    missing = [str(p.relative_to(bundle_dir)) for p in required_files if not p.is_file()]
    if missing:
        raise RuntimeError(f"Bundle is missing required views: {', '.join(missing)}")

    # Verify staged config.yaml is self-contained (every dir:: ref exists inside bundle_dir
    # and programmatic macro + clkbuf_20 placements are baked in).
    cfg_text = (bundle_dir / "config.yaml").read_text(encoding="utf-8")
    for token in ("instances:", "MANUAL_GLOBAL_PLACEMENTS:"):
        if token not in cfg_text:
            raise RuntimeError(f"Staged bundle config.yaml is missing {token!r}")
    broken_refs = [
        rel
        for rel in re.findall(r"dir::([^\s,'\"\]]+)", cfg_text)
        if not (bundle_dir / rel).is_file()
    ]
    if broken_refs:
        raise RuntimeError(f"Staged config.yaml has broken dir:: refs: {', '.join(broken_refs)}")

    metrics = json.loads((final_dir / "metrics.json").read_text(encoding="utf-8"))
    failures = []
    for key in ZERO_METRIC_GATES:
        val = metrics.get(key)
        if val != 0:
            failures.append(f"{key}={val} (expected 0)")

    antenna_val = _get_antenna_violations(metrics)
    if antenna_val != 0:
        failures.append(f"antenna_violations={antenna_val} (expected 0)")

    for key in ("timing__setup__wns", "timing__hold__wns"):
        val = metrics.get(key)
        if val is None or val < 0:
            failures.append(f"{key}={val} (expected >= 0)")

    if failures:
        raise RuntimeError("Signoff metric gate failed: " + "; ".join(failures))

    return metrics


def _format_corner_violation(metrics: dict, metric_prefix: str) -> str:
    """Format worst-corner violation count alongside nom_tt and violating corner(s)."""
    worst = metrics.get(metric_prefix)
    corner_prefix = f"{metric_prefix}__corner:"
    corner_vals = {
        key[len(corner_prefix) :]: val
        for key, val in metrics.items()
        if key.startswith(corner_prefix) and val is not None
    }
    if worst is None and corner_vals:
        worst = max(corner_vals.values())
    if worst is None:
        return "`?`"
    nom_tt = corner_vals.get("nom_tt_025C_5v00")
    worst_corners = sorted(c for c, v in corner_vals.items() if v == worst and v > 0)
    if worst == 0:
        return "`0` (all corners clean)"
    details = []
    if worst_corners:
        if len(worst_corners) == len(corner_vals) and len(worst_corners) > 1:
            details.append(f"all {len(worst_corners)} corners")
        else:
            details.append(f"worst: {', '.join(worst_corners)}")
    if nom_tt is not None:
        details.append(f"nom_tt: {nom_tt}")
    if details:
        return f"`{worst}` ({'; '.join(details)})"
    return f"`{worst}`"


def format_summary(metrics: dict) -> str:
    """Render a compact Markdown summary of macro signoff metrics."""
    util_pct = float(metrics.get("design__instance__utilization", 0.0)) * 100.0
    stdcells = metrics.get("design__instance__count__stdcell", "?")
    setup_ws = metrics.get("timing__setup__ws", "?")
    hold_ws = metrics.get("timing__hold__ws", "?")
    antenna_val = _get_antenna_violations(metrics)
    slew_str = _format_corner_violation(metrics, "design__max_slew_violation__count")
    cap_str = _format_corner_violation(metrics, "design__max_cap_violation__count")
    lines = [
        "### ISHI会 `megabytebeat_macro` Signoff Summary",
        "",
        "| Metric | Value |",
        "| --- | --- |",
        f"| Die BBox | `{metrics.get('design__die__bbox', '?')}` |",
        f"| Stdcells / Utilization | `{stdcells}` (`{util_pct:.1f}%`) |",
        f"| Magic DRC / KLayout DRC | `{metrics.get('magic__drc_error__count')}` / `{metrics.get('klayout__drc_error__count')}` |",
        f"| KLayout XOR / Netgen LVS | `{metrics.get('design__xor_difference__count')}` / `{metrics.get('design__lvs_error__count')}` |",
        f"| Antenna / Route DRC | `{antenna_val}` / `{metrics.get('route__drc_errors')}` |",
        f"| STA Setup WS / Hold WS | `{setup_ws}` ns / `{hold_ws}` ns |",
        f"| Max Slew Violations (worst corner) | {slew_str} |",
        f"| Max Cap Violations (worst corner) | {cap_str} |",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--final-dir", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--summary-file", type=Path, default=None)
    args = parser.parse_args()

    metrics = stage_bundle(
        args.repo_root,
        args.final_dir,
        args.out_dir,
        config_path=args.config,
    )
    summary = format_summary(metrics)
    print(summary)
    if args.summary_file is not None:
        with args.summary_file.open("a", encoding="utf-8") as fh:
            fh.write(summary + "\n")


if __name__ == "__main__":
    main()
