"""PLUGIN_VERSION must change whenever plugin/ changes.

The bridge tells a user to re-run the plugin only when the running build id
differs from the one in plugin/code.js. Edit plugin/ without bumping it and
every open plugin silently keeps old code while reporting itself current.

tests/plugin_fingerprint.json maps each build id to a hash of plugin/code.js +
plugin/ui.html. This test fails when the code changed under an unchanged id,
and tells you exactly what to do.

    pytest tests/test_plugin_version.py
"""
import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "plugin"
FINGERPRINTS = Path(__file__).resolve().parent / "plugin_fingerprint.json"


def plugin_version():
    src = (PLUGIN / "code.js").read_text(encoding="utf-8")
    m = re.search(r'PLUGIN_VERSION\s*=\s*"([^"]+)"', src)
    assert m, "plugin/code.js has no PLUGIN_VERSION"
    return m.group(1)


def plugin_hash():
    h = hashlib.sha256()
    for name in ("code.js", "ui.html"):
        # Normalize line endings: git may check these out as CRLF on Windows.
        h.update((PLUGIN / name).read_bytes().replace(b"\r\n", b"\n"))
    return h.hexdigest()


def test_plugin_version_bumped_with_plugin_code():
    version, digest = plugin_version(), plugin_hash()
    known = json.loads(FINGERPRINTS.read_text(encoding="utf-8"))

    if version not in known:
        raise AssertionError(
            f"new PLUGIN_VERSION {version!r} — record it in {FINGERPRINTS.name}:\n"
            f'    "{version}": "{digest}"')

    assert known[version] == digest, (
        f"plugin/ changed but PLUGIN_VERSION is still {version!r}. Bump it in "
        f"plugin/code.js (e.g. today's date + .1), then run this test again — "
        f"it will print the line to add to {FINGERPRINTS.name}.")
