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


class GatedPlugin(FakePlugin):
    """Holds every exec until the test lets it go (release), without blocking its socket.
    A script "runs" as long as the test wants, so nothing here waits on the clock."""

    def __init__(self, client):
        super().__init__(client)
        self.held = []         # (id, code) of the execs that have no answer yet

    async def _pump(self):
        async for msg in self.ws:
            m = json.loads(msg.data)
            self.received.append(m)
            if m.get("type") == "exec":
                self.seen_codes.append(m["code"])
                self.held.append((m["id"], m["code"]))

    async def release(self):
        rid, code = self.held.pop(0)
        await self.ws.send_str(json.dumps({"type": "result", "id": rid, "text": code}))


async def until(cond, timeout=5.0, what="condition"):
    end = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < end:
        if cond():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"timed out waiting for {what}")


async def settle():
    """Give the bridge a moment to send what it would send (it must not)."""
    await asyncio.sleep(0.05)


def post(c, **body):
    body.setdefault("timeout", 5)
    return c.post("/exec", json=body)


def test_writers_on_one_file_run_one_after_another():
    async def go():
        c = await make_client()
        async with GatedPlugin(c) as p:
            await hello(p, "A", "doc-a")
            jobs = [asyncio.create_task(post(c, code=f"w{i}", agent=f"agent{i}")) for i in range(3)]
            for n in (1, 2, 3):
                await until(lambda: len(p.seen_codes) == n, what=f"exec {n}")
                await settle()
                assert len(p.seen_codes) == n, "writes overlapped instead of queueing"
                await p.release()
            rs = await asyncio.gather(*jobs)
            assert [r.status for r in rs] == [200, 200, 200]
            waited = sorted([(await r.json())["queued_ms"] for r in rs])
            assert waited[0] < 100 and waited[-1] > waited[0]
        await c.close()
    run(go())


def test_different_files_do_not_wait_for_each_other():
    async def go():
        c = await make_client()
        async with GatedPlugin(c) as a, GatedPlugin(c) as b:
            await hello(a, "A", "doc-a")
            await hello(b, "B", "doc-b")
            jobs = [asyncio.create_task(post(c, code="x", target="A")),
                    asyncio.create_task(post(c, code="y", target="B"))]
            # Both scripts are sent while neither has an answer yet.
            await until(lambda: a.seen_codes == ["x"] and b.seen_codes == ["y"], what="both sent")
            await a.release()
            await b.release()
            rs = await asyncio.gather(*jobs)
            assert [r.status for r in rs] == [200, 200]
        await c.close()
    run(go())


def test_busy_file_gives_up_without_running():
    """A caller that won't wait longer than queue_timeout gets 503 'file busy',
    and its script is never sent — so retrying it is safe."""
    async def go():
        c = await make_client()
        async with GatedPlugin(c) as p:
            await hello(p, "A", "doc-a")
            first = asyncio.create_task(post(c, code="long", agent="designer"))
            await until(lambda: p.seen_codes == ["long"], what="the first script runs")
            r = await post(c, code="impatient", agent="copywriter", queue_timeout=0.05)
            body = await r.json()
            assert r.status == 503 and "file busy" in body["error"]
            assert body["busy"]["running"] == "designer"
            await p.release()
            assert (await first).status == 200
            assert p.seen_codes == ["long"], "the impatient script must not run"
        await c.close()
    run(go())


def test_two_tabs_on_one_document_share_one_queue():
    """Same document in two tabs = two connections, one document: writes routed
    to either tab must still queue behind each other."""
    async def go():
        c = await make_client()
        async with GatedPlugin(c) as tab1, GatedPlugin(c) as tab2:
            await hello(tab1, "A", "doc-a")
            await hello(tab2, "A", "doc-a")
            conns = [f["conn"] for f in (await (await c.get("/targets")).json())["files"]]
            jobs = [asyncio.create_task(post(c, code="1", target=conns[0])),
                    asyncio.create_task(post(c, code="2", target=conns[1]))]
            seen = lambda: len(tab1.seen_codes) + len(tab2.seen_codes)  # noqa: E731
            for n in (1, 2):
                await until(lambda: seen() == n, what=f"exec {n}")
                await settle()
                assert seen() == n, "the two tabs ran scripts at once"
                await (tab1 if tab1.held else tab2).release()
            rs = await asyncio.gather(*jobs)
            assert [r.status for r in rs] == [200, 200]
        await c.close()
    run(go())


def test_same_named_files_are_reachable_by_conn_id():
    """Two different files both called 'Untitled': the name is ambiguous (409
    that suggests an id), and either the document id or the conn id picks one."""
    async def go():
        c = await make_client()
        async with FakePlugin(c) as a, FakePlugin(c) as b:
            await hello(a, "Untitled", "dk2m9first")
            await hello(b, "Untitled", "dk2m9second")
            r = await post(c, code="x", target="Untitled")
            assert r.status == 409 and "-T " in (await r.json())["error"]

            conns = {f["conn"] for f in (await (await c.get("/targets")).json())["files"]}
            for conn in conns:
                r = await post(c, code=f"to {conn}", target=conn)
                assert r.status == 200
            assert len(a.seen_codes) == 1 and len(b.seen_codes) == 1

            # The document id survives reconnects, so it is the one to keep.
            r = await post(c, code="by doc", target="dk2m9s")
            assert r.status == 200 and b.seen_codes[-1] == "by doc"
        await c.close()
    run(go())


def test_status_shows_who_is_running_and_waiting():
    async def go():
        c = await make_client()
        async with GatedPlugin(c) as p:
            await hello(p, "A", "doc-a")
            jobs = [asyncio.create_task(post(c, code="1", agent="designer"))]
            await until(lambda: p.seen_codes == ["1"], what="the designer takes the file")
            jobs.append(asyncio.create_task(post(c, code="2", agent="copywriter")))

            async def queue():
                return (await (await c.get("/status")).json())["files"][0]["queue"]
            for _ in range(100):
                q = await queue()
                if q["waiting"]:
                    break
                await asyncio.sleep(0.01)
            assert q["running"]["agent"] == "designer"
            assert q["waiting"] == ["copywriter"]
            await p.release()
            await until(lambda: len(p.seen_codes) == 2, what="the copywriter runs")
            await p.release()
            await asyncio.gather(*jobs)
            assert await queue() == {"running": None, "waiting": []}
        await c.close()
    run(go())
