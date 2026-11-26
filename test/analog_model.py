# Copyright 2026 Google LLC.
# SPDX-License-Identifier: Apache-2.0
"""Cocotb Python analog models for passive R-2R converter macros (RTL + Gate-Level).

Follows cocotb's `analog_model` pattern: auto-discovers preserved `u_r2r` macro instances
in both hierarchical RTL and flattened post-PnR Gate-Level netlists, keeping Verilog stubs
pure pass-throughs while verifying full-resolution converter codes and voltages in Python.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import sys
import tempfile

from cocotb.handle import HierarchyArrayObject, HierarchyObject

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

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
    """Live `libngspice0` BSIM3v3 + LVS R-2R ladder co-simulation bridge via `cocotbext-ams`."""

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
        r2r_spice: Path | None = None,
        corner: str = "typical",
        rcorner: str = "res_typical",
        temp: float | None = None,
        spef_path: Path | None = None,
        out_nets: tuple[str, ...] | list[str] | None = None,
        route_rc: dict[int, tuple[float, float] | dac.ChannelParasitics] | None = None,
        edge_time_ns: float = 5.0,
        max_sync_interval_ns: float = 10_000.0,
        tran_step: str = "5u",
    ) -> DacAmsBridge | None:
        """Create a `DacAmsBridge` if prerequisites exist, or raise when `AMS_NGSPICE=1`."""
        mode = os.environ.get("AMS_NGSPICE", "").strip().lower()
        if mode in ("0", "false", "no", "off"):
            return None
        required = mode in ("1", "true", "yes", "on")

        def _missing(reason: str) -> None:
            if required:
                raise RuntimeError(f"AMS_NGSPICE=1 requested but {reason}")
            dut._log.info("Skipping ngspice AMS co-sim (%s)", reason)

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

        spice_path = dac.find_spice(r2r_spice)
        if spice_path is None and r2r_spice is None and not os.environ.get("R2R_SPICE"):
            if analog.kf is not None:
                s, cell, pins = dac.build_dac()
                _, _, _, spice_path = analog.emit_macro(s, cell, pins, REPO_ROOT / "gen/dac")
                dut._log.info("Generated passive DAC SPICE netlist -> %s", spice_path)
        if spice_path is None:
            _missing(f"{dac.CELL_NAME}.spice not found (set R2R_SPICE or install kfactory)")
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
                spef_res = dac.check_dac_parasitics(
                    found_spef, channels=len(insts), out_nets=out_nets
                )
                assert spef_res.ok, "; ".join(spef_res.violations)
                route_rc = spef_res.channels
                dut._log.info(
                    "Loaded post-PnR SPEF parasitics (%s, ladder=%s): %s",
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

    async def start(
        self,
        duration_ns: float,
        analog_vcd: str | Path | None = None,
    ) -> None:
        from cocotbext.ams import MixedSignalBridge

        self._stop_task = None
        self._tmp_dir = tempfile.TemporaryDirectory(prefix="dac_ams_")
        wrapper = dac.prepare_ams_spice(
            Path(self._tmp_dir.name), self.r2r_spice, route_rc=self.route_rc
        )

        blocks = [
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
        import cocotb

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
