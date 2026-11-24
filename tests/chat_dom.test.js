// The chat and the shell of the plugin window, end to end in a fake browser (jsdom).
//
// chat_ui.test.js checks the pure parts (ChatCore, code.js). This file loads
// the real plugin/ui.html, fakes the bridge socket and the Figma host, and
// drives the window like a designer does: type, paste a photo, pick a photo,
// quote, press an action button, answer a question, stop. It also covers the
// shell: the socket, reconnects, the status icon states, settings, language,
// the update offers and the slash popup.
//
// Texts are read from the page's own I18N table (tr), so a copy edit does not
// break a test. The page's long timers (reconnect, error hold, toast, flash)
// run SPEED times faster here; slow(ms) waits that many page milliseconds.
//
//     npm install --no-save jsdom      (once)
//     node tests/chat_dom.test.js
//
// JSDOM=/path/to/node_modules/jsdom picks a jsdom from another folder.
const fs = require("fs");
const path = require("path");

let JSDOM, VirtualConsole;
try { ({ JSDOM, VirtualConsole } = require(process.env.JSDOM || "jsdom")); }
catch (e) {
  console.log("SKIP chat_dom.test.js: jsdom is not installed (npm install --no-save jsdom)");
  process.exit(0);
}

const HTML = fs.readFileSync(path.join(__dirname, "..", "plugin", "ui.html"), "utf8");
const DOC = "dTest";
const PNG = "data:image/jpeg;base64,SHRUNK";
const SPEED = 20;

let failed = 0;
function check(label, actual, expected) {
  const a = JSON.stringify(actual), e = JSON.stringify(expected);
  const ok = a === e;
  if (!ok) failed++;
  console.log(`${ok ? "  ok  " : " FAIL "} ${label}${ok ? "" : `\n         got ${a}\n         want ${e}`}`);
}
const truthy = (label, v) => check(label, !!v, true);
// DOM nodes all turn into "{}" in JSON, so check() cannot tell two of them
// apart. same() compares by identity and prints a short name.
const tag = (el) => !el ? String(el) : el.tagName + (el.id ? "#" + el.id : "") + (el.closest && el.closest(".qf") ? "[" + el.closest(".qf").dataset.k + "]" : "");
function same(label, actual, expected) {
  const ok = actual === expected;
  if (!ok) failed++;
  console.log(`${ok ? "  ok  " : " FAIL "} ${label}${ok ? "" : `\n         got ${tag(actual)}\n         want ${tag(expected)}`}`);
}
const wait = (ms = 30) => new Promise((r) => setTimeout(r, ms));
const slow = (ms) => wait(ms / SPEED);
// Timers are coarse on Windows (~16 ms): wait for a state, not a fixed time.
async function until(fn, ms = 2000) { const end = Date.now() + ms; while (!fn() && Date.now() < end) await wait(10); }

// ─── harness ──────────────────────────────────────────────────────────────

async function open({ mode = "chat", history = [], busy = false, activity = [], commands, agents,
                      identity = { type: "identity", name: "Banner", docSig: DOC }, wsThrows = 0, darkMedia = null } = {}) {
  const sent = [], posted = [], sockets = [], observers = [];
  const errors = [];
  let throwsLeft = wsThrows, made = 0, urls = 0, k = 0;
  const vc = new VirtualConsole();  // the plugin log is noise here; script errors are kept
  vc.on("jsdomError", (e) => errors.push(e.message));
  const dom = new JSDOM(HTML, {
    runScripts: "dangerously", virtualConsole: vc, url: "http://localhost/", pretendToBeVisual: true,
    beforeParse(w) {
      // Long page timers run SPEED times faster; short ones (frames, holds under 300 ms) as is.
      const later = w.setTimeout.bind(w);
      w.setTimeout = (fn, ms = 0, ...a) => later(fn, ms >= 300 ? ms / SPEED : ms, ...a);
      w.WebSocket = class {
        constructor(url) {
          made++;
          if (throwsLeft > 0) { throwsLeft--; throw new Error("no socket"); }
          this.url = url; this.readyState = 0; sockets.push(this);
          setTimeout(() => { if (this.readyState === 0) { this.readyState = 1; this.onopen && this.onopen(); } }, 5);
        }
        send(d) { sent.push(JSON.parse(d)); }
        close() { this.readyState = 3; this.onclose && this.onclose(); }
      };
      w.WebSocket.CONNECTING = 0; w.WebSocket.OPEN = 1; w.WebSocket.CLOSING = 2; w.WebSocket.CLOSED = 3;
      w.parent.postMessage = (m) => posted.push(m.pluginMessage);
      // Photos are shrunk on a canvas; jsdom has no canvas, so fake the parts used.
      // Each photo gets its own data URL, so a test can tell which one was removed.
      w.URL.createObjectURL = () => "blob:x" + (urls++); w.URL.revokeObjectURL = () => {};
      w.Image = class {
        set src(u) { this._src = u; setTimeout(() => (this.broken ? this.onerror() : this.onload()), 1); }
        get naturalWidth() { return 3200; } get naturalHeight() { return 1600; }
      };
      w.HTMLCanvasElement.prototype.getContext = () => ({ fillRect() {}, drawImage() {}, set fillStyle(v) {} });
      w.HTMLCanvasElement.prototype.toDataURL = function () { return PNG + "-" + this.width + "x" + this.height + "-" + (k++); };
      w.Element.prototype.scrollIntoView = function () { this._scrolledTo = true; };
      w.ResizeObserver = class { constructor(cb) { this.cb = cb; observers.push(this); } observe(el) { this.el = el; } };
      if (darkMedia !== null) {
        w.matchMedia = (q) => ({ matches: /dark/.test(q) ? darkMedia : false, media: q, addEventListener() {} });
      }
    },
  });
  const w = dom.window, d = w.document;
  w.addEventListener("error", (e) => errors.push(e.message));
  const pm = (m) => w.dispatchEvent(new w.MessageEvent("message", { data: { pluginMessage: m } }));
  const ws = () => sockets[sockets.length - 1];
  const push = (event, doc = DOC) => ws().onmessage({ data: JSON.stringify({ type: "chat", doc, event }) });
  await wait(20);
  if (identity) pm(identity);
  pm({ type: "ui-mode", mode });
  await wait();
  if (identity) {
    push(Object.assign({ kind: "history", busy, activity, messages: history }, commands ? { commands } : {}, agents || {}));
    await wait();
  }
  const $ = (s) => d.querySelector(s);
  const input = $("#cInput");
  return {
    w, d, $, sent, posted, push, ws, sockets, errors, observers, input, pm,
    made: () => made,
    // A text from the page's I18N table, in the current language.
    tr: (key, vars) => w.eval("t")(key, vars),
    bridge(m) { ws().onmessage({ data: JSON.stringify(m) }); },
    card: () => d.querySelector("#cMsgs .notice"),
    st: () => [...d.querySelector("#icon").classList].filter((c) => c.startsWith("st-")),
    label: () => d.querySelector("#icon").getAttribute("aria-label"),
    of: (t) => posted.filter((m) => m && m.type === t),
    sentOf: (t) => sent.filter((m) => m.type === t),
    chatSends: () => sent.filter((m) => m.type === "chat-send"),
    type(text) { input.value = text; input.dispatchEvent(new w.Event("input", { bubbles: true })); },
    enter(el = input, opts = {}) { el.dispatchEvent(new w.KeyboardEvent("keydown", Object.assign({ key: "Enter", bubbles: true, cancelable: true }, opts))); },
    key(k, el = input, opts = {}) { const ev = new w.KeyboardEvent("keydown", Object.assign({ key: k, bubbles: true, cancelable: true }, opts)); el.dispatchEvent(ev); return ev; },
    file(name, type) { return new w.File(["x"], name, { type }); },
    paste({ files = [], items = null, text = null } = {}) {
      const ev = new w.Event("paste", { bubbles: true, cancelable: true });
      ev.clipboardData = {
        files,
        items: items || files.map((f) => ({ kind: "file", type: f.type, getAsFile: () => f })),
        types: text ? ["text/plain"] : files.length ? ["Files"] : [],
        getData: (t) => (t === "text/plain" ? text || "" : ""),
      };
      input.dispatchEvent(ev);
      return ev;
    },
    pick(files) {
      const inp = $("#cFile");
      Object.defineProperty(inp, "files", { value: files, configurable: true });
      inp.dispatchEvent(new w.Event("change", { bubbles: true }));
    },
    // The settings page dropdowns (scale, theme, lang): a user picks an option, the select fires change.
    choose(k, v) {
      const s = $(`#settings select#${PAGE_SEL[k]}`); if (!s) throw new Error("no select " + k);
      s.value = String(v); s.dispatchEvent(new w.Event("change", { bubbles: true }));
    },
    chosen: (k) => { const s = $(`#settings select#${PAGE_SEL[k]}`); return s ? s.value : null; },
    thumbs: () => d.querySelectorAll("#cAtt .th").length,
    thumbSrcs: () => [...d.querySelectorAll("#cAtt .th img")].map((i) => i.getAttribute("src")),
    rows: () => [...d.querySelectorAll("#cMsgs .mrow")],
    toast: () => $("#cToast").classList.contains("on") ? $("#cToast").textContent : "",
    close() { w.close(); },
  };
}
// The settings page dropdowns for the code.js settings: native <select>s, values as stored.
const PAGE_SEL = { scale: "sScale", theme: "sTheme", lang: "sLang" };
const msg = (id, from, text, extra = {}) => Object.assign({ id, from, text, t: 1795280000 + id }, extra);

// ─── tests ────────────────────────────────────────────────────────────────

async function loadAndModes() {
  const p = await open({ mode: "bar" });
  truthy("asks for this file's chat once connected", p.sent.some((m) => m.type === "chat-load"));
  check("bar mode hides the chat", p.d.body.classList.contains("chatmode"), false);
  p.$("#chatbtn").click();
  check("chat icon asks code.js for the chat", p.posted.filter((m) => m && m.type === "ui-mode").pop(), { type: "ui-mode", mode: "chat" });
  p.push({ kind: "message", message: msg(1, "claude", "привіт") });
  await wait();
  truthy("a reply while in the bar marks the icon unread", p.d.body.classList.contains("unread"));
  p.w.dispatchEvent(new p.w.MessageEvent("message", { data: { pluginMessage: { type: "ui-mode", mode: "chat" } } }));
  await wait();
  truthy("chat mode shows the chat", p.d.body.classList.contains("chatmode"));
  check("opening the chat clears unread", p.d.body.classList.contains("unread"), false);
  check("history is drawn", p.rows().length, 1);
  p.$("#chatbtn").click();
  check("chat icon in chat mode asks for the bar", p.posted.filter((m) => m && m.type === "ui-mode").pop(), { type: "ui-mode", mode: "bar" });
  p.close();

  const e = await open();
  truthy("empty chat shows the hint", e.$("#cMsgs .hint"));
  e.push({ kind: "message", message: msg(1, "claude", "чужий файл") }, "dOther");
  await wait();
  check("events of another file are ignored", e.rows().length, 0);
  e.close();
}

async function sending() {
  const p = await open();
  p.type("Зроби кнопку синьою");
  p.enter();
  check("Enter sends the text", p.chatSends(), [{ type: "chat-send", text: "Зроби кнопку синьою" }]);
  check("input is cleared after send", p.input.value, "");

  p.type("рядок 1");
  p.enter(p.input, { shiftKey: true });
  check("Shift+Enter does not send", p.chatSends().length, 1);

  p.type("   ");
  p.enter();
  check("blank text is not sent", p.chatSends().length, 1);

  p.type("кнопкою");
  p.$("#cSend").click();
  check("send button sends", p.chatSends().pop().text, "кнопкою");

  p.push({ kind: "message", message: msg(1, "me", "Зроби кнопку синьою") });
  p.push({ kind: "busy", busy: true });
  await wait();
  check("my message is drawn", p.rows().length, 1);
  p.push({ kind: "refused" });
  await wait();
  check("a refused send shows a toast", p.toast(), p.tr("notSent"));
  check("a refused send puts the text back", p.input.value, "кнопкою");
  p.close();

  const off = await open();
  off.ws().readyState = 3;
  off.type("без мосту");
  off.enter();
  check("no bridge: nothing is sent", off.chatSends().length, 0);
  check("no bridge: a toast says so", off.toast(), off.tr("noBridge"));
  check("no bridge: the text stays", off.input.value, "без мосту");
  off.close();
}

async function photos() {
  // Paste from the clipboard: a screenshot or a copied image.
  let p = await open();
  const ev = p.paste({ files: [p.file("shot.png", "image/png")] });
  await wait();
  check("pasted image is attached", p.thumbs(), 1);
  truthy("pasting an image does not paste text", ev.defaultPrevented);
  p.type("Зроби так само");
  p.enter();
  const s = p.chatSends().pop();
  check("photo goes with the message, shrunk to 1600 px", s.images.map((x) => x.startsWith(PNG + "-1600x800-")), [true]);
  check("thumbnails are cleared after send", p.thumbs(), 0);
  p.close();

  // Some apps put the image only in clipboardData.items, not in files.
  p = await open();
  p.paste({ items: [{ kind: "file", type: "image/png", getAsFile: () => p.file("a.png", "image/png") }] });
  await wait();
  check("image from clipboard items is attached", p.thumbs(), 1);
  p.close();

  p = await open();
  const t = p.paste({ text: "просто текст" });
  await wait();
  check("pasting text attaches nothing", p.thumbs(), 0);
  check("pasting text keeps the normal paste", t.defaultPrevented, false);
  p.paste({ files: [p.file("doc.pdf", "application/pdf")] });
  await wait();
  check("a non-image file is ignored", p.thumbs(), 0);
  p.close();

  // The clip button opens the file picker; the picked photos are attached.
  p = await open();
  let clicked = false;
  p.$("#cFile").click = () => { clicked = true; };
  p.$("#cClip").click();
  truthy("clip button opens the file picker", clicked);
  p.pick([p.file("a.png", "image/png"), p.file("b.jpg", "image/jpeg"), p.file("c.txt", "text/plain")]);
  await wait();
  check("picked images are attached, other files skipped", p.thumbs(), 2);
  p.$("#cAtt .th button").click();
  check("the x removes a photo", p.thumbs(), 1);
  p.$("#cSend").click();
  const only = p.chatSends().pop();
  check("a photo without text can be sent", [only.text, only.images.length], ["", 1]);
  p.close();

  p = await open();
  p.pick(Array.from({ length: 10 }, (_, i) => p.file(i + ".png", "image/png")));
  await until(() => p.thumbs() >= 8);
  await wait(100);
  check("at most 8 photos per message", p.thumbs(), 8);
  p.close();

  p = await open();
  p.w.Image = class { set src(u) { setTimeout(() => this.onerror(), 1); } };
  p.paste({ files: [p.file("bad.png", "image/png")] });
  await wait();
  check("a broken image shows a toast", p.toast(), p.tr("noPhoto"));
  check("a broken image is not attached", p.thumbs(), 0);
  p.close();

  p = await open({ history: [msg(1, "me", "", { images: ["/chat/img/dTest/a.jpg"] })] });
  check("a sent photo is shown from the bridge", p.$("#cMsgs .mrow img").getAttribute("src"), "http://localhost:8787/chat/img/dTest/a.jpg");
  p.close();
}

async function quotes() {
  const p = await open({ history: [msg(1, "claude", "Готово. Кнопка   синя."), msg(2, "me", "дякую")] });
  p.rows()[0].querySelector(".qbtn").click();
  check("quote bar shows who and what", [p.$("#cQbar").hidden, p.$("#cQbar b").textContent, p.$("#cQbar span").textContent], [false, "Claude", "Готово. Кнопка синя."]);
  p.type("А тепер червоною");
  p.enter();
  check("quote goes with the message", p.chatSends().pop(), { type: "chat-send", text: "А тепер червоною", quote: 1 });
  check("quote bar closes after send", p.$("#cQbar").hidden, true);
  p.rows()[1].querySelector(".qbtn").click();
  p.$("#cQbarX").click();
  check("x removes the quote", p.$("#cQbar").hidden, true);

  p.push({ kind: "message", message: msg(3, "me", "про це", { quote: { id: 1, from: "claude", text: "Готово." } }) });
  await wait();
  p.rows()[2].querySelector(".mq").click();
  truthy("clicking a quote scrolls to that message", p.rows()[0]._scrolledTo);
  p.push({ kind: "message", message: msg(4, "me", "про старе", { quote: { id: 99, from: "claude", text: "давно" } }) });
  await wait();
  p.rows()[3].querySelector(".mq").click();
  check("a quote of a removed message says so", p.toast(), p.tr("gone"));
  p.close();
}

async function actionsAndQuestions() {
  const p = await open({ history: [
    msg(1, "claude", "Старе", { actions: ["Ні"] }),
    msg(2, "claude", "Що далі?", { actions: ["Зроби темну тему", "Залиш так"] }),
  ] });
  check("buttons only under the last reply", p.d.querySelectorAll(".act").length, 2);
  check("buttons are lettered A, B", [...p.d.querySelectorAll(".act .ak")].map((x) => x.textContent), ["A", "B"]);
  p.d.querySelector(".act").click();
  check("a button sends its text", p.chatSends().pop(), { type: "chat-send", text: "Зроби темну тему" });
  truthy("buttons lock after a click", [...p.d.querySelectorAll(".act")].every((b) => b.disabled));
  p.push({ kind: "busy", busy: true });
  await wait();
  check("no buttons while Claude works", p.d.querySelectorAll(".act").length, 0);
  p.close();

  const q = await open({ history: [msg(5, "claude", "Пара питань", { questions: ["Який колір?", "Який шрифт?"] })] });
  check("each question has its own answer field", q.d.querySelectorAll(".qf").length, 2);
  const f = q.d.querySelector(".qf textarea");
  f.value = "синій"; f.dispatchEvent(new q.w.Event("input", { bubbles: true }));
  q.enter(f);
  check("an answer is sent with its question", q.chatSends().pop(), { type: "chat-send", text: "синій", answer: { msg: 5, i: 0 } });
  q.push({ kind: "message", message: msg(6, "me", "синій", { answer: { msg: 5, i: 0 } }) });
  await wait();
  check("answered question shows the answer", q.$(".qa.done .qans").textContent, "синій");
  check("progress counts answers", q.$(".qs-head span").textContent, q.tr("answers", { d: 1, n: 2 }));
  q.close();
}

async function busyStreamStop() {
  const p = await open({ history: [msg(1, "me", "Опиши фрейми")] });
  check("idle: no typing line", p.$("#cTyping").hidden, true);
  p.push({ kind: "busy", busy: true });
  await wait();
  check("busy: typing line is shown", p.$("#cTyping").hidden, false);
  truthy("busy: glow is on", p.$("#cGlow").classList.contains("on"));
  check("busy without steps says thinking", p.$("#cStep").textContent, p.tr("thinking") + "…");
  truthy("busy: the seconds run", new RegExp("^" + p.tr("sec", { n: "\\d+" }) + "$").test(p.$("#cTime").textContent));

  p.push({ kind: "activity", t: 1795280002, text: "Читаю сторінку" });
  await wait();
  check("the last step is shown", p.$("#cStep").textContent, "Читаю сторінку…");

  p.push({ kind: "delta", text: "Фрейм 1: " });
  p.push({ kind: "delta", text: "1080x1350" });
  await wait();
  check("streamed text is drawn as it comes", p.$("#cMsgs .msg.stream").textContent, "Фрейм 1: 1080x1350");

  // The glow sits on the top edge of the compose box, whatever its height.
  const box = p.$("#cCompose");
  Object.defineProperty(box, "offsetHeight", { value: 120, configurable: true });
  p.observers.filter((o) => o.el === box).forEach((o) => o.cb([]));
  check("glow follows the compose height", parseFloat(p.$("#cGlow").style.bottom) > 120, true);

  p.$("#cStop").click();
  check("stop sends chat-stop", p.sent.filter((m) => m.type === "chat-stop").length, 1);
  check("stop shows stopping", p.$("#cStep").textContent, p.tr("stopping") + "…");
  check("stop button locks", p.$("#cStop").disabled, true);

  p.push({ kind: "message", message: msg(2, "claude", "Фрейм 1: 1080x1350") });
  p.push({ kind: "busy", busy: false });
  await wait();
  check("final message replaces the stream", [p.d.querySelectorAll(".msg.stream").length, p.rows().length], [0, 2]);
  check("idle again: typing line hidden", p.$("#cTyping").hidden, true);
  check("idle again: glow off", p.$("#cGlow").classList.contains("on"), false);

  p.push({ kind: "busy", busy: true });
  await wait();
  check("next turn: stop button works again", [p.$("#cStop").disabled, p.$("#cStep").textContent], [false, p.tr("thinking") + "…"]);
  p.close();

  const r = await open({ busy: true, activity: [{ t: Math.floor(Date.now() / 1000) - 75, text: "Малюю" }] });
  check("opened mid-task: shows the step and real time", [r.$("#cStep").textContent, r.$("#cTime").textContent], ["Малюю…", r.tr("min", { n: 1 })]);
  r.close();
}

// What the user sees when something breaks (docs/CHAT.md, Error states).
async function errorStates() {
  const p = await open({ history: [msg(1, "me", "Зроби"), msg(2, "system", "Claude Code не увійшов в акаунт. Запусти claude.\nNot logged in", { error: true })] });
  const sys = p.$("#cMsgs .mrow.system");
  truthy("an error note is drawn as a system row", sys && sys.querySelector(".msg.system"));
  truthy("the error note shows its text", /не увійшов/.test(sys.textContent));
  check("an error note has no quote button", sys.querySelector(".qbtn"), null);

  p.push({ kind: "busy", busy: true });
  await wait();
  p.ws().close();             // the bridge went away mid-turn
  p.$("#cStop").click();
  check("stop with no bridge: a toast says so", p.toast(), p.tr("noBridge"));
  check("stop with no bridge: nothing is locked", p.$("#cStop").disabled, false);
  const before = p.sent.length;
  Object.defineProperty(p.d, "visibilityState", { value: "visible", configurable: true });
  p.d.dispatchEvent(new p.w.Event("visibilitychange"));
  await until(() => p.ws().readyState === 1);
  await wait();
  const again = p.sent.slice(before).map((m) => m.type);
  truthy("reconnect says hello and asks for the chat again", again.includes("hello") && again.includes("chat-load"));
  p.push({ kind: "history", busy: false, activity: [], messages: [msg(1, "me", "Зроби"), msg(3, "system", "Міст зупинився.", { error: true })] });
  await wait();
  check("history after reconnect: not busy any more", [p.$("#cTyping").hidden, p.$("#cGlow").classList.contains("on")], [true, false]);
  p.close();

  // Stop that could not be sent must not lock the button for the rest of the turn.
  const s = await open({ busy: true });
  s.ws().send = () => { throw new Error("closed"); };
  s.$("#cStop").click();
  check("stop that fails to send: a toast says so", s.toast(), s.tr("notStopped"));
  check("stop that fails to send: the button works again", [s.$("#cStop").disabled, s.$("#cStep").textContent], [false, s.tr("thinking") + "…"]);
  s.close();

  // Stop pressed, then the socket dropped before the bridge got it: still busy after reconnect.
  const r = await open({ busy: true });
  r.$("#cStop").click();
  check("stop pressed: button locks", r.$("#cStop").disabled, true);
  r.push({ kind: "history", busy: true, activity: [], messages: [] });
  await wait();
  check("fresh history while busy: stop can be pressed again", r.$("#cStop").disabled, false);
  r.close();

  const f = await open();
  f.ws().send = () => { throw new Error("closed"); };
  f.type("не дійде");
  f.enter();
  check("send that throws: a toast says so", f.toast(), f.tr("notSent"));
  check("send that throws: the text stays", f.input.value, "не дійде");
  f.ws().onmessage({ data: "not json {" });
  f.ws().onmessage({ data: "null" });
  f.ws().onmessage({ data: "[1,2]" });
  await wait();
  check("broken socket data: no script errors", f.errors, []);
  f.close();

  // The bridge never comes up: the socket never opens.
  const n = await open();
  n.ws().readyState = 0;
  n.type("є хто");
  n.enter();
  check("bridge not up yet: send gives a toast", n.toast(), n.tr("noBridge"));
  n.$("#cStop").hidden = false;
  n.$("#cStop").click();
  check("bridge not up yet: stop gives a toast", n.toast(), n.tr("noBridge"));
  n.close();
}

// jsdom has no layout. A fake list: 300 px tall, content height set by the test.
// Like a browser, the scroll event comes later, and the content can grow after a photo loads.
// hidden(): the list is not shown (display none), so it has no height.
function fakeLayout(p, hidden = () => false) {
  const box = p.$("#cMsgs"), L = { H: 1000, top: 0 };
  const fire = () => setTimeout(() => box.dispatchEvent(new p.w.Event("scroll")), 0);
  Object.defineProperty(box, "clientHeight", { get: () => hidden() ? 0 : 300 });
  Object.defineProperty(box, "scrollHeight", { get: () => hidden() ? 0 : L.H });
  Object.defineProperty(box, "scrollTop", { get: () => L.top, set: (v) => { const n = Math.max(0, Math.min(v, L.H - 300)); if (n !== L.top) { L.top = n; fire(); } } });
  L.user = (v) => { box.scrollTop = v; };  // a scroll by the user
  L.bottom = () => L.top === L.H - 300;
  return L;
}

async function scrolling() {
  const p = await open({ history: [msg(1, "claude", "Привіт")] });
  const L = fakeLayout(p);
  p.push({ kind: "message", message: msg(2, "me", "", { images: ["/chat/img/dTest/a.jpg"] }) });
  await wait(5);
  truthy("a new message scrolls to the bottom", L.bottom());
  L.H = 1400;  // the photo loads and makes the list taller
  p.$("#cMsgs .mrow img").dispatchEvent(new p.w.Event("load"));
  await wait(5);
  truthy("a loaded photo keeps the list at the bottom", L.bottom());
  L.H = 1700; p.$("#cMsgs .mrow img").dispatchEvent(new p.w.Event("load")); await wait(5);
  // The reply redraws the list. The photo is drawn again with no height yet, so the list is short.
  // The late scroll event comes when the photo is back, and the list is taller again.
  L.H = 1500;
  p.push({ kind: "message", message: msg(3, "claude", "Синій фон, жовте коло, червоний квадрат.") });
  L.H = 2000; await wait(5);
  p.$("#cMsgs .mrow img").dispatchEvent(new p.w.Event("load"));
  await wait(5);
  truthy("Claude's reply after a photo is scrolled into view", L.bottom());
  L.user(200); await wait(5);
  p.push({ kind: "message", message: msg(4, "claude", "Ще одне") });
  await wait(5);
  check("after the user scrolls up, a new message does not jump", L.top, 200);
  L.user(L.H); await wait(5); L.H = 2300;
  p.push({ kind: "message", message: msg(5, "claude", "І ще") });
  await wait(5);
  truthy("back at the bottom, the list follows again", L.bottom());
  p.close();
}

// The settings page hides the list (display none), so the browser drops its scroll position
// and fires a scroll event at 0. Back in the chat the list must be where the user left it.
async function settingsScroll() {
  const p = await open({ history: [msg(1, "claude", "Привіт"), msg(2, "me", "ще")] });
  const html = p.d.documentElement, box = p.$("#cMsgs");
  const L = fakeLayout(p, () => html.classList.contains("settings-open"));
  const drop = async () => { L.top = 0; box.dispatchEvent(new p.w.Event("scroll")); await wait(5); };
  p.push({ kind: "message", message: msg(3, "claude", "Відповідь") }); await wait(5);
  truthy("at the bottom before settings", L.bottom());

  p.$("#setbtn").click(); await drop();
  p.push({ kind: "message", message: msg(4, "claude", "Нова, поки налаштування відкриті") }); await wait(5);
  L.H = 1200;
  p.$("#sBack").click(); await wait(5);
  truthy("back arrow: the list is at the bottom again", L.bottom());

  p.$("#setbtn").click(); await drop();
  p.choose("scale", "1.25"); L.H = 1500;  // a bigger scale makes the list taller
  p.$("#setbtn").click(); await wait(5);
  truthy("gear toggle after a scale change: at the bottom again", L.bottom());

  L.user(400); await wait(5);
  p.$("#setbtn").click(); await drop();
  p.push({ kind: "message", message: msg(5, "claude", "Ще одна") }); await wait(5);
  p.$("#sBack").click(); await wait(5);
  check("scrolled up before settings: the position is kept", L.top, 400);
  p.push({ kind: "message", message: msg(6, "claude", "І ще") }); await wait(5);
  check("scrolled up: a new message after settings does not jump", L.top, 400);
  check("no script errors", p.errors, []);
  p.close();
}

// The header has no colored strip: only the small status icon carries the
// color (st-* class), with the state in words in its label.
async function statusIcon() {
  const p = await open({ mode: "chat" });
  const sock = (m) => p.ws().onmessage({ data: JSON.stringify(m) });
  const seen = (label, cls, key, vars) => {
    check(label + ": icon state", p.st(), [cls]);
    truthy(label + ": the label says so", (p.label() || "").includes(p.tr(key, vars)));
  };
  seen("connected", "st-ok", "stOk");
  check("the tooltip equals the label", p.$("#icon").title, p.label());
  check("file name is in the header", p.$("#status").textContent, "Banner");
  sock({ type: "peers", index: 2, total: 2 }); await wait(5);
  check("several files: count stays", p.$("#peers").textContent, "2/2");
  truthy("several files: label tells the slot", p.label().includes(p.tr("stSlot", { i: 2, n: 2 })));
  sock({ type: "exec", id: "e1", code: "return 1" }); await wait(5);
  seen("script running", "st-busy", "stBusy");
  check("busy: count still shown", p.$("#peers").textContent, "2/2");
  p.pm({ type: "error", id: "e1", text: "boom" });
  await until(() => p.st()[0] === "st-err");
  seen("script failed", "st-err", "stErr");
  await until(() => p.st()[0] === "st-ok");
  sock({ type: "outdated", running: "1", expected: "2" }); await wait(5);
  seen("plugin code is old", "st-update", "stRerun");
  check("re-run offer stays in words", p.$("#status").textContent, p.tr("rerunBar"));
  p.w.eval("outdated = false; render()");
  sock({ type: "update", latest: "2.4.0", current: "2.3.0", url: "https://x" }); await wait(5);
  seen("new release", "st-update", "stUpdate", { v: "2.4.0" });
  check("update offer stays in words", p.$("#status").textContent, p.tr("updateBar", { v: "2.4.0" }));
  truthy("update state is on the body", p.d.body.classList.contains("update"));
  p.ws().onerror(); await wait(5);
  seen("socket error", "st-err", "stLinkErr");
  check("error text stays", p.$("#status").textContent, p.tr("error"));
  p.ws().onclose(); await wait(5);
  seen("reconnecting", "st-conn", "stConn");
  check("the tooltip still equals the label", p.$("#icon").title, p.label());
  truthy("chat icon stays in the header", p.$("#bar #barActions #chatbtn"));
  p.close();

  const b = await open({ mode: "bar" });
  check("collapsed bar: status icon is green", b.st(), ["st-ok"]);
  b.close();
}

// The settings page: interface scale, theme and language. code.js stores them (figma.clientStorage)
// and sends them back on start as {type: "settings", scale, theme, lang}.
//
// Contract of the page (#settings): a plain list of rows, no cards. Every setting is one row,
// .srow: its heading (h3, the label) on the left and a native <select> in a .dd wrapper on the
// right, with aria-labelledby pointing at that heading. The rows in order: scale (select#sScale,
// values 0.9 1 1.1 1.25), theme (select#sTheme, system light dark), language (select#sLang, uk en,
// each option with its own lang attribute), then the agent rows (sAgent, sModel, sEffort).
// A row has no card background and no shadow. Visible rows are split by one divider: the top
// border of every visible row after the first visible one; a hidden row adds no divider.
// A change on a select sends exactly one message; a re-render keeps the focus on the select.
async function settings() {
  const p = await open();
  const html = p.d.documentElement;
  const lastSet = () => p.posted.filter((m) => m && m.type === "settings").pop();
  const scaleVar = () => html.style.getPropertyValue("--ui-scale");
  const sel = (k) => p.$(`#settings select#${PAGE_SEL[k]}`);
  const vals = (k) => sel(k) ? [...sel(k).options].map((o) => o.value) : [];

  truthy("each page setting is a select", ["scale", "theme", "lang"].every((k) => sel(k) && sel(k).tagName === "SELECT"));
  truthy("each select is named by its heading in the same row", ["scale", "theme", "lang"].every((k) => {
    const h = sel(k) && p.d.getElementById(sel(k).getAttribute("aria-labelledby"));
    return h && h.tagName === "H3" && h.textContent.trim() && h.closest(".srow") && h.closest(".srow") === sel(k).closest(".srow");
  }));
  check("the options are the stored values", [vals("scale"), vals("theme"), vals("lang")],
        [["0.9", "1", "1.1", "1.25"], ["system", "light", "dark"], ["uk", "en"]]);
  check("the scale options read as percent", sel("scale") ? [...sel("scale").options].map((o) => o.textContent.trim()) : [], ["90%", "100%", "110%", "125%"]);
  check("the theme options read as the I18N texts", sel("theme") ? [...sel("theme").options].map((o) => o.textContent.trim()) : [],
        [p.tr("themeSystem"), p.tr("themeLight"), p.tr("themeDark")]);
  check("each language option carries its own lang", sel("lang") ? [...sel("lang").options].map((o) => o.lang) : [], ["uk", "en"]);
  check("no radio buttons and no cards on the page", p.d.querySelectorAll("#settings [role=radio], #settings .seg, #settings .sgroup").length, 0);
  const rows = [...p.d.querySelectorAll("#settings .srow")];
  check("one row for each of the seven settings (the notes row shows only where notes work)", rows.length, 7);
  truthy("every select sits in a row", [...p.d.querySelectorAll("#settings select")].every((s) => s.closest(".srow")));
  const cs = (el) => p.w.getComputedStyle(el);
  const flat = (v) => !v || v === "none" || v === "transparent" || v === "initial" || /^rgba\(0, 0, 0, 0\)$/.test(v);
  truthy("rows have no shadow and no card background", rows.every((r) => flat(cs(r).boxShadow) && flat(cs(r).backgroundColor) && flat(cs(r).background)));
  // Dividers: the top border of each visible row after the first visible one, from a token.
  // The page itself is closed here: a row counts as shown by its own hidden state and display.
  const rowOn = (r) => !r.hidden && cs(r).display !== "none";
  const lines = () => rows.filter(rowOn).map((r) => cs(r).borderTopStyle === "solid");
  check("an older bridge: three rows, one divider between each two", lines(), [false, true, true]);
  truthy("the divider color is a token", rows.slice(1).every((r) => /^var\(--/.test(cs(r).borderTopColor)));
  p.push({ kind: "providers", providers: [{ id: "claude", name: "Claude Code", models: [] }], settings: { provider: "claude" } }); await wait();
  check("hidden rows add no divider", lines(), [false, true, true, true]);
  check("defaults before storage answers: 100 percent", [scaleVar(), p.chosen("scale")], ["1", "1"]);
  check("defaults before storage answers: system theme", [p.chosen("theme"), html.classList.contains("dark")], ["system", false]);

  p.$("#setbtn").click();
  truthy("the settings button opens the settings page", html.classList.contains("settings-open"));
  p.$("#sBack").click();
  check("back returns to the chat", html.classList.contains("settings-open"), false);
  p.$("#setbtn").click(); p.$("#setbtn").click();
  check("the settings button closes the page too", html.classList.contains("settings-open"), false);
  p.$("#setbtn").click();
  p.pm({ type: "ui-mode", mode: "bar" }); await wait();
  check("going to the bar closes the settings page", html.classList.contains("settings-open"), false);
  p.pm({ type: "ui-mode", mode: "chat" }); await wait();
  p.$("#setbtn").click();

  const before = p.of("settings").length;
  sel("scale") && sel("scale").focus();
  p.choose("scale", "1.25");
  check("a scale change sends one message", p.of("settings").length, before + 1);
  check("scale goes to code.js with the theme", lastSet(), { type: "settings", scale: 1.25, theme: "system", lang: "uk" });
  check("scale is applied to the whole page", scaleVar(), "1.25");
  check("the chosen scale is shown", p.chosen("scale"), "1.25");
  same("a scale change keeps the focus on its select", p.d.activeElement, sel("scale"));
  check("scale changes send nothing to the bridge", p.sentOf("chat-settings").length, 0);

  p.choose("theme", "dark");
  check("a theme change sends one message", p.of("settings").length, before + 2);
  check("theme goes to code.js with the scale", lastSet(), { type: "settings", scale: 1.25, theme: "dark", lang: "uk" });
  truthy("dark theme sets the dark class", html.classList.contains("dark"));
  p.choose("theme", "light");
  html.classList.add("figma-dark"); await wait();
  check("light theme stays light in a dark Figma", html.classList.contains("dark"), false);
  p.choose("theme", "system");
  truthy("system theme follows a dark Figma", html.classList.contains("dark"));
  html.classList.remove("figma-dark"); html.classList.add("figma-light"); await wait();
  check("system theme follows Figma back to light", html.classList.contains("dark"), false);
  check("a theme tap keeps the scale", lastSet(), { type: "settings", scale: 1.25, theme: "system", lang: "uk" });
  p.close();

  const r = await open();
  const n = r.posted.length;
  r.pm({ type: "settings", scale: 1.1, theme: "dark" }); await wait();
  const rh = r.d.documentElement;
  check("restored scale from storage", rh.style.getPropertyValue("--ui-scale"), "1.1");
  truthy("restored dark theme from storage", rh.classList.contains("dark"));
  check("restored values are shown", [r.chosen("scale"), r.chosen("theme"), r.chosen("lang")], ["1.1", "dark", "uk"]);
  check("restoring does not send the settings back", r.posted.slice(n).filter((m) => m && m.type === "settings"), []);
  r.pm({ type: "settings" }); await wait();
  check("empty storage gives the defaults", [rh.style.getPropertyValue("--ui-scale"), rh.classList.contains("dark")], ["1", false]);
  r.pm({ type: "settings", scale: 7, theme: "neon" }); await wait();
  check("bad stored values give the defaults", [r.chosen("scale"), r.chosen("theme"), r.chosen("lang")], ["1", "system", "uk"]);
  check("no script errors in settings", [...p.errors, ...r.errors], []);
  r.close();
}

// Language: Українська or English on the settings page. It switches the whole interface at
// once, goes to code.js with the other settings, and to the bridge in chat-load (the chat language).
async function language() {
  const CYR = /[Ѐ-ӿ]/, DASHQ = /[—–"«»“”„]/;
  const p = await open({ commands: [{ name: "clear" }, { name: "compact" }] });
  const html = p.d.documentElement;
  const lastSet = () => p.posted.filter((m) => m && m.type === "settings").pop();
  const lastLoad = () => p.sent.filter((m) => m.type === "chat-load").pop();
  const checked = () => [p.chosen("lang")];
  const buttons = () => [...p.card().querySelectorAll("button")].map((b) => b.textContent.trim());
  // Every text and label a designer can see or hear, outside the chat messages.
  const uiText = () => {
    const out = [];
    p.d.querySelectorAll("body *").forEach((el) => {
      if (el.closest("[data-lang], option[lang]") || el.closest("script")) return;   // a language names itself
      for (const a of ["title", "aria-label", "placeholder"]) if (el.getAttribute(a)) out.push(el.getAttribute(a));
      for (const n of el.childNodes) if (n.nodeType === 3 && n.textContent.trim()) out.push(n.textContent.trim());
    });
    return out;
  };

  check("Ukrainian by default", [html.lang, checked()], ["uk", ["uk"]]);
  check("chat-load tells the bridge the language", lastLoad().lang, "uk");

  p.$("#setbtn").click();
  const loads = p.sent.filter((m) => m.type === "chat-load").length;
  const lsel = p.$("#settings select#sLang");
  lsel && lsel.focus();
  const sets = p.of("settings").length;
  p.choose("lang", "en");
  await wait();
  check("a language change sends one message", p.of("settings").length, sets + 1);
  same("the live re-render keeps the focus on the language select", p.d.activeElement, lsel);
  check("the language select shows English", p.chosen("lang"), "en");
  check("the theme options follow the language", [...p.d.querySelectorAll("#settings select#sTheme option")].map((o) => o.textContent.trim()),
        [p.tr("themeSystem"), p.tr("themeLight"), p.tr("themeDark")]);
  check("the language goes to code.js with the rest", lastSet(), { type: "settings", scale: 1, theme: "system", lang: "en" });
  check("the bridge is told at once", [p.sent.filter((m) => m.type === "chat-load").length, lastLoad().lang], [loads + 1, "en"]);
  check("English is marked", checked(), ["en"]);
  check("page language", html.lang, "en");
  check("the static texts follow", [p.$("#cInput").placeholder, p.$("#settings h2").textContent], [p.tr("placeholder"), p.tr("settings")]);
  check("the status label follows", [p.label(), p.$("#icon").title], [p.tr("stOk"), p.tr("stOk")]);
  p.$("#sBack").click();
  check("the empty chat card follows", p.$("#cMsgs .hint h2").textContent, p.w.eval("WELCOME_TEXT").en.title);
  check("no Ukrainian left in the English interface", uiText().filter((x) => CYR.test(x)), []);
  check("English texts have no dashes or quotes", uiText().filter((x) => DASHQ.test(x)), []);

  p.bridge(UPD("2.4.0")); await wait();
  check("update card buttons follow", buttons(), [p.tr("updGo"), p.tr("skip")]);
  truthy("update card has no Ukrainian", !CYR.test(p.card().textContent) && !DASHQ.test(p.card().textContent));
  p.bridge({ type: "outdated", running: "1", expected: "2" }); await wait();
  truthy("re-run card has no Ukrainian", !CYR.test(p.card().textContent));
  truthy("re-run state label follows", p.label().includes(p.tr("stRerun")));

  p.type("/cl"); await wait();
  truthy("slash command hint in English", !CYR.test(p.$("#cCmds .cmd span").textContent));
  p.type("");

  p.$("#setbtn").click();
  p.choose("lang", "uk");
  await wait();
  check("back to Ukrainian at once", [html.lang, p.$("#cInput").placeholder, p.label().startsWith(p.tr("stRerun"))], ["uk", p.tr("placeholder"), true]);
  check("card buttons follow back", buttons(), [p.tr("rerunGo"), p.tr("skip")]);
  check("Ukrainian goes to the bridge", lastLoad().lang, "uk");
  p.close();

  // On start code.js sends the stored language: the UI switches without sending it back.
  const r = await open();
  const n = r.posted.length;
  r.pm({ type: "settings", scale: 1, theme: "system", lang: "en" }); await wait();
  check("restored English", [r.d.documentElement.lang, r.$("#cInput").placeholder], ["en", r.tr("placeholder")]);
  check("restoring does not send the settings back", r.posted.slice(n).filter((m) => m && m.type === "settings"), []);
  check("restored language reaches the bridge", r.sent.filter((m) => m.type === "chat-load").pop().lang, "en");
  r.pm({ type: "settings", scale: 1, theme: "system" }); await wait();
  check("old settings without lang give Ukrainian", r.d.documentElement.lang, "uk");
  r.pm({ type: "settings", lang: "fr" }); await wait();
  check("a bad language gives Ukrainian", r.d.documentElement.lang, "uk");
  check("no script errors in language", [...p.errors, ...r.errors], []);
  r.close();
}

// ─── update: the bar states and the card in the chat ──────────────────────

const UPD = (latest, current = "2.3.0") => ({ type: "update", latest, current,
  url: latest ? `https://github.com/denysosadchyi/figmosha2/releases/tag/v${latest}` : undefined });

async function updateBar() {
  const p = await open({ mode: "bar" });
  const status = () => p.$("#status").textContent;
  const bar = (v) => p.tr("updateBar", { v });
  check("no update: no Update button", p.d.body.classList.contains("update"), false);
  p.bridge(UPD("2.4.0"));
  await wait();
  truthy("update: the bar has the update state", p.d.body.classList.contains("update"));
  check("update: the bar names the version", status(), bar("2.4.0"));
  p.$("#update").click();
  check("Update opens the release page", p.posted.filter((m) => m && m.type === "open-url").pop(),
        { type: "open-url", url: "https://github.com/denysosadchyi/figmosha2/releases/tag/v2.4.0" });
  check("no card in bar mode", p.card(), null);

  p.bridge({ type: "outdated", running: "2026-01-01.1", expected: "2026-11-21.9" });
  await wait();
  truthy("stale plugin beats update", p.d.body.classList.contains("outdated"));
  check("stale: the bar asks for a re-run", status(), p.tr("rerunBar"));
  check("stale: no Update button", p.d.body.classList.contains("update"), false);

  p.ws().close();
  await until(() => p.sockets.length === 2 && p.ws().readyState === 1);
  await wait(5);
  check("a new bridge connection forgets both until told again",
        [p.d.body.classList.contains("outdated"), p.d.body.classList.contains("update")], [false, false]);
  p.bridge(UPD("2.4.0"));
  await wait();
  p.bridge(UPD(null));
  await wait();
  check("a withdrawn release clears the offer", p.d.body.classList.contains("update"), false);
  check("no script errors", p.errors, []);
  p.close();
}

async function updateCard() {
  const p = await open({ history: [msg(1, "claude", "привіт")] });
  check("no update: no card", p.card(), null);
  p.bridge(UPD("2.4.0"));
  await wait();
  const c = p.card();
  truthy("update in chat mode: a card is shown", c);
  truthy("the card names the new version", /2\.4\.0/.test(c && c.textContent));
  check("the card is after the messages", p.d.querySelector("#cMsgs").lastElementChild === c, true);
  truthy("user text has no dashes or quotes", c && !/[—–"«»“”„']/.test(c.textContent));

  c.querySelector("[data-n=go]").click();
  check("Update in the card does what the bar button does", p.posted.filter((m) => m && m.type === "open-url").pop(),
        { type: "open-url", url: "https://github.com/denysosadchyi/figmosha2/releases/tag/v2.4.0" });
  truthy("the card stays after Update", p.card());

  p.card().querySelector("[data-n=skip]").click();
  await wait();
  check("Skip stores this version", p.posted.filter((m) => m && m.type === "notice-skip").pop(),
        { type: "notice-skip", kind: "update", version: "2.4.0" });
  check("Skip hides the card", p.card(), null);
  check("Skip clears the header offer too", p.d.body.classList.contains("update"), false);
  check("the header shows the file name again", p.$("#status").textContent, "Banner");
  p.bridge(UPD("2.4.0"));
  await wait();
  check("the same version again: still hidden", p.card(), null);
  p.bridge(UPD("2.5.0"));
  await wait();
  truthy("a newer version shows again", p.card() && /2\.5\.0/.test(p.card().textContent));
  truthy("a newer version: the header offers it again", p.d.body.classList.contains("update"));
  check("a newer version: the header names it", p.$("#status").textContent, p.tr("updateBar", { v: "2.5.0" }));
  p.push({ kind: "message", message: msg(2, "claude", "ще") });
  await wait();
  check("the card stays last after a new message", p.d.querySelector("#cMsgs").lastElementChild === p.card(), true);
  p.close();

  // A new window: code.js tells the UI what was skipped before.
  const q = await open();
  q.pm({ type: "notice-skips", update: "2.4.0", rerun: null });
  q.bridge(UPD("2.4.0"));
  await wait();
  check("skip persists: no card for that version", q.card(), null);
  check("skip persists: no header offer for that version", [q.d.body.classList.contains("update"), q.$("#status").textContent], [false, "Banner"]);
  q.pm({ type: "ui-mode", mode: "bar" }); await wait();
  check("skip persists in bar mode too", [q.d.body.classList.contains("update"), q.$("#status").textContent], [false, "Banner"]);
  q.pm({ type: "ui-mode", mode: "chat" }); await wait();
  truthy("the empty chat still shows the hint", q.$("#cMsgs .hint"));
  q.bridge(UPD("2.4.1"));
  await wait();
  truthy("a newer version than the skipped one shows", q.card());
  truthy("the card sits under the hint in an empty chat", q.$("#cMsgs .hint"));
  q.close();

  // Skips that arrive after the update still hide the card.
  const r = await open();
  r.bridge(UPD("2.4.0"));
  await wait();
  truthy("card before code.js answers", r.card());
  r.pm({ type: "notice-skips", update: "2.4.0", rerun: null });
  await wait();
  check("late skips hide it", r.card(), null);
  check("late skips clear the header offer", [r.d.body.classList.contains("update"), r.$("#status").textContent], [false, "Banner"]);
  r.close();
}

async function rerunCard() {
  const p = await open();
  p.bridge(UPD("2.4.0"));
  p.bridge({ type: "outdated", running: "2026-01-01.1", expected: "2026-11-21.9" });
  await wait();
  const c = p.card();
  truthy("stale plugin: a re-run card", c && c.dataset.kind === "rerun");
  check("only one card", p.d.querySelectorAll("#cMsgs .notice").length, 1);
  truthy("user text has no dashes or quotes", c && !/[—–"«»“”„']/.test(c.textContent));
  c.querySelector("[data-n=go]").click();
  check("Restart asks code.js to close the plugin", p.posted.filter((m) => m && m.type === "rerun").length, 1);
  p.card().querySelector("[data-n=skip]").click();
  await wait();
  check("skip stores the plugin build", p.posted.filter((m) => m && m.type === "notice-skip").pop(),
        { type: "notice-skip", kind: "rerun", version: "2026-11-21.9" });
  truthy("after the re-run card is skipped, the update card shows", p.card() && p.card().dataset.kind === "update");
  p.close();

  const q = await open();
  q.pm({ type: "notice-skips", update: null, rerun: "2026-11-21.9" });
  q.bridge({ type: "outdated", running: "2026-01-01.1", expected: "2026-11-21.9" });
  await wait();
  check("skipped build: no re-run card", q.card(), null);
  q.bridge({ type: "outdated", running: "2026-01-01.1", expected: "2026-11-22.1" });
  await wait();
  truthy("a newer build shows it again", q.card());
  check("no script errors", q.errors, []);
  q.close();
}

async function slashCommands() {
  const commands = [{ name: "clear", aliases: ["new"] }, { name: "compact", hint: "<instructions>" }, { name: "context" },
                    { name: "my-skill", description: "Does a thing. More." }];
  const p = await open({ commands });
  const pop = p.$("#cCmds");
  const items = () => [...p.d.querySelectorAll("#cCmds .cmd")];
  const names = () => items().map((x) => x.dataset.cmd);
  const sel = () => { const x = p.$("#cCmds .cmd[aria-selected=true]"); return x && x.dataset.cmd; };
  check("no popup at the start", pop.hidden, true);
  p.type("/");
  check("a slash opens the popup", pop.hidden, false);
  check("it lists the bridge's commands", names(), ["clear", "compact", "context", "my-skill"]);
  check("the first one is selected", sel(), "clear");
  truthy("an unknown one shows Claude's description", items()[3].textContent.includes("Does a thing."));
  check("the input points at the list", p.input.getAttribute("aria-expanded"), "true");

  p.type("/co");
  check("typing filters the list", names(), ["compact", "context"]);
  check("the selection goes back to the top", sel(), "compact");
  p.key("ArrowDown");
  check("arrow down moves the selection", sel(), "context");
  p.key("ArrowDown");
  check("and wraps around", sel(), "compact");
  p.key("ArrowUp");
  check("arrow up wraps back", sel(), "context");
  const ev = p.key("Enter");
  check("Enter picks the command", p.input.value, "/context ");
  truthy("Enter on the popup is taken by it", ev.defaultPrevented);
  check("and does not send", p.chatSends().length, 0);
  check("the popup closes after a pick", pop.hidden, true);

  p.type("/cl");
  p.key("Tab");
  check("Tab picks too", p.input.value, "/clear ");

  p.type("/m");
  items()[0].dispatchEvent(new p.w.MouseEvent("mousedown", { bubbles: true, cancelable: true }));
  check("a click picks", p.input.value, "/my-skill ");
  check("the input keeps the focus", p.d.activeElement, p.input);

  p.type("/c");
  p.key("Escape");
  check("Esc closes the popup", pop.hidden, true);
  check("Esc keeps the text", p.input.value, "/c");
  p.type("/co");
  check("typing again opens it again", pop.hidden, false);

  p.type("/clear");
  p.enter();
  check("the full name and Enter send the command", p.chatSends().map((m) => m.text), ["/clear"]);
  check("the popup is closed after sending", pop.hidden, true);

  p.type("/zzz");
  check("no popup for an unknown name", pop.hidden, true);
  p.enter();
  check("an unknown command is sent as is", p.chatSends().pop().text, "/zzz");

  p.type("/c");
  p.push({ kind: "commands", commands: [{ name: "cost" }] });
  await wait();
  check("a commands event updates the open popup", names(), ["cost"]);
  p.type("");
  check("an empty input closes it", pop.hidden, true);
  check("no script errors", p.errors, []);
  p.close();

  const f = await open();
  f.type("/");
  truthy("without a list from the bridge a built-in one is used", [...f.d.querySelectorAll("#cCmds .cmd")].some((x) => x.dataset.cmd === "compact"));
  f.close();

  const n = await open({ history: [msg(1, "me", "/clear"), msg(2, "system", "Почав нову розмову."), msg(3, "system", "Збій", { error: true })] });
  const sys = [...n.d.querySelectorAll("#cMsgs .msg.system")];
  check("a bridge note is not drawn as an error", [sys[0].classList.contains("info"), sys[1].classList.contains("info")], [true, false]);
  n.close();
}

async function safety() {
  const p = await open({ history: [msg(1, "claude", "<img src=x onerror=alert(1)> **ок**")] });
  check("Claude's text cannot inject HTML", p.$("#cMsgs img"), null);
  check("markdown bold still works", p.$("#cMsgs .msg b").textContent, "ок");
  const t = await open({ history: [msg(1, "claude", "### Skills\n| Skill | Tokens |\n|---|---:|\n| a | 12 |")] });
  truthy("a heading renders as a heading", t.$("#cMsgs .msg .mh[role=heading]"));
  truthy("a table renders inside a scroll box", t.$("#cMsgs .msg .mtable > table td"));
  check("table cells", [...t.d.querySelectorAll("#cMsgs .msg td")].map((x) => x.textContent), ["a", "12"]);

  t.close();
  check("no script errors in the whole run", p.errors, []);
  p.close();
}

// ─── chat edges ───────────────────────────────────────────────────────────

async function composeKeys() {
  const p = await open();
  p.type("ні");
  p.enter(p.input, { isComposing: true });
  check("Enter while an IME composes does not send", p.chatSends().length, 0);
  check("the composed text stays", p.input.value, "ні");
  p.type("  з пробілами  ");
  p.enter();
  check("text is trimmed before send", p.chatSends().pop(), { type: "chat-send", text: "з пробілами" });

  // A refused send does not overwrite what the user typed since.
  p.type("нове");
  p.push({ kind: "refused" });
  await wait();
  check("refused: new text in the input is kept", p.input.value, "нове");
  check("refused: the toast is shown", p.toast(), p.tr("notSent"));
  check("no script errors", p.errors, []);
  p.close();
}

async function markdown() {
  const p = await open({ history: [msg(1, "claude",
    "Дивись https://figma.com/file/abc. Далі `**не жирне**` і **жирне**\nрядок 2\n```\nif (a < b) <x>\n```\nкінець")] });
  const m = p.$("#cMsgs .msg.claude");
  const a = m.querySelector("a");
  check("a link is drawn without the dot after it", a && a.getAttribute("href"), "https://figma.com/file/abc");
  check("a link opens in a new tab safely", a && [a.getAttribute("target"), a.getAttribute("rel")], ["_blank", "noopener"]);
  check("code span keeps the stars as text", m.querySelector("code").textContent, "**не жирне**");
  check("bold outside the code span", [...m.querySelectorAll("b")].map((b) => b.textContent), ["жирне"]);
  truthy("a new line becomes a line break", m.querySelector("br"));
  check("a code block keeps its text escaped", m.querySelector("pre").textContent, "if (a < b) <x>");
  check("nothing from the code block becomes a tag", m.querySelectorAll("pre *").length, 0);
  check("text after the code block is kept", /кінець$/.test(m.textContent), true);

  // My own text is drawn the same safe way.
  p.push({ kind: "message", message: msg(2, "me", "<b>не тег</b>") });
  await wait();
  check("my text is escaped", p.rows()[1].querySelector(".msg.me").textContent, "<b>не тег</b>");
  check("no tags from my text", p.rows()[1].querySelectorAll(".msg.me b").length, 0);
  check("no script errors", p.errors, []);
  p.close();
}

async function streaming() {
  const p = await open({ history: [msg(1, "me", "Опиши")] });
  p.push({ kind: "busy", busy: true });
  p.push({ kind: "delta", text: "Ось **код**: `x`" });
  await wait();
  const s = p.$("#cMsgs .msg.stream");
  check("the stream is drawn with markdown", [s.querySelector("b").textContent, s.querySelector("code").textContent], ["код", "x"]);
  check("the stream sits last, as Claude's row", p.d.querySelector("#cMsgs").lastElementChild.className, "mrow claude");
  check("stream without steps says writing", p.$("#cStep").textContent, p.tr("writing") + "…");

  // My message during the stream keeps the stream going.
  p.push({ kind: "message", message: msg(2, "me", "ще одне") });
  await wait();
  check("my message does not end the stream", p.$("#cMsgs .msg.stream") && p.$("#cMsgs .msg.stream").textContent, "Ось код: x");
  check("the stream stays after my message", p.d.querySelector("#cMsgs").lastElementChild.querySelector(".stream") !== null, true);

  // Idle without a final message clears the stream (a stop).
  p.push({ kind: "busy", busy: false });
  await wait();
  check("idle clears the stream", p.d.querySelectorAll(".msg.stream").length, 0);

  // A message with the same id replaces the old one in place.
  p.push({ kind: "message", message: msg(2, "me", "виправлено") });
  await wait();
  check("same id replaces the row", [p.rows().length, p.rows()[1].querySelector(".msg").textContent], [2, "виправлено"]);
  p.close();
}

async function typingState() {
  // In the bar the chat is not drawn, but the icon knows Claude works.
  const b = await open({ mode: "bar" });
  b.push({ kind: "busy", busy: true });
  await wait();
  truthy("bar mode: busy sets cbusy on the body", b.d.body.classList.contains("cbusy"));
  check("bar mode: the chat list is not drawn", b.$("#cMsgs").innerHTML, "");
  b.push({ kind: "busy", busy: false });
  await wait();
  check("bar mode: idle clears cbusy", b.d.body.classList.contains("cbusy"), false);
  b.push({ kind: "message", message: msg(1, "me", "моє") });
  await wait();
  check("bar mode: my own message does not mark unread", b.d.body.classList.contains("unread"), false);
  b.push({ kind: "message", message: msg(2, "system", "Збій", { error: true }) });
  await wait();
  truthy("bar mode: a system note marks unread", b.d.body.classList.contains("unread"));
  b.close();

  // In the chat, a reply does not mark unread.
  const c = await open();
  c.push({ kind: "message", message: msg(1, "claude", "відповідь") });
  await wait();
  check("chat mode: a reply does not mark unread", c.d.body.classList.contains("unread"), false);
  // A reply does not move the focus; opening the chat puts it in the input.
  c.input.blur();
  c.pm({ type: "ui-mode", mode: "bar" });
  c.pm({ type: "ui-mode", mode: "chat" });
  await wait();
  same("opening the chat puts the focus in the input", c.d.activeElement, c.input);
  c.close();

  // The time counts in minutes after 60 seconds; the last of several steps is shown.
  const now = Math.floor(Date.now() / 1000);
  const r = await open({ busy: true, activity: [{ t: now - 130, text: "Крок 1" }, { t: now - 10, text: "Крок 2" }] });
  check("time from the first step, in minutes", r.$("#cTime").textContent, r.tr("min", { n: 2 }));
  check("the last step is shown", r.$("#cStep").textContent, "Крок 2…");
  r.close();
}

async function photoRemoval() {
  const p = await open();
  // jsdom always reads "" from a file input, so give it a value that shows
  // whether the change handler really cleared it.
  Object.defineProperty(p.$("#cFile"), "value", {
    configurable: true, get() { return this._v === undefined ? "C:\\fakepath\\c.png" : this._v; }, set(v) { this._v = v; },
  });
  check("the fake picker value starts set", p.$("#cFile").value, "C:\\fakepath\\c.png");
  p.pick([p.file("a.png", "image/png"), p.file("b.png", "image/png"), p.file("c.png", "image/png")]);
  await until(() => p.thumbSrcs().length === 3);
  const [a, , c] = p.thumbSrcs();
  check("three photos, the box is shown", [p.thumbSrcs().length, p.$("#cAtt").hidden], [3, false]);
  p.d.querySelectorAll("#cAtt .th button")[1].click();
  check("the x removes that photo, not another", p.thumbSrcs(), [a, c]);
  p.d.querySelectorAll("#cAtt .th button")[0].click();
  p.d.querySelectorAll("#cAtt .th button")[0].click();
  check("no photos left: the box hides", [p.thumbSrcs().length, p.$("#cAtt").hidden], [0, true]);
  check("the picker is reset so the same file can be picked again", p.$("#cFile").value, "");

  // Paste adds to the photos picked before; the limit counts both.
  p.pick(Array.from({ length: 6 }, (_, i) => p.file(i + ".png", "image/png")));
  await until(() => p.thumbSrcs().length === 6);
  p.paste({ files: [p.file("x.png", "image/png"), p.file("y.png", "image/png"), p.file("z.png", "image/png")] });
  await wait(100);
  check("paste adds up to 8 in all", p.thumbSrcs().length, 8);

  // Text, photos and a quote go in one message.
  p.push({ kind: "message", message: msg(1, "claude", "Готово") });
  await wait();
  p.rows()[0].querySelector(".qbtn").click();
  p.type("ось так");
  p.enter();
  const s = p.chatSends().pop();
  check("text, photos and quote go together", [s.text, s.images.length, s.quote], ["ось так", 8, 1]);
  check("after send: no photos, no quote", [p.$("#cAtt").hidden, p.$("#cQbar").hidden], [true, true]);

  // A send that fails keeps the photos for another try.
  const f = await open();
  f.paste({ files: [f.file("a.png", "image/png")] });
  await until(() => f.thumbSrcs().length === 1);
  f.ws().readyState = 3;
  f.$("#cSend").click();
  check("no bridge: the photo stays attached", f.thumbSrcs().length, 1);
  check("no script errors", [...p.errors, ...f.errors], []);
  p.close(); f.close();
}

async function quoteEdges() {
  const p = await open({ history: [
    msg(1, "me", "", { images: ["/chat/img/dTest/a.jpg"] }),
    msg(2, "me", "моє"),
    msg(3, "claude", "ок"),
  ] });
  p.input.blur();
  p.rows()[0].querySelector(".qbtn").click();
  check("quote of a photo only message says photo", p.$("#cQbar span").textContent, p.tr("photo"));
  check("quote of my message says you", p.$("#cQbar b").textContent, p.tr("you"));
  check("a photo only message has no bubble", p.rows()[0].querySelector(".msg"), null);
  same("quoting puts the focus in the input", p.d.activeElement, p.input);
  p.rows()[1].querySelector(".qbtn").click();
  check("a second quote replaces the first", p.$("#cQbar span").textContent, "моє");
  p.type("і ще");
  p.enter();
  check("the last chosen quote is sent", p.chatSends().pop().quote, 2);

  // A photo with a quote and no text: the quote is shown alone, no empty bubble.
  p.push({ kind: "message", message: msg(4, "me", "", { images: ["/chat/img/dTest/b.jpg"], quote: { id: 3, from: "claude", text: "ок" } }) });
  await wait();
  const row = p.rows()[3];
  check("photo with quote: no text bubble", row.querySelector(".msg"), null);
  check("photo with quote: the quote is shown", row.querySelector(".mq b").textContent, "Claude");
  row.querySelector(".mq").click();
  truthy("the quoted row flashes", p.rows()[2].classList.contains("flash"));
  await slow(1300);
  check("the flash goes away", p.rows()[2].classList.contains("flash"), false);

  check("each row shows its time", p.rows()[2].querySelector(".meta").textContent,
        new Date((1795280000 + 3) * 1000).toLocaleTimeString(p.tr("locale"), { hour: "2-digit", minute: "2-digit" }));
  check("no script errors", p.errors, []);
  p.close();
}

async function actionEdges() {
  const p = await open({ history: [msg(1, "claude", "Вибери", { actions: ["Так <b>", "Ні", "Може"] })] });
  check("three buttons lettered A B C", [...p.d.querySelectorAll(".act .ak")].map((x) => x.textContent), ["A", "B", "C"]);
  check("button text is escaped", p.d.querySelector(".act span:last-child").textContent, "Так <b>");
  p.ws().readyState = 3;
  p.d.querySelectorAll(".act")[1].click();
  check("no bridge: a toast says so", p.toast(), p.tr("noBridge"));
  check("no bridge: buttons work again", [...p.d.querySelectorAll(".act")].map((b) => b.disabled), [false, false, false]);
  p.ws().readyState = 1;
  p.d.querySelectorAll(".act")[0].click();
  check("the escaped text is sent as is", p.chatSends().pop().text, "Так <b>");

  // Buttons only under Claude's last reply: my newer message hides them.
  p.push({ kind: "message", message: msg(2, "me", "Так") });
  await wait();
  check("my message after the buttons hides them", p.d.querySelectorAll(".act").length, 0);
  p.close();

  // When Claude is done, the buttons come back under its last reply.
  const b = await open({ busy: true, history: [msg(1, "claude", "Далі?", { actions: ["Так"] })] });
  check("busy: no buttons", b.d.querySelectorAll(".act").length, 0);
  b.push({ kind: "busy", busy: false });
  await wait();
  check("idle: buttons are back", b.d.querySelectorAll(".act").length, 1);
  b.close();
}

async function questionEdges() {
  const q = await open({ history: [msg(5, "claude", "Питання", { questions: ["Який **колір**?", "Шрифт?"] })] });
  const fields = () => [...q.d.querySelectorAll(".qf textarea")];
  check("question text uses markdown", q.$(".qq b").textContent, "колір");
  check("progress starts at zero", [q.$(".qs-head span").textContent, q.$(".qs-head i").getAttribute("style")], [q.tr("answers", { d: 0, n: 2 }), "--p:0"]);

  // Blank and Shift+Enter do not send. The focus starts elsewhere, so the
  // check sees the blank answer pull it back to the field.
  q.input.focus();
  q.enter(fields()[0]);
  check("blank answer is not sent", q.chatSends().length, 0);
  same("blank answer keeps the focus on the field", q.d.activeElement, fields()[0]);
  const f1 = fields()[1];
  f1.value = "Inter"; f1.dispatchEvent(new q.w.Event("input", { bubbles: true }));
  q.enter(f1, { shiftKey: true });
  check("Shift+Enter in an answer does not send", q.chatSends().length, 0);

  // A draft survives a redraw of the list.
  q.push({ kind: "activity", text: "x" });
  q.push({ kind: "message", message: msg(6, "claude", "інше") });
  await wait();
  check("a draft answer survives a redraw", fields()[1].value, "Inter");

  // A failed send leaves the form open.
  q.ws().readyState = 3;
  q.d.querySelectorAll(".qf")[1].requestSubmit();
  check("no bridge: the answer is not sent", q.chatSends().length, 0);
  check("no bridge: the form is not locked", [q.d.querySelectorAll(".qf")[1].classList.contains("sending"), fields()[1].disabled], [false, false]);
  q.ws().readyState = 1;

  // The send button sends; the form locks; the focus moves to the other question.
  // The focus sits in the answered field, so only the move to the next open
  // question can bring it to the first field.
  fields()[1].focus();
  q.d.querySelectorAll(".qf")[1].querySelector(".qsend").click();
  check("the send button sends the answer", q.chatSends().pop(), { type: "chat-send", text: "Inter", answer: { msg: 5, i: 1 } });
  check("the sent form locks", [q.d.querySelectorAll(".qf")[1].classList.contains("sending"), fields()[1].disabled], [true, true]);
  q.push({ kind: "message", message: msg(7, "me", "Inter", { answer: { msg: 5, i: 1 } }) });
  await wait();
  same("after the answer lands the focus goes to the open question", q.d.activeElement, fields()[0]);
  const f0 = fields()[0];
  f0.value = "синій"; f0.dispatchEvent(new q.w.Event("input", { bubbles: true }));
  q.enter(f0);
  q.push({ kind: "message", message: msg(8, "me", "синій", { answer: { msg: 5, i: 0 } }) });
  await wait();
  check("all answered: full progress", [q.$(".qs-head span").textContent, q.$(".qs-head i").getAttribute("style")], [q.tr("answers", { d: 2, n: 2 }), "--p:1"]);
  check("all answered: no fields left", q.d.querySelectorAll(".qf").length, 0);
  check("answers are escaped text", [...q.d.querySelectorAll(".qans")].map((x) => x.textContent), ["синій", "Inter"]);
  check("no script errors", q.errors, []);
  q.close();
}

async function historyLoads() {
  const p = await open({ history: [msg(1, "me", "старе")] });
  p.push({ kind: "busy", busy: true });
  p.push({ kind: "delta", text: "пишу" });
  await wait();
  p.push({ kind: "history", busy: false, activity: [], messages: [msg(10, "claude", "нове")] });
  await wait();
  check("a new history replaces the list", p.rows().map((r) => r.dataset.mid), ["10"]);
  check("a new history ends the stream", p.d.querySelectorAll(".msg.stream").length, 0);
  check("a new history not busy hides the typing line", p.$("#cTyping").hidden, true);
  p.push({ kind: "history", busy: false, activity: [], messages: [] });
  await wait();
  truthy("an empty history shows the hint again", p.$("#cMsgs .hint"));

  // The chat is asked for once the bridge knows the file, and again on a new identity.
  const loads = () => p.sent.filter((m) => m.type === "chat-load").length;
  const n = loads();
  p.pm({ type: "identity", name: "Banner 2", docSig: DOC });
  check("a new identity asks for the chat again", loads(), n + 1);
  p.pm({ type: "identity", name: "Без підпису" });
  check("no docSig: the chat is not asked for", loads(), n + 1);
  p.close();

  // Opening the chat from the bar draws the history that came in the bar.
  const b = await open({ mode: "bar", history: [msg(1, "claude", "раніше")] });
  check("bar mode: nothing drawn yet", b.rows().length, 0);
  b.pm({ type: "ui-mode", mode: "chat" });
  await wait();
  check("chat mode: the history is drawn", b.rows().length, 1);
  b.close();
}

async function emptyStates() {
  const p = await open();
  // No chat yet: the same card as the welcome screen, not a bare line of text.
  const W = p.w.eval("WELCOME_TEXT").uk;
  check("the empty chat shows the welcome card", p.$("#cMsgs .hint h2") && p.$("#cMsgs .hint h2").textContent, W.title);
  truthy("the card has the illustration", p.$("#cMsgs .hint svg.willo"));
  check("the card has the feature rows", [...p.d.querySelectorAll("#cMsgs .hint .wrow")].map((r) => r.textContent), W.rows.map((r) => r.text));
  check("the card has no second composer", p.$("#cMsgs .hint .compose"), null);
  check("the card adds no duplicate ids", p.d.querySelectorAll("#welcomeH").length, 1);
  p.push({ kind: "busy", busy: true });
  p.push({ kind: "delta", text: "a" });
  await wait();
  check("the stream replaces the hint", p.$("#cMsgs .hint"), null);
  p.push({ kind: "busy", busy: false });
  await wait();
  truthy("no messages and no stream: the hint is back", p.$("#cMsgs .hint"));
  check("idle: the typing line is hidden", p.$("#cTyping").hidden, true);
  check("the quote bar and photo box start hidden", [p.$("#cQbar").hidden, p.$("#cAtt").hidden], [true, true]);
  p.close();
}

// The toast lasts 2.6 s; a new toast starts its time again.
async function toasts() {
  const p = await open();
  p.ws().readyState = 3;
  p.type("a"); p.enter();
  check("a toast is shown", p.toast(), p.tr("noBridge"));
  await slow(1500);
  p.$("#cStop").hidden = false;
  p.ws().readyState = 1;
  p.ws().send = () => { throw new Error("x"); };
  p.$("#cStop").click();
  check("a new toast replaces the text", p.toast(), p.tr("notStopped"));
  await slow(1500);
  check("a new toast restarts the timer", p.toast(), p.tr("notStopped"));
  await slow(1500);
  check("the toast hides after a while", p.toast(), "");
  p.close();
}

// ─── shell: socket, relay, icon states, reconnects ────────────────────────

// The hello that registers this window, and the identity request on open.
async function helloAndIdentity() {
  const p = await open({ identity: null, mode: "bar" });
  check("socket goes to the bridge plugin endpoint", p.ws().url, "ws://localhost:8787/plugin");
  check("on open the UI asks code.js for the identity", p.of("need-identity").length, 1);
  check("no identity yet: the status says connected", p.$("#status").textContent, p.tr("connected"));
  check("no identity yet: the chat is not asked for", p.sentOf("chat-load").length, 0);
  p.pm({ type: "identity", fileKey: "FK1", name: "Poster", docSig: DOC, pluginVersion: "2026-11-21.3" });
  await wait();
  check("identity sends a full hello", p.sentOf("hello").pop(),
        { type: "hello", version: "2.1", plugin: "2026-11-21.3", fileKey: "FK1", name: "Poster", docSig: DOC });
  check("identity puts the file name in the bar", p.$("#status").textContent, "Poster");
  check("identity then asks for the chat", p.sentOf("chat-load").length, 1);
  p.pm({ type: "identity" });
  await wait();
  check("an empty identity sends nulls in the hello", p.sentOf("hello").pop(),
        { type: "hello", version: "2.1", plugin: null, fileKey: null, name: null, docSig: null });
  check("an empty identity keeps the old name in the bar", p.$("#status").textContent, "Poster");
  p.close();

  // Identity before the socket is open: no hello yet, the hello comes on open.
  const q = await open({ identity: null, wsThrows: 0 });
  q.ws().readyState = 0;
  const n = q.sentOf("hello").length;
  q.pm({ type: "identity", name: "Early", docSig: DOC });
  await wait();
  check("socket not open: identity sends nothing", q.sentOf("hello").length, n);
  q.ws().readyState = 1; q.ws().onopen(); await wait();
  check("on open the stored identity is used", [q.sentOf("hello").pop().name, q.$("#status").textContent], ["Early", "Early"]);
  check("no script errors", [...p.errors, ...q.errors], []);
  q.close();
}

// Bridge to code.js: exec, abort, ping. code.js to bridge: result, error.
async function relay() {
  const p = await open({ mode: "bar" });
  p.bridge({ type: "exec", id: "abcdef123456", code: "return 1", timeout: 5000 });
  await wait(5);
  check("exec goes to code.js with its timeout", p.of("exec").pop(), { type: "exec", id: "abcdef123456", code: "return 1", timeout: 5000 });
  check("exec sets the busy state on the body", p.d.body.classList.contains("busy"), true);
  p.bridge({ type: "abort", id: "abcdef123456" });
  check("abort goes to code.js", p.of("abort").pop(), { type: "abort", id: "abcdef123456" });
  p.bridge({ type: "ping" });
  check("ping is answered with pong", p.sentOf("pong").length, 1);

  p.pm({ type: "result", id: "abcdef123456", valueJson: '{"a":[1,2]}' });
  check("a serialized result is spliced in as value", p.sent[p.sent.length - 1], { type: "result", id: "abcdef123456", value: { a: [1, 2] } });
  check("right after the result the spinner holds", p.st(), ["st-busy"]);
  await until(() => p.st()[0] === "st-ok", 1500);
  check("after the hold the icon is green again", p.st(), ["st-ok"]);
  check("and the body is not busy", p.d.body.classList.contains("busy"), false);

  p.pm({ type: "result", id: "r2", value: 5 });
  check("a plain result is sent as is", p.sent[p.sent.length - 1], { type: "result", id: "r2", value: 5 });
  p.pm({ type: "error", id: "r3", text: "boom" });
  check("an error goes to the bridge", p.sent[p.sent.length - 1], { type: "error", id: "r3", text: "boom" });
  await until(() => p.st()[0] === "st-err", 1000);
  check("after the hold an error turns the body red", p.d.body.classList.contains("err"), true);

  // Two runs at once: the spinner stays while one is still going.
  p.bridge({ type: "exec", id: "x1", code: "" });
  p.bridge({ type: "exec", id: "x2", code: "" });
  p.pm({ type: "result", id: "x1", value: 1 });
  await slow(500);
  check("one of two runs done: still busy", p.st(), ["st-busy"]);
  p.pm({ type: "result", id: "x2", value: 1 });
  await until(() => p.st()[0] === "st-ok", 3000);
  check("both done, error flash over: green", p.st(), ["st-ok"]);

  // Socket down: results are dropped, not thrown.
  p.ws().readyState = 3;
  const n = p.sent.length;
  p.pm({ type: "result", id: "late", value: 1 });
  check("socket closed: the result is dropped", p.sent.length, n);
  p.ws().readyState = 1;
  p.ws().send = () => { throw new Error("gone"); };
  p.pm({ type: "result", id: "late2", value: 1 });
  p.bridge({ type: "ping" });
  p.w.dispatchEvent(new p.w.MessageEvent("message", { data: { notPlugin: 1 } }));
  p.w.dispatchEvent(new p.w.MessageEvent("message", { data: null }));
  p.bridge({ type: "nope" });
  await wait();
  check("send that throws and stray messages: no script errors", p.errors, []);
  p.close();
}

// Icon priority and the body classes for each state.
async function iconPriority() {
  const p = await open({ mode: "bar" });
  check("connected: body is ok", p.d.body.className, "ok");
  p.bridge({ type: "peers", index: 1, total: 1 });
  await wait(5);
  check("one file: no count", [p.$("#peers").textContent, p.d.body.classList.contains("multi")], ["", false]);
  check("one file: the label has no slot", p.label(), p.tr("stOk"));
  p.bridge({ type: "peers", index: 1, total: 3 });
  const slot = p.tr("stSlot", { i: 1, n: 3 });
  check("three files: count and multi", [p.$("#peers").textContent, p.d.body.classList.contains("multi")], ["1/3", true]);
  check("three files: the label tells the slot", p.label(), p.tr("stOk") + slot);

  p.bridge({ type: "update", latest: "2.4.0", current: "2.3.0", url: "https://r" });
  p.bridge({ type: "outdated", running: "a", expected: "b" });
  check("stale beats update", [p.st(), p.d.body.className], [["st-update"], "ok outdated multi"]);
  p.pm({ type: "error", id: "e" });
  await until(() => p.st()[0] === "st-err", 1000);
  check("a failed run beats stale", [p.st(), p.label(), p.$("#status").textContent],
        [["st-err"], p.tr("stErr") + slot, "Banner"]);
  check("failed: body is err", p.d.body.className.split(" ")[0], "err");
  p.bridge({ type: "exec", id: "e2", code: "" });
  check("a running script beats a failed one", p.st(), ["st-busy"]);
  check("busy: the bar keeps the file name", p.$("#status").textContent, "Banner");

  p.ws().onerror();
  check("socket error: the count is gone", [p.$("#peers").textContent, p.d.body.classList.contains("multi")], ["", false]);
  check("socket error: running scripts are forgotten", p.d.body.classList.contains("busy"), false);
  check("socket error: plain error label", p.label(), p.tr("stLinkErr"));
  check("socket error: no update offer in the bar", p.d.body.classList.contains("update"), false);
  p.ws().onopen();   // the socket comes back: the old run and the old count must not return
  check("after the error and a new open: not busy, no count",
        [p.d.body.classList.contains("busy"), p.$("#peers").textContent], [false, ""]);
  p.ws().onclose();
  check("disconnect clears the count", [p.$("#peers").textContent, p.d.body.classList.contains("multi")], ["", false]);
  check("no script errors", p.errors, []);
  p.close();
}

// The Update button in the bar.
async function updateButton() {
  const p = await open({ mode: "bar" });
  p.$("#update").click();
  check("no release: the button opens nothing", p.of("open-url"), []);
  p.bridge({ type: "update", latest: "2.4.0", current: "2.3.0", url: "https://r/2.4.0" });
  p.bridge({ type: "update", latest: null });
  p.$("#update").click();
  check("withdrawn release: the button opens nothing", p.of("open-url"), []);
  p.close();

  // A card without the current version still shows; the version is text, not markup.
  const q = await open();
  q.bridge({ type: "update", latest: "3.0.0", url: "https://r" });
  await wait();
  const c = q.card();
  truthy("card without a current version", c && c.dataset.kind === "update");
  q.bridge({ type: "update", latest: "<b>9</b>", current: "1", url: "https://r" });
  await wait();
  check("the version is escaped in the card", q.card().querySelectorAll(".ntext b b").length, 0);
  truthy("the escaped version is shown as text", /<b>9<\/b>/.test(q.card().textContent));
  q.close();

  // Re-run card text and order in bar mode then chat mode.
  const r = await open({ mode: "bar" });
  r.bridge({ type: "outdated", running: "a", expected: "b2" });
  await wait();
  check("bar mode: no re-run card drawn", r.card(), null);
  r.pm({ type: "ui-mode", mode: "chat" });
  await wait();
  truthy("opening the chat draws the re-run card", r.card() && r.card().dataset.kind === "rerun");
  r.ws().close();
  await until(() => r.sockets.length === 2 && r.ws().readyState === 1, 3000);
  await wait();
  check("a new bridge connection clears the card", r.card(), null);
  r.close();
}

// Reconnects: timer, a socket that cannot be made, visibility.
async function reconnects() {
  const p = await open({ mode: "bar", wsThrows: 1, identity: null });
  check("socket cannot be made: status waits", p.$("#status").textContent, p.tr("connecting"));
  check("socket cannot be made: connecting icon", p.st(), ["st-conn"]);
  await until(() => p.sockets.length === 1 && p.ws().readyState === 1, 3000);
  check("after 2 s it tries again and connects", [p.made(), p.st()], [2, ["st-ok"]]);

  p.ws().close();
  check("close: status says reconnecting", p.$("#status").textContent, p.tr("reconnecting"));
  check("close: no new socket at once", p.sockets.length, 1);
  await until(() => p.sockets.length === 2 && p.ws().readyState === 1, 3000);
  check("close: a new socket after 2 s", p.sockets.length, 2);
  check("the new socket says hello", p.sentOf("hello").length >= 2, true);

  const vis = (v) => { Object.defineProperty(p.d, "visibilityState", { value: v, configurable: true }); p.d.dispatchEvent(new p.w.Event("visibilitychange")); };
  vis("visible");
  check("visible with an open socket: no new socket", p.sockets.length, 2);
  p.ws().readyState = 3;
  vis("hidden");
  check("hidden: no reconnect", p.sockets.length, 2);
  vis("visible");
  check("visible with a closed socket: reconnect at once", p.sockets.length, 3);
  p.ws().readyState = 2;
  vis("visible");
  check("visible with a closing socket: reconnect at once", p.sockets.length, 4);
  // A pending timer from a close is cancelled by a visible reconnect.
  await until(() => p.ws().readyState === 1);
  p.ws().onclose();
  vis("visible");   // readyState is 1 in the fake: no reconnect; force one:
  p.ws().readyState = 3; vis("visible");
  const after = p.sockets.length;
  await slow(2200);
  check("the close timer is cancelled by the visible reconnect", p.sockets.length, after);
  check("no script errors", p.errors, []);
  p.close();
}

// The old socket's close lands after a visible reconnect made a new one.
async function staleCloseAfterVisible() {
  const p = await open({ mode: "bar" });
  const old = p.ws();
  old.readyState = 2;   // closing
  Object.defineProperty(p.d, "visibilityState", { value: "visible", configurable: true });
  p.d.dispatchEvent(new p.w.Event("visibilitychange"));
  await until(() => p.ws() !== old && p.ws().readyState === 1);
  old.readyState = 3;   // the browser now finishes closing the old one: error, then close
  old.onerror();
  check("a late error of the old socket keeps the status", p.st(), ["st-ok"]);
  old.onclose();
  check("a late close of the old socket keeps the status", p.st(), ["st-ok"]);
  await slow(2200);
  check("a late close of the old socket opens no extra socket", p.sockets.length, 2);
  // The new socket still reconnects after its own close.
  p.ws().close();
  check("the new socket's close says reconnecting", p.st(), ["st-conn"]);
  await until(() => p.sockets.length === 3 && p.ws().readyState === 1, 3000);
  check("the new socket's close reconnects after 2 s", [p.sockets.length, p.st()], [3, ["st-ok"]]);
  check("no script errors", p.errors, []);
  p.close();
}

// Thin bar and chat toggle edges.
async function modes() {
  const p = await open({ mode: "bar" });
  p.pm({ type: "ui-mode", mode: "weird" });
  check("an unknown mode is the bar", p.d.body.classList.contains("chatmode"), false);
  p.$("#chatbtn").click();
  check("the chat icon still asks for the chat", p.of("ui-mode").pop(), { type: "ui-mode", mode: "chat" });
  check("the click alone does not switch: code.js answers", p.d.body.classList.contains("chatmode"), false);
  p.input.blur();
  check("in the bar the input has no focus", p.d.activeElement.id, "");
  p.pm({ type: "ui-mode", mode: "chat" });
  check("chat mode on the body", p.d.body.classList.contains("chatmode"), true);
  check("chat mode puts the focus in the input", p.d.activeElement.id, "cInput");
  p.$("#chatbtn").click();
  check("in chat the icon asks for the bar", p.of("ui-mode").pop(), { type: "ui-mode", mode: "bar" });
  p.pm({ type: "ui-mode", mode: "bar" });
  check("back in the bar", p.d.body.classList.contains("chatmode"), false);
  p.close();
}

// Settings page edges: OS theme, 90 percent, focus after back.
async function settingsEdges() {
  const p = await open({ darkMedia: true });
  const html = p.d.documentElement;
  truthy("system theme follows a dark OS when Figma gives no class", html.classList.contains("dark"));
  html.classList.add("figma-light"); await wait();
  check("a figma-light class beats the dark OS", html.classList.contains("dark"), false);
  p.$("#setbtn").click();
  p.choose("scale", "0.9");
  check("90 percent is sent and set", [p.of("settings").pop().scale, html.style.getPropertyValue("--ui-scale")], [0.9, "0.9"]);
  const n = p.of("settings").length;
  p.$("#settings h3").click();
  check("a tap on a label sends nothing", p.of("settings").length, n);
  p.input.blur();
  check("before back the input has no focus", p.d.activeElement.id, "");
  p.$("#sBack").click();
  check("back closes the settings page", html.classList.contains("settings-open"), false);
  check("back puts the focus in the input", p.d.activeElement.id, "cInput");
  check("the picked scale is shown", p.chosen("scale"), "0.9");
  check("the system theme stays shown", p.chosen("theme"), "system");
  check("no script errors", p.errors, []);
  p.close();

  const q = await open({ darkMedia: false });
  check("system theme on a light OS is light", q.d.documentElement.classList.contains("dark"), false);
  q.pm({ type: "settings", scale: "1.1", theme: "light" });
  check("a scale as a string is not trusted", q.d.documentElement.style.getPropertyValue("--ui-scale"), "1");
  q.close();
}

// Slash popup: aria state and keys.
async function slashAria() {
  const p = await open();
  p.push({ kind: "commands", commands: [{ name: "clear" }, { name: "compact" }, { name: "context" }] });
  await wait();
  const pop = p.$("#cCmds");
  p.type("/c");
  check("the active option is named on the input", p.input.getAttribute("aria-activedescendant"), "cCmd0");
  p.key("ArrowUp");
  check("arrow up from the top wraps to the end", p.input.getAttribute("aria-activedescendant"), "cCmd2");
  truthy("the selected option is scrolled into view", p.$("#cCmd2")._scrolledTo);
  check("only one option is selected", p.d.querySelectorAll('#cCmds [aria-selected="true"]').length, 1);
  const ev = p.key("Enter", p.input, { shiftKey: true });
  check("Shift+Enter is not taken by the popup", [ev.defaultPrevented, pop.hidden], [false, false]);
  const ime = p.key("ArrowDown", p.input, { isComposing: true });

  check("keys while an IME composes are left alone", [ime.defaultPrevented, p.input.getAttribute("aria-activedescendant")], [false, "cCmd2"]);
  p.key("Escape");
  check("Esc: the input is no longer expanded", [p.input.getAttribute("aria-expanded"), p.input.hasAttribute("aria-activedescendant")], ["false", false]);
  p.type("/c");
  check("the same text after Esc keeps it closed", pop.hidden, true);
  p.type("/co");
  check("new text opens it, selection at the top", [pop.hidden, p.input.getAttribute("aria-activedescendant")], [false, "cCmd0"]);
  p.type("/COMPACT");
  const send = p.key("Enter");
  check("a full name in capitals is sent, not picked", [send.defaultPrevented, p.sentOf("chat-send").pop().text], [true, "/COMPACT"]);
  p.type("hello /c");
  check("a slash not at the start opens nothing", pop.hidden, true);
  const tab = p.key("Tab");
  check("Tab with no popup is not taken", tab.defaultPrevented, false);
  check("no script errors", p.errors, []);
  p.close();
}

// ─── agent: provider, model and effort (Claude Code or Codex) ─────────────
//
// Interface (the bridge holds ONE agent setting for all files, in DATA_DIR/settings.json):
//
//   bridge -> plugin, inside the chat event ({type: "chat", doc, event}):
//     history   gets  providers, settings  (and commands for the active provider)
//     {kind: "providers", providers, settings, commands?}   after a set, a re-detect, or a change
//                                                           made on another file
//     providers: absent = not known yet (an older bridge): the chat is drawn as before
//                [] = no agent found
//                [{id: "claude"|"codex", name, model (its default), models: [{id, name, efforts: [..]}]}]
//                Only detected providers are in the list. The model lists come from here only.
//     settings:  {provider, model, effort}, e.g. {provider: "claude", model: "opus", effort: "medium"}
//     commands:  when present it replaces the list (as in the commands event)
//
//   plugin -> bridge, over the socket:
//     {type: "chat-settings", provider?, model?, effort?}   a tap on the settings page; it carries at
//                                                          least the field that changed. The bridge
//                                                          answers with a providers event.
//     {type: "chat-detect"}                                Check again: find the agents again
//   These are bridge settings: they never go to code.js (no extra {type: "settings"} post).
//
//   Settings page (#settings), three dropdowns after the scale, theme and lang dropdowns, one row
//   each (see settings()). Each is a native <select> with aria-labelledby pointing at its heading
//   (texts in I18N uk and en):
//     select#sAgent    options claude and codex, both always there; one that is not in providers
//                      is a disabled option, and a change to it sends nothing
//     select#sModel    options are the models of the selected provider, in the bridge's order,
//                      the label shows the model name
//     select#sEffort   options are the efforts of the selected model
//     The selected option follows settings. A row with no options is hidden, and the agent row
//     is hidden while providers is unknown (an older bridge).
//     A change event on a select sends exactly one chat-settings with that field. A re-render from
//     a bridge event sends nothing and keeps the keyboard focus on the same select.

//   No agent (providers = []):
//     #cCompose is not shown; #cNoAgent is shown where the composer would be (the chat, or the
//     welcome screen while it is open) instead. It follows a language switch with no new event.
//     It names Claude Code and Codex,
//     has one install command for each (a <code> element each) and a button #cRecheck that sends
//     chat-detect (toast noBridge when there is no socket). A providers event with a non empty list
//     brings the composer back and hides the card.
//
//   Slash popup: with provider codex only /clear works, so the popup shows only clear (its aliases
//   reset and new still match), whatever list is cached, and never the Claude fallback list.
const EFF = ["low", "medium", "high", "xhigh", "max"];
const CLAUDE = { id: "claude", name: "Claude Code", model: "opus", models: [
  { id: "opus", name: "Opus", efforts: EFF }, { id: "sonnet", name: "Sonnet", efforts: EFF },
  { id: "haiku", name: "Haiku", efforts: ["low", "medium", "high"] }] };
const CODEX = { id: "codex", name: "Codex", model: "gpt-5.6-sol", models: [
  { id: "gpt-6-astra", name: "GPT-6 Astra", efforts: EFF.concat(["ultra"]) },
  { id: "gpt-5.6-sol", name: "GPT-5.6 Sol", efforts: ["low", "medium", "high"] }] };
const DEF = { provider: "claude", model: "opus", effort: "medium" };
const PROV = (providers, settings = DEF, extra = {}) => Object.assign({ kind: "providers", providers, settings }, extra);
// Shown: no hidden attribute and no display:none on it or any parent (jsdom runs the page CSS).
function visible(el) {
  if (!el) return false;
  for (let e = el; e && e.nodeType === 1; e = e.parentElement) {
    if (e.hidden || e.ownerDocument.defaultView.getComputedStyle(e).display === "none") return false;
  }
  return true;
}
const off = (b) => !!b && (b.disabled || b.getAttribute("aria-disabled") === "true");
const SEL = { provider: "sAgent", model: "sModel", "effort": "sEffort" };
function agentUi(p) {
  const sel = (k) => p.$(`#settings select#${SEL[k]}`);
  const opts = (k) => sel(k) ? [...sel(k).options].filter((o) => o.value) : [];   // a placeholder has no value
  return {
    sel,
    ids: (k) => opts(k).map((o) => o.value),
    btn: (k, v) => opts(k).find((o) => o.value === v) || null,
    checked: (k) => opts(k).filter((o) => o.selected).map((o) => o.value),
    on: (k) => opts(k).filter((o) => !o.disabled).map((o) => o.value),
    // What a user does: pick an option, the select fires change.
    pick: (k, v) => {
      const s = sel(k); if (!s) throw new Error("no select " + k);
      s.value = v; s.dispatchEvent(new p.d.defaultView.Event("change", { bubbles: true }));
    },
    lastSet: () => p.sentOf("chat-settings").pop(),
  };
}
const has = (obj, part) => !!obj && Object.keys(part).every((k) => obj[k] === part[k]);
const tap = (el) => { if (el) el.click(); else throw new Error("no such button"); };

// The agent part of the settings page: provider, model and effort come from the bridge.
async function agentSettings() {
  const p = await open({ agents: { providers: [CLAUDE], settings: DEF } });
  const a = agentUi(p);
  p.$("#setbtn").click();
  check("both providers are offered", a.ids("provider").sort(), ["claude", "codex"]);
  check("only the detected one can be picked", a.on("provider"), ["claude"]);
  truthy("each setting is a select", ["provider", "model", "effort"].every((k) => a.sel(k) && a.sel(k).tagName === "SELECT"));
  truthy("each select is named by its heading", ["provider", "model", "effort"].every((k) => {
    const h = a.sel(k) && p.d.getElementById(a.sel(k).getAttribute("aria-labelledby"));
    return h && h.tagName === "H3" && h.textContent.trim();
  }));
  check("defaults are shown: Claude, Opus, medium", [a.checked("provider"), a.checked("model"), a.checked("effort")],
        [["claude"], ["opus"], ["medium"]]);
  check("the models are the bridge's list, in its order", a.ids("model"), ["opus", "sonnet", "haiku"]);
  check("the efforts are the ones of the model", a.ids("effort"), EFF);
  truthy("a model shows its name", a.btn("model", "opus") && a.btn("model", "opus").textContent.includes("Opus"));
  check("opening the page sends nothing", p.sentOf("chat-settings").length, 0);

  const codeJs = p.of("settings").length;
  a.pick("provider", "codex");
  check("a provider that is not there sends nothing", p.sentOf("chat-settings").length, 0);
  a.pick("model", "sonnet");
  check("a model change sends one message", p.sentOf("chat-settings").length, 1);
  truthy("a model change goes to the bridge", has(a.lastSet(), { type: "chat-settings", model: "sonnet" }));
  a.pick("effort", "high");
  check("an effort change sends one message", p.sentOf("chat-settings").length, 2);
  truthy("an effort change goes to the bridge", has(a.lastSet(), { type: "chat-settings", "effort": "high" }));
  a.sel("effort").focus();
  check("agent settings do not go to code.js", p.of("settings").length, codeJs);

  p.push(PROV([CLAUDE, CODEX], { provider: "claude", model: "sonnet", "effort": "high" })); await wait();
  check("the bridge answer is marked", [a.checked("model"), a.checked("effort")], [["sonnet"], ["high"]]);
  same("the answer keeps the keyboard focus on the select", p.d.activeElement, a.sel("effort"));
  check("a re-render sends nothing", p.sentOf("chat-settings").length, 2);
  check("a provider found later can be picked", a.on("provider").sort(), ["claude", "codex"]);
  a.sel("model").focus();
  a.pick("provider", "codex");
  check("a provider change sends one message", p.sentOf("chat-settings").length, 3);
  truthy("a provider change goes to the bridge", has(a.lastSet(), { type: "chat-settings", provider: "codex" }));

  p.push(PROV([CLAUDE, CODEX], { provider: "codex", model: "gpt-5.6-sol", "effort": "medium" })); await wait();
  check("Codex is marked", a.checked("provider"), ["codex"]);
  same("a new model list keeps the focus on the model select", p.d.activeElement, a.sel("model"));
  check("the models follow the provider", a.ids("model"), ["gpt-6-astra", "gpt-5.6-sol"]);
  check("its default model and effort are marked", [a.checked("model"), a.checked("effort")], [["gpt-5.6-sol"], ["medium"]]);
  check("the efforts follow the model", a.ids("effort"), ["low", "medium", "high"]);
  check("still no message by itself", p.sentOf("chat-settings").length, 3);

  const NEW = Object.assign({}, CLAUDE, { models: [{ id: "nova", name: "Nova", efforts: ["low", "medium"] }] });
  p.push(PROV([NEW], { provider: "claude", model: "nova", "effort": "low" })); await wait();
  check("a new list from the bridge is drawn as is", [a.ids("model"), a.checked("model"), a.on("provider")], [["nova"], ["nova"], ["claude"]]);
  p.push(PROV([CLAUDE], DEF), "dOther"); await wait();
  check("events of another file are ignored", a.ids("model"), ["nova"]);
  p.push(PROV([{ id: "claude", name: "Claude Code", models: [] }], { provider: "claude" })); await wait();
  check("no models: the model and effort rows are hidden", [visible(a.sel("model")), visible(a.sel("effort"))], [false, false]);
  truthy("no models: the agent row stays", visible(a.sel("provider")));

  p.$("#sBack").click();
  truthy("back in the chat the composer is there", visible(p.$("#cCompose")));
  check("no script errors", p.errors, []);
  p.close();

  // History from an older bridge has no providers: the chat works as before.
  const q = await open();
  truthy("unknown providers: the composer is shown", visible(q.$("#cCompose")));
  check("unknown providers: no setup card", visible(q.$("#cNoAgent")), false);
  check("no script errors with unknown providers", q.errors, []);
  q.close();
}

// No Claude Code and no Codex: a card says what to install, Check again finds them.
async function noAgent() {
  const p = await open({ agents: { providers: [], settings: DEF } });
  const card = () => p.$("#cNoAgent");
  check("no agent: the composer is hidden", visible(p.$("#cCompose")), false);
  truthy("no agent: the card is shown", visible(card()));
  truthy("the card names both agents", card() && card().textContent.includes("Claude Code") && card().textContent.includes("Codex"));
  check("one install command for each", !!card() && card().querySelectorAll("code").length >= 2, true);
  truthy("the card has Check again", visible(p.$("#cRecheck")) && card().contains(p.$("#cRecheck")));

  tap(p.$("#cRecheck"));
  check("Check again asks the bridge to look again", p.sentOf("chat-detect").length, 1);
  p.push(PROV([])); await wait();
  truthy("still nothing found: the card stays", visible(card()) && !visible(p.$("#cCompose")));
  tap(p.$("#cRecheck"));
  check("Check again works more than once", p.sentOf("chat-detect").length, 2);

  p.$("#setbtn").click();
  check("no agent: no provider can be picked", [agentUi(p).ids("provider").length, agentUi(p).on("provider")], [2, []]);
  p.$("#sBack").click();

  p.push(PROV([CODEX], { provider: "codex", model: "gpt-5.6-sol", effort: "medium" })); await wait();
  truthy("an agent found: the composer comes back", visible(p.$("#cCompose")));
  check("an agent found: the card is gone", visible(card()), false);
  p.type("привіт"); p.enter();
  check("and a message can be sent", p.chatSends().length, 1);

  p.push(PROV([])); await wait();
  truthy("the agent is gone again: the card is back", visible(card()) && !visible(p.$("#cCompose")));
  p.ws().readyState = 3;
  tap(p.$("#cRecheck"));
  check("no socket: Check again says so", [p.toast(), p.sentOf("chat-detect").length], [p.tr("noBridge"), 2]);
  // The bridge itself is gone: that is not the no agent state.
  p.ws().onclose(); await wait(5);
  check("bridge offline: no setup card, the composer as before", [visible(card()), visible(p.$("#cCompose"))], [false, true]);
  check("no script errors", p.errors, []);
  p.close();
}

// First run in a file: the welcome screen holds the composer. With no agent it shows the card there.
async function noAgentWelcome() {
  const q = await open({ agents: { providers: [], settings: DEF } });
  q.pm({ type: "welcome", show: true }); await wait();
  check("welcome, no agent: the card and not the composer", [visible(q.$("#cNoAgent")), visible(q.$("#cCompose"))], [true, false]);
  q.push(PROV([CLAUDE])); await wait();
  check("welcome, an agent found: the composer and not the card", [visible(q.$("#cNoAgent")), visible(q.$("#cCompose"))], [false, true]);
  check("no script errors on the welcome screen", q.errors, []);
  q.close();
}

// The new texts exist in Ukrainian and English, short and plain (no dashes, no quotes).
async function agentTexts() {
  const CYR = /[Ѐ-ӿ]/, DASHQ = /[—–"«»“”„]/;
  const p = await open({ agents: { providers: [], settings: DEF } });
  const card = () => p.$("#cNoAgent");
  const words = (root) => {
    if (!root) return ["(missing)"];
    const out = [];
    [root, ...root.querySelectorAll("*")].forEach((el) => {
      if (el.closest("[data-lang], option[lang]") || el.closest("code")) return;
      for (const at of ["title", "aria-label", "placeholder"]) if (el.getAttribute(at)) out.push(el.getAttribute(at));
      for (const n of el.childNodes) if (n.nodeType === 3 && n.textContent.trim()) out.push(n.textContent.trim());
    });
    return out;
  };
  const ukCard = card() ? card().textContent : "";
  truthy("the Ukrainian card is in Ukrainian", CYR.test(ukCard));
  check("the Ukrainian card has no dashes or quotes", card() ? words(card()).filter((x) => DASHQ.test(x)) : ["(missing)"], []);
  p.$("#setbtn").click();
  const group = () => p.$("#settings select#sAgent");
  const head = () => group() && p.d.getElementById(group().getAttribute("aria-labelledby"));
  truthy("the provider group has a heading", head() && head().textContent.trim());
  truthy("the provider heading is in Ukrainian", head() && CYR.test(head().textContent));
  // No new bridge event here: the card follows the switch by itself.
  p.choose("lang", "en"); await wait();
  check("English card: no Ukrainian", card() ? words(card()).filter((x) => CYR.test(x)) : ["(missing)"], []);
  check("English card: no dashes or quotes", card() ? words(card()).filter((x) => DASHQ.test(x)) : ["(missing)"], []);
  truthy("the card is translated", card() && card().textContent !== ukCard);
  p.push(PROV([CLAUDE, CODEX])); await wait();
  check("English settings: no Ukrainian", words(p.$("#settings")).filter((x) => CYR.test(x)), []);
  check("English settings: no dashes or quotes", words(p.$("#settings")).filter((x) => DASHQ.test(x)), []);
  truthy("the provider heading is in English", head() && head().textContent.trim() && !CYR.test(head().textContent));
  p.choose("lang", "uk"); await wait();
  truthy("the provider heading goes back to Ukrainian", head() && CYR.test(head().textContent));
  check("no script errors", p.errors, []);
  p.close();
}

// Codex runs only /clear: the popup shows only that.
async function codexSlash() {
  const CODEX_ON = { provider: "codex", model: "gpt-5.6-sol", effort: "medium" };
  const names = (p) => [...p.d.querySelectorAll("#cCmds .cmd")].map((x) => x.dataset.cmd);
  const p = await open({ agents: { providers: [CLAUDE, CODEX], settings: CODEX_ON } });
  p.type("/");
  check("Codex with no list: only clear", names(p), ["clear"]);
  p.type("/co");
  check("Codex: no Claude fallback commands", [names(p), p.$("#cCmds").hidden], [[], true]);
  p.type("/re");
  check("Codex: an alias of clear still finds it", names(p), ["clear"]);
  p.type("");
  p.close();

  const claudeCmds = [{ name: "clear", aliases: ["reset", "new"] }, { name: "compact" }, { name: "context" }];
  const q = await open({ commands: claudeCmds, agents: { providers: [CLAUDE, CODEX], settings: DEF } });
  q.type("/co");
  check("Claude: its commands", names(q), ["compact", "context"]);
  q.type("");
  q.push(PROV([CLAUDE, CODEX], CODEX_ON)); await wait();
  q.type("/");
  check("switched to Codex: the cached Claude list is not offered", names(q), ["clear"]);
  q.type("");
  q.push(PROV([CLAUDE, CODEX], CODEX_ON, { commands: [{ name: "clear", aliases: ["reset", "new"] }] })); await wait();
  q.type("/");
  check("the bridge's Codex list", names(q), ["clear"]);
  q.type("");
  q.push(PROV([CLAUDE, CODEX], DEF, { commands: claudeCmds })); await wait();
  q.type("/co");
  check("back to Claude: its commands again", names(q), ["compact", "context"]);
  check("no script errors", [...p.errors, ...q.errors], []);
  q.close();
}

(async () => {
  for (const t of [loadAndModes, sending, photos, quotes, actionsAndQuestions, busyStreamStop, errorStates, scrolling, settingsScroll,
                   statusIcon, settings, language, updateBar, updateCard, rerunCard, slashCommands, safety,
                   composeKeys, markdown, streaming, typingState, photoRemoval, quoteEdges, actionEdges, questionEdges, historyLoads, emptyStates, toasts,
                   helloAndIdentity, relay, iconPriority, updateButton, reconnects, staleCloseAfterVisible, modes, settingsEdges, slashAria,
                   agentSettings, noAgent, noAgentWelcome, agentTexts, codexSlash]) {
    console.log(`\n${t.name}`);
    try { await t(); } catch (e) { failed++; console.log(" FAIL  threw: " + (e.stack || e).toString().split("\n").slice(0, 3).join(" | ")); }
  }
  console.log(failed ? `\n${failed} failed` : "\nall passed");
  process.exit(failed ? 1 : 0);
})();
