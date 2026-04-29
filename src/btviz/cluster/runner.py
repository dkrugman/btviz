"""Cluster runner: orchestrate cluster_pair across candidate device pairs.

This module owns the run-level narration (human-readable INFO lines)
and per-pair decision emission (JSON-per-line). It is deliberately
data-source-agnostic: ``run_once(devices)`` takes a sequence of
Device objects and a candidate-generation function. The DB-backed
caller and the synthetic-data tests both go through here.

Transitive closure (union-find over merge edges) is applied at the
end of each run, so the summary counts reflect the *cluster* count
not the *edge* count — three RPAs collapsing into one cluster show
as "3 → 1" not "3 → 0". Persistence to ``device_clusters`` is TODO
and lands with the schema migration PR.
"""

from __future__ import annotations

import json
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable, Iterable, Sequence

from .aggregator import cluster_pair_with_reason
from .base import ClusterContext, Decision, Device
from .cluster_log import get_cluster_logger

log = get_cluster_logger()

CandidateFn = Callable[[Sequence[Device]], Iterable[tuple[Device, Device]]]


@dataclass
class RunResult:
    """Outcome of a single ``run_once`` call.

    ``merges_by_class`` counts merge *edges* (pair decisions). For a
    user-facing "how many clusters did we collapse into" number, use
    ``clusters_by_class`` (post-union-find), or ``cluster_count`` for
    the total.

    ``abstain_reasons_by_class`` is the per-class breakdown of *why*
    pairs abstained — surfaces things like "1485 apple_device pairs
    abstained on below_min_total_weight:0.55/0.60" so it's clear when
    the issue is profile config vs. signal output.
    """

    elapsed_s: float
    devices_in: int
    pairs_evaluated: int
    merge_decisions: list[tuple[int, int, Decision]] = field(default_factory=list)
    abstain_count: int = 0
    no_merge_count: int = 0
    by_class: Counter[str] = field(default_factory=Counter)
    merges_by_class: Counter[str] = field(default_factory=Counter)
    clusters_by_class: Counter[str] = field(default_factory=Counter)
    cluster_count: int = 0
    abstain_reasons_by_class: dict[str, Counter[str]] = field(
        default_factory=dict,
    )


class ClusterRunner:
    """Run the aggregator across candidate device pairs.

    The candidate-generation function is injected so the cheap
    pre-filter (same class + recent overlap + bucket hash) can be
    swapped without touching the runner. Default: all-pairs within
    the same device_class.
    """

    def __init__(
        self,
        ctx: ClusterContext,
        candidates: CandidateFn | None = None,
    ) -> None:
        self.ctx = ctx
        self.candidates = candidates or _same_class_pairs

    def run_once(self, devices: Sequence[Device]) -> RunResult:
        start = time.monotonic()
        by_class: Counter[str] = Counter(d.device_class for d in devices)
        log.info(
            "cluster analysis starting — %d devices, %d classes",
            len(devices),
            len(by_class),
        )

        result = RunResult(
            elapsed_s=0.0,
            devices_in=len(devices),
            pairs_evaluated=0,
            by_class=by_class,
        )

        # Per-class narration: emit one line as we move into each
        # class so the log reads like a story.
        pairs_by_class: dict[str, list[tuple[Device, Device]]] = {}
        for a, b in self.candidates(devices):
            pairs_by_class.setdefault(a.device_class, []).append((a, b))

        for cls, pairs in pairs_by_class.items():
            log.info(
                "analyzing %d %s%s (%d candidate pairs)",
                by_class[cls],
                cls,
                "s" if by_class[cls] != 1 else "",
                len(pairs),
            )
            for a, b in pairs:
                self._evaluate(a, b, result)

        self._compute_closure(devices, result)
        result.elapsed_s = time.monotonic() - start
        log.info(
            "cluster analysis complete (%.2fs)\n%s",
            result.elapsed_s,
            self._format_summary(result),
        )
        return result

    def _compute_closure(
        self, devices: Sequence[Device], result: RunResult
    ) -> None:
        """Union-find over merge edges → counts of clusters per class.

        Devices with no merge edges are 1-element clusters and still
        count toward ``clusters_by_class``.
        """
        parent: dict[int, int] = {d.id: d.id for d in devices}

        def find(x: int) -> int:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(x: int, y: int) -> None:
            rx, ry = find(x), find(y)
            if rx != ry:
                parent[rx] = ry

        for a_id, b_id, _ in result.merge_decisions:
            if a_id in parent and b_id in parent:
                union(a_id, b_id)

        cluster_classes: dict[int, str] = {}
        for d in devices:
            cluster_classes[find(d.id)] = d.device_class

        result.cluster_count = len(cluster_classes)
        result.clusters_by_class = Counter(cluster_classes.values())

    def _evaluate(
        self, a: Device, b: Device, result: RunResult
    ) -> None:
        result.pairs_evaluated += 1
        decision, abstain_reason, contribs = cluster_pair_with_reason(
            self.ctx, a, b,
        )

        if decision is None:
            result.abstain_count += 1
            if abstain_reason:
                bucket = result.abstain_reasons_by_class.setdefault(
                    a.device_class, Counter(),
                )
                bucket[abstain_reason] += 1
            # Per-pair abstain detail at DEBUG so the user can opt in
            # to "tell me exactly what each pair scored" without
            # drowning the default INFO log. Volume is O(n²) per class;
            # a 50-device run with 4 classes can easily produce
            # thousands of these lines.
            log.debug(
                "abstain %s %s vs %s (%s%s)",
                a.device_class,
                _device_ref(a),
                _device_ref(b),
                abstain_reason or "no_reason",
                _format_contribs(contribs),
            )
            return

        log.info("decision %s", _decision_json(a, b, decision))

        if decision.merge:
            result.merge_decisions.append((a.id, b.id, decision))
            result.merges_by_class[a.device_class] += 1
            log.info(
                "merge %s %s ← %s (score %.2f)",
                a.device_class,
                _device_ref(b),
                _device_ref(a),
                decision.score,
            )
        else:
            result.no_merge_count += 1
            reason = (
                f", abort: {decision.abort_reason}"
                if decision.abort_reason
                else ""
            )
            log.info(
                "no-merge %s %s vs %s (score %.2f%s)",
                a.device_class,
                _device_ref(a),
                _device_ref(b),
                decision.score,
                reason,
            )

    def _format_summary(self, r: RunResult) -> str:
        indent = " " * 21
        lines: list[str] = []
        absorbed = r.devices_in - r.cluster_count
        lines.append(
            f"{indent}{r.devices_in} → {r.cluster_count} devices "
            f"({absorbed} merged)"
        )
        if not r.by_class:
            return "\n".join(lines)
        width = max(len(c) for c in r.by_class)
        for cls, count in r.by_class.most_common():
            after = r.clusters_by_class.get(cls, count)
            tag = "" if after != count else " (unchanged)"
            lines.append(
                f"{indent}{count:>4} {cls:<{width}}  → {after:>4}{tag}"
            )

        # Abstain breakdown — when a class is "unchanged" it's almost
        # always because every pair abstained for the same reason; show
        # which one so the user can tell config gaps from data gaps.
        if r.abstain_reasons_by_class:
            lines.append(f"{indent}abstains:")
            for cls in r.by_class:
                reasons = r.abstain_reasons_by_class.get(cls)
                if not reasons:
                    continue
                for reason, count in reasons.most_common():
                    lines.append(
                        f"{indent}  {count:>4} {cls:<{width}}  {reason}"
                    )
        return "\n".join(lines)


def _same_class_pairs(
    devices: Sequence[Device],
) -> Iterable[tuple[Device, Device]]:
    """Default candidate generator: every same-class pair, no de-dup."""
    by_class: dict[str, list[Device]] = {}
    for d in devices:
        by_class.setdefault(d.device_class, []).append(d)
    for group in by_class.values():
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                yield group[i], group[j]


def _addr(d: Device) -> str:
    return ":".join(f"{b:02x}" for b in d.address.bytes_)


def _device_ref(d: Device) -> str:
    """Stable-id-first reference suitable for human-readable lines."""
    return f"device_{d.id} ({_addr(d)})"


def _format_contribs(
    contribs: dict[str, tuple[float, float]] | None,
) -> str:
    """Render contributions as ", signals: name=score×weight, …" or "".

    Empty contribs (no signal had an opinion) returns an empty string
    so the caller's format string degrades cleanly.
    """
    if not contribs:
        return ""
    parts = ", ".join(
        f"{name}={s:.2f}×{w:.2f}" for name, (s, w) in contribs.items()
    )
    return f", signals: {parts}"


def _decision_json(a: Device, b: Device, decision: Decision) -> str:
    payload = {
        "a": a.id,
        "b": b.id,
        "a_addr": _addr(a),
        "b_addr": _addr(b),
        "profile": decision.profile,
        "merge": decision.merge,
        "score": round(decision.score, 4),
        "abort_reason": decision.abort_reason,
        "signals": {
            name: [round(s, 4), round(w, 4)]
            for name, (s, w) in decision.signals.items()
        },
    }
    return json.dumps(payload, separators=(",", ":"))
