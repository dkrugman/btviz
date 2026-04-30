"""rotation_cohort signal.

Scores the temporal-handoff plausibility of two RPAs being the same
device rotating identity. RPA-rotating devices (AirTags, iPhones,
AirPods) disappear and re-appear on a cadence; if A vanishes and B
appears within a plausible window on the same sniffer, that is
evidence of one device handing off identity.

This signal is implementation-target-1 because it works on temporal
data alone (no AD-vocabulary, no decoded payload, no spatial
diversity required) and gives the largest immediate disambiguation
win for the AirTag / iPhone clutter that motivates the whole effort.

Data source. Production: the ``packets`` table once the schema PR
lands. For this branch (pre-schema), the signal reads timestamps
from a per-device ``observations`` field on the Device-equivalent
test object — the in-memory shape used by the synthetic-data tests.
The DB-backed version is a one-line swap.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from ..base import ClusterContext, Device


@dataclass(frozen=True)
class _Params:
    expected_rotation: float = 900.0  # seconds
    window_min: float = 0.05
    window_max: float = 60.0
    min_observations: int = 1


def _params(raw: Mapping[str, Any] | None) -> _Params:
    raw = raw or {}
    return _Params(
        expected_rotation=float(raw.get("expected_rotation", 900.0)),
        window_min=float(raw.get("window_min", 0.05)),
        window_max=float(raw.get("window_max", 60.0)),
        min_observations=int(raw.get("min_observations", 1)),
    )


def _observations_on_sniffer(
    ctx: ClusterContext, device: Device
) -> dict[int, list[float]]:
    """Return {sniffer_id: [ts, ...]} for the device.

    Two ingestion paths share this function:

    1. **In-memory tests.** Synthetic observations are pre-loaded
       into ``ctx.cache["observations"]`` as ``{device_id: {sniffer_id:
       [ts, ...]}}``. Used by tests/cluster/test_framework.py.

    2. **Production (DB-backed).** When ``ctx.cache["observations"]``
       has no entry for this device, lazy-load from the ``packets``
       table. The result is cached so subsequent pairs sharing this
       device read from memory. With cache disabled the cost is one
       SQL query per device per run; ``run_once`` typically touches
       N devices and queries each one ~N times across the O(n²)
       pair loop, so caching turns N² queries into N.

    The fallback path also writes its result back into the cache, so
    tests that mix synthetic + DB-backed inputs in one ctx (none
    today, but future-proofing) get consistent behavior.

    Returns an empty dict when the device has no packet history (no
    sniffers ever attributed observations to it). The signal's
    ``applies_to`` will then return False and the aggregator routes
    the pair through ``missing_eventually`` if rotation_cohort is
    required-eventually for the profile.
    """
    cache = ctx.cache.setdefault("observations", {})
    if device.id in cache:
        return cache[device.id]

    if ctx.db is None:
        cache[device.id] = {}
        return {}

    rows = ctx.db.conn.execute(
        "SELECT sniffer_id, ts FROM packets"
        " WHERE device_id = ? AND sniffer_id IS NOT NULL",
        (device.id,),
    ).fetchall()
    out: dict[int, list[float]] = {}
    for r in rows:
        sniffer_id = r["sniffer_id"] if not isinstance(r, (tuple, list)) else r[0]
        ts = r["ts"] if not isinstance(r, (tuple, list)) else r[1]
        out.setdefault(sniffer_id, []).append(ts)
    cache[device.id] = out
    return out


class RotationCohort:
    name = "rotation_cohort"

    def applies_to(
        self, ctx: ClusterContext, a: Device, b: Device
    ) -> bool:
        if a.address.kind != "random_resolvable":
            return False
        if b.address.kind != "random_resolvable":
            return False
        obs_a = _observations_on_sniffer(ctx, a)
        obs_b = _observations_on_sniffer(ctx, b)
        if not obs_a or not obs_b:
            return False
        common = set(obs_a) & set(obs_b)
        if not common:
            return False
        return True

    def score(
        self,
        ctx: ClusterContext,
        a: Device,
        b: Device,
        params: Mapping[str, Any] | None = None,
    ) -> float | None:
        p = _params(params)
        obs_a = _observations_on_sniffer(ctx, a)
        obs_b = _observations_on_sniffer(ctx, b)
        common = set(obs_a) & set(obs_b)
        if not common:
            return None

        # Concurrent-existence rejection (refinement C6a).
        # If they were both observed *together* by the same sniffer
        # within the rotation window, they cannot be one rotating
        # device — return active-mismatch score.
        for s in common:
            if self._concurrently_observed(obs_a[s], obs_b[s], p.expected_rotation):
                return -1.0

        best: float | None = None
        for s in common:
            ts_a = obs_a[s]
            ts_b = obs_b[s]
            if len(ts_a) < p.min_observations or len(ts_b) < p.min_observations:
                continue
            score = self._score_handoff(ts_a, ts_b, p)
            if score is None:
                continue
            if best is None or score > best:
                best = score
        return best

    @staticmethod
    def _concurrently_observed(
        ts_a: list[float], ts_b: list[float], rotation_s: float
    ) -> bool:
        """True if any pair of timestamps from a, b is within rotation_s."""
        if not ts_a or not ts_b:
            return False
        i = j = 0
        ts_a = sorted(ts_a)
        ts_b = sorted(ts_b)
        while i < len(ts_a) and j < len(ts_b):
            d = ts_a[i] - ts_b[j]
            if abs(d) < rotation_s:
                return True
            if d < 0:
                i += 1
            else:
                j += 1
        return False

    @staticmethod
    def _score_handoff(
        ts_a: list[float], ts_b: list[float], p: _Params
    ) -> float | None:
        """Score the best handoff between sequence a and sequence b.

        We allow either direction (a→b or b→a) since the labels are
        arbitrary. Take the smallest forward gap that lands in the
        plausible window; score it by proximity to expected rotation.
        """
        last_a = max(ts_a)
        first_b = min(ts_b)
        last_b = max(ts_b)
        first_a = min(ts_a)

        candidates = []
        # a-then-b handoff
        gap_ab = first_b - last_a
        if gap_ab > 0:
            candidates.append(gap_ab)
        # b-then-a handoff
        gap_ba = first_a - last_b
        if gap_ba > 0:
            candidates.append(gap_ba)

        if not candidates:
            return None

        best_gap = min(candidates)

        if best_gap > p.window_max:
            return 0.0
        if best_gap < p.window_min:
            return 0.5

        delta = abs(best_gap - p.expected_rotation)
        return max(0.0, 1.0 - delta / p.expected_rotation)
