"""Tests for the co_lifespan_match signal."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from btviz.cluster.base import Address, ClusterContext, Device
from btviz.cluster.signals.co_lifespan_match import (
    CoLifespanMatch, _score_window_pair,
)
from btviz.db.repos import Repos
from btviz.db.store import Store


def _make_store() -> Store:
    d = tempfile.mkdtemp()
    return Store(Path(d) / "test.db")


def _setup(store: Store) -> tuple[int, int]:
    repos = Repos(store)
    proj = repos.projects.create("test")
    sess = repos.sessions.start(proj.id, source_type="live")
    return proj.id, sess.id


def _seed_device(
    store: Store, session_id: int, stable_key: str,
    first_seen: float, last_seen: float, packet_count: int = 100,
) -> int:
    """Insert a device + an observations row spanning the window."""
    repos = Repos(store)
    d = repos.devices.upsert(stable_key, "random_static_mac", now=first_seen)
    store.conn.execute(
        "INSERT INTO observations(session_id, device_id, packet_count,"
        " adv_count, data_count, rssi_sum, rssi_samples,"
        " first_seen, last_seen, pdu_types_json, channels_json, phy_json)"
        " VALUES (?, ?, ?, ?, 0, 0, 0, ?, ?, '{}', '{}', '{}')",
        (session_id, d.id, packet_count, packet_count, first_seen, last_seen),
    )
    return d.id


def _device(did: int) -> Device:
    return Device(
        id=did,
        device_class="apple_device",
        address=Address(
            bytes_=bytes.fromhex(f"aabbccddee{did:02x}"),
            kind="random_resolvable",
        ),
        first_seen=0.0,
        last_seen=0.0,
    )


# ---------------------------------------------------------------- pure helpers

class TestScoreWindowPair(unittest.TestCase):
    """Pure-function tests for ``_score_window_pair`` — no DB needed."""

    def test_identical_concurrent_windows_score_high(self):
        # Two RPAs that both started together and are both still active —
        # the pattern from session 93's 16333/16334 apple_device pair.
        score = _score_window_pair(
            (1000.0, 1500.0), (1000.0, 1500.0),
            min_overlap_pct=0.90,
            max_handoff_gap=60.0,
            near_instant_gap=5.0,
        )
        self.assertEqual(score, 0.95)

    def test_almost_identical_concurrent_windows_score_high(self):
        # Slight clock skew between sniffers — both windows essentially
        # identical (overlap > 95 %).
        score = _score_window_pair(
            (1000.0, 1500.0), (1002.0, 1499.0),
            min_overlap_pct=0.90,
            max_handoff_gap=60.0,
            near_instant_gap=5.0,
        )
        self.assertEqual(score, 0.95)

    def test_partial_overlap_scores_weak(self):
        # 250s overlap inside a 750s union = 0.33 — below 0.5 threshold.
        score = _score_window_pair(
            (1000.0, 1500.0), (1250.0, 1750.0),
            min_overlap_pct=0.90,
            max_handoff_gap=60.0,
            near_instant_gap=5.0,
        )
        self.assertEqual(score, 0.0)

    def test_significant_overlap_scores_moderate(self):
        # 400s overlap inside a 600s union = 0.67 — between 0.5 and 0.90.
        score = _score_window_pair(
            (1000.0, 1500.0), (1100.0, 1600.0),
            min_overlap_pct=0.90,
            max_handoff_gap=60.0,
            near_instant_gap=5.0,
        )
        self.assertEqual(score, 0.4)

    def test_near_instant_handoff_scores_high(self):
        # Airtag rotation pattern from session 93's 16324 → 16335:
        # one ends at 1500, the next starts at 1500.
        score = _score_window_pair(
            (1000.0, 1500.0), (1500.0, 2000.0),
            min_overlap_pct=0.90,
            max_handoff_gap=60.0,
            near_instant_gap=5.0,
        )
        self.assertEqual(score, 0.95)

    def test_handoff_at_max_gap_scores_low(self):
        # 60s gap is the boundary — score decays linearly.
        score = _score_window_pair(
            (1000.0, 1500.0), (1560.0, 2000.0),
            min_overlap_pct=0.90,
            max_handoff_gap=60.0,
            near_instant_gap=5.0,
        )
        self.assertAlmostEqual(score, 0.45, places=5)

    def test_handoff_beyond_max_gap_abstains(self):
        score = _score_window_pair(
            (1000.0, 1500.0), (1700.0, 2000.0),
            min_overlap_pct=0.90,
            max_handoff_gap=60.0,
            near_instant_gap=5.0,
        )
        self.assertIsNone(score)

    def test_disjoint_order_does_not_matter(self):
        # Same two windows, swapped order — should yield the same score.
        a = (1000.0, 1500.0)
        b = (1500.0, 2000.0)
        s1 = _score_window_pair(
            a, b, min_overlap_pct=0.90,
            max_handoff_gap=60.0, near_instant_gap=5.0,
        )
        s2 = _score_window_pair(
            b, a, min_overlap_pct=0.90,
            max_handoff_gap=60.0, near_instant_gap=5.0,
        )
        self.assertEqual(s1, s2)


# --------------------------------------------------------------- DB-backed

class TestCoLifespanMatchSignal(unittest.TestCase):
    """Integration tests that exercise the SQL paths."""

    def test_concurrent_windows_in_one_session_score_high(self):
        store = _make_store()
        _, sid = _setup(store)
        a_id = _seed_device(store, sid, "rs:aa", 1000.0, 1500.0)
        b_id = _seed_device(store, sid, "rs:bb", 1000.0, 1500.0)

        sig = CoLifespanMatch()
        ctx = ClusterContext(
            signals={}, profiles={}, now=1500.0, db=store,
        )
        score = sig.score(ctx, _device(a_id), _device(b_id))
        self.assertEqual(score, 0.95)

    def test_no_common_session_abstains(self):
        store = _make_store()
        repos = Repos(store)
        proj = repos.projects.create("test")
        s1 = repos.sessions.start(proj.id, source_type="live")
        s2 = repos.sessions.start(proj.id, source_type="live")
        a_id = _seed_device(store, s1.id, "rs:aa", 1000.0, 1500.0)
        b_id = _seed_device(store, s2.id, "rs:bb", 1000.0, 1500.0)

        sig = CoLifespanMatch()
        ctx = ClusterContext(
            signals={}, profiles={}, now=1500.0, db=store,
        )
        self.assertIsNone(sig.score(ctx, _device(a_id), _device(b_id)))

    def test_best_session_wins(self):
        # Two common sessions — one with weak alignment, one with
        # strong. Should return the strong score.
        store = _make_store()
        repos = Repos(store)
        proj = repos.projects.create("test")
        s1 = repos.sessions.start(proj.id, source_type="live")
        s2 = repos.sessions.start(proj.id, source_type="live")
        # Session 1: weak overlap
        _seed_device(store, s1.id, "rs:aa", 1000.0, 1500.0)
        _seed_device(store, s1.id, "rs:bb", 1450.0, 2000.0)
        # Session 2: identical windows (re-seed under same stable_keys)
        a_id = store.conn.execute(
            "SELECT id FROM devices WHERE stable_key='rs:aa'"
        ).fetchone()[0]
        b_id = store.conn.execute(
            "SELECT id FROM devices WHERE stable_key='rs:bb'"
        ).fetchone()[0]
        store.conn.execute(
            "INSERT INTO observations(session_id, device_id, packet_count,"
            " adv_count, data_count, rssi_sum, rssi_samples,"
            " first_seen, last_seen, pdu_types_json, channels_json, phy_json)"
            " VALUES (?, ?, 100, 100, 0, 0, 0, ?, ?, '{}', '{}', '{}')",
            (s2.id, a_id, 3000.0, 3500.0),
        )
        store.conn.execute(
            "INSERT INTO observations(session_id, device_id, packet_count,"
            " adv_count, data_count, rssi_sum, rssi_samples,"
            " first_seen, last_seen, pdu_types_json, channels_json, phy_json)"
            " VALUES (?, ?, 100, 100, 0, 0, 0, ?, ?, '{}', '{}', '{}')",
            (s2.id, b_id, 3000.0, 3500.0),
        )

        sig = CoLifespanMatch()
        ctx = ClusterContext(
            signals={}, profiles={}, now=4000.0, db=store,
        )
        self.assertEqual(sig.score(ctx, _device(a_id), _device(b_id)), 0.95)

    def test_no_db_abstains(self):
        sig = CoLifespanMatch()
        ctx = ClusterContext(signals={}, profiles={}, now=0.0, db=None)
        self.assertFalse(sig.applies_to(ctx, _device(1), _device(2)))
        self.assertIsNone(sig.score(ctx, _device(1), _device(2)))


if __name__ == "__main__":
    unittest.main()
