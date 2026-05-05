-- Active-interrogation schema additions.
--
-- Drafted alongside the design in docs/active_interrogation/.
-- Will be lifted into src/btviz/db/store.py as ``_V5_TO_V6_SQL`` and
-- spliced into the migration ladder when the first real consumer
-- (probe coordinator + storage adapter) lands.
--
-- Three tables and one index:
--
--   gatt_values         content-addressed value store (dedup across devices)
--   device_gatt_chars   one row per (device, service, char) — current value
--   probe_runs          one row per probe attempt — audit + debugging trail
--
-- See docs/active_interrogation/03_revised_plan.md §6 for rationale.

-- ──────────────────────────────────────────────────────────────────────
-- gatt_values: content-addressed bytes
--
-- Same value seen on N devices stores once. value_hash is a SHA-1 hex
-- string of value_blob — collision-resistant enough for dedup, easy to
-- read in ad-hoc queries, deterministic across processes.
-- ──────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS gatt_values (
    value_hash  TEXT    PRIMARY KEY,            -- sha1(value_blob).hexdigest()
    value_blob  BLOB    NOT NULL,
    value_text  TEXT,                           -- decoded UTF-8 if printable
    first_seen  REAL    NOT NULL,
    last_seen   REAL    NOT NULL
);

-- ──────────────────────────────────────────────────────────────────────
-- device_gatt_chars: per-device per-characteristic state
--
-- Exactly one of (value_hash, att_error) is set — char absence is
-- represented by *no row*, not by NULL on both. The CHECK constraint
-- enforces this.
--
-- last_seen / read_count let "we've already read this immutable char"
-- be a cheap UPDATE rather than a re-INSERT each time the user re-runs
-- a probe.
-- ──────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS device_gatt_chars (
    device_id     INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    service_uuid  TEXT    NOT NULL,             -- full 128-bit UUID
    char_uuid     TEXT    NOT NULL,             -- full 128-bit UUID
    value_hash    TEXT    REFERENCES gatt_values(value_hash),
    att_error     INTEGER,                      -- per Core Spec Vol 3 Part F §3.4.1.1
    first_seen    REAL    NOT NULL,
    last_seen     REAL    NOT NULL,
    read_count    INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (device_id, service_uuid, char_uuid),
    CHECK (
        (value_hash IS NOT NULL AND att_error IS NULL)
        OR (value_hash IS NULL AND att_error IS NOT NULL)
    )
);

-- "show me everyone with the same Manufacturer Name" / "everyone whose
-- Firmware Revision matches this hash" — both are (char_uuid, value_hash)
-- joins, indexed here so they don't scan.
CREATE INDEX IF NOT EXISTS idx_dgc_char_value
    ON device_gatt_chars(char_uuid, value_hash);

-- ──────────────────────────────────────────────────────────────────────
-- probe_runs: audit + debug trail
--
-- One row per probe attempt regardless of outcome. The detail column
-- is free-form so we can record HCI-level error codes, ATT response
-- summaries, "no TX-capable dongle free", etc.
-- ──────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS probe_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id   INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    sniffer_id  INTEGER REFERENCES sniffers(id) ON DELETE SET NULL,
    started_at  REAL    NOT NULL,
    ended_at    REAL,
    outcome     TEXT    NOT NULL DEFAULT 'pending',  -- pending|success|timeout|rejected|cancelled|error
    detail      TEXT
);
CREATE INDEX IF NOT EXISTS idx_probe_runs_device ON probe_runs(device_id);
