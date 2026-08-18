figma.showUI(__html__, { width: 220, height: 28, title: "Figmosha Bridge" });

// Tell the UI which file we're in, so it can register this connection with the
// bridge by name (figma.root.name). The bridge routes --target by that name.
function postIdentity() {
  let fileKey = null;
  try { fileKey = figma.fileKey || null; } catch (e) { /* not always available */ }
  figma.ui.postMessage({ type: "identity", fileKey: fileKey, name: figma.root.name });
}
postIdentity();

function safeStringify(value) {
  if (value === undefined) return null;
  try { return JSON.parse(JSON.stringify(value)); } catch (e) {
    try { return String(value); } catch (e2) { return null; }
  }
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

  // Quick async accessors
  async node(id)      { return await figma.getNodeByIdAsync(id); },
  async var_(idOrKey) { return await resolveVar(idOrKey); },
  async importComp(key) { return await figma.importComponentByKeyAsync(key); },
  async importVar(key)  { return await figma.variables.importVariableByKeyAsync(key); },
};

// ──────────────────────────────────────────────────────────────────────────

// Ids the bridge gave up waiting for. A running script cannot be killed, so
// cancellation is cooperative: long loops call h.ck() and bail out.
const ABORTED = new Set();

figma.ui.onmessage = async (msg) => {
  if (msg.type === "need-identity") { postIdentity(); return; }
  if (msg.type === "abort") {
    if (msg.id) ABORTED.add(msg.id);
    return;
  }
  if (msg.type !== "exec") return;
  const { id, code } = msg;

  const logs = [];
  const print = (...args) => {
    const text = args.map((a) =>
      typeof a === "object" ? JSON.stringify(a, null, 2) : String(a)
    ).join(" ");
    logs.push(text);
    figma.ui.postMessage({ type: "log", id, text });
  };

  // Per-exec helper view: h.ck() throws once the bridge has abandoned this run,
  // so chunked sweeps stop instead of mutating under the next caller.
  const h = Object.create(HELPERS);
  h.ck = () => {
    if (ABORTED.has(id)) throw new Error("aborted: bridge stopped waiting for this script");
    return true;
  };
  h.aborted = () => ABORTED.has(id);

  CURRENT_PRINT = print;
  try {
    const fn = new Function(
      "figma", "print", "h",
      `return (async () => { ${code} })();`
    );
    const result = await fn(figma, print, h);

    ABORTED.delete(id);
    figma.ui.postMessage({
      type: "result",
      id,
      text: asText(result, logs),
      value: safeStringify(result),
    });
  } catch (e) {
    ABORTED.delete(id);
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
