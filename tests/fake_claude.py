#!/usr/bin/env python3
"""A stand-in for `claude -p --input-format stream-json --output-format stream-json`.

The chat tests never start the real Claude Code. The bridge is pointed at this
script instead (chat.CLAUDE_CMD), and it speaks the same line-delimited JSON:

    stdin   {"type": "user", "message": {"role": "user", "content": ...}}
            {"type": "control_request", "request_id": "...", "request": {"subtype": "interrupt"}}
    stdout  system/init, stream_event (text deltas), assistant, user (tool_result),
            control_response, result

What it does is chosen by the first word of the user's text:

    say <text>          reply with <text> verbatim (newlines and all)
    stream <text>       stream <text> word by word as deltas, then the full reply
    sleep <sec> <text>  wait <sec> seconds, then reply with <text>
    hold <text>         wait until the test drops the file `release` next to the log
                        ($FAKE_CLAUDE_LOG), then reply with <text>
    tool                one Bash tool call with a description, then "done"
    slow                say "starting", then wait (up to 30 s) for an interrupt
    hang                ignore everything, including interrupts, for 60 s
    crash               print "boom" to stderr and exit with code 3
    /compact            the real compact events: status, compact_boundary, an empty result
    anything else       reply "echo: <text>"

The init event lists slash_commands like the real one, and the first turn
also sends commands_changed with descriptions (SLASH, below).

Failure modes, shaped like the real Claude Code 2.1 output (tests/test_chat_errors.py):

    autherr             not logged in: an api error message, then an error result
    ratelimit           the subscription limit is used up
    apierr <kind> <t>   an api error of <kind> (billing_error, server_error, ...) with text <t>
    maxturns            result error_max_turns with no text
    execerr             result error_during_execution with an errors list
    garbage             broken lines and odd JSON, then reply "still ok"
    die                 exit 0 in the middle of the turn, no result, no stderr
    partial             reply "half", then print "fatal" to stderr and exit 1

`--resume gone-...` behaves like a session that no longer exists: the real CLI
prints an error result and "No conversation found" to stderr and exits 1, at once.
FAKE_CLAUDE_START_DELAY=<sec> makes the start slow.

Every start and every stdin line is appended to $FAKE_CLAUDE_LOG (JSON lines),
so a test can check the flags, the environment and what the bridge sent.
"""
import json
import os
import queue
import sys
import threading
import time
import uuid

for stream in (sys.stdout, sys.stderr):
    stream.reconfigure(encoding="utf-8")
sys.stdin.reconfigure(encoding="utf-8")

LOG = os.environ.get("FAKE_CLAUDE_LOG")


def log(rec):
    if LOG:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


args = sys.argv[1:]
session = args[args.index("--resume") + 1] if "--resume" in args else "sess-" + uuid.uuid4().hex[:12]
log({"start": True, "argv": args, "pid": os.getpid(), "cwd": os.getcwd(), "session": session,
     "env": {k: os.environ.get(k) for k in ("FIGMOSHA_TARGET", "FIGMOSHA_AGENT", "FIGMOSHA_FILE",
                                            "PATH")}})

if os.environ.get("FAKE_CLAUDE_START_DELAY"):
    time.sleep(float(os.environ["FAKE_CLAUDE_START_DELAY"]))


def out(obj):
    obj.setdefault("session_id", session)
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


LANG_NOTE = ("(Мову чату змінено", "(The chat language is now")   # chat.TEXTS lang_switched


def text_of(content):
    if isinstance(content, str):
        return content
    return "\n".join(b.get("text", "") for b in content
                     if b.get("type") == "text" and not b.get("text", "").startswith(LANG_NOTE))


def say(text):
    out({"type": "assistant", "message": {"role": "assistant",
                                          "content": [{"type": "text", "text": text}]}})


def result(subtype="success", text=""):
    out({"type": "result", "subtype": subtype, "is_error": subtype != "success", "result": text})


SLASH = [
    {"name": "clear", "description": "Start a new session", "argumentHint": "", "aliases": ["reset", "new"]},
    {"name": "compact", "description": "Free up context", "argumentHint": "<instructions>"},
    {"name": "context", "description": "Show current context usage", "argumentHint": ""},
    {"name": "review-it", "description": "A project skill", "argumentHint": "[target]"},
    {"name": "claude.ai Notion:page (MCP)", "description": "An MCP prompt with spaces"},
]


def api_error(kind, text):
    """What the real CLI prints when the API call fails (not logged in, limits, ...)."""
    out({"type": "assistant", "error": kind, "is_api_error_message": True,
         "message": {"role": "assistant", "model": "<synthetic>",
                     "content": [{"type": "text", "text": text}]}})
    out({"type": "result", "subtype": "success", "is_error": True, "result": text})


if session.startswith("gone-"):
    gone = f"No conversation found with session ID: {session}"
    out({"type": "result", "subtype": "error_during_execution", "is_error": True,
         "num_turns": 0, "errors": [gone]})
    sys.stderr.write(gone + "\n")
    sys.stderr.flush()
    sys.exit(1)

inbox = queue.Queue()
announced = []


def reader():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        log({"stdin": msg})
        inbox.put(msg)
    inbox.put(None)


threading.Thread(target=reader, daemon=True).start()
deferred = []   # user messages that arrived while a turn was running


def wait_release(seconds=10):
    """hold: wait (polling, no fixed sleep) until the test creates the file
    `release` next to the log, then take it, so the next hold waits again."""
    gate = os.path.join(os.path.dirname(os.path.abspath(LOG or "x")), "release")
    end = time.time() + seconds
    while time.time() < end:
        try:
            os.remove(gate)
            return
        except OSError:
            time.sleep(0.01)


def answer_interrupt(msg):
    out({"type": "control_response",
         "response": {"subtype": "success", "request_id": msg.get("request_id")}})


def wait_for_interrupt(seconds):
    end = time.time() + seconds
    while time.time() < end:
        try:
            msg = inbox.get(timeout=0.05)
        except queue.Empty:
            continue
        if msg is None:
            sys.exit(0)
        if msg.get("type") == "control_request" and msg["request"].get("subtype") == "interrupt":
            answer_interrupt(msg)
            return True
        deferred.append(msg)
    return False


def turn(msg):
    out({"type": "system", "subtype": "init", "cwd": os.getcwd(), "tools": ["Bash", "Read"],
         "slash_commands": [c["name"] for c in SLASH]})
    if not announced:
        announced.append(True)
        out({"type": "system", "subtype": "commands_changed", "commands": SLASH})
    text = text_of(msg["message"]["content"])
    if text.startswith("/compact"):
        out({"type": "system", "subtype": "status", "status": "compacting"})
        out({"type": "system", "subtype": "status", "status": None, "compact_result": "success"})
        out({"type": "system", "subtype": "compact_boundary",
             "compact_metadata": {"trigger": "manual", "pre_tokens": 100, "post_tokens": 10}})
        out({"type": "user", "message": {"role": "user", "content": "Summary: ..."}, "isReplay": True})
        result()
        return
    word, _, rest = text.partition(" ")
    if word == "say":
        say(rest)
    elif word == "stream":
        parts = rest.split(" ")
        for i, p in enumerate(parts):
            delta = p if i == 0 else " " + p
            out({"type": "stream_event", "event": {
                "type": "content_block_delta", "index": 0,
                "delta": {"type": "text_delta", "text": delta}}})
            time.sleep(0.02)
        say(rest)
    elif word == "sleep":
        sec, _, reply = rest.partition(" ")
        time.sleep(float(sec))
        say(reply)
    elif word == "hold":
        wait_release()
        say(rest)
    elif word == "tool":
        out({"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "toolu_1", "name": "Bash",
             "input": {"command": "dir", "description": "Перевіряю файл"}}]}})
        time.sleep(0.05)
        out({"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"}]}})
        say("done")
    elif word == "slow":
        say("starting")
        if wait_for_interrupt(30):
            result("error_during_execution")
            return
        say("finished")
    elif word == "hang":
        time.sleep(60)
    elif word == "crash":
        sys.stderr.write("boom\n")
        sys.stderr.flush()
        sys.exit(3)
    elif word == "autherr":
        return api_error("authentication_failed", "Not logged in · Please run /login")
    elif word == "ratelimit":
        return api_error("rate_limit", "You've hit your limit · resets 5pm (Europe/Kyiv)")
    elif word == "apierr":
        kind, _, t = rest.partition(" ")
        return api_error(kind, t)
    elif word == "maxturns":
        out({"type": "result", "subtype": "error_max_turns", "is_error": True})
        return
    elif word == "execerr":
        out({"type": "result", "subtype": "error_during_execution", "is_error": True,
             "errors": ["Tool runner failed"]})
        return
    elif word == "garbage":
        sys.stdout.write("this is not json\n")
        sys.stdout.write("[1, 2, 3]\n\n")
        sys.stdout.flush()
        sys.stdout.buffer.write(b"\xff\xfe broken utf-8\n")
        sys.stdout.flush()
        for odd in ({"type": "assistant", "message": "oops"},
                    {"type": "assistant", "message": {"content": ["x", 5, None]}},
                    {"type": "assistant", "message": {"content": [{"type": "text", "text": 7}]}},
                    {"type": "assistant", "message": {"content": [{"type": "tool_use", "input": "x"}]}},
                    {"type": "stream_event", "event": "x"},
                    {"type": "stream_event", "event": {"type": "content_block_delta", "delta": "x"}},
                    {"type": "user", "message": None},
                    {"type": 5}):
            out(odd)
        say("still ok")
    elif word == "die":
        sys.exit(0)
    elif word == "partial":
        say("half")
        sys.stderr.write("fatal\n")
        sys.stderr.flush()
        sys.exit(1)
    else:
        say("echo: " + text)
    result()


while True:
    msg = deferred.pop(0) if deferred else inbox.get()
    if msg is None:
        break
    if msg.get("type") == "control_request":
        answer_interrupt(msg)   # an interrupt with nothing running: acknowledge, do nothing
        continue
    if msg.get("type") == "user":
        turn(msg)
