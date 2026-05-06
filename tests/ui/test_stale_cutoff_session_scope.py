"""Per-session staleness cutoff scope.

The previous fix anchored the staleness cutoff in the packet-clock
domain via ``MAX(observations.last_seen)``, but did so project-wide.
On multi-session DBs that fails because each capture session has its
own firmware-clock baseline (dongle replug resets the firmware
clock). A leftover row in some prior session can sit at a higher
firmware ts than the active session's freshest packet, swamping the
project-wide MAX and hiding currently-active devices that didn't
happen to also exist in the high-baseline session.

The fix: the staleness cutoff anchor AND the per-device MAX in the
HAVING clause both scope to the most-recent session, keeping both
sides of the comparison in the same firmware-clock domain.
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
    store = Store(Path(d) / "ssc.db")
    repos = Repos(store)
    project = repos.projects.create("p")
    return store, repos, project


def _start_session(repos: Repos, project_id: int, *, started_at: float) -> int:
    sess = repos.sessions.start(
        project_id, source_type="live", name=f"sess-{int(started_at)}",
    )
    # Override the started_at column so we can deterministically pick
    # which session is "most recent" in tests.
    repos.store.conn.execute(
        "UPDATE sessions SET started_at = ? WHERE id = ?",
        (started_at, sess.id),
    )
    return sess.id


def _seed_obs(repos: Repos, sess_id: int, suffix: str, *,
              ts: float) -> int:
    """Seed one device with one observation in the given session at the
    given firmware-clock ts."""
    dev = repos.devices.upsert(
        f"rpa:00:11:22:33:44:{suffix}", "rpa",
    )
    repos.devices.merge_identity(dev.id, device_class="apple_device")
    repos.observations.record_packet(
        sess_id, dev.id,
        ts=ts, is_adv=True, rssi=-60, channel=37,
        phy="1M", pdu_type="ADV_IND",
    )
    repos.addresses.upsert(
        f"00:11:22:33:44:{suffix}", "rpa", dev.id, now=ts,
    )
    return dev.id


class SessionScopedStalenessTests(unittest.TestCase):

    def test_session_scoped_cutoff_hides_older_session_high_baseline(self):
        # Reproduces the user's scenario: a prior session ran on a
        # firmware clock baseline far above the current session's.
        # Project-wide MAX would pick the prior session's ts and
        # mis-flag currently-active devices as stale.
        store, repos, project = _make_db()
        try:
            # OLD session: started long ago, firmware ts very high.
            old_sess = _start_session(repos, project.id, started_at=1000.0)
            stale_in_old = _seed_obs(repos, old_sess, "01", ts=9_999_999_000.0)

            # CURRENT session: started later (newer started_at), but
            # firmware clock starts low because dongle was replugged.
            new_sess = _start_session(repos, project.id, started_at=2000.0)
            fresh_in_new = _seed_obs(repos, new_sess, "02", ts=500.0)
            also_fresh = _seed_obs(repos, new_sess, "03", ts=499.0)

            # Cutoff anchored to the new session's MAX (500), 5 sec
            # window → cutoff 495. Both new-session devices clear.
            loaded = {d.device_id for d in load_canvas_devices(
                store, project.id,
                stale_cutoff=500.0 - 5.0,
                stale_session_id=new_sess,
            )}
            # The currently-active devices are visible — exactly what
            # the user wanted but couldn't get with the old MAX.
            self.assertIn(fresh_in_new, loaded)
            self.assertIn(also_fresh, loaded)
            # The leftover from the prior session does NOT show up,
            # because it has no observation in the active session
            # and the per-session MAX is NULL for it.
            self.assertNotIn(stale_in_old, loaded)
        finally:
            store.close()

    def test_device_seen_in_old_session_only_excluded_under_session_scope(self):
        # A device that exists in the project but has zero
        # observations in the most-recent session must not pass the
        # per-session staleness check, regardless of how high its
        # ts is in older sessions.
        store, repos, project = _make_db()
        try:
            old_sess = _start_session(repos, project.id, started_at=1000.0)
            old_only = _seed_obs(repos, old_sess, "01", ts=9_999_999_999.0)

            new_sess = _start_session(repos, project.id, started_at=2000.0)
            in_new = _seed_obs(repos, new_sess, "02", ts=100.0)

            loaded = {d.device_id for d in load_canvas_devices(
                store, project.id,
                stale_cutoff=100.0 - 60.0,
                stale_session_id=new_sess,
            )}
            self.assertIn(in_new, loaded)
            self.assertNotIn(old_only, loaded)
        finally:
            store.close()

    def test_device_seen_in_both_sessions_uses_new_session_ts(self):
        # The headline fix. Same device row exists in both sessions:
        # high ts in old, low ts in new. With session-scoped cutoff,
        # we compare against the new session's ts only and the
        # device passes when fresh in the new session.
        store, repos, project = _make_db()
        try:
            old_sess = _start_session(repos, project.id, started_at=1000.0)
            dev_id = _seed_obs(repos, old_sess, "01", ts=9_999_999_000.0)

            new_sess = _start_session(repos, project.id, started_at=2000.0)
            # Same stable_key ⇒ same device row, but a fresh obs row
            # for the new session.
            repos.observations.record_packet(
                new_sess, dev_id,
                ts=500.0, is_adv=True, rssi=-60, channel=37,
                phy="1M", pdu_type="ADV_IND",
            )

            loaded = {d.device_id for d in load_canvas_devices(
                store, project.id,
                stale_cutoff=500.0 - 5.0,
                stale_session_id=new_sess,
            )}
            self.assertIn(dev_id, loaded)
        finally:
            store.close()

    def test_no_session_scope_falls_back_to_project_wide_max(self):
        # ``stale_session_id=None`` preserves the previous-fix
        # behavior — useful when the caller genuinely wants
        # project-wide scoping (no live session, browse mode).
        store, repos, project = _make_db()
        try:
            sess = _start_session(repos, project.id, started_at=1000.0)
            old = _seed_obs(repos, sess, "01", ts=100.0)
            fresh = _seed_obs(repos, sess, "02", ts=200.0)

            loaded = {d.device_id for d in load_canvas_devices(
                store, project.id,
                stale_cutoff=200.0 - 5.0,
                stale_session_id=None,
            )}
            self.assertIn(fresh, loaded)
            self.assertNotIn(old, loaded)
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
