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

## Canvas UX

- [ ] **`apply_grid_layout` collision avoidance.** When a subset of
      devices have saved layouts that happen to land on the
      auto-grid's lattice (e.g. saved at (21, 326), grid cell at
      (20, 326)), unplaced devices stack on top of them and the
      visible count diverges from the data count. Either skip
      occupied grid slots when laying out unplaced devices, or shift
      the saved-position devices to vacant slots. "Reset layout" is
      the user-facing workaround today.
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

## RPA collapse / clustering (the post-CRC problem)

- [ ] **Wide fingerprint schema migration.** Three new tables:
    - `device_ad_history (device_id, ad_type, ad_value, first_seen,
      last_seen, count)` — per-device AD-entry vocabulary.
    - `packets (session_id, device_id, address_id, ts, rssi,
      channel, pdu_type, sniffer_short_id)` — slim per-packet event
      log for temporal analysis.
    - Add `packets.raw` BLOB column for forensic re-decode capability.
- [ ] **Cluster-based RPA collapse.** Once wide fingerprint exists:
      DBSCAN / hierarchical clustering on a feature vector
      `(vendor_id, sorted_service_uuids, tx_power, mfg_data_prefix,
      conn_interval, adv_interval_ms, rssi_signature)`. Goal:
      collapse the ~30 `Apple, Inc. apple_device` rows that are
      really 5–10 physical iPhones rotating RPAs.
- [ ] **Manual merge action.** Multi-select device boxes →
      right-click → "Merge as same device" → creates a synthetic
      identity row, re-points addresses. Fallback for when automatic
      methods fall short or for explicit user labeling.
- [ ] **IRK resolution.** UI to import IRKs (paste-text or
      .btsnoop import), AES-128 verify each unresolved RPA against
      each IRK, populate `addresses.resolved_via_irk_id`, merge
      `devices` rows when multiple RPAs resolve to one IRK. Schema
      already supports this — only the UI + crypto path are missing.
- [ ] **LLM oracle for cluster identification.** Once a cluster's
      fingerprint is known, ask an LLM "what device emits these
      service UUIDs + manufacturer data prefix?" with citations to
      BLE assigned-numbers docs. Useful only AFTER clustering is in.
- [ ] **Per-packet retention policy.** Knob to drop `packets` older
      than N days while keeping aggregates. Required once per-packet
      table exists; not urgent before that.

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
