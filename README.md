# btviz

Bluetooth troubleshooting and visualization tool. Drives one or more Nordic
nRF Sniffer for Bluetooth LE dongles via their `extcap` plugin (the same one
Wireshark uses), aggregates packets, identifies devices, and visualizes them
on a per-project canvas.

Targeted at debugging BLE topology and audio quality — particularly LE Audio
broadcasts (Auracast) — across multiple captures over time.

## System requirements

- macOS or Linux (Windows later)
- **Python 3.11+**
- **Wireshark with `tshark`** on the system path (or in the standard macOS
  install location). btviz invokes `tshark` as a subprocess for packet
  dissection — it is *not* a Python dependency. Verify with `tshark -v`.
- **Wireshark "nRF Sniffer for Bluetooth LE" extcap** for live capture.
  Verify with `tshark -D` showing entries like
  `nRF Sniffer for Bluetooth LE COM…`.
- One or more **Nordic nRF52840 dongles or DKs** flashed with the official
  nRF Sniffer firmware (only needed for live capture; file ingest works
  without hardware). See [docs/HARDWARE.md](docs/HARDWARE.md) for the full
  compatible-device list, firmware-flashing steps, and troubleshooting.

## Install

From a fresh clone:

```sh
cd btviz
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

`requirements.txt` is a one-line `-e .` — it does an editable install of
this package, which pulls in dependencies declared in `pyproject.toml`
(currently just `PySide6`). Equivalent and also fine:

```sh
pip install -e .
```

After install, the `btviz` command is on the venv's PATH.

## Quickstart

The fastest way to see something work, without any sniffer hardware, is to
ingest a pcap/pcapng file you've already captured in Wireshark:

```sh
btviz ingest path/to/capture.pcapng --project home-lab
```

That command:
- creates the `home-lab` project if it doesn't exist,
- dissects the file via `tshark` (with CRC-failed packets dropped by default
  — see `--keep-bad-crc` for interference analysis),
- writes one device row per unique advertising address, identity clues from
  AD structures (vendor, appearance, name when present), and per-session
  observation aggregates,
- prints a summary report.

Then open the project on the canvas:

```sh
btviz canvas --project home-lab
```

Each device is a draggable box. **Double-click** a box to expand it
(showing all known addresses, PDU/channel histograms, RSSI range);
double-click again to collapse. Positions persist per project.

## Subcommands

| Command | Purpose |
| --- | --- |
| `btviz` | Default GUI — live-capture table window |
| `btviz canvas [--project NAME] [--db PATH]` | Per-project canvas board (DB-backed; live updates not yet wired) |
| `btviz ingest <file> [--project NAME] [--keep-bad-crc] [--db PATH]` | Load a pcap/pcapng file into the DB |
| `btviz sniffers` | Interactive shell for managing connected nRF dongles (list / pin / scan / follow / idle) |
| `btviz --list-interfaces` | Print discovered nRF Sniffer dongles and exit |

The default DB lives at:

- macOS: `~/Library/Application Support/btviz/btviz.db`
- Linux: `$XDG_DATA_HOME/btviz/btviz.db` (typically `~/.local/share/btviz/btviz.db`)

Override with `--db PATH` on any command, or set `$BTVIZ_DB_PATH`.

## Project layout

```
btviz/
  pyproject.toml         # package metadata + Python deps
  requirements.txt       # one-liner: pip install -r requirements.txt → -e .
  src/btviz/
    __main__.py          # CLI entry: subcommand dispatch
    config.py            # paths, defaults, channel plan
    bus.py               # tiny pub/sub event bus
    vendors.py           # MAC OUI + Bluetooth SIG company-id lookups
    data/                # bundled lookups (company_identifiers.json)
    extcap/              # locate Nordic extcap, enumerate dongles, run capture
      discovery.py
      sniffer.py
    capture/             # role assignment, scan/follow state machine
      coordinator.py
      packet.py
      roles.py
    decode/
      adv.py             # minimal ADV decoder (live path; tshark handles ingest)
    tracking/            # in-memory live device inventory
      device.py
      inventory.py
    db/                  # SQLite store
      schema.sql         # global devices/addresses + per-project layouts/sessions
      models.py
      repos.py
      store.py
    ingest/              # file ingest pipeline
      tshark.py          # subprocess wrapper, EK JSON streaming
      normalize.py       # tshark record → Packet
      pipeline.py        # writer: project/session/device/observation rows
    cli/
      sniffers.py        # `btviz sniffers` interactive shell
      ingest.py          # `btviz ingest` argparse + runner
    ui/
      app.py             # default GUI (live table + sniffer controls)
      canvas.py          # `btviz canvas` device board
  tests/
    fixtures/            # checked-in test fixtures (no real keys)
  private/               # gitignored: local pcaps, IRKs/LTKs, scratch DBs
```

## Notes

`private/` is in `.gitignore` — that's where I keep test pcaps, real IRKs,
and anything else that shouldn't ship with the repo. The DB schema does
support keys (IRKs, LTKs) and there's nothing stopping you from putting
real values into the default DB; just don't share that file.
