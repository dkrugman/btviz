"""Concrete signals.

Each module in this package exports one Signal-protocol-conforming
class. ``load_signals`` instantiates them and returns the {name:
signal} mapping that goes into ClusterContext.signals.

Currently shipped:
- rotation_cohort: temporal handoff scoring (works without DB schema)

Planned (one PR per signal):
- rssi_signature
- adv_interval
- service_uuid_match
- mfg_data_prefix
- apple_continuity
- tx_power_match
- status_byte_match
- pdu_distribution
- irk_resolution (cryptographic; gated on IRK import UI)
"""

from __future__ import annotations

from ..base import Signal
from .rotation_cohort import RotationCohort


def load_signals() -> dict[str, Signal]:
    return {sig.name: sig for sig in (RotationCohort(),)}


__all__ = ["RotationCohort", "load_signals"]
