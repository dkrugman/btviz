# Device-class icons

Drop one SVG file per `device_class` here, named `<device_class>.svg`.
The canvas's `DeviceItem.paint()` will load and render it via
`QSvgRenderer` at 36×36 inside the box header. If no SVG exists for a
class, the canvas falls back to the emoji entry in `_DEVICE_CLASS_ICONS`
in `src/btviz/ui/canvas.py`.

## Recognized classes

These match the values populated by the ingest pipeline (Apple
Continuity decoder + GAP appearance fallback):

```
airpods             airtag              apple_watch
apple_device        apple_airplay       homekit
ibeacon             phone               computer
watch               clock               display
remote_control      eyewear             tag
keyring             media_player        barcode_scanner
thermometer         heart_rate_sensor   blood_pressure_monitor
hid                 glucose_meter       running_walking_sensor
cycling_sensor      pulse_oximeter      weight_scale
fitness_tracker     hearing_aid         personal_mobility_device
```

Add new classes by populating `device_class` from the ingest pipeline
and dropping a matching SVG here.

## SVG guidelines

- Square viewBox (e.g. `viewBox="0 0 24 24"` or `0 0 64 64`). The
  renderer scales to the icon area.
- Keep strokes inside the viewBox — anything that bleeds out gets
  clipped by `QSvgRenderer`.
- Plain SVG (no embedded fonts, no external references). Fonts referenced
  by `font-family` won't resolve at render time.
- Either fill with explicit colors, or use `currentColor` if you want
  the icon to be tinted later (we don't tint yet, but it's a clean
  hook for active/dormant coloring).

## Licensing reminder

This directory ships with the package. Anything you commit here will be
distributed under btviz's license. If you use icons from a marketplace
(Iconscout, Noun Project, etc.), confirm the license permits
redistribution and add attribution where required. Free MIT-licensed
sets that work without ceremony: [Lucide](https://lucide.dev),
[Tabler Icons](https://tabler-icons.io), [Phosphor](https://phosphoricons.com).
