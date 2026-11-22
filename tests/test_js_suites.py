"""Runs the Node test files from pytest, so one `pytest -q` checks everything.

Skipped when Node is missing. chat_dom.test.js also needs jsdom
(`npm install --no-save jsdom` in the repo, or JSDOM=/path/to/jsdom).
"""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

TESTS = Path(__file__).parent
NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="Node is not installed")
# Every *.test.js file in tests/ runs, so new suites are picked up without editing this list.
@pytest.mark.parametrize("name", sorted(p.name for p in TESTS.glob("*.test.js")))
def test_node_suite(name):
    r = subprocess.run([NODE, str(TESTS / name)], cwd=TESTS.parent, capture_output=True,
                       text=True, encoding="utf-8", timeout=120, env=os.environ.copy())
    out = r.stdout + r.stderr
    if r.returncode == 0 and out.startswith("SKIP"):
        pytest.skip(out.strip())
    assert r.returncode == 0, out[-4000:]
    assert "FAIL" not in out, out[-4000:]
