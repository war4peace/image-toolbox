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
# Real-ESRGAN (#18 B) is a LIGHT fixed-ratio GAN that tiles on OOM, so it runs on any
# modern card regardless of target; its floor is a low constant, NOT the SeedVR2 target
# floors above (which would wrongly exclude the cheap low-VRAM cards the remote esrgan
# path exists to use). 8 GB clears every deployable RunPod card.
ESRGAN_VRAM_FLOOR = 8


def job_vram_floor(job):
    """The VRAM floor one queued job imposes: the low ESRGAN floor for a fixed_ratio job,
    else the SeedVR2 per-target floor. Engine defaults to seedvr2 when the column is NULL."""
    if (job.get("engine") or "seedvr2") == "fixed_ratio":
        return ESRGAN_VRAM_FLOOR
    return VRAM_FLOOR.get(job["target"], 0)

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


# Dynamic upscale-RATIO targets (feature #7): a low-res source gets 2x/4x targets computed
# from its own size, not just the named preset boxes. The token is "2X"/"4X"; its output is
# simply source x N (no box-fit), so a 320x240 source -> 640x480 / 1280x960. Ordered small
# first. (2x and 4x only, by product decision.)
RATIO_TARGETS = {"2X": 2, "4X": 4}


def ratio_of(target):
    """The integer upscale ratio for a ratio target token ('2X' -> 2), or None for a preset
    (1080p/1440p/4K) or an unknown token."""
    return RATIO_TARGETS.get(str(target or "").upper())


def is_ratio_target(target):
    return ratio_of(target) is not None


def fit_scale(src_w, src_h, target):
    """The upscale factor for (src -> target). For a RATIO target it is the ratio itself
    (2/4); for a preset it is the box-fit scale min(box_w/w, box_h/h) that fits the source
    INSIDE the target's landscape box (preserving aspect). >1 = an upscale, <1 = a downscale.
    None if the dims or target are unknown."""
    if not src_w or not src_h:
        return None
    n = ratio_of(target)
    if n:
        return float(n)                              # a ratio target scales by N, no box-fit
    box = TARGET_BOX.get(target)
    if not box:
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


# Fixed queue-guard threshold (a safety net, NOT the configurable skip-cutoff):
# an upscale that enlarges by less than this factor is allowed but WARNED about.
MARGINAL_UPSCALE = 1.5      # < 1.5x (< 50% larger) -> "marginal"


def classify_upscale(src_w, src_h, target):
    """Queue-guard verdict for a (source -> target) upscale, from the box-fit scale:
      'downscale' — scale <= 1.0 (would shrink or not enlarge the frame): block it.
      'marginal'  — 1.0 < scale < 1.5 (< 50% larger): allow, but warn.
      None        — a healthy upscale (>= 50% larger), or the dims/target are unknown.
    The thresholds are FIXED, independent of the user's skip-cutoff setting."""
    s = fit_scale(src_w, src_h, target)
    if s is None or s >= MARGINAL_UPSCALE:
        return None
    return "downscale" if s <= 1.0 else "marginal"


def output_megapixels(src_w, src_h, target):
    """Output megapixels per frame for a (source, target) upscale. Falls back to
    the 4:3 BENCH_OUT_MP when the source size is unknown, so callers that lack
    dimensions keep the old benchmark-aspect behaviour."""
    d = output_dims(src_w, src_h, target)
    if d:
        return d[0] * d[1] / 1_000_000.0
    return BENCH_OUT_MP.get(target)


# ─────────────────────────────────────────────
#  FEASIBILITY: max output a GPU can upscale to (#7)
# ─────────────────────────────────────────────
# The VAE-decode ceiling scales with OUTPUT megapixels and is model-independent (docs 14),
# so a card's reach is best expressed as a max feasible output-MP. These per-VRAM-tier caps
# are SEEDS: 24 GB = 1080p (~2.1 MP) is measured on the 3090 (user's call: allow the tight
# 1080p); the rest are conservative guesses biased LOW so the guard errs toward NOT offering
# an OOM target. A card's OWN benchmark/learned data (db.max_feasible_output_mp) RAISES its
# cap above the seed once it proves a bigger output fits. Ascending (min VRAM GB, max MP).
_MAX_MP_TIERS = [
    (8,  0.6),
    (10, 0.9),
    (12, 1.1),
    (16, 1.4),
    (20, 1.8),
    (24, 2.1),      # 1080p (1920x1080 = 2.07 MP) — measured on the 3090
    (32, 3.8),      # 1440p (2560x1440 = 3.69 MP)
    (48, 8.4),      # 4K   (3840x2160 = 8.29 MP)
    (80, 8.4),
]


def _seed_max_mp(total_vram_gb):
    """The seed max feasible output-MP for a card of `total_vram_gb` (largest tier <= VRAM,
    +0.5 GB tolerance for a 23.6-reported 24 GB card). None if VRAM is unknown."""
    if not total_vram_gb or total_vram_gb <= 0:
        return None
    cap = _MAX_MP_TIERS[0][1]
    for thr, mp in _MAX_MP_TIERS:
        if total_vram_gb + 0.5 >= thr:
            cap = mp
    return cap


def max_output_mp(total_vram_gb, gpu_id=None, conn=None):
    """The largest OUTPUT megapixels this card is believed able to upscale to: the VRAM-tier
    seed, RAISED to anything the card's own benchmark/learned data proves feasible (never
    lowered — a single contended failure shouldn't hide a target; the benchmark's free-VRAM
    tag handles that). Returns 0.0 if VRAM is unknown (callers then don't filter)."""
    cap = _seed_max_mp(total_vram_gb) or 0.0
    if conn is not None and gpu_id:
        try:
            import db
            proven = db.max_feasible_output_mp(conn, gpu_id)
            if proven and proven > cap:
                cap = proven
        except Exception:
            pass
    return cap


# ─────────────────────────────────────────────
#  TARGET ENUMERATION (ratios + presets), feature #7
# ─────────────────────────────────────────────

# All candidate targets, small output first: the ratio targets then the named presets. The
# feasible/eligible helpers filter this by "is it an upscale" and "does it fit the GPU".
_ALL_TARGETS = list(RATIO_TARGETS) + list(SHORT_SIDE)   # ["2X","4X","1080p","1440p","4K"]
# Public alias: the complete, canonical set of whole-video target tokens. Used by the scan's
# disk-reconcile (batch_video_upscale.reconcile_outputs_from_disk) to check every target a
# producing install could have written, independent of THIS install's feasibility/cutoff config.
ALL_TARGETS = _ALL_TARGETS

# Ratio targets are for LOW-RES sources: never offer a ratio whose output exceeds the largest
# preset (4K, ~8.3 MP), so a 4K source isn't offered an 8K "2x" (and still counts as
# "already >= 4K, nothing to upscale to" in the scan, as before).
_MAX_RATIO_OUTPUT_MP = TARGET_BOX["4K"][0] * TARGET_BOX["4K"][1] / 1_000_000.0


def source_eligible_targets(src_w, src_h, skip_cutoff_pct=0):
    """Targets that are a genuine UPSCALE of this source (ignoring the GPU), ratios first
    then presets, ordered by output size and DEDUPED by output dims (if a ratio and a preset
    yield the same frame, the preset name wins). A downscale / barely-below source is dropped
    (skip-cutoff), and a ratio bigger than 4K is dropped (ratios are a low-res-source feature).
    This is the source-only set; feasible_targets() then filters it by VRAM."""
    if not src_w or not src_h:
        return []
    min_scale = 1.0 + (skip_cutoff_pct or 0) / 100.0
    seen_dims = {}
    for t in _ALL_TARGETS:
        s = fit_scale(src_w, src_h, t)
        d = output_dims(src_w, src_h, t)
        if s is None or d is None or s <= min_scale:
            continue
        if is_ratio_target(t):
            if d in seen_dims:                          # a preset at the same size wins
                continue
            if d[0] * d[1] / 1_000_000.0 > _MAX_RATIO_OUTPUT_MP + 1e-6:
                continue                                # ratio bigger than 4K: not offered
        seen_dims[d] = t
    # order by output area (small first), stable
    ordered = sorted(seen_dims.items(), key=lambda kv: kv[0][0] * kv[0][1])
    return [t for _d, t in ordered]


def feasible_targets(src_w, src_h, max_mp, skip_cutoff_pct=0):
    """The source-eligible targets that ALSO fit the GPU (output-MP <= max_mp). When max_mp
    is falsy (unknown GPU) the source-eligible set is returned unfiltered (no false guard)."""
    elig = source_eligible_targets(src_w, src_h, skip_cutoff_pct)
    if not max_mp:
        return elig
    out = []
    for t in elig:
        mp = output_megapixels(src_w, src_h, t)
        if mp is None or mp <= max_mp + 1e-6:
            out.append(t)
    return out


def target_is_feasible(src_w, src_h, target, max_mp):
    """True if (source -> target) fits a GPU whose max feasible output-MP is `max_mp`. A
    falsy max_mp (unknown) is treated as feasible (don't gray a row we can't judge)."""
    if not max_mp:
        return True
    mp = output_megapixels(src_w, src_h, target)
    return mp is None or mp <= max_mp + 1e-6


def target_label(src_w, src_h, target):
    """A combobox/display label showing the concrete output size, e.g. '2x (640x480)' or
    '1080p (1440x1080)'. Falls back to the bare token when dims are unknown."""
    d = output_dims(src_w, src_h, target)
    n = ratio_of(target)
    name = f"{n}x" if n else str(target)
    return f"{name} ({d[0]}x{d[1]})" if d else str(target)


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
            # The EXACT target has no history, but this card may have run OTHER targets. SeedVR2
            # cost is ~per-output-MP, so the card's pooled video-MP rate is a good cross-target
            # estimate -- enough to keep the live progress bar / estimate alive on the FIRST run
            # of a new target (which then records its own rate). Uses the user's OWN data.
            any_100 = db.get_video_mp_rate_any(conn, gpu_id, min_images=300)
            if any_100:
                return any_100 / 100.0
        except Exception:
            pass
    # A ratio target (2x/4x) has no benchmark row of its own; its s/MP is well-approximated
    # by the 1080p preset's (SeedVR2 cost is ~per-output-MP), so remote cost/GPU-ranking works
    # for a ratio-target queue instead of dropping every card for "no rate".
    rate_target = "1080p" if (is_ratio_target(target) and target not in RATES) else target
    model = map_gpu(gpu_id)
    rate = RATES.get(rate_target, {}).get(model) if model else None
    bench_mp = BENCH_OUT_MP.get(rate_target)
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


# ─────────────────────────────────────────────
#  LOCAL time estimate (feature #7): no cost, no pod spin-up
# ─────────────────────────────────────────────
# The local estimate is HISTORY-DRIVEN and honest: it uses the user's OWN measured
# seconds-per-output-MP for THIS card+target (db.gpu_perf, the same store the remote
# path fills, keyed by the nvidia-smi card name), and shows nothing invented before
# that. A run self-calibrates per segment (batch_video_upscale records each segment),
# so the SECOND run onward shows a real measured time; the first run is covered by the
# in-run live ETA. LOCAL_RATES is a SEED hook the per-card benchmark suite (docs §16)
# populates with rigorously-measured rates -- deliberately empty now rather than
# fabricated (we have no honest per-card table yet, only the dev's own 3090, which
# history captures directly). Cards absent from both history and seed estimate as
# "calibrates after the first segment", not a guessed number.
LOCAL_RATES = {}                 # {target: {card model token: seconds per output-MP}}
_LOCAL_MODEL_TOKENS = []         # [(substring, model token)] — grows with LOCAL_RATES

# Trust floor (cumulative output-MP) before the LOCAL estimate uses measured history. Far
# lower than the remote 300: a local card is a single deterministic device, so even a
# small first segment (or one benchmark probe) is a trustworthy sample. One 60 s segment is
# thousands of MP, so real runs still calibrate after the first segment; this just lets the
# benchmark's short clip (tens of MP) register too.
LOCAL_MIN_MP = 40


def map_local_gpu(gpu_id_or_name):
    """Resolve a local card name to a LOCAL_RATES seed token, or None if unseeded."""
    s = (gpu_id_or_name or "").upper()
    for token, model in _LOCAL_MODEL_TOKENS:
        if token in s:
            return model
    return None


def local_seconds_per_mp(gpu_id, target, conn=None):
    """(seconds/output-MP, calibrated) for a LOCAL (gpu, target). Prefers the user's OWN
    measured history (db.gpu_perf, `video-mp-<target>`) -> calibrated=True; else a
    LOCAL_RATES seed -> calibrated=False; else (None, False) when we have neither (the GUI
    then shows work-size only, honestly declining to invent a time)."""
    if conn is not None:
        try:
            import db
            per_100 = db.get_gpu_perf(conn, f"video-mp-{target}", gpu_id, min_images=LOCAL_MIN_MP)
            if per_100:
                return per_100 / 100.0, True
            # Exact target unmeasured, but the card may have run OTHER targets: its pooled
            # video-MP rate is a rough cross-target estimate (SeedVR2 cost is ~per-output-MP),
            # so the FIRST run of a new target still gets a live bar / estimate. Rough (the s/MP
            # varies by regime), so calibrated=False -> the readout marks it "(rough)".
            any_100 = db.get_video_mp_rate_any(conn, gpu_id, min_images=LOCAL_MIN_MP)
            if any_100:
                return any_100 / 100.0, False
        except Exception:
            pass
    model = map_local_gpu(gpu_id)
    spm = LOCAL_RATES.get(target, {}).get(model) if model else None
    return (spm, False) if spm else (None, False)


def record_benchmark_rate(conn, gpu_id, target, out_megapixels, seconds):
    """Record a BENCHMARK probe's timing as the measured rate for (gpu, target). Like
    record_run but with a low trust floor (a benchmark clip is short but a controlled, clean
    single-probe measurement, not noisy accumulation), so one probe calibrates the estimate."""
    try:
        import db
        db.record_gpu_perf(conn, f"video-mp-{target}", gpu_id, out_megapixels, seconds,
                           min_images=1)
    except Exception:
        pass


def estimate_queue_local(jobs, gpu_id, conn=None):
    """TIME estimate for the whole queue on a LOCAL card: no cost, no pod spin-up (the
    engine is already resident). `jobs` = [{frames, target, segments, width, height}].
    Returns {duration_seconds, total_frames, segments, calibrated} or None when the card
    has no rate (history or seed) for some queued target. `calibrated` is True only when
    EVERY job's rate came from measured history (so the GUI can mark a seeded guess)."""
    total_proc = 0.0
    total_frames = 0
    total_segments = 0
    calibrated = True
    for j in jobs:
        spm, cal = local_seconds_per_mp(gpu_id, j["target"], conn)
        mp_per_frame = output_megapixels(j.get("width"), j.get("height"), j["target"])
        if spm is None or mp_per_frame is None:
            return None
        total_proc += (j.get("frames") or 0) * mp_per_frame * spm
        calibrated = calibrated and cal
        total_frames += j.get("frames") or 0
        total_segments += j.get("segments") or 0
    return {
        "duration_seconds": total_proc,
        "processing_seconds": total_proc,
        "total_frames": total_frames,
        "segments": total_segments,
        "calibrated": calibrated,
    }


def max_target_floor(jobs):
    """The VRAM floor the queue's most demanding job imposes (engine-aware: a fixed_ratio
    job is light, a SeedVR2 job scales with its target)."""
    return max((job_vram_floor(j) for j in jobs), default=0)


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
