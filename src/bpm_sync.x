// Copyright 2026 Google LLC.
// SPDX-License-Identifier: Apache-2.0

// 32.768 kHz Bresenham BPM / Tap-Tempo Sync.
// Measures 32.768 kHz clock ticks between rising edges of synchronized bpm_in
// (clamped >= 1024 ticks) and emits 1024 sample_tick pulses per measured period.

pub struct BpmState {
    prev_bpm: bool,
    tick_cnt: u16,
    period_m: u16,
    acc: u16,
    params: u16,
}

pub fn bpm_sync_step(
    st: BpmState, bpm_in: bool, params_in: u16, params_vld: bool
) -> (BpmState, bool, u16) {
    let params = if params_vld { params_in } else { st.params };
    let rising = bpm_in && !st.prev_bpm;
    let measured = if rising && (st.tick_cnt >= u16:1024) { st.tick_cnt } else { st.period_m };
    let next_tick_cnt = if rising {
        u16:1
    } else {
        if st.tick_cnt == u16:65535 { u16:65535 } else { st.tick_cnt + u16:1 }
    };
    let next_acc = (st.acc as u17) + u17:1024;
    let sample_tick = next_acc >= (measured as u17);
    let acc = (if sample_tick { next_acc - (measured as u17) } else { next_acc }) as u16;
    (BpmState { prev_bpm: bpm_in, tick_cnt: next_tick_cnt, period_m: measured, acc, params }, sample_tick, params)
}

pub proc bpm_sync {
    bpm_r: chan<bool> in;
    adc_params_r: chan<u16> in;
    beat_params_s: chan<u16> out;

    init {
        BpmState {
            prev_bpm: false,
            tick_cnt: u16:0,
            period_m: u16:4096,
            acc: u16:0,
            params: u16:0x0a2a,
        }
    }

    config(bpm_r: chan<bool> in, adc_params_r: chan<u16> in, beat_params_s: chan<u16> out) {
        (bpm_r, adc_params_r, beat_params_s)
    }

    next(st: BpmState) {
        let tok = token();
        let (tok_b, bpm_in, _) = recv_non_blocking(tok, bpm_r, false);
        let (tok_p, p_in, p_vld) = recv_non_blocking(tok, adc_params_r, st.params);
        let tok = join(tok_b, tok_p);
        let (next_st, sample_tick, p_out) = bpm_sync_step(st, bpm_in, p_in, p_vld);
        send_if(tok, beat_params_s, sample_tick, p_out);
        next_st
    }
}

#[test]
fn bpm_sync_step_test() {
    let init_st = BpmState {
        prev_bpm: false,
        tick_cnt: u16:0,
        period_m: u16:4096,
        acc: u16:0,
        params: u16:0x1234,
    };
    // Default 4096-clock period (120 BPM) emits 1 sample_tick every 4 clocks -> 16 ticks in 64 clocks
    let (_, ticks) = for (_, (st, cnt)): (u32, (BpmState, u32)) in u32:0..u32:64 {
        let (next_st, tick, _) = bpm_sync_step(st, false, u16:0, false);
        (next_st, cnt + (tick as u32))
    }((init_st, u32:0));
    assert_eq(ticks, u32:16);
}
