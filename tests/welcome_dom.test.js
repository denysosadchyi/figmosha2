// Welcome screen: the first screen a Figma FILE shows when it opens the plugin
// for the first time. Written before the code (TDD).
//
// ─── message contract ─────────────────────────────────────────────────────
//
//   Flag: figma.root pluginData "figmosha-welcome" = "1" once dismissed.
//         It lives in the FILE (like "figmosha-doc"), not in clientStorage,
//         so another file that never saw the screen shows it again.
//
//   code.js -> ui   { type:"welcome", show:true|false }, once on every start.
//                   A separate message: "settings" keeps its exact old shape
//                   { type, scale, theme } (tests/chat_ui.test.js checks it).
//                   show:true  = this file has not seen the screen yet.
//                   show:false or no welcome message (an older code.js) = no screen.
//   code.js         When welcome is true, code.js also puts the window in the
//                   chat size on start and posts { type:"ui-mode", mode:"chat" },
//                   so the screen never shows in the thin bar.
//   ui -> code.js   { type:"welcome-done" }  once, on the first sent message, the
//                   settings link, or Esc.
//   code.js         On welcome-done: figma.root.setPluginData("figmosha-welcome", "1").
//                   The window stays in the chat; the UI shows the normal chat.
//
//   DOM names: container #welcome (role=dialog or region, with a label),
//   the chat composer at the bottom (its field focused on show; the old Почати
//   button #welcomeGo is gone, see tests/welcome_composer.test.js), feature rows .wrow (4 to 5,
//   each with an svg icon), illustration svg.willo (inline, colors from tokens).
//   All new strings in one const object WELCOME_TEXT in ui.html.
//
//     JSDOM=/path/to/node_modules/jsdom node tests/welcome_dom.test.js
const fs = require("fs");
const path = require("path");
const ROOT = path.join(__dirname, "..");

let JSDOM, VirtualConsole;
try { ({ JSDOM, VirtualConsole } = require(process.env.JSDOM || "jsdom")); }
catch (e) {
  console.log("SKIP welcome_dom.test.js: jsdom is not installed (npm install --no-save jsdom)");
  process.exit(0);
}

const HTML = fs.readFileSync(path.join(ROOT, "plugin", "ui.html"), "utf8");
const DOC = "dTest";

let failed = 0;
function check(label, actual, expected) {
  const a = JSON.stringify(actual), e = JSON.stringify(expected);
  const ok = a === e;
  if (!ok) failed++;
  console.log(`${ok ? "  ok  " : " FAIL "} ${label}${ok ? "" : `\n         got ${a}\n         want ${e}`}`);
}
const truthy = (label, v) => check(label, !!v, true);
const wait = (ms = 30) => new Promise((r) => setTimeout(r, ms));

// ─── 1. code.js: the per-file flag ────────────────────────────────────────

function loadPlugin({ data = {}, mode, throwData = false } = {}) {
  const calls = { resize: [], posted: [], set: [], show: 0, setData: [] };
  // A user who finished the first-run onboarding (tests/onboard_code.test.js).
  const store = Object.assign({ "figmosha-onboarded": 1 }, mode === undefined ? {} : { "figmosha-ui-mode": mode });
  const figma = {
    showUI() {},
    ui: {
      onmessage: null,
      postMessage(m) { calls.posted.push(m); },
      resize(w, h) { calls.resize.push([w, h]); },
      show() { calls.show++; },
    },
    clientStorage: {
      async getAsync(k) { return store[k]; },
      async setAsync(k, v) { store[k] = v; calls.set.push([k, v]); },
    },
    currentPage: { selection: [] },
    openExternal() {}, closePlugin() {},
    root: {
      name: "Banner", children: [{ id: "0:1" }],
      getPluginData(k) { if (throwData) throw new Error("no data"); return data[k] || ""; },
      setPluginData(k, v) { if (throwData) throw new Error("no data"); data[k] = v; calls.setData.push([k, v]); },
    },
    variables: {},
  };
  const src = fs.readFileSync(path.join(ROOT, "plugin", "code.js"), "utf8");
  new Function("figma", "__html__", src + "\nreturn HELPERS;")(figma, "");
  return { figma, calls, store, data };
}
const settingsMsg = (p) => p.calls.posted.filter((m) => m && m.type === "settings").pop();
const welcomeMsg = (p) => p.calls.posted.filter((m) => m && m.type === "welcome").pop();

async function codeFlag() {
  // A file that never saw the screen.
  let p = loadPlugin({ data: { "figmosha-doc": DOC } });
  await wait();
  check("new file: the welcome message says show true", (welcomeMsg(p) || {}).show, true);
  const last = p.calls.resize[p.calls.resize.length - 1] || [0, 0];
  truthy("new file: the window opens in the chat size, not the bar", last[0] >= 360 && last[1] >= 480);
  truthy("new file: code.js tells the UI it is in chat mode",
         p.calls.posted.some((m) => m.type === "ui-mode" && m.mode === "chat"));
  truthy("the window is shown", p.calls.show >= 1);

  await p.figma.ui.onmessage({ type: "welcome-done" });
  check("welcome-done marks this file", p.data["figmosha-welcome"], "1");
  check("the flag is not kept per user in clientStorage",
        p.calls.set.filter(([k]) => /welcome/i.test(k)), []);
  check("the doc id is kept", p.data["figmosha-doc"], DOC);

  // The same file opened again: no screen, the stored bar mode is respected.
  p = loadPlugin({ data: p.data, mode: "bar" });
  await wait();
  check("seen file: the welcome message says show false", (welcomeMsg(p) || {}).show, false);
  check("seen file in bar mode: no resize", p.calls.resize, []);

  // Another file that never saw it: the screen shows again there.
  p = loadPlugin({ data: { "figmosha-doc": "dOther" }, mode: "bar" });
  await wait();
  check("another new file: show true again", (welcomeMsg(p) || {}).show, true);

  // Plugin data cannot be read: the plugin still starts.
  p = loadPlugin({ throwData: true });
  await wait();
  truthy("plugin data off: the window is still shown", p.calls.show >= 1);
  truthy("plugin data off: settings are still sent", settingsMsg(p));
  let threw = null;
  try { await p.figma.ui.onmessage({ type: "welcome-done" }); } catch (e) { threw = e.message; }
  check("plugin data off: welcome-done does not throw", threw, null);
}

// ─── 2. ui.html: the screen ───────────────────────────────────────────────

async function open({ welcome, theme = "system", order = "settings-first", lang } = {}) {
  const sent = [], posted = [], errors = [];
  const vc = new VirtualConsole();
  vc.on("jsdomError", (e) => errors.push(e.message));
  const dom = new JSDOM(HTML, {
    runScripts: "dangerously", virtualConsole: vc, url: "http://localhost/", pretendToBeVisual: true,
    beforeParse(w) {
      w.WebSocket = class {
        constructor() { this.readyState = 0; setTimeout(() => { this.readyState = 1; this.onopen && this.onopen(); }, 5); }
        send(d) { sent.push(JSON.parse(d)); }
        close() { this.readyState = 3; this.onclose && this.onclose(); }
      };
      w.WebSocket.OPEN = 1;
      w.parent.postMessage = (m) => posted.push(m.pluginMessage);
      w.Element.prototype.scrollIntoView = function () {};
      w.ResizeObserver = class { observe() {} };
    },
  });
  const w = dom.window, d = w.document;
  w.addEventListener("error", (e) => errors.push(e.message));
  const pm = (m) => w.dispatchEvent(new w.MessageEvent("message", { data: { pluginMessage: m } }));
  await wait(20);
  pm({ type: "identity", name: "Banner", docSig: DOC });
  const s = { type: "settings", scale: 1, theme };
  if (lang) s.lang = lang;
  // No welcome message at all = an older code.js.
  const wm = () => { if (welcome !== undefined) pm({ type: "welcome", show: welcome }); };
  // code.js on start: settings, welcome, then the chat size. Also try the other order.
  if (order === "settings-first") { pm(s); wm(); pm({ type: "ui-mode", mode: "chat" }); }
  else { pm({ type: "ui-mode", mode: "chat" }); pm(s); wm(); }
  await wait();
  const $ = (q) => d.querySelector(q);
  return {
    w, d, $, posted, errors, pm, sent,
    // Type into the welcome composer and press its send button.
    sendText(text) {
      const f = d.querySelector("#welcome textarea"), b = d.querySelector("#welcome button.send");
      if (!f || !b) return false;
      f.value = text; f.dispatchEvent(new w.Event("input", { bubbles: true })); b.click(); return true;
    },
    done: () => posted.filter((m) => m && m.type === "welcome-done"),
    key(k, el) { const ev = new w.KeyboardEvent("keydown", { key: k, bubbles: true, cancelable: true }); (el || d.activeElement || d.body).dispatchEvent(ev); return ev; },
    close() { w.close(); },
  };
}
// Shown = in the DOM, no hidden ancestor, no display:none on the way up.
function shown(p, el) {
  if (!el) return false;
  for (let n = el; n && n.nodeType === 1; n = n.parentElement) {
    if (n.hidden) return false;
    if (p.w.getComputedStyle(n).display === "none") return false;
  }
  return true;
}

async function appears() {
  const p = await open({ welcome: true });
  const box = p.$("#welcome");
  truthy("welcome true: #welcome is shown", shown(p, box));
  check("welcome true: the chat behind it is not shown", shown(p, p.$("#chat")), false);
  truthy("the window is in chat mode", p.d.body.classList.contains("chatmode"));
  check("no script errors", p.errors, []);
  p.close();

  const q = await open({ welcome: true, order: "mode-first" });
  truthy("settings after ui-mode: still shown", shown(q, q.$("#welcome")));
  check("settings after ui-mode: the composer field has the focus", q.d.activeElement && q.d.activeElement.id, "cInput");
  truthy("settings after ui-mode: the focused field is in the screen", q.$("#welcome").contains(q.d.activeElement));
  q.close();

  const f = await open({ welcome: false });
  check("welcome false: no screen", shown(f, f.$("#welcome")), false);
  truthy("welcome false: the chat is shown", shown(f, f.$("#chat")));
  f.close();

  const m = await open({});
  check("no welcome message (older code.js): no screen", shown(m, m.$("#welcome")), false);
  truthy("no welcome message: the chat is shown", shown(m, m.$("#chat")));
  check("no welcome message: nothing is posted", m.done().length, 0);
  m.close();
}

async function content() {
  const p = await open({ welcome: true });
  const box = p.$("#welcome");
  if (!box) { failed++; console.log(" FAIL  #welcome is missing"); p.close(); return; }
  const role = box.getAttribute("role");
  truthy("role is dialog or region", role === "dialog" || role === "region");
  const lbl = box.getAttribute("aria-label") || (box.getAttribute("aria-labelledby") && (p.d.getElementById(box.getAttribute("aria-labelledby")) || {}).textContent);
  truthy("the screen has a label", lbl && lbl.trim());

  const title = box.querySelector("h1, h2, [role=heading]");
  truthy("there is a title", title && title.textContent.trim());

  const rows = [...box.querySelectorAll(".wrow")];
  truthy("4 to 5 feature rows (got " + rows.length + ")", rows.length >= 4 && rows.length <= 5);
  check("every row has an svg icon", rows.map((r) => !!r.querySelector("svg")), rows.map(() => true));

  const illo = box.querySelector("svg.willo");
  truthy("there is an svg illustration", illo);
  check("the illustration is hidden from screen readers", illo && illo.getAttribute("aria-hidden"), "true");
  check("the illustration has no raster or external image", illo && illo.querySelectorAll("image, use[href^=http], foreignObject").length, 0);

  const field = box.querySelector("textarea");
  truthy("there is the composer field in the screen", field);
  check("the composer field has the focus", p.d.activeElement && p.d.activeElement.id, "cInput");


  truthy("strings live in one const WELCOME_TEXT object", /const\s+WELCOME_TEXT\s*=\s*\{/.test(HTML));
  check("no script errors", p.errors, []);
  p.close();
}

async function dismiss() {
  // The first message
  let p = await open({ welcome: true });
  p.sendText("Привіт");
  await wait();
  check("send: the screen is hidden", shown(p, p.$("#welcome")), false);
  truthy("send: the chat is shown", shown(p, p.$("#chat")));
  truthy("send: still in chat mode", p.d.body.classList.contains("chatmode"));
  check("send: welcome-done is posted once", p.done(), [{ type: "welcome-done" }]);
  check("send: the chat input has the focus", p.d.activeElement && p.d.activeElement.id, "cInput");
  check("a second send does not reach the screen", p.sendText("ще"), false);
  p.key("Escape", p.d.body);
  await wait();
  check("Esc after it does not post again", p.done().length, 1);
  check("the window was not sent to the bar", p.posted.filter((m) => m && m.type === "ui-mode" && m.mode === "bar").length, 0);
  check("no script errors", p.errors, []);
  p.close();

  // Esc
  p = await open({ welcome: true });
  p.key("Escape");
  await wait();
  check("Esc: the screen is hidden", shown(p, p.$("#welcome")), false);
  truthy("Esc: the chat is shown", shown(p, p.$("#chat")));
  check("Esc: welcome-done is posted once", p.done(), [{ type: "welcome-done" }]);
  p.close();

  // Enter with an empty field does not close the screen.
  p = await open({ welcome: true });
  p.key("Enter", p.d.querySelector("#welcome textarea"));
  await wait();
  truthy("Enter on an empty field: the screen stays", shown(p, p.$("#welcome")));
  check("Enter on an empty field: no welcome-done", p.done().length, 0);
  p.close();

  // After close the screen is hidden for screen readers too, not only by CSS.
  p = await open({ welcome: true });
  p.sendText("Привіт");
  await wait();
  check("send: #welcome gets the hidden attribute back", p.$("#welcome").hidden, true);
  p.close();

  // The settings icon in the header also closes the screen first.
  p = await open({ welcome: true });
  p.$("#setbtn").click();
  await wait();
  check("settings icon: the screen is hidden", shown(p, p.$("#welcome")), false);
  check("settings icon: welcome-done is posted once", p.done(), [{ type: "welcome-done" }]);
  truthy("settings icon: #settings is shown", shown(p, p.$("#settings")));
  p.close();

  // Esc without the screen does nothing.
  p = await open({ welcome: false });
  p.key("Escape");
  await wait();
  check("no screen: Esc posts nothing", p.done().length, 0);
  p.close();
}

async function themes() {
  const p = await open({ welcome: true, theme: "dark" });
  truthy("dark theme is on", p.d.documentElement.classList.contains("dark"));
  truthy("dark theme: the screen is shown", shown(p, p.$("#welcome")));

  const css = [...p.d.querySelectorAll("style")].map((s) => s.textContent).join("\n").replace(/\/\*[\s\S]*?\*\//g, "");
  // Plain rules whose selector names the welcome parts.
  const rules = [];
  css.replace(/([^{}]+)\{([^{}]*)\}/g, (all, sel, body) => {
    if (/#welcome|\.wrow|\.willo|\.welcome/.test(sel) && !/^\s*@/.test(sel)) rules.push({ sel: sel.trim(), body });
    return all;
  });
  const own = rules.filter((r) => /#welcome|\.wrow|\.willo/.test(r.sel));
  truthy("the welcome screen has its own CSS rules", own.length >= 2);
  truthy("the illustration takes colors from tokens",
         rules.some((r) => /willo/.test(r.sel) && /var\(--/.test(r.body)) || /var\(--/.test((p.$("svg.willo") || {}).outerHTML || ""));
  truthy("there is an entrance animation", rules.some((r) => /animation/.test(r.body)) || /@keyframes\s+w/.test(css));
  truthy("the animation stops for reduced motion",
         /@media\s*\(\s*prefers-reduced-motion\s*:\s*reduce\s*\)\s*\{[^@]*(#welcome|\.wrow|\.willo)/.test(css));
  check("no script errors", p.errors, []);
  p.close();
}

// The screen follows the language setting, and its buttons still work after a re-render.
async function language() {
  let p = await open({ welcome: true, lang: "en" });
  check("en: no Cyrillic on the screen", /[\u0400-\u04FF]/.test(p.$("#welcome").textContent + p.$("#welcome").getAttribute("aria-label")), false);
  p.sendText("Hi"); await wait();
  check("en: a sent message closes the screen once", p.done().length, 1);
  p.close();
  p = await open({ welcome: true });
  p.pm({ type: "settings", scale: 1, theme: "system", lang: "en" }); await wait();
  check("a switch to en re-renders the screen", /[\u0400-\u04FF]/.test(p.$("#welcome").textContent), false);
  p.sendText("Hi"); await wait();
  check("after the re-render a sent message still closes it", p.done().length, 1);
  check("no script errors", p.errors, []);
  p.close();
}

(async () => {
  for (const t of [codeFlag, appears, content, dismiss, themes, language]) {
    console.log(`\n${t.name}`);
    try { await t(); } catch (e) { failed++; console.log(" FAIL  threw: " + (e.stack || e).toString().split("\n").slice(0, 3).join(" | ")); }
  }
  console.log(failed ? `\n${failed} failed` : "\nall passed");
  process.exit(failed ? 1 : 0);
})();
