"""
Run exclusivity (App.refresh_tab_exclusivity): while ANY tool run is active, every OTHER
notebook tab is disabled so a second run can't start (two runs would fight over the local
GPU and the shared SQLite cache). The running tab, and Settings/RunPod, follow the same
rule. The method only touches ttk state, so it is exercised here against a fake notebook.
"""

import types

from gui.app import App


def _make(running_tab=None):
    """Build a fake `self` for App.refresh_tab_exclusivity: five tool tabs (one optionally
    running), plus Settings/RunPod widgets, wired through a recording fake notebook."""
    tool = {name: types.SimpleNamespace(running=False)
            for name in ("upscale", "tag", "conciliate", "video", "stabilize")}
    if running_tab is not None:
        tool[running_tab].running = True
    settings = object()
    runpod = object()

    class _FakeNb:
        def __init__(self, mapping):
            self._map = mapping
            self.states = {}

        def tabs(self):
            return tuple(self._map)

        def nametowidget(self, tab_id):
            return self._map[tab_id]

        def tab(self, tab_id, state=None, **_kw):
            if state is not None:
                self.states[tab_id] = state

    mapping = {"u": tool["upscale"], "t": tool["tag"], "c": tool["conciliate"],
               "v": tool["video"], "b": tool["stabilize"], "s": settings, "r": runpod}
    app = types.SimpleNamespace(
        upscale_tab=tool["upscale"], tag_tab=tool["tag"],
        conciliate_tab=tool["conciliate"], video_tab=tool["video"],
        stabilize_tab=tool["stabilize"],
        nb=_FakeNb(mapping))
    # The real App resolves "is anything running" through ONE helper, which the
    # hand-off to Video Stabilization (#23 item 1) also consults - a hand-off must
    # not target a tab exclusivity has just made unreachable.
    app.active_tool_tab = lambda: App.active_tool_tab(app)
    return app


def test_idle_enables_every_tab():
    app = _make(running_tab=None)
    App.refresh_tab_exclusivity(app)
    assert set(app.nb.states.values()) == {"normal"}


def test_a_local_video_run_disables_every_other_tab():
    # The reported case: a local Video Upscale must lock the Batch Upscaler (and the rest).
    app = _make(running_tab="video")
    App.refresh_tab_exclusivity(app)
    assert app.nb.states["v"] == "normal"                 # the running tab stays reachable
    assert all(app.nb.states[k] == "disabled"
               for k in ("u", "t", "c", "b", "s", "r"))   # incl. Settings + RunPod


def test_a_batch_upscale_run_disables_every_other_tab():
    app = _make(running_tab="upscale")
    App.refresh_tab_exclusivity(app)
    assert app.nb.states["u"] == "normal"
    assert all(app.nb.states[k] == "disabled"
               for k in ("t", "c", "v", "b", "s", "r"))


def test_a_stabilization_run_disables_every_other_tab():
    """#20 requires the same exclusivity as every other tool, even though this one
    uses no GPU: it is still a long local job over the user's files."""
    app = _make(running_tab="stabilize")
    App.refresh_tab_exclusivity(app)
    assert app.nb.states["b"] == "normal"
    assert all(app.nb.states[k] == "disabled"
               for k in ("u", "t", "c", "v", "s", "r"))


# ─────────────────────────────────────────────
#  The hand-off obeys the same lock  (#23 item 1)
# ─────────────────────────────────────────────


def test_active_tool_tab_names_the_running_tab():
    assert App.active_tool_tab(_make(running_tab=None)) is None
    app = _make(running_tab="conciliate")
    assert App.active_tool_tab(app) is app.conciliate_tab


def test_a_handoff_is_refused_while_anything_is_running(monkeypatch):
    """Exclusivity has already greyed the Stabilization tab out, so a hand-off would
    land on a tab the user cannot reach. It must say so, not queue the intent."""
    import gui.app as ga

    said = []
    monkeypatch.setattr(ga.messagebox, "showinfo", lambda *a, **k: said.append(a))
    app = _make(running_tab="video")
    app.stabilize_tab.add_sources = lambda *a, **k: 1 / 0     # must never be called
    assert App.send_to_stabilize(app, [r"C:\clips\shaky.mp4"]) is False
    assert said


def test_a_handoff_reaches_the_tab_when_nothing_is_running():
    app = _make(running_tab=None)
    handed = []
    app.stabilize_tab.add_sources = lambda paths, select_tab=False: (
        handed.append((paths, select_tab)) or len(paths))
    assert App.send_to_stabilize(app, [r"C:\clips\shaky.mp4"]) is True
    assert handed == [([r"C:\clips\shaky.mp4"], True)]
