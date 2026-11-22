// Welcome screen, the code.js side. Written before the code (TDD).
//
//     node tests/welcome_code.test.js
//
// MESSAGE CONTRACT (plugin/code.js <-> plugin/ui.html)
//
//   Flag: figma.root pluginData, key "figmosha-welcome". It lives in the
//   Figma FILE, not in clientStorage, so every file shows the screen once.
//   The file has seen the screen when the value is a non-empty string.
//   code.js writes "1" there on dismiss. (Same store as "figmosha-doc".)
//
//   code.js -> UI, once on every run, after it read the flag:
//     { type: "welcome", show: true }    this file never saw it
//     { type: "welcome", show: false }   seen, or the flag can not be read
//   This is a NEW message. The "settings" message keeps its exact old shape
//   { type, scale, theme }, so old UIs and old tests do not break.
//
//   While show is true, code.js opens the window at the chat size
//   (UI_MODES.chat times the stored scale) and posts { type: "ui-mode",
//   mode: "chat" }, before figma.ui.show(), so the thin bar never flashes.
//
//   UI -> code.js, when the user presses Почати (or Esc):
//     { type: "welcome-done" }
//   code.js sets the flag to "1". The window stays at the chat size (the
//   UI goes on to the normal chat view). Errors from getPluginData or
//   setPluginData are caught: the plugin starts and keeps working.
const fs = require("fs");
const path = require("path");
const ROOT = path.join(__dirname, "..");

let failed = 0;
function check(label, actual, expected) {
  const a = JSON.stringify(actual), e = JSON.stringify(expected);
  const ok = a === e;
  if (!ok) failed++;
  console.log(`${ok ? "  ok  " : " FAIL "} ${label}${ok ? "" : `\n         got ${a}\n         want ${e}`}`);
}
function truthy(label, v) { check(label, !!v, true); }
const tick = () => new Promise((r) => setTimeout(r, 20));

const WELCOME_KEY = "figmosha-welcome";
const CHAT = [380, 560];
const BAR = [220, 28];

// One fake Figma file: its own pluginData. `client` is clientStorage, shared
// by all files of one user. `pdOff`: "read" | "write" | "both" makes the
// pluginData calls throw.
function makeFile(data = {}, pdOff = null) {
  return {
    data: Object.assign({}, data),
    pdOff,
  };
}

function loadPlugin(file, client = {}, opts = {}) {
  // A user who finished the first-run onboarding (tests/onboard_code.test.js),
  // so this file tests the per-file welcome on its own.
  if (!Object.prototype.hasOwnProperty.call(client, "figmosha-onboarded")) client["figmosha-onboarded"] = 1;
  const events = [];
  const calls = { showUI: [], resize: [], posted: [], show: 0, pdSet: [] };
  const figma = {
    showUI(html, o) { calls.showUI.push(o); events.push(["showUI"]); },
    ui: {
      onmessage: null,
      postMessage(m) { calls.posted.push(m); events.push(["post", m]); },
      resize(w, h) { calls.resize.push([w, h]); events.push(["resize", [w, h]]); },
      show() { calls.show++; events.push(["show"]); },
    },
    clientStorage: {
      async getAsync(k) { if (opts.storageOff) throw new Error("off"); return client[k]; },
      async setAsync(k, v) { if (opts.storageOff) throw new Error("off"); client[k] = v; },
    },
    currentPage: { selection: [] },
    openExternal() {},
    closePlugin() {},
    root: {
      name: file.name || "Banner",
      children: [{ id: "0:1" }],
      getPluginData(k) {
        if (file.pdOff === "read" || file.pdOff === "both") throw new Error("pluginData read off");
        return Object.prototype.hasOwnProperty.call(file.data, k) ? file.data[k] : "";
      },
      setPluginData(k, v) {
        if (file.pdOff === "write" || file.pdOff === "both") throw new Error("pluginData write off");
        file.data[k] = String(v);
        calls.pdSet.push([k, String(v)]);
      },
    },
    variables: {},
  };
  const src = fs.readFileSync(path.join(ROOT, "plugin", "code.js"), "utf8");
  let startError = null;
  try {
    new Function("figma", "__html__", src + "\nreturn HELPERS;")(figma, "");
  } catch (e) { startError = e; }
  return { figma, calls, events, client, file, startError };
}

const welcomeMsgs = (p) => p.calls.posted.filter((m) => m && m.type === "welcome");
const lastResize = (p) => p.calls.resize[p.calls.resize.length - 1] || null;
async function send(p, msg) {
  try { await p.figma.ui.onmessage(msg); return null; } catch (e) { return e; }
}

async function firstRunTests() {
  // A file that never saw the screen.
  const file = makeFile();
  const client = {};
  let p = loadPlugin(file, client);
  await tick();
  check("first run: no start error", p.startError && String(p.startError), null);
  check("first run: one welcome message with show true", welcomeMsgs(p),
        [{ type: "welcome", show: true }]);
  check("first run: window opens at the chat size", lastResize(p), CHAT);
  truthy("first run: UI is told it is in chat mode",
         p.calls.posted.some((m) => m.type === "ui-mode" && m.mode === "chat"));
  const iResize = p.events.findIndex((e) => e[0] === "resize" && e[1][0] === CHAT[0]);
  const iShow = p.events.findIndex((e) => e[0] === "show");
  truthy("first run: chat size is set before the window is shown", iResize >= 0 && iShow > iResize);
  check("first run: window is shown once", p.calls.show, 1);
  check("first run: settings message keeps its old shape",
        p.calls.posted.filter((m) => m.type === "settings"),
        [{ type: "settings", scale: 1, theme: "system", lang: "uk" }]);
  check("first run: the flag is not set before dismiss", file.data[WELCOME_KEY] || "", "");
  truthy("first run: the doc id is still stored", /^d/.test(file.data["figmosha-doc"] || ""));
  const docId = file.data["figmosha-doc"];

  // Почати.
  const err = await send(p, { type: "welcome-done" });
  await tick();
  check("welcome-done: no error", err && String(err), null);
  check("welcome-done: flag is set to 1 in the file", file.data[WELCOME_KEY], "1");
  check("welcome-done: the doc id is kept", file.data["figmosha-doc"], docId);
  check("welcome-done: window stays at the chat size", lastResize(p), CHAT);
  check("welcome-done: flag is not in clientStorage",
        Object.keys(client).filter((k) => /welcome/.test(k)), []);

  // Next run in the same file: not shown again.
  p = loadPlugin(file, client);
  await tick();
  check("second run: welcome show false", welcomeMsgs(p), [{ type: "welcome", show: false }]);
  check("second run: bar again, no resize", p.calls.resize, []);
  check("second run: bar size at start", [p.calls.showUI[0].width, p.calls.showUI[0].height], BAR);

  // Another file, same user: shown again there.
  const other = makeFile();
  p = loadPlugin(other, client);
  await tick();
  check("other file: welcome show true", welcomeMsgs(p), [{ type: "welcome", show: true }]);
  check("other file: chat size", lastResize(p), CHAT);
  check("first file keeps its flag", file.data[WELCOME_KEY], "1");
}

async function variantTests() {
  // Stored scale applies to the welcome window too.
  let p = loadPlugin(makeFile(), { "figmosha-settings": { scale: 1.25, theme: "dark" } });
  await tick();
  check("scale 1.25: welcome window is the scaled chat", lastResize(p), [475, 700]);

  // Chat was open last time and the file is new: still the chat, one welcome.
  p = loadPlugin(makeFile(), { "figmosha-ui-mode": "chat" });
  await tick();
  check("stored chat mode: welcome show true", welcomeMsgs(p), [{ type: "welcome", show: true }]);
  check("stored chat mode: chat size", lastResize(p), CHAT);

  // Any non-empty old value counts as seen.
  p = loadPlugin(makeFile({ [WELCOME_KEY]: "yes" }));
  await tick();
  check("non-empty flag counts as seen", welcomeMsgs(p), [{ type: "welcome", show: false }]);

  // welcome-done twice is fine.
  const f = makeFile();
  p = loadPlugin(f);
  await tick();
  await send(p, { type: "welcome-done" });
  const e2 = await send(p, { type: "welcome-done" });
  check("welcome-done twice: no error", e2 && String(e2), null);
  check("welcome-done twice: flag still 1", f.data[WELCOME_KEY], "1");

  // The bar toggle still works after the welcome.
  await send(p, { type: "ui-mode", mode: "bar" });
  await tick();
  check("after welcome the bar toggle works", lastResize(p), BAR);
}

async function errorTests() {
  // pluginData read throws: start does not crash, the window is shown, show false.
  let f = makeFile({}, "read");
  let p = loadPlugin(f);
  await tick();
  check("read off: no start error", p.startError && String(p.startError), null);
  check("read off: window is shown", p.calls.show, 1);
  check("read off: welcome show false", welcomeMsgs(p), [{ type: "welcome", show: false }]);
  truthy("read off: identity is still sent", p.calls.posted.some((m) => m.type === "identity"));

  // pluginData write throws: shown (flag is empty), dismiss does not crash.
  f = makeFile({}, "write");
  p = loadPlugin(f);
  await tick();
  check("write off: no start error", p.startError && String(p.startError), null);
  check("write off: welcome show true", welcomeMsgs(p), [{ type: "welcome", show: true }]);
  const e = await send(p, { type: "welcome-done" });
  check("write off: welcome-done does not throw", e && String(e), null);
  check("write off: window stays at the chat size", lastResize(p), CHAT);
  const e3 = await send(p, { type: "ui-mode", mode: "bar" });
  check("write off: plugin keeps working", [e3 && String(e3), lastResize(p)], [null, BAR]);

  // Both off plus clientStorage off.
  f = makeFile({}, "both");
  p = loadPlugin(f, {}, { storageOff: true });
  await tick();
  check("all off: no start error", p.startError && String(p.startError), null);
  check("all off: window is shown", p.calls.show, 1);
  check("all off: one welcome message", welcomeMsgs(p).length, 1);
  const e4 = await send(p, { type: "welcome-done" });
  check("all off: welcome-done does not throw", e4 && String(e4), null);

  // clientStorage off only: the per-file flag still works.
  f = makeFile();
  p = loadPlugin(f, {}, { storageOff: true });
  await tick();
  check("storage off: welcome show true", welcomeMsgs(p), [{ type: "welcome", show: true }]);
  await send(p, { type: "welcome-done" });
  check("storage off: flag still set in the file", f.data[WELCOME_KEY], "1");
}

(async () => {
  await firstRunTests();
  await variantTests();
  await errorTests();
  console.log(failed ? `\n${failed} FAILED` : "\nall ok");
  process.exit(failed ? 1 : 0);
})();
