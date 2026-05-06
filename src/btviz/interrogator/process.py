"""InterrogatorProcess — wraps a Connectivity-firmware Nordic board.

The interrogator owns one dongle and exposes a small request/response
API to the canvas. v1 ships only :py:meth:`request_scan_response`;
later primitives (GATT discovery, SMP pubkey collect) plug in as
additional methods.

Lifetime model mirrors :class:`btviz.extcap.sniffer.SnifferProcess`:
the canvas constructs one at capture-start when an
interrogator-eligible dongle is discovered, and tears it down at
capture-stop. While the actual radio integration via
``pc-ble-driver-py`` lands later, the surrounding plumbing —
DB writes, dedup against passive sniffers, lifecycle bookkeeping —
is wired up here so that filling the radio call is the only remaining
piece.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


# --- exceptions ------------------------------------------------------


class InterrogatorNotImplemented(NotImplementedError):
    """Raised when a primitive is invoked before the radio binding lands.

    Surfaced rather than logged so the canvas's manual-trigger path
    can show "Interrogator not yet wired up — needs Connectivity
    firmware on the DK" in the toolbar status without silently
    eating the click. Tests assert on this exception type so
    accidental promotion to a log-and-return won't slip through.
    """


# --- result types ----------------------------------------------------


@dataclass(frozen=True)
class ScanResponseResult:
    """Outcome of a SCAN_REQ → SCAN_RSP exchange.

    ``raw`` is the SCAN_RSP PDU bytes verbatim — kept for forensic
    re-decode and audit-log persistence. ``ad_records`` is the
    parsed AD-structure list (LTV-decoded), built by reusing the
    existing ingest decoder so harvested fields fold back into the
    same ``device_ad_history`` / ``addresses.local_name`` paths the
    passive sniffers populate.
    """

    target_addr: str               # e.g., "78:d3:33:c0:35:69"
    target_addr_random: bool       # True if random/RPA, False if public
    rssi: int                      # signed dBm reported by the host stack
    raw: bytes                     # SCAN_RSP PDU bytes
    ad_records: tuple[tuple[int, bytes], ...] = ()
    received_at: float = 0.0       # unix epoch when the host saw the response


# --- the driver skeleton --------------------------------------------


@dataclass
class InterrogatorState:
    """Mutable per-process state surfaced to the canvas / panel."""

    running: bool = False
    role: str = "interrogator"
    own_addresses: tuple[str, ...] = ()  # dedup hint for LiveIngest
    last_attempt_at: float | None = None
    last_error: str | None = None
    started_at: float | None = None


class InterrogatorProcess:
    """Owns one Connectivity-firmware Nordic dongle.

    Construction is cheap (no radio activity); :py:meth:`start` is
    where the radio-binding will eventually open the serial port and
    initialize the SoftDevice over RPC. Until then,
    :py:meth:`request_scan_response` raises
    :class:`InterrogatorNotImplemented` so callers can fail loudly.

    Threading: the canvas creates one instance, calls
    :py:meth:`request_scan_response` on the main thread, and the
    eventual radio binding will fan responses back via a queue or a
    Qt signal. For the scaffold all calls are synchronous.

    Args:
        dongle: object with ``.short_id`` and ``.serial_number`` —
            the same shape :class:`btviz.extcap.sniffer.SnifferProcess`
            consumes, so dongle discovery code can be reused
            unchanged.
        clock: injectable for tests; production passes ``time.time``.
    """

    def __init__(
        self,
        *,
        dongle: Any,
        clock: Callable[[], float] | None = None,
    ) -> None:
        import time as _time
        self._dongle = dongle
        self._clock = clock or _time.time
        self.state = InterrogatorState()

    # --- lifecycle --------------------------------------------------

    def start(self) -> None:
        """Open the radio binding (eventually).

        Currently a no-op except for state bookkeeping — the actual
        ``pc-ble-driver-py`` ``Adapter.open()`` happens once the DK is
        running Connectivity firmware. We still flip ``running`` so
        the rest of the canvas (panel rendering, watchdog eligibility)
        can treat the slot as occupied and not try to spawn a passive
        SnifferProcess on the same dongle.
        """
        self.state.running = True
        self.state.started_at = self._clock()

    def stop(self) -> None:
        """Tear down. Idempotent."""
        self.state.running = False
        self.state.started_at = None

    # --- primitives -------------------------------------------------

    def request_scan_response(
        self,
        target_addr: str,
        target_addr_random: bool,
        timeout_s: float = 1.0,
    ) -> ScanResponseResult:
        """Send a SCAN_REQ to ``target_addr`` and harvest the SCAN_RSP.

        Will eventually drive ``pc-ble-driver-py`` to:

          1. Configure the adapter as an active scanner.
          2. Set a one-shot scan filter for ``target_addr``.
          3. Wait up to ``timeout_s`` for the SCAN_RSP frame.
          4. Return the raw PDU plus parsed AD records.

        Raises:
            InterrogatorNotImplemented: until the radio binding lands.
        """
        raise InterrogatorNotImplemented(
            "Interrogator radio binding not wired up yet — "
            "DK board needs Connectivity firmware reflashed"
        )
