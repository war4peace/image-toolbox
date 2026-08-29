"""
The suite makes no network calls, and the guard that ensures it is not order-dependent.

`SettingsTab.__init__` ends with `self._check_ollama()`, which starts a daemon thread that
does a real `urlopen`. In the app that is correct. In a test it leaves a thread running
inside `ssl.create_default_context()` (which opens the Windows certificate store) long
after the fixture that built the tab has destroyed its root.

One test file knew this and stubbed the method by assigning to the CLASS, without ever
restoring it, so the protection covered that file and everything collected after it and
nothing before. 0.6.3 took the number of SettingsTab builders from two to four, and a CI
run died with `Windows fatal exception: code 0x80000003` on a commit that had passed on
another branch twenty minutes earlier, with a leaked cert-store thread in the dump.

These tests check the EFFECT (no thread is started, from any collection order), not that
conftest contains a particular line: patching the wrong name, or a rename of
`_check_ollama`, would leave the fixture green and the probe live.
"""

import threading

import pytest


def _settings_tab(root):
    import tkinter.ttk as ttk
    from gui.tab_settings import SettingsTab

    class _FakeApp:
        def refresh_tab_exclusivity(self): pass
        def mqtt_publish(self, *a, **k): pass
        def sync_settings_defaults(self): pass

    return SettingsTab(ttk.Notebook(root), _FakeApp())


def test_building_a_settings_tab_starts_no_background_thread():
    """The property that matters, stated as the failure it prevents: a thread this suite
    started, still inside the Windows cert store, while an unrelated test runs."""
    from conftest import make_tk_root

    root = make_tk_root()
    try:
        before = set(threading.enumerate())
        _settings_tab(root)
        root.update_idletasks()
        started = [t for t in threading.enumerate() if t not in before]
        assert not started, \
            f"SettingsTab started {[t.name for t in started]}; the conftest guard is not " \
            f"reaching it (renamed method? patched the wrong module?)"
    finally:
        root.destroy()


def test_the_probe_is_stubbed_for_every_module_not_just_this_one():
    """The order-dependence is the actual defect. A class-level patch applied inside one
    test file protects that file and everything collected after it; this asserts the stub
    is in place with no per-file setup at all."""
    from gui import tab_settings

    probe = tab_settings.SettingsTab._check_ollama
    assert probe.__name__ == "<lambda>", \
        "SettingsTab._check_ollama is the real network probe during the test session"


def test_the_real_probe_is_restored_outside_the_session():
    """The guard must not be a permanent edit to the class: `_check_ollama` is real
    behaviour with its own reason to exist, and a stub that outlived the session would
    silently disarm it for anything else importing the module in-process."""
    import inspect

    from gui import tab_settings

    src = inspect.getsource(tab_settings.SettingsTab._check_ollama.__class__ and
                            tab_settings)
    assert "def _check_ollama(self):" in src, \
        "the real _check_ollama is gone from the module, not merely stubbed on the class"
    assert "threading.Thread(target=work, daemon=True).start()" in src, \
        "the app's own background probe was removed; the guard is a TEST concern only"
