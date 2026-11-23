// Chat window position, the plugin/code.js side.
//
//     node tests/position_code.test.js
//
// Contract:
//   - The chat window position is kept per user in figma.clientStorage key
//     "figmosha-chat-pos" as WINDOW space {x, y} (px relative to the Figma
//     window, from figma.ui.getPosition().windowSpace). It means the same
//     place on screen whatever the canvas zoom or scroll.
//   - Only chat mode. The bar position is never restored or polled.
//   - Track: in chat mode code.js polls figma.ui.getPosition() about once a
//     second (setInterval or a setTimeout chain). When the window space
//     position differs from the last known one it stores it (debounced:
//     at most a few seconds later, and never again while it stays the same).
//     The first position seen (or the restored one) is the baseline: a window
//     that never moves stores nothing by polling.
//   - Also stores the current position on {type:"resize", done:true} and
//     right before a switch to the bar ({type:"ui-mode", mode:"bar"}).
//   - Restore: on start in chat mode, and when the chat icon switches the bar
//     to chat, a valid stored {x, y} (two finite numbers) is clamped to
//     x in [0, max(0, bounds.width*zoom - 80)], y in [0, max(0, bounds.height*zoom - 40)],
//     converted to canvas space cx = bounds.x + x / zoom, cy = bounds.y + y / zoom,
//     and passed to figma.ui.reposition(cx, cy) AFTER figma.ui.resize.
//     A bad stored value is ignored: no reposition.
//   - Old Figma: getPosition, reposition, viewport or figma.on may be missing,
//     or getPosition may throw. Nothing may throw or reject because of that.
//   - clientStorage errors never break anything (no unhandled rejection).
//   - figma.on("close") clears the poll timer: no getPosition after close.
//   - The welcome screen of a new file opens the chat, so it restores too.
//   - A resize drag in progress: the poll never resizes or repositions.
//
// Timers: setInterval/clearInterval/setTimeout/clearTimeout are passed to
// code.js as a fake clock this test advances by hand.
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
function checkTrue(label, cond, info) {
  if (!cond) failed++;
  console.log(`${cond ? "  ok  " : " FAIL "} ${label}${cond ? "" : `\n         ${info || ""}`}`);
}
const realTimeout = setTimeout;
const tick = () => new Promise((r) => realTimeout(r, 15));
const POS_KEY = "figmosha-chat-pos";

// A fake clock. advance(ms) runs due timers in time order, letting promises settle.
function makeClock() {
  let now = 0, seq = 1;
  const timers = new Map();
  const add = (fn, ms, repeat) => {
    const id = seq++;
    const d = Math.max(0, Number(ms) || 0);
    timers.set(id, { fn, at: now + d, every: repeat ? Math.max(1, d) : 0 });
    return id;
  };
  const clear = (id) => { timers.delete(id); };
  async function advance(ms) {
    const end = now + ms;
    for (;;) {
      let next = null;
      for (const [id, t] of timers) if (t.at <= end && (!next || t.at < next[1].at)) next = [id, t];
      if (!next) break;
      const [id, t] = next;
      now = t.at;
      if (t.every) t.at = now + t.every; else timers.delete(id);
      try { t.fn(); } catch (e) { /* counted by the caller */ }
      await tick();
    }
    now = end;
    await tick();
  }
  return {
    setInterval: (fn, ms) => add(fn, ms, true),
    clearInterval: clear,
    setTimeout: (fn, ms) => add(fn, ms, false),
    clearTimeout: clear,
    advance,
    repeating: () => [...timers.values()].filter((t) => t.every).length,
  };
}

// opts: { mode, pos, size, settings, bounds, zoom, win, noGetPos, noRepos, noViewport,
//         noOn, getPosThrows, storageOff, setOff, resizeMoves }
function loadPlugin(opts = {}) {
  const calls = { ops: [], resize: [], repos: [], set: [], getPos: 0, on: {} };
  // The first-launch onboarding is done, so a start follows the stored mode.
  const store = opts.store || {};
  if (!("figmosha-onboarded" in store)) store["figmosha-onboarded"] = 1;
  if (opts.mode !== undefined) store["figmosha-ui-mode"] = opts.mode;
  if (opts.settings !== undefined) store["figmosha-settings"] = opts.settings;
  if (opts.size !== undefined) store["figmosha-chat-size"] = opts.size;
  if (opts.pos !== undefined) store[POS_KEY] = opts.pos;
  const viewport = {
    bounds: Object.assign({ x: 0, y: 0, width: 1600, height: 900 }, opts.bounds || {}),
    zoom: opts.zoom || 1,
  };
  // The window position in window space. reposition(cx, cy) moves it there.
  const win = Object.assign({ x: 40, y: 60 }, opts.win || {});
  // Where the canvas viewport starts in window space (toolbar, left panel).
  const org = Object.assign({ x: 0, y: 0 }, opts.origin || {});
  const ui = {
    onmessage: null,
    postMessage() {},
    resize(w, h) {
      calls.resize.push([w, h]); calls.ops.push("resize");
      // opts.resizeMoves: this fake Figma moves the window on a resize, so a
      // position read after the resize is not the chat position any more.
      if (opts.resizeMoves) { win.x += 1000; win.y += 1000; }
    },
    show() {},
  };
  if (!opts.noGetPos) {
    ui.getPosition = function () {
      calls.getPos++;
      if (opts.getPosThrows) throw new Error("no position");
      const b = viewport.bounds, z = viewport.zoom;
      return { windowSpace: { x: win.x, y: win.y },
               canvasSpace: { x: b.x + (win.x - org.x) / z, y: b.y + (win.y - org.y) / z } };
    };
  }
  if (!opts.noRepos) {
    ui.reposition = function (cx, cy) {
      calls.repos.push([cx, cy]); calls.ops.push("reposition");
      const b = viewport.bounds, z = viewport.zoom;
      win.x = (cx - b.x) * z + org.x; win.y = (cy - b.y) * z + org.y;
    };
  }
  const figma = {
    showUI() {},
    ui,
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
            getPluginData(k) { return k === "figmosha-welcome" ? (opts.welcome ? "" : "1") : "dTest"; },
            setPluginData() {} },
    variables: {},
  };
  if (!opts.noViewport) figma.viewport = viewport;
  if (!opts.noOn) {
    figma.on = function (ev, fn) { (calls.on[ev] = calls.on[ev] || []).push(fn); };
  }
  const clock = makeClock();
  const src = fs.readFileSync(path.join(ROOT, "plugin", "code.js"), "utf8");
  new Function("figma", "__html__", "setInterval", "clearInterval", "setTimeout", "clearTimeout",
    src + "\nreturn HELPERS;")(figma, "", clock.setInterval, clock.clearInterval,
    clock.setTimeout, clock.clearTimeout);
  const send = (m) => figma.ui.onmessage(m);
  const posSets = () => calls.set.filter((c) => c[0] === POS_KEY).map((c) => c[1]);
  const close = () => { for (const fn of calls.on.close || []) fn(); };
  return { figma, calls, store, win, viewport, clock, send, posSets, close };
}

async function chat(opts) {
  const p = loadPlugin(Object.assign({ mode: "chat" }, opts));
  await tick();
  return p;
}

async function trackTests() {
  let p = await chat();
  await p.clock.advance(5000);
  check("a window that never moves stores nothing", p.posSets(), []);
  checkTrue("chat mode polls getPosition about once a second",
    p.calls.getPos >= 3 && p.calls.getPos <= 20, "getPosition calls in 5 s: " + p.calls.getPos);

  p.win.x = 300; p.win.y = 200;
  await p.clock.advance(4000);
  check("a moved window stores its window space position", p.store[POS_KEY], { x: 300, y: 200 });
  const n = p.posSets().length;
  checkTrue("one move stores once or a few times", n >= 1 && n <= 2, "stores: " + n);
  await p.clock.advance(10000);
  check("no more stores while it stays put", p.posSets().length, n);

  p.win.x = 500; p.win.y = 120;
  await p.clock.advance(4000);
  check("a second move stores the new position", p.store[POS_KEY], { x: 500, y: 120 });

  // Moving every second for a while, then stopping: the last one wins.
  p = await chat();
  for (let i = 1; i <= 6; i++) { p.win.x = 100 + i * 10; p.win.y = 100; await p.clock.advance(1000); }
  await p.clock.advance(4000);
  check("after a drag the last position is stored", p.store[POS_KEY], { x: 160, y: 100 });

  // Zoom and scroll do not change what is stored: it is window space.
  p = await chat({ zoom: 2, bounds: { x: -500, y: 250, width: 800, height: 450 } });
  p.win.x = 222; p.win.y = 111;
  await p.clock.advance(4000);
  check("zoom 2 and scroll: still window space", p.store[POS_KEY], { x: 222, y: 111 });

  // Resize done stores the position at once, no polling needed.
  p = await chat();
  p.win.x = 410; p.win.y = 90;
  await p.send({ type: "resize", w: 500, h: 700, done: true });
  await tick();
  check("resize done stores the position", p.store[POS_KEY], { x: 410, y: 90 });

  // A drag step (no done) does not store the position by itself.
  p = await chat();
  p.win.x = 411; p.win.y = 91;
  await p.send({ type: "resize", w: 500, h: 700 });
  await tick();
  check("a resize drag step does not store the position", p.posSets(), []);

  // Switch to the bar stores the chat position, before the bar resize.
  p = await chat();
  p.win.x = 333; p.win.y = 222;
  await p.send({ type: "ui-mode", mode: "bar" });
  await tick();
  check("switch to the bar stores the chat position", p.store[POS_KEY], { x: 333, y: 222 });
  const m = p.posSets().length;
  p.win.x = 700; p.win.y = 10;
  await p.clock.advance(6000);
  check("bar mode: moving the bar stores nothing", p.posSets().length, m);
  check("bar mode: the stored chat position stays", p.store[POS_KEY], { x: 333, y: 222 });
}

async function barTests() {
  let p = loadPlugin({ mode: "bar", pos: { x: 100, y: 50 } });
  await tick();
  check("bar start: no reposition", p.calls.repos, []);
  p.win.x = 900; p.win.y = 30;
  await p.clock.advance(6000);
  check("bar start: moving the bar stores nothing", p.posSets(), []);
  check("bar start: the stored chat position is kept", p.store[POS_KEY], { x: 100, y: 50 });

  p = loadPlugin({ pos: { x: 100, y: 50 } });
  await tick();
  await p.clock.advance(3000);
  check("fresh start (bar): no reposition", p.calls.repos, []);

  p = await chat({ pos: { x: 100, y: 50 } });
  const r = p.calls.repos.length;
  await p.send({ type: "ui-mode", mode: "bar" });
  check("switch to the bar does not reposition", p.calls.repos.length, r);

  // Bar mode, the poll later starts in chat mode.
  p = loadPlugin({ mode: "bar" });
  await tick();
  await p.send({ type: "ui-mode", mode: "chat" });
  p.win.x = 250; p.win.y = 170;
  await p.clock.advance(4000);
  check("bar then chat: polling stores the moved chat", p.store[POS_KEY], { x: 250, y: 170 });
}

async function restoreTests() {
  // Zoom 1, no scroll.
  let p = await chat({ pos: { x: 100, y: 50 } });
  check("start in chat: reposition to the stored place (zoom 1)", p.calls.repos, [[100, 50]]);
  check("reposition comes after the resize", p.calls.ops.lastIndexOf("reposition") > p.calls.ops.indexOf("resize"), true);

  // Zoom 1, scrolled canvas.
  p = await chat({ pos: { x: 100, y: 50 }, bounds: { x: -200, y: 300, width: 1600, height: 900 } });
  check("zoom 1, scrolled: canvas = bounds + window", p.calls.repos, [[-100, 350]]);

  // Zoom 2, scrolled: viewport is 1600x900 px, bounds are half that in canvas units.
  p = await chat({ pos: { x: 100, y: 50 }, zoom: 2, bounds: { x: -200, y: 300, width: 800, height: 450 } });
  check("zoom 2, scrolled: canvas = bounds + window / zoom", p.calls.repos, [[-150, 325]]);

  // Zoom 0.5.
  p = await chat({ pos: { x: 100, y: 50 }, zoom: 0.5, bounds: { x: 1000, y: -400, width: 3200, height: 1800 } });
  check("zoom 0.5: canvas = bounds + window * 2", p.calls.repos, [[1200, -300]]);

  // The viewport starts at (240, 48) in window space: the restore lands on
  // the stored screen spot, not shifted by the panel, and stores nothing.
  p = await chat({ pos: { x: 400, y: 300 }, origin: { x: 240, y: 48 }, zoom: 2,
                   bounds: { x: 10, y: 20, width: 800, height: 450 } });
  check("panel offset: canvas = bounds + (window - offset) / zoom", p.calls.repos, [[90, 146]]);
  check("panel offset: window lands on the stored spot", [p.win.x, p.win.y], [400, 300]);
  await p.clock.advance(6000);
  check("panel offset: no drift, nothing stored", p.posSets(), []);
  p = await chat({ pos: { x: 5000, y: 5000 }, origin: { x: 240, y: 48 }, zoom: 2,
                   bounds: { x: 10, y: 20, width: 800, height: 450 } });
  check("panel offset: clamp to the viewport right and bottom edges", p.calls.repos, [[770, 450]]);

  // No stored position: no reposition.
  p = await chat();
  check("no stored position: no reposition", p.calls.repos, []);

  // Restored position is the baseline: no store when it stays.
  p = await chat({ pos: { x: 100, y: 50 } });
  await p.clock.advance(6000);
  check("restored and unchanged: nothing stored", p.posSets(), []);

  // The chat icon in the bar restores the stored position.
  p = loadPlugin({ mode: "bar", pos: { x: 120, y: 80 }, zoom: 2, bounds: { x: 10, y: 20, width: 800, height: 450 } });
  await tick();
  check("bar start: still no reposition", p.calls.repos, []);
  await p.send({ type: "ui-mode", mode: "chat" });
  await tick();
  check("chat icon: reposition to the stored place", p.calls.repos, [[70, 60]]);
  check("chat icon: reposition after the resize", p.calls.ops[p.calls.ops.length - 1], "reposition");

  // Chat, moved, bar, chat again in one run: back where the chat was.
  p = await chat();
  p.win.x = 300; p.win.y = 200;
  await p.send({ type: "ui-mode", mode: "bar" });
  p.win.x = 5; p.win.y = 5;            // Figma puts the bar somewhere else
  p.viewport.zoom = 2;                  // and the user zooms the canvas
  p.viewport.bounds = { x: 100, y: 100, width: 800, height: 450 };
  await p.send({ type: "ui-mode", mode: "chat" });
  await tick();
  check("chat, bar, chat: same screen place at the new zoom", p.calls.repos[p.calls.repos.length - 1], [250, 200]);
}

async function clampTests() {
  // Viewport 1600x900 px at zoom 1: x max 1520, y max 860.
  let p = await chat({ pos: { x: 5000, y: 3000 } });
  check("far right and down clamps to viewport - 80 / - 40", p.calls.repos, [[1520, 860]]);
  p = await chat({ pos: { x: -300, y: -50 } });
  check("negative clamps to 0", p.calls.repos, [[0, 0]]);
  p = await chat({ pos: { x: 1520, y: 860 } });
  check("the limits themselves are allowed", p.calls.repos, [[1520, 860]]);
  p = await chat({ pos: { x: 5000, y: -10 }, zoom: 2, bounds: { x: -200, y: 300, width: 800, height: 450 } });
  check("zoom 2: clamped in px, then converted", p.calls.repos, [[-200 + 1520 / 2, 300]]);
  p = await chat({ pos: { x: 500, y: 500 }, bounds: { x: 0, y: 0, width: 60, height: 30 } });
  check("viewport smaller than the margin: 0, 0", p.calls.repos, [[0, 0]]);
}

async function badValueTests() {
  const bad = ["100,50", { x: "100", y: 50 }, { x: 100 }, { y: 50 }, { x: NaN, y: 50 },
               { x: 100, y: Infinity }, { x: null, y: null }, null, 42, true, [100, 50], {}];
  for (const v of bad) {
    let threw = null, p = null;
    try { p = await chat({ pos: v }); } catch (e) { threw = e.message; }
    check("bad stored position " + JSON.stringify(v) + ": no throw, no reposition, chat still resized",
      [threw, p && p.calls.repos, !!p && p.calls.resize.length > 0], [null, [], true]);
  }
}

async function oldFigmaTests() {
  let unhandled = 0;
  const onRej = () => { unhandled++; };
  process.on("unhandledRejection", onRej);
  const variants = [
    ["no getPosition", { noGetPos: true }],
    ["no reposition", { noRepos: true }],
    ["no getPosition and no reposition", { noGetPos: true, noRepos: true }],
    ["no viewport", { noViewport: true }],
    ["no figma.on", { noOn: true }],
    ["getPosition throws", { getPosThrows: true }],
  ];
  for (const [name, o] of variants) {
    let threw = null, p = null;
    try {
      p = await chat(Object.assign({ pos: { x: 100, y: 50 } }, o));
      p.win.x = 300;
      await p.clock.advance(4000);
      await p.send({ type: "resize", w: 500, h: 700, done: true });
      await p.send({ type: "ui-mode", mode: "bar" });
      await p.send({ type: "ui-mode", mode: "chat" });
      await p.clock.advance(3000);
      p.close();
    } catch (e) { threw = e.message; }
    check(name + ": nothing throws", threw, null);
    if (p) check(name + ": the chat still resizes", p.calls.resize[p.calls.resize.length - 1], [500, 700]);
  }
  // Without a viewport there is nothing to convert: no reposition.
  const p = await chat({ pos: { x: 100, y: 50 }, noViewport: true });
  check("no viewport: no reposition", p.calls.repos, []);
  // getPosition missing: no store by polling (nothing to read).
  const q = await chat({ noGetPos: true });
  await q.clock.advance(5000);
  check("no getPosition: nothing stored", q.posSets(), []);
  await tick();
  process.off("unhandledRejection", onRej);
  check("old Figma: no unhandled rejection", unhandled, 0);
}

async function storageTests() {
  let unhandled = 0;
  const onRej = () => { unhandled++; };
  process.on("unhandledRejection", onRej);
  let threw = null, p = null;
  try {
    p = loadPlugin({ storageOff: true });
    await tick();
    await p.send({ type: "ui-mode", mode: "chat" });
    p.win.x = 300;
    await p.clock.advance(4000);
    await p.send({ type: "resize", w: 500, h: 700, done: true });
    await p.send({ type: "ui-mode", mode: "bar" });
  } catch (e) { threw = e.message; }
  check("storage off: nothing throws", threw, null);
  check("storage off: no reposition", p ? p.calls.repos : null, []);

  threw = null;
  try {
    p = await chat({ setOff: true, pos: { x: 100, y: 50 } });
    p.win.x = 300; p.win.y = 300;
    await p.clock.advance(4000);
    p.win.x = 310;
    await p.clock.advance(4000);
    await p.send({ type: "resize", w: 500, h: 700, done: true });
    await p.send({ type: "ui-mode", mode: "bar" });
  } catch (e) { threw = e.message; }
  check("setAsync throws: nothing throws", threw, null);
  check("setAsync throws: the stored position still restores", p.calls.repos, [[100, 50]]);
  checkTrue("setAsync throws: polling keeps going", p.calls.getPos >= 4, "getPosition calls: " + p.calls.getPos);
  await tick();
  process.off("unhandledRejection", onRej);
  check("storage errors: no unhandled rejection", unhandled, 0);
}

async function closeTests() {
  let p = await chat();
  checkTrue("a close handler is registered", (p.calls.on.close || []).length >= 1,
    "figma.on handlers: " + JSON.stringify(Object.keys(p.calls.on)));
  await p.clock.advance(2000);
  let threw = null;
  try { p.close(); } catch (e) { threw = e.message; }
  check("close handler does not throw", threw, null);
  const n = p.calls.getPos;
  p.win.x = 777;
  await p.clock.advance(10000);
  check("after close: no more getPosition", p.calls.getPos, n);
  check("after close: no repeating timer left", p.clock.repeating(), 0);

  // Close in bar mode is fine too.
  p = loadPlugin({ mode: "bar" });
  await tick();
  threw = null;
  try { p.close(); } catch (e) { threw = e.message; }
  check("close in bar mode does not throw", threw, null);
}

// Added by the mutation skeptic: each check kills a code.js mutant that
// survived the tests above.
async function mutantTests() {
  // Same spot as stored: resize done and the switch to bar write nothing.
  let p = await chat({ pos: { x: 100, y: 50 } });
  await p.send({ type: "resize", w: 500, h: 700, done: true });
  await tick();
  check("resize done at the stored spot: no position write", p.posSets(), []);
  await p.send({ type: "ui-mode", mode: "bar" });
  await tick();
  check("switch to bar at the stored spot: no position write", p.posSets(), []);

  // A repeated ui-mode chat while already in chat does not move the window.
  p = await chat({ pos: { x: 100, y: 50 } });
  p.win.x = 600; p.win.y = 300;
  await p.send({ type: "ui-mode", mode: "chat" });
  await tick();
  check("repeated ui-mode chat: no second reposition", p.calls.repos, [[100, 50]]);
  check("repeated ui-mode chat: the window stays put", { x: p.win.x, y: p.win.y }, { x: 600, y: 300 });

  // Debounce: a window that moves on every tick is not stored on every tick.
  p = await chat();
  for (let i = 1; i <= 8; i++) { p.win.x = 100 + i * 10; p.win.y = 100; await p.clock.advance(1000); }
  checkTrue("a drag that moves every tick is not stored every tick",
    p.posSets().length <= 2, "stores during an 8 s drag: " + p.posSets().length);
  await p.clock.advance(4000);
  check("after the drag stops: the last spot is stored", p.store[POS_KEY], { x: 180, y: 100 });

  // The chat position is read BEFORE the bar resize.
  p = await chat({ resizeMoves: true });
  p.win.x = 333; p.win.y = 222;
  await p.send({ type: "ui-mode", mode: "bar" });
  await tick();
  check("switch to bar reads the position before the bar resize", p.store[POS_KEY], { x: 333, y: 222 });
}

async function welcomeTests() {
  // First open of a file: code.js opens the chat for the welcome, even with the bar stored.
  let p = await chat({ welcome: true, mode: "bar", pos: { x: 320, y: 140 } });
  check("welcome first open restores the stored spot", [p.win.x, p.win.y], [320, 140]);
  p = await chat({ welcome: true, mode: undefined });
  check("welcome, nothing stored: no reposition", p.calls.repos, []);
}

async function dragTests() {
  // The poll ticks between resize drag steps: it must not resize, reposition or reorder.
  const p = await chat({ pos: { x: 300, y: 200 } });
  const rep = p.calls.repos.length, r0 = p.calls.resize.length;
  const steps = [[400, 600], [420, 620], [450, 650], [480, 700]];
  for (const [w, h] of steps) { await p.send({ type: "resize", w, h }); await p.clock.advance(1100); }
  check("drag steps are the only resizes during the drag", p.calls.resize.slice(r0), steps);
  check("the poll does not reposition during a drag", p.calls.repos.length, rep);
}

async function restartTests() {
  // Size and position together after a restart, at another zoom: same spot on screen.
  const store = {};
  let p = await chat({ store });
  await p.send({ type: "resize", w: 600, h: 800, done: true });
  p.win.x = 250; p.win.y = 120;
  await p.clock.advance(3000);
  p.close();
  p = await chat({ store, mode: undefined, zoom: 2, bounds: { x: 100, y: 50, width: 800, height: 450 } });
  check("restart: stored size", p.calls.resize[p.calls.resize.length - 1], [600, 800]);
  check("restart: one reposition, after the resize, to the same window spot",
    [p.calls.repos.length, p.calls.ops[p.calls.ops.length - 1], p.win.x, p.win.y], [1, "reposition", 250, 120]);
}

async function edgeAnchorTests() {
  // Left and top edges: the opposite side stays in place. The UI names the
  // edge in the resize message; code.js moves the window by the size change
  // measured from the drag start (window px / zoom = canvas units).
  let p = await chat({ zoom: 2, bounds: { x: 100, y: 50, width: 800, height: 450 }, win: { x: 500, y: 300 } });
  const r0 = p.calls.repos.length;
  await p.send({ type: "resize", w: 440, h: 560, edge: "w" });
  check("w: window moves left by the added width", [p.win.x, p.win.y], [440, 300]);
  await p.send({ type: "resize", w: 480, h: 560, edge: "w" });
  check("w: each step measures from the drag start", [p.win.x, p.win.y], [400, 300]);
  await p.send({ type: "resize", w: 480, h: 560, edge: "w", done: true });
  check("w: done keeps the right side", [p.win.x, p.win.y], [400, 300]);
  check("w: reposition comes after the resize", p.calls.ops.slice(-2), ["resize", "reposition"]);
  checkTrue("w: repositions happened", p.calls.repos.length > r0, "repos " + p.calls.repos.length);
  // A new drag measures from its own start.
  await p.send({ type: "resize", w: 380, h: 560, edge: "w", done: true });
  check("w: second drag narrows back from its own start", [p.win.x, p.win.y], [500, 300]);

  p = await chat({ win: { x: 500, y: 300 } });
  await p.send({ type: "resize", w: 380, h: 600, edge: "n", done: true });
  check("n: window moves up by the added height", [p.win.x, p.win.y], [500, 260]);

  p = await chat({ win: { x: 500, y: 300 } });
  await p.send({ type: "resize", w: 400, h: 600, edge: "nw", done: true });
  check("nw: both sides move", [p.win.x, p.win.y], [480, 260]);

  p = await chat({ win: { x: 500, y: 300 } });
  await p.send({ type: "resize", w: 400, h: 600, edge: "sw", done: true });
  check("sw: only the left side moves", [p.win.x, p.win.y], [480, 300]);
  p = await chat({ win: { x: 500, y: 300 } });
  await p.send({ type: "resize", w: 400, h: 600, edge: "ne", done: true });
  check("ne: only the top side moves", [p.win.x, p.win.y], [500, 260]);

  for (const edge of ["e", "s", "se", undefined, 7]) {
    p = await chat({ win: { x: 500, y: 300 } });
    const n = p.calls.repos.length;
    await p.send({ type: "resize", w: 450, h: 650, edge, done: true });
    check("edge " + edge + ": no reposition", p.calls.repos.length, n);
  }

  // The scale and the clamp count: the move is the real window size change.
  p = await chat({ win: { x: 500, y: 300 }, settings: { scale: 1.25, theme: "system" } });
  await p.send({ type: "resize", w: 420, h: 560, edge: "w", done: true });
  check("scale 1.25: moves by the scaled width", [p.win.x, p.win.y], [450, 300]);
  p = await chat({ win: { x: 900, y: 300 } });
  await p.send({ type: "resize", w: 5000, h: 560, edge: "w", done: true });
  check("clamped width: moves by the clamped change", [p.win.x, p.win.y], [380, 300]);

  // Old Figma: no getPosition or no reposition. The resize still works.
  for (const o of [{ noGetPos: true }, { noRepos: true }, { getPosThrows: true }, { noViewport: true }]) {
    p = await chat(Object.assign({ win: { x: 500, y: 300 } }, o));
    await p.send({ type: "resize", w: 440, h: 560, edge: "w", done: true });
    check("old Figma " + JSON.stringify(o) + ": resize works", p.calls.resize[p.calls.resize.length - 1], [440, 560]);
  }
}

(async () => {
  for (const [name, fn] of [["edge anchor", edgeAnchorTests], ["welcome", welcomeTests], ["drag", dragTests], ["restart", restartTests],["mutants", mutantTests], ["track", trackTests], ["bar", barTests], ["restore", restoreTests],
      ["clamp", clampTests], ["bad values", badValueTests], ["old Figma", oldFigmaTests],
      ["storage", storageTests], ["close", closeTests]]) {
    try { await fn(); } catch (e) { failed++; console.log(` FAIL  ${name} threw: ${e.stack || e.message}`); }
  }
  console.log(failed ? `\n${failed} failed` : "\nall passed");
  process.exit(failed ? 1 : 0);
})();
