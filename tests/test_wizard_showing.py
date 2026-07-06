"""
Regression for the wizard's stuck "Checking whether <model> is installed …" bug
(0.4.6). The async Ollama check finished, but the re-render guard compared bound
methods with `is` (`self._steps[self._i] is self._step_ollama_pull`), which is
ALWAYS False because attribute access mints a fresh bound method each time. So the
step never repainted until the user navigated away and back.

_showing() compares the underlying functions instead. Pinned here by binding it to
a minimal stub (no tkinter window needed): the plain `is` it replaced would fail
the on-step case.
"""
import types

import pytest

pytest.importorskip("tkinter")   # wizard imports tkinter at module load

from gui.wizard import FirstStartWizard   # noqa: E402


def _stub_on(index):
    steps = [FirstStartWizard._step_welcome,
             FirstStartWizard._step_gpu,
             FirstStartWizard._step_ollama_pull]
    return types.SimpleNamespace(_steps=steps, _i=index)


def test_showing_true_for_current_step():
    stub = _stub_on(2)   # on the Ollama step
    assert FirstStartWizard._showing(stub, FirstStartWizard._step_ollama_pull)


def test_showing_false_for_other_steps():
    stub = _stub_on(2)
    assert not FirstStartWizard._showing(stub, FirstStartWizard._step_welcome)
    assert not FirstStartWizard._showing(stub, FirstStartWizard._step_gpu)


def test_showing_is_stable_across_bound_method_accesses():
    # The exact failure mode: two separate accesses of a bound method are not
    # `is`-identical, but _showing must still report the same step as showing.
    class Tiny:
        def _step_ollama_pull(self, parent):
            pass
    obj = Tiny()
    assert obj._step_ollama_pull is not obj._step_ollama_pull   # the trap
    stub = types.SimpleNamespace(_steps=[obj._step_ollama_pull], _i=0)
    assert FirstStartWizard._showing(stub, obj._step_ollama_pull)


def test_showing_out_of_range_is_false():
    stub = types.SimpleNamespace(_steps=[], _i=0)
    assert not FirstStartWizard._showing(stub, FirstStartWizard._step_ollama_pull)
