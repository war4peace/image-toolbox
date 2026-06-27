#!/usr/bin/env python3
"""
ram_probe.py — SYSTEM-RAM test for the SeedVR2 video path (Video Upscaler #2).

Validates the frame-accumulation concern in docs/video-upscaler.md section 8:
SeedVR2 run load-all (`chunk_size = 0`) holds every output frame uncompressed in
RAM before encoding, so RAM scales with segment length; streaming (`chunk_size >
0`) writes each chunk and frees it, bounding RAM to ~`chunk_size x frame_bytes`.

Runs ON the pod. Loads the engine once, then for each --chunk-sizes value processes
the SAME frames through the **real** `inference_cli.process_single_file` (the
streaming path the worker will use) and reports peak process RSS during processing.
The delta over the post-load baseline is the frame-accumulation footprint: it should
balloon for load-all and stay small + flat for streaming. `--frames` (load_cap)
sizes the segment without needing an ffmpeg slice.

    python ram_probe.py --video Pisici.AVI \
        --repo-dir /workspace/seedvr2 --model-dir /workspace/models/seedvr2 \
        --target 1080 --batch-size 13 --frames 900 --chunk-sizes 0 33

`upscale_engine.py` must sit beside this file. Needs psutil (present on the pod).
"""
import os
import sys
import time
import argparse
import threading

GB = 1024 ** 3


def main(argv=None):
    p = argparse.ArgumentParser(description="Peak system-RAM test: load-all vs streaming.")
    p.add_argument("--video", required=True)
    p.add_argument("--repo-dir", required=True)
    p.add_argument("--model-dir", required=True)
    p.add_argument("--target", type=int, default=1080, help="short-side output resolution")
    p.add_argument("--batch-size", type=int, default=13)
    p.add_argument("--frames", type=int, default=900, help="frames to process (load_cap); 30s@30fps")
    p.add_argument("--chunk-sizes", type=int, nargs="+", default=[0, 33],
                   help="0 = load-all (no streaming); N = stream N frames per chunk")
    p.add_argument("--out-dir", default="/root/bench/ram_out")
    args = p.parse_args(argv)

    sys.path.insert(0, args.repo_dir)
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    # Allocator ordering: set before any torch import (see bench_video.py).
    os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "backend:cudaMallocAsync")

    from upscale_engine import UpscaleEngine                        # noqa: E402 (light)

    t0 = time.time()
    engine = UpscaleEngine(args.repo_dir, args.model_dir, {})
    import psutil                                                   # noqa: E402
    import torch                                                    # noqa: E402
    print(f"[ram] engine loaded in {time.time() - t0:.1f}s on {engine.device_name}", flush=True)

    os.makedirs(args.out_dir, exist_ok=True)
    engine.args.resolution = int(args.target)
    engine.args.batch_size = int(args.batch_size)
    engine.args.seed = 42
    engine.args.temporal_overlap = 0
    engine.args.load_cap = int(args.frames)        # cap segment length (no slicing)
    engine.args.output_format = "mp4"
    proc = psutil.Process()

    print(f"[ram] target={args.target} batch_size={args.batch_size} "
          f"frames={args.frames} (~{args.frames/30:.0f}s @30fps)", flush=True)

    for cs in args.chunk_sizes:
        engine.args.chunk_size = int(cs)
        torch.cuda.empty_cache()
        baseline = proc.memory_info().rss
        peak = [baseline]
        stop = threading.Event()

        def sampler():
            while not stop.is_set():
                try:
                    peak[0] = max(peak[0], proc.memory_info().rss)
                except Exception:
                    pass
                time.sleep(0.25)

        th = threading.Thread(target=sampler, daemon=True)
        th.start()
        out = os.path.join(args.out_dir, f"out_cs{cs}.mp4")
        t1 = time.time()
        try:
            n = engine._cli.process_single_file(
                args.video, engine.args, ["0"],
                output_path=out, runner_cache=engine._runner_cache)
        finally:
            stop.set()
            th.join()
        dt = time.time() - t1
        label = "load-all" if cs == 0 else f"chunk={cs}"
        print(f"[ram] {label:>10}: frames={n}  time={dt:.1f}s  "
              f"baseline={baseline/GB:.1f}GB  peakRSS={peak[0]/GB:.1f}GB  "
              f"frame-accum_delta={(peak[0]-baseline)/GB:.1f}GB", flush=True)
        try:
            os.remove(out)
        except OSError:
            pass

    engine.close()
    print("[ram] done.", flush=True)


if __name__ == "__main__":
    main()
