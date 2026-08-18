"""Bridge tests driven by a fake plugin over the real WebSocket.

Everything here runs without Figma: a small asyncio client plays the plugin's
part, which is enough to exercise the parts most likely to regress — the shared
PENDING / PLUGIN_WS state, the slot handover, and the origin/host guard.

    pip install pytest aiohttp
    pytest -q
"""

import asyncio
import json
import sys
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import bridge  # noqa: E402


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def clean_state():
    """The module keeps its state in globals; give every test a fresh one."""
    bridge.PENDING.clear()
    bridge.PLUGIN_WS = None
    bridge.PLUGIN_LAST_SEEN = 0.0
    bridge.ALLOWED_HOSTS = set()
    yield
    bridge.PENDING.clear()
    bridge.PLUGIN_WS = None
    bridge.ALLOWED_HOSTS = set()


class FakePlugin:
    """Stands in for plugin/ui.html: echoes exec requests, answers pings."""

    def __init__(self, client, *, answer_ping=True, reply=None):
        self.client = client
        self.answer_ping = answer_ping
        self.reply = reply or (lambda code: {"text": "ok", "value": 42})
        self.ws = None
        self._task = None
        self.seen_codes = []

    async def __aenter__(self):
        self.ws = await self.client.ws_connect("/plugin", headers={"Origin": "null"})
        await self.ws.send_str(json.dumps({"type": "hello", "version": "test"}))
        self._task = asyncio.create_task(self._pump())
        await asyncio.sleep(0.1)
        return self

    async def __aexit__(self, *exc):
        if self._task:
            self._task.cancel()
        if self.ws and not self.ws.closed:
            await self.ws.close()

    def go_silent(self):
        """Stop answering without closing — a laptop that went to sleep."""
        if self._task:
            self._task.cancel()

    async def _pump(self):
        async for msg in self.ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                continue
            m = json.loads(msg.data)
            if m.get("type") == "ping":
                if self.answer_ping:
                    await self.ws.send_str(json.dumps({"type": "pong"}))
            elif m.get("type") == "exec":
                self.seen_codes.append(m["code"])
                out = self.reply(m["code"])
                await self.ws.send_str(json.dumps(
                    {"type": out.pop("type", "result"), "id": m["id"], **out}))


async def make_client():
    client = TestClient(TestServer(bridge.build_app()))
    await client.start_server()
    return client


# ─── guard ────────────────────────────────────────────────────────────────

def test_local_request_allowed():
    async def go():
        c = await make_client()
        r = await c.get("/status")
        assert r.status == 200
        assert (await r.json())["plugin_connected"] is False
        await c.close()
    run(go())


def test_request_with_origin_is_refused():
    async def go():
        c = await make_client()
        for path in ("/", "/status"):
            r = await c.get(path, headers={"Origin": "https://evil.example"})
            assert r.status == 403, path
        r = await c.post("/exec", data='{"code":"return 1"}',
                         headers={"Content-Type": "text/plain",
                                  "Origin": "https://evil.example"})
        assert r.status == 403
        await c.close()
    run(go())


def test_unexpected_host_is_refused():
    async def go():
        c = await make_client()
        bridge.ALLOWED_HOSTS = {"localhost:8787"}
        r = await c.get("/status", headers={"Host": "evil.example"})
        assert r.status == 403
        assert "Host" in (await r.json())["error"]
        await c.close()
    run(go())


def test_cross_origin_websocket_is_refused():
    async def go():
        c = await make_client()
        with pytest.raises(aiohttp.WSServerHandshakeError) as e:
            await c.ws_connect("/plugin", headers={"Origin": "https://evil.example"})
        assert e.value.status == 403
        await c.close()
    run(go())


# ─── exec round trip ──────────────────────────────────────────────────────

def test_exec_without_plugin_is_503():
    async def go():
        c = await make_client()
        r = await c.post("/exec", json={"code": "return 1"})
        assert r.status == 503
        await c.close()
    run(go())


def test_exec_round_trip():
    async def go():
        c = await make_client()
        async with FakePlugin(c) as plugin:
            r = await c.post("/exec", json={"code": "return 40 + 2"})
            body = await r.json()
            assert r.status == 200 and body["ok"] is True
            assert body["value"] == 42
            assert plugin.seen_codes == ["return 40 + 2"]
            assert bridge.PENDING == {}, "request left behind in PENDING"
        await c.close()
    run(go())


def test_error_response_carries_a_hint():
    async def go():
        c = await make_client()
        reply = lambda code: {"type": "error", "text": "in an unloaded font"}
        async with FakePlugin(c, reply=reply):
            r = await c.post("/exec", json={"code": "n.characters = 'x'"})
            body = await r.json()
            assert r.status == 500 and body["ok"] is False
            assert "h.setText" in body["hint"]
            assert bridge.PENDING == {}
        await c.close()
    run(go())


def test_timeout_returns_504_and_clears_pending():
    async def go():
        c = await make_client()
        async with FakePlugin(c, reply=lambda code: {"type": "__drop__"}):
            # plugin never answers; the bridge must give up on its own
            r = await c.post("/exec", json={"code": "sleep", "timeout": 0.3})
            assert r.status == 504
            assert bridge.PENDING == {}
        await c.close()
    run(go())


def test_disconnect_fails_inflight_requests():
    async def go():
        c = await make_client()
        plugin = FakePlugin(c, reply=lambda code: {"type": "__drop__"})
        await plugin.__aenter__()
        task = asyncio.create_task(
            c.post("/exec", json={"code": "slow", "timeout": 10}))
        await asyncio.sleep(0.2)
        await plugin.__aexit__()
        r = await asyncio.wait_for(task, timeout=5)
        body = await r.json()
        assert r.status == 500
        assert "disconnected" in body["error"]
        await c.close()
    run(go())


# ─── the single plugin slot ───────────────────────────────────────────────

def test_two_files_coexist_and_need_a_target():
    """Multi-file contract: one plugin per open file, all coexist; an exec with
    two files connected must name its target (409 otherwise)."""
    async def go():
        c = await make_client()
        async with FakePlugin(c) as a, FakePlugin(c) as b:
            await a.ws.send_str(json.dumps(
                {"type": "hello", "version": "test", "name": "FileA"}))
            await b.ws.send_str(json.dumps(
                {"type": "hello", "version": "test", "name": "FileB"}))
            await asyncio.sleep(0.1)

            r = await c.get("/targets")
            files = (await r.json())["files"]
            assert sorted(f["name"] for f in files) == ["FileA", "FileB"]

            r = await c.post("/exec", json={"code": "return 1", "timeout": 2})
            assert r.status == 409  # ambiguous — must name a target

            r = await c.post("/exec", json={"code": "return 1", "timeout": 5,
                                            "target": "FileA"})
            assert r.status == 200
            assert a.seen_codes and not b.seen_codes
        await c.close()
    run(go())


def test_same_file_reconnect_replaces_stale():
    """A plugin re-Run in the same file takes over at `hello` time — the stale
    connection is closed and the new one serves; no reconnect lockout."""
    async def go():
        c = await make_client()
        first = FakePlugin(c)
        await first.__aenter__()
        await first.ws.send_str(json.dumps(
            {"type": "hello", "version": "test", "name": "FileA"}))
        await asyncio.sleep(0.1)
        first.go_silent()

        async with FakePlugin(c) as second:
            await second.ws.send_str(json.dumps(
                {"type": "hello", "version": "test", "name": "FileA"}))
            await asyncio.sleep(0.2)

            r = await c.get("/targets")
            files = (await r.json())["files"]
            assert [f["name"] for f in files] == ["FileA"], files

            r = await c.post("/exec", json={"code": "return 1", "timeout": 5,
                                            "target": "FileA"})
            assert r.status == 200
            assert second.seen_codes, "exec never reached the new plugin"
            assert first.seen_codes == [], "stale socket was still being used"
        await first.__aexit__()
        await c.close()
    run(go())


# ─── hints ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("error_text,expected", [
    ("Cannot assign to read only property 'fills'", "h.bF()"),
    ("in an unloaded font Inter Bold", "h.setText"),
    ("teamlibrary permission not specified in manifest", "manifest.json"),
    ("Unable to find a variant matching", "h.variantsOf"),
    ("something entirely unrecognised", None),
])
def test_find_hint(error_text, expected):
    hint = bridge.find_hint(error_text)
    if expected is None:
        assert hint is None
    else:
        assert expected in hint
