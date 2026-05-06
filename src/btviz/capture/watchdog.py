"""Capture-side stall watchdog.

Periodically scans the running sniffer subprocesses and detects when
one has stopped delivering packets. On detection:

- logs a ``STALL`` line to ``~/.btviz/capture.log``
- increments the sniffer's lifetime ``stall_count`` in the DB
- asks the coordinator to restart the wedged subprocess
- caps restart attempts at ``max_attempts`` per session, with a
  minimum gap between attempts; after exhaustion, marks the sniffer
  ``stuck`` so the panel can surface a more prominent indicator

The watchdog is pure Python (no Qt) so it's unit-testable in
isolation. The canvas drives it by calling :py:meth:`tick` from its
existing periodic reload hook; no separate timer needed.

Token ``STALL`` is used in every log line. The user-facing badge
shows the same token verbatim, so ``grep STALL ~/.btviz/capture.log``
is the obvious next step from the UI.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..capture_log import get_capture_logger

log = get_capture_logger()


# --- defaults --------------------------------------------------------

#: How long a running sniffer can be silent before we declare a stall.
#: Apple Continuity emits adv on at least one of 37/38/39 every few
#: seconds in any normal environment, so 60 s of silence on a Pinned
#: dongle is genuinely abnormal.
DEFAULT_STALL_THRESHOLD_S = 60.0

#: How often the canvas should call :py:meth:`tick`. Not enforced by
#: this module — exposed for callers that want to align their timer.
DEFAULT_WATCHDOG_PERIOD_S = 10.0

#: Maximum restart attempts per (sniffer, session) before giving up.
#: After exhaustion the sniffer is flagged ``stuck`` and the watchdog
#: stops trying. Replug → fresh discovery → fresh attempts.
DEFAULT_MAX_ATTEMPTS = 3

#: Minimum elapsed time between two restart attempts on the same
#: sniffer. Prevents tight-loop thrash if restart succeeds in spawning
#: but the kernel-level CDC endpoint is wedged and re-stalls instantly.
DEFAULT_MIN_GAP_S = 30.0


# --- protocol the watchdog talks to ----------------------------------
#
# The coordinator and the SnifferProcess instances are passed in
# rather than imported here, so the watchdog stays unit-testable
# against fakes. The minimum interface a "sniffer-like" object must
# expose to the watchdog:
#
#   .state.running:        bool
#   .state.role:           str ("idle" | "scan" | "follow")
#   .state.last_packet_ts: float | None
#   .state.started_at:     float | None
#   ._dongle.short_id:     str
#   ._dongle.serial_number: str | None  (for DB lookup)
#
# And the coordinator must expose a callable that restarts a sniffer
# by short_id, returning True on successful spawn:
#
#   restart_fn(short_id: str) -> bool
#
# All real impls already satisfy this; tests pass plain dataclasses.

RestartFn = Callable[[str], bool]


@dataclass
class _Attempt:
    """Per-sniffer in-memory restart-attempt bookkeeping."""

    count: int = 0
    last_at: float = 0.0
    given_up: bool = False


class StallWatchdog:
    """Detect-and-restart for wedged capture subprocesses.

    Created by the canvas at capture-start, fed live ``SnifferProcess``
    references from the coordinator, ticked periodically. Owns its
    own attempt-counter state; the lifetime ``stall_count`` lives on
    the DB row and is bumped via the repos handle.

    Typical wiring (canvas):

        self._watchdog = StallWatchdog(
            sniffers=lambda: list(self._coord.sniffers.values()),
            repos=self.repos,
            restart=self._coord.restart_one,
        )
        # ... in _live_tick:
        self._watchdog.tick()
    """

    def __init__(
        self,
        *,
        sniffers: Callable[[], list[Any]],
        repos: Any,
        restart: RestartFn,
        threshold_s: float = DEFAULT_STALL_THRESHOLD_S,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        min_gap_s: float = DEFAULT_MIN_GAP_S,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._sniffers = sniffers
        self._repos = repos
        self._restart = restart
        self._threshold_s = threshold_s
        self._max_attempts = max_attempts
        self._min_gap_s = min_gap_s
        self._clock = clock
        self._attempts: dict[str, _Attempt] = {}
        # Sniffers that have crossed ``max_attempts`` and are
        # flagged stuck. Surfaced via :py:meth:`stuck_short_ids`.
        self._stuck: set[str] = set()

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    def tick(self) -> list[str]:
        """Run one detection pass.

        Returns the list of ``short_id`` values that stalled in this
        tick (whether or not we tried to restart them) — useful for
        the canvas to update its status text.
        """
        now = self._clock()
        stalled: list[str] = []
        for sniffer in self._sniffers():
            if not self._is_eligible(sniffer):
                continue
            if not self._is_stalled(sniffer, now):
                continue
            short_id = sniffer._dongle.short_id
            stalled.append(short_id)
            self._on_stall(sniffer, now)
        return stalled

    def stuck_short_ids(self) -> set[str]:
        """short_ids the watchdog has given up on (replug required)."""
        return frozenset(self._stuck)

    def currently_silent_short_ids(self) -> frozenset[str]:
        """short_ids of sniffers currently silent past ``threshold_s``.

        Read-only inspector — no logging, no DB bumps, no restart
        attempts. Mirrors the eligibility + silence checks of
        :py:meth:`tick` so the canvas can poll between ticks (every
        scene reload, ~2 s) and keep the toolbar STALL warning in
        sync without having to wait for the next 10 s watchdog tick.

        Excludes sniffers already in the stuck set — those are
        reported separately via :py:meth:`stuck_short_ids` so the
        caller can render the two states with different urgency
        ("silent, retrying" vs "given up, replug required").
        """
        now = self._clock()
        silent: list[str] = []
        for sniffer in self._sniffers():
            if not self._is_eligible(sniffer):
                continue
            if not self._is_stalled(sniffer, now):
                continue
            silent.append(sniffer._dongle.short_id)
        return frozenset(silent)

    def reset(self, short_id: str) -> None:
        """Forget per-session state for one sniffer.

        Called when the user replugs or triggers a manual restart —
        attempts counter and stuck flag reset so the watchdog will
        try again on the next stall.
        """
        self._attempts.pop(short_id, None)
        self._stuck.discard(short_id)

    def reset_all(self) -> None:
        """Forget per-session state for every sniffer.

        Called when the user stops + restarts capture so a stuck
        sniffer from the previous session gets fresh attempts.
        """
        self._attempts.clear()
        self._stuck.clear()

    # ------------------------------------------------------------------
    # internal
    # ------------------------------------------------------------------

    def _is_eligible(self, sniffer: Any) -> bool:
        """True if the sniffer is in a state where stall detection makes sense.

        Only sniffers that are *supposed* to be capturing — running
        AND not in the Idle role — count. Idle sniffers correctly
        produce zero packets and would otherwise trip the watchdog.
        Sniffers we've already given up on are skipped to avoid
        endless retries.
        """
        st = sniffer.state
        if not st.running:
            return False
        if st.role == "idle":
            return False
        if sniffer._dongle.short_id in self._stuck:
            return False
        return True

    def _is_stalled(self, sniffer: Any, now: float) -> bool:
        """Apply the stall rule to one sniffer at the given clock."""
        st = sniffer.state
        if st.last_packet_ts is None:
            # Grace period: if the subprocess just started and hasn't
            # produced its first packet yet, give it ``threshold_s``
            # before declaring stall. ``started_at`` is set in
            # ``SnifferProcess.start`` after subprocess spawn.
            if st.started_at is None:
                return False
            return (now - st.started_at) > self._threshold_s
        return (now - st.last_packet_ts) > self._threshold_s

    def _on_stall(self, sniffer: Any, now: float) -> None:
        """Handle one stall event: log, bump DB, attempt restart."""
        short_id = sniffer._dongle.short_id
        attempt = self._attempts.setdefault(short_id, _Attempt())
        st = sniffer.state
        silent_for = (
            now - st.last_packet_ts
            if st.last_packet_ts is not None
            else (now - (st.started_at or now))
        )

        # If we restarted recently, give the new subprocess time to
        # produce its first packet before declaring another stall.
        if attempt.last_at and (now - attempt.last_at) < self._min_gap_s:
            return

        attempt.count += 1
        attempt.last_at = now

        # WARNING-level: "detected" / "restarted" — recoverable
        # events that the watchdog handled on its own. Above INFO
        # so they're still visible at the strictest user-friendly
        # level, but below ERROR which is reserved for unrecoverable
        # ("you must replug") states.
        log.warning(
            "STALL detected sniffer=%s role=%s silent_for=%.1fs attempt=%d",
            short_id, st.role, silent_for, attempt.count,
        )

        # Bump the lifetime counter in the DB. Best-effort —
        # discovery may not have persisted this dongle yet (rare,
        # but possible if a stall happens between USB enumeration
        # and the first record_discovered sweep).
        self._bump_db_counter(sniffer, now)

        if attempt.count > self._max_attempts:
            attempt.given_up = True
            self._stuck.add(short_id)
            # ERROR-level: ``gave_up`` is the only state that
            # genuinely requires user action (physical replug or
            # firmware reflash). Even a 'log_level=error' setting
            # surfaces this so a quiet log isn't a misleading log.
            log.error(
                "STALL gave_up sniffer=%s attempts=%d — replug required",
                short_id, attempt.count - 1,
            )
            return

        try:
            ok = self._restart(short_id)
        except Exception as e:  # noqa: BLE001 — never let a watchdog tick raise
            log.error(
                "STALL restart_failed sniffer=%s error=%r",
                short_id, e,
            )
            return

        if ok:
            log.warning(
                "STALL restarted sniffer=%s spawned new subprocess",
                short_id,
            )
        else:
            # Coordinator-declined restart is recoverable on the
            # next tick (the watchdog will try again subject to the
            # min-gap rule), so it stays at WARNING — error tier
            # is reserved for terminal states.
            log.warning(
                "STALL restart_failed sniffer=%s coordinator declined",
                short_id,
            )

    def _bump_db_counter(self, sniffer: Any, now: float) -> None:
        sn = getattr(sniffer._dongle, "serial_number", None)
        if not sn:
            return
        # Repos lookup by serial — keeps us decoupled from how the
        # canvas / coordinator hold sniffer rows.
        repo = getattr(self._repos, "sniffers", None)
        if repo is None:
            return
        row = repo.get_by_serial(sn) if hasattr(repo, "get_by_serial") else None
        if row is None or row.id is None:
            return
        try:
            repo.bump_stall_counter(row.id, now)
        except Exception as e:  # noqa: BLE001
            log.error("STALL db_bump_failed sniffer=%s error=%r",
                      sniffer._dongle.short_id, e)
