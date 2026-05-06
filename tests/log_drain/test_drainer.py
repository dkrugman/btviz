"""DrainerEngine — log-line clustering + summary emission.

Pure-Python tests against the engine class. No file I/O, no
threads, no real ``btviz.capture`` logger — all input is built
as ``LineRecord`` objects and the output is the engine's return
values.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from btviz.log_drain.drainer import (  # noqa: E402
    DrainerEngine, LineRecord, parse_capture_line,
)


def _rec(body: str, ts: float, level: str = "DEBUG") -> LineRecord:
    """Build a LineRecord without going through the full parser.

    ``raw`` is set to the canonical ``timestamp  LEVEL  body`` form
    so verbatim emits read like a real log line.
    """
    return LineRecord(
        raw=f"2026-05-06 14:23:00.000  {level}    {body}\n",
        ts_seconds=ts,
        level=level,
        body=body,
    )


class ParserTests(unittest.TestCase):

    def test_parses_canonical_line(self):
        rec = parse_capture_line(
            "2026-05-06 14:23:01.123  INFO     btviz startup\n"
        )
        self.assertIsNotNone(rec)
        assert rec is not None
        self.assertEqual(rec.level, "INFO")
        self.assertEqual(rec.body, "btviz startup")
        self.assertGreater(rec.ts_seconds, 0)

    def test_parses_line_without_level(self):
        # Legacy entries (pre-PR-102) had no level token.
        rec = parse_capture_line(
            "2026-05-06 14:23:01.000  STALL detected sniffer=abc\n"
        )
        self.assertIsNotNone(rec)
        assert rec is not None
        self.assertIsNone(rec.level)
        self.assertIn("STALL detected", rec.body)

    def test_blank_line_returns_none(self):
        self.assertIsNone(parse_capture_line(""))
        self.assertIsNone(parse_capture_line("\n"))
        self.assertIsNone(parse_capture_line("   \n"))

    def test_unparseable_line_keeps_body(self):
        # A multi-line traceback continuation has no timestamp;
        # the parser falls back to fallback_now and uses the line
        # as body so the engine can still cluster it.
        rec = parse_capture_line(
            "  File 'foo.py', line 42, in bar\n",
            fallback_now=1000.0,
        )
        self.assertIsNotNone(rec)
        assert rec is not None
        self.assertEqual(rec.ts_seconds, 1000.0)
        self.assertIsNone(rec.level)
        self.assertIn("File", rec.body)


class FirstOccurrenceTests(unittest.TestCase):

    def test_first_match_emits_verbatim(self):
        eng = DrainerEngine()
        rec = _rec(
            "watchdog tick — silent=0 stuck=0 ids_silent=- ids_stuck=-",
            ts=1000.0,
        )
        emit = eng.ingest(rec)
        self.assertEqual(len(emit), 1)
        self.assertIn("watchdog tick", emit[0])

    def test_repeat_emits_nothing_until_summary(self):
        eng = DrainerEngine()
        eng.ingest(_rec("watchdog tick — silent=0 stuck=0", ts=1000.0))
        # Same template, different numeric — Drain3 maps to same cluster.
        emit = eng.ingest(_rec("watchdog tick — silent=2 stuck=0", ts=1010.0))
        self.assertEqual(emit, ())

    def test_distinct_templates_each_emit_verbatim(self):
        eng = DrainerEngine()
        e1 = eng.ingest(_rec("foo bar baz one", ts=1000.0, level="INFO"))
        e2 = eng.ingest(_rec(
            "completely different sentence here", ts=1010.0, level="INFO",
        ))
        self.assertEqual(len(e1), 1)
        self.assertEqual(len(e2), 1)


class SummaryTickTests(unittest.TestCase):

    def test_summary_emitted_for_repeated_cluster(self):
        eng = DrainerEngine()
        # 5 watchdog ticks, 10 s apart. First emits verbatim;
        # the next 4 are repeats waiting for the summary tick.
        for i in range(5):
            eng.ingest(_rec(
                f"watchdog tick — silent={i} stuck=0",
                ts=1000.0 + i * 10.0,
            ))
        summaries = eng.tick_summary()
        self.assertEqual(len(summaries), 1)
        s = summaries[0]
        # Window count = 5 — total occurrences in the window,
        # including the one that already emitted verbatim. The
        # SUMMARY answers "how many times did this happen", not
        # "how many were collapsed."
        self.assertEqual(s.count, 5)
        self.assertAlmostEqual(s.avg_interval_s, 10.0, places=1)
        self.assertLess(s.jitter_s, 0.001)
        rendered = s.render()
        self.assertIn("[×5]", rendered)
        self.assertIn("every 10.00s", rendered)
        self.assertIn("SUMMARY", rendered)

    def test_summary_resets_window_counter(self):
        eng = DrainerEngine()
        for i in range(3):
            eng.ingest(_rec(
                f"watchdog tick — silent={i} stuck=0",
                ts=1000.0 + i * 10.0,
            ))
        s1 = eng.tick_summary()
        self.assertEqual(len(s1), 1)
        # Second tick with no new input: nothing to emit.
        s2 = eng.tick_summary()
        self.assertEqual(s2, ())

    def test_singleton_cluster_skips_summary(self):
        # A cluster that only fired once was already covered by the
        # verbatim emission — no SUMMARY needed.
        eng = DrainerEngine()
        eng.ingest(_rec("rare one-off event happens", ts=1000.0, level="INFO"))
        self.assertEqual(eng.tick_summary(), ())

    def test_jitter_reflects_interval_variance(self):
        # 5 ticks with deliberately uneven gaps.
        eng = DrainerEngine()
        eng.ingest(_rec("watchdog tick — silent=0", ts=1000.0))
        eng.ingest(_rec("watchdog tick — silent=1", ts=1010.0))
        eng.ingest(_rec("watchdog tick — silent=2", ts=1015.0))  # 5 s gap
        eng.ingest(_rec("watchdog tick — silent=3", ts=1030.0))  # 15 s gap
        summaries = eng.tick_summary()
        self.assertEqual(len(summaries), 1)
        s = summaries[0]
        self.assertGreater(s.jitter_s, 1.0)  # mixed-gap shows jitter


class EmissionTimestampTests(unittest.TestCase):
    """SUMMARY's prefix timestamp is the emission time, not last-seen.

    Without this, summaries appear "back in time" relative to the
    verbatim lines emitted between their last sample and the
    summary tick — confusing for ``tail -f`` reading.
    """

    def test_emission_ts_overrides_last_seen_in_render(self):
        eng = DrainerEngine()
        for i in range(3):
            eng.ingest(_rec(
                f"watchdog tick — silent={i}", ts=1000.0 + i * 10.0,
            ))
        # Sample times: 1000, 1010, 1020. Pretend we're emitting
        # the summary at t=2000 (much later) — the rendered line
        # should reflect 2000, not 1020.
        summaries = eng.tick_summary(now=2000.0)
        self.assertEqual(len(summaries), 1)
        s = summaries[0]
        self.assertEqual(s.emission_ts, 2000.0)
        self.assertEqual(s.last_ts, 1020.0)
        from datetime import datetime
        expected_prefix = datetime.fromtimestamp(2000.0).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        self.assertTrue(s.render().startswith(expected_prefix))

    def test_emission_ts_defaults_to_last_seen(self):
        # Backward-compat: if a caller doesn't pass ``now``, the
        # emission_ts falls back to last_seen so existing test
        # patterns keep working.
        eng = DrainerEngine()
        for i in range(3):
            eng.ingest(_rec(
                f"watchdog tick — silent={i}", ts=1000.0 + i * 10.0,
            ))
        summaries = eng.tick_summary()  # no now
        self.assertEqual(summaries[0].emission_ts, 1020.0)


class VerbatimMatchTests(unittest.TestCase):

    def test_stall_lines_always_verbatim(self):
        # The default verbatim regex catches STALL events. Repeats
        # of a STALL line still emit verbatim each time, never
        # rolling into a summary, because each STALL is load-bearing
        # for diagnosis.
        eng = DrainerEngine()
        for i in range(3):
            emit = eng.ingest(_rec(
                f"STALL detected sniffer=abc role=scan silent_for=70.{i}s "
                f"attempt={i+1}",
                ts=1000.0 + i * 60.0, level="WARNING",
            ))
            self.assertEqual(len(emit), 1)

    def test_per_dongle_discovery_lines_always_verbatim(self):
        # Per-dongle discovery rows ("dongle short_id=… serial=…")
        # differ only in numeric ID — Drain3 would otherwise cluster
        # them. They're load-bearing for diagnosis (which dongle is
        # which?) so the default regex keeps them verbatim. Same
        # rationale that already covers STALL.
        eng = DrainerEngine()
        for sid in ("2234201", "223101", "2234301"):
            emit = eng.ingest(_rec(
                f"  dongle short_id={sid} serial=ABCDEF "
                f"port=/dev/cu.usbmodem{sid} display=nRF Sniffer",
                ts=1000.0, level="VERBOSE",
            ))
            self.assertEqual(len(emit), 1)

    def test_per_role_assignment_lines_always_verbatim(self):
        # Per-sniffer role rows are similarly load-bearing.
        eng = DrainerEngine()
        for sid in ("2234201", "223101", "2234301"):
            emit = eng.ingest(_rec(
                f"  role short_id={sid} role=scan running=True",
                ts=1000.0, level="VERBOSE",
            ))
            self.assertEqual(len(emit), 1)

    def test_capture_lifecycle_lines_always_verbatim(self):
        # capture started / stopped / btviz exit also bypass clustering.
        eng = DrainerEngine()
        e1 = eng.ingest(_rec(
            "capture started — 3/7 dongles capturing (4 idle 3 scan)",
            ts=1000.0, level="INFO",
        ))
        e2 = eng.ingest(_rec(
            "capture stopped — duration=10m05s, packets=120000, dropped=0",
            ts=1605.0, level="INFO",
        ))
        e3 = eng.ingest(_rec(
            "btviz exit — uptime=15m12s",
            ts=1612.0, level="INFO",
        ))
        self.assertEqual((len(e1), len(e2), len(e3)), (1, 1, 1))

    def test_custom_verbatim_regex(self):
        # User-supplied regex overrides the default. Here we drop
        # the always-verbatim behaviour entirely so STALL lines
        # cluster like everything else (purely a test config —
        # production keeps the default).
        import re
        eng = DrainerEngine(verbatim_match=re.compile(r"^DOES_NOT_MATCH$"))
        eng.ingest(_rec(
            "STALL detected sniffer=abc role=scan silent_for=70.0s attempt=1",
            ts=1000.0, level="WARNING",
        ))
        emit = eng.ingest(_rec(
            "STALL detected sniffer=abc role=scan silent_for=70.5s attempt=2",
            ts=1060.0, level="WARNING",
        ))
        self.assertEqual(emit, ())  # second one rolls into summary


if __name__ == "__main__":
    unittest.main()
