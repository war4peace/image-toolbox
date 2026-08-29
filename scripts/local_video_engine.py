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
import re
import sys
import json
import time
import queue
import tempfile
import threading
import subprocess
import contextlib

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
# Throwaway passes a benchmark probe runs before its TIMED one. 1 is enough: it loads the
# model and compiles this rung's shape, so the timed pass measures the steady-state cost a
# real segment pays. Applies to BOTH regimes -- the model load is not compile's fault, and
# it alone was inflating short probes by ~2x. Costs one extra pass per rung; that is the
# price of a rate that means what video_estimate thinks it means. Probe-only: a production
# run never warms (it loads and compiles ONCE, then amortises over every segment).
PROBE_WARMUP_PASSES = 1
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
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=10, creationflags=_CREATE_NO_WINDOW)
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


# SeedVR2's per-chunk streaming marker, e.g. "Chunk 3/12: 98 new + 8 context frames"
# (inference_cli._stream_video_chunks, force-logged once per chunk). We parse it to print a
# clean, live per-chunk progress line so a long multi-chunk segment isn't silent for minutes.
_CHUNK_RE = re.compile(r"Chunk\s+(\d+)\s*/\s*(\d+)")


class _LiveEngineOutput:
    """redirect_stdout/stderr target that processes the SeedVR2 engine's output LIVE, one
    line at a time, instead of buffering it all until the segment ends (UpscaleEngine's own
    capture=True drains only in a `finally`, so nothing appears for the whole 20-minute run).

    Each completed line is handed to `on_line` -- carriage-return redraws (tqdm bars) collapse
    to their final text, blank lines are dropped. The runner points `on_line` at the file-only
    logger (setup banners / tqdm tails -> logs/video_<hash>.log, never the terminal) and, for a
    segment, also at the chunk detector so a live per-chunk progress line can be printed.

    The trailing (un-terminated) partial is collapsed on each write to bound memory: a tqdm bar
    redraws with \\r hundreds of times before a \\n, and we only ever want its latest state."""

    def __init__(self, on_line):
        self._on_line = on_line
        self._buf = ""

    def write(self, text):
        self._buf += str(text)
        while True:
            nl = self._buf.find("\n")
            if nl < 0:
                break
            line, self._buf = self._buf[:nl], self._buf[nl + 1:]
            self._emit(line)
        if "\r" in self._buf:                            # keep only the latest redraw of a bar
            self._buf = self._buf.rsplit("\r", 1)[-1]
        return len(text)

    def _emit(self, line):
        line = line.rsplit("\r", 1)[-1]                  # collapse an in-line \r redraw
        if line.strip():
            try:
                self._on_line(line)
            except Exception:                            # noqa: BLE001 (logging is best-effort)
                pass

    def flush(self):
        pass

    def close(self):
        """Flush any trailing partial line (no terminating newline)."""
        if self._buf:
            self._emit(self._buf)
            self._buf = ""


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
                 use_subprocess=False, thrash_stall_seconds=DEFAULT_STALL_SECONDS,
                 diag_sink=None):
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
        spike / simple path).

        `diag_sink` (optional callable(str)) receives the SeedVR2 engine's own stdout --
        the setup banners / tqdm tails -- one line at a time, so the runner can send them
        to logs/video_<hash>.log ONLY, keeping the GUI terminal to the clean run narrative.
        When None (e.g. the benchmark), the engine prints those diagnostics to stdout as
        before."""
        self._repo_dir = os.path.abspath(repo_dir)
        self._model_dir = os.path.abspath(model_dir)
        self._settings = dict(settings or {})
        self._debug = bool(debug or self._settings.get("debug", False))
        self.use_subprocess = bool(use_subprocess)
        self.thrash_stall_seconds = int(thrash_stall_seconds or DEFAULT_STALL_SECONDS)
        self._settings_path = None                   # lazily written temp JSON (subprocess mode)
        # Route engine diagnostics to the runner's file log (never the terminal) when given.
        self._diag_sink = diag_sink

        self.model = self._settings.get("dit_model") or "seedvr2_ema_7b_fp16.safetensors"
        if self.use_subprocess:
            # Keep the parent GPU-free: no in-process engine. Identify the card and its
            # residency regime WITHOUT loading torch/CUDA here (each child does that).
            self._engine = None
            self.device_name = _query_gpu_name() or "local"
            self.resident = False                    # a <40 GB local card offloads (child confirms)
        else:
            from upscale_engine import UpscaleEngine
            # Capture the engine's stdout to the file-only sink so the in-process path stops
            # leaking SeedVR2 diagnostics into the GUI terminal. Construction is captured too,
            # not just process_video: the "SeedVR2 optimizations check…" / "Conv3d workaround…"
            # banners print once at seedvr2's first IMPORT, which happens while UpscaleEngine
            # loads -- before any per-segment capture window.
            if self._diag_sink is not None:
                live = _LiveEngineOutput(self._diag_sink)
                try:
                    with contextlib.redirect_stdout(live), contextlib.redirect_stderr(live):
                        self._engine = UpscaleEngine(self._repo_dir, self._model_dir,
                                                     self._settings, debug=self._debug)
                finally:
                    live.close()
            else:
                self._engine = UpscaleEngine(self._repo_dir, self._model_dir,
                                             self._settings, debug=self._debug)
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
        # Per-segment context for the live per-chunk progress line, set in process_segment.
        self._seg_ctx = (None, None, 0)      # (seg_index 0-based, seg_total, frames_total)
        self._chunk_state = {"last": 0}      # last chunk index we announced (per segment)
        self._real_stdout = None             # the runner's stdout, saved before any redirect
        self._seg_on_progress = None         # runner callback, so per-chunk lines also move the bar

    # ── live per-chunk progress ──────────────────────────────────────────────

    def _emit_progress(self, msg):
        """Print a clean progress line to the runner's REAL stdout (so the GUI terminal shows
        it) AND to the file log. Writing to the saved real stdout is what makes this safe to
        call while the engine's own stdout is redirected to the file sink: the message bypasses
        that redirect instead of being captured back into the file. Fail-safe."""
        out = self._real_stdout or sys.stdout
        try:
            out.write(msg + "\n")
            out.flush()
        except Exception:                                # noqa: BLE001
            pass
        if self._diag_sink is not None:
            try:
                self._diag_sink(msg)
            except Exception:                            # noqa: BLE001
                pass

    def _maybe_emit_chunk(self, line):
        """If `line` is a SeedVR2 'Chunk X/N' marker for a NOT-yet-announced chunk, print a
        clean per-chunk progress line (segment context + frames), so a long multi-chunk segment
        reports steadily instead of going silent for the whole run."""
        m = _CHUNK_RE.search(line)
        if not m:
            return
        idx, tot = int(m.group(1)), int(m.group(2))
        state = getattr(self, "_chunk_state", None)
        if state is None:
            state = self._chunk_state = {"last": 0}
        if idx == state.get("last"):
            return
        state["last"] = idx
        seg_i, seg_n, frames = getattr(self, "_seg_ctx", (None, None, 0))
        where = f"segment {seg_i + 1}/{seg_n}, " if (seg_i is not None and seg_n) else ""
        self._emit_progress(f"    {where}chunk {idx}/{tot}"
                            + (f" of {frames} frames" if frames else "") + " …")
        # Also STEP THE PROGRESS BAR: the remote engine feeds real frames_processed via
        # status polls, but a local segment runs synchronously and only reported at start
        # and 'done', so the bar sat at 0 % then jumped. A newly-started chunk means the
        # PREVIOUS chunks are finished, so report (idx-1)/tot of the segment as done. The
        # 'done' event then supplies the exact final count. Fail-safe (never break output).
        cb = getattr(self, "_seg_on_progress", None)
        if cb is not None and frames and tot:
            try:
                cb({"state": "running",
                    "frames_processed": int(round((idx - 1) / tot * frames)),
                    "total_chunks": int(tot)})
            except Exception:                            # noqa: BLE001
                pass

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
                        poll_interval=0, on_progress=None, should_stop=None,
                        seg_index=None, seg_total=None, model=None):
        """Upscale one segment file to dest_path on the LOCAL GPU and return the frame
        count written. Emits runner-compatible `on_progress` status dicts. On a CUDA
        OOM it retries at a smaller window (down to BATCH_FLOOR) so an optimistic batch
        self-corrects instead of failing the whole run, mirroring the pod worker.

        `seg_index` / `seg_total` (0-based index, count) are used only to label the live
        per-chunk progress line ("segment 2/5, chunk 3/12 …"); they are optional so the
        other engines' identical call is unaffected.

        `should_stop` is accepted for interface parity; a local segment runs
        synchronously, so it is only checked before the (uninterruptible) GPU call."""
        self.last_segment_seconds = None
        self.last_phase = {}
        # Save the runner's real stdout NOW, before any redirect, so the live per-chunk line
        # can bypass the engine-output capture and reach the terminal. Reset the chunk counter.
        self._real_stdout = sys.stdout
        self._chunk_state = {"last": 0}
        self._seg_on_progress = on_progress          # so per-chunk lines step the progress bar

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
        self._seg_ctx = (seg_index, seg_total, frames_total)   # for the per-chunk line

        # AUTO (batch<=0): the predictive VRAM sizer picks the largest window that should
        # fit on the FIRST try (per model + free VRAM + learned history), so we never
        # start high and cascade. An explicit batch is honored as-is. In subprocess mode
        # the free-VRAM read goes through nvidia-smi so the parent never inits CUDA.
        if int(batch_size) <= 0 and _sizer is not None and out_w and out_h:
            free_gb, total_gb = _sizer.free_vram_gb(prefer_smi=self.use_subprocess)
            picked_b, picked_o = _sizer.pick(self.model, out_w, out_h,
                                             conn=self.conn, gpu_id=self.gpu_id,
                                             free_gb=free_gb, total_gb=total_gb,
                                             tags=_sizer.learn_tag(self._settings))
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
                                     out_w, out_h, batch, ok=True,
                                     tags=_sizer.learn_tag(self._settings))
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
            out = self._run_subprocess_attempt(
                src, dest, resolution, batch, chunk, overlap, seed, backend,
                use_10bit, should_stop)
            return out["frames"], out["alloc"], out["reserved"]
        # With a diag sink, redirect the engine's stdout/stderr to a LIVE line processor: each
        # line streams to the file log as it happens (not all at segment end, as the engine's
        # own capture=True would), and the 'Chunk X/N' markers drive a live per-chunk progress
        # line on the terminal. Without a sink (the benchmark), keep the engine printing live to
        # stdout. _maybe_emit_chunk writes to the SAVED real stdout, so it isn't captured back.
        if getattr(self, "_diag_sink", None) is not None:
            def _on_line(line):
                try:
                    self._diag_sink(line)                # every engine line -> file log
                except Exception:                        # noqa: BLE001
                    pass
                self._maybe_emit_chunk(line)             # 'Chunk X/N' -> clean terminal line
            live = _LiveEngineOutput(_on_line)
            try:
                with contextlib.redirect_stdout(live), contextlib.redirect_stderr(live):
                    n = self._engine.process_video(
                        src, dest, resolution=int(resolution), batch_size=batch,
                        chunk_size=chunk, temporal_overlap=overlap, seed=seed,
                        video_backend=backend, use_10bit=bool(use_10bit), capture=False)
            finally:
                live.close()
        else:
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
                                seed, backend, use_10bit, should_stop, warmup_passes=0,
                                warmup_src=None):
        """Upscale one attempt in a FRESH child process, watched for thrash. The parent
        reads the child's stdout: every line is a liveness heartbeat, so a gap longer than
        `thrash_stall_seconds` (a healthy step is seconds; a thrashing one was ~25 min)
        means the GPU is thrashing/hung -> kill the tree and raise ThrashDetected. A CUDA
        OOM (marker / exit 42) becomes a normal OOM error so the caller retries smaller in
        a NEW clean process. Pipeline progress is forwarded to our stdout for the GUI log.

        Returns a dict: `frames`, `alloc`/`reserved` (peak GB, the WORST case over every pass
        -- what a ceiling is made of), `steady_alloc`/`steady_reserved` (the warm pass alone,
        None when the probe ran a single pass), and `seconds` (the child's own timing of its
        TIMED pass, None when it reported none -- an older worker -- so the caller falls back
        to wall time). `warmup_passes` > 0 asks the child to absorb the model load and
        torch.compile's per-process cost first, so `seconds` is a WARM rate and the steady
        peaks are separable from the cold ones. `warmup_src` (optional) points those warmup
        passes at a SHORT clip instead of `src`: the compile + peak a warmup reproduces are
        set by the window (batch), not the clip length, so a batch+1-frame clip warms the same
        graphs at the same ceiling far cheaper. See local_video_worker's docstring."""
        cmd = [sys.executable, "-u", _WORKER,
               "--repo-dir", self._repo_dir, "--model-dir", self._model_dir,
               "--settings", self._settings_json(),
               "--input", src, "--output", dest,
               "--resolution", str(int(resolution)), "--batch", str(int(batch)),
               "--chunk", str(int(chunk)), "--overlap", str(int(overlap)),
               "--video-backend", backend]
        if warmup_passes > 0:
            cmd += ["--warmup-passes", str(int(warmup_passes))]
            if warmup_src and os.path.abspath(warmup_src) != os.path.abspath(src):
                cmd += ["--warmup-input", warmup_src]
        if seed is not None:
            cmd += ["--seed", str(int(seed))]
        if use_10bit:
            cmd += ["--use-10bit"]

        # MUST decode as UTF-8: the worker hardens its stdout to UTF-8 and SeedVR2 prints emoji
        # banners (e.g. 🏃). Without an explicit encoding, text=True defaults to the Windows ANSI
        # code page (cp1252), whose undefined byte 0x8F (part of 🏃's UTF-8) raises
        # UnicodeDecodeError in the reader thread -> the pipe stops draining -> the worker blocks
        # on write -> proc.wait() deadlocks (the "stuck at first segment" hang). errors="replace"
        # keeps any stray bytes from ever killing the reader again.
        # stdin=DEVNULL is REQUIRED, not cosmetic: the worker never reads stdin (Stop is
        # delivered by killing the process tree), but if it INHERITS the GUI's live stdin
        # pipe, Python's `-u` interpreter startup deadlocks in a native stdio flush/seek
        # (Py_InitializeFromConfig -> fflush -> lseek -> NtQueryInformationFile) before our
        # code ever runs -- especially through this venv's redirector python.exe, which adds
        # an stdio-pumping process hop. That is the "stuck at probing batch N, VRAM flat,
        # never loads" benchmark hang. A null stdin can't block, and detaches the worker from
        # the GUI's stdin entirely (so it can never steal the watcher's Stop line either).
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace",
                                bufsize=1, creationflags=_CREATE_NO_WINDOW)
        lines = queue.Queue()

        def _reader():
            try:
                for ln in proc.stdout:
                    lines.put(ln)
            finally:
                lines.put(None)                       # EOF sentinel
        threading.Thread(target=_reader, daemon=True).start()

        frames = alloc = reserved = None
        steady_alloc = steady_reserved = None         # the warm pass alone (warmed probes only)
        worker_seconds = None
        oom = False
        oom_pass = None                               # 'warmup' | 'timed' (see the OOM marker)
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
                    elif tok.startswith("seconds="):
                        worker_seconds = float(tok[len("seconds="):] or 0)
                    elif tok.startswith("alloc_steady="):
                        steady_alloc = float(tok[len("alloc_steady="):] or 0)
                    elif tok.startswith("reserved_steady="):
                        steady_reserved = float(tok[len("reserved_steady="):] or 0)
                continue
            if _OOM_MARK in s:
                oom = True
                for tok in s.split():
                    if tok.startswith("pass="):
                        oom_pass = tok[len("pass="):]
                continue
            # With a file-only diag sink, the worker's engine stdout goes to the run log
            # (whitespace-only lines dropped), not the GUI terminal, but the 'Chunk X/N' markers
            # still drive a clean per-chunk progress line; otherwise forward it live.
            # getattr: some harnesses build the engine via __new__ and never set _diag_sink.
            diag_sink = getattr(self, "_diag_sink", None)
            if diag_sink is not None:
                if s.strip():
                    try:
                        diag_sink(s)
                    except Exception:                 # noqa: BLE001 (logging is best-effort)
                        pass
                    self._maybe_emit_chunk(s)         # 'Chunk X/N' -> clean terminal line
            else:
                print(s, flush=True)                  # forward pipeline progress to the GUI log
            tail.append(s)
            if len(tail) > 40:
                tail.pop(0)

        rc = proc.wait()
        if oom or rc == _OOM_EXIT:
            # Name the pass in the message: an OOM in the TIMED pass of a warmed probe is
            # suspect (it fit while cold, then not in the warmup's leftover pool), whereas an
            # OOM in the warmup is the batch genuinely not fitting. The sweep treats both as
            # 'oom'; this is what lets a human tell them apart afterwards in the log.
            raise RuntimeError(f"CUDA out of memory (worker) at batch {batch}"
                               + (f" [{oom_pass} pass]" if oom_pass else ""))
        if frames is None:
            raise RuntimeError(
                f"local video worker failed (exit {rc}): " + " | ".join(tail[-6:]))
        return {"frames": frames, "alloc": alloc, "reserved": reserved,
                "seconds": worker_seconds,
                "steady_alloc": steady_alloc, "steady_reserved": steady_reserved}

    def probe_batch(self, src_path, dest_path, *, resolution, batch, overlap=None,
                    frames=None, should_stop=None, on_progress=None, warmup_src=None,
                    model=None):
        """Benchmark primitive (feature #7, docs 16/20): run ONE upscale attempt at a FIXED
        batch and report the outcome, WITHOUT the process_segment OOM step-down (the benchmark
        sweeps UPWARD to failure, so a failed probe must fail, not silently shrink). Requires
        `use_subprocess` -- an honest upward sweep MUST isolate each attempt in a fresh CUDA
        context (an in-process sweep fragments VRAM and under-reports, docs 14.2/14.3).

        Returns a dict {outcome, batch, overlap, frames, seconds, peak_alloc_gb,
        peak_reserved_gb} where outcome is 'ok' | 'oom' | 'thrash' | 'stopped' | 'error'.
        Never raises for an OOM/thrash (they are expected sweep outcomes); a genuine
        bad-setup error is returned as 'error' with the message in `error`.

        `on_progress` is ACCEPTED AND IGNORED, so the local and remote engines keep one
        `probe_batch` contract for video_benchmark's single code path. There is no status
        channel to report from: the probe is an isolated subprocess whose output the parent
        only reads once it exits. Remote needs the callback (it polls a live pod).

        `warmup_src` (optional) is a SHORT clip the warmup pass(es) run on instead of the timed
        `src_path`, absorbing the model load + torch.compile at the same per-window ceiling for
        a fraction of the work (see local_video_worker). The caller sizes it (>= batch+1 frames);
        None warms on the full clip.

        `model` is VERIFIED, not applied (#26 Part A). A local engine bakes its DiT in at
        construction, so a multi-model sweep builds one engine per model; the remote engine
        instead swaps the model on a shared pod, which is why the shared `probe_batch`
        contract carries it. Checking it here rather than ignoring it is the point: silently
        measuring a different model than the caller asked for would file the numbers under
        the wrong regime key and be believed, which is exactly the failure the per-model keys
        exist to prevent. None = no claim, no check."""
        if model and model != self.model:
            raise RuntimeError(
                f"probe_batch asked for {model!r} but this engine holds {self.model!r}; "
                f"a local sweep must build one engine per model")
        if not self.use_subprocess:
            raise RuntimeError("probe_batch requires use_subprocess=True (fresh CUDA context "
                               "per probe is mandatory for an honest upward sweep)")
        b = _to_4n1(batch)
        # Default overlap = the sizer's quality-floored auto value for this batch (>=6 once
        # the window allows), so a benchmark ceiling is measured at the overlap real runs use.
        ov = (_sizer.auto_overlap(b) if _sizer is not None
              else (max(0, b - 1) if b <= 6 else min(15, max(6, round(b / 6)))))
        if overlap is not None:
            ov = min(max(0, int(overlap)), max(0, b - 1))
        _b, ov, chunk = self._resolve_window(b, 0, ov, frames)
        t0 = time.time()
        try:
            out = self._run_subprocess_attempt(
                src_path, dest_path, resolution, b, chunk, ov, None,
                "opencv", False, should_stop, warmup_passes=PROBE_WARMUP_PASSES,
                warmup_src=warmup_src)
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
        # Prefer the worker's own timing of its WARM pass. Wall time here would also carry
        # process spawn + torch import + the 16 GB model load + torch.compile, none of which
        # a real run pays per segment; the worker's `seconds` is the pass alone.
        wsecs = out["seconds"]
        dt = wsecs if wsecs is not None else (time.time() - t0)
        return {"outcome": "ok", "batch": b, "overlap": ov, "frames": int(out["frames"] or 0),
                "seconds": dt,
                "peak_alloc_gb": out["alloc"], "peak_reserved_gb": out["reserved"],
                "peak_alloc_steady_gb": out["steady_alloc"],
                "peak_reserved_steady_gb": out["steady_reserved"]}

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
