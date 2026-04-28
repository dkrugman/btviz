# Open TODOs

Tracking deferred work surfaced during development. Most items are
intentional follow-ups: a known limitation we accepted in service of
shipping the surrounding feature, or a "next step once X lands"
dependency. Tag items as `[done]` when merged so the file stays a
useful diff against the codebase.

## Capture / decode

- [ ] **AUX_ADV_IND ↔ primary pairing via ADI.** Today the live status
      shows `rec ≈ 78% of dec`. The 22% gap is BLE 5.0 `AUX_ADV_IND`
      continuation packets that omit `AdvA` because the primary
      `ADV_EXT_IND` on 37/38/39 already carried it. They decode and
      contribute to `bcast`, but `record_packet` skips them
      (no `adv_addr`). Pair primary↔AUX by `AdvDataInfo` (DID + SID)
      so the AUX inherits the primary's broadcaster identity. Would
      lift `rec` to ~98% of `dec`.
- [ ] **2M PHY config knob on `SnifferProcess.start()`.** Standard
      Nordic firmware sniffs 1M only by default. Some Auracast
      broadcasters use 2M for their AUX. Add a `--phy 2M` (or
      whatever the extcap supports) when we see a known-2M
      broadcaster reporting `ext=0` in the live status.
- [ ] **BASE structure parsing for Auracast.** Deferred per
      `auracast.py` docstring — BASE lives in the Periodic Advertising
      train, structurally complex; needs PA-syncing toolkit firmware
      feeding the same DB before it pays off.

## Capture / discovery

- [ ] **Switch live discovery to `list_dongles_fast()` (ioreg) instead
      of `list_dongles()` (slow extcap probe).** The slow probe has
      now demonstrably failed multiple times — it both under-counts
      (silently omitting Nordic dongles whose firmware is in a hung
      state) and *mis-attributes* (labels Adafruit + DK as "nRF
      Sniffer for Bluetooth LE"). The fast probe reads USB descriptors
      directly via ioreg and has been reliable. Switch live capture's
      `CaptureCoordinator.refresh_dongles` to fast-probe; keep slow
      probe as a fallback for non-macOS platforms (ioreg is macOS-only)
      and for devices where we need extcap-supplied interface_id paths.

## Canvas UX

- [ ] **`apply_grid_layout` collision avoidance.** When a subset of
      devices have saved layouts that happen to land on the
      auto-grid's lattice (e.g. saved at (21, 326), grid cell at
      (20, 326)), unplaced devices stack on top of them and the
      visible count diverges from the data count. Either skip
      occupied grid slots when laying out unplaced devices, or shift
      the saved-position devices to vacant slots. "Reset layout" is
      the user-facing workaround today.
- [ ] **[done in branch]** Visible fast-vs-slow discovery mismatch
      in the sniffer panel — sniffer rows that fast-discovery found
      but slow-discovery couldn't probe show
      `"USB-detected but not responding to extcap probe — try replug
      to recover."` in their tooltip. Implementation:
      `feat/visible-extcap-discovery-mismatch`.
- [ ] **[done in branch]** Surface `SnifferProcess` startup errors in
      the toolbar status. Canvas now subscribes to
      `TOPIC_SNIFFER_STATE` while live is running and shows
      `state.last_error` when it appears, instead of letting the
      capture-loop exit silently. Implementation:
      `feat/visible-extcap-discovery-mismatch`.
- [ ] **Smooth slide animation for sniffer panel toggle.** Currently
      an instant snap — `QPropertyAnimation` on `maximumWidth` driving
      the `QHBoxLayout` reflow would make it feel deliberate.
- [ ] **Box-size scaling vs column-count scaling.** Today column
      count adjusts to viewport width, box width stays fixed at
      220 px. If we want boxes themselves to grow/shrink with window
      width that's a separate, more invasive piece (DeviceItem
      `boundingRect` becomes dynamic; needs a geometry-provider hook
      from the canvas).
- [ ] **iPhone / iPad / Mac SVG icons.** Currently only emoji
      fallbacks exist; the `apple_device.svg` cascade covers them.
- [ ] **Radiating-waves activity animation on active devices.** Boxes
      whose ``last_seen`` is within the last few seconds emit a brief
      expanding-circle animation behind the icon — a visual heartbeat
      that's distinct from the existing static opacity fade.
      Implementation likely needs a per-device QTimer + custom paint
      (or a single scene-level QPropertyAnimation on a phase value).
- [ ] **Auto-hide stale devices.** Toolbar toggle / preference to
      flip ``hidden=1`` on devices not observed in the last N hours
      (e.g. 1h / 24h / 7d / never). Hidden devices reappear when
      observed again. Doesn't fix the duplicates-multiplying problem
      (RPA collapse does), but immediately winnows visual clutter
      from stale captures. Needs a "show hidden" toggle so users can
      surface them again without "Reset layout".

## RPA collapse / clustering (the post-CRC problem)

The framework design lives at `docs/rpa_collapse/` — module-per-signal,
per-device-class profiles weighting them, a small aggregator that
scores candidate pairs and decides whether to merge. The list below is
the implementation order.

- [ ] **Wide fingerprint schema migration.** Three new tables:
    - `device_ad_history (device_id, ad_type, ad_value, first_seen,
      last_seen, count)` — per-device AD-entry vocabulary.
    - `packets (session_id, device_id, address_id, ts, rssi,
      channel, pdu_type, sniffer_short_id)` — slim per-packet event
      log for temporal analysis.
    - Add `packets.raw` BLOB column for forensic re-decode capability.
- [ ] **Pluggable signal framework.** ``Signal`` protocol +
      ``ClusterContext`` + per-device-class ``ClassProfile`` weights.
      Skeleton in `src/btviz/cluster/` (this branch). Concrete signal
      modules: rotation_cohort, rssi_signature, adv_interval,
      service_uuid, mfg_data_prefix, apple_continuity, tx_power,
      status_byte, pdu_distribution.
- [ ] **Cluster aggregator + merge.** Weighted-sum aggregator over
      applicable signals; threshold-based decision; writes to a new
      `device_clusters` table (cluster id → member device ids).
      Manual merge action (multi-select → "Merge as same device")
      uses the same table.
- [ ] **IRK resolution.** UI to import IRKs (paste-text or
      .btsnoop import), AES-128 verify each unresolved RPA against
      each IRK, populate `addresses.resolved_via_irk_id`, merge
      `devices` rows when multiple RPAs resolve to one IRK. Schema
      already supports this — only the UI + crypto path are missing.
      Treated as a separate signal in the framework above (the
      strongest one — cryptographic proof rather than probability).
- [ ] **LLM oracle for cluster identification.** Once a cluster's
      fingerprint is known, ask an LLM "what device emits these
      service UUIDs + manufacturer data prefix?" with citations to
      BLE assigned-numbers docs. Useful only AFTER clustering is in.
- [ ] **Per-packet retention policy.** Knob to drop `packets` older
      than N days while keeping aggregates. Required once per-packet
      table exists; not urgent before that.

## Privacy / threat-model awareness

- [ ] **Traffic-analysis self-audit (defense direction).** Barman et
      al. 2021 ("Every Byte Matters") show that even encrypted BLE
      traffic leaks device model, app opens, and fine-grained user
      actions (e.g. "record insulin injection") via packet sizes +
      timings. We're a *capture* tool, so this is informational —
      but worth being aware that the patterns we use to *fingerprint
      and cluster* are the same patterns an adversary uses to
      *track and profile* a wearer. If we ever ship features that
      stream user data over BLE (own broadcasters / fake clients)
      this becomes a real concern. Reference:
      Proc. ACM IMWUT 5(2), Article 54.
- [ ] **Apple Find My / OF awareness.** Heinrich et al. 2021 ("Who
      Can Find My Devices?") fully specify Apple's offline-finding
      protocol. Each AirTag emits 28 bytes of EC public key per
      packet, rotating every 15 min. Without the IRK, two consecutive
      rotations are AES-128-uncorrelated — confirms the only passive
      avenue for AirTag collapse is behavioral (rotation cohort,
      RSSI signature, status byte). Cited in `docs/rpa_collapse/`.

## Cleanup

- [ ] **Strip one-time diagnostic logs.** Two stderr lines that were
      useful when debugging the DLT 272 / NBE-flags-byte mismatches
      and have served their purpose:
    - `[sniffer] <id> pcap magic=0xa1b2c3d4 dlt=N` in
      `extcap/sniffer.py::_capture_loop`.
    - `[live-decode] reject src=… first32=…` in
      `capture/live_ingest.py::_on_packet`.
- [ ] **Stale `app.py` reference in `canvas.py` docstring.** Module
      docstring still says "Lives alongside the live-capture table
      window in app.py (which is unchanged)". `app.py` was retired
      in `feat/canvas-follow-and-retire-app`. One-line fix.
