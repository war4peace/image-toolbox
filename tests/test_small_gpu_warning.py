"""
Defect D4: a local GPU below the documented 8 GB minimum was offered with nothing said
about it, while the REMOTE picker had always refused cards under a task's floor. So the
app was careful about a card the user rents by the hour and silent about the one they
own.

The decision was **warn, never forbid**, and these tests pin that as much as the warning
itself. SeedVR2 offloads, so a small card is slower rather than incapable; the wizard
already treats its VRAM tiers as suggestions with every option selectable; and some
combinations genuinely work down there (Tag & Rename with the smallest vision model).
Greying out the only GPU somebody owns would deny them a feature that would have run.
"""

import gui.common as common


# ── the note itself (pure) ──────────────────────────────────────────────────


def test_a_card_under_the_minimum_is_noted():
    assert common.small_gpu_note(6) == "below the 8 GB minimum"


def test_a_card_at_or_above_the_minimum_is_not():
    assert common.small_gpu_note(8) is None
    assert common.small_gpu_note(24) is None


def test_an_unknown_size_is_not_noted():
    """nvidia-smi can answer [N/A] per field, and a card we know nothing about must not
    be labelled as if we had measured it."""
    for value in (None, "", "n/a", 0, -1):
        assert common.small_gpu_note(value) is None


def test_the_note_names_the_threshold_it_uses():
    """The number in the message and the constant cannot drift apart."""
    assert str(common.LOCAL_VRAM_MIN_GB) in common.small_gpu_note(2)


# ── how it reaches the user ────────────────────────────────────────────────


def _tab(monkeypatch, memory_gb, warned=False):
    from gui.tooltab import ToolTab
    ToolTab._small_gpu_warned = warned
    tab = ToolTab.__new__(ToolTab)          # no widgets: only the decision is tested
    tab._gpu_choices = [{"id": "GeForce RTX 2060", "name": "GeForce RTX 2060",
                         "memory_gb": memory_gb, "price": None, "stock": "local"}]
    monkeypatch.setattr(ToolTab, "_selected_gpu", lambda self: self._gpu_choices[0])
    return ToolTab, tab


def test_a_small_card_is_warned_about_but_still_allowed(monkeypatch):
    from gui import tooltab
    asked = []
    monkeypatch.setattr(tooltab.messagebox, "askyesno",
                        lambda *a, **k: asked.append(a) or True)
    _cls, tab = _tab(monkeypatch, 6)
    assert tab.confirm_small_gpu() is True          # the user may proceed
    assert asked, "no warning was shown"
    body = asked[0][1]
    assert "6 GB" in body and "below the 8 GB minimum" in body
    # It must say what to expect and what to do, not just that the card is small.
    assert "slow" in body.lower()
    assert "Remote: RunPod" in body


def test_saying_no_stops_the_run(monkeypatch):
    from gui import tooltab
    monkeypatch.setattr(tooltab.messagebox, "askyesno", lambda *a, **k: False)
    _cls, tab = _tab(monkeypatch, 6)
    assert tab.confirm_small_gpu() is False


def test_a_big_enough_card_is_never_interrupted(monkeypatch):
    from gui import tooltab
    monkeypatch.setattr(tooltab.messagebox, "askyesno",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("asked")))
    _cls, tab = _tab(monkeypatch, 24)
    assert tab.confirm_small_gpu() is True


def test_the_warning_is_shown_once_per_session(monkeypatch):
    """The card does not change between runs. A dialog on every Start would train the
    user to click through it, which is how a warning stops being one."""
    from gui import tooltab
    n = []
    monkeypatch.setattr(tooltab.messagebox, "askyesno", lambda *a, **k: n.append(1) or True)
    cls, tab = _tab(monkeypatch, 6)
    for _ in range(4):
        assert tab.confirm_small_gpu() is True
    assert len(n) == 1
    cls._small_gpu_warned = False            # leave the class as found


def test_both_image_tabs_ask_before_a_local_run():
    """Wiring, not behaviour: the check is useless if a tab forgets to call it, and
    both tabs' local branch already calls confirm_gpu_overlap right there."""
    import io
    import os
    root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "scripts", "gui")
    for name in ("tab_upscale.py", "tab_tag.py"):
        with io.open(os.path.join(root, name), encoding="utf-8") as fh:
            src = fh.read()
        assert "confirm_small_gpu()" in src, f"{name} never warns about a small card"
