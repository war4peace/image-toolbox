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

Usage:
    python tag_and_rename.py <directory>

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
import unicodedata
import urllib.request
import urllib.error
import traceback
from collections import defaultdict


# ─────────────────────────────────────────────────────────────
#  CONFIG  –  adjust to match your setup
# ─────────────────────────────────────────────────────────────

OLLAMA_URL   = "http://127.0.0.1:11434"
OLLAMA_MODEL = "llava:34b"

# Resolution threshold for non-upscaled originals.
# Process if width >= MIN_WIDTH OR height >= MIN_HEIGHT.
MIN_WIDTH  = 3840
MIN_HEIGHT = 2160

# Name of the upscaled subfolder (must match the upscaling script).
UPSCALED_SUBDIR = "upscaled"

# Image extensions to consider.
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".tiff", ".tif"}

# Camera default filename patterns (case-insensitive regex).
# A file is eligible for RENAMING only if its stem matches one of these.
# Files not matching are still TAGGED but keep their existing name.
# Add more patterns as you discover them in your archive.
CAMERA_FILENAME_PATTERNS = [
    r"^IMG_\d+",           # IMG_3548       — Canon, Apple, many others
    r"^DSC\d+",            # DSC00123       — Sony
    r"^DSCF\d+",           # DSCF0045       — Fujifilm
    r"^DSCN\d+",           # DSCN1234       — Nikon Coolpix
    r"^STA\d+",            # STA0003        — Samsung (older)
    r"^HPIM\d+",           # HPIM0042       — HP cameras
    r"^IMAG\d+",           # IMAG0099       — HTC / early Android
    r"^P\d{7}",            # P1000001       — Panasonic
    r"^MVI_\d+",           # MVI_1234       — Canon video stills
    r"^MOV_\d+",           # MOV_0001       — various
    r"^GOPR\d+",           # GOPR0001       — GoPro
    r"^PXL_\d{8}",         # PXL_20210915   — Google Pixel
    r"^PANO_\d+",          # PANO_0001      — panorama stills
    r"^VID_\d+",           # VID_20200101   — Android video stills
    r"^WP_\d+",            # WP_20140510    — Windows Phone
    r"^DCIM\d*",           # DCIM generic
    r"^\d{8}_\d{6}$",      # 20210915_143022 — generic timestamp names
    r"^\d+$",              # 6, 7, 42       — bare numeric names
]

# Maximum words in the condensed filename description.
CONDENSED_MAX_WORDS = 5

# Ollama request timeout in seconds.
OLLAMA_TIMEOUT = 120

# How many consecutive failures trigger an outage pause.
OUTAGE_THRESHOLD = 3

# Marker written to EXIF UserComment after successful processing.
# Files with this marker in UserComment are skipped on re-run.
PROCESSED_MARKER = "TaggedBy:tag_and_rename"


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
    Strips _upscaled suffix before matching so e.g. '6_upscaled.jpg'
    is treated the same as '6.jpg'.
    """
    stem = os.path.splitext(filename)[0]
    stem = re.sub(r"_upscaled$", "", stem, flags=re.IGNORECASE)
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


def analyse_image(path):
    """
    Send the image to Ollama and return (long_description, condensed_title).
    Raises RuntimeError on failure.
    """
    with open(path, "rb") as f:
        img_b64 = base64.b64encode(f.read()).decode("ascii")

    prompt = (
        "You are an image analysis assistant. Look at this image carefully "
        "and respond with EXACTLY two lines and nothing else:\n"
        "LINE 1: A single natural-language sentence (20-40 words) describing "
        "the main subject, setting, and any notable details. Be specific "
        "and factual.\n"
        "LINE 2: A condensed 4-5 word title suitable for a filename "
        "(Title_Case_With_Underscores, no punctuation, no articles like "
        "a/an/the). Example: Romanian_Street_Night_Scene\n"
        "Do not include labels like 'LINE 1:' or 'LINE 2:' in your response."
    )

    payload = json.dumps({
        "model":   OLLAMA_MODEL,
        "prompt":  prompt,
        "images":  [img_b64],
        "stream":  False,
        "options": {"temperature": 0.1, "num_predict": 200, "num_ctx": 16384},
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
#  DIRECTORY SCANNER
# ─────────────────────────────────────────────────────────────

def collect_work_items(root):
    """
    Walk root recursively and return a list of qualifying image paths.

    Inside an "upscaled/" subfolder:  all images qualify.
    Outside an "upscaled/" subfolder: only images meeting the resolution
                                      threshold qualify.
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
            if is_upscaled_dir:
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
        print("  Configuration (edit the CONFIG block at the top of the script):")
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
        print("    python tag_and_rename.py <directory>")
        print()
        sys.exit(0)

    root = os.path.abspath(sys.argv[1])
    if not os.path.isdir(root):
        print(f"ERROR: '{root}' is not a valid directory.")
        sys.exit(1)

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
    work_items = collect_work_items(root)

    if not work_items:
        print("  No qualifying images found.")
        sys.exit(0)

    total = len(work_items)
    print(f"  Found {total} qualifying image(s).\n")

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
        if is_already_processed(path):
            print(f"  {prefix} SKIP (already tagged)  {path}")
            folder_stats[dirpath]["skipped"] += 1
            total_skipped += 1
            continue

        w, h    = get_image_dimensions(path)
        dim_str = f"{w}x{h}px" if w else "?x?px"
        print(f"  {prefix} {dim_str}  {path}")

        img_start = time.time()

        try:
            # 1. Analyse
            long_desc, condensed = analyse_image(path)

            # 2. Write EXIF description + original filename
            write_exif(path, long_desc, filename)

            # 3. Rename if camera default name
            will_rename = has_camera_default_name(filename)
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


if __name__ == "__main__":
    main()