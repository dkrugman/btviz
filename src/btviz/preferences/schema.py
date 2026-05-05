"""Single source of truth for every user preference.

Adding a new knob:

1. Append a ``Field`` entry to ``SCHEMA``.
2. Read it via ``get_prefs().get(field.key)`` at the consumption site.

Both the dialog UI and the on-disk TOML layout are derived from
this schema. There is no "register this knob with the dialog"
step — the schema *is* the registration.

Field shape:

* ``key``        — dotted path used by callers (``"cluster.max_per_class"``).
* ``file``       — which TOML it lives in (``general``, ``capture``,
                   ``cluster``, ``canvas``, ``probe``).
* ``section``    — ``[section]`` header in that TOML.
* ``name``       — leaf key in the TOML.
* ``type``       — ``int`` / ``float`` / ``bool`` / ``str``.
* ``default``    — fallback when neither TOML nor env supplies a value.
* ``label``      — human-readable label for the dialog.
* ``description``— tooltip / help text.
* ``min`` / ``max`` — numeric range (inclusive). Out-of-range values
                   in the TOML fall back to default with a log warning.
* ``enum``       — fixed set of allowed values. Renders as a combobox.
* ``env``        — env var that overrides this preference. Mostly for
                   the legacy ``BTVIZ_DB_PATH`` / ``BTVIZ_NRF_EXTCAP``
                   contracts; new fields shouldn't grow env support
                   unless there's a CI / dev-loop need.
* ``requires_restart`` — flagged in the dialog so users know a save
                   won't take effect mid-session.
* ``ui_kind``    — usually inferred from type/enum, but path-style
                   strings need an explicit hint to render a Browse
                   button. Allowed values: ``"path"`` (file or dir),
                   ``"text"`` (free-form string).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Field:
    key: str
    file: str
    section: str
    name: str
    type: type
    default: Any
    label: str
    description: str
    min: float | None = None
    max: float | None = None
    enum: tuple[Any, ...] | None = None
    env: str | None = None
    requires_restart: bool = False
    ui_kind: str | None = None


# Default DB / log paths — computed lazily because they depend on
# the host platform. The loader resolves these via the helper, not
# the value here.
_PATH_DEFAULTS = {
    "db_path": "<platform default>",      # see preferences/loader.py
    "log_dir": "~/.btviz",
}


SCHEMA: tuple[Field, ...] = (
    # ─── general ──────────────────────────────────────────────────────
    Field(
        key="general.db_path",
        file="general", section="paths", name="db_path",
        type=str, default=_PATH_DEFAULTS["db_path"],
        label="Database file",
        description=(
            "SQLite file btviz reads/writes. Defaults to the platform's "
            "Application Support directory. Changing this requires a "
            "btviz restart."
        ),
        env="BTVIZ_DB_PATH", requires_restart=True, ui_kind="path",
    ),
    Field(
        key="general.log_dir",
        file="general", section="paths", name="log_dir",
        type=str, default=_PATH_DEFAULTS["log_dir"],
        label="Log directory",
        description=(
            "Where rotating log files (cluster.log, capture.log) are "
            "written. Restart required."
        ),
        requires_restart=True, ui_kind="path",
    ),
    Field(
        key="general.nrf_extcap_path",
        file="general", section="paths", name="nrf_extcap_path",
        type=str, default="",
        label="Nordic extcap binary",
        description=(
            "Override path to nrf_sniffer_ble.sh / .py. Empty = auto-"
            "discover from the standard Wireshark plugin locations."
        ),
        env="BTVIZ_NRF_EXTCAP", requires_restart=True, ui_kind="path",
    ),

    # ─── capture: sniffer subprocess flags ────────────────────────────
    Field(
        key="capture.only_advertising",
        file="capture", section="sniffer_flags", name="only_advertising",
        type=bool, default=True,
        label="Only advertising packets",
        description=(
            "Pass --only-advertising to the Nordic extcap. Filters out "
            "data-channel traffic at the firmware level."
        ),
    ),
    Field(
        key="capture.only_legacy_advertising",
        file="capture", section="sniffer_flags", name="only_legacy_advertising",
        type=bool, default=False,
        label="Only legacy advertising",
        description=(
            "Pass --only-legacy-advertising. Disables Bluetooth 5.0+ "
            "extended advertising capture."
        ),
    ),
    Field(
        key="capture.scan_follow_rsp",
        file="capture", section="sniffer_flags", name="scan_follow_rsp",
        type=bool, default=True,
        label="Follow scan responses",
        description=(
            "Capture SCAN_RSP packets that follow each adv. Most useful "
            "device-name evidence comes from these."
        ),
    ),
    Field(
        key="capture.scan_follow_aux",
        file="capture", section="sniffer_flags", name="scan_follow_aux",
        type=bool, default=True,
        label="Follow extended-adv AUX_*",
        description=(
            "Hop to secondary channels when an AUX_ADV_IND points there. "
            "Required to see most extended-advertising payloads."
        ),
    ),
    Field(
        key="capture.coded_phy",
        file="capture", section="sniffer_flags", name="coded_phy",
        type=bool, default=False,
        label="Capture on coded PHY",
        description=(
            "Pass --coded. Enables long-range Coded PHY capture. Most "
            "real-world devices use 1M PHY; leave off unless probing "
            "long-range beacons."
        ),
    ),

    # ─── capture: stall watchdog ──────────────────────────────────────
    Field(
        key="watchdog.stall_threshold_s",
        file="capture", section="watchdog", name="stall_threshold_s",
        type=float, default=60.0, min=10.0, max=600.0,
        label="Stall threshold (s)",
        description=(
            "Sniffer is declared stalled after this many seconds of "
            "silence. Default 60 s catches USB-CDC wedges; lower "
            "values may false-positive in RF-quiet environments."
        ),
    ),
    Field(
        key="watchdog.period_s",
        file="capture", section="watchdog", name="period_s",
        type=float, default=10.0, min=1.0, max=120.0,
        label="Watchdog period (s)",
        description=(
            "How often the watchdog walks the sniffers. Cheap operation; "
            "shouldn't normally need tuning."
        ),
    ),
    Field(
        key="watchdog.max_attempts",
        file="capture", section="watchdog", name="max_attempts",
        type=int, default=3, min=1, max=10,
        label="Max restart attempts",
        description=(
            "After this many failed restart attempts the watchdog gives "
            "up on the sniffer; the panel surfaces a 'replug required' "
            "indicator."
        ),
    ),
    Field(
        key="watchdog.min_gap_s",
        file="capture", section="watchdog", name="min_gap_s",
        type=float, default=30.0, min=5.0, max=300.0,
        label="Min gap between restarts (s)",
        description=(
            "Don't try to restart a sniffer twice within this window. "
            "Prevents tight-loop thrash when the kernel CDC endpoint "
            "is wedged."
        ),
    ),

    # ─── cluster runner ───────────────────────────────────────────────
    Field(
        key="cluster.max_per_class",
        file="cluster", section="runner", name="max_per_class",
        type=int, default=1500, min=100, max=20000,
        label="Max devices per class",
        description=(
            "Classes exceeding this are skipped before pair iteration "
            "to prevent O(N²) blow-up. Skipped classes still appear "
            "as 1-element clusters."
        ),
    ),
    Field(
        key="cluster.collapse_threshold",
        file="cluster", section="runner", name="collapse_threshold",
        type=float, default=0.9, min=0.5, max=1.0,
        label="Collapse confidence threshold",
        description=(
            "Cluster members are collapsed onto one canvas card only "
            "when every pair-edge score is at or above this value. "
            "Higher = more conservative merges."
        ),
    ),

    # ─── canvas / display ─────────────────────────────────────────────
    Field(
        key="canvas.stale_window_default_s",
        file="canvas", section="display", name="stale_window_default_s",
        type=float, default=60.0, min=10.0, max=86400.0,
        label="Default 'Show:' window (s)",
        description=(
            "Initial value for the toolbar's stale-window selector. "
            "Devices last seen longer ago than this are hidden."
        ),
    ),
    Field(
        key="canvas.recency_fresh_s",
        file="canvas", section="aging", name="recency_fresh_s",
        type=float, default=60.0, min=1.0, max=3600.0,
        label="Recency: fresh (s)",
        description=(
            "Devices seen within this window paint at full opacity."
        ),
    ),
    Field(
        key="canvas.recency_dormant_s",
        file="canvas", section="aging", name="recency_dormant_s",
        type=float, default=86400.0, min=60.0, max=2592000.0,
        label="Recency: dormant (s)",
        description=(
            "Devices not seen for this long fade to the minimum opacity. "
            "Aging is interpolated between fresh and dormant."
        ),
    ),
    Field(
        key="canvas.recency_min_opacity",
        file="canvas", section="aging", name="recency_min_opacity",
        type=float, default=0.10, min=0.0, max=1.0,
        label="Minimum opacity",
        description=(
            "Floor for fade-out. Set above 0 so old devices remain "
            "visible (but ghosted) on the canvas."
        ),
    ),
    Field(
        key="canvas.stable_class_min_packets",
        file="canvas", section="display", name="stable_class_min_packets",
        type=int, default=500, min=1, max=100000,
        label="Stable-section min packets",
        description=(
            "RPA devices need at least this many packets to be "
            "promoted to the stable 'Devices' section. Lower lets "
            "newly-seen RPAs surface faster."
        ),
    ),
)


def fields_for_file(file: str) -> tuple[Field, ...]:
    return tuple(f for f in SCHEMA if f.file == file)


def files() -> tuple[str, ...]:
    """Distinct TOML file names referenced by the schema, in order."""
    seen: list[str] = []
    for f in SCHEMA:
        if f.file not in seen:
            seen.append(f.file)
    return tuple(seen)


def by_key(key: str) -> Field | None:
    for f in SCHEMA:
        if f.key == key:
            return f
    return None
