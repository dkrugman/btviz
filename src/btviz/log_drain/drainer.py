"""DrainerEngine — pure-Python core of the log drainer.

Stateful but I/O-free: callers feed it parsed log lines and tick
the clock; it returns the strings that should be written to the
drained log file. Pulled out from the file-tailer in
:mod:`btviz.log_drain.tailer` so the clustering + summarization
logic is unit-testable against synthetic input without spinning up
real files or threads.

The engine wraps Drain3's ``TemplateMiner`` with a small overlay
that tracks per-cluster timing (count, first/last/intervals) so
we can emit the periodic SUMMARY lines that make the drained log
useful — Drain3 alone gives you the cluster id and template, but
not the "every 10.0s ±0.05" framing.

Output decisions per input line:

  * **First occurrence of a cluster** → emit verbatim immediately.
    Returned as ``(verbatim_line,)``.
  * **Repeat** → no verbatim emission. Counter + last-seen
    timestamp updated on the cluster; the line is included in the
    next periodic summary.

On a clock tick (``tick_summary``), the engine emits one SUMMARY
line per cluster with ≥2 occurrences since the previous tick, then
resets the per-window counters. Clusters with 0 or 1 occurrence in
a window produce no summary (the verbatim already covered them).
"""
from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

# Drain3 import is lazy to keep imports cheap in tests that mock
# the engine; the drainer module won't try to import drain3 until
# DrainerEngine is constructed.

# --- log-line parser --------------------------------------------------

#: Format produced by ``capture_log.py`` and ``cluster_log.py``:
#:   ``YYYY-MM-DD HH:MM:SS.fff  LEVEL    body``
#: ``LEVEL`` is exactly one of the five known names; any other
#: leading uppercase token (e.g., ``STALL``) is part of the body
#: and must NOT be parsed as a level — that's how legacy capture
#: lines end up with ``level=None`` and the body intact.
_LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d+)?)\s+"
    r"(?:(?P<level>ERROR|WARNING|INFO|VERBOSE|DEBUG|SUMMARY)\s+)?"
    r"(?P<body>.*)$"
)


@dataclass
class LineRecord:
    """One parsed input line.

    ``raw`` keeps the original text so the verbatim-on-first-match
    path can echo it without reconstructing. ``ts_seconds`` is the
    timestamp converted to a unix-style float so summary intervals
    can be computed cheaply; the parser sets it from the prefix
    when present, else inherits the wall clock at parse time.
    """
    raw: str
    ts_seconds: float
    level: str | None
    body: str


def parse_capture_line(
    line: str, fallback_now: float | None = None,
) -> LineRecord | None:
    """Parse a single log line. Returns None for empty/whitespace.

    Lines that don't match the regex (e.g., a multi-line traceback
    continuation) keep the previous timestamp via ``fallback_now``
    and use the whole line as ``body`` with ``level=None``.
    """
    s = line.rstrip("\n\r")
    if not s.strip():
        return None
    m = _LINE_RE.match(s)
    if m is None:
        return LineRecord(
            raw=line,
            ts_seconds=fallback_now if fallback_now is not None else 0.0,
            level=None,
            body=s,
        )
    ts_str = m.group("ts")
    ts_seconds = _parse_ts(ts_str, fallback_now)
    return LineRecord(
        raw=line,
        ts_seconds=ts_seconds,
        level=m.group("level"),
        body=m.group("body"),
    )


def _parse_ts(ts_str: str, fallback: float | None) -> float:
    import datetime as _dt
    try:
        if "." in ts_str:
            dt = _dt.datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S.%f")
        else:
            dt = _dt.datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
        return dt.timestamp()
    except ValueError:
        return fallback if fallback is not None else 0.0


# --- cluster bookkeeping ---------------------------------------------


@dataclass
class _ClusterStats:
    """Per-template state used to compose the SUMMARY lines."""
    template: str
    level: str | None
    last_level: str | None
    first_seen: float
    last_seen: float
    total_count: int = 0           # lifetime count, never resets
    window_count: int = 0          # since last tick_summary
    window_first: float | None = None
    intervals: list[float] = field(default_factory=list)


@dataclass(frozen=True)
class DrainSummary:
    """One emitted summary line.

    Wrapped as a record (rather than a raw string) so callers can
    format consistently across CLI / test / future programmatic
    consumers. ``render`` produces the on-wire string.

    Two timestamps for one event:
      * ``emission_ts`` — when this SUMMARY was actually written.
        Used as the on-wire prefix so a ``tail -f`` reader sees a
        monotonic stream (otherwise summaries appear "back in
        time" relative to the verbatim lines emitted between
        their last sample and the summary tick).
      * ``last_ts`` — when the most recent sample landed.
        Available on the dataclass for programmatic consumers but
        intentionally NOT in the rendered output, since
        ``count`` + ``avg_interval_s`` already describe the window.
    """
    cluster_id: int
    template: str
    level: str | None
    count: int
    span_s: float
    avg_interval_s: float
    jitter_s: float
    last_ts: float
    emission_ts: float

    def render(self) -> str:
        from datetime import datetime
        ts = datetime.fromtimestamp(self.emission_ts).strftime(
            "%Y-%m-%d %H:%M:%S.%f",
        )[:-3]
        level_part = f"{self.level} " if self.level else ""
        if self.avg_interval_s > 0:
            interval_part = (
                f" (every {self.avg_interval_s:.2f}s "
                f"±{self.jitter_s:.2f})"
            )
        else:
            interval_part = ""
        return (
            f"{ts}  SUMMARY  [×{self.count}]  "
            f"{level_part}{self.template}{interval_part}"
        )


# --- the engine ------------------------------------------------------


class DrainerEngine:
    """Drain3-backed clustering + periodic-summary engine.

    Args:
        verbatim_match: regex applied to the *body* of each line.
            If it matches, the line is emitted verbatim every time
            (not just on first occurrence). Default keeps every
            STALL event and every capture-lifecycle line as
            first-class — they're load-bearing for diagnosis.
        depth: Drain3 tree depth. Default 4 matches the upstream
            recommendation for typical log formats.
    """

    DEFAULT_VERBATIM_RE = (
        r"\bSTALL\b"
        r"|\bcapture (started|stopped)\b"
        r"|\bbtviz (startup|exit)\b"
        r"|\bdongle short_id="     # per-dongle discovery rows
        r"|\brole short_id="       # per-sniffer role assignment
    )

    def __init__(
        self,
        *,
        verbatim_match: str | re.Pattern[str] | None = None,
        depth: int = 4,
    ) -> None:
        from drain3 import TemplateMiner
        from drain3.template_miner_config import TemplateMinerConfig
        cfg = TemplateMinerConfig()
        cfg.drain_depth = depth
        cfg.drain_sim_th = 0.4
        cfg.drain_max_children = 100
        cfg.drain_max_clusters = 1024
        cfg.snapshot_interval_minutes = 0  # don't write snapshots
        cfg.profiling_enabled = False
        # Quiet drain3's own logger — it warns about missing config
        # files at INFO and that noise belongs nowhere near the user.
        logging.getLogger("drain3").setLevel(logging.WARNING)
        logging.getLogger("drain3.template_miner").setLevel(
            logging.WARNING,
        )
        self._miner = TemplateMiner(config=cfg)
        if verbatim_match is None:
            verbatim_match = self.DEFAULT_VERBATIM_RE
        self._verbatim_re = (
            verbatim_match if isinstance(verbatim_match, re.Pattern)
            else re.compile(verbatim_match)
        )
        self._stats: dict[int, _ClusterStats] = {}

    # -- per-line ingest --------------------------------------------

    def ingest(self, rec: LineRecord) -> tuple[str, ...]:
        """Feed one parsed line. Returns lines to write (0 or 1).

        Returns ``()`` when the line is a repeat that the engine
        will roll into the next summary. Returns ``(rec.raw,)`` for
        first occurrences (or always-verbatim matches).
        """
        # Always-verbatim: no clustering bookkeeping at all. These
        # lines are infrequent and we want them to read 1:1 in the
        # drained file just like in the source.
        if self._verbatim_re.search(rec.body):
            return (rec.raw,)

        result = self._miner.add_log_message(rec.body)
        cluster_id = result["cluster_id"]
        template = result["template_mined"]
        change_type = result["change_type"]

        st = self._stats.get(cluster_id)
        if st is None:
            # First time we've seen this cluster — initialize and
            # emit verbatim. Subsequent matches roll into summaries.
            st = _ClusterStats(
                template=template,
                level=rec.level,
                last_level=rec.level,
                first_seen=rec.ts_seconds,
                last_seen=rec.ts_seconds,
                total_count=1,
                window_count=1,
                window_first=rec.ts_seconds,
            )
            self._stats[cluster_id] = st
            return (rec.raw,)

        # Update bookkeeping for repeats. Drain3 may rewrite the
        # template as it sees more variants — keep the latest so
        # SUMMARY lines reflect the most-general form.
        if change_type != "none":
            st.template = template
        if rec.level is not None:
            st.last_level = rec.level
        prev_seen = st.last_seen
        st.last_seen = rec.ts_seconds
        st.total_count += 1
        st.window_count += 1
        if st.window_first is None:
            st.window_first = rec.ts_seconds
        delta = rec.ts_seconds - prev_seen
        if delta > 0:
            st.intervals.append(delta)
            # Cap interval samples so a long-running template
            # doesn't grow this list unboundedly. 256 samples is
            # plenty for stable mean+stddev.
            if len(st.intervals) > 256:
                st.intervals = st.intervals[-256:]
        return ()

    # -- periodic summary tick --------------------------------------

    def tick_summary(self, now: float | None = None) -> tuple[DrainSummary, ...]:
        """Emit a summary for every cluster active this window.

        "Active" = ``window_count >= 2``. Single events were
        already covered by the verbatim emission. Returns the
        summaries in stable cluster_id order so the drained log
        groups consistently.

        ``now`` is the wall-clock used as the SUMMARY's
        ``emission_ts`` — i.e., what the user sees as the prefix
        timestamp on the rendered line. Defaults to the latest
        sample's timestamp (``last_ts``) for callers that don't
        care about emission ordering, but the tailer always passes
        the real clock so a ``tail -f`` reader sees monotonic
        timestamps in the file.
        """
        summaries: list[DrainSummary] = []
        for cid in sorted(self._stats.keys()):
            st = self._stats[cid]
            if st.window_count < 2:
                # Reset window counters even when we don't emit so
                # the next window starts fresh.
                st.window_count = 0
                st.window_first = None
                continue
            avg, jitter = _interval_stats(st.intervals)
            span_s = (
                st.last_seen - st.window_first
                if st.window_first is not None else 0.0
            )
            emission_ts = now if now is not None else st.last_seen
            summaries.append(DrainSummary(
                cluster_id=cid,
                template=st.template,
                level=st.last_level,
                count=st.window_count,
                span_s=span_s,
                avg_interval_s=avg,
                jitter_s=jitter,
                last_ts=st.last_seen,
                emission_ts=emission_ts,
            ))
            st.window_count = 0
            st.window_first = None
        return tuple(summaries)

    # -- introspection (used by tests) ------------------------------

    def cluster_count(self) -> int:
        return len(self._stats)

    def stats(self) -> Iterable[tuple[int, _ClusterStats]]:
        return self._stats.items()


def _interval_stats(intervals: list[float]) -> tuple[float, float]:
    """Mean + population stddev of a list of float intervals."""
    if not intervals:
        return 0.0, 0.0
    n = len(intervals)
    mean = sum(intervals) / n
    var = sum((x - mean) ** 2 for x in intervals) / n
    return mean, math.sqrt(var)
