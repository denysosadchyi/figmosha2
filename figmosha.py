#!/usr/bin/env python3
"""figmosha — CLI client for the Figmosha 2.0 bridge.

Commands:
    figmosha exec "<js>"             # run arbitrary JS in plugin context
    figmosha exec --file f.js
    figmosha exec --stdin
    figmosha status                   # check server / plugin connection
    figmosha doctor                   # diagnose the whole chain, with fixes
    figmosha sel                      # what is selected in Figma right now
    figmosha tree <id> [--depth N] [--layout]
    figmosha find <id> <filter>       # find descendants (name=X, name~X, type=X, text=X)
    figmosha text <id> "<new text>"   # set TEXT node characters (autoloads font)
    figmosha variant <id> "P=V" ...   # set INSTANCE variant property values
    figmosha clone <id> [--right|--left|--up|--down] [--gap N] [--name N]
    figmosha rm <id> [<id> ...]       # remove one or more nodes
    figmosha import-component <key>   # import library component, instantiate, focus
    figmosha "<js>"                   # shorthand for `exec`

Anywhere a node id is taken, `page` (current page) and `sel` (first selected
node) work too. Bridge address comes from FIGMOSHA_HOST / FIGMOSHA_PORT.

Helpers available inside exec'd code (as `h.*`):
    h.bF(node, idx, varOrId)    h.bS(node, idx, varOrId)   h.bN(node, prop, varOrId)
    h.findByName(root, name)    h.findAllByName(root, name)
    h.dumpTree(node, {maxDepth, showSize, showText, showLayout})
    h.withFonts(root, asyncFn)  h.setText(node, text)
    h.cloneNext(node, {direction, gap, name})
    h.variant(instance, props)  h.variantsOf(instance)
    h.sel()                     h.resolve(idOrAlias)
    h.hex("#1a2b3c")            h.solid("#1a2b3c", opacity?)
    h.frame(parent, {layout, w, h, spacing, padding, align, fill, radius, name})
    h.node(id)                  h.var_(idOrKey)
    h.importComp(key)           h.importVar(key)
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request


DEFAULT_HOST = os.environ.get("FIGMOSHA_HOST", "localhost")
DEFAULT_PORT = int(os.environ.get("FIGMOSHA_PORT", "8787"))

HOST = DEFAULT_HOST
PORT = DEFAULT_PORT

KNOWN_CMDS = {
    "exec", "status", "doctor", "sel", "tree", "find", "text", "variant",
    "clone", "rm", "import-component", "icomp",
}


def force_utf8_output():
    """Windows consoles default to cp1252 and raise on anything outside it.

    Layer names are routinely Cyrillic, CJK or emoji, so without this a plain
    `figmosha tree` blows up with UnicodeEncodeError instead of printing.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def node_expr(id_or_alias):
    """JS that resolves a node id, or the aliases `page` / `sel`."""
    return f"await h.resolve({json.dumps(id_or_alias)})"


def _request(method, path, payload=None, timeout=65):
    url = f"http://{HOST}:{PORT}{path}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        body = e.read() or b"{}"
        try:
            return e.code, json.loads(body)
        except json.JSONDecodeError:
            return e.code, {"ok": False, "error": body.decode("utf-8", "replace")}
    except urllib.error.URLError as e:
        return 0, {"ok": False, "error": f"connection: {e.reason}"}
    except TimeoutError:
        return 0, {"ok": False, "error": "request timed out"}


def _exec(code, timeout=60):
    # The socket deadline has to outlast the server-side one, otherwise a long
    # --timeout dies here at the default 65s while the bridge is still waiting,
    # and we lose the bridge's own error payload (including any hint).
    return _request("POST", "/exec", {"code": code, "timeout": timeout},
                    timeout=timeout + 5)


def _emit(resp, raw=False):
    if raw:
        print(json.dumps(resp, indent=2))
        return 0 if resp.get("ok") else 1

    for line in resp.get("logs") or []:
        print(f"  log: {line}", file=sys.stderr)

    if resp.get("ok") is False:
        print(f"figmosha: {resp.get('error', 'unknown')}", file=sys.stderr)
        if resp.get("hint"):
            print(f"   hint: {resp['hint']}", file=sys.stderr)
        if resp.get("stack"):
            print(resp["stack"], file=sys.stderr)
        return 1

    if resp.get("result"):
        print(resp["result"])
    print(f"  ({resp.get('elapsed_ms', '?')}ms)", file=sys.stderr)
    return 0


# ─── command builders ─────────────────────────────────────────────────────

def cmd_status(args):
    status, resp = _request("GET", "/status")
    print(json.dumps(resp, indent=2))
    return 0 if status == 200 else 2


def cmd_sel(args):
    return _emit(_exec("return h.sel();", args.timeout)[1], raw=args.raw)


def cmd_doctor(args):
    """Walk the chain bridge -> plugin -> Figma, naming the fix at each break."""
    def ok(text):
        print(f"  ✓  {text}")

    def fail(text, fix):
        print(f"  ✗  {text}")
        print(f"     → {fix}")

    status, resp = _request("GET", "/status")
    if status == 0:
        fail(f"bridge not answering on {HOST}:{PORT} — {resp.get('error')}",
             "start it:  bash start-bridge.sh   (Windows: .\\start-bridge.ps1)")
        return 1
    if status == 403:
        fail(f"bridge refused the request: {resp.get('error')}",
             "a proxy is rewriting Host/Origin — talk to the bridge directly")
        return 1
    ok(f"bridge answering on {HOST}:{PORT}")

    if not resp.get("plugin_connected"):
        fail("plugin not connected",
             "in Figma Desktop: Plugins → Development → Figmosha Bridge")
        return 1
    ok("plugin connected")

    status, r = _exec("return 1 + 1;", 10)
    if not r.get("ok") or r.get("value") != 2:
        fail(f"round trip failed: {r.get('error', r)}",
             "close the plugin window in Figma and run it again")
        return 1
    ok(f"round trip works ({r.get('elapsed_ms', '?')}ms)")

    status, r = _exec(
        "return {file: figma.root.name, page: figma.currentPage.name, "
        "pages: figma.root.children.length};", 10)
    if r.get("ok"):
        v = r.get("value") or {}
        ok(f"editing «{v.get('file')}» — page «{v.get('page')}» "
           f"of {v.get('pages')}")
        print("\n  The plugin is bound to whichever file was open when you ran it.")
        print("  Switched files? Run the plugin again in the new one.")
    return 0


def cmd_exec(args):
    if args.file:
        with open(args.file, encoding="utf-8") as f:
            code = f.read()
    elif args.stdin:
        code = sys.stdin.read()
    elif args.code:
        code = args.code
    else:
        print("figmosha: provide code (positional, --file, or --stdin)", file=sys.stderr)
        return 2
    return _emit(_exec(code, args.timeout)[1], raw=args.raw)


def cmd_tree(args):
    opts = json.dumps({
        "maxDepth": args.depth,
        "showSize": not args.no_size,
        "showText": not args.no_text,
        "showLayout": args.layout,
    })
    code = (
        f"const n = {node_expr(args.node_id)};"
        f"if (!n) throw new Error('node not found: ' + {json.dumps(args.node_id)});"
        f"return h.dumpTree(n, {opts});"
    )
    return _emit(_exec(code, args.timeout)[1], raw=args.raw)


def cmd_find(args):
    if "=" not in args.filter and "~" not in args.filter:
        print("figmosha: filter must be key=value or key~value", file=sys.stderr)
        print("  forms: name=X (exact), name~X (substring), type=X, text=X (substring)", file=sys.stderr)
        return 2

    # Whichever separator comes first decides the mode, so a value may contain
    # the other character (name~a=b is a substring search for "a=b").
    tilde = args.filter.find("~")
    equals = args.filter.find("=")
    substring_mode = tilde != -1 and (equals == -1 or tilde < equals)

    if substring_mode:
        key, value = args.filter.split("~", 1)
        if key == "name":
            predicate = f"n.name.includes({json.dumps(value)})"
        elif key == "text":
            predicate = f"n.type === 'TEXT' && n.characters.includes({json.dumps(value)})"
        else:
            print(f"figmosha: substring filter only supports name~ and text~ (got {key}~)", file=sys.stderr)
            return 2
    else:
        key, value = args.filter.split("=", 1)
        if key == "name":
            predicate = f"n.name === {json.dumps(value)}"
        elif key == "type":
            predicate = f"n.type === {json.dumps(value.upper())}"
        elif key == "text":
            predicate = f"n.type === 'TEXT' && n.characters === {json.dumps(value)}"
        else:
            print(f"figmosha: unknown filter key '{key}'. Use name, type, text", file=sys.stderr)
            return 2

    code = (
        f"const root = {node_expr(args.node_id)};"
        f"if (!root) throw new Error('node not found: ' + {json.dumps(args.node_id)});"
        f"if (!root.findAll) throw new Error('node has no findAll (type: ' + root.type + ')');"
        f"return root.findAll(n => {predicate}).map(n => ({{"
        f"id: n.id, name: n.name, type: n.type, w: n.width, h: n.height,"
        f"chars: n.type === 'TEXT' ? n.characters : undefined"
        f"}}));"
    )
    return _emit(_exec(code, args.timeout)[1], raw=args.raw)


def cmd_text(args):
    code = (
        f"const n = {node_expr(args.node_id)};"
        f"if (!n) throw new Error('node not found');"
        f"if (n.type !== 'TEXT') throw new Error('not a TEXT node (got ' + n.type + ')');"
        f"const before = n.characters;"
        f"await h.setText(n, {json.dumps(args.text)});"
        f"return {{id: n.id, before, after: n.characters}};"
    )
    return _emit(_exec(code, args.timeout)[1], raw=args.raw)


def cmd_variant(args):
    props = {}
    for kv in args.props:
        if "=" not in kv:
            print(f"figmosha: variant prop must be 'Property=Value', got {kv!r}", file=sys.stderr)
            return 2
        k, v = kv.split("=", 1)
        props[k.strip()] = v.strip()

    code = (
        f"const n = {node_expr(args.node_id)};"
        f"if (!n) throw new Error('node not found');"
        f"if (n.type !== 'INSTANCE') throw new Error('not an INSTANCE (got ' + n.type + ')');"
        f"await n.setProperties({json.dumps(props)});"
        f"const out = {{}};"
        f"for (const k in n.componentProperties) out[k] = n.componentProperties[k].value;"
        f"return {{id: n.id, applied: {json.dumps(props)}, current: out}};"
    )
    return _emit(_exec(code, args.timeout)[1], raw=args.raw)


def cmd_clone(args):
    direction = "right"
    for d in ("left", "right", "up", "down"):
        if getattr(args, d, False):
            direction = d
    opts = {"direction": direction, "gap": args.gap}
    if args.name:
        opts["name"] = args.name

    code = (
        f"const n = {node_expr(args.node_id)};"
        f"if (!n) throw new Error('node not found');"
        f"const c = h.cloneNext(n, {json.dumps(opts)});"
        f"figma.viewport.scrollAndZoomIntoView([n, c]);"
        f"return {{clone_id: c.id, x: c.x, y: c.y, name: c.name}};"
    )
    return _emit(_exec(code, args.timeout)[1], raw=args.raw)


def cmd_rm(args):
    # Cleaning up after an experiment is almost always plural.
    code = (
        f"const ids = {json.dumps(args.node_ids)};"
        f"const removed = [], missing = [];"
        f"for (const id of ids) {{"
        f"  const n = await h.resolve(id);"
        f"  if (!n) {{ missing.push(id); continue; }}"
        f"  removed.push({{id: n.id, name: n.name, type: n.type}});"
        f"  n.remove();"
        f"}}"
        f"if (missing.length) throw new Error('node not found: ' + missing.join(', ') + "
        f"  (removed.length ? ' (removed ' + removed.length + ' before that)' : ''));"
        f"return removed;"
    )
    return _emit(_exec(code, args.timeout)[1], raw=args.raw)


def cmd_import_component(args):
    code = (
        f"const comp = await figma.importComponentByKeyAsync({json.dumps(args.key)});"
        f"const inst = comp.createInstance();"
        f"figma.currentPage.appendChild(inst);"
        f"figma.viewport.scrollAndZoomIntoView([inst]);"
        f"return {{component: comp.name, instance_id: inst.id, w: inst.width, h: inst.height}};"
    )
    return _emit(_exec(code, args.timeout)[1], raw=args.raw)


# ─── argparse / dispatch ───────────────────────────────────────────────────

def _add_common_flags(p):
    p.add_argument("--timeout", "-t", type=int, default=60)
    p.add_argument("--raw", action="store_true", help="print full JSON response")


def build_parser():
    ap = argparse.ArgumentParser(prog="figmosha", description=__doc__.splitlines()[0],
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default=DEFAULT_HOST,
                    help="bridge host (env FIGMOSHA_HOST)")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT,
                    help="bridge port (env FIGMOSHA_PORT)")

    sub = ap.add_subparsers(dest="cmd")

    sub.add_parser("status")

    p_doctor = sub.add_parser("doctor", help="diagnose the bridge -> plugin -> Figma chain")
    _add_common_flags(p_doctor)

    p_sel = sub.add_parser("sel", help="what is selected in Figma right now")
    _add_common_flags(p_sel)

    p_exec = sub.add_parser("exec")
    _add_common_flags(p_exec)
    g = p_exec.add_mutually_exclusive_group()
    g.add_argument("code", nargs="?")
    g.add_argument("--file", "-f")
    g.add_argument("--stdin", action="store_true")

    p_tree = sub.add_parser("tree")
    _add_common_flags(p_tree)
    p_tree.add_argument("node_id", help="node id, or `page` / `sel`")
    p_tree.add_argument("--depth", type=int, default=99)
    p_tree.add_argument("--no-size", action="store_true")
    p_tree.add_argument("--no-text", action="store_true")
    p_tree.add_argument("--layout", action="store_true",
                        help="show layoutMode, gap, padding and sizing modes")

    p_find = sub.add_parser("find")
    _add_common_flags(p_find)
    p_find.add_argument("node_id")
    p_find.add_argument("filter", help="name=X | name~X | type=X | text=X | text~X")

    p_text = sub.add_parser("text")
    _add_common_flags(p_text)
    p_text.add_argument("node_id")
    p_text.add_argument("text")

    p_variant = sub.add_parser("variant")
    _add_common_flags(p_variant)
    p_variant.add_argument("node_id")
    p_variant.add_argument("props", nargs="+")

    p_clone = sub.add_parser("clone")
    _add_common_flags(p_clone)
    p_clone.add_argument("node_id")
    p_clone.add_argument("--right", action="store_true")
    p_clone.add_argument("--left", action="store_true")
    p_clone.add_argument("--up", action="store_true")
    p_clone.add_argument("--down", action="store_true")
    p_clone.add_argument("--gap", type=int, default=100)
    p_clone.add_argument("--name")

    p_rm = sub.add_parser("rm")
    _add_common_flags(p_rm)
    p_rm.add_argument("node_ids", nargs="+", help="one or more node ids, or `sel`")

    for name in ("import-component", "icomp"):
        p = sub.add_parser(name)
        _add_common_flags(p)
        p.add_argument("key")

    return ap


def main():
    force_utf8_output()

    # Backward-compat shorthand: `figmosha "<js>"` → `figmosha exec "<js>"`
    if (
        len(sys.argv) >= 2
        and sys.argv[1] not in KNOWN_CMDS
        and not sys.argv[1].startswith("-")
    ):
        sys.argv.insert(1, "exec")

    ap = build_parser()
    args = ap.parse_args()

    if args.cmd is None:
        ap.print_help()
        sys.exit(2)

    global HOST, PORT
    HOST = args.host
    PORT = args.port

    dispatch = {
        "status": cmd_status,
        "doctor": cmd_doctor,
        "sel": cmd_sel,
        "exec": cmd_exec,
        "tree": cmd_tree,
        "find": cmd_find,
        "text": cmd_text,
        "variant": cmd_variant,
        "clone": cmd_clone,
        "rm": cmd_rm,
        "import-component": cmd_import_component,
        "icomp": cmd_import_component,
    }
    sys.exit(dispatch[args.cmd](args))


if __name__ == "__main__":
    main()
