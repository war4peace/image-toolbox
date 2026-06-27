#!/usr/bin/env python3
"""
bench_video.py — SeedVR2 video-settings benchmark (Video Upscaler #2, Phase 1).

Runs ON the pod. Answers the two open questions in docs/video-upscaler.md (#7/#13)
that need a real GPU and cannot be derived from source:

  1. Per-frame throughput for the **temporal-batch** video path at each target
     resolution (the cost estimator needs this; the carried-over image numbers are
     single-frame rates and may differ once batch_size > 1).
  2. The max-viable `batch_size` per (target x card) and its VRAM curve (drives the
     defaults and the 4K-needs-a-big-card warning).

Method: load the SeedVR2 engine ONCE (reusing the app's UpscaleEngine setup, models
from the network volume), then for each (target short-side resolution x batch_size)
process exactly **one temporal window of `batch_size` frames** through the engine's
video core and measure it. One window is the steady-state unit of video throughput,
so per-frame = window_time / batch_size, and the window's peak VRAM is what decides
whether that batch_size fits. An out-of-memory error marks the ceiling for that
target (larger batch sizes are skipped). Frames come straight from the source video
via the engine's own frame reader, so preprocessing matches a real run exactly.

This drives the real `_process_frames_core` the worker will call, so the numbers are
representative of production. It does NOT use ffmpeg (no split/mux here — that is the
local Phase 2 pipeline); it reads frames directly with the engine's loader.

Usage (on the pod, with the volume venv's python):

    python bench_video.py --video Pisici.AVI \
        --repo-dir /workspace/seedvr2 \
        --model-dir /workspace/models/seedvr2 \
        [--settings worker_settings.json] \
        [--targets 1080 1440 2160] \
        [--batch-sizes 1 5 9 13 21 29 45 61 89] \
        [--temporal-overlap 0] [--seed 42] \
        [--out-csv bench_video_results.csv]

`upscale_engine.py` must sit beside this file (scp them together, same as
upscale_one.py / worker.py).
"""
import os
import sys
import csv
import json
import time
import argparse

GB = 1024 ** 3


def _coerce_4n1(n):
    """SeedVR2 requires batch_size to follow 4n+1 (1, 5, 9, ...). Round DOWN to the
    nearest valid value so a careless ladder entry can't silently misbehave."""
    n = max(1, int(n))
    return n - ((n - 1) % 4)


def main(argv=None):
    p = argparse.ArgumentParser(description="SeedVR2 video-settings benchmark (runs on the pod).")
    p.add_argument("--video", required=True, help="source video file to read frames from")
    p.add_argument("--repo-dir", required=True, help="path to the cloned seedvr2 repo")
    p.add_argument("--model-dir", required=True, help="dir holding the .safetensors weights")
    p.add_argument("--settings", default=None,
                   help="optional upscale-settings JSON (the worker's); defaults to engine defaults")
    p.add_argument("--targets", type=int, nargs="+", default=[1080, 1440, 2160],
                   help="target SHORT-SIDE resolutions to test (1080=1080p, 1440, 2160=4K)")
    p.add_argument("--batch-sizes", type=int, nargs="+",
                   default=[1, 5, 9, 13, 21, 29, 45, 61, 89],
                   help="temporal window sizes to test (coerced to 4n+1, ascending)")
    p.add_argument("--temporal-overlap", type=int, default=0)
    p.add_argument("--seed", type=int, default=42, help="fixed seed (per the v1 fixed-seed decision)")
    p.add_argument("--out-csv", default="bench_video_results.csv")
    args = p.parse_args(argv)

    sys.path.insert(0, args.repo_dir)                               # seedvr2 engine
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # upscale_engine.py

    # Establish the CUDA allocator backend the SeedVR2 CLI expects BEFORE torch
    # loads its allocator. inference_cli.py does
    #   os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "backend:cudaMallocAsync")
    # at import time. If torch is imported first (a bare `import torch`), it loads
    # the NATIVE allocator and the later switch crashes with "Allocator backend
    # parsed at runtime != at load time - No available backend!" + a segfault. The
    # worker path avoids this because UpscaleEngine.__init__ imports inference_cli
    # (which sets this) before torch. Set it here too, then let the engine do the
    # heavy imports in the right order; grab torch/cv2 only afterwards.
    os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "backend:cudaMallocAsync")

    from upscale_engine import UpscaleEngine                        # noqa: E402 (light)

    settings = {}
    if args.settings:
        with open(args.settings, encoding="utf-8") as f:
            settings = json.load(f)

    batch_sizes = sorted({_coerce_4n1(b) for b in args.batch_sizes})
    max_bs = max(batch_sizes)

    # ── load the engine once (models from the volume) ──────────────────────────
    # This imports inference_cli (sets the allocator env) then torch, in that order.
    t0 = time.time()
    engine = UpscaleEngine(args.repo_dir, args.model_dir, settings)

    # torch + cv2 are now initialised by the engine; importing them here just binds
    # the already-loaded modules (no re-init, correct ordering preserved).
    import torch                                                    # noqa: E402
    import cv2                                                      # noqa: E402
    total_vram = torch.cuda.get_device_properties(0).total_memory / GB
    print(f"[bench] engine loaded in {time.time() - t0:.1f}s on {engine.device_name} "
          f"({total_vram:.0f} GB VRAM, resident={engine.resident})", flush=True)

    # ── read enough frames once, via the engine's own video loader ─────────────
    cap = cv2.VideoCapture(args.video)
    if not cap.isOpened():
        print(f"[bench] ERROR: cannot open video: {args.video}", flush=True)
        return 2
    src_fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
    src_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    src_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    frames = engine._cli._read_frames_from_cap(cap, max_bs)
    cap.release()
    if frames is None or frames.shape[0] < max_bs:
        have = 0 if frames is None else frames.shape[0]
        print(f"[bench] ERROR: video has only {have} frames, need {max_bs} "
              f"for the largest batch size.", flush=True)
        return 2
    print(f"[bench] source {src_w}x{src_h} @ {src_fps:.2f} fps; read {frames.shape[0]} frames.",
          flush=True)

    engine.args.seed = int(args.seed)
    engine.args.temporal_overlap = int(args.temporal_overlap)
    engine.args.uniform_batch_size = False     # one full window; no final-batch padding

    # Warm up: the FIRST _run_core call materialises DiT/VAE onto the GPU and builds
    # the runner template (a one-time ~14 s cost here). Do it untimed so it doesn't
    # contaminate the first measured config (otherwise bs=1 looks ~20x too slow).
    engine.args.resolution = int(args.targets[0])
    engine.args.batch_size = 1
    print("[bench] warmup (materialising DiT/VAE to GPU, building runner) …", flush=True)
    _w = engine._run_core(frames[:1].contiguous())
    del _w
    torch.cuda.synchronize()
    torch.cuda.empty_cache()
    print("[bench] warmup done.", flush=True)

    rows = []
    for target in args.targets:
        engine.args.resolution = int(target)
        print(f"\n[bench] === target short-side {target}px ===", flush=True)
        for bs in batch_sizes:
            window = frames[:bs].contiguous()
            engine.args.batch_size = bs

            torch.cuda.empty_cache()
            torch.cuda.reset_peak_memory_stats()
            torch.cuda.synchronize()
            t1 = time.time()
            status, peak_gb, out_dims = "ok", None, ""
            try:
                result = engine._run_core(window)
                torch.cuda.synchronize()
                elapsed = time.time() - t1
                peak_gb = torch.cuda.max_memory_reserved() / GB
                # result is [T, H, W, C]; record the upscaled frame size once.
                out_dims = f"{int(result.shape[2])}x{int(result.shape[1])}"
                del result
            except RuntimeError as exc:
                elapsed = time.time() - t1
                # OOM wording varies by allocator: the native allocator says "out of
                # memory"; cudaMallocAsync (what inference_cli selects) says
                # "Allocation on device … would exceed allowed memory".
                msg = str(exc).lower()
                is_oom = ("out of memory" in msg
                          or "allocation on device" in msg
                          or "exceed allowed memory" in msg)
                if is_oom:
                    status = "oom"
                    print(f"[bench]   bs={bs:>3}  OUT OF MEMORY (ceiling for this target)",
                          flush=True)
                else:
                    status = "error"
                    print(f"[bench]   bs={bs:>3}  ERROR: {exc}", flush=True)
                torch.cuda.empty_cache()

            if status == "ok":
                per_frame = elapsed / bs
                print(f"[bench]   bs={bs:>3}  window={elapsed:7.1f}s  "
                      f"per-frame={per_frame:6.2f}s  peakVRAM={peak_gb:5.1f}GB  out={out_dims}",
                      flush=True)
                rows.append((engine.device_name, f"{total_vram:.0f}", target, bs,
                             out_dims, f"{elapsed:.2f}", f"{per_frame:.3f}",
                             f"{peak_gb:.1f}", status))
            else:
                rows.append((engine.device_name, f"{total_vram:.0f}", target, bs,
                             out_dims, f"{elapsed:.2f}", "", "" if peak_gb is None
                             else f"{peak_gb:.1f}", status))

            if status == "oom":
                break    # larger batch sizes for this target would only OOM too

    engine.close()

    with open(args.out_csv, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["gpu", "vram_gb", "target_short_px", "batch_size", "out_dims",
                    "window_s", "per_frame_s", "peak_vram_gb", "status"])
        w.writerows(rows)
    print(f"\n[bench] wrote {len(rows)} rows -> {args.out_csv}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
