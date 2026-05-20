# Figmosha 2.0 — Windows Setup

A two-piece bridge between Claude (or any HTTP client) and Figma Desktop:

```
PowerShell      Python server         Figma Desktop
─────────       ────────────          ─────────────
                                        ┌──────────────┐
 figmosha.py  → /exec (HTTP)            │ Figma file   │
                  │                     │ ┌──────────┐ │
                  ▼                     │ │ Figmosha │ │
              broadcast ───── ws ─────►│ │  Bridge  │ │
                                        │ │ (plugin) │ │
                  ▲                     │ └────┬─────┘ │
                  │                     │      │ Plugin│
              result   ◄────── ws ──────│      ▼  API  │
                                        │ Figma canvas │
                                        └──────────────┘
```

No Playwright. No Firefox. No clipboard paste. No screenshots. Plugin keeps running even when Figma is minimized.

## Requirements

- Windows 10 or 11
- **Python 3.10+** — install from <https://python.org/downloads> (tick "Add python.exe to PATH"), or `winget install Python.Python.3.12`
- **Figma Desktop** (Stable or Beta) — <https://www.figma.com/downloads/>

## One-time setup

### 1. Install Python dependency

In PowerShell:

```powershell
pip install aiohttp
```

### 2. Get the repo on Windows

If you don't have git: install from <https://git-scm.com/download/win>.

```powershell
git clone https://github.com/denysosadchyi/figmosha.git
cd figmosha
```

(Or copy the folder from your other machine.)

### 3. Import the plugin into Figma Desktop

1. Open **Figma Desktop**
2. Open any Figma file (or create a new one)
3. Top menu → **Plugins** → **Development** → **Import plugin from manifest…**
4. Browse to `plugin\manifest.json` inside the repo
5. Figma registers "Figmosha Bridge" under Plugins → Development

You only do this once. The plugin stays in Figma's Development list.

## Daily use

### Start the server

```powershell
python bridge.py
```

Output:
```
[bridge] listening on http://127.0.0.1:8787
[bridge] plugin should connect to ws://localhost:8787/plugin
```

Leave this terminal running.

### Open the plugin in Figma

1. In Figma Desktop, open the file you want to work in
2. Menu → **Plugins** → **Development** → **Figmosha Bridge** → **Run**
3. Plugin window appears: "bridge: **connected**" (green)
4. In the server terminal you'll see `[plugin] connected from 127.0.0.1`

### Send code

```powershell
# Inline
python figmosha.py "figma.notify('hello from Claude')"

# From file
python figmosha.py --file script.js

# From stdin (handy in pipelines)
Get-Content script.js | python figmosha.py --stdin

# Read a value
python figmosha.py "return figma.currentPage.name"

# JSON-dump tree of selection
python figmosha.py "return figma.currentPage.selection.map(n => ({id:n.id, name:n.name, w:n.width, h:n.height}))"
```

### Check status

```powershell
python figmosha.py --status
```

```json
{
  "plugin_connected": true,
  "pending": 0
}
```

## Smoke test (do this first)

After steps above, run from a second PowerShell:

```powershell
# 1. Server reachable
python figmosha.py --status
#   → {"plugin_connected": true, ...}

# 2. Plugin executes code, returns scalar
python figmosha.py "return 1 + 1"
#   → 2

# 3. Plugin reads Figma state
python figmosha.py "return figma.root.children.map(p => p.name)"
#   → ["Page 1", "Page 2", ...]

# 4. Plugin mutates canvas
python figmosha.py "const r = figma.createRectangle(); r.x = 100; r.y = 100; r.resize(200, 100); r.name = 'smoketest'; return r.id"
#   → "123:456"
```

A rectangle named `smoketest` should appear on the current Figma page.

## Background-execution test

This is the whole point of 2.0: working without Figma in focus.

1. Open plugin, see "connected"
2. **Minimize** Figma window (Win+Down or click minimize)
3. From PowerShell:
   ```powershell
   python figmosha.py "return new Date().toISOString()"
   ```
4. You should get a fresh ISO timestamp back in <100ms
5. Restore Figma — plugin still says "connected", log shows the exec happened

If this works: you can ignore Figma entirely while Claude runs scripts.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `connection refused` from CLI | Server isn't running | `python bridge.py` in another PowerShell |
| `plugin not connected` (503) | Plugin window not open in Figma | Plugins → Development → Figmosha Bridge → Run |
| Plugin says `disconnected, retrying…` | Server is down or restarted | Start `bridge.py`; plugin auto-reconnects within 2s |
| 504 timeout | Code threw or hung | Add `--timeout 120`; if still hangs, close plugin (X) and Run again |
| `pip install aiohttp` fails | No internet / proxy / Python not in PATH | Check `python --version`; if not found, reinstall Python with "Add to PATH" checked |
| Plugin doesn't appear in Plugins menu | Manifest not imported, or imported into a different Figma file | Re-import via Plugins → Development → Import plugin from manifest |
| Code returns Figma node object — output looks weird | Node objects can't be JSON-serialized cleanly | Return scalars (`return node.id`) or call `node.exportAsync` for bytes |

## Limits / gotchas

- Plugin runs against the **currently open Figma file**. If you switch files, the plugin closes — re-run it.
- One plugin instance at a time per server. Open the plugin in two Figma windows and the second one is rejected by the server.
- Server binds `127.0.0.1` by default. To expose on LAN: `python bridge.py --host 0.0.0.0` (not recommended — anyone on your LAN can run arbitrary Figma API code).
- Figma plugin sandboxing is enforced — your code runs as if pasted into a plugin's `main`. No Node.js modules, no `require`. All of `figma.*` works.
- Don't quit Figma. If you do, plugin dies — restart Figma → reopen file → Run plugin.

## Files

```
bridge.py            HTTP/WS server (~150 lines)
figmosha.py          CLI client (~80 lines)
plugin/
  manifest.json      Plugin manifest (declares localhost as allowed origin)
  code.js            Plugin sandbox main: evaluates code via new Function(...)
  ui.html            Plugin UI: WebSocket client + log panel
```

Old `run.py` (the Playwright-based v1) is kept in the repo for reference but isn't used in 2.0.
