"""
spike_local_video.py
--------------------
Feasibility spike for LOCAL video upscaling (feature #7, docs/local-video-upscaler.md,
step 2 "LocalVideoEngine spike").

Proves the ONE thing everything else depends on: that SeedVR2 video upscaling runs
IN-PROCESS on the local GPU, end to end, producing a valid upscaled clip. It cuts a
short clip from a source (the source is never touched), runs it through
`LocalVideoEngine.process_segment` (the same call the batch runner will make), then
probes the result and prints a PASS/FAIL report with timing and PEAK VRAM.

This is NOT the feature: no GUI, no queue, no custom-target UI. It is the smallest
decisive test, and it doubles as the seed for the benchmark harness (section 8):
crank `--resolution` up until it OOMs to find this card's real ceiling.

Run inside the venv:
    .venv\\Scripts\\python.exe scripts\\spike_local_video.py
    .venv\\Scripts\\python.exe scripts\\spike_local_video.py path\\to\\source.mp4 \\
        --seconds 5 --resolution 1080 --batch 0

NOTE: the SeedVR2 weights (~16 GB) download on first run if models/SEEDVR2 is empty.
"""

import os
import sys
import argparse
import traceback

APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(APP_ROOT, "scripts"))

DEFAULT_SOURCE = os.path.join(APP_ROOT, "benchmark-videos", "Pisici.AVI")


def _resolve(p, default):
    # Expand %USERPROFILE% / ~ etc first (config stores model_dir as a %VAR% path),
    # matching batch_upscale._resolve_path; only then decide absolute vs APP_ROOT-relative.
    p = os.path.expandvars(os.path.expanduser(p or ""))
    if p and os.path.isabs(p):
        return os.path.abspath(p)
    return os.path.join(APP_ROOT, p or default)


def _load_settings():
    """The engine's settings = the 'upscale' section merged with the 'video' knobs
    (video wins), so the video model / compile flags / noise apply. Unknown keys are
    ignored by UpscaleEngine._build_args, so the merge is safe."""
    try:
        from runner_common import load_config
        cfg = load_config()
    except Exception:                                # noqa: BLE001
        import json
        with open(os.path.join(APP_ROOT, "config.json"), "r", encoding="utf-8") as f:
            cfg = json.load(f)
    seed = cfg.get("seedvr2", {})
    repo_dir = _resolve(seed.get("repo_dir", ""), "seedvr2")
    model_dir = _resolve(seed.get("model_dir", ""), os.path.join("models", "SEEDVR2"))
    settings = {**cfg.get("upscale", {}), **cfg.get("video", {})}
    return repo_dir, model_dir, settings, cfg.get("video", {})


def _progress(st):
    state = st.get("state")
    if state == "running" and st.get("resolved_batch"):
        if st.get("oom_backoff"):
            print(f"  [OOM] retry at batch {st['resolved_batch']} "
                  f"(overlap {st.get('resolved_overlap')})", flush=True)
        else:
            print(f"  resolved window: batch {st['resolved_batch']}, "
                  f"overlap {st.get('resolved_overlap')}, "
                  f"attention {st.get('resolved_attention')}, "
                  f"frames {st.get('total_frames')}", flush=True)
    elif state == "done":
        print(f"  done: {st.get('frames_written')} frames in "
              f"{st.get('seconds', 0):.1f}s, peak VRAM {st.get('peak_alloc_gb')} GB "
              f"alloc / {st.get('peak_reserved_gb')} GB reserved", flush=True)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Local video upscaling feasibility spike.")
    ap.add_argument("source", nargs="?", default=DEFAULT_SOURCE,
                    help=f"source video (default: {DEFAULT_SOURCE})")
    ap.add_argument("--out", default=None, help="output mp4 (default: alongside source)")
    ap.add_argument("--seconds", type=float, default=5.0, help="clip length to test")
    ap.add_argument("--resolution", type=int, default=1080,
                    help="SeedVR2 output SHORT side (px). Raise until OOM to find the ceiling.")
    ap.add_argument("--batch", type=int, default=0, help="batch_size (0 = AUTO/floor)")
    ap.add_argument("--overlap", type=int, default=0, help="temporal_overlap")
    ap.add_argument("--dit-model", default=None,
                    help="override the SeedVR2 DiT model filename (downloads if absent), "
                         "e.g. seedvr2_ema_7b_fp8_e4m3fn_mixed_block35_fp16.safetensors")
    ap.add_argument("--video-backend", default="opencv", choices=["opencv", "ffmpeg"],
                    help="opencv is the most dependency-light for the spike")
    ap.add_argument("--use-10bit", action="store_true")
    ap.add_argument("--debug", action="store_true", help="verbose SeedVR2 pipeline output")
    args = ap.parse_args(argv)

    # seedvr2 prints emojis at import time; a piped stdout defaults to cp1252 on
    # Windows and crashes on them. Every real runner hardens stdout to UTF-8 first.
    try:
        from runner_common import harden_stdout
        harden_stdout()
    except Exception:                                # noqa: BLE001
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:                            # noqa: BLE001
            pass

    src = os.path.abspath(args.source)
    if not os.path.exists(src):
        print(f"FAIL: source not found: {src}")
        return 2

    import video_pipeline as vp
    from local_video_engine import LocalVideoEngine

    repo_dir, model_dir, settings, _vcfg = _load_settings()
    if args.dit_model:
        settings = {**settings, "dit_model": args.dit_model}
    if args.debug:
        settings = {**settings, "debug": True}

    work_dir = os.path.join(APP_ROOT, "benchmark-videos", "_spike")
    os.makedirs(work_dir, exist_ok=True)
    clip = os.path.join(work_dir, "clip_src.mkv")
    out = args.out or os.path.join(work_dir, "clip_local_upscaled.mp4")

    print("=" * 68)
    print("LOCAL VIDEO UPSCALING SPIKE")
    print("=" * 68)
    print(f"source     : {src}")
    print(f"repo/model : {repo_dir}  |  {model_dir}")
    print(f"dit_model  : {settings.get('dit_model') or 'seedvr2_ema_7b_fp16.safetensors (default)'}")
    if not os.path.isdir(model_dir) or not any(
            f.endswith(".safetensors") for f in os.listdir(model_dir) if os.path.isfile(os.path.join(model_dir, f))):
        print("note       : SeedVR2 weights not present -> ~16 GB will download on first run.")

    try:
        info = vp.probe(src, count=True)
        print(f"in         : {info.width}x{info.height}, {info.nb_frames} frames, "
              f"{float(info.fps):.2f} fps, {info.duration:.1f}s")
        end = min(args.seconds, info.duration or args.seconds)
        print(f"cutting    : [0, {end:.1f}s) -> {clip}")
        vp.extract_clip(info, 0.0, end, clip)
        clip_info = vp.probe(clip, count=True)
        print(f"clip       : {clip_info.width}x{clip_info.height}, "
              f"{clip_info.nb_frames} frames")
    except Exception as exc:                          # noqa: BLE001
        print(f"FAIL (clip extraction): {exc}")
        traceback.print_exc()
        return 3

    print(f"target     : short side {args.resolution}px, batch "
          f"{args.batch or 'AUTO'}, backend {args.video_backend}, "
          f"10bit {'on' if args.use_10bit else 'off'}")
    print("-" * 68)

    engine = None
    try:
        print("loading SeedVR2 engine (first run downloads weights + is slow)...", flush=True)
        engine = LocalVideoEngine(repo_dir, model_dir, settings, debug=args.debug)
        print(f"device     : {engine.device_name} (resident={engine.resident})")
        n = engine.process_segment(
            clip, out, resolution=args.resolution, batch_size=args.batch,
            chunk_size=0, temporal_overlap=args.overlap,
            video_backend=args.video_backend, use_10bit=args.use_10bit,
            on_progress=_progress)
    except Exception as exc:                           # noqa: BLE001
        print("-" * 68)
        print(f"FAIL (upscale): {type(exc).__name__}: {exc}")
        traceback.print_exc()
        return 4
    finally:
        if engine is not None:
            engine.close()

    print("-" * 68)
    try:
        out_info = vp.probe(out, count=True)
    except Exception as exc:                           # noqa: BLE001
        print(f"FAIL (output unreadable): {exc}")
        return 5

    ok = bool(out_info.nb_frames and out_info.nb_frames > 0
              and out_info.width > clip_info.width)
    secs = engine.last_segment_seconds or 0
    out_mp = (out_info.width * out_info.height) / 1e6
    spf = (secs / n) if n else 0
    spmp = (secs / (out_mp * n)) if (n and out_mp) else 0
    print(f"out        : {out_info.width}x{out_info.height}, {out_info.nb_frames} frames "
          f"({out_mp:.2f} MP/frame)")
    req_b = args.batch or "AUTO"
    ran_b = engine.last_resolved_batch
    fell = " (fell back from requested)" if (ran_b and args.batch and ran_b < args.batch) else ""
    print(f"window     : requested batch {req_b}/ov {args.overlap}  ->  RAN batch "
          f"{ran_b}/ov {engine.last_overlap}{fell}")
    print(f"time       : {secs:.1f}s  ->  {spf:.2f} s/frame, {spmp:.2f} s/output-MP")
    print(f"file       : {out}")
    print("=" * 68)
    print("PASS: local in-process SeedVR2 video upscaling works." if ok
          else "FAIL: output invalid or not upscaled.")
    print("=" * 68)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
