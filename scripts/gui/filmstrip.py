"""
gui/filmstrip.py
----------------
The FilmStrip thumbnail wall (the live preview strip on each tool tab): a
scrollable grid of thumbnails with green/red outcome frames and the blue
"current" frame, right-click context menu, and zoomable cells. Pillow is
imported lazily inside the decode methods so the module loads PIL-free.
"""

import os
import time
import queue
import threading

import tkinter as tk
from tkinter import ttk

from gui.common import open_in_explorer

# RAW/DNG thumbnails (#19). Guarded so an install that predates raw_decode - or
# one where rawpy failed to install - simply draws RAW cells the way it always
# did (blank) instead of failing to build the strip at all.
try:
    import raw_decode

    def _is_raw(path):
        return raw_decode.is_raw(path)

    def _raw_thumb(path, box):
        return raw_decode.thumbnail(path, box)
except Exception:                       # noqa: BLE001
    def _is_raw(_path):
        return False

    def _raw_thumb(_path, _box):
        return None


THUMB_MASTER = 512           # px — bounding box the master thumbnail is decoded to
CELL_DEFAULT = 150           # px — default square cell edge for one thumbnail
CELL_MIN     = 90            # px — smallest cell (zoom out)
CELL_HARD_MAX = 1000         # px — absolute ceiling for a cell
CELL_STEP    = 30            # px — change per +/- click
GRID_GAP     = 10            # px — gap between cells
BATCH_SIZE   = 100           # thumbnails decoded per batch


class FilmStrip(ttk.Frame):
    """
    Responsive thumbnail wall of the queued images.

    The full ordered queue arrives via a QUEUE event. Thumbnails flow left to
    right and wrap into as many columns as the width allows, re-flowing on
    resize; each image keeps its aspect ratio inside a square cell. The image
    currently being processed is highlighted (blue frame) and any thumbnail can
    be double-clicked to open it. The +/- buttons (zoom_in / zoom_out) change the
    cell size.

    Per-image outcomes arrive via RESULT events (set_status): a thin green frame
    marks a success / an image with an upscaled counterpart that exists (so it
    can be compared), a thin red frame marks a failure, and no frame means
    not-yet-processed. Outcomes are run-level (kept across batch swaps) and
    follow a file through a rename; the blue current-frame always draws on top.

    Thumbnails are decoded in a background thread, one page at a time (when the
    current image crosses into the next page, that page is loaded), so very
    large queues stay snappy. The page size is per-widget (`page_size=`): the
    tool tabs use BATCH_SIZE, the upscaled-image browser asks for more.
    """

    BG          = "#202329"
    HILITE      = "#4f9cff"
    OUTLINE     = "#3a3f48"
    # Per-image outcome frames (scaffolding for the comparison feature): green =
    # processed successfully / an upscaled counterpart exists (comparable); red =
    # processing failed. No frame = not yet processed.
    OK_FRAME    = "#3fb950"
    FAIL_FRAME  = "#f85149"

    def __init__(self, master, cell=CELL_DEFAULT, on_zoom=None,
                 page_size=BATCH_SIZE):
        super().__init__(master)
        self._on_zoom = on_zoom
        # How many thumbnails one page holds. The tool tabs keep BATCH_SIZE (a
        # run's strip is a few hundred px tall, so a bigger batch would only
        # decode images nobody can see); the upscaled-image browser (#22) asks
        # for more, because a maximised window at 4K fits over 200 cells and a
        # 100-image page leaves half the wall empty.
        self._page_size = max(1, int(page_size))
        self.canvas = tk.Canvas(self, highlightthickness=0, bg=self.BG)
        ys = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=ys.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        ys.grid(row=0, column=1, sticky="ns")
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        self.canvas.bind("<MouseWheel>", self._wheel)
        self.canvas.bind("<Double-Button-1>", self._on_double)
        self.canvas.bind("<Button-3>", self._on_right_click)
        self.canvas.bind("<Configure>", self._on_resize)

        self._paths   = []        # full queue
        self._index   = {}        # path -> position in queue
        self._renamed = {}        # old path -> new path (renamed mid-run)
        self._batch   = None      # batch number currently displayed
        self._order   = []        # paths in the displayed batch, in order
        self._pos     = {}        # path -> position within the batch
        self._master  = {}        # path -> PIL master thumbnail (<= THUMB_MASTER)
        self._photo   = {}        # path -> PhotoImage at the current cell size
        self._img_id  = {}        # path -> canvas image item id
        self._current = None
        self._cell    = max(CELL_MIN, min(CELL_HARD_MAX, int(cell)))
        self._cols    = 1
        self._hl_id   = None      # highlight rectangle item id
        self._status  = {}        # path -> "ok"/"fail" (run-level, all batches)
        self._compare = {}        # source path -> upscaled output path (this run)
        self._frame_id = {}       # path -> status-frame rect id (current batch)
        # Optional callback(source_path, output_path): set by the Upscaler tab to
        # open the comparison window when a green (comparable) thumbnail is
        # double-clicked. When unset, double-click just opens the file.
        self.on_compare = None
        self._gen     = 0         # invalidates stale loader threads
        self._q       = queue.Queue()
        self._resize_after = None

    def _wheel(self, event):
        self.canvas.yview_scroll(-(event.delta // 120) * 2, "units")

    # ── Zoom ─────────────────────────────────────────────────────────────────

    def zoom_in(self):
        self._set_cell(self._cell + CELL_STEP)

    def zoom_out(self):
        self._set_cell(self._cell - CELL_STEP)

    def _max_cell(self):
        """A cell may grow until it fills the smaller of the viewport's width
        or height (one image fills the preview), bounded by a hard ceiling."""
        w = max(self.canvas.winfo_width(), 1)
        h = max(self.canvas.winfo_height(), 1)
        return max(CELL_MIN, min(CELL_HARD_MAX, min(w, h) - 2 * GRID_GAP))

    def _set_cell(self, cell):
        cell = max(CELL_MIN, min(self._max_cell(), cell))
        if cell == self._cell:
            return
        self._cell = cell
        for p in list(self._master):     # regenerate photos at the new size
            self._make_photo(p)
        self._relayout(force=True)
        if self._on_zoom:
            self._on_zoom(self._cell)

    # ── Queue management ─────────────────────────────────────────────────────

    def set_queue(self, paths):
        self._paths = [p.strip() for p in paths if p.strip()]
        self._index = {p: i for i, p in enumerate(self._paths)}
        self._renamed = {}
        self._status = {}               # outcomes belong to one run only
        self._compare = {}
        self._batch = None              # force a rebuild on the next IMG event

    def clear(self):
        self._gen += 1
        self._paths, self._index, self._renamed = [], {}, {}
        self._status = {}
        self._compare = {}
        self._batch, self._current = None, None
        self._build_cells([])

    def rename(self, old, new):
        """
        A queued file changed name on disk (tag rename or undo revert).
        Remap every reference so highlighting, double-click-to-open and
        late-arriving thumbnails all follow the file to its new path.
        """
        if old == new or old not in self._index:
            return
        i = self._index.pop(old)
        self._paths[i] = new
        self._index[new] = i
        self._renamed[old] = new
        if self._current == old:
            self._current = new
        if old in self._pos:
            self._pos[new] = self._pos.pop(old)
            self._order[self._pos[new]] = new
        for d in (self._master, self._photo, self._img_id, self._status,
                  self._frame_id, self._compare):
            if old in d:
                d[new] = d.pop(old)

    def refresh(self, path):
        """Re-decode one thumbnail whose file changed on disk (e.g. after an
        auto-straighten rotation), so the strip shows the corrected pixels.

        Reuses the background loader: the decode runs off-thread and the
        result is placed by the regular drain() tick. Dropping the cached
        photo first guards against a stale image lingering if the file is
        outside the currently displayed batch."""
        p = (path or "").strip()
        if not p:
            return
        self._photo.pop(p, None)
        threading.Thread(target=self._load_batch,
                         args=([p], self._gen), daemon=True).start()

    def page_count(self):
        """How many pages the queue spans (0 when it is empty)."""
        return (len(self._paths) + self._page_size - 1) // self._page_size

    def current_page(self):
        """The page on screen, or None before the first one is built."""
        return self._batch

    def show_page(self, n, first=None):
        """Display page `n` (0-based, clamped). `first` decodes that image ahead
        of the rest.

        The visible batch used to be a pure side effect of set_current (the batch
        was derived from the current image and _build_cells was reachable from
        nowhere else), so a caller with pages but no "current" image — the
        upscaled-image browser (#22) — had to fake one to turn the page. This is
        the same code, driven directly; set_current calls it too.

        It deliberately does NOT touch the blue current-frame: a page is a
        viewport, not a statement about what is being processed.
        """
        pages = self.page_count()
        if pages == 0:
            self._batch = None
            self._build_cells([])
            return
        n = max(0, min(pages - 1, int(n)))
        if n == self._batch:
            return
        self._batch = n
        sl = self._paths[n * self._page_size:(n + 1) * self._page_size]
        self._build_cells(sl, first=first)
        self.canvas.yview_moveto(0)         # new page — start at the top

    def set_current(self, path):
        if path not in self._index:     # rescan oddity — still show the image
            self._index[path] = len(self._paths)
            self._paths.append(path)
        idx = self._index[path]
        self.show_page(idx // self._page_size, first=path)
        # The current image is only marked (highlighted); the view is NOT
        # moved, so scrolling/browsing is never interrupted mid-run.
        self._current = path
        self._draw_highlight()

    def set_status(self, path, status, compare_to=None):
        """Record a per-image outcome ('ok'/'fail') and, if that image is in the
        batch on screen, frame it (green/red). Run-level: kept across batches, so
        scrolling back to an earlier batch still shows its outcomes. `compare_to`
        (the upscaled output path) enables double-click-to-compare for that
        thumbnail."""
        path = (path or "").strip()
        if not path or status not in ("ok", "fail"):
            return
        self._status[path] = status
        if compare_to:
            self._compare[path] = compare_to
        if path in self._pos:
            self._draw_frame(path)
            self._draw_highlight()      # keep the blue current-frame on top

    def clear_current(self):
        """Drop the blue 'currently processing' highlight (called when a run
        ends). Without this the last image keeps its blue frame even though it is
        finished — it should show its own green/red outcome frame instead."""
        if self._current is None:
            return
        last = self._current
        self._current = None
        self._draw_highlight()          # _current is None now → removes the rect
        if last in self._pos:           # reveal the finished image's outcome frame
            self._draw_frame(last)

    # ── Layout ───────────────────────────────────────────────────────────────

    def _build_cells(self, order, first=None):
        self._order   = list(order)
        self._pos     = {p: i for i, p in enumerate(order)}
        self._master  = {}
        self._photo   = {}
        self._img_id  = {}
        self._frame_id = {}
        self._hl_id   = None
        self._relayout(force=True)
        self._start_loader(order, first=first)

    def _grid_cols(self):
        w = max(self.canvas.winfo_width(), 1)
        return max(1, (w - GRID_GAP) // (self._cell + GRID_GAP))

    def _cell_origin(self, i):
        col = i % self._cols
        row = i // self._cols
        x0  = GRID_GAP + col * (self._cell + GRID_GAP)
        y0  = GRID_GAP + row * (self._cell + GRID_GAP)
        return x0, y0

    def _relayout(self, force=False):
        cols = self._grid_cols()
        if not force and cols == self._cols:
            return
        self._cols = cols
        self.canvas.delete("all")
        self._img_id = {}
        self._frame_id = {}
        self._hl_id  = None
        cell = self._cell
        for i, p in enumerate(self._order):
            x0, y0 = self._cell_origin(i)
            self.canvas.create_rectangle(x0, y0, x0 + cell, y0 + cell,
                                         outline=self.OUTLINE, width=1)
            photo = self._photo.get(p)
            if photo is not None:
                self._img_id[p] = self.canvas.create_image(
                    x0 + cell / 2, y0 + cell / 2, image=photo)
        self._update_scrollregion()
        self._draw_frames()
        self._draw_highlight()

    def _update_scrollregion(self):
        cols = max(self._cols, 1)
        rows = (len(self._order) + cols - 1) // cols
        h    = GRID_GAP + rows * (self._cell + GRID_GAP)
        w    = max(self.canvas.winfo_width(), 1)
        self.canvas.configure(
            scrollregion=(0, 0, w, max(h, self.canvas.winfo_height())))

    def _place(self, p):
        """Draw or refresh one thumbnail at its grid position."""
        i = self._pos.get(p)
        if i is None:
            return
        photo = self._photo.get(p)
        if photo is None:
            return
        x0, y0 = self._cell_origin(i)
        cx, cy = x0 + self._cell / 2, y0 + self._cell / 2
        if p in self._img_id:
            self.canvas.itemconfigure(self._img_id[p], image=photo)
            self.canvas.coords(self._img_id[p], cx, cy)
        else:
            self._img_id[p] = self.canvas.create_image(cx, cy, image=photo)

    def _draw_frames(self):
        """(Re)draw every status frame for the batch on screen."""
        for p in list(self._frame_id):
            self.canvas.delete(self._frame_id.pop(p))
        for p in self._order:
            if p in self._status:
                self._draw_frame(p)

    def _draw_frame(self, path):
        """Draw (or redraw) one image's green/red outcome frame."""
        old = self._frame_id.pop(path, None)
        if old is not None:
            self.canvas.delete(old)
        i = self._pos.get(path)
        status = self._status.get(path)
        if i is None or status is None:
            return
        x0, y0 = self._cell_origin(i)
        cell  = self._cell
        color = self.OK_FRAME if status == "ok" else self.FAIL_FRAME
        self._frame_id[path] = self.canvas.create_rectangle(
            x0, y0, x0 + cell, y0 + cell, outline=color, width=2)

    def _draw_highlight(self):
        if self._hl_id is not None:
            self.canvas.delete(self._hl_id)
            self._hl_id = None
        i = self._pos.get(self._current) if self._current else None
        if i is None:
            return
        x0, y0 = self._cell_origin(i)
        cell = self._cell
        self._hl_id = self.canvas.create_rectangle(
            x0 - 1, y0 - 1, x0 + cell + 1, y0 + cell + 1,
            outline=self.HILITE, width=3)

    def _on_resize(self, _event):
        if self._resize_after is not None:
            self.after_cancel(self._resize_after)
        self._resize_after = self.after(150, self._do_resize)

    def _do_resize(self):
        self._resize_after = None
        # A shrunk viewport may no longer fit the current cell — pull it in.
        max_cell = self._max_cell()
        if self._cell > max_cell:
            self._cell = max_cell
            for p in list(self._master):
                self._make_photo(p)
            self._relayout(force=True)
            if self._on_zoom:
                self._on_zoom(self._cell)
        elif self._grid_cols() != self._cols:
            self._relayout(force=True)
        else:
            self._update_scrollregion()

    def _path_at(self, event):
        """Map a canvas click to the queued image path under it, or None for
        empty space. Shared by double-click-to-open and the right-click menu."""
        x = self.canvas.canvasx(event.x)
        y = self.canvas.canvasy(event.y)
        col = int((x - GRID_GAP) // (self._cell + GRID_GAP))
        row = int((y - GRID_GAP) // (self._cell + GRID_GAP))
        if col < 0 or col >= self._cols:
            return None
        idx = row * self._cols + col
        if not (0 <= idx < len(self._order)):
            return None
        return self._order[idx]

    def _on_double(self, event):
        path = self._path_at(event)
        if not path:
            return
        # A comparable (green) thumbnail with a known upscaled output opens the
        # comparison window; anything else just opens the file.
        out = self._compare.get(path)
        if self.on_compare and out and os.path.exists(out) and os.path.exists(path):
            self.on_compare(path, out)
        else:
            self._open(path)

    def _on_right_click(self, event):
        """Context menu over a thumbnail. Entries depend on the image's outcome:
        a failed image offers only its folder; a processed (green) image offers
        the original, the upscaled counterpart, their folders and Compare; an
        unprocessed/processing image offers just open-image / open-folder."""
        path = self._path_at(event)
        if not path:
            return
        status   = self._status.get(path)
        upscaled = self._compare.get(path)
        menu = tk.Menu(self, tearoff=0)
        if status == "fail":
            menu.add_command(label="Open failed image folder",
                             command=lambda p=path: self._open_folder(p))
        elif status == "ok" and upscaled:
            menu.add_command(label="Open original image",
                             command=lambda p=path: self._open(p))
            menu.add_command(label="Open original image folder",
                             command=lambda p=path: self._open_folder(p))
            menu.add_command(label="Open upscaled image",
                             command=lambda p=upscaled: self._open(p))
            menu.add_command(label="Open upscaled image folder",
                             command=lambda p=upscaled: self._open_folder(p))
            menu.add_separator()
            can_compare = bool(self.on_compare and os.path.exists(upscaled)
                               and os.path.exists(path))
            menu.add_command(
                label="Compare images",
                command=lambda p=path, o=upscaled: self.on_compare(p, o),
                state=("normal" if can_compare else "disabled"))
        else:
            # Unprocessed or currently processing — no outcome recorded yet (and
            # tag-only "ok" with no upscaled counterpart falls here too).
            menu.add_command(label="Open image",
                             command=lambda p=path: self._open(p))
            menu.add_command(label="Open image folder",
                             command=lambda p=path: self._open_folder(p))
        # Clipboard helpers, useful in every state (the thumbnail's source path).
        menu.add_separator()
        menu.add_command(label="Copy path",
                         command=lambda p=path: self._to_clipboard(p))
        menu.add_command(label="Copy filename",
                         command=lambda p=path: self._to_clipboard(os.path.basename(p)))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _to_clipboard(self, text):
        """Put `text` on the clipboard (so it survives after the app closes,
        Tk owns the selection while running). Fail-safe."""
        try:
            self.clipboard_clear()
            self.clipboard_append(text)
        except tk.TclError:
            pass

    @staticmethod
    def _open(path):
        if os.path.exists(path):
            try:
                os.startfile(path)
            except OSError:
                pass

    @staticmethod
    def _open_folder(path):
        """Open the folder holding `path` in Explorer with the file selected.

        Delegates to the ONE implementation in gui.common. This used to be a copy,
        and the copy is where the bug lived: four call sites had spelled the
        explorer command three different ways, and two of the three spellings are
        wrong (see `open_in_explorer` for the measurements). A gesture this small
        does not deserve four implementations, and four is exactly how a fix in one
        of them fails to reach the other three."""
        open_in_explorer(path)

    # ── Background thumbnail loading ─────────────────────────────────────────

    def _start_loader(self, paths, first=None):
        self._gen += 1
        if first in paths:              # decode the current image first
            i = paths.index(first)
            paths = paths[i:] + paths[:i]
        threading.Thread(target=self._load_batch,
                         args=(list(paths), self._gen), daemon=True).start()

    def _resolve_renamed(self, p):
        """Follow any rename(s) recorded for `p` to its latest on-disk path.
        The undo pass renames each file back to its original right after we
        start decoding, so the path captured at batch start may be stale."""
        seen = 0
        while p in self._renamed and seen < 8:
            p = self._renamed[p]
            seen += 1
        return p

    def _load_batch(self, paths, gen):
        from PIL import Image, ImageOps
        for p in paths:
            if gen != self._gen:
                return                  # batch changed — abandon
            img = None
            # Decode the current on-disk path. A rename can land mid-decode
            # (file vanishes from under us), so re-resolve and retry a few
            # times before giving up — otherwise that thumbnail stays blank.
            for attempt in range(4):
                src = self._resolve_renamed(p)
                # A RAW cannot be opened by Pillow at all (#19), so it would burn
                # the whole retry loop below and still draw an empty cell. Its
                # embedded preview is right there and costs nothing.
                if _is_raw(src):
                    img = _raw_thumb(src, THUMB_MASTER)
                    if img is not None:
                        break
                try:
                    with Image.open(src) as f:
                        f.draft("RGB", (THUMB_MASTER, THUMB_MASTER))
                        f = ImageOps.exif_transpose(f)
                        f.thumbnail((THUMB_MASTER, THUMB_MASTER), Image.LANCZOS)
                        img = f.convert("RGB")
                    break
                except Exception:
                    img = None
                    if gen != self._gen:
                        return
                    time.sleep(0.05)    # let a pending rename event land
            self._q.put((gen, p, img))

    def _make_photo(self, p):
        from PIL import Image, ImageTk
        m = self._master.get(p)
        if m is None:
            return
        # Fit the image to the square cell, preserving aspect. Cells larger
        # than the decoded master upscale it (mildly soft at extreme zoom).
        scale = min(self._cell / m.width, self._cell / m.height)
        w = max(1, int(round(m.width * scale)))
        h = max(1, int(round(m.height * scale)))
        img = m if (w == m.width and h == m.height) else m.resize((w, h), Image.LANCZOS)
        self._photo[p] = ImageTk.PhotoImage(img)

    def drain(self):
        """Main thread (via _tick): turn decoded masters into placed thumbnails."""
        changed = False
        while True:
            try:
                gen, p, img = self._q.get_nowait()
            except queue.Empty:
                break
            # The file may have been renamed while its thumbnail was decoding
            for _ in range(8):
                if p in self._renamed:
                    p = self._renamed[p]
                else:
                    break
            if gen != self._gen or p not in self._pos or img is None:
                continue
            self._master[p] = img
            self._make_photo(p)
            self._place(p)
            changed = True
        if changed:
            # A freshly placed thumbnail is created above any existing frame, so
            # redraw the frames (and the highlight) to keep them on top.
            self._draw_frames()
            self._draw_highlight()
