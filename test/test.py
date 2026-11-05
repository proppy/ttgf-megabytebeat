# Copyright 2025 Google LLC.
# SPDX-License-Identifier: Apache-2.0

import difflib
import filecmp
import os
from pathlib import Path
import pprint
import wave

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge


SAMPLE_RATE = 8000
PWM_PERIOD = 256
FREQ = SAMPLE_RATE * PWM_PERIOD
PERIOD_PS = 488280
SIM_SUFFIX = "glsim" if os.environ.get("GATES") == "yes" else "sim"


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


def golden_sample(t: int, a: int, b: int, c: int, d: int) -> list[int]:
    return [fn(t, a, b, c, d) for fn in FORMULAS]


def write_wav(path: Path, samples: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(1)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(samples)


async def collect_raw_samples(dut, num_cycles: int):
    for _ in range(num_cycles):
        await RisingEdge(dut.clk)
        assert dut.uo_out.value.is_resolvable
        yield int(dut.uo_out.value)


async def collect_samples(dut, num_samples: int) -> list[list[int]]:
    raw = [val async for val in collect_raw_samples(dut, num_samples * PWM_PERIOD)]
    # count high cycles over each 256-clock period to recover the PWM duty cycle (8-bit PCM sample)
    return [
        [
            sum((val >> ch) & 1 for val in raw[i : i + PWM_PERIOD])
            for ch in range(len(FORMULAS))
        ]
        for i in range(0, len(raw), PWM_PERIOD)
    ]


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
    # advance to the first rising edge where rst_n == 1 is sampled (start of the first 256-clock PWM period)
    await ClockCycles(dut.clk, 1)

    num_samples = 16
    samples = await collect_samples(dut, num_samples)

    expected = [golden_sample(t, a, b, c, d) for t in range(num_samples)]
    assert samples == expected, "\n" + "\n".join(
        list(
            difflib.unified_diff(
                pprint.pformat(expected).splitlines(),
                pprint.pformat(samples).splitlines(),
                fromfile="golden",
                tofile=SIM_SUFFIX,
                lineterm="",
            )
        )[:32]
    )


@cocotb.test()
async def test_wav(dut):
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

    dut.rst_n.value = 1 # take out of reset
    # advance to the first rising edge where rst_n == 1 is sampled (start of the first 256-clock PWM period)
    await ClockCycles(dut.clk, 1)

    num_samples = int(os.environ.get("NUM_SAMPLES", str(8 * SAMPLE_RATE)))
    samples = await collect_samples(dut, num_samples)

    expected = [golden_sample(t, a, b, c, d) for t in range(num_samples)]
    out_dir = Path(__file__).resolve().parent / "output"
    for ch, fn in enumerate(FORMULAS):
        if fn is not silence:
            write_wav(out_dir / f"{fn.__name__}_{SIM_SUFFIX}.wav", bytes([s[ch] for s in samples]))
            write_wav(out_dir / f"{fn.__name__}_golden.wav", bytes([s[ch] for s in expected]))

    for fn in FORMULAS:
        if fn is not silence:
            wav_path = out_dir / f"{fn.__name__}_{SIM_SUFFIX}.wav"
            golden_path = out_dir / f"{fn.__name__}_golden.wav"
            assert filecmp.cmp(wav_path, golden_path, shallow=False), (
                f"{wav_path.name} differs from {golden_path.name}"
            )
