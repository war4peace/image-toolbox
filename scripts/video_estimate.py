"""
video_estimate.py
-----------------
Cost / duration estimator for the Video Upscaler (#2) GUI (docs/video-upscaler.md
section 15.7). Pure stdlib + db; no torch, no network here (the GUI passes in the
live GPU list from runpod_client.available_gpus).

Inputs:
  * a per-(target, GPU) seconds/frame RATE TABLE, seeded from the benchmark
    (section 7) and refined by the user's OWN history via db.gpu_perf;
  * a per-target VRAM FLOOR, so a card too small for the queue's hardest target is
    not offered;
  * a SPIN-UP overhead (pod boot + model load) counted ONCE per Start, since it
    dominates the cost of short clips.

The queue cost on a card:
    total_seconds = spin_up + sum_over_jobs(frames * s_per_frame[target])
    cost          = total_seconds / 3600 * price_per_hour

`recommend_gpus` intersects the live-available cards with the rate table, drops
those below the max-target VRAM floor (and, optionally, above a price ceiling),
and sorts by cheapest TOTAL queue cost (section 15.1 Q3).
"""

import math

# Seconds/frame per (target, GPU model), warm, from the benchmark (section 7).
# Keyed by a model token; map_gpu() resolves a RunPod gpuTypeId/name to one.
RATES = {
    "1080p": {"RTX 5090": 0.94, "RTX PRO 6000": 0.74, "H200": 0.47, "B200": 0.35},
    "1440p": {"RTX PRO 6000": 1.96, "A100 80GB": 3.42, "H200": 1.90, "B200": 1.64},
    "4K":    {"RTX PRO 6000": 6.63, "H200": 7.20, "B200": 6.40},
}

# Minimum VRAM (GB) for a target. 1080p needs ~31 GB (5090 offload peak measured
# 30.7); 1440p plateaus 71-77 GB; 4K needs ~80 GB to fit a small window and a
# big-VRAM card (~140 GB) for a usable continuity window, so the floor admits
# 96 GB+ cards only (section 7 / 15.7).
VRAM_FLOOR = {"1080p": 32, "1440p": 80, "4K": 90}

# Pod boot + worker model load, billed once per Start (measured ~354 s worker-ready
# on a 5090). Overridable via config video.spin_up_seconds.
DEFAULT_SPIN_UP_SECONDS = 360

# Order matters: match the most specific token first.
_MODEL_TOKENS = [
    ("B200", "B200"),
    ("H200", "H200"),
    ("A100", "A100 80GB"),
    ("PRO 6000", "RTX PRO 6000"),
    ("6000 BLACKWELL", "RTX PRO 6000"),
    ("5090", "RTX 5090"),
]


def map_gpu(gpu_id_or_name):
    """Resolve a RunPod gpuTypeId/name to a rate-table model token, or None if we
    have no benchmark for it."""
    s = (gpu_id_or_name or "").upper()
    for token, model in _MODEL_TOKENS:
        if token in s:
            return model
    return None


def rate_for(gpu_id, target, conn=None):
    """Seconds/frame for (gpu, target). Prefers the user's OWN measured history
    (db.gpu_perf, task `video-<target>`) once there is enough of it, else the
    benchmark table, else None (an un-benchmarked card we can't estimate)."""
    if conn is not None:
        try:
            import db
            per_100 = db.get_gpu_perf(conn, f"video-{target}", gpu_id, min_images=300)
            if per_100:
                return per_100 / 100.0          # gpu_perf stores seconds / 100 units
        except Exception:
            pass
    model = map_gpu(gpu_id)
    return RATES.get(target, {}).get(model) if model else None


def record_run(conn, gpu_id, target, frames, seconds):
    """Accumulate a finished video run's timing so future estimates self-improve
    (reuses db.gpu_perf with a `video-<target>` task key). Best-effort."""
    try:
        import db
        db.record_gpu_perf(conn, f"video-{target}", gpu_id, frames, seconds,
                           min_images=300)
    except Exception:
        pass


def estimate_job(frames, target, gpu_id, conn=None):
    """Processing seconds for one (frames, target) job on a GPU, or None if the
    card has no rate for that target."""
    r = rate_for(gpu_id, target, conn)
    return None if r is None else frames * r


def estimate_queue(jobs, gpu_id, price_per_hour, spin_up_seconds=DEFAULT_SPIN_UP_SECONDS,
                   conn=None):
    """Estimate the whole queue on one GPU. `jobs` = [{frames, target, segments}].
    Returns a dict, or None if the GPU can't serve every target in the queue.

    Spin-up is counted ONCE (one pod for the queue). Cost uses the live price."""
    total_proc = 0.0
    total_frames = 0
    total_segments = 0
    for j in jobs:
        secs = estimate_job(j.get("frames") or 0, j["target"], gpu_id, conn)
        if secs is None:
            return None                          # card can't do one of the targets
        total_proc += secs
        total_frames += j.get("frames") or 0
        total_segments += j.get("segments") or 0
    duration = spin_up_seconds + total_proc
    cost = duration / 3600.0 * float(price_per_hour or 0)
    return {
        "duration_seconds": duration,
        "processing_seconds": total_proc,
        "spin_up_seconds": spin_up_seconds,
        "cost": cost,
        "total_frames": total_frames,
        "segments": total_segments,
        "cost_per_segment": (cost / total_segments) if total_segments else 0.0,
    }


def max_target_floor(jobs):
    """The VRAM floor the queue's most demanding target imposes."""
    return max((VRAM_FLOOR.get(j["target"], 0) for j in jobs), default=0)


def recommend_gpus(available, jobs, spin_up_seconds=DEFAULT_SPIN_UP_SECONDS,
                   price_cap=None, conn=None):
    """Rank the live-available GPUs for this queue, cheapest TOTAL cost first.

    `available` = dicts from runpod_client.available_gpus (keys id, name,
    memory_gb, price, stock). Drops cards below the queue's max-target VRAM floor,
    cards we have no rate for, and (if `price_cap` set) cards over the hourly cap.
    Returns [{id, name, memory_gb, price, stock, estimate}], sorted by estimate
    cost; `estimate` is the estimate_queue dict."""
    floor = max_target_floor(jobs)
    ranked = []
    for g in available or []:
        if (g.get("memory_gb") or 0) < floor:
            continue
        if price_cap and g.get("price") and g["price"] > price_cap:
            continue
        est = estimate_queue(jobs, g.get("id") or g.get("name"), g.get("price"),
                             spin_up_seconds, conn)
        if est is None:
            continue                             # no rate for some target on this card
        ranked.append({**g, "estimate": est})
    ranked.sort(key=lambda r: r["estimate"]["cost"])
    return ranked


def fmt_duration(seconds):
    """Human hh:mm:ss for a duration in seconds."""
    seconds = int(round(seconds or 0))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:d}:{s:02d}"
