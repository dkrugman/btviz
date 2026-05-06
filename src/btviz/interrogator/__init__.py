"""Active-interrogation driver.

The interrogator dedicates one Nordic dongle (running the
Connectivity firmware, *not* the Sniffer firmware) to issuing
targeted BLE primitives at observed RPAs:

  * SCAN_REQ → SCAN_RSP harvest (v1, this scaffold's target).
  * Later: GATT-discovery for service UUID + characteristic
    enumeration; SMP ``Pairing_Public_Key`` collect for
    crypto-identity hashing.

Each attempt is logged into ``device_interrogation_log`` (see the
:class:`btviz.db.repos.Interrogations` repo). The interrogator's
own broadcaster address is published to ``LiveIngest`` so its own
SCAN_REQs aren't double-counted as third-party traffic by the
passive sniffers (see ``LiveIngest.set_own_interrogator_addresses``).

This first cut wires up the surface area — role enum, process
skeleton, repo, dedup hook — *without* the actual
``pc-ble-driver-py`` integration. The real radio call is gated
behind :py:meth:`InterrogatorProcess.request_scan_response` raising
:class:`InterrogatorNotImplemented`. The DK board needs Connectivity
firmware reflashed before that path can light up; once it has,
filling the stub is one method.
"""
from __future__ import annotations

from .process import (
    InterrogatorNotImplemented,
    InterrogatorProcess,
    ScanResponseResult,
)

__all__ = [
    "InterrogatorNotImplemented",
    "InterrogatorProcess",
    "ScanResponseResult",
]
