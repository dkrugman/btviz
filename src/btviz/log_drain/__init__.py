"""btviz log drainer — Drain3-backed readability compression.

Reads ``~/.btviz/capture.log`` (and/or ``cluster.log``), groups
similar lines into templates via the Drain3 algorithm, and emits a
parallel ``drained_*.log`` file optimized for tail-follow review.
First occurrences of a new template are emitted verbatim
(immediately) so novel events stay live; repeated occurrences are
rolled up into a periodic SUMMARY line every N seconds:

    2026-05-06 14:23:01.000  SUMMARY  [×6]  DEBUG watchdog tick (every 10.0s ±0.05)

Single events emit verbatim and never appear in summaries; repeated
events emit one verbatim sample then quiet down. Originals are
never modified — the drained file lives alongside.

CLI: ``btviz drain capture`` / ``btviz drain cluster`` /
``btviz drain both``.
"""
from __future__ import annotations

from .drainer import (
    DrainSummary,
    DrainerEngine,
    LineRecord,
    parse_capture_line,
)
from .tailer import drain_file

__all__ = [
    "DrainSummary",
    "DrainerEngine",
    "LineRecord",
    "drain_file",
    "parse_capture_line",
]
