"""
Settings > Notifications: the Home Assistant webhook rows (idea 2, 0.5.8).

The backend itself is covered by test_notifications_ha.py. What is left, and what
nothing else in the suite touches, is the WIRING: a field that is built but never
collected saves nothing, and one that is collected but never reverted breaks the
unsaved-changes guard. Both fail silently in a way only a human clicking Save
would notice, which is exactly the sort of thing to pin.

Also pinned here: the normalisation happens at collect time. A user who pastes the
whole endpoint into one box must end up with the pair stored split, so what Home
Assistant is sent and what the form shows afterwards agree.

Builds the real tab in a hidden root, like the other tkinter tests, and skips
where tkinter is unavailable.
"""

import pytest

pytest.importorskip("tkinter")

import tkinter as tk                  # noqa: E402
from tkinter import ttk               # noqa: E402

import notifications                  # noqa: E402


BASE = "http://homeassistant.local:8123"
WID = "imgtbx_a8f3c1"


@pytest.fixture(scope="module")
def tab():
    """One hidden root and one real tab for the module: building a Tk root per test
    is what makes tkinter suites flaky. The Ollama reachability probe is stubbed
    out because it spawns a background thread that would call back into a
    destroyed widget after the last test."""
    from gui import tab_settings

    try:
        root = tk.Tk()
    except tk.TclError:                       # no display
        pytest.skip("no Tk display")
    root.withdraw()
    tab_settings.SettingsTab._check_ollama = lambda self: None
    widget = tab_settings.SettingsTab(ttk.Notebook(root),
                                      app=type("App", (), {"root": root})())
    yield widget
    root.destroy()


def _notif(tab):
    sections, errors = tab._collect()
    assert not errors, errors
    return sections["notifications"]


def test_the_two_fields_exist_and_are_collected(tab):
    tab.ha_url_var.set(BASE)
    tab.ha_webhook_var.set(WID)
    notif = _notif(tab)
    assert notif["ha_url"] == BASE
    assert notif["ha_webhook_id"] == WID


def test_a_pasted_endpoint_is_split_on_save(tab):
    """The user pastes what they copied out of Home Assistant. Storing it whole
    would build .../api/webhook//api/webhook/<id> at send time."""
    tab.ha_url_var.set(f"{BASE}/api/webhook/{WID}")
    tab.ha_webhook_var.set("")
    notif = _notif(tab)
    assert notif["ha_url"] == BASE
    assert notif["ha_webhook_id"] == WID


def test_blank_fields_collect_as_blank_not_missing(tab):
    """Blank must round-trip as "" (the backend's off switch), never as a missing
    key that would read as "unchanged" against a previously configured value."""
    tab.ha_url_var.set("")
    tab.ha_webhook_var.set("")
    notif = _notif(tab)
    assert notif["ha_url"] == "" and notif["ha_webhook_id"] == ""


def test_the_fields_take_part_in_the_unsaved_changes_guard(tab):
    """_collect feeds _snapshot, which is what the "Not saved" indicator and the
    leave-the-tab prompt compare. A field missing from _collect is a silent edit."""
    before = tab._snapshot()
    tab.ha_webhook_var.set("something-new")
    assert tab._snapshot() != before


def test_revert_restores_both_fields(tab):
    tab.ha_url_var.set(BASE)
    tab.ha_webhook_var.set(WID)
    tab.revert()
    saved = notifications.resolve_settings(
        __import__("gui.common", fromlist=["CFG"]).CFG)
    assert tab.ha_url_var.get() == saved.get("ha_url", "")
    assert tab.ha_webhook_var.get() == saved.get("ha_webhook_id", "")


def test_test_button_shows_the_disclaiming_wording(tab, monkeypatch):
    """A success is deliberately NOT green: Home Assistant answers 200 to a webhook
    ID it has never heard of, so the row must not look like a confirmation."""
    monkeypatch.setattr(notifications, "test_ha_webhook",
                        lambda *a, **k: (True, notifications.HA_TEST_OK))
    tab.ha_url_var.set(BASE)
    tab.ha_webhook_var.set(WID)
    tab._test_ha_webhook()
    assert str(tab.ha_status.cget("text")) == notifications.HA_TEST_OK
    assert str(tab.ha_status.cget("foreground")) == "#666"   # muted, not the green


def test_test_button_shows_a_failure_in_red(tab, monkeypatch):
    monkeypatch.setattr(notifications, "test_ha_webhook",
                        lambda *a, **k: (False, "Could not reach Home Assistant: nope"))
    tab._test_ha_webhook()
    assert str(tab.ha_status.cget("foreground")) == "#b3261e"
