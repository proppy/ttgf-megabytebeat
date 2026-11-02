# Copyright 2025 Google LLC.
# SPDX-License-Identifier: Apache-2.0

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles


FREQ = 8000 * 256

@cocotb.test()
async def test_bytebeat(dut):
    dut._log.info("Start")

    # Set the clock to ~2048000hz (8000 * 256)
    clock = Clock(dut.clk, 488280, unit="ps")
    cocotb.start_soon(clock.start())

    dut.ena.value = 1 # enable project
    dut.rst_n.value = 0 # low to reset
    # set "interesting" input parameters
    dut.ui_in.value = 0x15  # a=5 b=1
    dut.uio_in.value = 0x03 # c=3 d=0
    # TODO(#1): Remove pre-reset clock cycles once synchronous reset starvation on !clk_8[7] is fixed.
    await ClockCycles(dut.clk, 10)
    dut.rst_n.value = 1
    await ClockCycles(dut.clk, 130)
    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 512)
    assert dut.uio_oe.value == 0
    assert dut.uio_out.value == 0

    dut.rst_n.value = 1 # take out of reset
    await ClockCycles(dut.clk, 2)

    events = [0] * 8
    prev = int(dut.uo_out.value)

    for _ in range(16 * 256):
        await ClockCycles(dut.clk, 1)
        assert dut.uo_out.value.is_resolvable
        val = int(dut.uo_out.value)
        diff = val ^ prev
        for ch in range(8):
            events[ch] += (diff >> ch) & 1
        prev = val

    assert events == [14, 32, 27, 18, 0, 0, 0, 30]
