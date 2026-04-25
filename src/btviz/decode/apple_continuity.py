"""Decode Apple Continuity advertising payloads.

Apple has never published a spec for its BLE Continuity protocols beyond
iBeacon (sub-type 0x02). Everything here is reverse-engineered by the
security research community.

Source-of-truth references (no official feed exists):
  * furiousMAC/continuity (US Naval Academy researchers): the de facto
    canonical reference for sub-type bytes and payload formats.
    https://github.com/furiousMAC/continuity
  * Wireshark's Bluetooth dissectors: pull from furiousMAC; new sub-types
    typically land within a release of community discovery.
  * Hexway / Damien Cauquil writeups: occasional updates around new iOS
    releases.

To check for updates: watch furiousMAC commits and Wireshark release
notes for BLE / btcommon dissector changes. Apple adds new sub-types
roughly once a year, usually around fall iOS releases.

Design — defensive parsing:
  * Walking the TLV chain catches truncation: a malformed entry produces
    an entry with parse_error set instead of raising.
  * Per-sub-type parsers are wrapped in try/except. Unknown sub-types
    still produce an entry with the raw bytes preserved.
  * No exception escapes ``parse_continuity()``. Calling code can rely
    on always getting a list, possibly empty.

The aim is "bend, not break" so unknown / future / corrupted payloads
degrade gracefully into "we saw type 0x?? but couldn't decode" rather
than crashing the ingest pipeline.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

# ──────────────────────────────────────────────────────────────────────────
# Sub-type table
# ──────────────────────────────────────────────────────────────────────────
# Names follow furiousMAC's vocabulary so future readers can cross-reference.
# Sub-types we don't decode payloads for still get a name from this table —
# tracking which sub-types appear is itself useful identity evidence.

_TYPE_NAMES: dict[int, str] = {
    0x01: "iCloud",
    0x02: "iBeacon",
    0x03: "AirPrint",
    0x05: "AirDrop",
    0x06: "HomeKit",
    0x07: "AirPods",                    # proximity pairing
    0x08: "Hey Siri",
    0x09: "AirPlay Target",
    0x0A: "AirPlay Source",
    0x0B: "Magic Switch",
    0x0C: "Handoff",
    0x0D: "WiFi Settings",
    0x0E: "Instant Hotspot",
    0x0F: "WiFi Join Network",
    0x10: "Nearby Info",                # iPhone / iPad / Mac status
    0x11: "Apple Watch",                # legacy
    0x12: "Find My",                    # offline finding (non-AirTag)
    0x14: "AirPods Tile",
    0x15: "AirPods Tile (alt)",
    0x16: "AirTag",                     # offline finding (lost mode)
}


# AirPods model code → product name. Model code is 2 bytes at payload[1:3].
# Source: furiousMAC + AirPods firmware analysis (community).
_AIRPODS_MODELS: dict[int, str] = {
    0x0220: "AirPods (1st gen)",
    0x0F20: "AirPods (2nd gen)",
    0x1320: "AirPods (3rd gen)",
    0x1920: "AirPods (4th gen)",
    0x1B20: "AirPods (4th gen, ANC)",
    0x0E20: "AirPods Pro",
    0x1420: "AirPods Pro (2nd gen)",
    0x2420: "AirPods Pro (2nd gen, USB-C)",
    0x0A20: "AirPods Max",
    0x1F20: "AirPods Max (USB-C)",
    0x0520: "BeatsX",
    0x0620: "Beats Solo3",
    0x0920: "BeatsStudio3",
    0x0B20: "Powerbeats3",
    0x0C20: "Beats Solo Pro",
    0x1020: "Powerbeats Pro",
    0x1120: "Beats Flex",
    0x1720: "Beats Studio Buds",
    0x1820: "Beats Fit Pro",
    0x1D20: "Beats Studio Pro",
    0x2520: "Beats Solo 4",
    0x2620: "Beats Studio Buds+",
}


# Nearby Info action codes (lower nibble of the status byte). These
# represent USER ACTIONS, not device types directly — Apple uses different
# action ranges across iPhone / iPad / Mac so they can be a heuristic for
# device class but aren't a clean mapping.
_NEARBY_ACTIONS: dict[int, str] = {
    0x00: "activity_unspecified",
    0x01: "ringtone",
    0x03: "lock_screen",
    0x05: "transition",                  # iPhone unlocking, etc.
    0x07: "ringing",
    0x09: "transferring_call",
    0x0A: "active_user",                 # primary device, screen on
    0x0B: "audio_playing",
    0x0C: "active_user_with_screen",
    0x0D: "watch_lock_screen",
    0x0E: "tentative_pairing",
    0x0F: "wake",                        # Mac wake
}


# Nearby Info status flags (upper nibble of the status byte).
_NEARBY_FLAG_BITS: dict[int, str] = {
    0x01: "primary_iCloud",
    0x02: "AirPods_connected",
    0x04: "auth_tag_present",
    0x08: "WiFi_on",
}


# ──────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────

@dataclass
class ContinuityEntry:
    """One TLV entry from an Apple manufacturer-data payload."""
    type_byte: int
    type_name: str                # known label or f"unknown_0x{byte:02x}"
    payload: bytes                # raw payload bytes (length as advertised)
    parsed: dict[str, Any] | None # decoded fields when a parser succeeded
    parse_error: str | None       # exception text when a parser raised


def parse_continuity(data: bytes) -> list[ContinuityEntry]:
    """Walk an Apple Continuity TLV chain. Always returns a list.

    ``data`` is the manufacturer-data bytes AFTER the 2-byte company ID
    (0x4C 0x00). A truncated final entry produces an entry with
    ``parse_error="truncated payload"`` and parsing stops; bytes preceding
    it are still returned.

    No exception escapes this function — corrupted bytes degrade into
    entries with ``parse_error`` set. Callers can iterate the result and
    use ``parsed`` if non-None; the raw ``payload`` is always available
    for forensic logging.
    """
    if not data:
        return []

    entries: list[ContinuityEntry] = []
    i = 0
    n = len(data)
    while i + 2 <= n:
        type_byte = data[i]
        length = data[i + 1]
        i += 2
        if i + length > n:
            entries.append(ContinuityEntry(
                type_byte=type_byte,
                type_name=_name_for(type_byte),
                payload=bytes(data[i:n]),
                parsed=None,
                parse_error=f"truncated: declared {length}, have {n - i}",
            ))
            return entries
        payload = bytes(data[i:i + length])
        i += length
        entries.append(_parse_one(type_byte, payload))
    return entries


def classify(entries: list[ContinuityEntry]) -> tuple[str | None, str | None]:
    """Pick a (device_class, model) from a set of seen Continuity entries.

    Priority: AirPods (most specific, includes a model code) > AirTag >
    Apple Watch > Find My > Nearby Info (generic Apple device) > AirPlay >
    HomeKit > everything else. Returns (None, None) if nothing classifies.
    """
    by_type = {e.type_byte: e for e in entries}

    if 0x07 in by_type:
        e = by_type[0x07]
        model = (e.parsed or {}).get("model")
        return ("airpods", model)

    if 0x16 in by_type:
        return ("airtag", "AirTag")

    if 0x11 in by_type:
        return ("apple_watch", "Apple Watch")

    if 0x12 in by_type:
        # Find My beacon from a non-AirTag iOS device. Class is generic.
        return ("apple_device", None)

    if 0x10 in by_type:
        # Nearby Info from a primary iOS / macOS device.
        return ("apple_device", None)

    if 0x09 in by_type or 0x0A in by_type:
        # AirPlay endpoint — Apple TV / HomePod / Mac.
        return ("apple_airplay", None)

    if 0x06 in by_type:
        return ("homekit", None)

    if 0x02 in by_type:
        return ("ibeacon", None)

    return (None, None)


# ──────────────────────────────────────────────────────────────────────────
# Internals
# ──────────────────────────────────────────────────────────────────────────

def _name_for(type_byte: int) -> str:
    return _TYPE_NAMES.get(type_byte, f"unknown_0x{type_byte:02x}")


def _parse_one(type_byte: int, payload: bytes) -> ContinuityEntry:
    name = _name_for(type_byte)
    parser = _PARSERS.get(type_byte)
    if parser is None:
        return ContinuityEntry(type_byte, name, payload, None, None)
    try:
        parsed = parser(payload)
    except Exception as exc:  # noqa: BLE001 — defensive by design
        return ContinuityEntry(type_byte, name, payload, None, repr(exc))
    return ContinuityEntry(type_byte, name, payload, parsed, None)


# Each parser takes the payload bytes (without the type/length header) and
# returns a dict of decoded fields. Parsers raise on malformed input — the
# wrapper above turns the exception into parse_error.

def _parse_ibeacon(p: bytes) -> dict[str, Any]:
    # Apple-published spec. Fixed 23 bytes: 0x02 0x15 + 16 UUID + 2 major
    # + 2 minor + 1 tx_power. We're called with the bytes AFTER the type
    # and length, so the leading 0x02 0x15 is already gone — wait, no.
    # The Apple manuf-data wraps each sub-type with type+length, where the
    # sub-type byte is part of the OUTER chain. iBeacon's own spec puts
    # 0x02 0x15 right after the company ID, which IS our outer type/length
    # pair. So payload starts directly with the 16-byte UUID.
    if len(p) < 21:
        raise ValueError(f"iBeacon payload too short: {len(p)}")
    return {
        "uuid": p[0:16].hex(),
        "major": int.from_bytes(p[16:18], "big"),
        "minor": int.from_bytes(p[18:20], "big"),
        "tx_power": int.from_bytes(p[20:21], "big", signed=True),
    }


def _parse_airpods(p: bytes) -> dict[str, Any]:
    # AirPods proximity-pairing payload (sub-type 0x07). Length varies
    # across firmware (commonly 25 or 27 bytes). Fields after the model
    # code are encrypted on newer firmware so we expose the model and the
    # status nibble; deeper fields (battery, lid state) are best-effort.
    if len(p) < 3:
        raise ValueError(f"AirPods payload too short: {len(p)}")
    model_code = int.from_bytes(p[1:3], "big")
    out: dict[str, Any] = {
        "model_code": model_code,
        "model": _AIRPODS_MODELS.get(model_code) or f"AirPods (0x{model_code:04x})",
    }
    # Status / battery / lid — only attempt if length permits. These offsets
    # are documented for older firmwares; newer ones may scramble the bytes.
    if len(p) >= 5:
        out["status_byte"] = p[3]
        battery_byte = p[4]
        out["battery_left_pct"] = (battery_byte & 0x0F) * 10  # 0xF = unknown
        out["battery_right_pct"] = ((battery_byte >> 4) & 0x0F) * 10
    if len(p) >= 6:
        lid = p[5]
        out["lid_open_count"] = lid & 0x0F
        out["case_battery_pct"] = ((lid >> 4) & 0x0F) * 10
    return out


def _parse_nearby(p: bytes) -> dict[str, Any]:
    # Nearby Info (0x10): 1 status byte + auth tag (3-byte preview) +
    # optional 16-byte authenticated key. We surface action / flags and
    # whether a tag is present; the tag itself isn't useful without the
    # IRK.
    if len(p) < 1:
        raise ValueError("Nearby payload empty")
    status = p[0]
    action = status & 0x0F
    flags_byte = (status >> 4) & 0x0F
    flags = [name for bit, name in _NEARBY_FLAG_BITS.items() if flags_byte & bit]
    return {
        "status_byte": status,
        "action_code": action,
        "action": _NEARBY_ACTIONS.get(action, f"action_0x{action:02x}"),
        "flags_byte": flags_byte,
        "flags": flags,
        "auth_tag_present": len(p) > 1,
        "auth_tag_len": len(p) - 1 if len(p) > 1 else 0,
    }


def _parse_airtag(p: bytes) -> dict[str, Any]:
    # AirTag offline-finding (0x16): 1 status byte + 22-byte truncated
    # public key + 1 hint byte. We just expose status flags + key length;
    # the public key itself rotates every 15 min and is opaque without
    # the owner's master key.
    if len(p) < 1:
        raise ValueError("AirTag payload empty")
    status = p[0]
    out = {
        "status_byte": status,
        "battery_state": (status >> 6) & 0x03,  # 0=full, 1=med, 2=low, 3=critical
        "key_len": max(0, len(p) - 1),
    }
    return out


def _parse_findmy(p: bytes) -> dict[str, Any]:
    # Find My non-AirTag offline-finding (0x12). Format mirrors AirTag at
    # the byte level but the device is an iPhone/iPad/Mac in offline mode.
    if len(p) < 1:
        raise ValueError("Find My payload empty")
    return {
        "status_byte": p[0],
        "battery_state": (p[0] >> 6) & 0x03,
        "key_len": max(0, len(p) - 1),
    }


def _parse_handoff(p: bytes) -> dict[str, Any]:
    # Handoff (0x0C): 1 clipboard byte + 2 IV + 1 auth tag size + 16
    # encrypted payload. Encrypted, so just expose presence + IV.
    if len(p) < 4:
        raise ValueError(f"Handoff payload too short: {len(p)}")
    return {
        "clipboard_status": p[0],
        "iv": p[1:3].hex(),
        "auth_tag_size": p[3],
        "encrypted_len": max(0, len(p) - 4),
    }


def _parse_homekit(p: bytes) -> dict[str, Any]:
    # HomeKit (0x06): status flag + device id + accessory category +
    # global state # + config #.
    if len(p) < 13:
        raise ValueError(f"HomeKit payload too short: {len(p)}")
    return {
        "status_flag": p[0],
        "device_id": p[1:7][::-1].hex(":"),  # AID, little-endian on wire
        "accessory_category": int.from_bytes(p[7:9], "little"),
        "global_state_num": int.from_bytes(p[9:11], "little"),
        "config_num": p[11],
        "compat_version": p[12],
    }


_PARSERS: dict[int, Callable[[bytes], dict[str, Any]]] = {
    0x02: _parse_ibeacon,
    0x06: _parse_homekit,
    0x07: _parse_airpods,
    0x0C: _parse_handoff,
    0x10: _parse_nearby,
    0x12: _parse_findmy,
    0x16: _parse_airtag,
}
