"""Bridge tests driven by a fake plugin over the real WebSocket.

Everything here runs without Figma: a small asyncio client plays the plugin's
part, which is enough to exercise the parts most likely to regress — the shared
PENDING / PLUGIN_WS state, the slot handover, and the origin/host guard.

    pip install -r requirements-dev.txt
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
    def reset():
        for registry in (bridge.PENDING, bridge.PLUGINS, bridge.LOCKS, bridge.ABANDONED,
                         bridge.QUEUE):
            registry.clear()
        bridge.ALLOWED_HOSTS = set()
        bridge.UPDATE = None
    reset()
    yield
    reset()


class FakePlugin:
    """Stands in for plugin/ui.html: echoes exec requests, answers pings."""

    def __init__(self, client, *, answer_ping=True, reply=None):
        self.client = client
        self.answer_ping = answer_ping
        self.reply = reply or (lambda code: {"text": "ok", "value": 42})
        self.ws = None
        self._task = None
        self.seen_codes = []
        self.exec_ids = []
        self.received = []   # every message the bridge sent, in order

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
            self.received.append(m)
            if m.get("type") == "ping":
                if self.answer_ping:
                    await self.ws.send_str(json.dumps({"type": "pong"}))
            elif m.get("type") == "exec":
                self.seen_codes.append(m["code"])
                self.exec_ids.append(m["id"])
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


def test_timed_out_script_interlocks_the_file_until_it_finishes():
    """A 504 does not stop the script, so the next exec on that file must get a
    409 instead of racing it; the plugin is told to abort (h.ck), and the
    interlock lifts by itself once the orphan finally replies."""
    async def go():
        c = await make_client()
        async with FakePlugin(c, reply=lambda code: {"type": "__drop__"}) as plugin:
            r = await c.post("/exec", json={"code": "slow", "timeout": 0.3})
            assert r.status == 504
            orphan = plugin.exec_ids[0]

            r = await c.post("/exec", json={"code": "next", "timeout": 0.3})
            body = await r.json()
            assert r.status == 409 and body["abandoned"] == [orphan[:8]]
            assert {"type": "abort", "id": orphan} in plugin.received

            await plugin.ws.send_str(json.dumps(
                {"type": "result", "id": orphan, "text": "late"}))
            await asyncio.sleep(0.1)
            assert bridge.ABANDONED == {}
            plugin.reply = lambda code: {"text": "ok", "value": 1}
            r = await c.post("/exec", json={"code": "after", "timeout": 2})
            assert r.status == 200
        await c.close()
    run(go())


def test_outdated_plugin_is_told_to_rerun():
    """A plugin running an older build than plugin/code.js gets an `outdated`
    message (the bar asks for a re-Run), /status flags it, and every exec reply
    carries a notice so an agent sees it too. A current build gets none."""
    async def go():
        c = await make_client()
        current = bridge.expected_plugin_version()
        assert current, "plugin/code.js should declare PLUGIN_VERSION"
        async with FakePlugin(c) as old, FakePlugin(c) as new:
            # FakePlugin's own hello carries no build id, which is outdated too.
            old.received.clear()
            new.received.clear()
            await old.ws.send_str(json.dumps(
                {"type": "hello", "name": "Old", "plugin": "2000-01-01.1"}))
            await new.ws.send_str(json.dumps(
                {"type": "hello", "name": "New", "plugin": current}))
            await asyncio.sleep(0.1)

            assert any(m["type"] == "outdated" and m["expected"] == current
                       for m in old.received)
            assert not any(m["type"] == "outdated" for m in new.received)

            files = {f["name"]: f for f in (await (await c.get("/status")).json())["files"]}
            assert files["Old"]["outdated"] is True
            assert files["New"]["outdated"] is False

            r = await c.post("/exec", json={"code": "return 1", "target": "Old"})
            assert "re-run" in (await r.json())["notice"]
            r = await c.post("/exec", json={"code": "return 1", "target": "New"})
            assert "notice" not in await r.json()
        await c.close()
    run(go())


def test_update_on_github_is_offered_to_plugins():
    """When GitHub has newer commits, connected plugins get an `update` message
    (the bar shows "New version" + Update), a plugin connecting later gets it at
    hello, /status reports it and exec replies carry a notice."""
    async def go():
        c = await make_client()
        async with FakePlugin(c) as early:
            await bridge.set_update(4)
            await asyncio.sleep(0.05)
            msg = [m for m in early.received if m["type"] == "update"][-1]
            assert msg["behind"] == 4 and msg["url"].startswith("https://github.com/")

            async with FakePlugin(c) as late:
                assert any(m["type"] == "update" for m in late.received)

            assert (await (await c.get("/status")).json())["update"]["behind"] == 4
            r = await c.post("/exec", json={"code": "return 1"})
            assert "git pull" in (await r.json())["notice"]

            await bridge.set_update(0)
            assert (await (await c.get("/status")).json())["update"] is None
        await c.close()
    run(go())


@pytest.mark.parametrize("compare, behind", [
    ({"status": "identical", "ahead_by": 0}, 0),
    ({"status": "ahead", "ahead_by": 3}, 3),        # GitHub has 3 we lack
    ({"status": "behind", "behind_by": 2}, 0),      # we are ahead: nothing to pull
    ({"status": "diverged", "ahead_by": 1, "behind_by": 5}, 1),
])
def test_commits_behind(compare, behind):
    assert bridge.commits_behind(compare) == behind


def test_peers_counts_only_files_that_said_hello():
    """The plugin bar's "1/2" pill: each identified connection learns its index
    and the total; a socket that has not sent `hello` yet is not a file."""
    async def go():
        c = await make_client()
        async with FakePlugin(c) as a, FakePlugin(c) as b:
            await asyncio.sleep(0.1)
            silent = await c.ws_connect("/plugin", headers={"Origin": "null"})
            await a.ws.send_str(json.dumps({"type": "hello", "name": "FileA"}))
            await asyncio.sleep(0.1)
            last = lambda p: [m for m in p.received if m["type"] == "peers"][-1]
            assert last(a) == {"type": "peers", "index": 1, "total": 2}
            assert last(b) == {"type": "peers", "index": 2, "total": 2}
            await silent.close()
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


def test_same_file_reconnect_replaces_unresponsive():
    """A plugin re-Run in the same file takes over at `hello` time — but only
    after the incumbent fails a liveness probe, so a socket the OS has not torn
    down yet (a laptop that slept) cannot keep the name to itself."""
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
            # The incumbent is probed before it is dropped; its socket is still
            # open, so the handover costs one probe timeout.
            await asyncio.sleep(bridge.SLOT_PROBE_TIMEOUT + 0.4)

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


def test_same_document_in_two_windows_coexists():
    """One file open in two tabs/windows reports the same name AND the same
    docSig. Both connections stay — evicting a live one made the two sides kick
    each other out in a loop — and a target resolves to the newest live view,
    which is safe because both edit the same document."""
    async def go():
        c = await make_client()
        async with FakePlugin(c) as first, FakePlugin(c) as second:
            for p in (first, second):
                await p.ws.send_str(json.dumps(
                    {"type": "hello", "version": "test",
                     "name": "FileA", "docSig": "abc123"}))
            await asyncio.sleep(bridge.SLOT_PROBE_TIMEOUT + 0.4)

            files = (await (await c.get("/targets")).json())["files"]
            assert [f["name"] for f in files] == ["FileA", "FileA"], files

            r = await c.post("/exec", json={"code": "return 1", "timeout": 5,
                                            "target": "FileA"})
            assert r.status == 200, await r.text()
            assert second.seen_codes, "target did not reach the newest live view"
            assert not first.seen_codes
        await c.close()
    run(go())


def test_two_documents_sharing_a_name_stay_ambiguous():
    """Same name, different docSig = two genuinely different files. Neither may
    be picked for the caller — a write would land in a file they did not choose."""
    async def go():
        c = await make_client()
        async with FakePlugin(c) as first, FakePlugin(c) as second:
            await first.ws.send_str(json.dumps(
                {"type": "hello", "version": "test",
                 "name": "Untitled", "docSig": "aaa"}))
            await second.ws.send_str(json.dumps(
                {"type": "hello", "version": "test",
                 "name": "Untitled", "docSig": "bbb"}))
            await asyncio.sleep(bridge.SLOT_PROBE_TIMEOUT + 0.4)

            r = await c.post("/exec", json={"code": "return 1", "timeout": 5,
                                            "target": "Untitled"})
            assert r.status == 409
            assert not first.seen_codes and not second.seen_codes
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


# ─── result shape (the plugin ships each value once) ──────────────────────

@pytest.mark.parametrize("msg, text", [
    ({"value": "plain"}, "plain"),
    ({"value": 42}, "42"),
    ({"value": True}, "true"),
    ({"value": None}, "null"),
    ({"value": {"a": [1, "Привіт"]}}, '{\n  "a": [\n    1,\n    "Привіт"\n  ]\n}'),
    ({"value": None, "text": "Done"}, "Done"),          # no return value
    ({"value": None, "text": "NaN"}, "NaN"),            # value can't say it
    ({"value": 1, "text": "from an older plugin"}, "from an older plugin"),
])
def test_result_text_is_derived_from_the_value(msg, text):
    assert bridge._result_text(msg) == text


def test_huge_results_skip_the_slow_pretty_printer():
    big = [{"id": i} for i in range(200_000)]
    text = bridge._result_text({"value": big})
    assert "\n" not in text and json.loads(text) == big


def test_batched_log_lines_arrive_in_order():
    async def go():
        c = await make_client()
        async with FakePlugin(c, reply=lambda code: {"type": "__drop__"}) as p:
            task = asyncio.create_task(c.post("/exec", json={"code": "x", "timeout": 5}))
            await asyncio.sleep(0.1)
            rid = p.exec_ids[0]
            await p.ws.send_str(json.dumps({"type": "log", "id": rid, "lines": ["a", "b"]}))
            await p.ws.send_str(json.dumps({"type": "log", "id": rid, "text": "c"}))  # old plugin
            await p.ws.send_str(json.dumps({"type": "result", "id": rid, "value": 1}))
            body = await (await task).json()
            assert body["logs"] == ["a", "b", "c"] and body["result"] == "1"
        await c.close()
    run(go())
