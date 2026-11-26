# ISHI会 `slot 0p5x1` Integration Target (`megabytebeat_macro`)

This directory maintains the hardened mixed-signal `megabytebeat_macro` integration target
and matching `slot 0p5x1` padring/testbench overlay for
[`ishi-kai/wafer_space_RUN3_sample`](https://github.com/ishi-kai/wafer_space_RUN3_sample)
directly from `src/` (single source of truth, zero duplicated RTL).

## Contract

- **Macro**: `megabytebeat_macro` (`960 × 960 µm`, dedicated `VDD_BB` / shared `VSS`, `RT_MAX_LAYER: Metal4`, `60 µm` Metal4/Metal5 PDN pins, 4× passive `r2r_ladder_4bit_sum` ADCs + 2× passive `r2r_ladder_8bit` DACs)
- **Wrapper**: [`megabytebeat_macro.sv`](megabytebeat_macro.sv) wraps `../src/tt_um_proppy_megabytebeat.v` with an integrated clock gate (`gf180mcu_fd_sc_mcu7t5v0__icgtp_4`, quiet by default when `ena == 0` with all 4 ADC and 2 DAC ladders clamped to `0 V`) and `VDD_BB`-domain padring tie outputs (`vdd_bb_hi_s/e`, `vss_lo_s/w/e`).
- **Config / Pins / SDC / PDN**: [`config.yaml`](config.yaml), [`pin_order.cfg`](pin_order.cfg), [`megabytebeat_macro.sdc`](megabytebeat_macro.sdc), and [`pdn_cfg.tcl`](pdn_cfg.tcl) match `chip/macros/megabytebeat/` in `ishi-kai/wafer_space_RUN3_sample`.
- **Padring & Testbench Patch**: [`wafer_space.patch`](wafer_space.patch) is a minimal `git diff` against `ishi-kai/wafer_space_RUN3_sample` updating `chip/src/{slot_defines.svh,chip_top.sv,chip_core.sv}`, `chip/librelane/{slots/slot_0p5x1.yaml,pad_cfg.tcl,chip_top.sdc}`, and `chip/cocotb/chip_top_tb.py` so the `VDD_BB` padring (`bb_ua_PAD[3:0]` = 4× ADC CV inputs, `bb_ua_PAD[5:4]` = 2× DAC outputs via `gf180mcu_fd_io__asig_5p0`, `bb_uo_PAD[4:0]` = 5× PWM, `bb_ui_bpm_PAD` = BPM sync, `bb_uio_PAD[5:0]` = DAC selectors) and full-chip tests evolve in lockstep with `src/`.

## Automated CI Verification & Fork Sync

[`.github/workflows/ishikai.yaml`](../.github/workflows/ishikai.yaml) runs on every push:

1. Compiles `src/megabytebeat.x` via Google XLS (`gf180mcu_5v0`), builds & verifies `gen/dac/r2r_ladder_8bit` and `gen/adc/r2r_ladder_4bit_sum` (DRC, LVS, PVT ngspice, Magic PEX), hardens `megabytebeat_macro` with LibreLane (`gf180mcuD`), and verifies post-PnR 4× ADC & 2× DAC SPEF parasitics.
2. Stages and validates all views required by `macros_gnss.yaml` (`.gds`, `.lef`, `.vh`, `.pnl.v`, `.spice`, and all 9 corner `.lib` files) and asserts zero DRC/LVS/XOR/Antenna/STA errors.
3. Clones `ishi-kai/wafer_space_RUN3_sample`, applies [`wafer_space.patch`](wafer_space.patch), drops in the new `chip/macros/megabytebeat/`, and runs ISHI会's full-chip `chip/cocotb/chip_top_tb.py` in both **RTL (`GL=0`)** and **Gate-Level (`GL=1`)**.
4. On pushes to `main` (or manual `workflow_dispatch`), force-pushes a clean commit on top of upstream `ishi-kai/wafer_space_RUN3_sample:main` to `proppy/wafer_space_RUN3_sample:megabytebeat-sync` and prints a one-click GitHub PR URL in the workflow summary.

### One-Time Secret Setup for Auto-Sync

To enable automatic pushes to `proppy/wafer_space_RUN3_sample`:

1. Create a GitHub fine-grained PAT with **Repository permissions → Contents: Read and write** scoped **only** to `proppy/wafer_space_RUN3_sample`.
2. Add it as repository secret **`ISHIKAI_FORK_TOKEN`** in `proppy/ttgf-megabytebeat`.
