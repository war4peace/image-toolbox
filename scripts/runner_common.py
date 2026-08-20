"""
runner_common.py
----------------
Shared infrastructure for the headless runners (batch_upscale, tag_and_rename,
conciliate, batch_video_upscale) and the provisioning driver. The runners each
grew private copies of the same helpers; the cost was drift (the clearest case:
the UTF-8 stdout hardening lived only in the video runner, leaving the others
crashable on a non-ASCII glyph in an odd console). 0.3.8 proved the pattern by
unifying notifications into notifications.py; this does the same for the rest of
the runner scaffolding.

What lives here (the genuinely shared, behaviour-identical pieces):
  * APP_ROOT + load_config()          - config.json at the app root
  * harden_stdout()                   - make stdout/stderr non-ASCII-proof
  * GUI_MARKER / stdin_is_piped() / GUI_MODE / gui_event()  - the @@TBX@@ protocol
  * fmt_duration / fmt_mmss / fmt_hhmmss                     - duration formatting
  * get_image_dimensions() + header parsers                 - Pillow-free size read
  * image_variant_reason()            - "the upscaler cannot round-trip this
                                        image" detector (future-features #17)
  * is_oom_error()                    - CUDA/torch OOM classifier (watchdog)
  * remote_pod_stopped(session)       - "did the pod's dead-man's switch fire?"
  * DerivedPruner                     - keep our own output/archive folders out
                                        of every input walk (future-features #16)

What deliberately stays per-runner (divergent by design, NOT pure moves):
  * The session loggers (batch_upscale.Logger vs tag_and_rename._TeeOutput vs
    conciliate.Logger) - different tee strategies and user-visible log-file
    names (log_/tag_/conc_).
  * The stdin control loops (PauseController vs RemoteControl) - different
    command sets and pause semantics.
  * The send_notification wrappers - one line each, differing only by username.

Stdlib only; heavy imports (runpod_client, Pillow) stay lazy so this module
loads torch-free and instantly.
"""

import os
import sys
import struct

try:                                        # optional, never fatal
    from debug_log import debug_log
except Exception:                           # noqa: BLE001 (older install)
    def debug_log(*_a, **_k):
        pass

# App root = parent of scripts/. config.json, seedvr2/, models/, logs/ and the
# .venv all live at the app root, not beside these modules. Anchored off this
# file so it is correct regardless of the current working directory.
APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_config():
    """
    Load settings from config.json at the app root (the parent of scripts/),
    merged with the untracked config.local.json secrets overlay (config_store).
    Prints a clear error and exits if the base file is missing or malformed - the
    same behaviour every runner had in its private _load_config().
    """
    import config_store
    config_path = os.path.join(APP_ROOT, "config.json")
    if not os.path.exists(config_path):
        print(f"\nERROR: config.json not found at: {config_path}")
        print("Run setup.ps1 first to generate it, or create it manually.")
        print("See README.md for the expected format.\n")
        sys.exit(1)
    cfg = config_store.load(APP_ROOT)
    if cfg is None:                 # present but malformed
        print(f"\nERROR: could not parse config.json at: {config_path}\n")
        sys.exit(1)
    _apply_runpod_api_version(cfg)
    return cfg


def _apply_runpod_api_version(cfg):
    """Point runpod_client at the configured REST version (`runpod.api_version`,
    default v2; REST v1 returns 410 Gone on 2026-11-15).

    Hooked HERE, in the one loader every runner subprocess already calls, rather
    than at each call site: a caller that forgot would talk to the wrong
    transport, and that failure is a renamed response field silently reading None
    (see the normalisation seam in runpod_client). Guarded and best-effort, so a
    runner with nothing to do with RunPod is never blocked by it.
    """
    try:
        import runpod_client
        runpod_client.configure(cfg)
    except Exception as exc:                             # noqa: BLE001 (fail-safe)
        debug_log("could not apply runpod.api_version", exc)


def harden_stdout():
    """
    Make stdout/stderr robust to non-ASCII output (unicode filenames, the ⏸/▶/⏹
    control glyphs, section marks) regardless of the console code page. A
    headless runner must never die on a UnicodeEncodeError - the Windows console
    defaults to cp1252. Fail-safe: a stream that can't be reconfigured is left
    as-is. Call once, as early as possible, before any output.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


# ─────────────────────────────────────────────
#  GUI EVENT PROTOCOL  (the @@TBX@@ line channel)
# ─────────────────────────────────────────────

# Marker prefix the GUI intercepts and strips (never shown to the user there).
GUI_MARKER = "@@TBX@@"


def stdin_is_piped():
    """True when stdin is a pipe - i.e. we are driven by toolbox_gui.py."""
    try:
        return sys.stdin is not None and not sys.stdin.isatty()
    except Exception:
        return False


# GUI mode: control arrives as stdin lines instead of keypresses, and
# machine-readable event lines keep the GUI's preview strip in sync. Computed
# once at import, exactly as each runner did (stdin state does not change).
GUI_MODE = stdin_is_piped()


def gui_event(kind, payload):
    """
    Emit one event line for the GUI (no-op outside GUI mode):

        <GUI_MARKER><KIND>|<payload>\\n

    Written as ONE atomic write (not print's two) so a background-thread event -
    e.g. the remote telemetry sampler - can't be byte-split by the main thread's
    concurrent writes; the GUI parser strips it even mid-line. Targets the RAW
    stream when stdout has been wrapped by a logging tee (tag_and_rename), so a
    marker never leaks into the on-disk session log; a plain stdout has no .raw
    attribute and is written directly.
    """
    if GUI_MODE:
        out = getattr(sys.stdout, "raw", sys.stdout)
        out.write(f"{GUI_MARKER}{kind}|{payload}\n")
        out.flush()


# ─────────────────────────────────────────────
#  DURATION FORMATTING
# ─────────────────────────────────────────────

def fmt_duration(seconds):
    """Format a duration in seconds as  Xh Ym Zs  or  Ym Zs  or  Zs."""
    seconds = int(seconds)
    h, rem  = divmod(seconds, 3600)
    m, s    = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m {s:02d}s"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


def fmt_mmss(seconds):
    """Format a duration as mm:ss - used for per-image elapsed time."""
    seconds = int(seconds)
    m, s    = divmod(seconds, 60)
    return f"{m:02d}:{s:02d}"


def fmt_hhmmss(seconds):
    """Format a duration as hh:mm:ss - used for total elapsed time."""
    seconds = int(seconds)
    h, rem  = divmod(seconds, 3600)
    m, s    = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


# ─────────────────────────────────────────────
#  IMAGE DIMENSION READER  (no Pillow needed for the common formats)
# ─────────────────────────────────────────────
# Reads width/height straight from the file header, avoiding a full decode
# during eligibility scanning. Supersedes the two drifted copies (batch_upscale
# had TIFF but no Pillow fallback; tag_and_rename had the fallback but no TIFF) -
# this handles png/jpeg/bmp/webp/tiff via headers AND falls back to Pillow for
# anything the fast path can't parse (progressive JPEGs, odd variants). Any
# failure yields (0, 0), exactly as both originals did.
#
# Consolidating also fixed a latent bug both copies shared: the lossy-WebP (VP8)
# branch skipped the wrong number of header bytes and applied a spurious +1, so a
# .webp reported wrong (non-zero) dimensions - and because it returned without
# raising, tag_and_rename's Pillow fallback never got a chance to correct it. Now
# tested against real Pillow-written WebP files.

# RAW extensions live in raw_decode, which owns them (#19). Imported at module
# level on purpose: raw_decode's own top-level imports are os + io, so this stays
# torch-free, Pillow-free and instant; rawpy and Pillow are lazy inside it.
try:
    from raw_decode import RAW_EXTS
except Exception:                       # noqa: BLE001 (old install without the module)
    RAW_EXTS = ()


def _read_png_dimensions(f):
    f.read(8)                       # PNG signature
    f.read(4)                       # first chunk length
    assert f.read(4) == b"IHDR"
    w = struct.unpack(">I", f.read(4))[0]
    h = struct.unpack(">I", f.read(4))[0]
    return w, h


def _read_jpeg_dimensions(f):
    assert f.read(2) == b"\xff\xd8"
    while True:
        marker = f.read(2)
        if len(marker) < 2 or marker[0] != 0xFF:
            break
        marker_type = marker[1]
        length = struct.unpack(">H", f.read(2))[0]
        if 0xC0 <= marker_type <= 0xCF and marker_type not in (0xC4, 0xC8, 0xCC):
            f.read(1)
            h = struct.unpack(">H", f.read(2))[0]
            w = struct.unpack(">H", f.read(2))[0]
            return w, h
        else:
            f.read(length - 2)
    raise ValueError("Could not find JPEG SOF marker")


def _read_bmp_dimensions(f):
    f.read(18)
    w = struct.unpack("<I", f.read(4))[0]
    h = struct.unpack("<I", f.read(4))[0]
    return w, abs(h)


def _read_webp_dimensions(f):
    f.read(4)                       # "RIFF"
    f.read(4)                       # file size
    assert f.read(4) == b"WEBP"
    chunk = f.read(4)
    f.read(4)                       # chunk size
    if chunk == b"VP8 ":
        f.read(3)               # frame tag
        f.read(3)               # keyframe start code (0x9d 0x01 0x2a)
        raw = f.read(4)         # 14-bit width then 14-bit height (stored as-is)
        w = struct.unpack("<H", raw[:2])[0] & 0x3FFF
        h = struct.unpack("<H", raw[2:])[0] & 0x3FFF
        return w, h
    elif chunk == b"VP8L":
        f.read(1)
        b = f.read(4)
        bits = struct.unpack("<I", b)[0]
        w = (bits & 0x3FFF) + 1
        h = ((bits >> 14) & 0x3FFF) + 1
        return w, h
    elif chunk == b"VP8X":
        f.read(4)
        w = struct.unpack("<I", f.read(3) + b"\x00")[0] + 1
        h = struct.unpack("<I", f.read(3) + b"\x00")[0] + 1
        return w, h
    raise ValueError("Unknown WebP sub-format")


def _read_tiff_dimensions(f):
    """Read width/height from a TIFF header (little and big endian)."""
    header = f.read(4)
    if header[:2] == b"II":
        endian = "<"
    elif header[:2] == b"MM":
        endian = ">"
    else:
        raise ValueError("Not a TIFF file")
    ifd_offset = struct.unpack(endian + "I", f.read(4))[0]
    f.seek(ifd_offset)
    num_entries = struct.unpack(endian + "H", f.read(2))[0]
    w = h = 0
    for _ in range(num_entries):
        tag, typ, count, val_raw = struct.unpack(endian + "HHI4s", f.read(12))
        if typ in (3, 4):           # SHORT or LONG
            fmt = endian + ("H" if typ == 3 else "I")
            val = struct.unpack(fmt, val_raw[:struct.calcsize(fmt)])[0]
            if tag == 256:   w = val    # ImageWidth
            elif tag == 257: h = val    # ImageLength
        if w and h:
            break
    return w, h


def get_image_dimensions(path):
    """Return (width, height), or (0, 0) if it can't be determined. Tries the
    fast header reader first, then falls back to Pillow (handles progressive
    JPEGs and other edge cases). Never raises.

    RAW is answered by LibRaw and NEVER reaches the Pillow fallback (#19). This
    inverts the module's usual instinct, and it has to: a DNG/CR2/NEF/ARW is a
    TIFF/EP derivative that puts a small preview in IFD 0 by spec, and Pillow
    sniffs content rather than extension - so the fallback does not fail, it
    answers confidently and WRONGLY. Measured on 24 real camera files
    (docs/raw-preview-survey.csv): 15 report a wrong size that way, and several
    of those are plausible photo sizes rather than obvious thumbnails (a 20D CR2
    reads as 1536x1024, a 5D as 2496x1664). That would upscale a preview into a
    4K file, in bulk, without ever looking wrong in a log. An unreadable RAW
    returns (0, 0) and is reported unreadable, exactly like a corrupt JPEG.
    """
    ext = os.path.splitext(path)[1].lower()
    if ext in RAW_EXTS:
        try:
            import raw_decode
            return raw_decode.raw_dimensions(path)
        except Exception:                   # noqa: BLE001 (module absent: still no Pillow)
            return (0, 0)
    try:
        with open(path, "rb") as f:
            if ext == ".png":
                return _read_png_dimensions(f)
            elif ext in (".jpg", ".jpeg"):
                return _read_jpeg_dimensions(f)
            elif ext == ".bmp":
                return _read_bmp_dimensions(f)
            elif ext == ".webp":
                return _read_webp_dimensions(f)
            elif ext in (".tif", ".tiff"):
                return _read_tiff_dimensions(f)
    except Exception:
        pass
    # Fallback: Pillow parses formats/variants the fast path can't (e.g.
    # progressive JPEGs). Lazy import keeps this module torch/Pillow-free to load.
    try:
        from PIL import Image
        with Image.open(path) as img:
            return img.size        # (width, height)
    except Exception:
        pass
    return (0, 0)


# ─────────────────────────────────────────────
#  IMAGE VARIANTS THE PIPELINE CANNOT ROUND-TRIP  (future-features #17)
# ─────────────────────────────────────────────
# The upscale engine is RGB-only end to end: `_load_image` does
# `img.convert("RGB")` and `_save_image` writes `arr[..., :3]` as mode="RGB",
# frame 0 only. Anything outside three 8-bit channels of page zero is therefore
# silently discarded, and the output keeps the SAME name and extension, so
# Conciliation's mirrored-name fallback matches it with full confidence and
# reports an ordinary "replaced": the original is archived (or deleted) and the
# transparency / extra pages / 16-bit depth are gone with it.
#
# The decision (#17) is to SKIP these, not to fix them: supporting alpha,
# multi-page and high-bit-depth output properly is a pile of format design
# questions this project has not needed to answer, and guessing at them is how
# quiet data loss happens. Skipping is conservative, reversible, and copies a
# behaviour the app already has (non-media files are counted, listed and left
# alone). The other half of that decision - actually supporting the formats -
# lives in dropped-ideas.md with its revisit trigger.
#
# Detection is header-only (Pillow's lazy `Image.open`, no decode) and is not
# even attempted for the extensions that cannot carry any of the three traits,
# so a tree of JPEGs - the normal case - pays nothing for this check.
#
# CMYK is deliberately NOT treated as a variant: a colour-space conversion is a
# different argument from losing a channel/page/bit depth, and testing for it
# would force a Pillow open on every JPEG.

# Extensions that can carry alpha, several pages, or >8-bit samples. JPEG cannot
# in any form this app will meet, and it is the bulk of a photo tree, so the
# normal case pays nothing. BMP is in the list for the rare 32-bit file with a
# real alpha mask; the common BGRX kind reads back as plain RGB, so it does not
# produce a false positive.
VARIANT_CANDIDATE_EXTS = (".png", ".webp", ".tif", ".tiff", ".bmp")

# Every variant reason starts with this, so a cached skip_reason can be
# classified later without a second look at the file (the eligibility cache
# stores the reason string, not a category).
VARIANT_PREFIX = "would lose "

# Pillow modes that carry more than 8 bits per sample. Used as the fallback when
# the format-specific read below can't answer.
_MODE_BITS = {"I": 32, "F": 32, "I;16": 16, "I;16B": 16, "I;16L": 16,
              "I;16N": 16, "I;32": 32, "I;32B": 32, "I;32L": 32}

_ALPHA_MODES = ("RGBA", "LA", "PA", "RGBa", "La")


def is_variant_reason(reason):
    """True when `reason` (an eligibility-cache skip_reason) is a #17 variant
    skip rather than an ordinary too-large skip."""
    return bool(reason) and str(reason).startswith(VARIANT_PREFIX)


def _sample_bits(img, path, ext):
    """Bits per colour sample, or 8 when it can't be determined.

    Pillow's `mode` is not enough on its own: a 16-bit-per-channel PNG or TIFF
    is presented as plain "RGB" (Pillow truncates it on load, which is exactly
    the loss we are looking for), so the format's own header is read instead.
    """
    if ext in (".tif", ".tiff"):
        try:
            bps = img.tag_v2.get(258)          # BitsPerSample
            if bps:
                vals = bps if isinstance(bps, (tuple, list)) else (bps,)
                return max(int(v) for v in vals)
        except Exception:
            pass
    elif ext == ".png":
        try:
            with open(path, "rb") as f:
                head = f.read(26)
            if head[:8] == b"\x89PNG\r\n\x1a\n" and head[12:16] == b"IHDR":
                return int(head[24])           # IHDR bit depth
        except Exception:
            pass
    return _MODE_BITS.get(getattr(img, "mode", ""), 8)


def image_variant_reason(path):
    """
    Return a short plain-English reason when upscaling `path` would silently
    discard part of it, else None.

    Reasons are user-facing and always start with VARIANT_PREFIX, e.g.
    "would lose transparency", "would lose 3 of 4 pages",
    "would lose transparency, 16-bit depth".

    Never raises. An unreadable file returns None on purpose: the runners
    already have a "corrupted / unreadable" path that reports it properly, and
    this check must not steal that classification.
    """
    ext = os.path.splitext(path)[1].lower()
    if ext not in VARIANT_CANDIDATE_EXTS:
        return None
    try:
        from PIL import Image
    except Exception:
        return None                            # no Pillow: nothing to claim

    losses = []
    try:
        with Image.open(path) as img:
            if img.mode in _ALPHA_MODES or "transparency" in (img.info or {}):
                losses.append("transparency")

            try:
                pages = int(getattr(img, "n_frames", 1) or 1)
            except Exception:
                pages = 1
            if pages > 1:
                unit = "frames" if ext == ".webp" else "pages"
                losses.append(f"{pages - 1} of {pages} {unit}")

            bits = _sample_bits(img, path, ext)
            if bits > 8:
                losses.append(f"{bits}-bit depth")
    except Exception:
        return None

    return VARIANT_PREFIX + ", ".join(losses) if losses else None


# ─────────────────────────────────────────────
#  DERIVED DIRECTORIES  (future-features #16)
# ─────────────────────────────────────────────
# The app writes its outputs INSIDE the tree it scans (the Batch Upscaler's
# default output is <source>/__upscaled__, Conciliation archives originals into
# <source>/__Archive__). That nesting is deliberate and correct (a shared
# sibling root would collide the moment a second source tree is processed), but
# it means every scanner can walk into another tool's output and treat it as
# fresh input. The archive case was the expensive one: it holds the only copies
# still BELOW the resolution target, so the next upscale scan finds them all
# eligible and re-upscales files the user already conciliated (billed GPU time on
# a rented pod).
#
# The fix is a NAME rule, not a database lookup: the app names its derived
# folders with a consistent `__name__` convention, so one shared list checked per
# directory is stateless, free, and retroactive (it works on archives created
# before this shipped, on a second install, and after `db/cache.db` is deleted,
# none of which a DB record would survive). Rejected alternatives are recorded in
# docs/future-features.md #16.
#
# The rule is "prune matching SUBDIRECTORIES", never "refuse the root": a user
# who deliberately points a tool at an `__upscaled__` folder as their chosen root
# still gets it scanned. Pruning an os.walk `dirnames` list gives that for free:
# the root is yielded before any pruning can apply to it.

# Case-insensitive (Windows). Add new output folder names here, in one place.
DERIVED_DIRNAMES = (
    "__upscaled__",         # Batch Upscaler + Video Upscaler default output
    "__Archive__",          # Conciliation archive mode (conciliate.ARCHIVE_DIRNAME)
    ".imgtbx_video",        # Video Upscaler work area (batch_video_upscale.WORK_DIRNAME)
)


def derived_dirnames(cfg=None):
    """
    The lowercased set of directory names that are OURS and must never be
    re-scanned as input. `cfg` (a loaded config.json) adds the configured video
    output subfolder when the user changed it from the default; passing None
    loads it best-effort. Never raises.
    """
    names = {n.lower() for n in DERIVED_DIRNAMES}
    if cfg is None:
        try:
            import config_store
            cfg = config_store.load(APP_ROOT)
        except Exception:               # noqa: BLE001 (fail-safe: the fixed list still applies)
            cfg = None
    try:
        sub = (cfg or {}).get("video", {}).get("output_subdir", "")
        if isinstance(sub, str) and sub.strip():
            names.add(sub.strip().lower())
    except Exception:                   # noqa: BLE001
        pass
    return names


class DerivedPruner:
    """
    Prunes the app's own derived directories out of an os.walk, and remembers
    what it skipped so the scan can print ONE summary line afterwards (a user
    wondering why their archived files aren't picked up gets a hint; a per-folder
    line would drown a big tree).

    Usage:
        pruner = DerivedPruner(cfg)
        for dirpath, dirnames, filenames in os.walk(root):
            pruner.prune(dirnames)
            ...
        line = pruner.summary()         # None when nothing was pruned
    """

    def __init__(self, cfg=None, extra=None):
        self.names = derived_dirnames(cfg)
        if extra:
            self.names |= {e.strip().lower() for e in extra if e and e.strip()}
        self.count = 0                  # how many directories were pruned
        self.seen  = set()              # the distinct names actually pruned

    def prune(self, dirnames):
        """Remove our derived folders from an os.walk `dirnames` list, in place
        (os.walk only honours in-place edits). Returns the pruned names."""
        removed = []
        keep    = []
        for d in dirnames:
            if d.lower() in self.names:
                removed.append(d)
                self.count += 1
                self.seen.add(d)
            else:
                keep.append(d)
        if removed:
            dirnames[:] = keep
        return removed

    def summary(self):
        """One line for the scan log, or None when nothing was pruned."""
        if not self.count:
            return None
        names = ", ".join(sorted(self.seen, key=str.lower))
        return (f"Skipped {self.count} folder(s) this app created "
                f"(not re-scanned as input): {names}")


# ─────────────────────────────────────────────
#  ERROR CLASSIFICATION
# ─────────────────────────────────────────────

def is_oom_error(exc):
    """
    True when an exception looks like a CUDA/torch out-of-memory error. With
    "Prefer No Sysmem Fallback" set in the NVIDIA panel, a degraded GPU fails
    fast with a hard OOM instead of crawling - the watchdog treats that as the
    same degradation episode as a sustained slow streak.
    """
    s = f"{type(exc).__name__}: {exc}".lower()
    return ("out of memory" in s
            or "outofmemory" in s
            or "cuda_error_out_of_memory" in s
            or ("cuda error" in s and "memory" in s))


# ─────────────────────────────────────────────
#  REMOTE POD STATE  (roadmap #1)
# ─────────────────────────────────────────────

def remote_pod_stopped(session):
    """
    True if the remote pod is no longer RUNNING - its dead-man's switch fired
    (idle / max-runtime) or it was stopped/terminated. Called only AFTER a remote
    request has failed, so a pod that is gone/EXITED/TERMINATED (or unreadable - a
    terminated pod 404s) means: end the run gracefully (the resume/skip logic
    continues next time) instead of treating the dropped connection as a
    recoverable local outage. A still-RUNNING pod returns False, so a genuine
    transient blip still uses the normal outage path.
    """
    if session is None:
        return False
    try:
        import runpod_client as rp
        status = rp.pod_status(session.api_key, session.pod_id)
    except Exception:                # noqa: BLE001 (fail-safe)
        return False
    return status in (None, rp.STATUS_EXITED, rp.STATUS_TERMINATED)
