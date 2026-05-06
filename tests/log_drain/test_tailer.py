"""drain_file — file-tailer integration.

Drives the tailer end-to-end against a fixture log file in a
tempdir. Uses ``stop_when_eof=True`` so the loop exits cleanly
once the synthetic input is consumed (the production caller runs
forever until SIGINT).
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from btviz.log_drain import drain_file  # noqa: E402


def _write_log(path: Path, lines: list[str]) -> None:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


class _AdvancingClock:
    """Test clock that ticks forward on each call.

    Lets us walk past the summary interval inside ``stop_when_eof``
    mode without sleeping. Every ``__call__`` advances by ``step``.
    """
    def __init__(self, start: float = 1_000_000.0, step: float = 0.5):
        self.t = start
        self.step = step

    def __call__(self) -> float:
        self.t += self.step
        return self.t


class TailerEndToEndTests(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.in_path = Path(self._tmp.name) / "capture.log"
        self.out_path = Path(self._tmp.name) / "drained.log"

    def test_first_occurrences_emit_verbatim(self):
        # Three distinct templates → three verbatim lines in output.
        _write_log(self.in_path, [
            "2026-05-06 14:23:01.000  INFO     btviz startup",
            "2026-05-06 14:23:02.000  INFO     capture started — 3/7 dongles capturing (4 idle 3 scan)",
            "2026-05-06 14:23:12.000  DEBUG    watchdog tick — silent=0 stuck=0 ids_silent=- ids_stuck=-",
        ])
        drain_file(
            self.in_path, self.out_path,
            from_start=True, stop_when_eof=True,
            summary_interval_s=10_000,  # never tick during this test
        )
        body = self.out_path.read_text(encoding="utf-8")
        self.assertIn("btviz startup", body)
        self.assertIn("capture started", body)
        self.assertIn("watchdog tick", body)
        # No summary lines yet — single occurrences each.
        self.assertNotIn("SUMMARY", body)

    def test_repeats_collapse_to_summary(self):
        # 1 verbatim watchdog-tick + 4 repeats → one SUMMARY at EOF.
        lines = [
            f"2026-05-06 14:23:{10 + i*10:02d}.000  DEBUG    "
            f"watchdog tick — silent={i} stuck=0 ids_silent=- ids_stuck=-"
            for i in range(5)
        ]
        _write_log(self.in_path, lines)
        # Use a short summary interval AND an advancing clock so
        # the tailer's "no new input → tick" branch fires after EOF.
        drain_file(
            self.in_path, self.out_path,
            from_start=True, stop_when_eof=True,
            summary_interval_s=0.1,
            clock=_AdvancingClock(),
        )
        body = self.out_path.read_text(encoding="utf-8")
        # Verbatim first occurrence still appears.
        self.assertIn("watchdog tick", body)
        # Plus exactly one SUMMARY counting all 5 occurrences in
        # the window (the verbatim is included — count is "how
        # many times did this happen", not "how many were
        # collapsed").
        self.assertIn("SUMMARY", body)
        self.assertIn("[×5]", body)
        self.assertIn("every 10.0", body)

    def test_stall_lines_always_verbatim_no_summary(self):
        # STALL is the canonical "always-verbatim" pattern.
        lines = [
            f"2026-05-06 14:23:{i*10:02d}.000  WARNING  "
            f"STALL detected sniffer=abc role=scan silent_for=70.{i}s attempt={i+1}"
            for i in range(3)
        ]
        _write_log(self.in_path, lines)
        drain_file(
            self.in_path, self.out_path,
            from_start=True, stop_when_eof=True,
            summary_interval_s=0.1,
            clock=_AdvancingClock(),
        )
        body = self.out_path.read_text(encoding="utf-8")
        # All three STALL lines appear verbatim.
        self.assertEqual(body.count("STALL detected"), 3)
        # No SUMMARY for STALL (always-verbatim bypasses clustering).
        self.assertNotIn("SUMMARY", body)

    def test_append_mode_preserves_prior_drained_content(self):
        # First run produces some output. Second run with the same
        # output path appends — doesn't truncate. Critical for
        # ``tail -f`` survivability across drainer restarts.
        _write_log(self.in_path, [
            "2026-05-06 14:23:01.000  INFO     btviz startup",
        ])
        drain_file(
            self.in_path, self.out_path,
            from_start=True, stop_when_eof=True,
            summary_interval_s=10_000,
        )
        first_body = self.out_path.read_text(encoding="utf-8")
        self.assertIn("btviz startup", first_body)
        # Rewrite input with a different line; rerun.
        _write_log(self.in_path, [
            "2026-05-06 14:24:01.000  INFO     btviz exit — uptime=1m00s",
        ])
        drain_file(
            self.in_path, self.out_path,
            from_start=True, stop_when_eof=True,
            summary_interval_s=10_000,
        )
        second_body = self.out_path.read_text(encoding="utf-8")
        self.assertIn("btviz startup", second_body)  # preserved
        self.assertIn("btviz exit", second_body)
        self.assertGreater(len(second_body), len(first_body))


if __name__ == "__main__":
    unittest.main()
