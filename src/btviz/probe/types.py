"""Dataclasses crossing the probe-coordinator / worker / storage seam.

Kept deliberately plain — these objects travel between the main
thread and a worker thread via Qt queued signals, so they need to
be picklable / copyable without surprises.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field


class ProbeOutcome(str, enum.Enum):
    """Terminal state of one ``ProbeRequest``."""

    PENDING = "pending"
    SUCCESS = "success"
    TIMEOUT = "timeout"
    REJECTED = "rejected"        # target rejected the connection
    CANCELLED = "cancelled"      # user-initiated cancel
    ERROR = "error"              # transport or HCI-level failure


@dataclass(frozen=True)
class ProbeRequest:
    """One probe targeted at one device.

    ``device_id`` is the btviz row; ``addr`` is whatever address the
    target was last advertising on. ``addr_random`` distinguishes
    public-MAC targets from random (RPA / static-random) targets so
    the HCI ``LE Create Connection`` command sets the peer address
    type correctly.

    ``timeout_s`` is per-request so different device classes can
    have different patience (hearing aids and LE Audio peripherals
    need ~12 s; everything else 5 s).
    """

    device_id: int
    addr: str                      # "aa:bb:cc:dd:ee:ff"
    addr_random: bool
    timeout_s: float = 5.0
    irk_hex: str | None = None     # if known, helps re-acquire after RPA rotation


@dataclass(frozen=True)
class GattCharObservation:
    """One characteristic read result.

    Exactly one of (``value`` is not None) and (``att_error`` is not
    None) holds — enforced by the storage layer's CHECK constraint
    too. ``value=b''`` (empty bytes) means "char present, read
    succeeded, value is empty" and is *not* an error.
    """

    service_uuid: str              # full 128-bit UUID
    char_uuid: str
    value: bytes | None = None
    att_error: int | None = None   # 0x01..0xFF, see Core Spec Vol 3 Part F §3.4.1.1


@dataclass(frozen=True)
class GattService:
    """One primary service discovered on the target."""

    uuid: str
    char_uuids: tuple[str, ...] = ()


@dataclass
class ProbeResult:
    """What a probe actually delivered.

    Mutable so the worker can append observations as it goes; the
    storage adapter consumes a final snapshot. ``outcome`` starts at
    ``PENDING`` and transitions exactly once.
    """

    request: ProbeRequest
    started_at: float
    ended_at: float | None = None
    outcome: ProbeOutcome = ProbeOutcome.PENDING
    detail: str | None = None
    services: list[GattService] = field(default_factory=list)
    chars: list[GattCharObservation] = field(default_factory=list)
