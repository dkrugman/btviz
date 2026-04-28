# Signal Specifications

Each signal is a self-contained module exposing the same protocol:

```python
class Signal(Protocol):
    name: str
    def applies_to(self, ctx: ClusterContext, a: Device, b: Device) -> bool: ...
    def score(self, ctx: ClusterContext, a: Device, b: Device) -> float | None: ...
```

`applies_to` returns False when the signal is structurally inapplicable
(wrong device class, missing data, addresses of incompatible kinds).
`score` returns either a float in `[0.0, 1.0]` indicating the
probability that `a` and `b` are the same physical device, or `None`
when the signal has *no opinion* (data sparse, ambiguous). The
aggregator distinguishes these: `applies_to=False` excludes the signal
from the weighted sum; `score=None` excludes it but is allowed to
abort the decision if the signal was listed in `profile.required`.

Below: every signal in the plan, ordered by implementation priority
(easiest + biggest immediate win first).

---

## 1. `rotation_cohort` — temporal disappearance/appearance pairing

**Intent.** RPA-rotating devices disappear and re-appear on a cadence.
A phone rotates roughly every 15 minutes (Apple's documented behavior;
in practice 7–17 min observed in our captures); an AirTag rotates
exactly every 15 min (Heinrich et al. § 4.2). When RPA `A` vanishes
and a new RPA `B` appears within a small temporal window on the same
sniffer at the same RSSI, that's evidence they're the same physical
device handing off identity.

**Inputs.** `packets` table, columns `(device_id, ts, rssi,
sniffer_short_id)`. Specifically: the last-seen timestamp of `a` (call
it `t_a_last`), and the first-seen timestamp of `b` (`t_b_first`),
both restricted to the *same* sniffer.

**Score function.**

```
gap = t_b_first - t_a_last
if gap < 0:           # b appeared before a vanished — overlapping, not handoff
    return None
if gap > params.window_max:
    return 0.0        # too long — different device
if gap < params.window_min:
    return 0.5        # suspiciously fast — could be coincidence
# gap is in the plausible range — score peaks at expected_rotation
delta = abs(gap - params.expected_rotation)
return max(0.0, 1.0 - delta / params.expected_rotation)
```

**Tunable parameters per class.**

| param | airtag | iphone | airpods |
|---|---|---|---|
| `expected_rotation` | 900 s | 900 s | 900 s (variable) |
| `window_min` | 0.05 s | 0.05 s | 0.05 s |
| `window_max` | 60 s | 180 s | 600 s |

`window_max` accounts for transmit-gap drift: AirTags rotate
*atomically* (last old-RPA packet to first new-RPA packet within ~120
ms in the OpenHaystack reference); iPhones may have longer gaps when
the radio is idle for power-save; AirPods can be in a low-duty mode
between adverts.

**Failure modes.**

- *Multiple devices rotating concurrently* — if two iPhones both
  rotate within the same window on the same sniffer, this signal
  pairs them randomly. Mitigation: combine with `rssi_signature` so
  same-RSSI gets weight, different-RSSI is rejected.
- *Coverage gaps* — sniffer drops some packets. `t_a_last` is
  underestimated, `gap` looks larger than it is. Effect: false
  negatives (we miss real handoffs). Acceptable; we can re-process
  later.
- *Static-address devices* — does not apply; `applies_to` returns
  False when either address is `random_static` or `public`.

**`applies_to` rules.**

- Both `a.address.kind == 'random_resolvable'` (RPA).
- At least one observation of each on the same sniffer.

**Cost.** O(1) per pair given indexed `packets(device_id, ts)`. The
expensive part is the candidate-generation step that decides which
pairs are worth scoring at all (see `03_aggregator.md`).

---

## 2. `rssi_signature` — multi-sniffer RSSI vector match

**Intent.** With 3 sniffers in different positions, every transmission
produces a 3-tuple `(rssi_1, rssi_2, rssi_3)`. Path loss is roughly
constant per (transmitter position, sniffer position) pair, so a
device sitting in one place has a stable RSSI signature. Two RPAs
with the same signature, observed close in time, are very likely the
same physical device.

**Inputs.** `packets(device_id, ts, rssi, sniffer_short_id)`. For
each device, compute the recent per-sniffer RSSI distribution (mean +
std-dev over the last N seconds).

**Score function.**

```
# build per-sniffer RSSI distributions for both devices
sigs_a = {sniffer: (mean, std) for ...}
sigs_b = {sniffer: (mean, std) for ...}
common = sigs_a.keys() & sigs_b.keys()
if len(common) < params.min_sniffers:
    return None
# z-score distance averaged across shared sniffers
z = mean(
    abs(sigs_a[s].mean - sigs_b[s].mean) /
    max(sigs_a[s].std + sigs_b[s].std, params.std_floor)
    for s in common
)
# z=0 → identical, z=4 → very different
return max(0.0, 1.0 - z / params.z_full_mismatch)
```

**Tunable parameters per class.**

| param | airtag | iphone | hearing_aid |
|---|---|---|---|
| `min_sniffers` | 2 | 2 | 1 |
| `std_floor` | 1.5 dB | 2.0 dB | 1.5 dB |
| `z_full_mismatch` | 4.0 | 5.0 | 4.0 |
| `recent_window` | 30 s | 60 s | 120 s |

Hearing aids run `min_sniffers=1` because they're often only heard
from the user's body-mounted sniffer; the body-shadow itself is part
of the signature.

**Failure modes.**

- *Moving devices.* A walking phone has high std on every sniffer; the
  `z_full_mismatch` saturation catches that as "no opinion."
  Implementation: when std exceeds a threshold, return `None` not 0.
- *Single-sniffer captures.* `min_sniffers=2` returns `None`. By
  design — without spatial diversity, RSSI alone is too noisy.
- *Co-located devices.* Two AirTags side-by-side will match; this
  signal alone can't separate them. Combined with `rotation_cohort`
  (different rotation phases) and `apple_continuity` (different OF
  status bytes) the aggregator can usually disambiguate.

**Cost.** O(n_packets * n_sniffers) per device for signature building.
Cache per-device signatures; rebuild when last-seen advances by > 5 s.

---

## 3. `adv_interval` — broadcaster crystal fingerprint

**Intent.** BLE advertisement intervals are software-controlled but
clocked off the chip's 32-kHz crystal. Two devices configured for the
same nominal interval (e.g. 100 ms) will tick at slightly different
rates — typically ±50 ppm for spec-compliant crystals. Over a few
hundred packets the actual mean interval is measurable to sub-ppm
precision, which gives a per-device hardware fingerprint.

**Inputs.** `packets(device_id, ts)` ordered by `ts`. Compute pairwise
deltas between consecutive packets on the same device; reject deltas
that are integer multiples of the nominal interval (missed packets);
take the mean of the remainder.

**Score function.**

```
mean_a = compute_mean_interval(a, params.min_packets)
mean_b = compute_mean_interval(b, params.min_packets)
if mean_a is None or mean_b is None:
    return None  # too few packets to measure
ppm_diff = abs(mean_a - mean_b) / mean_a * 1e6
# 0 ppm → identical clock → very likely same device
# 100 ppm → at the edge of crystal tolerance → could still be same
# 500 ppm → definitely different chips
return max(0.0, 1.0 - ppm_diff / params.ppm_full_mismatch)
```

**Tunable parameters per class.**

| param | airtag | iphone | hearing_aid |
|---|---|---|---|
| `min_packets` | 50 | 30 | 100 |
| `ppm_full_mismatch` | 200 | 400 | 100 |

Hearing aids tend to use TCXO-grade clocks (±1 ppm) so we can be
much stricter; AirTags use cheaper crystals; phones use whatever
the SoC gives them, plus they may pause adverts under various
power-save conditions which inflates measurement noise.

**Failure modes.**

- *Different nominal intervals.* The signal is only meaningful when
  both devices target the same nominal interval. `applies_to` checks
  that the *quantized* interval (rounded to nearest 1.25 ms BLE slot)
  matches; if not, returns False (this signal expresses nothing).
- *Power-save interruptions.* Phones can pause and resume advertising
  with a phase shift; that single discontinuity will inflate
  `mean_interval`. Mitigation: trim outliers > 3σ from the delta
  distribution before averaging.
- *Spec-compliant random jitter.* BLE 5.0 mandates 0–10 ms random
  jitter on each advert to avoid systematic collisions. This is
  zero-mean over many packets, so it averages out — but it requires
  `min_packets ≥ 30` for the mean to converge.

**Cost.** O(n_packets) per device for interval calculation; cacheable.

---

## 4. `service_uuid_match` — advertised service-UUID list

**Intent.** Many non-Apple devices advertise a stable list of service
UUIDs (e.g. hearing aids advertise HAS / VCS / CAS / ASCS UUIDs;
heart-rate monitors advertise 0x180D; etc.). The list is part of the
device firmware, not part of the privacy-rotation envelope, so two
RPAs from one device share it.

**Inputs.** `device_ad_history(device_id, ad_type=0x02|0x03|0x06|
0x07, ad_value)`. AD types 0x02/0x03 are 16-bit complete/incomplete
service UUID lists; 0x06/0x07 are 128-bit. Build a set per device.

**Score function.**

```
set_a = service_uuids(a)
set_b = service_uuids(b)
if not set_a or not set_b:
    return None
# Jaccard similarity, weighted toward exact match
intersect = len(set_a & set_b)
union = len(set_a | set_b)
jaccard = intersect / union if union else 0.0
# A perfect match on a non-trivial set is dispositive
if jaccard == 1.0 and len(set_a) >= params.dispositive_min_uuids:
    return 1.0
return jaccard
```

**Tunable parameters per class.**

| param | hearing_aid | wearable | beacon |
|---|---|---|---|
| `dispositive_min_uuids` | 2 | 2 | 3 |

Hearing aids: 2 UUIDs (e.g. ASCS+HAS) is already enough to say
"definitely the same model in the same configuration." Beacons
require 3 because most beacon protocols advertise a single common
UUID (Eddystone, iBeacon) shared across vendors.

**Failure modes.**

- *Apple devices.* Apple privacy policy: don't advertise raw service
  UUIDs in the connectable advertisement; instead use Continuity
  encoded in manufacturer data. This signal returns `None` (no UUIDs
  observed). Profile selection must steer Apple devices to
  `apple_continuity` instead.
- *Multiple identical-model devices.* Two of the same hearing-aid
  model will have identical UUID sets; this signal alone can't
  distinguish them. Combined with `rssi_signature` and
  `mfg_data_prefix` (which often encodes a serial in the high bytes)
  the aggregator can.
- *Truncation.* `incomplete-list` (0x02, 0x06) AD types mean the
  device wanted to advertise more UUIDs than fit; the observed set is
  a subset. Mitigation: use only `complete-list` (0x03, 0x07) when
  available; fall back to incomplete only for hint matching.

**Cost.** O(1) given indexed `device_ad_history(device_id, ad_type)`.
Set comparison is trivial.

---

## 5. `mfg_data_prefix` — manufacturer-data leading bytes

**Intent.** AD type 0xFF (manufacturer-specific data) starts with a
2-byte LE company ID followed by vendor-defined payload. The vendor
prefix often encodes a stable per-device fingerprint: serial fragment,
firmware version, hardware revision, capability flags. Apple
Continuity (company 0x004C) is the most-mined example, but many
non-Apple vendors use the same trick (Garmin, Fitbit, Polar).

**Inputs.** `device_ad_history(device_id, ad_type=0xFF, ad_value)`.
For each device, get the most-frequent prefix at length N.

**Score function.**

```
# Try matching at multiple prefix lengths; pick the longest exact match.
for L in [params.max_prefix, params.max_prefix - 4, ..., params.min_prefix]:
    pref_a = top_prefix(a, L)
    pref_b = top_prefix(b, L)
    if pref_a is None or pref_b is None:
        continue
    if pref_a == pref_b:
        # longer match → more bits of evidence → higher score
        return min(1.0, L / params.full_match_length)
return None
```

**Tunable parameters per class.**

| param | iphone | hearing_aid | airtag |
|---|---|---|---|
| `min_prefix` | 4 | 4 | 2 |
| `max_prefix` | 16 | 16 | 4 |
| `full_match_length` | 12 | 8 | — |

AirTag-OF prefix is only 2 bytes (0x4C 0x12) before the rotating EC
key, so this signal saturates quickly for AirTags and contributes
little — by design (status_byte_match takes over).

**Failure modes.**

- *Rolling counters.* Some devices include a monotonic counter in the
  first 4 bytes of mfg_data; the prefix changes every advertisement.
  Mitigation: take the mode (most-frequent) at each length, not the
  raw bytes. If even the mode rotates faster than the RPA, this
  signal returns `None`.
- *Empty mfg_data.* Some hearing aids don't advertise 0xFF at all.
  `applies_to` returns False.

**Cost.** O(unique_prefixes_per_device); typically tiny.

---

## 6. `apple_continuity` — Continuity sub-type sequence match

**Intent.** Apple's manufacturer-data encoding (company 0x004C) carries
a tag-length-value sequence of "Continuity sub-types." Each sub-type
identifies a specific Apple feature: 0x07 = AirPods; 0x09 = AirPlay
target; 0x10 = Nearby (every iPhone 6+); 0x12 = Find My; 0x05 =
AirDrop hash; 0x07/0x0F = Handoff. The *combination* of sub-types
present, plus their internal payloads, is a high-fidelity device-class
fingerprint.

**Inputs.** Decoded Continuity payloads from `device_ad_history` (or
inline if a Continuity decoder is loaded). Catalog source:
FuriousMAC continuity database.

**Score function.**

```
sigs_a = continuity_signature(a)  # set of sub-type IDs present
sigs_b = continuity_signature(b)
if not sigs_a or not sigs_b:
    return None
# Exact match on the sub-type set is strong evidence
if sigs_a == sigs_b:
    # If both are AirTag-OF (sub-type 0x12), require deeper match
    if sigs_a == {0x12}:
        return params.airtag_set_match_score  # weak — need other signals
    if len(sigs_a) >= 2:
        return 1.0
    return 0.85
# Asymmetric — one is a strict subset of the other
if sigs_a < sigs_b or sigs_b < sigs_a:
    return 0.5
return 0.0
```

**Tunable parameters per class.**

| param | airtag | iphone | airpods |
|---|---|---|---|
| `airtag_set_match_score` | 0.40 | — | — |

Why 0.40 for AirTags? Every AirTag in the room emits Continuity
0x12 with the *same* sub-type but *different* EC keys. The set-match
alone tells you it's an AirTag, not which AirTag — so this signal is
deliberately weak for that case and the aggregator leans on
`rotation_cohort` + `rssi_signature` + `status_byte_match`.

**Failure modes.**

- *Sub-type 0x10 ubiquity.* Every iPhone 6+ emits 0x10 (Nearby).
  Two strangers' iPhones in a coffee shop will match on the
  sub-type set. Mitigation: lookup the inner payload of 0x10
  (a 3-byte status field) and require *that* to match too — but the
  status field changes with phone state (locked/unlocked/Wi-Fi state)
  so this is itself probabilistic.
- *Continuity decoder out of date.* Apple adds sub-types regularly;
  unknown ones reduce comparison fidelity. Mitigation: include
  unknown sub-type IDs in the set verbatim; an unknown 0xFF tag
  matches itself.

**Cost.** O(advert_count) for first decode; O(1) thereafter (cached).

---

## 7. `tx_power_match` — advertised TX power level

**Intent.** AD type 0x0A is "TX Power Level," a single signed byte
giving the radio output in dBm. It is set by firmware and is
hardware-stable; it changes only across firmware updates. Two RPAs
with the same advertised TX power are *consistent* with being the
same device but on its own this is weak (most devices advertise -4
or 0 dBm; very few unique values).

**Inputs.** `device_ad_history(device_id, ad_type=0x0A)`.

**Score function.**

```
tx_a = tx_power(a)
tx_b = tx_power(b)
if tx_a is None or tx_b is None:
    return None
if tx_a == tx_b:
    # rare values are stronger evidence than common ones
    rarity = 1.0 - corpus_frequency(tx_a)
    return params.match_base_score + rarity * (1.0 - params.match_base_score)
return 0.0
```

**Tunable parameters per class.**

| param | all classes |
|---|---|
| `match_base_score` | 0.30 |

Base 0.30 because every device with `tx=-4 dBm` (a very common value)
matches every other; the rarity term rewards unusual values.

**Failure modes.**

- *Most devices use -4 dBm or 0 dBm.* This signal contributes little
  in practice — by design, low weight in profiles. Useful only as a
  *negative* signal: mismatched TX power is moderate evidence that
  two RPAs are different devices.
- *Devices that don't advertise it.* AirTags don't include AD type
  0x0A. `applies_to` returns False.

**Cost.** O(1).

---

## 8. `status_byte_match` — Find My status field continuity

**Intent.** The Apple OF advertisement (28 bytes after the 0x004C
prefix) ends with a 1-byte status field encoding battery level,
hash-of-public-key tail, and "lost mode" state. This byte is rolled
forward by the AirTag's firmware in a deterministic way: it does not
randomize across rotations. So a sequence of (timestamp, status)
points across multiple RPAs from the same physical AirTag should form
a *coherent trajectory* (slowly draining battery, occasional state
flips) — whereas different AirTags have independent trajectories.

**Inputs.** Decoded OF status byte from per-packet records, indexed by
`(device_id, ts)`.

**Score function.**

```
# Get the status sequence for each over the last few rotations
seq_a = status_sequence(a, params.lookback)
seq_b = status_sequence(b, params.lookback)
if len(seq_a) < params.min_packets or len(seq_b) < params.min_packets:
    return None
# If a's last status matches b's first status, that's a clean handoff.
gap = seq_b[0].ts - seq_a[-1].ts
if 0 < gap < params.handoff_window:
    if seq_a[-1].status == seq_b[0].status:
        return 1.0
    # battery may have ticked one notch
    if abs(status_battery(seq_a[-1]) - status_battery(seq_b[0])) <= 1:
        return 0.85
    return 0.0
return None  # not a handoff candidate temporally — this signal abstains
```

**Tunable parameters per class.**

| param | airtag |
|---|---|
| `handoff_window` | 30 s |
| `lookback` | 1800 s |
| `min_packets` | 5 |

**Failure modes.**

- *Battery ticks during the rotation window.* The status byte's
  battery field is 2 bits — four levels — so it ticks rarely. The
  ±1-step tolerance covers this.
- *Lost-mode flip.* If the owner switches the tag to lost mode
  between rotations, the status byte changes a lot. Mitigation:
  decode the status field properly and only require the *non-state*
  bits to match.
- *Non-Apple devices.* `applies_to` returns False.

**Cost.** O(lookback / advert_interval) per device — at 2 s adverts
and 30-min lookback, ~900 status bytes per device. Trivial.

---

## 9. `pdu_distribution` — PDU-type histogram

**Intent.** The BLE advertising PDU type (`ADV_IND`, `ADV_NONCONN_IND`,
`ADV_DIRECT_IND`, `ADV_SCAN_IND`, `SCAN_RSP`, `ADV_EXT_IND`,
`AUX_ADV_IND`) is part of the firmware behavior and stable across
RPA rotations. The *distribution* of PDU types per device — e.g.
"73% ADV_NONCONN_IND, 27% ADV_IND" — is a moderate fingerprint.

**Inputs.** `packets(device_id, pdu_type)`. Build a histogram per
device.

**Score function.**

```
hist_a = pdu_histogram(a, normalized=True)
hist_b = pdu_histogram(b, normalized=True)
if min(sum(hist_a.values()), sum(hist_b.values())) < params.min_packets:
    return None
# Total variation distance: 0=identical, 1=disjoint
tvd = 0.5 * sum(abs(hist_a.get(k, 0) - hist_b.get(k, 0))
                for k in hist_a.keys() | hist_b.keys())
return max(0.0, 1.0 - tvd / params.tvd_full_mismatch)
```

**Tunable parameters per class.**

| param | all classes |
|---|---|
| `min_packets` | 50 |
| `tvd_full_mismatch` | 0.5 |

**Failure modes.**

- *Single-PDU devices.* AirTags emit only `ADV_NONCONN_IND` —
  every AirTag has identical PDU distribution `{0x02: 1.0}`. The
  signal saturates at 1.0 for every AirTag pair, so it contributes
  no *discriminating* power. Effectively, this signal returns 1.0
  for AirTags but the profile assigns it weight 0 — by design.
- *Connected vs scanning.* When a device is in a connection, it
  stops advertising. Histogram becomes truncated. Mitigation: build
  the histogram over a fixed *recent* window (e.g. 60 s of recent
  activity rather than all-time) so connected periods don't
  permanently distort the distribution.

**Cost.** O(unique_pdu_types) per device; cacheable.

---

## 10. `irk_resolution` — cryptographic IRK match

**Intent.** When the user provides the IRK (Identity Resolving Key) of
a known device — typically obtained from the device's pairing or
from `defaults read /Library/Preferences/com.apple.Bluetooth` on
their own Mac — every RPA can be tested against it via AES-128. If
the RPA verifies, it cryptographically resolves to that identity.
This is the *only* signal that gives certainty; all others are
probabilistic.

**Inputs.** A list of `(identity_label, irk)` pairs from
`btviz.identities` (a new table). The candidate device's last-seen
random_resolvable address bytes.

**Score function.**

```
# RPA structure: [3-byte hash || 3-byte prand].
# The high 2 bits of prand[0] must be 0b01 for a resolvable address.
addr = a.address.bytes
prand = addr[0:3]
hash_observed = addr[3:6]
for ident in ctx.irks:
    # AES-128: compute hash(prand) under each IRK; compare.
    hash_computed = aes128(ident.irk, b'\x00'*13 + prand)[-3:]
    if hash_computed == hash_observed:
        # a is identity ident. Now check b.
        if matches_same_identity(b, ident):
            return 1.0   # cryptographic match
        else:
            return 0.0   # a resolves to ident, b does not — different
return None  # no IRK matches a — no opinion (other signals can decide)
```

**Tunable parameters.** None — cryptography is not a knob.

**Failure modes.**

- *No IRKs imported.* Returns `None` for every pair. The aggregator
  treats this signal as absent and falls back to behavioral signals.
- *Wrong IRK provided.* Hash will not match; returns `None`. Failed
  matches are not logged as 0.0 — that would be unfair to the
  device, since absence of a key isn't evidence of difference.
- *Non-resolvable address kinds.* `applies_to` returns False for
  `random_static`, `public`, `random_non_resolvable`.

**Cost.** O(n_irks) AES-128 ops per resolved RPA — at <100 IRKs
imported, microseconds. The naive implementation tests every IRK
against every unresolved RPA on every cluster check; better: cache
the resolution result on the address row (`addresses.resolved_via_irk
_id`), so subsequent checks are O(1).

---

## Signal cross-summary

| Signal | Apple | non-Apple stable | non-Apple rotating | beacon | hearing aid |
|---|---|---|---|---|---|
| rotation_cohort | strong | — | strong | — | — |
| rssi_signature | strong | strong | strong | medium | strong |
| adv_interval | medium | strong | medium | strong | strong |
| service_uuid_match | — | strong | strong | weak | dispositive |
| mfg_data_prefix | medium | strong | medium | weak | strong |
| apple_continuity | dispositive | — | — | — | — |
| tx_power_match | weak | weak | weak | weak | weak |
| status_byte_match | dispositive (AirTag) | — | — | — | — |
| pdu_distribution | weak | medium | medium | medium | medium |
| irk_resolution | dispositive (own) | dispositive (own) | dispositive (own) | — | dispositive (own) |

"strong" = positive contribution worth a heavy weight; "medium" =
useful corroboration; "weak" = include only as a tie-breaker;
"dispositive" = if it fires, ignore the rest.

This matrix becomes the basis for `02_class_profiles.md`.
