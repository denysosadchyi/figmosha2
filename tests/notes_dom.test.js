// The notes part of the plugin window (docs/CHAT.md, Notes), in jsdom: the pin
// button in the message field, the tasks page, Do and Do all, and removal.
// code.js is faked: the test reads what the window posts to it and answers.
//
//     npm install --no-save jsdom      (once)
//     node tests/notes_dom.test.js
//
// JSDOM=/path/to/node_modules/jsdom picks a jsdom from another folder.
const fs = require("fs");
const path = require("path");

let JSDOM, VirtualConsole;
try { ({ JSDOM, VirtualConsole } = require(process.env.JSDOM || "jsdom")); }
catch (e) {
  console.log("SKIP notes_dom.test.js: jsdom is not installed (npm install --no-save jsdom)");
  process.exit(0);
}

const HTML = fs.readFileSync(path.join(__dirname, "..", "plugin", "ui.html"), "utf8");
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

async function open({ lang = "uk", history = [], notes = true } = {}) {
  const sent = [], posted = [], sockets = [], errors = [];
  const vc = new VirtualConsole();
  vc.on("jsdomError", (e) => errors.push(e.message));
  const dom = new JSDOM(HTML, {
    runScripts: "dangerously", virtualConsole: vc, url: "http://localhost/", pretendToBeVisual: true,
    beforeParse(w) {
      w.WebSocket = class {
        constructor() { this.readyState = 0; sockets.push(this); setTimeout(() => { this.readyState = 1; this.onopen && this.onopen(); }, 5); }
        send(d) { sent.push(JSON.parse(d)); }
        close() { this.readyState = 3; this.onclose && this.onclose(); }
      };
      w.WebSocket.CONNECTING = 0; w.WebSocket.OPEN = 1; w.WebSocket.CLOSING = 2; w.WebSocket.CLOSED = 3;
      w.parent.postMessage = (m) => posted.push(m.pluginMessage);
      w.Element.prototype.scrollIntoView = function () {};
      w.ResizeObserver = class { observe() {} };
    },
  });
  const w = dom.window, d = w.document;
  w.addEventListener("error", (e) => errors.push(e.message));
  const pm = (m) => w.dispatchEvent(new w.MessageEvent("message", { data: { pluginMessage: m } }));
  const push = (event) => sockets[sockets.length - 1].onmessage({ data: JSON.stringify({ type: "chat", doc: DOC, event }) });
  await wait(20);
  pm({ type: "identity", name: "Banner", docSig: DOC, notes });
  pm({ type: "settings", scale: 1, theme: "light", lang });
  pm({ type: "ui-mode", mode: "chat" });
  await wait();
  push({ kind: "history", busy: false, activity: [], messages: history });
  await wait();
  const $ = (s) => d.querySelector(s);
  const tr = (k) => w.eval(`t(${JSON.stringify(k)})`);
  const html = d.documentElement;
  return {
    w, d, $, pm, push, sent, posted, errors, tr, html,
    toCode: (type) => posted.filter((m) => m && m.type === type),
    chatSent: () => sent.filter((m) => m.type === "chat-send"),
    type: (text) => { const i = $("#cInput"); i.value = text; i.dispatchEvent(new w.Event("input", { bubbles: true })); },
    toast: () => ($("#cToast").classList.contains("on") ? $("#cToast").textContent : null),
    close: () => w.close(),
  };
}

const NOTES = [
  { id: "1:4", i: 0, name: "Card <b>", type: "FRAME", text: "Make it blue", done: false, reply: "" },
  { id: "1:4", i: 1, name: "Card <b>", type: "FRAME", text: "Round the corners", done: true, reply: "Radius is 16" },
  { id: "1:5", i: 0, name: "Title", type: "TEXT", text: "Shorter text", done: false, reply: "" },
];

async function pinTests() {
  const p = await open();
  truthy("the pin button sits in the message field", p.$("#cCompose .field #cPin"));
  check("its name comes from the texts", p.$("#cPin").getAttribute("aria-label"), p.tr("notePin"));

  p.$("#cPin").click();
  check("no text: a toast, nothing goes to code.js", [p.toast(), p.toCode("note-add").length], [p.tr("noteNoText"), 0]);

  p.type("  Make it blue ");
  p.$("#cPin").click();
  check("the pin button sends the trimmed text to code.js", p.toCode("note-add"), [{ type: "note-add", text: "Make it blue" }]);
  check("nothing goes to the chat", p.chatSent().length, 0);
  check("the text stays until code.js answers", p.$("#cInput").value, "  Make it blue ");

  const before = p.sent.length;
  p.pm({ type: "notes", items: NOTES.slice(0, 1), error: null, added: NOTES[0] });
  check("pinned: the field is cleared and a toast says so", [p.$("#cInput").value, p.toast()], ["", p.tr("notePinned")]);
  check("the answer of code.js is not forwarded to the bridge", p.sent.length, before);

  p.type("Second"); p.$("#cPin").click(); p.type("Second, edited");
  p.pm({ type: "notes", items: NOTES.slice(0, 1), error: null, added: NOTES[0] });
  check("a text edited after the click is kept", p.$("#cInput").value, "Second, edited");

  const key = (el, k, o = {}) => el.dispatchEvent(new p.w.KeyboardEvent("keydown", Object.assign({ key: k, bubbles: true, cancelable: true }, o)));
  const pins = () => p.toCode("note-add").length;
  let n0 = pins();
  p.type("By keys");
  key(p.$("#cInput"), "Enter", { shiftKey: true, metaKey: true });
  check("Cmd+Shift+Enter pins the text", [pins(), p.toCode("note-add").pop().text, p.chatSent().length], [n0 + 1, "By keys", 0]);
  key(p.$("#cInput"), "Enter", { shiftKey: true, ctrlKey: true });
  check("Ctrl+Shift+Enter pins it too", pins(), n0 + 2);
  key(p.$("#cInput"), "Enter", { shiftKey: true });
  check("Shift+Enter alone pins nothing and sends nothing", [pins(), p.chatSent().length], [n0 + 2, 0]);
  p.pm({ type: "notes", items: NOTES.slice(0, 1), error: null, added: NOTES[0] });
  p.type("Second, edited");

  for (const [code, key] of [["nosel", "noteNoSel"], ["unsupported", "noteBadSel"], ["gone", "noteGone"], ["fail", "noteFail"]]) {
    p.pm({ type: "note-error", code });
    check(`error ${code} shows its toast`, p.toast(), p.tr(key));
  }
  check("an error keeps the text", p.$("#cInput").value, "Second, edited");
  check("errors are not forwarded to the bridge", p.sent.length, before);
  check("no script errors", p.errors, []);
  p.close();
}

async function pageTests() {
  const p = await open();
  truthy("the tasks icon is in the header", p.$("#bar #barActions #notebtn"));
  check("the page is closed at first", p.html.classList.contains("notes-open"), false);

  p.$("#notebtn").click();
  check("the icon opens the page and asks code.js for the list",
        [p.html.classList.contains("notes-open"), p.toCode("notes-load").length], [true, 1]);
  check("nothing is drawn before the list arrives", p.$("#nList").innerHTML, "");

  p.pm({ type: "notes", items: [], error: null });
  check("no tasks: the hint, and no Do all", [p.$("#nList .nempty").textContent, p.$("#nRunAll").hidden], [p.tr("noteEmpty"), true]);

  p.pm({ type: "notes", items: NOTES, error: null });
  const rows = () => [...p.d.querySelectorAll("#nList .nrow")];
  check("one row per task", rows().length, 3);
  check("a row shows the layer name as text, the task and the reply",
        [rows()[1].querySelector(".nname b").textContent, rows()[1].querySelector(".ntext").textContent, rows()[1].querySelector(".nreply").textContent],
        ["Card <b>", "Round the corners", "Radius is 16"]);
  check("a done task is marked and has no Do button",
        [rows()[1].classList.contains("done"), rows()[1].querySelector(".nname em").textContent, !!rows()[1].querySelector("[data-n=run]")],
        [true, p.tr("noteDone"), false]);
  check("an open task has Do and Remove", [...rows()[0].querySelectorAll(".nact")].map((b) => b.dataset.n), ["run", "rm"]);
  check("Do all shows while a task is open", p.$("#nRunAll").hidden, false);
  truthy("every icon button has a name", [...p.d.querySelectorAll("#nList .nact")].every((b) => b.getAttribute("aria-label")));

  rows()[2].querySelector(".nmain").click();
  check("a click on a row shows the layer", p.toCode("note-focus").pop(), { type: "note-focus", id: "1:5", i: 0, text: "Shorter text" });

  rows()[1].querySelector("[data-n=rm]").click();
  check("Remove names the note by id, index and text", p.toCode("note-remove").pop(), { type: "note-remove", id: "1:4", i: 1, text: "Round the corners" });

  rows()[2].querySelector("[data-n=run]").click();
  const one = p.chatSent().pop();
  check("Do sends the task to the chat", one.text, p.w.ChatCore.notePrompt([NOTES[2]], "uk"));
  check("and goes back to the chat", p.html.classList.contains("notes-open"), false);
  check("leaving the page tells code.js to stop following the canvas", p.toCode("notes-close").length, 1);

  p.$("#notebtn").click();
  p.$("#nRunAll").click();
  check("Do all sends only the open tasks, in one message",
        [p.chatSent().length, p.chatSent().pop().text], [2, p.w.ChatCore.notePrompt([NOTES[0], NOTES[2]], "uk")]);

  // The page reloads when the agent finishes a turn while it is open.
  p.$("#notebtn").click();
  const loads = p.toCode("notes-load").length;
  p.push({ kind: "busy", busy: true }); await wait(5);
  check("no reload while the agent works", p.toCode("notes-load").length, loads);
  p.push({ kind: "busy", busy: false }); await wait(5);
  check("a reload when the turn ends", p.toCode("notes-load").length, loads + 1);
  p.$("#nBack").click();
  p.push({ kind: "busy", busy: true }); p.push({ kind: "busy", busy: false }); await wait(5);
  check("no reload while the page is closed", p.toCode("notes-load").length, loads + 1);

  p.$("#notebtn").click();
  p.d.dispatchEvent(new p.w.KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true }));
  check("Esc closes the tasks page", p.html.classList.contains("notes-open"), false);

  // One page at a time, and the bar closes both.
  const closes = p.toCode("notes-close").length;
  p.$("#notebtn").click(); p.$("#setbtn").click();
  check("the settings close the tasks page", [p.html.classList.contains("settings-open"), p.html.classList.contains("notes-open")], [true, false]);
  check("and code.js hears it", p.toCode("notes-close").length, closes + 1);
  p.$("#notebtn").click();
  check("the tasks page closes the settings", [p.html.classList.contains("settings-open"), p.html.classList.contains("notes-open")], [false, true]);
  p.pm({ type: "ui-mode", mode: "bar" });
  check("going to the bar closes the tasks page", p.html.classList.contains("notes-open"), false);

  p.pm({ type: "ui-mode", mode: "chat" });
  p.$("#notebtn").click();
  p.pm({ type: "notes", items: [], error: "noapi" });
  check("a list that cannot be read closes the page and hides the feature",
        [p.html.classList.contains("notes-open"), p.html.classList.contains("has-notes")], [false, false]);
  check("no script errors", p.errors, []);
  p.close();
}

async function hiddenTests() {
  const shown = (p, sel) => p.w.getComputedStyle(p.$(sel)).display !== "none";
  const on = await open();
  check("with annotations the icon and the pin button show", [shown(on, "#notebtn"), shown(on, "#cPin")], [true, true]);
  on.close();
  const p = await open({ notes: false });
  check("without annotations both are hidden", [shown(p, "#notebtn"), shown(p, "#cPin")], [false, false]);
  p.type("Hidden");
  p.$("#cInput").dispatchEvent(new p.w.KeyboardEvent("keydown", { key: "Enter", shiftKey: true, ctrlKey: true, bubbles: true, cancelable: true }));
  check("and the pin keys do nothing", p.toCode("note-add").length, 0);
  p.pm({ type: "identity", name: "Banner", docSig: DOC });
  check("an old code.js that says nothing keeps them hidden", [shown(p, "#notebtn"), shown(p, "#cPin")], [false, false]);
  check("and the settings have no row for them", p.$("#sNotesRow").hidden, true);
  check("no script errors", p.errors, []);
  p.close();

  // The user's own switch on the settings page.
  const q = await open();
  const pick = (v) => { const s = q.$("#sNotes"); s.value = v; s.dispatchEvent(new q.w.Event("change", { bubbles: true })); };
  check("the settings row shows and says On", [q.$("#sNotesRow").hidden, q.$("#sNotes").value], [false, "on"]);
  q.$("#notebtn").click();
  pick("off");
  check("Off hides the icon and the pin button and closes the page",
        [shown(q, "#notebtn"), shown(q, "#cPin"), q.html.classList.contains("notes-open")], [false, false, false]);
  check("the choice goes to code.js", q.toCode("notes-setting"), [{ type: "notes-setting", on: false }]);
  pick("on");
  check("On brings them back", [shown(q, "#notebtn"), shown(q, "#cPin"), q.toCode("notes-setting").pop().on], [true, true, true]);
  q.pm({ type: "notes-setting", on: false });
  check("a stored Off applies on start and is not sent back",
        [shown(q, "#notebtn"), q.$("#sNotes").value, q.toCode("notes-setting").length], [false, "off", 2]);
  check("the stored choice is not forwarded to the bridge", q.sent.filter((m) => m.type === "notes-setting").length, 0);
  check("no script errors", q.errors, []);
  q.close();
}

async function langTests() {
  const p = await open({ lang: "en" });
  p.$("#notebtn").click();
  p.pm({ type: "notes", items: NOTES, error: null });
  check("English: the static texts", [p.$("#notes h2").textContent, p.$("#nRunAll").textContent, p.$("#notebtn").title],
        ["Tasks on the canvas", "Do all", "Tasks on the canvas"]);
  p.$("#nRunAll").click();
  truthy("English: the chat message is English", /^Do the tasks/.test(p.chatSent().pop().text));
  p.$("#notebtn").click();
  p.pm({ type: "settings", scale: 1, theme: "light", lang: "uk" });
  check("a language switch redraws the rows", p.d.querySelector("#nList .nrow.done .nname em").textContent, "готово");
  check("no script errors", p.errors, []);
  p.close();
}

(async () => {
  await pinTests();
  await pageTests();
  await hiddenTests();
  await langTests();
  console.log(failed ? `\n${failed} failed` : "\nall passed");
  process.exit(failed ? 1 : 0);
})();
