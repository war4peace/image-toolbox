"""
Dynamic ratio targets + GPU feasibility guard (feature #7). Pure/GPU-free coverage of the
target resolver, the per-VRAM feasibility caps, the offered-target enumeration (ratios +
presets, deduped, filtered), and the runner's defer-if-too-big rule.
"""

import os

import pytest

import db
import video_estimate as ve
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


# ── ratio resolver ───────────────────────────────────────────────────────────

def test_ratio_target_resolves_by_scale():
    assert ve.ratio_of("2X") == 2 and ve.ratio_of("4x") == 4
    assert ve.ratio_of("1080p") is None
    assert ve.fit_scale(320, 240, "2X") == 2.0
    assert ve.output_dims(320, 240, "4X") == (1280, 960)
    assert ve.fit_short_side(320, 240, "2X") == 480          # feeds the pipeline --resolution
    assert ve.output_megapixels(320, 240, "4X") == pytest.approx(1.2288)
    assert ve.classify_upscale(320, 240, "2X") is None        # a ratio is always a healthy upscale


def test_target_label_shows_output_resolution():
    assert ve.target_label(320, 240, "2X") == "2x (640x480)"
    assert ve.target_label(320, 240, "1080p") == "1080p (1440x1080)"


# ── source-eligible enumeration + dedupe ─────────────────────────────────────

def test_source_eligible_orders_and_dedupes():
    # 320x240: ratios then presets, ascending output size.
    assert ve.source_eligible_targets(320, 240) == ["2X", "4X", "1080p", "1440p", "4K"]
    # 960x540: 2x == 1080p and 4x == 4K exactly, so the ratio duplicates drop out.
    assert ve.source_eligible_targets(960, 540) == ["1080p", "1440p", "4K"]
    # a source already at/above 4K has nothing to upscale to.
    assert ve.source_eligible_targets(3840, 2160) == []


# ── feasibility caps by VRAM ─────────────────────────────────────────────────

def test_max_output_mp_by_tier():
    assert ve.max_output_mp(24) == pytest.approx(2.1)         # 1080p (user's call)
    assert ve.max_output_mp(32) == pytest.approx(3.8)         # 1440p
    assert ve.max_output_mp(48) == pytest.approx(8.4)         # 4K
    assert ve.max_output_mp(12) < 1.3                         # small card, well below 1080p
    assert ve.max_output_mp(0) == 0.0                         # unknown -> no filter


def test_feasible_targets_match_the_examples():
    # 320x240 on a 24 GB card: 2x, 4x, 1080p (NOT 1440p/4K).
    assert ve.feasible_targets(320, 240, ve.max_output_mp(24)) == ["2X", "4X", "1080p"]
    # a 5090 (32 GB) additionally reaches 1440p.
    assert ve.feasible_targets(320, 240, ve.max_output_mp(32)) == ["2X", "4X", "1080p", "1440p"]
    # a 12 GB card only manages 2x for this source.
    assert ve.feasible_targets(320, 240, ve.max_output_mp(12)) == ["2X"]
    # unknown GPU -> no filtering (returns the full source-eligible set).
    assert ve.feasible_targets(320, 240, 0) == ve.source_eligible_targets(320, 240)


def test_target_is_feasible():
    assert ve.target_is_feasible(320, 240, "1080p", ve.max_output_mp(24)) is True
    assert ve.target_is_feasible(320, 240, "1440p", ve.max_output_mp(24)) is False
    assert ve.target_is_feasible(320, 240, "1440p", 0) is True   # unknown -> don't gray


# ── benchmark data RAISES the cap above the seed ─────────────────────────────

def test_benchmark_ok_raises_feasibility(db_conn):
    gpu = "NVIDIA GeForce RTX 3090"
    # seed cap for 24 GB is 2.1 MP; a proven-ok 1440p probe (3.69 MP) should raise it.
    assert ve.max_output_mp(24, gpu, db_conn) == pytest.approx(2.1)
    db.record_bench_probe(db_conn, gpu, "7b", 2560, 1440, 9, "ok", frames=37, seconds=200.0)
    assert ve.max_output_mp(24, gpu, db_conn) == pytest.approx(2560 * 1440 / 1e6)


# ── runner defers (does not fail) a too-big job ──────────────────────────────

def test_job_exceeds_gpu_defers(db_conn, tmp_path, monkeypatch):
    root = db.get_video_root_id(db_conn, str(tmp_path / "s"), str(tmp_path / "o"))
    db.upsert_video_file(db_conn, root, "v.avi", width=1920, height=1080,
                         duration=10.0, nb_frames=300, fps=30.0, vcodec="h264")
    job_1080 = {"rel_path": "v.avi", "target": "1080p", "clip_id": 0}
    job_4k = {"rel_path": "v.avi", "target": "4K", "clip_id": 0}
    # 24 GB card (~2.1 MP): 1080p (2.07) runs, 4K (8.29) is deferred.
    assert bv._job_exceeds_gpu(db_conn, root, job_1080, 2.1) is False
    assert bv._job_exceeds_gpu(db_conn, root, job_4k, 2.1) is True
    # no cap -> nothing deferred.
    assert bv._job_exceeds_gpu(db_conn, root, job_4k, 0) is False


def test_eligible_targets_now_includes_ratios():
    # the scan path (bv.eligible_targets) now returns ratios + presets.
    assert bv.eligible_targets(320, 240) == ["2X", "4X", "1080p", "1440p", "4K"]
