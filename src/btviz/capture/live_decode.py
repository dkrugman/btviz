"""In-process raw → ``Packet`` decoder for live capture.

The live-capture path receives ``RawPacket`` objects (Nordic pseudo-header
+ BLE LL bytes) from the bus and needs to feed them into the same
``record_packet()`` helper that file ingest uses. ``record_packet()``
reads identity / Auracast clues from a tshark-shaped
``pkt.extras["layers"]`` dict.

We don't run tshark per packet (subprocess overhead would crater the
hot path). Instead this module:

  1. Parses the Nordic-pseudo-header + BLE LL packet using the existing
     in-process decoder in :mod:`btviz.decode.adv` — gets channel, RSSI,
     PDU type, advertiser address, and the raw advertising-data byte
     stream.
  2. Walks the AD-structure stream and synthesizes a tshark-shaped
     ``layers`` dict containing the same per-AD-entry keys
     ``record_packet()`` looks for (device_name, company_id, appearance,
     broadcast_name, plus the parallel ``type`` / ``uuid_16`` /
     ``service_data`` arrays needed for Auracast detection).

Tradeoffs vs. tshark dissection:

  * **Adv only.** Data-channel packets (LL control PDUs, ATT/L2CAP, ISO)
    aren't dissected — the underlying decoder returns None for them, and
    we return None too. Devices keep getting upserted via their adv
    packets; deeper protocol detail awaits a future tshark-streaming
    integration.
  * **No PHY beyond 1M.** Standard nRF Sniffer firmware captures on the
    1M PHY by default; 2M / Coded require additional config that we
    don't surface yet.
  * **Same enrichment, same Auracast detection.** Apple Continuity sub-
    type classification, OUI-vendor lookups, broadcast_name extraction,
    and BAA-service-data → broadcast_id all work identically because
    they read from the synthesized layers dict.
"""
from __future__ import annotations

import struct
from typing import Any

from ..decode.adv import (
    AD_COMPLETE_LIST_16,
    AD_COMPLETE_LOCAL_NAME,
    AD_INCOMPLETE_LIST_16,
    AD_MANUFACTURER_DATA,
    AD_SERVICE_DATA_16,
    AD_SHORTENED_LOCAL_NAME,
    classify_address,
    decode_nbe_packet,
    decode_phdr_packet,
    parse_ad_structures,
)
from .packet import Packet

# Pcap link-types we know how to dispatch on. Anything else falls back
# to the legacy LE_LL_WITH_PHDR layout — which historically produced
# usable output for files we've ingested, so it's the safer default
# than failing closed.
_DLT_BLUETOOTH_LE_LL_WITH_PHDR = 256
_DLT_NORDIC_BLE = 272

# AD types we map into the synthesized tshark layers dict.
_AD_APPEARANCE = 0x19
_AD_BROADCAST_NAME = 0x30
_AD_TX_POWER = 0x0A

# Nordic Sniffer firmware default PHY — standard FW only sniffs 1M unless
# explicitly told to capture Coded; 2M would surface as a different
# field in the Nordic header that decode_phdr_packet doesn't expose.
_DEFAULT_LIVE_PHY = "1M"


def decode_live_packet(
    raw_bytes: bytes, *, source: str, ts: float, dlt: int | None = None,
) -> Packet | None:
    """Decode a Nordic pcap record into a ``Packet``.

    ``dlt`` is the pcap link-type from the file's global header. Two
    formats are in active use today:

      * 256 = ``BLUETOOTH_LE_LL_WITH_PHDR`` — the classic 10-byte Nordic
        header (older firmware, also what tshark dissects against).
      * 272 = ``NORDIC_BLE`` — current Nordic Sniffer firmware, with a
        17-byte v2 header (board id, packet/event counters, timestamp
        delta) preceding the BLE LL frame. Channel and RSSI live at
        offsets 9 and 10 instead of 0 and 1.

    Falls back to the DLT-256 decoder when ``dlt`` is None (callers that
    don't know — historically the only path).

    Returns None if the bytes aren't a recognizable BLE advertising
    packet (data-channel / non-adv / malformed). Caller can drop those.
    """
    if dlt == _DLT_NORDIC_BLE:
        decoded = decode_nbe_packet(raw_bytes)
    else:
        decoded = decode_phdr_packet(raw_bytes)
    if decoded is None:
        return None

    adv_addr = decoded.adv_addr
    addr_type = (
        classify_address(adv_addr, decoded.tx_add_random)
        if adv_addr else None
    )

    ad_entries = parse_ad_structures(decoded.adv_data) if decoded.adv_data else []
    layers = {"btle": _synth_btle_layer(ad_entries)}

    return Packet(
        ts=ts,
        source=source,
        channel=decoded.channel,
        rssi=decoded.rssi,
        phy=_DEFAULT_LIVE_PHY,
        pdu_type=decoded.pdu_type,
        adv_addr=adv_addr,
        adv_addr_type=addr_type,
        init_addr=None,    # would need further LL-PDU parsing
        target_addr=None,  # ditto
        adv_data=decoded.adv_data,
        raw=raw_bytes,
        extras={"layers": layers},
    )


# ──────────────────────────────────────────────────────────────────────────
# Synthesizing the tshark "layers" dict
# ──────────────────────────────────────────────────────────────────────────

# Keys that record_packet / _extract_ad_clues / parse_auracast read from
# layers["btle"]. We populate these in the same shape tshark's EK output
# uses (parallel arrays for repeated AD entries; scalars or strings for
# single-occurrence fields).
_K_TYPE = "btcommon_btcommon_eir_ad_entry_type"
_K_UUID_16 = "btcommon_btcommon_eir_ad_entry_uuid_16"
_K_SERVICE_DATA = "btcommon_btcommon_eir_ad_entry_service_data"
_K_DEVICE_NAME = "btcommon_btcommon_eir_ad_entry_device_name"
_K_COMPANY_ID = "btcommon_btcommon_eir_ad_entry_company_id"
_K_DATA = "btcommon_btcommon_eir_ad_entry_data"
_K_APPEARANCE = "btcommon_btcommon_eir_ad_entry_appearance"
_K_BROADCAST_NAME = "btcommon_btcommon_eir_ad_entry_broadcast_name"


def _synth_btle_layer(ad_entries: list[tuple[int, bytes]]) -> dict[str, Any]:
    """Build the tshark-shaped layer dict from parsed AD entries.

    For repeated AD types (Service Data 16, list-of-UUID-16) we emit
    parallel arrays so the existing pairing logic in pipeline /
    auracast.py walks them correctly. Single-occurrence fields
    (device_name, appearance, broadcast_name) are written as scalars.
    Manufacturer-data fields likewise (company_id + data).
    """
    layer: dict[str, Any] = {}
    types: list[str] = []
    uuids_16: list[str] = []
    service_data: list[str] = []

    for ad_type, value in ad_entries:
        types.append(str(ad_type))

        if ad_type in (AD_SHORTENED_LOCAL_NAME, AD_COMPLETE_LOCAL_NAME):
            try:
                name = value.decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                name = ""
            if name:
                layer[_K_DEVICE_NAME] = name

        elif ad_type == AD_MANUFACTURER_DATA and len(value) >= 2:
            cid = struct.unpack("<H", value[:2])[0]
            layer[_K_COMPANY_ID] = str(cid)
            # tshark formats the trailing data as colon-separated hex.
            tail = value[2:]
            layer[_K_DATA] = ":".join(f"{b:02x}" for b in tail) if tail else ""

        elif ad_type == _AD_APPEARANCE and len(value) >= 2:
            ap = struct.unpack("<H", value[:2])[0]
            layer[_K_APPEARANCE] = str(ap)

        elif ad_type == _AD_BROADCAST_NAME:
            try:
                bn = value.decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                bn = ""
            if bn:
                layer[_K_BROADCAST_NAME] = bn

        elif ad_type in (AD_INCOMPLETE_LIST_16, AD_COMPLETE_LIST_16):
            # Multiple 16-bit UUIDs in one AD entry. Add each to the
            # uuid_16 array.
            for off in range(0, len(value) - 1, 2):
                u = struct.unpack("<H", value[off:off + 2])[0]
                uuids_16.append(str(u))

        elif ad_type == AD_SERVICE_DATA_16 and len(value) >= 2:
            # 16-bit UUID + payload. Both parallel-arrays.
            u = struct.unpack("<H", value[:2])[0]
            uuids_16.append(str(u))
            payload = value[2:]
            service_data.append(":".join(f"{b:02x}" for b in payload))

    # Only emit the parallel-array keys when populated (mirrors tshark's
    # behavior: missing keys when no entry of that family was present).
    if types:
        layer[_K_TYPE] = types
    if uuids_16:
        layer[_K_UUID_16] = uuids_16
    if service_data:
        layer[_K_SERVICE_DATA] = service_data

    return layer
