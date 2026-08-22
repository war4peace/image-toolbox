"""
LogPane's "collapse repeating progress lines" option (the per-minute video
"Processing:" heartbeat). Display-only: a run of matching lines collapses to just
the latest, leaving non-matching lines and separate runs intact. Needs a real Tk,
so it skips cleanly where there's no display (headless CI).
"""

import pytest
from conftest import make_tk_root

tk = pytest.importorskip("tkinter")

from gui.widgets import LogPane, COLLAPSE_PROCESSING_RE


@pytest.fixture(scope="module")
def root():
    # One Tk per module: repeatedly creating/destroying roots in a single process is
    # flaky (an occasional TclError), so tests share a root and get their own LogPane.
    r = make_tk_root()
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


def test_model_download_progress_collapses_but_keeps_the_header(pane):
    # The SeedVR2 weight pre-download prints a run of "<file>: NN% (X/Y MB)" lines under a
    # non-matching "downloading ..." header. The header stays; the progress collapses to the last.
    pane.set_collapse(True, COLLAPSE_PROCESSING_RE)
    pane.feed("    SeedVR2 model 'seedvr2_ema_3b-Q8_0.gguf' not found locally; downloading …\n")
    for done in (0, 31, 62, 3491):
        pct = int(done * 100 / 3491)
        pane.feed(f"    seedvr2_ema_3b-Q8_0.gguf: {pct}% ({done}/3491 MB)\n")
    lines = _lines(pane)
    assert len(lines) == 2                                   # header + one refreshing progress line
    assert "downloading" in lines[0]
    assert "100% (3491/3491 MB)" in lines[1]


def test_byte_only_download_progress_collapses(pane):
    # When the server sends no Content-Length the progress is byte-only ("<file>: N MB").
    pane.set_collapse(True, COLLAPSE_PROCESSING_RE)
    for mb in (10, 20, 30):
        pane.feed(f"    ema_vae_fp16.safetensors: {mb} MB\n")
    lines = _lines(pane)
    assert len(lines) == 1 and "30 MB" in lines[0]
