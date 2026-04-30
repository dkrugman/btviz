"""Normalized packet representation post-decode."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Packet:
    ts: float                      # seconds since epoch (or capture start)
    source: str                    # dongle short id
    channel: int | None = None     # BLE channel index (0-39), if known
    rssi: int | None = None
    phy: str | None = None         # "1M" | "2M" | "Coded"
    pdu_type: str | None = None    # "ADV_IND", "CONNECT_IND", etc.
    adv_addr: str | None = None    # advertiser address, lowercase colons
    adv_addr_type: str | None = None  # "public" | "random_static" | "rpa" | "nrpa"
    init_addr: str | None = None
    target_addr: str | None = None
    adv_data: bytes | None = None
    raw: bytes = b""
    extras: dict[str, Any] = field(default_factory=dict)
    # Firmware-reported CRC validity. False means the radio received
    # bytes but the LL-frame CRC check failed — bit-error corruption
    # somewhere in the payload. The captured fields beyond ``channel``
    # and ``rssi`` may not be trustworthy on a CRC-failed packet
    # (the address bits in particular often differ from a real one
    # by 1-4 positions). Used by the panel to render a distinct
    # "dropout" flash without ever attributing the packet to a device.
    crc_ok: bool = True
