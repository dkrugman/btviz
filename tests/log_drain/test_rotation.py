"""Drained-log rotation — _RotatingTextSink.

Direct tests of the sink class, plus an end-to-end via
``drain_file`` to confirm the wiring. Mirrors the policy of
``logging.handlers.RotatingFileHandler``: rename current →
``foo.log.1``, shift others down, drop the oldest, reopen.

Default budget (50 MB × 5) is larger than capture.log's
because drained bytes are denser per byte; tests use a
much smaller cap to exercise rotation cheaply.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from btviz.log_drain import drain_file  # noqa: E402
from btviz.log_drain.tailer import _RotatingTextSink  # noqa: E402


class _AdvancingClock:
    """Test clock — see test_tailer.py for the same pattern."""
    def __init__(self, start: float = 1_000_000.0, step: float = 0.5):
        self.t = start
        self.step = step

    def __call__(self) -> float:
        self.t += self.step
        return self.t


class RotatingSinkTests(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "drained.log"

    def test_under_cap_does_not_rotate(self):
        sink = _RotatingTextSink(self.path, max_bytes=1024, backup_count=3)
        try:
            sink.write("a" * 100 + "\n")
            sink.write("b" * 100 + "\n")
            sink.flush()
        finally:
            sink.close()
        self.assertTrue(self.path.exists())
        self.assertFalse((self.path.parent / "drained.log.1").exists())

    def test_exceeds_cap_rotates(self):
        sink = _RotatingTextSink(self.path, max_bytes=200, backup_count=3)
        try:
            # First write: 100 bytes — under cap, no rotation.
            sink.write("a" * 99 + "\n")  # 100 bytes
            sink.flush()
            # Second write: would push to 200, equal — no rotate.
            sink.write("b" * 99 + "\n")
            sink.flush()
            # Third write: 200 + 100 > 200 → rotate first.
            sink.write("c" * 99 + "\n")
            sink.flush()
        finally:
            sink.close()
        # After the third write, drained.log holds the 'c' content;
        # drained.log.1 holds the prior 'a'+'b'.
        self.assertTrue(self.path.exists())
        rotated = self.path.parent / "drained.log.1"
        self.assertTrue(rotated.exists())
        self.assertIn("c" * 99, self.path.read_text(encoding="utf-8"))
        prior = rotated.read_text(encoding="utf-8")
        self.assertIn("a" * 99, prior)
        self.assertIn("b" * 99, prior)

    def test_backup_chain_shifts_correctly(self):
        # Force three rotations so we end up with .log + .log.1 +
        # .log.2 + .log.3 — fully populated chain at backup_count=3.
        sink = _RotatingTextSink(self.path, max_bytes=100, backup_count=3)
        try:
            for marker in ("first", "second", "third", "fourth"):
                sink.write(marker + "\n" + "x" * 99 + "\n")
                sink.flush()
        finally:
            sink.close()
        # Newest content (fourth) is in the live file. Older content
        # walks back: .1=third, .2=second, .3=first.
        self.assertIn("fourth", self.path.read_text(encoding="utf-8"))
        self.assertIn(
            "third",
            (self.path.parent / "drained.log.1").read_text(encoding="utf-8"),
        )
        self.assertIn(
            "second",
            (self.path.parent / "drained.log.2").read_text(encoding="utf-8"),
        )
        self.assertIn(
            "first",
            (self.path.parent / "drained.log.3").read_text(encoding="utf-8"),
        )

    def test_oldest_dropped_at_backup_count_cap(self):
        # backup_count=2 → keep at most foo.log.1 + foo.log.2.
        # A rotation that would create .3 instead drops the oldest.
        sink = _RotatingTextSink(self.path, max_bytes=100, backup_count=2)
        try:
            for marker in ("first", "second", "third", "fourth", "fifth"):
                sink.write(marker + "\n" + "x" * 99 + "\n")
                sink.flush()
        finally:
            sink.close()
        # Live file has 'fifth'. .1 has 'fourth'. .2 has 'third'.
        # .3 must NOT exist — the cap is 2 backups.
        self.assertIn("fifth", self.path.read_text(encoding="utf-8"))
        self.assertIn(
            "fourth",
            (self.path.parent / "drained.log.1").read_text(encoding="utf-8"),
        )
        self.assertIn(
            "third",
            (self.path.parent / "drained.log.2").read_text(encoding="utf-8"),
        )
        self.assertFalse((self.path.parent / "drained.log.3").exists())

    def test_drain_file_passes_rotation_through(self):
        # End-to-end: drain a fixture log with a tiny rotation cap
        # so the drainer's verbatim-emit path triggers a rotation.
        # We use the always-verbatim STALL pattern so every input
        # line becomes an output line (no template collapsing) —
        # otherwise Drain3 would cluster them together and we'd
        # get one verbatim + N repeats, never enough bytes to
        # trigger rotation. This also matches real-world usage
        # since STALL lines are exactly the kind of always-verbatim
        # event that fills a long-running drained log.
        in_path = Path(self._tmp.name) / "capture.log"
        out_path = Path(self._tmp.name) / "drained.log"
        in_path.write_text(
            "\n".join(
                f"2026-05-06 14:23:{i:02d}.000  WARNING  "
                f"STALL detected sniffer=abc role=scan "
                f"silent_for=70.{i}s attempt={i+1}"
                for i in range(30)
            ) + "\n",
            encoding="utf-8",
        )
        drain_file(
            in_path, out_path,
            from_start=True, stop_when_eof=True,
            summary_interval_s=10_000,
            max_bytes=1024,
            backup_count=2,
            clock=_AdvancingClock(),
        )
        self.assertTrue(out_path.exists())
        self.assertTrue((out_path.parent / "drained.log.1").exists())


if __name__ == "__main__":
    unittest.main()
