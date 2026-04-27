"""Read USB descriptor info for plugged-in devices.

Lets us pair an ``extcap`` interface (which only knows the OS-side device
node like ``/dev/cu.usbmodem...``) with the physical USB unit's serial
number and Location ID. Location ID is the stable sort key the canvas
uses to order sniffers top-to-bottom by physical hub position.

Implementation notes:
  * macOS: parses ``ioreg -p IOUSB -l -w 0`` output. No external
    dependencies.
  * Linux / Windows: not yet implemented; ``query()`` returns [] so
    callers degrade gracefully.

We return ALL USB devices we can introspect (any vendor), not just
Nordic/SEGGER. The reason: BLE sniffers come in three flavors —

  * Native USB on the chip itself (Nordic VID 0x1915 — official PCA10059
    nRF52840 dongle, Taidacent clones, anything running the Open DFU
    Bootloader)
  * Behind an onboard J-Link (SEGGER VID 0x1366 — nRF5340 Audio DK
    exposes its USB through its onboard SEGGER J-Link debug interface)
  * Behind a USB-to-UART bridge (Silicon Labs 0x10C4 / FTDI 0x0403 / etc.
    — the Adafruit Bluefruit LE Sniffer is in this third class: nRF51822
    has no native USB, so a CP2104 sits between the host and the chip)

Filtering out the third class meant the Adafruit sniffer was invisible
to our pairing logic. The fix is to surface all devices and let the
classification step decide what kind each one is.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from dataclasses import dataclass

# Vendor IDs we recognize at classification time. NOT used as a query
# filter anymore — query() returns every device it can see. Listed here
# so callers can identify what they got. Add more as we encounter them.
NORDIC_VID = 0x1915
SEGGER_VID = 0x1366
SILABS_VID = 0x10C4   # Silicon Labs CP2102/2104 — Adafruit Bluefruit LE
FTDI_VID = 0x0403
PROLIFIC_VID = 0x067B
CH340_VID = 0x1A86


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
        if vid is None:
            continue
        # Skip USB hubs — they have idProduct but typically no useful
        # iSerial and they're never sniffer endpoints. Recognized by USB
        # class 0x09 (hub class) where available. Cheap to keep them
        # filtered out so the device list is just leaf endpoints.
        usb_class = _maybe_int(fields.get("bDeviceClass"))
        if usb_class == 0x09:
            continue
        serial = _strip_quotes(fields.get("USB Serial Number")
                               or fields.get("kUSBSerialNumberString"))
        # No serial: device-node-name pairing falls back to Location ID.
        # We still emit a row (empty serial) so the caller can pair by
        # location_id_hex. This is exactly what makes Adafruit-style
        # USB-to-UART sniffers show up — their CP2104 chips don't always
        # publish a serial.
        pid = _maybe_int(fields.get("idProduct")) or 0
        product = _strip_quotes(fields.get("USB Product Name")
                                or fields.get("kUSBProductString"))
        loc = _maybe_int(fields.get("locationID"))
        loc_hex = f"0x{loc:08x}" if loc is not None else None
        devices.append(UsbDeviceInfo(
            serial_number=serial or "",
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
