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

Output rotation uses a hand-rolled text sink rather than
``logging.handlers.RotatingFileHandler`` because we're emitting
raw lines (already timestamped + level-tagged by the engine),
not LogRecords through a Formatter. The rotation policy mirrors
the one capture.log uses, with a much larger budget — drained
bytes are denser per byte than raw bytes so a 50 MB × 5 budget
holds far more time horizon than the same on capture.log.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import IO, Callable

from .drainer import DrainerEngine, parse_capture_line


#: Default rotation budget for drained_*.log. Larger than
#: capture.log's 10 MB cap because the drained file is a
#: long-term audit trail — repetitive content is collapsed
#: to one SUMMARY/min so 50 MB holds many sessions worth of
#: history rather than hours.
DEFAULT_MAX_BYTES = 50 * 1024 * 1024
DEFAULT_BACKUP_COUNT = 5


class _RotatingTextSink:
    """Append-with-rotation sink for the drained log.

    Mirrors ``logging.handlers.RotatingFileHandler`` semantics
    (rename ``foo.log`` → ``foo.log.1`` → ``foo.log.2`` …, drop
    the oldest, reopen) without going through Python's logging
    machinery — we already have fully-formatted strings to emit
    and don't want a second layer of formatting.

    Size check happens *before* each write rather than after, so
    the sink never produces a file marginally larger than
    ``max_bytes``. The check counts UTF-8 bytes of the candidate
    write to match what actually lands on disk.
    """

    def __init__(
        self,
        path: Path,
        max_bytes: int = DEFAULT_MAX_BYTES,
        backup_count: int = DEFAULT_BACKUP_COUNT,
    ) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path = path
        self._max_bytes = max_bytes
        self._backup_count = backup_count
        self._fh: IO[str] | None = open(path, "a", encoding="utf-8")

    def write(self, text: str) -> None:
        if self._fh is None:
            return
        n = len(text.encode("utf-8"))
        try:
            current = self._path.stat().st_size
        except FileNotFoundError:
            current = 0
        if current + n > self._max_bytes and current > 0:
            self._rotate()
        assert self._fh is not None
        self._fh.write(text)

    def flush(self) -> None:
        if self._fh is not None:
            self._fh.flush()

    def close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            except Exception:  # noqa: BLE001
                pass
            self._fh = None

    def _rotated_path(self, n: int) -> Path:
        # foo.log → foo.log.1 / .2 / …
        return self._path.with_name(f"{self._path.name}.{n}")

    def _rotate(self) -> None:
        # Close current, shift backups, reopen fresh.
        if self._fh is not None:
            try:
                self._fh.close()
            except Exception:  # noqa: BLE001
                pass
        # Drop the oldest if it would push us past the cap.
        oldest = self._rotated_path(self._backup_count)
        if oldest.exists():
            try:
                oldest.unlink()
            except OSError:
                pass
        # Shift down: .N-1 → .N, ..., .1 → .2.
        for i in range(self._backup_count - 1, 0, -1):
            src = self._rotated_path(i)
            dst = self._rotated_path(i + 1)
            if src.exists():
                try:
                    src.rename(dst)
                except OSError:
                    pass
        # Current → .1.
        if self._path.exists():
            try:
                self._path.rename(self._rotated_path(1))
            except OSError:
                pass
        self._fh = open(self._path, "a", encoding="utf-8")


def drain_file(
    input_path: Path,
    output_path: Path,
    *,
    summary_interval_s: float = 60.0,
    from_start: bool = False,
    poll_interval_s: float = 0.25,
    stop_when_eof: bool = False,
    max_bytes: int = DEFAULT_MAX_BYTES,
    backup_count: int = DEFAULT_BACKUP_COUNT,
    clock: Callable[[], float] = time.time,
) -> int:
    """Run the drainer loop. Returns the line count processed.

    Args:
        input_path: source log file (e.g., ``~/.btviz/capture.log``).
        output_path: destination drained log. Opened in append mode
            so multiple drainer runs over time accumulate cleanly,
            and ``tail -f`` survives drainer restarts. Rotates at
            ``max_bytes`` with ``backup_count`` history files.
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
        max_bytes: per-file cap before the drained log rotates.
            Default 50 MB; larger than capture.log's 10 MB because
            drained bytes are denser per byte (lots of repeats
            collapsed) so the same time horizon needs fewer files.
        backup_count: how many rotated backups to keep
            (``foo.log.1``…``foo.log.N``). Default 5, matching
            capture.log's policy.
        clock: injectable time source for tests.

    Returns: number of input lines processed.
    """
    engine = DrainerEngine()
    sink = _RotatingTextSink(
        output_path, max_bytes=max_bytes, backup_count=backup_count,
    )
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
                        sink.write(emit)
                        if not emit.endswith("\n"):
                            sink.write("\n")
                    sink.flush()
                line_count += 1
                continue

            now = clock()
            if now - last_summary_at >= summary_interval_s:
                summaries = engine.tick_summary()
                if summaries:
                    for s in summaries:
                        sink.write(s.render() + "\n")
                    sink.flush()
                last_summary_at = now

            if stop_when_eof:
                # Final flush of any pending repeats so the test
                # sees them without waiting for a tick.
                summaries = engine.tick_summary()
                if summaries:
                    for s in summaries:
                        sink.write(s.render() + "\n")
                    sink.flush()
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
        sink.close()


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
