"""
benchmark_clip.py
-----------------
Source-clip provider for the per-card VRAM benchmark suite (feature #7,
docs/local-video-upscaler.md sections 16 / 20 / 22). The benchmark upscales a SHORT clip at
ascending batches to find a card's ceiling. The CONTENT is irrelevant to the wall (set by
OUTPUT size, docs 14), but every user must benchmark against the SAME footage for the numbers
to be comparable machine-to-machine. So the suite pulls a small, fixed set of Creative-Commons
source videos on demand (Big Buck Bunny + two Wikimedia clips for the 4:3 / 3:4 aspects that
16:9 Big Buck Bunny can't provide), each SHA-256 pinned, and feeds every benchmark cell its
NATIVE, aspect-matched source with NO rescale: the engine then upscales that native clip to the
cell's target, exactly as a real run would (so the input->output ratio is real, not a synthetic
"half the output" placeholder).

Downloads are lazy (only the sources a run actually touches), cached under samples/videos/,
integrity-checked (an unpinned or mismatched download is refused), and the 1080p source is
delivered as a .zip whose extracted mp4 is the thing we verify. Stdlib + the bundled ffmpeg;
no torch. Fail-safe: if a source can't be fetched the cell falls back to an ffmpeg-synthesised
clip at the native size, with a LOUD warning (those numbers won't be comparable to a real run).
"""

import os
import hashlib
import zipfile
import urllib.parse
import urllib.request

import video_pipeline as vp

try:                                          # 0.5.0 shared HTTPS trust (certifi on a fresh VM)
    from net_ssl import ssl_context
except Exception:                             # pragma: no cover - older tree without net_ssl
    def ssl_context():
        return None


# App root = parent of scripts/. The downloaded standard sources cache here; the user may also
# drop them in by hand and a hash-matching file is reused (never re-downloaded).
APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_SOURCE_CACHE = os.path.join(APP_ROOT, "samples", "videos")

# Wikimedia's policy wants a descriptive User-Agent (contact + URL) or it serves a 403; harmless
# for the Blender host too, so it is used for every fetch.
_UA = ("image-toolbox-benchmark/0.5 "
       "(+https://github.com/war4peace/image-toolbox; war4peace@gmail.com)")

DEFAULT_FPS = 30

# The standard benchmark sources: one per aspect/size the cell ladder needs, each verified by
# the SHA-256 of the FILE WE USE (for the zip source, the EXTRACTED mp4, so the member's internal
# name is irrelevant). Big Buck Bunny is (c) Blender Foundation, CC-BY 3.0; the two .ogv clips are
# Creative-Commons/PD from Wikimedia Commons. `w`/`h` are the native dimensions the engine sees
# (evened for yuv420p on cut: the 640x359 source is padded to 640x360). `zip_member` marks a
# source delivered inside a .zip.
SOURCES = {
    "p3x4": {                                 # 3:4 portrait 240x320 -> the 540x720 / 810x1080 cells
        "url": "https://upload.wikimedia.org/wikipedia/commons/8/83/USMC_reelistment_cermony.ogv",
        "sha256": "6739a5ba0f04dbe2a7f6ac8e202e5e7e7ffa85964fd952b48bd282d4624f9960",
        "w": 240, "h": 320},
    "p4x3": {                                 # 4:3 landscape 320x240 -> the four 4:3 cells
        "url": "https://upload.wikimedia.org/wikipedia/commons/b/be/01stoomedewageningen.ogv",
        "sha256": "653a97f2693f7898ef963c82cafe87f65ab9273242c258d640d6a19d16bcb560",
        "w": 320, "h": 240},
    "w360": {                                 # 16:9 640x360 -> the 1080p cell (source is 640x359)
        "url": "https://download.blender.org/peach/bigbuckbunny_movies/BigBuckBunny_640x360.m4v",
        "sha256": "738e2f999860553d056dd79c952f58f63cbb73892a57c72342ce9e5330d9d2d7",
        "w": 640, "h": 360},
    "w720": {                                 # 16:9 1280x720 -> the 1440p cell
        "url": "https://download.blender.org/peach/bigbuckbunny_movies/big_buck_bunny_720p_h264.mov",
        "sha256": "45c8bafeb9a53df7f491198d2e71529701bcf1cd51805782089fac1d32869f9b",
        "w": 1280, "h": 720},
    "w1080": {                                # 16:9 1920x1080 -> the 4K cell (delivered as a .zip)
        "url": "https://download.blender.org/demo/movies/BBB/bbb_sunflower_1080p_30fps_normal.mp4.zip",
        "sha256": "ae51005850b0ff757fe60c3dd7a12d754d3cd2397d87d939b55235e457f97658",
        "w": 1920, "h": 1080,
        "zip_member": "bbb_sunflower_1080p_30fps_normal.mp4"},
}


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _fetch(url, dest, log=None):
    """Stream `url` to `dest` (returns the .part temp path; the caller verifies then renames).
    Descriptive UA + certifi trust (a fresh Windows VM's OS store often can't verify these public
    CAs, and Python's OpenSSL, unlike SChannel, won't paper over it)."""
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    tmp = dest + ".part"
    if log:
        log(f"Downloading benchmark source: {url}")
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=120, context=ssl_context()) as r, open(tmp, "wb") as f:
        while True:
            block = r.read(1024 * 256)
            if not block:
                break
            f.write(block)
    return tmp


def download_base_clip(url, sha256, dest, log=None):
    """Download `url` to `dest` and verify SHA-256 == `sha256` (an unpinned download is refused,
    per the project's integrity rule). A cached `dest` already matching is reused. Returns dest;
    raises on a missing pin, a network error, or a hash mismatch (the partial file is deleted)."""
    if not url:
        raise ValueError("no benchmark clip URL configured")
    if not sha256:
        raise ValueError("benchmark clip URL has no SHA-256 pin (refusing an unverified download)")
    sha256 = sha256.strip().lower()
    if os.path.exists(dest) and _sha256(dest).lower() == sha256:
        return dest
    tmp = _fetch(url, dest, log=log)
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


def download_zip_member(url, sha256, dest, log=None):
    """Download a .zip from `url`, extract the .mp4 member whose SHA-256 matches `sha256`, and
    place it at `dest`. The pin verifies the CONTENT we use, so the member's internal name/path is
    irrelevant (any .mp4 in the archive whose bytes match is accepted). A cached `dest` already
    matching is reused. Raises (refuses) if the pin is missing or no member matches -- the same
    integrity guarantee as a direct download."""
    if not sha256:
        raise ValueError("zip source has no SHA-256 pin (refusing an unverified download)")
    sha256 = sha256.strip().lower()
    if os.path.exists(dest) and _sha256(dest).lower() == sha256:
        return dest
    ztmp = _fetch(url, dest + ".zip", log=log)
    try:
        with zipfile.ZipFile(ztmp) as z:
            for m in (n for n in z.namelist() if n.lower().endswith(".mp4")):
                part = dest + ".part"
                with z.open(m) as src, open(part, "wb") as f:
                    while True:
                        block = src.read(1024 * 256)
                        if not block:
                            break
                        f.write(block)
                if _sha256(part).lower() == sha256:
                    os.replace(part, dest)
                    return dest
                try:
                    os.remove(part)
                except OSError:
                    pass
    finally:
        try:
            os.remove(ztmp)
        except OSError:
            pass
    raise ValueError(f"no .mp4 in {os.path.basename(url)} matched the pin "
                     f"{sha256[:12]}… -- refusing it")


def ensure_source(source_key, cache_dir=None, log=None):
    """Return a path to the pinned NATIVE source video for `source_key`, downloading + verifying
    it once into `cache_dir` (default samples/videos/). A .zip source is extracted and the
    extracted mp4 verified. Idempotent (a hash-matching cached file is reused)."""
    s = SOURCES[source_key]
    cache_dir = cache_dir or DEFAULT_SOURCE_CACHE
    os.makedirs(cache_dir, exist_ok=True)
    if s.get("zip_member"):
        dest = os.path.join(cache_dir, s["zip_member"])
        return download_zip_member(s["url"], s["sha256"], dest, log=log)
    name = os.path.basename(urllib.parse.urlparse(s["url"]).path)
    dest = os.path.join(cache_dir, name)
    return download_base_clip(s["url"], s["sha256"], dest, log=log)


def synth_clip(path, w, h, frames, fps=DEFAULT_FPS, log=None):
    """Synthesise a short `frames`-frame `w`x`h` clip with ffmpeg's `testsrc2` (a moving test
    pattern). Deterministic and offline; the FALLBACK when a standard source can't be fetched, so
    the benchmark still runs (its numbers just won't be comparable to another machine's). Returns
    `path`."""
    ffmpeg, _ = vp.find_ffmpeg()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    if log:
        log(f"Generating a {w}x{h} x{frames}-frame benchmark clip (ffmpeg testsrc2).")
    vp._run([ffmpeg, "-hide_banner", "-y",
             "-f", "lavfi", "-i", f"testsrc2=size={w}x{h}:rate={fps}",
             "-frames:v", str(int(frames)), "-pix_fmt", "yuv420p",
             "-c:v", "libx264", "-crf", "18", path])
    return path


def cut_source_clip(src, path, frames, log=None):
    """Cut the first `frames` frames of native source `src` to `path`, LOOPING the source if it is
    shorter than `frames` (content is irrelevant to the VRAM ceiling, docs 14; a loop seam costs
    nothing here, and it lets a short standard clip serve any batch). Native resolution is kept,
    only evened to satisfy yuv420p (the 640x359 source pads to 640x360). Audio is dropped.
    libx264/yuv420p so the SeedVR2 reader decodes it like any real mp4. Returns `path`."""
    ffmpeg, _ = vp.find_ffmpeg()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    if log:
        log(f"Cutting a {int(frames)}-frame benchmark clip from {os.path.basename(src)} (looped).")
    vp._run([ffmpeg, "-hide_banner", "-y",
             "-stream_loop", "-1", "-i", src,
             "-frames:v", str(int(frames)),
             "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2",
             "-an", "-pix_fmt", "yuv420p", "-c:v", "libx264", "-crf", "18", path])
    return path


def ensure_source_clip(work_dir, source, frames, *, cache_dir=None, log=None):
    """Return a cached `frames`-frame benchmark clip for cell source `source`, creating it if
    absent. Downloads + verifies the native standard source once (shared cache), then cuts the
    first `frames` frames (looped) at native resolution; the engine upscales it to the cell's
    target. Idempotent (cached by source + frames). Fail-safe: if the standard source can't be
    fetched it falls back to an ffmpeg-synthesised clip at the native size with a LOUD warning
    (those numbers won't be comparable to a real run)."""
    os.makedirs(work_dir, exist_ok=True)
    out = os.path.join(work_dir, f"src_{source}_{int(frames)}.mp4")
    if os.path.exists(out):
        return out
    s = SOURCES[source]
    try:
        native = ensure_source(source, cache_dir=cache_dir, log=log)
    except Exception as exc:                  # noqa: BLE001 (never fail a benchmark on a fetch)
        if log:
            log(f"  WARNING: standard source '{source}' unavailable ({exc}); synthesising a "
                f"{s['w']}x{s['h']} clip instead -- these numbers won't be comparable to a real run.")
        return synth_clip(out, s["w"], s["h"], frames, log=log)
    return cut_source_clip(native, out, frames, log=log)
