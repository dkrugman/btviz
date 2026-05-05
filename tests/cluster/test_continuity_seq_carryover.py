"""Tests for the continuity_seq_carryover signal.

Covers Handoff seq extraction at the protocol layer + the cluster
signal's pair-scoring rules. Builds the same test fixtures as the
apple_continuity tests for symmetry.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from btviz.cluster.base import Address, ClusterContext, Device
from btviz.cluster.signals._continuity_protocol import (
    APPLE_CID_BE, extract_handoff_seq, parse_continuity,
)
from btviz.cluster.signals.continuity_seq_carryover import (
    ContinuitySeqCarryover,
)
from btviz.db.repos import Repos
from btviz.db.store import Store

AD_TYPE_MFG = 0xFF


def _make_store() -> Store:
    d = tempfile.mkdtemp()
    return Store(Path(d) / "test.db")


def _device_row(store: Store, stable_key: str) -> int:
    return Repos(store).devices.upsert(
        stable_key, "random_static_mac", now=0.0,
    ).id


def _device(dev_id: int) -> Device:
    return Device(
        id=dev_id,
        device_class="apple_device",
        address=Address(bytes_=b"\xaa" * 6, kind="random_resolvable"),
        first_seen=0.0,
        last_seen=0.0,
    )


def _ctx(store: Store) -> ClusterContext:
    return ClusterContext(signals={}, profiles={}, now=0.0, db=store)


def _handoff_blob(seq: int, *, clipboard: int = 0x00,
                  trailing: bytes = b"\x00" * 11) -> bytes:
    """Construct an Apple-CID mfg-data blob with one Handoff TLV.

    Layout per Martin et al §4.2 fig 2:
        [Apple CID 0x4C 0x00] [Type 0x0C] [Length] [Clipboard 1B] [Seq 2B] [Data...]
    """
    payload = bytes([clipboard]) + seq.to_bytes(2, "big") + trailing
    return APPLE_CID_BE + bytes([0x0C, len(payload)]) + payload


def _seed_mfg(store: Store, device_id: int, *,
              blob: bytes, first_seen: float, last_seen: float) -> None:
    """Insert one device_ad_history row with the given timing."""
    store.conn.execute(
        "INSERT INTO device_ad_history"
        " (device_id, ad_type, ad_value, first_seen, last_seen, count)"
        " VALUES (?, ?, ?, ?, ?, 1)",
        (device_id, AD_TYPE_MFG, blob, first_seen, last_seen),
    )


# ─── Protocol-layer tests ──────────────────────────────────────────────

class HandoffDecodeTests(unittest.TestCase):
    """Verify cleartext seq extraction from Handoff TLV."""

    def test_extract_seq_from_clean_handoff_blob(self):
        blob = _handoff_blob(seq=0x1234)
        self.assertEqual(extract_handoff_seq(blob), 0x1234)

    def test_extract_returns_none_when_no_handoff_tlv(self):
        # iBeacon-only blob — no 0x0C TLV present.
        blob = APPLE_CID_BE + bytes([0x02, 0x15]) + b"\x00" * 21
        self.assertIsNone(extract_handoff_seq(blob))

    def test_extract_returns_none_for_non_apple_blob(self):
        self.assertIsNone(extract_handoff_seq(b"\x00\x4C" + b"\x00" * 8))

    def test_seq_survives_truncated_trailing_data(self):
        # Many real-world Handoff captures show short payloads; we
        # only need the first 3 bytes (clipboard + seq) to read seq.
        payload = b"\x00\xAB\xCD"
        blob = APPLE_CID_BE + bytes([0x0C, 3]) + payload
        self.assertEqual(extract_handoff_seq(blob), 0xABCD)

    def test_parser_decodes_seq_into_tlv_metadata(self):
        # The full parser exposes seq via the .decoded field too,
        # which is what the apple_continuity dialog / log surfaces.
        tlvs = parse_continuity(_handoff_blob(seq=42))
        self.assertEqual(len(tlvs), 1)
        self.assertEqual(tlvs[0].decoded["seq"], 42)


# ─── Signal-layer tests ────────────────────────────────────────────────

class CarryoverSignalTests(unittest.TestCase):
    """End-to-end through the DB-backed signal."""

    def setUp(self):
        self.store = _make_store()
        self.a_id = _device_row(self.store, "rs:aa:bb:cc:dd:ee:11")
        self.b_id = _device_row(self.store, "rs:aa:bb:cc:dd:ee:22")
        self.sig = ContinuitySeqCarryover()
        self.ctx = _ctx(self.store)

    def tearDown(self):
        self.store.close()

    def test_exact_carryover_scores_one(self):
        # Device A had seq=100 around t=10; B picks up at seq=101 at
        # t=15. Classic RPA-rotation carry-over.
        _seed_mfg(self.store, self.a_id,
                  blob=_handoff_blob(100), first_seen=8.0, last_seen=12.0)
        _seed_mfg(self.store, self.b_id,
                  blob=_handoff_blob(101), first_seen=15.0, last_seen=20.0)
        score = self.sig.score(
            self.ctx, _device(self.a_id), _device(self.b_id),
        )
        self.assertEqual(score, 1.0)

    def test_close_but_not_adjacent_seq_scores_partial(self):
        # Δseq = 3 — within max_seq_gap=5, awards 0.6 (probably
        # missed packets at the rotation boundary).
        _seed_mfg(self.store, self.a_id,
                  blob=_handoff_blob(100), first_seen=8.0, last_seen=12.0)
        _seed_mfg(self.store, self.b_id,
                  blob=_handoff_blob(103), first_seen=15.0, last_seen=20.0)
        self.assertEqual(
            self.sig.score(self.ctx, _device(self.a_id), _device(self.b_id)),
            0.6,
        )

    def test_far_apart_seq_scores_zero(self):
        # Δseq = 50 — these are unrelated devices' trajectories.
        _seed_mfg(self.store, self.a_id,
                  blob=_handoff_blob(100), first_seen=8.0, last_seen=12.0)
        _seed_mfg(self.store, self.b_id,
                  blob=_handoff_blob(150), first_seen=15.0, last_seen=20.0)
        self.assertEqual(
            self.sig.score(self.ctx, _device(self.a_id), _device(self.b_id)),
            0.0,
        )

    def test_time_window_too_wide_scores_zero(self):
        # Δseq = 1 (would be exact carry-over) but they're 30 min
        # apart — beyond the default 600 s window. Reject.
        _seed_mfg(self.store, self.a_id,
                  blob=_handoff_blob(100), first_seen=0.0, last_seen=10.0)
        _seed_mfg(self.store, self.b_id,
                  blob=_handoff_blob(101), first_seen=2000.0, last_seen=2010.0)
        self.assertEqual(
            self.sig.score(self.ctx, _device(self.a_id), _device(self.b_id)),
            0.0,
        )

    def test_abstains_when_one_device_has_no_handoff(self):
        # A has Handoff, B doesn't. Signal can't compare → None.
        _seed_mfg(self.store, self.a_id,
                  blob=_handoff_blob(100), first_seen=8.0, last_seen=12.0)
        # Seed B with a non-Handoff TLV (NearbyInfo).
        nearby = APPLE_CID_BE + bytes([0x10, 0x05, 0x47, 0x1c, 0x69, 0xee, 0x65])
        _seed_mfg(self.store, self.b_id,
                  blob=nearby, first_seen=15.0, last_seen=20.0)
        self.assertIsNone(
            self.sig.score(self.ctx, _device(self.a_id), _device(self.b_id))
        )

    def test_picks_best_pair_from_multiple_observations(self):
        # A has seq=100 then seq=101 (two observations); B picks up
        # at seq=102. The pair (a_seq=101 → b_seq=102) should win.
        _seed_mfg(self.store, self.a_id,
                  blob=_handoff_blob(100), first_seen=0.0, last_seen=2.0)
        _seed_mfg(self.store, self.a_id,
                  blob=_handoff_blob(101), first_seen=8.0, last_seen=12.0)
        _seed_mfg(self.store, self.b_id,
                  blob=_handoff_blob(102), first_seen=15.0, last_seen=20.0)
        self.assertEqual(
            self.sig.score(self.ctx, _device(self.a_id), _device(self.b_id)),
            1.0,
        )

    def test_overlapping_windows_use_zero_dt(self):
        # If both devices broadcast Handoff with adjacent seq while
        # their windows overlap, the time-gap test passes even
        # though there's no clean "old then new" sequencing. Real
        # case: a Mac and an iPhone of the same user emitting
        # different Handoff streams concurrently. Same-device or
        # not? The signal shouldn't false-positive here, but seq=N
        # and seq=N+1 with overlapping windows is rare for two
        # different devices. We accept it (1.0) and rely on other
        # signals to push back if it's a false match.
        _seed_mfg(self.store, self.a_id,
                  blob=_handoff_blob(100), first_seen=0.0, last_seen=20.0)
        _seed_mfg(self.store, self.b_id,
                  blob=_handoff_blob(101), first_seen=10.0, last_seen=30.0)
        self.assertEqual(
            self.sig.score(self.ctx, _device(self.a_id), _device(self.b_id)),
            1.0,
        )


# ─── Loader / kill-switch tests ────────────────────────────────────────

class SignalLoaderTests(unittest.TestCase):
    """``load_signals`` honors per-signal enable flags from preferences."""

    def setUp(self):
        # Reset the prefs singleton so each test loads fresh from
        # its own temp dir.
        from btviz.preferences import reset_singleton_for_tests
        reset_singleton_for_tests(None)
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        from btviz.preferences import reset_singleton_for_tests
        reset_singleton_for_tests(None)

    def _install_prefs(self, **flags) -> None:
        """Construct + cache a Preferences object with the given
        signal flags overridden, leaving every other field at default."""
        from btviz.preferences import Preferences, reset_singleton_for_tests
        prefs = Preferences.load(Path(self.tmpdir))
        for name, enabled in flags.items():
            prefs.set(f"cluster.signals.{name}", enabled)
        reset_singleton_for_tests(prefs)

    def test_default_loads_all_signals(self):
        from btviz.cluster.signals import load_signals
        self._install_prefs()  # all defaults = all enabled
        sigs = load_signals()
        # The six known signal names.
        self.assertIn("apple_continuity", sigs)
        self.assertIn("co_lifespan_match", sigs)
        self.assertIn("mfg_data_prefix", sigs)
        self.assertIn("rotation_cohort", sigs)
        self.assertIn("service_uuid_match", sigs)
        self.assertIn("continuity_seq_carryover", sigs)

    def test_disabling_a_signal_drops_it_from_load(self):
        from btviz.cluster.signals import load_signals
        self._install_prefs(co_lifespan_match=False)
        sigs = load_signals()
        self.assertNotIn("co_lifespan_match", sigs)
        # Other signals still present.
        self.assertIn("apple_continuity", sigs)
        self.assertIn("continuity_seq_carryover", sigs)

    def test_disabling_continuity_seq_drops_it(self):
        from btviz.cluster.signals import load_signals
        self._install_prefs(continuity_seq_carryover=False)
        sigs = load_signals()
        self.assertNotIn("continuity_seq_carryover", sigs)


if __name__ == "__main__":
    unittest.main()
