"""
Cross-install consistency: the Video Upscaler scan must reflect the DESTINATION folder, not
just the local db/cache.db. A second install (e.g. a laptop upscaling via a remote pod while
the desktop GPU is busy) that shares the same source + destination had an empty DB and showed
videos as un-upscaled even though another install had already produced the outputs on disk.

reconcile_outputs_from_disk adopts on-disk outputs missing from THIS DB as 'done' jobs, the
mirror of reconcile_video_outputs (which drops DB rows for outputs deleted off disk). These
tests use a real DB + real files (no ffmpeg/engine); bytes stand in for the .mp4 outputs.
"""

import os

import pytest

import db
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


def _write(path, data=b"x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    return path


def test_adopts_existing_outputs_missing_from_db(db_conn, tmp_path):
    # Mirrors the reported case: a subfolder source upscaled to 2X and 4X by ANOTHER install.
    src_root = str(tmp_path / "Poze")
    out_root = str(tmp_path / "Poze" / "__upscaled__")
    rel = os.path.join("!Blackberry", "VID 00003-20110601-2110.3GP")
    _write(os.path.join(out_root, "!Blackberry", "VID 00003-20110601-2110_2X.mp4"))
    _write(os.path.join(out_root, "!Blackberry", "VID 00003-20110601-2110_4X.mp4"))
    root_id = db.get_video_root_id(db_conn, src_root, out_root)

    adopted = bv.reconcile_outputs_from_disk(db_conn, root_id, out_root, rel)
    assert sorted(t for _r, t in adopted) == ["2X", "4X"]

    done = {o["target"]: o for o in db.get_video_outputs_for(db_conn, root_id, rel)}
    assert set(done) == {"2X", "4X"}
    assert all(done[t]["status"] == "done" for t in done)
    assert done["2X"]["output_path"] == bv._output_path(out_root, rel, "2X")


def test_no_outputs_on_disk_adopts_nothing(db_conn, tmp_path):
    src_root = str(tmp_path / "src")
    out_root = str(tmp_path / "out")
    root_id = db.get_video_root_id(db_conn, src_root, out_root)
    assert bv.reconcile_outputs_from_disk(db_conn, root_id, out_root, "clip.avi") == []
    assert db.get_video_outputs_for(db_conn, root_id, "clip.avi") == []


def test_does_not_touch_existing_db_rows(db_conn, tmp_path):
    # A target already known to this DB (e.g. queued or in-progress) must be left as-is, even
    # if its output file happens to exist: adoption only fills GAPS, never overwrites state.
    src_root = str(tmp_path / "src")
    out_root = str(tmp_path / "out")
    root_id = db.get_video_root_id(db_conn, src_root, out_root)
    rel = "clip.avi"
    db.upsert_video_output(db_conn, root_id, rel, "2X", status="queued")
    _write(bv._output_path(out_root, rel, "2X"))        # a file exists for the queued target
    _write(bv._output_path(out_root, rel, "4X"))        # and for a brand-new one

    adopted = bv.reconcile_outputs_from_disk(db_conn, root_id, out_root, rel)
    assert adopted == [(rel, "4X")]                     # only the gap is adopted
    rows = {o["target"]: o["status"] for o in db.get_video_outputs_for(db_conn, root_id, rel)}
    assert rows == {"2X": "queued", "4X": "done"}       # the queued row untouched


def test_ignores_clip_outputs(db_conn, tmp_path):
    # A clip output (<base>_<label>_<target>.mp4) shares the <base>_ prefix but is NOT a
    # whole-video output; it must never be adopted as one (it would mark a partial as the whole).
    src_root = str(tmp_path / "src")
    out_root = str(tmp_path / "out")
    root_id = db.get_video_root_id(db_conn, src_root, out_root)
    rel = "movie.avi"
    _write(os.path.join(out_root, "movie_scene1_2X.mp4"))   # a clip, not movie_2X.mp4
    assert bv.reconcile_outputs_from_disk(db_conn, root_id, out_root, rel) == []
    assert db.get_video_outputs_for(db_conn, root_id, rel) == []


def test_idempotent_second_scan_adopts_nothing_new(db_conn, tmp_path):
    src_root = str(tmp_path / "src")
    out_root = str(tmp_path / "out")
    root_id = db.get_video_root_id(db_conn, src_root, out_root)
    rel = "clip.avi"
    _write(bv._output_path(out_root, rel, "4X"))
    first = bv.reconcile_outputs_from_disk(db_conn, root_id, out_root, rel)
    second = bv.reconcile_outputs_from_disk(db_conn, root_id, out_root, rel)
    assert first == [(rel, "4X")]
    assert second == []                                 # already adopted -> no duplicate
