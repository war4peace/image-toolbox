"""
gui/browse_upscaled.py
----------------------
The "Browse upscaled…" window on the Batch Upscaler tab (future-features #22):
a folders-only tree on the left, a paged thumbnail wall on the right, and a
double-click that opens the existing comparison window (wipe + lens) on any pair.

**The gap it closes.** Comparison pairing was in-memory and run-scoped:
`FilmStrip._compare` is filled from RESULT events during a run and wiped by both
`set_queue()` and `clear()`, so the comparison window (and its 0.6.0 lens, the
app's most directly persuasive feature) was reachable only while a run was on
screen. Close the app and every pair was forgotten, though both files were still
on disk. The Video Upscaler already had the equivalent, driven from the DB; the
older and more-used Batch Upscaler had nothing.

**Pairing walks the OUTPUT tree and derives the source back**, rather than
reading the DB. The upscaler mirrors the source tree via `os.path.relpath` and
keeps the source filename with a lowercased extension, so the inverse is a couple
of `os.path.isfile` probes: free, needs no hashing, and it works on a tree
produced by another install or after `db/cache.db` was deleted (`lineage`'s
stored paths are marked "informational only" in the schema, so they are not a
substitute). A tagged & renamed output resolves through the *inverted* tag cache
first, which is not an edge case: tagging is step 2 of the documented workflow.
Content-hash matching exists for the remainder but is opt-in, because it reads
whole files.

The pairing and paging arithmetic is a set of pure module-level functions with no
Tk import, so it is unit-tested without a display, the shape the lens geometry
took in 0.6.0 and for the same reason: showing two unrelated files side by side is
the one failure this must not have, and an off-by-one page is invisible in a
screenshot.
"""

import os
import queue
import functools
import threading

import tkinter as tk
from tkinter import ttk

import runner_common
from gui.common import (DEFAULT_WINDOW_GEOMETRY, _geometry_on_screen,
                        save_settings)
from gui.filmstrip import CELL_DEFAULT, FilmStrip
from gui.widgets import Tooltip

# Thumbnails per page. Deliberately DOUBLE the tool tabs' BATCH_SIZE (100): a
# maximised browser on a 4K monitor fits a little over 200 cells at the default
# size, so a 100-image page left half the wall empty and the window looked
# unfinished. The tabs keep 100 because a run's preview strip is a few hundred
# pixels tall, where a bigger batch only decodes images nobody can see.
#
# Measured before changing it (220 real 4K JPEGs, cell 150 / 300, 1800x1000):
# decode is LINEAR (~23 ms per image either way, so 2.2 s -> 4.5 s for a full
# page) and it runs off the UI thread, so time to the FIRST thumbnail is
# unchanged at ~25-50 ms and the wall fills progressively. Memory is the real
# price: +99 MB -> +192 MB at cell 150, +140 MB -> +276 MB at cell 300, nearly
# all of it the <=512 px PIL masters, released on the next page or on close.
# That is small for an app that loads a 16 GB model, and the browser is modal so
# no run is competing for it. The one visible regression is a zoom click, which
# regenerates every PhotoImage on the page: 215 ms -> 432 ms at cell 300.
# Scrolling is unaffected (~5 ms/notch).
BROWSE_PAGE_SIZE = 200

# Mirrors batch_upscale.IMAGE_EXTS (the set the upscaler accepts, so the set its
# output tree can hold). Defined locally rather than imported: batch_upscale is a
# runner and reconfigures stdout at import time, which has no business happening
# inside the GUI process.
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff", ".tif"}


def _is_raw_render(name):
    """True when `name` is a JPEG this app rendered from a RAW (#19). Guarded the
    same way filmstrip's RAW support is, so an install predating raw_decode just
    treats it as an ordinary output."""
    try:
        import raw_decode
        return raw_decode.is_render_name(name)
    except Exception:                       # noqa: BLE001
        return False


# ─────────────────────────────────────────────
#  PAIRING  (pure — no Tk, no filesystem except the injected `isfile`)
# ─────────────────────────────────────────────

def invert_tag_index(tr_index):
    """Invert `conciliate.find_tr_cache`'s map for use in the other direction.

    That cache maps the normcased ORIGINAL rel path (which, inside a processed
    tree, is exactly the mirrored-name path the upscaler wrote) to the CURRENT
    rel path after Tag & Rename renamed it. Browsing starts from the file on
    disk, i.e. the renamed one, so the useful direction is the reverse: renamed
    output -> mirrored name -> source.
    """
    inv = {}
    for orig, curr in (tr_index or {}).items():
        if curr:
            inv.setdefault(os.path.normcase(curr), orig)
    return inv


@functools.lru_cache(maxsize=512)
def _dir_index(dirpath):
    """`{normcased filename: real filename}` for one directory, or `{}`."""
    try:
        return {os.path.normcase(e.name): e.name
                for e in os.scandir(dirpath) if e.is_file()}
    except OSError:
        return {}


def clear_path_cache():
    """Drop the cached directory listings. Called at the start of every scan, so
    a browse session never serves a listing taken before it began."""
    _dir_index.cache_clear()


def resolve_file(path):
    """`path` spelled the way it really is on disk, or None if it is not there.

    Not `os.path.isfile`, and the difference is the point. The upscaler writes
    `<stem><ext.lower()>`, so a source `DSC_0001.JPG` comes back as
    `DSC_0001.jpg` and only the extension's CASE differs. Windows stats
    case-insensitively, so a plain `isfile` probe finds the file and then hands
    back a path whose extension is not the file's own: everything still opens,
    but the browser would show, sort and copy a filename that disagrees with
    Explorer. One cached `scandir` per directory settles it, and it is cheaper
    than the several stats it replaces — an output folder's images nearly all
    resolve into the same source folder, so the listing is read once and reused.
    """
    d, name = os.path.split(path)
    real = _dir_index(d).get(os.path.normcase(name))
    return os.path.join(d, real) if real else None


def pair_source(out_rel, source_root, inv_tag=None, resolve=resolve_file):
    """The original that produced the output at `out_rel` (relative to the output
    root), as an absolute path spelled as it is on disk, or None.

    Resolution order: the inverted tag index (a renamed output), then the
    mirrored name, then nothing. Content-hash matching is deliberately not here:
    it is the opt-in third step and it needs I/O this function must not do.
    """
    if not source_root or not out_rel:
        return None
    rels = []
    mirrored = (inv_tag or {}).get(os.path.normcase(out_rel))
    if mirrored:
        rels.append(mirrored)
    if out_rel not in rels:
        rels.append(out_rel)
    for rel in rels:
        got = resolve(os.path.normpath(os.path.join(source_root, rel)))
        if got:
            return got
    return None


def folder_rows(counts):
    """`(rel_dir, parent_rel, own_count)` rows for the folder tree, ordered so a
    parent always precedes its children.

    `counts` maps a rel dir ("" is the root) to how many images it holds ITSELF.
    A folder is included when its subtree holds at least one image: a tree of
    empty folders is noise, but an ancestor is needed to reach a folder that is
    not empty, and such an ancestor honestly shows 0 (decision 4: a folder lists
    its own images, never its subfolders', so clicking it correctly lists
    nothing).
    """
    keep = set()
    for rel, n in (counts or {}).items():
        if not n:
            continue
        r = rel
        while True:
            keep.add(r)
            if not r:
                break
            r = os.path.dirname(r)

    def sort_key(rel):
        # A parent's tuple is a prefix of its children's, so prefix order alone
        # puts parents first; case-insensitive so `Zoo` and `apple` interleave
        # the way the user reads them.
        return tuple(p.lower() for p in rel.split(os.sep)) if rel else ()

    return [(r, (os.path.dirname(r) if r else None), int(counts.get(r, 0)))
            for r in sorted(keep, key=sort_key)]


# ─────────────────────────────────────────────
#  PAGING  (pure)
# ─────────────────────────────────────────────

def page_count(total, page_size=BROWSE_PAGE_SIZE):
    """How many pages `total` items span. Zero items is zero pages, not one:
    'Page 1 of 1 (0 images)' claims a page that has nothing on it."""
    if total <= 0 or page_size <= 0:
        return 0
    return (total + page_size - 1) // page_size


def clamp_page(page, total, page_size=BROWSE_PAGE_SIZE):
    n = page_count(total, page_size)
    if n == 0:
        return 0
    try:
        page = int(page)
    except (TypeError, ValueError):
        page = 0
    return max(0, min(n - 1, page))


def page_slice(page, total, page_size=BROWSE_PAGE_SIZE):
    """(start, end) indices of `page` — the half-open range the strip shows."""
    if total <= 0:
        return 0, 0
    start = clamp_page(page, total, page_size) * page_size
    return start, min(total, start + page_size)


def page_label(page, total, page_size=BROWSE_PAGE_SIZE):
    """The page bar's readout. Names both the page and the item range, because
    'Page 3 of 7' alone leaves the user counting in hundreds."""
    n = page_count(total, page_size)
    if n == 0:
        return "No images in this folder"
    start, end = page_slice(page, total, page_size)
    p = clamp_page(page, total, page_size)
    if n == 1:
        return f"{total:,} image{'' if total == 1 else 's'}"
    return f"Page {p + 1} of {n}   ({start + 1:,}-{end:,} of {total:,})"


def sort_entries(entries):
    """Case-insensitively by filename, then by full path as a tie-break.

    Not by mtime: every file in an upscaled tree shares "when the batch ran", so
    a time sort is an arbitrary order that merely looks meaningful.
    """
    return sorted(entries, key=lambda e: (os.path.basename(e.key).lower(),
                                          e.key.lower()))


# ─────────────────────────────────────────────
#  SCAN  (I/O — runs off the UI thread)
# ─────────────────────────────────────────────

class Pair:
    """One upscaled output and, when it could be resolved, its original.

    `key` is what the film strip is keyed by, and the choice is load-bearing: a
    paired entry is keyed by its SOURCE (so the thumbnail, the green frame and
    the right-click menu's "Open original" all line up with the run strip), while
    a pair-less one is keyed by its OUTPUT. That second case then costs nothing:
    with no `_compare` entry, double-click falls through to `os.startfile`, no
    status means no green frame, and the context menu drops into its plain
    "Open image / Open image folder" branch, all pointed at the file that exists.
    Keying it by a source path that is not there is what would make it expensive.
    """

    __slots__ = ("out", "src", "rel_dir", "by_content")

    def __init__(self, out, src=None, rel_dir="", by_content=False):
        self.out = out
        self.src = src
        self.rel_dir = rel_dir
        self.by_content = by_content

    @property
    def key(self):
        return self.src or self.out


def load_tag_index(processed_root):
    """The Tag & Rename cache for `processed_root`, inverted, or {}.

    Guarded + lazy: `conciliate` is a runner (it reconfigures stdout at import),
    so it is imported at scan time rather than at GUI import, and a failure here
    only costs renamed outputs their pairing.
    """
    try:
        import conciliate
        return invert_tag_index(conciliate.find_tr_cache(processed_root))
    except Exception:                       # noqa: BLE001 (fail-safe)
        return {}


def scan_pairs(output_root, source_root, inv_tag=None,
               on_progress=None, abort=None):
    """Walk the OUTPUT tree and derive each output's original back.

    The direction matters. Walking the source tree instead would list images that
    were skipped (already big enough, or a #17 variant the engine refuses) or that
    failed, none of which have anything to browse.

    Returns `(pairs, pruned_summary)`.
    """
    clear_path_cache()
    pruner = runner_common.DerivedPruner()
    pairs = []
    for dirpath, dirnames, filenames in os.walk(output_root):
        if abort is not None and abort():
            break
        # A nested __Archive__ or .imgtbx_video under the output root is not
        # "upscaled images" (#16). The pruner prunes SUBdirectories only, never
        # the chosen root — which matters here, because the root normally IS
        # `__upscaled__`.
        pruner.prune(dirnames)
        rel_dir = os.path.relpath(dirpath, output_root)
        rel_dir = "" if rel_dir == "." else rel_dir
        for fn in filenames:
            if os.path.splitext(fn)[1].lower() not in IMAGE_EXTS:
                continue
            out_abs = os.path.normpath(os.path.join(dirpath, fn))
            out_rel = os.path.join(rel_dir, fn) if rel_dir else fn
            # A RAW render is deliberately left unpaired (#19). Its source is a
            # negative, and the comparison window draws the source as its "before"
            # half: Pillow cannot open a CR3/ORF/RW2 at all, and answers a
            # CR2/NEF/DNG with the small preview in IFD 0. For a render-only file
            # there is no before/after either - the output IS the first viewable
            # version. An unpaired entry already behaves correctly here (keyed by
            # its own path, opens on double-click, no green frame), so this only
            # skips work the mirror inversion could never have completed anyway.
            src = None if _is_raw_render(fn) else pair_source(out_rel, source_root,
                                                              inv_tag)
            # Same file on both sides (a source root pointed at the output root):
            # not a pair, and showing it against itself would be a lie.
            if src and os.path.normcase(src) == os.path.normcase(out_abs):
                src = None
            pairs.append(Pair(out_abs, src, rel_dir))
        if on_progress is not None:
            on_progress(len(pairs))
    return pairs, pruner.summary()


def _lineage_source_hash(conn, out_hash):
    """The source hash recorded for an upscaled (or tagged) output, or None.
    Read-only: no schema change, and `lineage`'s stored paths are not consulted
    (the schema marks them informational)."""
    row = conn.execute(
        "SELECT src_hash FROM lineage WHERE tagged_hash = ? OR upscaled_hash = ? "
        "LIMIT 1", (out_hash, out_hash)).fetchone()
    return row["src_hash"] if row is not None else None


def match_by_content(pairs, source_root, on_progress=None, abort=None):
    """Second-chance pairing for the outputs the mirror could not resolve: the
    original was moved or renamed after it was upscaled, so only its content
    still identifies it. Opt-in, because it reads whole files.

    The cheap end goes first. Each unpaired OUTPUT is hashed and looked up in
    `lineage`; if not one of them has a recorded source hash, the source tree is
    never walked at all. Hashes are memoised (`db.hash_file_cached`), so a second
    run over an unchanged tree costs nothing.

    Mutates the matched `Pair`s in place and returns how many were matched.
    """
    unpaired = [p for p in pairs if not p.src]
    if not unpaired or not source_root or not os.path.isdir(source_root):
        return 0
    try:
        import db
        conn = db.get_conn()
    except Exception:                       # noqa: BLE001 (fail-safe)
        return 0

    wanted = {}                             # src_hash -> [Pair, …]
    for i, p in enumerate(unpaired):
        if abort is not None and abort():
            return 0
        if on_progress is not None:
            on_progress(f"Hashing unmatched results … {i + 1:,} / {len(unpaired):,}")
        try:
            h = db.hash_file_cached(conn, p.out)
            src_hash = _lineage_source_hash(conn, h) if h else None
        except Exception:                   # noqa: BLE001
            continue
        if src_hash:
            wanted.setdefault(src_hash, []).append(p)
    if not wanted:
        return 0

    used = {os.path.normcase(p.src) for p in pairs if p.src}
    pruner = runner_common.DerivedPruner()
    matched = 0
    seen = 0
    for dirpath, dirnames, filenames in os.walk(source_root):
        if abort is not None and abort() or not wanted:
            break
        pruner.prune(dirnames)
        for fn in filenames:
            if not wanted:
                break
            if os.path.splitext(fn)[1].lower() not in IMAGE_EXTS:
                continue
            path = os.path.normpath(os.path.join(dirpath, fn))
            if os.path.normcase(path) in used:
                continue                    # already somebody's original
            seen += 1
            if on_progress is not None and seen % 25 == 0:
                on_progress(f"Reading originals … {seen:,} checked, "
                            f"{matched:,} matched")
            try:
                h = db.hash_file_cached(conn, path)
            except Exception:               # noqa: BLE001
                continue
            for p in wanted.pop(h, ()) if h else ():
                p.src = path
                p.by_content = True
                matched += 1
    try:
        conn.commit()                       # flush the freshly-memoised hashes
    except Exception:                       # noqa: BLE001
        pass
    return matched


# ─────────────────────────────────────────────
#  THE WINDOW
# ─────────────────────────────────────────────

EMPTY_TEXT = (
    "No upscaled images found here.\n\n"
    "If you have already run Conciliation on this batch, its upscaled files were "
    "moved into your original photo folder and this output folder was emptied. "
    "That is expected, not an error — but browsing a conciliated folder (whose "
    "originals now sit in __Archive__) is not supported yet."
)

MISSING_TEXT = ("That output folder does not exist yet.\n\n"
                "Upscale a batch first, or point 'Save upscaled to' at the folder "
                "an earlier batch was written to.")


class BrowseUpscaledWindow(tk.Toplevel):
    """Browse an already-upscaled tree and compare any pair (#22).

    Modality is copied from `gui/video_benchmark.py`, whose comments record four
    traps that were each paid for once, and one of them is sharper here:

      1. **No `grab_set()`.** Beyond the Windows title-bar problem (an
         application grab makes the OS swallow MINIMIZE/MAXIMIZE), a local grab
         routes events to the grabbing window *and its descendants* — and the
         shared `ComparisonWindow` is a child of `App`, not of this window. A
         grab would make the comparison window open and then ignore every click,
         which is the entire feature.
      2. **No `transient(master)`**: a transient child is auto-hidden when its
         master is hidden, so it would vanish the instant it withdrew the main
         window. Non-transient also keeps its own taskbar button.
      3. **`withdraw()`, not `iconify()`**: Windows RESTORES a merely-minimised
         root to service a mid-session dialog, which then sits reachable behind
         this window and defeats the point.
      4. **Restore the main window on every teardown path** (`<Destroy>` as well
         as `WM_DELETE_WINDOW`, guarded to this widget, idempotent via
         `_closing`). This is the riskiest line in the feature: everything else
         fails visibly, but this one fails by leaving the app running with no
         visible window at all, and `single_instance.py` then blocks a relaunch.
    """

    def __init__(self, master, source_root, output_root, app=None):
        super().__init__(master)
        # Build hidden, reveal once laid out (a Toplevel maps at a default size
        # first, which flashes a small square).
        self.withdraw()
        self._app = app
        self._master_win = master
        self._closing = False
        self.source_root = os.path.normpath(source_root) if source_root else ""
        self.output_root = os.path.normpath(output_root) if output_root else ""
        self.title(f"Browse upscaled images — {self.output_root}")

        geo = app.settings.get("browse_geometry") if app is not None else None
        # First-ever open matches the MAIN window's first-run size: this window
        # replaces it on screen, so opening at a different size reads as the app
        # having jumped somewhere else. After that its own remembered geometry
        # wins, independently of the main window's.
        self.geometry(geo if (geo and _geometry_on_screen(self, geo))
                      else DEFAULT_WINDOW_GEOMETRY)
        self.minsize(860, 520)
        self._last_normal_geo = None
        if app is not None and app.settings.get("browse_zoomed"):
            try:
                self.state("zoomed")
            except tk.TclError:
                pass

        self._pairs = []            # every Pair found, all folders
        self._by_folder = {}        # rel_dir -> [Pair] (sorted)
        self._folder = None         # rel_dir on screen
        self._entries = []          # the selected folder's pairs
        self._page = 0
        self._scan_q = queue.Queue()
        self._abort = threading.Event()
        self._busy = False

        self._build()
        self.bind("<Configure>", self._track_geometry, add="+")
        self.protocol("WM_DELETE_WINDOW", self._close)
        # Safety net (trap 4): any teardown, not just the normal close, must
        # bring the main window back. <Destroy> bubbles from every child, so
        # guard it to this widget.
        self.bind("<Destroy>", self._on_destroy, add="+")
        self.deiconify()
        self._hide_master()
        self._start_scan()

    # ── modality ─────────────────────────────────────────────────────────────

    def _hide_master(self):
        try:
            if self._master_win is not None and self._master_win.winfo_exists():
                self._master_win.withdraw()
        except Exception:                   # noqa: BLE001
            pass

    def _restore_master(self):
        if self._closing:
            return                          # idempotent: _close + <Destroy> both call
        self._closing = True
        try:
            if self._master_win is not None and self._master_win.winfo_exists():
                self._master_win.deiconify()
                self._master_win.lift()
        except Exception:                   # noqa: BLE001
            pass

    def _on_destroy(self, event):
        if event.widget is self:
            self._restore_master()

    def _close(self):
        self._abort.set()
        self.save_geometry()
        self._restore_master()
        # The shared comparison window is a sibling of the main window, not a
        # child of this one, so it deliberately survives the browser closing.
        try:
            self.destroy()
        except tk.TclError:
            pass

    def _track_geometry(self, event):
        if event.widget is self:
            try:
                if self.state() == "normal":
                    self._last_normal_geo = self.geometry()
            except tk.TclError:
                pass

    def save_geometry(self):
        if self._app is None or not self.winfo_exists():
            return
        try:
            zoomed = (self.state() == "zoomed")
        except tk.TclError:
            zoomed = False
        self._app.settings["browse_geometry"] = self._last_normal_geo or self.geometry()
        self._app.settings["browse_zoomed"] = zoomed
        save_settings(self._app.settings)

    # ── layout ───────────────────────────────────────────────────────────────

    def _build(self):
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)          # the panes take the slack

        panes = ttk.PanedWindow(self, orient="horizontal")
        panes.grid(row=0, column=0, sticky="nsew", padx=10, pady=(8, 0))

        left = ttk.Frame(panes)
        left.rowconfigure(1, weight=1)          # row 0 is the tree's own control
        left.columnconfigure(0, weight=1)
        # "Match by content" sits over the TREE, because that is what it changes:
        # it re-pairs images, which moves them between folders and changes the
        # counts on these rows. Over the wall it read as a filter on the
        # thumbnails on screen, which it is not.
        self.content_var = tk.BooleanVar(value=False)
        self.content_chk = ttk.Checkbutton(
            left, text="Match by content (slower)", variable=self.content_var,
            command=self._toggle_content)
        self.content_chk.grid(row=0, column=0, columnspan=2, sticky="w",
                              pady=(0, 4))
        Tooltip(self.content_chk,
                "Also match originals that were moved or renamed after they were "
                "upscaled, by reading the files themselves. Slower: it reads whole "
                "files instead of just looking at names.",
                wraplength=Tooltip.WRAP_NARROW)

        self.tree = ttk.Treeview(left, show="tree", selectmode="browse")
        tys = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=tys.set)
        self.tree.grid(row=1, column=0, sticky="nsew")
        tys.grid(row=1, column=1, sticky="ns")
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)
        Tooltip(self.tree,
                "Folders that hold upscaled images. Click one to see its own "
                "images on the right; sub-folders are listed separately.",
                wraplength=Tooltip.WRAP_NARROW)
        panes.add(left, weight=1)

        right = ttk.Frame(panes)
        right.rowconfigure(1, weight=1)         # row 0 is the strip's toolbar
        right.columnconfigure(0, weight=1)
        cell = int((self._app.settings.get("thumb_cell", CELL_DEFAULT)
                    if self._app is not None else CELL_DEFAULT))
        # on_zoom=None ON PURPOSE. FilmStrip._do_resize clamps the cell to the
        # viewport and writes the clamped value BACK through on_zoom. A maximised
        # browser reaches nearly CELL_HARD_MAX while a tab's preview strip is a
        # few hundred px tall, so sharing the key read-write would have the tab
        # clamp and re-save the smaller value the moment this window closed:
        # browse zoom destroyed on every close, with no visible cause. Reading
        # the shared size as a starting point and never writing it back leaves
        # browse zoom a per-session adjustment, consistent with this window
        # remembering neither its root nor its folder.
        self.strip = FilmStrip(right, cell=cell, on_zoom=None,
                               page_size=BROWSE_PAGE_SIZE)

        # The strip's own toolbar: it acts on the thumbnails, so it spans the
        # thumbnail pane and nothing else. Spanning the whole window would put
        # the zoom and paging controls under the folder tree as well, which they
        # have nothing to do with — and the tree is resizable, so they would
        # drift further from the wall they belong to as the user drags the split.
        self._bar = ttk.Frame(right)
        self._bar.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        # Three columns: zoom (left), paging (centre), an empty balancing column
        # (right). Both outer columns take an equal share of the slack, so the
        # centre group is centred on the WALL, not merely on whatever space the
        # zoom buttons left over. `uniform` is what ties their widths together;
        # weight alone would let them differ.
        for col in (0, 2):
            self._bar.columnconfigure(col, weight=1, uniform="browsebar")

        zoom = ttk.Frame(self._bar)
        zoom.grid(row=0, column=0, sticky="w")
        smaller = ttk.Button(zoom, text="−", width=3, command=self.strip.zoom_out)
        larger = ttk.Button(zoom, text="+", width=3, command=self.strip.zoom_in)
        smaller.pack(side="left")
        larger.pack(side="left", padx=(4, 0))
        Tooltip(smaller, "Smaller thumbnails, so more images fit on screen.",
                wraplength=Tooltip.WRAP_NARROW)
        Tooltip(larger, "Larger thumbnails, so more detail is visible in each image.",
                wraplength=Tooltip.WRAP_NARROW)

        # Paging: one centred group, so the count sits between the arrows that
        # change it.
        nav = ttk.Frame(self._bar)
        nav.grid(row=0, column=1)
        self._page_btns = {}

        def _nav_btn(key, label, delta, hint):
            b = ttk.Button(nav, text=label, width=4,
                           command=lambda d=delta: self._go_page(d))
            b.pack(side="left", padx=2)
            Tooltip(b, hint, wraplength=Tooltip.WRAP_NARROW)
            self._page_btns[key] = b

        _nav_btn("back5", "⏪", -5, "Back 5 pages.")
        _nav_btn("prev", "◀", -1, "Previous page.")
        self.page_var = tk.StringVar(value="")
        page_lbl = ttk.Label(nav, textvariable=self.page_var, anchor="center")
        page_lbl.pack(side="left", padx=10)
        Tooltip(page_lbl,
                f"Which images this page is showing. A page holds "
                f"{BROWSE_PAGE_SIZE} thumbnails.", wraplength=Tooltip.WRAP_NARROW)
        _nav_btn("next", "▶", 1, "Next page.")
        _nav_btn("fwd5", "⏩", 5, "Forward 5 pages.")

        self.strip.grid(row=1, column=0, sticky="nsew")
        self.strip.on_compare = self._on_compare
        Tooltip(self.strip.canvas,
                "Double-click an image to compare the original with the upscaled "
                "result • right-click for more • use +/− to resize")
        # Shown instead of the strip when there is nothing to browse.
        self.empty_lbl = ttk.Label(right, text=EMPTY_TEXT, wraplength=520,
                                   justify="left", anchor="center")
        panes.add(right, weight=4)
        self._panes = panes
        self._right = right

        # Status bar along the bottom: what the scan found, and the way out.
        # Close comes down here with it rather than sitting alone in a header row
        # of its own, which is what moving the status line out of the top would
        # otherwise have left behind.
        self._foot = foot = ttk.Frame(self)
        foot.grid(row=1, column=0, sticky="ew", padx=10, pady=(4, 8))
        foot.columnconfigure(0, weight=1)
        self.status_var = tk.StringVar(value="Scanning …")
        self.status_lbl = ttk.Label(foot, textvariable=self.status_var, anchor="w")
        self.status_lbl.grid(row=0, column=0, sticky="ew")
        close = ttk.Button(foot, text="Close", command=self._close)
        close.grid(row=0, column=1, sticky="e", padx=(12, 0))
        Tooltip(close, "Close the browser and go back to the app.",
                wraplength=Tooltip.WRAP_NARROW)

        self._refresh_page_bar()

    # ── scan ─────────────────────────────────────────────────────────────────

    def _start_scan(self):
        if not self.output_root or not os.path.isdir(self.output_root):
            self.status_var.set("Output folder not found.")
            self._show_empty(MISSING_TEXT)
            return
        self._busy = True
        self._set_controls_enabled(False)
        self.status_var.set(f"Scanning {self.output_root} …")
        # Read the checkbox HERE, on the UI thread, and hand the worker a plain
        # bool. A Tk variable belongs to the interpreter's main thread and a
        # `.get()` from a worker raises "main thread is not in main loop" — which
        # the worker's own except would then report as a failed scan.
        want_content = bool(self.content_var.get())
        threading.Thread(target=self._scan_worker, args=(want_content,),
                         daemon=True).start()
        self.after(100, self._drain_scan)

    def _scan_worker(self, want_content):
        try:
            inv = load_tag_index(self.output_root)
            pairs, pruned = scan_pairs(
                self.output_root, self.source_root, inv,
                on_progress=lambda n: self._scan_q.put(("progress", n)),
                abort=self._abort.is_set)
            if want_content:
                match_by_content(
                    pairs, self.source_root,
                    on_progress=lambda m: self._scan_q.put(("note", m)),
                    abort=self._abort.is_set)
            self._scan_q.put(("done", pairs, pruned))
        except Exception as exc:            # noqa: BLE001 (never kill the window)
            self._scan_q.put(("error", f"{type(exc).__name__}: {exc}"))

    def _alive(self):
        """True while this window still exists. A pending `after` callback can
        outlive the window (Close sets the abort flag and destroys immediately),
        and a destroyed widget answers with a TclError rather than False."""
        try:
            return bool(self.winfo_exists())
        except tk.TclError:
            return False

    def _drain_scan(self):
        if not self._alive():
            return
        done = False
        while True:
            try:
                msg = self._scan_q.get_nowait()
            except queue.Empty:
                break
            kind = msg[0]
            if kind == "progress":
                self.status_var.set(f"Scanning … {msg[1]:,} upscaled image(s) found")
            elif kind == "note":
                self.status_var.set(msg[1])
            elif kind == "error":
                self.status_var.set(f"Scan failed — {msg[1]}")
                self._busy = False
                self._set_controls_enabled(True)
                done = True
            elif kind == "done":
                self._on_scan_done(msg[1], msg[2])
                done = True
        if not done:
            self.after(100, self._drain_scan)
        self.strip.drain()

    def _on_scan_done(self, pairs, pruned):
        self._busy = False
        self._pairs = pairs
        self._regroup()
        self._set_controls_enabled(True)
        paired = sum(1 for p in pairs if p.src)
        if not pairs:
            self.status_var.set("Nothing to browse.")
            self._show_empty(EMPTY_TEXT)
            return
        self._show_strip()
        self._pruned_note = pruned
        self._set_status_summary()
        # Keep decoding thumbnails as they arrive.
        self._tick()

    def _set_status_summary(self, extra=None):
        """Write the bottom status line: what the scan found.

        ONE place, because the same summary is re-shown after content matching
        and after un-ticking it, and three copies of a sentence are three chances
        for them to disagree.
        """
        folders = sum(1 for v in self._by_folder.values() if v)
        paired = sum(1 for p in self._pairs if p.src)
        bits = [f"{len(self._pairs):,} upscaled image(s) in "
                f"{folders:,} folder(s)",
                f"{paired:,} with the original alongside"]
        if extra:
            bits.append(extra)
        # No "tick Match by content to …" hint: the checkbox has its own tooltip,
        # and the hint only crowded the line.
        if getattr(self, "_pruned_note", None):
            bits.append(self._pruned_note)
        self.status_var.set(" · ".join(bits))

    def _tick(self):
        """Place decoded thumbnails. FilmStrip decodes off-thread and hands the
        results back through a queue that somebody on the UI thread has to drain;
        on a tool tab that is the run's poll loop, and here it is this."""
        if not self._alive():
            return
        self.strip.drain()
        self.after(120, self._tick)

    def _regroup(self):
        by = {}
        for p in self._pairs:
            by.setdefault(p.rel_dir, []).append(p)
        self._by_folder = {k: sort_entries(v) for k, v in by.items()}
        self._build_tree()

    def _build_tree(self):
        self.tree.delete(*self.tree.get_children())
        counts = {k: len(v) for k, v in self._by_folder.items()}
        rows = folder_rows(counts)
        root_label = os.path.basename(self.output_root) or self.output_root
        first = None
        for rel, parent, n in rows:
            iid = rel or "\x00"
            parent_iid = "" if parent is None else (parent or "\x00")
            name = root_label if not rel else os.path.basename(rel)
            # Only the root starts open. A deep photo tree expanded in full is a
            # wall of folders the user has to scroll past to reach the one they
            # want; collapsed, the root's children are visible immediately and
            # everything else is one click away.
            self.tree.insert(parent_iid, "end", iid=iid,
                             text=f"{name}  ({n:,})", open=(rel == ""))
            if first is None:
                first = iid
        if first is not None:
            self.tree.selection_set(first)
            self.tree.see(first)

    def _on_tree_select(self, _event=None):
        sel = self.tree.selection()
        if not sel:
            return
        self._select_folder("" if sel[0] == "\x00" else sel[0])

    def _select_folder(self, rel_dir):
        self._folder = rel_dir
        self._entries = self._by_folder.get(rel_dir, [])
        self._page = 0
        self.strip.set_queue([e.key for e in self._entries])
        # Order matters: set_queue clears the outcome maps and show_page builds
        # the cells, so the green frames are recorded after both.
        self.strip.show_page(0)
        for e in self._entries:
            if e.src:
                self.strip.set_status(e.src, "ok", compare_to=e.out)
        self._refresh_page_bar()

    # ── paging ───────────────────────────────────────────────────────────────

    def _go_page(self, delta):
        total = len(self._entries)
        self._page = clamp_page(self._page + delta, total)
        self.strip.show_page(self._page)
        self._refresh_page_bar()

    def _refresh_page_bar(self):
        total = len(self._entries)
        pages = page_count(total)
        self.page_var.set(page_label(self._page, total))
        at_first = self._page <= 0
        at_last = self._page >= pages - 1
        for key, disabled in (("back5", at_first), ("prev", at_first),
                              ("next", at_last), ("fwd5", at_last)):
            self._page_btns[key].configure(
                state="disabled" if (disabled or pages <= 1) else "normal")

    # ── content matching ─────────────────────────────────────────────────────

    def _toggle_content(self):
        if self._busy:
            return
        if not self.content_var.get():
            # Untick: drop the pairs only content matching produced, so the two
            # states really are the same view with and without the extra pass.
            for p in self._pairs:
                if p.by_content:
                    p.src, p.by_content = None, False
            self._regroup()
            self._reselect()
            self._set_status_summary()
            return
        self._busy = True
        self._set_controls_enabled(False)
        self.status_var.set("Matching by content …")
        threading.Thread(target=self._content_worker, daemon=True).start()
        self.after(100, self._drain_content)

    def _content_worker(self):
        try:
            n = match_by_content(
                self._pairs, self.source_root,
                on_progress=lambda m: self._scan_q.put(("note", m)),
                abort=self._abort.is_set)
            self._scan_q.put(("matched", n))
        except Exception as exc:            # noqa: BLE001
            self._scan_q.put(("error", f"{type(exc).__name__}: {exc}"))

    def _drain_content(self):
        if not self._alive():
            return
        done = False
        while True:
            try:
                msg = self._scan_q.get_nowait()
            except queue.Empty:
                break
            if msg[0] == "note":
                self.status_var.set(msg[1])
            elif msg[0] == "error":
                self.status_var.set(f"Content matching failed — {msg[1]}")
                done = True
            elif msg[0] == "matched":
                self._regroup()
                self._reselect()
                self._set_status_summary(f"{msg[1]:,} found by content")
                done = True
        if done:
            self._busy = False
            self._set_controls_enabled(True)
        else:
            self.after(100, self._drain_content)
        self.strip.drain()

    def _reselect(self):
        """Re-show the folder that was on screen after the pair list changed."""
        want = self._folder or ""
        iid = want or "\x00"
        if self.tree.exists(iid):
            self.tree.selection_set(iid)
            self._select_folder(want)
        else:
            self._build_tree()

    # ── misc ─────────────────────────────────────────────────────────────────

    def _set_controls_enabled(self, enabled):
        state = "normal" if enabled else "disabled"
        try:
            self.content_chk.configure(state=state)
        except tk.TclError:
            pass

    def _show_empty(self, text):
        """Replace the wall with an explanation. The toolbar goes with it: zoom
        and paging controls over nothing are just dead buttons."""
        self.empty_lbl.configure(text=text)
        self.strip.grid_remove()
        self._bar.grid_remove()
        self.empty_lbl.grid(row=1, column=0, sticky="nsew", padx=24, pady=24)

    def _show_strip(self):
        self.empty_lbl.grid_remove()
        self._bar.grid()
        self.strip.grid()

    def _on_compare(self, src, out):
        """Hand the pair to the app's single shared comparison window.

        It is a sibling Toplevel of the (withdrawn) main window rather than a
        child of this one, so it maps, raises and takes focus independently — and
        it survives this window closing, which is why no grab may be taken here.
        """
        if self._app is not None:
            self._app.show_comparison(src, out)
