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

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sys
import time
import uuid
from pathlib import Path

from aiohttp import web, WSMsgType


PENDING: dict = {}        # rid -> {"future", "logs", "t0", "conn"}
PLUGINS: dict = {}        # conn_id -> {"ws", "fileKey", "name", "docSig"}
SLOT_PROBE_TIMEOUT = 1.0  # how long an incumbent connection has to answer a ping
LOCKS: dict = {}          # conn_id -> asyncio.Lock — one exec at a time per file
ABANDONED: dict = {}      # rid -> {"conn", "t0", "timeout"} — timed out, may still be running

# Host: values accepted in the Host header. Populated in main() from the bind
# address. Empty set means "don't check" — chosen when the user binds a
# non-loopback address on purpose (--host 0.0.0.0).
ALLOWED_HOSTS: set = set()

# ─── version checks ───────────────────────────────────────────────────────
# A running plugin keeps the code it started with, and a running bridge keeps
# the bridge.py it started with. After a `git pull` both can be stale without
# any sign of it, so compare what is running against what is on disk.

HERE = Path(__file__).resolve().parent
PLUGIN_CODE = HERE / "plugin" / "code.js"
_VERSION_RE = re.compile(r'PLUGIN_VERSION\s*=\s*"([^"]+)"')


def _file_digest(path: Path):
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


BRIDGE_DIGEST = _file_digest(Path(__file__).resolve())


def expected_plugin_version():
    """PLUGIN_VERSION as written in plugin/code.js right now, or None."""
    try:
        m = _VERSION_RE.search(PLUGIN_CODE.read_text(encoding="utf-8"))
    except OSError:
        return None
    return m.group(1) if m else None


def plugin_outdated(info) -> bool:
    """True when this connection runs older plugin code than plugin/code.js.

    A plugin from before version reporting sends nothing — that is outdated too.
    """
    expected = expected_plugin_version()
    return expected is not None and info.get("pluginVersion") != expected


def bridge_outdated() -> bool:
    """True when bridge.py on disk differs from the one this process loaded."""
    return _file_digest(Path(__file__).resolve()) != BRIDGE_DIGEST


def _lock_for(conn_id):
    """One lock per connected file.

    The Figma plugin sandbox runs a single-threaded async message handler, so two
    concurrent execs interleave at every `await` inside them — over one shared
    document and one shared undo stack. Serializing per connection is what makes
    concurrent callers (orchestrator + agents) safe on the same file.
    """
    lock = LOCKS.get(conn_id)
    if lock is None:
        lock = LOCKS[conn_id] = asyncio.Lock()
    return lock


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
     "manifest.json missing a permission — edit plugin/manifest.json (the file Figma loads directly), then re-import the plugin in Figma"),
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


async def _still_alive(info, timeout: float = SLOT_PROBE_TIMEOUT) -> bool:
    """Ping one plugin connection and wait for its pong.

    An open socket is not a live plugin: a laptop that slept, or a Figma tab that
    was closed uncleanly, leaves a socket the OS has not torn down yet. Only an
    answered ping distinguishes that from a plugin that is simply idle — which
    matters because the same file legitimately open in two tabs also produces two
    live registrations, and those must NOT evict each other.
    """
    ws = info["ws"]
    if ws.closed:
        return False
    fut = asyncio.get_event_loop().create_future()
    info["pong"] = fut
    try:
        await ws.send_str(json.dumps({"type": "ping"}))
    except Exception:
        info.pop("pong", None)
        return False
    try:
        await asyncio.wait_for(fut, timeout)
        return True
    except (asyncio.TimeoutError, asyncio.CancelledError):
        return False
    finally:
        info.pop("pong", None)


def _label(conn_id: str) -> str:
    info = PLUGINS.get(conn_id) or {}
    return info.get("name") or f"({conn_id[:8]})"


async def _broadcast_peers():
    """Tell every connected plugin its position and the total, for the "1/2" badge.

    Only connections that have said `hello` count: one that just opened is not a
    file yet, and counting it made the badge flash "1/3" during a reconnect.
    """
    live = [(cid, i) for cid, i in _live_plugins() if i.get("hello")]
    for idx, (_, info) in enumerate(live, 1):
        try:
            await info["ws"].send_str(json.dumps(
                {"type": "peers", "index": idx, "total": len(live)}))
        except Exception:
            pass


async def plugin_ws_handler(request: web.Request):
    # CSRF/rebinding guard (upstream): the plugin UI iframe reports Origin "null".
    blocked = _guard(request, allow_null_origin=True)
    if blocked is not None:
        print(f"[plugin] refused connection from {request.remote} "
              f"(origin={request.headers.get('Origin')!r})")
        return blocked

    ws = web.WebSocketResponse(heartbeat=20, max_msg_size=16 * 1024 * 1024)
    await ws.prepare(request)

    # Each plugin (one per open Figma file) gets its own registry slot. Identity
    # (fileKey / file name) arrives in the `hello` message a moment after connect.
    # Reconnects never lock out: a stale same-file connection is replaced at
    # `hello` time, so no incumbent check is needed in the multi-file model.
    conn_id = str(uuid.uuid4())
    PLUGINS[conn_id] = {"ws": ws, "fileKey": None, "name": None, "docSig": None}
    print(f"[plugin] connected {conn_id[:8]} from {request.remote}")

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
                file_key = m.get("fileKey")
                name = m.get("name")
                doc_sig = m.get("docSig")
                # Drop an older registration for the same file ONLY if its socket
                # is already dead (plugin re-Run without a clean close).
                #
                # Evicting a LIVE one is what the previous build did, and with the
                # same file open in two tabs/windows it looped forever: each side
                # kicked the other out, the loser reconnected 2s later and kicked
                # back. Two live views of one document are legitimate — keep both
                # and let resolve_target pick (they edit the same file either way).
                for cid, info in list(PLUGINS.items()):
                    if cid == conn_id:
                        continue
                    same = (file_key and info.get("fileKey") == file_key) or (
                        name and info.get("fileKey") is None and info.get("name") == name
                    )
                    if not same:
                        continue
                    # Two live views of one document (same file in two tabs or
                    # windows) — both stay; resolve_target routes by docSig.
                    if (doc_sig is not None
                            and info.get("docSig") == doc_sig
                            and not info["ws"].closed):
                        continue
                    if await _still_alive(info):
                        continue
                    print(f"[plugin] dropping dead connection for {name!r}")
                    try:
                        await info["ws"].close(code=1000, message=b"superseded")
                    except Exception:
                        pass
                    PLUGINS.pop(cid, None)
                PLUGINS[conn_id]["fileKey"] = file_key
                PLUGINS[conn_id]["name"] = name
                PLUGINS[conn_id]["docSig"] = doc_sig
                PLUGINS[conn_id]["hello"] = True
                PLUGINS[conn_id]["pluginVersion"] = m.get("plugin")
                print(f"[plugin] hello v{m.get('version', '?')} "
                      f"file={name!r} key={file_key} sig={doc_sig} "
                      f"plugin={m.get('plugin')}")
                if plugin_outdated(PLUGINS[conn_id]):
                    # The plugin bar turns purple and asks for a re-Run.
                    try:
                        await ws.send_str(json.dumps({
                            "type": "outdated",
                            "running": m.get("plugin"),
                            "expected": expected_plugin_version(),
                        }))
                    except Exception:
                        pass
                await _broadcast_peers()
                continue
            if mtype == "pong":
                # Answer to a liveness probe: this slot is defended, so a new
                # connection for the same file will not take it over.
                fut = PLUGINS.get(conn_id, {}).pop("pong", None)
                if fut is not None and not fut.done():
                    fut.set_result(True)
                continue

            rid = m.get("id")
            entry = PENDING.get(rid)
            if not entry:
                # Late reply for a request that already timed out. The client got a
                # 504 long ago, but the script kept running — record that it has
                # finally finished so the interlock on this file can lift.
                orphan = ABANDONED.pop(rid, None)
                if orphan is not None and mtype in ("result", "error"):
                    late = time.time() - orphan["t0"]
                    print(f"[orphan] {rid[:8]} finished after {late:.0f}s "
                          f"({mtype}) — client already received a 504")
                continue

            if mtype == "log":
                entry["logs"].append(m.get("text", ""))
            elif mtype in ("result", "error"):
                if not entry["future"].done():
                    entry["future"].set_result(m)
    finally:
        PLUGINS.pop(conn_id, None)
        LOCKS.pop(conn_id, None)
        print(f"[plugin] disconnected {conn_id[:8]}")
        # Fail in-flight requests routed to THIS connection so clients don't hang.
        for rid, entry in list(PENDING.items()):
            if entry.get("conn") == conn_id and not entry["future"].done():
                entry["future"].set_result({
                    "id": rid, "type": "error", "text": "plugin disconnected mid-request",
                })
        # The sandbox died with the connection, so nothing can still be mutating
        # through it — drop its interlock instead of wedging the next reconnect.
        for rid, orphan in list(ABANDONED.items()):
            if orphan.get("conn") == conn_id:
                ABANDONED.pop(rid, None)
        await _broadcast_peers()
    return ws


def _live_plugins():
    """Connected, non-closed plugin slots as a list of (conn_id, info)."""
    return [(cid, i) for cid, i in PLUGINS.items() if not i["ws"].closed]


def resolve_target(target):
    """Map a target string (file name, fileKey, or substring) to a connection.

    Returns (conn_id, info) on success, or (None, reason) where reason is a
    human-readable string explaining the miss for the error response.
    """
    live = _live_plugins()
    if not live:
        return None, "plugin not connected — open Figmosha Bridge in Figma"

    if target is None or target == "":
        if len(live) == 1:
            return live[0]
        names = ", ".join(repr(i.get("name") or f"({c[:8]})") for c, i in live)
        return None, (f"{len(live)} files connected — specify a target. "
                      f"Connected: {names}")

    t = target.lower()
    # 1. exact name (case-insensitive) — must be unique, or the caller cannot know
    #    which of two same-named windows it just wrote to.
    exact = [(cid, i) for cid, i in live if i.get("name") and i["name"].lower() == t]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        # Same name, same document signature = one file open in several tabs or
        # windows. Both views edit the same document, so routing to the newest
        # live one is safe; the caller cannot end up in "the other copy".
        sigs = {i.get("docSig") for _, i in exact}
        if len(sigs) == 1 and None not in sigs:
            return exact[-1]
        # Same name, different documents (or a plugin build too old to report a
        # signature) — genuinely ambiguous, and a write would land in a file the
        # caller did not choose.
        conns = ", ".join(c[:8] for c, _ in exact)
        return None, (f"{len(exact)} connected files are named {target!r} "
                      f"(conns: {conns}) — different documents sharing a name; "
                      f"close one, target cannot be resolved safely")
    # 2. exact fileKey
    for cid, i in live:
        if i.get("fileKey") == target:
            return cid, i
    # 3. unambiguous substring of the name
    subs = [(cid, i) for cid, i in live if i.get("name") and t in i["name"].lower()]
    if len(subs) == 1:
        return subs[0]

    names = ", ".join(repr(i.get("name") or f"({c[:8]})") for c, i in live)
    if len(subs) > 1:
        return None, f"target {target!r} matches multiple files: {names}"
    return None, f"no connected file matches {target!r}. Connected: {names}"


async def exec_handler(request: web.Request) -> web.Response:
    blocked = _guard(request)
    if blocked is not None:
        return blocked

    try:
        body = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"ok": False, "error": "invalid JSON body"}, status=400)

    code = body.get("code")
    if not isinstance(code, str) or not code.strip():
        return web.json_response({"ok": False, "error": "missing or empty 'code'"}, status=400)

    conn_id, info = resolve_target(body.get("target"))
    if conn_id is None:
        # info holds the human-readable reason. 503 if nothing connected at all.
        no_plugins = not _live_plugins()
        return web.json_response({"ok": False, "error": info},
                                 status=503 if no_plugins else 409)
    target_ws = info["ws"]

    timeout = float(body.get("timeout", 60))
    parallel = bool(body.get("parallel", False))
    force = bool(body.get("force", False))

    # Reads are safe to fan out; anything that mutates must take the file's lock.
    if parallel:
        return await _dispatch(target_ws, conn_id, code, timeout, force)
    async with _lock_for(conn_id):
        return await _dispatch(target_ws, conn_id, code, timeout, force)


async def _send_abort(target_ws, rid):
    try:
        await target_ws.send_str(json.dumps({"id": rid, "type": "abort"}))
    except Exception:
        pass


def _abandon(rid, conn_id, timeout, target_ws):
    """Mark a request as abandoned-but-possibly-still-running.

    Nothing can kill a script already executing in the sandbox, so the next caller
    on this file is blocked (409) rather than left racing an invisible writer.
    Cancellation is requested cooperatively; scripts opt in by calling h.ck().
    """
    entry = PENDING.get(rid, {})
    ABANDONED[rid] = {"conn": conn_id,
                      "t0": entry.get("t0", time.time()),
                      "timeout": timeout}
    asyncio.get_running_loop().create_task(_send_abort(target_ws, rid))


async def _dispatch(target_ws, conn_id, code, timeout, force) -> web.Response:
    """Send one exec to a plugin and await its reply. Caller holds the lock."""
    stale = [r for r, o in ABANDONED.items() if o.get("conn") == conn_id]
    if stale and not force:
        return web.json_response({
            "ok": False,
            "error": (f"a previous script on this file timed out and may still be running "
                      f"(rid {stale[0][:8]}) — its mutations were not rolled back. Wait for "
                      f"it to finish, POST /clear to drop the interlock, or resend with "
                      f"\"force\": true."),
            "abandoned": [r[:8] for r in stale],
        }, status=409)

    rid = str(uuid.uuid4())
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    PENDING[rid] = {"future": fut, "logs": [], "t0": time.time(), "conn": conn_id}

    try:
        try:
            await target_ws.send_str(json.dumps({"id": rid, "type": "exec", "code": code}))
        except Exception as e:
            return web.json_response(
                {"ok": False, "error": f"send to plugin failed: {e}"}, status=500)

        try:
            result = await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.CancelledError:
            # Client vanished mid-request (aiohttp cancels the handler). The script
            # is still running, so the file stays interlocked.
            _abandon(rid, conn_id, timeout, target_ws)
            raise
        except asyncio.TimeoutError:
            entry = PENDING.get(rid, {})
            _abandon(rid, conn_id, timeout, target_ws)
            return web.json_response({
                "ok": False,
                "error": f"timeout after {timeout:.0f}s",
                "warning": ("the script was NOT cancelled — it may still be running and "
                            "mutating the document, and anything it already applied is "
                            "not rolled back. Re-read the nodes before trusting them."),
                "rid": rid[:8],
                "logs": list(entry.get("logs", [])),
            }, status=504)

        return _reply(PENDING[rid], result)
    finally:
        # Always reap, including on client disconnect / task cancellation —
        # otherwise /status `pending` inflates permanently.
        PENDING.pop(rid, None)


def _reply(entry, result) -> web.Response:
    elapsed_ms = int((time.time() - entry["t0"]) * 1000)

    if result.get("type") == "error":
        error_text = result.get("text", "unknown error")
        body = {
            "ok": False,
            "error": error_text,
            "hint": find_hint(error_text),
            "stack": result.get("stack"),
            "logs": entry["logs"],
            "elapsed_ms": elapsed_ms,
        }
        status = 500
    else:
        body = {
            "ok": True,
            "result": result.get("text", ""),
            "value": result.get("value"),
            "logs": entry["logs"],
            "elapsed_ms": elapsed_ms,
        }
        status = 200

    # Agents read responses, not the plugin bar — so say it here too.
    notice = _version_notice(PLUGINS.get(entry.get("conn")) or {})
    if notice:
        body["notice"] = notice
    return web.json_response(body, status=status)


def _version_notice(info):
    """One line asking for a re-Run / restart when running code is stale."""
    parts = []
    if info and plugin_outdated(info):
        parts.append(f"the plugin in «{info.get('name') or '?'}» runs an older build "
                     f"({info.get('pluginVersion') or 'unversioned'}, current "
                     f"{expected_plugin_version()}) — re-run it in Figma: "
                     f"Plugins → Development → Figmosha Bridge")
    if bridge_outdated():
        parts.append("bridge.py changed since the bridge started — restart it "
                     "(bash start-bridge.sh, or .\\start-bridge.ps1 -Restart)")
    return "; ".join(parts) or None


def _files_payload():
    live = _live_plugins()
    seen = {}
    out = []
    for cid, i in live:
        key = (i.get("name"), i.get("docSig"))
        entry = {"name": i.get("name"), "fileKey": i.get("fileKey"), "conn": cid[:8],
                 "plugin": i.get("pluginVersion"), "outdated": plugin_outdated(i)}
        if i.get("docSig") is not None and key in seen:
            # Another tab/window on the same document. Harmless — flagged so the
            # count in `files` is not read as "two different files".
            entry["sameDocAs"] = seen[key]
        else:
            seen[key] = cid[:8]
        out.append(entry)
    return out


async def status_handler(request: web.Request) -> web.Response:
    blocked = _guard(request)
    if blocked is not None:
        return blocked

    files = _files_payload()
    return web.json_response({
        "plugin_connected": len(files) > 0,
        "plugin_version": expected_plugin_version(),
        "bridge_outdated": bridge_outdated(),
        "files": files,
        "pending": len(PENDING),
        "abandoned": [
            {"rid": r[:8], "conn": o["conn"][:8], "age_s": int(time.time() - o["t0"])}
            for r, o in ABANDONED.items()
        ],
    })


async def clear_handler(request: web.Request) -> web.Response:
    """Drop the abandoned-script interlock for one file, after a 504."""
    blocked = _guard(request)
    if blocked is not None:
        return blocked

    try:
        body = await request.json()
    except json.JSONDecodeError:
        body = {}

    conn_id, info = resolve_target(body.get("target"))
    if conn_id is None:
        no_plugins = not _live_plugins()
        return web.json_response({"ok": False, "error": info},
                                 status=503 if no_plugins else 409)

    dropped = [r for r, o in list(ABANDONED.items()) if o.get("conn") == conn_id]
    for r in dropped:
        ABANDONED.pop(r, None)
    return web.json_response({"ok": True, "cleared": [r[:8] for r in dropped],
                              "file": info.get("name")})


async def targets_handler(request: web.Request) -> web.Response:
    blocked = _guard(request)
    if blocked is not None:
        return blocked

    return web.json_response({"files": _files_payload()})


async def root_handler(request: web.Request) -> web.Response:
    blocked = _guard(request)
    if blocked is not None:
        return blocked

    return web.json_response({
        "service": "figmosha-bridge",
        "version": "2.0",
        "endpoints": {
            "POST /exec": ("{code, target?, timeout?, parallel?, force?} -> "
                           "{ok, result, value, logs, elapsed_ms}. Serialized per file "
                           "unless parallel:true (reads only)."),
            "GET /status": "{plugin_connected, files, pending, abandoned}",
            "GET /targets": "{files: [{name, fileKey, conn}]}",
            "POST /clear": "{target} -> drop a file's abandoned-script interlock after a 504",
            "WS /plugin": "Figma plugin connects here (one per open file)",
        },
    })


def build_app() -> web.Application:
    app = web.Application(client_max_size=16 * 1024 * 1024)
    app.router.add_get("/", root_handler)
    app.router.add_get("/status", status_handler)
    app.router.add_get("/targets", targets_handler)
    app.router.add_post("/exec", exec_handler)
    app.router.add_post("/clear", clear_handler)
    app.router.add_get("/plugin", plugin_ws_handler)
    return app


def main():
    global ALLOWED_HOSTS

    # File names are routinely Cyrillic, CJK or emoji. With stdout redirected to
    # a log on Windows, Python falls back to the ANSI code page and a `hello`
    # print would raise UnicodeEncodeError inside the plugin handler.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

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

    loop = asyncio.new_event_loop()
    loop.set_exception_handler(_quiet_connection_resets)
    web.run_app(build_app(), host=args.host, port=args.port, print=None, loop=loop)


def _quiet_connection_resets(loop, context):
    """Drop the Proactor traceback Windows logs when a Figma tab closes its socket.

    asyncio's Proactor loop reports WinError 10054 from inside its own transport
    callback, after aiohttp has already handled the disconnect — so it is pure
    noise, and it filled bridge.err.log with tracebacks on every closed tab.
    """
    if isinstance(context.get("exception"), ConnectionResetError):
        return
    loop.default_exception_handler(context)


if __name__ == "__main__":
    main()
