"""
Item 5: staging work dirs must not leak on the app drive.

A job's staging dir (split segments, gigabytes for a 4K video) was cleaned only on
success. It leaked forever when a job was removed from the queue, gave up (item 4), or
its output path changed so the hash-keyed dir was never revisited. Now: an immediate
removal when a job leaves the queue for good, and a run-start sweep that reclaims any
staging dir under the base that no active job owns. The base is shared across source
roots, so the sweep's keep-set is every active job's output path in the whole DB.
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


def _make_staging(out_video, base):
    """Create the staging dir _work_dirs maps out_video to, with a dummy segment."""
    root = bv._work_dirs(out_video, base)[2]
    in_dir = os.path.join(root, "in")
    os.makedirs(in_dir, exist_ok=True)
    with open(os.path.join(in_dir, "seg_00000.mkv"), "wb") as f:
        f.write(b"x" * 1000)
    return root


# ── _remove_job_staging ──────────────────────────────────────────────────────

def test_remove_job_staging_deletes_the_dir(tmp_path):
    base = str(tmp_path / "stage")
    out = os.path.join("O", "movie_4K.mp4")
    root = _make_staging(out, base)
    assert os.path.isdir(root)
    bv._remove_job_staging(out, base)
    assert not os.path.exists(root)


def test_remove_job_staging_noops_on_empty(tmp_path):
    base = str(tmp_path / "stage")
    os.makedirs(base)
    bv._remove_job_staging("", base)          # no output_path
    bv._remove_job_staging(os.path.join("O", "x.mp4"), "")   # no base
    assert os.path.isdir(base)                # nothing blew up, base untouched


# ── _sweep_orphan_staging (DB-backed keep-set) ───────────────────────────────

def test_sweep_keeps_active_deletes_orphans(db_conn, tmp_path):
    base = str(tmp_path / "stage")
    root = db.get_video_root_id(db_conn, str(tmp_path / "src"), str(tmp_path / "out"))
    active_out = os.path.join(str(tmp_path / "out"), "keep_4K.mp4")
    db.upsert_video_output(db_conn, root, "keep.avi", "4K",
                           status="partial", output_path=active_out)
    active_dir = _make_staging(active_out, base)             # owned by an active job
    orphan_dir = _make_staging(os.path.join("O", "gone_1080p.mp4"), base)  # no job owns it

    removed, freed = bv._sweep_orphan_staging(db_conn, base)
    assert removed == 1
    assert freed >= 1000
    assert os.path.isdir(active_dir)          # active job's dir kept
    assert not os.path.exists(orphan_dir)     # orphan reclaimed


def test_sweep_ignores_stray_files_and_missing_base(db_conn, tmp_path):
    # a missing base is a clean no-op
    assert bv._sweep_orphan_staging(db_conn, str(tmp_path / "nope")) == (0, 0)
    base = str(tmp_path / "stage")
    os.makedirs(base)
    with open(os.path.join(base, "a-stray-file.txt"), "wb") as f:
        f.write(b"leave me")
    removed, _freed = bv._sweep_orphan_staging(db_conn, base)
    assert removed == 0
    assert os.path.exists(os.path.join(base, "a-stray-file.txt"))   # files are left alone


def test_sweep_does_not_touch_another_roots_active_dirs(db_conn, tmp_path):
    # Two roots share the base; a sweep must key off EVERY root's active jobs.
    base = str(tmp_path / "stage")
    r1 = db.get_video_root_id(db_conn, str(tmp_path / "s1"), str(tmp_path / "o1"))
    r2 = db.get_video_root_id(db_conn, str(tmp_path / "s2"), str(tmp_path / "o2"))
    out1 = os.path.join(str(tmp_path / "o1"), "a_4K.mp4")
    out2 = os.path.join(str(tmp_path / "o2"), "b_4K.mp4")
    db.upsert_video_output(db_conn, r1, "a.avi", "4K", status="queued", output_path=out1)
    db.upsert_video_output(db_conn, r2, "b.avi", "4K", status="partial", output_path=out2)
    d1 = _make_staging(out1, base)
    d2 = _make_staging(out2, base)

    removed, _freed = bv._sweep_orphan_staging(db_conn, base)
    assert removed == 0
    assert os.path.isdir(d1) and os.path.isdir(d2)   # neither root's dir is touched


# ── give-up removes staging (run_queue) ──────────────────────────────────────

def test_giveup_removes_staging(db_conn, tmp_path, monkeypatch):
    src_root = str(tmp_path / "src")
    out_root = str(tmp_path / "out")
    os.makedirs(src_root, exist_ok=True)
    base = str(tmp_path / "work")
    root = db.get_video_root_id(db_conn, src_root, out_root)
    out_video = os.path.join(out_root, "bad_1080p.mp4")
    db.upsert_video_output(db_conn, root, "bad.avi", "1080p",
                           status="queued", output_path=out_video, queue_order=0)
    stage = _make_staging(out_video, base)

    # already failed twice; the next failure is the give-up
    db.bump_video_fail_count(db_conn, root, "bad.avi", "1080p")
    db.bump_video_fail_count(db_conn, root, "bad.avi", "1080p")
    monkeypatch.setattr(bv, "process_job",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("black output")))

    vcfg = bv.resolve_video_cfg({})
    vcfg["work_root"] = base
    bv.run_queue(None, db_conn, root, src_root, vcfg, bv.RunBudget(0, 0.0))

    job = db.get_video_output(db_conn, root, "bad.avi", "1080p")
    assert job["status"] == "skipped"
    assert not os.path.exists(stage)          # gave up -> staging reclaimed
