// Exercise the pure helpers from plugin/code.js against a stubbed Figma.
//
// The parts worth pinning down are the ones with no Figma in them: the hex
// conversion, and the order h.frame() applies auto-layout properties in —
// getting that order wrong makes Figma silently ignore the settings.
//
//     node tests/helpers.test.js
const fs = require("fs");
const path = require("path").join(__dirname, "..", "plugin", "code.js");
const src = fs.readFileSync(path, "utf8");

const order = [];   // records the sequence of property writes on a frame

function makeFrame() {
  const f = {
    _children: [],
    width: 100, height: 100,
    appendChild(c) { order.push("appendChild"); this._children.push(c); },
    resize(w, h) { order.push("resize"); this.width = w; this.height = h; },
  };
  return new Proxy(f, {
    set(t, k, v) {
      if (typeof v !== "function") order.push(String(k));
      t[k] = v;
      return true;
    },
  });
}

const figma = {
  showUI() {},
  ui: { onmessage: null, postMessage() {} },
  createFrame: makeFrame,
  currentPage: { selection: [] },
  variables: {},
};

// code.js is written for the plugin sandbox: evaluate it with our stub in scope
// and hand back the HELPERS object it builds.
const h = new Function("figma", "__html__", src + "\nreturn HELPERS;")(figma, "");

let failed = 0;
function check(label, actual, expected) {
  const a = JSON.stringify(actual), e = JSON.stringify(expected);
  const ok = a === e;
  if (!ok) failed++;
  console.log(`${ok ? "  ok  " : " FAIL "} ${label}${ok ? "" : `\n         got ${a}\n         want ${e}`}`);
}

// hex — the /255 conversion that gets hand-rolled wrong
check("hex #ffffff", h.hex("#ffffff"), { r: 1, g: 1, b: 1 });
check("hex 000000 without #", h.hex("000000"), { r: 0, g: 0, b: 0 });
check("hex shorthand #f00", h.hex("#f00"), { r: 1, g: 0, b: 0 });
check("hex #808080", h.hex("#808080"), { r: 128 / 255, g: 128 / 255, b: 128 / 255 });

let threw = false;
try { h.hex("#12"); } catch (e) { threw = /expected #RGB/.test(e.message); }
check("hex rejects garbage", threw, true);

// solid
check("solid paint", h.solid("#ff0000"),
      [{ type: "SOLID", color: { r: 1, g: 0, b: 0 } }]);
check("solid with opacity", h.solid("#ff0000", 0.5),
      [{ type: "SOLID", color: { r: 1, g: 0, b: 0 }, opacity: 0.5 }]);

// frame — the whole point is the order Figma demands
order.length = 0;
const parent = makeFrame();
const f = h.frame(parent, {
  name: "Card", layout: "V", spacing: 16, padding: [24, 12], radius: 8,
  fill: "#ffffff", align: { primary: "CENTER", counter: "MIN" },
});
const idx = (k) => order.indexOf(k);
check("appendChild before layoutMode", idx("appendChild") < idx("layoutMode"), true);
check("layoutMode before sizing", idx("layoutMode") < idx("primaryAxisSizingMode"), true);
check("sizing before itemSpacing", idx("primaryAxisSizingMode") < idx("itemSpacing"), true);
check("hugs both axes when unsized", [f.primaryAxisSizingMode, f.counterAxisSizingMode],
      ["AUTO", "AUTO"]);
check("padding [v,h] expands", [f.paddingTop, f.paddingRight, f.paddingBottom, f.paddingLeft],
      [24, 12, 24, 12]);
check("layout shorthand V", f.layoutMode, "VERTICAL");
check("fill from hex", f.fills, [{ type: "SOLID", color: { r: 1, g: 1, b: 1 } }]);
check("align applied", [f.primaryAxisAlignItems, f.counterAxisAlignItems], ["CENTER", "MIN"]);

// a pinned width must not be overwritten by hug
order.length = 0;
const fixed = h.frame(parent, { layout: "H", w: 320 });
check("pinned width stays FIXED", fixed.primaryAxisSizingMode, undefined);
check("pinned width still hugs height", fixed.counterAxisSizingMode, "AUTO");
check("resize after layoutMode", order.indexOf("layoutMode") < order.indexOf("resize"), true);

// numeric padding
const p = h.frame(parent, { layout: "V", padding: 20 });
check("numeric padding", [p.paddingTop, p.paddingRight], [20, 20]);

// sel
figma.currentPage.selection = [
  { id: "1:2", name: "Btn", type: "FRAME", width: 100, height: 40 },
];
check("sel maps selection", h.sel(), [
  { id: "1:2", name: "Btn", type: "FRAME", w: 100, h: 40 },
]);

console.log(failed ? `\n${failed} FAILED` : "\nall helper checks passed");
process.exit(failed ? 1 : 0);
