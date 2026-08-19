"""
gui/tab_stabilize.py
--------------------
The Video Stabilization tab (future-features #20, workflow #23).

Still the plainest of the tool tabs, and still by design: this is a sibling of
Conciliation (local ffmpeg file work), not of the three GPU tabs. There is no
"Run on" row, no GPU picker, no funds readout and no benchmark - a folder of
videos, an output folder, two knobs and Start.

#23 added the workflow around #20's foundation:

  * A FOLDER LOADER + A QUEUE (items 2+3). This does NOT reverse #20's "not a
    batch tool" decision: that decision is about the ALGORITHM being whole-file
    (vidstab measures camera motion across the entire clip, so smoothing it per
    segment jolts at every boundary), and a queue of N independent whole-file
    jobs preserves it exactly. Nobody should later "simplify" the queue into
    segmenting one video.

  * "ALREADY STABILISED" IS THE RESUME STATE. DerivedPruner (#16) is not enough
    here, because a result defaults to sitting BESIDE its source rather than in a
    folder of ours, so a second scan would re-offer every result as fresh input.
    The list checks for an existing result per source (recorded pair first, then
    the canonical name), exactly as the Batch Upscaler reports "already
    upscaled" - which is also why a separate output folder is the recommended
    setup, and why the tab says so.

  * COMPARISON (item 4) is playback-first. At the shipped defaults the result has
    the same dimensions and frame count as the source, so the pair is 1:1 and
    timestamp-aligned - the easiest pair the comparison code has ever been
    handed. Steadiness is a temporal artifact, so only MOTION answers "is it
    steadier"; a paused pair mostly shows that the frame has moved. The still
    wipe stays available as the secondary view, where the lens (#14) is the
    useful part for inspecting what crop=keep did at the borders.

Drives video_stabilize.py as a subprocess, reusing ToolTab's stdin/marker
plumbing exactly as ConciliateTab does (and dropping the same film-strip
assumptions, since there are no per-image thumbnails here).
"""

import os
import json
import tempfile
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import mqtt_publisher
from gui.common import APP_TITLE, CFG, get_default_folder, set_default_folder
from gui.widgets import Tooltip, ProgressBar, TelemetryRow
from gui.tooltab import ToolTab
from gui.comparison import VideoComparisonWindow, VideoPlaybackWindow

# Containers offered in the file picker. Matches video_stabilize.VIDEO_EXTS; the
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

# Row states. Start acts on QUEUED and FAILED, which is what makes a second run of
# the same folder a resume rather than a repeat.
ST_QUEUED = "Queued"
ST_DONE_BEFORE = "Stabilised"       # a result was already there when we looked
ST_RUNNING = "Working…"
ST_DONE = "Done"
ST_FAILED = "Failed"

# What Start will pick up. A FAILED video belongs here: a stabilise fails for
# environmental reasons (an encoder this machine cannot actually run, a locked file,
# an unreadable source), so the user fixes the cause and wants to try again - and
# with FAILED excluded there was no way to, short of removing the row and adding it
# back. It also produced a state that READS AS A HUNG APP: after a one-video run
# failed, the list had nothing runnable so Start greyed out, and Stop was already
# grey because the run was over (docs/known-defects.md D3).
ST_RUNNABLE = (ST_QUEUED, ST_FAILED)


class StabilizeTab(ToolTab):
    """Stabilise shaky videos into new files. Sources are never modified."""

    def __init__(self, notebook, app):
        super().__init__(notebook, app)
        self.tool_name      = "Video Stabilization"
        self.mqtt_task_name = "stabilizing"
        self.telemetry_interval_ms = 30000       # sample every 30 s while running
        self.folder_var = tk.StringVar()
        self.out_var    = tk.StringVar()
        self.smooth_var = tk.IntVar(value=SMOOTHING_DEFAULT)
        self.zoom_var   = tk.BooleanVar(value=False)
        self._rows      = {}                     # iid -> {source, output, status, reason}
        self._result    = None                   # last DONE summary dict
        self._refused   = ""                     # REFUSED reason, if the run was refused
        self._queue_file = None                  # temp JSON handed to the runner
        self._cur_file  = None                   # (index, total, name) of the running video
        self._cur_pass  = ""
        self._scanning  = False
        self._prune_line = ""                    # DerivedPruner's one line for this scan
        self._out_job   = None                   # debounce for the output-folder trace
        self._build()

        self.restore_defaults_if_empty()
        self.folder_var.trace_add("write", lambda *_: self._refresh_buttons())
        self.out_var.trace_add("write", lambda *_: self._on_output_folder_changed())
        self._refresh_buttons()

    def restore_defaults_if_empty(self):
        """Pre-fill both folder fields from Settings.

        #20 pre-filled neither, and that was right when the tab took ONE FILE: no
        pinned folder can name a file. Now that the source is a FOLDER the two
        defaults can act where they were always meant to. They follow the rules the
        other tabs use: a source folder must EXIST (a vanished photo/video source is
        meaningless), while the output folder is taken as-is, since the tool CREATES
        it on the first run and the useful suggestion is normally a folder that is
        not there yet."""
        if not self.folder_var.get().strip():
            src = get_default_folder("stabilize_source")
            if src and os.path.isdir(src):
                self.folder_var.set(src)
        if not self.out_var.get().strip():
            out = get_default_folder("stabilize_output")
            if out:
                self.out_var.set(out)

    # ── construction ──────────────────────────────────────────────────────────

    def _build(self):
        self.columnconfigure(1, weight=1)
        W = Tooltip.WRAP_NARROW

        # ── Source folder ────────────────────────────────────────────────────
        ttk.Label(self, text="Videos in folder:").grid(row=0, column=0, sticky="w", pady=3)
        ttk.Entry(self, textvariable=self.folder_var).grid(row=0, column=1, sticky="ew",
                                                           padx=6, pady=3)
        self.browse_src_btn = ttk.Button(self, text="Browse…", command=self._pick_folder)
        self.browse_src_btn.grid(row=0, column=2, pady=3)
        self.save_src_btn = ttk.Button(self, text="Save as Default",
                                       command=lambda: self._save_default("source"))
        self.save_src_btn.grid(row=0, column=3, sticky="ew", padx=(8, 0), pady=3)
        Tooltip(self.browse_src_btn,
                "Pick a folder of shaky videos. Everything below it is listed, and "
                "the videos are only read, never changed: each result is written as "
                "a new file.", wraplength=W)
        Tooltip(self.save_src_btn,
                "Remember this folder as the default, pre-filled every time the app "
                "starts.", wraplength=W)

        # ── Output folder ────────────────────────────────────────────────────
        ttk.Label(self, text="Save results to:").grid(row=1, column=0, sticky="w", pady=3)
        ttk.Entry(self, textvariable=self.out_var).grid(row=1, column=1, sticky="ew",
                                                        padx=6, pady=3)
        self.browse_out_btn = ttk.Button(self, text="Browse…", command=self._pick_output)
        self.browse_out_btn.grid(row=1, column=2, pady=3)
        self.save_out_btn = ttk.Button(self, text="Save as Default",
                                       command=lambda: self._save_default("output"))
        self.save_out_btn.grid(row=1, column=3, sticky="ew", padx=(8, 0), pady=3)
        Tooltip(self.browse_out_btn,
                "Where every steadied copy is written, as '<name>_stabilized.mp4'. "
                "Leave it empty to write each result next to its own video.\n"
                "A separate folder is the tidier choice: it keeps results out of the "
                "list the next time you scan, and it makes them easy to move on to "
                "the Video Upscaler.", wraplength=W)
        Tooltip(self.save_out_btn,
                "Remember this folder as the default, pre-filled every time the app "
                "starts.", wraplength=W)

        # ── The list ─────────────────────────────────────────────────────────
        lf = ttk.LabelFrame(self, text=" Videos ", padding=6)
        lf.grid(row=2, column=0, columnspan=4, sticky="nsew", pady=(10, 0))
        lf.columnconfigure(0, weight=1)
        lf.rowconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)

        cols = ("name", "folder", "result", "status")
        self.tree = ttk.Treeview(lf, columns=cols, show="headings", height=8,
                                 selectmode="extended")
        for key, text, width, anchor in (
                ("name",   "Video",  240, "w"),
                ("folder", "Folder", 240, "w"),
                ("result", "Result", 200, "w"),
                ("status", "Status",  90, "w")):
            self.tree.heading(key, text=text)
            self.tree.column(key, width=width, anchor=anchor,
                             stretch=(key in ("name", "folder", "result")))
        self.tree.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(lf, orient="vertical", command=self.tree.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.bind("<Double-1>", self._on_row_double)
        self.tree.bind("<Button-3>", self._on_row_right)
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._refresh_buttons())
        Tooltip(self.tree,
                "Every video found, and what will happen to it. 'Queued' will be "
                "stabilised; 'Stabilised' already has a result and is left alone.\n"
                "Double-click a finished pair to watch the original and the steadied "
                "copy side by side; right-click for more.", wraplength=W)

        lb = ttk.Frame(lf)
        lb.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        self.scan_btn   = ttk.Button(lb, text="Scan folder", command=self._scan)
        self.addfile_btn = ttk.Button(lb, text="Add video…", command=self._add_file)
        self.remove_btn = ttk.Button(lb, text="Remove", command=self._remove_selected)
        self.clear_btn  = ttk.Button(lb, text="Clear", command=self._clear_list)
        self.compare_btn = ttk.Button(lb, text="Compare…", command=self._compare_selected)
        for b in (self.scan_btn, self.addfile_btn, self.remove_btn, self.clear_btn,
                  self.compare_btn):
            b.pack(side="left", padx=(0, 6))
        Tooltip(self.scan_btn,
                "List every video in the folder above, including subfolders. Videos "
                "that already have a result are listed as 'Stabilised' and skipped, "
                "so running the same folder again just picks up where you left off.",
                wraplength=W)
        Tooltip(self.addfile_btn,
                "Add one video by hand, from anywhere. Useful for a single clip, or "
                "for one you sent over from the Video Upscaler.", wraplength=W)
        Tooltip(self.remove_btn, "Take the selected videos out of this list. Nothing "
                                 "on disk is touched.", wraplength=W)
        Tooltip(self.clear_btn, "Empty the list. Nothing on disk is touched.",
                wraplength=W)
        Tooltip(self.compare_btn,
                "Watch the selected video and its steadied copy side by side, with "
                "sound, so you can judge whether it is actually steadier. Available "
                "once a result exists.", wraplength=W)

        # ── Options ──────────────────────────────────────────────────────────
        opt = ttk.LabelFrame(self, text=" Options ", padding=6)
        opt.grid(row=3, column=0, columnspan=4, sticky="ew", pady=(10, 0))

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
        btns.grid(row=4, column=0, columnspan=4, sticky="w", pady=(10, 0))
        self.start_btn = ttk.Button(btns, text="Start", command=self._start)
        self.stop_btn  = ttk.Button(btns, text="Stop", command=self._stop, state="disabled")
        self.open_btn  = ttk.Button(btns, text="Open output folder", command=self._open_out)
        self.viewlog_btn = ttk.Button(btns, text="View log", command=self._view_log,
                                      state="disabled")
        for b in (self.start_btn, self.stop_btn, self.open_btn, self.viewlog_btn):
            b.pack(side="left", padx=(0, 6))
        Tooltip(self.start_btn,
                "Steady every queued video, one after another. Each runs in two "
                "passes: the first measures how the camera moved, the second writes "
                "the steadied copy. Expect each to take a little under the length of "
                "the video itself.", wraplength=W)
        Tooltip(self.stop_btn,
                "Stop now. The video being worked on is abandoned and its "
                "half-written result discarded; videos already finished are kept, and "
                "the rest simply stay queued for next time.", wraplength=W)
        Tooltip(self.open_btn,
                "Open the folder the results are being written to, in Windows "
                "Explorer.", wraplength=W)
        Tooltip(self.viewlog_btn,
                "Open the log window with the full output of this run. Available once "
                "one has started.", wraplength=W)

        # ── Status + progress ────────────────────────────────────────────────
        sf = ttk.Frame(self)
        sf.grid(row=5, column=0, columnspan=4, sticky="ew", pady=(10, 2))
        sf.columnconfigure(0, weight=1)
        self.status_lbl = ttk.Label(sf, text="Choose a folder and press Scan folder.",
                                    anchor="w")
        self.status_lbl.grid(row=0, column=0, sticky="ew")
        self.progress = ProgressBar(sf, width=200)
        self.progress.grid(row=0, column=1, sticky="e", padx=(8, 0))
        self.progress.grid_remove()

        self.telemetry_row = TelemetryRow(self)
        self.telemetry_row.grid(row=6, column=0, columnspan=4, sticky="ew", pady=(4, 0))

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
            # While a video is being worked on the tab OWNS the status line: it
            # composes it from the FILE + PASS events, which is the only way the
            # line can say WHICH of fifty videos the pass belongs to (the runner's
            # own per-pass line cannot know it is one of fifty). Outside that window
            # - the preflight, a refusal, the closing summary - the runner's message
            # is the whole story and is shown verbatim.
            if self._cur_file is None:
                self.status_lbl.configure(text=payload)
                if self.running:
                    self.app.mqtt_publish({mqtt_publisher.TASK_DETAILS_TOPIC: payload})
        elif kind == "FILE":
            try:
                d = json.loads(payload)
            except ValueError:
                return
            self._cur_file = (d.get("index", 1), d.get("total", 1),
                              d.get("name", ""))
            self._cur_pass = ""
            self._set_row_status(d.get("source", ""), ST_RUNNING)
            self._render_running_status()
        elif kind == "PASS":
            try:
                d = json.loads(payload)
            except ValueError:
                return
            self._cur_pass = (f"pass {d.get('pass', '?')} of {d.get('of', 2)} - "
                              f"{d.get('name', '')}")
            self._render_running_status()
        elif kind == "RESULT":
            try:
                d = json.loads(payload)
            except ValueError:
                return
            self._set_row_status(d.get("source", ""),
                                 ST_DONE if d.get("ok") else ST_FAILED,
                                 output=d.get("output"), reason=d.get("reason"))
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
            # Hand the status line back to the runner for its closing summary.
            self._cur_file = None
            self.app.flash_attention()
        else:
            super()._handle_event(kind, payload)

    def _render_running_status(self):
        if not self._cur_file:
            return
        idx, total, name = self._cur_file
        prefix = f"[{idx}/{total}] " if total > 1 else ""
        text = f"{prefix}{name}" + (f" - {self._cur_pass} …" if self._cur_pass else " …")
        self.status_lbl.configure(text=text)
        if self.running:
            self.app.mqtt_publish({mqtt_publisher.TASK_DETAILS_TOPIC: text})

    def _handle_stabilize_eta(self, payload):
        """'elapsed|done|done|total', all in pass-frames across the WHOLE run (both
        passes of every video). Publishes the run's MQTT task state; the tab itself
        shows progress on the bar, so there is no on-screen ETA field to feed."""
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

    # ── the list ──────────────────────────────────────────────────────────────

    @staticmethod
    def existing_result(src, out_folder):
        """The steadied copy of `src` that is already on disk, or None.

        Recorded pair FIRST, canonical name second. The record survives a renamed
        result (and knows what was actually produced); the name rule is what makes
        the answer retroactive - it works on a tree stabilised by another install,
        or after db/cache.db was deleted, which is the same reasoning that made #22
        derive its pairs from the output tree instead of the database.

        Static and given the folder explicitly so the folder scan can call it from
        its worker thread (reading a tk variable off the UI thread is not safe)."""
        import video_stabilize as vs
        try:
            import db
            recorded = db.stabilized_output(db.get_conn(), src)
            if recorded:
                return recorded
        except Exception:                            # noqa: BLE001
            pass
        canonical = vs.canonical_output_path(src, out_folder or None)
        return canonical if os.path.isfile(canonical) else None

    def _existing_result(self, src):
        return self.existing_result(src, self.out_var.get().strip())

    def _planned_output(self, src, taken):
        """Where this source's result WOULD go, avoiding both the files on disk and
        the names already claimed by other rows in the list (two videos with the same
        stem in different folders both want one name in a shared output folder)."""
        import video_stabilize as vs
        out = vs.suggest_output_path(src, folder=self.out_var.get().strip() or None,
                                     taken=taken)
        taken.add(os.path.normcase(out))
        return out

    def _add_rows(self, sources, announce=True, resolved=None):
        """Add sources to the list, skipping ones already there. Returns how many
        were added. `resolved` is an optional src -> existing-result map the folder
        scan precomputed off the UI thread."""
        have = {os.path.normcase(r["source"]) for r in self._rows.values()}
        taken = {os.path.normcase(r["output"]) for r in self._rows.values()}
        added = 0
        for src in sources:
            src = os.path.abspath(src)
            if os.path.normcase(src) in have:
                continue
            have.add(os.path.normcase(src))
            existing = (resolved.get(src) if resolved is not None
                        else self._existing_result(src))
            if existing:
                out, status = existing, ST_DONE_BEFORE
            else:
                out, status = self._planned_output(src, taken), ST_QUEUED
            iid = self.tree.insert("", "end", values=(
                os.path.basename(src), os.path.dirname(src),
                os.path.basename(out), status))
            self._rows[iid] = {"source": src, "output": out, "status": status,
                               "reason": ""}
            added += 1
        if announce:
            self._set_summary_status()
        self._refresh_buttons()
        return added

    def add_sources(self, paths, select_tab=False):
        """Public hand-off (#23 item 1): add videos from elsewhere in the app.

        The Video Upscaler calls this with a SOURCE video, never one of its outputs:
        #20's documented ordering is stabilise BEFORE upscaling, so the crop happens
        at source resolution and the box-fit target still fills the finished framing.
        Stabilising an upscaled file silently inverts that, and the result looks fine
        until someone compares framing."""
        added = self._add_rows(paths)
        if select_tab:
            try:
                self.app.nb.select(self)
            except Exception:                        # noqa: BLE001
                pass
        if added:
            for iid, row in self._rows.items():
                if os.path.normcase(row["source"]) == os.path.normcase(
                        os.path.abspath(paths[-1])):
                    self.tree.selection_set(iid)
                    self.tree.see(iid)
                    break
        return added

    def _scan(self):
        """(Re)load the list from the source folder, off the UI thread."""
        folder = self.folder_var.get().strip()
        if not os.path.isdir(folder):
            messagebox.showwarning(APP_TITLE, "That folder does not exist:\n" + folder)
            return
        self._scanning = True
        self._refresh_buttons()
        self.status_lbl.configure(text="Looking for videos …")
        # Captured HERE, on the UI thread: a tk variable must not be read from a
        # worker, and the answer to "does this already have a result" depends on it.
        out_folder = self.out_var.get().strip()

        def work():
            import video_stabilize as vs
            try:
                found, pruner = vs.iter_videos(folder, recursive=True, cfg=CFG)
                line = pruner.summary()
            except Exception as exc:                 # noqa: BLE001
                found, line = [], f"Could not read that folder: {exc}"
            # Resolved off the UI thread too: one DB lookup plus one stat per video
            # is nothing each and a visible freeze across a few hundred of them on a
            # network share.
            resolved = {os.path.abspath(p): self.existing_result(p, out_folder)
                        for p in found}
            self.after(0, lambda: self._scan_done(found, line, resolved))

        threading.Thread(target=work, daemon=True).start()

    def _scan_done(self, found, prune_line, resolved):
        self._scanning = False
        self._clear_list(announce=False)
        self._add_rows(found, announce=False, resolved=resolved)
        # The pruner's one line goes on the STATUS, not into the run console: the log
        # window is unreachable until a run has started, and this is exactly the moment
        # a user wonders why the count is smaller than the folder looks (#16's rule is
        # that a scan explains itself once). REMEMBERED rather than written once,
        # because the summary is rewritten by anything that re-counts the list (adding
        # a video, or the output folder being re-planned), and the explanation belongs
        # to these rows for as long as they are the ones on screen.
        self._prune_line = prune_line or ""
        self._set_summary_status(empty_text="No videos found in that folder.")
        self._refresh_buttons()

    def _set_summary_status(self, empty_text="Choose a folder and press Scan folder."):
        extra = self._prune_line
        if not self._rows:
            self.status_lbl.configure(
                text=(empty_text + (f" ({extra})" if extra else "")))
            return
        queued = sum(1 for r in self._rows.values() if r["status"] == ST_QUEUED)
        already = sum(1 for r in self._rows.values() if r["status"] == ST_DONE_BEFORE)
        bits = [f"{queued} to stabilise"]
        if already:
            bits.append(f"{already} already stabilised")
        self.status_lbl.configure(
            text=", ".join(bits) + "." + (f" {extra}" if extra else ""))

    def _set_row_status(self, source, status, output=None, reason=None):
        if not source:
            return
        key = os.path.normcase(os.path.abspath(source))
        for iid, row in self._rows.items():
            if os.path.normcase(row["source"]) != key:
                continue
            row["status"] = status
            if output:
                row["output"] = os.path.abspath(output)
            if reason is not None:
                row["reason"] = reason
            self.tree.set(iid, "status", status)
            self.tree.set(iid, "result", os.path.basename(row["output"]))
            self.tree.see(iid)
            return

    def _on_output_folder_changed(self):
        """The planned result of every not-yet-run row follows the output folder, and
        so does whether it counts as already stabilised. Leaving that stale would
        show results in a folder the run will not use.

        Debounced, because this fires per KEYSTROKE while the path is being typed and
        each pass costs a stat per row."""
        self._refresh_buttons()
        if self._out_job is not None:
            try:
                self.after_cancel(self._out_job)
            except Exception:                        # noqa: BLE001
                pass
        self._out_job = self.after(400, self._replan_pending)

    def _replan_pending(self):
        """Re-derive the planned result of every row that has not run yet. Rows this
        run already produced (or failed) keep their real outcome: their output is a
        fact about a file on disk, not a plan."""
        self._out_job = None
        if self.running or not self._rows or not self.winfo_exists():
            return
        pending = [iid for iid, r in self._rows.items()
                   if r["status"] in (ST_QUEUED, ST_DONE_BEFORE)]
        if not pending:
            return
        sources = [self._rows[iid]["source"] for iid in pending]
        for iid in pending:
            self.tree.delete(iid)
            del self._rows[iid]
        self._add_rows(sources)

    def _remove_selected(self):
        for iid in self.tree.selection():
            self._rows.pop(iid, None)
            self.tree.delete(iid)
        self._set_summary_status()
        self._refresh_buttons()

    def _clear_list(self, announce=True):
        for iid in list(self._rows):
            self.tree.delete(iid)
        self._rows.clear()
        if announce:
            self._prune_line = ""
            self.status_lbl.configure(text="Choose a folder and press Scan folder.")
        self._refresh_buttons()

    def _add_file(self):
        start = get_default_folder("stabilize_source") or ""
        paths = filedialog.askopenfilenames(
            title="Choose video(s) to stabilise", filetypes=VIDEO_PATTERNS,
            initialdir=start if os.path.isdir(start) else None)
        if paths:
            self._add_rows([os.path.normpath(p) for p in paths])

    def _selected_rows(self):
        rows = [self._rows[i] for i in self.tree.selection() if i in self._rows]
        if not rows and len(self._rows) == 1:
            rows = list(self._rows.values())
        return rows

    def _requeue_selected(self):
        """Stabilise these again, into a NEW file. Never over the previous result:
        the user may still be judging it, and a second attempt with a different
        Steadiness is exactly why one would re-run at all."""
        rows = [(i, self._rows[i]) for i in self.tree.selection() if i in self._rows]
        taken = {os.path.normcase(r["output"]) for r in self._rows.values()}
        for iid, row in rows:
            try:
                import db
                db.forget_stabilized(db.get_conn(), row["source"])
            except Exception:                        # noqa: BLE001
                pass
            row["output"] = self._planned_output(row["source"], taken)
            row["status"] = ST_QUEUED
            row["reason"] = ""
            self.tree.set(iid, "result", os.path.basename(row["output"]))
            self.tree.set(iid, "status", ST_QUEUED)
        self._set_summary_status()
        self._refresh_buttons()

    # ── comparison (#23 item 4) ───────────────────────────────────────────────

    def _pair(self, row):
        """(source, result) when both files are on disk, else None."""
        if not row:
            return None
        src, out = row.get("source"), row.get("output")
        if src and out and os.path.isfile(src) and os.path.isfile(out):
            return src, out
        return None

    def _compare_selected(self):
        rows = self._selected_rows()
        pair = self._pair(rows[0]) if rows else None
        if pair:
            self._open_playback(*pair)

    def _open_playback(self, src, out):
        """Side by side, in motion, with sound - the DEFAULT comparison here.

        Steadiness is a temporal artifact: only motion answers "is it steadier". A
        paused pair mostly shows that the frame has moved, which reads as a
        difference rather than as an improvement."""
        win = getattr(self.app, "video_playback_window", None)
        if win is not None and win.winfo_exists():
            win.show_videos(src, out)
            win.deiconify()
            win.lift()
        else:
            self.app.video_playback_window = VideoPlaybackWindow(
                self.app, src, out, app=self.app)

    def _open_compare(self, src, out):
        """The frozen before/after wipe - the SECONDARY view, and it answers a
        different question: what crop=keep did at the EDGES. Those border pixels are
        filled in from earlier frames, which is the documented cost of choosing
        coverage over steadiness, and the lens (#14) is the useful part for peering
        at one."""
        win = getattr(self.app, "video_comparison_window", None)
        if win is not None and win.winfo_exists():
            win.show_videos(src, out)
            win.deiconify()
            win.lift()
        else:
            self.app.video_comparison_window = VideoComparisonWindow(
                self.app, src, out, app=self.app)

    def _on_row_double(self, event):
        iid = self.tree.identify_row(event.y)
        row = self._rows.get(iid)
        pair = self._pair(row)
        if pair:
            self._open_playback(*pair)
        elif row:
            self._open_path(row["source"])

    def _on_row_right(self, event):
        iid = self.tree.identify_row(event.y)
        if not iid:
            return
        if iid not in self.tree.selection():
            self.tree.selection_set(iid)
        row = self._rows.get(iid)
        if not row:
            return
        pair = self._pair(row)
        m = tk.Menu(self, tearoff=0)
        if pair:
            m.add_command(label="Compare (play side by side)",
                          command=lambda: self._open_playback(*pair))
            m.add_command(label="Compare frames (wipe + lens)",
                          command=lambda: self._open_compare(*pair))
            m.add_separator()
        m.add_command(label="Open original video",
                      command=lambda: self._open_path(row["source"]))
        m.add_command(label="Open original folder",
                      command=lambda: self._open_folder(row["source"]))
        if pair:
            m.add_command(label="Open steadied video",
                          command=lambda: self._open_path(row["output"]))
            m.add_command(label="Open steadied folder",
                          command=lambda: self._open_folder(row["output"]))
        m.add_separator()
        if row["status"] in (ST_DONE_BEFORE, ST_DONE, ST_FAILED):
            m.add_command(label="Stabilise again (new file)",
                          command=self._requeue_selected,
                          state="disabled" if self.running else "normal")
        if row.get("reason"):
            m.add_command(label="Show reason",
                          command=lambda: messagebox.showinfo(APP_TITLE, row["reason"]))
        m.add_command(label="Remove from list", command=self._remove_selected,
                      state="disabled" if self.running else "normal")
        try:
            m.tk_popup(event.x_root, event.y_root)
        finally:
            m.grab_release()

    def _open_path(self, p):
        try:
            os.startfile(p)
        except Exception as exc:                     # noqa: BLE001
            messagebox.showerror(APP_TITLE, f"Could not open:\n{p}\n{exc}")

    def _open_folder(self, p):
        import subprocess
        try:
            subprocess.Popen(["explorer", "/select,", os.path.normpath(p)])
        except Exception:                            # noqa: BLE001
            self._open_path(os.path.dirname(p))

    # ── helpers ───────────────────────────────────────────────────────────────

    def _refresh_buttons(self):
        queued = sum(1 for r in self._rows.values() if r["status"] in ST_RUNNABLE)
        busy = self.running or self._scanning
        if not busy:
            self.start_btn.configure(state="normal" if queued else "disabled")
        for b in (self.scan_btn, self.addfile_btn, self.remove_btn, self.clear_btn,
                  self.browse_src_btn, self.browse_out_btn):
            b.configure(state="disabled" if busy else "normal")
        self.save_src_btn.configure(
            state="disabled" if busy
            else ("normal" if os.path.isdir(self.folder_var.get().strip() or "")
                  else "disabled"))
        self.save_out_btn.configure(
            state="disabled" if busy
            else ("normal" if self.out_var.get().strip() else "disabled"))
        rows = self._selected_rows()
        self.compare_btn.configure(
            state="normal" if (rows and self._pair(rows[0])) else "disabled")
        self.open_btn.configure(state="normal" if self._output_root() else "disabled")

    def _save_default(self, which):
        if which == "source":
            folder = self.folder_var.get().strip()
            if not os.path.isdir(folder):
                return
            set_default_folder("stabilize_source", folder)
            self._flash_saved(self.save_src_btn)
        else:
            folder = self.out_var.get().strip()
            if not folder:
                return
            set_default_folder("stabilize_output", folder)
            self._flash_saved(self.save_out_btn)
        self.app.sync_settings_defaults()             # mirror into the Settings tab

    def _flash_saved(self, btn):
        btn.configure(text="Saved ✓")
        self.after(1200, lambda: btn.configure(text="Save as Default"))

    def _pick_folder(self):
        start = self.folder_var.get().strip() or get_default_folder("stabilize_source")
        path = filedialog.askdirectory(
            title="Choose the folder holding the videos to stabilise",
            initialdir=start if os.path.isdir(start or "") else None)
        if path:
            self.folder_var.set(os.path.normpath(path))
            self._scan()

    def _pick_output(self):
        start = self.out_var.get().strip() or get_default_folder("stabilize_output")
        path = filedialog.askdirectory(
            title="Choose where to save the steadied videos",
            initialdir=start if os.path.isdir(start or "") else None)
        if path:
            self.out_var.set(os.path.normpath(path))

    def _output_root(self):
        """The folder Open output folder should show: the configured one, else the
        folder of whatever result the list is pointing at."""
        out = self.out_var.get().strip()
        if out:
            return os.path.abspath(out)
        rows = self._selected_rows() or list(self._rows.values())
        if rows:
            return os.path.dirname(os.path.abspath(rows[0]["output"]))
        return ""

    def _open_out(self):
        folder = self._output_root()
        if folder and os.path.isdir(folder):
            os.startfile(folder)
        else:
            messagebox.showinfo(APP_TITLE, "That folder does not exist yet.")

    # ── actions ───────────────────────────────────────────────────────────────

    def _start(self):
        runnable = [(iid, r) for iid, r in self._rows.items()
                    if r["status"] in ST_RUNNABLE]
        jobs = [{"source": r["source"], "output": r["output"]}
                for _iid, r in runnable]
        if not jobs:
            messagebox.showwarning(APP_TITLE, "Nothing is queued. Scan a folder or "
                                              "add a video first.")
            return
        missing = [j["source"] for j in jobs if not os.path.isfile(j["source"])]
        if missing:
            messagebox.showwarning(
                APP_TITLE, "These videos are no longer there:\n\n"
                + "\n".join(os.path.basename(m) for m in missing[:10]))
            return
        out_root = self.out_var.get().strip()
        if out_root and not os.path.isdir(out_root):
            if not messagebox.askyesno(
                    APP_TITLE, f"The output folder does not exist:\n{out_root}\n\n"
                               "Create it?"):
                return
            try:
                os.makedirs(out_root, exist_ok=True)
            except OSError as exc:
                messagebox.showerror(APP_TITLE, f"Could not create it:\n{exc}")
                return

        # The queue goes over in a FILE, not on the command line: a folder of a few
        # hundred videos would blow past Windows' command-line length limit, and the
        # GUI is the authority on each output name (it has already shown them here).
        try:
            fd, path = tempfile.mkstemp(prefix="imgtbx_stabq_", suffix=".json")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"jobs": jobs}, fh)
        except OSError as exc:
            messagebox.showerror(APP_TITLE, f"Could not prepare the queue:\n{exc}")
            return
        self._discard_queue_file()
        self._queue_file = path
        # A retried video stops showing "Failed" the moment it is queued again,
        # rather than carrying last run's verdict until its turn comes round.
        for iid, row in runnable:
            if row["status"] != ST_QUEUED:
                row["status"] = ST_QUEUED
                self.tree.set(iid, "status", ST_QUEUED)

        args = ["--queue", path,
                "--smoothing", str(int(self.smooth_var.get() or SMOOTHING_DEFAULT)),
                "--optzoom", "1" if self.zoom_var.get() else "0"]
        self.progress.set(0)
        self.progress.grid_remove()
        self._result = None
        self._refused = ""
        self._cur_file = None
        self._cur_pass = ""
        self._reset_stream_state()
        self.status_lbl.configure(text="Starting …")
        if self.launch("video_stabilize.py", args):
            self._set_running(True)
        else:
            self._discard_queue_file()

    def _discard_queue_file(self):
        if self._queue_file and os.path.exists(self._queue_file):
            try:
                os.remove(self._queue_file)
            except OSError:
                pass
        self._queue_file = None

    def _stop(self):
        self.send("q")
        self.stop_btn.configure(state="disabled")
        self.status_lbl.configure(text="Stopping …")

    def _set_running(self, running):
        self.stop_btn.configure(state="normal" if running else "disabled")
        if running:
            self.start_btn.configure(state="disabled")
        for w in (self.smooth_spin, self.zoom_chk):
            w.configure(state="disabled" if running else "normal")
        self._refresh_buttons()
        self.app.refresh_tab_exclusivity()

    def on_exit(self, code):
        self._set_running(False)
        self._discard_queue_file()
        self._cur_file = None
        # A video the run was in the middle of goes back to Queued, not left showing
        # "Working…" forever. It genuinely IS queued again: a stabilise has no resume,
        # so the abandoned `.part` was discarded and the next run starts it from the
        # beginning. Leaving it as-is would also make Start refuse ("nothing is
        # queued") on the very file the user stopped to come back to.
        for iid, row in self._rows.items():
            if row["status"] == ST_RUNNING:
                row["status"] = ST_QUEUED
                self.tree.set(iid, "status", ST_QUEUED)
        r = self._result or {}
        if self._refused:
            # Shown as a dialog, not just a status line: it is several sentences of
            # explanation plus what to do about it, and the run did nothing.
            self.progress.grid_remove()
            first = self._refused.splitlines()[0]
            self.status_lbl.configure(text=first)
            messagebox.showwarning(APP_TITLE, self._refused)
        elif r.get("processed") or r.get("failed"):
            done, failed = r.get("processed", 0), r.get("failed", 0)
            stopped = bool(r.get("stopped_by_user"))
            # The bar is only honest while a run is in flight. A run that ended with
            # any failure used to keep whatever fraction it had reached (50% for a
            # single video that died in pass 2), which is the single thing that made
            # a finished run look like a hung one.
            if done and not failed and not stopped:
                self.progress.set(100)
            else:
                self.progress.grid_remove()
            bits = [f"{done} stabilised"]
            if failed:
                bits.append(f"{failed} failed")
            left = sum(1 for row in self._rows.values() if row["status"] == ST_QUEUED)
            if stopped and left:
                bits.append(f"{left} still queued")
            self.status_lbl.configure(
                text=("Stopped - " if stopped else "Done - ") + ", ".join(bits) + ".")
        elif r.get("stopped_by_user"):
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
