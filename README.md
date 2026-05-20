# Figmosha 2.0

Drive Figma from a CLI / Claude Code / any HTTP client. Plugin sits inside Figma Desktop and holds an open WebSocket to a tiny local server; you send Plugin API code over HTTP and get the result back.

No Playwright. No browser automation. No screenshots. ~150–950× faster than v1 on real operations (reads ~5 ms, mutations ~30 ms).

## Architecture

```
PowerShell / curl / figmosha.py        bridge.py (Python)       Figma Desktop
─────────────────────────              ─────────────────        ─────────────
                                                                  ┌────────────┐
   POST /exec  ─────────────────►   HTTP server                   │ open file  │
                                       │                          │            │
                                       ▼                          │ ┌────────┐ │
                                   WS server  ──── ws://localhost ┼─┤Figmosha│ │
                                                                  │ │ Bridge │ │
                                       ▲                          │ │(plugin)│ │
                                       │                          │ └───┬────┘ │
   ◄────────────────────  HTTP response                           │     │      │
        {ok, result, value, logs, elapsed_ms}                     │     ▼      │
                                                                  │ Plugin API │
                                                                  └────────────┘
```

## What's in here

| File | Purpose |
|---|---|
| `bridge.py` | Local server. HTTP `/exec`, `/status` + WS `/plugin`. Pure Python + aiohttp. |
| `figmosha.py` | CLI client. `figmosha.py "return ..."` → result. Stdlib-only. |
| `plugin/manifest.json` | Figma plugin manifest. Declares localhost as allowed origin. |
| `plugin/code.js` | Plugin sandbox: `new Function(code)(figma, print)` → returns result + logs. |
| `plugin/ui.html` | Plugin UI: WebSocket client, auto-reconnect, log panel. |
| `start-bridge.sh` | Restart bridge cleanly in a tmux session. |
| `SETUP-WINDOWS.md` | Step-by-step Windows setup (Figma Desktop + WSL bridge). |
| `CLAUDE.md` | Conventions for Claude Code sessions driving Figmosha. |

## Quick start

See `SETUP-WINDOWS.md` for full install.

```bash
# In WSL (or any Linux/macOS with python3.10+):
python3 -m venv venv
./venv/bin/pip install aiohttp
bash start-bridge.sh

# In Figma Desktop:
#   Plugins → Development → Import plugin from manifest…
#   pick plugin/manifest.json
#   Plugins → Development → Figmosha Bridge → Run
#   (plugin UI should show "connected" in green)

# Anywhere with HTTP:
python figmosha.py "return figma.currentPage.name"
curl -s http://localhost:8787/exec -d '{"code":"return 1+1"}' -H 'Content-Type: application/json'
```

## What you can do

```js
// Read the file
figmosha.py "return figma.root.children.map(p => p.name)"

// Walk the page
figmosha.py "return figma.currentPage.findAll(n => n.type === 'INSTANCE').length"

// Mutate
figmosha.py "const r = figma.createRectangle(); r.x=100; r.y=100; r.resize(200,100); return r.id"

// Use library components
figmosha.py "
  const comp = await figma.importComponentByKeyAsync('<key>');
  const inst = comp.createInstance();
  figma.currentPage.appendChild(inst);
  return inst.id;
"

// Variables from connected libraries
figmosha.py "return (await figma.teamLibrary.getAvailableLibraryVariableCollectionsAsync()).map(c => c.name)"

// Override variants
figmosha.py "
  const node = await figma.getNodeByIdAsync('<id>');
  await node.setProperties({'Property 1': 'Default'});
  return 'ok';
"
```

## License

MIT
