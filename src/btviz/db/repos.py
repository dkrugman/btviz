"""CRUD methods over the btviz schema.

Grouped into small classes per entity. Each takes the Store and delegates
to its underlying sqlite connection. Caller is responsible for wrapping
multi-step flows in `store.tx()` when atomicity is needed.

Design notes:
  - A Device row exists only when we have a stable identity (public MAC,
    random_static MAC, or an IRK-resolved identity). NRPAs and unresolved
    RPAs live in `addresses` with device_id NULL.
  - stable_key canonicalizes identity across sessions/projects:
      "pub:aa:bb:cc:dd:ee:ff"   public MAC
      "rs:aa:bb:cc:dd:ee:ff"    random static MAC
      "irk:<32-hex>"            IRK-resolved identity
"""
from __future__ import annotations

import json
import time
from typing import Iterable

from .models import (
    Address,
    Broadcast,
    BroadcastReceiver,
    CanvasState,
    Connection,
    Device,
    DeviceLayout,
    DeviceProjectMeta,
    Group,
    IRK,
    LTK,
    Observation,
    Project,
    Session,
)
from .store import Store


# --- small helpers ---------------------------------------------------------

def _now() -> float:
    return time.time()


def _row_to_device(r) -> Device:
    return Device(
        id=r["id"],
        stable_key=r["stable_key"],
        kind=r["kind"],
        user_name=r["user_name"],
        local_name=r["local_name"],
        gatt_device_name=r["gatt_device_name"],
        vendor=r["vendor"],
        vendor_id=r["vendor_id"],
        oui_vendor=r["oui_vendor"],
        model=r["model"],
        device_class=r["device_class"],
        appearance=r["appearance"],
        identifiers=json.loads(r["identifiers_json"]),
        notes=r["notes"],
        first_seen=r["first_seen"],
        last_seen=r["last_seen"],
        created_at=r["created_at"],
    )


def _row_to_address(r) -> Address:
    return Address(
        id=r["id"],
        address=r["address"],
        address_type=r["address_type"],
        device_id=r["device_id"],
        resolved_via_irk_id=r["resolved_via_irk_id"],
        first_seen=r["first_seen"],
        last_seen=r["last_seen"],
    )


def _row_to_project(r) -> Project:
    return Project(
        id=r["id"],
        name=r["name"],
        description=r["description"],
        created_at=r["created_at"],
        updated_at=r["updated_at"],
    )


def _row_to_group(r) -> Group:
    return Group(
        id=r["id"],
        project_id=r["project_id"],
        parent_group_id=r["parent_group_id"],
        name=r["name"],
        color=r["color"],
        collapsed=bool(r["collapsed"]),
        pos_x=r["pos_x"],
        pos_y=r["pos_y"],
        width=r["width"],
        height=r["height"],
        z_order=r["z_order"],
    )


def _row_to_irk(r) -> IRK:
    return IRK(
        id=r["id"],
        project_id=r["project_id"],
        key_hex=r["key_hex"],
        label=r["label"],
        device_id=r["device_id"],
        notes=r["notes"],
        created_at=r["created_at"],
    )


def _row_to_ltk(r) -> LTK:
    return LTK(
        id=r["id"],
        key_hex=r["key_hex"],
        ediv=r["ediv"],
        rand_hex=r["rand_hex"],
        label=r["label"],
        device_a_id=r["device_a_id"],
        device_b_id=r["device_b_id"],
        notes=r["notes"],
        created_at=r["created_at"],
    )


def _row_to_session(r) -> Session:
    return Session(
        id=r["id"],
        project_id=r["project_id"],
        source_type=r["source_type"],
        started_at=r["started_at"],
        name=r["name"],
        source_path=r["source_path"],
        ended_at=r["ended_at"],
        notes=r["notes"],
    )


# --- repositories ----------------------------------------------------------

class Projects:
    def __init__(self, store: Store) -> None:
        self.s = store

    def create(self, name: str, description: str | None = None) -> Project:
        cur = self.s.conn.execute(
            "INSERT INTO projects(name, description) VALUES(?, ?)",
            (name, description),
        )
        return self.get(cur.lastrowid)

    def get(self, project_id: int) -> Project:
        row = self.s.conn.execute(
            "SELECT * FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"project {project_id} not found")
        return _row_to_project(row)

    def get_by_name(self, name: str) -> Project | None:
        row = self.s.conn.execute(
            "SELECT * FROM projects WHERE name = ?", (name,)
        ).fetchone()
        return _row_to_project(row) if row else None

    def list(self) -> list[Project]:
        rows = self.s.conn.execute(
            "SELECT * FROM projects ORDER BY name"
        ).fetchall()
        return [_row_to_project(r) for r in rows]

    def rename(self, project_id: int, name: str) -> None:
        self.s.conn.execute(
            "UPDATE projects SET name = ?, updated_at = ? WHERE id = ?",
            (name, _now(), project_id),
        )

    def touch(self, project_id: int) -> None:
        self.s.conn.execute(
            "UPDATE projects SET updated_at = ? WHERE id = ?",
            (_now(), project_id),
        )

    def delete(self, project_id: int) -> None:
        self.s.conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))


class Devices:
    def __init__(self, store: Store) -> None:
        self.s = store

    @staticmethod
    def stable_key_for(address: str, address_type: str) -> str | None:
        """Canonical stable_key for a MAC+type, or None if not stable."""
        if address_type == "public":
            return f"pub:{address}"
        if address_type == "random_static":
            return f"rs:{address}"
        return None  # rpa / nrpa are not stable on their own

    @staticmethod
    def stable_key_for_irk(key_hex: str) -> str:
        return f"irk:{key_hex.lower()}"

    def upsert(self, stable_key: str, kind: str, now: float | None = None) -> Device:
        """Insert-or-touch by stable_key. Returns the full row."""
        ts = now if now is not None else _now()
        self.s.conn.execute(
            """
            INSERT INTO devices(stable_key, kind, first_seen, last_seen)
            VALUES(?, ?, ?, ?)
            ON CONFLICT(stable_key) DO UPDATE SET last_seen = excluded.last_seen
            """,
            (stable_key, kind, ts, ts),
        )
        return self.get_by_stable_key(stable_key)

    def get(self, device_id: int) -> Device:
        row = self.s.conn.execute(
            "SELECT * FROM devices WHERE id = ?", (device_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"device {device_id} not found")
        return _row_to_device(row)

    def get_by_stable_key(self, stable_key: str) -> Device:
        row = self.s.conn.execute(
            "SELECT * FROM devices WHERE stable_key = ?", (stable_key,)
        ).fetchone()
        if row is None:
            raise KeyError(f"device {stable_key!r} not found")
        return _row_to_device(row)

    # Columns callers may set directly via merge_identity().
    _IDENTITY_COLS = (
        "local_name", "gatt_device_name", "vendor", "vendor_id",
        "oui_vendor", "model", "device_class", "appearance",
    )

    def merge_identity(
        self,
        device_id: int,
        *,
        local_name: str | None = None,
        gatt_device_name: str | None = None,
        vendor: str | None = None,
        vendor_id: int | None = None,
        oui_vendor: str | None = None,
        model: str | None = None,
        device_class: str | None = None,
        appearance: int | None = None,
        identifiers: dict[str, str] | None = None,
    ) -> None:
        """Fill in identity clues learned from the wire.

        Only non-None args are applied; user_name is intentionally NOT updatable
        here (that requires set_user_name). `identifiers` is merged into the
        existing JSON map (new keys override).
        """
        cols, vals = [], []
        for col, val in [
            ("local_name", local_name),
            ("gatt_device_name", gatt_device_name),
            ("vendor", vendor),
            ("vendor_id", vendor_id),
            ("oui_vendor", oui_vendor),
            ("model", model),
            ("device_class", device_class),
            ("appearance", appearance),
        ]:
            if val is not None:
                cols.append(f"{col} = ?")
                vals.append(val)

        if identifiers:
            row = self.s.conn.execute(
                "SELECT identifiers_json FROM devices WHERE id = ?", (device_id,)
            ).fetchone()
            current = json.loads(row["identifiers_json"]) if row else {}
            current.update(identifiers)
            cols.append("identifiers_json = ?")
            vals.append(json.dumps(current))

        if not cols:
            return
        vals.append(device_id)
        self.s.conn.execute(
            f"UPDATE devices SET {', '.join(cols)} WHERE id = ?", vals
        )

    def set_user_name(self, device_id: int, user_name: str | None) -> None:
        """User-assigned override label. Stored separately from wire-inferred clues."""
        self.s.conn.execute(
            "UPDATE devices SET user_name = ? WHERE id = ?", (user_name, device_id)
        )

    def set_notes(self, device_id: int, notes: str | None) -> None:
        self.s.conn.execute(
            "UPDATE devices SET notes = ? WHERE id = ?", (notes, device_id)
        )


class Addresses:
    def __init__(self, store: Store) -> None:
        self.s = store

    def upsert(
        self,
        address: str,
        address_type: str,
        device_id: int | None,
        now: float | None = None,
    ) -> Address:
        ts = now if now is not None else _now()
        self.s.conn.execute(
            """
            INSERT INTO addresses(address, address_type, device_id, first_seen, last_seen)
            VALUES(?, ?, ?, ?, ?)
            ON CONFLICT(address, address_type) DO UPDATE SET
                last_seen = excluded.last_seen,
                device_id = COALESCE(addresses.device_id, excluded.device_id)
            """,
            (address, address_type, device_id, ts, ts),
        )
        row = self.s.conn.execute(
            "SELECT * FROM addresses WHERE address = ? AND address_type = ?",
            (address, address_type),
        ).fetchone()
        return _row_to_address(row)

    def link_to_device(self, address_id: int, device_id: int, via_irk_id: int | None = None) -> None:
        self.s.conn.execute(
            """
            UPDATE addresses
               SET device_id = ?, resolved_via_irk_id = ?
             WHERE id = ?
            """,
            (device_id, via_irk_id, address_id),
        )

    def unresolved_rpas(self) -> list[Address]:
        rows = self.s.conn.execute(
            "SELECT * FROM addresses WHERE address_type = 'rpa' AND device_id IS NULL"
        ).fetchall()
        return [_row_to_address(r) for r in rows]

    def for_device(self, device_id: int) -> list[Address]:
        rows = self.s.conn.execute(
            "SELECT * FROM addresses WHERE device_id = ? ORDER BY last_seen DESC",
            (device_id,),
        ).fetchall()
        return [_row_to_address(r) for r in rows]


class Sessions:
    def __init__(self, store: Store) -> None:
        self.s = store

    def start(
        self,
        project_id: int,
        source_type: str,
        source_path: str | None = None,
        name: str | None = None,
    ) -> Session:
        cur = self.s.conn.execute(
            """
            INSERT INTO sessions(project_id, source_type, source_path, name, started_at)
            VALUES(?, ?, ?, ?, ?)
            """,
            (project_id, source_type, source_path, name, _now()),
        )
        return self.get(cur.lastrowid)

    def end(self, session_id: int, notes: str | None = None) -> None:
        self.s.conn.execute(
            "UPDATE sessions SET ended_at = ?, notes = COALESCE(?, notes) WHERE id = ?",
            (_now(), notes, session_id),
        )

    def get(self, session_id: int) -> Session:
        row = self.s.conn.execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"session {session_id} not found")
        return _row_to_session(row)

    def list_for_project(self, project_id: int) -> list[Session]:
        rows = self.s.conn.execute(
            "SELECT * FROM sessions WHERE project_id = ? ORDER BY started_at DESC",
            (project_id,),
        ).fetchall()
        return [_row_to_session(r) for r in rows]


class Observations:
    def __init__(self, store: Store) -> None:
        self.s = store

    def record_packet(
        self,
        session_id: int,
        device_id: int,
        *,
        ts: float,
        is_adv: bool,
        rssi: int | None,
        channel: int | None,
        phy: str | None,
        pdu_type: str | None,
    ) -> None:
        """Apply one packet's contribution to the session/device aggregate.

        This reads the existing row, updates counters + histograms, and writes
        back in a single statement. Called in the packet hot path; keep it tight.
        """
        row = self.s.conn.execute(
            "SELECT * FROM observations WHERE session_id = ? AND device_id = ?",
            (session_id, device_id),
        ).fetchone()

        if row is None:
            pdu_types = {pdu_type: 1} if pdu_type else {}
            channels = {str(channel): 1} if channel is not None else {}
            phys = {phy: 1} if phy else {}
            self.s.conn.execute(
                """
                INSERT INTO observations(
                    session_id, device_id,
                    packet_count, adv_count, data_count,
                    rssi_min, rssi_max, rssi_sum, rssi_samples,
                    first_seen, last_seen,
                    pdu_types_json, channels_json, phy_json
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id, device_id,
                    1, 1 if is_adv else 0, 0 if is_adv else 1,
                    rssi, rssi, rssi or 0, 1 if rssi is not None else 0,
                    ts, ts,
                    json.dumps(pdu_types), json.dumps(channels), json.dumps(phys),
                ),
            )
            return

        pdu_types = json.loads(row["pdu_types_json"])
        channels = json.loads(row["channels_json"])
        phys = json.loads(row["phy_json"])
        if pdu_type:
            pdu_types[pdu_type] = pdu_types.get(pdu_type, 0) + 1
        if channel is not None:
            ck = str(channel)
            channels[ck] = channels.get(ck, 0) + 1
        if phy:
            phys[phy] = phys.get(phy, 0) + 1

        new_rssi_min = row["rssi_min"]
        new_rssi_max = row["rssi_max"]
        new_rssi_sum = row["rssi_sum"]
        new_rssi_samples = row["rssi_samples"]
        if rssi is not None:
            new_rssi_min = rssi if new_rssi_min is None else min(new_rssi_min, rssi)
            new_rssi_max = rssi if new_rssi_max is None else max(new_rssi_max, rssi)
            new_rssi_sum += rssi
            new_rssi_samples += 1

        self.s.conn.execute(
            """
            UPDATE observations SET
                packet_count = packet_count + 1,
                adv_count = adv_count + ?,
                data_count = data_count + ?,
                rssi_min = ?, rssi_max = ?, rssi_sum = ?, rssi_samples = ?,
                last_seen = ?,
                pdu_types_json = ?, channels_json = ?, phy_json = ?
             WHERE session_id = ? AND device_id = ?
            """,
            (
                1 if is_adv else 0, 0 if is_adv else 1,
                new_rssi_min, new_rssi_max, new_rssi_sum, new_rssi_samples,
                ts,
                json.dumps(pdu_types), json.dumps(channels), json.dumps(phys),
                session_id, device_id,
            ),
        )

    def get(self, session_id: int, device_id: int) -> Observation | None:
        row = self.s.conn.execute(
            "SELECT * FROM observations WHERE session_id = ? AND device_id = ?",
            (session_id, device_id),
        ).fetchone()
        if row is None:
            return None
        return Observation(
            session_id=row["session_id"],
            device_id=row["device_id"],
            packet_count=row["packet_count"],
            adv_count=row["adv_count"],
            data_count=row["data_count"],
            rssi_min=row["rssi_min"],
            rssi_max=row["rssi_max"],
            rssi_sum=row["rssi_sum"],
            rssi_samples=row["rssi_samples"],
            first_seen=row["first_seen"],
            last_seen=row["last_seen"],
            pdu_types=json.loads(row["pdu_types_json"]),
            channels={int(k): v for k, v in json.loads(row["channels_json"]).items()},
            phys=json.loads(row["phy_json"]),
        )


class Groups:
    def __init__(self, store: Store) -> None:
        self.s = store

    def create(
        self,
        project_id: int,
        name: str,
        *,
        parent_group_id: int | None = None,
        color: str | None = None,
        pos_x: float = 0.0,
        pos_y: float = 0.0,
    ) -> Group:
        cur = self.s.conn.execute(
            """
            INSERT INTO groups(project_id, parent_group_id, name, color, pos_x, pos_y)
            VALUES(?, ?, ?, ?, ?, ?)
            """,
            (project_id, parent_group_id, name, color, pos_x, pos_y),
        )
        return self.get(cur.lastrowid)

    def get(self, group_id: int) -> Group:
        row = self.s.conn.execute(
            "SELECT * FROM groups WHERE id = ?", (group_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"group {group_id} not found")
        return _row_to_group(row)

    def list_for_project(self, project_id: int) -> list[Group]:
        rows = self.s.conn.execute(
            "SELECT * FROM groups WHERE project_id = ? ORDER BY z_order, id",
            (project_id,),
        ).fetchall()
        return [_row_to_group(r) for r in rows]

    def update(
        self,
        group_id: int,
        *,
        name: str | None = None,
        color: str | None = None,
        collapsed: bool | None = None,
        pos_x: float | None = None,
        pos_y: float | None = None,
        width: float | None = None,
        height: float | None = None,
        parent_group_id: int | None = None,
        z_order: int | None = None,
    ) -> None:
        fields, vals = [], []
        for col, val in [
            ("name", name), ("color", color),
            ("collapsed", int(collapsed) if collapsed is not None else None),
            ("pos_x", pos_x), ("pos_y", pos_y),
            ("width", width), ("height", height),
            ("parent_group_id", parent_group_id),
            ("z_order", z_order),
        ]:
            if val is not None:
                fields.append(f"{col} = ?")
                vals.append(val)
        if not fields:
            return
        vals.append(group_id)
        self.s.conn.execute(
            f"UPDATE groups SET {', '.join(fields)} WHERE id = ?", vals
        )

    def delete(self, group_id: int) -> None:
        self.s.conn.execute("DELETE FROM groups WHERE id = ?", (group_id,))

    def add_device(self, group_id: int, device_id: int) -> None:
        self.s.conn.execute(
            "INSERT OR IGNORE INTO group_devices(group_id, device_id) VALUES(?, ?)",
            (group_id, device_id),
        )

    def remove_device(self, group_id: int, device_id: int) -> None:
        self.s.conn.execute(
            "DELETE FROM group_devices WHERE group_id = ? AND device_id = ?",
            (group_id, device_id),
        )

    def devices_in(self, group_id: int) -> list[int]:
        rows = self.s.conn.execute(
            "SELECT device_id FROM group_devices WHERE group_id = ?",
            (group_id,),
        ).fetchall()
        return [r["device_id"] for r in rows]


class Layouts:
    """Per-project canvas layout for devices + canvas viewport state."""

    def __init__(self, store: Store) -> None:
        self.s = store

    def upsert_device(self, layout: DeviceLayout) -> None:
        self.s.conn.execute(
            """
            INSERT INTO device_layouts(project_id, device_id, pos_x, pos_y, collapsed, hidden, z_order)
            VALUES(?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(project_id, device_id) DO UPDATE SET
                pos_x = excluded.pos_x,
                pos_y = excluded.pos_y,
                collapsed = excluded.collapsed,
                hidden = excluded.hidden,
                z_order = excluded.z_order
            """,
            (
                layout.project_id, layout.device_id,
                layout.pos_x, layout.pos_y,
                int(layout.collapsed), int(layout.hidden),
                layout.z_order,
            ),
        )

    def get_device(self, project_id: int, device_id: int) -> DeviceLayout | None:
        row = self.s.conn.execute(
            "SELECT * FROM device_layouts WHERE project_id = ? AND device_id = ?",
            (project_id, device_id),
        ).fetchone()
        if row is None:
            return None
        return DeviceLayout(
            project_id=row["project_id"],
            device_id=row["device_id"],
            pos_x=row["pos_x"],
            pos_y=row["pos_y"],
            collapsed=bool(row["collapsed"]),
            hidden=bool(row["hidden"]),
            z_order=row["z_order"],
        )

    def all_for_project(self, project_id: int) -> list[DeviceLayout]:
        rows = self.s.conn.execute(
            "SELECT * FROM device_layouts WHERE project_id = ?",
            (project_id,),
        ).fetchall()
        return [
            DeviceLayout(
                project_id=r["project_id"],
                device_id=r["device_id"],
                pos_x=r["pos_x"],
                pos_y=r["pos_y"],
                collapsed=bool(r["collapsed"]),
                hidden=bool(r["hidden"]),
                z_order=r["z_order"],
            )
            for r in rows
        ]

    def get_canvas(self, project_id: int) -> CanvasState:
        row = self.s.conn.execute(
            "SELECT * FROM canvas_state WHERE project_id = ?", (project_id,)
        ).fetchone()
        if row is None:
            return CanvasState(project_id=project_id)
        return CanvasState(
            project_id=row["project_id"],
            zoom=row["zoom"],
            pan_x=row["pan_x"],
            pan_y=row["pan_y"],
            last_opened_at=row["last_opened_at"],
        )

    def set_canvas(self, state: CanvasState) -> None:
        self.s.conn.execute(
            """
            INSERT INTO canvas_state(project_id, zoom, pan_x, pan_y, last_opened_at)
            VALUES(?, ?, ?, ?, ?)
            ON CONFLICT(project_id) DO UPDATE SET
                zoom = excluded.zoom,
                pan_x = excluded.pan_x,
                pan_y = excluded.pan_y,
                last_opened_at = excluded.last_opened_at
            """,
            (state.project_id, state.zoom, state.pan_x, state.pan_y, state.last_opened_at),
        )


class Keys:
    """IRKs and LTKs, scoped per-project."""

    def __init__(self, store: Store) -> None:
        self.s = store

    def add_irk(
        self,
        project_id: int,
        key_hex: str,
        *,
        label: str | None = None,
        device_id: int | None = None,
        notes: str | None = None,
    ) -> IRK:
        cur = self.s.conn.execute(
            """
            INSERT INTO irks(project_id, key_hex, label, device_id, notes)
            VALUES(?, ?, ?, ?, ?)
            """,
            (project_id, key_hex.lower(), label, device_id, notes),
        )
        row = self.s.conn.execute(
            "SELECT * FROM irks WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
        return _row_to_irk(row)

    def list_irks(self, project_id: int) -> list[IRK]:
        rows = self.s.conn.execute(
            "SELECT * FROM irks WHERE project_id = ? ORDER BY created_at",
            (project_id,),
        ).fetchall()
        return [_row_to_irk(r) for r in rows]

    def remove_irk(self, irk_id: int) -> None:
        self.s.conn.execute("DELETE FROM irks WHERE id = ?", (irk_id,))

    def set_irk_device(self, irk_id: int, device_id: int | None) -> None:
        self.s.conn.execute(
            "UPDATE irks SET device_id = ? WHERE id = ?",
            (device_id, irk_id),
        )

    def add_ltk(
        self,
        key_hex: str,
        *,
        ediv: int | None = None,
        rand_hex: str | None = None,
        label: str | None = None,
        device_a_id: int | None = None,
        device_b_id: int | None = None,
        notes: str | None = None,
    ) -> LTK:
        cur = self.s.conn.execute(
            """
            INSERT INTO ltks(key_hex, ediv, rand_hex, label,
                             device_a_id, device_b_id, notes)
            VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (key_hex.lower(), ediv, rand_hex, label,
             device_a_id, device_b_id, notes),
        )
        row = self.s.conn.execute(
            "SELECT * FROM ltks WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
        return _row_to_ltk(row)

    def list_ltks(self) -> list[LTK]:
        rows = self.s.conn.execute(
            "SELECT * FROM ltks ORDER BY created_at"
        ).fetchall()
        return [_row_to_ltk(r) for r in rows]

    def list_ltks_for_device(self, device_id: int) -> list[LTK]:
        rows = self.s.conn.execute(
            "SELECT * FROM ltks WHERE device_a_id = ? OR device_b_id = ? ORDER BY created_at",
            (device_id, device_id),
        ).fetchall()
        return [_row_to_ltk(r) for r in rows]

    def remove_ltk(self, ltk_id: int) -> None:
        self.s.conn.execute("DELETE FROM ltks WHERE id = ?", (ltk_id,))


class Broadcasts:
    """Auracast broadcast records, keyed per-session by Broadcast_ID.

    Each (session_id, broadcast_id) pair corresponds to one observed
    Auracast stream. A broadcaster that streams across multiple sessions
    yields one row per session — that's deliberate, since QoS / channel
    activity per session is what we care about.

    Schema lives in db/schema.sql under `broadcasts`. There's no UNIQUE
    constraint on (session_id, broadcast_id) at the SQL level, so we
    do the existence check in Python before inserting. For typical
    captures (a handful of broadcasters), this is fine.
    """

    def __init__(self, store: Store) -> None:
        self.s = store

    def upsert(
        self,
        session_id: int,
        broadcast_id: int,
        *,
        broadcaster_device_id: int | None = None,
        broadcast_name: str | None = None,
        bis_count: int | None = None,
        phy: str | None = None,
        encrypted: bool = False,
        ts: float | None = None,
    ) -> int:
        """Insert or refresh a broadcast row. Returns the row id.

        Only non-None fields are applied on update — so a stronger signal
        from a later packet (e.g. BIGInfo finally arrived with bis_count)
        replaces a weaker one, but a lone BAA-only packet won't clobber a
        fully-populated row.
        """
        ts = ts if ts is not None else _now()
        row = self.s.conn.execute(
            """
            SELECT id, broadcaster_device_id, broadcast_name,
                   bis_count, phy, encrypted
              FROM broadcasts
             WHERE session_id = ? AND broadcast_id = ?
            """,
            (session_id, broadcast_id),
        ).fetchone()

        if row is None:
            cur = self.s.conn.execute(
                """
                INSERT INTO broadcasts(
                    session_id, broadcaster_device_id, broadcast_id,
                    broadcast_name, bis_count, phy, encrypted,
                    first_seen, last_seen
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (session_id, broadcaster_device_id, broadcast_id,
                 broadcast_name, bis_count, phy, int(encrypted),
                 ts, ts),
            )
            return cur.lastrowid

        # Update — only override existing fields when we have a value, so
        # we accumulate evidence rather than thrashing.
        new_dev = broadcaster_device_id if broadcaster_device_id is not None else row["broadcaster_device_id"]
        new_name = broadcast_name if broadcast_name is not None else row["broadcast_name"]
        new_bis = bis_count if bis_count is not None else row["bis_count"]
        new_phy = phy if phy is not None else row["phy"]
        # encrypted is True-leaning: once True, stays True.
        new_enc = int(encrypted or bool(row["encrypted"]))
        self.s.conn.execute(
            """
            UPDATE broadcasts SET
                broadcaster_device_id = ?,
                broadcast_name = ?,
                bis_count = ?,
                phy = ?,
                encrypted = ?,
                last_seen = ?
             WHERE id = ?
            """,
            (new_dev, new_name, new_bis, new_phy, new_enc, ts, row["id"]),
        )
        return row["id"]

    def list_for_session(self, session_id: int) -> list[Broadcast]:
        rows = self.s.conn.execute(
            "SELECT * FROM broadcasts WHERE session_id = ? ORDER BY first_seen",
            (session_id,),
        ).fetchall()
        return [
            Broadcast(
                id=r["id"],
                session_id=r["session_id"],
                broadcaster_device_id=r["broadcaster_device_id"],
                broadcast_id=r["broadcast_id"],
                broadcast_name=r["broadcast_name"],
                big_handle=r["big_handle"],
                bis_count=r["bis_count"],
                phy=r["phy"],
                encrypted=bool(r["encrypted"]),
                first_seen=r["first_seen"],
                last_seen=r["last_seen"],
            )
            for r in rows
        ]


class Meta:
    """App-level key/value state (e.g. last active project)."""

    LAST_PROJECT = "last_project_id"

    def __init__(self, store: Store) -> None:
        self.s = store

    def get(self, key: str) -> str | None:
        row = self.s.conn.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else None

    def set(self, key: str, value: str | None) -> None:
        if value is None:
            self.s.conn.execute("DELETE FROM meta WHERE key = ?", (key,))
            return
        self.s.conn.execute(
            """
            INSERT INTO meta(key, value) VALUES(?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )


# --- umbrella convenience --------------------------------------------------

class Repos:
    """Groups all repo classes so callers can pass a single object around."""

    def __init__(self, store: Store) -> None:
        self.store = store
        self.projects = Projects(store)
        self.devices = Devices(store)
        self.addresses = Addresses(store)
        self.sessions = Sessions(store)
        self.observations = Observations(store)
        self.groups = Groups(store)
        self.layouts = Layouts(store)
        self.keys = Keys(store)
        self.broadcasts = Broadcasts(store)
        self.meta = Meta(store)
