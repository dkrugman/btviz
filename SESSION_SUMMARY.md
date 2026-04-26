# Overnight session summary — 2026-04-26

Branch: `auto/overnight-2026-04-26`
Three commits, one per task. Each was smoke-tested headlessly before
committing. **No `main` writes; the branch is purely additive.**

## What landed

### Task 1 — passive Auracast extraction (commit `d54241d`)
- New `src/btviz/decode/auracast.py` parses Broadcast Audio Announcement
  Service (UUID 0x1852) payloads out of `ADV_EXT_IND` packets.
- New `Broadcasts` repo in `src/btviz/db/repos.py` with `upsert(...)`.
- `ingest/pipeline.py` now populates the `broadcasts` table during ingest.
- `IngestReport` gained a `broadcasts_seen` counter.

**Verification on `private/test.pcapng`:** 1 Auracast detected
("Avantree Oasis Aura_65ac", broadcast_id `0xB865AC`). Existing
device/packet baselines unchanged (40 devices, 8944 observations, 0.9s).

BIGInfo / BASE structure parsing is wired but the standard nRF Sniffer
firmware in this capture didn't sync to PA, so those fields stay null —
they'll populate naturally once toolkit-firmware captures feed the
same DB.

### Task 3 — IRK input on `follow` CLI (commit `694dc3e`)
- `Follow` dataclass now takes optional `irk_hex: str | None`. Validates
  32 hex chars (128 bits) with optional `0x` prefix.
- `btviz sniffers`'s `follow` accepts `--irk <32hex>`:
  ```
  follow <dongle> <addr> [r] [--irk 0123456789abcdef0123456789abcdef]
  ```
- Coordinator threads `role.irk_hex` through to the existing
  `sp.add_irk()` in `extcap/sniffer.py`, which writes the proper
  Wireshark control-pipe sequence.
- IRK is **never echoed in full** — display in `short_name()` masks to
  first4…last4.

**Discovery note:** the Nordic extcap has no `--irk` CLI flag. IRK is
delivered via Wireshark's extcap control-pipe protocol (`KEY_TYPE_IRK
= 5`, payload `0x<32hex>`). The wire format was already implemented in
`extcap/sniffer.py` — this task just wired the new role field to it.
Confirmed `KEY_TYPE_IRK = 5` against `nrf_sniffer_ble.py
--extcap-config`.

### Task 5 — apple_device subdivision (commit `129b5c1`)
- `apple_continuity.classify()` now refines based on Nearby Info
  action_code:
  - action `0x0D` (watch_lock_screen) → `apple_watch`
  - action `0x0F` (wake) → `mac`
- `iphone`, `ipad`, `mac` registered in `_DEVICE_CLASS_ICONS`.
- New `_class_precedence` guard in `pipeline.py` prevents downgrades —
  once a device's class is set to a specific identity, a later packet
  carrying only a generic signal can't overwrite it.

**Verification on `private/test.pcapng`:** 1 device reclassified
`apple_device` → `mac` (rpa:7e:a3:88:f8:74:72 emits Nearby action
0x0F). Other 28 apple_device entries lacked distinctive action codes
and stayed generic — exactly the conservative behavior I aimed for.

iPhone vs iPad is **not** refined. Both broadcast similar Nearby ranges
and reliable discrimination from passive sniffing isn't viable without
cellular-related sub-types we don't interpret.

## What was deferred (per prompt)
- Thread 2 (PacketLogger `.pklg` ingest)
- Thread 4 (Auracast toolkit serial-shell wrapper)
- Thread 6 (Live capture → DB)

These were explicitly out of scope. Nothing in those areas was touched.

## Resume protocol
```sh
git fetch
git log --oneline auto/overnight-2026-04-26
git checkout auto/overnight-2026-04-26
# Read this file. Then merge / cherry-pick at your discretion.
```

To smoke-test the final state on your machine:
```sh
rm -f private/ingest_test.db*
.venv/bin/btviz ingest private/test.pcapng --project home-lab \
    --db private/ingest_test.db
sqlite3 private/ingest_test.db "
  SELECT 'class' AS k, device_class AS v, COUNT(*) AS n
    FROM devices GROUP BY device_class
  UNION ALL
  SELECT 'broadcast', broadcast_name,
         printf('id=0x%06X', broadcast_id)
    FROM broadcasts;
"
```

Expected: 5 airtag, 3 airpods, 28 apple_device, 2 hearing_aid, 1 phone,
1 mac, plus the Avantree broadcast (0xB865AC).
