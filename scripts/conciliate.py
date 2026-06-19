"""
conciliate.py
-------------
Image tree conciliation — replace original photos with their processed
(upscaled, optionally tagged & renamed) counterparts.

Runs in two phases, driven over stdin by toolbox_gui.py (or interactively from
a terminal):

  1. SCAN  — match each original image to its processed counterpart. Matching
             prefers content-hash lineage (db.lineage), which is independent of
             paths and so survives moving or renaming either tree; it falls back
             to mirrored-name matching (using the tag/rename cache) for files
             with no recorded lineage. Emits a per-folder preview (replaced /
             skipped / kept). NOTHING on disk is touched in this phase.
  2. RUN   — once the user confirms, perform the chosen operation per matched
             pair:
               archive — move the original into  <original>/__Archive__/<rel>,
                         then move its processed counterpart into the original
                         tree (keeping the processed file's name).
               delete  — delete the original, then move its processed
                         counterpart into the original tree.

Safety rules (non-negotiable):
  * An original image with NO processed counterpart on disk is NEVER touched.
  * Non-image files in the original tree are NEVER touched (counted as "kept").
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

SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
# App root = parent of scripts/. Data folders (logs/, db/, …) live there.
APP_ROOT    = os.path.dirname(SCRIPT_DIR)
GUI_MARKER  = "@@TBX@@"
IMAGE_EXTS  = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff", ".tif"}
ARCHIVE_DIRNAME = "__Archive__"


def _stdin_is_piped():
    """True when stdin is a pipe — i.e. we are driven by toolbox_gui.py."""
    try:
        return sys.stdin is not None and not sys.stdin.isatty()
    except Exception:
        return False

GUI_MODE = _stdin_is_piped()


def _gui_event(kind, payload):
    """Emit one machine-readable event line for the GUI (no-op in a terminal)."""
    if GUI_MODE:
        print(f"{GUI_MARKER}{kind}|{payload}", flush=True)


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

    def tee(self, msg=""):
        print(msg)
        try:
            self._fh.write(msg + "\n")
        except Exception:
            pass

    def log_only(self, msg):
        try:
            self._fh.write(msg + "\n")
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
    Map content-hash -> absolute path for every image in the processed tree.
    Hashes are memoised in the DB (db.hash_file_cached), so this is cheap on
    repeat scans of an unchanged tree. The first matching path for a hash wins.
    """
    index = {}
    for dirpath, dirnames, filenames in os.walk(processed_root):
        if abort is not None and abort():
            break
        for fn in filenames:
            if os.path.splitext(fn)[1].lower() not in IMAGE_EXTS:
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


def build_plan(original_root, processed_root, tr_index, conn=None,
               abort=None, status_cb=None):
    """
    Walk the original tree and pair each image with its processed counterpart.

    Matching prefers content-hash lineage (path-independent — survives moving or
    renaming either tree); it falls back to mirrored-name matching for files
    that have no recorded lineage (e.g. older data). The processed-tree hash
    index is built lazily, only once a lineage match is actually needed.

    Returns (plan, folders, kept_files):
      plan       — list of (original_abs, processed_abs, original_rel) to act on.
      folders    — list of (rel_dir, replaced, skipped, kept) per folder, for the
                   preview. 'kept' counts non-image files (never touched);
                   'skipped' counts images with no processed counterpart.
      kept_files — absolute paths of the non-image files that were kept, so the
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
            if os.path.splitext(fn)[1].lower() not in IMAGE_EXTS:
                kept += 1
                kept_files.append(abs_f)
                continue
            o_rel = os.path.relpath(abs_f, original_root)
            p_abs = None
            if has_lineage:
                p_abs = resolve_by_lineage(abs_f, conn, get_index)
            if p_abs is None:
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
        parts.append(f"{kept} non-image file(s) kept")
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
        log.tee("Non-image files: ")
        for p in kept_files:
            log.tee(p)
    log.tee("")
    log.tee(f"  Total: {total_replaced} image(s) to replace, "
            f"{total_skipped} without a match (left untouched), "
            f"{total_kept} non-image file(s) kept.")

    if total_replaced == 0:
        _gui_event("STATUS", "Nothing to conciliate.")
        log.tee("")
        log.tee("Nothing to do — no original images have a processed counterpart.")
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
    _gui_event("DONE", json.dumps(
        {"done": done, "conflicts": conflicts, "errors": errors,
         "removed_dirs": removed_dirs}))
    _gui_event("STATUS", f"Done — {done} image(s) replaced.")
    log.close()
    sys.exit(0 if errors == 0 else 1)


if __name__ == "__main__":
    main()
