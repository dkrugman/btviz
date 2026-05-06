"""btviz CLI entrypoint."""
from __future__ import annotations

import argparse
import sys


def main() -> int:
    from .cluster import (
        apply_cluster_log_prefs, configure_cluster_log, get_cluster_logger,
    )
    from .capture_log import (
        apply_capture_log_prefs, configure_capture_log, get_capture_logger,
    )
    configure_cluster_log()
    configure_capture_log()
    # Apply log-level preferences at startup. Both ``capture.log_level``
    # and ``cluster.log_level`` are 5-state dropdowns
    # (error/warning/info/verbose/debug); the apply functions resolve
    # the string to a numeric level. Errors reading prefs (e.g.,
    # early-bootstrap) silently fall back to the configure_*_log
    # defaults (INFO).
    try:
        from .preferences import get_prefs
        prefs = get_prefs()
        apply_cluster_log_prefs(prefs.get("cluster.log_level"))
        apply_capture_log_prefs(prefs.get("capture.log_level"))
    except Exception:  # noqa: BLE001 — preferences unavailable
        pass
    # "btviz startup" denotes program start — fires once per process
    # at __main__.main(). Capture-session lifecycle ("capture started"
    # / "capture stopped") logs separately from _start_live /
    # _stop_live so the two events are never conflated in the file.
    get_cluster_logger().info("btviz startup")
    get_capture_logger().info("btviz startup")

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
