# Critique of v1 Plan — Failure Modes and Refinements

This is a self-review of `00_overview.md` through `04_schema.md` written
to find problems *before* code is written. Each section names a
problem and proposes the v2 refinement.

## C1. The aggregator collapses too eagerly when signals abstain

**Problem.** In v1, if all signals return `None` except one weak signal
that scores 0.85 with weight 0.05, the weighted-mean is 0.85
(0.05*0.85 / 0.05). With threshold 0.75, this triggers a merge — based
on a *single weak signal*. That's wrong. A high score from a low-weight
signal should not be sufficient evidence on its own.

**Refinement.** Add a `min_total_weight` to the profile. If
`total_weight < profile.min_total_weight`, return `None` (no opinion)
regardless of the score. Default values:

| profile | min_total_weight |
|---|---|
| airtag | 0.50 |
| iphone | 0.50 |
| airpods | 0.50 |
| hearing_aid | 0.55 |
| wearable | 0.55 |
| find_my_accessory | 0.50 |
| default | 0.60 |

Phrased differently: "I need at least half the *intended* evidence to
have weighed in before I'll commit to a merge."

## C2. Required-but-absent isn't quite the right semantic

**Problem.** v1 says: a signal in `profile.required` that returns None
or has `applies_to=False` aborts the decision. But that's overly harsh
for some cases: e.g. for AirTags, `rotation_cohort` is required but a
fresh device with only 30s of capture won't yet have a rotation pair —
making it impossible for the framework to ever cluster newly-seen
devices, even when the other signals are conclusive.

**Refinement.** Distinguish two kinds of "required":

- `required_eventually`: must produce a non-None score *eventually*.
  When all members of this set return None, the decision is `None`
  (no opinion right now, retry later) — *not* an abort.
- `required_for_merge`: must produce a non-None score *for the merge
  to happen*. If all return None, the decision is `merge=False` with
  `abort_reason="missing_required_for_merge"`.

In v1's TOML, `required = ["rotation_cohort"]` becomes
`required_eventually = ["rotation_cohort"]` for AirTags. The
distinction: "I won't merge AirTags without rotation evidence, but
I'm willing to wait and revisit."

`required_for_merge` is reserved for cases like `service_uuid_match`
in hearing_aid: it would be wrong to ever merge two hearing aids
without UUID evidence.

## C3. Candidate generation skips manually-isolated devices

**Problem.** v1 represents manual exclusions as single-member clusters
with `source='manual'`. The candidate generator skips pairs that
touch them. But this means a manually-isolated device is *never*
considered for merging again, even if the user later changes their
mind without explicitly un-isolating.

**Refinement.** Manual exclusions get a TTL and a "respect manual"
flag in the runner. Default: respect indefinitely. Power-user knob:
"Re-evaluate manual decisions after N days." Most users will leave
this off; the option exists for tuning runs.

Additionally, the explicit `Re-cluster now` action ignores the manual
exclusion for the run that immediately follows it (one-shot override),
so the user can experiment without permanently changing the policy.

## C4. Transitive closure can chain weak edges into a wrong merge

**Problem.** Three devices A, B, C. Decisions:
- (A,B): merge, score 0.78 (just above threshold)
- (B,C): merge, score 0.78
- (A,C): no opinion (signals abstain)

Union-find says `{A, B, C}` is one cluster. But the *direct* evidence
(A,C) was zero — the cluster's coherence relies on B as a bridge.
With weights chosen poorly, B could be a "promiscuous bridge" that
matches everything weakly.

**Refinement.** After the union-find pass, run a *cluster cohesion
check*. For each cluster of size N, sample up to K random pairs
within the cluster (or check all pairs if N ≤ 5). Require that the
average pair-score within the cluster exceeds
`cluster_cohesion_threshold` (default 0.65, lower than the merge
threshold but not by much). If a cluster fails cohesion, split it on
its weakest edge and re-check.

This is essentially hierarchical clustering with a single-link
criterion — equivalent to running DBSCAN on the score graph. We get
this for free as a post-process.

## C5. Re-clustering is too coarse — full re-runs are expensive

**Problem.** v1 says "every 10 minutes, scan all candidates." On a DB
with 5000 active devices, even with the bucket-filter that's still
many thousands of `cluster_pair` evaluations per run. Each evaluation
loads recent packet history from the `packets` table — expensive.

**Refinement.** Two-tier scheduling:

1. **Hot path.** Whenever a *new* device appears or a device gets a
   batch of new observations, schedule a *targeted* `cluster_pair`
   evaluation against the existing cluster cache (limited to recent
   devices in the same bucket). This is O(bucket size) per event,
   typically tens of pairs.

2. **Cold path.** A full re-cluster runs once per hour (not every 10
   min) and re-evaluates everything. Catches the case where slow
   evidence accumulation (e.g. accumulating TX power observations for
   a now-stable device) flips a previously-no-opinion pair into a
   merge.

The hot path is what the user sees in real-time on the canvas. The
cold path is corrective.

## C6. Same-RSSI multi-AirTag scenario isn't well-handled

**Problem.** Three AirTags sitting on the same desk. They have
identical `rssi_signature` (within noise) and identical
`apple_continuity` (sub-type 0x12 set match). Their `rotation_cohort`
events on the same sniffer with the same RSSI are interleaved
randomly in time. v1 will likely merge all three into one — wrong.

**Refinement.** Two complementary fixes:

(a) **`rotation_cohort` requires non-overlapping observation windows.**
    If A and B were both *concurrently observed* (within their
    rotation interval) by the same sniffer, they cannot be the same
    device. Add this as an additional constraint:

    ```python
    def applies_to(ctx, a, b):
        if both_observed_within(a, b, params.expected_rotation):
            return False  # they coexist; not handoff candidates
        return True
    ```

(b) **`status_byte_match` becomes more important.** When the OF
    status byte sequences disagree at the handoff point, this is
    *strong* evidence of different devices. Bump its weight from
    0.10 to 0.20 in the AirTag profile, with a corresponding
    reduction in `rotation_cohort` from 0.45 to 0.35.

After this fix, three same-desk AirTags should produce three separate
clusters because their concurrent existence prevents any
`rotation_cohort` opinion.

## C7. `apple_continuity` 0x10 (Nearby) ubiquity

**Problem.** Every modern iPhone emits sub-type 0x10. Two strangers'
phones in a coffee shop will have *identical* sub-type sets if both
are screen-locked + Wi-Fi-on. v1 returns 0.85 for them — pushing the
weighted mean toward merge.

**Refinement.** When the only matching Continuity sub-type is 0x10
*alone* (no other sub-types in either device), cap the score at 0.30:

```python
if sigs_a == {0x10} and sigs_b == {0x10}:
    return 0.30  # consistent with same device, but ubiquitous so weak
```

When *additional* unusual sub-types match (0x09 AirPlay, 0x05 AirDrop,
0x07 AirPods proximity), the score remains high.

## C8. RSSI signature is fragile to sniffer power cycle

**Problem.** A sniffer that gets unplugged and replugged gets a new
`sniffer_short_id`. The RSSI signatures cached for previous devices
are now stale — they reference a sniffer that doesn't exist. Worse,
if the new sniffer takes the previous short_id (LRU reuse), signatures
appear *valid* but reference different physical positions.

**Refinement.**
- Sniffer short_id is keyed by USB serial number, so power-cycling
  the same physical sniffer keeps the same id. Already enforced.
- Cached signatures invalidate when their `recent_window` falls
  outside the most-recent observation. Already implicit.
- Add an explicit "sniffer moved" signal: if a known sniffer's RSSI
  distribution shifts by more than 6 dB across all observed devices
  simultaneously, invalidate the entire RSSI cache for that sniffer.
  This catches accidental physical re-positioning.

## C9. The framework has no error budget concept

**Problem.** A buggy signal that consistently raises exceptions still
contributes nothing to the weighted sum (per failure-isolation
design), but the user has no easy way to see it's broken — only a
log line per pair.

**Refinement.** Per-run error counters surfaced in the canvas status
bar:

```
clustering: last run 14:32:18; 1247 pairs; 8 merges; 3 signal errors:
  apple_continuity:2  status_byte_match:1
```

Click → details. A signal that's been raising for >50% of pairs in 3
consecutive runs gets auto-disabled for the next run with a UI
warning ("apple_continuity is failing — check decoder version").

## C10. Manual merges should still be auditable

**Problem.** v1 manual merges write `decided_by='manual'` but no
contributions JSON. If the user later asks "why is X in this
cluster?" we can only say "you said so." That's fine, but we should
*also* compute the auto-decision for the same pair and log it for
audit.

**Refinement.** When a manual merge happens, compute the would-be
auto decision in the background and store it in
`device_cluster_members.contributions`:

```json
{
  "_source": "manual",
  "_auto_decision": {
    "merge": false,
    "score": 0.62,
    "signals": {"rotation_cohort": [0.71, 0.45], ...}
  }
}
```

When the user un-merges, show the auto-decision as the suggested
default. This makes manual decisions teaching examples for the
weights system.

## C11. IRK comparison must be constant-time

**Problem.** v1's `irk_resolution` does a byte equality check
(`hash_computed == hash_observed`). In Python this is constant-time
for `bytes`, but the loop `for ident in ctx.irks` short-circuits on
match — leaking via timing which IRK was the matching one. Probably
fine since the timing leak requires local code execution, but worth
calling out.

**Refinement.** Loop through *all* IRKs even after a match; record
the matching one(s) and return at the end. Also use `hmac.compare_
digest()` for the byte comparison even though it's already
constant-time for `bytes` — defense in depth + intent signaling.

## C12. There's no negative evidence

**Problem.** v1 signals all return scores in `[0.0, 1.0]` where 0.0
means "this evidence indicates same device with confidence zero" —
it's *neutral*. There's no way to express "this evidence
*affirmatively indicates different devices*."

**Refinement.** Allow signals to return scores in `[-1.0, 1.0]`,
where negative scores indicate active evidence of difference. The
weighted mean is still well-defined (could go negative; clamp final
to `[0.0, 1.0]` for the threshold comparison, or — better — change
the threshold to a signed cutoff at the rationalist mid-point).

Concretely:
- `tx_power_match`: equal TX → +0.30 (with rarity bonus). Different
  TX → -0.50 (active evidence of difference).
- `pdu_distribution`: matching → small +; substantially different
  TVD → -0.5.
- `service_uuid_match`: full subset disagreement → -0.7.

The weighted mean is then thresholded against a value like 0.5 (not
0.75); intuitively "more evidence for than against, with a margin."

This is a v3 change — not strictly necessary for the first ship,
but the protocol should permit it from day one (so float scores in
`[-1.0, 1.0]` not `[0.0, 1.0]`). The existing signals all happen to
return non-negative; nothing breaks.

## C13. Per-device-class profile selection is fragile to misclassification

**Problem.** If a device is misclassified — say, a third-party
beacon misclassified as `iphone` — it gets the iPhone profile, where
required signals like `apple_continuity` will return `None`,
producing no opinion forever. The framework gives up on the device
silently.

**Refinement.** Two safety nets:
- A device that has been in `default` profile for >24h and has
  accumulated >100 packets gets re-classified by the AD-history
  vocabulary classifier. (This is a separate task — DLT-class-
  refinement — but the cluster framework should *trigger* it, not
  build it.)
- The `default` profile is genuinely useful (universal signals only,
  high threshold). A misclassified-but-active device can still be
  matched correctly via `default` even before the re-classification
  runs.

## C14. `device_cluster_members` allows ambiguous join

**Problem.** v1 schema PRIMARY KEY is `(cluster_id, device_id)`, so
the same `device_id` can appear in multiple clusters during a
re-clustering transition. Querying "what cluster is this device in?"
returns multiple rows.

**Refinement.** Add a `current` boolean column with a partial unique
index:

```sql
ALTER TABLE device_cluster_members
    ADD COLUMN current INTEGER NOT NULL DEFAULT 1;

CREATE UNIQUE INDEX uq_dcm_current
    ON device_cluster_members(device_id) WHERE current = 1;
```

Re-clustering writes new rows with `current=1` and demotes the
previous rows to `current=0` in one transaction. The history is
preserved for audit; the canonical view is the `current=1` rows.

## What the v2 plan adds

Summary of the deltas to roll into the code skeleton:

1. `min_total_weight` per profile (C1).
2. Split `required` into `required_eventually` and `required_for_merge` (C2).
3. Manual-exclusion TTL knob, default off (C3).
4. Cluster-cohesion check post-union-find (C4).
5. Two-tier scheduling: hot/cold paths (C5).
6. `rotation_cohort.applies_to` rejects concurrent observations (C6a).
7. Re-balance AirTag weights toward `status_byte_match` (C6b).
8. `apple_continuity` cap when only 0x10 matches (C7).
9. Sniffer-power-cycle RSSI cache invalidation (C8).
10. Per-run error budget surfaced in UI (C9).
11. Manual merges store auto would-be decision in contributions JSON (C10).
12. Constant-time IRK loop with `hmac.compare_digest` (C11).
13. Negative-score protocol (`[-1.0, 1.0]`); existing signals
    unchanged but tx_power/pdu_distribution/service_uuid get negative
    branches (C12).
14. `current` column + partial unique index on `device_cluster_members` (C14).

Items C13 is a follow-up for the classifier, not this framework.

These refinements are folded into the skeleton code in the next step.
