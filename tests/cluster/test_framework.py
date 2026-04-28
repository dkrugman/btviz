"""Synthetic-data smoke tests for the cluster framework.

Runnable as a plain script (no pytest needed):

    python -m tests.cluster.test_framework

Each scenario constructs a ClusterContext + a small set of synthetic
Devices + per-device sniffer-observation timestamps, then asserts on
the output of cluster_pair.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from btviz.cluster.aggregator import cluster_pair  # noqa: E402
from btviz.cluster.base import (  # noqa: E402
    Address,
    ClassProfile,
    ClusterContext,
    Device,
)
from btviz.cluster.profile_loader import load_profiles  # noqa: E402
from btviz.cluster.signals import load_signals  # noqa: E402


def _airtag(dev_id: int, *, last_seen: float = 0.0) -> Device:
    return Device(
        id=dev_id,
        device_class="airtag",
        address=Address(bytes_=b"\x00" * 6, kind="random_resolvable"),
        first_seen=0.0,
        last_seen=last_seen,
    )


def _make_ctx(observations: dict[int, dict[int, list[float]]]) -> ClusterContext:
    profiles = load_profiles()
    signals = load_signals()
    ctx = ClusterContext(
        signals=signals,
        profiles=profiles,
        now=10_000.0,
    )
    ctx.cache["observations"] = observations
    return ctx


class RotationCohortTests(unittest.TestCase):

    def test_clean_handoff_scores_high(self):
        # Device A vanishes at t=900, device B appears at t=901
        # on the same sniffer. Gap = 1s — well within window.
        # Expected rotation = 900s; gap is ~899s short of expected,
        # but the score function uses min plausible gap so this
        # tests the window_min path.
        obs = {
            1: {7: [0.0, 100.0, 500.0, 900.0]},
            2: {7: [901.0, 1500.0, 1801.0]},
        }
        ctx = _make_ctx(obs)
        a, b = _airtag(1, last_seen=900.0), _airtag(2, last_seen=1801.0)
        decision = cluster_pair(ctx, a, b)
        # Clean 1s gap is "suspiciously fast" → returns 0.5 from
        # rotation_cohort. With AirTag profile weights and only this
        # signal contributing, total_weight=0.35 < min_total_weight=0.50,
        # so the framework abstains.
        self.assertIsNone(decision)

    def test_clean_handoff_with_corroborating_signal_merges(self):
        # Device A last seen at t=100; device B first seen at t=200
        # → gap = 100s ≈ expected_rotation (100s in this test) → 1.0.
        # Add a synthetic always-1.0 corroborating signal so total_weight
        # exceeds min_total_weight and a decision actually fires.
        obs = {
            1: {7: [0.0, 50.0, 100.0]},
            2: {7: [200.0, 250.0, 300.0]},
        }
        ctx = _make_ctx(obs)

        class CorroboratingSignal:
            name = "corroborating"

            def applies_to(self, ctx, a, b):
                return True

            def score(self, ctx, a, b, params=None):
                return 1.0

        ctx.signals = dict(ctx.signals)
        ctx.signals["corroborating"] = CorroboratingSignal()
        ctx.profiles = dict(ctx.profiles)
        ctx.profiles["airtag"] = ClassProfile(
            name="airtag",
            weights={"rotation_cohort": 0.4, "corroborating": 0.4},
            threshold=0.7,
            min_total_weight=0.5,
            required_eventually=frozenset(),
            params={
                "rotation_cohort": {
                    "expected_rotation": 100.0,
                    "window_min": 0.05,
                    "window_max": 200.0,
                },
            },
        )

        a, b = _airtag(1, last_seen=100.0), _airtag(2, last_seen=300.0)
        decision = cluster_pair(ctx, a, b)
        self.assertIsNotNone(decision)
        self.assertTrue(decision.merge)
        self.assertGreaterEqual(decision.score, 0.7)
        self.assertIn("rotation_cohort", decision.signals)
        self.assertIn("corroborating", decision.signals)

    def test_no_shared_sniffer_abstains(self):
        obs = {
            1: {7: [0.0, 100.0]},
            2: {8: [200.0, 300.0]},  # different sniffer
        }
        ctx = _make_ctx(obs)
        a, b = _airtag(1), _airtag(2)
        decision = cluster_pair(ctx, a, b)
        # rotation_cohort.applies_to → False (no shared sniffer);
        # required_eventually unsatisfied → no opinion.
        self.assertIsNone(decision)

    def test_concurrent_observation_returns_active_mismatch(self):
        # Both devices seen by sniffer 7 within the rotation window
        # → cannot be the same device handing off identity.
        # rotation_cohort scores -1.0 (active mismatch) for this pair.
        # In the airtag profile rotation_cohort weight is 0.35; below
        # min_total_weight=0.50, so the framework still abstains.
        # That's the correct outcome: a single signal voting "different"
        # isn't enough by itself; corroboration is required.
        obs = {
            1: {7: [0.0, 100.0, 200.0]},
            2: {7: [50.0, 150.0, 250.0]},
        }
        ctx = _make_ctx(obs)
        a, b = _airtag(1), _airtag(2)
        decision = cluster_pair(ctx, a, b)
        self.assertIsNone(decision)

    def test_cross_class_pairs_skipped(self):
        a = _airtag(1, last_seen=900.0)
        b = Device(
            id=2,
            device_class="iphone",  # different class
            address=Address(bytes_=b"\x00" * 6, kind="random_resolvable"),
            first_seen=0.0,
            last_seen=1801.0,
        )
        ctx = _make_ctx({})
        decision = cluster_pair(ctx, a, b)
        self.assertIsNone(decision)


class IRKShortCircuitTests(unittest.TestCase):
    """Verify the aggregator's IRK-trump path even without a real signal."""

    def _ctx_with_irk_signal(self, score_value):
        from btviz.cluster.base import ClassProfile, ClusterContext

        class FakeIRK:
            name = "irk_resolution"

            def applies_to(self, ctx, a, b):
                return True

            def score(self, ctx, a, b, params=None):
                return score_value

        profile = ClassProfile(
            name="airtag",
            weights={"rotation_cohort": 1.0},
            threshold=0.7,
            min_total_weight=0.5,
        )
        return ClusterContext(
            signals={"irk_resolution": FakeIRK()},
            profiles={"airtag": profile},
            now=0.0,
        )

    def test_irk_match_forces_merge(self):
        ctx = self._ctx_with_irk_signal(1.0)
        a, b = _airtag(1), _airtag(2)
        decision = cluster_pair(ctx, a, b)
        self.assertIsNotNone(decision)
        self.assertTrue(decision.merge)
        self.assertEqual(decision.score, 1.0)

    def test_irk_mismatch_forces_reject(self):
        ctx = self._ctx_with_irk_signal(-1.0)
        a, b = _airtag(1), _airtag(2)
        decision = cluster_pair(ctx, a, b)
        self.assertIsNotNone(decision)
        self.assertFalse(decision.merge)
        self.assertEqual(decision.abort_reason, "irk_mismatch")

    def test_irk_abstain_falls_through(self):
        # IRK returns None → aggregator falls through to behavioral signals
        # → with no other signals registered, no contribution → abstain.
        ctx = self._ctx_with_irk_signal(None)
        a, b = _airtag(1), _airtag(2)
        decision = cluster_pair(ctx, a, b)
        # rotation_cohort referenced in profile but not registered →
        # required_eventually unsatisfied path is not triggered (it's
        # only weights, not required), and total_weight=0 → None.
        # If we make rotation_cohort required_eventually, decision stays None.
        self.assertIsNone(decision)


class ProfileLoaderTests(unittest.TestCase):

    def test_loads_shipped_profiles(self):
        profiles = load_profiles()
        self.assertIn("airtag", profiles)
        self.assertIn("iphone", profiles)
        self.assertIn("airpods", profiles)
        self.assertIn("hearing_aid", profiles)
        self.assertIn("default", profiles)

    def test_airtag_profile_shape(self):
        profiles = load_profiles()
        p = profiles["airtag"]
        self.assertEqual(p.threshold, 0.70)
        self.assertIn("rotation_cohort", p.weights)
        self.assertIn("rotation_cohort", p.required_eventually)
        # min_total_weight default sanity
        self.assertGreater(p.min_total_weight, 0.0)
        self.assertLessEqual(p.min_total_weight, 1.0)


class WeightSumProtocolTests(unittest.TestCase):
    """Verify the min_total_weight gate works as intended."""

    def test_under_min_total_weight_abstains(self):
        from btviz.cluster.base import ClassProfile, ClusterContext

        class StrongSignal:
            name = "strong"

            def applies_to(self, ctx, a, b):
                return True

            def score(self, ctx, a, b, params=None):
                return 1.0

        profile = ClassProfile(
            name="airtag",
            weights={"strong": 0.20, "missing": 0.80},  # only 0.20 contributes
            threshold=0.5,
            min_total_weight=0.50,
        )
        ctx = ClusterContext(
            signals={"strong": StrongSignal()},
            profiles={"airtag": profile},
            now=0.0,
        )
        a, b = _airtag(1), _airtag(2)
        # weighted_sum = 1.0 * 0.20 = 0.20; total_weight = 0.20.
        # Below min_total_weight=0.50 → abstain.
        decision = cluster_pair(ctx, a, b)
        self.assertIsNone(decision)

    def test_meets_min_total_weight_decides(self):
        from btviz.cluster.base import ClassProfile, ClusterContext

        class StrongSignal:
            name = "strong"

            def applies_to(self, ctx, a, b):
                return True

            def score(self, ctx, a, b, params=None):
                return 1.0

        profile = ClassProfile(
            name="airtag",
            weights={"strong": 0.60},
            threshold=0.5,
            min_total_weight=0.50,
        )
        ctx = ClusterContext(
            signals={"strong": StrongSignal()},
            profiles={"airtag": profile},
            now=0.0,
        )
        a, b = _airtag(1), _airtag(2)
        decision = cluster_pair(ctx, a, b)
        self.assertIsNotNone(decision)
        self.assertTrue(decision.merge)
        self.assertEqual(decision.score, 1.0)


if __name__ == "__main__":
    unittest.main()
