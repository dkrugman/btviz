# Per-Class Profiles

A `ClassProfile` declares which signals matter for a device class,
their relative weights, which are required (without them no decision
is possible), and the score threshold above which a merge is
performed.

```python
@dataclass(frozen=True)
class ClassProfile:
    name: str                   # e.g. "airtag", "iphone", "hearing_aid"
    weights: dict[str, float]   # signal name -> weight (need not sum to 1)
    required: set[str]          # signals that must produce a non-None score
    threshold: float            # min weighted-mean to declare a merge
    params: dict[str, dict]     # per-signal parameter overrides
```

Profiles are stored as TOML in `src/btviz/cluster/profiles/` and
loaded at startup. They can be tuned without code changes.

## Profile-selection logic

```
def pick_profile(dev_a, dev_b) -> ClassProfile | None:
    if dev_a.class != dev_b.class:
        return None  # cross-class pairs are never merged automatically
    return PROFILES.get(dev_a.class) or PROFILES["default"]
```

Cross-class merges (e.g. "this AirTag and this iPhone are the same
physical thing") are forbidden by construction. They wouldn't be
meaningful anyway: the device class is itself an inferred property
based on the kinds of advertisements seen, and an iPhone never emits
AirTag-OF advertisements.

If a device is unclassified, fall through to `default` which uses
only universally-applicable signals.

## The profiles

### `airtag`

```toml
[airtag]
threshold = 0.70
required = ["rotation_cohort"]

[airtag.weights]
rotation_cohort   = 0.45
rssi_signature    = 0.25
apple_continuity  = 0.10
status_byte_match = 0.10
adv_interval      = 0.05
pdu_distribution  = 0.05

[airtag.params.rotation_cohort]
expected_rotation = 900.0
window_min        = 0.05
window_max        = 60.0

[airtag.params.rssi_signature]
min_sniffers      = 2
std_floor         = 1.5
z_full_mismatch   = 4.0
recent_window     = 30.0

[airtag.params.apple_continuity]
airtag_set_match_score = 0.40
```

**Rationale.** AirTags reveal the bare minimum: just an EC public key
plus a status byte. The OF protocol is engineered to make passive
linking impossible, so the only signals that work are temporal
(`rotation_cohort`) and spatial (`rssi_signature`), corroborated by
the deliberately-weak `apple_continuity` (sub-type 0x12 matches
itself; doesn't tell us *which* AirTag) and `status_byte_match` for
clean handoffs. `rotation_cohort` is required because without it we'd
be guessing — RSSI alone can't distinguish co-located AirTags. The
threshold is 0.70 (relatively low) because the signals available are
all somewhat weak individually, so we accept lower per-signal
confidence in exchange for combining many.

### `iphone`

```toml
[iphone]
threshold = 0.75
required = []   # no single signal is required

[iphone.weights]
apple_continuity  = 0.40
rssi_signature    = 0.25
rotation_cohort   = 0.15
mfg_data_prefix   = 0.10
adv_interval      = 0.05
pdu_distribution  = 0.05

[iphone.params.rotation_cohort]
expected_rotation = 900.0
window_min        = 0.05
window_max        = 180.0

[iphone.params.adv_interval]
ppm_full_mismatch = 400.0
```

**Rationale.** iPhones leak more than AirTags. The Continuity payload
is rich (Nearby + Handoff + AirDrop + AirPlay are all common combos);
two RPAs from the same phone usually share an exact Continuity
sub-type set including unusual sub-types. So `apple_continuity` is
the heaviest weight. `rotation_cohort` is *not* required for
iPhones because the rotation cadence is more variable (power-save
modes interrupt it) so demanding a clean rotation handoff misses
real same-device pairs.

### `airpods`

```toml
[airpods]
threshold = 0.75
required = ["apple_continuity"]   # without 0x07 sub-type, this isn't AirPods

[airpods.weights]
apple_continuity  = 0.50
rssi_signature    = 0.20
rotation_cohort   = 0.15
mfg_data_prefix   = 0.10
adv_interval      = 0.05

[airpods.params.rotation_cohort]
expected_rotation = 900.0
window_min        = 0.05
window_max        = 600.0   # AirPods drop adverts in low-duty mode

[airpods.params.rssi_signature]
min_sniffers      = 2
std_floor         = 2.5      # ear motion adds noise
recent_window     = 60.0
```

**Rationale.** AirPods broadcast a 0x07 sub-type whose internal
payload encodes which earbud (left/right/case) is communicating
plus battery state. The earbuds + case form a *single physical
device* but emit from three RPAs; the inner-payload analysis
distinguishes them. `apple_continuity` is required because without
0x07 we can't even confirm it's AirPods. `rssi_signature` has a
larger `std_floor` because the earbuds move with the head while
the case is stationary — within-device RSSI variance is naturally
high.

### `hearing_aid`

```toml
[hearing_aid]
threshold = 0.75
required = ["service_uuid_match"]

[hearing_aid.weights]
service_uuid_match = 0.40
rssi_signature     = 0.25
mfg_data_prefix    = 0.15
adv_interval       = 0.10
tx_power_match     = 0.05
pdu_distribution   = 0.05

[hearing_aid.params.service_uuid_match]
dispositive_min_uuids = 2

[hearing_aid.params.rssi_signature]
min_sniffers      = 1     # often only the user's body sniffer hears them
std_floor         = 1.5
recent_window     = 120.0  # adverts are slow

[hearing_aid.params.adv_interval]
min_packets         = 100
ppm_full_mismatch   = 100.0  # TCXO-grade clocks
```

**Rationale.** LE-Audio hearing aids advertise stable service UUID
sets (HAS / VCS / CAS / ASCS) — typically using random_static
addresses (no rotation). The clustering problem here is *less*
about RPA collapse and *more* about distinguishing left/right
buds of one set vs different users' aids in the same room.
`service_uuid_match` is required and dispositive when matching;
the other signals separate co-located pairs. Note: most of the
work here applies to bonded-device pairing scenarios that we
don't sniff (encrypted ASCS streams) — the framework is still
useful for the connectable-mode advertisements emitted while
unbonded.

### `find_my_accessory`

```toml
[find_my_accessory]
threshold = 0.70
required = ["rotation_cohort"]

[find_my_accessory.weights]
rotation_cohort   = 0.40
rssi_signature    = 0.25
apple_continuity  = 0.15
mfg_data_prefix   = 0.10
adv_interval      = 0.05
pdu_distribution  = 0.05
```

**Rationale.** Third-party Find My accessories (Chipolo, Pebblebee,
eufy, etc.) implement Apple's OF spec but often with looser timing
and slightly different mfg_data layouts. Same threshold as AirTag,
with mfg_data_prefix elevated since these vendors often leak more
than Apple does (firmware version, hardware revision). Re-uses
AirTag tuning otherwise.

### `wearable`

```toml
[wearable]
threshold = 0.80
required = ["service_uuid_match"]

[wearable.weights]
service_uuid_match = 0.35
mfg_data_prefix    = 0.25
adv_interval       = 0.15
rssi_signature     = 0.15
tx_power_match     = 0.05
pdu_distribution   = 0.05
```

**Rationale.** Fitness trackers (Garmin, Fitbit, Polar, Whoop,
Oura) advertise both stable service UUIDs *and* rich mfg_data
(serial fragments, firmware version). They typically use
random_static — no rotation — so the temporal signals are
useless. Higher threshold because users often own multiples of
similar devices (e.g. two Whoops, his/hers).

### `default`

```toml
[default]
threshold = 0.85
required = []

[default.weights]
service_uuid_match = 0.30
mfg_data_prefix    = 0.25
adv_interval       = 0.15
rssi_signature     = 0.15
pdu_distribution   = 0.10
tx_power_match     = 0.05
```

**Rationale.** When the device class is unknown, use only the
device-class-agnostic signals and demand a high threshold (0.85).
Better to leave devices un-merged than to merge wrongly. As the
classifier improves, this fallback will see less use.

## Relationship to `irk_resolution`

`irk_resolution` is **not** in any profile's `weights` — it short-
circuits the aggregator entirely (see `03_aggregator.md`). It does
not need to be weighted because its score is always either 1.0 or
None — there's nothing to combine.

## Tuning workflow

1. Run capture for a known scenario (e.g. user's own iPhone +
   AirTag + AirPods, with IRKs imported so we know ground truth).
2. The aggregator writes per-pair contributions to `device_clusters`.
3. Compare against the IRK ground truth: which signals fired
   correctly, which gave wrong opinions, which abstained when
   they shouldn't have?
4. Adjust weights and thresholds. Re-run.
5. Repeat across users / scenarios; profiles converge.

The contributions JSON in `device_clusters` is the audit trail —
without it, weight tuning would be blind.
