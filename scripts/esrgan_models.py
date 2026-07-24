"""
esrgan_models.py
----------------
Catalog + lazy, hash-verified download for the fixed-ratio (non-SeedVR) video engine's
models (docs/local-video-upscaler.md section 11). The engine itself
(fixed_ratio_engine.py) only knows how to run a spandrel model file; THIS module owns
"which models exist, where to get them, and how to prove the download is intact",
mirroring the SeedVR2 lazy-weights pattern and the standing verify-third-party-downloads
rule (every artifact is SHA-256-pinned).

Torch-free: stdlib + net_ssl (the shared certifi HTTPS context, so a fresh Windows VM's
OS root store can't block the GitHub-release download the way it blocked RunPod's API).

Two models ship (both selectable in Settings):

  * realesr-general-x4v3 (COMPACT, SRVGGNetCompact): the DEFAULT. Fast + low-VRAM (~1.5 GB
    at 1080p x4 on a 3090, ~5 fps) -- the "fast, runs on any GPU" tier that justifies this
    engine next to SeedVR2. Best general-purpose speed/size trade.
  * RealESRGAN_x4plus (QUALITY, RRDBNet 23-block): sharper fine detail, but ~16x slower and
    far heavier (~8 GB for a single 480p frame; tiling mandatory at 1080p). The
    "slower-but-prettier" option.

Both are BSD-3-licensed weights from xinntao's Real-ESRGAN releases; the hashes were
measured locally (docs/local-video-upscaler.md section 11 as-built).
"""

import os
import hashlib
import tempfile
import urllib.request

try:
    from net_ssl import ssl_context as _ssl_context
except Exception:                                    # noqa: BLE001
    def _ssl_context():
        return None

APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Browser-ish UA: GitHub release asset redirects to objects storage; the default urllib UA
# is fine here (unlike get.videolan.org), but a UA never hurts and matches updater.py.
_UA = "image-toolbox/esrgan-models"


class ModelSpec:
    """One catalog entry. `kind` groups the GUI (compact | quality); `scale` is the fixed
    upscale ratio the model applies (spandrel reports the same value on load -- kept here so
    the GUI can label a target without loading torch)."""

    def __init__(self, key, filename, url, sha256, scale, kind, label, description):
        self.key = key
        self.filename = filename
        self.url = url
        self.sha256 = sha256.lower()
        self.scale = int(scale)
        self.kind = kind
        self.label = label
        self.description = description


# key -> ModelSpec. Add a model by dropping a verified row here; nothing else changes.
MODELS = {
    "realesr-general-x4v3": ModelSpec(
        key="realesr-general-x4v3",
        filename="realesr-general-x4v3.pth",
        url="https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/realesr-general-x4v3.pth",
        sha256="8dc7edb9ac80ccdc30c3a5dca6616509367f05fbc184ad95b731f05bece96292",
        scale=4, kind="compact",
        label="Compact (fast): realesr-general-x4v3",
        description="Fast, low-VRAM, general-purpose. Best speed/size; runs on a small GPU. "
                    "The recommended default."),
    "RealESRGAN_x4plus": ModelSpec(
        key="RealESRGAN_x4plus",
        filename="RealESRGAN_x4plus.pth",
        url="https://github.com/xinntao/Real-ESRGAN/releases/download/v0.1.0/RealESRGAN_x4plus.pth",
        sha256="4fa0d38905f75ac06eb49a7951b426670021be3018265fd191d2125df9d682f1",
        scale=4, kind="quality",
        label="Quality (slower): RealESRGAN_x4plus",
        description="Sharper fine detail, but much slower and VRAM-heavy (tiling needed at "
                    "higher resolutions). Choose for best quality on a capable GPU."),
    # Native x2 QUALITY weight. Same RRDBNet family as x4plus, but scale 2, so a 2x target
    # (the common old-1080p -> 4K / 720p -> 1440p case) computes the OUTPUT size directly
    # instead of making a 4x frame and throwing 3/4 of it away. Auto-selected by ratio (see
    # resolve_for_ratio); it is NOT a separate Method entry.
    "RealESRGAN_x2plus": ModelSpec(
        key="RealESRGAN_x2plus",
        filename="RealESRGAN_x2plus.pth",
        url="https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.1/RealESRGAN_x2plus.pth",
        sha256="49fafd45f8fd7aa8d31ab2a22d14d91b536c34494a5cfe31eb5d89c2fa266abb",
        scale=2, kind="quality",
        label="Quality x2: RealESRGAN_x2plus",
        description="Native 2x of the Quality family, used automatically for 2x targets."),
}

DEFAULT_MODEL = "realesr-general-x4v3"

# The tiers shown in the UI (one Method entry each): (kind, representative key). The engine
# auto-picks the best-scale concrete weight WITHIN a tier per target (resolve_for_ratio), so
# extra scale variants (e.g. x2plus) are NOT offered as separate methods.
TIERS = [
    ("compact", "realesr-general-x4v3"),
    ("quality", "RealESRGAN_x4plus"),
]


def catalog():
    """The tier representatives (one ModelSpec per tier: compact, then quality) for the
    Method/Settings picklists. Scale variants inside a tier are auto-selected, not listed."""
    return [spec(rep) for _kind, rep in TIERS]


def spec(key):
    """The ModelSpec for `key`, or the default's spec if the key is unknown (so a stale
    config value degrades to a working model instead of failing)."""
    return MODELS.get(key) or MODELS[DEFAULT_MODEL]


def tier_scales(kind):
    """The native upscale factors a tier can produce with NO resize (e.g. quality -> [2, 4],
    compact -> [4]). Used to offer ONLY exact-ratio targets for Real-ESRGAN, so a generated
    frame is never ffmpeg-resized up or down (quality loss the user didn't ask for, #11)."""
    return sorted({m.scale for m in MODELS.values() if m.kind == kind})


def resolve_for_ratio(kind, ratio):
    """The best concrete model key in tier `kind` for a needed upscale `ratio`: the smallest
    native scale that still reaches the ratio (so we upscale enough, then downscale at most a
    little), or the largest available scale when the ratio exceeds every variant. Picks the
    right-sized weight so a 2x target on the Quality tier uses the native x2 model instead of
    computing 4x and downscaling (#11)."""
    variants = sorted((m for m in MODELS.values() if m.kind == kind), key=lambda m: m.scale)
    if not variants:                                 # unknown tier: fall back to the default
        return DEFAULT_MODEL
    r = float(ratio or 0)
    for m in variants:                               # smallest scale >= ratio
        if m.scale + 1e-6 >= r:
            return m.key
    return variants[-1].key                          # ratio bigger than all: largest scale


def models_dir(app_root=None):
    """Where the ESRGAN weights live: <app_root>/models/ESRGAN (beside models/SEEDVR2)."""
    return os.path.join(app_root or APP_ROOT, "models", "ESRGAN")


def model_path(key, app_root=None):
    """The on-disk path a model WOULD live at (whether or not it is present yet)."""
    return os.path.join(models_dir(app_root), spec(key).filename)


def _sha256(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def is_present(key, app_root=None):
    """True iff the model file exists AND its sha256 matches the pinned value. A mismatch
    (partial/corrupt download, or a swapped file) reads as 'not present' so ensure_model
    re-fetches it."""
    p = model_path(key, app_root)
    if not os.path.exists(p):
        return False
    try:
        return _sha256(p) == spec(key).sha256
    except OSError:
        return False


def ensure_model(key, app_root=None, *, log=None, progress=None):
    """Return the local path to model `key`, downloading + verifying it first if missing or
    corrupt. Raises RuntimeError on a failed download or a hash mismatch (never returns an
    unverified file). `progress(done_bytes, total_bytes)` is called during the download
    (total may be 0 if the server omits Content-Length)."""
    sp = spec(key)
    dest = model_path(key, app_root)
    if is_present(key, app_root):
        return dest
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    if log:
        log(f"downloading {sp.filename} ({sp.kind}) …")

    req = urllib.request.Request(sp.url, headers={"User-Agent": _UA})
    fd, tmp = tempfile.mkstemp(prefix=sp.filename + ".", suffix=".part",
                               dir=os.path.dirname(dest))
    try:
        with os.fdopen(fd, "wb") as out, \
                urllib.request.urlopen(req, context=_ssl_context(), timeout=60) as resp:
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            while True:
                block = resp.read(1 << 16)
                if not block:
                    break
                out.write(block)
                done += len(block)
                if progress:
                    try:
                        progress(done, total)
                    except Exception:                # noqa: BLE001
                        pass
        got = _sha256(tmp)
        if got != sp.sha256:
            raise RuntimeError(
                f"{sp.filename}: sha256 mismatch (expected {sp.sha256[:12]}…, got "
                f"{got[:12]}…): refusing the download")
        os.replace(tmp, dest)
        if log:
            log(f"{sp.filename} ready ({os.path.getsize(dest) // (1 << 20)} MB, verified).")
        return dest
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
