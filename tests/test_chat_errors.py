"""Every way the chat can fail, end to end.

Each test drives the real bridge with tests/fake_claude.py in one failure mode
and checks two things: the designer sees a clear Ukrainian line, and the chat
does not stay busy. The full list is the table in docs/CHAT.md (Error states).

    pytest -q tests/test_chat_errors.py
"""

import asyncio
import json
import os
import re
import stat
import sys
import time

import pytest

import chat
from test_chat import (PLUGIN, PNG, Tab, bridge_client, chat_env, errored, errors,  # noqa: F401
                       history, idle_with, release, run, send, starts, system, until)

DASHES_QUOTES = re.compile(r"[—–\"«»„“”‘’]")


def clear_uk(text):
    """The first line is plain Ukrainian: Cyrillic, no dashes, no quotes."""
    first = text.splitlines()[0]
    return bool(re.search(r"[А-Яа-яІіЇїЄєҐґ]", first)) and not DASHES_QUOTES.search(first)


async def still_works(c, doc, n_claude):
    await send(c, doc, "say знову працює")
    h = await idle_with(c, doc, n_claude)
    assert h["messages"][-1]["text"] == "знову працює"
    return h


# ─── what Claude Code itself reports ──────────────────────────────────────

@pytest.mark.parametrize("kind,raw,word", [
    ("authentication_failed", "Not logged in · Please run /login", "/login"),
    (None, "Claude AI usage limit reached|1763740800", "Ліміт"),
    (None, "something new", "помилк"),
])
def test_error_text_is_clear_ukrainian(kind, raw, word):
    text = chat.error_text(kind, raw)
    assert clear_uk(text), text
    assert word in text
    if raw:
        assert raw in text          # the original goes along as the detail line


def test_not_logged_in_is_one_clear_line(chat_env):
    async def go():
        async with bridge_client() as c:
            await send(c, "dA", "autherr")
            h = await errored(c, "dA")
            assert len(errors(h)) == 1
            assert clear_uk(errors(h)[0]["text"]) and "/login" in errors(h)[0]["text"]
            # The CLI's own English line is not shown as if Claude said it.
            assert [m for m in h["messages"] if m["from"] == "claude"] == []
            await still_works(c, "dA", 1)
    run(go())


def test_used_up_subscription_limit_is_explained():
    async def go():
        async with bridge_client() as c:
            await send(c, "dA", "ratelimit")
            h = await errored(c, "dA")
            e = errors(h)[0]["text"]
            assert clear_uk(e) and "Ліміт" in e and "resets 5pm" in e
            assert not [m for m in h["messages"] if m["from"] == "claude"]
    run(go())


@pytest.mark.parametrize("kind,text", [
    ("billing_error", "Credit balance is too low"),
    ("server_error", "API Error: 529 Overloaded"),
    ("invalid_request", "Prompt is too long"),
    ("unknown", "Something odd"),
])
def test_api_errors_are_explained(kind, text):
    async def go():
        async with bridge_client() as c:
            await send(c, "dA", f"apierr {kind} {text}")
            h = await errored(c, "dA")
            assert len(errors(h)) == 1 and clear_uk(errors(h)[0]["text"])
            assert text in errors(h)[0]["text"]
            await still_works(c, "dA", 1)
    run(go())


def test_too_many_turns_result_is_explained():
    async def go():
        async with bridge_client() as c:
            await send(c, "dA", "maxturns")
            h = await errored(c, "dA")
            assert clear_uk(errors(h)[0]["text"]) and "error_max_turns" not in errors(h)[0]["text"]
            await still_works(c, "dA", 1)
    run(go())


def test_execution_error_shows_its_errors_list():
    async def go():
        async with bridge_client() as c:
            await send(c, "dA", "execerr")
            h = await errored(c, "dA")
            e = errors(h)[0]["text"]
            assert clear_uk(e) and "Tool runner failed" in e
            await still_works(c, "dA", 1)
    run(go())


def test_broken_output_lines_are_skipped(chat_env):
    async def go():
        async with bridge_client() as c:
            await send(c, "dA", "garbage")
            h = await idle_with(c, "dA", 1)
            assert [m["text"] for m in h["messages"] if m["from"] == "claude"] == ["still ok"]
            assert errors(h) == []
            await still_works(c, "dA", 2)
            assert len(starts(chat_env)) == 1     # the process was not lost on the way
    run(go())


# ─── the process dies ─────────────────────────────────────────────────────

def test_exit_in_the_middle_of_a_turn_is_reported(chat_env):
    async def go():
        async with bridge_client() as c:
            await send(c, "dA", "die")
            h = await errored(c, "dA")
            assert clear_uk(errors(h)[0]["text"]) and "0" in errors(h)[0]["text"]
            await still_works(c, "dA", 1)
        assert len(starts(chat_env)) == 2
    run(go())


def test_crash_after_part_of_the_reply_keeps_that_part():
    async def go():
        async with bridge_client() as c:
            await send(c, "dA", "partial")
            h = await errored(c, "dA")
            assert [m["text"] for m in h["messages"] if m["from"] == "claude"] == ["half"]
            assert "fatal" in errors(h)[0]["text"] and clear_uk(errors(h)[0]["text"])
            await still_works(c, "dA", 2)
    run(go())


def test_process_dying_while_idle_is_silent_and_the_next_message_resumes(chat_env):
    async def go():
        async with bridge_client() as c:
            await send(c, "dA", "say перша")
            await idle_with(c, "dA", 1)
            chat.SESSIONS["dA"].proc.kill()
            await until(lambda: chat.SESSIONS["dA"].proc is None, what="process gone")
            h = await still_works(c, "dA", 2)
            assert errors(h) == []
        s = starts(chat_env)
        assert len(s) == 2 and s[1]["argv"][s[1]["argv"].index("--resume") + 1] == s[0]["session"]
    run(go())


def test_claude_that_cannot_run_is_a_clear_error(tmp_path):
    async def go():
        bad = tmp_path / "claude-not-executable"
        bad.write_text("not a program")
        bad.chmod(stat.S_IRUSR | stat.S_IWUSR)
        chat.CLAUDE_CMD = [str(bad)]
        async with bridge_client() as c:
            await send(c, "dA", "hi")
            h = await errored(c, "dA")
            assert clear_uk(errors(h)[0]["text"])
    if os.name == "nt":
        pytest.skip("no exec bit on Windows")
    run(go())


def test_missing_claude_binary_says_how_to_fix_it():
    async def go():
        chat.CLAUDE_CMD = ["definitely-not-claude-xyz"]
        async with bridge_client() as c:
            await send(c, "dA", "hi")
            h = await errored(c, "dA")
            assert clear_uk(errors(h)[0]["text"]) and "Встанови" in errors(h)[0]["text"]
            await send(c, "dA", "ще раз")
            assert len(errors(await errored(c, "dA", 2))) == 2   # every try says it again
    run(go())


# ─── the saved session is gone ────────────────────────────────────────────

def test_lost_session_starts_a_new_one_and_answers(chat_env):
    async def go():
        chat.DATA_DIR.mkdir(parents=True)
        (chat.DATA_DIR / "sessions.json").write_text(json.dumps({"dA": "gone-123"}))
        async with bridge_client() as c:
            await send(c, "dA", "say привіт")
            h = await idle_with(c, "dA", 1)
            assert h["messages"][-1]["text"] == "привіт"
            assert errors(h) == []
            note = system(h)
            assert len(note) == 1 and clear_uk(note[0]["text"]) and "нов" in note[0]["text"]
        s = starts(chat_env)
        assert len(s) == 2
        assert "--resume" in s[0]["argv"] and "--resume" not in s[1]["argv"]
        saved = json.loads((chat.DATA_DIR / "sessions.json").read_text())
        assert saved["dA"] == s[1]["session"]
    run(go())


# ─── stop ─────────────────────────────────────────────────────────────────

def test_stop_while_claude_is_still_starting(chat_env, monkeypatch):
    async def go():
        monkeypatch.setenv("FAKE_CLAUDE_START_DELAY", "1.5")
        async with bridge_client() as c:
            await send(c, "dA", "say не треба")
            await asyncio.sleep(0.05)
            t0 = time.monotonic()
            await c.post("/chat/stop", json={"doc": "dA"}, headers=PLUGIN)
            await until(lambda: not chat.SESSIONS["dA"].busy, what="idle after stop")
            assert time.monotonic() - t0 < 3
            h = await history(c, "dA")
            assert [m["from"] for m in h["messages"]] == ["me"] and errors(h) == []
            monkeypatch.delenv("FAKE_CLAUDE_START_DELAY")   # the next start is quick again
            await still_works(c, "dA", 1)
    run(go())


def test_stop_twice_then_send_again():
    async def go():
        async with bridge_client() as c:
            await send(c, "dA", "slow")
            await until(lambda: chat.SESSIONS["dA"].sent, what="slow reached claude")
            await asyncio.gather(
                c.post("/chat/stop", json={"doc": "dA"}, headers=PLUGIN),
                c.post("/chat/stop", json={"doc": "dA"}, headers=PLUGIN))
            await until(lambda: not chat.SESSIONS["dA"].busy, what="idle after stop")
            h = await still_works(c, "dA", 1)
            assert errors(h) == []
    run(go())


# ─── the plugin window comes and goes ─────────────────────────────────────

def test_window_reopened_mid_turn_gets_busy_and_can_stop():
    async def go():
        async with bridge_client() as c:
            async with Tab(c, "dA") as a:
                await a.ws.send_str(json.dumps({"type": "chat-send", "text": "slow"}))
                await until(lambda: any(e["kind"] == "message" and e["message"]["text"] == "starting"
                                        for e in a.events), what="starting")
            # The first window is gone (socket dropped). A new one opens on the same file.
            async with Tab(c, "dA") as b:
                await b.ws.send_str(json.dumps({"type": "chat-load"}))
                await until(lambda: "history" in b.kinds(), what="history")
                hist = next(e for e in b.events if e["kind"] == "history")
                assert hist["busy"] is True
                assert [m["text"] for m in hist["messages"]] == ["slow", "starting"]
                await b.ws.send_str(json.dumps({"type": "chat-stop"}))
                await until(lambda: {"kind": "busy", "busy": False} in b.events, what="idle event")
    run(go())


def test_reply_that_lands_while_the_window_is_closed_is_in_history(chat_env):
    async def go():
        async with bridge_client() as c:
            async with Tab(c, "dA") as a:
                await a.ws.send_str(json.dumps({"type": "chat-send", "text": "hold готово"}))
                await until(lambda: chat.SESSIONS.get("dA") and chat.SESSIONS["dA"].busy)
            release(chat_env)                     # the window is closed now
            await until(lambda: not chat.SESSIONS["dA"].busy, what="turn ends with no window")
            async with Tab(c, "dA") as b:
                await b.ws.send_str(json.dumps({"type": "chat-load"}))
                await until(lambda: "history" in b.kinds(), what="history")
                hist = next(e for e in b.events if e["kind"] == "history")
                assert hist["busy"] is False and hist["messages"][-1]["text"] == "готово"
    run(go())


def test_bridge_restart_mid_turn_leaves_a_note():
    async def go():
        async with bridge_client() as c:
            await send(c, "dA", "hang")           # ignores stdin close: must be killed
            await until(lambda: chat.SESSIONS["dA"].sent, what="hang reached claude")
            proc = chat.SESSIONS["dA"].proc
            t0 = time.monotonic()
        assert time.monotonic() - t0 < 8
        assert proc.returncode is not None
        chat.reset()                              # a new bridge process
        async with bridge_client() as c:
            h = await history(c, "dA")
            assert h["busy"] is False
            note = system(h)
            assert [m["text"] for m in note] == [chat.tr("uk", "bridge_down")] and note[0].get("error")
            await still_works(c, "dA", 1)
    run(go())


def test_window_without_a_file_id_is_refused_not_ignored():
    async def go():
        async with bridge_client() as c:
            async with Tab(c, None) as t:
                await t.ws.send_str(json.dumps({"type": "chat-send", "text": "hi"}))
                await until(lambda: "refused" in t.kinds(), what="refused")
    run(go())


def test_a_chat_failure_does_not_drop_the_plugin_socket(monkeypatch):
    async def go():
        async def boom(doc, body):
            raise RuntimeError("bad day")
        async with bridge_client() as c:
            async with Tab(c, "dA") as t:
                monkeypatch.setattr(chat, "send", boom)
                await t.ws.send_str(json.dumps({"type": "chat-send", "text": "hi"}))
                await until(lambda: "refused" in t.kinds(), what="refused")
                await t.ws.send_str(json.dumps({"type": "chat-load"}))
                await until(lambda: "history" in t.kinds(), what="socket still serves the chat")
                assert not t.ws.closed
    run(go())


# ─── what the designer sends ──────────────────────────────────────────────

def test_too_big_or_broken_photo_is_dropped_with_a_note(monkeypatch):
    async def go():
        monkeypatch.setattr(chat, "MAX_IMAGE_BYTES", 10)
        async with bridge_client() as c:
            status, _ = await send(c, "dA", "say текст дійшов",
                                   images=[f"data:image/png;base64,{PNG}", "data:image/png;base64,@@"])
            assert status == 200
            h = await idle_with(c, "dA", 1)
            assert "images" not in h["messages"][0]
            note = system(h)
            assert len(note) == 1 and clear_uk(note[0]["text"]) and "Фото" in note[0]["text"]
            assert h["messages"][-1]["text"] == "текст дійшов"
            status, body = await send(c, "dA", "", images=[f"data:image/png;base64,{PNG}"])
            assert status == 400 and body["ok"] is False     # nothing left to send
            assert not (await history(c, "dA"))["busy"]
    run(go())


@pytest.mark.parametrize("images", [{"a": 1}, 5, "data:image/png;base64,AAAA", [1, None, {}], True])
def test_odd_images_field_never_breaks_the_chat(images):
    async def go():
        async with bridge_client() as c:
            status, body = await send(c, "dA", "say ок", images=images)
            assert status == 200 and body["ok"]
            await idle_with(c, "dA", 1)
            async with Tab(c, "dA") as t:
                await t.ws.send_str(json.dumps({"type": "chat-send", "text": "say ок", "images": images}))
                await idle_with(c, "dA", 2)
                assert not t.ws.closed
    run(go())


def test_photo_that_cannot_be_saved_is_dropped_with_a_note():
    async def go():
        chat.DATA_DIR.mkdir(parents=True)
        (chat.DATA_DIR / "uploads").write_text("a file where the folder should be")
        async with bridge_client() as c:
            status, _ = await send(c, "dA", "say без фото", images=[f"data:image/png;base64,{PNG}"])
            assert status == 200
            h = await idle_with(c, "dA", 1)
            assert len(system(h)) == 1 and "Фото" in system(h)[0]["text"]
    run(go())


# ─── files on disk ────────────────────────────────────────────────────────

def test_corrupt_history_file_starts_a_clean_chat():
    async def go():
        f = chat.DATA_DIR / "history" / "dA.json"
        f.parent.mkdir(parents=True)
        f.write_text('{"next_id": 4, "messages": [{"id"')
        async with bridge_client() as c:
            assert (await history(c, "dA"))["messages"] == []
            await still_works(c, "dA", 1)
        assert json.loads(f.read_text())["messages"]
    run(go())


def test_history_file_with_wrong_shapes_still_loads():
    async def go():
        f = chat.DATA_DIR / "history" / "dA.json"
        f.parent.mkdir(parents=True)
        f.write_text(json.dumps({"next_id": "x", "messages": [1, "a", None,
                                                              {"id": 3, "from": "me", "text": "ok", "t": 1}]}))
        async with bridge_client() as c:
            h = await history(c, "dA")
            assert [m["text"] for m in h["messages"]] == ["ok"]
            await send(c, "dA", "say далі", quote=3)
            h = await idle_with(c, "dA", 1)
            ids = [m["id"] for m in h["messages"]]
            assert len(set(ids)) == len(ids) and all(i > 3 for i in ids[1:])
    run(go())


def test_corrupt_sessions_file_starts_a_new_session(chat_env):
    async def go():
        chat.DATA_DIR.mkdir(parents=True)
        (chat.DATA_DIR / "sessions.json").write_text("[[[")
        async with bridge_client() as c:
            await still_works(c, "dA", 1)
        assert "--resume" not in starts(chat_env)[0]["argv"]
        assert json.loads((chat.DATA_DIR / "sessions.json").read_text())["dA"]
    run(go())


def test_history_that_cannot_be_saved_does_not_block_the_chat():
    async def go():
        chat.DATA_DIR.mkdir(parents=True)
        (chat.DATA_DIR / "history").write_text("a file where the folder should be")
        async with bridge_client() as c:
            await send(c, "dA", "say перша")
            h = await idle_with(c, "dA", 1)
            await send(c, "dA", "say друга")
            h = await idle_with(c, "dA", 2)
            warn = errors(h)
            assert len(warn) == 1 and clear_uk(warn[0]["text"])   # said once, not on every line
            assert h["messages"][-1]["text"] == "друга"
    run(go())
