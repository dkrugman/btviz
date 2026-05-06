"""``cluster.verbose_log`` preference field.

The toolbar's "Verbose cluster log" toggle moved into preferences.
The pref is read at app startup (in __main__.main) and bumps the
cluster logger to DEBUG when enabled. Tested here at the schema
layer (default + write/read round-trip) and at the apply layer
(level lift on the actual logger).
"""

from __future__ import annotations

import logging
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from btviz.cluster import configure_cluster_log, get_cluster_logger  # noqa: E402
from btviz.preferences import (  # noqa: E402
    Preferences, SCHEMA, by_key, reset_singleton_for_tests,
)


class SchemaTests(unittest.TestCase):

    def test_field_is_registered(self):
        keys = {f.key for f in SCHEMA}
        self.assertIn("cluster.verbose_log", keys)

    def test_default_is_false(self):
        field = by_key("cluster.verbose_log")
        self.assertFalse(field.default)
        # Type is bool so the picker dialog renders a checkbox.
        self.assertEqual(field.type, bool)
        # Logger level is set once at startup, so the user has to
        # restart for a change to take effect.
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

    def _apply_pref(self, prefs: Preferences) -> None:
        """Mirror the snippet in ``__main__.main`` so the test
        exercises the same code path's effect without spinning up
        the full CLI."""
        if bool(prefs.get("cluster.verbose_log")):
            self.logger.setLevel(logging.DEBUG)
            for h in self.logger.handlers:
                h.setLevel(logging.DEBUG)

    def test_pref_off_keeps_logger_at_info_default(self):
        d = tempfile.mkdtemp()
        prefs = Preferences.load(Path(d))  # defaults: verbose_log=False
        self._apply_pref(prefs)
        self.assertEqual(get_cluster_logger().level, logging.INFO)

    def test_pref_on_lifts_logger_to_debug(self):
        d = tempfile.mkdtemp()
        prefs = Preferences.load(Path(d))
        prefs.set("cluster.verbose_log", True)
        prefs.save()
        # Reload to ensure persistence works end-to-end.
        prefs = Preferences.load(Path(d))
        self._apply_pref(prefs)
        self.assertEqual(get_cluster_logger().level, logging.DEBUG)
        # Handlers also dropped to DEBUG so messages actually surface.
        for h in get_cluster_logger().handlers:
            self.assertEqual(h.level, logging.DEBUG)


if __name__ == "__main__":
    unittest.main()
