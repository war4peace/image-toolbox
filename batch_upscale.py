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
import hashlib

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
_comfy_models_dir        = _C.get("models_dir",        "")
COMFYUI_OUTPUT_DIR       = os.path.normpath(os.path.join(_comfy_models_dir, "..", "output")) if _comfy_models_dir else ""
IMAGE_EXTS               = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff", ".tif"}
POLL_INTERVAL            = _U.get("poll_interval",    3)
POLL_TIMEOUT             = _U.get("poll_timeout",     600)
OUTPUT_SUBDIR            = _U.get("output_subdir",    "upscaled")
RESOLUTION               = _U.get("resolution",       2160)
MAX_RESOLUTION           = _U.get("max_resolution",   3840)
DISCORD_WEBHOOK_URL      = _U.get("discord_webhook_url", "")
COMFYUI_OUTAGE_THRESHOLD = _U.get("outage_threshold", 3)

# Images whose shortest dimension is already >= this fraction of the target
# will be skipped. Default 66% means a 2538x1428 image (66% of 3840x2160)
# would be skipped — only images that need at least a 1.5x upscale are processed.
# Set to 0 to disable (process all eligible images regardless of how close to target).
UPSCALE_CUTOFF_PCT = _U.get("upscale_cutoff_pct", 66)


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
            headers={
                "Content-Type": "application/json",
                "User-Agent":   "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            }
        )
        urllib.request.urlopen(req, timeout=10)
    except urllib.error.HTTPError as exc:
        body = ""
        try: body = exc.read().decode("utf-8", "replace")
        except Exception: pass
        print(f"  [Discord] Failed to send notification: HTTP {exc.code} {exc.reason} -- {body}")
    except Exception as exc:
        print(f"  [Discord] Failed to send notification: {exc}")


# ─────────────────────────────────────────────
#  TERMINAL HELPERS
# ─────────────────────────────────────────────

def _osc8_link(path):
    """
    Wrap a filesystem path in an OSC 8 hyperlink (ESC ] 8 ;; URI ESC backslash).
    Supported by Windows Terminal and VS Code terminal.
    Shift+Click opens the file in the default application.
    """
    ESC = chr(27)
    ST  = chr(92)   # string terminator: backslash (ESC + backslash = ESC ST)
    uri = "file:///" + path.replace(chr(92), "/")
    return ESC + "]8;;" + uri + ESC + ST + path + ESC + "]8;;" + ESC + ST


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
        log_dir  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
        os.makedirs(log_dir, exist_ok=True)
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
#  ELIGIBILITY CACHE
# ─────────────────────────────────────────────

class EligibilityCache:
    """
    Persists per-file eligibility results to avoid re-scanning on every run.

    Cache file location:
        <script_dir>/scans/cache_<src_hash>_<out_hash>.json

    Each entry is keyed by the file's path relative to the source root and
    stores mtime, size, eligible flag, already_done flag, and skip_reason.
    The mtime+size fingerprint detects changes to source files between runs.
    """

    VERSION = 1

    def __init__(self, source_root, output_root):
        self.source_root = source_root
        self.output_root = output_root

        # Derive a short hash from the two roots for a unique filename
        key      = f"{source_root}|{output_root}".encode("utf-8")
        digest   = hashlib.sha256(key).hexdigest()[:12]
        scans_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scans")
        os.makedirs(scans_dir, exist_ok=True)
        self.path = os.path.join(scans_dir, f"cache_{digest}.json")

        self._data   = {}   # rel_path -> entry dict
        self._dirty  = False
        self._load()

    def _load(self):
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if raw.get("version") != self.VERSION:
                return   # incompatible version — start fresh
            if raw.get("source_root") != self.source_root:
                return   # wrong source root — start fresh
            self._data = raw.get("entries", {})
        except Exception:
            self._data = {}

    def save(self):
        if not self._dirty:
            return
        payload = {
            "version":     self.VERSION,
            "source_root": self.source_root,
            "output_root": self.output_root,
            "saved_at":    datetime.datetime.now().isoformat(),
            "entries":     self._data,
        }
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            self._dirty = False
        except Exception:
            pass   # non-fatal — cache is best-effort

    def _fingerprint(self, path):
        """Return (mtime, size) for a file, or (0, 0) if unreadable."""
        try:
            st = os.stat(path)
            return round(st.st_mtime, 3), st.st_size
        except OSError:
            return 0, 0

    def get(self, local_path):
        """
        Return the cached entry for local_path if the fingerprint matches,
        else None (meaning a fresh check is needed).
        """
        rel = os.path.relpath(local_path, self.source_root)
        entry = self._data.get(rel)
        if entry is None:
            return None
        mtime, size = self._fingerprint(local_path)
        if entry.get("mtime") != mtime or entry.get("size") != size:
            return None   # file changed — re-check
        return entry

    def set(self, local_path, eligible, already_done, skip_reason=None):
        """Store or update the cache entry for local_path."""
        rel = os.path.relpath(local_path, self.source_root)
        mtime, size = self._fingerprint(local_path)
        self._data[rel] = {
            "mtime":        mtime,
            "size":         size,
            "eligible":     eligible,
            "already_done": already_done,
            "skip_reason":  skip_reason,
        }
        self._dirty = True

    def mark_done(self, local_path):
        """Mark a file as already_done=True after successful processing."""
        rel = os.path.relpath(local_path, self.source_root)
        if rel in self._data:
            self._data[rel]["already_done"] = True
            self._dirty = True

    def remove_missing(self, source_root, progress_cb=None):
        """
        Remove entries for files that no longer exist on disk.
        progress_cb: optional callable(current_path) called for each entry checked.
        """
        to_remove = []
        for rel in self._data:
            full_path = os.path.join(source_root, rel)
            if progress_cb:
                progress_cb(os.path.dirname(full_path))
            if not os.path.exists(full_path):
                to_remove.append(rel)
        for rel in to_remove:
            del self._data[rel]
        if to_remove:
            self._dirty = True
        return len(to_remove)

    @property
    def entry_count(self):
        return len(self._data)

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


def should_skip_resolution(path, cutoff_pct=None):
    """
    Skip if EITHER dimension already meets or exceeds the output target:
      width  >= MAX_RESOLUTION (3840) — already at or beyond max horizontal
      height >= RESOLUTION     (2160) — already at or beyond max vertical

    Also skip if the image is already close enough to the target that upscaling
    would give minimal benefit. The cutoff is expressed as a percentage of the
    target resolution. For example, at 66%:
      width  >= 0.66 * 3840 = 2534px  OR
      height >= 0.66 * 2160 = 1426px
    means the image is skipped (would be upscaled less than 1.52x).
    """
    if cutoff_pct is None:
        cutoff_pct = UPSCALE_CUTOFF_PCT

    w, h = get_image_dimensions(path)
    if w == 0 and h == 0:
        return False, ""

    # Already at or above target
    if w >= MAX_RESOLUTION:
        return True, f"width {w}px >= {MAX_RESOLUTION}px"
    if h >= RESOLUTION:
        return True, f"height {h}px >= {RESOLUTION}px"

    # Within cutoff percentage of target — upscale gain too small
    if cutoff_pct > 0:
        cutoff_w = MAX_RESOLUTION * cutoff_pct / 100
        cutoff_h = RESOLUTION     * cutoff_pct / 100
        if w >= cutoff_w or h >= cutoff_h:
            scale = min(MAX_RESOLUTION / w, RESOLUTION / h)
            return True, f"within cutoff ({w}x{h}px, would upscale {scale:.2f}x < {100/cutoff_pct:.2f}x minimum)"

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
            img       = images[0]
            subfolder = img.get("subfolder", "")
            filename  = img["filename"]
            img_type  = img.get("type", "output")

            params = urllib.parse.urlencode({
                "filename":  filename,
                "subfolder": subfolder,
                "type":      img_type,
            })

            os.makedirs(output_dir, exist_ok=True)
            dest_path = os.path.join(output_dir, dest_name)

            # Download from ComfyUI output folder to our destination
            with urllib.request.urlopen(f"{COMFYUI_URL}/view?{params}") as resp:
                with open(dest_path, "wb") as f:
                    f.write(resp.read())

            # Delete from ComfyUI output folder to prevent accumulation
            _delete_comfyui_output(filename, subfolder, img_type)

            return dest_path
    raise RuntimeError("No output image found in history entry.")


def _delete_comfyui_output(filename, subfolder, img_type):
    """
    Remove the file from ComfyUI's output folder after we have copied it.
    Uses direct filesystem deletion via the configured ComfyUI output path.
    Silently ignores failures — deletion is best-effort.
    """
    import glob

    comfy_output_dir = COMFYUI_OUTPUT_DIR
    if not comfy_output_dir or not os.path.isdir(comfy_output_dir):
        return

    deleted = 0

    # Primary: delete the exact file ComfyUI reported
    target_dir = os.path.join(comfy_output_dir, subfolder) if subfolder else comfy_output_dir
    comfy_file = os.path.join(target_dir, filename)
    try:
        if os.path.exists(comfy_file):
            os.remove(comfy_file)
            deleted += 1
    except Exception:
        pass

    # Fallback: delete any __batch__*.png files in the output root
    # ComfyUI names batch outputs as __batch___00001_.png etc.
    if deleted == 0:
        for stale in glob.glob(os.path.join(comfy_output_dir, "__batch__*.png")):
            try:
                os.remove(stale)
                deleted += 1
            except Exception:
                pass



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

def collect_work_items(root, output_root, already_done=None):
    """
    Walk root recursively.
    Returns (items, folder_count) where items is a list of
    (dirpath, local_path, output_dir, out_name).
    Never descends into output_root.
    already_done: optional set of local_path strings to skip (used for rescan).
    """
    already_done = already_done or set()
    items = []
    folder_count = 0
    output_root_norm = os.path.normcase(os.path.normpath(output_root))

    _tw = (min(os.get_terminal_size().columns, 200) - 1) if hasattr(os, "get_terminal_size") else 119

    for dirpath, dirnames, filenames in os.walk(root):
        # Skip the output root entirely when scanning
        dirnames[:] = [
            d for d in dirnames
            if os.path.normcase(os.path.normpath(os.path.join(dirpath, d)))
            != output_root_norm
        ]

        folder_count += 1

        # Mirror the source subdirectory structure under output_root
        rel_dir    = os.path.relpath(dirpath, root)
        output_dir = os.path.normpath(os.path.join(output_root, rel_dir))

        for filename in sorted(filenames):
            if os.path.splitext(filename)[1].lower() not in IMAGE_EXTS:
                continue
            local_path = os.path.join(dirpath, filename)
            if local_path in already_done:
                continue
            stem     = os.path.splitext(filename)[0]
            ext      = os.path.splitext(filename)[1].lower()
            out_name = f"{stem}{ext}"
            items.append((dirpath, local_path, output_dir, out_name))

        # Live progress: update in place every folder
        display = f"  {folder_count} folder(s), {len(items)} image(s) found ..."
        sys.stdout.write(display[:_tw].ljust(_tw) + chr(13))
        sys.stdout.flush()

    # Clear the progress line — final count printed by caller
    sys.stdout.write(" " * _tw + chr(13))
    sys.stdout.flush()

    return items, folder_count


# ─────────────────────────────────────────────
#  SKIP SUMMARY HELPER
# ─────────────────────────────────────────────

def _emit_skip_summary(dirpath, root, folder_stats, logger):
    """
    If the folder had any skipped files, emit a single summary line.
    Missing-file skips are logged (important); routine skips are terminal only.
    """
    done    = folder_stats[dirpath]["skipped_done"]
    large   = folder_stats[dirpath]["skipped_size"]
    missing = folder_stats[dirpath]["skipped_missing"]
    corrupt = folder_stats[dirpath]["skipped_corrupt"]
    if done == 0 and large == 0 and missing == 0 and corrupt == 0:
        return
    parts = []
    if done    > 0: parts.append(f"{done} already done")
    if large   > 0: parts.append(f"{large} too large")
    if missing > 0: parts.append(f"{missing} no longer exist")
    if corrupt > 0: parts.append(f"{corrupt} corrupted/unreadable")
    summary = ", ".join(parts)
    if missing > 0 or corrupt > 0:
        logger.tee(f"  [skipped {summary}]")
    else:
        logger.terminal_only(f"  [skipped {summary}]")


def run_pass(work_items, root, output_root, grand_start, pause, logger,
             processed_paths, cache=None, pass_label="Pass"):
    """
    Process a list of work items. Returns a stats dict.
    Adds each successfully found local_path to processed_paths so the rescan
    can exclude already-attempted files regardless of outcome.
    """
    consecutive_failures = 0

    folder_stats = defaultdict(lambda: {
        "processed": 0, "skipped_done": 0, "skipped_size": 0,
        "skipped_missing": 0, "skipped_corrupt": 0, "failed": 0, "elapsed": 0.0
    })

    total_processed       = 0
    total_skipped_done    = 0
    total_skipped_size    = 0
    total_skipped_missing = 0
    total_skipped_corrupt = 0
    total_failed          = 0
    total                 = len(work_items)
    current_folder        = None
    folder_start          = None

    for idx, (dirpath, local_path, output_dir, out_name) in enumerate(work_items, 1):
        out_path = os.path.join(output_dir, out_name)
        prefix   = f"[{idx}/{total}]" if not pass_label else f"[{pass_label} {idx}/{total}]"

        # ── Folder banner ────────────────────────────────────────────────────
        if dirpath != current_folder:
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

        # ── Pause / quit check ───────────────────────────────────────────────
        if not pause.check():
            logger.tee("  Stopping at user request.")
            break

        # ── File still exists? ───────────────────────────────────────────────
        if not os.path.exists(local_path):
            logger.tee(f"  {prefix} SKIP (file no longer exists)  {local_path}", timestamp=True)
            folder_stats[dirpath]["skipped_missing"] += 1
            total_skipped_missing += 1
            continue

        # ── Already upscaled? ────────────────────────────────────────────────
        if os.path.exists(out_path):
            folder_stats[dirpath]["skipped_done"] += 1
            total_skipped_done += 1
            continue

        # ── Too large? ───────────────────────────────────────────────────────
        skip, reason = should_skip_resolution(local_path)
        if skip:
            folder_stats[dirpath]["skipped_size"] += 1
            total_skipped_size += 1
            continue

        # ── Process ──────────────────────────────────────────────────────────
        w, h      = get_image_dimensions(local_path)
        img_start = time.time()

        # Unreadable dimensions = corrupted/unsupported file — skip without
        # counting as a ComfyUI failure or incrementing the outage counter.
        if w == 0 or h == 0:
            logger.tee(f"  {prefix} SKIP (unreadable image — file may be corrupted)  {local_path}", timestamp=True)
            folder_stats[dirpath]["skipped_corrupt"] += 1
            total_skipped_corrupt += 1
            continue

        scale   = min(MAX_RESOLUTION / w, RESOLUTION / h)
        out_w   = round(w * scale)
        out_h   = round(h * scale)
        dim_str = f"{w}x{h}px -> {out_w}x{out_h}px"

        os.makedirs(output_dir, exist_ok=True)
        linked_path = _osc8_link(local_path)
        # Clear any lingering status line, then print the new image line
        _tw = (min(os.get_terminal_size().columns, 200) - 1) if hasattr(os, "get_terminal_size") else 119
        sys.stdout.write(" " * _tw + chr(13)); sys.stdout.flush()
        import datetime as _dt
        _ts = _dt.datetime.now().strftime("%Y-%m-%d | %H:%M:%S")
        print(f"{_ts} |   {prefix} {dim_str}  {linked_path}", flush=True)
        logger.log_only(f"  {prefix} {dim_str}  {local_path}", timestamp=True)

        try:
            comfy_name    = upload_image(local_path)
            prompt_id     = submit_prompt(build_prompt(comfy_name, w, h))
            history       = wait_for_completion(prompt_id)
            fetch_output_image(history, output_dir, out_name)

            img_elapsed   = time.time() - img_start
            grand_elapsed = time.time() - grand_start - pause.paused_seconds
            status = f"Last: {fmt_mmss(img_elapsed)} | Total elapsed: {fmt_hhmmss(grand_elapsed)}"
            _tw = (min(os.get_terminal_size().columns, 200) - 1) if hasattr(os, "get_terminal_size") else 119
            sys.stdout.write((" " * 13 + status)[:_tw].ljust(_tw) + chr(13))
            sys.stdout.flush()
            logger.log_only(f"           Done in {fmt_mmss(img_elapsed)} | Total elapsed: {fmt_hhmmss(grand_elapsed)}", timestamp=True)

            consecutive_failures = 0
            processed_paths.add(local_path)
            if cache is not None:
                cache.mark_done(local_path)
                cache.save()
            folder_stats[dirpath]["processed"] += 1
            total_processed += 1

        except Exception as e:
            img_elapsed        = time.time() - img_start
            grand_elapsed      = time.time() - grand_start - pause.paused_seconds
            consecutive_failures += 1
            # Clear the status line first, then print failure visibly
            _tw = (min(os.get_terminal_size().columns, 200) - 1) if hasattr(os, "get_terminal_size") else 119
            sys.stdout.write(" " * _tw + chr(13)); sys.stdout.flush()
            logger.tee(f"           FAILED in {fmt_mmss(img_elapsed)} | Total elapsed: {fmt_hhmmss(grand_elapsed)} -- {e}", timestamp=True)
            folder_stats[dirpath]["failed"] += 1
            total_failed += 1

            if consecutive_failures >= COMFYUI_OUTAGE_THRESHOLD:
                outage_msg = (
                    f"{consecutive_failures} consecutive image(s) failed. "
                    f"ComfyUI may be down or unresponsive.\n"
                    f"Last error: {e}"
                )
                logger.tee(f"  WARNING: OUTAGE DETECTED ({consecutive_failures} consecutive failures) -- pausing.")
                logger.tee(f"  Repair ComfyUI, then press Space or P to resume.")

                send_discord_notification(
                    title       = "Upscale Script -- ComfyUI Outage Detected",
                    description = outage_msg,
                    color       = 15548997,
                    fields      = [
                        {"name": "Last failed image", "value": local_path},
                        {"name": "Progress",          "value": f"{idx}/{total}"},
                        {"name": "Total elapsed",     "value": fmt_hhmmss(grand_elapsed)},
                        {"name": "Machine",           "value": os.environ.get("COMPUTERNAME", "unknown")},
                    ]
                )

                with pause._lock:
                    pause._paused      = True
                    pause._pause_start = time.time()
                    consecutive_failures = 0

                logger.tee("  Press Space/P to resume, or Q to quit gracefully.")
                if not pause.check():
                    logger.tee("  Stopping at user request.")
                    break

                send_discord_notification(
                    title       = "Upscale Script -- Resumed",
                    description = "Script resumed after outage pause.",
                    color       = 3066993,
                    fields      = [
                        {"name": "Progress",      "value": f"{idx}/{total}"},
                        {"name": "Total elapsed", "value": fmt_hhmmss(time.time() - grand_start - pause.paused_seconds)},
                    ]
                )

            continue

    # ── Close last folder ────────────────────────────────────────────────────
    if current_folder is not None:
        _emit_skip_summary(current_folder, root, folder_stats, logger)
        elapsed = time.time() - folder_start
        folder_stats[current_folder]["elapsed"] += elapsed
        if folder_stats[current_folder]["processed"] + folder_stats[current_folder]["failed"] > 0:
            logger.tee(f"  Folder done in {fmt_duration(elapsed)}")

    return {
        "folder_stats":          folder_stats,
        "total_processed":       total_processed,
        "total_skipped_done":    total_skipped_done,
        "total_skipped_size":    total_skipped_size,
        "total_skipped_missing": total_skipped_missing,
        "total_skipped_corrupt": total_skipped_corrupt,
        "total_failed":          total_failed,
        "user_quit":             pause._quit if hasattr(pause, '_quit') else False,
    }


# ─────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────

def main():
    global UPSCALE_CUTOFF_PCT
    if len(sys.argv) < 2:
        print("Usage: python batch_upscale.py <source_dir> [output_dir] [cutoff_pct]")
        print("")
        print("  source_dir   Directory to scan for images (searched recursively).")
        print("  output_dir   Optional. Where to write upscaled images.")
        print("               Defaults to <source_dir>\\__upscaled__")
        print("               Can be an absolute path outside the source tree.")
        print(f"  cutoff_pct   Optional. Skip images already at or above this percentage")
        print(f"               of the target resolution (default: {UPSCALE_CUTOFF_PCT}%).")
        print(f"               Example: 66 means skip images >= 66% of target size.")
        print(f"               Set to 0 to process all eligible images.")
        print("")
        print("  Examples:")
        print("    python batch_upscale.py \"X:\\Photos\\old\"")
        print("    python batch_upscale.py \"X:\\Photos\\old\" \"X:\\Photos\\new\"")
        print("    python batch_upscale.py \"X:\\Photos\\old\" \"X:\\Photos\\new\" 75")
        print("    python batch_upscale.py \"X:\\Photos\\old\" \"\" 50  # empty string = use default output path")
        sys.exit(0)

    root = os.path.abspath(sys.argv[1])
    if not os.path.isdir(root):
        print(f"ERROR: Source directory not found: '{root}'")
        sys.exit(1)

    # ── Determine output root ────────────────────────────────────────────────
    default_output = os.path.join(root, "__upscaled__")

    if len(sys.argv) >= 3:
        # Output path provided as second argument
        output_root = os.path.abspath(sys.argv[2])
        print(f"Output directory: {output_root}")
    else:
        # Prompt with default
        print(f"")
        print(f"  Output directory for upscaled images.")
        print(f"  Press Enter to use default, or type an absolute path:")
        print(f"  Default: {default_output}")
        print(f"")
        user_input = input("  Output path: ").strip()
        if user_input:
            output_root = os.path.abspath(user_input)
        else:
            output_root = default_output

    # Optional third argument: upscale cutoff percentage
    # Overrides the value from config.json for this run only.
    if len(sys.argv) >= 4:
        try:
            UPSCALE_CUTOFF_PCT = int(sys.argv[3])
            if not 0 <= UPSCALE_CUTOFF_PCT <= 99:
                raise ValueError
            print(f"Upscale cutoff: {UPSCALE_CUTOFF_PCT}% (from command line)")
        except ValueError:
            print(f"WARNING: Invalid cutoff percentage '{sys.argv[3]}' — using config value ({UPSCALE_CUTOFF_PCT}%)")

    # Create output root if needed
    os.makedirs(output_root, exist_ok=True)

    try:
        api("/system_stats")
    except Exception as e:
        print(f"ERROR: Cannot reach ComfyUI at {COMFYUI_URL}\n  -> {e}")
        print("Make sure ComfyUI is running before starting this script.")
        sys.exit(1)

    logger = Logger()
    logger.tee(f"Log file: {logger.path}")
    logger.tee(f"Source:   {root}")
    logger.tee(f"Output:   {output_root}")
    logger.tee(f"Cutoff:   {UPSCALE_CUTOFF_PCT}% (skip images >= {UPSCALE_CUTOFF_PCT}% of target resolution)")
    logger.tee(f"Scanning '{root}' recursively ...")
    all_items, total_folders = collect_work_items(root, output_root)

    if not all_items:
        logger.tee("No images found.")
        sys.exit(0)

    logger.tee(f"Found {total_folders} folder(s) and {len(all_items)} image(s).")

    # ── Load eligibility cache ────────────────────────────────────────────────
    cache = EligibilityCache(root, output_root)
    if cache.entry_count > 0:
        logger.tee(f"Loaded eligibility cache: {cache.entry_count} entries ({cache.path})")
        print("  Verifying cached entries against disk (may take a moment on network drives) ...", flush=True)

        _last_folder = [""]
        _folder_idx  = [0]
        def _verify_progress(folder):
            if folder != _last_folder[0]:
                _last_folder[0] = folder
                _folder_idx[0] += 1
                pct     = int(_folder_idx[0] / max(total_folders, 1) * 100)
                display = f"{pct}% ({_folder_idx[0]}/{total_folders}) | {folder}"
                _tw = (min(os.get_terminal_size().columns, 200) - 1) if hasattr(os, "get_terminal_size") else 119
                sys.stdout.write(display[:_tw].ljust(_tw) + chr(13))
                sys.stdout.flush()

        removed = cache.remove_missing(root, progress_cb=_verify_progress)
        _tw = min(os.get_terminal_size().columns - 1, 200) if hasattr(os, "get_terminal_size") else 119; sys.stdout.write(" " * _tw + chr(13)); sys.stdout.flush()  # clear the folder line
        if removed:
            logger.tee(f"  Removed {removed} stale entries (files no longer on disk).")
        else:
            logger.tee(f"  All {cache.entry_count} cached entries verified.")
    else:
        logger.tee(f"No cache found — full eligibility check required.")

    logger.tee(f"Checking eligibility (this may take a while) ...")

    # ── Eligibility pre-check (cache-aware) ───────────────────────────────────
    work_items     = []
    pre_done       = 0
    pre_too_large  = 0
    cache_hits     = 0
    total_all      = len(all_items)

    for i, item in enumerate(all_items, 1):
        dirpath, local_path, output_dir, out_name = item
        out_path = os.path.join(output_dir, out_name)

        # Progress indicator every 200 files or on last file
        if i % 200 == 0 or i == total_all:
            pct = int(i / total_all * 100)
            sys.stdout.write("  Checking eligibility ... %d%% (%d/%d) | cache hits: %d    " % (
                pct, i, total_all, cache_hits) + chr(13)); sys.stdout.flush()

        cached = cache.get(local_path)

        if cached is not None:
            cache_hits += 1
            if cached["already_done"]:
                pre_done += 1
                continue
            if not cached["eligible"]:
                pre_too_large += 1
                continue
            # Eligible and not done — add to work list
            work_items.append(item)
            continue

        # Cache miss — do full check and store result
        if os.path.exists(out_path):
            cache.set(local_path, eligible=True, already_done=True)
            pre_done += 1
            continue

        skip, reason = should_skip_resolution(local_path)
        if skip:
            cache.set(local_path, eligible=False, already_done=False, skip_reason=reason)
            pre_too_large += 1
            continue

        cache.set(local_path, eligible=True, already_done=False)
        work_items.append(item)

    print()  # newline after progress indicator
    cache.save()

    logger.tee(f"Found {len(work_items)} eligible file(s) "
               f"({pre_done} already done, {pre_too_large} too large — "
               f"{cache_hits}/{total_all} from cache).")

    if not work_items:
        logger.tee("Nothing to process.")
        sys.exit(0)

    pause = PauseController()
    if pause.available:
        logger.tee("  Keyboard control active: Space/P = pause, Q = quit after current image.")
    logger.tee("")
    logger.tee("Processing ...")

    # ── Run first pass ──────────────────────────────────────────────────────
    grand_start     = time.time()
    processed_paths = set()
    stats1 = run_pass(work_items, root, output_root, grand_start,
                      pause, logger, processed_paths, cache=cache, pass_label="")

    # ── Rescan for new/renamed files (skip if user quit) ────────────────────
    stats2 = None
    if stats1.get("user_quit"):
        logger.tee("")
        logger.tee("  Quit requested by user — skipping rescan.")
    else:
        logger.tee("")
        logger.tee("  Rescanning source directory for any new or renamed files ...")
        rescan_items, _ = collect_work_items(root, output_root, already_done=processed_paths)

        # Eligibility filter for new items — check resolution and already-done
        eligible_rescan = []
        for item in rescan_items:
            dirpath, local_path, output_dir, out_name = item
            out_path = os.path.join(output_dir, out_name)
            if os.path.exists(out_path):
                cache.set(local_path, eligible=True, already_done=True)
                continue
            cached = cache.get(local_path)
            if cached is not None:
                if cached["already_done"] or not cached["eligible"]:
                    continue
                eligible_rescan.append(item)
            else:
                skip, reason = should_skip_resolution(local_path)
                if skip:
                    cache.set(local_path, eligible=False, already_done=False, skip_reason=reason)
                    continue
                cache.set(local_path, eligible=True, already_done=False)
                eligible_rescan.append(item)
        cache.save()
        rescan_items = eligible_rescan

        if rescan_items:
            logger.tee(f"  Found {len(rescan_items)} new item(s) — processing second pass.")
            logger.tee("")
            logger.tee("Processing new items ...")
            grand_start2 = time.time()
            stats2 = run_pass(rescan_items, root, output_root, grand_start2,
                              pause, logger, processed_paths, cache=cache, pass_label="")
        else:
            logger.tee("  No new items found.")

    # ── Combined summary table ──────────────────────────────────────────────
    grand_elapsed = time.time() - grand_start

    # Merge stats from both passes
    def merge(s1, s2):
        if s2 is None:
            return s1
        merged = defaultdict(lambda: {
            "processed": 0, "skipped_done": 0, "skipped_size": 0,
            "skipped_missing": 0, "failed": 0, "elapsed": 0.0
        })
        for d, v in s1["folder_stats"].items():
            for k in v: merged[d][k] += v[k]
        for d, v in s2["folder_stats"].items():
            for k in v: merged[d][k] += v[k]
        return {
            "folder_stats":          merged,
            "total_processed":       s1["total_processed"]       + s2["total_processed"],
            "total_skipped_done":    s1["total_skipped_done"]    + s2["total_skipped_done"],
            "total_skipped_size":    s1["total_skipped_size"]    + s2["total_skipped_size"],
            "total_skipped_missing": s1["total_skipped_missing"] + s2["total_skipped_missing"],
            "total_skipped_corrupt": s1["total_skipped_corrupt"] + s2["total_skipped_corrupt"],
            "total_failed":          s1["total_failed"]          + s2["total_failed"],
        }

    combined        = merge(stats1, stats2)
    folder_stats    = combined["folder_stats"]
    total_processed       = combined["total_processed"]
    total_skipped_done    = combined["total_skipped_done"]
    total_skipped_size    = combined["total_skipped_size"]
    total_skipped_missing = combined["total_skipped_missing"]
    total_skipped_corrupt = combined["total_skipped_corrupt"]
    total_failed          = combined["total_failed"]

    col_path = min(60, max(len("Folder"),
        max((len(os.path.relpath(p, root)) for p in folder_stats), default=6)))
    col_proc = len("Processed")
    col_skip = len("Skipped")
    col_fail = len("Failed")
    col_time = max(len("Elapsed"),
        max((len(fmt_duration(v["elapsed"])) for v in folder_stats.values()), default=7))

    def trunc(s, n):
        return s if len(s) <= n else "..." + s[-(n - 3):]

    sep = "=" * (col_path + col_proc + col_skip + col_fail + col_time + 16)
    row = f"  {{:<{col_path}}}  {{:>{col_proc}}}  {{:>{col_skip}}}  {{:>{col_fail}}}  {{:>{col_time}}}"

    logger.tee("")
    logger.tee(sep)
    logger.tee(row.format("Folder", "Processed", "Skipped", "Failed", "Elapsed"))
    logger.tee("-" * (col_path + col_proc + col_skip + col_fail + col_time + 16))

    for dp, stats in folder_stats.items():
        rel = os.path.relpath(dp, root) if dp != root else "."
        total_skipped_folder = stats["skipped_done"] + stats["skipped_size"]
        logger.tee(row.format(trunc(rel, col_path),
                              stats["processed"], total_skipped_folder,
                              stats["failed"], fmt_duration(stats["elapsed"])))

    logger.tee("=" * (col_path + col_proc + col_skip + col_fail + col_time + 16))
    total_skipped = total_skipped_done + total_skipped_size
    logger.tee(row.format("TOTAL", total_processed, total_skipped,
                          total_failed, fmt_hhmmss(grand_elapsed)))
    logger.tee(sep)
    parts = [f"{total_processed} processed", f"{total_skipped_done} already done",
             f"{total_skipped_size} too large"]
    if total_skipped_missing > 0: parts.append(f"{total_skipped_missing} missing")
    if total_skipped_corrupt > 0: parts.append(f"{total_skipped_corrupt} corrupted")
    if total_failed          > 0: parts.append(f"{total_failed} failed")
    else: parts.append("0 failed")
    logger.tee(f"  ({', '.join(parts)})")
    logger.tee(f"Log written to: {logger.path}")
    logger.close()


if __name__ == "__main__":
    main()