"""A tkinter button whose `command` is None draws, enables and does nothing.

This is a real defect that shipped in 0.6.1's diagnostics dialog: the method was
called `_report` and `__init__` set `self._report = None` for the gathered report,
so `_build` (which runs before the gather thread finishes) bound the button to
`None`. Tkinter accepts `command=None` silently, so there was no traceback, no log
line and no visual difference: "Report with this file" simply did nothing, while
"Report without it" beside it worked.

Nothing catches that at import time and nothing catches it in a headless test, so
the guard is structural: in a GUI class, an instance attribute must not share a
name with a method, and every `command=self.x` must name a method that exists.
"""

import ast
import io
import os
import glob

import tkinter as tk
from tkinter import ttk

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _gui_sources():
    files = sorted(glob.glob(os.path.join(REPO, "scripts", "gui", "*.py")))
    files.append(os.path.join(REPO, "scripts", "toolbox_gui.py"))
    return [f for f in files if os.path.exists(f)]


def _own_methods(node):
    return {n.name for n in node.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def _base_names(node):
    """Base class names, however they are spelled (`ToolTab`, `tk.Toplevel`)."""
    out = []
    for base in node.bases:
        if isinstance(base, ast.Name):
            out.append(base.id)
        elif isinstance(base, ast.Attribute):
            out.append(base.attr)
    return out


def _class_index():
    """name -> (own methods, base names), across every GUI source.

    Handlers are routinely inherited: a tab's "View log" button is bound to
    `ToolTab._view_log`, which is not in the tab's own body. Resolving the base
    chain here keeps the check precise instead of degrading it to "some class
    somewhere has this method".
    """
    index = {}
    for path in _gui_sources():
        tree = ast.parse(io.open(path, encoding="utf-8").read(), path)
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                index[node.name] = (_own_methods(node), _base_names(node))
    return index


_INDEX = _class_index()


def _external_methods(base):
    """Members of a base this repo does not define, resolved for real.

    Skipping such a class outright was the first cut and it was worthless: the
    dialog that broke inherits from `tk.Toplevel`, so the one class the test exists
    for would have been excluded from it. Resolving the name against tkinter gives
    the real member list, and an unknown base falls back to `object`.
    """
    for module in (tk, ttk):
        cls = getattr(module, base, None)
        if isinstance(cls, type):
            return set(dir(cls))
    return set(dir(object))


def _visible_methods(node):
    """Own methods plus everything reachable through the base chain."""
    names = set(_own_methods(node))
    seen, queue = set(), list(_base_names(node))
    while queue:
        base = queue.pop()
        if base in seen:
            continue
        seen.add(base)
        if base in _INDEX:
            own, more = _INDEX[base]
            names |= own
            queue.extend(more)
        elif base != "object":
            names |= _external_methods(base)
    return names


def _classes(path):
    tree = ast.parse(io.open(path, encoding="utf-8").read(), path)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            yield node, _own_methods(node)


def _self_assignments(node):
    """`self.x = ...` targets anywhere in the class body, with a line number."""
    found = {}
    for sub in ast.walk(node):
        if not isinstance(sub, (ast.Assign, ast.AnnAssign)):
            continue
        targets = sub.targets if isinstance(sub, ast.Assign) else [sub.target]
        for t in targets:
            if (isinstance(t, ast.Attribute)
                    and isinstance(t.value, ast.Name)
                    and t.value.id == "self"):
                found.setdefault(t.attr, sub.lineno)
    return found


@pytest.mark.parametrize("path", _gui_sources(), ids=os.path.basename)
def test_an_attribute_never_shadows_a_method_of_the_same_gui_class(path):
    """`self._report = None` beside `def _report` is how the button broke."""
    bad = []
    for node, methods in _classes(path):
        for name, lineno in sorted(_self_assignments(node).items()):
            if name in methods:
                bad.append("%s.%s shadows the method of the same name (line %d)"
                           % (node.name, name, lineno))
    assert not bad, (
        "an instance attribute hides a method; if the method is a widget callback "
        "the widget silently becomes inert:\n  " + "\n  ".join(bad))


@pytest.mark.parametrize("path", _gui_sources(), ids=os.path.basename)
def test_every_command_self_x_names_a_method_that_exists(path):
    """A misspelt or later-renamed handler is inert, not an AttributeError."""
    bad = []
    for node, _own in _classes(path):
        methods = _visible_methods(node)
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Call):
                continue
            for kw in sub.keywords:
                if kw.arg != "command":
                    continue
                val = kw.value
                if (isinstance(val, ast.Attribute)
                        and isinstance(val.value, ast.Name)
                        and val.value.id == "self"
                        and val.attr not in methods):
                    bad.append("%s: command=self.%s is not a method of this class "
                               "(line %d)" % (node.name, val.attr, val.lineno))
    assert not bad, (
        "a widget is bound to something that is not a method here; check it is not "
        "an attribute that is None when the widget is built:\n  " + "\n  ".join(bad))


def test_the_diagnostics_dialog_report_button_is_bound_to_a_real_method():
    """The specific regression, pinned by name.

    `_report_with_file` must stay distinct from the `_report` attribute; renaming
    it back to `_report` is exactly the change that broke the button.
    """
    path = os.path.join(REPO, "scripts", "gui", "dialogs.py")
    src = io.open(path, encoding="utf-8").read()
    assert "command=self._report_with_file" in src
    assert "def _report_with_file(self)" in src
    assert "def _report(self)" not in src
