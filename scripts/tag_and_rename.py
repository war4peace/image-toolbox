"""
tag_and_rename.py
-----------------
Analyses images using a local Ollama vision model, writes a long description
into EXIF ImageDescription (UTF-8) and the Windows XPTitle field, stores the
original filename in EXIF XPComment, writes a processing timestamp into EXIF
UserComment (used to skip already-processed files on re-run), and renames each
file to:

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
  - The shared SQLite cache (db/cache.db, tag_files table) records the original
    filename and EXIF state of every scanned file before any modification is made.
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
import base64
import hashlib
import unicodedata
import urllib.request
import urllib.error
import traceback
import threading
from collections import defaultdict

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
import notifications
import runner_common

# Fail-safe diagnostic trail for the swallowed-error handlers (guarded import).
try:
    from debug_log import debug_log
except Exception:
    def debug_log(*_a, **_k):
        pass

# Make stdout/stderr non-ASCII-proof before any output (see runner_common). Done
# before the session-log tee wraps stdout, so the tee inherits the utf-8 stream.
runner_common.harden_stdout()

# App root = parent of scripts/. config.json, logs/ and db/cache.db live at the
# app root, not beside this module.
APP_ROOT = runner_common.APP_ROOT


# ─────────────────────────────────────────────────────────────
#  GUI INTEGRATION  (event lines + session log)
# ─────────────────────────────────────────────────────────────

# The @@TBX@@ event protocol + GUI-mode detection live in runner_common; its
# gui_event targets stdout's raw stream, so markers still bypass the session-log
# tee this runner installs later. The event kinds this runner emits: IMG|<path>,
# QUEUE|<json>, LOG|<path>, RENAME|<json> ([old, new]), REFRESH|<path> (pixels
# changed - re-decode the thumb), RESULT|<json> ([final_path, "ok"|"fail"]).
_stdin_is_piped = runner_common.stdin_is_piped
GUI_MODE        = runner_common.GUI_MODE
GUI_MARKER      = runner_common.GUI_MARKER
_gui_event      = runner_common.gui_event


class _TeeOutput:
    """Mirrors everything written to stdout into the session log file. Each line
    in the FILE is prefixed with a wall-clock timestamp (0.3.9) so the on-disk
    log can reconstruct run timing; stdout itself is untouched (the GUI window
    adds its own per-line timestamp, and markers bypass the file entirely)."""

    def __init__(self, stream, fh):
        self.raw = stream     # _gui_event writes markers here, bypassing the log
        self._fh = fh
        self._fh_line_start = True   # next file character begins a fresh line

    def write(self, data):
        self.raw.write(data)
        try:
            self._fh.write(self._stamp_for_file(data))
        except Exception:
            pass

    def _stamp_for_file(self, data):
        """Insert a '<date> | <time> | ' prefix at the start of each new line of
        `data` for the session log. \\r is passed through; one timestamp per
        write() call is shared by the lines in that chunk (they arrive together)."""
        if not data:
            return data
        ts = time.strftime("%Y-%m-%d | %H:%M:%S")
        out = []
        for part in re.split("(\n)", data):
            if part == "\n":
                out.append("\n")
                self._fh_line_start = True
            elif part:
                if self._fh_line_start:
                    out.append(f"{ts} | ")
                    self._fh_line_start = False
                out.append(part)
        return "".join(out)

    def flush(self):
        self.raw.flush()
        try:
            self._fh.flush()
        except Exception:
            pass

    def isatty(self):
        return self.raw.isatty()


def _setup_session_log(root):
    """
    Mirror all terminal output into  logs/tag_<12-char hash of root>.log
    (append mode, one file per source folder — same scheme as the upscaler).
    Returns the log file path.
    """
    norm    = os.path.normcase(os.path.abspath(root))
    digest  = hashlib.sha256(norm.encode("utf-8")).hexdigest()[:12]
    log_dir = os.path.join(APP_ROOT, "logs")
    os.makedirs(log_dir, exist_ok=True)
    path = os.path.join(log_dir, f"tag_{digest}.log")
    fh = open(path, "a", encoding="utf-8", buffering=1)
    fh.write(f"\n{'=' * 64}\n")
    fh.write(f"Session started: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
    fh.write(f"Source: {root}\n")
    fh.write(f"{'=' * 64}\n")
    sys.stdout = _TeeOutput(sys.stdout, fh)
    return path


# ─────────────────────────────────────────────────────────────
#  REMOTE CONTROL  (GUI integration)
# ─────────────────────────────────────────────────────────────

class RemoteControl:
    """
    Line-based control over piped stdin, used when this script runs as a
    child of toolbox_gui.py (stdin is a pipe, not a console):

        "q"            → stop gracefully after the current image
        "p"            → the GUI's dual-purpose button (see below)
        any other line → resume after an outage pause

    The dual button: the tab shows ONE button that is "Pause" during normal
    work and "Resume" while an outage holds the run open. Those two states are
    mutually exclusive in this loop — it is either tagging an image or blocked
    in wait_resume(), never both — so one button can serve both.

    Crucially "p" does not carry the meaning; THIS class decides it, from the
    state only it knows for sure. The GUI's label is a replica that lags the
    run by one pipe hop, so a click can land in the window where the run just
    entered an outage but the button still reads "Pause". Letting the button
    dictate ("pause now") would make that click do the wrong thing; letting the
    runner interpret it ("the user pressed the button") cannot, because
    _waiting_resume is true exactly when the meaning is "resume".

    Inactive (no thread, no stdin reads) when stdin is an interactive
    console, so normal terminal usage is unchanged.
    """

    def __init__(self):
        self.active  = False
        self._stop   = threading.Event()
        self._resume = threading.Event()
        self._lock   = threading.Lock()
        self._paused = False      # user pause, between images
        self._waiting_resume = False   # True while wait_resume() is blocked
        try:
            if sys.stdin is not None and not sys.stdin.isatty():
                self.active = True
                threading.Thread(target=self._watch, daemon=True).start()
        except Exception:
            pass

    def _watch(self):
        try:
            for line in sys.stdin:
                cmd = line.strip().lower()
                if cmd in ("q", "quit"):
                    self._stop.set()
                elif cmd == "qstop":          # quit + stop the remote pod now
                    _set_remote_teardown("stop")
                    self._stop.set()
                elif cmd == "qkeep":          # quit but leave the remote pod running
                    _set_remote_teardown("keep")
                    self._stop.set()
                elif cmd in ("p", "pause"):
                    self._dual_button()
                else:
                    self._resume.set()
        except Exception:
            pass
        # EOF — the controlling process is gone; stop gracefully.
        self._stop.set()
        self._resume.set()

    def _dual_button(self):
        """Apply a press of the GUI's dual Pause / Resume button. An outage wait
        wins: there, the only sensible meaning is 'continue'."""
        with self._lock:
            if self._waiting_resume:
                self._resume.set()
                return
            self._paused = not self._paused

    @property
    def stop_requested(self):
        return self._stop.is_set()

    @property
    def paused(self):
        with self._lock:
            return self._paused

    def wait_resume(self):
        """Block until a resume line arrives. Returns False if stop instead."""
        self._resume.clear()
        with self._lock:
            self._waiting_resume = True
            # An outage supersedes a user pause: the run is already held open,
            # and the same button now means 'continue'. Clearing this here stops
            # a pause taken just before the outage from re-blocking the loop the
            # moment the user resolves the outage.
            self._paused = False
        try:
            while not self._resume.wait(0.5):
                if self._stop.is_set():
                    return False
            return not self._stop.is_set()
        finally:
            with self._lock:
                self._waiting_resume = False

    def wait_while_paused(self, on_pause=None, on_resume=None):
        """
        Call between images. Blocks while the user has paused the run, firing
        on_pause once it goes to sleep and on_resume when it wakes (that is where
        the vision model is unloaded, so a pause actually frees the GPU).

        Returns True to continue, False if a stop was requested meanwhile.
        Hooks run on the CALLING thread, where no tagging request is in flight.
        """
        if not self.paused:
            return True
        _fire(on_pause)
        try:
            while self.paused:
                if self._stop.is_set():
                    return False
                time.sleep(0.25)
            return not self._stop.is_set()
        finally:
            if not self._stop.is_set():
                _fire(on_resume)


def _fire(hook):
    """Run a pause hook, never letting it break the run."""
    if hook is None:
        return
    try:
        hook()
    except Exception as exc:                       # noqa: BLE001
        debug_log("tag_and_rename pause hook failed", exc=exc)


# ─────────────────────────────────────────────────────────────
#  CONFIG  –  loaded from config.json
# ─────────────────────────────────────────────────────────────

# config.json load lives in runner_common (shared by every runner).
_load_config = runner_common.load_config

_CFG = _load_config()
_O   = _CFG.get("ollama",  {})
_T   = _CFG.get("tagging", {})

OLLAMA_URL   = _O.get("url",   "http://127.0.0.1:11434")
OLLAMA_MODEL = _O.get("model", "qwen2.5vl:7b")

# Remote Tag & Rename (#1): the GUI sets IMGTBX_TAG_REMOTE=1 to run Ollama (and
# the auto-straighten CNN) on a rented RunPod pod. tag_and_rename still runs
# LOCALLY (it reads/writes the local files and does EXIF/rename) — only the model
# calls go over an ssh tunnel: OLLAMA_URL is repointed at the tunnel and the
# orientation CNN is called on the pod via REMOTE_ORIENT. Set up in main().
REMOTE        = os.environ.get("IMGTBX_TAG_REMOTE") == "1"
REMOTE_ORIENT = None      # pod analyse(path) -> (degrees, confidence) when remote
REMOTE_SESSION = None      # the RemoteSession (set in main) — to detect a pod stop
_REMOTE_TEARDOWN = None    # "stop"/"keep"/None — the GUI Stop modal's choice


def _set_remote_teardown(value):
    global _REMOTE_TEARDOWN
    _REMOTE_TEARDOWN = value


def _remote_pod_stopped():
    """See runner_common.remote_pod_stopped: True if this run's remote tag pod is
    no longer RUNNING (dead-man's switch fired / stopped), so a failure ends the
    run cleanly (already-tagged files are skipped on re-run) instead of pausing on
    what looks like an Ollama outage."""
    return runner_common.remote_pod_stopped(REMOTE_SESSION)

MIN_WIDTH       = _T.get("min_width",       3840)
MIN_HEIGHT      = _T.get("min_height",      2160)
UPSCALED_SUBDIR = _T.get("upscaled_subdir", "upscaled")
# `.gif` is deliberately ABSENT (#27), the same call RAW gets (#19): this tool
# writes a description into the file's own metadata, and GIF has nowhere to put
# one. Nothing is lost by it, because the documented workflow points this tab at
# the UPSCALED folder, where a GIF has already become `<stem>_gif.png` and is
# tagged like any other PNG.
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
# Cap the longest edge of the image SENT TO THE VISION MODEL. A full-res photo
# (e.g. 2272x1704) makes qwen2.5vl emit a huge number of vision tokens, which
# OOMs a small-VRAM GPU and makes Ollama answer HTTP 400 — every ≤24 GB remote
# card crashed on the first large image until this was added. Downscaling to
# ~1280 px (the size that always worked) fixes it and speeds up tagging on every
# GPU, with no loss for describe-and-title use (we don't OCR). 0 = send full res.
# The SOURCE FILE IS NEVER TOUCHED — only the in-memory copy sent to the model.
TAG_MAX_IMAGE_PX    = int(_T.get("max_image_px",    1280))

# Cap the Ollama context window (KV cache) for the tagging call. Newer vision
# models ship a very large native context (qwen3-vl declares 256K), and Ollama
# sizes the KV cache off that declared context, so it grabs almost the whole
# card (measured: a 6.1 GB q4 qwen3-vl:8b pinned a 24 GB 3090 at 98% VRAM, which
# thrashes into system RAM and slows the run). Our actual need is tiny: the image
# is downscaled to TAG_MAX_IMAGE_PX and the reply is capped at 120 tokens, so a
# small context loses nothing for describe-and-title. 0 = don't set num_ctx (let
# the model/Ollama default stand).
TAG_NUM_CTX         = int(_T.get("ollama_num_ctx",  8192))

# Auto-straighten: detect sideways photos with a small CNN and rotate them
# upright before tagging. Only confident 90/270 calls are acted on (see
# orientation.py); 180 and low-confidence calls are left alone and logged.
AUTO_STRAIGHTEN        = bool(_T.get("auto_straighten", True))
STRAIGHTEN_CONFIDENCE  = float(_T.get("straighten_min_confidence", 0.9))
PROCESSED_MARKER    = "TaggedBy:Image Toolbox (https://github.com/war4peace/image-toolbox)"
# Marker written by older versions — still recognised so photos tagged before
# the rebrand are not re-processed after an upgrade.
LEGACY_MARKERS      = ("TaggedBy:tag_and_rename",)

# Notification backends (Discord webhook + Telegram bot) live in the
# "notifications" section of config.json; resolve_settings() also reads the legacy
# upscale.discord_webhook_url location for backward compatibility.
NOTIFY = notifications.resolve_settings(_CFG)


def send_notification(title, description, color, fields=None):
    """
    Fan out an alert to every configured backend (Discord webhook, Telegram bot).
    No-op for any backend that isn't configured; fail-safe.
    color: a notifications.COLOR_* severity constant (never a raw int).
    """
    notifications.notify(NOTIFY, title, description, color, fields, username="Tag & Rename Bot")


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

# The tag/rename cache lives in the shared SQLite db (db/cache.db, tag_files
# table); see db.py. The legacy per-folder trcache/<md5>.cache JSON files are
# read only once, on first DB creation, for the one-time import (in db.py) and are
# otherwise vestigial. This version stamps each entry so a schema change can be
# detected on load.
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


# The EXIF fields this script writes – all are tracked for undo.
_TRACKED_EXIF_FIELDS = {
    "ImageDescription": ("0th",  270),    # piexif.ImageIFD.ImageDescription (UTF-8)
    "XPTitle":          ("0th",  40091),  # Windows XP Title (UTF-16LE) — item 3
    "XPComment":        ("0th",  40092),  # Windows XP Comment (UTF-16LE)
    "UserComment":      ("Exif", 37510),  # piexif.ExifIFD.UserComment
}


# ─────────────────────────────────────────────────────────────
#  TIMING HELPERS
# ─────────────────────────────────────────────────────────────

# Duration formatting lives in runner_common (shared).
fmt_mmss     = runner_common.fmt_mmss
fmt_hhmmss   = runner_common.fmt_hhmmss
fmt_duration = runner_common.fmt_duration


# Header-based image-size reader (with a Pillow fallback) lives in runner_common,
# shared with the other runners.
get_image_dimensions = runner_common.get_image_dimensions


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

def _ollama_model_available(models):
    """True if OLLAMA_MODEL is present in `models` (a list of names from /api/tags
    or /api/ps). Matches on the model name up to the ':tag' boundary, NOT as a
    loose substring: a configured 'llava' matches 'llava:latest' but not
    'llava-phi3', and 'qwen2.5vl' does not match a future 'qwen2.5vl-max'. The old
    `base in m` substring test let those through, so the pre-flight could pass
    while /api/generate then failed with 'model not found' and burned the outage
    path instead of the clean startup error. See item 11."""
    base = OLLAMA_MODEL.split(":")[0]
    return any(m == base or m.startswith(base + ":") for m in models)


def check_ollama():
    """Return (ok: bool, message: str)."""
    try:
        req = urllib.request.Request(f"{OLLAMA_URL}/api/tags")
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
        models = [m["name"] for m in data.get("models", [])]
        found  = _ollama_model_available(models)
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


def unload_model():
    """
    Ask Ollama to unload the vision model immediately (keep_alive=0),
    releasing VRAM for other workloads. Best-effort and quiet.
    Checks /api/ps first so the call can never trigger a pointless load
    of a model that is not in memory.
    Returns True if an unload was actually performed.
    """
    try:
        with urllib.request.urlopen(f"{OLLAMA_URL}/api/ps", timeout=5) as resp:
            loaded = [m.get("name", "") for m in json.loads(resp.read()).get("models", [])]
        if not _ollama_model_available(loaded):
            return False
        payload = json.dumps({"model": OLLAMA_MODEL, "keep_alive": 0}).encode("utf-8")
        req = urllib.request.Request(
            f"{OLLAMA_URL}/api/generate", data=payload,
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=30)
        return True
    except Exception:
        return False


def _encode_image_for_model(path):
    """Return the base64 image to send to the vision model.

    Downscales the longest edge to TAG_MAX_IMAGE_PX (in memory) so a large photo
    can't OOM a small-VRAM GPU into an HTTP 400, and applies EXIF orientation so
    the model sees the picture upright. Fail-safe: any problem (Pillow missing,
    odd format, already small, or downscaling disabled) falls back to the raw file
    bytes — tagging must never break because of the resize. The source file on
    disk is never modified."""
    if TAG_MAX_IMAGE_PX and TAG_MAX_IMAGE_PX > 0:
        try:
            import io
            from PIL import Image, ImageOps
            with Image.open(path) as im:
                im = ImageOps.exif_transpose(im)       # honour orientation
                if max(im.size) > TAG_MAX_IMAGE_PX:
                    im.thumbnail((TAG_MAX_IMAGE_PX, TAG_MAX_IMAGE_PX),
                                 Image.LANCZOS)
                if im.mode not in ("RGB", "L"):
                    im = im.convert("RGB")
                buf = io.BytesIO()
                im.save(buf, format="JPEG", quality=90)
                return base64.b64encode(buf.getvalue()).decode("ascii")
        except Exception:                              # noqa: BLE001 (fail-safe)
            pass                                       # fall back to raw bytes
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("ascii")


def analyse_image(path, language="English"):
    """
    Send the image to Ollama and return (long_description, condensed_title).
    language controls the language of LINE 1 (the EXIF description).
    LINE 2 (the filename title) is always in English so filenames stay ASCII-safe.
    Raises RuntimeError on failure.
    """
    img_b64 = _encode_image_for_model(path)

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

    options = {"temperature": 0.2, "num_predict": 120}
    if TAG_NUM_CTX and TAG_NUM_CTX > 0:
        options["num_ctx"] = TAG_NUM_CTX      # cap the KV cache, see TAG_NUM_CTX

    payload = json.dumps({
        "model":   OLLAMA_MODEL,
        "prompt":  prompt,
        "images":  [img_b64],
        "stream":  False,
        "options": options,
    }).encode("utf-8")

    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/generate",
        data    = payload,
        headers = {"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=OLLAMA_TIMEOUT) as resp:
            result = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        # Surface Ollama's ACTUAL error. A 500's response body carries the real
        # cause (model-runner crash, CUDA "no kernel image" on an unsupported
        # arch, OOM, corrupt model) — the bare "HTTP Error 500: Internal Server
        # Error" hides it, which sent us chasing the wrong thing (a "venv" issue).
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace").strip()
        except Exception:                              # noqa: BLE001 (best-effort)
            pass
        raise RuntimeError(
            f"Ollama HTTP {exc.code} from /api/generate"
            + (f": {detail}" if detail else f" ({exc.reason})")) from exc

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


# Formats where embedding EXIF is safe AND reversible: Pillow re-saves keeping the
# container, piexif reads the block back (so the skip-on-rerun marker round-trips)
# and the undo snapshot/restore path works. JPEG is the only one that clears all
# three: piexif can't read PNG/TIFF EXIF back (the marker would never round-trip and
# undo couldn't strip it), and although WebP round-trips it's left out to keep one
# simple rule. Non-JPEG files are still RENAMED (format-agnostic) and their
# skip-on-rerun marker lives in the cache ("processed" status) instead of EXIF.
_EXIF_WRITABLE_FORMATS = {"JPEG", "MPO"}


def _image_format(path):
    """The image's true container format (Pillow name, upper-case), read from the
    file content rather than the extension. None if it can't be opened. Used to
    decide whether EXIF can be embedded safely — the extension can lie (an upscaled
    tree can hold a .png that is really a PNG, never a JPEG)."""
    try:
        from PIL import Image
        with Image.open(path) as im:
            return (im.format or "").upper()
    except Exception:
        return None


def _exif_writable(path):
    """True only for formats where embedding EXIF is safe and reversible (JPEG)."""
    return _image_format(path) in _EXIF_WRITABLE_FORMATS


def _save_with_exif(path, exif_dict):
    """
    Embed EXIF into a JPEG.

    Preferred path is piexif.insert(): it rewrites ONLY the APP1/EXIF segment and
    leaves the compressed pixel data byte-for-byte untouched — no generation loss,
    minimal I/O, and safe to repeat any number of times (re-tagging never degrades
    the image). This is what makes tagging effectively lossless (item 6). It works
    even on a bare JPEG with no existing APP0/APP1 markers (e.g. ComfyUI SaveImage
    output).

    Fallback is a full Pillow re-encode at quality=95 (near-lossless), used only if
    insert() can't patch the file in place (a malformed/unusual JPEG). It is one
    re-encode, not two — the old code re-encoded once for the description and again
    for the processed marker; those are now a single write.

    JPEG-ONLY BY CONTRACT: callers gate on _exif_writable(). The guard below makes
    it impossible to ever save "jpeg" bytes over a .png/.webp/.tif (which would
    corrupt the file or raise on an RGBA image) even if a future caller forgets.
    """
    import piexif
    from PIL import Image

    fmt = _image_format(path)
    if fmt not in _EXIF_WRITABLE_FORMATS:
        raise ValueError(
            f"refusing to write JPEG EXIF into a {fmt or 'non-JPEG'} file "
            f"(would corrupt it): {path}")
    exif_bytes = piexif.dump(exif_dict)
    try:
        piexif.insert(exif_bytes, path)   # lossless: touches only the EXIF segment
        return
    except Exception as exc:
        # Rare: an odd JPEG insert() can't patch. Fall back to a single re-encode.
        debug_log("tag_and_rename._save_with_exif: insert failed, re-encoding", exc=exc)
    img = Image.open(path)
    img.save(path, "jpeg", exif=exif_bytes, quality=95, subsampling=0)


def write_exif(path, long_description, original_filename):
    """
    Write ALL of the tag's EXIF in ONE save (item 6): the description to
    ImageDescription (0th IFD tag 270, UTF-8) mirrored into XPTitle (tag 40091,
    UTF-16LE); original_filename to XPComment (tag 40092, UTF-16LE); and the
    processed-marker timestamp to Exif.UserComment (the skip-on-rerun signal).

    This used to be two calls (write_exif then write_processed_marker), each doing
    its own JPEG save — two generations of loss and double the I/O per image. They
    are now one dict and one _save_with_exif call, and that save is itself lossless
    (piexif.insert, see _save_with_exif), so tagging no longer degrades the pixels
    at all.

    The description is written as UTF-8 bytes, NOT ascii/replace — the selectable
    description language means a Romanian/German/etc. description is the flagship
    output, and ascii-replacing it turned every diacritic into "?" in the one
    place it is persisted ("Pisică" -> "Pisic?"). UTF-8 in ImageDescription is
    read correctly by essentially every modern viewer; XPTitle (UTF-16LE) is the
    Windows-native Unicode field Explorer shows as "Title", so the full accented
    text also appears in the shell. See item 3. (Filenames stay ASCII-sanitised
    on purpose — that is elsewhere and unaffected.)

    JPEG only: for PNG/WebP/TIFF, embedding EXIF would corrupt the container or
    fail to round-trip, so the EXIF write is skipped (the file is left byte-for-
    byte untouched) and the descriptive filename carries the result instead; the
    cache's 'processed' status is then the skip-on-rerun signal (is_already_processed
    consults it). Returns True if EXIF was written, False if skipped. See item 2.
    """
    import piexif

    if not _exif_writable(path):
        return False

    exif_dict = _load_exif_safe(path)

    # ImageDescription (tag 270): UTF-8 bytes, so diacritics survive.
    exif_dict["0th"][piexif.ImageIFD.ImageDescription] = (
        long_description.encode("utf-8")
    )
    # XPTitle (tag 40091): UTF-16LE — the Windows-native Unicode title field.
    exif_dict["0th"][40091] = long_description.encode("utf-16-le")
    # XPComment (tag 40092): UTF-16LE bytes — Windows standard for XP* fields
    exif_dict["0th"][40092] = original_filename.encode("utf-16-le")

    # UserComment: the processed marker + timestamp, folded into this same save.
    timestamp    = time.strftime("%Y-%m-%d %H:%M:%S")
    marker_text  = f"{PROCESSED_MARKER} @ {timestamp}"
    exif_dict.setdefault("Exif", {})[piexif.ExifIFD.UserComment] = (
        _EXIF_ASCII_HEADER + marker_text.encode("ascii")
    )

    _save_with_exif(path, exif_dict)
    return True


def write_processed_marker(path):
    """
    Write ONLY the processed-marker timestamp into Exif.UserComment.

    Kept for callers/tests that want the skip-on-rerun marker on its own; the main
    tag loop no longer calls this, because write_exif now folds the marker into its
    single save (item 6). JPEG only (a non-JPEG carries the 'processed' status in
    the cache instead). Returns True if written, False if skipped. See item 2.
    """
    import piexif

    if not _exif_writable(path):
        return False

    timestamp    = time.strftime("%Y-%m-%d %H:%M:%S")
    marker_text  = f"{PROCESSED_MARKER} @ {timestamp}"
    user_comment = _EXIF_ASCII_HEADER + marker_text.encode("ascii")

    exif_dict = _load_exif_safe(path)
    exif_dict.setdefault("Exif", {})[piexif.ExifIFD.UserComment] = user_comment

    _save_with_exif(path, exif_dict)
    return True


def is_already_processed(path, cache=None, source_root=None):
    """
    Return True if the file has already been tagged by this script.

    Primary signal: the EXIF Exif.UserComment PROCESSED_MARKER (JPEG). Fallback:
    the cache's 'processed' status — the ONLY skip signal for non-JPEG files,
    which carry no EXIF marker (see write_processed_marker / item 2). Pass
    cache+source_root to enable the fallback; without them only EXIF is checked.
    """
    try:
        import piexif
        exif_dict = _load_exif_safe(path)
        raw = exif_dict.get("Exif", {}).get(piexif.ExifIFD.UserComment, b"")
        if raw:
            # Skip the 8-byte charset header, decode remaining ASCII
            text = raw[8:].decode("ascii", "ignore").strip()
            if text.startswith((PROCESSED_MARKER,) + LEGACY_MARKERS):
                return True
    except Exception:
        pass
    if cache is not None and source_root is not None:
        _key, entry = _find_entry(cache, source_root, path)
        if entry is not None and entry.get("status") == "processed":
            return True
    return False


# ─────────────────────────────────────────────────────────────
#  AUTO-STRAIGHTEN
# ─────────────────────────────────────────────────────────────

_STRAIGHTEN_DISABLED = False   # set True after a hard failure, to stop retrying


def warm_up_straighten():
    """Load the orientation model up-front (downloads ~82 MB once) so the cost
    is paid before the queue starts, not mid-first-image. Disables the feature
    cleanly if torch/timm are unavailable."""
    global _STRAIGHTEN_DISABLED
    if not AUTO_STRAIGHTEN:
        return
    if REMOTE:
        # The CNN runs on the pod (REMOTE_ORIENT, set up in main) — nothing to
        # load locally, so a torch-less Remote-only install still straightens.
        print("  Auto-straighten: orientation CNN runs on the pod "
              "(local stays torch-free).\n")
        return
    try:
        import orientation
        if not orientation.is_available():
            print("  Auto-straighten: torch/timm not available — feature disabled.")
            _STRAIGHTEN_DISABLED = True
            return
        print("  Auto-straighten: loading orientation model "
              "(first run downloads ~82 MB) ...")
        orientation._get_model()
        print("  Auto-straighten: ready.\n")
    except Exception as exc:
        print(f"  Auto-straighten: could not initialise ({exc}) — feature disabled.")
        _STRAIGHTEN_DISABLED = True


def straighten_if_needed(path):
    """Detect orientation and rotate `path` upright if confidently sideways.

    Returns (clockwise_degrees_applied, log_message). The message is returned
    rather than printed so the caller can log it next to the per-image result
    (the rotation physically happens before tagging, but reads more naturally
    in the log just above the "Done in" line). All failures are non-fatal:
    tagging proceeds on the un-rotated image.
    """
    if not AUTO_STRAIGHTEN or _STRAIGHTEN_DISABLED:
        return 0, ""
    import orientation   # torch-free for should_rotate/straighten; analyse may be remote
    try:
        if REMOTE_ORIENT is not None:
            # Detect on the pod (sends only a thumbnail); rotate locally with PIL.
            deg, conf = REMOTE_ORIENT(path)
        else:
            deg, conf = orientation.analyse(path)
    except Exception as exc:
        return 0, f"orientation check skipped: {exc}"

    if not orientation.should_rotate(deg, conf, STRAIGHTEN_CONFIDENCE):
        if deg != 0:
            why = "180deg" if deg == 180 else f"below {STRAIGHTEN_CONFIDENCE:.2f} confidence"
            return 0, f"orientation: {deg}deg @ {conf:.2f} — left as-is ({why})"
        return 0, ""

    try:
        cw = orientation.straighten(path, deg)
        direction = "clockwise" if cw > 0 else "counter-clockwise"
        return cw, (f"straightened: rotated 90deg {direction} "
                    f"(detected {deg}deg @ {conf:.2f})")
    except Exception as exc:
        return 0, f"straighten FAILED: {exc}"


# ─────────────────────────────────────────────────────────────
#  RENAME LOGIC
# ─────────────────────────────────────────────────────────────

def build_new_path(original_path, condensed, base_stem=None):
    """
    Build: BASE_STEM_Condensed.ext
    base_stem defaults to the current filename's stem; pass the ORIGINAL
    stem when re-tagging an already-renamed file so the new description
    REPLACES the old one instead of being appended to it.
    Appends _2, _3 etc. on collision (the file's own current name is not
    a collision — re-tagging may produce the same name again).
    """
    dir_  = os.path.dirname(original_path)
    stem  = base_stem if base_stem is not None else os.path.splitext(os.path.basename(original_path))[0]
    ext   = os.path.splitext(original_path)[1]
    new   = os.path.join(dir_, f"{stem}_{condensed}{ext}")
    if os.path.normcase(new) == os.path.normcase(original_path) or not os.path.exists(new):
        return new
    counter = 2
    while True:
        candidate = os.path.join(dir_, f"{stem}_{condensed}_{counter}{ext}")
        if not os.path.exists(candidate):
            return candidate
        counter += 1


def _decode_xp_field(raw, ext_should_match=None):
    """Decode a Windows XP* EXIF field (UTF-16LE, stored as bytes or piexif's int
    tuple) to a stripped string, or None. If ext_should_match is given, the decoded
    value must carry that extension (guards against unrelated content)."""
    if not raw:
        return None
    try:
        if isinstance(raw, (tuple, list)):
            raw = bytes(raw)
        name = raw.decode("utf-16-le", "ignore").rstrip("\x00").strip()
    except Exception:
        return None
    if not name:
        return None
    if ext_should_match is not None and \
            os.path.splitext(name)[1].lower() != ext_should_match.lower():
        return None
    return name


def _recorded_original_name(path):
    """The original filename this tool stored in EXIF XPComment when it first
    tagged `path` (JPEG), or None. Lets a rename recover the true original after
    the cache is lost, so it rebuilds from '001.jpg' rather than appending to an
    already-renamed '001_Child_At_Window.jpg'."""
    try:
        exif = _load_exif_safe(path)
    except Exception:
        return None
    raw = exif.get("0th", {}).get(40092)   # XPComment, UTF-16LE
    return _decode_xp_field(raw, ext_should_match=os.path.splitext(path)[1])


def get_original_name(path, cache, source_root):
    """
    The file's original name from before any rename by this script.

    Source of truth: the cache entry's original_rel_path (seeded once, on first
    scan, and never changed). Falls back to the EXIF XPComment record, then the
    current name. Because the descriptive suffix is always rebuilt from THIS name
    (build_new_path), a wrong answer here is what makes a re-rename append instead
    of overwrite — so ensure_cache_entry seeds the entry from XPComment too, and a
    correct entry is virtually always found here.
    """
    _key, entry = _find_entry(cache, source_root, path)
    if entry is not None and entry.get("original_rel_path"):
        return os.path.basename(entry["original_rel_path"])
    return _recorded_original_name(path) or os.path.basename(path)


# ─────────────────────────────────────────────────────────────
#  CACHE MANAGEMENT
# ─────────────────────────────────────────────────────────────

def get_cache_path(source_root):
    """Return a label for the cache backing store (shown in log messages).
    The cache now lives in the shared SQLite database, not a per-folder file."""
    return db.DB_PATH


def load_cache(source_root):
    """
    Load the cache for source_root from the shared database, or build a fresh
    empty cache. The in-memory shape is unchanged from the old JSON format
    ({schema_version, source_root, created_at, last_updated, files: {key: entry}})
    so the rest of the module is untouched. The original-state snapshots are
    never overwritten — entries are read as last persisted.
    """
    conn = db.get_conn()
    root = db.find_tag_root(conn, source_root)
    now  = time.strftime("%Y-%m-%dT%H:%M:%S")
    # "_dirty"/"_index" are in-memory only (never serialised into a row): the set
    # of keys awaiting an incremental save_cache, and the O(1) lookup index
    # {normcase(current_rel_path): key} that keeps _find_entry off a linear scan.
    # See item 5.
    cache = {
        "schema_version": CACHE_SCHEMA_VERSION,
        "source_root":    os.path.abspath(source_root),
        "created_at":     now,
        "last_updated":   now,
        "files":          {},
        "_dirty":         set(),
        "_index":         {},
    }
    if root is None:
        return cache
    meta = conn.execute("SELECT created_at, last_updated FROM tag_roots WHERE id = ?",
                        (root["id"],)).fetchone()
    if meta is not None:
        cache["created_at"]   = meta["created_at"] or now
        cache["last_updated"] = meta["last_updated"] or now
    for row in conn.execute(
            "SELECT original_rel_path, entry_json FROM tag_files WHERE root_id = ?",
            (root["id"],)):
        try:
            key   = row["original_rel_path"]
            entry = json.loads(row["entry_json"])
            cache["files"][key] = entry
            cache["_index"][os.path.normcase(
                entry.get("current_rel_path") or key)] = key
        except Exception:
            pass
    return cache


def _entry_row(root_id, key, entry):
    """One tag_files row tuple for a cache entry."""
    return (root_id,
            entry.get("original_rel_path", key),
            entry.get("current_rel_path"),
            entry.get("status"),
            json.dumps(entry, ensure_ascii=False))


_TAG_FILES_UPSERT = (
    "INSERT OR REPLACE INTO tag_files "
    "(root_id, original_rel_path, current_rel_path, status, entry_json) "
    "VALUES (?, ?, ?, ?, ?)")


def save_cache(cache, source_root, full=False):
    """Persist the cache dict to the shared database (atomic).

    Incremental by default: only the entries marked dirty since the last save are
    upserted (tag_files' PRIMARY KEY (root_id, original_rel_path) makes
    INSERT OR REPLACE a true upsert). The old code deleted and re-inserted EVERY
    row of the root on every call, and it is called after each processed image /
    failure / skip / rotation — O(N^2) JSON-serialisations + DB work over a run,
    minutes of pure overhead on a large photo tree. See item 5.

    full=True (or a cache built without dirty tracking) does the whole-root
    rewrite — used by undo, which saves once at the end and rewrites in place."""
    cache["last_updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    conn    = db.get_conn()
    root_id = db.get_tag_root_id(conn, os.path.abspath(source_root),
                                 created_at=cache.get("created_at"))
    dirty   = cache.get("_dirty")

    if full or dirty is None:
        rows = [_entry_row(root_id, k, e) for k, e in cache["files"].items()]
        try:
            with conn:   # one transaction; rolls back on error
                conn.execute("DELETE FROM tag_files WHERE root_id = ?", (root_id,))
                if rows:
                    conn.executemany(_TAG_FILES_UPSERT, rows)
                conn.execute("UPDATE tag_roots SET last_updated = ? WHERE id = ?",
                             (cache["last_updated"], root_id))
            if dirty is not None:
                dirty.clear()
        except Exception as exc:
            print(f"  WARNING: Could not save cache to database ({exc}).")
            debug_log("tag_and_rename.save_cache", exc=exc)
        return

    if not dirty:
        return   # nothing changed since the last save — no DB churn

    rows = [_entry_row(root_id, k, cache["files"][k])
            for k in dirty if k in cache["files"]]
    try:
        with conn:   # one transaction; rolls back on error
            if rows:
                conn.executemany(_TAG_FILES_UPSERT, rows)
            conn.execute("UPDATE tag_roots SET last_updated = ? WHERE id = ?",
                         (cache["last_updated"], root_id))
        dirty.clear()
    except Exception as exc:
        print(f"  WARNING: Could not save cache to database ({exc}).")
        # The print reaches the live log pane but not across sessions: also
        # persist it, since a cache that never saves re-tags already-done files.
        debug_log("tag_and_rename.save_cache", exc=exc)


def _snapshot_exif(path):
    """
    Snapshot the tracked EXIF fields from a file.
    Returns a dict  { field_name: base64(raw_bytes) | None }.
    None means the field was absent in the file.
    Raw bytes are base64-encoded so they survive JSON round-trips safely.
    """
    snap = {name: None for name in _TRACKED_EXIF_FIELDS}
    try:
        exif = _load_exif_safe(path)
        for name, (ifd, tag) in _TRACKED_EXIF_FIELDS.items():
            raw = exif.get(ifd, {}).get(tag)
            if raw is None:
                continue
            # piexif returns the XP* tags (XPTitle/XPComment) as int TUPLES, not
            # bytes; base64.b64encode chokes on a tuple. Normalise first, or a
            # snapshot that reaches an XP field would abort mid-loop and silently
            # drop the fields after it (a latent undo gap before item 3 added a
            # second XP tag). bytes() maps the 0-255 int tuple back to raw bytes.
            if isinstance(raw, (tuple, list)):
                raw = bytes(raw)
            snap[name] = base64.b64encode(raw).decode("ascii")
    except Exception:
        pass
    return snap


def _mark_dirty(cache, key):
    """Flag a cache entry for the next incremental save_cache (item 5). No-op on a
    cache built without dirty tracking (that path does a full rewrite instead)."""
    d = cache.get("_dirty")
    if d is not None and key is not None:
        d.add(key)


def _index_set(cache, key, current_rel_path, old_current=None):
    """Maintain the {normcase(current_rel_path): key} lookup index so _find_entry
    stays O(1) after a rename. Drops the old current-path mapping first."""
    idx = cache.get("_index")
    if idx is None:
        return
    if old_current is not None and os.path.normcase(old_current) != os.path.normcase(current_rel_path):
        idx.pop(os.path.normcase(old_current), None)
    idx[os.path.normcase(current_rel_path)] = key


def _find_entry(cache, source_root, abs_path):
    """
    Locate a cache entry for the given absolute path.

    Fast path: the cache key (original_rel_path) directly, then the
    {normcase(current_rel_path): key} index — O(1), maintained by
    ensure_cache_entry / update_cache_entry (item 5; _find_entry is called
    several times per image, so the old linear scan was O(N^2) over a run). A
    cache built WITHOUT that index (e.g. a hand-built one in tests) falls back to
    the linear scan — same result, just O(N).
    Returns (cache_key, entry_dict) or (None, None).
    """
    files = cache["files"]
    rel = os.path.relpath(abs_path, source_root)
    # Direct key match (original path, or file was never renamed)
    if rel in files:
        return rel, files[rel]
    rel_norm = os.path.normcase(rel)
    index = cache.get("_index")
    if index is not None:
        # Index is authoritative: a miss means the file is genuinely not cached
        # (e.g. a brand-new file during the scan) — return in O(1), don't scan.
        key = index.get(rel_norm)
        if key is None:
            return None, None
        entry = files.get(key)
        # Verify against the live entry so a stale index can never return a wrong
        # match; it would just fall through to the scan below.
        if entry is not None and os.path.normcase(entry.get("current_rel_path", "")) == rel_norm:
            return key, entry
    # No index (hand-built cache), or a stale hit: authoritative linear scan.
    for key, entry in files.items():
        if os.path.normcase(entry.get("current_rel_path", "")) == rel_norm:
            return key, entry
    return None, None


def ensure_cache_entry(cache, source_root, abs_path):
    """
    Ensure abs_path has a cache entry with an original-state snapshot.
    If the entry already exists (from a prior run), it is left untouched so
    the original snapshot is never overwritten.
    Returns the cache key (always the original_rel_path).

    When NO entry exists but the file was already tagged by a previous run whose
    cache we've since lost (a reset DB, or the same folder reached via a different
    root path, e.g. mapped drive vs UNC), the file still carries its ORIGINAL name
    in EXIF XPComment. We seed original_rel_path from that, so a re-rename rebuilds
    from '001.jpg' instead of appending to the renamed '001_Child_At_Window.jpg'
    (which then poisons XPComment too, and the name keeps growing every pass).
    """
    key, entry = _find_entry(cache, source_root, abs_path)
    if entry is not None:
        return key  # original snapshot already preserved

    rel  = os.path.relpath(abs_path, source_root)
    snap = _snapshot_exif(abs_path)

    # Recover the true original name from the file's own XPComment record (decoded
    # from the snapshot we just took, so no extra EXIF read). Only accept it if it
    # differs from the current name AND its key isn't already claimed by another
    # file, so we never clobber an existing entry.
    recorded = _decode_xp_field(
        base64.b64decode(snap["XPComment"]) if snap.get("XPComment") else None,
        ext_should_match=os.path.splitext(abs_path)[1])
    original_rel = rel
    was_renamed  = False
    original_exif = snap                 # brand-new file: current IS the original
    if recorded and recorded != os.path.basename(abs_path):
        cand = os.path.join(os.path.dirname(rel), recorded) if os.path.dirname(rel) else recorded
        if cand not in cache["files"]:
            original_rel  = cand
            was_renamed   = True
            # The pristine original had none of the fields this tool writes, so
            # undo (revert to original) should strip them: snapshot them as absent.
            original_exif = {name: None for name in _TRACKED_EXIF_FIELDS}

    cache["files"][original_rel] = {
        "original_rel_path": original_rel,
        "current_rel_path":  rel,
        "original_exif":     original_exif,
        "current_exif":      dict(snap),
        "was_renamed":       was_renamed,
        "rotation":          0,   # net clockwise degrees auto-straighten applied
        "first_seen_at":     time.strftime("%Y-%m-%dT%H:%M:%S"),
        "last_processed_at": None,
        "status":            "scanned",
    }
    _index_set(cache, original_rel, rel)
    _mark_dirty(cache, original_rel)
    return original_rel


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

    old_current = entry.get("current_rel_path")
    new_rel = os.path.relpath(new_abs_path, source_root)
    entry["current_rel_path"]  = new_rel
    entry["was_renamed"]       = (
        os.path.normcase(entry["original_rel_path"]) != os.path.normcase(new_rel)
    )
    entry["current_exif"]      = _snapshot_exif(new_abs_path)
    entry["last_processed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    # Always reflect the latest outcome — a file that was undone and then
    # re-tagged is "processed" again, not stuck at "undone".
    entry["status"] = status
    _index_set(cache, key, new_rel, old_current=old_current)
    _mark_dirty(cache, key)


# ─────────────────────────────────────────────────────────────
#  UNDO
# ─────────────────────────────────────────────────────────────

def _restore_exif_fields(path, original_snap):
    """
    Restore the tracked EXIF fields to their original state.
    Fields that were absent originally (None in snapshot) are DELETED — this
    includes the processed marker, so an undone file is re-taggable.
    Fields that existed are written back from the stored raw bytes.
    Only saves the file if at least one field actually changed.
    Returns (success, changed).

    Note: uses _save_with_exif, which patches only the EXIF segment losslessly
    (piexif.insert, with a single re-encode fallback), consistent with how the
    fields were written in the first place (item 6).
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
        return True, changed
    except Exception as exc:
        print(f"           EXIF restore error: {exc}")
        return False, False


def _undo_entry(entry, source_root, undo_names, undo_exif):
    """
    Perform the undo operation for a single cache entry.
    undo_names – if True, rename the file back to its original name
    undo_exif  – if True, restore the tracked EXIF fields

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
    rotate_ok = True

    # ── Step 0: revert auto-straighten rotation (content change, reverted
    #    alongside EXIF; names-only undo leaves pixels untouched). ──
    if undo_exif:
        net = entry.get("rotation", 0) % 360
        if net:
            try:
                import orientation
                orientation.unrotate(curr_abs, net)
                entry["rotation"] = 0
                notes.append(f"rotation reverted ({net}deg)")
            except Exception as exc:
                rotate_ok = False
                notes.append(f"rotation revert FAILED: {exc}")

    # ── Step 1: restore EXIF fields (while file is at its current path) ──
    # NOTE: an all-None snapshot is NOT "nothing to do" — it means the
    # original had none of the tracked fields, so the ones this script
    # added (including the processed marker) must be deleted. Skipping
    # this used to leave undone files marked as "already tagged".
    if undo_exif:
        orig_snap = entry.get("original_exif") or {}
        exif_ok, exif_changed = _restore_exif_fields(curr_abs, orig_snap)
        if not exif_ok:
            notes.append("EXIF restore FAILED")
        else:
            entry["current_exif"] = orig_snap.copy()
            notes.append("EXIF restored" if exif_changed else "EXIF already original")

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
                _gui_event("RENAME", json.dumps([curr_abs, orig_abs]))
                entry["current_rel_path"] = entry["original_rel_path"]
                entry["was_renamed"]      = False
                notes.append(f"renamed back to {os.path.basename(orig_abs)}")
            except Exception as exc:
                rename_ok = False
                notes.append(f"rename FAILED: {exc}")

    success = exif_ok and rename_ok and rotate_ok
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

    _gui_event("QUEUE", json.dumps([
        os.path.join(root, e.get("current_rel_path") or e.get("original_rel_path", ""))
        for e in entries_to_undo
    ]))

    for entry in entries_to_undo:
        display = entry.get("current_rel_path") or entry.get("original_rel_path", "?")
        print(f"  {display}")
        _gui_event("IMG", os.path.join(root, display))
        success, msg = _undo_entry(entry, root, undo_names, undo_exif)
        print(f"           -> {msg}")
        if success:
            ok_count += 1
        else:
            fail_count += 1

    # Undo mutates entries in place and saves once at the end (not the per-image
    # hot path), so a full rewrite is simplest and keeps the DB in step with the
    # reverted current_rel_path / status of every entry.
    save_cache(cache, root, full=True)

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

    Folders this app produced (`__upscaled__`, `__Archive__`, …) are pruned (#16):
    tagging an upscale AND its original was never intended, and the archive holds
    pre-upscale copies that force_tag would happily rename. To tag an upscaled
    tree, point this AT `__upscaled__`: only subdirectories are pruned, never the
    chosen root.
    """
    items = []
    pruner = runner_common.DerivedPruner(_CFG)
    for dirpath, dirnames, filenames in os.walk(root):
        pruner.prune(dirnames)
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
    _pruned = pruner.summary()
    if _pruned:
        print(f"  {_pruned}")
    return items


def _start_remote_telemetry(engine, interval=10.0):
    """Poll the pod's telemetry and stream it to the GUI as RTELEM events so the
    Tag & Rename tab shows the same 'Remote pod' CPU/RAM/VRAM/temp row as the
    upscaler (#4). The worker serves /telemetry in tag mode too. Daemon thread,
    best-effort — a failed sample is just skipped."""
    def _loop():
        while True:
            time.sleep(interval)
            try:
                sample = engine.telemetry()
            except Exception:                             # noqa: BLE001 (fail-safe)
                sample = None
            if sample:
                _gui_event("RTELEM", json.dumps(sample))
    threading.Thread(target=_loop, daemon=True).start()


def _setup_remote_tagging():
    """Remote Tag & Rename (#1): create/reuse a pod running Ollama + the
    orientation CNN, repoint OLLAMA_URL at the ssh tunnel and route straighten to
    the pod (REMOTE_ORIENT). Returns the RemoteSession (so the caller keeps a
    reference; teardown is registered via atexit) or None when not in remote
    mode. Exits on failure. The on-pod dead-man's switch is the teardown backup.

    Kept out of main() so the `global OLLAMA_URL` reassignment doesn't clash with
    main() reading OLLAMA_URL earlier in its body."""
    global OLLAMA_URL, REMOTE_ORIENT, REMOTE_SESSION
    if not REMOTE:
        return None
    import atexit
    from remote_run import RemoteSession

    def _remote_status(msg):
        print(f"  [remote] {msg}", flush=True)
        _gui_event("STATUS", msg)

    _gui_event("STATUS", "Starting the remote pod for tagging …")
    # Dev-only: IMGTBX_REMOTE_ATTACH="pod_id,host,ssh_port" reuses a running pod.
    _attach = None
    _att = os.environ.get("IMGTBX_REMOTE_ATTACH")
    if _att:
        _p = _att.split(",")
        _attach = (_p[0], _p[1], int(_p[2]))
    try:
        session = RemoteSession(_CFG.get("runpod", {}), _CFG.get("upscale", {}),
                                APP_ROOT, on_event=_remote_status,
                                attach=_attach, mode="tag")
        engine = session.start()
    except Exception as exc:                          # noqa: BLE001
        print(f"ERROR: Could not start the remote pod for tagging.\n  -> {exc}")
        _gui_event("STATUS", f"Remote tagging failed to start: {exc}")
        sys.exit(1)
    OLLAMA_URL = session.ollama_url                   # tag_and_rename now calls the pod's Ollama
    REMOTE_ORIENT = engine.analyse                    # straighten detection runs on the pod
    REMOTE_SESSION = session                          # so the loop can detect a pod stop
    # Tell the GUI which pod is live, so the RunPod tab won't offer to terminate
    # the pod this run depends on (cleared when the run exits).
    _gui_event("POD", session.pod_id or "")
    # The pod's real billed rate ($/h) drives the GUI's live cost readout.
    if session.cost_per_hr is not None:
        _gui_event("RCOST", f"{session.cost_per_hr}")
    _start_remote_telemetry(engine)                   # feed the GUI's 'Remote pod' row

    def _remote_teardown():
        session.close(stop_pod={"stop": True, "keep": False}.get(_REMOTE_TEARDOWN))
    atexit.register(_remote_teardown)
    print(f"  Remote tagging ready — Ollama via {OLLAMA_URL}")
    return session


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
        print("  EXIF fields written (JPEG only; other formats are renamed, pixels untouched):")
        print("    * ImageDescription  — long natural-language description (20-40 words, UTF-8)")
        print("    * XPTitle           — same description in the Windows 'Title' field (UTF-16LE)")
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
        print("    Every run records original filenames and EXIF data in the shared")
        print(f"    SQLite cache:  {os.path.join(APP_ROOT, 'db', 'cache.db')}  (tag_files table)")
        print("    This lets you reverse renames, EXIF changes, or both at any time.")
        print()
        print("  Configuration (edit config.json at the app root, the parent of scripts/):")
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
        print("    --no-prompt        Skip the 'Press Enter when ready' pre-flight prompt")
        print("                       (used by the GUI; also handy for scripted runs).")
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
    no_prompt    = "--no-prompt"  in args   # GUI mode: skip the pre-flight Enter prompt

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
        "-ftag", "-frename", "--undo-all", "--names-only", "--exif-only", "--no-prompt"
    ) and not a.lower().startswith("--language:")]

    if not args:
        print("ERROR: No directory specified.")
        sys.exit(1)

    # Strip stray quotes — PowerShell turns a trailing backslash before a
    # closing quote into a literal quote (e.g. "X:\Photos\" -> X:\Photos").
    root = os.path.abspath(args[0].strip().strip('"').strip("'"))
    if not os.path.isdir(root):
        print(f"ERROR: '{root}' is not a valid directory.")
        sys.exit(1)

    # ── Dependency check (before any prompts or Ollama calls) ───
    check_dependencies()

    # ── Session log: everything printed below also lands in the log file ──
    log_path = _setup_session_log(root)
    _gui_event("LOG", log_path)

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

    # ── Auto-straighten: load the orientation model now, in the MAIN thread,
    #    BEFORE RemoteControl starts its stdin-reader thread. Importing torch
    #    (a heavy C extension) while a background thread is already blocked in
    #    stdin.readline() deadlocks on the Windows loader lock — so the import
    #    must happen here, before any other thread exists. ──
    # Remote Tag & Rename (#1): start the pod (Ollama + orientation CNN) and
    # repoint OLLAMA_URL / REMOTE_ORIENT BEFORE warm-up and the Ollama check, so
    # both see the remote setup. No-op locally.
    _remote_session = _setup_remote_tagging()

    warm_up_straighten()

    # ── Remote control (active only when stdin is piped, e.g. GUI mode) ──
    control = RemoteControl()

    # ── Pre-flight ───────────────────────────────────────────
    if not (no_prompt or control.active):
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

    # Free VRAM no matter how this run ends (finish, stop, error, Ctrl+C):
    # the model must never stay resident after Image Toolbox is done with it.
    import atexit
    atexit.register(unload_model)

    # ── Scan ─────────────────────────────────────────────────
    print(f"  Scanning '{root}' ...\n")
    work_items = collect_work_items(root, force_tag=force_tag)

    if not work_items:
        print("  No qualifying images found.")
        sys.exit(0)

    total = len(work_items)
    print(f"  Found {total} qualifying image(s).\n")
    _gui_event("QUEUE", json.dumps(work_items))

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
        "processed": 0, "rotated": 0, "skipped": 0, "failed": 0, "elapsed": 0.0
    })

    total_processed   = 0
    total_rotated     = 0
    total_skipped     = 0
    total_failed      = 0
    consecutive_fails = 0
    grand_start       = time.time()
    current_folder    = None
    folder_start      = None
    stop_reason       = None      # "user" or "ollama" when the run ends early

    # ── Pause frees the GPU ──────────────────────────────────
    # Same bargain as the upscaler's Pause: the user wants the card back for
    # something else without losing the queue. Ollama holds the vision model
    # resident (its own keep_alive would only drop it minutes later), so a pause
    # that did not unload would not actually free anything.
    #
    # Local runs only: on a remote run the model sits on the pod, where
    # unloading frees nothing on this PC and costs a reload on a billed machine.
    def _pause_release():
        _gui_event("PSTATE", "paused")
        # Free EVERY model this run holds, not just the big one: the vision model
        # (in the Ollama server) and the auto-straighten CNN (in this process).
        # No size-based exceptions — an exception is one more thing to remember.
        freed = []
        if not REMOTE:
            if unload_model():
                freed.append("vision model")
            # Remote runs detect orientation on the pod (REMOTE_ORIENT), so
            # locally there is nothing loaded to release.
            try:
                import orientation          # lazy, as everywhere else in here
                if orientation.unload():
                    freed.append("straighten model")
            except Exception as exc:                   # noqa: BLE001
                debug_log("tag_and_rename: orientation unload failed", exc=exc)
        if freed:
            print(f"\n  ⏸  PAUSED — {' and '.join(freed)} unloaded, the GPU is "
                  f"free for other apps. Press Resume to continue.")
        else:
            print("\n  ⏸  PAUSED — press Resume to continue.")

    def _pause_reload():
        _gui_event("PSTATE", "running")
        # Ollama reloads the model on the next request by itself, so there is
        # nothing to do here beyond warning that the next image is slower.
        print("  ▶  RESUMED — the vision model reloads on the next image.\n")

    _gui_event("PSTATE", "running")

    for idx, path in enumerate(work_items, 1):
        if control.stop_requested:
            print("\n  Stop requested — stopping before the next image.")
            stop_reason = "user"
            break

        if not control.wait_while_paused(_pause_release, _pause_reload):
            print("\n  Stop requested — stopping before the next image.")
            stop_reason = "user"
            break

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
        # EXIF marker (JPEG) OR the cache's "processed" status (the skip signal
        # for non-JPEG files, which carry no EXIF marker — item 2).
        if not force_tag and is_already_processed(path, cache, root):
            print(f"  {prefix} SKIP (already tagged)  {path}")
            # Mark as skipped in cache only if it hasn't been fully processed before
            _key, _entry = _find_entry(cache, root, path)
            if _entry and _entry.get("status") == "scanned":
                _entry["status"] = "skipped"
                _mark_dirty(cache, _key)
                save_cache(cache, root)
            folder_stats[dirpath]["skipped"] += 1
            total_skipped += 1
            continue

        img_start = time.time()

        # The name from before any rename by this script — keeps re-tagging
        # idempotent: the descriptive suffix is rebuilt from the ORIGINAL
        # stem (replacing the previous description), never appended to it,
        # and EXIF XPComment always keeps the true original filename.
        original_name = get_original_name(path, cache, root)

        # Hash the file in its pre-tag state (== the upscaled output, if this
        # tree came from the upscaler) BEFORE auto-straighten/EXIF/rename change
        # its bytes. This is the join key back to the upscale lineage.
        in_hash = db.hash_file_cached(db.get_conn(), path)

        # ── 0. Auto-straighten (before tagging, so the description is generated
        #       on the corrected image and the preview shows it upright). The
        #       log message is deferred and printed next to the result below. ──
        rotation_cw, straighten_msg = straighten_if_needed(path)
        if rotation_cw:
            total_rotated += 1
            folder_stats[dirpath]["rotated"] += 1
            _rk, _re = _find_entry(cache, root, path)
            if _re is not None:
                _re["rotation"] = (_re.get("rotation", 0) + rotation_cw) % 360
                # Persist immediately so a mid-image crash can't orphan a
                # rotated file from its undo record.
                _mark_dirty(cache, _rk)
                save_cache(cache, root)

        w, h    = get_image_dimensions(path)
        dim_str = f"{w}x{h}px" if w else "?x?px"
        _gui_event("IMG", path)    # GUI preview strip: current (now upright) image
        print(f"  {prefix} {dim_str}  {path}")

        try:
            # 1. Analyse
            long_desc, condensed = analyse_image(path, language=language)

            # 2. Write ALL of the tag's EXIF in one lossless save (item 6):
            #    description + XPTitle + original filename (XPComment) + the
            #    processed marker (UserComment), before the rename so it lands on
            #    the same bytes either way. JPEG only; a non-JPEG is left byte-for-
            #    byte untouched and carries its description in the filename + a
            #    "processed" marker in the cache (item 2).
            exif_written = write_exif(path, long_desc, original_name)

            # 3. Rename if camera default name
            will_rename = force_rename or has_camera_default_name(original_name)
            if will_rename:
                new_path    = build_new_path(path, condensed,
                                             base_stem=os.path.splitext(original_name)[0])
                os.rename(path, new_path)
                _gui_event("RENAME", json.dumps([path, new_path]))
                result_name = os.path.basename(new_path)
                print(f"           -> {result_name}  (renamed)")
            else:
                new_path    = path
                result_name = filename
                print(f"           -> {result_name}  (tagged only, name kept)")

            # 4. Update cache with final state (also the non-JPEG skip marker)
            update_cache_entry(cache, root, path, new_path, "processed")
            save_cache(cache, root)

            # 4b. Link the tagged result back to its upscaled input by content
            # hash, so conciliation can match it even after a folder move.
            try:
                out_hash = db.hash_file_cached(db.get_conn(), new_path)
                db.record_tag_lineage(db.get_conn(), in_hash, out_hash, new_path)
            except Exception as exc:
                debug_log("tag_and_rename.record_tag_lineage", exc=exc)

            # An auto-straightened image was rotated on disk AFTER its strip
            # thumbnail was first decoded, so that thumbnail is stale. Ask the
            # GUI to re-decode it for the final (post-rename) path.
            if rotation_cw:
                _gui_event("REFRESH", new_path)

            img_elapsed   = time.time() - img_start
            grand_elapsed = time.time() - grand_start
            print(f"           -> \"{long_desc}\"")
            if not exif_written:
                ext = os.path.splitext(path)[1].lower()
                print(f"           (EXIF not embedded for {ext}: description is in "
                      f"the filename, skip-marker in the cache)")
            if straighten_msg:
                print(f"           {straighten_msg}")
            print(f"           Done in {fmt_mmss(img_elapsed)} | "
                  f"Total elapsed: {fmt_hhmmss(grand_elapsed)}\n")

            consecutive_fails = 0
            folder_stats[dirpath]["processed"] += 1
            total_processed += 1
            # Strip: green. Key by the final (post-rename) path the strip tracks.
            _gui_event("RESULT", json.dumps([new_path, "ok"]))
            # GUI ETA: elapsed, images processed this session, position, total.
            # Averaged over processed count, not the counter (which also
            # advances on skipped/already-tagged files). Trailing 'P' marks the
            # real processing phase for the GUI's live cost readout.
            _gui_event("ETA", f"{grand_elapsed:.3f}|{total_processed}|{idx}|{total}|P")

        except Exception as e:
            img_elapsed   = time.time() - img_start
            grand_elapsed = time.time() - grand_start
            consecutive_fails += 1
            if straighten_msg:
                print(f"           {straighten_msg}")
            print(f"           FAILED in {fmt_mmss(img_elapsed)} | "
                  f"Total elapsed: {fmt_hhmmss(grand_elapsed)}")
            print(f"           Error: {type(e).__name__}: {e}")
            traceback.print_exc()
            print()

            update_cache_entry(cache, root, path, path, "failed")
            save_cache(cache, root)

            # Strip: red. No rename happens on failure, so the path is unchanged.
            _gui_event("RESULT", json.dumps([path, "fail"]))
            folder_stats[dirpath]["failed"] += 1
            total_failed += 1

            # ── Remote pod stopped (dead-man's switch) ───────
            # If the pod is gone, this isn't a recoverable Ollama outage — end the
            # run cleanly (re-run continues; already-tagged files are skipped).
            if _remote_pod_stopped():
                print("  The remote pod has stopped — its dead-man's switch reached "
                      "the idle / max-runtime limit (or it was stopped). Ending the "
                      "run cleanly; re-run to continue.")
                _gui_event("STATUS", "Remote pod stopped — ending the run.")
                send_notification(
                    title       = "Tag & Rename -- Remote pod stopped",
                    description = ("The remote pod's dead-man's switch fired (idle or "
                                   "max-runtime), or the pod was stopped. The run ended "
                                   "cleanly — re-run to continue; tagged files are skipped."),
                    color       = notifications.COLOR_ORANGE,
                    fields      = [
                        {"name": "Progress", "value": f"{idx}/{total}"},
                        {"name": "Machine",  "value": os.environ.get("COMPUTERNAME", "unknown")},
                    ],
                )
                stop_reason = "remote"
                break

            # ── Outage detection ─────────────────────────────
            if consecutive_fails >= OUTAGE_THRESHOLD:
                print(f"  WARNING: {consecutive_fails} consecutive failures.")
                send_notification(
                    title       = "Tag & Rename -- Repeated Failures Detected",
                    description = (f"{consecutive_fails} consecutive image(s) failed. "
                                   f"Ollama may be unreachable or the model unloaded.\n"
                                   f"Last error: {type(e).__name__}: {e}"),
                    color       = notifications.COLOR_RED,
                    fields      = [
                        {"name": "Last failed image", "value": path},
                        {"name": "Progress",          "value": f"{idx}/{total}"},
                        {"name": "Machine",           "value": os.environ.get("COMPUTERNAME", "unknown")},
                    ],
                )
                if unload_model():
                    print("  (Vision model unloaded while the run is held - VRAM released.)")
                if control.active:
                    print("  Restart Ollama if needed, then press Resume in the app.")
                    # Relabel the dual button: while the outage holds the run, it
                    # means Resume, not Pause.
                    _gui_event("PSTATE", "outage")
                    resumed = control.wait_resume()
                    _gui_event("PSTATE", "running")
                    if not resumed:
                        print("  Stop requested — exiting.")
                        stop_reason = "user"
                        break
                else:
                    print("  Restart Ollama if needed, then press Enter to resume.")
                    input("  Press Enter to resume ...")
                ok, msg = check_ollama()
                if not ok:
                    print(f"  ERROR: {msg}")
                    print("  Exiting. Restart Ollama and run the script again.")
                    stop_reason = "ollama"
                    break
                print(f"  {msg}\n")
                consecutive_fails = 0

            continue

    # ── Close last folder ────────────────────────────────────
    if current_folder is not None:
        elapsed = time.time() - folder_start
        folder_stats[current_folder]["elapsed"] += elapsed
        print(f"\n  Folder done in {fmt_duration(elapsed)}\n")

    # ── Release VRAM: unload the vision model now that tagging is over ──
    if unload_model():
        print("  Ollama vision model unloaded - VRAM released.\n")

    # ── Summary table ─────────────────────────────────────────
    grand_elapsed = time.time() - grand_start

    col_path = min(60, max(
        len("Folder"),
        max((len(os.path.relpath(p, root)) for p in folder_stats), default=6)
    ))
    col_proc = len("Processed")
    col_rot  = len("Rotated")
    col_skip = len("Skipped")
    col_fail = len("Failed")
    col_time = max(
        len("Elapsed"),
        max((len(fmt_duration(v["elapsed"])) for v in folder_stats.values()), default=7)
    )

    def trunc(s, n):
        return s if len(s) <= n else "..." + s[-(n - 3):]

    width = col_path + col_proc + col_rot + col_skip + col_fail + col_time + 18
    sep = "=" * width
    row = (f"  {{:<{col_path}}}  {{:>{col_proc}}}  {{:>{col_rot}}}  "
           f"{{:>{col_skip}}}  {{:>{col_fail}}}  {{:>{col_time}}}")

    print("\n" + sep)
    print(row.format("Folder", "Processed", "Rotated", "Skipped", "Failed", "Elapsed"))
    print("-" * width)

    for dp, stats in folder_stats.items():
        rel = os.path.relpath(dp, root) if dp != root else "."
        print(row.format(
            trunc(rel, col_path),
            stats["processed"], stats["rotated"], stats["skipped"],
            stats["failed"], fmt_duration(stats["elapsed"])
        ))

    print(sep)
    print(row.format(
        "TOTAL", total_processed, total_rotated, total_skipped,
        total_failed, fmt_hhmmss(grand_elapsed)
    ))
    print(sep)
    print(f"\n  ({total_rotated} rotated, {total_failed} failed, {total_skipped} already tagged)\n")
    print(f"  Undo cache: {cache_path_display}\n")

    # GUI: machine-readable run summary (drives the MQTT last_run topic).
    _gui_event("DONE", json.dumps({
        "tool":            "tag",
        "processed":       total_processed,
        "rotated":         total_rotated,
        "skipped":         total_skipped,
        "failed":          total_failed,
        "elapsed_seconds": round(grand_elapsed, 1),
        "stop_reason":     stop_reason or "completed",
    }))

    # ── Discord: queue finished / stopped ────────────────────────────────────
    if stop_reason == "remote":
        notif_title, notif_color = ("Tag & Rename -- Remote Pod Stopped (re-run to continue)",
                                    notifications.COLOR_ORANGE)
    elif stop_reason == "ollama":
        notif_title, notif_color = ("Tag & Rename -- Stopped (Ollama Unreachable)",
                                    notifications.COLOR_RED)
    elif total_failed > 0:
        notif_title, notif_color = ("Tag & Rename -- Finished with Failures",
                                    notifications.COLOR_YELLOW)
    elif stop_reason == "user":
        notif_title, notif_color = ("Tag & Rename -- Stopped by User",
                                    notifications.COLOR_YELLOW)
    else:
        notif_title, notif_color = ("Tag & Rename -- Finished",
                                    notifications.COLOR_GREEN)
    send_notification(
        title       = notif_title,
        description = f"{total_processed} processed, {total_rotated} rotated, {total_skipped} already tagged, {total_failed} failed",
        color       = notif_color,
        fields      = [
            {"name": "Folder",        "value": root},
            {"name": "Total elapsed", "value": fmt_hhmmss(grand_elapsed)},
            {"name": "Machine",       "value": os.environ.get("COMPUTERNAME", "unknown")},
        ],
    )


if __name__ == "__main__":
    main()
