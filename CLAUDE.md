# Figmosha 2.0 — Claude Code instructions

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
curl -s http://localhost:8787/status   # {"plugin_connected": true/false, "pending": 0}
```

If the bridge isn't running: `bash start-bridge.sh` (runs in tmux `figmosha-bridge`; logs at `/tmp/figmosha-bridge.log`).

If the plugin isn't connected: tell the user — `Plugins → Development → Figmosha Bridge → Run`.

## Helpers (available as `h.*` in every exec)

The plugin runtime exposes a small helper namespace. Use these to keep scripts short:

| Helper | What |
|---|---|
| `await h.bF(node, idx, varOrId)` | Bind fill paint to variable (id or instance) |
| `await h.bS(node, idx, varOrId)` | Bind stroke paint to variable |
| `await h.bN(node, prop, varOrId)` | Bind numeric prop (radius, padding, size...) |
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
- **`teamlibrary permission not specified`** (or similar): manifest needs a new permission. Edit `plugin/manifest.json`, sync it to wherever Figma imports the plugin from (see `CLAUDE.local.md`), then ask the user to **re-import** — Plugins → Development → Manage plugins → remove + Import again. A manifest change needs a re-import, not just a re-Run.
- **Result looks weird / undefined**: you forgot `return`. The wrapper expects a value.
- **Switch Figma file → plugin disconnects**: plugin is bound to the open file. After switching, ask user to Run plugin again.

The error response includes a `hint` field for common cases — read it before debugging.

## Local setup

Machine-specific paths, hosts and sync commands live in `CLAUDE.local.md`,
which is gitignored. If it's missing, ask where the bridge runs and where
Figma reads the plugin from, then write it there.

After editing `plugin/code.js` or `plugin/ui.html`, copy them to wherever
Figma imports the plugin from and ask the user to re-Run it — a running plugin
keeps the code it started with. If `manifest.json` changed, they need to
re-Import, not just re-Run.
