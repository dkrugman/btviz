# Classic Bluetooth integration

Separate concern from BLE active interrogation. Same physical band
(2.4 GHz ISM) but a different link layer, different addressing,
and different sniffing tools. This document is a phase-4 plan and
deliberately less detailed than the BLE work.

## Why bother

Three forces:

1. The "Even Black Cats" paper (Cominelli et al, 2022) demonstrated
   that BT Classic is *not* immune to tracking despite using fixed
   BD_ADDRs that aren't cleartext on the wire. Their full-band SDR
   sniffer recovers the UAP and tracks the master across hops.
2. Many devices run dual-mode (BR/EDR + BLE). Headphones, hearing
   aids' AirPods-style pairing, car stereos, smart-home hubs. btviz
   today shows half the picture.
3. You already asked about HackRF One/Pro. The literature path
   exists; this is "what would it take to land it in btviz."

## Hardware options

| Tool | Role | Cost | Capability ceiling |
|---|---|---|---|
| Ubertooth One | Single-channel BT Classic + BLE survey | ~$120 | LAP detection, partial UAP, no decryption, libbtbb decode |
| HackRF One | General SDR | ~$300 | Full-band BT Classic via ice9-bluetooth-sniffer; no built-in TX-Classic capability |
| HackRF Pro | Higher-spec SDR | ~$500 | Same software path as HackRF One, cleaner RF |
| nRF52840 (existing) | BLE only | already owned | Useless for BT Classic |

The recommendation in `private/papers/`'s "Even Black Cats" review
stands: **read the paper before buying hardware**. Implementation
effort dominates; the radio is the cheap part.

## Software stack

For Ubertooth:
- `libbtbb` — Bluetooth baseband decoder. C library; Python bindings
  via `pybtbb` exist but are old.
- `ubertooth-rx` CLI emits libbtbb-formatted packets to stdout or
  pcap with `DLT_BLUETOOTH_BREDR_BB` (DLT 161).

For HackRF + ice9:
- [`mikeryan/ice9-bluetooth-sniffer`](https://github.com/mikeryan/ice9-bluetooth-sniffer)
  — full-band sniff using HackRF, GFSK demodulation, hop sequence
  recovery, pcap output. C with Python wrappers possible.
- Output is also DLT_BLUETOOTH_BREDR_BB or a custom format
  depending on flags.

Both paths land in pcap files. btviz can ingest them via the
existing `extcap`-style flow but with a different DLT and a
different decoder.

## What changes in btviz

### Schema

A new table `bd_addresses` (parallel to `addresses`):

```sql
CREATE TABLE bd_addresses (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id   INTEGER REFERENCES devices(id) ON DELETE CASCADE,
    bd_addr     TEXT    NOT NULL,            -- "AA:BB:CC:DD:EE:FF"
    lap         INTEGER NOT NULL,            -- 24-bit Lower Address Part
    uap         INTEGER,                     -- 8-bit Upper Address Part (recovered)
    nap         INTEGER,                     -- 16-bit Non-significant Address Part (recovered)
    first_seen  REAL    NOT NULL,
    last_seen   REAL    NOT NULL
);
CREATE INDEX idx_bd_addr_device ON bd_addresses(device_id);
```

`devices.kind` gains a value: `bd_classic`. `devices.stable_key`
encoding for Classic: `bd:<bd_addr_lowercase>`.

### Decode

New module `src/btviz/decode/classic.py` mirroring `decode/adv.py`.
Parses libbtbb-shaped pcap records. Returns a `DecodedClassic`
dataclass that ingests into the same `record_packet` pipeline (with
appropriate pdu_type / channel translation).

### Capture

New `src/btviz/capture/classic.py` — Ubertooth or HackRF path is
selected based on which device is detected. Likely separate
extcap-style subprocesses launched alongside the BLE sniffers.

### UI

Canvas needs a third `kind` color tint (`bd_classic`). The Classic
card schema needs to show LAP/UAP separately and indicate when UAP
is "recovered" vs "observed in clear." Following / probing don't
apply — Classic capture is read-only by definition; no "connect to
Classic" feature is in scope.

### Cross-correlation

A device may show up on both BLE and BT Classic with related
identifiers. The strongest correlator is OUI: BLE public MAC and
BT Classic NAP/UAP both contain the OUI in the top 24 bits. If
they match *and* the local_name overlaps, we can link the two
`devices` rows via a new `device_links` table:

```sql
CREATE TABLE device_links (
    a_device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    b_device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    confidence  REAL    NOT NULL,
    reason      TEXT    NOT NULL,           -- 'oui_plus_name_match', etc.
    decided_at  REAL    NOT NULL,
    PRIMARY KEY (a_device_id, b_device_id)
);
```

Cluster framework gets a new signal class — `dual_mode_match` —
that scores BLE/Classic device pairs and feeds the same
aggregator. Existing infrastructure (signals, profiles, runner)
works unchanged.

## Phasing

This is a v4-or-later concern. The immediate-term path:

1. Get BLE active interrogation working (the bulk of this design
   doc).
2. Use it for ~3 months. Learn what the user actually wants.
3. Decide whether Classic BT pays for itself given the
   implementation effort.

If yes: start with Ubertooth (cheaper, more mature tooling). HackRF
is the upgrade path for higher fidelity once Ubertooth proves the
value.
