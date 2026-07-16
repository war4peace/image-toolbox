"""
local_video_worker.py
---------------------
Upscale ONE video segment in a fresh subprocess (feature #7, thrash watchdog,
docs/local-video-upscaler.md section 17). This is the local, no-pod analogue of
pod/worker.py's per-segment work, run as a CHILD of LocalVideoEngine so the parent
can KILL it if it thrashes (a synchronous in-process CUDA call can't be interrupted
from a thread, docs 14.2/14.3). Running each attempt in its own process also gives a
FRESH CUDA context -- no fragmentation carryover, so an OOM retry at a smaller batch
starts clean instead of inheriting the failed attempt's fragmented pool.

One process = one `UpscaleEngine.process_video` call at a FIXED batch (the parent
owns sizing + OOM-retry, spawning a new worker per attempt). Progress streams to
stdout (the parent's watchdog reads it for a liveness heartbeat); the outcome is a
single marker line the parent parses:

    @@LVW-RESULT@@ frames=<n> alloc=<gb> reserved=<gb> [seconds=<s>]   (success, exit 0)
    @@LVW-OOM@@ batch=<b>                                              (CUDA OOM,  exit 42)
    (any other exception -> traceback on stderr,                       exit 1)

`--warmup-passes N` (benchmark only) runs N throwaway passes before the timed one and
reports `seconds=` for the TIMED pass alone. `--warmup-input CLIP` points those warmup
passes at a SHORT clip (a few frames longer than the window) instead of the full timed
clip: what a warmup must reproduce is the per-SHAPE compile and the model load, and the
compiled graph + the per-window VRAM peak are set by the WINDOW (--batch), not by how many
frames the clip has (encode/decode iterate windows of --batch; the decoded output streams
to CPU). So a batch+1-frame warmup hits the SAME ceiling and warms the SAME graphs while
running ~1-2 windows instead of dozens, cutting a small-batch probe's cost nearly in half.
Omitted, the warmup falls back to --input (the pre-0.5.0 behaviour).

It exists because a probe is a FRESH PROCESS per rung while a real run is ONE process for
a whole video, so a probe pays per-measurement what production pays once per run:

  * the model load. UpscaleEngine.__init__ only DOWNLOADS the weights; the 16 GB VRAM load
    happens inside the FIRST process_video (later segments reuse `_runner_cache`). So every
    probe, compiled or not, charged a model load to its rate: measured, a 37-frame probe at
    bs17 stored 1.01 s/frame where the upscale itself ran at roughly half that.
  * torch.compile's per-process cost (dynamo trace + guard build + inductor cache load),
    ~30s at every new shape, and every rung IS a new shape under static compile. That is
    ~0.9 s/frame of artifact on a 37-frame probe, which was enough to invert the
    compile-vs-uncompiled verdict entirely.

Warming in a SEPARATE process cannot fix either: the charges are per-process, and the
compile one survived an already-warm on-disk inductor cache. So the warm pass happens here,
and the timed pass measures what a real segment actually costs.

Run with `python -u` so the parent sees progress in real time. Must run in the venv.
"""

import os
import sys
import json
import time
import argparse

APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(APP_ROOT, "scripts"))

OOM_EXIT = 42
RESULT_MARK = "@@LVW-RESULT@@"
OOM_MARK = "@@LVW-OOM@@"


def _is_oom(exc):
    try:
        from runner_common import is_oom_error
        return is_oom_error(exc)
    except Exception:                                # noqa: BLE001
        s = str(exc).lower()
        return "out of memory" in s or "cuda oom" in s


def main(argv=None):
    ap = argparse.ArgumentParser(description="Upscale one video segment (subprocess worker).")
    ap.add_argument("--repo-dir", required=True)
    ap.add_argument("--model-dir", required=True)
    ap.add_argument("--settings", required=True, help="path to a JSON file of the engine settings")
    ap.add_argument("--input", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--resolution", type=int, required=True)
    ap.add_argument("--batch", type=int, required=True)
    ap.add_argument("--chunk", type=int, required=True)
    ap.add_argument("--overlap", type=int, default=0)
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--video-backend", default="opencv")
    ap.add_argument("--use-10bit", action="store_true")
    ap.add_argument("--warmup-passes", type=int, default=0,
                    help="throwaway passes before the timed one, to absorb torch.compile's "
                         "per-process cost (benchmark only; see the module docstring)")
    ap.add_argument("--warmup-input", default=None,
                    help="a SHORT clip (>= batch+1 frames) the warmup passes run on instead of "
                         "--input; the window shape they compile and the peak they hit are set "
                         "by --batch, not the clip length, so this warms the same graphs at the "
                         "same ceiling far cheaper. Falls back to --input when omitted.")
    args = ap.parse_args(argv)

    # UTF-8 stdout before importing seedvr2 (its emoji banners crash cp1252 pipes).
    try:
        from runner_common import harden_stdout
        harden_stdout()
    except Exception:                                # noqa: BLE001
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:                            # noqa: BLE001
            pass

    with open(args.settings, "r", encoding="utf-8") as f:
        settings = json.load(f)

    # A fresh worker is SILENT during torch + CUDA init + model load (import time, before any
    # pipeline output). Locally that is usually a few seconds (weights are on a warm local
    # disk); it is only a cold torch/CUDA init that can stretch it. With no heartbeat that
    # dead air looks like a hang -- the reason a healthy probe got Stopped early. Emit an
    # immediate liveness line (flushed) BEFORE the heavy import so the parent forwards it to
    # the GUI at once, and it also resets the parent's thrash-stall timer.
    print(f"Loading CUDA + SeedVR2 model for batch {args.batch} "
          f"(a few seconds; no progress until it loads) ...", flush=True)

    from upscale_engine import UpscaleEngine
    engine = UpscaleEngine(args.repo_dir, args.model_dir, settings,
                           debug=bool(settings.get("debug", False)))
    print(f"Model loaded; upscaling {args.resolution}px at batch {args.batch} ...", flush=True)

    def _reset_peaks():
        try:
            import torch
            torch.cuda.reset_peak_memory_stats()
        except Exception:                            # noqa: BLE001
            pass

    def _peaks():
        """(alloc, reserved) GB since the last reset, then RESET so the NEXT pass is measured
        on its own. Per-pass, because the cold pass (model load + torch.compile, whose
        inductor autotuning allocates at the compiled shape) and the warm pass cost very
        different amounts, and fusing them hides which one a ceiling is made of.
        (None, None) if torch can't answer."""
        try:
            import torch
            gb = 1024 ** 3
            a = round(torch.cuda.max_memory_allocated() / gb, 1)
            r = round(torch.cuda.max_memory_reserved() / gb, 1)
            torch.cuda.reset_peak_memory_stats()
            return a, r
        except Exception:                            # noqa: BLE001
            return None, None

    _reset_peaks()                                   # start the first pass from a clean mark

    def _pass(inp):
        return engine.process_video(
            inp, args.output,
            resolution=args.resolution, batch_size=args.batch,
            chunk_size=args.chunk, temporal_overlap=args.overlap,
            seed=args.seed, video_backend=args.video_backend,
            use_10bit=args.use_10bit, capture=False)

    def _release():
        """Hand the warmup's cached blocks back before the timed pass.

        A probe is a fresh process precisely so a previous attempt's fragmented VRAM cannot
        poison it (an in-process sweep fragments and UNDER-REPORTS the ceiling; Windows has no
        `expandable_segments` to save it). Running a warmup here puts TWO passes in one process
        and re-opens exactly that door, so the warmup's pool is returned to the driver before
        the timed pass allocates. This does NOT hide the warmup's cost from the ceiling:
        reset_peak_memory_stats is not called again, so the reported peak still spans both.
        """
        try:
            import torch
            torch.cuda.synchronize()
            torch.cuda.empty_cache()
        except Exception:                            # noqa: BLE001
            pass

    n = None
    seconds = None
    phase = "timed"
    cold_a = cold_r = None                           # worst warmup pass (cold + compile)
    try:
        warm_in = args.warmup_input or args.input     # a short clip if given, else the timed one
        short = " (short clip)" if args.warmup_input else ""
        for i in range(max(0, int(args.warmup_passes))):
            phase = "warmup"
            print(f"Warmup pass {i + 1}/{args.warmup_passes} at batch {args.batch}{short} "
                  f"(discarded; absorbs torch.compile) ...", flush=True)
            _pass(warm_in)
            a, r = _peaks()                          # this warmup's peak, and reset for the next
            cold_a = max(cold_a or 0, a or 0) or None
            cold_r = max(cold_r or 0, r or 0) or None
            _release()
        phase = "timed"
        t0 = time.time()
        n = _pass(args.input)
        seconds = time.time() - t0
    except Exception as exc:                          # noqa: BLE001
        if _is_oom(exc):
            # An OOM in the WARMUP is the same answer as an OOM in the timed pass: this batch
            # does not fit. Report it identically so the sweep records the ceiling -- but SAY
            # which pass died. The distinction is the only evidence that separates a real
            # ceiling (the warmup, i.e. the cold+compile peak, could not fit) from this
            # warmup's own side effect (the timed pass could not fit in the pool the warmup
            # left behind), and without it a spurious OOM is indistinguishable from a wall.
            print(f"{OOM_MARK} batch={args.batch} pass={phase}", flush=True)
            try:
                engine.close()
            except Exception:                        # noqa: BLE001
                pass
            return OOM_EXIT
        raise

    # The timed pass's own peak (the warmup's was taken + reset above), then the pair the
    # ceiling is made of: the WORST over every pass, because a real run's first segment pays
    # the cold+compile peak too. Reporting only the steady one would read as free headroom
    # that production does not have. When the warmup ran on a SHORT clip its cold peak is a
    # per-window number (the peak is set by the window, not the clip length), so it is directly
    # comparable to the full timed pass's steady peak -- the ceiling is still their max.
    steady_a, steady_r = _peaks()
    alloc = max(steady_a or 0, cold_a or 0)
    reserved = max(steady_r or 0, cold_r or 0)
    try:
        engine.close()
    except Exception:                                # noqa: BLE001
        pass

    extra = f" seconds={seconds:.3f}" if seconds is not None else ""
    if cold_a is not None:                           # only a warmed probe has two passes
        extra += f" alloc_steady={steady_a} reserved_steady={steady_r}"
    print(f"{RESULT_MARK} frames={int(n or 0)} alloc={alloc} reserved={reserved}{extra}",
          flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
