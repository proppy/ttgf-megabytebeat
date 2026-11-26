# Copyright 2026 Google LLC.
# SPDX-License-Identifier: Apache-2.0
"""Cocotb Python & `cocotbext-ams` analog models for passive R-2R converters (RTL + Gate-Level).

Follows cocotb's `analog_model` pattern: auto-discovers preserved `u_r2r` macro instances
in both hierarchical RTL and flattened post-PnR Gate-Level netlists, keeping Verilog stubs
pure pass-throughs while modeling real R-2R voltages and comparator trip points in Python
(`R2RDacModel`, `R2RAdcModel`) and live BSIM3v3 + LVS/PEX `libngspice0` co-simulation
(`DacAmsBridge`, `AdcAmsBridge`, `MixedSignalAmsBridge`).
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import sys
import tempfile

import cocotb
from cocotb.handle import Force, HierarchyArrayObject, HierarchyObject, Release
from cocotb.triggers import ValueChange

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import adc  # noqa: E402
import analog  # noqa: E402
import dac  # noqa: E402
import gf180  # noqa: E402


def _natural_key(text: str) -> list[int | str]:
    norm = re.sub(r"__(\d+)_inst(?=\.|$)", r"[\1]_inst", text)
    norm = re.sub(r"(?<!\])_inst(?=\.|$)", "[0]_inst", norm)
    return [int(tok) if tok.isdigit() else tok for tok in re.split(r"(\d+)", norm)]


def find_r2r_handles(dut, width: int) -> list[tuple[str, HierarchyObject, dict]]:
    """Find all `u_r2r` macro instances whose `in` bus has bit width `width`."""
    found: list[tuple[str, HierarchyObject, dict]] = []

    def walk(obj, prefix: str = "") -> None:
        if isinstance(obj, HierarchyArrayObject):
            for sub in obj:
                walk(sub, prefix)
            return
        if not isinstance(obj, HierarchyObject):
            return
        name = obj._name.strip()
        full = f"{prefix}.{name}" if prefix else name
        if name == "u_r2r" or name.endswith(".u_r2r"):
            children = {c._name.strip(): c for c in obj}
            if "in" in children and len(children["in"]) == width:
                found.append((full, obj, children))
            return
        for sub in obj:
            if isinstance(sub, (HierarchyObject, HierarchyArrayObject)):
                walk(sub, full)

    walk(dut)
    found.sort(key=lambda item: _natural_key(item[0]))
    return found


def find_r2r_instances(dut, width: int) -> list[tuple[str, dict]]:
    """Find all `u_r2r` macro instances whose `in` bus has bit width `width`."""
    return [(full, children) for full, _, children in find_r2r_handles(dut, width)]


def _check_ams_prereqs(dut, label: str) -> Path | None:
    """Return GF180MCU `PDK_ROOT` if `cocotbext-ams` + `libngspice0` + PDK are available."""
    mode = os.environ.get("AMS_NGSPICE", "auto").strip().lower()
    if mode in ("0", "false", "no", "off"):
        return None
    require = mode in ("1", "true", "yes", "on", "require")

    def _missing(reason: str) -> None:
        if require:
            raise RuntimeError(f"AMS_NGSPICE={mode} ({label}) requested, but {reason}")
        dut._log.info("Skipping %s: %s", label, reason)

    try:
        import cocotbext.ams  # noqa: F401
    except ImportError:
        _missing("cocotbext-ams is not installed")
        return None

    if not analog.has_libngspice():
        _missing("libngspice.so.0 is not installed")
        return None

    pdk_root_env = os.environ.get("PDK_ROOT")
    if not pdk_root_env:
        _missing("PDK_ROOT is not set")
        return None
    pdk_root = Path(pdk_root_env)
    missing_pdk = [str(p) for p in gf180.ngspice_pdk_files(pdk_root) if not p.is_file()]
    if missing_pdk:
        _missing(f"GF180MCU PDK SPICE files missing: {missing_pdk}")
        return None
    return pdk_root


class R2RDacModel:
    """Python analog model for passive 8-bit `r2r_ladder_8bit` DAC instances."""

    def __init__(self, dut, expected_channels: int | None = None, vdd: float = 3.3) -> None:
        self.vdd = vdd
        self._insts = find_r2r_instances(dut, width=dac.BITS)
        if expected_channels is not None:
            assert len(self._insts) == expected_channels, (
                f"Expected {expected_channels} {dac.CELL_NAME} instances, "
                f"found {len(self._insts)}: {[p for p, _ in self._insts]}"
            )

    @property
    def channels(self) -> int:
        return len(self._insts)

    def code(self, ch: int) -> int:
        path, pins = self._insts[ch]
        term = int(pins["term"].value)
        assert term == 0, f"{path}.term={term}, expected 0 (tiel -> clkbuf_20)"
        return int(pins["in"].value)

    def codes(self) -> list[int]:
        return [self.code(ch) for ch in range(self.channels)]

    def voltage(self, ch: int) -> float:
        return dac.ideal_voltage(self.code(ch), vdd=self.vdd)

    def voltages(self) -> list[float]:
        return [self.voltage(ch) for ch in range(self.channels)]


class DacAmsBridge:
    """Live `libngspice0` BSIM3v3 + LVS/PEX R-2R DAC co-simulation bridge via `cocotbext-ams`."""

    def __init__(
        self,
        dut,
        insts: list[tuple[str, HierarchyObject, dict]],
        r2r_spice: Path,
        pdk_root: Path,
        vdd: float = 3.3,
        corner: str = "typical",
        rcorner: str = "res_typical",
        temp: float | None = None,
        route_rc: dict[int, tuple[float, float] | dac.ChannelParasitics] | None = None,
        edge_time_ns: float = 5.0,
        max_sync_interval_ns: float = 10_000.0,
        tran_step: str = "5u",
    ) -> None:
        self._dut = dut
        self._insts = insts
        self.r2r_spice = r2r_spice
        self._pdk_root = pdk_root
        self.vdd = vdd
        self.corner = corner
        self.rcorner = rcorner
        self.temp = temp
        self.route_rc = route_rc
        self._edge_time_ns = edge_time_ns
        self.max_sync_interval_ns = max_sync_interval_ns
        self._tran_step = tran_step
        self._tmp_dir: tempfile.TemporaryDirectory[str] | None = None
        self._bridge = None
        self._stop_task = None

    @classmethod
    def create(
        cls,
        dut,
        expected_channels: int | None = None,
        vdd: float = 3.3,
        corner: str = "typical",
        rcorner: str = "res_typical",
        temp: float | None = None,
        r2r_spice: Path | None = None,
        spef_path: Path | None = None,
        route_rc: dict[int, tuple[float, float] | dac.ChannelParasitics] | None = None,
        out_nets: tuple[str, ...] | None = None,
        edge_time_ns: float = 5.0,
        max_sync_interval_ns: float = 10_000.0,
        tran_step: str = "5u",
    ) -> DacAmsBridge | None:
        """Create a `DacAmsBridge`, or return `None` when AMS prerequisites are absent."""
        pdk_root = _check_ams_prereqs(dut, "DacAmsBridge")
        if pdk_root is None:
            return None

        mode = os.environ.get("AMS_NGSPICE", "auto").strip().lower()
        require = mode in ("1", "true", "yes", "on", "require")

        spice_path = dac.find_spice(r2r_spice)
        if spice_path is None:
            msg = f"{dac.CELL_NAME}.spice not found (set R2R_SPICE or run src/dac.py)"
            if require:
                raise RuntimeError(f"AMS_NGSPICE={mode} requested, but {msg}")
            dut._log.info("Skipping DacAmsBridge: %s", msg)
            return None

        insts = find_r2r_handles(dut, width=dac.BITS)
        if expected_channels is not None:
            assert len(insts) == expected_channels, (
                f"Expected {expected_channels} {dac.CELL_NAME} instances, "
                f"found {len(insts)}: {[p for p, _, _ in insts]}"
            )

        if route_rc is None and (
            spef_path is not None or os.environ.get("SPEF_FILE") or os.environ.get("GATES") == "yes"
        ):
            found_spef = dac.find_spef(spef_path)
            if found_spef is not None:
                route_rc = dac.check_dac_parasitics(
                    found_spef, channels=len(insts), out_nets=out_nets
                )
                dut._log.info(
                    "Loaded post-PnR DAC SPEF parasitics (%s, ladder=%s): %s",
                    found_spef.name,
                    spice_path.name,
                    dac.format_parasitics_summary(route_rc),
                )

        return cls(
            dut=dut,
            insts=insts,
            r2r_spice=spice_path,
            pdk_root=pdk_root,
            vdd=vdd,
            corner=corner,
            rcorner=rcorner,
            temp=temp,
            route_rc=route_rc,
            edge_time_ns=edge_time_ns,
            max_sync_interval_ns=max_sync_interval_ns,
            tran_step=tran_step,
        )

    @property
    def channels(self) -> int:
        return len(self._insts)

    def measure_dc_luts(self) -> list[list[float]]:
        """Measure per-channel 256-code BSIM3v3 + R-2R (+ SPEF RC) DC curves via `libngspice0`."""
        assert self._bridge is None, "Call measure_dc_luts() before start() or after stop()"
        return dac.measure_dc_luts(
            self.r2r_spice,
            channels=self.channels,
            vdd=self.vdd,
            corner=self.corner,
            rcorner=self.rcorner,
            temp=self.temp,
            route_rc=self.route_rc,
            pdk_root=self._pdk_root,
        )

    def measure_dc_lut(self) -> list[float]:
        """Measure the 256-code BSIM3v3 + R-2R DC transfer curve in-process via `libngspice0`."""
        return self.measure_dc_luts()[0]

    def build_blocks(self, tmp_dir: Path) -> list:
        """Prepare SPICE wrapper in `tmp_dir` and return per-channel `AnalogBlock` instances."""
        wrapper = dac.prepare_ams_spice(tmp_dir, self.r2r_spice, route_rc=self.route_rc)
        return [
            dac.ams_block(
                name=f"dac{ch}",
                wrapper_spice=wrapper,
                handle=handle,
                vdd=self.vdd,
                corner=self.corner,
                rcorner=self.rcorner,
                temp=self.temp,
                edge_time_ns=self._edge_time_ns,
                tran_step=self._tran_step,
                subckt=f"{dac.AMS_SUBCKT}_ch{ch}"
                if self.route_rc and ch in self.route_rc
                else dac.AMS_SUBCKT,
                pdk_root=self._pdk_root,
            )
            for ch, (_, handle, _) in enumerate(self._insts)
        ]

    async def start(
        self,
        duration_ns: float,
        analog_vcd: str | Path | None = None,
    ) -> None:
        from cocotbext.ams import MixedSignalBridge

        self._stop_task = None
        self._tmp_dir = tempfile.TemporaryDirectory(prefix="dac_ams_")
        blocks = self.build_blocks(Path(self._tmp_dir.name))

        if analog_vcd is not None:
            Path(analog_vcd).parent.mkdir(parents=True, exist_ok=True)

        self._bridge = MixedSignalBridge(
            self._dut,
            blocks,
            max_sync_interval_ns=self.max_sync_interval_ns,
        )
        await self._bridge.start(
            duration_ns=duration_ns,
            analog_vcd=analog_vcd,
            blocking=False,
        )

    def voltage(self, ch: int) -> float:
        assert self._bridge is not None, "DacAmsBridge not started"
        return self._bridge.get_analog_voltage(f"dac{ch}", "out")

    def voltages(self) -> list[float]:
        return [self.voltage(ch) for ch in range(self.channels)]

    def begin_stop(self) -> None:
        """Detach `libngspice0` in the background without delaying the caller's clock phase."""
        if self._bridge is not None and self._stop_task is None:
            self._stop_task = cocotb.start_soon(self._do_stop())

    async def _do_stop(self) -> None:
        if self._bridge is not None:
            bridge = self._bridge
            self._bridge = None
            await bridge.stop()
        if self._tmp_dir is not None:
            self._tmp_dir.cleanup()
            self._tmp_dir = None

    async def stop(self) -> None:
        if self._stop_task is not None:
            task = self._stop_task
            self._stop_task = None
            await task
        await self._do_stop()


class R2RAdcModel:
    """Event-driven Python analog model for 4-bit summing `r2r_ladder_4bit_sum` ADCs.

    Watches each `u_r2r.in[3:0]` bus (= `~step[3:0]` after `clkinv_12` drivers in both RTL
    and GL) via `ValueChange`, computes the resistive-summing node voltage
    `V_sum = (V_in + V_dd * in / 16) / 2`, and drives `drive_handle` (e.g. `dut.ua_in`)
    with the comparator decision `V_sum > V_dd / 2`.
    """

    def __init__(
        self,
        dut,
        drive_handle,
        expected_channels: int = 4,
        vdd: float = 3.3,
    ) -> None:
        self.vdd = vdd
        self._drive = drive_handle
        self._insts = find_r2r_instances(dut, width=adc.BITS)
        assert len(self._insts) == expected_channels, (
            f"Expected {expected_channels} {adc.CELL_NAME} instances, "
            f"found {len(self._insts)}: {[p for p, _ in self._insts]}"
        )
        self._vin = [0.0] * expected_channels
        self._last_mask = -1
        self._tasks: list = []

    @property
    def channels(self) -> int:
        return len(self._insts)

    def ladder_codes(self) -> list[int]:
        """Return inverted ramp codes `in[3:0]` arriving at each passive ladder (0 during off)."""
        out = []
        for path, pins in self._insts:
            term = int(pins["term"].value)
            assert term == 0, f"{path}.term={term}, expected 0 (tieh -> clkinv_12)"
            out.append(int(pins["in"].value))
        return out

    def set_voltages(self, voltages: list[float]) -> None:
        assert len(voltages) == self.channels
        self._vin = list(voltages)
        self._update_comparators()

    def set_codes(self, codes: list[int]) -> None:
        """Set mid-bin analog CV voltages corresponding to target 4-bit codes (0..15)."""
        self.set_voltages([self.vdd * (c + 0.5) / 16.0 for c in codes])

    def _update_comparators(self) -> None:
        mask = 0
        for ch, (_, pins) in enumerate(self._insts):
            in_val = pins["in"].value
            if not in_val.is_resolvable:
                continue
            v_dac = self.vdd * int(in_val) / 16.0
            v_sum = 0.5 * (self._vin[ch] + v_dac)
            if v_sum > 0.5 * self.vdd:
                mask |= 1 << ch
        if mask != self._last_mask:
            self._last_mask = mask
            self._drive.value = mask

    def start(self) -> None:
        self._last_mask = -1
        self._update_comparators()
        for _, pins in self._insts:
            self._tasks.append(cocotb.start_soon(self._watch(pins["in"])))

    def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        self._tasks.clear()

    async def _watch(self, sig) -> None:
        while True:
            await ValueChange(sig)
            self._update_comparators()


class _BitDriveProxy:
    """Adapts one bit of `dut.ua_in` for `cocotbext-ams` `Force`/`Release` in Verilator & Icarus.

    Shares a Python-side shadow bitmask across channels so same-delta-cycle updates from
    multiple `AnalogBlock` instances never clobber each other before the HDL scheduler commits.
    """

    def __init__(self, drive_handle, mask_state: list[int], bit: int) -> None:
        self._drive = drive_handle
        self._mask_state = mask_state
        self._bit = bit

    @property
    def value(self) -> int:
        return (self._mask_state[0] >> self._bit) & 1

    @value.setter
    def value(self, val) -> None:
        if isinstance(val, Release):
            return
        bit_val = int(val.value) if isinstance(val, Force) else int(val)
        mask = self._mask_state[0]
        if bit_val & 1:
            mask |= 1 << self._bit
        else:
            mask &= ~(1 << self._bit)
        self._mask_state[0] = mask
        self._drive.value = mask


class _AdcBlockHandle:
    """Duck-typed handle binding `u_r2r.{term,in}` inputs and `dut.ua_in[ch]` `cmp` output."""

    def __init__(self, pins: dict, drive_handle, mask_state: list[int], ch: int) -> None:
        self.term = pins["term"]
        setattr(self, "in", pins["in"])
        self.cmp = _BitDriveProxy(drive_handle, mask_state, ch)


class AdcAmsBridge:
    """Live `libngspice0` BSIM3v3 + LVS/PEX 4-bit ramp ADC bridge via `cocotbext-ams`."""

    def __init__(
        self,
        dut,
        drive_handle,
        insts: list[tuple[str, HierarchyObject, dict]],
        r2r_spice: Path,
        pdk_root: Path,
        vdd: float = 3.3,
        corner: str = "typical",
        rcorner: str = "res_typical",
        temp: float | None = None,
        route_rc: dict[int, adc.AdcChannelParasitics] | None = None,
        edge_time_ns: float = 5.0,
        max_sync_interval_ns: float = 5_000.0,
        tran_step: str = "2u",
    ) -> None:
        self._dut = dut
        self._drive = drive_handle
        self._insts = insts
        self.r2r_spice = r2r_spice
        self._pdk_root = pdk_root
        self.vdd = vdd
        self.corner = corner
        self.rcorner = rcorner
        self.temp = temp
        self.route_rc = route_rc
        self._edge_time_ns = edge_time_ns
        self.max_sync_interval_ns = max_sync_interval_ns
        self._tran_step = tran_step
        self._vin: list[float] = [0.0] * len(insts)
        self._tmp_dir: tempfile.TemporaryDirectory[str] | None = None
        self._bridge = None

    @classmethod
    def create(
        cls,
        dut,
        drive_handle,
        expected_channels: int = adc.CHANNELS,
        vdd: float = 3.3,
        corner: str = "typical",
        rcorner: str = "res_typical",
        temp: float | None = None,
        r2r_spice: Path | None = None,
        spef_path: Path | None = None,
        route_rc: dict[int, adc.AdcChannelParasitics] | None = None,
        in_nets: tuple[str, ...] | None = None,
        edge_time_ns: float = 5.0,
        max_sync_interval_ns: float = 5_000.0,
        tran_step: str = "2u",
    ) -> AdcAmsBridge | None:
        """Create an `AdcAmsBridge`, or return `None` when AMS prerequisites are absent."""
        pdk_root = _check_ams_prereqs(dut, "AdcAmsBridge")
        if pdk_root is None:
            return None

        mode = os.environ.get("AMS_NGSPICE", "auto").strip().lower()
        require = mode in ("1", "true", "yes", "on", "require")

        spice_path = adc.find_spice(r2r_spice)
        if spice_path is None:
            msg = f"{adc.CELL_NAME}.spice not found (set ADC_R2R_SPICE or run src/adc.py)"
            if require:
                raise RuntimeError(f"AMS_NGSPICE={mode} requested, but {msg}")
            dut._log.info("Skipping AdcAmsBridge: %s", msg)
            return None

        insts = find_r2r_handles(dut, width=adc.BITS)
        assert len(insts) == expected_channels, (
            f"Expected {expected_channels} {adc.CELL_NAME} instances, "
            f"found {len(insts)}: {[p for p, _, _ in insts]}"
        )

        if route_rc is None and (
            spef_path is not None or os.environ.get("SPEF_FILE") or os.environ.get("GATES") == "yes"
        ):
            found_spef = dac.find_spef(spef_path)
            if found_spef is not None:
                route_rc = adc.check_adc_parasitics(
                    found_spef, channels=len(insts), in_nets=in_nets
                )
                dut._log.info(
                    "Loaded post-PnR ADC SPEF parasitics (%s, ladder=%s): %s",
                    found_spef.name,
                    spice_path.name,
                    adc.format_parasitics_summary(route_rc),
                )

        return cls(
            dut=dut,
            drive_handle=drive_handle,
            insts=insts,
            r2r_spice=spice_path,
            pdk_root=pdk_root,
            vdd=vdd,
            corner=corner,
            rcorner=rcorner,
            temp=temp,
            route_rc=route_rc,
            edge_time_ns=edge_time_ns,
            max_sync_interval_ns=max_sync_interval_ns,
            tran_step=tran_step,
        )

    @property
    def channels(self) -> int:
        return len(self._insts)

    def ladder_codes(self) -> list[int]:
        """Return inverted ramp codes `in[3:0]` arriving at each passive ladder (0 during off)."""
        out = []
        for path, _, pins in self._insts:
            term = int(pins["term"].value)
            assert term == 0, f"{path}.term={term}, expected 0 (tieh -> clkinv_12)"
            out.append(int(pins["in"].value))
        return out

    def measure_trip_points(self, channel: int | None = None) -> list[float]:
        """Measure the 15 BSIM3v3 + R-2R (+ SPEF RC) DC comparator trip voltages via `libngspice0`."""
        assert self._bridge is None, "Call measure_trip_points() before start() or after stop()"
        return adc.measure_trip_points(
            self.r2r_spice,
            vdd=self.vdd,
            corner=self.corner,
            rcorner=self.rcorner,
            temp=self.temp,
            route_rc=self.route_rc,
            channel=channel,
            pdk_root=self._pdk_root,
        )

    @staticmethod
    def code_voltage(code: int, trips: list[float], vdd: float) -> float:
        """Return the bin-center analog voltage for 4-bit `code` (0..15) given 15 `trips`."""
        assert 0 <= code <= 15, f"code {code} out of range 0..15"
        step = (trips[-1] - trips[0]) / (len(trips) - 1)
        if code == 0:
            return max(0.0, min(trips[0] - 0.5 * step, 0.5 * trips[0]))
        if code == 15:
            return min(vdd, max(trips[-1] + 0.5 * step, 0.5 * (trips[-1] + vdd)))
        return 0.5 * (trips[code - 1] + trips[code])

    @staticmethod
    def predict_code(voltage: float, trips: list[float]) -> int:
        """Return expected 4-bit ADC code for `voltage` given monotonic 15 `trips`."""
        return sum(1 for vt in trips if voltage > vt)

    def set_voltages(self, voltages: list[float]) -> None:
        assert len(voltages) == self.channels
        self._vin = list(voltages)
        self._apply_voltages()

    def set_codes(self, codes: list[int], trips: list[float] | None = None) -> None:
        """Drive bin-center analog CV voltages for target 4-bit `codes` (0..15)."""
        assert len(codes) == self.channels
        if trips is not None:
            self.set_voltages([self.code_voltage(c, trips, self.vdd) for c in codes])
        else:
            self.set_voltages([self.vdd * (c + 0.5) / 16.0 for c in codes])

    def vsum_voltages(self) -> list[float]:
        assert self._bridge is not None, "AdcAmsBridge not started"
        return [self._bridge.get_analog_voltage(f"adc{ch}", "vsum") for ch in range(self.channels)]

    def build_blocks(self, tmp_dir: Path) -> list:
        """Prepare ADC SPICE wrapper in `tmp_dir` and return per-channel `AnalogBlock` instances."""
        wrapper = adc.prepare_ams_spice(tmp_dir, self.r2r_spice, route_rc=self.route_rc)
        mask_state = [0]
        # Keep SPICE `dc 0.0 external` so ngspice does not add a DC offset to `GetVSRCData`;
        # actual runtime voltages are applied immediately via `set_analog_input()` on start.
        return [
            adc.ams_block(
                name=f"adc{ch}",
                wrapper_spice=wrapper,
                handle=_AdcBlockHandle(pins, self._drive, mask_state, ch),
                initial_vin=0.0,
                vdd=self.vdd,
                corner=self.corner,
                rcorner=self.rcorner,
                temp=self.temp,
                edge_time_ns=self._edge_time_ns,
                tran_step=self._tran_step,
                subckt=f"{adc.AMS_SUBCKT}_ch{ch}"
                if self.route_rc and ch in self.route_rc
                else adc.AMS_SUBCKT,
                pdk_root=self._pdk_root,
            )
            for ch, (_, _, pins) in enumerate(self._insts)
        ]

    def _apply_voltages(self) -> None:
        if self._bridge is not None:
            for ch, v in enumerate(self._vin):
                self._bridge.set_analog_input(f"adc{ch}", "adc_in", v)

    async def start(
        self,
        duration_ns: float,
        analog_vcd: str | Path | None = None,
    ) -> None:
        from cocotbext.ams import MixedSignalBridge

        self._tmp_dir = tempfile.TemporaryDirectory(prefix="adc_ams_")
        blocks = self.build_blocks(Path(self._tmp_dir.name))
        if analog_vcd is not None:
            Path(analog_vcd).parent.mkdir(parents=True, exist_ok=True)
        self._bridge = MixedSignalBridge(
            self._dut,
            blocks,
            max_sync_interval_ns=self.max_sync_interval_ns,
        )
        await self._bridge.start(
            duration_ns=duration_ns,
            analog_vcd=analog_vcd,
            blocking=False,
        )
        self._apply_voltages()

    async def stop(self) -> None:
        if self._bridge is not None:
            bridge = self._bridge
            self._bridge = None
            await bridge.stop()
        if self._tmp_dir is not None:
            self._tmp_dir.cleanup()
            self._tmp_dir = None


class MixedSignalAmsBridge:
    """Single-`libngspice0` co-simulation bridge combining `AdcAmsBridge` (4ch) + `DacAmsBridge` (2ch)."""

    def __init__(
        self,
        dut,
        adc_ams: AdcAmsBridge,
        dac_ams: DacAmsBridge,
        max_sync_interval_ns: float = 5_000.0,
    ) -> None:
        self._dut = dut
        self.adc = adc_ams
        self.dac = dac_ams
        self.max_sync_interval_ns = max_sync_interval_ns
        self._tmp_dir: tempfile.TemporaryDirectory[str] | None = None
        self._bridge = None

    async def start(
        self,
        duration_ns: float,
        analog_vcd: str | Path | None = None,
    ) -> None:
        from cocotbext.ams import MixedSignalBridge

        self._tmp_dir = tempfile.TemporaryDirectory(prefix="mixed_ams_")
        base = Path(self._tmp_dir.name)
        blocks = [
            *self.adc.build_blocks(base / "adc"),
            *self.dac.build_blocks(base / "dac"),
        ]
        if analog_vcd is not None:
            Path(analog_vcd).parent.mkdir(parents=True, exist_ok=True)
        self._bridge = MixedSignalBridge(
            self._dut,
            blocks,
            max_sync_interval_ns=self.max_sync_interval_ns,
        )
        self.adc._bridge = self._bridge
        self.dac._bridge = self._bridge
        await self._bridge.start(
            duration_ns=duration_ns,
            analog_vcd=analog_vcd,
            blocking=False,
        )
        self.adc._apply_voltages()

    async def stop(self) -> None:
        if self._bridge is not None:
            bridge = self._bridge
            self._bridge = None
            self.adc._bridge = None
            self.dac._bridge = None
            await bridge.stop()
        if self._tmp_dir is not None:
            self._tmp_dir.cleanup()
            self._tmp_dir = None
