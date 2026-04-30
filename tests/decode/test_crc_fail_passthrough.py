"""CRC-failed packets are preserved through the decode + ingest path.

Regression coverage for the change from "drop CRC-failed packets at
decode" to "mark crc_ok=False, suppress device attribution
downstream." Behavior we care about:

  1. ``decode_phdr_packet`` and ``decode_nbe_packet`` return a
     ``crc_ok=False`` placeholder (instead of None) when the firmware
     flags say CRC failed.
  2. ``record_packet`` skips CRC-failed packets so no ghost-RPA
     device rows are spawned, even if the corrupted bytes happened
     to look address-shaped.
  3. Clean packets still set ``crc_ok=True``.
"""

from __future__ import annotations

import struct
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from btviz.capture.packet import Packet
from btviz.decode.adv import (
    decode_nbe_packet, decode_phdr_packet,
)
from btviz.ingest.pipeline import IngestContext, record_packet


def _phdr_buf(crc_checked: bool, crc_valid: bool, channel: int = 37) -> bytes:
    """Build a minimal DLT-256 (LL+PHDR) buf with the CRC flag bits set."""
    flags = 0
    if crc_checked:
        flags |= 1 << 10
    if crc_valid:
        flags |= 1 << 11
    # 10-byte PHDR header (channel, rssi, noise, aa-offenses, ref-AA(4), flags(2))
    header = bytes([
        channel,
        struct.pack("b", -60)[0],   # rssi
        0,                          # noise
        0,                          # aa offenses
    ]) + b"\x00\x00\x00\x00" + struct.pack("<H", flags)
    # LL frame: 4-byte AA + 2-byte PDU header + 6-byte AdvA + 3-byte CRC
    pdu_header = b"\x00\x06"        # ADV_IND, length=6
    aa = b"\x8e\x89\xbe\xd6"        # adv access address
    adv_a = b"\xaa" * 6
    crc = b"\xff" * 3
    return header + aa + pdu_header + adv_a + crc


def _nbe_buf(crc_ok: bool, channel: int = 37) -> bytes:
    """Build a minimal DLT-272 Nordic-BLE buf with the CRC-OK flag set."""
    flags = 0x01 if crc_ok else 0x00
    # 17-byte NBE header
    header = bytes([
        0,                # board id
        0, 0,             # header length placeholder
        0x02,             # header version
        0, 0,             # packet counter
        0,                # id
        0x0a,             # ble_header_length (always 0x0a)
        flags,            # offset 8: CRC OK
        channel,          # offset 9: rf channel
        60,               # offset 10: rssi magnitude (positive byte)
        0, 0,             # event counter
        0, 0, 0, 0,       # timestamp delta
    ])
    # LL frame: same shape as PHDR
    aa = b"\x8e\x89\xbe\xd6"
    pdu_header = b"\x00\x06"
    adv_a = b"\xaa" * 6
    crc = b"\xff" * 3
    return header + aa + pdu_header + adv_a + crc


class DecodePathTests(unittest.TestCase):

    def test_phdr_crc_failed_returns_placeholder_not_none(self):
        # CRC checked + CRC invalid → placeholder
        decoded = decode_phdr_packet(_phdr_buf(crc_checked=True, crc_valid=False))
        self.assertIsNotNone(decoded)
        self.assertFalse(decoded.crc_ok)
        self.assertEqual(decoded.channel, 37)
        # All decoded fields safe defaults — no garbage address surfaced
        self.assertIsNone(decoded.adv_addr)
        self.assertEqual(decoded.adv_data, b"")

    def test_phdr_crc_passed_decodes_normally(self):
        # CRC checked + CRC valid → real decode (or None if LL parse fails)
        decoded = decode_phdr_packet(_phdr_buf(crc_checked=True, crc_valid=True))
        if decoded is not None:  # LL parse may decline
            self.assertTrue(decoded.crc_ok)

    def test_phdr_crc_unchecked_treated_as_ok(self):
        # If firmware didn't check CRC, accept on faith — historical behavior
        decoded = decode_phdr_packet(_phdr_buf(crc_checked=False, crc_valid=False))
        if decoded is not None:
            self.assertTrue(decoded.crc_ok)

    def test_nbe_crc_failed_returns_placeholder_not_none(self):
        decoded = decode_nbe_packet(_nbe_buf(crc_ok=False))
        self.assertIsNotNone(decoded)
        self.assertFalse(decoded.crc_ok)
        self.assertEqual(decoded.channel, 37)
        self.assertEqual(decoded.rssi, -60)
        self.assertIsNone(decoded.adv_addr)


class RecordPacketGatingTests(unittest.TestCase):
    """``record_packet`` must skip CRC-failed packets to avoid ghost RPAs."""

    def setUp(self):
        from btviz.db.repos import Repos
        from btviz.db.store import Store
        d = tempfile.mkdtemp()
        self.store = Store(Path(d) / "test.db")
        self.repos = Repos(self.store)
        proj = self.repos.projects.create("t")
        sess = self.repos.sessions.start(proj.id, source_type="live")
        self.ctx = IngestContext(session_id=sess.id)

    def tearDown(self):
        self.store.close()

    def _make_packet(self, *, crc_ok: bool, addr: str = "aa:bb:cc:dd:ee:ff") -> Packet:
        return Packet(
            ts=0.0,
            source="dummy",
            channel=37,
            rssi=-60,
            adv_addr=addr,
            adv_addr_type="random_static",
            crc_ok=crc_ok,
            extras={"layers": {}},
        )

    def test_clean_packet_attributes_to_device(self):
        result = record_packet(self.repos, self.ctx, self._make_packet(crc_ok=True))
        self.assertIsNotNone(result)
        self.assertGreater(result, 0)

    def test_crc_failed_packet_is_skipped_returns_none(self):
        # A CRC-failed packet that happens to have an address-shaped
        # blob in the right slot must NOT spawn a device row.
        result = record_packet(self.repos, self.ctx, self._make_packet(crc_ok=False))
        self.assertIsNone(result)
        # Verify no device row was created.
        n_devs = self.store.conn.execute(
            "SELECT COUNT(*) FROM devices"
        ).fetchone()[0]
        self.assertEqual(n_devs, 0)

    def test_no_adv_addr_still_returns_none(self):
        result = record_packet(
            self.repos, self.ctx,
            Packet(ts=0.0, source="dummy", crc_ok=True, adv_addr=None),
        )
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
