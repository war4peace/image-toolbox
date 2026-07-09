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
against the real RemoteVideoEngine or, with --passthrough, a local no-pod engine
that stream-copies each segment (for testing the whole pipeline without a GPU).

Usage:
    python scripts/batch_video_upscale.py <source> [output] [--target 1080p]
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
    }
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
    log(f"    torch.compile {'on' if vcfg.get('compile', True) else 'off'}, "
        f"uniform batch {'on' if vcfg.get('uniform_batch_size', True) else 'off'}, "
        f"input noise {noise if noise > 0 else 'off'}")


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


def ensure_split(info, in_dir, vcfg):
    """Return ([vp.Segment], split_mode). Reuses an existing split (segment input
    files already present, e.g. a resumed run) so a `-c copy` re-split is skipped;
    otherwise plans and splits. Splitting is deterministic, so reuse keeps the
    upscaled outputs index-aligned."""
    existing = _enumerate_segments(in_dir)
    if existing:
        return existing, "reused"
    plan = vp.plan_split(info, vcfg["segment_seconds"], vcfg["max_segment_seconds"])
    segs = vp.split(info, plan, in_dir)
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
    out = []
    for t in TARGET_RES:
        s = ve.fit_scale(width, height, t)
        if s is not None and s > 1.0 + skip_cutoff_pct / 100.0:
            out.append(t)
    return out


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

def process_job(engine, conn, root_id, source_root, job, vcfg, budget, index, total):
    """Process one queued (source, target) job. Returns ('done', seconds). Raises
    StopInstallment when a cap is hit mid-job (the rest stays pending, the job is
    marked 'partial')."""
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
    _initial_batch = [None]      # batch the pod first resolved (to flag a later OOM drop)
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
                _stats["batch"] = rb
                if _initial_batch[0] is None:
                    _initial_batch[0] = rb
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

        _progress({"state": "running"})
        n = engine.process_segment(
            s.path, up_path, resolution=resolution, batch_size=batch,
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
        if b is not None and _initial_batch[0] is not None and b != _initial_batch[0]:
            drop_txt = f"; batch dropped {_initial_batch[0]} -> {b} (OOM recovery)"
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
        # megapixels vs seconds against the GPU that ran it (IMGTBX_GPU_OVERRIDE,
        # the same key the GUI estimate uses). Megapixels, not frames, so the
        # learned rate is aspect-independent (a 16:9 video costs more per frame than
        # the 4:3 benchmark). Remote runs only; fail-safe, never breaks a run.
        gpu_id = os.environ.get("IMGTBX_GPU_OVERRIDE", "").strip()
        if gpu_id and n and secs:
            try:
                import video_estimate as ve
                out_mp = n * ve.output_megapixels(info.width, info.height, target)
                ve.record_run(conn, gpu_id, target, out_mp, secs)
            except Exception:                          # noqa: BLE001 (best-effort)
                pass

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


def run_queue(engine, conn, root_id, source_root, vcfg, budget,
              notify_settings=None):
    """Process the durable queue (video_outputs not yet done) for this root against
    an injected engine. Returns a summary dict.

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

    initial = db.get_video_queue(conn, root_id)
    gui_event("QUEUE", [{"rel": j["rel_path"], "target": j["target"]} for j in initial])
    log(f"Queue: {len(initial)} job(s) under {source_root}.")

    done = failed = 0
    done_frames = 0
    stopped = None
    attempted = set()                  # (rel, target, clip_id) tried this run
    while not _STOP.is_set():
        pending = [j for j in db.get_video_queue(conn, root_id)
                   if (j["rel_path"], j["target"], j["clip_id"]) not in attempted]
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
                        idx, total)
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
        except Exception as exc:                       # noqa: BLE001 — log, keep going
            failed += 1
            db.upsert_video_output(conn, root_id, rel, target, clip_id=clip_id,
                                   status="failed", skip_reason=str(exc)[:300])
            log(f"[{idx}/{total}] FAILED {rel} -> {target}: {exc}")
            gui_event("VRESULT", {"rel": rel, "target": target, "clip_id": clip_id,
                                  "outcome": "fail", "error": str(exc)[:300]})
    if _STOP.is_set() and stopped is None:
        stopped = "stopped by user"

    summary = {"done": done, "failed": failed, "stopped": stopped,
               "total": done + failed}
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


def _notify_summary(notify_settings, summary, src_root):
    if not notify_settings:
        return
    try:
        if summary["failed"]:
            color, title = 0xE74C3C, "Video upscale finished with errors"
        elif summary["stopped"]:
            color, title = 0xF1C40F, "Video upscale paused (per-run cap)"
        else:
            color, title = 0x2ECC71, "Video upscale complete"
        desc = (f"{summary['done']} done, {summary['failed']} failed of "
                f"{summary['total']} job(s).")
        if summary["stopped"]:
            desc += f"\n{summary['stopped']} — re-run to continue."
        notifications.notify(notify_settings, title, desc, color,
                             fields=[{"name": "Source", "value": src_root}])
    except Exception:
        pass


def main(argv=None):
    p = argparse.ArgumentParser(description="Video Upscaler runner (RunPod-only).")
    p.add_argument("source", help="source folder (searched recursively)")
    p.add_argument("output", nargs="?",
                   help="output folder (default: <source>/<video.output_subdir>)")
    p.add_argument("--target", choices=list(TARGET_RES),
                   help="headless: scan + enqueue every eligible video to this "
                        "target before running. Omit to run the existing queue "
                        "(the GUI populates it via Prepare).")
    p.add_argument("--passthrough", action="store_true",
                   help="no pod: stream-copy each segment locally (pipeline test only)")
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
        threading.Thread(target=_watch_stdin_for_stop, daemon=True).start()

    # Headless: pre-populate the queue from the folder (the GUI does this per-file).
    if args.target:
        n = enqueue_folder(conn, root_id, src_root, out_root, args.target, vcfg)
        log(f"Enqueued {n} eligible video(s) to {args.target}.")

    budget = RunBudget(vcfg["per_run_minute_cap"], vcfg["per_run_cost_cap"])
    session = None
    engine = None
    tele_stop = threading.Event()
    try:
        if args.passthrough:
            log("Passthrough mode — no pod; segments are stream-copied locally.")
            engine = PassthroughVideoEngine()
        else:
            from remote_run import RemoteSession
            # The pod's SeedVR2 engine reads the image upscale config; overlay the
            # video-only quality/speed knobs so they apply on the video pod without
            # changing the Batch Upscaler's behaviour. Engine flag names (see
            # upscale_engine._build_args): compile_dit/compile_vae, uniform_batch_size,
            # input_noise_scale.
            worker_cfg = dict(cfg.get("upscale", {}))
            worker_cfg["compile_dit"]        = vcfg["compile"]
            worker_cfg["compile_vae"]        = vcfg["compile"]
            worker_cfg["uniform_batch_size"] = vcfg["uniform_batch_size"]
            worker_cfg["input_noise_scale"]  = vcfg["input_noise_scale"]
            worker_cfg["dit_model"]          = vcfg["dit_model"]
            session = RemoteSession(cfg.get("runpod", {}), worker_cfg,
                                    APP_ROOT, on_event=log, mode="video")
            engine = session.start()
            budget.cost_per_hr = session.cost_per_hr
            # Tell the GUI which pod is live so the RunPod tab won't offer to
            # terminate the pod this (paid, multi-hour) run depends on, and hand
            # it the pod's real billed $/h for the live cost readout. Mirrors the
            # image runners (batch_upscale / tag_and_rename). NOTE: this runner's
            # gui_event() JSON-encodes its payload, so the Video tab reads the
            # decoded value (data), not the raw payload string.
            gui_event("POD", session.pod_id or "")
            if session.cost_per_hr is not None:
                gui_event("RCOST", session.cost_per_hr)
            # Stream the pod's CPU/RAM/GPU to the GUI's remote-telemetry row.
            _start_remote_telemetry(engine, tele_stop)

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
        title, desc = _failure_notice(engine is not None, exc)
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
        tele_stop.set()
        if session is not None:
            session.close()
        elif engine is not None:
            engine.close()
        _close_log()
    return 0


if __name__ == "__main__":
    sys.exit(main())
