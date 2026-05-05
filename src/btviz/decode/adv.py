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
    # Firmware-reported CRC validity. False means the radio captured
    # bytes but the LL-frame CRC failed — corruption somewhere in the
    # payload, so the address / pdu_type / data fields are NOT
    # trustworthy. We surface the placeholder so the live-ingest path
    # can drive a "dropout" flash on the sniffer panel WITHOUT ever
    # passing the packet through ``record_packet`` (which would
    # otherwise spawn ghost-RPA device rows).
    crc_ok: bool = True


def _crc_fail_placeholder(channel: int, rssi: int) -> DecodedAdv:
    """Return a minimal DecodedAdv for a CRC-failed packet.

    Carries only channel + RSSI + ``crc_ok=False``; all decoded fields
    set to safe defaults because the underlying bytes are corrupted.
    Caller must NOT use ``adv_addr`` or any other parsed field for
    attribution.
    """
    return DecodedAdv(
        channel=channel,
        rssi=rssi,
        pdu_type="CRC_FAIL",
        tx_add_random=False,
        rx_add_random=False,
        adv_addr=None,
        adv_data=b"",
        raw_pdu_header=0,
        crc_ok=False,
    )


def decode_phdr_packet(buf: bytes) -> DecodedAdv | None:
    """Decode a Nordic BLE LL+PHDR pcap payload (DLT 256). None if not adv.

    The 16-bit flags field at bytes 8-9 (LE) carries CRC validity:
      bit 10 = CRC checked
      bit 11 = CRC valid
    If the firmware checked the CRC and it failed, return a
    ``crc_ok=False`` placeholder rather than dropping outright — the
    caller (live-ingest) needs to know a packet was *attempted* on
    that channel so the UI can render a dropout flash. The historical
    "drop entirely" behavior persists for the device-attribution path
    in ``record_packet``, which checks ``pkt.crc_ok`` before spawning
    a device row (otherwise bit-error corruption produces ghost
    devices whose addresses are 1-4 bits away from a real one).

    If the firmware didn't check the CRC (bit 10 unset), we accept
    the packet on faith and return crc_ok=True.
    """
    if len(buf) < 10 + 4 + 2 + 6 + 3:
        return None
    rf_channel = buf[0]
    rssi_dbm = struct.unpack("b", buf[1:2])[0]
    # buf[2] noise, buf[3] aa offenses, buf[4:8] ref AA, buf[8:10] flags
    flags = struct.unpack("<H", buf[8:10])[0]
    if (flags >> 10) & 0x1 and not (flags >> 11) & 0x1:
        return _crc_fail_placeholder(rf_channel, rssi_dbm)
    return _decode_ll(buf[10:], channel=rf_channel, rssi=rssi_dbm)


# Nordic BLE (DLT 272) header layout — what current Nordic Sniffer
# firmware emits. Total header is 17 bytes. Field offsets follow the
# Nordic SnifferAPI/Packet.py constants but shifted by +1 because the
# pcap output prepends a `board_id` byte before the structure described
# there.
#
#   [0]      board id
#   [1-2]    header length / payload_len (u16 LE)
#   [3]      header version (= 0x02)
#   [4-5]    packet counter (u16 LE)
#   [6]      id
#   [7]      ble_header_length (always 10 = 0x0a)
#   [8]      flags         ← bit 0 = CRC OK (per Nordic SnifferAPI/Packet.py:421)
#   [9]      RF channel
#   [10]     RSSI magnitude (positive byte; actual value is negative)
#   [11-12]  event counter (u16 LE)
#   [13-16]  timestamp delta (u32 LE)
#   [17..]   BLE LL frame (AA + PDU header + payload + CRC)
_NBE_HDR_LEN = 17
_NBE_FLAGS_OFFSET = 8
_NBE_FLAG_CRC_OK = 0x01


def decode_nbe_packet(buf: bytes) -> DecodedAdv | None:
    """Decode a Nordic-BLE (DLT 272) pcap payload.

    Returns None when the buffer is too short to be a packet at all.
    For CRC-failed packets, returns a ``crc_ok=False`` placeholder —
    same pattern as ``decode_phdr_packet``. The placeholder carries
    only channel + RSSI; the LL-frame parse is skipped because the
    underlying bytes can't be trusted.

    Bit 0 of the flags byte at offset 8 is the firmware's CRC-OK flag
    (per Nordic's SnifferAPI/Packet.py:421 — ``self.crcOK = self.flags
    & 1``). Watch the offsets carefully: in the SnifferAPI source
    FLAGS_POS = 7, but that's relative to the post-syncword UART
    structure. The pcap-output format prepends a board_id byte, so on
    the wire here flags is at byte 8.

    Historical note: we used to drop CRC-failed packets entirely here
    because passing them to ``record_packet`` produced ghost devices
    whose addresses were 1-4 bits away from a real canonical (the
    diagnostic showed 72% of random-kind device rows in the user's DB
    were such ghosts). The current behavior — placeholder back to the
    caller — preserves that property because ``record_packet`` checks
    ``pkt.crc_ok`` before spawning a device row, while the sniffer
    panel still gets to see the packet for its dropout flash.
    """
    # Need the pseudo-header to read flags/channel/RSSI. The full
    # LL-frame minimum is checked below, but only on the CRC-OK path
    # — short CRC-failed frames (e.g. a 30-byte truncated capture)
    # are still useful for the panel dropout flash and must be routed
    # through _crc_fail_placeholder rather than silently dropped.
    if len(buf) < _NBE_HDR_LEN:
        return None
    rf_channel = buf[9]
    rssi_dbm = -buf[10]
    if not (buf[_NBE_FLAGS_OFFSET] & _NBE_FLAG_CRC_OK):
        return _crc_fail_placeholder(rf_channel, rssi_dbm)
    if len(buf) < _NBE_HDR_LEN + 4 + 2 + 6 + 3:
        return None
    return _decode_ll(buf[_NBE_HDR_LEN:], channel=rf_channel, rssi=rssi_dbm)


# Extended-header field bit masks (Core Spec Vol 6 Part B §2.3.4.1).
# Order matters — fields appear in this order whenever their bit is set.
_EXT_HDR_FLAG_ADVA      = 0x01   # 6 bytes
_EXT_HDR_FLAG_TARGETA   = 0x02   # 6 bytes
_EXT_HDR_FLAG_CTE_INFO  = 0x04   # 1 byte
_EXT_HDR_FLAG_ADI       = 0x08   # 2 bytes (AdvDataInfo: DID + SID)
_EXT_HDR_FLAG_AUX_PTR   = 0x10   # 3 bytes
_EXT_HDR_FLAG_SYNC_INFO = 0x20   # 18 bytes
_EXT_HDR_FLAG_TX_POWER  = 0x40   # 1 byte
# bit 7 is reserved.

PDU_TYPE_ADV_EXT_IND = 0x7


def _decode_ll(ll: bytes, *, channel: int, rssi: int) -> DecodedAdv | None:
    """Parse the BLE LL frame portion (after any pcap pseudo-header).

    Shared by both DLT 256 (10-byte PHDR) and DLT 272 (17-byte NBE header)
    decoders — once the per-DLT header is stripped, the LL layout is
    identical. Returns None for data-channel / non-adv / truncated input.

    Two payload shapes depending on PDU type:

      * Legacy adv (0x0..0x6): ``[AdvA(6)][AdvData(N)][CRC(3)]`` — AD
        structures start immediately after AdvA.
      * Extended adv (0x7 = ADV_EXT_IND / AUX_ADV_IND / etc.):
        ``[ExtHdrLen+AdvMode(1)][HdrFlags(1)][optional fields…][ACAD]
        [AdvData(N)][CRC(3)]``. AdvA is *optional* in the extended
        header (continuation AUX packets often omit it; the AdvA was
        in the primary on 37/38/39).
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

    if pdu_type == PDU_TYPE_ADV_EXT_IND:
        adv_addr, adv_data = _split_extended_adv_payload(payload)
    else:
        # Legacy advertising layout.
        if len(payload) < 6:
            return None
        addr_le = payload[:6]
        adv_addr = ":".join(f"{b:02x}" for b in reversed(addr_le))
        adv_data = bytes(payload[6:])

    return DecodedAdv(
        channel=channel,
        rssi=rssi,
        pdu_type=PDU_TYPE_NAMES.get(pdu_type, f"0x{pdu_type:X}"),
        tx_add_random=tx_add,
        rx_add_random=rx_add,
        adv_addr=adv_addr,
        adv_data=adv_data,
        raw_pdu_header=hdr,
    )


def _split_extended_adv_payload(payload: bytes) -> tuple[str | None, bytes]:
    """Walk the BLE 5.0 extended-adv payload to find AdvA (if present)
    and the start of the AD-structure stream.

    Returns ``(adv_addr, adv_data)``. ``adv_addr`` is None when the
    extended header didn't include an AdvA — common for AUX_ADV_IND
    continuation packets that pair with a primary ADV_EXT_IND on
    37/38/39 by AdvDataInfo (DID). The caller will see no adv_addr but
    can still parse adv_data for service data (e.g. BAA for Auracast).

    Defensive — returns ``(None, b"")`` on any layout violation rather
    than raising, so a malformed packet just produces an unattributed
    empty ``DecodedAdv`` instead of crashing the reader thread.
    """
    if len(payload) < 1:
        return None, b""
    ext_hdr_len = payload[0] & 0x3F        # bits 0-5; bits 6-7 are AdvMode
    # Extended header is [HdrFlags(1)] + optional fields, total ext_hdr_len bytes.
    if ext_hdr_len == 0:
        # No extended header — AdvData immediately follows the length byte.
        return None, bytes(payload[1:])
    if 1 + ext_hdr_len > len(payload):
        return None, b""               # truncated header; bail
    flags = payload[1]
    cursor = 2                         # next byte after HdrFlags
    end_of_ext_hdr = 1 + ext_hdr_len   # AdvData starts here

    adv_addr: str | None = None
    if flags & _EXT_HDR_FLAG_ADVA:
        if cursor + 6 > end_of_ext_hdr:
            return None, b""
        addr_le = payload[cursor:cursor + 6]
        adv_addr = ":".join(f"{b:02x}" for b in reversed(addr_le))
        cursor += 6
    if flags & _EXT_HDR_FLAG_TARGETA:
        cursor += 6
    if flags & _EXT_HDR_FLAG_CTE_INFO:
        cursor += 1
    if flags & _EXT_HDR_FLAG_ADI:
        cursor += 2
    if flags & _EXT_HDR_FLAG_AUX_PTR:
        cursor += 3
    if flags & _EXT_HDR_FLAG_SYNC_INFO:
        cursor += 18
    if flags & _EXT_HDR_FLAG_TX_POWER:
        cursor += 1
    # Bytes between cursor and end_of_ext_hdr are ACAD — skip them
    # (BIGInfo and similar live there; we don't dissect them yet).
    if cursor > end_of_ext_hdr:
        return None, b""               # walked past header; malformed
    adv_data = bytes(payload[end_of_ext_hdr:])
    return adv_addr, adv_data


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
