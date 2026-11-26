// Copyright 2026 Google LLC.
// SPDX-License-Identifier: Apache-2.0

// Unified Mixed-Signal Multi-Track Bytebeat Synthesizer Proc.
// Composes 4x 4-bit ramp ADCs (`adc_ramp::adc_ramp_step` via #[extern_verilog]),
// 5 canonical bytebeat formulas from `src/bytebeat.x` (single shared `t: u32` counter
// enabling XLS cross-track CSE), and 2x 8-bit poly R-2R DACs (`dac::r2r_dac_bank<u32:2>`
// via #[extern_verilog]) with combinational `en` standby power-gating.

#![feature(explicit_state_access)]

import adc_ramp;
import bytebeat;
import dac;

pub struct SynthState {
    adc: adc_ramp::AdcState,
    params: u16,
    t: u32,
    pcm: u8[5],
}

pub const SYNTH_INIT_STATE = SynthState {
    adc: adc_ramp::ADC_INIT_STATE,
    params: u16:0x0a2a,  // a=0xa, b=0x2, c=0xa, d=0x0
    t: u32:0,
    pcm: u8[5]:[0, ...],
};

pub fn eval_tracks(t: u32, packed_params: u16) -> u8[5] {
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

pub fn select_dac_track(pcm: u8[5], sel: u3, fallback: u8) -> u8 {
    match sel {
        u3:0 => pcm[0],
        u3:1 => pcm[1],
        u3:2 => pcm[2],
        u3:3 => pcm[3],
        u3:4 => pcm[4],
        _ => fallback,
    }
}

pub proc megabytebeat {
    en_r: chan<bool> in,
    adc_in_r: chan<u4[4]> in,
    dac_sel_r: chan<u3[2]> in,
    adc_step_s: chan<u4> out,
    adc_params_s: chan<u16> out,
    pcm_s: chan<u8[5]> out,
    dac_s: chan<u1[2]> out,
    state: SynthState,
}

impl megabytebeat {
    pub fn new(
        en_r: chan<bool> in,
        adc_in_r: chan<u4[4]> in,
        dac_sel_r: chan<u3[2]> in,
        adc_step_s: chan<u4> out,
        adc_params_s: chan<u16> out,
        pcm_s: chan<u8[5]> out,
        dac_s: chan<u1[2]> out,
    ) -> Self {
        Self {
            en_r,
            adc_in_r,
            dac_sel_r,
            adc_step_s,
            adc_params_s,
            pcm_s,
            dac_s,
            state: SYNTH_INIT_STATE,
        }
    }

    fn next(self) {
        let st = read(self.state);
        let (tok_e, en) = recv(join(), self.en_r);
        let (tok_a, adc_in) = recv(join(), self.adc_in_r);
        let (tok_d, dac_sel) = recv(join(), self.dac_sel_r);
        let tok = join(tok_e, tok_a, tok_d);

        let (next_adc, step_eff, adc_vld, adc_params) =
            adc_ramp::adc_ramp_step(st.adc, en, adc_in);
        let active_params = if adc_vld { adc_params } else { st.params };
        // Advance sample every 4 ticks (/64 * 4 = /256, aligned with 256-clock PWM frames).
        let sample_tick = (st.adc.step & u4:3) == u4:3;
        let next_pcm = if sample_tick { eval_tracks(st.t, active_params) } else { st.pcm };
        let next_t = if sample_tick { st.t + u32:1 } else { st.t };

        let dac_codes = u8[2]:[
            select_dac_track(st.pcm, dac_sel[0], st.pcm[0]),
            select_dac_track(st.pcm, dac_sel[1], st.pcm[1]),
        ];
        let dacs = dac::r2r_dac_bank<u32:2>(en, dac_codes);

        send(tok, self.adc_step_s, step_eff);
        send_if(tok, self.adc_params_s, adc_vld, adc_params);
        send(tok, self.pcm_s, st.pcm);
        send(tok, self.dac_s, dacs);
        write(
            self.state,
            SynthState { adc: next_adc, params: active_params, t: next_t, pcm: next_pcm },
        );
    }
}

#[test]
fn megabytebeat_samples_test() {
    let params = u16:0x0a2a;  // a=0xa, b=0x2, c=0xa, d=0x0
    assert_eq(eval_tracks(u32:0, u16:0x0000), u8[5]:[0, 0, 0, 0, 0]);
    assert_eq(eval_tracks(u32:0, params), u8[5]:[0, 0, 0, 0, 0]);
    assert_eq(eval_tracks(u32:0, u16:0xffff), u8[5]:[0, 0, 0, 0, 0]);
    let got = eval_tracks(u32:2048, params);
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
    adc_in_s: chan<u4[4]> out,
    dac_sel_s: chan<u3[2]> out,
    adc_step_r: chan<u4> in,
    adc_params_r: chan<u16> in,
    pcm_r: chan<u8[5]> in,
    dac_r: chan<u1[2]> in,
}

impl megabytebeat_test {
    fn new(terminator: chan<bool> out) -> Self {
        let (en_s, en_r) = chan<bool>("en");
        let (adc_in_s, adc_in_r) = chan<u4[4]>("adc_in");
        let (dac_sel_s, dac_sel_r) = chan<u3[2]>("dac_sel");
        let (adc_step_s, adc_step_r) = chan<u4>("adc_step");
        let (adc_params_s, adc_params_r) = chan<u16>("adc_params");
        let (pcm_s, pcm_r) = chan<u8[5]>("pcm");
        let (dac_s, dac_r) = chan<u1[2]>("dac");
        megabytebeat::new(en_r, adc_in_r, dac_sel_r, adc_step_s, adc_params_s, pcm_s, dac_s)
            .spawn();
        Self { terminator, en_s, adc_in_s, dac_sel_s, adc_step_r, adc_params_r, pcm_r, dac_r }
    }

    fn next(self) {
        // Sweep 1 (16 ticks): knobs [5, 1, 3, 0], DAC0=track 2, DAC1=track 1.
        let (tok, t_cnt, held_pcm) = for (i, (tok, t_cnt, held_pcm)): (u32, (token, u32, u8[5])) in
            u32:0..u32:16 {
            let tok = send(tok, self.en_s, true);
            let tok = send(tok, self.adc_in_s, u4[4]:[5, 1, 3, 0]);
            let tok = send(tok, self.dac_sel_s, u3[2]:[2, 1]);
            let (tok, got_step) = recv(tok, self.adc_step_r);
            let (tok, got_pcm) = recv(tok, self.pcm_r);
            let (tok, got_dac) = recv(tok, self.dac_r);
            assert_eq(got_step, i as u4);
            assert_eq(got_pcm, held_pcm);
            assert_eq(got_dac, u1[2]:[held_pcm[2][7+:u1], held_pcm[1][7+:u1]]);
            if (i % u32:4) == u32:3 {
                let exp_params = if i == u32:15 { u16:0x0315 } else { u16:0x0a2a };
                (tok, t_cnt + u32:1, eval_tracks(t_cnt, exp_params))
            } else {
                (tok, t_cnt, held_pcm)
            }
        }((join(), u32:0, u8[5]:[0, ...]));
        let (tok, got_p1) = recv(tok, self.adc_params_r);
        assert_eq(got_p1, u16:0x0315);

        // Out-of-range selectors [7, 6] fall back to [track 0, track 1];
        // standby en=false clamps step to 0xF and both DACs to 0.
        let tok = send(tok, self.en_s, true);
        let tok = send(tok, self.adc_in_s, u4[4]:[10, 2, 10, 0]);
        let tok = send(tok, self.dac_sel_s, u3[2]:[7, 6]);
        let (tok, got_step) = recv(tok, self.adc_step_r);
        let (tok, got_pcm) = recv(tok, self.pcm_r);
        let (tok, got_dac) = recv(tok, self.dac_r);
        assert_eq(got_step, u4:0);
        assert_eq(got_pcm, held_pcm);
        assert_eq(got_dac, u1[2]:[held_pcm[0][7+:u1], held_pcm[1][7+:u1]]);

        let tok = send(tok, self.en_s, false);
        let tok = send(tok, self.adc_in_s, u4[4]:[10, 2, 10, 0]);
        let tok = send(tok, self.dac_sel_s, u3[2]:[2, 1]);
        let (tok, got_step_off) = recv(tok, self.adc_step_r);
        let (tok, _) = recv(tok, self.pcm_r);
        let (tok, got_dac_off) = recv(tok, self.dac_r);
        assert_eq(got_step_off, u4:15);
        assert_eq(got_dac_off, u1[2]:[0, 0]);
        send(tok, self.terminator, true);
    }
}
