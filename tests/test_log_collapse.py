"""
LogPane's "collapse repeating progress lines" option (the per-minute video
"Processing:" heartbeat). Display-only: a run of matching lines collapses to just
the latest, leaving non-matching lines and separate runs intact. Needs a real Tk,
so it skips cleanly where there's no display (headless CI).
"""

import pytest

tk = pytest.importorskip("tkinter")

from gui.widgets import LogPane, COLLAPSE_PROCESSING_RE


@pytest.fixture(scope="module")
def root():
    # One Tk per module: repeatedly creating/destroying roots in a single process is
    # flaky (an occasional TclError), so tests share a root and get their own LogPane.
    try:
        r = tk.Tk()
    except tk.TclError:
        pytest.skip("no display for tkinter")
    r.withdraw()
    try:
        yield r
    finally:
        r.destroy()


@pytest.fixture
def pane(root):
    p = LogPane(root)
    try:
        yield p
    finally:
        p.destroy()


def _lines(pane):
    body = pane.text.get("1.0", "end-1c")
    return [ln for ln in body.split("\n") if ln.strip()]


def test_collapse_keeps_only_the_latest_of_a_run(pane):
    pane.set_collapse(True, COLLAPSE_PROCESSING_RE)
    for i in range(1, 5):
        pane.feed(f"    Processing: {4400 + i}/4874 frames (runtime 01:0{i}:00)\n")
    lines = _lines(pane)
    assert len(lines) == 1
    assert "4404/4874" in lines[0]


def test_collapse_off_keeps_every_line(pane):
    pane.set_collapse(False, COLLAPSE_PROCESSING_RE)
    for i in range(1, 5):
        pane.feed(f"    Processing: {4400 + i}/4874 frames\n")
    assert len(_lines(pane)) == 4


def test_a_non_matching_line_breaks_the_run(pane):
    pane.set_collapse(True, COLLAPSE_PROCESSING_RE)
    pane.feed("    Processing: 10/100 frames\n")
    pane.feed("    segment 1/2: 300 frames in 00:05:00\n")   # a real, keep-it line
    pane.feed("    Processing: 60/100 frames\n")
    pane.feed("    Processing: 90/100 frames\n")
    lines = _lines(pane)
    assert len(lines) == 3                                   # 10, segment, 90
    assert "10/100" in lines[0]
    assert "segment 1/2" in lines[1]
    assert "90/100" in lines[2]


def test_flush_collapses_a_trailing_run_without_a_newline(pane):
    # A re-fed backlog ends mid-line (no trailing newline); flush_collapse finishes it.
    pane.set_collapse(True, COLLAPSE_PROCESSING_RE)
    pane.feed("    Processing: 1/9 frames\n"
              "    Processing: 2/9 frames\n"
              "    Processing: 3/9 frames")                  # no trailing \n
    pane.flush_collapse()
    lines = _lines(pane)
    assert len(lines) == 1
    assert "3/9" in lines[0]


def test_non_processing_lines_are_untouched(pane):
    pane.set_collapse(True, COLLAPSE_PROCESSING_RE)
    pane.feed("[1/3] movie.mp4 -> 1080p\n")
    pane.feed("    SeedVR2: short-side 1080px\n")
    pane.feed("[1/3] DONE movie.mp4\n")
    assert len(_lines(pane)) == 3
