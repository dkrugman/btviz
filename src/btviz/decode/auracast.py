"""Decode Auracast broadcast metadata from advertising packets.

Looks for Bluetooth SIG Service UUIDs and AD types specific to LE Audio
broadcasts (Auracast):

  - 0x1852 — Broadcast Audio Announcement Service (BAA): 3-byte
            Broadcast_ID identifies the broadcaster's stream.
  - 0x1856 — Public Broadcast Announcement Service (PBA): 1-byte
            features (bit 0 = encrypted), then 1-byte metadata length,
            then optional LTV metadata.
  - AD type 0x30 — Broadcast Name (UTF-8 string).
  - btcommon.eir_ad.entry.biginfo.* — full BIGInfo parameters if the
            sniffer captured a Periodic Advertising packet carrying it.
            Standard nRF Sniffer firmware doesn't reliably sync to PA,
            so BIGInfo is opportunistic. Most fields stay None until a
            PA-syncing tool (e.g. auracast-hackers-toolkit) fills them in.

Used by the ingest pipeline to populate the ``broadcasts`` table when
ADV_EXT_IND / AUX_ADV_IND packets carrying BAA service data are observed.
BASE structure parsing is not done here — BASE lives in the Periodic
Advertising train and is structurally complex; defer until we have the
toolkit firmware feeding the same DB.

Source-of-truth references:
  - Bluetooth SIG Assigned Numbers (Service Data UUIDs, AD types)
  - Basic Audio Profile (BAP) 1.0 specification
  - Public Broadcast Profile (PBP) 1.0 specification
  - tshark's btcommon dissector (Wireshark 4.x has dedicated fields for
    broadcast_name, broadcast_code, and full biginfo decoding)

Defensive parsing — same posture as apple_continuity.py: malformed input
returns None, never raises. The caller (pipeline) treats absence of an
Auracast result as "this packet isn't Auracast" and moves on.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# 16-bit Service UUIDs.
BAA_UUID = 0x1852
PBA_UUID = 0x1856

# AD types.
AD_BROADCAST_NAME = 0x30          # 48 decimal
AD_SERVICE_DATA_16 = 0x16         # 22 decimal
AD_INCOMPLETE_LIST_16 = 0x02
AD_COMPLETE_LIST_16 = 0x03
AD_SERVICE_SOLICITATION_16 = 0x14

# Which AD types contribute entries to tshark's parallel arrays.
# Any 16-bit-UUID-bearing entry adds to btcommon...uuid_16; any service-data
# entry (16/32/128 variants) adds to btcommon...service_data. The pairing
# lines up with the order of the AD entries within the packet — we walk
# `ad_entry_type` and advance per-array indices accordingly.
_TYPES_WITH_UUID16 = {
    AD_INCOMPLETE_LIST_16,
    AD_COMPLETE_LIST_16,
    AD_SERVICE_SOLICITATION_16,
    AD_SERVICE_DATA_16,
}
_TYPES_WITH_SERVICE_DATA = {
    AD_SERVICE_DATA_16,
    0x20,                         # Service Data 32-bit
    0x21,                         # Service Data 128-bit
}

# BIGInfo PHY enumeration in tshark output (LE Audio spec values).
_PHY_NAMES = {
    1: "1M",
    2: "2M",
    4: "Coded",
}


@dataclass
class AuracastInfo:
    """Cleartext Auracast metadata extractable from a single adv packet."""
    broadcast_id: int                      # 24-bit Broadcast_ID (BAA payload)
    broadcast_name: str | None             # AD type 0x30 if present
    pba_features: int | None               # PBA features byte if present
    encrypted: bool                        # PBA features bit 0 OR BIGInfo gskd

    # BIGInfo fields — populated when the source packet was a Periodic
    # Advertising packet carrying BIGInfo. Standard nRF Sniffer captures
    # rarely include these; toolkit-firmware captures will. Each is None
    # when not observed.
    bis_count: int | None = None           # num_bis
    phy: str | None = None                 # "1M" | "2M" | "Coded"
    max_pdu: int | None = None
    sdu_interval_us: int | None = None
    max_sdu: int | None = None
    iso_interval: int | None = None        # in 1.25 ms units


# ──────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────

def parse_auracast(layers: dict) -> AuracastInfo | None:
    """Extract Auracast metadata from a tshark EK layers dict.

    Returns None if the packet doesn't carry a BAA (i.e. isn't an Auracast
    announcement). Any other parse failures degrade to partial data —
    e.g. broadcast_id present but PBA features / BIGInfo not parseable
    still returns an AuracastInfo with the available fields.
    """
    if not isinstance(layers, dict):
        return None
    btle = layers.get("btle")
    if not isinstance(btle, dict):
        return None

    # Quick reject: if there's no 0x1852 anywhere in the packet's 16-bit
    # UUID list, it's not Auracast.
    uuids = _to_list(btle.get("btcommon_btcommon_eir_ad_entry_uuid_16"))
    if not _any_uuid_equals(uuids, BAA_UUID):
        return None

    # Extract Broadcast_ID and (optionally) PBA features by walking the
    # AD-entry types alongside the parallel uuid_16 / service_data arrays.
    types = _to_list(btle.get("btcommon_btcommon_eir_ad_entry_type"))
    sds = _to_list(btle.get("btcommon_btcommon_eir_ad_entry_service_data"))
    broadcast_id, pba_features = _walk_service_data(types, uuids, sds)

    # Heuristic fallback: if pairing failed (other AD types interleaved
    # with service data in a way the walker couldn't reconcile), accept
    # the first 3-byte service_data we see as the BAA payload. We've
    # already confirmed BAA is present in this packet via the uuid check.
    if broadcast_id is None:
        for sd in sds:
            data = _hex_to_bytes(sd)
            if data is not None and len(data) == 3:
                broadcast_id = int.from_bytes(data, "little")
                break

    if broadcast_id is None:
        return None  # Couldn't extract — not useful.

    # Broadcast Name (AD type 0x30) has its own dedicated field.
    name_raw = btle.get("btcommon_btcommon_eir_ad_entry_broadcast_name")
    bn = name_raw if isinstance(name_raw, str) and name_raw else None

    # Encryption signal: bit 0 of PBA features, OR presence of BIGInfo gskd.
    encrypted = bool(pba_features and (pba_features & 0x01))

    info = AuracastInfo(
        broadcast_id=broadcast_id,
        broadcast_name=bn,
        pba_features=pba_features,
        encrypted=encrypted,
    )

    # BIGInfo fields — opportunistic. tshark exposes each as a dedicated
    # field; pull the ones that map cleanly onto schema columns.
    _maybe_attach_biginfo(info, btle)
    return info


# ──────────────────────────────────────────────────────────────────────────
# Internals
# ──────────────────────────────────────────────────────────────────────────

def _to_list(v: Any) -> list:
    """tshark EK wraps single-occurrence fields as scalars and multi-
    occurrence as lists. Normalize."""
    if v is None:
        return []
    if isinstance(v, list):
        return v
    return [v]


def _as_int(v: Any) -> int | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, int):
        return v
    try:
        s = str(v).strip()
        return int(s, 0) if s.startswith(("0x", "0X")) else int(s)
    except (TypeError, ValueError, AttributeError):
        return None


def _hex_to_bytes(s: Any) -> bytes | None:
    """tshark's service_data field comes as hex with optional ':' separators
    (e.g. ``'ac:65:b8'`` or ``'ac65b8'``). Returns None on parse failure."""
    if not isinstance(s, str) or not s:
        return None
    try:
        return bytes.fromhex(s.replace(":", "").replace(" ", ""))
    except ValueError:
        return None


def _any_uuid_equals(uuids: list, target: int) -> bool:
    for u in uuids:
        if _as_int(u) == target:
            return True
    return False


def _walk_service_data(
    types: list, uuids: list, sds: list
) -> tuple[int | None, int | None]:
    """Walk AD entries, tracking per-array indices, to find the BAA / PBA
    service-data payloads.

    Returns (broadcast_id, pba_features). Either may be None if not found
    or unparseable.
    """
    uuid_idx = 0
    sd_idx = 0
    broadcast_id: int | None = None
    pba_features: int | None = None

    for t_raw in types:
        t = _as_int(t_raw)
        if t is None:
            continue

        # Snapshot the parallel-array entries this AD type would consume.
        u = (_as_int(uuids[uuid_idx])
             if t in _TYPES_WITH_UUID16 and uuid_idx < len(uuids)
             else None)
        d = (_hex_to_bytes(sds[sd_idx])
             if t in _TYPES_WITH_SERVICE_DATA and sd_idx < len(sds)
             else None)

        # Auracast lives in 16-bit Service Data entries.
        if t == AD_SERVICE_DATA_16 and u is not None and d is not None:
            if u == BAA_UUID and len(d) >= 3 and broadcast_id is None:
                broadcast_id = int.from_bytes(d[:3], "little")
            elif u == PBA_UUID and len(d) >= 1 and pba_features is None:
                pba_features = d[0]

        # Advance whichever indices we used.
        if t in _TYPES_WITH_UUID16:
            uuid_idx += 1
        if t in _TYPES_WITH_SERVICE_DATA:
            sd_idx += 1

    return broadcast_id, pba_features


def _maybe_attach_biginfo(info: AuracastInfo, btle: dict) -> None:
    """Pull BIGInfo fields if tshark dissected any. No-op when absent."""
    BI = "btcommon_btcommon_eir_ad_entry_biginfo_"
    num_bis = _as_int(btle.get(BI + "num_bis"))
    if num_bis is not None:
        info.bis_count = num_bis
    phy = _as_int(btle.get(BI + "phy"))
    if phy is not None:
        info.phy = _PHY_NAMES.get(phy)
    info.max_pdu = _as_int(btle.get(BI + "max_pdu"))
    info.max_sdu = _as_int(btle.get(BI + "max_sdu"))
    info.sdu_interval_us = _as_int(btle.get(BI + "sdu_interval"))
    info.iso_interval = _as_int(btle.get(BI + "iso_interval"))
    # Presence of gskd implies an encrypted BIG. Only override `encrypted`
    # to True (don't downgrade if PBA features already said it's encrypted).
    if btle.get(BI + "gskd") is not None:
        info.encrypted = True
