"""Tests for device_ad_history population and _extract_ad_entries."""

from __future__ import annotations

import struct
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from btviz.db.store import Store
from btviz.db.repos import Repos
from btviz.ingest.pipeline import (
    IngestContext,
    record_packet,
    _extract_ad_entries,
)
from btviz.capture.packet import Packet


def _make_store() -> tuple[Store, Path]:
    d = tempfile.mkdtemp()
    p = Path(d) / "test.db"
    return Store(p), p


def _layers(*, name=None, uuid16=None, company_id=None, mfg_data=None,
            tx_power=None, appearance=None) -> dict:
    """Build a minimal tshark EK layers dict for testing."""
    btcommon: dict = {}
    if name is not None:
        btcommon["btcommon_btcommon_eir_ad_entry_device_name"] = name
    if uuid16 is not None:
        btcommon["btcommon_btcommon_eir_ad_entry_uuid_16"] = uuid16
    if company_id is not None:
        btcommon["btcommon_btcommon_eir_ad_entry_company_id"] = hex(company_id)
    if mfg_data is not None:
        btcommon["btcommon_btcommon_eir_ad_entry_data"] = mfg_data
    if tx_power is not None:
        btcommon["btcommon_btcommon_eir_ad_entry_power_level"] = tx_power
    if appearance is not None:
        btcommon["btcommon_btcommon_eir_ad_entry_appearance"] = appearance
    return {"btle": {}, "btcommon": btcommon}


def _pkt(layers: dict, addr: str = "aa:bb:cc:dd:ee:ff") -> Packet:
    return Packet(
        ts=1000.0,
        source="file",
        channel=37,
        rssi=-70,
        phy="1M",
        pdu_type="ADV_IND",
        adv_addr=addr,
        adv_addr_type="random_static",
        extras={"layers": layers},
    )


class ExtractAdEntriesTests(unittest.TestCase):

    def test_local_name(self):
        entries = _extract_ad_entries(_layers(name="My Device"))
        self.assertIn((0x09, b"My Device"), entries)

    def test_uuid16_single(self):
        entries = _extract_ad_entries(_layers(uuid16="0x180a"))
        self.assertIn((0x03, struct.pack("<H", 0x180A)), entries)

    def test_uuid16_list(self):
        entries = _extract_ad_entries(_layers(uuid16=["0x180a", "0x180d"]))
        self.assertIn((0x03, struct.pack("<H", 0x180A)), entries)
        self.assertIn((0x03, struct.pack("<H", 0x180D)), entries)

    def test_manufacturer_specific_with_data(self):
        # Apple: company_id=0x004C, data="12:19:00"
        entries = _extract_ad_entries(
            _layers(company_id=0x004C, mfg_data="12:19:00")
        )
        expected = struct.pack("<H", 0x004C) + bytes([0x12, 0x19, 0x00])
        self.assertIn((0xFF, expected), entries)

    def test_manufacturer_specific_no_data(self):
        entries = _extract_ad_entries(_layers(company_id=0x0059))
        expected = struct.pack("<H", 0x0059)
        self.assertIn((0xFF, expected), entries)

    def test_tx_power(self):
        entries = _extract_ad_entries(_layers(tx_power=-8))
        self.assertIn((0x0A, struct.pack("b", -8)), entries)

    def test_appearance(self):
        entries = _extract_ad_entries(_layers(appearance=0x0180))
        self.assertIn((0x19, struct.pack("<H", 0x0180)), entries)

    def test_empty_layers(self):
        entries = _extract_ad_entries({})
        self.assertEqual(entries, [])

    def test_multiple_types_together(self):
        entries = _extract_ad_entries(
            _layers(name="Watch", appearance=0x00C0, tx_power=4)
        )
        types = {t for t, _ in entries}
        self.assertIn(0x09, types)
        self.assertIn(0x19, types)
        self.assertIn(0x0A, types)


class AdHistoryPopulationTests(unittest.TestCase):

    def setUp(self):
        self.store, _ = _make_store()
        self.repos = Repos(self.store)

    def tearDown(self):
        self.store.close()

    def _add_device(self) -> int:
        d = self.repos.devices.upsert("rs:aa:bb:cc:dd:ee:ff", "random_static_mac", now=0.0)
        return d.id

    def test_upsert_inserts_new_row(self):
        dev_id = self._add_device()
        self.repos.ad_history.upsert_many(
            dev_id, [(0x09, b"Hello")], ts=1000.0
        )
        row = self.store.conn.execute(
            "SELECT count, first_seen, last_seen FROM device_ad_history"
            " WHERE device_id=? AND ad_type=? AND ad_value=?",
            (dev_id, 0x09, b"Hello"),
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["count"], 1)

    def test_upsert_increments_count(self):
        dev_id = self._add_device()
        self.repos.ad_history.upsert_many(dev_id, [(0x09, b"Hello")], ts=1000.0)
        self.repos.ad_history.upsert_many(dev_id, [(0x09, b"Hello")], ts=2000.0)
        row = self.store.conn.execute(
            "SELECT count, last_seen FROM device_ad_history"
            " WHERE device_id=? AND ad_type=? AND ad_value=?",
            (dev_id, 0x09, b"Hello"),
        ).fetchone()
        self.assertEqual(row["count"], 2)
        self.assertEqual(row["last_seen"], 2000.0)

    def test_different_values_are_separate_rows(self):
        dev_id = self._add_device()
        self.repos.ad_history.upsert_many(
            dev_id,
            [(0x03, struct.pack("<H", 0x180A)), (0x03, struct.pack("<H", 0x180D))],
            ts=1000.0,
        )
        count = self.store.conn.execute(
            "SELECT COUNT(*) FROM device_ad_history WHERE device_id=?", (dev_id,)
        ).fetchone()[0]
        self.assertEqual(count, 2)

    def test_record_packet_populates_ad_history(self):
        proj = self.repos.projects.create("test")
        sess = self.repos.sessions.start(proj.id, "file", name="s1")
        ctx = IngestContext(session_id=sess.id)

        pkt = _pkt(_layers(name="AirTag", company_id=0x004C, mfg_data="12:05"))
        record_packet(self.repos, ctx, pkt)

        rows = self.store.conn.execute(
            "SELECT ad_type FROM device_ad_history ORDER BY ad_type"
        ).fetchall()
        ad_types = {r["ad_type"] for r in rows}
        self.assertIn(0x09, ad_types)   # local name
        self.assertIn(0xFF, ad_types)   # mfg data

    def test_keep_packets_false_writes_no_packet_rows(self):
        proj = self.repos.projects.create("test")
        sess = self.repos.sessions.start(proj.id, "file", name="s1")
        ctx = IngestContext(session_id=sess.id, keep_packets=False)

        pkt = _pkt(_layers(name="X"))
        record_packet(self.repos, ctx, pkt)

        count = self.store.conn.execute(
            "SELECT COUNT(*) FROM packets"
        ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_keep_packets_true_writes_packet_row(self):
        proj = self.repos.projects.create("test")
        sess = self.repos.sessions.start(proj.id, "file", name="s1")
        ctx = IngestContext(session_id=sess.id, keep_packets=True)

        pkt = _pkt(_layers(name="X"))
        record_packet(self.repos, ctx, pkt)

        count = self.store.conn.execute(
            "SELECT COUNT(*) FROM packets"
        ).fetchone()[0]
        self.assertEqual(count, 1)
        row = self.store.conn.execute("SELECT * FROM packets").fetchone()
        self.assertEqual(row["session_id"], sess.id)
        self.assertEqual(row["ts"], 1000.0)
        self.assertEqual(row["rssi"], -70)
        self.assertEqual(row["channel"], 37)


if __name__ == "__main__":
    unittest.main()
