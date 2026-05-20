# Figmosha 2.0 — Claude Code instructions

Drive Figma by sending JS code through a local bridge that's connected to a custom plugin running inside Figma Desktop.

## How to send code

```bash
# Inline
python figmosha.py "return figma.currentPage.name"

# Multi-line (heredoc / file)
python figmosha.py --file script.js

# Quick HTTP (no Python needed)
curl -s -X POST http://localhost:8787/exec \
  -H 'Content-Type: application/json' \
  -d '{"code":"return figma.currentPage.id"}'

# Status
curl -s http://localhost:8787/status   # {"plugin_connected": true/false, "pending": 0}
```

If the bridge isn't running: `bash start-bridge.sh` (runs in tmux session `figmosha-bridge`; logs at `/tmp/figmosha-bridge.log`).

If the plugin isn't connected: tell the user to open it — `Plugins → Development → Figmosha Bridge → Run`.

## How code is evaluated

```js
new Function('figma', 'print', `return (async () => { <YOUR CODE> })();`)(figma, print)
```

- Whatever you `return` becomes the `result` field of the HTTP response (stringified for display; raw value also returned as `value` if JSON-serializable).
- `await` works everywhere — body is wrapped in an async IIFE.
- `print(...args)` collects log lines (returned in the `logs` array).
- Exceptions become `{ok:false, error, stack}` with HTTP 500.

## Conventions

### Just `return` — no print dance

```js
// good
return figma.currentPage.children.map(n => ({id:n.id, name:n.name, type:n.type}))

// avoid (v1 leftover style — print() exists but is rarely needed)
print("page name:", figma.currentPage.name)
```

### Use Async APIs

The plugin runs under documentAccess: dynamic-page where most node lookups are async:

```js
const node = await figma.getNodeByIdAsync(id)
const main = await instance.getMainComponentAsync()
const cols = await figma.teamLibrary.getAvailableLibraryVariableCollectionsAsync()
const comp = await figma.importComponentByKeyAsync(key)
```

`figma.getNodeById` (sync) still works for already-loaded nodes but prefer the async forms.

### Auto-Layout: order matters

`resize()` / spacing / sizing modes are ignored if set before `layoutMode`. Strict order:

```js
const f = figma.createFrame()
parent.appendChild(f)            // 1. into tree first
f.layoutMode = "VERTICAL"        // 2. layoutMode
f.resize(400, 100)               // 3. size
f.primaryAxisSizingMode = "AUTO" // 4. sizing
f.itemSpacing = 16               // 5. spacing/padding
f.paddingTop = 24
```

### Font loading before text ops

```js
await figma.loadFontAsync(node.fontName)
node.characters = "..."
```

Mixed-font text: load each unique font in the range or set the whole thing to a single font first.

### Fills binding (variables)

`node.setBoundVariable("fills", 0, v)` does NOT work. Use the paint helper:

```js
const f = JSON.parse(JSON.stringify(node.fills))   // unfreeze
f[0] = figma.variables.setBoundVariableForPaint(f[0], "color", v)
node.fills = f
```

For numeric props (radii, padding, size) — `node.setBoundVariable("topLeftRadius", v)` works directly.

### Two-stage workflow for big builds

For complex builds (component sets with many variants + variable binding): split into Step 1 = build visual structure with hardcoded RGB; Step 2 = walk nodes by `name` and bind variables. Mixing both in one call still sometimes fails silently — easier to verify each step.

Name nodes meaningfully in Step 1 so Step 2 can `findOne(n => n.name === "...")` them.

### Variant changes on instances

```js
await instance.setProperties({"Property 1": "Default"})
```

Look up valid values via the component set:

```js
const main = await instance.getMainComponentAsync()
return main.parent.variantGroupProperties   // {"Property 1": {values: [...]}}
```

### Reading is cheap, mutating is moderate

Roundtrip overhead: ~3–7 ms (read), ~30–80 ms (single mutation including Figma render). Batch mutations into one `/exec` call when possible — fewer roundtrips, but the bigger win is that Figma collapses them into one undo step.

### Don't take screenshots for verification

The bridge returns the data you need. Verify by:

```js
// Verify what was created/changed
return figma.currentPage.findOne(n => n.name === "...").width
return root.findAll(n => n.type === "TEXT").map(t => t.characters)
```

`node.exportAsync({format:"PNG"})` exists if you genuinely need pixels — returns bytes you can save server-side. Don't use it as a "is the code working" check.

## When something looks wrong

- **`plugin not connected` (503)**: plugin window closed in Figma. Ask user to Run it again.
- **Timeout (504)**: probably an infinite loop or unresolved `await`. Ask user to close & re-run the plugin.
- **`teamlibrary permission not specified`** (or similar): manifest needs a new permission. Edit `plugin/manifest.json`, sync to user's Windows copy (`/mnt/c/Users/User/figmosha-plugin/manifest.json` on their WSL), ask user to **re-import** the plugin (Plugins → Development → Manage plugins in development → remove → Import again).
- **Result looks weird / undefined**: you forgot `return`. The wrapper expects a value.
- **Switch Figma file → plugin disconnects**: plugin is bound to the open file. After switching, ask user to Run plugin again.

## Where things live (user's setup)

- Bridge: `~/figmosha2/` on WSL Ubuntu at `192.168.31.105` (passwordless ssh as `user`)
- Plugin source: `~/figmosha2/plugin/`
- Plugin Windows-side (for Figma to import): `C:\Users\User\figmosha-plugin\`
- Tmux session: `figmosha-bridge`
- Log: `/tmp/figmosha-bridge.log` on WSL

To restart bridge from this dev machine: `ssh user@192.168.31.105 'bash ~/figmosha2/start-bridge.sh'`.
