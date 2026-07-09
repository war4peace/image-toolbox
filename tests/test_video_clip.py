"""
Segment extractor (section 16) — the clip output naming, prepare_clip enqueue, and
a full extract -> split -> passthrough-upscale -> reassemble round trip.

The pure naming test always runs. The prepare/round-trip tests need a real ffmpeg
(they synthesise a small source clip with lavfi and exercise extract_clip +
PassthroughVideoEngine), so they skip cleanly where ffmpeg is absent. A fresh
cache.db under tmp_path isolates every test from the real machine.
"""

import os
import subprocess

import pytest

import db
import video_pipeline as vp
import batch_video_upscale as bv


# ── fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def db_conn(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_DIR", str(tmp_path / "db"))
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "db" / "cache.db"))
    monkeypatch.setattr(db, "_conn", None)
    monkeypatch.setattr(db, "import_legacy_json", lambda conn: None)
    conn = db.get_conn()
    yield conn
    conn.close()
    monkeypatch.setattr(db, "_conn", None)


def _have_ffmpeg():
    try:
        vp.find_ffmpeg()
        return True
    except Exception:
        return False


needs_ffmpeg = pytest.mark.skipif(not _have_ffmpeg(), reason="ffmpeg not available")


def _make_source(path, seconds=20, fps=30, size="320x240"):
    """A small old-camera-style AVI (mjpeg video + a sine tone), via lavfi."""
    ffmpeg, _ = vp.find_ffmpeg()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    subprocess.run(
        [ffmpeg, "-hide_banner", "-y",
         "-f", "lavfi", "-i", f"testsrc=size={size}:rate={fps}:duration={seconds}",
         "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
         "-c:v", "mjpeg", "-q:v", "5", "-c:a", "pcm_s16le", "-shortest", path],
        check=True, capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0)


# ── output naming (pure) ─────────────────────────────────────────────────────

def test_output_path_whole_file_unchanged():
    p = bv._output_path("/out", "dir/movie.mp4", "4K")
    assert p == os.path.join("/out", "dir", "movie_4K.mp4")


def test_output_path_clip_uses_label():
    p = bv._output_path("/out", "birthday.avi", "1080p", clip_id=1,
                        clip_label="Cake time!", clip_start=151.0, clip_end=312.3)
    assert os.path.basename(p) == "birthday_Cake-time_1080p.mp4"


def test_output_path_clip_falls_back_to_timecode():
    p = bv._output_path("/out", "birthday.avi", "1080p", clip_id=2,
                        clip_label="", clip_start=151.0, clip_end=312.3)
    assert os.path.basename(p) == "birthday_02m31s-05m12s_1080p.mp4"


def test_output_path_clip_id_uniquifies_when_no_label_or_range():
    p = bv._output_path("/out", "v.mp4", "4K", clip_id=7, clip_label=None)
    assert os.path.basename(p) == "v_clip7_4K.mp4"


# ── staging work area (local by default) ─────────────────────────────────────

def test_work_root_defaults_to_local_app_dir():
    # An empty video.work_root resolves to a folder on the APP drive, NOT beside the
    # (possibly network) output — so a mid-run share drop can't strand segments.
    vcfg = bv.resolve_video_cfg({})
    assert vcfg["work_root"] == bv._default_work_base()
    assert vcfg["work_root"].startswith(bv.APP_ROOT)


def test_work_root_honours_explicit_setting():
    vcfg = bv.resolve_video_cfg({"video": {"work_root": r"D:\scratch"}})
    assert vcfg["work_root"] == r"D:\scratch"


def test_work_dirs_local_are_under_the_base_and_keyed_by_output():
    base = os.path.join("Z:", "scratch")
    a_in, a_up, a_root = bv._work_dirs("/out/a/movie.mp4", base)
    b_in, b_up, b_root = bv._work_dirs("/out/b/movie.mp4", base)
    # both live under the LOCAL base (not beside their outputs)...
    assert a_root.startswith(base) and b_root.startswith(base)
    assert os.path.dirname(a_root) == base
    # ...and two sources sharing a basename get distinct, stable work dirs.
    assert a_root != b_root
    assert a_in == os.path.join(a_root, "in") and a_up == os.path.join(a_root, "up")
    assert bv._work_dirs("/out/a/movie.mp4", base)[2] == a_root   # deterministic


def test_work_dirs_falsy_base_keeps_legacy_beside_output():
    in_dir, up_dir, root = bv._work_dirs("/out/a/movie.mp4", "")
    assert root == os.path.join("/out/a", bv.WORK_DIRNAME, "movie")


# ── walk excludes the output tree (don't rescan finished upscales as sources) ────────

def _touch(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "wb").close()


def test_iter_videos_skips_the_output_subtree(tmp_path):
    src = str(tmp_path / "Poze")
    _touch(os.path.join(src, "!A New Life", "clip.avi"))                 # a real source
    out = os.path.join(src, "__upscaled__")                             # default: inside src
    _touch(os.path.join(out, "!A New Life", "clip_1080p.mp4"))          # a finished upscale
    _touch(os.path.join(src, bv.WORK_DIRNAME, "x_ab12", "in", "seg_00000.mkv"))

    # Without the skip, the walk re-reads the upscale as a source (the reported bug).
    rels_unguarded = {rel for _ap, rel in bv.iter_videos(src)}
    assert any(r.startswith("__upscaled__") for r in rels_unguarded)

    # With the output root pruned, only the true source remains (work dir always pruned).
    rels = {rel for _ap, rel in bv.iter_videos(src, skip_roots=[out])}
    assert rels == {os.path.join("!A New Life", "clip.avi")}


def test_walk_videos_forwards_skip_roots(tmp_path):
    src = str(tmp_path / "src")
    _touch(os.path.join(src, "a.mp4"))
    out = os.path.join(src, "__upscaled__")
    _touch(os.path.join(out, "a_4K.mp4"))
    rels = {rel for _ap, rel in bv.walk_videos(src, skip_roots=[out])}
    assert rels == {"a.mp4"}


# ── staging-folder guards (don't stage inside the scanned tree / warn on network) ────

def test_work_root_conflict_empty_is_safe():
    assert bv.work_root_conflict("", os.path.join("S", "videos")) is None


def test_work_root_conflict_inside_source_is_rejected():
    src = os.path.abspath(os.path.join("S", "videos"))
    inside = os.path.join(src, "__upscaled__", "staging")     # under the output subfolder
    reason = bv.work_root_conflict(inside, src)
    assert reason and "inside the source" in reason


def test_work_root_conflict_above_source_is_rejected():
    src = os.path.abspath(os.path.join("S", "videos", "trip"))
    above = os.path.abspath(os.path.join("S", "videos"))      # source nested under work
    reason = bv.work_root_conflict(above, src)
    assert reason and "contains the source" in reason


def test_work_root_conflict_sibling_is_fine():
    src = os.path.abspath(os.path.join("S", "videos"))
    sibling = os.path.abspath(os.path.join("S", "scratch"))
    assert bv.work_root_conflict(sibling, src) is None


def test_is_network_path_flags_unc_and_ignores_local(tmp_path):
    assert bv.is_network_path("") is False
    assert bv.is_network_path(str(tmp_path)) is False         # a real local dir
    if os.name == "nt":
        assert bv.is_network_path(r"\\nas\media\clips") is True


def test_run_queue_refuses_staging_inside_source(db_conn, tmp_path):
    # A work_root inside the scanned source must fail the run up-front (no pod rented),
    # not silently stage segments the scanner would then re-read.
    src_root = str(tmp_path / "src")
    out_root = str(tmp_path / "out")
    os.makedirs(src_root, exist_ok=True)
    root = db.get_video_root_id(db_conn, src_root, out_root)
    vcfg = bv.resolve_video_cfg({})
    vcfg["work_root"] = os.path.join(src_root, "staging")     # inside the scanned tree

    summary = bv.run_queue(None, db_conn, root, src_root, vcfg, bv.RunBudget(0, 0.0))
    assert summary["done"] == 0 and summary["total"] == 0
    assert summary["stopped"] and "inside the source" in summary["stopped"]


# ── prepare_clip ─────────────────────────────────────────────────────────────

@needs_ffmpeg
def test_prepare_clip_enqueues_a_virtual_job(db_conn, tmp_path):
    src_root = str(tmp_path / "src")
    out_root = str(tmp_path / "out")
    _make_source(os.path.join(src_root, "birthday.avi"), seconds=20)
    root = db.get_video_root_id(db_conn, src_root, out_root)
    vcfg = bv.resolve_video_cfg({})

    info = bv.prepare_clip(db_conn, root, src_root, out_root, "birthday.avi",
                           "1080p", 5.0, 12.0, "cake", vcfg)
    assert info["clip_id"] == 1
    assert info["nb_frames"] == pytest.approx(7 * 30, abs=3)   # ~7 s at 30 fps

    clips = db.get_video_clips(db_conn, root)
    assert len(clips) == 1
    c = clips[0]
    assert c["clip_id"] == 1 and c["clip_label"] == "cake"
    assert c["clip_start"] == 5.0 and c["clip_end"] == 12.0
    assert c["output_path"].endswith(os.path.join("out", "birthday_cake_1080p.mp4"))
    # the whole-file queue helpers ignore the clip
    assert db.get_video_outputs_for(db_conn, root, "birthday.avi") == []
    # a second prepare makes a SECOND clip, not a duplicate
    info2 = bv.prepare_clip(db_conn, root, src_root, out_root, "birthday.avi",
                            "1080p", 1.0, 3.0, "intro", vcfg)
    assert info2["clip_id"] == 2
    assert len(db.get_video_clips(db_conn, root)) == 2


# ── keyframe_times (picker navigation) ───────────────────────────────────────

@needs_ffmpeg
def test_keyframe_times_are_sorted_and_start_at_zero(tmp_path):
    src = os.path.join(str(tmp_path), "kf.avi")
    _make_source(src, seconds=6)
    times = vp.keyframe_times(src)
    assert times, "expected at least one keyframe"
    assert times == sorted(times)
    assert times[0] == pytest.approx(0.0, abs=0.05)     # frame 0 is always a keyframe
    # max_keyframe_gap must agree with the list it now derives from.
    if len(times) >= 2:
        assert vp.max_keyframe_gap(src) == pytest.approx(
            max(b - a for a, b in zip(times, times[1:])))


# ── full round trip (extract -> split -> passthrough -> reassemble) ──────────

@needs_ffmpeg
def test_clip_round_trip_passthrough(db_conn, tmp_path):
    src_root = str(tmp_path / "src")
    out_root = str(tmp_path / "out")
    src = os.path.join(src_root, "birthday.avi")
    _make_source(src, seconds=20)
    before = os.stat(src)

    root = db.get_video_root_id(db_conn, src_root, out_root)
    vcfg = bv.resolve_video_cfg({})
    vcfg["work_root"] = str(tmp_path / "work")     # stage under tmp, not the app drive
    bv.prepare_clip(db_conn, root, src_root, out_root, "birthday.avi",
                    "1080p", 5.0, 12.0, "cake", vcfg)

    engine = bv.PassthroughVideoEngine()
    budget = bv.RunBudget(0, 0.0)
    summary = bv.run_queue(engine, db_conn, root, src_root, vcfg, budget)
    assert summary == {"done": 1, "failed": 0, "stopped": None, "total": 1}

    out_path = os.path.join(out_root, "birthday_cake_1080p.mp4")
    assert os.path.exists(out_path)
    out = vp.probe(out_path, count=True)
    assert out.nb_frames == pytest.approx(7 * 30, abs=4)   # the clipped range only
    assert 6.5 <= (out.duration or 0) <= 7.6

    # the clip job is marked done and the source is untouched
    job = db.get_video_output(db_conn, root, "birthday.avi", "1080p", clip_id=1)
    assert job["status"] == "done"
    after = os.stat(src)
    assert (before.st_size, before.st_mtime) == (after.st_size, after.st_mtime)
    # the work area was cleaned up on completion (no stray clip/segment files)
    work_root = bv._work_dirs(out_path, vcfg["work_root"])[2]
    assert not os.path.exists(work_root)


@needs_ffmpeg
def test_whole_file_and_clip_coexist_in_one_queue(db_conn, tmp_path):
    src_root = str(tmp_path / "src")
    out_root = str(tmp_path / "out")
    _make_source(os.path.join(src_root, "v.avi"), seconds=12)
    root = db.get_video_root_id(db_conn, src_root, out_root)
    vcfg = bv.resolve_video_cfg({})
    vcfg["work_root"] = str(tmp_path / "work")     # stage under tmp, not the app drive

    bv.prepare_job(db_conn, root, src_root, out_root, "v.avi", "1080p", vcfg)
    bv.prepare_clip(db_conn, root, src_root, out_root, "v.avi", "1080p", 2.0, 6.0, "bit", vcfg)
    assert len(db.get_video_queue(db_conn, root)) == 2   # whole-file + clip

    summary = bv.run_queue(bv.PassthroughVideoEngine(), db_conn, root, src_root,
                           vcfg, bv.RunBudget(0, 0.0))
    assert summary["done"] == 2 and summary["failed"] == 0
    assert os.path.exists(os.path.join(out_root, "v_1080p.mp4"))          # whole file
    assert os.path.exists(os.path.join(out_root, "v_bit_1080p.mp4"))      # clip
    whole = vp.probe(os.path.join(out_root, "v_1080p.mp4"), count=True)
    clip = vp.probe(os.path.join(out_root, "v_bit_1080p.mp4"), count=True)
    assert whole.nb_frames > clip.nb_frames                              # clip is shorter
