"""``cluster.log_level`` preference field.

The toolbar's "Verbose cluster log" toggle moved into preferences,
and the bool was later replaced by a 5-state dropdown
(error/warning/info/verbose/debug) for parity with the capture
log dropdown. The pref is read at app startup (in
``__main__.main``) and ``apply_cluster_log_prefs`` resolves the
string to a numeric log level. Tested here at the schema layer
(field shape + enum membership) and at the apply layer (level
lift on the actual logger).
"""

from __future__ import annotations

import logging
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from btviz.cluster import (  # noqa: E402
    apply_cluster_log_prefs, configure_cluster_log, get_cluster_logger,
)
from btviz.preferences import (  # noqa: E402
    Preferences, SCHEMA, by_key, reset_singleton_for_tests,
)


class SchemaTests(unittest.TestCase):

    def test_field_is_registered(self):
        keys = {f.key for f in SCHEMA}
        self.assertIn("cluster.log_level", keys)

    def test_default_is_info(self):
        field = by_key("cluster.log_level")
        self.assertEqual(field.default, "info")
        self.assertEqual(field.type, str)
        # Logger level is read at startup; UI shows a dropdown
        # (Field.enum is non-None) so the prefs dialog renders
        # a QComboBox via the standard render path.
        self.assertEqual(
            field.enum, ("error", "warning", "info", "verbose", "debug"),
        )
        self.assertTrue(field.requires_restart)


class StartupApplyTests(unittest.TestCase):
    """Mirror the apply-at-startup behaviour from ``__main__.main``."""

    def setUp(self) -> None:
        # Each test sees a fresh logger state so leakage from prior
        # tests / app starts can't make this confusing.
        self.logger = logging.getLogger("btviz.cluster")
        self._prev_level = self.logger.level
        self._prev_handler_levels = [(h, h.level) for h in self.logger.handlers]
        # Configure once with a tempdir log to avoid touching
        # ~/.btviz from a unit test.
        self._tmp = tempfile.TemporaryDirectory()
        configure_cluster_log(log_file=Path(self._tmp.name) / "cluster.log")

    def tearDown(self) -> None:
        # Restore prior state for any sibling tests in the same run.
        self.logger.setLevel(self._prev_level)
        for h, lvl in self._prev_handler_levels:
            h.setLevel(lvl)
        self._tmp.cleanup()

    def test_default_pref_keeps_logger_at_info(self):
        d = tempfile.mkdtemp()
        prefs = Preferences.load(Path(d))  # defaults: log_level="info"
        apply_cluster_log_prefs(prefs.get("cluster.log_level"))
        self.assertEqual(get_cluster_logger().level, logging.INFO)

    def test_debug_pref_lifts_logger_to_debug(self):
        d = tempfile.mkdtemp()
        prefs = Preferences.load(Path(d))
        prefs.set("cluster.log_level", "debug")
        prefs.save()
        # Reload to ensure persistence works end-to-end.
        prefs = Preferences.load(Path(d))
        apply_cluster_log_prefs(prefs.get("cluster.log_level"))
        self.assertEqual(get_cluster_logger().level, logging.DEBUG)
        # Handlers also dropped to DEBUG so messages actually surface.
        for h in get_cluster_logger().handlers:
            self.assertEqual(h.level, logging.DEBUG)

    def test_warning_pref_lifts_logger_to_warning(self):
        # New tier surfaced by the dropdown — quiets info-level
        # decision/merge narration but still shows skipped-class
        # warnings and exception-traceback log lines.
        d = tempfile.mkdtemp()
        prefs = Preferences.load(Path(d))
        prefs.set("cluster.log_level", "warning")
        prefs.save()
        prefs = Preferences.load(Path(d))
        apply_cluster_log_prefs(prefs.get("cluster.log_level"))
        self.assertEqual(get_cluster_logger().level, logging.WARNING)


if __name__ == "__main__":
    unittest.main()
