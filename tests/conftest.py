"""Shared test setup: every test, in every file, starts with a clean bridge.

The bridge keeps its state in module globals. Without this reset a test that
pins the Host check, or leaves a plugin registered, leaks into the next test
file — the fixture used to live in test_bridge.py only.
"""
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
