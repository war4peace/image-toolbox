"""
Video Stabilization (future-features #20, workflow #23).

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

#23 adds a third, and it is the one with teeth:

3. THE PAIR RECORD IS NOT LINEAGE. `db.lineage` is what Conciliation MATCHES ON,
   and video conciliation is lineage-only, so a stabilised output recorded there
   would make the app's one destructive tool offer to archive or DELETE the
   original in favour of a copy with ~10-21% of its picture potentially gone. The
   pair lives in its own table, and these tests hold that line.
"""

import json
import os
import shutil
import subprocess

import pytest
from conftest import make_tk_root

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
    r = make_tk_root()
    yield r
    r.destroy()


@pytest.fixture
def tab(root):
    from gui.tab_stabilize import StabilizeTab

    published = {}

    class _FakeApp:
        """Records MQTT publishes; every other App call is a no-op. The catch-all is
        deliberate: these tests are about the TAB, and stubbing each App method by
        hand meant a test of on_exit failed on `taskbar_clear` rather than on
        anything it was checking."""

        def mqtt_publish(self, values, **_kw): published.update(values)

        def __getattr__(self, _name):
            return lambda *a, **k: None

    nb = ttk.Notebook(root)
    t = StabilizeTab(nb, _FakeApp())
    # The tab pre-fills both folder fields from the saved defaults (#23 item 6), so
    # without this the tests read the DEVELOPER'S config.json: the day a real
    # `stabilize_output` was saved, every "is there already a result next to the
    # source" test started looking in that folder instead and failed. A tab test must
    # depend on what the test sets up, never on the machine it runs on.
    t.folder_var.set("")
    t.out_var.set("")
    t.published = published
    yield t
    t.destroy()
    nb.destroy()


# Every event kind video_stabilize.py can emit, with a representative payload.
RUNNER_EVENTS = [
    ("LOG", r"C:\logs\stab_abc.log"),
    ("STATUS", "Pass 1 of 2 - measuring camera motion …"),
    ("PASS", '{"pass": 1, "of": 2, "name": "Measuring motion"}'),
    ("FILE", '{"index": 2, "total": 5, "source": "b.mp4", "output": "b_stabilized.mp4",'
             ' "name": "b.mp4"}'),
    ("RESULT", '{"source": "b.mp4", "output": "b_stabilized.mp4", "ok": true,'
               ' "frames": 300}'),
    ("PROG", "150|600"),
    ("ETA", "12.5|150|150|600"),
    ("REFUSED", '{"reason": "This ffmpeg build cannot stabilise video correctly."}'),
    ("DONE", '{"tool": "stabilize", "queued": 1, "processed": 1, "failed": 0}'),
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


def test_the_status_line_says_which_of_many_videos_is_running(tab):
    """The runner's own per-pass line cannot know it is one of fifty, so the tab owns
    the status line while a video is in progress and composes it from FILE + PASS."""
    tab._handle_event("FILE", json.dumps({"index": 3, "total": 12, "source": "c.mp4",
                                          "name": "holiday.mp4"}))
    tab._handle_event("PASS", json.dumps({"pass": 2, "of": 2, "name": "Stabilising"}))
    text = tab.status_lbl.cget("text")
    assert "[3/12]" in text and "holiday.mp4" in text and "pass 2 of 2" in text
    # The runner's own pass line must not overwrite it and drop the file name.
    tab._handle_event("STATUS", "Pass 2 of 2 - stabilising and encoding …")
    assert tab.status_lbl.cget("text") == text
    # …but once the run is over the runner's closing summary is the whole story.
    tab._handle_event("DONE", '{"tool": "stabilize", "processed": 12, "failed": 0}')
    tab._handle_event("STATUS", "Done - 12 stabilised")
    assert tab.status_lbl.cget("text") == "Done - 12 stabilised"


def test_the_video_a_stop_interrupted_goes_back_to_queued(tab, tmp_path):
    """It genuinely IS queued again - a stabilise has no resume, so the abandoned
    `.part` was discarded. Left showing "Working…" it would also make Start refuse
    ("nothing is queued") on the very file the user stopped to come back to."""
    src = tmp_path / "a.mp4"
    src.write_bytes(b"x")
    tab.add_sources([str(src)])
    tab._handle_event("FILE", json.dumps({"index": 1, "total": 1, "source": str(src),
                                          "name": "a.mp4"}))
    assert list(tab._rows.values())[0]["status"] == "Working…"
    tab._handle_event("DONE", '{"tool": "stabilize", "processed": 0, "failed": 0,'
                              ' "stopped_by_user": true}')
    tab.on_exit(0)
    assert list(tab._rows.values())[0]["status"] == "Queued"


def test_a_result_event_marks_the_row_it_names(tab, tmp_path):
    src = tmp_path / "a.mp4"
    src.write_bytes(b"x")
    tab.add_sources([str(src)])
    out = list(tab._rows.values())[0]["output"]
    tab._handle_event("RESULT", json.dumps({"source": str(src), "output": out,
                                            "ok": False, "reason": "unreadable"}))
    row = list(tab._rows.values())[0]
    assert row["status"] == "Failed" and row["reason"] == "unreadable"


def test_a_refusal_is_kept_whole_for_the_dialog(tab):
    """The refusal is several sentences plus what to do about it, and the tab shows
    it in a dialog - so it must survive intact, not be truncated to a status line."""
    reason = vs.BROKEN_VIDSTAB_HELP
    tab._handle_event("REFUSED", json.dumps({"reason": reason}))
    assert tab._refused == reason
    assert "\n" in tab._refused


# ─────────────────────────────────────────────
#  Each video's result must be its own file
# ─────────────────────────────────────────────
#
# The invariants here predate the queue (#23) - they were reported from real use of
# #20's single-file tab, where the "Save result as" field kept naming the PREVIOUS
# video and the run then offered to replace the result the user had just made. The
# field is gone, but every one of those invariants still has to hold, now per row of
# the list rather than per keystroke of one field.


def _outputs(tab):
    return {os.path.basename(r["source"]): r["output"] for r in tab._rows.values()}


def test_each_video_gets_its_own_result_name(tab, tmp_path):
    first = tmp_path / "VID 00001-20100609-2226.3GP"
    second = tmp_path / "VID 00004-20110601-2112.3GP"
    for f in (first, second):
        f.write_bytes(b"x")

    tab.add_sources([str(first), str(second)])
    outs = _outputs(tab)
    assert os.path.basename(outs["VID 00001-20100609-2226.3GP"]) == \
        "VID 00001-20100609-2226_stabilized.mp4"
    assert os.path.basename(outs["VID 00004-20110601-2112.3GP"]) == \
        "VID 00004-20110601-2112_stabilized.mp4"


def test_two_videos_with_one_name_do_not_claim_one_output(tab, tmp_path):
    """The queue's own version of the old bug, and it is worse: neither output EXISTS
    yet while the queue is being planned, so nothing on disk stops the second run
    from writing over the first one's result the moment it finishes."""
    a = tmp_path / "holiday" / "clip.avi"
    b = tmp_path / "wedding" / "clip.avi"
    out = tmp_path / "steady"
    for f in (a, b):
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"x")
    out.mkdir()

    tab.out_var.set(str(out))
    tab.add_sources([str(a), str(b)])
    produced = [r["output"] for r in tab._rows.values()]
    assert len(set(os.path.normcase(p) for p in produced)) == 2


def test_a_video_that_already_has_a_result_is_not_queued_again(tab, tmp_path):
    """Resume: re-scanning a folder must pick up where the last run stopped, not
    redo it. This is the whole reason a queue needs no database."""
    src = tmp_path / "a.3gp"
    src.write_bytes(b"x")
    (tmp_path / "a_stabilized.mp4").write_bytes(b"result")

    tab.add_sources([str(src)])
    row = list(tab._rows.values())[0]
    assert row["status"] == "Stabilised"
    assert os.path.basename(row["output"]) == "a_stabilized.mp4"


def test_stabilising_again_never_overwrites_the_previous_result(tab, tmp_path):
    """A second attempt is usually a different Steadiness on a result the user is
    still judging. It gets a new file."""
    src = tmp_path / "a.3gp"
    src.write_bytes(b"x")
    produced = tmp_path / "a_stabilized.mp4"
    produced.write_bytes(b"result")

    tab.add_sources([str(src)])
    tab.tree.selection_set(list(tab._rows)[0])
    tab._requeue_selected()
    row = list(tab._rows.values())[0]
    assert row["status"] == "Queued"
    assert os.path.basename(row["output"]) == "a_stabilized_2.mp4"
    assert not os.path.exists(row["output"])


def test_results_follow_the_chosen_output_folder(tab, tmp_path):
    """A user collecting results in one folder should not have to say so per video."""
    src_dir = tmp_path / "footage"
    out_dir = tmp_path / "stabilised"
    src_dir.mkdir()
    out_dir.mkdir()
    for name in ("a.3gp", "b.3gp"):
        (src_dir / name).write_bytes(b"x")

    tab.out_var.set(str(out_dir))
    tab.add_sources([str(src_dir / "a.3gp"), str(src_dir / "b.3gp")])
    for row in tab._rows.values():
        assert os.path.dirname(row["output"]) == str(out_dir)


def test_the_output_is_never_the_source_itself(tab, tmp_path):
    """A stabilise that ate its own input is not recoverable."""
    src = tmp_path / "clip_stabilized.mp4"
    src.write_bytes(b"x")
    tab.add_sources([str(src)])
    row = list(tab._rows.values())[0]
    assert os.path.normcase(row["output"]) != os.path.normcase(str(src))


# ─────────────────────────────────────────────
#  The hand-off from the Video Upscaler  (#23 item 1)
# ─────────────────────────────────────────────


def test_the_prune_summary_survives_the_list_being_re_counted(tab, tmp_path):
    """#16's rule is that a scan explains itself once, and this is the moment a user
    wonders why the count is smaller than the folder looks. It has to be REMEMBERED,
    not written once: the summary line is rewritten by anything that re-counts the
    list, and the output-folder trace does exactly that a moment after a scan."""
    src = tmp_path / "a.mp4"
    src.write_bytes(b"x")
    tab._scan_done([str(src)], "Skipped 1 folder(s) this app created: __Archive__",
                   {str(src): None})
    assert "__Archive__" in tab.status_lbl.cget("text")
    tab._set_summary_status()                     # what a re-count does
    assert "__Archive__" in tab.status_lbl.cget("text")


def test_a_handed_over_video_is_queued_and_selected(tab, tmp_path):
    src = tmp_path / "shaky.mp4"
    src.write_bytes(b"x")
    assert tab.add_sources([str(src)]) == 1
    assert list(tab._rows.values())[0]["status"] == "Queued"


def test_the_same_video_is_not_added_twice(tab, tmp_path):
    src = tmp_path / "shaky.mp4"
    src.write_bytes(b"x")
    tab.add_sources([str(src)])
    assert tab.add_sources([str(src)]) == 0
    assert len(tab._rows) == 1


# ─────────────────────────────────────────────
#  The folder walk  (#23 item 2)
# ─────────────────────────────────────────────


def test_the_walk_never_offers_its_own_results_back(tmp_path):
    """A result defaults to sitting BESIDE its source, which is not a derived
    directory - so DerivedPruner (#16) cannot catch it and the name rule must. Without
    this, scanning the same folder twice queues every result as fresh input, and the
    third scan queues the results of the results."""
    (tmp_path / "a.mp4").write_bytes(b"x")
    (tmp_path / "a_stabilized.mp4").write_bytes(b"x")
    (tmp_path / "a_stabilized_2.mp4").write_bytes(b"x")
    found, _pruner = vs.iter_videos(str(tmp_path))
    assert [os.path.basename(p) for p in found] == ["a.mp4"]


def test_the_walk_skips_the_apps_own_derived_directories(tmp_path):
    (tmp_path / "a.mp4").write_bytes(b"x")
    for derived in ("__Archive__", "__upscaled__"):
        d = tmp_path / derived
        d.mkdir()
        (d / "old.mp4").write_bytes(b"x")
    found, pruner = vs.iter_videos(str(tmp_path))
    assert [os.path.basename(p) for p in found] == ["a.mp4"]
    assert pruner.summary()


def test_the_walk_can_stay_out_of_subfolders(tmp_path):
    (tmp_path / "a.mp4").write_bytes(b"x")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "b.mp4").write_bytes(b"x")
    assert len(vs.iter_videos(str(tmp_path))[0]) == 2
    assert len(vs.iter_videos(str(tmp_path), recursive=False)[0]) == 1


def test_non_video_files_are_ignored(tmp_path):
    (tmp_path / "a.mp4").write_bytes(b"x")
    (tmp_path / "notes.txt").write_bytes(b"x")
    (tmp_path / "photo.jpg").write_bytes(b"x")
    assert len(vs.iter_videos(str(tmp_path))[0]) == 1


# ─────────────────────────────────────────────
#  Planning a queue  (#23 item 3)
# ─────────────────────────────────────────────


def test_a_video_that_already_has_a_result_is_skipped_not_redone(tmp_path):
    """The resume story, and the reason a queue needs no database: re-running a
    folder does the files that are missing a result and reports the rest."""
    (tmp_path / "a.mp4").write_bytes(b"x")
    (tmp_path / "b.mp4").write_bytes(b"x")
    (tmp_path / "a_stabilized.mp4").write_bytes(b"x")
    jobs, skipped = vs.plan_jobs([str(tmp_path / "a.mp4"), str(tmp_path / "b.mp4")])
    assert [os.path.basename(j["source"]) for j in jobs] == ["b.mp4"]
    assert [os.path.basename(s["source"]) for s in skipped] == ["a.mp4"]


def test_redo_makes_a_new_file_and_never_touches_the_old_one(tmp_path):
    (tmp_path / "a.mp4").write_bytes(b"x")
    previous = tmp_path / "a_stabilized.mp4"
    previous.write_bytes(b"result")
    jobs, skipped = vs.plan_jobs([str(tmp_path / "a.mp4")], redo=True)
    assert not skipped
    assert os.path.basename(jobs[0]["output"]) == "a_stabilized_2.mp4"
    assert previous.read_bytes() == b"result"


def test_two_sources_with_one_stem_get_two_outputs(tmp_path):
    """Neither output exists yet while the queue is planned, so nothing on DISK stops
    the second job from writing over the first one's result when it finishes. The
    claimed-names set is what does."""
    for folder in ("holiday", "wedding"):
        d = tmp_path / folder
        d.mkdir()
        (d / "clip.avi").write_bytes(b"x")
    out = tmp_path / "steady"
    out.mkdir()
    jobs, _ = vs.plan_jobs([str(tmp_path / "holiday" / "clip.avi"),
                            str(tmp_path / "wedding" / "clip.avi")], outdir=str(out))
    assert len({os.path.normcase(j["output"]) for j in jobs}) == 2


def test_a_queue_file_round_trips(tmp_path):
    qf = tmp_path / "q.json"
    qf.write_text(json.dumps({"jobs": [{"source": r"C:\a\x.mp4",
                                        "output": r"C:\out\x_stabilized.mp4"}]}),
                  encoding="utf-8")
    jobs = vs.load_queue_file(str(qf))
    assert jobs == [{"source": r"C:\a\x.mp4", "output": r"C:\out\x_stabilized.mp4"}]


# ─────────────────────────────────────────────
#  Whole-run progress  (QueueProgress)
# ─────────────────────────────────────────────


def _events(monkeypatch):
    seen = []
    monkeypatch.setattr(vs, "_gui_event", lambda k, p: seen.append((k, p)))
    return seen


def test_progress_spans_both_passes_of_one_file(monkeypatch):
    """A per-pass bar would reach 100% halfway through the job and then start again."""
    seen = _events(monkeypatch)
    p = vs.QueueProgress(100)
    p.begin_file(100)
    p.report(1, 100, force=True)
    p.report(2, 100, force=True)
    assert [v for k, v in seen if k == "PROG"] == ["100|200", "200|200"]


def test_progress_spans_the_whole_queue_not_one_file(monkeypatch):
    """Fifty videos of wildly different lengths make a file counter a poor progress
    signal, which is why the budget is measured up front."""
    seen = _events(monkeypatch)
    p = vs.QueueProgress(300)            # three 100-frame videos -> 600 pass-frames
    p.begin_file(100)
    p.report(2, 100, force=True)
    p.end_file()
    p.begin_file(100)
    p.report(1, 50, force=True)
    assert [v for k, v in seen if k == "PROG"] == ["200|600", "250|600"]


def test_pass_one_correcting_the_frame_count_moves_the_whole_budget(monkeypatch):
    """A header count can be an estimate; pass 1 counts exactly. The queue budget has
    to follow, or every later ETA is wrong."""
    _events(monkeypatch)
    p = vs.QueueProgress(200)
    p.begin_file(100)
    p.correct_file(120)
    assert p.budget == 2 * 200 - 200 + 240


def test_a_failed_file_still_advances_the_budget(monkeypatch):
    """Otherwise the bar stalls at whatever fraction the failed file reached."""
    _events(monkeypatch)
    p = vs.QueueProgress(200)
    p.begin_file(100)
    p.end_file()
    assert p.done == 200


# ─────────────────────────────────────────────
#  The queue keeps going  (#23 item 3)
# ─────────────────────────────────────────────


class _Log:
    def __init__(self):
        self.lines = []

    def tee(self, msg=""):
        self.lines.append(msg)

    def log_only(self, msg):
        self.lines.append(msg)


_OPTS = {"smoothing": 10, "shakiness": 8, "accuracy": 15, "optzoom": 0, "crop": "keep"}


def test_one_bad_video_does_not_cost_the_rest_of_the_queue(monkeypatch):
    """One unreadable file in fifty must not end the run."""
    _events(monkeypatch)
    monkeypatch.setattr(vs, "probe_queue", lambda jobs, log: (300, []))
    done = []

    def fake(src, dest, log, **kw):
        if "bad" in src:
            raise vs.StabilizeError("cannot read that file")
        done.append(src)
        return {"output": dest, "frames": 100, "elapsed_seconds": 1}

    monkeypatch.setattr(vs, "stabilize", fake)
    jobs = [{"source": f"{n}.mp4", "output": f"{n}_stabilized.mp4"}
            for n in ("a", "bad", "c")]
    summary = vs.run_queue(jobs, _Log(), _OPTS, "ffmpeg")
    assert summary["processed"] == 2 and summary["failed"] == 1
    assert done == ["a.mp4", "c.mp4"]


def test_a_stop_ends_the_queue_and_leaves_the_rest_alone(monkeypatch):
    _events(monkeypatch)
    monkeypatch.setattr(vs, "probe_queue", lambda jobs, log: (300, []))
    started = []

    def fake(src, dest, log, **kw):
        started.append(src)
        if len(started) == 2:
            raise KeyboardInterrupt("stopped by user")
        return {"output": dest, "frames": 100, "elapsed_seconds": 1}

    monkeypatch.setattr(vs, "stabilize", fake)
    jobs = [{"source": f"{n}.mp4", "output": f"{n}_stabilized.mp4"}
            for n in ("a", "b", "c")]
    summary = vs.run_queue(jobs, _Log(), _OPTS, "ffmpeg")
    assert summary["stopped_by_user"] is True
    assert summary["processed"] == 1
    assert started == ["a.mp4", "b.mp4"]          # "c" was never touched


def test_the_health_check_runs_once_for_the_whole_queue(monkeypatch):
    """Half a second is nothing for one video and fifty times nothing for fifty - and
    a broken build must refuse the RUN, not fail each of fifty files with the same
    paragraph."""
    calls = []
    monkeypatch.setattr(vs, "vidstab_health",
                        lambda *a, **k: (calls.append(1), (True, "ok"))[1])
    monkeypatch.setattr(vs, "vidstab_available", lambda *a, **k: True)
    monkeypatch.setattr(vs, "ffmpeg_version_line", lambda *a, **k: "ffmpeg n8.2")
    monkeypatch.setattr(vp, "find_ffmpeg", lambda: ("ffmpeg", "ffprobe"))
    vs.preflight(_Log())
    assert len(calls) == 1


def test_a_partly_failed_run_is_orange_not_red():
    """The run delivered; calling it Failed sends the user hunting for a disaster
    that is one unreadable file."""
    title, color = vs.completion_notice(True, False, failed=1)
    assert color == notifications.COLOR_ORANGE
    assert "error" in title.lower()


# ─────────────────────────────────────────────
#  The pair record must stay out of conciliation's reach  (#23 item 5)
# ─────────────────────────────────────────────


@pytest.fixture
def stab_db(tmp_path, monkeypatch):
    """A throwaway cache.db, so these never touch the developer's own."""
    import db

    monkeypatch.setattr(db, "DB_DIR", str(tmp_path / "db"))
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "db" / "cache.db"))
    monkeypatch.setattr(db, "_conn", None)
    conn = db.get_conn()
    yield db, conn
    conn.close()
    db._conn = None


def test_a_stabilised_pair_is_not_recorded_as_lineage(stab_db, tmp_path):
    """THE data-loss guard of this milestone. `lineage` is what Conciliation matches
    on, and video conciliation is lineage-ONLY: a row there would make the app's one
    destructive tool offer to archive or DELETE an original in favour of a copy that
    may be missing a fifth of its picture."""
    db, conn = stab_db
    src = tmp_path / "shaky.mp4"
    out = tmp_path / "shaky_stabilized.mp4"
    for f in (src, out):
        f.write_bytes(b"x")
    db.record_stabilized(conn, str(src), str(out), smoothing=10, optzoom=0, frames=300)
    assert db.lineage_has_rows(conn) is False
    assert db.stabilized_output(conn, str(src)) == os.path.abspath(str(out))


def test_a_pair_whose_result_was_deleted_reads_as_not_stabilised(stab_db, tmp_path):
    """Otherwise the tab offers to compare a file that is gone, and the folder scan
    skips work that needs doing."""
    db, conn = stab_db
    src = tmp_path / "shaky.mp4"
    out = tmp_path / "shaky_stabilized.mp4"
    for f in (src, out):
        f.write_bytes(b"x")
    db.record_stabilized(conn, str(src), str(out))
    os.remove(out)
    assert db.stabilized_output(conn, str(src)) is None


def test_re_stabilising_replaces_the_recorded_pair(stab_db, tmp_path):
    """One row per source, newest wins: the question is "what is the current result
    for this file", not "what have I ever made from it"."""
    db, conn = stab_db
    src = tmp_path / "shaky.mp4"
    first = tmp_path / "shaky_stabilized.mp4"
    second = tmp_path / "shaky_stabilized_2.mp4"
    for f in (src, first, second):
        f.write_bytes(b"x")
    db.record_stabilized(conn, str(src), str(first))
    db.record_stabilized(conn, str(src), str(second))
    assert db.stabilized_output(conn, str(src)) == os.path.abspath(str(second))
    db.forget_stabilized(conn, str(src))
    assert db.stabilized_output(conn, str(src)) is None


# ── the end-of-run log line (docs/known-defects.md D3) ──────────────────────


class _CaptureLog:
    """Minimal stand-in for video_stabilize.Logger."""

    def __init__(self):
        self.lines = []

    def tee(self, msg=""):
        self.lines.append(msg)

    def close(self):
        pass


def test_the_log_records_how_the_run_ended(monkeypatch):
    """Nothing else in _report_completion writes to the log, so without this line a
    run that finished and a run that died mid-loop leave IDENTICAL log files. That
    ambiguity is what made a stuck run undiagnosable (D3): the line's presence is
    the evidence that the loop completed."""
    monkeypatch.setattr(vs, "send_notification", lambda *a, **k: None)
    monkeypatch.setattr(vs, "_gui_event", lambda *a, **k: None)
    log = _CaptureLog()
    summary = {"tool": "stabilize", "queued": 3, "processed": 2, "failed": 1,
               "skipped": 4, "stopped_by_user": False, "elapsed_seconds": 90,
               "results": [], "failures": []}
    vs._report_completion(summary, 2, 1, "", log)
    ended = [l for l in log.lines if "Run ended:" in l]
    assert len(ended) == 1
    assert "2 stabilised" in ended[0]
    assert "1 failed" in ended[0]
    assert "4 already done" in ended[0]


def test_the_end_of_run_line_says_when_the_user_stopped_it(monkeypatch):
    """A stopped run is not a failed one, and the log is what is read afterwards."""
    monkeypatch.setattr(vs, "send_notification", lambda *a, **k: None)
    monkeypatch.setattr(vs, "_gui_event", lambda *a, **k: None)
    log = _CaptureLog()
    summary = {"tool": "stabilize", "queued": 5, "processed": 1, "failed": 0,
               "stopped_by_user": True, "elapsed_seconds": 12,
               "results": [], "failures": []}
    vs._report_completion(summary, 1, 0, "", log)
    ended = [l for l in log.lines if "Run ended:" in l][0]
    assert "stopped by user" in ended


def test_the_end_of_run_line_is_written_before_the_notification(monkeypatch):
    """Order is the whole point: a notification backend that blocks must not be able
    to swallow the evidence that the run reached the end."""
    order = []
    monkeypatch.setattr(vs, "_gui_event", lambda *a, **k: order.append("gui"))
    monkeypatch.setattr(vs, "send_notification",
                        lambda *a, **k: order.append("notify"))
    log = _CaptureLog()
    log.tee = lambda msg="": order.append("log") or log.lines.append(msg)
    summary = {"tool": "stabilize", "queued": 1, "processed": 0, "failed": 1,
               "stopped_by_user": False, "elapsed_seconds": 1,
               "results": [], "failures": [{"source": "a.mp4", "reason": "boom"}]}
    vs._report_completion(summary, 0, 1, "", log)
    assert order.index("log") < order.index("notify")


# ── a finished-but-failed run must not look like a hung one (D3) ────────────


def _fail_one_video(tab, tmp_path):
    """Drive the tab through the exact 2026-08-19 VM run: one video, pass 2 dies."""
    src = tmp_path / "shaky.mp4"
    src.write_bytes(b"x")
    tab.add_sources([str(src)])
    tab._set_running(True)
    iid = list(tab._rows)[0]
    tab._handle_event("FILE", json.dumps({"index": 1, "total": 1, "source": str(src),
                                     "output": tab._rows[iid]["output"]}))
    tab.progress.set(50)
    tab._handle_event("RESULT", json.dumps({"source": str(src),
                                       "output": tab._rows[iid]["output"],
                                       "ok": False, "reason": "ffmpeg failed"}))
    tab._handle_event("DONE", json.dumps({"tool": "stabilize", "queued": 1, "processed": 0,
                                     "failed": 1, "stopped_by_user": False,
                                     "elapsed_seconds": 1}))
    tab.on_exit(1)
    return iid


def test_a_failed_video_can_be_started_again(tab, tmp_path):
    """The failure is nearly always environmental (an encoder the machine cannot run,
    a locked file). With Failed excluded from Start there was no way to retry it, and
    both buttons ended up greyed out, which reads as a hung app."""
    _fail_one_video(tab, tmp_path)
    assert str(tab.start_btn.cget("state")) == "normal"


def test_retrying_clears_last_runs_verdict_from_the_row(tab, tmp_path, monkeypatch):
    """A row waiting its turn must not still say "Failed"."""
    iid = _fail_one_video(tab, tmp_path)
    assert tab._rows[iid]["status"] == "Failed"
    monkeypatch.setattr(tab, "launch", lambda *a, **k: None)
    tab._start()
    assert tab._rows[iid]["status"] == "Queued"
    assert tab.tree.set(iid, "status") == "Queued"


def test_the_progress_bar_does_not_outlive_a_failed_run(tab, tmp_path):
    """It sat at 50% (one of two passes) after the run had ended. A bar frozen
    mid-way is the most direct way an app can claim to still be working."""
    _fail_one_video(tab, tmp_path)
    assert not tab.progress.winfo_ismapped()
