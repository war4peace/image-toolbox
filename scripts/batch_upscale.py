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
import notifications
import runner_common
import raw_decode                    # RAW / DNG input (#19); rawpy stays lazy inside it

# Fail-safe diagnostic trail for the swallowed-error handlers (guarded import).
try:
    from debug_log import debug_log
except Exception:
    def debug_log(*_a, **_k):
        pass

# Make stdout/stderr non-ASCII-proof before any output (see runner_common).
runner_common.harden_stdout()

# ─────────────────────────────────────────────
#  CONFIG  –  loaded from config.json
# ─────────────────────────────────────────────

# config.json load lives in runner_common (shared by every runner).
_load_config = runner_common.load_config

_CFG = _load_config()
_S   = _CFG.get("seedvr2", {})
_U   = _CFG.get("upscale", {})

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
# App root = parent of scripts/. config.json, seedvr2/, models/, logs/ and the
# .venv all live at the app root, not beside this module.
APP_ROOT    = os.path.dirname(_SCRIPT_DIR)

def _resolve_path(value, default_rel):
    """
    Resolve a config path; relative paths are anchored at the app root.
    Environment variables (%USERPROFILE%, %USERNAME%, …) are expanded, so
    config.json stays portable between machines and users.
    """
    p = os.path.expandvars(value or default_rel)
    return p if os.path.isabs(p) else os.path.normpath(os.path.join(APP_ROOT, p))

SEEDVR2_REPO_DIR    = _resolve_path(_S.get("repo_dir", ""),  "seedvr2")
SEEDVR2_MODEL_DIR   = _resolve_path(_S.get("model_dir", ""), os.path.join("models", "SEEDVR2"))

# `.gif` (#27) is an ordinary member here: unlike a RAW it IS written to disk in
# a form that replaces it, and Conciliation is expected to match and move it in.
# What makes it safe is that `.gif` joined runner_common.VARIANT_CANDIDATE_EXTS
# in the SAME change, so an animated or transparent one is skipped and reported
# rather than flattened to its first frame. Never add one without the other.
IMAGE_EXTS          = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff", ".tif",
                       ".gif"}
# RAW / DNG input (#19). Kept as its OWN set rather than folded into IMAGE_EXTS,
# because a RAW is not interchangeable with the others anywhere downstream: it is
# never written to (Tag & Rename must not touch it), it never keeps its extension
# on output, and Conciliation must never archive or delete one. `INPUT_EXTS` is
# what the walk accepts; every later branch asks which of the two it is.
RAW_EXTS            = set(raw_decode.RAW_EXTS)
INPUT_EXTS          = IMAGE_EXTS | RAW_EXTS
OUTPUT_SUBDIR       = _U.get("output_subdir",    "upscaled")
RESOLUTION          = _U.get("resolution",       2160)
MAX_RESOLUTION      = _U.get("max_resolution",   3840)
NOTIFY              = notifications.resolve_settings(_CFG)
OUTAGE_THRESHOLD    = _U.get("outage_threshold", 3)

# Performance watchdog (0.3.0). On a clean boot the 3090 upscales at a steady
# rate, but after accumulating GPU/driver state the pipeline can degrade — per
# image time climbs from seconds to minutes (the GPU thrashing VRAM into system
# RAM), or it surfaces as a hard OOM. Only a reboot cures it, and it wastes power
# for no progress meanwhile. The watchdog compares each image's **seconds per
# output megapixel** against the run's **running minimum** (the GPU's healthy
# throughput) — anchoring to the minimum, not a rolling average, means a slow
# CREEPING ramp can't drift the baseline up and evade detection, and the per-MP
# normalisation keeps it valid across very different image resolutions. EITHER a
# sustained slow streak OR a hard OOM is one "degradation episode": it notifies
# (GUI + Discord + taskbar flash) and AUTO-STOPS after the current image. The
# resume cache picks the queue back up after a reboot, so stopping is safe.
WATCHDOG_ENABLED     = bool(_U.get("watchdog_enabled", True))
WATCHDOG_FACTOR      = float(_U.get("watchdog_factor", 3.0))   # x the healthy rate = "slow"
WATCHDOG_CONSECUTIVE = int(_U.get("watchdog_consecutive", 2))  # slow images in a row to trip
WATCHDOG_MIN_SAMPLES = int(_U.get("watchdog_min_samples", 3))  # images seen before tripping

# Auto-straighten BEFORE upscaling (0.2.7). The upscaler targets 3840px wide OR
# 2160px tall (whichever is reached first) so the result fits a 4K screen with no
# resizing. A sideways photo upscaled as-is fills the wrong axis; when Tag &
# Rename later rotates it 90deg the dimensions swap and it no longer fits. So we
# rotate first: detect orientation with the same CNN/threshold as Tag & Rename
# (orientation.py), rotate a TEMP COPY upright, upscale that, and delete it — the
# source is never touched. Set auto_straighten=false to keep the old behaviour.
AUTO_STRAIGHTEN       = bool(_U.get("auto_straighten", True))
STRAIGHTEN_CONFIDENCE = float(_U.get("straighten_min_confidence", 0.9))
# Carry the source's EXIF onto the upscaled image (#13a). The copy itself happens
# in upscale_engine (LOCAL or on the pod, wherever the file is written), which
# reads this same key out of the pushed upscale settings; this copy exists so the
# run banner can state it once. Conciliation reads it too, for the #13b backfill.
COPY_METADATA         = bool(_U.get("copy_metadata", True))
# Cap the orientation-model load. Normally ~3 s (or a one-time ~82 MB download);
# but a stuck local CUDA init (a degraded/contended GPU) could hang forever and
# block the whole run — especially in remote mode where the GPU work is on the
# pod. If the load exceeds this, fail safe to "no straighten" and continue.
STRAIGHTEN_INIT_TIMEOUT = float(_U.get("straighten_init_timeout", 90))
_STRAIGHTEN_DISABLED  = False     # set True at warm-up if torch/timm unavailable
# Remote mode (#1 option B): the orientation CNN runs ON THE POD. The local side
# stays torch-free — it sends a thumbnail to the worker's /orient endpoint, then
# rotates a temp copy (pure PIL) and uploads that, exactly like the local path.
# Set in warm_up_straighten(); detect_rotation routes the CNN to ENGINE.analyse.
REMOTE_STRAIGHTEN     = False

# The SeedVR2 engine — created in main() after CLI validation so that
# `python batch_upscale.py` (usage help) stays instant.
ENGINE = None


# The @@TBX@@ event protocol + GUI-mode detection live in runner_common. The
# event kinds this runner emits: IMG|<path> (image being processed),
# QUEUE|<json> (queued paths this pass), RESULT|<json> ([path, "ok"|"fail"(,
# out_path)]; the strip frames it green when an upscaled counterpart exists so it
# is comparable, red on failure).
_stdin_is_piped = runner_common.stdin_is_piped
GUI_MODE        = runner_common.GUI_MODE
GUI_MARKER      = runner_common.GUI_MARKER
_gui_event      = runner_common.gui_event


# Remote-pod teardown override (roadmap #1): when the GUI's "Stop" modal lets the
# user choose, it sends "qstop"/"qkeep" on stdin which sets this. The remote
# teardown reads it: "stop" → stop the pod now (even a reused one), "keep" → leave
# it running for the dead-man's switch, None → default (stop only a pod we made).
_REMOTE_TEARDOWN = None

# The active RemoteSession (roadmap #1), set in main() for a remote run. Lets the
# run loop ask whether the pod is still alive when an upscale fails, so a
# dead-man's-switch stop ends the run cleanly instead of looking like an outage.
REMOTE_SESSION = None


def _set_remote_teardown(value):
    global _REMOTE_TEARDOWN
    _REMOTE_TEARDOWN = value


def _remote_pod_stopped():
    """See runner_common.remote_pod_stopped: True if this run's remote pod is no
    longer RUNNING (dead-man's switch fired / stopped / terminated), so a failed
    upscale ends the run gracefully instead of looking like a local GPU outage."""
    return runner_common.remote_pod_stopped(REMOTE_SESSION)


def _start_remote_telemetry(engine, interval=10.0):
    """Remote #1, Feature #4: poll the pod's telemetry and stream it to the GUI
    as RTELEM events, so it can show a dedicated 'remote pod' readout row. The
    worker answers /telemetry lock-free, so this works during an upscale. Daemon
    thread; best-effort (a failed sample is just skipped)."""
    def _loop():
        while True:
            time.sleep(interval)
            try:
                sample = engine.telemetry()
            except Exception:
                sample = None
            if sample:
                _gui_event("RTELEM", json.dumps(sample))
    threading.Thread(target=_loop, daemon=True).start()


# Images whose shortest dimension is already >= this fraction of the target
# will be skipped. Default 66% means a 2538x1428 image (66% of 3840x2160)
# would be skipped — only images that need at least a 1.5x upscale are processed.
# Set to 0 to disable (process all eligible images regardless of how close to target).
UPSCALE_CUTOFF_PCT = _U.get("upscale_cutoff_pct", 66)


# ─────────────────────────────────────────────
#  TIMING HELPERS
# ─────────────────────────────────────────────

# Duration formatting + OOM classification live in runner_common (shared).
fmt_duration  = runner_common.fmt_duration
fmt_mmss      = runner_common.fmt_mmss
fmt_hhmmss    = runner_common.fmt_hhmmss
_is_oom_error = runner_common.is_oom_error


# ─────────────────────────────────────────────
#  NOTIFICATIONS  (Discord + Telegram, see notifications.py)
# ─────────────────────────────────────────────

def send_notification(title, description, color, fields=None):
    """
    Fan out an alert to every configured backend (Discord webhook, Telegram bot).
    No-op for any backend that isn't configured; fail-safe.
    color: a notifications.COLOR_* severity constant (never a raw int).
    fields: list of {"name": str, "value": str} dicts, or None.
    """
    notifications.notify(NOTIFY, title, description, color, fields, username="Upscale Bot")


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
        log_dir  = os.path.join(APP_ROOT, "logs")
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
        """Print to terminal and write to log file. Every file line carries a
        timestamp (0.3.9) so the on-disk log reconstructs run timing; stdout
        stays clean (the GUI window adds its own per-line timestamp). The legacy
        `timestamp` flag is kept for call-site compatibility but no longer needed."""
        print(msg)
        self._fh.write(f"{self._ts()} | {msg}\n")

    def log_only(self, msg, timestamp=False):
        """Write to log file only (not printed to terminal); always timestamped."""
        self._fh.write(f"{self._ts()} | {msg}\n")

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
        except Exception as exc:
            # Non-fatal (cache is best-effort), but a save that never lands means
            # a stopped batch won't resume: leave a trail.
            debug_log("EligibilityCache.save", exc=exc)

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
        except Exception as exc:
            # Best-effort: a missing lineage row only makes conciliation fall
            # back to name matching, but record why it was skipped.
            debug_log("EligibilityCache.record_lineage", exc=exc)

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

# Header-based image-size reader lives in runner_common (shared with the other
# runners; superset with a Pillow fallback for the formats the fast path misses).
get_image_dimensions = runner_common.get_image_dimensions

# Images the engine cannot round-trip (alpha, multi-page, >8-bit): detected and
# left alone rather than silently flattened (#17). See runner_common.
image_variant_reason = runner_common.image_variant_reason
is_variant_reason    = runner_common.is_variant_reason


def variant_next_step(path, reason):
    """A pointer to the tool that CAN handle this file, or "" when none exists.

    Only one case has an answer today: an ANIMATED GIF, which this tool genuinely
    cannot process (it makes images, not videos) but the Video Upscaler can since #27
    phase 2. Saying only "would lose 5 of 6 frames" is correct about the danger and
    useless about the remedy, and it leaks the app's own internal split -- whether a
    thing is "a video or a series of images" is a technical detail a user should never
    have to hold in order to find the right tab.

    Animated is detected from the REASON rather than by re-opening the file, and the
    coupling is deliberate: only the multi-frame branch of image_variant_reason emits
    " frames", so a transparent STATIC GIF ("would lose transparency") correctly gets
    no pointer, while a transparent animated one does. tests/test_gif_video.py pins
    both directions, since the two strings live in different modules."""
    if os.path.splitext(path)[1].lower() == ".gif" and " frames" in (reason or ""):
        return "  ->  the Video Upscaler tab can upscale this as a video"
    return ""


def _skip_for_dims(w, h, cutoff_pct):
    """
    The skip rule for a single (already-upright) width/height.

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
    if w >= MAX_RESOLUTION:
        return True, f"width {w}px >= {MAX_RESOLUTION}px"
    if h >= RESOLUTION:
        return True, f"height {h}px >= {RESOLUTION}px"

    if cutoff_pct > 0:
        cutoff_w = MAX_RESOLUTION * cutoff_pct / 100
        cutoff_h = RESOLUTION     * cutoff_pct / 100
        if w >= cutoff_w or h >= cutoff_h:
            scale = min(MAX_RESOLUTION / w, RESOLUTION / h)
            return True, f"within cutoff ({w}x{h}px, would upscale {scale:.2f}x < {100/cutoff_pct:.2f}x minimum)"

    return False, ""


def should_skip_resolution(path, cutoff_pct=None):
    """
    Cheap eligibility check used by the scan and as a pre-filter in the run loop.
    Reads the stored dimensions (header only — no decode) and applies the skip
    rule (see _skip_for_dims).

    When auto-straighten is active, a sideways photo's TRUE (upright) dimensions
    are the transpose of its stored dimensions, which can flip this decision. We
    deliberately do NOT run the orientation CNN here — eligibility scanning walks
    every file and must stay cheap — so we are conservative: an image counts as
    "too large" only when it would be skipped in BOTH orientations. The precise,
    orientation-aware skip is made at process time once the CNN has run, against
    the real upright dimensions (see run_pass).

    A RAW is NEVER too large (#19). For every other format "already at target"
    means "nothing to do, it already displays"; a RAW displays nowhere, so its
    render is the deliverable whatever its size, and the size only decides
    render-only vs render-then-upscale. Answering here rather than at each caller
    is what keeps the eligibility scan, its cache, the rescan filter and the run
    loop agreeing with each other — they all ask this one question.
    """
    if cutoff_pct is None:
        cutoff_pct = UPSCALE_CUTOFF_PCT

    if os.path.splitext(path)[1].lower() in RAW_EXTS:
        return False, ""

    w, h = get_image_dimensions(path)
    if w == 0 and h == 0:
        return False, ""

    skip, reason = _skip_for_dims(w, h, cutoff_pct)
    if skip and AUTO_STRAIGHTEN and not _STRAIGHTEN_DISABLED:
        skip_transposed, _ = _skip_for_dims(h, w, cutoff_pct)
        if not skip_transposed:
            return False, ""    # may become eligible once rotated upright
    return skip, reason


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
#  AUTO-STRAIGHTEN (rotate sideways photos upright BEFORE upscaling)
#  Mirrors Tag & Rename's straighten path (same model, threshold and policy),
#  but never touches the source: it rotates a temp copy and upscales that.
# ─────────────────────────────────────────────

def warm_up_straighten():
    """Load the orientation model up-front (downloads ~82 MB once) so the first
    image doesn't pay the cost mid-run, and so the eligibility check below knows
    whether straightening is actually available. Fails safe: any problem just
    disables straightening and upscaling proceeds without rotation."""
    global _STRAIGHTEN_DISABLED, REMOTE_STRAIGHTEN
    if not AUTO_STRAIGHTEN:
        return
    # In REMOTE mode the orientation CNN runs ON THE POD (option B): the worker's
    # /orient endpoint owns the only torch-dependent step. The local side never
    # imports torch — which both keeps the offload honest and sidesteps the
    # OpenBLAS/numpy loader-lock DEADLOCK that froze the GUI when it tried to load
    # the model locally (confirmed via py-spy's native stack). detect_rotation
    # sends a thumbnail to the pod and rotates a temp copy with pure PIL; the
    # skip / resolution math is unchanged. Straighten stays ENABLED.
    if os.environ.get("IMGTBX_UPSCALE_REMOTE") == "1":
        REMOTE_STRAIGHTEN = True
        print("  Auto-straighten: orientation CNN runs on the pod "
              "(local stays torch-free); rotating + uploading copies locally.")
        return
    try:
        import orientation
        if not orientation.is_available():
            print("  Auto-straighten: torch/timm not available — upscaling without rotation.")
            _STRAIGHTEN_DISABLED = True
            return
        print("  Auto-straighten: loading orientation model "
              "(first run downloads ~82 MB) ...")
        # Import the heavy NATIVE libs in the MAIN thread first. Importing
        # torch/numpy in a background thread can deadlock in numpy's C-extension
        # init — hit in REMOTE mode, where (unlike local) no engine has already
        # imported torch on the main thread, so orientation was the first to do
        # so. Only the model build + CUDA move is then time-boxed on a thread, so
        # a genuinely stuck GPU still can't hang the whole run.
        import numpy, torch, timm        # noqa: F401,E401 — main-thread native import
        import threading
        result = {}

        def _load():
            try:
                orientation._get_model()
                result["ok"] = True
            except Exception as exc:        # noqa: BLE001 — reported below
                result["err"] = exc

        th = threading.Thread(target=_load, daemon=True)
        th.start()
        th.join(STRAIGHTEN_INIT_TIMEOUT)
        if th.is_alive():
            print(f"  Auto-straighten: model load did not finish within "
                  f"{STRAIGHTEN_INIT_TIMEOUT:.0f}s (local GPU stuck?) — upscaling without rotation.")
            _STRAIGHTEN_DISABLED = True
            return
        if result.get("err"):
            raise result["err"]
        print("  Auto-straighten: ready.")
    except Exception as exc:
        print(f"  Auto-straighten: could not initialise ({exc}) — upscaling without rotation.")
        _STRAIGHTEN_DISABLED = True


def detect_rotation(path):
    """Return (degrees, message). `degrees` is the CNN class to correct: 90 or
    270 when the photo is confidently sideways (the caller should upscale a
    rotated copy), otherwise 0 (leave as-is). The message is for the log. Never
    raises — any failure yields (0, reason) so upscaling proceeds un-rotated."""
    if not AUTO_STRAIGHTEN or _STRAIGHTEN_DISABLED:
        return 0, ""
    import orientation                              # torch-free here (policy + PIL)
    try:
        if REMOTE_STRAIGHTEN and ENGINE is not None:
            # The CNN runs on the pod; we send only a thumbnail (no local torch).
            deg, conf = ENGINE.analyse(path)
        else:
            deg, conf = orientation.analyse(path)
    except Exception as exc:
        return 0, f"orientation check skipped: {exc}"

    if not orientation.should_rotate(deg, conf, STRAIGHTEN_CONFIDENCE):
        if deg != 0:
            why = "180deg" if deg == 180 else f"below {STRAIGHTEN_CONFIDENCE:.2f} confidence"
            return 0, f"orientation: {deg}deg @ {conf:.2f} — left as-is ({why})"
        return 0, ""

    return deg, f"straightened before upscale (detected {deg}deg @ {conf:.2f})"


def detect_rotation_image(pil_img):
    """`detect_rotation` for an ALREADY-OPEN image (#19).

    A RAW has no file the CNN can be pointed at: opening one with Pillow returns
    the small preview in IFD 0 for a CR2/NEF/DNG and fails outright for a
    CR3/ORF/RW2, so the rendered image has to be handed over in memory. Routes to
    the pod in remote mode exactly as detect_rotation does, so the two differ only
    in what they are given."""
    if not AUTO_STRAIGHTEN or _STRAIGHTEN_DISABLED:
        return 0, ""
    import orientation
    try:
        if REMOTE_STRAIGHTEN and ENGINE is not None:
            deg, conf = ENGINE.analyse_image(pil_img)
        else:
            deg, conf = orientation.analyse_image(pil_img)
    except Exception as exc:
        return 0, f"orientation check skipped: {exc}"

    if not orientation.should_rotate(deg, conf, STRAIGHTEN_CONFIDENCE):
        if deg != 0:
            why = "180deg" if deg == 180 else f"below {STRAIGHTEN_CONFIDENCE:.2f} confidence"
            return 0, f"orientation: {deg}deg @ {conf:.2f} — left as-is ({why})"
        return 0, ""

    return deg, f"straightened before upscale (detected {deg}deg @ {conf:.2f})"


def _straighten_image(pil_img, degrees):
    """Rotate an in-memory image upright for CNN class `degrees`. Returns the
    rotated image, or the original on any failure (never raises: a rotation that
    cannot be worked out must not cost the picture)."""
    try:
        import orientation
        transpose_const, _cw = orientation._ensure_correct_map()[degrees]
        return pil_img.transpose(transpose_const)
    except Exception as exc:                       # noqa: BLE001
        debug_log("batch_upscale._straighten_image", exc=exc)
        return pil_img


def _save_render(pil_img, dest_path, exif_blob):
    """Write a rendered RAW to `dest_path` as JPEG, atomically.

    Quality 95 with chroma subsampling disabled, matching
    upscale_engine._save_image, so a rendered-only file and an upscaled one are
    encoded identically and a user never has to know which path a photo took.

    The metadata block is sanitised through exif_copy, NOT written raw: the
    pixels handed in here are already upright (LibRaw's flip applied during the
    render, auto-straighten possibly on top), so the source's Orientation would
    make every viewer rotate an upright photo a second time - the same trap #13
    fixed for the upscale path, arrived at from a different direction.
    """
    meta = {}
    try:
        import exif_copy
        blob = exif_copy.exif_for_upscaled_blob(exif_blob, dest_path, pil_img.size)
        meta = exif_copy.save_kwargs(dest_path, blob)
    except Exception as exc:                       # noqa: BLE001 (module absent / odd block)
        debug_log("batch_upscale._save_render: metadata skipped", exc=exc)

    tmp_path = dest_path + ".tmp.jpg"
    try:
        try:
            pil_img.save(tmp_path, "jpeg", quality=95, subsampling=0,
                         optimize=True, **meta)
        except Exception as exc:                   # noqa: BLE001
            # The metadata is a bonus, the image is the product (the same rule
            # upscale_engine._save_image follows).
            if not meta:
                raise
            debug_log("batch_upscale._save_render: retrying without metadata", exc=exc)
            pil_img.save(tmp_path, "jpeg", quality=95, subsampling=0, optimize=True)
        os.replace(tmp_path, dest_path)
    except Exception:
        _safe_remove(tmp_path)
        raise


def _write_upscale_input(pil_img, exif_blob):
    """Write the rendered RAW to ONE temp file for the upscaler to read, and
    return its path.

    PNG, never JPEG. The whole point of rendering from RAW is to avoid the
    generational loss the app exists to undo, and a JPEG temp here would spend a
    generation before SeedVR2 sees a pixel - the easy accident, because `.jpg` is
    what the output is. compress_level=1 because this file lives for one image:
    deflate effort buys nothing and costs seconds on a 24 MP frame.

    The EXIF is sanitised before it is written (Orientation forced to 1), which
    is load-bearing rather than tidy: upscale_engine._load_image runs
    ImageOps.exif_transpose on whatever it is given, so a temp carrying the
    camera's original Orientation would have its already-upright pixels rotated
    again. Carrying the block at all is what lets the REMOTE path work unchanged -
    the pod receives an ordinary PNG with metadata and needs to know nothing
    about RAW.
    """
    import tempfile
    fd, tmp = tempfile.mkstemp(suffix=".png", prefix="itbx_raw_")
    os.close(fd)
    try:
        meta = {}
        try:
            import exif_copy
            blob = exif_copy.exif_for_upscaled_blob(exif_blob, tmp, pil_img.size)
            meta = exif_copy.save_kwargs(tmp, blob)
        except Exception as exc:                   # noqa: BLE001
            debug_log("batch_upscale._write_upscale_input: metadata skipped", exc=exc)
        try:
            pil_img.save(tmp, "png", compress_level=1, **meta)
        except Exception as exc:                   # noqa: BLE001
            if not meta:
                raise
            debug_log("batch_upscale._write_upscale_input: retrying without metadata",
                      exc=exc)
            pil_img.save(tmp, "png", compress_level=1)
        return tmp
    except Exception:
        _safe_remove(tmp)
        raise


def _make_straightened_copy(src_path, degrees):
    """Copy `src_path` to a temp file and rotate that copy upright for CNN class
    `degrees`. The source is never modified. Returns the temp path, or None on
    failure (the caller then upscales the original, un-rotated)."""
    import tempfile
    import shutil
    tmp = None
    try:
        ext = os.path.splitext(src_path)[1] or ".png"
        fd, tmp = tempfile.mkstemp(suffix=ext, prefix="itbx_straighten_")
        os.close(fd)
        shutil.copy2(src_path, tmp)
        import orientation
        orientation.straighten(tmp, degrees)   # rotates the COPY, clears EXIF orient
        return tmp
    except Exception:
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass
        return None


def _safe_remove(path):
    try:
        os.remove(path)
    except OSError:
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
        # Optional hooks, run by .check() on the MAIN thread around the blocking
        # wait: on_pause when the run goes to sleep, on_resume when it wakes.
        # main() uses them to unload the models so a paused run frees the GPU.
        # They must NOT be called from the watcher thread — it fires while the
        # main thread is mid-upscale, and touching the engine there would race
        # a live CUDA workload. At .check() time the engine is idle by
        # construction (it is called between images).
        self.on_pause        = None
        self.on_resume       = None
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
                elif cmd == "qstop":          # quit + stop the remote pod now
                    _set_remote_teardown("stop")
                    self._handle_key("q")
                elif cmd == "qkeep":          # quit but leave the remote pod running
                    _set_remote_teardown("keep")
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
        # Hand the GPU back before sleeping (the point of Pause), then just sleep
        # until resumed or quit, no further output.
        self._fire(self.on_pause)
        try:
            while True:
                with self._lock:
                    if self._quit:
                        return False
                    if not self._paused:
                        return True
                time.sleep(0.5)
        finally:
            # Reload only when the run actually continues. On quit the loop is
            # about to break and the engine is never used again, so paying a
            # cold model load there would just delay the shutdown.
            if not self._quit:
                self._fire(self.on_resume)

    @staticmethod
    def _fire(hook):
        """Run a pause hook, never letting it break the run."""
        if hook is None:
            return
        try:
            hook()
        except Exception as exc:                       # noqa: BLE001
            debug_log("PauseController hook failed", exc=exc)

    def force_pause(self):
        """Programmatically enter the paused state (as if the user pressed Pause),
        so a following .check() blocks until Resume/Stop. Used by the outage
        handler, which must hold the run open until the user acts. Idempotent: a
        no-op if already paused, so it never clobbers a running pause's start time
        (which would corrupt the paused_seconds accounting)."""
        with self._lock:
            if not self._paused:
                self._paused      = True
                self._pause_start = time.time()

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
    # Never walk into folders this app produced (#16). Pruning THIS run's output
    # root by path is not enough: an earlier run with a different output folder,
    # and Conciliation's __Archive__ (which holds the only copies still below the
    # target, so they all look eligible), are both inside the source tree.
    pruner = runner_common.DerivedPruner(_CFG)

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
        pruner.prune(dirnames)          # …and any other derived folder (#16)

        folder_count += 1

        # Mirror the source subdirectory structure under output_root
        rel_dir    = os.path.relpath(dirpath, root)
        output_dir = os.path.normpath(os.path.join(output_root, rel_dir))

        for filename in sorted(filenames):
            ext = os.path.splitext(filename)[1].lower()
            if ext not in INPUT_EXTS:
                continue
            local_path = os.path.join(dirpath, filename)
            if local_path in already_done:
                continue
            stem     = os.path.splitext(filename)[0]
            # Two formats cannot keep their own extension, for the same reason
            # in two costumes: the output is a different format, and a sibling
            # of that format would collide with it. A RAW becomes
            # `<stem>_raw.jpg` (#19, raw_decode.render_name); a GIF becomes
            # `<stem>_gif.png` (#27, runner_common.gif_output_name), because
            # writing it back as GIF would re-quantise the result to 256
            # colours. Everything else keeps its stem and a lowercased
            # extension, which is the mirrored name every inverse relies on.
            if ext in RAW_EXTS:
                out_name = raw_decode.render_name(filename)
            elif ext == ".gif":
                out_name = runner_common.gif_output_name(filename)
            else:
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

    _pruned = pruner.summary()
    if _pruned:
        print(f"  {_pruned}")

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
    variant = folder_stats[dirpath]["skipped_variant"]
    if done == 0 and large == 0 and missing == 0 and corrupt == 0 and variant == 0:
        return
    parts = []
    if done    > 0: parts.append(f"{done} already done")
    if large   > 0: parts.append(f"{large} too large")
    if variant > 0: parts.append(f"{variant} left as-is (would lose data)")
    if missing > 0: parts.append(f"{missing} no longer exist")
    if corrupt > 0: parts.append(f"{corrupt} corrupted/unreadable")
    summary = ", ".join(parts)
    if missing > 0 or corrupt > 0:
        logger.tee(f"  [skipped {summary}]")
    else:
        logger.terminal_only(f"  [skipped {summary}]")


# Per-folder counters accumulated during a pass. Built from ONE factory so
# run_pass and the two-pass merge() can't drift: merge()'s template used to omit
# "skipped_corrupt", so `merged[d]["skipped_corrupt"] += ...` KeyError'd and took
# down the whole end-of-run summary (table + DONE event + MQTT + notification) on
# any run that had a rescan second pass. See item 4.
_FOLDER_STAT_KEYS = ("processed", "rendered", "skipped_done", "skipped_size",
                     "skipped_variant", "skipped_missing", "skipped_corrupt",
                     "failed")


def _new_folder_stats():
    stats = {k: 0 for k in _FOLDER_STAT_KEYS}
    stats["elapsed"] = 0.0
    return stats


def _merge_pass_stats(s1, s2):
    """Combine the stats dicts from the first pass and the rescan second pass into
    one for the end-of-run summary. `s2` is None when there was no second pass
    (returns `s1` unchanged). Pure and module-level so it is unit-testable; the
    per-run flags (user_quit/degraded/remote_stopped) are deliberately NOT merged —
    the caller reads those from the individual passes. See item 4."""
    if s2 is None:
        return s1
    merged = defaultdict(_new_folder_stats)
    for d, v in s1["folder_stats"].items():
        for k in v:
            merged[d][k] += v[k]
    for d, v in s2["folder_stats"].items():
        for k in v:
            merged[d][k] += v[k]
    # Totals are summed through .get(): adding a counter (#17's
    # total_skipped_variant was the first) must never be able to KeyError the
    # whole end-of-run summary again, which is the failure this function's
    # _FOLDER_STAT_KEYS factory was written to stop.
    out = {"folder_stats": merged}
    for key in ("total_processed", "total_rendered", "total_skipped_done",
                "total_skipped_size", "total_skipped_variant",
                "total_skipped_missing", "total_skipped_corrupt", "total_failed"):
        out[key] = s1.get(key, 0) + s2.get(key, 0)
    for key in ("corrupt_files", "variant_files"):
        out[key] = list(s1.get(key, [])) + list(s2.get(key, []))
    return out


def run_pass(work_items, root, output_root, grand_start, pause, logger,
             processed_paths, cache=None, pass_label="Pass"):
    """
    Process a list of work items. Returns a stats dict.
    Adds each successfully found local_path to processed_paths so the rescan
    can exclude already-attempted files regardless of outcome.
    """
    consecutive_failures = 0

    # ── Performance watchdog state (0.3.0) ───────────────────────────────────
    # min_spmp = the run's best (lowest) seconds-per-output-megapixel = the GPU's
    # healthy throughput; the baseline is the running minimum so a creeping ramp
    # can't drift it up. slow_streak counts consecutive degraded images;
    # degraded_stop breaks out after the current image (temp copy cleaned up by
    # the finally first).
    min_spmp     = None
    samples_seen = 0
    slow_streak  = 0
    degraded_stop = False
    remote_stopped = False    # remote run: the pod's dead-man's switch stopped it

    def _trigger_degradation(reason, image, idx, total, grand_elapsed):
        """Announce a degradation episode (GUI + Discord) and stop the run.

        Called once per episode (the loop breaks straight after), so the
        notification is naturally edge-triggered — no per-slow-image spam."""
        logger.tee("  WARNING: PERFORMANCE DEGRADATION DETECTED -- stopping after this image.")
        logger.tee(f"  {reason}")
        logger.tee("  A Windows reboot is the known cure; the resume cache will pick the queue back up.")
        _gui_event("DEGRADED", json.dumps({
            "reason": reason, "image": image, "idx": idx, "total": total,
        }))
        send_notification(
            title       = "Upscale Script -- Performance Degradation Detected",
            description = reason,
            color       = notifications.COLOR_ORANGE,
            fields      = [
                {"name": "Last image",    "value": image},
                {"name": "Progress",      "value": f"{idx}/{total}"},
                {"name": "Total elapsed", "value": fmt_hhmmss(grand_elapsed)},
                {"name": "Machine",       "value": os.environ.get("COMPUTERNAME", "unknown")},
            ],
        )

    # GUI preview strip: announce the full ordered queue for this pass
    if GUI_MODE:
        _gui_event("QUEUE", json.dumps([item[1] for item in work_items]))

    folder_stats = defaultdict(_new_folder_stats)

    total_processed       = 0
    total_rendered        = 0      # RAW written at native size, no upscale (#19)
    total_skipped_done    = 0
    total_skipped_size    = 0
    total_skipped_variant = 0
    total_skipped_missing = 0
    total_skipped_corrupt = 0
    total_failed          = 0
    corrupt_files         = []      # absolute paths of unreadable/corrupt images
    variant_files         = []      # (path, reason) for images left as-is (#17)
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

        # ── Can the engine even round-trip this image? (#17) ─────────────────
        # Checked BEFORE the already-done branch on purpose: whether some earlier
        # (pre-0.5.9) run left a flattened output next to it does not change the
        # fact that this is a file we do not process. Reporting it here is also
        # how a user finds the outputs that run produced.
        _variant = image_variant_reason(local_path)
        if _variant:
            folder_stats[dirpath]["skipped_variant"] += 1
            total_skipped_variant += 1
            variant_files.append((local_path, _variant))
            logger.log_only(f"  {prefix} SKIP ({_variant})  {local_path}", timestamp=True)
            # Persist it so the next run classifies it from the cache instead of
            # re-opening the file (and so a pre-#17 "eligible" entry is corrected).
            if cache is not None:
                cache.set(local_path, eligible=False, already_done=False,
                          skip_reason=_variant)
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
            # An upscaled counterpart already exists → green (comparable). The
            # third element is the output path, so a double-click can compare.
            _gui_event("RESULT", json.dumps([local_path, "ok", out_path]))
            continue

        is_raw = os.path.splitext(local_path)[1].lower() in RAW_EXTS

        # ── Too large? ───────────────────────────────────────────────────────
        # A RAW is deliberately exempt from the early size skip (#19). For every
        # other format an over-target file needs nothing done to it: it already
        # displays. A RAW displays NOWHERE - not in a browser, not on a TV, not
        # in this app's own film strip - so the render IS the deliverable and
        # skipping it would produce nothing at all. Measured on 24 real camera
        # files: at the shipped 4K target and 66% cutoff, ZERO of them would ever
        # have been upscaled (docs/raw-preview-survey.csv), because RAW is by
        # nature a high-resolution format and this app targets low-resolution
        # photos. The size check still runs below, on the true upright
        # dimensions; it just decides render-only vs render-then-upscale instead
        # of do-something vs do-nothing.
        if not is_raw:
            skip, reason = should_skip_resolution(local_path)
            if skip:
                folder_stats[dirpath]["skipped_size"] += 1
                total_skipped_size += 1
                continue

        # ── Process ──────────────────────────────────────────────────────────
        raw_w, raw_h = get_image_dimensions(local_path)
        img_start    = time.time()

        # Unreadable dimensions = corrupted/unsupported file — skip without
        # counting as an engine failure or incrementing the outage counter.
        if raw_w == 0 or raw_h == 0:
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

        # ── Auto-straighten: detect orientation, work out the upright size ────
        # The upright dimensions (stored size transposed for a 90/270 photo)
        # drive both the skip decision and the SeedVR2 target, so the output
        # fits a 4K screen once rotated. The source is never modified — a temp
        # copy is rotated and upscaled, then removed in the finally below.
        #
        # A RAW takes the other branch: decode ONCE, here, and carry the picture
        # in memory through the rest of the chain (render -> straighten -> upscale,
        # and its upright size comes from the real pixels). The alternative,
        # a temp file per stage, would put a RAW through several lossy
        # generations before SeedVR2 ever sees a pixel — a self-inflicted version
        # of exactly the degradation this app exists to undo. Exactly one temp is
        # written, further down, and only when the file is actually upscaled.
        render = None
        if is_raw:
            try:
                render = raw_decode.render(local_path)
            except Exception as exc:                 # noqa: BLE001
                import datetime as _dt
                _ts = _dt.datetime.now().strftime("%Y-%m-%d | %H:%M:%S")
                print(f"{_ts} |   {prefix} SKIP (RAW unreadable) {_osc8_link(local_path)}")
                logger.log_only(f"  {prefix} SKIP (RAW could not be decoded: {exc})  "
                                f"{local_path}", timestamp=True)
                folder_stats[dirpath]["skipped_corrupt"] += 1
                total_skipped_corrupt += 1
                corrupt_files.append(local_path)
                continue
            rot_deg, rot_msg = detect_rotation_image(render.image)
            rotate           = rot_deg in (90, 270)
            if rotate:
                render.image = _straighten_image(render.image, rot_deg)
            w, h = render.image.size                 # authoritative: the real pixels
        else:
            rot_deg, rot_msg = detect_rotation(local_path)
            rotate           = rot_deg in (90, 270)
            w, h             = (raw_h, raw_w) if rotate else (raw_w, raw_h)

        # Precise, orientation-aware skip against the true upright dimensions.
        # The scan pre-check (should_skip_resolution) is deliberately lenient —
        # it keeps an image eligible if EITHER orientation would be processed —
        # so the authoritative decision has to be made here, now the real
        # orientation is known, for both the rotated and upright cases.
        skip, reason = _skip_for_dims(w, h, UPSCALE_CUTOFF_PCT)
        # For a RAW the same verdict means something different: not "do nothing"
        # but "the render is already big enough, write it and stop there".
        render_only = bool(skip and is_raw)
        if skip and not is_raw:
            folder_stats[dirpath]["skipped_size"] += 1
            total_skipped_size += 1
            note = " (upright, after straighten)" if rotate else ""
            logger.log_only(f"  {prefix} SKIP ({reason}){note}  {local_path}", timestamp=True)
            continue

        # Build the upscale source: a rotated temp copy when straightening, or
        # the single lossless temp the RAW chain writes at its end.
        src_path = local_path
        tmp_path = None
        if is_raw:
            if not render_only:
                try:
                    tmp_path = _write_upscale_input(render.image, render.exif)
                    src_path = tmp_path
                except Exception as exc:             # noqa: BLE001
                    # Nothing has been written yet, so fall back to the render:
                    # a viewable JPEG at native size beats no output at all.
                    debug_log("batch_upscale: RAW temp write failed", exc=exc)
                    logger.log_only(f"  {prefix} could not stage the RAW for upscaling "
                                    f"({exc}) — writing the render as-is", timestamp=True)
                    render_only = True
        elif rotate:
            tmp_path = _make_straightened_copy(local_path, rot_deg)
            if tmp_path is None:
                rotate  = False                      # rotation failed — upscale as-is
                w, h    = raw_w, raw_h
                rot_msg = "straighten FAILED — upscaled without rotation"
            else:
                src_path = tmp_path

        if render_only:
            out_w, out_h = w, h
            dim_str = (f"{raw_w}x{raw_h}px ↻ {w}x{h}px (render only, {render.how})"
                       if rotate else f"{w}x{h}px (render only, {render.how})")
        else:
            scale   = min(MAX_RESOLUTION / w, RESOLUTION / h)
            out_w   = round(w * scale)
            out_h   = round(h * scale)
            dim_str = (f"{raw_w}x{raw_h}px ↻ {w}x{h}px -> {out_w}x{out_h}px"
                       if rotate else f"{w}x{h}px -> {out_w}x{out_h}px")
            if is_raw:
                dim_str = f"{dim_str} ({render.how})"

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
            if render_only:
                _save_render(render.image, out_path, render.exif)
            else:
                ENGINE.upscale(src_path, out_path, compute_seedvr2_resolution(w, h))

            img_elapsed   = time.time() - img_start
            grand_elapsed = time.time() - grand_start - pause.paused_seconds
            timing          = f" | {fmt_mmss(img_elapsed)} | Total: {fmt_hhmmss(grand_elapsed)}"
            out_full        = os.path.join(output_dir, out_name)
            out_folder_link = _osc8_link(output_dir).replace(output_dir, "📁")
            checkmark       = _osc8_link(out_full).replace(out_full, "✓")
            sys.stdout.write(f" {out_folder_link} {checkmark}{timing}\n")
            sys.stdout.flush()
            logger.log_only(f"  {prefix} {dim_str}  {local_path} 📁 ✓{timing}", timestamp=True)
            if rot_msg:
                logger.log_only(f"           {rot_msg}")

            consecutive_failures = 0
            processed_paths.add(local_path)
            # Green + output path so a double-click can compare original↔upscaled.
            # A RAW gets "ok" with NO output path on purpose, whether it was
            # upscaled or only rendered. The comparison window draws the ORIGINAL
            # as its "before" half, and the original here is a RAW: Pillow cannot
            # open a CR3/ORF/RW2 at all and answers a CR2/NEF/DNG with the small
            # preview in IFD 0. Beyond that, for a render-only file there is no
            # before/after to show in the first place - the output IS the first
            # viewable version this photo has ever had.
            _gui_event("RESULT", json.dumps([local_path, "ok"] if is_raw
                                            else [local_path, "ok", out_path]))
            if cache is not None:
                cache.mark_done(local_path)
                cache.record_lineage(local_path, out_path)
                cache.save()
            if render_only:
                # Counted apart from `processed`, and deliberately excluded from
                # BOTH the ETA average and the watchdog below. A render is ~1000x
                # faster than an upscale (measured: 0.3 s vs minutes), so folding
                # the two together would collapse the ETA's per-image average and,
                # worse, hand the watchdog a "healthy rate" no real upscale could
                # ever match — making it trip on the first genuine image.
                folder_stats[dirpath]["rendered"] += 1
                total_rendered += 1
            else:
                folder_stats[dirpath]["processed"] += 1
                total_processed += 1
                # GUI ETA: pause-excluded elapsed, images actually processed this
                # session, current position and total. The trailing 'P' marks this
                # as the real image-PROCESSING phase (scan/verify/eligibility ETAs
                # omit it), so the GUI only tracks cost while images are actually
                # upscaled. The GUI averages over the processed count (NOT the
                # counter, which also advances on skips).
                _gui_event("ETA", f"{grand_elapsed:.3f}|{total_processed}|{idx}|{total}|P")

            # ── Performance watchdog: degradation detection ──────────────────
            # Seconds per output megapixel vs the run's healthy minimum. The
            # minimum can only be set by genuinely fast images, so a slow ramp
            # can't poison it (unlike a rolling average), and per-MP normalisation
            # keeps the comparison fair across mixed resolutions. Trip on a
            # sustained jump to >= WATCHDOG_FACTOR x that floor.
            if WATCHDOG_ENABLED and not render_only:
                out_mp = max((out_w * out_h) / 1_000_000.0, 1e-6)
                spmp   = img_elapsed / out_mp
                samples_seen += 1
                if min_spmp is None or spmp < min_spmp:
                    min_spmp = spmp
                if samples_seen >= WATCHDOG_MIN_SAMPLES and spmp >= min_spmp * WATCHDOG_FACTOR:
                    slow_streak += 1
                    ratio = spmp / min_spmp
                    logger.tee(
                        f"  WATCHDOG: image #{idx} took {fmt_mmss(img_elapsed)} for "
                        f"{out_w}x{out_h} (~{ratio:.1f}x the healthy rate) "
                        f"[{slow_streak}/{WATCHDOG_CONSECUTIVE}]")
                    if slow_streak >= WATCHDOG_CONSECUTIVE:
                        _trigger_degradation(
                            reason=(f"Per-image upscale time degraded to ~{ratio:.1f}x the "
                                    f"healthy rate ({fmt_mmss(img_elapsed)} for {out_w}x{out_h}) "
                                    f"for {slow_streak} consecutive images. The GPU is likely "
                                    f"thrashing VRAM into system memory."),
                            image=local_path, idx=idx, total=total,
                            grand_elapsed=grand_elapsed)
                        degraded_stop = True
                else:
                    slow_streak = 0

        except Exception as e:
            img_elapsed        = time.time() - img_start
            grand_elapsed      = time.time() - grand_start - pause.paused_seconds
            consecutive_failures += 1
            timing = f" | FAILED {fmt_mmss(img_elapsed)} | Total: {fmt_hhmmss(grand_elapsed)} -- {e}"
            sys.stdout.write(timing + "\n")
            sys.stdout.flush()
            logger.log_only(f"  {prefix} {dim_str}  {local_path}{timing}", timestamp=True)
            _gui_event("RESULT", json.dumps([local_path, "fail"]))   # strip: red
            folder_stats[dirpath]["failed"] += 1
            total_failed += 1

            # A hard OOM is the degraded-GPU symptom in its fail-fast form (the
            # "Prefer No Sysmem Fallback" case). Treat it as a degradation episode
            # immediately — auto-stop rather than waiting for OUTAGE_THRESHOLD.
            if WATCHDOG_ENABLED and _is_oom_error(e):
                _trigger_degradation(
                    reason=(f"GPU ran out of memory on "
                            f"'{os.path.basename(local_path)}'. The pipeline has "
                            f"likely degraded (VRAM fragmentation / sysmem-fallback "
                            f"OOM). Last error: {e}"),
                    image=local_path, idx=idx, total=total,
                    grand_elapsed=grand_elapsed)
                degraded_stop = True

            elif _remote_pod_stopped():
                # Remote run: the upscale failed AND the pod is no longer running —
                # its dead-man's switch fired (idle / max-runtime) or it was
                # stopped. This is NOT a recoverable local outage, so end the run
                # cleanly; the resume cache continues the queue on the next run.
                logger.tee("  The remote pod has stopped — its dead-man's switch "
                           "reached the idle / max-runtime limit (or it was "
                           "stopped). Ending this run cleanly; the resume cache "
                           "will continue the queue next time.")
                _gui_event("STATUS", "Remote pod stopped — ending the run (resume cache saved).")
                send_notification(
                    title       = "Upscale -- Remote pod stopped",
                    description = ("The remote pod's dead-man's switch fired (idle "
                                   "or max-runtime), or the pod was stopped. The run "
                                   "ended cleanly — the resume cache continues the "
                                   "queue on the next run."),
                    color       = notifications.COLOR_ORANGE,
                    fields      = [
                        {"name": "Progress",      "value": f"{idx}/{total}"},
                        {"name": "Total elapsed", "value": fmt_hhmmss(grand_elapsed)},
                        {"name": "Machine",       "value": os.environ.get("COMPUTERNAME", "unknown")},
                    ]
                )
                remote_stopped = True

            elif consecutive_failures >= OUTAGE_THRESHOLD:
                outage_msg = (
                    f"{consecutive_failures} consecutive image(s) failed. "
                    f"The GPU pipeline may be in a bad state (out of VRAM, "
                    f"driver issue) or these files may be unreadable.\n"
                    f"Last error: {e}"
                )
                logger.tee(f"  WARNING: OUTAGE DETECTED ({consecutive_failures} consecutive failures) -- pausing.")
                logger.tee("  Free up VRAM / check the log, then use Resume in the app "
                           "to continue (or Stop to end the run).")

                send_notification(
                    title       = "Upscale Script -- Repeated Failures Detected",
                    description = outage_msg,
                    color       = notifications.COLOR_RED,
                    fields      = [
                        {"name": "Last failed image", "value": local_path},
                        {"name": "Progress",          "value": f"{idx}/{total}"},
                        {"name": "Total elapsed",     "value": fmt_hhmmss(grand_elapsed)},
                        {"name": "Machine",           "value": os.environ.get("COMPUTERNAME", "unknown")},
                    ]
                )

                pause.force_pause()
                consecutive_failures = 0

                if not pause.check():
                    logger.tee("  Stopping at user request.")
                    break

                send_notification(
                    title       = "Upscale Script -- Resumed",
                    description = "Script resumed after outage pause.",
                    color       = notifications.COLOR_GREEN,
                    fields      = [
                        {"name": "Progress",      "value": f"{idx}/{total}"},
                        {"name": "Total elapsed", "value": fmt_hhmmss(time.time() - grand_start - pause.paused_seconds)},
                    ]
                )

            if not (degraded_stop or remote_stopped):
                continue
            # else: fall through so the finally cleans up, then break below.

        finally:
            # Always remove the rotated temp copy, whether the upscale succeeded,
            # failed, or the loop is about to break for an outage pause / stop.
            if tmp_path:
                _safe_remove(tmp_path)

        # Stop the run cleanly after this image when the performance watchdog
        # tripped (degraded GPU — resume after a reboot) or the remote pod stopped
        # (dead-man's switch — resume on the next run).
        if degraded_stop or remote_stopped:
            break

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
        "total_rendered":        total_rendered,
        "total_skipped_done":    total_skipped_done,
        "total_skipped_size":    total_skipped_size,
        "total_skipped_variant": total_skipped_variant,
        "total_skipped_missing": total_skipped_missing,
        "total_skipped_corrupt": total_skipped_corrupt,
        "total_failed":          total_failed,
        "corrupt_files":         corrupt_files,
        "variant_files":         variant_files,
        "user_quit":             getattr(pause, "quit_requested", False),
        "degraded":              degraded_stop,
        "remote_stopped":        remote_stopped,
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
    global ENGINE, REMOTE_SESSION
    # Remote mode (roadmap #1): the GUI sets IMGTBX_UPSCALE_REMOTE=1 to run the
    # GPU work on a RunPod pod. The queue, resume cache, skip logic and the
    # performance watchdog all stay LOCAL — only the per-image upscale is remote.
    REMOTE = os.environ.get("IMGTBX_UPSCALE_REMOTE") == "1"
    try:
        if REMOTE:
            import atexit
            from remote_run import RemoteSession

            def _remote_status(msg):
                print(f"  [remote] {msg}", flush=True)
                _gui_event("STATUS", msg)

            _gui_event("STATUS", "Starting remote pod …")
            # Dev-only: IMGTBX_REMOTE_ATTACH="pod_id,host,ssh_port" reuses a
            # running pod instead of creating one (no teardown on close).
            _attach = None
            _att = os.environ.get("IMGTBX_REMOTE_ATTACH")
            if _att:
                _p = _att.split(",")
                _attach = (_p[0], _p[1], int(_p[2]))
            session = RemoteSession(_CFG.get("runpod", {}), _U, APP_ROOT,
                                    on_event=_remote_status, attach=_attach)
            ENGINE = session.start()
            REMOTE_SESSION = session   # lets run_pass detect a dead-man's-switch stop
            # Tell the GUI which pod is live, so the RunPod tab won't offer to
            # terminate the pod this run depends on (cleared when the run exits).
            _gui_event("POD", session.pod_id or "")
            # The pod's real billed rate ($/h) drives the GUI's live cost readout.
            if session.cost_per_hr is not None:
                _gui_event("RCOST", f"{session.cost_per_hr}")
            # Guarantee teardown on any normal/sys.exit path; the on-pod
            # dead-man's switch is the backup if the process is hard-killed.
            # The GUI's "Stop" modal can override the teardown (stop the pod now
            # even if reused, or leave it running) via _REMOTE_TEARDOWN.
            def _remote_teardown():
                session.close(stop_pod={"stop": True, "keep": False}.get(_REMOTE_TEARDOWN))
            atexit.register(_remote_teardown)
            # Stream the pod's CPU/RAM/GPU to the GUI's remote telemetry row.
            _start_remote_telemetry(ENGINE)
        else:
            from upscale_engine import UpscaleEngine
            # debug=true in config.json's "upscale" section restores the verbose
            # SeedVR2 pipeline output in the terminal.
            ENGINE = UpscaleEngine(SEEDVR2_REPO_DIR, SEEDVR2_MODEL_DIR, _U,
                                   debug=bool(_U.get("debug", False)))
    except Exception as e:
        where = "remote pod" if REMOTE else "SeedVR2 engine"
        print(f"ERROR: Could not initialize the {where}.\n  -> {e}")
        if not REMOTE:
            print("Make sure this script runs inside the toolbox venv, e.g.:")
            print(f"  {os.path.join(APP_ROOT, '.venv', 'Scripts', 'python.exe')} scripts\\batch_upscale.py <source_dir>")
        send_notification(
            title       = "Upscale Script -- Engine Failed to Start",
            description = f"Could not initialize the {where}.\n{e}",
            color       = notifications.COLOR_RED,
            fields      = [{"name": "Machine", "value": os.environ.get("COMPUTERNAME", "unknown")}],
        )
        sys.exit(1)
    print(f"  Engine ready on {ENGINE.device_name}.")
    # Say it once per run rather than per image (#13a). The engine reads the same
    # key on the pod, so a remote run reports it the same way.
    if COPY_METADATA:
        print("  Metadata: the original's capture date, camera and GPS are copied "
              "onto each upscaled image.")
    else:
        print("  Metadata: NOT copied (upscaled images will have none).")

    # Created before the long preparation phases so a quit request ("q" from
    # the GUI, or Q on the keyboard) can cancel scanning / eligibility checks.
    pause = PauseController()

    def _cancelled():
        return pause.quit_requested

    logger = Logger(root)
    # SeedVR2 pipeline output (phase banners, progress bars) goes to the log
    # file only — the terminal shows just the per-image status lines.
    ENGINE.log_sink = logger._fh

    # ── Pause frees the GPU ──────────────────────────────────────────────────
    # Pausing exists so the user can go do something else that needs the card
    # (a game, another AI tool) and come back to the queue where it stood.
    # Merely stopping the loop would not achieve that: the models stay loaded
    # and their VRAM stays held, so the pause only frees the GPU if we actually
    # unload. Resuming reloads them and continues at the next image, so the
    # scan / eligibility work done so far is never repeated.
    #
    # Local runs only. On a remote run the models sit on the rented pod, so
    # unloading frees nothing on this PC and would just pay a reload on a
    # billed-by-the-hour machine.
    if not REMOTE:
        def _pause_release():
            before = ENGINE.vram_used_gb()
            _gui_event("STATUS", "Paused — releasing the GPU …")
            ENGINE.release()
            # Free EVERY model, not just the big one. Auto-straighten's CNN is
            # tiny beside SeedVR2, but a pause that leaves something resident is
            # an exception someone has to remember; there are none.
            try:
                import orientation
                if orientation.unload():
                    logger.tee("  Straighten model unloaded.")
            except Exception as exc:                   # noqa: BLE001
                debug_log("batch_upscale: orientation unload failed", exc=exc)
            after = ENGINE.vram_used_gb()
            if before is not None and after is not None:
                logger.tee(f"  GPU released: {before:.1f} GB → {after:.1f} GB "
                           f"VRAM held. The card is free for other apps.")
            else:
                logger.tee("  Models unloaded — the card is free for other apps.")
            _gui_event("STATUS", "Paused — GPU released. Press Resume to continue.")

        def _pause_reload():
            # The models reload lazily on the next image; say so, because that
            # first image after a resume takes noticeably longer than the rest.
            logger.tee("  Resuming — the AI engine reloads on the next image "
                       "(this one takes longer).")
            _gui_event("STATUS", "Resuming — reloading the AI engine …")

        pause.on_pause  = _pause_release
        pause.on_resume = _pause_reload

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

    # Load the orientation model now (main thread). Done before the eligibility
    # check so its conservative skip rule knows whether straightening is actually
    # available, and so the first image isn't delayed by the one-off model load.
    warm_up_straighten()

    logger.tee(f"Checking eligibility (this may take a while) ...")
    _gui_event("STATUS", "Checking image eligibility …")

    # ── Eligibility pre-check (cache-aware) ───────────────────────────────────
    work_items     = []
    pre_done       = 0
    pre_too_large  = 0
    pre_variant    = 0
    variant_list   = []      # (path, reason) - named below, not just counted
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
                # "Done" is only true if the upscaled output is still on disk.
                # If the user deleted it (e.g. to force a re-run), the cached
                # flag is stale — re-evaluate eligibility from the unchanged
                # source so the image gets processed again.
                if os.path.exists(out_path):
                    pre_done += 1
                    continue
                skip, reason = should_skip_resolution(local_path)
                if skip:
                    cache.set(local_path, eligible=False, already_done=False, skip_reason=reason)
                    pre_too_large += 1
                    continue
                cache.set(local_path, eligible=True, already_done=False)
                work_items.append(item)
                continue
            if not cached["eligible"]:
                if is_variant_reason(cached.get("skip_reason")):
                    pre_variant += 1
                    variant_list.append((local_path, cached["skip_reason"]))
                else:
                    pre_too_large += 1
                continue
            # Eligible and not done — add to work list
            work_items.append(item)
            continue

        # Cache miss — do full check and store result
        _variant = image_variant_reason(local_path)
        if _variant:
            cache.set(local_path, eligible=False, already_done=False,
                      skip_reason=_variant)
            pre_variant += 1
            variant_list.append((local_path, _variant))
            continue

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

    _variant_note = f", {pre_variant} left as-is" if pre_variant else ""
    logger.tee(f"Found {len(work_items)} eligible file(s) "
               f"({pre_done} already done, {pre_too_large} too large"
               f"{_variant_note} — {cache_hits}/{total_all} from cache).")

    # Name them here rather than leaving a bare count (#17). Most variants never
    # reach run_pass at all: once cached they are filtered out right here, so this
    # is the only place a re-run can report them.
    if variant_list:
        logger.tee(f"  Left as they are - upscaling would discard part of these "
                   f"images ({len(variant_list)}):")
        for _vp, _vr in variant_list:
            _next = variant_next_step(_vp, _vr)
            logger.log_only(f"    {_vp}  ({_vr}){_next}")
            print(f"    {_osc8_link(_vp)}  ({_vr}){_next}")

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
    elif stats1.get("degraded"):
        logger.tee("")
        logger.tee("  GPU performance degraded — auto-stopped, skipping rescan. "
                   "Reboot and re-run; the resume cache continues the queue.")
    elif stats1.get("remote_stopped"):
        logger.tee("")
        logger.tee("  Remote pod stopped (dead-man's switch) — skipping rescan. "
                   "Re-run to continue; the resume cache picks up the queue.")
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
            cached = cache.get(local_path)
            # #17 first: an image we do not process stays that way whatever sits
            # next to it. Without this, an output left by a pre-0.5.9 run would
            # send it down the already-done branch below, which would overwrite
            # the variant entry with eligible=True and hide it again.
            if cached is not None and is_variant_reason(cached.get("skip_reason")):
                continue
            if cached is None:
                _variant = image_variant_reason(local_path)
                if _variant:
                    cache.set(local_path, eligible=False, already_done=False,
                              skip_reason=_variant)
                    continue
            if os.path.exists(out_path):
                cache.set(local_path, eligible=True, already_done=True)
                continue
            if cached is not None:
                if not cached["eligible"]:
                    continue
                # The output does not exist (checked just above), so any cached
                # "done" flag is stale — clear it and reprocess.
                if cached["already_done"]:
                    cache.set(local_path, eligible=True, already_done=False)
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

    # Merge stats from both passes (module-level _merge_pass_stats, unit-tested).
    combined        = _merge_pass_stats(stats1, stats2)
    folder_stats    = combined["folder_stats"]
    total_processed       = combined["total_processed"]
    total_rendered        = combined.get("total_rendered", 0)
    total_skipped_done    = combined["total_skipped_done"]
    total_skipped_size    = combined["total_skipped_size"]
    total_skipped_variant = combined.get("total_skipped_variant", 0)
    total_skipped_missing = combined["total_skipped_missing"]
    total_skipped_corrupt = combined["total_skipped_corrupt"]
    total_failed          = combined["total_failed"]
    corrupt_files         = combined.get("corrupt_files", [])
    variant_files         = combined.get("variant_files", [])

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
        total_skipped_folder = (stats["skipped_done"] + stats["skipped_size"] +
                                stats["skipped_variant"])
        # A rendered RAW counts under "Processed": the user got a file out of it.
        # The two are split apart only in the one-line summary below, where the
        # distinction (upscaled vs rendered at native size) actually tells them
        # something.
        folder_done  = stats["processed"] + stats.get("rendered", 0)
        folder_total = (folder_done + total_skipped_folder +
                        stats["skipped_corrupt"] + stats["skipped_missing"] +
                        stats["failed"])
        rel_truncated = trunc(rel, col_path)
        plain_row = row.format(rel_truncated, folder_total,
                               folder_done, total_skipped_folder,
                               stats["skipped_corrupt"], stats["failed"],
                               fmt_duration(stats["elapsed"]))
        # Terminal: replace the plain folder name with a clickable link
        linked_folder = _osc8_link(dp).replace(dp, rel_truncated)
        term_row = plain_row.replace(rel_truncated, linked_folder, 1)
        logger.log_only(plain_row)
        print(term_row)

    logger.tee("=" * _w)
    total_skipped = total_skipped_done + total_skipped_size + total_skipped_variant
    total_done  = total_processed + total_rendered
    grand_total = total_done + total_skipped + total_skipped_corrupt + total_skipped_missing + total_failed
    logger.tee(row.format("TOTAL", grand_total, total_done, total_skipped,
                          total_skipped_corrupt, total_failed, fmt_hhmmss(grand_elapsed)))
    logger.tee(sep)
    parts = [f"{total_processed} processed"]
    # Named separately because it is the honest description AND the answer to the
    # question a RAW user will otherwise ask: the file was converted to a viewable
    # JPEG but not enlarged, because it was already at or above the target.
    if total_rendered > 0: parts.append(f"{total_rendered} RAW rendered (already at target)")
    parts += [f"{total_skipped_done} already done", f"{total_skipped_size} too large"]
    if total_skipped_variant > 0: parts.append(f"{total_skipped_variant} left as-is")
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

    # List every image left as-is, with the reason. A bare count would be the
    # same mystery #17 set out to remove, and these are the files a user may want
    # to handle by other means (see docs/dropped-ideas.md).
    if variant_files:
        seen = {}
        for vp, vr in variant_files:
            seen.setdefault(vp, vr)
        logger.tee("")
        logger.tee(sep)
        logger.tee(f"  Left as they are ({len(seen)}) - upscaling would discard part of them:")
        logger.tee("-" * _w)
        for vp, vr in seen.items():
            _next = variant_next_step(vp, vr)
            logger.log_only(f"  {vp}  ({vr}){_next}")
            print(f"  {_osc8_link(vp)}  ({vr}){_next}")
        logger.tee(sep)

    # ── Discord: queue finished / stopped ───────────────────────────────────
    user_quit = bool(stats1.get("user_quit")) or (stats2 is not None and bool(stats2.get("user_quit")))
    degraded  = bool(stats1.get("degraded"))  or (stats2 is not None and bool(stats2.get("degraded")))
    remote_stopped = bool(stats1.get("remote_stopped")) or (stats2 is not None and bool(stats2.get("remote_stopped")))

    # GUI: machine-readable run summary (drives the MQTT last_run topic).
    _gui_event("DONE", json.dumps({
        "tool":            "upscale",
        "processed":       total_processed,
        "skipped":         total_skipped + total_skipped_missing + total_skipped_corrupt,
        "corrupt":         total_skipped_corrupt,
        "failed":          total_failed,
        "elapsed_seconds": round(grand_elapsed, 1),
        "stopped_by_user": user_quit,
        "degraded":        degraded,
        "remote_stopped":  remote_stopped,
    }))
    # Degraded takes precedence: a watchdog OOM also bumps total_failed, so check
    # it first or the run would be mislabelled "Finished with Failures". A remote
    # pod stop is next (its last image also counts as a failure).
    if degraded:
        notif_title, notif_color = ("Upscale Queue -- Auto-Stopped (GPU Degraded)",
                                    notifications.COLOR_ORANGE)
    elif remote_stopped:
        notif_title, notif_color = ("Upscale Queue -- Remote Pod Stopped (resume to continue)",
                                    notifications.COLOR_ORANGE)
    elif total_failed > 0:
        notif_title, notif_color = ("Upscale Queue -- Finished with Failures",
                                    notifications.COLOR_YELLOW)
    elif user_quit:
        notif_title, notif_color = ("Upscale Queue -- Stopped by User",
                                    notifications.COLOR_YELLOW)
    else:
        notif_title, notif_color = ("Upscale Queue -- Finished",
                                    notifications.COLOR_GREEN)
    send_notification(
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
