# Copyright 2025 Google LLC.
# SPDX-License-Identifier: Apache-2.0

import os
from pathlib import Path
import wave

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, Timer, ValueChange
from cocotb.utils import get_sim_time


SAMPLE_RATE = 8000
PWM_PERIOD = 256
FREQ = SAMPLE_RATE * PWM_PERIOD
PERIOD_PS = 488280
HALF_PERIOD_PS = PERIOD_PS // 2
CHANNELS = {
    0: "the42melody",
    1: "fractaltrees",
    2: "untitleddroid",
    3: "atunetoshare",
    7: "sierpinskiharmony",
}


def golden_sample(ch: int, t: int, a: int, b: int, c: int, d: int) -> int:
    t &= 0xFFFFFFFF
    ab = (b << 4) | a
    cd = (d << 4) | c
    if ch == 0:
        s = t * (ab & (t >> cd))
    elif ch == 1:
        ab_nz = 1 if ab == 0 else ab
        s = t | (t % ab_nz) | (t % (cd + 2))
    elif ch == 2:
        cond = (t >> a) & b
        s = (t >> c) if cond != 0 else (((~t + 1) & 0xFFFFFFFF) >> d)
    elif ch == 3:
        s = (t * ((t >> a) | (t >> b))) & cd
    elif ch == 7:
        s = ((t * a) & (t >> b)) | ((t * c) & (t >> d))
    else:
        s = 0
    return s & 0xFF


def write_wav(path: Path, samples: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(1)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(samples)


@cocotb.test()
async def test_bytebeat(dut):
    dut._log.info("Start")

    # Set the clock to ~2048000hz (8000 * 256)
    clock = Clock(dut.clk, PERIOD_PS, unit="ps", impl="gpi")
    cocotb.start_soon(clock.start())

    a, b, c, d = 0x5, 0x1, 0x3, 0x0
    dut.ena.value = 1 # enable project
    dut.rst_n.value = 0 # low to reset
    # set "interesting" input parameters
    dut.ui_in.value = (b << 4) | a   # 0x15: a=5 b=1
    dut.uio_in.value = (d << 4) | c  # 0x03: c=3 d=0
    # wait 3 cycles so rst_n propagates past t=0 and non-blocking/unit-delay assignments take effect
    await ClockCycles(dut.clk, 3)
    assert dut.uio_oe.value == 0
    assert dut.uio_out.value == 0

    dut.rst_n.value = 1 # take out of reset
    # advance to the first rising edge where rst_n == 1 is sampled (count == 0)
    await ClockCycles(dut.clk, 1)
    edge0_ps = int(get_sim_time("ps"))
    # step to the midpoint of cycle 0 so NBA / gate-level delays have settled
    await Timer(HALF_PERIOD_PS, unit="ps")

    num_samples = int(os.environ.get("NUM_SAMPLES", str(8 * SAMPLE_RATE)))
    total_cycles = num_samples * PWM_PERIOD
    assert dut.uo_out.value.is_resolvable
    transitions = [(0, int(dut.uo_out.value))]

    async def monitor_uo_out():
        val_change = ValueChange(dut.uo_out)
        while True:
            await val_change
            assert dut.uo_out.value.is_resolvable
            c_idx = (int(get_sim_time("ps")) - edge0_ps + HALF_PERIOD_PS) // PERIOD_PS
            val = int(dut.uo_out.value)
            if transitions[-1][0] == c_idx:
                transitions[-1] = (c_idx, val)
            else:
                transitions.append((c_idx, val))

    monitor_task = cocotb.start_soon(monitor_uo_out())
    await Timer(total_cycles * PERIOD_PS, unit="ps")
    monitor_task.cancel()
    if transitions[-1][0] < total_cycles:
        transitions.append((total_cycles, transitions[-1][1]))

    pcm_sim = {ch: bytearray(num_samples) for ch in range(8)}
    for i in range(len(transitions) - 1):
        c0, val = transitions[i]
        c1, _ = transitions[i + 1]
        if c0 >= total_cycles or val == 0:
            continue
        if c1 > total_cycles:
            c1 = total_cycles
        cur = c0
        while cur < c1:
            t = cur // PWM_PERIOD
            frame_end = (t + 1) * PWM_PERIOD
            nxt = c1 if c1 < frame_end else frame_end
            dk = nxt - cur
            for ch in range(8):
                if (val >> ch) & 1:
                    pcm_sim[ch][t] += dk
            cur = nxt

    out_dir = Path(__file__).resolve().parent / "output"
    for ch in range(8):
        sim_bytes = bytes(pcm_sim[ch])
        if ch in CHANNELS:
            name = CHANNELS[ch]
            write_wav(out_dir / f"{name}.wav", sim_bytes)
            expected_bytes = bytes(
                golden_sample(ch, t, a, b, c, d) for t in range(num_samples)
            )
            assert sim_bytes == expected_bytes, (
                f"Channel {ch} ({name}) mismatch: "
                f"got {list(sim_bytes[:16])}, expected {list(expected_bytes[:16])}"
            )
        else:
            assert sim_bytes == bytes(num_samples), (
                f"Unused channel {ch} non-zero: {list(sim_bytes[:16])}"
            )
