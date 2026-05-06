"""Skeleton-level tests for ``InterrogatorProcess``.

The actual radio binding (``pc-ble-driver-py`` against the
Connectivity firmware) isn't wired up yet — these tests pin down the
*shape* of the driver so the future radio impl drops into a known
contract: lifecycle bookkeeping, state surface, and the intentional
``InterrogatorNotImplemented`` raised by ``request_scan_response``.
"""

from __future__ import annotations

import sys
import unittest
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from btviz.interrogator import (  # noqa: E402
    InterrogatorNotImplemented,
    InterrogatorProcess,
)


@dataclass
class _DongleFake:
    short_id: str = "dk-001"
    serial_number: str | None = "DK_SERIAL_001"


class _ClockFake:
    def __init__(self, t: float) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class InterrogatorSkeletonTests(unittest.TestCase):

    def test_construction_does_not_start_radio(self):
        # Building the process must not flip ``running`` — start() owns
        # the radio bring-up. Tests for the eventual integration will
        # verify start() opens the adapter, but for now construction is
        # cheap and side-effect-free.
        proc = InterrogatorProcess(dongle=_DongleFake())
        self.assertFalse(proc.state.running)
        self.assertIsNone(proc.state.started_at)

    def test_start_marks_running_and_records_started_at(self):
        clock = _ClockFake(t=12_345.0)
        proc = InterrogatorProcess(dongle=_DongleFake(), clock=clock)
        proc.start()
        self.assertTrue(proc.state.running)
        self.assertEqual(proc.state.started_at, 12_345.0)

    def test_stop_clears_running_state(self):
        proc = InterrogatorProcess(dongle=_DongleFake())
        proc.start()
        proc.stop()
        self.assertFalse(proc.state.running)
        self.assertIsNone(proc.state.started_at)

    def test_stop_is_idempotent(self):
        # Two stops in a row must not raise — the canvas's tear-down
        # path may end up double-calling stop() in edge cases (e.g.,
        # capture-stop racing with a watchdog teardown).
        proc = InterrogatorProcess(dongle=_DongleFake())
        proc.stop()
        proc.stop()
        self.assertFalse(proc.state.running)

    def test_state_role_is_interrogator(self):
        # The role string is what the sniffer panel keys off to render
        # this dongle differently from passive scanners. Pin the value
        # so a future rename has to update tests too.
        proc = InterrogatorProcess(dongle=_DongleFake())
        self.assertEqual(proc.state.role, "interrogator")

    def test_request_scan_response_raises_until_radio_lands(self):
        # The whole point of the scaffold: the driver surface exists,
        # the DB / dedup / role plumbing is testable, but the radio
        # call refuses loudly. When the Connectivity firmware is on
        # the DK and pc-ble-driver-py is wired in, this test gets
        # replaced by a real round-trip test.
        proc = InterrogatorProcess(dongle=_DongleFake())
        proc.start()
        with self.assertRaises(InterrogatorNotImplemented):
            proc.request_scan_response(
                target_addr="aa:bb:cc:dd:ee:ff",
                target_addr_random=True,
            )

    def test_not_implemented_is_a_notimplementederror(self):
        # Subclass relationship matters because callers may catch
        # ``NotImplementedError`` generically. Lock that in so the
        # exception hierarchy doesn't drift.
        self.assertTrue(
            issubclass(InterrogatorNotImplemented, NotImplementedError)
        )


if __name__ == "__main__":
    unittest.main()
