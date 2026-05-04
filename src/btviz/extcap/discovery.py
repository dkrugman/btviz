"""Discover sniffer dongles via direct USB enumeration.

Historically btviz invoked the Nordic ``nrf_sniffer_ble.py`` extcap
binary in ``--extcap-interfaces`` mode and parsed its output. That
probe turned out to be unreliable: on multi-dongle setups the
binary silently dropped most of the connected dongles (e.g.
returning 2 of 7 plugged-in sniffer-firmware devices), apparently
serializing per-port probes with timeouts that didn't scale.

This module now enumerates sniffer dongles directly via ``pyserial``
(which reads USB descriptors), then synthesizes the extcap
``interface_id`` from the macOS-allocated device path. The Nordic
extcap is only invoked at *capture* time, with a single
``--extcap-interface <id>`` per dongle — no broad enumeration
probe is needed.

The interface_id format Nordic's extcap accepts at capture time is
``<device_path>-None`` (the trailing ``-None`` is the unset
"address" field). E.g. ``/dev/cu.usbmodem223101-None``.
"""
from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from serial.tools import list_ports

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
    """Return all currently connected sniffer dongles.

    Implementation note: ``extcap`` and ``timeout`` are accepted for
    backward compatibility with the pre-pyserial signature but are no
    longer used. The Nordic extcap binary's ``--extcap-interfaces``
    probe was found to silently drop most dongles on multi-device
    setups (returning 2 of 7 in the user's case). We now read USB
    descriptors directly via ``pyserial.tools.list_ports.comports``
    and synthesize the extcap interface_id from the device path.
    """
    return _enumerate_via_pyserial()


# Kept as a separate function for backward compatibility — the canvas
# imports both. Now that the slow path is fast, both call the same
# implementation and the fast/slow distinction is vestigial.
def list_dongles_fast() -> list[Dongle]:
    """Same as ``list_dongles`` — both paths are now USB-descriptor-only."""
    return _enumerate_via_pyserial()


def _enumerate_via_pyserial() -> list[Dongle]:
    """Enumerate sniffer dongles via pyserial USB-descriptor introspection.

    Includes:
      * Nordic-VID devices whose ``product`` advertises the nRF Sniffer
        firmware (any case).
      * SEGGER-VID devices likewise (DK running sniffer firmware
        appears as a SEGGER J-Link bridge).

    Each unique physical device contributes one ``Dongle``. SEGGER
    bridges expose two vcom interfaces (one is the application UART,
    the other is RTT/SWO); we dedup by ``serial_number`` and keep the
    lowest device path lexically — that's vcom 0 on macOS, which is
    where the sniffer firmware exposes its protocol.

    The extcap ``interface_id`` is constructed as ``"<device>-None"``
    to match the format Nordic's ``nrf_sniffer_ble.py`` accepts at
    capture time (the trailing ``-None`` is the unset address field
    in their internal value scheme).
    """
    by_serial: dict[str, Dongle] = {}
    seen_no_serial: list[Dongle] = []
    for p in list_ports.comports():
        if not p.vid:
            continue
        kind, display_default = _hint_for_vid(p.vid)
        if kind is None:
            continue
        product = p.product or ""
        if "sniffer" not in product.lower():
            # Skip non-sniffer firmware (e.g. SEGGER J-Link with
            # connectivity firmware on the DK). The active-probing
            # path will pick those up via a separate enumeration when
            # that infrastructure lands.
            continue
        device_path = p.device  # /dev/cu.usbmodem<short>
        if "/tty.usbmodem" in device_path:
            # pyserial usually returns /dev/cu.* on macOS, but be safe.
            continue
        # Nordic extcap accepts <device>-None as its interface_id.
        interface_id = f"{device_path}-None"
        dongle = Dongle(
            interface_id=interface_id,
            display=product or display_default or "Sniffer",
            serial_path=device_path,
            serial_number=p.serial_number or None,
            location_id_hex=None,  # pyserial's location field is a
                                    # hub-path string ("0-1.4") not a
                                    # macOS Location ID; we drop it
                                    # and sort by serial_path instead.
            usb_product=product or None,
            kind=kind,
        )
        if p.serial_number:
            existing = by_serial.get(p.serial_number)
            # Keep the lexically-lowest device_path per serial — that's
            # vcom 0 on a SEGGER J-Link bridge (the application UART,
            # which is where sniffer firmware exposes its protocol;
            # vcom 1 is RTT/SWO).
            if existing is None or device_path < existing.serial_path:
                by_serial[p.serial_number] = dongle
        else:
            seen_no_serial.append(dongle)

    found = list(by_serial.values()) + seen_no_serial
    # Stable order so UI assignments don't shuffle between scans.
    found.sort(key=lambda d: (d.serial_number or "", d.serial_path))
    return found


# ──────────────────────────────────────────────────────────────────────────
# VID/kind table — used by the pyserial enumerator to filter to candidates
# we recognize. The pyserial probe is fast enough that the historical
# fast/slow split is no longer needed.
# ──────────────────────────────────────────────────────────────────────────

_SNIFFER_VID_HINTS: tuple[tuple[int, str, str], ...] = (
    # (vendor_id, kind, default_display)
    (usb_info.SEGGER_VID,    "dk",     "SEGGER J-Link (nRF5340 DK)"),
    (usb_info.NORDIC_VID,    "dongle", "Nordic nRF Sniffer dongle"),
    (usb_info.SILABS_VID,    "dongle", "Silicon Labs USB-to-UART (Adafruit Bluefruit LE)"),
    (usb_info.FTDI_VID,      "dongle", "FTDI USB-to-UART"),
    (usb_info.PROLIFIC_VID,  "dongle", "Prolific USB-to-UART"),
    (usb_info.CH340_VID,     "dongle", "CH340 USB-to-UART"),
)


def _hint_for_vid(vid: int) -> tuple[str | None, str | None]:
    """Return (kind, display) hint for a recognized vendor, else (None, None)."""
    for v, kind, display in _SNIFFER_VID_HINTS:
        if vid == v:
            return kind, display
    return None, None


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
