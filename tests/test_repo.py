"""Repo-level checks that no behaviour test catches.

  * Technical ids must stay byte for byte: Figma keys the installed dev plugin
    by the manifest id, and users' saved settings live under the storage keys.
  * README and docs/DEVELOPERS.md relative links and images must point to
    real files (relative to the doc's own folder).
  * tools/bump_plugin.py (the pre-commit hook) counts PLUGIN_VERSION up right.

    pytest -q tests/test_repo.py
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import bump_plugin  # noqa: E402

STORAGE_KEYS = {
    "UI_MODE_KEY": "figmosha-ui-mode",
    "SETTINGS_KEY": "figmosha-settings",
    "CHAT_SIZE_KEY": "figmosha-chat-size",
    "CHAT_POS_KEY": "figmosha-chat-pos",
    "WELCOME_KEY": "figmosha-welcome",
    "ONBOARD_KEY": "figmosha-onboarded",
    "SKIP_KEY": "figmosha-notice-skip",
}


def test_manifest_id_unchanged():
    manifest = json.loads((ROOT / "plugin/manifest.json").read_text(encoding="utf-8"))
    assert manifest["id"] == "figmosha-bridge-dev"


def test_client_storage_and_plugin_data_keys_unchanged():
    src = (ROOT / "plugin/code.js").read_text(encoding="utf-8")
    for const, value in STORAGE_KEYS.items():
        assert re.search(rf'const {const} = "{re.escape(value)}";', src), f"{const} != {value}"
    assert 'getPluginData("figmosha-doc")' in src and 'setPluginData("figmosha-doc"' in src


MD_LINK = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)[^)]*\)")
IMG_SRC = re.compile(r'<img\b[^>]*\bsrc\s*=\s*"([^"]*)"', re.IGNORECASE)


import pytest  # noqa: E402


@pytest.mark.parametrize("doc", ["README.md", "docs/DEVELOPERS.md"])
def test_doc_relative_links_exist(doc):
    path_doc = ROOT / doc
    text = path_doc.read_text(encoding="utf-8")
    targets = MD_LINK.findall(text) + IMG_SRC.findall(text)
    assert any(t.endswith("CHANGELOG.md") for t in targets), "the link scan found nothing"
    missing = []
    for t in targets:
        if re.match(r"^(?:[a-z][a-z0-9+.-]*:|#|//)", t, re.IGNORECASE):
            continue
        path = t.split("#", 1)[0].split("?", 1)[0]
        if path and not (path_doc.parent / path).exists():
            missing.append(t)
    assert not missing, doc + " points to missing files: " + ", ".join(missing)


def test_bump_counts_up_on_the_same_day_and_restarts_on_a_new_one():
    assert bump_plugin.next_version("2026-11-21.9", "2026-11-21") == "2026-11-21.10"
    assert bump_plugin.next_version("2026-11-21.9", "2026-11-22") == "2026-11-22.1"
    src = 'const PLUGIN_VERSION = "2026-11-21.3";\nconst X = "2026-11-21.3";\n'
    out, old, new = bump_plugin.bump(src, "2026-11-21")
    assert (old, new) == ("2026-11-21.3", "2026-11-21.4")
    assert out == 'const PLUGIN_VERSION = "2026-11-21.4";\nconst X = "2026-11-21.3";\n'
