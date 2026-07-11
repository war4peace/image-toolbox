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

    @@LVW-RESULT@@ frames=<n> alloc=<gb> reserved=<gb>     (success, exit 0)
    @@LVW-OOM@@ batch=<b>                                  (CUDA OOM,  exit 42)
    (any other exception -> traceback on stderr,           exit 1)

Run with `python -u` so the parent sees progress in real time. Must run in the venv.
"""

import os
import sys
import json
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

    from upscale_engine import UpscaleEngine
    engine = UpscaleEngine(args.repo_dir, args.model_dir, settings,
                           debug=bool(settings.get("debug", False)))

    try:
        import torch
        torch.cuda.reset_peak_memory_stats()
    except Exception:                                # noqa: BLE001
        pass

    try:
        n = engine.process_video(
            args.input, args.output,
            resolution=args.resolution, batch_size=args.batch,
            chunk_size=args.chunk, temporal_overlap=args.overlap,
            seed=args.seed, video_backend=args.video_backend,
            use_10bit=args.use_10bit, capture=False)
    except Exception as exc:                          # noqa: BLE001
        if _is_oom(exc):
            print(f"{OOM_MARK} batch={args.batch}", flush=True)
            try:
                engine.close()
            except Exception:                        # noqa: BLE001
                pass
            return OOM_EXIT
        raise

    alloc = reserved = 0.0
    try:
        import torch
        gb = 1024 ** 3
        alloc = round(torch.cuda.max_memory_allocated() / gb, 1)
        reserved = round(torch.cuda.max_memory_reserved() / gb, 1)
    except Exception:                                # noqa: BLE001
        pass
    try:
        engine.close()
    except Exception:                                # noqa: BLE001
        pass

    print(f"{RESULT_MARK} frames={int(n or 0)} alloc={alloc} reserved={reserved}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
