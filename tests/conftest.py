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


def make_tk_root(attempts=4, delay=0.25):
    """Create a hidden Tk root, RETRYING before giving up, or skip if there is none.

    Every GUI test module used to open with its own `try: tk.Tk() / except
    tk.TclError: pytest.skip("no Tk display")`. Eight copies, and the copy is not the
    problem: the SKIP is. On a Windows CI runner there is always a display, so a
    TclError there is a transient, not an unsupported environment, and answering a
    transient with `skip` turns it into lost coverage that still reports SUCCESS.

    Measured: two runs of the SAME commit, minutes apart. One skipped 27 tests in
    `test_video_stabilize.py` for "no Tk display" and reported 1414 passed / 52
    skipped; the other ran them all and reported 1441 passed / 25 skipped. 1441 =
    1414 + 27. Nothing failed either time, so the green tick was covering a quarter
    of a test file that never executed, and only a diff of the skip counts showed it.

    So: retry first, and only skip when Tk really cannot start. `skip` stays as the
    last resort rather than `fail` because a genuinely headless environment (a Linux
    contributor, a container) must still be able to run the non-GUI suite.
    """
    import time
    import tkinter as tk

    last = None
    for i in range(max(1, attempts)):
        try:
            r = tk.Tk()
        except tk.TclError as exc:                 # transient, or genuinely no display
            last = exc
            if i + 1 < attempts:
                time.sleep(delay)
            continue
        r.withdraw()
        return r
    pytest.skip("no Tk display after %d attempts (%s)" % (attempts, last))


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


@pytest.fixture(autouse=True)
def _never_touch_the_real_cache_db(monkeypatch, request):
    """Fail any test that opens the REAL db/cache.db instead of a temp one.

    This is not hygiene, it is damage control. A test that calls a runner entry point
    without taking the `db_conn` fixture gets the app's own database, and the runners WRITE:
    one such test silently overwrote four real RTX 5090 benchmark rows with its fake engine's
    numbers, including turning a genuine `oom` at batch 9 into an `ok`. Those rows are
    measured on rented hardware and cost real money to produce, and a fabricated `ok` does
    not just lose data, it makes the sizer pick a batch that OOMs.

    The failure was invisible: the test passed, the suite was green, and nothing pointed at
    the database. So the guard reaches for the EFFECT (was the real path opened?) rather than
    trusting every future test to remember a fixture. `db_conn` repoints DB_PATH before
    connecting, so a test using it never trips this.
    """
    import db as _db
    real = os.path.abspath(_db.DB_PATH)

    def _refuse(*_a, **_k):
        raise AssertionError(
            f"{request.node.name} tried to open the REAL cache database ({real}). "
            "Take the `db_conn` fixture (it repoints db.DB_PATH at tmp_path). Runner entry "
            "points WRITE to whatever connection they find, and this database holds "
            "benchmark results that cost money to measure.")

    orig = _db._open_conn

    def _guarded():
        if os.path.abspath(_db.DB_PATH) == real:
            _refuse()
        return orig()

    monkeypatch.setattr(_db, "_open_conn", _guarded)
    yield


@pytest.fixture
def db_conn(tmp_path, monkeypatch):
    """A throwaway SQLite cache in tmp_path, and the ONLY safe way for a test to reach db.

    Ten test modules already define this fixture privately, byte for byte. It lives here too
    so a NEW module does not have to copy it a eleventh time, which is exactly how a test
    ended up running a benchmark against the app's real database. A module-local copy shadows
    this one, so nothing existing changes.
    """
    import db as _db
    monkeypatch.setattr(_db, "DB_DIR", str(tmp_path / "db"))
    monkeypatch.setattr(_db, "DB_PATH", str(tmp_path / "db" / "cache.db"))
    monkeypatch.setattr(_db, "_conn", None)
    monkeypatch.setattr(_db, "import_legacy_json", lambda conn: None)
    conn = _db.get_conn()
    yield conn
    conn.close()
    monkeypatch.setattr(_db, "_conn", None)
