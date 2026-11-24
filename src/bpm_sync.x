// Copyright 2026 Google LLC.
// SPDX-License-Identifier: Apache-2.0

// 32.768 kHz Bresenham BPM / Tap-Tempo Sync.
// Measures 32.768 kHz clock ticks between rising edges of synchronized bpm_in
// (clamped >= 1024 ticks) and emits 1024 sample_tick pulses per measured period.

#![feature(explicit_state_access)]

pub struct BpmState {
    prev_bpm: bool,
    tick_cnt: u16,
    period_m: u16,
    acc: u16,
    params: u16,
}

pub const BPM_INIT_STATE = BpmState {
    prev_bpm: false,
    tick_cnt: u16:0,
    period_m: u16:4096,
    acc: u16:0,
    params: u16:0x0a2a,
};

pub fn bpm_sync_step(
    st: BpmState,
    bpm_in: bool,
    params_in: u16,
    params_vld: bool,
) -> (BpmState, bool, u16) {
    let params = if params_vld { params_in } else { st.params };
    let rising = bpm_in && !st.prev_bpm;
    let measured = if rising && (st.tick_cnt >= u16:1024) { st.tick_cnt } else { st.period_m };
    let next_tick_cnt = if rising {
        u16:1
    } else if st.tick_cnt == u16:65535 {
        u16:65535
    } else {
        st.tick_cnt + u16:1
    };
    let next_acc = (st.acc as u17) + u17:1024;
    let sample_tick = next_acc >= (measured as u17);
    let acc = (if sample_tick { next_acc - (measured as u17) } else { next_acc }) as u16;
    (
        BpmState { prev_bpm: bpm_in, tick_cnt: next_tick_cnt, period_m: measured, acc, params },
        sample_tick,
        params,
    )
}

pub proc bpm_sync {
    bpm_r: chan<bool> in,
    adc_params_r: chan<u16> in,
    beat_params_s: chan<u16> out,
    state: BpmState,
}

impl bpm_sync {
    pub fn new(
        bpm_r: chan<bool> in,
        adc_params_r: chan<u16> in,
        beat_params_s: chan<u16> out,
    ) -> Self {
        Self { bpm_r, adc_params_r, beat_params_s, state: BPM_INIT_STATE }
    }

    fn next(self) {
        let st = read(self.state);
        let (tok_b, bpm_in) = recv(join(), self.bpm_r);
        let (tok_p, p_in, p_vld) = recv_non_blocking(join(), self.adc_params_r, st.params);
        let tok = join(tok_b, tok_p);
        let (next_st, sample_tick, p_out) = bpm_sync_step(st, bpm_in, p_in, p_vld);
        send_if(tok, self.beat_params_s, sample_tick, p_out);
        write(self.state, next_st);
    }
}

#[test]
fn bpm_sync_step_test() {
    // Default 4096-clock period (120 BPM) emits exactly 1024 sample ticks over 4096 clocks.
    let (st, ticks) = for (_, (st, cnt)): (u32, (BpmState, u32)) in u32:0..u32:4096 {
        let (next_st, tick, _) = bpm_sync_step(st, false, u16:0, false);
        (next_st, cnt + (tick as u32))
    }((BPM_INIT_STATE, u32:0));
    assert_eq(ticks, u32:1024);

    // Re-lock to 2048-clock period (240 BPM) via two rising edges 2048 clocks apart.
    let st = for (cyc, st): (u32, BpmState) in u32:0..u32:2049 {
        let bpm_hi = (cyc == u32:0) || (cyc == u32:2048);
        let (next_st, _, _) = bpm_sync_step(st, bpm_hi, u16:0x5678, true);
        next_st
    }(st);
    assert_eq(st.period_m, u16:2048);
    assert_eq(st.params, u16:0x5678);

    // Sub-minimum glitch edge (2 ticks < 1024) must not overwrite period_m.
    let (st, _, _) = bpm_sync_step(st, false, u16:0, false);
    let (st, _, _) = bpm_sync_step(st, true, u16:0, false);
    assert_eq(st.period_m, u16:2048);

    let (_, relock_ticks) = for (_, (st, cnt)): (u32, (BpmState, u32)) in u32:0..u32:2048 {
        let (next_st, tick, _) = bpm_sync_step(st, false, u16:0, false);
        (next_st, cnt + (tick as u32))
    }((st, u32:0));
    assert_eq(relock_ticks, u32:1024);
}
