"""
First-start Wizard: the optional torch.compile-speedup step (feature #7, local video).

Two pieces are pinned here. (1) `_detect_compile` runs OFF the UI thread and must map
the Triton/compiler probe to the right step state without ever raising on first launch
(an old install missing triton_setup/msvc_setup has to degrade, not crash). (2) The
verdict copy comes from the pure `recommend_compile` (covered in test_wizard_recommend).

The probe is bound to a minimal stub (no tkinter window), the same technique
test_wizard_showing uses: `_ui` just runs the queued callback inline and
`_on_compile_detected` records the result tuple.
"""
import sys
import types

import pytest

pytest.importorskip("tkinter")   # gui.wizard imports tkinter at module load

import gui.wizard as W            # noqa: E402


def _stub():
    rec = []
    stub = types.SimpleNamespace(
        _ui=lambda fn: fn(),                       # run the queued UI callback inline
        _on_compile_detected=lambda *a: rec.append(a),
    )
    return stub, rec


def test_detect_compile_unsupported_combo(monkeypatch):
    """No pinned Triton wheel for this Python/torch -> 'unsupported', nothing to offer."""
    fake_ts = types.SimpleNamespace(is_supported=lambda: False,
                                    triton_installed=lambda: False)
    monkeypatch.setitem(sys.modules, "triton_setup", fake_ts)
    stub, rec = _stub()
    W.FirstStartWizard._detect_compile(stub)
    assert rec[0][0] == "unsupported"


def test_detect_compile_degrades_when_a_probe_raises(monkeypatch):
    """An old/broken triton_setup must degrade to 'unavailable', never crash first launch."""
    def boom():
        raise RuntimeError("no torch")
    fake_ts = types.SimpleNamespace(is_supported=boom, triton_installed=lambda: False)
    monkeypatch.setitem(sys.modules, "triton_setup", fake_ts)
    stub, rec = _stub()
    W.FirstStartWizard._detect_compile(stub)
    assert rec[0][0] == "unavailable"


def test_detect_compile_ready_reports_both_halves(monkeypatch):
    """Supported combo: report Triton + a VERIFIED compiler (verify_toolchain compiles)."""
    fake_ts = types.SimpleNamespace(is_supported=lambda: True,
                                    triton_installed=lambda: True)
    fake_ms = types.SimpleNamespace(verify_toolchain=lambda: (True, "compiled ok"),
                                    status_line=lambda: "MSVC is on PATH")
    monkeypatch.setitem(sys.modules, "triton_setup", fake_ts)
    monkeypatch.setitem(sys.modules, "msvc_setup", fake_ms)
    stub, rec = _stub()
    W.FirstStartWizard._detect_compile(stub)
    state, triton, compiler_ok, hint = rec[0]
    assert state == "ready" and triton is True and compiler_ok is True
    assert hint == "MSVC is on PATH"


def test_detect_compile_ready_without_a_compiler(monkeypatch):
    """Triton present but no working compiler: still 'ready' (the step offers the MS
    link), with compiler=False so the row shows 'Get C++ Build Tools …'."""
    fake_ts = types.SimpleNamespace(is_supported=lambda: True,
                                    triton_installed=lambda: False)
    fake_ms = types.SimpleNamespace(
        verify_toolchain=lambda: (False, "no MSVC"),
        status_line=lambda: "No Visual Studio found")
    monkeypatch.setitem(sys.modules, "triton_setup", fake_ts)
    monkeypatch.setitem(sys.modules, "msvc_setup", fake_ms)
    stub, rec = _stub()
    W.FirstStartWizard._detect_compile(stub)
    state, triton, compiler_ok, _ = rec[0]
    assert state == "ready" and triton is False and compiler_ok is False


def test_detect_compile_ready_even_if_msvc_module_is_missing(monkeypatch):
    """A supported Triton combo with msvc_setup import failing must still be 'ready'
    (compiler just reports absent), not crash."""
    fake_ts = types.SimpleNamespace(is_supported=lambda: True,
                                    triton_installed=lambda: True)
    monkeypatch.setitem(sys.modules, "triton_setup", fake_ts)
    # Force `import msvc_setup` to fail inside the probe.
    monkeypatch.setitem(sys.modules, "msvc_setup", None)
    stub, rec = _stub()
    W.FirstStartWizard._detect_compile(stub)
    state, triton, compiler_ok, _ = rec[0]
    assert state == "ready" and triton is True and compiler_ok is False
