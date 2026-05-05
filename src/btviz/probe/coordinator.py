"""Probe queue + scheduler.

**Stub.** Real implementation lands in the next PR. This file
defines the public interface so callers (canvas, role planner,
storage) can be wired up against a contract.

Lifetime model mirrors the cluster worker pattern from PR #83:
one persistent ``QObject`` lives on a dedicated ``QThread`` for
the canvas's lifetime. Probe requests arrive via a class-level
Signal on the canvas; the coordinator's slot runs on the worker
thread, dispatches to ``HciDriver``, and emits ``finished`` /
``failed`` back to the main thread for storage + UI update.

The coordinator is the *only* user of ``HciDriver``. It also owns
the borrow/release dance with the capture coordinator's
``borrow_tx_dongle`` / ``release_dongle`` (to be added).
"""
from __future__ import annotations

from collections.abc import Callable

from .types import ProbeRequest, ProbeResult


# Signature for the borrow callback the capture coordinator exposes.
# Takes a requester string (for logging / debugging), returns the
# short_id of the loaned dongle or None when none available.
BorrowFn = Callable[[str], "str | None"]
ReleaseFn = Callable[[str], None]


class ProbeCoordinator:
    """Manages an in-flight probe and a backlog queue.

    v1 invariants:
      * At most one probe in flight at any time. Subsequent
        requests queue.
      * Per-class timeouts honored (looked up via
        ``request.timeout_s``, set by the caller).
      * On capture-stop, queue is drained and any in-flight
        probe is cancelled.

    Concrete behaviour is implemented in a follow-up PR. This stub
    documents the seam.
    """

    def __init__(
        self,
        *,
        borrow_dongle: BorrowFn,
        release_dongle: ReleaseFn,
    ) -> None:
        self._borrow = borrow_dongle
        self._release = release_dongle
        self._queue: list[ProbeRequest] = []
        self._in_flight: ProbeRequest | None = None

    def submit(self, request: ProbeRequest) -> None:
        """Append to the queue. No-op if already queued for this device."""
        raise NotImplementedError("ProbeCoordinator.submit: stub")

    def cancel(self, device_id: int) -> None:
        """Cancel a pending or in-flight probe for ``device_id``."""
        raise NotImplementedError("ProbeCoordinator.cancel: stub")

    def shutdown(self) -> None:
        """Drain the queue and cancel any in-flight probe.

        Called from ``CanvasWindow.closeEvent`` and on capture-stop.
        """
        raise NotImplementedError("ProbeCoordinator.shutdown: stub")

    # --- worker-thread entry (queued slot) ----------------------------
    #
    # Real impl will take a ProbeRequest (or pull from queue), borrow
    # a dongle, instantiate HciDriver, run probe(), persist result via
    # storage adapter, release dongle. For now just a sketch:
    #
    #   def run_request(self, request: ProbeRequest) -> None:
    #       short_id = self._borrow("probe")
    #       if short_id is None:
    #           result = ProbeResult(request=request, started_at=...,
    #                                 outcome=ProbeOutcome.REJECTED,
    #                                 detail="no TX-capable dongle free")
    #           self.finished.emit(result)
    #           return
    #       driver = HciDriver(serial_path_for(short_id))
    #       try:
    #           driver.open()
    #           result = driver.probe(request)
    #       finally:
    #           driver.close()
    #           self._release(short_id)
    #       self.finished.emit(result)
