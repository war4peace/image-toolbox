"""
Real-ESRGAN (fixed-ratio) benchmark wiring (#18 B benchmark): the new pinned sources, the
ratio-cell catalog + plan, the esrgan tier bench key, and the ESRGAN rate store that lets a
fixed_ratio queue estimate from measured data. All pure/offline (no torch, no GPU, no network):
the live pod/engine round-trip is validated separately (a real card, the user's side).
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import benchmark_clip as bclip
import video_benchmark as vb
import video_estimate as ve
import db


# ── new pinned sources ───────────────────────────────────────────────────────

def test_esrgan_sources_present_with_exact_native_dims():
    # The three ratio-cell sources exist with a URL + a 64-hex SHA pin (verify-third-party rule)
    # and their native dims are the exact sizes the ratio math needs (no rescale).
    want = {"e480": (640, 480), "e720": (1280, 720), "e1080": (1920, 1080)}
    for key, (w, h) in want.items():
        s = bclip.SOURCES[key]
        assert (s["w"], s["h"]) == (w, h)
        assert s["url"].startswith("https://")
        assert len(s["sha256"]) == 64 and all(c in "0123456789abcdef" for c in s["sha256"])
        assert s.get("name")                          # a tidy cache filename (URLs are %-escaped)


def test_ratio_outputs_are_whole_number_upscales():
    # 640x480 x4 -> 2560x1920 ; 1280x720 x2 -> 2560x1440 ; 1920x1080 x2 -> 3840x2160.
    assert (640 * 4, 480 * 4) == (2560, 1920)
    assert (1280 * 2, 720 * 2) == (2560, 1440)
    assert (1920 * 2, 1080 * 2) == (3840, 2160)


# ── ratio cells + plan ───────────────────────────────────────────────────────

def test_esrgan_targets_gated_by_tier_native_scales():
    # compact has only a native x4 weight -> 4X only; quality has native x2 + x4 -> both.
    assert vb.esrgan_targets("compact") == ["4X"]
    assert vb.esrgan_targets("quality") == ["2X", "4X"]


def test_build_esrgan_plan_compact_is_4x_only():
    plan = vb.build_esrgan_plan(["2X", "4X"], "compact")
    assert [c["name"] for c in plan] == ["4X-480"]
    c = plan[0]
    assert (c["out_w"], c["out_h"]) == (2560, 1920)
    assert c["resolution"] == 1920                     # output short side, == 480 * 4 (no resize)
    assert c["engine"] == "fixed_ratio"
    # compact resolves to the native x4 compact weight.
    assert c["model"] == "realesr-general-x4v3"


def test_build_esrgan_plan_quality_covers_both_ratios_natively():
    plan = vb.build_esrgan_plan(["2X", "4X"], "quality")
    by_name = {c["name"]: c for c in plan}
    assert set(by_name) == {"2X-720", "2X-1080", "4X-480"}
    # 2x targets use the NATIVE x2 weight (not x4-then-downscale); 4x uses x4plus.
    assert by_name["2X-720"]["model"] == "RealESRGAN_x2plus"
    assert by_name["2X-1080"]["model"] == "RealESRGAN_x2plus"
    assert by_name["4X-480"]["model"] == "RealESRGAN_x4plus"
    assert (by_name["2X-1080"]["out_w"], by_name["2X-1080"]["out_h"]) == (3840, 2160)


def test_build_esrgan_plan_respects_selected_ratios():
    # Only the ticked ratios expand to cells (quality, 2X only -> the two 2x cells).
    plan = vb.build_esrgan_plan(["2X"], "quality")
    assert sorted(c["name"] for c in plan) == ["2X-1080", "2X-720"]
    assert vb.build_esrgan_plan(["4X"], "quality")[0]["name"] == "4X-480"
    assert vb.build_esrgan_plan(["2X"], "compact") == []     # compact has no native 2x


def test_esrgan_cells_size_to_a_fixed_duration():
    # Cells are sized by DURATION (default 10 s) from each source's native fps, not a fixed frame
    # count, so the probe averages out per-frame noise without a long benchmark. 25 fps -> 250,
    # 30 fps -> 300.
    assert vb.DEFAULT_ESR_SECONDS == 10
    plan = {c["name"]: c for c in vb.build_esrgan_plan(["2X", "4X"], "quality")}
    assert plan["2X-720"]["frames"] == 25 * 10       # e720 is 25 fps
    assert plan["2X-1080"]["frames"] == 30 * 10      # e1080 is 30 fps
    assert plan["4X-480"]["frames"] == 25 * 10       # e480 is 25 fps
    # A custom duration scales the frame count linearly.
    short = {c["name"]: c for c in vb.build_esrgan_plan(["2X"], "quality", seconds=4)}
    assert short["2X-720"]["frames"] == 25 * 4


def test_esrgan_bench_model_namespaces_by_tier():
    assert vb.esrgan_bench_model("compact") == "esrgan-compact"
    assert vb.esrgan_bench_model("quality") == "esrgan-quality"
    assert vb.esrgan_bench_model("compact") != vb.esrgan_bench_model("quality")


# ── estimator: ESRGAN rate store + fixed_ratio job path ──────────────────────

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


def test_esrgan_rate_roundtrip_is_tier_scoped(db_conn):
    assert ve.esrgan_seconds_per_mp("CARD-X", "compact", db_conn) is None
    # The store keeps cumulative (MP, seconds) as the runner records: a whole clip's OUTPUT MP
    # (frames x cell MP, a large total), so 250 MP over 125 s -> 0.5 s/MP.
    ve.record_esrgan_rate(db_conn, "CARD-X", "compact", 250.0, 125.0)
    spm = ve.esrgan_seconds_per_mp("CARD-X", "compact", db_conn)
    assert spm == pytest.approx(0.5, rel=0.02)
    # A DIFFERENT tier is independent (compact and quality differ ~16x; never proxied).
    assert ve.esrgan_seconds_per_mp("CARD-X", "quality", db_conn) is None


def test_esrgan_tier_from_model_key():
    assert ve._esrgan_tier("realesr-general-x4v3") == "compact"
    assert ve._esrgan_tier("RealESRGAN_x4plus") == "quality"
    assert ve._esrgan_tier("RealESRGAN_x2plus") == "quality"
    assert ve._esrgan_tier(None) == "compact"          # fail-safe default


def test_estimate_job_uses_esrgan_rate_not_seedvr2(db_conn):
    # With a measured compact rate, a fixed_ratio job estimates from it; a SeedVR2 job on the
    # same (unbenchmarked) card has no rate and returns None -- proving the two paths are separate.
    ve.record_esrgan_rate(db_conn, "CARD-X", "compact", 250.0, 125.0)    # 0.5 s/MP
    secs = ve.estimate_job(100, "4X", "CARD-X", db_conn, src_w=640, src_h=480,
                           engine="fixed_ratio", model="realesr-general-x4v3")
    # 100 frames x (2560*1920/1e6 = 4.9152 MP) x 0.5 s/MP.
    assert secs == pytest.approx(100 * 4.9152 * 0.5, rel=0.01)
    assert ve.estimate_job(100, "4X", "CARD-X", db_conn, src_w=640, src_h=480,
                           engine="seedvr2") is None


def test_estimate_queue_local_fixed_ratio(db_conn):
    ve.record_esrgan_rate(db_conn, "CARD-X", "compact", 250.0, 125.0)    # 0.5 s/MP
    jobs = [{"frames": 50, "target": "4X", "width": 640, "height": 480, "segments": 1,
             "engine": "fixed_ratio", "model": "realesr-general-x4v3"}]
    est = ve.estimate_queue_local(jobs, "CARD-X", db_conn)
    assert est is not None and est["calibrated"] is True
    assert est["duration_seconds"] == pytest.approx(50 * 4.9152 * 0.5, rel=0.02)
    # An UNmeasured card returns None (honest: no rate yet), not a wrong SeedVR2 guess.
    assert ve.estimate_queue_local(jobs, "OTHER-CARD", db_conn) is None


def test_job_vram_floor_low_for_fixed_ratio():
    # Reaffirm the engine-aware floor: a Real-ESRGAN job never imposes the SeedVR2 target floors.
    assert ve.job_vram_floor({"engine": "fixed_ratio", "target": "4X"}) == ve.ESRGAN_VRAM_FLOOR
    assert ve.ESRGAN_VRAM_FLOOR <= 16


# ── contribute: title/filename follow the SUBMITTED rows, not the selection ──

def test_contribute_label_follows_deduped_rows():
    """Bug fix: selecting 3 GPUs but (after the community dedup) only one card's rows are new must
    title the issue for THAT card, not '3 GPUs'. The label/filename derive from _cards_in_rows(to_send)."""
    pytest.importorskip("tkinter")
    from gui.video_benchmark import BenchmarkWindow as BW
    # 3 selected, but only the 3090 has new rows left after dedup.
    to_send = [{"gpu_id": "NVIDIA GeForce RTX 3090", "target": "4K"},
               {"gpu_id": "NVIDIA GeForce RTX 3090", "target": "1440p"}]
    sent = BW._cards_in_rows(to_send)
    assert sent == ["NVIDIA GeForce RTX 3090"]                 # de-duped, first-appearance order
    assert BW._share_label(sent) == "NVIDIA GeForce RTX 3090"  # NOT "3 GPUs"
    assert BW._default_csv_name(BW, sent) == "benchmark_NVIDIA_GeForce_RTX_3090.csv"
    # A genuine multi-card submission still labels as N GPUs.
    multi = [{"gpu_id": "A"}, {"gpu_id": "B"}, {"gpu_id": "A"}]
    assert BW._cards_in_rows(multi) == ["A", "B"]
    assert BW._share_label(BW._cards_in_rows(multi)) == "2 GPUs"
    assert BW._cards_in_rows([]) == []                          # empty is safe
