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

Safety rules (non-negotiable):
  * An original file with NO processed counterpart on disk is NEVER touched.
  * Non-media files in the original tree are NEVER touched (counted as "kept").
  * The processed counterpart is moved in only AFTER the original has been
    archived/deleted, so the freed name is available; an unrelated file already
    occupying the destination name aborts that one pair (original left intact).

Usage:
    python conciliate.py <original_dir> <processed_dir> [archive|delete]

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
IMAGE_EXTS  = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff", ".tif"}
# Mirrors batch_video_upscale.VIDEO_EXTS. Defined locally (not imported) to keep
# conciliate.py torch-free — batch_video_upscale pulls in the heavy engine stack.
VIDEO_EXTS  = {".mp4", ".avi", ".mov", ".mkv", ".m4v", ".wmv", ".mpg", ".mpeg",
               ".flv", ".webm", ".3gp", ".ts", ".mts", ".m2ts", ".vob"}
MEDIA_EXTS  = IMAGE_EXTS | VIDEO_EXTS
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
NOTIFY = notifications.resolve_settings(config_store.load(APP_ROOT) or {})


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
    for dirpath, dirnames, filenames in os.walk(processed_root):
        if abort is not None and abort():
            break
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
    """
    stem, ext    = os.path.splitext(o_rel)
    upscaled_rel = stem + ext.lower()           # same dir, lowercased extension

    candidates = []
    if tr_index is not None:
        curr = tr_index.get(os.path.normcase(upscaled_rel))
        if curr:
            candidates.append(curr)             # upscaled then tagged & renamed
    candidates.append(upscaled_rel)             # upscaled only (mirrored name)
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
               abort=None, status_cb=None):
    """
    Walk the original tree and pair each image/video with its processed
    counterpart.

    Matching prefers content-hash lineage (path-independent — survives moving or
    renaming either tree). IMAGES fall back to mirrored-name matching (tag/rename
    cache) when they have no recorded lineage; VIDEOS are lineage-only (a name
    guess could mistake a partial clip for a whole-video match). The processed-tree
    hash index is built lazily, only once a lineage match is actually needed.

    Returns (plan, folders, kept_files):
      plan       — list of (original_abs, processed_abs, original_rel) to act on.
      folders    — list of (rel_dir, replaced, skipped, kept) per folder, for the
                   preview. 'kept' counts non-media files (never touched);
                   'skipped' counts media files with no processed counterpart.
      kept_files — absolute paths of the non-media files that were kept, so the
                   preview can list exactly what was left untouched (e.g. a
                   hidden Thumbs.db that Explorer doesn't show).
    Skips the __Archive__ subfolder and the processed tree if it is nested
    inside the original tree.
    """
    plan         = []
    folders      = []
    kept_files   = []
    archive_abs  = _norm(os.path.join(original_root, ARCHIVE_DIRNAME))
    processed_ab = _norm(processed_root)

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
        # Never descend into our own archive or into the processed tree.
        dirnames[:] = [
            d for d in dirnames
            if _norm(os.path.join(dirpath, d)) not in (archive_abs, processed_ab)
        ]
        replaced = skipped = kept = 0
        for fn in sorted(filenames):
            abs_f = os.path.join(dirpath, fn)
            ext = os.path.splitext(fn)[1].lower()
            if ext not in MEDIA_EXTS:
                kept += 1
                kept_files.append(abs_f)
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
    return plan, folders, kept_files


# ─────────────────────────────────────────────
#  EXECUTION
# ─────────────────────────────────────────────

def _move_processed_in(p_abs, o_rel, original_root):
    """Compute the destination of the processed file inside the original tree
    (keeping the processed file's own name) and move it there."""
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


def execute(plan, original_root, mode, log, abort=None):
    """Perform the conciliation. Returns (done, skipped_conflict, errors)."""
    archive_root = os.path.join(original_root, ARCHIVE_DIRNAME)
    total = len(plan)
    done = conflicts = errors = 0

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

        try:
            if mode == "archive":
                arch_dest = os.path.join(archive_root, o_rel)
                os.makedirs(os.path.dirname(arch_dest), exist_ok=True)
                if os.path.exists(arch_dest):
                    log.tee(f"  SKIP (already archived): {o_rel}")
                    conflicts += 1
                    continue
                shutil.move(o_abs, arch_dest)
            else:  # delete
                os.remove(o_abs)

            new_dest = _move_processed_in(p_abs, o_rel, original_root)
            done += 1
            log.log_only(f"  OK: {o_rel} -> {os.path.relpath(new_dest, original_root)}"
                         f"  ({'archived' if mode == 'archive' else 'deleted'} original)")
        except Exception as exc:
            errors += 1
            log.tee(f"  ERROR on {o_rel}: {exc}")

    return done, conflicts, errors


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
            parts.append(f"{skipped} without a match (left untouched)")
        parts.append(f"{kept} non-media file(s) kept")
        log.tee(f"    {label.ljust(width)}  -  {', '.join(parts)}")


def main():
    args = [a for a in sys.argv[1:]]
    if len(args) < 2:
        print("Usage: python conciliate.py <original_dir> <processed_dir> [archive|delete]")
        sys.exit(0)

    original_root = os.path.abspath(args[0])
    processed_root = os.path.abspath(args[1])
    mode = (args[2].lower() if len(args) >= 3 else "archive")
    if mode not in ("archive", "delete"):
        print(f"ERROR: unknown mode '{mode}' (expected 'archive' or 'delete').")
        sys.exit(1)

    if not os.path.isdir(original_root):
        print(f"ERROR: Original folder not found: '{original_root}'")
        sys.exit(1)
    if not os.path.isdir(processed_root):
        print(f"ERROR: Processed folder not found: '{processed_root}'")
        sys.exit(1)
    if _norm(original_root) == _norm(processed_root):
        print("ERROR: The original and processed folders must be different.")
        sys.exit(1)

    if GUI_MODE:
        threading.Thread(target=_watch_stdin, daemon=True).start()

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
    plan, folders, kept_files = build_plan(original_root, processed_root, tr_index,
                                           conn=conn, abort=_quit_evt.is_set,
                                           status_cb=lambda m: _gui_event("STATUS", m))
    if _quit_evt.is_set():
        log.tee("Cancelled during scan.")
        log.close()
        sys.exit(0)

    total_replaced = sum(f[1] for f in folders)
    total_skipped  = sum(f[2] for f in folders)
    total_kept     = sum(f[3] for f in folders)

    for rel_dir, replaced, skipped, kept in folders:
        _gui_event("FOLDER", json.dumps(
            {"dir": rel_dir, "replaced": replaced, "skipped": skipped, "kept": kept}))
    _gui_event("PLAN", json.dumps(
        {"replaced": total_replaced, "skipped": total_skipped,
         "kept": total_kept, "mode": mode}))

    _print_preview_table(folders, log)
    # List the kept non-image files by full path. These are easy to miss in
    # Explorer (e.g. a hidden Thumbs.db), so spell them out to make clear
    # exactly what was left untouched and why the "kept" count is non-zero.
    if kept_files:
        log.tee("")
        log.tee("Non-media files: ")
        for p in kept_files:
            log.tee(p)
    log.tee("")
    log.tee(f"  Total: {total_replaced} file(s) to replace, "
            f"{total_skipped} without a match (left untouched), "
            f"{total_kept} non-media file(s) kept.")

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
    done, conflicts, errors = execute(plan, original_root, mode, log,
                                      abort=_quit_evt.is_set)
    elapsed = time.time() - started

    # Folders in the processed tree that we emptied by moving their files out are
    # no longer useful (e.g. a leftover '__upscaled__') — clean them up.
    removed_dirs = remove_empty_dirs(processed_root, log)

    log.tee("")
    log.tee(f"Done in {elapsed:.1f}s — {done} replaced, "
            f"{conflicts} skipped (conflict), {errors} error(s)"
            + (f", {removed_dirs} empty folder(s) removed." if removed_dirs else "."))
    # GUI: machine-readable run summary (drives the MQTT last_run topic + the
    # run-finished event). `processed`/`failed`/`elapsed_seconds` mirror the other
    # three runners' key names so ONE Home Assistant automation covers every tool
    # (this runner predates that convention); the original four keys stay, so a
    # template already reading them is unaffected.
    _gui_event("DONE", json.dumps(
        {"done": done, "conflicts": conflicts, "errors": errors,
         "removed_dirs": removed_dirs,
         "processed": done, "failed": errors,
         "elapsed_seconds": round(elapsed, 1),
         "stopped_by_user": _quit_evt.is_set()}))
    _gui_event("STATUS", f"Done — {done} file(s) replaced.")

    # Notify: conciliation used to finish silently (item 9). Colour by outcome,
    # matching the upscaler's palette (green clean, yellow stopped/with-issues).
    n_title, n_color = _completion_notice(done, conflicts, errors, _quit_evt.is_set())
    send_notification(
        title       = n_title,
        description = f"{done} replaced, {conflicts} skipped (conflict), {errors} error(s).",
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
