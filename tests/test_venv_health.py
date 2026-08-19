r"""
Defect D1: the app would not start after the system Python was uninstalled or
reinstalled, and neither the launcher nor the bootstrapper could recover it.

A venv is not self-contained on Windows: `.venv\Scripts\python.exe` is a stub that reads
`.venv\pyvenv.cfg` and executes the base Python named there. Remove or move that base and
the stub exits 103 - while every file in the app folder still looks perfectly healthy.
Under `pythonw.exe` there is no console for it to say so in, so the app "does not start"
with no window, no error and no crash log, because none of our code ever runs.

Both halves of the fix are shell code, so these tests pin the INVARIANTS as text: that
neither half decides from a path existing, and that the repair keeps its guards. The
behaviour itself was verified against a real broken venv (detected, repointed, healthy
again, no BOM written, and a cross-minor repair refused).
"""

import io
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PROBE = r'.venv\Scripts\python.exe" -c "import sys"'


def _read(name):
    with io.open(os.path.join(ROOT, name), encoding="utf-8", errors="replace") as fh:
        return fh.read()


def test_the_launcher_runs_the_interpreter_before_starting_the_gui():
    """An existence check is exactly what failed: the file IS there, it just cannot
    run. The launcher has to ask the interpreter to prove it works."""
    cmd = _read("Image Toolbox.cmd")
    assert PROBE in cmd, "the launcher no longer probes the venv interpreter"
    # A failed probe must lead to the bootstrapper, not to a launch anyway.
    between = cmd[cmd.index(PROBE):cmd.index('start "Image Toolbox"')]
    assert "goto bootstrap" in between


def test_the_launcher_still_starts_the_gui_without_a_console():
    cmd = _read("Image Toolbox.cmd")
    assert "pythonw.exe" in cmd
    assert r"scripts\toolbox_gui.py" in cmd


def _venv_step():
    ps = _read("bootstrap.ps1")
    step = ps[ps.index('Step "Creating the Python environment'):]
    return step[:step.index("# -- 4.")]


def _repair_fn():
    ps = _read("bootstrap.ps1")
    fn = ps[ps.index("function Repair-VenvHome"):]
    return fn[:fn.index("\nfunction ")]


def test_bootstrap_decides_the_venv_by_running_it():
    """`if (Test-Path ".venv\\Scripts\\python.exe")` kept the broken environment and then
    failed later, in pip, with an error naming neither Python nor the venv."""
    ps = _read("bootstrap.ps1")
    assert "function Test-VenvWorks" in ps
    step = _venv_step()
    assert "if (Test-VenvWorks)" in step
    assert step.index("if (Test-VenvWorks)") < step.index(r'Test-Path ".venv')


def test_bootstrap_repairs_before_it_rebuilds():
    """A rebuild re-downloads every package, PyTorch included. Rewriting the two lines
    in pyvenv.cfg is the whole repair when a compatible Python is present again."""
    ps = _read("bootstrap.ps1")
    assert "function Repair-VenvHome" in ps
    step = _venv_step()
    assert step.index("Repair-VenvHome") < step.index("Remove-Item"), \
        "the cheap repair must be tried before the expensive rebuild"


def test_the_repair_is_guarded_by_the_abi_boundary():
    """Repointing a 3.12 venv at a 3.13 would give a venv that starts and then fails on
    every import: worse than the honest failure it replaced."""
    fn = _repair_fn()
    assert "wantMinor" in fn and "$wantMinor -ne $ver" in fn


def test_the_repair_writes_pyvenv_cfg_without_a_bom():
    """The stub parses that file line by line, so a BOM would ride on the first key.
    Set-Content -Encoding utf8 writes one in Windows PowerShell."""
    fn = _repair_fn()
    assert "UTF8Encoding($false)" in fn
    code = [l for l in fn.splitlines() if not l.strip().startswith("#")]
    assert not any("Set-Content" in l for l in code), \
        "Set-Content would write a BOM into pyvenv.cfg"
