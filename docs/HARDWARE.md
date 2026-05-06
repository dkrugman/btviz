# Compatible hardware & firmware

btviz drives Bluetooth Low Energy capture through Nordic Semiconductor's
**nRF Sniffer for Bluetooth LE** firmware running on a USB dongle or
development kit. Live capture requires at least one of these devices,
flashed with the right firmware, and the corresponding Wireshark extcap
plugin installed on the host.

> *File ingest works without any hardware* — `btviz ingest <pcap>` reads
> existing capture files and runs the full decode + analysis pipeline.
> Hardware is only needed for live capture.

## Compatible devices

The following Nordic boards are known to work as capture devices when
flashed with the nRF Sniffer firmware:

| Board                                      | Notes                                                    |
|--------------------------------------------|----------------------------------------------------------|
| **nRF52840 Dongle** (PCA10059)             | The primary recommended device. Cheap, USB-A form factor, no debugger needed. |
| **nRF52840 DK** (PCA10056)                 | Works; **must be plugged into the nRF USB port**, not the IF/J-Link USB port (Nordic plugin bug). |
| **nRF52DK / nRF52833 DK**                  | Older silicon; not validated against the latest sniffer firmware — proceed with caution. |
| **Adafruit nRF52840 Feather / Sense**      | Hardware capable, but requires manually flashing Nordic Sniffer firmware (out-of-box ships with CircuitPython or similar). |

btviz can drive multiple dongles simultaneously — typical setup is three
dongles on advertising channels 37, 38, 39 (one each) plus optional
extras for follow / data-channel scanning.

## Firmware requirements

Only one firmware is supported for live capture: **Nordic nRF Sniffer
for Bluetooth LE**, distributed by Nordic Semiconductor at:

  https://www.nordicsemi.com/Products/Development-tools/nRF-Sniffer-for-Bluetooth-LE

A pre-built `.hex` lives in the download zip under
`hex/sniffer_nrf52840dongle_nrf52840_*.hex` (filename varies by
firmware version).

Other firmware that **will not work** with btviz:

* Nordic Connectivity firmware (used by `pc-ble-driver-py`) — different
  protocol; intended for the active-interrogation driver, not passive
  sniffing.
* Nordic UART Service (NUS) sample / template firmware — makes the
  dongle act as a peripheral, not a sniffer.
* CircuitPython, Zephyr samples, manufacturer demo firmware, etc.

## Flashing the firmware

Use Nordic's **nRF Connect for Desktop → Programmer** app:

1. Download nRF Connect for Desktop:
   https://www.nordicsemi.com/Products/Development-tools/nrf-connect-for-desktop
2. Open the **Programmer** app inside nRF Connect.
3. Plug in the dongle. For the nRF52840 Dongle (PCA10059) put it into
   bootloader mode (press the small RESET button) until the LED pulses red.
4. Select the device from the top-right.
5. **Add file** → choose the `sniffer_nrf52840dongle_nrf52840_*.hex` from
   the Nordic Sniffer download.
6. **Write** to flash.
7. Replug the dongle once the write completes.

For the nRF52840 DK, the IF MCU (J-Link) handles flashing automatically
via the Programmer; no bootloader-button dance required.

## Verifying the install

Once a dongle is plugged in and flashed:

```sh
# btviz's own probe
btviz --list-interfaces
# Should print one line per dongle, e.g.
#   2234201   /dev/cu.usbmodem2234201   (nRF Sniffer for Bluetooth LE)

# Wireshark also recognizes them
tshark -D | grep -i nrf
# Should show nRF Sniffer entries
```

If `btviz --list-interfaces` prints `extcap: …` but no dongle lines,
btviz can find the plugin script but the plugin can't find the
dongles. Most common causes:

* Dongle is plugged into the wrong USB port (DK only).
* Wrong firmware is on the dongle (verify via `nRF Connect Programmer →
  Read`; the file region should match the Sniffer hex).
* USB-CDC enumeration is wedged — try a replug.

## Wireshark / extcap plugin

btviz invokes the Nordic `nrf_sniffer_ble.py` extcap script — the same
one Wireshark uses. If you have Wireshark installed and the Nordic
plugin works in Wireshark, btviz should also find it.

* **macOS / Linux:** install the plugin via Nordic's instructions
  (typically extract into `~/.local/lib/wireshark/extcap/` so both
  Wireshark and btviz find it on `find_extcap_binary()` lookup).
* **Wireshark version pairing:** Wireshark 3.6 has historically been
  the most reliable pairing for the Nordic plugin. Wireshark 4.0+ has
  had repeated extcap-folder structure changes that break the plugin
  bootstrap; check Nordic's release notes if you hit issues.
* **Avoid `nrfutil-ble-sniffer` v0.19.0** (released 2026-04-30) on
  macOS — the Wireshark extcap shim is broken in that release. Pin to
  v0.18.x or use the legacy Python script directly. btviz uses the
  Python script path by default, sidestepping this regression.

## Known limitations of the Nordic firmware

These are documented Nordic firmware behaviours, not btviz bugs.
Awareness helps when interpreting capture results:

* **Connection events may be intermittently missed.** Nordic's own
  documentation states "the Sniffer may not pick up all connect
  requests and will not always pick up on a connection." Reconnect
  the target device or replug the dongle if it appears stuck.
* **Long-uptime stalls.** Some users have reported a "missing every
  other connection event" state after hours of uptime that only
  recovers via replug or reflash, not on its own. btviz's stall
  watchdog detects this and surfaces a `STALL` indicator in the
  toolbar; physical replug is the canonical recovery.
* **Encrypted-link malformed packets.** The sniffer can start showing
  malformed packets after a while when sniffing an encrypted link.
  This is a documented limitation in older Nordic user guides.
* **DK USB port.** Due to a bug in the nrfutil Sniffer plugin, you
  must use the nRF USB port (not the IF/J-Link USB port) on
  development kits — otherwise the plugin can't reach the radio.

## Troubleshooting

| Symptom                                    | Likely cause / fix                                        |
|--------------------------------------------|-----------------------------------------------------------|
| btviz shows no dongles ever                | Plugin not installed, or no Nordic hardware plugged in. Run `btviz --list-interfaces`. |
| Dongle visible but Start Capture stalls    | Wrong firmware on the dongle. Reflash via Programmer. |
| Some dongles work, others stay silent      | Mixed firmware versions. Reflash the silent ones to the same version that works. |
| Captures fine for hours then go quiet      | Documented Nordic firmware limitation. Replug; if persistent, reflash. |
| LEDs don't flash when capture starts       | Firmware isn't responding to the Sniffer API. Verify firmware via Programmer's Read. |
| All dongles stuck in `STALL ×3 — replug`   | Watchdog has given up. Physically unplug and replug the affected dongles. |

## Reporting issues

If you have hardware that should work but doesn't, please open an issue
at https://github.com/dkrugman/btviz/issues with:

* Hardware model + revision
* Firmware version (from nRF Connect Programmer → Read → file info)
* Output of `btviz --list-interfaces`
* Output of `tshark -D | grep -i nrf`
* OS + Python version
* Relevant `~/.btviz/capture.log` lines (especially any `STALL` events)

PRs adding tested-and-working hardware to the table above are welcome.
