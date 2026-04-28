# RPA Collapse — Architecture Overview

The post-CRC problem. After bit-error corruption is filtered (DLT-272
flags-byte fix that landed in main), the canvas still shows multiple
device rows that are really one physical device — phones rotate RPAs
every ~15 min, AirTags rotate Find-My keys every 15 min, AirPods
broadcast multiple advertisement contexts, etc. The cryptography is
specifically designed to make these unlinkable from passive
observation. We can never get cryptographic certainty without owning
the device's IRK; we can get *high probability* from behavioral
signals.

This document is the architecture for a pluggable framework that:

1. Computes pairwise similarity between candidate device rows along
   multiple independent signals (rotation timing, RSSI signature,
   manufacturer-data prefix, service UUIDs, …).
2. Combines those signals into a single confidence score using a
   per-device-class profile (AirTags weight rotation_cohort high;
   hearing aids weight service UUIDs high; iPhones weight
   apple_continuity sub-types high; etc.).
3. Decides whether to merge based on a threshold, writing the result
   to a `device_clusters` table.
4. Surfaces the merged identity in the canvas, with manual override.

## Why a framework, not one big algorithm

Three reasons:

1. **Different device classes need different evidence.** AirTags
   reveal nothing in their advertisement except an EC public key plus
   a 1-byte status field — rotation timing and RSSI signature are
   essentially the only passive signals. Hearing aids advertise
   stable service UUID lists (HAS, VCS, CAS, ASCS) and TX power, and
   often use random_static addresses (no rotation), so the evidence
   profile is completely different. Forcing one algorithm to handle
   both is a recipe for a brittle, parameter-overfit blob.

2. **Signals improve independently.** When better data lands (per-
   packet RSSI vector across sniffers, full AD-entry history,
   AdvDataInfo-paired primary↔AUX), each signal that consumes that
   data improves without touching the others. New signals plug in.

3. **Explainability.** When a merge happens, we want to be able to
   tell the user *why*: "merged because RPA A vanished within 180 ms
   of RPA B appearing on the same sniffer at RSSI -54±2 dBm, plus
   matching Apple Continuity sub-type 0x07 (AirPods) sequence." If
   the algorithm is one black box, "we merged these" is the only
   possible explanation. With a framework, every signal contributes
   a score we can quote.

## The signals (full list, detailed in `01_signals.md`)

| Signal | Cost | Best for | Useless for |
|---|---|---|---|
| `rotation_cohort` | needs `packets` table | RPA-rotating devices | static addresses |
| `rssi_signature` | needs `packets` table + 2+ sniffers | concurrent observation | sequential captures |
| `adv_interval` | needs `packets` table | hardware fingerprint (±50 ppm) | same-model devices |
| `service_uuid_match` | needs `device_ad_history` | hearing aids, fitness trackers | Apple devices (privacy-rotated) |
| `mfg_data_prefix` | needs `device_ad_history` | per-vendor protocols | beacons w/ rolling payloads |
| `apple_continuity` | needs Continuity decoder + history | iPhones, AirPods, AirTags | non-Apple |
| `tx_power_match` | needs new AD-entry capture | every device that advertises 0x0A | devices that don't advertise it |
| `status_byte_match` | needs OF-packet decoder | AirTags / Find-My devices | non-OF |
| `pdu_distribution` | needs `packets` table | distinguishing connectable vs scannable | uniform devices |
| **`irk_resolution`** | needs IRK from user | owner's own devices (gold-standard signal) | strangers' devices |

Note that `irk_resolution` is special: it's the only cryptographic
signal. When it succeeds it returns 1.0 (cryptographic certainty);
when it can't (no IRK available, or AES doesn't verify), it returns
None. Other signals are probabilistic.

## The aggregator (detailed in `03_aggregator.md`)

Pseudocode:

```
def cluster_pair(ctx, dev_a, dev_b):
    profile = pick_profile(dev_a, dev_b)   # by device_class + kind
    if profile is None:
        return None  # no profile applies → no opinion

    # IRK is dispositive when it fires.
    irk_score = signals.irk_resolution.score(ctx, dev_a, dev_b)
    if irk_score is not None:
        return Decision(merge=True, score=1.0, signals={"irk": 1.0})

    weighted_sum, total_weight = 0, 0
    contributions = {}
    for sig_name, weight in profile.weights.items():
        sig = signals[sig_name]
        if not sig.applies_to(dev_a, dev_b):
            continue
        s = sig.score(ctx, dev_a, dev_b)
        if s is None:
            if sig_name in profile.required:
                return None  # data not available → can't decide
            continue
        weighted_sum += s * weight
        total_weight += weight
        contributions[sig_name] = (s, weight)

    if total_weight == 0:
        return None
    final = weighted_sum / total_weight
    return Decision(
        merge=(final >= profile.threshold),
        score=final,
        signals=contributions,
    )
```

Decisions are written to `device_clusters` with the contributions
JSON-serialized — useful for explainability and for tuning weights
later.

## Per-class profiles (detailed in `02_class_profiles.md`)

Profiles live as TOML in `src/btviz/cluster/profiles/`. The aggregator
picks one based on device kind + device_class. Examples:

```toml
# AirTag: relies almost entirely on temporal/spatial signals because
# the OF protocol leaks nothing else.
[airtag]
weights = { rotation_cohort = 0.45, rssi_signature = 0.25,
            apple_continuity = 0.15, status_byte = 0.10,
            adv_interval = 0.05 }
required = ["rotation_cohort"]
threshold = 0.70

[hearing_aid]
# random_static usually — no rotation. Service-UUID list is the killer.
weights = { service_uuid_match = 0.40, rssi_signature = 0.30,
            tx_power_match = 0.15, mfg_data_prefix = 0.10,
            adv_interval = 0.05 }
required = ["service_uuid_match"]
threshold = 0.75
```

Tunable per device class as data accumulates.

## Dependencies + implementation order

```
[1] Wide fingerprint schema migration   ── prereq for almost every signal
        ↓
[2] Skeleton: Signal protocol, profiles loader, aggregator   ── this branch
        ↓
[3] Concrete signals (one PR per signal, or batched)
    rotation_cohort        — easiest, biggest immediate win
    rssi_signature         — leverage 3 existing sniffers
    apple_continuity       — best for iPhones / AirTags / AirPods
    service_uuid_match     — best for hearing aids
    mfg_data_prefix
    tx_power_match
    status_byte_match
    adv_interval
    pdu_distribution
        ↓
[4] Aggregator + device_clusters table + UI
    Canvas: collapse cluster members under one box (with member list
    in the tooltip + expand-on-click)
    Right-click "Merge"/"Unmerge" overrides
        ↓
[5] IRK resolution as a special-case signal (cryptographic)
        ↓
[6] LLM oracle to identify cluster-by-fingerprint (separate model)
```

## What this branch produces

This branch is *only* steps 2 above + the doc tree. It's intentionally
not the full implementation — that's gated on schema migration which
is itself a chunky PR. What lands here:

- This documentation tree (`docs/rpa_collapse/`)
- `src/btviz/cluster/` skeleton:
  - `signals/base.py` — Signal protocol + ClusterContext
  - `signals/rotation_cohort.py` — first concrete signal (works on
    in-memory data; the DB-backed version waits for the schema PR)
  - `aggregator.py` — weighted-sum + decision struct
  - `profiles/` — initial TOML profiles
- Synthetic-data tests in `tests/cluster/` (or `/tmp/` if no test
  framework is set up yet) proving the framework wires together.

The intent is that the schema PR + the per-signal PRs can land
incrementally, each one adding measurable disambiguation power
without changing the framework.

## References

- Heinrich, Stute, Kornhuber, Hollick. *Who Can Find My Devices?
  Security and Privacy of Apple's Crowd-Sourced Bluetooth Location
  Tracking System.* PoPETs 2021.
  → Specifies the Find-My / AirTag protocol exactly. Confirms that
  the OF advertisement leaks only the EC public key + status byte,
  and that the master beacon key never appears OTA. Establishes that
  passive AirTag cluster collapse must be behavioral.

- Barman, Dumur, Pyrgelis, Hubaux. *Every Byte Matters: Traffic
  Analysis of Bluetooth Wearable Devices.* Proc. ACM IMWUT 5(2),
  Art. 54, June 2021.
  → Shows that even encrypted BLE leaks device model, app opens, and
  fine-grained user actions via packet-size + inter-arrival features.
  Same kinds of features we'll use for clustering. Worth being aware
  that our clustering tools and an adversarial tracking tool share
  signal pipelines.

- FuriousMAC continuity database. github.com/furiousMAC/continuity
  → Catalog of Apple Continuity sub-types and their semantics. Useful
  to enrich the `apple_continuity` signal: knowing that sub-type 0x07
  is AirPods and 0x10 is Nearby helps weight what counts as a match.

- WHAD (Wireless Hardware Abstraction Layer for Discovery). whad.io
  → Tooling for live BLE manipulation, including IRK-based RPA
  resolution. Useful as a reference implementation for the IRK
  signal, even though we wouldn't depend on it at runtime.
