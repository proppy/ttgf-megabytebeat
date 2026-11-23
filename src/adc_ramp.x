// Copyright 2026 Google LLC.
// SPDX-License-Identifier: Apache-2.0

// 4-channel x 4-bit In-Place Crossing-Latch Ramp ADC Controller (24 FFs total).
// Drives step_s[3:0] (combinational via flop_outputs=false) into 4x sc_adc_ramp_1ch_4b
// and latches each channel in-place on the single clock where cmp[i] transitions 1 -> 0.

#![feature(explicit_state_access)]

pub struct AdcState { step: u4, done: u4, vals: u4[4] }

pub proc adc_ramp {
    cmp_r: chan<u4> in,
    step_s: chan<u4> out,
    params_s: chan<u16> out,
    state: AdcState,
}

impl adc_ramp {
    pub fn step(st: AdcState, cmp: u4) -> (AdcState, u4, bool, u16) {
        let is_zero = st.step == u4:0;
        let is_last = st.step == u4:15;
        let (next_done, next_vals) = for (i, (done, vals)): (u32, (u4, u4[4])) in u32:0..u32:4 {
            let d_prev = !is_zero && (st.done[i+:u1] == u1:1);
            let cmp_hi = cmp[i+:u1] == u1:1;
            let trigger = !d_prev && (!cmp_hi || is_last);
            let next_v = if trigger { st.step } else { vals[i] };
            let done_bit = ((d_prev || trigger) as u4) << (i as u4);
            (done | done_bit, update(vals, i, next_v))
        }((u4:0, st.vals));
        let params: u16 = next_vals[3] ++ next_vals[2] ++ next_vals[1] ++ next_vals[0];
        let next_st = AdcState { step: st.step + u4:1, done: next_done, vals: next_vals };
        (next_st, st.step, is_last, params)
    }

    pub fn new(cmp_r: chan<u4> in, step_s: chan<u4> out, params_s: chan<u16> out) -> Self {
        Self {
            cmp_r,
            step_s,
            params_s,
            state: AdcState { step: u4:0, done: u4:0, vals: u4[4]:[0xa, 0x2, 0xa, 0x0] },
        }
    }

    fn next(self) {
        let st = read(self.state);
        let (tok, cmp) = recv(join(), self.cmp_r);
        let (next_st, step_out, emit_params, params) = Self::step(st, cmp);
        let tok_s = send(tok, self.step_s, step_out);
        let tok_p = send_if(tok, self.params_s, emit_params, params);
        join(tok_s, tok_p);
        write(self.state, next_st);
    }
}

#[test]
fn adc_ramp_step_test() {
    let init_st = AdcState { step: u4:0, done: u4:0, vals: u4[4]:[0, 0, 0, 0] };
    // Virtual analog inputs corresponding to codes (v0=2, v1=5, v2=8, v3=11) -> 0xb852
    let (_, _, last_vld, last_packed) =
        for (i, (st, _, _, _)): (u32, (AdcState, u4, bool, u16)) in u32:0..u32:16 {
            let cmp = ((i < u32:2) as u4) | (((i < u32:5) as u4) << u4:1) |
                      (((i < u32:8) as u4) << u4:2) | (((i < u32:11) as u4) << u4:3);
            let (next_st, step_out, vld, packed) = adc_ramp::step(st, cmp);
            assert_eq(step_out, i as u4);
            (next_st, step_out, vld, packed)
        }((init_st, u4:0, false, u16:0));
    assert_eq(last_vld, true);
    assert_eq(last_packed, u16:0xb852);
}

#[test]
proc adc_ramp_test {
    terminator: chan<bool> out,
    cmp_s: chan<u4> out,
    step_r: chan<u4> in,
    params_r: chan<u16> in,
}

impl adc_ramp_test {
    fn new(terminator: chan<bool> out) -> Self {
        let (cmp_s, cmp_r) = chan<u4>("cmp");
        let (step_s, step_r) = chan<u4>("step");
        let (params_s, params_r) = chan<u16>("params");
        adc_ramp::new(cmp_r, step_s, params_s).spawn();
        Self { terminator, cmp_s, step_r, params_r }
    }

    fn next(self) {
        // Sweep 1: virtual analog codes [2, 5, 8, 11] -> 0xb852
        let tok = for (i, tok): (u32, token) in u32:0..u32:16 {
            let cmp = ((i < u32:2) as u4) | (((i < u32:5) as u4) << u4:1) |
                      (((i < u32:8) as u4) << u4:2) | (((i < u32:11) as u4) << u4:3);
            let tok = send(tok, self.cmp_s, cmp);
            let (tok, got_step) = recv(tok, self.step_r);
            assert_eq(got_step, i as u4);
            tok
        }(join());
        let (tok, sw1) = recv(tok, self.params_r);
        assert_eq(sw1, u16:0xb852);

        // Sweep 2 (verifies wraparound reset + updated targets): [15, 0, 7, 3] -> 0x370f
        let tok = for (i, tok): (u32, token) in u32:0..u32:16 {
            let cmp = ((i < u32:15) as u4) | (((i < u32:0) as u4) << u4:1) |
                      (((i < u32:7) as u4) << u4:2) | (((i < u32:3) as u4) << u4:3);
            let tok = send(tok, self.cmp_s, cmp);
            let (tok, got_step) = recv(tok, self.step_r);
            assert_eq(got_step, i as u4);
            tok
        }(tok);
        let (tok, sw2) = recv(tok, self.params_r);
        assert_eq(sw2, u16:0x370f);
        send(tok, self.terminator, true);
    }
}
