"""Canvas UI: per-project device board.

A QGraphicsScene populated from the DB. Each device is a draggable
``DeviceItem`` that can toggle between a compact summary and a detailed
view. Layout persists to the ``device_layouts`` table on drag-end.

Entry point: ``run_canvas(db_path=None, project_name=None)``. Called by
``btviz canvas`` in ``__main__.py``. The live-capture sniffer panel
docks into the same QMainWindow.
"""
from __future__ import annotations

import json
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PySide6.QtCore import (
    QObject,
    QPointF,
    QRectF,
    Qt,
    QThread,
    QTimer,
    Signal,
    Slot,
)
from PySide6.QtGui import (
    QAction,
    QBrush,
    QColor,
    QFont,
    QFontMetricsF,
    QLinearGradient,
    QPainter,
    QPen,
    QPolygonF,
)
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QGraphicsItem,
    QGraphicsLineItem,
    QGraphicsScene,
    QGraphicsSimpleTextItem,
    QDialog,
    QDialogButtonBox,
    QGraphicsView,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QPushButton,
    QSizePolicy,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from ..bus import EventBus, TOPIC_SNIFFER_STATE
from ..device_classes import DEVICE_CLASSES, display_label as device_class_label
from ..capture.coordinator import CaptureCoordinator, FollowRequest
from ..capture.live_ingest import LiveIngest
from ..db.models import DeviceLayout
from ..db.repos import Repos
from ..db.store import Store, open_store
from .channel_colors import (
    channel_label as _channel_label,
    color_for_channel as _channel_color,
    text_color_for_channel as _channel_text_color,
)

# Bundled SVG icons (optional). Drop ``<device_class>.svg`` here and the
# canvas renders it instead of the emoji fallback. See data/icons/README.md
# for the file-naming convention.
_ICONS_DIR = Path(__file__).resolve().parent.parent / "data" / "icons"
_renderer_cache: dict[str, QSvgRenderer | None] = {}


def _icon_renderer(device_class: str | None) -> QSvgRenderer | None:
    """Return a cached ``QSvgRenderer`` for ``device_class``, or None.

    Returns None when no SVG exists (or the file fails to parse), so the
    caller can fall back to the emoji table. Renderers are cached per-class
    for the life of the process — they're cheap once loaded.
    """
    if not device_class:
        return None
    if device_class in _renderer_cache:
        return _renderer_cache[device_class]
    path = _ICONS_DIR / f"{device_class}.svg"
    if not path.exists():
        _renderer_cache[device_class] = None
        return None
    r = QSvgRenderer(str(path))
    _renderer_cache[device_class] = r if r.isValid() else None
    return _renderer_cache[device_class]

# Visual constants. Tuned for a ~1400×900 starting window.
_BOX_W = 220
_HEADER_H = 50
# Collapsed body now holds three lines: summary, quality counters, and a
# gauge icon strip. Expanded body grew by the same amount so the detail
# block keeps the same vertical space it had before.
_BOX_H_COLLAPSED = _HEADER_H + 76
_BOX_H_EXPANDED = _HEADER_H + 242
_BOX_RADIUS = 10
_GRID_DX = _BOX_W + 24               # column pitch (box + gutter)
_GRID_DY = _BOX_H_COLLAPSED + 22     # row pitch (collapsed-box + gutter)
_GRID_MARGIN_X = 20                  # left margin before the first column

# Two-section canvas: the top "Devices" zone holds stable identities
# (public/static MACs, IRK-resolved devices, cluster primaries that
# absorbed RPAs, user-named devices) and the bottom "Unidentified
# Advertisements" zone holds everything else (unresolved RPAs,
# nrpas, unknowns). Both are always rendered with their labels even
# when empty so the user always knows where new devices will appear.
_SECTION_TOP_LABEL = "Devices"
_SECTION_BOTTOM_LABEL = "Unidentified Advertisements"


def _section_label_with_count(base: str, count: int) -> str:
    """Append "(N)" to a section label, or leave the base bare for 0.

    The empty-section state already shows a "(no stable devices yet)"
    placeholder; appending "(0)" to the heading on top of that reads
    redundant, so we drop the parenthesis entirely in that case.
    """
    return f"{base} ({count})" if count else base
_SECTION_LABEL_FONT_PT = 11
_SECTION_LABEL_H = 22
_SECTION_LABEL_LEFT = 20
_SECTION_GAP_BEFORE_DIVIDER = 14
_SECTION_GAP_AFTER_DIVIDER = 10
_SECTION_PLACEHOLDER_H = _BOX_H_COLLAPSED  # min content area when empty
_SECTION_DIVIDER_COLOR = QColor(170, 170, 180)
_SECTION_LABEL_COLOR = QColor(60, 60, 80)
_SECTION_PLACEHOLDER_COLOR = QColor(150, 150, 160)
# Viewport-responsive column counts are clamped between these so a
# pathologically narrow window still places one column per row, and a
# wide one doesn't spread devices so far apart that they're tedious to
# scan.
_GRID_COLS_MIN = 1
_GRID_COLS_MAX = 12
# Fallback when no viewport width is available (headless / pre-show
# initialization). Matches the previous fixed default.
_GRID_COLS_DEFAULT = 6

# Z-stacking. Expanded boxes sit above collapsed ones so their detail
# region isn't occluded by neighbors. The actively-dragged item rises
# above everything during the drag, then settles back to its
# state-appropriate level on release.
_Z_NORMAL = 1
_Z_EXPANDED = 10
_Z_DRAGGING = 100
# Section labels and the divider line sit ABOVE all device boxes so
# they're never occluded — even by an expanded box that extends up
# into the heading area.
_Z_SECTION_DECOR = 200

# Colors by address kind. Muted so text stays readable.
_KIND_FILL = {
    "public_mac": QColor(210, 235, 210),
    "random_static_mac": QColor(220, 225, 245),
    "unresolved_rpa": QColor(245, 230, 215),
    "nrpa": QColor(230, 230, 230),
    "irk_identity": QColor(210, 235, 230),
    "unknown": QColor(235, 235, 235),
}

_FALLBACK_SVG_NAME = "fallback_icon"  # data/icons/fallback_icon.svg — unknown classes

# Channel-activity flash on each device box. The badge appears in the
# top-right corner of the header for this duration after each packet
# attributed to the device. 0.6 s matches the sniffer-panel dot-flash
# window so spectrum activity reads consistently across the two UIs.
_CHANNEL_FLASH_DURATION_S = 0.6
_CH_FLASH_BADGE_W = 34
_CH_FLASH_BADGE_H = 18
_CH_FLASH_BADGE_MARGIN = 6
_CH_FLASH_TRAIL_W = 6   # width of each comet-tail prior-channel pip

# Advertising-channel strip (37/38/39): three small squares placed below
# the data-channel badge in the header. Splits adv vs data activity so a
# device sitting on a primary channel doesn't drown out occasional
# data-channel hits (or vice-versa). Each square is independently faded.
_ADV_CH_BOX_W = 14
_ADV_CH_BOX_H = 14
_ADV_CH_BOX_GAP = 2
_ADV_STRIP_TOP_Y = (
    _CH_FLASH_BADGE_MARGIN + _CH_FLASH_BADGE_H + 4
)

# Dropout flash (CRC-fail). When a packet is reported but its CRC didn't
# verify we paint the indicator black with a red number — same treatment
# the sniffer-panel channel tag uses, so the two UIs read alike.
_FLASH_DROPOUT_BG = QColor(20, 20, 28)
_FLASH_DROPOUT_FG = QColor(230, 70, 70)

# Quality bar at the bottom of the device-box body. Horizontal bar
# whose left segment (green) is the cumulative good-packet share and
# right segment (red) is the CRC-fail share. A small caret marks the
# boundary; the good-percentage prints below the caret.
_QUALITY_GREEN = QColor(60, 180, 90)
_QUALITY_RED = QColor(220, 70, 70)
_QUALITY_NEUTRAL = QColor(170, 170, 170)  # before any packets observed
_QUALITY_BAR_H = 8
_QUALITY_BAR_RADIUS = 2
_QUALITY_LABEL_W = 44      # width reserved for the "Quality" / "Signal" label
_QUALITY_CARET_W = 6       # base width of the caret triangle
_QUALITY_CARET_H = 4
_QUALITY_CARET_FILL = QColor(40, 40, 50)

# Signal-strength bar — same visual vocabulary as the quality bar but
# paints the [-100..0] dBm RSSI range as a red→green gradient with a
# downward caret marking ``rssi_avg``. The dBm label sits ABOVE the
# caret so the bar reads "value first, then where it sits on the
# scale" — opposite of the quality bar (which leads with the bar and
# explains itself with the percentage below).
_SIGNAL_BAR_H = _QUALITY_BAR_H
_SIGNAL_BAR_RADIUS = _QUALITY_BAR_RADIUS
_SIGNAL_RSSI_MIN = -100   # left edge of the bar (no signal)
_SIGNAL_RSSI_MAX = -20    # right edge (effectively touching antenna)
_SIGNAL_LO = QColor(220, 70, 70)     # red at -100 dBm
_SIGNAL_MID = QColor(220, 170, 60)   # amber midpoint
_SIGNAL_HI = QColor(60, 180, 90)     # green at -20 dBm

# Rolling window length for the per-device Signal and Quality meters.
# The meters average packet observations over the last N seconds so the
# bars reflect *current* link health, not session-cumulative aggregates.
# When no packets have arrived inside the window the bar shows neutral
# grey with no caret — honest "no current signal."
_RECENT_WINDOW_S = 5.0

# Cluster-collapse confidence threshold. The canvas hydrator only
# collapses a multi-device cluster into one primary box when *every*
# member's score is at or above this value; weaker clusters keep all
# members visible so the user can verify before the merge becomes
# permanent in their mental model. 0.9 matches the runner's "very
# high confidence" tier — apple_continuity exact-match (1.0) and
# rotation_cohort handoffs near the expected gap clear it; weaker
# evidence does not.
_CLUSTER_COLLAPSE_THRESHOLD = 0.9

# Rank ordering for picking the most-stable identity in a cluster
# as its canvas primary. Public MACs are always preferred (they're
# the real device identity); among RPAs we prefer the longest-lived,
# most-observed row. ``unknown``/``nrpa`` are last-resort.
_CLUSTER_KIND_RANK: dict[str, int] = {
    "public_mac": 0,
    "random_static_mac": 1,
    "irk_identity": 1,
    "rs": 1,
    "unresolved_rpa": 2,
    "nrpa": 3,
    "unknown": 4,
}

# Cluster badge — small "↔ N" chip painted in the top-left corner of
# any device box that's a cluster primary with absorbed siblings.
# Visible across the canvas at a glance so the user can pick out
# "this is one device represented by 4 RPAs" without reading the body.
_CLUSTER_BADGE_BG = QColor(50, 90, 170)      # blue, distinct from kind tints
_CLUSTER_BADGE_FG = QColor(245, 245, 250)
_CLUSTER_BADGE_H = 16
_CLUSTER_BADGE_PAD_X = 5
_CLUSTER_BADGE_MARGIN = 4

# Capture button styling — green pill when idle (Start), red pill when
# capturing (Stop). Padding and rounded corners pull it out of the
# row of plain QToolButton text labels around it so the user's eye
# lands here first.
_CAPTURE_BUTTON_STYLE_IDLE = """
    QToolButton#captureButton {
        background-color: #2d8f4a;
        color: white;
        font-weight: bold;
        font-size: 12pt;
        padding: 5px 16px;
        margin: 2px 6px 2px 4px;
        border-radius: 4px;
        border: 1px solid #267e3e;
    }
    QToolButton#captureButton:hover {
        background-color: #36a558;
    }
    QToolButton#captureButton:pressed {
        background-color: #267e3e;
    }
"""
_CAPTURE_BUTTON_STYLE_CAPTURING = """
    QToolButton#captureButton {
        background-color: #b53a3a;
        color: white;
        font-weight: bold;
        font-size: 12pt;
        padding: 5px 16px;
        margin: 2px 6px 2px 4px;
        border-radius: 4px;
        border: 1px solid #993131;
    }
    QToolButton#captureButton:hover {
        background-color: #c84545;
    }
    QToolButton#captureButton:pressed {
        background-color: #993131;
    }
"""


# ──────────────────────────────────────────────────────────────────────────
# Data loading
# ──────────────────────────────────────────────────────────────────────────

@dataclass
class CanvasDevice:
    """Everything the canvas needs for one device in a project.

    Aggregates observations across all sessions in the project so a device
    seen in multiple captures shows its total activity.
    """
    device_id: int
    stable_key: str
    kind: str
    label: str
    addresses: list[tuple[str, str]] = field(default_factory=list)  # (addr, type)
    vendor: str | None = None
    oui_vendor: str | None = None
    vendor_id: int | None = None
    appearance: int | None = None
    # Effective class — what every consumer (icon, label, cluster
    # profile lookup, tooltip header) should read. ``user_device_class``
    # wins when set; otherwise the auto-detected value flows through.
    device_class: str | None = None
    # Wire-inferred class produced by the auto-detection layer
    # (apple_continuity / appearance fallback). Preserved separately so
    # the tooltip can show the auto value alongside an override and
    # the user can clearly see what btviz inferred independent of any
    # manual pin. NULL when no auto-detection ever fired.
    auto_device_class: str | None = None
    user_device_class: str | None = None
    local_name: str | None = None
    gatt_device_name: str | None = None
    user_name: str | None = None
    model: str | None = None
    # When this device is the source of an Auracast broadcast somewhere in
    # the project, the most recent broadcast_name for that broadcast.
    broadcast_name: str | None = None
    packet_count: int = 0
    adv_count: int = 0
    data_count: int = 0
    # Cumulative CRC-failed packets attributed to this device across
    # all observed sessions (via the live-ingest last-clean-device
    # cache). Drives the right segment of the quality bar; survives
    # capture stop because it's persisted to ``observations``.
    bad_packet_count: int = 0
    rssi_min: int | None = None
    rssi_max: int | None = None
    rssi_avg: float | None = None
    last_seen: float = 0.0
    channels: dict[int, int] = field(default_factory=dict)
    pdu_types: dict[str, int] = field(default_factory=dict)
    # 16-bit service UUIDs the device has been seen advertising
    # (AD types 0x02 incomplete + 0x03 complete, merged). Stable
    # across captures because they're loaded from device_ad_history.
    # Cluster primaries inherit the union across absorbed members.
    service_uuids: list[int] = field(default_factory=list)
    # Cluster membership. ``cluster_id`` is set whenever this device
    # belongs to a row in ``device_cluster_members``; the canvas
    # primary that absorbed N RPAs has ``cluster_member_ids`` populated
    # with the device_ids of the absorbed siblings (counts/channels/
    # addresses below already include their contributions). For
    # low-confidence clusters or unclustered devices the badge fields
    # stay empty so the box renders as a standalone identity.
    cluster_id: int | None = None
    cluster_member_ids: list[int] = field(default_factory=list)
    cluster_min_score: float | None = None
    # Layout
    pos_x: float = 0.0
    pos_y: float = 0.0
    collapsed: bool = True
    hidden: bool = False

    @property
    def cluster_member_count(self) -> int:
        """Total devices in this cluster including the primary."""
        return 1 + len(self.cluster_member_ids)


def _pick_cluster_primary(members: list[CanvasDevice]) -> CanvasDevice:
    """Return the cluster member that should own the canvas box.

    Selection is by stability of identity, then by observation depth:
      1. Lowest ``_CLUSTER_KIND_RANK`` (public MAC > random static > RPA …)
      2. Most packets seen
      3. Most recent ``last_seen`` (a tiebreaker so newly-rotated
         identities don't stick around as primary just because they
         happened to be first to score in the cluster)
    """
    return max(
        members,
        key=lambda d: (
            -_CLUSTER_KIND_RANK.get(d.kind, 99),
            d.packet_count,
            d.last_seen,
        ),
    )


def _absorb_cluster_member(
    primary: CanvasDevice, member: CanvasDevice,
) -> None:
    """Merge ``member``'s observations into ``primary`` in-place.

    RSSI is blended as a packet-count-weighted average so the
    primary's mean RSSI tracks the combined population (an
    approximation — packet_count over-counts the rssi_samples
    denominator slightly, but the existing per-device rssi_avg has
    the same approximation, so consistency wins).

    Channels and PDU-type histograms sum element-wise; the addresses
    list concatenates with dedup. The primary's ``cluster_member_ids``
    grows by one — that's how the badge later knows the count.
    """
    if primary.rssi_avg is not None and member.rssi_avg is not None:
        total = primary.packet_count + member.packet_count
        if total > 0:
            primary.rssi_avg = (
                primary.rssi_avg * primary.packet_count
                + member.rssi_avg * member.packet_count
            ) / total
    elif member.rssi_avg is not None:
        primary.rssi_avg = member.rssi_avg
    if primary.rssi_min is None or (
        member.rssi_min is not None
        and member.rssi_min < primary.rssi_min
    ):
        primary.rssi_min = member.rssi_min
    if primary.rssi_max is None or (
        member.rssi_max is not None
        and member.rssi_max > primary.rssi_max
    ):
        primary.rssi_max = member.rssi_max
    primary.packet_count += member.packet_count
    primary.adv_count += member.adv_count
    primary.data_count += member.data_count
    primary.bad_packet_count += member.bad_packet_count
    primary.last_seen = max(primary.last_seen, member.last_seen)
    for ch, n in member.channels.items():
        primary.channels[ch] = primary.channels.get(ch, 0) + n
    for pdu, n in member.pdu_types.items():
        primary.pdu_types[pdu] = primary.pdu_types.get(pdu, 0) + n
    seen_addrs = {addr for addr, _ in primary.addresses}
    for addr, kind in member.addresses:
        if addr not in seen_addrs:
            primary.addresses.append((addr, kind))
            seen_addrs.add(addr)
    seen_uuids = set(primary.service_uuids)
    for u in member.service_uuids:
        if u not in seen_uuids:
            primary.service_uuids.append(u)
            seen_uuids.add(u)
    if member.broadcast_name and not primary.broadcast_name:
        primary.broadcast_name = member.broadcast_name
    primary.cluster_member_ids.append(member.device_id)


def load_canvas_devices(
    store: Store,
    project_id: int,
    *,
    stale_cutoff: float | None = None,
    stale_session_id: int | None = None,
) -> list[CanvasDevice]:
    """Load all devices observed in the project plus their saved layout.

    ``stale_cutoff`` is an absolute timestamp threshold; devices whose
    latest observation is below it are excluded. ``None`` (default)
    keeps every device the project has ever seen.

    ``stale_session_id`` scopes the freshness check to a single
    session — pass it together with ``stale_cutoff`` so the threshold
    and the per-device MAX live in the same firmware-clock domain.
    Across sessions, dongle replug resets the firmware clock; a
    project-wide MAX would mix domains and either hide currently-
    active devices (if some other session has a higher baseline) or
    keep stale ones visible. With the session scope set, devices not
    observed in that session simply don't pass the staleness check —
    the right answer when the user asks "what's active now".

    Filter applies in SQL via HAVING so the addresses / broadcasts /
    histograms follow-up queries stay scoped to the surviving set.
    """
    conn = store.conn
    having = ""
    params: list = [project_id]
    if stale_cutoff is not None:
        if stale_session_id is not None:
            # Scope the per-device MAX to observations from the
            # given session. CASE returns NULL for rows in other
            # sessions; MAX over all-NULLs is NULL, which never
            # passes ``>= ?`` — so devices with no rows in this
            # session are filtered out as expected.
            having = (
                "HAVING MAX(CASE WHEN o.session_id = ? "
                "THEN o.last_seen END) >= ?"
            )
            params.append(stale_session_id)
            params.append(stale_cutoff)
        else:
            having = "HAVING MAX(o.last_seen) >= ?"
            params.append(stale_cutoff)
    rows = conn.execute(
        f"""
        SELECT
            d.id, d.stable_key, d.kind,
            d.user_name, d.gatt_device_name, d.local_name,
            d.vendor, d.vendor_id, d.oui_vendor, d.model,
            d.device_class AS auto_device_class,
            d.user_device_class,
            COALESCE(d.user_device_class, d.device_class) AS device_class,
            d.appearance, d.last_seen,
            SUM(o.packet_count)     AS packet_count,
            SUM(o.adv_count)        AS adv_count,
            SUM(o.data_count)       AS data_count,
            SUM(o.bad_packet_count) AS bad_packet_count,
            MIN(o.rssi_min)     AS rssi_min,
            MAX(o.rssi_max)     AS rssi_max,
            SUM(o.rssi_sum)     AS rssi_sum_total,
            SUM(o.rssi_samples) AS rssi_n_total,
            MAX(o.last_seen)    AS last_obs
        FROM observations o
        JOIN sessions s ON s.id = o.session_id
        JOIN devices  d ON d.id = o.device_id
        WHERE s.project_id = ?
        GROUP BY d.id
        {having}
        """,
        params,
    ).fetchall()

    devices: dict[int, CanvasDevice] = {}
    for r in rows:
        label = _row_best_label(r)
        rssi_avg = (
            r["rssi_sum_total"] / r["rssi_n_total"]
            if r["rssi_n_total"] else None
        )
        cd = CanvasDevice(
            device_id=r["id"],
            stable_key=r["stable_key"],
            kind=r["kind"],
            label=label,
            vendor=r["vendor"],
            vendor_id=r["vendor_id"],
            oui_vendor=r["oui_vendor"],
            appearance=r["appearance"],
            device_class=r["device_class"],
            auto_device_class=r["auto_device_class"],
            user_device_class=r["user_device_class"],
            local_name=r["local_name"],
            gatt_device_name=r["gatt_device_name"],
            user_name=r["user_name"],
            model=r["model"],
            packet_count=r["packet_count"] or 0,
            adv_count=r["adv_count"] or 0,
            data_count=r["data_count"] or 0,
            bad_packet_count=r["bad_packet_count"] or 0,
            rssi_min=r["rssi_min"],
            rssi_max=r["rssi_max"],
            rssi_avg=rssi_avg,
            last_seen=r["last_obs"] or r["last_seen"],
        )
        devices[cd.device_id] = cd

    if not devices:
        return []

    # Aggregated pdu_type / channel histograms per device.
    placeholders = ",".join("?" * len(devices))
    hist_rows = conn.execute(
        f"""
        SELECT device_id, pdu_types_json, channels_json
          FROM observations o
          JOIN sessions s ON s.id = o.session_id
         WHERE s.project_id = ? AND o.device_id IN ({placeholders})
        """,
        (project_id, *devices.keys()),
    ).fetchall()
    for r in hist_rows:
        cd = devices[r["device_id"]]
        for k, v in json.loads(r["pdu_types_json"]).items():
            cd.pdu_types[k] = cd.pdu_types.get(k, 0) + v
        for k, v in json.loads(r["channels_json"]).items():
            ch = int(k)
            cd.channels[ch] = cd.channels.get(ch, 0) + v

    # All addresses per device.
    addr_rows = conn.execute(
        f"SELECT device_id, address, address_type FROM addresses "
        f"WHERE device_id IN ({placeholders}) ORDER BY last_seen DESC",
        tuple(devices.keys()),
    ).fetchall()
    for r in addr_rows:
        devices[r["device_id"]].addresses.append((r["address"], r["address_type"]))

    # Service UUIDs (AD type 0x02 incomplete / 0x03 complete). Each
    # row in device_ad_history is one UUID stored as 2 bytes
    # little-endian; multiple UUIDs per device produce multiple rows.
    # Aggregate as the unique UUID set per device so the tooltip
    # surfaces what services the device advertised.
    uuid_rows = conn.execute(
        f"SELECT device_id, ad_value FROM device_ad_history "
        f"WHERE ad_type IN (2, 3) AND device_id IN ({placeholders})",
        tuple(devices.keys()),
    ).fetchall()
    for r in uuid_rows:
        val = r["ad_value"]
        if val is None or len(val) < 2:
            continue
        u = val[0] | (val[1] << 8)
        cd = devices[r["device_id"]]
        if u not in cd.service_uuids:
            cd.service_uuids.append(u)

    # Most recent broadcast_name per broadcaster (across all sessions in
    # this project). Walk in last_seen DESC order and keep first per
    # device_id so the freshest name wins. Devices that never broadcast
    # stay with broadcast_name=None.
    bcast_rows = conn.execute(
        f"""
        SELECT b.broadcaster_device_id AS did, b.broadcast_name
          FROM broadcasts b
          JOIN sessions s ON s.id = b.session_id
         WHERE s.project_id = ?
           AND b.broadcaster_device_id IN ({placeholders})
           AND b.broadcast_name IS NOT NULL
         ORDER BY b.last_seen DESC
        """,
        (project_id, *devices.keys()),
    ).fetchall()
    for r in bcast_rows:
        cd = devices[r["did"]]
        if cd.broadcast_name is None:
            cd.broadcast_name = r["broadcast_name"]

    # ---- Cluster membership -----------------------------------------------
    # Loaded once and reused by both the rename-propagation pass below
    # and the cluster-collapse pass that follows. The runner persists
    # one row per (cluster, device) in ``device_cluster_members``; we
    # group by cluster_id for both passes.
    cm_rows = conn.execute(
        f"""
        SELECT cluster_id, device_id, score
          FROM device_cluster_members
         WHERE device_id IN ({placeholders})
        """,
        tuple(devices.keys()),
    ).fetchall()
    clusters: dict[int, list[tuple[int, float | None]]] = {}
    for r in cm_rows:
        clusters.setdefault(r["cluster_id"], []).append(
            (r["device_id"], r["score"]),
        )

    # ---- Rename lookup tables (NOT stale-filtered) -----------------------
    # Pull rename evidence from the WHOLE devices table, not just the
    # in-window subset, so that a fresh HA RPA arriving after the
    # renamed instance has aged out still inherits the rename. Without
    # this, "Show: 1m" caused new RPAs to display as the bare
    # ``local_name`` because the renamed device with the same
    # ``local_name`` had fallen out of the stale window and wasn't
    # available as a propagation source.
    rename_by_local_all: dict[str, tuple[float, str]] = {}
    # When two physical devices broadcast the same local_name and the
    # user has explicitly renamed them differently (e.g. left/right
    # hearing aids → "Doug HA (L)" vs "Doug HA (R)"), the most-recent
    # wins is wrong: a fresh RPA from the right HA would inherit the
    # left's name. Track distinct user_names per local_name and skip
    # propagation when ambiguous.
    distinct_names_by_local: dict[str, set[str]] = {}
    for r in conn.execute(
        "SELECT user_name, local_name, last_seen FROM devices"
        " WHERE user_name IS NOT NULL AND local_name IS NOT NULL"
    ).fetchall():
        un = r["user_name"] if not isinstance(r, tuple) else r[0]
        ln = r["local_name"] if not isinstance(r, tuple) else r[1]
        ls = r["last_seen"] if not isinstance(r, tuple) else r[2]
        existing = rename_by_local_all.get(ln)
        if existing is None or ls > existing[0]:
            rename_by_local_all[ln] = (ls, un)
        distinct_names_by_local.setdefault(ln, set()).add(un)

    # Same for cluster-based propagation: pull renamed members of any
    # cluster, even if those members aren't in the current
    # stale-filtered ``devices`` dict.
    rename_by_cluster_all: dict[int, tuple[float, str]] = {}
    distinct_names_by_cluster: dict[int, set[str]] = {}
    # Same shape for user_device_class — when the user pinned a class
    # on one cluster member, propagate to the rendered primary so the
    # override doesn't appear to flip back when the runner re-elects a
    # different primary. Multiple distinct overrides in one cluster →
    # bail (mirror the rename ambiguity rule).
    class_by_cluster_all: dict[int, tuple[float, str]] = {}
    distinct_classes_by_cluster: dict[int, set[str]] = {}
    if clusters:
        cluster_ids = list(clusters.keys())
        cph = ",".join("?" * len(cluster_ids))
        for r in conn.execute(
            f"SELECT m.cluster_id, d.user_name, d.last_seen"
            f" FROM device_cluster_members m JOIN devices d ON d.id = m.device_id"
            f" WHERE d.user_name IS NOT NULL AND m.cluster_id IN ({cph})",
            cluster_ids,
        ).fetchall():
            cid = r["cluster_id"] if not isinstance(r, tuple) else r[0]
            un = r["user_name"] if not isinstance(r, tuple) else r[1]
            ls = r["last_seen"] if not isinstance(r, tuple) else r[2]
            existing = rename_by_cluster_all.get(cid)
            if existing is None or ls > existing[0]:
                rename_by_cluster_all[cid] = (ls, un)
            distinct_names_by_cluster.setdefault(cid, set()).add(un)
        for r in conn.execute(
            f"SELECT m.cluster_id, d.user_device_class, d.last_seen"
            f" FROM device_cluster_members m JOIN devices d ON d.id = m.device_id"
            f" WHERE d.user_device_class IS NOT NULL AND m.cluster_id IN ({cph})",
            cluster_ids,
        ).fetchall():
            cid = r["cluster_id"] if not isinstance(r, tuple) else r[0]
            cls = r["user_device_class"] if not isinstance(r, tuple) else r[1]
            ls = r["last_seen"] if not isinstance(r, tuple) else r[2]
            existing = class_by_cluster_all.get(cid)
            if existing is None or ls > existing[0]:
                class_by_cluster_all[cid] = (ls, cls)
            distinct_classes_by_cluster.setdefault(cid, set()).add(cls)

    # ---- Rename propagation (cluster-based, most accurate) ---------------
    # For each cluster represented in this canvas reload, propagate the
    # ``user_name`` of the cluster's most-recently-renamed member to
    # any unnamed member also visible in this reload. Source draws on
    # ALL clustered devices (via ``rename_by_cluster_all``) so the
    # rename survives even when the originally-renamed RPA has fallen
    # out of the stale window.
    for cluster_id, mems in clusters.items():
        live_devs = [devices[did] for did, _ in mems if did in devices]
        if not live_devs:
            continue

        # User-name propagation. Multiple distinct user_names within
        # one cluster means the user has labelled members differently
        # (e.g. a stereo pair incorrectly merged into one cluster,
        # then split apart by rename). Don't guess — leave each
        # member's stored user_name as-is.
        if len(distinct_names_by_cluster.get(cluster_id, ())) <= 1:
            picked = rename_by_cluster_all.get(cluster_id)
            if picked is not None:
                chosen = picked[1]
                for d in live_devs:
                    if not d.user_name:
                        d.user_name = chosen

        # User-device-class propagation. Same ambiguity rule. The
        # cluster runner can re-elect a different primary across
        # reloads; without this, an override set on member X is
        # invisible once the primary becomes member Y. Updates the
        # effective ``device_class`` too because the Python-side
        # COALESCE downstream uses the per-row value loaded from SQL.
        if len(distinct_classes_by_cluster.get(cluster_id, ())) <= 1:
            picked_cls = class_by_cluster_all.get(cluster_id)
            if picked_cls is not None:
                chosen_cls = picked_cls[1]
                for d in live_devs:
                    if not d.user_device_class:
                        d.user_device_class = chosen_cls
                        d.device_class = chosen_cls

    # ---- Rename propagation (local_name fallback) ------------------------
    # For devices NOT in a cluster, fall back to matching the
    # broadcast ``local_name``. Most-recently-renamed device per
    # local_name wins. Source is the WHOLE devices table so a fresh
    # RPA arriving after the renamed instance has aged out still
    # inherits the rename.
    #
    # Display-only — never writes back to the DB, so a wrong match
    # never poisons the underlying ``devices.user_name`` column.
    if rename_by_local_all:
        for cd in devices.values():
            if cd.user_name:
                continue
            ln = cd.local_name or ""
            # Two physical devices that share a local_name and have
            # been renamed to different things (left/right hearing
            # aids, stereo earbuds, etc.) are ambiguous from local_name
            # alone — bail rather than guess.
            if len(distinct_names_by_local.get(ln, ())) > 1:
                continue
            picked = rename_by_local_all.get(ln)
            if picked is not None:
                cd.user_name = picked[1]

    # ---- Cluster collapse -------------------------------------------------
    # Multi-member clusters whose weakest score clears the threshold
    # collapse into one primary box that aggregates the absorbed
    # siblings; weaker clusters keep all members visible (still
    # tagged with cluster_id so the UI can surface "this is part of
    # cluster N — verify it").
    for cluster_id, mems in clusters.items():
        # Filter to members that survived the stale-window cut.
        live_mems = [(did, s) for did, s in mems if did in devices]
        if not live_mems:
            continue
        scores = [s for _, s in live_mems if s is not None]
        min_score = min(scores) if scores else None
        # Read threshold per call so a live preference change takes
        # effect on the next reload without restart.
        from ..preferences import get_prefs
        collapse_threshold = float(
            get_prefs().get("cluster.collapse_threshold")
        )
        if (
            len(live_mems) < 2
            or min_score is None
            or min_score < collapse_threshold
        ):
            # Tag membership but don't collapse — the badge can still
            # appear on individual boxes ("part of cluster N") but
            # they keep their own canvas positions.
            for did, _ in live_mems:
                devices[did].cluster_id = cluster_id
                devices[did].cluster_min_score = min_score
            continue
        # Collapse: pick primary, absorb others, drop them.
        member_devs = [devices[did] for did, _ in live_mems]
        primary = _pick_cluster_primary(member_devs)
        primary.cluster_id = cluster_id
        primary.cluster_min_score = min_score
        for member in member_devs:
            if member.device_id == primary.device_id:
                continue
            _absorb_cluster_member(primary, member)
            del devices[member.device_id]

    # Saved layout per project. Run AFTER cluster collapse so we don't
    # waste a layout lookup on absorbed device_ids that won't render.
    surviving_ids = list(devices.keys())
    surviving_placeholders = ",".join("?" * len(surviving_ids))
    layout_rows = conn.execute(
        f"SELECT * FROM device_layouts WHERE project_id = ? AND device_id IN ({surviving_placeholders})",
        (project_id, *surviving_ids),
    ).fetchall()
    for r in layout_rows:
        cd = devices[r["device_id"]]
        cd.pos_x = r["pos_x"]
        cd.pos_y = r["pos_y"]
        cd.collapsed = bool(r["collapsed"])
        cd.hidden = bool(r["hidden"])

    return list(devices.values())


def _row_best_label(r: Any) -> str:
    """Compute a device best-label from a sqlite Row (avoids constructing a
    full Device dataclass just for the string).

    Note: broadcast_name isn't on the devices row so it can't influence
    this label directly. ``DeviceItem`` adjusts its display to prefer
    broadcast_name when this device is a broadcaster (see
    ``_pick_display_label``).
    """
    if r["user_name"]:
        return r["user_name"]
    if r["gatt_device_name"]:
        return r["gatt_device_name"]
    if r["local_name"]:
        return r["local_name"]
    vendor = r["vendor"] or r["oui_vendor"]
    if vendor and r["model"]:
        return f"{vendor} {r['model']}"
    if vendor and r["device_class"]:
        return f"{vendor} {r['device_class']}"
    if vendor:
        return vendor
    sk = r["stable_key"]
    for p in ("pub:", "rs:", "rpa:", "nrpa:", "irk:", "anon:"):
        if sk.startswith(p):
            return sk[len(p):]
    return sk


# Well-known 16-bit BLE service UUIDs the canvas might surface.
# Curated set — annotates the tooltip's UUID list with friendly names
# for the services this app's user community commonly sees (LE Audio,
# Apple/Google identity beacons, common GATT profiles). Anything not in
# this dict displays as the bare hex value.
_KNOWN_UUID16: dict[int, str] = {
    # Generic GATT profiles
    0x1800: "GAP",
    0x1801: "GATT",
    0x180A: "Device Information",
    0x180D: "Heart Rate",
    0x180F: "Battery",
    0x1812: "HID",
    # LE Audio (Hearing Access + Audio Stream stack)
    0x1843: "AICS (Audio Input Control)",
    0x1844: "VCS (Volume Control)",
    0x1845: "VOCS (Volume Offset Control)",
    0x1846: "AICS",
    0x1850: "PACS (Published Audio Capabilities)",
    0x1851: "BAS (Basic Audio)",
    0x1852: "BASS (Broadcast Audio Scan)",
    0x1853: "CAS (Common Audio)",
    0x1854: "HAS (Hearing Access)",
    0x184E: "ASCS (Audio Stream Control)",
    0x184F: "BASS (Broadcast Audio Scan)",
    # Vendor / ecosystem service UUIDs
    0xFD3D: "Apple Watch (LE Audio)",
    0xFD43: "Meta",
    0xFD6F: "Apple Continuity / Exposure Notifications",
    0xFDF0: "Google Nearby",
    0xFE2C: "Google Fast Pair",
    0xFE9A: "Estimote",
    0xFEAA: "Eddystone",
    0xFEE7: "Tencent",
}


# Truncate the tooltip's address list to keep it screen-sized for
# heavy-merged clusters. Big clusters (200+ RPAs absorbed into one
# apple_device primary) used to render 200+ address lines and overflow
# Qt's tooltip viewport.
_TOOLTIP_ADDR_MAX = 12


# Sentinel string used by ``DeviceClassDialog`` to signal "clear the
# user override and fall back to auto-detection". Picked to be
# obviously not a real class string.
_CLASS_RESET_SENTINEL = "__reset_to_auto__"


class DeviceClassDialog(QDialog):
    """Searchable picker over the canonical device-class list.

    The list is fixed (driven by ``DEVICE_CLASSES``) — the user can
    only choose values btviz knows about. Type into the search box
    to filter; double-click an item or hit Enter to accept; Escape
    cancels. A "Reset to auto-detected" entry sits at the top so the
    user can clear an override without leaving the dialog.

    Display labels run through ``device_class_label`` so underscored
    machine values present as readable phrases ("auracast source"
    instead of "auracast_source"). The underlying value the dialog
    returns is always the underscored form.
    """

    _RESET_LABEL = "(reset to auto-detected)"

    def __init__(
        self,
        parent: QWidget | None,
        *,
        current: str | None,
        auto: str | None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Set device class")
        self.setModal(True)
        self.resize(360, 480)

        layout = QVBoxLayout(self)

        header = QLabel(self)
        if auto:
            header.setText(f"Auto-detected: <b>{auto}</b>")
        else:
            header.setText("Auto-detection has no class for this device.")
        layout.addWidget(header)

        self._search = QLineEdit(self)
        self._search.setPlaceholderText("Type to filter…")
        self._search.textChanged.connect(self._refilter)
        layout.addWidget(self._search)

        self._list = QListWidget(self)
        self._list.itemActivated.connect(self._on_activated)
        self._list.itemDoubleClicked.connect(self._on_activated)
        layout.addWidget(self._list, 1)

        # Build entries: reset row first, then sorted classes.
        reset_item = QListWidgetItem(self._RESET_LABEL)
        reset_item.setData(0x100, _CLASS_RESET_SENTINEL)  # Qt.UserRole
        self._list.addItem(reset_item)
        for klass in sorted(DEVICE_CLASSES):
            item = QListWidgetItem(device_class_label(klass))
            item.setData(0x100, klass)
            self._list.addItem(item)

        # Pre-select the current override (or the auto value as a hint
        # of "what's already in effect"). Selecting the row scrolls it
        # into view so the user sees their starting point.
        prefer = current
        if prefer:
            for i in range(self._list.count()):
                if self._list.item(i).data(0x100) == prefer:
                    self._list.setCurrentRow(i)
                    self._list.scrollToItem(self._list.item(i))
                    break

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel, parent=self,
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _refilter(self, text: str) -> None:
        needle = text.strip().lower()
        for i in range(self._list.count()):
            item = self._list.item(i)
            # The reset row is always visible — clearing the override
            # shouldn't require typing the right magic string.
            if item.data(0x100) == _CLASS_RESET_SENTINEL:
                item.setHidden(False)
                continue
            label = item.text().lower()
            value = (item.data(0x100) or "").lower()
            item.setHidden(needle not in label and needle not in value)

    def _on_activated(self, _item: QListWidgetItem) -> None:
        self.accept()

    def chosen_value(self) -> str | None:
        """Return the picked class string, the reset sentinel, or
        None when the user cancelled / nothing is selected."""
        item = self._list.currentItem()
        if item is None or item.isHidden():
            return None
        return item.data(0x100)


def _pick_display_label(d: CanvasDevice) -> str:
    """Choose the strongest identity string for the box header.

    Same precedence as ``_row_best_label`` but includes broadcast_name
    near the top — for a device whose primary identity in this project
    is being an Auracast broadcaster, the broadcast name (e.g. "Avantree
    Oasis Aura_65ac") is the most informative thing we can show.
    """
    if d.user_name:
        return d.user_name
    if d.gatt_device_name:
        return d.gatt_device_name
    if d.local_name:
        return d.local_name
    if d.broadcast_name:
        return d.broadcast_name
    return d.label  # already-computed fallback (vendor + model / class / key)


def _build_tooltip(d: CanvasDevice) -> str:
    """Comprehensive plain-text tooltip for the device box body.

    Skipped when the device is a cluster primary (member_count > 1):
    cluster info + the merged address list move to a separate
    badge-specific tooltip via ``_build_cluster_tooltip`` so the main
    tooltip stays readable on heavy merges. Plain text only (Qt's
    tooltip handles ``\\n``); avoids HTML for cross-platform render
    consistency.
    """
    is_cluster = d.cluster_member_count > 1
    lines: list[str] = []
    lines.append(_pick_display_label(d))
    lines.append("─" * 36)
    lines.append(f"Device ID:     {d.device_id}")
    lines.append(f"Stable key:    {d.stable_key}")
    lines.append(f"Kind:          {d.kind}")
    # Cluster identity moves to the badge tooltip when this device is
    # a primary that absorbed siblings — the badge itself signals
    # "this is a merge" so its tooltip is the natural home for the
    # cluster details and the union of addresses.
    if d.cluster_id is not None and not is_cluster:
        score = (
            f" (min score {d.cluster_min_score:.2f})"
            if d.cluster_min_score is not None else ""
        )
        lines.append(
            f"Cluster:       {d.cluster_id} · "
            f"{d.cluster_member_count} member"
            f"{'s' if d.cluster_member_count != 1 else ''}{score}"
        )
        if d.cluster_member_ids:
            ids = ", ".join(str(i) for i in d.cluster_member_ids)
            lines.append(f"  absorbed:    {ids}")
    if d.device_class:
        # Show "(auto: X)" suffix when the user has pinned an override
        # that disagrees with what auto-detection inferred — so the
        # user can tell at a glance that the class they see is their
        # own choice and what btviz would otherwise have shown.
        if (
            d.user_device_class
            and d.auto_device_class
            and d.user_device_class != d.auto_device_class
        ):
            lines.append(
                f"Class:         {d.device_class}  "
                f"(auto: {d.auto_device_class})"
            )
        else:
            lines.append(f"Class:         {d.device_class}")
    if d.user_name:
        lines.append(f"User name:     {d.user_name}")
    if d.gatt_device_name:
        lines.append(f"GATT name:     {d.gatt_device_name}")
    if d.local_name:
        lines.append(f"Local name:    {d.local_name}")
    if d.broadcast_name:
        lines.append(f"Broadcast:     {d.broadcast_name}")
    if d.model:
        lines.append(f"Model:         {d.model}")
    vendor_full = d.vendor or "(none)"
    lines.append(f"Vendor:        {vendor_full}")
    if d.vendor_id is not None:
        lines.append(f"Vendor ID:     0x{d.vendor_id:04X}")
    if d.oui_vendor:
        lines.append(f"OUI vendor:    {d.oui_vendor}")
    if d.appearance is not None:
        lines.append(f"Appearance:    0x{d.appearance:04X}")
    lines.append("")
    lines.append(
        f"Packets:       {d.packet_count:,} "
        f"(adv {d.adv_count:,}, data {d.data_count:,})"
    )
    if d.rssi_avg is not None:
        lines.append(
            f"RSSI:          avg {d.rssi_avg:.0f} dBm "
            f"(min {d.rssi_min}, max {d.rssi_max})"
        )
    if d.pdu_types:
        lines.append("")
        lines.append("PDU types:")
        for pdu, n in sorted(d.pdu_types.items(), key=lambda kv: -kv[1]):
            lines.append(f"  {pdu}: {n}")
    if d.channels:
        lines.append("")
        lines.append("Channels:")
        for ch, n in sorted(d.channels.items()):
            lines.append(f"  {ch}: {n}")
    if d.service_uuids:
        lines.append("")
        lines.append(f"Service UUIDs ({len(d.service_uuids)}):")
        for u in sorted(d.service_uuids):
            label = _KNOWN_UUID16.get(u)
            if label:
                lines.append(f"  0x{u:04X}  {label}")
            else:
                lines.append(f"  0x{u:04X}")
    # Addresses move to the cluster-badge tooltip for cluster
    # primaries (where the list is the union over absorbed members
    # and would otherwise overflow the main tooltip). Single-device
    # devices still see their address list here — there's no badge
    # tooltip to host it on.
    if d.addresses and not is_cluster:
        lines.append("")
        lines.append(f"Addresses ({len(d.addresses)}):")
        for addr, atype in d.addresses[:_TOOLTIP_ADDR_MAX]:
            lines.append(f"  {addr}  ({atype})")
        if len(d.addresses) > _TOOLTIP_ADDR_MAX:
            lines.append(
                f"  +{len(d.addresses) - _TOOLTIP_ADDR_MAX} more"
            )
    return "\n".join(lines)


def _build_cluster_tooltip(d: CanvasDevice) -> str:
    """Tooltip shown when the cursor hovers the cluster badge.

    Carries the cluster identity (id, member count, min score, the
    list of absorbed device_ids) and the union of addresses across
    the cluster. This is the home for "what got merged into this
    box" so the main tooltip can stay focused on the primary's own
    identity and stats.

    Returns an empty string when the device isn't a cluster primary
    — the badge isn't drawn in that case so the tooltip is unused.
    """
    if d.cluster_member_count <= 1:
        return ""
    lines: list[str] = []
    lines.append(_pick_display_label(d))
    lines.append("─" * 36)
    score = (
        f" (min score {d.cluster_min_score:.2f})"
        if d.cluster_min_score is not None else ""
    )
    lines.append(
        f"Cluster:       {d.cluster_id} · "
        f"{d.cluster_member_count} member"
        f"{'s' if d.cluster_member_count != 1 else ''}{score}"
    )
    if d.cluster_member_ids:
        ids = ", ".join(str(i) for i in d.cluster_member_ids)
        lines.append(f"Absorbed IDs:  {ids}")
    if d.addresses:
        lines.append("")
        lines.append(f"Addresses ({len(d.addresses)}):")
        for addr, atype in d.addresses[:_TOOLTIP_ADDR_MAX]:
            lines.append(f"  {addr}  ({atype})")
        if len(d.addresses) > _TOOLTIP_ADDR_MAX:
            lines.append(
                f"  +{len(d.addresses) - _TOOLTIP_ADDR_MAX} more"
            )
    return "\n".join(lines)


# Sort keys for the canvas toolbar's two-level sort dropdowns. Each value
# is a function that maps a CanvasDevice to a sort-comparable key. RSSI
# and Packets are negated so the natural ascending sort surfaces the
# loudest / most-active device first; missing values get a sentinel that
# pushes them to the end. Same dict is the source of truth for both the
# primary and secondary dropdowns; "_SORT_KEY_LABELS" preserves a stable
# UI order distinct from the dict's iteration order.
_SORT_KEY_LABELS: tuple[str, ...] = (
    "Type",
    "Name",
    "Address kind",
    "RSSI (avg)",
    "Vendor",
    "Packets",
    "Last seen",
)
_SORT_KEYS: dict[str, "Callable[[CanvasDevice], Any]"] = {
    "Type":         lambda d: (d.device_class or "~"),
    "Name":         lambda d: _pick_display_label(d).lower(),
    "Address kind": lambda d: (d.kind or "~"),
    "RSSI (avg)":   lambda d: -(d.rssi_avg if d.rssi_avg is not None else -200),
    "Vendor":       lambda d: ((d.vendor or d.oui_vendor) or "~").lower(),
    "Packets":      lambda d: -d.packet_count,
    # Most-recent first. Devices with no observation timestamp get
    # pushed to the end via a sentinel (negated max float).
    "Last seen":    lambda d: -(d.last_seen or 0.0),
}

# Cluster auto-run cadences. The integer is the number of _live_tick
# callbacks (each = 250 ms) between automatic cluster runs. ``off``
# disables the auto-run entirely so the toolbar's "Run cluster" button
# is the only trigger — useful when iterating on signal logic and you
# don't want the runner to fire mid-edit.
_CLUSTER_PERIOD_LABELS: tuple[str, ...] = (
    "off", "5s", "15s", "30s", "1m", "5m",
)
_CLUSTER_PERIOD_TICKS: dict[str, int] = {
    "off": 0,
    "5s":  20,
    "15s": 60,
    "30s": 120,
    "1m":  240,
    "5m":  1200,
}

# Stale-device window. Devices whose latest observation in the project
# is older than this cutoff are hidden from the canvas AND excluded
# from the cluster runner's hydrator. ``all`` disables the filter so
# every device the project has ever seen stays visible. Default 30m
# is a compromise between "live session feel" and "let me see what
# was here a few minutes ago".
_STALE_WINDOW_LABELS: tuple[str, ...] = (
    "5s", "15s", "30s", "1m", "5m",
    "10m", "15m", "30m", "60m", "90m", "24hr", "all",
)
_STALE_WINDOW_SECONDS: dict[str, float | None] = {
    "5s":   5.0,
    "15s":  15.0,
    "30s":  30.0,
    "1m":   60.0,
    "5m":   300.0,
    "10m":  600.0,
    "15m":  900.0,
    "30m":  1800.0,
    "60m":  3600.0,
    "90m":  5400.0,
    "24hr": 86400.0,
    "all":  None,
}


# ──────────────────────────────────────────────────────────────────────────
# Recency → box opacity
# ──────────────────────────────────────────────────────────────────────────

# Time thresholds for the dormancy fade. Devices observed in the last
# minute paint at full opacity; anything older than 24 hours bottoms out
# at the floor. Between, opacity decays linearly in *log* time so a
# 5-minute-old device looks distinctly fresher than a 1-hour-old one
# (linear-in-seconds would have the difference be invisible).
_RECENCY_FRESH_S = 60.0           # < 1 min ago → fully opaque
_RECENCY_DORMANT_S = 86400.0      # > 24 hr ago → at floor
_RECENCY_MIN_OPACITY = 0.10       # never disappear entirely


def opacity_for_recency(
    last_seen_ts: float,
    now_ts: float | None = None,
    *,
    dormant_s: float | None = None,
) -> float:
    """Linear-in-log-time opacity for a CanvasDevice based on its
    last_seen wall-clock timestamp.

    100% for fresh devices, decaying to 10% at the dormant horizon and
    flooring there. Both ends capped. Devices that never produced an
    observation (last_seen=0) return the floor opacity — they're
    listed because of identity info but produced no traffic in this
    project.

    ``dormant_s`` overrides the default 24 h horizon so the canvas can
    tie the fade to the toolbar's ``show:`` cutoff: with ``show: 5m``
    the dormant point is 300 s — a device just shy of falling off the
    canvas paints near the floor, telegraphing it's about to vanish.

    For very short dormant horizons we shrink the "fresh" window to
    ``dormant_s * 0.2`` so the fade is visible across the user's
    chosen range; otherwise a 60-s fresh threshold against a 30-s
    horizon would never enter the log-decay branch.
    """
    if last_seen_ts is None or last_seen_ts <= 0:
        return _RECENCY_MIN_OPACITY
    if now_ts is None:
        now_ts = time.time()
    age = max(0.0, now_ts - last_seen_ts)

    horizon = dormant_s if dormant_s is not None else _RECENCY_DORMANT_S
    fresh = min(_RECENCY_FRESH_S, horizon * 0.2)
    # Guard against a degenerate horizon (caller passed 0 or negative).
    if horizon <= fresh:
        return 1.0 if age < horizon else _RECENCY_MIN_OPACITY

    if age < fresh:
        return 1.0
    if age >= horizon:
        return _RECENCY_MIN_OPACITY
    import math
    log_age = math.log10(age)
    log_min = math.log10(fresh)
    log_max = math.log10(horizon)
    frac = (log_age - log_min) / (log_max - log_min)
    return 1.0 - (1.0 - _RECENCY_MIN_OPACITY) * frac


def _shorten_source_label(src: str) -> str:
    """Compact a sniffer ``short_id`` for status-bar display.

    Strips the trailing ``-None`` suffix that macOS appends to
    serial-less device nodes (Nordic dongles without a USB iSerial)
    and truncates very long iSerial-based ids — keeping the head
    digits intact. Used only for human-readable diagnostics; the
    full short_id is the ``_source_to_serial`` key elsewhere.
    """
    s = src or "?"
    if s.endswith("-None"):
        s = s[: -len("-None")]
    if len(s) > 8:
        s = s[:8]
    return s


def cols_for_viewport(viewport_width: int | None) -> int:
    """Pick a column count that fits the current viewport.

    Returns ``_GRID_COLS_DEFAULT`` when ``viewport_width`` is None
    (headless / pre-show). Otherwise divides the available width by the
    column pitch (box + gutter) and clamps to ``[_GRID_COLS_MIN,
    _GRID_COLS_MAX]`` so a pathologically narrow window still gives 1
    column per row, and a very wide one doesn't spread boxes so far
    apart that they're tedious to scan.
    """
    if viewport_width is None or viewport_width <= 0:
        return _GRID_COLS_DEFAULT
    usable = max(0, viewport_width - 2 * _GRID_MARGIN_X)
    cols = max(1, usable // _GRID_DX)
    return max(_GRID_COLS_MIN, min(_GRID_COLS_MAX, int(cols)))


def apply_grid_layout(
    devices: list[CanvasDevice],
    *,
    cols: int = _GRID_COLS_DEFAULT,
) -> None:
    """Assign default grid positions to any device missing a layout (both
    pos_x and pos_y equal to 0 and no layout row in the DB). Keeps existing
    positions intact.

    ``cols`` lets the caller respect the current viewport width so newly-
    placed devices flow into the available area instead of being stuck
    at the historical 6-column grid.
    """
    unplaced = [d for d in devices if d.pos_x == 0.0 and d.pos_y == 0.0]
    for i, d in enumerate(unplaced):
        col = i % cols
        row = i // cols
        d.pos_x = _GRID_MARGIN_X + col * _GRID_DX
        d.pos_y = _GRID_MARGIN_X + row * _GRID_DY


_STABLE_KINDS = frozenset({
    "public_mac", "random_static_mac", "irk_identity",
})

# Device classes specific enough that classification + meaningful
# observation history is itself an identification. Excludes the
# generic catch-alls ``apple_device`` and ``unknown`` which fire on
# any Apple-vendor mfg_data or anything we couldn't classify
# specifically — those would pull churn into the top section.
_SPECIFIC_DEVICE_CLASSES = frozenset({
    "airtag", "airpods", "apple_watch", "hearing_aid",
    "auracast_source", "phone", "mac",
})

# Minimum packets needed before a classified-but-RPA device earns a
# slot in the top section. Filters out one-off appearances; an AirTag
# we've heard from for a few minutes (~thousands of packets) clears
# this; a brief blip during a rotation does not.
_STABLE_CLASS_MIN_PACKETS = 500


def is_stable_device(d: CanvasDevice) -> bool:
    """True if this device belongs in the top "Devices" section.

    A device is "stable" — and gets its own permanent box in the top
    section — when any of these holds:

      * ``kind`` is a stable address kind (public_mac, random_static_mac,
        or irk_identity). The address itself is the device's identity;
        no rotation or guesswork required.
      * ``cluster_member_count > 1``. The cluster runner has merged at
        least one RPA into this primary, so we trust the merge enough
        to call it one device.
      * ``user_name`` is set. The user has manually identified it.
      * ``local_name`` is set. The device chooses to broadcast a
        friendly name (e.g. "Douglas Hearing Aids") — that's strong
        self-identification regardless of address kind.
      * ``device_class`` is in the specific-class list AND the device
        has been seen for at least ``_STABLE_CLASS_MIN_PACKETS``
        packets. Catches AirTags / Apple Watches / hearing aids that
        the cluster runner hasn't yet had time to merge across
        rotations.

    Everything else — unresolved RPAs not yet merged, nrpas, anons,
    unknowns, and brief classified blips — falls into the bottom
    "Unidentified Advertisements" section.
    """
    if d.kind in _STABLE_KINDS:
        return True
    if d.cluster_member_count > 1:
        return True
    if d.user_name:
        return True
    if d.local_name:
        return True
    if (
        d.device_class in _SPECIFIC_DEVICE_CLASSES
        and d.packet_count >= _STABLE_CLASS_MIN_PACKETS
    ):
        return True
    return False


def section_grid_layout(
    devices: list[CanvasDevice],
    *,
    cols: int,
    top_y: float,
) -> float:
    """Place ``devices`` in a grid starting at ``top_y``; return next y.

    Always overrides ``pos_x`` / ``pos_y`` (no saved-position respect)
    because the section assignment is structural — when a device
    migrates between top and bottom (e.g. cluster runner promotes an
    RPA into a multi-member cluster) it should appear in the right
    section regardless of any saved coordinates from before.

    Returns the y-coordinate just past the last row (top_y when empty
    so the caller can still reserve placeholder space).
    """
    if not devices:
        return top_y
    for i, d in enumerate(devices):
        col = i % cols
        row = i // cols
        d.pos_x = _GRID_MARGIN_X + col * _GRID_DX
        d.pos_y = top_y + row * _GRID_DY
    return max(d.pos_y for d in devices) + _BOX_H_COLLAPSED


# ──────────────────────────────────────────────────────────────────────────
# Device box (QGraphicsItem)
# ──────────────────────────────────────────────────────────────────────────


@dataclass
class _DeviceLiveState:
    """Per-device live state that survives canvas reload.

    The canvas rebuilds DeviceItems every ~2 s during live capture
    (``scene.clear()`` inside ``reload()``). Anything stored on the
    DeviceItem itself would reset every reload — flashing the
    Signal/Quality bars to neutral, breaking channel-flash continuity,
    and freezing the packet counter for 2 s at a time. Hoisting these
    pieces here gives them a stable home keyed by ``device_id`` on the
    canvas; each new DeviceItem receives its slice by reference.
    """
    # Rolling-window samples driving the Signal/Quality meters.
    # Pruned on read in ``DeviceItem._recent_stats``.
    recent: deque = field(default_factory=deque)
    # Channel-flash tail for data channels (0-36). Pruned in place
    # in ``DeviceItem.notify_channel_hit``.
    data_flash_recent: list = field(default_factory=list)
    # Latest flash per advertising channel (37/38/39).
    adv_flash: dict = field(default_factory=dict)
    # Live packet count delta on top of ``CanvasDevice.packet_count``.
    # Incremented per attribution between reloads; zeroed at reload
    # because the fresh DB count already includes those packets.
    live_packet_delta: int = 0


class DeviceItem(QGraphicsItem):
    """Draggable, collapsible box representing one device.

    Double-click toggles expanded/collapsed. Drag moves and, on release,
    persists position (and collapsed state) via the owning scene's callback.
    """

    def __init__(
        self,
        device: CanvasDevice,
        persist_cb,
        context_cb=None,
        live_state: "_DeviceLiveState | None" = None,
    ) -> None:
        super().__init__()
        self.device = device
        self._persist = persist_cb
        # Optional callback the scene installs to populate the
        # right-click menu. Signature: (device) -> list[QAction]. When
        # None, no context menu is shown.
        self._context_cb = context_cb
        # All per-device live state lives on the canvas-owned
        # ``_DeviceLiveState`` so it survives ``scene.clear()`` in
        # reload(). Falls back to a private instance when no live
        # state is provided (tests / standalone construction).
        # ``_recent``, ``_data_flash_recent``, ``_adv_flash`` are
        # aliases pointing at the dataclass fields — mutate in place
        # (append, popleft, slice-assign), never reassign, or the
        # alias drifts away from the canvas-owned state.
        self._live: _DeviceLiveState = (
            live_state if live_state is not None else _DeviceLiveState()
        )
        self._data_flash_recent = self._live.data_flash_recent
        self._adv_flash = self._live.adv_flash
        self._recent = self._live.recent
        self.setFlag(QGraphicsItem.ItemIsMovable, True)
        self.setFlag(QGraphicsItem.ItemIsSelectable, True)
        self.setFlag(QGraphicsItem.ItemSendsGeometryChanges, True)
        self.setPos(device.pos_x, device.pos_y)
        self.setZValue(_Z_EXPANDED if not device.collapsed else _Z_NORMAL)
        # Pre-built tooltip strings, swapped on hover by region:
        # over the cluster badge → cluster identity + merged-address
        # list; anywhere else → the main tooltip (everything except
        # the badge-owned content). Cached so we're not rebuilding
        # the strings on every mouse-move event.
        self._main_tooltip = _build_tooltip(device)
        self._cluster_tooltip = _build_cluster_tooltip(device)
        self.setToolTip(self._main_tooltip)
        # Hover events drive the region-specific tooltip swap.
        self.setAcceptHoverEvents(True)

    def notify_channel_hit(
        self,
        channel: int | None,
        crc_ok: bool = True,
        rssi: int | None = None,
    ) -> None:
        """Record a packet hit on this device on the given channel.

        Called from the canvas's per-device LiveIngest callback.
        Channels 0-36 (data) flash the header badge; 37/38/39 (adv)
        flash one of the three squares in the adv strip below it.
        Each indicator is independently faded over
        ``_CHANNEL_FLASH_DURATION_S``.

        ``crc_ok=False`` flags a dropout — the indicator paints black
        with a red glyph instead of the channel-color.

        ``rssi`` (dBm, negative) feeds the rolling-window Signal meter.
        It can be None for channels we couldn't pin down.

        Hits with ``channel=None`` are still counted in the rolling
        window so Signal/Quality stay correct, but no flash badge is
        drawn (no channel to point to).
        """
        now = time.time()
        self._recent.append((now, rssi, crc_ok))
        self._live.live_packet_delta += 1
        if channel is None:
            self.update()
            return
        if channel >= 37:
            self._adv_flash[channel] = (now, not crc_ok)
        else:
            self._data_flash_recent.append((now, channel, not crc_ok))
            cutoff = now - _CHANNEL_FLASH_DURATION_S
            # Slice-assign so the canvas-owned list keeps the same
            # identity — reassigning to a fresh list would break the
            # alias and our updates would land on a list nobody else
            # references.
            self._data_flash_recent[:] = [
                e for e in self._data_flash_recent if e[0] >= cutoff
            ][-6:]
        self.update()

    def _recent_stats(
        self, now: float | None = None,
    ) -> tuple[float | None, int, int]:
        """Return ``(rssi_avg, good_count, bad_count)`` over the live
        rolling window, pruning samples older than ``_RECENT_WINDOW_S``.

        ``rssi_avg`` is None when no in-window samples carried an RSSI
        (or no samples at all). Callers paint a neutral bar in that
        case. Pruning happens here on read so the deque doesn't grow
        unboundedly between paints when no fresh packets arrive.
        """
        if now is None:
            now = time.time()
        cutoff = now - _RECENT_WINDOW_S
        while self._recent and self._recent[0][0] < cutoff:
            self._recent.popleft()
        if not self._recent:
            return (None, 0, 0)
        rssi_total = 0
        rssi_n = 0
        good = 0
        bad = 0
        for _ts, rssi, crc_ok in self._recent:
            if crc_ok:
                good += 1
            else:
                bad += 1
            if rssi is not None:
                rssi_total += rssi
                rssi_n += 1
        rssi_avg = rssi_total / rssi_n if rssi_n else None
        return (rssi_avg, good, bad)

    # --- geometry -----------------------------------------------------

    def boundingRect(self) -> QRectF:
        h = _BOX_H_EXPANDED if not self.device.collapsed else _BOX_H_COLLAPSED
        return QRectF(0, 0, _BOX_W, h)

    def paint(self, painter: QPainter, _option, _widget=None) -> None:
        r = self.boundingRect()
        fill = _KIND_FILL.get(self.device.kind, _KIND_FILL["unknown"])
        pen = QPen(QColor(90, 90, 90), 1)
        if self.isSelected():
            pen = QPen(QColor(20, 90, 200), 2)
        painter.setPen(pen)
        painter.setBrush(QBrush(fill))
        painter.drawRoundedRect(r, _BOX_RADIUS, _BOX_RADIUS)

        # Header band — taller, with an icon and a two-line text region.
        header_rect = QRectF(0, 0, _BOX_W, _HEADER_H)
        painter.setBrush(QBrush(fill.darker(108)))
        painter.setPen(Qt.NoPen)
        painter.drawRoundedRect(header_rect, _BOX_RADIUS, _BOX_RADIUS)
        # Square off the bottom of the header so it joins the body cleanly.
        painter.drawRect(QRectF(0, _BOX_RADIUS, _BOX_W, _HEADER_H - _BOX_RADIUS))

        # Reserve room on the right for the data-flash badge + adv
        # strip column so a long wrapped title doesn't bleed under
        # the 37/38/39 indicators. The cluster badge no longer steals
        # header space — it lives in the lower-left corner of the body.
        adv_reserve = (
            _CH_FLASH_BADGE_MARGIN
            + max(_CH_FLASH_BADGE_W, 3 * _ADV_CH_BOX_W + 2 * _ADV_CH_BOX_GAP)
            + 4
        )

        # Icon (left side): device_class SVG, falling back to fallback_icon.svg.
        icon_rect = QRectF(6, 0, 44, _HEADER_H)
        renderer = (
            _icon_renderer(self.device.device_class)
            or _icon_renderer(_FALLBACK_SVG_NAME)
        )
        if renderer is not None:
            svg_size = 36
            cx = icon_rect.x() + icon_rect.width() / 2
            cy = icon_rect.y() + icon_rect.height() / 2
            renderer.render(
                painter,
                QRectF(cx - svg_size / 2, cy - svg_size / 2, svg_size, svg_size),
            )

        # Title text (right of icon, two-line region with word wrap).
        # Prefer broadcast_name / GATT name / local name over the
        # vendor-derived fallback when one of those is set.
        label_font = QFont()
        label_font.setBold(True)
        label_font.setPointSize(11)
        painter.setFont(label_font)
        painter.setPen(QColor(30, 30, 30))
        text_left = icon_rect.right() + 2
        text_rect = QRectF(
            text_left, 4,
            _BOX_W - text_left - adv_reserve,
            _HEADER_H - 8,
        )
        painter.drawText(
            text_rect,
            Qt.AlignVCenter | Qt.AlignLeft | Qt.TextWordWrap,
            self._truncate(_pick_display_label(self.device), 56),
        )

        # Body
        body_font = QFont()
        body_font.setPointSize(8)
        painter.setFont(body_font)
        painter.setPen(QColor(50, 50, 50))

        if self.device.collapsed:
            self._paint_collapsed_body(painter)
        else:
            self._paint_expanded_body(painter)

        self._paint_channel_flash(painter)
        if self.device.cluster_member_count > 1:
            self._paint_cluster_badge(painter)

    def _paint_channel_flash(self, painter: QPainter) -> None:
        """Render the per-device channel-activity indicators.

        Two stacked elements in the top-right of the header band:

        * The data-channel badge — most-recently-active channel in
          0-36 with its canonical channel-color, plus a comet-tail of
          prior data hits for hopping streams (e.g. Auracast).
        * The advertising strip — three small squares (37/38/39),
          each independently lit when that primary channel sees a hit.

        Both fade over ``_CHANNEL_FLASH_DURATION_S``. CRC-failed hits
        paint black with a red glyph (matching the sniffer panel's
        dropout treatment) so a dropping device reads the same way
        in both UIs.
        """
        now = time.time()
        cutoff = now - _CHANNEL_FLASH_DURATION_S

        # ---- data badge (channels 0-36) ---------------------------------
        # Drop expired entries while we're here so memory doesn't grow.
        self._data_flash_recent = [
            e for e in self._data_flash_recent if e[0] >= cutoff
        ]

        right_x = _BOX_W - _CH_FLASH_BADGE_MARGIN
        top_y = _CH_FLASH_BADGE_MARGIN
        badge_rect = QRectF(
            right_x - _CH_FLASH_BADGE_W, top_y,
            _CH_FLASH_BADGE_W, _CH_FLASH_BADGE_H,
        )

        if self._data_flash_recent:
            flash_t, flash_ch, flash_bad = self._data_flash_recent[-1]
            age = max(0.0, now - flash_t)
            alpha = max(0.0, 1.0 - age / _CHANNEL_FLASH_DURATION_S)

            prior = list(self._data_flash_recent[:-1])
            if prior:
                font = QFont()
                font.setPointSize(7)
                font.setBold(True)
                painter.setFont(font)
                for i, (t, ch, bad) in enumerate(reversed(prior)):
                    tail_age = max(0.0, now - t)
                    tail_alpha = max(
                        0.0, 1.0 - tail_age / _CHANNEL_FLASH_DURATION_S,
                    )
                    pip = QColor(
                        _FLASH_DROPOUT_BG if bad else _channel_color(ch)
                    )
                    pip.setAlphaF(tail_alpha * 0.85)
                    pip_rect = QRectF(
                        badge_rect.left()
                        - (i + 1) * (_CH_FLASH_TRAIL_W + 1),
                        top_y + 2,
                        _CH_FLASH_TRAIL_W, _CH_FLASH_BADGE_H - 4,
                    )
                    painter.setPen(Qt.PenStyle.NoPen)
                    painter.setBrush(QBrush(pip))
                    painter.drawRoundedRect(pip_rect, 2, 2)

            self._paint_flash_cell(
                painter, badge_rect, flash_ch, flash_bad, alpha,
                font_pt=8,
            )

        # ---- adv strip (channels 37/38/39) ------------------------------
        # Three squares right-aligned under the data badge. Even when
        # nothing has fired yet we draw faint placeholders so the user
        # knows where the indicators live.
        strip_total_w = (
            3 * _ADV_CH_BOX_W + 2 * _ADV_CH_BOX_GAP
        )
        strip_left = right_x - strip_total_w
        for idx, ch in enumerate((37, 38, 39)):
            cell_x = strip_left + idx * (_ADV_CH_BOX_W + _ADV_CH_BOX_GAP)
            cell_rect = QRectF(
                cell_x, _ADV_STRIP_TOP_Y, _ADV_CH_BOX_W, _ADV_CH_BOX_H,
            )
            entry = self._adv_flash.get(ch)
            if entry is not None and entry[0] >= cutoff:
                age = max(0.0, now - entry[0])
                alpha = max(0.0, 1.0 - age / _CHANNEL_FLASH_DURATION_S)
                self._paint_flash_cell(
                    painter, cell_rect, ch, entry[1], alpha,
                    font_pt=7, radius=3,
                )
            else:
                # Idle placeholder: faint outline of the channel color
                # so the user can locate the indicator at a glance.
                outline = QColor(_channel_color(ch))
                outline.setAlphaF(0.18)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.setPen(QPen(outline, 1))
                painter.drawRoundedRect(cell_rect, 3, 3)

    @staticmethod
    def _paint_flash_cell(
        painter: QPainter,
        rect: QRectF,
        channel: int,
        crc_fail: bool,
        alpha: float,
        *,
        font_pt: int,
        radius: int = 4,
    ) -> None:
        """Shared rendering for one flash cell (data badge or adv square).

        Picks the channel-color (or the dropout black/red pair for
        CRC failures) and draws filled rect + centered channel number.
        """
        if crc_fail:
            bg = QColor(_FLASH_DROPOUT_BG)
            fg = QColor(_FLASH_DROPOUT_FG)
        else:
            bg = QColor(_channel_color(channel))
            fg = QColor(_channel_text_color(channel))
        bg.setAlphaF(alpha)
        fg.setAlphaF(alpha)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(bg))
        painter.drawRoundedRect(rect, radius, radius)

        font = QFont()
        font.setBold(True)
        font.setPointSize(font_pt)
        painter.setFont(font)
        painter.setPen(QPen(fg))
        painter.drawText(
            rect,
            Qt.AlignmentFlag.AlignCenter,
            str(channel),
        )

    def _cluster_badge_text(self) -> str:
        return f"↔ {self.device.cluster_member_count}"

    def _cluster_badge_rect(self) -> QRectF:
        """Lower-left placement for the cluster badge.

        Anchored to the bottom-left corner of the box's bounding rect
        (collapsed or expanded — the bounding rect already accounts
        for state), with ``_CLUSTER_BADGE_MARGIN`` on both axes. The
        width grows with the digit count so "↔ 213" still fits.
        Returns an empty QRectF when the device isn't a cluster
        primary — callers can use ``isEmpty()`` as a "no badge" check.
        """
        if self.device.cluster_member_count <= 1:
            return QRectF()
        font = QFont()
        font.setBold(True)
        font.setPointSize(8)
        text_w = QFontMetricsF(font).horizontalAdvance(self._cluster_badge_text())
        badge_w = text_w + 2 * _CLUSTER_BADGE_PAD_X
        box_h = self.boundingRect().height()
        y = box_h - _CLUSTER_BADGE_H - _CLUSTER_BADGE_MARGIN
        return QRectF(
            _CLUSTER_BADGE_MARGIN, y, badge_w, _CLUSTER_BADGE_H,
        )

    def _paint_cluster_badge(self, painter: QPainter) -> None:
        """Render the ↔N badge in the box's lower-left corner.

        Only called when the device represents a collapsed cluster
        (more than one member). The badge sits over the bottom-left
        of the body, distinct in colour from any kind tint so it
        scans across a grid of boxes regardless of kind. The icon and
        title text in the header are no longer shifted to make room
        for it — the badge has its own corner now.
        """
        rect = self._cluster_badge_rect()
        if rect.isEmpty():
            return
        font = QFont()
        font.setBold(True)
        font.setPointSize(8)
        painter.setFont(font)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(_CLUSTER_BADGE_BG))
        painter.drawRoundedRect(rect, 4, 4)
        painter.setPen(QPen(_CLUSTER_BADGE_FG))
        painter.drawText(
            rect, Qt.AlignmentFlag.AlignCenter, self._cluster_badge_text(),
        )

    def _paint_collapsed_body(self, painter: QPainter) -> None:
        d = self.device
        all_chs = sorted(d.channels.items(), key=lambda kv: -kv[1])
        top_chs = all_chs[:3]
        if all_chs:
            ch_str = "/".join(str(c) for c, _ in top_chs)
            extra = len(all_chs) - len(top_chs)
            if extra > 0:
                ch_str += f" +{extra}"
        else:
            ch_str = "—"

        # Signal-strength bar at the top of the body. ``_paint_signal_line``
        # is the vertical mirror of ``_paint_quality_line``: dBm label
        # + ▼ caret on top, gradient bar below. Total height ~25 px
        # from the y origin to the bar's bottom.
        self._paint_signal_line(painter, _HEADER_H + 4)

        # Pkt count + channels — single line between the two bars.
        # ``live_packet_delta`` adds packets attributed since the most
        # recent reload so the counter ticks live instead of jumping
        # once per ~2 s reload cycle.
        pkts = d.packet_count + self._live.live_packet_delta
        line = f"{pkts:,} pkts · ch {ch_str}"
        painter.drawText(
            QRectF(8, _HEADER_H + 32, _BOX_W - 16, 14),
            Qt.AlignVCenter | Qt.AlignLeft, line,
        )

        # CRC quality bar at the bottom (bar + ▲ caret + % below it).
        self._paint_quality_line(painter, _HEADER_H + 48)

    def _paint_signal_line(self, painter: QPainter, y: float) -> None:
        """Render the per-device signal-strength bar.

        Layout (vertical mirror of ``_paint_quality_line``):

            [Signal]   −64 dBm
                          ▼
                       [██░░░░░░]

        Bar maps the [_SIGNAL_RSSI_MIN, _SIGNAL_RSSI_MAX] range to a
        red→amber→green gradient. The caret's x position tracks the
        average RSSI over the last ``_RECENT_WINDOW_S`` seconds —
        not the session aggregate — so the meter reads "right now."
        Values outside the range clamp to the bar's edges. When no
        RSSI samples are in-window, paints a neutral grey bar with no
        caret/label so the layout slot stays consistent.
        """
        rssi_avg, _good, _bad = self._recent_stats()

        # Same horizontal layout as the quality bar so the two bars
        # align under each other.
        label_x = 8
        bar_left = label_x + _QUALITY_LABEL_W + 6
        bar_right = _BOX_W - 8
        bar_w = bar_right - bar_left

        # Bar sits at the BOTTOM of the line so caret + label can stack
        # above it. Total reserved height ≈ 25 px (10 label + 4 caret +
        # 1 gap + 8 bar + a little padding).
        label_h = 10
        caret_h = _QUALITY_CARET_H
        bar_top = y + label_h + caret_h + 1
        bar_bottom = bar_top + _SIGNAL_BAR_H

        # "Signal" label, vertically centered against the bar (mirrors
        # the "Quality" label position so the two lines read aligned).
        label_font = QFont()
        label_font.setPointSize(8)
        label_font.setBold(True)
        painter.setFont(label_font)
        painter.setPen(QColor(50, 50, 50))
        painter.drawText(
            QRectF(label_x, bar_top - 1, _QUALITY_LABEL_W, _SIGNAL_BAR_H + 2),
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
            "Signal",
        )

        if rssi_avg is None:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(_QUALITY_NEUTRAL))
            painter.drawRoundedRect(
                QRectF(bar_left, bar_top, bar_w, _SIGNAL_BAR_H),
                _SIGNAL_BAR_RADIUS, _SIGNAL_BAR_RADIUS,
            )
            return

        # Three-stop horizontal gradient (red → amber → green).
        gradient = QLinearGradient(bar_left, 0, bar_right, 0)
        gradient.setColorAt(0.0, _SIGNAL_LO)
        gradient.setColorAt(0.5, _SIGNAL_MID)
        gradient.setColorAt(1.0, _SIGNAL_HI)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(gradient))
        painter.drawRoundedRect(
            QRectF(bar_left, bar_top, bar_w, _SIGNAL_BAR_H),
            _SIGNAL_BAR_RADIUS, _SIGNAL_BAR_RADIUS,
        )

        # Caret position: linear map clamped to [_SIGNAL_RSSI_MIN, MAX].
        rssi_clamped = max(
            _SIGNAL_RSSI_MIN, min(_SIGNAL_RSSI_MAX, rssi_avg),
        )
        frac = (
            (rssi_clamped - _SIGNAL_RSSI_MIN)
            / (_SIGNAL_RSSI_MAX - _SIGNAL_RSSI_MIN)
        )
        caret_x = bar_left + frac * bar_w

        # Caret pointing DOWN at the bar from above.
        caret_bottom_y = bar_top - 1
        caret_top_y = caret_bottom_y - _QUALITY_CARET_H
        caret_poly = QPolygonF([
            QPointF(caret_x, caret_bottom_y),
            QPointF(
                caret_x - _QUALITY_CARET_W / 2.0,
                caret_top_y,
            ),
            QPointF(
                caret_x + _QUALITY_CARET_W / 2.0,
                caret_top_y,
            ),
        ])
        painter.setBrush(QBrush(_QUALITY_CARET_FILL))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawPolygon(caret_poly)

        # dBm label, centered above the caret. Clipped to the bar's
        # horizontal bounds so it stays visible at extremes.
        label_str = f"{rssi_avg:.0f} dBm"
        val_font = QFont()
        val_font.setPointSize(7)
        painter.setFont(val_font)
        metrics = painter.fontMetrics()
        val_w = metrics.horizontalAdvance(label_str)
        val_x = caret_x - val_w / 2.0
        if val_x < bar_left:
            val_x = bar_left
        elif val_x + val_w > bar_right:
            val_x = bar_right - val_w
        val_y = caret_top_y - label_h
        painter.setPen(QColor(50, 50, 50))
        painter.drawText(
            QRectF(val_x, val_y, val_w + 1, label_h),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
            label_str,
        )

    def _paint_quality_line(self, painter: QPainter, y: float) -> None:
        """Render the per-device CRC-quality bar.

        Layout:

            [Quality] [████████████░░░░]
                                  ▲
                                 95%

        Rolling window over the last ``_RECENT_WINDOW_S`` seconds.
        Green segment width = good_packet_share; red segment width =
        CRC-fail share. The caret marks the boundary; the percentage
        below it is the good-share rounded to a whole number. When no
        packets are in-window, the bar shows a neutral grey fill with
        no caret — the device is silent right now.
        """
        _rssi, good, bad = self._recent_stats()
        total = good + bad

        # Layout — left label, bar to its right, caret + % beneath.
        label_x = 8
        bar_left = label_x + _QUALITY_LABEL_W + 6
        bar_right = _BOX_W - 8
        bar_w = bar_right - bar_left
        bar_top = y + 1

        # "Quality" label, vertically centered against the bar.
        label_font = QFont()
        label_font.setPointSize(8)
        label_font.setBold(True)
        painter.setFont(label_font)
        painter.setPen(QColor(50, 50, 50))
        painter.drawText(
            QRectF(label_x, y, _QUALITY_LABEL_W, _QUALITY_BAR_H + 2),
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
            "Quality",
        )

        if total == 0:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(_QUALITY_NEUTRAL))
            painter.drawRoundedRect(
                QRectF(bar_left, bar_top, bar_w, _QUALITY_BAR_H),
                _QUALITY_BAR_RADIUS, _QUALITY_BAR_RADIUS,
            )
            return

        good_frac = good / total
        boundary_x = bar_left + good_frac * bar_w

        painter.setPen(Qt.PenStyle.NoPen)
        # Green segment (good). Always painted because a 0%-good edge
        # case still wants a faint green sliver to indicate "this is
        # the good-side of the bar."
        if good_frac > 0:
            painter.setBrush(QBrush(_QUALITY_GREEN))
            painter.drawRoundedRect(
                QRectF(
                    bar_left, bar_top,
                    boundary_x - bar_left, _QUALITY_BAR_H,
                ),
                _QUALITY_BAR_RADIUS, _QUALITY_BAR_RADIUS,
            )
        # Red segment (CRC-fail).
        if good_frac < 1.0:
            painter.setBrush(QBrush(_QUALITY_RED))
            painter.drawRoundedRect(
                QRectF(
                    boundary_x, bar_top,
                    bar_right - boundary_x, _QUALITY_BAR_H,
                ),
                _QUALITY_BAR_RADIUS, _QUALITY_BAR_RADIUS,
            )

        # Caret pointing UP to the boundary, sitting just under the bar.
        caret_top_y = bar_top + _QUALITY_BAR_H + 1
        caret_poly = QPolygonF([
            QPointF(boundary_x, caret_top_y),
            QPointF(
                boundary_x - _QUALITY_CARET_W / 2.0,
                caret_top_y + _QUALITY_CARET_H,
            ),
            QPointF(
                boundary_x + _QUALITY_CARET_W / 2.0,
                caret_top_y + _QUALITY_CARET_H,
            ),
        ])
        painter.setBrush(QBrush(_QUALITY_CARET_FILL))
        painter.drawPolygon(caret_poly)

        # Percentage label, centered under the caret. Clipped to the
        # bar's horizontal bounds so it stays visible at extremes.
        pct_str = f"{int(round(good_frac * 100))}%"
        pct_font = QFont()
        pct_font.setPointSize(7)
        painter.setFont(pct_font)
        metrics = painter.fontMetrics()
        pct_w = metrics.horizontalAdvance(pct_str)
        pct_x = boundary_x - pct_w / 2.0
        if pct_x < bar_left:
            pct_x = bar_left
        elif pct_x + pct_w > bar_right:
            pct_x = bar_right - pct_w
        pct_y = caret_top_y + _QUALITY_CARET_H + 1
        painter.setPen(QColor(50, 50, 50))
        painter.drawText(
            QRectF(pct_x, pct_y, pct_w + 1, 10),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
            pct_str,
        )

    def _paint_expanded_body(self, painter: QPainter) -> None:
        d = self.device
        y = _HEADER_H + 4
        lh = 13  # line height

        def line(txt: str) -> None:
            nonlocal y
            painter.drawText(QRectF(8, y, _BOX_W - 16, lh),
                             Qt.AlignVCenter | Qt.AlignLeft, txt)
            y += lh

        rssi = (
            f"{d.rssi_avg:.0f} dBm (min {d.rssi_min}, max {d.rssi_max})"
            if d.rssi_avg is not None else "—"
        )
        line(f"id: {d.device_id}")
        if d.cluster_id is not None:
            score = (
                f" (min {d.cluster_min_score:.2f})"
                if d.cluster_min_score is not None else ""
            )
            line(
                f"cluster: {d.cluster_id} · "
                f"{d.cluster_member_count} mem{score}"
            )
        line(f"kind: {d.kind}")
        # Class is what determines the icon and most of the label fallback —
        # users want to know where it came from. Show the class string and
        # the appearance value (if any) that produced it.
        if d.device_class:
            line(f"class: {d.device_class}")
        if d.appearance is not None:
            line(f"appearance: 0x{d.appearance:04X}")
        pkts_total = d.packet_count + self._live.live_packet_delta
        line(f"pkts: {pkts_total:,} (adv {d.adv_count:,}, data {d.data_count:,})")
        line(f"rssi: {rssi}")

        # Identity strings — show every name source so it's obvious which
        # one drove the label. Empty values stay unprinted to save vertical
        # space; the tooltip lists them all regardless.
        vendor = d.vendor or d.oui_vendor or "—"
        line(f"vendor: {self._truncate(vendor, 26)}")
        if d.model:
            line(f"model: {self._truncate(d.model, 28)}")
        if d.user_name:
            line(f"user_name: {self._truncate(d.user_name, 24)}")
        if d.gatt_device_name:
            line(f"gatt_name: {self._truncate(d.gatt_device_name, 24)}")
        if d.local_name:
            line(f"local_name: {self._truncate(d.local_name, 22)}")
        if d.broadcast_name:
            line(f"broadcast: {self._truncate(d.broadcast_name, 23)}")

        # Top PDU types
        if d.pdu_types:
            top = sorted(d.pdu_types.items(), key=lambda kv: -kv[1])[:3]
            line("pdu: " + ", ".join(f"{k}={v}" for k, v in top))
        if d.channels:
            top_chs = sorted(d.channels.items(), key=lambda kv: -kv[1])[:5]
            line("ch:  " + ", ".join(f"{c}:{v}" for c, v in top_chs))
        line(f"addresses ({len(d.addresses)}):")
        for addr, _atype in d.addresses[:4]:
            line(f"  {addr}")
        if len(d.addresses) > 4:
            line(f"  +{len(d.addresses) - 4} more")

        # CRC-quality bar at the bottom of the body. Same rendering
        # as the collapsed view so the indicator's location stays
        # consistent when the user expands a box. 26 px reserves room
        # for the bar + caret + percentage label below it.
        body_h = _BOX_H_EXPANDED - _HEADER_H
        self._paint_quality_line(painter, _HEADER_H + body_h - 26)

    @staticmethod
    def _truncate(s: str, n: int) -> str:
        return s if len(s) <= n else s[: n - 1] + "…"

    def _state_z(self) -> int:
        """Z value this item should have when not actively being dragged."""
        return _Z_EXPANDED if not self.device.collapsed else _Z_NORMAL

    # --- interaction --------------------------------------------------

    def hoverMoveEvent(self, event) -> None:
        """Swap the active tooltip based on which region the cursor
        is over. The cluster badge gets its own tooltip carrying the
        cluster identity and the merged-address list; everywhere else
        on the box shows the main identity tooltip. Both strings are
        pre-built in __init__ so this is just a rect-contains check
        per move event.
        """
        if (
            self._cluster_tooltip
            and self._cluster_badge_rect().contains(event.pos())
        ):
            if self.toolTip() != self._cluster_tooltip:
                self.setToolTip(self._cluster_tooltip)
        else:
            if self.toolTip() != self._main_tooltip:
                self.setToolTip(self._main_tooltip)
        super().hoverMoveEvent(event)

    def mousePressEvent(self, event) -> None:
        # Float to the top while the user is interacting with this box —
        # ensures any drag movement (and the rubber-band selection halo)
        # paints above neighbors, including expanded ones.
        self.setZValue(_Z_DRAGGING)
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        self.prepareGeometryChange()
        self.device.collapsed = not self.device.collapsed
        self.setZValue(self._state_z())
        self.update()
        self._persist(self.device, save_pos=False)
        event.accept()

    def _persist_moved_selection(self) -> None:
        """Persist position for *every* selected item that actually moved.

        Qt translates a multi-selection drag by repositioning all selected
        items together, but only the grabbed item's release event fires —
        so we walk the selection here and write each one whose position
        drifted past a small dead-band. Also covers the (rare) case where
        a drag happens on an unselected item.
        """
        scene = self.scene()
        if scene is None:
            return
        moved: list[DeviceItem] = []
        for item in scene.selectedItems():
            if not isinstance(item, DeviceItem):
                continue
            p = item.pos()
            if (abs(p.x() - item.device.pos_x) > 0.5
                    or abs(p.y() - item.device.pos_y) > 0.5):
                item.device.pos_x = float(p.x())
                item.device.pos_y = float(p.y())
                moved.append(item)
        if not self.isSelected():
            p = self.pos()
            if (abs(p.x() - self.device.pos_x) > 0.5
                    or abs(p.y() - self.device.pos_y) > 0.5):
                self.device.pos_x = float(p.x())
                self.device.pos_y = float(p.y())
                moved.append(self)
        for item in moved:
            item._persist(item.device, save_pos=True)

    def mouseReleaseEvent(self, event) -> None:
        super().mouseReleaseEvent(event)
        # Settle the dragged item back into its state-appropriate stack.
        self.setZValue(self._state_z())
        self._persist_moved_selection()

    def contextMenuEvent(self, event) -> None:
        """Right-click → menu of device actions, built by the scene.

        The callback returns a fully-assembled ``QMenu`` (rather than a
        list of actions) so it can include submenus — the Copy submenu
        is built that way.
        """
        if self._context_cb is None:
            return
        menu = self._context_cb(self.device)
        if menu is None:
            return
        menu.exec(event.screenPos())
        event.accept()


# ──────────────────────────────────────────────────────────────────────────
# Canvas view with directional rubber-band selection
# ──────────────────────────────────────────────────────────────────────────

class _CanvasView(QGraphicsView):
    """Adobe / CAD-style directional rubber-band selection.

    Drag from upper-left toward lower-right → only items *fully enclosed*
    by the rubber band get selected (``ContainsItemShape``).
    Drag in any other direction → items *intersecting* the rubber band
    get selected (``IntersectsItemShape``).

    Implementation: we don't replace Qt's rubber-band drag — we just
    flip its selection mode mid-drag based on where the mouse is now
    relative to where it pressed down. ``setRubberBandSelectionMode``
    takes effect on the live rubber-band selection, so the user sees the
    selection change as they reverse direction.
    """

    def __init__(self, scene: QGraphicsScene) -> None:
        super().__init__(scene)
        self.setDragMode(QGraphicsView.RubberBandDrag)
        self.setRubberBandSelectionMode(Qt.IntersectsItemShape)
        self._press_pos = None

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self._press_pos = event.position().toPoint()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._press_pos is not None and event.buttons() & Qt.LeftButton:
            cur = event.position().toPoint()
            dx = cur.x() - self._press_pos.x()
            dy = cur.y() - self._press_pos.y()
            mode = (Qt.ContainsItemShape
                    if dx > 0 and dy > 0
                    else Qt.IntersectsItemShape)
            if self.rubberBandSelectionMode() != mode:
                self.setRubberBandSelectionMode(mode)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        self._press_pos = None
        super().mouseReleaseEvent(event)


# ──────────────────────────────────────────────────────────────────────────
# Project picker
# ──────────────────────────────────────────────────────────────────────────

class _ConfirmDialog(QDialog):
    """Tiny modal Yes/No prompt — used in place of QMessageBox.question,
    which segfaults on macOS Tahoe + PySide6 6.11 in the Qt metaobject
    builder. Defaults focus to "No" so Enter doesn't confirm a destructive
    action by accident.
    """

    def __init__(self, parent, title: str, message: str) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        layout = QVBoxLayout(self)
        msg = QLabel(message)
        msg.setWordWrap(True)
        layout.addWidget(msg)
        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Yes
            | QDialogButtonBox.StandardButton.No
        )
        bb.button(QDialogButtonBox.StandardButton.No).setDefault(True)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        layout.addWidget(bb)


class ProjectPicker(QDialog):
    """Dialog shown at launch to pick / create / delete the active project."""

    def __init__(self, store: Store, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("btviz — Select Project")
        self.resize(380, 180)
        self.store = store
        self.repos = Repos(store)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Project:"))
        self.combo = QComboBox()
        layout.addWidget(self.combo)

        # Side-by-side row: New / Delete. Delete is disabled until a
        # project is selected and turned red so the destructive action
        # reads as such.
        from PySide6.QtWidgets import QHBoxLayout
        action_row = QHBoxLayout()
        new_btn = QPushButton("New project…")
        new_btn.clicked.connect(self._new_project)
        action_row.addWidget(new_btn)
        self._delete_btn = QPushButton("Delete project…")
        self._delete_btn.setStyleSheet("color: #b00;")
        self._delete_btn.clicked.connect(self._delete_project)
        action_row.addWidget(self._delete_btn)
        layout.addLayout(action_row)

        # Inline status label for transient messages (e.g. "name already
        # exists"). Used instead of QMessageBox.warning, which crashes on
        # macOS Tahoe + PySide6 6.11 in the Qt metaobject builder. Empty
        # by default; rendered in red when populated.
        self._status_label = QLabel("")
        self._status_label.setStyleSheet("color: #b00; font-size: 11px;")
        layout.addWidget(self._status_label)

        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        layout.addWidget(bb)

        self.combo.currentIndexChanged.connect(self._update_delete_enabled)
        self._reload(select_last=True)
        self._update_delete_enabled()

    def _update_delete_enabled(self) -> None:
        self._delete_btn.setEnabled(self.combo.count() > 0)

    def _reload(self, select_id: int | None = None, *, select_last: bool = False) -> None:
        self.combo.clear()
        projects = self.repos.projects.list()
        for p in projects:
            self.combo.addItem(p.name, p.id)
        if select_id is not None:
            idx = next(
                (i for i in range(self.combo.count())
                 if self.combo.itemData(i) == select_id),
                -1,
            )
            if idx >= 0:
                self.combo.setCurrentIndex(idx)
                return
        if select_last:
            last = self.repos.meta.get(self.repos.meta.LAST_PROJECT)
            if last:
                try:
                    self._reload(select_id=int(last))
                except (TypeError, ValueError):
                    pass

    def _new_project(self) -> None:
        # Clear any prior inline error before this attempt.
        self._status_label.setText("")
        name, ok = QInputDialog.getText(self, "New project", "Name:")
        if not ok or not name.strip():
            return
        name = name.strip()
        if self.repos.projects.get_by_name(name):
            # Inline message instead of QMessageBox.warning (see __init__
            # for why). Selecting the existing entry in the combo is the
            # natural follow-through.
            self._status_label.setText(
                f"Project {name!r} already exists — selected."
            )
            existing = self.repos.projects.get_by_name(name)
            self._reload(select_id=existing.id if existing else None)
            return
        proj = self.repos.projects.create(name)
        self._reload(select_id=proj.id)

    def _delete_project(self) -> None:
        """Delete the currently-selected project after confirmation.

        FK cascades take care of sessions, observations, broadcasts,
        device_layouts, groups, and per-project device meta. Devices
        and addresses are global and stay (other projects may have
        observed them too).
        """
        self._status_label.setText("")
        i = self.combo.currentIndex()
        if i < 0:
            return
        proj_id = self.combo.itemData(i)
        proj_name = self.combo.itemText(i)
        if proj_id is None:
            return
        confirm = _ConfirmDialog(
            self,
            "Delete project?",
            f"Permanently delete project '{proj_name}' and all its "
            "sessions, observations, broadcasts, and canvas layout?\n\n"
            "Devices and addresses are global and will remain.",
        )
        if confirm.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            self.repos.projects.delete(int(proj_id))
        except Exception as e:  # noqa: BLE001
            self._status_label.setText(f"Delete failed: {e}")
            return
        # Clear LAST_PROJECT if it pointed at this one so the next
        # picker open doesn't try to re-select a deleted id.
        last = self.repos.meta.get(self.repos.meta.LAST_PROJECT)
        if last and str(last) == str(proj_id):
            self.repos.meta.set(self.repos.meta.LAST_PROJECT, "")
        self._reload()
        self._update_delete_enabled()

    def selected_project_id(self) -> int | None:
        i = self.combo.currentIndex()
        return None if i < 0 else self.combo.itemData(i)


# ──────────────────────────────────────────────────────────────────────────
# Cluster analysis worker
# ──────────────────────────────────────────────────────────────────────────

class _ClusterWorker(QObject):
    """Persistent worker that runs ``ClusterRunner.run_once`` on a
    worker QThread.

    Lifetime model: created once at canvas startup, lives for the
    entire window. Each cluster pass dispatches a ``run_request``
    invocation via a queued signal connection from the canvas; the
    worker runs the pass on its own thread, emits ``finished`` (or
    ``failed``), then sits idle in its event loop waiting for the
    next request.

    Why persistent rather than per-pass:
    Earlier designs created a fresh ``QThread`` + ``_ClusterWorker``
    per tick and tore them down via the ``deleteLater`` chain. Two
    user-reproducible crashes traced to a Shiboken-vs-Qt ownership
    race during teardown — Python's wrapper finalizer and Qt's
    DeferredDelete event would both try to destroy the same C++
    QObject, just on different threads. Persistent objects avoid
    the entire teardown question: nothing is destroyed mid-run.
    Cleanup happens only at canvas close, when the thread is quit
    and joined deterministically before Python releases anything.

    The worker opens a fresh sqlite3 connection per request and
    closes it before emitting ``finished``. The main thread's
    connection is locked to the GUI thread; WAL mode (set in
    ``Store.__init__``) lets the worker read concurrently with
    whatever the main thread is doing.
    """

    finished = Signal(object)   # RunResult
    failed = Signal(str)        # error message

    @Slot(object, object, object, int)
    def run_request(
        self, ctx_proto, db_path, devices, max_per_class: int,
    ) -> None:
        """Execute one cluster pass. Runs on the worker thread.

        Receives all inputs as queued-signal arguments rather than
        reading instance state — keeps the worker stateless between
        runs and avoids any cross-thread reads of mutable state.
        ``max_per_class`` arrives per-call so a live preferences
        change takes effect on the next cluster tick (no worker
        restart needed).
        """
        try:
            import sqlite3
            from types import SimpleNamespace
            from ..cluster import ClusterRunner
            from ..cluster.base import ClusterContext

            conn = sqlite3.connect(str(db_path), isolation_level=None)
            conn.row_factory = sqlite3.Row
            try:
                local_ctx = ClusterContext(
                    signals=ctx_proto.signals,
                    profiles=ctx_proto.profiles,
                    now=ctx_proto.now,
                    db=SimpleNamespace(conn=conn),
                )
                runner = ClusterRunner(local_ctx, max_per_class=max_per_class)
                result = runner.run_once(devices)
            finally:
                conn.close()
            self.finished.emit(result)
        except Exception as e:  # noqa: BLE001 — surface as failed signal
            self.failed.emit(repr(e))


# ──────────────────────────────────────────────────────────────────────────
# Main window
# ──────────────────────────────────────────────────────────────────────────

class CanvasWindow(QMainWindow):
    # Class-level signal used to dispatch a cluster-analysis request to
    # the persistent ``_ClusterWorker``. Emission is queued across
    # threads automatically, so the worker's slot runs on its own
    # thread regardless of where ``emit`` is called.
    _dispatch_cluster = Signal(object, object, object, int)

    # Initial-discovery sweep result. Emitted by a daemon thread at
    # startup once the fast (ioreg) + slow (extcap) probes have run.
    # Carries either the per-dongle records list (DB-shaped dicts
    # produced by ``discovered_to_db_records``) or ``None`` when
    # both probes failed — the slot interprets ``None`` as
    # "discovery didn't actually run, leave the DB alone" so a
    # transient double-failure can't deactivate plugged-in dongles.
    _initial_discovery_signal = Signal(object)

    def __init__(self, store: Store, project_id: int) -> None:
        super().__init__()
        self.store = store
        self.repos = Repos(store)
        self.project_id = project_id
        self.project = self.repos.projects.get(project_id)
        self.setWindowTitle(f"btviz canvas — {self.project.name}")
        # 1600 px is the smallest width that fits the full toolbar
        # (Start Capture + capture group + view group + cluster group)
        # without truncating the trailing combos. Smaller widths still
        # work — the toolbar provides its own overflow menu — but a
        # default that fits everything saves the user from having to
        # discover the overflow chevron.
        self.resize(1600, 900)

        self.scene = QGraphicsScene()
        self.view = _CanvasView(self.scene)
        self.view.setRenderHints(
            QPainter.Antialiasing | QPainter.TextAntialiasing
        )
        # Anchor scene to the top-left so device boxes stay at the
        # left edge regardless of viewport width. QGraphicsView's
        # default ``Qt.AlignCenter`` floats the scene to the middle
        # when it's narrower than the viewport, which made small
        # device counts look "centered" rather than left-aligned.
        self.view.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
        )

        # Sniffer panel + canvas view live side by side in a HBoxLayout
        # so expanding the panel pushes canvas content right rather than
        # covering it. Below them, the channel-spectrum strip spans the
        # full width — collapsed it's a per-channel activity-indicator
        # row, expanded it grows into a histogram. Wrapping the row in
        # a VBoxLayout keeps both vertical neighbours' widths in sync.
        from .channel_strip import ChannelStrip
        from .sniffer_panel import SnifferPanel
        self.sniffer_panel = SnifferPanel(store=store)
        # The panel's top slot used to host a "Refresh" button that
        # re-ran USB discovery. After PR #76's pyserial-based
        # discovery, fast and slow paths converged and Start Capture
        # already triggers a fresh sweep — so the button became
        # redundant. The slot now hosts the session timer
        # (start_session_timer / stop_session_timer) driven from
        # _start_live / _stop_live.
        self.channel_strip = ChannelStrip()

        central = QWidget()
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        upper = QWidget()
        layout = QHBoxLayout(upper)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.sniffer_panel)
        layout.addWidget(self.view, 1)  # stretch factor 1 → view fills the rest

        outer.addWidget(upper, 1)
        outer.addWidget(self.channel_strip, 0)
        self.setCentralWidget(central)

        # Live-capture state. Created on first Start; recycled across
        # Start/Stop toggles within the same CanvasWindow instance.
        self._bus: EventBus | None = None
        self._coord: CaptureCoordinator | None = None
        self._live: LiveIngest | None = None
        self._live_timer: QTimer | None = None
        # Stall watchdog — set up on _start_live, torn down on
        # _stop_live. None outside of an active capture session.
        self._stall_watchdog = None
        # Most-recent stderr message per sniffer short_id, populated by
        # ``_on_sniffer_state`` and rendered by ``_live_tick`` as part
        # of the toolbar status prefix. Held on the canvas (not the
        # watchdog) because it tracks SnifferProcess stderr surfacing,
        # which is independent of stall detection.
        self._sniffer_errors: dict[str, str] = {}
        self._reload_tick = 0       # increments per timer fire; reload() runs every Nth
        # Per-device live state surviving scene.clear() during reload:
        # rolling-window samples for Signal/Quality, channel-flash tails,
        # and a live packet-count delta on top of the DB total. See
        # ``_DeviceLiveState``. Pruned in reload() to drop entries for
        # devices that have fallen out of load_canvas_devices.
        self._live_state: dict[int, _DeviceLiveState] = {}
        # Wall-clock at the moment the most recent capture session ended,
        # or None while a session is active (or before the first Start).
        # Used by the top "Devices" section to FREEZE opacity-fade — top
        # devices use this as ``now`` instead of time.time(), so a device
        # at 60% opacity at the moment of Stop stays at 60% until the
        # next Start. Bottom section ignores this and always uses real
        # time so its boxes continue to age out as if capture never
        # ended.
        self._capture_stopped_at: float | None = None
        # Cluster-runner state. ``_cluster_ctx`` is built lazily on the
        # first cluster tick; profiles + signals are static so we cache
        # the context for the lifetime of the window. ``_cluster_tick``
        # paces the heavy O(n²) cluster pass at a longer cadence than
        # the scene reload. ``_cluster_period_ticks`` controls cadence:
        # 0 = off (manual-only), N = run every N _live_tick callbacks
        # (each tick is 250 ms). Defaults to 60 = 15s.
        self._cluster_ctx = None
        self._cluster_tick = 0
        self._cluster_period_ticks = 60
        # Cluster analysis runs on a worker thread so a multi-second
        # pass on a 4000-device class never freezes the UI.
        # ``_cluster_busy`` gates new dispatches: while a run is in
        # flight we drop subsequent reload-tick triggers rather than
        # queueing them, since the data we'd cluster against would
        # be the same set the in-flight run is already chewing on.
        #
        # Worker + thread are created once and live for the lifetime
        # of the window. Per-tick spawn/destroy was tried earlier and
        # produced a Shiboken-vs-Qt double-delete race during teardown
        # (Python wrapper finalize on main thread vs. Qt DeferredDelete
        # on worker thread). Persistent objects sidestep the entire
        # teardown question — only ``closeEvent`` quits + joins the
        # thread, after which Python release is safe.
        self._cluster_busy = False
        self._cluster_thread = QThread(self)
        self._cluster_worker = _ClusterWorker()
        self._cluster_worker.moveToThread(self._cluster_thread)
        self._dispatch_cluster.connect(self._cluster_worker.run_request)
        self._cluster_worker.finished.connect(self._on_cluster_finished)
        self._cluster_worker.failed.connect(self._on_cluster_failed)
        self._cluster_thread.start()
        # Stale-device cutoff in seconds. Devices whose latest
        # observation is older than this are hidden from the canvas AND
        # excluded from the cluster hydrator. ``None`` disables the
        # filter. Default 1m matches the toolbar dropdown's default —
        # narrow enough that the canvas only shows what's actively on
        # the air right now, which is what you want during cluster
        # review work.
        self._stale_window_s: float | None = 60.0
        # short_id (pkt.source) → serial_number, so the bus subscriber's
        # per-source notifications can drive the panel's serial-keyed
        # activity dot.
        self._source_to_serial: dict[str, str] = {}
        # Dongles found by the last extcap slow-probe (at capture start).
        # Cached here so "Refresh Sniffers" can include hub-connected dongles
        # even after the coordinator is torn down on stop.
        self._last_coord_dongles: list = []
        # Bus unsubscribe callback for TOPIC_SNIFFER_STATE — set in
        # _start_live and cleared in _stop_live. Surfaces per-sniffer
        # errors (notably capture-loop FIFO failures) on the toolbar.
        self._sniffer_state_unsub: "Callable[[], None] | None" = None

        # Active sort keys (None = honor saved per-device layouts). A
        # transient view-mode toggle: changing either dropdown triggers
        # an immediate re-flow without persisting to device_layouts, so
        # sorts don't clobber positions the user has dragged. Reset
        # layout / Clear all data go through their own paths.
        self._current_sort_primary: str | None = None
        self._current_sort_secondary: str | None = None

        tb = QToolBar("main")
        self.addToolBar(tb)
        # Uniform 12 pt across every widget added to the toolbar so
        # action labels, combos, and the QLabel separators all read
        # at the same baseline. Without this Qt picks system defaults
        # per-widget-type (action button text, combo, label) which
        # don't always match — produces a "haphazard" look. The
        # capture-pill stylesheet sets its own font-size to stay in
        # sync; everything else inherits this.
        _tb_font = QFont()
        _tb_font.setPointSize(12)
        tb.setFont(_tb_font)
        # Toolbar layout reads left-to-right as three logical groups
        # separated by visual breaks: Capture (primary action) → View
        # (canvas filters / sort) → Cluster (analysis). Low-frequency
        # and destructive actions (Reload, Reset layout, Refresh
        # sniffers, Clear all data) live in an overflow menu at the
        # right edge so they don't compete for attention with the
        # day-to-day controls.

        # ---- Capture group --------------------------------------------------
        # "Start Capture" is the primary action of this window — the
        # whole reason a user opens the canvas during a live session.
        # We render it as a coloured pill so it stands out from the
        # plain text actions surrounding it. Colour swaps to red when
        # capture is running so "stop" reads as a distinct mode.
        self._live_action = tb.addAction(
            "Start Capture", self._toggle_live,
        )
        live_btn = tb.widgetForAction(self._live_action)
        if live_btn is not None:
            live_btn.setObjectName("captureButton")
            live_btn.setStyleSheet(_CAPTURE_BUTTON_STYLE_IDLE)
        self._live_button = live_btn
        # "Record packets" gates the per-packet write to the ``packets``
        # table. ON by default — required for ``rotation_cohort`` and
        # the future ``rssi_signature`` cluster signals, both of which
        # silently abstain without it. Locked during a live session —
        # read once at ``_start_live`` and applied to the IngestContext.
        # Cost: ~4 GB/day of DB growth under active capture; users who
        # don't want that can untoggle before clicking Start.
        # Rendered as a checkable QAction (mirrors "Verbose cluster log"
        # below) so the toolbar reads as a uniform row of buttons rather
        # than a button row with a stray checkbox indicator.
        self._keep_packets_action = tb.addAction("Record packets")
        self._keep_packets_action.setCheckable(True)
        self._keep_packets_action.setChecked(True)
        self._keep_packets_action.setToolTip(
            "Write each decoded packet to the packets table.\n"
            "Required for rotation_cohort and rssi_signature cluster\n"
            "signals — leaving this on lets the cluster runner merge\n"
            "RPA rotations across ~15-min boundaries. Cost: ~4 GB/day\n"
            "of DB growth under active capture; untoggle before Start\n"
            "if you don't want the per-packet history."
        )
        tb.addSeparator()

        # ---- View group -----------------------------------------------------
        # ``Show:`` first because the stale-window filter is the most-
        # adjusted view control during cluster review. ``Sort by`` /
        # ``then by`` follow with a fixed-width combo each so the
        # dropdowns line up regardless of label length.
        tb.addWidget(QLabel("  Show: "))
        self._stale_window_combo = QComboBox()
        for label in _STALE_WINDOW_LABELS:
            self._stale_window_combo.addItem(label)
        self._stale_window_combo.setCurrentText("1m")
        self._stale_window_combo.currentTextChanged.connect(
            self._on_stale_window_changed,
        )
        tb.addWidget(self._stale_window_combo)

        tb.addWidget(QLabel("   Sort: "))
        self._sort_combo_primary = QComboBox()
        self._sort_combo_primary.addItem("(saved positions)")
        for label in _SORT_KEY_LABELS:
            self._sort_combo_primary.addItem(label)
        self._sort_combo_primary.currentTextChanged.connect(self._on_sort_changed)
        tb.addWidget(self._sort_combo_primary)

        tb.addWidget(QLabel("   then: "))
        self._sort_combo_secondary = QComboBox()
        self._sort_combo_secondary.addItem("(none)")
        for label in _SORT_KEY_LABELS:
            self._sort_combo_secondary.addItem(label)
        self._sort_combo_secondary.setEnabled(False)  # disabled until primary picked
        self._sort_combo_secondary.currentTextChanged.connect(self._on_sort_changed)
        tb.addWidget(self._sort_combo_secondary)
        tb.addSeparator()

        # ---- Cluster group --------------------------------------------------
        # Manual button runs the aggregator on demand (works whether or
        # not capture is live — the aggregator only needs the DB). The
        # dropdown picks how often the live-tick path auto-runs it.
        # "off" makes capture-time analysis manual-only.
        tb.addAction("Run cluster", self._run_cluster_tick)
        tb.addWidget(QLabel("  every: "))
        self._cluster_period_combo = QComboBox()
        for label in _CLUSTER_PERIOD_LABELS:
            self._cluster_period_combo.addItem(label)
        self._cluster_period_combo.setCurrentText("15s")
        self._cluster_period_combo.currentTextChanged.connect(
            self._on_cluster_period_changed,
        )
        tb.addWidget(self._cluster_period_combo)
        # Verbose cluster log moved to Preferences (cluster.verbose_log)
        # — applied at app startup, no toolbar slot needed for a
        # diagnostic toggle that's mostly off in normal use.
        tb.addSeparator()

        # ---- Maintenance group (right side) --------------------------------
        # Reset layout / Clear all data sit in their own group to the
        # right of the cluster controls — visible at a glance but
        # separated from the day-to-day capture/view/cluster flow.
        # ``Clear all data…`` keeps its trailing ellipsis to signal it
        # opens a confirmation dialog before destroying anything.
        # ``Reload`` removed: the canvas auto-reloads every ~2 s during
        # live capture and after every state-changing action (rename,
        # set class, hide, follow, …), so a manual reload button never
        # had a reachable purpose. ``Refresh sniffers`` lives at the
        # top of the sniffer panel.
        tb.addAction("Reset layout", self.reset_layout)
        tb.addAction("Clear all data…", self.clear_all_data)
        # Preferences action — Qt routes this to the application menu
        # ("btviz → Preferences…") on macOS via the PreferencesRole;
        # on Linux/Windows it stays on the toolbar.
        prefs_action = tb.addAction("Preferences…", self._open_preferences)
        prefs_action.setMenuRole(QAction.MenuRole.PreferencesRole)
        tb.addSeparator()

        # ---- Status (right edge) -------------------------------------------
        # Spacer pushes the status label to the right edge so the
        # main groups stay left-aligned regardless of window width.
        spacer = QWidget()
        spacer.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred,
        )
        tb.addWidget(spacer)
        self.status = QLabel("")
        tb.addWidget(self.status)

        # Always-on canvas refresh. _live_tick reloads every 2s during
        # capture but stops firing on Stop, so without this timer
        # devices that age past the stale-window cutoff would still
        # show until the next manual Reload. 5s is fast enough to
        # match user expectations and slow enough to barely register
        # as work.
        self._canvas_refresh_timer = QTimer(self)
        self._canvas_refresh_timer.setInterval(5_000)
        self._canvas_refresh_timer.timeout.connect(self._maybe_refresh_canvas)
        self._canvas_refresh_timer.start()

        # 20 Hz repaint pump for the per-device channel-flash badges.
        # Only one of these widgets exists per canvas, and it only runs
        # cycles while at least one DeviceItem has an active flash —
        # the timer auto-stops when the scene goes idle so it doesn't
        # burn CPU at rest.
        self._channel_flash_timer = QTimer(self)
        self._channel_flash_timer.setInterval(50)
        self._channel_flash_timer.timeout.connect(self._tick_channel_flash)
        # Don't start until first hit; _on_device_packet kicks it off.

        # Defer the initial reload until after the window is shown. At
        # this point in __init__ the QGraphicsView's viewport hasn't
        # been laid out yet — viewport().width() returns Qt's default
        # (~300 px) which gives us a 2-column grid regardless of the
        # actual window size. QTimer.singleShot(0) posts the reload to
        # the event loop; by the time it fires, the show event has
        # finished and the viewport has its final width.
        QTimer.singleShot(0, self.reload)
        # Read whatever is already in the DB so the window draws immediately
        # (with possibly-stale activity dots), then schedule a background
        # discovery sweep that re-stamps is_active flags using BOTH the
        # fast (ioreg) and slow (extcap) probes. Without this pass, a user
        # who closed btviz with dongles plugged in, travelled, and reopened
        # btviz without them sees green dots from the prior session — the
        # exact "all green at startup, nothing actually plugged in"
        # confusion that prompted the no-dongles dialog. Combined fast +
        # slow mirrors what ``_start_live`` does so hub-connected dongles
        # (missed by fast alone) are still recognised. Runs on a daemon
        # thread so the window appears immediately and a slow extcap probe
        # doesn't block first paint.
        self.sniffer_panel.refresh()
        self._initial_discovery_signal.connect(self._on_initial_discovery_done)
        threading.Thread(
            target=self._initial_discovery_worker,
            daemon=True,
            name="btviz-initial-discovery",
        ).start()
        self.repos.meta.set(self.repos.meta.LAST_PROJECT, str(project_id))

    # --- data ---------------------------------------------------------

    def reload(self) -> None:
        self.scene.clear()
        cutoff = None
        recent_session_id: int | None = None
        if self._stale_window_s is not None:
            # Anchor the staleness cutoff to the most-recent session's
            # firmware clock. Each session has its own clock baseline
            # (dongle firmware uptime resets on replug), so a
            # project-wide MAX would let a session with a high
            # baseline swamp the active session — exactly the bug
            # where currently-flashing devices got filtered out at
            # short windows because some older session's leftover
            # observations had higher firmware ts. Per-session scope
            # keeps "now" in the live session's domain and the
            # per-device MAX in HAVING in the same domain.
            row = self.store.conn.execute(
                "SELECT id FROM sessions WHERE project_id = ? "
                "ORDER BY started_at DESC LIMIT 1",
                (self.project_id,),
            ).fetchone()
            recent_session_id = row["id"] if row else None
            if recent_session_id is not None:
                row = self.store.conn.execute(
                    "SELECT MAX(last_seen) AS now_ts FROM observations "
                    "WHERE session_id = ?",
                    (recent_session_id,),
                ).fetchone()
                packet_now = (
                    row["now_ts"] if row and row["now_ts"] else time.time()
                )
                cutoff = packet_now - self._stale_window_s
            else:
                # No sessions yet — fall back to wallclock so the
                # filter still does something for an empty project.
                cutoff = time.time() - self._stale_window_s
        devs = load_canvas_devices(
            self.store, self.project_id,
            stale_cutoff=cutoff,
            stale_session_id=recent_session_id,
        )

        # Prune _live_state to the devices we just loaded, and zero the
        # live packet delta on survivors — the fresh DB count already
        # includes everything that was attributed since the last reload,
        # so the delta starts again from 0 and ticks up between reloads.
        # Rolling-window samples and channel-flash tails carry across
        # untouched; they're time-decayed by their own pruning logic.
        live_ids = {d.device_id for d in devs}
        self._live_state = {
            dev_id: state
            for dev_id, state in self._live_state.items()
            if dev_id in live_ids
        }
        for state in self._live_state.values():
            state.live_packet_delta = 0

        # Partition into the two sections. Section assignment is
        # structural (see ``is_stable_device``) — when a device
        # migrates between sections (typically because the cluster
        # runner promoted an RPA into a multi-member cluster) we
        # auto-place it in the new section, overriding any saved
        # position from before. Saved positions WITHIN a section are
        # also overwritten in this PR — the previous "drop where I
        # left it" UX is deferred until the section split has settled.
        visible_devs = [d for d in devs if not d.hidden]
        top_devs = [d for d in visible_devs if is_stable_device(d)]
        bottom_devs = [d for d in visible_devs if not is_stable_device(d)]

        # Sort mode (toolbar dropdowns) sorts within each section.
        # Saved positions don't apply when sort is set.
        if self._current_sort_primary:
            p_fn = _SORT_KEYS.get(self._current_sort_primary)
            if p_fn is not None:
                s_fn = (
                    _SORT_KEYS.get(self._current_sort_secondary)
                    if self._current_sort_secondary else None
                )
                key_fn = (
                    (lambda d: (p_fn(d), s_fn(d)))
                    if s_fn is not None else p_fn
                )
                top_devs.sort(key=key_fn)
                bottom_devs.sort(key=key_fn)

        cols = cols_for_viewport(self.view.viewport().width())

        # ---- Top "Devices" section ------------------------------------
        # Heading sits at the very top with a small margin; the device
        # row begins below it with enough gap that the label is never
        # occluded — even if the row above migrates upward across
        # reloads.
        top_label_y = 6
        top_content_top = top_label_y + _SECTION_LABEL_H + 6
        self._add_section_label(
            _section_label_with_count(_SECTION_TOP_LABEL, len(top_devs)),
            top_label_y,
        )
        next_y = section_grid_layout(top_devs, cols=cols, top_y=top_content_top)
        if not top_devs:
            self._add_placeholder_text(
                "(no stable devices yet)",
                top_content_top + _SECTION_PLACEHOLDER_H / 2,
            )
            next_y = top_content_top + _SECTION_PLACEHOLDER_H

        # ---- Divider position + bottom "Unidentified" section ----------
        # The divider line itself is drawn AFTER the scene rect is
        # computed below — that's the only point where we know how
        # wide it should be (full scene width, not just enough for
        # the device columns we placed).
        divider_y = next_y + _SECTION_GAP_BEFORE_DIVIDER
        bottom_label_y = divider_y + _SECTION_GAP_AFTER_DIVIDER
        self._add_section_label(
            _section_label_with_count(_SECTION_BOTTOM_LABEL, len(bottom_devs)),
            bottom_label_y,
        )
        bottom_content_top = bottom_label_y + _SECTION_LABEL_H + 6
        bottom_next_y = section_grid_layout(
            bottom_devs, cols=cols, top_y=bottom_content_top,
        )
        if not bottom_devs:
            self._add_placeholder_text(
                "(no unidentified RPAs)",
                bottom_content_top + _SECTION_PLACEHOLDER_H / 2,
            )
            bottom_next_y = bottom_content_top + _SECTION_PLACEHOLDER_H

        # ---- Add the device items with section-aware opacity ----------
        # Single ``now`` reference so all opacities computed in this
        # reload pass see consistent ages — avoids tearing if reload
        # is triggered mid-tick.
        now_ts = time.time()
        # Top section freezes at capture-stop time. When capture is
        # active (or has never run), use real time.
        capture_active = self._live is not None and self._live.running
        top_now = (
            self._capture_stopped_at
            if (not capture_active and self._capture_stopped_at is not None)
            else now_ts
        )

        for d in top_devs:
            item = DeviceItem(
                d, self._persist_device,
                context_cb=self._device_context_menu,
                live_state=self._live_state.setdefault(
                    d.device_id, _DeviceLiveState(),
                ),
            )
            item.setOpacity(opacity_for_recency(
                d.last_seen, top_now, dormant_s=self._stale_window_s,
            ))
            self.scene.addItem(item)
        for d in bottom_devs:
            item = DeviceItem(
                d, self._persist_device,
                context_cb=self._device_context_menu,
                live_state=self._live_state.setdefault(
                    d.device_id, _DeviceLiveState(),
                ),
            )
            item.setOpacity(opacity_for_recency(
                d.last_seen, now_ts, dormant_s=self._stale_window_s,
            ))
            self.scene.addItem(item)

        # ---- Status + scene size --------------------------------------
        hidden_count = len(devs) - len(visible_devs)
        total_pkts = sum(d.packet_count for d in visible_devs)
        hidden_note = f" ({hidden_count} hidden)" if hidden_count else ""
        freeze_note = (
            "  (top frozen)"
            if not capture_active and self._capture_stopped_at is not None
            else ""
        )
        self.status.setText(
            f"  {len(top_devs)} stable · {len(bottom_devs)} unidentified"
            f"{hidden_note} · {total_pkts:,} pkts · "
            f"project id {self.project_id}{freeze_note}"
        )
        # Size the scene to contain everything with bottom margin.
        # Divider must span at least the viewport width so it reaches
        # the right edge regardless of how far devices flow horizontally.
        viewport_w = self.view.viewport().width()
        content_w = (
            max((d.pos_x for d in visible_devs), default=_GRID_MARGIN_X)
            + _BOX_W + 40
        )
        max_x = max(content_w, viewport_w)
        max_y = bottom_next_y + 40
        self.scene.setSceneRect(0, 0, max_x, max_y)
        # Now that the scene width is known, draw the divider so it
        # spans the full canvas width — not just the placed device
        # columns.
        self._add_section_divider(divider_y, max_x)

    def _add_section_label(self, text: str, y: float) -> None:
        """Add a small heading label to the scene at the given y.

        Z is high so the label is never occluded by a device box
        (which can extend upward into the heading band when expanded).
        """
        label = QGraphicsSimpleTextItem(text)
        font = QFont()
        font.setPointSize(_SECTION_LABEL_FONT_PT)
        font.setBold(True)
        label.setFont(font)
        label.setBrush(QBrush(_SECTION_LABEL_COLOR))
        label.setPos(_SECTION_LABEL_LEFT, y)
        label.setZValue(_Z_SECTION_DECOR)
        self.scene.addItem(label)

    def _add_section_divider(self, y: float, width: float) -> None:
        """Add a horizontal divider line of the given width at y."""
        line = QGraphicsLineItem(0, y, width, y)
        pen = QPen(_SECTION_DIVIDER_COLOR, 1)
        line.setPen(pen)
        line.setZValue(_Z_SECTION_DECOR)
        self.scene.addItem(line)

    def _add_placeholder_text(self, text: str, y: float) -> None:
        """Italic placeholder for an empty section."""
        placeholder = QGraphicsSimpleTextItem(text)
        font = QFont()
        font.setPointSize(_SECTION_LABEL_FONT_PT - 1)
        font.setItalic(True)
        placeholder.setFont(font)
        placeholder.setBrush(QBrush(_SECTION_PLACEHOLDER_COLOR))
        placeholder.setPos(_SECTION_LABEL_LEFT + 8, y - 8)
        placeholder.setZValue(_Z_SECTION_DECOR)
        self.scene.addItem(placeholder)

    def reset_layout(self) -> None:
        with self.store.tx():
            self.store.conn.execute(
                "DELETE FROM device_layouts WHERE project_id = ?",
                (self.project_id,),
            )
        # Going back to a fresh grid means the user no longer wants the
        # transient sort view either — clear both dropdowns.
        self._current_sort_primary = None
        self._current_sort_secondary = None
        self._sort_combo_primary.setCurrentIndex(0)
        self._sort_combo_secondary.setCurrentIndex(0)
        self._sort_combo_secondary.setEnabled(False)
        self.reload()

    def _on_sort_changed(self, _label: str) -> None:
        """Toolbar sort-dropdown change → re-flow scene by chosen keys.

        Reads both combos directly so we don't have to track which one
        emitted the signal. Translates the combo's first sentinel item
        ("(saved positions)" / "(none)") to None.
        """
        p = self._sort_combo_primary.currentText()
        self._current_sort_primary = p if p in _SORT_KEYS else None
        # Secondary is meaningless when there's no primary.
        self._sort_combo_secondary.setEnabled(self._current_sort_primary is not None)
        s = self._sort_combo_secondary.currentText()
        self._current_sort_secondary = (
            s if (self._current_sort_primary and s in _SORT_KEYS) else None
        )
        self.reload()

    def _on_cluster_period_changed(self, label: str) -> None:
        """Toolbar dropdown change → adjust auto-run cadence.

        The change takes effect on the next ``_live_tick`` callback.
        Switching to ``off`` halts the auto-run; the toolbar's "Run
        cluster" button still works.
        """
        self._cluster_period_ticks = _CLUSTER_PERIOD_TICKS.get(label, 60)

    def _on_stale_window_changed(self, label: str) -> None:
        """Toolbar dropdown change → re-filter canvas + cluster hydrator.

        Reload immediately so the canvas reflects the new cutoff (the
        cluster runner picks it up on its next tick).
        """
        self._stale_window_s = _STALE_WINDOW_SECONDS.get(label, 1800.0)
        self.reload()

    def _maybe_refresh_canvas(self) -> None:
        """Heartbeat reload so stale-window cutoff stays current.

        During live capture, ``_live_tick`` already reloads every 2s,
        so this skips to avoid double work. When stopped, this is the
        only thing keeping aged-out boxes from sticking around until
        the user clicks Reload.
        """
        if self._live is None:
            self.reload()

    def _open_preferences(self) -> None:
        """Open the modal Preferences dialog.

        Reads the singleton ``Preferences``, hands it to the dialog,
        and saves on accept. Knobs are picked up by their consumers
        on the next read (per-call sites read fresh values), so most
        changes take effect without restart. Fields tagged
        ``requires_restart`` (e.g. DB path) need a btviz relaunch —
        the dialog's label suffix tells the user.
        """
        from ..preferences import get_prefs
        from ..preferences.ui import PreferencesDialog
        dlg = PreferencesDialog(get_prefs(), parent=self)
        dlg.exec()

    def clear_all_data(self) -> None:
        """Wipe all observations, sessions, broadcasts, and layout for
        this project. Devices and addresses are global and stay
        (other projects may have observed them too).

        Confirmed via _ConfirmDialog because this is destructive and
        irreversible without a backup.
        """
        # Refuse while live capture is running so we don't yank the DB
        # out from under an active write loop.
        if self._live is not None and self._live.running:
            self.status.setText(
                "  cannot clear: stop live capture first"
            )
            return
        confirm = _ConfirmDialog(
            self,
            "Clear all project data?",
            f"Permanently delete all sessions, observations, broadcasts, "
            f"and canvas layout for project '{self.project.name}'?\n\n"
            f"Devices and addresses are global and will remain — they may "
            f"reappear if other projects have observed them, or as soon as "
            f"a new live capture starts.",
        )
        if confirm.exec() != QDialog.DialogCode.Accepted:
            return
        # Sessions cascade to observations + broadcasts via FK ON DELETE
        # CASCADE (see schema.sql). device_layouts cascades from project,
        # but we don't want to nuke the project itself, so delete the
        # layout rows explicitly.
        with self.store.tx():
            self.store.conn.execute(
                "DELETE FROM sessions WHERE project_id = ?",
                (self.project_id,),
            )
            self.store.conn.execute(
                "DELETE FROM device_layouts WHERE project_id = ?",
                (self.project_id,),
            )
        self.reload()
        self.status.setText("  cleared all data for this project")

    def _persist_device(self, d: CanvasDevice, *, save_pos: bool) -> None:
        """Called from DeviceItem on drag-end or expand-toggle."""
        layout = DeviceLayout(
            project_id=self.project_id,
            device_id=d.device_id,
            pos_x=d.pos_x,
            pos_y=d.pos_y,
            collapsed=d.collapsed,
            hidden=d.hidden,
        )
        self.repos.layouts.upsert_device(layout)
        # Touch the project so most-recent-used ordering stays meaningful.
        self.repos.projects.touch(self.project_id)

    # --- live capture -------------------------------------------------

    def _toggle_live(self) -> None:
        """Toolbar action handler: start live capture if stopped, stop if running."""
        if self._live is not None and self._live.running:
            self._stop_live()
        else:
            self._start_live()

    def _set_capture_button_state(self, *, capturing: bool) -> None:
        """Sync the capture button's text and colour with session state.

        Idle: green "Start Capture". Running: red "Stop Capture". The
        colour swap signals the destructive direction (clicking now
        ends the session) without needing a confirmation dialog.
        """
        self._live_action.setText(
            "Stop Capture" if capturing else "Start Capture"
        )
        if self._live_button is not None:
            self._live_button.setStyleSheet(
                _CAPTURE_BUTTON_STYLE_CAPTURING if capturing
                else _CAPTURE_BUTTON_STYLE_IDLE
            )

    def _initial_discovery_worker(self) -> None:
        """Background USB + extcap probe at canvas startup.

        Runs on a daemon thread; emits ``_initial_discovery_signal``
        with the discovered records when done. Combines the fast
        ioreg probe (catches what's physically plugged in including
        bus-only devices) with the slow extcap probe (catches
        hub-connected dongles the fast probe misses).

        DB writes happen on the **main thread** — SQLite connections
        are thread-bound and the canvas's connection lives on the
        Qt thread, so the worker just gathers records and hands
        them to the slot for persistence + panel refresh.

        **Empty-probes safety:** if BOTH probes raise (e.g.,
        missing ioreg + missing extcap binary, or a transient
        environment failure), we emit ``None`` instead of an empty
        list. The slot interprets that as "discovery couldn't run;
        leave the DB alone" — without this guard, a transient
        double-failure would call ``record_discovered([])`` which
        deactivates EVERY sniffer row, including dongles that are
        physically plugged in. A clean run that legitimately found
        zero dongles still emits ``[]`` and IS allowed to clear
        the active flags.
        """
        from ..extcap.discovery import (
            discovered_to_db_records, list_dongles_fast,
        )
        from ..extcap import find_extcap_binary, list_dongles
        from ..capture_log import get_capture_logger
        cap_log = get_capture_logger()
        fast: list = []
        slow: list = []
        fast_ok = False
        slow_ok = False
        try:
            fast = list_dongles_fast() or []
            fast_ok = True
        except Exception as e:  # noqa: BLE001
            cap_log.warning("startup discovery: fast probe failed: %s", e)
        try:
            binary = find_extcap_binary()
            slow = list_dongles(binary) or []
            slow_ok = True
        except Exception as e:  # noqa: BLE001
            cap_log.warning("startup discovery: slow probe failed: %s", e)
        if not (fast_ok or slow_ok):
            # Neither probe ran cleanly — DB update would mark all
            # dongles inactive based on no actual evidence. Skip it.
            cap_log.warning(
                "startup discovery: both probes failed; "
                "leaving sniffers.is_active flags untouched",
            )
            self._initial_discovery_signal.emit(None)
            return
        slow_keys = {(d.serial_number or d.serial_path) for d in slow}
        extra_fast = [
            d for d in fast
            if (d.serial_number or d.serial_path) not in slow_keys
        ]
        merged = slow + extra_fast
        try:
            records = discovered_to_db_records(merged)
        except Exception as e:  # noqa: BLE001
            cap_log.warning("startup discovery: record build failed: %s", e)
            records = []
        # Hand off to the main thread — the slot writes to SQLite
        # and refreshes the panel.
        self._initial_discovery_signal.emit(records)

    @Slot(object)
    def _on_initial_discovery_done(self, records: object) -> None:
        """Main-thread slot: persist discovery + refresh the panel.

        Receives the records produced by ``_initial_discovery_worker``,
        or ``None`` when both probes failed.

        ``None`` path: refresh the panel from the existing DB state
        but don't touch ``is_active`` — we don't have evidence to
        update those flags and the previous-session state is the
        best fallback. This prevents a transient double-probe
        failure from greying out plugged-in dongles.

        list path (including empty list): pass to
        ``record_discovered`` which sets ``is_active=1`` for every
        serial in the list and clears it for any active row not in
        the list. An empty list correctly says "no dongles found"
        and grays out stale entries from a prior session.
        """
        from ..capture_log import get_capture_logger
        cap_log = get_capture_logger()
        if records is None:
            # Probes failed — leave the DB alone; just repaint
            # whatever's already there.
            self.sniffer_panel.refresh()
            return
        try:
            self.repos.sniffers.record_discovered(records)
        except Exception as e:  # noqa: BLE001 — never break startup
            cap_log.warning("startup discovery: db update failed: %s", e)
        try:
            serials = {
                (r.get("serial_number") or r.get("serial_path"))
                for r in records
            }
            db_sniffers = self.repos.sniffers.list_all(
                active_only=False, include_removed=False,
            )
            unreachable = {
                s.serial_number for s in db_sniffers
                if s.serial_number and s.serial_number not in serials
            }
            self.sniffer_panel.set_extcap_unreachable(unreachable)
        except Exception:  # noqa: BLE001
            pass
        self.sniffer_panel.refresh()

    def _show_no_dongles_dialog(
        self,
        *,
        title: str = "No capture devices found",
        detail: str | None = None,
    ) -> None:
        """Surface a clear dialog when Start Capture finds no dongles.

        Replaces the previous silent-toolbar-text behaviour, which
        was easy to miss — clicking Start with no dongles plugged in
        appeared to do nothing. The dialog includes a clickable link
        to ``docs/HARDWARE.md`` for compatible-device guidance.

        ``QMessageBox.critical(None, ...)`` segfaults on macOS Tahoe
        + PySide6 6.11 (see the same workaround in ``run_canvas``);
        passing ``self`` as parent avoids the metaobject path that
        triggers the crash. We also catch construction errors and
        fall back to stderr so a dialog failure never blocks Start.
        """
        body = (
            "btviz didn't find any nRF Sniffer dongles attached to this "
            "machine.<br><br>"
            "Plug in one or more compatible USB dongles flashed with the "
            "Nordic <i>nRF Sniffer for Bluetooth LE</i> firmware, then "
            "click Start Capture again.<br><br>"
            'See <a href="https://github.com/dkrugman/btviz/blob/main/'
            'docs/HARDWARE.md">docs/HARDWARE.md</a> for the list of '
            "compatible devices, firmware requirements, and "
            "troubleshooting tips."
        )
        if detail:
            body += f"<br><br><small>{detail}</small>"
        try:
            from PySide6.QtCore import Qt as _Qt
            from PySide6.QtWidgets import QMessageBox as _QMB
            box = _QMB(self)
            box.setWindowTitle("btviz")
            box.setIcon(_QMB.Icon.Warning)
            box.setText(title)
            box.setTextFormat(_Qt.TextFormat.RichText)
            box.setTextInteractionFlags(
                _Qt.TextInteractionFlag.TextBrowserInteraction
            )
            box.setInformativeText(body)
            box.setStandardButtons(_QMB.StandardButton.Ok)
            box.exec()
        except Exception as e:  # noqa: BLE001 — dialog must never block Start
            import sys as _sys
            print(
                f"btviz: {title} — see "
                f"https://github.com/dkrugman/btviz/blob/main/docs/HARDWARE.md "
                f"(dialog suppressed: {e!r})",
                file=_sys.stderr,
            )

    def _start_live(self) -> None:
        """Begin a live capture session for this project.

        Wires up: EventBus → CaptureCoordinator (which spawns SnifferProcess
        per dongle) → bus.publish(TOPIC_PACKET) → LiveIngest (decodes and
        queues) → QTimer flush + periodic reload.
        """
        if self._live is not None and self._live.running:
            return
        # Clear the freeze: top section resumes real-time fade now that
        # capture is live again.
        self._capture_stopped_at = None
        self._bus = EventBus()
        self._coord = CaptureCoordinator(self._bus)

        # Discovery: list_dongles() runs the slow extcap probe — acceptable
        # at capture-start (the user pressed Start; they expect to wait).
        from ..capture_log import get_capture_logger
        cap_log = get_capture_logger()
        try:
            self._coord.refresh_dongles()
        except Exception as e:  # noqa: BLE001
            self.status.setText(f"  live: discovery failed: {e}")
            cap_log.error("capture aborted — discovery failed: %s", e)
            self._show_no_dongles_dialog(
                title="Discovery failed",
                detail=f"The extcap probe raised: {e}",
            )
            self._bus = None
            self._coord = None
            return
        if not self._coord.dongles:
            self.status.setText("  live: no capture devices found")
            cap_log.warning("capture aborted — no dongles discovered")
            self._show_no_dongles_dialog()
            # Reset DB sniffer rows to inactive — the panel was
            # showing stale "active" badges from a prior session
            # but the extcap probe just confirmed nothing is
            # currently connected. Without this, a user who closed
            # btviz with dongles plugged → travelled → reopened
            # btviz without them sees green dots and is misled.
            try:
                self.repos.sniffers.record_discovered([])
                self.sniffer_panel.refresh()
            except Exception:  # noqa: BLE001
                pass
            self._bus = None
            self._coord = None
            return
        # Verbose: per-dongle discovery rows. Pinned at VERBOSE so a
        # default-tier log stays compact even with many dongles.
        cap_log.verbose(
            "discovered %d dongle%s",
            len(self._coord.dongles),
            "" if len(self._coord.dongles) == 1 else "s",
        )
        for d in self._coord.dongles:
            cap_log.verbose(
                "  dongle short_id=%s serial=%s port=%s display=%s",
                getattr(d, "short_id", "?"),
                getattr(d, "serial_number", None) or getattr(d, "serial_path", "?"),
                getattr(d, "serial_path", "?"),
                getattr(d, "display", ""),
            )

        # Persist the discovered dongles into the sniffers table so the
        # panel renders them as active. Each detection path has blind
        # spots: fast (ioreg) misses hub-connected dongles on some
        # systems; slow (extcap) intermittently misses the DK when its
        # serial port is held by a stale handle. Pass the union to
        # record_discovered so a row is only deactivated when *neither*
        # path sees it. A device that ioreg sees but extcap missed still
        # won't capture this session (no SnifferProcess), but the panel
        # accurately reflects "plugged in".
        try:
            from ..extcap.discovery import (
                discovered_to_db_records, list_dongles_fast,
            )
            slow_dongles = list(self._coord.dongles)
            slow_keys = {(d.serial_number or d.serial_path) for d in slow_dongles}
            extra_fast = [
                d for d in list_dongles_fast()
                if (d.serial_number or d.serial_path) not in slow_keys
            ]
            records = discovered_to_db_records(slow_dongles + extra_fast)
            self.repos.sniffers.record_discovered(records)
            self.sniffer_panel.refresh()
        except Exception:  # noqa: BLE001
            pass

        # Build short_id → DB-serial map so the per-source notifier
        # can drive the panel's serial-keyed activity dot.
        # Must match discovered_to_db_records: serial_number or serial_path.
        # Dongles without a USB serial (common on nRF52840 dongle firmware)
        # fall back to serial_path, which is what the sniffers table stores.
        # Using short_id here causes a key mismatch against the panel lookup.
        self._source_to_serial = {
            d.short_id: (d.serial_number or d.serial_path)
            for d in self._coord.dongles
        }

        # Compare the slow extcap probe's discovered set against the DB
        # rows the panel already shows (those came from the fast/USB
        # probe). Anything in the DB that the extcap probe missed gets
        # flagged "USB-detected but not extcap-reachable" — typically a
        # Nordic firmware in a hung state that a replug clears. Tooltip
        # in the panel explains the recovery.
        extcap_serials = {
            (d.serial_number or d.serial_path)
            for d in self._coord.dongles
        }
        try:
            db_sniffers = self.repos.sniffers.list_all(
                active_only=False, include_removed=False,
            )
            unreachable = {
                s.serial_number for s in db_sniffers
                if s.serial_number and s.serial_number not in extcap_serials
            }
        except Exception:  # noqa: BLE001 - never let a UX hint break live start
            unreachable = set()
        self.sniffer_panel.set_extcap_unreachable(unreachable)

        # Surface any per-sniffer state changes (notably last_error from
        # capture-loop failures) on the toolbar status. Without this
        # subscription, a SnifferProcess that fails its FIFO open
        # exits silently and the user sees "capturing on N" with fewer
        # blinking dots than they expect.
        self._sniffer_state_unsub = self._bus.subscribe(
            TOPIC_SNIFFER_STATE, self._on_sniffer_state,
        )

        self._live = LiveIngest(
            self._bus, self.repos, self.project_id,
            session_name=f"live-{int(time.time())}",
            keep_packets=self._keep_packets_action.isChecked(),
        )
        # Lock the checkbox while a session is running so the user
        # can't toggle it mid-stream — IngestContext.keep_packets is
        # captured at LiveIngest.start() time and not consulted again,
        # so a mid-session change wouldn't take effect anyway.
        self._keep_packets_action.setEnabled(False)
        self._live.set_packet_callback(self._on_live_packet)
        self._live.set_device_packet_callback(self._on_device_packet)
        self._live.start()

        # Spawn sniffers and apply default roles. Subprocess startup can
        # take a beat — the action becomes "Stop" so a second click stops.
        self._coord.start_discover()

        # Push role-derived channel sets to the panel so each row's
        # channel-tag column reflects what the sniffer is actually
        # listening to. Idle sniffers (no role yet) get a stub data
        # channel via find_unmonitored_stream() so the display has
        # something visible during testing.
        self._publish_sniffer_channels()

        self._live_timer = QTimer(self)
        self._live_timer.timeout.connect(self._live_tick)
        # 250ms flush cadence: low enough latency that the activity dot
        # feels responsive, high enough that DB writes batch usefully
        # under heavy adv traffic.
        self._live_timer.start(250)

        # Stall watchdog. Detects a wedged USB-CDC endpoint within
        # ~60s of silence and restarts the wedged subprocess. Bumps
        # ``sniffers.stall_count`` in the DB so chronic per-dongle
        # stalls show up as a "STALL ×N" badge in the panel — both
        # within a session and across btviz restarts. Tunables are
        # all read from preferences so the user can widen the
        # threshold for RF-quiet environments without code edits.
        from ..capture.watchdog import StallWatchdog
        from ..preferences import get_prefs
        prefs = get_prefs()
        self._stall_watchdog = StallWatchdog(
            sniffers=lambda: list(self._coord.sniffers.values())
                              if self._coord is not None else [],
            repos=self.repos,
            restart=self._coord.restart_one,
            threshold_s=prefs.get("watchdog.stall_threshold_s"),
            max_attempts=prefs.get("watchdog.max_attempts"),
            min_gap_s=prefs.get("watchdog.min_gap_s"),
        )

        # Count what actually started — with N dongles and 3 primary
        # advertising channels, default_roles pins 3 and parks the rest
        # as ScanUnmonitored, which only spin up if a primary frees
        # (e.g. when one is re-tasked to Follow). Reserved sniffers are
        # idle subprocesses that haven't been started yet.
        running = sum(
            1 for sp in self._coord.sniffers.values() if sp.state.running
        )
        total = len(self._coord.dongles)
        reserved = total - running
        msg = f"  live: capturing on {running} of {total} devices"
        if reserved > 0:
            msg += f" ({reserved} reserved for follow)"
        msg += "…"
        self._set_capture_button_state(capturing=True)
        # Start the panel's session timer. Reads the live system
        # clock so it matches whatever epoch the session row in the
        # DB was stamped with (within one tick).
        self.sniffer_panel.start_session_timer(time.time())
        self.status.setText(msg)

        # INFO-level lifecycle line — always lands in capture.log
        # regardless of verbose/debug prefs. Format chosen so a
        # ``grep "capture started" ~/.btviz/capture.log`` works as a
        # session boundary marker.
        roles_count: dict[str, int] = {}
        for sp in self._coord.sniffers.values():
            r = getattr(sp.state, "role", "idle") or "idle"
            roles_count[r] = roles_count.get(r, 0) + 1
        roles_str = " ".join(
            f"{n} {r}" for r, n in sorted(roles_count.items())
        ) or "no roles assigned"
        cap_log.info(
            "capture started — %d/%d dongles capturing (%s)",
            running, total, roles_str,
        )
        # Verbose: per-sniffer role assignment so a long log with
        # multiple captures has the role-vs-stall correlation.
        for short_id, sp in self._coord.sniffers.items():
            cap_log.verbose(
                "  role short_id=%s role=%s running=%s",
                short_id,
                getattr(sp.state, "role", "?"),
                getattr(sp.state, "running", False),
            )
        cap_log.verbose(
            "watchdog started — threshold=%.0fs max_attempts=%d min_gap=%.0fs",
            float(prefs.get("watchdog.stall_threshold_s")),
            int(prefs.get("watchdog.max_attempts")),
            float(prefs.get("watchdog.min_gap_s")),
        )

        # Stash session-start metadata for the matching "capture
        # stopped" summary line. Lifetime: cleared in _stop_live.
        self._capture_started_at_log = time.time()
        self._capture_started_running = running
        self._capture_started_total = total

    def _stop_live(self) -> None:
        # Capture summary stats BEFORE we drop self._live, since
        # the lifecycle log line needs them.
        from ..capture_log import get_capture_logger
        cap_log = get_capture_logger()
        live_stats = self._live.stats if self._live is not None else None
        started_at_log = getattr(self, "_capture_started_at_log", None)

        if self._live_timer is not None:
            self._live_timer.stop()
            self._live_timer = None
        if self._sniffer_state_unsub is not None:
            try:
                self._sniffer_state_unsub()
            except Exception:  # noqa: BLE001
                pass
            self._sniffer_state_unsub = None
        if self._coord is not None:
            # Save before teardown so "Refresh Sniffers" can merge them in
            # after the coordinator is gone (list_dongles_fast misses hub dongles).
            self._last_coord_dongles = list(self._coord.dongles)
            try:
                self._coord.stop_all()
            except Exception:  # noqa: BLE001
                pass
        if self._live is not None:
            self._live.stop()
        self._set_capture_button_state(capturing=False)
        # Freeze the panel's session timer at the just-elapsed value
        # so the user sees how long their session ran. Resets on
        # the next Start Capture.
        self.sniffer_panel.stop_session_timer()
        # Snapshot the moment-of-stop so the top "Devices" section
        # freezes its opacity-fade at this point. Bottom section keeps
        # ageing on real time so RPAs continue to fade out as before.
        self._capture_stopped_at = time.time()
        # Re-enable the keep-packets toggle now that the session is
        # ended — the user can flip it before the next Start.
        self._keep_packets_action.setEnabled(True)
        self._live = None
        self._coord = None
        self._bus = None
        self._source_to_serial = {}
        # Drop the watchdog so per-session attempt state doesn't leak
        # into the next capture run. Lifetime ``stall_count`` lives in
        # the DB and survives.
        self._stall_watchdog = None
        # Drop any per-sniffer errors and clear the toolbar warning
        # styling — the next session starts with a clean slate.
        self._sniffer_errors.clear()
        self._apply_status_severity("ok")
        # Clear the panel's "extcap-unreachable" hint — the next live
        # start will recompute it from a fresh discovery sweep.
        self.sniffer_panel.set_extcap_unreachable(set())

        # INFO-level lifecycle line — counterpart to "capture started".
        # Format chosen so a single ``grep "capture " capture.log`` shows
        # the lifecycle envelope of every session.
        if started_at_log is not None:
            duration_s = self._capture_stopped_at - started_at_log
            h, rem = divmod(int(duration_s), 3600)
            m, s = divmod(rem, 60)
            duration_str = (
                f"{h}h{m:02d}m{s:02d}s" if h else f"{m}m{s:02d}s"
            )
            pkts = live_stats.packets_recorded if live_stats is not None else 0
            dropped = live_stats.packets_dropped if live_stats is not None else 0
            cap_log.info(
                "capture stopped — duration=%s, packets=%d, dropped=%d",
                duration_str, pkts, dropped,
            )
        self._capture_started_at_log = None
        # Leave sniffer rows as-is: stopping capture doesn't unplug them.
        # Dots stay green to reflect "detected, idle". A row only goes grey
        # when a fresh discovery sweep (Refresh sniffers / next Start Capture)
        # fails to find it.
        # One last reload so the user sees the final state of the session.
        self.reload()

    def _on_sniffer_state(self, state) -> None:
        """Bus subscriber for ``TOPIC_SNIFFER_STATE``.

        Stashes the most-recent ``last_error`` per sniffer so the
        next ``_live_tick`` status refresh can render it as part of
        the live-stats prefix. Previously this method called
        ``self.status.setText`` directly, but the next tick's stats
        update (every ~2 s) would clobber it before the user could
        read it; routing through ``self._sniffer_errors`` lets the
        message persist until either a fresh non-empty error arrives
        or capture stops.

        Runs on the bus reader thread; assignment to a dict slot is
        atomic in CPython, and the read on the Qt thread is harmless
        if it races with a write (worst case: one tick sees the old
        value).
        """
        err = getattr(state, "last_error", None)
        if not err:
            return
        sid = getattr(getattr(state, "dongle", None), "short_id", "?")
        self._sniffer_errors[sid] = err

    def _compose_stall_status(self) -> tuple[str, str]:
        """Return ``(prefix, severity)`` for the toolbar status label.

        Severity ordering — most urgent wins so the user sees the
        worst-state token first:

          * ``"stuck"``  — watchdog has given up (replug required)
          * ``"silent"`` — at least one sniffer is silent past the
                           watchdog threshold but hasn't exhausted
                           restart attempts yet
          * ``"error"``  — non-empty ``last_error`` from
                           ``_on_sniffer_state`` exists but no stall
                           is currently flagged (rare; mainly catches
                           spawn-time failures the watchdog hasn't
                           timed out on yet)
          * ``"ok"``     — clear; default styling

        The prefix is short and ends with a separator so it composes
        cleanly with the live-stats string.
        """
        wd = self._stall_watchdog
        stuck = frozenset()
        silent = frozenset()
        if wd is not None:
            try:
                stuck = wd.stuck_short_ids()
                silent = wd.currently_silent_short_ids()
            except Exception:  # noqa: BLE001 — never break live status
                pass

        if stuck:
            ids = ", ".join(sorted(stuck))
            return f"  ⚠ STALL [{ids}] — replug required  ·", "stuck"
        if silent:
            ids = ", ".join(sorted(silent))
            return f"  ⚠ silent [{ids}] — restarting  ·", "silent"
        if self._sniffer_errors:
            sid, msg = next(iter(self._sniffer_errors.items()))
            # Truncate long Python tracebacks down to one usable line.
            short = msg.splitlines()[0][:80]
            return f"  ⚠ sniffer [{sid}]: {short}  ·", "error"
        return "", "ok"

    def _apply_status_severity(self, severity: str) -> None:
        """Style the toolbar status label according to severity.

        Stylesheet is empty in the ``ok`` case so we revert cleanly
        to the toolbar's default rendering. The padding + radius on
        the warning styles let the colored block read as a badge
        rather than a stripe across the whole toolbar.
        """
        if severity == "stuck":
            self.status.setStyleSheet(
                "color: #721c24; background-color: #f8d7da; "
                "padding: 2px 6px; border-radius: 3px; font-weight: bold;"
            )
        elif severity == "silent":
            self.status.setStyleSheet(
                "color: #856404; background-color: #fff3cd; "
                "padding: 2px 6px; border-radius: 3px;"
            )
        elif severity == "error":
            self.status.setStyleSheet(
                "color: #721c24; background-color: #f8d7da; "
                "padding: 2px 6px; border-radius: 3px;"
            )
        else:
            self.status.setStyleSheet("")

    def _live_tick(self) -> None:
        """QTimer callback (main thread). Drains the queue; reloads every Nth."""
        if self._live is None:
            return
        self._live.flush()
        self._reload_tick += 1
        # Stall watchdog tick at ~10 s cadence (40 ticks * 250 ms).
        # Cheap operation — walks the sniffers dict and compares
        # timestamps. Lifetime DB bumps + log writes only fire on
        # actual stall detection.
        if self._reload_tick % 40 == 0 and self._stall_watchdog is not None:
            try:
                self._stall_watchdog.tick()
                # Push stuck-set into the panel so red "replug" badge
                # surfaces when the watchdog has given up on a
                # sniffer. Resolve short_id → serial via the
                # coordinator's dongles list (panel keys by serial).
                if self._coord is not None:
                    short_to_serial = {
                        d.short_id: (d.serial_number or d.serial_path)
                        for d in self._coord.dongles
                    }
                    stuck_serials = {
                        short_to_serial[sid]
                        for sid in self._stall_watchdog.stuck_short_ids()
                        if sid in short_to_serial
                    }
                    self.sniffer_panel.set_stuck_serials(stuck_serials)
                # Debug-tier: per-tick eligibility + silence snapshot.
                # Loud (every 10 s) but only when capture.debug_log
                # is on, so this is opt-in fire-hose narration.
                from ..capture_log import get_capture_logger
                cap_log = get_capture_logger()
                if cap_log.isEnabledFor(10):  # logging.DEBUG
                    silent = self._stall_watchdog.currently_silent_short_ids()
                    stuck = self._stall_watchdog.stuck_short_ids()
                    cap_log.debug(
                        "watchdog tick — silent=%d stuck=%d ids_silent=%s ids_stuck=%s",
                        len(silent), len(stuck),
                        ",".join(sorted(silent)) or "-",
                        ",".join(sorted(stuck)) or "-",
                    )
            except Exception as e:  # noqa: BLE001 — never break live capture
                self.status.setText(f"  watchdog error: {e}")
        # Verbose-tier: 5-minute in-flight summary so a long capture
        # has periodic context interleaved with STALL events. 1200
        # ticks * 250 ms = 5 min. Skipped when verbose is off so the
        # default-tier file stays compact.
        if self._reload_tick % 1200 == 0 and self._live is not None:
            from ..capture_log import get_capture_logger as _get_cap_log
            _cap_log = _get_cap_log()
            if _cap_log.isEnabledFor(15):  # capture_log.VERBOSE
                stats = self._live.stats
                running = (
                    sum(
                        1 for sp in self._coord.sniffers.values()
                        if sp.state.running
                    )
                    if self._coord is not None else 0
                )
                stuck_n = (
                    len(self._stall_watchdog.stuck_short_ids())
                    if self._stall_watchdog is not None else 0
                )
                _cap_log.verbose(
                    "summary @ tick=%d — running=%d packets=%d dropped=%d stuck=%d",
                    self._reload_tick, running,
                    stats.packets_recorded, stats.packets_dropped,
                    stuck_n,
                )
        # Reload the scene every ~2s (8 ticks * 250ms). Full rebuild is
        # heavy (re-runs the project-aggregate query and rebuilds every
        # DeviceItem) — incremental updates can replace this later.
        if self._reload_tick % 8 == 0:
            self.reload()
            stats = self._live.stats
            base = (
                f"  live: rx={stats.packets_received:,} "
                f"dec={stats.packets_decoded:,} "
                f"rec={stats.packets_recorded:,} "
                f"drop={stats.packets_dropped} "
                f"dev={stats.devices_touched} "
                f"ext={stats.ext_adv_seen}"
                f"({stats.ext_adv_with_baa} baa) "
                f"bcast={stats.broadcasts_seen}"
            )
            # Per-source diagnostic: short_id → received/rejected. Lets
            # the user spot at a glance which sniffer is producing
            # decoded packets vs which is silent or all-rejecting. E.g.
            # ``[ 213101:5/5 213201:8200/12 0010502893191:11000/0 ]``
            # means dongle 213101 produced 5 bus packets, all rejected
            # at decode (something wrong with that one); 213201 is
            # producing 8200 bytes-worth with only 12 rejects (healthy);
            # the DK is doing the bulk of the work.
            health = self._live.source_health()
            if health:
                pieces = []
                for src, (recv, rej) in sorted(health.items()):
                    short = _shorten_source_label(src)
                    pieces.append(f"{short}:{recv:,}/{rej}")
                base += "    [ " + " ".join(pieces) + " ]"
            prefix, severity = self._compose_stall_status()
            self.status.setText(prefix + base)
            self._apply_status_severity(severity)
        # Cluster pass at a slower cadence than the scene reload — it's
        # O(n²) over recent devices and we don't want to compete with
        # ingest. Cadence is the toolbar dropdown's selection (default
        # 60 ticks = 15 s); 0 means "off, manual-only".
        period = self._cluster_period_ticks
        if period > 0 and self._reload_tick % period == 0:
            self._run_cluster_tick()
        # Re-randomize idle sniffers' stub data channels every 2 s so
        # the panel's channel-tag column visibly cycles during testing.
        # Pinned / ScanUnmonitored rows are deterministic from their
        # role and stay put; only the Idle test stub rotates. Drop
        # this when the real "tune to expected stream" assignment
        # logic replaces find_unmonitored_stream().
        if self._reload_tick % 8 == 0:
            self._publish_sniffer_channels()

    def _run_cluster_tick(self) -> None:
        """Dispatch a cluster pass to the worker thread.

        The heavy work (signal evaluation across O(N²) pairs) runs in
        ``_ClusterWorker.run`` on a ``QThread``; the UI returns to its
        event loop immediately. Persistence + status update happen on
        the main thread in ``_on_cluster_finished`` once the worker
        emits ``finished``.

        If a previous run is still in flight we drop the new tick.
        Cluster cadence is one pass every 15 s by default — by the
        time the next tick fires, the in-flight run has either
        finished (cleared ``_cluster_busy``) or hasn't, in which case
        skipping is correct because we'd be re-clustering the same
        device set.
        """
        if self._cluster_busy:
            return
        try:
            from ..cluster import (
                ClusterContext, load_devices, load_profiles, load_signals,
            )
            if self._cluster_ctx is None:
                self._cluster_ctx = ClusterContext(
                    signals=load_signals(),
                    profiles=load_profiles(),
                    now=time.time(),
                    db=self.store,
                )
            else:
                # Refresh ``now`` so age-sensitive signals (rotation_cohort,
                # rssi_signature recent-window) see the current clock.
                self._cluster_ctx.now = time.time()

            devices = load_devices(
                self.store, recent_window_s=self._stale_window_s,
            )
            if not devices:
                return
        except Exception as e:  # noqa: BLE001
            self.status.setText(f"  cluster error: {e}")
            return

        # Dispatch the request to the persistent worker. The signal's
        # queued connection guarantees ``run_request`` runs on the
        # worker thread, never on the main thread. ``_cluster_busy``
        # is cleared by the matched ``finished`` / ``failed`` slot.
        # ``max_per_class`` reads from prefs at dispatch time so a
        # live preference change takes effect on the next tick.
        from ..preferences import get_prefs
        max_per_class = int(get_prefs().get("cluster.max_per_class"))
        self._cluster_busy = True
        self._dispatch_cluster.emit(
            self._cluster_ctx, self.store.path, devices, max_per_class,
        )

    @Slot(object)
    def _on_cluster_finished(self, result) -> None:
        """Worker delivered a RunResult. Persist + show status.

        Runs on the main thread (the worker emits across threads, Qt
        queues the slot invocation here). SQLite writes via
        ``self.repos.clusters.apply_run`` are bound to the main
        thread's connection — that's the side that owns the writable
        connection.
        """
        self._cluster_busy = False
        try:
            written = self.repos.clusters.apply_run(
                result.merge_decisions, time.time(),
            )
        except Exception as e:  # noqa: BLE001
            self.status.setText(f"  cluster error: {e}")
            return
        self._cluster_tick += 1
        parts: list[str] = []
        if result.merge_decisions:
            parts.append(
                f"clusters: {result.devices_in} → {result.cluster_count} "
                f"({written} groups, {result.elapsed_s:.2f}s)"
            )
        if result.skipped_classes:
            skipped = ", ".join(
                f"{cls} ({n})" for cls, n in result.skipped_classes
            )
            parts.append(f"skipped: {skipped}")
        if parts:
            self.status.setText("  " + " · ".join(parts))

    @Slot(str)
    def _on_cluster_failed(self, msg: str) -> None:
        self._cluster_busy = False
        self.status.setText(f"  cluster error: {msg}")

    def _on_live_packet(
        self, source: str, channel: int | None, crc_ok: bool = True,
    ) -> None:
        """LiveIngest per-source notifier. Drives the panel's activity
        dot, channel-tag highlight, the channel-spectrum strip's bars,
        and CRC-failed dropout flashes on both.
        """
        # Channel-strip aggregates across all sniffers, so feed it on
        # every packet whether or not the source maps to a known
        # serial. This also covers extcap source ids that haven't been
        # joined to the sniffers table yet (they still produce decoded
        # packets that belong on the spectrum view).
        self.channel_strip.notify_packet(channel, crc_ok=crc_ok)
        serial = self._source_to_serial.get(source)
        if serial is None:
            return
        self.sniffer_panel.notify_packet(
            serial, channel=channel, crc_ok=crc_ok,
        )

    def _on_device_packet(
        self,
        device_id: int,
        channel: int | None,
        crc_ok: bool = True,
        rssi: int | None = None,
    ) -> None:
        """LiveIngest per-device notifier. Drives the per-DeviceItem
        channel-flash badge and rolling-window Signal/Quality meters
        so the canvas shows current link health per-device in real time.

        ``crc_ok=False`` is fired by LiveIngest for CRC-failed packets
        that it credited to this device via the last-clean-device
        cache, so the canvas can render a dropout flash matching the
        sniffer panel and count the dropout in the rolling window.
        ``rssi`` (dBm) feeds the Signal meter's rolling average.

        Looks up the DeviceItem by ``device_id`` in the scene. The
        scene gets fully rebuilt by ``reload()`` every ~2 s during
        live capture, so an item we found a moment ago may have been
        replaced — do the lookup fresh on every hit. The cost is one
        scene-items() iteration; the alternative is maintaining a
        per-canvas ``id -> DeviceItem`` index synchronized with each
        reload, which is more bookkeeping for marginal gain at the
        device counts we care about (low thousands).
        """
        for item in self.scene.items():
            if isinstance(item, DeviceItem) and item.device.device_id == device_id:
                item.notify_channel_hit(channel, crc_ok=crc_ok, rssi=rssi)
                if not self._channel_flash_timer.isActive():
                    self._channel_flash_timer.start()
                return

    def _tick_channel_flash(self) -> None:
        """20 Hz repaint pump — calls update() on every DeviceItem
        with an active flash OR an in-window Signal/Quality sample,
        so badge alpha fades smoothly and the rolling-window meters
        drain visibly when packets stop arriving. Stops the timer
        when no items have anything left to fade so the canvas is
        idle at rest.
        """
        any_active = False
        for item in self.scene.items():
            if isinstance(item, DeviceItem) and (
                item._data_flash_recent or item._adv_flash or item._recent
            ):
                any_active = True
                item.update()
        if not any_active:
            self._channel_flash_timer.stop()

    def _publish_sniffer_channels(self) -> None:
        """Push each sniffer's listening-channel set to the panel.

        Reads the coordinator's role assignments and translates them
        into a per-serial tuple of channel ints:
          - Pinned(channels)  -> the channel tuple as-is
          - ScanUnmonitored   -> the channels NOT covered by other
                                 pinned sniffers (recomputed each call)
          - Follow            -> currently empty (data-channel hopping
                                 sequence isn't tracked in btviz yet)
          - Idle              -> a single random data channel from
                                 find_unmonitored_stream() — testing
                                 stub for the panel display until the
                                 advertising-data-driven assignment
                                 logic lands

        Maps short_id -> serial via the same key shape as record_discovered
        so the panel rows match. Called once at start_discover; future
        role changes (Follow assigned, etc.) should call this again.
        """
        if self._coord is None:
            return
        from ..capture.roles import (
            Idle, Pinned, ScanUnmonitored, Follow,
            PRIMARY_ADV_CHANNELS, find_unmonitored_stream,
        )

        # Channels already pinned by some other sniffer — used to
        # compute ScanUnmonitored's set and to spread idle stubs across
        # data channels rather than colliding.
        pinned_adv: set[int] = set()
        for role in self._coord.roles.values():
            if isinstance(role, Pinned):
                pinned_adv.update(role.channels)

        idle_data_used: set[int] = set()
        for d in self._coord.dongles:
            role = self._coord.roles.get(d.short_id, Idle())
            if isinstance(role, Pinned):
                channels = list(role.channels)
            elif isinstance(role, ScanUnmonitored):
                channels = [
                    c for c in PRIMARY_ADV_CHANNELS if c not in pinned_adv
                ]
            elif isinstance(role, Follow):
                channels = []
            else:  # Idle / unknown — testing stub
                ch = find_unmonitored_stream(idle_data_used)
                idle_data_used.add(ch)
                channels = [ch]
            serial = self._source_to_serial.get(d.short_id)
            if serial is not None:
                self.sniffer_panel.set_sniffer_channels(serial, channels)

    # --- device context menu / follow --------------------------------

    def _device_context_menu(self, device: CanvasDevice) -> QMenu:
        """Build the right-click menu for one DeviceItem.

        Returns a fully-assembled ``QMenu`` so the canvas can include
        submenus (rather than just a flat list of actions). Entries:

          * ``Rename device…`` — sets a per-device ``user_name`` that
            wins over every automatic naming source (local_name,
            gatt_device_name, broadcast_name, vendor+model fallback).
          * ``Follow this device`` — re-tasks one sniffer to track the
            device's most-recent address. Useful for capturing
            post-CONNECT_IND data-channel traffic and (with an IRK
            loaded) resolving its rotating RPAs back to a stable
            identity. Disabled when prerequisites aren't met.
          * ``Copy ▶`` submenu — Address / Name / Stable key /
            All info (the multi-line tooltip we already build).
        """
        menu = QMenu()

        rename_action = menu.addAction("Rename device…")
        rename_action.setToolTip(
            "Set a custom name for this device. Wins over every "
            "automatic naming source (local name, GATT name, vendor)."
        )
        rename_action.triggered.connect(
            lambda checked=False, d=device: self._rename_device(d)
        )

        class_action = menu.addAction("Set device class…")
        class_action.setToolTip(
            "Pin a device_class for this device. Wins over the "
            "auto-detected class for icon, label, and cluster "
            "profile. Pick from the canonical list."
        )
        class_action.triggered.connect(
            lambda checked=False, d=device: self._set_device_class(d)
        )

        follow_action = menu.addAction("Follow this device")
        if self._coord is None or self._live is None or not self._live.running:
            follow_action.setEnabled(False)
            follow_action.setToolTip(
                "Start live capture first (toolbar → Start Capture)."
            )
        elif not device.addresses:
            follow_action.setEnabled(False)
            follow_action.setToolTip(
                "This device has no recorded addresses to follow."
            )
        else:
            addr, addr_type = device.addresses[0]
            is_random = addr_type != "public"
            follow_action.setToolTip(
                f"Re-task one sniffer to follow {addr} "
                f"({addr_type or 'unknown'})."
            )
            follow_action.triggered.connect(
                lambda checked=False, a=addr, r=is_random:
                    self._follow_device(a, r)
            )

        menu.addSeparator()
        copy_menu = menu.addMenu("Copy")

        # Most-recent address (addresses[0]; load_canvas_devices sorts
        # them by last_seen DESC). Disabled when the device has none.
        addr_str = device.addresses[0][0] if device.addresses else ""
        addr_action = copy_menu.addAction("Address")
        if addr_str:
            addr_action.triggered.connect(
                lambda checked=False, s=addr_str: self._copy_to_clipboard(s)
            )
        else:
            addr_action.setEnabled(False)

        name_str = _pick_display_label(device)
        name_action = copy_menu.addAction("Name")
        name_action.triggered.connect(
            lambda checked=False, s=name_str: self._copy_to_clipboard(s)
        )

        key_action = copy_menu.addAction("Stable key")
        key_action.triggered.connect(
            lambda checked=False, s=device.stable_key: self._copy_to_clipboard(s)
        )

        all_action = copy_menu.addAction("All info (tooltip)")
        all_action.triggered.connect(
            lambda checked=False, d=device: self._copy_to_clipboard(_build_tooltip(d))
        )

        return menu

    def _copy_to_clipboard(self, text: str) -> None:
        """Put ``text`` on the system clipboard and surface what was
        copied via the toolbar status line.

        Long values (the All-info tooltip) get truncated in the
        confirmation message so the status bar doesn't blow out width.
        """
        if not text:
            return
        QApplication.clipboard().setText(text)
        first_line = text.splitlines()[0] if "\n" in text else text
        snippet = first_line if len(first_line) <= 60 else first_line[:57] + "…"
        self.status.setText(f"  copied: {snippet}")

    def _rename_device(self, device: CanvasDevice) -> None:
        """Prompt for a new ``user_name`` and persist via the devices repo.

        Empty input clears the override. Reload after the change so the
        new label propagates to the box title and tooltip.
        """
        current = device.user_name or ""
        new_name, ok = QInputDialog.getText(
            self,
            "Rename device",
            f"Name for {_pick_display_label(device)}:",
            text=current,
        )
        if not ok:
            return
        new_name = new_name.strip()
        # Empty string clears the override; pass None to set_user_name.
        self.repos.devices.set_user_name(
            device.device_id, new_name if new_name else None,
        )
        self.reload()

    def _set_device_class(self, device: CanvasDevice) -> None:
        """Open the canonical-class picker and persist the choice.

        Selecting the reset entry clears the override (sets the
        column to NULL) so the auto-detected ``device_class`` flows
        through unchanged on the next reload.
        """
        dlg = DeviceClassDialog(
            self,
            current=device.user_device_class,
            auto=device.auto_device_class,
        )
        if dlg.exec() != QDialog.Accepted:
            return
        choice = dlg.chosen_value()
        if choice is None:
            return
        new_class: str | None
        if choice == _CLASS_RESET_SENTINEL:
            new_class = None
        else:
            new_class = choice
        self.repos.devices.set_user_device_class(
            device.device_id, new_class,
        )
        self.reload()

    def _follow_device(self, address: str, is_random: bool) -> None:
        """Ask the coordinator to dedicate a sniffer to this address.

        Called from the device context-menu handler. The coordinator
        prefers an idle sniffer, falls back to a scan-unmonitored one,
        and re-tasks any other dongle if neither is available.
        """
        if self._coord is None:
            return
        chosen = self._coord.follow(
            FollowRequest(target_addr=address, is_random=is_random)
        )
        if chosen is None:
            self.status.setText(
                f"  follow: no sniffer available to retask for {address}"
            )
        else:
            self.status.setText(
                f"  follow: sniffer {chosen} now tracking {address}"
            )

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        # Cleanup order matters: live capture first (so the
        # "capture stopped" line lands BEFORE the exit line), then
        # cluster worker, then exit log. ``_stop_live`` is a no-op
        # if no session is active.
        from ..capture_log import get_capture_logger, get_program_started_at
        cap_log = get_capture_logger()
        was_capturing = self._live is not None and self._live.running
        if was_capturing:
            cap_log.verbose("window closing — stopping live capture for shutdown")
            self._stop_live()
        # Stop the persistent cluster worker thread cleanly: quit its
        # event loop and wait for the underlying pthread to exit
        # before Python releases anything. After ``wait`` returns, no
        # one else can touch the worker QObject, so Shiboken's
        # finalizer can destroy the C++ wrapper safely.
        if self._cluster_thread is not None:
            self._cluster_thread.quit()
            self._cluster_thread.wait(2000)
        # INFO-level lifecycle counterpart to "btviz startup". Includes
        # process uptime so a chronic-stall report has the duration
        # of the run that produced it without the user having to
        # subtract timestamps manually. Uptime starts at the first
        # ``configure_capture_log()`` call (effectively program-start).
        started_at = get_program_started_at()
        if started_at is not None:
            import time as _time
            uptime_s = _time.time() - started_at
            h, rem = divmod(int(uptime_s), 3600)
            m, s = divmod(rem, 60)
            uptime_str = (
                f"{h}h{m:02d}m{s:02d}s" if h else f"{m}m{s:02d}s"
            )
            cap_log.info(
                "btviz exit — uptime=%s%s",
                uptime_str,
                " (capture was active)" if was_capturing else "",
            )
        else:
            cap_log.info("btviz exit")
        super().closeEvent(event)

# ──────────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────────

def run_canvas(db_path: Path | None = None, project_name: str | None = None) -> int:
    """Launch the canvas window. If project_name is None, show the picker."""
    app = QApplication.instance() or QApplication([])
    store = open_store(db_path)

    # Single-instance enforcement: only one canvas may run per DB.
    # Two instances on the same DB collide on USB-CDC ports
    # (sniffer subprocess fights over /dev/cu.usbmodem*), corrupt
    # capture.log rotation (RotatingFileHandler isn't multi-process
    # safe), and double-run cluster passes. The lock auto-releases
    # on process death, so a crash leaves no stale state.
    from ..single_instance import acquire_db_lock, conflict_message
    lock_result = acquire_db_lock(store.path)
    if not lock_result.acquired:
        # Existing instance — show a dialog (with parent=None
        # because we don't have a window yet; QMessageBox at this
        # point in startup is before any QMainWindow exists, so
        # the macOS-Tahoe segfault path that haunts in-canvas
        # dialogs doesn't apply — but we still wrap in try/except
        # for a stderr fallback).
        msg = conflict_message(lock_result)
        try:
            from PySide6.QtWidgets import QMessageBox as _QMB
            box = _QMB()
            box.setWindowTitle("btviz")
            box.setIcon(_QMB.Icon.Critical)
            box.setText("btviz is already running")
            box.setInformativeText(msg)
            box.setStandardButtons(_QMB.StandardButton.Ok)
            box.exec()
        except Exception:  # noqa: BLE001
            import sys as _sys
            print(f"btviz: {msg}", file=_sys.stderr)
        try:
            store.close()
        except Exception:  # noqa: BLE001
            pass
        return 2

    repos = Repos(store)
    project_id: int | None = None
    if project_name:
        proj = repos.projects.get_by_name(project_name)
        if proj is None:
            # NOTE: previously this used QMessageBox.critical(None, ...) but
            # it segfaults on macOS Tahoe + PySide6 6.11 in the Qt
            # metaobject builder (PyUnicode_InternFromString → bad ptr deref).
            # Bug isn't ours; until Qt/PySide ship a fix, surface the error
            # via stderr — it's better CLI UX anyway.
            import sys
            print(
                f"error: project {project_name!r} not found. "
                f"Ingest something first, or omit --project to use the "
                f"picker dialog.",
                file=sys.stderr,
            )
            return 2
        project_id = proj.id
    else:
        # Create a default project if the DB is empty so the picker isn't blank.
        if not repos.projects.list():
            repos.projects.create("default")
        picker = ProjectPicker(store)
        if picker.exec() != QDialog.Accepted:
            return 0
        project_id = picker.selected_project_id()
        if project_id is None:
            return 0

    win = CanvasWindow(store, project_id)
    win.show()
    rc = app.exec()
    # Cleanup after the Qt event loop returns. ``CanvasWindow.closeEvent``
    # already tore down live capture + cluster worker + emitted the
    # "btviz exit" log line; here we close the SQLite store explicitly
    # (rather than relying on GC) and flush logging handlers so any
    # tail-end log entries land on disk before interpreter shutdown.
    try:
        store.close()
    except Exception:  # noqa: BLE001 — best-effort; process is exiting
        pass
    try:
        import logging as _logging
        _logging.shutdown()
    except Exception:  # noqa: BLE001
        pass
    # Explicit lock release — closing the file handle releases
    # the flock. The kernel would do this on process exit anyway,
    # but explicit close keeps the cleanup chain symmetric with
    # store + logging shutdown above and means a long-lived
    # parent process (e.g., a test runner reusing run_canvas)
    # frees the lock immediately rather than at GC time.
    if lock_result.file_handle is not None:
        try:
            lock_result.file_handle.close()
        except Exception:  # noqa: BLE001
            pass
    return rc
