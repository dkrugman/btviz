"""Active interrogation of BLE devices via the connectivity-firmware DK.

See ``docs/active_interrogation/`` for the design. v1 scope is
manual-only Tier-1 reads (GAP + Device Information + service list)
with one in-flight probe at a time.
"""
from __future__ import annotations

from .types import (
    GattCharObservation,
    GattService,
    ProbeOutcome,
    ProbeRequest,
    ProbeResult,
)

__all__ = [
    "GattCharObservation",
    "GattService",
    "ProbeOutcome",
    "ProbeRequest",
    "ProbeResult",
]
