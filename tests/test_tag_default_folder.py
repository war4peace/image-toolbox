"""
Tag & Rename's "Photo folder" pre-fill chain (0.5.9).

Roadmap #16 stopped Tag & Rename descending into `__upscaled__`, which used to make
pointing it at a source root work by accident (the low-res originals fell below the
3840/2160 threshold, the 4K upscales inside `__upscaled__` passed it). The supported
route is now to point it AT the upscaled folder, so the tab offers that folder
instead of making the user retype what the Batch Upscaler tab already knows.

First usable rule wins, then stop:
  1. a value already in the field
  2. Settings -> Default folders -> Tag & Rename Photo folder  (must EXIST)
  3. the Batch Upscaler tab's live "Save upscaled to"
  4. Settings -> Default folders -> Batch Upscaler Output folder
  5. otherwise empty

These bind the method to a stub, so no tkinter window is needed.
"""
import types

import pytest

pytest.importorskip("tkinter")

import gui.tab_tag as tt                                    # noqa: E402
from gui.tab_tag import TagTab                              # noqa: E402


class _Var:
    """The two StringVar methods restore_defaults_if_empty uses."""

    def __init__(self, value=""):
        self._v = value

    def get(self):
        return self._v

    def set(self, v):
        self._v = v


def _tab(field="", upscale_out=None):
    """A TagTab stand-in. upscale_out=None means "no Batch Upscaler tab at all"."""
    app = types.SimpleNamespace()
    if upscale_out is not None:
        app.upscale_tab = types.SimpleNamespace(out_var=_Var(upscale_out))
    return types.SimpleNamespace(dir_var=_Var(field), app=app)


@pytest.fixture
def defaults(monkeypatch):
    """Control both pinned defaults, and make every path look like a real folder."""
    store = {}
    monkeypatch.setattr(tt, "get_default_folder", lambda k: store.get(k, ""))
    monkeypatch.setattr(tt.os.path, "isdir", lambda p: bool(p) and p not in _MISSING)
    return store


_MISSING = set()


@pytest.fixture(autouse=True)
def _clear_missing():
    _MISSING.clear()
    yield
    _MISSING.clear()


# ── rule order ───────────────────────────────────────────────────────────────

def test_rule1_an_existing_value_is_never_overwritten(defaults):
    defaults["tag_folder"] = r"D:\Pinned"
    tab = _tab(field=r"D:\What The User Typed", upscale_out=r"D:\Pics\__upscaled__")
    TagTab.restore_defaults_if_empty(tab)
    assert tab.dir_var.get() == r"D:\What The User Typed"


def test_rule2_the_tools_own_default_wins_over_the_upscaler(defaults):
    defaults["tag_folder"] = r"D:\Pinned"
    defaults["upscale_output"] = r"D:\Pics\__upscaled__"
    tab = _tab(upscale_out=r"D:\Live\__upscaled__")
    TagTab.restore_defaults_if_empty(tab)
    assert tab.dir_var.get() == r"D:\Pinned"


def test_rule3_falls_back_to_the_upscaler_tabs_live_output(defaults):
    defaults["upscale_output"] = r"D:\Pinned Output"
    tab = _tab(upscale_out=r"D:\Live\__upscaled__")
    TagTab.restore_defaults_if_empty(tab)
    assert tab.dir_var.get() == r"D:\Live\__upscaled__"


def test_rule4_falls_back_to_the_upscalers_pinned_output(defaults):
    defaults["upscale_output"] = r"D:\Pinned Output"
    tab = _tab(upscale_out="")                    # tab present but field empty
    TagTab.restore_defaults_if_empty(tab)
    assert tab.dir_var.get() == r"D:\Pinned Output"


def test_rule5_nothing_configured_leaves_it_empty(defaults):
    tab = _tab(upscale_out="")
    TagTab.restore_defaults_if_empty(tab)
    assert tab.dir_var.get() == ""


# ── the existence asymmetry (deliberate, see the method's docstring) ─────────

def test_a_missing_tag_default_falls_through_instead_of_dead_ending(defaults):
    """Rule 2 requires the folder to exist: a photo SOURCE that is gone is
    meaningless, so the chain continues rather than pre-filling a dead path."""
    defaults["tag_folder"] = r"Z:\Unplugged Drive"
    _MISSING.add(r"Z:\Unplugged Drive")
    tab = _tab(upscale_out=r"D:\Live\__upscaled__")
    TagTab.restore_defaults_if_empty(tab)
    assert tab.dir_var.get() == r"D:\Live\__upscaled__"


def test_an_upscale_output_that_does_not_exist_yet_is_still_offered(defaults):
    """Rules 3/4 take the value as-is: the upscaler CREATES its output folder on the
    first run, so the useful pre-fill is normally a folder that isn't there yet."""
    _MISSING.add(r"D:\Pics\__upscaled__")
    tab = _tab(upscale_out=r"D:\Pics\__upscaled__")
    TagTab.restore_defaults_if_empty(tab)
    assert tab.dir_var.get() == r"D:\Pics\__upscaled__"


# ── robustness: the Tag tab must never break over a neighbouring tab ─────────

def test_a_missing_upscale_tab_degrades_to_the_next_rule(defaults):
    defaults["upscale_output"] = r"D:\Pinned Output"
    tab = _tab(upscale_out=None)                  # app has no .upscale_tab
    TagTab.restore_defaults_if_empty(tab)
    assert tab.dir_var.get() == r"D:\Pinned Output"


def test_an_upscale_tab_that_raises_degrades_to_the_next_rule(defaults):
    defaults["upscale_output"] = r"D:\Pinned Output"

    class _Boom:
        def get(self):
            raise RuntimeError("tab still building")

    app = types.SimpleNamespace(upscale_tab=types.SimpleNamespace(out_var=_Boom()))
    tab = types.SimpleNamespace(dir_var=_Var(""), app=app)
    TagTab.restore_defaults_if_empty(tab)
    assert tab.dir_var.get() == r"D:\Pinned Output"


def test_whitespace_only_values_do_not_count_as_a_value(defaults):
    defaults["upscale_output"] = "   "
    tab = _tab(field="   ", upscale_out="  ")
    TagTab.restore_defaults_if_empty(tab)
    assert tab.dir_var.get().strip() == ""
