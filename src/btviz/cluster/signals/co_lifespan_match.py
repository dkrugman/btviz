"""co_lifespan_match signal.

Scores two devices by the alignment of their per-session observation
windows. Catches two patterns the user identified as "very strong":

  * **Co-emission.** Same physical device emits two RPAs in parallel
    (e.g. legacy + extended advertising, or two distinct adv sets).
    Both windows nearly identical → near-certainty same-device.

  * **Rotation handoff.** A's window ends and B's begins within a
    plausible rotation gap, with comparable durations. → strong
    same-device.

Reads from the ``observations`` table (per (session, device)
``first_seen`` / ``last_seen``) — NOT the ``devices`` table's
all-time fields, which would conflate separate sessions. Does NOT
need ``packets`` populated; observation rows are written for every
device-session pairing regardless of ``keep_packets``, so this
signal works on a fresh capture immediately.

The signal is designed to be a "decisive" one in the aggregator's
new pathway: a ≥ 0.95 score plus no contradicting evidence merges
on its own, without needing other signals to also fire.

Output domain:
  * None     no common session, or windows too disjoint → abstain
  *  0.95+   near-identical concurrent windows OR near-instant
             handoff → strong same-device
  *  0.40    significant overlap but not near-identical → weak
             same-class hint
  *  0.0+    weak overlap or distant-but-still-plausible handoff
"""

from __future__ import annotations

from typing import Any, Mapping

from ..base import ClusterContext, Device


# Defaults tuned for the patterns observed in the user's session 93:
# - Co-emission pairs (16322/16323 apple_watch; 16333/16334 apple_device)
#   have window-overlap ratios > 99 %. Threshold of 0.90 leaves room
#   for clock skew or a slightly-late-starting second emission.
# - The airtag rotation pair (16324 → 16335) had a near-zero gap; we
#   want gap < 5 s to land at the high-confidence ceiling.
_DEFAULT_MIN_OVERLAP_PCT = 0.90
_DEFAULT_MAX_HANDOFF_GAP_S = 60.0
_DEFAULT_NEAR_INSTANT_GAP_S = 5.0


def _common_sessions(db, did_a: int, did_b: int) -> list[int]:
    """Return session_ids that observed both devices (any order)."""
    rows = db.conn.execute(
        "SELECT DISTINCT a.session_id FROM observations a"
        " JOIN observations b ON a.session_id = b.session_id"
        " WHERE a.device_id = ? AND b.device_id = ?",
        (did_a, did_b),
    ).fetchall()
    out: list[int] = []
    for r in rows:
        sid = r["session_id"] if not isinstance(r, (tuple, list)) else r[0]
        out.append(sid)
    return out


def _session_window(
    db, device_id: int, session_id: int,
) -> tuple[float, float] | None:
    """Return (first_seen, last_seen) for the device in the given session."""
    row = db.conn.execute(
        "SELECT first_seen, last_seen FROM observations"
        " WHERE device_id = ? AND session_id = ?",
        (device_id, session_id),
    ).fetchone()
    if row is None:
        return None
    if isinstance(row, (tuple, list)):
        return (float(row[0]), float(row[1]))
    return (float(row["first_seen"]), float(row["last_seen"]))


class CoLifespanMatch:
    """Cluster signal: temporal-window alignment in a shared session."""

    name = "co_lifespan_match"

    def applies_to(
        self, ctx: ClusterContext, a: Device, b: Device,
    ) -> bool:
        return ctx.db is not None

    def score(
        self,
        ctx: ClusterContext,
        a: Device,
        b: Device,
        params: Mapping[str, Any] | None = None,
    ) -> float | None:
        if ctx.db is None:
            return None
        params = params or {}
        min_overlap_pct = float(params.get(
            "min_overlap_pct", _DEFAULT_MIN_OVERLAP_PCT,
        ))
        max_handoff_gap = float(params.get(
            "max_handoff_gap_s", _DEFAULT_MAX_HANDOFF_GAP_S,
        ))
        near_instant_gap = float(params.get(
            "near_instant_gap_s", _DEFAULT_NEAR_INSTANT_GAP_S,
        ))

        sessions = _common_sessions(ctx.db, a.id, b.id)
        if not sessions:
            return None

        best: float | None = None
        for sid in sessions:
            wa = _session_window(ctx.db, a.id, sid)
            wb = _session_window(ctx.db, b.id, sid)
            if wa is None or wb is None:
                continue
            sc = _score_window_pair(
                wa, wb,
                min_overlap_pct=min_overlap_pct,
                max_handoff_gap=max_handoff_gap,
                near_instant_gap=near_instant_gap,
            )
            if sc is None:
                continue
            if best is None or sc > best:
                best = sc
        return best


def _score_window_pair(
    a: tuple[float, float],
    b: tuple[float, float],
    *,
    min_overlap_pct: float,
    max_handoff_gap: float,
    near_instant_gap: float,
) -> float | None:
    """Score one (session, device-pair) window alignment.

    Concurrent windows are scored by Jaccard-style overlap ratio;
    disjoint windows are scored by handoff-gap proximity. Anything
    farther apart than ``max_handoff_gap`` returns None so the
    aggregator treats this session as silent rather than negative.
    """
    af, al = a
    bf, bl = b
    # Concurrent (overlap with non-zero duration). A zero-width touch
    # at a single instant (al == bf) is treated as a handoff, not as
    # concurrent — that's the strongest rotation signal we have and
    # it would otherwise score 0.0 from a 0/union ratio.
    overlap_lo = max(af, bf)
    overlap_hi = min(al, bl)
    if overlap_lo < overlap_hi:
        overlap = overlap_hi - overlap_lo
        union = max(al, bl) - min(af, bf)
        if union <= 0:
            return None
        pct = overlap / union
        if pct >= min_overlap_pct:
            return 0.95
        if pct >= 0.5:
            return 0.4
        return 0.0

    # Disjoint — rotation-handoff candidate. Order so a is the earlier.
    if af > bf:
        af, al, bf, bl = bf, bl, af, al
    gap = bf - al
    if gap > max_handoff_gap:
        return None
    if gap <= near_instant_gap:
        return 0.95
    # Linear decay from 0.95 at the near-instant cutoff to 0.45 at
    # max_handoff_gap. The slope is gentle on purpose — a 30-second
    # gap with comparable durations is still pretty good evidence.
    span = max_handoff_gap - near_instant_gap
    if span <= 0:
        return 0.95
    return 0.95 - 0.5 * ((gap - near_instant_gap) / span)
