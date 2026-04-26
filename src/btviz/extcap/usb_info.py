"""Read USB descriptor info for plugged-in devices.

Lets us pair an ``extcap`` interface (which only knows the OS-side device
node like ``/dev/cu.usbmodem...``) with the physical USB unit's serial
number and Location ID. The Location ID is the stable sort key the
canvas uses to order sniffers top-to-bottom by physical hub position.

Implementation notes:
  * macOS: parses ``ioreg -p IOUSB -l -w 0`` output. No external
    dependencies. Same approach we used in this conversation when we
    walked through detecting the Taidacent dongle.
  * Linux / Windows: not yet implemented; ``query()`` returns [] so
    callers degrade gracefully.

Returns one ``UsbDeviceInfo`` per interesting USB descriptor — only
those with a serial number, since that's what we key sniffer identity
on. Filters by USB Vendor ID to keep the noise down (Nordic 0x1915,
SEGGER 0x1366 for the J-Link in the nRF5340 DK).
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from dataclasses import dataclass

# Vendor IDs we care about. SEGGER appears for the nRF5340 Audio DK
# (its onboard J-Link debug interface enumerates as a SEGGER device).
NORDIC_VID = 0x1915
SEGGER_VID = 0x1366
_INTERESTING_VIDS = {NORDIC_VID, SEGGER_VID}


@dataclass(frozen=True)
class UsbDeviceInfo:
    serial_number: str           # USB Serial Number string descriptor
    vendor_id: int
    product_id: int
    product_name: str | None     # USB Product Name string descriptor
    location_id_hex: str | None  # macOS IOUSB Location ID, e.g. "0x14400000"


def query() -> list[UsbDeviceInfo]:
    """Return descriptor info for all relevant plugged-in USB devices.

    Empty list on platforms we don't support yet.
    """
    if sys.platform == "darwin":
        return _query_macos()
    # TODO: Linux via /sys/bus/usb/devices, Windows via WMI.
    return []


# ──────────────────────────────────────────────────────────────────────────
# macOS
# ──────────────────────────────────────────────────────────────────────────

# ioreg dumps a tree where each device is a stanza of "key" = value lines.
# Values can be quoted strings, ints, or booleans. We split on stanzas
# (identified by the "+-o ... <class IO...>" header) and pull the keys
# we need from each.
_IOREG_STANZA_HEAD = re.compile(r"^\s*\+-o\s+.+<class\s+\w+", re.M)
_KV_LINE = re.compile(r'^\s*\|?\s*"?([\w\.]+)"?\s*=\s*(.+?)\s*$')


def _query_macos() -> list[UsbDeviceInfo]:
    if shutil.which("ioreg") is None:
        return []
    try:
        out = subprocess.run(
            ["ioreg", "-p", "IOUSB", "-l", "-w", "0"],
            capture_output=True, text=True, timeout=10,
        ).stdout
    except (subprocess.SubprocessError, OSError):
        return []

    devices: list[UsbDeviceInfo] = []
    # Walk the output one logical device at a time. ioreg is a tree, so
    # we naively split on stanza headers and parse each block separately.
    # Good enough — we only need the leaf USB device entries.
    blocks = _IOREG_STANZA_HEAD.split(out)
    for blk in blocks:
        fields = _parse_ioreg_block(blk)
        vid = _maybe_int(fields.get("idVendor"))
        if vid is None or vid not in _INTERESTING_VIDS:
            continue
        serial = _strip_quotes(fields.get("USB Serial Number")
                               or fields.get("kUSBSerialNumberString"))
        if not serial:
            continue
        pid = _maybe_int(fields.get("idProduct")) or 0
        product = _strip_quotes(fields.get("USB Product Name")
                                or fields.get("kUSBProductString"))
        loc = _maybe_int(fields.get("locationID"))
        loc_hex = f"0x{loc:08x}" if loc is not None else None
        devices.append(UsbDeviceInfo(
            serial_number=serial,
            vendor_id=vid,
            product_id=pid,
            product_name=product,
            location_id_hex=loc_hex,
        ))
    return devices


def _parse_ioreg_block(block: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in block.splitlines():
        m = _KV_LINE.match(line)
        if m:
            # ioreg sometimes shows the same key multiple times in one
            # tree; the first occurrence is the device's own value.
            fields.setdefault(m.group(1), m.group(2))
    return fields


def _strip_quotes(s: str | None) -> str | None:
    if s is None:
        return None
    s = s.strip()
    if s.startswith('"') and s.endswith('"') and len(s) >= 2:
        return s[1:-1]
    return s


def _maybe_int(s: str | None) -> int | None:
    if s is None:
        return None
    s = s.strip().strip('"')
    try:
        return int(s, 0) if s.startswith(("0x", "0X")) else int(s)
    except ValueError:
        return None
