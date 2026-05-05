"""StallWatchdog detection / restart logic.

Pure-Python tests — no Qt, no real subprocesses, no real DB.
The watchdog talks to a small protocol (``.state.running``,
``.state.role``, ``.state.last_packet_ts``, etc.) which we
satisfy with simple namespace fakes.
"""

from __future__ import annotations

import sys
import unittest
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from btviz.capture.watchdog import StallWatchdog  # noqa: E402


@dataclass
class _State:
    running: bool = True
    role: str = "scan"
    last_packet_ts: float | None = 1000.0
    started_at: float | None = 1000.0


@dataclass
class _Dongle:
    short_id: str = "abc"
    serial_number: str | None = "SERIAL_ABC"


@dataclass
class _Sniffer:
    state: _State = field(default_factory=_State)
    _dongle: _Dongle = field(default_factory=_Dongle)


class _SniffersRepoFake:
    """Minimal repo-shaped object the watchdog talks to."""

    def __init__(self):
        self.bumps: list[tuple[int, float]] = []
        self.row = type("Row", (), {"id": 1})()

    def get_by_serial(self, serial: str):
        return self.row

    def bump_stall_counter(self, sniffer_id: int, when: float) -> None:
        self.bumps.append((sniffer_id, when))


class _ReposFake:
    def __init__(self):
        self.sniffers = _SniffersRepoFake()


class _ClockFake:
    def __init__(self, t: float):
        self.t = t

    def __call__(self) -> float:
        return self.t


class WatchdogTests(unittest.TestCase):

    def _make(self, sniffer, *, threshold=60.0, max_attempts=3,
              min_gap=30.0, t=1100.0):
        repos = _ReposFake()
        clock = _ClockFake(t)
        restarts: list[str] = []

        def restart(short_id: str) -> bool:
            restarts.append(short_id)
            sniffer.state.last_packet_ts = clock()  # successful spawn
            sniffer.state.started_at = clock()
            return True

        wd = StallWatchdog(
            sniffers=lambda: [sniffer],
            repos=repos,
            restart=restart,
            threshold_s=threshold,
            max_attempts=max_attempts,
            min_gap_s=min_gap,
            clock=clock,
        )
        return wd, repos, restarts, clock

    # ---- detection rules ----

    def test_silent_long_enough_triggers_restart(self):
        # Last packet was 70s ago, threshold is 60s → stall.
        sniffer = _Sniffer(state=_State(last_packet_ts=1030.0))
        wd, repos, restarts, _clock = self._make(sniffer, threshold=60.0)
        stalled = wd.tick()
        self.assertEqual(stalled, ["abc"])
        self.assertEqual(restarts, ["abc"])
        # DB lifetime counter bumped exactly once.
        self.assertEqual(repos.sniffers.bumps, [(1, 1100.0)])

    def test_recent_packet_does_not_trigger(self):
        # Last packet 5s ago; well under threshold.
        sniffer = _Sniffer(state=_State(last_packet_ts=1095.0))
        wd, _, restarts, _ = self._make(sniffer, threshold=60.0)
        self.assertEqual(wd.tick(), [])
        self.assertEqual(restarts, [])

    def test_idle_role_skipped(self):
        # An Idle sniffer correctly produces zero packets — must not
        # trip the watchdog.
        sniffer = _Sniffer(state=_State(role="idle", last_packet_ts=0.0))
        wd, _, restarts, _ = self._make(sniffer)
        self.assertEqual(wd.tick(), [])
        self.assertEqual(restarts, [])

    def test_not_running_skipped(self):
        sniffer = _Sniffer(state=_State(running=False, last_packet_ts=0.0))
        wd, _, restarts, _ = self._make(sniffer)
        self.assertEqual(wd.tick(), [])
        self.assertEqual(restarts, [])

    # ---- grace period for fresh subprocess ----

    def test_grace_period_for_first_packet(self):
        # Subprocess just spawned, no packet yet, only 30s in.
        # Threshold is 60s → no stall declared.
        sniffer = _Sniffer(state=_State(
            last_packet_ts=None, started_at=1070.0,
        ))
        wd, _, restarts, _ = self._make(sniffer, threshold=60.0)
        self.assertEqual(wd.tick(), [])
        self.assertEqual(restarts, [])

    def test_grace_period_expired_triggers(self):
        # Subprocess started 90s ago, still no packet → stall.
        sniffer = _Sniffer(state=_State(
            last_packet_ts=None, started_at=1010.0,
        ))
        wd, _, restarts, _ = self._make(sniffer, threshold=60.0)
        self.assertEqual(wd.tick(), ["abc"])
        self.assertEqual(restarts, ["abc"])

    # ---- attempt cap + min gap ----

    def test_min_gap_prevents_back_to_back_restarts(self):
        sniffer = _Sniffer(state=_State(last_packet_ts=1030.0))
        wd, _, restarts, clock = self._make(sniffer, threshold=60.0,
                                             min_gap=30.0)
        wd.tick()
        self.assertEqual(restarts, ["abc"])
        # Re-stall 5s later — within min_gap, so detection still
        # fires (tick reports the short_id) but the watchdog
        # suppresses the actual restart call. Critical assertion:
        # restart_fn was NOT called a second time.
        clock.t = 1105.0
        sniffer.state.last_packet_ts = 1030.0  # still wedged
        wd.tick()
        self.assertEqual(restarts, ["abc"])

    def test_give_up_after_max_attempts(self):
        sniffer = _Sniffer(state=_State(last_packet_ts=0.0))
        wd, _, restarts, clock = self._make(
            sniffer, threshold=60.0, max_attempts=2, min_gap=10.0,
        )
        # Three consecutive stalls separated by min_gap.
        for i in range(4):
            clock.t = 1100.0 + i * 30.0
            sniffer.state.last_packet_ts = 0.0
            wd.tick()
        # Restarted twice (max_attempts), then gave up.
        self.assertEqual(restarts, ["abc", "abc"])
        self.assertIn("abc", wd.stuck_short_ids())

    def test_stuck_sniffer_is_skipped(self):
        sniffer = _Sniffer(state=_State(last_packet_ts=0.0))
        wd, _, restarts, clock = self._make(
            sniffer, threshold=60.0, max_attempts=1, min_gap=10.0,
        )
        # Stall once → restart. Stall again → give up.
        wd.tick()
        clock.t = 1200.0
        wd.tick()
        self.assertIn("abc", wd.stuck_short_ids())
        # Subsequent ticks don't try to restart.
        clock.t = 1300.0
        wd.tick()
        clock.t = 1400.0
        wd.tick()
        self.assertEqual(len(restarts), 1)

    def test_reset_clears_stuck_state(self):
        sniffer = _Sniffer(state=_State(last_packet_ts=0.0))
        wd, _, restarts, clock = self._make(
            sniffer, threshold=60.0, max_attempts=1, min_gap=10.0,
        )
        wd.tick()
        clock.t = 1200.0
        wd.tick()
        self.assertIn("abc", wd.stuck_short_ids())
        wd.reset("abc")
        self.assertNotIn("abc", wd.stuck_short_ids())
        # Now a fresh stall should trigger a new restart attempt.
        clock.t = 1400.0
        sniffer.state.last_packet_ts = 0.0
        wd.tick()
        self.assertEqual(len(restarts), 2)


if __name__ == "__main__":
    unittest.main()
