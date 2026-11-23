// Hidden until the stored mode and settings (scale, theme) are read, so the
// window does not flash at the wrong size or in the wrong theme.
// themeColors: Figma puts figma-light / figma-dark on <html> for the System theme.
figma.showUI(__html__, { width: 220, height: 28, title: "Figmosha", themeColors: true, visible: false });

// Build id of this plugin code. The bridge compares it with plugin/code.js on
// disk and asks for a re-Run when they differ, because a running plugin keeps
// the code it started with. Bump it on every change to plugin/ —
// python3 tools/bump_plugin.py does it (or the tools/pre-commit hook).
const PLUGIN_VERSION = "2026-11-23.11";

// Tell the UI which file we're in, so it can register this connection with the
// bridge by name (figma.root.name). The bridge routes --target by that name.
function postIdentity() {
  let fileKey = null;
  try { fileKey = figma.fileKey || null; } catch (e) { /* not always available */ }
  figma.ui.postMessage({
    type: "identity",
    fileKey: fileKey,
    name: figma.root.name,
    docSig: docSignature(),
    pluginVersion: PLUGIN_VERSION,
    notes: notesHere(),
  });
}
// Notes need the annotations of a design file: FigJam, Dev Mode and an old
// Figma have none, and there the window hides the feature.
function notesHere() {
  try {
    const api = figma.annotations;
    return figma.editorType !== "figjam" && figma.editorType !== "dev"
      && !!api && typeof api.getAnnotationCategoriesAsync === "function";
  } catch (e) { return false; }
}

// `figma.fileKey` is null for a local dev plugin and `figma.root.id` is "0:0" in
// every file, so neither identifies the document. The bridge needs an id that is
// the same for one file open in two windows and different for two files that
// share a name, to route a target safely.
//
// Page ids are not enough: every new file starts with a single page "0:1", so two
// fresh "Untitled" files looked like one document and both got the writes meant
// for one of them. So the plugin stores a random id in the document itself, once,
// and reads it back on every run. (A duplicated file copies it; rename one.)
function docSignature() {
  try {
    let id = figma.root.getPluginData("figmosha-doc");
    if (!id) {
      // Random part first, so a short prefix (-T dk2m9q) is already unique.
      id = "d" + Math.random().toString(36).slice(2, 10) + Date.now().toString(36);
      figma.root.setPluginData("figmosha-doc", id);
    }
    return id;
  } catch (e) {
    return pageSignature();
  }
}

// Fallback when plugin data can't be read or written: hash of the page ids.
function pageSignature() {
  try {
    const ids = figma.root.children.map((p) => p.id).join(",");
    let hash = 5381;
    for (let i = 0; i < ids.length; i++) {
      hash = ((hash * 33) ^ ids.charCodeAt(i)) >>> 0;
    }
    return hash.toString(16);
  } catch (e) {
    return null;
  }
}
postIdentity();

// Window mode: the thin bar or the chat, switched by the chat icon in the UI.
// The choice is kept per user in clientStorage and restored on the next run.
const UI_MODES = { bar: [220, 28], chat: [380, 560] };
const UI_MODE_KEY = "figmosha-ui-mode";
// Settings page: interface scale and theme. The window size follows the scale,
// because the UI is zoomed as a whole and would be clipped otherwise.
const SETTINGS_KEY = "figmosha-settings";
const UI_SCALES = [0.9, 1, 1.1, 1.25];
const UI_THEMES = ["system", "light", "dark"];
// Interface and chat language. Settings stored before it existed have no lang: uk.
const UI_LANGS = ["uk", "en"];
const RERUN_HINT = {
  uk: "Запусти Figmosha знову. Він візьме нову версію.",
  en: "Run Figmosha again. It will load the new version.",
};
// Chat window size set by dragging its edges (base px at scale 1), kept per user.
const CHAT_SIZE_KEY = "figmosha-chat-size";
const CHAT_MIN = [320, 400], CHAT_MAX = [900, 1400];
let chatSize = UI_MODES.chat.slice();
let uiMode = "bar";
let uiScale = 1;
let uiLang = "uk";
const NOTES_SET_KEY = "figmosha-notes";   // "off" when the user switched the notes off
function cleanSettings(s) {
  s = s && typeof s === "object" ? s : {};
  return {
    scale: UI_SCALES.indexOf(s.scale) >= 0 ? s.scale : 1,
    theme: UI_THEMES.indexOf(s.theme) >= 0 ? s.theme : "system",
    lang: UI_LANGS.indexOf(s.lang) >= 0 ? s.lang : "uk",
  };
}
function cleanChatSize(v) {
  if (!v || typeof v !== "object" || Array.isArray(v)) return null;
  if (typeof v.w !== "number" || typeof v.h !== "number" || !isFinite(v.w) || !isFinite(v.h)) return null;
  return [Math.min(CHAT_MAX[0], Math.max(CHAT_MIN[0], v.w)), Math.min(CHAT_MAX[1], Math.max(CHAT_MIN[1], v.h))];
}
// {type: "resize", w, h, edge[, done]} from the UI edge zones. Chat mode only.
// A left or top edge (w, nw, sw, n, ne) keeps the opposite side in place: the
// window moves by the size change since the drag start, see docs/CHAT.md (Window size).
let rzAnchor = null;  // {x, y, w, h}: canvas spot and window px size at the drag start
function canvasPos() {
  try {
    if (typeof figma.ui.getPosition !== "function") return null;
    const p = figma.ui.getPosition();
    return cleanChatPos(p && p.canvasSpace);
  } catch (e) { return null; }
}
// Move the window so the side opposite the dragged edge stays where it was at
// the drag start. reposition takes canvas units: window px / zoom.
function keepOppositeSide(edge, size) {
  try {
    const vp = figma.viewport, z = vp && vp.zoom, a = rzAnchor;
    if (!a || !(z > 0) || typeof figma.ui.reposition !== "function") return;
    const w = Math.round(size[0] * uiScale), h = Math.round(size[1] * uiScale);
    figma.ui.reposition(/w$/.test(edge) ? a.x + (a.w - w) / z : a.x,
                        /^n/.test(edge) ? a.y + (a.h - h) / z : a.y);
  } catch (e) { /* old Figma */ }
}
async function setChatSize(msg) {
  if (uiMode !== "chat") return;
  const size = cleanChatSize(msg);
  if (!size) return;
  const edge = typeof msg.edge === "string" ? msg.edge : "";
  const moves = /w$|^n/.test(edge);
  if (!moves) rzAnchor = null;
  else if (!rzAnchor) {
    const c = canvasPos();  // before the resize: the spot at the drag start
    if (c) rzAnchor = { x: c.x, y: c.y, w: Math.round(chatSize[0] * uiScale), h: Math.round(chatSize[1] * uiScale) };
  }
  chatSize = size;
  resizeUi();
  if (moves) keepOppositeSide(edge, size);
  if (msg.done) {
    rzAnchor = null;
    try { await figma.clientStorage.setAsync(CHAT_SIZE_KEY, { w: size[0], h: size[1] }); } catch (e) { /* storage off */ }
    await saveChatPos(readPos());
  }
}
// Chat window position, kept per user in WINDOW space {x, y} (px from the
// Figma window corner, so the same screen spot at any canvas zoom or scroll).
// There is no window-moved event: chat mode polls getPosition once a second
// and stores a new spot once it held still for one tick. Older Figma may lack
// getPosition, reposition or viewport: then nothing happens. See docs/CHAT.md.
const CHAT_POS_KEY = "figmosha-chat-pos";
const POS_POLL_MS = 1000;
let chatPos = null;   // last stored (or restored) chat spot
let posSeen = null;   // last spot the poll saw
let posMoved = false; // it moved since the baseline and is not stored yet
let posTimer = null;
function cleanChatPos(v) {
  if (!v || typeof v !== "object" || Array.isArray(v)) return null;
  if (typeof v.x !== "number" || typeof v.y !== "number" || !isFinite(v.x) || !isFinite(v.y)) return null;
  return { x: v.x, y: v.y };
}
function readPos() {
  try {
    if (typeof figma.ui.getPosition !== "function") return null;
    const p = figma.ui.getPosition();
    return cleanChatPos(p && p.windowSpace);
  } catch (e) { return null; }
}
const samePos = (a, b) => !!a && !!b && a.x === b.x && a.y === b.y;
async function saveChatPos(pos) {
  if (!pos || samePos(pos, chatPos)) return;
  chatPos = pos;
  try { await figma.clientStorage.setAsync(CHAT_POS_KEY, { x: pos.x, y: pos.y }); } catch (e) { /* storage off */ }
}
function restoreChatPos() {
  try {
    const vp = figma.viewport;
    if (!chatPos || !vp || !vp.bounds || typeof figma.ui.reposition !== "function") return;
    const b = vp.bounds, z = vp.zoom;
    if (!(z > 0) || !isFinite(b.x) || !isFinite(b.y)) return;
    // Window space starts at the Figma window corner, the canvas viewport
    // starts lower and to the right (toolbar, left panel). When getPosition
    // gives both spaces, measure that offset from the current spot, so the
    // restored spot does not drift by the panel size on every run.
    let ox = 0, oy = 0;
    try {
      const cur = typeof figma.ui.getPosition === "function" ? figma.ui.getPosition() : null;
      const w = cleanChatPos(cur && cur.windowSpace), c = cleanChatPos(cur && cur.canvasSpace);
      if (w && c) { ox = w.x - (c.x - b.x) * z; oy = w.y - (c.y - b.y) * z; }
    } catch (e) { /* old Figma: viewport starts at the window corner */ }
    const x = Math.min(Math.max(0, ox + b.width * z - 80), Math.max(0, chatPos.x));
    const y = Math.min(Math.max(0, oy + b.height * z - 40), Math.max(0, chatPos.y));
    figma.ui.reposition(b.x + (x - ox) / z, b.y + (y - oy) / z);
  } catch (e) { /* old Figma */ }
}
function pollPos() {
  try {
    const pos = readPos();
    if (!pos) return;
    if (!samePos(pos, posSeen)) { posSeen = pos; posMoved = true; return; }
    if (!posMoved) return;
    posMoved = false;
    return saveChatPos(pos);
  } catch (e) { /* never break the plugin */ }
}
function stopPosPoll() {
  if (posTimer !== null) { clearInterval(posTimer); posTimer = null; }
}
function startPosPoll() {
  stopPosPoll();
  posSeen = readPos();
  posMoved = false;
  posTimer = setInterval(pollPos, POS_POLL_MS);
}
try {
  if (typeof figma.on === "function") figma.on("close", stopPosPoll);
} catch (e) { /* old Figma */ }
function resizeUi() {
  const [w, hgt] = uiMode === "chat" ? chatSize : UI_MODES[uiMode];
  figma.ui.resize(Math.round(w * uiScale), Math.round(hgt * uiScale));
}
async function setSettings(msg) {
  const s = cleanSettings(msg);
  uiScale = s.scale;
  uiLang = s.lang;
  resizeUi();
  try { await figma.clientStorage.setAsync(SETTINGS_KEY, s); } catch (e) { /* storage off */ }
}
async function setUiMode(mode, store) {
  if (!Object.prototype.hasOwnProperty.call(UI_MODES, mode)) return;
  const wasChat = uiMode === "chat";
  if (wasChat && mode !== "chat") { stopPosPoll(); await saveChatPos(readPos()); }
  uiMode = mode;
  resizeUi();
  if (mode === "chat" && !wasChat) { restoreChatPos(); startPosPoll(); }
  if (store) {
    try { await figma.clientStorage.setAsync(UI_MODE_KEY, mode); } catch (e) { /* storage off */ }
  }
  figma.ui.postMessage({ type: "ui-mode", mode });
}
// Welcome screen: shown once per Figma FILE, so the flag lives in the file
// (figma.root pluginData, like figmosha-doc), not in clientStorage. Any
// non-empty value means seen. If the flag can not be read, it is not shown.
const WELCOME_KEY = "figmosha-welcome";
function welcomeSeen() {
  try { return !!figma.root.getPluginData(WELCOME_KEY); } catch (e) { return true; }
}
function welcomeDone() {
  try { figma.root.setPluginData(WELCOME_KEY, "1"); } catch (e) { /* plugin data off */ }
}
// Onboarding: shown once per USER on the very first run at all, so the flag
// lives in clientStorage (shared by all files). Any truthy value means done.
// If the flag can not be read, it is not shown (like the welcome).
const ONBOARD_KEY = "figmosha-onboarded";
async function onboardingDone() {
  try { await figma.clientStorage.setAsync(ONBOARD_KEY, 1); } catch (e) { /* storage off */ }
  // No second intro in a row: this file counts as welcomed too.
  welcomeDone();
}
(async () => {
  try {
    let mode = null, stored = null, onboarded = true;
    try { onboarded = !!(await figma.clientStorage.getAsync(ONBOARD_KEY)); } catch (e) { /* storage off */ }
    try { mode = await figma.clientStorage.getAsync(UI_MODE_KEY); } catch (e) { /* storage off */ }
    try { stored = await figma.clientStorage.getAsync(SETTINGS_KEY); } catch (e) { /* storage off */ }
    let size = null;
    try { size = cleanChatSize(await figma.clientStorage.getAsync(CHAT_SIZE_KEY)); } catch (e) { /* storage off */ }
    if (size) chatSize = size;
    try { chatPos = cleanChatPos(await figma.clientStorage.getAsync(CHAT_POS_KEY)); } catch (e) { /* storage off */ }
    const s = cleanSettings(stored);
    uiScale = s.scale;
    uiLang = s.lang;
    figma.ui.postMessage({ type: "settings", scale: s.scale, theme: s.theme, lang: s.lang });
    // The user can switch the notes off (settings page): on a free plan Figma
    // draws no annotations on the canvas, so some do not want the feature.
    let notesOff = null;
    try { notesOff = await figma.clientStorage.getAsync(NOTES_SET_KEY); } catch (e) { /* storage off */ }
    figma.ui.postMessage({ type: "notes-setting", on: notesOff !== "off" });
    // Separate messages, so "settings" keeps its old shape for older UIs.
    // The onboarding comes first and replaces the welcome on its run.
    const onboarding = !onboarded;
    figma.ui.postMessage({ type: "onboarding", show: onboarding });
    const welcome = !onboarding && !welcomeSeen();
    figma.ui.postMessage({ type: "welcome", show: welcome });
    // Both screens need the chat size. It is not stored as the user's mode.
    if (mode === "chat" || welcome || onboarding) await setUiMode("chat", false);
    else if (uiScale !== 1) resizeUi();
  } finally {
    if (typeof figma.ui.show === "function") figma.ui.show();
  }
})();

// The update card in the chat: Пропустити hides it for one version only.
// Kept per user in clientStorage: { update: "2.4.0", rerun: "<plugin build>" }.
const SKIP_KEY = "figmosha-notice-skip";
async function readSkips() {
  let s = null;
  try { s = await figma.clientStorage.getAsync(SKIP_KEY); } catch (e) { /* storage off */ }
  return s && typeof s === "object" ? s : {};
}
async function postSkips() {
  const s = await readSkips();
  figma.ui.postMessage({
    type: "notice-skips",
    update: typeof s.update === "string" ? s.update : null,
    rerun: typeof s.rerun === "string" ? s.rerun : null,
  });
}
async function skipNotice(kind, version) {
  if ((kind !== "update" && kind !== "rerun") || typeof version !== "string") return;
  const s = await readSkips();
  s[kind] = version;
  try { await figma.clientStorage.setAsync(SKIP_KEY, s); } catch (e) { /* storage off */ }
}
postSkips();

function safeStringify(value) {
  if (value === undefined) return null;
  try { return JSON.parse(JSON.stringify(value)); } catch (e) {
    try { return String(value); } catch (e2) { return null; }
  }
}

// The `result` message for a finished exec. It carries the value once: the
// bridge derives the human-readable `result` text from it. Sending both, as
// before, doubled every payload and pretty-printed objects a third time — a
// 50k-item array took ~0.6 s to come back. `text` is only sent when the value
// can't express it: no return value (logs or "Done"), or NaN / BigInt / etc.
function resultMessage(id, result, logs) {
  const t = typeof result;
  if (result === undefined) {
    return { type: "result", id, value: null, text: asText(result, logs) };
  }
  if (t === "string" || t === "boolean" || (t === "number" && isFinite(result))) {
    return { type: "result", id, value: result };
  }
  if (t === "object") {  // objects, arrays, null
    // Serialized exactly once, here. The UI splices this string into the
    // WebSocket frame as is, so the object is never cloned across postMessage
    // or stringified again (that was 3 passes over a big result).
    try {
      const json = JSON.stringify(result);
      if (json !== undefined) return { type: "result", id, valueJson: json };
    } catch (e) { /* cyclic, BigInt inside… — fall through */ }
    return { type: "result", id, value: safeStringify(result) };
  }
  return { type: "result", id, value: safeStringify(result), text: String(result) };
}

function asText(value, logs) {
  try {
    if (value !== undefined) {
      return typeof value === "object" ? JSON.stringify(value, null, 2) : String(value);
    }
  } catch (e) { /* unserializable — fall back to logs */ }
  return logs.length > 0 ? logs.join("\n") : "Done";
}

// ─── helpers exposed as `h.*` to every exec ──────────────────────────────

// Set for the duration of one exec so helpers can surface warnings through the
// same `print()` the user's code gets. No-op outside an exec.
let CURRENT_PRINT = () => {};

async function resolveVar(varOrId) {
  if (varOrId == null) return null;
  if (typeof varOrId !== "string") return varOrId;

  // Local variables are addressed as "VariableID:1:23"; anything else is a
  // library key, which has to be imported rather than looked up.
  if (varOrId.indexOf("VariableID:") === 0) {
    return await figma.variables.getVariableByIdAsync(varOrId);
  }
  // A library key is not a well-formed id, and getVariableByIdAsync rejects
  // those by throwing rather than returning null — so this has to be guarded,
  // otherwise the import below is unreachable for the very case it exists for.
  let local = null;
  try {
    local = await figma.variables.getVariableByIdAsync(varOrId);
  } catch (e) {
    local = null;
  }
  if (local) return local;

  try {
    return await figma.variables.importVariableByKeyAsync(varOrId);
  } catch (e) {
    return null;
  }
}

// "#1a2b3c" / "1a2b3c" / "#f00" -> {r,g,b} in Figma's 0..1 range.
function hexToRgb(value) {
  let s = String(value).trim().replace(/^#/, "");
  if (s.length === 3) s = s[0] + s[0] + s[1] + s[1] + s[2] + s[2];
  if (!/^[0-9a-fA-F]{6}$/.test(s)) {
    throw new Error("h.hex: expected #RGB or #RRGGBB, got " + JSON.stringify(value));
  }
  const n = parseInt(s, 16);
  return {
    r: ((n >> 16) & 255) / 255,
    g: ((n >> 8) & 255) / 255,
    b: (n & 255) / 255,
  };
}

// Normalise padding given as a number, [v, h], or {top,right,bottom,left}.
function paddingOf(p) {
  if (p == null) return null;
  if (typeof p === "number") return { top: p, right: p, bottom: p, left: p };
  if (Array.isArray(p)) {
    const v = p[0], hz = p.length > 1 ? p[1] : p[0];
    return { top: v, right: hz, bottom: v, left: hz };
  }
  return {
    top: p.top || 0, right: p.right || 0,
    bottom: p.bottom || 0, left: p.left || 0,
  };
}

// Copy a paint array before mutating it — node.fills/strokes are frozen.
function copyPaints(node, prop, who) {
  const paints = node[prop];
  if (typeof paints === "symbol") {
    throw new Error(
      who + ": '" + node.name + "' has mixed " + prop +
      "; set the paint per-range, or unify " + prop + " on the node first"
    );
  }
  if (!Array.isArray(paints)) {
    throw new Error(who + ": '" + node.name + "' has no " + prop);
  }
  return JSON.parse(JSON.stringify(paints));
}

const HELPERS = {
  // Bind fill paint at index to a variable (id or instance)
  async bF(node, idx, varOrId) {
    const v = await resolveVar(varOrId);
    if (!v) throw new Error("h.bF: variable not found: " + varOrId);
    const f = copyPaints(node, "fills", "h.bF");
    if (!f[idx]) throw new Error("h.bF: '" + node.name + "' has no fill at index " + idx);
    f[idx] = figma.variables.setBoundVariableForPaint(f[idx], "color", v);
    node.fills = f;
    return v;
  },

  // Bind stroke paint at index
  async bS(node, idx, varOrId) {
    const v = await resolveVar(varOrId);
    if (!v) throw new Error("h.bS: variable not found: " + varOrId);
    const s = copyPaints(node, "strokes", "h.bS");
    if (!s[idx]) throw new Error("h.bS: '" + node.name + "' has no stroke at index " + idx);
    s[idx] = figma.variables.setBoundVariableForPaint(s[idx], "color", v);
    node.strokes = s;
    return v;
  },

  // Bind numeric property (radii, padding, sizes, itemSpacing, etc.)
  async bN(node, prop, varOrId) {
    const v = await resolveVar(varOrId);
    if (!v) throw new Error("h.bN: variable not found: " + varOrId);
    node.setBoundVariable(prop, v);
    return v;
  },

  // First descendant by exact name
  findByName(root, name) {
    return root.findOne((n) => n.name === name);
  },

  // All descendants by exact name
  findAllByName(root, name) {
    return root.findAll((n) => n.name === name);
  },

  // Dump subtree as indented text
  dumpTree(node, opts) {
    opts = opts || {};
    const maxDepth = opts.maxDepth == null ? 99 : opts.maxDepth;
    const showSize = opts.showSize !== false;
    const showText = opts.showText !== false;
    const showLayout = opts.showLayout === true;
    const lines = [];
    const walk = (n, d) => {
      if (d > maxDepth) return;
      const pad = "  ".repeat(d);
      let line = pad + n.name + " [" + n.type + "] " + n.id;
      if (showSize && n.width !== undefined) {
        line += " " + Math.round(n.width) + "×" + Math.round(n.height);
      }
      if (showLayout && n.layoutMode && n.layoutMode !== "NONE") {
        line += " {" + n.layoutMode[0] +
          " gap:" + n.itemSpacing +
          " pad:" + n.paddingTop + "," + n.paddingRight + "," + n.paddingBottom + "," + n.paddingLeft +
          " " + n.primaryAxisSizingMode + "/" + n.counterAxisSizingMode + "}";
      }
      if (showText && n.type === "TEXT") line += ' "' + n.characters + '"';
      lines.push(line);
      if (n.children) for (const c of n.children) walk(c, d + 1);
    };
    walk(node, 0);
    return lines.join("\n");
  },

  // Load every unique font in subtree, then run async fn
  async withFonts(rootNode, asyncFn) {
    const texts = rootNode.findAll
      ? rootNode.findAll((n) => n.type === "TEXT")
      : (rootNode.type === "TEXT" ? [rootNode] : []);
    const seen = new Set();
    const fonts = [];
    const skipped = [];
    for (const t of texts) {
      // Mixed-font nodes can't be loaded wholesale; editing one later throws a
      // confusing "font not loaded" far from here, so say it out loud now.
      if (typeof t.fontName === "symbol") { skipped.push(t.name); continue; }
      const fn = t.fontName;
      const key = fn.family + "|" + fn.style;
      if (!seen.has(key)) { seen.add(key); fonts.push(fn); }
    }
    if (skipped.length) {
      CURRENT_PRINT(
        "h.withFonts: skipped " + skipped.length + " mixed-font text node(s): " +
        skipped.slice(0, 5).join(", ") + (skipped.length > 5 ? ", …" : "") +
        " — editing them will fail unless you load each range manually"
      );
    }
    await Promise.all(fonts.map((f) => figma.loadFontAsync(f)));
    return await asyncFn();
  },

  // Set a text node's characters with auto font load (single-font texts only)
  async setText(node, text) {
    if (typeof node.fontName === "symbol") {
      throw new Error("h.setText: text '" + node.name + "' has mixed fonts; load each range manually");
    }
    await figma.loadFontAsync(node.fontName);
    node.characters = text;
  },

  // Clone node and place it next to the original
  cloneNext(node, opts) {
    opts = opts || {};
    const direction = opts.direction || "right";
    const gap = opts.gap == null ? 100 : opts.gap;
    const c = node.clone();
    node.parent.appendChild(c);
    if (direction === "right") { c.x = node.x + node.width + gap; c.y = node.y; }
    else if (direction === "left")  { c.x = node.x - node.width - gap; c.y = node.y; }
    else if (direction === "down")  { c.x = node.x; c.y = node.y + node.height + gap; }
    else if (direction === "up")    { c.x = node.x; c.y = node.y - node.height - gap; }
    if (opts.name) c.name = opts.name;
    return c;
  },

  // Set instance variant properties
  async variant(instance, props) {
    await instance.setProperties(props);
    return instance;
  },

  // Available variants for an instance's component
  async variantsOf(instance) {
    const main = await instance.getMainComponentAsync();
    if (!main) return null;
    const set = main.parent && main.parent.type === "COMPONENT_SET" ? main.parent : null;
    return set
      ? { current: main.name, groups: set.variantGroupProperties, all: set.children.map(c => c.name) }
      : { current: main.name, groups: null, all: null };
  },

  // What the user has selected right now — the bridge between "this one here"
  // and a node id you can act on.
  sel() {
    return figma.currentPage.selection.map((n) => ({
      id: n.id, name: n.name, type: n.type,
      w: n.width, h: n.height,
      chars: n.type === "TEXT" ? n.characters : undefined,
    }));
  },

  // Hex string -> {r,g,b}. Hand-rolling this is where the missing /255 lives.
  hex(value) { return hexToRgb(value); },

  // Ready-to-assign paint array: node.fills = h.solid("#1a2b3c")
  solid(value, opacity) {
    const paint = { type: "SOLID", color: hexToRgb(value) };
    if (opacity != null) paint.opacity = opacity;
    return [paint];
  },

  // Create a frame with auto-layout applied in the order Figma demands:
  // into the tree -> layoutMode -> size -> sizing mode -> spacing/padding.
  // Getting that order wrong silently drops the settings.
  frame(parent, opts) {
    opts = opts || {};
    const f = figma.createFrame();
    if (parent) parent.appendChild(f);

    if (opts.name) f.name = opts.name;

    if (opts.layout) {
      const l = String(opts.layout).toUpperCase();
      f.layoutMode = l === "V" ? "VERTICAL" : l === "H" ? "HORIZONTAL" : l;
    }

    if (opts.w != null || opts.h != null) {
      f.resize(opts.w == null ? f.width : opts.w, opts.h == null ? f.height : opts.h);
    }

    if (f.layoutMode && f.layoutMode !== "NONE") {
      // Hug by default on axes the caller didn't pin to a number.
      if (opts.hug !== false) {
        const horizontalIsPrimary = f.layoutMode === "HORIZONTAL";
        const primaryFixed = horizontalIsPrimary ? opts.w != null : opts.h != null;
        const counterFixed = horizontalIsPrimary ? opts.h != null : opts.w != null;
        if (!primaryFixed) f.primaryAxisSizingMode = "AUTO";
        if (!counterFixed) f.counterAxisSizingMode = "AUTO";
      }
      if (opts.spacing != null) f.itemSpacing = opts.spacing;
      if (opts.align) {
        if (opts.align.primary) f.primaryAxisAlignItems = opts.align.primary;
        if (opts.align.counter) f.counterAxisAlignItems = opts.align.counter;
      }
      const pad = paddingOf(opts.padding);
      if (pad) {
        f.paddingTop = pad.top; f.paddingRight = pad.right;
        f.paddingBottom = pad.bottom; f.paddingLeft = pad.left;
      }
    }

    if (opts.fill != null) f.fills = opts.fill === false ? [] : HELPERS.solid(opts.fill);
    if (opts.radius != null) f.cornerRadius = opts.radius;
    return f;
  },

  // Accept "page" / "sel" alongside a real node id, so callers can say
  // "the thing I'm looking at" without first hunting for its id.
  async resolve(idOrAlias) {
    if (idOrAlias === "page") return figma.currentPage;
    if (idOrAlias === "sel") {
      const s = figma.currentPage.selection;
      if (!s.length) throw new Error("nothing selected in Figma");
      return s[0];
    }
    return await figma.getNodeByIdAsync(idOrAlias);
  },

  // Notes: tasks pinned to nodes as annotations of the "Figmosha" category.
  // notes() lists the current page: [{id, i, name, type, text, done, reply}].
  async notes() { return await listNotes(); },
  // Pin a note with a task to a node (a node, an id, or "sel").
  async note(nodeOrId, text) { return await addNote(nodeOrId, text); },
  // Mark the open notes of a node as done and say what was done.
  async noteDone(nodeOrId, reply, i) { return await doneNote(nodeOrId, reply, i); },
  // Remove the notes of a node.
  async noteClear(nodeOrId, i) { return await clearNote(nodeOrId, i); },

  // Quick async accessors
  async node(id)      { return await figma.getNodeByIdAsync(id); },
  async var_(idOrKey) { return await resolveVar(idOrKey); },
  async importComp(key) { return await figma.importComponentByKeyAsync(key); },
  async importVar(key)  { return await figma.variables.importVariableByKeyAsync(key); },
};

// ─── Notes: tasks pinned to nodes (docs/CHAT.md, Notes) ───────────────────
// A note is a native Figma annotation in the category "Figmosha" on the node
// it is about, so it shows on the canvas and is not a layer. A done note moves
// to the category "Figmosha · done" and keeps the agent's reply after DONE_SEP.
// The plugin API writes annotations on any plan; the Figma tool itself needs a
// paid seat, so the plugin window is the way to add one for everybody.
const NOTE_CATS = {
  open: { label: "Figmosha", color: "violet" },
  done: { label: "Figmosha · done", color: "green" },
};
const NOTE_TYPES = ["COMPONENT", "COMPONENT_SET", "ELLIPSE", "FRAME", "INSTANCE", "LINE",
                    "POLYGON", "RECTANGLE", "STAR", "TEXT", "VECTOR"];
const NOTE_MAX = 2000;
const DONE_SEP = "\n\n✓ ";

// {open, done}: the category ids, or null where the file has none yet.
async function noteCats(create) {
  const api = figma.annotations;
  if (!api || typeof api.getAnnotationCategoriesAsync !== "function") {
    throw new Error("notes: this Figma has no annotations");
  }
  const cats = await api.getAnnotationCategoriesAsync();
  const out = { open: null, done: null };
  for (const k of ["open", "done"]) {
    let c = cats.find((x) => x.label === NOTE_CATS[k].label);
    if (!c && create) c = await api.addAnnotationCategoryAsync({ label: NOTE_CATS[k].label, color: NOTE_CATS[k].color });
    out[k] = c ? c.id : null;
  }
  return out;
}
const noteAble = (n) => !!n && NOTE_TYPES.indexOf(n.type) >= 0 && "annotations" in n;
// node.annotations is read-only and frozen: copy what a write accepts.
function plainAnnotation(a) {
  const o = {};
  if (a.labelMarkdown) o.labelMarkdown = a.labelMarkdown;
  else if (a.label) o.label = a.label;
  if (a.properties && a.properties.length) o.properties = a.properties.map((p) => ({ type: p.type }));
  if (a.categoryId) o.categoryId = a.categoryId;
  return o;
}
function noteOf(node, a, i, cats) {
  const done = !!cats.done && a.categoryId === cats.done;
  if (!done && !(cats.open && a.categoryId === cats.open)) return null;
  // Figma rewrites the label of a note it stored as markdown and can add a line
  // break before the reply, so the separator is matched loosely and both parts trimmed.
  const raw = String(a.label || ""), m = done ? /\n{2,}✓ /.exec(raw) : null;
  return {
    id: node.id, i: i, name: node.name, type: node.type, done: done,
    text: (m ? raw.slice(0, m.index) : raw).trim(),
    reply: m ? raw.slice(m.index + m[0].length).trim() : "",
  };
}
// Every note on the current page, in layer order. One scan of the page: about
// 2 s on 60k nodes, so it runs when asked for, never on its own.
async function listNotes() {
  const cats = await noteCats(false);
  if (!cats.open && !cats.done) return [];
  const page = figma.currentPage;
  const nodes = typeof page.findAllWithCriteria === "function"
    ? page.findAllWithCriteria({ types: NOTE_TYPES })
    : page.findAll((n) => NOTE_TYPES.indexOf(n.type) >= 0);
  const out = [];
  for (const n of nodes) {
    const list = n.annotations;
    if (!list || !list.length) continue;
    for (let i = 0; i < list.length; i++) {
      const note = noteOf(n, list[i], i, cats);
      if (note) out.push(note);
    }
  }
  return out;
}
async function noteNode(nodeOrId, who) {
  const node = typeof nodeOrId === "string" ? await HELPERS.resolve(nodeOrId) : nodeOrId;
  if (!node) throw new Error(who + ": node not found: " + nodeOrId);
  if (!noteAble(node)) {
    throw new Error(who + ": a " + node.type + " cannot hold a note; use a frame, an instance, a shape or a text inside it");
  }
  return node;
}
async function addNote(nodeOrId, text) {
  const node = await noteNode(nodeOrId, "h.note");
  const t = String(text == null ? "" : text).trim().slice(0, NOTE_MAX);
  if (!t) throw new Error("h.note: the note has no text");
  const cats = await noteCats(true);
  node.annotations = (node.annotations || []).map(plainAnnotation).concat([{ label: t, categoryId: cats.open }]);
  return noteOf(node, node.annotations[node.annotations.length - 1], node.annotations.length - 1, cats);
}
// Mark the open notes of a node as done (all of them, or the one at index i).
async function doneNote(nodeOrId, reply, i) {
  const node = await noteNode(nodeOrId, "h.noteDone");
  const cats = await noteCats(true);
  const r = String(reply == null ? "" : reply).trim().slice(0, NOTE_MAX);
  let n = 0;
  const next = (node.annotations || []).map((a, k) => {
    if (a.categoryId !== cats.open || (i != null && i !== k)) return plainAnnotation(a);
    n++;
    return { label: String(a.label || "") + (r ? DONE_SEP + r : ""), categoryId: cats.done };
  });
  if (!n) throw new Error("h.noteDone: '" + node.name + "' has no open note");
  node.annotations = next;
  return n;
}
// Remove the notes of a node (all of them, or the one at index i). Other annotations stay.
async function clearNote(nodeOrId, i) {
  const node = await noteNode(nodeOrId, "h.noteClear");
  const cats = await noteCats(false);
  const mine = (a) => (!!cats.open && a.categoryId === cats.open) || (!!cats.done && a.categoryId === cats.done);
  const all = node.annotations || [];
  const next = all.filter((a, k) => !(mine(a) && (i == null || i === k)));
  if (next.length !== all.length) node.annotations = next.map(plainAnnotation);
  return all.length - next.length;
}

// The plugin window side: {type: "notes-load"} (the page opened), {type: "notes-close"},
// {type: "note-add", text} (on the selection),
// {type: "note-focus", id}, {type: "note-remove", id, i, text}.
// Answers are "notes" (the list) and "note-error" (a code the UI words).
async function postNotes(extra) {
  let items = [], error = null;
  try { items = await listNotes(); } catch (e) { error = "noapi"; }
  figma.ui.postMessage(Object.assign({ type: "notes", items: items, error: error }, extra || {}));
}
// While the tasks page is open, the list follows the canvas: a note written in
// Figma itself, by the agent or from the CLI shows up without a reload. Only
// changes that can alter the list count, and a slow scan (a huge page) waits longer.
let noteWatch = null;   // {page, fn, pageFn} while the tasks page is open
let noteTimer = null, noteCost = 0;
function noteTouches(e) {
  const list = (e && e.nodeChanges) || [];
  for (let k = 0; k < list.length; k++) {
    const c = list[k], props = c.properties || [];
    if (c.type !== "PROPERTY_CHANGE" || props.indexOf("annotations") >= 0 || props.indexOf("name") >= 0) return true;
  }
  return false;
}
function noteRescan() {
  if (noteTimer || !noteWatch) return;
  noteTimer = setTimeout(async () => {
    noteTimer = null;
    if (!noteWatch) return;
    const t0 = Date.now();
    await postNotes();
    noteCost = Date.now() - t0;
  }, Math.max(300, noteCost * 3));
}
function noteWatchPage() {
  const w = noteWatch;
  if (!w) return;
  try { if (w.page) w.page.off("nodechange", w.fn); } catch (e) {}
  w.page = figma.currentPage;
  try { w.page.on("nodechange", w.fn); } catch (e) { w.page = null; }
}
function watchNotes(on) {
  if (on === !!noteWatch) return;
  if (on) {
    noteWatch = { page: null, fn: (e) => { if (noteTouches(e)) noteRescan(); }, pageFn: () => { noteWatchPage(); noteRescan(); } };
    noteWatchPage();
    try { figma.on("currentpagechange", noteWatch.pageFn); } catch (e) {}
    return;
  }
  const w = noteWatch;
  noteWatch = null;
  if (noteTimer) { clearTimeout(noteTimer); noteTimer = null; }
  try { if (w.page) w.page.off("nodechange", w.fn); } catch (e) {}
  try { figma.off("currentpagechange", w.pageFn); } catch (e) {}
}
async function noteMessage(msg) {
  try {
    if (msg.type === "notes-load") watchNotes(true);
    if (msg.type === "notes-close") { watchNotes(false); return; }
    if (msg.type === "note-add") {
      const s = figma.currentPage.selection;
      if (!s.length) { figma.ui.postMessage({ type: "note-error", code: "nosel" }); return; }
      if (!noteAble(s[0])) { figma.ui.postMessage({ type: "note-error", code: "unsupported", name: s[0].name }); return; }
      const note = await addNote(s[0], msg.text);
      await postNotes({ added: note });
      return;
    }
    if (msg.type === "note-focus") {
      const node = await figma.getNodeByIdAsync(String(msg.id));
      if (!node || node.removed) { figma.ui.postMessage({ type: "note-error", code: "gone" }); await postNotes(); return; }
      figma.currentPage.selection = [node];
      figma.viewport.scrollAndZoomIntoView([node]);
      return;
    }
    if (msg.type === "note-remove") {
      // The window's list can be stale (the agent or the user changed the notes
      // since), so the index alone could hit another note: the text must match.
      const node = await figma.getNodeByIdAsync(String(msg.id));
      const cats = await noteCats(false);
      const list = node && !node.removed && noteAble(node) ? node.annotations || [] : [];
      const at = (k) => { const n = list[k] && noteOf(node, list[k], k, cats); return !!n && n.text === msg.text; };
      let i = typeof msg.i === "number" && at(msg.i) ? msg.i : -1;
      for (let k = 0; i < 0 && k < list.length; k++) if (at(k)) i = k;
      if (i < 0) figma.ui.postMessage({ type: "note-error", code: "gone" });
      else await clearNote(node, i);
    }
    await postNotes();
  } catch (e) {
    figma.ui.postMessage({ type: "note-error", code: "fail", text: (e && e.message) || String(e) });
  }
}
// ──────────────────────────────────────────────────────────────────────────

// Ids the bridge gave up waiting for. A running script cannot be killed, so
// cancellation is cooperative: long loops call h.ck() and bail out.
const ABORTED = new Set();

figma.ui.onmessage = async (msg) => {
  if (msg.type === "need-identity") { postIdentity(); await postSkips(); return; }
  if (msg.type === "notice-skip") { await skipNotice(msg.kind, msg.version); return; }
  if (msg.type === "rerun") {
    // A plugin cannot restart itself. Close it and say how to run it again.
    figma.closePlugin(RERUN_HINT[uiLang]);
    return;
  }
  if (msg.type === "ui-mode") { await setUiMode(msg.mode, true); return; }
  if (msg.type === "settings") { await setSettings(msg); return; }
  if (msg.type === "notes-setting") {
    if (msg.on !== true) watchNotes(false);
    try { await figma.clientStorage.setAsync(NOTES_SET_KEY, msg.on === true ? "on" : "off"); } catch (e) { /* storage off */ }
    return;
  }
  if (msg.type === "welcome-done") { welcomeDone(); return; }
  if (msg.type === "onboarding-done") { await onboardingDone(); return; }
  if (msg.type === "resize") { await setChatSize(msg); return; }
  if (msg.type === "notes-load" || msg.type === "notes-close" || msg.type === "note-add" || msg.type === "note-focus" || msg.type === "note-remove") {
    await noteMessage(msg);
    return;
  }
  if (msg.type === "open-url") {
    // The Update button. Only ever the project's GitHub pages: the slash keeps out
    // look-alike repos such as figmosha2evil.
    if (typeof msg.url === "string" && msg.url.startsWith("https://github.com/denysosadchyi/figmosha2/")) {
      figma.openExternal(msg.url);
    }
    return;
  }
  if (msg.type === "abort") {
    if (msg.id) ABORTED.add(msg.id);
    return;
  }
  if (msg.type !== "exec") return;
  const { id, code } = msg;
  // When the bridge gives up on this run. Checked by h.ck() against the
  // sandbox's own clock, because the bridge's `abort` message can't get in
  // while a loop only awaits Figma APIs: those promises settle as microtasks,
  // so the plugin never returns to its message queue until the loop ends.
  const deadline = typeof msg.timeout === "number" ? Date.now() + msg.timeout * 1000 : Infinity;

  const logs = [];
  // print() lines go to the bridge in batches, not one message per line: each
  // message crosses two hops (sandbox -> UI -> WebSocket), and 20k separate
  // lines took ~0.5 s. They are still streamed (flushed every 200 lines or
  // 100 ms), so a script that times out still returns the logs it got to.
  let unsent = [];
  let flushTimer = null;
  const flushLogs = () => {
    if (flushTimer !== null) { clearTimeout(flushTimer); flushTimer = null; }
    if (unsent.length) {
      figma.ui.postMessage({ type: "log", id, lines: unsent });
      unsent = [];
    }
  };
  const print = (...args) => {
    const text = args.map((a) =>
      typeof a === "object" ? JSON.stringify(a, null, 2) : String(a)
    ).join(" ");
    logs.push(text);
    unsent.push(text);
    if (unsent.length >= 200) flushLogs();
    else if (flushTimer === null) flushTimer = setTimeout(flushLogs, 100);
  };

  // Per-exec helper view: h.ck() throws once the bridge has given up on this
  // run (its timeout passed, or it sent `abort`), so chunked sweeps stop
  // instead of mutating under the next caller.
  const h = Object.create(HELPERS);
  h.aborted = () => ABORTED.has(id) || Date.now() > deadline;
  h.ck = () => {
    if (h.aborted()) throw new Error("aborted: past this exec's timeout — the bridge stopped waiting");
    return true;
  };

  CURRENT_PRINT = print;
  try {
    const fn = new Function(
      "figma", "print", "h",
      `return (async () => { ${code} })();`
    );
    const result = await fn(figma, print, h);

    ABORTED.delete(id);
    flushLogs();
    figma.ui.postMessage(resultMessage(id, result, logs));
  } catch (e) {
    ABORTED.delete(id);
    flushLogs();
    figma.ui.postMessage({
      type: "error",
      id,
      text: (e && e.message) || String(e),
      stack: (e && e.stack) || null,
    });
  } finally {
    CURRENT_PRINT = () => {};
  }
};
