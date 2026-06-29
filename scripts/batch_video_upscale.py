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
import datetime
import argparse
import threading

try:
    import crash_logger
    crash_logger.install(notify=False)
except Exception:
    pass

# Make stdout robust to non-ASCII (unicode filenames, drift warnings, the §
# section marks) regardless of the console's code page — a headless runner must
# never die on a UnicodeEncodeError (the Windows console defaults to cp1252).
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

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

WORK_DIRNAME = ".imgtbx_video"        # per-output-tree work area for segments

GUI_MARKER = "@@TBX@@"


def _gui_mode():
    try:
        return sys.stdin is not None and not sys.stdin.isatty()
    except Exception:
        return False

GUI_MODE = _gui_mode()


def _ts():
    return datetime.datetime.now().strftime("%H:%M:%S")


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
    """Human-readable, timestamped progress to stdout (the GUI log pane / console)
    and, when a run log is open, to logs/video_<hash>.log for later troubleshooting.
    The file line carries a full date+time so the on-disk log reconstructs timing;
    stdout stays HH:MM:SS (the GUI window adds its own per-line stamp)."""
    sys.stdout.write(f"{_ts()} | {msg}\n")
    sys.stdout.flush()
    if _LOG_FH is not None:
        try:
            stamp = datetime.datetime.now().strftime("%Y-%m-%d | %H:%M:%S")
            _LOG_FH.write(f"{stamp} | {msg}\n")
        except Exception:                          # noqa: BLE001
            pass


def gui_event(kind, payload):
    """Machine-readable event line for the future GUI tab (phase 5); a no-op
    headless. Mirrors batch_upscale's GUI_MARKER convention.
      QUEUE|<json list of rel paths>
      VIDEO|<json>     – the video now being processed (rel, index, total, segments)
      SEGMENT|<json>   – per-segment progress (video_rel, seg_index, total, state)
      VRESULT|<json>   – [rel, "ok"|"fail"|"skip", output_path, warnings?]
      RTELEM|<json>    – a remote-pod telemetry sample (cpu/ram/gpu)
    """
    if GUI_MODE:
        sys.stdout.write(f"{GUI_MARKER}{kind}|{json.dumps(payload)}\n")
        sys.stdout.flush()


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


def _load_config():
    path = os.path.join(APP_ROOT, "config.json")
    if not os.path.exists(path):
        print(f"\nERROR: config.json not found at: {path}\n")
        sys.exit(1)
    with open(path, "r", encoding="utf-8-sig") as f:
        return json.load(f)


class StopInstallment(Exception):
    """Raised to end a run cleanly when a per-run cap is reached OR the user pressed
    Stop; the rest stays `pending`/`partial` and resumes next run."""


# Set when the GUI sends "q" on stdin (Stop). Checked between segments so a Stop
# finishes the current segment, then tears the pod down (15.3 step 8).
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
        "skip_cutoff_pct":     float(v.get("skip_cutoff_pct", 0) or 0),
        "segment_seconds":     float(v.get("segment_seconds", 60) or 60),
        "max_segment_seconds": float(v.get("max_segment_seconds", 120) or 120),
        "batch_size":          int(v.get("batch_size", 0) or 0),
        "temporal_overlap":    int(v.get("temporal_overlap", 0) or 0),
        "chunk_size":          int(v.get("chunk_size", 0) or 0),
        # opencv (SeedVR2 default, x264) unless 10-bit is asked for, which needs
        # the ffmpeg backend (x265 10-bit). Defaulting to opencv avoids a pod-side
        # "ffmpeg not in PATH" failure on a minimal image (deviates from the draft
        # §9 default deliberately; revisit once provision.sh ships ffmpeg on the pod).
        "video_backend":       v.get("video_backend", "opencv"),
        "use_10bit":           bool(v.get("use_10bit", False)),
        "seed":                v.get("seed"),               # None = per-video stable seed
        "per_run_minute_cap":  float(v.get("per_run_minute_cap", 0) or 0),
        "per_run_cost_cap":    float(v.get("per_run_cost_cap", 0) or 0),
    }
    if out["use_10bit"]:
        out["video_backend"] = "ffmpeg"
    for k, val in (overrides or {}).items():
        if val is not None:
            out[k] = val
    return out


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

def iter_videos(src_root):
    """Yield (abs_path, rel_path) for every video under src_root **as they are
    discovered**, skipping the work area. Unsorted (discovery order): walking a
    large tree on a network drive can take many seconds, so a caller that wants
    live feedback iterates this and counts/streams while the walk is still
    running, then sorts the collected list itself. `walk_videos` is the sorted,
    fully-materialised convenience wrapper used by the headless runner."""
    for dirpath, dirnames, filenames in os.walk(src_root):
        dirnames[:] = [d for d in dirnames if d != WORK_DIRNAME]
        for name in filenames:
            if os.path.splitext(name)[1].lower() in VIDEO_EXTS:
                ap = os.path.join(dirpath, name)
                yield ap, os.path.relpath(ap, src_root)


def walk_videos(src_root):
    """All videos under src_root as a sorted (abs_path, rel_path) list, skipping
    the work area so a re-run never tries to upscale its own intermediates."""
    out = list(iter_videos(src_root))
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
                        poll_interval=0, on_progress=None):
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

def _work_dirs(out_video):
    """Per-video work area beside the output: <out_dir>/.imgtbx_video/<base>/
    {in,up}. Survives between runs (segment-level resume); removed on completion."""
    base = os.path.splitext(os.path.basename(out_video))[0]
    root = os.path.join(os.path.dirname(out_video), WORK_DIRNAME, base)
    return os.path.join(root, "in"), os.path.join(root, "up"), root


# ─────────────────────────────────────────────
#  SCAN + ELIGIBILITY + PREPARE  (shared with the GUI, in-process, no GPU)
# ─────────────────────────────────────────────

def eligible_targets(width, height, skip_cutoff_pct=0):
    """Targets whose short side is meaningfully ABOVE the source short side
    (15.5). A 320x240 clip -> all three; a 1920x1080 clip -> 1440p/4K; a >=4K clip
    -> none. Honours the skip-cutoff so a 'barely below' source is not offered."""
    short = min(width, height) if width and height else 0
    if not short:
        return []
    return [t for t, res in TARGET_RES.items()
            if short < res * (1 - skip_cutoff_pct / 100.0)]


def _output_path(output_root, rel, target):
    """Mirror the source tree under output_root, encoding the target in the name:
    <output_root>/<rel_dir>/<base>_<target>.mp4 (15.1)."""
    rel_dir = os.path.dirname(rel)
    base = os.path.splitext(os.path.basename(rel))[0]
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
                         mtime=round(st.st_mtime, 3), size=st.st_size)
    return db.get_video_file(conn, root_id, rel)


def prepare_job(conn, root_id, source_root, output_root, rel, target, vcfg):
    """The Prepare step (15.3 step 5), run in-process by the GUI or the headless
    CLI: do the EXACT pass for this (file, target) (counted frames — the header
    lies), then enqueue a video_outputs job. Returns a dict with nb_frames and an
    approximate segment count for the estimate. Idempotent: re-preparing an
    existing job leaves its queue position and any progress intact."""
    abs_path = os.path.join(source_root, rel)
    info = vp.probe(abs_path, count=True)         # exact frames for cost + drift
    db.upsert_video_file(conn, root_id, rel,
                         width=info.width, height=info.height,
                         vcodec=info.vcodec, acodec=info.acodec,
                         fps=float(info.fps), duration=info.duration,
                         nb_frames=info.nb_frames)
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


def enqueue_folder(conn, root_id, source_root, output_root, target, vcfg):
    """Headless CLI helper: scan the tree and Prepare every ELIGIBLE video to
    `target` (the GUI does this per-file via prepare_job). Skips ineligible files
    and (target) jobs already done."""
    n = 0
    for abs_path, rel in walk_videos(source_root):
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
    resolution = TARGET_RES.get(target, 1080)
    src_abs = os.path.join(source_root, rel)
    out_video = job["output_path"] or _output_path(
        os.path.join(source_root, vcfg["output_subdir"]), rel, target)
    info = vp.probe(src_abs, count=True)          # counted frames: CFR-mistag (14) + drift
    gui_event("VIDEO", {"rel": rel, "target": target, "index": index, "total": total,
                        "width": info.width, "height": info.height,
                        "frames": info.nb_frames})

    os.makedirs(os.path.dirname(out_video), exist_ok=True)
    in_dir, up_dir, work_root = _work_dirs(out_video)
    os.makedirs(up_dir, exist_ok=True)
    db.upsert_video_output(conn, root_id, rel, target, status="splitting",
                           output_path=out_video, skip_reason=None)

    segs, mode = ensure_split(info, in_dir, vcfg)
    if not segs:
        raise vp.FFmpegError(f"split produced no segments for {rel}")
    log(f"[{index}/{total}] {rel} -> {target}: {info.width}x{info.height} "
        f"{info.nb_frames}f; {len(segs)} segment(s) ({mode})")

    recorded = db.get_video_segments(conn, root_id, rel, target)
    if len(recorded) != len(segs):
        db.clear_video_segments(conn, root_id, rel, target)
        for s in segs:
            db.upsert_video_segment(conn, root_id, rel, target, s.index,
                                    in_frames=s.frame_count, status="pending")
        recorded = db.get_video_segments(conn, root_id, rel, target)

    db.upsert_video_output(conn, root_id, rel, target, status="streaming")
    batch = resolve_batch(vcfg, target)
    chunk = resolve_chunk(vcfg, batch)
    seed = per_video_seed(vcfg, rel)
    total_secs = 0.0

    for s in segs:
        up_path = os.path.join(up_dir, f"seg_{s.index:05d}.mp4")
        if recorded[s.index]["status"] == "done" and os.path.exists(up_path):
            continue                              # segment-level resume

        def _progress(st, _i=s.index, _tot=s.frame_count):
            gui_event("SEGMENT", {"video_rel": rel, "target": target,
                                  "seg_index": _i, "total": len(segs),
                                  "state": st.get("state"),
                                  "frames_processed": st.get("frames_processed"),
                                  "seg_frames": _tot,
                                  "output_bytes": st.get("output_bytes")})

        _progress({"state": "running"})
        n = engine.process_segment(
            s.path, up_path, resolution=resolution, batch_size=batch,
            chunk_size=chunk, temporal_overlap=vcfg["temporal_overlap"],
            seed=seed, video_backend=vcfg["video_backend"],
            use_10bit=vcfg["use_10bit"], on_progress=_progress)
        secs = getattr(engine, "last_segment_seconds", None)
        db.upsert_video_segment(conn, root_id, rel, target, s.index, status="done",
                                out_frames=n, output_path=up_path, seconds=secs)
        total_secs += secs or 0
        budget.add(secs)
        log(f"    segment {s.index + 1}/{len(segs)}: {n} frames"
            + (f" in {secs:.1f}s" if secs else ""))

        cap = budget.exceeded() or ("stopped by user" if _STOP.is_set() else None)
        remaining = [r for r in db.get_video_segments(conn, root_id, rel, target)
                     if r["status"] != "done"]
        if cap and remaining:
            db.upsert_video_output(conn, root_id, rel, target, status="partial")
            log(f"  PAUSED: {cap}; stopping after this segment "
                f"({len(remaining)} segment(s) left — resume to continue).")
            raise StopInstallment(cap)

    # All segments done -> reassemble locally (concat + audio mux) and drift-check.
    up_paths = [os.path.join(up_dir, f"seg_{s.index:05d}.mp4") for s in segs]
    concat_path = os.path.join(work_root, "concat.mp4")
    vp.concat_segments(up_paths, concat_path)
    vp.mux_audio(concat_path, src_abs, out_video, log=lambda m: log("    " + m.strip()))
    out_info = vp.probe(out_video, count=True)

    seg_out = [r["out_frames"] for r in db.get_video_segments(conn, root_id, rel, target)]
    reference = sum(s.frame_count for s in segs)   # SeedVR2 preserves per-segment frames (6.3)
    report = vp.check_drift(info, out_info, seg_out, reference_frames=reference)
    db.upsert_video_output(conn, root_id, rel, target, status="done",
                           output_path=out_video, out_frames=out_info.nb_frames)
    shutil.rmtree(work_root, ignore_errors=True)
    try:
        os.rmdir(os.path.dirname(work_root))      # tidy the empty work parent
    except OSError:
        pass

    if report.ok:
        log(f"[{index}/{total}] DONE {rel} -> {os.path.basename(out_video)} "
            f"({out_info.nb_frames} frames)")
    else:
        log(f"[{index}/{total}] DONE (review) {rel}: " + "; ".join(report.warnings))
    gui_event("VRESULT", {"rel": rel, "target": target, "outcome": "ok",
                          "output_path": out_video, "warnings": report.warnings})
    return "done", total_secs


# ─────────────────────────────────────────────
#  RUN THE QUEUE
# ─────────────────────────────────────────────

def run_queue(engine, conn, root_id, source_root, vcfg, budget,
              notify_settings=None):
    """Process the durable queue (video_outputs not yet done) for this root against
    an injected engine. Returns a summary dict."""
    jobs = db.get_video_queue(conn, root_id)
    gui_event("QUEUE", [{"rel": j["rel_path"], "target": j["target"]} for j in jobs])
    log(f"Queue: {len(jobs)} job(s) under {source_root}.")

    done = failed = 0
    stopped = None
    for i, job in enumerate(jobs, 1):
        if _STOP.is_set():
            stopped = "stopped by user"
            break
        rel, target = job["rel_path"], job["target"]
        try:
            process_job(engine, conn, root_id, source_root, job, vcfg, budget,
                        i, len(jobs))
            done += 1
        except StopInstallment as exc:
            stopped = str(exc)
            break
        except Exception as exc:                       # noqa: BLE001 — log, keep going
            failed += 1
            db.upsert_video_output(conn, root_id, rel, target, status="failed",
                                   skip_reason=str(exc)[:300])
            log(f"[{i}/{len(jobs)}] FAILED {rel} -> {target}: {exc}")
            gui_event("VRESULT", {"rel": rel, "target": target, "outcome": "fail",
                                  "error": str(exc)[:300]})

    summary = {"done": done, "failed": failed, "stopped": stopped, "total": len(jobs)}
    _notify_summary(notify_settings, summary, source_root)
    log(f"Summary: {done} done, {failed} failed of {len(jobs)}"
        + (f", stopped early ({stopped})" if stopped else "") + ".")
    return summary


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
            session = RemoteSession(cfg.get("runpod", {}), cfg.get("upscale", {}),
                                    APP_ROOT, on_event=log, mode="video")
            engine = session.start()
            budget.cost_per_hr = session.cost_per_hr
            # Stream the pod's CPU/RAM/GPU to the GUI's remote-telemetry row.
            _start_remote_telemetry(engine, tele_stop)

        run_queue(engine, conn, root_id, src_root, vcfg, budget,
                  notify_settings=notify_settings)
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
