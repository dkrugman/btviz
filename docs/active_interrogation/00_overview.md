# Active interrogation — overview

btviz today is purely passive. Sniffer dongles run nRF Sniffer
firmware, capture advertising and (when followed) data-channel
packets, decode PHDR pseudo-header + BLE link-layer, and persist
observations. Identity is inferred from on-air evidence: local_name,
manufacturer-data prefixes, service-UUID sets, Apple Continuity TLVs,
co-lifespan timing.

There's a well-known ceiling to passive: a device may broadcast
nothing identifying — a flat MAC and an RPA payload — and only
return Manufacturer Name "Starkey", Model "Omega AI 24", and a PnP ID
*when an attached central reads its GATT 0x180A Device Information
Service*. Nordic's iOS Scanner app does this routinely (your hearing
aids' detail screen comes from a GATT connect + characteristic read);
btviz does not.

The DK board you reflashed with connectivity firmware is the entry
point. It exposes HCI over UART, can act as a Central, and can drive
a GATT client. PR #75 added the `is_tx_capable` flag and the role
planner reservation that earmarks one DK as the future "active
probe." This design document is what we put on top of that flag.

## Scope

This document defines:

- When btviz should connect to a target vs. continue passively
  observing it.
- When to drop a connection.
- The relationship between *following* (passive RPA-tracking via
  IRK) and *connecting* (active GATT exchange).
- Which BLE GATT data is worth reading from the spec, what to keep,
  and what to discard.
- A storage model that doesn't drown in immutable or repeated data.
- A self-critique of the above and a revised plan that addresses
  the issues raised.
- Integration path for Classic Bluetooth via Ubertooth One,
  HackRF One/Pro, and `ice9-bluetooth-sniffer`.

## What this document is *not*

- A full implementation. Scaffolding is committed alongside this doc
  but the HCI layer, the GATT client, and the storage adapter are
  stubs. The next PR will fill them in once you've reviewed the
  shape.
- A security tool. Active interrogation is dual-use. btviz reads
  what targets *willingly publish* via GATT. We do not pair, do not
  attempt cryptanalysis, do not implement the BlueDoor downgrade
  attack from the literature even though we cite it.
- A blanket policy. "Probe everything" is the wrong default;
  we'll discuss the policy knobs.

## Reading order

1. `01_initial_plan.md` — first-pass design. Answers your questions
   directly with concrete defaults.
2. `02_critique.md` — self-critique. Holes, ambiguities, open
   questions, things the initial plan handwaves.
3. `03_revised_plan.md` — answers worked through, design updated,
   defaults revised.
4. `04_classic_bt_integration.md` — separate concern, separate
   document. Where Ubertooth, HackRF, and `ice9-bluetooth-sniffer`
   fit.
5. `05_scaffolding.md` — code committed in this PR, why each piece
   exists, what's stub vs. real.
