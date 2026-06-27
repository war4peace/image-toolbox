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
import time
import shutil
import hashlib
import datetime
import argparse

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


def log(msg):
    """Human-readable, timestamped progress to stdout (the GUI log pane / console)."""
    sys.stdout.write(f"{_ts()} | {msg}\n")
    sys.stdout.flush()


def gui_event(kind, payload):
    """Machine-readable event line for the future GUI tab (phase 5); a no-op
    headless. Mirrors batch_upscale's GUI_MARKER convention.
      QUEUE|<json list of rel paths>
      VIDEO|<json>     – the video now being processed (rel, index, total, segments)
      SEGMENT|<json>   – per-segment progress (video_rel, seg_index, total, state)
      VRESULT|<json>   – [rel, "ok"|"fail"|"skip", output_path, warnings?]
    """
    if GUI_MODE:
        sys.stdout.write(f"{GUI_MARKER}{kind}|{json.dumps(payload)}\n")
        sys.stdout.flush()


def _load_config():
    path = os.path.join(APP_ROOT, "config.json")
    if not os.path.exists(path):
        print(f"\nERROR: config.json not found at: {path}\n")
        sys.exit(1)
    with open(path, "r", encoding="utf-8-sig") as f:
        return json.load(f)


class StopInstallment(Exception):
    """Raised to end a run cleanly when a per-run cap is reached; the rest stays
    `pending` and resumes next run."""


# ─────────────────────────────────────────────
#  CONFIG RESOLUTION
# ─────────────────────────────────────────────

def resolve_video_cfg(cfg, overrides=None):
    """Merge the config.json `video` section with sane defaults and any CLI
    overrides, returning a plain dict the runner reads."""
    v = dict(cfg.get("video", {}) or {})
    out = {
        "target":              v.get("target", "1080p"),
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

def walk_videos(src_root):
    """Yield (abs_path, rel_path) for every video under src_root, skipping the
    work area so a re-run never tries to upscale its own intermediates."""
    out = []
    for dirpath, dirnames, filenames in os.walk(src_root):
        dirnames[:] = [d for d in dirnames if d != WORK_DIRNAME]
        for name in sorted(filenames):
            if os.path.splitext(name)[1].lower() in VIDEO_EXTS:
                ap = os.path.join(dirpath, name)
                out.append((ap, os.path.relpath(ap, src_root)))
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


def process_one(engine, conn, root_id, src_abs, rel, out_root, vcfg, budget,
                index, total):
    """Process one source video. Returns ('done'|'skipped', processing_seconds).
    Raises StopInstallment when a cap is hit mid-video (the rest stays pending)."""
    target = vcfg["target"]
    resolution = TARGET_RES.get(target, 1080)
    info = vp.probe(src_abs, count=True)
    gui_event("VIDEO", {"rel": rel, "index": index, "total": total,
                        "width": info.width, "height": info.height,
                        "frames": info.nb_frames, "target": target})

    # Near-target skip (parity with the image path's skip-cutoff): if the short
    # side already meets the target (within the cutoff), there is nothing to gain.
    eff = min(info.width, info.height) if info.width and info.height else 0
    threshold = resolution * (1 - vcfg["skip_cutoff_pct"] / 100.0)
    if eff and eff >= threshold:
        reason = f"short side {eff}px >= {target} target ({threshold:.0f}px)"
        db.upsert_video_file(conn, root_id, rel, width=info.width, height=info.height,
                             fps=float(info.fps), frames=info.nb_frames,
                             duration=info.duration, target=target,
                             status="skipped", skip_reason=reason)
        log(f"[{index}/{total}] SKIP {rel} — {reason}")
        gui_event("VRESULT", {"rel": rel, "outcome": "skip", "reason": reason})
        return "skipped", 0.0

    out_video = os.path.join(out_root, os.path.splitext(rel)[0] + ".mp4")
    os.makedirs(os.path.dirname(out_video), exist_ok=True)
    in_dir, up_dir, work_root = _work_dirs(out_video)
    os.makedirs(up_dir, exist_ok=True)

    db.upsert_video_file(conn, root_id, rel, width=info.width, height=info.height,
                         fps=float(info.fps), frames=info.nb_frames,
                         duration=info.duration, target=target, status="splitting",
                         output_path=out_video, skip_reason=None)

    segs, mode = ensure_split(info, in_dir, vcfg)
    if not segs:
        raise vp.FFmpegError(f"split produced no segments for {rel}")
    log(f"[{index}/{total}] {rel}: {info.width}x{info.height} {info.nb_frames}f "
        f"-> {target}; {len(segs)} segment(s) ({mode})")

    # (Re)align the DB segment plan with the split.
    recorded = db.get_video_segments(conn, root_id, rel)
    if len(recorded) != len(segs):
        db.clear_video_segments(conn, root_id, rel)
        for s in segs:
            db.upsert_video_segment(conn, root_id, rel, s.index,
                                    in_frames=s.frame_count, status="pending")
        recorded = db.get_video_segments(conn, root_id, rel)

    db.upsert_video_file(conn, root_id, rel, status="streaming")
    batch = resolve_batch(vcfg, target)
    chunk = resolve_chunk(vcfg, batch)
    seed = per_video_seed(vcfg, rel)
    total_secs = 0.0

    for s in segs:
        up_path = os.path.join(up_dir, f"seg_{s.index:05d}.mp4")
        rec = recorded[s.index]
        if rec["status"] == "done" and os.path.exists(up_path):
            continue                       # segment-level resume
        gui_event("SEGMENT", {"video_rel": rel, "seg_index": s.index,
                              "total": len(segs), "state": "running"})

        def _progress(st, _i=s.index):
            gui_event("SEGMENT", {"video_rel": rel, "seg_index": _i,
                                  "total": len(segs), "state": st.get("state"),
                                  "output_bytes": st.get("output_bytes")})

        n = engine.process_segment(
            s.path, up_path, resolution=resolution, batch_size=batch,
            chunk_size=chunk, temporal_overlap=vcfg["temporal_overlap"],
            seed=seed, video_backend=vcfg["video_backend"],
            use_10bit=vcfg["use_10bit"], on_progress=_progress)
        secs = getattr(engine, "last_segment_seconds", None)
        db.upsert_video_segment(conn, root_id, rel, s.index, status="done",
                                out_frames=n, output_path=up_path, seconds=secs)
        total_secs += secs or 0
        budget.add(secs)
        log(f"    segment {s.index + 1}/{len(segs)}: {n} frames"
            + (f" in {secs:.1f}s" if secs else ""))

        cap = budget.exceeded()
        remaining = [r for r in db.get_video_segments(conn, root_id, rel)
                     if r["status"] != "done"]
        if cap and remaining:
            log(f"  PAUSED: {cap}; stopping after this segment "
                f"({len(remaining)} segment(s) left — resume to continue).")
            raise StopInstallment(cap)

    # All segments done -> reassemble locally (concat + audio mux) and drift-check.
    up_paths = [os.path.join(up_dir, f"seg_{s.index:05d}.mp4") for s in segs]
    concat_path = os.path.join(work_root, "concat.mp4")
    vp.concat_segments(up_paths, concat_path)
    vp.mux_audio(concat_path, src_abs, out_video, log=lambda m: log("    " + m.strip()))
    out_info = vp.probe(out_video, count=True)

    seg_out = [r["out_frames"] for r in db.get_video_segments(conn, root_id, rel)]
    reference = sum(s.frame_count for s in segs)   # SeedVR2 preserves per-segment frames (6.3)
    report = vp.check_drift(info, out_info, seg_out, reference_frames=reference)
    db.upsert_video_file(conn, root_id, rel, status="done", output_path=out_video,
                         out_frames=out_info.nb_frames)
    shutil.rmtree(work_root, ignore_errors=True)
    # Tidy the shared work parent (<out>/.imgtbx_video) once it is empty, so a
    # finished tree leaves nothing behind but the output videos.
    try:
        os.rmdir(os.path.dirname(work_root))
    except OSError:
        pass

    if report.ok:
        log(f"[{index}/{total}] DONE {rel} -> {os.path.basename(out_video)} "
            f"({out_info.nb_frames} frames)")
    else:
        log(f"[{index}/{total}] DONE (review) {rel}: " + "; ".join(report.warnings))
    gui_event("VRESULT", {"rel": rel, "outcome": "ok", "output_path": out_video,
                          "warnings": report.warnings})
    return "done", total_secs


# ─────────────────────────────────────────────
#  RUN
# ─────────────────────────────────────────────

def run_batch(src_root, out_root, vcfg, engine, conn, root_id,
              notify_settings=None, cost_per_hr=None):
    """Drive the whole queue against an injected engine. Returns a summary dict."""
    videos = walk_videos(src_root)
    gui_event("QUEUE", [rel for _a, rel in videos])
    log(f"Found {len(videos)} video(s) under {src_root}; target {vcfg['target']}.")
    budget = RunBudget(vcfg["per_run_minute_cap"], vcfg["per_run_cost_cap"], cost_per_hr)

    done = skipped = failed = 0
    stopped = None
    for i, (abs_path, rel) in enumerate(videos, 1):
        existing = db.get_video_file(conn, root_id, rel)
        if existing and existing["status"] == "done" and existing["output_path"] \
                and os.path.exists(existing["output_path"]):
            log(f"[{i}/{len(videos)}] already done: {rel}")
            done += 1
            continue
        try:
            outcome, _secs = process_one(engine, conn, root_id, abs_path, rel,
                                         out_root, vcfg, budget, i, len(videos))
            if outcome == "done":
                done += 1
            elif outcome == "skipped":
                skipped += 1
        except StopInstallment as exc:
            stopped = str(exc)
            break
        except Exception as exc:                       # noqa: BLE001 — log, keep going
            failed += 1
            db.upsert_video_file(conn, root_id, rel, status="failed",
                                 skip_reason=str(exc)[:300])
            log(f"[{i}/{len(videos)}] FAILED {rel}: {exc}")
            gui_event("VRESULT", {"rel": rel, "outcome": "fail", "error": str(exc)[:300]})

    summary = {"done": done, "skipped": skipped, "failed": failed,
               "stopped": stopped, "total": len(videos)}
    _notify_summary(notify_settings, summary, src_root)
    log(f"Summary: {done} done, {skipped} skipped, {failed} failed"
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
        desc = (f"{summary['done']} done, {summary['skipped']} skipped, "
                f"{summary['failed']} failed of {summary['total']}.")
        if summary["stopped"]:
            desc += f"\n{summary['stopped']} — re-run to continue."
        notifications.notify(notify_settings, title, desc, color,
                             fields=[{"name": "Source", "value": src_root}])
    except Exception:
        pass


def main(argv=None):
    p = argparse.ArgumentParser(description="Video Upscaler runner (RunPod-only).")
    p.add_argument("source", help="source folder (searched recursively)")
    p.add_argument("output", nargs="?", help="output folder (default: <source>/_upscaled_video)")
    p.add_argument("--target", choices=list(TARGET_RES), help="override the configured target")
    p.add_argument("--passthrough", action="store_true",
                   help="no pod: stream-copy each segment locally (pipeline test only)")
    args = p.parse_args(argv)

    src_root = os.path.abspath(args.source)
    if not os.path.isdir(src_root):
        print(f"ERROR: source folder not found: {src_root}")
        return 2
    out_root = os.path.abspath(args.output) if args.output \
        else os.path.join(src_root, "_upscaled_video")

    cfg = _load_config()
    overrides = {"target": args.target} if args.target else None
    vcfg = resolve_video_cfg(cfg, overrides)
    notify_settings = notifications.resolve_settings(cfg)

    conn = db.get_conn()
    root_id = db.get_video_root_id(conn, src_root, out_root)

    session = None
    cost_per_hr = None
    if args.passthrough:
        log("Passthrough mode — no pod; segments are stream-copied locally.")
        engine = PassthroughVideoEngine()
    else:
        from remote_run import RemoteSession
        session = RemoteSession(cfg.get("runpod", {}), cfg.get("upscale", {}),
                                APP_ROOT, on_event=log, mode="video")
        engine = session.start()
        cost_per_hr = session.cost_per_hr

    try:
        run_batch(src_root, out_root, vcfg, engine, conn, root_id,
                  notify_settings=notify_settings, cost_per_hr=cost_per_hr)
    finally:
        if session is not None:
            session.close()
        else:
            engine.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
