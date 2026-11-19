// Copyright 2026 Google LLC.
// SPDX-License-Identifier: Apache-2.0

// Unified Multi-Track Bytebeat Synthesizer Proc.
// Imports the 5 canonical bytebeat formulas from src/bytebeat_*.x, evaluates them
// concurrently with a single shared t: u32 counter (enabling XLS cross-track CSE),
// and uses blocking recv(params_r) backpressure as the sample strobe.

import bytebeat_atunetoshare;
import bytebeat_fractaltrees;
import bytebeat_sierpinskiharmony;
import bytebeat_the42melody;
import bytebeat_untitleddroid;

pub fn megabytebeat_eval(t: u32, packed_params: u16) -> u8[5] {
    let a = (packed_params >> u16:0) as u4;
    let b = (packed_params >> u16:4) as u4;
    let c = (packed_params >> u16:8) as u4;
    let d = (packed_params >> u16:12) as u4;
    u8[5]:[
        bytebeat_the42melody::the42melody(t, a, b, c, d),
        bytebeat_fractaltrees::fractaltrees(t, a, b, c, d),
        bytebeat_untitleddroid::untitleddroid(t, a, b, c, d),
        bytebeat_atunetoshare::atunetoshare(t, a, b, c, d),
        bytebeat_sierpinskiharmony::sierpinskiharmony(t, a, b, c, d),
    ]
}

pub proc megabytebeat {
    params_r: chan<u16> in;
    pcm_s: chan<u8[5]> out;

    init { u32:0 }

    config(params_r: chan<u16> in, pcm_s: chan<u8[5]> out) { (params_r, pcm_s) }

    next(t: u32) {
        let tok = token();
        let (tok, params) = recv(tok, params_r);
        let pcm = megabytebeat_eval(t, params);
        send(tok, pcm_s, pcm);
        t + u32:1
    }
}

#[test]
fn megabytebeat_eval_test() {
    let params = u16:0x0a2a;  // a=0xa, b=0x2, c=0xa, d=0x0
    let out0 = megabytebeat_eval(u32:0, params);
    assert_eq(out0[0], u8:0);
    let out1 = megabytebeat_eval(u32:2048, params);
    assert_eq(out1[0], bytebeat_the42melody::the42melody(u32:2048, u4:0xa, u4:0x2, u4:0xa, u4:0x0));
    assert_eq(out1[1], bytebeat_fractaltrees::fractaltrees(u32:2048, u4:0xa, u4:0x2, u4:0xa, u4:0x0));
    assert_eq(out1[2], bytebeat_untitleddroid::untitleddroid(u32:2048, u4:0xa, u4:0x2, u4:0xa, u4:0x0));
    assert_eq(out1[3], bytebeat_atunetoshare::atunetoshare(u32:2048, u4:0xa, u4:0x2, u4:0xa, u4:0x0));
    assert_eq(out1[4], bytebeat_sierpinskiharmony::sierpinskiharmony(u32:2048, u4:0xa, u4:0x2, u4:0xa, u4:0x0));
}
