"""
gui/tab_stabilize.py
--------------------
The Video Stabilization tab (future-features #20).

Deliberately the plainest tool tab in the app, and that is the design: this is a
sibling of Conciliation (local ffmpeg file work), not of the three GPU tabs. So
there is no "Run on" row, no GPU picker, no funds readout and no benchmark - just
one input file, one output file, two knobs and Start. It takes ONE video at a
time rather than a folder, because vidstab is a two-pass whole-file algorithm.

Drives video_stabilize.py as a subprocess, reusing ToolTab's stdin/marker
plumbing exactly as ConciliateTab does (and dropping the same film-strip
assumptions, since there are no per-image thumbnails here).
"""

import os
import json
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import mqtt_publisher
from gui.common import APP_TITLE, get_default_folder, set_default_folder
from gui.widgets import Tooltip, ProgressBar, TelemetryRow
from gui.tooltab import ToolTab

# Containers offered in the file pickers. Matches conciliate.VIDEO_EXTS; the
# stabilizer itself accepts whatever ffmpeg can open, so this only shapes the dialog.
VIDEO_PATTERNS = [
    ("Video files", "*.mp4 *.avi *.mov *.mkv *.m4v *.wmv *.mpg *.mpeg *.flv "
                    "*.webm *.3gp *.ts *.mts *.m2ts *.vob"),
    ("All files", "*.*"),
]

# Steadiness bounds. libvidstab accepts more, but past ~50 the correction fights the
# footage rather than smoothing it, and below 1 it does nothing.
SMOOTHING_MIN = 1
SMOOTHING_MAX = 60
SMOOTHING_DEFAULT = 10


class StabilizeTab(ToolTab):
    """Stabilise one shaky video into a new file. The source is never modified."""

    def __init__(self, notebook, app):
        super().__init__(notebook, app)
        self.tool_name      = "Video Stabilization"
        self.mqtt_task_name = "stabilizing"
        self.telemetry_interval_ms = 30000       # sample every 30 s while running
        self.src_var    = tk.StringVar()
        self.out_var    = tk.StringVar()
        self.smooth_var = tk.IntVar(value=SMOOTHING_DEFAULT)
        self.zoom_var   = tk.BooleanVar(value=False)
        self._result    = None                   # last DONE summary dict
        self._refused   = ""                     # REFUSED reason, if the run was refused
        self._last_src_dir = ""                  # where the picker last opened, this session
        self._build()

        self.restore_defaults_if_empty()
        self.src_var.trace_add("write", lambda *_: self._on_source_changed())
        self.out_var.trace_add("write", lambda *_: self._refresh_buttons())
        self._refresh_buttons()

    def restore_defaults_if_empty(self):
        # Neither field is pre-filled from a default, and that is deliberate: this tab
        # takes ONE FILE, so no pinned folder can name the source, and an output path
        # without a source to derive it from would be a guess. The two Settings
        # defaults act at the moment they can mean something instead -
        # `stabilize_source` opens the Browse dialog in the right place, and
        # `stabilize_output` decides where the suggested result is written once a
        # source exists (see _on_source_changed).
        pass

    # ── construction ──────────────────────────────────────────────────────────

    def _build(self):
        self.columnconfigure(1, weight=1)
        W = Tooltip.WRAP_NARROW

        ttk.Label(self, text="Video to stabilise:").grid(row=0, column=0, sticky="w", pady=3)
        ttk.Entry(self, textvariable=self.src_var).grid(row=0, column=1, sticky="ew",
                                                        padx=6, pady=3)
        browse_src = ttk.Button(self, text="Browse…", command=self._pick_source)
        browse_src.grid(row=0, column=2, pady=3)
        Tooltip(browse_src,
                "Pick the shaky video you want to steady. This file is only read, "
                "never changed: the result is written to the second box as a new "
                "file.", wraplength=W)

        ttk.Label(self, text="Save result as:").grid(row=1, column=0, sticky="w", pady=3)
        ttk.Entry(self, textvariable=self.out_var).grid(row=1, column=1, sticky="ew",
                                                        padx=6, pady=3)
        browse_out = ttk.Button(self, text="Browse…", command=self._pick_output)
        browse_out.grid(row=1, column=2, pady=3)
        Tooltip(browse_out,
                "Where to write the steadied copy. Filled in for you as "
                "'<name>_stabilized.mp4' next to the original once you choose a "
                "video; change it if you want it somewhere else.", wraplength=W)

        # ── Options ──────────────────────────────────────────────────────────
        opt = ttk.LabelFrame(self, text=" Options ", padding=6)
        opt.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(10, 0))

        row = ttk.Frame(opt)
        row.pack(fill="x")
        smooth_lbl = ttk.Label(row, text="Steadiness:")
        smooth_lbl.pack(side="left", padx=(0, 6))
        self.smooth_spin = ttk.Spinbox(row, from_=SMOOTHING_MIN, to=SMOOTHING_MAX,
                                       textvariable=self.smooth_var, width=6)
        self.smooth_spin.pack(side="left")
        smooth_tip = (f"How hard to smooth out the camera movement "
                      f"(recommended: {SMOOTHING_DEFAULT}).\n"
                      "Higher steadies more but pulls harder on the picture, so the "
                      "edges of the frame are filled in from earlier moments more "
                      "often. Lower follows the original camera movement more "
                      "closely. A deliberate pan or a slow zoom is not shake: if the "
                      "result looks like it is fighting the camera, lower this.")
        Tooltip(smooth_lbl, smooth_tip, wraplength=W)
        Tooltip(self.smooth_spin, smooth_tip, wraplength=W)

        self.zoom_chk = ttk.Checkbutton(
            row, text="Zoom in to hide the moving edges (loses picture)",
            variable=self.zoom_var)
        self.zoom_chk.pack(side="left", padx=(18, 0))
        # Leads with the consequence: this is the money/data-affecting control on this
        # tab, and it is the one ffmpeg turns on by default, so a user who has read a
        # tutorial elsewhere will expect it and needs to know what it costs here.
        Tooltip(self.zoom_chk,
                "Off is recommended, and is the opposite of what most ffmpeg guides "
                "do.\n"
                "OFF: the whole picture is kept. Where a corrected frame would show "
                "an empty border, it is filled in from earlier frames, so at worst an "
                "extreme edge looks slightly stale for a moment.\n"
                "ON: the video is zoomed in far enough that no border is ever "
                "visible, which permanently throws away the outer part of every "
                "frame. Measured on real camcorder footage that is about a fifth of "
                "the picture, and it is set by the single worst jolt in the whole "
                "video.", wraplength=W)

        hint = ttk.Label(opt,
                         text="Stabilise first, then upscale: the Video Upscaler's "
                              "target then applies to the finished framing.",
                         foreground="#666")
        hint.pack(anchor="w", pady=(6, 0))

        # ── Action buttons ───────────────────────────────────────────────────
        btns = ttk.Frame(self)
        btns.grid(row=3, column=0, columnspan=3, sticky="w", pady=(10, 0))
        self.start_btn = ttk.Button(btns, text="Start", command=self._start)
        self.stop_btn  = ttk.Button(btns, text="Stop", command=self._stop, state="disabled")
        self.open_btn  = ttk.Button(btns, text="Open output folder", command=self._open_out)
        self.viewlog_btn = ttk.Button(btns, text="View log", command=self._view_log,
                                      state="disabled")
        for b in (self.start_btn, self.stop_btn, self.open_btn, self.viewlog_btn):
            b.pack(side="left", padx=(0, 6))
        Tooltip(self.start_btn,
                "Steady the chosen video. It runs in two passes: the first measures "
                "how the camera moved, the second writes the steadied copy. Expect it "
                "to take a little under the length of the video itself.", wraplength=W)
        Tooltip(self.stop_btn,
                "Stop now. The half-written result is discarded, so there is nothing "
                "to clean up and nothing to resume: start again when you are ready.",
                wraplength=W)
        Tooltip(self.open_btn,
                "Open the folder the result is being written to, in Windows Explorer.",
                wraplength=W)
        Tooltip(self.viewlog_btn,
                "Open the log window with the full output of this run. Available once "
                "one has started.", wraplength=W)

        # ── Status + progress ────────────────────────────────────────────────
        sf = ttk.Frame(self)
        sf.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(10, 2))
        sf.columnconfigure(0, weight=1)
        self.status_lbl = ttk.Label(sf, text="Choose a video, then press Start.",
                                    anchor="w")
        self.status_lbl.grid(row=0, column=0, sticky="ew")
        self.progress = ProgressBar(sf, width=200)
        self.progress.grid(row=0, column=1, sticky="e", padx=(8, 0))
        self.progress.grid_remove()

        # Spacer row so the telemetry row sits at the bottom rather than floating
        # directly under the buttons on a tall window.
        ttk.Frame(self).grid(row=5, column=0, columnspan=3, sticky="nsew")
        self.rowconfigure(5, weight=1)

        self.telemetry_row = TelemetryRow(self)
        self.telemetry_row.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(4, 0))

    # ── overrides that drop ToolTab's thumbnail/strip assumptions ─────────────

    def _process_chunk(self, text):
        text = self._filter_markers(text)
        if text:
            self.console.feed(text)

    def _tick(self):
        pass

    def _reset_stream_state(self):
        self._at_line_start = True
        self._marker_buf    = None
        self._hold          = ""
        self.console.clear()

    # ── GUI events from video_stabilize.py ────────────────────────────────────

    def _handle_event(self, kind, payload):
        if kind == "STATUS":
            self.status_lbl.configure(text=payload)
            if self.running:
                self.app.mqtt_publish({mqtt_publisher.TASK_DETAILS_TOPIC: payload})
        elif kind == "PASS":
            try:
                d = json.loads(payload)
            except ValueError:
                return
            self.status_lbl.configure(
                text=f"Pass {d.get('pass', '?')} of {d.get('of', 2)} - "
                     f"{d.get('name', '')} …")
        elif kind == "PROG":
            cur, _, tot = payload.partition("|")
            try:
                cur, tot = int(cur), int(tot)
            except ValueError:
                return
            if tot > 0:
                self.progress.grid()
                self.progress.set(cur * 100 / tot)
                self.app.taskbar_progress(cur, tot)
                self.app.mqtt_publish({mqtt_publisher.TASK_PROGRESS_TOPIC: f"{cur}/{tot}"})
        elif kind == "ETA":
            # Handled HERE rather than by ToolTab._handle_eta, which writes to the
            # eta_var / cost-per-100 widgets that _build_output_area creates - and
            # this tab deliberately builds none of them (no images, no billed pod).
            # Falling through to super() would raise AttributeError mid-run.
            self._handle_stabilize_eta(payload)
        elif kind == "REFUSED":
            # Kept apart from a generic failure: a refusal means the run never
            # started and nothing was written, and it carries an explanation worth
            # showing in full rather than truncating onto the status line.
            try:
                self._refused = (json.loads(payload) or {}).get("reason", "")
            except ValueError:
                self._refused = ""
        elif kind == "DONE":
            self._last_done = payload       # MQTT last_run, published on exit
            try:
                self._result = json.loads(payload)
            except ValueError:
                self._result = None
            self.app.flash_attention()
        else:
            super()._handle_event(kind, payload)

    def _handle_stabilize_eta(self, payload):
        """'elapsed|done|done|total', all in whole-job frames (both passes). Publishes
        the run's MQTT task state; the tab itself shows progress on the bar, so there
        is no on-screen ETA field to feed."""
        parts = payload.split("|")
        try:
            elapsed = float(parts[0])
            done    = int(parts[1])
            total   = int(parts[3])
        except (ValueError, IndexError):
            return
        eta_txt = None
        if done > 0 and total > 0:
            remaining = max(0, total - done) * (elapsed / done)
            m, s = divmod(int(remaining), 60)
            h, m = divmod(m, 60)
            eta_txt = f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
        self.mqtt_task_update(progress=f"{done}/{total}" if total else None,
                              eta=eta_txt, runtime=int(elapsed))

    # ── helpers ───────────────────────────────────────────────────────────────

    def _on_source_changed(self):
        """Re-derive the output path EVERY time the source changes, not only when the
        field is empty.

        Only filling a blank field looked tidy and was a trap: after one stabilise the
        field still named the FIRST video, so choosing a second source left an output
        called after the first. The run would then offer to replace that file - a
        dialog inviting the user to destroy the result they just made, which is not the
        same thing as a safeguard.

        The FOLDER survives, though: only the filename is rebuilt. It is taken from the
        first rule that has one, the same shape Tag & Rename uses to pre-fill its folder:
        whatever is already in the field (the user's on-screen choice), then the pinned
        `stabilize_output` default from Settings, else beside the source. So saving
        results to one collecting folder keeps working, and the name always matches the
        video actually selected."""
        src = self.src_var.get().strip()
        if src and os.path.isfile(src):
            current = self.out_var.get().strip()
            folder = (os.path.dirname(os.path.abspath(current)) if current
                      else get_default_folder("stabilize_output").strip())
            self.out_var.set(self._suggest_output(src, folder or None))
        self._refresh_buttons()

    @staticmethod
    def _suggest_output(src, folder=None):
        """`<stem>_stabilized.mp4`, in `folder` (default: beside the source). Mirrors
        video_stabilize.suggest_output_path, kept here so the field can be filled in
        without importing the runner (which pulls in ffmpeg lookup and the notification
        stack) just to name a file.

        Never returns an existing file or the source itself: picking the same video
        twice yields `_stabilized_2`, so re-running cannot quietly eat the previous
        result."""
        src = os.path.abspath(src)
        folder = folder or os.path.dirname(src)
        stem = os.path.splitext(os.path.basename(src))[0]
        candidate = os.path.join(folder, f"{stem}_stabilized.mp4")
        n = 2
        while (os.path.exists(candidate)
               or os.path.normcase(candidate) == os.path.normcase(src)):
            candidate = os.path.join(folder, f"{stem}_stabilized_{n}.mp4")
            n += 1
        return candidate

    def _refresh_buttons(self):
        src = self.src_var.get().strip()
        out = self.out_var.get().strip()
        ready = bool(src) and os.path.isfile(src) and bool(out)
        if not self.running:
            self.start_btn.configure(state="normal" if ready else "disabled")
        self.open_btn.configure(state="normal" if out else "disabled")

    def _pick_source(self):
        # Start where the user keeps footage: the folder they last picked from this
        # session, else the pinned Settings default.
        start = self._last_src_dir or get_default_folder("stabilize_source") or ""
        path = filedialog.askopenfilename(title="Choose the video to stabilise",
                                          filetypes=VIDEO_PATTERNS,
                                          initialdir=start if os.path.isdir(start) else None)
        if path:
            self.src_var.set(os.path.normpath(path))
            # Remember the FOLDER for this session, not the file: re-offering one
            # specific clip would be noise, but reopening where the footage lives is
            # useful. Only Settings (or Save as Default) pins it across launches.
            self._last_src_dir = os.path.dirname(os.path.abspath(path))

    def _pick_output(self):
        src = self.src_var.get().strip()
        initial = self.out_var.get().strip() or (self._suggest_output(src) if src else "")
        path = filedialog.asksaveasfilename(
            title="Save the stabilised video as",
            defaultextension=".mp4", filetypes=VIDEO_PATTERNS,
            initialdir=os.path.dirname(initial) if initial else None,
            initialfile=os.path.basename(initial) if initial else None)
        if path:
            self.out_var.set(os.path.normpath(path))

    def _open_out(self):
        out = self.out_var.get().strip()
        folder = os.path.dirname(os.path.abspath(out)) if out else ""
        if folder and os.path.isdir(folder):
            os.startfile(folder)
        else:
            messagebox.showinfo(APP_TITLE, "That folder does not exist yet.")

    # ── actions ───────────────────────────────────────────────────────────────

    def _start(self):
        src = self.src_var.get().strip()
        out = self.out_var.get().strip()
        if not os.path.isfile(src):
            messagebox.showwarning(APP_TITLE, "Please choose a video to stabilise.")
            return
        if not out:
            messagebox.showwarning(APP_TITLE, "Please choose where to save the result.")
            return
        if os.path.normcase(os.path.abspath(src)) == os.path.normcase(os.path.abspath(out)):
            messagebox.showwarning(
                APP_TITLE, "The result must be saved as a different file from the "
                           "original, so your source video is never overwritten.")
            return
        if os.path.exists(out) and not messagebox.askyesno(
                APP_TITLE, f"{os.path.basename(out)} already exists.\n\nReplace it?"):
            return
        folder = os.path.dirname(os.path.abspath(out))
        if folder and not os.path.isdir(folder):
            messagebox.showwarning(APP_TITLE, f"That folder does not exist:\n{folder}")
            return

        args = [src, out,
                "--smoothing", str(int(self.smooth_var.get() or SMOOTHING_DEFAULT)),
                "--optzoom", "1" if self.zoom_var.get() else "0"]
        self.progress.set(0)
        self.progress.grid_remove()
        self._result = None
        self._refused = ""
        self._reset_stream_state()
        self.status_lbl.configure(text="Starting …")
        if self.launch("video_stabilize.py", args):
            self._set_running(True)

    def _stop(self):
        self.send("q")
        self.stop_btn.configure(state="disabled")
        self.status_lbl.configure(text="Stopping …")

    def _set_running(self, running):
        self.start_btn.configure(state="disabled" if running else "normal")
        self.stop_btn.configure(state="normal" if running else "disabled")
        for w in (self.smooth_spin, self.zoom_chk):
            w.configure(state="disabled" if running else "normal")
        self._refresh_buttons()
        self.app.refresh_tab_exclusivity()

    def on_exit(self, code):
        self._set_running(False)
        if self._refused:
            # Shown as a dialog, not just a status line: it is several sentences of
            # explanation plus what to do about it, and the run did nothing.
            self.progress.grid_remove()
            first = self._refused.splitlines()[0]
            self.status_lbl.configure(text=first)
            messagebox.showwarning(APP_TITLE, self._refused)
        elif self._result and self._result.get("output"):
            r = self._result
            self.progress.set(100)
            mb = (r.get("size_bytes") or 0) / (1024 * 1024)
            extra = " (deinterlaced)" if r.get("deinterlaced") else ""
            self.status_lbl.configure(
                text=f"Done - {r.get('frames', 0)} frames{extra} -> "
                     f"{os.path.basename(r['output'])} ({mb:.0f} MB)")
        elif self._result and self._result.get("stopped_by_user"):
            self.progress.grid_remove()
            self.status_lbl.configure(text="Stopped - nothing was written.")
        elif code == 0:
            self.status_lbl.configure(text="Finished.")
        else:
            self.status_lbl.configure(
                text=f"Stopped with an error (code {code}) - see the log.")
        self.app.mqtt_publish(
            {mqtt_publisher.TASK_DETAILS_TOPIC: self.status_lbl.cget("text")})
        super().on_exit(code)
