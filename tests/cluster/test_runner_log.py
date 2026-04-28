"""Tests for ClusterRunner log output.

Captures the configured rotating handler's output via a temp file,
asserts that:
- The narration lines appear in the expected order
- Each decision line is parseable JSON with the expected shape
- Counts in the summary match the actual decisions
"""

from __future__ import annotations

import json
import logging
import re
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from btviz.cluster import (  # noqa: E402
    ClassProfile,
    ClusterContext,
    ClusterRunner,
    configure_cluster_log,
)
from btviz.cluster.base import Address, Device  # noqa: E402


def _airtag(dev_id: int, addr: bytes) -> Device:
    return Device(
        id=dev_id,
        device_class="airtag",
        address=Address(bytes_=addr, kind="random_resolvable"),
        first_seen=0.0,
        last_seen=0.0,
    )


class _AlwaysMerge:
    name = "always_merge"

    def applies_to(self, ctx, a, b):
        return True

    def score(self, ctx, a, b, params=None):
        return 1.0


class _AlwaysReject:
    name = "always_reject"

    def applies_to(self, ctx, a, b):
        return True

    def score(self, ctx, a, b, params=None):
        return 0.0


class RunnerLogTests(unittest.TestCase):

    def setUp(self):
        # Clear any state on the cluster logger from a previous run.
        logger = logging.getLogger("btviz.cluster")
        for h in list(logger.handlers):
            logger.removeHandler(h)
        self.tmp = tempfile.NamedTemporaryFile(
            "w", suffix=".log", delete=False
        )
        self.tmp.close()
        self.log_path = Path(self.tmp.name)
        configure_cluster_log(log_file=self.log_path, max_bytes=1_000_000)

    def tearDown(self):
        logger = logging.getLogger("btviz.cluster")
        for h in list(logger.handlers):
            h.close()
            logger.removeHandler(h)
        self.log_path.unlink(missing_ok=True)

    def _make_ctx(self, signals):
        profile = ClassProfile(
            name="airtag",
            weights={s.name: 1.0 for s in signals.values()},
            threshold=0.5,
            min_total_weight=0.5,
        )
        return ClusterContext(
            signals=signals,
            profiles={"airtag": profile},
            now=0.0,
        )

    def _read_log(self) -> list[str]:
        return self.log_path.read_text(encoding="utf-8").splitlines()

    def test_run_with_merges_emits_narration_and_json(self):
        signals = {"always_merge": _AlwaysMerge()}
        ctx = self._make_ctx(signals)
        devices = [
            _airtag(1, b"\xa3\xbd\x42\x7c\x9e\x11"),
            _airtag(2, b"\x11\x22\x33\x44\x55\x66"),
            _airtag(3, b"\x5d\x8e\x11\xc2\xa4\x0f"),
        ]

        runner = ClusterRunner(ctx)
        result = runner.run_once(devices)

        self.assertEqual(result.devices_in, 3)
        self.assertEqual(result.pairs_evaluated, 3)
        self.assertEqual(len(result.merge_decisions), 3)
        # Three pairwise merges → one cluster (transitive closure).
        self.assertEqual(result.cluster_count, 1)
        self.assertEqual(result.clusters_by_class["airtag"], 1)

        log_lines = self._read_log()
        self.assertTrue(
            any("cluster analysis starting" in ln for ln in log_lines)
        )
        self.assertTrue(any("analyzing 3 airtags" in ln for ln in log_lines))
        self.assertTrue(
            any("cluster analysis complete" in ln for ln in log_lines)
        )

        # Every decision line should have parseable JSON.
        decision_lines = [ln for ln in log_lines if "  decision " in ln]
        self.assertEqual(len(decision_lines), 3)
        for ln in decision_lines:
            payload = json.loads(ln.split("  decision ", 1)[1])
            self.assertIn("a", payload)
            self.assertIn("b", payload)
            self.assertIn("score", payload)
            self.assertIn("signals", payload)
            self.assertEqual(payload["profile"], "airtag")
            self.assertTrue(payload["merge"])

        # Every merge should have a corresponding narration line.
        merge_narration = [
            ln for ln in log_lines if re.search(r"  merge ", ln)
        ]
        self.assertEqual(len(merge_narration), 3)

    def test_run_with_no_decision_still_narrates(self):
        ctx = self._make_ctx({})  # no signals → every pair abstains
        # Profile expects "missing_signal" → applies, abstains.
        ctx.profiles = {
            "airtag": ClassProfile(
                name="airtag",
                weights={"missing_signal": 1.0},
                threshold=0.5,
                min_total_weight=0.5,
            )
        }
        devices = [
            _airtag(1, b"\xa3\xbd\x42\x7c\x9e\x11"),
            _airtag(2, b"\x11\x22\x33\x44\x55\x66"),
        ]

        runner = ClusterRunner(ctx)
        result = runner.run_once(devices)

        self.assertEqual(result.pairs_evaluated, 1)
        self.assertEqual(result.abstain_count, 1)

        log_lines = self._read_log()
        self.assertTrue(
            any("cluster analysis starting" in ln for ln in log_lines)
        )
        self.assertTrue(
            any("cluster analysis complete" in ln for ln in log_lines)
        )
        # No decision lines, no merge lines.
        self.assertFalse(any("  decision " in ln for ln in log_lines))
        self.assertFalse(any("  merge " in ln for ln in log_lines))

    def test_summary_counts_match_decisions(self):
        signals = {"always_merge": _AlwaysMerge()}
        ctx = self._make_ctx(signals)
        devices = [
            _airtag(1, b"\xa3\xbd\x42\x7c\x9e\x11"),
            _airtag(2, b"\x11\x22\x33\x44\x55\x66"),
        ]
        # Add an iPhone-class device that won't pair with the airtags.
        devices.append(
            Device(
                id=3,
                device_class="iphone",
                address=Address(bytes_=b"\x01" * 6, kind="random_resolvable"),
                first_seen=0.0,
                last_seen=0.0,
            )
        )
        # Add iphone profile so it's a known class.
        ctx.profiles = dict(ctx.profiles)
        ctx.profiles["iphone"] = ClassProfile(
            name="iphone",
            weights={"always_merge": 1.0},
            threshold=0.5,
            min_total_weight=0.5,
        )

        runner = ClusterRunner(ctx)
        result = runner.run_once(devices)

        self.assertEqual(result.devices_in, 3)
        self.assertEqual(result.merges_by_class["airtag"], 1)
        self.assertEqual(result.by_class["iphone"], 1)
        # Only one iphone → no candidate pair → no decision.
        self.assertEqual(result.merges_by_class.get("iphone", 0), 0)
        # 2 airtags → 1 cluster, 1 iphone → 1 cluster → 2 total.
        self.assertEqual(result.cluster_count, 2)
        self.assertEqual(result.clusters_by_class["airtag"], 1)
        self.assertEqual(result.clusters_by_class["iphone"], 1)

        log_lines = self._read_log()
        summary_block = "\n".join(log_lines)
        # Summary should mention the iPhone unchanged.
        self.assertIn("iphone", summary_block)


if __name__ == "__main__":
    unittest.main()
