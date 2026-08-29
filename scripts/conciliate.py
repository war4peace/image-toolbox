"""
conciliate.py
-------------
Media tree conciliation — replace original photos AND videos with their
processed (upscaled, optionally tagged & renamed) counterparts.

Runs in two phases, driven over stdin by toolbox_gui.py (or interactively from
a terminal):

  1. SCAN  — match each original image/video to its processed counterpart.
             Matching prefers content-hash lineage (db.lineage), which is
             independent of paths and so survives moving or renaming either tree.
             IMAGES additionally fall back to mirrored-name matching (via the
             tag/rename cache) when no lineage exists; VIDEOS are matched by
             lineage only (see the video note below). Emits a per-folder preview
             (replaced / no-match / kept). NOTHING on disk is touched here.
  2. RUN   — once the user confirms, perform the chosen operation per matched
             pair:
               archive — move the original into  <original>/__Archive__/<rel>,
                         then move its processed counterpart into the original
                         tree (keeping the processed file's name).
               delete  — delete the original, then move its processed
                         counterpart into the original tree.
             Before either, an IMAGE pair gets the #13b metadata backfill: every
             EXIF field the processed file is missing is copied from the original,
             which is possible here and nowhere else (this is the one moment the
             app holds both files, already matched, and in delete mode the last
             moment the original exists at all). Scan/Preview only COUNTS it.

Video note (roadmap #5): a video is matched by content-hash lineage ONLY — no
name fallback. The Video Upscaler records source<->output lineage on completion
(item 10, on by default), and a video output is named <stem>_<target>.mp4 (a
suffix, often a different container), NOT a mirror of the source name. A name
guess would be unsafe: a *clip* extract (`<base>_<label>_<target>.mp4`) records no
lineage yet shares the source's `<base>_` prefix, so a name match could replace a
whole source with a short clip. Requiring lineage rules that out — an un-lineaged
video (a clip, or one upscaled with record_lineage off) has no match and is left
untouched. A video is only ever acted on when its processed output is actually
present in the chosen processed tree, so pointing this at an image-only processed
folder never touches a video (and vice versa).

Undo (#18): a Run journals every file action to the DB (db.conc_runs /
conc_actions) BEFORE performing it, so an ARCHIVE run is fully reversible: the
processed files go back to the processed tree and the originals come back out of
__Archive__. A DELETE run is journalled too, but only as evidence of what was
removed: the bytes are gone and no record can bring them back, so undo refuses it
and lists what was deleted instead. Undo verifies each file is still the one the
journal recorded before it moves anything, and works from the disk state, so an
interrupted run unwinds correctly.

Safety rules (non-negotiable):
  * An original file with NO processed counterpart on disk is NEVER touched.
  * Non-media files in the original tree are NEVER touched (counted as "kept").
  * The processed counterpart is moved in only AFTER the original has been
    archived/deleted, so the freed name is available; an unrelated file already
    occupying the destination name aborts that one pair (original left intact).
  * Undo never overwrites: a file that has changed since the run, or a name that
    something else now occupies, is reported as a conflict and left alone.

Usage:
    python conciliate.py <original_dir> <processed_dir> [archive|delete]
    python conciliate.py <original_dir> <processed_dir> --undo   # reverse the last run

GUI control lines (stdin):  run | q
"""

import os
import sys
import json
import time
import shutil
import hashlib
import datetime
import threading

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
import config_store
import notifications
import runner_common

# Make stdout/stderr non-ASCII-proof before any output (see runner_common).
runner_common.harden_stdout()

SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
# App root = parent of scripts/. Data folders (logs/, db/, …) live there.
APP_ROOT    = runner_common.APP_ROOT
# `.gif` (#27) is here so a GIF original is walked, guarded and matched like any
# other image. It is the one source whose processed counterpart does NOT mirror
# its name (`<stem>_gif.png`), which resolve_by_name inverts explicitly. An
# animated or transparent GIF never reaches that point: image_variant_reason is
# checked on the ORIGINAL first, so it is reported and left untouched.
IMAGE_EXTS  = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff", ".tif", ".gif"}
# Mirrors batch_video_upscale.VIDEO_EXTS. Defined locally (not imported) to keep
# conciliate.py torch-free — batch_video_upscale pulls in the heavy engine stack.
VIDEO_EXTS  = {".mp4", ".avi", ".mov", ".mkv", ".m4v", ".wmv", ".mpg", ".mpeg",
               ".flv", ".webm", ".3gp", ".ts", ".mts", ".m2ts", ".vob"}
MEDIA_EXTS  = IMAGE_EXTS | VIDEO_EXTS
# RAW originals (#19). Deliberately NOT part of MEDIA_EXTS: nothing here ever
# acts on one. Sourced from raw_decode so the list lives in one place; the
# fallback keeps this module runnable on an install that predates it, and an
# empty set simply means a RAW is treated as an untouched non-media file - which
# is the same safe outcome, just less clearly reported.
try:
    from raw_decode import RAW_EXTS as _RAW_EXTS
    RAW_EXTS = set(_RAW_EXTS)
except Exception:                                  # noqa: BLE001 (old install)
    RAW_EXTS = set()
ARCHIVE_DIRNAME = "__Archive__"

# The @@TBX@@ event protocol + GUI-mode detection live in runner_common.
_stdin_is_piped = runner_common.stdin_is_piped
GUI_MODE        = runner_common.GUI_MODE
GUI_MARKER      = runner_common.GUI_MARKER
_gui_event      = runner_common.gui_event

# Notification settings (Discord/Telegram/ntfy, see notifications.py). Loaded
# fail-safe: config_store.load returns None on a missing/malformed config.json
# (conciliate is otherwise config-free and must still run without one), and
# resolve_settings({}) yields all-unconfigured backends, so send_notification is
# then a no-op. This closes the gap where a long archive/delete finished silently
# while upscale/tag/video all notified (item 9).
_CFG   = config_store.load(APP_ROOT) or {}
NOTIFY = notifications.resolve_settings(_CFG)

# Retroactive metadata backfill (#13b). Shares ONE setting with the upscaler's
# copy-at-save-time (#13a): a user who wants scrubbed output wants it from both,
# and two switches for one intention is one more thing to get wrong. Conciliation
# is the last moment both files exist, so in Delete mode this is the final chance
# to recover metadata an upscale run made before #13a shipped.
COPY_METADATA = bool(_CFG.get("upscale", {}).get("copy_metadata", True))

# conciliate.py is otherwise pure file I/O with no Pillow import, which is part of
# why it is fast and cheap to run. The backfill changes that, so the import is
# guarded and the feature simply reports itself unavailable rather than becoming a
# hard requirement of the whole tool.
try:
    import exif_copy
except Exception:                                  # noqa: BLE001 (no Pillow / old install)
    exif_copy = None


def send_notification(title, description, color, fields=None):
    """Fan out an alert to every configured backend; no-op for any that isn't
    configured, and fail-safe. Mirrors the other runners' wrapper.
    color: a notifications.COLOR_* severity constant (never a raw int)."""
    notifications.notify(NOTIFY, title, description, color, fields,
                         username="Conciliate Bot")


def _completion_notice(done, conflicts, errors, stopped):
    """Pick (title, color) for the end-of-run notification from the outcome
    (item 9). Green when the run finished clean; yellow when the user stopped it
    or it finished with conflicts/errors. Pure, so it is unit-tested.
    Colours are notifications.COLOR_* constants, never raw ints."""
    if stopped:
        return "Conciliation -- Stopped by User", notifications.COLOR_YELLOW
    if errors > 0 or conflicts > 0:
        return "Conciliation -- Finished with Issues", notifications.COLOR_YELLOW
    return "Conciliation -- Finished", notifications.COLOR_GREEN


def _norm(p):
    """Case- and separator-normalised absolute path, for reliable comparisons."""
    return os.path.normcase(os.path.normpath(os.path.abspath(p)))


# ─────────────────────────────────────────────
#  LOGGING
# ─────────────────────────────────────────────

class Logger:
    """Writes to both the terminal and logs/conc_<hash of original root>.log."""

    def __init__(self, original_root):
        digest  = hashlib.sha256(original_root.encode("utf-8")).hexdigest()[:12]
        log_dir = os.path.join(APP_ROOT, "logs")
        os.makedirs(log_dir, exist_ok=True)
        self.path = os.path.join(log_dir, f"conc_{digest}.log")
        self._fh  = open(self.path, "a", encoding="utf-8", buffering=1)
        ts = datetime.datetime.now().strftime("%Y-%m-%d | %H:%M:%S")
        self._fh.write(f"\n{'=' * 64}\nConciliation session: {ts}\n"
                       f"Original: {original_root}\n{'=' * 64}\n")

    def _ts(self):
        return datetime.datetime.now().strftime("%Y-%m-%d | %H:%M:%S")

    def tee(self, msg=""):
        # Stdout stays clean (the GUI window adds its own timestamp); the on-disk
        # log carries a per-line timestamp (0.3.9) for run-timing reconstruction.
        print(msg)
        try:
            self._fh.write(f"{self._ts()} | {msg}\n")
        except Exception:
            pass

    def log_only(self, msg):
        try:
            self._fh.write(f"{self._ts()} | {msg}\n")
        except Exception:
            pass

    def close(self):
        try:
            self._fh.close()
        except Exception:
            pass


# ─────────────────────────────────────────────
#  CACHE DISCOVERY
# ─────────────────────────────────────────────

def find_tr_cache(processed_root):
    """
    Locate the tag&rename cache whose source_root matches processed_root in the
    shared database. Returns a dict mapping the normcased original_rel_path
    (== the upscaled, mirrored-name path) to the current (possibly renamed)
    rel path, or None if no cache is found.
    """
    conn = db.get_conn()
    root = db.find_tag_root(conn, processed_root)
    if root is None:
        return None
    index = {}
    for r in conn.execute(
            "SELECT original_rel_path, current_rel_path FROM tag_files WHERE root_id = ?",
            (root["id"],)):
        orig = r["original_rel_path"]
        curr = r["current_rel_path"] or orig
        if orig:
            index[os.path.normcase(orig)] = curr
    return index


# ─────────────────────────────────────────────
#  MATCHING
# ─────────────────────────────────────────────

def build_processed_hash_index(processed_root, conn, abort=None):
    """
    Map content-hash -> absolute path for every image/video in the processed tree.
    Hashes are memoised in the DB (db.hash_file_cached), so this is cheap on
    repeat scans of an unchanged tree. The first matching path for a hash wins.

    Videos are hashed the same way as images (not cached-only): a processed video
    the user moved or renamed since it was produced has a fresh path, so it must be
    re-hashed to still be matched by content — that path-independence is the whole
    point of lineage matching. The app's outputs were already hashed at production
    time (keyed by their then-path), so an unmoved tree is served entirely from the
    cache; only genuinely new/moved files pay a hash, once, then they too are cached.
    """
    index = {}
    pruner = runner_common.DerivedPruner()
    for dirpath, dirnames, filenames in os.walk(processed_root):
        if abort is not None and abort():
            break
        # The Video Upscaler's `.imgtbx_video` work area lives inside the output
        # tree and holds every per-segment intermediate. Hashing multi-GB segments
        # here is pure waste, and a segment must never become a match candidate (#16).
        pruner.prune(dirnames)
        for fn in filenames:
            if os.path.splitext(fn)[1].lower() not in MEDIA_EXTS:
                continue
            p_abs = os.path.join(dirpath, fn)
            h = db.hash_file_cached(conn, p_abs)
            if h:
                index.setdefault(h, p_abs)
    conn.commit()   # flush the freshly-computed file_hashes rows
    return index


def resolve_by_lineage(o_abs, conn, get_index):
    """
    Match an original file to its processed counterpart by content-hash lineage,
    independent of any path. Hash the original (H0), look up the final hash of
    its lineage (tagged, else upscaled), then locate that hash in the processed
    tree. Returns the absolute processed path or None.
    """
    h0 = db.hash_file_cached(conn, o_abs)
    if not h0:
        return None
    final = db.lineage_final_hash(conn, h0)
    if not final:
        return None
    hit = get_index().get(final)
    if hit and os.path.isfile(hit):
        return hit
    return None


def resolve_by_name(o_rel, processed_root, tr_index):
    """
    Fallback matching when no hash lineage exists: the upscaler mirrors the tree
    and keeps the source filename with a lowercased extension, so the upscaled
    file lives at <dir>/<stem><ext.lower()>. If it was later tagged & renamed,
    the tag/rename cache maps it to its current name. Returns abs path or None.

    A GIF is the ONE source that breaks the mirror (#27): it becomes
    `<stem>_gif.png`, because writing the upscale back as GIF would re-quantise
    it to 256 colours. So the rule is inverted here explicitly rather than left
    to the content-hash lineage. Lineage would usually match it, but "usually"
    is not good enough for the tool that archives or deletes the original: a
    tree upscaled by another install, or one whose `db/cache.db` was deleted,
    has no lineage row at all, and the whole point of this fallback is to keep
    working without one.
    """
    stem, ext    = os.path.splitext(o_rel)
    upscaled_rel = stem + ext.lower()           # same dir, lowercased extension

    # Most specific first. Each base is tried as itself AND as whatever Tag &
    # Rename renamed it to, so a GIF's output is still found after tagging.
    bases = []
    if ext.lower() == ".gif":
        bases.append(stem + runner_common.GIF_OUTPUT_SUFFIX)
    bases.append(upscaled_rel)

    candidates = []
    for base in bases:
        if tr_index is not None:
            curr = tr_index.get(os.path.normcase(base))
            if curr:
                candidates.append(curr)         # upscaled then tagged & renamed
        candidates.append(base)                 # upscaled only (mirrored name)
    if ext != ext.lower():
        candidates.append(o_rel)                # exact original extension, just in case

    seen = set()
    for rel in candidates:
        key = os.path.normcase(rel)
        if key in seen:
            continue
        seen.add(key)
        p_abs = os.path.join(processed_root, rel)
        if os.path.isfile(p_abs):
            return p_abs
    return None


# NB: videos are matched by content-hash LINEAGE ONLY — there is deliberately no
# <stem>_<target> name fallback for them (unlike images, which mirror the source
# name). A whole-video upscale records lineage by default (item 10); a *clip*
# extract does NOT, and a clip output (`<base>_<label>_<target>.mp4`) shares the
# source's `<base>_` prefix, so a name fallback could match a full source to a short
# clip and propose replacing the whole video with it — a loss the counts-only preview
# would not reveal. Requiring lineage makes that impossible: an un-lineaged video (a
# clip, or one upscaled with record_lineage off) simply has no match and is left
# untouched. Custom targets make the name equally unreliable, so lineage it is.


def build_plan(original_root, processed_root, tr_index, conn=None,
               abort=None, status_cb=None, log_cb=None):
    """
    Walk the original tree and pair each image/video with its processed
    counterpart.

    Matching prefers content-hash lineage (path-independent — survives moving or
    renaming either tree). IMAGES fall back to mirrored-name matching (tag/rename
    cache) when they have no recorded lineage; VIDEOS are lineage-only (a name
    guess could mistake a partial clip for a whole-video match). The processed-tree
    hash index is built lazily, only once a lineage match is actually needed.

    Returns (plan, folders, kept_files, variant_files, raw_files):
      plan       — list of (original_abs, processed_abs, original_rel) to act on.
      folders    — list of (rel_dir, replaced, skipped, kept) per folder, for the
                   preview. 'kept' counts non-media files (never touched);
                   'skipped' counts media files with no processed counterpart
                   PLUS the #17 variants below (both are "left untouched").
      kept_files — absolute paths of the non-media files that were kept, so the
                   preview can list exactly what was left untouched (e.g. a
                   hidden Thumbs.db that Explorer doesn't show).
      variant_files - (abs_path, reason) for originals the upscaler cannot
                   round-trip (#17), which are never matched or replaced.
      raw_files  - absolute paths of the RAW originals, which are never matched
                   or replaced either (#19). Returned separately from the other
                   two so the preview can say "your negatives were left alone"
                   in those words: a user handing this tool a folder of CR2s
                   needs to see that as a decision, not infer it from a count.
    Skips every folder this app created (__Archive__ at ANY depth, __upscaled__,
    the video work area: see runner_common.DerivedPruner) plus the processed tree
    if it is nested inside the original tree under some other name.
    """
    plan          = []
    folders       = []
    kept_files    = []
    variant_files = []
    raw_files     = []
    processed_ab  = _norm(processed_root)
    # By NAME, so a nested archive deeper in the tree is skipped too: the old
    # single `<original_root>/__Archive__` path check only caught the top one (#16).
    pruner       = runner_common.DerivedPruner(extra=[ARCHIVE_DIRNAME])

    has_lineage = bool(conn is not None and db.lineage_has_rows(conn))
    _index_cache = {}   # built on first lineage hit

    def get_index():
        if "v" not in _index_cache:
            if status_cb is not None:
                status_cb("Hashing processed files …")
            _index_cache["v"] = build_processed_hash_index(
                processed_root, conn, abort=abort)
        return _index_cache["v"]

    for dirpath, dirnames, filenames in os.walk(original_root):
        if abort is not None and abort():
            break
        # Never descend into our own archive/output folders or into the processed tree.
        pruner.prune(dirnames)
        dirnames[:] = [
            d for d in dirnames
            if _norm(os.path.join(dirpath, d)) != processed_ab
        ]
        replaced = skipped = kept = 0
        for fn in sorted(filenames):
            abs_f = os.path.join(dirpath, fn)
            ext = os.path.splitext(fn)[1].lower()
            # A RAW original is NEVER archived or deleted (#19), and this says so
            # out loud rather than relying on RAW being absent from MEDIA_EXTS.
            # The rule is the general one - replace an original only when the
            # processed file is a SUPERSET of it - and a rendered JPEG is not a
            # superset of a negative: it is one interpretation of it, at 8 bits,
            # already demosaiced, with the sensor data gone. Losing that is
            # unrecoverable and would be the single worst thing this app could do
            # to a photographer's archive, so the guard is explicit, is tested,
            # and must not be "simplified" into the extension list above.
            if ext in RAW_EXTS:
                raw_files.append(abs_f)
                skipped += 1
                continue
            if ext not in MEDIA_EXTS:
                kept += 1
                kept_files.append(abs_f)
                continue
            # An image the upscaler cannot round-trip is never replaced (#17).
            # This is checked on the ORIGINAL, before any matching, so it also
            # protects a tree upscaled BEFORE #17 shipped: that run wrote a
            # flattened file under the SAME name and extension, which the
            # mirrored-name fallback would match with full confidence and report
            # as an ordinary "replaced" - archiving or DELETING the only copy
            # that still has the transparency / pages / bit depth.
            _variant = runner_common.image_variant_reason(abs_f)
            if _variant:
                variant_files.append((abs_f, _variant))
                skipped += 1
                continue

            o_rel = os.path.relpath(abs_f, original_root)
            p_abs = None
            if has_lineage:
                p_abs = resolve_by_lineage(abs_f, conn, get_index)
            # Images also get a mirrored-name fallback (survives a missing lineage);
            # videos are lineage-only on purpose (see the note above resolve_by_name /
            # build_plan) so a clip can never be mistaken for a whole-video match.
            if p_abs is None and ext in IMAGE_EXTS:
                p_abs = resolve_by_name(o_rel, processed_root, tr_index)
            if p_abs is None:
                skipped += 1
                continue
            plan.append((abs_f, p_abs, o_rel))
            replaced += 1

        rel_dir = os.path.relpath(dirpath, original_root)
        if replaced or skipped:
            folders.append((rel_dir, replaced, skipped, kept))
    if conn is not None:
        conn.commit()   # flush file_hashes computed for original files
    if log_cb is not None:
        summary = pruner.summary()
        if summary:
            log_cb(f"  {summary}")
    return plan, folders, kept_files, variant_files, raw_files


# ─────────────────────────────────────────────
#  EXECUTION
# ─────────────────────────────────────────────

def _is_image_pair(o_abs, p_abs):
    """#13b is images only. Container-level video metadata is an ffmpeg job with
    its own rules, so a video pair is left alone (and never opened with Pillow,
    which would report every one of them as 'unreadable')."""
    return (os.path.splitext(o_abs)[1].lower() in IMAGE_EXTS
            and os.path.splitext(p_abs)[1].lower() in IMAGE_EXTS)


def count_pending_metadata(plan, abort=None, status_cb=None):
    """
    How many pairs in `plan` WOULD gain metadata from their original (#13b).

    Reads only; Scan/Preview promises to touch nothing, so the count has to be
    produced without writing. Two header reads per pair (Pillow opens lazily and
    never decodes the pixels), which is nothing next to the whole-file hashing
    build_plan has already done for the same pairs.
    """
    if not COPY_METADATA or exif_copy is None:
        return 0
    n = 0
    for i, (o_abs, p_abs, _rel) in enumerate(plan):
        if abort is not None and abort():
            break
        if status_cb is not None and i and i % 200 == 0:
            status_cb(f"Checking metadata … ({i}/{len(plan)})")
        if _is_image_pair(o_abs, p_abs) and exif_copy.pending_backfill(o_abs, p_abs):
            n += 1
    return n


def _move_processed_in(p_abs, o_rel, original_root):
    """Compute the destination of the processed file inside the original tree
    (keeping the processed file's own name) and move it there.

    Keeping the PROCESSED name is what carries a Tag & Rename rename through,
    and it is also why a conciliated GIF lands as `<stem>_gif.png` rather than
    `<stem>.png` (#27). Stripping the marker on the way in looks tidier and is
    not safe: `logo.gif` and `logo.png` can both exist in one folder, both get
    conciliated, and both would then want the name `logo.png` -- and this moves
    with `shutil.move`, which overwrites without asking. The suffix that stops
    two sources sharing one OUTPUT is the same suffix that stops them sharing
    one destination here.
    """
    dest = os.path.join(original_root, os.path.dirname(o_rel), os.path.basename(p_abs))
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    shutil.move(p_abs, dest)
    return dest


def remove_empty_dirs(root, log):
    """
    Remove now-empty folders under `root` (including `root` itself if it ends up
    empty), deepest first so emptied parents are also cleaned. Only truly empty
    directories are removed — anything still holding files is left alone.
    Returns the number of folders removed.
    """
    removed = 0
    for dirpath, _dirnames, _filenames in os.walk(root, topdown=False):
        try:
            if not os.listdir(dirpath):
                os.rmdir(dirpath)
                removed += 1
                log.log_only(f"  Removed empty folder: {dirpath}")
        except OSError:
            pass   # in use / permission — leave it, best-effort cleanup
    return removed


def _backfill_metadata(o_abs, p_abs, o_rel, log):
    """Copy the fields the processed file is missing from the original (#13b).

    Returns True when something was written. Never raises: this is a bonus pass
    running immediately before the original is archived or deleted, and it must
    not be able to abort the file operation it precedes, leave a file in two
    places, or block the run. A failure is logged and the run carries on.
    """
    if not COPY_METADATA or exif_copy is None or not _is_image_pair(o_abs, p_abs):
        return False
    try:
        added, reason = exif_copy.backfill(o_abs, p_abs)
    except Exception as exc:                       # noqa: BLE001 (belt and braces)
        log.log_only(f"  metadata: skipped {o_rel} ({type(exc).__name__})")
        return False
    if added:
        log.log_only(f"  metadata: restored {added} field(s) into "
                     f"{os.path.basename(p_abs)}")
        return True
    if reason:
        log.log_only(f"  metadata: not restored for {o_rel} ({reason})")
    return False


class UndoRecorder:
    """Journals what a Run does, so it can be reversed afterwards (#18).

    Conciliation is the app's only destructive tool and was the only one with no
    undo record: the __Archive__ folder was the sole evidence a run had happened,
    and a Delete run left not even that. Every action is written BEFORE the file
    operation it describes, which is what makes an interrupted run recoverable: the
    row exists, and undo decides what to do from the disk rather than from a status.

    Fail-safe, deliberately: a DB failure disables the journal for the rest of the
    run and is reported ONCE, loudly, on the log. Aborting a conciliation because
    its safety net failed would cost the user the thing they actually asked for.
    """

    def __init__(self, conn, original_root, processed_root, mode, log):
        self._conn = conn
        self._original_root = original_root
        self._processed_root = processed_root
        self._mode = mode
        self._log = log
        self.run_id = None
        self.enabled = conn is not None

    def _fail(self, exc):
        if self.enabled:
            self.enabled = False
            self._log.tee(f"  WARNING: could not record the undo journal ({exc}). "
                          f"The conciliation continues, but this run will not be "
                          f"undoable from the app.")

    def begin(self):
        if not self.enabled:
            return
        try:
            self.run_id = db.begin_conc_run(self._conn, self._original_root,
                                            self._processed_root, self._mode)
        except Exception as exc:                       # noqa: BLE001 (fail-safe)
            self._fail(exc)

    def fingerprint(self, path):
        """(size, mtime, hash-if-already-memoised) for a file about to be moved.
        The hash is taken only when db already holds a valid one, so recording
        never reads a file: see db.cached_hash for why that trade is the right way
        round (recording is every run, verifying is the rare recovery)."""
        try:
            st = os.stat(path)
        except OSError:
            return (None, None, None)
        digest = None
        try:
            digest = db.cached_hash(self._conn, path)
        except Exception:                              # noqa: BLE001 (fail-safe)
            digest = None
        return (st.st_size, round(st.st_mtime, 3), digest)

    def record(self, action, original_path, archive_path, processed_path, dest_path):
        """Journal one pending pair and return its action id (None when disabled)."""
        if not self.enabled or self.run_id is None:
            return None
        try:
            return db.record_conc_action(
                self._conn, self.run_id, action, original_path, archive_path,
                processed_path, dest_path,
                orig_fp=self.fingerprint(original_path),
                proc_fp=self.fingerprint(processed_path))
        except Exception as exc:                       # noqa: BLE001 (fail-safe)
            self._fail(exc)
            return None

    def mark(self, action_id, status):
        if not self.enabled or action_id is None:
            return
        try:
            # No commit: see db.set_conc_action_status. The next record commits it,
            # and finish() flushes the last one.
            db.set_conc_action_status(self._conn, action_id, status, commit=False)
        except Exception as exc:                       # noqa: BLE001 (fail-safe)
            self._fail(exc)

    def finish(self):
        if not self.enabled or self.run_id is None:
            return
        try:
            db.finish_conc_run(self._conn, self.run_id)
            db.prune_conc_runs(self._conn, self._original_root)
        except Exception as exc:                       # noqa: BLE001 (fail-safe)
            self._fail(exc)


def execute(plan, original_root, mode, log, abort=None, recorder=None):
    """Perform the conciliation. Returns (done, skipped_conflict, errors, restored)."""
    archive_root = os.path.join(original_root, ARCHIVE_DIRNAME)
    total = len(plan)
    done = conflicts = errors = restored = 0

    for i, (o_abs, p_abs, o_rel) in enumerate(plan, 1):
        if abort is not None and abort():
            log.tee("  Stopped at user request.")
            break

        if i % 25 == 0 or i == total:
            _gui_event("PROG", f"{i}|{total}")

        if not os.path.isfile(p_abs):
            log.tee(f"  SKIP (processed file vanished): {o_rel}")
            errors += 1
            continue

        dest = os.path.join(original_root, os.path.dirname(o_rel), os.path.basename(p_abs))
        # Refuse to clobber an unrelated file. On case-insensitive Windows the
        # original and dest can differ only by extension case — that's the same
        # file, so it is allowed (the original is freed first).
        if (os.path.exists(dest)
                and _norm(dest) != _norm(o_abs)
                and _norm(dest) != _norm(p_abs)):
            log.tee(f"  SKIP (destination already exists): {o_rel} -> {os.path.basename(dest)}")
            conflicts += 1
            continue

        # Metadata FIRST, while the original is still in place (#13b). In delete
        # mode this is the last moment it exists at all, and in either mode the
        # pair is already matched and both files are on disk - the one point in
        # the app where that is true.
        if _backfill_metadata(o_abs, p_abs, o_rel, log):
            restored += 1

        arch_dest = os.path.join(archive_root, o_rel) if mode == "archive" else None
        action_id = None
        try:
            if mode == "archive":
                os.makedirs(os.path.dirname(arch_dest), exist_ok=True)
                if os.path.exists(arch_dest):
                    log.tee(f"  SKIP (already archived): {o_rel}")
                    conflicts += 1
                    continue

            # Journal the pair BEFORE either file moves (#18). Both must still be
            # in place for their fingerprints to mean anything, and a row written
            # afterwards is missing in exactly the case that makes it matter: a
            # crash, a power cut, a half-moved pair. A row left 'pending' says
            # "started, outcome unknown", which is what undo is built to resolve.
            if recorder is not None:
                action_id = recorder.record(
                    "archived" if mode == "archive" else "deleted",
                    o_abs, arch_dest, p_abs, dest)

            if mode == "archive":
                shutil.move(o_abs, arch_dest)
            else:  # delete
                os.remove(o_abs)

            new_dest = _move_processed_in(p_abs, o_rel, original_root)
            done += 1
            if recorder is not None:
                recorder.mark(action_id, "done")
            log.log_only(f"  OK: {o_rel} -> {os.path.relpath(new_dest, original_root)}"
                         f"  ({'archived' if mode == 'archive' else 'deleted'} original)")
        except Exception as exc:
            errors += 1
            # The row stays 'pending' on purpose: the pair may be half-done, and
            # 'pending' is precisely the state undo re-derives from the disk.
            log.tee(f"  ERROR on {o_rel}: {exc}")

    return done, conflicts, errors, restored


# ─────────────────────────────────────────────
#  UNDO  (#18)
# ─────────────────────────────────────────────

# Timestamp comparison tolerance. A move preserves mtime, but SMB and FAT-derived
# filesystems round it (FAT to 2 s), so an exact match would report spurious
# conflicts on the network shares this app is routinely pointed at. Two seconds is
# far below the gap between a run and a human deciding to undo it, and the content
# hash (when one was recorded) is the real check anyway.
_MTIME_TOL = 2.0


def _still_recorded_file(path, size, mtime, digest):
    """True if `path` is still the file the undo journal recorded.

    Cheap gate first: an ordinary edit changes (size, mtime) and that costs one
    stat. The content hash is verified only when one was recorded and the gate
    passed, which catches the remaining case (a deliberate replacement that kept
    both). Undo is the rare path, so it can afford the read; recording, which runs
    on every conciliation, never does.
    """
    try:
        st = os.stat(path)
    except OSError:
        return False
    if size is not None and st.st_size != size:
        return False
    if mtime is not None and abs(st.st_mtime - mtime) > _MTIME_TOL:
        return False
    if digest:
        return db.content_hash(path) == digest
    return True


def undo_one(row):
    """Reverse ONE journalled pair. Returns (status, message) where status is
    'undone', 'skipped' (nothing left to do) or 'conflict'.

    Nothing is moved until BOTH halves have been checked, so a refusal leaves the
    pair exactly as it was rather than half-unwound. The checks are deliberately
    about the disk, not the row's status: a run that crashed mid-pair leaves a
    'pending' row whose real state is only knowable by looking.
    """
    orig = row["original_path"]
    arch = row["archive_path"]
    proc = row["processed_path"]
    dest = row["dest_path"]

    if not arch:
        return "conflict", "the original was deleted, not archived"

    dest_here = os.path.isfile(dest)
    proc_here = os.path.isfile(proc)
    arch_here = os.path.isfile(arch)
    orig_here = os.path.isfile(orig)
    same_name = _norm(dest) == _norm(orig)

    # Nothing to reverse: the original is back at its own name and the processed
    # file is back in the processed tree. Covers an undo re-run (so undo is
    # idempotent) and a journalled pair whose file operations never started.
    # Checked FIRST because when the processed file took the original's own name,
    # "a file exists at dest" is true in both states and says nothing on its own.
    if orig_here and proc_here and not arch_here:
        return "skipped", "already undone"

    # ── half 1: the processed file goes back to the processed tree ───────────
    move_processed = False
    if dest_here:
        if not _still_recorded_file(dest, row["proc_size"], row["proc_mtime"],
                                    row["proc_hash"]):
            return "conflict", f"{dest} has changed since the run"
        if proc_here:
            return "conflict", f"{proc} is occupied by another file"
        move_processed = True
    elif not proc_here:
        return "conflict", "the processed file is missing from both folders"

    # ── half 2: the original comes back out of the archive ───────────────────
    move_original = False
    if arch_here:
        if not _still_recorded_file(arch, row["orig_size"], row["orig_mtime"],
                                    row["orig_hash"]):
            return "conflict", f"{arch} has changed since the run"
        # The original's own name is freed by half 1 when the processed file took
        # it, which is the usual case; any OTHER occupant is somebody else's file.
        if orig_here and not (same_name and move_processed):
            return "conflict", f"{orig} is occupied by another file"
        move_original = True
    elif not orig_here or (same_name and move_processed):
        # `same_name and move_processed` is the trap: a file DOES sit at the
        # original's path, but half 1 is about to move it away because it is the
        # processed file, not the original. Reading that as "the original is
        # already back" would leave the tree with neither.
        return "conflict", "the archived original is missing"

    if not move_processed and not move_original:
        return "skipped", "already undone"

    # ── perform, processed file first so it frees the original's name ────────
    if move_processed:
        os.makedirs(os.path.dirname(proc), exist_ok=True)
        shutil.move(dest, proc)
    if move_original:
        os.makedirs(os.path.dirname(orig), exist_ok=True)
        shutil.move(arch, orig)
    return "undone", ""


def run_undo(original_root, log, abort=None):
    """Reverse the most recent recorded conciliation of `original_root`.
    Returns (undone, conflicts, errors, reason) — `reason` is set (and the counts
    are zero) when there was nothing that COULD be undone."""
    conn = db.get_conn()
    run = db.latest_conc_run(conn, original_root)
    if run is None:
        return 0, 0, 0, ("No conciliation of this folder has been recorded, so "
                         "there is nothing to undo. Runs made before this version "
                         "of the app were not journalled.")
    if run["schema_version"] != db.CONC_UNDO_SCHEMA:
        return 0, 0, 0, (f"That run was recorded by a different version of the undo "
                         f"journal (v{run['schema_version']}), so this version will "
                         f"not act on it.")

    rows = db.get_conc_actions(conn, run["id"])
    when = (run["started_at"] or "").replace("T", " ")

    if run["mode"] != "archive":
        # Honest by design: the bytes are gone and no journal can bring them back.
        # The record still earns its place, so spend it on the question a user
        # actually asks after a bad delete run: what exactly did it remove?
        log.tee("")
        log.tee(f"The last run on this folder ({when}) DELETED its originals, so "
                f"they cannot be restored.")
        log.tee(f"For the record, these {len(rows)} original file(s) were deleted:")
        for r in rows:
            log.tee(f"  {r['original_path']}")
        return 0, 0, 0, ("The last run deleted its originals; deleted files cannot "
                         "be restored. The log lists exactly what was removed.")

    total = len(rows)
    log.tee("")
    log.tee(f"Undoing the conciliation of {when} — {total} file(s).")
    log.tee("  Each processed file goes back to the processed folder and each "
            "original comes back out of __Archive__.")
    log.tee("")

    undone = conflicts = errors = 0
    for i, row in enumerate(rows, 1):
        if abort is not None and abort():
            log.tee("  Stopped at user request.")
            break
        if i % 25 == 0 or i == total:
            _gui_event("PROG", f"{i}|{total}")
        try:
            status, message = undo_one(row)
        except Exception as exc:                       # noqa: BLE001 (per-file)
            errors += 1
            log.tee(f"  ERROR on {row['original_path']}: {exc}")
            continue
        if status == "conflict":
            conflicts += 1
            log.tee(f"  SKIP: {row['original_path']} — {message}")
            db.set_conc_action_status(conn, row["id"], "conflict")
        else:
            if status == "undone":
                undone += 1
            log.log_only(f"  OK: restored {row['original_path']}")
            db.set_conc_action_status(conn, row["id"], "undone")

    # The archive folders we emptied are no longer useful; the processed tree's
    # folders were recreated on the way back, so only this side needs cleaning.
    removed = remove_empty_dirs(os.path.join(original_root, ARCHIVE_DIRNAME), log)

    if conflicts == 0 and errors == 0 and (abort is None or not abort()):
        db.mark_conc_run_undone(conn, run["id"])
    log.tee("")
    log.tee(f"Undo finished — {undone} restored, {conflicts} skipped (conflict), "
            f"{errors} error(s)"
            + (f", {removed} empty archive folder(s) removed." if removed else "."))
    if conflicts:
        log.tee("  The skipped files were left exactly as they were: each one had "
                "changed, moved, or its name is now taken by something else.")
    return undone, conflicts, errors, None


# ─────────────────────────────────────────────
#  STDIN CONTROL
# ─────────────────────────────────────────────

_run_evt  = threading.Event()
_quit_evt = threading.Event()


def _watch_stdin():
    """GUI control: 'run' starts execution, 'q' aborts."""
    try:
        for line in sys.stdin:
            cmd = line.strip().lower()
            if cmd == "run":
                _run_evt.set()
            elif cmd == "q":
                _quit_evt.set()
                break
    except Exception:
        pass


# ─────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────

def _print_preview_table(folders, log):
    log.tee("")
    log.tee("  Preview (nothing has been changed yet):")
    width = max((len(f[0]) for f in folders), default=6)
    for rel_dir, replaced, skipped, kept in folders:
        label = rel_dir if rel_dir != "." else "(root)"
        parts = [f"{replaced} replaced"]
        if skipped:
            parts.append(f"{skipped} left untouched")
        parts.append(f"{kept} non-media file(s) kept")
        log.tee(f"    {label.ljust(width)}  -  {', '.join(parts)}")


def _run_undo_main(original_root):
    """The --undo entry point: reverse the last recorded run on this folder (#18).

    Deliberately NOT a two-phase scan/confirm like a normal run: the GUI already
    confirms, and there is no plan to build — the journal IS the plan, so the only
    honest preview is the one the caller already saw.
    """
    log = Logger(original_root)
    _gui_event("LOG", log.path)
    log.tee(f"Original folder:  {original_root}")
    log.tee("Operation:        Undo the last conciliation")
    _gui_event("STATUS", "Undoing the last conciliation …")

    started = time.time()
    undone, conflicts, errors, reason = run_undo(
        original_root, log, abort=_quit_evt.is_set)
    elapsed = time.time() - started

    if reason:
        log.tee("")
        log.tee(reason)
        _gui_event("STATUS", reason)
        _gui_event("UNDONE", json.dumps({"undone": 0, "conflicts": 0, "errors": 0,
                                         "refused": reason}))
        log.close()
        sys.exit(0)

    _gui_event("UNDONE", json.dumps(
        {"undone": undone, "conflicts": conflicts, "errors": errors,
         "elapsed_seconds": round(elapsed, 1),
         "stopped_by_user": _quit_evt.is_set()}))
    _gui_event("STATUS", f"Undo finished — {undone} file(s) restored.")
    send_notification(
        title       = ("Conciliation -- Undo Finished" if not (conflicts or errors)
                       else "Conciliation -- Undo Finished with Issues"),
        description = (f"{undone} restored, {conflicts} skipped (conflict), "
                       f"{errors} error(s)."),
        color       = (notifications.COLOR_GREEN if not (conflicts or errors)
                       else notifications.COLOR_YELLOW),
        fields      = [
            {"name": "Original", "value": original_root},
            {"name": "Elapsed",  "value": f"{elapsed:.1f}s"},
            {"name": "Machine",  "value": os.environ.get("COMPUTERNAME", "unknown")},
        ],
    )
    log.close()
    sys.exit(0 if errors == 0 else 1)


def main():
    args = [a for a in sys.argv[1:]]
    undo_mode = "--undo" in args
    args = [a for a in args if not a.startswith("--")]
    # Undo takes ONE folder: every path it needs is in the journal, and the
    # processed folder may not even exist any more (a finished run empties it and
    # its now-empty folders are removed).
    if len(args) < (1 if undo_mode else 2):
        print("Usage: python conciliate.py <original_dir> <processed_dir> [archive|delete]")
        print("       python conciliate.py <original_dir> --undo")
        sys.exit(0)

    original_root = os.path.abspath(args[0])
    if not os.path.isdir(original_root):
        print(f"ERROR: Original folder not found: '{original_root}'")
        sys.exit(1)

    if GUI_MODE:
        threading.Thread(target=_watch_stdin, daemon=True).start()

    if undo_mode:
        _run_undo_main(original_root)
        return

    processed_root = os.path.abspath(args[1])
    mode = (args[2].lower() if len(args) >= 3 else "archive")
    if mode not in ("archive", "delete"):
        print(f"ERROR: unknown mode '{mode}' (expected 'archive' or 'delete').")
        sys.exit(1)
    if not os.path.isdir(processed_root):
        print(f"ERROR: Processed folder not found: '{processed_root}'")
        sys.exit(1)
    if _norm(original_root) == _norm(processed_root):
        print("ERROR: The original and processed folders must be different.")
        sys.exit(1)

    log = Logger(original_root)
    _gui_event("LOG", log.path)
    log.tee(f"Original folder:  {original_root}")
    log.tee(f"Processed folder: {processed_root}")
    log.tee(f"Operation:        {'Archive originals' if mode == 'archive' else 'Delete originals'}")

    # ── Locate caches ─────────────────────────────────────────────────────────
    _gui_event("STATUS", "Reading caches …")
    conn = db.get_conn()
    has_lineage = db.lineage_has_rows(conn)
    tr_index = find_tr_cache(processed_root)
    log.tee("")
    log.tee(f"  Hash lineage:     {'available (path-independent matching)' if has_lineage else 'none (using mirrored-name matching)'}")
    log.tee(f"  Tag/rename cache: {'found' if tr_index is not None else 'not found'}")

    # ── Scan / build the plan ─────────────────────────────────────────────────
    _gui_event("STATUS", "Scanning the original folder …")
    log.tee("")
    log.tee("Scanning …")
    plan, folders, kept_files, variant_files, raw_files = build_plan(
        original_root, processed_root, tr_index,
        conn=conn, abort=_quit_evt.is_set,
        status_cb=lambda m: _gui_event("STATUS", m),
        log_cb=log.tee)
    if _quit_evt.is_set():
        log.tee("Cancelled during scan.")
        log.close()
        sys.exit(0)

    total_replaced = sum(f[1] for f in folders)
    total_skipped  = sum(f[2] for f in folders)
    total_kept     = sum(f[3] for f in folders)

    # #13b: how many pairs would have metadata restored. Counted here, written
    # only by the Run phase below, so Scan/Preview still touches nothing.
    _gui_event("STATUS", "Checking metadata …")
    pending_meta = count_pending_metadata(
        plan, abort=_quit_evt.is_set,
        status_cb=lambda m: _gui_event("STATUS", m))

    for rel_dir, replaced, skipped, kept in folders:
        _gui_event("FOLDER", json.dumps(
            {"dir": rel_dir, "replaced": replaced, "skipped": skipped, "kept": kept}))
    _gui_event("PLAN", json.dumps(
        {"replaced": total_replaced, "skipped": total_skipped,
         "kept": total_kept, "mode": mode, "metadata": pending_meta}))

    _print_preview_table(folders, log)
    # List the kept non-image files by full path. These are easy to miss in
    # Explorer (e.g. a hidden Thumbs.db), so spell them out to make clear
    # exactly what was left untouched and why the "kept" count is non-zero.
    if kept_files:
        log.tee("")
        log.tee("Non-media files: ")
        for p in kept_files:
            log.tee(p)
    # Same treatment for the #17 variants: they are inside the "left untouched"
    # count, and a user who upscaled them before 0.5.9 has a flattened copy in
    # the processed tree that will now never be moved in. Name them.
    if variant_files:
        log.tee("")
        log.tee("Left untouched - upscaling would discard part of these images:")
        for p, reason in variant_files:
            log.tee(f"{p}  ({reason})")
    # RAW originals are never replaced (#19). Spelled out for the same reason the
    # two lists above are: a user pointing this at a folder of negatives has to
    # be able to SEE that they are safe, and see it before pressing Run in Delete
    # mode, not work it out from a count that says "left untouched".
    if raw_files:
        log.tee("")
        log.tee("RAW originals - never archived or deleted, whatever the mode:")
        for p in raw_files:
            log.tee(p)
    log.tee("")
    log.tee(f"  Total: {total_replaced} file(s) to replace, "
            f"{total_skipped} left untouched, "
            f"{total_kept} non-media file(s) kept.")
    if variant_files:
        log.tee(f"  Of the untouched, {len(variant_files)} image(s) were kept "
                f"because upscaling would discard part of them; the rest had no "
                f"processed counterpart.")
    if raw_files:
        log.tee(f"  Of the untouched, {len(raw_files)} are RAW originals: a "
                f"rendered JPEG is one interpretation of a negative, not a "
                f"replacement for it, so they are kept and their renders stay in "
                f"the processed folder.")
    if pending_meta:
        log.tee(f"  Metadata to restore: {pending_meta} image(s) will get the "
                f"capture date, camera and other fields their original still has "
                f"and the processed copy is missing.")
    elif COPY_METADATA and exif_copy is None:
        log.tee("  Metadata restore: unavailable (Pillow is not installed).")

    if total_replaced == 0:
        _gui_event("STATUS", "Nothing to conciliate.")
        log.tee("")
        log.tee("Nothing to do — no original files have a processed counterpart.")
        log.close()
        sys.exit(0)

    # ── Wait for confirmation ─────────────────────────────────────────────────
    _gui_event("STATUS", "Preview ready — review and click Run.")
    if GUI_MODE:
        log.tee("")
        log.tee("Preview ready. Review the summary above, then click Run to proceed.")
        while not _run_evt.is_set() and not _quit_evt.is_set():
            time.sleep(0.1)
        if _quit_evt.is_set():
            log.tee("Cancelled — nothing was changed.")
            log.close()
            sys.exit(0)
    else:
        verb = "ARCHIVE" if mode == "archive" else "DELETE"
        resp = input(f"\nType 'run' to {verb} originals and move processed files in "
                     f"(anything else aborts): ").strip().lower()
        if resp != "run":
            log.tee("Aborted — nothing was changed.")
            log.close()
            sys.exit(0)

    # ── Execute ───────────────────────────────────────────────────────────────
    _gui_event("STATUS",
               "Archiving originals and moving processed files in …" if mode == "archive"
               else "Deleting originals and moving processed files in …")
    log.tee("")
    log.tee("Running …")
    started = time.time()
    # Journal what this run does, so it can be undone afterwards (#18). Archive
    # mode is fully reversible; a delete run is recorded only as evidence of what
    # was removed, which undo reports and refuses to act on.
    recorder = UndoRecorder(conn, original_root, processed_root, mode, log)
    recorder.begin()
    done, conflicts, errors, restored = execute(plan, original_root, mode, log,
                                                abort=_quit_evt.is_set,
                                                recorder=recorder)
    recorder.finish()
    elapsed = time.time() - started

    # Folders in the processed tree that we emptied by moving their files out are
    # no longer useful (e.g. a leftover '__upscaled__') — clean them up.
    removed_dirs = remove_empty_dirs(processed_root, log)

    log.tee("")
    log.tee(f"Done in {elapsed:.1f}s — {done} replaced, "
            f"{conflicts} skipped (conflict), {errors} error(s)"
            + (f", {removed_dirs} empty folder(s) removed." if removed_dirs else "."))
    if restored:
        log.tee(f"  Metadata restored into {restored} image(s) from their "
                f"originals (see the log for the per-file detail).")
    if done and recorder.enabled and recorder.run_id:
        log.tee("  This run was recorded: "
                + ("use 'Undo last run' on the Conciliation tab to reverse it."
                   if mode == "archive" else
                   "the log lists every deleted original, but deleted files "
                   "cannot be restored."))
    # GUI: machine-readable run summary (drives the MQTT last_run topic + the
    # run-finished event). `processed`/`failed`/`elapsed_seconds` mirror the other
    # three runners' key names so ONE Home Assistant automation covers every tool
    # (this runner predates that convention); the original four keys stay, so a
    # template already reading them is unaffected.
    _gui_event("DONE", json.dumps(
        {"done": done, "conflicts": conflicts, "errors": errors,
         "removed_dirs": removed_dirs, "metadata_restored": restored,
         "processed": done, "failed": errors,
         "elapsed_seconds": round(elapsed, 1),
         "stopped_by_user": _quit_evt.is_set()}))
    _gui_event("STATUS", f"Done — {done} file(s) replaced.")

    # Notify: conciliation used to finish silently (item 9). Colour by outcome,
    # matching the upscaler's palette (green clean, yellow stopped/with-issues).
    n_title, n_color = _completion_notice(done, conflicts, errors, _quit_evt.is_set())
    send_notification(
        title       = n_title,
        description = (f"{done} replaced, {conflicts} skipped (conflict), "
                       f"{errors} error(s)"
                       + (f", metadata restored into {restored}." if restored else ".")),
        color       = n_color,
        fields      = [
            {"name": "Original",  "value": original_root},
            {"name": "Operation", "value": "Archive" if mode == "archive" else "Delete"},
            {"name": "Elapsed",   "value": f"{elapsed:.1f}s"},
            {"name": "Machine",   "value": os.environ.get("COMPUTERNAME", "unknown")},
        ],
    )

    log.close()
    sys.exit(0 if errors == 0 else 1)


if __name__ == "__main__":
    main()
