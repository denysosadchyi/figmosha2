# Universal Rule: Adding a Component from Code Repo to Figma

## Pipeline per component

```
Read code → Step 1 (create) → Verify sizes → Step 2 (bind variables) → Verify bindings → Propstar (if Component Set)
```

## 0. Read code

Read `src/components/ui/{name}/index.ts` (CVA definition) and `{Name}.vue` template.
Extract:
- variants, sizes, states
- heights (h-N → N*4 px), widths, paddings, gaps
- border-radius class → map via table below
- colors → map to existing Figma Variables (see section 5)
- inner elements (icon, text, indicator)

## 1. Step 1 — Create visual structure

Single Scripter script. Rules:

```js
// 1. Find maxY to avoid overlap
let maxY = 0;
for (const c of figma.currentPage.children) {
  const b = c.y + c.height;
  if (b > maxY) maxY = b;
}

// 2. Create component(s) with HARDCODED colors (no variable binding!)
const comp = figma.createComponent();
comp.name = "State=Default"; // for variant naming

// 3. Auto Layout — strict order:
parent.appendChild(comp);        // a) add to tree FIRST
comp.layoutMode = "HORIZONTAL";  // b) then layoutMode
comp.resize(W, H);               // c) then size
comp.primaryAxisSizingMode = "AUTO"; // d) then sizing
comp.itemSpacing = 8;            // e) then spacing/padding
comp.paddingLeft = 10;

// 4. Give meaningful names to every child node
rect.name = "Track";
thumb.name = "Thumb";
txt.name = "Label";

// 5. For Component Sets:
const compSet = figma.combineAsVariants(variants, figma.currentPage);
compSet.name = "ComponentName";
compSet.x = 0;
compSet.y = maxY + 80;

// 6. For single components:
comp.x = 0;
comp.y = maxY + 80;

// 7. Always print result for verification
print("ComponentName|" + Math.round(comp.width) + "x" + Math.round(comp.height));
```

**Max script size:** <5KB. If larger — split creation into multiple scripts.

## 2. Verify after Step 1

```bash
sleep 4 && cat /home/den/figmosha/output.txt
```

Check: name, width, height match expected values from code.

## 3. Step 2 — Bind Variables

Separate script. Find nodes by name, bind to existing Figma Variables.

```js
// Helper: get variable by name
async function getVar(name) {
  const vars = await figma.variables.getLocalVariablesAsync();
  return vars.find(v => v.name === name);
}

// Helper: bind FILL color variable
async function bF(node, varName) {
  const v = await getVar(varName);
  if (!v) return;
  const f = JSON.parse(JSON.stringify(node.fills));
  f[0] = figma.variables.setBoundVariableForPaint(f[0], "color", v);
  node.fills = f;
}

// Helper: bind STROKE color variable
async function bS(node, varName) {
  const v = await getVar(varName);
  if (!v) return;
  const s = JSON.parse(JSON.stringify(node.strokes));
  s[0] = figma.variables.setBoundVariableForPaint(s[0], "color", v);
  node.strokes = s;
}

// Helper: bind NUMERIC variable (radius, size, spacing, padding)
async function bN(node, prop, varName) {
  const v = await getVar(varName);
  if (!v) return;
  node.setBoundVariable(prop, v);
}

// Helper: bind TEXT STYLE (from Figma local text styles)
async function bT(node, styleName) {
  const styles = await figma.getLocalTextStylesAsync();
  const style = styles.find(s => s.name === styleName);
  if (!style) return;
  node.textStyleId = style.id;
}

// Helper: bind EFFECT STYLE (shadow etc.)
async function bE(node, styleName) {
  const styles = await figma.getLocalEffectStylesAsync();
  const style = styles.find(s => s.name === styleName);
  if (!style) return;
  node.effectStyleId = style.id;
}

// Usage:
const page = figma.currentPage;
const comp = page.findOne(n => n.name === "ComponentName");
// or for Component Set children:
const set = page.findOne(n => n.name === "ComponentName" && n.type === "COMPONENT_SET");

// Bind fills
await bF(someNode, "semantic/primary");

// Bind border radius (all 4 corners)
for (const p of ["topLeftRadius","topRightRadius","bottomLeftRadius","bottomRightRadius"]) {
  await bN(someNode, p, "radius/lg");
}

// Bind stroke
await bS(someNode, "semantic/border");

// Bind numeric props
await bN(someNode, "itemSpacing", "spacing/2");
await bN(someNode, "paddingLeft", "spacing/25");
await bN(someNode, "width", "size/avatar");
await bN(someNode, "height", "size/badge-height");

// Bind text style to text nodes
await bT(textNode, "body/sm/medium");  // 14px medium
await bT(textNode, "body/xs/medium");  // 12px medium
await bT(textNode, "body/base/regular"); // 16px regular

// Bind effect style
await bE(someNode, "shadow/sm");

print("Bound OK");
```

**NEVER** use `node.setBoundVariable("fills", 0, v)` — it crashes.
**ALWAYS** `JSON.parse(JSON.stringify(node.fills))` before modifying — fills are frozen.
**PRESERVE opacity** when binding: if the paint has opacity (e.g. `bg-destructive/10`), set `f[0].opacity = 0.1` BEFORE calling `setBoundVariableForPaint`. Otherwise variable color replaces opacity with 1 and colors merge visually.

## 4. Verify bindings after Step 2

Run a separate Scripter script that reads `boundVariables` from each node and prints the actual variable names. Compare with expected mapping.

```js
// Verification script template
async function checkBindings(node, expected) {
  const results = [];
  const bv = node.boundVariables || {};

  // Check fills
  if (expected.fill) {
    const fillBinding = bv.fills && bv.fills[0];
    if (fillBinding) {
      const v = await figma.variables.getVariableByIdAsync(fillBinding.id);
      const actual = v ? v.name : "NONE";
      const ok = actual === expected.fill ? "OK" : "FAIL";
      results.push("fill:" + ok + "(" + actual + " vs " + expected.fill + ")");
    } else {
      results.push("fill:FAIL(no binding)");
    }
  }

  // Check strokes
  if (expected.stroke) {
    const strokeBinding = bv.strokes && bv.strokes[0];
    if (strokeBinding) {
      const v = await figma.variables.getVariableByIdAsync(strokeBinding.id);
      const actual = v ? v.name : "NONE";
      const ok = actual === expected.stroke ? "OK" : "FAIL";
      results.push("stroke:" + ok + "(" + actual + ")");
    } else {
      results.push("stroke:FAIL(no binding)");
    }
  }

  // Check numeric props (radius, width, height, padding, etc.)
  if (expected.nums) {
    for (const [prop, varName] of Object.entries(expected.nums)) {
      const binding = bv[prop];
      if (binding) {
        const v = await figma.variables.getVariableByIdAsync(binding.id);
        const actual = v ? v.name : "NONE";
        const ok = actual === varName ? "OK" : "FAIL";
        results.push(prop + ":" + ok + "(" + actual + ")");
      } else {
        results.push(prop + ":FAIL(no binding)");
      }
    }
  }

  return results;
}

// Usage for a Component Set:
const cs = figma.currentPage.findOne(n => n.name === "CompName" && n.type === "COMPONENT_SET");
const lines = [];
for (const child of cs.children) {
  const name = child.name;
  const r = await checkBindings(child, {
    fill: "semantic/primary",
    stroke: "semantic/input",
    nums: {topLeftRadius: "radius/lg", height: "size/input-height"}
  });
  lines.push(name + ": " + r.join(", "));
  // Also check children
  const txt = child.findOne(n => n.name === "Label");
  if (txt) {
    const tr = await checkBindings(txt, {fill: "semantic/foreground"});
    lines.push("  Label: " + tr.join(", "));
  }
}
print(lines.join("\n"));
```

**Rules:**
- Every FAIL must be investigated and fixed before proceeding
- If output shows "no binding" — Step 2 script missed that node or used wrong variable name
- Re-run Step 2 with fixes, then verify again until all OK

## 5. Propstar (only for Component Sets)

```bash
# a) Select the component set via Scripter
python run.py --file select_comp.js

# b) Run Propstar
python run.py "__plugin__:Propstar > Create property table"

# c) Wait and reopen Scripter
sleep 15
python run.py "__reopen_scripter__"
```

## Radius mapping (--radius: 0.25rem = 4px)

| Tailwind class | CSS value | Pixels | Figma variable |
|---|---|---|---|
| rounded-none | 0 | 0 | radius/none |
| rounded-sm | calc(var(--radius) - 4px) | 0 | radius/sm |
| rounded-md | calc(var(--radius) - 2px) | 2 | radius/md |
| rounded / rounded-lg | var(--radius) | 4 | radius/lg |
| rounded-xl | calc(var(--radius) + 4px) | 8 | radius/xl |
| rounded-full | 9999 | 9999 | radius/full |

**`rounded` without suffix = `radius/lg` = 4px** in this design system.

## Color mapping

| Code token | Figma variable |
|---|---|
| bg-primary | semantic/primary |
| text-primary-foreground | semantic/primary-foreground |
| bg-secondary | semantic/secondary |
| text-secondary-foreground | semantic/secondary-foreground |
| bg-destructive | semantic/destructive |
| bg-muted | semantic/muted |
| text-muted-foreground | semantic/muted-foreground |
| bg-accent | semantic/accent |
| border-input | semantic/input |
| bg-background | semantic/background |
| text-foreground | semantic/foreground |
| bg-card | semantic/card |
| border-border | semantic/border |
| ring-ring | semantic/ring |
| bg-popover | semantic/popover |

Badge colors: `badge/{color}-bg`, `badge/{color}-fg`

## Text style mapping

| Code (Tailwind) | Figma text style |
|---|---|
| text-xs font-medium | body/xs/medium |
| text-xs font-normal | body/xs/regular |
| text-sm font-medium | body/sm/medium |
| text-sm font-normal | body/sm/regular |
| text-base font-normal | body/base/regular |
| text-base font-medium | body/base/medium |
| text-lg font-medium | body/lg/medium |
| text-xl font-medium | body/xl/medium |

Button text: `text-sm font-medium` → `body/sm/medium` (sizes default/lg)
Button text xs: `text-xs` → `body/xs/medium`
Button text sm: `text-[0.8rem]` ≈ 13px → `body/xs/medium` (closest)
Badge text: `text-xs font-medium` → `body/xs/medium`
Input text: `text-base` → `body/base/regular`
Label text: `text-sm font-medium` → `body/sm/medium`
Tooltip text: `text-xs` → `body/xs/regular`

## Effect style mapping

| Code | Figma effect style |
|---|---|
| shadow-xs | shadow/xs |
| shadow-sm | shadow/sm |
| shadow-md | shadow/md |
| shadow-lg | shadow/lg |
| shadow-xl | shadow/xl |

## Available size variables

| Variable | Value |
|---|---|
| size/button-height-xs | 24 |
| size/button-height-sm | 28 |
| size/button-height-default | 32 |
| size/button-height-lg | 36 |
| size/input-height | 36 |
| size/badge-height | 20 |
| size/avatar | 32 |
| size/checkbox | 17 |
| spacing/05 | 2 |
| spacing/1 | 4 |
| spacing/15 | 6 |
| spacing/2 | 8 |
| spacing/25 | 10 |
| spacing/3 | 12 |
| spacing/4 | 16 |
| spacing/5 | 20 |
| spacing/6 | 24 |
| spacing/8 | 32 |

## Common mistakes

| Mistake | Fix |
|---|---|
| setBoundVariable("fills", 0, v) | Use setBoundVariableForPaint() |
| Mutate node.fills directly | JSON.parse(JSON.stringify()) first |
| Set padding before layoutMode | Set layoutMode FIRST, then padding |
| resize() before appendChild | appendChild FIRST |
| Mix creation + binding in one script | ALWAYS separate scripts |
| Forget node names in Step 1 | Step 2 can't find nodes |
| rounded-lg = 8px | NO! radius/lg = 4px in this DS |
| Script >5KB | Split into parts |
| Multiple print() calls | Single print(lines.join("\n")) |
