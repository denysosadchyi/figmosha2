"""Randomized and hostile tests: scenario fuzzing, a misbehaving plugin, and
attacks on the request guard.

The scenario fuzzer drives the bridge through random sequences of plugins
connecting and dropping, tabs on shared documents, agents writing, giving up,
hanging up and failing — and after every scenario checks the invariants that
matter for several agents sharing files:

  * a document never runs two scripts at once,
  * no lock, queue entry or pending request is left behind,
  * every file is immediately usable again.

    pytest tests/test_fuzz.py
    FIGMOSHA_FUZZ_SEEDS=500 pytest tests/test_fuzz.py -k scenario   # longer run
"""
import asyncio
import json
import os
import random

import aiohttp
import pytest

import bridge
from test_bridge import make_client, run
from test_stress import Document, SandboxPlugin, call

SEEDS = int(os.environ.get("FIGMOSHA_FUZZ_SEEDS", "40"))


# ─── scenario fuzzer ──────────────────────────────────────────────────────

async def scenario(seed):
    rng = random.Random(seed)
    c = await make_client()
    docs = {f"doc{seed}x{i}": Document() for i in range(rng.randint(1, 3))}
    plugins = []          # live SandboxPlugins
    inflight = []         # (task, kind)

    async def open_tab(doc_id):
        p = SandboxPlugin(c, f"File-{doc_id}", doc_id, docs[doc_id])
        await p.__aenter__()
        plugins.append(p)

    for doc_id in docs:
        await open_tab(doc_id)

    for _ in range(rng.randint(20, 60)):
        action = rng.random()
        doc_id = rng.choice(list(docs))
        if action < 0.55:
            cmd = rng.choice([{"op": "rmw", "pause": rng.uniform(0, 0.01)},
                              {"op": "sleep", "s": rng.uniform(0, 0.02)},
                              {"op": "error"}])
            body = {"target": doc_id, "timeout": 3, "agent": f"a{rng.randint(1, 5)}"}
            if rng.random() < 0.2:
                body["queue_timeout"] = rng.uniform(0.001, 0.03)
            task = asyncio.create_task(call(c, cmd, **body))
            if rng.random() < 0.15:  # the agent hangs up at a random moment
                asyncio.get_running_loop().call_later(rng.uniform(0, 0.05), task.cancel)
            inflight.append(task)
        elif action < 0.70 and len(plugins) > 1:
            p = plugins.pop(rng.randrange(len(plugins)))   # a tab closes
            await p.__aexit__()
        elif action < 0.85:
            await open_tab(doc_id)                         # a tab opens
        else:
            await asyncio.sleep(rng.uniform(0, 0.02))

    # Every document needs a live tab for the final checks.
    for doc_id in docs:
        if not any(p.doc == doc_id for p in plugins):
            await open_tab(doc_id)
    await asyncio.wait_for(asyncio.gather(*inflight, return_exceptions=True), timeout=20)
    await asyncio.sleep(0.2)

    for doc_id, d in docs.items():
        assert d.max_running <= 1, f"seed {seed}: two scripts ran at once in {doc_id}"
        r = await call(c, {"op": "rmw", "pause": 0}, target=doc_id, queue_timeout=1)
        assert r.status == 200, f"seed {seed}: {doc_id} stuck: {await r.text()}"
    files = (await (await c.get("/status")).json())["files"]
    assert all(f["queue"] == {"running": None, "waiting": []} for f in files), (seed, files)
    assert bridge.PENDING == {}, f"seed {seed}: leaked pending {list(bridge.PENDING)}"
    assert not any(lock.locked() for lock in bridge.LOCKS.values()), f"seed {seed}: leaked lock"

    for p in plugins:
        await p.__aexit__()
    await c.close()


@pytest.mark.parametrize("seed", range(SEEDS))
def test_random_scenario_keeps_invariants(seed):
    run(scenario(seed))


# ─── a misbehaving plugin ─────────────────────────────────────────────────

HOSTILE_PLUGIN_MESSAGES = [
    "not json at all",
    "[]",
    "null",
    "42",
    '{"type": 5}',
    '{"type": "result"}',                                   # no id
    '{"type": "result", "id": "no-such-request", "value": 1}',
    '{"type": "result", "id": ["list"], "value": 1}',
    '{"type": "error", "id": {"o": 1}}',
    '{"type": "log", "id": "nope", "lines": "not a list"}',
    '{"type": "hello", "name": 12345, "docSig": ["x"], "plugin": {"v": 1}}',
    '{"type": "hello", "name": "' + "N" * 100000 + '"}',
    '{"type": "pong"}',
    '{"type": "totally-unknown"}',
]


def test_hostile_plugin_messages_do_not_break_the_bridge():
    """A buggy or hostile plugin connection sends junk; the bridge must keep
    serving the well-behaved file next to it."""
    async def go():
        c = await make_client()
        async with SandboxPlugin(c, "Good", "docGOOD01"):
            bad = await c.ws_connect("/plugin", headers={"Origin": "null"})
            for msg in HOSTILE_PLUGIN_MESSAGES:
                await bad.send_str(msg)
            await bad.send_bytes(b"\x00\xff binary")
            await asyncio.sleep(0.2)
            r = await call(c, {"op": "rmw", "pause": 0}, target="docGOOD01")
            assert r.status == 200
            assert (await c.get("/status")).status == 200
            assert (await c.get("/targets")).status == 200
            await bad.close()
        await c.close()
    run(go())


def test_duplicate_and_late_results_are_ignored():
    """A plugin answering one request twice, or answering after the timeout,
    must not complete some other caller's request or corrupt state."""
    async def go():
        c = await make_client()
        ws = await c.ws_connect("/plugin", headers={"Origin": "null"})
        await ws.send_str(json.dumps({"type": "hello", "name": "A", "docSig": "docAAAAAA"}))
        await asyncio.sleep(0.05)
        task = asyncio.create_task(call(c, {"op": "x"}, timeout=2))
        msg = json.loads((await ws.receive()).data)
        while msg.get("type") != "exec":
            msg = json.loads((await ws.receive()).data)
        rid = msg["id"]
        for value in ("first", "second", "third"):
            await ws.send_str(json.dumps({"type": "result", "id": rid, "value": value}))
        body = await (await task).json()
        assert body["value"] == "first"
        assert bridge.PENDING == {}
        await ws.close()
        await c.close()
    run(go())


# ─── the request guard under attack ───────────────────────────────────────

@pytest.mark.parametrize("headers", [
    {"Origin": "https://evil.example"},
    {"Origin": "null"},                                   # null is for the plugin WS only
    {"Origin": "http://localhost:8787"},                  # even "same-looking" origins
    {"Host": "evil.example"},
    {"Host": "localhost.evil.example:8787"},
    {"Host": "127.0.0.1.nip.io:8787"},                    # DNS rebinding style
    {"Host": "localhost:9999"},
    {"Host": ""},
])
def test_guard_refuses_browser_and_rebinding_requests(headers):
    async def go():
        c = await make_client()
        h = {"Host": "127.0.0.1:8787", **headers}
        async with SandboxPlugin(c, "A", "docAAAAAA") as p:
            # Pin the Host check the way main() does for a real bridge on 8787
            # (after the test plugin is connected, which uses the test port).
            bridge.ALLOWED_HOSTS = {"localhost:8787", "127.0.0.1:8787", "[::1]:8787"}
            for method, path, data in (("GET", "/status", None),
                                       ("POST", "/exec", json.dumps({"code": json.dumps({"op": "rmw"})})),
                                       ("POST", "/clear", "{}")):
                r = await c.request(method, path, data=data,
                                    headers={**h, "Content-Type": "text/plain"})
                assert r.status == 403, (headers, path, r.status)
            assert p.counter == 0, "a refused request still ran a script"
        await c.close()
    run(go())


def test_websocket_from_a_web_page_is_refused():
    async def go():
        c = await make_client()
        for origin in ("https://evil.example", "http://localhost:3000"):
            with pytest.raises(aiohttp.WSServerHandshakeError):
                await c.ws_connect("/plugin", headers={"Origin": origin})
        assert bridge.PLUGINS == {}
        await c.close()
    run(go())


# ─── endurance ────────────────────────────────────────────────────────────

def test_thousands_of_execs_leave_no_residue():
    """3000 execs across 3 files, some failing, some impatient, some hung up:
    the bridge's bookkeeping returns to empty — nothing grows without bound."""
    async def go():
        c = await make_client()
        rng = random.Random(1)
        plugins = [SandboxPlugin(c, f"F{i}", f"docsoak{i}x") for i in range(3)]
        for p in plugins:
            await p.__aenter__()
        for batch in range(30):
            tasks = []
            for i in range(100):
                target = f"docsoak{rng.randrange(3)}x"
                cmd = {"op": "error"} if rng.random() < 0.1 else {"op": "rmw", "pause": 0}
                extra = {"queue_timeout": 0.001} if rng.random() < 0.1 else {}
                t = asyncio.create_task(call(c, cmd, target=target, **extra))
                if rng.random() < 0.05:
                    t.cancel()
                tasks.append(t)
            await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.sleep(0.2)
        assert bridge.PENDING == {}
        assert bridge.ABANDONED == {}
        assert len(bridge.LOCKS) <= 3 and len(bridge.QUEUE) <= 3
        assert all(q == {"waiting": [], "running": None} for q in bridge.QUEUE.values())
        assert all(p.max_running == 1 for p in plugins)
        for p in plugins:
            await p.__aexit__()
        await c.close()
    run(go())
