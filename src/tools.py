#!/usr/bin/env python3
# Copyright 2026 Google LLC.
# SPDX-License-Identifier: Apache-2.0
"""Verify native EDA tool versions match the pinned LibreLane container toolchain."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
import os
import re
import shutil
import subprocess

LIBRELANE_VERSION = "3.0.17"

# Exact versions shipped in ghcr.io/librelane/librelane:3.0.17.
LIBRELANE_TOOL_VERSIONS: dict[str, str] = {
    "magic": "8.3.623",
    "klayout": "0.30.7",
    "verilator": "5.044",
}

# Native minimum overrides when external simulation requires a newer feature than LibreLane's lint:
# GF180MCU `gf180mcu_fd_sc_mcu7t5v0.v` (`buf (weak0, weak1)`) fails in Verilator 5.044 (`UNSUPPORTED`).
NATIVE_MIN_TOOL_VERSIONS: dict[str, str] = {
    "verilator": "5.054",
}

_VERSION_SPECS: dict[str, tuple[tuple[str, ...], str]] = {
    "magic": (("magic", "--version"), r"(\d+\.\d+\.\d+)"),
    "klayout": (("klayout", "-v"), r"KLayout\s+(\d+\.\d+\.\d+)"),
    "verilator": (("verilator", "--version"), r"Verilator\s+(\d+\.\d+)"),
}


def _ver_tuple(ver: str) -> tuple[int, ...]:
    return tuple(int(tok) for tok in ver.split("."))


@dataclass(frozen=True)
class ToolVersionCheck:
    """Result of comparing one tool's detected version against the LibreLane pin."""

    name: str
    expected: str
    actual: str | None
    source: str | None
    at_least: bool = False

    @property
    def ok(self) -> bool:
        if self.actual is None:
            return False
        if self.at_least:
            return _ver_tuple(self.actual) >= _ver_tuple(self.expected)
        return self.actual == self.expected


def librelane_docker_image() -> str:
    """Return the pinned LibreLane container image (respecting LIBRELANE_IMAGE_OVERRIDE)."""
    if override := os.environ.get("LIBRELANE_IMAGE_OVERRIDE"):
        return override
    return f"ghcr.io/librelane/librelane:{LIBRELANE_VERSION}"


def parse_tool_version(name: str, output: str) -> str | None:
    """Extract the canonical version string for `name` from its CLI `--version` output."""
    _, pattern = _VERSION_SPECS[name]
    m = re.search(pattern, output)
    return m.group(1) if m else None


def detect_native_version(name: str) -> tuple[str | None, str | None]:
    """Return `(version, resolved_path)` for a native tool on `PATH`."""
    cmd_args, _ = _VERSION_SPECS[name]
    bin_path = shutil.which(cmd_args[0])
    if bin_path is None:
        return None, None
    res = subprocess.run([bin_path, *cmd_args[1:]], capture_output=True, text=True)
    if res.returncode != 0:
        return None, bin_path
    return parse_tool_version(name, res.stdout + res.stderr), bin_path


def check_native_tools(tools: Iterable[str] | None = None) -> list[ToolVersionCheck]:
    """Compare native tool binaries on `PATH` against LibreLane's pinned versions."""
    names = tuple(tools) if tools is not None else tuple(LIBRELANE_TOOL_VERSIONS)
    results: list[ToolVersionCheck] = []
    for name in names:
        if name not in LIBRELANE_TOOL_VERSIONS:
            raise ValueError(f"Unknown LibreLane tool: {name!r}")
        actual, path = detect_native_version(name)
        min_ver = NATIVE_MIN_TOOL_VERSIONS.get(name)
        if min_ver is not None:
            expected = (
                min_ver
                if _ver_tuple(min_ver) >= _ver_tuple(LIBRELANE_TOOL_VERSIONS[name])
                else LIBRELANE_TOOL_VERSIONS[name]
            )
            at_least = True
        else:
            expected = LIBRELANE_TOOL_VERSIONS[name]
            at_least = False
        results.append(
            ToolVersionCheck(
                name=name,
                expected=expected,
                actual=actual,
                source=path,
                at_least=at_least,
            )
        )
    return results


def check_librelane_package_version() -> ToolVersionCheck:
    """Verify installed Python `librelane.__version__` matches `LIBRELANE_VERSION`."""
    try:
        import librelane
    except ImportError:
        return ToolVersionCheck(
            name="librelane",
            expected=LIBRELANE_VERSION,
            actual=None,
            source=None,
        )
    return ToolVersionCheck(
        name="librelane",
        expected=LIBRELANE_VERSION,
        actual=str(librelane.__version__),
        source=str(librelane.__file__),
    )


def check_librelane_container_tools(
    image: str | None = None,
    tools: Iterable[str] | None = None,
) -> list[ToolVersionCheck]:
    """Query the LibreLane Docker image directly to verify `LIBRELANE_TOOL_VERSIONS` parity."""
    docker_bin = shutil.which("docker")
    if docker_bin is None:
        raise FileNotFoundError("docker is required to verify LibreLane container tools")
    img = image or librelane_docker_image()
    names = tuple(tools) if tools is not None else tuple(LIBRELANE_TOOL_VERSIONS)
    shell_parts = [f"echo '=== {n} ==='; {' '.join(_VERSION_SPECS[n][0])}" for n in names]
    res = subprocess.run(
        [docker_bin, "run", "--rm", img, "bash", "-c", "; ".join(shell_parts)],
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        raise RuntimeError(f"Failed to query {img}:\n{res.stdout}\n{res.stderr}")
    sections: dict[str, str] = {}
    cur: str | None = None
    for line in (res.stdout + "\n" + res.stderr).splitlines():
        if line.startswith("=== ") and line.endswith(" ==="):
            cur = line[4:-4]
            sections[cur] = ""
        elif cur is not None:
            sections[cur] += line + "\n"
    return [
        ToolVersionCheck(
            name=n,
            expected=LIBRELANE_TOOL_VERSIONS[n],
            actual=parse_tool_version(n, sections.get(n, "")),
            source=img,
        )
        for n in names
    ]


def main() -> None:
    checks: list[ToolVersionCheck] = [
        check_librelane_package_version(),
        *check_native_tools(),
        *check_librelane_container_tools(),
    ]

    for c in checks:
        status = "OK" if c.ok else "MISMATCH"
        op = ">=" if c.at_least else "=="
        print(
            f"  [tools | {c.name:9s}] {status}: "
            f"actual={c.actual or 'missing'} {op} {c.expected} ({c.source or 'not on PATH'})"
        )
    failed = [c for c in checks if not c.ok]
    assert not failed, f"Tool version mismatch vs LibreLane {LIBRELANE_VERSION}: " + ", ".join(
        f"{c.name}={c.actual} (expected {'>=' if c.at_least else '=='} {c.expected})"
        for c in failed
    )


if __name__ == "__main__":
    main()
