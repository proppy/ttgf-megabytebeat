# ISHI会 `slot 0p5x1` Integration Target (`megabytebeat_macro`)

This directory maintains the hardened `megabytebeat_macro` integration target for
[`ishi-kai/wafer_space_RUN3_sample`](https://github.com/ishi-kai/wafer_space_RUN3_sample)
directly from `src/` (single source of truth, zero duplicated RTL).

## Contract

- **Macro**: `megabytebeat_macro` (`960 × 960 µm`, dedicated `VDD_BB` / shared `VSS`, `RT_MAX_LAYER: Metal4`, `60 µm` Metal4/Metal5 PDN pins)
- **Wrapper**: [`megabytebeat_macro.sv`](megabytebeat_macro.sv) wraps `../src/tt_um_proppy_megabytebeat.v` with an integrated clock gate (`gf180mcu_fd_sc_mcu7t5v0__icgtp_4`, quiet by default when `ena == 0`) and `VDD_BB`-domain padring tie outputs (`vdd_bb_hi_s/e`, `vss_lo_s/w/e`).
- **Config / Pins**: [`config.yaml`](config.yaml) and [`pin_order.cfg`](pin_order.cfg) match `chip/macros/megabytebeat/` in `ishi-kai/wafer_space_RUN3_sample`.

## Automated CI Verification & Fork Sync

[`.github/workflows/ishikai.yaml`](../.github/workflows/ishikai.yaml) runs on every push:

1. Compiles `src/megabytebeat.x` via Google XLS and hardens `megabytebeat_macro` with LibreLane (`gf180mcuD`).
2. Stages and validates all views required by `macros_gnss.yaml` (`.gds`, `.lef`, `.vh`, `.pnl.v`, `.spice`, and all 9 corner `.lib` files) and asserts zero DRC/LVS/XOR/Antenna/STA errors.
3. Clones `ishi-kai/wafer_space_RUN3_sample`, drops in the new `chip/macros/megabytebeat/`, and runs ISHI会's full-chip `chip/cocotb/chip_top_tb.py` in both **RTL (`GL=0`)** and **Gate-Level (`GL=1`)**.
4. On pushes to `main` (or manual `workflow_dispatch`), force-pushes a clean commit on top of upstream `ishi-kai/wafer_space_RUN3_sample:main` to `proppy/wafer_space_RUN3_sample:megabytebeat-sync` and prints a one-click GitHub PR URL in the workflow summary.

### One-Time Secret Setup for Auto-Sync

To enable automatic pushes to `proppy/wafer_space_RUN3_sample`:

1. Create a GitHub fine-grained PAT with **Repository permissions → Contents: Read and write** scoped **only** to `proppy/wafer_space_RUN3_sample`.
2. Add it as repository secret **`ISHIKAI_FORK_TOKEN`** in `proppy/ttgf-megabytebeat`.
