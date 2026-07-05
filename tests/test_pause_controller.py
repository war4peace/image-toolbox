"""
Item 10: the upscaler's outage handler no longer reaches into PauseController
privates (`with pause._lock: pause._paused = True; pause._pause_start = ...`) and
the stats dict no longer reads `pause._quit`. It uses the new public
`force_pause()` and the existing `quit_requested` property instead.

These tests pin that public surface: force_pause() actually makes .check() block
(the outage handler relies on this to hold the run open until the user acts), it
is idempotent w.r.t. the pause-time accounting, and quit_requested / check()
reflect a requested quit.

The watcher threads are neutralised and `_available` is forced on so the state
machine is exercised deterministically on any platform (Windows msvcrt vs a piped
Linux CI would otherwise differ). stdlib only.
"""

import threading
import time

import batch_upscale as bu


def _quiet_controller(monkeypatch):
    # No real keyboard/stdin watcher: we drive the state ourselves. Forcing
    # _available on makes .check() active regardless of tty/msvcrt availability.
    monkeypatch.setattr(bu.PauseController, "_watch", lambda self: None)
    monkeypatch.setattr(bu.PauseController, "_watch_stdin", lambda self: None)
    pc = bu.PauseController()
    pc._available = True
    return pc


def test_force_pause_blocks_check_until_resumed(monkeypatch):
    pc = _quiet_controller(monkeypatch)
    assert pc.check() is True               # not paused -> passes straight through

    pc.force_pause()
    result = {}
    t = threading.Thread(target=lambda: result.setdefault("r", pc.check()))
    t.start()
    t.join(0.3)
    assert t.is_alive(), "force_pause() must make .check() block"

    pc._handle_key("p")                     # resume, as the user's Resume would
    t.join(1.0)
    assert not t.is_alive()
    assert result.get("r") is True          # .check() returned True after resume


def test_force_pause_is_idempotent(monkeypatch):
    pc = _quiet_controller(monkeypatch)
    pc.force_pause()
    first_start = pc._pause_start
    time.sleep(0.02)
    pc.force_pause()                        # second call must NOT restart the clock
    assert pc._pause_start == first_start   # else paused_seconds would be corrupted


def test_quit_requested_and_check_reflect_quit(monkeypatch):
    pc = _quiet_controller(monkeypatch)
    assert pc.quit_requested is False
    assert pc.check() is True

    pc._handle_key("q")
    assert pc.quit_requested is True
    assert pc.check() is False              # a requested quit stops the loop


def test_force_pause_then_quit_unblocks_check(monkeypatch):
    # Stop (not Resume) while paused must also release .check(), returning False.
    pc = _quiet_controller(monkeypatch)
    pc.force_pause()
    result = {}
    t = threading.Thread(target=lambda: result.setdefault("r", pc.check()))
    t.start()
    t.join(0.3)
    assert t.is_alive()

    pc._handle_key("q")
    t.join(1.0)
    assert not t.is_alive()
    assert result.get("r") is False
