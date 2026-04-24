"""Per-device runtime record."""
from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class Device:
    address: str                    # primary MAC (may rotate if RPA)
    address_type: str               # public | random_static | rpa | nrpa
    first_seen: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    packet_count: int = 0
    last_rssi: int | None = None
    last_channel: int | None = None
    local_name: str | None = None
    company_id: int | None = None    # from manufacturer data, if any
    services_16: set[int] = field(default_factory=set)
    flags: int | None = None
    pdu_types: set[str] = field(default_factory=set)
    # Tier-based name will go here later; for now the inventory shows
    # local_name or address.

    @property
    def display_name(self) -> str:
        return self.local_name or f"({self.address_type}) {self.address}"
