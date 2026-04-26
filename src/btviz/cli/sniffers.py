"""Interactive CLI for managing sniffers.

Usage:
    btviz sniffers

Starts the coordinator, applies default roles to all connected dongles,
and drops into a prompt. Commands:

    list | ls                     show sniffers + roles + packet counts
    pin    <dongle> <chs>         Pinned(chs)   e.g. 'pin 0 37'  'pin 0 37,38'
    scan   <dongle>               ScanUnmonitored
    follow <dongle> <addr> [r] [--irk <32hex>]
                                  Follow(addr); 'r' for random addr type;
                                  --irk pins a 128-bit IRK so the sniffer
                                  follows across RPA rotations
    idle   <dongle>               Idle
    refresh                       rediscover dongles
    help | ?                      this help
    quit | exit                   stop all sniffers and exit

<dongle> can be an index (0, 1, ...) or a short_id.
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict

from ..bus import EventBus, TOPIC_PACKET, TOPIC_SNIFFER_STATE
from ..capture.coordinator import CaptureCoordinator
from ..capture.roles import (
    Follow,
    Idle,
    Pinned,
    ScanUnmonitored,
    short_name,
)


class _PacketCounter:
    def __init__(self) -> None:
        self.counts: dict[str, int] = defaultdict(int)
        self._lock = threading.Lock()

    def on_packet(self, pkt) -> None:
        with self._lock:
            self.counts[pkt.source] += 1

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self.counts)


def _resolve_dongle(coord: CaptureCoordinator, token: str) -> str | None:
    """Accept either a numeric index or a short_id substring."""
    ids = [d.short_id for d in coord.dongles]
    if token.isdigit():
        i = int(token)
        if 0 <= i < len(ids):
            return ids[i]
        return None
    # short_id or unique substring
    matches = [s for s in ids if token in s]
    if len(matches) == 1:
        return matches[0]
    return None


def _list(coord: CaptureCoordinator, counter: _PacketCounter) -> None:
    counts = counter.snapshot()
    if not coord.dongles:
        print("(no dongles)")
        return
    print(f"{'idx':<4}{'short_id':<32}{'role':<28}{'running':<9}{'adv_hop':<14}{'pkts':>8}")
    for i, d in enumerate(coord.dongles):
        sp = coord.sniffers.get(d.short_id)
        role = coord.get_role(d.short_id)
        running = sp.state.running if sp else False
        adv_hop = ",".join(str(c) for c in (sp.state.adv_hop if sp else []))
        n = counts.get(d.short_id, 0)
        print(f"{i:<4}{d.short_id:<32}{short_name(role):<28}{str(running):<9}{adv_hop:<14}{n:>8}")


def _parse_channels(arg: str) -> tuple[int, ...]:
    parts = [p.strip() for p in arg.split(",") if p.strip()]
    return tuple(int(p) for p in parts)


def _do_command(
    coord: CaptureCoordinator,
    counter: _PacketCounter,
    line: str,
) -> bool:
    """Run one command line. Returns False if we should exit."""
    parts = line.strip().split()
    if not parts:
        return True
    cmd, args = parts[0].lower(), parts[1:]

    if cmd in ("quit", "exit", "q"):
        return False
    if cmd in ("help", "?", "h"):
        print(__doc__)
        return True
    if cmd in ("list", "ls"):
        _list(coord, counter)
        return True
    if cmd == "refresh":
        coord.refresh_dongles()
        coord.start_discover()
        _list(coord, counter)
        return True

    # role-changing commands all need a <dongle> token
    if not args:
        print(f"error: {cmd} requires a <dongle> argument")
        return True
    dongle_token = args[0]
    did = _resolve_dongle(coord, dongle_token)
    if did is None:
        print(f"error: no dongle matches {dongle_token!r}")
        return True

    try:
        if cmd == "pin":
            if len(args) < 2:
                print("usage: pin <dongle> <channels>  e.g. pin 0 37  pin 0 37,38")
                return True
            chs = _parse_channels(args[1])
            coord.set_role(did, Pinned(chs))
        elif cmd == "scan":
            coord.set_role(did, ScanUnmonitored())
        elif cmd == "follow":
            if len(args) < 2:
                print("usage: follow <dongle> <addr> [random] [--irk <32-hex>]")
                return True
            addr = args[1]
            is_random = False
            irk_hex: str | None = None
            i = 2
            while i < len(args):
                tok = args[i]
                if tok.lower().startswith("r"):
                    is_random = True
                elif tok == "--irk":
                    if i + 1 >= len(args):
                        print("usage: --irk requires a 32-hex-char value")
                        return True
                    irk_hex = args[i + 1]
                    i += 1
                else:
                    print(f"unrecognized follow arg: {tok!r}")
                    return True
                i += 1
            coord.set_role(did, Follow(addr, is_random, irk_hex=irk_hex))
        elif cmd == "idle":
            coord.set_role(did, Idle())
        else:
            print(f"unknown command: {cmd} (try 'help')")
            return True
        _list(coord, counter)
    except Exception as e:  # noqa: BLE001
        print(f"error: {e}")
    return True


def run_sniffers_cli() -> int:
    bus = EventBus()
    coord = CaptureCoordinator(bus)
    counter = _PacketCounter()

    bus.subscribe(TOPIC_PACKET, counter.on_packet)
    bus.subscribe(
        TOPIC_SNIFFER_STATE,
        lambda s: s.last_error and print(f"[{s.dongle.short_id}] {s.last_error}"),
    )

    print("btviz sniffers  (type 'help' for commands, 'quit' to exit)")
    print()
    try:
        coord.refresh_dongles()
    except Exception as e:  # noqa: BLE001
        print(f"error: {e}")
        return 2

    if not coord.dongles:
        print("no dongles found.")
        return 1

    coord.start_discover()
    time.sleep(0.3)   # let extcap processes settle
    _list(coord, counter)

    try:
        while True:
            try:
                line = input("btviz> ")
            except EOFError:
                print()
                break
            if not _do_command(coord, counter, line):
                break
    except KeyboardInterrupt:
        print()
    finally:
        coord.stop_all()

    return 0
