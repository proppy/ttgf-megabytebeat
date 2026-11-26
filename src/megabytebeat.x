// Copyright 2026 Google LLC.
// SPDX-License-Identifier: Apache-2.0

// Unified Mixed-Signal Multi-Track Bytebeat Synthesizer Proc.
// Composes 4x 4-bit ramp ADCs (`adc_ramp::adc_ramp_step` via #[extern_verilog]),
// Bresenham BPM / tap-tempo sync (`bpm_sync::bpm_sync_step`), 5 canonical bytebeat
// formulas from `src/bytebeat.x` (single shared `t: u32` counter enabling XLS cross-track
// CSE), and 2x 8-bit poly R-2R DACs (`dac::r2r_dac_bank<u32:2>` via #[extern_verilog])
// with combinational `en` standby power-gating.

#![feature(explicit_state_access)]

import adc_ramp;
import bpm_sync;
import bytebeat;
import dac;

pub struct State {
    // Flopped ramp-ADC state (`step`, `done`, and per-channel crossing latches `vals`):
    // each 4-bit conversion sweeps 16 ticks, and registering comparator results isolates
    // raw analog `sc_adc_ramp_1ch_4b` transitions from the bytebeat/DAC combinational
    // paths under `--flop_outputs=false`.
    adc: adc_ramp::AdcState,
    // Bresenham BPM/tap-tempo state and last completed 16-step ADC parameter snapshot
    // (latched atomically on `step == 15` so mid-sweep writes never tear parameters).
    bpm: bpm_sync::BpmState,
    t: u32,
    // Registered 5-track PCM frame for glitch-free R-2R DAC & PWM drive under
    // `--flop_outputs=false`.
    pcm: u8[5],
}

pub proc MegaBytebeat {
    en_r: chan<bool> in,
    bpm_r: chan<bool> in,
    adc_in_r: chan<u4[4]> in,
    dac_sel_r: chan<u3[2]> in,
    pcm_s: chan<u8[5]> out,
    dac_s: chan<u1[2]> out,
    state: State,
}

impl MegaBytebeat {
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
        bpm_r: chan<bool> in,
        adc_in_r: chan<u4[4]> in,
        dac_sel_r: chan<u3[2]> in,
        pcm_s: chan<u8[5]> out,
        dac_s: chan<u1[2]> out,
    ) -> Self {
        Self {
            en_r,
            bpm_r,
            adc_in_r,
            dac_sel_r,
            pcm_s,
            dac_s,
            state: State {
                adc: adc_ramp::ADC_INIT_STATE,
                bpm: bpm_sync::BPM_INIT_STATE,
                t: u32:0,
                pcm: u8[5]:[0, ...],
            },
        }
    }

    fn next(self) {
        let state = read(self.state);
        let (tok_e, en) = recv(join(), self.en_r);
        let (tok_b, bpm_in) = recv(join(), self.bpm_r);
        let (tok_a, adc_in) = recv(join(), self.adc_in_r);
        let (tok_d, dac_sel) = recv(join(), self.dac_sel_r);
        let tok = join(tok_e, tok_b, tok_a, tok_d);

        let (next_adc, _, adc_vld, adc_params) = adc_ramp::adc_ramp_step(state.adc, en, adc_in);
        let (next_bpm, sample_tick, params) =
            bpm_sync::bpm_sync_step(state.bpm, bpm_in, adc_params, adc_vld);
        let next_pcm = if sample_tick { Self::samples(state.t, params) } else { state.pcm };
        let next_t = if sample_tick { state.t + u32:1 } else { state.t };

        let dac_codes = u8[2]:[
            state.pcm[if dac_sel[0] < u3:5 { dac_sel[0] } else { u3:0 }],
            state.pcm[if dac_sel[1] < u3:5 { dac_sel[1] } else { u3:1 }],
        ];
        let dacs = dac::r2r_dac_bank<u32:2>(en, dac_codes);

        send(tok, self.pcm_s, state.pcm);
        send(tok, self.dac_s, dacs);
        write(self.state, State { adc: next_adc, bpm: next_bpm, t: next_t, pcm: next_pcm });
    }
}

#[test]
fn megabytebeat_samples_test() {
    let params = u16:0x0a2a;  // a=0xa, b=0x2, c=0xa, d=0x0
    assert_eq(MegaBytebeat::samples(u32:0, u16:0x0000), u8[5]:[0, 0, 0, 0, 0]);
    assert_eq(MegaBytebeat::samples(u32:0, params), u8[5]:[0, 0, 0, 0, 0]);
    assert_eq(MegaBytebeat::samples(u32:0, u16:0xffff), u8[5]:[0, 0, 0, 0, 0]);
    let got = MegaBytebeat::samples(u32:2048, params);
    assert_eq(got[0], bytebeat::the42melody(u32:2048, u4:0xa, u4:0x2, u4:0xa, u4:0x0));
    assert_eq(got[1], bytebeat::fractaltrees(u32:2048, u4:0xa, u4:0x2, u4:0xa, u4:0x0));
    assert_eq(got[2], bytebeat::untitleddroid(u32:2048, u4:0xa, u4:0x2, u4:0xa, u4:0x0));
    assert_eq(got[3], bytebeat::atunetoshare(u32:2048, u4:0xa, u4:0x2, u4:0xa, u4:0x0));
    assert_eq(got[4], bytebeat::sierpinskiharmony(u32:2048, u4:0xa, u4:0x2, u4:0xa, u4:0x0));
}

#[test]
proc MegaBytebeatTest {
    terminator: chan<bool> out,
    en_s: chan<bool> out,
    bpm_s: chan<bool> out,
    adc_in_s: chan<u4[4]> out,
    dac_sel_s: chan<u3[2]> out,
    pcm_r: chan<u8[5]> in,
    dac_r: chan<u1[2]> in,
}

impl MegaBytebeatTest {
    fn new(terminator: chan<bool> out) -> Self {
        let (en_s, en_r) = chan<bool>("en");
        let (bpm_s, bpm_r) = chan<bool>("bpm");
        let (adc_in_s, adc_in_r) = chan<u4[4]>("adc_in");
        let (dac_sel_s, dac_sel_r) = chan<u3[2]>("dac_sel");
        let (pcm_s, pcm_r) = chan<u8[5]>("pcm");
        let (dac_s, dac_r) = chan<u1[2]>("dac");
        MegaBytebeat::new(en_r, bpm_r, adc_in_r, dac_sel_r, pcm_s, dac_s).spawn();
        Self { terminator, en_s, bpm_s, adc_in_s, dac_sel_s, pcm_r, dac_r }
    }

    fn next(self) {
        // Sweep 1 (16 ticks): knobs [5, 1, 3, 0] -> 0x0315 on tick 15; DAC0=track 2, DAC1=track 1.
        let (tok, t_cnt, held_pcm) =
            for (i, (tok, t_cnt, held_pcm)): (u32, (token, u32, u8[5])) in u32:0..u32:16 {
                let tok = send(tok, self.en_s, true);
                let tok = send(tok, self.bpm_s, false);
                let tok = send(tok, self.adc_in_s, u4[4]:[5, 1, 3, 0]);
                let tok = send(tok, self.dac_sel_s, u3[2]:[2, 1]);
                let (tok, got_pcm) = recv(tok, self.pcm_r);
                let (tok, got_dac) = recv(tok, self.dac_r);
                assert_eq(got_pcm, held_pcm);
                assert_eq(got_dac, u1[2]:[held_pcm[2][7+:u1], held_pcm[1][7+:u1]]);
                if (i % u32:4) == u32:3 {
                    let exp_params = if i == u32:15 { u16:0x0315 } else { u16:0x0a2a };
                    (tok, t_cnt + u32:1, MegaBytebeat::samples(t_cnt, exp_params))
                } else {
                    (tok, t_cnt, held_pcm)
                }
            }((join(), u32:0, u8[5]:[0, ...]));

        // Tick 16 observes the latched 0x0315 sweep result; out-of-range selectors [7, 6]
        // fall back to [track 0, track 1]; standby en=false clamps both DACs to 0.
        let tok = send(tok, self.en_s, true);
        let tok = send(tok, self.bpm_s, false);
        let tok = send(tok, self.adc_in_s, u4[4]:[10, 2, 10, 0]);
        let tok = send(tok, self.dac_sel_s, u3[2]:[7, 6]);
        let (tok, got_pcm) = recv(tok, self.pcm_r);
        let (tok, got_dac) = recv(tok, self.dac_r);
        assert_eq(got_pcm, held_pcm);
        assert_eq(got_pcm, MegaBytebeat::samples(u32:3, u16:0x0315));
        assert_eq(got_dac, u1[2]:[held_pcm[0][7+:u1], held_pcm[1][7+:u1]]);

        let tok = send(tok, self.en_s, false);
        let tok = send(tok, self.bpm_s, false);
        let tok = send(tok, self.adc_in_s, u4[4]:[10, 2, 10, 0]);
        let tok = send(tok, self.dac_sel_s, u3[2]:[2, 1]);
        let (tok, _) = recv(tok, self.pcm_r);
        let (tok, got_dac_off) = recv(tok, self.dac_r);
        assert_eq(got_dac_off, u1[2]:[0, 0]);
        send(tok, self.terminator, true);
    }
}
