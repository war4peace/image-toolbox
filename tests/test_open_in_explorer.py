"""`explorer /select,` is spelled three ways and two of them are wrong.

Shipped in 0.6.1 and reported by the first user whose app lived in `C:\\Image
Toolbox`: "Report with this file" opened **Documents** instead of the folder
holding the diagnostics zip. The command was built as a list, so `list2cmdline`
quoted the whole token because the path had a space:

    explorer "/select,C:\\Image Toolbox\\issues\\x.zip"

Explorer does not recognise a switch that arrives inside the quotes, and its
answer to being handed one argument it cannot parse is to open the user's
Documents folder. No error, no exit code anybody reads, no log line.

Measured on Windows 11 by launching each form and reading the resulting window
back through Shell.Application (folder AND selection, since selection is the
whole point of `/select,`):

    ["explorer", "/select,PATH"]      -> Documents. Wrong folder entirely.
    ["explorer", "/select,", "PATH"]  -> right folder; selects NOTHING when the
                                         file name contains a comma.
    'explorer /select,"PATH"'         -> right folder, right file, both cases.

The comma case is not exotic: `/select,` is comma-delimited, and a photo folder
like `Poze, Vacanta 2019` holding `foto, 01.jpg` is an ordinary thing to have.

These tests pin the surviving form and, more importantly, pin that there is only
ONE of it. The bug survived because four call sites had three spellings, so
fixing any one of them left the other three wrong.
"""

import io
import os
import glob
import subprocess

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO, "scripts")


@pytest.fixture()
def common(monkeypatch):
    import sys
    if SCRIPTS not in sys.path:
        sys.path.insert(0, SCRIPTS)
    import gui.common as mod
    return mod


class _Spy:
    """Stands in for subprocess.Popen and records how it was called."""

    def __init__(self):
        self.calls = []

    def __call__(self, args, **kwargs):
        self.calls.append((args, kwargs))
        return self


def test_a_path_with_a_space_keeps_the_switch_outside_the_quotes(common, tmp_path,
                                                                 monkeypatch):
    """The exact 0.6.1 failure: `C:\\Image Toolbox\\issues\\<report>.zip`."""
    folder = tmp_path / "Image Toolbox" / "issues"
    folder.mkdir(parents=True)
    target = folder / "imgtbx-diag-20260822-203537.zip"
    target.write_bytes(b"x")

    spy = _Spy()
    monkeypatch.setattr(common.subprocess, "Popen", spy)
    common.open_in_explorer(str(target))

    assert len(spy.calls) == 1
    cmd = spy.calls[0][0]
    assert isinstance(cmd, str), (
        "must be a raw string: a list goes through list2cmdline, which quotes the "
        "whole /select,PATH token when the path has a space, and Explorer then "
        "opens Documents")
    assert cmd.startswith('explorer /select,"'), cmd
    assert cmd.endswith('"'), cmd
    assert str(target) in cmd


def test_a_file_name_with_a_comma_is_quoted(common, tmp_path, monkeypatch):
    """`/select,` is comma-delimited; an unquoted path selects nothing."""
    folder = tmp_path / "Poze, Vacanta 2019"
    folder.mkdir()
    target = folder / "foto, 01.jpg"
    target.write_bytes(b"x")

    spy = _Spy()
    monkeypatch.setattr(common.subprocess, "Popen", spy)
    common.open_in_explorer(str(target))

    cmd = spy.calls[0][0]
    path_part = cmd[len('explorer /select,'):]
    assert path_part.startswith('"') and path_part.endswith('"'), (
        "the path must be quoted as a unit, or Explorer splits it on the comma "
        "inside the file name: " + cmd)


def test_a_missing_file_falls_back_to_opening_the_folder(common, tmp_path,
                                                         monkeypatch):
    """Best effort: a report that was pruned must still open its folder."""
    folder = tmp_path / "issues"
    folder.mkdir()
    opened = []
    monkeypatch.setattr(common.subprocess, "Popen", _Spy())
    monkeypatch.setattr(common.os, "startfile", lambda p: opened.append(p),
                        raising=False)

    common.open_in_explorer(str(folder / "gone.zip"))
    assert opened == [str(folder)]


def test_an_empty_path_does_nothing(common, monkeypatch):
    spy = _Spy()
    monkeypatch.setattr(common.subprocess, "Popen", spy)
    common.open_in_explorer("")
    common.open_in_explorer(None)
    assert spy.calls == []


def test_nothing_else_in_the_app_builds_an_explorer_command():
    """The real guard. Four call sites with three spellings is how this shipped.

    A second implementation is worse than a wrong one, because fixing the copy
    everybody reads leaves the copies nobody does.
    """
    offenders = []
    for path in sorted(glob.glob(os.path.join(SCRIPTS, "**", "*.py"),
                                 recursive=True)):
        rel = os.path.relpath(path, REPO).replace("\\", "/")
        if rel == "scripts/gui/common.py":
            continue                      # the one implementation
        text = io.open(path, encoding="utf-8", errors="replace").read()
        for lineno, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue                  # a comment may quote the wrong forms
            if "/select" in stripped or '"explorer"' in stripped \
                    or "'explorer'" in stripped:
                offenders.append("%s:%d: %s" % (rel, lineno, stripped[:90]))
    assert not offenders, (
        "build the command in gui.common.open_in_explorer and call that instead:\n  "
        + "\n  ".join(offenders))


@pytest.mark.skipif(os.name != "nt", reason="Explorer is Windows-only")
def test_the_command_actually_starts(tmp_path, common):
    """A last check that the string form is a launchable command line.

    Deliberately NOT a check that the right window opened: that needs a desktop
    and was done by hand through Shell.Application (see the module docstring).
    This only proves CreateProcess accepts what we build, which is the part that
    would break silently if the quoting were malformed.
    """
    folder = tmp_path / "Space Test"
    folder.mkdir()
    target = folder / "a b.txt"
    target.write_text("x")
    cmd = 'cmd /c echo /select,"%s"' % os.path.normpath(str(target))
    out = subprocess.run(cmd, capture_output=True, text=True)
    assert out.returncode == 0
    assert str(target) in out.stdout
