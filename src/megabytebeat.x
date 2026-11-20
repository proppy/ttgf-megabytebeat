// Copyright 2026 Google LLC.
// SPDX-License-Identifier: Apache-2.0

// Unified Multi-Track Bytebeat Synthesizer Proc.
// Evaluates all 5 canonical bytebeat formulas from src/bytebeat.x concurrently
// with a single shared t: u32 counter (enabling XLS cross-track CSE), using
// blocking recv(params_r) backpressure as the sample strobe.

#![feature(explicit_state_access)]

import bytebeat;

pub proc megabytebeat {
    params_r: chan<u16> in,
    pcm_s: chan<u8[5]> out,
    t: u32,
}

impl megabytebeat {
    pub fn samples(t: u32, packed_params: u16) -> u8[5] {
        let a = packed_params[0+:u4];
        let b = packed_params[4+:u4];
        let c = packed_params[8+:u4];
        let d = packed_params[12+:u4];
        u8[5]:[
            bytebeat::the42melody(t, a, b, c, d), bytebeat::fractaltrees(t, a, b, c, d),
            bytebeat::untitleddroid(t, a, b, c, d), bytebeat::atunetoshare(t, a, b, c, d),
            bytebeat::sierpinskiharmony(t, a, b, c, d),
        ]
    }

    pub fn new(params_r: chan<u16> in, pcm_s: chan<u8[5]> out) -> Self {
        Self { params_r, pcm_s, t: u32:0 }
    }

    fn next(self) {
        let t = read(self.t);
        let (tok, params) = recv(join(), self.params_r);
        let pcm = Self::samples(t, params);
        send(tok, self.pcm_s, pcm);
        write(self.t, t + u32:1);
    }
}

#[test]
fn megabytebeat_samples_test() {
    let params = u16:0x0a2a;  // a=0xa, b=0x2, c=0xa, d=0x0
    assert_eq(megabytebeat::samples(u32:0, params), u8[5]:[0, 0, 0, 0, 0]);
    let got = megabytebeat::samples(u32:2048, params);
    assert_eq(got[0], bytebeat::the42melody(u32:2048, u4:0xa, u4:0x2, u4:0xa, u4:0x0));
    assert_eq(got[1], bytebeat::fractaltrees(u32:2048, u4:0xa, u4:0x2, u4:0xa, u4:0x0));
    assert_eq(got[2], bytebeat::untitleddroid(u32:2048, u4:0xa, u4:0x2, u4:0xa, u4:0x0));
    assert_eq(got[3], bytebeat::atunetoshare(u32:2048, u4:0xa, u4:0x2, u4:0xa, u4:0x0));
    assert_eq(got[4], bytebeat::sierpinskiharmony(u32:2048, u4:0xa, u4:0x2, u4:0xa, u4:0x0));
}

#[test]
proc megabytebeat_test {
    terminator: chan<bool> out,
    params_s: chan<u16> out,
    pcm_r: chan<u8[5]> in,
}

impl megabytebeat_test {
    fn new(terminator: chan<bool> out) -> Self {
        let (params_s, params_r) = chan<u16>("params");
        let (pcm_s, pcm_r) = chan<u8[5]>("pcm");
        megabytebeat::new(params_r, pcm_s).spawn();
        Self { terminator, params_s, pcm_r }
    }

    fn next(self) {
        // Sweep across default and updated parameter packs to verify t state + live params.
        let tok = for (i, tok): (u32, token) in u32:0..u32:32 {
            let params = if i < u32:16 { u16:0x0a2a } else { u16:0xa375 };
            let tok = send(tok, self.params_s, params);
            let (tok, got) = recv(tok, self.pcm_r);
            assert_eq(got, megabytebeat::samples(i, params));
            tok
        }(join());
        send(tok, self.terminator, true);
    }
}
