"""Start Capture with no dongles → dialog instead of silent fail.

We can't drive a real ``QMessageBox.exec()`` deterministically in
tests (it would block on user input), so the test exercises the
*dialog factory*: confirms the method exists, runs without raising,
and that the link target points at the canonical docs path. The
behavior is also pinned at the schema level via the existence of
``docs/HARDWARE.md`` so the link can't go dead.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication  # noqa: E402

from btviz.db.repos import Repos  # noqa: E402
from btviz.db.store import Store  # noqa: E402
from btviz.ui.canvas import CanvasWindow  # noqa: E402


_app: QApplication | None = None


def _ensure_app() -> QApplication:
    global _app
    if _app is None:
        _app = QApplication.instance() or QApplication([])
    return _app


class HardwareDocExistsTests(unittest.TestCase):
    """The dialog's link points at docs/HARDWARE.md — make sure it exists."""

    def test_hardware_md_present(self):
        path = REPO_ROOT / "docs" / "HARDWARE.md"
        self.assertTrue(
            path.exists(),
            "docs/HARDWARE.md is the link target in the no-dongles dialog "
            "and the README. Don't delete without updating both.",
        )

    def test_hardware_md_has_compatible_section(self):
        # The dialog and README both promise that this file lists
        # compatible devices. Pin the section header so a future
        # refactor that removes it gets caught.
        body = (REPO_ROOT / "docs" / "HARDWARE.md").read_text(encoding="utf-8")
        self.assertIn("Compatible devices", body)
        self.assertIn("nRF Sniffer", body)


class NoDonglesDialogTests(unittest.TestCase):

    def setUp(self):
        _ensure_app()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = Store(Path(self._tmp.name) / "test.db")
        self.addCleanup(self.store.close)
        repos = Repos(self.store)
        project = repos.projects.create("test")
        self.canvas = CanvasWindow(self.store, project.id)
        self.addCleanup(self.canvas.deleteLater)

    def test_method_exists(self):
        # The method is the contract _start_live calls when discovery
        # comes back empty. Pin its presence so a future refactor
        # doesn't silently drop the dialog path.
        self.assertTrue(callable(getattr(self.canvas, "_show_no_dongles_dialog", None)))

    def test_dialog_runs_without_raising(self):
        # Patch QMessageBox.exec so the test doesn't block on the
        # modal dialog. The point is to confirm the construction +
        # invocation path doesn't raise — i.e., the rich-text body,
        # the parent assignment, and the icon all wire up.
        with patch("PySide6.QtWidgets.QMessageBox.exec", return_value=0):
            try:
                self.canvas._show_no_dongles_dialog()
            except Exception as e:  # noqa: BLE001
                self.fail(f"_show_no_dongles_dialog raised: {e!r}")

    def test_dialog_runs_with_detail(self):
        # Detail path is taken when discovery raises an exception
        # (e.g., extcap probe failure). Should still complete.
        with patch("PySide6.QtWidgets.QMessageBox.exec", return_value=0):
            self.canvas._show_no_dongles_dialog(
                title="Discovery failed",
                detail="extcap probe raised TimeoutError",
            )

    def test_construction_failure_falls_back_to_stderr(self):
        # If QMessageBox itself fails to construct (the macOS Tahoe +
        # PySide6 6.11 segfault pattern), the method must not raise —
        # instead it logs to stderr so Start Capture can continue
        # cleanly. Mock the QMessageBox class to raise on init.
        import io as _io
        captured = _io.StringIO()
        with patch("sys.stderr", captured), \
                patch(
                    "PySide6.QtWidgets.QMessageBox",
                    side_effect=RuntimeError("simulated Qt crash"),
                ):
            try:
                self.canvas._show_no_dongles_dialog()
            except Exception as e:  # noqa: BLE001
                self.fail(f"dialog must swallow construction errors: {e!r}")
        self.assertIn(
            "docs/HARDWARE.md", captured.getvalue(),
            "stderr fallback should still surface the help link",
        )


if __name__ == "__main__":
    unittest.main()
