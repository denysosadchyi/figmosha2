# Figmosha 2.0 — agent instructions

For any coding agent working in this repo: Codex reads this file directly, Claude Code through
`CLAUDE.md` (which just imports it). Machine-specific paths and hosts live in `CLAUDE.local.md`
(gitignored) — **read it first if it exists**, whichever agent you are.

Drive Figma by sending JS code through a local bridge that's connected to a custom plugin running inside Figma Desktop.

## How to send code

```bash
# Preferred — subcommand-style
python figmosha.py exec "return figma.currentPage.name"
python figmosha.py exec --file script.js

# Shorthand (auto-prepends `exec`)
python figmosha.py "return figma.currentPage.name"

# High-level commands (covered below) save tokens for common operations
python figmosha.py text 185:21880 "Привіт"
python figmosha.py variant 185:21883 "Property 1=Default"

# Quick HTTP (no Python needed)
curl -s -X POST http://localhost:8787/exec \
  -H 'Content-Type: application/json' \
  -d '{"code":"return figma.currentPage.id"}'

# Status
curl -s http://localhost:8787/status   # {"plugin_connected": ..., "files": [...], "pending": 0, "abandoned": []}
```

## Multiple files: targets (`-T`) — READ THIS FIRST

The bridge holds **one connection per open Figma file** that is running the plugin, not one globally.

```bash
python figmosha.py targets                      # name / doc id / fileKey / conn / queue (busy, waiting)
curl -s http://localhost:8787/targets

# -T is a per-subcommand flag — it goes AFTER the subcommand, never before it
python figmosha.py exec "return figma.root.name" -T "Component Library"
python figmosha.py tree 123:456 -T "Component Library"
curl -s -X POST http://localhost:8787/exec -d '{"code":"...","target":"Icons"}'
```

`target` matching (`resolve_target` in `bridge.py`): document id (the `doc` column, 6+ chars) →
connection id → exact file name (case-insensitive) → exact `fileKey` → unambiguous substring of the name.

- **Two different files with the same name** (two fresh "Untitled" files, say) → the name is a 409;
  target each by its **document id**: `-T dk2m9q4x`. Each file carries its own id (plugin data
  `figmosha-doc` on the document root, written once on first run), so the bridge never mixes them up,
  and the id survives reconnects and bridge restarts. The `conn` id changes on every reconnect — don't
  store it.

- The name is `figma.root.name` as the plugin reports it, which is often not the name you remember —
  a trailing plural, a rename that never propagated. **Verify with `targets`, don't assume.**
- **`fileKey` targeting does not work for a local dev plugin** — it reports `fileKey: null` (shown as
  `-` in `targets`). A file key is still what `importComponentByKeyAsync` needs; it is just not usable
  as a `-T`. Match by name only.

- **No target + exactly 1 file connected** → routed there (the old default).
- **No target + 2 or more connected** → **HTTP 409** `"N files connected — specify a target"`.
  This is the #1 wasted call. **Always pass `-T`** with the file name from the project profile.
- Ambiguous substring → 409 listing the candidates. Nothing connected → 503.

### Concurrency: the per-file queue

**The bridge queues execs per document.** Requests are multiplexed by request id, and `/exec` waits for
the document's lock before sending — so two callers on the same file (orchestrator + agent, or two
agents) run **one after another, never interleaved**, and callers on *different* files never wait for
each other. The lock belongs to the document, not the connection: one file open in two tabs is one
queue.

**Several agents at once — name yourself and bound your wait:**

```bash
export FIGMOSHA_AGENT=designer            # or --agent designer / -A designer, or "agent" in the JSON
python figmosha.py exec --file build.js -T "Component Library" --queue-timeout 30
```

- `targets` / `/status` show each file's queue: who is running and for how long, who is waiting.
- A reply that waited carries `queued_ms`; the CLI prints `(waited 2.1s in the file's queue)`.
- `--queue-timeout` (JSON `queue_timeout`, default = `--timeout`) caps the wait. When it runs out the
  reply is **503 `file busy`** naming who holds the file, and **nothing was run** — retrying is safe,
  unlike after a 504.
- A caller that hangs up while queued is dropped from the queue; its script never runs.

Why the lock is needed: each file's plugin sandbox is a single-threaded async message handler over one
shared document and one shared undo stack. Without it, two scripts yield to each other at **every
`await`**, invalidating each other's `findAll` snapshots mid-run.

- **Writes: just fire them.** The lock makes concurrent writers safe. **One exec = one transaction** —
  a read-modify-write split across two calls still lets the other writer land in the gap.
- **Reads: pass `--parallel`** (`{"parallel": true}`) to bypass the lock and fan out. **Read-only
  scripts only** — a parallel writer interleaves exactly as before.
- Keep each exec ~10s and chunk sweeps (≤25 nodes). A long script now genuinely blocks that file, so
  other callers wait on the lock and can hit their own timeout.

**A 504 does not mean the write didn't happen.** Nothing can kill a script already running in the
sandbox, so on timeout the bridge marks it **abandoned** and returns 504 with a `warning` and the `rid`.
While a file has an abandoned script, further execs on it return **409** rather than racing an invisible
writer. The interlock lifts by itself when the orphan finally replies (logged `[orphan]`), or manually:

```bash
python figmosha.py clear -T "Component Library"   # drop the interlock
curl -s -X POST http://localhost:8787/clear -d '{"target":"Component Library"}'
```

`{"force": true}` pushes past the interlock if you know the orphan is harmless. `GET /status` reports
`pending` (in-flight) and `abandoned` (`[{rid, conn, age_s}]`).

- **Cooperative cancellation:** chunked sweeps should call **`h.ck()`** each iteration — it throws once
  the bridge has given up on that run, so the loop stops instead of mutating under the next caller.
  (Requires the plugin re-Run after 2026-08-10; harmless on older builds, which ignore `abort`.)
- **The same file open twice is fine** (2026-08-27). Each tab/window gets its own slot; the plugin reports
  a `docSig` (hash of the page ids, since `figma.fileKey` is null for a dev plugin and `figma.root.id`
  is `"0:0"` everywhere), so the bridge can tell two **views of one document** from two **different
  files sharing a name**. Same signature → `-T` routes to the newest live view, and `/status` flags the
  extra with `sameDocAs`. Different signatures → 409, close one; the write would otherwise land in a
  file you did not pick.
- **A live connection is never evicted.** Before this, a `hello` kicked out any same-named connection,
  so one file open in two windows looped forever: each side kicked the other, the loser reconnected
  2s later and kicked back. Only a connection whose socket is already closed is dropped.

If you drive this bridge from several agents, keep your own rules about who may write where. Note the
lock makes concurrent writers **corruption-safe, not conflict-safe**: it orders writes, it cannot tell that
two agents meant to change the same node. Overlapping writers = last write wins, silently. Partition by
**ownership of mains / variants / variables — not by frame or screen**: a main-component or variable edit
propagates file-wide, into frames the other writer has already verified.

If the bridge isn't running: `bash start-bridge.sh` on macOS / Linux / WSL (tmux `figmosha-bridge`,
log `/tmp/figmosha-bridge.log`), or `.\start-bridge.ps1` on native Windows (log `bridge.out.log`).
See [Running the bridge](#running-the-bridge).

If the plugin isn't connected: tell the user — `Plugins → Development → Figmosha Bridge → Run`.

## Helpers (available as `h.*` in every exec)

The plugin runtime exposes a small helper namespace. Use these to keep scripts short:

| Helper | What |
|---|---|
| `await h.bF(node, idx, varOrId)` | Bind fill paint to variable (id or instance) |
| `await h.bS(node, idx, varOrId)` | Bind stroke paint to variable |
| `await h.bN(node, prop, varOrId)` | Bind numeric prop (radius, padding, size...) |
| `h.ck()` | Throws if the bridge abandoned this run — call it each loop iteration in a sweep |
| `h.findByName(root, name)` | First descendant by exact name |
| `h.findAllByName(root, name)` | All descendants by exact name |
| `h.dumpTree(node, {maxDepth, showSize, showText, showLayout})` | Indented tree string |
| `await h.withFonts(root, asyncFn)` | Loads every unique font in subtree, then runs `asyncFn` |
| `await h.setText(node, text)` | Set TEXT node chars with auto font load |
| `h.cloneNext(node, {direction, gap, name})` | Clone + place adjacent (`right`/`left`/`up`/`down`) |
| `await h.variant(instance, props)` | Wrapper around `instance.setProperties(...)` |
| `await h.variantsOf(instance)` | `{ current, groups, all }` for the component set |
| `h.sel()` | Currently selected nodes as `{id,name,type,w,h}` |
| `h.resolve(idOrAlias)` | Node by id, or the aliases `page` / `sel` |
| `h.hex("#1a2b3c")` | Hex to Figma's 0..1 `{r,g,b}` |
| `h.solid("#1a2b3c", opacity?)` | Ready-to-assign paint array |
| `h.frame(parent, opts)` | Frame with auto-layout applied in the right order |
| `await h.node(id)` | Shorthand for `figma.getNodeByIdAsync(id)` |
| `await h.var_(idOrKey)` | Resolve a variable from instance, local id, or library key |
| `await h.importComp(key)` | `figma.importComponentByKeyAsync(key)` |
| `await h.importVar(key)` | `figma.variables.importVariableByKeyAsync(key)` |

**Use them.** Compared to inline boilerplate, helpers save ~70% of the script and avoid common mistakes (frozen `node.fills`, missing `loadFontAsync`, etc.).

### Bad vs good

```js
// Bad — verbose, easy to miss
const f = JSON.parse(JSON.stringify(node.fills));
f[0] = figma.variables.setBoundVariableForPaint(f[0], "color", v);
node.fills = f;

// Good — helper handles freezing + setBoundVariableForPaint
await h.bF(node, 0, v);
```

```js
// Bad — must remember to load fonts first; mixed-font case is silent
await figma.loadFontAsync(node.fontName);
node.characters = "new";

// Good
await h.setText(node, "new");
```

```js
// Bad — manual font collection
const texts = root.findAll(n => n.type === "TEXT");
const fonts = [...new Set(texts.map(t => `${t.fontName.family}|${t.fontName.style}`))];
// ... load each ...

// Good
await h.withFonts(root, async () => {
  // bulk-edit text inside `root` here
});
```

## CLI subcommands (save tokens for common ops)

| Command | Equivalent JS | Use case |
|---|---|---|
| `figmosha doctor` | — | Diagnose bridge → plugin → Figma, with the fix for each break |
| `figmosha sel` | `h.sel()` | What the user has selected right now |
| `figmosha tree <id>` | `h.dumpTree(await h.node(id))` | Explore node structure |
| `figmosha find <id> name=Button` | `(await h.node(id)).findAll(n => n.name === "Button")` | Locate by name |
| `figmosha find <id> name~Btn` | `findAll(n => n.name.includes("Btn"))` | Substring name match |
| `figmosha find <id> type=INSTANCE` | `findAll(n => n.type === "INSTANCE")` | Filter by type |
| `figmosha find <id> text~Привіт` | `findAll(n => n.type === "TEXT" && n.characters.includes(...))` | Find by text |
| `figmosha text <id> "новий"` | `await h.setText(n, "новий")` | Edit text safely |
| `figmosha variant <id> "Property 1=Default"` | `await n.setProperties({...})` | Switch variant |
| `figmosha clone <id> --right --gap 100` | `h.cloneNext(n, {direction:'right',gap:100})` | Duplicate adjacent |
| `figmosha rm <id> [<id>…]` | `n.remove()` | Delete one or more |
| `figmosha icomp <key>` | `(await h.importComp(key)).createInstance()` | Pull from library |

Anywhere an id is taken, `page` and `sel` work too — `figmosha tree sel --layout`
dumps the selected subtree without hunting for its id first.

Use subcommands when the op fits one of these. Fall back to `exec` for anything else.

When the user says "this frame" or "the selected one", call `figmosha sel` — don't
ask them to find an id by hand.

## How exec evaluates code

```js
new Function("figma", "print", "h", `return (async () => { <YOUR CODE> })();`)(figma, print, HELPERS)
```

- `return ...` becomes the `result` field of the response (stringified + raw `value` if JSON-serializable).
- `await` works everywhere.
- `print(...)` collects log lines (returned in the `logs` array; also streamed to plugin UI).
- Exceptions → `{ok:false, error, hint?, stack, logs}` with HTTP 500.

The bridge **adds a `hint` field** when it recognizes a common error (fills/strokes binding, frozen array, font not loaded, missing permission, appendChild order, variant typo). Pay attention to it.

## Conventions

### Use async APIs

The plugin runs under dynamic-page documentAccess where lookups are async:

```js
const node = await figma.getNodeByIdAsync(id)        // or: await h.node(id)
const main = await instance.getMainComponentAsync()
const cols = await figma.teamLibrary.getAvailableLibraryVariableCollectionsAsync()
const comp = await figma.importComponentByKeyAsync(key)  // or: await h.importComp(key)
```

### Auto-layout: order matters

`resize()` / spacing / sizing modes are ignored if set before `layoutMode`:

```js
const f = figma.createFrame()
parent.appendChild(f)            // 1. into tree first
f.layoutMode = "VERTICAL"        // 2. layoutMode
f.resize(400, 100)               // 3. size
f.primaryAxisSizingMode = "AUTO" // 4. sizing
f.itemSpacing = 16               // 5. spacing/padding
f.paddingTop = 24
```

`h.frame` does all of that in the right order — prefer it:

```js
const f = h.frame(parent, {
  layout: "V", spacing: 16, padding: [24, 16],
  fill: "#ffffff", radius: 8, name: "Card",
})
```

### Two-stage workflow for big builds

For complex builds (component sets with many variants + variable binding): split into Step 1 = build structure with hardcoded RGB; Step 2 = walk nodes by `name` and bind via `h.bF`/`h.bS`/`h.bN`. Verify each step independently.

Name nodes in Step 1 so Step 2 can `h.findByName(root, "...")` them.

### Don't take screenshots for verification

The bridge returns the data you need. Verify by:

```js
return (await h.node("...")).width
return root.findAll(n => n.type === "TEXT").map(t => t.characters)
```

`node.exportAsync({format:"PNG"})` exists if you genuinely need pixels — returns bytes. Don't use it as "is the code working" check.

## When something looks wrong

- **`plugin not connected` (503)**: plugin window closed in Figma. Ask user to Run it again.
- **Timeout (504)**: probably infinite loop or unresolved `await`. Ask user to close & re-run plugin.
- **`teamlibrary permission not specified`** (or similar): manifest needs a new permission. Edit `plugin/manifest.json` in place (that is the file Figma loads, unless `CLAUDE.local.md` says it is copied elsewhere), then ask the user to **re-import** the plugin (Plugins → Development → Manage plugins → remove + Import again).
- **Result looks weird / undefined**: you forgot `return`. The wrapper expects a value.
- **Switch Figma tab → the plugin keeps running.** Each tab holds its own bridge connection, and a
  background tab still answers `exec` — **tabs in one window work; separate windows are not required**
  (verified 2026-08-27: 4 files answered while Figma itself was unfocused). What is still per-document:
  the plugin has to be **Run once in each tab** — `⌘⌥P` re-runs the last plugin in the current tab.
  If a tab stops answering, Run it again there; the bridge slot re-registers by file name on reconnect.

The error response includes a `hint` field for common cases — read it before debugging.

## Running the bridge

Setup, once: `python -m venv venv`, then `pip install -r requirements.txt` with the venv's pip
(`./venv/bin/pip` on macOS / Linux / WSL, `.\venv\Scripts\pip` on Windows). Tests need
`requirements-dev.txt`.

**macOS / Linux / WSL**

```bash
bash start-bridge.sh                 # start or restart (kills the old tmux session first)
tmux attach -t figmosha-bridge       # watch it
tmux kill-session -t figmosha-bridge # stop it
# log: /tmp/figmosha-bridge.log
```

**Native Windows** — no bash or tmux needed:

```powershell
.\start-bridge.ps1            # start, detached (no-op if already running)
.\start-bridge.ps1 -Restart   # after editing bridge.py
.\start-bridge.ps1 -Stop
# logs: bridge.out.log, bridge.err.log next to the script
```

Windows gotchas, all hit in practice:

- If PowerShell says *running scripts is disabled*, run it as
  `powershell -ExecutionPolicy Bypass -File .\start-bridge.ps1`.
- `start-bridge.ps1` must stay **pure ASCII** — Windows PowerShell 5.1 reads a BOM-less script as ANSI,
  and one em dash or arrow breaks parsing of the whole file.
- `python` resolving to `...\WindowsApps\python.exe` is the Microsoft Store stub, not Python.
- In Windows PowerShell `curl` is `Invoke-WebRequest`; use `curl.exe` or `python figmosha.py`.

**Where Figma loads the plugin from.** Import `plugin/manifest.json` straight from the repo, and Figma
reads `plugin/` in place — an edit is live after a re-Run, with **no copy step**. The exception is a
bridge inside WSL: Figma needs a Windows path, so `plugin/` is copied out (README → Install → WSL2).
Record anything machine-specific (paths, copies, hooks) in `CLAUDE.local.md`.

The plugin must be started by hand **once per open file (tab or window — either works)**; `⌘⌥P`
(`Ctrl+Alt+P` on Windows) re-runs the last plugin in the tab you are on.

After editing `plugin/code.js` or `plugin/ui.html`: **bump `PLUGIN_VERSION`** at the top of
`plugin/code.js` (date + counter, e.g. `2026-10-02.2`) and run `pytest tests/test_plugin_version.py` — it
prints the line to add to `tests/plugin_fingerprint.json`, and fails if you forget the bump. Then ask the
user to re-Run the plugin (Plugins → Development → Figmosha Bridge). After editing
`plugin/manifest.json` (e.g. adding a permission): ask them to **re-import** it (Plugins → Development →
Manage plugins → remove, then Import from `plugin/manifest.json`).

**Stale code is reported, so act on it.** The bridge compares each plugin's build with `plugin/code.js`
on disk and its own `bridge.py` with the one it started from. When either is stale, every `/exec` reply
carries a `notice` (the CLI prints it as `⚠`), `/status` flags the file with `outdated: true`, the
plugin bar turns purple with "New version: re-run plugin", and `figmosha doctor` names the fix. Tell
the user — don't keep working against old code.

**Newer Figmosha on GitHub.** Every 6 hours the bridge asks GitHub how far this checkout is behind
`master`. If it is, `/status` has `update: {behind, url}`, `/exec` replies carry a `notice`, the plugin
bar shows "New version" with an Update button (opens the CHANGELOG), and `doctor` prints a `!` line.
Mention it to the user once; updating is `git pull`, restart the bridge, re-run the plugin. Off with
`FIGMOSHA_NO_UPDATE_CHECK=1`.
