"""
Item 10: record source <-> output lineage when a whole-video job completes, so a future
video conciliation (roadmap #5) can re-match the pair by content hash after a move/rename
(the image runners already do this; the video runner recorded nothing). The pure helper is
tested with a real DB + temp files (no ffmpeg); the caller's whole-video-only + config-gate
behaviour is tested end to end where ffmpeg is available.
"""

import os
import subprocess

import pytest

import db
import video_pipeline as vp
import batch_video_upscale as bv


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


def _make_source(path, seconds=6, fps=30, size="320x240"):
    ffmpeg, _ = vp.find_ffmpeg()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    subprocess.run(
        [ffmpeg, "-hide_banner", "-y",
         "-f", "lavfi", "-i", f"testsrc=size={size}:rate={fps}:duration={seconds}",
         "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
         "-c:v", "mjpeg", "-q:v", "5", "-c:a", "pcm_s16le", "-shortest", path],
        check=True, capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0)


# ── pure helper ──────────────────────────────────────────────────────────────

def test_records_source_to_output_link(db_conn, tmp_path):
    src = tmp_path / "src.mkv"; src.write_bytes(b"a-source-video-bytes")
    out = tmp_path / "out.mp4"; out.write_bytes(b"the-upscaled-output-bytes")
    bv._record_video_lineage(db_conn, str(src), str(out))
    src_hash = db.hash_file_cached(db_conn, str(src))
    out_hash = db.hash_file_cached(db_conn, str(out))
    assert db.lineage_final_hash(db_conn, src_hash) == out_hash


def test_skips_when_source_missing(db_conn, tmp_path):
    out = tmp_path / "out.mp4"; out.write_bytes(b"out")
    bv._record_video_lineage(db_conn, str(tmp_path / "gone.mkv"), str(out))
    assert not db.lineage_has_rows(db_conn)


def test_fail_safe_on_bad_inputs(db_conn):
    # None / empty paths never raise and never write a row.
    bv._record_video_lineage(db_conn, "", "")
    bv._record_video_lineage(db_conn, None, None)
    assert not db.lineage_has_rows(db_conn)


# ── end-to-end (ffmpeg): whole-video-only + config gate ──────────────────────

@needs_ffmpeg
def test_whole_video_run_records_lineage(db_conn, tmp_path):
    src_root = str(tmp_path / "src")
    out_root = str(tmp_path / "out")
    _make_source(os.path.join(src_root, "clip.avi"))
    root = db.get_video_root_id(db_conn, src_root, out_root)
    vcfg = bv.resolve_video_cfg({})
    vcfg["work_root"] = str(tmp_path / "work")
    assert vcfg["record_lineage"] is True

    bv.prepare_job(db_conn, root, src_root, out_root, "clip.avi", "1080p", vcfg)
    summary = bv.run_queue(bv.PassthroughVideoEngine(), db_conn, root, src_root,
                           vcfg, bv.RunBudget(0, 0.0))
    assert summary["done"] == 1

    src_hash = db.hash_file_cached(db_conn, os.path.join(src_root, "clip.avi"))
    assert db.lineage_final_hash(db_conn, src_hash) is not None


@needs_ffmpeg
def test_record_lineage_disabled_writes_nothing(db_conn, tmp_path):
    src_root = str(tmp_path / "src")
    out_root = str(tmp_path / "out")
    _make_source(os.path.join(src_root, "clip.avi"))
    root = db.get_video_root_id(db_conn, src_root, out_root)
    vcfg = bv.resolve_video_cfg({})
    vcfg["work_root"] = str(tmp_path / "work")
    vcfg["record_lineage"] = False

    bv.prepare_job(db_conn, root, src_root, out_root, "clip.avi", "1080p", vcfg)
    bv.run_queue(bv.PassthroughVideoEngine(), db_conn, root, src_root,
                 vcfg, bv.RunBudget(0, 0.0))
    assert not db.lineage_has_rows(db_conn)
