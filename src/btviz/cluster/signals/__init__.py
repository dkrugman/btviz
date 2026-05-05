"""Concrete signals.

Each module in this package exports one Signal-protocol-conforming
class. ``load_signals`` instantiates them and returns the {name:
signal} mapping that goes into ClusterContext.signals.

Currently shipped:
- rotation_cohort:    temporal handoff scoring (works without DB schema)
- service_uuid_match: 16-bit UUID Jaccard similarity (reads device_ad_history)
- mfg_data_prefix:    manufacturer data prefix match (reads device_ad_history)
- apple_continuity:   Apple Continuity TLV-payload fingerprint (reads device_ad_history)
- co_lifespan_match:  per-session window alignment — co-emission + handoff (reads observations)

Planned (one PR per signal):
- rssi_signature
- adv_interval
- tx_power_match
- status_byte_match
- pdu_distribution
- irk_resolution (cryptographic; gated on IRK import UI)
"""

from __future__ import annotations

from ..base import Signal
from .apple_continuity import AppleContinuity
from .co_lifespan_match import CoLifespanMatch
from .continuity_seq_carryover import ContinuitySeqCarryover
from .mfg_data_prefix import MfgDataPrefix
from .rotation_cohort import RotationCohort
from .service_uuid_match import ServiceUuidMatch


def load_signals() -> dict[str, Signal]:
    """Build the {name: signal} mapping for the current run.

    Honors per-signal enable/disable flags from preferences
    (``cluster.signals.<name>``). A disabled signal is omitted from
    the returned mapping entirely — the runner won't query it, the
    aggregator can't weight it, profiles silently ignore the missing
    weight entry. Toggling requires app restart since signals are
    cached on the canvas's ClusterContext.

    Falls back to "all enabled" if the preferences module isn't
    available (e.g., during early bootstrap or in tests that drive
    the cluster framework without a running app).
    """
    candidates = (
        RotationCohort(),
        ServiceUuidMatch(),
        MfgDataPrefix(),
        AppleContinuity(),
        CoLifespanMatch(),
        ContinuitySeqCarryover(),
    )
    try:
        from ...preferences import get_prefs
        prefs = get_prefs()
        return {
            sig.name: sig
            for sig in candidates
            if bool(prefs.get(f"cluster.signals.{sig.name}"))
        }
    except Exception:  # noqa: BLE001 — preferences unavailable
        return {sig.name: sig for sig in candidates}


__all__ = [
    "AppleContinuity",
    "CoLifespanMatch",
    "ContinuitySeqCarryover",
    "MfgDataPrefix",
    "RotationCohort",
    "ServiceUuidMatch",
    "load_signals",
]
