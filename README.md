# ERB 335 Printers — Universal Agent Skill & CLI

Zero-dependency Python 3 CLI and universal **Agent Skill** (`SKILL.md`) for the **UTA ERB 335 Senior Design Lab** 3D printers on `Skynet` Wi-Fi:
- **4× Creality Ender-3 S1 Pro** via OctoPrint (`steve`, `saed`, `kate`, `chris`)
- **4× Bambu Lab** via LAN MQTT + FTPS (`h2d`, `p1s-1`, `p1s-2`, `a1`)

Works out of the box with **Antigravity**, **Claude Code**, **OpenAI Codex**, and plain terminals (Windows, macOS, Linux).

---

## One-Command Install

### Windows (PowerShell — Antigravity, Claude Code, Codex + global `erb335-printers` CLI)
```powershell
irm https://raw.githubusercontent.com/RyanHailey21/erb335-printers/main/install.ps1 | iex
```

### macOS / Linux (Bash)
```bash
curl -fsSL https://raw.githubusercontent.com/RyanHailey21/erb335-printers/main/install.sh | bash
```

### Or via the universal `skills` CLI (Claude Code / Codex / Cursor)
```bash
npx skills add RyanHailey21/erb335-printers
```

---

## Quick Start

```powershell
# 1. Connect to Skynet Wi-Fi (Windows: runs native WlanScan first to avoid 6 GHz -> 2.4 GHz cache bug)
erb335-printers wifi connect

# 2. (Optional, one-time) Seed local Bambu Lab LAN access-code cache from Bambu Studio
erb335-printers bambu sync-codes

# 3. Check all printers (OctoPrint + Bambu Lab + loaded AMS trays)
erb335-printers status

# 4. Upload & print on an Ender-3 S1 Pro
erb335-printers upload saed .\part.gcode --select
erb335-printers job saed start
erb335-printers watch saed
```

## Notes
- **Requirements:** Python 3.10+ (zero third-party `pip` packages required).
- **Credentials:** Bambu Lab LAN access codes are **never** stored in this repo. They are stored locally on your machine at `~/.config/erb335-printers/bambu.json` via `bambu sync-codes` or `bambu set`.
- **Lab Safety:** Always verify the physical build plate is clear, filament matches your sliced material (PLA or PETG; no ABS), and supervise the first layer.
