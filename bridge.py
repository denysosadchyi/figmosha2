#!/usr/bin/env python3
"""Figmosha 2.0 bridge: HTTP -> WS -> Figma plugin -> back.

HTTP API (clients like curl / figmosha CLI talk here):
    POST /exec     {"code": "...", "timeout": 60} -> {ok, result, value, logs, elapsed_ms}
    GET  /status                                  -> {plugin_connected, pending}

WebSocket (the Figma plugin connects here once it's opened in Figma Desktop):
    WS   /plugin

Run:
    python bridge.py                 # default 127.0.0.1:8787
    python bridge.py --port 9000
    python bridge.py --host 0.0.0.0  # expose on LAN (not recommended)
"""

import argparse
import asyncio
import json
import time
import uuid
from aiohttp import web, WSMsgType


PENDING: dict = {}        # rid -> {"future", "logs", "t0"}
PLUGIN_WS: web.WebSocketResponse | None = None
PLUGIN_LAST_SEEN: float = 0.0   # monotonic clock, bumped on every plugin message

# Host: values accepted in the Host header. Populated in main() from the bind
# address. Empty set means "don't check" — chosen when the user binds a
# non-loopback address on purpose (--host 0.0.0.0).
ALLOWED_HOSTS: set = set()


def _guard(request: web.Request, *, allow_null_origin: bool = False):
    """Reject browser-driven requests. Returns an error Response, or None if OK.

    The bridge executes arbitrary JS inside the user's Figma file, so any web
    page the user happens to have open is part of the threat model — binding to
    127.0.0.1 only keeps other machines out, not other tabs.

    Two checks:
      * Origin — local clients (curl, the figmosha CLI) never send this header.
        A browser always does on cross-origin requests, so its mere presence
        means the request came from a page. This blocks CSRF, including the
        "simple request" trick of posting JSON as text/plain to dodge preflight.
      * Host — a page whose DNS is re-pointed at 127.0.0.1 (DNS rebinding)
        becomes same-origin with the bridge and could then read responses.
        Pinning Host to the loopback names we actually serve closes that.
    """
    if ALLOWED_HOSTS:
        host = (request.headers.get("Host") or "").lower()
        if host not in ALLOWED_HOSTS:
            return web.json_response(
                {"ok": False, "error": f"unexpected Host header: {host!r}"}, status=403,
            )

    origin = request.headers.get("Origin")
    if origin is not None:
        # The Figma plugin UI runs in a sandboxed iframe, which reports "null".
        if not (allow_null_origin and origin == "null"):
            return web.json_response(
                {"ok": False,
                 "error": "cross-origin requests are not allowed",
                 "hint": "the bridge only accepts local clients (curl, figmosha CLI) "
                         "and the Figma plugin"},
                status=403,
            )
    return None


ERROR_HINTS = [
    ("fills and strokes variable bindings must be set on paints directly",
     "use h.bF(node, idx, varId) to bind a fill paint to a variable"),
    ("strokes variable bindings must be set on paints directly",
     "use h.bS(node, idx, varId) to bind a stroke paint to a variable"),
    ("Cannot assign to read only property",
     "node.fills/strokes is frozen — copy via JSON.parse(JSON.stringify(...)) before mutating, or use h.bF()/h.bS()"),
    ("permission not specified in manifest",
     "manifest.json missing a permission — edit plugin/manifest.json, sync to /mnt/c/Users/User/figmosha-plugin/, then re-import the plugin in Figma"),
    ("unloaded font",
     "use h.setText(node, text) or h.withFonts(root, fn) — they autoload fonts. Or manually: await figma.loadFontAsync(node.fontName)"),
    ("font has not been loaded",
     "use h.setText(node, text) or h.withFonts(root, fn) — they autoload fonts"),
    ("Cannot find font",
     "fontName may be missing or mixed — check node.fontName before loading"),
    ("appendChild",
     "create node, then parent.appendChild(node) BEFORE setting layoutMode/resize/itemSpacing/padding"),
    ("Unable to find a variant",
     "no variant matches those property values — check available: const v = await h.variantsOf(instance); return v.groups"),
    ("Invalid property name",
     "check available variants: const v = await h.variantsOf(instance); return v.groups"),
    ("Invalid value",
     "check variant values: const v = await h.variantsOf(instance); return v.groups"),
    ("setProperties",
     "if 'Unable to find variant' — check available values via h.variantsOf(instance)"),
    ("not a function",
     "API may be deprecated or renamed — check figma.* available methods, or use Async variants"),
]


def find_hint(error_text):
    if not error_text:
        return None
    low = error_text.lower()
    for needle, hint in ERROR_HINTS:
        if needle.lower() in low:
            return hint
    return None


def _fail_pending(reason: str) -> None:
    """Resolve every in-flight request with an error so clients don't hang."""
    for rid, entry in list(PENDING.items()):
        if not entry["future"].done():
            entry["future"].set_result({"id": rid, "type": "error", "text": reason})


async def _incumbent_answers(timeout: float = 1.0) -> bool:
    """Ping the connected plugin and report whether it replied in time.

    `ws.closed` is not enough to tell a live plugin from a half-open socket: a
    laptop that slept keeps its socket "open" until the heartbeat gives up ~20s
    later. Asking directly distinguishes the two in about a second, which is
    what lets a reconnecting plugin take the slot immediately while a genuine
    second instance still gets turned away.
    """
    ws = PLUGIN_WS
    if ws is None or ws.closed:
        return False
    before = PLUGIN_LAST_SEEN
    try:
        await ws.send_str(json.dumps({"type": "ping"}))
    except Exception:
        return False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        await asyncio.sleep(0.05)
        if PLUGIN_LAST_SEEN > before:
            return True
    return False


async def plugin_ws_handler(request: web.Request):
    global PLUGIN_WS, PLUGIN_LAST_SEEN

    blocked = _guard(request, allow_null_origin=True)
    if blocked is not None:
        print(f"[plugin] refused connection from {request.remote} "
              f"(origin={request.headers.get('Origin')!r})")
        return blocked

    ws = web.WebSocketResponse(heartbeat=20, max_msg_size=16 * 1024 * 1024)
    await ws.prepare(request)

    old = PLUGIN_WS
    if old is not None and not old.closed and await _incumbent_answers():
        # Someone is really there — a second Figma window, or the plugin open in
        # both the stable and Beta apps. Only one may own the slot.
        print(f"[plugin] rejecting second connection from {request.remote}")
        await ws.send_str(json.dumps(
            {"type": "error", "text": "another plugin instance is already connected"}))
        await ws.close(code=1008, message=b"already connected")
        return ws

    PLUGIN_WS = ws
    PLUGIN_LAST_SEEN = time.monotonic()
    if old is not None and not old.closed:
        print("[plugin] superseding a connection that stopped answering")
        _fail_pending("plugin reconnected mid-request")
        try:
            await old.close(code=1012, message=b"superseded")
        except Exception:
            pass

    print(f"[plugin] connected from {request.remote}")

    try:
        async for msg in ws:
            if msg.type == WSMsgType.ERROR:
                print(f"[plugin] ws error: {ws.exception()}")
                break
            if msg.type != WSMsgType.TEXT:
                continue

            if PLUGIN_WS is ws:
                PLUGIN_LAST_SEEN = time.monotonic()

            try:
                m = json.loads(msg.data)
            except json.JSONDecodeError:
                print(f"[plugin] bad json: {msg.data[:200]!r}")
                continue

            mtype = m.get("type")

            if mtype == "hello":
                print(f"[plugin] hello v{m.get('version', '?')}")
                continue
            if mtype == "pong":
                # Answer to _incumbent_answers(); the timestamp above is the
                # whole point, nothing else to do.
                continue

            rid = m.get("id")
            entry = PENDING.get(rid)
            if not entry:
                # late reply for a request that already timed out — drop it
                continue

            if mtype == "log":
                entry["logs"].append(m.get("text", ""))
            elif mtype in ("result", "error"):
                if not entry["future"].done():
                    entry["future"].set_result(m)
    finally:
        # If this socket was already superseded, the requests now in flight
        # belong to its replacement — leave them alone.
        was_active = PLUGIN_WS is ws
        if was_active:
            PLUGIN_WS = None
            print("[plugin] disconnected")
            _fail_pending("plugin disconnected mid-request")
        else:
            print("[plugin] stale connection closed")
    return ws


async def exec_handler(request: web.Request) -> web.Response:
    blocked = _guard(request)
    if blocked is not None:
        return blocked

    if PLUGIN_WS is None or PLUGIN_WS.closed:
        return web.json_response(
            {"ok": False, "error": "plugin not connected — open Figmosha Bridge in Figma"},
            status=503,
        )

    try:
        body = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"ok": False, "error": "invalid JSON body"}, status=400)

    code = body.get("code")
    if not isinstance(code, str) or not code.strip():
        return web.json_response({"ok": False, "error": "missing or empty 'code'"}, status=400)

    timeout = float(body.get("timeout", 60))
    rid = str(uuid.uuid4())
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    PENDING[rid] = {"future": fut, "logs": [], "t0": time.time()}

    try:
        await PLUGIN_WS.send_str(json.dumps({"id": rid, "type": "exec", "code": code}))
    except Exception as e:
        PENDING.pop(rid, None)
        return web.json_response({"ok": False, "error": f"send to plugin failed: {e}"}, status=500)

    try:
        result = await asyncio.wait_for(fut, timeout=timeout)
    except asyncio.TimeoutError:
        PENDING.pop(rid, None)
        return web.json_response(
            {"ok": False, "error": f"timeout after {timeout:.0f}s"}, status=504,
        )

    entry = PENDING.pop(rid)
    elapsed_ms = int((time.time() - entry["t0"]) * 1000)

    if result.get("type") == "error":
        error_text = result.get("text", "unknown error")
        return web.json_response(
            {
                "ok": False,
                "error": error_text,
                "hint": find_hint(error_text),
                "stack": result.get("stack"),
                "logs": entry["logs"],
                "elapsed_ms": elapsed_ms,
            },
            status=500,
        )

    return web.json_response({
        "ok": True,
        "result": result.get("text", ""),
        "value": result.get("value"),
        "logs": entry["logs"],
        "elapsed_ms": elapsed_ms,
    })


async def status_handler(request: web.Request) -> web.Response:
    blocked = _guard(request)
    if blocked is not None:
        return blocked

    return web.json_response({
        "plugin_connected": PLUGIN_WS is not None and not PLUGIN_WS.closed,
        "pending": len(PENDING),
    })


async def root_handler(request: web.Request) -> web.Response:
    blocked = _guard(request)
    if blocked is not None:
        return blocked

    return web.json_response({
        "service": "figmosha-bridge",
        "version": "2.0",
        "endpoints": {
            "POST /exec": "{code, timeout?} -> {ok, result, value, logs, elapsed_ms}",
            "GET /status": "{plugin_connected, pending}",
            "WS /plugin": "Figma plugin connects here",
        },
    })


def build_app() -> web.Application:
    app = web.Application(client_max_size=16 * 1024 * 1024)
    app.router.add_get("/", root_handler)
    app.router.add_get("/status", status_handler)
    app.router.add_post("/exec", exec_handler)
    app.router.add_get("/plugin", plugin_ws_handler)
    return app


def main():
    global ALLOWED_HOSTS

    ap = argparse.ArgumentParser(description="Figmosha 2.0 bridge server")
    ap.add_argument("--host", default="127.0.0.1", help="bind host (default 127.0.0.1)")
    ap.add_argument("--port", type=int, default=8787, help="bind port (default 8787)")
    args = ap.parse_args()

    if args.host in ("127.0.0.1", "localhost", "::1"):
        ALLOWED_HOSTS = {
            f"localhost:{args.port}",
            f"127.0.0.1:{args.port}",
            f"[::1]:{args.port}",
        }
    else:
        # Binding beyond loopback is a deliberate choice, and the reachable
        # hostnames are unknowable from here — skip the Host check and say so.
        print(f"[bridge] WARNING: bound to {args.host} — Host check disabled, "
              f"anyone who can reach this port can run code in your Figma file")

    print(f"[bridge] listening on http://{args.host}:{args.port}")
    print(f"[bridge] plugin should connect to ws://localhost:{args.port}/plugin")
    print(f"[bridge] try: curl -X POST http://localhost:{args.port}/exec "
          f"-H 'Content-Type: application/json' "
          f"-d '{{\"code\":\"return figma.currentPage.name\"}}'")

    web.run_app(build_app(), host=args.host, port=args.port, print=None)


if __name__ == "__main__":
    main()
