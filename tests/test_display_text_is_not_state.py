"""
Logic must not read a widget's displayed text (0.5.8).

Two places derived a decision from what a widget was *showing*, which works only
while the UI is monolingual and nobody renames a label:

  * `tab_upscale` looked its pause-button tooltip up by the button's own text. An
    unknown label returned None and the button silently kept the previous phase's
    hint. Cosmetic, but it also meant every relabel had to remember a matching
    _sync call.
  * `tooltab` derived the run mode from the "Run on" combobox's display value
    (`run_on_var.get() == RUN_ON_REMOTE`). That one is a money bug in waiting: an
    unrecognised label reads as "not remote", so a run the user sent to a rented
    pod would quietly execute on the local GPU instead (or, on a Remote-only
    install, not at all). It was safe only because both sides happened to be the
    same literal.

The fix in both cases is the pattern `tab_video` already used: a stable internal
key, with the display string as a pure output. These tests pin that, including the
behaviour when a label is not recognised, which is the case no manual click-through
would ever produce.

Builds the real tabs in a hidden root, and skips where tkinter is unavailable.
"""

import pytest

pytest.importorskip("tkinter")

import tkinter as tk                      # noqa: E402
from tkinter import ttk                   # noqa: E402

from gui.common import RUN_ON_LOCAL, RUN_ON_REMOTE   # noqa: E402


def _noop(*a, **k):
    return None


@pytest.fixture(scope="module")
def upscale_tab():
    """One hidden root and one real tab for the module. The App methods the tab
    calls back into are stubbed; none of them are what is under test."""
    from gui.tab_upscale import UpscaleTab

    try:
        root = tk.Tk()
    except tk.TclError:                    # no display
        pytest.skip("no Tk display")
    root.withdraw()
    app = type("App", (), {
        "root": root, "settings": {}, "mqtt": None, "mqtt_publish": _noop,
        "refresh_benchmark_lock": _noop, "refresh_tab_exclusivity": _noop,
        "refresh_funds_readout": _noop,
    })()
    tab = UpscaleTab(ttk.Notebook(root), app)
    tab._refresh_gpus = _noop              # never call RunPod from a test
    tab._populate_gpus = _noop
    yield tab
    root.destroy()


# ── the pause button: one table, keyed by phase ──────────────────────────────

@pytest.mark.parametrize("phase,label", [
    ("cancel", "Cancel"), ("pause", "Pause"), ("resume", "Resume"),
])
def test_the_pause_phase_sets_both_the_label_and_the_hint(upscale_tab, phase, label):
    upscale_tab._set_pause_phase(phase)
    assert upscale_tab.pause_btn.cget("text") == label
    assert upscale_tab.pause_tip.text == upscale_tab.PAUSE_PHASES[phase][1]


def test_every_phase_has_a_distinct_label_and_hint(upscale_tab):
    labels = [l for l, _ in upscale_tab.PAUSE_PHASES.values()]
    hints = [h for _, h in upscale_tab.PAUSE_PHASES.values()]
    assert len(set(labels)) == len(labels)
    assert len(set(hints)) == len(hints)
    assert all(labels) and all(hints)


def test_the_hint_follows_the_phase_not_the_label(upscale_tab):
    """The regression this replaces: relabel the button behind the helper's back
    and the phase, not the stale text, still decides the hint."""
    upscale_tab._set_pause_phase("resume")
    upscale_tab.pause_btn.configure(text="Something else")
    upscale_tab._set_pause_phase("cancel")
    assert upscale_tab.pause_tip.text == upscale_tab.PAUSE_PHASES["cancel"][1]


def test_nothing_looks_the_hint_up_by_the_buttons_text():
    """Structural: the old shape was `PAUSE_TIPS.get(self.pause_btn.cget("text"))`.
    Reading any widget's text to decide something is the pattern being removed."""
    import inspect

    from gui import tab_upscale

    src = inspect.getsource(tab_upscale)
    assert 'cget("text")' not in src


# ── the "Run on" selector: the label is a display, not the state ─────────────

@pytest.mark.parametrize("label,expected_remote", [
    (RUN_ON_LOCAL, False), (RUN_ON_REMOTE, True),
])
def test_the_run_on_label_maps_to_the_mode_it_means(upscale_tab, label, expected_remote):
    upscale_tab.run_on_var.set(label)
    upscale_tab._on_run_on_change()
    assert bool(upscale_tab.remote_var.get()) is expected_remote


def test_an_unrecognised_label_leaves_the_run_where_it_was(upscale_tab):
    """The money case. If a label ever stops matching (renamed, translated, or set
    from somewhere else), the fallback must be "keep the current mode", never the
    implicit False that a `==` comparison produces: that would send a run the user
    aimed at a rented pod to the local GPU instead, silently."""
    upscale_tab.run_on_var.set(RUN_ON_REMOTE)
    upscale_tab._on_run_on_change()
    assert bool(upscale_tab.remote_var.get()) is True

    upscale_tab.run_on_var.set("Ruleaza pe: RunPod")     # renamed / translated
    upscale_tab._on_run_on_change()
    assert bool(upscale_tab.remote_var.get()) is True, "an unknown label moved the run"


def test_the_mode_table_covers_every_offered_label(upscale_tab):
    """Whatever the combobox can show must be in the table, or picking it would hit
    the fallback and appear to do nothing."""
    offered = set(upscale_tab.run_on_combo.cget("values"))
    assert offered
    assert offered <= set(upscale_tab._run_on_modes)


def test_the_video_tab_already_separates_token_from_label():
    """VideoTab is where this pattern came from: `mode_var` holds the
    "local"/"remote" token every method reads, `mode_pick_var` holds the label, and
    `_mode_tokens` maps one to the other. Pinned so a future edit does not
    "simplify" it back into comparing display strings."""
    import inspect

    from gui import tab_video

    src = inspect.getsource(tab_video)
    assert "_mode_tokens" in src
    assert "self.mode_var.get() == RUN_ON_REMOTE" not in src
