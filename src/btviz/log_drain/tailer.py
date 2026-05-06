"""drain_file — tail-follow loop that drives the DrainerEngine.

Reads an input log path with ``tail -F`` semantics (handles
log rotation by re-stat'ing the file periodically) and writes
drained output to a destination path. Designed to run forever
until SIGINT — that's the typical user workflow:

    Terminal A:   btviz                       # produces capture.log
    Terminal B:   btviz drain capture         # this loop
    Terminal C:   tail -f drained_capture.log # human-readable view

The tailer is the I/O boundary; the actual clustering / summary
logic lives in :class:`DrainerEngine` so it can be unit-tested
without real files. The two are connected only via
``LineRecord``.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import IO, Callable

from .drainer import DrainerEngine, parse_capture_line


def drain_file(
    input_path: Path,
    output_path: Path,
    *,
    summary_interval_s: float = 60.0,
    from_start: bool = False,
    poll_interval_s: float = 0.25,
    stop_when_eof: bool = False,
    clock: Callable[[], float] = time.time,
) -> int:
    """Run the drainer loop. Returns the line count processed.

    Args:
        input_path: source log file (e.g., ``~/.btviz/capture.log``).
        output_path: destination drained log. Opened in append mode
            so multiple drainer runs over time accumulate cleanly,
            and ``tail -f`` survives drainer restarts.
        summary_interval_s: emit periodic SUMMARY lines this often.
            60 s default matches "I want a per-minute heartbeat
            view" without flooding the file under quiet conditions.
        from_start: if False (default), seek to end of input before
            reading new lines — the typical "tail" mode. If True,
            replay the entire file first, then tail. Useful for
            post-mortem on an existing log.
        poll_interval_s: how long to sleep when no new bytes are
            available. Trades latency vs. CPU; 250 ms feels
            instant under tail -f.
        stop_when_eof: if True, exit cleanly at EOF instead of
            blocking. Used by the test suite to drive a fixture
            file deterministically.
        clock: injectable time source for tests.

    Returns: number of input lines processed.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    engine = DrainerEngine()
    out: IO[str] = open(output_path, "a", encoding="utf-8")
    inp = _open_input(input_path, from_start=from_start)
    last_summary_at = clock()
    last_inode = _stat_inode(input_path)
    line_count = 0

    try:
        while True:
            line = inp.readline() if inp is not None else ""
            if line:
                rec = parse_capture_line(line, fallback_now=clock())
                if rec is not None:
                    for emit in engine.ingest(rec):
                        out.write(emit)
                        if not emit.endswith("\n"):
                            out.write("\n")
                    out.flush()
                line_count += 1
                continue

            now = clock()
            if now - last_summary_at >= summary_interval_s:
                summaries = engine.tick_summary()
                if summaries:
                    for s in summaries:
                        out.write(s.render() + "\n")
                    out.flush()
                last_summary_at = now

            if stop_when_eof:
                # Final flush of any pending repeats so the test
                # sees them without waiting for a tick.
                summaries = engine.tick_summary()
                if summaries:
                    for s in summaries:
                        out.write(s.render() + "\n")
                    out.flush()
                return line_count

            # Rotation detection: if the input file has been
            # renamed (capture.log → capture.log.1) and a fresh
            # one created in its place, our open fd is on the
            # rotated copy. Check the inode and reopen on change.
            current_inode = _stat_inode(input_path)
            if current_inode is not None and current_inode != last_inode:
                if inp is not None:
                    try:
                        inp.close()
                    except Exception:  # noqa: BLE001
                        pass
                inp = _open_input(input_path, from_start=True)
                last_inode = current_inode
                continue
            time.sleep(poll_interval_s)
    finally:
        if inp is not None:
            try:
                inp.close()
            except Exception:  # noqa: BLE001
                pass
        try:
            out.close()
        except Exception:  # noqa: BLE001
            pass


def _open_input(path: Path, *, from_start: bool) -> IO[str] | None:
    """Open the input log; seek to end unless ``from_start``.

    Returns None when the file doesn't yet exist — caller can
    poll until it appears (matches ``tail -F`` retry semantics).
    """
    if not path.exists():
        return None
    f = open(path, "r", encoding="utf-8", errors="replace")
    if not from_start:
        f.seek(0, os.SEEK_END)
    return f


def _stat_inode(path: Path) -> int | None:
    try:
        return path.stat().st_ino
    except FileNotFoundError:
        return None
