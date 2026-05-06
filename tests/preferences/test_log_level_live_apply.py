"""Log-level prefs live-apply on save (no restart required).

The ``capture.log_level`` and ``cluster.log_level`` dropdowns
should take effect immediately when the user clicks Save in the
prefs dialog — both apply functions are idempotent so running
them per-save is cheap and avoids the "I changed the level but
nothing happened" footgun.
"""

from __future__ import annotations

import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication  # noqa: E402

from btviz.capture_log import (  # noqa: E402
    VERBOSE, configure_capture_log, get_capture_logger,
)
from btviz.cluster import (  # noqa: E402
    configure_cluster_log, get_cluster_logger,
)
from btviz.preferences import Preferences, by_key  # noqa: E402
from btviz.preferences.ui import PreferencesDialog  # noqa: E402


_app: QApplication | None = None


def _ensure_app() -> QApplication:
    global _app
    if _app is None:
        _app = QApplication.instance() or QApplication([])
    return _app


class SchemaFlagTests(unittest.TestCase):
    """Pin: log-level fields are NOT requires_restart."""

    def test_capture_log_level_is_live(self):
        # ``requires_restart=False`` (the default) means the prefs
        # dialog applies the change immediately. If a future change
        # accidentally re-flips this to True, the test catches it
        # so the live-apply path doesn't go silently dead.
        self.assertFalse(by_key("capture.log_level").requires_restart)

    def test_cluster_log_level_is_live(self):
        self.assertFalse(by_key("cluster.log_level").requires_restart)


class LiveApplyTests(unittest.TestCase):
    """Saving the prefs dialog re-applies log levels immediately."""

    def setUp(self):
        _ensure_app()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._prefs_dir = Path(self._tmp.name) / "prefs"
        self._prefs_dir.mkdir()
        # Detach any handler from prior tests (configure_capture_log
        # is idempotent — if a handler is already attached, it
        # short-circuits and ignores our log_file path). Resetting
        # state here gives each test a known, isolated handler
        # writing to the tempdir file we expect to read.
        self._cap = get_capture_logger()
        self._cluster = get_cluster_logger()
        self._cap_prev_handlers = list(self._cap.handlers)
        self._cluster_prev_handlers = list(self._cluster.handlers)
        for h in list(self._cap.handlers):
            self._cap.removeHandler(h)
        for h in list(self._cluster.handlers):
            self._cluster.removeHandler(h)
        configure_capture_log(log_file=Path(self._tmp.name) / "capture.log")
        configure_cluster_log(log_file=Path(self._tmp.name) / "cluster.log")
        # Snapshot levels so test order doesn't bleed.
        self._prev_cap = self._cap.level
        self._prev_cluster = self._cluster.level
        self.addCleanup(self._restore)

    def _restore(self):
        self._cap.setLevel(self._prev_cap)
        self._cluster.setLevel(self._prev_cluster)
        # Tear down our isolated handlers and restore whatever
        # was attached before (if anything). Skipping this would
        # leave a closed FileHandler on the global logger, which
        # would error on next emit.
        for h in list(self._cap.handlers):
            try:
                h.close()
            except Exception:  # noqa: BLE001
                pass
            self._cap.removeHandler(h)
        for h in list(self._cluster.handlers):
            try:
                h.close()
            except Exception:  # noqa: BLE001
                pass
            self._cluster.removeHandler(h)
        for h in self._cap_prev_handlers:
            self._cap.addHandler(h)
        for h in self._cluster_prev_handlers:
            self._cluster.addHandler(h)

    def _make_dialog_with_prefs(self, **values: str) -> PreferencesDialog:
        prefs = Preferences.load(self._prefs_dir)
        for k, v in values.items():
            prefs.set(k, v)
        # PreferencesDialog reads from the Preferences instance it's
        # given; we instantiate the dialog directly so we can drive
        # _on_save without a real Qt user click.
        dlg = PreferencesDialog(prefs)
        self.addCleanup(dlg.deleteLater)
        return dlg

    def test_save_applies_capture_level_immediately(self):
        # Start at INFO; flip pref to "debug"; trigger _on_save;
        # logger drops to DEBUG without a restart.
        self._cap.setLevel(logging.INFO)
        dlg = self._make_dialog_with_prefs(**{"capture.log_level": "debug"})
        dlg._on_save()
        self.assertEqual(self._cap.level, logging.DEBUG)
        for h in self._cap.handlers:
            self.assertEqual(h.level, logging.DEBUG)

    def test_save_applies_cluster_level_immediately(self):
        self._cluster.setLevel(logging.INFO)
        dlg = self._make_dialog_with_prefs(**{"cluster.log_level": "warning"})
        dlg._on_save()
        self.assertEqual(self._cluster.level, logging.WARNING)
        for h in self._cluster.handlers:
            self.assertEqual(h.level, logging.WARNING)

    def test_save_applies_both_in_one_pass(self):
        self._cap.setLevel(logging.INFO)
        self._cluster.setLevel(logging.INFO)
        dlg = self._make_dialog_with_prefs(**{
            "capture.log_level": "verbose",
            "cluster.log_level": "error",
        })
        dlg._on_save()
        self.assertEqual(self._cap.level, VERBOSE)
        self.assertEqual(self._cluster.level, logging.ERROR)

    def test_change_message_lands_at_new_level(self):
        # Confirmation message lands AT the new level so it
        # remains visible after the level change. Setting to ERROR
        # produces an ERROR-tier "level: error (was info)" line.
        self._cap.setLevel(logging.INFO)
        capture_log_path = Path(self._tmp.name) / "capture.log"
        dlg = self._make_dialog_with_prefs(**{
            "capture.log_level": "error",
        })
        dlg._on_save()
        # Force the handler to flush so the test reads the on-disk
        # file rather than racing the rotating-file buffer.
        for h in self._cap.handlers:
            h.flush()
        body = capture_log_path.read_text(encoding="utf-8")
        # Survives ERROR filter because we log AT the new level.
        self.assertIn("ERROR", body)
        self.assertIn("capture log level: error (was info)", body)

    def test_no_change_message_when_level_unchanged(self):
        # Saving with the same level is a no-op (the apply
        # function short-circuits the confirmation when old==new).
        # Avoids spamming the log on every save when the user only
        # touched unrelated prefs.
        self._cap.setLevel(logging.INFO)
        capture_log_path = Path(self._tmp.name) / "capture.log"
        # Pre-fill the log with a known sentinel so we can detect
        # the absence of a new "log level" entry.
        before = capture_log_path.read_text(encoding="utf-8") if capture_log_path.exists() else ""
        dlg = self._make_dialog_with_prefs(**{
            "capture.log_level": "info",  # same as current
        })
        dlg._on_save()
        for h in self._cap.handlers:
            h.flush()
        after = capture_log_path.read_text(encoding="utf-8") if capture_log_path.exists() else ""
        delta = after[len(before):]
        self.assertNotIn("capture log level:", delta)

    def test_apply_failure_does_not_break_save(self):
        # If the apply path raises (e.g., capture_log import
        # fails for some reason), the save itself must still
        # succeed — the values are already on disk and the user
        # shouldn't see a dialog error for a logging-only issue.
        # We force a failure by monkeypatching apply_capture_log_prefs
        # to raise, then assert the file actually got written.
        from btviz import capture_log as _cl

        def _boom(*_a, **_kw):
            raise RuntimeError("simulated apply failure")

        prev = _cl.apply_capture_log_prefs
        _cl.apply_capture_log_prefs = _boom
        try:
            dlg = self._make_dialog_with_prefs(**{
                "capture.log_level": "debug",
            })
            # Should not raise; should still write to disk.
            dlg._on_save()
            reloaded = Preferences.load(self._prefs_dir)
            self.assertEqual(reloaded.get("capture.log_level"), "debug")
        finally:
            _cl.apply_capture_log_prefs = prev


if __name__ == "__main__":
    unittest.main()
