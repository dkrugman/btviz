"""rssi_signature signal.

Two RPAs from the same physical device, observed concurrently by the
same sniffer, will have near-identical RSSI distributions — they're
the same radio at the same distance from the same antenna. Two
distinct devices, even of the same class, sit at different positions
and will have different distributions.

The signal computes a per-sniffer mean RSSI for each device over a
recent window and compares the means via a z-score scaled by the
combined sample stddev. Z-score is converted to ``[0, 1]`` linearly
between "identical" and ``z_full_mismatch``, then averaged across
shared sniffers (a single matching sniffer is too easy to coincide;
the profile's ``min_sniffers`` gates it).

Reads from the ``packets`` table (requires capture to have run with
``keep_packets`` ON, which has been the default since PR #94's
"Record packets" toolbar toggle landed). Devices without packet
history abstain via ``applies_to``.

Output domain:
  *  None              no shared sniffer with enough recent packets
  *  0.0..1.0          mean per-sniffer agreement score
  *  ~1.0              identical means within stddev → likely same device
  *  ~0.0              means apart by ``z_full_mismatch`` σ or more

Defaults (overridable per-profile):
  * ``min_sniffers``     = 2     — agreement on one sniffer is weak
                                   evidence; require corroboration
  * ``std_floor``        = 1.5   — floor for combined stddev so that
                                   measurement quantization (1 dBm
                                   resolution typical) doesn't divide
                                   by near-zero
  * ``z_full_mismatch``  = 4.0   — 4σ separation → score 0.0
  * ``recent_window``    = 30.0s — only consider packets within this
                                   window of each device's most-recent
                                   packet ts, on a per-sniffer basis,
                                   so stale RSSI data doesn't poison
                                   the mean
  * ``min_packets_per_sniffer`` = 3 — a single sample is noise
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping

from ..base import ClusterContext, Device


@dataclass(frozen=True)
class _Params:
    min_sniffers: int = 2
    std_floor: float = 1.5
    z_full_mismatch: float = 4.0
    recent_window: float = 30.0
    min_packets_per_sniffer: int = 3


def _params(raw: Mapping[str, Any] | None) -> _Params:
    raw = raw or {}
    return _Params(
        min_sniffers=int(raw.get("min_sniffers", 2)),
        std_floor=float(raw.get("std_floor", 1.5)),
        z_full_mismatch=float(raw.get("z_full_mismatch", 4.0)),
        recent_window=float(raw.get("recent_window", 30.0)),
        min_packets_per_sniffer=int(raw.get("min_packets_per_sniffer", 3)),
    )


@dataclass(frozen=True)
class _PerSniffer:
    """Per-sniffer windowed-RSSI summary for one device."""
    mean: float
    std: float
    n: int


def _load_packets(
    ctx: ClusterContext, device: Device,
) -> dict[int, list[tuple[float, int]]]:
    """Return ``{sniffer_id: [(ts, rssi), ...]}`` for the device.

    Param-independent — windowing and min-sample filtering happen
    later inside ``score()`` so profile overrides aren't shadowed by
    a cache built with another profile's params. ``ctx.cache`` keyed
    on the device id mirrors how ``rotation_cohort`` does it; the
    O(N²) pair loop reads each device's packet rows once per run.
    """
    cache = ctx.cache.setdefault("rssi_packets", {})
    cached = cache.get(device.id)
    if cached is not None:
        return cached
    if ctx.db is None:
        cache[device.id] = {}
        return {}

    rows = ctx.db.conn.execute(
        "SELECT sniffer_id, ts, rssi FROM packets"
        " WHERE device_id = ? AND sniffer_id IS NOT NULL"
        " AND rssi IS NOT NULL",
        (device.id,),
    ).fetchall()

    out: dict[int, list[tuple[float, int]]] = {}
    for r in rows:
        sid = r["sniffer_id"] if not isinstance(r, (tuple, list)) else r[0]
        ts = r["ts"] if not isinstance(r, (tuple, list)) else r[1]
        rssi = r["rssi"] if not isinstance(r, (tuple, list)) else r[2]
        out.setdefault(sid, []).append((float(ts), int(rssi)))
    cache[device.id] = out
    return out


def _windowed_stats(
    by_sniffer: dict[int, list[tuple[float, int]]], p: _Params,
) -> dict[int, _PerSniffer]:
    """Apply the recent-window and min-sample filters per sniffer.

    Per-sniffer "now" anchoring (latest ts on that sniffer, not a
    global one) handles dongles whose firmware clocks have drifted
    apart — same discipline as the canvas's session-scoped staleness
    cutoff.
    """
    out: dict[int, _PerSniffer] = {}
    for sid, packets in by_sniffer.items():
        if not packets:
            continue
        latest = max(t for t, _ in packets)
        cutoff = latest - p.recent_window
        recent = [r for t, r in packets if t >= cutoff]
        if len(recent) < p.min_packets_per_sniffer:
            continue
        n = len(recent)
        mean = sum(recent) / n
        # Population stddev — treat the sample as the population
        # for what we're measuring (RSSI fluctuation around the
        # device's true mean on this sniffer).
        var = sum((x - mean) ** 2 for x in recent) / n
        out[sid] = _PerSniffer(mean=mean, std=math.sqrt(var), n=n)
    return out


def _agreement_score(
    a: _PerSniffer, b: _PerSniffer, p: _Params,
) -> float:
    """Per-sniffer agreement score in ``[0, 1]``.

    Combined stddev is the pooled standard deviation, with a floor so
    that two devices reporting identical means at perfect resolution
    don't divide by zero. The z-score is the |mean delta| in
    combined-stddev units; we map ``[0, z_full_mismatch]`` linearly to
    ``[1, 0]`` and clamp.
    """
    pooled_var = (a.std ** 2 + b.std ** 2) / 2.0
    pooled_std = max(math.sqrt(pooled_var), p.std_floor)
    z = abs(a.mean - b.mean) / pooled_std
    if z >= p.z_full_mismatch:
        return 0.0
    return max(0.0, 1.0 - z / p.z_full_mismatch)


class RssiSignature:
    name = "rssi_signature"

    def applies_to(
        self, ctx: ClusterContext, a: Device, b: Device,
    ) -> bool:
        # Cheap existence check — does each device have at least one
        # packet row with a non-NULL sniffer_id, and do their sniffer
        # sets overlap? Avoids materializing per-sniffer stats here
        # because ``score()`` may run with profile-overridden params
        # (recent_window, min_packets_per_sniffer) that would change
        # which sniffers qualify; computing once with defaults and
        # caching would let the cache shadow those overrides.
        if ctx.db is None:
            return False
        cache = ctx.cache.setdefault("rssi_sniffers", {})

        def _sniffer_set(dev_id: int) -> set[int]:
            cached = cache.get(dev_id)
            if cached is not None:
                return cached
            rows = ctx.db.conn.execute(
                "SELECT DISTINCT sniffer_id FROM packets"
                " WHERE device_id = ? AND sniffer_id IS NOT NULL"
                " AND rssi IS NOT NULL",
                (dev_id,),
            ).fetchall()
            sids = set()
            for r in rows:
                sid = r["sniffer_id"] if not isinstance(r, (tuple, list)) else r[0]
                sids.add(sid)
            cache[dev_id] = sids
            return sids

        return bool(_sniffer_set(a.id) & _sniffer_set(b.id))

    def score(
        self,
        ctx: ClusterContext,
        a: Device,
        b: Device,
        params: Mapping[str, Any] | None = None,
    ) -> float | None:
        p = _params(params)
        stats_a = _windowed_stats(_load_packets(ctx, a), p)
        stats_b = _windowed_stats(_load_packets(ctx, b), p)
        shared = set(stats_a) & set(stats_b)
        if len(shared) < p.min_sniffers:
            return None
        per_sniffer = [
            _agreement_score(stats_a[s], stats_b[s], p) for s in shared
        ]
        return sum(per_sniffer) / len(per_sniffer)
