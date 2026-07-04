"""
video_estimate — the Video Upscaler cost/duration estimator (item 2). The
aspect-ratio under-estimate was a real unit bug (the 4:3 benchmark applied to a
16:9 clip), so the box-fit / output-megapixel math is worth pinning down. Pure
stdlib; db is only touched when a conn is passed (we never pass one here).
"""

import math

import pytest

import video_estimate as ve


# ── map_gpu ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name,model", [
    ("NVIDIA B200", "B200"),
    ("H200 SXM", "H200"),
    ("A100 80GB PCIe", "A100 80GB"),
    ("RTX PRO 6000 Blackwell", "RTX PRO 6000"),
    ("RTX 5090", "RTX 5090"),
])
def test_map_gpu_resolves_known_tokens(name, model):
    assert ve.map_gpu(name) == model


def test_map_gpu_unknown_is_none():
    assert ve.map_gpu("RTX 4060") is None
    assert ve.map_gpu(None) is None


# ── box fit ─────────────────────────────────────────────────────────────────

def test_fit_scale_landscape_16x9_to_4k_hits_width_and_height_together():
    # A 1280x720 (16:9) clip to the 3840x2160 box scales x3 on both axes.
    assert ve.fit_scale(1280, 720, "4K") == pytest.approx(3.0)


def test_fit_scale_portrait_is_capped_by_height():
    # A 1080x1920 portrait clip to the 4K box: height is the binding limit.
    s = ve.fit_scale(1080, 1920, "4K")
    assert s == pytest.approx(2160 / 1920)


def test_output_dims_fit_inside_the_box():
    for target, (bw, bh) in ve.TARGET_BOX.items():
        w, h = ve.output_dims(1280, 720, target)
        assert w <= bw + 1 and h <= bh + 1


def test_fit_scale_unknown_target_or_dims_is_none():
    assert ve.fit_scale(1280, 720, "8K") is None
    assert ve.fit_scale(0, 720, "4K") is None


# ── output megapixels: the aspect-ratio bug regression ──────────────────────

def test_16x9_costs_more_megapixels_than_the_4x3_benchmark():
    # This is the exact bug: a 16:9 output at "4K" has ~33% more pixels than the
    # 4:3 benchmark frame, so the estimate must scale up, not reuse the benchmark.
    mp_16x9 = ve.output_megapixels(1280, 720, "4K")
    assert mp_16x9 > ve.BENCH_OUT_MP["4K"]
    assert mp_16x9 == pytest.approx(3840 * 2160 / 1_000_000.0)


def test_output_megapixels_falls_back_to_benchmark_without_dims():
    assert ve.output_megapixels(None, None, "1080p") == ve.BENCH_OUT_MP["1080p"]


# ── per-frame / per-job seconds ─────────────────────────────────────────────

def test_seconds_per_mp_from_benchmark():
    # RTX PRO 6000 @ 4K: 6.63 s/frame over the 6.2208 MP benchmark frame.
    spm = ve.seconds_per_mp("RTX PRO 6000", "4K")
    assert spm == pytest.approx(6.63 / ve.BENCH_OUT_MP["4K"])


def test_seconds_per_mp_unknown_card_is_none():
    assert ve.seconds_per_mp("RTX 4060", "4K") is None


def test_estimate_job_scales_with_frames_and_pixels():
    one = ve.estimate_job(1, "4K", "RTX PRO 6000", src_w=1280, src_h=720)
    ten = ve.estimate_job(10, "4K", "RTX PRO 6000", src_w=1280, src_h=720)
    assert ten == pytest.approx(one * 10)
    assert one is not None and one > 0


def test_estimate_job_none_when_card_cannot_serve_target():
    # RTX 5090 has a 1080p rate but no 4K rate in the table.
    assert ve.estimate_job(100, "4K", "RTX 5090", src_w=1280, src_h=720) is None


# ── whole-queue estimate ────────────────────────────────────────────────────

def test_estimate_queue_counts_spin_up_once_and_prices_it():
    jobs = [
        {"frames": 100, "target": "4K", "segments": 2, "width": 1280, "height": 720},
        {"frames": 50, "target": "4K", "segments": 1, "width": 1280, "height": 720},
    ]
    est = ve.estimate_queue(jobs, "RTX PRO 6000", price_per_hour=3.6,
                            spin_up_seconds=300)
    assert est is not None
    proc = ve.estimate_job(150, "4K", "RTX PRO 6000", src_w=1280, src_h=720)
    assert est["processing_seconds"] == pytest.approx(proc)
    assert est["duration_seconds"] == pytest.approx(300 + proc)
    assert est["cost"] == pytest.approx(est["duration_seconds"] / 3600 * 3.6)
    assert est["total_frames"] == 150
    assert est["segments"] == 3


def test_estimate_queue_none_if_any_target_unservable():
    jobs = [{"frames": 100, "target": "4K", "segments": 1,
             "width": 1280, "height": 720}]
    # RTX 5090: no 4K rate -> whole queue unestimable on that card.
    assert ve.estimate_queue(jobs, "RTX 5090", price_per_hour=1.0) is None


# ── recommend_gpus ──────────────────────────────────────────────────────────

def test_recommend_gpus_drops_below_vram_floor_and_sorts_cheapest_first():
    # ids must carry a token map_gpu recognises ("PRO 6000", "H200", "5090"),
    # exactly as recommend_gpus feeds g["id"] to the rate table.
    jobs = [{"frames": 100, "target": "4K", "segments": 1,
             "width": 1280, "height": 720}]      # floor = VRAM_FLOOR["4K"] = 90
    available = [
        {"id": "H200", "name": "H200", "memory_gb": 141, "price": 4.0, "stock": "high"},
        {"id": "RTX PRO 6000", "name": "RTX PRO 6000", "memory_gb": 96, "price": 2.0, "stock": "high"},
        {"id": "RTX 5090", "name": "RTX 5090", "memory_gb": 32, "price": 0.9, "stock": "high"},
    ]
    ranked = ve.recommend_gpus(available, jobs)
    ids = [r["id"] for r in ranked]
    # 5090 dropped by the 90 GB floor; PRO 6000 cheaper total than H200.
    assert "RTX 5090" not in ids
    assert ids == ["RTX PRO 6000", "H200"]
    assert ranked[0]["estimate"]["cost"] <= ranked[1]["estimate"]["cost"]


def test_recommend_gpus_honours_price_cap():
    jobs = [{"frames": 10, "target": "4K", "segments": 1,
             "width": 1280, "height": 720}]
    available = [
        {"id": "H200", "name": "H200", "memory_gb": 141, "price": 4.0, "stock": "high"},
        {"id": "RTX PRO 6000", "name": "RTX PRO 6000", "memory_gb": 96, "price": 2.0, "stock": "high"},
    ]
    ranked = ve.recommend_gpus(available, jobs, price_cap=3.0)
    assert [r["id"] for r in ranked] == ["RTX PRO 6000"]


# ── fmt_duration ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("secs,text", [
    (0, "0:00"),
    (59, "0:59"),
    (60, "1:00"),
    (600, "10:00"),
    (3600, "1:00:00"),
    (3661, "1:01:01"),
])
def test_fmt_duration(secs, text):
    assert ve.fmt_duration(secs) == text
