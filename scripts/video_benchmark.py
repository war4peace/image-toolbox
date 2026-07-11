"""
video_benchmark.py
------------------
Per-card VRAM benchmark suite for LOCAL video upscaling (feature #7,
docs/local-video-upscaler.md sections 16 / 20). Finds each card's REAL ceiling by
upscaling a short clip at an ASCENDING series of batches until it OOMs or thrashes,
then writes the last-good batch into the sizer's learned store (`db.video_batch_learn`)
and the measured rate into the estimate's store (`db.gpu_perf`), so AUTO runs start at
the true ceiling and the time estimate is calibrated -- without the developer guessing.

Why a dedicated tool and not the AUTO path: AUTO must be safe on the first try (no
cascade), so it only ever learns downward from a seed or upward one careful segment at a
time. The benchmark is the sanctioned place to push UPWARD to failure, and it is safe to
do so ONLY because each probe runs in an isolated subprocess (LocalVideoEngine.probe_batch
-> local_video_worker.py): a failed probe's fragmented VRAM dies with its process instead
of poisoning the next (docs 14.2/14.3; `expandable_segments` can't save an in-process sweep
on Windows).

Every probe is persisted the moment it finishes (`db.record_bench_probe`), so a stopped
run RESUMES at the nearest untried batch and never repeats finished work. Driven by the GUI
(gui/video_benchmark.py) over the shared @@TBX@@ event protocol; also runnable headless:

    python scripts/video_benchmark.py --targets 1080p,1440p,4K [--resume]
"""

import os
import sys
import json
import time
import argparse
import threading

try:
    import crash_logger
    crash_logger.install(notify=False)
except Exception:
    pass

import runner_common
runner_common.harden_stdout()

import db
import video_pipeline as vp
import video_estimate as ve
import video_vram_sizer as sizer
import benchmark_clip as bclip
import batch_video_upscale as bv

GUI_MODE = runner_common.GUI_MODE
fmt_hhmmss = runner_common.fmt_hhmmss

APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ── plan constants ───────────────────────────────────────────────────────────

BATCH_FLOOR = 5
# The benchmark deliberately probes PAST the sizer's AUTO cap (33): it is the sanctioned
# "test till it breaks" tool, and a big card's true ceiling can be higher. A learned value
# above 33 is honoured by the sizer (a measured truth beats the AUTO safety cap).
DEFAULT_BATCH_CAP = 65
DEFAULT_FRAMES = 37                 # >= the largest batch so every window runs at least once
DEFAULT_PROBES_PER_CELL = 6         # runtime-estimate heuristic (most cards break by ~bs29)

# Target name -> OUTPUT box (landscape 16:9), mirrors video_estimate.TARGET_BOX so the
# benchmarked output-MP buckets line up with what real runs look up.
TARGETS = {"1080p": (1920, 1080), "1440p": (2560, 1440), "4K": (3840, 2160)}


def batch_series(floor=BATCH_FLOOR, cap=DEFAULT_BATCH_CAP):
    """The ascending 4n+1 batches to probe (5, 9, 13, ... <= cap)."""
    return [b for b in range(floor, cap + 1) if (b - 1) % 4 == 0]


def build_cell(name, out_w, out_h, frames=DEFAULT_FRAMES):
    """One benchmark cell: an OUTPUT size + the (2x) source it is upscaled from. The source
    content is irrelevant to the ceiling (set by output size, docs 14); a clean 2x keeps the
    source small/fast. Even dimensions for yuv420p."""
    sw = max(2, (out_w // 2) & ~1)
    sh = max(2, (out_h // 2) & ~1)
    return {"name": name, "out_w": int(out_w), "out_h": int(out_h),
            "src_w": sw, "src_h": sh, "resolution": int(min(out_w, out_h)),
            "mp": out_w * out_h / 1_000_000.0, "frames": int(frames)}


def build_plan(targets, frames=DEFAULT_FRAMES):
    """Cells for the chosen target names (unknown names skipped, order preserved)."""
    plan = []
    for t in targets:
        box = TARGETS.get(t)
        if box:
            plan.append(build_cell(t, box[0], box[1], frames))
    return plan


# If a recorded failure happened with at least this much LESS free VRAM than is available
# now, treat it as an other-app-contention artifact (not the true ceiling) and re-probe it.
STALE_MARGIN_GB = 1.5


def next_batch(cell_probes, series, free_now=None, stale_margin=STALE_MARGIN_GB):
    """The next batch to probe for a cell, or None if the cell is FINISHED. `cell_probes` =
    [{batch, outcome, free_vram?}]. A recorded oom/thrash normally ends the cell (nothing
    bigger can fit) -- EXCEPT a failure recorded with materially less free VRAM than `free_now`
    (other-app contention, not the ceiling): that one is ignored so the batch is re-probed, and
    it doesn't cap the sweep below itself. Returns the lowest untried batch below any trustworthy
    failure, else None."""
    seen = {}                       # batch -> outcome, EXCLUDING stale contended failures
    terminal = None                 # lowest trustworthy failure batch (nothing >= it can fit)
    for p in cell_probes:
        b = int(p["batch"])
        oc = p["outcome"]
        if oc in ("oom", "thrash"):
            pf = p.get("free_vram")
            if free_now is not None and pf is not None and pf < free_now - stale_margin:
                continue            # contended failure: allow a re-probe
            terminal = b if terminal is None else min(terminal, b)
        seen[b] = oc
    for b in series:
        if terminal is not None and b >= terminal:
            break
        if b not in seen:
            return b
    return None


def cell_ceiling(cell_probes):
    """The largest batch that ran clean for a cell (its ceiling), or None if even the floor
    failed (the card can't do this output size)."""
    oks = [int(p["batch"]) for p in cell_probes if p["outcome"] == "ok"]
    return max(oks) if oks else None


def _probe_seconds_estimate(cell, gpu_id, conn, default_spf=4.0):
    """Rough seconds for ONE probe of a cell: frames x (measured s/MP x cell MP), or a
    conservative default s/frame when the card is unmeasured (the whole reason to benchmark)."""
    spm, _ = ve.local_seconds_per_mp(gpu_id, cell["name"], conn)
    spf = (spm * cell["mp"]) if spm else default_spf
    return cell["frames"] * spf


def estimate_runtime(plan, gpu_id, conn=None, probes_per_cell=DEFAULT_PROBES_PER_CELL,
                     done=0):
    """A ROUGH total-runtime estimate (seconds) for a plan: ~`probes_per_cell` probes per
    cell (most cards break by ~bs29) x the per-probe estimate, minus probes already `done`.
    Deliberately rough (we don't know where a card breaks until we run it); the GUI labels
    it approximate. Never negative."""
    total_probes = 0.0
    weighted = 0.0
    for cell in plan:
        total_probes += probes_per_cell
        weighted += probes_per_cell * _probe_seconds_estimate(cell, gpu_id, conn)
    per_probe = (weighted / total_probes) if total_probes else 0.0
    remaining = max(0.0, total_probes - done)
    return remaining * per_probe


# ── logging + events ─────────────────────────────────────────────────────────

_LOG_FH = None
_STOP = threading.Event()


def _open_log():
    global _LOG_FH
    try:
        log_dir = os.path.join(APP_ROOT, "logs")
        os.makedirs(log_dir, exist_ok=True)
        _LOG_FH = open(os.path.join(log_dir, "video_benchmark.log"), "a",
                       encoding="utf-8", buffering=1)
        import datetime
        _LOG_FH.write(f"\n{'=' * 60}\nBenchmark session: "
                      f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S}\n{'=' * 60}\n")
    except Exception:                                  # noqa: BLE001
        _LOG_FH = None


def log(msg):
    sys.stdout.write(f"{msg}\n")
    sys.stdout.flush()
    if _LOG_FH is not None:
        try:
            _LOG_FH.write(msg + "\n")
        except Exception:                              # noqa: BLE001
            pass


def gui_event(kind, payload):
    """Emit a machine-readable @@TBX@@ event for the GUI (JSON payload), via the shared
    atomic writer so the marker format (`<GUI_MARKER><KIND>|<json>`) matches what the GUI
    parser expects. No-op headless. Kinds: BSTART / BCELL / BPROBE / BCEILING / BDONE."""
    try:
        runner_common.gui_event(kind, json.dumps(payload))
    except Exception:                                  # noqa: BLE001
        pass


def _watch_stdin_for_stop():
    try:
        for line in sys.stdin:
            if line.strip().lower() in ("q", "quit", "stop"):
                _STOP.set()
                break
    except Exception:                                  # noqa: BLE001
        pass


# ── the run ──────────────────────────────────────────────────────────────────

def _engine_settings(cfg, vcfg):
    """The SeedVR2 engine settings: the image-upscale config overlaid with the video-only
    quality/speed knobs (mirrors batch_video_upscale._worker_cfg), so the benchmark measures
    the SAME configuration a real local run uses."""
    wc = dict(cfg.get("upscale", {}))
    wc["compile_dit"] = vcfg["compile"]
    wc["compile_vae"] = vcfg["compile"]
    wc["uniform_batch_size"] = vcfg["uniform_batch_size"]
    wc["input_noise_scale"] = vcfg["input_noise_scale"]
    wc["dit_model"] = vcfg["dit_model"]
    return wc


def _record_cell_result(conn, gpu_id, model_tag, cell):
    """After a cell's sweep, copy its ceiling into the sizer's learned store and its rate
    into the estimate store, so AUTO + the estimate self-improve. Reads the persisted probes
    (the ceiling row carries the timing). Returns the ceiling batch (or None)."""
    probes = db.get_bench_probes(conn, gpu_id, model_tag, cell["out_w"], cell["out_h"])
    ceil = cell_ceiling(probes)
    if not ceil:
        return None
    db.put_learned_batch(conn, f"{gpu_id}|{model_tag}", sizer.mp_bucket(cell["mp"]), ceil)
    row = max((p for p in probes if p["outcome"] == "ok"), key=lambda p: p["batch"], default=None)
    if row and row.get("seconds") and row.get("frames"):
        out_mp = row["frames"] * cell["mp"]
        ve.record_benchmark_rate(conn, gpu_id, cell["name"], out_mp, row["seconds"])
    return ceil


def run_benchmark(targets, frames=DEFAULT_FRAMES, resume=True, batch_cap=DEFAULT_BATCH_CAP):
    """Drive the sweep. Persists every probe, honours a Stop (stdin 'q') between and DURING a
    probe (the current probe's subprocess is killed, its partial result discarded so it
    re-runs on resume), and writes learned batch + rate per finished cell. Returns a summary."""
    cfg = bv._load_config()
    vcfg = bv.resolve_video_cfg(cfg)
    conn = db.get_conn()

    from local_video_engine import LocalVideoEngine, _query_gpu_name
    gpu_id = _query_gpu_name() or "local"
    model_tag = sizer.model_tag(vcfg["dit_model"])
    repo_dir, model_dir = bv._local_seedvr2_paths(cfg)
    work = os.path.join(vcfg["work_root"], "benchmark")
    os.makedirs(work, exist_ok=True)

    series = batch_series(cap=int(batch_cap or DEFAULT_BATCH_CAP))
    plan = build_plan(targets, frames)
    if not plan:
        log("No valid benchmark targets selected; nothing to do.")
        return {"cells": 0, "stopped": None}

    if not resume:
        db.clear_bench(conn, gpu_id, model_tag)

    done = sum(len(db.get_bench_probes(conn, gpu_id, model_tag, c["out_w"], c["out_h"]))
               for c in plan) if resume else 0
    est = estimate_runtime(plan, gpu_id, conn, done=done)
    log(f"Benchmarking {gpu_id} ({model_tag}) — {len(plan)} target(s), frames={frames}, "
        f"batches {series[0]}..{series[-1]}.")
    log(f"Estimated runtime: ~{fmt_hhmmss(est)} (rough).")
    gui_event("BSTART", {"gpu": gpu_id, "model": model_tag,
                         "plan": [{"name": c["name"], "out_w": c["out_w"], "out_h": c["out_h"],
                                   "mp": round(c["mp"], 2)} for c in plan],
                         "series": series, "frames": frames, "estimate_seconds": round(est)})

    engine = LocalVideoEngine(repo_dir, model_dir, _engine_settings(cfg, vcfg),
                              use_subprocess=True, conn=conn, gpu_id=gpu_id)
    probe_out = os.path.join(work, "probe_out.mp4")
    stopped = None
    try:
        for cell in plan:
            if _STOP.is_set():
                stopped = "stopped by user"
                break
            gui_event("BCELL", {"name": cell["name"], "out_w": cell["out_w"],
                                "out_h": cell["out_h"]})
            log(f"\n[{cell['name']}] output {cell['out_w']}x{cell['out_h']} "
                f"({cell['mp']:.2f} MP), source {cell['src_w']}x{cell['src_h']}:")
            try:
                clip = bclip.ensure_source_clip(
                    work, cell["src_w"], cell["src_h"], frames,
                    base_url=vcfg.get("benchmark_clip_url"),
                    base_sha256=vcfg.get("benchmark_clip_sha256"), log=log)
            except Exception as exc:                    # noqa: BLE001 (skip this cell)
                log(f"  could not prepare a source clip ({exc}); skipping this target.")
                continue

            cell_probes = list(db.get_bench_probes(conn, gpu_id, model_tag,
                                                   cell["out_w"], cell["out_h"]))
            while not _STOP.is_set():
                # Free VRAM at probe start (via nvidia-smi, parent stays GPU-free): tags this
                # probe AND lets a failure recorded earlier under contention be re-tried now.
                free_now, _tot = sizer.free_vram_gb(prefer_smi=True)
                b = next_batch(cell_probes, series, free_now=free_now)
                if b is None:
                    break
                gui_event("BPROBE", {"name": cell["name"], "batch": b, "state": "running"})
                log(f"  probing batch {b} …")
                res = engine.probe_batch(clip, probe_out, resolution=cell["resolution"],
                                         batch=b, frames=frames, should_stop=_STOP.is_set)
                if res["outcome"] == "stopped":
                    stopped = "stopped by user"
                    break
                db.record_bench_probe(
                    conn, gpu_id, model_tag, cell["out_w"], cell["out_h"], b,
                    res["outcome"], frames=res.get("frames"), seconds=res.get("seconds"),
                    peak_alloc=res.get("peak_alloc_gb"), peak_reserved=res.get("peak_reserved_gb"),
                    free_vram=free_now)
                cell_probes = [p for p in cell_probes if int(p["batch"]) != b]
                cell_probes.append({"batch": b, "outcome": res["outcome"], "free_vram": free_now})
                spf = (res["seconds"] / res["frames"]) if res.get("frames") else None
                log(f"    batch {b}: {res['outcome']}"
                    + (f", {res['seconds']:.0f}s ({spf:.2f} s/frame), "
                       f"peak {res.get('peak_alloc_gb')}/{res.get('peak_reserved_gb')} GB"
                       if res["outcome"] == "ok" else ""))
                gui_event("BPROBE", {"name": cell["name"], "batch": b, "state": "done",
                                     "outcome": res["outcome"],
                                     "seconds": round(res.get("seconds") or 0, 1),
                                     "spf": round(spf, 2) if spf else None,
                                     "peak_alloc": res.get("peak_alloc_gb"),
                                     "peak_reserved": res.get("peak_reserved_gb")})
                if res["outcome"] != "ok":
                    break
            if _STOP.is_set() and stopped is None:
                stopped = "stopped by user"
            ceil = _record_cell_result(conn, gpu_id, model_tag, cell)
            log(f"  {cell['name']} ceiling: "
                + (f"batch {ceil} (saved)" if ceil else "no batch fit (card can't do this target)"))
            gui_event("BCEILING", {"name": cell["name"], "ceiling": ceil,
                                   "overlap": sizer.auto_overlap(ceil) if ceil else None})
            if stopped:
                break
    finally:
        try:
            engine.close()
        except Exception:                              # noqa: BLE001
            pass
        try:
            if os.path.exists(probe_out):
                os.remove(probe_out)
        except OSError:
            pass

    summary = {"cells": len(plan), "stopped": stopped}
    gui_event("BDONE", summary)
    log(f"\nBenchmark {'stopped' if stopped else 'complete'}: {len(plan)} target(s). "
        "Results saved; AUTO runs and the time estimate now use them.")
    return summary


def main(argv=None):
    p = argparse.ArgumentParser(description="Per-card local video benchmark (feature #7).")
    p.add_argument("--targets", default="1080p",
                   help="comma-separated target names to benchmark (1080p,1440p,4K).")
    p.add_argument("--frames", type=int, default=DEFAULT_FRAMES)
    p.add_argument("--batch-cap", type=int, default=DEFAULT_BATCH_CAP)
    p.add_argument("--restart", action="store_true",
                   help="discard prior probes for this card+model and start fresh "
                        "(default resumes).")
    args = p.parse_args(argv)

    _open_log()
    if GUI_MODE:
        threading.Thread(target=_watch_stdin_for_stop, daemon=True).start()
    targets = [t.strip() for t in args.targets.split(",") if t.strip()]
    try:
        run_benchmark(targets, frames=args.frames, resume=not args.restart,
                      batch_cap=args.batch_cap)
    except Exception as exc:                            # noqa: BLE001
        import traceback
        log(f"Benchmark failed: {exc}")
        if _LOG_FH is not None:
            _LOG_FH.write(traceback.format_exc() + "\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
