"""``capture_log.get_program_started_at`` returns the configure-time
wall-clock so the exit-event logger can compute btviz process uptime.

Pinned in a small dedicated test rather than folded into
``test_capture_log_levels`` because the global state interaction
(module-level ``_PROGRAM_STARTED_AT``) needs careful setup/teardown
that would muddy the level tests.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from btviz import capture_log  # noqa: E402


class ProgramStartedAtTests(unittest.TestCase):

    def setUp(self):
        # Snapshot + reset the module-level so each test starts
        # from a known state. Without this the first test to run
        # would leave a value in place that subsequent tests would
        # see, masking the "first call sets it" assertion.
        self._prev = capture_log._PROGRAM_STARTED_AT  # type: ignore[attr-defined]
        capture_log._PROGRAM_STARTED_AT = None  # type: ignore[attr-defined]
        self.addCleanup(self._restore)

    def _restore(self):
        capture_log._PROGRAM_STARTED_AT = self._prev  # type: ignore[attr-defined]

    def test_none_before_configure(self):
        self.assertIsNone(capture_log.get_program_started_at())

    def test_first_configure_sets_timestamp(self):
        with tempfile.TemporaryDirectory() as d:
            before = _now()
            capture_log.configure_capture_log(
                log_file=Path(d) / "capture.log",
            )
            after = _now()
            t = capture_log.get_program_started_at()
            self.assertIsNotNone(t)
            assert t is not None
            self.assertGreaterEqual(t, before)
            self.assertLessEqual(t, after)

    def test_subsequent_configure_does_not_overwrite(self):
        # Idempotency contract: ``configure_capture_log`` is
        # already idempotent for handlers; uptime measurement
        # depends on the *first* call timestamp persisting across
        # later calls (e.g., during the prefs-apply path).
        with tempfile.TemporaryDirectory() as d:
            capture_log.configure_capture_log(
                log_file=Path(d) / "capture.log",
            )
            first = capture_log.get_program_started_at()
            assert first is not None
            # Sleep just enough for the clock to advance.
            import time as _time
            _time.sleep(0.01)
            capture_log.configure_capture_log(
                log_file=Path(d) / "capture.log",
            )
            second = capture_log.get_program_started_at()
            self.assertEqual(first, second)


def _now() -> float:
    import time as _time
    return _time.time()


if __name__ == "__main__":
    unittest.main()
