#!/usr/bin/env python3
"""Bump PLUGIN_VERSION in plugin/code.js when plugin/ changes.

The bridge tells a user to re-run the plugin only when the running build id
differs from the one in plugin/code.js. Edit plugin/ without a bump and every
open plugin silently keeps old code while reporting itself current.

    python3 tools/bump_plugin.py            # bump now: today's date + counter
    python3 tools/bump_plugin.py --staged   # pre-commit mode (tools/pre-commit)

--staged bumps only when plugin/code.js or plugin/ui.html is staged and the
staged PLUGIN_VERSION still equals the one in HEAD, then stages code.js again.
Enable the hook once per clone:

    git config core.hooksPath tools        # or: cp tools/pre-commit .git/hooks/
"""
import datetime
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CODE = ROOT / "plugin" / "code.js"
VERSION_RE = re.compile(r'(PLUGIN_VERSION\s*=\s*")([^"]+)(")')
WATCHED = ("plugin/code.js", "plugin/ui.html")


def version_of(src):
    m = VERSION_RE.search(src or "")
    return m.group(2) if m else None


def next_version(old, today=None):
    """YYYY-MM-DD.N: the counter goes up on the same day, restarts at 1 on a new day."""
    today = today or datetime.date.today().isoformat()
    m = re.fullmatch(r"(\d{4}-\d{2}-\d{2})\.(\d+)", old or "")
    if m and m.group(1) == today:
        return f"{today}.{int(m.group(2)) + 1}"
    return f"{today}.1"


def bump(src, today=None):
    old = version_of(src)
    if old is None:
        raise SystemExit("plugin/code.js has no PLUGIN_VERSION")
    new = next_version(old, today)
    return VERSION_RE.sub(lambda m: m.group(1) + new + m.group(3), src, count=1), old, new


def git(*args):
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True,
                          encoding="utf-8")


def staged_mode():
    staged = git("diff", "--cached", "--name-only").stdout.split()
    if not any(p in staged for p in WATCHED):
        return 0
    head = git("show", "HEAD:plugin/code.js")
    index = git("show", ":plugin/code.js").stdout
    if head.returncode == 0 and version_of(index) != version_of(head.stdout):
        return 0                       # already bumped by hand
    if git("diff", "--name-only", "--", "plugin/code.js").stdout.strip():
        print("pre-commit: plugin/code.js has unstaged edits. Stage or stash them, "
              "or bump PLUGIN_VERSION by hand.", file=sys.stderr)
        return 1
    src, old, new = bump(index)
    CODE.write_bytes(src.encode("utf-8"))
    git("add", "plugin/code.js")
    print(f"pre-commit: PLUGIN_VERSION {old} -> {new}")
    return 0


def main(argv):
    if "--staged" in argv:
        return staged_mode()
    src, old, new = bump(CODE.read_bytes().decode("utf-8"))
    CODE.write_bytes(src.encode("utf-8"))
    print(f"PLUGIN_VERSION {old} -> {new}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
