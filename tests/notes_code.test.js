// Notes in plugin/code.js: tasks pinned to nodes as Figma annotations
// (docs/CHAT.md, Notes), against a fake Figma. Covers the h.* helpers, the
// messages of the plugin window, and ChatCore.notePrompt from plugin/ui.html.
//
//     node tests/notes_code.test.js
const fs = require("fs");
const path = require("path");
const ROOT = path.join(__dirname, "..");
const SRC = fs.readFileSync(path.join(ROOT, "plugin", "code.js"), "utf8");
const HTML = fs.readFileSync(path.join(ROOT, "plugin", "ui.html"), "utf8");

let failed = 0;
function check(label, actual, expected) {
  const a = JSON.stringify(actual), e = JSON.stringify(expected);
  const ok = a === e;
  if (!ok) failed++;
  console.log(`${ok ? "  ok  " : " FAIL "} ${label}${ok ? "" : `\n         got ${a}\n         want ${e}`}`);
}
const truthy = (label, v) => check(label, !!v, true);
async function throws(label, fn, re) {
  let msg = null;
  try { await fn(); } catch (e) { msg = e.message; }
  check(label, msg !== null && re.test(msg), true);
}

// A fake node. Like Figma, it hands out frozen copies of its annotations and
// refuses a write that carries both label and labelMarkdown.
function node(id, name, type = "FRAME", annotations = []) {
  let list = annotations;
  const n = { id, name, type, removed: false, writes: 0 };
  if (type !== "GROUP" && type !== "SECTION") {
    Object.defineProperty(n, "annotations", {
      enumerable: true,
      get() { return list.map((a) => Object.freeze(Object.assign({}, a))); },
      set(v) {
        v.forEach((a) => { if (a.label && a.labelMarkdown) throw new Error("label and labelMarkdown together"); });
        // Figma fills both fields on read.
        list = v.map((a) => Object.assign({}, a, a.labelMarkdown ? { label: a.labelMarkdown.replace(/\*/g, "") } : {}));
        n.writes++;
      },
    });
  }
  return n;
}

function loadPlugin({ nodes = [], selection = [], cats = [], noApi = false } = {}) {
  const posted = [];
  const categories = cats.slice();
  let nextCat = 10;
  const heard = [];
  const page = {
    name: "Page 1", selection, heard,
    on(ev, fn) { heard.push(fn); },
    off(ev, fn) { const k = heard.indexOf(fn); if (k >= 0) heard.splice(k, 1); },
    findAllWithCriteria({ types }) { return nodes.filter((n) => types.indexOf(n.type) >= 0); },
  };
  const figma = {
    showUI() {},
    ui: { onmessage: null, postMessage(m) { posted.push(m); }, resize() {}, show() {} },
    clientStorage: { stored: {}, async getAsync() { return 1; }, async setAsync(k, v) { this.stored[k] = v; } },
    currentPage: page,
    root: { name: "Banner", children: [{ id: "0:1" }], getPluginData: () => "x", setPluginData() {} },
    getNodeByIdAsync: async (id) => nodes.find((n) => n.id === id) || null,
    viewport: { zoomed: [], scrollAndZoomIntoView(ns) { this.zoomed.push(ns.map((n) => n.id)); } },
    variables: {},
  };
  if (!noApi) {
    figma.annotations = {
      async getAnnotationCategoriesAsync() { return categories.slice(); },
      async addAnnotationCategoryAsync(c) { const x = Object.assign({ id: "1:" + (nextCat++) }, c); categories.push(x); return x; },
    };
  }
  const h = new Function("figma", "__html__", SRC + "\nreturn HELPERS;")(figma, "");
  const send = (m) => figma.ui.onmessage(m);
  const last = (type) => posted.filter((m) => m.type === type).pop();
  return { figma, h, posted, send, last, categories, page };
}

async function helperTests() {
  const a = node("1:4", "Card"), b = node("1:5", "Title", "TEXT"), g = node("1:6", "Group", "GROUP");
  const p = loadPlugin({ nodes: [a, b, g] });

  check("no categories yet: an empty list, nothing created", [await p.h.notes(), p.categories.length], [[], 0]);

  const first = await p.h.note("1:4", "  Make it blue  ");
  check("note() pins the trimmed text", first, { id: "1:4", i: 0, name: "Card", type: "FRAME", done: false, text: "Make it blue", reply: "" });
  check("the first note creates the open and the done category",
        p.categories.map((c) => [c.label, c.color]), [["Figmosha", "violet"], ["Figmosha · done", "green"]]);
  await p.h.note(a, "Round the corners");
  await p.h.note(b, "Shorter text");
  check("categories are made once", p.categories.length, 2);
  check("notes() lists the page in layer order",
        (await p.h.notes()).map((n) => [n.id, n.i, n.text, n.done]),
        [["1:4", 0, "Make it blue", false], ["1:4", 1, "Round the corners", false], ["1:5", 0, "Shorter text", false]]);

  await throws("an empty note is refused", () => p.h.note("1:4", "   "), /no text/);
  await throws("a group cannot hold a note", () => p.h.note("1:6", "x"), /GROUP cannot hold a note/);
  await throws("an unknown node is reported", () => p.h.note("9:9", "x"), /node not found/);
  check("a long note is cut", (await p.h.note(b, "x".repeat(5000))).text.length, 2000);
  await p.h.noteClear(b, 1);

  check("noteDone(i) marks one note", await p.h.noteDone("1:4", "Fill is #1765FF", 1), 1);
  check("the done note keeps the task and the reply",
        (await p.h.notes()).filter((n) => n.id === "1:4").map((n) => [n.i, n.done, n.text, n.reply]),
        [[0, false, "Make it blue", ""], [1, true, "Round the corners", "Fill is #1765FF"]]);
  check("noteDone() without an index marks every open note", await p.h.noteDone("1:4", ""), 1);
  check("a done note with no reply has none", (await p.h.notes())[0], { id: "1:4", i: 0, name: "Card", type: "FRAME", done: true, text: "Make it blue", reply: "" });
  await throws("nothing open: noteDone says so", () => p.h.noteDone("1:4", "again"), /no open note/);

  check("noteClear(i) removes one note", [await p.h.noteClear("1:4", 0), (await p.h.notes()).map((n) => n.text)], [1, ["Round the corners", "Shorter text"]]);
  check("noteClear() removes the rest of the node", [await p.h.noteClear("1:4"), (await p.h.notes()).map((n) => n.id)], [1, ["1:5"]]);
  const writes = a.writes;
  check("noteClear on a node with no notes writes nothing", [await p.h.noteClear("1:4"), a.writes], [0, writes]);

  // Annotations of other categories, and ones written with markdown, survive a rewrite.
  const c = node("1:7", "Mixed", "FRAME", [
    { labelMarkdown: "**Spec**", label: "Spec", categoryId: "1:0", properties: [{ type: "width" }] },
    { label: "plain, no category" },
  ]);
  const q = loadPlugin({ nodes: [c], cats: [{ id: "1:0", label: "Development", color: "green" }] });
  await q.h.note(c, "Task");
  await q.h.noteDone(c, "ok");
  check("foreign annotations are kept as they were", c.annotations.slice(0, 2).map((x) => [x.labelMarkdown || null, x.label, x.categoryId || null, x.properties || null]),
        [["**Spec**", "Spec", "1:0", [{ type: "width" }]], [null, "plain, no category", null, null]]);
  check("only the notes are listed", (await q.h.notes()).map((n) => [n.i, n.text, n.reply]), [[2, "Task", "ok"]]);
  await q.h.noteClear(c);
  check("noteClear leaves the foreign ones", c.annotations.length, 2);

  // Figma's own rewrite of a done note: one more line break before the reply.
  const r = node("1:8", "Rewritten", "FRAME", [{ label: "Round the corners\n\n\n✓ Radius is 16", categoryId: "1:2" }]);
  const z = loadPlugin({ nodes: [r], cats: [{ id: "1:1", label: "Figmosha", color: "violet" }, { id: "1:2", label: "Figmosha · done", color: "green" }] });
  check("a label Figma rewrote still splits into the task and the reply",
        (await z.h.notes()).map((n) => [n.text, n.reply]), [["Round the corners", "Radius is 16"]]);

  const old = loadPlugin({ nodes: [node("1:4", "Card")], noApi: true });
  await throws("a Figma without annotations is reported", () => old.h.notes(), /no annotations/);
}

async function messageTests() {
  const a = node("1:4", "Card"), g = node("1:6", "Group", "GROUP");
  const p = loadPlugin({ nodes: [a, g] });

  await p.send({ type: "notes-load" });
  check("notes-load answers with the list", p.last("notes"), { type: "notes", items: [], error: null });

  await p.send({ type: "note-add", text: "Make it blue" });
  check("no selection: an error code, no note", [p.last("note-error"), a.annotations.length], [{ type: "note-error", code: "nosel" }, 0]);

  p.page.selection = [g];
  await p.send({ type: "note-add", text: "Make it blue" });
  check("a group in the selection: unsupported", p.last("note-error"), { type: "note-error", code: "unsupported", name: "Group" });

  p.page.selection = [a, g];
  await p.send({ type: "note-add", text: "Make it blue" });
  const added = p.last("notes");
  check("note-add pins to the first selected node and sends the list",
        [added.added.id, added.added.text, added.items.map((n) => n.text)], ["1:4", "Make it blue", ["Make it blue"]]);

  await p.send({ type: "note-add", text: "  " });
  check("an empty text fails with a code", p.last("note-error").code, "fail");

  await p.send({ type: "note-focus", id: "1:4" });
  check("note-focus selects the node and zooms to it", [p.page.selection.map((n) => n.id), p.figma.viewport.zoomed], [["1:4"], [["1:4"]]]);
  await p.send({ type: "note-focus", id: "9:9" });
  check("note-focus on a missing node: gone", p.last("note-error"), { type: "note-error", code: "gone" });

  // A stale list: the index points at another note now. The text decides.
  await p.h.note(a, "Second");
  await p.h.noteClear(a, 0);  // "Second" is now at index 0
  await p.send({ type: "note-remove", id: "1:4", i: 1, text: "Second" });
  check("note-remove finds the note by its text", [a.annotations.length, p.last("notes").items], [0, []]);
  await p.h.note(a, "Third");
  await p.send({ type: "note-remove", id: "1:4", i: 0, text: "Not this one" });
  check("note-remove never removes another note", [a.annotations.map((x) => x.label), p.posted[p.posted.length - 2]],
        [["Third"], { type: "note-error", code: "gone" }]);

  // The open tasks page follows the canvas.
  const w = loadPlugin({ nodes: [a], cats: [{ id: "1:1", label: "Figmosha", color: "violet" }] });
  const lists = () => w.posted.filter((m) => m.type === "notes").length;
  const fire = (changes) => w.page.heard.forEach((fn) => fn({ nodeChanges: changes }));
  const tick = () => new Promise((r) => setTimeout(r, 350));
  check("nothing listens before the page opens", w.page.heard.length, 0);
  await w.send({ type: "notes-load" });
  await w.send({ type: "notes-load" });
  check("the open page listens to the canvas, once", [w.page.heard.length, lists()], [1, 2]);
  fire([{ type: "PROPERTY_CHANGE", properties: ["fills", "x"] }]);
  await tick();
  check("a change that cannot touch a note costs no scan", lists(), 2);
  a.annotations = [{ label: "From Figma", categoryId: w.categories[0].id }];
  fire([{ type: "PROPERTY_CHANGE", properties: ["annotations"] }]);
  fire([{ type: "DELETE" }]);
  await tick();
  check("a note written outside the plugin arrives, in one scan",
        [lists(), w.last("notes").items.map((n) => n.text)], [3, ["From Figma"]]);
  await w.send({ type: "notes-close" });
  check("a closed page stops listening", w.page.heard.length, 0);
  await w.h.noteClear(a);

  await w.send({ type: "notes-load" });
  await w.send({ type: "notes-setting", on: false });
  check("switching the notes off stores the choice and stops listening",
        [w.figma.clientStorage.stored["figmosha-notes"], w.page.heard.length], ["off", 0]);
  await w.send({ type: "notes-setting", on: true });
  check("switching them on stores it too", w.figma.clientStorage.stored["figmosha-notes"], "on");

  const idOf = async (q) => { await q.send({ type: "need-identity" }); return q.last("identity").notes; };
  w.figma.editorType = "figma";
  check("a design file offers notes", await idOf(w), true);
  w.figma.editorType = "figjam";
  check("FigJam does not", await idOf(w), false);

  const old = loadPlugin({ nodes: [a], noApi: true });
  check("a Figma without annotations does not", await idOf(old), false);
  await old.send({ type: "notes-load" });
  check("no annotations API: an empty list with an error", old.last("notes"), { type: "notes", items: [], error: "noapi" });
  await old.send({ type: "exec", id: "x1", code: "return 1" });
  check("the exec path still works next to the notes", old.last("result"), { type: "result", id: "x1", value: 1 });
}

function promptTests() {
  const core = HTML.match(/<script id="chat-core">([\s\S]*?)<\/script>/)[1];
  const C = new Function(core + "\nreturn ChatCore;")();
  const one = [{ id: "1:4", i: 2, name: "Card", text: " Make it blue " }];
  const two = one.concat([{ id: "1:5", i: 0, name: "Title", text: "Shorter" }]);
  check("one task, Ukrainian", C.notePrompt(one, "uk"),
        "Виконай задачу з нотатки Figmosha.\n\n«Card» (id 1:4, note 2): Make it blue\n\n"
        + 'Після кожної задачі познач нотатку: await h.noteDone("<id>", "<що зроблено>", <note>).');
  check("several tasks are numbered, English", C.notePrompt(two, "en"),
        "Do the tasks from the Figmosha notes, one by one.\n\n1. «Card» (id 1:4, note 2): Make it blue\n2. «Title» (id 1:5, note 0): Shorter\n\n"
        + 'After each task mark its note: await h.noteDone("<id>", "<what you did>", <note>).');
  truthy("an unknown language falls back to Ukrainian", /^Виконай/.test(C.notePrompt(one, "fr")));
}

(async () => {
  await helperTests();
  await messageTests();
  promptTests();
  console.log(failed ? `\n${failed} failed` : "\nall passed");
  process.exit(failed ? 1 : 0);
})();
