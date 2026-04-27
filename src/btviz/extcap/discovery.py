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
    """A discovered nRF Sniffer interface, optionally enriched with USB info.

    Defaults ``kind="dongle"`` because anything the extcap binary lists is
    a sniffer of some sort. Specific overrides:
      * SEGGER J-Link product → ``"dk"`` (nRF5340 Audio DK debug interface)
      * Otherwise stays ``"dongle"`` (Nordic nRF52840, Taidacent clone,
        Silicon Labs / FTDI / CH340-bridged BLE 4.x sniffers like the
        Adafruit Bluefruit LE Sniffer)
    """
    interface_id: str        # value passed to --extcap-interface
    display: str             # human-readable name from extcap
    serial_path: str         # /dev/cu.usbmodem... on macOS, /dev/ttyACM... on Linux
    serial_number: str | None = None    # USB serial (stable across replugs)
    location_id_hex: str | None = None  # USB physical-port id (sort key)
    usb_product: str | None = None      # USB Product Name descriptor
    kind: str = "dongle"

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


def list_dongles(
    extcap: Path | None = None,
    *,
    timeout: float = 60.0,
) -> list[Dongle]:
    """Return all currently connected nRF Sniffer dongles.

    Filters out macOS `/dev/tty.*` duplicates of `/dev/cu.*` devices.

    The Nordic extcap probes every serial-class USB device on the host
    looking for the sniffer protocol. Pass-through devices like the
    Silicon Labs CP2104 (used on the Adafruit Bluefruit LE Sniffer) can
    push the probe over 10s, so the default timeout is conservative.
    """
    extcap = extcap or find_extcap_binary()
    out = subprocess.run(
        [str(extcap), "--extcap-interfaces"],
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
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

    # Drop SLAB_USBtoUART duplicates when an equivalent /dev/cu.usbserial-
    # node exists for the same physical device. macOS exposes both names
    # for Silicon Labs USB-to-UART chips (the Adafruit Bluefruit LE Sniffer
    # uses one). Silicon Labs's driver creates /dev/cu.SLAB_USBtoUART as a
    # convenience alias for the canonical /dev/cu.usbserial-XXXX node;
    # both nodes point at the same chip and the extcap binary lists each
    # one, so without this filter every Silicon-Labs-bridged sniffer
    # double-counts in the panel.
    found = _dedupe_slab_usbtouart_aliases(found)

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


def _dedupe_slab_usbtouart_aliases(dongles: list[Dongle]) -> list[Dongle]:
    """Drop SLAB_USBtoUART aliases when an equivalent usbserial node exists.

    Silicon Labs's macOS driver creates ``/dev/cu.SLAB_USBtoUART`` as a
    duplicate alias of the canonical ``/dev/cu.usbserial-<serial>``. When
    both nodes are active the extcap lists both, and we'd double-count.
    Strategy: if there's any ``usbserial`` entry, drop every
    ``SLAB_USBtoUART`` entry. Both names point at the same chip, and the
    canonical name carries the iSerial.
    """
    has_usbserial = any("usbserial" in d.serial_path for d in dongles)
    if not has_usbserial:
        return dongles
    return [d for d in dongles if "SLAB_USBtoUART" not in d.serial_path]


def _enrich_with_usb(dongles: list[Dongle]) -> list[Dongle]:
    """Match each Dongle to a USB descriptor and copy in the descriptor info.

    Pairing strategy, in priority order:
      1. **Substring serial match.** When a USB device has an iSerial,
         macOS embeds it in the device-node path
         (``/dev/cu.usbmodem<iSerial>1``). The Nordic dongles + the DK's
         J-Link interface follow this pattern.
      2. **Location-ID-prefix match.** USB devices without an iSerial get
         a path like ``/dev/cu.usbmodem<location_prefix>-<…>`` where the
         prefix is the upper hex digits of the parent USB Location ID
         (e.g. path ``usbmodem22330`` ↔ location ``0x22330000``). The
         Adafruit Bluefruit LE Sniffer (Silicon Labs CP2104 bridge) and
         other USB-to-UART sniffers fall through to this path.

    Each USB device is matched at most once. Unmatched dongles keep their
    base fields (kind="dongle" by default — see Dongle dataclass).
    """
    usb_devices = usb_info.query()
    if not usb_devices:
        return dongles

    used_keys: set[str] = set()
    enriched: list[Dongle] = []

    def _key(u: "usb_info.UsbDeviceInfo") -> str:
        # Unique identity for deduplication. Serial first if present;
        # otherwise the location ID is the next-most-stable identifier.
        return u.serial_number or u.location_id_hex or f"{u.vendor_id:x}:{u.product_id:x}"

    for d in dongles:
        match: usb_info.UsbDeviceInfo | None = None
        path_lower = d.serial_path.lower()

        # 1. Substring match on serial.
        for u in usb_devices:
            k = _key(u)
            if k in used_keys or not u.serial_number:
                continue
            sn = u.serial_number.lower()
            if sn in path_lower or _serial_root_in_path(sn, path_lower):
                match = u
                break

        # 2. Location-ID prefix match. The path's hex-ish run after
        # "usbmodem" (and similar) is the location prefix when iSerial
        # was empty.
        if match is None:
            for u in usb_devices:
                k = _key(u)
                if k in used_keys or not u.location_id_hex:
                    continue
                if _location_prefix_in_path(u.location_id_hex, path_lower):
                    match = u
                    break

        if match is None:
            enriched.append(d)
            continue
        used_keys.add(_key(match))

        # Override kind to "dk" only for SEGGER / J-Link interfaces;
        # everything else extcap listed is some flavor of dongle.
        kind = _classify_kind(match)
        enriched.append(Dongle(
            interface_id=d.interface_id,
            display=d.display,
            serial_path=d.serial_path,
            serial_number=match.serial_number or None,
            location_id_hex=match.location_id_hex,
            usb_product=match.product_name,
            kind=kind,
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


def _location_prefix_in_path(location_hex: str, path_lower: str) -> bool:
    """Match a USB Location ID prefix against an unserialized device path.

    macOS encodes the parent hub's location into device-node names for
    devices without an iSerial. The encoding drops trailing zeros and
    interleaves digits — examples observed in the wild:

      location 0x22330000  →  ``/dev/cu.usbmodem22330-...``
      location 0x22320000  →  ``/dev/cu.usbmodem22320-...``

    We strip the leading ``0x`` and trailing zero pairs from the location,
    then check substring containment against the lowercased path. Tries a
    couple of common truncations to cover both 4- and 5-digit forms.
    """
    h = location_hex.lower().removeprefix("0x").rstrip("0")
    if not h:
        return False
    # Try the rstripped form, plus 4- and 5-digit truncations
    candidates = {h, h[:5], h[:4]}
    return any(c and c in path_lower for c in candidates)


def _classify_kind(u: "usb_info.UsbDeviceInfo") -> str:
    """Derive the sniffer ``kind`` from USB descriptor strings.

    Defaults to ``"dongle"`` — anything the extcap surfaced is a sniffer,
    so unless we positively recognize it as a development kit we treat
    it as a dongle. Specific overrides:

      * SEGGER J-Link product or VID → DK (the nRF5340 Audio DK exposes
        its onboard SEGGER J-Link interface to the host)
    """
    name = (u.product_name or "").lower()
    if "j-link" in name or "j_link" in name or "jlink" in name:
        return "dk"
    if u.vendor_id == usb_info.SEGGER_VID:
        return "dk"
    return "dongle"


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
