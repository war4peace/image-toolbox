"""
The Tag & Rename tab shows ONE dual-purpose button: "Pause" during normal
tagging, "Resume" while paused, "Resume after error" while an outage holds the
run open. Those states are mutually exclusive in the runner's loop (it is either
tagging an image or blocked in wait_resume), which is what lets one button serve
both jobs.

The press sends a bare "p" and the RUNNER assigns the meaning, because the GUI's
label lags the run by a pipe hop. These tests pin that: the same command pauses
during normal work, resumes an outage wait, and the unload hook fires only while
actually paused.

The stdin watcher is neutralised and commands are injected through the same
handler it would call, so the state machine is exercised deterministically.
stdlib only.
"""

import threading
import time

import tag_and_rename as tr


def _control(monkeypatch):
    monkeypatch.setattr(tr.RemoteControl, "_watch", lambda self: None)
    c = tr.RemoteControl()
    c.active = True
    return c


def _press(control):
    """Simulate the GUI's dual button: the tab always sends a bare 'p'."""
    control._dual_button()


def test_press_pauses_normal_work_and_fires_the_unload(monkeypatch):
    c = _control(monkeypatch)
    calls = []
    assert c.wait_while_paused(lambda: calls.append("unload")) is True
    assert calls == []                      # not paused -> no unload churn

    _press(c)
    assert c.paused is True
    t = threading.Thread(target=lambda: c.wait_while_paused(
        lambda: calls.append("unload"), lambda: calls.append("reload")))
    t.start()
    t.join(0.3)
    assert t.is_alive(), "a paused run must block between images"
    assert calls == ["unload"], "the GPU must be freed while paused"

    _press(c)                               # same button resumes
    t.join(1.0)
    assert not t.is_alive()
    assert calls == ["unload", "reload"]
    assert c.paused is False


def test_press_during_an_outage_wait_resumes_instead_of_pausing(monkeypatch):
    """The lag window: the button may still read 'Pause' when the runner has just
    entered an outage. The runner's state decides, so the click resumes."""
    c = _control(monkeypatch)
    done = {}
    t = threading.Thread(target=lambda: done.setdefault("r", c.wait_resume()))
    t.start()
    t.join(0.3)
    assert t.is_alive()

    _press(c)
    t.join(1.0)
    assert not t.is_alive()
    assert done.get("r") is True
    assert c.paused is False, "an outage press must not leave the run paused"


def test_outage_supersedes_a_pause_taken_just_before_it(monkeypatch):
    """Pause, then an outage happens before the loop reaches its pause check.
    Resolving the outage must not drop the run straight back into a pause."""
    c = _control(monkeypatch)
    _press(c)
    assert c.paused is True

    t = threading.Thread(target=c.wait_resume)
    t.start()
    t.join(0.3)
    assert c.paused is False                # the outage wait cleared it

    _press(c)
    t.join(1.0)
    assert not t.is_alive()
    assert c.wait_while_paused() is True     # loop continues, not re-blocked


def test_stop_while_paused_ends_the_run(monkeypatch):
    c = _control(monkeypatch)
    _press(c)
    result = {}
    t = threading.Thread(
        target=lambda: result.setdefault("r", c.wait_while_paused()))
    t.start()
    t.join(0.3)
    assert t.is_alive()

    c._stop.set()
    t.join(1.0)
    assert result.get("r") is False


def test_stop_while_paused_skips_the_reload(monkeypatch):
    c = _control(monkeypatch)
    calls = []
    _press(c)
    t = threading.Thread(target=lambda: c.wait_while_paused(
        lambda: calls.append("unload"), lambda: calls.append("reload")))
    t.start()
    t.join(0.3)
    c._stop.set()
    t.join(1.0)
    assert calls == ["unload"]


def test_plain_resume_line_still_releases_an_outage(monkeypatch):
    """Backward compatibility: 'r' (any non-command line) keeps its old meaning."""
    c = _control(monkeypatch)
    done = {}
    t = threading.Thread(target=lambda: done.setdefault("r", c.wait_resume()))
    t.start()
    t.join(0.3)
    c._resume.set()
    t.join(1.0)
    assert done.get("r") is True


def test_pause_hook_failure_never_breaks_the_run(monkeypatch):
    c = _control(monkeypatch)

    def boom():
        raise RuntimeError("ollama said no")

    _press(c)
    result = {}
    t = threading.Thread(
        target=lambda: result.setdefault("r", c.wait_while_paused(boom, boom)))
    t.start()
    t.join(0.3)
    _press(c)
    t.join(1.0)
    assert result.get("r") is True
