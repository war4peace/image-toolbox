"""
Import smoke test (item 2). Import every module in scripts/ in a torch-free
environment. This directly guards two past incident classes:

  * a Remote-only install breaking because a module imports torch eagerly (the
    local torch stack isn't installed in that mode), and
  * the installer shipping a broken module set (the 0.2.5 packaging bug) — an
    import error here is the earliest, cheapest signal.

The CI runner has no torch/PIL/cv2 installed, so a module that pulls any of them
at import time fails loudly. Locally the venv HAS torch, so we ALSO assert that
importing the app modules did not drag torch in (every heavy import must stay
lazy, inside the function that needs it).
"""

import importlib
import os
import sys

import pytest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.join(os.path.dirname(_TESTS_DIR), "scripts")

# toolbox_gui imports tkinter at module load; skip that one module (only) where
# tkinter is unavailable, rather than failing the whole sweep.
_NEEDS_TK = {"toolbox_gui"}

_MODULES = sorted(
    os.path.splitext(f)[0]
    for f in os.listdir(SCRIPTS_DIR)
    if f.endswith(".py") and not f.startswith("_")
)


def test_there_are_modules_to_import():
    # A guard against the glob silently matching nothing (which would make the
    # sweep below a no-op that always "passes").
    assert len(_MODULES) >= 20, _MODULES


@pytest.mark.parametrize("mod", _MODULES)
def test_module_imports(mod):
    if mod in _NEEDS_TK and importlib.util.find_spec("tkinter") is None:
        pytest.skip("tkinter unavailable")
    importlib.import_module(mod)


def test_no_module_imported_torch_eagerly():
    # Import them all, then assert torch was never pulled in. The whole point of
    # the Remote-only install is that these modules load without the GPU stack.
    for mod in _MODULES:
        if mod in _NEEDS_TK and importlib.util.find_spec("tkinter") is None:
            continue
        importlib.import_module(mod)
    for heavy in ("torch", "timm", "cv2"):
        assert heavy not in sys.modules, (
            f"{heavy} was imported at module load — it must stay a lazy, "
            f"in-function import so Remote-only installs work")
