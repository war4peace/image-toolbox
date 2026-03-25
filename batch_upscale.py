"""
comfyui_batch_upscale.py
------------------------
Batch-upscales every JPG/PNG in a source directory (recursively) using
ComfyUI + SeedVR2. Upscaled images are saved to an "upscaled" subfolder
inside each processed directory, preserving the original folder structure.

Skips images where EITHER dimension already meets or exceeds the target:
  - width  >= MAX_RESOLUTION  (3840 by default)
  - height >= RESOLUTION      (2160 by default)

The upscale target respects BOTH limits simultaneously: the image is scaled
until the first limit is reached, so a portrait image will never exceed 2160px
in height even if its width hasn't reached 3840px yet.

Timing is reported per image, per folder, and in a final summary table.

Usage:
    python comfyui_batch_upscale.py "X:\\Personale\\Poze\\04-01-2004"

Requirements:
    - ComfyUI must be running locally (default: http://127.0.0.1:8188)
    - No extra Python packages needed (uses stdlib only)

Configuration:
    Edit the CONFIG block below if your setup differs.
"""

import sys
import os
import json
import time
import struct
import urllib.request
import urllib.parse
import urllib.error
import mimetypes
import random
import uuid
from collections import defaultdict
import threading
import datetime

# ─────────────────────────────────────────────
#  CONFIG  –  loaded from config.json
# ─────────────────────────────────────────────

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
_C   = _CFG.get("comfyui", {})
_U   = _CFG.get("upscale", {})

COMFYUI_URL              = _C.get("url",              "http://127.0.0.1:8000")
IMAGE_EXTS               = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff", ".tif"}
POLL_INTERVAL            = _U.get("poll_interval",    3)
POLL_TIMEOUT             = _U.get("poll_timeout",     600)
OUTPUT_SUBDIR            = _U.get("output_subdir",    "upscaled")
RESOLUTION               = _U.get("resolution",       2160)
MAX_RESOLUTION           = _U.get("max_resolution",   3840)
DISCORD_WEBHOOK_URL      = _U.get("discord_webhook_url", "")
COMFYUI_OUTAGE_THRESHOLD = _U.get("outage_threshold", 3)


# ─────────────────────────────────────────────
#  TIMING HELPERS
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
    """Format a duration as mm:ss — used for per-image elapsed time."""
    seconds = int(seconds)
    m, s    = divmod(seconds, 60)
    return f"{m:02d}:{s:02d}"


def fmt_hhmmss(seconds):
    """Format a duration as hh:mm:ss — used for total elapsed time."""
    seconds = int(seconds)
    h, rem  = divmod(seconds, 3600)
    m, s    = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


# ─────────────────────────────────────────────
#  DISCORD NOTIFICATION
# ─────────────────────────────────────────────

def send_discord_notification(title, description, color, fields=None):
    """
    Send an embed message to the configured Discord webhook.
    Silently does nothing if DISCORD_WEBHOOK_URL is empty.
    color: integer (e.g. 15548997 = red, 16776960 = yellow, 3066993 = green).
    fields: list of {"name": str, "value": str} dicts, or None.
    """
    if not DISCORD_WEBHOOK_URL:
        return
    embed = {
        "title":       title,
        "description": description,
        "color":       color,
        "fields":      fields or [],
    }
    payload = json.dumps({
        "username": "Upscale Bot",
        "embeds":   [embed],
    }).encode()
    try:
        req = urllib.request.Request(
            DISCORD_WEBHOOK_URL, data=payload,
            headers={"Content-Type": "application/json"}
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception as exc:
        print(f"  [Discord] Failed to send notification: {exc}")


# ─────────────────────────────────────────────
#  LOGGER
# ─────────────────────────────────────────────

class Logger:
    """
    Writes timestamped lines to both the terminal and a log file.
    Log file is created next to the script:
        batch_upscale_YYYY-MM-DD_HH-MM-SS.log
    Skipped files are NOT written to the log (counted silently instead).
    """
    def __init__(self):
        ts       = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        log_dir  = os.path.dirname(os.path.abspath(__file__))
        self.path = os.path.join(log_dir, f"batch_upscale_{ts}.log")
        self._fh  = open(self.path, "w", encoding="utf-8", buffering=1)

    def _ts(self):
        return datetime.datetime.now().strftime("%Y-%m-%d | %H:%M:%S")

    def tee(self, msg, timestamp=False):
        """Print to terminal and write to log file."""
        if timestamp:
            line = f"{self._ts()} | {msg}"
        else:
            line = msg
        print(line)
        self._fh.write(line + "\n")

    def log_only(self, msg, timestamp=False):
        """Write to log file only (not printed to terminal)."""
        if timestamp:
            line = f"{self._ts()} | {msg}"
        else:
            line = msg
        self._fh.write(line + "\n")

    def terminal_only(self, msg):
        """Print to terminal only (not written to log)."""
        print(msg)

    def close(self):
        self._fh.close()


# ─────────────────────────────────────────────
#  IMAGE DIMENSION READER  (no Pillow needed)
# ─────────────────────────────────────────────

def _read_png_dimensions(f):
    f.read(8)
    f.read(4)
    assert f.read(4) == b"IHDR"
    w = struct.unpack(">I", f.read(4))[0]
    h = struct.unpack(">I", f.read(4))[0]
    return w, h


def _read_jpeg_dimensions(f):
    assert f.read(2) == b"\xff\xd8"
    while True:
        marker = f.read(2)
        if len(marker) < 2:
            break
        if marker[0] != 0xFF:
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
    f.read(4)
    f.read(4)
    assert f.read(4) == b"WEBP"
    chunk = f.read(4)
    f.read(4)
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
        w = struct.unpack("<I", f.read(3) + b"\x00")[0] + 1
        h = struct.unpack("<I", f.read(3) + b"\x00")[0] + 1
        return w, h
    raise ValueError("Unknown WebP sub-format")


def get_image_dimensions(path):
    ext = os.path.splitext(path)[1].lower()
    with open(path, "rb") as f:
        try:
            if ext == ".png":
                return _read_png_dimensions(f)
            elif ext in (".jpg", ".jpeg"):
                return _read_jpeg_dimensions(f)
            elif ext == ".bmp":
                return _read_bmp_dimensions(f)
            elif ext == ".webp":
                return _read_webp_dimensions(f)
            else:
                return (0, 0)
        except Exception:
            return (0, 0)


def should_skip_resolution(path):
    """
    Skip if EITHER dimension already meets or exceeds the output target:
      width  >= MAX_RESOLUTION (3840) — already at or beyond max horizontal
      height >= RESOLUTION     (2160) — already at or beyond max vertical
    Both axes checked independently, so banners in either orientation are caught.
    """
    w, h = get_image_dimensions(path)
    if w == 0 and h == 0:
        return False, ""
    if w >= MAX_RESOLUTION:
        return True, f"width {w}px >= {MAX_RESOLUTION}px"
    if h >= RESOLUTION:
        return True, f"height {h}px >= {RESOLUTION}px"
    return False, ""


def compute_seedvr2_resolution(w, h):
    """
    Calculate the correct 'resolution' value (short side) to pass to SeedVR2
    so that the output respects BOTH axis limits simultaneously.

    SeedVR2 scales the image so its SHORT side == resolution, preserving
    aspect ratio. Naively passing RESOLUTION (2160) causes portrait images
    to overshoot the horizontal limit because 2160 becomes the short side
    (the width), scaling the height well beyond MAX_RESOLUTION (3840) or
    the vertical limit.

    Fix: compute the scale factor that satisfies both limits, then derive
    the correct short-side value from the actual output dimensions.

    Examples (MAX_RESOLUTION=3840, RESOLUTION=2160):
      600x799  → scale=2.703x → output 1622x2160 → short side = 1622  ✓
      1600x1200 → scale=1.800x → output 2880x2160 → short side = 2160  ✓
      1080x1920 → scale=1.125x → output 1215x2160 → short side = 1215  ✓
    """
    scale     = min(MAX_RESOLUTION / w, RESOLUTION / h)
    out_w     = round(w * scale)
    out_h     = round(h * scale)
    return min(out_w, out_h)


# ─────────────────────────────────────────────
#  PROMPT TEMPLATE
# ─────────────────────────────────────────────

def build_prompt(image_filename, w, h):
    """
    w, h are the source image dimensions, used to compute the correct
    SeedVR2 'resolution' (short side) that respects both axis limits.
    """
    seedvr2_resolution = compute_seedvr2_resolution(w, h)
    return {
        "1": {
            "class_type": "LoadImage",
            "inputs": {"image": image_filename, "upload": "image"}
        },
        "3": {
            "class_type": "SeedVR2LoadDiTModel",
            "inputs": {
                "model":              _U.get("dit_model", "seedvr2_ema_7b_fp16.safetensors"),
                "device":             "cuda:0",
                 "blocks_to_swap":     _U.get("blocks_to_swap", 0),
                "swap_io_components": False,
                "offload_device":     "none",
                "cache_model":        False,
                "attention_mode":     _U.get("attention_mode", "sdpa")
            }
        },
        "4": {
            "class_type": "SeedVR2LoadVAEModel",
            "inputs": {
                "model":               _U.get("vae_model", "ema_vae_fp16.safetensors"),
                "device":              "cuda:0",
                 "encode_tiled":        _U.get("encode_tiled", False),
                 "encode_tile_size":    _U.get("encode_tile_size", 1024),
                "encode_tile_overlap": 128,
                 "decode_tiled":        _U.get("decode_tiled", False),
                 "decode_tile_size":    _U.get("decode_tile_size", 1024),
                "decode_tile_overlap": 128,
                "tile_debug":          "false",
                "offload_device":      "none",
                "cache_model":         False
            }
        },
        "7": {
            "class_type": "SeedVR2VideoUpscaler",
            "inputs": {
                "image":              ["1", 0],
                "dit":                ["3", 0],
                "vae":                ["4", 0],
                "seed":               random.randint(0, 2**32 - 1),
                "resolution":         seedvr2_resolution,
                "max_resolution":     MAX_RESOLUTION,
                "batch_size":         5,
                "uniform_batch_size": False,
                "color_correction":   "lab",
                "temporal_overlap":   0,
                "prepend_frames":     0,
                "input_noise_scale":  0.0,
                "latent_noise_scale": 0.0,
                "offload_device":     "none",
                "enable_debug":       False
            }
        },
        "8": {
            "class_type": "SaveImage",
            "inputs": {"images": ["7", 0], "filename_prefix": "__batch__"}
        }
    }


# ─────────────────────────────────────────────
#  COMFYUI API HELPERS
# ─────────────────────────────────────────────

def api(path, data=None, content_type="application/json"):
    url = f"{COMFYUI_URL}{path}"
    req = urllib.request.Request(url, data=data, headers={"Content-Type": content_type})
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def upload_image(local_path):
    filename  = os.path.basename(local_path)
    mime_type = mimetypes.guess_type(filename)[0] or "image/jpeg"
    boundary  = uuid.uuid4().hex
    with open(local_path, "rb") as f:
        file_data = f.read()
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="image"; filename="{filename}"\r\n'
        f"Content-Type: {mime_type}\r\n\r\n"
    ).encode() + file_data + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        f"{COMFYUI_URL}/upload/image", data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())["name"]


def submit_prompt(prompt):
    payload = json.dumps({"prompt": prompt, "client_id": uuid.uuid4().hex}).encode()
    return api("/prompt", data=payload)["prompt_id"]


def wait_for_completion(prompt_id):
    deadline = time.time() + POLL_TIMEOUT
    while time.time() < deadline:
        try:
            history = api(f"/history/{prompt_id}")
        except urllib.error.HTTPError:
            time.sleep(POLL_INTERVAL)
            continue
        if prompt_id in history:
            entry  = history[prompt_id]
            status = entry.get("status", {})
            if status.get("completed") or status.get("status_str") == "success":
                return entry
            for msg_type, msg_data in status.get("messages", []):
                if msg_type == "execution_error":
                    raise RuntimeError(f"ComfyUI execution error: {msg_data}")
        time.sleep(POLL_INTERVAL)
    raise TimeoutError(f"Timed out waiting for prompt {prompt_id}")


def fetch_output_image(history_entry, output_dir, dest_name):
    for _, node_output in history_entry.get("outputs", {}).items():
        images = node_output.get("images", [])
        if images:
            img    = images[0]
            params = urllib.parse.urlencode({
                "filename":  img["filename"],
                "subfolder": img.get("subfolder", ""),
                "type":      img.get("type", "output")
            })
            os.makedirs(output_dir, exist_ok=True)
            dest_path = os.path.join(output_dir, dest_name)
            with urllib.request.urlopen(f"{COMFYUI_URL}/view?{params}") as resp:
                with open(dest_path, "wb") as f:
                    f.write(resp.read())
            return dest_path
    raise RuntimeError("No output image found in history entry.")



# ─────────────────────────────────────────────
#  PAUSE / QUIT CONTROLLER
#  Runs a background thread that watches for keypresses.
#
#  Space or P  →  toggle pause
#  Q           →  request graceful quit after current image finishes
#
#  Works on Windows (msvcrt) only. On other platforms it degrades
#  gracefully: pause/quit via keyboard is silently unavailable and
#  the script runs straight through as before.
# ─────────────────────────────────────────────

class PauseController:
    """
    Background thread watches for keypresses without blocking the main loop.
    Main loop calls .check() between images; that method blocks while paused
    and returns False when a quit has been requested.

    Tracks total time spent paused via .paused_seconds so callers can subtract
    it from wall-clock elapsed times, keeping the "Total elapsed" counter honest.
    """

    def __init__(self):
        self._paused         = False
        self._quit           = False
        self._lock           = threading.Lock()
        self._available      = False
        self._pause_start    = None   # wall time when current pause began
        self._paused_total   = 0.0   # accumulated seconds spent paused

        try:
            import msvcrt
            self._msvcrt = msvcrt
            self._available = True
            t = threading.Thread(target=self._watch, daemon=True)
            t.start()
        except ImportError:
            pass   # non-Windows — no keyboard control, script runs normally

    def _watch(self):
        """Background thread: poll for keypresses at ~10 Hz."""
        while True:
            if self._msvcrt.kbhit():
                key = self._msvcrt.getwch().lower()
                with self._lock:
                    if key in (" ", "p"):
                        self._paused = not self._paused
                        if self._paused:
                            self._pause_start = time.time()
                            print("\n  ⏸  PAUSED — press Space or P to resume, Q to quit after current image …")
                        else:
                            if self._pause_start is not None:
                                self._paused_total += time.time() - self._pause_start
                                self._pause_start   = None
                            print("\n  ▶  RESUMED\n")
                    elif key == "q":
                        self._quit   = True
                        self._paused = False
                        if self._pause_start is not None:
                            self._paused_total += time.time() - self._pause_start
                            self._pause_start   = None
                        print("\n  ⏹  QUIT REQUESTED — finishing current image then stopping …\n")
            time.sleep(0.1)

    def check(self):
        """
        Call this between images.
        Blocks while paused; prints the pause message exactly once per pause.
        Returns True  → continue processing
        Returns False → quit was requested, stop the loop
        """
        if not self._available:
            return True
        with self._lock:
            if self._quit:
                return False
            if not self._paused:
                return True
        # Script is paused — the pause message was already printed by _watch.
        # Just sleep until resumed or quit, no further output.
        while True:
            with self._lock:
                if self._quit:
                    return False
                if not self._paused:
                    return True
            time.sleep(0.5)

    @property
    def paused_seconds(self):
        """Total wall-clock seconds spent in pause (excluding any active pause)."""
        with self._lock:
            total = self._paused_total
            if self._paused and self._pause_start is not None:
                total += time.time() - self._pause_start
            return total

    @property
    def available(self):
        return self._available


# ─────────────────────────────────────────────
#  DIRECTORY SCANNER  (recursive)
# ─────────────────────────────────────────────

def collect_work_items(root):
    """
    Walk root recursively.
    Returns list of (local_path, output_dir, out_name), grouped by source folder.
    Never descends into OUTPUT_SUBDIR folders.
    """
    items = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d.lower() != OUTPUT_SUBDIR.lower()]
        output_dir  = os.path.join(dirpath, OUTPUT_SUBDIR)
        for filename in sorted(filenames):
            if os.path.splitext(filename)[1].lower() not in IMAGE_EXTS:
                continue
            local_path = os.path.join(dirpath, filename)
            stem       = os.path.splitext(filename)[0]
            ext        = os.path.splitext(filename)[1].lower()
            out_name   = f"{stem}_upscaled{ext}"
            items.append((dirpath, local_path, output_dir, out_name))
    return items


# ─────────────────────────────────────────────
#  SKIP SUMMARY HELPER
# ─────────────────────────────────────────────

def _emit_skip_summary(dirpath, root, folder_stats, logger):
    """
    If the folder had any skipped files, print (terminal only) a single
    summary line instead of one line per skipped file.
    """
    done  = folder_stats[dirpath]["skipped_done"]
    large = folder_stats[dirpath]["skipped_size"]
    if done == 0 and large == 0:
        return
    rel = os.path.relpath(dirpath, root) if dirpath != root else "."
    parts = []
    if done  > 0: parts.append(f"{done} already done")
    if large > 0: parts.append(f"{large} too large")
    summary = ", ".join(parts)
    # Terminal only — skips are not written to the log file
    logger.tee(f"  [skipped {summary}]")


# ─────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print("Usage: python comfyui_batch_upscale.py <directory>")
        sys.exit(1)

    root = os.path.abspath(sys.argv[1])
    if not os.path.isdir(root):
        print(f"ERROR: '{root}' is not a valid directory.")
        sys.exit(1)

    try:
        api("/system_stats")
    except Exception as e:
        print(f"ERROR: Cannot reach ComfyUI at {COMFYUI_URL}\n  → {e}")
        print("Make sure ComfyUI is running before starting this script.")
        sys.exit(1)

    logger = Logger()
    logger.tee(f"Log file: {logger.path}")
    logger.tee(f"Scanning '{root}' recursively ...")
    work_items = collect_work_items(root)

    if not work_items:
        logger.tee("No images found.")
        sys.exit(0)

    logger.tee(f"Found {len(work_items)} image(s) total.")

    pause = PauseController()
    if pause.available:
        logger.tee("  Keyboard control active: Space/P = pause, Q = quit after current image.")

    consecutive_failures = 0   # reset to 0 on every successful image

    # ── Per-folder stats (keyed by dirpath) ─────────────────────────────────
    # Each entry: {"processed": int, "skipped_done": int, "skipped_size": int,
    #              "failed": int, "elapsed": float}
    folder_stats = defaultdict(lambda: {
        "processed": 0, "skipped_done": 0, "skipped_size": 0,
        "failed": 0, "elapsed": 0.0
    })

    total_processed  = 0
    total_skipped_done  = 0
    total_skipped_size  = 0
    total_failed     = 0
    grand_start      = time.time()
    total            = len(work_items)
    current_folder   = None
    folder_start     = None

    for idx, (dirpath, local_path, output_dir, out_name) in enumerate(work_items, 1):
        rel_path = os.path.relpath(local_path, root)
        out_path = os.path.join(output_dir, out_name)
        prefix   = f"[{idx}/{total}]"

        # Print a folder banner whenever we move into a new directory
        if dirpath != current_folder:
            # Emit pending skip summary for the previous folder
            if current_folder is not None:
                _emit_skip_summary(current_folder, root, folder_stats, logger)
                elapsed = time.time() - folder_start
                folder_stats[current_folder]["elapsed"] += elapsed
                if folder_stats[current_folder]["processed"] + folder_stats[current_folder]["failed"] > 0:
                    logger.tee(f"  Folder done in {fmt_duration(elapsed)}")
                logger.tee("─" * 64)

            current_folder = dirpath
            folder_start   = time.time()
            rel_folder     = os.path.relpath(dirpath, root) if dirpath != root else "."
            logger.tee(f"📁  {rel_folder}")

        # ── Pause / quit check ──────────────────
        if not pause.check():
            logger.tee("  Stopping at user request.")
            break

        # ── Already upscaled? ───────────────────
        if os.path.exists(out_path):
            folder_stats[dirpath]["skipped_done"] += 1
            total_skipped_done += 1
            continue

        # ── Too large? ──────────────────────────
        skip, reason = should_skip_resolution(local_path)
        if skip:
            folder_stats[dirpath]["skipped_size"] += 1
            total_skipped_size += 1
            continue

        # ── Process ─────────────────────────────
        w, h      = get_image_dimensions(local_path)
        img_start = time.time()

        if w:
            scale   = min(MAX_RESOLUTION / w, RESOLUTION / h)
            out_w   = round(w * scale)
            out_h   = round(h * scale)
            dim_str = f"{w}x{h}px → {out_w}x{out_h}px"
        else:
            dim_str = "?x?px"

        logger.tee(f"  {prefix} {dim_str}  {local_path}", timestamp=True)

        try:
            comfy_name    = upload_image(local_path)
            prompt_id     = submit_prompt(build_prompt(comfy_name, w, h))
            history       = wait_for_completion(prompt_id)
            fetch_output_image(history, output_dir, out_name)

            img_elapsed   = time.time() - img_start
            grand_elapsed = time.time() - grand_start - pause.paused_seconds
            logger.tee(f"           ✓ Done in {fmt_mmss(img_elapsed)} | Total elapsed: {fmt_hhmmss(grand_elapsed)}\n", timestamp=True)

            consecutive_failures = 0
            folder_stats[dirpath]["processed"] += 1
            total_processed += 1

        except Exception as e:
            img_elapsed        = time.time() - img_start
            grand_elapsed      = time.time() - grand_start - pause.paused_seconds
            consecutive_failures += 1
            logger.tee(f"           ✗ FAILED in {fmt_mmss(img_elapsed)} | Total elapsed: {fmt_hhmmss(grand_elapsed)} — {e}\n", timestamp=True)
            folder_stats[dirpath]["failed"] += 1
            total_failed += 1

            # ── Outage detection ─────────────────────────────────────────
            if consecutive_failures >= COMFYUI_OUTAGE_THRESHOLD:
                outage_msg = (
                    f"{consecutive_failures} consecutive image(s) failed. "
                    f"ComfyUI may be down or unresponsive.\n"
                    f"Last error: {e}"
                )
                print(f"  ⚠️  OUTAGE DETECTED ({consecutive_failures} consecutive failures) — pausing.")
                print(f"  Repair ComfyUI, then press Space or P to resume.\n")

                send_discord_notification(
                    title       = "⚠️ Upscale Script — ComfyUI Outage Detected",
                    description = outage_msg,
                    color       = 15548997,  # red
                    fields      = [
                        {"name": "Last failed image", "value": local_path},
                        {"name": "Progress",          "value": f"{idx}/{total} images ({total_processed} done, {total_failed} failed)"},
                        {"name": "Total elapsed",     "value": fmt_hhmmss(grand_elapsed)},
                        {"name": "Machine",           "value": os.environ.get("COMPUTERNAME", "unknown")},
                    ]
                )

                # Force a pause — blocks here until Space/P or Q is pressed.
                # We set the internal flag directly so the existing pause
                # machinery handles the blocking and timing correctly.
                with pause._lock:
                    pause._paused      = True
                    pause._pause_start = time.time()
                    consecutive_failures = 0   # reset so we don't re-trigger immediately

                # check() will block until the user resumes
                if not pause.check():
                    print("  Stopping at user request.\n")
                    break

                send_discord_notification(
                    title       = "▶️ Upscale Script — Resumed",
                    description = "Script resumed after outage pause.",
                    color       = 3066993,   # green
                    fields      = [
                        {"name": "Progress",      "value": f"{idx}/{total} images"},
                        {"name": "Total elapsed", "value": fmt_hhmmss(time.time() - grand_start - pause.paused_seconds)},
                    ]
                )

            continue

    # Close out the last folder's timing
    if current_folder is not None:
        _emit_skip_summary(current_folder, root, folder_stats, logger)
        elapsed = time.time() - folder_start
        folder_stats[current_folder]["elapsed"] += elapsed
        if folder_stats[current_folder]["processed"] + folder_stats[current_folder]["failed"] > 0:
            logger.tee(f"  Folder done in {fmt_duration(elapsed)}")

    # ── Final summary table ──────────────────────────────────────────────────
    grand_elapsed = time.time() - grand_start - pause.paused_seconds

    # Determine column widths dynamically
    col_path  = max(
        len("Folder"),
        max((len(os.path.relpath(p, root)) for p in folder_stats), default=6)
    )
    col_proc  = len("Processed")
    col_skip  = len("Skipped")
    col_fail  = len("Failed")
    col_time  = max(len("Elapsed"), max((len(fmt_duration(v["elapsed"])) for v in folder_stats.values()), default=7))

    # Cap path column at 60 chars to avoid very wide tables
    col_path = min(col_path, 60)

    def trunc(s, n):
        return s if len(s) <= n else "…" + s[-(n - 1):]

    sep   = "═" * (col_path + col_proc + col_skip + col_fail + col_time + 16)
    row   = f"  {{:<{col_path}}}  {{:>{col_proc}}}  {{:>{col_skip}}}  {{:>{col_fail}}}  {{:>{col_time}}}"
    logger.tee("\n" + sep)
    logger.tee(row.format("Folder", "Processed", "Skipped", "Failed", "Elapsed"))
    logger.tee("─" * (col_path + col_proc + col_skip + col_fail + col_time + 16))

    for dirpath, stats in folder_stats.items():
        rel = os.path.relpath(dirpath, root) if dirpath != root else "."
        skipped = stats["skipped_done"] + stats["skipped_size"]
        logger.tee(row.format(
            trunc(rel, col_path),
            stats["processed"],
            skipped,
            stats["failed"],
            fmt_duration(stats["elapsed"])
        ))

    logger.tee("═" * (col_path + col_proc + col_skip + col_fail + col_time + 16))
    total_skipped = total_skipped_done + total_skipped_size
    logger.tee(row.format(
        "TOTAL",
        total_processed,
        total_skipped,
        total_failed,
        fmt_duration(grand_elapsed)
    ))
    logger.tee(sep)
    logger.tee(f"\n  ({total_processed} processed, {total_skipped_done} already done, {total_skipped_size} too large, {total_failed} failed)\n")
    logger.tee(f"Log written to: {logger.path}")
    logger.close()


if __name__ == "__main__":
    main()