# btviz

Bluetooth troubleshooting and visualization tool. Drives one or more Nordic
nRF Sniffer for Bluetooth LE dongles via their `extcap` plugin (the same one
Wireshark uses), aggregates packets, identifies devices, and (eventually)
visualizes topology, connections, and Auracast broadcasts.

## Status

Scaffold. Phase 1 = discover dongles, pin each to a primary advertising
channel, build a live device inventory, expose follow-target selection.

## Requirements

- macOS or Linux (Windows later)
- Python 3.11+
- Wireshark with the **nRF Sniffer for Bluetooth LE** extcap installed
  (available from Nordic; verify with `tshark -D` showing
  `nRF Sniffer for Bluetooth LE: …` interfaces)
- One or more Nordic nRF52840 dongles or DKs flashed with the official
  nRF Sniffer firmware

## Install (dev)

```sh
cd btviz
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

## Run

```sh
btviz                    # GUI
btviz --list-interfaces  # print discovered dongles and exit
```

## Layout

```
btviz/
  config.py            # paths, defaults, channel plan
  bus.py               # tiny pub/sub event bus
  extcap/
    discovery.py       # find Nordic extcap binary + enumerate dongles
    sniffer.py         # one process per dongle; pins channel; emits packets
  capture/
    coordinator.py     # role assignment, scan/follow state machine
    packet.py          # normalized packet record
  decode/
    adv.py             # minimal adv-data parser for inventory
  tracking/
    inventory.py       # device table fed by adv packets
    device.py
  ui/
    app.py             # PySide6 main window (sniffer panel + device table)
```
updated 4/24/26 2:54pm