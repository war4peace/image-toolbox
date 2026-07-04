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

_MODULES = sorted(
    os.path.splitext(f)[0]
    for f in os.listdir(SCRIPTS_DIR)
    if f.endswith(".py") and not f.startswith("_")
)

# The GUI package (scripts/gui/, split out of toolbox_gui.py in 0.4.3). Swept as
# `gui.<name>` so a subpackage module left out of the installer glob (the packaging
# trap called out in the split) fails here too. gui.common is tkinter-free; the
# widget/tab modules import tkinter at load.
_GUI_DIR = os.path.join(SCRIPTS_DIR, "gui")
_GUI_MODULES = sorted(
    "gui." + os.path.splitext(f)[0]
    for f in os.listdir(_GUI_DIR)
    if f.endswith(".py") and f != "__init__.py"
) if os.path.isdir(_GUI_DIR) else []

_MODULES += _GUI_MODULES

# toolbox_gui and the gui widget/tab modules import tkinter at module load; skip
# those (only) where tkinter is unavailable, rather than failing the whole sweep.
_NEEDS_TK = {"toolbox_gui"} | {m for m in _GUI_MODULES if m != "gui.common"}


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
