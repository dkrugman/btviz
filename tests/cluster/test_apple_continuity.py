"""Tests for the apple_continuity signal."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from btviz.cluster.base import Address, ClusterContext, Device
from btviz.cluster.signals.apple_continuity import (
    APPLE_CID_BE, AppleContinuity, _parse_continuity_tlvs,
)
from btviz.db.repos import Repos
from btviz.db.store import Store

AD_TYPE_MFG = 0xFF


def _make_store() -> Store:
    d = tempfile.mkdtemp()
    return Store(Path(d) / "test.db")


def _add_device(store: Store, stable_key: str) -> int:
    repos = Repos(store)
    d = repos.devices.upsert(stable_key, "random_static_mac", now=0.0)
    return d.id


def _device(dev_id: int, cls: str = "apple_device") -> Device:
    return Device(
        id=dev_id,
        device_class=cls,
        address=Address(bytes_=b"\xaa" * 6, kind="random_resolvable"),
        first_seen=0.0,
        last_seen=0.0,
    )


def _seed_mfg(store: Store, device_id: int, blobs: list[bytes]) -> None:
    for blob in blobs:
        store.conn.execute(
            "INSERT OR IGNORE INTO device_ad_history"
            " (device_id, ad_type, ad_value, first_seen, last_seen, count)"
            " VALUES (?, ?, ?, 0, 0, 1)",
            (device_id, AD_TYPE_MFG, blob),
        )


def _ctx(store: Store) -> ClusterContext:
    return ClusterContext(signals={}, profiles={}, now=0.0, db=store)


class ParserTests(unittest.TestCase):
    """Unit-level: TLV parsing handles real captures + edge cases."""

    def test_strips_apple_cid_and_parses_single_tlv(self):
        blob = APPLE_CID_BE + bytes.fromhex("12020003")
        tlvs = _parse_continuity_tlvs(blob)
        self.assertEqual(tlvs, [(0x12, b"\x00\x03")])

    def test_parses_multiple_tlvs_in_one_blob(self):
        # Real-world: 0x0C Handoff (14 bytes) + 0x10 NearbyInfo (5 bytes)
        blob = bytes.fromhex(
            "4c00"                          # Apple CID
            "0c0e08e55a11b4b17dd224409c38608b"  # type 0x0C, len 0x0E
            "1005471c69ee65"                # type 0x10, len 0x05
        )
        tlvs = _parse_continuity_tlvs(blob)
        self.assertEqual(len(tlvs), 2)
        self.assertEqual(tlvs[0][0], 0x0C)
        self.assertEqual(len(tlvs[0][1]), 14)
        self.assertEqual(tlvs[1][0], 0x10)
        self.assertEqual(len(tlvs[1][1]), 5)

    def test_rejects_non_apple_cid(self):
        # Microsoft CID (0x0006), should yield nothing
        blob = bytes.fromhex("06001202aabb")
        self.assertEqual(_parse_continuity_tlvs(blob), [])

    def test_handles_truncated_payload(self):
        # Length byte claims 10 but only 4 bytes of payload follow
        blob = APPLE_CID_BE + bytes.fromhex("0c0a") + bytes.fromhex("aabbccdd")
        # Should stop parsing rather than crash or emit garbage
        tlvs = _parse_continuity_tlvs(blob)
        self.assertEqual(tlvs, [])

    def test_handles_short_blob(self):
        self.assertEqual(_parse_continuity_tlvs(b""), [])
        self.assertEqual(_parse_continuity_tlvs(b"\x4c"), [])
        self.assertEqual(_parse_continuity_tlvs(APPLE_CID_BE), [])


class ScoringTests(unittest.TestCase):

    def setUp(self):
        self.store = _make_store()
        self.signal = AppleContinuity()
        self.id_a = _add_device(self.store, "rs:aa:bb:cc:dd:ee:01")
        self.id_b = _add_device(self.store, "rs:aa:bb:cc:dd:ee:02")
        self.a = _device(self.id_a)
        self.b = _device(self.id_b)

    def tearDown(self):
        self.store.close()

    def test_abstains_without_db(self):
        ctx = ClusterContext(signals={}, profiles={}, now=0.0, db=None)
        self.assertFalse(self.signal.applies_to(ctx, self.a, self.b))
        self.assertIsNone(self.signal.score(ctx, self.a, self.b))

    def test_abstains_when_either_device_has_no_continuity_data(self):
        ctx = _ctx(self.store)
        # Neither seeded → both empty → None
        self.assertIsNone(self.signal.score(ctx, self.a, self.b))
        # One seeded, one not → still None
        _seed_mfg(self.store, self.id_a,
                  [APPLE_CID_BE + bytes.fromhex("0c0e08e55a11b4b17dd224409c38608b")])
        self.assertIsNone(self.signal.score(ctx, self.a, self.b))

    def test_abstains_when_only_non_apple_mfg_data(self):
        # Microsoft (0x0006) and Google (0x00E0) — neither is Apple
        _seed_mfg(self.store, self.id_a, [bytes.fromhex("06001202aabbcc")])
        _seed_mfg(self.store, self.id_b, [bytes.fromhex("e0001202aabbcc")])
        ctx = _ctx(self.store)
        self.assertIsNone(self.signal.score(ctx, self.a, self.b))

    def test_exact_long_payload_match_strong(self):
        # 14-byte Handoff payload — well above 8-byte fingerprint floor.
        payload = APPLE_CID_BE + bytes.fromhex("0c0e08e55a11b4b17dd224409c38608b")
        _seed_mfg(self.store, self.id_a, [payload])
        _seed_mfg(self.store, self.id_b, [payload])
        ctx = _ctx(self.store)
        self.assertEqual(self.signal.score(ctx, self.a, self.b), 1.0)

    def test_short_payload_match_does_not_count(self):
        # 0x12 with 2-byte payload — too generic, shared across many
        # devices of the same model. Must not trigger a 1.0 match.
        payload = APPLE_CID_BE + bytes.fromhex("12020003")
        _seed_mfg(self.store, self.id_a, [payload])
        _seed_mfg(self.store, self.id_b, [payload])
        ctx = _ctx(self.store)
        s = self.signal.score(ctx, self.a, self.b)
        # Common types (just 0x12), no long-payload fingerprint:
        # falls through to the type-Jaccard branch → 0.4 * (1/1) = 0.4
        self.assertEqual(s, 0.4)

    def test_common_types_no_payload_match_soft_positive(self):
        # Both broadcast Handoff + NearbyInfo but with different
        # encrypted contents — same Apple class, not same device.
        a_blob = bytes.fromhex(
            "4c00"
            "0c0e08e55a11b4b17dd224409c38608b"   # Handoff A
            "1005471c69ee65"                     # NearbyInfo A
        )
        b_blob = bytes.fromhex(
            "4c00"
            "0c0e08AAAAAAAAAAAAAAAAAAAAAAAAAA"   # Handoff B (different)
            "1005ffffffffff"                     # NearbyInfo B (different)
        )
        _seed_mfg(self.store, self.id_a, [a_blob])
        _seed_mfg(self.store, self.id_b, [b_blob])
        ctx = _ctx(self.store)
        s = self.signal.score(ctx, self.a, self.b)
        # types {0x0C, 0x10} ∩ {0x0C, 0x10} = 2, union = 2, jaccard = 1.0
        self.assertEqual(s, 0.4)

    def test_disjoint_types_mild_negative(self):
        # A broadcasts only Handoff, B broadcasts only AirPlay
        a_blob = bytes.fromhex(
            "4c00"
            "0c0e08e55a11b4b17dd224409c38608b"
        )
        b_blob = bytes.fromhex(
            "4c00"
            "0916bbcceedd0011223344556677889900112233"
        )
        _seed_mfg(self.store, self.id_a, [a_blob])
        _seed_mfg(self.store, self.id_b, [b_blob])
        ctx = _ctx(self.store)
        self.assertEqual(self.signal.score(ctx, self.a, self.b), -0.3)

    def test_min_fingerprint_bytes_param_is_honored(self):
        # 4-byte payload would normally be filtered out (default min 8),
        # but caller can lower the threshold. Important: this is a
        # safety knob for tests / debugging; production should keep the
        # default since shorter payloads false-positive easily.
        payload = APPLE_CID_BE + bytes.fromhex("12040000aabb")
        _seed_mfg(self.store, self.id_a, [payload])
        _seed_mfg(self.store, self.id_b, [payload])
        ctx = _ctx(self.store)
        # default: 4 bytes < 8 → falls to common-types branch → 0.4
        self.assertEqual(self.signal.score(ctx, self.a, self.b), 0.4)
        # explicit param lowering: 4 bytes >= 4 → exact match → 1.0
        self.assertEqual(
            self.signal.score(ctx, self.a, self.b,
                              params={"min_fingerprint_bytes": 4}),
            1.0,
        )


if __name__ == "__main__":
    unittest.main()
