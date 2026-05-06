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
    # Peak score sits at this gap; score decays linearly as the
    # observed gap moves away from it. Defaults to 900 s (15 min,
    # the typical RPA rotation cadence for AirTags / iPhones).
    expected_rotation: float = 900.0
    # Smallest gap we'll score. Below this, the handoff is so fast
    # it's "suspiciously instantaneous" — score 0.5 (moderate).
    window_min: float = 0.05
    # Largest gap we'll score. Beyond this, the gap is too long for
    # a plausible same-device handoff regardless of cadence — score
    # 0.0. Default 1800 s (30 min) so typical 5-15 min rotations
    # land near the 900 s peak without being clipped.
    window_max: float = 1800.0
    # Concurrent-existence rejection: how much the two devices'
    # observation windows are allowed to overlap before treating
    # them as "both alive at once" (and thus impossible to be one
    # device handing off identity). Default 0 — strict overlap is
    # rejection. Tunable upward to absorb ~1 s of measurement jitter
    # if RPA rotation isn't atomic in your data.
    overlap_slack: float = 0.0
    min_observations: int = 1


def _params(raw: Mapping[str, Any] | None) -> _Params:
    raw = raw or {}
    return _Params(
        expected_rotation=float(raw.get("expected_rotation", 900.0)),
        window_min=float(raw.get("window_min", 0.05)),
        window_max=float(raw.get("window_max", 1800.0)),
        overlap_slack=float(raw.get("overlap_slack", 0.0)),
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

        # Concurrent-existence rejection. If the two devices' emission
        # windows overlap on the same sniffer, they were both alive at
        # once and cannot be one device handing off identity. The
        # previous formulation used ``expected_rotation`` as a
        # "nearest-pair distance" threshold, which conflated the
        # rotation cadence with the overlap-detection radius and made
        # any sub-rotation handoff look concurrent — fixed by switching
        # to true window-overlap semantics.
        for s in common:
            if self._concurrently_observed(
                obs_a[s], obs_b[s], p.overlap_slack,
            ):
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
        ts_a: list[float], ts_b: list[float], slack_s: float,
    ) -> bool:
        """True if a's and b's emission windows overlap by more than
        ``slack_s`` seconds on this sniffer.

        Each device's "alive window" is ``[min(ts), max(ts)]``. They're
        concurrent iff those windows intersect. Overlap is computed as
        ``min(last_a, last_b) - max(first_a, first_b)``; a value > 0
        means the windows intersected for that many seconds.

        ``slack_s`` lets a small measured overlap still count as
        handoff (RPA rotation isn't atomic — adjacent windows can
        bleed into each other by a fraction of a second). Default 0
        treats any overlap as rejection.
        """
        if not ts_a or not ts_b:
            return False
        first_a, last_a = min(ts_a), max(ts_a)
        first_b, last_b = min(ts_b), max(ts_b)
        overlap = min(last_a, last_b) - max(first_a, first_b)
        return overlap > slack_s

    @staticmethod
    def _score_handoff(
        ts_a: list[float], ts_b: list[float], p: _Params
    ) -> float | None:
        """Score the best handoff between sequence a and sequence b.

        We allow either direction (a→b or b→a) since the labels are
        arbitrary. Take the smallest forward gap (last-of-one to
        first-of-the-other) and score by proximity to ``expected_rotation``:

          * gap > ``window_max``                    → 0.0  (too long for plausible handoff)
          * gap < ``window_min``                    → 0.5  (suspiciously instantaneous)
          * otherwise: 1.0 − |gap − expected| / expected
            (peaks at 1.0 when gap == expected, decays linearly)

        ``window_max`` should be large enough to span the expected
        rotation cadence (default 1800 s for a 900 s peak). The
        previous default of 60 s clipped the score formula's peak
        region entirely and made the signal unable to score real
        rotations — fixed in this revision.
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
            # No positive forward gap means the emission windows
            # overlap. The caller's concurrent-check has already
            # verified the overlap is within ``overlap_slack`` (else
            # we'd have returned -1.0 before getting here), so this
            # is jitter at the rotation boundary. Treat as a
            # zero-gap handoff — the window_min branch picks it up
            # as "suspiciously instantaneous" (score 0.5).
            best_gap = 0.0
        else:
            best_gap = min(candidates)

        if best_gap > p.window_max:
            return 0.0
        if best_gap < p.window_min:
            return 0.5

        delta = abs(best_gap - p.expected_rotation)
        return max(0.0, 1.0 - delta / p.expected_rotation)
