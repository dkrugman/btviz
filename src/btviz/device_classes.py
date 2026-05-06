"""Canonical set of device-class strings.

A single frozenset of every ``device_class`` value the rest of the
codebase is allowed to assign — by auto-detection or by user override.
Centralizing the list lets the user-override picker show a curated
menu, and lets future schema-level CHECK constraints reject typos.

Three populations contribute:

  1. **Auto-detected** classes produced by ``decode/appearance.py``
     (GAP-Appearance category fallback) and ``decode/apple_continuity.py``
     (Continuity sub-type classifier).
  2. **Manual-override targets** — classes the auto-detection layer
     never produces but that exist as visual identities the user can
     refine to (e.g., ``iphone`` and ``ipad`` are never set by
     ``classify_apple()``, which only ever returns the generic
     ``apple_device``; the icons exist precisely so the user can
     refine ``apple_device`` → the specific model). Likewise
     ``camera``, ``headphones``, ``windows_computer``.
  3. **Curated additions** — useful labels not produced by either
     layer (``auracast_source`` for Avantree-style transmitters).

Icons live separately under ``src/btviz/data/icons/<class>.svg`` and
are looked up by exact filename match. Classes without an icon fall
back to ``fallback_icon.svg`` — that's by design, so the class
taxonomy can grow ahead of the icon set.

``DEVICE_CLASS_LABELS`` exposes the picker-dialog presentation
override (underscores → spaces, etc.). When a class needs a
human-readable form that isn't just ``s.replace("_", " ")``, add it
here.
"""
from __future__ import annotations


# Auto-detected by decode/appearance.py via GAP Appearance categories.
_APPEARANCE_CLASSES: frozenset[str] = frozenset({
    "phone",
    "computer",
    "watch",
    "clock",
    "display",
    "remote_control",
    "eyewear",
    "tag",
    "keyring",
    "media_player",
    "barcode_scanner",
    "thermometer",
    "heart_rate_sensor",
    "blood_pressure_monitor",
    "hid",
    "hid_keyboard",
    "hid_mouse",
    "hid_joystick",
    "hid_gamepad",
    "glucose_meter",
    "running_walking_sensor",
    "cycling_sensor",
    "pulse_oximeter",
    "weight_scale",
    "personal_mobility_device",
    "continuous_glucose_monitor",
    "insulin_pump",
    "medication_delivery",
    "hearing_aid",
    "fitness_tracker",
})

# Auto-detected by decode/apple_continuity.py classify().
_APPLE_CONTINUITY_CLASSES: frozenset[str] = frozenset({
    "airpods",
    "airtag",
    "apple_watch",
    "mac",
    "apple_device",
    "apple_airplay",
    "homekit",
    "ibeacon",
})

# Never produced by auto-detection but available as user-override
# targets (typically because the auto-classifier returns a coarser
# label and the user wants to refine).
_MANUAL_TARGETS: frozenset[str] = frozenset({
    "iphone",
    "ipad",
    "headphones",
    "camera",
    "windows_computer",
    "auracast_source",
})


DEVICE_CLASSES: frozenset[str] = (
    _APPEARANCE_CLASSES | _APPLE_CONTINUITY_CLASSES | _MANUAL_TARGETS
)


# Display-label overrides for the picker dialog. Most classes render
# correctly via ``.replace("_", " ")``; entries here win when the
# default substitution would be awkward. Keep keys in DEVICE_CLASSES.
DEVICE_CLASS_LABELS: dict[str, str] = {
    "hid": "HID (generic)",
    "hid_keyboard": "HID keyboard",
    "hid_mouse": "HID mouse",
    "hid_joystick": "HID joystick",
    "hid_gamepad": "HID gamepad",
}


def display_label(device_class: str) -> str:
    """Human-readable label for a class string. Underscores become
    spaces unless an explicit override is registered above."""
    override = DEVICE_CLASS_LABELS.get(device_class)
    if override is not None:
        return override
    return device_class.replace("_", " ")
