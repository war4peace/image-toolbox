"""
batch_upscale.py
----------------
Batch-upscales every image in a source directory (recursively) using the
SeedVR2 pipeline directly in-process — no ComfyUI required. Upscaled images
are saved to the destination folder, preserving the original folder structure.

Skips images where EITHER dimension already meets or exceeds the target:
  - width  >= MAX_RESOLUTION  (3840 by default)
  - height >= RESOLUTION      (2160 by default)

The upscale target respects BOTH limits simultaneously: the image is scaled
until the first limit is reached, so a portrait image will never exceed 2160px
in height even if its width hasn't reached 3840px yet.

Timing is reported per image, per folder, and in a final summary table.

Usage (from the toolbox folder, inside its venv):
    .venv\\Scripts\\python.exe batch_upscale.py "X:\\Personale\\Poze\\04-01-2004"

Requirements:
    - The cloned SeedVR2 repository (see config.json "seedvr2.repo_dir")
    - A venv with PyTorch (CUDA) + seedvr2/requirements.txt + pillow
    - Model weights are downloaded automatically on first run

Configuration:
    config.json in the same directory as this script (run setup.ps1).
"""

import sys
import os
import json
import time
import struct
import urllib.request
import urllib.parse
import urllib.error
from collections import defaultdict
import threading
import datetime
import hashlib

# Write a logs/crash_*.log on any unhandled crash. notify=False: this runs
# headless as a GUI subprocess, whose traceback already reaches the GUI log pane
# via stderr — no message box. Defensive import so a missing module can't break
# the run.
try:
    import crash_logger
    crash_logger.install(notify=False)
except Exception:
    pass

import db

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
_S   = _CFG.get("seedvr2", {})
_U   = _CFG.get("upscale", {})

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

def _resolve_path(value, default_rel):
    """
    Resolve a config path; relative paths are anchored at this script's dir.
    Environment variables (%USERPROFILE%, %USERNAME%, …) are expanded, so
    config.json stays portable between machines and users.
    """
    p = os.path.expandvars(value or default_rel)
    return p if os.path.isabs(p) else os.path.normpath(os.path.join(_SCRIPT_DIR, p))

SEEDVR2_REPO_DIR    = _resolve_path(_S.get("repo_dir", ""),  "seedvr2")
SEEDVR2_MODEL_DIR   = _resolve_path(_S.get("model_dir", ""), os.path.join("models", "SEEDVR2"))

IMAGE_EXTS          = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff", ".tif"}
OUTPUT_SUBDIR       = _U.get("output_subdir",    "upscaled")
RESOLUTION          = _U.get("resolution",       2160)
MAX_RESOLUTION      = _U.get("max_resolution",   3840)
DISCORD_WEBHOOK_URL = _U.get("discord_webhook_url", "")
OUTAGE_THRESHOLD    = _U.get("outage_threshold", 3)

# The SeedVR2 engine — created in main() after CLI validation so that
# `python batch_upscale.py` (usage help) stays instant.
ENGINE = None


def _stdin_is_piped():
    """True when stdin is a pipe — i.e. we are driven by toolbox_gui.py."""
    try:
        return sys.stdin is not None and not sys.stdin.isatty()
    except Exception:
        return False

# GUI mode: control arrives as stdin lines instead of keypresses, and
# machine-readable event lines keep the GUI's preview strip in sync.
GUI_MODE = _stdin_is_piped()

# Marker prefix the GUI intercepts (never shown to the user there).
GUI_MARKER = "@@TBX@@"


def _gui_event(kind, payload):
    """
    Emit one event line for the GUI (no-op outside GUI mode).
      IMG|<path>      – image now being processed
      QUEUE|<json>    – ordered list of queued image paths for this pass
    """
    if GUI_MODE:
        print(f"{GUI_MARKER}{kind}|{payload}", flush=True)

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

def _terminal_width():
    """
    Terminal width capped at 200, minus one. Falls back to 119 when stdout
    is not a console (redirected output, scheduled runs).
    """
    try:
        return min(os.get_terminal_size().columns, 200) - 1
    except (OSError, ValueError):
        return 119


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

    Log file is keyed by source root so all sessions for the same source
    folder append to the same file:
        logs/log_<12-char hash of source root>.log

    Each session opens with a header line so individual runs are
    distinguishable within the file.
    """
    def __init__(self, source_root):
        digest   = hashlib.sha256(source_root.encode("utf-8")).hexdigest()[:12]
        log_dir  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
        os.makedirs(log_dir, exist_ok=True)
        self.path = os.path.join(log_dir, f"log_{digest}.log")
        # Append mode — preserves previous sessions
        self._fh  = open(self.path, "a", encoding="utf-8", buffering=1)
        # Write a session start marker so runs are distinguishable in the file
        ts = datetime.datetime.now().strftime("%Y-%m-%d | %H:%M:%S")
        self._fh.write(f"\n{'=' * 64}\n")
        self._fh.write(f"Session started: {ts}\n")
        self._fh.write(f"Source: {source_root}\n")
        self._fh.write(f"{'=' * 64}\n")

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

    Backed by the shared SQLite database (db/cache.db, tables upscale_roots /
    upscale_files); the per-folder JSON files in scans/ are no longer written.
    Entries for this run's source root are loaded into memory; writes are
    flushed incrementally on save() (only changed/removed rows), keyed by the
    file's path relative to the source root. The mtime+size fingerprint detects
    changes to source files between runs.
    """

    def __init__(self, source_root, output_root):
        self.source_root = source_root
        self.output_root = output_root
        self.path        = db.DB_PATH      # shown in log messages

        self._conn    = db.get_conn()
        self._root_id = db.get_upscale_root_id(self._conn, source_root, output_root)
        self._data    = {}            # rel_path -> entry dict
        self._dirty   = set()         # rel_paths needing upsert
        self._removed = set()         # rel_paths needing delete
        self._load()

    def _load(self):
        for row in self._conn.execute(
                "SELECT rel_path, mtime, size, eligible, already_done, skip_reason "
                "FROM upscale_files WHERE root_id = ?", (self._root_id,)):
            self._data[row["rel_path"]] = {
                "mtime":        row["mtime"],
                "size":         row["size"],
                "eligible":     bool(row["eligible"]),
                "already_done": bool(row["already_done"]),
                "skip_reason":  row["skip_reason"],
            }

    def save(self):
        if not self._dirty and not self._removed:
            return
        try:
            with self._conn:   # one transaction; rolls back on error
                if self._removed:
                    self._conn.executemany(
                        "DELETE FROM upscale_files WHERE root_id = ? AND rel_path = ?",
                        [(self._root_id, rel) for rel in self._removed])
                if self._dirty:
                    rows = []
                    for rel in self._dirty:
                        e = self._data.get(rel)
                        if e is None:
                            continue
                        rows.append((self._root_id, rel, e["mtime"], e["size"],
                                     1 if e["eligible"] else 0,
                                     1 if e["already_done"] else 0,
                                     e["skip_reason"]))
                    self._conn.executemany(
                        "INSERT OR REPLACE INTO upscale_files "
                        "(root_id, rel_path, mtime, size, eligible, already_done, skip_reason) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
                self._conn.execute(
                    "UPDATE upscale_roots SET output_root = ?, saved_at = ? WHERE id = ?",
                    (self.output_root, datetime.datetime.now().isoformat(), self._root_id))
            self._dirty.clear()
            self._removed.clear()
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
        self._dirty.add(rel)
        self._removed.discard(rel)

    def mark_done(self, local_path):
        """Mark a file as already_done=True after successful processing."""
        rel = os.path.relpath(local_path, self.source_root)
        if rel in self._data:
            self._data[rel]["already_done"] = True
            self._dirty.add(rel)

    def record_lineage(self, src_path, out_path):
        """
        Hash the source and its freshly-written upscaled output and link them in
        the lineage table, so the pair can be re-matched by content after either
        tree is moved or renamed. Best-effort: never fails an upscale.
        """
        try:
            src_hash = db.hash_file_cached(self._conn, src_path)
            out_hash = db.hash_file_cached(self._conn, out_path)
            db.record_upscale_lineage(self._conn, src_hash, out_hash,
                                      src_path, out_path)
        except Exception:
            pass

    def remove_missing(self, source_root, progress_cb=None, abort_check=None):
        """
        Remove entries for files that no longer exist on disk.
        progress_cb:  optional callable(current_path) called for each entry checked.
        abort_check:  optional callable; when it returns True the check stops
                      early (removals confirmed so far are still applied).
        """
        to_remove = []
        for rel in self._data:
            if abort_check is not None and abort_check():
                break
            full_path = os.path.join(source_root, rel)
            if progress_cb:
                progress_cb(os.path.dirname(full_path))
            if not os.path.exists(full_path):
                to_remove.append(rel)
        for rel in to_remove:
            del self._data[rel]
            self._removed.add(rel)
            self._dirty.discard(rel)
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


def _read_tiff_dimensions(f):
    """Read width/height from a TIFF file header (supports little and big endian)."""
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
        if typ in (3, 4):  # SHORT or LONG
            fmt = endian + ("H" if typ == 3 else "I")
            val = struct.unpack(fmt, val_raw[:struct.calcsize(fmt)])[0]
            if tag == 256:   w = val   # ImageWidth
            elif tag == 257: h = val   # ImageLength
        if w and h:
            break
    return w, h


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
            elif ext in (".tif", ".tiff"):
                return _read_tiff_dimensions(f)
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
        # Real terminal stream, captured before the engine redirects
        # sys.stdout during upscales — keypress feedback must reach the
        # terminal even while the redirect is active in the main thread.
        self._term           = sys.stdout

        try:
            import msvcrt
            self._msvcrt = msvcrt
        except ImportError:
            self._msvcrt = None   # non-Windows

        piped = False
        try:
            piped = sys.stdin is not None and not sys.stdin.isatty()
        except Exception:
            pass

        if piped:
            # GUI / pipe mode: line commands on stdin replace the keyboard
            # ("p" toggles pause, "q" quits — see toolbox_gui.py).
            self._available = True
            threading.Thread(target=self._watch_stdin, daemon=True).start()
        elif self._msvcrt is not None:
            self._available = True
            threading.Thread(target=self._watch, daemon=True).start()

    def _handle_key(self, key):
        """Apply a control command: space/p toggles pause, q requests quit."""
        with self._lock:
            if key in (" ", "p"):
                self._paused = not self._paused
                if self._paused:
                    self._pause_start = time.time()
                    if GUI_MODE:
                        print("\n  ⏸  PAUSED — click Resume to continue, or Stop to finish after the current image …",
                              file=self._term, flush=True)
                    else:
                        print("\n  ⏸  PAUSED — press Space or P to resume, Q to quit after current image …",
                              file=self._term, flush=True)
                else:
                    if self._pause_start is not None:
                        self._paused_total += time.time() - self._pause_start
                        self._pause_start   = None
                    print("\n  ▶  RESUMED\n", file=self._term, flush=True)
            elif key == "q":
                self._quit   = True
                self._paused = False
                if self._pause_start is not None:
                    self._paused_total += time.time() - self._pause_start
                    self._pause_start   = None
                print("\n  ⏹  QUIT REQUESTED — finishing current image then stopping …\n",
                      file=self._term, flush=True)

    def _watch(self):
        """Background thread: poll the console for keypresses at ~10 Hz."""
        while True:
            if self._msvcrt.kbhit():
                self._handle_key(self._msvcrt.getwch().lower())
            time.sleep(0.1)

    def _watch_stdin(self):
        """
        Background thread for GUI/pipe mode: one command per stdin line.
        EOF means the controlling process is gone — stop gracefully so the
        run never continues unattended after the GUI closes.
        """
        try:
            for line in sys.stdin:
                cmd = line.strip().lower()
                if cmd in ("p", "pause", "resume"):
                    self._handle_key("p")
                elif cmd in ("q", "quit"):
                    self._handle_key("q")
        except Exception:
            pass
        if not self._quit:
            self._handle_key("q")

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
    def quit_requested(self):
        """True once a graceful quit/cancel has been requested."""
        with self._lock:
            return self._quit

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

def collect_work_items(root, output_root, already_done=None, abort_check=None,
                       status_prefix="Scanning for images …"):
    """
    Walk root recursively.
    Returns (items, folder_count) where items is a list of
    (dirpath, local_path, output_dir, out_name).
    Never descends into output_root.
    already_done: optional set of local_path strings to skip (used for rescan).
    abort_check:  optional callable; when it returns True the walk stops early
                  and the partial result is returned (caller decides what to do).
    status_prefix: leading text for the live GUI status line; the running folder
                  and image counts are appended so the user sees scan progress.
    """
    already_done = already_done or set()
    items = []
    folder_count = 0
    output_root_norm = os.path.normcase(os.path.normpath(output_root))

    _tw = _terminal_width()
    _last_status = 0.0          # throttle GUI status updates to a few per second

    def _emit_status():
        _gui_event(
            "STATUS",
            f"{status_prefix} {folder_count} folder(s) and {len(items)} image(s) found")

    for dirpath, dirnames, filenames in os.walk(root):
        if abort_check is not None and abort_check():
            break
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

        # Mirror the same live counts to the GUI status line, throttled so a
        # huge tree doesn't flood the event pipe.
        now = time.time()
        if now - _last_status >= 0.25:
            _last_status = now
            _emit_status()

    # Clear the progress line — final count printed by caller
    sys.stdout.write(" " * _tw + chr(13))
    sys.stdout.flush()
    _emit_status()                       # final exact totals

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

    # GUI preview strip: announce the full ordered queue for this pass
    if GUI_MODE:
        _gui_event("QUEUE", json.dumps([item[1] for item in work_items]))

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
    corrupt_files         = []      # absolute paths of unreadable/corrupt images
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
            # Self-heal the cache: the output exists but this item was still in
            # the work list (its cached entry said not-done, e.g. the output
            # was created by an older run). Record it so future runs exclude it.
            if cache is not None:
                cache.mark_done(local_path)
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
        # counting as an engine failure or incrementing the outage counter.
        if w == 0 or h == 0:
            import datetime as _dt
            _ts = _dt.datetime.now().strftime("%Y-%m-%d | %H:%M:%S")
            src_folder_link = _osc8_link(dirpath).replace(dirpath, "📁")
            linked_path     = _osc8_link(local_path)
            print(f"{_ts} |   {prefix} SKIP (unreadable) {src_folder_link} {linked_path}")
            logger.log_only(f"  {prefix} SKIP (unreadable image — file may be corrupted)  {local_path}", timestamp=True)
            folder_stats[dirpath]["skipped_corrupt"] += 1
            total_skipped_corrupt += 1
            corrupt_files.append(local_path)
            continue

        scale   = min(MAX_RESOLUTION / w, RESOLUTION / h)
        out_w   = round(w * scale)
        out_h   = round(h * scale)
        dim_str = f"{w}x{h}px -> {out_w}x{out_h}px"

        os.makedirs(output_dir, exist_ok=True)
        _gui_event("IMG", local_path)    # GUI preview strip: current image
        linked_path = _osc8_link(local_path)
        import datetime as _dt
        _ts = _dt.datetime.now().strftime("%Y-%m-%d | %H:%M:%S")
        # Print without newline — timing will be appended on the same line when done
        src_folder_link = _osc8_link(dirpath).replace(dirpath, "📁")
        sys.stdout.write(f"{_ts} |   {prefix} {dim_str} {src_folder_link} {linked_path}")
        sys.stdout.flush()
        # Log is written only after completion (with timing) — not here

        try:
            ENGINE.upscale(local_path, out_path, compute_seedvr2_resolution(w, h))

            img_elapsed   = time.time() - img_start
            grand_elapsed = time.time() - grand_start - pause.paused_seconds
            timing          = f" | {fmt_mmss(img_elapsed)} | Total: {fmt_hhmmss(grand_elapsed)}"
            out_full        = os.path.join(output_dir, out_name)
            out_folder_link = _osc8_link(output_dir).replace(output_dir, "📁")
            checkmark       = _osc8_link(out_full).replace(out_full, "✓")
            sys.stdout.write(f" {out_folder_link} {checkmark}{timing}\n")
            sys.stdout.flush()
            logger.log_only(f"  {prefix} {dim_str}  {local_path} 📁 ✓{timing}", timestamp=True)

            consecutive_failures = 0
            processed_paths.add(local_path)
            if cache is not None:
                cache.mark_done(local_path)
                cache.record_lineage(local_path, out_path)
                cache.save()
            folder_stats[dirpath]["processed"] += 1
            total_processed += 1
            # GUI ETA: pause-excluded elapsed, images actually processed this
            # session, current position and total. The GUI averages over the
            # processed count (NOT the counter, which also advances on skips).
            _gui_event("ETA", f"{grand_elapsed:.3f}|{total_processed}|{idx}|{total}")

        except Exception as e:
            img_elapsed        = time.time() - img_start
            grand_elapsed      = time.time() - grand_start - pause.paused_seconds
            consecutive_failures += 1
            timing = f" | FAILED {fmt_mmss(img_elapsed)} | Total: {fmt_hhmmss(grand_elapsed)} -- {e}"
            sys.stdout.write(timing + "\n")
            sys.stdout.flush()
            logger.log_only(f"  {prefix} {dim_str}  {local_path}{timing}", timestamp=True)
            folder_stats[dirpath]["failed"] += 1
            total_failed += 1

            if consecutive_failures >= OUTAGE_THRESHOLD:
                outage_msg = (
                    f"{consecutive_failures} consecutive image(s) failed. "
                    f"The GPU pipeline may be in a bad state (out of VRAM, "
                    f"driver issue) or these files may be unreadable.\n"
                    f"Last error: {e}"
                )
                logger.tee(f"  WARNING: OUTAGE DETECTED ({consecutive_failures} consecutive failures) -- pausing.")
                logger.tee(f"  Check the log / free up VRAM, then press Space or P to resume.")

                send_discord_notification(
                    title       = "Upscale Script -- Repeated Failures Detected",
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

    # Persist any cache changes from this pass — including already-done marks
    # recorded while skipping, and especially when the run ends with a Stop
    # (per-image saves only cover images that were actually processed).
    if cache is not None:
        cache.save()

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
        "corrupt_files":         corrupt_files,
        "user_quit":             pause._quit if hasattr(pause, '_quit') else False,
    }


# ─────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────

def _clean_path_arg(p):
    """
    Sanitize a path passed on the command line or typed at a prompt.

    PowerShell/cmd treat a trailing backslash before a closing quote as an
    escape, so  "D:\\photos\\23 August 2005\\"  arrives as
    D:\\photos\\23 August 2005"  (literal trailing quote). Copy-pasted paths
    may also keep their surrounding quotes. Strip both before resolving.
    """
    p = p.strip().strip('"').strip("'")
    # A bare drive letter ("D:") would resolve to that drive's current
    # directory — restore the root backslash instead.
    if len(p) == 2 and p[1] == ":":
        p += "\\"
    return p


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

    root = os.path.abspath(_clean_path_arg(sys.argv[1]))
    if not os.path.isdir(root):
        print(f"ERROR: Source directory not found: '{root}'")
        sys.exit(1)

    # ── Determine output root ────────────────────────────────────────────────
    default_output = os.path.join(root, "__upscaled__")

    if len(sys.argv) >= 3:
        # Output path provided as second argument (empty string = default)
        out_arg     = _clean_path_arg(sys.argv[2])
        output_root = os.path.abspath(out_arg) if out_arg else default_output
        print(f"Output directory: {output_root}")
    else:
        # Prompt with default
        print(f"")
        print(f"  Output directory for upscaled images.")
        print(f"  Press Enter to use default, or type an absolute path:")
        print(f"  Default: {default_output}")
        print(f"")
        user_input = _clean_path_arg(input("  Output path: "))
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

    # ── Initialize the SeedVR2 engine (replaces the ComfyUI server) ──────────
    print("")
    print("  Loading SeedVR2 engine (first run may download model weights) ...")
    _gui_event("STATUS", "Loading the AI engine …")
    global ENGINE
    try:
        from upscale_engine import UpscaleEngine
        # debug=true in config.json's "upscale" section restores the verbose
        # SeedVR2 pipeline output in the terminal.
        ENGINE = UpscaleEngine(SEEDVR2_REPO_DIR, SEEDVR2_MODEL_DIR, _U,
                               debug=bool(_U.get("debug", False)))
    except Exception as e:
        print(f"ERROR: Could not initialize the SeedVR2 engine.\n  -> {e}")
        print("Make sure this script runs inside the toolbox venv, e.g.:")
        print(f"  {os.path.join(_SCRIPT_DIR, '.venv', 'Scripts', 'python.exe')} batch_upscale.py <source_dir>")
        send_discord_notification(
            title       = "Upscale Script -- Engine Failed to Start",
            description = f"Could not initialize the SeedVR2 engine.\n{e}",
            color       = 15548997,   # red
            fields      = [{"name": "Machine", "value": os.environ.get("COMPUTERNAME", "unknown")}],
        )
        sys.exit(1)
    print(f"  Engine ready on {ENGINE.device_name}.")

    # Created before the long preparation phases so a quit request ("q" from
    # the GUI, or Q on the keyboard) can cancel scanning / eligibility checks.
    pause = PauseController()

    def _cancelled():
        return pause.quit_requested

    logger = Logger(root)
    # SeedVR2 pipeline output (phase banners, progress bars) goes to the log
    # file only — the terminal shows just the per-image status lines.
    ENGINE.log_sink = logger._fh
    _gui_event("LOG", logger.path)
    # Print log path to terminal only — it's already written to the session header
    log_link = _osc8_link(logger.path)
    print(f"Log file: {log_link}")
    logger.tee(f"Source:   {root}")
    logger.tee(f"Output:   {output_root}")
    logger.tee(f"Cutoff:   {UPSCALE_CUTOFF_PCT}% (skip images >= {UPSCALE_CUTOFF_PCT}% of target resolution)")
    logger.tee(f"Scanning '{root}' recursively ...")
    _gui_event("STATUS", "Scanning for images …")
    all_items, total_folders = collect_work_items(root, output_root, abort_check=_cancelled)

    if pause.quit_requested:
        logger.tee("  Cancelled at user request during scanning.")
        logger.close()
        sys.exit(0)

    if not all_items:
        logger.tee("No images found.")
        sys.exit(0)

    logger.tee(f"Found {total_folders} folder(s) and {len(all_items)} image(s).")

    # ── Load eligibility cache ────────────────────────────────────────────────
    cache = EligibilityCache(root, output_root)
    if cache.entry_count > 0:
        logger.tee(f"Loaded eligibility cache: {cache.entry_count} entries ({cache.path})")
        print("  Verifying cached entries against disk (may take a moment on network drives) ...", flush=True)
        _gui_event("STATUS", "Verifying cached entries …")

        _last_folder = [""]
        _folder_idx  = [0]
        _verify_start = time.time()      # for the GUI "time remaining" estimate
        def _verify_progress(folder):
            if folder != _last_folder[0]:
                _last_folder[0] = folder
                _folder_idx[0] += 1
                pct     = int(_folder_idx[0] / max(total_folders, 1) * 100)
                display = f"{pct}% ({_folder_idx[0]}/{total_folders}) | {folder}"
                _tw = _terminal_width()
                sys.stdout.write(display[:_tw].ljust(_tw) + chr(13))
                sys.stdout.flush()
                _gui_event("PROG", f"{_folder_idx[0]}|{total_folders}")
                # Feed the GUI's ETA handler (elapsed|processed|idx|total);
                # folders are verified at a roughly uniform rate, so
                # processed == idx.
                _gui_event("ETA", f"{time.time() - _verify_start:.3f}|"
                                  f"{_folder_idx[0]}|{_folder_idx[0]}|{total_folders}")

        removed = cache.remove_missing(root, progress_cb=_verify_progress,
                                       abort_check=_cancelled)
        _tw = _terminal_width(); sys.stdout.write(" " * _tw + chr(13)); sys.stdout.flush()  # clear the folder line
        if pause.quit_requested:
            logger.tee("  Cancelled at user request during cache verification.")
            cache.save()
            logger.close()
            sys.exit(0)
        if removed:
            logger.tee(f"  Removed {removed} stale entries (files no longer on disk).")
        else:
            logger.tee(f"  All {cache.entry_count} cached entries verified.")
    else:
        logger.tee(f"No cache found — full eligibility check required.")

    logger.tee(f"Checking eligibility (this may take a while) ...")
    _gui_event("STATUS", "Checking image eligibility …")

    # ── Eligibility pre-check (cache-aware) ───────────────────────────────────
    work_items     = []
    pre_done       = 0
    pre_too_large  = 0
    cache_hits     = 0
    total_all      = len(all_items)
    elig_start     = time.time()        # for the GUI "time remaining" estimate

    for i, item in enumerate(all_items, 1):
        if pause.quit_requested:
            break

        dirpath, local_path, output_dir, out_name = item
        out_path = os.path.join(output_dir, out_name)

        # Progress indicator every 200 files or on last file
        if i % 200 == 0 or i == total_all:
            pct = int(i / total_all * 100)
            sys.stdout.write("  Checking eligibility ... %d%% (%d/%d) | cache hits: %d    " % (
                pct, i, total_all, cache_hits) + chr(13)); sys.stdout.flush()
            _gui_event("PROG", f"{i}|{total_all}")
            # Reuse the GUI's ETA handler (elapsed|processed|idx|total): every
            # item is examined at a roughly uniform rate, so processed == idx.
            _gui_event("ETA", f"{time.time() - elig_start:.3f}|{i}|{i}|{total_all}")

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
    cache.save()   # keep results checked so far — also on cancellation

    if pause.quit_requested:
        logger.tee("  Cancelled at user request during eligibility check.")
        logger.tee("  (Results checked so far were saved to the cache.)")
        logger.close()
        sys.exit(0)

    logger.tee(f"Found {len(work_items)} eligible file(s) "
               f"({pre_done} already done, {pre_too_large} too large — "
               f"{cache_hits}/{total_all} from cache).")

    if not work_items:
        logger.tee("Nothing to process.")
        sys.exit(0)

    if pause.available and not GUI_MODE:
        logger.tee("  Keyboard control active: Space/P = pause, Q = quit after current image.")
    logger.tee("")
    logger.tee("Processing ...")
    _gui_event("STATUS", "Processing images …")

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
        _gui_event("STATUS", "Rescanning for new or renamed files …")
        rescan_items, _ = collect_work_items(root, output_root,
                                             already_done=processed_paths,
                                             abort_check=_cancelled,
                                             status_prefix="Rescanning for new or renamed files …")

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
            _gui_event("STATUS", "Processing new items …")
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
            "corrupt_files":         s1.get("corrupt_files", []) + s2.get("corrupt_files", []),
        }

    combined        = merge(stats1, stats2)
    folder_stats    = combined["folder_stats"]
    total_processed       = combined["total_processed"]
    total_skipped_done    = combined["total_skipped_done"]
    total_skipped_size    = combined["total_skipped_size"]
    total_skipped_missing = combined["total_skipped_missing"]
    total_skipped_corrupt = combined["total_skipped_corrupt"]
    total_failed          = combined["total_failed"]
    corrupt_files         = combined.get("corrupt_files", [])

    col_path = min(60, max(len("Folder"),
        max((len(os.path.relpath(p, root)) for p in folder_stats), default=6)))
    col_totl = len("Total")
    col_proc = len("Processed")
    col_skip = len("Skipped")
    col_corr = len("Corrupt")
    col_fail = len("Failed")
    col_time = max(len("Elapsed"),
        max((len(fmt_duration(v["elapsed"])) for v in folder_stats.values()), default=7))

    def trunc(s, n):
        return s if len(s) <= n else "..." + s[-(n - 3):]

    _w = col_path + col_totl + col_proc + col_skip + col_corr + col_fail + col_time + 24
    sep = "=" * _w
    row = (f"  {{:<{col_path}}}  {{:>{col_totl}}}  {{:>{col_proc}}}  {{:>{col_skip}}}"
           f"  {{:>{col_corr}}}  {{:>{col_fail}}}  {{:>{col_time}}}")

    logger.tee("")
    logger.tee(sep)
    logger.tee(row.format("Folder", "Total", "Processed", "Skipped", "Corrupt", "Failed", "Elapsed"))
    logger.tee("-" * _w)

    for dp, stats in folder_stats.items():
        rel = os.path.relpath(dp, root) if dp != root else "."
        total_skipped_folder = stats["skipped_done"] + stats["skipped_size"]
        folder_total = (stats["processed"] + total_skipped_folder +
                        stats["skipped_corrupt"] + stats["skipped_missing"] +
                        stats["failed"])
        rel_truncated = trunc(rel, col_path)
        plain_row = row.format(rel_truncated, folder_total,
                               stats["processed"], total_skipped_folder,
                               stats["skipped_corrupt"], stats["failed"],
                               fmt_duration(stats["elapsed"]))
        # Terminal: replace the plain folder name with a clickable link
        linked_folder = _osc8_link(dp).replace(dp, rel_truncated)
        term_row = plain_row.replace(rel_truncated, linked_folder, 1)
        logger.log_only(plain_row)
        print(term_row)

    logger.tee("=" * _w)
    total_skipped = total_skipped_done + total_skipped_size
    grand_total = total_processed + total_skipped + total_skipped_corrupt + total_skipped_missing + total_failed
    logger.tee(row.format("TOTAL", grand_total, total_processed, total_skipped,
                          total_skipped_corrupt, total_failed, fmt_hhmmss(grand_elapsed)))
    logger.tee(sep)
    parts = [f"{total_processed} processed", f"{total_skipped_done} already done",
             f"{total_skipped_size} too large"]
    if total_skipped_missing > 0: parts.append(f"{total_skipped_missing} missing")
    if total_skipped_corrupt > 0: parts.append(f"{total_skipped_corrupt} corrupted")
    if total_failed          > 0: parts.append(f"{total_failed} failed")
    else: parts.append("0 failed")
    logger.tee(f"  ({', '.join(parts)})")

    # List every corrupt/unreadable file so the user can find and review them.
    corrupt_files = list(dict.fromkeys(corrupt_files))   # dedup, keep order
    if corrupt_files:
        logger.tee("")
        logger.tee(sep)
        logger.tee(f"  Corrupted / unreadable images ({len(corrupt_files)}) — review or recover manually:")
        logger.tee("-" * _w)
        for cf in corrupt_files:
            logger.log_only(f"  {cf}")
            print(f"  {_osc8_link(cf)}")     # terminal: clickable; GUI strips the link
        logger.tee(sep)

    # ── Discord: queue finished / stopped ───────────────────────────────────
    user_quit = bool(stats1.get("user_quit")) or (stats2 is not None and bool(stats2.get("user_quit")))

    # GUI: machine-readable run summary (drives the MQTT last_run topic).
    _gui_event("DONE", json.dumps({
        "tool":            "upscale",
        "processed":       total_processed,
        "skipped":         total_skipped + total_skipped_missing + total_skipped_corrupt,
        "corrupt":         total_skipped_corrupt,
        "failed":          total_failed,
        "elapsed_seconds": round(grand_elapsed, 1),
        "stopped_by_user": user_quit,
    }))
    if total_failed > 0:
        notif_title, notif_color = "Upscale Queue -- Finished with Failures", 16776960   # yellow
    elif user_quit:
        notif_title, notif_color = "Upscale Queue -- Stopped by User", 16776960          # yellow
    else:
        notif_title, notif_color = "Upscale Queue -- Finished", 3066993                  # green
    send_discord_notification(
        title       = notif_title,
        description = ", ".join(parts),
        color       = notif_color,
        fields      = [
            {"name": "Source",        "value": root},
            {"name": "Total elapsed", "value": fmt_hhmmss(grand_elapsed)},
            {"name": "Machine",       "value": os.environ.get("COMPUTERNAME", "unknown")},
        ],
    )

    _log_link = _osc8_link(logger.path)
    logger.log_only(f"Session ended: {datetime.datetime.now().strftime('%Y-%m-%d | %H:%M:%S')}")
    print(f"Log file: {_log_link}")
    logger.close()


if __name__ == "__main__":
    main()
