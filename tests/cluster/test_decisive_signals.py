"""Tests for the decisive-signal pathway in the aggregator.

A signal listed in a profile's ``decisive_signals`` that scores at or
above ``decisive_threshold`` short-circuits the merge — bypassing
``min_total_weight`` and the weighted-sum threshold — provided no
signal returns below ``negative_block_threshold``.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any, Mapping

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from btviz.cluster.aggregator import cluster_pair_with_reason  # noqa: E402
from btviz.cluster.base import (  # noqa: E402
    Address, ClassProfile, ClusterContext, Device,
)


class _FixedSignal:
    """Test double: returns a configured score for any pair."""
    def __init__(self, name: str, score: float | None) -> None:
        self.name = name
        self._score = score

    def applies_to(self, ctx, a, b):
        return True

    def score(self, ctx, a, b, params: Mapping[str, Any] | None = None):
        return self._score


def _device(did: int, kind: str = "test") -> Device:
    return Device(
        id=did,
        device_class=kind,
        address=Address(bytes_=b"\x00" * 6, kind="random_resolvable"),
        first_seen=0.0,
        last_seen=0.0,
    )


def _profile(
    *,
    decisive: list[str] | None = None,
    decisive_threshold: float = 0.95,
    negative_block: float = -0.3,
    weights: dict[str, float] | None = None,
    threshold: float = 0.95,            # high so non-decisive paths don't merge
    min_total_weight: float = 0.99,     # high so summed-weight path is unreachable
) -> ClassProfile:
    return ClassProfile(
        name="test",
        weights=weights or {"decisive": 0.10, "supporting": 0.10},
        threshold=threshold,
        min_total_weight=min_total_weight,
        decisive_signals=frozenset(decisive or []),
        decisive_threshold=decisive_threshold,
        negative_block_threshold=negative_block,
    )


def _ctx(profile: ClassProfile, signals: dict) -> ClusterContext:
    return ClusterContext(
        signals=signals, profiles={"test": profile}, now=0.0,
    )


class DecisiveSignalAggregatorTests(unittest.TestCase):

    def test_decisive_signal_above_threshold_merges_alone(self):
        profile = _profile(decisive=["decisive"])
        ctx = _ctx(profile, {
            "decisive": _FixedSignal("decisive", 0.96),
            "supporting": _FixedSignal("supporting", 0.0),
        })
        decision, reason, _ = cluster_pair_with_reason(
            ctx, _device(1), _device(2),
        )
        self.assertIsNotNone(decision)
        self.assertTrue(decision.merge)
        self.assertGreaterEqual(decision.score, 0.95)

    def test_decisive_signal_below_threshold_does_not_short_circuit(self):
        profile = _profile(decisive=["decisive"])
        ctx = _ctx(profile, {
            "decisive": _FixedSignal("decisive", 0.94),  # just below
            "supporting": _FixedSignal("supporting", 0.0),
        })
        decision, reason, _ = cluster_pair_with_reason(
            ctx, _device(1), _device(2),
        )
        # Falls through to the weight-sum path, which can't clear
        # min_total_weight → abstain.
        self.assertIsNone(decision)
        self.assertIsNotNone(reason)
        self.assertIn("below_min_total_weight", reason)

    def test_strong_negative_blocks_decisive_short_circuit(self):
        # A decisive signal scores 0.99 — but another signal returns
        # -0.4 (below the -0.3 negative-block threshold). The merge
        # must NOT fire; the user's "no conflicts" rule.
        # NOTE: ``conflict`` must be in the profile's weights for the
        # aggregator to consult it — only profile-listed signals
        # contribute to the contributions dict the block check reads.
        profile = _profile(
            decisive=["decisive"],
            weights={"decisive": 0.10, "conflict": 0.10},
        )
        ctx = _ctx(profile, {
            "decisive": _FixedSignal("decisive", 0.99),
            "conflict": _FixedSignal("conflict", -0.4),
        })
        decision, reason, _ = cluster_pair_with_reason(
            ctx, _device(1), _device(2),
        )
        self.assertIsNone(decision)

    def test_mild_negative_does_not_block(self):
        # A negative within the noise band (-0.2, above the -0.3
        # block threshold) should NOT block the decisive pathway.
        profile = _profile(
            decisive=["decisive"],
            weights={"decisive": 0.10, "weak_neg": 0.10},
        )
        ctx = _ctx(profile, {
            "decisive": _FixedSignal("decisive", 0.99),
            "weak_neg": _FixedSignal("weak_neg", -0.2),
        })
        decision, _, _ = cluster_pair_with_reason(
            ctx, _device(1), _device(2),
        )
        self.assertIsNotNone(decision)
        self.assertTrue(decision.merge)

    def test_signal_not_in_decisive_list_does_not_short_circuit(self):
        # The decisive-eligible signal scores low; a non-listed signal
        # scores 1.0. No short circuit — fall through to weight sum.
        profile = _profile(decisive=["decisive"])
        ctx = _ctx(profile, {
            "decisive": _FixedSignal("decisive", 0.5),
            "other": _FixedSignal("other", 1.0),
        })
        decision, reason, _ = cluster_pair_with_reason(
            ctx, _device(1), _device(2),
        )
        self.assertIsNone(decision)
        self.assertIn("below_min_total_weight", reason)

    def test_decisive_short_circuit_overrides_missing_eventually(self):
        # rotation_cohort is required_eventually; the signal isn't
        # registered so the aggregator would normally abstain on
        # missing_eventually. But a decisive signal already proves
        # same-device, so the merge should fire anyway.
        profile = ClassProfile(
            name="test",
            weights={"decisive": 0.10, "rotation_cohort": 0.20},
            threshold=0.95,
            min_total_weight=0.99,
            required_eventually=frozenset({"rotation_cohort"}),
            decisive_signals=frozenset({"decisive"}),
            decisive_threshold=0.95,
        )
        ctx = ClusterContext(
            signals={"decisive": _FixedSignal("decisive", 0.99)},
            profiles={"test": profile},
            now=0.0,
        )
        decision, reason, _ = cluster_pair_with_reason(
            ctx, _device(1), _device(2),
        )
        self.assertIsNotNone(decision)
        self.assertTrue(decision.merge)

    def test_no_decisive_signals_listed_uses_legacy_path(self):
        # Empty decisive_signals = old behavior — never short-circuits.
        profile = _profile(decisive=[], min_total_weight=0.05)
        ctx = _ctx(profile, {
            "decisive": _FixedSignal("decisive", 1.0),
            "supporting": _FixedSignal("supporting", 1.0),
        })
        decision, _, _ = cluster_pair_with_reason(
            ctx, _device(1), _device(2),
        )
        # Both signals contributed 1.0 × 0.10 = 0.20 weighted; total
        # weight = 0.20 ≥ 0.05; final score = 1.0 ≥ 0.95 threshold.
        # Merges via the regular path — not the decisive path.
        self.assertIsNotNone(decision)
        self.assertTrue(decision.merge)


if __name__ == "__main__":
    unittest.main()
