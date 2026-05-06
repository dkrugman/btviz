"""Three-tier capture-log wiring.

Verifies:
  * VERBOSE level is registered between INFO and DEBUG.
  * ``log.verbose(...)`` shorthand is callable on any Logger.
  * ``apply_capture_log_prefs`` flips the logger + every attached
    handler to the right numeric level for each (verbose, debug)
    combination.
  * Default capture.log lines (INFO, VERBOSE, DEBUG) reach a file
    handler iff the level threshold permits.
"""

from __future__ import annotations

import logging
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from btviz import capture_log  # noqa: E402


class VerboseLevelTests(unittest.TestCase):

    def test_verbose_level_constant(self):
        self.assertEqual(capture_log.VERBOSE, 15)
        self.assertGreater(capture_log.VERBOSE, logging.DEBUG)
        self.assertLess(capture_log.VERBOSE, logging.INFO)

    def test_verbose_level_named(self):
        # ``addLevelName`` makes the level name show up in formatted
        # output. Round-trip name ⇄ level number.
        self.assertEqual(
            logging.getLevelName(capture_log.VERBOSE), "VERBOSE",
        )

    def test_logger_has_verbose_shorthand(self):
        logger = logging.getLogger("test.verbose.shorthand")
        self.assertTrue(callable(getattr(logger, "verbose", None)))


class ApplyPrefsTests(unittest.TestCase):

    def setUp(self):
        # Run on the real ``btviz.capture`` logger so the integration
        # is actually exercised. Snapshot + restore level / handlers
        # so test order doesn't bleed into other tests.
        self._logger = logging.getLogger(capture_log.LOG_NAME)
        self._prev_level = self._logger.level
        self._prev_handlers = list(self._logger.handlers)
        self.addCleanup(self._restore)

    def _restore(self):
        self._logger.setLevel(self._prev_level)
        # Remove any handlers added during the test.
        for h in list(self._logger.handlers):
            if h not in self._prev_handlers:
                self._logger.removeHandler(h)

    def _attach_temp_handler(self) -> logging.Handler:
        h = logging.NullHandler()
        h.setLevel(logging.WARNING)  # deliberately stale
        self._logger.addHandler(h)
        return h

    def test_none_falls_back_to_info(self):
        h = self._attach_temp_handler()
        capture_log.apply_capture_log_prefs(None)
        self.assertEqual(self._logger.level, logging.INFO)
        self.assertEqual(h.level, logging.INFO)

    def test_info_string(self):
        h = self._attach_temp_handler()
        capture_log.apply_capture_log_prefs("info")
        self.assertEqual(self._logger.level, logging.INFO)
        self.assertEqual(h.level, logging.INFO)

    def test_verbose_string_promotes_to_15(self):
        h = self._attach_temp_handler()
        capture_log.apply_capture_log_prefs("verbose")
        self.assertEqual(self._logger.level, capture_log.VERBOSE)
        self.assertEqual(h.level, capture_log.VERBOSE)

    def test_debug_string_promotes_to_debug(self):
        h = self._attach_temp_handler()
        capture_log.apply_capture_log_prefs("debug")
        self.assertEqual(self._logger.level, logging.DEBUG)
        self.assertEqual(h.level, logging.DEBUG)

    def test_warning_string_promotes_to_warning(self):
        h = self._attach_temp_handler()
        capture_log.apply_capture_log_prefs("warning")
        self.assertEqual(self._logger.level, logging.WARNING)
        self.assertEqual(h.level, logging.WARNING)

    def test_error_string_promotes_to_error(self):
        h = self._attach_temp_handler()
        capture_log.apply_capture_log_prefs("error")
        self.assertEqual(self._logger.level, logging.ERROR)
        self.assertEqual(h.level, logging.ERROR)

    def test_uppercase_string_normalized(self):
        # Stale TOML or hand-edited prefs may carry "INFO" instead
        # of "info"; resolver lowercases before lookup.
        h = self._attach_temp_handler()
        capture_log.apply_capture_log_prefs("INFO")
        self.assertEqual(self._logger.level, logging.INFO)
        self.assertEqual(h.level, logging.INFO)

    def test_unknown_string_falls_back_to_info(self):
        # Defensive: an old-pref-version string (e.g., "trace") or
        # typo shouldn't crash; INFO is the safe default.
        h = self._attach_temp_handler()
        capture_log.apply_capture_log_prefs("trace")
        self.assertEqual(self._logger.level, logging.INFO)
        self.assertEqual(h.level, logging.INFO)

    def test_int_passed_through(self):
        # Caller can pass a numeric level directly (e.g., a future
        # programmatic API). resolve_level treats ints as already-
        # resolved.
        h = self._attach_temp_handler()
        capture_log.apply_capture_log_prefs(logging.WARNING)
        self.assertEqual(self._logger.level, logging.WARNING)
        self.assertEqual(h.level, logging.WARNING)

    def test_level_names_table_complete(self):
        # The dropdown values in the prefs schema must match the
        # LEVEL_NAMES keys. Pin the set so a future schema rename
        # without a code update gets caught.
        self.assertEqual(
            set(capture_log.LEVEL_NAMES.keys()),
            {"error", "warning", "info", "verbose", "debug"},
        )


class LogReachesFileTests(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._path = Path(self._tmp.name) / "capture.log"
        # Use a fresh logger name so the configure path actually
        # attaches a handler (the real ``btviz.capture`` logger may
        # already be configured by other tests).
        self._logger_name = "btviz.test.capture.tier"
        self._logger = logging.getLogger(self._logger_name)
        self._prev_level = self._logger.level
        self._prev_handlers = list(self._logger.handlers)
        self.addCleanup(self._restore)
        # Attach a file handler manually so we control the path.
        self._handler = logging.FileHandler(self._path, encoding="utf-8")
        self._handler.setFormatter(logging.Formatter(
            "%(levelname)-7s %(message)s"
        ))
        self._logger.addHandler(self._handler)

    def _restore(self):
        self._logger.setLevel(self._prev_level)
        try:
            self._handler.close()
        except Exception:  # noqa: BLE001
            pass
        for h in list(self._logger.handlers):
            if h not in self._prev_handlers:
                self._logger.removeHandler(h)

    def _set_level(self, level: int) -> None:
        self._logger.setLevel(level)
        self._handler.setLevel(level)

    def _read(self) -> str:
        self._handler.flush()
        return self._path.read_text(encoding="utf-8")

    def test_default_drops_verbose_and_debug(self):
        self._set_level(logging.INFO)
        self._logger.info("info-line")
        self._logger.verbose("verbose-line")  # type: ignore[attr-defined]
        self._logger.debug("debug-line")
        body = self._read()
        self.assertIn("info-line", body)
        self.assertNotIn("verbose-line", body)
        self.assertNotIn("debug-line", body)

    def test_verbose_passes_info_and_verbose_drops_debug(self):
        self._set_level(capture_log.VERBOSE)
        self._logger.info("info-line")
        self._logger.verbose("verbose-line")  # type: ignore[attr-defined]
        self._logger.debug("debug-line")
        body = self._read()
        self.assertIn("info-line", body)
        self.assertIn("verbose-line", body)
        self.assertNotIn("debug-line", body)

    def test_debug_passes_all_three(self):
        self._set_level(logging.DEBUG)
        self._logger.info("info-line")
        self._logger.verbose("verbose-line")  # type: ignore[attr-defined]
        self._logger.debug("debug-line")
        body = self._read()
        self.assertIn("info-line", body)
        self.assertIn("verbose-line", body)
        self.assertIn("debug-line", body)


if __name__ == "__main__":
    unittest.main()
