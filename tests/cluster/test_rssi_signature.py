"""Tests for the ``rssi_signature`` cluster signal.

The signal scores two devices by per-sniffer mean-RSSI agreement.
Same physical device on the same sniffer ⇒ same distance from the
antenna ⇒ near-identical RSSI distributions. Different devices at
different positions ⇒ different distributions.

Reads from the ``packets`` table; abstains when the table is empty
or the two devices don't share enough sniffers.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from btviz.cluster.base import Address, ClusterContext, Device  # noqa: E402
from btviz.cluster.signals.rssi_signature import RssiSignature  # noqa: E402
from btviz.db.repos import Repos  # noqa: E402
from btviz.db.store import Store  # noqa: E402


def _make_store() -> Store:
    d = tempfile.mkdtemp()
    return Store(Path(d) / "rssi.db")


def _setup_session(store: Store) -> int:
    repos = Repos(store)
    proj = repos.projects.create("test")
    sess = repos.sessions.start(proj.id, source_type="live")
    return sess.id


def _device(store: Store, mac: str) -> int:
    repos = Repos(store)
    d = repos.devices.upsert(f"rs:{mac}", "random_static_mac", now=0.0)
    return d.id


def _seed_packets(
    store: Store, session_id: int, device_id: int,
    observations: dict[int, list[tuple[float, int]]],
) -> None:
    """Insert packet rows for a device. Idempotent across multiple
    calls for the same device — the address row is inserted once and
    looked up on subsequent calls.

    ``observations`` is {sniffer_id: [(ts, rssi), ...]}.
    """
    for sid in observations:
        store.conn.execute(
            "INSERT OR IGNORE INTO sniffers(id, serial_number, kind,"
            " is_active, removed, first_seen, last_seen) "
            "VALUES (?, ?, 'dongle', 1, 0, 0, 0)",
            (sid, f"sniffer-{sid}"),
        )
    addr = f"aa:bb:cc:00:00:{device_id:02x}"
    store.conn.execute(
        "INSERT OR IGNORE INTO addresses(address, address_type, device_id,"
        " first_seen, last_seen) VALUES (?, 'rpa', ?, 0, 0)",
        (addr, device_id),
    )
    addr_row = store.conn.execute(
        "SELECT id FROM addresses WHERE address = ? AND address_type = 'rpa'",
        (addr,),
    ).fetchone()
    addr_id = addr_row[0] if addr_row else None
    for sniffer_id, packets in observations.items():
        for ts, rssi in packets:
            store.conn.execute(
                "INSERT INTO packets(session_id, device_id, address_id,"
                " ts, rssi, channel, pdu_type, sniffer_id)"
                " VALUES (?, ?, ?, ?, ?, 37, 0, ?)",
                (session_id, device_id, addr_id, ts, rssi, sniffer_id),
            )


def _rpa(dev_id: int) -> Device:
    return Device(
        id=dev_id,
        device_class="apple_device",
        address=Address(bytes_=b"\xaa" * 6, kind="random_resolvable"),
        first_seen=0.0,
        last_seen=0.0,
    )


def _ctx(store: Store) -> ClusterContext:
    return ClusterContext(
        signals={}, profiles={}, now=0.0, db=store,
    )


class AppliesToTests(unittest.TestCase):

    def setUp(self) -> None:
        self.store = _make_store()
        self.sess = _setup_session(self.store)
        self.signal = RssiSignature()
        self.id_a = _device(self.store, "00:00:00:00:00:0a")
        self.id_b = _device(self.store, "00:00:00:00:00:0b")
        self.a = _rpa(self.id_a)
        self.b = _rpa(self.id_b)

    def tearDown(self) -> None:
        self.store.close()

    def test_no_db_abstains(self):
        ctx = ClusterContext(signals={}, profiles={}, now=0.0, db=None)
        self.assertFalse(self.signal.applies_to(ctx, self.a, self.b))

    def test_one_side_no_packets_abstains(self):
        _seed_packets(self.store, self.sess, self.id_a,
                      {1: [(100.0, -55)]})
        # b has no packets at all
        self.assertFalse(self.signal.applies_to(_ctx(self.store), self.a, self.b))

    def test_disjoint_sniffer_sets_abstain(self):
        _seed_packets(self.store, self.sess, self.id_a, {1: [(100.0, -55)]})
        _seed_packets(self.store, self.sess, self.id_b, {2: [(100.0, -55)]})
        self.assertFalse(self.signal.applies_to(_ctx(self.store), self.a, self.b))

    def test_overlapping_sniffer_sets_apply(self):
        _seed_packets(self.store, self.sess, self.id_a, {1: [(100.0, -55)]})
        _seed_packets(self.store, self.sess, self.id_b, {1: [(100.0, -55)]})
        self.assertTrue(self.signal.applies_to(_ctx(self.store), self.a, self.b))


class ScoreTests(unittest.TestCase):

    def setUp(self) -> None:
        self.store = _make_store()
        self.sess = _setup_session(self.store)
        self.signal = RssiSignature()
        self.id_a = _device(self.store, "00:00:00:00:00:0a")
        self.id_b = _device(self.store, "00:00:00:00:00:0b")
        self.a = _rpa(self.id_a)
        self.b = _rpa(self.id_b)

    def tearDown(self) -> None:
        self.store.close()

    def _seed_block(
        self, dev_id: int, sniffer_id: int, base_ts: float,
        rssi_values: list[int],
    ) -> None:
        _seed_packets(
            self.store, self.sess, dev_id,
            {sniffer_id: [
                (base_ts + 0.05 * i, r) for i, r in enumerate(rssi_values)
            ]},
        )

    def test_identical_means_score_near_one(self):
        # 5 packets on each of 2 sniffers, same mean per sniffer.
        for s in (1, 2):
            self._seed_block(self.id_a, s, 100.0, [-55, -55, -55, -55, -55])
            self._seed_block(self.id_b, s, 100.0, [-55, -55, -55, -55, -55])
        score = self.signal.score(_ctx(self.store), self.a, self.b)
        self.assertIsNotNone(score)
        self.assertGreater(score, 0.95)

    def test_far_apart_means_score_zero(self):
        # 30 dB apart on each sniffer, well past the std_floor =
        # 1.5 dB and the default z_full_mismatch = 4.0.
        for s in (1, 2):
            self._seed_block(self.id_a, s, 100.0, [-30, -30, -30, -30, -30])
            self._seed_block(self.id_b, s, 100.0, [-90, -90, -90, -90, -90])
        score = self.signal.score(_ctx(self.store), self.a, self.b)
        self.assertEqual(score, 0.0)

    def test_partial_disagreement_scores_in_between(self):
        # Sniffer 1: identical (1.0). Sniffer 2: ~2σ apart (0.5).
        # Final = mean of per-sniffer scores.
        self._seed_block(self.id_a, 1, 100.0, [-55, -55, -55, -55, -55])
        self._seed_block(self.id_b, 1, 100.0, [-55, -55, -55, -55, -55])
        # Use a synthetic stddev (3 dB spread) so the floor doesn't
        # dominate — z = 3 / 3 = 1, score = 0.75.
        self._seed_block(self.id_a, 2, 100.0, [-50, -52, -54, -55, -56])
        self._seed_block(self.id_b, 2, 100.0, [-53, -55, -57, -58, -59])
        score = self.signal.score(_ctx(self.store), self.a, self.b)
        self.assertIsNotNone(score)
        self.assertGreater(score, 0.5)
        self.assertLess(score, 1.0)

    def test_below_min_sniffers_abstains(self):
        # Only one shared sniffer — default min_sniffers=2.
        self._seed_block(self.id_a, 1, 100.0, [-55, -55, -55])
        self._seed_block(self.id_b, 1, 100.0, [-55, -55, -55])
        self.assertIsNone(self.signal.score(_ctx(self.store), self.a, self.b))

    def test_min_sniffers_override_allows_single_sniffer_match(self):
        # The hearing_aid profile sets min_sniffers=1 because real
        # captures often only have one dongle covering an HA's
        # advertising channel — verify the override works.
        self._seed_block(self.id_a, 1, 100.0, [-55, -55, -55])
        self._seed_block(self.id_b, 1, 100.0, [-55, -55, -55])
        score = self.signal.score(
            _ctx(self.store), self.a, self.b,
            params={"min_sniffers": 1},
        )
        self.assertIsNotNone(score)
        self.assertGreater(score, 0.95)

    def test_recent_window_excludes_old_packets(self):
        # Per-sniffer "now" is the latest ts on that sniffer; a 5 s
        # window should exclude packets >5 s old. We give each
        # device 3 fresh packets at t=100..100.1 plus 3 stale ones
        # at t=10..10.1 with very different means. The signal should
        # only see the fresh ones.
        _seed_packets(self.store, self.sess, self.id_a, {
            1: [(10.0, -10), (10.05, -10), (10.1, -10),
                (100.0, -55), (100.05, -55), (100.1, -55)],
            2: [(10.0, -10), (10.05, -10), (10.1, -10),
                (100.0, -55), (100.05, -55), (100.1, -55)],
        })
        _seed_packets(self.store, self.sess, self.id_b, {
            1: [(10.0, -90), (10.05, -90), (10.1, -90),
                (100.0, -55), (100.05, -55), (100.1, -55)],
            2: [(10.0, -90), (10.05, -90), (10.1, -90),
                (100.0, -55), (100.05, -55), (100.1, -55)],
        })
        # 5 s recent_window keeps only the fresh block (t≈100..100.1)
        # → identical means → near-1.0. Without the window, the wide
        # spread would drag the score down.
        score = self.signal.score(
            _ctx(self.store), self.a, self.b,
            params={"recent_window": 5.0},
        )
        self.assertIsNotNone(score)
        self.assertGreater(score, 0.95)

    def test_min_packets_per_sniffer_filters_thin_data(self):
        # Sniffer 1 has plenty of samples (qualifies).
        # Sniffer 2 has only 1 packet on each side (default
        # min_packets_per_sniffer=3 → drops out).
        # Result: only sniffer 1 contributes. Below default
        # min_sniffers=2 → abstain.
        self._seed_block(self.id_a, 1, 100.0, [-55, -55, -55, -55, -55])
        self._seed_block(self.id_b, 1, 100.0, [-55, -55, -55, -55, -55])
        self._seed_block(self.id_a, 2, 100.0, [-55])
        self._seed_block(self.id_b, 2, 100.0, [-55])
        self.assertIsNone(self.signal.score(_ctx(self.store), self.a, self.b))


class CacheRespectsParamOverridesTests(unittest.TestCase):
    """The signal caches raw packet rows per device, NOT pre-windowed
    statistics — so ``score`` calls with different ``recent_window`` /
    ``min_packets_per_sniffer`` see correctly-filtered data even when
    the same context is reused across pairs / classes."""

    def test_same_ctx_two_window_settings(self):
        # If the cache stored pre-windowed stats keyed only by
        # device id, both ``score`` calls below would return the
        # same value because the second call would hit the cached
        # data from the first. The signal caches raw packet rows
        # (param-independent) so the windowing runs fresh on each
        # call — proven here by the two scores diverging materially.
        store = _make_store()
        try:
            sess = _setup_session(store)
            id_a = _device(store, "00:00:00:00:00:0a")
            id_b = _device(store, "00:00:00:00:00:0b")
            for s in (1, 2):
                _seed_packets(store, sess, id_a, {
                    s: [(10.0, -90), (10.05, -90), (10.1, -90),
                        (100.0, -55), (100.05, -55), (100.1, -55)],
                })
                _seed_packets(store, sess, id_b, {
                    s: [(10.0, -10), (10.05, -10), (10.1, -10),
                        (100.0, -55), (100.05, -55), (100.1, -55)],
                })
            ctx = _ctx(store)
            sig = RssiSignature()
            a, b = _rpa(id_a), _rpa(id_b)

            # Wide window includes the old, far-apart block.
            score_wide = sig.score(ctx, a, b, params={"recent_window": 200.0})
            # Narrow window keeps only the fresh, identical block.
            score_narrow = sig.score(ctx, a, b, params={"recent_window": 5.0})
            self.assertIsNotNone(score_wide)
            self.assertIsNotNone(score_narrow)
            # Narrow window sees identical means → near 1.0.
            self.assertGreater(score_narrow, 0.95)
            # Wide window sees a much messier distribution → score
            # is materially lower (the per-sniffer pooled stddev
            # widens, dragging the agreement score down).
            self.assertLess(score_wide, score_narrow - 0.3)
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
