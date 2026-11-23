"""Chat tests: a Claude Code session per Figma file, driven from the plugin.

Written before the code (TDD). The contract they pin down is in docs/CHAT.md.
Nothing here starts the real Claude Code: chat.CLAUDE_CMD points at
tests/fake_claude.py, which speaks the same stream-json protocol.

    pytest -q tests/test_chat.py
"""

import asyncio
import base64
import contextlib
import json
import os
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import aiohttp
import pytest
from aiohttp.test_utils import TestClient, TestServer

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import bridge  # noqa: E402
import chat    # noqa: E402

FAKE = Path(__file__).resolve().parent / "fake_claude.py"
PLUGIN = {"Origin": "null"}            # what the plugin UI iframe sends
EVIL = {"Origin": "https://evil.example"}
PNG = base64.b64encode(
    bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010806000000"
                  "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082")
).decode()


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def chat_env(tmp_path, monkeypatch):
    """Fresh chat state, data in a temp dir, the fake claude instead of the real one."""
    chat.reset()
    chat.DATA_DIR = tmp_path / "chat-data"
    chat.WORKDIR = tmp_path / "work"
    chat.WORKDIR.mkdir()
    chat.CLAUDE_CMD = [sys.executable, str(FAKE)]
    chat.STOP_GRACE = 1.0
    chat.HISTORY_LIMIT = 300
    monkeypatch.setattr(chat, "CODEX_CMD", ["definitely-not-codex-xyz"], raising=False)  # claude only
    monkeypatch.setattr(chat, "BREW_DIRS", ())      # never the real /opt/homebrew or /usr/local
    for k in ("CODEX_THREAD_ID", "CODEX_SESSION_ID", "CODEX_CI", "CODEX_HOME"):
        monkeypatch.delenv(k, raising=False)       # the default provider must not follow our shell
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(tmp_path / "claude.jsonl"))
    yield tmp_path
    chat.reset()


def claude_log(tmp_path):
    f = tmp_path / "claude.jsonl"
    if not f.exists():
        return []
    return [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines() if x.strip()]


def starts(tmp_path):
    return [r for r in claude_log(tmp_path) if r.get("start")]


def stdin_msgs(tmp_path):
    return [r["stdin"] for r in claude_log(tmp_path) if "stdin" in r]


@contextlib.asynccontextmanager
async def bridge_client():
    """The real bridge app on a test server. Closing it must stop every session."""
    c = TestClient(TestServer(bridge.build_app()))
    await c.start_server()
    try:
        yield c
    finally:
        await c.close()


class Tab:
    """A plugin window on one Figma file: says hello, records chat events."""

    def __init__(self, client, doc, name="Banner"):
        self.client, self.doc, self.name = client, doc, name
        self.events = []
        self.ws = None
        self._task = None

    async def __aenter__(self):
        self.ws = await self.client.ws_connect("/plugin", headers=PLUGIN)
        await self.ws.send_str(json.dumps({"type": "hello", "version": "test", "fileKey": None,
                                           "name": self.name, "docSig": self.doc}))
        self._task = asyncio.create_task(self._pump())
        await asyncio.sleep(0.1)
        return self

    async def __aexit__(self, *exc):
        self._task.cancel()
        if not self.ws.closed:
            await self.ws.close()

    async def _pump(self):
        async for msg in self.ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                continue
            m = json.loads(msg.data)
            if m.get("type") == "ping":
                await self.ws.send_str(json.dumps({"type": "pong"}))
            elif m.get("type") == "chat":
                assert m["doc"] == self.doc
                self.events.append(m["event"])

    def kinds(self):
        return [e["kind"] for e in self.events]


async def until(cond, timeout=8.0, what="condition"):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = cond() if not asyncio.iscoroutinefunction(cond) else await cond()
        if v:
            return v
        await asyncio.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def release(tmp_path):
    """Let a fake's `hold <text>` turn go on (the fakes poll for this file)."""
    (tmp_path / "release").touch()


async def send(c, doc, text="", **extra):
    r = await c.post("/chat/send", json={"doc": doc, "text": text, **extra}, headers=PLUGIN)
    return r.status, await r.json()


async def history(c, doc):
    r = await c.get("/chat", params={"doc": doc}, headers=PLUGIN)
    assert r.status == 200
    return await r.json()


async def idle_with(c, doc, n_claude):
    """Wait until the session is idle and has at least n_claude Claude messages."""
    async def ok():
        h = await history(c, doc)
        mine = [m for m in h["messages"] if m["from"] == "claude"]
        return h if (not h["busy"] and len(mine) >= n_claude) else None
    return await until(ok, what=f"{n_claude} claude message(s) and idle")


# ─── guard and validation ─────────────────────────────────────────────────

def test_chat_routes_refuse_web_pages():
    async def go():
        async with bridge_client() as c:
            assert (await c.get("/chat", params={"doc": "dA"}, headers=EVIL)).status == 403
            assert (await c.post("/chat/send", json={"doc": "dA", "text": "hi"}, headers=EVIL)).status == 403
            assert (await c.post("/chat/stop", json={"doc": "dA"}, headers=EVIL)).status == 403
    run(go())


def test_chat_routes_accept_the_plugin_and_local_clients():
    async def go():
        async with bridge_client() as c:
            assert (await c.get("/chat", params={"doc": "dA"}, headers=PLUGIN)).status == 200
            assert (await c.get("/chat", params={"doc": "dA"})).status == 200
    run(go())


def test_unknown_doc_has_empty_idle_chat():
    async def go():
        async with bridge_client() as c:
            h = await history(c, "dNew")
            assert h["messages"] == [] and h["busy"] is False and h["activity"] == []
    run(go())


@pytest.mark.parametrize("doc", [None, "", "../etc", "a/b", "x" * 200, "d d"])
def test_bad_doc_is_rejected(doc):
    async def go():
        async with bridge_client() as c:
            body = {"text": "hi"} if doc is None else {"doc": doc, "text": "hi"}
            r = await c.post("/chat/send", json=body, headers=PLUGIN)
            assert r.status == 400
            r = await c.get("/chat", params={} if doc is None else {"doc": doc}, headers=PLUGIN)
            assert r.status == 400
    run(go())


def test_empty_message_is_rejected():
    async def go():
        async with bridge_client() as c:
            status, body = await send(c, "dA", "   ")
            assert status == 400 and body["ok"] is False
            assert starts(chat.DATA_DIR.parent) == []   # no claude started for nothing
    run(go())


# ─── one session per file ─────────────────────────────────────────────────

def test_send_stores_my_message_and_claude_reply(chat_env):
    async def go():
        async with bridge_client() as c:
            status, body = await send(c, "dA", "say Привіт, дизайнере")
            assert status == 200 and body["ok"] is True and isinstance(body["id"], int)
            h = await idle_with(c, "dA", 1)
            me, claude = h["messages"][0], h["messages"][-1]
            assert me["from"] == "me" and me["text"] == "say Привіт, дизайнере" and me["id"] == body["id"]
            assert claude["from"] == "claude" and claude["text"] == "Привіт, дизайнере"
            assert claude["id"] > me["id"]
            assert all(isinstance(m["t"], int) for m in h["messages"])
    run(go())


def test_claude_runs_headless_with_bypass_and_streaming(chat_env):
    async def go():
        async with bridge_client() as c:
            await send(c, "dA", "say ok")
            await idle_with(c, "dA", 1)
        argv = starts(chat_env)[0]["argv"]
        assert "-p" in argv
        assert argv[argv.index("--input-format") + 1] == "stream-json"
        assert argv[argv.index("--output-format") + 1] == "stream-json"
        assert "--verbose" in argv
        assert "--include-partial-messages" in argv
        assert "--dangerously-skip-permissions" in argv
        assert "--resume" not in argv          # first ever message: a new session
    run(go())


def test_claude_always_runs_opus_with_medium_effort(chat_env):
    async def go():
        async with bridge_client() as c:
            await send(c, "dA", "say before")
            await idle_with(c, "dA", 1)
        chat.reset()   # restart: the next process resumes the session
        async with bridge_client() as c:
            await send(c, "dA", "say after")
            await idle_with(c, "dA", 2)
        new, resumed = starts(chat_env)
        assert "--resume" in resumed["argv"]
        for argv in (new["argv"], resumed["argv"]):
            assert argv[argv.index("--model") + 1] == "opus"
            assert argv[argv.index("--effort") + 1] == "medium"
    run(go())


def test_session_is_pointed_at_its_own_figma_file(chat_env):
    async def go():
        async with bridge_client() as c:
            async with Tab(c, "dBanner", name="Banner"):
                await send(c, "dBanner", "say ok")
                await idle_with(c, "dBanner", 1)
        s = starts(chat_env)[0]
        assert s["env"]["FIGMOSHA_TARGET"] == "dBanner"   # figmosha.py calls go to this file
        assert s["env"]["FIGMOSHA_AGENT"] == "chat"        # shown in the file's queue
        assert s["env"]["FIGMOSHA_FILE"] == "Banner"       # the file's name, for Claude to read
        assert Path(s["cwd"]).resolve() == chat.WORKDIR.resolve()
    run(go())


def test_one_process_per_file_stays_alive_between_messages(chat_env):
    async def go():
        async with bridge_client() as c:
            await send(c, "dA", "say one")
            await idle_with(c, "dA", 1)
            await send(c, "dA", "say two")
            await idle_with(c, "dA", 2)
            assert len(starts(chat_env)) == 1
    run(go())


def test_two_files_get_two_separate_sessions(chat_env):
    async def go():
        async with bridge_client() as c:
            await send(c, "dA", "say a")
            await send(c, "dB", "say b")
            ha = await idle_with(c, "dA", 1)
            hb = await idle_with(c, "dB", 1)
            assert [m["text"] for m in ha["messages"]] == ["say a", "a"]
            assert [m["text"] for m in hb["messages"]] == ["say b", "b"]
        s = starts(chat_env)
        assert len(s) == 2
        assert {x["env"]["FIGMOSHA_TARGET"] for x in s} == {"dA", "dB"}
        assert s[0]["session"] != s[1]["session"]
    run(go())


def test_history_and_session_survive_a_bridge_restart(chat_env):
    async def go():
        async with bridge_client() as c:
            await send(c, "dA", "say before")
            await idle_with(c, "dA", 1)
        first = starts(chat_env)[0]["session"]
        chat.reset()   # the bridge process restarts: memory gone, DATA_DIR stays
        async with bridge_client() as c:
            h = await history(c, "dA")
            assert [m["text"] for m in h["messages"]] == ["say before", "before"]
            assert h["session"] == first
            await send(c, "dA", "say after")
            h = await idle_with(c, "dA", 2)
            assert [m["text"] for m in h["messages"]][-1] == "after"
            assert h["messages"][-1]["id"] > h["messages"][1]["id"]   # ids keep counting
        argv = starts(chat_env)[1]["argv"]
        assert argv[argv.index("--resume") + 1] == first
    run(go())


def test_closing_the_bridge_stops_every_claude_process():
    async def go():
        async with bridge_client() as c:
            await send(c, "dA", "say a")
            await send(c, "dB", "say b")
            await idle_with(c, "dA", 1)
            await idle_with(c, "dB", 1)
            procs = [s.proc for s in chat.SESSIONS.values()]
            assert len(procs) == 2
        assert all(p.returncode is not None for p in procs)
    run(go())


def test_history_is_capped():
    async def go():
        chat.HISTORY_LIMIT = 5
        async with bridge_client() as c:
            for i in range(4):
                await send(c, "dA", f"say r{i}")

                async def answered(i=i):
                    h = await history(c, "dA")
                    return h if not h["busy"] and h["messages"][-1]["text"] == f"r{i}" else None
                h = await until(answered, what=f"reply r{i}")
            assert len(h["messages"]) == 5                    # 8 written, oldest dropped
            assert [m["text"] for m in h["messages"]][-2:] == ["say r3", "r3"]
        chat.reset()
        async with bridge_client() as c:
            assert len((await history(c, "dA"))["messages"]) == 5   # the file is capped too
    run(go())


# ─── live events to the plugin ────────────────────────────────────────────

def test_events_go_to_the_tab_of_that_file_only():
    async def go():
        async with bridge_client() as c:
            async with Tab(c, "dA") as a, Tab(c, "dB") as b:
                await send(c, "dA", "say hello")
                await idle_with(c, "dA", 1)
                await until(lambda: a.events and a.events[-1] == {"kind": "busy", "busy": False},
                            what="busy:false on tab A")
                assert a.kinds()[0] == "message" and a.events[0]["message"]["from"] == "me"
                assert {"kind": "busy", "busy": True} in a.events
                claude = [e["message"] for e in a.events
                          if e["kind"] == "message" and e["message"]["from"] == "claude"]
                assert [m["text"] for m in claude] == ["hello"]
                assert b.events == []
    run(go())


def test_same_file_in_two_tabs_both_see_the_chat():
    async def go():
        async with bridge_client() as c:
            async with Tab(c, "dA") as t1, Tab(c, "dA") as t2:
                await send(c, "dA", "say both")
                await idle_with(c, "dA", 1)
                for t in (t1, t2):
                    await until(lambda t=t: {"kind": "busy", "busy": False} in t.events)
                    texts = [e["message"]["text"] for e in t.events if e["kind"] == "message"]
                    assert texts == ["say both", "both"]
    run(go())


def test_text_streams_as_deltas_before_the_final_message():
    async def go():
        async with bridge_client() as c:
            async with Tab(c, "dA") as t:
                await send(c, "dA", "stream один два три")
                await idle_with(c, "dA", 1)
                await until(lambda: {"kind": "busy", "busy": False} in t.events)
                deltas = [e["text"] for e in t.events if e["kind"] == "delta"]
                assert "".join(deltas) == "один два три"
                last_delta = max(i for i, e in enumerate(t.events) if e["kind"] == "delta")
                final = next(i for i, e in enumerate(t.events)
                             if e["kind"] == "message" and e["message"]["from"] == "claude")
                assert last_delta < final
    run(go())


def test_tool_calls_show_up_as_activity_while_busy():
    async def go():
        async with bridge_client() as c:
            async with Tab(c, "dA") as t:
                await send(c, "dA", "tool")
                await until(lambda: {"kind": "activity", "text": "Перевіряю файл"} in t.events,
                            what="activity event")
                h = await history(c, "dA")
                if h["busy"]:
                    assert "Перевіряю файл" in [a["text"] for a in h["activity"]]
                h = await idle_with(c, "dA", 1)
                assert h["activity"] == []          # activity is only kept while busy
                assert h["messages"][-1]["text"] == "done"
    run(go())


@pytest.mark.parametrize("block,line", [
    ({"name": "Bash", "input": {"command": "ls -la", "description": "Дивлюсь файли"}}, "Дивлюсь файли"),
    ({"name": "Bash", "input": {"command": "python figmosha.py tree"}}, "Bash: python figmosha.py tree"),
    ({"name": "Read", "input": {"file_path": "C:\\Users\\nerdf\\figmosha2\\AGENTS.md"}}, "Read AGENTS.md"),
    ({"name": "Edit", "input": {"file_path": "/x/y/plugin/ui.html"}}, "Edit ui.html"),
    ({"name": "WebSearch", "input": {"query": "figma auto layout"}}, "WebSearch"),
])
def test_activity_line_for_a_tool_call(block, line):
    assert chat.activity_line({"type": "tool_use", "id": "t", **block}) == line


def test_activity_line_is_short():
    long = {"type": "tool_use", "id": "t", "name": "Bash", "input": {"command": "x" * 500}}
    assert len(chat.activity_line(long)) <= 120


# ─── replies: buttons and questions ───────────────────────────────────────

def test_parse_reply_takes_out_actions_and_questions():
    text = ("Готово, макет зібрано.\n\n"
            "```actions\nЗроби темну тему\nДодай мобільну версію\n```\n"
            "```questions\nЯкий шрифт?\nСкільки кольорів?\n```")
    clean, actions, questions = chat.parse_reply(text)
    assert clean == "Готово, макет зібрано."
    assert actions == ["Зроби темну тему", "Додай мобільну версію"]
    assert questions == ["Який шрифт?", "Скільки кольорів?"]


def test_parse_reply_without_blocks_keeps_text():
    assert chat.parse_reply("Просто текст\n```js\nreturn 1\n```") == \
        ("Просто текст\n```js\nreturn 1\n```", [], [])


def test_parse_reply_limits():
    many = "x\n```actions\n" + "\n".join(f"a{i}" for i in range(10)) + "\n```"
    _, actions, _ = chat.parse_reply(many)
    assert actions == ["a0", "a1", "a2", "a3"]           # at most 4 buttons


def test_claude_reply_with_buttons_is_stored_with_them():
    async def go():
        async with bridge_client() as c:
            await send(c, "dA", "say Зроблено.\n```actions\nДалі\nСтоп\n```")
            h = await idle_with(c, "dA", 1)
            m = h["messages"][-1]
            assert m["text"] == "Зроблено." and m["actions"] == ["Далі", "Стоп"]
            assert "questions" not in m
    run(go())


def test_answer_to_a_question_is_quoted_and_sent_with_context(chat_env):
    async def go():
        async with bridge_client() as c:
            await send(c, "dA", "say Питаю.\n```questions\nЯкий шрифт?\nЯкий колір?\n```")
            h = await idle_with(c, "dA", 1)
            q = h["messages"][-1]
            assert q["questions"] == ["Який шрифт?", "Який колір?"]
            status, _ = await send(c, "dA", "Синій", answer={"msg": q["id"], "i": 1})
            assert status == 200
            h = await idle_with(c, "dA", 2)
            a = [m for m in h["messages"] if m["from"] == "me"][-1]
            assert a["answer"] == {"msg": q["id"], "i": 1}
            assert a["quote"]["id"] == q["id"] and "Який колір?" in a["quote"]["text"]
        sent = [m for m in stdin_msgs(chat_env) if m["type"] == "user"][-1]
        content = sent["message"]["content"]
        text = content if isinstance(content, str) else " ".join(
            b.get("text", "") for b in content if b["type"] == "text")
        assert "Який колір?" in text and "Синій" in text      # Claude knows what was answered
    run(go())


def test_answer_to_a_missing_question_is_ignored():
    async def go():
        async with bridge_client() as c:
            await send(c, "dA", "say no questions here")
            h = await idle_with(c, "dA", 1)
            await send(c, "dA", "say x", answer={"msg": h["messages"][-1]["id"], "i": 0})
            h = await idle_with(c, "dA", 2)
            assert "answer" not in [m for m in h["messages"] if m["from"] == "me"][-1]
    run(go())


def test_quote_of_an_earlier_message(chat_env):
    async def go():
        async with bridge_client() as c:
            await send(c, "dA", "say Ось варіант із синьою кнопкою")
            h = await idle_with(c, "dA", 1)
            target = h["messages"][-1]
            await send(c, "dA", "say ок", quote=target["id"])
            h = await idle_with(c, "dA", 2)
            mine = [m for m in h["messages"] if m["from"] == "me"][-1]
            assert mine["quote"] == {"id": target["id"], "from": "claude",
                                     "text": "Ось варіант із синьою кнопкою"}
        sent = [m for m in stdin_msgs(chat_env) if m["type"] == "user"][-1]
        assert "Ось варіант із синьою кнопкою" in json.dumps(sent, ensure_ascii=False)
    run(go())


# ─── images ───────────────────────────────────────────────────────────────

def test_pasted_image_is_saved_shown_and_sent_to_claude(chat_env):
    async def go():
        async with bridge_client() as c:
            status, body = await send(c, "dA", "say бачу", images=[f"data:image/png;base64,{PNG}"])
            assert status == 200
            h = await idle_with(c, "dA", 1)
            me = h["messages"][0]
            assert len(me["images"]) == 1
            url = me["images"][0]
            assert url.startswith("/chat/uploads/")
            r = await c.get(url, headers=PLUGIN)
            assert r.status == 200 and await r.read() == base64.b64decode(PNG)
        sent = [m for m in stdin_msgs(chat_env) if m["type"] == "user"][0]
        blocks = sent["message"]["content"]
        img = [b for b in blocks if b["type"] == "image"]
        assert img and img[0]["source"] == {"type": "base64", "media_type": "image/png", "data": PNG}
        assert any(b["type"] == "text" and b["text"] == "say бачу" for b in blocks)
    run(go())


def test_image_only_message_is_allowed():
    async def go():
        async with bridge_client() as c:
            status, _ = await send(c, "dA", "", images=[f"data:image/png;base64,{PNG}"])
            assert status == 200
            h = await idle_with(c, "dA", 1)
            assert h["messages"][0]["images"]
    run(go())


def test_bad_images_are_dropped():
    async def go():
        async with bridge_client() as c:
            status, _ = await send(c, "dA", "say ok", images=[
                "https://example.com/x.png", "data:text/html;base64,PGgxPg==",
                "data:image/png;base64,@@@"])
            assert status == 200
            h = await idle_with(c, "dA", 1)
            assert "images" not in h["messages"][0]
    run(go())


def test_uploads_cannot_escape_their_folder():
    async def go():
        async with bridge_client() as c:
            for p in ("/chat/uploads/..%2f..%2fbridge.py", "/chat/uploads/../bridge.py"):
                r = await c.get(p, headers=PLUGIN)
                assert r.status in (400, 403, 404)
    run(go())


# ─── busy, queue, stop, crash ─────────────────────────────────────────────

def test_messages_sent_while_busy_are_answered_in_order(chat_env):
    async def go():
        async with bridge_client() as c:
            await send(c, "dA", "hold перша")
            await until(lambda: chat.SESSIONS["dA"].sent, what="hold reached claude")
            await send(c, "dA", "say друга")
            release(chat_env)
            h = await idle_with(c, "dA", 2)
            assert [m["text"] for m in h["messages"] if m["from"] == "claude"] == ["перша", "друга"]
            assert len(starts(chat_env)) == 1
    run(go())


def test_stop_interrupts_the_turn_without_a_chat_line(chat_env):
    async def go():
        async with bridge_client() as c:
            async with Tab(c, "dA") as t:
                await send(c, "dA", "slow")
                await until(lambda: any(e["kind"] == "message" and e["message"]["text"] == "starting"
                                        for e in t.events), what="starting")
                r = await c.post("/chat/stop", json={"doc": "dA"}, headers=PLUGIN)
                assert r.status == 200
                h = await idle_with(c, "dA", 1)
                assert [m["text"] for m in h["messages"]] == ["slow", "starting"]   # no line for Stop
                await until(lambda: t.events[-1] == {"kind": "busy", "busy": False})
            assert any(m["type"] == "control_request" and m["request"]["subtype"] == "interrupt"
                       for m in stdin_msgs(chat_env))
            await send(c, "dA", "say still here")
            h = await idle_with(c, "dA", 2)
            assert h["messages"][-1]["text"] == "still here"
            assert len(starts(chat_env)) == 1                # the same process carried on
    run(go())


def test_stop_kills_a_session_that_ignores_the_interrupt(chat_env):
    async def go():
        async with bridge_client() as c:
            await send(c, "dA", "hang")
            await until(lambda: any(m.get("message", {}).get("content") == "hang"
                                    for m in stdin_msgs(chat_env)), what="hang reached claude")
            t0 = time.monotonic()
            await c.post("/chat/stop", json={"doc": "dA"}, headers=PLUGIN)
            await until(lambda: not chat.SESSIONS["dA"].busy, timeout=chat.STOP_GRACE + 4)
            assert time.monotonic() - t0 < chat.STOP_GRACE + 4
            await send(c, "dA", "say back")
            h = await idle_with(c, "dA", 1)
            assert h["messages"][-1]["text"] == "back"
        s = starts(chat_env)
        assert len(s) == 2
        assert s[1]["argv"][s[1]["argv"].index("--resume") + 1] == s[0]["session"]
    run(go())


def test_stop_when_idle_is_harmless():
    async def go():
        async with bridge_client() as c:
            r = await c.post("/chat/stop", json={"doc": "dA"}, headers=PLUGIN)
            assert r.status == 200
            assert (await history(c, "dA"))["messages"] == []
    run(go())


def test_a_crash_is_reported_and_the_next_message_resumes(chat_env):
    async def go():
        async with bridge_client() as c:
            await send(c, "dA", "say first")
            await idle_with(c, "dA", 1)
            await send(c, "dA", "crash")

            async def errored():
                h = await history(c, "dA")
                errs = [m for m in h["messages"] if m["from"] == "system" and m.get("error")]
                return h if errs and not h["busy"] else None
            h = await until(errored, what="error message")
            err = [m for m in h["messages"] if m["from"] == "system"][-1]
            assert "boom" in err["text"]
            await send(c, "dA", "say recovered")
            h = await idle_with(c, "dA", 2)
            assert h["messages"][-1]["text"] == "recovered"
        s = starts(chat_env)
        assert len(s) == 2
        assert s[1]["argv"][s[1]["argv"].index("--resume") + 1] == s[0]["session"]
    run(go())


def test_missing_claude_binary_is_a_clear_error():
    async def go():
        chat.CLAUDE_CMD = ["definitely-not-claude-xyz"]
        async with bridge_client() as c:
            status, _ = await send(c, "dA", "hi")
            assert status == 200

            async def errored():
                h = await history(c, "dA")
                return h if any(m.get("error") for m in h["messages"]) and not h["busy"] else None
            h = await until(errored, what="error message")
            assert "claude" in [m for m in h["messages"] if m.get("error")][-1]["text"].lower()
    run(go())


def test_status_shows_chat_busy_per_file():
    async def go():
        async with bridge_client() as c:
            async with Tab(c, "dA"):
                await send(c, "dA", "slow")
                await until(lambda: chat.SESSIONS.get("dA") and chat.SESSIONS["dA"].busy)
                st = await (await c.get("/status")).json()
                f = next(x for x in st["files"] if x["doc"] == "dA")
                assert f["chat"] == {"busy": True}
                await c.post("/chat/stop", json={"doc": "dA"}, headers=PLUGIN)
    run(go())


# ─── slash commands ───────────────────────────────────────────────────────

def user_texts(tmp_path):
    out = []
    for m in stdin_msgs(tmp_path):
        if m["type"] != "user":
            continue
        c = m["message"]["content"]
        out.append(c if isinstance(c, str) else "\n".join(b.get("text", "") for b in c if b.get("type") == "text"))
    return out


async def idle_with_system(c, doc, n):
    async def ok():
        h = await history(c, doc)
        sys_ = [m for m in h["messages"] if m["from"] == "system"]
        return h if (not h["busy"] and len(sys_) >= n) else None
    return await until(ok, what=f"{n} system message(s) and idle")


def names(cmds):
    return [x["name"] for x in cmds]


def test_commands_have_a_fallback_before_claude_ever_ran():
    async def go():
        async with bridge_client() as c:
            h = await history(c, "dNew")
            assert {"clear", "compact", "context"} <= set(names(h["commands"]))
    run(go())


def test_commands_come_from_claude_init_and_survive_a_restart(chat_env):
    async def go():
        async with bridge_client() as c:
            async with Tab(c, "dA") as t:
                await send(c, "dA", "say hi")
                await idle_with(c, "dA", 1)
                h = await history(c, "dA")
                got = {x["name"]: x for x in h["commands"]}
                assert "review-it" in got                       # a project skill from init
                assert got["review-it"]["description"] == "A project skill"
                assert got["review-it"]["hint"] == "[target]"
                assert got["clear"]["aliases"] == ["reset", "new"]
                assert not any(" " in n for n in got)           # an MCP prompt with spaces cannot be typed
                await until(lambda: any(e["kind"] == "commands" for e in t.events), what="commands event")
        chat.reset()
        async with bridge_client() as c:
            assert "review-it" in names((await history(c, "dB"))["commands"])
    run(go())


def test_chat_load_over_the_plugin_socket_carries_the_commands():
    async def go():
        async with bridge_client() as c:
            async with Tab(c, "dA") as t:
                await t.ws.send_str(json.dumps({"type": "chat-load"}))
                ev = await until(lambda: next((e for e in t.events if e["kind"] == "history"), None),
                                 what="history event")
                assert "clear" in names(ev["commands"])
    run(go())


def test_clear_starts_a_new_session_and_keeps_the_history(chat_env):
    async def go():
        async with bridge_client() as c:
            await send(c, "dA", "say before")
            await idle_with(c, "dA", 1)
            old = chat.SESSIONS["dA"].proc
            status, body = await send(c, "dA", "/clear")
            assert status == 200 and body["ok"]
            h = await idle_with_system(c, "dA", 1)
            assert [m["text"] for m in h["messages"]][:3] == ["say before", "before", "/clear"]
            note = h["messages"][-1]
            assert note["from"] == "system" and not note.get("error")
            assert h["session"] is None
            assert "dA" not in json.loads((chat.DATA_DIR / "sessions.json").read_text(encoding="utf-8"))
            assert old.returncode is not None                    # the old process is gone
            await send(c, "dA", "say after")
            h = await idle_with(c, "dA", 2)
            assert h["messages"][-1]["text"] == "after"
        assert "/clear" not in user_texts(chat_env)              # the bridge handled it
        s = starts(chat_env)
        assert len(s) == 2 and "--resume" not in s[1]["argv"]
        assert s[1]["session"] != s[0]["session"]
    run(go())


@pytest.mark.parametrize("alias", ["/reset", "/new"])
def test_clear_aliases(chat_env, alias):
    async def go():
        async with bridge_client() as c:
            await send(c, "dA", "say x")
            await idle_with(c, "dA", 1)
            await send(c, "dA", alias)
            h = await idle_with_system(c, "dA", 1)
            assert h["session"] is None
    run(go())


def test_clear_sent_while_busy_waits_for_the_turn(chat_env):
    async def go():
        async with bridge_client() as c:
            await send(c, "dA", "hold перша")
            await until(lambda: chat.SESSIONS["dA"].sent, what="hold reached claude")
            await send(c, "dA", "/clear")
            await send(c, "dA", "say друга")
            release(chat_env)
            h = await idle_with(c, "dA", 2)
            seq = [m["from"] for m in h["messages"] if m["from"] != "me"]
            assert seq == ["claude", "system", "claude"]            # in the order they were sent
            assert h["messages"][-1]["text"] == "друга"
        s = starts(chat_env)
        assert len(s) == 2 and "--resume" not in s[1]["argv"]
    run(go())


def test_clear_with_no_session_yet_is_harmless(chat_env):
    async def go():
        async with bridge_client() as c:
            await send(c, "dA", "/clear")
            h = await idle_with_system(c, "dA", 1)
            assert not h["messages"][-1].get("error")
        assert starts(chat_env) == []
    run(go())


def test_compact_goes_to_claude_and_is_reported(chat_env):
    async def go():
        async with bridge_client() as c:
            async with Tab(c, "dA") as t:
                await send(c, "dA", "say a")
                await idle_with(c, "dA", 1)
                await send(c, "dA", "/compact")
                h = await idle_with_system(c, "dA", 1)
                note = h["messages"][-1]
                assert note["from"] == "system" and not note.get("error")
                assert any(e["kind"] == "activity" for e in t.events)   # a line while it compacts
        assert user_texts(chat_env)[-1] == "/compact"
        assert len(starts(chat_env)) == 1
    run(go())


def test_a_known_command_goes_to_claude_as_is_even_with_a_quote(chat_env):
    async def go():
        async with bridge_client() as c:
            await send(c, "dA", "say a")
            await idle_with(c, "dA", 1)
            await send(c, "dA", "/review-it src", quote=2)
            await idle_with(c, "dA", 2)
        assert user_texts(chat_env)[-1] == "/review-it src"
    run(go())


@pytest.mark.parametrize("text", ["/nope", "/nope with args"])
def test_unknown_command_gets_a_clear_system_message(chat_env, text):
    async def go():
        async with bridge_client() as c:
            await send(c, "dA", "say a")
            await idle_with(c, "dA", 1)
            status, _ = await send(c, "dA", text)
            assert status == 200
            h = await idle_with_system(c, "dA", 1)
            assert h["messages"][-2]["text"] == text
            assert "/nope" in h["messages"][-1]["text"] and h["busy"] is False
        assert text not in user_texts(chat_env)
    run(go())


@pytest.mark.parametrize("cmd", ["/help", "/exit", "/resume", "/login"])
def test_terminal_only_commands_are_not_sent(chat_env, cmd):
    async def go():
        async with bridge_client() as c:
            await send(c, "dA", cmd)
            h = await idle_with_system(c, "dA", 1)
            assert cmd in h["messages"][-1]["text"]
        assert starts(chat_env) == []
    run(go())


@pytest.mark.parametrize("cmd", ["/model sonnet", "/effort high", "/fast on"])
def test_model_commands_are_locked_to_opus_medium(chat_env, cmd):
    async def go():
        async with bridge_client() as c:
            await send(c, "dA", cmd)
            h = await idle_with_system(c, "dA", 1)
            assert "налаштуван" in h["messages"][-1]["text"] and h["busy"] is False
        assert starts(chat_env) == []
    run(go())


def test_model_commands_are_not_offered():
    names = {c["name"] for c in chat.commands()}
    assert not names & {"model", "effort", "fast"}
    chat._merge_commands(["model", "effort", "fast", "compact"])
    names = {c["name"] for c in chat.commands()}
    assert "compact" in names and not names & {"model", "effort", "fast"}


@pytest.mark.parametrize("text", ["/tmp/shot.png що тут", "/ просто риска", "//comment"])
def test_text_that_only_starts_with_a_slash_is_a_normal_message(chat_env, text):
    async def go():
        async with bridge_client() as c:
            await send(c, "dA", "say a")
            await idle_with(c, "dA", 1)
            await send(c, "dA", text)
            await idle_with(c, "dA", 2)
        assert user_texts(chat_env)[-1] == text
    run(go())


# ─── language: Ukrainian or English, from the plugin settings ────────────

def prompt_of(start):
    argv = start["argv"]
    return argv[argv.index("--append-system-prompt") + 1]


def test_default_language_is_ukrainian(chat_env):
    async def go():
        async with bridge_client() as c:
            await send(c, "dA", "say a")
            await idle_with(c, "dA", 1)
            h = await history(c, "dA")
            assert h["lang"] == "uk"
        p = prompt_of(starts(chat_env)[0])
        assert "Reply in Ukrainian" in p and "English" not in p
    run(go())


def test_lang_reaches_claude_instructions(chat_env):
    async def go():
        async with bridge_client() as c:
            await send(c, "dA", "say a", lang="en")
            await idle_with(c, "dA", 1)
        assert "Reply in English" in prompt_of(starts(chat_env)[0])
    run(go())


def test_language_change_restarts_claude_and_keeps_the_session(chat_env):
    async def go():
        async with bridge_client() as c:
            await send(c, "dA", "say a")
            await idle_with(c, "dA", 1)
            await send(c, "dA", "say b")          # no lang: the stored one stays
            await idle_with(c, "dA", 2)
            assert len(starts(chat_env)) == 1
            await send(c, "dA", "say c", lang="en")
            h = await idle_with(c, "dA", 3)
            assert h["messages"][-1]["text"] == "c"
            assert not [m for m in h["messages"] if m["from"] == "system"]
        first, second = starts(chat_env)
        assert "Reply in Ukrainian" in prompt_of(first)
        assert "Reply in English" in prompt_of(second)
        assert second["argv"][second["argv"].index("--resume") + 1] == first["session"]
    run(go())


def _blocks(msg):
    c = msg["message"]["content"]
    return [c] if isinstance(c, str) else [b.get("text", "") for b in c if b.get("type") == "text"]


def test_language_change_tells_claude_once(chat_env):
    """--resume keeps the old talk, and Claude follows its language over a one-line
    system prompt. So the first message after a switch carries a note too, once."""
    async def go():
        async with bridge_client() as c:
            await send(c, "dA", "say a")
            await idle_with(c, "dA", 1)
            await send(c, "dA", "say b", lang="en")
            h = await idle_with(c, "dA", 2)
            assert h["messages"][-1]["text"] == "b"
            assert [m["text"] for m in h["messages"] if m["from"] == "me"] == ["say a", "say b"]
            await send(c, "dA", "say c")
            await idle_with(c, "dA", 3)
            await send(c, "dA", "say d", lang="uk")
            await idle_with(c, "dA", 4)
        a, b, cc, d = stdin_msgs(chat_env)
        assert not [x for x in _blocks(a) if "language" in x or "Мову" in x]
        assert any("Reply in English" in x for x in _blocks(b)), _blocks(b)
        assert not [x for x in _blocks(cc) if "language" in x], "the note is sent once"
        assert any("українською" in x for x in _blocks(d)), _blocks(d)
    run(go())


def test_language_note_skips_a_fresh_chat(chat_env):
    async def go():
        async with bridge_client() as c:
            await send(c, "dA", "say a", lang="en")      # nothing said yet: the system prompt is enough
            await idle_with(c, "dA", 1)
            await send(c, "dA", "say b", lang="uk")
            await idle_with(c, "dA", 2)
        a, b = stdin_msgs(chat_env)
        assert not [x for x in _blocks(a) if "language" in x or "Мову" in x]
        assert any("українською" in x for x in _blocks(b))
    run(go())


def test_bad_lang_is_ignored(chat_env):
    async def go():
        async with bridge_client() as c:
            await send(c, "dA", "/help", lang="fr")
            h = await idle_with_system(c, "dA", 1)
            assert h["lang"] == "uk" and "лише в терміналі" in h["messages"][-1]["text"]
    run(go())


@pytest.mark.parametrize("cmd,words", [
    ("/help", "only works in the terminal"),
    ("/model sonnet", "settings"),
    ("/nope", "There is no /nope command"),
])
def test_system_notes_are_in_english_when_lang_is_en(chat_env, cmd, words):
    async def go():
        async with bridge_client() as c:
            await send(c, "dA", "say a", lang="en")
            await idle_with(c, "dA", 1)
            await send(c, "dA", cmd, lang="en")
            h = await idle_with_system(c, "dA", 1)
            note = h["messages"][-1]["text"]
            assert words in note, note
            assert not re.search("[а-яіїєґ]", note, re.I), note
    run(go())


def test_clear_note_follows_the_language(chat_env):
    async def go():
        async with bridge_client() as c:
            await send(c, "dA", "/clear", lang="en")
            h = await idle_with_system(c, "dA", 1)
            assert h["messages"][-1]["text"] == "Started a new conversation. Claude does not see the earlier messages."
            await send(c, "dA", "/clear", lang="uk")
            h = await idle_with_system(c, "dA", 2)
            assert h["messages"][-1]["text"] == "Почав нову розмову. Claude не бачить попередні повідомлення."
    run(go())


def test_chat_load_sets_the_language_and_it_survives_a_restart(chat_env):
    async def go():
        async with bridge_client() as c:
            async with Tab(c, "dA") as t:
                await t.ws.send_str(json.dumps({"type": "chat-load", "lang": "en"}))
                ev = await until(lambda: next((e for e in t.events if e["kind"] == "history"), None),
                                 what="history event")
                assert ev["lang"] == "en"
            await send(c, "dA", "/help")           # an HTTP send without lang
            h = await idle_with_system(c, "dA", 1)
            assert "only works in the terminal" in h["messages"][-1]["text"]
        chat.reset()
        async with bridge_client() as c:
            assert (await history(c, "dA"))["lang"] == "en"
    run(go())


def test_chat_send_over_the_socket_carries_the_language(chat_env):
    async def go():
        async with bridge_client() as c:
            async with Tab(c, "dA") as t:
                await t.ws.send_str(json.dumps({"type": "chat-send", "text": "/exit", "lang": "en"}))
                await idle_with_system(c, "dA", 1)
        assert starts(chat_env) == []          # no Claude for a terminal command
        assert chat.SESSIONS["dA"].lang == "en"
    run(go())


@pytest.mark.parametrize("kind,raw,word", [
    ("authentication_failed", "Not logged in", "/login"),
    ("rate_limit", "limit", "limit"),
    ("server_error", "API Error: 529 Overloaded", "server"),
    (None, "", "error"),
])
def test_error_text_in_english(kind, raw, word):
    text = chat.error_text(kind, raw, "en")
    assert word in text.lower() or word in text
    first = text.splitlines()[0]
    assert not re.search("[а-яіїєґ]", first, re.I) and not re.search("[—–\"«»“”„]", first), first


# ─── figmosha.py follows the session's file ───────────────────────────────

class _Recorder(BaseHTTPRequestHandler):
    bodies = []

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        _Recorder.bodies.append(json.loads(self.rfile.read(n)))
        b = json.dumps({"ok": True, "result": "x", "value": "x", "logs": [], "elapsed_ms": 1}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def log_message(self, *a):
        pass


@pytest.mark.parametrize("env_target,flag,expected", [
    ("dBanner", None, "dBanner"),        # inside a chat session: no -T needed
    ("dBanner", "Other", "Other"),       # an explicit -T still wins
    (None, None, None),                  # outside a session: unchanged
])
def test_figmosha_cli_targets_the_session_file(env_target, flag, expected):
    _Recorder.bodies = []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Recorder)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        env = {k: v for k, v in os.environ.items() if k != "FIGMOSHA_TARGET"}
        env["FIGMOSHA_PORT"] = str(srv.server_address[1])
        if env_target:
            env["FIGMOSHA_TARGET"] = env_target
        cmd = [sys.executable, str(ROOT / "figmosha.py"), "exec"] + (["-T", flag] if flag else []) + ["return 1"]
        subprocess.run(cmd, env=env, capture_output=True, timeout=30)
    finally:
        srv.shutdown()
    assert _Recorder.bodies, "figmosha.py did not call the bridge"
    assert _Recorder.bodies[0].get("target") == expected

# ═══ edge branches: commands, errors, sessions, queue, quotes, photos ═══

def errors(h):
    return [m for m in h["messages"] if m["from"] == "system" and m.get("error")]


def system(h):
    return [m for m in h["messages"] if m["from"] == "system"]


def user_contents(tmp_path):
    return [m["message"]["content"] for m in stdin_msgs(tmp_path) if m["type"] == "user"]


async def errored(c, doc, n=1):
    """Wait until the chat is idle and shows at least n error lines."""
    async def ok():
        h = await history(c, doc)
        return h if not h["busy"] and len(errors(h)) >= n else None
    return await until(ok, timeout=12, what=f"{n} error line(s) and idle")


class Peer:
    """A plugin socket that records what the bridge pushes."""

    def __init__(self, fail=False):
        self.fail = fail
        self.got = []

    async def send_str(self, msg):
        if self.fail:
            raise ConnectionResetError("gone")
        self.got.append(json.loads(msg))

    def events(self):
        return [m["event"] for m in self.got]


def with_peers(monkeypatch, *pairs):
    monkeypatch.setattr(chat, "_peers", lambda doc: list(pairs))


class FakeProc:
    """Enough of an asyncio process for Session._read."""

    def __init__(self, out=b"", err=b"", code=0, limit=1000):
        self.stdout = asyncio.StreamReader(limit=limit)
        self.stderr = asyncio.StreamReader()
        self.stdout.feed_data(out)
        self.stdout.feed_eof()
        self.stderr.feed_data(err)
        self.stderr.feed_eof()
        self.returncode = code

    async def wait(self):
        return self.returncode


def line(obj):
    return (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")


# ─── pure helpers: slash_command, _is_known, _command ─────────────────────

@pytest.mark.parametrize("text,name", [
    ("/Clear", "clear"),
    ("/HELP me", "help"),
    ("/review-it src", "review-it"),
    ("/plugin:skill.x", "plugin:skill.x"),
    ("/compact\nkeep the colors", "compact"),
    ("/", None),
    ("/ x", None),
    ("//x", None),
    ("hi /clear", None),
    ("", None),
    (None, None),
])
def test_slash_command_parses_the_name(text, name):
    assert chat.slash_command(text) == name


def test_is_known_matches_names_and_aliases_in_any_case():
    cmds = [{"name": "Usage", "aliases": ["Cost", "stats"]}, {"name": "init"}]
    assert chat._is_known("usage", cmds)
    assert chat._is_known("cost", cmds)
    assert chat._is_known("stats", cmds)
    assert chat._is_known("init", cmds)
    assert not chat._is_known("cos", cmds)
    assert not chat._is_known("nope", cmds)
    assert not chat._is_known("init", [])


def test_command_normalizes_one_entry():
    c = chat._command({"name": "x", "argumentHint": "<a>", "description": None,
                       "aliases": ["ok", "with space", 5, "", "-bad", "y.z"]})
    assert c == {"name": "x", "description": "", "hint": "<a>", "aliases": ["ok", "y.z"]}
    assert chat._command({"name": "y", "hint": "h", "argumentHint": "other"})["hint"] == "h"
    assert chat._command({"name": "z", "aliases": None})["aliases"] == []


# ─── stored list of commands ──────────────────────────────────────────────

def _write_commands(raw):
    chat.DATA_DIR.mkdir(parents=True, exist_ok=True)
    (chat.DATA_DIR / "commands.json").write_text(raw, encoding="utf-8")


@pytest.mark.parametrize("raw", ["{oops", "[1, 2]", '{"commands": "x"}', '{"other": []}', "null"])
def test_known_commands_is_none_for_a_bad_file(raw):
    _write_commands(raw)
    assert chat.known_commands() is None
    assert [c["name"] for c in chat.commands()] == [c["name"] for c in chat.FALLBACK_COMMANDS]


def test_known_commands_is_none_without_a_file():
    assert chat.known_commands() is None


def test_known_commands_drops_entries_without_a_name():
    _write_commands(json.dumps({"commands": [{"name": "ok"}, {"name": 5}, "x", None, {}]}))
    assert chat.known_commands() == [{"name": "ok"}]


def test_fallback_commands_offer_clear_with_aliases_and_no_locked_ones():
    got = {c["name"]: c for c in chat.commands()}
    assert got["clear"]["aliases"] == ["reset", "new"]
    assert got["compact"]["hint"]
    assert not set(got) & chat.LOCKED_COMMANDS
    assert all(set(c) == {"name", "description", "hint", "aliases"} for c in got.values())


def test_stored_list_hides_locked_commands():
    _write_commands(json.dumps({"commands": [{"name": "model"}, {"name": "init"}, {"name": "fast"}]}))
    assert [c["name"] for c in chat.commands()] == ["init"]


def test_merge_commands_reports_changes_only():
    assert chat._merge_commands([]) is False
    assert chat._merge_commands(None) is False
    assert chat._merge_commands(["bad name", 5, None, {"name": 3}]) is False   # nothing valid
    assert not (chat.DATA_DIR / "commands.json").exists()
    assert chat._merge_commands(["init", "compact"]) is True
    assert chat._merge_commands(["init", "compact"]) is False                  # same list again
    assert [c["name"] for c in chat.known_commands()] == ["init", "compact"]


def test_merge_commands_by_name_keeps_descriptions_from_the_last_full_list():
    chat._merge_commands([{"name": "review-it", "description": "A skill", "argumentHint": "[t]"},
                          {"name": "init", "description": "Init"}])
    assert chat._merge_commands(["review-it", "context"]) is True     # init dropped, context new
    got = {c["name"]: c for c in chat.known_commands()}
    assert set(got) == {"review-it", "context"}
    assert got["review-it"]["description"] == "A skill" and got["review-it"]["hint"] == "[t]"
    assert got["context"] == {"name": "context", "description": "", "hint": "", "aliases": []}


# ─── slash commands end to end ────────────────────────────────────────────

def test_any_command_goes_to_claude_before_it_ever_ran(chat_env):
    """With no stored list the bridge cannot tell an unknown command; Claude decides."""
    async def go():
        async with bridge_client() as c:
            await send(c, "dA", "/nope here")
            h = await idle_with(c, "dA", 1)
            assert system(h) == []
        assert user_texts(chat_env) == ["/nope here"]
    run(go())


def test_command_alias_from_claudes_list_goes_to_claude(chat_env):
    async def go():
        async with bridge_client() as c:
            chat._merge_commands([{"name": "usage", "aliases": ["cost"]}])
            await send(c, "dA", "/COST")
            h = await idle_with(c, "dA", 1)
            assert system(h) == []
        assert user_texts(chat_env) == ["/COST"]
    run(go())


@pytest.mark.parametrize("text", ["/RESET", "/Clear now please"])
def test_clear_is_case_insensitive_and_ignores_args(chat_env, text):
    async def go():
        async with bridge_client() as c:
            await send(c, "dA", "say a")
            await idle_with(c, "dA", 1)
            await send(c, "dA", text)
            async def done():
                h = await history(c, "dA")
                return h if not h["busy"] and system(h) else None
            h = await until(done, what="clear note")
            assert h["session"] is None and system(h)[-1]["text"] == chat.tr("uk", "cleared", agent="Claude")
        assert text not in user_texts(chat_env)
    run(go())


@pytest.mark.parametrize("text,cmd", [
    ("/HELP", "help"), ("/status now", "status"), ("/memory", "memory"),
])
def test_terminal_commands_with_case_or_args_are_not_sent(chat_env, text, cmd):
    async def go():
        async with bridge_client() as c:
            status, body = await send(c, "dA", text)
            assert status == 200 and body["ok"]
            h = await history(c, "dA")
            note = system(h)[-1]
            assert note["text"] == chat.tr("uk", "terminal", cmd=cmd) and not note.get("error")
            assert h["busy"] is False
        assert starts(chat_env) == []
    run(go())


def test_terminal_command_wins_over_claudes_list(chat_env):
    """/status is not in Claude's list; it is still a terminal one, not an unknown one."""
    async def go():
        async with bridge_client() as c:
            chat._merge_commands(["init"])
            await send(c, "dA", "/status")
            h = await history(c, "dA")
            assert [m["text"] for m in system(h)] == [chat.tr("uk", "terminal", cmd="status")]
        assert starts(chat_env) == []
    run(go())


def test_slash_text_with_a_photo_is_a_normal_message(chat_env):
    """A photo turns /clear into plain text for Claude, not a new session."""
    async def go():
        async with bridge_client() as c:
            await send(c, "dA", "say a")
            await idle_with(c, "dA", 1)
            sid = chat.SESSIONS["dA"].session_id
            await send(c, "dA", "/clear", images=[f"data:image/png;base64,{PNG}"])
            h = await idle_with(c, "dA", 2)
            assert h["session"] == sid and system(h) == []
        assert user_texts(chat_env)[-1] == "/clear"
        assert len(starts(chat_env)) == 1
    run(go())


def test_slash_text_as_an_answer_is_sent_with_its_question(chat_env):
    async def go():
        async with bridge_client() as c:
            await send(c, "dA", "say Ок\n```questions\nЯка команда?\n```")
            h = await idle_with(c, "dA", 1)
            q = next(m for m in h["messages"] if m.get("questions"))
            await send(c, "dA", "/help", answer={"msg": q["id"], "i": 0})
            h = await idle_with(c, "dA", 2)
            assert system(h) == []
        sent = user_texts(chat_env)[-1]
        assert sent.endswith("/help") and "Яка команда?" in sent
    run(go())


# ─── error_text variants ──────────────────────────────────────────────────

def _line(kind):
    return next(e[2] for e in chat.ERRORS if e[0] == kind)


def test_error_text_kind_wins_over_the_text():
    assert chat.error_text("rate_limit", "Not logged in").splitlines()[0] == _line("rate_limit")


def test_error_text_unknown_kind_falls_back_to_the_text():
    assert chat.error_text("weird_kind", "Credit balance is too low").splitlines()[0] == _line("billing_error")
    assert chat.error_text("weird_kind", "nothing known").startswith(chat.GENERIC_ERROR)


def test_error_text_trims_and_drops_an_empty_detail():
    assert chat.error_text(None, "  \n ") == chat.GENERIC_ERROR
    assert chat.error_text(None, None) == chat.GENERIC_ERROR
    assert chat.error_text("rate_limit", "") == chat.ERRORS[1][2]
    assert chat.error_text(None, "  Prompt is too long \n") == _line("invalid_request") + "\nPrompt is too long"


# ─── a lost session (--resume finds nothing) ──────────────────────────────

def _wrapper(tmp_path, on_resume):
    """A claude that fails on --resume as on_resume says, and is the fake one otherwise."""
    p = tmp_path / "wrap_claude.py"
    p.write_text(
        "import json, os, sys, time\n"
        "args = sys.argv[1:]\n"
        "if '--resume' in args:\n"
        f"{on_resume}"
        f"os.execv(sys.executable, [sys.executable, {str(FAKE)!r}] + args)\n",
        encoding="utf-8")
    return [sys.executable, str(p)]


def _saved(doc, sid):
    chat.DATA_DIR.mkdir(parents=True, exist_ok=True)
    (chat.DATA_DIR / "sessions.json").write_text(json.dumps({doc: sid}))


def test_lost_session_seen_only_in_stderr_starts_a_new_one(chat_env):
    """No result line: the reader finds No conversation found in stderr."""
    async def go():
        _saved("dA", "old-1")
        chat.CLAUDE_CMD = _wrapper(chat_env, (
            "    sys.stderr.write('No conversation found with session ID: old-1\\n')\n"
            "    sys.stderr.flush()\n"
            "    sys.exit(1)\n"))
        async with bridge_client() as c:
            await send(c, "dA", "say привіт")
            h = await idle_with(c, "dA", 1)
            assert h["messages"][-1]["text"] == "привіт"
            assert errors(h) == []
            assert [m["text"] for m in system(h)] == [chat.tr("uk", "lost")]
            assert h["session"] and h["session"] != "old-1"
        s = starts(chat_env)
        assert len(s) == 1 and "--resume" not in s[0]["argv"]
    run(go())


def test_lost_session_process_that_stays_alive_is_killed(chat_env):
    """The lost result came, but the process does not exit: it is killed, not waited for."""
    async def go():
        _saved("dA", "old-2")
        chat.CLAUDE_CMD = _wrapper(chat_env, (
            "    print(json.dumps({'type': 'result', 'subtype': 'error_during_execution',"
            " 'is_error': True, 'errors': ['No conversation found with session ID: old-2']}),"
            " flush=True)\n"
            "    time.sleep(15)\n"
            "    sys.exit(0)\n"))           # never falls through to the fake claude
        async with bridge_client() as c:
            t0 = time.monotonic()
            await send(c, "dA", "say швидко")
            h = await idle_with(c, "dA", 1)
            assert time.monotonic() - t0 < 4          # no 5 s wait for the old reader
            assert h["messages"][-1]["text"] == "швидко" and errors(h) == []
            assert system(h)[0]["text"] == chat.tr("uk", "lost")
        saved = json.loads((chat.DATA_DIR / "sessions.json").read_text())
        assert saved["dA"] == starts(chat_env)[0]["session"]
    run(go())


# ─── OSError and other failures around a turn ─────────────────────────────

def test_sessions_file_that_cannot_be_written_does_not_stop_the_chat(chat_env, capsys):
    async def go():
        (chat.DATA_DIR / "sessions.json").mkdir(parents=True)   # a folder where the file should be
        async with bridge_client() as c:
            await send(c, "dA", "say працює")
            h = await idle_with(c, "dA", 1)
            assert h["messages"][-1]["text"] == "працює" and errors(h) == []
            assert h["session"]                        # kept in memory
            await send(c, "dA", "say ще")
            await idle_with(c, "dA", 2)
        assert len(starts(chat_env)) == 1              # the same process answers both
    run(go())
    assert "cannot save sessions.json" in capsys.readouterr().err


def test_save_session_id_none_for_an_unknown_doc_writes_nothing():
    chat._save_session_id("dA", None)
    assert not (chat.DATA_DIR / "sessions.json").exists()
    chat._save_session_id("dA", "s1")
    chat._save_session_id("dB", "s2")
    chat._save_session_id("dA", None)
    assert json.loads((chat.DATA_DIR / "sessions.json").read_text()) == {"dB": "s2"}


def test_claude_dying_under_the_write_reports_its_own_stderr(monkeypatch):
    async def go():
        chat.CLAUDE_CMD = [sys.executable, "-c",
                           "import sys; sys.stderr.write('bad flag\\n'); sys.exit(2)"]

        async def write(self, obj):
            await asyncio.wait_for(self.proc.wait(), 5)     # the process is gone by now
            raise BrokenPipeError("pipe")
        monkeypatch.setattr(chat.Session, "_write", write)
        async with bridge_client() as c:
            await send(c, "dA", "hi")
            h = await errored(c, "dA")
            e = errors(h)[0]["text"]
            assert "код 2" in e and "bad flag" in e
            assert "Не вдалося запустити" not in e
    run(go())


# ─── /clear when the old process will not exit ────────────────────────────


# ─── process argv and environment ─────────────────────────────────────────

def test_argv_carries_the_file_name_and_cleans_the_environment(monkeypatch):
    async def go():
        monkeypatch.setenv("CLAUDECODE", "1")
        with_peers(monkeypatch, (Peer(), ""), (Peer(), "Banner"))
        argv, env = chat.Session("dA")._argv()
        assert "CLAUDECODE" not in env                  # the bridge may run inside Claude Code
        assert env["PYTHONIOENCODING"] == "utf-8"
        assert env["FIGMOSHA_FILE"] == "Banner"          # the first tab with a name
        prompt = argv[argv.index("--append-system-prompt") + 1]
        assert '"Banner"' in prompt
        assert "--resume" not in argv
    run(go())


def test_argv_without_a_file_name_uses_the_doc_id(monkeypatch):
    async def go():
        with_peers(monkeypatch)
        argv, env = chat.Session("dA")._argv()
        assert env["FIGMOSHA_FILE"] == "" and env["FIGMOSHA_TARGET"] == "dA"
        assert '"dA"' in argv[argv.index("--append-system-prompt") + 1]
    run(go())


def test_argv_resumes_the_saved_session_id():
    async def go():
        chat._write_json(chat.DATA_DIR / "sessions.json", {"dA": "sess-saved", "dB": "other"})
        s = chat.Session("dA")
        assert s.session_id == "sess-saved"
        argv, _ = s._argv()
        assert argv[-2:] == ["--resume", "sess-saved"]
    run(go())


def test_claude_from_the_native_installer_is_found_off_the_path(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".local" / "bin").mkdir(parents=True)
    exe = home / ".local" / "bin" / "claude"
    exe.write_text("")
    monkeypatch.setattr(chat.Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(chat.shutil, "which", lambda name: None)
    monkeypatch.setattr(chat, "CLAUDE_CMD", ["claude", "--x"])
    assert chat._home_claude("claude") == str(exe)
    assert chat._home_claude("other-cli") is None        # only the plain claude command
    argv, _ = chat.Session("dA")._argv()
    assert argv[0] == str(exe) and argv[1] == "--x"
    (home / ".local" / "bin" / "claude.exe").write_text("")
    assert chat._home_claude("claude") == str(home / ".local" / "bin" / "claude.exe")  # Windows first
    exe.unlink()
    (home / ".local" / "bin" / "claude.exe").unlink()
    assert chat._home_claude("claude") is None


# ─── session id save and load ─────────────────────────────────────────────

def test_a_new_session_id_mid_stream_is_saved_next_to_the_others(monkeypatch):
    async def go():
        with_peers(monkeypatch)
        chat._write_json(chat.DATA_DIR / "sessions.json", {"dB": "b-1"})
        s = chat.Session("dA")
        await s._on_event({"type": "system", "subtype": "other", "session_id": "a-1"})
        await s._on_event({"type": "system", "subtype": "other", "session_id": "a-2"})
        assert s.session_id == "a-2"
        assert json.loads((chat.DATA_DIR / "sessions.json").read_text()) == {"dB": "b-1", "dA": "a-2"}
        chat._save_session_id("dA", None)
        assert json.loads((chat.DATA_DIR / "sessions.json").read_text()) == {"dB": "b-1"}
    run(go())


# ─── history persistence ──────────────────────────────────────────────────

def test_next_id_from_the_file_is_kept_when_it_is_ahead(monkeypatch):
    async def go():
        with_peers(monkeypatch)
        f = chat.DATA_DIR / "history" / "dA.json"
        chat._write_json(f, {"next_id": 50, "messages": [{"id": 2, "from": "me", "text": "a", "t": 1}]})
        s = chat.Session("dA")
        m = await s.add("me", "b", images=[], quote=None, answer="")
        assert m["id"] == 50
        assert set(m) == {"id", "from", "text", "t"}       # empty extras are not stored
        saved = json.loads(f.read_text(encoding="utf-8"))
        assert saved["next_id"] == 51 and [x["id"] for x in saved["messages"]] == [2, 50]
        chat._write_json(f, {"next_id": 1, "messages": [{"id": 7, "from": "me", "text": "a", "t": 1}]})
        s = chat.Session("dA")
        assert (await s.add("me", "c"))["id"] == 8           # never reuses an id from the file
    run(go())


# ─── push and streaming events ────────────────────────────────────────────

def test_a_broken_socket_does_not_stop_events_to_the_others(monkeypatch):
    async def go():
        bad, good = Peer(fail=True), Peer()
        with_peers(monkeypatch, (bad, "x"), (good, "x"))
        s = chat.Session("dA")
        await s.push({"kind": "busy", "busy": True})
        assert good.got == [{"type": "chat", "doc": "dA", "event": {"kind": "busy", "busy": True}}]
    run(go())


def test_only_text_deltas_are_streamed(monkeypatch):
    async def go():
        p = Peer()
        with_peers(monkeypatch, (p, "x"))
        s = chat.Session("dA")
        for e in ({"type": "content_block_start", "content_block": {"type": "text"}},
                  {"type": "content_block_delta", "delta": {"type": "input_json_delta", "partial_json": "{"}},
                  {"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "hm"}},
                  {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Привіт"}}):
            await s._on_event({"type": "stream_event", "event": e})
        assert p.events() == [{"kind": "delta", "text": "Привіт"}]
    run(go())


def test_activity_is_capped_and_cleared_when_idle(monkeypatch):
    async def go():
        p = Peer()
        with_peers(monkeypatch, (p, "x"))
        s = chat.Session("dA")
        await s.set_busy(True)
        for i in range(chat.ACTIVITY_LIMIT + 5):
            await s._on_event({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Bash", "input": {"command": f"c{i}"}}]}})
        assert len(s.activity) == chat.ACTIVITY_LIMIT
        assert s.activity[0]["text"] == "Bash: c5" and s.activity[-1]["text"] == f"Bash: c{chat.ACTIVITY_LIMIT + 4}"
        assert s.state()["activity"] == s.activity
        await s.set_busy(False)
        await s.set_busy(False)                            # same state: no second event
        assert s.activity == []
        busy = [e for e in p.events() if e["kind"] == "busy"]
        assert busy == [{"kind": "busy", "busy": True}, {"kind": "busy", "busy": False}]
    run(go())


def test_commands_event_only_when_the_list_changes(monkeypatch):
    async def go():
        p = Peer()
        with_peers(monkeypatch, (p, "x"))
        s = chat.Session("dA")
        init = {"type": "system", "subtype": "init", "slash_commands": ["clear", "compact"]}
        await s._on_event(init)
        await s._on_event(init)
        cmds = [e for e in p.events() if e["kind"] == "commands"]
        assert len(cmds) == 1
        assert [c["name"] for c in cmds[0]["commands"]] == ["clear", "compact"]
    run(go())


def test_api_error_kind_lasts_one_turn_only(monkeypatch):
    async def go():
        with_peers(monkeypatch)
        s = chat.Session("dA")
        loop = asyncio.get_running_loop()
        s.turn = loop.create_future()
        await s._on_event({"type": "assistant", "error": "rate_limit", "is_api_error_message": True,
                           "message": {"content": [{"type": "text", "text": "x"}]}})
        assert s.messages == []                            # the CLI's English line is not shown
        await s._on_event({"type": "result", "subtype": "success", "is_error": True, "result": "boom"})
        assert s.turn.result() == _line("rate_limit") + "\nboom"
        s.turn = loop.create_future()
        await s._on_event({"type": "result", "subtype": "success", "is_error": True, "result": "boom"})
        assert s.turn.result() == chat.GENERIC_ERROR + "\nboom"
    run(go())


def test_error_result_while_stopping_is_silent(monkeypatch):
    async def go():
        with_peers(monkeypatch)
        s = chat.Session("dA")
        s.turn = asyncio.get_running_loop().create_future()
        s.stopping = True
        await s._on_event({"type": "result", "subtype": "error_during_execution", "is_error": True})
        assert s.turn.result() is None
    run(go())


# ─── the reader ───────────────────────────────────────────────────────────

def test_an_output_line_over_the_limit_is_skipped(monkeypatch):
    async def go():
        with_peers(monkeypatch)
        s = chat.Session("dA")
        s.turn = asyncio.get_running_loop().create_future()
        proc = FakeProc(out=b'{"type": "assistant", "x": "' + b"y" * 5000 + b'"}\n'
                        + line({"type": "assistant", "message": {"content": [{"type": "text", "text": "після"}]}})
                        + line({"type": "result", "subtype": "success", "is_error": False}))
        s.proc = proc
        await s._read(proc)
        assert [m["text"] for m in s.messages] == ["після"]
        assert s.turn.result() is None and s.proc is None
    run(go())


def test_exit_with_a_lost_session_on_stderr_marks_it_lost(monkeypatch):
    async def go():
        with_peers(monkeypatch)
        s = chat.Session("dA")
        s.turn = asyncio.get_running_loop().create_future()
        proc = FakeProc(err=b"No conversation found with session ID: x\n", code=1)
        await s._read(proc)
        assert s.lost is True
        assert "(код 1)" in s.turn.result() and "No conversation found" in s.turn.result()
    run(go())


def test_exit_while_stopping_ends_the_turn_without_an_error(monkeypatch):
    async def go():
        with_peers(monkeypatch)
        s = chat.Session("dA")
        s.turn = asyncio.get_running_loop().create_future()
        s.stopping = True
        await s._read(FakeProc(err=b"killed\n", code=-9))
        assert s.turn.result() is None and s.lost is False
    run(go())


# ─── a turn that fails under the write ────────────────────────────────────


def test_write_error_with_a_live_process_says_it_could_not_start(chat_env, monkeypatch):
    async def bad_write(self, obj):
        raise ConnectionResetError("reset")

    async def go():
        monkeypatch.setattr(chat.Session, "_write", bad_write)
        async with bridge_client() as c:
            await send(c, "dA", "say x")
            h = await errored(c, "dA")
            assert errors(h)[-1]["text"] == chat.tr("uk", "no_start", e="reset")
            assert h["busy"] is False
    run(go())


def test_an_unexpected_error_in_a_turn_never_leaves_the_chat_busy(chat_env, monkeypatch):
    real = chat.Session._write
    calls = []

    async def flaky(self, obj):
        calls.append(obj)
        if len(calls) == 1:
            raise RuntimeError("oops")
        await real(self, obj)

    async def go():
        monkeypatch.setattr(chat.Session, "_write", flaky)
        async with bridge_client() as c:
            await send(c, "dA", "say перша")
            h = await errored(c, "dA")
            assert errors(h)[-1]["text"] == chat.tr("uk", "broken", e="oops")
            await send(c, "dA", "say друга")
            h = await idle_with(c, "dA", 1)
            assert h["messages"][-1]["text"] == "друга"
        assert len(starts(chat_env)) == 1                  # the same process took the next turn
    run(go())


# ─── queue and stop ───────────────────────────────────────────────────────

def test_queued_turns_keep_the_chat_busy_until_the_last(chat_env):
    async def go():
        async with bridge_client() as c:
            async with Tab(c, "dA") as t:
                await send(c, "dA", "hold a")
                await send(c, "dA", "say b")
                await send(c, "dA", "say c")
                release(chat_env)
                await idle_with(c, "dA", 3)
                await until(lambda: {"kind": "busy", "busy": False} in t.events)
                busy = [e for e in t.events if e["kind"] == "busy"]
                assert busy == [{"kind": "busy", "busy": True}, {"kind": "busy", "busy": False}]
        assert user_contents(chat_env) == ["hold a", "say b", "say c"]
    run(go())


def test_stop_drops_the_queued_messages_too(chat_env):
    async def go():
        async with bridge_client() as c:
            await send(c, "dA", "slow")
            await until(lambda: chat.SESSIONS["dA"].sent, what="slow reached claude")
            await send(c, "dA", "say в черзі")
            assert chat.SESSIONS["dA"].inbox.qsize() == 1
            await c.post("/chat/stop", json={"doc": "dA"}, headers=PLUGIN)
            await until(lambda: not chat.SESSIONS["dA"].busy, what="idle after stop")
            await send(c, "dA", "say далі")                    # the queue is in order: a dropped
            h = await idle_with(c, "dA", 2)                    # message would go before this one
            assert h["messages"][-1]["text"] == "далі"
            assert "say в черзі" in [m["text"] for m in h["messages"]]   # still in the chat
            assert errors(h) == []
        assert "say в черзі" not in user_contents(chat_env)              # never sent to Claude
    run(go())


def test_stop_before_the_worker_takes_the_message_just_goes_idle(monkeypatch):
    async def go():
        p = Peer()
        with_peers(monkeypatch, (p, "x"))
        s = chat.session("dA")
        await s.set_busy(True)
        s.inbox.put_nowait("say x")
        await chat.stop("dA")
        assert s.busy is False and s.stopping is False and s.inbox.empty()
        assert p.events()[-1] == {"kind": "busy", "busy": False}
    run(go())


def test_stop_on_an_idle_session_pushes_nothing(monkeypatch):
    async def go():
        p = Peer()
        with_peers(monkeypatch, (p, "x"))
        s = chat.session("dA")
        await chat.stop("dA")
        await chat.stop("dNobody")                         # no session at all
        assert p.got == [] and s.stopping is False
        assert chat.status_of("dA") == {"busy": False} and chat.status_of("dNobody") is None
    run(go())


# ─── closing and /clear with a process that will not exit ─────────────────


def test_new_session_kills_a_process_that_will_not_exit(monkeypatch):
    async def go():
        with_peers(monkeypatch)
        s = chat.Session("dA")
        s.session_id = "old"
        chat._save_session_id("dA", "old")
        s.proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c", "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)",
            stdin=asyncio.subprocess.PIPE)
        proc = s.proc
        t0 = time.monotonic()
        await s._new_session()
        assert time.monotonic() - t0 < 10                  # killed, not waited out (sleep 30)
        assert proc.returncode is not None
        assert s.proc is None and s.session_id is None
        assert "dA" not in chat._sessions()
        assert s.messages[-1]["from"] == "system" and not s.messages[-1].get("error")
    run(go())


# ─── quotes and answers in the content ────────────────────────────────────

def seed_history(messages):
    chat._write_json(chat.DATA_DIR / "history" / "dA.json",
                     {"next_id": max(m["id"] for m in messages) + 1, "messages": messages})


def test_multi_line_quote_is_sent_as_a_block_quote(chat_env):
    async def go():
        seed_history([{"id": 1, "from": "claude", "text": "рядок 1\nрядок 2", "t": 1}])
        async with bridge_client() as c:
            await send(c, "dA", "say ок", quote=1)
            await idle_with(c, "dA", 2)
        assert user_contents(chat_env)[-1] == "[Цитата з чату]\n> рядок 1\n> рядок 2\n\nsay ок"
    run(go())


@pytest.mark.parametrize("quote", [True, 99, "1", 1.0])
def test_a_quote_that_is_not_a_known_message_id_is_ignored(chat_env, quote):
    async def go():
        seed_history([{"id": 1, "from": "claude", "text": "ціль", "t": 1}])
        async with bridge_client() as c:
            await send(c, "dA", "say ок", quote=quote)
            h = await idle_with(c, "dA", 2)
            assert "quote" not in [m for m in h["messages"] if m["from"] == "me"][-1]
        assert user_contents(chat_env)[-1] == "say ок"
    run(go())


def test_answer_wins_over_a_quote_and_has_a_fixed_prefix(chat_env):
    async def go():
        seed_history([{"id": 1, "from": "claude", "text": "інше", "t": 1},
                      {"id": 2, "from": "claude", "text": "Питаю.", "t": 1,
                       "questions": ["Який шрифт?", "Який колір?"]}])
        async with bridge_client() as c:
            await send(c, "dA", "Синій", answer={"msg": 2, "i": 1}, quote=1)
            h = await idle_with(c, "dA", 3)
            mine = [m for m in h["messages"] if m["from"] == "me"][-1]
            assert mine["answer"] == {"msg": 2, "i": 1}
            assert mine["quote"] == {"id": 2, "from": "claude", "text": "Питання 2. Який колір?"}
        assert user_contents(chat_env)[-1] == "[Відповідь на твоє питання: Питання 2. Який колір?]\nСиній"
    run(go())


@pytest.mark.parametrize("answer", [{"msg": 2, "i": -1}, {"msg": 2, "i": 2}, {"msg": 9, "i": 0},
                                    {"msg": 2}, {"msg": "2", "i": 0}, [2, 0]])
def test_an_answer_to_no_real_question_falls_back_to_plain_text(chat_env, answer):
    async def go():
        seed_history([{"id": 2, "from": "claude", "text": "Питаю.", "t": 1, "questions": ["А?", "Б?"]}])
        async with bridge_client() as c:
            await send(c, "dA", "say ок", answer=answer)
            h = await idle_with(c, "dA", 2)
            mine = [m for m in h["messages"] if m["from"] == "me"][-1]
            assert "answer" not in mine and "quote" not in mine
        assert user_contents(chat_env)[-1] == "say ок"
    run(go())


def test_a_bool_answer_index_is_refused_like_a_bool_quote(chat_env):
    async def go():
        seed_history([{"id": 2, "from": "claude", "text": "Питаю.", "t": 1, "questions": ["А?", "Б?"]}])
        async with bridge_client() as c:
            await send(c, "dA", "say ок", answer={"msg": 2, "i": True})
            h = await idle_with(c, "dA", 2)
            assert "answer" not in [m for m in h["messages"] if m["from"] == "me"][-1]
    run(go())


def test_a_bool_answer_msg_id_is_refused_like_a_bool_quote(chat_env):
    async def go():
        # True == 1: a bool msg id would find message 1 and take its question 0.
        seed_history([{"id": 1, "from": "claude", "text": "Питаю.", "t": 1, "questions": ["А?", "Б?"]}])
        async with bridge_client() as c:
            await send(c, "dA", "say ок", answer={"msg": True, "i": 0})
            h = await idle_with(c, "dA", 2)
            assert "answer" not in [m for m in h["messages"] if m["from"] == "me"][-1]
    run(go())


# ─── photos in the content ────────────────────────────────────────────────

def test_photo_with_a_quote_sends_image_then_quote_then_text(chat_env):
    async def go():
        seed_history([{"id": 1, "from": "claude", "text": "ціль", "t": 1}])
        async with bridge_client() as c:
            await send(c, "dA", "say ок", images=[f"data:image/png;base64,{PNG}"], quote=1)
            await idle_with(c, "dA", 2)
        blocks = user_contents(chat_env)[-1]
        assert [b["type"] for b in blocks] == ["image", "text", "text"]
        assert blocks[1]["text"] == "[Цитата з чату]\n> ціль"
        assert blocks[2]["text"] == "say ок"
    run(go())


def test_photo_only_message_sends_only_image_blocks(chat_env):
    async def go():
        async with bridge_client() as c:
            await send(c, "dA", "", images=[f"data:image/png;base64,{PNG}"] * 2)
            await idle_with(c, "dA", 1)
        blocks = user_contents(chat_env)[-1]
        assert [b["type"] for b in blocks] == ["image", "image"]
    run(go())


def test_jpeg_with_line_breaks_is_saved_as_jpg(chat_env):
    async def go():
        wrapped = "\n".join(PNG[i:i + 20] for i in range(0, len(PNG), 20))
        async with bridge_client() as c:
            await send(c, "dA", "say ок", images=[f"data:image/jpeg;base64,{wrapped}"])
            h = await idle_with(c, "dA", 1)
            url = h["messages"][0]["images"][0]
            assert url.endswith(".jpg")
            r = await c.get(url, headers=PLUGIN)
            assert r.status == 200 and await r.read() == base64.b64decode(PNG)
        img = [b for b in user_contents(chat_env)[-1] if b["type"] == "image"][0]
        assert img["source"] == {"type": "base64", "media_type": "image/jpeg", "data": PNG}
    run(go())


def test_photos_over_the_limit_and_bad_padding_are_dropped_with_a_count(chat_env):
    async def go():
        good = f"data:image/png;base64,{PNG}"
        imgs = [good] * (chat.MAX_IMAGES - 1) + ["data:image/png;base64,abc"] + [good] * 2
        async with bridge_client() as c:
            await send(c, "dA", "say ок", images=imgs)
            h = await idle_with(c, "dA", 1)
            assert len(h["messages"][0]["images"]) == chat.MAX_IMAGES - 1
            note = errors(h)
            assert [m["text"] for m in note] == [chat.tr("uk", "photos_dropped", n=3)]
        assert len([b for b in user_contents(chat_env)[-1] if b["type"] == "image"]) == chat.MAX_IMAGES - 1
        assert len(list((chat.DATA_DIR / "uploads").iterdir())) == chat.MAX_IMAGES - 1
    run(go())


# ─── HTTP edges of the core routes ────────────────────────────────────────

def test_http_edges_of_send_stop_and_uploads():
    async def go():
        async with bridge_client() as c:
            r = await c.post("/chat/send", data=b"not json", headers={**PLUGIN, "Content-Type": "application/json"})
            assert r.status == 400 and (await r.json())["ok"] is False
            r = await c.post("/chat/stop", json={"doc": "../x"}, headers=PLUGIN)
            assert r.status == 400
            r = await c.get("/chat/uploads/" + "a" * 32 + ".png", headers=PLUGIN)
            assert r.status == 404 and (await r.json())["error"] == "not found"
            r = await c.get("/chat/uploads/x.png", headers=PLUGIN)
            assert r.status == 404 and (await r.json())["error"] == "bad file name"
    run(go())
