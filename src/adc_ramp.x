// Copyright 2026 Google LLC.
// SPDX-License-Identifier: Apache-2.0

// 4-channel x 4-bit In-Place Crossing-Latch Ramp ADC Controller (24 FFs total).
// Drives step_s[3:0] (combinational via flop_outputs=false) into 4x sc_adc_ramp_1ch_4b
// and latches each channel in-place on the single clock where cmp[i] transitions 1 -> 0.

pub struct AdcState {
    step: u4,
    done: u4,
    v0: u4,
    v1: u4,
    v2: u4,
    v3: u4,
}

pub fn adc_ramp_step(st: AdcState, cmp: u4) -> (AdcState, u4, bool, u16) {
    let is_zero = st.step == u4:0;
    let is_last = st.step == u4:15;

    let d0 = !is_zero && (((st.done >> u4:0) & u4:1) == u4:1);
    let cmp0 = ((cmp >> u4:0) & u4:1) == u4:1;
    let t0 = !d0 && (!cmp0 || is_last);
    let v0 = if t0 { st.step } else { st.v0 };

    let d1 = !is_zero && (((st.done >> u4:1) & u4:1) == u4:1);
    let cmp1 = ((cmp >> u4:1) & u4:1) == u4:1;
    let t1 = !d1 && (!cmp1 || is_last);
    let v1 = if t1 { st.step } else { st.v1 };

    let d2 = !is_zero && (((st.done >> u4:2) & u4:1) == u4:1);
    let cmp2 = ((cmp >> u4:2) & u4:1) == u4:1;
    let t2 = !d2 && (!cmp2 || is_last);
    let v2 = if t2 { st.step } else { st.v2 };

    let d3 = !is_zero && (((st.done >> u4:3) & u4:1) == u4:1);
    let cmp3 = ((cmp >> u4:3) & u4:1) == u4:1;
    let t3 = !d3 && (!cmp3 || is_last);
    let v3 = if t3 { st.step } else { st.v3 };

    let next_done = (((d0 || t0) as u4) << u4:0)
        | (((d1 || t1) as u4) << u4:1)
        | (((d2 || t2) as u4) << u4:2)
        | (((d3 || t3) as u4) << u4:3);
    let packed: u16 = v3 ++ v2 ++ v1 ++ v0;
    let next_st = AdcState { step: st.step + u4:1, done: next_done, v0, v1, v2, v3 };
    (next_st, st.step, is_last, packed)
}

pub proc adc_ramp {
    cmp_r: chan<u4> in;
    step_s: chan<u4> out;
    params_s: chan<u16> out;

    init { AdcState { step: u4:0, done: u4:0, v0: u4:0xa, v1: u4:0x2, v2: u4:0xa, v3: u4:0x0 } }

    config(cmp_r: chan<u4> in, step_s: chan<u4> out, params_s: chan<u16> out) {
        (cmp_r, step_s, params_s)
    }

    next(st: AdcState) {
        let tok = token();
        let (tok, cmp, _) = recv_non_blocking(tok, cmp_r, u4:0);
        let (next_st, step_out, emit_params, packed) = adc_ramp_step(st, cmp);
        let tok_s = send(tok, step_s, step_out);
        let tok_p = send_if(tok, params_s, emit_params, packed);
        join(tok_s, tok_p);
        next_st
    }
}

#[test]
fn adc_ramp_step_test() {
    let init_st = AdcState { step: u4:0, done: u4:0, v0: u4:0, v1: u4:0, v2: u4:0, v3: u4:0 };
    // Virtual analog inputs corresponding to codes (v0=2, v1=5, v2=8, v3=11) -> 0xb852
    let (_, _, last_vld, last_packed) = for (i, (st, _, _, _)): (u32, (AdcState, u4, bool, u16)) in u32:0..u32:16 {
        let cmp = ((i < u32:2) as u4)
            | (((i < u32:5) as u4) << u4:1)
            | (((i < u32:8) as u4) << u4:2)
            | (((i < u32:11) as u4) << u4:3);
        let (next_st, step_out, vld, packed) = adc_ramp_step(st, cmp);
        assert_eq(step_out, i as u4);
        (next_st, step_out, vld, packed)
    }((init_st, u4:0, false, u16:0));
    assert_eq(last_vld, true);
    assert_eq(last_packed, u16:0xb852);
}
