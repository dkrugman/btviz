"""SnifferPanel STALL badge: session-gated rendering.

Verifies that ``last_stall_at`` from a previous btviz process no
longer triggers a stall badge on a fresh launch. The badge only
appears once a capture session in *this* process has started, and
only for events whose ``last_stall_at`` is at-or-after the session
anchor. Survives Stop so the user can still see "stalls happened
in your last session" until they click Start again.
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
from PySide6.QtWidgets import QApplication  # noqa: E402

from btviz.db.store import Store  # noqa: E402
from btviz.ui.sniffer_panel import SnifferPanel  # noqa: E402


_app: QApplication | None = None


def _ensure_app() -> QApplication:
    global _app
    if _app is None:
        _app = QApplication.instance() or QApplication([])
    return _app


class StallBadgeSessionGateTests(unittest.TestCase):
    """Test the gating *predicate* directly.

    The actual paint code lives inside ``_paint_sniffer_row`` which
    needs a QPainter; instead of standing one up, we exercise the
    same condition the paint code uses: the badge renders iff
    ``self._badge_session_anchor_ts is not None`` AND
    ``last_stall is not None`` AND ``last_stall >= anchor``. The
    test reproduces that logic against the panel's actual state
    after the lifecycle calls it cares about.
    """

    def setUp(self):
        _ensure_app()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = Store(Path(self._tmp.name) / "test.db")
        self.addCleanup(self.store.close)
        self.panel = SnifferPanel(store=self.store)
        self.addCleanup(self.panel.deleteLater)

    @staticmethod
    def _badge_visible(panel, last_stall_at: float | None) -> bool:
        """Mirror the rendering predicate in ``_paint_sniffer_row``.

        Lives in the test rather than as a panel method so a
        future code-only refactor (e.g., extracting a helper) is
        forced to update this test, keeping the predicate's
        behavior pinned.
        """
        anchor = panel._badge_session_anchor_ts
        if anchor is None:
            return False
        if last_stall_at is None:
            return False
        return last_stall_at >= anchor

    def test_anchor_is_none_at_construction(self):
        # btviz startup state: no session has begun in this process,
        # so any historical stall_count from the DB is stale.
        self.assertIsNone(self.panel._badge_session_anchor_ts)
        self.assertFalse(self._badge_visible(self.panel, last_stall_at=999.0))

    def test_anchor_set_on_start_session(self):
        self.panel.start_session_timer(start_ts=1_000.0)
        self.assertEqual(self.panel._badge_session_anchor_ts, 1_000.0)

    def test_old_stall_hidden_after_session_starts(self):
        # Stall happened at 500.0 (yesterday's btviz process).
        # Today's session starts at 1_000.0. Old badge stays hidden.
        self.panel.start_session_timer(start_ts=1_000.0)
        self.assertFalse(self._badge_visible(self.panel, last_stall_at=500.0))

    def test_current_session_stall_visible(self):
        self.panel.start_session_timer(start_ts=1_000.0)
        # Stall at 1_050.0 — inside this session.
        self.assertTrue(self._badge_visible(self.panel, last_stall_at=1_050.0))

    def test_stall_at_session_start_visible(self):
        # Edge case: a stall that fires exactly at session-start
        # second (cheap clock resolution). ``>=`` covers it.
        self.panel.start_session_timer(start_ts=1_000.0)
        self.assertTrue(self._badge_visible(self.panel, last_stall_at=1_000.0))

    def test_anchor_survives_stop(self):
        # User sees badges from session A; clicks Stop. The badge
        # should remain visible (not vanish) so the user can read
        # what happened during A. Anchor stays set; the timer
        # label gets frozen separately.
        self.panel.start_session_timer(start_ts=1_000.0)
        self.panel.stop_session_timer()
        self.assertEqual(self.panel._badge_session_anchor_ts, 1_000.0)
        self.assertTrue(self._badge_visible(self.panel, last_stall_at=1_050.0))

    def test_anchor_advances_on_next_start(self):
        # Session A had stalls. Session B starts. Anchor advances
        # to B's start time, hiding A's badges (so the user starts
        # fresh) until something stalls in B.
        self.panel.start_session_timer(start_ts=1_000.0)
        self.panel.stop_session_timer()
        self.panel.start_session_timer(start_ts=2_000.0)
        self.assertEqual(self.panel._badge_session_anchor_ts, 2_000.0)
        # A's stall (at 1_050) — now stale.
        self.assertFalse(self._badge_visible(self.panel, last_stall_at=1_050.0))
        # B's stall (at 2_050) — visible.
        self.assertTrue(self._badge_visible(self.panel, last_stall_at=2_050.0))


if __name__ == "__main__":
    unittest.main()
