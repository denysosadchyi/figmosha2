// First-run onboarding (level 1), the code.js side. Written before the code (TDD).
//
//     node tests/onboard_code.test.js
//
// MESSAGE CONTRACT (plugin/code.js <-> plugin/ui.html)
//
//   Flag: clientStorage key "figmosha-onboarded". It is per USER (shared by
//   all Figma files), not per file. Any truthy value means done. Level 2,
//   the per-file welcome screen (pluginData "figmosha-welcome"), stays as it is.
//
//   code.js -> UI, once on every run, after the "settings" message:
//     { type: "onboarding", show: true }    the user never finished it
//     { type: "onboarding", show: false }   done, or clientStorage can not be read
//   The "settings" message keeps its exact shape { type, scale, theme, lang }.
//   The "welcome" message is still sent once on every run, but while the
//   onboarding shows it is { type: "welcome", show: false }: no two intros in
//   a row.
//
//   While onboarding show is true, code.js opens the window at the chat size
//   (UI_MODES.chat times the stored scale) and posts { type: "ui-mode",
//   mode: "chat" }, before figma.ui.show(), so the thin bar never flashes.
//   The chat size is not stored as the user's mode.
//
//   During the onboarding the UI saves language, theme and scale with the
//   existing { type: "settings", scale, theme, lang } message. code.js
//   resizes the chat window at once and stores the settings, as today.
//
//   UI -> code.js, when the user finishes (Почати, Skip link or Esc):
//     { type: "onboarding-done" }
//   code.js stores a truthy "figmosha-onboarded" in clientStorage AND sets
//   pluginData "figmosha-welcome" to "1" in the current file. The window
//   stays at the chat size. Errors from clientStorage, getPluginData or
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

const ONBOARD_KEY = "figmosha-onboarded";
const WELCOME_KEY = "figmosha-welcome";
const CHAT = [380, 560];
const BAR = [220, 28];

function makeFile(data = {}, pdOff = null) {
  return { data: Object.assign({}, data), pdOff };
}

// opts.storageOff: "read" | "write" | "both" (true = both) makes clientStorage throw.
function loadPlugin(file, client = {}, opts = {}) {
  const off = opts.storageOff === true ? "both" : opts.storageOff || null;
  const events = [];
  const calls = { showUI: [], resize: [], posted: [], show: 0, pdSet: [], csSet: [] };
  const figma = {
    showUI(html, o) { calls.showUI.push(o); events.push(["showUI"]); },
    ui: {
      onmessage: null,
      postMessage(m) { calls.posted.push(m); events.push(["post", m]); },
      resize(w, h) { calls.resize.push([w, h]); events.push(["resize", [w, h]]); },
      show() { calls.show++; events.push(["show"]); },
    },
    clientStorage: {
      async getAsync(k) {
        if (off === "read" || off === "both") throw new Error("storage read off");
        return client[k];
      },
      async setAsync(k, v) {
        if (off === "write" || off === "both") throw new Error("storage write off");
        client[k] = v;
        calls.csSet.push([k, v]);
      },
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

const msgsOf = (p, type) => p.calls.posted.filter((m) => m && m.type === type);
const onboardMsgs = (p) => msgsOf(p, "onboarding");
const welcomeMsgs = (p) => msgsOf(p, "welcome");
const lastResize = (p) => p.calls.resize[p.calls.resize.length - 1] || null;
const postIndex = (p, type) => p.events.findIndex((e) => e[0] === "post" && e[1] && e[1].type === type);
async function send(p, msg) {
  try { await p.figma.ui.onmessage(msg); return null; } catch (e) { return e; }
}

async function firstRunTests() {
  const file = makeFile();
  const client = {};
  let p = loadPlugin(file, client);
  await tick();
  check("first run: no start error", p.startError && String(p.startError), null);
  check("first run: one onboarding message with show true", onboardMsgs(p),
        [{ type: "onboarding", show: true }]);
  const iSettings = postIndex(p, "settings"), iOnboard = postIndex(p, "onboarding");
  truthy("first run: onboarding is sent after settings", iSettings >= 0 && iOnboard > iSettings);
  truthy("first run: onboarding is sent before welcome", iOnboard >= 0 && postIndex(p, "welcome") > iOnboard);
  check("first run: settings message keeps its shape", msgsOf(p, "settings"),
        [{ type: "settings", scale: 1, theme: "system", lang: "uk" }]);
  check("first run: welcome is sent once with show false", welcomeMsgs(p),
        [{ type: "welcome", show: false }]);
  check("first run: window opens at the chat size", lastResize(p), CHAT);
  truthy("first run: UI is told it is in chat mode",
         p.calls.posted.some((m) => m.type === "ui-mode" && m.mode === "chat"));
  const iResize = p.events.findIndex((e) => e[0] === "resize" && e[1][0] === CHAT[0]);
  const iShow = p.events.findIndex((e) => e[0] === "show");
  truthy("first run: chat size is set before the window is shown", iResize >= 0 && iShow > iResize);
  check("first run: window is shown once", p.calls.show, 1);
  check("first run: onboarded flag is not stored before done", client[ONBOARD_KEY], undefined);
  check("first run: chat mode is not stored as the user mode", client["figmosha-ui-mode"], undefined);
  check("first run: file welcome flag is not set before done", file.data[WELCOME_KEY] || "", "");
  const docId = file.data["figmosha-doc"];

  // The user picks English, dark theme and 125 percent inside the onboarding.
  let err = await send(p, { type: "settings", scale: 1.25, theme: "dark", lang: "en" });
  await tick();
  check("onboarding settings: no error", err && String(err), null);
  check("onboarding settings: chat resizes to the new scale", lastResize(p), [475, 700]);
  check("onboarding settings: settings are stored", client["figmosha-settings"],
        { scale: 1.25, theme: "dark", lang: "en" });

  // Finish.
  err = await send(p, { type: "onboarding-done" });
  await tick();
  check("onboarding-done: no error", err && String(err), null);
  truthy("onboarding-done: flag is stored in clientStorage", client[ONBOARD_KEY]);
  check("onboarding-done: file welcome is marked seen", file.data[WELCOME_KEY], "1");
  check("onboarding-done: the doc id is kept", file.data["figmosha-doc"], docId);
  check("onboarding-done: window stays at the chat size", lastResize(p), [475, 700]);
  check("onboarding-done: onboarded flag is not in the file",
        Object.keys(file.data).filter((k) => /onboard/.test(k)), []);

  // Second run in the same file: no onboarding, no welcome, bar again.
  p = loadPlugin(file, client);
  await tick();
  check("second run: onboarding show false", onboardMsgs(p), [{ type: "onboarding", show: false }]);
  check("second run: welcome show false", welcomeMsgs(p), [{ type: "welcome", show: false }]);
  check("second run: bar at the stored scale", lastResize(p), [275, 35]);
  check("second run: settings come back", msgsOf(p, "settings"),
        [{ type: "settings", scale: 1.25, theme: "dark", lang: "en" }]);

  // A new file after onboarding: no onboarding, the per-file welcome shows.
  const other = makeFile();
  p = loadPlugin(other, client);
  await tick();
  check("new file: onboarding show false", onboardMsgs(p), [{ type: "onboarding", show: false }]);
  check("new file: welcome show true", welcomeMsgs(p), [{ type: "welcome", show: true }]);
  check("new file: chat size for the welcome", lastResize(p), [475, 700]);
  await send(p, { type: "welcome-done" });
  check("new file: welcome-done still sets the file flag", other.data[WELCOME_KEY], "1");
  check("new file: welcome-done does not touch the onboarded flag", !!client[ONBOARD_KEY], true);
}

async function variantTests() {
  // Any truthy stored value counts as done.
  for (const v of [true, 1, "1", "yes", { at: 1 }]) {
    const p = loadPlugin(makeFile(), { [ONBOARD_KEY]: v });
    await tick();
    check(`stored ${JSON.stringify(v)}: onboarding show false`, onboardMsgs(p),
          [{ type: "onboarding", show: false }]);
  }
  // Falsy stored values do not count.
  for (const v of [false, 0, "", null]) {
    const p = loadPlugin(makeFile(), { [ONBOARD_KEY]: v });
    await tick();
    check(`stored ${JSON.stringify(v)}: onboarding show true`, onboardMsgs(p),
          [{ type: "onboarding", show: true }]);
  }

  // Not per file: a file that already saw the welcome still gets the
  // onboarding for a new user.
  let f = makeFile({ [WELCOME_KEY]: "1" });
  let p = loadPlugin(f, {});
  await tick();
  check("seen file, new user: onboarding show true", onboardMsgs(p), [{ type: "onboarding", show: true }]);
  check("seen file, new user: welcome show false", welcomeMsgs(p), [{ type: "welcome", show: false }]);
  check("seen file, new user: chat size", lastResize(p), CHAT);

  // Stored scale applies to the onboarding window.
  p = loadPlugin(makeFile(), { "figmosha-settings": { scale: 1.25, theme: "dark" } });
  await tick();
  check("scale 1.25: onboarding window is the scaled chat", lastResize(p), [475, 700]);
  check("scale 1.25: onboarding show true", onboardMsgs(p), [{ type: "onboarding", show: true }]);

  // Stored chat mode: still one chat, one onboarding.
  p = loadPlugin(makeFile(), { "figmosha-ui-mode": "chat" });
  await tick();
  check("stored chat mode: one onboarding true", onboardMsgs(p), [{ type: "onboarding", show: true }]);
  check("stored chat mode: chat size", lastResize(p), CHAT);
  check("stored chat mode: window shown once", p.calls.show, 1);

  // onboarding-done twice is fine.
  f = makeFile();
  const client = {};
  p = loadPlugin(f, client);
  await tick();
  await send(p, { type: "onboarding-done" });
  const e2 = await send(p, { type: "onboarding-done" });
  check("onboarding-done twice: no error", e2 && String(e2), null);
  truthy("onboarding-done twice: flag still set", client[ONBOARD_KEY]);
  check("onboarding-done twice: file flag still 1", f.data[WELCOME_KEY], "1");

  // The bar toggle still works after the onboarding.
  await send(p, { type: "ui-mode", mode: "bar" });
  await tick();
  check("after onboarding the bar toggle works", lastResize(p), BAR);
  // And the chat edge resize still works.
  await send(p, { type: "ui-mode", mode: "chat" });
  await send(p, { type: "resize", w: 500, h: 600, done: true });
  await tick();
  check("after onboarding the edge resize works", lastResize(p), [500, 600]);
}

async function errorTests() {
  // clientStorage read throws: start does not crash, the window is shown,
  // onboarding show false (like the welcome when its flag can not be read).
  let f = makeFile();
  let p = loadPlugin(f, {}, { storageOff: "read" });
  await tick();
  check("read off: no start error", p.startError && String(p.startError), null);
  check("read off: window is shown", p.calls.show, 1);
  check("read off: onboarding show false", onboardMsgs(p), [{ type: "onboarding", show: false }]);
  check("read off: one welcome message", welcomeMsgs(p).length, 1);
  truthy("read off: identity is still sent", p.calls.posted.some((m) => m.type === "identity"));

  // clientStorage write throws: shown, done does not crash, the file is still marked.
  f = makeFile();
  p = loadPlugin(f, {}, { storageOff: "write" });
  await tick();
  check("write off: no start error", p.startError && String(p.startError), null);
  check("write off: onboarding show true", onboardMsgs(p), [{ type: "onboarding", show: true }]);
  let e = await send(p, { type: "onboarding-done" });
  check("write off: onboarding-done does not throw", e && String(e), null);
  check("write off: file welcome is still marked seen", f.data[WELCOME_KEY], "1");
  check("write off: window stays at the chat size", lastResize(p), CHAT);
  e = await send(p, { type: "ui-mode", mode: "bar" });
  check("write off: plugin keeps working", [e && String(e), lastResize(p)], [null, BAR]);

  // pluginData write throws: done still stores the user flag.
  f = makeFile({}, "write");
  let client = {};
  p = loadPlugin(f, client);
  await tick();
  check("pd write off: onboarding show true", onboardMsgs(p), [{ type: "onboarding", show: true }]);
  e = await send(p, { type: "onboarding-done" });
  await tick();
  check("pd write off: onboarding-done does not throw", e && String(e), null);
  truthy("pd write off: user flag still stored", client[ONBOARD_KEY]);

  // pluginData read throws: onboarding still decided by clientStorage.
  f = makeFile({}, "read");
  p = loadPlugin(f, {});
  await tick();
  check("pd read off: no start error", p.startError && String(p.startError), null);
  check("pd read off: onboarding show true", onboardMsgs(p), [{ type: "onboarding", show: true }]);
  check("pd read off: welcome show false", welcomeMsgs(p), [{ type: "welcome", show: false }]);

  // Everything off.
  f = makeFile({}, "both");
  p = loadPlugin(f, {}, { storageOff: true });
  await tick();
  check("all off: no start error", p.startError && String(p.startError), null);
  check("all off: window is shown", p.calls.show, 1);
  check("all off: one onboarding message", onboardMsgs(p).length, 1);
  e = await send(p, { type: "onboarding-done" });
  check("all off: onboarding-done does not throw", e && String(e), null);
}

(async () => {
  await firstRunTests();
  await variantTests();
  await errorTests();
  console.log(failed ? `\n${failed} FAILED` : "\nall ok");
  process.exit(failed ? 1 : 0);
})();
