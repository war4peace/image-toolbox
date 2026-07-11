"""
benchmark_clip.py
-----------------
Source-clip provider for the per-card VRAM benchmark suite (feature #7,
docs/local-video-upscaler.md sections 16 / 20). The benchmark upscales a SHORT clip at
ascending batches to find a card's ceiling; the CONTENT is irrelevant (the wall is set
by OUTPUT size, docs 14), so any clip of the right dimensions works.

Two sources, in order:
  1. a **pinned** standard clip downloaded from GitHub (config `video.benchmark_clip_url`
     + `benchmark_clip_sha256`), verified by SHA-256 before use (the project's
     download-integrity rule); the base is then SCALED to each cell's source size.
  2. an **ffmpeg-synthesised** clip (`testsrc2`) at the exact size, so the benchmark works
     OFFLINE and out of the box with no asset to publish.

Either way each cell gets a cached `src_<w>x<h>_<frames>.mp4`. Stdlib + the bundled
ffmpeg; no torch. Fail-safe download (a bad hash deletes the file and falls back to synth).
"""

import os
import hashlib
import urllib.request

import video_pipeline as vp

# The standard benchmark clip. Empty by default: no asset is published yet, so the suite
# synthesises its source locally (see ensure_source_clip). Set both in config.json's
# `video` section to use a real pinned clip: benchmark_clip_url + benchmark_clip_sha256
# (fetch it once, sha256 it, embed the digest -- an unpinned download is refused).
DEFAULT_CLIP_URL = ""
DEFAULT_CLIP_SHA256 = ""

DEFAULT_FPS = 30
# A browser-ish UA is unnecessary for raw.githubusercontent/release assets; use the
# stdlib default. Kept explicit so a future host swap is a one-line change.
_UA = "image-toolbox-benchmark"


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def download_base_clip(url, sha256, dest, log=None):
    """Download `url` to `dest` and verify its SHA-256 == `sha256` (required: an unpinned
    download is refused, per the project's integrity rule). Returns dest on success; raises
    on a missing pin, a network error, or a hash mismatch (the partial file is deleted).
    A cached `dest` whose hash already matches is reused without re-downloading."""
    if not url:
        raise ValueError("no benchmark clip URL configured")
    if not sha256:
        raise ValueError("benchmark clip URL has no SHA-256 pin (refusing an unverified download)")
    sha256 = sha256.strip().lower()
    if os.path.exists(dest) and _sha256(dest).lower() == sha256:
        return dest
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    tmp = dest + ".part"
    if log:
        log(f"Downloading benchmark clip: {url}")
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as f:
        while True:
            block = r.read(1024 * 256)
            if not block:
                break
            f.write(block)
    got = _sha256(tmp).lower()
    if got != sha256:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise ValueError(f"benchmark clip hash mismatch (expected {sha256[:12]}…, "
                         f"got {got[:12]}…) -- refusing it")
    os.replace(tmp, dest)
    return dest


def synth_clip(path, w, h, frames, fps=DEFAULT_FPS, log=None):
    """Synthesise a short `frames`-frame `w`x`h` clip with ffmpeg's `testsrc2` (a moving
    test pattern). Deterministic, offline, and the exact size we need. libx264/yuv420p so
    the SeedVR2 reader decodes it like any real mp4. Returns `path`."""
    ffmpeg, _ = vp.find_ffmpeg()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    if log:
        log(f"Generating a {w}x{h} x{frames}-frame benchmark clip (ffmpeg testsrc2).")
    vp._run([ffmpeg, "-hide_banner", "-y",
             "-f", "lavfi", "-i", f"testsrc2=size={w}x{h}:rate={fps}",
             "-frames:v", str(int(frames)), "-pix_fmt", "yuv420p",
             "-c:v", "libx264", "-crf", "18", path])
    return path


def derive_clip(base, path, w, h, frames, fps=DEFAULT_FPS, log=None):
    """Scale a downloaded base clip to `w`x`h` and trim to `frames` frames. Returns `path`."""
    ffmpeg, _ = vp.find_ffmpeg()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    if log:
        log(f"Deriving a {w}x{h} x{frames}-frame benchmark clip from the standard source.")
    vp._run([ffmpeg, "-hide_banner", "-y", "-i", base,
             "-vf", f"scale={w}:{h},fps={fps}",
             "-frames:v", str(int(frames)), "-pix_fmt", "yuv420p",
             "-c:v", "libx264", "-crf", "18", path])
    return path


def ensure_source_clip(work_dir, w, h, frames, *, base_url=None, base_sha256=None,
                       base_cache=None, log=None):
    """Return a path to a cached `w`x`h` x`frames` source clip for one benchmark cell,
    creating it if absent. Prefers deriving from the pinned standard clip (downloaded +
    verified once into `base_cache`); on any download/verify failure falls back to a
    locally SYNTHESISED clip so the benchmark always has a valid source. Idempotent:
    the per-cell file is cached by (w, h, frames)."""
    os.makedirs(work_dir, exist_ok=True)
    out = os.path.join(work_dir, f"src_{int(w)}x{int(h)}_{int(frames)}.mp4")
    if os.path.exists(out):
        return out
    base = None
    if base_url:
        try:
            cache = base_cache or os.path.join(work_dir, "bench_base.mp4")
            base = download_base_clip(base_url, base_sha256, cache, log=log)
        except Exception as exc:                          # noqa: BLE001 (fall back to synth)
            if log:
                log(f"Standard clip unavailable ({exc}); synthesising the source instead.")
            base = None
    if base:
        try:
            return derive_clip(base, out, w, h, frames, log=log)
        except Exception as exc:                          # noqa: BLE001
            if log:
                log(f"Could not derive from the standard clip ({exc}); synthesising instead.")
    return synth_clip(out, w, h, frames, log=log)
