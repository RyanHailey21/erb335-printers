---
name: erb335-printers
description: "Control and inspect the UTA ERB 335 3D printers on Skynet Wi-Fi (4x Creality Ender-3 S1 Pro via OctoPrint + 4x Bambu Lab printers) using the erb335-printers CLI. Triggers on ERB 335, Skynet Wi-Fi, OctoPrint, Bambu Lab at UTA, steve.local, saed.local, kate.local, chris.local, or RoboMasters printing."
---

# UTA ERB 335 3D Printers (`erb335-printers`)

Programmatic Wi-Fi + OctoPrint control for the ERB 335 Senior Design Lab 3D printer fleet via the global `erb335-printers` CLI (backed by `scripts/printer_cli.py`, stdlib-only Python 3).

## Network
- **Wi-Fi:** SSID `Skynet` (password `t3rm1n4t0r`, 2.4 GHz Ch 1, `192.168.50.0/24`; has internet access).
  - *Windows quirk:* Switching from 6 GHz `UTA WiFi` to 2.4 GHz `Skynet` via bare `netsh` fails on stale BSS cache. Always use `erb335-printers wifi connect`, which triggers a native Windows `WlanScan` via `wlanapi.dll` first.

## Fleet on `Skynet` (8 Printers)

### 4× Creality Ender-3 S1 Pro (OctoPrint - HTTP `:80`)
- **Specs:** 220 × 220 × 270 mm, 0.4 mm nozzle, all-metal Sprite extruder (up to 300°C), heated bed (up to 110°C).
- **Filaments:** **PLA and PETG** supported (match slicer temps to the loaded spool - e.g., ~200°C / 60°C for PLA, ~230-245°C / 70-80°C for PETG). ABS is prohibited due to fumes.
- **Slicer Note:** OctoPrint's internal profile on these printers is left at `Generic RepRap Printer` (200×200×200 mm). Always slice using a **Creality Ender-3 S1 Pro** profile.
- **Auth:** Username `student`, password `student` (session login `/api/login` + `X-CSRF-Token`, no API key).

| Name | Hostname | IP | Notes |
| :--- | :--- | :--- | :--- |
| **Steve** | `steve.local` | `192.168.50.108` | Active |
| **Saed** | `saed.local` | `192.168.50.109` | Active |
| **Kate** | `kate.local` | `192.168.50.104` | Active |
| **Chris** | `chris.local` | `192.168.50.107` | Excluded from default `status` (pass `--all`) |

### 4× Bambu Lab Printers (LAN MQTT `:8883` + FTPS `:990`)
*(Requires each printer's 8-digit LAN Access Code for status/FTPS, plus **LAN Only Mode + Developer Mode** enabled on the touchscreen for automated `upload --print` / `job start` over LAN MQTT. When slicing Bambu profiles headlessly via `bambu-studio.exe --load-settings`, always merge both `"inherits"` and `"include"` template JSONs so native start/end G-code, ABL, and Textured PEI Z-offset `G29.1 Z-0.02` are preserved.)*

| IP | Serial (TLS Cert) | Model |
| :--- | :--- | :--- |
| `192.168.50.103` | `0948AD532300868` | H2D (`094`) |
| `192.168.50.110` | `01P00C4B2900458` | P1S (`01P00C`) |
| `192.168.50.111` | `01P00A442101323` | P1S / P1P (`01P00A`) |
| `192.168.50.112` | `03919C450503017` | A1 (`039`) |

## Lab Rules
- Verify the physical bed is clear, filament is loaded, and no Out of Order tag is posted before printing.
- Supervise initial layers in person; control OctoPrint printers via OctoPrint rather than the touchscreen.

## CLI Reference (`erb335-printers`)

```powershell
# Wi-Fi (native WlanScan + connect to Skynet)
erb335-printers wifi status
erb335-printers wifi connect
erb335-printers wifi fix-profiles          # removes duplicate lowercase 'skynet' profile

# Bambu Lab local cache & DHCP-proof discovery (~/.config/erb335-printers/bambu.json)
erb335-printers bambu sync-codes           # discovers Bambu IPs + seeds local cache from BambuStudio.conf
erb335-printers bambu discover             # re-scans subnet (:8883) and refreshes cached IPs
erb335-printers bambu set h2d --serial 0948AD532300868 --code 12345678

# Unified fleet status (OctoPrint + Bambu Lab + AMS trays)
erb335-printers status                     # steve, saed, kate + h2d, p1s-1, p1s-2, a1
erb335-printers status --all               # includes chris
erb335-printers status --octoprint         # OctoPrint only
erb335-printers status --bambu             # Bambu Lab only
erb335-printers status h2d                 # single printer

# Files & uploading (.gcode -> OctoPrint; .gcode or .gcode.3mf -> Bambu FTPS :990)
erb335-printers files steve --limit 10
erb335-printers upload steve .\part.gcode --select
erb335-printers upload steve .\part.gcode --print
erb335-printers upload a1 .\part.gcode --print   # auto-wraps to .gcode.3mf + starts via LAN MQTT (requires Developer Mode)
erb335-printers job a1 start --file part.gcode.3mf

# Temperatures, job control & live watch
erb335-printers temp steve --tool 235 --bed 75   # PETG example
erb335-printers temp steve --tool 200 --bed 60   # PLA example
erb335-printers job steve start                  # start | pause | resume | cancel
erb335-printers watch h2d --interval 5           # live monitor (works for OctoPrint & Bambu)

# OctoPrint serial connection
erb335-printers connect steve
erb335-printers disconnect steve
```
