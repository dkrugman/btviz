"""continuity_seq_carryover signal.

Implements the address-rotation-defeating sequence-number tracking
described in Martin et al, "Handoff All Your Privacy" §4.2 (2019),
specifically the Handoff (TLV 0x0C) 2-byte sequence number.

The seq is cleartext (bytes 1-2 of the Handoff payload after the
1-byte clipboard status). It increments on user actions —
opening/closing a Handoff-enabled app, unlocking, rebooting — and
**does not reset across MAC address rotation**. So if device A's
last-seen Handoff seq is 1234 and device B's first-seen Handoff
seq is 1235 within a small time window, A and B are with very high
probability the same physical device.

Output domain:
  * None    one or both devices have no Handoff observations → abstain
  *  1.0    seq carry-over within tolerance — effectively-certain merge
  *  0.6    seq values are close (within ``max_seq_gap``) but not
            adjacent — likely missed packets at the rotation boundary
  *  0.0    seq distances suggest different devices

Output is intentionally absent (None) most of the time. Many
captures contain few or no Handoff observations, since Handoff
emits only on user actions, not continuously. When the signal
*does* fire it's near-deterministic, which is exactly the
counterweight needed to ``co_lifespan_match``'s overconfidence on
short captures.

Per-signal enable/disable is in the user's preferences
(``cluster.signals.continuity_seq_carryover``); when disabled the
signal isn't loaded into the runner at all. Default: enabled.
"""

from __future__ import annotations

from typing import Any, Mapping

from ..base import ClusterContext, Device
from ._continuity_protocol import APPLE_CID_BE, extract_handoff_seq

AD_TYPE_MFG = 0xFF


# How close two seq values must be (in absolute increment count) to
# count as "same device with possible packet loss." Per Martin et al
# Fig 9, daily increment rates were 275–630 across users; on a
# minute-scale capture, a few-unit gap is easily explained by a
# missed handoff event between rotation observations. Anything
# beyond ~50 starts to be a different device's lower-frequency
# trajectory crossing through.
DEFAULT_MAX_SEQ_GAP = 5

# How far apart in time two Handoff observations can be and still
# count as a candidate carry-over. Bounds the search window: we're
# trying to link rotations that happened minutes-to-hours apart,
# not days. Days-of-separation tracking (per the paper's §5) is a
# different use case.
DEFAULT_MAX_DT_S = 600.0


def _device_seq_observations(
    db, device_id: int,
) -> list[tuple[float, float, int]]:
    """Return per-device list of ``(first_seen, last_seen, seq)``.

    Each row in ``device_ad_history`` is one distinct mfg-data blob
    from this device. We extract the Handoff seq when present and
    keep the temporal window the blob was observed in.

    Sorted by ``first_seen`` ascending.
    """
    rows = db.execute(
        "SELECT ad_value, first_seen, last_seen FROM device_ad_history"
        " WHERE device_id = ? AND ad_type = ?",
        (device_id, AD_TYPE_MFG),
    ).fetchall()
    out: list[tuple[float, float, int]] = []
    for r in rows:
        # sqlite3.Row supports both index and name access; tuples
        # only the former. Stay agnostic.
        if isinstance(r, (tuple, list)):
            blob, first_seen, last_seen = r[0], r[1], r[2]
        else:
            blob = r["ad_value"]
            first_seen = r["first_seen"]
            last_seen = r["last_seen"]
        if not blob or len(blob) < 4 or blob[:2] != APPLE_CID_BE:
            continue
        seq = extract_handoff_seq(bytes(blob))
        if seq is None:
            continue
        out.append((float(first_seen), float(last_seen), int(seq)))
    out.sort(key=lambda x: x[0])
    return out


class ContinuitySeqCarryover:
    """Cluster signal: Handoff sequence-number carry-over across rotations."""

    name = "continuity_seq_carryover"

    def applies_to(self, ctx: ClusterContext, a: Device, b: Device) -> bool:
        # Need DB access to read device_ad_history. Apple devices
        # only — the signal is meaningless for non-Apple traffic.
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
        max_seq_gap = int(params.get("max_seq_gap", DEFAULT_MAX_SEQ_GAP))
        max_dt_s = float(params.get("max_dt_s", DEFAULT_MAX_DT_S))

        a_obs = _device_seq_observations(ctx.db.conn, a.id)
        b_obs = _device_seq_observations(ctx.db.conn, b.id)
        if not a_obs or not b_obs:
            return None

        return _carryover_score(a_obs, b_obs, max_seq_gap, max_dt_s)


def _carryover_score(
    a_obs: list[tuple[float, float, int]],
    b_obs: list[tuple[float, float, int]],
    max_seq_gap: int,
    max_dt_s: float,
) -> float:
    """Best-pair carry-over score between two devices' seq trajectories.

    Walk every (a_obs, b_obs) pair where their observation windows
    are within ``max_dt_s``. Pick the best score:

      * exact carry-over (|Δseq| == 1) → 1.0
      * |Δseq| within ``max_seq_gap`` → 0.6
      * otherwise → 0.0

    Direction is symmetric: both A→B and B→A handoffs are linkable;
    the signal isn't trying to determine which RPA was first.
    """
    best = 0.0
    for a_first, a_last, a_seq in a_obs:
        for b_first, b_last, b_seq in b_obs:
            # Time gap between the two windows. Use the tightest
            # available bound: if A ended before B started, the gap
            # is (b_first - a_last); vice versa for the reverse.
            if a_last < b_first:
                dt = b_first - a_last
            elif b_last < a_first:
                dt = a_first - b_last
            else:
                dt = 0.0   # windows overlap
            if dt > max_dt_s:
                continue
            d = abs(a_seq - b_seq)
            if d == 1:
                return 1.0   # short-circuit: exact carry-over
            if d <= max_seq_gap:
                best = max(best, 0.6)
    return best
