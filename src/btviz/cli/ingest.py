"""`btviz ingest` — load a pcap/pcapng file into the btviz DB.

Usage::

    btviz ingest <file> [--project NAME] [--session NAME] [--keep-bad-crc]
                        [--db PATH]

A session row is created under the named project (created on first use).
Prints an IngestReport summary on success.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ..db.store import default_db_path, open_store
from ..ingest import TsharkError, TsharkNotFound, ingest_file


def build_parser(p: argparse.ArgumentParser) -> argparse.ArgumentParser:
    p.add_argument("file", type=Path, help="pcap or pcapng file to ingest")
    p.add_argument(
        "--project", default="default",
        help="project name; created if it doesn't exist (default: 'default')",
    )
    p.add_argument(
        "--session", default=None, metavar="NAME",
        help="optional session label (defaults to the file's basename)",
    )
    p.add_argument(
        "--keep-bad-crc", action="store_true",
        help="retain CRC-failed packets (normally dropped: they produce "
             "phantom addresses)",
    )
    p.add_argument(
        "--db", type=Path, default=None, metavar="PATH",
        help=f"SQLite DB path (default: {default_db_path()})",
    )
    return p


def run(args: argparse.Namespace) -> int:
    if not args.file.exists():
        print(f"error: file not found: {args.file}", file=sys.stderr)
        return 2

    session_name = args.session or args.file.name

    try:
        with open_store(args.db) as store:
            report = ingest_file(
                args.file, store,
                project=args.project,
                session_name=session_name,
                keep_bad_crc=args.keep_bad_crc,
            )
    except TsharkNotFound as e:
        print(f"error: {e}", file=sys.stderr)
        return 3
    except TsharkError as e:
        print(f"error: tshark failed: {e}", file=sys.stderr)
        return 4

    print(report.format())
    return 0
