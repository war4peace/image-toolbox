"""
Regression tests for item 1: the Video Upscaler's POD / RCOST events.

The money bug was two-fold: batch_video_upscale never emitted the POD event, so
the Video tab's `active_pod_id` stayed None and the RunPod tab would happily
terminate a live (paid) video pod; and the tab's POD handler read the raw
`payload` string, which for THIS runner is JSON-encoded (unlike the image runners
whose runner_common.gui_event writes payload raw). So even once POD was emitted,
the pod id would arrive wrapped in quotes.

These tests pin the emitter (batch_video_upscale.gui_event) to the parser
(VideoTab._filter_markers -> _on_marker -> _handle_event) end to end, driving the
REAL parser on a minimal fake tab so no tkinter window is created. Emitter and
parser drifting apart is exactly the class of bug this guards.
"""

import contextlib
import io
import types

import pytest

pytest.importorskip("tkinter")   # tab_video imports tkinter at module load

import batch_video_upscale as bv          # noqa: E402
import runner_common                       # noqa: E402
from gui.tab_video import VideoTab         # noqa: E402


class _FakeApp:
    def __init__(self):
        self.pod_changed = 0

    def notify_active_pods_changed(self):
        self.pod_changed += 1


class FakeVideoTab:
    """Carries just the parser's state; reuses the real VideoTab parsing +
    dispatch methods. Only the POD / RCOST branches of _handle_event are
    exercised, so no other tab attributes are needed."""
    _filter_markers = VideoTab._filter_markers
    _on_marker = VideoTab._on_marker
    _handle_event = VideoTab._handle_event

    def __init__(self):
        self._hold = ""
        self._marker_buf = None
        self.active_pod_id = None
        self._remote_rate = None
        self.app = _FakeApp()

    def feed(self, text):
        return self._filter_markers(text)


def _emit(monkeypatch, *events):
    """Produce the exact wire bytes the runner writes for `events`, using the
    real batch_video_upscale.gui_event (which JSON-encodes its payload and now
    delegates the write to runner_common.gui_event — item 7). Emission is gated on
    runner_common.GUI_MODE, so that is the flag to force on."""
    monkeypatch.setattr(runner_common, "GUI_MODE", True)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        for kind, payload in events:
            bv.gui_event(kind, payload)
    return buf.getvalue()


def test_pod_event_sets_active_pod_id(monkeypatch):
    wire = _emit(monkeypatch, ("POD", "pod_abc123"))
    tab = FakeVideoTab()
    human = tab.feed(wire)
    assert tab.active_pod_id == "pod_abc123"   # decoded, no stray JSON quotes
    assert tab.app.pod_changed == 1
    assert human == ""                          # the whole line was the event


def test_rcost_event_arms_the_live_rate(monkeypatch):
    wire = _emit(monkeypatch, ("RCOST", 0.44))
    tab = FakeVideoTab()
    tab.feed(wire)
    assert tab._remote_rate == pytest.approx(0.44)


def test_empty_pod_event_clears_protection(monkeypatch):
    # On teardown the runner emits POD with an empty id; the tab must drop the
    # pod so the RunPod tab stops protecting a pod that no longer exists.
    tab = FakeVideoTab()
    tab.active_pod_id = "pod_abc123"
    wire = _emit(monkeypatch, ("POD", ""))
    tab.feed(wire)
    assert tab.active_pod_id is None
    assert tab.app.pod_changed == 1


def test_pod_and_rcost_in_one_stream(monkeypatch):
    wire = _emit(monkeypatch, ("POD", "pod_xyz"), ("RCOST", 1.29))
    tab = FakeVideoTab()
    tab.feed(wire)
    assert tab.active_pod_id == "pod_xyz"
    assert tab._remote_rate == pytest.approx(1.29)


def test_gui_event_delegates_to_runner_common(monkeypatch):
    """Item 7: the video runner must not keep a private writer — gui_event has to
    hand the JSON-encoded payload to runner_common.gui_event (the single atomic,
    tee-aware emitter). Pin the delegation so a future refactor can't silently
    re-introduce a drifting private copy."""
    seen = []
    monkeypatch.setattr(runner_common, "gui_event",
                        lambda kind, payload: seen.append((kind, payload)))
    bv.gui_event("VRESULT", ["clip.mp4", "ok", "/out/clip.mp4"])
    assert seen == [("VRESULT", '["clip.mp4", "ok", "/out/clip.mp4"]')]  # JSON string


def test_active_remote_pod_ids_includes_video_tab():
    """App.active_remote_pod_ids must consult the Video tab, or a live video pod
    is never protected from the RunPod tab's Terminate. Asserted structurally
    (no tkinter App is built) by reading the method's source."""
    import inspect
    from gui.app import App
    src = inspect.getsource(App.active_remote_pod_ids)
    assert "video_tab" in src, "video_tab must be in active_remote_pod_ids()"
    idle = inspect.getsource(App._any_task_running)
    assert "video_tab" in idle, "video_tab must be in _any_task_running()"
