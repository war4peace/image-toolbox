"""
Run exclusivity (App.refresh_tab_exclusivity): while ANY tool run is active, every OTHER
notebook tab is disabled so a second run can't start (two runs would fight over the local
GPU and the shared SQLite cache). The running tab, and Settings/RunPod, follow the same
rule. The method only touches ttk state, so it is exercised here against a fake notebook.
"""

import types

from gui.app import App


def _make(running_tab=None):
    """Build a fake `self` for App.refresh_tab_exclusivity: four tool tabs (one optionally
    running), plus Settings/RunPod widgets, wired through a recording fake notebook."""
    tool = {name: types.SimpleNamespace(running=False)
            for name in ("upscale", "tag", "conciliate", "video")}
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
               "v": tool["video"], "s": settings, "r": runpod}
    app = types.SimpleNamespace(
        upscale_tab=tool["upscale"], tag_tab=tool["tag"],
        conciliate_tab=tool["conciliate"], video_tab=tool["video"],
        nb=_FakeNb(mapping))
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
               for k in ("u", "t", "c", "s", "r"))        # incl. Settings + RunPod


def test_a_batch_upscale_run_disables_every_other_tab():
    app = _make(running_tab="upscale")
    App.refresh_tab_exclusivity(app)
    assert app.nb.states["u"] == "normal"
    assert all(app.nb.states[k] == "disabled"
               for k in ("t", "c", "v", "s", "r"))
