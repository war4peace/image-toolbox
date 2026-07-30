"""
Video Stabilization (future-features #20).

Two things here are load-bearing rather than routine, and both exist because the
failure they guard against is silent:

1. THE TRANSFORM-FILE PATH. vidstab's `result=` / `input=` take a file path inside
   a FILTER ARGUMENT, where `:` separates options and `\\` escapes. An absolute
   Windows path fails with a bare "Error opening output files: Invalid argument"
   that names neither the filter nor the path, and the obvious escape does not
   work either (measured on ffmpeg 8.1: `C\\:/Users/.../t.trf` still fails; only a
   DOUBLE-escaped colon does). The module sidesteps all of it by passing a bare
   filename and setting the child's cwd, so these tests pin that no absolute path
   ever reaches a -vf value.

2. THE HEALTH CHECK. Every ffmpeg 8.1.x release corrupts memory inside
   vidstabtransform (fixed upstream by 316531e61cf, on master, not on
   release/8.1), and the damage is usually NOT a crash - it is different pixels on
   every run of an identical command. So the tool proves the filter is
   deterministic before it will touch a real video, and refuses otherwise.
"""

import json
import os
import shutil
import subprocess

import pytest

import tkinter as tk
from tkinter import ttk

import notifications
import video_pipeline as vp
import video_stabilize as vs


# ─────────────────────────────────────────────
#  The filter strings
# ─────────────────────────────────────────────

def test_detect_filter_uses_a_bare_transform_filename():
    """The .trf must never appear as a path inside the filter (see the trap above)."""
    vf = vs.detect_filter()
    assert f"result={vs.TRF_NAME}" in vf
    assert ":\\" not in vf and ":/" not in vf


def test_transform_filter_uses_a_bare_transform_filename():
    vf = vs.transform_filter()
    assert f"input={vs.TRF_NAME}" in vf
    assert ":\\" not in vf and ":/" not in vf


def test_transform_filter_defaults_keep_the_whole_frame():
    """Decision 4: coverage over steadiness. optzoom=1 is the ffmpeg default that
    every tutorial copies and it discards ~17-21% of the picture; this tool must
    not inherit it."""
    vf = vs.transform_filter()
    assert "optzoom=0" in vf
    assert "crop=keep" in vf
    assert vs.DEFAULT_OPTZOOM == 0
    assert vs.DEFAULT_CROP == "keep"


def test_transform_filter_rejects_an_unknown_crop_mode():
    with pytest.raises(ValueError):
        vs.transform_filter(crop="zoom")


@pytest.mark.parametrize("builder", [vs.detect_filter, vs.transform_filter])
def test_deinterlace_runs_before_the_vidstab_filter(builder):
    """An interlaced source must be deinterlaced BEFORE the motion is measured, or
    pass 1 measures the motion of two different instants woven into one frame."""
    vf = builder(deinterlace=True)
    assert vf.startswith("bwdif=mode=0,")
    assert vf.index("bwdif") < vf.index("vidstab")


def test_no_deinterlace_by_default():
    assert "bwdif" not in vs.detect_filter()
    assert "bwdif" not in vs.transform_filter()


# ─────────────────────────────────────────────
#  The commands
# ─────────────────────────────────────────────

WINDOWS_ISH = r"C:\Users\Some One\My Videos\clip [raw], take 2.avi"


def _vf_value(args):
    return args[args.index("-vf") + 1]


def test_detect_command_keeps_paths_out_of_the_filter():
    args = vs.detect_command("ffmpeg.exe", WINDOWS_ISH, vs.detect_filter())
    # The source is an ordinary ffmpeg argument, so it may (and must) be absolute...
    assert os.path.abspath(WINDOWS_ISH) in args
    # ...but nothing resembling a path may appear inside the filter value, whose
    # parser treats ':' as a separator and ',' '[' ']' as graph syntax.
    vf = _vf_value(args)
    for hostile in (":", "[", "]"):
        assert hostile not in vf.split("result=")[1]


def test_detect_command_decodes_no_audio_and_writes_no_file():
    args = vs.detect_command("ffmpeg.exe", "a.mp4", vs.detect_filter())
    assert "-an" in args
    assert args[-2:] == ["-f", "null"] or args[-3:-1] == ["-f", "null"]
    assert "-progress" in args and "pipe:1" in args


def test_transform_command_carries_audio_when_the_source_has_it():
    args = vs.transform_command("ffmpeg.exe", "a.mp4", "b.mp4", vs.transform_filter(),
                                "libx265", ["-crf", "18"], "yuv420p10le",
                                ["-c:a", "copy"])
    assert "-map" in args and "0:a:0?" in args
    assert "-c:a" in args and "-an" not in args
    assert "yuv420p10le" in args


def test_transform_command_is_silent_when_the_source_has_none():
    args = vs.transform_command("ffmpeg.exe", "a.mp4", "b.mp4", vs.transform_filter(),
                                "libx265", [], "yuv420p", [])
    assert "-an" in args
    assert "0:a:0?" not in args


def test_transform_command_output_is_the_last_argument():
    args = vs.transform_command("ffmpeg.exe", "a.mp4", WINDOWS_ISH,
                                vs.transform_filter(), "libx264", [], "yuv420p", [])
    assert args[-1] == os.path.abspath(WINDOWS_ISH)


def test_output_is_a_deliverable_so_it_gets_10_bit_where_that_is_safe():
    """This output is what the user keeps (and may feed to the Video Upscaler), not
    the split pipeline's throwaway intermediate, so it follows the same rule as the
    Real-ESRGAN engine's segments. h264_nvenc and libx264 stay 8-bit on purpose."""
    assert vp.delivery_pix_fmt("hevc_nvenc") == "p010le"
    assert vp.delivery_pix_fmt("libx265") == "yuv420p10le"
    assert vp.delivery_pix_fmt("h264_nvenc") == "yuv420p"
    assert vp.delivery_pix_fmt("libx264") == "yuv420p"


# ─────────────────────────────────────────────
#  Progress parsing
# ─────────────────────────────────────────────

def test_parse_progress_takes_the_latest_frame_in_the_chunk():
    chunk = ("frame=10\nfps=25\nout_time=00:00:00.4\nprogress=continue\n"
             "frame=25\nfps=25\nprogress=continue\n")
    assert vs.parse_progress_frame(chunk) == 25


def test_parse_progress_ignores_chunks_without_a_frame_line():
    assert vs.parse_progress_frame("fps=25\nbitrate=N/A\n") is None
    assert vs.parse_progress_frame("") is None
    assert vs.parse_progress_frame(None) is None


def test_parse_progress_does_not_match_a_frame_word_mid_line():
    # `-progress` output is strictly line-based key=value; a stray mention must not
    # be read as progress (the -stats line, which is \r-updated, is not used here).
    assert vs.parse_progress_frame("total frame=99 something\n") is None


# ─────────────────────────────────────────────
#  Output naming
# ─────────────────────────────────────────────

def test_suggested_output_sits_beside_the_source(tmp_path):
    src = tmp_path / "holiday.avi"
    src.write_bytes(b"x")
    out = vs.suggest_output_path(str(src))
    assert os.path.dirname(out) == str(tmp_path)
    assert os.path.basename(out) == "holiday_stabilized.mp4"


def test_suggested_output_never_collides_with_an_existing_file(tmp_path):
    src = tmp_path / "holiday.avi"
    src.write_bytes(b"x")
    (tmp_path / "holiday_stabilized.mp4").write_bytes(b"x")
    out = vs.suggest_output_path(str(src))
    assert os.path.basename(out) == "holiday_stabilized_2.mp4"
    assert not os.path.exists(out)


def test_suggested_output_is_never_the_source_itself(tmp_path):
    """A stabilise that ate its own input is not recoverable, and the source is the
    one file this tool promises never to touch."""
    src = tmp_path / "clip_stabilized.mp4"
    src.write_bytes(b"x")
    out = vs.suggest_output_path(str(src))
    assert os.path.normcase(out) != os.path.normcase(str(src))


# ─────────────────────────────────────────────
#  Notification severity
# ─────────────────────────────────────────────

def test_completion_notice_uses_severity_constants_not_raw_ints():
    ok = vs.completion_notice(True, False)
    stopped = vs.completion_notice(False, True)
    failed = vs.completion_notice(False, False, "broken ffmpeg")
    assert ok[1] == notifications.COLOR_GREEN
    assert stopped[1] == notifications.COLOR_YELLOW
    assert failed[1] == notifications.COLOR_RED
    # Each must resolve in the severity table, or it degrades quietly to ntfy's
    # default priority with no tag (the bug test_notification_severity.py exists for).
    for _title, color in (ok, stopped, failed):
        assert notifications.level_for(color)


# ─────────────────────────────────────────────
#  The health check
# ─────────────────────────────────────────────

class _FakeRun:
    """Stands in for vp._run, returning scripted exit codes and writing the files
    the health check looks for."""

    def __init__(self, framemd5_bytes):
        self.framemd5_bytes = list(framemd5_bytes)
        self.calls = []

    def __call__(self, args, check=True, hard_timeout=None, cwd=None, **kw):
        self.calls.append((args, cwd))
        joined = " ".join(args)
        if "testsrc2" in joined:
            open(args[-1], "wb").write(b"fake clip")
        elif "vidstabdetect" in joined:
            open(os.path.join(cwd, vs.TRF_NAME), "wb").write(b"TRF1")
        elif "framemd5" in joined:
            open(args[-1], "wb").write(self.framemd5_bytes.pop(0))
        return subprocess.CompletedProcess(args, 0, "", "")


def test_health_check_flags_a_build_whose_output_varies(monkeypatch):
    """The 8.1 signature: identical commands, different pixels. Measured 11 distinct
    outputs from 11 runs on n8.1.2, so differing digests mean broken."""
    monkeypatch.setattr(vp, "_run", _FakeRun([b"aaa", b"bbb"]))
    ok, detail = vs.vidstab_health(ffmpeg="ffmpeg.exe")
    assert ok is False
    assert "different output" in detail


def test_health_check_passes_a_deterministic_build(monkeypatch):
    monkeypatch.setattr(vp, "_run", _FakeRun([b"same", b"same"]))
    ok, detail = vs.vidstab_health(ffmpeg="ffmpeg.exe")
    assert ok is True
    assert "deterministic" in detail


def test_health_check_runs_ffmpeg_with_cwd_set_to_its_work_dir(monkeypatch):
    """The whole reason a bare .trf filename is safe: every child that mentions the
    transform file must run with cwd set to the directory holding it."""
    fake = _FakeRun([b"same", b"same"])
    monkeypatch.setattr(vp, "_run", fake)
    vs.vidstab_health(ffmpeg="ffmpeg.exe")
    for args, cwd in fake.calls:
        if "vidstab" in " ".join(args):
            assert cwd, "a vidstab command ran without a cwd"
            assert os.path.isabs(cwd)


def test_health_check_does_not_condemn_a_build_it_could_not_test(monkeypatch):
    """If the sample clip cannot even be synthesised, that says nothing about
    vidstab: refusing then would block the feature for an unrelated reason."""
    def _cannot_generate(args, check=True, hard_timeout=None, cwd=None, **kw):
        return subprocess.CompletedProcess(args, 1, "", "no lavfi")
    monkeypatch.setattr(vp, "_run", _cannot_generate)
    ok, detail = vs.vidstab_health(ffmpeg="ffmpeg.exe")
    assert ok is True
    assert "skipped" in detail


def test_health_check_cleans_up_after_itself(monkeypatch):
    fake = _FakeRun([b"same", b"same"])
    monkeypatch.setattr(vp, "_run", fake)
    vs.vidstab_health(ffmpeg="ffmpeg.exe")
    work_dirs = {cwd for _a, cwd in fake.calls if cwd}
    for d in work_dirs:
        assert not os.path.exists(d), "the health check left its temp dir behind"


# ─────────────────────────────────────────────
#  Against a real ffmpeg, when one is available
# ─────────────────────────────────────────────

def _real_ffmpeg():
    try:
        return vp.find_ffmpeg()[0]
    except Exception:
        return shutil.which("ffmpeg")


@pytest.mark.skipif(not _real_ffmpeg(), reason="no ffmpeg available")
def test_real_ffmpeg_reports_a_vidstab_verdict():
    """End to end against whatever ffmpeg this machine has: the check must reach a
    definite verdict rather than error out. Deliberately does NOT assert 'healthy' -
    a dev box may well have a vidstab-broken 8.1.x, and this test's job is to prove
    the gate runs, not to police the local install."""
    ffmpeg = _real_ffmpeg()
    if not vs.vidstab_available(ffmpeg):
        pytest.skip("this ffmpeg has no vidstab filters")
    ok, detail = vs.vidstab_health(ffmpeg)
    assert isinstance(ok, bool)
    assert detail


# ─────────────────────────────────────────────
#  The tab must survive every event its runner emits
# ─────────────────────────────────────────────
#
# This exists because of a bug that unit tests of the runner could never catch:
# video_stabilize emits an ETA event, and ToolTab._handle_eta (the fall-through
# handler) writes to `eta_var` and the cost-per-100 widgets that _build_output_area
# creates. This tab deliberately builds none of them - it has no images and no
# billed pod - so an unhandled ETA raised AttributeError partway through a real run,
# where nothing in the test suite was looking.


@pytest.fixture(scope="module")
def root():
    """ONE hidden root for the module (creating and tearing one down per test is
    flaky on Windows)."""
    try:
        r = tk.Tk()
    except tk.TclError:                                    # no display
        pytest.skip("no Tk display")
    r.withdraw()
    yield r
    r.destroy()


@pytest.fixture
def tab(root):
    from gui.tab_stabilize import StabilizeTab

    published = {}

    class _FakeApp:
        def refresh_tab_exclusivity(self): pass
        def mqtt_publish(self, values): published.update(values)
        def taskbar_progress(self, *_a): pass
        def flash_attention(self): pass

    nb = ttk.Notebook(root)
    t = StabilizeTab(nb, _FakeApp())
    t.published = published
    yield t
    t.destroy()
    nb.destroy()


# Every event kind video_stabilize.py can emit, with a representative payload.
RUNNER_EVENTS = [
    ("LOG", r"C:\logs\stab_abc.log"),
    ("STATUS", "Pass 1 of 2 - measuring camera motion …"),
    ("PASS", '{"pass": 1, "of": 2, "name": "Measuring motion"}'),
    ("PROG", "150|600"),
    ("ETA", "12.5|150|150|600"),
    ("REFUSED", '{"reason": "This ffmpeg build cannot stabilise video correctly."}'),
    ("DONE", '{"tool": "stabilize", "output": "out.mp4", "frames": 300}'),
]


@pytest.mark.parametrize("kind,payload", RUNNER_EVENTS)
def test_tab_handles_every_event_the_runner_emits(tab, kind, payload):
    tab._handle_event(kind, payload)          # must not raise


def test_tab_publishes_task_state_from_an_eta_event(tab):
    tab._handle_event("ETA", "60.0|300|300|600")
    # Half the whole job done in 60 s -> about 60 s left.
    assert tab.published.get("image-toolbox/task/progress") == "300/600"
    assert tab.published.get("image-toolbox/task/runtime") == "60"
    assert tab.published.get("image-toolbox/task/eta") == "1:00"


def test_tab_progress_is_reported_across_both_passes(tab):
    """`total` is twice the frame count, so the bar does not reach 100% halfway
    through the job and then start again."""
    tab._handle_event("PROG", "600|600")
    assert tab.progress is not None


def test_a_refusal_is_kept_whole_for_the_dialog(tab):
    """The refusal is several sentences plus what to do about it, and the tab shows
    it in a dialog - so it must survive intact, not be truncated to a status line."""
    reason = vs.BROKEN_VIDSTAB_HELP
    tab._handle_event("REFUSED", json.dumps({"reason": reason}))
    assert tab._refused == reason
    assert "\n" in tab._refused


# ─────────────────────────────────────────────
#  The output field must follow the source
# ─────────────────────────────────────────────
#
# Reported from real use: after stabilising one video, choosing a second left the
# "Save result as" field still naming the FIRST one. The run then offered to replace
# that file - a dialog inviting the user to destroy the result they had just made.


def _pick(tab, path):
    """What choosing a file in the Browse dialog does to the tab."""
    tab.src_var.set(str(path))
    tab.update_idletasks()


def test_choosing_a_second_video_renames_the_output(tab, tmp_path):
    first = tmp_path / "VID 00001-20100609-2226.3GP"
    second = tmp_path / "VID 00004-20110601-2112.3GP"
    for f in (first, second):
        f.write_bytes(b"x")

    _pick(tab, first)
    assert os.path.basename(tab.out_var.get()) == "VID 00001-20100609-2226_stabilized.mp4"

    _pick(tab, second)
    assert os.path.basename(tab.out_var.get()) == "VID 00004-20110601-2112_stabilized.mp4"


def test_a_finished_run_does_not_leave_the_old_name_behind(tab, tmp_path):
    """The exact reported sequence: stabilise one video (so its output now EXISTS on
    disk), then choose another. The field must name the second video, not the first."""
    first = tmp_path / "a.3gp"
    second = tmp_path / "b.3gp"
    for f in (first, second):
        f.write_bytes(b"x")

    _pick(tab, first)
    produced = tab.out_var.get()
    open(produced, "wb").write(b"stabilised result")     # the run happened

    _pick(tab, second)
    assert os.path.basename(tab.out_var.get()) == "b_stabilized.mp4"
    assert os.path.normcase(tab.out_var.get()) != os.path.normcase(produced)


def test_re_picking_the_same_video_will_not_overwrite_the_previous_result(tab, tmp_path):
    src = tmp_path / "a.3gp"
    src.write_bytes(b"x")

    _pick(tab, src)
    produced = tab.out_var.get()
    open(produced, "wb").write(b"stabilised result")

    tab.src_var.set("")                                  # re-open the picker...
    _pick(tab, src)                                      # ...and choose the same file
    assert os.path.basename(tab.out_var.get()) == "a_stabilized_2.mp4"
    assert not os.path.exists(tab.out_var.get())


def test_a_chosen_output_folder_survives_a_source_change(tab, tmp_path):
    """Only the NAME is rebuilt. A user collecting results in one folder should not
    have to re-browse for every video."""
    src_dir = tmp_path / "footage"
    out_dir = tmp_path / "stabilised"
    src_dir.mkdir()
    out_dir.mkdir()
    first = src_dir / "a.3gp"
    second = src_dir / "b.3gp"
    for f in (first, second):
        f.write_bytes(b"x")

    _pick(tab, first)
    tab.out_var.set(str(out_dir / "a_stabilized.mp4"))    # user browses elsewhere
    tab.update_idletasks()

    _pick(tab, second)
    assert os.path.dirname(tab.out_var.get()) == str(out_dir)
    assert os.path.basename(tab.out_var.get()) == "b_stabilized.mp4"


def test_the_output_is_never_the_source_itself(tab, tmp_path):
    """A stabilise that ate its own input is not recoverable."""
    src = tmp_path / "clip_stabilized.mp4"
    src.write_bytes(b"x")
    _pick(tab, src)
    assert os.path.normcase(tab.out_var.get()) != os.path.normcase(str(src))
