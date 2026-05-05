"""SIG-assigned BLE GATT UUIDs and human names.

Static lookup. Kept in code rather than in the DB so it travels
with the codebase and gets reviewed via PR rather than via a seed
migration.

Only the UUIDs btviz actually reads in v1 (Tier 1 — GAP + Device
Information) are seeded. Other SIG services (Heart Rate, Battery,
HID, LE Audio's ASCS / BASS / MICS, etc.) get added as we extend
into Tier 2 reads.

UUIDs are the full 128-bit form. The 16-bit "short" UUIDs are
expanded against the SIG base UUID
``0000xxxx-0000-1000-8000-00805f9b34fb``.
"""
from __future__ import annotations

_BASE = "0000{:04x}-0000-1000-8000-00805f9b34fb"


def _u16(short: int) -> str:
    return _BASE.format(short)


# --- services ---------------------------------------------------------

SVC_GAP = _u16(0x1800)
SVC_GATT = _u16(0x1801)
SVC_DEVICE_INFO = _u16(0x180A)


# --- characteristics --------------------------------------------------

# GAP service
CHAR_DEVICE_NAME = _u16(0x2A00)
CHAR_APPEARANCE = _u16(0x2A01)
CHAR_PERIPHERAL_PREFERRED_CONN_PARAMS = _u16(0x2A04)

# Device Information service
CHAR_MANUFACTURER_NAME = _u16(0x2A29)
CHAR_MODEL_NUMBER = _u16(0x2A24)
CHAR_SERIAL_NUMBER = _u16(0x2A25)
CHAR_HARDWARE_REVISION = _u16(0x2A27)
CHAR_FIRMWARE_REVISION = _u16(0x2A26)
CHAR_SOFTWARE_REVISION = _u16(0x2A28)
CHAR_SYSTEM_ID = _u16(0x2A23)
CHAR_PNP_ID = _u16(0x2A50)


HUMAN_NAMES: dict[str, str] = {
    SVC_GAP: "Generic Access",
    SVC_GATT: "Generic Attribute",
    SVC_DEVICE_INFO: "Device Information",

    CHAR_DEVICE_NAME: "Device Name",
    CHAR_APPEARANCE: "Appearance",
    CHAR_PERIPHERAL_PREFERRED_CONN_PARAMS: "Peripheral Preferred Connection Parameters",

    CHAR_MANUFACTURER_NAME: "Manufacturer Name",
    CHAR_MODEL_NUMBER: "Model Number",
    CHAR_SERIAL_NUMBER: "Serial Number",
    CHAR_HARDWARE_REVISION: "Hardware Revision",
    CHAR_FIRMWARE_REVISION: "Firmware Revision",
    CHAR_SOFTWARE_REVISION: "Software Revision",
    CHAR_SYSTEM_ID: "System ID",
    CHAR_PNP_ID: "PnP ID",
}


# --- Tier 1 read targets (executed by every probe by default) ---------

TIER1_CHARS: tuple[str, ...] = (
    CHAR_DEVICE_NAME,
    CHAR_APPEARANCE,
    CHAR_MANUFACTURER_NAME,
    CHAR_MODEL_NUMBER,
    CHAR_SERIAL_NUMBER,
    CHAR_HARDWARE_REVISION,
    CHAR_FIRMWARE_REVISION,
    CHAR_SOFTWARE_REVISION,
    CHAR_PNP_ID,
)


def human_name(uuid: str) -> str:
    """Return a human-readable name for a UUID, or the UUID itself."""
    return HUMAN_NAMES.get(uuid.lower(), uuid)
