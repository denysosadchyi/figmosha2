// Welcome screen with the chat composer at its bottom.
//
// ─── contract ─────────────────────────────────────────────────────────────
//
//   The welcome screen (#welcome) shows the real chat composer at its bottom:
//   the text field, the paperclip (.clip) and the send button (.send). The
//   field gets the focus when the welcome opens. The small settings link
//   (#welcomeSet) is gone; the header settings button closes the welcome.
//
//   Send (Enter or the send button) with text:
//     the welcome closes, { type:"welcome-done" } is posted once (as Start did),
//     and the text goes to the bridge as a normal { type:"chat-send", text }.
//   Shift+Enter makes a new line and sends nothing.
//   Empty or blank text: nothing is sent, the welcome stays open.
//   A pasted image is attached as in the chat and goes with the message.
//   Enter while an IME is composing does not send. A text paste stays a text
//   paste. The paperclip opens the photo picker; a picked photo goes with the
//   message.
//   Esc still closes the welcome (also with the focus in the field).
//   Strings follow the language; the composer keeps working after a re-render.
//   (Colors: tests/colors_lint.test.js.)
//
//   Edge cases (skeptic pass):
//     - No id is doubled in the page: the welcome reuses the chat composer
//       (#cCompose moved in and back) or has its own parts with other ids.
//       After the welcome closes, exactly one shown chat field is left, in #chat.
//     - A draft is never lost: Esc or the settings link with text (and photos)
//       in the welcome field leaves them in #cInput / #cAtt. A language switch
//       while the welcome is open keeps the typed text and the photos.
//     - A send is never doubled: Enter then a click gives one chat-send and one
//       welcome-done; the chat field is empty after it, the text is not in two
//       places. A "refused" reply brings the text back into #cInput once.
//     - Bridge offline: nothing is sent, the text stays (in the welcome field
//       while it is open, or in #cInput when it closed), and the noBridge
//       toast is shown where the user can see it (#cToast sits in #chat, which
//       is display:none while the welcome is open). After the bridge comes up
//       the same text sends once and welcome-done is posted once in total.
//     - Claude busy (history busy:true, a turn from another window): a send
//       from the welcome is not blocked, as in the chat (the bridge queues it).
//     - The paperclip opens the file picker; a picked photo shows in the
//       welcome composer.
//
//     JSDOM=/path/to/node_modules/jsdom node tests/welcome_composer.test.js
const fs = require("fs");
const path = require("path");
const ROOT = path.join(__dirname, "..");

let JSDOM, VirtualConsole;
try { ({ JSDOM, VirtualConsole } = require(process.env.JSDOM || "jsdom")); }
catch (e) {
  console.log("SKIP welcome_composer.test.js: jsdom is not installed (npm install --no-save jsdom)");
  process.exit(0);
}

const HTML = fs.readFileSync(path.join(ROOT, "plugin", "ui.html"), "utf8");
const DOC = "dTest";
const PNG = "data:image/jpeg;base64,SHRUNK";
const CYR = /[\u0400-\u04ff]/;

let failed = 0;
function check(label, actual, expected) {
  const a = JSON.stringify(actual), e = JSON.stringify(expected);
  const ok = a === e;
  if (!ok) failed++;
  console.log(`${ok ? "  ok  " : " FAIL "} ${label}${ok ? "" : `\n         got ${a}\n         want ${e}`}`);
}
const truthy = (label, v) => check(label, !!v, true);
const wait = (ms = 30) => new Promise((r) => setTimeout(r, ms));
async function until(fn, ms = 2000) { const end = Date.now() + ms; while (!fn() && Date.now() < end) await wait(10); }

// ─── harness ──────────────────────────────────────────────────────────────

async function open({ welcome = true, theme = "system", lang, order = "settings-first", online = true, busy = false } = {}) {
  const sent = [], posted = [], sockets = [], errors = [], picks = [];
  const vc = new VirtualConsole();
  vc.on("jsdomError", (e) => errors.push(e.message));
  const dom = new JSDOM(HTML, {
    runScripts: "dangerously", virtualConsole: vc, url: "http://localhost/", pretendToBeVisual: true,
    beforeParse(w) {
      w.WebSocket = class {
        constructor() { this.readyState = 0; sockets.push(this); if (online) setTimeout(() => this.up(), 5); }
        up() { this.readyState = 1; this.onopen && this.onopen(); }
        send(d) { if (this.readyState !== 1) throw new Error("not open"); sent.push(JSON.parse(d)); }
        close() { this.readyState = 3; this.onclose && this.onclose(); }
      };
      w.WebSocket.OPEN = 1;
      w.parent.postMessage = (m) => posted.push(m.pluginMessage);
      // Photos are shrunk on a canvas; jsdom has no canvas (as in chat_dom.test.js).
      w.URL.createObjectURL = () => "blob:x"; w.URL.revokeObjectURL = () => {};
      w.Image = class {
        set src(u) { this._src = u; setTimeout(() => this.onload(), 1); }
        get naturalWidth() { return 3200; } get naturalHeight() { return 1600; }
      };
      w.HTMLCanvasElement.prototype.getContext = () => ({ fillRect() {}, drawImage() {}, set fillStyle(v) {} });
      w.HTMLCanvasElement.prototype.toDataURL = function () { return PNG + "-" + this.width + "x" + this.height; };
      w.Element.prototype.scrollIntoView = function () {};
      w.ResizeObserver = class { observe() {} };
      // The photo picker: record which file input was asked to open.
      const click0 = w.HTMLInputElement.prototype.click;
      w.HTMLInputElement.prototype.click = function () { if (this.type === "file") picks.push(this); else click0.call(this); };
    },
  });
  const w = dom.window, d = w.document;
  w.addEventListener("error", (e) => errors.push(e.message));
  const pm = (m) => w.dispatchEvent(new w.MessageEvent("message", { data: { pluginMessage: m } }));
  const ws = () => sockets[sockets.length - 1];
  await wait(20);
  pm({ type: "identity", name: "Banner", docSig: DOC });
  const s = { type: "settings", scale: 1, theme };
  if (lang) s.lang = lang;
  const wm = () => pm({ type: "welcome", show: welcome });
  if (order === "settings-first") { pm(s); wm(); pm({ type: "ui-mode", mode: "chat" }); }
  else { pm({ type: "ui-mode", mode: "chat" }); pm(s); wm(); }
  await wait();
  const history = () => ws().onmessage({ data: JSON.stringify({ type: "chat", doc: DOC, event: { kind: "history", busy,
    activity: busy ? [{ t: Date.now() / 1000, text: "Reading the file" }] : [], messages: [] } }) });
  if (online && ws()) history();
  await wait();
  const $ = (q) => d.querySelector(q);
  const box = () => $("#welcome");
  // The composer parts inside the welcome screen.
  const field = () => box() && box().querySelector("textarea");
  const clip = () => box() && box().querySelector("button.clip");
  const send = () => box() && box().querySelector("button.send");
  return {
    w, d, $, box, field, clip, send, sent, posted, errors, pm, sockets, picks,
    async goOnline() { ws().up(); await wait(); history(); await wait(); },
    bridge(ev) { ws().onmessage({ data: JSON.stringify({ type: "chat", doc: DOC, event: ev }) }); },
    done: () => posted.filter((m) => m && m.type === "welcome-done"),
    chatSends: () => sent.filter((m) => m && m.type === "chat-send"),
    type(text, el = field()) { el.value = text; el.dispatchEvent(new w.Event("input", { bubbles: true })); },
    key(k, el, opts = {}) {
      const ev = new w.KeyboardEvent("keydown", Object.assign({ key: k, bubbles: true, cancelable: true }, opts));
      (el || d.activeElement || d.body).dispatchEvent(ev); return ev;
    },
    paste(files, el = field()) {
      const ev = new w.Event("paste", { bubbles: true, cancelable: true });
      ev.clipboardData = { files, items: files.map((f) => ({ kind: "file", type: f.type, getAsFile: () => f })), types: ["Files"], getData: () => "" };
      el.dispatchEvent(ev); return ev;
    },
    file(name, type) { return new w.File(["x"], name, { type }); },
    close() { w.close(); },
  };
}
function shown(p, el) {
  if (!el) return false;
  for (let n = el; n && n.nodeType === 1; n = n.parentElement) {
    if (n.hidden) return false;
    if (p.w.getComputedStyle(n).display === "none") return false;
  }
  return true;
}
// Ids that appear more than once in the page.
function dupIds(p) {
  const n = {};
  p.d.querySelectorAll("[id]").forEach((e) => { n[e.id] = (n[e.id] || 0) + 1; });
  return Object.keys(n).filter((k) => n[k] > 1);
}
// Shown chat text fields (the chat composer, wherever it is).
function shownChatFields(p) {
  const ph = p.$("#cInput") && p.$("#cInput").getAttribute("placeholder");
  return [...p.d.querySelectorAll("textarea")].filter((e) => shown(p, e) && (e.id === "cInput" || e.getAttribute("placeholder") === ph));
}
// Stop a test early with one clear failure when the composer is not there yet.
function need(p, label) {
  if (p.field() && p.send()) return true;
  failed++; console.log(` FAIL  ${label}: no composer (textarea and button.send) inside #welcome`);
  return false;
}

// ─── tests ────────────────────────────────────────────────────────────────

async function composerShown() {
  const p = await open();
  truthy("welcome is shown", shown(p, p.box()));
  truthy("there is a text field inside #welcome", p.field());
  truthy("the text field is shown", shown(p, p.field()));
  truthy("there is the paperclip button inside #welcome", p.clip());
  truthy("the paperclip is shown", shown(p, p.clip()));
  truthy("there is the send button inside #welcome", p.send());
  truthy("the send button is shown", shown(p, p.send()));
  truthy("the composer is a form (Enter and the button submit it)", p.field() && p.field().closest("form") && p.box().contains(p.field().closest("form")));
  // The composer sits at the bottom: after the feature list and the art.
  const list = p.box() && p.box().querySelector(".wlist");
  truthy("the composer comes after the feature list", list && p.field() && (list.compareDocumentPosition(p.field()) & 4));
  check("only one #cInput in the page", p.d.querySelectorAll("#cInput").length <= 1, true);
  check("no doubled ids in the page", dupIds(p), []);
  check("one shown chat field in the page (the welcome one)", shownChatFields(p).length, 1);
  check("no script errors", p.errors, []);
  p.close();
}

async function focus() {
  let p = await open();
  truthy("on open the welcome field has the focus", p.field() && p.d.activeElement === p.field());
  p.close();
  p = await open({ order: "mode-first" });
  truthy("ui-mode before settings: the field still has the focus", p.field() && p.d.activeElement === p.field());
  p.close();
}

async function sendButton() {
  const p = await open();
  if (!need(p, "send button")) { p.close(); return; }
  p.type("Зроби кнопку синьою");
  p.send().click();
  await until(() => p.chatSends().length);
  check("send: the text goes to the chat", p.chatSends().map((m) => m.text), ["Зроби кнопку синьою"]);
  check("send: welcome-done is posted once", p.done(), [{ type: "welcome-done" }]);
  check("send: the welcome is hidden", shown(p, p.box()), false);
  check("send: #welcome gets the hidden attribute", p.box().hidden, true);
  truthy("send: the chat is shown", shown(p, p.$("#chat")));
  truthy("send: still in chat mode", p.d.body.classList.contains("chatmode"));
  truthy("send: the chat composer is shown in the chat", shown(p, p.$("#cInput")) && p.$("#chat").contains(p.$("#cInput")));
  check("send: the chat field is empty", p.$("#cInput").value, "");
  check("send: the chat field has the focus", p.d.activeElement && p.d.activeElement.id, "cInput");
  p.$("#cInput").value = "ще"; p.$("#cInput").dispatchEvent(new p.w.Event("input", { bubbles: true }));
  p.key("Enter", p.$("#cInput"));
  await until(() => p.chatSends().length > 1);
  check("after the welcome the chat sends as usual", p.chatSends().map((m) => m.text), ["Зроби кнопку синьою", "ще"]);
  check("welcome-done is not posted again", p.done().length, 1);
  check("after send: no doubled ids", dupIds(p), []);
  check("after send: one shown chat field, in #chat", shownChatFields(p).map((e) => p.$("#chat").contains(e)), [true]);
  check("the window was not sent to the bar", p.posted.filter((m) => m && m.type === "ui-mode" && m.mode === "bar").length, 0);
  check("no script errors", p.errors, []);
  p.close();
}

async function enterSends() {
  const p = await open();
  if (!need(p, "Enter")) { p.close(); return; }
  p.type("Додай заголовок");
  const ev = p.key("Enter", p.field());
  await until(() => p.chatSends().length);
  check("Enter: its default new line is dropped", ev.defaultPrevented, true);
  check("Enter: the text goes to the chat once", p.chatSends().map((m) => m.text), ["Додай заголовок"]);
  check("Enter: welcome-done is posted once", p.done(), [{ type: "welcome-done" }]);
  check("Enter: the welcome is hidden", shown(p, p.box()), false);
  truthy("Enter: the chat is shown", shown(p, p.$("#chat")));
  p.close();
}

async function shiftEnter() {
  const p = await open();
  if (!need(p, "Shift+Enter")) { p.close(); return; }
  p.type("рядок");
  const ev = p.key("Enter", p.field(), { shiftKey: true });
  await wait(60);
  check("Shift+Enter: nothing is sent", p.chatSends().length, 0);
  check("Shift+Enter: the default new line is kept", ev.defaultPrevented, false);
  check("Shift+Enter: no welcome-done", p.done().length, 0);
  truthy("Shift+Enter: the welcome stays open", shown(p, p.box()));
  p.close();
}

async function emptyText() {
  const p = await open();
  if (!need(p, "empty text")) { p.close(); return; }
  p.type("");
  p.key("Enter", p.field());
  p.send().click();
  await wait(60);
  check("empty: nothing is sent", p.chatSends().length, 0);
  check("empty: no welcome-done", p.done().length, 0);
  truthy("empty: the welcome stays open", shown(p, p.box()));
  p.type("   \n  ");
  p.key("Enter", p.field());
  p.send().click();
  await wait(60);
  check("blank: nothing is sent", p.chatSends().length, 0);
  check("blank: no welcome-done", p.done().length, 0);
  truthy("blank: the welcome stays open", shown(p, p.box()));
  check("no script errors", p.errors, []);
  p.close();
}

async function pasteImage() {
  const p = await open();
  if (!need(p, "paste")) { p.close(); return; }
  const ev = p.paste([p.file("shot.png", "image/png")]);
  await until(() => p.box().querySelectorAll(".th").length);
  truthy("paste: the image is not pasted as text", ev.defaultPrevented);
  check("paste: a thumbnail shows in the welcome composer", p.box().querySelectorAll(".th").length, 1);
  truthy("paste: the welcome stays open", shown(p, p.box()));
  check("paste: no welcome-done yet", p.done().length, 0);
  p.type("Встав це фото");
  p.key("Enter", p.field());
  await until(() => p.chatSends().length);
  const s = p.chatSends()[0] || {};
  check("paste then send: the text goes", s.text, "Встав це фото");
  check("paste then send: the image goes with it", (s.images || []).length, 1);
  check("paste then send: welcome-done once", p.done().length, 1);
  check("no script errors", p.errors, []);
  p.close();
}

async function escCloses() {
  let p = await open();
  p.key("Escape", p.field() || p.d.body);
  await wait();
  check("Esc in the field: the welcome is hidden", shown(p, p.box()), false);
  check("Esc in the field: welcome-done once", p.done(), [{ type: "welcome-done" }]);
  check("Esc: nothing is sent", p.chatSends().length, 0);
  truthy("Esc: the chat is shown", shown(p, p.$("#chat")));
  check("Esc: the chat field has the focus", p.d.activeElement && p.d.activeElement.id, "cInput");
  // Harness check: the fake bridge takes a chat send and a pasted photo.
  const ci = p.$("#cInput");
  p.paste([p.file("a.png", "image/png")], ci);
  await until(() => p.d.querySelectorAll("#cAtt .th").length);
  p.type("harness", ci); p.key("Enter", ci);
  await until(() => p.chatSends().length);
  check("harness: the chat sends text and a photo after Esc", p.chatSends().map((m) => [m.text, (m.images || []).length]), [["harness", 1]]);
  p.close();
  // Esc after some text: the welcome closes, the text is not sent.
  p = await open();
  if (p.field()) p.type("чернетка");
  p.key("Escape", p.field() || p.d.body);
  await wait();
  check("Esc with text: the welcome is hidden", shown(p, p.box()), false);
  check("Esc with text: nothing is sent", p.chatSends().length, 0);
  check("Esc with text: the composer is there in the chat", !!p.$("#cInput") && p.$("#chat").contains(p.$("#cInput")), true);
  check("Esc with text: the draft is kept in the chat field", p.$("#cInput") && p.$("#cInput").value, "чернетка");
  check("Esc with text: no doubled ids", dupIds(p), []);
  check("Esc with text: one shown chat field", shownChatFields(p).length, 1);
  p.close();
  // Esc after a pasted photo: the photo stays attached in the chat.
  p = await open();
  if (need(p, "Esc with a photo")) {
    p.paste([p.file("b.png", "image/png")]);
    await until(() => p.box().querySelectorAll(".th").length);
    p.key("Escape", p.field());
    await wait();
    check("Esc with a photo: the photo is in the chat composer", p.d.querySelectorAll("#chat #cAtt .th").length, 1);
    p.type("з фото", p.$("#cInput")); p.key("Enter", p.$("#cInput"));
    await until(() => p.chatSends().length);
    check("Esc with a photo: it goes with the next chat message", p.chatSends().map((m) => (m.images || []).length), [1]);
  }
  p.close();
}

async function imeEnter() {
  const p = await open();
  if (!need(p, "IME Enter")) { p.close(); return; }
  p.type("ням");
  const ev = p.key("Enter", p.field(), { isComposing: true });
  await wait(60);
  if (ev.isComposing !== true) { console.log("  skip  jsdom has no isComposing"); p.close(); return; }
  check("IME Enter: nothing is sent", p.chatSends().length, 0);
  check("IME Enter: no welcome-done", p.done().length, 0);
  truthy("IME Enter: the welcome stays open", shown(p, p.box()));
  p.close();
}

async function pasteText() {
  const p = await open();
  if (!need(p, "text paste")) { p.close(); return; }
  const ev = new p.w.Event("paste", { bubbles: true, cancelable: true });
  ev.clipboardData = { files: [], items: [{ kind: "string", type: "text/plain", getAsFile: () => null }], types: ["text/plain"], getData: () => "привіт" };
  p.field().dispatchEvent(ev);
  await wait(30);
  check("text paste: not blocked (the browser pastes the text)", ev.defaultPrevented, false);
  check("text paste: no thumbnail", p.box().querySelectorAll(".th").length, 0);
  truthy("text paste: the welcome stays open", shown(p, p.box()));
  check("text paste: nothing is sent", p.chatSends().length, 0);
  p.close();
}

async function clipPicker() {
  const p = await open();
  if (!need(p, "paperclip") || !p.clip()) { if (p.field() && !p.clip()) { failed++; console.log(" FAIL  paperclip: no button.clip inside #welcome"); } p.close(); return; }
  p.clip().click();
  await wait();
  check("paperclip: opens one photo picker", p.picks.length, 1);
  const inp = p.picks[0];
  check("paperclip: the picker takes images", inp && inp.getAttribute("accept"), "image/*");
  truthy("paperclip: the welcome stays open", shown(p, p.box()));
  if (inp) {
    Object.defineProperty(inp, "files", { value: [p.file("pick.png", "image/png")], configurable: true });
    inp.dispatchEvent(new p.w.Event("change", { bubbles: true }));
    await until(() => p.box().querySelectorAll(".th").length);
    check("picked photo: a thumbnail shows in the welcome composer", p.box().querySelectorAll(".th").length, 1);
    p.type("З фото"); p.key("Enter", p.field());
    await until(() => p.chatSends().length);
    check("picked photo: goes with the message", p.chatSends().map((m) => [m.text, (m.images || []).length]), [["З фото", 1]]);
    check("picked photo: welcome-done once", p.done().length, 1);
  }
  check("no script errors", p.errors, []);
  p.close();
}

async function settingsLink() {
  const p = await open();
  check("no settings link under the field", p.$("#welcomeSet"), null);
  p.$("#setbtn").click();
  await wait();
  check("settings button: the welcome is hidden", shown(p, p.box()), false);
  check("settings button: welcome-done once", p.done(), [{ type: "welcome-done" }]);
  truthy("settings button: #settings is shown", shown(p, p.$("#settings")));
  check("settings button: nothing is sent", p.chatSends().length, 0);
  p.close();
  // A draft survives the trip to the settings and back.
  const q = await open();
  if (need(q, "settings button with a draft")) {
    q.type("не загуби");
    q.$("#setbtn").click(); await wait();
    if (q.$("#sBack")) q.$("#sBack").click();
    await wait();
    check("settings and back: the draft is in the chat field", q.$("#cInput") && q.$("#cInput").value, "не загуби");
    check("settings and back: nothing is sent", q.chatSends().length, 0);
    check("settings and back: no doubled ids", dupIds(q), []);
  }
  q.close();
}

async function languages() {
  let p = await open({ lang: "en" });
  const labels = p.box() ? [...p.box().querySelectorAll("[aria-label],[title],[placeholder]")].map((e) => ["aria-label", "title", "placeholder"].map((a) => e.getAttribute(a) || "").join(" ")).join(" ") : "";
  check("en: no Cyrillic in the welcome texts and labels", /[Ѐ-ӿ]/.test((p.box() ? p.box().textContent : "") + labels), false);
  if (need(p, "en send")) {
    p.type("Make it blue"); p.key("Enter", p.field());
    await until(() => p.chatSends().length);
    check("en: send works", p.chatSends().map((m) => m.text), ["Make it blue"]);
    check("en: welcome-done once", p.done().length, 1);
  }
  p.close();

  // A switch of language while the welcome is open re-renders it; the composer still works.
  p = await open();
  check("uk: the placeholder is Ukrainian", CYR.test(p.field() && p.field().getAttribute("placeholder") || ""), true);
  p.pm({ type: "settings", scale: 1, theme: "system", lang: "en" }); await wait();
  check("switch to en: the placeholder follows", CYR.test(p.field() && p.field().getAttribute("placeholder") || "x"), false);
  truthy("switch to en: the paperclip is still there", p.clip());
  if (need(p, "after re-render")) {
    p.type("After switch"); p.send().click();
    await until(() => p.chatSends().length);
    check("after the re-render send still works once", p.chatSends().map((m) => m.text), ["After switch"]);
    check("after the re-render welcome-done once", p.done().length, 1);
  }
  check("no script errors", p.errors, []);
  p.close();

  // A switch of language while text and a photo are in the welcome field: both stay.
  p = await open();
  if (need(p, "re-render with a draft")) {
    p.paste([p.file("c.png", "image/png")]);
    await until(() => p.box().querySelectorAll(".th").length);
    p.type("Мій текст");
    p.pm({ type: "settings", scale: 1, theme: "system", lang: "en" }); await wait();
    check("switch with a draft: the text stays in the field", p.field() && p.field().value, "Мій текст");
    check("switch with a draft: the photo stays", p.box().querySelectorAll(".th").length, 1);
    check("switch with a draft: no doubled ids", dupIds(p), []);
    check("switch with a draft: #cInput is still in the page once", p.d.querySelectorAll("#cInput").length, 1);
    if (p.send()) p.send().click();
    await until(() => p.chatSends().length);
    check("switch with a draft: send gives the text and the photo", p.chatSends().map((m) => [m.text, (m.images || []).length]), [["Мій текст", 1]]);
  }
  check("no script errors", p.errors, []);
  p.close();
}

async function noDouble() {
  const p = await open();
  if (!need(p, "double send")) { p.close(); return; }
  p.type("Один раз");
  p.key("Enter", p.field());
  if (p.send()) p.send().click();
  await wait(60);
  check("Enter then click: one chat-send", p.chatSends().map((m) => m.text), ["Один раз"]);
  check("Enter then click: one welcome-done", p.done().length, 1);
  check("the welcome field does not keep the sent text", p.field() ? p.field().value : "", "");
  check("the chat field is empty", p.$("#cInput").value, "");
  // The bridge refuses the message: the text comes back once, in the chat field.
  p.bridge({ kind: "refused" }); await wait();
  check("refused: the text comes back into the chat field", p.$("#cInput").value, "Один раз");
  check("refused: one shown chat field, in #chat", shownChatFields(p).map((e) => p.$("#chat").contains(e)), [true]);
  check("refused: the welcome stays closed", shown(p, p.box()), false);
  check("no script errors", p.errors, []);
  p.close();
}

async function offline() {
  const NB = "Немає зв'язку з мостом";
  const p = await open({ online: false });
  truthy("offline: the welcome is shown", shown(p, p.box()));
  truthy("offline: the composer is there", p.field() && p.send());
  truthy("offline: the field has the focus", p.field() && p.d.activeElement === p.field());
  if (!need(p, "offline")) { p.close(); return; }
  p.type("Без мосту");
  p.key("Enter", p.field());
  await wait(60);
  check("offline: nothing is sent", p.chatSends().length, 0);
  const open_ = shown(p, p.box());
  const keeps = open_ ? p.field().value : p.$("#cInput").value;
  check("offline: the text is kept (welcome field if open, else the chat field)", keeps, "Без мосту");
  check("offline: welcome-done matches the screen (0 while open, 1 when closed)", p.done().length, open_ ? 0 : 1);
  const tips = [...p.d.querySelectorAll("body *")].filter((e) => e.children.length === 0 && e.textContent.trim() === NB);
  truthy("offline: the noBridge note is shown where the user sees it", tips.some((e) => shown(p, e)),
    "found in " + tips.map((e) => "#" + (e.id || e.className) + (shown(p, e) ? "" : " (hidden)")).join(", "));
  check("offline: no doubled ids", dupIds(p), []);
  // The bridge comes up: the same text sends once.
  await p.goOnline();
  const f = shown(p, p.box()) ? p.field() : p.$("#cInput");
  p.key("Enter", f);
  await until(() => p.chatSends().length);
  check("online again: the text sends once", p.chatSends().map((m) => m.text), ["Без мосту"]);
  check("online again: welcome-done once in total", p.done().length, 1);
  check("online again: the welcome is closed", shown(p, p.box()), false);
  check("online again: the chat field is empty", p.$("#cInput").value, "");
  check("no script errors", p.errors, []);
  p.close();
}

async function busy() {
  const p = await open({ busy: true });
  truthy("busy: the welcome is shown", shown(p, p.box()));
  if (!need(p, "busy")) { p.close(); return; }
  truthy("busy: the field has the focus", p.d.activeElement === p.field());
  check("busy: the send button is not disabled", p.send().disabled, false);
  p.type("Після цього");
  p.send().click();
  await until(() => p.chatSends().length);
  check("busy: the text is sent anyway (the bridge queues it)", p.chatSends().map((m) => m.text), ["Після цього"]);
  check("busy: welcome-done once", p.done().length, 1);
  check("busy: the welcome is hidden", shown(p, p.box()), false);
  truthy("busy: the working line shows in the chat", shown(p, p.$("#cTyping")) && p.$("#chat").contains(p.$("#cTyping")));
  check("busy: no doubled ids", dupIds(p), []);
  check("no script errors", p.errors, []);
  p.close();
}

(async () => {
  for (const t of [composerShown, focus, sendButton, enterSends, shiftEnter, imeEnter, emptyText, pasteImage, pasteText, clipPicker, escCloses, settingsLink, languages, noDouble, offline, busy]) {
    console.log(`\n${t.name}`);
    try { await t(); } catch (e) { failed++; console.log(" FAIL  threw: " + (e.stack || e).toString().split("\n").slice(0, 3).join(" | ")); }
  }
  console.log(failed ? `\n${failed} failed` : "\nall passed");
  process.exit(failed ? 1 : 0);
})();
