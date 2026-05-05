# Active interrogation — self-critique

The initial plan reads cleanly but several decisions are
under-justified or wrong. Issues and questions, grouped.

## A. Connection initiation policy

**A1. The auto-policy is too eager.** "First-time observation of a
device with vendor-prefixed local_name but no model" is most
devices in a busy capture. Result: a probe storm on capture start,
each probe consuming radio time on the one TX-capable dongle. Need
a rate limit and a "is this device interesting enough to probe"
gate.

**A2. We probe an RPA, but the RPA may rotate before connection
establishes.** Apple's RPA rotation interval is typically 15 min,
hearing aids more aggressively. If the address rotates between our
CONNECT_IND and the target's response, we have a half-open
connection and a wasted radio slot.

**A3. The "cluster confirmation" trigger is circular.** A 4-RPA
cluster crossing 0.95 is already "confirmed" by the cluster
framework. Probing it after that adds Manufacturer/Model that we
mostly already have from `apple_continuity` parsing of the adv
data. The probe is most valuable on devices the cluster framework
*can't* identify — which is the opposite of what the rule says.

**A4. We don't say what happens when no TX-capable device is
available.** With one DK, the queue is the entire policy. Need
explicit semantics: drop, defer, fail-loudly?

**A5. Probing iPhones is bad.** iOS Continuity makes iPhones extremely
chatty over BLE, but they also reject anonymous central connects
fast. We'll get an ATT error and waste a slot. iPhones / iPads /
Macs / Apple Watches should be in a "do not auto-probe" list by
default.

## B. Connection drop policy

**B1. 5 s timeout is too short for some devices.** Hearing aids,
Find My beacons, and LE Audio devices can take >5 s to respond to
read requests because they're optimized for ultra-low duty cycle.
Need per-class timeouts informed by device_class.

**B2. ATT error 0x05 / 0x0E (auth/encryption required) means we
should *retry with bonding*, not give up.** Decision needed: do we
ever pair? My suggestion: no, because pairing creates lasting
state on the target and on us, and that violates the "we read
what targets willingly publish" stance from the overview. But
that means we'll never get past 0x05/0x0E and we should record
it as a permanent attribute of the device, not a transient error.

**B3. We don't say what happens to in-flight ingest during a
connection.** When the DK is acting as a Central, it's not sniffing.
If it's the only TX-capable device and we stop using it for
`Pinned(37)`, we lose data on channel 37 for the duration of the
probe. That's an explicit trade-off the user should opt into, not
a side-effect. With ≥4 dongles + 1 DK, the role planner
already reserves the DK; the loss is zero. With 2-3 dongles + 1
DK, taking the DK for probing means downgrading scan coverage.

## C. Data model

**C1. `value_hash` storage is good but doesn't solve "is this
char's value the same as that other device's value" cleanly.** Two
devices both returning Manufacturer Name "Apple Inc." get one
`gatt_values` row. Good. But to query "which devices have
Manufacturer Name = Apple", we'd join `device_gatt_chars` →
`gatt_values` on `value_hash` filtered by `value_text = 'Apple
Inc.'`. That's fine but we should also have an index on
`(char_uuid, value_hash)` to make "everyone with the same value of
char X" cheap.

**C2. `device_gatt_history` is over-engineered for a feature we
don't have a UI for yet.** Simpler v1: just bump `last_seen` on
re-read and don't track history. Add `device_gatt_history` when
someone asks "show me when this firmware was upgraded."

**C3. We need a per-probe identifier to correlate "this set of
reads happened on this attempt." A `probe_runs` table:
`(id, device_id, sniffer_id, started_at, ended_at, outcome)`.
Without it, debugging "this probe gave us Manufacturer but not
Model — what error did it hit on Model?" is impossible.

**C4. ATT errors are first-class data, not exceptions.** "Char X
returns 0x02 read-not-permitted" is a fingerprintable property of
the device's firmware. Store the error code in
`device_gatt_chars.att_error` (initial plan said this; reinforce:
it's *not* optional, and the schema must allow `value_hash IS NULL`
when `att_error IS NOT NULL`).

**C5. The "Empty values are signal" claim needs structure.** A
device that *has* the char but read returns empty bytes is
different from a device that *doesn't have* the char at all.
Schema needs to distinguish:
- char present + read-permitted + value = "" (empty string)
- char present + read-not-permitted (att_error = 0x02)
- char absent (no row in `device_gatt_chars`)

## D. Architecture

**D1. HCI over UART is significant scope.** Implementing HCI from
scratch is weeks of work. `pc-ble-driver-py` (Nordic's official
Python bindings to their `pc-ble-driver` C library) gives us a
Central role with one import. Trade-off: it pulls in a Nordic-
specific binary blob; doesn't work on every host without
matching firmware versions.

Alternatives:
- **`bleak`** — cross-platform Python BLE client. Uses
  CoreBluetooth on macOS, BlueZ on Linux, WinRT on Windows. Doesn't
  use our DK at all — uses the host machine's own BT radio.
  Pro: zero hardware dependency. Con: bypasses our role-planning
  entirely, and we can't observe the connection in our own
  passive capture.
- **`pc-ble-driver-py`** — uses our DK as the central. Matches
  our architecture (we own the radio, the host doesn't).
  Pro: integrates with role planner. Con: tied to Nordic binary,
  binary pinned to specific connectivity-firmware build.
- **Custom HCI** — write our own. Pro: full control. Con: months
  of work and we'd reimplement what `pc-ble-driver` already does.

The right answer is `pc-ble-driver-py` for the DK path, and
*optionally* `bleak` as a fallback when no TX-capable device is
plugged in. But that "optional fallback" raises issues C1/C2/C3
about whose connection is observed by which sniffer — the host
machine's BT radio is not on our 37/38/39 sniffers.

**D2. We don't say where the probe coordinator runs.** UI thread?
Worker thread? Subprocess? Given the cluster QThread saga (PR #83),
this matters.

**D3. The probe coordinator's queue interacts with the role
planner's reservation.** PR #75 reserves TX-capable devices as
`Idle` when N≥4. The probe coordinator needs to (a) ask the
coordinator to take the reserved dongle, (b) put it back into
`Idle` when done. The interface for "borrow the TX-capable
dongle" doesn't exist yet.

**D4. We don't say what happens if the user stops capture
mid-probe.** PR #83's persistent thread pattern is the model —
quit + join cleanly. Same applies here.

## E. Use cases / value

**E1. Why are we doing this?** The overview says "Nordic's iOS
Scanner does it" but doesn't connect the feature to a btviz user
story. Concrete use cases:

- "What firmware is my hearing aid running?" — answered by 0x180A
  Firmware Revision. Once-per-device. Useful.
- "Is this Find My beacon a real AirTag or a clone?" — answered by
  PnP ID + service set comparison. Once-per-device. Useful.
- "Did the hearing aid's firmware actually update?" — answered by
  re-reading 0x180A after firmware update. Manual trigger; valuable.
- "Which specific device of identical-looking pairs is which?" —
  answered by Serial Number (0x2A25). Once-per-device, but only if
  the device exposes Serial Number; some don't.

What this is *not* good for:

- General reconnaissance — that's pen-test territory.
- Decryption / pairing — explicitly out of scope.
- Real-time monitoring — probing eats too much radio time.

**E2. Privacy posture.** Active probes are visible to the target.
A target's logs (if it has logs) record connections from our
public/random address. We should:

- Use a random address per probe by default; rotate before next
  probe. (This is standard CoreBluetooth Central behavior.)
- Never auto-probe; always require user opt-in to *enable* the
  feature, even if individual probes are then auto-triggered.
- Log every probe — when, who, outcome — into a per-project
  audit table. Users can review what btviz did on their behalf.

## F. Classic Bluetooth integration

**F1. Bigger gap than the overview implies.** Classic BT and BLE
share the 2.4 GHz band but the link layer is fundamentally different.
Same `device` row can't represent both — `stable_key` is BLE-shaped
(`pub:`, `rs:`, `rpa:`, `irk:`); BT Classic uses BD_ADDR (LAP/UAP/NAP).

**F2. We need a `bd_addr` column or a separate `classic_devices`
table.** Cross-correlation by vendor/local_name is possible but
cosmetic; same-physical-device-on-both-stacks is hard.

**F3. Ubertooth, HackRF, and ice9 produce different output formats.**
Ubertooth → libbtbb → pcap with DLT_BLUETOOTH_BREDR_BB. HackRF +
ice9 → custom output format that's been pcap-ified in some forks.
Each path needs its own ingest code. Common dataclass:
`ClassicLinkPacket` mirroring `RawPacket`.

## G. Open questions for the user

1. **Pairing:** confirm the "no" position. The alternative is one-
   off pairing for stubborn devices. Adds complexity but unlocks
   chars some devices guard.
2. **Auto-probe by default vs. manual-only?** I'm leaning toward
   manual-only for v1; auto-probe is a follow-up once the manual
   flow is debugged.
3. **Use of host BT radio (`bleak`) as a fallback?** Pro: works
   without the DK. Con: hides the connection from passive sniff.
   I'd say no.
4. **Classic BT priority.** Is this an immediate v1 goal or a
   later phase? Building it as a parallel sniffer/decoder/ingest
   path is non-trivial.
5. **Where does the probe coordinator live?** Suggesting the
   persistent-worker pattern from PR #83 — one `QThread` parented
   to the canvas, dispatch via Signal.
