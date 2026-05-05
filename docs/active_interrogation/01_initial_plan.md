# Active interrogation — initial plan

First pass. Direct answers to the questions in the original task.
Critiqued in `02_critique.md`; revised in `03_revised_plan.md`.

## 1. Following vs. connecting

These look similar but are very different operations.

| Aspect | Follow (passive) | Connect (active) |
|---|---|---|
| Role | `Follow(addr, irk?)` on a sniffer dongle | New `Probe(addr)` on the connectivity-firmware DK |
| Radio behaviour | Listen on adv channel, hop to data-channel map when CONNECT_IND seen | Send CONNECT_IND ourselves, become the central, manage L2CAP/ATT |
| Target awareness | Target has no idea we exist | Target sees a connect from our public/random address; logs us as "central" |
| What we see | Whatever the target broadcasts plus whatever its real central reads/writes | What we explicitly request (GATT reads, descriptor reads, service discovery) |
| Cost to target | Zero | Wakes radio, may break battery-saver hibernation, may displace other connection if peripheral is single-attach |
| Cost to us | One sniffer dongle on `Pinned`/`Follow` | One DK + radio time + may temporarily lose passive coverage of other channels |
| Survives RPA rotation | Yes if we have IRK | No — connection is over a fixed access address; if we drop, the next probe needs a fresh address resolution |

The two are complementary. A typical interrogation flow:

1. Passive observation surfaces a candidate device.
2. Cluster framework decides we have a stable identity (cluster
   confirmed, or single-row stable_kind).
3. *Optional* IRK-assisted follow to verify the candidate is one
   physical device across RPA rotations.
4. Schedule a probe — short connection, read GATT 0x180A, disconnect.
5. Persist GATT data into the device's row, drop back to passive.

Step 3 is optional because a probe doesn't *need* a follow first;
you can connect-by-address using whichever address the target
currently advertises. Following first just gives stronger confidence
that the address you're about to connect to is the device you think.

## 2. When to initiate a connection

Defaults (all overridable):

- **First-time observation of a device with vendor-prefixed local_name
  but no model.** Cheap one-shot probe to read 0x180A; if it gives us
  Manufacturer Name + Model Number, we never have to probe again.
- **User-triggered.** Right-click a device on canvas → "Probe."
  This is always available as long as the device's last_seen is
  recent (e.g. within 60s) and a TX-capable dongle is free.
- **Cluster confirmation rule.** A multi-RPA cluster that crosses a
  confirmation threshold (e.g., 4+ members, all with score ≥0.95)
  is worth one probe — confirms the cluster *is* a single physical
  device by reading the same Manufacturer/Model from any one RPA.

Defaults *not* in the auto-policy:

- **Repeated probing of the same device.** GATT 0x180A is immutable
  for the device's lifetime; reading it again gains nothing. Battery
  Level (0x2A19) and Firmware Revision (0x2A26) can change but slowly.
- **Probing devices with a stable cluster.** They've already
  surrendered enough identity passively.
- **Probing devices whose vendor we already recognize from OUI.**
  Public-MAC devices with a known OUI vendor get less per-probe
  value than RPA Apple/Samsung devices that hide everything.

## 3. When to drop a connection

A probe is a short-lived transaction, not a session. Drop on:

- **Success** — required GATT reads complete. Default targets:
  0x180A characteristics + GAP Appearance + service-list snapshot.
- **Idle timeout** — 5 s with no progress. Many devices reject
  reads from a non-bonded central; we don't want to hang.
- **Error** — ATT error response (0x05 insufficient authentication,
  0x0E insufficient encryption, etc.). Record the error and move on.
- **User cancel** — user clicks "Stop probe" or closes the canvas.
- **Higher-priority probe queued** — if a manual probe is requested
  while an auto-probe is running, the auto-probe yields.
- **Capture stop** — when the user stops live capture, all in-flight
  probes are cancelled.

## 4. What data can be returned (BLE)

Standard SIG services that are interesting:

- **GAP (0x1800)** — Device Name (0x2A00), Appearance (0x2A01),
  Peripheral Preferred Connection Parameters (0x2A04). The device's
  declared identity even when no local_name is broadcast.
- **GATT (0x1801)** — Service Changed (0x2A05). Mostly a control
  characteristic; not informative.
- **Device Information (0x180A)** — *the* identity treasure chest:
  - Manufacturer Name String (0x2A29)
  - Model Number String (0x2A24)
  - Serial Number String (0x2A25)
  - Hardware Revision String (0x2A27)
  - Firmware Revision String (0x2A26)
  - Software Revision String (0x2A28)
  - System ID (0x2A23)
  - PnP ID (0x2A50) — Vendor ID source + Vendor ID + Product ID + Product Version
- **Battery Service (0x180F)** — Battery Level (0x2A19). Stateful;
  worth recording with timestamp but not on every probe.
- **Heart Rate (0x180D)**, **Audio Stream Control (0x184E)**,
  **HID (0x1812)**, etc. — class-specific. The *presence* of these
  services is fingerprinting evidence (Zuo et al, "Automatic
  Fingerprinting of Vulnerable BLE IoT Devices"); the values are
  mostly stateful and not worth storing per-probe.

Vendor-specific services (UUIDs outside the SIG-allocated range)
are the most discriminating signal in practice — `BleScope`'s
finding was that 95% of nearby BLE IoT devices are uniquely
identifiable from advertised service UUIDs alone, and GATT
discovery extends that with the *full* UUID set rather than just
the few shoehorned into adv data.

## 5. How to analyze, what to keep

Layers, in increasing order of "discriminating per byte":

1. **Service-list signature** — the set of UUIDs advertised. Very
   stable per model. Cheap to store as a sorted hash.
2. **0x180A character values** — Manufacturer + Model uniquely
   identify a model line. Hardware/Firmware/Software revisions split
   model lines into firmware cohorts.
3. **PnP ID** — VID/PID pair. Vendor + product code that's machine-
   readable. Stable per model.
4. **Service-UUID *with* characteristic-UUID set per service** —
   models tend to use identical chars within a service; off-spec
   chars are vendor-specific and often unique.
5. **Battery / Firmware values** — slow-changing state. Useful for
   fingerprinting *individuals* across firmware updates.

Default: read everything in 0x180A + 0x1800 + service list. Only
read deeper when the user asks.

## 6. Upfront vs. reactive analysis

Upfront-known-useful (BT spec):

- 0x180A char values are universally identifying. Store all.
- GAP Appearance is discrete and always meaningful for icon picking.
- PnP ID is a fixed structure; parse and store as a tuple.

Reactive-discoverable (after seeing many captures):

- Vendor-specific UUIDs. Some will turn out to be unique per model;
  others will turn out to be ubiquitous (e.g., Apple's
  `7905f431-b5ce-4e99-a40f-4b1e122d00d0` in many Apple devices).
  Frequency analysis across captures reveals which UUIDs are
  identifying and which are noise.
- Field-by-field stability. Across 100 reads of the same device,
  which characteristics never change vs. which drift? Stability
  itself is metadata.

Implication: store *everything* once per device, and let an offline
analyzer compute "discriminating UUIDs / fields" by frequency.

## 7. Fingerprinting via name/value structure

Two devices' GATT directories overlap heavily:

- All BLE devices with 0x180A advertise the same standard char UUIDs.
- The *values* differ.
- The *which-chars-are-actually-readable* differs (some return ATT
  error 0x02 "read not permitted" on Serial Number, others return
  it; that pattern is itself fingerprinting evidence).

So:

- Same value set → likely same model.
- Same value set + same Serial Number → same physical device.
- Same UUID *structure* (which UUIDs exist, regardless of values) →
  likely same model line, possibly different firmware.

Empty values (no Serial Number returned, no Software Revision)
*are* signal — note the absence, don't drop the row.

## 8. Efficient storage

Don't store one row per (device, char, observation). Store:

- **`gatt_values`** (content-addressed): one row per distinct value
  ever observed. Columns: `value_hash` (SHA-1 hex), `value_blob`
  (raw bytes), `value_text` (decoded UTF-8 if applicable),
  `first_seen`, `last_seen`. Primary key on `value_hash`.
- **`device_gatt_chars`** (per-device, per-char observation):
  `device_id`, `service_uuid`, `char_uuid`, `value_hash`,
  `att_error` (if read failed, the error code), `first_seen`,
  `last_seen`, `read_count`. Primary key on
  (device_id, service_uuid, char_uuid). When a re-read returns the
  same value: bump `last_seen` and `read_count`, don't insert a
  new row. When it changes: update `value_hash` and bump counters.
- **`device_gatt_history`** (changes only): for chars where we want
  full history (Battery, Firmware), insert a row when the value
  changes from what's in `device_gatt_chars`. Most chars never
  change, so this table stays sparse.
- **`gatt_chars`** (static SIG dictionary): seed with SIG-assigned
  UUIDs and human names. Vendor UUIDs are not seeded; the analyzer
  populates them.

Result: re-reading an immutable char on the same device costs a
single `UPDATE` of `last_seen` + `read_count`. New device, same
model: rows in `device_gatt_chars` are unique but they all reference
the same handful of `value_hash` rows, so the bytes are written
once per model.

## 9. Architecture

```
src/btviz/probe/
  types.py         dataclasses (ProbeRequest, ProbeResult,
                   GattCharObservation, GattService)
  hci.py           HCI driver — pyserial-based UART transport
                   to the connectivity-firmware DK; HCI command
                   builder + event parser. Stub in this PR.
  gatt.py          GATT client over HCI events. Service discovery,
                   characteristic discovery, read/write. Stub.
  coordinator.py   Probe queue + scheduler. Decides which device
                   to probe next, picks a free TX-capable dongle,
                   dispatches to hci/gatt, persists results. Stub.
  storage.py       Storage adapter. Translates ProbeResult into
                   gatt_values / device_gatt_chars / gatt_history
                   inserts. Stub.
  gatt_dictionary.py   SIG UUID → human name. Static.

src/btviz/db/
  schema.sql                  add gatt_* tables
  migrations/v5_to_v6.sql     additive migration

src/btviz/capture/
  roles.py        add Probe(addr, irk?) role variant; default_roles
                  unchanged

src/btviz/ui/
  canvas.py       right-click → Probe action; show GATT data in
                  expanded device card; minimal "Probing…" status
```

Probe coordinator is event-driven — fires when:

- Live capture is running and the auto-policy heuristic says probe.
- User triggers manual probe.
- The cluster runner finishes and emits "cluster confirmed" for a
  newly-confirmed cluster.
