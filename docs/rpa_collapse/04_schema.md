# Schema Additions

The framework needs three new tables and one column addition. The
migration is gated by an `Alembic` revision separate from this branch
because it touches the live capture path (`record_packet`) and is a
chunky, reversible change in its own right.

## Existing schema (relevant subset)

```
devices         (device_id PK, vendor_id, ad_class, ad_kind, label,
                 first_seen, last_seen, hidden, layout_x, layout_y, ...)

addresses       (address_id PK, device_id FK, addr_bytes, kind,
                 first_seen, last_seen, resolved_via_irk_id NULLABLE)

ad_records      (ad_record_id PK, address_id FK, ts, raw_bytes, ...)
                  -- one row per advertisement seen
```

## New table: `device_ad_history`

**Purpose.** Per-device per-AD-entry vocabulary. Decoded once per
unique `(device_id, ad_type, ad_value)`; updated `last_seen` and
`count` on each subsequent observation. Cheap to query: "what
service UUIDs has this device ever advertised?"

```sql
CREATE TABLE device_ad_history (
    device_id    INTEGER NOT NULL,
    ad_type      INTEGER NOT NULL,           -- BLE AD type byte (0x01..0xFF)
    ad_value     BLOB NOT NULL,              -- AD-entry data bytes
    first_seen   REAL NOT NULL,
    last_seen    REAL NOT NULL,
    count        INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (device_id, ad_type, ad_value),
    FOREIGN KEY (device_id) REFERENCES devices(device_id)
);

CREATE INDEX idx_dah_type ON device_ad_history(ad_type);
```

**Population.** During live decode (`capture/live_ingest.py::
_on_packet`), after CRC validation, the AD-entry sequence is parsed.
For each entry:

```python
cur.execute("""
    INSERT INTO device_ad_history
        (device_id, ad_type, ad_value, first_seen, last_seen, count)
    VALUES (?, ?, ?, ?, ?, 1)
    ON CONFLICT (device_id, ad_type, ad_value) DO UPDATE SET
        last_seen = excluded.last_seen,
        count = count + 1
""", (device_id, ad_type, ad_value, ts, ts))
```

The `ON CONFLICT` upsert keeps the table size linear in unique
vocabulary, not packet count. Most devices have 5-20 unique
AD entries total (each entry is a fixed firmware-emitted blob).

**Storage estimate.** ~50 bytes per row average. 5000 devices × 10
unique entries = 50K rows, ~2.5 MB. Tiny.

## New table: `packets`

**Purpose.** Slim per-packet event log for temporal/spatial analysis.
Every packet that passes CRC writes a row here; the existing
`ad_records` continues to hold the *decoded payload* per RPA, while
this table holds the *event-stream* per device. Two different views
of the same firehose.

```sql
CREATE TABLE packets (
    packet_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id       INTEGER NOT NULL,        -- capture session
    device_id        INTEGER NOT NULL,
    address_id       INTEGER NOT NULL,
    ts               REAL NOT NULL,           -- unix epoch with sub-ms precision
    rssi             INTEGER NOT NULL,        -- signed dBm
    channel          INTEGER NOT NULL,        -- 37/38/39 primary, 0..36 data
    pdu_type         INTEGER NOT NULL,        -- 0..7 advertising; ext-only otherwise
    sniffer_short_id INTEGER NOT NULL,        -- which sniffer heard it
    raw              BLOB,                    -- NULLABLE: raw frame for forensic re-decode
    FOREIGN KEY (session_id) REFERENCES capture_sessions(session_id),
    FOREIGN KEY (device_id) REFERENCES devices(device_id),
    FOREIGN KEY (address_id) REFERENCES addresses(address_id)
);

CREATE INDEX idx_packets_device_ts    ON packets(device_id, ts);
CREATE INDEX idx_packets_sniffer_ts   ON packets(sniffer_short_id, ts);
CREATE INDEX idx_packets_session_ts   ON packets(session_id, ts);
```

**Why `raw` is nullable.** Per-packet raw bytes make the table grow
fast — at 200 packets/sec sustained, ~17M rows/day, multiplied by
~50 bytes raw = 850 MB/day raw storage. We want to *enable* forensic
re-decode (e.g. when a future signal needs a field we didn't decode
the first time) but make it opt-in via a `capture_sessions.keep_raw`
flag. Default off.

**Retention.** Per-packet rows are subject to the retention policy
TODO (drop rows older than N days). Aggregates live in
`device_ad_history` and are unaffected.

**Storage estimate.**
- Without `raw`: ~40 bytes/row → at 17M rows/day → 680 MB/day.
- With `raw`: ~95 bytes/row → ~1.6 GB/day.

That's why retention is a real concern, not just a TODO. We probably
want a 7-day rolling default with WAL checkpointing on rotation.

## New table: `device_clusters` and `device_cluster_members`

**Purpose.** Persistence layer for the aggregator's decisions. Members
are not joined back into `devices` — instead the cluster is a
*grouping*, leaving original device rows intact (and individually
queryable).

```sql
CREATE TABLE device_clusters (
    cluster_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    label            TEXT,
    created_at       REAL NOT NULL,
    last_decided_at  REAL NOT NULL,
    source           TEXT NOT NULL DEFAULT 'auto'  -- 'auto' | 'manual' | 'irk'
);

CREATE TABLE device_cluster_members (
    cluster_id     INTEGER NOT NULL,
    device_id      INTEGER NOT NULL,
    score          REAL,
    contributions  TEXT,                  -- JSON
    profile        TEXT,
    decided_at     REAL NOT NULL,
    decided_by     TEXT NOT NULL DEFAULT 'auto',
    PRIMARY KEY (cluster_id, device_id),
    FOREIGN KEY (cluster_id) REFERENCES device_clusters(cluster_id) ON DELETE CASCADE,
    FOREIGN KEY (device_id) REFERENCES devices(device_id)
);

CREATE INDEX idx_dcm_device ON device_cluster_members(device_id);
```

**Reverse lookup.** A device's cluster is found via:
```sql
SELECT c.cluster_id, c.label, m.score, m.contributions
FROM device_cluster_members m
JOIN device_clusters c ON c.cluster_id = m.cluster_id
WHERE m.device_id = ?;
```

A device should only ever be in one cluster (enforced by the
aggregator, not the schema; allows multi-membership during
re-clustering transitions).

**Manual exclusion rows.** The schema does not have a separate
`exclusions` table. Instead, manual exclusions are represented as
single-member clusters with `source='manual'`:

> "I, the user, have decided this RPA is its own physical device.
> Don't auto-merge it into anything."

The runner respects single-member manual clusters by skipping any
pair that touches them.

## Column addition: `addresses.resolved_via_irk_id`

**Status.** Already exists in the current schema (added when IRK
import was first scoped). Unchanged.

```sql
-- existing column on addresses
resolved_via_irk_id INTEGER NULLABLE,
FOREIGN KEY (resolved_via_irk_id) REFERENCES identity_keys(irk_id)
```

The `irk_resolution` signal reads this column directly; an
unresolved address has `resolved_via_irk_id IS NULL`.

## New table: `identity_keys`

**Purpose.** User-imported IRKs. One row per known device.

```sql
CREATE TABLE identity_keys (
    irk_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    label       TEXT NOT NULL,                 -- "Doug's iPhone 15 Pro"
    irk         BLOB NOT NULL,                 -- 16 bytes, AES-128 key
    source      TEXT NOT NULL,                 -- 'paste' | 'btsnoop' | 'macos-defaults'
    imported_at REAL NOT NULL
);
```

The IRK bytes are stored unencrypted. This is fine because:
- The user supplied them, knows they're sensitive, and chose to
  import them into a local DB on their own machine.
- They are equivalent in sensitivity to a Bluetooth pairing record,
  which the user's OS already stores unencrypted.
- The btviz DB itself is a local file under the user's home directory.

We do not include IRKs in any export, sync, or backup mechanism.
(The DB export feature deliberately strips this table.)

## Migration plan

The migration is one Alembic revision. It must be reversible. Order:

```
upgrade:
    create_table('device_ad_history', ...)
    create_table('packets', ...)
    create_table('device_clusters', ...)
    create_table('device_cluster_members', ...)
    create_table('identity_keys', ...)
    -- addresses.resolved_via_irk_id already exists; no-op

downgrade:
    drop all five tables; data is recoverable from existing
    devices/addresses/ad_records.
```

The migration must not block live capture. Strategy:

1. Apply with WAL mode active (already standard).
2. New tables are empty after upgrade — no backfill required for the
   new framework to begin scoring (signals that need historical data
   simply return `None` until enough data accumulates).
3. The capture-side population of `device_ad_history` and `packets`
   is added in a separate code change *gated* by a feature flag, so
   the migration can land before the population code goes live.
   (Keeps risk contained: schema without writers is harmless;
   writers without schema is a crash.)

## Backfill (optional, post-migration)

A background backfill task can populate `device_ad_history` from
existing `ad_records`:

```sql
INSERT OR IGNORE INTO device_ad_history (device_id, ad_type, ad_value,
                                          first_seen, last_seen, count)
SELECT a.device_id, ad.ad_type, ad.ad_value,
       MIN(ad.ts), MAX(ad.ts), COUNT(*)
FROM addresses a
JOIN ad_records ar ON ar.address_id = a.address_id
JOIN ad_entries ad ON ad.ad_record_id = ar.ad_record_id
GROUP BY a.device_id, ad.ad_type, ad.ad_value;
```

`packets` is *not* backfilled — old captures never carried the per-
packet metadata at the granularity `packets` requires (e.g. raw
bytes, sniffer attribution by short_id rather than long pcap path).

## What this branch ships re: schema

Nothing. Schema migration is a separate PR. This branch (the
framework skeleton) defines the *consumers* — signal modules and
the aggregator — that *will* read these tables. Until the migration
lands, the framework runs against synthetic in-memory data only,
and the live integration is feature-flagged off.
