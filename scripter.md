---
name: figma-scripter
description: >
  Use this skill whenever the user wants to write, debug, or generate Figma Scripter scripts
  (TypeScript/JavaScript that runs inside the Figma Scripter plugin). Triggers include: any mention
  of "Scripter", "figma script", "написати скрипт для фігми", creating nodes/frames/components
  programmatically in Figma, extracting component data via Scripter, applying tokens/variables via
  script, or automating Figma canvas operations. Always use this skill when generating Scripter code —
  it contains critical rules that prevent runtime crashes and silent failures.
---

# Figma Scripter Skill

Guidelines for writing reliable, crash-free Figma Scripter scripts. Apply all rules below
unconditionally — they exist because Scripter fails silently or throws cryptic errors when violated.

---

## Script Structure

Every script must follow this skeleton:

```ts
async function main() {
  try {
    // all logic here
    figma.notify("✅ Done");
  } catch (e: any) {
    figma.notify("❌ " + e.message, { error: true });
    console.error(e);
  }
}

main();
```

- Always `async` — Scripter requires `await` for font loading and some API calls.
- Always wrap in `try/catch` — Scripter does not surface stack traces well; `figma.notify` is the only visible error feedback.
- Always call `main()` at the bottom — Scripter executes top-level code synchronously.

---

## Rule 1 — Load Fonts Before Any Text Operation

**Every** font used in any `TextNode` must be loaded before setting `characters`, `fontSize`,
`fontName`, `textStyleId`, or any other text property.

```ts
// CORRECT
await figma.loadFontAsync({ family: "Inter", style: "Regular" });
await figma.loadFontAsync({ family: "Inter", style: "Bold" });

const t = figma.createText();
t.fontName = { family: "Inter", style: "Bold" };
t.characters = "Hello";

// WRONG — crashes with "Font not loaded"
const t = figma.createText();
t.characters = "Hello"; // ❌
```

**Custom/team fonts (Gilroy, etc.):** when editing existing text nodes, always read the font
from the node itself — never hardcode the font name. Use `.then()` instead of `await` for
reliability with custom fonts:

```ts
// CORRECT — read font from node, use .then()
const n = figma.getNodeById("109:1743");
figma.loadFontAsync(n.fontName).then(() => {
  n.characters = "New text";
  figma.notify("OK: " + n.characters);
}).catch(e => figma.notify("ERR: " + e.message, {error: true}));

// WRONG — hardcoding custom font name may fail silently
await figma.loadFontAsync({ family: "Gilroy", style: "Medium" }); // ❌ may not resolve
```

If multiple fonts are needed, load them all in parallel:

```ts
await Promise.all([
  figma.loadFontAsync({ family: "Inter", style: "Regular" }),
  figma.loadFontAsync({ family: "Inter", style: "Bold" }),
  figma.loadFontAsync({ family: "Inter", style: "Italic" }),
]);
```

---

## Rule 2 — Append to Scene Before Setting Properties

Add a node to the scene tree **before** calling `resize()`, setting `layoutMode`, or other
structural properties. Some properties silently no-op if the node has no parent.

```ts
// CORRECT
const frame = figma.createFrame();
figma.currentPage.appendChild(frame); // first
frame.resize(375, 812);               // then
frame.layoutMode = "VERTICAL";

// WRONG
const frame = figma.createFrame();
frame.resize(375, 812);               // ❌ may be ignored
figma.currentPage.appendChild(frame);
```

---

## Rule 3 — Auto Layout Property Order

Set `layoutMode` first. All other Auto Layout properties depend on it being active.

```ts
frame.layoutMode = "VERTICAL";        // 1. activate
frame.primaryAxisSizingMode = "AUTO"; // 2. then sizing
frame.counterAxisSizingMode = "FIXED";
frame.itemSpacing = 16;
frame.paddingTop = 24;
frame.paddingBottom = 24;
frame.paddingLeft = 16;
frame.paddingRight = 16;
```

Setting `itemSpacing` before `layoutMode = "VERTICAL"` has no effect.

---

## Rule 4 — Colors Use 0–1 RGB, Not Hex

```ts
// CORRECT
node.fills = [{ type: "SOLID", color: { r: 0.2, g: 0.5, b: 1 } }];

// WRONG — Figma API does not accept hex strings
node.fills = [{ type: "SOLID", color: "#3380FF" }]; // ❌
```

Helper for hex conversion:

```ts
function hex(h: string): RGB {
  return {
    r: parseInt(h.slice(1, 3), 16) / 255,
    g: parseInt(h.slice(3, 5), 16) / 255,
    b: parseInt(h.slice(5, 7), 16) / 255,
  };
}

node.fills = [{ type: "SOLID", color: hex("#3380FF") }];
```

---

## Rule 5 — Type-Check Before Accessing Node Properties

Not all nodes have `fills`, `fontSize`, `children`, etc. Always guard:

```ts
if (node.type === "TEXT") {
  (node as TextNode).fontSize = 16;
}

if ("fills" in node) {
  (node as GeometryMixin).fills = [...];
}

if ("children" in node) {
  for (const child of (node as ChildrenMixin).children) {
    // ...
  }
}
```

---

## Rule 6 — Cloning and Moving Nodes

`clone()` creates a node but leaves it in the same parent. Always `appendChild` to move it:

```ts
const clone = sourceNode.clone();
targetFrame.appendChild(clone); // moves AND attaches
clone.x = 0;
clone.y = 0;
```

To move (not clone) a node to another parent:

```ts
targetFrame.appendChild(node); // reparents in place
```

---

## Rule 7 — Variable / Token Binding

When applying Figma Variables (for design token migration pipelines):

### Binding COLOR variables to fills/strokes

**`setBoundVariable("fills", 0, v)` does NOT work for fills/strokes.** It throws:
`"fills and strokes variable bindings must be set on paints directly"`

Use `figma.variables.setBoundVariableForPaint()` instead:

```ts
// CORRECT — bind color variable to fill
const fills = JSON.parse(JSON.stringify(node.fills));
fills[0] = figma.variables.setBoundVariableForPaint(fills[0], "color", variable);
node.fills = fills;

// CORRECT — bind color variable to stroke
const strokes = JSON.parse(JSON.stringify(node.strokes));
strokes[0] = figma.variables.setBoundVariableForPaint(strokes[0], "color", variable);
node.strokes = strokes;

// WRONG — crashes on fills/strokes
node.setBoundVariable("fills", 0, variable); // ❌
```

**Important:** `JSON.parse(JSON.stringify(...))` is required because `node.fills` returns
a frozen array. Mutating it directly throws.

### Binding FLOAT variables to numeric properties

`setBoundVariable` works fine for non-paint properties:

```ts
// Bind radius
node.setBoundVariable("topLeftRadius", variable);
node.setBoundVariable("topRightRadius", variable);
node.setBoundVariable("bottomLeftRadius", variable);
node.setBoundVariable("bottomRightRadius", variable);

// Bind size
node.setBoundVariable("width", variable);
node.setBoundVariable("height", variable);

// Bind other numeric props
node.setBoundVariable("strokeWeight", variable);
node.setBoundVariable("opacity", variable);
node.setBoundVariable("paddingTop", variable);
```

### Building a variable lookup map

```ts
const V = {};
for (const col of figma.variables.getLocalVariableCollections())
  for (const id of col.variableIds) {
    const v = figma.variables.getVariableById(id);
    if (v) V[v.name] = v;
  }
// Usage: V["semantic/primary"], V["radius/xl"], V["spacing/4"]
```

### Helper functions (copy-paste ready)

```ts
function bF(node, varName) {
  const v = V[varName]; if (!v || !node) return;
  const f = JSON.parse(JSON.stringify(node.fills));
  if (!f.length) f.push({type:"SOLID", color:{r:.5,g:.5,b:.5}});
  f[0] = figma.variables.setBoundVariableForPaint(f[0], "color", v);
  node.fills = f;
}

function bS(node, varName) {
  const v = V[varName]; if (!v || !node) return;
  const s = JSON.parse(JSON.stringify(node.strokes));
  if (!s.length) return;
  s[0] = figma.variables.setBoundVariableForPaint(s[0], "color", v);
  node.strokes = s;
}

function bN(node, prop, varName) {
  const v = V[varName]; if (!v || !node) return;
  node.setBoundVariable(prop, v);
}
```

---

## Rule 8 — Exporting Data (YAML/JSON via console)

Scripter has no file-write access. To export data, print to console and copy from the Scripter
output panel:

```ts
const output: Record<string, any>[] = [];

for (const node of figma.currentPage.selection) {
  output.push({
    id: node.id,
    name: node.name,
    type: node.type,
    // ... other fields
  });
}

console.log(JSON.stringify(output, null, 2));
// or for YAML-like output:
console.log(output.map(n => `- id: ${n.id}\n  name: ${n.name}`).join("\n"));
```

---

## Rule 9 — Selection and Viewport

Always restore selection state and zoom to result after script runs:

```ts
const created: SceneNode[] = [];

// ... create nodes, push to created[]

figma.currentPage.selection = created;
figma.viewport.scrollAndZoomIntoView(created);
figma.notify(`✅ Created ${created.length} nodes`);
```

---

## Rule 10 — Iterating Mixed Selections Safely

```ts
for (const node of figma.currentPage.selection) {
  // Safe: works on any node
  console.log(node.name, node.type);

  // Guarded: only for frames/groups/components
  if ("children" in node) {
    for (const child of node.children) {
      // process child
    }
  }
}
```

---

## Complete Working Template

```ts
async function main() {
  const page = figma.currentPage;
  const created: SceneNode[] = [];

  // 1. Load all fonts first
  await Promise.all([
    figma.loadFontAsync({ family: "Inter", style: "Regular" }),
    figma.loadFontAsync({ family: "Inter", style: "Medium" }),
  ]);

  // 2. Create frame and append before setting properties
  const frame = figma.createFrame();
  page.appendChild(frame);
  frame.name = "MyFrame";
  frame.resize(375, 812);
  frame.layoutMode = "VERTICAL";
  frame.primaryAxisSizingMode = "AUTO";
  frame.counterAxisSizingMode = "FIXED";
  frame.itemSpacing = 16;
  frame.paddingTop = 24;
  frame.paddingBottom = 24;
  frame.paddingLeft = 16;
  frame.paddingRight = 16;
  frame.fills = [{ type: "SOLID", color: { r: 1, g: 1, b: 1 } }];

  // 3. Create text node
  const title = figma.createText();
  frame.appendChild(title);
  title.fontName = { family: "Inter", style: "Medium" };
  title.fontSize = 24;
  title.characters = "Hello Figma";
  title.fills = [{ type: "SOLID", color: { r: 0.1, g: 0.1, b: 0.1 } }];

  created.push(frame);

  // 4. Zoom to result
  figma.currentPage.selection = created;
  figma.viewport.scrollAndZoomIntoView(created);
  figma.notify(`✅ Done — ${created.length} node(s) created`);
}

main();
```

---

## Rule 11 — Components and Instances

Create components off-screen in a `_Components` frame, then use `createInstance()` on the page.

### Creating components — окрема сторінка "Components"

Компоненти зберігаються на **окремій сторінці** "Components", не на робочій сторінці.
Кожен компонент розкладений з відступами, не в купі.

```ts
// Знайти або створити сторінку Components
let compPage = figma.root.children.find(p => p.name === "Components");
if (!compPage) {
  compPage = figma.createPage();
  compPage.name = "Components";
}

// Створити компонент на сторінці Components
const inputComp = figma.createComponent();
compPage.appendChild(inputComp);
inputComp.name = "Input";
inputComp.resize(332, 48);  // ALWAYS set explicit w×h
inputComp.x = 0;
inputComp.y = 0;
// ... properties

const btnComp = figma.createComponent();
compPage.appendChild(btnComp);
btnComp.name = "Button";
btnComp.resize(332, 48);
btnComp.x = 0;
btnComp.y = 100;  // відступ від попереднього
// ... properties
```

**Правила:**
- Сторінка називається "Components" (єдина, спільна для всіх компонентів)
- Кожен компонент на окремому рядку (y += попередній.height + 40)
- `resize()` завжди з explicit width × height
- Перед створенням перевірити чи компонент вже існує: `compPage.findOne(n => n.name === 'Input')`
- Інстанси працюють крос-сторінково — `inputComp.createInstance()` на будь-якій сторінці

**Cross-page створення:**
- `compPage.appendChild(component)` НЕ працює для переносу між сторінками
- Правильно: `figma.currentPage = compPage` ПЕРЕД `figma.createComponent()`
- Після створення: `figma.currentPage = origPage` щоб повернутись

**Фіксація ширини з auto layout:**
- Горизонтальний компонент з layoutMode='HORIZONTAL' обтискає контент за замовчуванням
- Щоб зберегти ширину: `primaryAxisSizingMode = 'FIXED'` ПІСЛЯ resize()
- Без цього resize() ігнорується і ширина = сума дітей

### Using instances

```ts
const email = inputComp.createInstance();
card.appendChild(email);
email.layoutAlign = "STRETCH";
email.resize(332, 48);  // re-affirm size on instance too
```

### Changing text in instances

Do NOT use `setProperties()` — property names have unpredictable `#id` suffixes.
Instead, find the text node and change `characters` directly:

```ts
function setLabel(instance: InstanceNode, text: string) {
  const t = instance.findOne(n => n.type === "TEXT") as TextNode;
  if (t) t.characters = text;
}

setLabel(email, "Email address");
```

Font must already be loaded before calling `setLabel`.

---

## Rule 12 — No Screenshots by Default

When using `run.py`, don't take screenshots unless the user asks to see the result.
`figma.notify("✅ Done")` in try/catch confirms success. The server prints "OK" on success.
This saves ~1000 tokens per operation.

---

## Rule 13 — Reading Canvas Tree Without Screenshots

To see what's on the canvas, run a dump script. Output goes to `output.txt`:

```ts
const lines = [];
function dump(n, d) {
  const pad = "  ".repeat(d);
  const sz = "width" in n ? Math.round(n.width) + "x" + Math.round(n.height) : "";
  lines.push(pad + n.name + " [" + n.type + "] " + sz);
  if ("children" in n && n.type !== "INSTANCE") n.children.forEach(c => dump(c, d + 1));
}
figma.currentPage.children.filter(c => c.name[0] !== "_").forEach(c => dump(c, 0));
print(lines.join("\n"));
```

Then read `output.txt` (not `result.png`) for the tree. Cheaper than screenshot.
INSTANCE children are skipped (they mirror the component).

---

## Rule 14 — Two-Step Approach: Create First, Bind Second

When building complex layouts with variable bindings, **always split into two scripts**:

**Step 1 — Create the visual structure** with hardcoded colors/values:

```ts
// Use literal RGB values, no setBoundVariable calls
rect.fills = [{type:"SOLID", color:{r:0, g:0.58, b:0.52}}];
rect.cornerRadius = 8;
text.fills = [{type:"SOLID", color:{r:0.05, g:0.05, b:0.05}}];
```

**Step 2 — Walk the tree and bind variables** to existing nodes:

```ts
// Find nodes by name, type, or position and bind
const rect = section.findOne(n => n.name === "Primary" && n.type === "RECTANGLE");
bF(rect, "semantic/primary"); // helper from Rule 7
bN(rect, "topLeftRadius", "radius/xl");
```

**Why two steps:**
- Mixing creation + binding in one large script makes debugging impossible —
  if binding fails mid-way, you lose all visual work too
- Step 1 produces a verifiable visual result (dump tree, check screenshot)
- Step 2 can be re-run independently if bindings fail
- Smaller scripts are more reliable in Scripter (clipboard paste + eval)

**Naming convention for Step 2:** give meaningful `name` properties to frames/nodes
in Step 1 so Step 2 can find them with `findOne(n => n.name === "...")`.

---

## Rule 15 — Run Propstar After Creating Component Sets

After `figma.combineAsVariants()` creates a Component Set, **always** run the Propstar plugin
to arrange variants into a clean property table:

```bash
# 1. Select the component set (via Scripter)
python run.py --file select_component.js

# 2. Run Propstar (closes Scripter, opens plugin, clicks "Create property table")
python run.py "__plugin__:Propstar > Create property table"

# 3. Wait ~15s for Propstar to finish, then re-open Scripter
sleep 15
python run.py "__reopen_scripter__"
```

**What Propstar does:**
- Arranges all component variants in a visual grid/table by property values
- Labels rows and columns with property names (Variant, Size, State, etc.)
- Makes the component set readable and professional

**When NOT to use:** If the component has fewer than 4 variants, Propstar adds no value.

---

## Common Errors and Fixes

| Error | Cause | Fix |
|---|---|---|
| `Font not loaded` | Text property set before `loadFontAsync` | Load font first, always |
| `resize is not a function` | Node not yet in scene | `appendChild` before `resize` |
| `Cannot read property 'fills'` | Wrong node type | Guard with `"fills" in node` |
| Silent no-op on Auto Layout | `itemSpacing` set before `layoutMode` | Set `layoutMode` first |
| Clone stays on top of original | `clone()` without `appendChild` | Always `appendChild(clone)` to target |
| Variable not binding | Wrong variable ID format | Use `figma.variables.getVariableById(id)` and check result |
| `setProperties` crash | Property name needs `#id` suffix | Use `findOne(n=>n.type==='TEXT').characters` instead |
| Instance has 0 size | Component created without `resize()` | Always `resize(w, h)` on components AND instances |
| Components visible on canvas | Created on working page | Create on separate "Components" page |
| `$` disappears in text | Shell escapes `$` in inline code | Use `--file` for scripts with `$`, or `const D='$'` workaround |
| Components deleted by cleanup | `children.forEach(remove)` on same page | Components on separate "Components" page — safe from cleanup |
| Script silently fails mid-way | try/catch swallows error, partial result | Check `output.txt` tree to verify all nodes created |
| `fills and strokes variable bindings must be set on paints directly` | `setBoundVariable("fills", 0, v)` on fills/strokes | Use `figma.variables.setBoundVariableForPaint()` instead (Rule 7) |
| `Cannot assign to read only property` on fills | Mutating frozen `node.fills` array directly | `JSON.parse(JSON.stringify(node.fills))` before modifying |
| Variables not applied, no error | Mixed create+bind in one large script, script truncated | Split into two scripts: create first, bind second (Rule 14) |
| `output.txt` not updating | Scripter output panel not captured by run.py | Use `figma.notify()` for status; dump tree in separate script to verify |
| Frame has only title, rest missing | Large script silently truncated during clipboard paste | Split script into <5KB chunks; use `--file` instead of inline |
| `exit code 144` on server restart | pkill + start in same bash command | Split into two separate bash commands with `sleep 2` between |
| Quick Actions doesn't open | Scripter has focus, Ctrl+/ goes to Scripter not Figma | Press Escape to close Scripter first, click on canvas, then Ctrl+/ |
| Propstar not launching | Plugin search via Quick Actions fails while Scripter is open | Use `__plugin__:Name` command — it closes Scripter, focuses canvas, opens Quick Actions |
| After Propstar, Scripter gone | `open_plugin` closes Scripter but doesn't reopen it | Send `__reopen_scripter__` command after plugin completes |
| `layoutMode` props ignored | `itemSpacing`/`padding` set before `layoutMode = "VERTICAL"` | Always set `layoutMode` FIRST, then sizing, then spacing (Rule 3) |
| `resize()` has no effect | Node not yet appended to parent | `parent.appendChild(node)` BEFORE `resize()` (Rule 2) |
| Icon strokes wrong color | Instance icon vectors have default black stroke | Walk instance children with `findAll`, bind stroke color via `bS()` |
| `loadFontAsync` fails on custom font | Hardcoded `{family:"Gilroy",style:"Medium"}` with `await` | Read `n.fontName` from node directly, use `.then()` instead of `await` |
