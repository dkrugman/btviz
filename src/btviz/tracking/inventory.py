"""Live device inventory built from advertising packets."""
from __future__ import annotations

import struct
import time
from threading import RLock

from ..bus import EventBus, TOPIC_DEVICE_UPSERT, TOPIC_PACKET
from ..capture.packet import Packet
from ..decode.adv import (
    AD_COMPLETE_LOCAL_NAME,
    AD_COMPLETE_LIST_16,
    AD_FLAGS,
    AD_INCOMPLETE_LIST_16,
    AD_MANUFACTURER_DATA,
    AD_SHORTENED_LOCAL_NAME,
    classify_address,
    decode_phdr_packet,
    parse_ad_structures,
)
from .device import Device


class Inventory:
    """Address-keyed device store. Naming/identity merging comes later."""

    def __init__(self, bus: EventBus) -> None:
        self.bus = bus
        self._devices: dict[str, Device] = {}
        self._lock = RLock()
        bus.subscribe(TOPIC_PACKET, self._on_packet)

    def snapshot(self) -> list[Device]:
        with self._lock:
            return list(self._devices.values())

    # ------------------------------------------------------------------

    def _on_packet(self, pkt: Packet) -> None:
        decoded = decode_phdr_packet(pkt.raw)
        if decoded is None or decoded.adv_addr is None:
            return

        addr_type = classify_address(decoded.adv_addr, decoded.tx_add_random)
        with self._lock:
            dev = self._devices.get(decoded.adv_addr)
            if dev is None:
                dev = Device(address=decoded.adv_addr, address_type=addr_type)
                self._devices[decoded.adv_addr] = dev
            dev.last_seen = time.time()
            dev.packet_count += 1
            dev.last_rssi = decoded.rssi
            dev.last_channel = decoded.channel
            dev.pdu_types.add(decoded.pdu_type)

            for ad_type, value in parse_ad_structures(decoded.adv_data):
                if ad_type in (AD_COMPLETE_LOCAL_NAME, AD_SHORTENED_LOCAL_NAME):
                    try:
                        dev.local_name = value.decode("utf-8", errors="replace")
                    except Exception:  # noqa: BLE001
                        pass
                elif ad_type == AD_FLAGS and len(value) >= 1:
                    dev.flags = value[0]
                elif ad_type in (AD_COMPLETE_LIST_16, AD_INCOMPLETE_LIST_16):
                    for off in range(0, len(value) - 1, 2):
                        dev.services_16.add(struct.unpack("<H", value[off:off + 2])[0])
                elif ad_type == AD_MANUFACTURER_DATA and len(value) >= 2:
                    dev.company_id = struct.unpack("<H", value[:2])[0]

        self.bus.publish(TOPIC_DEVICE_UPSERT, dev)
