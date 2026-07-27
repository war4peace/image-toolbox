"""
Shared pytest fixtures / path setup for the Image Toolbox test suite.

The app is not a package: its modules live flat in scripts/ and import each
other by bare name (`import db`, `import notifications`, ...). So the one thing
every test needs is scripts/ on sys.path. We add it here, once, before any test
module is collected.

The pod/ daemons (deadman.py) live outside scripts/, so we add the repo root too
and import them as `pod.deadman` (pod/ has no __init__.py, but a namespace
package import works on Python 3).
"""

import os
import sys

import pytest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
APP_ROOT = os.path.dirname(_TESTS_DIR)
SCRIPTS_DIR = os.path.join(APP_ROOT, "scripts")

for _p in (SCRIPTS_DIR, APP_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)


@pytest.fixture(autouse=True)
def _block_real_notifications(monkeypatch):
    """Safety net: the test suite must NEVER contact a real Discord/Telegram/ntfy
    endpoint, or the developer's own Home Assistant. The runners resolve their
    notification settings from the developer's live config.json at import (a real
    webhook), so a test that calls send/notify with those settings would fire an
    actual message. Stub every per-backend sender (which notify() dispatches to by
    module-global name) to a no-op, for every test. Tests that assert on
    notify()/gui behaviour monkeypatch at a higher level and still work (their
    patch wins). Fail-safe if notifications is absent.

    ADD A NEW BACKEND'S SENDER HERE the moment it exists: this list is the only
    thing standing between the suite and a real endpoint."""
    try:
        import notifications
    except Exception:
        return
    for _name in ("send_discord", "send_telegram", "send_ntfy", "send_ha_webhook"):
        monkeypatch.setattr(notifications, _name, lambda *a, **k: None, raising=False)
