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
from . import usb_info


class ExtcapNotFound(RuntimeError):
    pass


@dataclass(frozen=True)
class Dongle:
    """A discovered nRF Sniffer interface, optionally enriched with USB info."""
    interface_id: str        # value passed to --extcap-interface
    display: str             # human-readable name from extcap
    serial_path: str         # /dev/cu.usbmodem... on macOS, /dev/ttyACM... on Linux
    serial_number: str | None = None    # USB serial (stable across replugs)
    location_id_hex: str | None = None  # USB physical-port id (sort key)
    usb_product: str | None = None      # USB Product Name descriptor
    kind: str = "unknown"               # dongle | dk | unknown

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

    # Enrich with USB descriptors when we can — gets us the real serial
    # number and Location ID (stable physical-port sort key). On platforms
    # where usb_info isn't supported, the dongles stay at the base fields.
    found = _enrich_with_usb(found)

    # Stable order so UI assignments don't shuffle between scans. Prefer
    # location_id when available (physical position in the hub); fall back
    # to serial_path for stable ordering on platforms without USB info.
    found.sort(key=lambda d: (
        d.location_id_hex is None,
        d.location_id_hex or "",
        d.serial_path,
    ))
    return found


def _enrich_with_usb(dongles: list[Dongle]) -> list[Dongle]:
    """Match each Dongle to a USB descriptor and copy in the descriptor info.

    Pairing is done by substring: a USB device's serial often appears
    embedded in the OS's device-node path (e.g. ``/dev/cu.usbmodem
    461A0A45E94DB33B1`` ↔ serial ``461A0A45E94DB33B``). We try both
    directions of substring containment to cover the various platform
    transformations (truncation, hex-fold, suffix digits).

    Each USB device is matched at most once. Unmatched dongles are
    returned unchanged — their serial / location_id stay None and the
    DB will use ``serial_path`` as a fallback identifier.
    """
    usb_devices = usb_info.query()
    if not usb_devices:
        return dongles

    used: set[str] = set()
    enriched: list[Dongle] = []
    for d in dongles:
        match: usb_info.UsbDeviceInfo | None = None
        path_lower = d.serial_path.lower()
        for u in usb_devices:
            if u.serial_number in used:
                continue
            sn = u.serial_number.lower()
            # Either direction of substring containment counts as a match.
            if sn in path_lower or _serial_root_in_path(sn, path_lower):
                match = u
                break
        if match is None:
            enriched.append(d)
            continue
        used.add(match.serial_number)
        enriched.append(Dongle(
            interface_id=d.interface_id,
            display=d.display,
            serial_path=d.serial_path,
            serial_number=match.serial_number,
            location_id_hex=match.location_id_hex,
            usb_product=match.product_name,
            kind=_classify_kind(match),
        ))
    return enriched


def _serial_root_in_path(sn_lower: str, path_lower: str) -> bool:
    """Looser match: ignore trailing single-char endings the OS sometimes
    drops when shortening (e.g. host serial '001050289319' vs USB serial
    '00105028931901'). Try shrinking the serial by up to 3 trailing chars."""
    for n in (1, 2, 3):
        if len(sn_lower) > n and sn_lower[:-n] in path_lower:
            return True
    return False


def _classify_kind(u: "usb_info.UsbDeviceInfo") -> str:
    """Derive the broad sniffer ``kind`` (dongle | dk | unknown) from
    USB descriptor strings. Heuristic — refine as we encounter more
    hardware variants. Currently:
      * "J-Link" / "J_Link" product → DK (the nRF5340 Audio DK exposes
        its onboard SEGGER J-Link interface to the host)
      * "nRF Sniffer for Bluetooth LE" product → dongle (covers official
        Nordic PCA10059 and clones flashed with the Nordic Sniffer FW)
    """
    name = (u.product_name or "").lower()
    if "j-link" in name or "j_link" in name or "jlink" in name:
        return "dk"
    if "nrf sniffer" in name or "nordic" in name:
        return "dongle"
    if u.vendor_id == usb_info.SEGGER_VID:
        return "dk"
    if u.vendor_id == usb_info.NORDIC_VID:
        return "dongle"
    return "unknown"


# ──────────────────────────────────────────────────────────────────────────
# DB persistence: write a discovery sweep into the sniffers table
# ──────────────────────────────────────────────────────────────────────────

def discovered_to_db_records(dongles: list[Dongle]) -> list[dict]:
    """Convert ``Dongle`` instances into the dict shape expected by
    ``Sniffers.record_discovered``. Dongles without a USB serial fall
    back to ``serial_path`` so the row still gets persisted.
    """
    records: list[dict] = []
    for d in dongles:
        sn = d.serial_number or d.serial_path
        records.append({
            "serial_number":   sn,
            "kind":            d.kind,
            "usb_port_id":     d.serial_path,
            "location_id_hex": d.location_id_hex,
            "interface_id":    d.interface_id,
            "display":         d.display,
            "usb_product":     d.usb_product,
        })
    return records
