-- btviz schema v3
-- Global: devices + addresses. Per-project: sessions, groups, layouts, keys.
-- v3 adds: device_ad_history, packets, device_clusters, device_cluster_members.

-- --------------------------------------------------------------------------
-- Global device identity
-- --------------------------------------------------------------------------

-- Devices accumulate identity clues over time. A computed "best label"
-- picks from these in confidence order: user_name > gatt_device_name >
-- local_name > "<vendor> <model>" > "<vendor> <device_class>" > vendor >
-- fallback to stable_key.
CREATE TABLE devices (
    id                INTEGER PRIMARY KEY,
    stable_key        TEXT NOT NULL UNIQUE,  -- "pub:<mac>" | "rs:<mac>" | "irk:<hex>"
    kind              TEXT NOT NULL,         -- public_mac | random_static_mac | irk_identity

    -- User override (wins over everything automatic)
    user_name         TEXT,

    -- Names observed on the wire
    local_name        TEXT,                  -- adv Complete/Shortened Local Name
    gatt_device_name  TEXT,                  -- GATT Device Name characteristic (if read)

    -- Vendor / class / model
    vendor            TEXT,                  -- e.g., "Apple, Inc."
    vendor_id         INTEGER,               -- Bluetooth SIG company id (e.g., 0x004C)
    oui_vendor        TEXT,                  -- vendor inferred from public MAC OUI
    model             TEXT,                  -- e.g., "iPhone 16 Pro Max"
    device_class      TEXT,                  -- phone | hearing_aid | airtag | auracast_source | ...

    -- BLE appearance value (uint16, GAP Appearance characteristic / AD type 0x19)
    appearance        INTEGER,

    -- Open-ended identity evidence: serial_number, firmware_rev, hardware_rev,
    -- manufacturer name string, apple_continuity_type, etc. Free-form so new
    -- clue types don't require schema changes.
    identifiers_json  TEXT NOT NULL DEFAULT '{}',

    notes             TEXT,
    first_seen        REAL NOT NULL,
    last_seen         REAL NOT NULL,
    created_at        REAL NOT NULL DEFAULT (strftime('%s','now'))
);

-- Observed BLE addresses. Many-to-one with devices.
-- RPAs without a matching IRK have device_id NULL until resolved.
CREATE TABLE addresses (
    id                   INTEGER PRIMARY KEY,
    address              TEXT NOT NULL,         -- aa:bb:cc:dd:ee:ff lowercase
    address_type         TEXT NOT NULL,         -- public | random_static | rpa | nrpa
    device_id            INTEGER REFERENCES devices(id) ON DELETE SET NULL,
    resolved_via_irk_id  INTEGER,               -- FK set after IRK added (nullable)
    first_seen           REAL NOT NULL,
    last_seen            REAL NOT NULL,
    UNIQUE(address, address_type)
);
CREATE INDEX idx_addresses_device ON addresses(device_id);

-- --------------------------------------------------------------------------
-- Projects + sessions
-- --------------------------------------------------------------------------

CREATE TABLE projects (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    description TEXT,
    created_at  REAL NOT NULL DEFAULT (strftime('%s','now')),
    updated_at  REAL NOT NULL DEFAULT (strftime('%s','now'))
);

CREATE TABLE sessions (
    id          INTEGER PRIMARY KEY,
    project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name        TEXT,
    source_type TEXT NOT NULL,  -- live | file
    source_path TEXT,           -- pcap path if file
    started_at  REAL NOT NULL,
    ended_at    REAL,
    notes       TEXT
);
CREATE INDEX idx_sessions_project ON sessions(project_id);

-- Per-device aggregates within a session. Updated in place as packets arrive.
CREATE TABLE observations (
    session_id        INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    device_id         INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    packet_count      INTEGER NOT NULL DEFAULT 0,
    adv_count         INTEGER NOT NULL DEFAULT 0,
    data_count        INTEGER NOT NULL DEFAULT 0,
    -- CRC-failed packets attributed to this device by the live-ingest
    -- last-clean-device cache. NEVER incremented by ``record_packet``
    -- (those packets have unreliable address bits and are skipped).
    -- Kept here so the canvas quality bar can show cumulative
    -- good/bad ratios across capture sessions, including before
    -- capture starts and after it stops.
    bad_packet_count  INTEGER NOT NULL DEFAULT 0,
    rssi_min          INTEGER,
    rssi_max          INTEGER,
    rssi_sum          INTEGER NOT NULL DEFAULT 0,   -- avg = rssi_sum / rssi_samples
    rssi_samples      INTEGER NOT NULL DEFAULT 0,
    first_seen        REAL NOT NULL,
    last_seen         REAL NOT NULL,
    pdu_types_json    TEXT NOT NULL DEFAULT '{}',   -- {"ADV_IND": 42, ...}
    channels_json     TEXT NOT NULL DEFAULT '{}',   -- {"37": 100, "38": 80, ...}
    phy_json          TEXT NOT NULL DEFAULT '{}',   -- {"1M": 90, "2M": 10, ...}
    PRIMARY KEY (session_id, device_id)
);

-- --------------------------------------------------------------------------
-- Canvas: groups, layouts, per-project metadata
-- --------------------------------------------------------------------------

-- Recursive via parent_group_id. A group can nest other groups.
CREATE TABLE groups (
    id              INTEGER PRIMARY KEY,
    project_id      INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    parent_group_id INTEGER REFERENCES groups(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    color           TEXT,
    collapsed       INTEGER NOT NULL DEFAULT 0,
    pos_x           REAL NOT NULL DEFAULT 0,
    pos_y           REAL NOT NULL DEFAULT 0,
    width           REAL,
    height          REAL,
    z_order         INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_groups_project ON groups(project_id);
CREATE INDEX idx_groups_parent  ON groups(parent_group_id);

CREATE TABLE group_devices (
    group_id  INTEGER NOT NULL REFERENCES groups(id)  ON DELETE CASCADE,
    device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    PRIMARY KEY (group_id, device_id)
);

-- Canvas position for each device, per project.
CREATE TABLE device_layouts (
    project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    device_id   INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    pos_x       REAL NOT NULL DEFAULT 0,
    pos_y       REAL NOT NULL DEFAULT 0,
    collapsed   INTEGER NOT NULL DEFAULT 1,     -- 1 = collapsed by default
    hidden      INTEGER NOT NULL DEFAULT 0,     -- user hid this device in this project
    z_order     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (project_id, device_id)
);

-- Per-project device overrides (label, color, tags, notes).
CREATE TABLE device_project_meta (
    project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    device_id   INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    label       TEXT,
    color       TEXT,
    tags_json   TEXT NOT NULL DEFAULT '[]',
    notes       TEXT,
    PRIMARY KEY (project_id, device_id)
);

-- Canvas viewport per project.
CREATE TABLE canvas_state (
    project_id     INTEGER PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
    zoom           REAL NOT NULL DEFAULT 1.0,
    pan_x          REAL NOT NULL DEFAULT 0,
    pan_y          REAL NOT NULL DEFAULT 0,
    last_opened_at REAL
);

-- --------------------------------------------------------------------------
-- Keys: IRKs for RPA resolution, LTKs for decryption
-- --------------------------------------------------------------------------

CREATE TABLE irks (
    id         INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    key_hex    TEXT NOT NULL,  -- 32 hex chars (16 bytes)
    label      TEXT,
    device_id  INTEGER REFERENCES devices(id) ON DELETE SET NULL,
    notes      TEXT,
    created_at REAL NOT NULL DEFAULT (strftime('%s','now')),
    UNIQUE(project_id, key_hex)
);

-- LTKs are global: they bind to device pairs, which are themselves global.
CREATE TABLE ltks (
    id            INTEGER PRIMARY KEY,
    key_hex       TEXT NOT NULL,   -- 32 hex chars
    ediv          INTEGER,
    rand_hex      TEXT,
    label         TEXT,
    device_a_id   INTEGER REFERENCES devices(id) ON DELETE SET NULL,
    device_b_id   INTEGER REFERENCES devices(id) ON DELETE SET NULL,
    notes         TEXT,
    created_at    REAL NOT NULL DEFAULT (strftime('%s','now'))
);

-- --------------------------------------------------------------------------
-- Observed topology: connections + Auracast broadcasts
-- --------------------------------------------------------------------------

CREATE TABLE connections (
    id                    INTEGER PRIMARY KEY,
    session_id            INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    access_address        INTEGER NOT NULL,
    central_device_id     INTEGER REFERENCES devices(id) ON DELETE SET NULL,
    peripheral_device_id  INTEGER REFERENCES devices(id) ON DELETE SET NULL,
    started_at            REAL NOT NULL,
    ended_at              REAL,
    interval_us           INTEGER,
    latency               INTEGER,
    timeout_ms            INTEGER
);
CREATE INDEX idx_connections_session ON connections(session_id);

CREATE TABLE broadcasts (
    id                     INTEGER PRIMARY KEY,
    session_id             INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    broadcaster_device_id  INTEGER REFERENCES devices(id) ON DELETE SET NULL,
    broadcast_id           INTEGER,   -- Broadcast_ID from BIGInfo
    broadcast_name         TEXT,
    big_handle             INTEGER,
    bis_count              INTEGER,
    phy                    TEXT,
    encrypted              INTEGER NOT NULL DEFAULT 0,
    first_seen             REAL NOT NULL,
    last_seen              REAL NOT NULL
);
CREATE INDEX idx_broadcasts_session ON broadcasts(session_id);

CREATE TABLE broadcast_receivers (
    broadcast_id      INTEGER NOT NULL REFERENCES broadcasts(id) ON DELETE CASCADE,
    device_id         INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    first_seen        REAL NOT NULL,
    last_seen         REAL NOT NULL,
    packets_received  INTEGER NOT NULL DEFAULT 0,
    packets_lost      INTEGER NOT NULL DEFAULT 0,
    rssi_avg          REAL,
    PRIMARY KEY (broadcast_id, device_id)
);

-- --------------------------------------------------------------------------
-- Sniffers: physical capture hardware (dongles, DKs). Identified by USB
-- serial number; we track which port they're plugged into so a multi-port
-- hub maps consistently into the canvas's vertical sort order.
-- --------------------------------------------------------------------------
-- "Active" = found in the most recent discovery sweep. "Removed" = user
-- hid it via the X button (still in the table for re-appearance later).
-- A sniffer's serial is unique across firmware modes (DFU bootloader vs
-- application FW): we record the most recent one.
CREATE TABLE sniffers (
    id              INTEGER PRIMARY KEY,
    serial_number   TEXT NOT NULL UNIQUE,
    kind            TEXT NOT NULL DEFAULT 'unknown',  -- dongle | dk | unknown
    name            TEXT,                              -- user-set or autogen
    usb_port_id     TEXT,                              -- /dev/cu.usbmodem... etc.
    location_id_hex TEXT,                              -- USB physical-port id
    interface_id    TEXT,                              -- extcap interface value
    display         TEXT,                              -- display from extcap
    usb_product     TEXT,                              -- from USB descriptor
    is_active       INTEGER NOT NULL DEFAULT 0,
    removed         INTEGER NOT NULL DEFAULT 0,
    first_seen      REAL NOT NULL,
    last_seen       REAL NOT NULL,
    notes           TEXT
);
CREATE INDEX idx_sniffers_active ON sniffers(is_active, removed);
CREATE INDEX idx_sniffers_location ON sniffers(location_id_hex);

-- --------------------------------------------------------------------------
-- App-level meta (last active project, misc state)
-- --------------------------------------------------------------------------

CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- --------------------------------------------------------------------------
-- RPA collapse / cluster framework (v3)
-- --------------------------------------------------------------------------

-- Per-device AD-type vocabulary. One row per unique (device, ad_type, ad_value)
-- tuple observed across all sessions. Upserted at ingest time; never deleted.
-- Used by signals: service_uuid_match, mfg_data_prefix, apple_continuity,
-- status_byte_match, tx_power_match.
CREATE TABLE device_ad_history (
    device_id   INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    ad_type     INTEGER NOT NULL,    -- BLE AD type byte (0x01..0xFF)
    ad_value    BLOB    NOT NULL,    -- raw AD payload bytes
    first_seen  REAL    NOT NULL,
    last_seen   REAL    NOT NULL,
    count       INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (device_id, ad_type, ad_value)
);
CREATE INDEX idx_dah_type ON device_ad_history(ad_type);

-- Per-packet event stream. One row per BLE packet that passes CRC. Populated
-- by the live-ingest path (gated behind sessions.keep_packets). Used by
-- signals: rotation_cohort (timestamps), rssi_signature (per-sniffer RSSI).
-- raw is nullable; only stored when sessions.keep_raw = 1.
CREATE TABLE packets (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  INTEGER NOT NULL REFERENCES sessions(id)  ON DELETE CASCADE,
    device_id   INTEGER NOT NULL REFERENCES devices(id)   ON DELETE CASCADE,
    address_id  INTEGER NOT NULL REFERENCES addresses(id) ON DELETE CASCADE,
    ts          REAL    NOT NULL,    -- unix epoch, sub-ms precision
    rssi        INTEGER NOT NULL,    -- signed dBm
    channel     INTEGER NOT NULL,    -- 37/38/39 primary; 0..36 data
    pdu_type    INTEGER NOT NULL,    -- BLE PDU type byte
    sniffer_id  INTEGER REFERENCES sniffers(id),
    raw         BLOB                 -- nullable; opt-in via sessions.keep_raw
);
CREATE INDEX idx_packets_device_ts  ON packets(device_id,    ts);
CREATE INDEX idx_packets_sniffer_ts ON packets(sniffer_id,   ts);
CREATE INDEX idx_packets_session_ts ON packets(session_id,   ts);

-- Cluster header. One row per physical device identity inferred by the runner.
-- source: 'auto' = runner decision, 'manual' = user override, 'irk' = IRK match.
CREATE TABLE device_clusters (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    label           TEXT,
    created_at      REAL NOT NULL,
    last_decided_at REAL NOT NULL,
    source          TEXT NOT NULL DEFAULT 'auto',
    -- Sticky-cluster flag. When True (default for new auto-runs),
    -- the cluster persists across runner ticks; future runs ADD
    -- members but never tear it down or remove members absent
    -- explicit negative evidence. Once user-confirmed via the UI,
    -- this also stays True. Manual splits set this to 0 to allow
    -- the next run to re-evaluate from scratch.
    confirmed       INTEGER NOT NULL DEFAULT 1
);

-- Cluster membership. A device belongs to at most one cluster; enforced by the
-- runner (not a schema constraint, to allow re-clustering transitions).
-- contributions is JSON: {"signal_name": [score, weight], ...}
CREATE TABLE device_cluster_members (
    cluster_id    INTEGER NOT NULL REFERENCES device_clusters(id) ON DELETE CASCADE,
    device_id     INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    score         REAL,
    contributions TEXT,    -- JSON
    profile       TEXT,
    decided_at    REAL NOT NULL,
    decided_by    TEXT NOT NULL DEFAULT 'auto',
    PRIMARY KEY (cluster_id, device_id)
);
CREATE INDEX idx_dcm_device ON device_cluster_members(device_id);
