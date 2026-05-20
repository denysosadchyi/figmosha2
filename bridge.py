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


async def plugin_ws_handler(request: web.Request) -> web.WebSocketResponse:
    global PLUGIN_WS
    ws = web.WebSocketResponse(heartbeat=20, max_msg_size=16 * 1024 * 1024)
    await ws.prepare(request)

    if PLUGIN_WS is not None and not PLUGIN_WS.closed:
        print(f"[plugin] rejecting second connection from {request.remote}")
        await ws.send_str(json.dumps({"type": "error", "text": "another plugin instance already connected"}))
        await ws.close(code=1008, message=b"already connected")
        return ws

    PLUGIN_WS = ws
    print(f"[plugin] connected from {request.remote}")

    try:
        async for msg in ws:
            if msg.type == WSMsgType.ERROR:
                print(f"[plugin] ws error: {ws.exception()}")
                break
            if msg.type != WSMsgType.TEXT:
                continue

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
        if PLUGIN_WS is ws:
            PLUGIN_WS = None
        print("[plugin] disconnected")
        # Fail any in-flight requests so clients don't hang
        for rid, entry in list(PENDING.items()):
            if not entry["future"].done():
                entry["future"].set_result({
                    "id": rid, "type": "error", "text": "plugin disconnected mid-request",
                })
    return ws


async def exec_handler(request: web.Request) -> web.Response:
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
    fut: asyncio.Future = asyncio.get_event_loop().create_future()
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


async def status_handler(_request: web.Request) -> web.Response:
    return web.json_response({
        "plugin_connected": PLUGIN_WS is not None and not PLUGIN_WS.closed,
        "pending": len(PENDING),
    })


async def root_handler(_request: web.Request) -> web.Response:
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
    ap = argparse.ArgumentParser(description="Figmosha 2.0 bridge server")
    ap.add_argument("--host", default="127.0.0.1", help="bind host (default 127.0.0.1)")
    ap.add_argument("--port", type=int, default=8787, help="bind port (default 8787)")
    args = ap.parse_args()

    print(f"[bridge] listening on http://{args.host}:{args.port}")
    print(f"[bridge] plugin should connect to ws://localhost:{args.port}/plugin")
    print(f"[bridge] try: curl -X POST http://localhost:{args.port}/exec "
          f"-H 'Content-Type: application/json' "
          f"-d '{{\"code\":\"return figma.currentPage.name\"}}'")

    web.run_app(build_app(), host=args.host, port=args.port, print=None)


if __name__ == "__main__":
    main()
