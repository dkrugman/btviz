"""btviz CLI entrypoint."""
from __future__ import annotations

import argparse
import sys


def main() -> int:
    p = argparse.ArgumentParser(prog="btviz")
    sub = p.add_subparsers(dest="cmd")

    # subcommand: sniffers (interactive role management)
    sub.add_parser("sniffers", help="Interactive sniffer management (list / pin / scan / follow / idle)")

    # subcommand: ingest (pcap/pcapng → DB)
    from .cli.ingest import build_parser as _build_ingest_parser
    _build_ingest_parser(
        sub.add_parser("ingest", help="Ingest a pcap/pcapng file into the DB")
    )

    # subcommand: canvas (per-project device board)
    canvas_p = sub.add_parser("canvas", help="Open the project canvas (GUI)")
    canvas_p.add_argument(
        "--project", default=None, metavar="NAME",
        help="open this project directly; if omitted, show the picker",
    )
    canvas_p.add_argument(
        "--db", default=None, metavar="PATH",
        help="SQLite DB path (default: platform XDG/Application Support)",
    )

    # top-level flags (kept for backward compat)
    p.add_argument(
        "--list-interfaces",
        action="store_true",
        help="Print discovered nRF Sniffer dongles and exit.",
    )
    args = p.parse_args()

    if args.list_interfaces:
        from .extcap import find_extcap_binary, list_dongles
        try:
            binary = find_extcap_binary()
        except Exception as e:  # noqa: BLE001
            print(f"error: {e}", file=sys.stderr)
            return 2
        print(f"extcap: {binary}")
        for d in list_dongles(binary):
            print(f"  {d.short_id:30s}  {d.serial_path}  ({d.display})")
        return 0

    if args.cmd == "sniffers":
        from .cli import run_sniffers_cli
        return run_sniffers_cli()

    if args.cmd == "ingest":
        from .cli.ingest import run as run_ingest
        return run_ingest(args)

    if args.cmd == "canvas":
        from pathlib import Path
        from .ui.canvas import run_canvas
        return run_canvas(
            db_path=Path(args.db) if args.db else None,
            project_name=args.project,
        )

    # Default (no subcommand): open the canvas with the project picker.
    # The legacy "Bluetooth Discovery" window (ui.app) was retired —
    # the canvas does everything it did and more (device-class
    # enrichment, Auracast extraction, persistent layout, right-click
    # Follow), and running both at once meant two SnifferProcess pools
    # fighting over the same FIFOs.
    from .ui.canvas import run_canvas
    return run_canvas()


if __name__ == "__main__":
    raise SystemExit(main())
