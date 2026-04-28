"""Concrete signals.

Each module in this package exports one Signal-protocol-conforming
class. ``load_signals`` instantiates them and returns the {name:
signal} mapping that goes into ClusterContext.signals.

Currently shipped:
- rotation_cohort:   temporal handoff scoring (works without DB schema)
- service_uuid_match: 16-bit UUID Jaccard similarity (reads device_ad_history)
- mfg_data_prefix:   manufacturer data prefix match (reads device_ad_history)

Planned (one PR per signal):
- rssi_signature
- adv_interval
- apple_continuity
- tx_power_match
- status_byte_match
- pdu_distribution
- irk_resolution (cryptographic; gated on IRK import UI)
"""

from __future__ import annotations

from ..base import Signal
from .mfg_data_prefix import MfgDataPrefix
from .rotation_cohort import RotationCohort
from .service_uuid_match import ServiceUuidMatch


def load_signals() -> dict[str, Signal]:
    return {sig.name: sig for sig in (
        RotationCohort(),
        ServiceUuidMatch(),
        MfgDataPrefix(),
    )}


__all__ = ["MfgDataPrefix", "RotationCohort", "ServiceUuidMatch", "load_signals"]
