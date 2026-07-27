"""
Tests for the Video Upscaler's MQTT / Home Assistant reporting (finding F1 of
docs/coarse-ideas-plan.md).

The gap: `VideoTab` is not a `ToolTab`, so it published NO task state at all and
`batch_video_upscale` emitted no `DONE` event. Home Assistant read `task/name =
idle` and a stale `last_run` right through the longest, most notification-worthy
runs the app does, so an automation on "a run finished" silently never fired for a
video queue.

What is pinned here:
  * `_done_payload`: the runner's end-of-run summary, its shared key names, the
    reductions (a set and a per-file list must not reach a retained MQTT payload),
    the cost sum, and JSON-serialisability.
  * `_run_finished`: the single seam that emits DONE *and* notifies, exactly once
    per run (the grouped / supervised paths suppress their per-pass copies).
  * The wire round-trip: the real emitter -> the real `VideoTab` parser -> the
    tab's `_last_done` -> the `last_run` payload MqttTaskState publishes.
  * The lifecycle: begin -> announce, end -> idle, driven through the real
    `VideoTab` methods on a widget-free fake.
"""

import contextlib
import io
import json

import pytest

pytest.importorskip("tkinter")   # tab_video/tooltab import tkinter at module load

import batch_video_upscale as bv           # noqa: E402
import mqtt_publisher as mp                # noqa: E402
import runner_common                       # noqa: E402
from gui.tab_video import VideoTab         # noqa: E402
from gui.tooltab import MqttTaskState      # noqa: E402


# ── fakes ────────────────────────────────────────────────────────────────────

class _FakeApp:
    """Just enough App for the MQTT seam: a truthy `mqtt` (so last_run is built)
    and a publish that records each batch with its retain/qos flags (the real
    App.mqtt_publish signature; the event topics depend on retain=False)."""

    def __init__(self):
        self.mqtt = object()
        self.published = []          # [(values, retain, qos)]

    def mqtt_publish(self, values, retain=True, qos=0):
        self.published.append((dict(values), retain, qos))

    def batches(self):
        return [v for v, _r, _q in self.published]

    def latest(self, topic):
        """The most recently published value for `topic` (None if never sent)."""
        for values, _r, _q in reversed(self.published):
            if topic in values:
                return values[topic]
        return None

    def flags_for(self, topic):
        """(retain, qos) of the batch that carried `topic`, or None."""
        for values, retain, qos in reversed(self.published):
            if topic in values:
                return retain, qos
        return None


class FakeVideoTab(MqttTaskState):
    """Carries the parser + MQTT state only; reuses the REAL VideoTab methods so
    emitter and parser can't drift apart. No tkinter window is created."""

    _filter_markers = VideoTab._filter_markers
    _on_marker      = VideoTab._on_marker
    _handle_event   = VideoTab._handle_event

    def __init__(self):
        self._hold = ""
        self._marker_buf = None
        self.active_pod_id = None
        self._remote_rate = None
        self._last_done = None
        self.mqtt_task_name = "video upscaling"
        self.app = _FakeApp()

    def feed(self, text):
        return self._filter_markers(text)


def _emit(monkeypatch, *events):
    """The exact wire bytes the runner writes, via the real bv.gui_event."""
    monkeypatch.setattr(runner_common, "GUI_MODE", True)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        for kind, payload in events:
            bv.gui_event(kind, payload)
    return buf.getvalue()


def _summary(done=3, failed=0, stopped=None, files=None):
    s = bv._final_summary(done, failed, stopped, files)
    s["attempted"] = {("a.mp4", "4K", 0)}      # a set: NOT JSON-serialisable
    return s


# ── _done_payload (pure) ─────────────────────────────────────────────────────

def test_done_payload_reports_the_counts_and_shared_key_names():
    p = bv._done_payload(_summary(done=3, failed=1), r"X:\Poze", 125.44)
    assert p["tool"] == "video"
    # These four names are shared with batch_upscale / tag_and_rename on purpose,
    # so ONE Home Assistant automation covers every tool.
    assert p["processed"] == 3
    assert p["failed"] == 1
    assert p["elapsed_seconds"] == 125.4
    assert p["total"] == 4
    assert p["source"] == r"X:\Poze"
    assert p["stop_reason"] == "completed"
    assert p["stopped_by_user"] is False


def test_done_payload_is_json_serialisable():
    """The summary carries a set ('attempted') and a per-file detail list; neither
    may reach the payload: json.dumps would raise and the GUI would publish
    nothing at all."""
    files = [{"name": "a.mp4", "wall": 61.0}, {"name": "b.mp4", "wall": 12.0}]
    p = bv._done_payload(_summary(files=files), "/src", 73.0)
    assert "attempted" not in p
    assert p["files"] == 2                       # a COUNT, not the records
    json.dumps(p)                                # must not raise


def test_done_payload_marks_a_user_stop():
    p = bv._done_payload(_summary(done=1, stopped="stopped by user"), "/src", 5.0)
    assert p["stopped_by_user"] is True
    assert p["stop_reason"] == "stopped by user"


def test_done_payload_carries_any_other_stop_reason_verbatim():
    p = bv._done_payload(_summary(stopped="per-run cost cap of $5.00 reached"),
                         "/src", 5.0)
    assert p["stopped_by_user"] is False
    assert p["stop_reason"].startswith("per-run cost cap")


def test_done_payload_sums_the_billed_cost():
    files = [{"name": "a.mp4", "cost": 1.25}, {"name": "b.mp4", "cost": 0.5}]
    assert bv._done_payload(_summary(files=files), "/src", 1.0)["cost"] == 1.75


def test_done_payload_cost_is_none_on_a_local_run():
    """A local run bills nothing, so its files carry no cost: the payload must say
    'unknown' (null), not a misleading $0.00."""
    files = [{"name": "a.mp4", "wall": 10.0}]
    assert bv._done_payload(_summary(files=files), "/src", 1.0)["cost"] is None


def test_done_payload_survives_an_empty_summary():
    p = bv._done_payload({}, "/src", 0.0)
    assert p["processed"] == 0 and p["failed"] == 0 and p["files"] == 0


# ── _run_finished (the single end-of-run seam) ───────────────────────────────

def test_run_finished_emits_done_and_notifies(monkeypatch):
    monkeypatch.setattr(runner_common, "GUI_MODE", True)
    seen = []
    monkeypatch.setattr(bv, "_notify_summary",
                        lambda *a, **k: seen.append(a))
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        bv._run_finished({"discord_webhook_url": "x"}, _summary(), "/src")
    assert f"{bv.GUI_MARKER}DONE|" in buf.getvalue()
    assert len(seen) == 1                        # the notification still goes out


def test_run_finished_still_emits_done_without_any_notification_backend(monkeypatch):
    """_notify_summary early-returns when nothing is configured. The DONE event
    must NOT ride on that: MQTT is a separate consumer, and a user with only Home
    Assistant configured has no notification backend at all."""
    monkeypatch.setattr(runner_common, "GUI_MODE", True)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        bv._run_finished(None, _summary(), "/src")
    assert f"{bv.GUI_MARKER}DONE|" in buf.getvalue()


def test_run_queue_sends_exactly_one_done_per_run():
    """Structural: every _notify_summary call site inside the runner's run paths
    must go through _run_finished, or a grouped/auto-resume run publishes a
    per-pod last_run (or none at all). The only bare caller left is
    _run_finished itself."""
    import inspect
    for fn in (bv.run_queue, bv._run_supervised, bv.main):
        src = inspect.getsource(fn)
        assert "_notify_summary(" not in src, \
            f"{fn.__name__} must call _run_finished, not _notify_summary directly"


# ── wire round-trip: runner -> tab -> MQTT last_run ──────────────────────────

def test_done_event_reaches_the_last_run_topic(monkeypatch):
    payload = bv._done_payload(_summary(done=2, failed=1), r"X:\Poze", 90.0)
    wire = _emit(monkeypatch, ("DONE", payload))
    tab = FakeVideoTab()
    human = tab.feed(wire)
    assert human == ""                           # the whole line was the event
    assert tab._last_done                         # parked for the end of the run
    tab.mqtt_task_idle()
    assert tab.app.latest(mp.TASK_NAME_TOPIC) == "idle"
    got = json.loads(tab.app.latest(mp.LAST_RUN_TOPIC))
    assert got["tool"] == "video"                 # the runner's own label wins
    assert got["processed"] == 2 and got["failed"] == 1
    assert got["finished_at"]                     # the freshness guard HA needs


def test_idle_without_a_done_event_publishes_no_last_run():
    """A run that died before finishing must not re-announce a stale summary as
    if it were this run's result."""
    tab = FakeVideoTab()
    tab.mqtt_task_idle()
    assert tab.app.latest(mp.TASK_NAME_TOPIC) == "idle"
    assert tab.app.latest(mp.LAST_RUN_TOPIC) is None
    assert tab.app.latest(mp.EVENT_RUN_FINISHED_TOPIC) is None


def test_a_new_run_clears_the_previous_summary():
    tab = FakeVideoTab()
    tab._last_done = '{"processed": 9}'
    tab.mqtt_task_started()
    assert tab._last_done is None
    assert tab.app.latest(mp.TASK_NAME_TOPIC) == "video upscaling"
    assert tab.app.latest(mp.TASK_RUNTIME_TOPIC) == "0"


# ── events vs retained state (finding F2) ────────────────────────────────────
#
# Every state topic is RETAINED, so a subscriber is re-sent it on subscribe: an
# automation triggering on `last_run` fires again on every HA restart and every
# broker reconnect, re-announcing a run that finished days ago. The event topics
# are the fix, and their whole value is the retain=False flag, hence these.

def test_run_finished_event_is_not_retained():
    tab = FakeVideoTab()
    tab._last_done = '{"processed": 4, "failed": 0}'
    tab.mqtt_task_idle()
    retain, qos = tab.app.flags_for(mp.EVENT_RUN_FINISHED_TOPIC)
    assert retain is False        # or HA re-fires the automation on every restart
    assert qos == 1               # no retained copy to fall back on


def test_run_started_event_is_not_retained():
    tab = FakeVideoTab()
    tab.mqtt_task_started()
    retain, qos = tab.app.flags_for(mp.EVENT_RUN_STARTED_TOPIC)
    assert (retain, qos) == (False, 1)
    started = json.loads(tab.app.latest(mp.EVENT_RUN_STARTED_TOPIC))
    assert started["tool"] == "video upscaling" and started["started_at"]


def test_state_topics_stay_retained():
    """The events must not have made the STATE topics one-shot: a dashboard has to
    survive a Home Assistant restart, which is what retained is for."""
    tab = FakeVideoTab()
    tab.mqtt_task_started()                 # this CLEARS _last_done, so set it after
    tab.mqtt_task_update(details="working")
    tab._last_done = '{"processed": 1}'
    tab.mqtt_task_idle()
    for topic in (mp.TASK_NAME_TOPIC, mp.TASK_DETAILS_TOPIC, mp.LAST_RUN_TOPIC):
        assert tab.app.flags_for(topic)[0] is True, topic


def test_the_event_and_last_run_carry_the_same_object():
    """One payload, two deliveries: whatever a user templates off the retained
    topic must work verbatim on the event, or the docs need two examples of
    everything."""
    tab = FakeVideoTab()
    tab._last_done = '{"processed": 7, "failed": 2, "tool": "video"}'
    tab.mqtt_task_idle()
    assert (json.loads(tab.app.latest(mp.EVENT_RUN_FINISHED_TOPIC))
            == json.loads(tab.app.latest(mp.LAST_RUN_TOPIC)))


def test_timestamps_carry_a_utc_offset():
    """A naive timestamp is read in whatever timezone the Home Assistant process
    runs in (commonly UTC in a container), which silently breaks the freshness
    condition the retained route depends on, by exactly the offset between the
    two machines. See gui.common.now_stamp."""
    import datetime
    from gui.common import now_stamp
    tab = FakeVideoTab()
    tab.mqtt_task_started()
    tab._last_done = "{}"                   # as a runner's DONE would arrive mid-run
    tab.mqtt_task_idle()
    stamps = [json.loads(tab.app.latest(mp.EVENT_RUN_STARTED_TOPIC))["started_at"],
              json.loads(tab.app.latest(mp.LAST_RUN_TOPIC))["finished_at"],
              now_stamp()]
    for s in stamps:
        assert datetime.datetime.fromisoformat(s).tzinfo is not None, s


# ── live task state ──────────────────────────────────────────────────────────

def test_task_update_publishes_only_what_it_knows():
    tab = FakeVideoTab()
    tab.mqtt_task_update(details="Upscaling a.mp4", progress="120/4000")
    assert tab.app.batches() == [{mp.TASK_DETAILS_TOPIC: "Upscaling a.mp4",
                                 mp.TASK_PROGRESS_TOPIC: "120/4000"}]


def test_task_update_never_blanks_a_value_it_was_not_given():
    """A None means 'no reading this tick', not 'clear it'; otherwise a run with
    no benchmark would wipe the ETA another tick published."""
    tab = FakeVideoTab()
    tab.mqtt_task_update(eta="1h 04m")
    tab.mqtt_task_update(progress="9/10")
    assert mp.TASK_ETA_TOPIC not in tab.app.batches()[1]
    assert tab.app.latest(mp.TASK_ETA_TOPIC) == "1h 04m"


def test_task_update_with_nothing_to_say_publishes_nothing():
    tab = FakeVideoTab()
    tab.mqtt_task_update()
    assert tab.app.published == []          # nothing at all, not an empty batch


def test_video_event_publishes_the_new_file_immediately(monkeypatch):
    """The per-file transition is the interesting one, so it bypasses the tick
    throttle: a viewer should not wait 10 s to see which file is running."""
    wire = _emit(monkeypatch, ("VIDEO", {"rel": "holiday.mp4", "target": "4K",
                                         "index": 2, "total": 5}))
    tab = FakeVideoTab()
    tab._cur_seg_frames = tab._cur_seg_done = 0
    tab.status_var = _StatusVar()
    tab.feed(wire)
    details = tab.app.latest(mp.TASK_DETAILS_TOPIC)
    assert "holiday.mp4" in details and "[2/5]" in details


class _StatusVar:
    def __init__(self):
        self.value = ""

    def set(self, v):
        self.value = v

    def get(self):
        return self.value


# ── the tab is wired to the mixin ────────────────────────────────────────────

def test_video_tab_mixes_in_the_publisher():
    assert issubclass(VideoTab, MqttTaskState)


def test_every_run_mode_keeps_the_local_telemetry_alive():
    """The app's idle sampler stands down while any task runs, so if the Video tab
    doesn't sample for itself the Local Unit row and the MQTT system/* topics freeze
    for the whole run. A remote run must still sample (slowly), and must NOT open a
    local usage graph: the pod's own history covers its GPU work."""
    import inspect
    src = inspect.getsource(VideoTab._launch)
    assert "_start_local_telemetry()" in src                     # local run: dense
    assert "history=False" in src                                # remote run: no graph
    assert "REMOTE_TELEM_MS" in src                              # remote run: slow
    assert VideoTab.REMOTE_TELEM_MS > VideoTab.LOCAL_TELEM_MS


def test_video_tab_lifecycle_is_wired():
    """Structural guard: the run's begin/end hooks must announce and clear the
    task, or the tab goes back to publishing nothing (the F1 regression)."""
    import inspect
    assert "mqtt_task_started" in inspect.getsource(VideoTab._begin_run)
    assert "mqtt_task_idle" in inspect.getsource(VideoTab._end_run)
    assert "_publish_task_state" in inspect.getsource(VideoTab._run_tick)
