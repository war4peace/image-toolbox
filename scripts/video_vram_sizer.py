"""
video_vram_sizer.py
-------------------
Predictive, per-model VRAM batch sizer for LOCAL video upscaling (feature #7,
docs/local-video-upscaler.md sections 7 / 14 / 15). Picks the largest safe temporal
window (batch, overlap) that fits on the FIRST TRY, per model, from the card's FREE
VRAM and the output megapixels. It NEVER starts high and cascades: on Windows an
over-large batch OOMs and the failed attempts fragment VRAM so badly that a batch
which fits from a clean start then fails too (docs 14.2/14.3), and the allocator fix
`expandable_segments` is unavailable on Windows (14.3). So sizing correctly up front
is the whole game.

Design = tier-anchored + self-calibrating (docs "option b"):
  * a SEED per (VRAM tier, output-MP) anchored to REAL measurements -- 24 GB card,
    1280x960 2x -> bs17/ov6; 24 GB, 1080p -> bs5/ov4 (docs 14.1/14.3, user decision).
    Other tiers have NO local data, so they are deliberately CONSERVATIVE (biased low):
    a too-small seed just shrinks the window; a too-big one causes the first-run OOM we
    are avoiding. The user-runnable benchmark suite (docs) + learning raise them safely.
  * a FREE-VRAM step-DOWN (never up): the user's desktop eats ~3 GB, so we budget the
    batch against LIVE free VRAM, not the card total, and shrink the seed if free is low.
  * a LEARNED override: after each segment the real outcome is recorded per
    (gpu|model, output-MP key) in db.video_batch_learn (model-qualified key, so 7B and
    3B keep separate rows and remote rows are untouched). A learned value supersedes the
    seed, so the sizer self-calibrates per card+model over time -- like the remote path
    self-improves db.gpu_perf. No fragile hand-fit curve is trusted as ground truth.

The working-set model used for the free-VRAM step-down (`_predicted_peak_gb`) is
calibrated on THIS project's RTX 3090 offload runs; it is only a STARTING estimate for
other GPUs (the curve is effectively per-architecture -- the pod's 5090 profile
under-predicted the 3090, docs 14.3), which is exactly why learning + the benchmark
suite, not the curve, are the source of truth off the measured point.

Stdlib + optional torch (only to read free VRAM; falls back to nvidia-smi, then to the
card-total assumption). Pure/tkinter-free and unit-testable. db import is guarded so an
older tree without it still loads (seed-only, no learning).
"""

import subprocess

try:
    import db as _db
except Exception:                                    # noqa: BLE001
    _db = None

# ── constants ────────────────────────────────────────────────────────────────

BATCH_FLOOR = 5              # below this a temporal window barely helps (matches pod)
BATCH_CAP = 33               # AUTO-path ceiling; a manual override may exceed it
MIN_OVERLAP = 6              # quality floor: measured, 3 left a seam, 6 was undetectable
MAX_OVERLAP = 15             # cost cap: the seam is a local transition, so blending more than
                            # ~15 frames adds redundant compute for no visible gain (batch/6
                            # ran away to 80 at batch 480). Above the floor, 15 hides any seam.
_MP_KEY_MP = 0.05            # fine MP DB-key grid (matches db.MP_KEY_UNIT + bv._MP_KEY_MP): the
                            # sizer reads the NEAREST key >= the run MP, so distinct real output
                            # sizes get distinct rows without a coarse bucket merging them

# Working-set model for the free-VRAM step-down: peak GB ~= FIXED + K * batch * out_MP.
# Fit on this project's RTX 3090 7B-fp16 OFFLOAD clean runs (docs 14.1/14.3):
#   1280x960 (1.23 MP) bs17 -> 20.9 GB ; 1080p (1.556 MP) bs5 -> 17.1 GB.
# => FIXED 14.85, K 0.289. Decode-dominated, so it is used for 3B too as a conservative
# default (3B shares the same VAE; its ceiling was model-independent at the wall). This
# is a STARTING estimate off-3090; learning corrects it per card.
_WS_FIXED_GB = 14.85
_WS_PER_BATCH_MP = 0.289

# Seed batch per (VRAM tier GB, output-MP ceiling) -> batch. Picked by the largest tier
# <= the card total, then the first MP ceiling >= the output MP. 24 GB is MEASURED / the
# user's decision; every other tier is a CONSERVATIVE guess (biased low) that the
# benchmark suite + learning raise. overlap is derived (auto_overlap), not stored.
_SEED_BY_TIER = {
    12: [(1.35, 5),  (99.0, 5)],
    16: [(1.35, 9),  (1.75, 5),  (99.0, 5)],
    24: [(1.35, 17), (1.75, 5),  (99.0, 5)],          # <-- measured anchor (docs 14.1/14.3)
    32: [(1.35, 21), (1.75, 13), (2.60, 9),  (99.0, 5)],
    48: [(1.35, 33), (1.75, 21), (2.60, 13), (99.0, 9)],
    80: [(1.35, 33), (1.75, 33), (2.60, 25), (99.0, 17)],
}
# Assumed FREE VRAM per tier when the seed was set (desktop overhead baked in). Used only
# to detect "free is unusually low" -- the step-down then trims the seed to fit live free.
_TIER_ASSUMED_FREE = {12: 10.0, 16: 13.5, 24: 21.0, 32: 29.0, 48: 45.0, 80: 76.0}


def to_4n1(x):
    """Round DOWN to the nearest 4n+1 (>=1). SeedVR2's causal temporal VAE wants a 4k+1
    window; normalise so batch/stride math stays predictable (matches the engine)."""
    n = max(1, int(x))
    return ((n - 1) // 4) * 4 + 1


def mp_bucket(mp):
    """Stable integer DB key for a per-frame output-MP value (fine _MP_KEY_MP grid),
    identical to batch_video_upscale._mp_bucket + db.MP_KEY_UNIT so keys line up."""
    return int(round(max(0.0, float(mp or 0)) / _MP_KEY_MP))


def auto_overlap(batch):
    """Frames blended between batches to hide the seam. A QUALITY floor with a cost cap:
    never below MIN_OVERLAP (6), grows ~batch/6, but capped at MAX_OVERLAP (15) since the
    seam is a local transition that a larger window doesn't widen, and clamped below batch.
    Matches pod/worker._auto_overlap."""
    return min(max(1, batch - 1), MAX_OVERLAP, max(MIN_OVERLAP, round(batch / 6)))


def model_tag(model):
    """Family tag for the learned-store key + seed selection: 3b_fp16 / 3b / 7b. Unknown
    models map to 7b (the heavier profile), so an unknown card errs conservative."""
    m = (model or "").lower()
    if "3b" in m:
        return "3b_fp16" if ("fp16" in m and "gguf" not in m) else "3b"
    return "7b"


def vram_tier(total_gb):
    """The largest seed tier <= the card's total VRAM (so a 24 GB card uses the 24 tier,
    a 20 GB card falls to 16). None if total is unknown."""
    if not total_gb or total_gb <= 0:
        return None
    tiers = sorted(_SEED_BY_TIER)
    pick = tiers[0]
    for t in tiers:
        if total_gb + 0.5 >= t:                       # +0.5: 23.6 GB reported -> 24 tier
            pick = t
    return pick


def free_vram_gb(prefer_smi=False):
    """(free_gb, total_gb) for GPU 0, or (None, None). Prefers torch.cuda.mem_get_info
    (reflects the live desktop overhead), then nvidia-smi, then gives up. Best-effort.

    `prefer_smi=True` skips torch and reads nvidia-smi directly -- used by the subprocess
    execution mode so the parent never inits a CUDA context (which would hold VRAM the
    child needs)."""
    if not prefer_smi:
        try:
            import torch
            if torch.cuda.is_available():
                free_b, total_b = torch.cuda.mem_get_info()
                return free_b / (1024 ** 3), total_b / (1024 ** 3)
        except Exception:                            # noqa: BLE001
            pass
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10)
        free_mib, total_mib = (float(x) for x in out.stdout.strip().splitlines()[0].split(","))
        return free_mib / 1024.0, total_mib / 1024.0
    except Exception:                                # noqa: BLE001
        return None, None


def _predicted_peak_gb(batch, mp):
    """Estimated peak VRAM (GB) for a batch at `mp` output megapixels (3090-calibrated,
    a starting estimate off that card)."""
    return _WS_FIXED_GB + _WS_PER_BATCH_MP * batch * mp


# Fraction of TOTAL VRAM the benchmark's starting batch should be predicted to use. Kept
# well under 1.0 so there is headroom to climb to the real wall, AND because the working-set
# model above is 3090-OFFLOAD-calibrated and OVER-predicts on a big resident card (a PRO 6000
# at 0.39 MP really used ~42 GB at batch 513, the model predicts ~72), so this predicted
# fraction maps to an even smaller real fraction: the floor is conservative by construction.
FLOOR_BUDGET_FRAC = 0.55


def vram_floor_batch(total_gb, mp, budget_frac=FLOOR_BUDGET_FRAC):
    """A VRAM-aware STARTING batch for the benchmark's ceiling climb: the largest 4n+1 whose
    predicted peak fits `budget_frac` of the card's total VRAM. On a high-VRAM card this skips
    the pointless low rungs (batch 5..N obviously fit), so the sweep opens near the interesting
    region instead of crawling up from 5. It is deliberately CONSERVATIVE (see FLOOR_BUDGET_FRAC):
    the geometric climb reaches the true wall from here in a few doublings, so the floor's exact
    value barely matters; it only has to avoid absurdly small starts. Falls back to BATCH_FLOOR
    when VRAM/MP is unknown."""
    if not total_gb or total_gb <= 0 or not mp or mp <= 0:
        return BATCH_FLOOR
    budget = float(total_gb) * float(budget_frac)
    if budget <= _WS_FIXED_GB:                        # not even the fixed working set fits
        return BATCH_FLOOR
    batch = (budget - _WS_FIXED_GB) / (_WS_PER_BATCH_MP * mp)
    return max(BATCH_FLOOR, to_4n1(batch))


def _seed_batch(tier, mp):
    """Seed batch for a (tier, output-MP) from the anchored table. Conservative default 5
    if the tier is unknown."""
    rows = _SEED_BY_TIER.get(tier)
    if not rows:
        return BATCH_FLOOR
    for mp_ceiling, batch in rows:
        if mp <= mp_ceiling:
            return batch
    return rows[-1][1]


def _fit_to_free(batch, mp, free_gb):
    """Step a batch DOWN (never up) until its predicted peak fits live free VRAM. This is
    the safety net for a busy desktop / a smaller-than-assumed free pool."""
    b = to_4n1(batch)
    while b > BATCH_FLOOR and _predicted_peak_gb(b, mp) > free_gb:
        b = to_4n1(b - 1)
    return b


def pick(model, out_w, out_h, conn=None, gpu_id=None, free_gb=None, total_gb=None):
    """Return (batch, overlap): the largest window predicted to fit on the FIRST try for
    this (model, output size, card). Precedence: a LEARNED value (per gpu|model + MP
    bucket) if present, else the tier SEED; then a live free-VRAM step-DOWN. `free_gb` /
    `total_gb` may be injected (tests / a cached probe); otherwise they are read live.

    Never raises; on any uncertainty it errs low (a smaller window runs, a too-big one
    OOMs). Overlap is the quality-floored auto value for the chosen batch."""
    mp = (float(out_w) * float(out_h) / 1_000_000.0) if (out_w and out_h) else 0.0
    if mp <= 0:
        return BATCH_FLOOR, auto_overlap(BATCH_FLOOR)

    if free_gb is None or total_gb is None:
        f, t = free_vram_gb()
        free_gb = free_gb if free_gb is not None else f
        total_gb = total_gb if total_gb is not None else t

    tag = model_tag(model)
    tier = vram_tier(total_gb)

    batch = None
    is_learned = False
    if conn is not None and gpu_id and _db is not None:
        try:
            # NEAREST recorded key >= this run's MP: a ceiling measured at a LARGER output is a
            # safe bound here (decode memory rises with MP), so each benchmarked size serves
            # nearby smaller runs without a coarse bucket merging distinct sizes.
            learned = _db.get_learned_batch_ge(conn, f"{gpu_id}|{tag}", mp_bucket(mp))
            if learned and learned > 0:
                batch = int(learned)
                is_learned = True
        except Exception:                            # noqa: BLE001 (fail-safe)
            batch = None
    if batch is None:
        batch = _seed_batch(tier, mp)

    if free_gb:
        batch = _fit_to_free(batch, mp, free_gb)

    # The BATCH_CAP is a safety ceiling for the SEED path (an unproven guess must stay
    # conservative). A LEARNED value is a real per-card measurement (from a run or the
    # benchmark suite), so it may legitimately exceed the cap -- honour it.
    batch = to_4n1(batch)
    if not is_learned:
        batch = min(BATCH_CAP, batch)
    batch = max(BATCH_FLOOR, batch)
    return batch, auto_overlap(batch)


def record_result(conn, gpu_id, model, out_w, out_h, batch, ok=True):
    """Persist the outcome of a segment so the sizer self-calibrates: on success, record
    `batch` as the known-good window for (gpu|model, MP bucket); the learned value then
    supersedes the seed next time. On a failure (an OOM that recovered to a smaller
    batch), pass the RECOVERED batch as `batch` with ok=True -- it is the new known-good.
    Best-effort; a missing db/args is a silent no-op."""
    if not ok or conn is None or not gpu_id or _db is None or not batch or batch <= 0:
        return
    mp = (float(out_w) * float(out_h) / 1_000_000.0) if (out_w and out_h) else 0.0
    if mp <= 0:
        return
    try:
        _db.put_learned_batch(conn, f"{gpu_id}|{model_tag(model)}", mp_bucket(mp), int(batch))
    except Exception:                                # noqa: BLE001 (fail-safe)
        pass
