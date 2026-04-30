"""DB-backed path tests for the rotation_cohort signal.

The in-memory cache path is exercised by test_framework.py. These
tests cover the production path that lazy-loads observations from
the packets table — added when we discovered the signal had been
abstaining on every airtag pair in production because no caller
ever populated ctx.cache['observations'].
"""

from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from btviz.cluster.base import Address, ClusterContext, Device
from btviz.cluster.signals.rotation_cohort import (
    RotationCohort, _observations_on_sniffer,
)
from btviz.db.repos import Repos
from btviz.db.store import Store


def _make_store() -> Store:
    d = tempfile.mkdtemp()
    return Store(Path(d) / "test.db")


def _setup_session(store: Store) -> tuple[int, int]:
    """Create a project + session; return (project_id, session_id)."""
    repos = Repos(store)
    proj = repos.projects.create("test")
    sess = repos.sessions.start(proj.id, source_type="live")
    return proj.id, sess.id


def _device(store: Store, mac: str, kind: str = "rs") -> int:
    repos = Repos(store)
    d = repos.devices.upsert(f"{kind}:{mac}", "random_static_mac", now=0.0)
    return d.id


def _seed_packets(store: Store, session_id: int, device_id: int,
                  observations: dict[int, list[float]]) -> None:
    """Insert raw packet rows directly so the DB-backed path has data.

    ``observations`` is {sniffer_id: [ts, ...]}. We also need an
    address row + sniffer row to satisfy FK constraints.
    """
    # Sniffer rows — one per distinct sniffer_id we need.
    for sid in observations:
        store.conn.execute(
            "INSERT OR IGNORE INTO sniffers(id, serial_number, kind,"
            " is_active, removed, first_seen, last_seen) "
            "VALUES (?, ?, 'dongle', 1, 0, 0, 0)",
            (sid, f"sniffer-{sid}"),
        )
    # Address row for this device
    cur = store.conn.execute(
        "INSERT INTO addresses(address, address_type, device_id,"
        " first_seen, last_seen) VALUES (?, 'rpa', ?, 0, 0)",
        (f"aa:bb:cc:00:00:{device_id:02x}", device_id),
    )
    addr_id = cur.lastrowid

    for sniffer_id, timestamps in observations.items():
        for ts in timestamps:
            store.conn.execute(
                "INSERT INTO packets(session_id, device_id, address_id,"
                " ts, rssi, channel, pdu_type, sniffer_id)"
                " VALUES (?, ?, ?, ?, -60, 37, 0, ?)",
                (session_id, device_id, addr_id, ts, sniffer_id),
            )


def _rpa_device(dev_id: int) -> Device:
    return Device(
        id=dev_id,
        device_class="airtag",
        address=Address(bytes_=b"\xaa" * 6, kind="random_resolvable"),
        first_seen=0.0,
        last_seen=0.0,
    )


class DBLoadTests(unittest.TestCase):
    """Verify the DB-backed lazy-load path returns the expected shape."""

    def setUp(self):
        self.store = _make_store()
        _, self.session_id = _setup_session(self.store)
        self.signal = RotationCohort()

    def tearDown(self):
        self.store.close()

    def _ctx(self) -> ClusterContext:
        return ClusterContext(signals={}, profiles={}, now=0.0, db=self.store)

    def test_returns_empty_when_device_has_no_packets(self):
        did = _device(self.store, "00:00:00:00:00:01")
        ctx = self._ctx()
        self.assertEqual(_observations_on_sniffer(ctx, _rpa_device(did)), {})

    def test_loads_packets_into_sniffer_keyed_dict(self):
        did = _device(self.store, "00:00:00:00:00:02")
        _seed_packets(self.store, self.session_id, did, {
            10: [100.0, 200.0, 300.0],
            20: [150.0, 250.0],
        })
        ctx = self._ctx()
        obs = _observations_on_sniffer(ctx, _rpa_device(did))
        self.assertEqual(set(obs.keys()), {10, 20})
        self.assertEqual(sorted(obs[10]), [100.0, 200.0, 300.0])
        self.assertEqual(sorted(obs[20]), [150.0, 250.0])

    def test_caches_result_so_second_call_doesnt_requery(self):
        did = _device(self.store, "00:00:00:00:00:03")
        _seed_packets(self.store, self.session_id, did, {1: [42.0]})
        ctx = self._ctx()
        first = _observations_on_sniffer(ctx, _rpa_device(did))
        # Mutate the underlying DB; a re-query would see the change.
        # Cache should NOT see it.
        addr_id = self.store.conn.execute(
            "SELECT address_id FROM packets WHERE device_id = ?", (did,)
        ).fetchone()[0]
        self.store.conn.execute(
            "INSERT INTO packets(session_id, device_id, address_id,"
            " ts, rssi, channel, pdu_type, sniffer_id) "
            "VALUES (?, ?, ?, 999.0, -60, 37, 0, 1)",
            (self.session_id, did, addr_id),
        )
        second = _observations_on_sniffer(ctx, _rpa_device(did))
        self.assertIs(first, second)
        self.assertEqual(sorted(second[1]), [42.0])  # the new row not visible

    def test_pre_populated_cache_takes_precedence(self):
        did = _device(self.store, "00:00:00:00:00:04")
        _seed_packets(self.store, self.session_id, did, {1: [42.0]})
        ctx = self._ctx()
        # Synthetic-data tests pre-populate ctx.cache["observations"];
        # that path must continue working unchanged.
        ctx.cache["observations"] = {did: {99: [777.0, 888.0]}}
        obs = _observations_on_sniffer(ctx, _rpa_device(did))
        self.assertEqual(obs, {99: [777.0, 888.0]})


class IntegrationWithSignalTests(unittest.TestCase):
    """End-to-end: signal + DB observations + scoring."""

    def setUp(self):
        self.store = _make_store()
        _, self.session_id = _setup_session(self.store)
        self.signal = RotationCohort()
        self.id_a = _device(self.store, "00:00:00:00:00:0a")
        self.id_b = _device(self.store, "00:00:00:00:00:0b")
        self.a = _rpa_device(self.id_a)
        self.b = _rpa_device(self.id_b)

    def tearDown(self):
        self.store.close()

    def _ctx(self) -> ClusterContext:
        return ClusterContext(
            signals={}, profiles={}, now=0.0, db=self.store,
        )

    def test_handoff_at_expected_rotation_scores_high(self):
        # Pass profile params with expected_rotation=30 + window_max=60
        # so the math is internally consistent. (The default airtag
        # profile sets expected=900 + window_max=60 which is a known
        # logic inconsistency in the existing signal — the delta-from-
        # expected branch is unreachable. Flagged in the deep-dive
        # doc for follow-up.)
        params = {
            "expected_rotation": 30.0,
            "window_min": 0.05,
            "window_max": 60.0,
        }
        # Device A at t=0, B at t=30 (gap == expected → score 1.0)
        _seed_packets(self.store, self.session_id, self.id_a, {1: [0.0]})
        _seed_packets(self.store, self.session_id, self.id_b, {1: [30.0]})
        ctx = self._ctx()
        self.assertTrue(self.signal.applies_to(ctx, self.a, self.b))
        score = self.signal.score(ctx, self.a, self.b, params=params)
        self.assertIsNotNone(score)
        self.assertGreater(score, 0.95)

    def test_concurrent_observation_actively_rejects(self):
        # Both observed simultaneously by same sniffer = cannot be one
        # rotating device.
        _seed_packets(self.store, self.session_id, self.id_a, {1: [100.0, 200.0]})
        _seed_packets(self.store, self.session_id, self.id_b, {1: [110.0, 210.0]})
        ctx = self._ctx()
        score = self.signal.score(ctx, self.a, self.b)
        self.assertEqual(score, -1.0)


if __name__ == "__main__":
    unittest.main()
