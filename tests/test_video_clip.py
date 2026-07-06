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
    work_root = bv._work_dirs(out_path)[2]
    assert not os.path.exists(work_root)


@needs_ffmpeg
def test_whole_file_and_clip_coexist_in_one_queue(db_conn, tmp_path):
    src_root = str(tmp_path / "src")
    out_root = str(tmp_path / "out")
    _make_source(os.path.join(src_root, "v.avi"), seconds=12)
    root = db.get_video_root_id(db_conn, src_root, out_root)
    vcfg = bv.resolve_video_cfg({})

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
