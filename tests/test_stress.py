"""Stress and chaos tests for the bridge's per-document queue.

The fake plugin here behaves like the real sandbox where it matters: it runs
every exec concurrently (an async handler that yields at each await), so two
scripts that are not serialized by the bridge really do interleave. Scripts are
tiny JSON commands instead of JavaScript:

    {"op": "rmw", "pause": 0.01}   read a shared counter, pause, write it + 1
    {"op": "sleep", "s": 0.2}      take a while
    {"op": "echo", "n": 5000000}   return a result of n bytes
    {"op": "error"}                throw
    {"op": "never"}                never answer (the plugin hung)

    pytest tests/test_stress.py
"""
import asyncio
import json
import random

import aiohttp
import pytest

import bridge
from test_bridge import make_client, run


class Document:
    """The shared state of one Figma document: what every tab on it edits."""

    def __init__(self):
        self.counter = 0
        self.running = 0
        self.max_running = 0   # most execs ever in flight at once in this document


class SandboxPlugin:
    """A plugin connection whose execs interleave like the real sandbox's."""

    def __init__(self, client, name, doc, document=None):
        self.client, self.name, self.doc = client, name, doc
        # Tabs on one document share it, exactly like Figma: pass the same
        # Document to several plugins to open "the same file" in several tabs.
        self.d = document or Document()
        self.ran = []          # agent tags, in the order their scripts started

    counter = property(lambda self: self.d.counter)
    max_running = property(lambda self: self.d.max_running)
    running = property(lambda self: self.d.running)

    async def __aenter__(self):
        self.ws = await self.client.ws_connect(
            "/plugin", headers={"Origin": "null"}, max_msg_size=32 * 1024 * 1024)
        await self.ws.send_str(json.dumps({"type": "hello", "name": self.name, "docSig": self.doc}))
        self._pump_task = asyncio.create_task(self._pump())
        await asyncio.sleep(0.05)
        return self

    async def __aexit__(self, *exc):
        self._pump_task.cancel()
        if not self.ws.closed:
            await self.ws.close()

    async def _pump(self):
        async for msg in self.ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                continue
            m = json.loads(msg.data)
            if m.get("type") == "ping":
                await self.ws.send_str(json.dumps({"type": "pong"}))
            elif m.get("type") == "exec":
                asyncio.create_task(self._run(m["id"], json.loads(m["code"])))

    async def _run(self, rid, cmd):
        d = self.d
        d.running += 1
        d.max_running = max(d.max_running, d.running)
        self.ran.append(cmd.get("tag"))
        try:
            op = cmd["op"]
            if op == "never":
                await asyncio.Event().wait()
            if op == "rmw":
                seen = d.counter
                await asyncio.sleep(cmd.get("pause", 0.01))
                d.counter = seen + 1
                out = {"type": "result", "value": d.counter}
            elif op == "sleep":
                await asyncio.sleep(cmd["s"])
                out = {"type": "result", "value": "slept"}
            elif op == "echo":
                out = {"type": "result", "value": "x" * cmd["n"]}
            elif op == "error":
                out = {"type": "error", "text": "boom"}
            await self.ws.send_str(json.dumps({"id": rid, **out}))
        finally:
            d.running -= 1


def call(c, cmd, **body):
    body.setdefault("timeout", 10)
    return c.post("/exec", json={"code": json.dumps(cmd), **body})


async def assert_file_is_free(c, target):
    """No leaked lock, no ghost entries: the next exec goes straight through."""
    r = await call(c, {"op": "rmw", "pause": 0}, target=target, queue_timeout=0.5)
    assert r.status == 200, await r.text()
    assert (await r.json())["queued_ms"] < 200
    files = (await (await c.get("/status")).json())["files"]
    for f in files:
        assert f["queue"] == {"running": None, "waiting": []}, f
    assert bridge.PENDING == {}


# ─── correctness under load ───────────────────────────────────────────────

def test_fifty_writers_lose_no_updates():
    """The classic lost-update race: 50 agents each read the counter, pause,
    and write it + 1. Serialized, the counter ends at exactly 50."""
    async def go():
        c = await make_client()
        async with SandboxPlugin(c, "A", "docAAAAAA") as p:
            rs = await asyncio.gather(*(
                call(c, {"op": "rmw", "pause": 0.005, "tag": i}, agent=f"agent{i}")
                for i in range(50)))
            assert all(r.status == 200 for r in rs)
            assert p.counter == 50, f"lost {50 - p.counter} updates"
            assert p.max_running == 1
        await c.close()
    run(go())


def test_the_race_is_real_without_the_queue():
    """Control for the test above: the same load with parallel=true does lose
    updates — proof that the fake sandbox interleaves like the real one."""
    async def go():
        c = await make_client()
        async with SandboxPlugin(c, "A", "docAAAAAA") as p:
            await asyncio.gather(*(
                call(c, {"op": "rmw", "pause": 0.01}, parallel=True) for _ in range(20)))
            assert p.counter < 20
            assert p.max_running > 1
        await c.close()
    run(go())


def test_many_agents_many_files():
    """4 files x 15 agents at once: every file serialized on its own, files
    progressing in parallel (total time ~ one file's queue, not all four)."""
    async def go():
        c = await make_client()
        plugins = [SandboxPlugin(c, f"File{i}", f"docfile{i}x") for i in range(4)]
        for p in plugins:
            await p.__aenter__()
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        jobs = [call(c, {"op": "rmw", "pause": 0.02}, target=f"File{i}", agent=f"a{i}-{j}")
                for j in range(15) for i in range(4)]
        random.shuffle(jobs)
        rs = await asyncio.gather(*jobs)
        elapsed = loop.time() - t0
        assert all(r.status == 200 for r in rs)
        assert [p.counter for p in plugins] == [15] * 4
        assert all(p.max_running == 1 for p in plugins)
        assert elapsed < 15 * 0.02 * 2.5, f"files waited on each other ({elapsed:.2f}s)"
        for p in plugins:
            await p.__aexit__()
        await c.close()
    run(go())


def test_queue_is_first_come_first_served():
    async def go():
        c = await make_client()
        async with SandboxPlugin(c, "A", "docAAAAAA") as p:
            jobs = []
            for i in range(10):
                jobs.append(asyncio.create_task(call(c, {"op": "sleep", "s": 0.03, "tag": i})))
                await asyncio.sleep(0.01)
            await asyncio.gather(*jobs)
            assert p.ran == list(range(10))
        await c.close()
    run(go())


# ─── chaos: nothing may leak ──────────────────────────────────────────────

def test_chaos_mix_leaves_no_leaked_lock():
    """Writers, impatient callers (503), failing scripts and callers that hang
    up mid-queue, all at once. Afterwards the file must be immediately usable,
    the counter must equal the writes that reported success, and no script of a
    caller who gave up may have run."""
    async def go():
        c = await make_client()
        rng = random.Random(7)
        async with SandboxPlugin(c, "A", "docAAAAAA") as p:
            tasks, kinds = [], []
            for i in range(60):
                kind = rng.choice(["write", "write", "impatient", "error", "hangup"])
                kinds.append(kind)
                if kind == "write":
                    coro = call(c, {"op": "rmw", "pause": 0.01, "tag": f"w{i}"})
                elif kind == "impatient":
                    coro = call(c, {"op": "rmw", "pause": 0.01, "tag": f"i{i}"}, queue_timeout=0.02)
                elif kind == "error":
                    coro = call(c, {"op": "error", "tag": f"e{i}"})
                else:
                    coro = call(c, {"op": "rmw", "pause": 0.01, "tag": f"h{i}"})
                task = asyncio.create_task(coro)
                if kind == "hangup":
                    asyncio.get_running_loop().call_later(rng.uniform(0.01, 0.1), task.cancel)
                tasks.append(task)
            results = await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.sleep(0.3)

            ok_writes = 0
            for kind, res in zip(kinds, results):
                if isinstance(res, BaseException):
                    continue
                if kind in ("write", "impatient", "hangup") and res.status == 200:
                    ok_writes += 1
                if kind == "impatient" and res.status == 503:
                    assert "file busy" in (await res.json())["error"]
            impatient_ran = [t for t in p.ran if t and t.startswith("i")]
            busy = sum(1 for k, r in zip(kinds, results)
                       if k == "impatient" and not isinstance(r, BaseException) and r.status == 503)
            assert len(impatient_ran) == kinds.count("impatient") - busy, \
                "a caller that got 'file busy' had its script run anyway"
            assert p.counter >= ok_writes
            assert p.max_running == 1
            await assert_file_is_free(c, "A")
        await c.close()
    run(go())


def test_plugin_dies_mid_queue():
    """The Figma tab closes while one script runs and five wait: the running one
    fails fast, the waiters fail fast (no hang), and a re-run plugin works."""
    async def go():
        c = await make_client()
        p = SandboxPlugin(c, "A", "docAAAAAA")
        await p.__aenter__()
        jobs = [asyncio.create_task(call(c, {"op": "sleep", "s": 0.3}, timeout=5))
                for _ in range(6)]
        await asyncio.sleep(0.1)
        await p.__aexit__()
        rs = await asyncio.wait_for(asyncio.gather(*jobs), timeout=8)
        assert all(r.status != 200 for r in rs[1:])

        async with SandboxPlugin(c, "A", "docAAAAAA"):
            await assert_file_is_free(c, "A")
        await c.close()
    run(go())


def test_hung_plugin_times_out_then_recovers():
    async def go():
        c = await make_client()
        async with SandboxPlugin(c, "A", "docAAAAAA") as p:
            r = await call(c, {"op": "never"}, timeout=0.2)
            assert r.status == 504
            r = await call(c, {"op": "rmw"}, timeout=1)
            assert r.status == 409  # interlocked: the hung script may still write
            r = await c.post("/clear", json={"target": "A"})
            assert r.status == 200
            await assert_file_is_free(c, "A")
            assert p.counter == 1
        await c.close()
    run(go())


def test_reconnect_storm_keeps_one_queue():
    """One document open in three tabs, a tab closing and another opening while
    30 writers hammer it: whichever tab each write is routed to, the document
    never runs two scripts at once and loses no update."""
    async def go():
        c = await make_client()
        doc = Document()
        tabs = []
        for _ in range(3):
            t = SandboxPlugin(c, "A", "docAAAAAA", doc)
            await t.__aenter__()
            tabs.append(t)
        conns = [f["conn"] for f in (await (await c.get("/targets")).json())["files"]]
        writers = [asyncio.create_task(call(c, {"op": "rmw", "pause": 0.01},
                                            target=conns[i % 3] if i % 2 else "docAAAAAA"))
                   for i in range(30)]
        await asyncio.sleep(0.1)
        await tabs[0].__aexit__()                         # a tab closes mid-run
        extra = SandboxPlugin(c, "A", "docAAAAAA", doc)   # a new one opens
        await extra.__aenter__()
        rs = await asyncio.gather(*writers)
        ok = sum(1 for r in rs if r.status == 200)
        assert doc.max_running == 1, "two scripts ran in one document at once"
        assert doc.counter >= ok
        await assert_file_is_free(c, "docAAAAAA")
        for t in tabs[1:] + [extra]:
            await t.__aexit__()
        await c.close()
    run(go())


# ─── hostile and odd input ────────────────────────────────────────────────

@pytest.mark.parametrize("raw, ctype", [
    ("not json", "application/json"),
    ("[1, 2, 3]", "application/json"),
    ('{"code": 42}', "application/json"),
    ('{"code": ""}', "application/json"),
    ('{"code": "   "}', "application/json"),
    ('{"code": null}', "application/json"),
    ("", "application/json"),
])
def test_malformed_requests_are_400(raw, ctype):
    async def go():
        c = await make_client()
        async with SandboxPlugin(c, "A", "docAAAAAA"):
            r = await c.post("/exec", data=raw, headers={"Content-Type": ctype})
            assert r.status == 400, (raw, r.status, await r.text())
            await assert_file_is_free(c, "A")
        await c.close()
    run(go())


@pytest.mark.parametrize("extra", [
    {"timeout": "soon"},
    {"timeout": -5},
    {"timeout": 1e12},
    {"queue_timeout": "never"},
    {"queue_timeout": -1},
    {"target": 12345},
    {"target": ["A"]},
    {"agent": "x" * 100000},
    {"agent": {"name": "evil"}},
    {"parallel": "yes"},
])
def test_odd_field_types_never_crash_the_bridge(extra):
    """Whatever an agent sends, the bridge answers with a status (2xx/4xx) and
    the file stays usable — never a 500 from the bridge itself, never a hang."""
    async def go():
        c = await make_client()
        async with SandboxPlugin(c, "A", "docAAAAAA"):
            body = {"code": json.dumps({"op": "rmw", "pause": 0}), "target": "A", **extra}
            r = await asyncio.wait_for(c.post("/exec", json=body), timeout=5)
            assert r.status in (200, 400, 409), (extra, r.status, await r.text())
            await assert_file_is_free(c, "A")
        await c.close()
    run(go())


@pytest.mark.parametrize("raw", ["[1]", '"text"', "null", '{"target": 5}', "not json"])
def test_clear_survives_odd_bodies(raw):
    async def go():
        c = await make_client()
        async with SandboxPlugin(c, "A", "docAAAAAA"):
            r = await c.post("/clear", data=raw, headers={"Content-Type": "application/json"})
            assert r.status in (200, 400), (raw, r.status, await r.text())
        await c.close()
    run(go())


def test_big_script_and_big_result():
    async def go():
        c = await make_client()
        async with SandboxPlugin(c, "A", "docAAAAAA"):
            cmd = {"op": "echo", "n": 5_000_000, "padding": "y" * 1_000_000}
            r = await call(c, cmd, timeout=20)
            assert r.status == 200
            assert len((await r.json())["value"]) == 5_000_000
        await c.close()
    run(go())


def test_unicode_names_and_agents():
    async def go():
        c = await make_client()
        async with SandboxPlugin(c, "Макет 🎨 — фінал", "docUNICODE1"):
            r = await call(c, {"op": "rmw"}, target="фінал", agent="дизайнер 👩‍🎨")
            assert r.status == 200
            files = (await (await c.get("/targets")).json())["files"]
            assert files[0]["name"] == "Макет 🎨 — фінал"
        await c.close()
    run(go())
