"""
Feature #7 (local video upscaling) runner wiring.

Covers the pieces batch_video_upscale gained for the LOCAL path, none of which need a
GPU / ffmpeg / pod:

  * resolve_video_cfg exposes the local knobs (thrash_stall_seconds, local_use_subprocess).
  * _local_seedvr2_paths resolves the vendored seedvr2 repo + SEEDVR2 weights off the app
    root (same keys the Batch Upscaler reads).
  * run_queue treats a ThrashDetected (the local thrash watchdog) as a DEGRADATION episode:
    it STOPS the run (does not roll on to the next job), leaves the job `partial` (so its
    finished segments resume next run), and does NOT bump the source's fail_count.
  * _stop_notice maps a "gpu thrash" stop to a red, resume-hinted notice.
"""

import os

import pytest

import db
import batch_video_upscale as bv
import video_estimate as ve


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


# ── config resolution ────────────────────────────────────────────────────────

def test_resolve_video_cfg_local_defaults():
    v = bv.resolve_video_cfg({})
    assert v["thrash_stall_seconds"] == 300
    assert v["local_use_subprocess"] is True


def test_resolve_video_cfg_local_overrides():
    v = bv.resolve_video_cfg({"video": {"thrash_stall_seconds": 120,
                                        "local_use_subprocess": False}})
    assert v["thrash_stall_seconds"] == 120
    assert v["local_use_subprocess"] is False


# ── seedvr2 path resolution ──────────────────────────────────────────────────

def test_local_seedvr2_paths_defaults_to_app_root():
    repo, model = bv._local_seedvr2_paths({})
    assert repo == os.path.join(bv.APP_ROOT, "seedvr2")
    assert model == os.path.join(bv.APP_ROOT, "models", "SEEDVR2")


def test_local_seedvr2_paths_honours_config_and_expands_env(monkeypatch):
    monkeypatch.setenv("MYWEIGHTS", "W")
    repo, model = bv._local_seedvr2_paths(
        {"seedvr2": {"repo_dir": r"C:\seed\repo",
                     "model_dir": r"%MYWEIGHTS%\SEEDVR2"}})
    assert repo == r"C:\seed\repo"                       # absolute: used as-is
    # relative (post-expansion) is anchored at the app root
    assert model == os.path.normpath(os.path.join(bv.APP_ROOT, "W", "SEEDVR2"))


# ── _stop_notice: the local GPU-thrash stop ──────────────────────────────────

def test_stop_notice_gpu_thrash_is_red_and_resumable():
    title, color, resume = bv._stop_notice("gpu thrash")
    assert "thrash" in title.lower()
    assert color == 0xE74C3C                             # red (a degradation, not a plain pause)
    assert resume is True                                # the queue resumes after a reboot


# ── run_queue: ThrashDetected stops the run without blaming the source ────────

def test_thrash_stops_run_leaves_partial_no_failcount(db_conn, tmp_path, monkeypatch):
    src_root = str(tmp_path / "src")
    out_root = str(tmp_path / "out")
    os.makedirs(src_root, exist_ok=True)
    root = db.get_video_root_id(db_conn, src_root, out_root)
    # Two queued jobs: the FIRST thrashes; the second must NOT be attempted (a degraded
    # GPU would just thrash again, so the run stops after the episode).
    db.upsert_video_output(db_conn, root, "a.avi", "1080p", status="queued", queue_order=0)
    db.upsert_video_output(db_conn, root, "b.avi", "1080p", status="queued", queue_order=1)

    seen = []

    def fake_process_job(engine, conn, root_id, source_root, job, *a, **k):
        seen.append(job["rel_path"])
        raise bv._ThrashDetected("no GPU progress for 300s at batch 17")

    monkeypatch.setattr(bv, "process_job", fake_process_job)

    vcfg = bv.resolve_video_cfg({})
    vcfg["work_root"] = str(tmp_path / "work")

    summary = bv.run_queue(None, db_conn, root, src_root, vcfg, bv.RunBudget(0, 0.0))

    assert seen == ["a.avi"]                              # stopped after the thrash, b untouched
    assert summary["stopped"] == "gpu thrash"
    assert summary["failed"] == 0                         # NOT a source failure
    a = db.get_video_output(db_conn, root, "a.avi", "1080p")
    assert a["status"] == "partial"                      # resumes next run
    assert a["fail_count"] == 0                           # source never blamed
    b = db.get_video_output(db_conn, root, "b.avi", "1080p")
    assert b["status"] == "queued"                       # never attempted


# ── local TIME estimate (video_estimate) ─────────────────────────────────────

def _job(frames=100, target="1080p", w=1920, h=1080, segs=1):
    return {"frames": frames, "target": target, "segments": segs, "width": w, "height": h}


def test_local_estimate_none_without_history_or_seed():
    # An unseeded card with no history: honestly decline to invent a time.
    assert ve.LOCAL_RATES == {}                          # ships with no fabricated seeds
    assert ve.estimate_queue_local([_job()], "NVIDIA GeForce RTX 4070", conn=None) is None


def test_local_estimate_uses_measured_history_calibrated(db_conn):
    gpu = "NVIDIA GeForce RTX 3090"
    # 1000 output-MP over 1600 s -> 1.6 s/MP (>= the 300-MP trust floor).
    db.record_gpu_perf(db_conn, "video-mp-1080p", gpu, 1000, 1600.0, min_images=300)
    est = ve.estimate_queue_local([_job(frames=100, w=1920, h=1080)], gpu, conn=db_conn)
    assert est is not None and est["calibrated"] is True
    # 1920x1080 fits the 1080p box 1:1 -> 2.0736 MP/frame; 100 * 2.0736 * 1.6 s.
    assert est["duration_seconds"] == pytest.approx(100 * 2.0736 * 1.6, rel=1e-3)
    assert est["segments"] == 1 and est["total_frames"] == 100


def test_local_estimate_seed_is_flagged_rough(monkeypatch):
    monkeypatch.setattr(ve, "LOCAL_RATES", {"1080p": {"TESTCARD": 2.0}})
    monkeypatch.setattr(ve, "_LOCAL_MODEL_TOKENS", [("TESTCARD", "TESTCARD")])
    est = ve.estimate_queue_local([_job(frames=10, w=1920, h=1080)], "MyTestCard 24GB", conn=None)
    assert est is not None and est["calibrated"] is False   # seeded, not measured -> "(rough)"
    assert est["duration_seconds"] == pytest.approx(10 * 2.0736 * 2.0, rel=1e-3)


def test_local_estimate_none_if_any_target_unrated(db_conn):
    gpu = "NVIDIA GeForce RTX 3090"
    db.record_gpu_perf(db_conn, "video-mp-1080p", gpu, 1000, 1600.0, min_images=300)
    # 1080p is calibrated, 4K is not -> the whole-queue estimate declines (None).
    jobs = [_job(target="1080p"), _job(target="4K", w=3840, h=2160)]
    assert ve.estimate_queue_local(jobs, gpu, conn=db_conn) is None
