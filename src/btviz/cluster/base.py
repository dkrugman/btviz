"""Core types for the cluster framework.

Defines the Signal protocol every concrete signal must satisfy, the
ClusterContext that holds shared state during a clustering run, and
the data classes that flow between layers.

Scoring convention: signals return a float in ``[-1.0, 1.0]`` or
``None``. Positive = evidence the two devices are the same physical
thing. Negative = evidence they are different (active mismatch, not
just absence of match). ``None`` = the signal abstains (data sparse,
ambiguous, or inapplicable). Most signals will return values in
``[0.0, 1.0]`` only — the negative range is reserved for signals
where mismatch is itself informative (tx_power_match,
pdu_distribution, service_uuid_match disagreement).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, runtime_checkable


@dataclass(frozen=True)
class Address:
    """A BLE address observation."""

    bytes_: bytes
    kind: str  # 'public' | 'random_static' | 'random_resolvable' | 'random_non_resolvable'
    resolved_via_irk_id: int | None = None


@dataclass(frozen=True)
class Device:
    """A device row as seen by the cluster framework.

    Concrete fields the signals need; everything else stays in the DB.
    The framework is built so a Device can be hydrated either from
    the SQLite DB (production) or from a Python dict (tests).
    """

    id: int
    device_class: str  # 'airtag' | 'iphone' | 'airpods' | 'hearing_aid' | ...
    address: Address
    first_seen: float
    last_seen: float
    label: str | None = None


@dataclass(frozen=True)
class ClassProfile:
    """Per-device-class weights and decision parameters."""

    name: str
    weights: Mapping[str, float]
    threshold: float
    min_total_weight: float = 0.50
    required_eventually: frozenset[str] = field(default_factory=frozenset)
    required_for_merge: frozenset[str] = field(default_factory=frozenset)
    params: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)


@dataclass
class ClusterContext:
    """Shared state for one clustering run.

    Built up at run start by ``ClusterRunner`` and passed read-only
    into every signal. ``cache`` is the only mutable field —
    individual signals may write per-device caches keyed by signal
    name to avoid recomputing distributions across the many pairs in
    a single run.
    """

    signals: Mapping[str, "Signal"]
    profiles: Mapping[str, ClassProfile]
    now: float
    irks: list[Any] = field(default_factory=list)
    db: Any = None
    cache: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Decision:
    """The aggregator's verdict on one device pair."""

    merge: bool
    score: float
    signals: dict[str, tuple[float, float]]  # name -> (score, weight)
    profile: str
    abort_reason: str | None = None


@runtime_checkable
class Signal(Protocol):
    """Protocol every concrete signal module must satisfy."""

    name: str

    def applies_to(
        self, ctx: ClusterContext, a: Device, b: Device
    ) -> bool:
        """Cheap pre-filter. Return False when the signal cannot meaningfully score this pair."""
        ...

    def score(
        self,
        ctx: ClusterContext,
        a: Device,
        b: Device,
        params: Mapping[str, Any] | None = None,
    ) -> float | None:
        """Return float in [-1.0, 1.0] or None to abstain."""
        ...


def pick_profile(
    ctx: ClusterContext, a: Device, b: Device
) -> ClassProfile | None:
    """Choose the profile to evaluate this pair under, or None to skip.

    Cross-class merges are forbidden by construction (returns None).
    Falls through to ``default`` when the device class is unknown.
    """
    if a.device_class != b.device_class:
        return None
    return ctx.profiles.get(a.device_class) or ctx.profiles.get("default")
