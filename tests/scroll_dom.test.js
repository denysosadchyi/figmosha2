// Overlay scrollbar of #cMsgs, #settings and #welcome (jsdom, real plugin/ui.html).
//
// Contract (docs/CHAT.md):
//   The native scrollbar of each area is hidden. Each area has one rail
//   .sbar[data-for=<area id>] outside the area (renders replace the area's innerHTML),
//   aria-hidden, with one .sbar-thumb. The rail lets the pointer through, the thumb takes it.
//   Geometry: overflow = sh - ch; no overflow hides the rail;
//     thumb height = max(24, ch*ch/sh); thumb top = scrollTop / overflow * (ch - height);
//     the rail spans the area (top = offsetTop, height = clientHeight).
//   Updates on scroll, ResizeObserver or window resize, and content changes.
//   Visibility: class "on" after a scroll or hover, removed about 1 s after the last one,
//   kept while hovered or dragging.
//   Drag: button 0 on the thumb captures the pointer, a move of dy scrolls
//   dy * overflow / (ch - height) (in layout px under UI zoom), clamped. A click does not
//   jump. pointerup, pointercancel, lostpointercapture or a move with buttons 0 end it.
//   Another pointer never steers it. No resize message.
//
// Timers of 100 ms and more run on a fake clock (p.tick), so the suite does not sleep.
//
//     JSDOM=/path/to/node_modules/jsdom node tests/scroll_dom.test.js
const fs = require("fs");
const path = require("path");

let JSDOM, VirtualConsole;
try { ({ JSDOM, VirtualConsole } = require(process.env.JSDOM || "jsdom")); }
catch (e) {
  console.log("SKIP scroll_dom.test.js: jsdom is not installed (npm install --no-save jsdom)");
  process.exit(0);
}

const HTML = fs.readFileSync(path.join(__dirname, "..", "plugin", "ui.html"), "utf8");
const DOC = "dTest";
const AREAS = ["cMsgs", "settings", "welcome"];

let failed = 0;
function check(label, actual, expected) {
  const a = JSON.stringify(actual), e = JSON.stringify(expected);
  const ok = a === e;
  if (!ok) failed++;
  console.log(`${ok ? "  ok  " : " FAIL "} ${label}${ok ? "" : `\n         got ${a}\n         want ${e}`}`);
}
const truthy = (label, v) => check(label, !!v, true);
const near = (label, got, want, tol = 1) => {
  const ok = typeof got === "number" && Number.isFinite(got) && Math.abs(got - want) <= tol;
  if (!ok) failed++;
  console.log(`${ok ? "  ok  " : " FAIL "} ${label}${ok ? "" : `\n         got ${JSON.stringify(got)}\n         want ${want} (+/-${tol})`}`);
};
const wait = (ms = 40) => new Promise((r) => setTimeout(r, ms));  // one frame plus the scroll event
const msg = (id, from, text, extra = {}) => Object.assign({ id, from, text, t: 1795280000 + id }, extra);
const px = (v) => (/^-?\d+(\.\d+)?px$/.test(String(v || "").trim()) ? parseFloat(v) : null);

// ─── harness ──────────────────────────────────────────────────────────────

async function open({ ro = true, welcome, history = [msg(1, "claude", "Привіт")] } = {}) {
  const posted = [], errors = [], sockets = [], observers = [];
  const vc = new VirtualConsole();
  vc.on("jsdomError", (e) => errors.push(e.message));
  // Fake clock for page timers of 100 ms and more; short ones stay real.
  let now = 0, seq = 0;
  const slow = new Map();
  const dom = new JSDOM(HTML, {
    runScripts: "dangerously", virtualConsole: vc, url: "http://localhost/", pretendToBeVisual: true,
    beforeParse(w) {
      const realSet = w.setTimeout.bind(w), realClear = w.clearTimeout.bind(w);
      w.setTimeout = (fn, ms = 0, ...a) => {
        if (!(ms >= 100)) return realSet(fn, ms, ...a);
        const id = "f" + (++seq); slow.set(id, { at: now + ms, fn: () => fn(...a) }); return id;
      };
      w.clearTimeout = (id) => { if (slow.has(id)) slow.delete(id); else realClear(id); };
      w.WebSocket = class {
        constructor() { this.readyState = 0; sockets.push(this); setTimeout(() => { this.readyState = 1; this.onopen && this.onopen(); }, 5); }
        send() {} close() { this.readyState = 3; this.onclose && this.onclose(); }
      };
      w.WebSocket.OPEN = 1;
      w.parent.postMessage = (m) => posted.push(m.pluginMessage);
      w.Element.prototype.scrollIntoView = function () {};
      if (ro) w.ResizeObserver = class { constructor(cb) { this.cb = cb; this.els = []; observers.push(this); } observe(el) { this.els.push(el); } unobserve() {} disconnect() {} };
      else delete w.ResizeObserver;
      w.Element.prototype.setPointerCapture = function (id) { this._cap = id; };
      w.Element.prototype.releasePointerCapture = function () { this._cap = null; };
      w.Element.prototype.hasPointerCapture = function (id) { return this._cap === id; };
    },
  });
  const tick = async (ms) => {
    const end = now + ms;
    for (;;) {
      const due = [...slow.entries()].filter(([, t]) => t.at <= end).sort((a, b) => a[1].at - b[1].at)[0];
      if (!due) break;
      slow.delete(due[0]); now = due[1].at; due[1].fn();
    }
    now = end; await wait(5);
  };
  const w = dom.window, d = w.document;
  w.addEventListener("error", (e) => errors.push(e.message));
  Object.defineProperty(w, "innerWidth", { value: 380, configurable: true });
  Object.defineProperty(w, "innerHeight", { value: 560, configurable: true });
  const pm = (m) => w.dispatchEvent(new w.MessageEvent("message", { data: { pluginMessage: m } }));
  const ws = () => sockets[sockets.length - 1];
  const push = (event) => ws().onmessage({ data: JSON.stringify({ type: "chat", doc: DOC, event }) });
  await wait(20);
  pm({ type: "identity", name: "Banner", docSig: DOC });
  pm({ type: "settings", scale: 1, theme: "system", lang: "uk" });
  if (welcome !== undefined) pm({ type: "welcome", show: welcome });
  pm({ type: "ui-mode", mode: "chat" });
  await wait();
  if (ws()) push({ kind: "history", busy: false, activity: [], messages: history });
  await wait();
  const $ = (q) => d.querySelector(q);
  const rail = (id) => d.querySelector(`.sbar[data-for="${id}"]`);
  const thumb = (id) => { const r = rail(id); return r && r.querySelector(".sbar-thumb"); };
  // jsdom has no layout: a fake box, ch tall, sh content. The scroll event comes later, like a browser.
  function layout(id, { ch = 400, sh = 1000, hidden = () => false } = {}) {
    const box = d.getElementById(id), L = { ch, sh, top: 0 };
    const fire = () => setTimeout(() => box.dispatchEvent(new w.Event("scroll")), 0);
    Object.defineProperty(box, "clientHeight", { configurable: true, get: () => hidden() ? 0 : L.ch });
    Object.defineProperty(box, "offsetHeight", { configurable: true, get: () => hidden() ? 0 : L.ch });
    Object.defineProperty(box, "scrollHeight", { configurable: true, get: () => hidden() ? 0 : Math.max(L.sh, L.ch) });
    Object.defineProperty(box, "scrollTop", { configurable: true, get: () => L.top,
      set: (v) => { const n = Math.max(0, Math.min(Math.round(+v || 0), Math.max(0, L.sh - L.ch))); if (n !== L.top) { L.top = n; fire(); } } });
    L.box = box;
    L.user = async (v) => { L.top = v; box.dispatchEvent(new w.Event("scroll")); await wait(); };
    L.bottom = () => L.top === L.sh - L.ch;
    L.observe = async () => { for (const o of observers) if (o.els.includes(box)) o.cb([{ target: box }], o); await wait(); };
    return L;
  }
  const ev = (el, type, y, o = {}) => el.dispatchEvent(new w.PointerEvent(type, Object.assign({
    bubbles: true, cancelable: true, composed: true, pointerId: 3, pointerType: "mouse", isPrimary: true,
    button: 0, buttons: type === "pointerup" || type === "pointercancel" ? 0 : 1,
    clientX: 370, clientY: y, screenX: 370, screenY: y }, o)));
  return { w, d, $, pm, push, posted, errors, rail, thumb, layout, ev, observers, tick, close() { w.close(); } };
}

const thumbTop = (th) => {
  if (!th) return null;
  const m = (th.style.transform || "").match(/translateY\(\s*(-?[\d.]+)px\s*\)/);
  return m ? parseFloat(m[1]) : px(th.style.top);
};
const thumbH = (th) => (th ? px(th.style.height) : null);
const hidden = (r) => !r || r.hidden;
const resizes = (p) => p.posted.filter((m) => m && m.type === "resize");

// The last declared value of prop among plain rules that match el (source order).
function lastDecl(d, el, prop, pseudo) {
  let v = null;
  const walk = (list) => { for (const r of list) {
    if (r.cssRules) continue;  // @media blocks
    if (!r.selectorText) continue;
    const hit = r.selectorText.split(",").some((part) => {
      part = part.trim();
      if (pseudo) { if (!part.endsWith(pseudo)) return false; part = part.slice(0, -pseudo.length); }
      else if (part.includes("::")) return false;
      try { return !!part && el.matches(part); } catch (e) { return false; }
    });
    if (hit) { const x = r.style.getPropertyValue(prop); if (x) v = x.trim(); }
  } };
  for (const s of d.styleSheets) walk(s.cssRules);
  return v;
}

// ─── tests ────────────────────────────────────────────────────────────────

async function structure() {
  const p = await open({ welcome: true });
  for (const id of AREAS) {
    const el = p.$("#" + id), r = p.rail(id);
    truthy(`${id}: one rail outside the area, one thumb, aria-hidden`, r && p.d.querySelectorAll(`.sbar[data-for="${id}"]`).length === 1
      && !el.contains(r) && r.querySelectorAll(".sbar-thumb").length === 1 && r.getAttribute("aria-hidden") === "true" && !r.textContent.trim());
    const wk = lastDecl(p.d, el, "display", "::-webkit-scrollbar"), ww = lastDecl(p.d, el, "width", "::-webkit-scrollbar");
    truthy(`${id}: the native scrollbar is hidden`, lastDecl(p.d, el, "scrollbar-width") === "none" && (wk === "none" || /^0(px)?$/.test(ww || "")));
    const rc = p.w.getComputedStyle(r), tc = p.w.getComputedStyle(p.thumb(id));
    check(`${id}: the rail lets the pointer through, the thumb takes it`, [rc.pointerEvents, tc.pointerEvents], ["none", "auto"]);
  }
  const ez = p.$('.rz[data-edge="e"]'), zi = (el) => parseInt(p.w.getComputedStyle(el).zIndex, 10);
  truthy("the e resize zone sits above the rail", Number.isFinite(zi(ez)) && !(zi(p.rail("cMsgs")) >= zi(ez)));
  const r = p.rail("cMsgs"), op = () => parseFloat(p.w.getComputedStyle(r).opacity);
  r.classList.remove("on"); const off = op(); r.classList.add("on");
  check("the rail is invisible without on and visible with it", [off, op()], [0, 1]);
  p.pm({ type: "settings", scale: 1, theme: "system", lang: "en" }); await wait();
  truthy("the welcome rail survives a re-render", p.thumb("welcome") && p.thumb("welcome").isConnected);
  check("no script errors", p.errors, []);
  p.close();
}

async function geometry() {
  const p = await open();
  const L = p.layout("cMsgs", { ch: 400, sh: 400 });
  const r = p.rail("cMsgs"), th = p.thumb("cMsgs");
  await L.user(0);
  truthy("no overflow: the rail is hidden", hidden(r));
  L.sh = 1000; await L.user(0);
  check("overflow: the rail is shown", hidden(r), false);
  near("thumb height = ch*ch/sh", thumbH(th), 160);
  await L.user(300);
  near("thumb top = st/overflow * (ch - h)", thumbTop(th), 120);
  await L.user(600);
  near("at the bottom: the thumb ends at the bottom", thumbTop(th), 240);
  L.sh = 20000; await L.user(9800);
  near("long content: the thumb keeps 24px", thumbH(th), 24);
  near("long content halfway: top uses the 24px thumb", thumbTop(th), 188);
  // The rail spans the area.
  let off = 37;
  Object.defineProperty(L.box, "offsetTop", { get: () => off, configurable: true });
  L.sh = 1000; await L.user(0);
  check("rail top and height follow the area", [r.style.top, r.style.height], ["37px", "400px"]);
  off = 61; L.ch = 600; L.sh = 2250; await L.observe();
  near("ResizeObserver: same thumb height", thumbH(th), 160);
  check("ResizeObserver: rail top and height still follow", [r.style.top, r.style.height], ["61px", "600px"]);
  L.ch = 1000; L.sh = 900; await L.observe();
  truthy("ResizeObserver: taller than the content hides the rail", hidden(r));
  check("no script errors", p.errors, []);
  p.close();

  // No ResizeObserver: a window resize alone updates the thumb, and does not scroll.
  const q = await open({ ro: false, welcome: true });
  const Q = q.layout("welcome", { ch: 400, sh: 1000 });
  await Q.user(200);
  Q.ch = 200; q.w.dispatchEvent(new q.w.Event("resize")); await wait();
  near("window resize: thumb = 200*200/1000", thumbH(q.thumb("welcome")), 40);
  check("window resize does not scroll", Q.top, 200);
  check("no script errors without ResizeObserver", q.errors, []);
  q.close();
}

async function content() {
  const p = await open();
  const L = p.layout("cMsgs", { ch: 400, sh: 400 });
  const th = p.thumb("cMsgs");
  L.sh = 800; p.push({ kind: "message", message: msg(2, "claude", "Довга відповідь") }); await wait(60);
  check("a new message shows the rail and the list follows the bottom", [hidden(p.rail("cMsgs")), L.top], [false, 400]);
  near("a new message: thumb height", thumbH(th), 200);
  p.push({ kind: "busy", busy: true }); await wait();
  for (let i = 1; i <= 4; i++) { L.sh = 800 + i * 300; p.push({ kind: "delta", text: "Рядок " + i + "\n" }); await wait(10); }
  await wait();
  truthy("streaming deltas keep the list at the bottom", L.bottom());
  near("streaming: the thumb is at the bottom", thumbTop(th) + thumbH(th), 400, 2);
  await L.user(300);
  L.sh = 4000; p.push({ kind: "delta", text: "Ще рядок\n" }); await wait(60);
  check("scrolled up: a delta does not jump", L.top, 300);
  near("scrolled up: a delta still updates the thumb", thumbH(th), 40);
  // A change deep inside the list (no re-render) still updates the thumb.
  const inner = L.box.querySelector(".msg") || L.box.firstElementChild;
  L.sh = 2000; inner.appendChild(p.d.createElement("span")); await wait(60);
  near("a nested element added updates the thumb", thumbH(th), 80);
  const tn = p.d.createTreeWalker(inner, 4).nextNode();
  L.sh = 1600; tn.data += " ще"; await wait(60);
  near("a text edit updates the thumb", thumbH(th), 100);
  check("no script errors", p.errors, []);
  p.close();
}

async function visibility() {
  const p = await open();
  const L = p.layout("cMsgs");
  const r = p.rail("cMsgs"), th = p.thumb("cMsgs");
  await L.user(100);
  truthy("a scroll shows the bar", r.classList.contains("on"));
  await p.tick(600);
  truthy("still on 0.6 s after", r.classList.contains("on"));
  await L.user(200); await p.tick(700);
  truthy("a new scroll restarts the timer", r.classList.contains("on"));
  await p.tick(400);
  check("off about 1 s after the last scroll", r.classList.contains("on"), false);
  L.box.dispatchEvent(new p.w.MouseEvent("mouseenter")); await wait(); await p.tick(1300);
  truthy("hovered area: on and kept on", r.classList.contains("on"));
  L.box.dispatchEvent(new p.w.MouseEvent("mouseleave"));
  th.dispatchEvent(new p.w.MouseEvent("mouseenter")); await p.tick(1300);
  truthy("hovered thumb: kept on", r.classList.contains("on"));
  th.dispatchEvent(new p.w.MouseEvent("mouseleave")); await p.tick(1300);
  check("hover gone: off", r.classList.contains("on"), false);
  p.ev(th, "pointerdown", 120); p.ev(th, "pointermove", 140); await p.tick(1300);
  truthy("kept on during a long drag", r.classList.contains("on"));
  p.ev(th, "pointerup", 140); await p.tick(1300);
  check("off about 1 s after the drag ends", r.classList.contains("on"), false);
  check("no script errors", p.errors, []);
  p.close();
}

async function drag() {
  const p = await open();
  const L = p.layout("cMsgs", { ch: 500, sh: 2000 });   // thumb 125, ratio 1500/375 = 4
  await L.user(400);
  const th = p.thumb("cMsgs"), r = p.rail("cMsgs");
  const before = resizes(p).length;
  const y0 = 100;
  p.ev(th, "pointerdown", y0 + 60); p.ev(th, "pointerup", y0 + 60); await wait();
  check("a click on the thumb does not jump", L.top, 400);
  const down = new p.w.PointerEvent("pointerdown", { bubbles: true, cancelable: true, pointerId: 3, pointerType: "mouse", isPrimary: true, button: 0, buttons: 1, clientX: 370, clientY: y0 });
  th.dispatchEvent(down);
  truthy("pointerdown captures, adds drag and on, and blocks text selection",
    th._cap === 3 && r.classList.contains("drag") && r.classList.contains("on") && down.defaultPrevented);
  p.ev(th, "pointermove", y0 + 50); await wait();
  near("drag down 50px scrolls 50 * overflow / (ch - h)", L.top, 600, 2);
  p.ev(th, "pointermove", y0 + 300, { pointerId: 9 });
  p.ev(th, "pointerup", y0 + 300, { pointerId: 9 });
  p.ev(th, "pointercancel", y0 + 300, { pointerId: 9 }); await wait();
  p.ev(th, "pointermove", y0 + 25); await wait();
  near("another pointer neither steers nor ends the drag", L.top, 500, 2);
  p.ev(th, "pointermove", y0 + 5000); await wait();
  check("drag far down clamps at the bottom", L.top, 1500);
  p.ev(th, "pointermove", y0 - 5000); await wait();
  check("drag far up clamps at the top", L.top, 0);
  p.ev(th, "pointerup", y0 - 5000); await wait();
  p.ev(th, "pointermove", y0 + 100); await wait();
  check("pointerup releases the capture and the drag", [th._cap, r.classList.contains("drag"), L.top], [null, false, 0]);
  for (const [name, stop] of [
    ["pointercancel", (y) => p.ev(th, "pointercancel", y)],
    ["lostpointercapture", () => { th._cap = null; th.dispatchEvent(new p.w.PointerEvent("lostpointercapture", { bubbles: true, pointerId: 3 })); }],
    ["a move with buttons 0", (y) => p.ev(th, "pointermove", y, { buttons: 0 })],
  ]) {
    await L.user(400);
    p.ev(th, "pointerdown", y0); p.ev(th, "pointermove", y0 + 25); await wait();
    stop(y0 + 25); await wait();
    p.ev(th, "pointermove", y0 + 100); await wait();
    check(`${name} ends the drag`, [L.top, r.classList.contains("drag")], [500, false]);
  }
  await L.user(400);
  p.ev(th, "pointerdown", y0, { button: 2, buttons: 2 }); p.ev(th, "pointermove", y0 + 50, { buttons: 2 }); await wait();
  check("the right button does not drag", L.top, 400);
  p.ev(th, "pointerup", y0 + 50, { button: 2 });
  check("a thumb drag posts no resize message", resizes(p).length, before);
  check("no script errors", p.errors, []);
  p.close();

  // UI zoom 2: 60 pointer px = 30 layout px = 30 * 600 / 240 = 75 px of content.
  const q = await open();
  const Q = q.layout("cMsgs", { ch: 400, sh: 1000 });
  Q.box.getBoundingClientRect = () => ({ top: 0, left: 0, right: 380, bottom: Q.ch * 2, width: 380, height: Q.ch * 2, x: 0, y: 0 });
  await Q.user(0);
  const t = q.thumb("cMsgs");
  q.ev(t, "pointerdown", 100); q.ev(t, "pointermove", 160); await wait();
  near("zoom 2: the drag uses layout px", Q.top, 75);
  q.ev(t, "pointerup", 160);
  q.close();
}

async function follow() {
  // A drag up stops the follow; a drag to the bottom starts it again.
  const p = await open();
  const L = p.layout("cMsgs", { ch: 500, sh: 2000 });
  p.push({ kind: "message", message: msg(2, "claude", "Відповідь") }); await wait();
  const th = p.thumb("cMsgs");
  let y = 100;
  p.ev(th, "pointerdown", y); p.ev(th, "pointermove", y - 100); await wait(); p.ev(th, "pointerup", y - 100); await wait();
  const top = L.top;
  L.sh = 2300; p.push({ kind: "message", message: msg(3, "claude", "Ще одна") }); await wait();
  check("after a drag up a new message does not jump", [top < 1500, L.top], [true, top]);
  p.ev(th, "pointerdown", y); p.ev(th, "pointermove", y + 5000); await wait(); p.ev(th, "pointerup", y + 5000); await wait();
  L.sh = 2600; p.push({ kind: "message", message: msg(4, "claude", "І ще") }); await wait();
  truthy("after a drag to the bottom the list follows again", L.bottom());
  p.close();
}

async function screens() {
  // The settings page has its own bar, and the chat comes back to its place.
  const p = await open({ history: [msg(1, "claude", "Привіт"), msg(2, "me", "ще")] });
  const html = p.d.documentElement;
  const L = p.layout("cMsgs", { ch: 500, sh: 2000, hidden: () => html.classList.contains("settings-open") });
  p.push({ kind: "message", message: msg(3, "claude", "Відповідь") }); await wait();
  await L.user(400);
  p.$("#setbtn").click(); await wait();
  const S = p.layout("settings", { ch: 400, sh: 800 });
  await S.user(0);
  const st = p.thumb("settings");
  p.ev(st, "pointerdown", 100); p.ev(st, "pointermove", 140); await wait();
  near("settings: drag 40px scrolls 40 * 400 / 200", S.top, 80);
  p.ev(st, "pointerup", 140);
  L.top = 0;  // a hidden list drops its position in a browser
  p.$("#sBack").click(); await wait(60);
  check("back in the chat: the list is where it was", L.top, 400);
  near("back in the chat: the thumb follows", thumbTop(p.thumb("cMsgs")), 400 / 1500 * 375, 2);
  check("no script errors", p.errors, []);
  p.close();

  const q = await open({ welcome: true });
  const Q = q.layout("welcome", { ch: 400, sh: 800 });
  await Q.user(200);
  near("welcome: its thumb follows its scroll", thumbTop(q.thumb("welcome")), 100);
  check("no script errors (welcome)", q.errors, []);
  q.close();
}

(async () => {
  for (const t of [structure, geometry, content, visibility, drag, follow, screens]) {
    console.log(`\n${t.name}`);
    try { await t(); } catch (e) { failed++; console.log(" FAIL  threw: " + (e.stack || e).toString().split("\n").slice(0, 3).join(" | ")); }
  }
  console.log(failed ? `\n${failed} failed` : "\nall passed");
  process.exit(failed ? 1 : 0);
})();
