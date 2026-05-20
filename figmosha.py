#!/usr/bin/env python3
"""figmosha — CLI client for the Figmosha 2.0 bridge.

Commands:
    figmosha exec "<js>"             # run arbitrary JS in plugin context
    figmosha exec --file f.js
    figmosha exec --stdin
    figmosha status                   # check server / plugin connection
    figmosha tree <id> [--depth N]    # dump node subtree
    figmosha find <id> <filter>       # find descendants (name=X, name~X, type=X, text=X)
    figmosha text <id> "<new text>"   # set TEXT node characters (autoloads font)
    figmosha variant <id> "P=V" ...   # set INSTANCE variant property values
    figmosha clone <id> [--right|--left|--up|--down] [--gap N] [--name N]
    figmosha rm <id>                  # remove node
    figmosha import-component <key>   # import library component, instantiate, focus
    figmosha "<js>"                   # shorthand for `exec`

Helpers available inside exec'd code (as `h.*`):
    h.bF(node, idx, varOrId)    h.bS(node, idx, varOrId)   h.bN(node, prop, varOrId)
    h.findByName(root, name)    h.findAllByName(root, name)
    h.dumpTree(node, {maxDepth, showSize, showText})
    h.withFonts(root, asyncFn)  h.setText(node, text)
    h.cloneNext(node, {direction, gap, name})
    h.variant(instance, props)  h.variantsOf(instance)
    h.node(id)                  h.var_(id)
    h.importComp(key)           h.importVar(key)
"""

import argparse
import json
import sys
import urllib.error
import urllib.request


HOST = "localhost"
PORT = 8787

KNOWN_CMDS = {
    "exec", "status", "tree", "find", "text", "variant",
    "clone", "rm", "import-component", "icomp",
}


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
    return _request("POST", "/exec", {"code": code, "timeout": timeout})


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
    })
    code = (
        f"const n = await figma.getNodeByIdAsync({json.dumps(args.node_id)});"
        f"if (!n) throw new Error('node not found: ' + {json.dumps(args.node_id)});"
        f"return h.dumpTree(n, {opts});"
    )
    return _emit(_exec(code, args.timeout)[1], raw=args.raw)


def cmd_find(args):
    if "=" not in args.filter and "~" not in args.filter:
        print("figmosha: filter must be key=value or key~value", file=sys.stderr)
        print("  forms: name=X (exact), name~X (substring), type=X, text=X (substring)", file=sys.stderr)
        return 2

    if "~" in args.filter and args.filter.index("~") < args.filter.index("=") if "=" in args.filter else True:
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
        f"const root = await figma.getNodeByIdAsync({json.dumps(args.node_id)});"
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
        f"const n = await figma.getNodeByIdAsync({json.dumps(args.node_id)});"
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
        f"const n = await figma.getNodeByIdAsync({json.dumps(args.node_id)});"
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
        f"const n = await figma.getNodeByIdAsync({json.dumps(args.node_id)});"
        f"if (!n) throw new Error('node not found');"
        f"const c = h.cloneNext(n, {json.dumps(opts)});"
        f"figma.viewport.scrollAndZoomIntoView([n, c]);"
        f"return {{clone_id: c.id, x: c.x, y: c.y, name: c.name}};"
    )
    return _emit(_exec(code, args.timeout)[1], raw=args.raw)


def cmd_rm(args):
    code = (
        f"const n = await figma.getNodeByIdAsync({json.dumps(args.node_id)});"
        f"if (!n) throw new Error('node not found');"
        f"const meta = {{id: n.id, name: n.name, type: n.type}};"
        f"n.remove();"
        f"return meta;"
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
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--port", type=int, default=8787)

    sub = ap.add_subparsers(dest="cmd")

    sub.add_parser("status")

    p_exec = sub.add_parser("exec")
    _add_common_flags(p_exec)
    g = p_exec.add_mutually_exclusive_group()
    g.add_argument("code", nargs="?")
    g.add_argument("--file", "-f")
    g.add_argument("--stdin", action="store_true")

    p_tree = sub.add_parser("tree")
    _add_common_flags(p_tree)
    p_tree.add_argument("node_id")
    p_tree.add_argument("--depth", type=int, default=99)
    p_tree.add_argument("--no-size", action="store_true")
    p_tree.add_argument("--no-text", action="store_true")

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
    p_rm.add_argument("node_id")

    for name in ("import-component", "icomp"):
        p = sub.add_parser(name)
        _add_common_flags(p)
        p.add_argument("key")

    return ap


def main():
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
