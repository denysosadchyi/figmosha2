"""Chat with an agent inside the Figma plugin: one session per file and provider.

The agent is Claude Code or Codex, picked on the plugin settings page with the
model and the effort (DATA_DIR/settings.json, one for the whole bridge).

Claude Code runs headless, one long-lived process per Figma file (keyed by the
plugin's docSig), and talks stream-json:

    claude -p --input-format stream-json --output-format stream-json \
           --verbose --include-partial-messages --model <model> --effort <effort> \
           --dangerously-skip-permissions [--resume <session_id>]

Codex runs one process per turn and prints JSON lines:

    codex exec [resume] [-i <photo>]... --json --skip-git-repo-check \
          --dangerously-bypass-approvals-and-sandbox -m <model> \
          -c model_reasoning_effort=<effort> -c developer_instructions=<str> [<thread_id>] -

The plugin window is only a view of that session. Messages, the session id and
pasted images live in DATA_DIR, so a bridge restart resumes the same session.
The contract (routes, events, message fields) is in docs/CHAT.md.
"""

import asyncio
import base64
import binascii
import json
import os
import sys
import re
import shutil
import time
import uuid
from pathlib import Path

from aiohttp import web

HERE = Path(__file__).resolve().parent

# Settings. Tests override these.
DATA_DIR = Path(os.environ.get("FIGMOSHA_CHAT_DIR") or (Path.home() / ".figmosha" / "chat"))
CLAUDE_CMD = (os.environ.get("FIGMOSHA_CLAUDE") or "claude").split()
CODEX_CMD = (os.environ.get("FIGMOSHA_CODEX") or "codex").split()
WORKDIR = HERE
STOP_GRACE = 5.0       # seconds an interrupted turn gets before the process is killed
HISTORY_LIMIT = 500    # messages kept per file
ACTIVITY_LIMIT = 30
MAX_IMAGES = 8
MAX_IMAGE_BYTES = 10 * 1024 * 1024

SESSIONS = {}          # doc -> Session
_FOUND = None          # detect_providers() result, kept until a recheck or reset()
_MODELS = {}           # provider id -> [model item], read with _FOUND

# Hooks the bridge fills in (setup): who to push events to, and the guard.
_peers = lambda doc: []          # noqa: E731  doc -> [(ws, file name)]
_tabs = lambda: []               # noqa: E731  every plugin tab with a file id: [(ws, doc)]
_guard = None

DOC_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
UPLOAD_RE = re.compile(r"^[a-f0-9]{32}\.(png|jpg|gif|webp)$")
DATA_URL_RE = re.compile(r"^data:image/(png|jpeg|jpg|gif|webp);base64,([A-Za-z0-9+/=\s]+)$")
BLOCK_RE = re.compile(r"```(actions|questions)[ \t]*\r?\n(.*?)```[ \t]*", re.S)
SLASH_RE = re.compile(r"^/([A-Za-z0-9][A-Za-z0-9_:.-]*)(?=\s|$)")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_:.-]*$")

# Slash commands. Claude Code runs most of them in -p mode too; these are
# the exceptions. /clear is done by the bridge (a new session for the file),
# the terminal ones only print "isn't available in this environment".
CLEAR_COMMANDS = {"clear", "reset", "new"}
TERMINAL_COMMANDS = {
    "help", "exit", "quit", "resume", "continue", "login", "logout", "ide", "terminal-setup",
    "vim", "theme", "permissions", "allowed-tools", "statusline", "keybindings", "status",
    "memory", "add-dir", "hooks", "export", "plugin", "plugins", "bug", "feedback",
    "release-notes", "upgrade", "privacy-settings", "tasks", "copy", "rewind", "agents-ui",
}
# The model and the effort come from the plugin settings, so these are refused.
LOCKED_COMMANDS = {"model", "effort", "fast"}
# Codex in exec mode has no slash commands: the bridge does /clear, the rest is refused.
CODEX_COMMANDS = [{"name": "clear", "description": "", "hint": "", "aliases": ["reset", "new"]}]
# Shown before Claude has started once and told us its list (system/init).
FALLBACK_COMMANDS = [
    {"name": "clear", "aliases": ["reset", "new"]},
    {"name": "compact", "hint": "<optional custom summarization instructions>"},
    {"name": "context"}, {"name": "usage", "aliases": ["cost", "stats"]},
    {"name": "init"}, {"name": "rename", "hint": "[name]"},
    {"name": "recap"}, {"name": "code-review", "aliases": ["review"]}, {"name": "security-review"},
    {"name": "simplify"}, {"name": "mcp"}, {"name": "config", "hint": "key=value"},
]

SYSTEM_PROMPT = """\
You are talking to a designer through a chat panel inside Figma (the Figmosha plugin).
The Figma file of this chat is "{name}". figmosha.py already targets it
(FIGMOSHA_TARGET is set), so do not pass -T unless you mean another file.
Use figmosha.py to read and change the file. {reply}
Keep replies short and plain: the panel is narrow.
When it helps, end a reply with up to 4 next steps the designer can click,
first one recommended, as a fenced block:
```actions
First option
Second option
```
Open questions you need answered go in a ```questions block, one per line.
"""

# The chat language, from the plugin settings: Claude's replies and the bridge's own notes.
# A message without lang keeps the file's last one (uk at first).
LANGS = ("uk", "en")
TEXTS = {
    "uk": {
        "reply": "Reply in Ukrainian.",
        "lang_switched": "(Мову чату змінено на українську. Reply in Ukrainian from this message on, "
                         "even if the talk so far was in another language. Відповідай українською.)",
        "disk": "Не можу зберегти історію чату на диск. Перевір місце на диску і права на папку.",
        "died": "Claude Code зупинився (код {code}). {tail}",
        "compacting": "Стискаю розмову",
        "compacted": "Розмову стиснуто. Далі Claude працює з її коротким підсумком.",
        "cleared": "Почав нову розмову. {agent} не бачить попередні повідомлення.",
        "no_claude": "Не знайшов Claude Code ({cmd}). Встанови claude і увійди в нього в терміналі.",
        "no_start": "Не вдалося запустити Claude Code: {e}",
        "broken": "Щось пішло не так у чаті. {e}",
        "lost": "Стару розмову не знайдено. Почав нову.",
        "bridge_down": "Міст зупинився, поки Claude працював. Відповідь перервано. "
                       "Надішли повідомлення ще раз.",
        "photo_dropped": "Фото не додано. Файл завеликий або пошкоджений.",
        "photos_dropped": "Фото не додано: {n}. Файли завеликі або пошкоджені.",
        "terminal": "Команда /{cmd} працює лише в терміналі. Тут її не запустити.",
        "locked": "Команда /{cmd} тут вимкнена. Модель і зусилля вибери в налаштуваннях.",
        "switched": "Тепер відповідає {provider}. Модель {model}.",
        "no_provider": "Не знайшов Claude Code чи Codex на комп'ютері з мостом. "
                       "Встанови один із них і увійди в нього в терміналі.",
        "codex_refused": "Команда /{cmd} працює лише з Claude Code. У Codex її немає.",
        "codex_died": "Codex зупинився (код {code}). {tail}",
        "codex_error": "Codex повернув помилку.",
        "codex_no_start": "Не вдалося запустити Codex: {e}",
        "unknown": "Команди /{cmd} немає. Набери / і вибери команду зі списку.",
        "question": "Питання {n}. {q}",
        "answer_to": "[Відповідь на твоє питання: {q}]\n",
        "quoted": "[Цитата з чату]\n{q}\n\n",
    },
    "en": {
        "reply": "Reply in English.",
        "lang_switched": "(The chat language is now English. Reply in English from this message on, "
                         "even if the talk so far was in another language.)",
        "disk": "Cannot save the chat history to disk. Check the free space and the folder rights.",
        "died": "Claude Code stopped (code {code}). {tail}",
        "compacting": "Compacting the conversation",
        "compacted": "The conversation is compacted. Claude now works from its short summary.",
        "cleared": "Started a new conversation. {agent} does not see the earlier messages.",
        "no_claude": "Claude Code ({cmd}) is not found. Install claude and log in to it in a terminal.",
        "no_start": "Could not start Claude Code: {e}",
        "broken": "Something went wrong in the chat. {e}",
        "lost": "The old conversation is not found. Started a new one.",
        "bridge_down": "The bridge stopped while Claude was working. The reply is cut off. "
                       "Send the message again.",
        "photo_dropped": "The photo is not added. The file is too big or broken.",
        "photos_dropped": "Photos not added: {n}. The files are too big or broken.",
        "terminal": "The /{cmd} command only works in the terminal. You cannot run it here.",
        "locked": "The /{cmd} command is off here. Pick the model and the effort in the settings.",
        "switched": "Now {provider} answers. The model is {model}.",
        "no_provider": "Claude Code or Codex is not found on the bridge computer. "
                       "Install one of them and log in to it in a terminal.",
        "codex_refused": "The /{cmd} command only works with Claude Code. Codex does not have it.",
        "codex_died": "Codex stopped (code {code}). {tail}",
        "codex_error": "Codex returned an error.",
        "codex_no_start": "Could not start Codex: {e}",
        "unknown": "There is no /{cmd} command. Type / and pick a command from the list.",
        "question": "Question {n}. {q}",
        "answer_to": "[Answer to your question: {q}]\n",
        "quoted": "[Quote from the chat]\n{q}\n\n",
    },
}


def tr(lang, key, **kw):
    """A bridge text in the chat language (uk when unknown)."""
    return TEXTS.get(lang, TEXTS["uk"])[key].format(**kw)


def reset():
    """Forget all in-memory state. DATA_DIR stays (a restart keeps the history)."""
    global _FOUND
    SESSIONS.clear()
    _FOUND = None
    _MODELS.clear()


def valid_doc(doc):
    return isinstance(doc, str) and bool(DOC_RE.match(doc))


# ─── pure helpers ─────────────────────────────────────────────────────────

def parse_reply(text):
    """Take ```actions / ```questions blocks out of a reply.

    Returns (clean_text, actions, questions). One item per line, at most 4 actions.
    """
    actions, questions = [], []

    def take(m):
        items = [x.strip() for x in m.group(2).splitlines() if x.strip()]
        (actions if m.group(1) == "actions" else questions).extend(items)
        return ""

    clean = BLOCK_RE.sub(take, text or "")
    if not actions and not questions:
        return text, [], []
    return clean.strip(), actions[:4], questions[:10]


# What Claude Code reports, in words the designer understands (uk, en). Matched first by the
# error kind (the assistant event's `error`, or the result subtype), then by the text.
ERRORS = [
    ("authentication_failed", r"log ?in|api key|oauth|authenticat",
     "Claude Code не увійшов в акаунт. Відкрий термінал на комп'ютері з мостом. "
     "Запусти claude і виконай /login.",
     "Claude Code is not logged in. Open a terminal on the bridge computer. "
     "Run claude and type /login."),
    ("rate_limit", r"limit", "Ліміт підписки Claude вичерпано. Спробуй пізніше.",
     "The Claude subscription limit is used up. Try again later."),
    ("billing_error", r"credit|billing", "Проблема з оплатою Claude. Перевір підписку в акаунті.",
     "There is a Claude billing problem. Check the subscription in your account."),
    ("server_error", r"overloaded|api error: 5\d\d",
     "Сервер Claude зараз не відповідає. Спробуй ще раз за хвилину.",
     "The Claude server does not answer now. Try again in a minute."),
    ("invalid_request", r"prompt is too long", "Claude не прийняв запит.",
     "Claude did not accept the request."),
    ("error_max_turns", None, "Claude зробив забагато кроків і зупинився. Напиши, щоб він продовжив.",
     "Claude took too many steps and stopped. Ask it to go on."),
]
GENERIC_ERROR = "Claude Code повернув помилку."
GENERIC_ERROR_EN = "Claude Code returned an error."
LOST_SESSION = "No conversation found"


def error_text(kind, raw, lang="uk"):
    """A line for a Claude Code error in the chat language, with the original text as the detail line."""
    raw = (raw or "").strip()
    i = 3 if lang == "en" else 2
    line = next((e[i] for e in ERRORS if kind and e[0] == kind), None)
    if line is None:
        line = next((e[i] for e in ERRORS if e[1] and raw and re.search(e[1], raw, re.I)),
                    GENERIC_ERROR_EN if lang == "en" else GENERIC_ERROR)
    return f"{line}\n{raw}" if raw else line


def _basename(p):
    return re.split(r"[\\/]", str(p).rstrip("\\/"))[-1] if p else ""


def activity_line(block):
    """One short line for a tool call, for the activity strip."""
    name = str(block.get("name") or "tool")
    inp = block.get("input") if isinstance(block.get("input"), dict) else {}
    if name == "Bash":
        line = inp.get("description") or f"Bash: {inp.get('command', '')}"
    elif inp.get("file_path") or inp.get("notebook_path"):
        line = f"{name} {_basename(inp.get('file_path') or inp.get('notebook_path'))}"
    else:
        line = name
    line = " ".join(line.split())
    return line if len(line) <= 120 else line[:119] + "…"


# ─── storage ──────────────────────────────────────────────────────────────

def _write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _sessions_file(provider="claude"):
    """claude keeps the old sessions.json; codex keeps its thread ids in sessions-codex.json."""
    return DATA_DIR / ("sessions.json" if provider == "claude" else f"sessions-{provider}.json")


def _sessions(provider="claude"):
    data = _read_json(_sessions_file(provider), {})
    return data if isinstance(data, dict) else {}


def _save_session_id(doc, sid, provider="claude"):
    data = _sessions(provider)
    if data.get(doc) != sid:
        if sid is None:
            data.pop(doc, None)
        else:
            data[doc] = sid
        try:
            _write_json(_sessions_file(provider), data)
        except OSError as e:     # the chat goes on; only a restart forgets the session
            print(f"[chat] cannot save {_sessions_file(provider).name}: {e!r}", file=sys.stderr, flush=True)


def _uploads():
    return DATA_DIR / "uploads"


def _commands_file():
    return DATA_DIR / "commands.json"


def _command(c):
    return {"name": c["name"], "description": str(c.get("description") or ""),
            "hint": str(c.get("hint") or c.get("argumentHint") or ""),
            "aliases": [a for a in (c.get("aliases") or []) if isinstance(a, str) and NAME_RE.match(a)]}


def known_commands():
    """Claude's own list from its last start, or None if it never ran here."""
    data = _read_json(_commands_file(), None)
    if not isinstance(data, dict) or not isinstance(data.get("commands"), list):
        return None
    return [c for c in data["commands"] if isinstance(c, dict) and isinstance(c.get("name"), str)]


def commands():
    """Slash commands for the popup: [{name, description, hint, aliases}]."""
    listed = known_commands() or [_command(c) for c in FALLBACK_COMMANDS]
    return [c for c in listed if c["name"] not in LOCKED_COMMANDS]


def _merge_commands(listed):
    """Store Claude's list. listed: names (init) or {name, description, ...} (commands_changed).

    Returns True when the stored list changed."""
    old = known_commands() or []
    by_name = {c["name"]: c for c in old}
    new = []
    for item in listed or []:
        c = item if isinstance(item, dict) else {"name": item}
        name = c.get("name")
        if not isinstance(name, str) or not NAME_RE.match(name):
            continue
        if isinstance(item, dict):
            new.append(_command(c))
        else:
            new.append(by_name.get(name) or _command(c))
    if not new or new == old:
        return False
    _write_json(_commands_file(), {"commands": new})
    return True


def slash_command(text):
    """'/name args' -> 'name' (lower case), anything else -> None."""
    m = SLASH_RE.match(text or "")
    return m.group(1).lower() if m else None


def _is_known(name, cmds):
    return any(name == c["name"].lower() or name in [a.lower() for a in c.get("aliases") or []]
               for c in cmds)


# ─── providers: Claude Code or Codex ──────────────────────────────────────

PROVIDER_NAMES = {"claude": "Claude Code", "codex": "Codex"}
PROVIDER_IDS = ("claude", "codex")
CLAUDE_EFFORTS = ["low", "medium", "high", "xhigh", "max"]
CLAUDE_MODELS = [
    {"id": "opus", "name": "Opus", "efforts": CLAUDE_EFFORTS, "default_effort": "medium"},
    {"id": "sonnet", "name": "Sonnet", "efforts": CLAUDE_EFFORTS, "default_effort": "medium"},
    {"id": "haiku", "name": "Haiku", "efforts": ["low", "medium", "high"], "default_effort": "medium"},
]
DEFAULT_EFFORT = "medium"
LOST_THREAD = "no rollout found"     # codex exec resume with a thread it does not have


def _which(cmd, env):
    # env None: the bridge's own PATH (a one-argument call, which tests may stub).
    return shutil.which(cmd) if env is None else shutil.which(cmd, path=env.get("PATH") or "")


def _given(cmd, env):
    """A command set by FIGMOSHA_CLAUDE / FIGMOSHA_CODEX (or a test): used as is, or None."""
    if cmd and (_which(cmd[0], env) or Path(cmd[0]).is_file()):
        return list(cmd)
    return None


def _version_key(name):
    try:
        return tuple(int(x) for x in name.split("."))
    except ValueError:
        return None


# Homebrew on Apple Silicon and on Intel. A Mac bridge started from launchd or a Figma-launched
# shell has only /usr/bin:/bin:/usr/sbin:/sbin on PATH, so these are looked at by hand.
BREW_DIRS = ("/opt/homebrew/bin", "/usr/local/bin")


def _file(p):
    return str(p) if p.is_file() else None


def _home_claude(cmd, home=None):
    """The native installer puts claude in ~/.local/bin, which a service's PATH may lack."""
    names = ("claude.exe", "claude") if cmd == "claude" else (cmd,)
    for name in names:
        found = _file((home or Path.home()) / ".local" / "bin" / name)
        if found:
            return found
    return None


def _newest(paths, version):
    """The newest of paths that are files, by the numeric version version(path) gives."""
    best = None
    for p in paths:
        key = _version_key(version(p))
        if key is not None and p.is_file() and (best is None or key > best[0]):
            best = (key, p)
    return str(best[1]) if best else None


def _extra_dirs(home=None):
    """Folders a minimal PATH misses: Homebrew, then the npm folders in the home."""
    home = home or Path.home()
    dirs = [Path(d) for d in BREW_DIRS] + [home / ".npm-global" / "bin", home / ".volta" / "bin"]
    return dirs, home / ".nvm" / "versions" / "node"


def _extra_agent(cmd, home=None):
    """cmd in Homebrew, ~/.npm-global/bin, ~/.volta/bin, then the newest nvm node, or None."""
    dirs, nvm = _extra_dirs(home)
    for d in dirs:
        found = _file(d / cmd)
        if found:
            return found
    return _newest(nvm.glob("v*/bin/" + cmd), lambda p: p.parent.parent.name[1:])


def _app_claude(env, home=None):
    """The copy of claude inside the Claude desktop app, newest version first.
    Windows: under LOCALAPPDATA. macOS: under ~/Library/Application Support/Claude."""
    local = env.get("LOCALAPPDATA")
    if local:
        found = _newest(Path(local).glob("Claude-Data/claude_*/claude-code/*/*/claude.exe"),
                        lambda p: p.parent.parent.name)
        if found:
            return found
    mac = (home or Path.home()) / "Library" / "Application Support" / "Claude" / "claude-code"
    copies = [(p, p.parents[3].name) for p in mac.glob("*/claude.app/Contents/MacOS/claude")]
    copies += [(p, p.parent.name) for p in mac.glob("*/claude")]
    ver = dict((str(p), v) for p, v in copies)
    return _newest([p for p, _ in copies], lambda p: ver[str(p)])


def _path_with(env, exe):
    """A copy of env with the agent's folder at the end of PATH, so a script it runs
    (an npm codex: #!/usr/bin/env node) finds what sits next to it. Windows: unchanged."""
    out = dict(env)
    if os.name == "nt":
        return out
    folder = str(Path(exe).parent)
    parts = [x for x in (out.get("PATH") or "").split(os.pathsep) if x]
    if folder not in parts:
        out["PATH"] = os.pathsep.join(parts + [folder])
    return out


def find_claude(env=None, home=None):
    """The argv that runs Claude Code here, or None when it is not found."""
    cmd = list(CLAUDE_CMD)
    if not cmd or cmd[0] != "claude":
        return _given(cmd, env)
    e = os.environ if env is None else env
    exe = (_which("claude", env) or _home_claude("claude", home) or _extra_agent("claude", home)
           or _app_claude(e, home))
    return [exe] + cmd[1:] if exe else None


def find_codex(env=None, home=None):
    """The argv that runs Codex here, or None. npm on Windows: the native exe, node, then the .cmd."""
    cmd = list(CODEX_CMD)
    if not cmd or cmd[0] != "codex":
        return _given(cmd, env)
    e = os.environ if env is None else env
    exe = _which("codex", env)
    if exe and Path(exe).suffix.lower() in (".cmd", ".bat", ".ps1"):
        # The npm shim on Windows PATH. A batch file gets its arguments through cmd.exe,
        # which breaks the quotes in developer_instructions, and a stop cannot reach the
        # native child. So run what the shim runs, from the npm folder it sits in.
        return _npm_codex(Path(exe).parent, e, env) or [exe] + cmd[1:]
    exe = exe or _home_claude("codex", home) or _extra_agent("codex", home)
    if exe:
        return [exe] + cmd[1:]
    if e.get("APPDATA"):
        return _npm_codex(Path(e["APPDATA"]) / "npm", e, env, shim=True)
    return None


def _npm_codex(npm, e, env, shim=False):
    """Codex in an npm global folder: the native exe, else node codex.js, else (shim) cmd /c codex.cmd."""
    extra = list(CODEX_CMD)[1:]
    pkg = npm / "node_modules" / "@openai" / "codex"
    native = sorted(p for p in pkg.glob("node_modules/@openai/codex-win32-*/vendor/*/bin/codex.exe")
                    if p.is_file())
    if native:
        return [str(native[0])] + extra
    js, node = pkg / "bin" / "codex.js", _which("node", env)
    if js.is_file() and node:
        return [node, str(js)] + extra
    if shim and (npm / "codex.cmd").is_file():
        return [e.get("COMSPEC") or "cmd.exe", "/c", str(npm / "codex.cmd")] + extra
    return None


def detect_providers(env=None, home=None):
    """The agents found on this computer: [{id, name, path}], Claude Code first."""
    out = []
    for pid, find in (("claude", find_claude), ("codex", find_codex)):
        argv = find(env=env, home=home)
        if argv:
            out.append({"id": pid, "name": PROVIDER_NAMES[pid], "path": argv[0]})
    return out


def _codex_home():
    return Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))


def _codex_config_model():
    """`model = "..."` at the top of the Codex config.toml, or None."""
    try:
        text = (_codex_home() / "config.toml").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("["):        # a table: the top-level keys are over
            return None
        m = re.match(r"""^model\s*=\s*["']([^"']+)["']""", line)
        if m:
            return m.group(1)
    return None


def _codex_items(data):
    models = data.get("models") if isinstance(data, dict) else None
    out = []
    for m in models if isinstance(models, list) else []:
        if not isinstance(m, dict) or m.get("visibility") != "list" or not isinstance(m.get("slug"), str):
            continue
        levels = m.get("supported_reasoning_levels") or []
        efforts = [x.get("effort") for x in levels if isinstance(x, dict) and isinstance(x.get("effort"), str)]
        if not efforts:
            continue
        prio = m.get("priority") if isinstance(m.get("priority"), (int, float)) else 1e9
        default = m.get("default_reasoning_level")
        out.append((prio, {"id": m["slug"], "name": str(m.get("display_name") or m["slug"]),
                           "efforts": efforts,
                           "default_effort": default if default in efforts else efforts[0]}))
    out.sort(key=lambda x: x[0])
    return [m for _, m in out]


def _codex_models(argv):
    """`codex debug models`, else ~/.codex/models_cache.json, else the config model alone."""
    import subprocess
    items = []
    try:
        r = subprocess.run(argv + ["debug", "models"], capture_output=True, timeout=20,
                           cwd=str(WORKDIR), env=_agent_env())
        items = _codex_items(json.loads(r.stdout.decode("utf-8", "replace")))
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    if not items:
        items = _codex_items(_read_json(_codex_home() / "models_cache.json", None))
    if not items:
        model = _codex_config_model()
        if model:
            items = [{"id": model, "name": model, "efforts": ["low", "medium", "high"],
                      "default_effort": DEFAULT_EFFORT}]
    return items


def providers():
    """The bridge's copy of detect_providers(): found at first use, again on recheck()."""
    global _FOUND
    if _FOUND is None:
        found = detect_providers()
        models = {}
        for p in found:
            models[p["id"]] = ([dict(m) for m in CLAUDE_MODELS] if p["id"] == "claude"
                               else _codex_models(find_codex() or list(CODEX_CMD)))
        _FOUND = found
        _MODELS.clear()
        _MODELS.update(models)
    return _FOUND


def recheck():
    """Look for the agents again (the plugin's Check again button)."""
    global _FOUND
    _FOUND = None
    providers()
    return settings_state()


def models_of(provider):
    providers()
    return _MODELS.get(provider) or []


def _model(provider, model):
    return next((m for m in models_of(provider) if m["id"] == model), None)


def model_name(provider, model):
    m = _model(provider, model)
    return m["name"] if m else str(model)


def _default_model(provider):
    if provider == "claude":
        return "opus"
    listed = [m["id"] for m in models_of(provider)]
    want = _codex_config_model()
    return want if want in listed else (listed[0] if listed else None)


def _effort_for(provider, model, want=DEFAULT_EFFORT):
    m = _model(provider, model)
    efforts = m["efforts"] if m else []
    if want in efforts:
        return want
    if DEFAULT_EFFORT in efforts:
        return DEFAULT_EFFORT
    return (m or {}).get("default_effort") or (efforts[0] if efforts else DEFAULT_EFFORT)


def _settings_file():
    return DATA_DIR / "settings.json"


def _first_pick(ids):
    """No settings yet: the agent that installed Figmosha, when both are found."""
    if len(ids) == 1:
        return ids[0]
    if os.environ.get("CODEX_THREAD_ID") and "codex" in ids:
        return "codex"
    return "claude" if "claude" in ids else ids[0]


def _save_settings(cfg, raw):
    data = dict(raw) if isinstance(raw, dict) else {}
    data.update(cfg)
    if data == raw:
        return
    try:
        _write_json(_settings_file(), data)
    except OSError as e:
        print(f"[chat] cannot save settings.json: {e!r}", file=sys.stderr, flush=True)


def settings():
    """{provider, model, effort} for the next turn of every file.

    A missing file gets the default (and is written once an agent is found). A saved
    provider that is gone falls back to the first found one, with its default model."""
    ids = [p["id"] for p in providers()]
    raw = _read_json(_settings_file(), None)
    raw = raw if isinstance(raw, dict) else None
    if not ids:
        return {"provider": "claude", "model": "opus", "effort": DEFAULT_EFFORT}
    if raw is not None and raw.get("provider") in ids:
        prov = raw["provider"]
        model = raw.get("model") if _model(prov, raw.get("model")) else _default_model(prov)
        effort = _effort_for(prov, model, raw.get("effort"))
    else:
        prov = _first_pick(ids) if raw is None or "provider" not in raw else ids[0]
        model = _default_model(prov)
        effort = _effort_for(prov, model)
    cfg = {"provider": prov, "model": model, "effort": effort}
    if raw is None or raw.get("provider") in ids or "provider" not in raw:
        _save_settings(cfg, raw)      # a saved agent that is gone for now keeps its file entry
    return cfg


def settings_state():
    """What the settings page needs: {ok, providers, settings, models} (found providers only)."""
    found = providers()
    cfg = settings()
    return {"ok": True, "settings": cfg,
            "providers": [dict(p, model=_default_model(p["id"]), models=models_of(p["id"])) for p in found],
            "models": {p["id"]: models_of(p["id"]) for p in found}}


def set_settings(body):
    """Change the provider, model or effort. Returns (status, json); nothing changes on 400."""
    if not isinstance(body, dict):
        body = {}
    for k in ("provider", "model", "effort"):
        if body.get(k) is not None and not isinstance(body[k], str):
            return 400, {"ok": False, "error": f"'{k}' must be a string"}
    ids = [p["id"] for p in providers()]
    cur = settings()
    prov = body.get("provider") or cur["provider"]
    if prov not in ids:
        return 400, {"ok": False, "error": f"provider {prov!r} is not found on this computer"}
    changed = prov != cur["provider"]
    if body.get("model") is not None:
        if not _model(prov, body["model"]):
            return 400, {"ok": False, "error": f"{PROVIDER_NAMES[prov]} has no model {body['model']!r}"}
        model = body["model"]
    else:
        model = _default_model(prov) if changed else cur["model"]
    if body.get("effort") is not None:
        if body["effort"] not in _model(prov, model)["efforts"]:
            return 400, {"ok": False, "error": f"model {model!r} has no effort {body['effort']!r}"}
        effort = body["effort"]
    else:
        effort = _effort_for(prov, model, DEFAULT_EFFORT if changed else cur["effort"])
    raw = _read_json(_settings_file(), None)
    _save_settings({"provider": prov, "model": model, "effort": effort},
                   raw if isinstance(raw, dict) else None)
    return 200, settings_state()


def commands_for(provider):
    return [dict(c) for c in CODEX_COMMANDS] if provider == "codex" else commands()


def _agent_env(name="", doc=None):
    """The bridge env for an agent: no markers of an agent the bridge itself runs in."""
    env = dict(os.environ)
    for k in ("CLAUDECODE", "CODEX_THREAD_ID", "CODEX_SESSION_ID", "CODEX_CI"):
        env.pop(k, None)
    if doc is not None:
        env.update({"FIGMOSHA_TARGET": doc, "FIGMOSHA_AGENT": "chat",
                    "FIGMOSHA_FILE": name, "PYTHONIOENCODING": "utf-8"})
    return env


# ─── session ──────────────────────────────────────────────────────────────

class Session:
    def __init__(self, doc):
        self.doc = doc
        self.proc = None
        self.busy = False
        self.stopping = False
        self.activity = []
        self.inbox = asyncio.Queue()
        self.worker = None
        self.reader = None
        self.turn = None            # future: the running turn has ended
        self.sent = False           # the turn's message reached Claude
        self.stderr = []
        self.api_error = None       # kind of the API error Claude Code reported in this turn
        self.lost = False           # --resume found no such session
        self.disk_warned = False
        self.session_id = _sessions().get(doc)            # Claude Code's session
        self.thread_id = _sessions("codex").get(doc)      # Codex's thread
        self.cproc = None           # the running codex process (one per turn)
        self.running = None         # the provider of the running turn
        self.want = None            # (model, effort) for Claude, from the settings
        self.proc_key = None        # (lang, model, effort) the claude process was started with
        self.codex_error = None     # the last error event codex printed in this turn
        self.picked = False         # the worker took a message and has not finished it
        h = _read_json(self._history_file(), {})
        h = h if isinstance(h, dict) else {}
        msgs = h.get("messages") if isinstance(h.get("messages"), list) else []
        self.messages = [m for m in msgs if isinstance(m, dict) and type(m.get("id")) is int]
        top = max((m["id"] for m in self.messages), default=0) + 1
        nid = h.get("next_id")
        self.next_id = max(nid, top) if type(nid) is int else top
        self.lang = h.get("lang") if h.get("lang") in LANGS else "uk"
        # The language changed after the agent spoke: the next message tells it once (see send).
        self.lang_note = h.get("lang_note") is True
        # (provider, model) of the file's last turn, for the switch note. Older files: Claude on Opus.
        agent = h.get("agent")
        if isinstance(agent, list) and len(agent) == 2 and all(isinstance(x, str) for x in agent):
            self.last_agent = tuple(agent)
        elif any(m.get("from") == "claude" for m in self.messages):
            self.last_agent = ("claude", "opus")
        else:
            self.last_agent = None

    def _history_file(self):
        return DATA_DIR / "history" / f"{self.doc}.json"

    def save(self):
        try:
            data = {"next_id": self.next_id, "messages": self.messages, "lang": self.lang}
            if self.lang_note:
                data["lang_note"] = True
            if self.last_agent:
                data["agent"] = list(self.last_agent)
            _write_json(self._history_file(), data)
            return True
        except OSError as e:
            print(f"[chat] cannot save history of {self.doc}: {e!r}", file=sys.stderr, flush=True)
            return False

    def state(self):
        st = settings_state()
        prov = st["settings"]["provider"]
        return {"ok": True, "doc": self.doc, "messages": self.messages, "busy": self.busy,
                "activity": self.activity,
                "session": self.thread_id if prov == "codex" else self.session_id,
                "commands": commands_for(prov), "lang": self.lang,
                "providers": st["providers"], "settings": st["settings"], "models": st["models"]}

    def set_lang(self, lang):
        """The plugin's language setting. Anything but uk or en is ignored."""
        if lang in LANGS and lang != self.lang:
            self.lang = lang
            # --resume keeps the old talk, and the agent follows its language more than
            # the system prompt. A chat with no agent reply yet needs no note.
            self.lang_note = any(m.get("from") not in ("me", "system") for m in self.messages)
            self.save()

    def t(self, key, **kw):
        return tr(self.lang, key, **kw)

    def find(self, mid):
        return next((m for m in self.messages if m["id"] == mid), None)

    async def push(self, event):
        msg = json.dumps({"type": "chat", "doc": self.doc, "event": event}, ensure_ascii=False)
        for ws, _ in _peers(self.doc):
            try:
                await ws.send_str(msg)
            except Exception:
                pass

    async def add(self, frm, text, **extra):
        m = {"id": self.next_id, "from": frm, "text": text, "t": int(time.time())}
        m.update({k: v for k, v in extra.items() if v not in (None, [], "")})
        self.next_id += 1
        self.messages.append(m)
        if len(self.messages) > HISTORY_LIMIT:
            del self.messages[:-HISTORY_LIMIT]
        saved = self.save()
        await self.push({"kind": "message", "message": m})
        if not saved and not self.disk_warned:    # once: the chat still works, from memory
            self.disk_warned = True
            await self.add("system", self.t("disk"), error=True)
        return m

    async def set_busy(self, busy):
        if self.busy == busy:
            return
        self.busy = busy
        if not busy:
            self.activity = []
        await self.push({"kind": "busy", "busy": busy})

    # ── process ──

    def _argv(self):
        name = next((n for _, n in _peers(self.doc) if n), "") or ""
        argv = list(CLAUDE_CMD) + [
            "-p", "--input-format", "stream-json", "--output-format", "stream-json",
            "--verbose", "--include-partial-messages", "--dangerously-skip-permissions",
            "--model", self._claude_want()[0], "--effort", self._claude_want()[1],
            "--append-system-prompt", SYSTEM_PROMPT.format(name=name or self.doc, reply=self.t("reply")),
        ]
        if self.session_id:
            argv += ["--resume", self.session_id]
        found = find_claude()
        if found:       # the found exe with the given extra args; else the bare name (FileNotFoundError)
            argv = found + argv[len(CLAUDE_CMD):]
        exe = argv[0]
        env = dict(os.environ)
        env.pop("CLAUDECODE", None)      # the bridge may itself run inside Claude Code
        env.update({"FIGMOSHA_TARGET": self.doc, "FIGMOSHA_AGENT": "chat",
                    "FIGMOSHA_FILE": name, "PYTHONIOENCODING": "utf-8"})
        return [exe] + argv[1:], _path_with(env, exe)

    def _claude_want(self):
        return self.want or ("opus", DEFAULT_EFFORT)

    def _claude_key(self):
        return (self.lang,) + tuple(self._claude_want())

    async def _start(self):
        argv, env = self._argv()
        self.stderr = []
        self.proc_key = self._claude_key()
        self.proc = await asyncio.create_subprocess_exec(
            *argv, cwd=str(WORKDIR), env=env,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, limit=64 * 1024 * 1024)
        self.reader = asyncio.create_task(self._read(self.proc))

    async def _write(self, obj):
        self.proc.stdin.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
        await self.proc.stdin.drain()

    def _end_turn(self, error=None):
        if self.turn is not None and not self.turn.done():
            self.turn.set_result(error)

    async def _read_stderr(self, proc):
        while True:
            line = await proc.stderr.readline()
            if not line:
                return
            self.stderr.append(line.decode("utf-8", "replace").rstrip())
            del self.stderr[:-20]

    async def _read(self, proc):
        err_task = asyncio.create_task(self._read_stderr(proc))
        try:
            while True:
                try:
                    line = await proc.stdout.readline()
                except ValueError:       # a line over the limit: skip it
                    continue
                if not line:
                    break
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(ev, dict):
                    continue
                try:
                    await self._on_event(ev)
                except Exception as e:   # an odd event must not stop the reader
                    print(f"[chat] skipped event: {e!r}", file=sys.stderr, flush=True)
        except (asyncio.CancelledError, ConnectionError):
            pass
        finally:
            code = await proc.wait()
            try:
                await asyncio.wait_for(err_task, 2)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
            if self.proc is proc:
                self.proc = None
            if self.turn is not None and not self.turn.done():
                tail = "\n".join(self.stderr[-5:])
                if LOST_SESSION in tail:
                    self.lost = True
                self._end_turn(None if self.stopping
                               else self.t("died", code=code, tail=tail).strip())

    async def _on_event(self, ev):
        sid = ev.get("session_id")
        if sid and sid != self.session_id:
            self.session_id = sid
            _save_session_id(self.doc, sid)
        t = ev.get("type")
        if t == "system":
            await self._on_system(ev)
        elif t == "stream_event":
            e = ev.get("event") or {}
            d = e.get("delta") or {}
            if e.get("type") == "content_block_delta" and d.get("type") == "text_delta":
                await self.push({"kind": "delta", "text": d.get("text", "")})
        elif t == "assistant":
            if ev.get("is_api_error_message") or ev.get("error"):
                # Not logged in, limits, overload: the CLI's own English line. The
                # result that follows carries it; the designer gets it in Ukrainian.
                self.api_error = str(ev.get("error") or "") or None
                return
            for b in (ev.get("message") or {}).get("content") or []:
                if b.get("type") == "text" and b.get("text", "").strip():
                    clean, actions, questions = parse_reply(b["text"])
                    await self.add("claude", clean, actions=actions, questions=questions)
                elif b.get("type") == "tool_use":
                    line = activity_line(b)
                    self.activity.append({"text": line, "t": int(time.time())})
                    del self.activity[:-ACTIVITY_LIMIT]
                    await self.push({"kind": "activity", "text": line})
        elif t == "result":
            err = None
            if ev.get("is_error") and not self.stopping:
                errs = ev.get("errors") if isinstance(ev.get("errors"), list) else []
                raw = ev.get("result") if isinstance(ev.get("result"), str) else ""
                raw = raw or "\n".join(str(x) for x in errs if x)
                if LOST_SESSION in raw:
                    self.lost = True
                sub = ev.get("subtype")
                kind = self.api_error or (sub if sub not in ("success", "error_during_execution") else None)
                err = error_text(kind, raw, self.lang)
            self.api_error = None
            self._end_turn(err)

    async def _on_system(self, ev):
        sub = ev.get("subtype")
        listed = (ev.get("slash_commands") if sub == "init"
                  else ev.get("commands") if sub == "commands_changed" else None)
        if isinstance(listed, list) and _merge_commands(listed):
            await self.push({"kind": "commands", "commands": commands()})
        elif sub == "status" and ev.get("status") == "compacting":
            line = self.t("compacting")
            self.activity.append({"text": line, "t": int(time.time())})
            await self.push({"kind": "activity", "text": line})
        elif sub == "compact_boundary":
            await self.add("system", self.t("compacted"))

    async def _new_session(self, provider="claude"):
        """/clear: forget that provider's session (and end Claude). The chat history stays."""
        if provider == "codex":
            self.thread_id = None
            _save_session_id(self.doc, None, "codex")
        else:
            await self._close_proc()
            self.session_id = None
            _save_session_id(self.doc, None)
        await self.add("system", self.t("cleared", agent="Codex" if provider == "codex" else "Claude"))

    async def _close_proc(self):
        """End the process gently and wait for its reader. The session id stays."""
        proc = self.proc
        if proc is not None and proc.returncode is None:
            try:
                proc.stdin.close()
            except Exception:
                pass
            try:
                await asyncio.wait_for(proc.wait(), 3)
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                await proc.wait()
        if self.reader is not None:     # its last events must not bring the old id back
            try:
                await asyncio.wait_for(asyncio.shield(self.reader), 3)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
        self.proc = None

    async def _turn(self, content):
        """One turn: start Claude if needed, send, wait for the end. Returns the error or None."""
        if self.proc is not None and self.proc.returncode is None and self.proc_key != self._claude_key():
            # The language (system prompt), model and effort are fixed at start: restart, --resume keeps the talk.
            await self._close_proc()
        self.turn = asyncio.get_running_loop().create_future()
        self.sent = False
        self.lost = False
        self.api_error = None
        started = False
        try:
            if self.proc is None or self.proc.returncode is not None:
                await self._start()
            started = True
            if self.stopping:         # Stop can land while Claude is starting
                return None
            await self._write({"type": "user", "session_id": self.session_id or "",
                               "message": {"role": "user", "content": content},
                               "parent_tool_use_id": None})
            self.sent = True
            return await self.turn
        except FileNotFoundError as e:
            print(f"[chat] claude not found: {e!r} argv0={self._argv()[0][0]!r} cwd={WORKDIR}", file=sys.stderr, flush=True)
            return self.t("no_claude", cmd=CLAUDE_CMD[0])
        except (OSError, ConnectionError) as e:
            if started:   # it died under the write: its own result or stderr says more
                try:
                    return await asyncio.wait_for(asyncio.shield(self.turn), 5)
                except asyncio.TimeoutError:
                    pass
            return self.t("no_start", e=e)
        except Exception as e:        # never leave the chat busy
            print(f"[chat] turn failed: {e!r}", file=sys.stderr, flush=True)
            return self.t("broken", e=e)
        finally:
            self.turn = None

    async def _forget_session(self):
        """--resume found nothing: end that process and start the next one fresh."""
        if self.running == "codex":
            self.thread_id = None
            _save_session_id(self.doc, None, "codex")
            return
        proc = self.proc
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        if self.reader:
            try:
                await asyncio.wait_for(asyncio.shield(self.reader), 5)
            except asyncio.TimeoutError:
                pass
        self.proc = None
        self.session_id = None
        _save_session_id(self.doc, None)

    async def _agent_turn(self, job, cfg):
        """One turn on the provider of the settings at the time the turn starts."""
        prov, model = cfg["provider"], cfg["model"]
        self.running = prov
        if prov == "codex":
            if self.proc is not None:      # Claude waits idle; --resume brings it back later
                await self._close_proc()
            return await self._codex_turn(job, model, cfg["effort"])
        self.want = (model, cfg["effort"])
        return await self._turn(job.content)

    async def _note_switch(self, cfg):
        now = (cfg["provider"], cfg["model"])
        if self.last_agent == now:
            return
        was, self.last_agent = self.last_agent, now
        if was is None:               # a file's first turn: nothing to compare with
            self.save()
            return
        await self.add("system", self.t("switched", provider=PROVIDER_NAMES[now[0]],
                                         model=model_name(*now)))

    async def _run(self):
        while True:
            job = await self.inbox.get()
            self.picked = True
            if isinstance(job, _Clear):
                if not self.stopping:
                    await self._new_session(job.provider)
                self.picked = False
                if self.inbox.empty():
                    self.stopping = False
                    await self.set_busy(False)
                continue
            if not providers():
                error = self.t("no_provider")
            else:
                cfg = settings()
                if not self.stopping:
                    await self._note_switch(cfg)
                error = None if self.stopping else await self._agent_turn(job, cfg)
                if self.lost and not self.stopping:
                    await self._forget_session()
                    await self.add("system", self.t("lost"))
                    error = await self._agent_turn(job, cfg)
            self.running = None
            self.picked = False
            if error:
                await self.add("system", error, error=True)
            if self.inbox.empty():
                self.stopping = False
                await self.set_busy(False)
            else:
                self.activity = []

    def ensure_worker(self):
        if self.worker is None or self.worker.done():
            self.worker = asyncio.create_task(self._run())

    async def stop(self):
        if not self.busy:
            return
        self.stopping = True
        while not self.inbox.empty():   # Esc: queued messages are not sent either
            self.inbox.get_nowait()
        turn = self.turn
        if turn is None and not self.picked:   # nothing picked up yet: just go idle
            self.stopping = False
            await self.set_busy(False)
            return
        if turn is None:              # picked up, not started: the worker skips it
            return
        if turn.done() or not self.sent:   # not sent yet: the worker skips it
            return
        if self.running == "codex":        # one process per turn: end it
            await self._stop_codex(turn)
            return
        try:
            await self._write({"type": "control_request", "request_id": uuid.uuid4().hex,
                               "request": {"subtype": "interrupt"}})
        except Exception:
            pass
        try:
            await asyncio.wait_for(asyncio.shield(turn), STOP_GRACE)
        except asyncio.TimeoutError:
            proc = self.proc
            if proc is not None and proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
            try:
                await asyncio.wait_for(asyncio.shield(turn), 5)
            except asyncio.TimeoutError:
                self._end_turn(None)

    # ── codex: one process per turn ──

    def _codex_argv(self, job, model, effort):
        name = next((n for _, n in _peers(self.doc) if n), "") or ""
        prompt = SYSTEM_PROMPT.format(name=name or self.doc, reply=self.t("reply"))
        base = find_codex() or list(CODEX_CMD)
        argv = base + ["exec"] + (["resume"] if self.thread_id else [])
        for path in job.files:        # first: -i takes several values, so it must not touch the thread id
            argv += ["-i", path]
        argv += ["--json", "--skip-git-repo-check", "--dangerously-bypass-approvals-and-sandbox",
                 "-m", model, "-c", f"model_reasoning_effort={effort}",
                 "-c", "developer_instructions=" + json.dumps(prompt)]
        if self.thread_id:
            argv.append(self.thread_id)
        argv.append("-")
        return argv, _path_with(_agent_env(name, self.doc), argv[0])

    async def _codex_turn(self, job, model, effort):
        """Start codex exec for one message, wait for the end. Returns the error or None."""
        self.turn = asyncio.get_running_loop().create_future()
        self.sent = False
        self.lost = False
        self.codex_error = None
        self.stderr = []
        try:
            argv, env = self._codex_argv(job, model, effort)
            try:
                proc = await asyncio.create_subprocess_exec(
                    *argv, cwd=str(WORKDIR), env=env,
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE, limit=64 * 1024 * 1024,
                    **_GROUP)
            except OSError as e:
                print(f"[chat] codex did not start: {e!r} argv0={argv[0]!r}", file=sys.stderr, flush=True)
                return self.t("codex_no_start", e=e)
            self.cproc = proc
            self.reader = asyncio.create_task(self._read_codex(proc))
            if self.stopping:         # Stop landed while codex was starting
                await self._stop_codex(self.turn)
                return None
            self.sent = True          # from here a Stop ends the process (no gap during drain)
            try:
                proc.stdin.write(job.prompt.encode("utf-8"))
                await proc.stdin.drain()
                proc.stdin.close()
            except (OSError, ConnectionError):
                pass                  # it died at once: the reader reports the stderr
            return await self.turn
        except Exception as e:        # never leave the chat busy
            print(f"[chat] codex turn failed: {e!r}", file=sys.stderr, flush=True)
            return self.t("broken", e=e)
        finally:
            if self.reader is not None and self.cproc is not None:
                try:
                    await asyncio.wait_for(asyncio.shield(self.reader), 5)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    pass
            self.cproc = None
            self.turn = None

    async def _stop_codex(self, turn):
        """Terminate the turn's process; kill it after STOP_GRACE. The turn ends with no line."""
        proc = self.cproc
        if proc is not None and proc.returncode is None:
            _signal_tree(proc, kill=False)
        try:
            await asyncio.wait_for(asyncio.shield(turn), STOP_GRACE)
            return
        except asyncio.TimeoutError:
            pass
        if proc is not None:          # the wrapper may be gone while its child still runs
            _signal_tree(proc, kill=True)
        try:
            await asyncio.wait_for(asyncio.shield(turn), 5)
        except asyncio.TimeoutError:
            self._end_turn(None)

    async def _read_codex(self, proc):
        err_task = asyncio.create_task(self._read_stderr(proc))
        shown = set()
        try:
            while True:
                try:
                    line = await proc.stdout.readline()
                except ValueError:
                    continue
                if not line:
                    break
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(ev, dict):
                    continue
                try:
                    await self._on_codex_event(ev, shown)
                except Exception as e:
                    print(f"[chat] skipped codex event: {e!r}", file=sys.stderr, flush=True)
        except (asyncio.CancelledError, ConnectionError):
            pass
        finally:
            code = await proc.wait()
            try:
                await asyncio.wait_for(err_task, 2)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
            if self.turn is not None and not self.turn.done():
                tail = "\n".join(self.stderr[-5:])
                if LOST_THREAD in tail.lower() and self.thread_id:
                    self.lost = True
                if self.stopping or self.lost:
                    self._end_turn(None)
                elif self.codex_error:
                    self._end_turn(self.codex_error)
                else:
                    self._end_turn(self.t("codex_died", code=code, tail=tail).strip())

    def _codex_error_text(self, raw):
        """The error line, with the API error code and message from codex's JSON when it has one."""
        raw = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
        detail = raw.strip()
        try:
            data = json.loads(raw)
        except ValueError:
            data = None
        if isinstance(data, dict):
            e = data.get("error") if isinstance(data.get("error"), dict) else data
            parts = [str(e.get(k)) for k in ("code", "message") if e.get(k)]
            if parts:
                detail = ": ".join(parts)
        return f"{self.t('codex_error')}\n{detail}" if detail else self.t("codex_error")

    async def _codex_activity(self, line):
        line = " ".join(str(line).split())
        line = line if len(line) <= 120 else line[:119] + "…"
        self.activity.append({"text": line, "t": int(time.time())})
        del self.activity[:-ACTIVITY_LIMIT]
        await self.push({"kind": "activity", "text": line})

    async def _on_codex_event(self, ev, shown):
        t = ev.get("type")
        if t == "thread.started":
            tid = ev.get("thread_id")
            if isinstance(tid, str) and tid and tid != self.thread_id:
                self.thread_id = tid
                _save_session_id(self.doc, tid, "codex")
        elif t in ("item.started", "item.completed", "item.updated"):
            item = ev.get("item")
            if not isinstance(item, dict) or not isinstance(item.get("type"), str):
                return
            kind, iid = item["type"], item.get("id")
            if t == "item.completed" and kind == "agent_message":
                clean, actions, questions = parse_reply(str(item.get("text") or ""))
                if clean.strip() or actions or questions:
                    await self.add("claude", clean, actions=actions, questions=questions, agent="codex")
            elif kind in ("agent_message", "error") or t == "item.updated":
                return                # a warning, or a part of a message: nothing to show
            elif iid is None or iid not in shown:
                if iid is not None:
                    shown.add(iid)
                await self._codex_activity(codex_command(item.get("command"))
                                           if kind == "command_execution" else kind)
        elif t == "error":
            self.codex_error = self._codex_error_text(ev.get("message") or "")
        elif t == "turn.failed":
            err = ev.get("error") if isinstance(ev.get("error"), dict) else {}
            msg = err.get("message") or self.codex_error or ""
            self._end_turn(None if self.stopping else
                           (self._codex_error_text(msg) if msg else self.t("codex_error")))
        elif t == "turn.completed":
            self._end_turn(None)

    async def close(self):
        was_busy = self.busy
        for task in (self.worker,):
            if task:
                task.cancel()
        cproc = self.cproc
        if cproc is not None:
            if cproc.returncode is None:
                _signal_tree(cproc, kill=False)
                try:
                    await asyncio.wait_for(cproc.wait(), 2)
                except asyncio.TimeoutError:
                    pass
            _signal_tree(cproc, kill=True)
            await cproc.wait()
        proc = self.proc
        if proc is not None and proc.returncode is None:
            try:
                proc.stdin.close()
            except Exception:
                pass
            try:
                await asyncio.wait_for(proc.wait(), 2)
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                await proc.wait()
        if self.reader:
            try:
                await asyncio.wait_for(self.reader, 2)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
        if was_busy:          # the next bridge starts idle: say why the answer is missing
            self.busy = False
            await self.add("system", self.t("bridge_down"), error=True)


# Codex from npm is a wrapper (node codex.js, or codex.cmd) that runs the native binary as a
# child. A kill of the wrapper alone leaves that child running, with the stdout pipe open.
# So codex starts in its own process group (POSIX) and a stop signals the whole tree.
_GROUP = {"start_new_session": True} if os.name != "nt" else {}


def _signal_tree(proc, kill):
    """SIGTERM (or SIGKILL) to the process and its children. Windows: taskkill /T."""
    if os.name == "nt":
        if proc.returncode is not None and not kill:
            return
        import subprocess
        try:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T"] + (["/F"] if kill else []),
                           capture_output=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            pass
        if kill and proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        return
    import signal
    try:
        os.killpg(proc.pid, signal.SIGKILL if kill else signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        if proc.returncode is None:
            try:
                proc.kill() if kill else proc.terminate()
            except ProcessLookupError:
                pass


class _Clear:
    """Inbox marker: start a new session of that provider (/clear)."""

    def __init__(self, provider):
        self.provider = provider


class _Job:
    """One message: Claude's content blocks, and the prompt and photo files for Codex."""

    def __init__(self, content, prompt, files):
        self.content, self.prompt, self.files = content, prompt, files


def codex_command(cmd):
    """A codex shell command for the activity strip, without the shell wrapper."""
    if isinstance(cmd, list):
        cmd = " ".join(str(x) for x in cmd)
    cmd = str(cmd or "command")
    m = re.match(r"""^\S*?(?:bash|zsh|sh|pwsh|powershell)(?:\.exe)?\s+(?:-\w+\s+)*?-\w*c\w*\s+(['"]?)(.*)\1$""",
                 cmd.strip(), re.S | re.I)
    return m.group(2) if m and m.group(2).strip() else cmd


def session(doc):
    s = SESSIONS.get(doc)
    if s is None:
        s = SESSIONS[doc] = Session(doc)
    return s


async def shutdown(app=None):
    for s in list(SESSIONS.values()):
        await s.close()


# ─── sending ──────────────────────────────────────────────────────────────

def _save_images(images):
    """Valid image data URLs -> [(url, media_type, base64)]. Anything else is dropped."""
    out = []
    if not isinstance(images, list):
        return out
    for img in images[:MAX_IMAGES]:
        if not isinstance(img, str):
            continue
        m = DATA_URL_RE.match(img)
        if not m:
            continue
        kind, b64 = m.group(1), re.sub(r"\s", "", m.group(2))
        try:
            raw = base64.b64decode(b64, validate=True)
        except (binascii.Error, ValueError):
            continue
        if not raw or len(raw) > MAX_IMAGE_BYTES:
            continue
        ext = "jpg" if kind in ("jpeg", "jpg") else kind
        media = "image/jpeg" if ext == "jpg" else f"image/{kind}"
        name = f"{uuid.uuid4().hex}.{ext}"
        try:
            _uploads().mkdir(parents=True, exist_ok=True)
            (_uploads() / name).write_bytes(raw)
        except OSError as e:
            print(f"[chat] cannot save a photo: {e!r}", file=sys.stderr, flush=True)
            continue
        out.append((f"/chat/uploads/{name}", media, b64))
    return out


SENDING = 0     # chat.send calls in flight: the bridge does not restart under them


async def send(doc, body):
    """Store the designer's message and queue it for Claude. Returns (status, json)."""
    global SENDING
    SENDING += 1
    try:
        return await _send(doc, body)
    finally:
        SENDING -= 1


async def _send(doc, body):
    if not valid_doc(doc):
        return 400, {"ok": False, "error": "'doc' must be the file's doc id"}
    text = body.get("text") if isinstance(body.get("text"), str) else ""
    text = text.strip()
    images = _save_images(body.get("images"))
    if not text and not images:
        return 400, {"ok": False, "error": "empty message"}

    s = session(doc)
    s.set_lang(body.get("lang"))
    quote = answer = None
    q_id, ans = body.get("quote"), body.get("answer")
    if isinstance(ans, dict) and isinstance(ans.get("msg"), int) and isinstance(ans.get("i"), int) \
            and not isinstance(ans.get("msg"), bool) and not isinstance(ans.get("i"), bool):
        target = s.find(ans["msg"])
        qs = (target or {}).get("questions") or []
        if 0 <= ans["i"] < len(qs):
            answer = {"msg": ans["msg"], "i": ans["i"]}
            quote = {"id": target["id"], "from": target["from"], **({"agent": target["agent"]} if target.get("agent") else {}),
                     "text": s.t("question", n=ans["i"] + 1, q=qs[ans["i"]])}
    if quote is None and isinstance(q_id, int) and not isinstance(q_id, bool):
        target = s.find(q_id)
        if target:
            quote = {"id": target["id"], "from": target["from"], **({"agent": target["agent"]} if target.get("agent") else {}), "text": target["text"]}

    m = await s.add("me", text, images=[u for u, _, _ in images], quote=quote, answer=answer)
    dropped = len(body["images"]) - len(images) if isinstance(body.get("images"), list) else 0
    if dropped > 0:
        await s.add("system", s.t("photo_dropped") if dropped == 1
                    else s.t("photos_dropped", n=dropped), error=True)

    if not providers():                 # no agent on this computer: say how to get one
        await s.add("system", s.t("no_provider"), error=True)
        return 200, {"ok": True, "id": m["id"]}
    provider = settings()["provider"]
    cmd = slash_command(text) if not images and not answer else None
    if cmd in CLEAR_COMMANDS:
        await s.set_busy(True)
        s.inbox.put_nowait(_Clear(provider))   # in order: after the turns queued before it
        s.ensure_worker()
        return 200, {"ok": True, "id": m["id"]}
    if cmd in TERMINAL_COMMANDS:
        await s.add("system", s.t("terminal", cmd=cmd))
        return 200, {"ok": True, "id": m["id"]}
    if cmd in LOCKED_COMMANDS:
        await s.add("system", s.t("locked", cmd=cmd))
        return 200, {"ok": True, "id": m["id"]}
    if cmd and provider == "codex":     # codex exec has no slash commands
        await s.add("system", s.t("codex_refused", cmd=cmd))
        return 200, {"ok": True, "id": m["id"]}
    known = known_commands()
    if cmd and known is not None and not _is_known(cmd, known):
        await s.add("system", s.t("unknown", cmd=cmd))
        return 200, {"ok": True, "id": m["id"]}
    if cmd:
        quote = None            # Claude must see the command first, as typed

    prefix = ""
    if answer:
        prefix = s.t("answer_to", q=quote["text"])
    elif quote:
        prefix = s.t("quoted", q="\n".join("> " + x for x in quote["text"].splitlines()))
    if images:
        content = [{"type": "image", "source": {"type": "base64", "media_type": media, "data": b64}}
                   for _, media, b64 in images]
        if prefix:
            content.append({"type": "text", "text": prefix.rstrip()})
        if text:
            content.append({"type": "text", "text": text})
    else:
        content = prefix + text
    prompt = prefix + text
    if s.lang_note:     # a separate last block: the message itself still comes first
        note = s.t("lang_switched")
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        content.append({"type": "text", "text": note})
        prompt += "\n\n" + note
        s.lang_note = False
        s.save()
    files = [str(_uploads() / u.rsplit("/", 1)[-1]) for u, _, _ in images]

    await s.set_busy(True)
    s.inbox.put_nowait(_Job(content, prompt, files))
    s.ensure_worker()
    return 200, {"ok": True, "id": m["id"]}


async def stop(doc):
    s = SESSIONS.get(doc)
    if s is not None:
        await s.stop()


def state(doc, lang=None):
    s = session(doc)
    s.set_lang(lang)
    return s.state()


def status_of(doc):
    s = SESSIONS.get(doc)
    return {"busy": s.busy} if s is not None else None


# ─── HTTP ─────────────────────────────────────────────────────────────────

def _blocked(request):
    return _guard(request, allow_null_origin=True) if _guard else None


async def _body(request):
    try:
        b = await request.json()
    except (ValueError, UnicodeDecodeError):
        b = None
    return b if isinstance(b, dict) else {}


async def history_handler(request):
    blocked = _blocked(request)
    if blocked is not None:
        return blocked
    doc = request.query.get("doc")
    if not valid_doc(doc):
        return web.json_response({"ok": False, "error": "'doc' must be the file's doc id"},
                                 status=400)
    return web.json_response(state(doc))


async def send_handler(request):
    blocked = _blocked(request)
    if blocked is not None:
        return blocked
    body = await _body(request)
    status, out = await send(body.get("doc"), body)
    return web.json_response(out, status=status)


async def stop_handler(request):
    blocked = _blocked(request)
    if blocked is not None:
        return blocked
    body = await _body(request)
    doc = body.get("doc")
    if not valid_doc(doc):
        return web.json_response({"ok": False, "error": "'doc' must be the file's doc id"},
                                 status=400)
    await stop(doc)
    return web.json_response({"ok": True})


async def upload_handler(request):
    blocked = _blocked(request)
    if blocked is not None:
        return blocked
    name = request.match_info.get("name", "")
    if not UPLOAD_RE.match(name):
        return web.json_response({"ok": False, "error": "bad file name"}, status=404)
    path = _uploads() / name
    if not path.is_file():
        return web.json_response({"ok": False, "error": "not found"}, status=404)
    return web.FileResponse(path, headers={"Cache-Control": "private, max-age=86400"})


async def settings_handler(request):
    blocked = _blocked(request)
    if blocked is not None:
        return blocked
    if request.method == "GET":
        return web.json_response(settings_state())
    status, out = set_settings(await _body(request))
    if status == 200:
        await broadcast_settings()
    return web.json_response(out, status=status)


async def recheck_handler(request):
    blocked = _blocked(request)
    if blocked is not None:
        return blocked
    out = recheck()
    await broadcast_settings()
    return web.json_response(out)


def settings_event(st=None, error=None):
    st = st or settings_state()
    ev = {"kind": "settings", "providers": st["providers"], "settings": st["settings"],
          "models": st["models"], "commands": commands_for(st["settings"]["provider"])}
    if error:
        ev["error"] = error
    return ev


async def _send_event(ws, doc, event):
    try:
        await ws.send_str(json.dumps({"type": "chat", "doc": doc, "event": event}, ensure_ascii=False))
    except Exception:
        pass


async def broadcast_settings():
    """A settings change or a recheck: every open plugin tab gets the new settings event."""
    ev = settings_event()
    for ws, doc in _tabs():
        await _send_event(ws, doc, ev)


SETTINGS_TYPES = ("chat-settings", "chat-settings-set", "chat-recheck", "chat-detect")


async def from_plugin(ws, doc, m):
    """The settings over the plugin socket. chat-settings with a field is a set, like chat-settings-set."""
    t = m.get("type")
    if t in ("chat-recheck", "chat-detect"):
        recheck()
        await broadcast_settings()
    elif t == "chat-settings-set" or any(k in m for k in ("provider", "model", "effort")):
        status, out = set_settings({k: m[k] for k in ("provider", "model", "effort") if k in m})
        if status == 200:
            await broadcast_settings()
        elif valid_doc(doc):
            await _send_event(ws, doc, settings_event(error=out.get("error")))
    elif valid_doc(doc):
        await _send_event(ws, doc, settings_event())


def setup(app, *, guard, peers, tabs=None):
    """Add the chat routes to the bridge app. guard/peers/tabs come from bridge.py."""
    global _guard, _peers, _tabs
    _guard, _peers = guard, peers
    if tabs is not None:
        _tabs = tabs
    app.router.add_get("/chat", history_handler)
    app.router.add_get("/chat/settings", settings_handler)
    app.router.add_post("/chat/settings", settings_handler)
    app.router.add_post("/chat/recheck", recheck_handler)
    app.router.add_post("/chat/send", send_handler)
    app.router.add_post("/chat/stop", stop_handler)
    app.router.add_get("/chat/uploads/{name}", upload_handler)
    app.on_cleanup.append(shutdown)
