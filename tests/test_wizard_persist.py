"""
Regression for the "wizard pops up every launch" bug (0.4.6). The whole app funnels
gui_settings.json writes through ONE shared dict (App.settings): every window's
geometry save rewrites it. The wizard originally persisted wizard_done via a
separate disk copy (mark_wizard_completed), so App.settings never learned the flag
and the next geometry save clobbered it off disk, and the wizard returned forever.

_mark_done() must write through that same shared App.settings dict. Pinned here by
binding it to a stub self (no tkinter window), with SETTINGS_PATH redirected to a
tmp file so the real gui_settings.json is untouched.
"""
import types

import pytest

pytest.importorskip("tkinter")   # gui.wizard imports tkinter at module load

import gui.common as common      # noqa: E402
import gui.wizard as W           # noqa: E402


def test_mark_done_writes_through_shared_settings(monkeypatch, tmp_path):
    monkeypatch.setattr(common, "SETTINGS_PATH", str(tmp_path / "gui_settings.json"))
    shared = {"main_geometry": "1000x700+0+0"}          # App.settings, no flag yet
    stub = types.SimpleNamespace(app=types.SimpleNamespace(settings=shared))

    W.FirstStartWizard._mark_done(stub)

    assert shared["wizard_done"] is True                # the shared dict itself
    assert common.load_settings().get("wizard_done") is True   # persisted to disk

    # THE bug: a later geometry save rewrites the shared dict; the flag must remain.
    shared["main_geometry"] = "1200x800+20+20"
    common.save_settings(shared)
    assert common.load_settings().get("wizard_done") is True
    assert common.wizard_completed() is True


def test_mark_done_falls_back_without_app_settings(monkeypatch, tmp_path):
    # A parent that is not a full App (bare Tk root in tests): still persist via the
    # disk helper rather than silently losing the flag.
    monkeypatch.setattr(common, "SETTINGS_PATH", str(tmp_path / "gui_settings.json"))
    stub = types.SimpleNamespace(app=object())          # no .settings attribute

    W.FirstStartWizard._mark_done(stub)

    assert common.load_settings().get("wizard_done") is True
