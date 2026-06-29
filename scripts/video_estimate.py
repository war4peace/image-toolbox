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
#
# IMPORTANT: these were measured on a **4:3** test clip (Pisici.AVI, 320x240), so
# each target's output frame is 4:3 (1080p = 1440x1080, 1440p = 1920x1440, 4K =
# 2880x2160). SeedVR2's cost scales with OUTPUT pixels, so a 16:9 video at the same
# target label is ~33 % more pixels per frame and costs ~33 % more. The estimator
# therefore converts these to **seconds per output-megapixel** (rate / BENCH_OUT_MP)
# and multiplies by each video's real output size, so any aspect ratio estimates
# correctly from this one 4:3 benchmark.
RATES = {
    "1080p": {"RTX 5090": 0.94, "RTX PRO 6000": 0.74, "H200": 0.47, "B200": 0.35},
    "1440p": {"RTX PRO 6000": 1.96, "A100 80GB": 3.42, "H200": 1.90, "B200": 1.64},
    "4K":    {"RTX PRO 6000": 6.63, "H200": 7.20, "B200": 6.40},
}

# Target -> output SHORT side (px). Mirrors batch_video_upscale.TARGET_RES.
SHORT_SIDE = {"1080p": 1080, "1440p": 1440, "4K": 2160}

# Each target is a LANDSCAPE box the output must fit INSIDE: max width OR max height
# (so a 4K clip plays 1:1 on a 3840x2160 screen). A portrait clip is therefore capped
# by its HEIGHT, not by pinning its short side to the target (which pushed a vertical
# clip to 3840 tall at "4K" = 2x the height and ~3x the pixels). 16:9 boxes.
TARGET_BOX = {"1080p": (1920, 1080), "1440p": (2560, 1440), "4K": (3840, 2160)}

# Output megapixels of the 4:3 benchmark clip per target (the denominator that turns
# a benchmark s/frame into s/MP). 1440x1080, 1920x1440, 2880x2160.
BENCH_OUT_MP = {"1080p": 1.5552, "1440p": 2.7648, "4K": 6.2208}

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


def fit_scale(src_w, src_h, target):
    """The scale that fits a (src_w x src_h) frame INSIDE the target's landscape box
    (preserving aspect): min(box_w/w, box_h/h). >1 = an upscale, <1 = a downscale.
    None if the dims or target are unknown."""
    box = TARGET_BOX.get(target)
    if not box or not src_w or not src_h:
        return None
    return min(box[0] / src_w, box[1] / src_h)


def output_dims(src_w, src_h, target):
    """The output (w, h) for a source upscaled to `target`: scaled to FIT the target
    box (so a portrait clip is capped by height, a landscape one by width/height).
    None if the source dimensions are unknown/invalid."""
    s = fit_scale(src_w, src_h, target)
    if s is None:
        return None
    return (round(src_w * s), round(src_h * s))


def fit_short_side(src_w, src_h, target):
    """The SeedVR2 `--resolution` (output SHORT side) that makes the output fit the
    target box at the source aspect: the short side of output_dims. None if unknown.
    For landscape this is the target height (e.g. 2160); for a portrait clip it is
    smaller (the long side is what hits the box's height cap)."""
    d = output_dims(src_w, src_h, target)
    return min(d) if d else None


def output_megapixels(src_w, src_h, target):
    """Output megapixels per frame for a (source, target) upscale. Falls back to
    the 4:3 BENCH_OUT_MP when the source size is unknown, so callers that lack
    dimensions keep the old benchmark-aspect behaviour."""
    d = output_dims(src_w, src_h, target)
    if d:
        return d[0] * d[1] / 1_000_000.0
    return BENCH_OUT_MP.get(target)


def seconds_per_mp(gpu_id, target, conn=None):
    """Seconds per OUTPUT megapixel for (gpu, target). Prefers the user's OWN
    measured history (db.gpu_perf, task `video-mp-<target>`, recorded in MP) once
    there is enough, else the benchmark rate converted from s/frame via the 4:3
    BENCH_OUT_MP, else None (an un-benchmarked card we can't estimate)."""
    if conn is not None:
        try:
            import db
            per_100 = db.get_gpu_perf(conn, f"video-mp-{target}", gpu_id, min_images=300)
            if per_100:
                return per_100 / 100.0          # gpu_perf stores seconds / 100 units
        except Exception:
            pass
    model = map_gpu(gpu_id)
    rate = RATES.get(target, {}).get(model) if model else None
    bench_mp = BENCH_OUT_MP.get(target)
    if rate is None or not bench_mp:
        return None
    return rate / bench_mp


def record_run(conn, gpu_id, target, out_megapixels, seconds):
    """Accumulate a finished video run's timing so future estimates self-improve
    (db.gpu_perf, task `video-mp-<target>`). The unit is OUTPUT MEGAPIXELS, not
    frames, so the learned rate is aspect-independent. Best-effort."""
    try:
        import db
        db.record_gpu_perf(conn, f"video-mp-{target}", gpu_id, out_megapixels,
                           seconds, min_images=300)
    except Exception:
        pass


def estimate_job(frames, target, gpu_id, conn=None, src_w=None, src_h=None):
    """Processing seconds for one (frames, target) job on a GPU, or None if the
    card has no rate for that target. Cost scales with OUTPUT megapixels, so pass
    the source `src_w`/`src_h` for an aspect-correct estimate; without them it
    assumes the 4:3 benchmark aspect (the old behaviour)."""
    spm = seconds_per_mp(gpu_id, target, conn)
    mp_per_frame = output_megapixels(src_w, src_h, target)
    if spm is None or mp_per_frame is None:
        return None
    return frames * mp_per_frame * spm


def estimate_queue(jobs, gpu_id, price_per_hour, spin_up_seconds=DEFAULT_SPIN_UP_SECONDS,
                   conn=None):
    """Estimate the whole queue on one GPU. `jobs` = [{frames, target, segments,
    width, height}]. width/height are the source size (optional); when present the
    estimate is aspect-correct (output megapixels), else it assumes 4:3.
    Returns a dict, or None if the GPU can't serve every target in the queue.

    Spin-up is counted ONCE (one pod for the queue). Cost uses the live price."""
    total_proc = 0.0
    total_frames = 0
    total_segments = 0
    for j in jobs:
        secs = estimate_job(j.get("frames") or 0, j["target"], gpu_id, conn,
                            src_w=j.get("width"), src_h=j.get("height"))
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
