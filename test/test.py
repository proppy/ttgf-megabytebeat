# Copyright 2025 Google LLC.
# SPDX-License-Identifier: Apache-2.0

import difflib
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


def the42melody(t: int, a: int, b: int, c: int, d: int) -> int:
    ab = (b << 4) | a
    cd = (d << 4) | c
    return (t * (ab & (t >> cd))) & 0xFF


def fractaltrees(t: int, a: int, b: int, c: int, d: int) -> int:
    ab = (b << 4) | a
    ab_nz = 1 if ab == 0 else ab
    cd = (d << 4) | c
    return (t | (t % ab_nz) | (t % (cd + 2))) & 0xFF


def untitleddroid(t: int, a: int, b: int, c: int, d: int) -> int:
    cond = (t >> a) & b
    s = (t >> c) if cond != 0 else (((~t + 1) & 0xFFFFFFFF) >> d)
    return s & 0xFF


def atunetoshare(t: int, a: int, b: int, c: int, d: int) -> int:
    cd = (d << 4) | c
    return (t * ((t >> a) | (t >> b)) & cd) & 0xFF


def sierpinskiharmony(t: int, a: int, b: int, c: int, d: int) -> int:
    return (((t * a) & (t >> b)) | ((t * c) & (t >> d))) & 0xFF


def silence(t: int, a: int, b: int, c: int, d: int) -> int:
    return 0


FORMULAS = [
    the42melody,
    fractaltrees,
    untitleddroid,
    atunetoshare,
    silence,
    silence,
    silence,
    sierpinskiharmony,
]


def golden_samples(num_samples: int, a: int, b: int, c: int, d: int) -> list[bytes]:
    return [
        bytes(fn(t & 0xFFFFFFFF, a, b, c, d) for t in range(num_samples))
        for fn in FORMULAS
    ]


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
    for ch, fn in enumerate(FORMULAS):
        if fn is not silence:
            write_wav(out_dir / f"{fn.__name__}.wav", bytes(pcm_sim[ch]))

    expected = golden_samples(num_samples, a, b, c, d)
    for ch, fn in enumerate(FORMULAS):
        sim_bytes = bytes(pcm_sim[ch])
        expected_bytes = expected[ch]
        if sim_bytes != expected_bytes:
            diff = "\n".join(
                list(
                    difflib.unified_diff(
                        [f"t={t}: {b}" for t, b in enumerate(expected_bytes)],
                        [f"t={t}: {b}" for t, b in enumerate(sim_bytes)],
                        fromfile="expected",
                        tofile="got",
                        lineterm="",
                    )
                )[:32]
            )
            assert sim_bytes == expected_bytes, (
                f"channel {ch} ({fn.__name__}) mismatch:\n{diff}"
            )
