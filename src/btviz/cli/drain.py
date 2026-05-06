"""``btviz drain`` — readability-compress capture.log / cluster.log.

Drives :func:`btviz.log_drain.drain_file` against the canonical
``~/.btviz/capture.log`` and / or ``~/.btviz/cluster.log`` paths.
Output lands in ``~/.btviz/drained_<source>.log`` (configurable)
and is appended to so multiple drainer runs accumulate cleanly
and ``tail -f`` survives drainer restarts.

Usage::

    btviz drain capture                       # tail capture.log
    btviz drain cluster                       # tail cluster.log
    btviz drain both                          # both, in parallel threads
    btviz drain capture --from-start          # replay then tail
    btviz drain capture --summary-interval-s 30
    btviz drain capture --input /path/to/x.log --output /tmp/y.log
"""
from __future__ import annotations

import argparse
import sys
import threading
from pathlib import Path

_DEFAULT_LOG_DIR = Path.home() / ".btviz"


def build_parser(p: argparse.ArgumentParser) -> argparse.ArgumentParser:
    p.add_argument(
        "source",
        choices=("capture", "cluster", "both"),
        help="which log to drain",
    )
    p.add_argument(
        "--input", type=Path, default=None, metavar="PATH",
        help="override input log path (default: ~/.btviz/<source>.log; "
             "ignored when source=both)",
    )
    p.add_argument(
        "--output", type=Path, default=None, metavar="PATH",
        help="override output drained-log path "
             "(default: ~/.btviz/drained_<source>.log; "
             "ignored when source=both)",
    )
    p.add_argument(
        "--summary-interval-s", type=float, default=60.0, metavar="N",
        help="how often to emit per-cluster SUMMARY lines (default: 60)",
    )
    p.add_argument(
        "--from-start", action="store_true",
        help="replay the entire input file from the beginning before "
             "tailing (default: start from end-of-file)",
    )
    p.add_argument(
        "--max-bytes", type=int, default=50 * 1024 * 1024, metavar="N",
        help="rotate the drained log at this size (default: 50 MB; "
             "larger than capture.log's 10 MB because drained bytes "
             "are denser)",
    )
    p.add_argument(
        "--backup-count", type=int, default=5, metavar="N",
        help="how many rotated backups (drained_*.log.1..N) to keep "
             "(default: 5)",
    )
    return p


def run(args: argparse.Namespace) -> int:
    """Top-level entry. Blocks until SIGINT (or EOF in tests)."""
    from ..log_drain import drain_file

    if args.source == "both":
        # Two threads, one per source. Daemon=True so a Ctrl-C kills
        # both promptly without each having to register a signal
        # handler. Outputs default to ~/.btviz/drained_*.log; the
        # --input/--output flags don't apply (would be ambiguous).
        if args.input or args.output:
            print(
                "error: --input / --output cannot be combined with "
                "source=both (the flags only address one stream).",
                file=sys.stderr,
            )
            return 2
        threads = [
            _spawn_drainer("capture", args),
            _spawn_drainer("cluster", args),
        ]
        try:
            for t in threads:
                t.join()
        except KeyboardInterrupt:
            print("\n  drain stopped (Ctrl-C)", file=sys.stderr)
        return 0

    inp = args.input or (_DEFAULT_LOG_DIR / f"{args.source}.log")
    out = args.output or (_DEFAULT_LOG_DIR / f"drained_{args.source}.log")
    print(
        f"  draining {inp} → {out}\n"
        f"  summary every {args.summary_interval_s:g}s "
        f"({'replay then tail' if args.from_start else 'tail only'})\n"
        f"  rotation: {args.max_bytes:,}B × {args.backup_count} backups\n"
        f"  (Ctrl-C to stop)",
        file=sys.stderr,
    )
    try:
        drain_file(
            inp, out,
            summary_interval_s=args.summary_interval_s,
            from_start=args.from_start,
            max_bytes=args.max_bytes,
            backup_count=args.backup_count,
        )
    except KeyboardInterrupt:
        print("\n  drain stopped (Ctrl-C)", file=sys.stderr)
    return 0


def _spawn_drainer(source: str, args: argparse.Namespace) -> threading.Thread:
    """Background drainer thread for one source. Used by source=both."""
    from ..log_drain import drain_file

    inp = _DEFAULT_LOG_DIR / f"{source}.log"
    out = _DEFAULT_LOG_DIR / f"drained_{source}.log"

    def _run() -> None:
        try:
            drain_file(
                inp, out,
                summary_interval_s=args.summary_interval_s,
                from_start=args.from_start,
                max_bytes=args.max_bytes,
                backup_count=args.backup_count,
            )
        except Exception as e:  # noqa: BLE001
            print(f"  drain {source} failed: {e!r}", file=sys.stderr)

    print(f"  draining {inp} → {out}", file=sys.stderr)
    t = threading.Thread(
        target=_run, daemon=True, name=f"drainer-{source}",
    )
    t.start()
    return t
