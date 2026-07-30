"""
Settings -> Default folders.

The section had drifted out of the tab order as tools were added (Conciliation's
pair sat above the Video Upscaler's, which ships after it), which makes a long
column of near-identical folder rows harder to scan than it needs to be. These
tests pin BOTH halves of that: the on-screen row order, and the config keys the
form writes - the two can drift apart independently, since one is widget layout
and the other is a dict literal a few hundred lines away.
"""

import re

import pytest

import tkinter as tk
from tkinter import ttk

from gui.tab_settings import SettingsTab


# The tab order the app presents (gui/app.py), minus the tools that own no folder.
# Video Stabilization is last because it is not a GPU tool and sits after
# Conciliation; see CLAUDE.md "Tab order".
EXPECTED_ROWS = [
    "Batch Upscaler Photo folder:",
    "Batch Upscaler Output folder:",
    "Tag & Rename Photo folder:",
    "Video Upscaler Video folder:",
    "Video Upscaler Output folder:",
    "Conciliation Original folder:",
    "Conciliation Processed folder:",
    "Video Stabilization Video folder:",
    "Video Stabilization Output folder:",
]

EXPECTED_KEYS = [
    "upscale_source",
    "upscale_output",
    "tag_folder",
    "video_source",
    "video_output",
    "conciliate_original",
    "conciliate_processed",
    "stabilize_source",
    "stabilize_output",
]


class _FakeApp:
    def refresh_tab_exclusivity(self): pass
    def mqtt_publish(self, *a, **k): pass
    def sync_settings_defaults(self): pass


@pytest.fixture(scope="module")
def settings_tab():
    try:
        root = tk.Tk()
    except tk.TclError:                                    # no display
        pytest.skip("no Tk display")
    root.withdraw()
    tab = SettingsTab(ttk.Notebook(root), _FakeApp())
    root.update_idletasks()
    yield tab
    root.destroy()


def _widget_text(widget):
    try:
        text = widget.cget("text")
    except tk.TclError:
        return ""
    return text if isinstance(text, str) else ""


def _default_folders_section(widget):
    """The 'Default folders' LabelFrame. Scoped deliberately: other sections have
    folder rows of their own (the Video Upscaler's 'Output subfolder:'), and a
    whole-form sweep silently picked those up."""
    for child in widget.winfo_children():
        if isinstance(child, ttk.LabelFrame) and "Default folders" in _widget_text(child):
            return child
        found = _default_folders_section(child)
        if found is not None:
            return found
    return None


def _folder_labels(tab):
    """Every '… folder:' label in that section, with the grid row it sits on."""
    sec = _default_folders_section(tab)
    assert sec is not None, "the 'Default folders' section is gone"
    found = []
    for child in sec.winfo_children():
        text = _widget_text(child)
        if text.endswith("folder:"):
            found.append((child.master, int(child.grid_info().get("row", 0)), text))
    return found


def test_the_rows_are_in_tab_order(settings_tab):
    rows = _folder_labels(settings_tab)
    assert rows, "no 'folder:' rows found - the form's shape changed"
    # They all live in one section frame; order them the way the user reads them.
    ordered = [text for _parent, _row, text in sorted(rows, key=lambda r: r[1])]
    assert ordered == EXPECTED_ROWS


def test_every_tool_with_folders_is_represented(settings_tab):
    """A new tool tab that owns folders must appear here too, or its defaults are
    unreachable from Settings and only settable from the tab itself."""
    rows = {text for _p, _r, text in _folder_labels(settings_tab)}
    for tool in ("Batch Upscaler", "Tag & Rename", "Video Upscaler",
                 "Conciliation", "Video Stabilization"):
        assert any(t.startswith(tool) for t in rows), f"{tool} has no default-folder row"


def test_the_written_keys_match_the_rows(settings_tab):
    """_collect() rebuilds the whole `defaults` dict from the form, so a key missing
    here is a key silently DROPPED from config.json on the next Save - which is why
    the Stabilization tab could not simply invent its own."""
    sections, errors = settings_tab._collect()
    assert not errors
    assert list(sections["defaults"]) == EXPECTED_KEYS


def test_the_new_stabilization_fields_round_trip(settings_tab):
    """Empty-by-default must read as 'unchanged', or the app would report unsaved
    edits the moment Settings is opened."""
    assert settings_tab.is_dirty() is False
    settings_tab.default_stout_var.set(r"D:\Stabilised")
    assert settings_tab.is_dirty() is True
    settings_tab.revert()
    assert settings_tab.is_dirty() is False


def test_a_row_label_names_the_tab_it_belongs_to(settings_tab):
    """Each row has to say which tool it configures: nine near-identical folder rows
    are only navigable because the label carries the tab name."""
    for _p, _r, text in _folder_labels(settings_tab):
        assert re.match(r"^(Batch Upscaler|Tag & Rename|Video Upscaler|Conciliation|"
                        r"Video Stabilization) ", text), text
