// Chat window resize, the plugin/code.js side. Written before the code (TDD).
//
//     node tests/resize_code.test.js
//
// Message contract (UI -> code.js):
//   {type: "resize", w, h}              while the user drags an edge or a corner
//                                       (rAF throttled in the UI)
//   {type: "resize", w, h, done: true}  once, when the drag ends
//   w and h are CSS px at scale 1 (the UI divides by its scale).
//
// code.js:
//   - acts only in chat mode (the settings page lives in chat mode too).
//     In bar mode a resize is ignored: no figma.ui.resize, nothing stored.
//   - w and h must both be finite numbers (typeof "number", not NaN, not
//     Infinity). Anything else (strings, even "500", null, missing) is ignored.
//   - clamps the base size to min 320 x 400, max 900 x 1400 (scale 1).
//   - calls figma.ui.resize(Math.round(w * scale), Math.round(h * scale)).
//   - on done: true stores the clamped base size {w, h} in
//     figma.clientStorage under "figmosha-chat-size". Plain drag steps
//     do not touch storage.
//   - on start in chat mode the stored size (clamped; a bad stored value
//     falls back to the default 380 x 560) replaces UI_MODES.chat.
//   - a scale change keeps the base size and multiplies it by the new scale;
//     the stored base size is not rewritten by a scale change.
//   - the bar stays 220 x 28 (times the scale) whatever the chat size is.
//   - clientStorage errors never break the resize.
//   - figma.ui.reposition is optional: the fake figma here has none, and
//     nothing may crash because of that.
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
const tick = () => new Promise((r) => setTimeout(r, 20));
const SIZE_KEY = "figmosha-chat-size";

// Same fake figma as tests/chat_ui.test.js. opts: { mode, settings, size, storageOff, setOff }
function loadPlugin(opts = {}) {
  const calls = { resize: [], posted: [], set: [], show: 0 };
  // A user who finished the first-run onboarding (tests/onboard_code.test.js).
  const store = { "figmosha-onboarded": 1 };
  if (opts.mode !== undefined) store["figmosha-ui-mode"] = opts.mode;
  if (opts.settings !== undefined) store["figmosha-settings"] = opts.settings;
  if (opts.size !== undefined) store[SIZE_KEY] = opts.size;
  const figma = {
    showUI() {},
    ui: {
      onmessage: null,
      postMessage(m) { calls.posted.push(m); },
      resize(w, h) { calls.resize.push([w, h]); },
      show() { calls.show++; },
    },
    clientStorage: {
      async getAsync(k) { if (opts.storageOff) throw new Error("off"); return store[k]; },
      async setAsync(k, v) {
        if (opts.storageOff || opts.setOff) throw new Error("off");
        store[k] = v; calls.set.push([k, v]);
      },
    },
    currentPage: { selection: [] },
    openExternal() {},
    closePlugin() {},
    root: { name: "Banner", children: [{ id: "0:1" }],
            getPluginData() { return "dTest"; }, setPluginData() {} },
    variables: {},
  };
  const src = fs.readFileSync(path.join(ROOT, "plugin", "code.js"), "utf8");
  new Function("figma", "__html__", src + "\nreturn HELPERS;")(figma, "");
  const last = () => calls.resize[calls.resize.length - 1];
  const send = (m) => figma.ui.onmessage(m);
  return { figma, calls, store, last, send };
}

async function chat(opts) {
  const p = loadPlugin(Object.assign({ mode: "chat" }, opts));
  await tick();
  return p;
}

async function resizeTests() {
  // A drag step resizes the window at scale 1.
  let p = await chat();
  check("chat starts at the default size", p.last(), [380, 560]);
  await p.send({ type: "resize", w: 500, h: 700 });
  check("resize message resizes the window", p.last(), [500, 700]);
  check("a drag step does not touch storage", p.calls.set.filter((c) => c[0] === SIZE_KEY), []);

  // The scale multiplies the base size.
  p = await chat({ settings: { scale: 1.25, theme: "system" } });
  await p.send({ type: "resize", w: 500, h: 700 });
  check("scale 1.25 multiplies w and h", p.last(), [625, 875]);
  p = await chat({ settings: { scale: 0.9, theme: "system" } });
  await p.send({ type: "resize", w: 501, h: 701 });
  check("scale 0.9 rounds to whole px", p.last(), [451, 631]);

  // Clamping at scale 1, and at another scale.
  p = await chat();
  await p.send({ type: "resize", w: 100, h: 50 });
  check("too small clamps to the min 320x400", p.last(), [320, 400]);
  await p.send({ type: "resize", w: 5000, h: 9000 });
  check("too big clamps to the max 900x1400", p.last(), [900, 1400]);
  await p.send({ type: "resize", w: 100, h: 9000 });
  check("w and h clamp on their own", p.last(), [320, 1400]);
  await p.send({ type: "resize", w: 320, h: 1400 });
  check("the limits themselves are allowed", p.last(), [320, 1400]);
  p = await chat({ settings: { scale: 1.25, theme: "system" } });
  await p.send({ type: "resize", w: 5000, h: 10 });
  check("clamping is on the base size, before the scale", p.last(), [1125, 500]);

  // Drag end stores the clamped base size.
  p = await chat({ settings: { scale: 1.25, theme: "system" } });
  await p.send({ type: "resize", w: 600, h: 800, done: true });
  await tick();
  check("done resizes too", p.last(), [750, 1000]);
  check("done stores the base size", p.store[SIZE_KEY], { w: 600, h: 800 });
  await p.send({ type: "resize", w: 2000, h: 100, done: true });
  await tick();
  check("done stores the clamped size", p.store[SIZE_KEY], { w: 900, h: 400 });
  check("resize does not change the stored mode", p.store["figmosha-ui-mode"], "chat");
}

async function restoreTests() {
  // Next run in chat mode uses the stored size.
  let p = await chat({ size: { w: 600, h: 800 } });
  check("next run uses the stored size", p.last(), [600, 800]);
  p = await chat({ size: { w: 600, h: 800 }, settings: { scale: 1.1, theme: "system" } });
  check("stored size times the stored scale", p.last(), [660, 880]);
  p = await chat({ size: { w: 10, h: 99999 } });
  check("stored size out of range is clamped", p.last(), [320, 1400]);
  for (const bad of ["600x800", { w: "600", h: 800 }, { w: NaN, h: 800 }, null, 42, [600, 800], {}]) {
    p = await chat({ size: bad });
    check("bad stored size " + JSON.stringify(bad) + " gives the default", p.last(), [380, 560]);
  }

  // Bar on start: no resize. The chat icon then opens the stored size.
  p = loadPlugin({ mode: "bar", size: { w: 600, h: 800 } });
  await tick();
  check("bar start ignores the stored chat size", p.calls.resize, []);
  await p.send({ type: "ui-mode", mode: "chat" });
  check("chat icon opens the stored size", p.last(), [600, 800]);
  p = loadPlugin({ size: { w: 600, h: 800 } });
  await tick();
  await p.send({ type: "ui-mode", mode: "chat" });
  check("fresh mode, stored size: chat icon opens the stored size", p.last(), [600, 800]);

  // Scale change keeps the base size.
  p = await chat();
  await p.send({ type: "resize", w: 500, h: 700, done: true });
  await p.send({ type: "settings", scale: 1.25, theme: "system" });
  await tick();
  check("scale change keeps the base size", p.last(), [625, 875]);
  check("scale change does not rewrite the stored size", p.store[SIZE_KEY], { w: 500, h: 700 });
  await p.send({ type: "settings", scale: 0.9, theme: "system" });
  check("scale down keeps the base size too", p.last(), [450, 630]);
  p = await chat({ size: { w: 600, h: 800 }, settings: { scale: 1.1, theme: "system" } });
  await p.send({ type: "settings", scale: 1, theme: "system" });
  check("stored size survives a scale change after start", p.last(), [600, 800]);

  // A drag without done still holds for this run.
  p = await chat();
  await p.send({ type: "resize", w: 450, h: 650 });
  await p.send({ type: "ui-mode", mode: "bar" });
  await p.send({ type: "ui-mode", mode: "chat" });
  check("bar then chat keeps the new size", p.last(), [450, 650]);
}

async function barTests() {
  let p = loadPlugin();
  await tick();
  await p.send({ type: "resize", w: 500, h: 700 });
  await p.send({ type: "resize", w: 500, h: 700, done: true });
  await tick();
  check("bar mode ignores resize", p.calls.resize, []);
  check("bar mode stores no chat size", p.store[SIZE_KEY], undefined);

  p = await chat();
  await p.send({ type: "resize", w: 600, h: 800, done: true });
  await p.send({ type: "ui-mode", mode: "bar" });
  check("the bar stays 220x28", p.last(), [220, 28]);
  const n = p.calls.resize.length;
  await p.send({ type: "resize", w: 700, h: 900 });
  check("resize after switching to the bar is ignored", p.calls.resize.length, n);
  await p.send({ type: "settings", scale: 1.25, theme: "system" });
  check("the bar follows the scale, not the chat size", p.last(), [275, 35]);
  await p.send({ type: "ui-mode", mode: "chat" });
  check("back to chat: the dragged size, scaled", p.last(), [750, 1000]);
}

async function badPayloadTests() {
  const bad = [
    { type: "resize" },
    { type: "resize", w: 500 },
    { type: "resize", h: 700 },
    { type: "resize", w: NaN, h: 700 },
    { type: "resize", w: 500, h: NaN },
    { type: "resize", w: "500", h: "700" },
    { type: "resize", w: "wide", h: 700 },
    { type: "resize", w: Infinity, h: 700 },
    { type: "resize", w: 500, h: -Infinity },
    { type: "resize", w: null, h: null },
    { type: "resize", w: {}, h: [] },
    { type: "resize", w: true, h: 700 },
    { type: "resize", w: "500", h: 700, done: true },
    { type: "resize", w: NaN, h: NaN, done: true },
  ];
  const p = await chat();
  const n = p.calls.resize.length;
  for (const m of bad) {
    let threw = null;
    try { await p.send(m); } catch (e) { threw = e.message; }
    if (threw) check("bad payload does not throw: " + JSON.stringify(m), threw, null);
  }
  await tick();
  check("bad payloads do not resize", p.calls.resize.length, n);
  check("bad payloads store nothing", p.store[SIZE_KEY], undefined);
  await p.send({ type: "resize", w: 500, h: 700 });
  check("a good payload after bad ones still works", p.last(), [500, 700]);
}

async function storageTests() {
  // clientStorage off: the plugin starts, the chat resizes, done does not throw.
  let p = loadPlugin({ storageOff: true });
  await tick();
  await p.send({ type: "ui-mode", mode: "chat" });
  let threw = null;
  try {
    await p.send({ type: "resize", w: 500, h: 700 });
    await p.send({ type: "resize", w: 600, h: 800, done: true });
    await tick();
  } catch (e) { threw = e.message; }
  check("storage off: resize does not throw", threw, null);
  check("storage off: the window still resizes", p.last(), [600, 800]);

  // Only setAsync fails: the size still applies for this run.
  p = await chat({ setOff: true });
  threw = null;
  try { await p.send({ type: "resize", w: 640, h: 820, done: true }); await tick(); }
  catch (e) { threw = e.message; }
  check("setAsync throws: done does not throw", threw, null);
  check("setAsync throws: the window still resizes", p.last(), [640, 820]);

  // Unhandled rejections from a storage error count as a crash.
  let unhandled = 0;
  const onRej = () => { unhandled++; };
  process.on("unhandledRejection", onRej);
  p = await chat({ setOff: true });
  p.send({ type: "resize", w: 500, h: 700, done: true });
  await tick();
  process.off("unhandledRejection", onRej);
  check("setAsync throws: no unhandled rejection", unhandled, 0);
}

(async () => {
  try { await resizeTests(); } catch (e) { failed++; console.log(" FAIL  resize threw: " + e.message); }
  try { await restoreTests(); } catch (e) { failed++; console.log(" FAIL  restore threw: " + e.message); }
  try { await barTests(); } catch (e) { failed++; console.log(" FAIL  bar threw: " + e.message); }
  try { await badPayloadTests(); } catch (e) { failed++; console.log(" FAIL  bad payloads threw: " + e.message); }
  try { await storageTests(); } catch (e) { failed++; console.log(" FAIL  storage threw: " + e.message); }
  console.log(failed ? `\n${failed} failed` : "\nall passed");
  process.exit(failed ? 1 : 0);
})();
