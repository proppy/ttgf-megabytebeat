// Copyright 2026 Google LLC.
// SPDX-License-Identifier: Apache-2.0

// 4-channel x 4-bit In-Place Crossing-Latch Ramp ADC Controller (24 FFs total).
// Uses XLS Verilog FFI (#[extern_verilog]) to instantiate 4x sc_adc_ramp_1ch_4b
// directly while supplying a cycle-accurate DSLX comparator model for JIT/interpreter tests.

#![feature(explicit_state_access)]

pub struct AdcState {
    step: u4,
    done: u4,
    vals: u4[4],
}

pub const ADC_INIT_STATE = AdcState {
    step: u4:0,
    done: u4:0,
    vals: u4[4]:[0xa, 0x2, 0xa, 0x0],
};

// Analog 4-bit resistive-summing ramp comparator channel (`sc_adc_ramp_1ch_4b`).
// In DSLX interpreter/JIT, `adc_in` is the quantized virtual input code (0..15) and
// returns 1 while `step < adc_in`. In emitted Verilog, `adc_in[0]` wires directly to the
// 1-bit analog pad `ua[ch]` (unused upper bits are tied low and optimized away by synthesis).
#[extern_verilog(
    "sc_adc_ramp_1ch_4b {fn} (.step({step}), .adc_in({adc_in}[0]), .cmp({return}))"
)]
pub fn adc_ramp_1ch(step: u4, adc_in: u4) -> u1 {
    (step < adc_in) as u1
}

// One ramp tick across all 4 channels.
// When `en` is false (reset or deselected `!ena`), `step_eff` clamps to 4'hF so the
// `clkinv_12` drivers hold all passive R-2R ladder inputs at 0 V (~0 nA standby).
pub fn adc_ramp_step(
    st: AdcState,
    en: bool,
    adc_in: u4[4],
) -> (AdcState, u4, bool, u16) {
    let step_eff = if en { st.step } else { u4:15 };
    let is_zero = st.step == u4:0;
    let is_last = st.step == u4:15;
    let (next_done, next_vals) = for (i, (done, vals)): (u32, (u4, u4[4])) in u32:0..u32:4 {
        let cmp_hi = adc_ramp_1ch(step_eff, adc_in[i]) == u1:1;
        let d_prev = !is_zero && (st.done[i+:u1] == u1:1);
        let trigger = !d_prev && (!cmp_hi || is_last);
        let next_v = if trigger { st.step } else { vals[i] };
        let done_bit = ((d_prev || trigger) as u4) << (i as u4);
        (done | done_bit, update(vals, i, next_v))
    }((u4:0, st.vals));
    let params: u16 = next_vals[3] ++ next_vals[2] ++ next_vals[1] ++ next_vals[0];
    let next_st = AdcState { step: st.step + u4:1, done: next_done, vals: next_vals };
    (next_st, step_eff, is_last, params)
}

pub proc adc_ramp {
    en_r: chan<bool> in,
    adc_in_r: chan<u4[4]> in,
    step_s: chan<u4> out,
    params_s: chan<u16> out,
    state: AdcState,
}

impl adc_ramp {
    pub fn new(
        en_r: chan<bool> in,
        adc_in_r: chan<u4[4]> in,
        step_s: chan<u4> out,
        params_s: chan<u16> out,
    ) -> Self {
        Self { en_r, adc_in_r, step_s, params_s, state: ADC_INIT_STATE }
    }

    fn next(self) {
        let st = read(self.state);
        let (tok_e, en) = recv(join(), self.en_r);
        let (tok_a, adc_in) = recv(join(), self.adc_in_r);
        let tok = join(tok_e, tok_a);
        let (next_st, step_eff, emit_params, params) = adc_ramp_step(st, en, adc_in);
        send(tok, self.step_s, step_eff);
        send_if(tok, self.params_s, emit_params, params);
        write(self.state, next_st);
    }
}

#[test]
fn adc_ramp_step_test() {
    let init_st = AdcState { step: u4:0, done: u4:0, vals: u4[4]:[0, 0, 0, 0] };
    let targets = u4[4]:[2, 5, 8, 11];
    let (_, _, last_vld, last_packed) =
        for (i, (st, _, _, _)): (u32, (AdcState, u4, bool, u16)) in u32:0..u32:16 {
            let (next_st, step_out, vld, packed) = adc_ramp_step(st, true, targets);
            assert_eq(step_out, i as u4);
            (next_st, step_out, vld, packed)
        }((init_st, u4:0, false, u16:0));
    assert_eq(last_vld, true);
    assert_eq(last_packed, u16:0xb852);

    // Standby power-gating: en=false forces step_eff=4'hF -> all clkinv_12 outputs 0 V.
    let (_, step_off, _, _) = adc_ramp_step(init_st, false, targets);
    assert_eq(step_off, u4:15);
}

#[test]
proc adc_ramp_test {
    terminator: chan<bool> out,
    en_s: chan<bool> out,
    adc_in_s: chan<u4[4]> out,
    step_r: chan<u4> in,
    params_r: chan<u16> in,
}

impl adc_ramp_test {
    fn new(terminator: chan<bool> out) -> Self {
        let (en_s, en_r) = chan<bool>("en");
        let (adc_in_s, adc_in_r) = chan<u4[4]>("adc_in");
        let (step_s, step_r) = chan<u4>("step");
        let (params_s, params_r) = chan<u16>("params");
        adc_ramp::new(en_r, adc_in_r, step_s, params_s).spawn();
        Self { terminator, en_s, adc_in_s, step_r, params_r }
    }

    fn next(self) {
        // Sweep 1: virtual analog codes [2, 5, 8, 11] -> 0xb852
        let tok = for (i, tok): (u32, token) in u32:0..u32:16 {
            let tok = send(tok, self.en_s, true);
            let tok = send(tok, self.adc_in_s, u4[4]:[2, 5, 8, 11]);
            let (tok, got_step) = recv(tok, self.step_r);
            assert_eq(got_step, i as u4);
            tok
        }(join());
        let (tok, sw1) = recv(tok, self.params_r);
        assert_eq(sw1, u16:0xb852);

        // Sweep 2 (verifies wraparound reset + boundary codes 0 & 15): [15, 0, 7, 3] -> 0x370f
        let tok = for (i, tok): (u32, token) in u32:0..u32:16 {
            let tok = send(tok, self.en_s, true);
            let tok = send(tok, self.adc_in_s, u4[4]:[15, 0, 7, 3]);
            let (tok, got_step) = recv(tok, self.step_r);
            assert_eq(got_step, i as u4);
            tok
        }(tok);
        let (tok, sw2) = recv(tok, self.params_r);
        assert_eq(sw2, u16:0x370f);
        send(tok, self.terminator, true);
    }
}
