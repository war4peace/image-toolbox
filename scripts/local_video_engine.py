"""
local_video_engine.py
---------------------
LocalVideoEngine — run the Video Upscaler's SeedVR2 work IN-PROCESS on the local
GPU, as a drop-in for RemoteVideoEngine / PassthroughVideoEngine in
batch_video_upscale.run_queue (feature #7, docs/local-video-upscaler.md).

This is the LOCAL counterpart of pod/worker.py's `--mode video`: it drives the
SAME `UpscaleEngine.process_video` streaming path (chunk_size>0) the pod worker
drives, but with no pod, no HTTP and no ssh tunnel. Everything else in the
orchestration (split, reassemble, mux, resume, watchdog) is unchanged and already
runs locally, so "local video" is mostly just swapping the injected engine.

SPIKE SCOPE (0.5.0). The goal here is to prove the in-process streaming path end to
end on a consumer GPU. Two deliberate simplifications, both to be replaced when this
graduates from spike to feature:

  * **AUTO batch/chunk sizing is conservative and self-contained** (a flat floor
    seed, not a VRAM model). pod/worker.py has the measured per-(card, output-MP)
    sizing (`_auto_batch` / `_max_vram_batch` / `_suggested_batch`); the production
    step should EXTRACT those into a shared module so local and remote size
    identically, instead of this file re-deriving them.
  * **No advisory VRAM warning / custom-target plumbing yet** — that is the GUI
    step (sections 6-7 of the design doc).

What IS here because it matters most on the consumer GPUs this feature targets:
OOM auto-recovery (retry the segment at a smaller window, down to a floor) and
per-segment PEAK VRAM reporting, emitted through `on_progress` in the SAME keys the
runner already reads (`resolved_batch` / `peak_alloc_gb` / ...), so the existing
per-segment VRAM log and auto-tuner work locally with no runner change. That peak
VRAM readout is exactly the signal the "test till it breaks" benchmark (section 8)
needs.

Must run inside the toolbox venv (PyTorch CUDA + seedvr2/requirements.txt).
"""

import os
import sys
import json
import time
import queue
import tempfile
import threading
import subprocess

# OOM detection is shared with the runners; fall back to a string match so this
# module still loads on an older tree without runner_common.
try:
    from runner_common import is_oom_error as _is_oom_error
except Exception:                                    # noqa: BLE001
    def _is_oom_error(exc):
        s = str(exc).lower()
        return "out of memory" in s or "cuda oom" in s or "cublas" in s and "alloc" in s

import video_pipeline as vp

# Predictive VRAM sizer (feature #7). Guarded so the engine still loads on an older
# tree without it (falls back to the flat auto_batch floor).
try:
    import video_vram_sizer as _sizer
except Exception:                                    # noqa: BLE001
    _sizer = None

# Markers/exit code the subprocess worker (local_video_worker.py) uses.
_WORKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "local_video_worker.py")
_RESULT_MARK = "@@LVW-RESULT@@"
_OOM_MARK = "@@LVW-OOM@@"
_OOM_EXIT = 42                                        # local_video_worker.py OOM exit code
# Default: a segment with NO pipeline progress for this many seconds is thrashing
# (VRAM soft-spilled to sysmem: a healthy decode batch is seconds, a thrashing one was
# ~25 min, docs 14). Generous so a legitimately heavy step never false-trips; low enough
# to abort a thrash in minutes, not hours. Config-overridable (video.thrash_stall_seconds).
DEFAULT_STALL_SECONDS = 300
_STALL_FLOOR = 30                                    # never trip faster than this (false-trip guard)

_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


class ThrashDetected(Exception):
    """The mid-segment watchdog killed a subprocess that stopped making progress (a
    thrashing / hung GPU). Distinct from a CUDA OOM (which the retry loop steps down
    from): a thrash is a DEGRADATION episode, so the runner must NOT retry it -- it
    surfaces loudly and stops the run (the segment resumes on the next, post-reboot run)."""
    pass


def _query_gpu_name():
    """GPU 0's name via nvidia-smi (no torch/CUDA init, so the parent stays GPU-free in
    subprocess mode). None on any failure."""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=10,
            creationflags=_CREATE_NO_WINDOW)
        name = out.stdout.strip().splitlines()[0].strip()
        return name or None
    except Exception:                                # noqa: BLE001
        return None


def _kill_tree(proc):
    """Kill a subprocess AND its children (the CUDA worker may spawn helpers). On Windows
    `taskkill /T /F` is the only reliable tree-kill; elsewhere terminate/kill. Fail-safe."""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, creationflags=_CREATE_NO_WINDOW)
        else:
            proc.kill()
    except Exception:                                # noqa: BLE001
        try:
            proc.kill()
        except Exception:                            # noqa: BLE001
            pass


def _to_4n1(x):
    """Round DOWN to the nearest 4n+1 (>=1). SeedVR2's causal temporal VAE wants a
    window of 4k+1 frames; a non-conforming batch is silently reshaped upstream, so
    we normalise here to keep the window (and its stride math) predictable. Mirrors
    pod/worker.py `_to_4n1`."""
    n = max(1, int(x))
    return ((n - 1) // 4) * 4 + 1


class LocalVideoEngine:
    """In-process SeedVR2 video engine. Implements the injected-engine contract
    `batch_video_upscale.process_job` expects: `process_segment(...)`,
    `last_segment_seconds`, `last_phase`, `device_name`, `resident`, `telemetry()`,
    `close()`.

    Loads DiT + VAE ONCE (via UpscaleEngine) and reuses them across segments, so a
    multi-segment video pays the model load only once, exactly like the resident pod
    worker."""

    # Below this a temporal window barely helps, and OOM-retry stops shrinking here
    # (matches pod/worker._BATCH_FLOOR). If the floor itself OOMs, the card genuinely
    # can't do this target: we let the error propagate (the watchdog / GUI surfaces it).
    BATCH_FLOOR = 5

    def __init__(self, repo_dir, model_dir, settings, debug=False,
                 auto_batch=None, conn=None, gpu_id=None,
                 use_subprocess=False, thrash_stall_seconds=DEFAULT_STALL_SECONDS):
        """repo_dir / model_dir / settings mirror UpscaleEngine (the "upscale" config
        section merged with the "video" knobs).

        `conn` (optional db connection) + `gpu_id` enable the PREDICTIVE VRAM sizer's
        learned self-calibration (video_vram_sizer): AUTO (batch<=0) then picks the
        largest window predicted to fit on the first try, and each segment's outcome is
        recorded per (gpu|model, output-MP) so it improves over time.

        `use_subprocess` (the PRODUCT path) runs each GPU attempt in a fresh child
        process so the mid-segment thrash WATCHDOG can kill a crawling segment (a
        synchronous in-process CUDA call can't be interrupted, docs 14/17) AND every
        attempt gets a clean CUDA context (no fragmentation carryover across OOM
        retries). Its cost is a model reload per attempt; the parent stays GPU-free.
        Left OFF (default) the engine runs in-process, reusing one cached model (the
        spike / simple path)."""
        self._repo_dir = os.path.abspath(repo_dir)
        self._model_dir = os.path.abspath(model_dir)
        self._settings = dict(settings or {})
        self._debug = bool(debug or self._settings.get("debug", False))
        self.use_subprocess = bool(use_subprocess)
        self.thrash_stall_seconds = int(thrash_stall_seconds or DEFAULT_STALL_SECONDS)
        self._settings_path = None                   # lazily written temp JSON (subprocess mode)

        self.model = self._settings.get("dit_model") or "seedvr2_ema_7b_fp16.safetensors"
        if self.use_subprocess:
            # Keep the parent GPU-free: no in-process engine. Identify the card and its
            # residency regime WITHOUT loading torch/CUDA here (each child does that).
            self._engine = None
            self.device_name = _query_gpu_name() or "local"
            self.resident = False                    # a <40 GB local card offloads (child confirms)
        else:
            from upscale_engine import UpscaleEngine
            self._engine = UpscaleEngine(self._repo_dir, self._model_dir, self._settings,
                                         debug=self._debug)
            self.device_name = getattr(self._engine, "device_name", "local")
            self.resident = getattr(self._engine, "resident", False)
            self.model = getattr(getattr(self._engine, "args", None), "dit_model", "") or self.model

        self.auto_batch = int(auto_batch) if auto_batch else self.BATCH_FLOOR
        self.conn = conn
        self.gpu_id = gpu_id or self.device_name
        self.last_segment_seconds = None
        self.last_phase = {}
        # The window that ACTUALLY ran (after any OOM auto-recovery), exposed for the
        # benchmark harness / report so a fell-back run records its true ceiling.
        self.last_resolved_batch = None
        self.last_overlap = None

    # ── window sizing (spike-grade; see module docstring) ────────────────────

    def _resolve_window(self, batch_size, chunk_size, temporal_overlap, frames):
        """Resolve (batch, overlap, chunk) from the requested values and the segment's
        frame count. AUTO batch (<=0) seeds from `self.auto_batch`; a window >= the clip
        collapses to one seam-free pass; chunk is sized so frames STREAM out (>0)."""
        batch = int(batch_size)
        if batch <= 0:
            batch = self.auto_batch
        batch = _to_4n1(batch)

        overlap = max(0, int(temporal_overlap))
        if overlap >= batch:                         # SeedVR2 resets overlap>=batch to 0
            overlap = max(0, batch - 1)

        # Fit the window to the real frame count: a batch >= the clip is one pass.
        if frames and batch >= frames:
            batch = _to_4n1(frames)
            overlap = 0

        chunk = int(chunk_size)
        if chunk <= 0:                               # MUST be >0 so output frames stream
            if frames and batch >= frames:
                chunk = batch
            elif batch > 89:
                chunk = max(1, batch - overlap)
            else:
                chunk = batch * max(1, round(90 / batch))
        return batch, max(0, overlap), max(1, chunk)

    # ── the engine contract ──────────────────────────────────────────────────

    def process_segment(self, src_path, dest_path, *, resolution, batch_size,
                        chunk_size, temporal_overlap=0, seed=None,
                        video_backend="opencv", use_10bit=False,
                        poll_interval=0, on_progress=None, should_stop=None):
        """Upscale one segment file to dest_path on the LOCAL GPU and return the frame
        count written. Emits runner-compatible `on_progress` status dicts. On a CUDA
        OOM it retries at a smaller window (down to BATCH_FLOOR) so an optimistic batch
        self-corrects instead of failing the whole run, mirroring the pod worker.

        `should_stop` is accepted for interface parity; a local segment runs
        synchronously, so it is only checked before the (uninterruptible) GPU call."""
        self.last_segment_seconds = None
        self.last_phase = {}

        # Probe frames AND source dims in one pass; derive the OUTPUT size (box-fit to the
        # short-side `resolution`) so the sizer can budget by output megapixels.
        frames_total, out_w, out_h = 0, 0, 0
        try:
            info = vp.probe(src_path, count=True)
            frames_total = int(info.nb_frames or 0)
            sw, sh = int(info.width or 0), int(info.height or 0)
            if sw and sh and resolution:
                scale = float(resolution) / min(sw, sh)
                out_w, out_h = round(sw * scale), round(sh * scale)
        except Exception:                            # noqa: BLE001 (best-effort)
            pass

        # AUTO (batch<=0): the predictive VRAM sizer picks the largest window that should
        # fit on the FIRST try (per model + free VRAM + learned history), so we never
        # start high and cascade. An explicit batch is honored as-is. In subprocess mode
        # the free-VRAM read goes through nvidia-smi so the parent never inits CUDA.
        if int(batch_size) <= 0 and _sizer is not None and out_w and out_h:
            free_gb, total_gb = _sizer.free_vram_gb(prefer_smi=self.use_subprocess)
            picked_b, picked_o = _sizer.pick(self.model, out_w, out_h,
                                             conn=self.conn, gpu_id=self.gpu_id,
                                             free_gb=free_gb, total_gb=total_gb)
            batch, overlap, chunk = self._resolve_window(
                picked_b, chunk_size, picked_o, frames_total)
        else:
            batch, overlap, chunk = self._resolve_window(
                batch_size, chunk_size, temporal_overlap, frames_total)
        first_batch = batch

        args = getattr(self._engine, "args", None)
        if on_progress:                              # lets the runner log the resolved window once
            on_progress({
                "state": "running",
                "resolved_batch": batch,
                "resolved_overlap": overlap,
                "resolved_attention": getattr(args, "attention_mode", None),
                "compile_dit": getattr(args, "compile_dit", False),
                "uniform_batch": getattr(args, "uniform_batch_size", False),
                "input_noise": getattr(args, "input_noise_scale", 0) or 0,
                "total_frames": frames_total,
            })

        if should_stop and should_stop():
            from remote_video_engine import RemoteVideoStopped
            raise RemoteVideoStopped("stopped by user")

        # In-process mode measures peak across all attempts (one CUDA context), so reset
        # once here; subprocess mode gets a per-attempt peak from each fresh worker.
        if not self.use_subprocess:
            try:
                import torch
                torch.cuda.reset_peak_memory_stats()
            except Exception:                        # noqa: BLE001
                pass

        # t0 BEFORE the retry loop: a too-big batch burns real GPU time before it OOMs, so
        # the reported seconds includes the failed attempt(s). A ThrashDetected (the
        # subprocess watchdog killed a crawling attempt) is NOT retried -- it is a
        # degradation episode that must surface and stop the run.
        t0 = time.time()
        n = peak_alloc = peak_reserved = None
        while True:
            try:
                n, peak_alloc, peak_reserved = self._run_attempt(
                    src_path, dest_path, resolution, batch, chunk, overlap,
                    seed, video_backend, use_10bit, on_progress, should_stop)
                break
            except ThrashDetected:
                raise
            except Exception as exc:                 # noqa: BLE001
                if not _is_oom_error(exc) or batch <= self.BATCH_FLOOR:
                    raise
                smaller = _to_4n1(batch - 1)
                if not self.use_subprocess:          # a subprocess retry gets a fresh context;
                    try:                             # in-process, at least drop the cache
                        import torch
                        torch.cuda.empty_cache()
                        torch.cuda.synchronize()
                    except Exception:                # noqa: BLE001
                        pass
                overlap = min(overlap, max(0, smaller - 1))
                _, overlap, chunk = self._resolve_window(smaller, 0, overlap, frames_total)
                batch = smaller
                if on_progress:
                    on_progress({"state": "running", "resolved_batch": batch,
                                 "resolved_overlap": overlap, "oom_backoff": True})

        dt = time.time() - t0
        self.last_segment_seconds = dt
        self.last_resolved_batch = batch
        self.last_overlap = overlap

        # Self-calibrate: record the batch that ACTUALLY ran clean (the seed if it fit, or
        # the OOM-recovered value) as the known-good for (gpu|model, output-MP). A learned
        # value then supersedes the seed next time, so the sizer converges to this card.
        if _sizer is not None and out_w and out_h:
            try:
                _sizer.record_result(self.conn, self.gpu_id, self.model,
                                     out_w, out_h, batch, ok=True)
            except Exception:                        # noqa: BLE001 (fail-safe)
                pass

        try:
            out_bytes = os.path.getsize(dest_path)
        except OSError:
            out_bytes = 0

        # Parity with RemoteVideoEngine.last_phase so the runner's phase log doesn't
        # break; local has no submit/fetch, so it is all "wait" (the GPU time).
        self.last_phase = {"submit": 0.0, "wait": dt, "fetch": 0.0,
                           "finalize": 0.0, "bytes": out_bytes}

        if on_progress:
            on_progress({
                "state": "done",
                "frames_written": n,
                "frames_processed": frames_total or n,
                "total_frames": frames_total,
                "seconds": dt,
                "resolved_batch": batch,
                "first_batch": first_batch,
                "resolved_overlap": overlap,
                "peak_alloc_gb": peak_alloc,
                "peak_reserved_gb": peak_reserved,
                "output_bytes": out_bytes,
            })
        return int(n or 0)

    # ── one attempt: in-process, or a killable subprocess (thrash watchdog) ────

    def _run_attempt(self, src, dest, resolution, batch, chunk, overlap, seed,
                     backend, use_10bit, on_progress, should_stop):
        """Run ONE upscale attempt at a fixed window. Returns (frames, peak_alloc_gb,
        peak_reserved_gb). Raises a CUDA-OOM error (caller retries smaller) or
        ThrashDetected (caller must not retry)."""
        if self.use_subprocess:
            return self._run_subprocess_attempt(src, dest, resolution, batch, chunk,
                                                overlap, seed, backend, use_10bit, should_stop)
        n = self._engine.process_video(
            src, dest, resolution=int(resolution), batch_size=batch, chunk_size=chunk,
            temporal_overlap=overlap, seed=seed, video_backend=backend,
            use_10bit=bool(use_10bit), capture=False)
        alloc = reserved = None
        try:
            import torch
            gb = 1024 ** 3
            alloc = round(torch.cuda.max_memory_allocated() / gb, 1)
            reserved = round(torch.cuda.max_memory_reserved() / gb, 1)
        except Exception:                            # noqa: BLE001
            pass
        return n, alloc, reserved

    def _settings_json(self):
        """A temp JSON of the engine settings (incl. the resolved dit_model) for the
        worker subprocess. Written once, reused across attempts, deleted on close()."""
        if self._settings_path and os.path.exists(self._settings_path):
            return self._settings_path
        fd, path = tempfile.mkstemp(prefix="lvw_settings_", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({**self._settings, "dit_model": self.model,
                       "debug": self._debug}, f)
        self._settings_path = path
        return path

    def _run_subprocess_attempt(self, src, dest, resolution, batch, chunk, overlap,
                                seed, backend, use_10bit, should_stop):
        """Upscale one attempt in a FRESH child process, watched for thrash. The parent
        reads the child's stdout: every line is a liveness heartbeat, so a gap longer than
        `thrash_stall_seconds` (a healthy step is seconds; a thrashing one was ~25 min)
        means the GPU is thrashing/hung -> kill the tree and raise ThrashDetected. A CUDA
        OOM (marker / exit 42) becomes a normal OOM error so the caller retries smaller in
        a NEW clean process. Pipeline progress is forwarded to our stdout for the GUI log."""
        cmd = [sys.executable, "-u", _WORKER,
               "--repo-dir", self._repo_dir, "--model-dir", self._model_dir,
               "--settings", self._settings_json(),
               "--input", src, "--output", dest,
               "--resolution", str(int(resolution)), "--batch", str(int(batch)),
               "--chunk", str(int(chunk)), "--overlap", str(int(overlap)),
               "--video-backend", backend]
        if seed is not None:
            cmd += ["--seed", str(int(seed))]
        if use_10bit:
            cmd += ["--use-10bit"]

        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, bufsize=1, creationflags=_CREATE_NO_WINDOW)
        lines = queue.Queue()

        def _reader():
            try:
                for ln in proc.stdout:
                    lines.put(ln)
            finally:
                lines.put(None)                       # EOF sentinel
        threading.Thread(target=_reader, daemon=True).start()

        frames = alloc = reserved = None
        oom = False
        tail = []
        last = time.monotonic()
        stall = max(_STALL_FLOOR, int(self.thrash_stall_seconds))
        while True:
            if should_stop and should_stop():
                _kill_tree(proc)
                from remote_video_engine import RemoteVideoStopped
                raise RemoteVideoStopped("stopped by user")
            try:
                line = lines.get(timeout=2.0)
            except queue.Empty:
                if time.monotonic() - last > stall:
                    _kill_tree(proc)
                    raise ThrashDetected(
                        f"no GPU progress for {stall}s at batch {batch} "
                        f"(VRAM thrash / hung GPU) -- run stopped")
                continue
            if line is None:
                break
            last = time.monotonic()                   # any output is a heartbeat
            s = line.rstrip("\n")
            if _RESULT_MARK in s:
                for tok in s.split():
                    if tok.startswith("frames="):
                        frames = int(tok[len("frames="):] or 0)
                    elif tok.startswith("alloc="):
                        alloc = float(tok[len("alloc="):] or 0)
                    elif tok.startswith("reserved="):
                        reserved = float(tok[len("reserved="):] or 0)
                continue
            if _OOM_MARK in s:
                oom = True
                continue
            print(s, flush=True)                      # forward pipeline progress to the GUI log
            tail.append(s)
            if len(tail) > 40:
                tail.pop(0)

        rc = proc.wait()
        if oom or rc == _OOM_EXIT:
            raise RuntimeError(f"CUDA out of memory (worker) at batch {batch}")
        if frames is None:
            raise RuntimeError(
                f"local video worker failed (exit {rc}): " + " | ".join(tail[-6:]))
        return frames, alloc, reserved

    def probe_batch(self, src_path, dest_path, *, resolution, batch, overlap=None,
                    frames=None, should_stop=None):
        """Benchmark primitive (feature #7, docs 16/20): run ONE upscale attempt at a FIXED
        batch and report the outcome, WITHOUT the process_segment OOM step-down (the benchmark
        sweeps UPWARD to failure, so a failed probe must fail, not silently shrink). Requires
        `use_subprocess` -- an honest upward sweep MUST isolate each attempt in a fresh CUDA
        context (an in-process sweep fragments VRAM and under-reports, docs 14.2/14.3).

        Returns a dict {outcome, batch, overlap, frames, seconds, peak_alloc_gb,
        peak_reserved_gb} where outcome is 'ok' | 'oom' | 'thrash' | 'stopped' | 'error'.
        Never raises for an OOM/thrash (they are expected sweep outcomes); a genuine
        bad-setup error is returned as 'error' with the message in `error`."""
        if not self.use_subprocess:
            raise RuntimeError("probe_batch requires use_subprocess=True (fresh CUDA context "
                               "per probe is mandatory for an honest upward sweep)")
        b = _to_4n1(batch)
        # Default overlap = the sizer's quality-floored auto value for this batch (>=6 once
        # the window allows), so a benchmark ceiling is measured at the overlap real runs use.
        ov = (_sizer.auto_overlap(b) if _sizer is not None
              else (max(0, b - 1) if b <= 6 else max(6, round(b / 6))))
        if overlap is not None:
            ov = min(max(0, int(overlap)), max(0, b - 1))
        _b, ov, chunk = self._resolve_window(b, 0, ov, frames)
        t0 = time.time()
        try:
            n, alloc, reserved = self._run_subprocess_attempt(
                src_path, dest_path, resolution, b, chunk, ov, None,
                "opencv", False, should_stop)
        except ThrashDetected as exc:
            return {"outcome": "thrash", "batch": b, "overlap": ov,
                    "seconds": time.time() - t0, "error": str(exc)}
        except Exception as exc:                          # noqa: BLE001
            from remote_video_engine import RemoteVideoStopped
            if isinstance(exc, RemoteVideoStopped):
                return {"outcome": "stopped", "batch": b, "overlap": ov}
            if _is_oom_error(exc):
                return {"outcome": "oom", "batch": b, "overlap": ov,
                        "seconds": time.time() - t0}
            return {"outcome": "error", "batch": b, "overlap": ov, "error": str(exc)}
        dt = time.time() - t0
        return {"outcome": "ok", "batch": b, "overlap": ov, "frames": int(n or 0),
                "seconds": dt, "peak_alloc_gb": alloc, "peak_reserved_gb": reserved}

    def telemetry(self):
        """Interface parity. Local GPU telemetry is sampled by the GUI's own
        `App.sample_telemetry` (the local row), so the engine does not duplicate it;
        returns None like PassthroughVideoEngine."""
        return None

    def close(self):
        try:
            if self._engine is not None:
                self._engine.close()
        except Exception:                            # noqa: BLE001
            pass
        if self._settings_path:
            try:
                os.remove(self._settings_path)
            except OSError:
                pass
            self._settings_path = None
