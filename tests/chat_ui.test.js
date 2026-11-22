// plugin/code.js (the Figma sandbox) and ChatCore (the pure part of plugin/ui.html),
// without a browser. Contract in docs/CHAT.md.
//
//   1. code.js: the window mode (thin bar or chat) and its size, settings,
//      update notices, identity and doc id, other messages, and the exec path
//      (result shapes, errors, logs, abort, timeout).
//   2. ChatCore in <script id="chat-core">: text rendering, the state
//      reducer and the slash commands.
//
// Window sizes are read from UI_MODES in code.js, so a size change does not
// break a test; the tests check how the size follows the mode and the scale.
//
//     node tests/chat_ui.test.js
const fs = require("fs");
const path = require("path");
const ROOT = path.join(__dirname, "..");
const SRC = fs.readFileSync(path.join(ROOT, "plugin", "code.js"), "utf8");
const VERSION = (SRC.match(/const PLUGIN_VERSION = "([^"]+)"/) || [])[1];
const MODES = SRC.match(/UI_MODES = \{ bar: \[(\d+), (\d+)\], chat: \[(\d+), (\d+)\] \}/).slice(1).map(Number);
const BAR = MODES.slice(0, 2), CHAT = MODES.slice(2);
const at = (size, s) => size.map((v) => Math.round(v * s));
const CYR = /[Ѐ-ӿ]/;

let failed = 0;
function check(label, actual, expected) {
  const a = JSON.stringify(actual), e = JSON.stringify(expected);
  const ok = a === e;
  if (!ok) failed++;
  console.log(`${ok ? "  ok  " : " FAIL "} ${label}${ok ? "" : `\n         got ${a}\n         want ${e}`}`);
}
function truthy(label, v) { check(label, !!v, true); }
const tick = (ms = 20) => new Promise((r) => setTimeout(r, ms));

// ─── 1. code.js ───────────────────────────────────────────────────────────

// A fake figma. opt: store (clientStorage), pluginData, storageOff (true, "get", "set"),
// pluginDataOff, pages, fileKey ("throw" for a getter that throws), selection, nodes, noShow.
function loadPlugin(opt = {}) {
  const calls = { showUI: [], resize: [], posted: [], set: [], show: 0, opened: [], closed: [], pluginData: [] };
  // A user who finished the first-run onboarding (tests/onboard_code.test.js).
  const store = Object.assign({ "figmosha-onboarded": 1 }, opt.store || {});
  const pluginData = Object.assign({ "figmosha-welcome": "1" }, opt.pluginData || {});
  const off = opt.storageOff;
  const ui = {
    onmessage: null,
    postMessage(m) { calls.posted.push(m); },
    resize(w, h) { calls.resize.push([w, h]); },
  };
  if (!opt.noShow) ui.show = () => { calls.show++; };
  const figma = {
    showUI(html, o) { calls.showUI.push(o); },
    ui,
    clientStorage: {
      async getAsync(k) { if (off === true || off === "get") throw new Error("off"); return store[k]; },
      async setAsync(k, v) { if (off === true || off === "set") throw new Error("off"); store[k] = v; calls.set.push([k, v]); },
    },
    currentPage: { name: "Page 1", selection: opt.selection || [] },
    openExternal(u) { calls.opened.push(u); },
    closePlugin(m) { calls.closed.push(m); },
    root: {
      name: opt.name || "Banner",
      children: "pages" in opt ? opt.pages : [{ id: "0:1" }],
      getPluginData(k) { if (opt.pluginDataOff) throw new Error("no data"); return pluginData[k] || ""; },
      setPluginData(k, v) { if (opt.pluginDataOff) throw new Error("no data"); pluginData[k] = v; calls.pluginData.push([k, v]); },
    },
    getNodeByIdAsync: async (id) => (opt.nodes || {})[id] || null,
    variables: {},
  };
  if ("fileKey" in opt) {
    if (opt.fileKey === "throw") Object.defineProperty(figma, "fileKey", { get() { throw new Error("no key"); } });
    else figma.fileKey = opt.fileKey;
  }
  // opt.clock: code.js's own timers run on that fake clock (scripts in an exec keep the real one).
  const c = opt.clock || { setTimeout, clearTimeout, setInterval, clearInterval };
  const HELPERS = new Function("figma", "__html__", "setTimeout", "clearTimeout", "setInterval", "clearInterval",
    SRC + "\nreturn HELPERS;")(figma, "<html>", c.setTimeout, c.clearTimeout, c.setInterval, c.clearInterval);
  return { figma, calls, store, pluginData, HELPERS };
}

// A fake clock for code.js timers: tick(ms) moves it on and runs what is due. No real wait.
function fakeClock() {
  let now = 0;
  const q = [];
  return {
    setTimeout(fn, ms = 0) { const t = { fn, at: now + ms }; q.push(t); return t; },
    clearTimeout(t) { const i = q.indexOf(t); if (i >= 0) q.splice(i, 1); },
    setInterval() { return 0; },
    clearInterval() {},
    async tick(ms) {
      now += ms;
      for (;;) {
        const due = q.filter((t) => t.at <= now).sort((a, b) => a.at - b.at)[0];
        if (!due) break;
        q.splice(q.indexOf(due), 1);
        due.fn();
      }
      await new Promise((r) => setImmediate(r));
    },
  };
}
const ofType = (p, t) => p.calls.posted.filter((m) => m && m.type === t);
const send = (p, m) => p.figma.ui.onmessage(m);

async function modeTests() {
  // Fresh install: the plugin opens as the thin bar, nothing is resized.
  let p = loadPlugin();
  await tick();
  const bar = p.calls.showUI[0];
  check("opens as the bar", [bar.width, bar.height], BAR);
  check("no resize when nothing is stored", p.calls.resize, []);

  // The chat was open last time: the window comes back as the chat.
  p = loadPlugin({ store: { "figmosha-ui-mode": "chat" } });
  await tick();
  const last = p.calls.resize[p.calls.resize.length - 1] || [0, 0];
  check("restores the chat size, in one resize", [p.calls.resize.length, last], [1, CHAT]);
  truthy("tells the UI it is in chat mode",
         p.calls.posted.some((m) => m.type === "ui-mode" && m.mode === "chat"));

  // The chat icon asks for the chat, then for the bar again.
  p = loadPlugin();
  await tick();
  await p.figma.ui.onmessage({ type: "ui-mode", mode: "chat" });
  await tick();
  const chatSize = p.calls.resize[p.calls.resize.length - 1] || [0, 0];
  check("chat icon opens the chat", chatSize, CHAT);
  check("chat mode is remembered", p.store["figmosha-ui-mode"], "chat");

  await p.figma.ui.onmessage({ type: "ui-mode", mode: "bar" });
  await tick();
  check("back to the bar size", p.calls.resize[p.calls.resize.length - 1], BAR);
  check("bar mode is remembered", p.store["figmosha-ui-mode"], "bar");

  const before = p.calls.resize.length;
  await p.figma.ui.onmessage({ type: "ui-mode", mode: "fullscreen" });
  await tick();
  check("unknown mode is ignored", p.calls.resize.length, before);
  check("unknown mode is not stored", p.store["figmosha-ui-mode"], "bar");

  // A stored value from a broken run does not break the start.
  p = loadPlugin({ store: { "figmosha-ui-mode": "garbage" } });
  await tick();
  check("bad stored mode keeps the bar", p.calls.resize, []);

  // clientStorage off (it throws): the plugin still starts, and the chat icon still works.
  p = loadPlugin({ storageOff: true });
  await tick();
  await p.figma.ui.onmessage({ type: "ui-mode", mode: "chat" });
  await tick();
  const off = p.calls.resize[p.calls.resize.length - 1] || [0, 0];
  check("storage off: opens as the bar", p.calls.showUI[0].height, BAR[1]);
  check("storage off: the chat still opens", off, CHAT);
}

// Settings page: scale and theme, stored per user, sent to the UI on start.
async function settingsTests() {
  let p = loadPlugin();
  await tick();
  const opts = p.calls.showUI[0];
  check("UI follows the Figma theme", opts.themeColors, true);
  check("window is hidden until the settings are read", opts.visible, false);
  check("window is shown once after the start", p.calls.show, 1);
  check("empty storage sends the defaults", p.calls.posted.filter((m) => m.type === "settings"),
        [{ type: "settings", scale: 1, theme: "system", lang: "uk" }]);

  await p.figma.ui.onmessage({ type: "ui-mode", mode: "chat" });
  await p.figma.ui.onmessage({ type: "settings", scale: 1.25, theme: "dark" });
  await tick();
  check("scale resizes the chat window", p.calls.resize[p.calls.resize.length - 1], at(CHAT, 1.25));
  check("settings are stored", p.store["figmosha-settings"], { scale: 1.25, theme: "dark", lang: "uk" });
  await p.figma.ui.onmessage({ type: "ui-mode", mode: "bar" });
  check("the bar follows the scale too", p.calls.resize[p.calls.resize.length - 1], at(BAR, 1.25));
  await p.figma.ui.onmessage({ type: "settings", scale: 3, theme: "neon" });
  check("bad values fall back to the defaults", p.store["figmosha-settings"], { scale: 1, theme: "system", lang: "uk" });
  await p.figma.ui.onmessage({ type: "settings", scale: 1, theme: "system", lang: "en" });
  check("the language is stored with the rest", p.store["figmosha-settings"], { scale: 1, theme: "system", lang: "en" });
  await p.figma.ui.onmessage({ type: "settings", scale: 1, theme: "system", lang: "fr" });
  check("a bad language falls back to uk", p.store["figmosha-settings"].lang, "uk");

  p = loadPlugin({ store: { "figmosha-ui-mode": "chat", "figmosha-settings": { scale: 0.9, theme: "light" } } });
  await tick();
  check("stored scale sizes the chat on start", p.calls.resize[p.calls.resize.length - 1], at(CHAT, 0.9));
  check("stored settings go to the UI", p.calls.posted.filter((m) => m.type === "settings"),
        [{ type: "settings", scale: 0.9, theme: "light", lang: "uk" }]);
  check("settings reach the UI before it is shown", p.calls.show, 1);

  p = loadPlugin({ store: { "figmosha-settings": { scale: 1, theme: "dark", lang: "en" } } });
  await tick();
  check("stored language goes to the UI", p.calls.posted.filter((m) => m.type === "settings"),
        [{ type: "settings", scale: 1, theme: "dark", lang: "en" }]);
  await p.figma.ui.onmessage({ type: "rerun" });
  truthy("re-run hint is in the chosen language", p.calls.closed[0] && !CYR.test(p.calls.closed[0]));
}

// Update notices in code.js: skipped versions, links, re-run.
async function noticeTests() {
  const KEY = "figmosha-notice-skip";
  let p = loadPlugin({ store: { [KEY]: { update: "2.4.0", rerun: "2026-11-21.7" } } });
  await tick();
  check("tells the UI the skipped versions at start",
        p.calls.posted.filter((m) => m.type === "notice-skips").pop(),
        { type: "notice-skips", update: "2.4.0", rerun: "2026-11-21.7" });
  p.calls.posted.length = 0;
  await p.figma.ui.onmessage({ type: "need-identity" });
  await tick();
  truthy("and again when the UI asks for its identity",
         p.calls.posted.some((m) => m.type === "notice-skips" && m.update === "2.4.0"));

  await p.figma.ui.onmessage({ type: "notice-skip", kind: "update", version: "2.5.0" });
  await tick();
  check("skip keeps one version per kind", p.store[KEY], { update: "2.5.0", rerun: "2026-11-21.7" });
  await p.figma.ui.onmessage({ type: "notice-skip", kind: "evil", version: "1" });
  await p.figma.ui.onmessage({ type: "notice-skip", kind: "update", version: { x: 1 } });
  await tick();
  check("bad skip is ignored", p.store[KEY], { update: "2.5.0", rerun: "2026-11-21.7" });

  p = loadPlugin();
  await tick();
  check("nothing stored: empty skips", p.calls.posted.filter((m) => m.type === "notice-skips").pop(),
        { type: "notice-skips", update: null, rerun: null });

  await p.figma.ui.onmessage({ type: "open-url", url: "https://github.com/denysosadchyi/figmosha2/releases/tag/v9.9.9" });
  await p.figma.ui.onmessage({ type: "open-url", url: "https://github.com/denysosadchyi/figmosha2evil/x" });
  await p.figma.ui.onmessage({ type: "open-url", url: "https://evil.example/" });
  check("Update opens only the project's pages", p.calls.opened,
        ["https://github.com/denysosadchyi/figmosha2/releases/tag/v9.9.9"]);

  await p.figma.ui.onmessage({ type: "rerun" });
  check("re-run closes the plugin with a hint", p.calls.closed.length, 1);
  truthy("the hint is Ukrainian by default", CYR.test(p.calls.closed[0] || ""));
}

// ─── identity and doc id ──────────────────────────────────────────────────

async function identityTests() {
  let p = loadPlugin({ fileKey: "abc", pluginData: { "figmosha-doc": "dKeep" } });
  const id = ofType(p, "identity");
  check("identity is posted once on load", id.length, 1);
  check("identity carries file key, name, doc id and the build",
        id[0], { type: "identity", fileKey: "abc", name: "Banner", docSig: "dKeep", pluginVersion: VERSION });
  check("an existing doc id is not rewritten", p.calls.pluginData, []);

  await send(p, { type: "need-identity" });
  check("need-identity posts the identity again, same doc id",
        ofType(p, "identity").map((m) => m.docSig), ["dKeep", "dKeep"]);

  p = loadPlugin();
  const first = ofType(p, "identity")[0];
  check("no fileKey (dev plugin) gives null", first.fileKey, null);
  truthy("a new doc id starts with d", /^d[0-9a-z]+$/.test(first.docSig));
  check("the new doc id is stored in the document", p.calls.pluginData, [["figmosha-doc", first.docSig]]);
  await send(p, { type: "need-identity" });
  check("the stored doc id is reused on the next ask", ofType(p, "identity")[1].docSig, first.docSig);
  check("it is written only once", p.calls.pluginData.length, 1);

  const a = loadPlugin(), b = loadPlugin();
  truthy("two fresh files get different doc ids",
         ofType(a, "identity")[0].docSig !== ofType(b, "identity")[0].docSig);

  p = loadPlugin({ fileKey: "throw" });
  check("a throwing fileKey getter gives null", ofType(p, "identity")[0].fileKey, null);

  // Plugin data off: fall back to a hash of the page ids.
  const fa = ofType(loadPlugin({ pluginDataOff: true, pages: [{ id: "0:1" }, { id: "5:2" }] }), "identity")[0].docSig;
  const fb = ofType(loadPlugin({ pluginDataOff: true, pages: [{ id: "0:1" }, { id: "5:2" }] }), "identity")[0].docSig;
  const fc = ofType(loadPlugin({ pluginDataOff: true, pages: [{ id: "0:1" }] }), "identity")[0].docSig;
  truthy("fallback id is a hex hash", /^[0-9a-f]+$/.test(fa));
  check("same pages give the same fallback id", fa, fb);
  truthy("other pages give another fallback id", fa !== fc);
  const broken = loadPlugin({ pluginDataOff: true, pages: null });
  check("no pages at all gives a null doc id", ofType(broken, "identity")[0].docSig, null);
}

// ─── window start-up and resize ───────────────────────────────────────────

async function windowTests() {
  let p = loadPlugin({ store: { "figmosha-settings": { scale: 1.1, theme: "dark" } } });
  await tick();
  check("bar mode with a stored scale resizes on start", p.calls.resize, [at(BAR, 1.1)]);
  check("bar start does not post ui-mode", ofType(p, "ui-mode"), []);

  p = loadPlugin({ store: { "figmosha-ui-mode": "chat" } });
  await tick();
  check("chat start does not store the mode again", p.calls.set.filter((s) => s[0] === "figmosha-ui-mode"), []);

  p = loadPlugin({ store: { "figmosha-settings": "junk" } });
  await tick();
  check("non-object settings fall back to defaults", ofType(p, "settings"), [{ type: "settings", scale: 1, theme: "system", lang: "uk" }]);
  check("and do not resize", p.calls.resize, []);

  p = loadPlugin({ noShow: true });
  await tick();
  check("no ui.show (old Figma): start still posts settings", ofType(p, "settings").length, 1);

  p = loadPlugin({ storageOff: true });
  await tick();
  check("storage off: still shown once", p.calls.show, 1);
  check("storage off: defaults posted", ofType(p, "settings"), [{ type: "settings", scale: 1, theme: "system", lang: "uk" }]);

  // ui-mode toggles post the new mode back to the UI.
  p = loadPlugin();
  await tick();
  await send(p, { type: "ui-mode", mode: "chat" });
  await send(p, { type: "ui-mode", mode: "bar" });
  check("each ui-mode change is echoed to the UI", ofType(p, "ui-mode").map((m) => m.mode), ["chat", "bar"]);
  await send(p, { type: "ui-mode", mode: "toString" });
  check("an inherited name is not a mode", ofType(p, "ui-mode").length, 2);
  await send(p, { type: "ui-mode" });
  check("a missing mode is ignored", ofType(p, "ui-mode").length, 2);
}

// ─── settings message ─────────────────────────────────────────────────────

async function settingsEdgeTests() {
  let p = loadPlugin();
  await tick();
  await send(p, { type: "settings", scale: 0.9 });
  check("scale only: theme falls back to system", p.store["figmosha-settings"], { scale: 0.9, theme: "system", lang: "uk" });
  check("scale 0.9 resizes the bar", p.calls.resize[p.calls.resize.length - 1], at(BAR, 0.9));
  await send(p, { type: "settings", theme: "light" });
  check("theme only: scale falls back to 1", p.store["figmosha-settings"], { scale: 1, theme: "light", lang: "uk" });
  check("and the bar is back to scale 1", p.calls.resize[p.calls.resize.length - 1], BAR);
  await send(p, { type: "settings", scale: "1.25", theme: "dark" });
  check("a string scale is not accepted", p.store["figmosha-settings"], { scale: 1, theme: "dark", lang: "uk" });
  check("settings do not post back to the UI", ofType(p, "settings").length, 1);

  p = loadPlugin({ storageOff: "set" });
  await tick();
  let threw = false;
  try { await send(p, { type: "settings", scale: 1.25, theme: "dark" }); } catch (e) { threw = true; }
  check("storage off: settings do not throw", threw, false);
  check("storage off: the window still follows the scale", p.calls.resize[p.calls.resize.length - 1], at(BAR, 1.25));
  threw = false;
  try { await send(p, { type: "ui-mode", mode: "chat" }); } catch (e) { threw = true; }
  check("storage off: ui-mode does not throw", threw, false);
  check("storage off: ui-mode is still echoed", ofType(p, "ui-mode").pop(), { type: "ui-mode", mode: "chat" });
}

// ─── notice skips ─────────────────────────────────────────────────────────

async function noticeEdgeTests() {
  const KEY = "figmosha-notice-skip";
  let p = loadPlugin({ store: { [KEY]: { update: 7, rerun: "x" } } });
  await tick();
  check("non-string stored versions are sent as null",
        ofType(p, "notice-skips").pop(), { type: "notice-skips", update: null, rerun: "x" });

  p = loadPlugin({ store: { [KEY]: "junk" } });
  await tick();
  check("junk stored skips give empty skips", ofType(p, "notice-skips").pop(), { type: "notice-skips", update: null, rerun: null });
  await send(p, { type: "notice-skip", kind: "rerun", version: VERSION });
  check("a skip over junk starts a fresh object", p.store[KEY], { rerun: VERSION });

  p = loadPlugin();
  await tick();
  await send(p, { type: "notice-skip", kind: "rerun", version: "b1" });
  check("rerun skip is stored", p.store[KEY], { rerun: "b1" });
  await send(p, { type: "need-identity" });
  check("need-identity reports the new skip", ofType(p, "notice-skips").pop(), { type: "notice-skips", update: null, rerun: "b1" });
  check("notice-skip itself posts nothing back", ofType(p, "notice-skips").length, 2);

  p = loadPlugin({ storageOff: true });
  await tick();
  check("storage off: empty skips at start", ofType(p, "notice-skips").pop(), { type: "notice-skips", update: null, rerun: null });
  let threw = false;
  try { await send(p, { type: "notice-skip", kind: "update", version: "2.0.0" }); } catch (e) { threw = true; }
  check("storage off: skipping does not throw", threw, false);
}

// ─── re-run, open-url, unknown messages ──────────────────────────────────

async function miscMessageTests() {
  let p = loadPlugin();
  await tick();
  await send(p, { type: "rerun" });
  truthy("re-run text has no dashes or quotes", !/[—–"'«»„“”]/.test(p.calls.closed[0]));

  p = loadPlugin();
  await tick();
  await send(p, { type: "open-url", url: "https://github.com/denysosadchyi/figmosha2" });
  await send(p, { type: "open-url", url: "http://github.com/denysosadchyi/figmosha2/x" });
  await send(p, { type: "open-url", url: { toString() { return "https://github.com/denysosadchyi/figmosha2/x"; } } });
  await send(p, { type: "open-url" });
  check("no slash, http, non-string or missing url is not opened", p.calls.opened, []);

  const before = p.calls.posted.length;
  await send(p, { type: "who-knows" });
  await send(p, { type: "abort" });
  check("unknown messages and an abort without id post nothing", p.calls.posted.length, before);
  check("and change no storage", p.calls.set, []);
}

// ─── exec ─────────────────────────────────────────────────────────────────

async function execTests() {
  const p = loadPlugin({ selection: [{ id: "1:1", name: "T", type: "TEXT", width: 10, height: 5, characters: "hi" }] });
  await tick();
  const run = async (id, code, extra = {}) => {
    await send(p, Object.assign({ type: "exec", id, code }, extra));
    return p.calls.posted.filter((m) => m && m.id === id && (m.type === "result" || m.type === "error")).pop();
  };

  check("number result", await run("n", "return 40 + 2"), { type: "result", id: "n", value: 42 });
  check("string result", await run("s", "return 'x'"), { type: "result", id: "s", value: "x" });
  check("boolean result", await run("b", "return false"), { type: "result", id: "b", value: false });
  check("object result is sent as json once", await run("o", "return {a: [1, 2]}"),
        { type: "result", id: "o", valueJson: "{\"a\":[1,2]}" });
  check("null result", await run("z", "return null"), { type: "result", id: "z", valueJson: "null" });
  check("no return gives Done", await run("u", "1 + 1"), { type: "result", id: "u", value: null, text: "Done" });
  check("no return with logs gives the logs", await run("l", "print('a'); print('b', 2)"),
        { type: "result", id: "l", value: null, text: "a\nb 2" });
  check("NaN goes as text", await run("nan", "return NaN"), { type: "result", id: "nan", value: null, text: "NaN" });
  check("Infinity goes as text", await run("inf", "return Infinity"), { type: "result", id: "inf", value: null, text: "Infinity" });
  check("BigInt goes as text", await run("big", "return 10n"), { type: "result", id: "big", value: "10", text: "10" });
  check("a function goes as text", (await run("fn", "return function f() {}")).text, "function f() {}");
  const cyc = await run("cyc", "const o = {}; o.o = o; return o");
  check("a cyclic object falls back to its String", cyc, { type: "result", id: "cyc", value: "[object Object]" });
  check("an object with toJSON undefined falls back", await run("tj", "return { toJSON() { return undefined; } }"),
        { type: "result", id: "tj", value: "[object Object]" });

  const err = await run("e", "throw new Error('boom')");
  check("a throw gives an error message", [err.type, err.id, err.text], ["error", "e", "boom"]);
  truthy("the error carries a stack", typeof err.stack === "string" && err.stack.length > 0);
  check("a thrown string gives its text and no stack", await run("e2", "throw 'plain'"),
        { type: "error", id: "e2", text: "plain", stack: null });
  check("a syntax error is reported, not thrown", (await run("e3", "return (")).type, "error");

  // print(): objects pretty-printed, lines batched, flushed before the result.
  p.calls.posted.length = 0;
  await run("pl", "print({k: 1}); print(null, undefined); return 1");
  const logs = ofType(p, "log").filter((m) => m.id === "pl");
  check("logs flush in one batch before the result", logs.map((m) => m.lines), [["{\n  \"k\": 1\n}", "null undefined"]]);
  const order = p.calls.posted.filter((m) => m.id === "pl").map((m) => m.type);
  check("log comes before result", order, ["log", "result"]);

  p.calls.posted.length = 0;
  await run("many", "for (let i = 0; i < 450; i++) print(i); return 0");
  const sizes = ofType(p, "log").filter((m) => m.id === "many").map((m) => m.lines.length);
  check("450 lines go in batches of 200", sizes, [200, 200, 50]);

  // The log flush timer runs on a fake clock; the script waits on a gate, not on time.
  const clock = fakeClock();
  const pc = loadPlugin({ clock });
  await tick();
  let open;
  pc.HELPERS.__gate = new Promise((r) => { open = r; });
  const slow = send(pc, { type: "exec", id: "slow", code: "print('early'); await h.__gate; return 1" });
  await clock.tick(60);
  check("before 100 ms the log is still held", ofType(pc, "log").filter((m) => m.id === "slow").length, 0);
  await clock.tick(40);
  const early = ofType(pc, "log").filter((m) => m.id === "slow");
  check("a slow script streams its logs after 100 ms, while still running",
        [early.map((m) => m.lines), pc.calls.posted.some((m) => m.id === "slow" && m.type === "result")], [[["early"]], false]);
  open();
  await slow;
  check("and no second copy at the end", ofType(pc, "log").filter((m) => m.id === "slow").length, 1);

  p.calls.posted.length = 0;
  await run("er2", "print('before'); throw new Error('x')");
  check("logs are flushed before an error too",
        p.calls.posted.filter((m) => m.id === "er2").map((m) => m.type), ["log", "error"]);

  // figma, print and h are in scope; h is per exec.
  check("figma is the sandbox object", (await run("fg", "return figma.root.name")).value, "Banner");
  check("h.sel shows the TEXT characters", (await run("sel", "return h.sel()")).valueJson,
        JSON.stringify([{ id: "1:1", name: "T", type: "TEXT", w: 10, h: 5, chars: "hi" }]));
  check("h.resolve('sel') gives the first selected node", (await run("rs", "return (await h.resolve('sel')).id")).value, "1:1");
  check("h.resolve('page') gives the current page", (await run("rp", "return (await h.resolve('page')).name")).value, "Page 1");
  check("h.aborted is false in a normal run", (await run("ab0", "return h.aborted()")).value, false);
  check("h.ck returns true in a normal run", (await run("ck0", "return h.ck()")).value, true);
  check("h.aborted is not shared on HELPERS", (await run("own", "return Object.getPrototypeOf(h).aborted === undefined")).value, true);

  // abort: the bridge's abort arrives while the script is running.
  const pending = send(p, { type: "exec", id: "ab", code: "await new Promise((r) => setTimeout(r, 60)); h.ck(); return 'not aborted'" });
  await send(p, { type: "abort", id: "ab" });
  await pending;
  const ab = p.calls.posted.filter((m) => m.id === "ab").pop();
  check("abort makes h.ck throw", [ab.type, /aborted/.test(ab.text)], ["error", true]);
  check("the id is free again after the run", (await run("ab", "return h.aborted()")).value, false);

  // timeout: h.ck checks the sandbox clock.
  const to = await run("to", "await new Promise((r) => setTimeout(r, 80)); h.ck(); return 1", { timeout: 0.03 });
  check("past the timeout h.ck throws", [to.type, /timeout/.test(to.text)], ["error", true]);
  check("timeout is in seconds: 40 ms into a 0.5 s run h.ck passes",
        (await run("to2", "await new Promise((r) => setTimeout(r, 40)); return h.ck()", { timeout: 0.5 })).value, true);
  check("a string timeout means no deadline", (await run("to3", "await new Promise((r) => setTimeout(r, 30)); return h.ck()", { timeout: "0.001" })).value, true);

  // withFonts warnings go to this exec's print, and not after it.
  p.calls.posted.length = 0;
  p.figma.loadFontAsync = async () => {};
  const mixed = Symbol("mixed");
  const node = { findAll: () => [{ type: "TEXT", name: "Mixed", fontName: mixed }, { type: "TEXT", name: "A", fontName: { family: "Inter", style: "Regular" } }] };
  p.HELPERS.__node = node;
  await run("wf2", "await h.withFonts(h.__node, async () => 1); return 2");
  const warn = ofType(p, "log").filter((m) => m.id === "wf2").flatMap((m) => m.lines);
  truthy("withFonts warning reaches the exec log", warn.length === 1 && /skipped 1 mixed-font/.test(warn[0]));
  pc.calls.posted.length = 0;
  pc.figma.loadFontAsync = async () => {};
  await pc.HELPERS.withFonts(node, async () => 1);
  await clock.tick(150);
  check("outside an exec the warning goes nowhere", ofType(pc, "log"), []);

  // A message without a type is not an exec.
  const n = p.calls.posted.length;
  await send(p, { id: "x", code: "return 1" });
  check("a message without type does not run code", p.calls.posted.length, n);
}

// ─── 2. ChatCore in ui.html ───────────────────────────────────────────────

function loadCore() {
  const html = fs.readFileSync(path.join(ROOT, "plugin", "ui.html"), "utf8");
  const m = html.match(/<script id="chat-core">([\s\S]*?)<\/script>/);
  if (!m) return null;
  return new Function(m[1] + "\nreturn ChatCore;")();
}

function coreTests() {
  const C = loadCore();
  truthy("ui.html has <script id=\"chat-core\"> with ChatCore", C);
  if (!C) return;

  // renderText: safe HTML from Claude's markdown-ish text
  check("escapes HTML", C.renderText("<img src=x onerror=alert(1)>"),
        "&lt;img src=x onerror=alert(1)&gt;");
  check("bold", C.renderText("це **важливо**"), "це <b>важливо</b>");
  check("inline code", C.renderText("run `figmosha.py tree`"), "run <code>figmosha.py tree</code>");
  check("line breaks", C.renderText("a\nb"), "a<br>b");
  check("code block keeps its lines and is escaped",
        C.renderText("до\n```js\nif (a < b) x()\n```\nпісля"),
        "до<br><pre>if (a &lt; b) x()</pre><br>після");
  check("no markup inside code", C.renderText("`**x**`"), "<code>**x**</code>");
  check("empty text", C.renderText(""), "");

  // headings: compact bold lines, not huge
  check("heading ###", C.renderText("### Skills"), '<div class="mh" role="heading" aria-level="3">Skills</div>');
  check("heading levels 1 and 6", [C.renderText("# A"), C.renderText("###### F")],
        ['<div class="mh" role="heading" aria-level="1">A</div>', '<div class="mh" role="heading" aria-level="6">F</div>']);
  check("heading between lines eats the line breaks", C.renderText("до\n## Назва\n\nпісля"),
        'до<div class="mh" role="heading" aria-level="2">Назва</div>після');
  check("heading keeps inline markdown and escapes", C.renderText("## **Ок** `a<b>`"),
        '<div class="mh" role="heading" aria-level="2"><b>Ок</b> <code>a&lt;b&gt;</code></div>');
  check("seven hashes or no space: not a heading", [C.renderText("####### x"), C.renderText("#tag")], ["####### x", "#tag"]);
  check("closing hashes are dropped", C.renderText("## Назва ##"), '<div class="mh" role="heading" aria-level="2">Назва</div>');

  // GFM pipe tables
  const T = "### Skills\n| Skill | Source | Tokens |\n|-------|:------:|-------:|\n| **a** | `x\\|` | 12 |\n| b<i> | y |\nпісля";
  check("table: header, alignment, inline markdown, escaped cells, short rows padded", C.renderText(T),
        '<div class="mh" role="heading" aria-level="3">Skills</div>'
        + '<div class="mtable"><table><thead><tr><th>Skill</th><th style="text-align:center">Source</th><th style="text-align:right">Tokens</th></tr></thead>'
        + '<tbody><tr><td><b>a</b></td><td style="text-align:center"><code>x|</code></td><td style="text-align:right">12</td></tr>'
        + '<tr><td>b&lt;i&gt;</td><td style="text-align:center">y</td><td style="text-align:right"></td></tr></tbody></table></div>'
        + "після");
  check("table without outer pipes", C.renderText("a | b\n--- | ---\n1 | 2"),
        '<div class="mtable"><table><thead><tr><th>a</th><th>b</th></tr></thead><tbody><tr><td>1</td><td>2</td></tr></tbody></table></div>');
  check("a lone pipe line stays text", C.renderText("a | b\nc"), "a | b<br>c");
  check("separator with a different column count: not a table", C.renderText("| a | b |\n|---|\n| 1 | 2 |"),
        "| a | b |<br>|---|<br>| 1 | 2 |");
  check("table cells cannot inject HTML", C.renderText("| <img src=x onerror=1> |\n|---|\n| \" |"),
        '<div class="mtable"><table><thead><tr><th>&lt;img src=x onerror=1&gt;</th></tr></thead><tbody><tr><td>&quot;</td></tr></tbody></table></div>');
  check("pipes inside a code block are not a table", C.renderText("```\n| a |\n|---|\n```"), "<pre>| a |\n|---|</pre>");

  check("action letters", [0, 1, 2, 3].map(C.actionLetter), ["A", "B", "C", "D"]);

  // reduce: the chat view state
  const s0 = C.initialState();
  check("initial state", s0, { messages: [], busy: false, activity: [], stream: "", commands: [] });

  const me = { id: 1, from: "me", text: "привіт", t: 1 };
  const cl = { id: 2, from: "claude", text: "Привіт!", t: 2 };

  let s = C.reduce(s0, { kind: "history", messages: [me], busy: true, activity: [{ text: "x", t: 1 }] });
  check("history replaces the state", [s.messages.length, s.busy, s.activity.length], [1, true, 1]);
  check("reduce does not change its input", s0.messages.length, 0);

  s = C.reduce(s, { kind: "message", message: me });
  check("same message id is not added twice", s.messages.length, 1);

  s = C.reduce(s, { kind: "delta", text: "При" });
  s = C.reduce(s, { kind: "delta", text: "віт" });
  check("deltas build the streaming text", s.stream, "Привіт");

  s = C.reduce(s, { kind: "message", message: cl });
  check("Claude's message lands and ends the stream",
        [s.messages.map((m) => m.id), s.stream], [[1, 2], ""]);

  s = C.reduce(C.initialState(), { kind: "busy", busy: true });
  for (let i = 0; i < 35; i++) s = C.reduce(s, { kind: "activity", text: "крок " + i, t: i });
  check("activity keeps the last 30", [s.activity.length, s.activity[0].text], [30, "крок 5"]);

  s = C.reduce(s, { kind: "delta", text: "half" });
  s = C.reduce(s, { kind: "busy", busy: false });
  check("idle clears activity and the stream", [s.busy, s.activity, s.stream], [false, [], ""]);

  s = C.reduce(C.initialState(), { kind: "unknown-kind", x: 1 });
  check("unknown event changes nothing", s, C.initialState());
}

function slashTests() {
  const C = loadCore();
  const list = [
    { name: "my-skill", description: "Does a thing. And more." },
    { name: "context" }, { name: "compact", hint: "<instructions>" }, { name: "clear", aliases: ["reset", "new"] },
    { name: "code-review", aliases: ["review"] }, { name: "__remote-workflow" }, { name: "security-review" },
  ];
  const names = (text) => C.matchCommands(list, text).map((c) => c.name);
  check("/ lists everything but internal names, known ones first",
        names("/"), ["clear", "compact", "context", "code-review", "security-review", "my-skill"]);
  check("/c: prefix matches", names("/c"), ["clear", "compact", "context", "code-review"]);
  check("/rev: an alias and then a substring", names("/rev"), ["code-review", "security-review"]);
  check("case does not matter", names("/CO"), ["compact", "context", "code-review"]);
  check("/new finds clear by its alias", names("/new"), ["clear"]);
  check("no popup once there is a space", names("/clear now"), []);
  check("no popup for plain text", names("hi /c"), []);
  check("no popup for a path", names("/tmp/x"), []);
  check("no popup for an unknown name", names("/zzz"), []);
  check("Ukrainian hint for a known command", C.commandHint({ name: "clear", description: "Start a new session" }), "Почати нову розмову");
  check("else the first sentence of Claude's description", C.commandHint(list[0]), "Does a thing.");
  check("else nothing", C.commandHint({ name: "x" }), "");
  check("English hint for a known command", C.commandHint({ name: "clear" }, "en"), "Start a new conversation");
  check("every known command has an English hint", Object.keys(C.HINTS).filter((k) => !C.HINTS_EN[k]), []);
  check("English: else Claude's own description", C.commandHint(list[0], "en"), "Does a thing.");
  truthy("there is a built-in fallback list with clear", C.FALLBACK_COMMANDS.some((c) => c.name === "clear"));

  let s = C.initialState();
  check("no commands at the start", s.commands, []);
  s = C.reduce(s, { kind: "history", messages: [], commands: [{ name: "a" }] });
  check("history brings the commands", s.commands, [{ name: "a" }]);
  s = C.reduce(s, { kind: "history", messages: [] });
  check("history without commands keeps them", s.commands, [{ name: "a" }]);
  s = C.reduce(s, { kind: "commands", commands: [{ name: "b" }] });
  check("a commands event replaces them", s.commands, [{ name: "b" }]);
}

(async () => {
  for (const [name, fn] of [["window mode", modeTests], ["settings", settingsTests], ["notice", noticeTests],
                            ["identity", identityTests], ["window", windowTests], ["settings edges", settingsEdgeTests],
                            ["notice edges", noticeEdgeTests], ["messages", miscMessageTests], ["exec", execTests],
                            ["ChatCore", coreTests], ["slash commands", slashTests]]) {
    try { await fn(); } catch (e) { failed++; console.log(` FAIL  ${name} threw: ${e.stack}`); }
  }
  console.log(failed ? `\n${failed} failed` : "\nall passed");
  process.exit(failed ? 1 : 0);
})();
