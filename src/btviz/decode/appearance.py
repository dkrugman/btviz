"""GAP Appearance (AD type 0x19) → device_class fallback.

The Bluetooth Core Spec defines a 16-bit Appearance value carried in the
`Appearance` AD entry (and as GATT characteristic 0x2A01). The upper 10
bits encode a category (e.g. 0x029 = Hearing Aid); the lower 6 bits an
optional subcategory (e.g. 0x29-0x42 = Behind-the-ear).

We use this only as a fallback when more specific identity sources (Apple
Continuity sub-types, vendor lookups) didn't classify the device. The
mapping is deliberately category-level — subcategory granularity rarely
adds debugging value here.

Source-of-truth: Bluetooth SIG Assigned Numbers.
  https://www.bluetooth.com/specifications/assigned-numbers/
"""
from __future__ import annotations

# HID subcategory (lower 6 bits of category 0x00F) -> device_class.
# Lets keyboards / mice / joysticks / gamepads pick distinct icons even
# though they share the top-level "hid" category.
_HID_SUBCATEGORY_TO_CLASS: dict[int, str] = {
    0x01: "hid_keyboard",
    0x02: "hid_mouse",
    0x03: "hid_joystick",
    0x04: "hid_gamepad",
    # 0x05: digitizer tablet, 0x06: card reader, 0x07: digital pen,
    # 0x08: barcode scanner — fall through to generic "hid".
}


# Category id (upper 10 bits of the 16-bit appearance) -> device_class.
# Limited to categories that turn up in BLE traffic; extend as needed.
_CATEGORY_TO_CLASS: dict[int, str] = {
    0x001: "phone",
    0x002: "computer",
    0x003: "watch",
    0x004: "clock",
    0x005: "display",
    0x006: "remote_control",
    0x007: "eyewear",
    0x008: "tag",
    0x009: "keyring",
    0x00A: "media_player",
    0x00B: "barcode_scanner",
    0x00C: "thermometer",
    0x00D: "heart_rate_sensor",
    0x00E: "blood_pressure_monitor",
    0x00F: "hid",                       # keyboard / mouse / joystick / gamepad
    0x010: "glucose_meter",
    0x011: "running_walking_sensor",
    0x012: "cycling_sensor",
    0x014: "pulse_oximeter",
    0x015: "weight_scale",
    0x016: "personal_mobility_device",
    0x017: "continuous_glucose_monitor",
    0x018: "insulin_pump",
    0x019: "medication_delivery",
    0x029: "hearing_aid",
    0x031: "fitness_tracker",
}


def appearance_to_class(appearance: int | None) -> str | None:
    """Map a 16-bit GAP Appearance value to a device_class, or None."""
    if appearance is None:
        return None
    category = (appearance >> 6) & 0x3FF
    if category == 0x00F:                 # HID — dispatch on subcategory
        sub = appearance & 0x3F
        return _HID_SUBCATEGORY_TO_CLASS.get(sub, "hid")
    return _CATEGORY_TO_CLASS.get(category)
