"""Discover the Nordic nRF Sniffer extcap binary and enumerate dongles.

The Wireshark extcap mechanism advertises capture interfaces via:
    <extcap_binary> --extcap-interfaces

Output format (one line per interface):
    interface {value=<id>}{display=<name>}
    ...

We invoke the Nordic extcap directly so the app does not need a running
Wireshark/tshark for enumeration. Capture itself uses the same binary
in --capture mode (see sniffer.py).
"""
from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..config import NRF_EXTCAP_CANDIDATE_PATHS


class ExtcapNotFound(RuntimeError):
    pass


@dataclass(frozen=True)
class Dongle:
    """A discovered nRF Sniffer interface."""
    interface_id: str        # value passed to --extcap-interface
    display: str             # human-readable name from extcap
    serial_path: str         # /dev/cu.usbmodem... on macOS, /dev/ttyACM... on Linux

    @property
    def short_id(self) -> str:
        """Trailing serial-ish suffix, used for UI labels."""
        # /dev/cu.usbmodem0010502893191-4.6 -> 0010502893191-4.6
        m = re.search(r"usbmodem([\w.\-]+)", self.serial_path)
        return m.group(1) if m else self.serial_path.rsplit("/", 1)[-1]


def find_extcap_binary() -> Path:
    """Locate the Nordic extcap binary. Honors $BTVIZ_NRF_EXTCAP override."""
    override = os.environ.get("BTVIZ_NRF_EXTCAP")
    if override:
        p = Path(override).expanduser()
        if p.exists():
            return p
        raise ExtcapNotFound(f"$BTVIZ_NRF_EXTCAP set but not found: {p}")
    for cand in NRF_EXTCAP_CANDIDATE_PATHS:
        p = Path(cand)
        if p.exists():
            return p
    raise ExtcapNotFound(
        "nRF Sniffer extcap binary not found. Install the Nordic nRF Sniffer "
        "for Bluetooth LE plugin into Wireshark, or set $BTVIZ_NRF_EXTCAP."
    )


_INTERFACE_LINE = re.compile(r"interface\s*\{value=([^}]+)\}\{display=([^}]+)\}")


def list_dongles(extcap: Path | None = None) -> list[Dongle]:
    """Return all currently connected nRF Sniffer dongles.

    Filters out macOS `/dev/tty.*` duplicates of `/dev/cu.*` devices.
    """
    extcap = extcap or find_extcap_binary()
    out = subprocess.run(
        [str(extcap), "--extcap-interfaces"],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout

    found: list[Dongle] = []
    for line in out.splitlines():
        m = _INTERFACE_LINE.match(line.strip())
        if not m:
            continue
        iface_id, display = m.group(1), m.group(2)
        # Nordic uses the serial device path as the interface value.
        serial_path = iface_id
        # Drop the macOS tty.* twin; keep cu.*
        if "/tty.usbmodem" in serial_path:
            continue
        found.append(Dongle(
            interface_id=iface_id,
            display=display,
            serial_path=serial_path,
        ))

    # Stable order so UI assignments don't shuffle between scans.
    found.sort(key=lambda d: d.serial_path)
    return found
