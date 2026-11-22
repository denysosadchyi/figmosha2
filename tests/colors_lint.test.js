// Lint: colors live only in the two token blocks of plugin/ui.html,
// `:root` (light) and `:root.dark` (dark). Everywhere else a color comes through
// var(--token), so switching the theme switches every pixel.
// Scans CSS declarations (nested @media and @keyframes included), style="..."
// attributes and svg/html color attributes. Allowed outside the tokens:
// transparent, currentColor, inherit, none, url(#id), var(--...).
//
//     node tests/colors_lint.test.js   (pure static scan, no jsdom)
const fs = require("fs");
const path = require("path");

const SRC = fs.readFileSync(path.join(__dirname, "..", "plugin", "ui.html"), "utf8");

// ─── helpers ──────────────────────────────────────────────────────────────
function lineOf(idx) { return SRC.slice(0, idx).split("\n").length; }

const NAMED = ("aliceblue antiquewhite aqua aquamarine azure beige bisque black blanchedalmond blue blueviolet brown "
  + "burlywood cadetblue chartreuse chocolate coral cornflowerblue cornsilk crimson cyan darkblue darkcyan darkgoldenrod "
  + "darkgray darkgreen darkgrey darkkhaki darkmagenta darkolivegreen darkorange darkorchid darkred darksalmon darkseagreen "
  + "darkslateblue darkslategray darkslategrey darkturquoise darkviolet deeppink deepskyblue dimgray dimgrey dodgerblue "
  + "firebrick floralwhite forestgreen fuchsia gainsboro ghostwhite gold goldenrod gray green greenyellow grey honeydew "
  + "hotpink indianred indigo ivory khaki lavender lavenderblush lawngreen lemonchiffon lightblue lightcoral lightcyan "
  + "lightgoldenrodyellow lightgray lightgreen lightgrey lightpink lightsalmon lightseagreen lightskyblue lightslategray "
  + "lightslategrey lightsteelblue lightyellow lime limegreen linen magenta maroon mediumaquamarine mediumblue mediumorchid "
  + "mediumpurple mediumseagreen mediumslateblue mediumspringgreen mediumturquoise mediumvioletred midnightblue mintcream "
  + "mistyrose moccasin navajowhite navy oldlace olive olivedrab orange orangered orchid palegoldenrod palegreen "
  + "paleturquoise palevioletred papayawhip peachpuff peru pink plum powderblue purple rebeccapurple red rosybrown "
  + "royalblue saddlebrown salmon sandybrown seagreen seashell sienna silver skyblue slateblue slategray slategrey snow "
  + "springgreen steelblue tan teal thistle tomato turquoise violet wheat white whitesmoke yellow yellowgreen "
  // system colors: fixed by the OS, not by our theme
  + "canvas canvastext linktext visitedtext activetext buttonface buttontext buttonborder field fieldtext highlight "
  + "highlighttext selecteditem selecteditemtext mark marktext graytext accentcolor accentcolortext").split(/\s+/);
const NAMED_SET = new Set(NAMED);
// Properties whose value is a color (or holds one): named colors are checked only here,
// so words like `white-space` or an animation name never count.
const COLOR_PROP = /(^|-)(color|background|border|outline|fill|stroke|shadow|caret|column-rule|text-decoration|text-emphasis|scrollbar-color|filter|mask)/i;
const FUNC_RE = /\b(rgba?|hsla?|hwb|lab|lch|oklab|oklch|color)\s*\(/gi;
const HEX_RE = /#(?:[0-9a-f]{8}|[0-9a-f]{6}|[0-9a-f]{3,4})(?![0-9a-z_-])/gi;

// Color literals in one declaration value. Returns a list of offending fragments.
function colorLiterals(prop, value) {
  const out = [];
  const v = value.replace(/url\([^)]*\)/gi, "url()"); // url(#id) is a reference, not a color
  for (const m of v.matchAll(HEX_RE)) out.push(m[0]);
  for (const m of v.matchAll(FUNC_RE)) out.push(m[0] + "...)");
  if (COLOR_PROP.test(prop) || /^(fill|stroke|stop-color|flood-color|lighting-color|color|bgcolor)$/i.test(prop)) {
    // identifiers outside var(); --name tokens keep their leading dashes so --red is not red
    const idents = v.replace(/var\(\s*--[\w-]+/g, " ").match(/-{0,2}[a-z][\w-]*/gi) || [];
    for (const id of idents) if (NAMED_SET.has(id.toLowerCase())) out.push(id);
  }
  return out;
}

function splitDecls(body) {
  const out = [];
  let depth = 0, cur = "", q = null;
  for (const ch of body) {
    if (q) { cur += ch; if (ch === q) q = null; continue; }
    if (ch === '"' || ch === "'") { q = ch; cur += ch; continue; }
    if (ch === "(") depth++;
    if (ch === ")") depth--;
    if (ch === ";" && depth === 0) { out.push(cur); cur = ""; continue; }
    cur += ch;
  }
  out.push(cur);
  return out.map((d) => d.trim()).filter(Boolean).map((d) => {
    const i = d.indexOf(":");
    return i < 0 ? null : { prop: d.slice(0, i).trim(), value: d.slice(i + 1).trim() };
  }).filter(Boolean);
}

// CSS → flat list of { sel, ctx, decls, line }. Handles nesting (@media, @keyframes).
function parseCss(css, baseIdx) {
  const rules = [];
  const clean = css.replace(/\/\*[\s\S]*?\*\//g, (m) => m.replace(/[^\n]/g, " "));
  const stack = [];
  let start = 0;
  for (let i = 0; i < clean.length; i++) {
    const ch = clean[i];
    if (ch === "{") {
      stack.push({ prelude: clean.slice(start, i).trim(), open: i });
      start = i + 1;
    } else if (ch === "}") {
      const top = stack.pop();
      if (!top) continue;
      const inner = clean.slice(top.open + 1, i);
      if (!inner.includes("{")) {
        rules.push({ sel: top.prelude, ctx: stack.map((s) => s.prelude), decls: splitDecls(inner),
          line: lineOf(baseIdx + top.open) });
      }
      start = i + 1;
    } else if (ch === ";" && stack.length === 0) {
      start = i + 1; // top-level @import etc.
    }
  }
  return rules;
}

const styleBlocks = [...SRC.matchAll(/<style[^>]*>([\s\S]*?)<\/style>/gi)];
const RULES = styleBlocks.flatMap((m) => parseCss(m[1], m.index + m[0].indexOf(">") + 1));
const isTokenSel = (s) => /^:root$/.test(s.trim()) || /^:root\.dark$/.test(s.trim());
const tokenRule = (s) => RULES.filter((r) => r.sel.trim() === s && r.ctx.length === 0);


const ATTR_OK = /^(none|currentcolor|transparent|inherit|url\(#[\w-]+\)|var\(--[\w-]+\))$/i;
const bad = [];
if (RULES.length < 50) bad.push("parser found only " + RULES.length + " css rules");
for (const r of RULES) {
  if (isTokenSel(r.sel)) continue;
  for (const d of r.decls) {
    const lits = colorLiterals(d.prop, d.value);
    if (lits.length) bad.push("line " + r.line + "  " + r.sel + " { " + d.prop + ": " + d.value + " }  -> " + lits.join(", "));
  }
}
for (const m of SRC.matchAll(/(?<![\w-])style\s*=\s*\\?(["'])([\s\S]*?)\\?\1/g)) {
  for (const d of splitDecls(m[2])) {
    const lits = colorLiterals(d.prop, d.value);
    if (lits.length) bad.push("line " + lineOf(m.index) + "  " + m[0] + "  -> " + lits.join(", "));
  }
}
for (const m of SRC.matchAll(/(?<![\w-])(fill|stroke|stop-color|flood-color|lighting-color|color|bgcolor)\s*=\s*\\?(["'])(.*?)\\?\2/gi)) {
  if (!ATTR_OK.test(m[3].trim())) bad.push("line " + lineOf(m.index) + "  " + m[0]);
}

if (bad.length) {
  console.log("FAIL no raw colors outside :root and :root.dark (" + bad.length + " found)\n     " + bad.join("\n     "));
  process.exit(1);
}
console.log("ok   no raw colors outside :root and :root.dark\n\n1 passed, 0 failed");
