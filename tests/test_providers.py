"""Chat providers: Claude Code or Codex, and the model and effort, from the settings.

Written before the code (TDD). Nothing here starts a real agent: chat.CLAUDE_CMD
points at tests/fake_claude.py and chat.CODEX_CMD at tests/fake_codex.py.

    pytest -q tests/test_providers.py

Contract (the builder implements to this; docs/CHAT.md gets the same text):

chat.py module
    CODEX_CMD        default (os.environ FIGMOSHA_CODEX or "codex").split(). Like CLAUDE_CMD:
                     a command that is not the bare default is used as is (found when its
                     argv[0] is on PATH or is a file), and no other place is searched.
    BREW_DIRS        ("/opt/homebrew/bin", "/usr/local/bin"): the Homebrew folders. A bridge
                     started on macOS from the Dock or launchd has PATH /usr/bin:/bin:/usr/sbin:/sbin
                     only, so these and the home npm folders are searched by hand.
    home npm folders home/.npm-global/bin, home/.volta/bin, then
                     home/.nvm/versions/node/<vX.Y.Z>/bin of the newest node by numeric version
                     (the "v" dropped; a version folder without the exe is skipped; a name that
                     is not a version is ignored).
    find_claude(env=None, home=None) -> argv list | None
                     env defaults to os.environ, home to Path.home(). Order for the bare
                     "claude": shutil.which on env["PATH"], home/.local/bin/claude(.exe), each
                     BREW_DIRS folder in order, the home npm folders, then the copy inside the
                     Claude desktop app: on Windows
                     <env LOCALAPPDATA>/Claude-Data/claude_*/claude-code/<version>/<hash>/claude.exe
                     (searched whenever LOCALAPPDATA is set), on macOS
                     home/Library/Application Support/Claude/claude-code/<version>/claude.app/
                     Contents/MacOS/claude or home/.../claude-code/<version>/claude (searched on
                     every platform, LOCALAPPDATA not needed). Searched on every call, newest
                     version by numeric tuple; a version folder with no exe is skipped.
                     Only a regular file counts, not a folder of that name. A found file is
                     [path] + the extra args of CLAUDE_CMD.
    find_codex(env=None, home=None) -> argv list | None
                     bare "codex": shutil.which on env["PATH"] (an npm .cmd shim there runs what
                     it runs, as below), home/.local/bin/codex, each BREW_DIRS folder in order,
                     the home npm folders, then under <env APPDATA>/npm:
                     the native node_modules/@openai/codex/node_modules/@openai/codex-win32-*/
                     vendor/*/bin/codex.exe first, then node + codex.js, then
                     [COMSPEC or cmd.exe, "/c", <APPDATA>/npm/codex.cmd]. Only a regular file
                     counts. A found file is [path] + the extra args of CODEX_CMD.
                     A custom CLAUDE_CMD / CODEX_CMD searches none of these places.
    _path_with(env, exe) -> a new env dict
                     PATH with the folder of exe appended (os.pathsep) when it is not already one
                     of its entries, else an unchanged copy. An empty or missing PATH becomes
                     just that folder (no empty entry). On Windows (os.name "nt") always an
                     unchanged copy. The agent child env (claude and codex) goes through it with
                     the found exe, so an npm agent with a "#!/usr/bin/env node" line finds the
                     node that sits next to it in a Homebrew or nvm folder.
    detect_providers(env=None, home=None) -> [{id, name, path}]
                     id "claude" (name "Claude Code") then "codex" (name "Codex"), found ones only.
                     It probes on every call. The bridge keeps its own copy of the result: it
                     detects at start (first use after reset) and on a recheck, and uses that
                     copy until then. reset() forgets the copy. home and the codex config are
                     read at call time (Path.home(), CODEX_HOME), never cached at import.
    _sessions(provider="claude"), _save_session_id(doc, sid, provider="claude"):
                     claude keeps DATA_DIR/sessions.json {doc: sid} (the old shape, unchanged);
                     codex keeps DATA_DIR/sessions-codex.json {doc: thread_id}.
    DATA_DIR/settings.json  {provider, model, effort, ...}: one for the whole bridge.
                     When it is missing, the first read picks the default and writes it:
                     only one provider found -> that one; else CODEX_THREAD_ID in the bridge
                     env -> codex; else claude (CLAUDECODE / CLAUDE_CODE_ENTRYPOINT, or no marker).
                     No file is written while no provider is found. When the saved provider is
                     not found any more, the first found provider is used with its default
                     model and medium (and saved).
                     Defaults: claude -> opus + medium; codex -> `model` from
                     ~/.codex/config.toml (CODEX_HOME when set), else the first listed model,
                     effort medium.
    Models: claude from a list in chat.py (ids include opus, sonnet, haiku; efforts low, medium,
                     high, xhigh, max). codex from `CODEX_CMD debug models` ({"models": [...]}),
                     falling back to ~/.codex/models_cache.json; only visibility "list",
                     sorted by priority; id = slug, name = display_name,
                     efforts = supported_reasoning_levels[].effort.
                     Model item: {id, name, efforts: [str], default_effort}.
    TEXTS keys (uk, en): "switched" (provider=<provider name>, model=<model name>),
                     "no_provider", "codex_refused" (cmd=<name>); "locked" now points to the
                     settings; "lost" no longer names Claude (it is shown for Codex too).
                     No dashes, no quotes.

HTTP (bridge guard: web pages get 403)
    GET  /chat/settings        -> {ok, providers, settings: {provider, model, effort},
                                   models: {<provider id>: [model item]}}  (found providers only)
    POST /chat/settings {provider?, model?, effort?} -> 200 same shape; 400 {ok: false, error}
                                   for an unknown or missing provider, a model not in that
                                   provider's list, an effort that model does not take, or a
                                   wrong type. A provider change without model takes that
                                   provider's default model and effort medium. Nothing changes
                                   on 400.
    POST /chat/recheck         -> detect again, then the same shape as GET /chat/settings.
    GET  /chat?doc=            -> also has providers, settings, models. `session` is the id of
                                   the current provider for that file. `commands` for codex is
                                   only clear (with aliases reset, new).

Plugin socket
    {type: "chat-settings"}                          -> {kind: "settings", providers, settings, models}
    {type: "chat-settings-set", provider?, model?, effort?}
    {type: "chat-recheck"}
        Each is a chat event like the others: {type: "chat", doc: <that tab's doc>, event}.
        A good set or a recheck sends that settings event to every open plugin tab.
        A bad set: only the asking tab gets the settings event (unchanged) with an `error` field.
    The chat-load history event also has providers, settings and models.

Sessions and turns
    One session per (file, provider). A settings change applies from the next turn of each file.
    Before that turn the file gets a system note (no error flag, after my message, before
    the reply)
    tr(lang, "switched", provider=..., model=...) when its last turn ran on another provider or
    model. Switching back resumes that provider's saved session. Agent replies are stored with
    from "claude" whatever the provider. Claude gets --model/--effort from the settings; a change
    restarts it with --resume.
    Codex: one process per turn, cwd WORKDIR, same FIGMOSHA_* env, CODEX_THREAD_ID,
    CODEX_SESSION_ID, CODEX_CI and CLAUDECODE removed.
        new:    CODEX_CMD exec --json --skip-git-repo-check --dangerously-bypass-approvals-and-sandbox
                -m <model> -c model_reasoning_effort=<e> -c developer_instructions=<toml str>
                [-i <upload path>]... -
        resume: CODEX_CMD exec resume <same flags> [-i ...] <thread_id> -
        The prompt (quote prefix + text) goes to stdin, then stdin is closed.
        Events: agent_message -> a message (parse_reply), no delta; command_execution started ->
        activity with the command; any other item type -> activity naming the type; error item ->
        nothing; turn.completed -> end; turn.failed or exit without turn.completed (not stopping)
        -> error note; stderr "no rollout found" -> the lost note and a new thread.
        Stop: terminate, kill after STOP_GRACE (the process is gone after it); the turn ends
        with no chat line. SESSIONS[doc].busy works for codex as for claude.
        Slash: /clear (/reset, /new) drops the codex thread of that file; /model, /effort, /fast get
        "locked"; terminal ones get "terminal"; any other command gets "codex_refused" and is not sent.
    No provider at all: providers [], a send stores my message and adds an error note
    tr(lang, "no_provider"); nothing is started.
"""

import asyncio
import json
import os
import re
import stat
import sys
import time
from pathlib import Path

import pytest

import chat
from test_chat import (EVIL, FAKE, PLUGIN, PNG, Tab, bridge_client, chat_env,  # noqa: F401
                       errored, errors, history, idle_with, idle_with_system, run, send,
                       release, starts, system, until)

FAKE_CODEX = Path(__file__).resolve().parent / "fake_codex.py"
MARKERS = ("CODEX_THREAD_ID", "CODEX_SESSION_ID", "CODEX_CI", "CODEX_HOME", "CLAUDECODE",
           "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ID", "AI_AGENT")
MISSING = ["definitely-not-an-agent-xyz"]
DASHES_QUOTES = re.compile(r"[—–\"«»„“”]")      # the set test_chat uses; the uk apostrophe is fine
posix_only = pytest.mark.skipif(os.name == "nt", reason="PATH lookup needs the exec bit")


@pytest.fixture(autouse=True)
def prov_env(chat_env, monkeypatch):
    """chat_env, plus the fake codex, a temp home and no agent markers in the env."""
    home = chat_env / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    for k in MARKERS + ("FAKE_CODEX_MODELS", "LOCALAPPDATA", "APPDATA"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("FAKE_CODEX_LOG", str(chat_env / "codex.jsonl"))
    monkeypatch.setattr(chat, "CODEX_CMD", [sys.executable, str(FAKE_CODEX)], raising=False)
    monkeypatch.setattr(chat, "BREW_DIRS", (), raising=False)    # never the real /opt/homebrew
    chat.reset()
    yield chat_env
    chat.reset()


def codex_log(tmp_path):
    f = tmp_path / "codex.jsonl"
    if not f.exists():
        return []
    return [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines() if x.strip()]


def codex_starts(tmp_path):
    return [r for r in codex_log(tmp_path) if r.get("start")]


def flag(argv, name):
    """Every value after `name` in argv."""
    return [argv[i + 1] for i, a in enumerate(argv[:-1]) if a == name]


async def get_settings(c):
    r = await c.get("/chat/settings", headers=PLUGIN)
    return r.status, await r.json()


async def set_settings(c, **body):
    r = await c.post("/chat/settings", json=body, headers=PLUGIN)
    return r.status, await r.json()


async def use_codex(c, **extra):
    status, out = await set_settings(c, provider="codex", **extra)
    assert status == 200, out
    return out


def model_name(st, provider, model):
    return next(m["name"] for m in st["models"][provider] if m["id"] == model)


def exe(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


# ─── detection ────────────────────────────────────────────────────────────

@posix_only
def test_claude_on_the_path_is_found(tmp_path, monkeypatch):
    monkeypatch.setattr(chat, "CLAUDE_CMD", ["claude"])
    claude = exe(tmp_path / "bin" / "claude")
    found = chat.find_claude(env={"PATH": str(tmp_path / "bin")}, home=tmp_path / "nohome")
    assert found and Path(found[0]) == claude


def test_claude_from_the_native_installer_is_found(tmp_path, monkeypatch):
    monkeypatch.setattr(chat, "CLAUDE_CMD", ["claude"])
    home = tmp_path / "h"
    claude = exe(home / ".local" / "bin" / "claude")
    found = chat.find_claude(env={"PATH": str(tmp_path / "empty")}, home=home)
    assert found and Path(found[0]) == claude


def test_claude_inside_the_desktop_app_newest_version_wins(tmp_path, monkeypatch):
    monkeypatch.setattr(chat, "CLAUDE_CMD", ["claude"])
    base = tmp_path / "Local" / "Claude-Data" / "claude_pzs8sxrjxfjjc" / "claude-code"
    for ver, h in (("2.1.328", "23e64dd91c09"), ("2.1.330", "9424831ad51c"), ("2.1.99", "aa11")):
        exe(base / ver / h / "claude.exe")
    (base / "2.1.900" / "nohash").mkdir(parents=True)         # an update in progress: no exe yet
    env = {"PATH": str(tmp_path / "empty"), "LOCALAPPDATA": str(tmp_path / "Local")}
    found = chat.find_claude(env=env, home=tmp_path / "nohome")
    assert found and Path(found[0]) == base / "2.1.330" / "9424831ad51c" / "claude.exe"
    newer = exe(base / "2.1.1000" / "bb22" / "claude.exe")    # the app updated itself
    assert Path(chat.find_claude(env=env, home=tmp_path / "nohome")[0]) == newer


def test_cli_claude_wins_over_the_app_copy(tmp_path, monkeypatch):
    monkeypatch.setattr(chat, "CLAUDE_CMD", ["claude"])
    home = tmp_path / "h"
    cli = exe(home / ".local" / "bin" / "claude")
    exe(tmp_path / "Local" / "Claude-Data" / "claude_x" / "claude-code" / "2.1.330" / "h1" / "claude.exe")
    env = {"PATH": str(tmp_path / "empty"), "LOCALAPPDATA": str(tmp_path / "Local")}
    assert Path(chat.find_claude(env=env, home=home)[0]) == cli


def test_an_explicit_command_is_used_as_is(tmp_path, monkeypatch):
    monkeypatch.setattr(chat, "CLAUDE_CMD", [sys.executable, str(FAKE)])
    assert chat.find_claude(env={"PATH": ""}, home=tmp_path) == [sys.executable, str(FAKE)]
    monkeypatch.setattr(chat, "CODEX_CMD", [sys.executable, str(FAKE_CODEX)])
    assert chat.find_codex(env={"PATH": ""}, home=tmp_path) == [sys.executable, str(FAKE_CODEX)]
    exe(tmp_path / ".local" / "bin" / "claude")               # not looked at for a custom command
    monkeypatch.setattr(chat, "CLAUDE_CMD", MISSING)
    monkeypatch.setattr(chat, "CODEX_CMD", MISSING)
    assert chat.find_claude(env={"PATH": ""}, home=tmp_path) is None
    assert chat.find_codex(env={"PATH": ""}, home=tmp_path) is None


@posix_only
def test_codex_on_the_path_is_found(tmp_path, monkeypatch):
    monkeypatch.setattr(chat, "CODEX_CMD", ["codex"])
    codex = exe(tmp_path / "bin" / "codex")
    found = chat.find_codex(env={"PATH": str(tmp_path / "bin")}, home=tmp_path / "nohome")
    assert found and Path(found[0]) == codex


def test_codex_from_npm_prefers_the_native_binary_over_the_cmd(tmp_path, monkeypatch):
    monkeypatch.setattr(chat, "CODEX_CMD", ["codex"])
    npm = tmp_path / "Roaming" / "npm"
    exe(npm / "codex.cmd")
    env = {"PATH": str(tmp_path / "empty"), "APPDATA": str(tmp_path / "Roaming")}
    found = chat.find_codex(env=env, home=tmp_path / "nohome")
    assert found and found[-1].endswith("codex.cmd")          # the last choice: through cmd /c
    assert "/c" in [a.lower() for a in found]
    native = exe(npm / "node_modules" / "@openai" / "codex" / "node_modules" / "@openai"
                 / "codex-win32-x64" / "vendor" / "x86_64-pc-windows-msvc" / "bin" / "codex.exe")
    assert chat.find_codex(env=env, home=tmp_path / "nohome") == [str(native)]


def test_the_npm_shim_on_the_path_runs_the_native_binary(tmp_path, monkeypatch):
    # Windows PATH finds codex.cmd first. A batch file mangles the quoted arguments.
    monkeypatch.setattr(chat, "CODEX_CMD", ["codex"])
    npm = tmp_path / "npm"
    shim = exe(npm / "codex.cmd")
    monkeypatch.setattr(chat, "_which", lambda cmd, env: str(shim) if cmd == "codex" else None)
    native = exe(npm / "node_modules" / "@openai" / "codex" / "node_modules" / "@openai"
                 / "codex-win32-x64" / "vendor" / "x86_64-pc-windows-msvc" / "bin" / "codex.exe")
    assert chat.find_codex(env={"PATH": str(npm)}, home=tmp_path) == [str(native)]


def test_detect_lists_found_providers_in_order(tmp_path, monkeypatch):
    monkeypatch.setattr(chat, "CLAUDE_CMD", [sys.executable, str(FAKE)])
    found = chat.detect_providers(env={"PATH": ""}, home=tmp_path)
    assert [p["id"] for p in found] == ["claude", "codex"]
    assert all(p["name"] and p["path"] for p in found)
    monkeypatch.setattr(chat, "CLAUDE_CMD", MISSING)
    assert [p["id"] for p in chat.detect_providers(env={"PATH": ""}, home=tmp_path)] == ["codex"]
    monkeypatch.setattr(chat, "CODEX_CMD", MISSING)
    assert chat.detect_providers(env={"PATH": ""}, home=tmp_path) == []


def npm_dirs(home):
    """The home npm folders in search order: .npm-global, .volta, nvm (one node version)."""
    return [home / ".npm-global" / "bin", home / ".volta" / "bin",
            home / ".nvm" / "versions" / "node" / "v20.11.1" / "bin"]


def mac_app(home, version, plain=False):
    base = home / "Library" / "Application Support" / "Claude" / "claude-code" / version
    return base / "claude" if plain else base / "claude.app" / "Contents" / "MacOS" / "claude"


def find(name, home, path=None, **env):
    """find_claude / find_codex with a PATH that holds nothing unless given."""
    e = {"PATH": str(path or home.parent / "empty")}
    e.update(env)
    return (chat.find_claude if name == "claude" else chat.find_codex)(env=e, home=home)


@pytest.mark.parametrize("name", ["claude", "codex"])
def test_an_agent_in_a_brew_dir_is_found_first_dir_first(tmp_path, monkeypatch, name):
    monkeypatch.setattr(chat, "CLAUDE_CMD", ["claude"])
    monkeypatch.setattr(chat, "CODEX_CMD", ["codex"])
    first, second = tmp_path / "opt" / "homebrew" / "bin", tmp_path / "usr" / "local" / "bin"
    monkeypatch.setattr(chat, "BREW_DIRS", (str(first), str(second)), raising=False)
    home = tmp_path / "h"
    assert find(name, home) is None
    late = exe(second / name)
    found = find(name, home)
    assert found and Path(found[0]) == late
    early = exe(first / name)
    assert Path(find(name, home)[0]) == early


@pytest.mark.parametrize("name", ["claude", "codex"])
def test_an_agent_in_the_home_npm_dirs_is_found_in_order(tmp_path, monkeypatch, name):
    monkeypatch.setattr(chat, "CLAUDE_CMD", ["claude"])
    monkeypatch.setattr(chat, "CODEX_CMD", ["codex"])
    home = tmp_path / "h"
    npm_global, volta, nvm = npm_dirs(home)
    found = exe(nvm / name)
    assert Path(find(name, home)[0]) == found
    found = exe(volta / name)
    assert Path(find(name, home)[0]) == found
    found = exe(npm_global / name)
    assert Path(find(name, home)[0]) == found


@pytest.mark.parametrize("name", ["claude", "codex"])
def test_the_newest_nvm_node_wins_by_number(tmp_path, monkeypatch, name):
    monkeypatch.setattr(chat, "CLAUDE_CMD", ["claude"])
    monkeypatch.setattr(chat, "CODEX_CMD", ["codex"])
    home = tmp_path / "h"
    node = home / ".nvm" / "versions" / "node"
    for ver in ("v9.0.0", "v18.20.4", "v20.9.0", "v20.11.1"):   # v9, v20.9 win as text, not as numbers
        exe(node / ver / "bin" / name)
    (node / "v22.0.0" / "bin").mkdir(parents=True)        # a newer node without the agent
    exe(node / "system" / "bin" / name)                   # not a version: ignored
    found = find(name, home)
    assert found and Path(found[0]) == node / "v20.11.1" / "bin" / name


def test_claude_inside_the_mac_app_newest_version_wins(tmp_path, monkeypatch):
    monkeypatch.setattr(chat, "CLAUDE_CMD", ["claude"])
    home = tmp_path / "h"
    exe(mac_app(home, "2.1.99"))
    exe(mac_app(home, "2.1.328"))
    exe(mac_app(home, "2.1.330", plain=True))             # the plain shape counts too
    (mac_app(home, "2.1.900").parent).mkdir(parents=True)  # an update in progress: no exe yet
    found = find("claude", home)                          # no LOCALAPPDATA, any sys.platform
    assert found and Path(found[0]) == mac_app(home, "2.1.330", plain=True)
    newer = exe(mac_app(home, "2.1.1000"))                # the app updated itself
    assert Path(find("claude", home)[0]) == newer


@posix_only
def test_earlier_claude_sources_win(tmp_path, monkeypatch):
    monkeypatch.setattr(chat, "CLAUDE_CMD", ["claude"])
    home = tmp_path / "h"
    brew1, brew2 = tmp_path / "brew1", tmp_path / "brew2"
    monkeypatch.setattr(chat, "BREW_DIRS", (str(brew1), str(brew2)), raising=False)
    order = [exe(tmp_path / "bin" / "claude"), exe(home / ".local" / "bin" / "claude"),
             exe(brew1 / "claude"), exe(brew2 / "claude")]
    order += [exe(d / "claude") for d in npm_dirs(home)]
    order.append(exe(mac_app(home, "2.1.330")))
    for want in order:
        found = find("claude", home, path=tmp_path / "bin")
        assert found and Path(found[0]) == want
        want.unlink()
    assert find("claude", home, path=tmp_path / "bin") is None


@posix_only
def test_earlier_codex_sources_win(tmp_path, monkeypatch):
    monkeypatch.setattr(chat, "CODEX_CMD", ["codex"])
    home = tmp_path / "h"
    brew1, brew2 = tmp_path / "brew1", tmp_path / "brew2"
    monkeypatch.setattr(chat, "BREW_DIRS", (str(brew1), str(brew2)), raising=False)
    order = [exe(tmp_path / "bin" / "codex"), exe(home / ".local" / "bin" / "codex"),
             exe(brew1 / "codex"), exe(brew2 / "codex")]
    order += [exe(d / "codex") for d in npm_dirs(home)]
    appdata = {"APPDATA": str(tmp_path / "Roaming")}
    native = exe(tmp_path / "Roaming" / "npm" / "node_modules" / "@openai" / "codex" / "node_modules"
                 / "@openai" / "codex-win32-x64" / "vendor" / "x86_64-pc-windows-msvc" / "bin"
                 / "codex.exe")
    for want in order + [native]:
        found = find("codex", home, path=tmp_path / "bin", **appdata)
        assert found and Path(found[0]) == want
        want.unlink()


@pytest.mark.parametrize("name", ["claude", "codex"])
def test_a_folder_named_like_the_agent_does_not_count(tmp_path, monkeypatch, name):
    monkeypatch.setattr(chat, "CLAUDE_CMD", ["claude"])
    monkeypatch.setattr(chat, "CODEX_CMD", ["codex"])
    home = tmp_path / "h"
    brew = tmp_path / "brew"
    monkeypatch.setattr(chat, "BREW_DIRS", (str(brew),), raising=False)
    dirs = [home / ".local" / "bin", brew] + npm_dirs(home)
    for d in dirs:
        (d / name).mkdir(parents=True)
    if name == "claude":
        mac_app(home, "2.1.330").mkdir(parents=True)
        mac_app(home, "2.1.331", plain=True).mkdir(parents=True)
    assert find(name, home) is None


@pytest.mark.parametrize("name", ["claude", "codex"])
def test_a_found_agent_keeps_the_extra_args(tmp_path, monkeypatch, name):
    monkeypatch.setattr(chat, "CLAUDE_CMD", ["claude", "--extra", "1"])
    monkeypatch.setattr(chat, "CODEX_CMD", ["codex", "--extra", "1"])
    brew = tmp_path / "brew"
    monkeypatch.setattr(chat, "BREW_DIRS", (str(brew),), raising=False)
    found_exe = exe(brew / name)
    found = find(name, tmp_path / "h")
    assert found and Path(found[0]) == found_exe and found[1:] == ["--extra", "1"]


def test_a_custom_command_searches_no_brew_or_npm_dirs(tmp_path, monkeypatch):
    home = tmp_path / "h"
    brew = tmp_path / "brew"
    monkeypatch.setattr(chat, "BREW_DIRS", (str(brew),), raising=False)
    for name in ("claude", "codex"):
        exe(brew / name)
        for d in npm_dirs(home):
            exe(d / name)
    exe(mac_app(home, "2.1.330"))
    monkeypatch.setattr(chat, "CLAUDE_CMD", MISSING)
    monkeypatch.setattr(chat, "CODEX_CMD", MISSING)
    assert find("claude", home) is None
    assert find("codex", home) is None


@posix_only
def test_path_with_appends_the_agent_folder(tmp_path):
    brew = tmp_path / "brew"
    env = {"PATH": os.pathsep.join(["/usr/bin", "/bin"]), "KEEP": "1"}
    out = chat._path_with(env, str(brew / "codex"))
    assert out is not env and env["PATH"] == os.pathsep.join(["/usr/bin", "/bin"])
    assert out["PATH"].split(os.pathsep) == ["/usr/bin", "/bin", str(brew)]
    assert out["KEEP"] == "1"
    again = chat._path_with(out, str(brew / "node"))      # already there: not added twice
    assert again is not out and again == out


@posix_only
def test_path_with_leaves_a_folder_already_on_path(tmp_path):
    brew = tmp_path / "brew"
    env = {"PATH": os.pathsep.join([str(brew), "/bin"])}
    out = chat._path_with(env, str(brew / "claude"))
    assert out is not env and out == env


@posix_only
def test_path_with_an_empty_or_missing_path_is_just_the_folder(tmp_path):
    brew = tmp_path / "brew"
    for env in ({}, {"PATH": ""}):
        out = chat._path_with(env, str(brew / "codex"))
        assert out["PATH"].split(os.pathsep) == [str(brew)]


@pytest.mark.skipif(os.name != "nt", reason="the Windows case")
def test_path_with_changes_nothing_on_windows(tmp_path):
    env = {"PATH": r"C:\Windows", "KEEP": "1"}
    out = chat._path_with(env, str(tmp_path / "npm" / "codex.exe"))
    assert out is not env and out == env


@posix_only
def test_a_brew_codex_child_gets_its_folder_on_path(chat_env, monkeypatch):
    # The bridge on macOS starts with a short PATH. npm codex is "#!/usr/bin/env node",
    # and node sits next to it in the Homebrew folder, so the child needs that folder.
    brew = chat_env / "brew"
    brew.mkdir()
    wrapper = brew / "codex"
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_CODEX}" "$@"\n', encoding="utf-8")
    wrapper.chmod(0o755)
    short = chat_env / "short-bin"
    short.mkdir()
    monkeypatch.setenv("PATH", str(short))
    monkeypatch.setattr(chat, "BREW_DIRS", (str(brew),), raising=False)
    monkeypatch.setattr(chat, "CODEX_CMD", ["codex"], raising=False)
    chat.reset()

    async def go():
        async with bridge_client() as c:
            await use_codex(c)
            await send(c, "dA", "say hi")
            h = await idle_with(c, "dA", 1)
            assert h["messages"][-1]["text"] == "hi"
        path = codex_starts(chat_env)[0]["env"]["PATH"].split(os.pathsep)
        assert str(brew) in path and str(short) in path      # the bridge PATH is kept, first
        assert path.index(str(short)) < path.index(str(brew))
    run(go())


@posix_only
def test_a_brew_claude_child_gets_its_folder_on_path(chat_env, monkeypatch):
    # The same for claude: Session builds its env apart from the codex one.
    brew = chat_env / "brew"
    brew.mkdir()
    wrapper = brew / "claude"
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n', encoding="utf-8")
    wrapper.chmod(0o755)
    short = chat_env / "short-bin"
    short.mkdir()
    monkeypatch.setenv("PATH", str(short))
    monkeypatch.setattr(chat, "BREW_DIRS", (str(brew),), raising=False)
    monkeypatch.setattr(chat, "CLAUDE_CMD", ["claude"])
    chat.reset()

    async def go():
        async with bridge_client() as c:
            await send(c, "dA", "say hi")
            h = await idle_with(c, "dA", 1)
            assert h["messages"][-1]["text"] == "hi"
        path = starts(chat_env)[0]["env"]["PATH"].split(os.pathsep)
        assert str(brew) in path and str(short) in path
        assert path.index(str(short)) < path.index(str(brew))
    run(go())


# ─── default settings: who installed Figmosha ─────────────────────────────

@pytest.mark.parametrize("marker,claude,codex,expected", [
    ({"CODEX_THREAD_ID": "t-1", "CODEX_CI": "1"}, True, True, "codex"),
    ({"CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli"}, True, True, "claude"),
    ({}, True, True, "claude"),
    ({"CODEX_THREAD_ID": "t-1"}, True, False, "claude"),      # only one found: that one
    ({"CLAUDECODE": "1"}, False, True, "codex"),
])
def test_default_provider_follows_the_installer(monkeypatch, marker, claude, codex, expected):
    for k, v in marker.items():
        monkeypatch.setenv(k, v)
    if not claude:
        monkeypatch.setattr(chat, "CLAUDE_CMD", MISSING)
    if not codex:
        monkeypatch.setattr(chat, "CODEX_CMD", MISSING)

    async def go():
        async with bridge_client() as c:
            status, st = await get_settings(c)
            assert status == 200 and st["ok"] is True
            assert st["settings"]["provider"] == expected
            assert st["settings"]["effort"] == "medium"
            assert st["settings"]["model"] == ("opus" if expected == "claude" else "gpt-sol")
    run(go())


def test_the_installer_choice_is_recorded_once(monkeypatch):
    monkeypatch.setenv("CODEX_THREAD_ID", "t-1")

    async def go():
        async with bridge_client() as c:
            assert (await get_settings(c))[1]["settings"]["provider"] == "codex"
        saved = json.loads((chat.DATA_DIR / "settings.json").read_text(encoding="utf-8"))
        assert saved["provider"] == "codex"
        monkeypatch.delenv("CODEX_THREAD_ID")
        monkeypatch.setenv("CLAUDECODE", "1")      # a later start from Claude Code changes nothing
        chat.reset()
        async with bridge_client() as c:
            assert (await get_settings(c))[1]["settings"]["provider"] == "codex"
    run(go())


def test_a_saved_provider_that_is_gone_falls_back_to_one_that_is_found(chat_env, monkeypatch):
    chat.DATA_DIR.mkdir(parents=True, exist_ok=True)
    chat._write_json(chat.DATA_DIR / "settings.json",
                     {"provider": "codex", "model": "gpt-terra", "effort": "high"})
    monkeypatch.setattr(chat, "CODEX_CMD", MISSING)           # Codex was removed

    async def go():
        async with bridge_client() as c:
            _, st = await get_settings(c)
            assert st["settings"] == {"provider": "claude", "model": "opus", "effort": "medium"}
            await send(c, "dA", "say ok")
            h = await idle_with(c, "dA", 1)
            assert h["messages"][-1]["text"] == "ok"
            monkeypatch.setattr(chat, "CODEX_CMD", [sys.executable, str(FAKE_CODEX)])
            r = await c.post("/chat/recheck", headers=PLUGIN)     # Codex is back: so is the pick
            assert (await r.json())["settings"]["provider"] == "codex"
        assert len(starts(chat_env)) == 1
    run(go())


def test_codex_default_model_comes_from_its_config(monkeypatch):
    (Path.home() / ".codex").mkdir()
    (Path.home() / ".codex" / "config.toml").write_text('model = "gpt-terra"\n', encoding="utf-8")

    async def go():
        async with bridge_client() as c:
            st = await use_codex(c)
            assert st["settings"] == {"provider": "codex", "model": "gpt-terra", "effort": "medium"}
    run(go())


def test_codex_default_model_without_config_is_the_first_listed():
    async def go():
        async with bridge_client() as c:
            st = await use_codex(c)
            assert st["settings"]["model"] == st["models"]["codex"][0]["id"] == "gpt-sol"
    run(go())


# ─── models ───────────────────────────────────────────────────────────────

def test_models_come_from_the_bridge_per_provider():
    async def go():
        async with bridge_client() as c:
            _, st = await get_settings(c)
            claude = {m["id"]: m for m in st["models"]["claude"]}
            assert {"opus", "sonnet", "haiku"} <= set(claude)
            assert {"low", "medium", "high", "xhigh", "max"} <= set(claude["opus"]["efforts"])
            codex = st["models"]["codex"]
            assert [m["id"] for m in codex] == ["gpt-sol", "gpt-terra", "gpt-luna"]   # by priority
            assert "gpt-hidden" not in [m["id"] for m in codex]
            luna = codex[-1]
            assert luna["name"] == "GPT Luna" and luna["efforts"] == ["low", "medium", "high"]
            assert luna["default_effort"] == "low"
            assert all(m["name"] and m["efforts"] for m in st["models"]["claude"] + codex)
    run(go())


def test_codex_models_fall_back_to_its_cache_file(chat_env, monkeypatch):
    bad = chat_env / "bad-models.json"
    bad.write_text("not json", encoding="utf-8")
    monkeypatch.setenv("FAKE_CODEX_MODELS", str(bad))           # `codex debug models` gives junk
    (Path.home() / ".codex").mkdir()
    (Path.home() / ".codex" / "models_cache.json").write_text(json.dumps({"models": [
        {"slug": "cached-b", "display_name": "B", "supported_reasoning_levels": [{"effort": "medium"}],
         "visibility": "list", "priority": 2, "default_reasoning_level": "medium"},
        {"slug": "cached-a", "display_name": "A", "supported_reasoning_levels": [{"effort": "medium"}],
         "visibility": "list", "priority": 1, "default_reasoning_level": "medium"},
    ]}), encoding="utf-8")

    async def go():
        async with bridge_client() as c:
            _, st = await get_settings(c)
            assert [m["id"] for m in st["models"]["codex"]] == ["cached-a", "cached-b"]
    run(go())


# ─── settings routes ──────────────────────────────────────────────────────

def test_settings_are_saved_and_survive_a_restart():
    async def go():
        async with bridge_client() as c:
            status, st = await set_settings(c, provider="codex", model="gpt-luna", effort="high")
            assert status == 200 and st["settings"] == {"provider": "codex", "model": "gpt-luna",
                                                        "effort": "high"}
            assert (await get_settings(c))[1]["settings"] == st["settings"]
        chat.reset()
        async with bridge_client() as c:
            assert (await get_settings(c))[1]["settings"]["model"] == "gpt-luna"
    run(go())


def test_effort_alone_keeps_the_provider_and_model():
    async def go():
        async with bridge_client() as c:
            status, st = await set_settings(c, effort="high")
            assert status == 200
            assert st["settings"] == {"provider": "claude", "model": "opus", "effort": "high"}
    run(go())


def test_provider_change_takes_its_default_model_and_medium():
    async def go():
        async with bridge_client() as c:
            await set_settings(c, model="sonnet", effort="max")
            st = await use_codex(c)
            assert st["settings"]["model"] == "gpt-sol" and st["settings"]["effort"] == "medium"
            _, st = await set_settings(c, provider="claude")
            assert st["settings"] == {"provider": "claude", "model": "opus", "effort": "medium"}
    run(go())


@pytest.mark.parametrize("body", [
    {"provider": "gpt"},
    {"provider": 5},
    {"model": "nope"},
    {"model": "gpt-sol"},                          # a codex model for claude
    {"provider": "codex", "model": "opus"},
    {"effort": "turbo"},
    {"provider": "codex", "model": "gpt-luna", "effort": "max"},   # luna has no max
    {"effort": ["medium"]},
])
def test_bad_settings_are_refused_and_change_nothing(body):
    async def go():
        async with bridge_client() as c:
            _, before = await get_settings(c)
            status, out = await set_settings(c, **body)
            assert status == 400 and out["ok"] is False and out["error"]
            assert (await get_settings(c))[1]["settings"] == before["settings"]
    run(go())


def test_a_provider_that_is_not_installed_cannot_be_picked(monkeypatch):
    monkeypatch.setattr(chat, "CODEX_CMD", MISSING)

    async def go():
        async with bridge_client() as c:
            _, st = await get_settings(c)
            assert [p["id"] for p in st["providers"]] == ["claude"] and "codex" not in st["models"]
            status, _ = await set_settings(c, provider="codex")
            assert status == 400
    run(go())


def test_settings_routes_refuse_web_pages():
    async def go():
        async with bridge_client() as c:
            assert (await c.get("/chat/settings", headers=EVIL)).status == 403
            assert (await c.post("/chat/settings", json={"effort": "high"}, headers=EVIL)).status == 403
            assert (await c.post("/chat/recheck", json={}, headers=EVIL)).status == 403
            assert (await get_settings(c))[1]["settings"]["effort"] == "medium"
    run(go())


def test_chat_state_carries_providers_settings_and_models():
    async def go():
        async with bridge_client() as c:
            h = await history(c, "dA")
            assert [p["id"] for p in h["providers"]] == ["claude", "codex"]
            assert h["settings"] == {"provider": "claude", "model": "opus", "effort": "medium"}
            assert set(h["models"]) == {"claude", "codex"}
            async with Tab(c, "dA") as t:
                await t.ws.send_str(json.dumps({"type": "chat-load"}))
                ev = await until(lambda: next((e for e in t.events if e["kind"] == "history"), None),
                                 what="history event")
                assert ev["settings"] == h["settings"] and ev["providers"] == h["providers"]
                assert set(ev["models"]) == {"claude", "codex"}
    run(go())


def test_settings_over_the_socket_reach_every_tab():
    async def go():
        async with bridge_client() as c:
            async with Tab(c, "dA") as a, Tab(c, "dB", name="Icons") as b:
                await a.ws.send_str(json.dumps({"type": "chat-settings"}))
                ev = await until(lambda: next((e for e in a.events if e["kind"] == "settings"), None),
                                 what="settings event")
                assert ev["settings"]["provider"] == "claude" and "codex" in ev["models"]
                a.events.clear()
                await a.ws.send_str(json.dumps({"type": "chat-settings-set", "provider": "codex",
                                                "model": "gpt-terra", "effort": "high"}))
                for t in (a, b):
                    ev = await until(lambda t=t: next((e for e in t.events if e["kind"] == "settings"),
                                                      None), what="settings pushed")
                    assert ev["settings"] == {"provider": "codex", "model": "gpt-terra", "effort": "high"}
                a.events.clear()
                b.events.clear()
                await a.ws.send_str(json.dumps({"type": "chat-settings-set", "effort": "turbo"}))
                ev = await until(lambda: next((e for e in a.events if e["kind"] == "settings"), None),
                                 what="refused settings")
                assert ev.get("error") and ev["settings"]["effort"] == "high"
                await asyncio.sleep(0.1)
                assert not [e for e in b.events if e["kind"] == "settings"]   # nothing changed for b
    run(go())


# ─── no provider at all, and checking again ───────────────────────────────

def test_no_provider_is_reported_and_nothing_starts(chat_env, monkeypatch):
    monkeypatch.setattr(chat, "CLAUDE_CMD", MISSING)
    monkeypatch.setattr(chat, "CODEX_CMD", MISSING)

    async def go():
        async with bridge_client() as c:
            h = await history(c, "dA")
            assert h["providers"] == [] and h["models"] == {}
            assert (await get_settings(c))[1]["providers"] == []
            status, _ = await send(c, "dA", "hi")
            assert status == 200
            h = await errored(c, "dA")
            assert h["messages"][0]["text"] == "hi"
            assert errors(h)[-1]["text"] == chat.tr("uk", "no_provider")
            status, _ = await send(c, "dA", "hi again", lang="en")
            h = await errored(c, "dA", 2)
            assert errors(h)[-1]["text"] == chat.tr("en", "no_provider")
        assert starts(chat_env) == [] and codex_starts(chat_env) == []
    run(go())


def test_recheck_finds_a_provider_installed_later(chat_env, monkeypatch):
    monkeypatch.setattr(chat, "CLAUDE_CMD", MISSING)
    monkeypatch.setattr(chat, "CODEX_CMD", MISSING)

    async def go():
        async with bridge_client() as c:
            async with Tab(c, "dA") as t:
                assert (await history(c, "dA"))["providers"] == []
                monkeypatch.setattr(chat, "CODEX_CMD", [sys.executable, str(FAKE_CODEX)])
                assert (await history(c, "dA"))["providers"] == []       # cached until a recheck
                r = await c.post("/chat/recheck", json={}, headers=PLUGIN)
                assert r.status == 200
                st = await r.json()
                assert [p["id"] for p in st["providers"]] == ["codex"]
                assert st["settings"]["provider"] == "codex"            # the only one found
                ev = await until(lambda: next((e for e in t.events if e["kind"] == "settings"), None),
                                 what="settings pushed after recheck")
                assert [p["id"] for p in ev["providers"]] == ["codex"]
                monkeypatch.setattr(chat, "CLAUDE_CMD", [sys.executable, str(FAKE)])
                t.events.clear()
                await t.ws.send_str(json.dumps({"type": "chat-recheck"}))
                ev = await until(lambda: next((e for e in t.events if e["kind"] == "settings"), None),
                                 what="settings after socket recheck")
                assert [p["id"] for p in ev["providers"]] == ["claude", "codex"]
                await send(c, "dA", "say тепер є")
                h = await idle_with(c, "dA", 1)
                assert h["messages"][-1]["text"] == "тепер є"
        assert len(codex_starts(chat_env)) == 1
    run(go())


# ─── sessions per (file, provider) ────────────────────────────────────────

def test_switching_provider_and_back_resumes_each_session(chat_env):
    async def go():
        async with bridge_client() as c:
            await send(c, "dA", "say a")
            await idle_with(c, "dA", 1)
            claude_sid = (await history(c, "dA"))["session"]
            st = await use_codex(c)
            await send(c, "dA", "say b")
            h = await idle_with(c, "dA", 2)
            thread = h["session"]
            assert thread and thread != claude_sid
            assert [m["text"] for m in h["messages"] if m["from"] != "system"] == ["say a", "a", "say b", "b"]
            note = system(h)[-1]
            assert note["text"] == chat.tr("uk", "switched", provider="Codex",
                                           model=model_name(st, "codex", "gpt-sol"))
            assert not note.get("error")
            assert [m["from"] for m in h["messages"]][2:] == ["me", "system", "claude"]
            await set_settings(c, provider="claude")
            await send(c, "dA", "say c")
            h = await idle_with(c, "dA", 3)
            assert h["session"] == claude_sid
            await use_codex(c)
            await send(c, "dA", "say d")
            h = await idle_with(c, "dA", 4)
            assert h["session"] == thread
            assert [m["text"] for m in h["messages"] if m["from"] == "claude"] == ["a", "b", "c", "d"]
        assert chat._sessions()["dA"] == claude_sid
        assert chat._sessions("codex")["dA"] == thread
        cs = starts(chat_env)                       # claude may stay up or restart: same session
        assert cs and all(flag(x["argv"], "--resume") == [claude_sid] for x in cs[1:])
        xs = codex_starts(chat_env)
        assert [x["resume"] for x in xs] == [False, True] and xs[1]["thread"] == thread
    run(go())


def test_each_file_has_its_own_codex_thread(chat_env):
    async def go():
        async with bridge_client() as c:
            await use_codex(c)
            await send(c, "dA", "say a")
            await send(c, "dB", "say b")
            ha = await idle_with(c, "dA", 1)
            hb = await idle_with(c, "dB", 1)
            assert ha["session"] and hb["session"] and ha["session"] != hb["session"]
            assert not system(ha)                      # the first turn of a file: no switch note
        assert {x["env"]["FIGMOSHA_TARGET"] for x in codex_starts(chat_env)} == {"dA", "dB"}
    run(go())


def test_old_flat_sessions_file_is_read_as_claude(chat_env):
    async def go():
        chat._write_json(chat.DATA_DIR / "sessions.json", {"dA": "old-flat"})
        async with bridge_client() as c:
            assert (await history(c, "dA"))["session"] == "old-flat"
            await use_codex(c)
            assert (await history(c, "dA"))["session"] is None       # no codex thread yet
            await set_settings(c, provider="claude")
            await send(c, "dA", "say ok")
            await idle_with(c, "dA", 1)
        assert flag(starts(chat_env)[0]["argv"], "--resume") == ["old-flat"]
    run(go())


def test_claude_model_and_effort_come_from_the_settings(chat_env):
    async def go():
        async with bridge_client() as c:
            await send(c, "dA", "say a")
            await idle_with(c, "dA", 1)
            st = (await set_settings(c, model="sonnet", effort="high"))[1]
            await send(c, "dA", "say b")
            h = await idle_with(c, "dA", 2)
            assert system(h)[-1]["text"] == chat.tr("uk", "switched", provider="Claude Code",
                                                    model=model_name(st, "claude", "sonnet"))
            await send(c, "dA", "say c")                   # nothing changed: same process, no note
            h = await idle_with(c, "dA", 3)
            assert len(system(h)) == 1
        first, second = starts(chat_env)
        assert flag(first["argv"], "--model") == ["opus"] and flag(first["argv"], "--effort") == ["medium"]
        assert flag(second["argv"], "--model") == ["sonnet"] and flag(second["argv"], "--effort") == ["high"]
        assert flag(second["argv"], "--resume") == [first["session"]]
    run(go())


def test_a_settings_change_during_a_turn_applies_from_the_next_one(chat_env):
    async def go():
        async with bridge_client() as c:
            await send(c, "dA", "hold перша")
            await until(lambda: chat.SESSIONS["dA"].busy, what="busy")
            await set_settings(c, effort="low")
            await send(c, "dA", "say друга")
            release(chat_env)
            h = await idle_with(c, "dA", 2)
            assert [m["text"] for m in h["messages"] if m["from"] == "claude"] == ["перша", "друга"]
        first, second = starts(chat_env)
        assert flag(first["argv"], "--effort") == ["medium"] and flag(second["argv"], "--effort") == ["low"]
    run(go())


def test_the_switch_note_follows_the_language(chat_env):
    async def go():
        async with bridge_client() as c:
            await send(c, "dA", "say a", lang="en")
            await idle_with(c, "dA", 1)
            st = await use_codex(c, model="gpt-terra")
            await send(c, "dA", "say b", lang="en")
            h = await idle_with(c, "dA", 2)
            assert system(h)[-1]["text"] == chat.tr("en", "switched", provider="Codex",
                                                    model=model_name(st, "codex", "gpt-terra"))
    run(go())


# ─── the codex runner ─────────────────────────────────────────────────────

def test_codex_new_thread_argv_env_and_prompt(chat_env, monkeypatch):
    monkeypatch.setenv("CODEX_THREAD_ID", "outer")    # the bridge itself may run inside Codex
    monkeypatch.setenv("CODEX_SESSION_ID", "outer")
    monkeypatch.setenv("CODEX_CI", "1")
    monkeypatch.setenv("CLAUDECODE", "1")

    async def go():
        async with bridge_client() as c:
            async with Tab(c, "dBanner", name="Banner"):
                await use_codex(c, model="gpt-terra", effort="high")
                await send(c, "dBanner", "say привіт")
                h = await idle_with(c, "dBanner", 1)
                assert h["messages"][-1]["text"] == "привіт" and h["messages"][-1]["from"] == "claude"
        x = codex_starts(chat_env)[0]
        argv = x["argv"]
        assert argv[0] == "exec" and not x["resume"]
        for f in ("--json", "--skip-git-repo-check", "--dangerously-bypass-approvals-and-sandbox"):
            assert f in argv
        assert argv[-1] == "-" and x["prompt"].strip() == "say привіт"
        assert x["model"] == "gpt-terra" and x["effort"] == "high"
        dev = next(v for v in x["config"] if v.startswith("developer_instructions="))
        assert "Reply in Ukrainian" in dev and "Banner" in dev
        assert Path(x["cwd"]).resolve() == chat.WORKDIR.resolve()
        assert x["env"]["FIGMOSHA_TARGET"] == "dBanner" and x["env"]["FIGMOSHA_AGENT"] == "chat"
        assert x["env"]["FIGMOSHA_FILE"] == "Banner"
        for k in ("CODEX_THREAD_ID", "CODEX_SESSION_ID", "CODEX_CI", "CLAUDECODE"):
            assert x["env"][k] is None, k
        assert starts(chat_env) == []                 # claude never started
    run(go())


def test_codex_runs_one_process_per_turn_and_resumes_its_thread(chat_env):
    async def go():
        async with bridge_client() as c:
            await use_codex(c, model="gpt-luna", effort="low")
            await send(c, "dA", "say one")
            h = await idle_with(c, "dA", 1)
            thread = h["session"]
            assert chat._sessions("codex")["dA"] == thread
            await send(c, "dA", "say two")
            await idle_with(c, "dA", 2)
        first, second = codex_starts(chat_env)
        assert second["argv"][:2] == ["exec", "resume"] and second["resume"]
        assert second["argv"][-2:] == [thread, "-"]
        assert second["model"] == "gpt-luna" and second["effort"] == "low"
        assert "--json" in second["argv"] and "--dangerously-bypass-approvals-and-sandbox" in second["argv"]
    run(go())


def test_codex_gets_photos_as_files(chat_env):
    async def go():
        async with bridge_client() as c:
            await use_codex(c)
            await send(c, "dA", "say бачу", images=[f"data:image/png;base64,{PNG}"] * 2)
            h = await idle_with(c, "dA", 1)
            assert len(h["messages"][0]["images"]) == 2
        x = codex_starts(chat_env)[0]
        assert len(x["images"]) == 2 and all(i["exists"] for i in x["images"])
        assert all(Path(i["path"]).resolve().parent == (chat.DATA_DIR / "uploads").resolve()
                   for i in x["images"])
        assert x["prompt"].strip() == "say бачу"
    run(go())


def test_codex_messages_queued_while_busy_are_answered_in_order(chat_env):
    async def go():
        async with bridge_client() as c:
            await use_codex(c)
            await send(c, "dA", "hold перша")
            await send(c, "dA", "say друга")
            release(chat_env)
            h = await idle_with(c, "dA", 2)
            assert [m["text"] for m in h["messages"] if m["from"] == "claude"] == ["перша", "друга"]
    run(go())


def test_codex_reply_buttons_and_activity_lines(chat_env):
    async def go():
        async with bridge_client() as c:
            async with Tab(c, "dA") as t:
                await use_codex(c)
                await send(c, "dA", "say Готово\n```actions\nДалі\nСтоп\n```")
                h = await idle_with(c, "dA", 1)
                assert h["messages"][-1]["text"] == "Готово"
                assert h["messages"][-1]["actions"] == ["Далі", "Стоп"]
                await send(c, "dA", "tool")
                await idle_with(c, "dA", 2)
                await send(c, "dA", "odd")
                h = await idle_with(c, "dA", 3)
                assert h["messages"][-1]["text"] == "done" and errors(h) == []
                lines = [e["text"] for e in t.events if e["kind"] == "activity"]
                assert any("ls -la" in x for x in lines)
                assert any("reasoning" in x for x in lines) and any("todo_list" in x for x in lines)
                assert all(len(x) <= 120 for x in lines)
                assert "delta" not in t.kinds()                # codex sends whole messages
                assert t.events[-1] == {"kind": "busy", "busy": False}
                assert (await history(c, "dA"))["activity"] == []
    run(go())


def test_codex_warning_item_does_not_end_the_turn(chat_env):
    async def go():
        async with bridge_client() as c:
            await use_codex(c)
            await send(c, "dA", "warn")
            h = await idle_with(c, "dA", 1)
            assert h["messages"][-1]["text"] == "still ok" and errors(h) == []
    run(go())


@pytest.mark.parametrize("word", ["fail", "die", "crash"])
def test_codex_failures_are_reported_and_the_next_message_resumes(chat_env, word):
    async def go():
        async with bridge_client() as c:
            await use_codex(c)
            await send(c, "dA", "say first")
            h = await idle_with(c, "dA", 1)
            thread = h["session"]
            await send(c, "dA", word)
            h = await errored(c, "dA")
            assert len(errors(h)) == 1
            if word == "crash":
                assert "boom" in errors(h)[0]["text"]
            if word == "fail":
                assert "model_not_found" in errors(h)[0]["text"]
            await send(c, "dA", "say back")
            h = await idle_with(c, "dA", 2)
            assert h["messages"][-1]["text"] == "back" and h["session"] == thread
        assert codex_starts(chat_env)[-1]["thread"] == thread
    run(go())


def test_codex_lost_thread_starts_a_new_one_and_answers(chat_env):
    async def go():
        chat._save_session_id("dA", "gone-1", "codex")
        async with bridge_client() as c:
            await use_codex(c)
            await send(c, "dA", "say привіт")
            h = await idle_with(c, "dA", 1)
            assert h["messages"][-1]["text"] == "привіт" and errors(h) == []
            assert [m["text"] for m in system(h)] == [chat.tr("uk", "lost")]
            assert h["session"] and h["session"] != "gone-1"
        assert chat._sessions("codex")["dA"] == h["session"]
        lost, fresh = codex_starts(chat_env)
        assert lost["resume"] and not fresh["resume"]
    run(go())


def test_codex_stop_terminates_the_turn_without_a_chat_line(chat_env):
    async def go():
        async with bridge_client() as c:
            async with Tab(c, "dA") as t:
                await use_codex(c)
                await send(c, "dA", "say first")
                h = await idle_with(c, "dA", 1)
                thread = h["session"]
                await send(c, "dA", "slow")
                await until(lambda: any(e["kind"] == "activity" for e in t.events), what="command started")
                t0 = time.monotonic()
                r = await c.post("/chat/stop", json={"doc": "dA"}, headers=PLUGIN)
                assert r.status == 200
                await until(lambda: not chat.SESSIONS["dA"].busy, what="idle after stop")
                assert time.monotonic() - t0 < chat.STOP_GRACE       # terminate, not the kill
                h = await history(c, "dA")
                assert [m["text"] for m in h["messages"]] == ["say first", "first", "slow"]
                assert t.events[-1] == {"kind": "busy", "busy": False}
            assert any(r.get("signal") == "TERM" for r in codex_log(chat_env))
            await send(c, "dA", "say still here")
            h = await idle_with(c, "dA", 2)
            assert h["messages"][-1]["text"] == "still here" and h["session"] == thread
    run(go())


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_codex_stop_kills_a_turn_that_ignores_terminate(chat_env, monkeypatch):
    monkeypatch.setattr(chat, "STOP_GRACE", 0.1)               # no real wait over 100 ms

    async def go():
        async with bridge_client() as c:
            await use_codex(c)
            await send(c, "dA", "hang")
            rec = await until(lambda: next((r for r in codex_log(chat_env) if r.get("ignoring")), None),
                              what="hang started")
            await c.post("/chat/stop", json={"doc": "dA"}, headers=PLUGIN)
            await until(lambda: not chat.SESSIONS["dA"].busy, timeout=4, what="idle after kill")
            if os.name != "nt":                                # os.kill(pid, 0) kills on Windows
                await until(lambda: not alive(rec["pid"]), timeout=4, what="the hung codex is gone")
            h = await history(c, "dA")
            assert errors(h) == []
            await send(c, "dA", "say back")
            h = await idle_with(c, "dA", 1)
            assert h["messages"][-1]["text"] == "back"
    run(go())


@posix_only
def test_codex_stop_also_ends_the_wrapper_child(chat_env, monkeypatch):
    monkeypatch.setattr(chat, "STOP_GRACE", 0.1)               # no real wait over 100 ms

    async def go():
        async with bridge_client() as c:
            await use_codex(c)
            await send(c, "dA", "wrapped")
            rec = await until(lambda: next((r for r in codex_log(chat_env) if r.get("kid")), None),
                              what="wrapped started")
            await c.post("/chat/stop", json={"doc": "dA"}, headers=PLUGIN)
            await until(lambda: not chat.SESSIONS["dA"].busy, timeout=4, what="idle after kill")
            await until(lambda: not alive(rec["kid"]), timeout=4, what="the child is gone too")
    run(go())


# ─── slash commands on codex ──────────────────────────────────────────────

def test_codex_offers_only_clear():
    async def go():
        async with bridge_client() as c:
            await use_codex(c)
            h = await history(c, "dA")
            assert [x["name"] for x in h["commands"]] == ["clear"]
            assert set(h["commands"][0]["aliases"]) == {"reset", "new"}
            await set_settings(c, provider="claude")
            assert {"clear", "compact"} <= {x["name"] for x in (await history(c, "dA"))["commands"]}
    run(go())


def test_codex_clear_drops_only_the_codex_thread(chat_env):
    async def go():
        async with bridge_client() as c:
            await send(c, "dA", "say a")
            await idle_with(c, "dA", 1)
            claude_sid = (await history(c, "dA"))["session"]
            await use_codex(c)
            await send(c, "dA", "say b")
            await idle_with(c, "dA", 2)
            await send(c, "dA", "/clear")
            h = await idle_with_system(c, "dA", 2)
            assert h["messages"][-1]["text"] == "Почав нову розмову. Codex не бачить попередні повідомлення."
            assert h["session"] is None
            assert "dA" not in chat._sessions("codex")
            assert chat._sessions()["dA"] == claude_sid
            await send(c, "dA", "say c")
            await idle_with(c, "dA", 3)
        xs = codex_starts(chat_env)
        assert len(xs) == 2 and not xs[1]["resume"]
        assert all("/clear" not in x["prompt"] for x in xs)
    run(go())


@pytest.mark.parametrize("cmd", ["/compact", "/context", "/review-it src", "/init"])
def test_codex_refuses_other_commands(chat_env, cmd):
    async def go():
        async with bridge_client() as c:
            await use_codex(c)
            await send(c, "dA", cmd)
            h = await idle_with_system(c, "dA", 1)
            name = cmd[1:].split()[0]
            assert h["messages"][-1]["text"] == chat.tr("uk", "codex_refused", cmd=name)
            assert not h["messages"][-1].get("error") and h["busy"] is False
        assert codex_starts(chat_env) == []
    run(go())


@pytest.mark.parametrize("cmd", ["/model gpt-sol", "/effort high"])
def test_model_commands_point_to_the_settings_on_both_providers(chat_env, cmd):
    async def go():
        async with bridge_client() as c:
            await send(c, "dA", cmd)
            await use_codex(c)
            await send(c, "dA", cmd)
            h = await idle_with_system(c, "dA", 2)
            name = cmd[1:].split()[0]
            assert [m["text"] for m in system(h)] == [chat.tr("uk", "locked", cmd=name)] * 2
        assert starts(chat_env) == [] and codex_starts(chat_env) == []
    run(go())


# ─── texts ────────────────────────────────────────────────────────────────

def test_new_texts_are_plain_in_both_languages():
    kw = {"provider": "Codex", "model": "GPT Sol", "cmd": "compact"}
    for key in ("switched", "no_provider", "codex_refused", "locked", "lost"):
        uk, en = chat.tr("uk", key, **kw), chat.tr("en", key, **kw)
        assert re.search(r"[А-Яа-яІіЇїЄєҐґ]", uk), key
        assert not re.search(r"[А-Яа-яІіЇїЄєҐґ]", en), key
        assert not DASHES_QUOTES.search(uk.replace("/compact", "")), key
        assert not DASHES_QUOTES.search(en.replace("/compact", "")), key
    assert "Claude" in chat.tr("en", "no_provider") and "Codex" in chat.tr("en", "no_provider")
    assert "settings" in chat.tr("en", "locked", cmd="model").lower()
    assert "налаштуван" in chat.tr("uk", "locked", cmd="model").lower()
    assert "Claude" not in chat.tr("en", "lost") + chat.tr("uk", "lost")   # shown for Codex too
