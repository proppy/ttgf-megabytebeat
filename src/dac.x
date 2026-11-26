// Copyright 2026 Google LLC.
// SPDX-License-Identifier: Apache-2.0

// 8-bit poly R-2R DAC Verilog FFI (`sc_r2r_dac_8bit`).
// In DSLX interpreter/JIT, returns MSB `d[7]` matching the 1-bit Verilog `SIM` stub.
// In emitted Verilog (with `--flop_outputs=false`), `out` wires directly to analog pad `ua[i]`.

#[extern_verilog("sc_r2r_dac_8bit {fn} (.d({d}), .out({return}))")]
pub fn r2r_dac_8bit(d: u8) -> u1 {
    d[7+:u1]
}

// Drive `N` 8-bit R-2R DAC macros with combinational standby gating (`en=false` -> 0x00 = 0 V).
pub fn r2r_dac_bank<N: u32>(en: bool, codes: u8[N]) -> u1[N] {
    let gated = if en { codes } else { zero!<u8[N]>() };
    map(gated, r2r_dac_8bit)
}

#[test]
fn r2r_dac_8bit_test() {
    assert_eq(r2r_dac_8bit(u8:0x00), u1:0);
    assert_eq(r2r_dac_8bit(u8:0x7f), u1:0);
    assert_eq(r2r_dac_8bit(u8:0x80), u1:1);
    assert_eq(r2r_dac_8bit(u8:0xff), u1:1);
}

#[test]
fn r2r_dac_bank_test() {
    let codes = u8[5]:[0x00, 0x7f, 0x80, 0xaa, 0xff];
    assert_eq(r2r_dac_bank<u32:5>(true, codes), u1[5]:[0, 0, 1, 1, 1]);
    assert_eq(r2r_dac_bank<u32:5>(false, codes), u1[5]:[0, 0, 0, 0, 0]);
}
