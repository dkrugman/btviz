# Active interrogation — revised plan

After working through the critique. This is the plan I'd actually
build. Differences from the initial plan are flagged with **Δ**.

## 1. Posture

**Manual-only for v1.** No auto-probe. The user explicitly clicks
"Probe device" on a canvas card; the coordinator runs one probe;
we display the result. Auto-probe is a follow-up when the manual
flow is debugged and proven non-disruptive. **Δ from §1 of initial.**

Rationale: (a) addresses A1 — no probe storms; (b) addresses E2 —
every probe is user-attested; (c) lets us learn which devices
respond well before automating.

## 2. Eligibility

A device is eligible for a probe when:

- A TX-capable dongle is currently free (role = `Idle` and
  `is_tx_capable=True`), AND
- The device's `last_seen` is within 60 s (else the address may
  have rotated), AND
- The device is not on the do-not-probe list.

Default do-not-probe list: device_class in {`iphone`, `ipad`,
`apple_watch`, `mac`}. **Δ — A5.** These reject anonymous central
connects fast. Probing them is a slot waste.

If no TX-capable dongle is free, the UI shows the "Probe" action
disabled with a tooltip explaining why. **Δ — A4.**

## 3. Following vs. connecting

Unchanged from §1 of initial plan. Both are first-class operations:

- `Follow(addr, irk?)` — sniffer dongle, RX-only, passive.
- `Probe(addr, irk?)` — DK, TX-capable, time-bounded transaction.

Both can run simultaneously on different devices. Following the
HA-L on dongle A while probing the iBeacon on the DK is fine.

## 4. When to drop

Per-class timeouts. **Δ — B1.**

```
default_probe_timeout_s    = 5.0
hearing_aid_timeout_s      = 12.0
le_audio_timeout_s         = 12.0
airtag_timeout_s           = 8.0
find_my_timeout_s          = 8.0
```

Stored in a `probe_timeouts` table seeded from a TOML config so
the user can tune without editing code, mirroring how
`cluster/profiles/*.toml` works.

Drop conditions:

- All required reads complete.
- Per-class timeout elapsed.
- ATT error 0x05 (insufficient auth) or 0x0E (insufficient
  encryption) — record as `requires_pairing=True` on the device.
  **Δ — B2.** We don't pair; we record the fact.
- ATT error 0x02 (read not permitted) on a specific char — record
  per-char and continue with other chars in the same probe.
- Disconnect-by-target — record the supervisor-timeout reason from
  HCI.
- User cancel — UI button, immediate `LL_TERMINATE_IND`.
- Capture stop — coordinator-driven, see §10.

## 5. Data targets per probe

Three tiers. v1 reads tier 1 only.

**Tier 1 (always):**
- GAP service: 0x2A00 Device Name, 0x2A01 Appearance.
- Device Information service (0x180A): all standard chars listed
  in `01_initial_plan.md` §4.
- Service-list snapshot (every primary service UUID).

**Tier 2 (on user request, e.g., "Probe deeply"):**
- For every primary service: enumerate characteristic UUIDs (no
  reads). Records the *structure* of the GATT directory without
  reading state-bearing chars.

**Tier 3 (debug only, manual):**
- Specific user-selected characteristic reads.

Battery level (0x2A19) and other state-bearing chars are never
read in tier 1 because they change. If the user wants battery,
they ask for it explicitly.

## 6. Storage model

**Δ — C2/C3/C4/C5 simplifications.**

Three tables (down from four):

```sql
CREATE TABLE gatt_values (
    value_hash  TEXT    PRIMARY KEY,           -- sha1 hex of value_blob
    value_blob  BLOB    NOT NULL,
    value_text  TEXT,                          -- decoded UTF-8 if applicable
    first_seen  REAL    NOT NULL,
    last_seen   REAL    NOT NULL
);

CREATE TABLE device_gatt_chars (
    device_id     INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    service_uuid  TEXT    NOT NULL,            -- e.g. '0000180a-...'
    char_uuid     TEXT    NOT NULL,            -- e.g. '00002a29-...'
    value_hash    TEXT    REFERENCES gatt_values(value_hash),
    att_error     INTEGER,                     -- NULL on success
    first_seen    REAL    NOT NULL,
    last_seen     REAL    NOT NULL,
    read_count    INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (device_id, service_uuid, char_uuid),
    CHECK (
        (value_hash IS NOT NULL AND att_error IS NULL)
        OR (value_hash IS NULL AND att_error IS NOT NULL)
    )
);
CREATE INDEX idx_dgc_char_value ON device_gatt_chars(char_uuid, value_hash);

CREATE TABLE probe_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id   INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    sniffer_id  INTEGER REFERENCES sniffers(id) ON DELETE SET NULL,
    started_at  REAL    NOT NULL,
    ended_at    REAL,
    outcome     TEXT    NOT NULL DEFAULT 'pending',  -- pending | success | timeout | rejected | cancelled | error
    detail      TEXT                                -- free-form, e.g. ATT error code summary
);
CREATE INDEX idx_probe_runs_device ON probe_runs(device_id);
```

`gatt_chars` (static SIG dictionary) lives in code, not the DB —
populated as a Python dict in `gatt_dictionary.py`. Cleaner than
seed migrations and keeps the dictionary versioned with the
codebase. **Δ — schema simpler than initial plan.**

`device_gatt_history` (initial plan §8) is dropped for v1 — C2.

The CHECK constraint on `device_gatt_chars` enforces that exactly
one of `value_hash` or `att_error` is set. **Δ — C5.** "Char
absent" is represented by row absence; "char present, read failed"
is `att_error IS NOT NULL`; "char present, read returned empty
bytes" is `value_hash` of `da39a3ee...` (sha1 of empty string).
All three states are distinct.

## 7. Architecture

### 7.1 HCI driver

`pc-ble-driver-py` for the DK path. **Δ — D1.** Decision: don't
write our own HCI from scratch. The library is mature, gives us
Central role + GATT client, and uses the connectivity firmware
we already flashed.

Trade-off acknowledged: tied to Nordic's binary. We pin the
version in `pyproject.toml`. If we outgrow it, we can swap to a
custom HCI later — but only if we have a compelling reason.

`bleak` (host BT radio) as a fallback is rejected for v1. **Δ —
D1, G3.** Hides the connection from our own passive sniff and
breaks the role-planning model. Maybe later as an "assist" mode
for users without a DK.

### 7.2 Coordinator placement

Persistent worker on its own QThread, lessons from PR #83. **Δ —
D2, G5.** Class-level Signal carries `ProbeRequest` from main
thread; worker emits `ProbeResult` back. SQLite writes happen on
main thread (matches Store connection's affinity).

### 7.3 Role-planner integration

New role: `Probe(addr, irk?)`. The probe coordinator asks the
capture coordinator to "borrow" a TX-capable dongle:

```python
# capture/coordinator.py
def borrow_tx_dongle(self, requester: str) -> str | None:
    """Hand a free TX-capable dongle to ``requester``. Returns
    its short_id, or None if none available. The dongle's role
    is set to Idle while borrowed; the requester is responsible
    for releasing it via ``release_dongle``."""

def release_dongle(self, short_id: str) -> None:
    """Return a borrowed dongle to the role planner."""
```

This keeps the role planner authoritative. The probe coordinator
holds the borrowed dongle for the duration of one probe, releases
on success/timeout/cancel. **Δ — D3.**

### 7.4 Capture-stop semantics

Probe coordinator subscribes to capture's stop event. On stop:
cancel all in-flight probes (LL_TERMINATE_IND), drain the queue
(no new probes), release any borrowed dongles. **Δ — D4.**

## 8. Privacy / audit

**Δ — E2.** Every probe inserts a row in `probe_runs`. The user can
review them in a "Probes" panel (future UI). The dongle uses a
fresh random address per probe — `pc-ble-driver-py` exposes
`set_address()` on the central role; we call it before each
connect. Don't reuse addresses across probes; don't reuse a
single random address for the canvas's lifetime.

## 9. Use cases (v1)

The supported user stories:

1. **"What firmware is this hearing aid running?"** Right-click
   the HA card → Probe. Result populates Manufacturer / Model /
   Firmware Revision in the device card and DB.
2. **"Is this Find My beacon a real AirTag or a clone?"** Same
   flow, on an AirTag-class card. Result populates PnP ID; if
   VID = 0x004C and PID = 0x10A1 → genuine AirTag.
3. **"Show me all devices with the Heart Rate service."** Query
   over `device_gatt_chars` for service_uuid = 0x180D. Works
   only after probes have been run; that's fine for v1.

## 10. Out of scope (v1)

- Auto-probe heuristics.
- Pairing.
- Tier 2 / Tier 3 reads.
- Battery level history.
- Firmware-update detection.
- bleak fallback.
- A "Probes" review panel.
- Classic Bluetooth — separate document, separate phase.

## 11. Phased rollout

| Phase | Scope | Estimated complexity |
|---|---|---|
| 0 (this PR) | Docs + scaffolding + schema migration | low |
| 1 | HCI driver + GATT client (Tier 1 reads) + manual probe action | medium-high |
| 2 | `probe_runs` UI panel + per-class timeouts from TOML | low |
| 3 | Auto-probe heuristics (with rate limit + opt-in) | medium |
| 4 | Tier 2/3 reads, battery history, classic BT path | high (separate plan) |
