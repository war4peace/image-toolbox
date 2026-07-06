"""
gui/video_segment_picker.py
---------------------------
The segment extractor's picker window (section 16.4). Right-clicking a scanned
video -> "Extract segment…" opens it: play/scrub the source (with audio when
libVLC is available), mark a frame-accurate start and end, optionally label the
scene, add it, define several scenes, then Queue them all as virtual clip jobs.

Playback: uses the shared `VideoPlayer` (libVLC). When libVLC is absent the window
degrades gracefully (section 16.2) to a **silent ffmpeg frame-scrub** for marking,
with an "Open in player" button for the audio cue; marks stay frame-accurate either
way. The window owns no pipeline logic: Queue hands the pending clips back to the
Video tab via the `on_queue` callback, which calls `batch_video_upscale.prepare_clip`
off the UI thread.
"""

import os
import threading
import subprocess
import tkinter as tk
from tkinter import ttk, messagebox

from gui.common import APP_TITLE, CREATE_NO_WINDOW
from gui.video_player import (VideoPlayer, VLC_AVAILABLE, load_vlc,
                              nearest_keyframe, time_to_frame)


def validate_marks(start, end, duration):
    """Pure check for a clip's marks. Returns (ok, message). Kept dependency-free
    and unit-tested; the window just surfaces the message."""
    if start is None or end is None:
        return False, "Mark both a start and an end first."
    if end <= start:
        return False, "The end mark must come after the start mark."
    if start < -1e-6:
        return False, "The start mark is before the beginning."
    if duration and end > duration + 0.5:
        return False, "The end mark is past the end of the video."
    return True, ""


def _fmt_t(t):
    t = max(0.0, float(t or 0))
    m, s = divmod(t, 60)
    return f"{int(m)}:{s:06.3f}"


class VideoSegmentPicker(tk.Toplevel):
    def __init__(self, master, app, src_abs, rel, targets, on_queue):
        super().__init__(master)
        self.app = app
        self.src_abs = src_abs
        self.rel = rel
        self._targets = list(targets or [])
        self._on_queue = on_queue
        self._fps = 30.0
        self._duration = 0.0
        self._keyframes = []
        self._t = 0.0
        self._start = None
        self._end = None
        self._pending = []            # [{start, end, label, target}]
        self._ui_updating = False
        self._scrub_after = None
        self._decode_seq = 0

        self.title(f"{APP_TITLE} — Extract segment — {os.path.basename(rel)}")
        geo = app.settings.get("segment_picker_geometry") if app else None
        from gui.common import _geometry_on_screen  # local: avoid import cycle at load
        self.geometry(geo if (geo and _geometry_on_screen(self, geo)) else "900x620")
        self.minsize(640, 460)
        self.protocol("WM_DELETE_WINDOW", self._close)

        self._build()
        self.after(50, self._probe)

    # ── layout ───────────────────────────────────────────────────────────────

    def _build(self):
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        top = ttk.Frame(self, padding=(10, 8))
        top.grid(row=0, column=0, sticky="ew")
        ttk.Label(top, text=self.rel, foreground="#aab2bf").pack(side="left")
        self.status_var = tk.StringVar(value="Loading video …")
        ttk.Label(top, textvariable=self.status_var, foreground="#7f8a99").pack(side="right")

        # Video surface: the libVLC player, or a fallback scrub canvas.
        stage = ttk.Frame(self, padding=(10, 0))
        stage.grid(row=1, column=0, sticky="nsew")
        stage.columnconfigure(0, weight=1)
        stage.rowconfigure(0, weight=1)
        self._use_vlc = load_vlc()
        if self._use_vlc:
            self.player = VideoPlayer(stage, on_time=self._on_player_time, fps=self._fps)
            self.player.grid(row=0, column=0, sticky="nsew")
            self._canvas = None
        else:
            self.player = None
            self._canvas = tk.Canvas(stage, background="#15181d", highlightthickness=0)
            self._canvas.grid(row=0, column=0, sticky="nsew")
            self._canvas.bind("<Configure>", lambda _e: self._render_fallback())
            self._fallback_img = None
            self._photo = None

        # Transport.
        tr = ttk.Frame(self, padding=(10, 6))
        tr.grid(row=2, column=0, sticky="ew")
        self._play_btn = ttk.Button(tr, text="▶ Play", width=9, command=self._toggle_play)
        self._play_btn.pack(side="left")
        ttk.Button(tr, text="|◀◀ kf", width=7,
                   command=lambda: self._keyframe(-1)).pack(side="left", padx=(8, 0))
        ttk.Button(tr, text="◀ frame", width=8,
                   command=lambda: self._step(-1)).pack(side="left", padx=(4, 0))
        ttk.Button(tr, text="frame ▶", width=8,
                   command=lambda: self._step(1)).pack(side="left", padx=(4, 0))
        ttk.Button(tr, text="kf ▶▶|", width=7,
                   command=lambda: self._keyframe(1)).pack(side="left", padx=(0, 8))
        self.time_var = tk.StringVar(value="0:00.000 / 0:00.000")
        ttk.Label(tr, textvariable=self.time_var, width=22,
                  font=("Consolas", 9)).pack(side="left")
        if not self._use_vlc:
            ttk.Button(tr, text="Open in player ▶", command=self._open_external).pack(
                side="right")
        self.timeline = ttk.Scale(self, from_=0.0, to=1.0, orient="horizontal",
                                  command=self._on_slider)
        self.timeline.grid(row=3, column=0, sticky="ew", padx=10)

        # Marks + label + target + add.
        mk = ttk.Frame(self, padding=(10, 6))
        mk.grid(row=4, column=0, sticky="ew")
        ttk.Button(mk, text="Mark start", command=self._mark_start).grid(row=0, column=0)
        ttk.Button(mk, text="Mark end", command=self._mark_end).grid(row=0, column=1, padx=(6, 12))
        self.marks_var = tk.StringVar(value="start —   end —   (—)")
        ttk.Label(mk, textvariable=self.marks_var, font=("Consolas", 9)).grid(
            row=0, column=2, sticky="w")
        mk.columnconfigure(2, weight=1)
        ttk.Label(mk, text="Label:").grid(row=0, column=3, padx=(8, 2))
        self.label_var = tk.StringVar()
        ttk.Entry(mk, textvariable=self.label_var, width=16).grid(row=0, column=4)
        ttk.Label(mk, text="Target:").grid(row=0, column=5, padx=(8, 2))
        self.target_var = tk.StringVar(value=self._targets[0] if self._targets else "")
        self.target_combo = ttk.Combobox(mk, textvariable=self.target_var, state="readonly",
                                         width=7, values=self._targets)
        self.target_combo.grid(row=0, column=6)
        ttk.Button(mk, text="Add segment", command=self._add_segment).grid(
            row=0, column=7, padx=(10, 0))

        # Pending clips + queue.
        pf = ttk.LabelFrame(self, text=" Segments to queue ", padding=4)
        pf.grid(row=5, column=0, sticky="nsew", padx=10, pady=(4, 0))
        self.rowconfigure(5, weight=1)
        pf.rowconfigure(0, weight=1)
        pf.columnconfigure(0, weight=1)
        self.tree = ttk.Treeview(pf, columns=("range", "dur", "target"),
                                 show="tree headings", height=4)
        self.tree.heading("#0", text="Label")
        self.tree.column("#0", width=200)
        for c, txt, w in (("range", "Range", 170), ("dur", "Duration", 90),
                          ("target", "Target", 70)):
            self.tree.heading(c, text=txt)
            self.tree.column(c, width=w, anchor="w")
        self.tree.grid(row=0, column=0, sticky="nsew")
        ttk.Button(pf, text="Remove", command=self._remove_pending).grid(
            row=0, column=1, sticky="n", padx=(4, 0))

        bar = ttk.Frame(self, padding=(10, 8))
        bar.grid(row=6, column=0, sticky="ew")
        self.queue_btn = ttk.Button(bar, text="Queue", command=self._queue, state="disabled")
        self.queue_btn.pack(side="right")
        ttk.Button(bar, text="Close", command=self._close).pack(side="right", padx=(0, 6))

    # ── probe (off the UI thread) ────────────────────────────────────────────

    def _probe(self):
        def work():
            import video_pipeline as vp
            try:
                info = vp.probe(self.src_abs)
                kf = vp.keyframe_times(self.src_abs)
            except Exception as exc:                 # noqa: BLE001
                self.after(0, lambda e=exc: self.status_var.set(f"Cannot read video: {e}"))
                return
            self.after(0, lambda: self._probed(info, kf))
        threading.Thread(target=work, daemon=True).start()

    def _probed(self, info, keyframes):
        self._fps = float(info.fps) or 30.0
        self._duration = info.duration or 0.0
        self._keyframes = keyframes or []
        self.timeline.configure(to=max(0.01, self._duration))
        if self.player is not None:
            self.player.fps = self._fps
            self.player.load(self.src_abs)
        self.status_var.set(
            "Ready." if self._use_vlc else
            "In-app playback needs libVLC (not installed) — scrub to mark; "
            "use 'Open in player' for audio.")
        self._update_time_ui(0.0)
        if not self._use_vlc:
            self._request_fallback_frame(0.0)

    # ── transport ────────────────────────────────────────────────────────────

    def _toggle_play(self):
        if self.player is None:
            return
        self.player.toggle()
        self._play_btn.configure(text="⏸ Pause" if self.player.is_playing() else "▶ Play")

    def _step(self, frames):
        if self.player is not None:
            self.player.step(frames)
        else:
            self._seek(self._t + frames / self._fps)

    def _keyframe(self, direction):
        kf = nearest_keyframe(self._keyframes, self._t, direction)
        if kf is not None:
            self._seek(kf)

    def _seek(self, t):
        t = max(0.0, min(self._duration or t, t))
        if self.player is not None:
            self.player.seek(t)
        else:
            self._t = t
            self._update_time_ui(t)
            self._request_fallback_frame(t)

    def _on_player_time(self, t):
        # libVLC playhead update: refresh readouts + slider (guarded against the
        # slider's own command firing back).
        self._t = t
        self._update_time_ui(t)

    def _on_slider(self, val):
        if self._ui_updating:
            return
        try:
            t = float(val)
        except (TypeError, ValueError):
            return
        self._t = t
        self._update_time_ui(t)
        if self.player is not None:
            self.player.seek(t)
        else:
            # Debounce the (expensive) ffmpeg decode until the drag settles.
            if self._scrub_after is not None:
                self.after_cancel(self._scrub_after)
            self._scrub_after = self.after(140, lambda: self._request_fallback_frame(t))

    def _update_time_ui(self, t):
        self._ui_updating = True
        try:
            self.timeline.set(t)
        finally:
            self._ui_updating = False
        frame = time_to_frame(t, self._fps)
        self.time_var.set(f"{_fmt_t(t)} / {_fmt_t(self._duration)}  (f{frame})")

    # ── fallback frame decode (no libVLC) ────────────────────────────────────

    def _request_fallback_frame(self, t):
        self._decode_seq += 1
        seq = self._decode_seq

        def work():
            img = self._decode_frame(t)
            self.after(0, lambda: self._apply_fallback(seq, img))
        threading.Thread(target=work, daemon=True).start()

    def _decode_frame(self, t):
        import io
        import video_pipeline as vp
        from PIL import Image
        try:
            ffmpeg, _ = vp.find_ffmpeg()
        except Exception:
            return None
        args = [ffmpeg, "-hide_banner", "-loglevel", "error",
                "-ss", f"{max(0.0, t):.3f}", "-i", self.src_abs, "-frames:v", "1",
                "-f", "image2pipe", "-vcodec", "png", "-"]
        try:
            cp = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                creationflags=CREATE_NO_WINDOW)
            if cp.returncode != 0 or not cp.stdout:
                return None
            return Image.open(io.BytesIO(cp.stdout)).convert("RGB")
        except Exception:                            # noqa: BLE001
            return None

    def _apply_fallback(self, seq, img):
        if seq != self._decode_seq or img is None or self._canvas is None:
            return
        self._fallback_img = img
        self._render_fallback()

    def _render_fallback(self):
        if self._canvas is None or self._fallback_img is None:
            return
        from PIL import Image, ImageTk
        cw = max(1, self._canvas.winfo_width())
        ch = max(1, self._canvas.winfo_height())
        img = self._fallback_img
        scale = min(cw / img.width, ch / img.height)
        w, h = max(1, int(img.width * scale)), max(1, int(img.height * scale))
        disp = img.resize((w, h), Image.LANCZOS)
        self._photo = ImageTk.PhotoImage(disp)
        self._canvas.delete("all")
        self._canvas.create_image(cw // 2, ch // 2, image=self._photo)

    def _open_external(self):
        try:
            os.startfile(self.src_abs)               # audio cue in the default player
        except Exception as exc:                     # noqa: BLE001
            messagebox.showerror(APP_TITLE, f"Could not open:\n{exc}")

    # ── marks ────────────────────────────────────────────────────────────────

    def _mark_start(self):
        self._start = self._t
        if self._end is not None and self._end <= self._start:
            self._end = None
        self._refresh_marks()

    def _mark_end(self):
        self._end = self._t
        self._refresh_marks()

    def _refresh_marks(self):
        s = _fmt_t(self._start) if self._start is not None else "—"
        e = _fmt_t(self._end) if self._end is not None else "—"
        dur = (f"{self._end - self._start:.3f}s"
               if (self._start is not None and self._end is not None
                   and self._end > self._start) else "—")
        self.marks_var.set(f"start {s}   end {e}   ({dur})")

    def _add_segment(self):
        ok, msg = validate_marks(self._start, self._end, self._duration)
        if not ok:
            messagebox.showinfo(APP_TITLE, msg)
            return
        if not self.target_var.get():
            messagebox.showinfo(APP_TITLE, "This video has no eligible upscale target.")
            return
        clip = {"start": float(self._start), "end": float(self._end),
                "label": self.label_var.get().strip(), "target": self.target_var.get()}
        self._pending.append(clip)
        label = clip["label"] or "(unlabelled)"
        self.tree.insert("", "end", text=label,
                         values=(f"{_fmt_t(clip['start'])} – {_fmt_t(clip['end'])}",
                                 f"{clip['end'] - clip['start']:.1f}s", clip["target"]))
        # Reset marks + label for the next scene; keep the target.
        self._start = self._end = None
        self.label_var.set("")
        self._refresh_marks()
        self.queue_btn.configure(state="normal")

    def _remove_pending(self):
        sel = self.tree.selection()
        if not sel:
            return
        idx = self.tree.index(sel[0])
        self.tree.delete(sel[0])
        if 0 <= idx < len(self._pending):
            del self._pending[idx]
        if not self._pending:
            self.queue_btn.configure(state="disabled")

    def _queue(self):
        if not self._pending:
            return
        clips = list(self._pending)
        if self._on_queue:
            self._on_queue(self.rel, clips)
        self._close()

    def _close(self):
        try:
            if self.app is not None and self.winfo_exists():
                self.app.settings["segment_picker_geometry"] = self.geometry()
                from gui.common import save_settings
                save_settings(self.app.settings)
        except Exception:                            # noqa: BLE001
            pass
        if self.player is not None:
            self.player.close()
        self.destroy()
