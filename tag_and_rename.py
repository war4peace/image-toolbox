"""
tag_and_rename.py
-----------------
Analyses images using a local Ollama vision model, writes a long description
into EXIF ImageDescription, stores the original filename in EXIF XPComment,
writes a processing timestamp into EXIF UserComment (used to skip already-
processed files on re-run), and renames each file to:

    ORIGINAL_STEM_Condensed_Description.ext

Which images are processed:
  - Files inside "upscaled/" subfolders (any resolution)
  - Files outside "upscaled/" whose resolution meets the threshold
    (width >= MIN_WIDTH OR height >= MIN_HEIGHT)

Which images are SKIPPED:
  - Already tagged (EXIF UserComment contains the PROCESSED_MARKER)
  - Resolution below threshold (for non-upscaled originals)

Renaming:
  - Only files whose stem matches a CAMERA_FILENAME_PATTERNS pattern are
    renamed. Files with human-readable names are tagged only.

Undo support:
  - A cache file (trcache/<md5>.cache) records the original filename and
    EXIF state of every scanned file before any modification is made.
  - Undo can be run at any time to revert renames, EXIF changes, or both.

Usage:
    python tag_and_rename.py <directory>                          # normal run
    python tag_and_rename.py <directory> -ftag                   # force-tag all
    python tag_and_rename.py <directory> -frename                # force-rename all
    python tag_and_rename.py <directory> --undo-all              # undo renames + EXIF
    python tag_and_rename.py <directory> --undo-all --names-only # undo renames only
    python tag_and_rename.py <directory> --undo-all --exif-only  # undo EXIF only
    python tag_and_rename.py <directory> --undo <file>           # undo one file

Requirements:
    pip install piexif pillow
    Ollama running locally with the configured model pulled.
"""

import sys
import os
import re
import json
import time
import struct
import base64
import hashlib
import unicodedata
import urllib.request
import urllib.error
import traceback
from collections import defaultdict


# ─────────────────────────────────────────────────────────────
#  CONFIG  –  loaded from config.json
# ─────────────────────────────────────────────────────────────

def _load_config():
    """
    Load settings from config.json in the same directory as this script.
    Raises a clear error if the file is missing or malformed.
    """
    import json as _json
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    if not os.path.exists(config_path):
        print(f"\nERROR: config.json not found at: {config_path}")
        print("Run setup.ps1 first to generate it, or create it manually.")
        print("See README.md for the expected format.\n")
        sys.exit(1)
    with open(config_path, "r", encoding="utf-8-sig") as _f:
        return _json.load(_f)

_CFG = _load_config()
_O   = _CFG.get("ollama",  {})
_T   = _CFG.get("tagging", {})

OLLAMA_URL   = _O.get("url",   "http://127.0.0.1:11434")
OLLAMA_MODEL = _O.get("model", "llava:34b")

MIN_WIDTH       = _T.get("min_width",       3840)
MIN_HEIGHT      = _T.get("min_height",      2160)
UPSCALED_SUBDIR = _T.get("upscaled_subdir", "upscaled")
IMAGE_EXTS      = {".jpg", ".jpeg", ".png", ".webp", ".tiff", ".tif"}

CAMERA_FILENAME_PATTERNS = _T.get("camera_filename_patterns", [
    r"^IMG_\d+", r"^DSC\d+", r"^DSCF\d+", r"^DSCN\d+",
    r"^STA\d+",  r"^HPIM\d+", r"^IMAG\d+", r"^P\d{7}",
    r"^MVI_\d+", r"^MOV_\d+", r"^GOPR\d+", r"^PXL_\d{8}",
    r"^PANO_\d+", r"^VID_\d+", r"^WP_\d+", r"^DCIM\d*",
    r"^\d{8}_\d{6}$", r"^\d+$",
])

CONDENSED_MAX_WORDS = _T.get("condensed_max_words", 5)
OLLAMA_TIMEOUT      = _T.get("ollama_timeout",      120)
OUTAGE_THRESHOLD    = _T.get("outage_threshold",    3)
PROCESSED_MARKER    = "TaggedBy:tag_and_rename"


# ─────────────────────────────────────────────────────────────
#  DEPENDENCY CHECK
# ─────────────────────────────────────────────────────────────

def check_dependencies():
    """
    Verify that piexif and Pillow are importable.
    Prints a clear, actionable error and exits if either is missing.
    Called once at startup, before any prompts or Ollama checks.
    """
    missing = []
    try:
        import piexif   # noqa: F401
    except ImportError:
        missing.append("piexif")
    try:
        from PIL import Image   # noqa: F401
    except ImportError:
        missing.append("Pillow")

    if missing:
        print()
        print("  ERROR: Required package(s) not found: " + ", ".join(missing))
        print()
        print("  Install them with:")
        print(f"    pip install {' '.join(missing)}")
        print()
        print("  If you are using a virtual environment, activate it first.")
        print("  If pip installs to a different Python than the one running")
        print("  this script, use:")
        print(f"    python -m pip install {' '.join(missing)}")
        print()
        sys.exit(1)


# ─────────────────────────────────────────────────────────────
#  CACHE CONSTANTS
# ─────────────────────────────────────────────────────────────

# Cache files live in a "trcache" subfolder next to this script.
# Each source folder gets its own cache file named after the MD5 of the
# normalised absolute path, e.g.  trcache/ab4531c2f8d9.cache
CACHE_DIR            = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trcache")
CACHE_SCHEMA_VERSION = 1

# ─────────────────────────────────────────────────────────────
#  LANGUAGE SUPPORT
# ─────────────────────────────────────────────────────────────

# ISO 639-1 two-letter codes → full English names used in the Ollama prompt.
# Any unrecognised value is passed through as-is, so "--language:Klingon" works too.
_ISO_639_NAMES = {
    "AF": "Afrikaans",  "SQ": "Albanian",   "AR": "Arabic",     "HY": "Armenian",
    "AZ": "Azerbaijani","EU": "Basque",      "BE": "Belarusian", "BN": "Bengali",
    "BS": "Bosnian",    "BG": "Bulgarian",   "CA": "Catalan",    "ZH": "Chinese",
    "HR": "Croatian",   "CS": "Czech",       "DA": "Danish",     "NL": "Dutch",
    "EN": "English",    "ET": "Estonian",    "FI": "Finnish",    "FR": "French",
    "GL": "Galician",   "KA": "Georgian",    "DE": "German",     "EL": "Greek",
    "HE": "Hebrew",     "HI": "Hindi",       "HU": "Hungarian",  "IS": "Icelandic",
    "ID": "Indonesian", "GA": "Irish",       "IT": "Italian",    "JA": "Japanese",
    "KK": "Kazakh",     "KO": "Korean",      "LV": "Latvian",    "LT": "Lithuanian",
    "MK": "Macedonian", "MS": "Malay",       "MT": "Maltese",    "NB": "Norwegian",
    "FA": "Persian",    "PL": "Polish",      "PT": "Portuguese", "RO": "Romanian",
    "RU": "Russian",    "SR": "Serbian",     "SK": "Slovak",     "SL": "Slovenian",
    "ES": "Spanish",    "SW": "Swahili",     "SV": "Swedish",    "TH": "Thai",
    "TR": "Turkish",    "UK": "Ukrainian",   "UR": "Urdu",       "VI": "Vietnamese",
    "CY": "Welsh",
}


def resolve_language(code_or_name):
    """
    Convert a language specifier to a full name for use in the Ollama prompt.
    Accepts ISO 639-1 codes (case-insensitive, e.g. 'RO', 'fr') or full names
    (e.g. 'Romanian', 'french').  Unrecognised values are returned title-cased.
    """
    stripped = code_or_name.strip()
    upper    = stripped.upper()
    if upper in _ISO_639_NAMES:
        return _ISO_639_NAMES[upper]
    # Check if it's already a full name that matches a value (e.g. "romanian")
    lower = stripped.lower()
    for name in _ISO_639_NAMES.values():
        if name.lower() == lower:
            return name
    # Unknown – pass through as-is (title-cased for neatness)
    return stripped.title()


# The three EXIF fields this script writes – all are tracked for undo.
_TRACKED_EXIF_FIELDS = {
    "ImageDescription": ("0th",  270),    # piexif.ImageIFD.ImageDescription
    "XPComment":        ("0th",  40092),  # Windows XP Comment (UTF-16LE)
    "UserComment":      ("Exif", 37510),  # piexif.ExifIFD.UserComment
}


# ─────────────────────────────────────────────────────────────
#  TIMING HELPERS
# ─────────────────────────────────────────────────────────────

def fmt_mmss(seconds):
    seconds = int(seconds)
    m, s = divmod(seconds, 60)
    return f"{m:02d}:{s:02d}"


def fmt_hhmmss(seconds):
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s   = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def fmt_duration(seconds):
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s   = divmod(rem, 60)
    if h:   return f"{h}h {m:02d}m {s:02d}s"
    if m:   return f"{m}m {s:02d}s"
    return f"{s}s"


# ─────────────────────────────────────────────────────────────
#  IMAGE DIMENSION READER  (no Pillow needed)
# ─────────────────────────────────────────────────────────────

JPEG_SOI = bytes([0xFF, 0xD8])
WEBP_SIG = bytes([0x52, 0x49, 0x46, 0x46])   # "RIFF"
WEBP_ID  = bytes([0x57, 0x45, 0x42, 0x50])   # "WEBP"
PNG_SIG  = bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A])
IHDR     = bytes([0x49, 0x48, 0x44, 0x52])   # "IHDR"
NULL1    = bytes([0x00])


def _read_png_dims(f):
    f.read(8)   # PNG signature
    f.read(4)   # chunk length
    assert f.read(4) == IHDR
    w = struct.unpack(">I", f.read(4))[0]
    h = struct.unpack(">I", f.read(4))[0]
    return w, h


def _read_jpeg_dims(f):
    assert f.read(2) == JPEG_SOI
    while True:
        marker = f.read(2)
        if len(marker) < 2 or marker[0] != 0xFF:
            break
        t = marker[1]
        length = struct.unpack(">H", f.read(2))[0]
        if 0xC0 <= t <= 0xCF and t not in (0xC4, 0xC8, 0xCC):
            f.read(1)
            h = struct.unpack(">H", f.read(2))[0]
            w = struct.unpack(">H", f.read(2))[0]
            return w, h
        f.read(length - 2)
    raise ValueError("No JPEG SOF marker found")


def _read_bmp_dims(f):
    f.read(18)
    w = struct.unpack("<I", f.read(4))[0]
    h = abs(struct.unpack("<I", f.read(4))[0])
    return w, h


def _read_webp_dims(f):
    f.read(4)   # RIFF
    f.read(4)   # file size
    assert f.read(4) == WEBP_ID
    chunk = f.read(4)
    f.read(4)   # chunk size
    if chunk == b"VP8 ":
        f.read(3)
        raw = f.read(4)
        w = (struct.unpack("<H", raw[:2])[0] & 0x3FFF) + 1
        h = (struct.unpack("<H", raw[2:])[0] & 0x3FFF) + 1
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
        w = struct.unpack("<I", f.read(3) + NULL1)[0] + 1
        h = struct.unpack("<I", f.read(3) + NULL1)[0] + 1
        return w, h
    raise ValueError("Unknown WebP sub-format")


def get_image_dimensions(path):
    """Return (width, height). Tries fast struct reader first, Pillow as fallback."""
    ext = os.path.splitext(path)[1].lower()
    try:
        with open(path, "rb") as f:
            if ext == ".png":
                return _read_png_dims(f)
            elif ext in (".jpg", ".jpeg"):
                return _read_jpeg_dims(f)
            elif ext == ".bmp":
                return _read_bmp_dims(f)
            elif ext == ".webp":
                return _read_webp_dims(f)
    except Exception:
        pass
    # Fallback: use Pillow (handles all edge cases including progressive JPEGs)
    try:
        from PIL import Image
        with Image.open(path) as img:
            return img.size  # (width, height)
    except Exception:
        pass
    return 0, 0


# ─────────────────────────────────────────────────────────────
#  FILENAME PATTERN CHECK
# ─────────────────────────────────────────────────────────────

_compiled_patterns = [re.compile(p, re.IGNORECASE) for p in CAMERA_FILENAME_PATTERNS]


def has_camera_default_name(filename):
    """
    Return True if the filename stem looks like a camera-generated default.
    Strips known camera-added suffixes before pattern matching:
      _upscaled   – added by upscaling tools, e.g. '6_upscaled.jpg'
      (N)         – added by cameras for same-second duplicates,
                    e.g. '20181018_163120(0).jpg'
    """
    stem = os.path.splitext(filename)[0]
    stem = re.sub(r"_upscaled$", "", stem, flags=re.IGNORECASE)
    stem = re.sub(r"\(\d+\)$", "", stem)          # strip trailing (0), (1), (00) …
    return any(p.match(stem) for p in _compiled_patterns)


# ─────────────────────────────────────────────────────────────
#  OLLAMA  –  connectivity check and image analysis
# ─────────────────────────────────────────────────────────────

def check_ollama():
    """Return (ok: bool, message: str)."""
    try:
        req = urllib.request.Request(f"{OLLAMA_URL}/api/tags")
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
        models = [m["name"] for m in data.get("models", [])]
        base   = OLLAMA_MODEL.split(":")[0]
        found  = any(base in m for m in models)
        if not found:
            return False, (
                f"Ollama is running but model '{OLLAMA_MODEL}' is not pulled.\n"
                f"  Available: {', '.join(models) if models else 'none'}\n"
                f"  Fix: ollama pull {OLLAMA_MODEL}"
            )
        return True, f"Ollama OK — model '{OLLAMA_MODEL}' available."
    except urllib.error.URLError:
        return False, (
            f"Cannot reach Ollama at {OLLAMA_URL}.\n"
            f"  Make sure Ollama is running:  ollama serve"
        )
    except Exception as e:
        return False, f"Ollama check failed: {e}"


def analyse_image(path, language="English"):
    """
    Send the image to Ollama and return (long_description, condensed_title).
    language controls the language of LINE 1 (the EXIF description).
    LINE 2 (the filename title) is always in English so filenames stay ASCII-safe.
    Raises RuntimeError on failure.
    """
    with open(path, "rb") as f:
        img_b64 = base64.b64encode(f.read()).decode("ascii")

    # For non-English, append a language directive to the LINE 1 instruction.
    # LINE 2 is kept in English regardless — filenames must survive ASCII sanitisation.
    if language.lower() == "english":
        lang_note = ""
    else:
        lang_note = f" Write this sentence in {language}."

    prompt = (
        "You are an image analysis assistant. Look at this image carefully "
        "and respond with EXACTLY two lines and nothing else:\n"
        "LINE 1: A single natural-language sentence (20-40 words) describing "
        f"the main subject, setting, and any notable details. Be specific and factual.{lang_note}\n"
        "LINE 2: A condensed 4-5 word title in English suitable for a filename "
        "(Title_Case_With_Underscores, no punctuation, no articles like "
        "a/an/the). Example: Romanian_Street_Night_Scene\n"
        "Do not include labels like 'LINE 1:' or 'LINE 2:' in your response."
    )

    payload = json.dumps({
        "model":   OLLAMA_MODEL,
        "prompt":  prompt,
        "images":  [img_b64],
        "stream":  False,
        "options": {"temperature": 0.2, "num_predict": 120},
    }).encode("utf-8")

    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/generate",
        data    = payload,
        headers = {"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=OLLAMA_TIMEOUT) as resp:
        result = json.loads(resp.read())

    raw   = result.get("response", "").strip()
    lines = [l.strip() for l in raw.splitlines() if l.strip()]

    # Strip common prompt-bleed prefixes the model sometimes outputs verbatim:
    #   "LINE 1: ..."
    #   "LINE 2: ..."
    #   "A single natural-language sentence (20-40 words): ..."
    #   "A condensed 4-5 word title ...: ..."
    def strip_prompt_bleed(text):
        # Remove "LINE N:" style prefixes
        text = re.sub(r"^LINE\s*\d+\s*:\s*", "", text, flags=re.IGNORECASE)
        # Remove the instruction preamble the model sometimes echoes back
        text = re.sub(r"^A single natural[- ]language sentence[^:]*:\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"^A condensed \d[^:]*:\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"^Title:\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"^Description:\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"^Filename:\s*", "", text, flags=re.IGNORECASE)
        return text.strip()

    lines = [strip_prompt_bleed(l) for l in lines]
    # Remove any lines that became empty after stripping
    lines = [l for l in lines if l]

    if len(lines) >= 2:
        long_desc = lines[0]
        condensed = lines[1]
    elif len(lines) == 1:
        long_desc = lines[0]
        condensed = _auto_condense(lines[0])
    else:
        raise RuntimeError("Ollama returned an empty response")

    condensed = _sanitize_condensed(condensed)
    return long_desc, condensed


def _auto_condense(text):
    words = re.findall(r"[A-Za-z0-9]+", text)
    return "_".join(w.capitalize() for w in words[:CONDENSED_MAX_WORDS])


def _sanitize_condensed(text):
    text  = unicodedata.normalize("NFKD", text)
    text  = text.encode("ascii", "ignore").decode()
    text  = re.sub(r"[\s\-]+", "_", text)
    text  = re.sub(r'[<>:"/\\|?*]', "", text)
    text  = re.sub(r"_+", "_", text)
    text  = text.strip("_")
    parts = [p for p in text.split("_") if p]
    if len(parts) > CONDENSED_MAX_WORDS:
        parts = parts[:CONDENSED_MAX_WORDS]
    return "_".join(parts) if parts else "Unknown_Image"


# ─────────────────────────────────────────────────────────────
#  EXIF WRITING
# ─────────────────────────────────────────────────────────────

# ASCII charset header for EXIF UserComment field (8 bytes: "ASCII" + 3 nulls)
_EXIF_ASCII_HEADER = b"ASCII" + bytes(3)


def _load_exif_safe(path):
    """Load EXIF from path, returning a blank dict if missing or unreadable."""
    try:
        import piexif
        return piexif.load(path)
    except Exception:
        return {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}}


def _save_with_exif(path, exif_dict):
    """
    Save EXIF back to a JPEG using Pillow's Image.save(), which works on any
    JPEG including bare files with no existing APP0/APP1 markers (e.g. ComfyUI
    SaveImage output). This re-encodes the JPEG at quality=95 to preserve
    near-lossless quality while writing the EXIF block.
    """
    import piexif
    from PIL import Image

    exif_bytes = piexif.dump(exif_dict)
    img = Image.open(path)
    img.save(path, "jpeg", exif=exif_bytes, quality=95, subsampling=0)


def write_exif(path, long_description, original_filename):
    """
    Write long_description to ImageDescription (0th IFD tag 270).
    Write original_filename to XPComment (0th IFD tag 40092, UTF-16LE).
    """
    import piexif

    exif_dict = _load_exif_safe(path)

    # ImageDescription: ASCII bytes
    exif_dict["0th"][piexif.ImageIFD.ImageDescription] = (
        long_description.encode("ascii", "replace")
    )

    # XPComment (tag 40092): UTF-16LE bytes — Windows standard for XP* fields
    exif_dict["0th"][40092] = original_filename.encode("utf-16-le")

    _save_with_exif(path, exif_dict)


def write_processed_marker(path):
    """
    Write a processing timestamp into EXIF Exif.UserComment.
    Format: "TaggedBy:tag_and_rename @ 2026-03-24 23:15:42"
    """
    import piexif

    timestamp    = time.strftime("%Y-%m-%d %H:%M:%S")
    marker_text  = f"{PROCESSED_MARKER} @ {timestamp}"
    user_comment = _EXIF_ASCII_HEADER + marker_text.encode("ascii")

    exif_dict = _load_exif_safe(path)
    exif_dict.setdefault("Exif", {})[piexif.ExifIFD.UserComment] = user_comment

    _save_with_exif(path, exif_dict)


def is_already_processed(path):
    """
    Return True if the file has already been tagged by this script.
    Checks EXIF Exif.UserComment for the PROCESSED_MARKER prefix.
    """
    try:
        import piexif
        exif_dict = _load_exif_safe(path)
        raw = exif_dict.get("Exif", {}).get(piexif.ExifIFD.UserComment, b"")
        if not raw:
            return False
        # Skip the 8-byte charset header, decode remaining ASCII
        text = raw[8:].decode("ascii", "ignore").strip()
        return text.startswith(PROCESSED_MARKER)
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────
#  RENAME LOGIC
# ─────────────────────────────────────────────────────────────

def build_new_path(original_path, condensed):
    """
    Build: ORIGINAL_STEM_Condensed.ext
    Appends _2, _3 etc. on collision.
    """
    dir_  = os.path.dirname(original_path)
    stem  = os.path.splitext(os.path.basename(original_path))[0]
    ext   = os.path.splitext(original_path)[1]
    new   = os.path.join(dir_, f"{stem}_{condensed}{ext}")
    if not os.path.exists(new):
        return new
    counter = 2
    while True:
        candidate = os.path.join(dir_, f"{stem}_{condensed}_{counter}{ext}")
        if not os.path.exists(candidate):
            return candidate
        counter += 1


# ─────────────────────────────────────────────────────────────
#  CACHE MANAGEMENT
# ─────────────────────────────────────────────────────────────

def get_cache_path(source_root):
    """
    Return the .cache file path for source_root.
    Uses the first 12 hex chars of the MD5 of the normalised absolute path,
    e.g.  trcache/ab4531c2f8d9.cache
    """
    norm     = os.path.normcase(os.path.abspath(source_root))
    hash_str = hashlib.md5(norm.encode("utf-8")).hexdigest()[:12]
    os.makedirs(CACHE_DIR, exist_ok=True)
    return os.path.join(CACHE_DIR, f"{hash_str}.cache")


def load_cache(source_root):
    """
    Load the cache for source_root from disk, or create a fresh empty cache.
    The original-state snapshots are never overwritten on subsequent loads.
    """
    cache_path = get_cache_path(source_root)
    if os.path.exists(cache_path):
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("schema_version") == CACHE_SCHEMA_VERSION:
                return data
            print("  WARNING: Cache schema mismatch — starting a fresh cache.")
        except Exception as exc:
            print(f"  WARNING: Could not read cache ({exc}) — starting a fresh cache.")
    return {
        "schema_version": CACHE_SCHEMA_VERSION,
        "source_root":    os.path.abspath(source_root),
        "created_at":     time.strftime("%Y-%m-%dT%H:%M:%S"),
        "last_updated":   time.strftime("%Y-%m-%dT%H:%M:%S"),
        "files":          {},
    }


def save_cache(cache, source_root):
    """Persist the cache dict to disk."""
    cache["last_updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    with open(get_cache_path(source_root), "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2, ensure_ascii=False)


def _snapshot_exif(path):
    """
    Snapshot the three tracked EXIF fields from a file.
    Returns a dict  { field_name: base64(raw_bytes) | None }.
    None means the field was absent in the file.
    Raw bytes are base64-encoded so they survive JSON round-trips safely.
    """
    snap = {name: None for name in _TRACKED_EXIF_FIELDS}
    try:
        exif = _load_exif_safe(path)
        for name, (ifd, tag) in _TRACKED_EXIF_FIELDS.items():
            raw = exif.get(ifd, {}).get(tag)
            if raw is not None:
                snap[name] = base64.b64encode(raw).decode("ascii")
    except Exception:
        pass
    return snap


def _find_entry(cache, source_root, abs_path):
    """
    Locate a cache entry for the given absolute path.
    Searches by original_rel_path first (cache key), then by current_rel_path.
    Returns (cache_key, entry_dict) or (None, None).
    """
    rel = os.path.relpath(abs_path, source_root)
    # Direct key match (original path, or file was never renamed)
    if rel in cache["files"]:
        return rel, cache["files"][rel]
    # Search by current path (file was renamed in a previous run)
    rel_norm = os.path.normcase(rel)
    for key, entry in cache["files"].items():
        if os.path.normcase(entry.get("current_rel_path", "")) == rel_norm:
            return key, entry
    return None, None


def ensure_cache_entry(cache, source_root, abs_path):
    """
    Ensure abs_path has a cache entry with an original-state snapshot.
    If the entry already exists (from a prior run), it is left untouched so
    the original snapshot is never overwritten.
    Returns the cache key (always the original_rel_path).
    """
    key, entry = _find_entry(cache, source_root, abs_path)
    if entry is not None:
        return key  # original snapshot already preserved

    rel  = os.path.relpath(abs_path, source_root)
    snap = _snapshot_exif(abs_path)
    cache["files"][rel] = {
        "original_rel_path": rel,
        "current_rel_path":  rel,
        "original_exif":     snap,
        "current_exif":      snap.copy(),
        "was_renamed":       False,
        "first_seen_at":     time.strftime("%Y-%m-%dT%H:%M:%S"),
        "last_processed_at": None,
        "status":            "scanned",
    }
    return rel


def update_cache_entry(cache, source_root, orig_abs_path, new_abs_path, status):
    """
    Update a cache entry after a file has been processed.
    orig_abs_path  = path of the file before renaming (may equal new_abs_path)
    new_abs_path   = final path after all EXIF writes and optional rename
    status         = "processed" | "failed" | "skipped"
    """
    key, entry = _find_entry(cache, source_root, orig_abs_path)
    if entry is None:
        return  # safety guard – should not happen

    new_rel = os.path.relpath(new_abs_path, source_root)
    entry["current_rel_path"]  = new_rel
    entry["was_renamed"]       = (
        os.path.normcase(entry["original_rel_path"]) != os.path.normcase(new_rel)
    )
    entry["current_exif"]      = _snapshot_exif(new_abs_path)
    entry["last_processed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    # Only advance status if it's a "more final" state
    if entry.get("status") not in ("undone",):
        entry["status"] = status


# ─────────────────────────────────────────────────────────────
#  UNDO
# ─────────────────────────────────────────────────────────────

def _restore_exif_fields(path, original_snap):
    """
    Restore the three tracked EXIF fields to their original state.
    Fields that were absent originally (None in snapshot) are deleted.
    Fields that existed are written back from the stored raw bytes.
    Only saves the file if at least one field actually changed.
    Returns True on success, False on error.

    Note: uses _save_with_exif which re-encodes as JPEG at quality=95,
    consistent with how the fields were written in the first place.
    """
    try:
        exif    = _load_exif_safe(path)
        changed = False
        for name, (ifd, tag) in _TRACKED_EXIF_FIELDS.items():
            orig_b64 = original_snap.get(name)
            ifd_dict = exif.setdefault(ifd, {})
            if orig_b64 is None:
                # Field should not exist — remove it if present
                if tag in ifd_dict:
                    del ifd_dict[tag]
                    changed = True
            else:
                raw = base64.b64decode(orig_b64)
                if ifd_dict.get(tag) != raw:
                    ifd_dict[tag] = raw
                    changed = True
        if changed:
            _save_with_exif(path, exif)
        return True
    except Exception as exc:
        print(f"           EXIF restore error: {exc}")
        return False


def _undo_entry(entry, source_root, undo_names, undo_exif):
    """
    Perform the undo operation for a single cache entry.
    undo_names – if True, rename the file back to its original name
    undo_exif  – if True, restore the three tracked EXIF fields

    Returns (success: bool, summary_message: str).
    The entry dict is mutated in-place on success to reflect the new state.
    """
    curr_abs = os.path.join(source_root, entry["current_rel_path"])
    orig_abs = os.path.join(source_root, entry["original_rel_path"])

    if not os.path.exists(curr_abs):
        return False, f"file not found: {curr_abs}"

    notes     = []
    exif_ok   = True
    rename_ok = True

    # ── Step 1: restore EXIF fields (while file is at its current path) ──
    if undo_exif:
        orig_snap = entry.get("original_exif") or {}
        if all(v is None for v in orig_snap.values()):
            notes.append("EXIF: nothing to restore (original had no tracked fields)")
        else:
            exif_ok = _restore_exif_fields(curr_abs, orig_snap)
            if exif_ok:
                entry["current_exif"] = orig_snap.copy()
                notes.append("EXIF restored")
            else:
                notes.append("EXIF restore FAILED")

    # ── Step 2: rename back to original filename ──────────────────────
    if undo_names:
        if not entry.get("was_renamed"):
            notes.append("rename: nothing to undo (file was not renamed by this script)")
        elif os.path.exists(orig_abs) and os.path.normcase(orig_abs) != os.path.normcase(curr_abs):
            rename_ok = False
            notes.append(f"rename skipped — target already exists: {os.path.basename(orig_abs)}")
        else:
            try:
                os.makedirs(os.path.dirname(orig_abs), exist_ok=True)
                os.rename(curr_abs, orig_abs)
                entry["current_rel_path"] = entry["original_rel_path"]
                entry["was_renamed"]      = False
                notes.append(f"renamed back to {os.path.basename(orig_abs)}")
            except Exception as exc:
                rename_ok = False
                notes.append(f"rename FAILED: {exc}")

    success = exif_ok and rename_ok
    if success:
        entry["status"] = "undone"

    return success, ", ".join(notes) if notes else "nothing to undo"


def run_undo(root, target, undo_names, undo_exif):
    """
    Main undo dispatcher.

    root        – absolute path to the source folder
    target      – "all", or a file specifier (absolute path, path relative to
                  root, or just the filename — matched against both original and
                  current names in the cache)
    undo_names  – whether to revert renames
    undo_exif   – whether to revert EXIF fields
    """
    cache = load_cache(root)
    if not cache["files"]:
        print("  No cache found for this folder. Run the script normally first.")
        return

    what = []
    if undo_names: what.append("file renames")
    if undo_exif:  what.append("EXIF fields")
    print(f"  Undoing: {', '.join(what)}")
    print(f"  Cache:   {get_cache_path(root)}")
    print()

    # ── Collect entries to undo ───────────────────────────────
    entries_to_undo = []

    if target == "all":
        # All entries that have something to undo (not already "undone" or "scanned")
        for entry in cache["files"].values():
            entries_to_undo.append(entry)
    else:
        # Resolve the target specifier against cache entries.
        # We try:  (a) exact match on original_rel_path or current_rel_path,
        #          (b) filename-only match (basename) on either.
        target_abs  = os.path.abspath(target)
        target_rel  = os.path.normcase(os.path.relpath(target_abs, root))
        target_base = os.path.normcase(os.path.basename(target))

        for entry in cache["files"].values():
            orig_nc = os.path.normcase(entry.get("original_rel_path", ""))
            curr_nc = os.path.normcase(entry.get("current_rel_path", ""))
            if (
                orig_nc == target_rel
                or curr_nc == target_rel
                or os.path.normcase(os.path.basename(entry.get("original_rel_path", ""))) == target_base
                or os.path.normcase(os.path.basename(entry.get("current_rel_path",  ""))) == target_base
            ):
                entries_to_undo.append(entry)

        if not entries_to_undo:
            print(f"  No cache entry found matching: {target}")
            print(f"  Tip: use --undo-all to undo everything, or check the cache file.")
            return

    # ── Process ──────────────────────────────────────────────
    ok_count   = 0
    fail_count = 0

    for entry in entries_to_undo:
        display = entry.get("current_rel_path") or entry.get("original_rel_path", "?")
        print(f"  {display}")
        success, msg = _undo_entry(entry, root, undo_names, undo_exif)
        print(f"           -> {msg}")
        if success:
            ok_count += 1
        else:
            fail_count += 1

    save_cache(cache, root)

    print()
    print(f"  Undo complete — {ok_count} OK, {fail_count} failed.")
    if fail_count:
        print("  (Cache has been updated for successful undos.)")


# ─────────────────────────────────────────────────────────────
#  DIRECTORY SCANNER
# ─────────────────────────────────────────────────────────────

def collect_work_items(root, force_tag=False):
    """
    Walk root recursively and return a list of qualifying image paths.

    Inside an "upscaled/" subfolder:  all images qualify.
    Outside an "upscaled/" subfolder: only images meeting the resolution
                                      threshold qualify, unless force_tag=True.
    force_tag=True: all image files qualify regardless of resolution.
    """
    items = []
    for dirpath, dirnames, filenames in os.walk(root):
        is_upscaled_dir = (
            os.path.basename(dirpath).lower() == UPSCALED_SUBDIR.lower()
        )
        for filename in sorted(filenames):
            ext = os.path.splitext(filename)[1].lower()
            if ext not in IMAGE_EXTS:
                continue
            full_path = os.path.join(dirpath, filename)
            if force_tag or is_upscaled_dir:
                items.append(full_path)
            else:
                w, h = get_image_dimensions(full_path)
                if w >= MIN_WIDTH or h >= MIN_HEIGHT:
                    items.append(full_path)
    return items


# ─────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print()
        print("  tag_and_rename.py — AI-powered image tagger and renamer")
        print()
        print("  Analyses images using a local Ollama vision model, writes a long")
        print("  description into EXIF ImageDescription, stores the original filename")
        print("  in EXIF XPComment, and renames each file to:")
        print()
        print("      ORIGINAL_STEM_Condensed_Description.ext")
        print()
        print("  Which images are processed:")
        print("    * Files inside 'upscaled/' subfolders (any resolution)")
        print("    * Files outside 'upscaled/' with width >= MIN_WIDTH OR height >= MIN_HEIGHT")
        print()
        print("  Which images are SKIPPED:")
        print("    * Already tagged (EXIF UserComment contains the processing marker)")
        print("    * Resolution below threshold (for non-upscaled originals)")
        print()
        print("  Renaming:")
        print("    * Only files matching a camera default pattern (IMG_, DSC_, etc.)")
        print("      are renamed. Others are tagged but keep their existing name.")
        print()
        print("  EXIF fields written:")
        print("    * ImageDescription  — long natural-language description (20-40 words)")
        print("    * XPComment         — original filename before rename (UTF-16LE)")
        print("    * UserComment       — processing timestamp (used for skip-on-rerun)")
        print()
        print("  Collision handling:")
        print("    IMG_3548_Black_And_White_Kitten_2.jpg  (counter suffix)")
        print()
        print(f"  Outage handling:")
        print(f"    After {OUTAGE_THRESHOLD} consecutive failures the script pauses")
        print("    and waits for Enter before retrying.")
        print()
        print("  Undo support:")
        print("    Every run saves a cache of original filenames and EXIF data to:")
        print(f"    {CACHE_DIR}\\<folderhash>.cache")
        print("    This lets you reverse renames, EXIF changes, or both at any time.")
        print()
        print("  Configuration (edit config.json in the same directory as this script):")
        print(f"    OLLAMA_URL                {OLLAMA_URL}")
        print(f"    OLLAMA_MODEL              {OLLAMA_MODEL}")
        print(f"    MIN_WIDTH / MIN_HEIGHT    {MIN_WIDTH} / {MIN_HEIGHT} px")
        print(f"    CONDENSED_MAX_WORDS       {CONDENSED_MAX_WORDS}")
        print(f"    OUTAGE_THRESHOLD          {OUTAGE_THRESHOLD}")
        print(f"    CAMERA_FILENAME_PATTERNS  {len(CAMERA_FILENAME_PATTERNS)} patterns configured")
        print()
        print("  Requirements:")
        print("    pip install piexif pillow")
        print(f"    ollama pull {OLLAMA_MODEL}")
        print()
        print("  Usage:")
        print("    python tag_and_rename.py <directory> [-ftag] [-frename] [--language:XX]")
        print("    python tag_and_rename.py <directory> --undo-all [--names-only | --exif-only]")
        print("    python tag_and_rename.py <directory> --undo <file> [--names-only | --exif-only]")
        print()
        print("  Processing flags (can be combined):")
        print("    -ftag              Tag all images regardless of resolution or prior tagging")
        print("    -frename           Rename all images regardless of filename pattern")
        print("    --language:XX      Language for EXIF descriptions. XX can be an ISO 639-1")
        print("                       code (e.g. RO, FR, DE) or a full name (e.g. Romanian).")
        print("                       Default: English. Filenames are always in English.")
        print()
        print("  Undo flags:")
        print("    --undo-all         Undo all processed files in the folder")
        print("    --undo <file>      Undo a single file (by current or original name / path)")
        print("    --names-only       Undo renames only (skip EXIF restore)")
        print("    --exif-only        Undo EXIF changes only (skip rename restore)")
        print()
        print("  Examples:")
        print(r"    python tag_and_rename.py X:\Photos                          # normal mode")
        print(r"    python tag_and_rename.py X:\Photos -ftag                    # tag everything")
        print(r"    python tag_and_rename.py X:\Photos -frename                 # rename everything")
        print(r"    python tag_and_rename.py X:\Photos --language:RO            # descriptions in Romanian")
        print(r"    python tag_and_rename.py X:\Photos --language:FR -ftag      # French, force-tag all")
        print(r"    python tag_and_rename.py X:\Photos --undo-all               # undo everything")
        print(r"    python tag_and_rename.py X:\Photos --undo-all --names-only  # undo renames only")
        print(r"    python tag_and_rename.py X:\Photos --undo IMG_3548_Sunset.jpg")
        print()
        sys.exit(0)

    # ── Parse flags ──────────────────────────────────────────
    args = sys.argv[1:]

    force_tag    = "-ftag"        in args
    force_rename = "-frename"     in args
    undo_all     = "--undo-all"   in args
    names_only   = "--names-only" in args
    exif_only    = "--exif-only"  in args

    # --undo <file>  (single-file undo, distinct from --undo-all)
    undo_target = None
    if "--undo" in args:
        idx = args.index("--undo")
        if idx + 1 < len(args) and not args[idx + 1].startswith("-"):
            undo_target = args[idx + 1]
            args = args[:idx] + args[idx + 2:]
        else:
            print("ERROR: --undo requires a file path argument.")
            print("       Use --undo-all to undo the entire folder.")
            sys.exit(1)

    # --language:XX  (e.g. --language:RO, --language:French)
    language = "English"
    lang_args = [a for a in args if a.lower().startswith("--language:")]
    if lang_args:
        raw_lang = lang_args[-1].split(":", 1)[1]   # take the last one if repeated
        language = resolve_language(raw_lang)
        if not language:
            print(f"ERROR: --language value is empty.")
            sys.exit(1)

    # Strip all recognised flags so only the directory remains
    args = [a for a in args if a not in (
        "-ftag", "-frename", "--undo-all", "--names-only", "--exif-only"
    ) and not a.lower().startswith("--language:")]

    if not args:
        print("ERROR: No directory specified.")
        sys.exit(1)

    root = os.path.abspath(args[0])
    if not os.path.isdir(root):
        print(f"ERROR: '{root}' is not a valid directory.")
        sys.exit(1)

    # ── Dependency check (before any prompts or Ollama calls) ───
    check_dependencies()

    # Undo scope: default is both names + EXIF; flags narrow it down
    undo_names = not exif_only    # True unless --exif-only
    undo_exif  = not names_only   # True unless --names-only

    # ── Undo mode ────────────────────────────────────────────
    if undo_all or undo_target is not None:
        target = "all" if undo_all else undo_target
        run_undo(root, target, undo_names, undo_exif)
        sys.exit(0)

    if force_tag:
        print("  [!] Force tag mode: all images will be tagged regardless of resolution or prior tagging.")
    if force_rename:
        print("  [!] Force rename mode: all images will be renamed regardless of filename pattern.")
    if language.lower() != "english":
        print(f"  [!] Language: EXIF descriptions will be written in {language}."
              f" Filenames remain in English.")

    # ── Pre-flight ───────────────────────────────────────────
    print()
    print("  +-----------------------------------------------------+")
    print("  |  PREPARATION                                         |")
    print("  |                                                       |")
    print("  |  Make sure Ollama is running before continuing.      |")
    print("  |  If it is not, open a new terminal and run:          |")
    print("  |                                                       |")
    print("  |      ollama serve                                     |")
    print("  |                                                       |")
    print("  |  Also ensure no other VRAM-heavy workload is active.  |")
    print("  +-----------------------------------------------------+")
    print()
    input("  Press Enter when ready to continue ...")
    print()

    print("  Checking Ollama ...")
    ok, msg = check_ollama()
    if not ok:
        print(f"\n  ERROR: {msg}\n")
        sys.exit(1)
    print(f"  {msg}\n")

    # ── Scan ─────────────────────────────────────────────────
    print(f"  Scanning '{root}' ...\n")
    work_items = collect_work_items(root, force_tag=force_tag)

    if not work_items:
        print("  No qualifying images found.")
        sys.exit(0)

    total = len(work_items)
    print(f"  Found {total} qualifying image(s).\n")

    # ── Cache: snapshot original state of every scanned file ─
    # This is done BEFORE any processing so that even a mid-run crash leaves
    # the original filenames and EXIF values safely recorded.
    print("  Building undo cache ...")
    cache = load_cache(root)
    new_entries = 0
    for item_path in work_items:
        key = ensure_cache_entry(cache, root, item_path)
        if cache["files"][key]["status"] == "scanned":
            new_entries += 1
    save_cache(cache, root)
    cache_path_display = get_cache_path(root)
    print(f"  Cache ready — {new_entries} new entr{'y' if new_entries == 1 else 'ies'} "
          f"({len(cache['files'])} total).")
    print(f"  Cache file: {cache_path_display}\n")

    # ── Stats ────────────────────────────────────────────────
    folder_stats = defaultdict(lambda: {
        "processed": 0, "skipped": 0, "failed": 0, "elapsed": 0.0
    })

    total_processed   = 0
    total_skipped     = 0
    total_failed      = 0
    consecutive_fails = 0
    grand_start       = time.time()
    current_folder    = None
    folder_start      = None

    for idx, path in enumerate(work_items, 1):
        dirpath  = os.path.dirname(path)
        filename = os.path.basename(path)
        prefix   = f"[{idx}/{total}]"

        # ── Folder banner ────────────────────────────────────
        if dirpath != current_folder:
            if current_folder is not None:
                elapsed = time.time() - folder_start
                folder_stats[current_folder]["elapsed"] += elapsed
                print(f"\n  Folder done in {fmt_duration(elapsed)}\n")
                print("-" * 64)
            current_folder = dirpath
            folder_start   = time.time()
            rel_folder     = os.path.relpath(dirpath, root) if dirpath != root else "."
            print(f"\n[DIR]  {rel_folder}\n")

        # ── Already processed? ───────────────────────────────
        if not force_tag and is_already_processed(path):
            print(f"  {prefix} SKIP (already tagged)  {path}")
            # Mark as skipped in cache only if it hasn't been fully processed before
            _key, _entry = _find_entry(cache, root, path)
            if _entry and _entry.get("status") == "scanned":
                _entry["status"] = "skipped"
                save_cache(cache, root)
            folder_stats[dirpath]["skipped"] += 1
            total_skipped += 1
            continue

        w, h    = get_image_dimensions(path)
        dim_str = f"{w}x{h}px" if w else "?x?px"
        print(f"  {prefix} {dim_str}  {path}")

        img_start = time.time()

        try:
            # 1. Analyse
            long_desc, condensed = analyse_image(path, language=language)

            # 2. Write EXIF description + original filename
            write_exif(path, long_desc, filename)

            # 3. Rename if camera default name
            will_rename = force_rename or has_camera_default_name(filename)
            if will_rename:
                new_path    = build_new_path(path, condensed)
                os.rename(path, new_path)
                result_name = os.path.basename(new_path)
                print(f"           -> {result_name}  (renamed)")
            else:
                new_path    = path
                result_name = filename
                print(f"           -> {result_name}  (tagged only, name kept)")

            # 4. Write processing marker (uses final path after rename)
            write_processed_marker(new_path)

            # 5. Update cache with final state
            update_cache_entry(cache, root, path, new_path, "processed")
            save_cache(cache, root)

            img_elapsed   = time.time() - img_start
            grand_elapsed = time.time() - grand_start
            print(f"           -> \"{long_desc}\"")
            print(f"           Done in {fmt_mmss(img_elapsed)} | "
                  f"Total elapsed: {fmt_hhmmss(grand_elapsed)}\n")

            consecutive_fails = 0
            folder_stats[dirpath]["processed"] += 1
            total_processed += 1

        except Exception as e:
            img_elapsed   = time.time() - img_start
            grand_elapsed = time.time() - grand_start
            consecutive_fails += 1
            print(f"           FAILED in {fmt_mmss(img_elapsed)} | "
                  f"Total elapsed: {fmt_hhmmss(grand_elapsed)}")
            print(f"           Error: {type(e).__name__}: {e}")
            traceback.print_exc()
            print()

            update_cache_entry(cache, root, path, path, "failed")
            save_cache(cache, root)

            folder_stats[dirpath]["failed"] += 1
            total_failed += 1

            # ── Outage detection ─────────────────────────────
            if consecutive_fails >= OUTAGE_THRESHOLD:
                print(f"  WARNING: {consecutive_fails} consecutive failures.")
                print("  Restart Ollama if needed, then press Enter to resume.")
                input("  Press Enter to resume ...")
                ok, msg = check_ollama()
                if not ok:
                    print(f"  ERROR: {msg}")
                    print("  Exiting. Restart Ollama and run the script again.")
                    break
                print(f"  {msg}\n")
                consecutive_fails = 0

            continue

    # ── Close last folder ────────────────────────────────────
    if current_folder is not None:
        elapsed = time.time() - folder_start
        folder_stats[current_folder]["elapsed"] += elapsed
        print(f"\n  Folder done in {fmt_duration(elapsed)}\n")

    # ── Summary table ─────────────────────────────────────────
    grand_elapsed = time.time() - grand_start

    col_path = min(60, max(
        len("Folder"),
        max((len(os.path.relpath(p, root)) for p in folder_stats), default=6)
    ))
    col_proc = len("Processed")
    col_skip = len("Skipped")
    col_fail = len("Failed")
    col_time = max(
        len("Elapsed"),
        max((len(fmt_duration(v["elapsed"])) for v in folder_stats.values()), default=7)
    )

    def trunc(s, n):
        return s if len(s) <= n else "..." + s[-(n - 3):]

    sep = "=" * (col_path + col_proc + col_skip + col_fail + col_time + 16)
    row = f"  {{:<{col_path}}}  {{:>{col_proc}}}  {{:>{col_skip}}}  {{:>{col_fail}}}  {{:>{col_time}}}"

    print("\n" + sep)
    print(row.format("Folder", "Processed", "Skipped", "Failed", "Elapsed"))
    print("-" * (col_path + col_proc + col_skip + col_fail + col_time + 16))

    for dp, stats in folder_stats.items():
        rel = os.path.relpath(dp, root) if dp != root else "."
        print(row.format(
            trunc(rel, col_path),
            stats["processed"], stats["skipped"],
            stats["failed"], fmt_duration(stats["elapsed"])
        ))

    print("=" * (col_path + col_proc + col_skip + col_fail + col_time + 16))
    print(row.format(
        "TOTAL", total_processed, total_skipped,
        total_failed, fmt_hhmmss(grand_elapsed)
    ))
    print(sep)
    print(f"\n  ({total_failed} failed, {total_skipped} already tagged)\n")
    print(f"  Undo cache: {cache_path_display}\n")


if __name__ == "__main__":
    main()
