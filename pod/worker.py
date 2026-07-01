#!/usr/bin/env python3
"""
worker.py — the resident upscale worker on the pod (remote upscaling #1, Phase 3).

Loads the SAME UpscaleEngine the local app uses **once** (models from the network
volume), then serves **one image per HTTP request** over localhost. The local
side reaches it through an `ssh -L` tunnel, so the worker is never exposed
publicly. Each request touches a heartbeat file so pod/deadman.py can tell the
pod is still doing work.

Why HTTP + single-threaded: the request/response shape makes streaming one image
up and one result down trivial (raw bytes in the body), and a single-threaded
server serialises GPU work (one image at a time) for free.

    python worker.py --repo-dir /workspace/seedvr2 \
                     --model-dir /workspace/models/seedvr2 \
                     --settings /root/worker_settings.json \
                     --port 8200 --heartbeat /tmp/upscale_heartbeat

Endpoints:
    GET  /health                      -> {"status":"ok","device":"...","count":N}
    POST /upscale?resolution=R&ext=.jpg   body = source bytes
                                      -> upscaled bytes (X-Process-Time header, s)
    POST /orient                      body = a small thumbnail of the source
                                      -> {"degrees":D,"confidence":C}  (auto-straighten,
                                         remote-pod #1 option B — the CNN runs here so
                                         the local side needs no torch; it sends only a
                                         ~512px thumbnail, then rotates + uploads locally)

  Video Upscaler (#2, phase 3) — a segment upscale takes minutes to hours, far
  too long for one synchronous request, so it is async (submit / poll / fetch):
    POST /video/submit?resolution=&batch_size=&chunk_size=&temporal_overlap=
                       &seed=&video_backend=&use_10bit=&ext=.mkv
                                      body = ONE segment's bytes
                                      -> {"id":..., "total_frames":N}  (starts work)
    GET  /video/status?id=ID          -> {state, total_frames, frames_written,
                                          output_bytes, elapsed, seconds, error}
    GET  /video/fetch?id=ID           -> upscaled mp4 bytes (409 until done)
  The heartbeat is refreshed on every line of pipeline progress (a tee over the
  SeedVR2 tqdm output), so a long segment that is genuinely working stays alive
  while a HUNG GPU goes stale and the dead-man's switch can reclaim the pod.
"""
import os
import re
import sys
import json
import time
import uuid
import shutil
import argparse
import tempfile
import threading
import contextlib
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

_ENGINE = None
_HEARTBEAT = None
_COUNT = 0
_VERSION = ""           # worker-code version (hash of the pushed .py files); a
                        # reused pod reloads the worker when this stops matching
_MODE = "full"          # "full" (SeedVR2 + /upscale), "tag" (/orient only), or
                        # "video" (SeedVR2 + /video/*); a reused pod also reloads
                        # when the requested mode differs
# GPU work (upscale + the orient CNN) is serialised through this lock so a
# /telemetry or /health request can still be answered WHILE an upscale runs
# (the server is multi-threaded; those two endpoints never take the lock).
_GPU_LOCK = threading.Lock()
_PREV_CPU = None        # (idle, total) jiffies from the last /telemetry sample

# The one active video job (segments are streamed one at a time, so the worker
# never runs two at once). Guarded by _VIDEO_LOCK. None until the first submit.
_VIDEO_JOB = None
_VIDEO_LOCK = threading.Lock()


def _touch(path):
    if not path:
        return
    try:
        with open(path, "a"):
            os.utime(path, None)
    except OSError:
        pass


def _log(msg):
    print(f"[worker] {msg}", flush=True)


# ── pod telemetry (remote #1, Feature #4) ───────────────────────────────────
# Linux equivalents of the app's system_telemetry.py (which is Windows-only):
# CPU from /proc/stat (busy fraction between calls), RAM from /proc/meminfo,
# GPU from nvidia-smi. All best-effort, fail safe to None — same sample shape
# the GUI's TelemetryRow already renders.

def _sample_cpu():
    """Busy % since the previous call (delta of /proc/stat jiffies)."""
    global _PREV_CPU
    try:
        with open("/proc/stat") as f:
            nums = [int(x) for x in f.readline().split()[1:]]
        idle = nums[3] + (nums[4] if len(nums) > 4 else 0)   # idle + iowait
        total = sum(nums)
        prev, _PREV_CPU = _PREV_CPU, (idle, total)
        if prev is None:
            return None
        di, dt = idle - prev[0], total - prev[1]
        if dt <= 0:
            return None
        return max(0.0, min(100.0, 100.0 * (1.0 - di / dt)))
    except Exception:
        return None


def _sample_ram():
    """(used_mb, total_mb) for THIS pod. Prefer the cgroup memory limit/usage —
    the container's own slice — so the % means a real fraction of the pod's RAM
    and matches the RunPod dashboard; fall back to host-wide /proc/meminfo only
    when no cgroup limit is set.

    Why: /proc/meminfo is NOT container-namespaced — inside a pod it reports the
    physical HOST's RAM and host-wide usage, over-reporting both total and used.
    cgroup v2 is memory.current/memory.max; v1 is
    memory.usage_in_bytes/memory.limit_in_bytes (a huge sentinel == unlimited).
    'used' is the working set (usage minus reclaimable page cache, from
    memory.stat) — the basis container dashboards report."""
    MB = 1024 * 1024
    for usage_p, limit_p, stat_p in (
            ("/sys/fs/cgroup/memory.current",
             "/sys/fs/cgroup/memory.max",
             "/sys/fs/cgroup/memory.stat"),
            ("/sys/fs/cgroup/memory/memory.usage_in_bytes",
             "/sys/fs/cgroup/memory/memory.limit_in_bytes",
             "/sys/fs/cgroup/memory/memory.stat")):
        try:
            raw = open(limit_p).read().strip()
            if raw == "max":                         # v2: no limit set
                continue
            limit = int(raw)
            if limit <= 0 or limit >= (1 << 62):     # v1 unlimited sentinel
                continue
            usage = int(open(usage_p).read().strip())
        except (OSError, ValueError):
            continue
        stat = {}
        try:
            for line in open(stat_p):
                p = line.split()
                if len(p) >= 2:
                    stat[p[0]] = p[1]
        except OSError:
            pass
        inactive = int(stat.get("total_inactive_file", stat.get("inactive_file", 0)))
        used = max(0, usage - inactive)
        return used // MB, limit // MB
    # No cgroup limit — fall back to host-wide /proc/meminfo.
    try:
        info = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, _, rest = line.partition(":")
                info[k] = int(rest.strip().split()[0])       # kB
        total = info.get("MemTotal", 0) // 1024
        avail = info.get("MemAvailable", info.get("MemFree", 0)) // 1024
        if total <= 0:
            return None
        return total - avail, total
    except Exception:
        return None


def _sample_gpu():
    """(vram_used_mb, vram_total_mb, temp_c) from nvidia-smi."""
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=memory.used,memory.total,temperature.gpu",
             "--format=csv,noheader,nounits"], text=True, timeout=8)
        used, total, temp = [p.strip() for p in out.strip().splitlines()[0].split(",")]
        return int(float(used)), int(float(total)), int(float(temp))
    except Exception:
        return None


def _sample_telemetry():
    ram, gpu = _sample_ram(), _sample_gpu()
    return {
        "cpu":          _sample_cpu(),
        "ram_used_mb":  ram[0] if ram else None,
        "ram_total_mb": ram[1] if ram else None,
        "gpu_used_mb":  gpu[0] if gpu else None,
        "gpu_total_mb": gpu[1] if gpu else None,
        "gpu_temp_c":   gpu[2] if gpu else None,
    }


# ── video jobs (Video Upscaler #2, phase 3) ─────────────────────────────────

def _count_video_frames(path):
    """Frame count of a segment via OpenCV (already a SeedVR2 dependency). Best
    effort: 0 on failure (status then reports total_frames=0, never blocks work)."""
    try:
        import cv2
        cap = cv2.VideoCapture(path)
        n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        cap.release()
        return max(0, n)
    except Exception:
        return 0


def _video_dims(path):
    """(width, height) of a segment via OpenCV. (0, 0) on failure."""
    try:
        import cv2
        cap = cv2.VideoCapture(path)
        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        cap.release()
        return max(0, w), max(0, h)
    except Exception:
        return 0, 0


def _vram_total_gb():
    """Total VRAM (GB) of GPU 0, or 0.0 if it can't be read."""
    try:
        import torch
        return torch.cuda.get_device_properties(0).total_memory / (1024 ** 3)
    except Exception:
        return 0.0


def _to_4n1(x):
    """Largest valid SeedVR2 batch (4n+1: 1,5,9,…) that is <= x."""
    if x < 5:
        return 1
    n = (int(x) - 1) // 4
    return 4 * n + 1


def _to_4n1_ceil(x):
    """Smallest valid SeedVR2 batch (4n+1: 1,5,9,…) that is >= x."""
    n = max(0, (int(x) + 2) // 4)        # ceil((x-1)/4)
    return 4 * n + 1


def _fit_batch_to_frames(batch, frames, overlap):
    """Snap a requested batch to the segment's actual frame count so we never waste
    compute or leave a tiny ragged final batch. Two cases:

      * batch covers the whole segment (batch >= frames): collapse to ONE batch — the
        nearest 4n+1 >= frames, with overlap 0 (a single pass has no seam to blend, and
        no neighbour to overlap). This is the "whole shot in one batch" the user wants.
      * otherwise: split the segment into N EVEN batches, where N is the fewest the
        requested batch allows (N = ceil(frames / stride)); the new batch is the even
        stride + overlap, rounded UP to a 4n+1 so N batches still cover the segment.
        e.g. 1205 frames @ requested 501 -> 3 even batches (~409), not 501+501+203.

    Only ever shrinks the batch, so VRAM is never increased. Returns (batch, overlap)."""
    if batch <= 0 or frames <= 0:
        return batch, overlap            # auto / unknown: leave for the caller to size
    if batch >= frames:
        return _to_4n1_ceil(frames), 0   # whole segment in a single batch
    step = max(1, batch - max(0, overlap))
    n = max(1, -(-frames // step))       # ceil: fewest batches at this requested window
    even_step = -(-frames // n)          # ceil: even stride across N batches
    return _to_4n1_ceil(even_step + max(0, overlap)), overlap


# Auto-tuner constants. WORKING_SET ~= FIXED + out_megapixels * (A + B*batch + C*batch^2).
# FIXED is the resolution-independent floor (resident weights + buffers). The batch cost is
# SUPER-LINEAR: temporal attention grows ~O(batch^2), so a plain linear fit (used briefly)
# under-predicted badly at large windows — it put 7B 1440p bs437 at ~119 GB when the real
# run hit ~171 GB (96% of a B200) and THRASHED the allocator. The quadratic C term fixes that.
#
# The coefficients differ a LOT by MODEL family — 7B fp16 carries far heavier weights and
# activations than the quantised 3B-Q8 — so they are kept as per-model PROFILES and selected
# by dit_model. Using the 7B numbers for 3B over-predicted ~1.85x and crushed the batch to the
# floor (an A100 1440p 3B run clamped to bs5); using none let 3B THRASH (bs153 1440p hit ~79 GB
# / 99% on an 80 GB A100). A per-model profile is the fix.
#   7B: fit on four B200 7B-fp16 anchors (working set, bs437 = the 96% reserved/thrash point):
#       4K bs33 = 152.9,  1440p bs33 = 77.0,  1440p bs121 = 86.1,  1440p bs437 = 171.0 GB.
#       The quadratic C term is REAL for 7B (the bs437 thrash needed it).
#   3B: fit on three clean 3B-Q8 anchors — 1440p portrait 1.17 MP bs153 = 33.0 GB, landscape
#       3.69 MP bs9 = 41.0 GB, and 3.69 MP bs117 = 78.1 GB (all peak working set). These fit a
#       LINEAR batch model almost exactly (C = 0): 3B's batch cost is linear in this range, so
#       borrowing 7B's quadratic (an earlier guess) UNDER-predicted — it put bs117 at ~67 GB
#       when it really hit 78.1 (98% / thrash on an 80 GB A100). Refit linear. NB: bs117/1440p
#       on 80 GB is ABOVE safe headroom (it thrashed and ran ~60% slower per MP than a healthy
#       run); the clamp now caps an 80 GB card at ~bs73 (~79%). Extrapolation beyond bs~150 is
#       unverified (no data there).
#   3b_fp16: the UNQUANTISED 3B carries ~3 GB more resident weight than the Q8 GGUF (measured
#       3.69 MP bs69 = 68.5 GB vs Q8's 65.5), so it thrashed an 80 GB A100 at a batch the Q8
#       profile cleared. Same shape as "3b" with FIXED bumped +3.0 (6.40 -> 9.40).
# SAFETY 0.80 leaves ~20% free for the allocator to breathe (7B 4K bs33 ran clean at 85%, and
# 3B bs117 thrashed at 98%). The model sizes the AUTO batch AND clamps a too-big explicit pick;
# OOM-recovery backstops any remaining miss.
_VRAM_PROFILES = {       # model family -> (FIXED, A, B, C)
    "7b":      (16.3, 15.98, 0.01096, 0.000111),
    "3b":      (6.40,  8.55, 0.0932,  0.0),
    "3b_fp16": (9.40,  8.55, 0.0932,  0.0),
}
_VRAM_SAFETY = 0.80          # max working-set fraction of the card (allocator thrashes near 1.0)
_BATCH_CAP = 33              # AUTO-path ceiling only: a safe default for unknown content on
                             # any card. Bigger windows add temporal consistency (SeedVR2's
                             # ideal is batch = shot length); a manual override can go far
                             # higher (the GUI offers up to 501), VRAM-clamped to what fits.
_BATCH_FLOOR = 5             # below this a temporal window barely helps


def _vram_profile(model):
    """The (FIXED, A, B, C) VRAM coefficients for a dit_model, by name. 3B splits into the
    unquantised fp16 (heavier weights) and the Q8 GGUF; the 7B family and any UNKNOWN model
    fall back to the heavier 7B profile, which over-estimates safely rather than risking a
    thrash."""
    m = (model or "").lower()
    if "3b" in m:
        if "fp16" in m and "gguf" not in m:     # unquantised 3B (~3 GB heavier than Q8)
            return _VRAM_PROFILES["3b_fp16"]
        return _VRAM_PROFILES["3b"]
    return _VRAM_PROFILES["7b"]


def _ws_gb(mp, batch, model=None):
    """Predicted working set (GB) for an output of `mp` megapixels at `batch` frames, using
    the VRAM profile for `model`."""
    f, a, b, c = _vram_profile(model)
    return f + mp * (a + b * batch + c * batch * batch)


def _max_vram_batch(out_w, out_h, vram_gb, resident, model=None):
    """Largest 4n+1 batch whose predicted working set (for this card + output + MODEL) fits
    the SAFETY budget. Returns a big sentinel when dims/VRAM are unknown (can't measure ->
    don't clamp; OOM-recovery backstops). The VRAM ceiling, separate from _BATCH_CAP."""
    mp = (out_w * out_h) / 1_000_000.0
    if mp <= 0 or vram_gb <= 0:
        return 997
    budget = vram_gb * (_VRAM_SAFETY if resident else 0.90)
    best, b = _BATCH_FLOOR, _BATCH_FLOOR
    while b <= 997:
        if _ws_gb(mp, b, model) <= budget:
            best = b
            b += 4
        else:
            break
    return best


def _auto_batch(out_w, out_h, vram_gb, resident, model=None):
    """Pick the largest safe temporal window (4n+1) for an output of out_w x out_h on
    a card with vram_gb: the most the VRAM budget allows, capped at _BATCH_CAP where
    continuity gains flatten. OOM auto-recovery backstops an optimistic guess. Returns a
    safe 13 if dims/VRAM are unknown."""
    if (out_w * out_h) <= 0 or vram_gb <= 0:
        return 13
    return max(_BATCH_FLOOR,
               min(_BATCH_CAP, _max_vram_batch(out_w, out_h, vram_gb, resident, model)))


_MIN_OVERLAP = 6                 # measured: 3 left a visible seam, 6 was undetectable


def _auto_overlap(batch):
    """Frames blended between batches to HIDE the seam. This is a quality floor, not a
    cost knob: too little (3) ruins the result, so never go below _MIN_OVERLAP (6),
    growing ~batch/6 for very large windows, clamped below the batch. With a big batch
    the fixed overlap is cheap (low redundancy); a tiny VRAM-forced batch pays more for
    it, which is the right trade (quality over a hair of cost). Use a big-VRAM card so
    the batch can be large and the overlap is nearly free."""
    return min(batch - 1, max(_MIN_OVERLAP, round(batch / 6)))


_NT_RE = re.compile(r"(\d+)\s*/\s*(\d+)")   # tqdm "n/total" pairs
# DiT/VAE phase lines (all force-printed by SeedVR2 with a leading icon). We TRACE the
# real cadence of these to the job so a run reveals the true progress signal instead of us
# guessing at the per-chunk batch count (the live s/frame + frame bar were built on an
# UNVALIDATED assumption about it). Diagnostic only — fail-safe, bounded, never affects the run.
_PHASE_RE = re.compile(r"(Encoding|Upscaling|Decoding) batch\s+(\d+)\s*/\s*(\d+)")
_PHASE_IDX = {"Encoding": 0, "Upscaling": 1, "Decoding": 2}
# Within a streaming chunk the pipeline runs encode -> upscale -> decode. At 1440p+ the
# VAE encode/decode DOMINATE the wall-time (the DiT upscale is a few seconds), so the
# progress bar must advance through E and D, not only U (mining U alone froze the bar for
# minutes each chunk then jumped). Each phase gets a (start, width) slice of the chunk's
# [0,1]; weighted toward E and D to roughly track where the time actually goes.
_PHASE_SPAN = {0: (0.00, 0.45), 1: (0.45, 0.10), 2: (0.55, 0.45)}


class _HeartbeatTee:
    """A stdout/stderr proxy that forwards to the real stream AND touches the
    heartbeat on every non-blank write. Wrapping the SeedVR2 pipeline's tqdm
    progress in this turns each per-batch redraw into a liveness signal, so the
    dead-man's switch sees a working segment as alive yet a hung GPU (no progress
    output) as idle and reclaims the pod.

    It also opportunistically extracts WITHIN-SEGMENT frame progress for the GUI's
    progress bar (15.8): any tqdm `n/total` whose total matches the segment's known
    frame count is taken as frames_processed. CONSERVATIVE on purpose — if the
    pipeline's bar counts something else (chunks/batches), nothing is reported and
    the GUI falls back to a time-based bar. Purely additive and fail-safe: a parse
    error can never affect the upscale."""

    def __init__(self, real, job):
        self._real = real
        self._job = job
        self._last_phase_idx = -1  # last phase seen (E=0/U=1/D=2); a DROP means a new chunk
        self._chunks_done = 0      # streaming chunks fully finished

    def write(self, s):
        try:
            self._real.write(s)
        except Exception:
            pass
        if s and s.strip():
            _touch(_HEARTBEAT)
            self._job["last_output_t"] = time.time()
            self._scan_progress(s)
        return len(s) if s else 0

    def _scan_progress(self, s):
        # Diagnostic trace (fail-safe, bounded): record the real cadence of the phase lines
        # so a run shows us the true progress signal. Captures the first time each distinct
        # "<phase> N/M" appears plus a running count, surfaced via /video/status.
        try:
            pm = _PHASE_RE.search(s)
            if pm:
                trace = self._job.setdefault("phase_trace", [])
                tag = f"{pm.group(1)[0]}{pm.group(2)}/{pm.group(3)}"   # e.g. U3/4, D1/2
                if len(trace) < 80 and (not trace or trace[-1] != tag):
                    trace.append(tag)
                self._job["phase_count"] = self._job.get("phase_count", 0) + 1
        except Exception:
            pass
        total = self._job.get("total_frames") or 0
        if total <= 0:
            return
        # Mine ALL three phase lines (encode/upscale/decode), each force-printed as
        # "<phase> batch N/M" (M = latent batches in THIS chunk). Mining only the upscale
        # phase left the bar blind through the long encode/decode (most of the wall-time at
        # 1440p+), so it froze then jumped. A chunk runs E -> U -> D; a new chunk begins when
        # the phase index DROPS (D/U -> E). Progress = (chunks_done + within-chunk fraction)
        # / total_chunks, weighted by _PHASE_SPAN and kept MONOTONIC. Fail-safe.
        try:
            pm = _PHASE_RE.search(s)
            if not pm:
                return
            phase, n, mt = pm.group(1), int(pm.group(2)), int(pm.group(3))
            pidx = _PHASE_IDX.get(phase)
            if pidx is None or mt <= 0:
                return
            if pidx < self._last_phase_idx:      # wrapped D/U -> E: the next chunk started
                self._chunks_done += 1
            self._last_phase_idx = pidx
            start, span = _PHASE_SPAN[pidx]
            within = start + span * (min(n, mt) / mt)
            tc = int(self._job.get("total_chunks") or 1)
            frac = min(1.0, (self._chunks_done + within) / tc)
            prev = self._job.get("frames_processed") or 0
            self._job["frames_processed"] = min(total, max(prev, int(round(frac * total))))
            # Live s/frame = a RUNNING AVERAGE (elapsed / frames processed so far): appears
            # from the first report, always defined, converges to the true final rate (incl.
            # the one-time compile warm-up, the honest cost the user is watching).
            fp = self._job["frames_processed"]
            started = self._job.get("started")
            if fp > 0 and started:
                el = time.time() - started
                if el > 0:
                    self._job["live_spf"] = round(el / fp, 2)
        except Exception:
            pass

    def flush(self):
        try:
            self._real.flush()
        except Exception:
            pass


def _is_oom(exc):
    """True if `exc` is a CUDA out-of-memory error (the case OOM-recovery retries)."""
    s = str(exc).lower()
    return ("out of memory" in s or "cuda oom" in s
            or exc.__class__.__name__ == "OutOfMemoryError")


def _empty_cuda_cache():
    try:
        import torch
        torch.cuda.empty_cache()
        torch.cuda.synchronize()
    except Exception:
        pass


def _resolve_auto_params(job, params):
    """Fill in batch_size / temporal_overlap. AUTO batch (<=0) is sized from the
    segment's OUTPUT size and this card's real VRAM. An EXPLICIT (Advanced override)
    batch is honoured but VRAM-CLAMPED: a pick whose predicted working set exceeds the
    SAFETY budget is reduced to the largest that fits, so a too-big manual value can't
    drive the card to the thrash zone (what bs437 did). Overlap is scaled to the batch."""
    in_w, in_h = _video_dims(job["input"])
    short = min(in_w, in_h) if in_w and in_h else 0
    scale = (params["resolution"] / short) if short else 1.0
    out_w, out_h = round(in_w * scale), round(in_h * scale)
    resident = bool(getattr(_ENGINE, "resident", False))
    model = getattr(getattr(_ENGINE, "args", None), "dit_model", "") or ""
    vram = _vram_total_gb()
    vmax = _max_vram_batch(out_w, out_h, vram, resident, model)  # VRAM ceiling for this card+output+model
    if params["batch_size"] <= 0:
        params["batch_size"] = _auto_batch(out_w, out_h, vram, resident, model)
        job["auto_batch"] = True
        _log(f"auto batch -> {params['batch_size']} (out {out_w}x{out_h}, "
             f"vram {vram:.0f}GB, vmax {vmax}, resident={resident}, model={model})")
    elif params["batch_size"] > vmax:
        _log(f"batch {params['batch_size']} exceeds the VRAM budget (out {out_w}x{out_h}, "
             f"vram {vram:.0f}GB) -> clamped to {vmax} (~{_ws_gb((out_w*out_h)/1e6, vmax, model):.0f}GB "
             f"working set); avoids the near-full allocator thrash.")
        params["batch_size"] = vmax
    if params["temporal_overlap"] < 0:
        params["temporal_overlap"] = _auto_overlap(params["batch_size"])
        _log(f"auto temporal_overlap -> {params['temporal_overlap']}")
    # Overlap MUST stay below the batch: SeedVR2 silently resets overlap>=batch to 0,
    # and (worse) an overlap >= batch corrupts the frame-fit's stride math below
    # (step collapses to 1, inflating the window). A VRAM-forced small batch with a
    # bigger explicit overlap is exactly how that happened. Clamp BEFORE the fit.
    if params["temporal_overlap"] >= params["batch_size"]:
        params["temporal_overlap"] = max(0, params["batch_size"] - 1)
        _log(f"overlap >= batch -> clamped to {params['temporal_overlap']}")
    # Fit the batch to the segment's real frame count: a window >= the clip collapses to
    # one seam-free pass; a window the clip exceeds is evened out so there's no tiny
    # ragged final batch. Only ever shrinks the batch (VRAM never rises). Applies to both
    # auto and explicit (Advanced override) batches.
    frames = int(job.get("total_frames") or 0)
    fb, fo = _fit_batch_to_frames(params["batch_size"], frames, params["temporal_overlap"])
    if (fb, fo) != (params["batch_size"], params["temporal_overlap"]):
        _log(f"fit batch to {frames} frames: batch {params['batch_size']} -> {fb}, "
             f"overlap {params['temporal_overlap']} -> {fo}")
        params["batch_size"], params["temporal_overlap"] = fb, fo
    if params["chunk_size"] <= 0:
        # Size streaming chunks (RAM-bound; MUST be > 0 so frames stream out). The whole
        # clip in one batch -> one chunk; a big window -> one batch per chunk (so the even
        # split holds, since each chunk prepends `overlap` context); otherwise the default
        # ~90-frame chunks (several small batches per chunk, no quality effect).
        b, o = params["batch_size"], params["temporal_overlap"]
        if frames and b >= frames:
            params["chunk_size"] = b
        elif b > 89:
            params["chunk_size"] = max(1, b - o)
        else:
            params["chunk_size"] = b * max(1, round(90 / b))
    # SeedVR2 silently resets overlap >= batch to 0; clamp so it never disables.
    if params["temporal_overlap"] >= params["batch_size"]:
        params["temporal_overlap"] = max(0, params["batch_size"] - 1)
    job["resolved_batch"] = params["batch_size"]
    job["resolved_overlap"] = params["temporal_overlap"]
    job["resolved_chunk"] = params["chunk_size"]
    if frames:
        # Real model passes, NOT chunks: a window advances by stride = batch - overlap
        # new frames per pass (a window >= the clip is a single pass). The old count
        # (frames / chunk_size) reported chunks, badly undercounting a small batch.
        b, o = params["batch_size"], params["temporal_overlap"]
        stride = max(1, b - o)
        nb = 1 if b >= frames else max(1, -(-(frames - o) // stride))
        _log(f"resolved window: batch {b}, overlap {o}, chunk {params['chunk_size']} -> "
             f"~{nb} batch(es) over {frames} frames")


def _run_video_job(job, params):
    """Upscale one segment to job['output'] (its own thread). GPU work is
    serialised through _GPU_LOCK; progress is teed to the heartbeat. Fail-safe:
    any error lands in job['error'] and the worker keeps serving.

    Auto-tunes batch/overlap when asked (AUTO sentinels), and on a CUDA OOM RETRIES
    the segment with a smaller window (down to the floor) so an optimistic guess
    self-corrects on the pod instead of failing the whole run."""
    global _COUNT
    job["state"] = "running"
    _touch(_HEARTBEAT)
    tee = _HeartbeatTee(sys.stdout, job)
    _resolve_auto_params(job, params)
    # How many streaming chunks the engine will read (ceil of frames / chunk). The
    # progress parser needs this because the per-chunk DiT bar restarts at each chunk,
    # so a frame count is only monotonic when scoped to (this chunk of total_chunks).
    ch = int(params.get("chunk_size") or 0)
    tf = int(job.get("total_frames") or 0)
    job["total_chunks"] = ((tf + ch - 1) // ch) if (ch > 0 and tf > ch) else 1
    try:
        with contextlib.redirect_stdout(tee), contextlib.redirect_stderr(tee):
            with _GPU_LOCK:
                n = dt = None
                try:                              # measure the real working set
                    import torch
                    torch.cuda.reset_peak_memory_stats()
                except Exception:
                    pass
                while True:
                    t0 = time.time()
                    try:
                        n = _ENGINE.process_video(
                            job["input"], job["output"],
                            resolution=params["resolution"],
                            batch_size=params["batch_size"],
                            chunk_size=params["chunk_size"],
                            temporal_overlap=params["temporal_overlap"],
                            seed=params["seed"],
                            video_backend=params["video_backend"],
                            use_10bit=params["use_10bit"])
                        dt = time.time() - t0
                        break
                    except Exception as exc:           # noqa: BLE001
                        if not _is_oom(exc) or params["batch_size"] <= _BATCH_FLOOR:
                            raise
                        smaller = _to_4n1(params["batch_size"] - 1)
                        _empty_cuda_cache()
                        _log(f"video job {job['id'][:8]} OOM at batch "
                             f"{params['batch_size']}; retrying at {smaller}")
                        params["batch_size"] = smaller
                        params["temporal_overlap"] = min(
                            params["temporal_overlap"], max(0, smaller - 1))
                        job["resolved_batch"] = smaller
                        job["resolved_overlap"] = params["temporal_overlap"]
        try:
            out_bytes = os.path.getsize(job["output"])
        except OSError:
            out_bytes = 0
        try:                                  # ground-truth VRAM: working set vs the
            import torch                       # caching-allocator pool nvidia-smi shows
            gb = 1024 ** 3
            job["peak_alloc_gb"] = round(torch.cuda.max_memory_allocated() / gb, 1)
            job["peak_reserved_gb"] = round(torch.cuda.max_memory_reserved() / gb, 1)
        except Exception:
            pass
        job["frames_written"] = n
        job["output_bytes"] = out_bytes
        job["seconds"] = dt
        job["state"] = "done"
        _COUNT += 1
        _log(f"video job {job['id'][:8]} done: {n} frames in {dt:.1f}s "
             f"-> {out_bytes}B (res={params['resolution']} bs={params['batch_size']} "
             f"chunk={params['chunk_size']} overlap={params['temporal_overlap']} "
             f"peakVRAM={job.get('peak_alloc_gb')}GB alloc/"
             f"{job.get('peak_reserved_gb')}GB reserved)")
    except Exception as exc:                 # noqa: BLE001 — report, keep serving
        job["state"] = "error"
        job["error"] = str(exc)
        _log(f"video job {job['id'][:8]} ERROR: {exc}")
    finally:
        _touch(_HEARTBEAT)


def _cleanup_job(job):
    """Remove a finished job's temp dir (input + output). Best effort."""
    if not job:
        return
    d = job.get("dir")
    if d and os.path.isdir(d):
        shutil.rmtree(d, ignore_errors=True)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):          # quiet the default per-request logging
        pass

    def _send(self, code, body=b"", ctype="application/octet-stream", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/health":
            body = json.dumps({"status": "ok",
                               "device": getattr(_ENGINE, "device_name", "?"),
                               "resident": bool(getattr(_ENGINE, "resident", False)),
                               "version": _VERSION,
                               "mode": _MODE,
                               "count": _COUNT}).encode()
            self._send(200, body, "application/json")
        elif path == "/telemetry":
            # Lock-free so it answers even while an upscale holds _GPU_LOCK.
            body = json.dumps(_sample_telemetry()).encode()
            self._send(200, body, "application/json")
        elif path == "/video/status":
            self._handle_video_status(parse_qs(urlparse(self.path).query))
        elif path == "/video/fetch":
            self._handle_video_fetch(parse_qs(urlparse(self.path).query))
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/orient":
            self._handle_orient()
            return
        if parsed.path == "/video/submit":
            self._handle_video_submit(parsed)
            return
        if parsed.path != "/upscale":
            self._send(404, b"not found", "text/plain")
            return
        self._handle_upscale(parsed)

    def _handle_orient(self):
        """Detect orientation (the auto-straighten CNN) on a thumbnail the local
        side sent. Fail safe: any error reports 'upright' (degrees 0) so the
        client leaves the image un-rotated."""
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0:
            self._send(400, b"empty body", "text/plain")
            return
        data = self.rfile.read(length)
        _touch(_HEARTBEAT)
        tmpdir = tempfile.mkdtemp(prefix="ori_")
        src = os.path.join(tmpdir, "in.jpg")
        try:
            with open(src, "wb") as f:
                f.write(data)
            import orientation
            with _GPU_LOCK:
                deg, conf = orientation.analyse(src)
            body = json.dumps({"degrees": int(deg), "confidence": float(conf)}).encode()
            self._send(200, body, "application/json")
        except Exception as exc:                 # noqa: BLE001 — report, fail safe
            _log(f"orient error: {exc}")
            body = json.dumps({"degrees": 0, "confidence": 0.0,
                               "error": str(exc)}).encode()
            self._send(200, body, "application/json")
        finally:
            try:
                os.remove(src)
            except OSError:
                pass
            try:
                os.rmdir(tmpdir)
            except OSError:
                pass

    def _handle_upscale(self, parsed):
        global _COUNT
        if _ENGINE is None:
            # Tag mode: the SeedVR2 engine wasn't loaded (this pod serves only
            # /orient for remote Tag & Rename, leaving VRAM for Ollama).
            self._send(503, b"upscale engine not loaded (worker is in tag mode)",
                       "text/plain")
            return
        q = parse_qs(parsed.query)
        try:
            resolution = int(q.get("resolution", ["1080"])[0])
        except ValueError:
            self._send(400, b"bad resolution", "text/plain")
            return
        ext = q.get("ext", [".jpg"])[0]
        if not ext.startswith("."):
            ext = "." + ext

        length = int(self.headers.get("Content-Length", 0))
        if length <= 0:
            self._send(400, b"empty body", "text/plain")
            return
        data = self.rfile.read(length)

        _touch(_HEARTBEAT)                       # work arriving = still alive
        tmpdir = tempfile.mkdtemp(prefix="wrk_")
        src = os.path.join(tmpdir, "in" + ext)
        dst = os.path.join(tmpdir, "out" + ext)
        try:
            with open(src, "wb") as f:
                f.write(data)
            with _GPU_LOCK:
                t0 = time.time()
                _ENGINE.upscale(src, dst, resolution)
                dt = time.time() - t0
            with open(dst, "rb") as f:
                out = f.read()
            _COUNT += 1
            _touch(_HEARTBEAT)
            _log(f"#{_COUNT} upscaled {len(data)}B -> {len(out)}B in {dt:.1f}s "
                 f"(res={resolution})")
            self._send(200, out, _ctype_for(ext), {"X-Process-Time": f"{dt:.3f}"})
        except Exception as exc:                 # noqa: BLE001 — report, keep serving
            _log(f"ERROR upscaling: {exc}")
            self._send(500, str(exc).encode(), "text/plain")
        finally:
            for p in (src, dst):
                try:
                    os.remove(p)
                except OSError:
                    pass
            try:
                os.rmdir(tmpdir)
            except OSError:
                pass


    # ── video (submit / status / fetch) ──────────────────────────────────────

    def _read_body(self):
        """Read the request body (draining the connection even on an early
        rejection, so HTTP keep-alive stays in sync)."""
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length) if length > 0 else b""

    def _handle_video_submit(self, parsed):
        global _VIDEO_JOB
        data = self._read_body()           # always drain first
        if _ENGINE is None:
            self._send(503, b"video engine not loaded (worker not in video mode)",
                       "text/plain")
            return
        if not data:
            self._send(400, b"empty body", "text/plain")
            return
        q = parse_qs(parsed.query)
        try:
            params = {
                "resolution":       int(q.get("resolution", ["1080"])[0]),
                # 0 = AUTO: the worker sizes the batch from the card's VRAM + the
                # output resolution. -1 overlap = AUTO (scaled to the batch).
                "batch_size":       int(q.get("batch_size", ["0"])[0]),
                "chunk_size":       int(q.get("chunk_size", ["0"])[0]),
                "temporal_overlap": int(q.get("temporal_overlap", ["-1"])[0]),
                "seed":             int(q["seed"][0]) if q.get("seed") else None,
                "video_backend":    q.get("video_backend", ["opencv"])[0],
                "use_10bit":        q.get("use_10bit", ["0"])[0] in ("1", "true", "True"),
            }
        except ValueError as exc:
            self._send(400, f"bad parameter: {exc}".encode(), "text/plain")
            return
        ext = q.get("ext", [".mkv"])[0]
        if not ext.startswith("."):
            ext = "." + ext

        with _VIDEO_LOCK:
            if _VIDEO_JOB is not None and _VIDEO_JOB["state"] in ("queued", "running"):
                self._send(409, b"a video job is already running", "text/plain")
                return
            _cleanup_job(_VIDEO_JOB)       # reclaim the previous (fetched) job's dir
            job_dir = tempfile.mkdtemp(prefix="vid_")
            in_path = os.path.join(job_dir, "in" + ext)
            with open(in_path, "wb") as f:
                f.write(data)
            job = {
                "id": uuid.uuid4().hex,
                "state": "queued",
                "dir": job_dir,
                "input": in_path,
                "output": os.path.join(job_dir, "out.mp4"),
                "total_frames": _count_video_frames(in_path),
                "frames_written": None,
                "frames_processed": None,
                "output_bytes": 0,
                "seconds": None,
                "error": None,
                "started": time.time(),
                "last_output_t": time.time(),
            }
            _VIDEO_JOB = job
            threading.Thread(target=_run_video_job, args=(job, params),
                             daemon=True).start()
        _log(f"video job {job['id'][:8]} accepted: {len(data)}B "
             f"({job['total_frames']} frames) res={params['resolution']} "
             f"bs={params['batch_size']} chunk={params['chunk_size']}")
        body = json.dumps({"id": job["id"],
                           "total_frames": job["total_frames"]}).encode()
        self._send(200, body, "application/json")

    def _handle_video_status(self, q):
        job = _VIDEO_JOB
        jid = (q.get("id") or [None])[0]
        if job is None or job["id"] != jid:
            self._send(404, json.dumps({"state": "unknown"}).encode(),
                       "application/json")
            return
        # While running, report the growing output size as coarse progress.
        out_bytes = job.get("output_bytes") or 0
        if job["state"] == "running":
            try:
                out_bytes = os.path.getsize(job["output"])
            except OSError:
                out_bytes = 0
        body = json.dumps({
            "id":               job["id"],
            "state":            job["state"],
            "total_frames":     job["total_frames"],
            "frames_written":   job.get("frames_written"),
            "frames_processed": job.get("frames_processed"),
            "output_bytes":     out_bytes,
            "elapsed":          round(time.time() - job["started"], 1),
            "seconds":          job.get("seconds"),
            "error":            job.get("error"),
            "resolved_batch":   job.get("resolved_batch"),
            "resolved_overlap": job.get("resolved_overlap"),
            "resolved_chunk":   job.get("resolved_chunk"),
            "total_chunks":     job.get("total_chunks"),   # GUI caps the pre-report bar to 1/this
            "resolved_attention": getattr(getattr(_ENGINE, "args", None),
                                          "attention_mode", None),
            "compile_dit":      getattr(getattr(_ENGINE, "args", None), "compile_dit", None),
            "uniform_batch":    getattr(getattr(_ENGINE, "args", None), "uniform_batch_size", None),
            "input_noise":      getattr(getattr(_ENGINE, "args", None), "input_noise_scale", None),
            "live_spf":         job.get("live_spf"),
            "peak_alloc_gb":    job.get("peak_alloc_gb"),
            "peak_reserved_gb": job.get("peak_reserved_gb"),
            "phase_trace":      job.get("phase_trace"),     # diagnostic: real phase cadence
            "phase_count":      job.get("phase_count"),
        }).encode()
        self._send(200, body, "application/json")

    def _handle_video_fetch(self, q):
        job = _VIDEO_JOB
        jid = (q.get("id") or [None])[0]
        if job is None or job["id"] != jid:
            self._send(404, b"no such job", "text/plain")
            return
        if job["state"] == "error":
            self._send(500, (job.get("error") or "error").encode(), "text/plain")
            return
        if job["state"] != "done":
            self._send(409, job["state"].encode(), "text/plain")
            return
        try:
            with open(job["output"], "rb") as f:
                out = f.read()
        except OSError as exc:
            self._send(500, f"output unreadable: {exc}".encode(), "text/plain")
            return
        self._send(200, out, "video/mp4",
                   {"X-Frames": str(job.get("frames_written") or 0),
                    "X-Process-Time": f"{job.get('seconds') or 0:.3f}"})
        _log(f"video job {job['id'][:8]} fetched: {len(out)}B")


def _ctype_for(ext):
    return {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".png": "image/png", ".webp": "image/webp"}.get(ext.lower(),
                                                             "application/octet-stream")


def main(argv=None):
    global _ENGINE, _HEARTBEAT, _VERSION, _MODE
    p = argparse.ArgumentParser(description="Resident upscale worker for a RunPod pod.")
    p.add_argument("--repo-dir", required=True)
    p.add_argument("--model-dir", required=True)
    p.add_argument("--settings", required=True)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8200)
    p.add_argument("--heartbeat", default="/tmp/upscale_heartbeat")
    p.add_argument("--worker-version", default="",
                   help="code version reported by /health so a reused pod can "
                        "detect a stale worker and reload it")
    p.add_argument("--mode", choices=("full", "tag", "video"), default="full",
                   help="full = load SeedVR2 and serve /upscale + /orient; "
                        "tag = skip the SeedVR2 load and serve /orient only "
                        "(remote Tag & Rename — leaves the VRAM for Ollama); "
                        "video = load SeedVR2 and serve /video/* (Video Upscaler)")
    args = p.parse_args(argv)

    _HEARTBEAT = args.heartbeat
    _VERSION = args.worker_version
    _MODE = args.mode
    sys.path.insert(0, args.repo_dir)
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

    if args.mode == "tag":
        # Tag mode: no SeedVR2 (orientation is lazy-loaded on the first /orient).
        # /health answers immediately so the client knows the pod is reachable.
        _log("tag mode — SeedVR2 engine NOT loaded; serving /orient + /telemetry.")
        _touch(_HEARTBEAT)
    else:
        # full and video both load SeedVR2 once; they differ only in which
        # endpoints they serve (/upscale vs /video/*).
        from upscale_engine import UpscaleEngine
        with open(args.settings, encoding="utf-8") as f:
            settings = json.load(f)
        _log(f"loading engine ({args.mode} mode, repo={args.repo_dir} "
             f"models={args.model_dir}) …")
        t0 = time.time()
        _ENGINE = UpscaleEngine(args.repo_dir, args.model_dir, settings)
        _log(f"engine ready in {time.time() - t0:.1f}s on {_ENGINE.device_name}")
        _touch(_HEARTBEAT)                        # ready = first heartbeat

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    _log(f"serving on {args.host}:{args.port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if _ENGINE is not None:
            _ENGINE.close()


if __name__ == "__main__":
    main()
