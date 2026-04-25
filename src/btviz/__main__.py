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

    # Default: launch GUI.
    from .ui.app import run_gui
    return run_gui()


if __name__ == "__main__":
    raise SystemExit(main())
