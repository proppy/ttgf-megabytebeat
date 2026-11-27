# ISHI会 `slot 0p5x1` Integration Target (`megabytebeat_macro`)

This directory maintains the hardened mixed-signal `megabytebeat_macro` integration target
and matching `slot 0p5x1` padring/testbench overlay for
[`ishi-kai/wafer_space_RUN3_sample`](https://github.com/ishi-kai/wafer_space_RUN3_sample)
directly from `src/` (single source of truth, zero duplicated RTL).

## Contract

- **Macro**: `megabytebeat_macro` (`960 × 960 µm`, dedicated `VDD_BB` / shared `VSS`, `32.768 kHz` `/4` = `8.192 kHz` DAC-only, `RT_MAX_LAYER: Metal4`, `60 µm` Metal4/Metal5 PDN pins, 5× passive `r2r_ladder_8bit` poly-resistor DACs, repaired against `DEFAULT_CORNER: nom_ss_125C_4v50`)
- **Wrapper**: [`megabytebeat_macro.sv`](megabytebeat_macro.sv) instantiates XLS `megabytebeat` directly with a 2-bit `/4` sample divider (`32.768 kHz -> 8.192 kHz`), an integrated clock gate (`gf180mcu_fd_sc_mcu7t5v0__icgtp_4`, quiet by default when `ena == 0` with all 5 R-2R DAC ladders clamped to `0 V`), and `VDD_BB`-domain padring tie outputs (`vdd_bb_hi_s`, `vss_lo_s/w/e`).
- **Config / Pins / SDC / PDN**: [`config.yaml`](config.yaml), [`pin_order.cfg`](pin_order.cfg), [`megabytebeat_macro.sdc`](megabytebeat_macro.sdc), and [`pdn_cfg.tcl`](pdn_cfg.tcl) serve as the base config; [`src/chip.py`](../src/chip.py) programmatically computes macro and tapcell-free `clkbuf_20` driver placements and emits a self-contained drop-in `config.yaml` for `chip/macros/megabytebeat/` in `ishi-kai/wafer_space_RUN3_sample`.
- **Padring, Straps & Testbench Patch**: [`wafer_space.patch`](wafer_space.patch) is a minimal `git diff` against `ishi-kai/wafer_space_RUN3_sample` updating `chip/src/{slot_defines.svh,chip_top.sv,chip_core.sv}`, `chip/librelane/{slots/slot_0p5x1.yaml,pad_cfg.tcl,chip_top.sdc,plugins/librelane_plugin_gnss/analog_straps.py}`, and `chip/cocotb/chip_top_tb.py` so the `VDD_BB` padring (`bb_ua_PAD[0..2]` on `PAD_WEST` slots 4..6, `bb_ua_PAD[3..4]` on `PAD_EAST` slots 0..1, `nc_bb[0..2]` on South, `bb_ui_PAD[7:0]`, `bb_uio_PAD[7:0]`), Metal2/Via2/Metal3 analog straps (with clearance, proximity, and non-NC `asig_5p0` invariants), and full-chip tests evolve in lockstep with `src/`.

## Automated CI Verification & Fork Sync

[`.github/workflows/ishikai.yaml`](../.github/workflows/ishikai.yaml) runs on every push:

1. Compiles `src/megabytebeat.x` via Google XLS (`gf180mcu_5v0`, `32.768 kHz`), builds & verifies `gen/dac/r2r_ladder_8bit` (DRC, LVS, PVT ngspice, Magic PEX), generates programmatic placements via [`src/chip.py`](../src/chip.py), hardens `megabytebeat_macro` with LibreLane (`gf180mcuD`), and verifies tightened post-PnR 5× DAC SPEF parasitics.
2. Stages and validates all views required by `macros_gnss.yaml` (`.gds`, `.lef`, `.vh`, `.pnl.v`, `.spice`, self-contained `config.yaml`, and all 9 corner `.lib` files), asserts zero DRC/LVS/XOR/Antenna/STA errors, and reports worst-corner max-slew / max-cap metrics in the workflow summary.
3. Clones `ishi-kai/wafer_space_RUN3_sample`, applies [`wafer_space.patch`](wafer_space.patch), drops in the new `chip/macros/megabytebeat/`, runs full-chip `chip_top` LibreLane PnR + `Netgen.LVS` (gating on 0 LVS / 0 DRT DRC / 0 disconnected pins / analog strap invariants), and runs ISHI会's full-chip `chip/cocotb/chip_top_tb.py` in both **RTL (`GL=0`)** and **Gate-Level (`GL=1`)** against the freshly generated `chip/final/pnl/chip_top.pnl.v`.
4. On pushes to `main` (or manual `workflow_dispatch`), cleans temporary run artifacts and appends a clean fast-forward commit to `proppy/wafer_space_RUN3_sample:megabytebeat-sync` when already based on upstream `main` (or resets cleanly with `--force-with-lease` if upstream `main` advanced) and prints a one-click GitHub PR URL in the workflow summary.

### One-Time Secret Setup for Auto-Sync

To enable automatic pushes to `proppy/wafer_space_RUN3_sample`:

1. Create a GitHub fine-grained PAT with **Repository permissions → Contents: Read and write** scoped **only** to `proppy/wafer_space_RUN3_sample`.
2. Add it as repository secret **`ISHIKAI_FORK_TOKEN`** in `proppy/ttgf-megabytebeat`.
