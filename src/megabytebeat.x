// Copyright 2026 Google LLC.
// SPDX-License-Identifier: Apache-2.0

// Unified Multi-Track Bytebeat Synthesizer Proc with 5x R-2R DAC Verilog FFI.
// Evaluates all 5 canonical bytebeat formulas from src/bytebeat.x concurrently
// with a single shared t: u32 counter (enabling XLS cross-track CSE), holds the
// registered `pcm: u8[5]` frame in proc state for glitch-free DAC & PWM drive, and
// instantiates 5x sc_r2r_dac_8bit via `dac::r2r_dac_bank` with combinational `en` gating.

#![feature(explicit_state_access)]

import bytebeat;
import dac;

pub proc megabytebeat {
    en_r: chan<bool> in,
    params_r: chan<u16> in,
    pcm_s: chan<u8[5]> out,
    dac_s: chan<u1[5]> out,
    t: u32,
    pcm: u8[5],
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

    pub fn new(
        en_r: chan<bool> in,
        params_r: chan<u16> in,
        pcm_s: chan<u8[5]> out,
        dac_s: chan<u1[5]> out,
    ) -> Self {
        // Mathematically all 5 bytebeat formulas evaluate to 0 at t=0 for *any* `params`
        // (0 * x = 0, 0 % n = 0, (-0) >> d = 0), so initializing `pcm=[0,...]` and `t=1`
        // keeps Frame 0 exact regardless of `params`.
        // Registering `pcm` in proc state is required because `--flop_outputs=false` (needed
        // so `#[extern_verilog]` `sc_r2r_dac_8bit.out` wires directly to analog pad `ua[i]`
        // without an XLS output flop) would otherwise expose raw combinational bytebeat logic
        // (`%`, `*`, shifts) to the R-2R DAC drivers (causing analog switching glitches), tear
        // 256-clock PWM frames whenever `params` changes mid-frame, and couple `ui_in`/`uio_in`
        // combinationally into the DAC/PWM outputs.
        Self { en_r, params_r, pcm_s, dac_s, t: u32:1, pcm: u8[5]:[0, ...] }
    }

    fn next(self) {
        let t = read(self.t);
        let pcm = read(self.pcm);
        let (tok_e, en) = recv(join(), self.en_r);
        let (tok_p, params) = recv(join(), self.params_r);
        let tok = join(tok_e, tok_p);
        let dacs = dac::r2r_dac_bank<u32:5>(en, pcm);
        send(tok, self.pcm_s, pcm);
        send(tok, self.dac_s, dacs);
        write(self.pcm, Self::samples(t, params));
        write(self.t, t + u32:1);
    }
}

#[test]
fn megabytebeat_samples_test() {
    let params = u16:0x0a2a;  // a=0xa, b=0x2, c=0xa, d=0x0
    assert_eq(megabytebeat::samples(u32:0, u16:0x0000), u8[5]:[0, 0, 0, 0, 0]);
    assert_eq(megabytebeat::samples(u32:0, params), u8[5]:[0, 0, 0, 0, 0]);
    assert_eq(megabytebeat::samples(u32:0, u16:0xffff), u8[5]:[0, 0, 0, 0, 0]);
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
    en_s: chan<bool> out,
    params_s: chan<u16> out,
    pcm_r: chan<u8[5]> in,
    dac_r: chan<u1[5]> in,
}

impl megabytebeat_test {
    fn new(terminator: chan<bool> out) -> Self {
        let (en_s, en_r) = chan<bool>("en");
        let (params_s, params_r) = chan<u16>("params");
        let (pcm_s, pcm_r) = chan<u8[5]>("pcm");
        let (dac_s, dac_r) = chan<u1[5]>("dac");
        megabytebeat::new(en_r, params_r, pcm_s, dac_s).spawn();
        Self { terminator, en_s, params_s, pcm_r, dac_r }
    }

    fn next(self) {
        // Sweep across default, updated, and standby-gated (en=false) frames.
        // Frame i outputs the registered sample latched on frame i-1 (Frame 0 = 0).
        let (tok, _) = for (i, (tok, held)): (u32, (token, u8[5])) in u32:0..u32:32 {
            let en = i < u32:30;
            let params = if i < u32:16 { u16:0x0a2a } else { u16:0xa375 };
            let tok = send(tok, self.en_s, en);
            let tok = send(tok, self.params_s, params);
            let (tok, got_pcm) = recv(tok, self.pcm_r);
            let (tok, got_dac) = recv(tok, self.dac_r);
            assert_eq(got_pcm, held);
            assert_eq(got_dac, dac::r2r_dac_bank<u32:5>(en, held));
            (tok, megabytebeat::samples(i + u32:1, params))
        }((join(), u8[5]:[0, ...]));
        send(tok, self.terminator, true);
    }
}
