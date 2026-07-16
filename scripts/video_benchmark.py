"""
video_benchmark.py
------------------
Per-card VRAM benchmark suite for video upscaling (feature #7,
docs/local-video-upscaler.md sections 16 / 20 / 22). Finds a card's REAL ceiling by
upscaling a short clip at an ASCENDING series of batches until it OOMs or thrashes,
then writes the last-good batch into the sizer's learned store (`db.video_batch_learn`)
and the measured rate into the estimate's store (`db.gpu_perf`), so AUTO runs start at
the true ceiling and the time estimate is calibrated -- without the developer guessing.

Runs on the LOCAL card (fresh-subprocess isolation per probe) OR (--remote) on a rented
RunPod GPU (in-process on the resident pod worker via /video/probe, section 22): the sweep
orchestration is IDENTICAL, only the injected engine changes. A remote sweep keys its
learned batch under the PLAIN RunPod id the run reads (section 22.3) and deploys + tears
down its own pod.

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
import notifications
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
# The benchmark is the sanctioned "test till it breaks" tool: it sweeps UPWARD until the card
# OOMs/thrashes, and that OOM is the real terminal -- NOT an arbitrary batch number. So the cap
# is only a very high SAFETY ceiling to bound the probe list / stop a (pathological) card that
# never OOMs; the sweep almost always stops far below it. A big remote card (future public
# benchmarking) can legitimately reach hundreds, so this is set high enough not to clip it.
# Configurable via --batch-cap. A learned value above the sizer's AUTO cap is honoured (a
# measured truth beats a safety cap).
DEFAULT_BATCH_CAP = 3000            # "go stupid" on high-VRAM cards: a PRO 6000 at 0.4 MP never
                                    # reached the old 513 wall (only 42/96 GB used), so raise the
                                    # safety ceiling far enough to find the real one. This is only
                                    # tractable because the sweep climbs GEOMETRICALLY (next_batch),
                                    # not every 4n+1 rung -- a linear sweep to 3000 would be ~750
                                    # probes; the doubling climb + binary refine is ~a dozen.
# The clip's frame count is sized PER PROBE to at least the batch (a window can't exceed the
# clip's frames, or SeedVR2 silently collapses it to a SMALLER batch). This is the MINIMUM /
# small-batch floor: probes at or below it reuse one short clip; a larger batch gets a clip
# grown to its own size so the probed batch is the batch that actually runs.
DEFAULT_FRAMES = 37
DEFAULT_PROBES_PER_CELL = 6         # runtime-estimate heuristic (most cards break by ~bs29)

# Target name -> (OUTPUT box, standard source key). Each cell is upscaled from a NATIVE, aspect-
# matched Creative-Commons source (benchmark_clip.SOURCES) with no rescale -- a real input->output
# ratio, not a synthetic "half the output" placeholder -- so users benchmark identical footage.
# Two groups:
#   * a 4:3 / 3:4 ladder (the common old-camera aspects), one cell per 0.5-MP sizer bucket a small
#     card can actually reach, each a size real runs produce -- so a 24 GB card has meaningful
#     targets BELOW the 2.1-MP 1080p edge (which was the only feasible preset before);
#   * the 16:9 presets (mirror video_estimate.TARGET_BOX) for widescreen sources / bigger cards.
# The VAE-decode ceiling is set by OUTPUT megapixels, not aspect (docs 14), and the sizer keys
# learned batch on the MP bucket -- so a PORTRAIT 3:4 target is the identical pixel count (hence
# identical ceiling) as its 4:3 transpose, and benchmarking only the 4:3 side covers both. The
# small 4:3/3:4 sources are only ~240px, so their cells are large upscales (3-5x): that is real
# SD-revival work, and the ceiling is output-bound regardless of factor.
TARGETS = {
    # 4:3 landscape ladder + the two DISTINCT 3:4 portrait points. A portrait output is NOT the
    # transpose of a landscape one: box-fit height-caps a 3:4 source to a SMALLER area than a 4:3
    # source (e.g. into the 1080p box a 4:3 source -> 1440x1080 = 1.56 MP, a 3:4 source ->
    # 810x1080 = 0.87 MP), so its VAE-decode ceiling differs and it needs its own cell. (A TRUE
    # transpose like 960x1280 == 1280x960 in pixels, so THOSE aren't duplicated.)
    "540x720":   (540, 720, "p3x4"),      # 0.39 MP  3:4     from 240x320 (2.25x)
    "960x720":   (960, 720, "p4x3"),      # 0.69 MP  4:3     from 320x240 (3x)
    "810x1080":  (810, 1080, "p3x4"),     # 0.87 MP  3:4     from 240x320 (3.375x)
    "1280x960":  (1280, 960, "p4x3"),     # 1.23 MP  4:3     from 320x240 (4x)
    "1440x1080": (1440, 1080, "p4x3"),    # 1.56 MP  4:3     from 320x240 (4.5x)
    "1600x1200": (1600, 1200, "p4x3"),    # 1.92 MP  4:3     from 320x240 (5x)
    "1080p": (1920, 1080, "w360"),        # 2.07 MP  16:9    from 640x360 (3x)
    "1440p": (2560, 1440, "w720"),        # 3.69 MP  16:9    from 1280x720 (2x)
    "4K":    (3840, 2160, "w1080"),       # 8.29 MP  16:9    from 1920x1080 (2x)
}


def batch_series(floor=BATCH_FLOOR, cap=DEFAULT_BATCH_CAP):
    """The ascending 4n+1 batches to probe (5, 9, 13, ... <= cap)."""
    return [b for b in range(floor, cap + 1) if (b - 1) % 4 == 0]


def build_cell(name, out_w, out_h, source, frames=DEFAULT_FRAMES):
    """One benchmark cell: an OUTPUT size + the NATIVE, aspect-matched standard source it is
    upscaled from (a benchmark_clip.SOURCES key). The source content is irrelevant to the ceiling
    (set by output size, docs 14); `src_w`/`src_h` are the source's native dims (what the engine
    reads), and `resolution` is the output short side the engine scales that source up to."""
    s = bclip.SOURCES[source]
    return {"name": name, "out_w": int(out_w), "out_h": int(out_h),
            "source": source, "src_w": int(s["w"]), "src_h": int(s["h"]),
            "resolution": int(min(out_w, out_h)),
            "mp": out_w * out_h / 1_000_000.0, "frames": int(frames)}


def build_plan(targets, frames=DEFAULT_FRAMES):
    """Cells for the chosen target names (unknown names skipped, order preserved)."""
    plan = []
    for t in targets:
        box = TARGETS.get(t)
        if box:
            plan.append(build_cell(t, box[0], box[1], box[2], frames))
    return plan


# If a recorded failure happened with at least this much LESS free VRAM than is available
# now, treat it as an other-app-contention artifact (not the true ceiling) and re-probe it.
STALE_MARGIN_GB = 1.5


def next_batch(cell_probes, floor, cap=DEFAULT_BATCH_CAP, free_now=None,
               stale_margin=STALE_MARGIN_GB):
    """The next batch to probe for a cell's ceiling search, or None when the ceiling is pinned
    (cell FINISHED). Two phases, exploiting that VRAM fit is MONOTONE in batch (once a batch
    OOMs, nothing bigger fits):

      * Phase 1 (no failure yet): geometric climb -- start at the VRAM-aware `floor`, then
        DOUBLE the largest ok batch each step, up to `cap`. A high-VRAM card reaches the wall
        in a few probes instead of crawling every 4n+1 rung.
      * Phase 2 (a genuine failure exists): binary-refine between the largest ok and the first
        failure to pin the exact max-fit rung; done when they are adjacent 4n+1 rungs.

    `cell_probes` = [{batch, outcome, free_vram?}]. A failure recorded with materially less free
    VRAM than `free_now` (local other-app contention, not the true ceiling) is ignored so it is
    re-probed and doesn't cap the sweep; remote passes free_now=None (a dedicated pod has no
    contention), disabling that path. `floor` only sets the FIRST probe, so a resumed cell (or
    the GUI's done-check via cell_done) gets the same terminal answer regardless of it."""
    lo_floor = sizer.to_4n1(max(BATCH_FLOOR, int(floor or BATCH_FLOOR)))
    hi_cap = sizer.to_4n1(max(lo_floor, int(cap or DEFAULT_BATCH_CAP)))

    oks, bads = [], []
    for p in cell_probes:
        b = sizer.to_4n1(int(p["batch"]))
        oc = p.get("outcome")
        if oc == "ok":
            oks.append(b)
        elif oc in ("oom", "thrash"):
            pf = p.get("free_vram")
            if free_now is not None and pf is not None and pf < free_now - stale_margin:
                continue            # contended failure: allow a re-probe, don't cap the sweep
            bads.append(b)
    oks = sorted(set(oks))
    bads = sorted(set(bads))
    tried = set(oks) | set(bads)
    min_bad = bads[0] if bads else None
    max_ok = oks[-1] if oks else None

    # Phase 2: bracket the ceiling below the first genuine failure.
    if min_bad is not None:
        below = [b for b in oks if b < min_bad]
        if not below:
            lo = sizer.to_4n1(BATCH_FLOOR)
            if lo >= min_bad:
                return None         # even the floor fails: the card can't do this cell
            if lo not in tried:
                return lo           # confirm a low anchor before bisecting
        else:
            lo = max(below)
        if lo + 4 >= min_bad:
            return None             # adjacent rungs: ceiling = lo, done
        mid = sizer.to_4n1((lo + min_bad) // 2)
        if mid <= lo:
            mid = lo + 4
        if mid >= min_bad or mid in tried:
            return None
        return mid

    # Phase 1: no failure yet -> geometric climb from the floor toward the cap.
    if max_ok is None:
        return lo_floor
    if max_ok >= hi_cap:
        return None                 # reached the cap without failing (capped ceiling)
    nxt = sizer.to_4n1(min(hi_cap, max_ok * 2))
    if nxt <= max_ok:
        nxt = min(hi_cap, max_ok + 4)
    return nxt if nxt > max_ok else None


def cell_done(cell_probes, cap=DEFAULT_BATCH_CAP):
    """True when a cell's ceiling is pinned and no more probes are needed. Floor-independent
    (the floor only affects the FIRST probe), so the GUI can render 'saved' vs 'partial' from
    the persisted probes alone, without knowing the card's VRAM."""
    return next_batch(cell_probes, BATCH_FLOOR, cap, free_now=None) is None


# Warming lives in the ENGINES now (LocalVideoEngine.PROBE_WARMUP_PASSES / the pod's
# /video/probe `warmup`), not here. A cell-level warmup could not do the job:
#
#   * it warmed ONE batch, and under static compile every rung is a different shape, so
#     every other rung still paid ~30s of torch.compile inside its own measurement;
#   * it was remote-only, on the reasoning that a local probe is "cold by design" (fresh
#     subprocess per probe). True, and precisely the bug: cold by design is not cold in
#     PRODUCTION, which loads the model and compiles ONCE per run and amortises both over
#     every segment. The sweep was charging per rung what a real run pays per video.
#   * a warmup in a SEPARATE process cannot help a local probe at all: the model load and
#     compile's dynamo/guard work are per-process, and the compile charge survived an
#     already-warm on-disk inductor cache.
#
# So each probe now warms ITSELF, in the process that measures it, on the clip and batch it
# is about to time. That also makes the old warmup's two hard-won constraints structural
# rather than remembered: it necessarily runs the probes' code path at the probe's own
# shape, so it can neither warm a graph no probe uses nor pick a rung that OOMs.

# How often a running probe reports in. A 4K probe can run 20+ minutes, and the pipeline
# goes fully silent for long stretches inside it (the DiT compile, then each atomic tiled
# decode), so with no heartbeat line the log is indistinguishable from a hang. It was, in
# fact, mistaken for one.
PROBE_PING_SEC = 60


def _probe_pinger(log, label):
    """Build an on_progress callback that logs at most one line per PROBE_PING_SEC.

    Reports the worker's `stalled_for`: seconds since the SeedVR2 pipeline last emitted
    ANY output. That number used to be the pod's liveness signal (>15 min of it had the
    dead-man's switch terminate a working pod); it is now purely diagnostic, and it is the
    only way to see whether a silent 4K probe is sitting in the compile or in a decode.
    Fail-safe: the caller already swallows exceptions, but a status field can be missing
    on an older worker, so nothing here is required."""
    state = {"last": 0.0}

    def _ping(st):
        now = time.time()
        if now - state["last"] < PROBE_PING_SEC:
            return
        state["last"] = now
        el = st.get("elapsed")
        stalled = st.get("stalled_for")
        bits = [f"{label}: running {_fmt_secs(el)}"]
        if stalled is not None:
            bits.append(f"no pipeline output for {_fmt_secs(stalled)}")
        fp, tf = st.get("frames_processed"), st.get("total_frames")
        if fp and tf:
            bits.append(f"~{fp}/{tf} frames")
        log("    " + ", ".join(bits))

    return _ping


def _fmt_secs(s):
    """m:ss for a probe-scale duration; '?' when the worker did not report one."""
    try:
        s = int(float(s))
    except (TypeError, ValueError):
        return "?"
    return f"{s // 60}m{s % 60:02d}s"



def drop_collapsed(rows):
    """Discard any 'ok' probe recorded with FEWER frames than its batch. Such a probe's
    temporal window was collapsed to the clip's frame count, so it secretly measured a SMALLER
    batch and was mislabelled (the pre-fix 37-frame clip made every 'batch 41+' really run 37).
    These phantom rows must not be trusted as tried (in the sweep) NOR as a ceiling (when
    recording), so they are dropped everywhere the persisted probes are read -- old data
    self-heals as the affected batches get re-probed with correctly-sized clips."""
    return [p for p in rows
            if not (p.get("outcome") == "ok" and p.get("frames")
                    and int(p["frames"]) < int(p["batch"]))]


def cell_ceiling(cell_probes):
    """The largest batch that ran clean for a cell (its ceiling / max fit), or None if even
    the floor failed (the card can't do this output size). This is the "how far can it push"
    number; the batch AUTO actually uses is throughput_optimal_batch (below)."""
    oks = [int(p["batch"]) for p in cell_probes if p["outcome"] == "ok"]
    return max(oks) if oks else None


def throughput_optimal_batch(cell_probes, tol=0.01):
    """The batch AUTO should USE: the one with the best measured throughput (min seconds per
    frame) among the clean 'ok' probes -- NOT the raw ceiling. As batch grows, s/frame falls
    (fixed costs amortise) until the working set overflows VRAM and spills to system RAM, which
    makes a BIGGER batch SLOWER. So the fastest batch is the LARGEST window that runs WITHOUT
    spill: best speed AND as much temporal continuity as the card sustains cleanly. The raw
    ceiling sits one step into that spill zone (it fits, but crawls). Within `tol` of the best
    s/frame the LARGER batch wins (equal speed -> favour the bigger window). None if no ok probe
    carries timing (the caller then falls back to the ceiling)."""
    timed = [p for p in cell_probes if p.get("outcome") == "ok"
             and p.get("seconds") and p.get("frames")]
    if not timed:
        return None
    def spf(p):
        return p["seconds"] / p["frames"]
    best = min(spf(p) for p in timed)
    return max(int(p["batch"]) for p in timed if spf(p) <= best * (1.0 + tol))


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

def _worker_settings(cfg, vcfg):
    """The SeedVR2 engine settings the benchmark measures with: the image-upscale config
    overlaid with the video-only quality/speed knobs (mirrors batch_video_upscale._worker_cfg),
    so the benchmark measures the SAME configuration a real run uses. Shared by the LOCAL and
    REMOTE paths; the local-compiler gate is applied by _engine_settings, NOT here (it is a
    check of the LOCAL machine's compiler and must not run for a pod, which has its own)."""
    wc = dict(cfg.get("upscale", {}))
    wc["compile_dit"] = vcfg["compile"]
    wc["compile_vae"] = vcfg["compile"]
    wc["uniform_batch_size"] = vcfg["uniform_batch_size"]
    wc["input_noise_scale"] = vcfg["input_noise_scale"]
    wc["dit_model"] = vcfg["dit_model"]
    # VAE tiling from the resolved video config, exactly as _worker_cfg does, so the
    # benchmark measures the ceiling the real run will actually get. Inheriting it
    # implicitly from cfg["upscale"] would let the image tab move these numbers.
    for _k in ("encode_tiled", "decode_tiled", "encode_tile_size", "decode_tile_size",
               "encode_tile_overlap", "decode_tile_overlap"):
        wc[_k] = vcfg[_k]
    # compile_dynamic comes from the resolved video config (video.*, else the image upscale.*
    # value, else off), exactly as _worker_cfg takes it, so the benchmark cannot measure a
    # different graph mode than the run. It defaults OFF: it was tried (one graph serving every
    # batch, so a sweep's per-rung recompiles vanish and one warmup warms them all) and
    # REVERTED after it cost a rented PRO 6000 a >32-minute, still-unfinished, single-threaded
    # compile on a cold pod -- against a ~9-minute static baseline. Two reasons it was a bad
    # trade:
    #   * its benefit is making rates COMPARABLE across rungs that SUCCEED, and a 4K cell has
    #     exactly ONE (batch 5). It solved a problem that cell does not have.
    #   * the cost is unbounded and unmeasured; static's is known.
    # The engine still supports it (upscale_engine._build_args passes --compile_dynamic when
    # `compile_dynamic` is set), so it stays reachable from config for measuring, but nothing
    # turns it on by default. Measure it LOCALLY before ever paying for it on a pod again.
    wc["compile_dynamic"] = vcfg["compile_dynamic"]
    # Use the VIDEO resident threshold (not the image 40) so the benchmark measures ceilings in
    # the SAME resident/phased mode a real video run will use (a benchmark measured resident
    # would under-report a card that runs phased in production).
    wc["vram_resident_threshold_gb"] = vcfg["vram_resident_threshold_gb"]
    return wc


def _engine_settings(cfg, vcfg, log_fn=None):
    """LOCAL engine settings: _worker_settings plus the compile-capability gate the local
    RUNNER applies. Without the gate a machine with no C compiler HANGS on the first probe's
    VAE compile under piped stdio (the "stuck at first segment" deadlock).

    `log_fn` overrides the runner's log for callers with nowhere to write it (the GUI resolves
    the same settings just to name the key, and must not narrate the gate into a log file it
    does not own)."""
    wc = _worker_settings(cfg, vcfg)
    bv.gate_local_compile(wc, log_fn or log)
    return wc


def effective_settings(cfg, vcfg, remote=False, log_fn=None):
    """The settings the engine will ACTUALLY run under, which is NOT the config: the LOCAL path
    routes through the compile gate, so a machine with no usable C compiler runs uncompiled no
    matter what config says. Remote is ungated here because the POD owns that decision (it runs
    its own _gate_compile and logs it); today's RunPod image has a compiler."""
    if remote:
        return _worker_settings(cfg, vcfg)
    return _engine_settings(cfg, vcfg, log_fn=log_fn)


def bench_key(vcfg, eff_settings):
    """The video_bench PROBE key. A probe's outcome ('ok' at batch N / 'oom') and its SECONDS
    are only true for the regime it ran under, so every setting that moves them joins the model
    tag. Each tag is "" when its feature is off, so existing probes keep their identity.
    `sizer.model_tag` alone stays the DISPLAY name.
      * tiling  -> moves the VRAM ceiling (an untiled OOM and a tiled OK at the same
                   (card, size, batch) are two different facts).
      * compile -> moves the RATE. Without it a compiled sweep resumes uncompiled rungs and
                   publishes their seconds as compiled ones.
    Derive it ONLY from effective_settings, never the raw config: the gate can turn compile off,
    and tagging such a run "|c" would file uncompiled seconds under the compiled key, exactly
    the lie the tag exists to prevent."""
    return (sizer.model_tag(vcfg["dit_model"])
            + sizer.tile_tag(eff_settings)
            + sizer.compile_tag(eff_settings))


def resolve_bench_key(remote=False, log_fn=None):
    """The key the NEXT sweep on this machine will WRITE under, config loaded fresh. For callers
    OUTSIDE the runner (the GUI's results table + resume estimate) that must read the rows the
    runner is going to produce. The GUI used to derive a bare model tag instead, so a compiled
    sweep wrote "7b|c" while the window read "7b": it showed the stale uncompiled baseline for
    the entire run and counted those rows as work already done."""
    cfg = bv._load_config()
    vcfg = bv.resolve_video_cfg(cfg)
    return bench_key(vcfg, effective_settings(cfg, vcfg, remote=remote, log_fn=log_fn))


def _deploy_remote_engine(cfg, vcfg):
    """Deploy a video pod for the selected GPU (IMGTBX_GPU_OVERRIDE) and return
    (engine, session), reusing the RUN's RemoteSession (deploy -> push -> start worker ->
    ssh tunnel -> funds guard -> dead-man's switch). The caller tears the session down
    (which stops the pod) in its finally. Emits POD / RCOST for the GUI's live cost readout."""
    from remote_run import RemoteSession
    sess = RemoteSession(cfg.get("runpod", {}), _worker_settings(cfg, vcfg),
                         APP_ROOT, on_event=log, mode="video")
    try:
        engine = sess.start()
    except Exception:
        try:
            sess.close()
        except Exception:                              # noqa: BLE001 (fail-safe teardown)
            pass
        raise
    gui_event("POD", sess.pod_id or "")
    if sess.cost_per_hr is not None:
        gui_event("RCOST", sess.cost_per_hr)
    return engine, sess


def _record_cell_result(conn, gpu_id, bench_model, cell, learn_key):
    """After a cell's sweep, save the THROUGHPUT-OPTIMAL batch (not the raw ceiling) into the
    sizer's learned store and record ITS rate into the estimate store, so AUTO runs use the
    fastest window and the estimate reflects it. `learn_key` is the video_batch_learn key the
    consuming run READS: `gpu|model` for a LOCAL run (the sizer's key), or the PLAIN RunPod id
    for a REMOTE run (process_job reads the un-model-qualified IMGTBX_GPU_OVERRIDE, section 22.3).
    The raw ceiling (max fit) is still returned for display. Returns (ceiling, saved) -- both
    None if the card can't do this output."""
    probes = drop_collapsed(db.get_bench_probes(conn, gpu_id, bench_model,
                                                cell["out_w"], cell["out_h"]))
    ceil = cell_ceiling(probes)
    if not ceil:
        return None, None
    saved = throughput_optimal_batch(probes) or ceil
    db.put_learned_batch(conn, learn_key, sizer.mp_bucket(cell["mp"]), saved)
    # Rate from the SAVED (optimal) probe, so the estimate matches the batch AUTO will run;
    # fall back to the largest ok probe if the optimal one somehow lacks timing.
    row = next((p for p in probes if p["outcome"] == "ok" and int(p["batch"]) == saved), None)
    if not (row and row.get("seconds") and row.get("frames")):
        row = max((p for p in probes if p["outcome"] == "ok"),
                  key=lambda p: p["batch"], default=None)
    if row and row.get("seconds") and row.get("frames"):
        out_mp = row["frames"] * cell["mp"]
        ve.record_benchmark_rate(conn, gpu_id, cell["name"], out_mp, row["seconds"])
    return ceil, saved


def _log_summary_table(conn, gpu_id, bench_model, plan):
    """Print a compact summary table to the terminal log when a sweep finishes, mirroring the
    GUI Results table (target -> max fit / saved batch / overlap / best s-per-frame / peak VRAM /
    runtime) so a user reading the log (headless, or scrolled back through a long sweep) gets the
    same at-a-glance verdict without hunting the per-probe lines. Reads the persisted probes, the
    same source of truth the GUI renders from. Fail-safe: any error just skips the table."""
    try:
        hdr = (f"{'Target':<10} {'Output':<11} {'MP':>5}  {'Fit':>4} {'Batch':>5} {'Ovl':>3}  "
               f"{'s/frame':>8}  {'Peak GB':>11}  {'Runtime':>8}  Status")
        lines = ["", "Summary:", hdr, "-" * len(hdr)]
        for cell in plan:
            probes = drop_collapsed(db.get_bench_probes(conn, gpu_id, bench_model,
                                                        cell["out_w"], cell["out_h"]))
            out = f"{cell['out_w']}x{cell['out_h']}"
            if not probes:
                lines.append(f"{cell['name']:<10} {out:<11} {cell['mp']:>5.2f}  "
                             f"{'-':>4} {'-':>5} {'-':>3}  {'-':>8}  {'-':>11}  {'-':>8}  "
                             f"not benchmarked")
                continue
            ceil = cell_ceiling(probes)
            saved = throughput_optimal_batch(probes) or ceil
            overlap = sizer.auto_overlap(saved) if saved else None
            timed = [p for p in probes if p["outcome"] == "ok"
                     and p.get("seconds") and p.get("frames")]
            best = min((p["seconds"] / p["frames"] for p in timed), default=None)
            peak = next((p for p in probes if p["outcome"] == "ok"
                         and int(p["batch"]) == (saved or -1)), None)
            runtime = sum((p["seconds"] or 0) for p in probes)
            if not ceil:
                status = "no fit (card can't do this target)"
            elif cell_done(probes):
                status = "saved"
            else:
                status = "partial (resumable)"
            peak_s = (f"{peak['peak_alloc']}/{peak['peak_reserved']}"
                      if peak and peak.get("peak_alloc") and peak.get("peak_reserved") else "-")
            lines.append(
                f"{cell['name']:<10} {out:<11} {cell['mp']:>5.2f}  "
                f"{(str(ceil) if ceil else '-'):>4} {(str(saved) if saved else '-'):>5} "
                f"{(str(overlap) if overlap is not None else '-'):>3}  "
                f"{(f'{best:.2f}' if best else '-'):>8}  {peak_s:>11}  "
                f"{fmt_hhmmss(runtime):>8}  {status}")
        for ln in lines:
            log(ln)
    except Exception:                                  # noqa: BLE001 (a summary must never fail a run)
        pass


def _notify_benchmark(notify_settings, gpu_id, model_tag, remote, results, stopped, elapsed):
    """Send one completion notification (Discord / Telegram / ntfy) when a sweep ends, so a
    long unattended benchmark (especially a remote pod running for hours) pings the user the
    same way a real run does. `results` is [(name, ceiling, saved)] per finished cell. Fail-safe
    (any error swallowed) and a no-op when no backend is configured."""
    if not notify_settings:
        return
    try:
        fitted = sum(1 for _n, _c, s in results if s)
        total = len(results)
        if stopped:
            color, title = 0xE67E22, "Video benchmark stopped"      # orange: incomplete
        else:
            color, title = 0x2ECC71, "Video benchmark complete"     # green: all done
        where = "remote pod" if remote else "local card"
        desc = (f"{gpu_id} ({model_tag}) on the {where}: {fitted}/{total} target(s) "
                f"measured in {fmt_hhmmss(elapsed)}.")
        if stopped:
            desc += f"\n{stopped}; finished probes are saved, re-run to continue."
        fields = []
        for name, ceil, saved in results:
            if saved:
                extra = f" (max fit {ceil})" if ceil and ceil != saved else ""
                fields.append({"name": name, "value": f"batch {saved}{extra}"})
            else:
                fields.append({"name": name, "value": "no batch fit"})
        notifications.notify(notify_settings, title, desc, color, fields=fields)
    except Exception:                                  # noqa: BLE001
        pass


def run_benchmark(targets, frames=DEFAULT_FRAMES, resume=True, batch_cap=DEFAULT_BATCH_CAP,
                  remote=False):
    """Drive the sweep on the LOCAL card or (remote=True) a rented RunPod GPU. Persists every
    probe, honours a Stop (stdin 'q') between and DURING a probe, and writes the learned batch +
    rate per finished cell. On the REMOTE path the sweep runs in-process on the pod (section 22),
    keys the learned batch under the PLAIN RunPod id the run reads, and tears the pod down at the
    end. Returns a summary."""
    cfg = bv._load_config()
    vcfg = bv.resolve_video_cfg(cfg)
    notify_settings = notifications.resolve_settings(cfg)
    t_start = time.monotonic()
    conn = db.get_conn()
    model_tag = sizer.model_tag(vcfg["dit_model"])          # the DISPLAY name
    # The regime-tagged probe key (see bench_key) + the settings it is derived from. The GUI
    # resolves the identical key via resolve_bench_key, so its table reads the rows this run
    # writes; keep the two on this one derivation.
    eff_settings = effective_settings(cfg, vcfg, remote=remote)
    bench_model = bench_key(vcfg, eff_settings)
    work = os.path.join(vcfg["work_root"], "benchmark")
    os.makedirs(work, exist_ok=True)

    cap = int(batch_cap or DEFAULT_BATCH_CAP)
    plan = build_plan(targets, frames)
    if not plan:
        log("No valid benchmark targets selected; nothing to do.")
        return {"cells": 0, "stopped": None}

    # Total VRAM drives the per-cell starting FLOOR (skip the low rungs a big card obviously
    # clears). Remote: the pod's card, passed by the GUI from the picker; local: read live.
    if remote:
        try:
            total_gb = float(os.environ.get("IMGTBX_GPU_VRAM_GB", "") or 0) or None
        except ValueError:
            total_gb = None
    else:
        total_gb = sizer.free_vram_gb(prefer_smi=True)[1]

    # Identify the card + the learned-batch key the CONSUMING run reads (section 22.3):
    #   local  -> the sizer's `gpu|model` key;
    #   remote -> the PLAIN RunPod id (process_job reads the un-model-qualified override).
    if remote:
        gpu_id = os.environ.get("IMGTBX_GPU_OVERRIDE", "").strip().split(",")[0].strip()
        if not gpu_id:
            log("Remote benchmark needs a selected GPU (none passed). Pick a GPU on the "
                "Video tab (Remote), then press Benchmark GPU.")
            return {"cells": 0, "stopped": "no GPU selected"}
        learn_key = gpu_id
    else:
        from local_video_engine import _query_gpu_name
        gpu_id = _query_gpu_name() or "local"
        learn_key = f"{gpu_id}|{model_tag}"
    # A ceiling measured under one VRAM regime is not the same measurement as one under
    # another, so it is stored under its own key and can never seed a run seeing the other
    # setting. Everything off tags to "" = the historical key. Both VAE tiling and
    # torch.compile move the ceiling, so learn_tag carries both (compile used to be omitted
    # here on the theory that it "barely" moved it; this card's own rows disproved that and
    # the story is in sizer.learn_tag). The sweep writes the OPTIMAL batch to this key, so a
    # missing tag does not just mislabel a row: it OVERWRITES the other regime's row with a
    # number measured under settings that run never saw.
    learn_key += sizer.learn_tag(eff_settings)

    if not resume:
        # Scope the wipe to the targets in THIS plan. A restart means "re-measure what I
        # picked", never "discard every cell on this card": the other targets are hours of
        # GPU time (billed, when remote) and nothing would announce their loss.
        db.clear_bench(conn, gpu_id, bench_model,
                       cells=[(c["out_w"], c["out_h"]) for c in plan])

    done = sum(len(db.get_bench_probes(conn, gpu_id, bench_model, c["out_w"], c["out_h"]))
               for c in plan) if resume else 0
    est = estimate_runtime(plan, gpu_id, conn, done=done)
    where = "on a RunPod pod" if remote else "locally"
    vram_note = (f"{total_gb:.0f} GB VRAM -> floor scales per target" if total_gb
                 else "VRAM unknown -> climbing from the base floor")
    log(f"Benchmarking {gpu_id} ({model_tag}) {where}: {len(plan)} target(s), frames={frames}, "
        f"geometric climb to a measured ceiling (cap {cap}, {vram_note}).")
    # The one setting these numbers are meaningless without: tiling moves the VRAM ceiling,
    # so a batch/rate result only compares against another run with the SAME tiling state.
    log(f"VAE tiling: {bv.describe_tiling(vcfg)}")
    # Compile state, stated up front. A compiling pod is SILENT for minutes with an idle GPU,
    # which reads exactly like a hang; not knowing whether compile was even on cost a rented
    # PRO 6000 a 35-minute diagnosis. Remote also reports its own gate decision (_gate_compile).
    log(f"torch.compile: {'on' if eff_settings.get('compile_dit') else 'off'}"
        f"{' (dynamic)' if eff_settings.get('compile_dynamic') else ''}"
        + (" -- expect a silent, GPU-idle compile before the first pass (cold pod = empty "
           "Triton cache)" if (remote and eff_settings.get("compile_dit")) else ""))
    log(f"Estimated runtime: ~{fmt_hhmmss(est)} (rough).")
    if remote:
        log("Note: the pod loads the model once from the network volume (a minute or two) before "
            "the first probe; the sweep then runs in-process on the pod. So nothing lost if you "
            "Stop -- finished probes are saved and resume next time.")
    else:
        log("Note: every probe runs in a fresh process (isolated CUDA context per batch), so it "
            "opens with a brief silent model load (a few seconds locally) before any progress. "
            "That is normal, not a hang.")
    gui_event("BSTART", {"gpu": gpu_id, "model": model_tag, "remote": bool(remote),
                         "plan": [{"name": c["name"], "out_w": c["out_w"], "out_h": c["out_h"],
                                   "mp": round(c["mp"], 2)} for c in plan],
                         "batch_cap": cap, "frames": frames, "estimate_seconds": round(est)})

    session = None
    tele_stop = None
    if remote:
        # Deploy the pod for the picked card. A capacity failure (stock empty, no substitution)
        # raises here and lands cleanly in the benchmark log (RemoteSession emits the reason).
        engine, session = _deploy_remote_engine(cfg, vcfg)
        # Stream the pod's CPU/RAM/VRAM/temp to the GUI (RTELEM events) exactly like a real
        # remote run, so the benchmark window shows the same "Remote pod" row and MQTT gets
        # the system/remote/* topics. Best-effort; stopped at teardown.
        tele_stop = threading.Event()
        bv._start_remote_telemetry(engine, tele_stop)
    else:
        from local_video_engine import LocalVideoEngine
        repo_dir, model_dir = bv._local_seedvr2_paths(cfg)
        engine = LocalVideoEngine(repo_dir, model_dir, eff_settings,
                                  use_subprocess=True, conn=conn, gpu_id=gpu_id)

    def funds_tripped():
        return bool(session is not None and getattr(session, "_funds_tripped", False))

    def stop_now():
        return _STOP.is_set() or funds_tripped()

    probe_out = os.path.join(work, "probe_out.mp4")
    stopped = None
    results = []                                       # (name, ceiling, saved) per finished cell
    try:
        for cell in plan:
            if stop_now():
                stopped = "funds guard" if funds_tripped() else "stopped by user"
                break
            gui_event("BCELL", {"name": cell["name"], "out_w": cell["out_w"],
                                "out_h": cell["out_h"]})
            log(f"\n[{cell['name']}] output {cell['out_w']}x{cell['out_h']} "
                f"({cell['mp']:.2f} MP), source {cell['src_w']}x{cell['src_h']}:")

            # Drop collapsed phantom rows (frames < batch) so they are neither trusted as tried
            # nor as a ceiling; the affected batches get re-probed with correctly-sized clips.
            cell_probes = drop_collapsed(db.get_bench_probes(conn, gpu_id, bench_model,
                                                             cell["out_w"], cell["out_h"]))
            floor = sizer.vram_floor_batch(total_gb, cell["mp"])   # VRAM-aware start rung
            while not stop_now():
                # Free VRAM at probe start. LOCAL: read via nvidia-smi (parent stays GPU-free)
                # to tag the probe AND allow a contended earlier failure to be re-tried. REMOTE:
                # the pod's VRAM isn't visible here, and a dedicated pod has no contention, so
                # skip it (free_now=None disables the stale-contention re-probe path).
                free_now = None if remote else sizer.free_vram_gb(prefer_smi=True)[0]
                b = next_batch(cell_probes, floor, cap, free_now=free_now)
                if b is None:
                    break
                # The clip MUST have >= b frames: a temporal window can't exceed the clip's
                # frame count, or SeedVR2 silently collapses it to a SMALLER batch (a 37-frame
                # clip probing "batch 65" really ran batch 37). Size it to at least b so the
                # batch we probe is the batch that runs; cached per (dims, frames), so each
                # distinct size is synthesised once and reused.
                probe_frames = max(int(frames), int(b))
                try:
                    clip = bclip.ensure_source_clip(work, cell["source"], probe_frames, log=log)
                except Exception as exc:                # noqa: BLE001 (can't source: stop this target)
                    log(f"  could not prepare a {probe_frames}-frame source clip ({exc}); "
                        f"stopping this target.")
                    break
                # Warm on the SHORTEST clip that still slides the temporal window (batch+1
                # frames), not the full probe clip. The compiled graphs and the per-window VRAM
                # peak are set by the WINDOW (batch), not the clip length (docs 14: content is
                # irrelevant to the ceiling), so a batch+1 warmup absorbs the model load +
                # torch.compile at the SAME ceiling while running ~1-2 windows instead of dozens
                # -- nearly halving a small-batch probe. min() with probe_frames means a large
                # batch (whose probe clip is already ~1 window) reuses the full clip, so no extra
                # file is cut and there is no speedup to chase there. Fail-safe: if the short cut
                # fails, warm on the full clip (correct, just slower). NB the warmup's compile is
                # fully reused by the timed pass only when the timed pass has no differently-shaped
                # ragged final batch; uniform_batch_size (on for benchmarking) guarantees that.
                warmup_frames = min(probe_frames, int(b) + 1)
                warmup_clip = clip
                if warmup_frames < probe_frames:
                    try:
                        warmup_clip = bclip.ensure_source_clip(
                            work, cell["source"], warmup_frames, log=None)
                    except Exception:                   # noqa: BLE001 (warm on the full clip instead)
                        warmup_clip = clip
                gui_event("BPROBE", {"name": cell["name"], "batch": b, "state": "running"})
                log(f"  probing batch {b} …")
                res = engine.probe_batch(clip, probe_out, resolution=cell["resolution"],
                                         batch=b, frames=probe_frames, should_stop=stop_now,
                                         on_progress=_probe_pinger(log, f"batch {b}"),
                                         warmup_src=warmup_clip)
                if res["outcome"] == "stopped":
                    stopped = "funds guard" if funds_tripped() else "stopped by user"
                    break
                db.record_bench_probe(
                    conn, gpu_id, bench_model, cell["out_w"], cell["out_h"], b,
                    res["outcome"], frames=res.get("frames"), seconds=res.get("seconds"),
                    peak_alloc=res.get("peak_alloc_gb"), peak_reserved=res.get("peak_reserved_gb"),
                    free_vram=free_now,
                    peak_alloc_steady=res.get("peak_alloc_steady_gb"),
                    peak_reserved_steady=res.get("peak_reserved_steady_gb"))
                cell_probes = [p for p in cell_probes if int(p["batch"]) != b]
                cell_probes.append({"batch": b, "outcome": res["outcome"], "free_vram": free_now})
                spf = (res["seconds"] / res["frames"]) if res.get("frames") else None
                # Show BOTH peaks when the probe warmed: "peak X/Y GB (steady A/B)". They are
                # different questions -- the first is the ceiling (a real run's first segment
                # pays the cold+compile peak too), the second is what every later segment
                # costs. Fused into one number, a compile-bound ceiling is indistinguishable
                # from an inference-bound one.
                steady = ""
                if res.get("peak_alloc_steady_gb") is not None:
                    steady = (f" (steady {res['peak_alloc_steady_gb']}/"
                              f"{res['peak_reserved_steady_gb']})")
                log(f"    batch {b}: {res['outcome']}"
                    + (f", {res['seconds']:.0f}s ({spf:.2f} s/frame), "
                       f"peak {res.get('peak_alloc_gb')}/{res.get('peak_reserved_gb')} GB{steady}"
                       if res["outcome"] == "ok" else ""))
                gui_event("BPROBE", {"name": cell["name"], "batch": b, "state": "done",
                                     "outcome": res["outcome"],
                                     "seconds": round(res.get("seconds") or 0, 1),
                                     "spf": round(spf, 2) if spf else None,
                                     "peak_alloc": res.get("peak_alloc_gb"),
                                     "peak_reserved": res.get("peak_reserved_gb")})
                # NB: do NOT break on an oom/thrash. The geometric climb overshoots the wall on
                # purpose; next_batch then binary-refines DOWNWARD (probing smaller batches) to
                # pin the exact max-fit rung, and ends the cell by returning None. Breaking here
                # would stop at the first (overshot) failure and mis-record the ceiling.
            if stop_now() and stopped is None:
                stopped = "funds guard" if funds_tripped() else "stopped by user"
            ceil, saved = _record_cell_result(conn, gpu_id, bench_model, cell, learn_key)
            results.append((cell["name"], ceil, saved))
            if saved:
                extra = f" (max fit {ceil})" if ceil and ceil != saved else ""
                log(f"  {cell['name']}: saved batch {saved}{extra}. AUTO runs use {saved} "
                    f"(the fastest window; the max that fits can be slower, from VRAM spill or "
                    f"measurement noise).")
            else:
                log(f"  {cell['name']}: no batch fit (card can't do this target)")
            gui_event("BCEILING", {"name": cell["name"], "ceiling": ceil, "saved": saved,
                                   "overlap": sizer.auto_overlap(saved) if saved else None})
            if stopped:
                break
    finally:
        if tele_stop is not None:
            tele_stop.set()                            # end the pod telemetry sampler
        try:
            engine.close()
        except Exception:                              # noqa: BLE001
            pass
        if session is not None:
            # Tear the pod down (billing stops). RemoteSession.close() stops a pod we
            # created; a pod we reused (attached) is left as we found it.
            try:
                session.close()
            except Exception:                          # noqa: BLE001 (fail-safe teardown)
                pass
            gui_event("POD", "")
        try:
            if os.path.exists(probe_out):
                os.remove(probe_out)
        except OSError:
            pass

    summary = {"cells": len(plan), "stopped": stopped}
    gui_event("BDONE", summary)
    _log_summary_table(conn, gpu_id, bench_model, plan)
    tail = ("Results saved; AUTO runs and the time estimate now use them."
            if not remote else
            "Results saved; remote runs on this card now seed from them.")
    log(f"\nBenchmark {'stopped' if stopped else 'complete'}: {len(plan)} target(s). " + tail)
    _notify_benchmark(notify_settings, gpu_id, model_tag, remote, results, stopped,
                      time.monotonic() - t_start)
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
    p.add_argument("--remote", action="store_true",
                   help="benchmark a rented RunPod GPU (the card in IMGTBX_GPU_OVERRIDE) "
                        "instead of the local card (feature #7, docs section 22).")
    args = p.parse_args(argv)

    _open_log()
    if GUI_MODE:
        threading.Thread(target=_watch_stdin_for_stop, daemon=True).start()
    targets = [t.strip() for t in args.targets.split(",") if t.strip()]
    try:
        run_benchmark(targets, frames=args.frames, resume=not args.restart,
                      batch_cap=args.batch_cap, remote=args.remote)
    except Exception as exc:                            # noqa: BLE001
        import traceback
        log(f"Benchmark failed: {exc}")
        if _LOG_FH is not None:
            _LOG_FH.write(traceback.format_exc() + "\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
