"""SnifferPanel session timer.

Replaces the panel's old Refresh button. Drives a 1 Hz QLabel
showing elapsed capture time; freezes on stop; resets on next start.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
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


class SessionTimerTests(unittest.TestCase):

    def setUp(self):
        _ensure_app()
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self._tmp.name) / "test.db")
        self.panel = SnifferPanel(store=self.store)

    def tearDown(self):
        self.panel.deleteLater()
        self.store.close()
        self._tmp.cleanup()

    # ---- formatting ----

    def test_idle_shows_zero(self):
        self.panel._update_session_label()
        self.assertEqual(self.panel._session_label.text(), "00:00:00")

    def test_seconds_only_renders_hh_mm_ss(self):
        self.panel.start_session_timer(time.time() - 5)
        self.panel._update_session_label()
        self.assertEqual(self.panel._session_label.text(), "00:00:05")

    def test_under_one_hour_renders_correctly(self):
        self.panel.start_session_timer(time.time() - (60 + 30))
        self.panel._update_session_label()
        self.assertEqual(self.panel._session_label.text(), "00:01:30")

    def test_over_one_hour_renders_correctly(self):
        self.panel.start_session_timer(time.time() - (3600 + 120 + 5))
        self.panel._update_session_label()
        self.assertEqual(self.panel._session_label.text(), "01:02:05")

    def test_day_crossing_renders_with_day_count(self):
        elapsed = 2 * 86400 + 3 * 3600 + 4 * 60 + 5
        self.panel.start_session_timer(time.time() - elapsed)
        self.panel._update_session_label()
        self.assertEqual(self.panel._session_label.text(), "2d 03:04:05")

    # ---- state transitions ----

    def test_stop_freezes_label_text(self):
        self.panel.start_session_timer(time.time() - 7)
        self.panel.stop_session_timer()
        frozen = self.panel._session_label.text()
        # Subsequent ticks while stopped don't bump the displayed
        # value (timer wasn't running anyway, but defensively check).
        self.panel._update_session_label()
        self.assertEqual(self.panel._session_label.text(), frozen)

    def test_stop_without_start_is_noop(self):
        # Idempotent guard — a stop before any start should leave
        # the timer at "00:00:00", not crash.
        self.panel.stop_session_timer()
        self.assertEqual(self.panel._session_label.text(), "00:00:00")

    def test_reset_clears_after_stop(self):
        self.panel.start_session_timer(time.time() - 12)
        self.panel.stop_session_timer()
        self.panel.reset_session_timer()
        self.assertEqual(self.panel._session_label.text(), "00:00:00")

    def test_restart_overwrites_frozen_value(self):
        # Run, stop (freeze), then start again — second run starts
        # fresh from 00:00:00, doesn't continue from the frozen value.
        self.panel.start_session_timer(time.time() - 30)
        self.panel.stop_session_timer()
        self.assertEqual(self.panel._session_label.text(), "00:00:30")
        self.panel.start_session_timer(time.time())
        self.panel._update_session_label()
        # Just-started; allow either 00:00:00 or 00:00:01 if the
        # test fixture is slow.
        self.assertIn(
            self.panel._session_label.text(),
            ("00:00:00", "00:00:01"),
        )

    # ---- QTimer wiring ----

    def test_timer_runs_after_start(self):
        self.panel.start_session_timer(time.time())
        self.assertTrue(self.panel._session_tick.isActive())

    def test_timer_stops_after_stop(self):
        self.panel.start_session_timer(time.time())
        self.panel.stop_session_timer()
        self.assertFalse(self.panel._session_tick.isActive())


if __name__ == "__main__":
    unittest.main()
