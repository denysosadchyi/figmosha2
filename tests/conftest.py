"""Shared test setup: every test, in every file, starts with a clean bridge.

The bridge keeps its state in module globals. Without this reset a test that
pins the Host check, or leaves a plugin registered, leaks into the next test
file — the fixture used to live in test_bridge.py only.
"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import bridge  # noqa: E402


@pytest.fixture(autouse=True)
def clean_state():
    def reset():
        for registry in (bridge.PENDING, bridge.PLUGINS, bridge.LOCKS, bridge.ABANDONED,
                         bridge.QUEUE):
            registry.clear()
        bridge.ALLOWED_HOSTS = set()
        bridge.UPDATE = None
    reset()
    yield
    reset()


# ─── slow tests ───────────────────────────────────────────────────────────
# Mark soak/endurance tests with @pytest.mark.slow. They are skipped unless
# you pass --slow (or set FIGMOSHA_SLOW=1).

def pytest_addoption(parser):
    parser.addoption("--slow", action="store_true", help="also run tests marked slow")


def pytest_configure(config):
    config.addinivalue_line("markers", "slow: long soak test, run with --slow")


def pytest_collection_modifyitems(config, items):
    if config.getoption("--slow") or os.environ.get("FIGMOSHA_SLOW") == "1":
        return
    skip = pytest.mark.skip(reason="slow test: run with --slow")
    for item in items:
        if "slow" in item.keywords:
            item.add_marker(skip)
