"""Minimal BLE advertising packet decoder.

This is intentionally narrow: just enough to populate the device inventory
during the discovery phase. Full LL/L2CAP/ATT/SMP/ISO decoding will be
delegated to tshark dissection later (PDML/JSON), which is more thorough
than anything we'd reimplement here.

Input is the payload of one pcap record from the Nordic sniffer, which
prepends a "BLE LL with PHDR" pseudo-header (DLT_BLUETOOTH_LE_LL_WITH_PHDR
== 256). Layout (Nordic-specific, simplified):

  0  rf_channel   u8
  1  signal_power i8 (RSSI)
  2  noise_power  i8
  3  access_addr_offenses u8
  4  ref_access_address u32 LE
  8  flags        u16 LE
  10 ll_pdu...

Then the LL packet:
  AccessAddress u32 LE  (advertising = 0x8E89BED6)
  PDU header    u16 LE  (type/RFU/ChSel/TxAdd/RxAdd, length)
  AdvA          6 bytes LE
  AdvData       N bytes
  CRC           3 bytes
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

ADV_ACCESS_ADDR = 0x8E89BED6

PDU_TYPE_NAMES = {
    0x0: "ADV_IND",
    0x1: "ADV_DIRECT_IND",
    0x2: "ADV_NONCONN_IND",
    0x3: "SCAN_REQ",
    0x4: "SCAN_RSP",
    0x5: "CONNECT_IND",
    0x6: "ADV_SCAN_IND",
    0x7: "ADV_EXT_IND",     # extended adv (5.0)
}


@dataclass
class DecodedAdv:
    channel: int
    rssi: int
    pdu_type: str
    tx_add_random: bool
    rx_add_random: bool
    adv_addr: str | None
    adv_data: bytes
    raw_pdu_header: int


def decode_phdr_packet(buf: bytes) -> DecodedAdv | None:
    """Decode a Nordic BLE LL+PHDR pcap payload (DLT 256). None if not adv."""
    if len(buf) < 10 + 4 + 2 + 6 + 3:
        return None
    rf_channel = buf[0]
    rssi_dbm = struct.unpack("b", buf[1:2])[0]
    # buf[2] noise, buf[3] aa offenses, buf[4:8] ref AA, buf[8:10] flags
    return _decode_ll(buf[10:], channel=rf_channel, rssi=rssi_dbm)


# Nordic BLE (DLT 272) header layout, version 2 — what current Nordic
# Sniffer firmware emits. Total header is 17 bytes; fields we need are
# channel and RSSI, the rest (board id, packet/event counters, timestamp
# delta) are diagnostics we don't surface yet.
#
#   [0]      board id
#   [1-2]    header length (u16 LE)
#   [3]      header version (= 0x02)
#   [4-5]    packet counter (u16 LE)
#   [6]      protover marker (typically 0x06)
#   [7]      flags
#   [8]      <flag/reserved>
#   [9]      RF channel
#   [10]     RSSI magnitude (positive byte; actual value is negative)
#   [11-12]  event counter (u16 LE)
#   [13-16]  timestamp delta (u32 LE)
#   [17..]   BLE LL frame (AA + PDU header + payload + CRC)
_NBE_HDR_LEN = 17


def decode_nbe_packet(buf: bytes) -> DecodedAdv | None:
    """Decode a Nordic-BLE (DLT 272) pcap payload. None if not adv.

    Same return shape as ``decode_phdr_packet`` so callers can swap
    based on the pcap link-type. Channel comes from offset 9, RSSI from
    offset 10 (stored as a positive magnitude — we negate). The BLE LL
    frame begins at offset 17 and is identical in layout to DLT 256.
    """
    if len(buf) < _NBE_HDR_LEN + 4 + 2 + 6 + 3:
        return None
    rf_channel = buf[9]
    rssi_dbm = -buf[10]
    return _decode_ll(buf[_NBE_HDR_LEN:], channel=rf_channel, rssi=rssi_dbm)


def _decode_ll(ll: bytes, *, channel: int, rssi: int) -> DecodedAdv | None:
    """Parse the BLE LL frame portion (after any pcap pseudo-header).

    Shared by both DLT 256 (10-byte PHDR) and DLT 272 (17-byte NBE header)
    decoders — once the per-DLT header is stripped, the LL layout is
    identical. Returns None for data-channel / non-adv / truncated input.
    """
    if len(ll) < 4 + 2 + 6 + 3:
        return None
    aa = struct.unpack("<I", ll[:4])[0]
    if aa != ADV_ACCESS_ADDR:
        return None  # data channel packet (connection); not for inventory
    hdr = struct.unpack("<H", ll[4:6])[0]
    pdu_type = hdr & 0x0F
    tx_add = bool((hdr >> 6) & 0x1)
    rx_add = bool((hdr >> 7) & 0x1)
    length = (hdr >> 8) & 0xFF
    payload = ll[6:6 + length]
    if len(payload) < 6:
        return None
    addr_le = payload[:6]
    adv_addr = ":".join(f"{b:02x}" for b in reversed(addr_le))
    adv_data = payload[6:]
    return DecodedAdv(
        channel=channel,
        rssi=rssi,
        pdu_type=PDU_TYPE_NAMES.get(pdu_type, f"0x{pdu_type:X}"),
        tx_add_random=tx_add,
        rx_add_random=rx_add,
        adv_addr=adv_addr,
        adv_data=bytes(adv_data),
        raw_pdu_header=hdr,
    )


def classify_address(addr_hex: str, random: bool) -> str:
    """Return public | random_static | rpa | nrpa | unknown."""
    if not random:
        return "public"
    try:
        msb = int(addr_hex.split(":")[0], 16)
    except Exception:  # noqa: BLE001
        return "unknown"
    top2 = msb >> 6
    if top2 == 0b11:
        return "random_static"
    if top2 == 0b01:
        return "rpa"
    if top2 == 0b00:
        return "nrpa"
    return "unknown"


def parse_ad_structures(adv_data: bytes) -> list[tuple[int, bytes]]:
    """Return [(ad_type, ad_value), ...] from a BLE AD-structure stream."""
    out: list[tuple[int, bytes]] = []
    i = 0
    while i < len(adv_data):
        ln = adv_data[i]
        if ln == 0 or i + 1 + ln > len(adv_data):
            break
        ad_type = adv_data[i + 1]
        value = adv_data[i + 2:i + 1 + ln]
        out.append((ad_type, value))
        i += 1 + ln
    return out


# Selected AD type constants (Bluetooth Core Supplement)
AD_FLAGS = 0x01
AD_INCOMPLETE_LIST_16 = 0x02
AD_COMPLETE_LIST_16 = 0x03
AD_SHORTENED_LOCAL_NAME = 0x08
AD_COMPLETE_LOCAL_NAME = 0x09
AD_TX_POWER = 0x0A
AD_SERVICE_DATA_16 = 0x16
AD_MANUFACTURER_DATA = 0xFF
