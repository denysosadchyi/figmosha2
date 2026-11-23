// Resizing the plugin window by its edges and corners (jsdom, real plugin/ui.html).
//
// Message contract (ui.html -> code.js, via parent.postMessage({ pluginMessage })):
//
//   { type: "resize", w, h }               while dragging, at most one per animation frame
//   { type: "resize", w, h, done: true }   once, on pointerup (or lostpointercapture) after a drag
//
//   w and h are the new window size in CSS px at scale 1 (integers):
//     start = window.innerWidth / innerHeight at pointerdown (device px, scale included)
//     delta = pointer movement since pointerdown (screenX/clientX, device px)
//     w = round((start.w +/- delta.x) / scale), h = round((start.h +/- delta.y) / scale)
//   e, ne, se grow w with +dx; w, nw, sw grow w with -dx (drag left = wider).
//   s, se, sw grow h with +dy; n, ne, nw grow h with -dy (drag up = taller).
//   An edge only changes its own axis: e/w keep h, n/s keep w.
//   code.js clamps (about 320x400 .. 900x1400 at scale 1), calls
//   figma.ui.resize(w * scale, h * scale) and, on done, stores {w, h} in
//   figma.clientStorage "figmosha-chat-size". Bar mode is never resized this way.
//
// Hit zones: 8 empty elements .rz with data-edge = n s e w ne nw se sw.
// They are empty, sit above every other layer, are active in chat mode (also on
// the settings page) and hidden or inert in bar mode.
//
//     JSDOM=/path/to/node_modules/jsdom node tests/resize_dom.test.js
const fs = require("fs");
const path = require("path");

let JSDOM, VirtualConsole;
try { ({ JSDOM, VirtualConsole } = require(process.env.JSDOM || "jsdom")); }
catch (e) {
  console.log("SKIP resize_dom.test.js: jsdom is not installed (npm install --no-save jsdom)");
  process.exit(0);
}

const HTML = fs.readFileSync(path.join(__dirname, "..", "plugin", "ui.html"), "utf8");
const DOC = "dTest";
const EDGES = ["n", "s", "e", "w", "ne", "nw", "se", "sw"];

let failed = 0;
function check(label, actual, expected) {
  const a = JSON.stringify(actual), e = JSON.stringify(expected);
  const ok = a === e;
  if (!ok) failed++;
  console.log(`${ok ? "  ok  " : " FAIL "} ${label}${ok ? "" : `\n         got ${a}\n         want ${e}`}`);
}
const truthy = (label, v) => check(label, !!v, true);
const near = (label, got, want) => {
  const ok = typeof got === "number" && Math.abs(got - want) <= 1;
  if (!ok) failed++;
  console.log(`${ok ? "  ok  " : " FAIL "} ${label}${ok ? "" : `\n         got ${JSON.stringify(got)}\n         want ${want} (+/-1)`}`);
};
const wait = (ms = 40) => new Promise((r) => setTimeout(r, ms));

// ─── harness ──────────────────────────────────────────────────────────────

async function open({ mode = "chat", scale = 1, size = [380, 560] } = {}) {
  const posted = [], errors = [];
  const vc = new VirtualConsole();
  vc.on("jsdomError", (e) => errors.push(e.message));
  const dom = new JSDOM(HTML, {
    runScripts: "dangerously", virtualConsole: vc, url: "http://localhost/", pretendToBeVisual: true,
    beforeParse(w) {
      w.WebSocket = class {
        constructor() { this.readyState = 0; setTimeout(() => { this.readyState = 1; this.onopen && this.onopen(); }, 5); }
        send() {} close() { this.readyState = 3; this.onclose && this.onclose(); }
      };
      w.WebSocket.OPEN = 1;
      w.parent.postMessage = (m) => posted.push(m.pluginMessage);
      w.Element.prototype.scrollIntoView = function () {};
      w.ResizeObserver = class { observe() {} disconnect() {} };
      // jsdom has no pointer capture: record it.
      w.Element.prototype.setPointerCapture = function (id) { this._cap = id; };
      w.Element.prototype.releasePointerCapture = function () { this._cap = null; };
      w.Element.prototype.hasPointerCapture = function (id) { return this._cap === id; };
    },
  });
  const w = dom.window, d = w.document;
  w.addEventListener("error", (e) => errors.push(e.message));
  const setSize = (sw, sh) => {
    Object.defineProperty(w, "innerWidth", { value: sw, configurable: true });
    Object.defineProperty(w, "innerHeight", { value: sh, configurable: true });
  };
  setSize(Math.round(size[0] * scale), Math.round(size[1] * scale));
  const pm = (m) => w.dispatchEvent(new w.MessageEvent("message", { data: { pluginMessage: m } }));
  await wait(20);
  pm({ type: "identity", name: "Banner", docSig: DOC });
  pm({ type: "settings", scale, theme: "system", lang: "uk" });
  pm({ type: "ui-mode", mode });
  await wait();
  const zone = (e) => d.querySelector(`.rz[data-edge="${e}"]`);
  const ptr = (el, type, x, y) => el.dispatchEvent(new w.PointerEvent(type, {
    bubbles: true, cancelable: true, composed: true, pointerId: 7, pointerType: "mouse", isPrimary: true,
    button: 0, buttons: type === "pointerup" ? 0 : 1, clientX: x, clientY: y, screenX: x, screenY: y,
  }));
  const resizes = () => posted.filter((m) => m && m.type === "resize");
  return {
    w, d, pm, posted, errors, zone, setSize, resizes,
    // drag from (x0, y0) by each step in turn; returns the resize messages posted by this drag
    async drag(edge, steps, { up = true, x0 = 200, y0 = 300 } = {}) {
      const el = zone(edge); const before = resizes().length;
      if (!el) throw new Error("no .rz zone for edge " + edge);
      ptr(el, "pointerdown", x0, y0);
      for (const [dx, dy] of steps) { ptr(el, "pointermove", x0 + dx, y0 + dy); await wait(); }
      if (up) { const [dx, dy] = steps[steps.length - 1] || [0, 0]; ptr(el, "pointerup", x0 + dx, y0 + dy); await wait(); }
      return resizes().slice(before);
    },
    ptr,
    close() { w.close(); },
  };
}
const moves = (list) => list.filter((m) => !m.done);
const dones = (list) => list.filter((m) => m.done);
const last = (list) => moves(list).pop() || {};

// ─── tests ────────────────────────────────────────────────────────────────

async function zones() {
  const p = await open();
  const all = [...p.d.querySelectorAll(".rz")];
  check("chat mode has 8 resize zones", all.length, 8);
  check("one zone per edge and corner", all.map((z) => z.dataset.edge).sort(), [...EDGES].sort());
  for (const e of EDGES) {
    const z = p.zone(e); if (!z) { check(`zone ${e} exists`, false, true); continue; }
    const cs = p.w.getComputedStyle(z);
    check(`zone ${e} is empty (no icon, no text)`, [z.children.length, z.textContent.trim()], [0, ""]);
    check(`zone ${e} is shown in chat mode`, [cs.display === "none", cs.pointerEvents === "none", z.hidden, z.hasAttribute("inert")], [false, false, false, false]);
  }
  check("no script errors", p.errors, []);
  p.close();
}

async function barMode() {
  const p = await open({ mode: "bar", size: [220, 28] });
  for (const e of EDGES) {
    const z = p.zone(e);
    if (!z) continue;  // no zone at all in the bar is fine
    const cs = p.w.getComputedStyle(z);
    truthy(`bar mode: zone ${e} is hidden or inert`, cs.display === "none" || cs.visibility === "hidden" || cs.pointerEvents === "none" || z.hidden || z.hasAttribute("inert"));
  }
  if (p.zone("se")) {
    const got = await p.drag("se", [[40, 40]]);
    check("bar mode: a drag on a corner posts no resize", got, []);
  }
  check("bar mode: nothing posted a resize", p.resizes(), []);
  // Back to chat: the zones work again.
  p.pm({ type: "ui-mode", mode: "chat" }); await new Promise((r) => setTimeout(r, 30));
  p.setSize(380, 560);
  const cs = p.zone("se") && p.w.getComputedStyle(p.zone("se"));
  truthy("chat mode again: the se zone is active", cs && cs.display !== "none" && cs.pointerEvents !== "none");
  const got = await p.drag("se", [[20, 30]]);
  near("chat mode again: se drag posts a wider window", last(got).w, 400);
  check("no script errors", p.errors, []);
  p.close();
}

async function corner() {
  const p = await open();
  const z = p.zone("se");
  const got = await p.drag("se", [[10, 10], [60, 80]], { up: false });
  truthy("pointerdown captures the pointer on the zone", z && z._cap === 7);
  truthy("dragging posts resize messages", moves(got).length >= 1);
  truthy("resize messages carry numbers", moves(got).every((m) => Number.isFinite(m.w) && Number.isFinite(m.h)));
  check("no done before pointerup", dones(got), []);
  near("se: width grows with the pointer", last(got).w, 440);
  near("se: height grows with the pointer", last(got).h, 640);
  truthy("se: integer sizes", Number.isInteger(last(got).w) && Number.isInteger(last(got).h));
  p.ptr(z, "pointerup", 260, 380); await wait();
  const after = p.resizes().slice(got.length);
  const d = dones(p.resizes());
  check("pointerup posts done once", d.length, 1);
  near("done carries the final width", d[0] && d[0].w, 440);
  near("done carries the final height", d[0] && d[0].h, 640);
  check("done is the last message", (after.pop() || {}).done, true);
  // A stray pointerup or move after the drag ends posts nothing.
  const n = p.resizes().length;
  p.ptr(z, "pointerup", 260, 380); p.ptr(z, "pointermove", 300, 400); await wait();
  check("no messages after the drag ended", p.resizes().length, n);
  check("no script errors", p.errors, []);
  p.close();
}

async function throttle() {
  const p = await open();
  const z = p.zone("se");
  if (!z) { check("se zone exists", false, true); p.close(); return; }
  p.ptr(z, "pointerdown", 100, 100);
  for (let i = 1; i <= 20; i++) p.ptr(z, "pointermove", 100 + i, 100 + i);  // 20 moves in one frame
  await wait(60);
  const m = moves(p.resizes());
  truthy(`moves in one frame are merged (${m.length} posted for 20 moves)`, m.length >= 1 && m.length <= 3);
  near("the merged message has the latest width", last(p.resizes()).w, 400);
  p.ptr(z, "pointerup", 120, 120); await wait();
  check("one done", dones(p.resizes()).length, 1);
  p.close();
}

async function edges() {
  const p = await open();
  let got = await p.drag("e", [[50, 70]]);
  near("e: width grows", last(got).w, 430);
  check("e: height stays", last(got).h, 560);
  check("e: done once", dones(got).length, 1);
  check("e: the message names the edge", [last(got).edge, dones(got)[0].edge], ["e", "e"]);
  p.close();

  const s = await open();
  got = await s.drag("s", [[50, 70]]);
  check("s: width stays", last(got).w, 380);
  near("s: height grows", last(got).h, 630);
  check("s: done once", dones(got).length, 1);
  s.close();

  const l = await open();
  got = await l.drag("w", [[-60, 30]]);
  near("w: dragging left makes it wider", last(got).w, 440);
  check("w: height stays", last(got).h, 560);
  check("w: the message names the edge", [last(got).edge, dones(got)[0].edge], ["w", "w"]);
  l.close();

  const t = await open();
  got = await t.drag("n", [[20, -90]]);
  check("n: width stays", last(got).w, 380);
  near("n: dragging up makes it taller", last(got).h, 650);
  t.close();

  const ne = await open();
  got = await ne.drag("ne", [[30, -40]]);
  near("ne: right grows width", last(got).w, 410);
  near("ne: up grows height", last(got).h, 600);
  ne.close();

  const nw = await open();
  got = await nw.drag("nw", [[-30, -40]]);
  near("nw: left grows width", last(got).w, 410);
  near("nw: up grows height", last(got).h, 600);
  check("nw: the message names the edge", last(got).edge, "nw");
  nw.close();

  const sw = await open();
  got = await sw.drag("sw", [[-30, 40]]);
  near("sw: left grows width", last(got).w, 410);
  near("sw: down grows height", last(got).h, 600);
  check("no script errors", sw.errors, []);
  sw.close();
}

async function scaled() {
  // At 125 percent the window is 475x700 device px; the messages are at scale 1.
  const p = await open({ scale: 1.25 });
  const got = await p.drag("se", [[100, 50]]);
  near("scale 1.25: width is divided by the scale", last(got).w, 460);
  near("scale 1.25: height is divided by the scale", last(got).h, 600);
  const d = dones(got)[0] || {};
  near("scale 1.25: done width at scale 1", d.w, 460);
  near("scale 1.25: done height at scale 1", d.h, 600);
  p.close();

  const q = await open({ scale: 0.9 });
  const g2 = await q.drag("e", [[-45, 0]]);
  near("scale 0.9: shrinking width is divided by the scale", last(g2).w, 330);
  check("scale 0.9: height stays at scale 1", last(g2).h, 560);
  q.close();
}

async function onSettingsPage() {
  const p = await open();
  p.d.querySelector("#setbtn").click(); await wait();
  truthy("the settings page is open", p.d.documentElement.classList.contains("settings-open"));
  const cs = p.zone("se") && p.w.getComputedStyle(p.zone("se"));
  truthy("settings page: the se zone is active", cs && cs.display !== "none" && cs.pointerEvents !== "none");
  const got = await p.drag("se", [[20, 20]]);
  near("settings page: corner drag resizes", last(got).w, 400);
  check("settings page: done once", dones(got).length, 1);
  check("no script errors", p.errors, []);
  p.close();
}

async function lostPointerup() {
  // The pointerup lands outside the iframe and never arrives: the next move
  // with no button held ends the drag, and plain hover does not resize.
  const p = await open();
  const z = p.zone("se");
  p.ptr(z, "pointerdown", 100, 100);
  p.ptr(z, "pointermove", 130, 140); await wait();
  const hover = (x, y) => z.dispatchEvent(new p.w.PointerEvent("pointermove", {
    bubbles: true, cancelable: true, pointerId: 7, pointerType: "mouse", isPrimary: true,
    button: -1, buttons: 0, clientX: x, clientY: y, screenX: x, screenY: y }));
  hover(200, 200); await wait();
  const d = dones(p.resizes());
  check("lost pointerup: a move with no button ends the drag once", d.length, 1);
  near("lost pointerup: done keeps the last dragged width", d[0] && d[0].w, 410);
  const n = p.resizes().length;
  hover(260, 260); await wait();
  check("lost pointerup: hover after that posts nothing", p.resizes().length, n);
  const got = await p.drag("se", [[20, 20]]);
  near("lost pointerup: a new drag still works", last(got).w, 400);
  check("no script errors", p.errors, []);
  p.close();
}

async function dragEnds() {
  // Custom pointer event: other pointerId or button than the ptr() helper.
  const ev = (p, el, type, x, y, o = {}) => el.dispatchEvent(new p.w.PointerEvent(type, Object.assign({
    bubbles: true, cancelable: true, pointerId: 7, pointerType: "mouse", isPrimary: true,
    button: 0, buttons: type === "pointerup" ? 0 : 1, clientX: x, clientY: y, screenX: x, screenY: y }, o)));

  // A click with no move is not a resize: nothing posted, capture released.
  let p = await open();
  let z = p.zone("se");
  p.ptr(z, "pointerdown", 100, 100); p.ptr(z, "pointerup", 100, 100); await wait();
  check("click without a move posts no resize", p.resizes(), []);
  check("pointerup releases the pointer capture", z._cap, null);
  p.close();

  // The right button does not start a drag.
  p = await open(); z = p.zone("se");
  ev(p, z, "pointerdown", 100, 100, { button: 2, buttons: 2 });
  ev(p, z, "pointermove", 140, 140, { buttons: 2 }); await wait();
  ev(p, z, "pointerup", 140, 140, { button: 2 }); await wait();
  check("right button drag posts no resize", p.resizes(), []);
  truthy("right button does not capture the pointer", z._cap !== 7);
  p.close();

  // pointercancel ends the drag with done at the last dragged size.
  p = await open(); z = p.zone("se");
  p.ptr(z, "pointerdown", 100, 100); p.ptr(z, "pointermove", 130, 140); await wait();
  ev(p, z, "pointercancel", 0, 0, { buttons: 0 }); await wait();
  let d = dones(p.resizes());
  check("pointercancel: done once", d.length, 1);
  near("pointercancel: done keeps the last dragged width", d[0] && d[0].w, 410);
  near("pointercancel: done keeps the last dragged height", d[0] && d[0].h, 600);
  check("pointercancel: capture released", z._cap, null);
  let n = p.resizes().length;
  p.ptr(z, "pointermove", 200, 200); await wait();
  check("pointercancel: later moves post nothing", p.resizes().length, n);
  p.close();

  // lostpointercapture ends the drag the same way.
  p = await open(); z = p.zone("e");
  p.ptr(z, "pointerdown", 100, 100); p.ptr(z, "pointermove", 150, 100); await wait();
  z.dispatchEvent(new p.w.PointerEvent("lostpointercapture", { bubbles: true, pointerId: 7, pointerType: "mouse" })); await wait();
  d = dones(p.resizes());
  check("lostpointercapture: done once", d.length, 1);
  near("lostpointercapture: done keeps the last dragged width", d[0] && d[0].w, 430);
  n = p.resizes().length;
  p.ptr(z, "pointermove", 200, 100); await wait();
  check("lostpointercapture: later moves post nothing", p.resizes().length, n);
  p.close();

  // A new pointerdown while a stale drag is open (its pointerup was lost):
  // the stale drag ends with done at its last size, then the new one starts.
  p = await open(); z = p.zone("se");
  p.ptr(z, "pointerdown", 100, 100); p.ptr(z, "pointermove", 130, 140); await wait();
  const zs = p.zone("s");
  p.ptr(zs, "pointerdown", 100, 100); await wait();
  d = dones(p.resizes());
  check("stale drag: ended with one done on the next pointerdown", d.length, 1);
  near("stale drag: done keeps its last width", d[0] && d[0].w, 410);
  check("stale drag: its zone released the capture", z._cap, null);
  p.ptr(zs, "pointermove", 100, 150); await wait();
  check("stale drag: the new s drag keeps the width", last(p.resizes()).w, 380);
  near("stale drag: the new s drag sets the height", last(p.resizes()).h, 610);
  p.ptr(zs, "pointerup", 100, 150); await wait();
  check("stale drag: two dones in all", dones(p.resizes()).length, 2);
  p.close();

  // Another pointer (a second finger or pen) does not end or steer the drag.
  p = await open(); z = p.zone("se");
  p.ptr(z, "pointerdown", 100, 100); p.ptr(z, "pointermove", 120, 120); await wait();
  ev(p, z, "pointerup", 300, 300, { pointerId: 9 });
  ev(p, z, "pointercancel", 300, 300, { pointerId: 9, buttons: 0 });
  ev(p, z, "pointermove", 300, 300, { pointerId: 9 }); await wait();
  check("other pointer: no done", dones(p.resizes()), []);
  near("other pointer: size not steered by it", last(p.resizes()).w, 400);
  p.ptr(z, "pointermove", 140, 150); await wait();
  near("other pointer: the drag goes on", last(p.resizes()).w, 420);
  p.ptr(z, "pointerup", 140, 150); await wait();
  check("other pointer: one done at the end", dones(p.resizes()).length, 1);
  check("no script errors", p.errors, []);
  p.close();
}

async function stacking() {
  // The zones sit above every other layer (toast, popups, header).
  const p = await open();
  let top = 0;
  for (const el of p.d.querySelectorAll("body *:not(.rz)")) {
    const v = parseInt(p.w.getComputedStyle(el).zIndex, 10);
    if (Number.isFinite(v) && v > top) top = v;
  }
  for (const e of EDGES) {
    const v = parseInt(p.w.getComputedStyle(p.zone(e) || p.d.body).zIndex, 10);
    truthy(`zone ${e} z-index (${v}) is above every other layer (${top})`, Number.isFinite(v) && v > top);
  }
  p.close();
}

// The window size changes (a drag, a scale change): the composer refits its
// height to the text at the new width. Before, a height from a narrow window
// stayed after it got wider, and the empty field was 3 lines tall.
async function composerRefit() {
  const p = await open();
  const ta = p.d.getElementById("cInput");
  let sh = 92;
  Object.defineProperty(ta, "scrollHeight", { get: () => sh, configurable: true });
  ta.dispatchEvent(new p.w.Event("input", { bubbles: true }));
  check("input grows the composer", ta.style.height, "92px");
  sh = 32; p.setSize(500, 560); p.w.dispatchEvent(new p.w.Event("resize")); await wait();
  check("window resize refits the composer", ta.style.height, "32px");
  check("no script errors", p.errors, []);
  p.close();
}

(async () => {
  for (const t of [composerRefit, zones, barMode, corner, throttle, edges, scaled, onSettingsPage, lostPointerup, dragEnds, stacking]) {
    console.log(`\n${t.name}`);
    try { await t(); } catch (e) { failed++; console.log(" FAIL  threw: " + (e.stack || e).toString().split("\n").slice(0, 3).join(" | ")); }
  }
  console.log(failed ? `\n${failed} failed` : "\nall passed");
  process.exit(failed ? 1 : 0);
})();
