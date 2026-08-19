# Figmosha 2.0

Drive Figma from your terminal / Claude Code / any HTTP client. A tiny custom plugin sits inside Figma Desktop and holds a WebSocket to a local Python server — you send Figma Plugin API code over HTTP and get the result back.

No clipboard hacks. No screenshots.

Fast enough to feel synchronous: reads ~5 ms, mutations ~30 ms, library component import ~150 ms.

<img width="1139" height="875" alt="image" src="https://github.com/user-attachments/assets/9aafc38c-39b8-4657-a7b6-c6fc94ea0805" />

## Why this exists

The Figma Plugin API is the most stable and powerful interface Figma offers. Thousands of plugins depend on it. But typically it's only accessible *inside* Figma's UI — you click "Run plugin", code executes, results appear in a panel.

Figmosha 2.0 keeps a plugin permanently open in Figma and exposes its Plugin API through a local network socket. You write code in your editor / Claude / a script, it runs inside Figma, and the result comes back to you.

```mermaid
flowchart TB
    subgraph client["PowerShell · curl · Claude Code"]
        CLI["figmosha.py<br/>or any HTTP client"]
    end

    subgraph bridge["bridge.py — 127.0.0.1:8787"]
        HTTP["HTTP server<br/>/exec · /status"]
        WSS["WS server<br/>/plugin"]
    end

    subgraph figma["Figma Desktop — open file"]
        PLUGIN["Figmosha Bridge<br/>(plugin)"]
        API["Figma Plugin API"]
    end

    CLI -- "POST /exec<br/>{ code }" --> HTTP
    HTTP --> WSS
    WSS -- "ws://localhost" --> PLUGIN
    PLUGIN --> API

    API -.-> PLUGIN
    PLUGIN -. "result · logs" .-> WSS
    WSS -.-> HTTP
    HTTP -. "{ ok, result, value,<br/>logs, elapsed_ms, hint? }" .-> CLI
```

Solid arrows carry the request, dotted ones the response.

## Highlights

- **One Python file** server + **one Python file** CLI, ~500 lines total. No npm. No frameworks.
- **Custom Figma plugin**, ~250 lines (JS + HTML). Imported in dev mode — no publishing.
- **20 helpers** baked into the plugin runtime as `h.*` so scripts stay short and safe (`h.bF`, `h.setText`, `h.withFonts`, `h.frame`, `h.hex`, `h.sel`, …).
- **11 high-level CLI subcommands** for common ops (`doctor`, `sel`, `tree`, `find`, `text`, `variant`, `clone`, `rm`, `icomp`, …).
- **`figmosha doctor`** walks the whole chain — bridge, plugin, round trip, which file is open — and names the fix at whichever link is broken.
- **Smart error hints** in responses — when a script fails with a known-pattern error, the response includes a `hint` field telling you how to fix it.
- **Works while Figma is minimized.** WebSocket stays alive; JavaScript keeps executing in the background.
- **Auto-reconnect** in the plugin UI — restart the server and the plugin is back within 2 s.
- **Tested without Figma** — a fake plugin drives the real WebSocket, so the bridge's guard, timeouts and slot handover are covered by `pytest`.

## What you can do with it

Anything the Figma Plugin API can do — which is most of what you can do by hand,
minus the clicking. In practice it comes down to the jobs that are miserable
manually because they repeat:

**Read the file.** Walk the node tree, find layers by name, type or their text,
dump a subtree with sizes and auto-layout settings, list local variables and
component sets, read what the user has selected right now.

**Edit content.** Set text on TEXT nodes with fonts loaded automatically —
including bulk passes over a whole subtree, where collecting every unique font
first is otherwise your problem. Rename layers, move and resize, clone next to
the original.

**Build structure.** Create frames, components and component sets, apply
auto-layout, nest and reorder. `h.frame` applies the properties in the order
Figma actually requires, which is not the order you'd guess.

**Work with the design system.** Bind fills, strokes, radii, padding, spacing
and sizes to variables. Import components and variables from a team library by
key. Switch instance variants, and ask what variants are even available.

**Get pixels out.** `node.exportAsync` returns PNG/SVG/PDF bytes; encode them
with `figma.base64Encode` and decode on the client side.

A worked example — build a three-variant button, then bind every colour and
every measurement to design tokens by name:

```bash
python figmosha.py exec --file build-button.js   # structure, hardcoded colours
python figmosha.py exec --file bind-tokens.js    # walk by name, bind variables
python figmosha.py "return h.dumpTree(await h.resolve('sel'), {showLayout:true})"
```

Splitting build from bind is the recommended shape for anything non-trivial:
each half is verifiable on its own, and a failure in the second doesn't leave
you guessing which half broke.

### What it can't do

- **Anything outside an open file.** Each plugin is bound to the Figma file it
  runs in; the bridge can route between files that run the plugin (see
  [Multiple files & concurrency](#multiple-files--concurrency)), but there are
  no cross-file operations inside one script and no file browser.
- **The parts Figma keeps to itself** — publishing to Community, plugin icons,
  account settings, comments (use the REST API for those).
- **Run without Figma Desktop open.** This is a bridge, not a headless renderer.
- **Survive a plugin restart mid-script.** Long operations are not resumable.

## HTTP API

The CLI is a convenience; the wire protocol is a handful of endpoints and no
authentication beyond being on the machine.

| Endpoint | Body | Returns |
|---|---|---|
| `POST /exec` | `{code, timeout?, target?, parallel?}` | `{ok, result, value, logs, elapsed_ms}` |
| `GET /status` | — | `{plugin_connected, files, pending, abandoned}` |
| `GET /targets` | — | `{files: [{name, fileKey, conn}]}` — connected Figma files |
| `POST /clear` | `{target?, force?}` | drops a file's abandoned-script interlock |
| `GET /` | — | service banner listing the endpoints |
| `WS /plugin` | — | where the Figma plugin connects (one per open file) |

```bash
curl -s -X POST http://localhost:8787/exec \
  -H 'Content-Type: application/json' \
  -d '{"code":"return figma.currentPage.name"}'
```

- `result` is your return value stringified; `value` is the same thing raw, when
  it survives JSON.
- `logs` collects everything `print(...)` emitted during the run.
- Failures come back `500` with `{ok: false, error, hint?, stack, logs}`.
- No plugin connected is `503`; a script that outlives its `timeout` is `504`.
- Bodies and WebSocket frames are capped at 16 MB, which is the practical limit
  on how large an export you can pull through in one call.

## Multiple files & concurrency

The bridge holds **one connection per open Figma file** running the plugin, not
one globally. Run the plugin in each file you want to drive; the plugin reports
its identity (`figma.root.name`, and `figma.fileKey` where available), and the
bridge routes by it.

```bash
python figmosha.py targets                          # name / fileKey / conn per file
python figmosha.py exec "return figma.root.name" -T "Component Library"
curl -s -X POST http://localhost:8787/exec -d '{"code":"...","target":"Component Library"}'
```

Target resolution: exact file name (case-insensitive) → exact `fileKey` →
unambiguous substring of the name.

- No target with exactly one file connected routes there — the old behavior.
- No target with two or more connected is `409` ("N files connected — specify a
  target"), and so is an ambiguous substring. Nothing connected stays `503`.
- Two connected windows reporting the same name are refused, not guessed — the
  `409` lists both connections so you can close one.
- Re-running the plugin in a file replaces its stale connection at `hello` time,
  so a re-Run never locks you out.

**Execs are serialized per file.** Each file's plugin sandbox is a
single-threaded async message handler over one shared document and one shared
undo stack — two concurrent scripts interleave at every `await`, invalidating
each other's `findAll` snapshots mid-run. The bridge therefore takes a per-file
lock around `/exec`, so concurrent callers on the same file run one after
another, and callers on different files don't wait on each other. One exec is
one transaction: a read-modify-write split across two calls still lets another
writer land in the gap.

- Read-only scripts can pass `--parallel` (`{"parallel": true}`) to bypass the
  lock and fan out. Reads only — a parallel writer interleaves exactly as before.
- **A `504` does not mean the write didn't happen.** A script already running in
  the sandbox cannot be killed, so on timeout the bridge marks it *abandoned*
  and returns the `504` with a `warning` and the request id. While a file has an
  abandoned script, further execs on it return `409` instead of racing an
  invisible writer. The interlock lifts by itself when the orphan finally
  replies, or manually with `figmosha.py clear -T <file>` (`{"force": true}` on
  `/exec` pushes past it).
- Long loops can cooperate with cancellation: `h.ck()` throws once the bridge
  has abandoned the run, so a chunked sweep calling it each iteration stops
  instead of mutating under the next caller. `h.aborted()` is the non-throwing
  check.

## Security

The bridge executes arbitrary JavaScript inside whichever Figma file you have
open. That makes every web page in your browser part of the threat model —
binding to `127.0.0.1` keeps other machines out, not other tabs.

Two checks handle it:

- **Origin** — local clients (curl, the CLI) never send this header and browsers
  always do on cross-origin requests, so its presence alone means the request
  came from a page, and it's refused. This closes the "simple request" trick of
  posting JSON as `text/plain` to dodge a CORS preflight. The plugin's sandboxed
  iframe reports `null` and is allowed through on the WebSocket only.
- **Host** — pinned to the loopback names actually served, which closes DNS
  rebinding, where a page re-points its own hostname at `127.0.0.1` to become
  same-origin with the bridge and read the responses.

`--host 0.0.0.0` disables the Host check, because the reachable names are then
unknowable. The bridge says so loudly at startup. Don't do it on a network you
share.

## Requirements

- **Figma Desktop** (Stable or Beta) — [download](https://www.figma.com/downloads/). The browser version cannot import local development plugins.
- **Python 3.10+** — for the bridge server and CLI client. Stdlib + a single dependency (`aiohttp`).
- **OS**: macOS, Windows (native or WSL2), or Linux.

## Install

Hand this repo to Claude Code and let it do the setup:

```
https://github.com/denysosadchyi/figmosha2 — set this up for me
```

It clones the repo, creates the venv, installs `aiohttp`, starts the bridge, and tells you what to click in Figma. `CLAUDE.md` in the repo root is written for exactly this — Claude reads it and knows the whole workflow, including the WSL2 path juggling if that's your setup.

Two things Claude cannot do for you, because Figma exposes no API for either:

1. **Import the plugin** — in Figma Desktop: **Plugins → Development → Import plugin from manifest…**, pick `plugin/manifest.json` from the repo. Once, ever.
2. **Run the plugin** — **Plugins → Development → Figmosha Bridge**. A small green **Connected** bar appears; the bridge logs `[plugin] connected from 127.0.0.1`. You're live.

Ask Claude for the smoke test and it will confirm the round trip works end to end.

<details>
<summary>Prefer to do it by hand?</summary>

```bash
git clone https://github.com/denysosadchyi/figmosha2.git
cd figmosha2

python3 -m venv venv && ./venv/bin/pip install aiohttp   # macOS / Linux / WSL
python  -m venv venv && .\venv\Scripts\pip install aiohttp   # Windows

bash start-bridge.sh        # detached tmux session "figmosha-bridge"
./venv/bin/python bridge.py # …or just keep a terminal open
```

The server listens on `127.0.0.1:8787`. Import and run the plugin as described above, then check it:

```bash
./venv/bin/python figmosha.py status
# → {"plugin_connected": true, "pending": 0}

./venv/bin/python figmosha.py "return figma.currentPage.name"
# → "Page 1"
```

**WSL2**: `localhost` ports forward to the Windows host automatically, so a bridge inside WSL is reachable from Figma on Windows. But Figma can only import a plugin from a Windows path — copy it out first:

```bash
mkdir -p /mnt/c/Users/$WIN_USER/figmosha-plugin
cp plugin/* /mnt/c/Users/$WIN_USER/figmosha-plugin/
```

Then import `C:\Users\<your-name>\figmosha-plugin\manifest.json`.

</details>

## Daily use

### Start a session

```bash
bash start-bridge.sh     # macOS / Linux / WSL — detached tmux session
.\start-bridge.ps1       # native Windows — detached, -Restart / -Stop too
# In Figma: Plugins → Development → Figmosha Bridge → Run
```

The bridge survives SSH disconnects and terminal closes (tmux). It does **not** survive OS reboot or WSL shutdown — restart it after either.

### Send code

```bash
# Inline JS
python figmosha.py "return figma.currentPage.children.length"

# From a file
python figmosha.py exec --file my-script.js

# From stdin
cat my-script.js | python figmosha.py exec --stdin

# Plain HTTP (no Python needed)
curl -s http://localhost:8787/exec \
  -H 'Content-Type: application/json' \
  -d '{"code":"return 1+1"}'
```

### High-level CLI commands

When the operation fits one of these, use the dedicated subcommand — much less typing and less risk of escape bugs:

```bash
python figmosha.py doctor                        # diagnose the chain, with fixes
python figmosha.py sel                           # what's selected in Figma right now
python figmosha.py tree 1:23 --depth 2           # dump subtree
python figmosha.py tree sel --layout             # subtree of the selection, with layout
python figmosha.py find 1:23 name=Button         # find by exact name
python figmosha.py find 1:23 name~Btn            # substring name match
python figmosha.py find 1:23 type=INSTANCE       # filter by type
python figmosha.py find 1:23 text~hello          # find TEXT containing "hello"
python figmosha.py text 1:25 "new content"       # set TEXT chars (autoloads fonts)
python figmosha.py variant 1:30 "Property 1=Default"
python figmosha.py clone 1:23 --right --gap 100  # clone adjacent
python figmosha.py rm 1:99 1:100 1:101           # delete one or more nodes
python figmosha.py icomp <component-key>         # import library component, place + zoom
python figmosha.py status                        # bridge + plugin connection state
```

## Code conventions

The plugin wraps your code as:

```js
new Function("figma", "print", "h", `return (async () => { <YOUR CODE> })();`)(figma, print, HELPERS)
```

- `await` works everywhere. Body is wrapped in an async IIFE.
- Whatever you `return` becomes the HTTP response's `result` (string) and `value` (raw JSON-serializable form).
- `print(...)` collects lines into the `logs` array — also streamed to the plugin UI for live debugging.

### Helpers (available as `h.*` in every exec)

| Helper | Use |
|---|---|
| `await h.bF(node, idx, varOrId)` | Bind fill paint at `idx` to variable (handles frozen-array dance) |
| `await h.bS(node, idx, varOrId)` | Bind stroke paint to variable |
| `await h.bN(node, prop, varOrId)` | Bind numeric prop (radius, padding, size, itemSpacing, …) |
| `h.findByName(root, name)` | First descendant with exact name |
| `h.findAllByName(root, name)` | All descendants with exact name |
| `h.dumpTree(node, {maxDepth, showSize, showText, showLayout})` | Indented tree string |
| `await h.withFonts(root, asyncFn)` | Auto-loads every unique font in the subtree, then runs your callback |
| `await h.setText(node, text)` | Sets `node.characters` with auto font load (single-font nodes only) |
| `h.cloneNext(node, {direction, gap, name})` | Clone + place adjacent (`right`/`left`/`up`/`down`) |
| `await h.variant(instance, props)` | Wrapper around `instance.setProperties(...)` |
| `await h.variantsOf(instance)` | `{current, groups, all}` of the component set |
| `h.sel()` | Currently selected nodes as `{id,name,type,w,h}` |
| `h.resolve(idOrAlias)` | Node by id, or the aliases `page` / `sel` |
| `h.hex("#1a2b3c")` | Hex to Figma's 0..1 `{r,g,b}` |
| `h.solid("#1a2b3c", opacity?)` | Ready-to-assign paint array |
| `h.frame(parent, opts)` | Frame with auto-layout applied in the right order |
| `await h.node(id)` | Shorthand for `figma.getNodeByIdAsync(id)` |
| `await h.var_(idOrKey)` | Resolve variable from instance, local id, or library key |
| `await h.importComp(key)` | `figma.importComponentByKeyAsync(key)` |
| `await h.importVar(key)` | `figma.variables.importVariableByKeyAsync(key)` |

Compared to inlined boilerplate, helpers reduce a typical script by ~60–70% and avoid common gotchas (frozen `node.fills`, missing `loadFontAsync`, deprecated sync `getVariableById`).

### Error hints

When a script fails with a recognized pattern, the response includes a `hint` field. The CLI prints it for you:

```
$ figmosha.py "node.characters = 'x'"
figmosha: Cannot write to node with unloaded font "Inter Regular"...
   hint: use h.setText(node, text) or h.withFonts(root, fn) — they autoload fonts
```

Currently hints cover: fills/strokes variable binding, frozen arrays, missing manifest permissions, unloaded fonts, appendChild order, invalid variant values, and a few more.

## Limits / gotchas

- Plugin is bound to the **currently open Figma file**. Switching files closes the plugin — re-Run it in the new file.
- Only **one plugin instance** holds the bridge at a time. A second one is turned away with `Slot busy` — but if the first has gone silent (laptop slept, network changed) the newcomer takes over within about a second, so a genuine reconnect is never locked out.
- **Figma sync errors** ("Unable to establish connection to Figma after 10 seconds") sometimes appear when fetching nodes from non-current pages. If you need cross-page access: `await figma.loadAllPagesAsync()` first.
- Bridge binds to `127.0.0.1` by default. For LAN access: `python bridge.py --host 0.0.0.0` (not recommended — anyone on your LAN can then run arbitrary code in your Figma).
- Manifest changes (new permissions, etc.) require **re-importing** the plugin in Figma. `code.js` and `ui.html` changes are picked up on next Run.

## Troubleshooting

Before reading this table, try `python figmosha.py doctor` — it walks the same
chain and tells you which link is broken.

| Symptom | Cause | Fix |
|---|---|---|
| `connection refused` from CLI | Server not running | `bash start-bridge.sh`, or `.\start-bridge.ps1` on Windows |
| `plugin not connected` (503) | Plugin window closed | Plugins → Development → Figmosha Bridge → Run |
| Plugin says `disconnected, retrying…` | Server is down or restarting | Start it; plugin auto-reconnects within 2 s |
| 504 timeout | Code threw silently or `await` never resolved | Close the plugin (X), Run again. Increase `--timeout` for legitimately long ops |
| `permission not specified in manifest` | API needs a permission not declared in `manifest.json` | Add to `permissions` array, sync to Windows path if applicable, **re-import** plugin |
| `Cannot write to node with unloaded font` | Need to load fonts first | Use `await h.setText(...)` or wrap edits in `h.withFonts(root, fn)` |
| `Cannot assign to read only property` | `node.fills` is frozen | Use `await h.bF(node, idx, varId)` or copy: `JSON.parse(JSON.stringify(node.fills))` |
| `pip install aiohttp` fails on Linux | Python externally-managed environment (PEP 668) | Use the venv approach (always preferred) or `pip install --user --break-system-packages aiohttp` |
| Tmux not installed (Windows native) | `start-bridge.sh` needs bash + tmux | Use `.\start-bridge.ps1` — same thing, detached, with `-Restart` and `-Stop` |
| `403 cross-origin requests are not allowed` | Something is adding an `Origin` header | Talk to the bridge directly, not through a proxy or a browser |
| Plugin shows `Slot busy` | The plugin is already running in another Figma window | Close it there; this one retries every 15 s |
| Helper missing: `h.X is not a function` | The running plugin still has the code it started with | Re-run the plugin in Figma after syncing `plugin/` |

## Project layout

```
bridge.py              HTTP/WS server: origin guard, slot handover, error hints
figmosha.py            CLI client and subcommands
start-bridge.sh        tmux-based bridge management (macOS / Linux / WSL)
start-bridge.ps1       detached launcher for native Windows (-Restart / -Stop)
plugin/
  manifest.json        Permissions + allowed origins
  code.js              Plugin sandbox: exec + the h.* helpers
  ui.html              WS client, auto-reconnect, status bar
  icon.png             128×128, for publishing to Community
tests/
  test_bridge.py       Bridge driven by a fake plugin over a real WebSocket
  helpers.test.js      Pure helpers against a stubbed Figma
CLAUDE.md              Conventions for Claude Code sessions driving Figmosha
CLAUDE.local.md        Your machine's paths and hosts — gitignored, never committed
README.md              This file
```

## Contributing / extending

The plugin runtime is just `new Function("figma", "print", "h", body)`. Add helpers to `HELPERS` in `plugin/code.js`, sync the file to your plugin path, and they're available in your next `exec`.

To add a new CLI subcommand:
1. Add a `cmd_<name>(args)` function in `figmosha.py` that builds JS via `json.dumps`-escaped templates
2. Add a subparser in `build_parser()`
3. Register in the `dispatch` map

To add an error hint:
1. Append a `(needle, hint)` tuple to `ERROR_HINTS` in `bridge.py`
2. Restart the bridge

## Tests

No Figma needed — a fake plugin drives the bridge over a real WebSocket:

```bash
pip install pytest aiohttp
pytest -q            # bridge: guard, exec round trip, timeouts, slot handover
node tests/helpers.test.js   # pure helpers: hex maths, auto-layout ordering
```

## Changelog

See [CHANGELOG.md](CHANGELOG.md). Releases are tagged; after upgrading, re-run the
plugin in Figma so it picks up the new `plugin/code.js`.

## License

MIT
