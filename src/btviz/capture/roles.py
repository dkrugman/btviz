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

import random
from dataclasses import dataclass

# Primary BLE advertising channels.
PRIMARY_ADV_CHANNELS: tuple[int, ...] = (37, 38, 39)

# Data channels — everything that's not a primary advertising channel.
# BLE channel index space is 0..39; 37/38/39 are advertising, 0..36 are
# data. Some of these can also carry secondary-advertising payloads on
# 5.0+ extended advertising, but for our purposes the distinction here
# is just "channels a sniffer can be pointed at to hear connection /
# extended-advertising traffic."
DATA_CHANNELS: tuple[int, ...] = tuple(range(37))


def find_unmonitored_stream(
    excluded: "set[int] | None" = None,
    *,
    rng: random.Random | None = None,
) -> int:
    """Pick a random data channel (0..36) the caller is not already on.

    Today this is a stub for testing the sniffer panel's channel
    display: idle sniffers are assigned a random data channel so the
    panel shows something meaningful while the real "tune to expected
    data-channel transmissions based on advertising data" logic is
    designed.

    ``excluded`` lets the caller pass channels already covered by other
    sniffers so we spread out instead of stacking. Passing ``None`` (or
    an empty set) returns any data channel. ``rng`` is for deterministic
    tests; defaults to the module-global Random.
    """
    pool = [c for c in DATA_CHANNELS if not excluded or c not in excluded]
    if not pool:
        # All 37 data channels covered — just pick any. Saves the caller
        # from needing to handle "all monitored already" as a special case
        # while we're in the testing-stub stage.
        pool = list(DATA_CHANNELS)
    rng = rng or random
    return rng.choice(pool)


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
    # Optional 128-bit Identity Resolving Key — 32 hex chars (16 bytes), no
    # ``0x`` prefix. When set, the sniffer can resolve the device's RPA
    # rotation and keep following across address changes. Wire-up to the
    # extcap is done via Wireshark's control-pipe protocol (Key selector =
    # IRK, Value = 0x<hex>) — see TODO in src/btviz/extcap/sniffer.py.
    irk_hex: str | None = None

    def __post_init__(self) -> None:
        # Light validation; real formatting happens in sniffer.py _format_addr.
        parts = self.target_addr.split(":")
        if len(parts) != 6 or any(len(p) != 2 for p in parts):
            raise ValueError(f"expected aa:bb:cc:dd:ee:ff, got {self.target_addr!r}")
        if self.irk_hex is not None:
            stripped = self.irk_hex.lower().removeprefix("0x")
            if len(stripped) != 32 or not all(c in "0123456789abcdef" for c in stripped):
                raise ValueError(
                    f"IRK must be 32 hex chars (128 bits), got {self.irk_hex!r}"
                )


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
        suffix = ""
        if role.is_random:
            suffix += " random"
        if role.irk_hex:
            # Show only first/last 4 chars so the key isn't echoed in full.
            irk = role.irk_hex.lower().removeprefix("0x")
            suffix += f" irk={irk[:4]}…{irk[-4:]}"
        return f"follow({role.target_addr}{suffix})"
    return str(role)  # unreachable with correct types


def default_roles(
    dongle_ids: list[str],
    *,
    tx_capable_ids: set[str] | None = None,
) -> dict[str, SnifferRole]:
    """Initial role assignment given N connected dongles.

    With ≤ 3 dongles the policy is unchanged: every dongle gets a
    sniffing role because we need every radio scanning the primary
    advertising channels. A TX-capable device in this regime gets a
    sniffing role too — it can be momentarily borrowed for an
    interrogation TX action.

    With ≥ 4 dongles we have spare radios, so we deliberately reserve
    TX-capable devices as ``Idle``; RX-only sniffer-firmware devices
    are preferred for the three primary-channel pin roles. The
    reserved TX device is then available for follow / interrogation
    tasks without disturbing primary-channel coverage.

    ``tx_capable_ids`` is the set of ``dongle_ids`` whose firmware is
    TX-capable. Pass ``None`` (the default) to treat every dongle as
    interchangeable, which preserves the pre-capability behaviour.

      1 dongle   -> d0=ScanUnmonitored                       (hops 37/38/39)
      2 dongles  -> d0=Pinned([37]),  d1=ScanUnmonitored     (-> hops [38,39])
      3 dongles  -> d0=Pinned([37]),  d1=Pinned([38]),  d2=Pinned([39])
      4+ dongles -> 3 RX-only pinned to 37/38/39 (when available);
                     TX-capable devices Idle (reserved); remaining
                     RX-only devices Idle.
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
    if n == 3:
        return {
            did: Pinned((ch,))
            for did, ch in zip(dongle_ids, PRIMARY_ADV_CHANNELS)
        }

    # n >= 4. Sort dongles so RX-only come first (preferred for the
    # three pin roles); TX-capable trail (preferred for the Idle
    # reservation pool).
    tx_set = tx_capable_ids or set()
    rx_first = sorted(dongle_ids, key=lambda d: (d in tx_set,))
    result: dict[str, SnifferRole] = {
        did: Pinned((ch,))
        for did, ch in zip(rx_first[:3], PRIMARY_ADV_CHANNELS)
    }
    for did in rx_first[3:]:
        result[did] = Idle()
    return result
