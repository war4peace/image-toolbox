"""
gui/comparison.py
-----------------
The floating before/after comparison windows: ComparisonWindow (original vs.
upscaled, a shared vertical-wipe canvas with linked zoom/pan) and its subclass
VideoComparisonWindow. One shared instance each, geometry persisted via
gui.common.

Pillow (Image/ImageOps/ImageTk) is imported lazily inside the methods that decode
frames, so this module loads without PIL (the CI import smoke runs PIL-free).

The lens-view geometry (future-features #14) lives in the three pure functions
below rather than inside the window, so it can be tested without a display: they
are the whole of the feature's arithmetic, and getting the sample rect wrong is
the only way a lens can lie about what it is showing.
"""

import os
import subprocess
import threading

import tkinter as tk
from tkinter import ttk

from gui.common import APP_TITLE, CREATE_NO_WINDOW, _geometry_on_screen, save_settings
from gui.video_player import VideoPlayer, load_vlc, prompt_install_libvlc
from gui.widgets import Tooltip


# ─────────────────────────────────────────────
#  LENS GEOMETRY  (pure, display-free: see #14)
# ─────────────────────────────────────────────

# Magnification used when the two images are the SAME size, so there is no upscale
# ratio to derive one from (a re-run at an already-reached target, a video pair
# both sides of which are 1080p). Upscayl hard-codes this figure for every case.
LENS_FALLBACK_MAG = 4.0
# Below this the pair is treated as "same size": a ratio of 1.02 is a rounding
# artefact of a box-fit, not an upscale, and magnifying by it would show nothing.
LENS_MIN_RATIO = 1.05

# Lens zoom steps, on top of the ratio-derived magnification. 1 puts the upscaled
# panel at its native 1:1; the rest multiply from there (a 2x pair at lens 4 shows
# the original at 8x and the upscaled at 4:1).
LENS_ZOOMS = (1, 2, 4, 8)
# Panel layout. The base panel FOLLOWS THE WINDOW (a maximised window on a big
# monitor got the same 180 px stamp as a 1100 px one, which is half of why the
# lens read as "so small I had to squint"), and a zoom step grows it further until
# the canvas runs out, after which the zoom keeps rising by narrowing the patch.
LENS_PANEL_FRAC = 0.16       # of canvas width, at zoom 1
LENS_PANEL_MIN  = 140
LENS_PANEL_MAX  = 640
LENS_PANEL_FLOOR = 60        # a tiny window still shows something
LENS_GAP     = 6             # between the two panels
LENS_PAD     = 6             # inside the surrounding box
LENS_LABEL_H = 16            # the "Original ·  2.0×" caption row
# Fraction of the canvas the whole box may occupy. Not 1.0 and not close to it:
# past roughly this, the panels swallow the picture they were magnifying out of and
# there is nothing left to tell you WHERE you are looking.
LENS_FIT_W   = 0.7
LENS_FIT_H   = 0.75


def lens_panel_size(canvas_w, canvas_h, zoom):
    """Side of ONE magnified panel, in px.

    Grows with the window and with the zoom step, but never past what the canvas
    can hold: both panels plus the frame have to fit, or the "bigger" zoom would
    push half the comparison off-screen. Once the cap bites, the zoom still works
    (`_lens_sample` samples a proportionally smaller patch into the same panel),
    it just stops growing the panel.
    """
    base = min(max(canvas_w * LENS_PANEL_FRAC, LENS_PANEL_MIN), LENS_PANEL_MAX)
    fit_w = (canvas_w * LENS_FIT_W - LENS_GAP - 2 * LENS_PAD) / 2.0
    fit_h = canvas_h * LENS_FIT_H - LENS_LABEL_H - 2 * LENS_PAD
    return int(max(LENS_PANEL_FLOOR,
                   min(base * zoom, fit_w, fit_h, float(LENS_PANEL_MAX))))


def lens_zoom_floor(view_scale, current):
    """The zoom to start at, given how large the canvas behind the lens is already
    drawing the upscaled image (`view_scale` = screen px per upscaled px).

    A lens that magnifies LESS than the view it sits on is not a lens: on a small
    video in a maximised window the canvas is already at ~2.7x, so a 1:1 panel
    showed the patch *smaller* than it appeared behind it. Never lowers the user's
    own choice, so this can only help.
    """
    for z in LENS_ZOOMS:
        if z >= view_scale - 1e-9:
            return max(current, z)
    return max(current, LENS_ZOOMS[-1])


def lens_magnification(old_size, new_size):
    """Screen px per ORIGINAL px inside the lens, and whether that lands the
    upscaled panel on its own native pixels.

    Derived from the REAL upscale ratio instead of a hard-coded figure: at the
    ratio, the upscaled panel is exactly 1:1 with the file that was produced, so
    the panel shows the deliverable's own pixels and the left panel shows the
    original interpolated up to meet it. That is the honest comparison, and it is
    the one the user is actually deciding about. Falls back to a fixed
    magnification when there is no ratio to use.

    Returns (magnification, native). `native` False means the fallback is in use
    and NEITHER panel is at 1:1.
    """
    if not old_size or not new_size:
        return LENS_FALLBACK_MAG, False
    ow, oh = old_size
    nw, nh = new_size
    if min(ow, oh, nw, nh) <= 0:
        return LENS_FALLBACK_MAG, False
    ratio = min(nw / float(ow), nh / float(oh))
    if ratio < LENS_MIN_RATIO:
        return LENS_FALLBACK_MAG, False
    return ratio, True


def lens_span(centre, extent_px, span_px, snap=False):
    """One axis of the sample rect, as a normalized (a0, a1) pair.

    `centre` is the pointer's position in that axis as a 0..1 fraction of the
    image; the window is `span_px` wide, clamped to stay inside the image (so the
    lens never samples off the edge and the two panels can never come out of
    step). `snap` aligns it to whole pixels, which is what lets the upscaled panel
    be a plain crop with no resampling at all.
    """
    extent_px = float(extent_px)
    if extent_px <= 0:
        return 0.0, 1.0
    span = min(float(span_px), extent_px)
    a = centre * extent_px - span / 2.0
    if snap:
        span = float(max(1, round(span)))
        a = float(round(a))
    a = min(max(0.0, a), max(0.0, extent_px - span))
    return a / extent_px, (a + span) / extent_px


def lens_placement(cx, cy, box_w, box_h, cw, ch, offset):
    """Top-left of the lens box for a pointer at (cx, cy): below-right of the
    cursor, flipped to the other side when it would overflow, then clamped into
    the canvas so the panels are never half off-screen at an edge (which is
    exactly where a magnifier is most wanted)."""
    x = cx + offset
    if x + box_w > cw:
        x = cx - offset - box_w
    y = cy + offset
    if y + box_h > ch:
        y = cy - offset - box_h
    x = min(max(0.0, x), max(0.0, cw - box_w))
    y = min(max(0.0, y), max(0.0, ch - box_h))
    return x, y


# ─────────────────────────────────────────────
#  COMPARISON WINDOW  (original vs. upscaled)
# ─────────────────────────────────────────────

class ComparisonWindow(tk.Toplevel):
    """
    Floating, resizable original-vs-upscaled comparison (Future Feature #1).

    Both images are drawn aligned on a single canvas, split by a vertical
    **before/after wipe**: left of the divider shows the original, right shows the
    upscaled result. Zoom and pan are *shared*, so the two halves always show the
    same region — the quality gain is directly visible (the lower-resolution
    original, magnified to the same on-screen scale, reads softer).

    Interaction:
      * mouse wheel  — zoom in/out, centred on the pointer (fit … 400% of the
        upscaled image's native pixels)
      * drag image   — pan, clamped so the view stays filled
      * drag divider — slide the wipe (grab the handle / the divider line)
      * Lens         : a second view mode (#14), see below

    Only the visible region of each side is decoded (Pillow ``resize`` with a
    float ``box``), so even 4K stays responsive; an interactive gesture renders
    with a fast filter and a crisp pass follows when it settles. Like the log
    window there is one shared instance, re-targeted on each double-click.

    **Lens view** (future-features #14). What a wipe cannot do is show one patch
    as original *and* upscaled at the same moment: it shows either, and you slide
    the divider to swap, so the eye is comparing against a memory. The lens shows
    the patch under the pointer twice, side by side, and the comparison happens in
    one glance. It is a MODE, not an overlay: while it is on the divider is put
    away and the canvas shows the upscaled image full-frame as the context to
    magnify out of, because two pointer gestures on one canvas would fight (the
    wipe wants the drag; the lens wants the hover).

    **The wheel zooms the lens** through 1x / 2x / 4x / 8x on top of the upscale
    ratio, growing the panels as it goes (Ctrl+wheel still zooms the picture
    behind). Without this the lens was only useful on a big pair in a modest
    window: a 320x240 -> 640x480 video in a maximised window is already drawn at
    ~2.7x on the canvas, so a "1:1" panel showed the patch SMALLER than it already
    appeared behind it, in a 180 px stamp. So the panel size follows the window,
    the zoom grows it further, and `lens_zoom_floor` starts the lens at least as
    strong as the view it sits on.

    Click to pin the lens where it is (and click again, or Esc, to release):
    without a pin it follows the pointer and vanishes when the pointer leaves,
    which is right for sweeping across an image but useless for looking at one
    spot while the hand moves, or for a screenshot. Both the toggle and the zoom
    are remembered across sessions.
    """

    ZOOM_STEP = 1.25         # per wheel notch
    ABS_MAX   = 4.0          # max screen px per upscaled-image px (≈400%)
    HANDLE_TOL = 9           # px around the divider that grabs the wipe
    DIVIDER   = "#e8edf3"
    HANDLE_FILL = "#e8edf3"
    HANDLE_EDGE = "#202329"

    # ── lens view (#14) ──
    LENS_SETTING = "compare_lens"           # remembered on/off, both windows
    LENS_ZOOM_SETTING = "compare_lens_zoom"  # remembered zoom step
    LENS_OFFSET  = 22        # px from the pointer to the box
    CLICK_TOL    = 4         # px of movement still counted as a click, not a pan
    LENS_BG      = "#1b1f26"
    LENS_EDGE    = "#8b93a1"
    LENS_PIN_EDGE = "#5aa9e6"
    LENS_TEXT    = "#aab2bf"

    # Simple defaults live on the class so BOTH windows inherit them: the video
    # subclass builds its own state block and would otherwise have to repeat them.
    _press_at = None
    _lens_on  = False        # set for real from the saved preference in _build_top_bar
    _lens_zoom = 1           # ditto
    _lens_floor_pending = False   # apply lens_zoom_floor at the next draw
    _lens_pin = None         # canvas (x, y) while pinned, else None
    _lens_pos = None         # canvas (x, y) the pointer is at, else None

    def __init__(self, master, source, output, app=None):
        super().__init__(master)
        self._app = app
        geo = app.settings.get("compare_geometry") if app is not None else None
        self.geometry(geo if (geo and _geometry_on_screen(self, geo)) else "1100x640")
        self.minsize(560, 360)
        self._last_normal_geo = None
        if app is not None and app.settings.get("compare_zoomed"):
            try:
                self.state("zoomed")
            except tk.TclError:
                pass

        self._build_top_bar()

        self.canvas = tk.Canvas(self, highlightthickness=0, bg="#15181d")
        self.canvas.pack(fill="both", expand=True)

        # View state (shared by both images)
        self._old = None          # PIL image, native resolution
        self._new = None          # PIL image, native resolution (the reference)
        self._zoom = 1.0          # 1.0 == fit-to-window
        self._ox = self._oy = 0.0 # canvas px of the display rect's top-left
        self._wipe_frac = 0.5     # divider position as a fraction of width
        self._inited = False      # offsets centred once images/size are known
        self._last_size = None    # (cw, ch) for centre-preserving resize
        self._drag = None         # None | ("pan", x, y) | ("wipe",)
        self._photos = []         # keep PhotoImage refs alive
        self._crisp_after = None

        self.canvas.bind("<Configure>", self._on_configure)
        self.canvas.bind("<MouseWheel>", self._on_wheel)
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Motion>", self._on_motion)
        self.canvas.bind("<Leave>", self._on_leave)
        self._bind_lens_keys()
        # Track the last non-maximised geometry on the Toplevel itself, so a
        # close-while-maximised still records a sensible size + the zoomed flag.
        self.bind("<Configure>", self._track_geometry, add="+")

        self.protocol("WM_DELETE_WINDOW", self._close)
        self.show(source, output)

    # ── Top bar + lens toggle (shared with the video subclass) ───────────────

    def _build_top_bar(self):
        """The caption strip: original (left), the lens toggle (centre), upscaled
        (right). Built here rather than in each __init__ so the toggle cannot end
        up on one comparison window and not the other."""
        bar = ttk.Frame(self, padding=(8, 4))
        bar.pack(fill="x")
        bar.columnconfigure(0, weight=1, uniform="caption")
        bar.columnconfigure(2, weight=1, uniform="caption")
        self._old_lbl = ttk.Label(bar, foreground="#aab2bf")
        self._old_lbl.grid(row=0, column=0, sticky="w")
        self._new_lbl = ttk.Label(bar, foreground="#aab2bf")
        self._new_lbl.grid(row=0, column=2, sticky="e")

        mid = ttk.Frame(bar)
        mid.grid(row=0, column=1)
        settings = self._app.settings if self._app is not None else {}
        on = bool(settings.get(self.LENS_SETTING))
        zoom = settings.get(self.LENS_ZOOM_SETTING)
        self._lens_on = on
        self._lens_zoom = zoom if zoom in LENS_ZOOMS else LENS_ZOOMS[0]
        self._lens_photos = []
        self.lens_var = tk.BooleanVar(value=on)
        self.lens_cb = ttk.Checkbutton(mid, text="Lens", variable=self.lens_var,
                                       command=self._on_lens_toggle)
        self.lens_cb.pack(side="left")
        Tooltip(self.lens_cb,
                "Magnify the spot under the pointer as BOTH images at once, side "
                "by side, instead of sliding the divider back and forth. "
                "Scroll to zoom the lens (1x / 2x / 4x / 8x, the panels grow with "
                "it); at 1x the upscaled half is its own real pixels. Click the "
                "image to pin the lens in place, click again (or press Esc) to "
                "release. Ctrl+scroll still zooms the picture behind. Shortcut: L.",
                wraplength=Tooltip.WRAP_NARROW)
        self._lens_hint = ttk.Label(mid, foreground="#6b7280")
        self._lens_hint.pack(side="left", padx=(8, 0))
        self._update_lens_hint()

    def _bind_lens_keys(self):
        self.bind("<KeyPress-l>", self._on_lens_key)
        self.bind("<KeyPress-L>", self._on_lens_key)
        self.bind("<Escape>", self._on_escape)

    def _on_lens_key(self, _event=None):
        self.lens_var.set(not self.lens_var.get())
        self._on_lens_toggle()

    def _on_escape(self, _event=None):
        """Release a pinned lens. Deliberately does NOT close the window: Esc is
        the way out of the pin, and a window holding a large decoded pair should
        not vanish on a stray keypress."""
        if self._lens_pin is not None:
            self._lens_pin = None
            self._update_lens_hint()
            self._render()

    def _on_lens_toggle(self):
        self._lens_on = bool(self.lens_var.get())
        self._lens_pin = None
        if not self._lens_on:
            self._lens_pos = None
        else:
            # Entering lens mode is one of the two moments the floor applies (the
            # other is a new pair); it is deferred to the draw, because the video
            # window has no frame decoded yet when a pair is opened.
            self._lens_floor_pending = True
        try:
            self.canvas.configure(cursor="crosshair" if self._lens_on else "fleur")
        except tk.TclError:
            pass
        self._update_lens_hint()
        self._save_lens_prefs()
        self._render()

    def _step_lens_zoom(self, direction, event=None):
        """One wheel notch: the next/previous zoom step. Redraws the lens items
        only, so a wheel spin never re-decodes the picture behind it."""
        i = LENS_ZOOMS.index(self._lens_zoom) if self._lens_zoom in LENS_ZOOMS else 0
        j = min(max(i + direction, 0), len(LENS_ZOOMS) - 1)
        self._lens_floor_pending = False       # the user has taken the wheel
        if j == i:
            return
        self._lens_zoom = LENS_ZOOMS[j]
        if event is not None and self._lens_pin is None:
            self._lens_pos = (event.x, event.y)
        self._update_lens_hint()
        self._save_lens_prefs()
        self._draw_lens()

    def _save_lens_prefs(self):
        if self._app is not None:
            self._app.settings[self.LENS_SETTING] = self._lens_on
            self._app.settings[self.LENS_ZOOM_SETTING] = self._lens_zoom
            save_settings(self._app.settings)

    def _update_lens_hint(self):
        if not self._lens_on:
            text = ""
        elif self._lens_pin is not None:
            text = f"scroll to zoom ({self._lens_zoom}×) · click or Esc to release"
        else:
            text = f"scroll to zoom ({self._lens_zoom}×) · click to pin"
        try:
            self._lens_hint.configure(text=text)
        except tk.TclError:
            pass

    # ── Public API (shared single instance) ──────────────────────────────────

    def show(self, source, output):
        """Point the window at a new pair and reset the view to fit."""
        from PIL import Image, ImageOps

        def _load(path):
            try:
                im = Image.open(path)
                return ImageOps.exif_transpose(im).convert("RGB")
            except Exception:
                return None

        self._old = _load(source)
        self._new = _load(output)
        self._old_lbl.configure(text="Original:  " + self._caption(source, self._old))
        self._new_lbl.configure(text="Upscaled:  " + self._caption(output, self._new))
        self.title(f"{APP_TITLE} — Compare — {os.path.basename(source)}")
        self._zoom = 1.0
        self._wipe_frac = 0.5
        self._inited = False
        self._reset_lens_position()
        self.lift()
        self.focus_set()
        self._render()

    def _track_geometry(self, event):
        if event.widget is self and self.state() == "normal":
            self._last_normal_geo = self.geometry()

    def save_geometry(self):
        if self._app is not None and self.winfo_exists():
            try:
                zoomed = (self.state() == "zoomed")
            except tk.TclError:
                zoomed = False
            self._app.settings["compare_geometry"] = self._last_normal_geo or self.geometry()
            self._app.settings["compare_zoomed"] = zoomed
            save_settings(self._app.settings)

    # ── Geometry helpers ─────────────────────────────────────────────────────

    @staticmethod
    def _caption(path, img):
        if img is None:
            return f"{os.path.basename(path)}  ·  (cannot open)"
        return f"{os.path.basename(path)}  ·  {img.width}×{img.height}"

    def _size(self):
        return max(self.canvas.winfo_width(), 1), max(self.canvas.winfo_height(), 1)

    def _fit(self, cw, ch):
        """Absolute scale (screen px per reference px) at zoom == 1.0."""
        return min(cw / self._new.width, ch / self._new.height)

    def _zoom_max(self, fit):
        return max(1.0, self.ABS_MAX / fit)

    def _view_scale(self):
        """Screen px per upscaled-image px on the canvas right now."""
        if self._new is None:
            return 1.0
        cw, ch = self._size()
        return self._fit(cw, ch) * self._zoom

    @staticmethod
    def _clamp_axis(o, disp, view):
        """Keep the view filled: centre when the image is smaller than the view,
        otherwise stop the image edges from leaving a gap."""
        if disp <= view:
            return (view - disp) / 2.0
        return min(0.0, max(view - disp, o))

    # ── Interaction ──────────────────────────────────────────────────────────

    def _on_configure(self, _event):
        if self._new is None:
            return
        cw, ch = self._size()
        if self._inited and self._last_size and self._last_size != (cw, ch):
            # Preserve the image point at the view centre across a resize.
            ocw, och = self._last_size
            os_ = self._fit(ocw, och) * self._zoom
            odw, odh = self._new.width * os_, self._new.height * os_
            cu = (ocw / 2 - self._ox) / odw if odw else 0.5
            cv = (och / 2 - self._oy) / odh if odh else 0.5
            ns = self._fit(cw, ch) * self._zoom
            self._ox = cw / 2 - cu * self._new.width * ns
            self._oy = ch / 2 - cv * self._new.height * ns
        self._render()

    # Ctrl held (Tk's ButtonPress/Motion state mask), the escape hatch that keeps
    # the picture behind the lens zoomable while the plain wheel drives the lens.
    CTRL_MASK = 0x0004

    def _on_wheel(self, event):
        if self._new is None:
            return
        if self._lens_on and not (getattr(event, "state", 0) & self.CTRL_MASK):
            self._step_lens_zoom(1 if event.delta > 0 else -1, event)
            return
        cw, ch = self._size()
        fit = self._fit(cw, ch)
        s = fit * self._zoom
        dw, dh = self._new.width * s, self._new.height * s
        # Image-space fraction under the pointer, kept fixed across the zoom.
        u = (event.x - self._ox) / dw if dw else 0.5
        v = (event.y - self._oy) / dh if dh else 0.5
        factor = self.ZOOM_STEP if event.delta > 0 else 1.0 / self.ZOOM_STEP
        nz = min(max(self._zoom * factor, 1.0), self._zoom_max(fit))
        if nz == self._zoom:
            return
        s2 = fit * nz
        self._ox = event.x - u * self._new.width * s2
        self._oy = event.y - v * self._new.height * s2
        self._zoom = nz
        self._render(fast=True)
        self._schedule_crisp()

    def _on_press(self, event):
        if self._new is None:
            return
        self._press_at = (event.x, event.y)
        cw, _ = self._size()
        wipe_x = self._wipe_frac * cw
        # In lens mode there is no divider on screen, so a press there is a pan
        # like anywhere else (grabbing an invisible handle would be a mystery).
        if not self._lens_on and abs(event.x - wipe_x) <= self.HANDLE_TOL:
            self._drag = ("wipe",)
        else:
            self._drag = ("pan", event.x, event.y)

    def _on_drag(self, event):
        if self._drag is None or self._new is None:
            return
        cw, ch = self._size()
        if self._drag[0] == "wipe":
            self._wipe_frac = min(1.0, max(0.0, event.x / cw))
        else:
            _, lx, ly = self._drag
            self._ox += event.x - lx
            self._oy += event.y - ly
            self._drag = ("pan", event.x, event.y)
        self._render(fast=True)

    def _on_release(self, event):
        # A press-and-release that did not move is a CLICK, and in lens mode a
        # click pins (or releases) the lens. Measured against the press point, not
        # against "did _on_drag fire": a hand shake of a pixel or two fires drag
        # motion but is plainly not a pan gesture.
        moved = None
        if self._press_at is not None:
            moved = max(abs(event.x - self._press_at[0]), abs(event.y - self._press_at[1]))
        self._press_at = None
        had_drag = self._drag is not None
        self._drag = None
        if self._lens_on and moved is not None and moved <= self.CLICK_TOL:
            self._toggle_pin(event.x, event.y)
            return
        if had_drag:
            self._render(fast=False)

    def _toggle_pin(self, x, y):
        self._lens_pin = None if self._lens_pin is not None else (x, y)
        self._lens_pos = (x, y)
        self._update_lens_hint()
        self._render(fast=False)

    def _reset_lens_position(self):
        """A new pair invalidates where the lens was pointing (the old spot means
        nothing on a different image); the on/off preference survives, and the zoom
        is floored against the new pair's fit scale (a small video fills the window
        at a scale a 1:1 lens would be weaker than)."""
        self._lens_pin = None
        self._lens_pos = None
        self._lens_floor_pending = self._lens_on
        self._update_lens_hint()

    def _on_motion(self, event):
        if self._drag is not None or self._new is None:
            return
        if self._lens_on:
            self.canvas.configure(cursor="crosshair")
            if self._lens_pin is None:
                self._lens_pos = (event.x, event.y)
                self._draw_lens()          # lens items only; the base stays put
            return
        cw, _ = self._size()
        near = abs(event.x - self._wipe_frac * cw) <= self.HANDLE_TOL
        self.canvas.configure(cursor="sb_h_double_arrow" if near else "fleur")

    def _on_leave(self, _event):
        """Transient by default: the lens goes away with the pointer, so it never
        sits stale over an image nobody is looking at. A pinned lens stays."""
        if self._lens_on and self._lens_pin is None and self._lens_pos is not None:
            self._lens_pos = None
            self._clear_lens()

    def _schedule_crisp(self):
        if self._crisp_after is not None:
            self.after_cancel(self._crisp_after)
        self._crisp_after = self.after(140, lambda: self._render(fast=False))

    # ── Rendering ────────────────────────────────────────────────────────────

    def _render(self, fast=False):
        from PIL import Image

        self._crisp_after = None
        # A redraw can be queued (off-thread decode's after(0), or the deferred
        # crisp pass) and then fire AFTER the window is closed and its canvas
        # destroyed -> "invalid command name …!canvas". Bail if the canvas is gone.
        if not self.canvas.winfo_exists():
            return
        self.canvas.delete("all")
        self._photos = []
        cw, ch = self._size()
        if self._new is None:
            self.canvas.create_text(cw / 2, ch / 2, fill="#aab2bf",
                                    text="Cannot open the upscaled image.")
            return

        s = self._fit(cw, ch) * self._zoom
        disp_w = self._new.width * s
        disp_h = self._new.height * s
        if not self._inited:
            self._ox = (cw - disp_w) / 2.0
            self._oy = (ch - disp_h) / 2.0
            self._inited = True
        self._ox = self._clamp_axis(self._ox, disp_w, cw)
        self._oy = self._clamp_axis(self._oy, disp_h, ch)
        self._last_size = (cw, ch)

        resample = Image.BILINEAR if fast else Image.LANCZOS
        if self._lens_on:
            # Lens mode replaces the wipe (see the class docstring): the canvas is
            # the upscaled image full-frame, as the context the lens magnifies out
            # of, and the comparison happens inside the two panels.
            self._blit(self._new, 0.0, float(cw), disp_w, disp_h, cw, ch, resample)
            self._draw_lens()
            return

        wipe_x = min(float(cw), max(0.0, self._wipe_frac * cw))
        # Left of the divider = original; right = upscaled. Same display rect, so
        # the two halves are pixel-aligned at every zoom/pan.
        if self._old is not None:
            self._blit(self._old, 0.0, wipe_x, disp_w, disp_h, cw, ch, resample)
        self._blit(self._new, wipe_x, float(cw), disp_w, disp_h, cw, ch, resample)
        self._draw_divider(wipe_x, ch)

    def _blit(self, img, x0, x1, disp_w, disp_h, cw, ch, resample):
        """Draw the slice of `img` visible in canvas x-range [x0, x1]."""
        from PIL import ImageTk

        cx0 = max(x0, self._ox, 0.0)
        cx1 = min(x1, self._ox + disp_w, float(cw))
        cy0 = max(0.0, self._oy)
        cy1 = min(float(ch), self._oy + disp_h)
        if cx1 - cx0 < 1 or cy1 - cy0 < 1:
            return
        iw, ih = img.size
        sx0 = (cx0 - self._ox) / disp_w * iw
        sx1 = (cx1 - self._ox) / disp_w * iw
        sy0 = (cy0 - self._oy) / disp_h * ih
        sy1 = (cy1 - self._oy) / disp_h * ih
        tw = max(1, int(round(cx1 - cx0)))
        th = max(1, int(round(cy1 - cy0)))
        tile = img.resize((tw, th), resample, box=(sx0, sy0, sx1, sy1))
        photo = ImageTk.PhotoImage(tile)
        self._photos.append(photo)
        self.canvas.create_image(int(round(cx0)), int(round(cy0)),
                                 anchor="nw", image=photo)

    def _draw_divider(self, wipe_x, ch):
        self.canvas.create_line(wipe_x, 0, wipe_x, ch, fill=self.DIVIDER, width=2)
        cy = ch / 2
        self.canvas.create_rectangle(wipe_x - 7, cy - 24, wipe_x + 7, cy + 24,
                                     fill=self.HANDLE_FILL, outline=self.HANDLE_EDGE)
        # Two little arrows hinting the handle slides horizontally.
        for dx, tip in ((-3, -7), (3, 7)):
            self.canvas.create_line(wipe_x + dx, cy - 5, wipe_x + tip, cy,
                                    wipe_x + dx, cy + 5, fill=self.HANDLE_EDGE)

    # ── Lens view (#14) ──────────────────────────────────────────────────────

    def _apply_zoom_floor(self):
        """Raise the zoom to at least the view's own scale, once per pair / once per
        switch into lens mode. Deferred to the draw because that is the first moment
        BOTH the image and the canvas size are known (the video window decodes its
        frame pair on a thread, so at `show_videos` time there is nothing to measure
        against). A wheel notch cancels it: past that point the zoom is the user's."""
        if not self._lens_floor_pending:
            return
        cw, ch = self._size()
        if cw < 50 or ch < 50:
            # The canvas is not realised yet (the first render happens inside
            # __init__, before the window is mapped, and Tk reports 1x1 there).
            # Keep the flag: the Configure that follows mapping tries again, and
            # a first open is exactly the case the floor exists for.
            return
        self._lens_floor_pending = False
        z = lens_zoom_floor(self._view_scale(), self._lens_zoom)
        if z != self._lens_zoom:
            self._lens_zoom = z
            self._update_lens_hint()
            self._save_lens_prefs()

    def _clear_lens(self):
        """Remove only the lens items. Every lens element carries the "lens" tag,
        so a pointer move redraws the two panels WITHOUT re-decoding the visible
        slice of the base image underneath (which is the expensive part)."""
        if self.canvas.winfo_exists():
            self.canvas.delete("lens")
        self._lens_photos = []

    def _lens_sample(self, cx, cy):
        """The normalized sample rect for a lens at canvas (cx, cy), plus the panel
        size it fills, the magnification and whether the upscaled panel is at 1:1.

        ONE rect, expressed in normalized coordinates and then mapped onto each
        image, is what guarantees the two panels magnify the same patch: deriving
        each panel's box from its own image would let them drift apart at the
        edges (where the clamp bites) and on any aspect-ratio mismatch. Returns
        None when the point is not on the image (the letterbox margin).
        """
        cw, ch = self._size()
        s = self._view_scale()
        disp_w, disp_h = self._new.width * s, self._new.height * s
        if disp_w < 1 or disp_h < 1:
            return None
        u = (cx - self._ox) / disp_w
        v = (cy - self._oy) / disp_h
        if not (0.0 <= u <= 1.0 and 0.0 <= v <= 1.0):
            return None
        old_size = self._old.size if self._old is not None else None
        mag, native = lens_magnification(old_size, self._new.size)
        ow, oh = old_size or self._new.size
        zoom = self._lens_zoom
        panel = lens_panel_size(cw, ch, zoom)
        # A panel shows `panel / (mag * zoom)` ORIGINAL pixels; the same patch in
        # the upscaled image is that times its own scale factor (== panel / zoom
        # when the magnification is the real ratio, so zoom 1 is native 1:1).
        orig_span = panel / float(mag * zoom)
        # Snapping buys the whole-pixel crop below, which only exists at 1:1.
        snap = native and zoom == 1
        u0, u1 = lens_span(u, self._new.width, orig_span * (self._new.width / float(ow)),
                           snap=snap)
        v0, v1 = lens_span(v, self._new.height, orig_span * (self._new.height / float(oh)),
                           snap=snap)
        return (u0, v0, u1, v1), panel, mag, native

    @staticmethod
    def _lens_tile(img, rect, size):
        """`rect` (normalized) of `img`, rendered into a `size`×`size` panel. A
        whole-pixel box that is already the panel's size is CROPPED, not resized:
        that is the native-1:1 case, and it must show the file's real pixels
        rather than a resampled approximation of them.

        Everything else goes through LANCZOS, for BOTH panels, at every zoom. The
        same filter on both sides is what keeps the comparison fair (nearest on the
        upscaled half would show it blocky against a smoothly interpolated
        original), and it is what the wipe does at 400% too."""
        from PIL import Image

        u0, v0, u1, v1 = rect
        box = (u0 * img.width, v0 * img.height, u1 * img.width, v1 * img.height)
        ints = [round(b) for b in box]
        if (all(abs(b - i) < 1e-6 for b, i in zip(box, ints))
                and ints[2] - ints[0] == size and ints[3] - ints[1] == size):
            return img.crop(tuple(ints))
        return img.resize((size, size), Image.LANCZOS, box=box)

    def _draw_lens(self):
        """Draw the two magnified panels (and the source-region marker). Cheap
        enough to run on every pointer move: both panels are small fixed-size
        renders, so there is no fast-filter-then-crisp dance here."""
        from PIL import ImageTk

        self._clear_lens()
        if not self._lens_on or self._new is None or not self.canvas.winfo_exists():
            return
        self._apply_zoom_floor()
        pos = self._lens_pin or self._lens_pos
        if pos is None:
            return
        sample = self._lens_sample(*pos)
        if sample is None:
            return
        rect, panel, mag, native = sample
        u0, v0, u1, v1 = rect
        cw, ch = self._size()
        pad, gap, lab_h = LENS_PAD, LENS_GAP, LENS_LABEL_H
        box_w = 2 * panel + gap + 2 * pad
        box_h = panel + lab_h + 2 * pad
        edge = self.LENS_PIN_EDGE if self._lens_pin is not None else self.LENS_EDGE

        # Marker on the full-frame view: which patch is in the panels.
        s = self._view_scale()
        disp_w, disp_h = self._new.width * s, self._new.height * s
        self.canvas.create_rectangle(self._ox + u0 * disp_w, self._oy + v0 * disp_h,
                                     self._ox + u1 * disp_w, self._oy + v1 * disp_h,
                                     outline=edge, dash=(3, 2), tags="lens")

        x, y = lens_placement(pos[0], pos[1], box_w, box_h, cw, ch, self.LENS_OFFSET)
        self.canvas.create_rectangle(x, y, x + box_w, y + box_h, fill=self.LENS_BG,
                                     outline=edge, tags="lens")
        # Both labels state the real magnification, so a zoomed lens can never be
        # mistaken for the 1:1 one (only zoom 1 may claim "1:1").
        zoom = self._lens_zoom
        left_lbl = f"Original  ·  {mag * zoom:.1f}×"
        right_lbl = f"Upscaled  ·  {zoom}:1" if native else f"Upscaled  ·  {mag * zoom:.1f}×"
        for i, (img, label) in enumerate(((self._old, left_lbl), (self._new, right_lbl))):
            px = x + pad + i * (panel + gap)
            py = y + pad + lab_h
            self.canvas.create_text(px + panel / 2, y + pad + lab_h / 2, text=label,
                                    fill=self.LENS_TEXT, font=("Segoe UI", 8),
                                    tags="lens")
            if img is None:
                self.canvas.create_text(px + panel / 2, py + panel / 2,
                                        text="(cannot open)", fill=self.LENS_TEXT,
                                        font=("Segoe UI", 8), tags="lens")
            else:
                photo = ImageTk.PhotoImage(self._lens_tile(img, rect, panel))
                self._lens_photos.append(photo)
                self.canvas.create_image(px, py, anchor="nw", image=photo, tags="lens")
            self.canvas.create_rectangle(px, py, px + panel, py + panel,
                                         outline=edge, tags="lens")

    def _close(self):
        self.save_geometry()
        self.destroy()


class VideoComparisonWindow(ComparisonWindow):
    """Original-vs-upscaled **video** comparison (Video Upscaler #2, phase 5). The
    video analogue of ComparisonWindow: it reuses the parent's shared zoom / pan /
    before-after-wipe rendering verbatim, and only swaps the image source — each
    seek decodes a frame PAIR from the two videos through the bundled ffmpeg
    (`-ss <t> -i <file> -frames:v 1` to a PNG pipe -> Pillow) and feeds them into
    the same renderer.

    v1 is **scrub + frame-step**, aligned by TIMESTAMP not frame index (the
    upscaled frame count can differ after a CFR-normalize, section 14), so seeking
    both sides to the same time keeps the same content under the wipe. Zoom/pan are
    preserved across seeks; only opening a new pair resets the view. Decoding runs
    off the UI thread. See docs/video-upscaler.md sections 11 / 15.

    The **lens** (#14) is inherited whole: the decoded frame pair is just another
    (old, new) pair to the renderer, so a lens on a paused frame works exactly as
    it does on a still. It does not fight the transport: the buttons and the
    scrubber are their own widgets, and a seek re-renders through the same path,
    which redraws the lens where it was pointing."""

    def __init__(self, master, source, upscaled, app=None):
        tk.Toplevel.__init__(self, master)
        self._app = app
        geo = app.settings.get("video_compare_geometry") if app is not None else None
        self.geometry(geo if (geo and _geometry_on_screen(self, geo)) else "1100x680")
        self.minsize(560, 380)
        self._last_normal_geo = None
        if app is not None and app.settings.get("video_compare_zoomed"):
            try:
                self.state("zoomed")
            except tk.TclError:
                pass

        self._build_top_bar()

        # Bottom transport: frame-step, a scrubber, and a time read-out. This window
        # is FRAMES ONLY (the before/after wipe with zoom/pan); it has NO libVLC.
        # Real-time playback with audio lives in the separate VideoPlaybackWindow
        # (section 16.3), so the two comparison styles never swap layouts mid-use.
        tl = ttk.Frame(self, padding=(8, 4))
        tl.pack(side="bottom", fill="x")
        ttk.Button(tl, text="◀ frame", width=8,
                   command=lambda: self._step(-1)).pack(side="left")
        ttk.Button(tl, text="frame ▶", width=8,
                   command=lambda: self._step(1)).pack(side="left", padx=(4, 8))
        self.time_var = tk.StringVar(value="0:00.000 / 0:00.000")
        ttk.Label(tl, textvariable=self.time_var, width=20,
                  font=("Consolas", 9)).pack(side="left")
        self.timeline = ttk.Scale(tl, from_=0.0, to=1.0, orient="horizontal",
                                  command=self._on_scrub)
        self.timeline.pack(side="left", fill="x", expand=True, padx=(8, 0))

        self.canvas = tk.Canvas(self, highlightthickness=0, bg="#15181d")
        self.canvas.pack(fill="both", expand=True)

        # Shared view state (consumed by the inherited renderer).
        self._old = self._new = None
        self._zoom = 1.0
        self._ox = self._oy = 0.0
        self._wipe_frac = 0.5
        self._inited = False
        self._last_size = None
        self._drag = None
        self._photos = []
        self._crisp_after = None
        # Video state.
        self._src_path = self._up_path = None
        self._duration = 0.0
        self._fps = 30.0
        self._cur_t = 0.0
        self._scrub_after = None
        self._seek_seq = 0

        self.canvas.bind("<Configure>", self._on_configure)
        self.canvas.bind("<MouseWheel>", self._on_wheel)
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Motion>", self._on_motion)
        self.canvas.bind("<Leave>", self._on_leave)
        self._bind_lens_keys()
        self.bind("<Configure>", self._track_geometry, add="+")
        # Modal via grab_set (below), NOT transient(): transient() strips the
        # Maximize box on Windows (tool-window style), and grab_set alone already
        # makes the window application-modal.
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.show_videos(source, upscaled)

    def _grab_modal(self):
        try:
            if self.winfo_exists():
                self.grab_set()                    # application-modal (bug #3)
        except tk.TclError:
            pass

    def save_geometry(self):
        if self._app is not None and self.winfo_exists():
            try:
                zoomed = (self.state() == "zoomed")
            except tk.TclError:
                zoomed = False
            self._app.settings["video_compare_geometry"] = self._last_normal_geo or self.geometry()
            self._app.settings["video_compare_zoomed"] = zoomed
            save_settings(self._app.settings)

    # ── public API (shared single instance) ──────────────────────────────────

    def show_videos(self, source, upscaled):
        """Point the window at a new (source, upscaled) pair and reset the view."""
        import video_pipeline as vp
        self._src_path, self._up_path = source, upscaled
        try:
            si, ui = vp.probe(source), vp.probe(upscaled)
        except Exception:
            si = ui = None
        sdim = f"{si.width}×{si.height}" if si else "?"
        udim = f"{ui.width}×{ui.height}" if ui else "?"
        self._duration = max((si.duration if si else 0) or 0,
                             (ui.duration if ui else 0) or 0) or 0.01
        self._fps = float((ui.fps if ui else None) or (si.fps if si else None) or 30)
        self._old_lbl.configure(text=f"Original:  {os.path.basename(source)}  ·  {sdim}")
        self._new_lbl.configure(text=f"Upscaled:  {os.path.basename(upscaled)}  ·  {udim}")
        self.title(f"{APP_TITLE} — Compare video — {os.path.basename(source)}")
        self._zoom = 1.0
        self._wipe_frac = 0.5
        self._inited = False
        self._reset_lens_position()
        self.timeline.configure(to=max(0.01, self._duration))
        self.timeline.set(0.0)
        self.lift()
        self.focus_set()
        self._seek(0.0)
        self.after(80, self._grab_modal)

    # ── transport ─────────────────────────────────────────────────────────────

    @staticmethod
    def _fmt_t(t):
        t = max(0.0, t)
        m, s = divmod(t, 60)
        return f"{int(m)}:{s:06.3f}"

    def _update_time_label(self, t):
        self.time_var.set(f"{self._fmt_t(t)} / {self._fmt_t(self._duration)}")

    def _on_scrub(self, val):
        try:
            t = float(val)
        except (TypeError, ValueError):
            return
        self._update_time_label(t)
        # Debounce: decode only after the scrub settles (decoding is ~hundreds of
        # ms per frame and we decode two), so dragging stays smooth.
        if self._scrub_after is not None:
            self.after_cancel(self._scrub_after)
        self._scrub_after = self.after(160, lambda: self._seek(t))

    def _step(self, frames):
        t = max(0.0, min(self._duration, self._cur_t + frames / max(1e-6, self._fps)))
        self.timeline.set(t)
        self._seek(t)

    def _seek(self, t):
        """Decode the frame pair at time `t` off the UI thread, then render."""
        self._cur_t = t
        self._update_time_label(t)
        src, up = self._src_path, self._up_path
        if not src or not up:
            return
        self._seek_seq += 1
        seq = self._seek_seq

        def work():
            o = self._decode_frame(src, t)
            n = self._decode_frame(up, t)
            self.after(0, lambda: self._apply_frames(seq, o, n))

        threading.Thread(target=work, daemon=True).start()

    def _apply_frames(self, seq, old_img, new_img):
        if seq != self._seek_seq:        # a newer seek superseded this one
            return
        if new_img is not None:
            self._new = new_img
        if old_img is not None:
            self._old = old_img
        self._render()

    def _decode_frame(self, path, t):
        """One frame at time `t` via the bundled ffmpeg -> PIL (no new dependency).
        Returns None on any failure (the renderer shows the placeholder)."""
        import io
        import video_pipeline as vp
        from PIL import Image
        try:
            ffmpeg, _ = vp.find_ffmpeg()
        except Exception:
            return None
        args = [ffmpeg, "-hide_banner", "-loglevel", "error",
                "-ss", f"{max(0.0, t):.3f}", "-i", path, "-frames:v", "1",
                "-f", "image2pipe", "-vcodec", "png", "-"]
        try:
            cp = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                creationflags=CREATE_NO_WINDOW)
            if cp.returncode != 0 or not cp.stdout:
                return None
            return Image.open(io.BytesIO(cp.stdout)).convert("RGB")
        except Exception:
            return None

    def _close(self):
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.save_geometry()
        self.destroy()


# ─────────────────────────────────────────────
#  VIDEO PLAYBACK WINDOW  (side-by-side / A-B flip, libVLC — section 16.3)
# ─────────────────────────────────────────────

class VideoPlaybackWindow(tk.Toplevel):
    """Real-time side-by-side (and A/B-flip) playback of source vs upscaled WITH
    audio, via libVLC. Deliberately SEPARATE from the frame-wipe
    VideoComparisonWindow so the two comparison styles never swap layouts mid-use,
    and so ALL libVLC embedding lives in this one window (the frame window is
    libVLC-free, hence crash-free). Audio is routed to the UPSCALED player ONLY
    (both carry the same muxed track): the user hears one reference stream and can
    watch each video track its sync against it. Aligns by TIMESTAMP (a CFR-normalize
    can change the upscaled frame count, section 14)."""

    def __init__(self, master, source, upscaled, app=None):
        super().__init__(master)
        self._app = app
        geo = app.settings.get("video_playback_geometry") if app is not None else None
        self.geometry(geo if (geo and _geometry_on_screen(self, geo)) else "1100x520")
        self.minsize(640, 400)
        self._last_normal_geo = None
        if app is not None and app.settings.get("video_playback_zoomed"):
            try:
                self.state("zoomed")               # restore maximised (like the main window)
            except tk.TclError:
                pass
        # Modal via grab_set (in _grab_modal), NOT transient(): transient() strips
        # the Maximize box on Windows; grab_set alone makes the window app-modal.

        bar = ttk.Frame(self, padding=(8, 4))
        bar.pack(fill="x")
        self._old_lbl = ttk.Label(bar, foreground="#aab2bf")
        self._old_lbl.pack(side="left")
        self._new_lbl = ttk.Label(bar, foreground="#aab2bf")
        self._new_lbl.pack(side="right")

        self._players_frame = tk.Frame(self, background="#15181d")
        self._players_frame.pack(fill="both", expand=True)
        self._src_player = self._up_player = None

        tl = ttk.Frame(self, padding=(8, 4))
        tl.pack(side="bottom", fill="x")
        self._play_btn = ttk.Button(tl, text="▶ Play", width=9, command=self._toggle_play)
        self._play_btn.pack(side="left", padx=(0, 8))
        ttk.Button(tl, text="◀ frame", width=8,
                   command=lambda: self._step(-1)).pack(side="left")
        ttk.Button(tl, text="frame ▶", width=8,
                   command=lambda: self._step(1)).pack(side="left", padx=(4, 8))
        self.time_var = tk.StringVar(value="0:00.000 / 0:00.000")
        ttk.Label(tl, textvariable=self.time_var, width=20,
                  font=("Consolas", 9)).pack(side="left")
        self._view_btn = ttk.Button(tl, text="Side-by-side", width=13,
                                    command=self._cycle_view)
        self._view_btn.pack(side="right")
        self.timeline = ttk.Scale(tl, from_=0.0, to=1.0, orient="horizontal",
                                  command=self._on_slider)
        self.timeline.pack(side="left", fill="x", expand=True, padx=(8, 0))

        self._src_path = self._up_path = None
        self._fps = 30.0
        self._duration = 0.0
        self._cur_t = 0.0
        self._playing = False
        self._view_mode = "sbs"          # sbs | a (source only) | b (upscaled only)
        self._resync_job = None
        self._ui_updating = False
        self._started = False            # players created + primed

        self.bind("<Configure>", self._track_geometry, add="+")
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.show_videos(source, upscaled)

    def _track_geometry(self, event):
        if event.widget is self:
            try:
                if self.state() == "normal":
                    self._last_normal_geo = self.geometry()
            except tk.TclError:
                pass

    def save_geometry(self):
        if self._app is not None and self.winfo_exists():
            try:
                zoomed = (self.state() == "zoomed")
            except tk.TclError:
                zoomed = False
            self._app.settings["video_playback_geometry"] = \
                self._last_normal_geo or self.geometry()
            self._app.settings["video_playback_zoomed"] = zoomed
            save_settings(self._app.settings)

    # ── public API (shared single instance) ──────────────────────────────────

    def show_videos(self, source, upscaled):
        import video_pipeline as vp
        self._teardown_players_keep_window()       # a new pair replaces the old
        self._src_path, self._up_path = source, upscaled
        try:
            si, ui = vp.probe(source), vp.probe(upscaled)
        except Exception:
            si = ui = None
        sdim = f"{si.width}×{si.height}" if si else "?"
        udim = f"{ui.width}×{ui.height}" if ui else "?"
        self._duration = max((si.duration if si else 0) or 0,
                             (ui.duration if ui else 0) or 0) or 0.01
        self._fps = float((ui.fps if ui else None) or (si.fps if si else None) or 30)
        self._old_lbl.configure(text=f"Original:  {os.path.basename(source)}  ·  {sdim}")
        self._new_lbl.configure(text=f"Upscaled:  {os.path.basename(upscaled)}  ·  {udim}")
        self.title(f"{APP_TITLE} — Play video — {os.path.basename(source)}")
        self.timeline.configure(to=max(0.01, self._duration))
        self._set_timeline(0.0)
        self._cur_t = 0.0
        self._playing = False
        self._play_btn.configure(text="▶ Play")
        self._update_time_label(0.0)
        self.lift()
        self.focus_set()
        self.after(60, self._ensure_started)       # realise the window, then prime
        self.after(80, self._grab_modal)

    def _grab_modal(self):
        try:
            if self.winfo_exists():
                self.grab_set()                    # application-modal (bug #3)
        except tk.TclError:
            pass

    def _ensure_started(self):
        """Create + prime the two players (needs libVLC; offer the in-app install if
        it is missing). Idempotent. Returns True when players are ready."""
        if self._started:
            return True
        if not (self._src_path and self._up_path):
            return False
        if not load_vlc():
            prompt_install_libvlc(
                self, on_done=lambda ok: self._ensure_started() if ok else None)
            return False
        # The source player is created SILENT (--no-audio); only the upscaled side is
        # heard, so there is never an echo regardless of mute/volume timing.
        self._src_player = VideoPlayer(self._players_frame, fps=self._fps, no_audio=True)
        # The upscaled player is the clock; its end drives the finish handler.
        self._up_player = VideoPlayer(self._players_frame, on_end=self._on_ended,
                                      fps=self._fps)
        ok = self._src_player.load(self._src_path) and self._up_player.load(self._up_path)
        if not ok:
            from tkinter import messagebox
            messagebox.showwarning(
                APP_TITLE, "Video playback could not start (libVLC failed to "
                           "initialise).")
            self._teardown_players_keep_window()
            return False
        self._started = True
        self._apply_view()
        return True

    # ── transport ────────────────────────────────────────────────────────────

    def _toggle_play(self):
        if self._playing:
            self._pause()
            return
        if not self._ensure_started():
            return
        self._playing = True
        self._play_btn.configure(text="⏸ Pause")
        self._src_player.play()
        self._up_player.play()
        # libVLC creates the audio output only once playback truly starts, and the
        # exact moment varies by host/aout, so a single set-mute right after play()
        # can land BEFORE the output exists and be silently ignored (no sound). Apply
        # the routing several times as it comes up.
        for delay in (150, 400, 900, 1500):
            self.after(delay, self._apply_audio)
        self._schedule_resync()

    def _pause(self):
        self._cancel_resync()
        for p in (self._up_player, self._src_player):
            if p is not None:
                p.pause()
        self._playing = False
        self._play_btn.configure(text="▶ Play")

    def _step(self, frames):
        if not self._ensure_started():
            return
        if self._playing:
            self._pause()
        for p in (self._up_player, self._src_player):
            if p is not None:
                p.step(frames)
        if self._up_player is not None:
            self._cur_t = self._up_player.get_time()
        self._set_timeline(self._cur_t)
        self._update_time_label(self._cur_t)

    def _on_slider(self, val):
        if self._ui_updating:
            return                                 # programmatic set from resync
        try:
            t = float(val)
        except (TypeError, ValueError):
            return
        self._cur_t = t
        self._update_time_label(t)
        for p in (self._up_player, self._src_player):
            if p is not None:
                p.seek(t)

    def _cycle_view(self):
        self._view_mode = {"sbs": "a", "a": "b", "b": "sbs"}[self._view_mode]
        self._view_btn.configure(text={"sbs": "Side-by-side", "a": "Original only",
                                       "b": "Upscaled only"}[self._view_mode])
        self._apply_view()

    def _apply_view(self):
        if not (self._src_player and self._up_player):
            return
        for p in (self._src_player, self._up_player):
            p.pack_forget()
        if self._view_mode == "a":
            self._src_player.pack(fill="both", expand=True)
        elif self._view_mode == "b":
            self._up_player.pack(fill="both", expand=True)
        else:
            self._src_player.pack(side="left", fill="both", expand=True)
            self._up_player.pack(side="right", fill="both", expand=True)
        self._apply_audio()

    def _apply_audio(self):
        """Audio = the UPSCALED player only. The source player is created SILENT
        (--no-audio at the instance level, in _ensure_started), so there is no echo
        and nothing to mute here. This just makes sure the upscaled side is audible.
        Re-applied after play() because libVLC creates the output only once playback
        starts (and the moment varies by host)."""
        if not self.winfo_exists():
            return
        if self._up_player is not None:
            # Set an audible volume AND unmute (unmute last so it sticks): the default
            # volume of a fresh player is not guaranteed to be 100, so unmuting alone
            # could leave it silent.
            self._up_player.set_volume(100)
            self._up_player.set_mute(False)

    # ── resync (upscaled is the clock, timestamp-aligned) ─────────────────────

    def _schedule_resync(self):
        self._resync_job = self.after(300, self._resync)

    def _cancel_resync(self):
        if self._resync_job is not None:
            try:
                self.after_cancel(self._resync_job)
            except Exception:
                pass
            self._resync_job = None

    def _resync(self):
        self._resync_job = None
        if not self._playing or self._up_player is None:
            return
        # Master (upscaled) reached the end -> finish now (belt: on_end also fires
        # from the player's poll a beat later).
        if self._up_player.is_ended():
            self._on_ended()
            return
        t = self._up_player.get_time()
        # Only nudge the source while it is STILL PLAYING. Once the source has ended
        # (it can be shorter than the upscaled), seeking it back to the master's time
        # made libVLC replay its last few frames (bug #2) — leave it frozen instead.
        if self._src_player is not None and not self._src_player.is_ended():
            if abs(self._src_player.get_time() - t) > 2.0 / max(1e-6, self._fps):
                self._src_player.seek(t)
        self._cur_t = t
        self._set_timeline(t)
        self._update_time_label(t)
        self._schedule_resync()

    def _on_ended(self):
        """Playback reached the end. libVLC leaves the players in the Ended state,
        from which play()/seek() silently do nothing (bug #1). Re-prime both to a
        paused frame-0 state so every control works again; the user presses Play to
        rewatch. Idempotent (a second trigger sees the players no longer ended)."""
        if self._up_player is None or not self._up_player.is_ended():
            # Already handled (restart moved it out of Ended); nothing to do.
            if not self._playing:
                return
        self._cancel_resync()
        self._playing = False
        self._play_btn.configure(text="▶ Play")
        for p in (self._up_player, self._src_player):
            if p is not None:
                p.restart()
        self._cur_t = 0.0
        self._set_timeline(0.0)
        self._update_time_label(0.0)
        self.after(300, self._apply_audio)          # re-mute source after the reload

    # ── helpers / teardown ────────────────────────────────────────────────────

    @staticmethod
    def _fmt_t(t):
        t = max(0.0, t)
        m, s = divmod(t, 60)
        return f"{int(m)}:{s:06.3f}"

    def _update_time_label(self, t):
        self.time_var.set(f"{self._fmt_t(t)} / {self._fmt_t(self._duration)}")

    def _set_timeline(self, t):
        self._ui_updating = True
        try:
            self.timeline.set(t)
        finally:
            self._ui_updating = False

    def _teardown_players_keep_window(self):
        self._cancel_resync()
        self._playing = False
        for p in (self._src_player, self._up_player):
            if p is not None:
                try:
                    p.pause()                      # quiet the vout before stop()
                except Exception:                  # noqa: BLE001
                    pass
                p.close()                          # stop -> detach HWND -> release
        self._src_player = self._up_player = None
        self._started = False

    def teardown_players(self):
        """For App exit: release players before root.destroy cascades into their
        surfaces (else the vout thread faults on the destroyed HWND)."""
        self._teardown_players_keep_window()

    def _finish_destroy(self):
        try:
            self.destroy()
        except tk.TclError:
            pass

    def _close(self):
        # Release the players OFF the UI thread. stop() on a STILL-PLAYING embedded
        # player blocks its caller until the vout thread unwinds, and that vout thread
        # is itself blocked SendMessage-ing our HWND -- which only THIS (UI) thread's
        # message pump can service. Doing it synchronously here deadlocks the app
        # ("Not responding"). So: cancel the Tk poll on the UI thread, withdraw the
        # window (but keep its HWND alive for the vout), release libVLC on a worker
        # thread while the pump keeps running, then destroy. See VideoPlayer.release_native.
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.save_geometry()
        self._cancel_resync()
        self._playing = False
        players = [p for p in (self._src_player, self._up_player) if p is not None]
        for p in players:
            try:
                p.stop_poll()                      # Tk call: must be on the UI thread
            except Exception:                      # noqa: BLE001
                pass
        self._src_player = self._up_player = None
        self._started = False
        try:
            self.withdraw()                        # hide now; HWND stays for the vout
        except tk.TclError:
            pass
        if not players:
            self.after(60, self.destroy)
            return

        def _work():
            for p in players:
                try:
                    p.release_native()
                except Exception:                  # noqa: BLE001
                    pass
            try:
                self.after(0, self._finish_destroy)
            except Exception:                      # noqa: BLE001
                pass

        threading.Thread(target=_work, daemon=True).start()
