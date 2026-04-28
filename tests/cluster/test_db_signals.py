"""Tests for DB-backed signals: service_uuid_match and mfg_data_prefix."""

from __future__ import annotations

import struct
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from btviz.cluster.base import Address, ClassProfile, ClusterContext, Device
from btviz.cluster.signals.service_uuid_match import ServiceUuidMatch
from btviz.cluster.signals.mfg_data_prefix import MfgDataPrefix
from btviz.db.store import Store
from btviz.db.repos import Repos

AD_UUID16 = 0x03
AD_MFG    = 0xFF
APPLE_CID = struct.pack("<H", 0x004C)


def _make_store() -> Store:
    d = tempfile.mkdtemp()
    return Store(Path(d) / "test.db")


def _device(dev_id: int, cls: str = "airtag") -> Device:
    return Device(
        id=dev_id,
        device_class=cls,
        address=Address(bytes_=b"\xaa" * 6, kind="random_resolvable"),
        first_seen=0.0,
        last_seen=0.0,
    )


def _uuid_blob(uuid16: int) -> bytes:
    return struct.pack("<H", uuid16)


def _mfg_blob(cid: bytes, payload: bytes) -> bytes:
    return cid + payload


def _ctx(store: Store) -> ClusterContext:
    return ClusterContext(
        signals={},
        profiles={},
        now=0.0,
        db=store,
    )


def _seed_uuids(store: Store, device_id: int, uuids: list[int]) -> None:
    for u in uuids:
        store.conn.execute(
            "INSERT OR IGNORE INTO device_ad_history"
            " (device_id, ad_type, ad_value, first_seen, last_seen, count)"
            " VALUES (?, ?, ?, 0, 0, 1)",
            (device_id, AD_UUID16, _uuid_blob(u)),
        )


def _seed_mfg(store: Store, device_id: int, cid: bytes, payload: bytes) -> None:
    store.conn.execute(
        "INSERT OR IGNORE INTO device_ad_history"
        " (device_id, ad_type, ad_value, first_seen, last_seen, count)"
        " VALUES (?, ?, ?, 0, 0, 1)",
        (device_id, AD_MFG, _mfg_blob(cid, payload)),
    )


def _add_device(store: Store, stable_key: str) -> int:
    repos = Repos(store)
    d = repos.devices.upsert(stable_key, "random_static_mac", now=0.0)
    return d.id


class ServiceUuidMatchTests(unittest.TestCase):

    def setUp(self):
        self.store = _make_store()
        self.signal = ServiceUuidMatch()
        self.id_a = _add_device(self.store, "rs:aa:bb:cc:dd:ee:01")
        self.id_b = _add_device(self.store, "rs:aa:bb:cc:dd:ee:02")

    def tearDown(self):
        self.store.close()

    def _ctx(self):
        return _ctx(self.store)

    def test_abstains_without_db(self):
        ctx = ClusterContext(signals={}, profiles={}, now=0.0, db=None)
        a, b = _device(self.id_a), _device(self.id_b)
        self.assertFalse(self.signal.applies_to(ctx, a, b))

    def test_abstains_when_no_uuid_history(self):
        ctx = self._ctx()
        result = self.signal.score(ctx, _device(self.id_a), _device(self.id_b))
        self.assertIsNone(result)

    def test_abstains_when_one_device_has_no_uuids(self):
        _seed_uuids(self.store, self.id_a, [0x180A])
        ctx = self._ctx()
        result = self.signal.score(ctx, _device(self.id_a), _device(self.id_b))
        self.assertIsNone(result)

    def test_identical_uuid_sets_score_1(self):
        _seed_uuids(self.store, self.id_a, [0x180A, 0x180D])
        _seed_uuids(self.store, self.id_b, [0x180A, 0x180D])
        ctx = self._ctx()
        result = self.signal.score(ctx, _device(self.id_a), _device(self.id_b))
        self.assertAlmostEqual(result, 1.0)

    def test_partial_overlap_jaccard(self):
        # {A, B} vs {A, C} → Jaccard = 1/3
        # rare_threshold=0 disables the rare-UUID penalty so we test pure Jaccard.
        _seed_uuids(self.store, self.id_a, [0x180A, 0x180D])
        _seed_uuids(self.store, self.id_b, [0x180A, 0x1810])
        ctx = self._ctx()
        result = self.signal.score(
            ctx, _device(self.id_a), _device(self.id_b),
            params={"rare_threshold": 0},
        )
        self.assertAlmostEqual(result, round(1/3, 4), places=3)

    def test_no_overlap_returns_zero(self):
        _seed_uuids(self.store, self.id_a, [0x180A])
        _seed_uuids(self.store, self.id_b, [0x1810])
        # Both UUIDs appear in 2 devices (above rare_threshold=3 default...
        # but we only have 2 devices total so count=1 each → rare → -0.5)
        # Use rare_threshold=0 to suppress the rare-UUID penalty and just
        # test the no-overlap path.
        ctx = self._ctx()
        result = self.signal.score(
            ctx, _device(self.id_a), _device(self.id_b),
            params={"rare_threshold": 0},
        )
        self.assertEqual(result, 0.0)

    def test_rare_uuid_mismatch_returns_negative(self):
        # Device A has a UUID that only 1 device has → distinctive mismatch
        _seed_uuids(self.store, self.id_a, [0x180A, 0xFFF0])
        _seed_uuids(self.store, self.id_b, [0x180A])
        ctx = self._ctx()
        result = self.signal.score(
            ctx, _device(self.id_a), _device(self.id_b),
            params={"rare_threshold": 3},
        )
        self.assertEqual(result, -0.5)


class MfgDataPrefixTests(unittest.TestCase):

    def setUp(self):
        self.store = _make_store()
        self.signal = MfgDataPrefix()
        self.id_a = _add_device(self.store, "rs:aa:bb:cc:dd:ee:01")
        self.id_b = _add_device(self.store, "rs:aa:bb:cc:dd:ee:02")

    def tearDown(self):
        self.store.close()

    def _ctx(self):
        return _ctx(self.store)

    def test_abstains_without_db(self):
        ctx = ClusterContext(signals={}, profiles={}, now=0.0, db=None)
        a, b = _device(self.id_a), _device(self.id_b)
        self.assertFalse(self.signal.applies_to(ctx, a, b))

    def test_abstains_when_no_mfg_data(self):
        ctx = self._ctx()
        result = self.signal.score(ctx, _device(self.id_a), _device(self.id_b))
        self.assertIsNone(result)

    def test_abstains_when_different_company(self):
        nordic_cid = struct.pack("<H", 0x0059)
        _seed_mfg(self.store, self.id_a, APPLE_CID,  b"\x12\x19\x00\x01")
        _seed_mfg(self.store, self.id_b, nordic_cid, b"\x12\x19\x00\x01")
        ctx = self._ctx()
        result = self.signal.score(ctx, _device(self.id_a), _device(self.id_b))
        self.assertIsNone(result)

    def test_same_company_matching_prefix_scores_1(self):
        _seed_mfg(self.store, self.id_a, APPLE_CID, b"\x12\x19\x00\x01\xAB\xCD")
        _seed_mfg(self.store, self.id_b, APPLE_CID, b"\x12\x19\x00\x01\xFF\xFF")
        ctx = self._ctx()
        result = self.signal.score(
            ctx, _device(self.id_a), _device(self.id_b),
            params={"prefix_len": 4},
        )
        self.assertEqual(result, 1.0)

    def test_same_company_mismatched_prefix_scores_zero(self):
        _seed_mfg(self.store, self.id_a, APPLE_CID, b"\x12\x19\x00\x01")
        _seed_mfg(self.store, self.id_b, APPLE_CID, b"\x10\x05\x01\x18")
        ctx = self._ctx()
        result = self.signal.score(
            ctx, _device(self.id_a), _device(self.id_b),
            params={"prefix_len": 4},
        )
        self.assertEqual(result, 0.0)

    def test_short_prefix_len_1(self):
        # Only first payload byte compared — match even if rest differs
        _seed_mfg(self.store, self.id_a, APPLE_CID, b"\x12\xFF\xFF\xFF")
        _seed_mfg(self.store, self.id_b, APPLE_CID, b"\x12\x00\x00\x00")
        ctx = self._ctx()
        result = self.signal.score(
            ctx, _device(self.id_a), _device(self.id_b),
            params={"prefix_len": 1},
        )
        self.assertEqual(result, 1.0)


if __name__ == "__main__":
    unittest.main()
