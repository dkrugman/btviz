"""Sniffer role types.

A role is the user's intent for one sniffer. The Coordinator translates
roles into concrete extcap control-pipe actions:

    Idle              -- extcap process stopped, waiting for work
    Pinned(channels)  -- extcap hops a fixed user-chosen subset of {37,38,39}
    ScanUnmonitored   -- extcap hops whichever primary adv channels are NOT
                         pinned by some other sniffer. Recomputed any time
                         a role changes.
    Follow(addr, random) -- extcap follows one specific BLE device

`ScanUnmonitored` is the only role with dynamic cross-sniffer policy;
everything else is a direct setting.
"""
from __future__ import annotations

from dataclasses import dataclass

# Primary BLE advertising channels.
PRIMARY_ADV_CHANNELS: tuple[int, ...] = (37, 38, 39)


@dataclass(frozen=True)
class Idle:
    pass


@dataclass(frozen=True)
class Pinned:
    channels: tuple[int, ...]

    def __post_init__(self) -> None:
        if not self.channels:
            raise ValueError("Pinned requires at least one channel")
        for ch in self.channels:
            if ch not in PRIMARY_ADV_CHANNELS:
                raise ValueError(
                    f"Pinned channel must be in {PRIMARY_ADV_CHANNELS}, got {ch}"
                )
        # reject duplicates
        if len(set(self.channels)) != len(self.channels):
            raise ValueError(f"Pinned channels must be unique: {self.channels}")


@dataclass(frozen=True)
class ScanUnmonitored:
    pass


@dataclass(frozen=True)
class Follow:
    target_addr: str                 # e.g. "aa:bb:cc:dd:ee:ff"
    is_random: bool = False

    def __post_init__(self) -> None:
        # Light validation; real formatting happens in sniffer.py _format_addr.
        parts = self.target_addr.split(":")
        if len(parts) != 6 or any(len(p) != 2 for p in parts):
            raise ValueError(f"expected aa:bb:cc:dd:ee:ff, got {self.target_addr!r}")


SnifferRole = Idle | Pinned | ScanUnmonitored | Follow


def short_name(role: SnifferRole) -> str:
    """Compact role label for CLI / UI display."""
    if isinstance(role, Idle):
        return "idle"
    if isinstance(role, Pinned):
        chs = ",".join(str(c) for c in role.channels)
        return f"pin[{chs}]"
    if isinstance(role, ScanUnmonitored):
        return "scan-unmonitored"
    if isinstance(role, Follow):
        return f"follow({role.target_addr}{' random' if role.is_random else ''})"
    return str(role)  # unreachable with correct types


def default_roles(dongle_ids: list[str]) -> dict[str, SnifferRole]:
    """Initial role assignment given N connected dongles.

      1 dongle   -> d0=ScanUnmonitored                       (hops 37/38/39)
      2 dongles  -> d0=Pinned([37]),  d1=ScanUnmonitored     (-> hops [38,39])
      3 dongles  -> d0=Pinned([37]),  d1=Pinned([38]),  d2=Pinned([39])
      4+ dongles -> first 3 pinned as above; extras Idle.
    """
    n = len(dongle_ids)
    if n == 0:
        return {}
    if n == 1:
        return {dongle_ids[0]: ScanUnmonitored()}
    if n == 2:
        return {
            dongle_ids[0]: Pinned((37,)),
            dongle_ids[1]: ScanUnmonitored(),
        }
    result: dict[str, SnifferRole] = {
        did: Pinned((ch,))
        for did, ch in zip(dongle_ids[:3], PRIMARY_ADV_CHANNELS)
    }
    for did in dongle_ids[3:]:
        result[did] = Idle()
    return result
