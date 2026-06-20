#!/usr/bin/env python3
"""
upscale_one.py — minimal single-image upscale on the pod, for validating the
remote stack (and the seed of the future pod/worker.py). Reuses the SAME
UpscaleEngine the local app uses (scp'd alongside this file), loading models
from the network volume.

    python upscale_one.py <repo_dir> <model_dir> <settings.json> <in> <out> <resolution>
"""
import os
import sys
import json
import time

repo_dir, model_dir, settings_path, src, dst, resolution = sys.argv[1:7]
sys.path.insert(0, repo_dir)                                   # seedvr2 engine
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # upscale_engine.py

from upscale_engine import UpscaleEngine                       # noqa: E402

with open(settings_path, encoding="utf-8") as f:
    settings = json.load(f)

t0 = time.time()
engine = UpscaleEngine(repo_dir, model_dir, settings)
t1 = time.time()
print(f"[pod] engine loaded in {t1 - t0:.1f}s on {engine.device_name}", flush=True)

engine.upscale(src, dst, int(resolution))
t2 = time.time()

from PIL import Image                                          # noqa: E402
with Image.open(src) as im:
    iw, ih = im.size
with Image.open(dst) as im:
    ow, oh = im.size
print(f"[pod] upscaled {iw}x{ih} -> {ow}x{oh} in {t2 - t1:.1f}s  ({dst})", flush=True)
engine.close()
