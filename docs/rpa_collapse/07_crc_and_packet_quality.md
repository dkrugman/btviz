# CRC errors + per-device packet quality

This doc captures the design constraints around CRC-error
visualization and per-device packet-quality tracking — the piece
that motivates the new `pkt.crc_ok` flag and the sniffer-panel
dropout flash.

## What this PR does

- Plumbs `crc_ok` through the decode → ingest path. Both DLT-256
  (PHDR) and DLT-272 (Nordic-BLE) decoders now return a
  `crc_ok=False` placeholder for CRC-failed packets instead of
  silently dropping them.
- `record_packet` skips CRC-failed packets early so they never
  spawn device rows — preserves the no-ghost-RPA invariant.
- `LiveIngest`'s per-source callback signature gains a third arg
  `crc_ok: bool`. The sniffer panel uses it to render a
  visually-distinct "dropout" flash (near-black with speckle noise
  inside the dot) plus per-sniffer good/bad counters surfaced in
  the row tooltip.

## Why per-device CRC tracking is harder than per-sniffer

The user's desired UX includes a per-device pie chart with three
slices:

  * received good
  * received bad
  * expected (not received)

This isn't trivially achievable from passive captures because:

### 1. CRC-failed packets have unreliable addresses

The 24-bit LL CRC is computed across the *whole* PDU including the
advertising address. When the firmware reports CRC fail, the
corruption is distributed across the bytes — usually the address
field itself has 1-4 bit errors. We can't trust those bits to
attribute the packet to a device row.

That's why this PR keeps CRC-failed packets out of `record_packet`.
A previous regression (visible in `git log fix/decode-drop-crc-
failed-packets`) showed 72% of random-kind device rows in the
user's DB were ghost addresses 1-4 bits off a real canonical —
exactly the problem we'd recreate by attributing CRC fails by
parsed address.

So per-device CRC-fail attribution needs *something other than the
parsed address bytes*. Two possible approaches:

### 2a. Sniffer-diversity inference

With 8-9 sniffers (post Friday's order), more than one sniffer is
typically tuned to the same channel at any moment. If sniffer A
gets a clean packet from device X on channel 37 at time T, and
sniffer B gets a CRC-failed packet on channel 37 at T ±100µs, the
CRC fail almost certainly belongs to device X — same packet, two
receivers, one decoded cleanly.

Implementation outline:

  * Index recent clean packets by `(channel, ts_window)` keyed
    per-sniffer.
  * On each CRC-fail event, look for any clean packet from a
    *different* sniffer in the last ~500 µs on the same channel.
    If found, attribute the bad packet to that device.
  * If no sniffer-diversity hit, the bad packet stays
    sniffer-level only.

Coverage depends on how often two sniffers happen to overlap on a
single PDU. For primary advertising channels with 3 sniffers
pinned to 37/38/39, every clean packet is on exactly one sniffer
and a CRC-fail on the same channel can only be attributed if the
geographic coverage overlaps. With 6+ sniffers some sharing data
channels, coverage improves.

### 2b. Expected-packets timing model

For periodic advertisers (Auracast, AirPods, AirTags, iPhones with
Continuity), the ad cadence is observable. After hearing N clean
packets from the same advertiser at intervals near `T_adv`, we
can predict the next packet's arrival. If a sniffer is on the
right channel at the predicted time and we DON'T receive (no
clean packet, no CRC fail), that's an "expected, not received."
If we get a CRC fail at the predicted time, we attribute it to
that advertiser.

This is the third pie slice ("expected"). It requires:

  * Per-device cadence estimator: rolling median of inter-packet
    intervals, with confidence based on observation count + std.
  * Per-device "next expected" timestamp computed from cadence.
  * Per-sniffer schedule check: was sniffer X on the right channel
    during the predicted ±2σ window?
  * If yes + no packet = "expected miss." If yes + CRC fail at
    that channel = "expected, received-corrupt."

Complexity is real — and the cadence estimator needs to handle
the packets table being populated (currently empty per
deep-dive §1c). Realistic effort: 8-12 hours including a
calibration pass against your live data to set thresholds.

### 2c. Combined approach

For the most accurate per-device numbers:

  * Use 2a (diversity) for *spontaneous* CRC-fail attribution.
  * Use 2b (timing) for *expected misses*.
  * Pie chart slices come out of:
      good          = clean packets attributed by address
      bad           = CRC fails attributed via sniffer diversity
      expected miss = predicted arrivals with no observed packet

## What this PR delivers vs. the full ask

| User ask | This PR | Future work |
|---|---|---|
| Distinct flash for CRC-fail | ✅ on the **sniffer panel** dot | DeviceItem flash variant once attribution is solved (§2a/2b) |
| Per-device good/bad counts | ❌ — sniffer-level only | Needs §2a sniffer-diversity inference |
| Per-device pie chart | ❌ | Needs §2a + §2b combined |
| Total dropouts toggle (count ↔ %) | ❌ | UI change once per-device counts exist |

The sniffer-level counters in this PR are the foundation. Once
the data is reliably populated, the same UI patterns map cleanly
to per-device displays.

## Recommended next step

Implement §2a (sniffer-diversity inference) first. It needs:

  * A small recent-packets cache per sniffer keyed by
    `(channel, ts_microsecond_bucket)`.
  * A lookup pass when a CRC fail arrives.
  * A new `pkt.attributed_device_id` field on the CRC-fail path.
  * Per-device good/bad counters on `DeviceItem`.
  * A small pie-chart paint helper for the expanded device box.

This unblocks the per-device pie chart's good/bad halves
immediately. The "expected" slice waits for §2b — and §2b also
gates on the packets table being populated, which is its own
trade-off (per deep-dive §1c).

## Visualization choice notes

The "dropout" flash uses near-black (`QColor(40, 40, 50)`) plus
random 1-2 white speckle pixels inside the dot. The speckle is a
clear visual cue that the packet was *received but corrupted*
rather than just rendered dark. Other candidates considered:

  * **Pure white-noise flash.** Would conflict with the channel-
    tag column's blue active highlight. Rejected.
  * **Red flash.** Already conflicts with the activity dot's
    "going inactive" gray and the canvas's various error-state
    reds. Rejected.
  * **Magenta / pink.** Reads as "alert" but doesn't communicate
    "dropout" specifically. Considered.
  * **Black flash with speckle.** Reads as "static / signal lost"
    intuitively. The speckle pattern reinforces "bit errors in
    the payload." **Chosen.**

If you decide a different palette is better after seeing it live,
the constants `_DOT_FLASH_CRC_FAIL` and `_CRC_FAIL_NOISE_PROB` in
`sniffer_panel.py` are the only knobs.
