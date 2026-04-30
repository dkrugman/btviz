"""Apple Continuity Protocol catalog + per-type decoder.

Shared utility for any signal that needs to read Apple Continuity
TLVs at a level richer than "blob of bytes." Built from a combination
of public reverse-engineering work:

  * FuriousMAC project's continuity catalog (the canonical open
    reference for Apple Continuity types and their layouts)
  * "Discontinued Privacy: Personal Data Leaks in Apple Bluetooth
    Low Energy Continuity Protocols" (Celosia & Cunche, 2020)
  * AirPods Battery / Pairing protocol writeups by various researchers
  * Bluetooth Core Specification 5.4 (PDU framing)

The protocol is a sequence of TLVs embedded in the Apple-CID
manufacturer-specific advertising data:

    [0x4C, 0x00, <type><length><payload>, <type><length><payload>, ...]

Each type encodes a different piece of Apple-ecosystem state:
device-pairing intent, app handoff context, AirPlay availability,
Find-My anchor data, etc. Some payload bytes are stable per
physical device + state (model id, action type, hardware capability
flags) and some rotate every ~15 minutes alongside the BLE address.

For clustering purposes we care most about:

  * **What types** a device emits (Apple class fingerprint)
  * **Which payload bytes are stable** (cross-rotation identity)
  * **Which decoded fields** are human-readable (so log lines /
    UI can say "AirPods Pro 2nd gen" instead of a hex blob)

Coverage note: every type definition here is a best-effort match
against captured payloads. Apple revises the encodings between iOS
releases; treat decoded fields as advisory. The TLV-level parsing
(types, lengths, stable-byte prefix counts) is solid because it's
spec-grounded.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

APPLE_CID_BE = b"\x4c\x00"   # little-endian 0x004C as it appears on the wire

# ──────────────────────────────────────────────────────────────────────────
# Type catalog
# ──────────────────────────────────────────────────────────────────────────

# Authoritative-as-of-today names for each Continuity TLV type byte.
# Names are drawn from FuriousMAC and Celosia-Cunche; entries marked
# "?" are types observed in the wild but without a confirmed name.
CONTINUITY_TYPES: dict[int, str] = {
    0x01: "unknown_01",          # observed; semantics not pinned down
    0x02: "iBeacon",
    0x03: "AirPrint",
    0x05: "AirDrop",
    0x06: "HomeKit",
    0x07: "ProximityPairing",    # AirPods + Beats hardware advertise this
    0x08: "HeySiri",
    0x09: "AirPlaySource",       # speakers / Apple TV broadcasting playable content
    0x0A: "AirPlayTarget",
    0x0B: "MagicSwitch",
    0x0C: "Handoff",             # encrypted user-activity continuation
    0x0D: "TetheringTarget",
    0x0E: "TetheringSource",
    0x0F: "NearbyAction",        # "Tap to Set Up" prompts on nearby iPhones
    0x10: "NearbyInfo",          # action-type + status flags + 4B auth tag
    0x11: "FindMyOrFamily",      # Find-My or family sharing contexts
    0x12: "Pairing",             # state codes (short) or Find-My anchor (long)
    0x13: "unknown_13",
    0x14: "unknown_14",
    0x16: "unknown_16",
}

# Number of leading payload bytes that are stable across an RPA
# rotation for each type. The remainder is either an encrypted auth
# tag, a rotating session counter, or volatile state (battery /
# transient flags) that changes too often to fingerprint reliably.
#
# 0 means "no stable prefix" — payload entirely rotates with the
# address. Such types contribute via type-set fingerprinting only.
STABLE_PREFIX_BYTES: dict[int, int] = {
    0x07: 4,   # ProximityPairing: byte 0 state-flags + bytes 1-2 model + byte 3 status
    0x09: 4,   # AirPlaySource: service descriptor stable bytes
    0x0A: 4,   # AirPlayTarget: similar
    0x0B: 2,   # MagicSwitch: top-byte action codes are stable
    0x0C: 0,   # Handoff: entirely encrypted, no stable prefix (matches via exact full-payload only)
    0x0D: 2,   # TetheringTarget: capability bits stable
    0x0E: 2,   # TetheringSource
    0x0F: 4,   # NearbyAction: action type + flags
    0x10: 2,   # NearbyInfo: action nibble + status flags survive rotation
    0x11: 1,   # FindMyOrFamily: top byte indicates context
    0x12: 1,   # Pairing: state byte stable; for Find-My variant most bytes do rotate
    0x16: 4,   # observed stable prefix in 12-byte type 0x16 payloads
}

# AirPods / Beats model ids embedded in ProximityPairing (type 0x07)
# at bytes 1-2 (big-endian). Catalog drawn from public lookups +
# adoptions of Apple's pairing schema. Not exhaustive; new product
# launches add to it. Unknown model bytes are kept as the raw hex
# pair so the decoded view is still informative.
AIRPODS_MODEL_BY_BYTES: dict[bytes, str] = {
    b"\x02\x20": "AirPods 1st gen",
    b"\x0F\x20": "AirPods 2nd gen",
    b"\x13\x20": "AirPods 3rd gen",
    b"\x19\x20": "AirPods 4th gen",
    b"\x1B\x20": "AirPods 4th gen w/ANC",
    b"\x0E\x20": "AirPods Pro 1st gen",
    b"\x14\x20": "AirPods Pro 2nd gen",
    b"\x24\x20": "AirPods Pro 2nd gen (USB-C)",
    b"\x05\x20": "AirPods Max",
    b"\x1F\x20": "AirPods Max (USB-C)",
    b"\x03\x20": "PowerBeats 3",
    b"\x06\x20": "PowerBeats Pro",
    b"\x0B\x20": "Beats Solo Pro",
    b"\x0C\x20": "PowerBeats 4",
    b"\x10\x20": "Beats Flex",
    b"\x11\x20": "Beats Studio Buds",
    b"\x17\x20": "Beats Studio Pro",
    b"\x18\x20": "Beats Solo 4",
    b"\x1A\x20": "Beats Studio Buds+",
}

# NearbyInfo action codes (top 4 bits of byte 0). Action is the
# user-facing iOS state; the bottom 4 bits are action-specific flags.
# Names from public iOS reverse engineering; some are advisory.
NEARBY_INFO_ACTIONS: dict[int, str] = {
    0x0: "ActivityLevel",        # activity reporting / generic info
    0x1: "VPN",
    0x2: "SetupNew",
    0x3: "WatchLockScreen",
    0x4: "TVTransferAuthority",
    0x5: "MeshRelay",
    0x6: "AutoUnlock",
    0x7: "ScreenSharing",
    0x8: "LiveAudio",
    0x9: "TargetPresence",
    0xA: "AudioTransfer",
    0xB: "FollowUpScreen",
    0xC: "ProximityPair",
    0xD: "PairCompletion",
    0xE: "Unknown",
}


# ──────────────────────────────────────────────────────────────────────────
# Parsed TLV representation
# ──────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ContinuityTLV:
    """One parsed (type, payload) entry with rich metadata."""

    type: int
    type_name: str
    payload: bytes
    stable_prefix: bytes        # the leading bytes that don't rotate
    decoded: dict[str, Any] = field(default_factory=dict)

    @property
    def payload_hex(self) -> str:
        return self.payload.hex()

    @property
    def stable_prefix_hex(self) -> str:
        return self.stable_prefix.hex()


# ──────────────────────────────────────────────────────────────────────────
# Parsing
# ──────────────────────────────────────────────────────────────────────────

def parse_continuity(blob: bytes) -> list[ContinuityTLV]:
    """Parse one Apple-CID mfg_data blob into a list of typed TLVs.

    Returns an empty list when the CID isn't Apple, the blob is
    truncated past usefulness, or no TLVs decode cleanly.

    Each TLV's ``decoded`` field is filled with type-specific
    structured info where the protocol layout is well-understood
    (see _DECODERS below). For unknown types or short payloads the
    field stays empty — caller falls back to ``payload_hex``.
    """
    if len(blob) < 4 or blob[:2] != APPLE_CID_BE:
        return []
    out: list[ContinuityTLV] = []
    i = 2
    n = len(blob)
    while i + 1 < n:
        t = blob[i]
        length = blob[i + 1]
        payload_start = i + 2
        payload_end = payload_start + length
        if payload_end > n:
            break  # truncated TLV — discard and stop
        payload = bytes(blob[payload_start:payload_end])
        stable_n = min(STABLE_PREFIX_BYTES.get(t, 0), len(payload))
        decoded = _decode_payload(t, payload)
        out.append(ContinuityTLV(
            type=t,
            type_name=CONTINUITY_TYPES.get(t, f"type_0x{t:02X}"),
            payload=payload,
            stable_prefix=payload[:stable_n],
            decoded=decoded,
        ))
        i = payload_end
    return out


# ──────────────────────────────────────────────────────────────────────────
# Per-type decoders
# ──────────────────────────────────────────────────────────────────────────

def _decode_proximity_pairing(payload: bytes) -> dict[str, Any]:
    """AirPods / Beats ProximityPairing (type 0x07).

    Layout (best-effort):
      byte 0:        flags (top nibble = state, bottom = ?)
      bytes 1-2:     model id (big-endian)
      byte 3:        status flags (lid, in-case, etc.)
      bytes 4-5:     left+right battery + charging state nibbles
      byte 6:        case battery + charging state nibble
      byte 7:        lid open count + battery state
      bytes 8-15:    color + connection metadata
      bytes 16+:     encrypted payload (rotates)
    """
    if len(payload) < 4:
        return {}
    model_bytes = payload[1:3]
    out: dict[str, Any] = {
        "model_bytes": model_bytes.hex(),
        "model_name": AIRPODS_MODEL_BY_BYTES.get(model_bytes, "unknown"),
        "state_byte": payload[0],
    }
    if len(payload) >= 4:
        out["status_byte"] = payload[3]
    if len(payload) >= 7:
        # Battery nibbles. Each value 0-9 represents 0-90% in 10% steps;
        # 0xF means "unknown."
        b_left  = (payload[4] >> 4) & 0xF
        b_right = payload[4] & 0xF
        b_case  = (payload[5] >> 4) & 0xF
        out.update({
            "battery_left_pct":  None if b_left  == 0xF else b_left  * 10,
            "battery_right_pct": None if b_right == 0xF else b_right * 10,
            "battery_case_pct":  None if b_case  == 0xF else b_case  * 10,
        })
    return out


def _decode_nearby_info(payload: bytes) -> dict[str, Any]:
    """NearbyInfo (type 0x10).

    Layout:
      byte 0: action (top 4 bits) + flags (bottom 4 bits)
      byte 1: status flags
      bytes 2..N: auth tag (rotates)
    """
    if len(payload) < 1:
        return {}
    action_code = (payload[0] >> 4) & 0x0F
    action_flags = payload[0] & 0x0F
    out: dict[str, Any] = {
        "action_code": action_code,
        "action_name": NEARBY_INFO_ACTIONS.get(action_code, "unknown"),
        "action_flags": action_flags,
    }
    if len(payload) >= 2:
        status = payload[1]
        out["status_byte"] = status
        # Common status bits as documented in iOS:
        out["wifi_on"] = bool(status & 0x80)
        out["airpods_connected"] = bool(status & 0x40)
        out["authenticated"] = bool(status & 0x20)
    return out


def _decode_pairing(payload: bytes) -> dict[str, Any]:
    """Pairing (type 0x12) — variants by length.

    Length 2:    state code (e.g. 0x0003 = pairing in progress)
    Length 4-6:  short auth / unknown
    Length 25+:  Find-My broadcast — anchor public-key derivation
    """
    out: dict[str, Any] = {"variant": _pairing_variant(len(payload))}
    if len(payload) == 2:
        out["state_code"] = (payload[0] << 8) | payload[1]
    return out


def _pairing_variant(n: int) -> str:
    if n == 2:
        return "state_code"
    if n in (4, 5, 6):
        return "short_auth"
    if n >= 24:
        return "find_my_anchor"
    return "unknown_length"


_DECODERS = {
    0x07: _decode_proximity_pairing,
    0x10: _decode_nearby_info,
    0x12: _decode_pairing,
}


def _decode_payload(t: int, payload: bytes) -> dict[str, Any]:
    fn = _DECODERS.get(t)
    if fn is None:
        return {}
    try:
        return fn(payload)
    except Exception:  # noqa: BLE001 — decoder bug must not corrupt the parser
        return {}
