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


# Auto-tuner constants. VRAM_USED ~= out_megapixels * (A + B * batch) for a RESIDENT
# 7B-fp16 run, fitted to measured anchors (RTX PRO 6000 96 GB: 4K 4:3 bs5 = 81 GB;
# B200 180 GB: 4K 16:9 bs33 = 172 GB). The 1440p plateau (~75 GB at bs33 16:9) checks
# out. Errs a touch high so the SAFETY margin + OOM auto-recovery cover the rest.
_VRAM_A, _VRAM_B = 11.69, 0.2746
_VRAM_SAFETY = 0.80          # use at most this fraction of the card before OOM-recovery
_BATCH_CAP = 33              # continuity gains flatten past here; throughput is flat past ~9
_BATCH_FLOOR = 5             # below this a temporal window barely helps


def _auto_batch(out_w, out_h, vram_gb, resident):
    """Pick the largest safe temporal window (4n+1) for an output of out_w x out_h on
    a card with vram_gb. Bigger = better continuity + fewer seams (throughput is flat
    past ~9), so we take the most the VRAM budget allows, capped where continuity
    stops improving. OOM auto-recovery backstops an optimistic guess. Returns a 4n+1
    in [_BATCH_FLOOR, _BATCH_CAP]; a safe 13 if dims/VRAM are unknown."""
    mp = (out_w * out_h) / 1_000_000.0
    if mp <= 0 or vram_gb <= 0:
        return 13
    budget = vram_gb * (_VRAM_SAFETY if resident else 0.90)
    raw = (budget / mp - _VRAM_A) / _VRAM_B        # invert the VRAM model for batch
    return max(_BATCH_FLOOR, min(_BATCH_CAP, _to_4n1(raw)))


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
        total = self._job.get("total_frames") or 0
        if total <= 0:
            return
        try:
            for m in _NT_RE.finditer(s):
                n, tot = int(m.group(1)), int(m.group(2))
                # Frame-scaled bar: denominator ~= the segment frame count.
                if tot > 0 and abs(tot - total) <= max(2, int(total * 0.05)):
                    self._job["frames_processed"] = min(n, total)
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
    """Fill in batch_size / temporal_overlap when the caller asked for AUTO
    (batch_size <= 0, temporal_overlap < 0): pick the window from the segment's
    OUTPUT size and this card's real VRAM, and scale the overlap to it. An explicit
    value from the caller is left untouched (the Advanced override)."""
    if params["batch_size"] <= 0:
        in_w, in_h = _video_dims(job["input"])
        short = min(in_w, in_h) if in_w and in_h else 0
        scale = (params["resolution"] / short) if short else 1.0
        out_w, out_h = round(in_w * scale), round(in_h * scale)
        resident = bool(getattr(_ENGINE, "resident", False))
        params["batch_size"] = _auto_batch(out_w, out_h, _vram_total_gb(), resident)
        job["auto_batch"] = True
        _log(f"auto batch -> {params['batch_size']} (out {out_w}x{out_h}, "
             f"vram {_vram_total_gb():.0f}GB, resident={resident})")
    if params["chunk_size"] <= 0:
        # Stream in ~90-frame chunks rounded to a whole number of batches (RAM-bound,
        # no quality effect); MUST be > 0 so frames stream out instead of ballooning.
        b = max(1, params["batch_size"])
        params["chunk_size"] = b * max(1, round(90 / b))
    if params["temporal_overlap"] < 0:
        params["temporal_overlap"] = _auto_overlap(params["batch_size"])
        _log(f"auto temporal_overlap -> {params['temporal_overlap']}")
    # SeedVR2 silently resets overlap >= batch to 0; clamp so it never disables.
    if params["temporal_overlap"] >= params["batch_size"]:
        params["temporal_overlap"] = max(0, params["batch_size"] - 1)
    job["resolved_batch"] = params["batch_size"]
    job["resolved_overlap"] = params["temporal_overlap"]


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
            "resolved_attention": getattr(getattr(_ENGINE, "args", None),
                                          "attention_mode", None),
            "peak_alloc_gb":    job.get("peak_alloc_gb"),
            "peak_reserved_gb": job.get("peak_reserved_gb"),
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
