"""
Item 4: a deterministically-failing video job must not be retried on every run forever.

A job that fails for a permanent reason (black-output guard, corrupt source, an
unreadable codec) used to stay `failed` in the durable queue and be re-attempted at the
top of every subsequent run (re-probe, re-split, sometimes pod time), burning work on an
installment-paid batch. Now each failure bumps a fail_count; after GIVE_UP_AFTER
consecutive failures the job is marked `skipped` (leaves get_video_queue) with the reason
prefixed. The GUI Retry zeroes the counter and re-queues it.

Pure decision (_failure_disposition), DB escalation (bump/reset), and the run_queue
give-up path (process_job monkeypatched to raise, so no ffmpeg/pod needed).
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


# ── _failure_disposition (pure) ──────────────────────────────────────────────

def test_disposition_below_threshold_stays_failed():
    status, reason = bv._failure_disposition(1, "black output", give_up_after=3)
    assert status == "failed" and reason == "black output"
    status, reason = bv._failure_disposition(2, "black output", give_up_after=3)
    assert status == "failed"


def test_disposition_at_threshold_gives_up():
    status, reason = bv._failure_disposition(3, "black output", give_up_after=3)
    assert status == "skipped"
    assert reason == "gave up after 3 attempts: black output"


def test_disposition_default_threshold_is_three():
    assert bv._failure_disposition(bv.GIVE_UP_AFTER, "x")[0] == "skipped"
    assert bv._failure_disposition(bv.GIVE_UP_AFTER - 1, "x")[0] == "failed"


# ── bump / reset (DB) ────────────────────────────────────────────────────────

def test_bump_increments_and_returns(db_conn, tmp_path):
    root = db.get_video_root_id(db_conn, str(tmp_path / "s"), str(tmp_path / "o"))
    db.upsert_video_output(db_conn, root, "v.avi", "1080p", status="queued")
    assert db.bump_video_fail_count(db_conn, root, "v.avi", "1080p") == 1
    assert db.bump_video_fail_count(db_conn, root, "v.avi", "1080p") == 2
    assert db.get_video_output(db_conn, root, "v.avi", "1080p")["fail_count"] == 2


def test_reset_zeroes_the_counter(db_conn, tmp_path):
    root = db.get_video_root_id(db_conn, str(tmp_path / "s"), str(tmp_path / "o"))
    db.upsert_video_output(db_conn, root, "v.avi", "1080p", status="failed")
    db.bump_video_fail_count(db_conn, root, "v.avi", "1080p")
    db.reset_video_fail_count(db_conn, root, "v.avi", "1080p")
    assert db.get_video_output(db_conn, root, "v.avi", "1080p")["fail_count"] == 0


def test_skipped_job_leaves_the_queue(db_conn, tmp_path):
    root = db.get_video_root_id(db_conn, str(tmp_path / "s"), str(tmp_path / "o"))
    db.upsert_video_output(db_conn, root, "v.avi", "1080p", status="skipped")
    assert db.get_video_queue(db_conn, root) == []


# ── run_queue give-up path (process_job raises; no ffmpeg/pod) ────────────────

def test_giveup_removes_job_after_three_failed_runs(db_conn, tmp_path, monkeypatch):
    src_root = str(tmp_path / "src")
    out_root = str(tmp_path / "out")
    os.makedirs(src_root, exist_ok=True)
    root = db.get_video_root_id(db_conn, src_root, out_root)
    db.upsert_video_output(db_conn, root, "bad.avi", "1080p",
                           status="queued", queue_order=0)

    monkeypatch.setattr(bv, "process_job",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("black output")))

    vcfg = bv.resolve_video_cfg({})
    vcfg["work_root"] = str(tmp_path / "work")     # a sibling of src: no conflict

    # run_queue dedupes within a run, so each call is one attempt (= one "run").
    for attempt in (1, 2):
        summary = bv.run_queue(None, db_conn, root, src_root, vcfg, bv.RunBudget(0, 0.0))
        assert summary["failed"] == 1
        job = db.get_video_output(db_conn, root, "bad.avi", "1080p")
        assert job["status"] == "failed" and job["fail_count"] == attempt
        assert len(db.get_video_queue(db_conn, root)) == 1   # still retried next run

    # third failure: give up -> skipped, drops out of the queue
    bv.run_queue(None, db_conn, root, src_root, vcfg, bv.RunBudget(0, 0.0))
    job = db.get_video_output(db_conn, root, "bad.avi", "1080p")
    assert job["status"] == "skipped" and job["fail_count"] == 3
    assert job["skip_reason"].startswith("gave up after 3 attempts:")
    assert "black output" in job["skip_reason"]
    assert db.get_video_queue(db_conn, root) == []           # no longer re-attempted

    # Retry: reset + re-queue puts it back with a fresh counter
    db.reset_video_fail_count(db_conn, root, "bad.avi", "1080p")
    db.upsert_video_output(db_conn, root, "bad.avi", "1080p", status="queued")
    job = db.get_video_output(db_conn, root, "bad.avi", "1080p")
    assert job["fail_count"] == 0
    assert len(db.get_video_queue(db_conn, root)) == 1
