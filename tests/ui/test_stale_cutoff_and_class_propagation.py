"""Two coupled fixes for canvas reload behaviour.

Bug 1 — staleness cutoff was computed in the wallclock domain
(``time.time()``) but compared against ``observations.last_seen``
which lives in the dongle firmware-clock domain. When the firmware
clock drifts ahead of wallclock (observed at ~53 min skew on the
user's setup), no device ever falls outside the window and the
"Show: 5s" filter does nothing. Fix: anchor the cutoff in the
packet-clock domain via ``MAX(observations.last_seen)``.

Bug 2 — ``user_device_class`` was per-row only, with no cluster
propagation. The cluster runner can re-elect a different primary
between reloads; the user's override set on member X then becomes
invisible once the primary is member Y. ``user_name`` already
propagates across the cluster — this test pins parity between the
two override fields.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from btviz.db.repos import Repos  # noqa: E402
from btviz.db.store import Store  # noqa: E402
from btviz.ui.canvas import load_canvas_devices  # noqa: E402


def _make_db():
    d = tempfile.mkdtemp()
    store = Store(Path(d) / "scp.db")
    repos = Repos(store)
    project = repos.projects.create("p")
    sess = repos.sessions.start(
        project.id, source_type="live", name="s",
    )
    return store, repos, project, sess


def _seed_dev(repos: Repos, sess_id: int, suffix: str, *,
              ts: float, auto_class: str = "apple_device",
              packet_count: int = 1) -> int:
    dev = repos.devices.upsert(
        f"rpa:00:11:22:33:44:{suffix}", "rpa",
    )
    repos.devices.merge_identity(dev.id, device_class=auto_class)
    # Multiple packets let tests promote a specific device to be the
    # cluster primary by making it the most-active member —
    # ``_pick_cluster_primary`` ties on packet count.
    for i in range(packet_count):
        repos.observations.record_packet(
            sess_id, dev.id,
            ts=ts + i * 0.001, is_adv=True, rssi=-60, channel=37,
            phy="1M", pdu_type="ADV_IND",
        )
    repos.addresses.upsert(
        f"00:11:22:33:44:{suffix}", "rpa", dev.id, now=ts,
    )
    return dev.id


def _seed_cluster(store: Store, cluster_id: int, device_ids: list[int],
                  ts: float = 1000.0) -> None:
    """Insert a cluster + its members directly into the DB."""
    store.conn.execute(
        "INSERT INTO device_clusters (id, created_at, last_decided_at, source) "
        "VALUES (?, ?, ?, 'auto')", (cluster_id, ts, ts),
    )
    for dev_id in device_ids:
        store.conn.execute(
            "INSERT INTO device_cluster_members "
            "(cluster_id, device_id, score, contributions, "
            "profile, decided_at, decided_by) "
            "VALUES (?, ?, 0.95, '{}', 'apple_device', ?, 'auto')",
            (cluster_id, dev_id, ts),
        )


class StaleCutoffClockDomainTests(unittest.TestCase):
    """Cutoff must use packet-clock domain for the fresh-window check.

    The bug surfaced when the firmware clock ran 53 minutes ahead of
    wallclock — every device's last_seen was far in the wallclock
    future, so ``MAX(last_seen) >= time.time() - window`` was always
    true and devices never aged out.
    """

    def test_devices_visible_when_recent_in_packet_clock(self):
        # Two devices, both stamped with packet ts far in the
        # wallclock future (simulating dongle-ahead-of-wallclock by
        # 1e9 sec — well beyond any realistic skew). Both should
        # remain visible against a 5-second window because they're
        # both fresh in *packet* time.
        store, repos, project, sess = _make_db()
        try:
            far_future = 9_999_999_999.0
            a = _seed_dev(repos, sess.id, "01", ts=far_future)
            b = _seed_dev(repos, sess.id, "02", ts=far_future - 2.0)
            loaded = {d.device_id for d in load_canvas_devices(
                store, project.id, stale_cutoff=None,
            )}
            self.assertEqual(loaded, {a, b})

            # 5-second window in packet-clock domain — both devices
            # were observed within 2 sec, so still visible.
            loaded = {d.device_id for d in load_canvas_devices(
                store, project.id,
                stale_cutoff=far_future - 5.0,
            )}
            self.assertEqual(loaded, {a, b})
        finally:
            store.close()

    def test_devices_filtered_when_stale_in_packet_clock(self):
        # One fresh, one stale by packet-clock domain. Cutoff in
        # packet-clock domain must hide only the stale one. This
        # mirrors what the canvas's reload now does: it queries
        # MAX(observations.last_seen) and subtracts the window from
        # it before passing as ``stale_cutoff``.
        store, repos, project, sess = _make_db()
        try:
            far = 9_999_999_999.0
            fresh = _seed_dev(repos, sess.id, "01", ts=far)
            stale = _seed_dev(repos, sess.id, "02", ts=far - 60.0)
            loaded = {d.device_id for d in load_canvas_devices(
                store, project.id, stale_cutoff=far - 5.0,
            )}
            self.assertIn(fresh, loaded)
            self.assertNotIn(stale, loaded)
        finally:
            store.close()


class UserDeviceClassClusterPropagationTests(unittest.TestCase):
    """An override set on any cluster member shows up on every other
    visible member (and the rendered primary). Mirrors the existing
    user_name propagation behaviour."""

    def test_override_on_member_propagates_to_collapsed_primary(self):
        # The user-reported scenario: the cluster runner re-elects a
        # different primary across reloads, so the override the user
        # set on member X must travel to whatever member becomes the
        # primary. Cluster collapse runs *after* propagation in
        # load_canvas_devices, so the surviving primary inherits.
        # Force the primary to be ``most_active`` (more packets ⇒
        # higher rank in _pick_cluster_primary) so a different row
        # carries the override than the rendered primary.
        store, repos, project, sess = _make_db()
        try:
            ts = 1000.0
            most_active = _seed_dev(
                repos, sess.id, "01", ts=ts, packet_count=10,
            )
            override_holder = _seed_dev(
                repos, sess.id, "02", ts=ts, packet_count=1,
            )
            _seed_cluster(store, cluster_id=1,
                          device_ids=[most_active, override_holder])
            repos.devices.set_user_device_class(override_holder, "iphone")

            loaded = list(load_canvas_devices(store, project.id))
            # Cluster collapse leaves exactly one primary box.
            self.assertEqual(len(loaded), 1)
            primary = loaded[0]
            # The primary is the more-active row, NOT the override
            # holder — pinned so the test exercises propagation.
            self.assertEqual(primary.device_id, most_active)
            # And it carries the override propagated from member.
            self.assertEqual(primary.device_class, "iphone")
            self.assertEqual(primary.user_device_class, "iphone")
            # auto_device_class still reflects wire inference for
            # the tooltip's "(auto: …)" suffix.
            self.assertEqual(primary.auto_device_class, "apple_device")
        finally:
            store.close()

    def test_two_distinct_overrides_in_one_cluster_do_not_propagate(self):
        # Mirror the user_name ambiguity rule. If two members
        # disagree on the override, leave each as it stands and
        # don't propagate to a third unset member. We make the
        # unset member the cluster primary so we can assert on its
        # post-collapse state.
        store, repos, project, sess = _make_db()
        try:
            ts = 1000.0
            unset_primary = _seed_dev(
                repos, sess.id, "01", ts=ts, packet_count=10,
            )
            override_a = _seed_dev(
                repos, sess.id, "02", ts=ts, packet_count=1,
            )
            override_b = _seed_dev(
                repos, sess.id, "03", ts=ts, packet_count=1,
            )
            _seed_cluster(store, cluster_id=1,
                          device_ids=[unset_primary, override_a, override_b])
            repos.devices.set_user_device_class(override_a, "iphone")
            repos.devices.set_user_device_class(override_b, "ipad")

            loaded = list(load_canvas_devices(store, project.id))
            self.assertEqual(len(loaded), 1)
            primary = loaded[0]
            self.assertEqual(primary.device_id, unset_primary)
            # Without propagation, the unset primary keeps its
            # auto-detected value. With ambiguous propagation, it
            # would have inherited "iphone" or "ipad" arbitrarily.
            self.assertEqual(primary.device_class, "apple_device")
            self.assertIsNone(primary.user_device_class)
        finally:
            store.close()

    def test_unique_override_propagates_even_when_held_by_member(self):
        # Single override anywhere in the cluster ⇒ unambiguous ⇒
        # propagate. Mirror of the user_name behaviour. Two members
        # plus the override-holder; primary is the one with the
        # most packets; override is on a low-activity member.
        store, repos, project, sess = _make_db()
        try:
            ts = 1000.0
            primary_id = _seed_dev(
                repos, sess.id, "01", ts=ts, packet_count=10,
            )
            other_unset = _seed_dev(
                repos, sess.id, "02", ts=ts, packet_count=1,
            )
            override_holder = _seed_dev(
                repos, sess.id, "03", ts=ts, packet_count=1,
            )
            _seed_cluster(
                store, cluster_id=1,
                device_ids=[primary_id, other_unset, override_holder],
            )
            repos.devices.set_user_device_class(override_holder, "auracast_source")

            loaded = list(load_canvas_devices(store, project.id))
            self.assertEqual(len(loaded), 1)
            self.assertEqual(loaded[0].device_class, "auracast_source")
            self.assertEqual(loaded[0].user_device_class, "auracast_source")
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
