// Onboarding: the first screen a USER sees on the very first launch of the
// plugin at all (level 1). The per-file welcome screen (#welcome, level 2,
// tests/welcome_dom.test.js) stays as it is. Written before the code (TDD).
// This file tests the UI side (plugin/ui.html) only.
//
// ─── message contract ─────────────────────────────────────────────────────
//
//   Flag: figma.clientStorage "figmosha-onboarded", any truthy value = done.
//         It is per USER (clientStorage), not per file.
//
//   code.js -> ui   { type:"onboarding", show:true|false }, once on start,
//                   after "settings" and before "welcome". When show is true,
//                   code.js also sends { type:"welcome", show:false } and opens
//                   the chat size ({ type:"ui-mode", mode:"chat" }).
//                   No onboarding message (an older code.js) = no screen.
//   ui              If onboarding shows, the welcome screen does not show on
//                   this run, even if a { welcome, show:true } arrives after it.
//   ui -> code.js   { type:"onboarding-done" } exactly once, on the final
//                   button, Enter on it, Esc, or the Skip link. The UI does
//                   NOT post welcome-done: code.js marks the file itself
//                   (setPluginData figmosha-welcome "1").
//   ui -> code.js   Every choice is saved at once with the existing message
//                   { type:"settings", scale, theme, lang } (the same shape as
//                   saveSettings on the settings page). Esc or Skip keeps what
//                   is already chosen and posts no extra settings message.
//
//   DOM names
//     #onboard        section, role=dialog, with aria-label or aria-labelledby,
//                     shown in the chat window instead of #chat and #welcome.
//                     It gets the hidden attribute back when it closes.
//     .ostep          4 steps inside #onboard, exactly one shown at a time:
//                       1 what Figmosha with Claude does (a heading naming
//                         Claude, an inline svg illustration, aria-hidden,
//                         colors only from tokens, and Solar icons, 2px stroke)
//                       2 language: buttons [data-lang="uk"|"en"], role=radio,
//                         aria-checked; Українська carries lang="uk",
//                         English carries lang="en"
//                       3 theme: buttons [data-theme="system"|"light"|"dark"],
//                         role=radio, aria-checked
//                       4 scale: buttons [data-scale="0.9"|"1"|"1.1"|"1.25"],
//                         role=radio, aria-checked
//                     each step has a heading (h2, h3 or role=heading).
//     .odot           4 progress dots; the current one has aria-current="step"
//                     (or the class "on").
//     #onBack         Назад / Back. Hidden or disabled on step 1.
//     #onNext         Далі / Next on steps 1 to 3, Почати / Start on step 4
//                     (step 4 finishes). Enter on it acts once and drops the
//                     default click (preventDefault).
//     #onSkip         Пропустити / Skip link, finishes at once.
//     Focus           #onNext on show; always inside #onboard while it is open
//                     (also after a language re-render); #cInput after finish.
//     Strings         {uk, en}: const ONBOARD_TEXT = { uk, en } (or onb* keys in
//                     I18N). Ukrainian is the default. A language switch
//                     re-renders the onboarding at the same step.
//     CSS             Selectors of the onboarding name #onboard, #onBack,
//                     #onNext, #onSkip or the classes .ostep .odots .odot .oacts
//                     .ohead .oart .olist .orow .oico .ochoice .oseg. Colors only
//                     from var(--*) tokens, radii only var(--r-*) (or 0, 50%,
//                     999px), an entrance animation or transition with a
//                     prefers-reduced-motion: reduce rule for it, and the
//                     actions stay in view (position:sticky; bottom:0 like
//                     .wacts, or a scrolling step area with flex:none actions).
//
//     JSDOM=/path/to/node_modules/jsdom node tests/onboard_dom.test.js
const fs = require("fs");
const path = require("path");
const ROOT = path.join(__dirname, "..");

let JSDOM, VirtualConsole;
try { ({ JSDOM, VirtualConsole } = require(process.env.JSDOM || "jsdom")); }
catch (e) {
  console.log("SKIP onboard_dom.test.js: jsdom is not installed (npm install --no-save jsdom)");
  process.exit(0);
}

const HTML = fs.readFileSync(path.join(ROOT, "plugin", "ui.html"), "utf8");
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

const CYR = /[Ѐ-ӿ]/;

// ─── loader (same as tests/welcome_dom.test.js) ───────────────────────────

async function open({ onboarding, welcome = false, theme = "system", scale = 1, lang, order = "settings-first" } = {}) {
  const posted = [], errors = [];
  const vc = new VirtualConsole();
  vc.on("jsdomError", (e) => errors.push(e.message));
  const dom = new JSDOM(HTML, {
    runScripts: "dangerously", virtualConsole: vc, url: "http://localhost/", pretendToBeVisual: true,
    beforeParse(w) {
      w.WebSocket = class {
        constructor() { this.readyState = 0; setTimeout(() => { this.readyState = 1; this.onopen && this.onopen(); }, 5); }
        send() {}
        close() { this.readyState = 3; this.onclose && this.onclose(); }
      };
      w.WebSocket.OPEN = 1;
      w.parent.postMessage = (m) => posted.push(m.pluginMessage);
      w.Element.prototype.scrollIntoView = function () {};
      w.ResizeObserver = class { observe() {} disconnect() {} };
      w.Element.prototype.setPointerCapture = function (id) { this._cap = id; };
      w.Element.prototype.releasePointerCapture = function () { this._cap = null; };
      w.Element.prototype.hasPointerCapture = function (id) { return this._cap === id; };
    },
  });
  const w = dom.window, d = w.document;
  w.addEventListener("error", (e) => errors.push(e.message));
  const pm = (m) => w.dispatchEvent(new w.MessageEvent("message", { data: { pluginMessage: m } }));
  await wait(20);
  pm({ type: "identity", name: "Banner", docSig: DOC });
  const s = { type: "settings", scale, theme };
  if (lang) s.lang = lang;
  // code.js on start: settings, onboarding, welcome, then the chat size.
  const ob = () => { if (onboarding !== undefined) pm({ type: "onboarding", show: onboarding }); };
  const wm = () => { if (welcome !== undefined) pm({ type: "welcome", show: welcome }); };
  if (order === "settings-first") { pm(s); ob(); wm(); pm({ type: "ui-mode", mode: "chat" }); }
  else { pm({ type: "ui-mode", mode: "chat" }); pm(s); ob(); wm(); }
  await wait();
  const $ = (q) => d.querySelector(q);
  const p = {
    w, d, $, posted, errors, pm,
    done: () => posted.filter((m) => m && m.type === "onboarding-done"),
    wdone: () => posted.filter((m) => m && m.type === "welcome-done"),
    sets: () => posted.filter((m) => m && m.type === "settings"),
    key(k, el) { const ev = new w.KeyboardEvent("keydown", { key: k, bubbles: true, cancelable: true }); (el || d.activeElement || d.body).dispatchEvent(ev); return ev; },
    steps: () => [...d.querySelectorAll("#onboard .ostep")],
    dots: () => [...d.querySelectorAll("#onboard .odot")],
    // Index of the one shown step (-1 if none, -2 if more than one).
    cur() {
      const on = p.steps().map((s, i) => (shown(p, s) ? i : -1)).filter((i) => i >= 0);
      return on.length === 1 ? on[0] : on.length ? -2 : -1;
    },
    curDot: () => p.dots().map((x, i) => (x.getAttribute("aria-current") === "step" || x.classList.contains("on") ? i : -1)).filter((i) => i >= 0),
    btn: (sel) => d.querySelector("#onboard " + sel),
    focusIn: () => { const a = d.activeElement; const box = d.querySelector("#onboard"); return !!(a && box && a !== d.body && box.contains(a)); },
    async next() { const b = d.querySelector("#onNext"); if (b) b.click(); await wait(); },
    async back() { const b = d.querySelector("#onBack"); if (b) b.click(); await wait(); },
    async go(i) { for (let k = 0; k < 8 && p.cur() !== i && p.cur() >= 0; k++) await p.next(); },
    close() { w.close(); },
  };
  return p;
}
// Shown = in the DOM, no hidden ancestor, no display:none on the way up.
function shown(p, el) {
  if (!el) return false;
  for (let n = el; n && n.nodeType === 1; n = n.parentElement) {
    if (n.hidden) return false;
    if (p.w.getComputedStyle(n).display === "none") return false;
  }
  return true;
}
const label = (el) => el ? (el.textContent || "").trim() : null;
// Text of the screen for the language rules: content, aria-label and title.
// Elements tagged with another language (the Українська or English button) are left out.
function screenText(p, lang) {
  const box = p.$("#onboard"); if (!box) return "";
  const c = box.cloneNode(true);
  c.querySelectorAll("[lang]").forEach((e) => { if (e.getAttribute("lang") !== lang) e.remove(); });
  c.querySelectorAll("svg").forEach((e) => e.remove());
  const attrs = [c, ...c.querySelectorAll("[aria-label],[title]")]
    .map((e) => (e.getAttribute("aria-label") || "") + " " + (e.getAttribute("title") || ""));
  return [c.textContent, ...attrs].join(" ");
}

// ─── 1. it appears ────────────────────────────────────────────────────────

async function appears() {
  let p = await open({ onboarding: true });
  const box = p.$("#onboard");
  truthy("onboarding true: #onboard exists", box);
  truthy("onboarding true: #onboard is shown", shown(p, box));
  check("onboarding true: the chat is not shown", shown(p, p.$("#chat")), false);
  check("onboarding true: the welcome screen is not shown", shown(p, p.$("#welcome")), false);
  check("onboarding true: the settings page is not shown", shown(p, p.$("#settings")), false);
  truthy("the window is in chat mode", p.d.body.classList.contains("chatmode"));
  check("4 steps", p.steps().length, 4);
  check("step 1 is the one shown", p.cur(), 0);
  check("4 progress dots", p.dots().length, 4);
  check("the first dot is current", p.curDot(), [0]);
  check("#onNext has the focus", p.d.activeElement && p.d.activeElement.id, "onNext");
  check("nothing is posted on show", p.done().length + p.wdone().length, 0);
  check("no settings message on show", p.sets().length, 0);
  check("no script errors", p.errors, []);
  p.close();

  p = await open({ onboarding: true, order: "mode-first" });
  truthy("ui-mode before onboarding: still shown", shown(p, p.$("#onboard")));
  check("ui-mode before onboarding: #onNext has the focus", p.d.activeElement && p.d.activeElement.id, "onNext");
  p.close();

  // The welcome must not show on the run that shows the onboarding.
  p = await open({ onboarding: true, welcome: true });
  truthy("onboarding and welcome both true: onboarding is shown", shown(p, p.$("#onboard")));
  check("onboarding and welcome both true: welcome is not shown", shown(p, p.$("#welcome")), false);
  await p.go(3); await p.next();
  check("after finish: welcome still not shown on this run", shown(p, p.$("#welcome")), false);
  truthy("after finish: the chat is shown", shown(p, p.$("#chat")));
  check("after finish: no welcome-done from the UI", p.wdone().length, 0);
  check("no script errors", p.errors, []);
  p.close();

  p = await open({ onboarding: false });
  check("onboarding false: no screen", shown(p, p.$("#onboard")), false);
  truthy("onboarding false: the chat is shown", shown(p, p.$("#chat")));
  p.close();

  p = await open({});
  check("no onboarding message (older code.js): no screen", shown(p, p.$("#onboard")), false);
  truthy("no onboarding message: the chat is shown", shown(p, p.$("#chat")));
  p.key("Escape", p.d.body); await wait();
  check("no screen: Esc posts no onboarding-done", p.done().length, 0);
  p.close();

  // Level 2 still works on its own: a seen user, a new file.
  p = await open({ onboarding: false, welcome: true });
  check("onboarding false + welcome true: no onboarding", shown(p, p.$("#onboard")), false);
  truthy("onboarding false + welcome true: the welcome screen is shown", shown(p, p.$("#welcome")));
  check("the welcome composer field has the focus", p.d.activeElement && p.d.activeElement.id, "cInput");
  truthy("the focused field is in the welcome screen", p.$("#welcome").contains(p.d.activeElement));
  p.key("Escape", p.d.activeElement); await wait();
  check("welcome-done still posted once", p.wdone().length, 1);
  check("no onboarding-done from the welcome", p.done().length, 0);
  check("no script errors", p.errors, []);
  p.close();
}

// ─── 2. content of the steps ──────────────────────────────────────────────

async function content() {
  const p = await open({ onboarding: true });
  const box = p.$("#onboard");
  if (!box) { failed++; console.log(" FAIL  #onboard is missing"); p.close(); return; }
  const role = box.getAttribute("role");
  check("role is dialog", role, "dialog");
  const lbl = box.getAttribute("aria-label") || (box.getAttribute("aria-labelledby") && (p.d.getElementById(box.getAttribute("aria-labelledby")) || {}).textContent);
  truthy("the screen has a label", lbl && lbl.trim());
  check("every step has a heading", p.steps().map((s) => !!(s.querySelector("h1, h2, h3, [role=heading]") || {}).textContent), [true, true, true, true]);

  const s1 = p.steps()[0];
  const svgs = s1 ? [...s1.querySelectorAll("svg")] : [];
  const illo = svgs.find((s) => s.getAttribute("aria-hidden") === "true" && s.getAttribute("viewBox") && s.getAttribute("viewBox") !== "0 0 24 24");
  truthy("step 1: an inline svg illustration, hidden from screen readers", illo);
  check("step 1: the illustration has no raster or external image", illo && illo.querySelectorAll("image, use[href^=http], foreignObject").length, 0);
  check("no duplicate ids in the document", (() => {
    const seen = {}, dup = [];
    p.d.querySelectorAll("[id]").forEach((e) => { if (seen[e.id]) dup.push(e.id); seen[e.id] = 1; });
    return dup;
  })(), []);

  const l = [...p.d.querySelectorAll("#onboard [data-lang]")];
  check("step 2: two language buttons", l.map((b) => b.dataset.lang), ["uk", "en"]);
  check("step 2: the language buttons are in step 2", l.map((b) => p.steps()[1] && p.steps()[1].contains(b)), [true, true]);
  check("step 2: each label carries its own lang", l.map((b) => b.getAttribute("lang")), ["uk", "en"]);
  check("step 2: radios", l.map((b) => b.getAttribute("role")), ["radio", "radio"]);
  check("step 2: uk is checked by default", l.map((b) => b.getAttribute("aria-checked")), ["true", "false"]);

  const th = [...p.d.querySelectorAll("#onboard [data-theme]")];
  check("step 3: three theme buttons", th.map((b) => b.dataset.theme), ["system", "light", "dark"]);
  check("step 3: the theme buttons are in step 3", th.map((b) => p.steps()[2] && p.steps()[2].contains(b)), [true, true, true]);
  check("step 3: system is checked by default", th.map((b) => b.getAttribute("aria-checked")), ["true", "false", "false"]);
  check("step 3: radios", th.map((b) => b.getAttribute("role")), ["radio", "radio", "radio"]);

  const sc = [...p.d.querySelectorAll("#onboard [data-scale]")];
  check("step 4: four scale buttons", sc.map((b) => b.dataset.scale), ["0.9", "1", "1.1", "1.25"]);
  check("step 4: the scale buttons are in step 4", sc.map((b) => p.steps()[3] && p.steps()[3].contains(b)), [true, true, true, true]);
  check("step 4: 100% is checked by default", sc.map((b) => b.getAttribute("aria-checked")), ["false", "true", "false", "false"]);

  truthy("Next, Back and Skip are inside #onboard", ["#onNext", "#onBack", "#onSkip"].every((q) => p.$(q) && box.contains(p.$(q))));
  check("Next and Back are buttons", [p.$("#onNext"), p.$("#onBack")].map((b) => b && b.tagName), ["BUTTON", "BUTTON"]);

  truthy("strings live in ONBOARD_TEXT {uk, en} or onb* keys in I18N",
         /const\s+ONBOARD_TEXT\s*=\s*\{/.test(HTML) || (HTML.match(/\bonb[A-Za-z]*\s*:/g) || []).length >= 2);
  check("no script errors", p.errors, []);
  p.close();
}

// ─── 3. Next, Back, Enter ─────────────────────────────────────────────────

async function navigation() {
  const p = await open({ onboarding: true });
  const b = p.$("#onBack");
  truthy("step 1: Back is hidden or disabled", !b || !shown(p, b) || b.disabled);
  if (b) b.click(); await wait();
  check("step 1: Back does nothing", p.cur(), 0);

  await p.next();
  check("Next: step 2", p.cur(), 1);
  check("Next: the second dot is current", p.curDot(), [1]);
  truthy("Next: the focus stays inside #onboard", p.focusIn());
  truthy("step 2: Back is shown and enabled", shown(p, p.$("#onBack")) && !p.$("#onBack").disabled);
  await p.next();
  check("Next: step 3", p.cur(), 2);
  await p.back();
  check("Back: step 2", p.cur(), 1);
  check("Back: the second dot is current", p.curDot(), [1]);
  truthy("Back: the focus stays inside #onboard", p.focusIn());

  // Enter on Next (jsdom does not turn Enter into a click; a browser does both).
  const ev = p.key("Enter", p.$("#onNext"));
  await wait();
  check("Enter on Next: step 3", p.cur(), 2);
  check("Enter on Next: its default click is dropped", ev.defaultPrevented, true);
  truthy("Enter on Next: the focus stays inside #onboard", p.focusIn());
  await p.next();
  check("step 4 reached", p.cur(), 3);
  check("step 4: the last dot is current", p.curDot(), [3]);
  check("nothing finished before the last button", p.done().length, 0);
  truthy("still shown on step 4", shown(p, p.$("#onboard")));
  check("no script errors", p.errors, []);
  p.close();
}

// ─── 4. finish, Esc, Skip ─────────────────────────────────────────────────

async function finish() {
  let p = await open({ onboarding: true });
  await p.go(3);
  await p.next();
  check("finish: onboarding-done is posted exactly once", p.done(), [{ type: "onboarding-done" }]);
  check("finish: the screen is hidden", shown(p, p.$("#onboard")), false);
  check("finish: #onboard gets the hidden attribute back", p.$("#onboard") && p.$("#onboard").hidden, true);
  truthy("finish: the chat is shown", shown(p, p.$("#chat")));
  truthy("finish: still in chat mode", p.d.body.classList.contains("chatmode"));
  check("finish: the chat input has the focus", p.d.activeElement && p.d.activeElement.id, "cInput");
  check("finish: the window was not sent to the bar", p.posted.filter((m) => m && m.type === "ui-mode" && m.mode === "bar").length, 0);
  if (p.$("#onNext")) p.$("#onNext").click();
  p.key("Escape", p.d.body);
  p.key("Enter", p.$("#onNext") || p.d.body);
  await wait();
  check("a second click, Esc or Enter does not post again", p.done().length, 1);
  check("no welcome-done from the UI", p.wdone().length, 0);
  check("no script errors", p.errors, []);
  p.close();

  // Enter on the final button, then the browser click.
  p = await open({ onboarding: true });
  await p.go(3);
  const ev = p.key("Enter", p.$("#onNext"));
  if (p.$("#onNext")) p.$("#onNext").click();
  await wait();
  check("Enter on Почати: finishes once", p.done().length, 1);
  check("Enter on Почати: its default click is dropped", ev.defaultPrevented, true);
  check("Enter on Почати: the chat input has the focus", p.d.activeElement && p.d.activeElement.id, "cInput");
  p.close();

  // Esc on step 1: defaults kept.
  p = await open({ onboarding: true });
  p.key("Escape");
  await wait();
  check("Esc: onboarding-done once", p.done(), [{ type: "onboarding-done" }]);
  check("Esc: the screen is hidden", shown(p, p.$("#onboard")), false);
  truthy("Esc: the chat is shown", shown(p, p.$("#chat")));
  check("Esc: the chat input has the focus", p.d.activeElement && p.d.activeElement.id, "cInput");
  check("Esc: no settings message", p.sets().length, 0);
  check("Esc: defaults kept (no dark, scale 1, uk)",
        [p.d.documentElement.classList.contains("dark"), p.d.documentElement.style.getPropertyValue("--ui-scale"), p.d.documentElement.lang],
        [false, "1", "uk"]);
  check("Esc: no welcome-done", p.wdone().length, 0);
  p.close();

  // Skip from step 2.
  p = await open({ onboarding: true });
  await p.next();
  const sk = p.$("#onSkip");
  truthy("Skip is shown on step 2", shown(p, sk));
  if (sk) sk.click();
  await wait();
  check("Skip: onboarding-done once", p.done(), [{ type: "onboarding-done" }]);
  check("Skip: the screen is hidden", shown(p, p.$("#onboard")), false);
  check("Skip: the chat input has the focus", p.d.activeElement && p.d.activeElement.id, "cInput");
  check("Skip: no settings message", p.sets().length, 0);
  p.close();

  // Esc keeps a choice made before it.
  p = await open({ onboarding: true });
  await p.go(2);
  if (p.btn('[data-theme="dark"]')) p.btn('[data-theme="dark"]').click();
  await wait();
  p.key("Escape");
  await wait();
  truthy("Esc after a theme choice: the dark theme stays", p.d.documentElement.classList.contains("dark"));
  check("Esc after a theme choice: one settings message only", p.sets().length, 1);
  check("Esc after a theme choice: onboarding-done once", p.done().length, 1);
  check("no script errors", p.errors, []);
  p.close();
}

// ─── 5. the choices apply at once and are saved ───────────────────────────

async function choices() {
  // Language.
  let p = await open({ onboarding: true });
  await p.go(1);
  const en = p.btn('[data-lang="en"]');
  if (en) en.click();
  await wait();
  check("en: settings posted with lang en", p.sets().pop(), { type: "settings", scale: 1, theme: "system", lang: "en" });
  check("en: the html lang is en", p.d.documentElement.lang, "en");
  check("en: the chat UI switched too", CYR.test(p.$("#cInput").getAttribute("placeholder")), false);
  check("en: the settings page switched too", CYR.test(label(p.$("#sThemeH"))), false);
  check("en: still on step 2 after the re-render", p.cur(), 1);
  check("en: the second dot is still current", p.curDot(), [1]);
  truthy("en: the focus stays inside #onboard", p.focusIn());
  check("en: the radios follow", [...p.d.querySelectorAll("#onboard [data-lang]")].map((b) => b.getAttribute("aria-checked")), ["false", "true"]);
  check("en: no Cyrillic on the screen", CYR.test(screenText(p, "en")), false);
  await p.next();
  check("en: Next still works after the re-render", p.cur(), 2);
  await p.back();
  const uk = p.btn('[data-lang="uk"]');
  if (uk) uk.click();
  await wait();
  check("back to uk: settings posted with lang uk", p.sets().pop(), { type: "settings", scale: 1, theme: "system", lang: "uk" });
  check("back to uk: the placeholder is Ukrainian", CYR.test(p.$("#cInput").getAttribute("placeholder")), true);
  check("no onboarding-done from a choice", p.done().length, 0);
  check("no script errors", p.errors, []);
  p.close();

  // Theme.
  p = await open({ onboarding: true });
  await p.go(2);
  const dark = p.btn('[data-theme="dark"]');
  if (dark) dark.click();
  await wait();
  truthy("dark: the html class is on at once", p.d.documentElement.classList.contains("dark"));
  check("dark: settings posted", p.sets().pop(), { type: "settings", scale: 1, theme: "dark", lang: "uk" });
  check("dark: the radios follow", [...p.d.querySelectorAll("#onboard [data-theme]")].map((b) => b.getAttribute("aria-checked")), ["false", "false", "true"]);
  check("dark: the settings page dropdown follows", (p.$("#settings select#sTheme") || {}).value, "dark");
  check("dark: still on step 3", p.cur(), 2);
  const light = p.btn('[data-theme="light"]');
  if (light) light.click();
  await wait();
  check("light: the dark class is off", p.d.documentElement.classList.contains("dark"), false);
  check("light: settings posted", p.sets().pop(), { type: "settings", scale: 1, theme: "light", lang: "uk" });
  p.close();

  // Scale.
  p = await open({ onboarding: true });
  await p.go(3);
  const big = p.btn('[data-scale="1.25"]');
  if (big) big.click();
  await wait();
  check("125%: settings posted with scale 1.25", p.sets().pop(), { type: "settings", scale: 1.25, theme: "system", lang: "uk" });
  check("125%: the UI scale is applied", p.d.documentElement.style.getPropertyValue("--ui-scale"), "1.25");
  check("125%: the radios follow", [...p.d.querySelectorAll("#onboard [data-scale]")].map((b) => b.getAttribute("aria-checked")), ["false", "false", "false", "true"]);
  check("125%: still on step 4, not finished", [p.cur(), p.done().length], [3, 0]);
  const small = p.btn('[data-scale="0.9"]');
  if (small) small.click();
  await wait();
  check("90%: settings posted with scale 0.9", p.sets().pop(), { type: "settings", scale: 0.9, theme: "system", lang: "uk" });
  await p.next();
  check("finish after choices: onboarding-done once", p.done().length, 1);
  check("finish after choices: the scale stays", p.d.documentElement.style.getPropertyValue("--ui-scale"), "0.9");
  check("no script errors", p.errors, []);
  p.close();

  // Stored settings from code.js show as the checked choices.
  p = await open({ onboarding: true, lang: "en", theme: "dark", scale: 1.1 });
  check("stored en: no Cyrillic", CYR.test(screenText(p, "en")), false);
  check("stored: the lang radio", [...p.d.querySelectorAll("#onboard [data-lang]")].map((b) => b.getAttribute("aria-checked")), ["false", "true"]);
  check("stored: the theme radio", [...p.d.querySelectorAll("#onboard [data-theme]")].map((b) => b.getAttribute("aria-checked")), ["false", "false", "true"]);
  check("stored: the scale radio", [...p.d.querySelectorAll("#onboard [data-scale]")].map((b) => b.getAttribute("aria-checked")), ["false", "false", "true", "false"]);
  await p.go(3);
  p.close();
}

// ─── 6. texts ─────────────────────────────────────────────────────────────

async function texts() {
  for (const lang of ["uk", "en"]) {
    const p = await open({ onboarding: true, lang });
    let all = "";
    for (let i = 0; i < 4; i++) { all += " " + screenText(p, lang); await p.next(); if (p.done().length) break; }
    if (lang === "uk") {
      truthy("uk: the screen is Ukrainian", CYR.test(all));
    } else {
      check("en: no Cyrillic on any step", (all.match(/[Ѐ-ӿ]+/g) || []), []);
    }
    truthy(lang + ": the screen has real text", all.replace(/\s+/g, " ").trim().length > 80);
    check(lang + ": no script errors", p.errors, []);
    p.close();
  }
  // The source dictionary itself: uk strings have no dashes or quotes.
  const m = HTML.match(/const\s+ONBOARD_TEXT\s*=\s*\{[\s\S]*?\n\};/);
  if (m) {
    const ukPart = (m[0].match(/\buk\s*:\s*\{([\s\S]*?)\n\s{2}\},/) || [])[1] || "";
    const strs = (ukPart.match(/"((?:[^"\\]|\\.)*)"/g) || []).map((s) => s.slice(1, -1)).filter((s) => CYR.test(s));
    truthy("ONBOARD_TEXT.uk has Ukrainian strings", strs.length >= 6);
    const enPart = (m[0].match(/\ben\s*:\s*\{([\s\S]*?)\n\s{2}\},?/) || [])[1] || "";
    check("ONBOARD_TEXT.en: no Cyrillic except the language name", ((enPart.match(/"((?:[^"\\]|\\.)*)"/g) || []).filter((s) => CYR.test(s) && !/Українська/.test(s))), []);
  }
}

// ─── 7. CSS: tokens, radii, motion, fit ───────────────────────────────────

function cssOf(p) { return [...p.d.querySelectorAll("style")].map((s) => s.textContent).join("\n").replace(/\/\*[\s\S]*?\*\//g, ""); }
const OB_SEL = /#onboard|#on(Back|Next|Skip)\b|\.o(step|dots|dot|acts|head|art|list|row|ico|choice|seg)\b/;
// Bodies of every @media (prefers-reduced-motion: reduce) block, braces balanced.
function reduceBlocks(css) {
  const out = []; const re = /@media\s*\(\s*prefers-reduced-motion\s*:\s*reduce\s*\)\s*\{/g; let m;
  while ((m = re.exec(css))) {
    let i = re.lastIndex, depth = 1;
    while (i < css.length && depth) { if (css[i] === "{") depth++; else if (css[i] === "}") depth--; i++; }
    out.push(css.slice(re.lastIndex, i - 1));
  }
  return out;
}

async function styles() {
  const p = await open({ onboarding: true, theme: "dark" });
  truthy("dark theme: the screen is shown", shown(p, p.$("#onboard")));
  const css = cssOf(p);
  const rules = [];
  css.replace(/([^{}]+)\{([^{}]*)\}/g, (all, sel, body) => {
    if (OB_SEL.test(sel) && !/^\s*@/.test(sel.trim()) && !/^\s*(from|to|\d+%)/.test(sel.trim())) rules.push({ sel: sel.trim(), body });
    return all;
  });
  truthy("the onboarding has its own CSS rules (got " + rules.length + ")", rules.length >= 4);
  truthy("there is an entrance animation or transition", rules.some((r) => /\b(animation|transition)\s*:/.test(r.body)));
  truthy("the motion stops for reduced motion",
         reduceBlocks(css).some((b) => OB_SEL.test(b) && /(animation|transition)\s*:\s*none/.test(b)));
  // Fits 380x560 at scale 1 and 1.25: the actions stay in view and nothing is wider than the window.
  const sticky = rules.some((r) => /position\s*:\s*sticky/.test(r.body) && /bottom\s*:\s*0/.test(r.body));
  const scroll = rules.some((r) => /overflow(-y)?\s*:\s*auto/.test(r.body)) && rules.some((r) => /flex\s*:\s*(none|0 0 auto)/.test(r.body));
  truthy("the actions stay in view (sticky, or a scrolling step area with fixed actions)", sticky || scroll);
  truthy("#onboard can shrink in the flex column (min-height:0)", rules.some((r) => /#onboard\b/.test(r.sel) && /min-height\s*:\s*0/.test(r.body)));
  check("no script errors", p.errors, []);
  p.close();
}

// ─── 8. the rest keeps working ────────────────────────────────────────────

async function rest() {
  // Resize zones work while the onboarding is open.
  let p = await open({ onboarding: true });
  const z = [...p.d.querySelectorAll(".rz")];
  check("8 resize zones", z.length, 8);
  check("the zones are shown during the onboarding", z.map((e) => shown(p, e)), z.map(() => true));
  truthy("the zones are outside #onboard", z.every((e) => !p.$("#onboard") || !p.$("#onboard").contains(e)));
  const se = p.d.querySelector('.rz[data-edge="se"]');
  const ptr = (type, x, y) => se.dispatchEvent(new p.w.PointerEvent(type, {
    bubbles: true, cancelable: true, composed: true, pointerId: 7, pointerType: "mouse", isPrimary: true,
    button: 0, buttons: type === "pointerup" ? 0 : 1, clientX: x, clientY: y, screenX: x, screenY: y,
  }));
  ptr("pointerdown", 200, 300); ptr("pointermove", 240, 340); await wait(); ptr("pointerup", 240, 340); await wait();
  truthy("a drag on a corner posts a resize with done", p.posted.some((m) => m && m.type === "resize" && m.done === true));
  truthy("the onboarding is still open after the drag", shown(p, p.$("#onboard")));
  check("no script errors", p.errors, []);
  p.close();

  // The settings page still works after the onboarding.
  p = await open({ onboarding: true });
  p.key("Escape"); await wait();
  p.$("#setbtn").click(); await wait();
  truthy("after onboarding: the settings page opens", p.d.documentElement.classList.contains("settings-open") && shown(p, p.$("#settings")));
  check("after onboarding: #onboard stays hidden", shown(p, p.$("#onboard")), false);
  const sd = p.d.querySelector("#settings select#sTheme");
  sd.value = "dark"; sd.dispatchEvent(new p.w.Event("change", { bubbles: true })); await wait();
  check("after onboarding: a settings pick posts settings", p.sets().pop(), { type: "settings", scale: 1, theme: "dark", lang: "uk" });
  truthy("after onboarding: the theme applies", p.d.documentElement.classList.contains("dark"));
  p.$("#sBack").click(); await wait();
  truthy("after onboarding: back in the chat", shown(p, p.$("#chat")));
  check("the settings page posted no onboarding-done", p.done().length, 1);
  check("no script errors", p.errors, []);
  p.close();

  // A language switch from code.js while the onboarding is open re-renders it.
  p = await open({ onboarding: true });
  await p.next();
  p.pm({ type: "settings", scale: 1, theme: "system", lang: "en" }); await wait();
  check("settings message with en: the onboarding is English", CYR.test(screenText(p, "en")), false);
  check("settings message with en: still on step 2", p.cur(), 1);
  check("settings message with en: no settings echo", p.sets().length, 0);
  check("no script errors", p.errors, []);
  p.close();
}

// ─── 9. guards found by mutation testing ──────────────────────────────────

async function guards() {
  // Arrow keys wrap around inside a radio group.
  let p = await open({ onboarding: true, theme: "system" });
  await p.go(2);
  check("arrows: on step 3", p.cur(), 2);
  const sys = p.btn('[data-theme="system"]');
  sys.focus();
  p.key("ArrowLeft", sys); await wait();
  check("ArrowLeft on the first card wraps to dark", p.sets().pop(), { type: "settings", scale: 1, theme: "dark", lang: "uk" });
  check("ArrowLeft: the dark card has the focus", p.d.activeElement && p.d.activeElement.dataset.theme, "dark");
  p.key("ArrowDown", p.d.activeElement); await wait();
  check("ArrowDown on the last card wraps to system", p.sets().pop(), { type: "settings", scale: 1, theme: "system", lang: "uk" });
  check("no script errors", p.errors, []);
  p.close();

  // The actions bar itself is sticky at the bottom of the scrolling #onboard.
  p = await open({ onboarding: true });
  const bar = p.$("#onNext") && p.$("#onNext").parentElement;
  const cls = bar && bar.classList[0];
  const css = cssOf(p);
  const barRules = [];
  css.replace(/([^{}]+)\{([^{}]*)\}/g, (all, sel, body) => { if (cls && sel.split(",").some((x) => new RegExp("\\." + cls + "\\s*$").test(x.trim()))) barRules.push(body); return all; });
  truthy("the bar with #onNext is position:sticky; bottom:0", barRules.some((b) => /position\s*:\s*sticky/.test(b) && /bottom\s*:\s*0/.test(b)));
  p.close();

  // The settings button during the onboarding finishes it once and opens the settings.
  p = await open({ onboarding: true });
  p.$("#setbtn").click(); await wait();
  check("settings button: onboarding-done posted once", p.done().length, 1);
  check("settings button: #onboard is hidden", shown(p, p.$("#onboard")), false);
  truthy("settings button: the settings page is open", p.d.documentElement.classList.contains("settings-open"));
  check("no script errors", p.errors, []);
  p.close();

  // A welcome that arrived first gives way to the onboarding.
  p = await open({ onboarding: undefined, welcome: true });
  truthy("welcome first: the welcome is shown", shown(p, p.$("#welcome")));
  p.pm({ type: "onboarding", show: true }); await wait();
  truthy("then onboarding: the onboarding is shown", shown(p, p.$("#onboard")));
  check("then onboarding: the welcome is hidden", shown(p, p.$("#welcome")), false);
  check("then onboarding: no welcome-done from the UI", p.wdone().length, 0);
  p.$("#onSkip").click(); await wait();
  check("welcome first, then Skip: the welcome does not come back", shown(p, p.$("#welcome")), false);
  check("welcome first, then Skip: the chat input has the focus", p.d.activeElement && p.d.activeElement.id, "cInput");
  check("welcome first, then Skip: onboarding-done once", p.done().length, 1);
  p.key("Escape"); await wait();
  check("welcome first, then Skip and Esc: still no welcome-done from the UI", p.wdone().length, 0);
  check("no script errors", p.errors, []);
  p.close();

  // When the focused control goes into a hidden step, the focus moves to Next.
  p = await open({ onboarding: true });
  await p.next();
  p.btn('[data-lang="uk"]').focus();
  await p.next();
  check("focus in a step that hides: it moves to #onNext", p.d.activeElement && p.d.activeElement.id, "onNext");
  p.close();

  // A second show:true does not reset the steps, and does not reopen after the finish.
  p = await open({ onboarding: true });
  await p.next(); await p.next();
  p.pm({ type: "onboarding", show: true }); await wait();
  check("a second show true: still on step 3", p.cur(), 2);
  p.key("Escape"); await wait();
  check("Esc: finished once", p.done().length, 1);
  p.pm({ type: "onboarding", show: true }); await wait();
  check("show true after the finish: the onboarding stays closed", shown(p, p.$("#onboard")), false);
  check("no script errors", p.errors, []);
  p.close();
}

(async () => {
  for (const t of [appears, content, navigation, finish, choices, texts, styles, rest, guards]) {
    console.log(`\n${t.name}`);
    try { await t(); } catch (e) { failed++; console.log(" FAIL  threw: " + (e.stack || e).toString().split("\n").slice(0, 3).join(" | ")); }
  }
  console.log(failed ? `\n${failed} failed` : "\nall passed");
  process.exit(failed ? 1 : 0);
})();
