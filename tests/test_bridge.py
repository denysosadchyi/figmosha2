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
import chat  # noqa: E402


def run(coro):
    return asyncio.run(coro)


# Every test starts from a clean bridge: see clean_state in conftest.py.


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


@pytest.mark.parametrize("origin", ["https://evil.example", "null", "http://localhost:8787"])
def test_request_with_origin_is_refused(origin):
    """Any Origin on HTTP is a browser page. "null" is for the plugin WS only."""
    async def go():
        c = await make_client()
        async with FakePlugin(c) as p:
            for path in ("/", "/status"):
                r = await c.get(path, headers={"Origin": origin})
                assert r.status == 403, path
            for path in ("/exec", "/clear"):
                r = await c.post(path, data='{"code":"return 1"}',
                                 headers={"Content-Type": "text/plain", "Origin": origin})
                assert r.status == 403, path
            assert p.seen_codes == [], "a refused request still ran a script"
        await c.close()
    run(go())


@pytest.mark.parametrize("host", ["evil.example", "localhost.evil.example:8787",
                                  "127.0.0.1.nip.io:8787", "localhost:9999"])
def test_unexpected_host_is_refused(host):
    """DNS-rebinding style Hosts are refused once main() pins the port."""
    async def go():
        c = await make_client()
        bridge.ALLOWED_HOSTS = {"localhost:8787", "127.0.0.1:8787", "[::1]:8787"}
        r = await c.get("/status", headers={"Host": host})
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


def test_new_release_is_offered_to_plugins():
    """When a newer vX.Y.Z is released, connected plugins get an `update`
    message (the bar shows "New version X.Y.Z" + Update to the release page),
    a plugin connecting later gets it at hello, /status reports it and exec
    replies carry a notice. The same or an older release offers nothing."""
    async def go():
        c = await make_client()
        major, minor, patch = bridge.parse_version(bridge.VERSION)
        newer = f"{major}.{minor + 1}.0"
        async with FakePlugin(c) as early:
            await bridge.set_update(newer)
            await asyncio.sleep(0.05)
            msg = [m for m in early.received if m["type"] == "update"][-1]
            assert msg["latest"] == newer and msg["current"] == bridge.VERSION
            assert msg["url"].endswith(f"/releases/tag/v{newer}")

            async with FakePlugin(c) as late:
                assert any(m["type"] == "update" for m in late.received)

            status = await (await c.get("/status")).json()
            assert status["update"]["latest"] == newer and status["version"] == bridge.VERSION
            r = await c.post("/exec", json={"code": "return 1"})
            assert "git pull" in (await r.json())["notice"]

            for not_newer in (bridge.VERSION, "0.9.0"):
                await bridge.set_update(not_newer)
                assert (await (await c.get("/status")).json())["update"] is None
        await c.close()
    run(go())


@pytest.mark.parametrize("tags, newest", [
    (["v2.1.0", "v2.2.0", "v2.3.0"], "2.3.0"),
    (["v2.10.0", "v2.9.9"], "2.10.0"),             # numeric, not alphabetical
    (["v3.0.0-rc1", "v2.4.0", "nightly"], "2.4.0"),  # pre-releases and junk ignored
    ([], None),
])
def test_newest_release_from_tags(tags, newest):
    assert bridge.newest_release(tags) == newest


def test_version_is_valid_and_matches_the_changelog():
    """VERSION must be x.y.z and have its own CHANGELOG section — the release
    checklist in AGENTS.md: bump both together."""
    import re
    from pathlib import Path
    assert bridge.parse_version(bridge.VERSION), bridge.VERSION
    changelog = (Path(bridge.__file__).parent / "CHANGELOG.md").read_text(encoding="utf-8")
    section = re.search(rf"^## \[{re.escape(bridge.VERSION)}\]", changelog, re.M)
    assert section, f"CHANGELOG.md has no '## [{bridge.VERSION}]' section"


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


def test_loopback_listens_on_ipv4_and_ipv6():
    """`localhost` is ::1 first on Windows and macOS; without ::1 every client
    saying `localhost` stalled ~2 s per request on Windows."""
    import socket
    hosts = bridge._bind_hosts("127.0.0.1", 0)
    assert hosts[0] == "127.0.0.1"
    if socket.has_ipv6:
        assert "::1" in hosts or len(hosts) == 1  # IPv6 may be disabled on a box
    assert bridge._bind_hosts("0.0.0.0", 0) == ["0.0.0.0"]


# ─── update flow: the whole path, not only set_update ─────────────────────

@pytest.mark.parametrize("latest, offered", [
    ("9.0.0", True),
    ("v9.0.0", True),          # a tag name with its v
    (None, False),             # the check could not tell
    ("2.3.0-rc1", False),      # pre-release
    ("junk", False),
])
def test_set_update_compares_versions(latest, offered):
    run(bridge.set_update(latest))
    assert (bridge.UPDATE is not None) is offered


def test_numeric_compare_not_text():
    """2.10.0 is newer than 2.9.0, though "2.10.0" < "2.9.0" as text."""
    saved = bridge.VERSION
    try:
        bridge.VERSION = "2.9.0"
        run(bridge.set_update("2.10.0"))
        assert bridge.UPDATE and bridge.UPDATE["latest"] == "2.10.0"
    finally:
        bridge.VERSION = saved


class _FakeGitHub:
    """aiohttp.ClientSession stand-in: answers or fails like GitHub might."""

    def __init__(self, status=200, body=None, error=None):
        self.status, self.body, self.error = status, body, error

    def __call__(self, *a, **kw):
        return self

    async def __aenter__(self):
        if self.error:
            raise self.error
        return self

    async def __aexit__(self, *exc):
        return False

    def get(self, *a, **kw):
        return self

    async def json(self):
        if isinstance(self.body, Exception):
            raise self.body
        return self.body


@pytest.mark.parametrize("fake", [
    _FakeGitHub(error=aiohttp.ClientConnectionError("offline")),
    _FakeGitHub(error=asyncio.TimeoutError()),
    _FakeGitHub(status=403, body={"message": "rate limit"}),
    _FakeGitHub(status=200, body=ValueError("not json")),
    _FakeGitHub(status=200, body={"message": "not a list"}),
])
def test_update_check_failure_is_silent(monkeypatch, fake):
    """Offline, rate limited, bad JSON: the check returns None, never raises."""
    monkeypatch.setattr(bridge.aiohttp, "ClientSession", fake)
    assert run(bridge.check_for_update()) is None


def test_update_check_reads_release_tags(monkeypatch):
    monkeypatch.setattr(bridge.aiohttp, "ClientSession", _FakeGitHub(
        body=[{"name": "v2.3.0"}, {"name": "v2.10.1"}, {"name": "nightly"}]))
    assert run(bridge.check_for_update()) == "2.10.1"


def test_update_loop_keeps_offer_when_a_check_fails(monkeypatch):
    """A failed check (None) does not clear an offer the last check made."""
    answers = iter(["9.0.0", None])

    async def fake_check():
        return next(answers)

    async def go():
        monkeypatch.setattr(bridge, "check_for_update", fake_check)
        monkeypatch.setattr(bridge, "UPDATE_EVERY", 0.01)
        monkeypatch.setattr(bridge, "UPDATE_FIRST_DELAY", 0)
        task = asyncio.create_task(bridge._update_loop(None))
        await asyncio.sleep(0.1)
        task.cancel()
    run(go())
    assert bridge.UPDATE and bridge.UPDATE["latest"] == "9.0.0"


def test_no_update_check_env_turns_it_off(monkeypatch):
    app = web.Application()
    monkeypatch.setenv("FIGMOSHA_NO_UPDATE_CHECK", "1")
    bridge.install_update_checks(app)
    assert bridge._start_update_checks not in app.on_startup
    app = web.Application()
    monkeypatch.delenv("FIGMOSHA_NO_UPDATE_CHECK")
    bridge.install_update_checks(app)
    assert bridge._start_update_checks in app.on_startup
    assert bridge._stop_update_checks in app.on_cleanup


def test_fake_latest_env_for_live_tests(monkeypatch):
    """FIGMOSHA_FAKE_LATEST=9.9.9 offers that version without asking GitHub."""
    async def no_network():
        raise AssertionError("GitHub must not be asked")

    async def go():
        monkeypatch.setenv("FIGMOSHA_FAKE_LATEST", "9.9.9")
        monkeypatch.setattr(bridge, "check_for_update", no_network)
        monkeypatch.setattr(bridge, "UPDATE_FIRST_DELAY", 0)
        task = asyncio.create_task(bridge._update_loop(None))
        await asyncio.sleep(0.05)
        task.cancel()
    run(go())
    assert bridge.UPDATE["latest"] == "9.9.9"


def test_withdrawn_release_clears_the_offer_in_plugins():
    """If the newest release goes away, plugins get update with latest None,
    so the bar and the chat card stop offering it."""
    async def go():
        c = await make_client()
        async with FakePlugin(c) as p:
            await bridge.set_update("9.0.0")
            await bridge.set_update(bridge.VERSION)
            await asyncio.sleep(0.05)
            ups = [m for m in p.received if m["type"] == "update"]
            assert ups[-1]["latest"] is None
            # a plugin that connects now gets nothing at hello
            async with FakePlugin(c) as late:
                assert not any(m["type"] == "update" for m in late.received)
        await c.close()
    run(go())


# ═══ edge branches: validation, /clear, routing, odd messages, chat socket, main() ═══

async def hello(plugin, name, sig=None, file_key=None):
    msg = {"type": "hello", "name": name}
    if sig is not None:
        msg["docSig"] = sig
    if file_key is not None:
        msg["fileKey"] = file_key
    await plugin.ws.send_str(json.dumps(msg))
    await asyncio.sleep(0.05)


class StubWS:
    """Enough of a WebSocketResponse for the unit-level helpers."""

    def __init__(self, closed=False, fail=False):
        self.closed = closed
        self.fail = fail
        self.sent = []

    async def send_str(self, s):
        if self.fail:
            raise ConnectionResetError("gone")
        self.sent.append(json.loads(s))

    async def close(self, **kw):
        self.closed = True


# ─── /exec body validation (before any plugin is consulted) ───────────────

@pytest.mark.parametrize("raw, word", [
    ("not json", "invalid JSON"),
    ("[1, 2]", "JSON object"),
    ('"just a string"', "JSON object"),
    ("{}", "missing or empty 'code'"),
    ('{"code": "   "}', "missing or empty 'code'"),
    ('{"code": 42}', "missing or empty 'code'"),
    ('{"code": "x", "target": 7}', "'target' must be a string"),
    ('{"code": "x", "timeout": "5"}', "'timeout' must be a number"),
    ('{"code": "x", "timeout": true}', "'timeout' must be a number"),
    ('{"code": "x", "timeout": 0}', "between 0 and 3600"),
    ('{"code": "x", "timeout": -1}', "between 0 and 3600"),
    ('{"code": "x", "timeout": 3601}', "between 0 and 3600"),
    ('{"code": "x", "queue_timeout": "soon"}', "'queue_timeout' must be a number"),
    ('{"code": "x", "queue_timeout": 99999}', "between 0 and 3600"),
])
def test_malformed_exec_body_is_a_400_that_says_why(raw, word):
    async def go():
        c = await make_client()
        async with FakePlugin(c) as p:
            r = await c.post("/exec", data=raw, headers={"Content-Type": "application/json"})
            body = await r.json()
            assert r.status == 400 and body["ok"] is False
            assert word in body["error"], body
            assert p.seen_codes == [], "a rejected body still reached the plugin"
        await c.close()
    run(go())


def test_seconds_accepts_ints_and_floats_up_to_the_limit():
    assert bridge._seconds(1, "t") == 1.0
    assert bridge._seconds(0.5, "t") == 0.5
    assert bridge._seconds(bridge.MAX_SECONDS, "t") == float(bridge.MAX_SECONDS)
    with pytest.raises(ValueError):
        bridge._seconds(None, "t")


def test_agent_name_is_trimmed_and_capped_in_the_queue():
    async def go():
        c = await make_client()
        async with FakePlugin(c, reply=lambda code: {"type": "__drop__"}) as p:
            await hello(p, "A", "docA")
            task = asyncio.create_task(c.post("/exec", json={
                "code": "x", "timeout": 5, "agent": "  " + "a" * 60 + "  "}))
            await asyncio.sleep(0.15)
            files = (await (await c.get("/status")).json())["files"]
            assert files[0]["queue"]["running"]["agent"] == "a" * 40
            await p.ws.send_str(json.dumps({"type": "result", "id": p.exec_ids[0], "value": 1}))
            assert (await task).status == 200
        await c.close()
    run(go())


# ─── parallel / force ─────────────────────────────────────────────────────

def test_parallel_exec_skips_the_file_lock():
    """parallel:true is for reads: it runs while a writer holds the file."""
    async def go():
        c = await make_client()
        replies = {"slow": {"type": "__drop__"}}
        async with FakePlugin(c, reply=lambda code: dict(replies.get(code, {"value": code}))) as p:
            await hello(p, "A", "docA")
            writer = asyncio.create_task(c.post("/exec", json={"code": "slow", "timeout": 5}))
            await asyncio.sleep(0.15)
            assert bridge.LOCKS["doc:docA"].locked()
            r = await c.post("/exec", json={"code": "read", "parallel": True, "timeout": 2})
            body = await r.json()
            assert r.status == 200 and body["value"] == "read"
            assert "queued_ms" not in body, "parallel exec went through the queue"
            # a non-parallel caller does wait and gives up
            r = await c.post("/exec", json={"code": "w2", "timeout": 2, "queue_timeout": 0.2})
            assert r.status == 503
            await p.ws.send_str(json.dumps({"type": "result", "id": p.exec_ids[0], "value": 1}))
            assert (await writer).status == 200
        await c.close()
    run(go())


def test_parallel_must_be_exactly_true():
    async def go():
        c = await make_client()
        async with FakePlugin(c) as p:
            await hello(p, "A", "docA")
            r = await c.post("/exec", json={"code": "x", "parallel": "yes", "timeout": 2})
            assert "queued_ms" in await r.json(), "a truthy string counted as parallel"
        await c.close()
    run(go())


def test_force_runs_past_an_abandoned_script():
    async def go():
        c = await make_client()
        async with FakePlugin(c, reply=lambda code: {"type": "__drop__"}) as p:
            await hello(p, "A", "docA")
            assert (await c.post("/exec", json={"code": "slow", "timeout": 0.2})).status == 504
            p.reply = lambda code: {"value": 7}
            assert (await c.post("/exec", json={"code": "x", "timeout": 2})).status == 409
            r = await c.post("/exec", json={"code": "x", "timeout": 2, "force": True})
            assert r.status == 200 and (await r.json())["value"] == 7
            assert len(bridge.ABANDONED) == 1, "force must not drop the interlock"
        await c.close()
    run(go())


# ─── POST /clear ──────────────────────────────────────────────────────────

def test_clear_without_plugin_is_503():
    async def go():
        c = await make_client()
        r = await c.post("/clear", json={})
        assert r.status == 503 and (await r.json())["ok"] is False
        await c.close()
    run(go())


def test_clear_rejects_a_non_string_target():
    async def go():
        c = await make_client()
        async with FakePlugin(c):
            r = await c.post("/clear", json={"target": 5})
            assert r.status == 400 and "'target' must be a string" in (await r.json())["error"]
        await c.close()
    run(go())


def test_clear_drops_the_interlock_and_the_file_runs_again():
    async def go():
        c = await make_client()
        async with FakePlugin(c, reply=lambda code: {"type": "__drop__"}) as p:
            await hello(p, "Main", "docM")
            assert (await c.post("/exec", json={"code": "slow", "timeout": 0.2})).status == 504
            orphan = p.exec_ids[0]
            status = await (await c.get("/status")).json()
            assert [a["rid"] for a in status["abandoned"]] == [orphan[:8]]
            assert status["abandoned"][0]["age_s"] >= 0

            # a body that is not JSON counts as no target
            r = await c.post("/clear", data="garbage")
            body = await r.json()
            assert r.status == 200 and body == {"ok": True, "cleared": [orphan[:8]], "file": "Main"}
            assert bridge.ABANDONED == {}

            p.reply = lambda code: {"value": 1}
            assert (await c.post("/exec", json={"code": "x", "timeout": 2})).status == 200
            r = await c.post("/clear", json={})
            assert (await r.json())["cleared"] == []
        await c.close()
    run(go())


def test_clear_touches_only_the_named_file():
    async def go():
        c = await make_client()
        drop = lambda code: {"type": "__drop__"}
        async with FakePlugin(c, reply=drop) as a, FakePlugin(c, reply=drop) as b:
            await hello(a, "Alpha", "docAlpha")
            await hello(b, "Beta", "docBeta")
            for t in ("Alpha", "Beta"):
                r = await c.post("/exec", json={"code": "s", "timeout": 0.2, "target": t})
                assert r.status == 504
            assert (await c.post("/clear", json={})).status == 409   # two files: ambiguous
            r = await c.post("/clear", json={"target": "Alpha"})
            assert (await r.json())["cleared"] == [a.exec_ids[0][:8]]
            assert list(bridge.ABANDONED) == [b.exec_ids[0]]
        await c.close()
    run(go())


# ─── GET / and /targets ───────────────────────────────────────────────────


def test_targets_is_guarded_and_empty_without_plugins():
    async def go():
        c = await make_client()
        assert await (await c.get("/targets")).json() == {"files": []}
        r = await c.get("/targets", headers={"Origin": "https://evil.example"})
        assert r.status == 403
        await c.close()
    run(go())


def test_allowed_host_passes_the_host_check():
    async def go():
        c = await make_client()
        bridge.ALLOWED_HOSTS = {"localhost:8787"}
        r = await c.get("/status", headers={"Host": "LOCALHOST:8787"})
        assert r.status == 200, "Host check must be case-insensitive"
        await c.close()
    run(go())


def test_status_counts_pending_requests():
    async def go():
        c = await make_client()
        async with FakePlugin(c, reply=lambda code: {"type": "__drop__"}) as p:
            task = asyncio.create_task(c.post("/exec", json={"code": "x", "timeout": 5}))
            await asyncio.sleep(0.15)
            st = await (await c.get("/status")).json()
            assert st["pending"] == 1 and st["plugin_connected"] is True
            await p.ws.send_str(json.dumps({"type": "result", "id": p.exec_ids[0], "value": 1}))
            await task
            assert (await (await c.get("/status")).json())["pending"] == 0
        await c.close()
    run(go())


# ─── resolve_target, unit level ───────────────────────────────────────────

def _register(cid, name=None, sig=None, key=None, closed=False):
    bridge.PLUGINS[cid] = {"ws": StubWS(closed=closed), "name": name,
                           "docSig": sig, "fileKey": key}


def test_resolve_by_file_key_substring_and_misses():
    _register("c1" * 8, "Landing page", "sigland", "KEY1")
    _register("c2" * 8, "Landing app", "sigapp", "KEY2")
    _register("c3" * 8, "Dashboard", "sigdash", None)
    assert bridge.resolve_target("KEY2")[0] == "c2" * 8
    assert bridge.resolve_target("dash")[0] == "c3" * 8                # substring
    assert bridge.resolve_target("landing page")[0] == "c1" * 8        # exact, any case
    cid, why = bridge.resolve_target("landing")
    assert cid is None and "matches multiple files" in why
    cid, why = bridge.resolve_target("nothing")
    assert cid is None and "no connected file matches 'nothing'" in why
    cid, why = bridge.resolve_target(None)
    assert cid is None and "3 files connected" in why


def test_resolve_by_doc_id_or_connection_id_prefix():
    _register("aaaaaaaa-1111", "Same", "sharedA1")
    _register("bbbbbbbb-2222", "Same", "sharedB2")
    cid, why = bridge.resolve_target("Same")
    assert cid is None and "different documents sharing a name" in why
    assert bridge.resolve_target("sharedb")[0] == "bbbbbbbb-2222"   # doc id, any case
    assert bridge.resolve_target("aaaaaaaa")[0] == "aaaaaaaa-1111"
    # a 6+ char prefix shared by both documents picks neither
    cid, why = bridge.resolve_target("shared")
    assert cid is None and "no connected file matches" in why


def test_resolve_ignores_closed_sockets():
    _register("dead" * 4, "File", "sigdead", closed=True)
    cid, why = bridge.resolve_target(None)
    assert cid is None and "plugin not connected" in why
    _register("live" * 4, "File", "siglive")
    assert bridge.resolve_target("File")[0] == "live" * 4


def test_no_target_with_two_tabs_on_one_document_routes_to_newest():
    async def go():
        c = await make_client()
        async with FakePlugin(c) as first, FakePlugin(c) as second:
            await hello(first, "F", "oneDoc")
            await hello(second, "F", "oneDoc")
            r = await c.post("/exec", json={"code": "x", "timeout": 2})
            assert r.status == 200
            assert second.seen_codes == ["x"] and first.seen_codes == []
        await c.close()
    run(go())


def test_no_target_with_two_tabs_without_doc_sig_stays_ambiguous():
    _register("old1" * 4, "F")
    _register("old2" * 4, "F")
    cid, why = bridge.resolve_target(None)
    assert cid is None and "2 files connected" in why


# ─── odd plugin messages ──────────────────────────────────────────────────

def test_junk_on_the_plugin_socket_is_ignored_and_the_socket_lives():
    async def go():
        c = await make_client()
        async with FakePlugin(c) as p:
            await p.ws.send_str("{not json")
            await p.ws.send_bytes(b"\x00\x01binary")
            await p.ws.send_str(json.dumps({"type": "result", "id": 123, "value": 1}))
            await p.ws.send_str(json.dumps({"type": "result", "id": "nobody", "value": 1}))
            await p.ws.send_str(json.dumps({"type": "pong"}))   # unsolicited
            await asyncio.sleep(0.1)
            assert not p.ws.closed
            r = await c.post("/exec", json={"code": "still here", "timeout": 2})
            assert r.status == 200
        await c.close()
    run(go())


def test_log_for_an_orphan_does_not_lift_its_interlock():
    """A timed-out script that keeps printing is still running: only its final
    result or error lifts the interlock, never a log line."""
    async def go():
        c = await make_client()
        async with FakePlugin(c, reply=lambda code: {"type": "__drop__"}) as p:
            assert (await c.post("/exec", json={"code": "s", "timeout": 0.2})).status == 504
            rid = p.exec_ids[0]
            await p.ws.send_str(json.dumps({"type": "log", "id": rid, "text": "still going"}))
            await asyncio.sleep(0.05)
            assert rid in bridge.ABANDONED
            await p.ws.send_str(json.dumps({"type": "error", "id": rid, "text": "boom"}))
            await asyncio.sleep(0.05)
            assert rid not in bridge.ABANDONED
        await c.close()
    run(go())


def test_disconnect_clears_interlock_lock_and_queue_of_that_file():
    async def go():
        c = await make_client()
        p = FakePlugin(c, reply=lambda code: {"type": "__drop__"})
        await p.__aenter__()
        await hello(p, "Gone", "docGone")
        assert (await c.post("/exec", json={"code": "s", "timeout": 0.2})).status == 504
        assert bridge.ABANDONED and "doc:docGone" in bridge.LOCKS
        await p.__aexit__()
        await asyncio.sleep(0.1)
        assert bridge.ABANDONED == {}
        assert bridge.LOCKS == {} and bridge.QUEUE == {}
        assert bridge.PLUGINS == {}
        await c.close()
    run(go())


def test_disconnect_keeps_the_lock_while_another_tab_holds_the_document():
    async def go():
        c = await make_client()
        a = FakePlugin(c)
        await a.__aenter__()
        async with FakePlugin(c) as b:
            await hello(a, "F", "shared")
            await hello(b, "F", "shared")
            assert (await c.post("/exec", json={"code": "x", "timeout": 2})).status == 200
            await a.__aexit__()
            await asyncio.sleep(0.1)
            assert "doc:shared" in bridge.LOCKS
        await c.close()
    run(go())


def test_hello_with_a_file_key_drops_a_dead_twin():
    """A re-Run that reports a fileKey replaces a silent same-key connection
    even though the names differ (a renamed file)."""
    async def go():
        c = await make_client()
        old = FakePlugin(c)
        await old.__aenter__()
        await hello(old, "Old name", file_key="FK1")
        old.go_silent()
        async with FakePlugin(c) as new:
            await hello(new, "New name", file_key="FK1")
            await asyncio.sleep(bridge.SLOT_PROBE_TIMEOUT + 0.3)
            names = [f["name"] for f in (await (await c.get("/targets")).json())["files"]]
            assert names == ["New name"]
        await old.__aexit__()
        await c.close()
    run(go())


# ─── helpers that must swallow socket errors ──────────────────────────────

def test_still_alive_is_false_for_closed_or_broken_sockets():
    async def go():
        info = {"ws": StubWS(closed=True)}
        assert await bridge._still_alive(info) is False
        info = {"ws": StubWS(fail=True)}
        assert await bridge._still_alive(info) is False
        assert "pong" not in info
        info = {"ws": StubWS()}
        assert await bridge._still_alive(info, timeout=0.05) is False   # no pong
        assert info["ws"].sent == [{"type": "ping"}] and "pong" not in info
    run(go())


def test_update_and_peers_survive_a_broken_socket():
    async def go():
        await bridge._send_update(StubWS(fail=True), clear=True)   # no raise
        bridge.PLUGINS["x"] = {"ws": StubWS(fail=True), "hello": True}
        good = StubWS()
        bridge.PLUGINS["y"] = {"ws": good, "hello": True}
        await bridge._broadcast_peers()
        assert good.sent == [{"type": "peers", "index": 2, "total": 2}]
        await bridge._send_abort(StubWS(fail=True), "rid")          # no raise
    run(go())


def test_send_update_says_nothing_without_an_offer():
    async def go():
        ws = StubWS()
        await bridge._send_update(ws)
        assert ws.sent == []
        await bridge._send_update(ws, clear=True)
        assert ws.sent == [{"type": "update", "latest": None}]
    run(go())


# ─── version helpers ──────────────────────────────────────────────────────

def test_plugin_version_unreadable_or_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(bridge, "PLUGIN_CODE", tmp_path / "nope.js")
    assert bridge.expected_plugin_version() is None
    assert bridge.plugin_outdated({"pluginVersion": None}) is False
    f = tmp_path / "code.js"
    f.write_text("const x = 1;", encoding="utf-8")
    monkeypatch.setattr(bridge, "PLUGIN_CODE", f)
    assert bridge.expected_plugin_version() is None
    f.write_text('const PLUGIN_VERSION = "2099-01-01.1";', encoding="utf-8")
    assert bridge.expected_plugin_version() == "2099-01-01.1"
    assert bridge.plugin_outdated({"pluginVersion": "2099-01-01.1"}) is False
    assert bridge.plugin_outdated({}) is True


def test_changed_bridge_file_is_flagged_in_status_and_exec(monkeypatch):
    monkeypatch.setattr(bridge, "BRIDGE_DIGEST", "not-the-real-digest")

    async def go():
        c = await make_client()
        st = await (await c.get("/status")).json()
        assert st["bridge_outdated"] is True
        async with FakePlugin(c) as p:
            await p.ws.send_str(json.dumps({"type": "hello", "name": "F",
                                            "plugin": bridge.expected_plugin_version()}))
            await asyncio.sleep(0.05)
            body = await (await c.post("/exec", json={"code": "x", "timeout": 2})).json()
            assert "bridge.py changed" in body["notice"]
            assert "re-run" not in body["notice"]
        await c.close()
    run(go())


def test_find_hint_of_nothing_is_none():
    assert bridge.find_hint(None) is None
    assert bridge.find_hint("") is None
    assert "h.bS" in bridge.find_hint("strokes variable bindings must be set on paints directly")


def test_error_reply_keeps_stack_and_logs():
    async def go():
        c = await make_client()
        reply = lambda code: {"type": "error", "text": "x is not a function", "stack": "at line 1"}
        async with FakePlugin(c, reply=reply):
            body = await (await c.post("/exec", json={"code": "x()", "timeout": 2})).json()
            assert body["stack"] == "at line 1" and body["logs"] == []
            assert "deprecated" in body["hint"] and isinstance(body["elapsed_ms"], int)
        await c.close()
    run(go())


def test_error_reply_without_text_says_unknown_error():
    async def go():
        c = await make_client()
        async with FakePlugin(c, reply=lambda code: {"type": "error"}):
            r = await c.post("/exec", json={"code": "x", "timeout": 2})
            assert r.status == 500 and (await r.json())["error"] == "unknown error"
        await c.close()
    run(go())


# ─── chat over the plugin socket ──────────────────────────────────────────

class ChatTab(FakePlugin):
    def chat_events(self):
        return [m["event"] for m in self.received if m.get("type") == "chat"]


def test_chat_load_answers_with_a_history_event(monkeypatch):
    monkeypatch.setattr(chat, "state", lambda doc, lang=None: {
        "messages": [{"role": "user", "text": "Привіт"}], "busy": True,
        "activity": "читаю файл", "commands": ["/help"], "session": "s1", "lang": "uk"})

    async def go():
        c = await make_client()
        async with ChatTab(c) as t:
            await hello(t, "F", "docChat")
            await t.ws.send_str(json.dumps({"type": "chat-load"}))
            await asyncio.sleep(0.1)
            msg = [m for m in t.received if m.get("type") == "chat"][-1]
            assert msg["doc"] == "docChat"
            assert msg["event"] == {"kind": "history",
                                    "messages": [{"role": "user", "text": "Привіт"}],
                                    "busy": True, "activity": "читаю файл",
                                    "commands": ["/help"], "lang": "uk"}
        await c.close()
    run(go())


def test_chat_send_refused_by_chat_is_reported_to_the_window(monkeypatch):
    async def fake_send(doc, body):
        return 400, {"ok": False, "error": "empty message"}
    monkeypatch.setattr(chat, "send", fake_send)

    async def go():
        c = await make_client()
        async with ChatTab(c) as t:
            await hello(t, "F", "docChat")
            await t.ws.send_str(json.dumps({"type": "chat-send", "text": ""}))
            await asyncio.sleep(0.1)
            assert t.chat_events() == [{"kind": "refused", "error": "empty message"}]
        await c.close()
    run(go())


def test_chat_send_accepted_sends_no_refusal(monkeypatch):
    seen = []

    async def fake_send(doc, body):
        seen.append((doc, body["text"]))
        return 200, {"ok": True, "id": "m1"}
    monkeypatch.setattr(chat, "send", fake_send)

    async def go():
        c = await make_client()
        async with ChatTab(c) as t:
            await hello(t, "F", "docChat")
            await t.ws.send_str(json.dumps({"type": "chat-send", "text": "hi"}))
            await asyncio.sleep(0.1)
            assert seen == [("docChat", "hi")]
            assert t.chat_events() == []
        await c.close()
    run(go())


def test_chat_stop_goes_to_that_document(monkeypatch):
    stopped = []

    async def fake_stop(doc):
        stopped.append(doc)
    monkeypatch.setattr(chat, "stop", fake_stop)

    async def go():
        c = await make_client()
        async with ChatTab(c) as t:
            await hello(t, "F", "docStop")
            await t.ws.send_str(json.dumps({"type": "chat-stop"}))
            await asyncio.sleep(0.1)
            assert stopped == ["docStop"]
        await c.close()
    run(go())


@pytest.mark.parametrize("mtype", ["chat-load", "chat-stop"])
def test_chat_load_or_stop_without_a_doc_id_is_silent(monkeypatch, mtype):
    # bridge wraps chat calls in `except Exception`, so a raising stub would be
    # swallowed; record calls instead and assert none happened.
    touched = []

    def record(*a, **kw):
        touched.append(a)
        raise RuntimeError("chat must not be touched without a doc id")

    async def record_stop(*a, **kw):
        record(*a)
    monkeypatch.setattr(chat, "state", record)
    monkeypatch.setattr(chat, "stop", record_stop)

    async def go():
        c = await make_client()
        async with ChatTab(c) as t:
            await t.ws.send_str(json.dumps({"type": mtype}))
            await asyncio.sleep(0.1)
            assert t.chat_events() == [] and not t.ws.closed
            assert touched == []
        await c.close()
    run(go())


def test_chat_load_failure_keeps_the_socket_and_sends_no_refusal(monkeypatch):
    def broken(doc):
        raise RuntimeError("disk on fire")
    monkeypatch.setattr(chat, "state", broken)

    async def go():
        c = await make_client()
        async with ChatTab(c) as t:
            await hello(t, "F", "docChat")
            await t.ws.send_str(json.dumps({"type": "chat-load"}))
            await asyncio.sleep(0.1)
            assert t.chat_events() == [] and not t.ws.closed
            assert (await c.post("/exec", json={"code": "x", "timeout": 2})).status == 200
        await c.close()
    run(go())


def test_chat_peers_lists_only_windows_on_that_document():
    a, b = StubWS(), StubWS()
    bridge.PLUGINS["1"] = {"ws": a, "name": "A", "docSig": "d1"}
    bridge.PLUGINS["2"] = {"ws": b, "name": "B", "docSig": "d2"}
    bridge.PLUGINS["3"] = {"ws": StubWS(closed=True), "name": "A", "docSig": "d1"}
    assert bridge._chat_peers("d1") == [(a, "A")]


# ─── startup / shutdown ───────────────────────────────────────────────────

def test_update_task_starts_with_the_app_and_stops_with_it(monkeypatch):
    calls = []

    async def fake_check():
        calls.append(1)
        return None
    monkeypatch.delenv("FIGMOSHA_NO_UPDATE_CHECK", raising=False)
    monkeypatch.delenv("FIGMOSHA_FAKE_LATEST", raising=False)
    monkeypatch.setattr(bridge, "check_for_update", fake_check)
    monkeypatch.setattr(bridge, "UPDATE_FIRST_DELAY", 0)

    async def go():
        from aiohttp.test_utils import TestServer
        app = bridge.build_app()
        bridge.install_update_checks(app)
        server = TestServer(app)
        await server.start_server()
        await asyncio.sleep(0.05)
        task = app["update_task"]
        assert not task.done() and calls == [1]
        await server.close()
        await asyncio.sleep(0)
        assert task.cancelled() or task.done()
    run(go())


def test_stop_update_checks_without_a_task_is_harmless():
    run(bridge._stop_update_checks({}))


def test_bind_hosts_without_ipv6(monkeypatch):
    import socket

    class NoV6:
        def __init__(self, *a, **kw):
            raise OSError("no IPv6")
    monkeypatch.setattr(socket, "socket", NoV6)
    assert bridge._bind_hosts("127.0.0.1", 0) == ["127.0.0.1"]
    assert bridge._bind_hosts("localhost", 0) == ["localhost"]


def test_quiet_connection_resets_only_drops_resets():
    class Loop:
        def __init__(self):
            self.passed = []

        def default_exception_handler(self, ctx):
            self.passed.append(ctx)
    loop = Loop()
    bridge._quiet_connection_resets(loop, {"exception": ConnectionResetError()})
    assert loop.passed == []
    other = {"exception": ValueError("real"), "message": "x"}
    bridge._quiet_connection_resets(loop, other)
    bridge._quiet_connection_resets(loop, {"message": "no exception"})
    assert loop.passed == [other, {"message": "no exception"}]


def _run_main(monkeypatch, argv):
    seen = {}

    def fake_run_app(app, **kw):
        seen["app"], seen["kw"] = app, kw
        seen["handler"] = kw["loop"].get_exception_handler()
        kw["loop"].close()
    monkeypatch.setattr(sys, "argv", ["bridge.py", *argv])
    monkeypatch.setattr(bridge.web, "run_app", fake_run_app)
    monkeypatch.setattr(bridge, "_bind_hosts", lambda host, port: [host])
    monkeypatch.setenv("FIGMOSHA_NO_UPDATE_CHECK", "1")
    bridge.main()
    return seen


def test_main_on_loopback_pins_the_host_header(monkeypatch):
    seen = _run_main(monkeypatch, ["--port", "9123"])
    assert bridge.ALLOWED_HOSTS == {"localhost:9123", "127.0.0.1:9123", "[::1]:9123"}
    assert seen["kw"]["host"] == ["127.0.0.1"] and seen["kw"]["port"] == 9123
    assert seen["handler"] is bridge._quiet_connection_resets
    assert isinstance(seen["app"], web.Application)


def test_main_on_a_lan_address_turns_the_host_check_off(monkeypatch):
    seen = _run_main(monkeypatch, ["--host", "0.0.0.0"])
    assert bridge.ALLOWED_HOSTS == set()
    assert seen["kw"]["host"] == ["0.0.0.0"] and seen["kw"]["port"] == 8787


def test_main_installs_update_checks_unless_turned_off(monkeypatch):
    seen = _run_main(monkeypatch, [])
    assert bridge._start_update_checks not in seen["app"].on_startup
    monkeypatch.delenv("FIGMOSHA_NO_UPDATE_CHECK")
    monkeypatch.setattr(sys, "argv", ["bridge.py"])
    got = {}
    monkeypatch.setattr(bridge.web, "run_app",
                        lambda app, **kw: (got.setdefault("app", app), kw["loop"].close()))
    bridge.main()
    assert bridge._start_update_checks in got["app"].on_startup


# ─── auto update ──────────────────────────────────────────────────────────
# Designers never type git. An agent installed Figmosha with `git clone`, so a
# newer release installs itself: fetch the tag, fast-forward, restart once the
# bridge is idle. A developer working copy (dirty or off master) is never
# touched: it keeps the old offer. Every git repo here is a temp one, with a
# local bare repo as origin: no network.

import os  # noqa: E402
import subprocess  # noqa: E402
import types  # noqa: E402


def _sh(cwd, *args):
    out = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


@pytest.fixture
def git_env(monkeypatch, tmp_path):
    """Isolated git config: a name for commits, master as the default branch."""
    cfg = tmp_path / "gitconfig"
    cfg.write_text("[user]\n\tname = t\n\temail = t@t\n[init]\n\tdefaultBranch = master\n"
                   "[advice]\n\tdetachedHead = false\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    return tmp_path


def _commit(repo, files, msg="c"):
    for name, text in files.items():
        (repo / name).write_text(text)
    _sh(repo, "add", "-A")
    _sh(repo, "commit", "-q", "-m", msg)
    return _sh(repo, "rev-parse", "HEAD")


@pytest.fixture
def release_repos(git_env, monkeypatch):
    """dev (where releases are made) -> origin.git (bare) -> app (the install).

    Returns a namespace with dev, origin, app and release(version, files)."""
    root = git_env
    dev, origin, app = root / "dev", root / "origin.git", root / "app"
    dev.mkdir()
    _sh(dev, "init", "-q")
    _commit(dev, {"bridge.py": "v1\n", "requirements.txt": "aiohttp\n", "README.md": "a\n"})
    _sh(dev, "tag", "v1.0.0")
    _sh(root, "clone", "-q", "--bare", str(dev), str(origin))
    _sh(root, "clone", "-q", str(origin), str(app))
    _sh(dev, "remote", "add", "origin", str(origin))

    def release(version, files):
        sha = _commit(dev, files, f"release {version}")
        _sh(dev, "tag", f"v{version}")
        _sh(dev, "push", "-q", "origin", "master", f"v{version}")
        return sha

    monkeypatch.setattr(bridge, "AUTO_UPDATE_DIR", app, raising=False)
    return types.SimpleNamespace(dev=dev, origin=origin, app=app, release=release)


# update_method: "git" only for a clean master clone

def test_update_dir_defaults_to_the_bridge_folder():
    assert Path(bridge.AUTO_UPDATE_DIR) == bridge.HERE


def test_update_method_is_git_for_a_clean_master_clone(release_repos):
    assert bridge.update_method() == "git"
    # untracked files (logs, a venv) do not make it a developer copy
    (release_repos.app / "bridge.log").write_text("x")
    assert bridge.update_method() == "git"


def test_update_method_is_none_for_a_dirty_clone(release_repos, capsys):
    (release_repos.app / "bridge.py").write_text("my local edit\n")
    capsys.readouterr()
    assert bridge.update_method() is None
    assert capsys.readouterr().out.strip()          # says why, once
    assert (release_repos.app / "bridge.py").read_text() == "my local edit\n"


def test_update_method_is_none_off_master(release_repos, capsys):
    _sh(release_repos.app, "checkout", "-q", "-b", "feature")
    capsys.readouterr()
    assert bridge.update_method() is None
    assert capsys.readouterr().out.strip()


def test_update_method_is_none_without_a_git_folder(monkeypatch, tmp_path, capsys):
    (tmp_path / "bridge.py").write_text("x")
    monkeypatch.setattr(bridge, "AUTO_UPDATE_DIR", tmp_path, raising=False)
    assert bridge.update_method() is None
    assert capsys.readouterr().out.strip()


def test_update_method_is_none_when_git_cannot_run(release_repos, monkeypatch):
    def no_git(*a, **kw):
        raise FileNotFoundError("git")
    monkeypatch.setattr(bridge.subprocess, "run", no_git)
    assert bridge.update_method() is None


def test_git_helper_runs_in_the_update_dir_and_never_raises(release_repos, monkeypatch):
    code, out = bridge._git("rev-parse", "--abbrev-ref", "HEAD")
    assert code == 0 and out.strip() == "master"
    code, _ = bridge._git("no-such-command")
    assert code != 0

    seen = {}

    def boom(*a, **kw):
        seen.update(kw)
        raise subprocess.TimeoutExpired("git", kw.get("timeout", 0))
    monkeypatch.setattr(bridge.subprocess, "run", boom)
    code, _ = bridge._git("fetch")
    assert code != 0 and seen.get("timeout") == 60


# apply_update: fetch the tag, fast-forward only

def test_apply_update_fast_forwards_to_the_release_tag(release_repos, capsys):
    sha = release_repos.release("9.0.0", {"bridge.py": "v9\n"})
    capsys.readouterr()
    assert bridge.apply_update("9.0.0") is True
    assert _sh(release_repos.app, "rev-parse", "HEAD") == sha
    assert (release_repos.app / "bridge.py").read_text() == "v9\n"
    assert _sh(release_repos.app, "rev-parse", "--abbrev-ref", "HEAD") == "master"
    assert "[update]" in capsys.readouterr().out


def test_apply_update_refuses_a_diverged_repo_and_leaves_it(release_repos):
    release_repos.release("9.0.0", {"bridge.py": "v9\n"})
    local = _commit(release_repos.app, {"bridge.py": "local work\n"}, "local")
    assert bridge.apply_update("9.0.0") is False
    assert _sh(release_repos.app, "rev-parse", "HEAD") == local
    assert (release_repos.app / "bridge.py").read_text() == "local work\n"
    assert _sh(release_repos.app, "status", "--porcelain", "--untracked-files=no") == ""


def test_apply_update_of_a_missing_tag_is_false(release_repos):
    before = _sh(release_repos.app, "rev-parse", "HEAD")
    assert bridge.apply_update("9.9.9") is False
    assert _sh(release_repos.app, "rev-parse", "HEAD") == before


def test_apply_update_never_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(bridge, "AUTO_UPDATE_DIR", tmp_path / "missing", raising=False)
    assert bridge.apply_update("9.0.0") is False

    def boom(*a, **kw):
        raise OSError("disk gone")
    monkeypatch.setattr(bridge, "AUTO_UPDATE_DIR", tmp_path, raising=False)
    monkeypatch.setattr(bridge.subprocess, "run", boom)
    assert bridge.apply_update("9.0.0") is False


def _record_pip(monkeypatch, returncode=0):
    """Let git through, record (and fake) every pip call."""
    calls = []
    real_run = subprocess.run

    def is_pip(cmd):
        return isinstance(cmd, (list, tuple)) and cmd and cmd[0] == sys.executable

    def fake_run(cmd, *a, **kw):
        if is_pip(cmd):
            calls.append((list(cmd), kw.get("cwd")))
            return subprocess.CompletedProcess(cmd, returncode, "", "pip failed" if returncode else "")
        return real_run(cmd, *a, **kw)

    def fake_call(cmd, *a, **kw):
        if is_pip(cmd):
            calls.append((list(cmd), kw.get("cwd")))
            if returncode:
                raise subprocess.CalledProcessError(returncode, cmd)
            return 0
        raise AssertionError("unexpected subprocess call")
    monkeypatch.setattr(bridge.subprocess, "run", fake_run)
    monkeypatch.setattr(bridge.subprocess, "check_call", fake_call)
    monkeypatch.setattr(bridge.subprocess, "call", fake_call)
    return calls


def test_apply_update_installs_requirements_only_when_they_changed(release_repos, monkeypatch):
    calls = _record_pip(monkeypatch)
    release_repos.release("9.0.0", {"README.md": "b\n"})
    assert bridge.apply_update("9.0.0") is True
    assert calls == []

    release_repos.release("9.1.0", {"requirements.txt": "aiohttp\nnewdep\n"})
    assert bridge.apply_update("9.1.0") is True
    assert len(calls) == 1
    cmd, cwd = calls[0]
    assert cmd == [sys.executable, "-m", "pip", "install", "-q", "-r", "requirements.txt"]
    assert cwd is not None and Path(cwd).resolve() == release_repos.app.resolve()


def test_pip_failure_rolls_back_the_update(release_repos, monkeypatch, capsys):
    calls = _record_pip(monkeypatch, returncode=1)
    old = _sh(release_repos.app, "rev-parse", "HEAD")
    release_repos.release("9.0.0", {"requirements.txt": "aiohttp\nnewdep\n"})
    capsys.readouterr()
    assert bridge.apply_update("9.0.0") is False
    assert len(calls) == 1
    assert _sh(release_repos.app, "rev-parse", "HEAD") == old
    assert _sh(release_repos.app, "status", "--porcelain", "--untracked-files=no") == ""
    assert capsys.readouterr().out.strip()


# bridge_idle: nothing running anywhere

def test_bridge_idle_when_nothing_runs():
    assert bridge.bridge_idle() is True


def test_bridge_not_idle_with_a_pending_exec():
    bridge.PENDING["rid"] = {"future": None, "logs": [], "t0": 0, "conn": "c"}
    assert bridge.bridge_idle() is False


def test_bridge_idle_follows_the_queue_holder():
    bridge.QUEUE["doc"] = {"waiting": [], "running": {"agent": "a", "since": 0}}
    assert bridge.bridge_idle() is False
    bridge.QUEUE["doc"] = {"waiting": [], "running": None}
    assert bridge.bridge_idle() is True


def test_bridge_idle_follows_busy_chat_sessions(monkeypatch):
    monkeypatch.setitem(chat.SESSIONS, "doc-a", types.SimpleNamespace(busy=False))
    assert bridge.bridge_idle() is True
    monkeypatch.setitem(chat.SESSIONS, "doc-b", types.SimpleNamespace(busy=True))
    assert bridge.bridge_idle() is False


# the update loop

class _AutoRig:
    """Fakes for the update loop: a release check, apply_update, idle, restart."""

    def __init__(self, monkeypatch, *, latest="9.0.0", method="git", ok=True, idle=True,
                 every=10):
        self.latest, self.ok, self.idle = latest, ok, idle
        self.checks, self.applied, self.restarts = 0, [], 0
        self.state_during_apply = None
        monkeypatch.delenv("FIGMOSHA_NO_AUTO_UPDATE", raising=False)
        monkeypatch.delenv("FIGMOSHA_FAKE_LATEST", raising=False)
        monkeypatch.setattr(bridge, "UPDATE_FIRST_DELAY", 0)
        monkeypatch.setattr(bridge, "UPDATE_EVERY", every)
        monkeypatch.setattr(bridge, "AUTO_RESTART_POLL", 0.005, raising=False)
        monkeypatch.setattr(bridge, "check_for_update", self.check)
        monkeypatch.setattr(bridge, "update_method", lambda: method, raising=False)
        monkeypatch.setattr(bridge, "apply_update", self.apply, raising=False)
        monkeypatch.setattr(bridge, "bridge_idle", lambda: self.idle, raising=False)
        monkeypatch.setattr(bridge, "restart_bridge", self.restart, raising=False)

    async def check(self):
        self.checks += 1
        return self.latest

    def apply(self, version):
        self.state_during_apply = dict(bridge.AUTO_UPDATE or {})
        self.applied.append(version)
        return self.ok

    def restart(self):
        self.restarts += 1


def _offers(plugin):
    return [m for m in plugin.received if m.get("type") == "update" and m.get("latest")]


def test_auto_update_installs_without_offering_and_restarts(monkeypatch):
    rig = _AutoRig(monkeypatch)

    async def go():
        c = await make_client()
        async with FakePlugin(c) as p:
            task = asyncio.create_task(bridge._update_loop(None))
            await asyncio.sleep(0.08)
            async with FakePlugin(c) as late:
                assert _offers(late) == []
            task.cancel()
            assert _offers(p) == []
        await c.close()
    run(go())
    assert rig.applied == ["9.0.0"]
    assert rig.state_during_apply == {"version": "9.0.0", "state": "installing"}
    assert bridge.AUTO_UPDATE == {"version": "9.0.0", "state": "installed"}
    assert rig.restarts == 1


def test_auto_update_restarts_only_once_the_bridge_is_idle(monkeypatch):
    rig = _AutoRig(monkeypatch, idle=False)

    async def go():
        task = asyncio.create_task(bridge._update_loop(None))
        await asyncio.sleep(0.06)
        assert rig.applied == ["9.0.0"] and rig.restarts == 0
        rig.idle = True
        await asyncio.sleep(0.05)
        task.cancel()
    run(go())
    assert rig.restarts == 1


def test_failed_auto_update_offers_nothing_and_waits_for_the_next_check(monkeypatch):
    rig = _AutoRig(monkeypatch, ok=False)

    async def go():
        c = await make_client()
        async with FakePlugin(c) as p:
            task = asyncio.create_task(bridge._update_loop(None))
            await asyncio.sleep(0.08)
            task.cancel()
            assert _offers(p) == []
        await c.close()
    run(go())
    assert rig.applied == ["9.0.0"]                 # no tight retry loop
    assert rig.restarts == 0
    assert bridge.AUTO_UPDATE == {"version": "9.0.0", "state": "failed"}


def test_failed_auto_update_is_tried_again_at_the_next_check(monkeypatch):
    rig = _AutoRig(monkeypatch, ok=False, every=0.02)

    async def go():
        task = asyncio.create_task(bridge._update_loop(None))
        await asyncio.sleep(0.09)
        task.cancel()
    run(go())
    assert len(rig.applied) >= 2
    assert len(rig.applied) <= rig.checks           # one try per check, never more
    assert rig.restarts == 0


def test_no_newer_release_installs_nothing(monkeypatch):
    rig = _AutoRig(monkeypatch, latest=bridge.VERSION)

    async def go():
        task = asyncio.create_task(bridge._update_loop(None))
        await asyncio.sleep(0.05)
        task.cancel()
    run(go())
    assert rig.applied == [] and rig.restarts == 0 and bridge.AUTO_UPDATE is None


@pytest.mark.parametrize("why", ["developer_clone", "no_auto_update", "fake_latest"])
def test_auto_update_off_keeps_the_offer(monkeypatch, why):
    """A developer clone, FIGMOSHA_NO_AUTO_UPDATE and FIGMOSHA_FAKE_LATEST
    never install: the plugins get today's offer."""
    rig = _AutoRig(monkeypatch, method=None if why == "developer_clone" else "git")
    if why == "no_auto_update":
        monkeypatch.setenv("FIGMOSHA_NO_AUTO_UPDATE", "1")
    if why == "fake_latest":
        monkeypatch.setenv("FIGMOSHA_FAKE_LATEST", "9.9.9")

    async def go():
        c = await make_client()
        async with FakePlugin(c) as p:
            task = asyncio.create_task(bridge._update_loop(None))
            await asyncio.sleep(0.08)
            task.cancel()
            assert _offers(p)
        await c.close()
    run(go())
    assert rig.applied == [] and rig.restarts == 0
    assert bridge.UPDATE is not None
    assert bridge.AUTO_UPDATE is None


# restart_bridge and main()

def test_restart_bridge_execs_the_same_command_and_marks_the_restart(monkeypatch):
    seen = {}
    flushed = []

    class Stream:
        def __init__(self, name):
            self.name = name

        def write(self, s):
            return len(s)

        def flush(self):
            flushed.append(self.name)

    def fake_execv(path, args):
        seen["path"], seen["args"] = path, list(args)
        seen["env"] = os.environ.get("FIGMOSHA_RESTARTED")
    monkeypatch.setenv("FIGMOSHA_RESTARTED", "0")
    monkeypatch.delenv("FIGMOSHA_RESTARTED")
    monkeypatch.setattr(bridge.os, "execv", fake_execv)
    monkeypatch.setattr(sys, "argv", ["bridge.py", "--port", "9123"])
    monkeypatch.setattr(sys, "stdout", Stream("out"))
    monkeypatch.setattr(sys, "stderr", Stream("err"))
    bridge.restart_bridge()
    assert seen["path"] == sys.executable
    assert seen["args"] == [sys.executable, "bridge.py", "--port", "9123"]
    assert seen["env"] == "1"
    assert "out" in flushed and "err" in flushed


def _flaky_run_app(monkeypatch, fails):
    calls = []

    def fake_run_app(app, **kw):
        calls.append(1)
        if fails is None or len(calls) <= fails:
            raise OSError(98, "address already in use")
        kw["loop"].close()
    monkeypatch.setattr(sys, "argv", ["bridge.py", "--port", "9123"])
    monkeypatch.setattr(bridge.web, "run_app", fake_run_app)
    monkeypatch.setattr(bridge, "_bind_hosts", lambda host, port: [host])
    monkeypatch.setattr(bridge.time, "sleep", lambda s: None)
    monkeypatch.setenv("FIGMOSHA_NO_UPDATE_CHECK", "1")
    return calls


def test_main_after_a_restart_waits_for_the_port(monkeypatch):
    calls = _flaky_run_app(monkeypatch, fails=3)
    monkeypatch.setenv("FIGMOSHA_RESTARTED", "1")
    bridge.main()
    assert len(calls) == 4


def test_main_after_a_restart_gives_up_in_the_end(monkeypatch):
    calls = _flaky_run_app(monkeypatch, fails=None)
    monkeypatch.setenv("FIGMOSHA_RESTARTED", "1")
    with pytest.raises(OSError):
        bridge.main()
    assert 10 <= len(calls) <= 30


def test_main_without_a_restart_does_not_retry(monkeypatch):
    calls = _flaky_run_app(monkeypatch, fails=None)
    with pytest.raises(OSError):
        bridge.main()
    assert len(calls) == 1


# /status

def test_status_reports_auto_update():
    async def go():
        c = await make_client()
        st = await (await c.get("/status")).json()
        assert "update" in st and st["auto_update"] is None
        bridge.AUTO_UPDATE = {"version": "9.0.0", "state": "installed"}
        st = await (await c.get("/status")).json()
        assert st["auto_update"] == {"version": "9.0.0", "state": "installed"}
        await c.close()
    run(go())


# the update loop must not restart forever, die quietly or cut a chat message

def test_a_release_that_still_reports_the_old_version_is_not_restarted_for_again(monkeypatch):
    """A tag whose code still says an older VERSION: after one restart the
    bridge does not install and restart again for it at every start."""
    rig = _AutoRig(monkeypatch)
    monkeypatch.setenv("FIGMOSHA_UPDATED_TO", "9.0.0")     # this process restarted for 9.0.0

    async def go():
        task = asyncio.create_task(bridge._update_loop(None))
        await asyncio.sleep(0.05)
        task.cancel()
    run(go())
    assert rig.applied == [] and rig.restarts == 0
    assert bridge.AUTO_UPDATE["state"] == "failed"


def test_auto_update_marks_the_version_it_restarts_for(monkeypatch):
    rig = _AutoRig(monkeypatch)
    monkeypatch.delenv("FIGMOSHA_UPDATED_TO", raising=False)
    seen = {}
    rig.restart = lambda: seen.setdefault("to", os.environ.get("FIGMOSHA_UPDATED_TO"))
    monkeypatch.setattr(bridge, "restart_bridge", rig.restart)

    async def go():
        task = asyncio.create_task(bridge._update_loop(None))
        await asyncio.sleep(0.05)
        task.cancel()
    run(go())
    assert seen["to"] == "9.0.0"


def test_a_failed_restart_keeps_the_update_loop_alive(monkeypatch):
    rig = _AutoRig(monkeypatch, every=0.02)

    def broken_restart():
        rig.restarts += 1
        raise OSError("python moved")
    monkeypatch.setattr(bridge, "restart_bridge", broken_restart)

    async def go():
        task = asyncio.create_task(bridge._update_loop(None))
        await asyncio.sleep(0.09)
        alive = not task.done()
        task.cancel()
        return alive
    assert run(go()) is True
    assert rig.restarts >= 2                       # tried again at the next check
    assert bridge.AUTO_UPDATE["state"] == "failed"


def test_restart_on_windows_quotes_paths_with_spaces(monkeypatch):
    seen = {}
    monkeypatch.setattr(bridge.os, "execv", lambda path, args: seen.update(args=list(args)))
    monkeypatch.setattr(bridge, "_WINDOWS", True, raising=False)
    monkeypatch.setattr(sys, "argv", [r"C:\Users\Anna K\figmosha2\bridge.py", "--port", "9123"])
    bridge.restart_bridge()
    assert seen["args"][1] == r'"C:\Users\Anna K\figmosha2\bridge.py"'
    assert seen["args"][2:] == ["--port", "9123"]


def test_bridge_is_busy_while_a_chat_message_is_being_stored(monkeypatch):
    """The designer's message is saved before the chat turns busy: a restart
    in that gap would leave the message without an answer."""
    gate = asyncio.Event

    class Slow:
        busy = False

        def __init__(self):
            self.gate = gate()

        def set_lang(self, lang):
            pass

        def find(self, i):
            return None

        def t(self, key, **kw):
            return key

        async def add(self, *a, **kw):
            await self.gate.wait()
            return {"id": 1}
    monkeypatch.setattr(chat, "providers", lambda: [])

    async def go():
        s = Slow()
        monkeypatch.setattr(chat, "session", lambda doc: s)
        task = asyncio.create_task(chat.send("doc1", {"text": "hi"}))
        await asyncio.sleep(0.01)
        during = bridge.bridge_idle()
        s.gate.set()
        await task
        return during
    assert run(go()) is False
    assert bridge.bridge_idle() is True
