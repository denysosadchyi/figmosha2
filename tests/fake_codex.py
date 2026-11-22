#!/usr/bin/env python3
"""A stand-in for `codex exec --json` (Codex CLI 0.155), one process per turn.

The provider tests never start the real Codex. The bridge is pointed at this
script instead (chat.CODEX_CMD = [python, fake_codex.py]). It reads the same argv:

    exec [resume] --json --skip-git-repo-check --dangerously-bypass-approvals-and-sandbox
         -m <model> -c model_reasoning_effort=<e> [-c developer_instructions=<toml>]
         [-i <image>]... [<thread_id>] -

and the prompt from stdin (read to EOF). It prints the real event lines on
stdout, one JSON object per line:

    thread.started {thread_id}, turn.started, item.started / item.completed
    (command_execution, agent_message, error, reasoning, todo_list),
    turn.completed {usage}, or error + turn.failed

What it does is chosen by the first word of the prompt:

    say <text>          reply with <text> verbatim (an agent_message)
    sleep <sec> <text>  wait <sec> seconds, then reply with <text>
    hold <text>         wait until the test drops the file `release` next to the log
                        ($FAKE_CODEX_LOG), then reply with <text>
    tool                one command_execution (started, completed), then "done"
    odd                 item types the bridge has no special case for, then "done"
    warn                a non-fatal error item, then "still ok"
    fail                error + turn.failed with model_not_found, exit 1
    die                 exit 0 after turn.started: no turn.completed, no stderr
    crash               print "boom" to stderr and exit 3
    slow                start a command, then wait up to 30 s; SIGTERM exits 0 at once
    hang                ignore SIGTERM, sleep 60 (only a kill ends it)
    wrapped             hang, with the work in a child process (like the npm wrapper)
    anything else       reply "echo: <prompt>"

`resume gone-...` is a thread that does not exist: no stdout, the real stderr
line (no rollout found ...), exit 1.
`debug models` prints {"models": [...]} like the real one ($FAKE_CODEX_MODELS
is a JSON file to print instead).

Every start (argv, cwd, env incl. PATH, model, effort, images, prompt) and every SIGTERM
is appended to $FAKE_CODEX_LOG (JSON lines).
"""
import json
import os
import signal
import sys
import time
import uuid

for stream in (sys.stdout, sys.stderr):
    stream.reconfigure(encoding="utf-8")

LOG = os.environ.get("FAKE_CODEX_LOG")

MODELS = [
    {"slug": "gpt-hidden", "display_name": "GPT Hidden", "default_reasoning_level": "medium",
     "supported_reasoning_levels": [{"effort": "low"}, {"effort": "medium"}],
     "visibility": "hide", "priority": 0},
    {"slug": "gpt-luna", "display_name": "GPT Luna", "default_reasoning_level": "low",
     "supported_reasoning_levels": [{"effort": "low"}, {"effort": "medium"}, {"effort": "high"}],
     "visibility": "list", "priority": 3},
    {"slug": "gpt-sol", "display_name": "GPT Sol", "default_reasoning_level": "medium",
     "supported_reasoning_levels": [{"effort": "low"}, {"effort": "medium"}, {"effort": "high"},
                                    {"effort": "xhigh"}, {"effort": "max"}],
     "visibility": "list", "priority": 1},
    {"slug": "gpt-terra", "display_name": "GPT Terra", "default_reasoning_level": "medium",
     "supported_reasoning_levels": [{"effort": "low"}, {"effort": "medium"}, {"effort": "high"}],
     "visibility": "list", "priority": 2},
]


def log(rec):
    if LOG:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def out(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


args = sys.argv[1:]

if args[:2] == ["debug", "models"]:
    path = os.environ.get("FAKE_CODEX_MODELS")
    if path:
        sys.stdout.write(open(path, encoding="utf-8").read())
    else:
        out({"models": MODELS})
    sys.exit(0)

if not args or args[0] != "exec":
    sys.stderr.write(f"error: unexpected argv {args!r}\n")
    sys.exit(2)

rest = args[1:]
resume = bool(rest) and rest[0] == "resume"
if resume:
    rest = rest[1:]
model, effort, config, images, positional = None, None, [], [], []
i = 0
while i < len(rest):
    a = rest[i]
    if a in ("-m", "--model", "-c", "--config", "-i", "--image") and i + 1 < len(rest):
        v = rest[i + 1]
        if a in ("-m", "--model"):
            model = v
        elif a in ("-c", "--config"):
            config.append(v)
            if v.startswith("model_reasoning_effort="):
                effort = v.split("=", 1)[1].strip('"')
        else:
            images.append({"path": v, "exists": os.path.isfile(v)})
        i += 2
        continue
    if not a.startswith("-") or a == "-":
        positional.append(a)
    i += 1

thread = positional[0] if resume and positional and positional[0] != "-" else None
prompt = sys.stdin.read() if positional and positional[-1] == "-" else ""
if not thread:
    thread = "th-" + uuid.uuid4().hex[:12]

log({"start": True, "argv": args, "pid": os.getpid(), "cwd": os.getcwd(), "resume": resume,
     "thread": thread, "model": model, "effort": effort, "config": config, "images": images,
     "prompt": prompt,
     "env": {k: os.environ.get(k) for k in ("FIGMOSHA_TARGET", "FIGMOSHA_AGENT", "FIGMOSHA_FILE",
                                            "CODEX_THREAD_ID", "CODEX_SESSION_ID", "CODEX_CI",
                                            "CLAUDECODE", "PATH")}})

if resume and thread.startswith("gone-"):
    sys.stderr.write("Error: thread/resume: thread/resume failed: no rollout found for thread id "
                     f"{thread} (code -32600)\n")
    sys.stderr.flush()
    sys.exit(1)


def on_term(signum, frame):
    log({"signal": "TERM", "pid": os.getpid()})
    os._exit(0)           # like the real one: exit 0, nothing more on stdout


signal.signal(signal.SIGTERM, on_term)


def message(text, n=1):
    out({"type": "item.completed", "item": {"id": f"item_{n}", "type": "agent_message", "text": text}})


def command(cmd, n=0, output="ok\n"):
    item = {"id": f"item_{n}", "type": "command_execution", "command": cmd,
            "aggregated_output": "", "exit_code": None, "status": "in_progress"}
    out({"type": "item.started", "item": item})
    return item


def done(item, output="ok\n"):
    item = dict(item, aggregated_output=output, exit_code=0, status="completed")
    out({"type": "item.completed", "item": item})


def completed():
    out({"type": "turn.completed", "usage": {"input_tokens": 10, "cached_input_tokens": 0,
                                             "cache_write_input_tokens": 0, "output_tokens": 2,
                                             "reasoning_output_tokens": 0}})


out({"type": "thread.started", "thread_id": thread})
word, _, tail = prompt.strip().partition(" ")


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


if word == "crash":
    sys.stderr.write("boom\n")
    sys.stderr.flush()
    sys.exit(3)

out({"type": "turn.started"})

if word == "say":
    message(tail)
elif word == "sleep":
    sec, _, reply = tail.partition(" ")
    time.sleep(float(sec))
    message(reply)
elif word == "hold":
    wait_release()
    message(tail)
elif word == "tool":
    item = command("/bin/bash -lc 'ls -la'")
    time.sleep(0.05)
    done(item)
    message("done")
elif word == "odd":
    out({"type": "item.completed", "item": {"id": "item_0", "type": "reasoning", "text": "thinking"}})
    out({"type": "item.started", "item": {"id": "item_1", "type": "todo_list", "items": []}})
    out({"type": "item.started", "item": "not a dict"})
    out({"type": "item.completed", "item": {"id": "item_2"}})
    message("done", 3)
elif word == "warn":
    out({"type": "item.completed", "item": {"id": "item_0", "type": "error",
                                            "message": "Model metadata for `x` not found."}})
    message("still ok")
elif word == "fail":
    raw = json.dumps({"type": "error", "status": 400, "error": {
        "type": "invalid_request_error", "code": "model_not_found",
        "message": f"The '{model}' model is not supported when using Codex with a ChatGPT account."}})
    out({"type": "error", "message": raw})
    out({"type": "turn.failed", "error": {"message": raw}})
    sys.exit(1)
elif word == "die":
    sys.exit(0)
elif word == "slow":
    command("/bin/bash -lc 'sleep 40'")
    time.sleep(30)
    message("finished")
elif word == "hang":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    log({"ignoring": "TERM", "pid": os.getpid()})
    command("/bin/bash -lc 'sleep 60'")
    time.sleep(60)
    sys.exit(0)
elif word == "wrapped":
    # Like the npm wrapper (node codex.js): the work runs in a child, and a kill of
    # the parent alone leaves that child running.
    import subprocess
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    kid = subprocess.Popen([sys.executable, "-c",
                            "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"])
    log({"ignoring": "TERM", "pid": os.getpid(), "kid": kid.pid})
    command("/bin/bash -lc 'sleep 60'")
    time.sleep(60)
    sys.exit(0)
else:
    message("echo: " + prompt.strip())

completed()
