# SPDX-FileCopyrightText: © 2026 GNSS AFE + megabytebeat Authors
# SPDX-License-Identifier: Apache-2.0
#
# Full-chip cocotb testbench for slot 0p5x1 dual-VDD GNSS AFE + proppy/ttgf-megabytebeat.
# Tests both RTL (GL=0) and Gate-Level (GL=1, powered megabytebeat_macro.pnl.v):
#   1. GNSS SPI reset default (ID=10101, ctl=19'h08000), write & readback.
#   2. megabytebeat bb_ena=0 quiet-by-default clamping (PWM == 0 and all 5 R-2R DACs == 0x00).
#   3. megabytebeat bb_ena=1 bit-exact 3-track PWM + 5-track 8-bit R-2R DAC verification
#      (both external bb_ua_PAD[4:0] connectivity and internal 8-bit ladder codes).
#   4. megabytebeat bb_ena 1->0 clean disable & DAC clamping.

import os
import re
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.handle import HierarchyArrayObject, HierarchyObject
from cocotb.triggers import ClockCycles, FallingEdge, RisingEdge, Timer
from cocotb_tools.runner import get_runner

sim = os.getenv("SIM", "icarus")
gl = bool(int(os.getenv("GL", "0")))
pdk_root = os.getenv("PDK_ROOT", str(Path(__file__).resolve().parent / "../gf180mcu"))
pdk = os.getenv("PDK", "gf180mcuD")
scl = os.getenv("SCL", "gf180mcu_fd_sc_mcu7t5v0")
pad = os.getenv("PAD", "gf180mcu_fd_io")
sram = os.getenv("SRAM", "gf180mcu_fd_ip_sram")
slot = os.getenv("SLOT", "0p5x1")

hdl_toplevel = "chip_top"


def bytebeat_golden(t: int, a: int, b: int, c: int, d: int) -> list[int]:
    """Exact golden formulas from proppy/ttgf-megabytebeat src/bytebeat.x."""
    t32 = t & 0xFFFFFFFF
    ab = ((b & 0xF) << 4) | (a & 0xF)
    cd = ((d & 0xF) << 4) | (c & 0xF)

    # Track 0: the 42 melody
    sh0 = (t32 >> cd) if cd < 32 else 0
    s0 = (t32 * (ab & sh0)) & 0xFF

    # Track 1: fractal trees
    ab_nz = 1 if ab == 0 else ab
    cd_257 = cd + 2
    s1 = (t32 | (t32 % ab_nz) | (t32 % cd_257)) & 0xFF

    # Track 2: untitled (droid)
    cond0 = (t32 >> a) & b
    p0 = t32 >> c
    p1 = ((-t32) & 0xFFFFFFFF) >> d
    s2 = (p0 if cond0 != 0 else p1) & 0xFF

    # Track 3: a tune to share
    s3 = (t32 * ((t32 >> a) | (t32 >> b)) & cd) & 0xFF

    # Track 7 (ua[4]): sierpinski harmony
    s7 = (((t32 * a) & (t32 >> b)) | ((t32 * c) & (t32 >> d))) & 0xFF

    return [s0, s1, s2, s3, s7]


def _dac_inst_sort_key(path: str) -> tuple[int, str]:
    m = re.search(r"call(?:__(\d+))?_inst", path)
    if m:
        return (int(m.group(1)) if m.group(1) else 0, path)
    return (999, path)


def _find_r2r_ladders(root) -> list[dict]:
    """Locate all 5 passive 8-bit `r2r_ladder_8bit` instances in RTL or GL hierarchy."""
    found: list[tuple[str, dict]] = []

    def walk(obj, prefix: str = "") -> None:
        if isinstance(obj, HierarchyArrayObject):
            for sub in obj:
                walk(sub, prefix)
            return
        if not isinstance(obj, HierarchyObject):
            return
        name = obj._name.lstrip("\\").strip()
        full = f"{prefix}.{name}" if prefix else name
        if name == "u_r2r" or name.endswith(".u_r2r"):
            children = {c._name.strip(): c for c in obj}
            if "in" in children and "term" in children and len(children["in"]) == 8:
                found.append((full, children))
            return
        for sub in obj:
            if isinstance(sub, (HierarchyObject, HierarchyArrayObject)):
                walk(sub, full)

    walk(root)
    found.sort(key=lambda item: _dac_inst_sort_key(item[0]))
    return [pins for _, pins in found]


def _read_dac_codes(ladders: list[dict]) -> list[int]:
    codes = []
    for pins in ladders:
        assert int(pins["term"].value) == 0, f"Expected R-2R term=0, got {pins['term'].value}"
        codes.append(int(pins["in"].value))
    return codes


async def init_chip(dut):
    if gl:
        for p, v in [
            ("VDD", 1),
            ("DVDD", 1),
            ("VDD_BB", 1),
            ("DVDD_BB", 1),
            ("VDDA", 1),
            ("VDDV", 1),
            ("VSS", 0),
            ("DVSS", 0),
            ("VSSA", 0),
        ]:
            if hasattr(dut, p):
                getattr(dut, p).value = v

    dut.clk_PAD.value = 0
    dut.rst_n_PAD.value = 0
    dut.input_PAD.value = 0b10  # CS_N=1, SDI=0

    dut.bb_clk_PAD.value = 0
    dut.bb_rst_n_PAD.value = 0
    dut.bb_ena_PAD.value = 0
    dut.bb_ui_PAD.value = 0
    dut.bb_uio_PAD.value = 0

    await Timer(500, unit="ns")
    dut.rst_n_PAD.value = 1
    dut.bb_rst_n_PAD.value = 1
    await Timer(500, unit="ns")


async def spi_transfer(dut, tx_word: int) -> int:
    """24-bit SPI mode 0 transaction: MSB first, sample SDO (bidir_PAD[5]) on rising SCLK."""
    rx = 0
    dut.input_PAD.value = 0b00  # CS_N=0
    await Timer(100, unit="ns")
    for bit in range(23, -1, -1):
        sdi = (tx_word >> bit) & 1
        dut.input_PAD.value = sdi  # CS_N=0, SDI=sdi
        await Timer(100, unit="ns")
        dut.clk_PAD.value = 1
        await Timer(50, unit="ns")
        sdo = int(dut.bidir_PAD.value[5])
        rx = (rx << 1) | sdo
        await Timer(50, unit="ns")
        dut.clk_PAD.value = 0
        await Timer(50, unit="ns")
    dut.input_PAD.value = 0b10  # CS_N=1 (latches sr[18:0] -> ctl)
    await Timer(200, unit="ns")
    return rx


@cocotb.test()
async def test_gnss_spi_and_megabytebeat(dut):
    """Verify GNSS SPI + megabytebeat bb_ena gating + 3-track PWM + 5-track 8-bit R-2R DACs."""
    await init_chip(dut)

    ladders = _find_r2r_ladders(dut)
    assert len(ladders) == 5, f"Expected 5 r2r_ladder_8bit instances, found {len(ladders)}"

    # ------------------------------------------------------------ 1. GNSS SPI test
    # First frame reads back reset default {ID=5'b10101, ctl=19'h08000} = 0xA88000
    w1 = 0x05A3C5  # ctl = 19'h5A3C5
    rd0 = await spi_transfer(dut, w1)
    assert rd0 == 0xA88000, f"Expected reset readback 0xA88000, got 0x{rd0:06X}"

    # Second frame reads back {5'b10101, 19'h5A3C5} = 0xADA3C5
    w2 = 0x02F10A
    rd1 = await spi_transfer(dut, w2)
    expected_rd1 = (0b10101 << 19) | (w1 & 0x7FFFF)
    assert rd1 == expected_rd1, f"Expected SPI readback 0x{expected_rd1:06X}, got 0x{rd1:06X}"
    cocotb.log.info(f"GNSS SPI PASS: reset=0x{rd0:06X}, frame1=0x{rd1:06X}")

    # ------------------------------------------------------------ 2. megabytebeat bb_ena=0 test
    bb_clk = Clock(dut.bb_clk_PAD, 488, unit="ns")  # ~2.048 MHz
    cocotb.start_soon(bb_clk.start())

    # Set active params (a=0xa, b=0x2, c=0xa, d=0x0) while bb_ena=0
    dut.bb_ui_PAD.value = 0x2A
    dut.bb_uio_PAD.value = 0x0A
    dut.bb_ena_PAD.value = 0
    await ClockCycles(dut.bb_clk_PAD, 300)
    for _ in range(32):
        await RisingEdge(dut.bb_clk_PAD)
        await Timer(10, unit="ns")
        assert int(dut.bb_uo_PAD.value) == 0, (
            f"Expected PWM=0 with bb_ena=0, got {dut.bb_uo_PAD.value}"
        )
        assert int(dut.bb_ua_PAD.value) == 0, (
            f"Expected DAC=0 with bb_ena=0, got {dut.bb_ua_PAD.value}"
        )
        assert _read_dac_codes(ladders) == [0, 0, 0, 0, 0], (
            f"Expected clamped R-2R codes [0..0] with bb_ena=0, got {_read_dac_codes(ladders)}"
        )
    cocotb.log.info("megabytebeat bb_ena=0 quiet PWM + DAC clamping PASS")

    # ------------------------------------------------------------ 3. megabytebeat bb_ena=1 5-DAC + 3-PWM test
    # Enable on falling edge so first posedge sees clean ena=1, rst_n=1
    await FallingEdge(dut.bb_clk_PAD)
    dut.bb_rst_n_PAD.value = 0
    dut.bb_ena_PAD.value = 1
    await ClockCycles(dut.bb_clk_PAD, 4)
    await FallingEdge(dut.bb_clk_PAD)
    dut.bb_rst_n_PAD.value = 1

    # Test with params a=0x5, b=0x2, c=0x3, d=0x1 (all 5 tracks produce distinct non-zero samples!)
    a, b, c, d = 0x5, 0x2, 0x3, 0x1
    dut.bb_ui_PAD.value = (b << 4) | a
    dut.bb_uio_PAD.value = (d << 4) | c

    mismatches = 0
    for t_idx in range(12):
        counts = [0, 0, 0]
        dac_codes = None
        ua_pad_val = None
        for cyc in range(256):
            await RisingEdge(dut.bb_clk_PAD)
            await Timer(20, unit="ns")
            val = int(dut.bb_uo_PAD.value)
            for ch in range(3):
                counts[ch] += (val >> ch) & 1
            if cyc == 128:
                dac_codes = _read_dac_codes(ladders)
                ua_pad_val = int(dut.bb_ua_PAD.value)
        exp = bytebeat_golden(t_idx, a, b, c, d)
        exp_ua_msb = sum(((exp[ch] >> 7) & 1) << ch for ch in range(5))
        cocotb.log.info(
            f"t={t_idx}: pwm={counts} (exp={exp[:3]}), dac={dac_codes} (exp={exp}), "
            f"ua_pad=0x{ua_pad_val:02x} (exp_msb=0x{exp_ua_msb:02x})"
        )
        if counts != exp[:3] or dac_codes != exp or ua_pad_val != exp_ua_msb:
            mismatches += 1
    assert mismatches == 0, f"megabytebeat PWM/DAC mismatches: {mismatches}"
    cocotb.log.info("megabytebeat 3-PWM + 5x 8-bit R-2R DAC bit-exact PASS (t=0..11)")

    # ------------------------------------------------------------ 4. bb_ena 1 -> 0 disable check
    await FallingEdge(dut.bb_clk_PAD)
    dut.bb_ena_PAD.value = 0
    await ClockCycles(dut.bb_clk_PAD, 10)
    await Timer(20, unit="ns")
    assert int(dut.bb_uo_PAD.value) == 0, (
        f"Expected PWM=0 after bb_ena=0, got {dut.bb_uo_PAD.value}"
    )
    assert int(dut.bb_ua_PAD.value) == 0, (
        f"Expected DAC=0 after bb_ena=0, got {dut.bb_ua_PAD.value}"
    )
    assert _read_dac_codes(ladders) == [0, 0, 0, 0, 0], (
        f"Expected clamped DAC codes [0..0] after bb_ena=0, got {_read_dac_codes(ladders)}"
    )
    cocotb.log.info("megabytebeat bb_ena 1->0 disable + DAC clamp PASS")


def chip_top_runner():
    proj_path = Path(__file__).resolve().parent
    sources = []
    defines = {f"SLOT_{slot.upper()}": True, "SIM": True}
    includes = [proj_path / "../src/"]

    defines[f"PDK_{pdk.replace('-', '_')}"] = True
    defines[f"SCL_{scl}"] = True
    defines[f"PAD_{pad}"] = True
    defines[f"SRAM_{sram}"] = True

    if gl:
        sources.append(Path(pdk_root) / pdk / "libs.ref" / scl / "verilog" / f"{scl}.v")
        sources.append(Path(pdk_root) / pdk / "libs.ref" / scl / "verilog" / "primitives.v")
        sources.append(proj_path / "../macros/megabytebeat/src/r2r_ladder_8bit.v")
        sources.append(proj_path / "../macros/megabytebeat/final/pnl/megabytebeat_macro.pnl.v")
        chip_pnl = proj_path / f"../final/pnl/{hdl_toplevel}.pnl.v"
        if chip_pnl.is_file() and "bb_ua_PAD" in chip_pnl.read_text(
            encoding="utf-8", errors="ignore"
        ):
            sources.append(chip_pnl)
        else:
            sources += [
                proj_path / "../src/chip_top.sv",
                proj_path / "../src/chip_core.sv",
            ]
        defines.update({"FUNCTIONAL": True, "USE_POWER_PINS": True})
    else:
        sources += [
            proj_path / "../src/chip_top.sv",
            proj_path / "../src/chip_core.sv",
            proj_path / "../macros/megabytebeat/src/r2r_ladder_8bit.v",
            proj_path / "../macros/megabytebeat/src/sc_r2r_dac_8bit.v",
            proj_path / "../macros/megabytebeat/src/megabytebeat.v",
            proj_path / "../macros/megabytebeat/src/pwm_audio.v",
            proj_path / "../macros/megabytebeat/src/tt_um_proppy_megabytebeat.v",
            proj_path / "../macros/megabytebeat/src/megabytebeat_macro.sv",
        ]

    sources += [
        Path(pdk_root) / pdk / f"libs.ref/{pad}/verilog/{pad}.v",
        proj_path / "../src/gnss_afe_analog.v",
        proj_path / "../ip/gf180mcu_gnss_io/vh/gf180mcu_gnss_io.v",
        proj_path / "../ip/gf180mcu_ws_ip__logo/vh/gf180mcu_ws_ip__logo.v",
        proj_path / "../ip/gf180mcu_ws_ip__marker/vh/gf180mcu_ws_ip__marker.v",
        proj_path / "../ip/gf180mcu_ws_ip__qrcode_id/vh/gf180mcu_ws_ip__qrcode_id.v",
        proj_path / "../ip/gf180mcu_ws_ip__shuttle_id/vh/gf180mcu_ws_ip__shuttle_id.v",
        proj_path / "../ip/gf180mcu_ws_ip__project_id/vh/gf180mcu_ws_ip__project_id.v",
    ]

    build_args = (
        ["-g2012"] if sim == "icarus" else ["--timing", "--trace", "--trace-fst", "--trace-structs"]
    )
    runner = get_runner(sim)
    runner.build(
        sources=sources,
        hdl_toplevel=hdl_toplevel,
        defines=defines,
        always=True,
        includes=includes,
        build_args=build_args,
        waves=False,
    )
    runner.test(
        hdl_toplevel=hdl_toplevel,
        test_module="chip_top_tb",
        waves=False,
    )


if __name__ == "__main__":
    chip_top_runner()
