"""HCI driver for the connectivity-firmware DK.

**Stub.** v1 plan is to use Nordic's ``pc-ble-driver-py`` rather
than implementing HCI from scratch — see
``docs/active_interrogation/03_revised_plan.md`` §7.1. This module
will hold the thin wrapper that adapts ``pc-ble-driver-py`` to
btviz's ``ProbeRequest`` / ``ProbeResult`` shape.

Why a wrapper at all rather than calling ``pc-ble-driver-py``
directly from the coordinator: the wrapper boundary lets us swap
the underlying transport later (custom HCI, ``bleak`` fallback)
without rewriting coordinator logic. Treat it as the only place
in btviz that knows about the Nordic library.
"""
from __future__ import annotations

from .types import ProbeRequest, ProbeResult


class HciDriverNotImplemented(NotImplementedError):
    """Raised by the stub until the real driver lands.

    Distinct exception class so the coordinator's error path can
    distinguish "driver not yet built" from "driver tried and
    failed at runtime."
    """


class HciDriver:
    """Wrapper around the Nordic Central role.

    Real implementation will be backed by ``pc-ble-driver-py``.
    ``open()`` initializes the connectivity firmware on the DK,
    ``probe()`` runs one transaction end-to-end, ``close()`` shuts
    everything down. One ``HciDriver`` instance per DK.

    Thread model: instance methods are called from the probe
    worker thread (see ``coordinator.py``). Not thread-safe;
    coordinator serializes calls.
    """

    def __init__(self, serial_path: str) -> None:
        self.serial_path = serial_path
        self._open = False

    def open(self) -> None:
        raise HciDriverNotImplemented(
            "HciDriver.open: not yet implemented. See "
            "docs/active_interrogation/05_scaffolding.md for status."
        )

    def close(self) -> None:
        # Idempotent so coordinator can call on cleanup paths.
        self._open = False

    def probe(self, request: ProbeRequest) -> ProbeResult:
        """Connect, run Tier-1 reads, disconnect, return result."""
        raise HciDriverNotImplemented(
            "HciDriver.probe: not yet implemented."
        )
