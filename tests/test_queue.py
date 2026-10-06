"""The per-document queue: several agents, several Figma files, no conflicts.

    pytest tests/test_queue.py
"""
import asyncio
import json

import bridge
from test_bridge import FakePlugin, make_client, run


async def hello(plugin, name, sig):
    await plugin.ws.send_str(json.dumps({"type": "hello", "name": name, "docSig": sig}))
    await asyncio.sleep(0.05)


class SlowPlugin(FakePlugin):
    """Answers every exec after `delay` seconds, without blocking its socket."""

    def __init__(self, client, delay):
        super().__init__(client)
        self.delay = delay

    async def _pump(self):
        async for msg in self.ws:
            m = json.loads(msg.data)
            self.received.append(m)
            if m.get("type") == "exec":
                self.seen_codes.append(m["code"])
                asyncio.create_task(self._answer(m["id"], m["code"]))

    async def _answer(self, rid, code):
        await asyncio.sleep(self.delay)
        await self.ws.send_str(json.dumps({"type": "result", "id": rid, "text": code}))


def post(c, **body):
    body.setdefault("timeout", 5)
    return c.post("/exec", json=body)


def test_writers_on_one_file_run_one_after_another():
    async def go():
        c = await make_client()
        async with SlowPlugin(c, 0.3) as p:
            await hello(p, "A", "doc-a")
            t0 = asyncio.get_running_loop().time()
            rs = await asyncio.gather(*(post(c, code=f"w{i}", agent=f"agent{i}") for i in range(3)))
            elapsed = asyncio.get_running_loop().time() - t0
            assert [r.status for r in rs] == [200, 200, 200]
            assert elapsed >= 0.85, "writes overlapped instead of queueing"
            waited = sorted([(await r.json())["queued_ms"] for r in rs])
            assert waited[0] < 100 and waited[-1] >= 500
        await c.close()
    run(go())


def test_different_files_do_not_wait_for_each_other():
    async def go():
        c = await make_client()
        async with SlowPlugin(c, 0.4) as a, SlowPlugin(c, 0.4) as b:
            await hello(a, "A", "doc-a")
            await hello(b, "B", "doc-b")
            t0 = asyncio.get_running_loop().time()
            rs = await asyncio.gather(post(c, code="x", target="A"), post(c, code="y", target="B"))
            assert [r.status for r in rs] == [200, 200]
            assert asyncio.get_running_loop().time() - t0 < 0.7
            assert a.seen_codes == ["x"] and b.seen_codes == ["y"]
        await c.close()
    run(go())


def test_busy_file_gives_up_without_running():
    """A caller that won't wait longer than queue_timeout gets 503 'file busy',
    and its script is never sent — so retrying it is safe."""
    async def go():
        c = await make_client()
        async with SlowPlugin(c, 0.8) as p:
            await hello(p, "A", "doc-a")
            first = asyncio.create_task(post(c, code="long", agent="designer"))
            await asyncio.sleep(0.1)
            r = await post(c, code="impatient", agent="copywriter", queue_timeout=0.2)
            body = await r.json()
            assert r.status == 503 and "file busy" in body["error"]
            assert body["busy"]["running"] == "designer"
            assert (await first).status == 200
            assert p.seen_codes == ["long"], "the impatient script must not run"
        await c.close()
    run(go())


def test_two_tabs_on_one_document_share_one_queue():
    """Same document in two tabs = two connections, one document: writes routed
    to either tab must still queue behind each other."""
    async def go():
        c = await make_client()
        async with SlowPlugin(c, 0.3) as tab1, SlowPlugin(c, 0.3) as tab2:
            await hello(tab1, "A", "doc-a")
            await hello(tab2, "A", "doc-a")
            conns = [f["conn"] for f in (await (await c.get("/targets")).json())["files"]]
            t0 = asyncio.get_running_loop().time()
            rs = await asyncio.gather(post(c, code="1", target=conns[0]),
                                      post(c, code="2", target=conns[1]))
            assert [r.status for r in rs] == [200, 200]
            assert asyncio.get_running_loop().time() - t0 >= 0.55
        await c.close()
    run(go())


def test_same_named_files_are_reachable_by_conn_id():
    """Two different files both called 'Untitled': the name is ambiguous (409
    that suggests a conn id), the conn id picks exactly one."""
    async def go():
        c = await make_client()
        async with FakePlugin(c) as a, FakePlugin(c) as b:
            await hello(a, "Untitled", "doc-1")
            await hello(b, "Untitled", "doc-2")
            r = await post(c, code="x", target="Untitled")
            assert r.status == 409 and "-T " in (await r.json())["error"]

            conns = {f["conn"] for f in (await (await c.get("/targets")).json())["files"]}
            for conn in conns:
                r = await post(c, code=f"to {conn}", target=conn)
                assert r.status == 200
            assert len(a.seen_codes) == 1 and len(b.seen_codes) == 1
        await c.close()
    run(go())


def test_status_shows_who_is_running_and_waiting():
    async def go():
        c = await make_client()
        async with SlowPlugin(c, 0.5) as p:
            await hello(p, "A", "doc-a")
            jobs = [asyncio.create_task(post(c, code="1", agent="designer"))]
            await asyncio.sleep(0.1)  # let the designer take the file first
            jobs.append(asyncio.create_task(post(c, code="2", agent="copywriter")))
            await asyncio.sleep(0.1)
            q = (await (await c.get("/status")).json())["files"][0]["queue"]
            assert q["running"]["agent"] == "designer"
            assert q["waiting"] == ["copywriter"]
            await asyncio.gather(*jobs)
            q = (await (await c.get("/status")).json())["files"][0]["queue"]
            assert q == {"running": None, "waiting": []}
        await c.close()
    run(go())
