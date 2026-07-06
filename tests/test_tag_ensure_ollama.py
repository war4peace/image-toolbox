"""
Tag & Rename local-run safety net (0.4.6): before a LOCAL tag run starts, the tab
checks the configured Ollama vision model is installed and offers to pull it if not
(Ollama does not auto-pull, so a missing model would just fail the run). This backs
up the first-start Wizard's pull, in case it was skipped or failed.

TagTab._ensure_ollama_model() returns True to proceed / False to abort. It is
exercised here by binding the unbound method to a minimal stub self and stubbing
the module-level helpers, so no tkinter window (or Ollama server) is needed.
"""
import types

import pytest

pytest.importorskip("tkinter")   # tab_tag imports tkinter at module load

import gui.tab_tag as T          # noqa: E402


class _FakeMB:
    def __init__(self, yes):
        self.yes = yes
        self.info = 0
        self.error = 0

    def askyesno(self, *a, **k):
        return self.yes

    def showinfo(self, *a, **k):
        self.info += 1

    def showerror(self, *a, **k):
        self.error += 1


def _run(monkeypatch, list_ret, present, yes=None, dlg_ok=None):
    monkeypatch.setitem(T.CFG, "ollama", {"url": "http://x", "model": "gemma3:4b"})
    monkeypatch.setattr(T, "ollama_list_models", lambda url: list_ret)
    monkeypatch.setattr(T, "ollama_model_present", lambda names, model: present)
    mb = _FakeMB(yes)
    monkeypatch.setattr(T, "messagebox", mb)
    if dlg_ok is not None:
        monkeypatch.setattr(T, "OllamaPullDialog",
                            lambda parent, url, model: types.SimpleNamespace(
                                ok=dlg_ok, error=None if dlg_ok else "boom"))
    stub = types.SimpleNamespace(wait_window=lambda d: None)
    return T.TagTab._ensure_ollama_model(stub), mb


def test_present_model_proceeds(monkeypatch):
    res, mb = _run(monkeypatch, (True, ["gemma3:4b"]), present=True)
    assert res is True
    assert mb.info == 0 and mb.error == 0     # no prompt at all


def test_unreachable_server_is_fail_open(monkeypatch):
    # Can't check -> don't block; the runner reports Ollama problems itself.
    res, _ = _run(monkeypatch, (False, "connection refused"), present=False)
    assert res is True


def test_missing_and_declined_aborts(monkeypatch):
    res, mb = _run(monkeypatch, (True, []), present=False, yes=False)
    assert res is False
    assert mb.info == 1                        # told how to pull it later


def test_missing_pull_success_proceeds(monkeypatch):
    res, _ = _run(monkeypatch, (True, []), present=False, yes=True, dlg_ok=True)
    assert res is True


def test_missing_pull_failure_aborts(monkeypatch):
    res, mb = _run(monkeypatch, (True, []), present=False, yes=True, dlg_ok=False)
    assert res is False
    assert mb.error == 1


def test_empty_model_config_proceeds(monkeypatch):
    # No configured model at all: nothing to check, don't block.
    monkeypatch.setitem(T.CFG, "ollama", {"url": "http://x", "model": ""})
    stub = types.SimpleNamespace(wait_window=lambda d: None)
    assert T.TagTab._ensure_ollama_model(stub) is True
