"""
Roadmap #5: video conciliation. The image conciliation pipeline is extended to match
and replace VIDEO originals with their upscaled outputs, using the content-hash lineage
the Video Upscaler records (item 10). Videos are matched by lineage ONLY (no name
fallback) so a partial clip can never be mistaken for a whole-video match. These tests
exercise the matching/plan logic and one end-to-end archive on a real DB with small temp
files (no ffmpeg, no engine): the bytes stand in for media.
"""

import os

import pytest

import db
import conciliate as cc


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


def _write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    return path


def _record_lineage(conn, src, out):
    """Link src -> out by content hash, as the Video Upscaler does on completion.
    Hashing `out` also caches its hash, which is what the processed index relies on."""
    src_hash = db.hash_file_cached(conn, src)
    out_hash = db.hash_file_cached(conn, out)
    db.record_upscale_lineage(conn, src_hash, out_hash, src, out)
    conn.commit()


class _FakeLog:
    """Minimal Logger stand-in for execute() (no on-disk log file in tests)."""
    def tee(self, msg=""): pass
    def log_only(self, msg=""): pass
    def close(self): pass


# ── build_plan: lineage matching (the ONLY video path) ───────────────────────

def test_build_plan_matches_video_by_lineage(db_conn, tmp_path):
    orig = str(tmp_path / "orig")
    proc = str(tmp_path / "proc")
    src = _write(os.path.join(orig, "holiday", "clip.avi"), b"the-source-video")
    # Output has a suffixed name AND a different container than the source.
    out = _write(os.path.join(proc, "holiday", "clip_4K.mp4"), b"the-upscaled-output")
    _record_lineage(db_conn, src, out)

    plan, folders, kept, _v = cc.build_plan(orig, proc, tr_index=None, conn=db_conn)
    assert plan == [(src, out, os.path.join("holiday", "clip.avi"))]
    assert folders == [(os.path.join("holiday"), 1, 0, 0)]
    assert kept == []


def test_build_plan_matches_video_by_lineage_after_rename(db_conn, tmp_path):
    # Content-hash lineage is path-independent: the output is renamed/moved after the
    # lineage was recorded, and it still matches (name would not).
    orig = str(tmp_path / "orig")
    proc = str(tmp_path / "proc")
    src = _write(os.path.join(orig, "clip.avi"), b"src-bytes-xyz")
    recorded_out = os.path.join(proc, "clip_4K.mp4")
    out = _write(recorded_out, b"out-bytes-xyz")
    _record_lineage(db_conn, src, out)
    # Rename the output on disk (content unchanged, so its cached hash still applies).
    moved = os.path.join(proc, "renamed_by_user.mp4")
    os.replace(recorded_out, moved)

    plan, _folders, _k, _v = cc.build_plan(orig, proc, tr_index=None, conn=db_conn)
    assert plan == [(src, moved, "clip.avi")]


def test_build_plan_video_without_lineage_is_skipped(db_conn, tmp_path):
    # No lineage -> no match for video (lineage-only; there is no name fallback).
    orig = str(tmp_path / "orig")
    proc = str(tmp_path / "proc")
    _write(os.path.join(orig, "clip.avi"), b"src")
    _write(os.path.join(proc, "clip_1440p.mp4"), b"out")   # plausible name, but no lineage
    plan, folders, _k, _v = cc.build_plan(orig, proc, tr_index=None, conn=db_conn)
    assert plan == []
    assert folders == [(".", 0, 1, 0)]      # 0 replaced, 1 no-match


def test_clip_like_output_never_matches_whole_source(db_conn, tmp_path):
    # SAFETY: a clip extract records no lineage and is named <base>_<label>_<target>.mp4,
    # sharing the source's <base>_ prefix. A name guess would replace the WHOLE movie with
    # a short clip. Lineage-only matching must leave the source untouched. Another,
    # lineaged pair is present so has_lineage is True (the realistic mixed case).
    orig = str(tmp_path / "orig")
    proc = str(tmp_path / "proc")
    lin_s = _write(os.path.join(orig, "other.avi"), b"other-src")
    lin_o = _write(os.path.join(proc, "other_4K.mp4"), b"other-out")
    _record_lineage(db_conn, lin_s, lin_o)

    _write(os.path.join(orig, "movie.avi"), b"the-whole-movie")     # no lineage
    _write(os.path.join(proc, "movie_scene1_4K.mp4"), b"just-a-clip")  # clip, no lineage

    plan, _folders, _k, _v = cc.build_plan(orig, proc, tr_index=None, conn=db_conn)
    srcs = {p[0] for p in plan}
    assert lin_s in srcs                                   # the lineaged pair matches
    assert os.path.join(orig, "movie.avi") not in srcs     # the movie is NOT matched to the clip


def test_build_plan_unmatched_video_skipped_nonmedia_kept(db_conn, tmp_path):
    orig = str(tmp_path / "orig")
    proc = str(tmp_path / "proc")
    _write(os.path.join(orig, "lonely.mkv"), b"no-output-for-me")   # no match anywhere
    note = _write(os.path.join(orig, "readme.txt"), b"keep me")     # non-media

    plan, folders, kept, _v = cc.build_plan(orig, proc, tr_index=None, conn=db_conn)
    assert plan == []
    assert folders == [(".", 0, 1, 1)]      # 0 replaced, 1 no-match, 1 non-media kept
    assert kept == [note]


def test_build_plan_images_and_videos_together(db_conn, tmp_path):
    # The unified scan pairs both media types in one pass.
    orig = str(tmp_path / "orig")
    proc = str(tmp_path / "proc")
    img_s = _write(os.path.join(orig, "photo.jpg"), b"photo-src")
    img_o = _write(os.path.join(proc, "photo.jpg"), b"photo-up")      # mirrored name
    vid_s = _write(os.path.join(orig, "clip.avi"), b"clip-src")
    vid_o = _write(os.path.join(proc, "clip_4K.mp4"), b"clip-up")
    _record_lineage(db_conn, img_s, img_o)
    _record_lineage(db_conn, vid_s, vid_o)

    plan, folders, _k, _v = cc.build_plan(orig, proc, tr_index=None, conn=db_conn)
    pairs = {p[0]: p[1] for p in plan}
    assert pairs == {img_s: img_o, vid_s: vid_o}
    assert folders == [(".", 2, 0, 0)]


# ── processed index: videos are indexed by content hash ──────────────────────

def test_processed_index_includes_videos(db_conn, tmp_path):
    # Videos are hashed into the index like images (so a moved/renamed output is
    # still matchable by content, not just when it sits at its recorded path).
    proc = str(tmp_path / "proc")
    vid = _write(os.path.join(proc, "a_4K.mp4"), b"a-video")
    img = _write(os.path.join(proc, "b.jpg"), b"an-image")
    index = cc.build_processed_hash_index(proc, db_conn)
    assert index.get(db.hash_file_cached(db_conn, vid)) == vid
    assert index.get(db.hash_file_cached(db_conn, img)) == img


# ── end-to-end execute (archive) ─────────────────────────────────────────────

def test_execute_archives_original_and_moves_video_in(db_conn, tmp_path):
    orig = str(tmp_path / "orig")
    proc = str(tmp_path / "proc")
    src = _write(os.path.join(orig, "holiday", "clip.avi"), b"src-bytes")
    out = _write(os.path.join(proc, "holiday", "clip_4K.mp4"), b"out-bytes")
    _record_lineage(db_conn, src, out)

    plan, _f, _k, _v = cc.build_plan(orig, proc, tr_index=None, conn=db_conn)
    done, conflicts, errors, restored = cc.execute(plan, orig, "archive", _FakeLog())
    assert (done, conflicts, errors) == (1, 0, 0)
    assert restored == 0            # videos never get the #13b image backfill

    # Original archived, output moved into the original tree keeping its own name.
    assert os.path.isfile(os.path.join(orig, cc.ARCHIVE_DIRNAME, "holiday", "clip.avi"))
    assert os.path.isfile(os.path.join(orig, "holiday", "clip_4K.mp4"))
    assert not os.path.isfile(src)      # moved out of its original spot
    assert not os.path.isfile(out)      # moved into the original tree
