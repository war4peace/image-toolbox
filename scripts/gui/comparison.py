"""
gui/comparison.py
-----------------
The floating before/after comparison windows: ComparisonWindow (original vs.
upscaled, a shared vertical-wipe canvas with linked zoom/pan) and its subclass
VideoComparisonWindow. One shared instance each, geometry persisted via
gui.common.

Pillow (Image/ImageOps/ImageTk) is imported lazily inside the methods that decode
frames, so this module loads without PIL (the CI import smoke runs PIL-free).
"""

import os
import subprocess
import threading

import tkinter as tk
from tkinter import ttk

from gui.common import APP_TITLE, CREATE_NO_WINDOW, _geometry_on_screen, save_settings
from gui.video_player import VideoPlayer, load_vlc, prompt_install_libvlc


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

    Only the visible region of each side is decoded (Pillow ``resize`` with a
    float ``box``), so even 4K stays responsive; an interactive gesture renders
    with a fast filter and a crisp pass follows when it settles. Like the log
    window there is one shared instance, re-targeted on each double-click.
    """

    ZOOM_STEP = 1.25         # per wheel notch
    ABS_MAX   = 4.0          # max screen px per upscaled-image px (≈400%)
    HANDLE_TOL = 9           # px around the divider that grabs the wipe
    DIVIDER   = "#e8edf3"
    HANDLE_FILL = "#e8edf3"
    HANDLE_EDGE = "#202329"

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

        bar = ttk.Frame(self, padding=(8, 4))
        bar.pack(fill="x")
        self._old_lbl = ttk.Label(bar, foreground="#aab2bf")
        self._old_lbl.pack(side="left")
        self._new_lbl = ttk.Label(bar, foreground="#aab2bf")
        self._new_lbl.pack(side="right")

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
        # Track the last non-maximised geometry on the Toplevel itself, so a
        # close-while-maximised still records a sensible size + the zoomed flag.
        self.bind("<Configure>", self._track_geometry, add="+")

        self.protocol("WM_DELETE_WINDOW", self._close)
        self.show(source, output)

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

    def _on_wheel(self, event):
        if self._new is None:
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
        cw, _ = self._size()
        wipe_x = self._wipe_frac * cw
        if abs(event.x - wipe_x) <= self.HANDLE_TOL:
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

    def _on_release(self, _event):
        if self._drag is not None:
            self._drag = None
            self._render(fast=False)

    def _on_motion(self, event):
        if self._drag is not None or self._new is None:
            return
        cw, _ = self._size()
        near = abs(event.x - self._wipe_frac * cw) <= self.HANDLE_TOL
        self.canvas.configure(cursor="sb_h_double_arrow" if near else "fleur")

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

        wipe_x = min(float(cw), max(0.0, self._wipe_frac * cw))
        resample = Image.BILINEAR if fast else Image.LANCZOS
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
    off the UI thread. See docs/video-upscaler.md sections 11 / 15."""

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

        bar = ttk.Frame(self, padding=(8, 4))
        bar.pack(fill="x")
        self._old_lbl = ttk.Label(bar, foreground="#aab2bf")
        self._old_lbl.pack(side="left")
        self._new_lbl = ttk.Label(bar, foreground="#aab2bf")
        self._new_lbl.pack(side="right")

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
            self._app.settings["video_playback_geometry"] = \
                self._last_normal_geo or self.geometry()
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
        self._src_player = VideoPlayer(self._players_frame, fps=self._fps)
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
        self.after(300, self._apply_audio)         # audio output exists only once playing
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
        """Audio = the UPSCALED player only (the source is muted + zero-volume). Both
        carry the same muxed track, so this is the single reference stream; keeping it
        on the upscaled side lets the user check the upscaled video's sync. Re-applied
        after play() because libVLC creates the output only once playback starts."""
        if self._src_player is not None:
            self._src_player.set_mute(True)
            self._src_player.set_volume(0)
        if self._up_player is not None:
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
