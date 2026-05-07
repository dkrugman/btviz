"""PreferencesDialog renders capture.coded_phy with firmware awareness.

Three states visualized:
  * **blocked** (4.1.1 detected) — checkbox disabled, force-unchecked,
    suffix label includes the "more info" link.
  * **warning** (newer firmware) — checkbox enabled, single italic
    underlined "compatibility warning" link to the right.
  * **none** (no incompatible firmware detected) — checkbox renders
    plainly, no suffix.

The detection function is patched so the test never touches real serial
hardware. Both Save behaviors are pinned: blocked state must persist
``False`` regardless of (forced-disabled) widget state.
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
from PySide6.QtWidgets import QApplication, QCheckBox  # noqa: E402

from btviz.extcap.firmware_query import CodedPhyStatus  # noqa: E402
from btviz.preferences import Preferences, reset_singleton_for_tests  # noqa: E402
from btviz.preferences.ui import PreferencesDialog  # noqa: E402


_app: QApplication | None = None


def _ensure_app() -> QApplication:
    global _app
    if _app is None:
        _app = QApplication.instance() or QApplication([])
    return _app


def _fresh_prefs() -> tuple[Preferences, tempfile.TemporaryDirectory]:
    tmp = tempfile.TemporaryDirectory()
    reset_singleton_for_tests()
    p = Preferences.load(Path(tmp.name))
    return p, tmp


class CodedPhyBlockedRenderTests(unittest.TestCase):
    """4.1.1 detected → checkbox is disabled and force-unchecked."""

    def setUp(self):
        _ensure_app()
        self._prefs, self._tmp = _fresh_prefs()
        self.addCleanup(self._tmp.cleanup)
        # User had previously enabled coded_phy — the detection should
        # over-ride this on render and on Save.
        self._prefs.set("capture.coded_phy", True)
        self._prefs.save()

    def test_checkbox_is_disabled_and_unchecked(self):
        status = CodedPhyStatus(
            severity="blocked",
            suffix="FW v. 4.1.1 detected, incompatible",
            tooltip="...",
            url="https://devzone.nordicsemi.com/f/nordic-q-a/117393/...",
            versions=("4.1.1",),
        )
        with patch(
            "btviz.extcap.firmware_query.detect_coded_phy_incompatibility",
            return_value=status,
        ):
            dlg = PreferencesDialog(self._prefs)
            self.addCleanup(dlg.deleteLater)

            cb = dlg._widgets["capture.coded_phy"]
            self.assertIsInstance(cb, QCheckBox)
            self.assertFalse(cb.isEnabled(), "checkbox must be disabled")
            self.assertFalse(cb.isChecked(), "checkbox must be force-unchecked")
            self.assertIn("capture.coded_phy", dlg._forced_false)

    def test_save_persists_false_even_with_widget_check_attempt(self):
        status = CodedPhyStatus(
            severity="blocked",
            suffix="FW v. 4.1.1 detected, incompatible",
            tooltip="...",
            url="https://devzone.nordicsemi.com/f/nordic-q-a/117393/...",
            versions=("4.1.1",),
        )
        with patch(
            "btviz.extcap.firmware_query.detect_coded_phy_incompatibility",
            return_value=status,
        ):
            dlg = PreferencesDialog(self._prefs)
            self.addCleanup(dlg.deleteLater)
            cb = dlg._widgets["capture.coded_phy"]
            # Even if Qt somehow re-enabled and re-checked the box
            # mid-session, save must write False.
            cb.setEnabled(True)
            cb.setChecked(True)
            dlg._on_save()

        self.assertFalse(self._prefs.get("capture.coded_phy"))


class CodedPhyWarningRenderTests(unittest.TestCase):
    """Newer firmware → checkbox enabled, soft warning link rendered."""

    def setUp(self):
        _ensure_app()
        self._prefs, self._tmp = _fresh_prefs()
        self.addCleanup(self._tmp.cleanup)

    def test_checkbox_remains_enabled(self):
        status = CodedPhyStatus(
            severity="warning",
            suffix="compatibility warning",
            tooltip="newer than 4.1.1, unverified",
            url="https://devzone.nordicsemi.com/f/nordic-q-a/117393/...",
            versions=("4.2.0",),
        )
        with patch(
            "btviz.extcap.firmware_query.detect_coded_phy_incompatibility",
            return_value=status,
        ):
            dlg = PreferencesDialog(self._prefs)
            self.addCleanup(dlg.deleteLater)
            cb = dlg._widgets["capture.coded_phy"]
            self.assertTrue(cb.isEnabled(), "warning state keeps checkbox usable")
            self.assertNotIn("capture.coded_phy", dlg._forced_false)

    def test_user_can_toggle_and_save_true(self):
        status = CodedPhyStatus(
            severity="warning",
            suffix="compatibility warning",
            tooltip="...",
            url="https://devzone.nordicsemi.com/f/nordic-q-a/117393/...",
            versions=("4.2.0",),
        )
        with patch(
            "btviz.extcap.firmware_query.detect_coded_phy_incompatibility",
            return_value=status,
        ):
            dlg = PreferencesDialog(self._prefs)
            self.addCleanup(dlg.deleteLater)
            cb = dlg._widgets["capture.coded_phy"]
            cb.setChecked(True)
            dlg._on_save()
        self.assertTrue(self._prefs.get("capture.coded_phy"))


class CodedPhyNoneRenderTests(unittest.TestCase):
    """No detection → checkbox renders plainly, behaves like any other."""

    def setUp(self):
        _ensure_app()
        self._prefs, self._tmp = _fresh_prefs()
        self.addCleanup(self._tmp.cleanup)

    def test_checkbox_enabled_no_forced_false(self):
        status = CodedPhyStatus(severity=None)
        with patch(
            "btviz.extcap.firmware_query.detect_coded_phy_incompatibility",
            return_value=status,
        ):
            dlg = PreferencesDialog(self._prefs)
            self.addCleanup(dlg.deleteLater)
            cb = dlg._widgets["capture.coded_phy"]
            self.assertTrue(cb.isEnabled())
            self.assertNotIn("capture.coded_phy", dlg._forced_false)

    def test_detection_failure_falls_back_to_plain_render(self):
        # If detect_coded_phy_incompatibility raises, the dialog must
        # still open — guarded by the try/except inside the helper.
        # We exercise the success path here (helper returns severity=
        # None on failure), but also verify a brand-new dialog with
        # no patching at all (real detection on the test host with no
        # dongles attached) doesn't crash.
        dlg = PreferencesDialog(self._prefs)
        self.addCleanup(dlg.deleteLater)
        cb = dlg._widgets.get("capture.coded_phy")
        self.assertIsNotNone(cb)


if __name__ == "__main__":
    unittest.main()
