"""
batch_video_upscale.py
----------------------
Video Upscaler (#2) runner — phase 4. The orchestrator that ties the local
ffmpeg container pipeline (video_pipeline.py, phase 2) to the remote SeedVR2
worker (pod/worker.py --mode video, phase 3). RunPod-only: the GPU work happens
on a rented pod; everything else (walk, split, reassemble, mux, drift, resume,
notifications) runs locally. The source is never touched.

Per video, the flow is:

    probe -> split into segments (local, -c copy or re-encode)
      -> stream each segment to the pod worker (submit/poll/fetch)
      -> reassemble (concat + audio-mux, local) -> duration-drift check

Resume + installments (docs/video-upscaler.md section 5) come from the db.py
`video_*` tables at TWO granularities: a stopped run resumes at the first
unfinished SEGMENT, not the first unfinished video. A per-run minute/cost cap
ends a run cleanly after the current segment, leaving the rest `pending` for the
next run — the cost is paid in affordable installments.

The engine is injected (run_batch(engine, ...)) so the same orchestration runs
against the real RemoteVideoEngine (a rented pod); the LOCAL SeedVR2 engine on this
machine's GPU (--local, feature #7); or, with --passthrough, a local no-pod engine
that stream-copies each segment (for testing the whole pipeline without a GPU).

Usage:
    python scripts/batch_video_upscale.py <source> [output] [--target 1080p]
    python scripts/batch_video_upscale.py <source> [output] --local        # this machine's GPU
    python scripts/batch_video_upscale.py <source> [output] --passthrough  # no pod
"""

import os
import sys
import json
import math
import time
import shutil
import hashlib
import tempfile
import datetime
import argparse
import traceback
import threading

try:
    import crash_logger
    crash_logger.install(notify=False)
except Exception:
    pass

import runner_common

# Make stdout/stderr non-ASCII-proof before any output (see runner_common):
# a headless runner must never die on a UnicodeEncodeError from a unicode
# filename, drift warning or § section mark (the Windows console defaults to
# cp1252). This hardening used to live only here; runner_common now shares it.
runner_common.harden_stdout()

import db
import notifications
import video_pipeline as vp

# Fail-safe diagnostic trail for the swallowed-error handlers (guarded import so an old
# install missing debug_log.py still runs). Same pattern the other runners use.
try:
    from debug_log import debug_log
except Exception:
    def debug_log(*_a, **_k):
        pass

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
APP_ROOT = os.path.dirname(_SCRIPT_DIR)

# Containers the walker treats as video. Old home-camera formats first.
VIDEO_EXTS = {".mp4", ".avi", ".mov", ".mkv", ".m4v", ".wmv", ".mpg", ".mpeg",
              ".flv", ".webm", ".3gp", ".ts", ".mts", ".m2ts", ".vob"}

# Target name -> SeedVR2 short-side output resolution.
TARGET_RES = {"1080p": 1080, "1440p": 1440, "4K": 2160}
# Per-target default temporal window (benchmark sweet spot; section 7). 4K's VRAM
# plateau allows more but per-frame is flat past bs9, so a moderate window stands.
DEFAULT_BATCH = {"1080p": 13, "1440p": 13, "4K": 5}
# Temporal overlap = frames blended between consecutive batches so a boundary doesn't
# show as a "break". -1 = AUTO (the pod scales it to ~1/6 of the resolved batch). An
# explicit >=0 overrides (0 = a deliberate hard cut). SeedVR2 silently resets an
# overlap >= batch back to 0, so the pod clamps it to batch-1.
AUTO_TEMPORAL_OVERLAP = -1

WORK_DIRNAME = ".imgtbx_video"        # per-output-tree work area for segments


def _default_work_base():
    """Default LOCAL staging base: <app>/.imgtbx_video. Local (not beside the
    possibly-network OUTPUT) so a dropped share mid-run can't strand in-progress
    segments; only the source read (at split) and the final output write ever touch
    the network. Overridable via the `video.work_root` setting."""
    return os.path.join(APP_ROOT, WORK_DIRNAME)


def _resolve_app_path(value, default_rel):
    """Resolve a config path anchored at the app root; env vars (%USERPROFILE% ...) are
    expanded so config.json stays portable. Mirrors batch_upscale._resolve_path so the
    LOCAL video engine finds the SAME vendored seedvr2 repo + SEEDVR2 weights the Batch
    Upscaler uses."""
    p = os.path.expandvars(value or default_rel)
    return p if os.path.isabs(p) else os.path.normpath(os.path.join(APP_ROOT, p))


def _local_seedvr2_paths(cfg):
    """(repo_dir, model_dir) for the LOCAL video engine, from the `seedvr2` config
    section (same keys the Batch Upscaler reads), defaulting to the vendored `seedvr2/`
    and `models/SEEDVR2/` under the app root."""
    s = cfg.get("seedvr2", {}) or {}
    repo = _resolve_app_path(s.get("repo_dir", ""), "seedvr2")
    model = _resolve_app_path(s.get("model_dir", ""), os.path.join("models", "SEEDVR2"))
    return repo, model


def gate_local_compile(settings, log=None):
    """Gate torch.compile on a real compile-capability probe, for ANY local SeedVR2 run
    (the batch runner AND the benchmark, which share this venv). torch.compile needs BOTH
    Triton AND a C compiler (MSVC cl.exe) on PATH -- inductor shells out to build kernels,
    so Triton alone is NOT enough. With Triton present but no compiler, SeedVR2's first VAE
    compile HANGS under the GUI's piped stdio (it would error 'cl is not found' in a
    console): the exact "stuck at first segment / first probe" symptom. So a machine
    without a compiler must never even try -- route EVERY local engine's settings through
    here, not just the batch runner's, or the benchmark trips the same hang.

    Mutates `settings` in place (compile_dit/compile_vae -> False when it can't compile) and
    returns (disabled: bool, reason: str|None). A user with triton-windows + an activated
    MSVC toolchain keeps the pod-grade speedup; everyone else runs fine without compile
    (speed only, not quality)."""
    want_compile = bool(settings.get("compile_dit") or settings.get("compile_vae"))
    if not want_compile:
        return False, None
    import importlib.util as _ilu
    import shutil as _sh
    triton_ok = _ilu.find_spec("triton") is not None
    compiler_ok = bool(_sh.which("cl") or _sh.which("cc") or _sh.which("gcc")
                       or _sh.which("clang"))
    if triton_ok and compiler_ok:
        if log:
            log("    torch.compile ON (Triton + compiler present): the first segment pays "
                "a one-time compile cost, then the rest runs faster (shared fixed shape).")
        return False, None
    why = ("Triton is not installed" if not triton_ok
           else "no C compiler (MSVC cl.exe) is on PATH")
    settings["compile_dit"] = False
    settings["compile_vae"] = False
    if log:
        log(f"    torch.compile disabled: {why}. It would otherwise stall the first "
            f"segment (compiling with no output) or fail. Local runs continue without "
            f"compile; only speed is affected, not quality.")
    return True, why

# The @@TBX@@ event protocol + GUI-mode detection live in runner_common (0.4.3
# item 5). Re-export them here exactly as the other three runners do, instead of
# keeping private copies that could drift from the shared marker/atomic-write rule
# (item 7). gui_event() below wraps the shared emitter with this runner's JSON
# convenience (every video payload is a list/dict/scalar sent as JSON).
GUI_MARKER      = runner_common.GUI_MARKER
GUI_MODE        = runner_common.GUI_MODE
_stdin_is_piped = runner_common.stdin_is_piped
fmt_hhmmss      = runner_common.fmt_hhmmss


_LOG_FH = None        # per-run file sink (logs/video_<hash>.log, append mode)


def _open_log(source_root):
    """Open (append) a persistent run log keyed by source root, so every session
    for the same folder accumulates in one file: logs/video_<12-char hash>.log.
    Mirrors batch_upscale's Logger. Best-effort: a logging failure never stops a
    run (the GUI console still shows everything)."""
    global _LOG_FH
    try:
        digest = hashlib.sha256(source_root.encode("utf-8")).hexdigest()[:12]
        log_dir = os.path.join(APP_ROOT, "logs")
        os.makedirs(log_dir, exist_ok=True)
        _LOG_FH = open(os.path.join(log_dir, f"video_{digest}.log"),
                       "a", encoding="utf-8", buffering=1)
        stamp = datetime.datetime.now().strftime("%Y-%m-%d | %H:%M:%S")
        _LOG_FH.write(f"\n{'=' * 64}\nSession started: {stamp}\n"
                      f"Source: {source_root}\n{'=' * 64}\n")
    except Exception:                              # noqa: BLE001 (logging is optional)
        _LOG_FH = None


def _close_log():
    global _LOG_FH
    if _LOG_FH is not None:
        try:
            stamp = datetime.datetime.now().strftime("%Y-%m-%d | %H:%M:%S")
            _LOG_FH.write(f"Session ended: {stamp}\n")
            _LOG_FH.close()
        except Exception:                          # noqa: BLE001
            pass
        _LOG_FH = None


def log(msg):
    """Human-readable progress to stdout (the GUI log pane / console) and, when a
    run log is open, to logs/video_<hash>.log for later troubleshooting. stdout is
    CLEAN (no timestamp): the GUI window adds its own '[HH:MM:SS]' per line, so
    stamping here too would double it. The file line carries a full date+time so
    the on-disk log reconstructs timing on its own."""
    sys.stdout.write(f"{msg}\n")
    sys.stdout.flush()
    if _LOG_FH is not None:
        try:
            stamp = datetime.datetime.now().strftime("%Y-%m-%d | %H:%M:%S")
            _LOG_FH.write(f"{stamp} | {msg}\n")
        except Exception:                          # noqa: BLE001
            pass


def log_file_only(msg):
    """Write ONLY to logs/video_<hash>.log, never to stdout (the GUI terminal / console).
    For developer detail that a non-technical user shouldn't see in the log pane — chiefly
    a Python traceback: the terminal gets the clean 'Run failed: …' one-liner, while the
    full trace is preserved on disk for troubleshooting."""
    if _LOG_FH is not None:
        try:
            stamp = datetime.datetime.now().strftime("%Y-%m-%d | %H:%M:%S")
            _LOG_FH.write(f"{stamp} | {msg}\n")
        except Exception:                          # noqa: BLE001
            pass


def gui_event(kind, payload):
    """Machine-readable event line for the GUI tab; a no-op headless. Every video
    payload is a Python object (list/dict/scalar), so this JSON-encodes it and
    hands the resulting string to runner_common.gui_event, which does the atomic,
    tee-aware write of `<GUI_MARKER><KIND>|<payload>` (item 7). Event kinds:
      QUEUE|<json list of rel paths>
      VIDEO|<json>     – the video now being processed (rel, index, total, segments)
      SEGMENT|<json>   – per-segment progress (video_rel, seg_index, total, state)
      VRESULT|<json>   – [rel, "ok"|"fail"|"skip", output_path, warnings?]
      RTELEM|<json>    – a remote-pod telemetry sample (cpu/ram/gpu)
      POD|<pod id>     – the live remote pod (empty on none); protects it from terminate
      RCOST|<float>    – the pod's real billed $/h, for the live cost readout
    """
    runner_common.gui_event(kind, json.dumps(payload))


def _start_remote_telemetry(engine, stop, interval=10.0):
    """Poll the pod's telemetry and stream it to the GUI as RTELEM events so the
    Video Upscaler tab can show a dedicated 'remote pod' readout row (mirrors
    batch_upscale's sampler). The worker answers /telemetry lock-free, so this
    keeps reporting while a segment is being upscaled. Daemon thread, best-effort:
    a failed sample is just skipped, and `stop` ends it cleanly at teardown."""
    def _loop():
        while not stop.is_set():
            stop.wait(interval)
            if stop.is_set():
                break
            try:
                sample = engine.telemetry()
            except Exception:                       # noqa: BLE001 (best-effort)
                sample = None
            if sample:
                gui_event("RTELEM", sample)
    threading.Thread(target=_loop, daemon=True).start()


# config.json load lives in runner_common (shared by every runner).
_load_config = runner_common.load_config


# A job that keeps failing (permanent cause: black-output guard, corrupt source, an
# unreadable codec) used to sit `failed` in the queue and be re-attempted at the top of
# EVERY run — re-probe, re-split, sometimes pod time — forever. After this many
# consecutive failed attempts the runner drops it from the queue (status 'skipped'); the
# GUI's Retry resets the counter. Item 4.
GIVE_UP_AFTER = 3


def _failure_disposition(fail_count, reason, give_up_after=GIVE_UP_AFTER):
    """(status, skip_reason) for a job that just failed for the fail_count-th time.
    At/after give_up_after the job is 'skipped' (leaves the queue) with the reason
    prefixed so the user sees why it stopped being retried; before that it stays
    'failed' and is retried next run. Pure, so it is unit-tested."""
    if fail_count >= give_up_after:
        return "skipped", f"gave up after {fail_count} attempts: {reason}"
    return "failed", reason


class VideoSlowWatch:
    """Notify-only slow-segment health signal for a video run (item 6). Anchors to the
    run's BEST (minimum) seconds-per-output-megapixel across completed segments, the same
    anchor-to-the-minimum reasoning as the image watchdog: only a genuinely fast segment
    can lower the baseline, so a slow creep can't drift it up and hide. Per-megapixel
    normalisation keeps it fair across mixed resolutions/targets. A segment at
    >= factor x the baseline is 'slow' (in the field: rare shared-infra/host contention
    on the pod, invisible from the guest).

    Deliberately NOTIFY-ONLY, never auto-stop: contention usually clears, and killing a
    half-done segment wastes its cost, so the user decides (unlike the image watchdog,
    which auto-stops a degraded LOCAL GPU that only a reboot cures). Edge-triggered: one
    warning per slow episode, re-armed once a segment returns to healthy. Pure (seconds
    are passed in), so it is unit-tested like funds_guard."""

    def __init__(self, factor=3.0, min_samples=2):
        self.factor = max(1.0, float(factor))
        self.min_samples = max(1, int(min_samples))
        self.min_spmp = None
        self.samples = 0
        self._in_episode = False          # inside a slow streak we've already warned on

    def observe(self, seconds, out_megapixels):
        """Feed one completed segment. Returns (warn, ratio): warn=True ONLY on the
        leading edge of a slow episode (emit exactly once); ratio = this segment's rate
        vs the healthy baseline (for the message). Non-positive inputs are ignored so a
        timing-less passthrough/local segment never trips it."""
        if not seconds or not out_megapixels or seconds <= 0 or out_megapixels <= 0:
            return False, 0.0
        spmp = seconds / out_megapixels
        self.samples += 1
        if self.min_spmp is None or spmp < self.min_spmp:
            self.min_spmp = spmp
        ratio = spmp / self.min_spmp if self.min_spmp else 1.0
        if self.samples < self.min_samples:          # need a trusted baseline first
            return False, ratio
        slow = spmp >= self.min_spmp * self.factor
        if slow and not self._in_episode:
            self._in_episode = True
            return True, ratio
        if not slow:
            self._in_episode = False                 # healthy again: re-arm the edge
        return False, ratio


class StopInstallment(Exception):
    """Raised to end a run cleanly when a per-run cap is reached OR the user pressed
    Stop; the rest stays `pending`/`partial` and resumes next run."""


# Mid-segment Stop signal raised by the remote engine's poll loop. Imported guarded so
# the headless / passthrough paths still load without the remote stack; the fallback
# never matches a real stop (only the remote engine raises the real class).
try:
    from remote_video_engine import RemoteVideoStopped as _RemoteVideoStopped
except Exception:                                  # pragma: no cover
    class _RemoteVideoStopped(Exception):
        pass

# Pod/transport failure classes, for the self-healing supervisor (#6). Guarded like the
# Stop signal so headless/passthrough load without the remote stack. `RemoteVideoError`
# = a pod/tunnel liveness failure (redeploy-worthy); `RemoteVideoWorkerError` = a bad
# source the worker rejected (a per-job failure, NEVER healed — a fresh pod would fail
# the same). The fallbacks are unreachable classes so `_is_pod_failure` is simply False
# without the remote stack.
try:
    from remote_video_engine import (RemoteVideoError as _RemoteVideoError,
                                      RemoteVideoWorkerError as _RemoteVideoWorkerError)
except Exception:                                  # pragma: no cover
    class _RemoteVideoError(Exception):
        pass

    class _RemoteVideoWorkerError(_RemoteVideoError):
        pass


class PodLost(Exception):
    """Raised out of run_queue (only when Auto-resume is armed) when the current job
    failed because the POD/tunnel died, not because the source is bad. The supervisor
    catches it, recovers the pod (reconnect or redeploy the identical card), and re-enters
    run_queue, which resumes from the first unfinished segment. Carries the raw reason plus
    the counts this pass completed BEFORE the loss, so the supervisor can accumulate an
    accurate final summary across redeploys (each pass counts only its own jobs; a resumed
    pass never re-counts a `done` job, so summing is correct)."""

    def __init__(self, reason, done=0, failed=0, done_frames=0):
        super().__init__(reason)
        self.done = done
        self.failed = failed
        self.done_frames = done_frames


def _is_pod_failure(exc):
    """True when `exc` is a pod/transport liveness failure the supervisor should heal
    (reconnect or redeploy), False for a bad-source worker error or anything else (which
    stays a per-job failure). A RemoteVideoWorkerError is a subclass of RemoteVideoError,
    so it is excluded explicitly."""
    if isinstance(exc, _RemoteVideoWorkerError):
        return False
    return isinstance(exc, _RemoteVideoError)


# Local-video thrash-watchdog signal (#7). Imported guarded so the remote / passthrough
# paths still load without the local GPU stack. A ThrashDetected is a DEGRADATION episode
# (a hung / VRAM-thrashing LOCAL GPU the mid-segment watchdog killed): it is NOT the
# source's fault and must NOT be retried -- the run stops loudly and the segment resumes on
# the next (post-reboot) run. The fallback class is unreachable, so `run_queue` simply never
# takes the thrash branch on a tree without the local engine.
try:
    from local_video_engine import ThrashDetected as _ThrashDetected
except Exception:                                  # pragma: no cover
    class _ThrashDetected(Exception):
        pass


# Set when the GUI sends "q" on stdin (Stop). Now checked DURING a segment too (the
# remote engine polls _STOP.is_set), so Stop aborts a long segment within a poll
# interval and tears the pod down, instead of waiting for the whole segment.
_STOP = threading.Event()


def _watch_stdin_for_stop():
    """Background reader: a 'q'/'quit'/'stop' line on stdin requests a graceful
    stop. Started in GUI mode only. Fail-safe (any error just ends the watcher)."""
    try:
        for line in sys.stdin:
            if line.strip().lower() in ("q", "quit", "stop"):
                _STOP.set()
                break
    except Exception:
        pass


# ─────────────────────────────────────────────
#  CONFIG RESOLUTION
# ─────────────────────────────────────────────

def resolve_video_cfg(cfg, overrides=None):
    """Merge the config.json `video` section with sane defaults and any CLI
    overrides, returning a plain dict the runner reads."""
    v = dict(cfg.get("video", {}) or {})
    u = dict(cfg.get("upscale", {}) or {})
    out = {
        "target":              v.get("target", "1080p"),
        "output_subdir":       v.get("output_subdir", "__upscaled__"),
        # Local scratch base for per-segment staging (0.4.8). Empty = a folder on the
        # app drive (_default_work_base). Staging locally keeps the multi-hour pod
        # compute + fetch off the (possibly network) output drive, so a mid-run share
        # drop can't strand segments. Keep it OUTSIDE the source tree (the default is)
        # or the walk could rescan its own .mp4 intermediates.
        "work_root":           (str(v.get("work_root") or "").strip() or _default_work_base()),
        "skip_cutoff_pct":     float(v.get("skip_cutoff_pct", 0) or 0),
        "segment_seconds":     float(v.get("segment_seconds", 60) or 60),
        "max_segment_seconds": float(v.get("max_segment_seconds", 120) or 120),
        # 0 = AUTO: the pod sizes the batch from its real VRAM + the output
        # resolution (no global value to misapply across targets). >0 overrides.
        "batch_size":          int(v.get("batch_size", 0) or 0),
        # -1 = AUTO: the pod scales the overlap to the batch. >=0 overrides (0 = a
        # deliberate hard cut). The pod clamps overlap >= batch so it never disables.
        "temporal_overlap":    (int(v["temporal_overlap"])
                                if v.get("temporal_overlap") is not None
                                else AUTO_TEMPORAL_OVERLAP),
        "chunk_size":          int(v.get("chunk_size", 0) or 0),
        # Default to the ffmpeg backend with H.265 10-bit: opencv's writer emits
        # mp4v (MPEG-4 Part 2), a low-quality codec that re-compresses the upscale
        # and that Windows can't always read. ffmpeg/x265-10bit keeps the detail and
        # cuts gradient banding. The pod is guaranteed ffmpeg by remote_run (volume
        # cache + launch-time fallback) and provision.sh. Pick "Standard — MPEG-4" in
        # Settings to fall back to opencv.
        "video_backend":       v.get("video_backend", "ffmpeg"),
        "use_10bit":           bool(v.get("use_10bit", True)),
        "seed":                v.get("seed"),               # None = per-video stable seed
        "per_run_minute_cap":  float(v.get("per_run_minute_cap", 0) or 0),
        "per_run_cost_cap":    float(v.get("per_run_cost_cap", 0) or 0),
        # SeedVR2 quality/speed knobs the pod applies (video path only). compile:
        # torch.compile the DiT+VAE (20-40% faster after the first segment pays a
        # one-time compile cost; safe because our segments share one fixed shape).
        # uniform_batch_size: pad the ragged final batch so it can't flicker.
        # input_noise_scale: >0 counters 4K over-smoothing (default 0 = off).
        "compile":             bool(v.get("compile", True)),
        "uniform_batch_size":  bool(v.get("uniform_batch_size", True)),
        "input_noise_scale":   float(v.get("input_noise_scale", 0.0) or 0.0),
        # SeedVR2 weights the pod loads (video path only; the Batch Upscaler keeps its
        # own). 7B fp16 = best detail (default); 3B-Q8 = smaller + more VRAM headroom for
        # bigger windows. The pod auto-downloads the file to the volume on first use, so
        # switching needs no reprovision. NOTE: the VRAM auto-batch model is 7B-calibrated;
        # 3B uses far less, so on 3B the auto/clamp is conservative (safe, not optimal).
        "dit_model":           v.get("dit_model", "seedvr2_ema_7b_fp16.safetensors"),
        # Slow-segment health signal (item 6): NOTIFY-ONLY. Reuses the image watchdog's
        # enable/factor keys (upscale.watchdog_*) as defaults so a user who tuned the
        # watchdog once gets consistent behaviour, overridable under video.* if wanted.
        "watchdog_enabled":    bool(v.get("watchdog_enabled", u.get("watchdog_enabled", True))),
        "watchdog_factor":     float(v.get("watchdog_factor",
                                           u.get("watchdog_factor", 3.0)) or 3.0),
        # LOCAL video only (#7). A local segment that makes NO pipeline progress for this
        # many seconds is thrashing (VRAM soft-spilled to sysmem: a healthy decode is
        # seconds, a thrashing one was ~25 min, docs 14) -> the mid-segment watchdog kills
        # it and stops the run. Generous so a legitimately heavy step never false-trips.
        "thrash_stall_seconds": int(v.get("thrash_stall_seconds", 300) or 300),
        # LOCAL video only (#7). Run each GPU attempt in a FRESH child process so the
        # thrash watchdog can kill a crawling segment (an in-process CUDA call can't be
        # interrupted) AND every OOM retry gets a clean CUDA context (no fragmentation
        # carryover). Default on (the product path); off = one cached in-process engine
        # (the spike path), faster per attempt but un-killable and fragmentation-prone.
        # Default OFF: the LOCAL engine runs IN-PROCESS, exactly like the proven image Batch
        # Upscaler (loads UpscaleEngine in the runner, calls process_video directly, streams
        # SeedVR2's output to the runner's own stdout). The subprocess-per-attempt path adds a
        # nested worker + stdout pipe purely for mid-segment thrash-kill; that pipe was the sole
        # source of the local "stall at first segment" hangs. Opt back in with
        # video.local_use_subprocess=true only if you specifically need the killable watchdog.
        "local_use_subprocess": bool(v.get("local_use_subprocess", False)),
        # (The per-card benchmark suite's sources are now a fixed, SHA-256-pinned set baked into
        # benchmark_clip.SOURCES, downloaded on demand; no config knob for them, docs 22.)
        # Adaptive batch tuning (item 9): on a multi-segment video, seed the batch from the
        # DB (or the pod's auto-pick), let the FIRST segment's real VRAM measurement refine
        # it up or down, then freeze + persist it (keyed by output-MP + card). Only active
        # for AUTO batch (an explicit batch_size override disables it). Default on.
        "auto_tune_batch":     bool(v.get("auto_tune_batch", True)),
        # Content-hash lineage (item 10): after a whole-video job completes, link the
        # source to its output by blake2b hash so a future video conciliation (roadmap
        # #5) can re-match them after a move/rename, exactly as the image runners do.
        # Hashing a multi-GB source over a network share costs a full read, so it is
        # gated here (default on) for the rare case a user wants it off. Clips are never
        # linked (their "source" is a virtual sub-range, not a replaceable file).
        "record_lineage":      bool(v.get("record_lineage", True)),
    }
    # Resident-vs-phase VRAM threshold for the VIDEO path, SEPARATE from the image one
    # (upscale.vram_resident_threshold_gb, default 40). A video temporal window's VAE decode is
    # far heavier than a single image, so keeping DiT+VAE resident is only safe on a much bigger
    # card; below the threshold the engine PHASES them (offloads the DiT to RAM before the decode,
    # freeing ~14 GB exactly when the decode needs it). Default 90 keeps PRO 6000 (96 GB) and up
    # resident and phases the 40-80 GB cards for video, where resident thrashes near the ceiling.
    # 0 = always phase. Injected into the worker settings so upscale_engine._resident_offload_device
    # reads it on the video pod (the image path keeps its own 40 GB value).
    _vrt = v.get("vram_resident_threshold_gb", 90)
    out["vram_resident_threshold_gb"] = float(90 if _vrt is None else _vrt)
    if out["use_10bit"]:
        out["video_backend"] = "ffmpeg"
    for k, val in (overrides or {}).items():
        if val is not None:
            out[k] = val
    return out


def log_video_settings(vcfg):
    """Echo the SeedVR2 / pipeline settings this run uses, to the log window and
    file, so a run (especially a failed one) records exactly what it was given.
    0/None knobs are shown as 'auto'/'per-video' (their effective value is resolved
    per job and logged there too)."""
    batch = vcfg.get("batch_size", 0) or 0
    chunk = vcfg.get("chunk_size", 0) or 0
    seed = vcfg.get("seed")
    log("Run settings (SeedVR2 / pipeline):")
    log(f"    default target {vcfg['target']}, skip-cutoff {vcfg['skip_cutoff_pct']:.0f}%, "
        f"segments ~{vcfg['segment_seconds']:.0f}s (max {vcfg['max_segment_seconds']:.0f}s)")
    log(f"    batch_size {batch or 'auto'}, chunk_size {chunk or 'auto'}, "
        f"temporal_overlap {vcfg['temporal_overlap']}, "
        f"seed {seed if seed is not None else 'per-video'}")
    log(f"    backend {vcfg['video_backend']}, "
        f"10-bit {'on' if vcfg['use_10bit'] else 'off'}, "
        f"output subdir '{vcfg['output_subdir']}'")
    log(f"    staging '{vcfg.get('work_root') or '(beside output)'}'")
    noise = float(vcfg.get("input_noise_scale", 0.0) or 0.0)
    log(f"    model {vcfg.get('dit_model', 'seedvr2_ema_7b_fp16.safetensors')}")
    log(f"    resident VRAM threshold {vcfg.get('vram_resident_threshold_gb', 90):.0f} GB "
        f"(cards >= this keep DiT+VAE resident; smaller cards phase the DiT to RAM for the decode)")
    log(f"    torch.compile {'on' if vcfg.get('compile', True) else 'off'}, "
        f"uniform batch {'on' if vcfg.get('uniform_batch_size', True) else 'off'}, "
        f"input noise {noise if noise > 0 else 'off'}")
    if vcfg.get("watchdog_enabled", True):
        log(f"    slow-segment watchdog on (warn at >={vcfg.get('watchdog_factor', 3.0):g}x "
            f"the run's healthy rate; notify-only, no auto-stop)")
    else:
        log("    slow-segment watchdog off")
    log(f"    auto-tune batch {'on' if vcfg.get('auto_tune_batch', True) else 'off'}"
        + (" (multi-segment auto-batch videos; seeds + freezes per card+output size)"
           if vcfg.get("auto_tune_batch", True) else ""))
    log(f"    record lineage {'on' if vcfg.get('record_lineage', True) else 'off'}"
        + (" (link source <-> output by content hash on completion)"
           if vcfg.get("record_lineage", True) else ""))


def resolve_batch(vcfg, target):
    b = int(vcfg.get("batch_size", 0) or 0)
    return b if b > 0 else DEFAULT_BATCH.get(target, 13)


def resolve_chunk(vcfg, batch):
    """Streaming chunk size. MUST be > 0 so output frames stream out instead of
    ballooning RAM (section 8). Auto = ~90 frames rounded to a multiple of the
    batch (the engine rounds up to a batch multiple anyway)."""
    c = int(vcfg.get("chunk_size", 0) or 0)
    if c > 0:
        return c
    k = max(1, round(90 / max(1, batch)))
    return batch * k


def per_video_seed(vcfg, rel):
    """One fixed seed per source video (6.2). An explicit config seed wins; else a
    stable hash of the rel path, so re-runs reproduce the same result and segment
    boundaries don't shift between runs."""
    if vcfg.get("seed") is not None:
        return int(vcfg["seed"])
    h = hashlib.blake2b(rel.encode("utf-8", "replace"), digest_size=8).hexdigest()
    return int(h, 16) % (2 ** 31 - 1)


# Adaptive batch tuning (item 9). A video must have at least this many segments before
# tuning engages: a 1-2 segment clip has nothing to amortise (and _fit_batch_to_frames
# already collapses a short clip to one batch), so it keeps the plain auto path.
MIN_TUNE_SEGMENTS = 2
# Output-megapixel key for the learned-batch DB row. A FINE grid: distinct real output sizes
# (portrait vs landscape box-fit outputs differ in area, so in VRAM ceiling) get their OWN row
# instead of colliding in a coarse bucket. History is still reused across nearby sizes because
# the readers look up the NEAREST recorded key >= the run's MP (get_learned_batch_ge) -- a
# ceiling measured at a larger output is a safe bound for a smaller one. Keying by target name
# alone would be wrong (box-fit makes output MP aspect-dependent).
_MP_KEY_MP = 0.05           # fine MP DB-key grid (matches db.MP_KEY_UNIT + sizer._MP_KEY_MP)


def _mp_bucket(mp):
    """Stable integer DB key for a per-frame output-megapixel value (fine _MP_KEY_MP grid)."""
    return int(round(max(0.0, float(mp or 0)) / _MP_KEY_MP))


def _request_batch(explicit_batch, tuned, learned):
    """The batch to REQUEST for a segment, by precedence: an explicit config batch wins
    (auto-tune is off then), else the frozen tuned value (auto-tuner), else an OOM-down-
    carried value (within-video recovery), else 0 = AUTO (the pod sizes it). Pure."""
    return explicit_batch or tuned or learned or 0


def updated_learned_batch(learned, first, final):
    """OOM-recovery carry-forward: after a segment FIRST tried `first` and ended at
    `final`, return the batch to reuse on this video's later segments.

    A drop (final < first) means the pod hit OOM and recovered to a smaller batch;
    all of a video's segments share the same resolution, so that smaller batch is
    safe for the rest and starting there skips the per-segment re-discovery (a failed
    forward pass + VRAM churn). Keep the smallest safe value seen; no drop leaves the
    prior learned value (possibly None) unchanged. Pure so it can be unit-tested apart
    from the heavy streaming loop."""
    if first and final and final < first:
        return final if learned is None else min(learned, final)
    return learned


# ─────────────────────────────────────────────
#  RUN BUDGET (installment caps)
# ─────────────────────────────────────────────

class RunBudget:
    """Tracks accumulated processing time and (approx) cost across a run, against
    the per-run caps. Cost is processing-seconds x $/h — an approximation (the pod
    is billed wall-clock including idle), but processing dominates, and it is only
    ever used to STOP earlier, never to spend more."""

    def __init__(self, minute_cap=0.0, cost_cap=0.0, cost_per_hr=None):
        self.minute_cap = float(minute_cap or 0)
        self.cost_cap = float(cost_cap or 0)
        self.cost_per_hr = cost_per_hr
        self.seconds = 0.0

    def add(self, secs):
        self.seconds += float(secs or 0)

    def exceeded(self):
        if self.minute_cap > 0 and self.seconds / 60.0 >= self.minute_cap:
            return f"per-run cap of {self.minute_cap:g} min reached"
        if self.cost_cap > 0 and self.cost_per_hr:
            spent = self.seconds / 3600.0 * float(self.cost_per_hr)
            if spent >= self.cost_cap:
                return f"per-run cost cap of ${self.cost_cap:g} reached (~${spent:.2f})"
        return None


# ─────────────────────────────────────────────
#  WALK + SPLIT helpers
# ─────────────────────────────────────────────

def iter_videos(src_root, skip_roots=None):
    """Yield (abs_path, rel_path) for every video under src_root **as they are
    discovered**, skipping the work area. Unsorted (discovery order): walking a
    large tree on a network drive can take many seconds, so a caller that wants
    live feedback iterates this and counts/streams while the walk is still
    running, then sorts the collected list itself. `walk_videos` is the sorted,
    fully-materialised convenience wrapper used by the headless runner.

    `skip_roots` prunes whole subtrees by absolute path (in addition to the always
    -pruned `.imgtbx_video` work dir). The caller passes the OUTPUT root here: when
    it lives inside the source tree (the default `<source>/__upscaled__`), the walk
    would otherwise re-read finished upscales as if they were fresh source videos.
    A skip_root outside src_root is simply never encountered (harmless)."""
    skip = set()
    for r in (skip_roots or ()):
        if r:
            try:
                skip.add(os.path.normcase(os.path.abspath(r)))
            except Exception:                            # noqa: BLE001
                pass
    for dirpath, dirnames, filenames in os.walk(src_root):
        keep = []
        for d in dirnames:
            if d == WORK_DIRNAME:
                continue
            if skip and os.path.normcase(os.path.abspath(os.path.join(dirpath, d))) in skip:
                continue                                 # the output subtree, don't rescan it
            keep.append(d)
        dirnames[:] = keep
        for name in filenames:
            if os.path.splitext(name)[1].lower() in VIDEO_EXTS:
                ap = os.path.join(dirpath, name)
                yield ap, os.path.relpath(ap, src_root)


def walk_videos(src_root, skip_roots=None):
    """All videos under src_root as a sorted (abs_path, rel_path) list, skipping the
    work area (and any `skip_roots`, e.g. the output tree) so a re-run never tries to
    upscale its own intermediates or its own finished outputs."""
    out = list(iter_videos(src_root, skip_roots=skip_roots))
    out.sort(key=lambda t: t[1].lower())
    return out


# Written into the split input dir the moment a split RUNS TO COMPLETION, so a
# resumed run can tell a finished split from one killed mid-write. Its absence (or a
# count/frame-sum that no longer matches the files on disk) means the split was
# interrupted (app closed, crash, power loss, the stall watchdog killing ffmpeg) and
# left a partial, often truncated segment set that must never be reused. See
# extract_clip's .part+os.replace guard (video_pipeline) for the same interrupted-write
# bug class, solved there but never on the split path until now.
_SPLIT_MARKER = "split.done"


def _write_split_marker(in_dir, segs, mode):
    """Record that a split completed: the segment count + total frame sum it produced
    (the ground truth a later reuse is validated against). Best-effort — a failed write
    only means the next run re-splits (safe, merely wasteful), never a wrong reuse."""
    try:
        with open(os.path.join(in_dir, _SPLIT_MARKER), "w", encoding="utf-8") as f:
            json.dump({"segment_count": len(segs),
                       "frame_sum": sum(s.frame_count for s in segs),
                       "mode": mode}, f)
    except OSError:
        pass


def _read_split_marker(in_dir):
    """The split.done marker dict, or None when absent/unreadable (an interrupted split,
    or a pre-marker resume dir from before this guard existed)."""
    try:
        with open(os.path.join(in_dir, _SPLIT_MARKER), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _clear_split_dir(in_dir):
    """Delete the seg_* files + split.done marker of an incomplete split so a fresh
    split can't inherit a stale/extra file (a re-split with FEWER segments would
    otherwise leave the old tail behind). Best-effort."""
    try:
        names = os.listdir(in_dir)
    except OSError:
        return
    for name in names:
        if (name.startswith("seg_") and name.endswith(vp.SEGMENT_EXT)) or name == _SPLIT_MARKER:
            try:
                os.remove(os.path.join(in_dir, name))
            except OSError:
                pass


def _enumerate_segments(in_dir):
    """Return [vp.Segment] for the seg_*.mkv already present in in_dir (a resumed
    split), each with its counted frame count."""
    segs = []
    if not os.path.isdir(in_dir):
        return segs
    for i, name in enumerate(sorted(os.listdir(in_dir))):
        if name.startswith("seg_") and name.endswith(vp.SEGMENT_EXT):
            p = os.path.join(in_dir, name)
            segs.append(vp.Segment(index=i, path=p, frame_count=vp.count_frames(p)))
    return segs


def split_is_complete(in_dir, segs):
    """(ok, reason) — is the split already in in_dir a COMPLETE, uncorrupted set a
    resume can trust? All three must hold, else the dir holds a partial/interrupted
    split that must be discarded and re-split (never streamed to the pod as a truncated
    deliverable):
      1. a split.done marker is present (the split ran to completion),
      2. the seg_NNNNN files are a gapless 0..N-1 sequence matching the marker's count,
      3. the counted frames on disk still sum to the marker's frame_sum (no segment
         truncated/lost since the split, which count_frames would under-count).
    Pure (no ffmpeg here; `segs` already carry counted frames), so it is unit-testable."""
    marker = _read_split_marker(in_dir)
    if marker is None:
        return False, "no split.done marker (the split did not finish)"
    for i, s in enumerate(segs):
        if os.path.basename(s.path) != f"seg_{i:05d}{vp.SEGMENT_EXT}":
            return False, f"segment files are not a gapless sequence (hole at index {i})"
    if len(segs) != marker.get("segment_count"):
        return False, (f"{len(segs)} segment file(s) on disk but the split recorded "
                       f"{marker.get('segment_count')}")
    frame_sum = sum(s.frame_count for s in segs)
    if frame_sum != marker.get("frame_sum"):
        return False, (f"segment frames sum to {frame_sum} but the split recorded "
                       f"{marker.get('frame_sum')} (a segment is truncated/corrupt)")
    return True, "ok"


def ensure_split(info, in_dir, vcfg):
    """Return ([vp.Segment], split_mode). Reuses an existing split (segment input files
    already present, e.g. a resumed run) so a `-c copy` re-split is skipped — but ONLY
    when that split is COMPLETE (split_is_complete: a split.done marker plus a matching
    segment count and frame sum on disk). A split killed mid-write leaves a partial,
    often truncated set; reusing it blindly upscales a truncated deliverable at real pod
    cost, caught only by the soft end-of-run drift warning. So an incomplete split is
    discarded and re-split. Splitting is deterministic, so a fresh split keeps any
    already-upscaled segments index-aligned."""
    existing = _enumerate_segments(in_dir)
    if existing:
        ok, why = split_is_complete(in_dir, existing)
        if ok:
            return existing, "reused"
        log(f"    discarding an incomplete split in the work area ({why}); re-splitting.")
        _clear_split_dir(in_dir)
    plan = vp.plan_split(info, vcfg["segment_seconds"], vcfg["max_segment_seconds"])
    segs = vp.split(info, plan, in_dir)
    _write_split_marker(in_dir, segs, plan.mode)
    return segs, plan.mode


# ─────────────────────────────────────────────
#  PASSTHROUGH ENGINE (no-pod testing / --passthrough)
# ─────────────────────────────────────────────

class PassthroughVideoEngine:
    """Local stand-in for RemoteVideoEngine: 'upscales' a segment by remuxing it
    to mp4 (no GPU), so the whole orchestration — split, per-segment streaming,
    resume, reassembly, drift — can be exercised end to end without a pod. The
    output is NOT upscaled; this is a pipeline test, not a real run."""

    device_name = "passthrough(local)"
    resident = False

    def __init__(self):
        self.last_segment_seconds = None

    def process_segment(self, src_path, dest_path, *, resolution, batch_size,
                        chunk_size, temporal_overlap=0, seed=None,
                        video_backend="opencv", use_10bit=False,
                        poll_interval=0, on_progress=None, should_stop=None):
        ffmpeg, _ = vp.find_ffmpeg()
        t0 = time.time()
        # mjpeg/h264 -> mp4 with -c copy keeps it lossless and fast; the real
        # worker returns an x264/x265 mp4, which concat handles identically.
        vp._run([ffmpeg, "-hide_banner", "-y", "-i", src_path,
                 "-map", "0:v:0", "-an", "-c", "copy", dest_path])
        self.last_segment_seconds = time.time() - t0
        n = vp.count_frames(dest_path)
        if on_progress:
            on_progress({"state": "done", "frames_written": n,
                         "seconds": self.last_segment_seconds})
        return n

    def telemetry(self):
        return None

    def close(self):
        pass


# ─────────────────────────────────────────────
#  PER-VIDEO PROCESSING
# ─────────────────────────────────────────────

def _work_dirs(out_video, work_base=None):
    """Per-video staging area (input + upscaled segments + clip extraction + resume
    state), returned as (in_dir, up_dir, work_root).

    With `work_base` given (the resolved vcfg["work_root"], a LOCAL disk by default)
    the dir is <work_base>/<name>_<hash>, keyed by a stable hash of the ABSOLUTE
    output path so a later run's segment-level resume finds the same folder (and two
    sources sharing a basename never collide). Staging on local disk keeps the long
    pod compute + fetch off the possibly-network output drive; it is removed on
    completion. A falsy `work_base` selects the legacy beside-the-output location
    (<out_dir>/.imgtbx_video/<base>), kept for direct callers/tests."""
    base = os.path.splitext(os.path.basename(out_video))[0]
    if work_base:
        key = hashlib.blake2b(os.path.abspath(out_video).encode("utf-8", "surrogatepass"),
                              digest_size=6).hexdigest()
        root = os.path.join(work_base, f"{base}_{key}")
    else:
        root = os.path.join(os.path.dirname(out_video), WORK_DIRNAME, base)
    return os.path.join(root, "in"), os.path.join(root, "up"), root


def _dir_size(path):
    """Best-effort total size (bytes) of a directory tree; 0 on any error. Used only
    for the sweep's 'freed X GB' log, so it must never raise."""
    total = 0
    try:
        for dirpath, _dirs, files in os.walk(path):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(dirpath, f))
                except OSError:
                    pass
    except Exception:                                   # noqa: BLE001
        pass
    return total


def _remove_job_staging(out_video, work_base):
    """Delete one job's staging dir (item 5). Called when a job leaves the queue for
    good (gave up, or a GUI remove) so its gigabytes of segments don't leak forever
    (cleanup otherwise only happened on success). Guarded to only ever delete a folder
    INSIDE the resolved staging base. Fail-safe: any error is logged, never raised."""
    try:
        if not out_video or not work_base:
            return
        root = _work_dirs(out_video, work_base)[2]
        if _path_within(root, work_base) and os.path.isdir(root):
            shutil.rmtree(root, ignore_errors=True)
    except Exception as exc:                            # noqa: BLE001
        debug_log("batch_video_upscale._remove_job_staging", exc=exc)


def _sweep_orphan_staging(conn, work_base):
    """Delete staging dirs under `work_base` that no active (non-terminal) job owns.
    A job's dir is intentionally kept for failed/partial jobs (resume needs it), so it
    is orphaned forever when a job is removed, gives up, or its output path changes (the
    hash-keyed dir is never revisited). The base is SHARED across source roots, so the
    keep-set is every active job's output path in the whole DB, never just this run's
    queue. Returns (count_removed, bytes_freed). Fail-safe: any error skips the sweep."""
    try:
        if not work_base or not os.path.isdir(work_base):
            return 0, 0
        keep = set()
        for out_path in db.get_active_video_output_paths(conn):
            if out_path:
                keep.add(os.path.normcase(os.path.abspath(_work_dirs(out_path, work_base)[2])))
        removed = freed = 0
        for name in os.listdir(work_base):
            root = os.path.join(work_base, name)
            if not os.path.isdir(root):
                continue                                # leave stray files alone
            if os.path.normcase(os.path.abspath(root)) in keep:
                continue                                # an active job owns this dir
            if not _path_within(root, work_base):       # belt-and-suspenders
                continue
            freed += _dir_size(root)
            shutil.rmtree(root, ignore_errors=True)
            removed += 1
        return removed, freed
    except Exception as exc:                            # noqa: BLE001
        debug_log("batch_video_upscale._sweep_orphan_staging", exc=exc)
        return 0, 0


def _path_within(child, parent):
    """True if `child` is `parent` or nested under it. Pure path math (no disk),
    case-insensitive on Windows via normcase, fail-safe False on bad input."""
    if not child or not parent:
        return False
    try:
        c = os.path.normcase(os.path.abspath(child))
        p = os.path.normcase(os.path.abspath(parent))
    except Exception:                                   # noqa: BLE001
        return False
    return c == p or c.startswith(p + os.sep)


def _record_video_lineage(conn, src_path, out_path, log=None):
    """Link a finished whole-video output to its source by content hash (item 10), so
    a future video conciliation (roadmap #5) can re-match the pair after a move/rename,
    exactly as the image runners do. Reuses the shared `lineage` table (src_hash ->
    upscaled_hash); the video has no tag stage, so tagged_hash stays NULL. Best-effort:
    a missing source, an unreadable file or any DB error only costs a fallback to name
    matching later, never the run. Skips a source path that no longer exists."""
    try:
        if not src_path or not out_path or not os.path.exists(src_path):
            return
        src_hash = db.hash_file_cached(conn, src_path)
        out_hash = db.hash_file_cached(conn, out_path)
        if not src_hash or not out_hash:
            return
        db.record_upscale_lineage(conn, src_hash, out_hash, src_path, out_path)
        if log:
            log("    lineage recorded (source <-> output linked by content hash)")
    except Exception as exc:                                # noqa: BLE001
        debug_log("batch_video_upscale._record_video_lineage", exc=exc)


def work_root_conflict(work_root, source_root):
    """Reason string if `work_root` is UNSAFE to stage a run over `source_root` into,
    else None. Staging INSIDE the scanned source tree (which includes an `__upscaled__`
    output subfolder under it) lets the walk re-read the run's own input segments as if
    they were fresh source videos; staging AT or ABOVE the source root conflates the two
    the same way. Pure path math, fail-safe."""
    if not work_root:                                   # empty = the local default, safe
        return None
    if _path_within(work_root, source_root):
        return (f"the staging work folder is inside the source folder being scanned "
                f"({work_root}): the scanner would re-read its own segments")
    if _path_within(source_root, work_root):
        return (f"the staging work folder contains the source folder being scanned "
                f"({work_root}): pick a folder outside the source tree")
    return None


def is_network_path(path):
    """Best-effort True if `path` is a network location (UNC or a mapped remote drive).
    Real detection is Windows-only (GetDriveTypeW); fail-safe False elsewhere / on any
    error. Used to WARN that staging on the network defeats local staging, not to block."""
    if not path:
        return False
    try:
        p = os.path.abspath(path)
        if p.startswith("\\\\") or p.startswith("//"):  # UNC
            return True
        if os.name != "nt":
            return False
        drive = os.path.splitdrive(p)[0]
        if not drive:
            return False
        import ctypes
        DRIVE_REMOTE = 4
        return ctypes.windll.kernel32.GetDriveTypeW(drive + "\\") == DRIVE_REMOTE
    except Exception:                                   # noqa: BLE001
        return False


# ─────────────────────────────────────────────
#  SCAN + ELIGIBILITY + PREPARE  (shared with the GUI, in-process, no GPU)
# ─────────────────────────────────────────────

def eligible_targets(width, height, skip_cutoff_pct=0):
    """Targets whose landscape box the source must be UPSCALED to fill (15.5). Uses
    the box-fit scale (max width OR max height), so orientation is handled: a portrait
    1080x1920 clip is eligible for 4K (-> 1215x2160) but NOT 1440p (that would DOWNSCALE
    its 1920 height to 1440). A 320x240 clip -> all three; a >=4K-filling clip -> none.
    The skip-cutoff drops a 'barely below' source (needs < cutoff% upscaling)."""
    if not width or not height:
        return []
    import video_estimate as ve
    # Ratios (2x/4x) first then presets, deduped by output size (feature #7). GPU feasibility
    # is applied by the GUI on top of this source-only set (the combobox filters to the
    # detected/selected card; the queue greys jobs the picked pod GPU can't reach).
    return ve.source_eligible_targets(width, height, skip_cutoff_pct)


def _tc(seconds):
    """A filesystem-safe timecode for a clip filename: 161.0 -> '02m41s'."""
    s = max(0, int(round(seconds or 0)))
    return f"{s // 60:02d}m{s % 60:02d}s"


def _clip_slug(label):
    """A short filesystem-safe slug from a user clip label (letters/digits/-/_ only),
    or '' when the label is empty/unusable."""
    import re
    s = re.sub(r"[^0-9A-Za-z_]+", "-", (label or "").strip()).strip("-")
    return s[:40]


def _output_path(output_root, rel, target, clip_id=0, clip_label=None,
                 clip_start=None, clip_end=None):
    """Mirror the source tree under output_root, encoding the target in the name:
    <output_root>/<rel_dir>/<base>_<target>.mp4 (15.1). For a CLIP (clip_id > 0,
    section 16.6) the range is encoded too so two scenes of the same source+target
    never collide: <base>_<label-or-mmss-mmss>_<target>.mp4, with clip_id as the
    last-resort uniquifier when two clips share a label/range."""
    rel_dir = os.path.dirname(rel)
    base = os.path.splitext(os.path.basename(rel))[0]
    if clip_id:
        tag = _clip_slug(clip_label)
        if not tag and clip_start is not None and clip_end is not None:
            tag = f"{_tc(clip_start)}-{_tc(clip_end)}"
        tag = tag or f"clip{clip_id}"
        return os.path.join(output_root, rel_dir, f"{base}_{tag}_{target}.mp4")
    return os.path.join(output_root, rel_dir, f"{base}_{target}.mp4")


def scan_file(conn, root_id, abs_path, rel):
    """Fast scan one video into video_files (15.4): ffprobe METADATA only (no demux,
    no hash), cached by (mtime, size) so an unchanged re-scan skips re-probing.
    Returns the video_files Row (cached or freshly probed). Best-effort: None on a
    probe failure (a corrupt/unreadable file)."""
    try:
        st = os.stat(abs_path)
    except OSError:
        return None
    if db.video_file_is_fresh(conn, root_id, rel, st.st_mtime, st.st_size):
        return db.get_video_file(conn, root_id, rel)
    try:
        info = vp.probe(abs_path)                 # fast: metadata only, count=False
    except Exception:
        return None
    db.upsert_video_file(conn, root_id, rel,
                         width=info.width, height=info.height,
                         vcodec=info.vcodec, acodec=info.acodec,
                         fps=float(info.fps), duration=info.duration,
                         mtime=round(st.st_mtime, 3), size=st.st_size,
                         probe_version=db.VIDEO_PROBE_VERSION)
    return db.get_video_file(conn, root_id, rel)


def reconcile_video_outputs(conn, root_id):
    """Drop DB records for upscaled outputs the user DELETED off disk: a 'done' job
    whose output file is gone is removed (with its segment rows), so it no longer shows
    as upscaled and can be re-prepared/re-queued. Only 'done' jobs are checked (others
    have no final file yet). Returns the [(rel, target), …] removed. Best-effort."""
    removed = []
    for o in db.get_video_outputs_all(conn, root_id):
        if o["status"] != "done":
            continue
        path = o["output_path"]
        if not path or not os.path.exists(path):
            db.delete_video_output(conn, root_id, o["rel_path"], o["target"])
            removed.append((o["rel_path"], o["target"]))
    return removed


def prepare_job(conn, root_id, source_root, output_root, rel, target, vcfg):
    """The Prepare step (15.3 step 5), run in-process by the GUI or the headless
    CLI: do the EXACT pass for this (file, target) (counted frames — the header
    lies), then enqueue a video_outputs job. Returns a dict with nb_frames and an
    approximate segment count for the estimate. Idempotent: re-preparing an
    existing job leaves its queue position and any progress intact."""
    abs_path = os.path.join(source_root, rel)
    info = vp.probe(abs_path, count=True)         # exact frames for cost + drift
    # Never enqueue a DOWNSCALE (target box smaller than the source): SeedVR2 is an
    # upscaler and a shrink would just degrade the video. Backstop for every path
    # (GUI + CLI); the GUI also blocks this up-front with a friendlier message.
    import video_estimate as ve
    if ve.classify_upscale(info.width, info.height, target) == "downscale":
        raise ValueError(f"{rel} is already {info.width}x{info.height}; target {target} "
                         f"would downscale it, not upscale.")
    db.upsert_video_file(conn, root_id, rel,
                         width=info.width, height=info.height,
                         vcodec=info.vcodec, acodec=info.acodec,
                         fps=float(info.fps), duration=info.duration,
                         nb_frames=info.nb_frames,
                         probe_version=db.VIDEO_PROBE_VERSION)
    existing = db.get_video_output(conn, root_id, rel, target)
    if existing is None:
        db.upsert_video_output(conn, root_id, rel, target, status="queued",
                               output_path=_output_path(output_root, rel, target),
                               queue_order=db.next_queue_order(conn, root_id))
    seg_secs = vcfg["segment_seconds"]
    approx_segments = max(1, math.ceil((info.duration or 0) / seg_secs)) if seg_secs else 1
    return {"nb_frames": info.nb_frames, "duration": info.duration,
            "segments": approx_segments,
            "width": info.width, "height": info.height}


def prepare_clip(conn, root_id, source_root, output_root, rel, target,
                 start, end, label, vcfg):
    """Enqueue a VIRTUAL clip job (the segment extractor, section 16.6/16.7): a new
    video_outputs row with a fresh clip_id and the marked [start, end) range. Unlike
    prepare_job this does NOT extract or count frames here — the subclip is
    materialised only at run time in the work area (source never touched), so
    clip_frames is the CFR approximation `(end-start) * fps`; the exact count and the
    drift reference come from the extracted clip's segments at run time. Caches the
    source properties (fast metadata) so the queue list can show them. Returns a dict
    with the assigned clip_id and the estimate inputs. Always adds a new clip (clip
    ids are unique per source), so calling twice makes two clips."""
    abs_path = os.path.join(source_root, rel)
    start, end = float(start), float(end)
    if end <= start:
        raise ValueError(f"clip end {end:.3f}s must be after start {start:.3f}s")
    info = vp.probe(abs_path)                      # metadata only (dims/fps/duration)
    import video_estimate as ve
    if ve.classify_upscale(info.width, info.height, target) == "downscale":
        raise ValueError(f"{rel} is already {info.width}x{info.height}; target {target} "
                         f"would downscale it, not upscale.")
    end = min(end, info.duration or end)           # clamp to the source length
    try:
        st = os.stat(abs_path)
        db.upsert_video_file(conn, root_id, rel,
                             width=info.width, height=info.height,
                             vcodec=info.vcodec, acodec=info.acodec,
                             fps=float(info.fps), duration=info.duration,
                             mtime=round(st.st_mtime, 3), size=st.st_size,
                             probe_version=db.VIDEO_PROBE_VERSION)
    except OSError:
        pass
    fps = float(info.fps) or 0.0
    clip_frames = max(1, round((end - start) * fps)) if fps else 0
    clip_id = db.next_clip_id(conn, root_id, rel)
    out_path = _output_path(output_root, rel, target, clip_id=clip_id,
                            clip_label=label, clip_start=start, clip_end=end)
    db.upsert_video_output(conn, root_id, rel, target, clip_id=clip_id,
                           status="queued", output_path=out_path,
                           queue_order=db.next_queue_order(conn, root_id),
                           clip_start=start, clip_end=end,
                           clip_label=(label or None), clip_frames=clip_frames)
    seg_secs = vcfg["segment_seconds"]
    approx_segments = max(1, math.ceil((end - start) / seg_secs)) if seg_secs else 1
    return {"clip_id": clip_id, "nb_frames": clip_frames, "duration": end - start,
            "segments": approx_segments,
            "width": info.width, "height": info.height}


def enqueue_folder(conn, root_id, source_root, output_root, target, vcfg):
    """Headless CLI helper: scan the tree and Prepare every ELIGIBLE video to
    `target` (the GUI does this per-file via prepare_job). Skips ineligible files
    and (target) jobs already done."""
    n = 0
    for abs_path, rel in walk_videos(source_root, skip_roots=[output_root]):
        row = scan_file(conn, root_id, abs_path, rel)
        if row is None:
            continue
        if target not in eligible_targets(row["width"], row["height"],
                                          vcfg["skip_cutoff_pct"]):
            continue
        done = db.get_video_output(conn, root_id, rel, target)
        if done is not None and done["status"] == "done":
            continue
        prepare_job(conn, root_id, source_root, output_root, rel, target, vcfg)
        n += 1
    return n


# ─────────────────────────────────────────────
#  PER-JOB PROCESSING (one queued (source, target))
# ─────────────────────────────────────────────

def process_job(engine, conn, root_id, source_root, job, vcfg, budget, index, total,
                notify_settings=None, slow_watch=None):
    """Process one queued (source, target) job. Returns ('done', seconds). Raises
    StopInstallment when a cap is hit mid-job (the rest stays pending, the job is
    marked 'partial'). `slow_watch` (a run-wide VideoSlowWatch) gets each completed
    segment's rate for the notify-only slow-segment signal (item 6)."""
    rel, target = job["rel_path"], job["target"]
    clip_id = job["clip_id"] or 0                  # 0 = whole file; >0 = a virtual clip
    job_start = time.time()                        # wall-clock, for the true per-file elapsed
    src_abs = os.path.join(source_root, rel)
    out_video = job["output_path"] or _output_path(
        os.path.join(source_root, vcfg["output_subdir"]), rel, target,
        clip_id=clip_id,
        clip_label=job["clip_label"] if clip_id else None,
        clip_start=job["clip_start"] if clip_id else None,
        clip_end=job["clip_end"] if clip_id else None)
    # Whole file: count frames now (CFR-mistag/drift). Clip: only dims/fps are needed
    # from the WHOLE source (a clip shares them); counting the whole 45-min source
    # would be wasted work, so probe metadata-only and count the extracted clip below.
    info = vp.probe(src_abs, count=not clip_id)
    # SeedVR2's --resolution is the output SHORT side. Compute it so the output FITS
    # the target's landscape box (max width OR max height) at the source aspect: for a
    # portrait clip the long side (height) hits the box cap, so the short side is < the
    # target (a vertical 4K clip is 1215x2160, NOT 2160x3840). Falls back to the target
    # short side if dims are unreadable.
    import video_estimate as ve
    resolution = ve.fit_short_side(info.width, info.height, target) or TARGET_RES.get(target, 1080)

    os.makedirs(os.path.dirname(out_video), exist_ok=True)
    in_dir, up_dir, work_root = _work_dirs(out_video, vcfg["work_root"])
    os.makedirs(up_dir, exist_ok=True)
    db.upsert_video_output(conn, root_id, rel, target, clip_id=clip_id,
                           status="splitting", output_path=out_video, skip_reason=None)

    # For a CLIP, materialise the marked [start, end) range into the work area and
    # treat THAT as the source to split/upscale/mux (section 16.7). The extraction is
    # virtual: it lives only in the hidden work dir (reused on resume, removed on
    # completion), so the source tree is never touched. `job_info` (the clip's frames,
    # duration and audio) drives the split, drift reference and audio mux; the whole-
    # source `info` is used only for the output resolution (a clip shares its dims).
    if clip_id:
        os.makedirs(work_root, exist_ok=True)
        clip_src = os.path.join(work_root, "clip" + vp.SEGMENT_EXT)
        if not os.path.exists(clip_src):
            vp.extract_clip(info, job["clip_start"], job["clip_end"], clip_src,
                            log=lambda m: log("    " + str(m).strip()))
        job_info = vp.probe(clip_src, count=True)
        mux_source = clip_src
        log(f"[{index}/{total}] {rel} clip #{clip_id} "
            f"[{_tc(job['clip_start'])}-{_tc(job['clip_end'])}]"
            + (f' "{job["clip_label"]}"' if job["clip_label"] else "")
            + f": {job_info.nb_frames}f extracted")
    else:
        job_info = info
        mux_source = src_abs

    gui_event("VIDEO", {"rel": rel, "target": target, "index": index, "total": total,
                        "width": info.width, "height": info.height,
                        "frames": job_info.nb_frames, "clip_id": clip_id})

    segs, mode = ensure_split(job_info, in_dir, vcfg)
    if not segs:
        raise vp.FFmpegError(f"split produced no segments for {rel}")
    log(f"[{index}/{total}] {rel} -> {target}: {info.width}x{info.height} "
        f"{job_info.nb_frames}f; {len(segs)} segment(s) ({mode})")

    # Black-output guard: a degenerate split re-encode (historically an interlaced source
    # through NVENC, which has no interlaced-HEVC path) yields all-black segments that
    # upscale to a black, audio-only deliverable — hours and real money for garbage, with
    # nothing to notice it. Catch it locally BEFORE streaming to the pod: if the source's
    # opening frames are clearly not black but the first segment's are, the re-encode
    # broke. Comparing against the SOURCE (a relative test) means a legitimately dark or
    # fade-from-black clip never false-trips. Fail-safe: a luma read returning None skips
    # the check rather than blocking the run.
    seg_luma = vp.mean_luma_head(segs[0].path)
    src_luma = vp.mean_luma_head(job_info.path)
    if vp.is_black_reencode(seg_luma, src_luma):
        raise vp.FFmpegError(
            f"first segment is black (mean luma {seg_luma:.1f}) though the source is not "
            f"({src_luma:.1f}) — the split re-encode produced black frames; aborting "
            f"{rel} before spending pod time. Check the source (interlacing/codec).")

    recorded = db.get_video_segments(conn, root_id, rel, target, clip_id=clip_id)
    if len(recorded) != len(segs):
        db.clear_video_segments(conn, root_id, rel, target, clip_id=clip_id)
        for s in segs:
            db.upsert_video_segment(conn, root_id, rel, target, s.index, clip_id=clip_id,
                                    in_frames=s.frame_count, status="pending")
        recorded = db.get_video_segments(conn, root_id, rel, target, clip_id=clip_id)

    db.upsert_video_output(conn, root_id, rel, target, clip_id=clip_id, status="streaming")
    # AUTO (batch 0 / overlap -1 / chunk 0) is resolved ON THE POD from its real VRAM
    # and the output resolution; an explicit value overrides. We pass the sentinels
    # through and log what the pod chose when it reports back (see _progress).
    batch = int(vcfg.get("batch_size", 0) or 0)        # 0 = auto
    overlap = int(vcfg["temporal_overlap"])            # -1 = auto
    # Defer chunk to the pod: it knows the segment's real frame count and the final
    # (frame-fitted) batch, so it sizes chunks to give whole-clip single passes / even
    # batches without a ragged tail. See worker _resolve_auto_params / _fit_batch_to_frames.
    chunk = 0
    seed = per_video_seed(vcfg, rel)
    log(f"    SeedVR2: short-side {resolution}px, "
        f"batch_size {batch if batch > 0 else 'auto'}, "
        f"chunk_size {chunk if chunk > 0 else 'auto'}, "
        f"temporal_overlap {overlap if overlap >= 0 else 'auto'}, "
        f"seed {seed}, backend {vcfg['video_backend']}, "
        f"10-bit {'on' if vcfg['use_10bit'] else 'off'} "
        f"(auto values resolved on the pod)")
    _resolved_logged = [False]
    _trace_logged = [False]
    _learned_batch = [None]      # OOM-corrected batch carried to this video's later segments
    _learn_logged = [False]      # log the carry-forward once per video, not per segment
    # Adaptive batch tuning (item 9): AUTO batch on a multi-segment video only. Seed from
    # the DB for this card + output-MP bucket; the first completed segment's measured
    # suggestion then refines + freezes it (all segments share one output size, so one
    # converged value serves the whole video AND respects torch.compile's single shape).
    gpu_id = os.environ.get("IMGTBX_GPU_OVERRIDE", "").strip()
    seg_out_mp = ve.output_megapixels(info.width, info.height, target)
    mp_bucket = _mp_bucket(seg_out_mp)
    tuning = bool(vcfg.get("auto_tune_batch", True) and gpu_id and not batch
                  and len(segs) >= MIN_TUNE_SEGMENTS)
    tuned = [None]               # frozen tuned batch (seed, then the converged measurement)
    converged = [False]
    if tuning:
        try:
            # Nearest recorded key >= this segment's MP (safe bound: a batch that fit a bigger
            # output fits a smaller one), so the fine key grid still seeds across nearby sizes.
            tuned[0] = db.get_learned_batch_ge(conn, gpu_id, mp_bucket)
        except Exception as exc:                       # noqa: BLE001 (fail-safe)
            debug_log("batch_video_upscale get_learned_batch", exc=exc)
        if tuned[0]:
            log(f"    auto-tune: seeding batch {tuned[0]} (learned for ~{seg_out_mp:.1f}MP "
                f"output on this card); the first segment refines it.")
    total_secs = 0.0

    for s in segs:
        up_path = os.path.join(up_dir, f"seg_{s.index:05d}.mp4")
        if recorded[s.index]["status"] == "done" and os.path.exists(up_path):
            continue                              # segment-level resume

        _seg_stats = {}          # per-segment peak VRAM + resolved batch (filled by _progress)
        _hb = [None]             # last "Processing" heartbeat wall time
        _seg_wall = [None]       # first 'running' wall time (segment runtime clock)

        def _progress(st, _i=s.index, _tot=s.frame_count, _stats=_seg_stats,
                      _hb=_hb, _sw=_seg_wall):
            # Periodic "Processing" heartbeat, emitted from the RUNNER (not the GUI) so it
            # lands in BOTH the console AND the on-disk log file (the GUI-side heartbeat
            # only reached the console, so a long segment left no on-disk trace). Every
            # ~60s while the pod reports 'running'.
            if st.get("state") == "running":
                now = time.time()
                if _sw[0] is None:
                    _sw[0] = now
                if _hb[0] is None:
                    _hb[0] = now
                elif now - _hb[0] >= 60:
                    _hb[0] = now
                    _fp = st.get("frames_processed") or 0
                    _spf = st.get("live_spf")
                    _spf_txt = f" · {_spf:.1f} s/frame" if _spf else ""
                    log(f"    Processing: {_fp}/{_tot} frames{_spf_txt} "
                        f"(runtime {fmt_hhmmss(now - _sw[0])})")
            rb = st.get("resolved_batch")
            if rb:
                _stats["batch"] = rb                 # final (after any OOM recovery)
                _stats.setdefault("first_batch", rb)  # what the pod tried FIRST this segment
            if rb and not _resolved_logged[0]:       # the pod reported its auto choices
                _resolved_logged[0] = True
                _noise = st.get("input_noise")
                # Real model passes over this segment, NOT streaming chunks: a window
                # advances by stride = batch - overlap new frames each pass (>= the clip
                # is one pass). The old frames/chunk_size reported chunks and badly
                # undercounted a small batch (e.g. batch 9/overlap 6 read as ~5).
                _rb = int(rb or 0)
                _ov = int(st.get("resolved_overlap") or 0)
                _stride = max(1, _rb - _ov)
                if not _tot:
                    _nb = 0
                elif _rb and _rb >= _tot:
                    _nb = 1
                else:
                    _nb = max(1, -(-(_tot - _ov) // _stride))
                _btxt = f", ~{_nb} batch(es) over {_tot}f" if _nb else ""
                log(f"    (pod resolved: batch_size {rb}, "
                    f"temporal_overlap {st.get('resolved_overlap')}{_btxt}, "
                    f"attention {st.get('resolved_attention') or '?'}, "
                    f"compile {'on' if st.get('compile_dit') else 'off'}, "
                    f"uniform {'on' if st.get('uniform_batch') else 'off'}, "
                    f"noise {_noise if _noise else 'off'})")
            pa = st.get("peak_alloc_gb")
            if pa is not None:                       # capture per-segment (logged below)
                _stats["alloc"] = pa
                _stats["reserved"] = st.get("peak_reserved_gb")
            sb = st.get("suggested_batch")           # auto-tuner: measured-anchored next batch
            if sb:
                _stats["suggested"] = sb
            # Diagnostic (temporary): the real per-phase cadence, so we can fix the
            # live s/frame + progress bar from ground truth instead of guesswork.
            _tr = st.get("phase_trace")
            if _tr and not _trace_logged[0] and (
                    st.get("state") == "done" or len(_tr) >= 16):
                _trace_logged[0] = True
                # File only: this E/U/D batch cadence is developer diagnostics — opaque
                # to a non-technical user, so it stays out of the GUI terminal but is kept
                # on disk for troubleshooting the progress-bar / s-per-frame calibration.
                log_file_only(f"    (pod phase trace [{st.get('phase_count')} lines]: "
                              f"{' '.join(_tr[:60])})")
            gui_event("SEGMENT", {"video_rel": rel, "target": target,
                                  "seg_index": _i, "total": len(segs),
                                  "state": st.get("state"),
                                  "frames_processed": st.get("frames_processed"),
                                  "seg_frames": _tot,
                                  "live_spf": st.get("live_spf"),
                                  "total_chunks": st.get("total_chunks"),
                                  "output_bytes": st.get("output_bytes")})

        # The batch to request: an explicit config batch (auto-tune off then) > the frozen
        # tuned/seeded value > a batch the pod already OOM-corrected on an earlier segment
        # of THIS video (same resolution, so the safe batch is the same; skips the per-
        # segment re-discovery) > 0 = AUTO (the pod sizes it).
        req_batch = _request_batch(batch, tuned[0], _learned_batch[0])
        if _learned_batch[0] and not tuned[0] and not _learn_logged[0]:
            _learn_logged[0] = True
            log(f"    reusing batch_size {req_batch} for the remaining segments "
                f"(OOM-corrected earlier in this video; skips re-discovery)")
        _progress({"state": "running"})
        n = engine.process_segment(
            s.path, up_path, resolution=resolution, batch_size=req_batch,
            chunk_size=chunk, temporal_overlap=overlap,
            seed=seed, video_backend=vcfg["video_backend"],
            use_10bit=vcfg["use_10bit"], on_progress=_progress,
            should_stop=_STOP.is_set)        # responsive Stop: abort the poll mid-segment
        secs = getattr(engine, "last_segment_seconds", None)
        db.upsert_video_segment(conn, root_id, rel, target, s.index, clip_id=clip_id,
                                status="done", out_frames=n, output_path=up_path,
                                seconds=secs)
        total_secs += secs or 0
        budget.add(secs)
        # Per-segment peak VRAM (working set + reserved/pool) on EVERY segment, not
        # just the first: on a long many-segment run this exposes VRAM creep toward the
        # card's wall and flags any OOM-recovery batch drop, so a thrash risk can be
        # seen as a rate across segments (tune batch_size against it). The peak is
        # per-segment (the pod resets the stats before each). See docs/video-upscaler.md.
        vram_txt = ""
        if _seg_stats.get("alloc") is not None:
            vram_txt = (f"; peak VRAM {_seg_stats['alloc']} GB working set / "
                        f"{_seg_stats.get('reserved')} GB reserved")
        drop_txt = ""
        b = _seg_stats.get("batch")
        first = _seg_stats.get("first_batch")
        if b is not None and first is not None and b < first:
            drop_txt = f"; batch dropped {first} -> {b} (OOM recovery)"
        # Learn the smallest safe batch so this video's later segments start there (a
        # forced batch that survives shows first == b, so it never re-learns).
        _learned_batch[0] = updated_learned_batch(_learned_batch[0], first, b)
        # Auto-tune convergence: after the FIRST completed segment, adopt the pod's
        # measured-anchored suggestion (up from a low seed, or down from a high one), FREEZE
        # it for the rest of this video (so torch.compile recompiles at most once), and
        # persist it for future videos of the same output size on this card.
        if tuning and not converged[0]:
            converged[0] = True
            sug = _seg_stats.get("suggested") or b or tuned[0]
            if sug and sug != tuned[0]:
                log(f"    auto-tune: batch {tuned[0] or 'auto'} -> {sug} (measured on this "
                    f"card for ~{seg_out_mp:.1f}MP output); frozen for this video.")
            tuned[0] = sug or tuned[0]
            try:
                db.put_learned_batch(conn, gpu_id, mp_bucket, tuned[0])
            except Exception as exc:                   # noqa: BLE001 (fail-safe)
                debug_log("batch_video_upscale put_learned_batch", exc=exc)
        # Phase breakdown (per-segment overhead beyond the pod time): submit=upload,
        # wait=submit->pod-done, fetch=download, finalize=local write. Since the pod now
        # times its whole retry loop (worker.py t0 before the loop), the reported `secs`
        # already includes any OOM-retry waste, so `gap = wait - secs` is just the small
        # pod-side scheduling/poll slop (near zero) -- the OOM-recovery tell is the
        # "batch dropped" flag above, not the gap. A large fetch would resurrect the
        # download theory with a hard MB/s number (it stays a few seconds / tens of MB).
        phase_txt = ""
        ph = getattr(engine, "last_phase", None)
        if ph:
            mb = (ph.get("bytes") or 0) / 1e6
            gap = ph.get("wait", 0) - (secs or 0)     # pod-side time NOT in the reported total
            phase_txt = (f" [submit {ph.get('submit', 0):.0f}s · wait {ph.get('wait', 0):.0f}s"
                         f" (gap {gap:+.0f}s) · fetch {ph.get('fetch', 0):.0f}s ({mb:.0f}MB)"
                         f" · finalize {ph.get('finalize', 0):.1f}s]")
        log(f"    segment {s.index + 1}/{len(segs)}: {n} frames"
            + (f" in {fmt_hhmmss(secs)}" if secs else "")
            + vram_txt + drop_txt + phase_txt)

        # Self-calibrate future estimates: record this segment's real OUTPUT
        # megapixels vs seconds against the GPU that ran it. Megapixels, not frames, so
        # the learned rate is aspect-independent (a 16:9 video costs more per frame than
        # the 4:3 benchmark). The key is the REMOTE card (IMGTBX_GPU_OVERRIDE, the same
        # key the remote GUI estimate uses) or, for a LOCAL run, the engine's own card
        # name (which the local GUI estimate reads back) -- so both paths feed the one
        # db.gpu_perf store. Passthrough has no gpu_id, so it never records. Fail-safe.
        gpu_id = (os.environ.get("IMGTBX_GPU_OVERRIDE", "").strip()
                  or getattr(engine, "gpu_id", "") or "")
        if gpu_id and n and secs:
            try:
                import video_estimate as ve
                out_mp = n * ve.output_megapixels(info.width, info.height, target)
                ve.record_run(conn, gpu_id, target, out_mp, secs)
            except Exception:                          # noqa: BLE001 (best-effort)
                pass

        # Slow-segment health signal (item 6): compare this segment's seconds-per-output-
        # megapixel to the run's healthy baseline; warn (once per episode) if far slower.
        # Notify-only, fail-safe: a warning misfire must never break a paid run.
        if slow_watch is not None and n and secs:
            try:
                import video_estimate as ve
                seg_out_mp = n * ve.output_megapixels(info.width, info.height, target)
                warn, ratio = slow_watch.observe(secs, seg_out_mp)
                if warn:
                    msg = (f"segment {s.index + 1}/{len(segs)} of {rel} ran ~{ratio:.1f}x "
                           f"slower than this run's baseline ({fmt_hhmmss(secs)} for {n} "
                           f"frames). Likely pod host contention; cost is accruing. The "
                           f"run continues (no auto-stop).")
                    log(f"    WATCHDOG: {msg}")
                    try:
                        notifications.notify(
                            notify_settings, "Video upscale: slow segment", msg, 0xF1C40F,
                            fields=[{"name": "Source", "value": rel}])
                    except Exception:                  # noqa: BLE001
                        pass
            except Exception as exc:                   # noqa: BLE001 (best-effort)
                debug_log("batch_video_upscale slow_watch", exc=exc)

        cap = budget.exceeded() or ("stopped by user" if _STOP.is_set() else None)
        remaining = [r for r in db.get_video_segments(conn, root_id, rel, target, clip_id=clip_id)
                     if r["status"] != "done"]
        if cap and remaining:
            db.upsert_video_output(conn, root_id, rel, target, clip_id=clip_id, status="partial")
            log(f"  PAUSED: {cap}; stopping after this segment "
                f"({len(remaining)} segment(s) left — resume to continue).")
            raise StopInstallment(cap)

    # All segments done -> reassemble (concat if >1 segment + audio mux) and
    # drift-check. The mp4 WRITES are staged on LOCAL disk, never straight onto the
    # (possibly network) output drive: finalizing an mp4 needs a seek-back to write
    # the moov atom, which an SMB share can leave unwritten -> an unplayable "no
    # codec" file (the segment fetch is a plain sequential write, so segments survive
    # but the reassembled file doesn't). We build the final locally, verify it
    # decodes, then move the finished bytes across (a plain copy, safe on any drive).
    up_paths = [os.path.join(up_dir, f"seg_{s.index:05d}.mp4") for s in segs]
    stage = tempfile.mkdtemp(prefix="imgtbx_video_reasm_")
    try:
        if len(up_paths) == 1:
            joined = up_paths[0]                  # one segment: concat is a no-op
        else:
            joined = os.path.join(stage, "concat.mp4")
            vp.concat_segments(up_paths, joined)
        staged_out = os.path.join(stage, "final" + (os.path.splitext(out_video)[1] or ".mp4"))
        # Mux from the CLIP (its clipped audio range) for a clip job, else the source.
        vp.mux_audio(joined, mux_source, staged_out, log=lambda m: log("    " + m.strip()))
        stage_info = vp.probe(staged_out, count=True)
        if not stage_info.nb_frames:              # fail loudly, never ship a dud
            raise vp.FFmpegError(f"reassembled output has no decodable frames: {rel}")
        shutil.move(staged_out, out_video)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    out_info = vp.probe(out_video, count=True)

    seg_rows = db.get_video_segments(conn, root_id, rel, target, clip_id=clip_id)
    seg_out = [r["out_frames"] for r in seg_rows]
    # Two numbers: the TRUE wall-clock elapsed for this file (upload + GPU upscale +
    # download + reassemble/mux + drift check) and the GPU-only portion (sum of the
    # segments' recorded seconds). The pod times its whole retry loop, so `pod_secs`
    # honestly includes any OOM-retry waste (real billed GPU time); `wall - pod_secs`
    # is then just the LOCAL pipeline overhead (upload/download over the tunnel, the
    # ffmpeg reassemble/mux, the drift probe), normally seconds to a couple of minutes.
    # An OOM-recovered segment shows up as the higher pod_secs plus its "batch dropped"
    # flag, not as a gap here (right-size the batch to fit first-try to avoid the waste).
    seg_secs = [r["seconds"] for r in seg_rows]
    pod_secs = sum(s for s in seg_secs if s)       # GPU time, correct across a resume (DB)
    wall = time.time() - job_start                 # this run's real elapsed for the file
    partial = ", timed only" if any(not s for s in seg_secs) else ""
    if pod_secs <= 0:
        runtime_txt = f" in {fmt_hhmmss(wall)}"
    elif wall >= pod_secs:                          # normal single-run: wall = GPU + overhead
        runtime_txt = f" in {fmt_hhmmss(wall)} ({fmt_hhmmss(pod_secs)} on the GPU{partial})"
    else:                                           # resumed: this run's wall covers only part
        runtime_txt = f" in {fmt_hhmmss(pod_secs)} on the GPU (resumed{partial})"
    reference = sum(s.frame_count for s in segs)   # SeedVR2 preserves per-segment frames (6.3)
    # Drift is checked against the CLIP (job_info) for a clip job, not the whole source.
    report = vp.check_drift(job_info, out_info, seg_out, reference_frames=reference)
    db.upsert_video_output(conn, root_id, rel, target, clip_id=clip_id, status="done",
                           output_path=out_video, out_frames=out_info.nb_frames)
    # Record source <-> output lineage for a WHOLE-video job (item 10). Skipped for a
    # clip: its "source" is a virtual sub-range of a longer file, not a replaceable
    # whole, so a src->clip link would be wrong for conciliation. src_abs is the untouched
    # source (never mux_source, which is the temp clip for a clip job).
    if not clip_id and vcfg.get("record_lineage", True):
        _record_video_lineage(conn, src_abs, out_video, log=log)
    shutil.rmtree(work_root, ignore_errors=True)
    try:
        os.rmdir(os.path.dirname(work_root))      # tidy the empty work parent
    except OSError:
        pass

    if report.ok:
        log(f"[{index}/{total}] DONE {rel} -> {os.path.basename(out_video)} "
            f"({out_info.nb_frames} frames){runtime_txt}")
    else:
        log(f"[{index}/{total}] DONE (review) {rel}{runtime_txt}: "
            + "; ".join(report.warnings))
    gui_event("VRESULT", {"rel": rel, "target": target, "clip_id": clip_id,
                          "outcome": "ok", "output_path": out_video,
                          "warnings": report.warnings})
    return "done", total_secs


# ─────────────────────────────────────────────
#  RUN THE QUEUE
# ─────────────────────────────────────────────

def _job_frames(conn, root_id, job):
    """Frame count that a queued job contributes to the progress denominator. For a
    CLIP that's its (approximate) clip_frames; for a whole-file job it's the source's
    counted nb_frames. 0 if not yet probed."""
    if job["clip_id"]:
        return job["clip_frames"] or 0
    vf = db.get_video_file(conn, root_id, job["rel_path"])
    return (vf["nb_frames"] if vf and vf["nb_frames"] else 0) or 0


def _job_exceeds_gpu(conn, root_id, job, max_out_mp, logged=None):
    """True if this job's OUTPUT megapixels exceed `max_out_mp` (the selected GPU's reach,
    #7), so it should be DEFERRED (left pending for a bigger card), not attempted. 0 =
    no cap. Logs each deferral once. A clip shares its source video's frame size."""
    if not max_out_mp:
        return False
    vf = db.get_video_file(conn, root_id, job["rel_path"])
    if not vf or not vf["width"] or not vf["height"]:
        return False                                   # can't judge -> let it run
    import video_estimate as ve
    mp = ve.output_megapixels(vf["width"], vf["height"], job["target"])
    if mp is None or mp <= max_out_mp + 1e-6:
        return False
    if logged is not None:
        key = (job["rel_path"], job["target"], job["clip_id"])
        if key not in logged:
            logged.add(key)
            log(f"  Deferred {job['rel_path']} -> {job['target']} ({mp:.1f} MP output): "
                f"exceeds the selected GPU's reach (~{max_out_mp:.1f} MP); left pending "
                f"for a larger card.")
    return True


def run_queue(engine, conn, root_id, source_root, vcfg, budget,
              notify_settings=None, auto_resume=False, notify_summary=True):
    """Process the durable queue (video_outputs not yet done) for this root against
    an injected engine. Returns a summary dict.

    When `auto_resume` is set (the self-healing supervisor, #6), a job that fails because
    the POD died (a liveness/transport error, per `_is_pod_failure`) is NOT counted as a
    source failure: run_queue re-raises `PodLost` so the supervisor can recover the pod and
    re-enter, and the job is left `pending`/`partial` so its finished segments resume. A
    bad-source worker error still counts against the job (item 4 give-up) as usual, and with
    `auto_resume` off the behaviour is exactly as before (mark failed, keep going).

    The LIVE DB queue is re-read before every job, so removing / reordering / adding a
    job in the GUI mid-run takes effect (the shared cache.db is the source of truth):
    a job the user removes after Start is no longer processed, and a reorder changes
    what runs next. Jobs already attempted this run are skipped so a 'failed' one is
    not retried in a loop."""
    # Refuse up-front (before any pod is rented) if the staging folder sits inside the
    # scanned source tree: the walk would re-read this run's own input segments as fresh
    # source videos. Empty work_root = the safe local default, so this only fires on a
    # bad custom Settings → Video → "Work folder (staging)".
    conflict = work_root_conflict(vcfg.get("work_root"), source_root)
    if conflict:
        log(f"ERROR: cannot start - {conflict}. Change Settings -> Video -> "
            f'"Work folder (staging)" to a folder outside the source tree '
            f"(leave it empty to use the default local folder).")
        return {"done": 0, "failed": 0, "stopped": conflict, "total": 0}

    # Reclaim staging dirs no active job owns anymore (removed / gave-up / re-targeted
    # jobs) before renting a pod: a long 4K video's segments are gigabytes and the base
    # is on the app drive by default. Item 5. Fail-safe.
    swept, freed = _sweep_orphan_staging(conn, vcfg.get("work_root"))
    if swept:
        log(f"Cleaned {swept} orphaned staging folder(s), {freed / 1024**3:.1f} GB freed.")

    initial = db.get_video_queue(conn, root_id)
    gui_event("QUEUE", [{"rel": j["rel_path"], "target": j["target"]} for j in initial])
    log(f"Queue: {len(initial)} job(s) under {source_root}.")

    done = failed = 0
    done_frames = 0
    stopped = None
    attempted = set()                  # (rel, target, clip_id) tried this run
    # Run-wide slow-segment health signal (item 6): one baseline across every segment of
    # every job this run (the pod is shared), notify-only.
    slow_watch = (VideoSlowWatch(factor=vcfg.get("watchdog_factor", 3.0))
                  if vcfg.get("watchdog_enabled", True) else None)
    # Feasibility cap (#7): the GUI passes the selected GPU's max feasible OUTPUT-MP so a job
    # whose target exceeds the card is DEFERRED (left pending for a bigger GPU), not attempted
    # and OOM-failed. 0 = no cap (headless / unknown card).
    max_out_mp = float(os.environ.get("IMGTBX_MAX_OUTPUT_MP", 0) or 0)
    deferred_logged = set()
    while not _STOP.is_set():
        pending = [j for j in db.get_video_queue(conn, root_id)
                   if (j["rel_path"], j["target"], j["clip_id"]) not in attempted
                   and not _job_exceeds_gpu(conn, root_id, j, max_out_mp, deferred_logged)]
        # Keep the GUI's progress denominator honest as the live queue changes.
        live_total = done_frames + sum(_job_frames(conn, root_id, j) for j in pending)
        gui_event("VTOTAL", live_total)
        if not pending:
            break
        job = pending[0]
        rel, target, clip_id = job["rel_path"], job["target"], job["clip_id"]
        attempted.add((rel, target, clip_id))
        idx = done + failed + 1
        total = idx + len(pending) - 1
        try:
            process_job(engine, conn, root_id, source_root, job, vcfg, budget,
                        idx, total, notify_settings=notify_settings, slow_watch=slow_watch)
            done += 1
            done_frames += _job_frames(conn, root_id, job)
        except StopInstallment as exc:
            stopped = str(exc)
            break
        except _RemoteVideoStopped:                    # responsive Stop, mid-segment
            db.upsert_video_output(conn, root_id, rel, target, clip_id=clip_id, status="partial")
            log(f"[{idx}/{total}] STOPPED {rel} -> {target} "
                f"(aborted mid-segment; pod will be torn down).")
            stopped = "stopped by user"
            break
        except _ThrashDetected as exc:                 # local GPU thrash/hang (#7)
            # A DEGRADATION episode, NOT a source failure: leave the job `partial` (its
            # finished segments stay on disk and resume next run), DON'T bump fail_count,
            # and stop the whole run loudly. Only a reboot reliably clears the degraded GPU
            # state (docs 14/17), so continuing to the next job would just thrash again.
            db.upsert_video_output(conn, root_id, rel, target, clip_id=clip_id, status="partial")
            log(f"[{idx}/{total}] GPU THRASH on {rel} -> {target}: {exc}")
            log("    The local GPU stopped making progress (VRAM thrashing / hung). This is "
                "a known SeedVR2 degradation of a long GPU session, not a code or source "
                "fault. Reboot to clear it, then re-run: the queue resumes from the first "
                "unfinished segment.")
            gui_event("VRESULT", {"rel": rel, "target": target, "clip_id": clip_id,
                                  "outcome": "fail", "error": f"gpu thrash: {exc}"})
            stopped = "gpu thrash"
            break
        except Exception as exc:                       # noqa: BLE001 — log, keep going
            # Self-healing (#6): a POD/transport death is not the source's fault. When the
            # supervisor is armed, hand the run back to it WITHOUT bumping this job's
            # fail_count or marking it failed — its finished segments stay on disk and the
            # redeployed pod resumes them. `attempted` was already updated, but a fresh
            # run_queue call (after redeploy) starts with an empty `attempted`, so the job
            # is retried on the new pod. A bad-source worker error falls through to the
            # normal per-job failure path below.
            if auto_resume and _is_pod_failure(exc):
                raise PodLost(str(exc)[:300], done=done, failed=failed,
                              done_frames=done_frames) from exc
            failed += 1
            reason = str(exc)[:300]
            n = db.bump_video_fail_count(conn, root_id, rel, target, clip_id=clip_id)
            status, skip_reason = _failure_disposition(n, reason)
            db.upsert_video_output(conn, root_id, rel, target, clip_id=clip_id,
                                   status=status, skip_reason=skip_reason)
            log(f"[{idx}/{total}] FAILED {rel} -> {target}: {exc}")
            if status == "skipped":
                log(f"    Gave up on {rel} -> {target} after {n} failed attempts; "
                    f"removed from the queue. Fix the source, then use Retry to re-queue it.")
                # It won't be resumed, so its staging segments are dead weight now.
                _remove_job_staging(job["output_path"], vcfg.get("work_root"))
            gui_event("VRESULT", {"rel": rel, "target": target, "clip_id": clip_id,
                                  "outcome": "fail", "error": reason})
    if _STOP.is_set() and stopped is None:
        stopped = "stopped by user"

    summary = {"done": done, "failed": failed, "stopped": stopped,
               "total": done + failed}
    # The supervisor (#6) sends ONE accumulated summary across all its redeploys, so it
    # passes notify_summary=False here to suppress a per-pod notification.
    if notify_summary:
        _notify_summary(notify_settings, summary, source_root)
    log(f"Summary: {done} done, {failed} failed of {done + failed}"
        + (f", stopped early ({stopped})" if stopped else "") + ".")
    return summary


def _failure_notice(started, exc):
    """(title, description) for the run-failure notification (item 9). `started`
    True = the engine was up and the run crashed mid-flight; False = the pod/engine
    never started (the path that used to be silent). Pure, so it is unit-tested."""
    if started:
        return "Video upscale failed", f"The run stopped on an error.\n{exc}"
    return ("Video upscale failed to start",
            f"Could not start the remote pod/engine.\n{exc}")


def _stop_notice(stopped):
    """(title, color, resume_hint) for a run that ended early, branched on the reason
    string in `stopped`. The old code hard-coded "paused (per-run cap)" for every early
    stop, so a user Stop or the work-root refusal was mislabeled as a cost cap. `stopped`
    is the raw reason from run_queue: "stopped by user", a per-run cap message
    ("per-run cap of ..."/"per-run cost cap of ..."), the work-root-conflict refusal, or
    "gpu thrash" (a local GPU degradation the watchdog stopped the run on).
    Pure, so it is unit-tested. resume_hint True = the rest of the queue stayed pending
    and re-running continues it; False = nothing was staged (the startup refusal)."""
    reason = (stopped or "").lower()
    if reason.startswith("per-run "):                 # covers cap AND cost-cap messages
        return "Video upscale paused (per-run cap)", 0xF1C40F, True
    if "staging work folder" in reason:               # work_root_conflict refusal
        return "Video upscale did not start", 0xE74C3C, False
    if "thrash" in reason:                            # local GPU degradation (#7)
        return "Video upscale stopped (GPU thrashing)", 0xE74C3C, True
    if reason == "stopped by user":
        return "Video upscale stopped", 0xF1C40F, True
    return "Video upscale stopped early", 0xF1C40F, True   # unknown reason: report plainly


def _notify_summary(notify_settings, summary, src_root):
    if not notify_settings:
        return
    try:
        stopped = summary["stopped"]
        resume_hint = False
        if summary["failed"]:                         # errors outrank an early stop
            color, title = 0xE74C3C, "Video upscale finished with errors"
            if stopped:
                resume_hint = _stop_notice(stopped)[2]
        elif stopped:
            title, color, resume_hint = _stop_notice(stopped)
        else:
            color, title = 0x2ECC71, "Video upscale complete"
        desc = (f"{summary['done']} done, {summary['failed']} failed of "
                f"{summary['total']} job(s).")
        if stopped:
            desc += f"\n{stopped}" + (", re-run to continue." if resume_hint else ".")
        notifications.notify(notify_settings, title, desc, color,
                             fields=[{"name": "Source", "value": src_root}])
    except Exception:
        pass


# ─────────────────────────────────────────────
#  SELF-HEALING SUPERVISOR (#6, "Auto-resume")
# ─────────────────────────────────────────────
# When the user arms Auto-resume, a video run survives LOSING ITS POD mid-run without
# babysitting: a connectivity blip reconnects to the surviving pod, a real pod loss waits
# (indefinitely, no time cap) for the IDENTICAL card to return to stock and redeploys it,
# and either way the run continues from the first unfinished segment (the segment-level
# resume already exists). The guardrail is MONEY, not time: nothing is billed while waiting
# for stock, so the only automatic stops are queue-done, user Stop, or the funds guard
# tripping (a redeployed, running pod draining the balance). No GPU substitution, ever
# (0.4.0): recovery redeploys only the card the user picked. See docs/future-features.md #6.

def _interruptible_sleep(seconds, stop):
    """Sleep up to `seconds`, waking early (within ~0.5s) once stop() is true. Keeps a
    long wait-for-stock backoff responsive to a user Stop."""
    end = time.monotonic() + max(0.0, seconds)
    while time.monotonic() < end:
        if stop():
            return
        time.sleep(min(0.5, end - time.monotonic()))


def _gpu_in_stock(gpus, gpu_id):
    """True if `gpu_id` is present (in stock) in an available_gpus() result. available_gpus
    already filters to in-stock cards, so mere presence means deployable. Pure."""
    return any(g.get("id") == gpu_id for g in (gpus or []))


def _notify_stock(notify_settings, source_root, label, *, recovered, waited=0.0):
    """One notification when a run enters the wait-for-stock state and one when the card
    returns, so an unattended check-in shows what the run is doing (and that $0 is being
    spent while it waits). Fail-safe."""
    if not notify_settings:
        return
    try:
        if recovered:
            title = "Video upscale resuming"
            desc = (f"{label} is back in stock after {fmt_hhmmss(waited)}; "
                    f"redeploying the same card and continuing.")
            color = 0x2ECC71                            # green
        else:
            title = "Video upscale waiting for GPU"
            desc = (f"{label} is out of stock. Auto-resume is waiting for it to return "
                    f"(no time cap, $0 billed while waiting).")
            color = 0xF1C40F                            # amber
        notifications.notify(notify_settings, title, desc, color,
                             fields=[{"name": "Source", "value": source_root}])
    except Exception:
        pass


def _wait_for_gpu_stock(list_gpus, gpu_id, gpu_label, *, notify_settings=None,
                        source_root="", on_event=log, stop=None, sleep=None,
                        first_interval=30.0, max_interval=300.0):
    """Block until `gpu_id` is deployable again, polling list_gpus() with backoff.

    No time cap (the guardrail is money, and nothing is billed while waiting) — only a user
    Stop ends it. Returns True when the card is back, False if stopped. Notifies on entering
    the wait and on recovery, and logs each poll. list_gpus / stop / sleep are injected so
    the loop is offline-testable."""
    stop = stop or _STOP.is_set
    sleep = sleep or _interruptible_sleep
    label = gpu_label or gpu_id
    t0 = time.monotonic()
    interval = first_interval
    entered = False
    while not stop():
        try:
            gpus = list_gpus()
        except Exception as exc:                       # noqa: BLE001 (keep waiting)
            on_event(f"Auto-resume: could not read GPU stock ({exc}); will retry.")
            gpus = []
        if _gpu_in_stock(gpus, gpu_id):
            if entered:
                waited = time.monotonic() - t0
                on_event(f"Auto-resume: {label} is back in stock after "
                         f"{fmt_hhmmss(waited)}; redeploying.")
                _notify_stock(notify_settings, source_root, label,
                              recovered=True, waited=waited)
            return True
        if not entered:
            entered = True
            on_event(f"Auto-resume: {label} is out of stock; waiting for it to return "
                     f"(no time cap, $0 billed while waiting, checking every "
                     f"{int(first_interval)}s, backing off to {int(max_interval)}s).")
            _notify_stock(notify_settings, source_root, label, recovered=False)
        else:
            on_event(f"Auto-resume: still waiting for {label} "
                     f"({fmt_hhmmss(time.monotonic() - t0)} elapsed, $0 billed).")
        sleep(interval, stop)
        interval = min(interval * 1.5, max_interval)    # back off (never a hard cap)
    return False


def _pod_still_running(list_pods, pod_id, name_prefix):
    """True if `pod_id` is still RUNNING under this run's pod-name prefix (a connectivity
    blip: reconnect, don't redeploy), False if it's gone (a real loss: redeploy). Any error
    reading the control plane is treated as 'gone' so the healer redeploys rather than
    hanging on a pod it can't confirm. `list_pods` is injected for testing."""
    if not pod_id:
        return False
    try:
        pods = list_pods()
    except Exception:                                  # noqa: BLE001 (can't confirm -> gone)
        return False
    for p in pods or []:
        if not isinstance(p, dict) or p.get("id") != pod_id:
            continue
        if p.get("desiredStatus") != "RUNNING":
            return False
        if name_prefix and not str(p.get("name", "")).startswith(name_prefix):
            return False
        return True
    return False


def _healer_gpu_target(cfg):
    """(gpu_id, gpu_label, region) the healer watches stock for and redeploys. The card is
    the one the user picked (IMGTBX_GPU_OVERRIDE, first entry = the picked card; the tail is
    only ever fallbacks the video run doesn't use), falling back to the configured default so
    a headless run still has a target. Region is the model volume's data center (a pod that
    mounts the volume MUST land there). Best-effort: region is None if it can't be read, and
    the redeploy still tries the card. Pure apart from the one volume_region lookup."""
    override = os.environ.get("IMGTBX_GPU_OVERRIDE", "").strip()
    rcfg = cfg.get("runpod", {})
    gpu_id = ((override.split(",")[0].strip() if override
               else rcfg.get("gpu_type_id", "")) or "").strip()
    region = None
    try:
        import runpod_client as rp
        vol = (rcfg.get("network_volume_id", "") or "").strip()
        if vol and rcfg.get("api_key"):
            region = rp.volume_region(rcfg["api_key"], vol)
    except Exception as exc:                           # noqa: BLE001 (best-effort)
        debug_log("batch_video_upscale._healer_gpu_target region", exc=exc)
    return gpu_id, gpu_id, region


def _final_summary(done, failed, stopped):
    return {"done": done, "failed": failed, "stopped": stopped, "total": done + failed}


def _run_supervised(session_factory, run_pass, *, pod_alive, wait_for_stock, is_stopped,
                    funds_tripped, close_session, on_event, notify_settings, source_root):
    """Drive a video run under Auto-resume (#6): loop deploy -> run one pass, healing a lost
    pod between passes, until the queue completes, the user stops, the funds guard trips, or
    a non-pod error escapes. Returns the accumulated final summary (and sends the ONE
    end-of-run notification, since each pass suppressed its own).

    All the RunPod-touching seams are injected so the loop is offline-testable:
      session_factory(first) -> (session, engine)   deploy+start a pod (first=True on the
                                                     very first attempt; it may raise, which
                                                     propagates as a clean fail-to-start)
      run_pass(engine)       -> summary              one run_queue pass (raises PodLost on a
                                                     mid-run pod death; returns on a clean end)
      pod_alive(session)     -> bool                 pod still up? (blip vs. loss)
      wait_for_stock()       -> bool                 block for the same card; False if stopped
      is_stopped()           -> bool                 user pressed Stop
      funds_tripped(session) -> bool                 the funds guard stopped this pod
      close_session(session, stop_pod)               teardown (stop_pod False keeps the pod)
    """
    acc_done = acc_failed = 0

    def finish(stopped):
        summary = _final_summary(acc_done, acc_failed, stopped)
        _notify_summary(notify_settings, summary, source_root)
        log(f"Summary: {acc_done} done, {acc_failed} failed of {acc_done + acc_failed}"
            + (f", stopped early ({stopped})" if stopped else "") + ".")
        return summary

    first = True
    while True:
        try:
            session, engine = session_factory(first)
        except Exception:
            if first:
                raise                                  # nothing ran yet: clean fail-to-start
            # A redeploy failed (a capacity race after stock appeared): wait and try again,
            # never substitute a card. A user Stop during the wait ends the run.
            on_event("Auto-resume: redeploy failed; waiting for the GPU again.")
            if not wait_for_stock():
                return finish("stopped by user")
            continue
        first = False
        try:
            summary = run_pass(engine)
        except PodLost as exc:
            acc_done += exc.done
            acc_failed += exc.failed
            reason = str(exc)
            # Hard stops (never redeploy): the funds guard deliberately stopped the pod, or
            # the user pressed Stop. Both look like a pod death to the engine; distinguishing
            # them here is what keeps the healer from redeploying INTO the funds guard.
            if funds_tripped(session):
                on_event("Auto-resume: the funds safety-net stopped the pod; not redeploying "
                         "(add funds or raise the cap, then re-run to continue).")
                close_session(session, True)
                return finish("funds safety-net")
            if is_stopped():
                close_session(session, True)
                return finish("stopped by user")
            if pod_alive(session):
                on_event(f"Auto-resume: connectivity blip; pod {session.pod_id} is still up, "
                         f"reconnecting. ({reason})")
                close_session(session, False)          # keep the pod; drop the local tunnel
                continue
            on_event(f"Auto-resume: pod lost ({reason}); terminating any remnant and waiting "
                     f"for the same GPU to return.")
            close_session(session, True)
            if not wait_for_stock():
                return finish("stopped by user")
            continue
        else:
            acc_done += summary["done"]
            acc_failed += summary["failed"]
            close_session(session, None)               # default teardown for the run's end
            return finish(summary.get("stopped"))


def main(argv=None):
    p = argparse.ArgumentParser(
        description="Video Upscaler runner (remote RunPod pod, or --local on this GPU).")
    p.add_argument("source", help="source folder (searched recursively)")
    p.add_argument("output", nargs="?",
                   help="output folder (default: <source>/<video.output_subdir>)")
    p.add_argument("--target", choices=list(TARGET_RES),
                   help="headless: scan + enqueue every eligible video to this "
                        "target before running. Omit to run the existing queue "
                        "(the GUI populates it via Prepare).")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--passthrough", action="store_true",
                      help="no pod: stream-copy each segment locally (pipeline test only)")
    mode.add_argument("--local", action="store_true",
                      help="no pod: upscale on THIS machine's GPU (feature #7). Uses the "
                           "local SeedVR2 engine + the predictive VRAM sizer + the "
                           "mid-segment thrash watchdog.")
    args = p.parse_args(argv)

    src_root = os.path.abspath(args.source)
    if not os.path.isdir(src_root):
        print(f"ERROR: source folder not found: {src_root}")
        return 2
    _open_log(src_root)               # persist this run to logs/video_<hash>.log

    cfg = _load_config()
    vcfg = resolve_video_cfg(cfg)
    notify_settings = notifications.resolve_settings(cfg)
    out_root = os.path.abspath(args.output) if args.output \
        else os.path.join(src_root, vcfg["output_subdir"])
    log_video_settings(vcfg)          # record the run's settings up front

    conn = db.get_conn()
    root_id = db.get_video_root_id(conn, src_root, out_root)

    # GUI mode (stdin is a pipe): watch for a "q" Stop line.
    if GUI_MODE:
        # Windows loader-lock deadlock guard (local in-process path). The stdin watcher blocks
        # in `for line in sys.stdin`; on Windows that blocked pipe read holds the loader lock.
        # Importing numpy AFTER it then DEADLOCKS: numpy's C-extension DLL spins up OpenBLAS/
        # OpenMP worker threads in its DllMain, and thread creation needs the loader lock the
        # blocked read is holding. The runner hangs before the engine loads; only a Stop (which
        # ends the watcher) unwedges it. Fix: import numpy FIRST, single-threaded, THEN start
        # the watcher. numpy ONLY -- do NOT pre-import torch here: SeedVR2 sets the CUDA
        # allocator backend and must import torch itself, and once numpy is loaded torch imports
        # cleanly after the thread anyway. In-process local path only (remote/subprocess don't
        # import numpy in this process).
        if args.local and not vcfg.get("local_use_subprocess"):
            try:
                import numpy       # noqa: F401  (the DLL that deadlocked; load it thread-free)
            except Exception:      # noqa: BLE001  (fail-safe: engine import will surface it)
                pass
        threading.Thread(target=_watch_stdin_for_stop, daemon=True).start()

    # Headless: pre-populate the queue from the folder (the GUI does this per-file).
    if args.target:
        n = enqueue_folder(conn, root_id, src_root, out_root, args.target, vcfg)
        log(f"Enqueued {n} eligible video(s) to {args.target}.")

    budget = RunBudget(vcfg["per_run_minute_cap"], vcfg["per_run_cost_cap"])
    # Auto-resume (#6): the GUI's per-run "Auto-resume" checkbox sets this env. Only
    # meaningful for a real pod run (a passthrough test has no pod to lose).
    auto_resume = (os.environ.get("IMGTBX_AUTO_RESUME", "").strip() in ("1", "true", "True")
                   and not args.passthrough and not args.local)
    engine = None                                  # set only for the passthrough path
    active = {"session": None}                     # the live session (armed + non-armed)
    started = {"v": False}                          # a pod started at least once (for _failure_notice)

    def _worker_cfg():
        # The pod's SeedVR2 engine reads the image upscale config; overlay the video-only
        # quality/speed knobs so they apply on the video pod without changing the Batch
        # Upscaler's behaviour. Engine flag names (see upscale_engine._build_args):
        # compile_dit/compile_vae, uniform_batch_size, input_noise_scale.
        wc = dict(cfg.get("upscale", {}))
        wc["compile_dit"]        = vcfg["compile"]
        wc["compile_vae"]        = vcfg["compile"]
        wc["uniform_batch_size"] = vcfg["uniform_batch_size"]
        wc["input_noise_scale"]  = vcfg["input_noise_scale"]
        wc["dit_model"]          = vcfg["dit_model"]
        # Video-specific resident threshold overrides the image one (default 40) so a video
        # temporal decode phases the DiT out on cards too small to hold it resident safely.
        wc["vram_resident_threshold_gb"] = vcfg["vram_resident_threshold_gb"]
        return wc

    def make_session(first=True):
        """Deploy + start one video pod, wire telemetry, and tell the GUI which pod is
        live. Returns (session, engine). On a start failure it closes the partial session
        (no orphan pod) before re-raising. Called once (non-armed) or once per redeploy
        (supervised); a redeploy transparently reuses a surviving pod via RemoteSession's
        _find_existing_pod (the blip path)."""
        from remote_run import RemoteSession
        sess = RemoteSession(cfg.get("runpod", {}), _worker_cfg(),
                             APP_ROOT, on_event=log, mode="video")
        try:
            eng = sess.start()
        except Exception:
            try:
                sess.close()
            except Exception as exc:               # noqa: BLE001 (fail-safe teardown)
                debug_log("batch_video_upscale.make_session close-on-fail", exc=exc)
            raise
        budget.cost_per_hr = sess.cost_per_hr
        # Tell the GUI which pod is live so the RunPod tab won't offer to terminate the
        # pod this (paid, multi-hour) run depends on, and hand it the pod's real billed
        # $/h for the live cost readout. NOTE: gui_event() JSON-encodes its payload, so
        # the Video tab reads the decoded value.
        gui_event("POD", sess.pod_id or "")
        if sess.cost_per_hr is not None:
            gui_event("RCOST", sess.cost_per_hr)
        ev = threading.Event()
        _start_remote_telemetry(eng, ev)           # stream CPU/RAM/GPU to the GUI row
        sess._tele_stop = ev
        active["session"] = sess
        started["v"] = True
        return sess, eng

    def close_session(sess, stop_pod=None):
        """Tear one session down (its telemetry + tunnel + pod). stop_pod: None = default
        (stop a pod we created), True = stop it, False = KEEP the pod (a blip reconnect
        reuses it). Clears the GUI's live-pod marker whenever a pod actually goes away."""
        ev = getattr(sess, "_tele_stop", None)
        if ev:
            ev.set()
        try:
            sess.close() if stop_pod is None else sess.close(stop_pod=stop_pod)
        except Exception as exc:                   # noqa: BLE001 (fail-safe teardown)
            debug_log("batch_video_upscale.close_session", exc=exc)
        if active.get("session") is sess:
            active["session"] = None
        if stop_pod is not False:                  # True or default => no pod remains
            gui_event("POD", "")

    try:
        if args.passthrough:
            log("Passthrough mode — no pod; segments are stream-copied locally.")
            engine = PassthroughVideoEngine()
            run_queue(engine, conn, root_id, src_root, vcfg, budget,
                      notify_settings=notify_settings)
        elif args.local:
            # Local mode (#7): the SeedVR2 work runs on THIS machine's GPU, no pod. Same
            # walk/split/reassemble/mux/resume pipeline as the remote path; only the injected
            # engine changes. The predictive VRAM sizer picks the batch (conn + gpu self-ID give
            # it learned self-calibration). DEFAULT is in-process (mirrors the image Batch
            # Upscaler); the opt-in subprocess mode adds the mid-segment thrash-kill watchdog.
            from local_video_engine import LocalVideoEngine
            repo_dir, model_dir = _local_seedvr2_paths(cfg)
            _sub = vcfg["local_use_subprocess"]
            log(f"Local mode — upscaling on this machine's GPU (no pod). "
                f"Engine: {'subprocess-per-attempt (thrash watchdog: ' + str(vcfg['thrash_stall_seconds']) + 's stall)' if _sub else 'in-process (like the image Batch Upscaler)'}.")
            # Gate torch.compile on a real compile-capability probe (shared with the
            # benchmark): without a C compiler it would HANG the first VAE compile under the
            # GUI's piped stdio ("stuck at first segment"). See gate_local_compile.
            local_cfg = _worker_cfg()
            gate_local_compile(local_cfg, log)
            engine = LocalVideoEngine(
                repo_dir, model_dir, local_cfg,
                conn=conn, gpu_id=None,
                use_subprocess=vcfg["local_use_subprocess"],
                thrash_stall_seconds=vcfg["thrash_stall_seconds"])
            run_queue(engine, conn, root_id, src_root, vcfg, budget,
                      notify_settings=notify_settings)
        elif auto_resume:
            import runpod_client as rp
            rcfg = cfg.get("runpod", {})
            api_key = rcfg.get("api_key", "")
            gpu_id, gpu_label, region = _healer_gpu_target(cfg)
            log("Auto-resume is ON: if the pod is lost, the run reconnects or waits for "
                f"the same GPU ({gpu_label or 'configured card'}) to return and continues "
                "from the first unfinished segment (no time cap; $0 billed while waiting).")
            _run_supervised(
                make_session,
                lambda eng: run_queue(eng, conn, root_id, src_root, vcfg, budget,
                                      notify_settings=notify_settings,
                                      auto_resume=True, notify_summary=False),
                pod_alive=lambda s: _pod_still_running(
                    lambda: rp.list_pods(api_key), s.pod_id, s.pod_name_prefix),
                wait_for_stock=lambda: _wait_for_gpu_stock(
                    lambda: rp.available_gpus(api_key, data_center_id=region),
                    gpu_id, gpu_label, notify_settings=notify_settings, source_root=src_root),
                is_stopped=_STOP.is_set,
                funds_tripped=lambda s: getattr(s, "_funds_tripped", False),
                close_session=close_session,
                on_event=log, notify_settings=notify_settings, source_root=src_root)
        else:
            _sess, engine = make_session(True)
            run_queue(engine, conn, root_id, src_root, vcfg, budget,
                      notify_settings=notify_settings)
    except Exception as exc:                       # noqa: BLE001
        # Surface a clean, actionable one-liner instead of a raw traceback (a
        # transient pod-GPU failure is the common case and is not a code bug, and a
        # non-technical user can't read a traceback); the full trace goes to the on-disk
        # run log only, never the GUI terminal.
        log("")
        log(f"Run failed: {exc}")
        log_file_only(traceback.format_exc())
        # Notify: a failure BEFORE the engine started (pod couldn't be created /
        # deployed) used to be silent, unlike the image runners' red "Engine Failed
        # to Start" alert; a mid-run crash that escapes run_queue is likewise
        # reported here (per-video failures are covered by the summary). Item 9.
        title, desc = _failure_notice(engine is not None or started["v"], exc)
        try:
            notifications.notify(
                notify_settings, title, desc, 0xE74C3C,   # red
                fields=[{"name": "Source",  "value": src_root},
                        {"name": "Machine", "value": os.environ.get("COMPUTERNAME", "unknown")}],
            )
        except Exception:
            pass
        return 1
    finally:
        # Close whatever is still live: the supervised path closes its final session itself
        # (active["session"] is then None), so this handles the non-armed run, a fail-to-
        # start, and the passthrough engine.
        sess = active.get("session")
        if sess is not None:
            close_session(sess, None)
        elif engine is not None:
            try:
                engine.close()
            except Exception as exc:               # noqa: BLE001 (fail-safe)
                debug_log("batch_video_upscale.main engine.close", exc=exc)
        _close_log()
    return 0


if __name__ == "__main__":
    sys.exit(main())
