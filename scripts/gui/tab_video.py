"""
gui/tab_video.py
----------------
The Video Upscaler tab.
"""

import os
import json
import time
import queue
import codecs
import threading
import subprocess
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import runpod_client
import ssh_setup
import taskbar_progress
from gui.common import SCRIPT_DIR, APP_ROOT, APP_TITLE, CREATE_NO_WINDOW, GUI_MARKER, CFG, get_default_folder, set_default_folder, PYTHON_EXE, _geometry_on_screen, save_settings, get_install_mode
from gui.widgets import (ProgressBar, TelemetryRow, _log_hms, ConsoleBuffer, Tooltip,
                         use_window_button_style)
from gui.comparison import VideoComparisonWindow, VideoPlaybackWindow
from gui.tooltab import ToolTab


# Where the per-bar-change progress diagnostics go: "window" = the tab's log pane,
# "file" = logs/video_progress_debug.log only, "" / None = off. The bar is validated
# now, so keep these OUT of the user-facing log (file-only). The per-minute "Processing"
# heartbeat now comes from the runner (console + on-disk log), not from here.
_PROGRESS_DEBUG_MODE = "file"


def _video_bar_done(run_done, seg_frames, seg_done, last_fp_time, now, live_spf,
                    seg_start, seg_expected, total_chunks, has_frames):
    """Frames-done estimate for the Video tab's progress bar (the caller divides by the
    run total and enforces monotonicity). Two regimes:

    - The worker IS reporting real within-segment frames (`has_frames`): anchor to them
      (`seg_done`) and smooth FORWARD from the last report with the pod's measured live
      s/frame, but never past one chunk's worth (the next real anchor) or the segment
      end. This fills the long encode/decode phases where `seg_done` would otherwise sit
      frozen and then jump.
    - No frame report yet: use the time estimate, but CAP it at the first chunk's share
      (1/total_chunks). The first real anchor lands around the end of chunk 1, so a
      too-low estimate can no longer rush the bar to ~100 % and snap back when it arrives.
    """
    tc = max(1, int(total_chunks or 1))
    if has_frames:
        # The worker now TIME-fills within each chunk (see _time_fill_frames) and reports it
        # every status poll, so we just track its frame count. No local live-spf interpolation:
        # live_spf is dominated by the fast encode/upscale, so interpolating with it RUSHED the
        # bar through the slow decode and then froze. (last_fp_time / live_spf kept for the tail.)
        return run_done + min(seg_frames, seg_done)
    if seg_expected and seg_start and seg_expected > 0:
        # Before the first real anchor: crawl on the time estimate, but cap LOW (half a
        # chunk) — the first phase report lands early (chunk 1 encode), so a high cap would
        # overshoot it and the monotonic bar could then sit ahead of reality.
        frac = min(0.5 / tc, (now - seg_start) / seg_expected)
        return run_done + frac * seg_frames
    return run_done


def _clip_tc(seconds):
    """A compact clip timecode for a queue/manager label: 161.0 -> '2:41'."""
    s = max(0, int(round(seconds or 0)))
    return f"{s // 60}:{s % 60:02d}"


# The "Method / Model" selector's options (#11): (label, engine, model). The SeedVR2 half
# mirrors tab_settings._VIDEO_MODEL_OPTIONS (kept short here for the combobox); the
# Real-ESRGAN half comes from the shared esrgan_models catalog. It runs BOTH locally (#11)
# and remotely (#18 B: a volume-free esrgan pod), so it is offered in both modes. Keep in
# sync with tab_settings if a SeedVR2 model is added.
_SEEDVR2_METHODS = [
    ("SeedVR2 / 7B FP16",  "seedvr2_ema_7b_fp16.safetensors"),
    ("SeedVR2 / 7B Sharp", "seedvr2_ema_7b_sharp_fp16.safetensors"),
    ("SeedVR2 / 3B Q8",    "seedvr2_ema_3b-Q8_0.gguf"),
    ("SeedVR2 / 3B FP16",  "seedvr2_ema_3b_fp16.safetensors"),
]


def _method_options(local):
    """The (label, engine, model) rows for the Method combobox. Real-ESRGAN rows are offered
    in BOTH modes now (local #11 and the remote volume-free esrgan pod #18 B). `local` is kept
    for signature stability (callers pass the current mode); it no longer gates the rows."""
    opts = [(lbl, "seedvr2", fname) for lbl, fname in _SEEDVR2_METHODS]
    try:
        import esrgan_models as em
        opts += [(f"Real-ESRGAN / {m.kind.capitalize()}", "fixed_ratio", m.key)
                 for m in em.catalog()]
    except Exception:                                # noqa: BLE001 (fail-safe: SeedVR2 still works)
        pass
    return opts


def _short_method(engine, model):
    """A compact 'Method' label for the queue list, e.g. 'SeedVR2 7B FP16' / 'ESRGAN Compact'."""
    e = (engine or "seedvr2").lower()
    if e == "fixed_ratio":
        try:
            import esrgan_models as em
            return "ESRGAN " + em.spec(model).kind.capitalize()
        except Exception:                            # noqa: BLE001
            return "ESRGAN"
    for lbl, fname in _SEEDVR2_METHODS:
        if fname == model:
            return lbl.replace(" / ", " ")
    return "SeedVR2"


def _short_gpu(gpu_id):
    """A compact 'GPU' label for the queue list from a stored per-item GPU type id (18).
    Trims the noisy vendor/generation words a RunPod id carries (e.g. 'NVIDIA RTX 6000 Ada
    Generation' -> 'RTX 6000 Ada') so the column stays narrow. Empty for an unbound row (a
    local run, or a legacy row queued before per-item binding)."""
    s = (gpu_id or "").strip()
    if not s:
        return ""
    for drop in ("NVIDIA ", " Generation"):
        s = s.replace(drop, "")
    return s.strip()


# ─────────────────────────────────────────────
#  SEGMENTS MANAGER (virtual clip jobs, section 16.5)
# ─────────────────────────────────────────────

class SegmentsManager(tk.Toplevel):
    """A focused view + editor over this root's VIRTUAL clip jobs (clip_id > 0). The
    clips are ordinary queue jobs (they run through the same pipeline), so this is
    not a separate run flow — just a place to review, rename, delete or open them."""

    def __init__(self, master, tab):
        super().__init__(master)
        self.tab = tab
        self._app = getattr(tab, "app", None)
        self.title(f"{APP_TITLE} — Segments")
        # Min width matches the main window so the right-side action buttons are
        # never clipped (bug: they were cut off at the old 560px min).
        geo = self._app.settings.get("segments_geometry") if self._app is not None else None
        self.geometry(geo if (geo and _geometry_on_screen(self, geo)) else "940x460")
        self.minsize(900, 320)
        self._last_normal_geo = None
        if self._app is not None and self._app.settings.get("segments_zoomed"):
            try:
                self.state("zoomed")               # restore maximised (like the main window)
            except tk.TclError:
                pass
        self.bind("<Configure>", self._track_geometry, add="+")
        # Modal (grab_set), like the Compare windows. Re-grab on FocusIn so opening a
        # child window from here (Compare / Play, themselves modal) and closing it
        # hands the application grab back to this window instead of freeing it.
        self.bind("<FocusIn>", lambda _e: self._grab_modal(), add="+")
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.after(60, self._grab_modal)

        # Filter bar (top): narrow the segment list by target or status. The Status
        # filter is the key one, to hide "done" clips on a long list. Fed the DISTINCT
        # values present (plus "All"); changing either re-renders the tree.
        filt = ttk.Frame(self, padding=(8, 6, 8, 0))
        filt.pack(side="top", fill="x")
        ttk.Label(filt, text="Filter:").pack(side="left")
        self.filter_target_var = tk.StringVar(value="All")
        self.filter_status_var = tk.StringVar(value="All")
        self._filter_combos = {}
        for label, var, width in (("Target", self.filter_target_var, 10),
                                  ("Status", self.filter_status_var, 12)):
            ttk.Label(filt, text=label).pack(side="left", padx=(10, 2))
            cb = ttk.Combobox(filt, textvariable=var, state="readonly",
                              width=width, values=["All"])
            cb.pack(side="left")
            cb.bind("<<ComboboxSelected>>", lambda _e: self._render())
            self._filter_combos[label] = cb
        ttk.Button(filt, text="Reset", width=6,
                   command=self._reset_filters).pack(side="left", padx=(10, 0))
        # "Display Done" — a quick, non-destructive hide of finished clips (they're
        # dead weight in the list once upscaled, but their record is what powers the
        # Compare / Play buttons, so hiding beats removing by default). The Status
        # combo can only pick ONE status; this composes as "everything, minus done".
        # Persisted per-user (default on) so the choice survives reopening the window.
        show_done = bool(self._app.settings.get("segments_show_done", True)) \
            if self._app is not None else True
        self.show_done_var = tk.BooleanVar(value=show_done)
        ttk.Checkbutton(filt, text="Display Done", variable=self.show_done_var,
                        command=self._toggle_show_done).pack(side="left", padx=(16, 0))

        body = ttk.Frame(self)
        body.pack(side="top", fill="both", expand=True)
        cols = ("label", "range", "dur", "target", "status")
        self.tree = ttk.Treeview(body, columns=cols, show="tree headings")
        self.tree.heading("#0", text="Source")
        self.tree.column("#0", width=220)
        for c, txt, w in (("label", "Label", 130), ("range", "Range", 130),
                          ("dur", "Duration", 80), ("target", "Target", 70),
                          ("status", "Status", 80)):
            self.tree.heading(c, text=txt)
            self.tree.column(c, width=w, anchor="w")
        self.tree.pack(fill="both", expand=True, side="left", padx=(8, 0), pady=8)
        self.tree.bind("<Double-1>", lambda _e: self._open_source())
        sb = ttk.Scrollbar(body, orient="vertical", command=self.tree.yview)
        sb.pack(fill="y", side="left", pady=8)
        self.tree.configure(yscrollcommand=sb.set)

        btns = ttk.Frame(body, padding=8)
        btns.pack(side="left", fill="y")
        for txt, cmd in (("Rename…", self._rename), ("Delete", self._delete),
                         ("Remove Done", self._remove_done),
                         ("Open source", self._open_source),
                         ("Open upscaled", self._open_upscaled),
                         ("Compare frames", self._compare),
                         ("Play videos", self._play), ("Refresh", self.refresh)):
            ttk.Button(btns, text=txt, width=14, command=cmd).pack(pady=2)
        self.status_var = tk.StringVar(value="")
        ttk.Label(btns, textvariable=self.status_var, foreground="#7f8a99",
                  wraplength=120).pack(pady=(8, 0))

        self._rows = {}          # iid -> clip Row (only the currently rendered rows)
        self._all_clips = []     # every clip Row for this root (pre-filter)
        self.refresh()

    def _grab_modal(self):
        """Take the application-modal grab, but never steal it from a child window or
        dialog that currently holds it (a Compare / Play window, or the Rename dialog).
        Called on open and on FocusIn, so the grab returns here once a child closes."""
        try:
            if not self.winfo_exists():
                return
            cur = self.grab_current()
            if cur is not None and cur is not self:
                return
            self.grab_set()
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
        if self._app is not None and self.winfo_exists():
            try:
                zoomed = (self.state() == "zoomed")
            except tk.TclError:
                zoomed = False
            self._app.settings["segments_geometry"] = self._last_normal_geo or self.geometry()
            self._app.settings["segments_zoomed"] = zoomed
            save_settings(self._app.settings)

    def _close(self):
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.save_geometry()
        self.destroy()

    def refresh(self):
        import batch_video_upscale as bv
        if self.tab._root_id is None:
            self._all_clips = []
        else:
            self._all_clips = list(bv.db.get_video_clips(self.tab._conn(), self.tab._root_id))
        self._populate_filters()
        self._render()

    def _populate_filters(self):
        """Fill the filter combos with the DISTINCT targets/statuses now present (plus
        'All'), keeping the current selection when it still occurs."""
        targets = sorted({c["target"] for c in self._all_clips if c["target"]})
        statuses = sorted({c["status"] for c in self._all_clips if c["status"]})
        for label, values, var in (("Target", targets, self.filter_target_var),
                                   ("Status", statuses, self.filter_status_var)):
            self._filter_combos[label].configure(values=["All"] + list(values))
            if var.get() != "All" and var.get() not in values:
                var.set("All")

    def _reset_filters(self):
        self.filter_target_var.set("All")
        self.filter_status_var.set("All")
        self._render()

    def _toggle_show_done(self):
        """Persist the Display Done choice and re-render (view-only, touches nothing)."""
        if self._app is not None:
            self._app.settings["segments_show_done"] = bool(self.show_done_var.get())
            save_settings(self._app.settings)
        self._render()

    def _remove_done(self):
        """Bulk-clear finished clips from the list. Removes only their queue/output
        RECORDS (like Delete, but for every done clip at once); the upscaled video
        files on disk are kept. Undone segments are never touched."""
        done = [c for c in self._all_clips if (c["status"] or "").lower() == "done"]
        if not done:
            self.status_var.set("No done segments to remove.")
            return
        if not messagebox.askyesno(
                APP_TITLE,
                f"Remove {len(done)} finished segment(s) from the list?\n\n"
                f"This clears their list entries only — the upscaled video files on "
                f"disk are kept.",
                parent=self):
            return
        import batch_video_upscale as bv
        conn = self.tab._conn()
        for c in done:
            bv.db.delete_video_output(conn, self.tab._root_id, c["rel_path"],
                                      c["target"], clip_id=c["clip_id"])
        self.refresh()
        self.tab._load_queue()
        self.status_var.set(f"Removed {len(done)} finished segment(s).")

    def _render(self):
        """(Re)draw the tree from _all_clips, applying the target/status filters and
        the Display Done toggle (hides finished clips when unchecked)."""
        self.tree.delete(*self.tree.get_children())
        self._rows = {}
        tf = self.filter_target_var.get()
        sf = self.filter_status_var.get()
        show_done = self.show_done_var.get()
        for c in self._all_clips:
            if tf not in ("All", "") and c["target"] != tf:
                continue
            if sf not in ("All", "") and c["status"] != sf:
                continue
            if not show_done and (c["status"] or "").lower() == "done":
                continue
            cdur = (c["clip_end"] or 0) - (c["clip_start"] or 0)
            iid = self.tree.insert(
                "", "end", text=c["rel_path"],
                values=(c["clip_label"] or "(unlabelled)",
                        f"{_clip_tc(c['clip_start'])} – {_clip_tc(c['clip_end'])}",
                        f"{cdur:.0f}s", c["target"], c["status"]))
            self._rows[iid] = c

    def _selected(self):
        sel = self.tree.selection()
        return self._rows.get(sel[0]) if sel else None

    def _abs(self, c):
        root = self.tab._src_root or ""
        return os.path.join(root, c["rel_path"])

    def _rename(self):
        c = self._selected()
        if not c:
            return
        if c["status"] != "queued":
            messagebox.showinfo(APP_TITLE, "Only a queued (not-yet-started) segment "
                                           "can be renamed.")
            return
        from tkinter import simpledialog
        new = simpledialog.askstring("Rename segment", "New label:",
                                     initialvalue=c["clip_label"] or "", parent=self)
        if new is None:
            return
        import batch_video_upscale as bv
        out = bv._output_path(self.tab._out_root, c["rel_path"], c["target"],
                              clip_id=c["clip_id"], clip_label=new,
                              clip_start=c["clip_start"], clip_end=c["clip_end"])
        bv.db.upsert_video_output(self.tab._conn(), self.tab._root_id, c["rel_path"],
                                  c["target"], clip_id=c["clip_id"],
                                  clip_label=(new.strip() or None), output_path=out)
        self.refresh()
        self.tab._load_queue()

    def _delete(self):
        c = self._selected()
        if not c:
            return
        if not messagebox.askyesno(APP_TITLE, f"Delete this segment"
                                              f"{' and its output record' if c['status']=='done' else ''}?"):
            return
        import batch_video_upscale as bv
        bv.db.delete_video_output(self.tab._conn(), self.tab._root_id, c["rel_path"],
                                  c["target"], clip_id=c["clip_id"])
        bv._remove_job_staging(c["output_path"], self.tab._vcfg().get("work_root"))
        self.refresh()
        self.tab._load_queue()

    def _open_source(self):
        c = self._selected()
        if c:
            self.tab._open_path(self._abs(c))

    def _open_upscaled(self):
        c = self._selected()
        if c and c["status"] == "done" and c["output_path"]:
            self.tab._open_path(c["output_path"])
        else:
            self.status_var.set("Not upscaled yet.")

    def _compare(self):
        c = self._selected()
        if c and c["status"] == "done" and c["output_path"]:
            self.tab._open_compare(self._abs(c), c["output_path"])
        else:
            self.status_var.set("Not upscaled yet.")

    def _play(self):
        c = self._selected()
        if c and c["status"] == "done" and c["output_path"]:
            self.tab._open_playback(self._abs(c), c["output_path"])
        else:
            self.status_var.set("Not upscaled yet.")


# ─────────────────────────────────────────────
#  APP WINDOW
# ─────────────────────────────────────────────

class VideoTab(ttk.Frame):
    """The Video Upscaler tab (#2, phase 5). Runs on a rented RunPod pod OR, feature #7,
    on this machine's LOCAL GPU (a "Run on" mode selector, gated by the install mode). A
    two-list setup flow (scan list -> Prepare -> a durable queue), a cheapest-first GPU
    picker with a live cost estimate (remote) / the detected card (local), and a
    frames-based running view. Standalone (not a ToolTab)
    because its shape is very different from the image tabs; it reuses the runner's
    GPU-free helpers (batch_video_upscale) and the cost estimator (video_estimate)
    in-process, and drives the queue via the batch_video_upscale subprocess.
    See docs/video-upscaler.md section 15."""

    def __init__(self, notebook, app):
        super().__init__(notebook, padding=(12, 10))
        self.app = app
        self.proc = None
        self._queue_io = queue.Queue()
        self._hold = ""
        self._marker_buf = None
        self.console = ConsoleBuffer()
        self.tool_name = "Video Upscaler"
        self.active_pod_id = None
        self._gpu_choices = []
        self._scan_rows = {}        # tree iid -> dict(rel, abs, width, height, ...)
        self._scan_order = []       # iids in insertion order (filtered detach/reattach)
        self._root_id = None
        self._src_root = None
        self._out_root = None
        # Scan progress (incremental, off the UI thread).
        self._scan_seq = 0
        self._scanning = False
        self._scan_q = None
        self._scan_total = 0
        self._scan_done = 0
        self._scan_start = None
        # Run progress state (frames-based, 15.8).
        self._run_total = 0
        self._run_done = 0
        self._cur_seg_frames = 0
        self._cur_seg_done = 0
        self._run_start = None
        self._rate = None           # observed s/frame, refined from done segments
        self._remote_rate = None    # pod's real billed $/h (RCOST); live cost readout
        self._run_tick_job = None
        self._local_telem_job = None   # local-run telemetry sampler (#7, graphs #9)
        self._build()
        self.after(200, self._check_readiness)
        self.after(300, self._initial_load)

    # ── config / db ──────────────────────────────────────────────────────────

    def _conn(self):
        import db
        return db.get_conn()

    def _vcfg(self):
        import batch_video_upscale as bv
        return bv.resolve_video_cfg(CFG)

    # ── layout ───────────────────────────────────────────────────────────────

    def _build(self):
        self.columnconfigure(0, weight=1)

        # 1) Mode selector + readiness strip. "Run on" picks REMOTE (a rented RunPod
        # GPU) or LOCAL (this machine's GPU, feature #7). The install mode gates it: a
        # Remote-only install can't run locally (no torch/SeedVR2) and a Local-only
        # install has no remote path, so the unavailable radio is disabled. On a "both"
        # install the last choice is remembered.
        W = Tooltip.WRAP_NARROW
        top = ttk.Frame(self)
        top.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        top.columnconfigure(3, weight=1)
        ttk.Label(top, text="Run on:").grid(row=0, column=0, sticky="w")
        imode = get_install_mode()
        if imode == "remote":
            default_mode = "remote"
        elif imode == "local":
            default_mode = "local"
        else:
            default_mode = (self.app.settings.get("video_mode", "remote")
                            if getattr(self.app, "settings", None) else "remote")
        self.mode_var = tk.StringVar(value=default_mode)
        self.mode_remote_rb = ttk.Radiobutton(
            top, text="Remote (RunPod)", value="remote",
            variable=self.mode_var, command=self._on_mode_change)
        self.mode_remote_rb.grid(row=0, column=1, padx=(6, 0))
        self.mode_local_rb = ttk.Radiobutton(
            top, text="Local GPU", value="local",
            variable=self.mode_var, command=self._on_mode_change)
        self.mode_local_rb.grid(row=0, column=2, padx=(6, 0))
        # One Tooltip per radio, retargeted (not a second one added) when an install
        # mode disables a choice: two Tooltips on one widget would both pop up.
        self.mode_remote_tip = Tooltip(
            self.mode_remote_rb,
            "Do the GPU work on a rented RunPod machine, streaming one segment at "
            "a time. Costs money per hour and needs a RunPod API key, but leaves "
            "this PC's graphics card free.", wraplength=W)
        self.mode_local_tip = Tooltip(
            self.mode_local_rb,
            "Do the GPU work on this PC's graphics card. Free, but the card is "
            "fully busy for the whole run, which can take hours per video.",
            wraplength=W)
        if imode == "remote":
            self.mode_local_rb.configure(state="disabled")
            self.mode_local_tip.set_text(
                "This is a Remote-only install (no local SeedVR2 / torch). "
                "Re-run setup as Local or Both to upscale on this machine.")
        elif imode == "local":
            self.mode_remote_rb.configure(state="disabled")
            self.mode_remote_tip.set_text(
                "This is a Local-only install (no RunPod remote stack).")
        self.ready_var = tk.StringVar(value="Checking readiness …")
        self.ready_lbl = tk.Label(top, textvariable=self.ready_var, anchor="w",
                                  fg="#7f8a99", font=("Segoe UI", 9))
        self.ready_lbl.grid(row=0, column=3, sticky="ew", padx=(16, 0))

        # 2) Source / output folders.
        ff = ttk.Frame(self)
        ff.grid(row=1, column=0, sticky="ew")
        ff.columnconfigure(1, weight=1)
        ttk.Label(ff, text="Video folder:").grid(row=0, column=0, sticky="w")
        self.src_var = tk.StringVar()
        ttk.Entry(ff, textvariable=self.src_var).grid(row=0, column=1, sticky="ew", padx=6)
        browse_src = ttk.Button(ff, text="Browse…", command=self._browse_source)
        browse_src.grid(row=0, column=2)
        self.scan_btn = ttk.Button(ff, text="Scan", command=self._scan)
        self.scan_btn.grid(row=0, column=3, padx=(6, 0))
        Tooltip(browse_src,
                "Pick the folder of videos to upscale. Sub-folders are included; "
                "your source files are never modified.", wraplength=W)
        Tooltip(self.scan_btn,
                "Look through the folder and list every video that could be "
                "upscaled, reading each one's resolution, length and frame rate. "
                "Videos already upscaled into the output folder are recognised as "
                "done. Nothing is upscaled by scanning.", wraplength=W)
        ttk.Label(ff, text="Save upscaled to:").grid(row=1, column=0, sticky="w", pady=(4, 0))
        self.out_var = tk.StringVar()
        ttk.Entry(ff, textvariable=self.out_var).grid(row=1, column=1, sticky="ew",
                                                      padx=6, pady=(4, 0))
        browse_out = ttk.Button(ff, text="Browse…", command=self._browse_output)
        browse_out.grid(row=1, column=2, pady=(4, 0))
        # Segments manager (extracted scenes = virtual clip jobs, section 16.5),
        # directly below Scan.
        self.segments_btn = ttk.Button(ff, text="Segments…", command=self._open_segments)
        self.segments_btn.grid(row=1, column=3, padx=(6, 0), pady=(4, 0))
        Tooltip(browse_out,
                "Pick where the upscaled videos are written. The source folder's "
                "structure is mirrored there.", wraplength=W)
        use_window_button_style(self.segments_btn)     # opens its own window
        Tooltip(self.segments_btn,
                "Manage extracted scenes: short pieces cut out of a long video and "
                "upscaled on their own, instead of the whole file. Useful to try a "
                "target quickly, or to keep only the part worth the GPU time.",
                wraplength=W)

        # 3) Scan list.
        sf = ttk.LabelFrame(self, text=" Eligible videos ", padding=4)
        sf.grid(row=2, column=0, sticky="nsew", pady=(8, 0))
        self.rowconfigure(2, weight=3)
        sf.rowconfigure(1, weight=1)          # the tree row (row 0 is the filter bar)
        sf.columnconfigure(0, weight=1)

        # Filter bar: narrow the list by folder path, resolution, duration bucket or
        # FPS. The combos CASCADE (faceted): each is fed only the DISTINCT values that
        # remain reachable given the OTHER active filters (plus "All"), so no
        # zero-result combination can be picked — selecting FPS 60 shrinks the
        # Resolution list to the resolutions that 60 fps videos actually have. A stale
        # selection that the other filters just excluded snaps back to "All". Changing
        # any re-applies the combined (AND) filter by detaching the non-matching rows.
        # See _refresh_filter_options / _populate_scan_filters / _apply_scan_filters.
        filt = ttk.Frame(sf)
        filt.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 4))
        ttk.Label(filt, text="Filter:").pack(side="left")
        self.filter_path_var = tk.StringVar(value="All")
        self.filter_res_var = tk.StringVar(value="All")
        self.filter_dur_var = tk.StringVar(value="All")
        self.filter_fps_var = tk.StringVar(value="All")
        self._filter_combos = {}
        for label, var, width in (("Path", self.filter_path_var, 26),
                                  ("Resolution", self.filter_res_var, 12),
                                  ("Duration", self.filter_dur_var, 16),
                                  ("FPS", self.filter_fps_var, 9)):
            ttk.Label(filt, text=label).pack(side="left", padx=(10, 2))
            cb = ttk.Combobox(filt, textvariable=var, state="readonly",
                              width=width, values=["All"])
            cb.pack(side="left")
            cb.bind("<<ComboboxSelected>>", lambda _e: self._on_scan_filter_change())
            Tooltip(cb, f"Show only videos whose {label.lower()} matches. The four "
                        f"filters combine, and each one offers only values that "
                        f"still exist under the others, so no combination can come "
                        f"back empty. This only changes what you see: nothing is "
                        f"removed from the list.", wraplength=W)
            self._filter_combos[label] = cb
        reset_scan = ttk.Button(filt, text="Reset", width=6,
                                command=self._reset_scan_filters)
        reset_scan.pack(side="left", padx=(10, 0))
        Tooltip(reset_scan, "Clear all four filters and show every video found.",
                wraplength=W)

        cols = ("res", "dur", "codec", "fps", "up", "upres", "status")
        self.scan_tree = ttk.Treeview(sf, columns=cols, show="tree headings", height=7)
        self._scan_sort = {}             # sort direction state for the scan headers
        _scan_titles = {"#0": "File"}
        self.scan_tree.column("#0", width=240, stretch=True)
        for c, txt, w in (("res", "Resolution", 90), ("dur", "Duration", 70),
                          ("codec", "Codec", 70), ("fps", "FPS", 55),
                          ("up", "Upscaled", 150), ("upres", "Up res", 80),
                          ("status", "Status", 80)):
            _scan_titles[c] = txt
            self.scan_tree.column(c, width=w, stretch=False, anchor="w")
        self._scan_sort["_titles"] = _scan_titles
        # Click a header (incl. File/#0) to sort by that column (toggles asc/desc).
        for c, txt in _scan_titles.items():
            self.scan_tree.heading(
                c, text=txt,
                command=lambda c=c: self._sort_tree(
                    self.scan_tree, c, self._scan_sort_key(c), self._scan_sort))
        self.scan_tree.tag_configure("haveup", foreground="#2f6f3f")
        self.scan_tree.tag_configure("failedup", foreground="#a04030")   # failed / gave-up output
        self.scan_tree.grid(row=1, column=0, sticky="nsew")
        sb = ttk.Scrollbar(sf, orient="vertical", command=self.scan_tree.yview)
        sb.grid(row=1, column=1, sticky="ns")
        self.scan_tree.configure(yscrollcommand=sb.set)
        self.scan_tree.bind("<<TreeviewSelect>>", self._on_scan_select)
        self.scan_tree.bind("<Double-1>", self._on_scan_double)
        self.scan_tree.bind("<Button-3>", self._on_scan_right)
        Tooltip(self.scan_tree,
                "Every video the scan found. Green means an upscaled version "
                "already exists; red means an earlier attempt failed.\n"
                "Click one to choose a target for it, double-click to open the "
                "video (double-click the Upscaled cell to compare before/after), "
                "right-click for more actions. Click a column heading to sort.",
                wraplength=W)

        # 4) Source file + Method/Model + Target + Prepare. The engine is chosen PER video
        # here (#11): the Method combobox picks engine+model, the Target combobox is then
        # filtered to what that method can reach, so a queue can freely mix methods. The
        # Source-file field is deliberately SHORT to leave room for the two selectors.
        pf = ttk.Frame(self)
        pf.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        ttk.Label(pf, text="Source file:").grid(row=0, column=0, sticky="w")
        self.srcfile_var = tk.StringVar()
        ttk.Entry(pf, textvariable=self.srcfile_var, state="readonly", width=24).grid(
            row=0, column=1, sticky="w", padx=(6, 10))
        method_lbl = ttk.Label(pf, text="Method:")
        method_lbl.grid(row=0, column=2, sticky="e")
        self.method_var = tk.StringVar()
        self.method_combo = ttk.Combobox(pf, textvariable=self.method_var,
                                         state="readonly", width=22, values=[])
        self.method_combo.grid(row=0, column=3, padx=(4, 10))
        self.method_combo.bind("<<ComboboxSelected>>", lambda _e: self._on_method_change())
        target_lbl = ttk.Label(pf, text="Target:")
        target_lbl.grid(row=0, column=4, sticky="e")
        self.target_var = tk.StringVar()
        self.target_combo = ttk.Combobox(pf, textvariable=self.target_var,
                                         state="readonly", width=15, values=[])
        self.target_combo.grid(row=0, column=5, padx=4)
        self.target_combo.bind("<<ComboboxSelected>>", lambda _e: self._sync_prepare_btn())
        self.prepare_btn = ttk.Button(pf, text="Prepare ▾ add to queue",
                                     command=self._prepare, state="disabled")
        self.prepare_btn.grid(row=0, column=6, padx=(6, 0))
        self._method_label_to_val = {}       # method label -> (engine, model)
        method_tip = ("How the video is upscaled. SeedVR2 invents new detail (best quality) "
                      "but is slow and VRAM-heavy on a local card; Real-ESRGAN is a fast, "
                      "light, fixed 2x/4x upscaler that runs on almost any GPU and keeps "
                      "text/edges cleaner. You can pick a different method for each video, "
                      "so one queue can mix them. Both run locally and on a remote pod "
                      "(Real-ESRGAN's remote pod is cheap: it needs no model volume and "
                      "runs on a low-cost GPU).")
        Tooltip(method_lbl, method_tip, wraplength=W)
        Tooltip(self.method_combo, method_tip, wraplength=W)
        target_tip = ("How large the upscaled video should be. The video is fitted "
                      "inside the chosen size, keeping its shape, so the first edge "
                      "to reach the limit decides the result. The list changes with the "
                      "chosen Method: what each method can reach on this machine differs. "
                      "Bigger targets look better but cost much more GPU time.")
        Tooltip(target_lbl, target_tip, wraplength=W)
        Tooltip(self.target_combo, target_tip, wraplength=W)
        Tooltip(self.prepare_btn,
                "Add the selected video, at the chosen Method and Target, to the queue "
                "below. This works out how the video will be split into segments; no GPU "
                "work happens until you press Start Upscaling.", wraplength=W)
        self._populate_method_combo()

        # 5) Queue list.
        qf = ttk.LabelFrame(self, text=" Upscale queue ", padding=4)
        qf.grid(row=4, column=0, sticky="nsew", pady=(8, 0))
        self.rowconfigure(4, weight=2)
        qf.rowconfigure(1, weight=1)          # the tree row (row 0 is the filter bar)
        qf.columnconfigure(0, weight=1)

        # Filter bar: narrow the queue by target or status (fed the DISTINCT values
        # present, plus "All"). Handy to hide "done" jobs on a long queue. Same
        # detach/reattach approach as the Eligible-videos filter.
        qfilt = ttk.Frame(qf)
        qfilt.grid(row=0, column=0, columnspan=3, sticky="ew", pady=(0, 4))
        ttk.Label(qfilt, text="Filter:").pack(side="left")
        self.qfilter_target_var = tk.StringVar(value="All")
        self.qfilter_status_var = tk.StringVar(value="All")
        self._qfilter_combos = {}
        for label, var, width in (("Target", self.qfilter_target_var, 10),
                                  ("Status", self.qfilter_status_var, 12)):
            ttk.Label(qfilt, text=label).pack(side="left", padx=(10, 2))
            cb = ttk.Combobox(qfilt, textvariable=var, state="readonly",
                              width=width, values=["All"])
            cb.pack(side="left")
            cb.bind("<<ComboboxSelected>>", lambda _e: self._apply_queue_filters())
            Tooltip(cb, f"Show only queued jobs with this {label.lower()}. Handy "
                        f"for hiding finished jobs on a long queue. This only "
                        f"changes what you see: the queue itself is unchanged.",
                    wraplength=W)
            self._qfilter_combos[label] = cb
        reset_queue = ttk.Button(qfilt, text="Reset", width=6,
                                 command=self._reset_queue_filters)
        reset_queue.pack(side="left", padx=(10, 0))
        Tooltip(reset_queue, "Clear both filters and show the whole queue.",
                wraplength=W)

        # "Place" = the run order (1 = next). Kept visible so re-sorting other columns
        # (a view-only sort) never hides where a job actually sits in the queue.
        qcols = ("place", "method", "gpu", "target", "status", "res", "dur", "codec", "fps",
                 "frames", "segs")
        self.queue_tree = ttk.Treeview(qf, columns=qcols, show="tree headings", height=5)
        self._queue_sort = {}
        self._queue_rows = {}            # iid -> {rel, target, props} for sort/actions
        self._queue_order = []           # iids in true (unfiltered) queue order
        _q_titles = {"#0": "File"}
        self.queue_tree.column("#0", width=200, stretch=True)
        for c, txt, w in (("place", "#", 36), ("method", "Method", 120),
                          ("gpu", "GPU", 110),
                          ("target", "Target", 60), ("status", "Status", 80),
                          ("res", "Resolution", 90), ("dur", "Duration", 70),
                          ("codec", "Codec", 60), ("fps", "FPS", 50),
                          ("frames", "Frames", 70), ("segs", "Segments", 70)):
            _q_titles[c] = txt
            self.queue_tree.column(c, width=w, stretch=False,
                                   anchor="e" if c == "place" else "w")
        self._queue_sort["_titles"] = _q_titles
        for c, txt in _q_titles.items():
            self.queue_tree.heading(
                c, text=txt,
                command=lambda c=c: self._sort_tree(
                    self.queue_tree, c, self._queue_sort_key(c), self._queue_sort))
        self.queue_tree.grid(row=1, column=0, rowspan=4, sticky="nsew")
        qsb = ttk.Scrollbar(qf, orient="vertical", command=self.queue_tree.yview)
        qsb.grid(row=1, column=1, rowspan=4, sticky="ns")
        self.queue_tree.configure(yscrollcommand=qsb.set)
        self.queue_tree.bind("<Double-1>", self._on_queue_double)
        self.queue_tree.bind("<Button-3>", self._on_queue_right)
        up_btn = ttk.Button(qf, text="↑", width=3, command=lambda: self._queue_move(-1))
        up_btn.grid(row=1, column=2, padx=(4, 0))
        down_btn = ttk.Button(qf, text="↓", width=3, command=lambda: self._queue_move(1))
        down_btn.grid(row=2, column=2, padx=(4, 0))
        remove_btn = ttk.Button(qf, text="Remove", command=self._queue_remove)
        remove_btn.grid(row=3, column=2, padx=(4, 0), pady=(4, 0))
        Tooltip(self.queue_tree,
                "The videos waiting to be upscaled, in run order (the # column, "
                "1 = next). Double-click to open a video, right-click for more "
                "actions. Sorting by a heading changes the view only, never the "
                "order they actually run in.", wraplength=W)
        Tooltip(up_btn, "Move the selected job one place earlier in the run order.",
                wraplength=W)
        Tooltip(down_btn, "Move the selected job one place later in the run order.",
                wraplength=W)
        Tooltip(remove_btn,
                "Take the selected job off the queue and delete the part-finished "
                "segment files it was holding (often several GB). The source video "
                "and any finished output are untouched.", wraplength=W)

        # 6) GPU picker + estimate.
        gf = ttk.Frame(self)
        gf.grid(row=5, column=0, sticky="ew", pady=(8, 0))
        gf.columnconfigure(5, weight=1)
        gpu_lbl = ttk.Label(gf, text="GPU:")
        gpu_lbl.grid(row=0, column=0, sticky="w")
        self.gpu_var = tk.StringVar()
        self.gpu_combo = ttk.Combobox(gf, textvariable=self.gpu_var, state="readonly",
                                     width=40, values=[])
        self.gpu_combo.grid(row=0, column=1, padx=4)
        self.gpu_combo.bind("<<ComboboxSelected>>", lambda _e: self._on_gpu_change())
        refresh_gpu = ttk.Button(gf, text="↻", width=3, command=self._refresh_gpus)
        refresh_gpu.grid(row=0, column=2)
        gpu_tip = ("Which graphics card runs the queue. For a rented pod this lists "
                   "the cards actually available right now with their hourly price, "
                   "cheapest first; the card you pick is the card you get, never a "
                   "substitute. The estimate on the right follows your choice.")
        Tooltip(gpu_lbl, gpu_tip, wraplength=W)
        Tooltip(self.gpu_combo, gpu_tip, wraplength=W)
        Tooltip(refresh_gpu,
                "Re-check which cards are in stock and at what price. Worth "
                "pressing if a run refused to start because the card sold out.",
                wraplength=W)
        # Local-only: benchmark THIS card to find its real per-target batch ceiling +
        # speed (feature #7). Created here, shown/hidden by _apply_mode_ui (pod runs
        # have no local benchmark). Column 3 so it sits right of the refresh button.
        self.benchmark_btn = ttk.Button(gf, text="Benchmark GPU…", command=self._open_benchmark)
        self.benchmark_btn.grid(row=0, column=3, padx=(8, 0))
        use_window_button_style(self.benchmark_btn)    # opens its own window
        Tooltip(self.benchmark_btn,
                "Measure the largest safe batch (and the real speed) for each target on "
                "this GPU. Runs a short test-till-it-breaks sweep; results calibrate local "
                "batch sizing + the time estimate. Safe to stop and resume.")
        # Local-only, on-demand: install Triton so the local engine can use torch.compile
        # (a pod-grade speedup). Shown only in Local mode when Triton is missing but a pinned
        # wheel exists for this Python/torch; hidden once installed. See _refresh_compile_offer.
        self.compile_btn = ttk.Button(gf, text="Enable compile speedup…",
                                      command=self._install_triton)
        self.compile_btn.grid(row=0, column=4, padx=(8, 0))
        self.compile_btn.grid_remove()
        Tooltip(self.compile_btn,
                "Install Triton (verified download) so local runs can use torch.compile, "
                "the same speedup the rented-pod runs use. Without it, local runs work fine "
                "but skip compile.")
        self.estimate_var = tk.StringVar(value="Add videos to the queue for an estimate.")
        ttk.Label(gf, textvariable=self.estimate_var, anchor="w",
                  foreground="#2f6f3f").grid(row=0, column=5, sticky="ew", padx=(12, 0))

        # 7) Start / Stop + Auto-resume + progress + status.
        af = ttk.Frame(self)
        af.grid(row=6, column=0, sticky="ew", pady=(8, 0))
        af.columnconfigure(3, weight=1)
        self.start_btn = ttk.Button(af, text="Start Upscaling", command=self._start)
        self.start_btn.grid(row=0, column=0)
        self.stop_btn = ttk.Button(af, text="Stop", command=self._stop, state="disabled")
        self.stop_btn.grid(row=0, column=1, padx=(6, 0))
        Tooltip(self.start_btn,
                "Work through the queue: each video is split into segments, "
                "upscaled a segment at a time, then put back together with its "
                "sound. On a rented pod this creates a billed machine and shuts it "
                "down when the queue ends.", wraplength=W)
        Tooltip(self.stop_btn,
                "End the run. The segment in progress is abandoned (its work is "
                "lost), but every finished segment is kept, so starting again "
                "carries on from there rather than from the beginning.",
                wraplength=W)
        # Self-healing (#6): opt-in, per-run, visible at the point of action (not a hidden
        # Setting), default OFF. When on, a lost pod reconnects or waits for the SAME GPU to
        # return and the run continues from the first unfinished segment. Passed to the
        # runner as IMGTBX_AUTO_RESUME (see _start).
        self.auto_resume_var = tk.BooleanVar(value=False)
        self.auto_resume_chk = ttk.Checkbutton(af, text="Auto-resume",
                                               variable=self.auto_resume_var)
        self.auto_resume_chk.grid(row=0, column=2, padx=(10, 0))
        Tooltip(self.auto_resume_chk,
                "Survive losing the pod mid-run without babysitting.\n"
                "A connectivity blip reconnects to the same pod; a real pod loss waits "
                "(no time cap, $0 billed while waiting) for the SAME GPU to come back in "
                "stock, redeploys it, and continues from the first unfinished segment.\n"
                "Never substitutes a different card. The funds safety-net, a completed "
                "queue, or Stop still end the run.")
        self.progress = ProgressBar(af, width=200)
        self.progress.grid(row=0, column=3, sticky="ew", padx=12)
        video_log_btn = ttk.Button(af, text="View log", command=self._view_log)
        video_log_btn.grid(row=0, column=4)
        Tooltip(video_log_btn,
                "Open the log window with the full output of the run: per-segment "
                "progress, timings and any errors.", wraplength=W)
        self.status_var = tk.StringVar(value="Ready.")
        tk.Label(self, textvariable=self.status_var, anchor="w", fg="#7f8a99",
                 font=("Consolas", 9)).grid(row=7, column=0, sticky="ew", pady=(4, 0))

        # 8) Local-machine telemetry (CPU/RAM/GPU), like the other tabs. A LOCAL
        # run (#7) works this GPU, so its load must be visible here too; the
        # earlier layout only had the remote row, so a local video run showed no
        # telemetry at all. Click it to open the usage-graph window (#9). The idle
        # sampler keeps it fresh between runs; a local run drives its own sampling
        # (see _start_local_telemetry) because the idle sampler pauses during a run.
        self.telemetry_row = TelemetryRow(self, prefix="Local Unit")
        self.telemetry_row.grid(row=8, column=0, sticky="ew", pady=(4, 0))

        # 9) Remote-pod telemetry (CPU/RAM/GPU). Created hidden; App.apply_remote_
        # telemetry reveals it on the first RTELEM sample of a run and _end_run
        # hides it again, so it only shows while a pod is actually streaming.
        self.remote_telemetry_row = TelemetryRow(self, prefix="Remote pod")
        self.remote_telemetry_row.grid(row=9, column=0, sticky="ew", pady=(2, 0))
        self.remote_telemetry_row.grid_remove()

        # Greys the pod-only Auto-resume control if we're starting in Local mode.
        self._apply_mode_ui()

    # ── readiness ────────────────────────────────────────────────────────────

    def restore_defaults_if_empty(self):
        if not self.src_var.get():
            d = get_default_folder("video_source")
            if d:
                self.src_var.set(os.path.normpath(d))
        if not self.out_var.get():
            d = get_default_folder("video_output")
            if d:
                self.out_var.set(os.path.normpath(d))

    def _initial_load(self):
        """Startup population of the durable queue (scheduled from __init__). Restore the
        folder fields from the pinned defaults FIRST, then load: when the Video Upscaler is the
        RESTORED last tab, on_enter's default-restore is not guaranteed to have run before this
        fires, and _load_queue early-returns on an empty source field, which left previously-cut
        segments invisible until the user acted (they only appeared once a NEW segment was added
        and forced a reload). Idempotent with on_enter (both restore-if-empty + reload)."""
        self.restore_defaults_if_empty()
        self._load_queue()

    def on_enter(self):
        """Called when the tab is entered (not only at startup): re-check remote
        readiness so a RunPod API key / SSH key / volume set after launch is seen,
        and refresh the durable queue."""
        self.restore_defaults_if_empty()
        self._apply_mode_ui()
        self._check_readiness()
        self._load_queue()
        # Local mode: auto-detect the card (a cheap nvidia-smi, unlike the remote GPU
        # list which hits the RunPod API and stays user-triggered via ↻).
        if self.mode_var.get() == "local":
            self._refresh_gpus()

    def _on_mode_change(self):
        """Local/Remote radio flipped: persist the choice (a 'both' install remembers
        it), re-check readiness, adapt the auto-resume control (pod-only) and refresh the
        GPU display + estimate for the new mode."""
        try:
            if getattr(self.app, "settings", None) is not None:
                self.app.settings["video_mode"] = self.mode_var.get()
                save_settings(self.app.settings)
        except Exception:                              # noqa: BLE001 (persist is best-effort)
            pass
        self._apply_mode_ui()
        self._check_readiness()
        self._refresh_gpus()
        # Real-ESRGAN is a local engine: the Method list gains/loses it with the mode, and the
        # reachable Target list can change with it (#11).
        self._populate_method_combo()
        self._on_method_change()

    def _apply_mode_ui(self):
        """Enable/disable the mode-specific controls. Auto-resume heals a LOST POD, so it is
        meaningless locally: grey it out (and clear it) in Local mode. The Benchmark button
        is the inverse: it calibrates the LOCAL card, so it only shows in Local mode."""
        local = self.mode_var.get() == "local"
        try:
            if local:
                self.auto_resume_var.set(False)
            self.auto_resume_chk.configure(state="disabled" if local else "normal")
        except Exception:                              # noqa: BLE001
            pass
        try:
            # The button now shows in BOTH modes: local calibrates THIS card, remote
            # calibrates a rented pod GPU (feature #7, docs section 22). Only the local
            # sweep contends for the local GPU, so only local honours the running-job lock.
            self.benchmark_btn.grid()
            if local:
                self._refresh_benchmark_lock()
            else:
                self.benchmark_btn.configure(state="normal")
        except Exception:                              # noqa: BLE001
            pass
        self._refresh_compile_offer()

    def _refresh_compile_offer(self):
        """Show the 'Enable compile speedup…' button only in Local mode, when Triton is not
        yet installed but a pinned wheel exists for this Python/torch. Hidden otherwise (Remote
        mode, already installed, or unsupported combo). Fail-safe."""
        btn = getattr(self, "compile_btn", None)
        if btn is None:
            return
        try:
            show = self.mode_var.get() == "local"
            if show:
                import triton_setup
                # Offer the Triton install only when it would actually enable compile: a pinned
                # wheel exists, Triton isn't installed yet, AND a C compiler is on PATH (inductor
                # needs one; Triton alone can't compile SeedVR2, so don't send the user to install
                # it in vain).
                show = (triton_setup.is_supported()
                        and not triton_setup.triton_installed()
                        and triton_setup.compiler_available())
            if show:
                btn.grid()
            else:
                btn.grid_remove()
        except Exception:                              # noqa: BLE001
            try:
                btn.grid_remove()
            except Exception:                          # noqa: BLE001
                pass

    def _install_triton(self):
        """On-demand: download + install the verified triton-windows wheel, then hide the
        offer. Local runs pick up torch.compile automatically on the next Start."""
        from gui.dialogs import prompt_install_triton

        def done(ok):
            self._refresh_compile_offer()
            if ok:
                messagebox.showinfo(
                    APP_TITLE, "Triton installed. Local runs will now use the torch.compile "
                               "speedup (the first segment pays a one-time compile cost).")
        prompt_install_triton(self.winfo_toplevel(), on_done=done)

    def _open_benchmark(self):
        """Open the per-card benchmark modal (feature #7). LOCAL calibrates this machine's
        GPU (which no other local job may be using); REMOTE calibrates the picked pod GPU
        (docs section 22): it deploys a pod for the selected card and sweeps it there."""
        remote = self.mode_var.get() != "local"
        gpu = None
        if remote:
            gpu = self._selected_gpu()
            if not gpu or not gpu.get("id"):
                messagebox.showinfo(APP_TITLE,
                                    "Pick a GPU from the list first (press ↻ to refresh "
                                    "live availability), then Benchmark GPU.")
                return
        else:
            # Any LOCAL-GPU job (this tab, Batch Upscale, or Tag & Rename) would fight the
            # benchmark for the card. The button is greyed while one runs; this is the
            # click-time backstop.
            if self.app.local_gpu_job_running():
                messagebox.showinfo(APP_TITLE,
                                    "Finish the current local job before benchmarking the GPU.")
                return
        # Reuse an already-open benchmark window instead of stacking a second one (the window
        # is modal, so this is a belt-and-suspenders guard).
        existing = getattr(self, "_benchmark_win", None)
        if existing is not None and existing.winfo_exists():
            existing.deiconify()
            existing.lift()
            existing.focus_set()
            return
        # Open on the tab's currently-selected method (SeedVR2 vs a Real-ESRGAN tier), so the
        # window matches what the user is about to run; it stays switchable in the window.
        method = None
        try:
            engine, model = self._selected_method()
            if engine == "fixed_ratio":
                import esrgan_models as em
                method = ("fixed_ratio", em.spec(model).kind)
            else:
                method = ("seedvr2", None)
        except Exception:                              # noqa: BLE001
            method = None
        try:
            from gui.video_benchmark import BenchmarkWindow
            self._benchmark_win = BenchmarkWindow(self.winfo_toplevel(), self,
                                                  remote=remote, gpu=gpu, method=method)
        except Exception as exc:                       # noqa: BLE001
            messagebox.showerror(APP_TITLE, f"Could not open the benchmark window:\n{exc}")

    def _refresh_benchmark_lock(self):
        """Grey the benchmark button while any local-GPU job is running (see
        App.local_gpu_job_running). The button is also hidden entirely in Remote mode by
        _apply_mode_ui; guard for that (grid_remove'd widgets still accept configure)."""
        btn = getattr(self, "benchmark_btn", None)
        if btn is None:
            return
        try:
            busy = self.app.local_gpu_job_running()
        except Exception:                              # noqa: BLE001
            busy = self.running
        btn.configure(state="disabled" if busy else "normal")

    def _check_readiness(self):
        rpc = CFG.get("runpod", {})
        mode = self.mode_var.get()

        def work():
            msg, ok = self._readiness_text(rpc, mode)
            self.after(0, lambda: self._set_ready(msg, ok))

        threading.Thread(target=work, daemon=True).start()

    def _readiness_text(self, rpc, mode="remote"):
        # Local mode (#7): only ffmpeg + a local NVIDIA GPU matter (no RunPod key / SSH /
        # volume). Everything else in the pipeline (split/reassemble/mux) is local anyway.
        if mode == "local":
            try:
                import video_pipeline as vp
                vp.find_ffmpeg()
            except Exception:
                return ("Not ready: ffmpeg not found - re-run the first-launch setup, or "
                        "put ffmpeg.exe + ffprobe.exe in ffmpeg\\bin (or on the PATH).", False)
            try:
                import system_telemetry as st
                g = st.sample_gpu()                    # dict of GPU fields, or None
                name = st.gpu_name()
            except Exception:
                g, name = None, None
            if not g:
                return ("Not ready: no NVIDIA GPU detected (nvidia-smi). Local upscaling "
                        "needs a CUDA GPU; use Remote instead.", False)
            vram = f"{g['gpu_total_mb'] / 1024:.0f} GB" if g.get("gpu_total_mb") else "?"
            return (f"Local ready — {name or 'GPU'}, {vram} VRAM. The first segment "
                    f"calibrates the batch size for your card.", True)
        # Local ffmpeg first (a purely local check): every video job needs the
        # local split/reassemble/mux, so without it nothing else matters. Only
        # a successful lookup is cached, so installing ffmpeg later and
        # re-entering the tab picks it up without a restart.
        try:
            import video_pipeline as vp
            vp.find_ffmpeg()
        except Exception:
            return ("Not ready: ffmpeg not found - re-run the first-launch setup, or "
                    "put ffmpeg.exe + ffprobe.exe in ffmpeg\\bin (or on the PATH).", False)
        if not rpc.get("api_key"):
            return "Not ready: set a RunPod API key (RunPod tab).", False
        try:
            import ssh_setup
            key = os.path.expandvars(rpc.get("ssh_key_path", "")) or ssh_setup.default_key_path()
        except Exception:
            key = os.path.expandvars(rpc.get("ssh_key_path", ""))
        if not (key and os.path.exists(key)):
            return "Not ready: no SSH key — use 'Set up SSH key' (Settings).", False
        vol = (rpc.get("network_volume_id") or "").strip()
        if not vol:
            return "Not ready: no model network volume configured (Settings).", False
        try:
            import runpod_client as rp
            region = rp.volume_region(rpc["api_key"], vol)
        except Exception as exc:
            return f"Not ready: could not reach RunPod ({exc}).", False
        if not region:
            return "Not ready: configured network volume not found.", False
        return f"Remote ready — models in {region}.", True

    def _set_ready(self, msg, ok):
        self.ready_var.set(msg)
        self.ready_lbl.configure(fg="#2f6f3f" if ok else "#b23b3b")
        self._ready = ok

    # ── browse / scan ────────────────────────────────────────────────────────

    def _browse_source(self):
        folder = filedialog.askdirectory(title="Choose the folder with videos to upscale")
        if folder:
            # askdirectory returns forward slashes on Windows; normpath gives the
            # native backslash form so both fields read consistently (no X:/a\b mix).
            folder = os.path.normpath(folder)
            self.src_var.set(folder)
            out = self.out_var.get().strip()
            if not out or os.path.basename(out) == "__upscaled__":
                self.out_var.set(os.path.join(folder, "__upscaled__"))

    def _browse_output(self):
        folder = filedialog.askdirectory(title="Choose where to save the upscaled videos")
        if folder:
            self.out_var.set(os.path.normpath(folder))

    def _scan(self):
        src = self.src_var.get().strip()
        if not src or not os.path.isdir(src):
            messagebox.showwarning(APP_TITLE, "Choose a valid video folder first.")
            return
        if self._scanning:
            return
        if not self.out_var.get().strip():
            self.out_var.set(os.path.join(src, "__upscaled__"))
        self._src_root = os.path.abspath(src)
        self._out_root = os.path.abspath(self.out_var.get().strip())
        # Reflect the canonical (backslash) form back into the fields, and remember
        # BOTH folders so a relaunch restores them together (output used to be lost).
        self.src_var.set(self._src_root)
        self.out_var.set(self._out_root)
        set_default_folder("video_source", self._src_root)
        set_default_folder("video_output", self._out_root)
        self.scan_tree.delete(*self.scan_tree.get_children())
        self._scan_rows.clear()
        self._scan_order.clear()
        self._reset_scan_filters(apply=False)   # drop the previous scan's filter values
        self.progress.set(0)
        self._scanning = True
        self._scan_total = self._scan_done = 0
        self._scan_skipped = []          # rels ffprobe could not read
        self._scan_ineligible = []       # rels already >= the largest target (4K source)
        self._scan_res = {}              # "WxH" -> count, for the summary breakdown
        self._scan_listing = 0           # files found so far during the tree walk
        self._scan_removed = []          # (rel, target) outputs reconciled off disk
        self._scan_adopted = []          # (rel, target) outputs found on disk, adopted into the DB
        self._scan_seq += 1
        seq = self._scan_seq
        self._scan_q = queue.Queue()
        self.scan_btn.configure(state="disabled")
        self.status_var.set("Listing files …")
        self.console.feed(f"Scanning {self._src_root} …\n")
        threading.Thread(target=lambda: self._scan_worker(seq), daemon=True).start()
        self.after(120, lambda: self._scan_poll(seq))

    def _scan_worker(self, seq):
        """Walk + probe each video off the UI thread, streaming results to the
        poller via a queue so the list/progress fill in live. ffprobe is cached by
        (mtime, size), so a re-scan of an unchanged tree races through.

        Walking a large tree on a network drive can take many seconds, so we
        iterate the walk lazily and stream a running 'found N' count while it runs
        (otherwise the first feedback is the long-delayed total)."""
        import batch_video_upscale as bv
        try:
            conn = self._conn()
            self._root_id = bv.db.get_video_root_id(conn, self._src_root, self._out_root)
            cutoff = self._vcfg()["skip_cutoff_pct"]
            # Drop DB records for upscaled outputs the user deleted off disk, so the
            # scan reflects what's actually there (and they can be re-queued).
            removed = bv.reconcile_video_outputs(conn, self._root_id)
            if removed:
                self._scan_q.put(("reconciled", removed))
            files = []
            # Prune the output tree: when "Save upscaled to" lives inside the source
            # folder (the default <source>/__upscaled__), the walk must NOT re-read the
            # finished upscales as if they were new source videos.
            for abs_path, rel in bv.iter_videos(self._src_root, skip_roots=[self._out_root]):
                if seq != self._scan_seq:
                    return
                files.append((abs_path, rel))
                if len(files) % 25 == 0:
                    self._scan_q.put(("listing", len(files)))
            files.sort(key=lambda t: t[1].lower())
            self._scan_q.put(("total", len(files)))
            for abs_path, rel in files:
                if seq != self._scan_seq:
                    return                       # a newer scan superseded this one
                r = bv.scan_file(conn, self._root_id, abs_path, rel)
                if r is None:
                    self._scan_q.put(("skip", rel))
                    continue
                elig = bv.eligible_targets(r["width"], r["height"], cutoff)
                if not elig:
                    # Already fills the largest target box (a 4K / >=3840-long-side source
                    # has nothing to upscale to) -> not an eligible video, so skip it.
                    self._scan_q.put(("ineligible", rel))
                    continue
                # Adopt any output another install already wrote to the shared destination but
                # that THIS install's DB doesn't know about (cross-install consistency), so the
                # row shows it as done instead of offering to redo it.
                adopted = bv.reconcile_outputs_from_disk(conn, self._root_id, self._out_root, rel)
                if adopted:
                    self._scan_q.put(("adopted", adopted))
                outs = [(o["target"], o["status"], o["output_path"])
                        for o in bv.db.get_video_outputs_for(conn, self._root_id, rel)]
                self._scan_q.put(("row", (rel, abs_path, dict(r), elig, outs)))
            self._scan_q.put(("done", None))
        except Exception as exc:                 # noqa: BLE001 (UI thread surfaces it)
            self._scan_q.put(("error", str(exc)))

    def _scan_poll(self, seq):
        if seq != self._scan_seq:
            return
        import video_estimate as ve
        drained = 0
        while drained < 400:                     # cap per cycle: stay responsive on
            try:                                 # a fast cached re-scan of a huge tree
                kind, data = self._scan_q.get_nowait()
            except queue.Empty:
                break
            drained += 1
            if kind == "reconciled":
                self._scan_removed = data
                for rel, tgt in data:
                    self.console.feed(f"  removed (deleted off disk): {rel} → {tgt}\n")
                self._load_queue()       # the removed jobs leave any queue view
            elif kind == "adopted":
                self._scan_adopted.extend(data)
                for rel, tgt in data:
                    self.console.feed(f"  found on disk (already upscaled elsewhere): {rel} → {tgt}\n")
            elif kind == "listing":
                self._scan_listing = data
                self.status_var.set(f"Listing files … {data} found")
            elif kind == "total":
                self._scan_total = data
                self._scan_start = time.time()
                self.console.feed(f"Found {data} video file(s); reading properties …\n")
                if data == 0:
                    self.status_var.set("No videos found in this folder.")
            elif kind == "row":
                self._insert_scan_row(*data)
                self._scan_done += 1
                w, h = data[2]["width"], data[2]["height"]
                key = f"{w}×{h}" if w and h else "unknown"
                self._scan_res[key] = self._scan_res.get(key, 0) + 1
                self.console.feed(f"  {data[0]}  ({w}×{h}, "
                                  f"{data[2]['vcodec']})\n")
            elif kind == "skip":
                self._scan_done += 1
                self._scan_skipped.append(data)
                self.console.feed(f"  {data}  (unreadable, skipped)\n")
            elif kind == "ineligible":
                self._scan_done += 1
                self._scan_ineligible.append(data)
                self.console.feed(f"  {data}  (already >= 4K, skipped)\n")
            elif kind == "error":
                self.status_var.set(f"Scan failed: {data}")
                self._finish_scan()
                return
            elif kind == "done":
                self._finish_scan()
                return
        if self._scan_total > 0:
            self.progress.set(100.0 * self._scan_done / self._scan_total)
            el = time.time() - (self._scan_start or time.time())
            eta = (self._scan_total - self._scan_done) * (el / self._scan_done) \
                if self._scan_done > 0 else None
            tail = f" · ETA {ve.fmt_duration(eta)}" if eta else ""
            self.status_var.set(f"Scanning {self._scan_done}/{self._scan_total}{tail}")
        # If the cap was hit there is likely more waiting — poll again promptly.
        self.after(1 if drained >= 400 else 120, lambda: self._scan_poll(seq))

    def _sort_tree(self, tree, col, keyfunc, state):
        """View-sort a Treeview by `col` (toggling asc/desc), keying each row through
        `keyfunc(iid)`. Header-only: it reorders the displayed rows, not any underlying
        queue order. `state` is a per-tree dict remembering the current direction."""
        reverse = not state.get(col, True)          # first click = ascending
        rows = [(keyfunc(iid), iid) for iid in tree.get_children("")]
        try:
            rows.sort(key=lambda t: t[0], reverse=reverse)
        except TypeError:                                # mixed types -> compare as str
            rows.sort(key=lambda t: str(t[0]), reverse=reverse)
        for i, (_k, iid) in enumerate(rows):
            tree.move(iid, "", i)
        titles = state.get("_titles", {})
        for k in list(state.keys()):                     # reset directions, keep titles
            if k != "_titles":
                del state[k]
        state[col] = reverse
        for c, base in titles.items():
            arrow = (" ▲" if not reverse else " ▼") if c == col else ""
            tree.heading(c, text=base + arrow)

    def _scan_sort_key(self, col):
        """Sort key for a scan row, by the UNDERLYING property (not the display text),
        so Resolution/Duration/FPS sort numerically."""
        def key(iid):
            row = self._scan_rows.get(iid) or {}
            r = row.get("r") or {}
            if col == "#0":
                return (row.get("rel") or "").lower()
            if col == "res":
                return (r.get("width") or 0) * (r.get("height") or 0)
            if col == "dur":
                return r.get("duration") or 0.0
            if col == "fps":
                return r.get("fps") or 0.0
            if col == "codec":
                return (r.get("vcodec") or "").lower()
            return (self.scan_tree.set(iid, col) or "").lower()
        return key

    @staticmethod
    def _scan_row_tags(outs):
        """Colour tag for a scan row: green when it has a done upscale, else a muted
        red when it has a failed / gave-up output the user may want to retry (item 4)."""
        if any(o[1] == "done" for o in outs):
            return ("haveup",)
        if any(o[1] in ("failed", "skipped") for o in outs):
            return ("failedup",)
        return ()

    def _insert_scan_row(self, rel, abs_path, r, elig, outs):
        import video_estimate as ve
        res = f"{r['width']}x{r['height']}" if r["width"] else "?"
        dur = ve.fmt_duration(r["duration"]) if r["duration"] else "?"
        fps = f"{r['fps']:.2f}" if r["fps"] else "?"
        done = [o for o in outs if o[1] == "done"]
        up = ", ".join(t for t, _s, _p in done)
        iid = self.scan_tree.insert(
            "", "end", text=rel,
            values=(res, dur, r["vcodec"] or "?", fps, up, up, ""),
            tags=self._scan_row_tags(outs))
        self._scan_rows[iid] = {"rel": rel, "abs": abs_path, "elig": elig,
                                "outs": outs, "r": dict(r)}
        self._scan_order.append(iid)

    def _finish_scan(self):
        self._scanning = False
        self.scan_btn.configure(state="normal")
        n = len(self._scan_rows)
        skipped = getattr(self, "_scan_skipped", [])
        ineligible = getattr(self, "_scan_ineligible", [])
        res = getattr(self, "_scan_res", {})
        removed = getattr(self, "_scan_removed", [])
        adopted = getattr(self, "_scan_adopted", [])
        self.progress.set(100 if self._scan_total else 0)
        tail = f", {len(ineligible)} already ≥ 4K (skipped)" if ineligible else ""
        tail += f", {len(skipped)} unreadable (skipped)" if skipped else ""
        tail += f", {len(removed)} stale removed" if removed else ""
        tail += f", {len(adopted)} found on disk" if adopted else ""
        self.status_var.set(f"{n} eligible video(s){tail}." if n
                            else f"No eligible videos found{tail}.")
        self._populate_scan_filters()
        # A clearly delimited summary block (find-able after the long per-file run):
        # totals, then a count grouped by resolution, then the skipped files.
        bar = "=" * 56
        self.console.feed(f"\n{bar}\n")
        self.console.feed("Scan summary\n")
        self.console.feed(f"  Videos ready to queue: {n}\n")
        if ineligible:
            self.console.feed(f"  Already >= 4K (skipped): {len(ineligible)}\n")
        if skipped:
            self.console.feed(f"  Unreadable (skipped):  {len(skipped)}\n")
        if removed:
            self.console.feed(f"  Upscaled removed (deleted off disk): {len(removed)}\n")
            for rel, tgt in removed:
                self.console.feed(f"     {rel} → {tgt}\n")
        if adopted:
            self.console.feed(f"  Already upscaled elsewhere (found on disk): {len(adopted)}\n")
            for rel, tgt in adopted:
                self.console.feed(f"     {rel} → {tgt}\n")
        if res:
            self.console.feed("  By resolution:\n")
            for key, cnt in sorted(res.items(), key=lambda kv: (-kv[1], kv[0])):
                self.console.feed(f"     {key:<13}{cnt}\n")
        if skipped:
            self.console.feed("  Unreadable files:\n")
            for rel in skipped:
                self.console.feed(f"     {rel}\n")
        self.console.feed(f"{bar}\n")

    # ── scan-list filters ──────────────────────────────────────────────────────

    _DUR_BUCKETS = (("0-10 seconds", 10), ("11-30 seconds", 30),
                    ("31-60 seconds", 60), ("60-300 seconds", 300),
                    ("over 300 seconds", None))

    def _dur_bucket(self, seconds):
        """The duration-filter bucket label for `seconds` (None if unknown)."""
        if not seconds:
            return None
        for label, hi in self._DUR_BUCKETS:
            if hi is None or seconds <= hi:
                return label
        return None

    @staticmethod
    def _fps_label(fps):
        """The FPS-filter value for a probed fps (2 decimals; None if unknown)."""
        return f"{fps:.2f}" if fps else None

    def _scan_filter_vars(self):
        """label -> StringVar for the four Eligible-videos filters, in bar order."""
        return {"Path": self.filter_path_var, "Resolution": self.filter_res_var,
                "Duration": self.filter_dur_var, "FPS": self.filter_fps_var}

    def _row_facet(self, row, label):
        """The value `row` contributes to the `label` filter (None if unknown)."""
        r = row.get("r") or {}
        if label == "Path":
            return os.path.dirname(row["rel"]) or "(root)"
        if label == "Resolution":
            return f"{r['width']}x{r['height']}" if r.get("width") else None
        if label == "Duration":
            return self._dur_bucket(r.get("duration"))
        if label == "FPS":
            return self._fps_label(r.get("fps"))
        return None

    def _sort_facet(self, label, values):
        """Order a facet's distinct values the way its combo should present them."""
        vals = [v for v in values if v is not None]
        if label == "Resolution":
            return sorted(vals, key=lambda s: [int(x) for x in s.split("x")])
        if label == "Duration":
            return [lbl for lbl, _hi in self._DUR_BUCKETS if lbl in vals]
        if label == "FPS":
            return sorted(vals, key=float)
        return sorted(vals, key=str.lower)

    def _row_matches_except(self, row, skip):
        """True if `row` satisfies every active filter other than `skip`'s. Used to
        compute the values still reachable in the `skip` combo (faceted narrowing)."""
        for label, var in self._scan_filter_vars().items():
            if label == skip:
                continue
            val = var.get()
            if val and val != "All" and self._row_facet(row, label) != val:
                return False
        return True

    def _refresh_filter_options(self):
        """Repopulate every filter combo with only the values still reachable given the
        OTHER active filters, so a zero-result combination can't be selected. A current
        selection the other filters just excluded is snapped back to 'All'. Iterated to a
        fixed point because such a reset can widen what the remaining combos may offer."""
        rows = list(self._scan_rows.values())
        vars_ = self._scan_filter_vars()
        for _ in range(len(vars_)):
            changed = False
            for label, var in vars_.items():
                reachable = {self._row_facet(row, label) for row in rows
                             if self._row_matches_except(row, label)}
                ordered = self._sort_facet(label, reachable)
                self._filter_combos[label].configure(values=["All"] + ordered)
                if var.get() != "All" and var.get() not in ordered:
                    var.set("All")
                    changed = True
            if not changed:
                break

    # Called at scan end; the initial fill (all filters at 'All') is just the full
    # distinct set of each facet, which _refresh_filter_options computes.
    _populate_scan_filters = _refresh_filter_options

    def _on_scan_filter_change(self):
        """A combo changed: recompute the reachable option lists, then re-apply."""
        self._refresh_filter_options()
        self._apply_scan_filters()

    def _reset_scan_filters(self, apply=True):
        """Set every filter back to 'All'. The Reset button (apply=True) repopulates the
        combos to their full reachable lists and re-shows all rows; a new scan calls it
        with apply=False, which additionally empties the stale value lists (repopulated
        at scan end)."""
        for var in (self.filter_path_var, self.filter_res_var,
                    self.filter_dur_var, self.filter_fps_var):
            var.set("All")
        if not apply:
            for cb in self._filter_combos.values():
                cb.configure(values=["All"])
        if apply:
            self._refresh_filter_options()
            self._apply_scan_filters()

    def _apply_scan_filters(self):
        """Show only the rows matching every active filter, by detaching the rest and
        reattaching matches in insertion order. Safe before any scan (empty order)."""
        idx = 0
        for iid in self._scan_order:
            if iid not in self._scan_rows:
                continue
            if self._row_matches_filters(iid):
                self.scan_tree.reattach(iid, "", idx)
                idx += 1
            else:
                self.scan_tree.detach(iid)

    def _row_matches_filters(self, iid):
        row = self._scan_rows.get(iid)
        if not row:
            return False
        r = row.get("r") or {}
        pf = self.filter_path_var.get()
        if pf and pf != "All" and (os.path.dirname(row["rel"]) or "(root)") != pf:
            return False
        rf = self.filter_res_var.get()
        if rf and rf != "All" and f"{r.get('width')}x{r.get('height')}" != rf:
            return False
        df = self.filter_dur_var.get()
        if df and df != "All" and self._dur_bucket(r.get("duration")) != df:
            return False
        ff = self.filter_fps_var.get()
        if ff and ff != "All" and self._fps_label(r.get("fps")) != ff:
            return False
        return True

    def _done_targets(self, rel):
        """Targets already upscaled for `rel`, read LIVE from the DB (not the scan
        cache) so a job that finished after the scan still disables re-Prepare."""
        if self._root_id is None:
            return set()
        import batch_video_upscale as bv
        return {o["target"] for o in bv.db.get_video_outputs_for(self._conn(), self._root_id, rel)
                if o["status"] == "done"}

    def _on_scan_select(self, _e=None):
        sel = self.scan_tree.selection()
        if not sel:
            return
        row = self._scan_rows.get(sel[0])
        if not row:
            return
        self.srcfile_var.set(row["abs"])
        self._populate_target_combo(row)
        self._sync_prepare_btn()

    def _current_max_mp(self):
        """The max feasible OUTPUT megapixels for the currently selected GPU (local detected
        card or the picked pod GPU), or 0.0 when no GPU is selected (then nothing is filtered).
        Local uses the card's own benchmark/learned data; remote uses the VRAM-tier seed."""
        g = self._selected_gpu()
        if not g:
            return 0.0
        import video_estimate as ve
        return ve.max_output_mp(g.get("memory_gb"), g.get("id") or g.get("name"), self._conn())

    def _queue_feasibility(self, jobs, g):
        """(max_mp, feasible_jobs, infeasible_count) for a queue on GPU `g`. When the card's
        max output-MP is unknown (0) nothing is filtered. Drives the Start refusal and the
        IMGTBX_MAX_OUTPUT_MP the runner uses to DEFER (not fail) jobs the card can't reach."""
        import video_estimate as ve
        max_mp = (ve.max_output_mp(g.get("memory_gb"), g.get("id") or g.get("name"),
                                   self._conn()) if g else 0.0)
        if not max_mp:
            return max_mp, jobs, 0
        # Real-ESRGAN (#11) is not bound by SeedVR2's output-MP ceiling, so a fixed_ratio job
        # is always feasible here (its real limits come from a benchmark later, not this cap).
        feasible = [j for j in jobs
                    if j.get("engine") == "fixed_ratio"
                    or ve.target_is_feasible(j.get("width"), j.get("height"), j["target"], max_mp)]
        return max_mp, feasible, len(jobs) - len(feasible)

    def _populate_method_combo(self):
        """Fill the Method combobox with engine+model options for the current run mode
        (Real-ESRGAN is offered in Local mode only). Preserves the current selection when it
        still exists, else pre-selects the configured default method (#11)."""
        local = bool(getattr(self, "mode_var", None) and self.mode_var.get() == "local")
        opts = _method_options(local)
        self._method_label_to_val = {lbl: (eng, mdl) for lbl, eng, mdl in opts}
        labels = [lbl for lbl, _e, _m in opts]
        self.method_combo.configure(values=labels)
        if self.method_var.get() not in self._method_label_to_val:
            self.method_var.set(self._default_method_label(opts))

    def _default_method_label(self, opts):
        """The Method label pre-selected from Settings defaults (video.engine + its model),
        falling back to the first option."""
        vid = CFG.get("video", {})
        eng = (vid.get("engine") or "seedvr2").lower()
        want = vid.get("fixed_ratio_model") if eng == "fixed_ratio" else vid.get("dit_model")
        for lbl, e, m in opts:
            if e == eng and (m == want or want is None):
                return lbl
        for lbl, e, m in opts:                       # engine matched, model didn't: first of engine
            if e == eng:
                return lbl
        return opts[0][0] if opts else ""

    def _selected_method(self):
        """(engine, model) for the chosen Method label; defaults to SeedVR2 if unset."""
        return self._method_label_to_val.get(self.method_var.get()) or ("seedvr2", None)

    def _on_method_change(self):
        """Method changed: the reachable targets differ by engine, so re-filter the Target
        combobox for the selected source, then re-sync the Prepare button. In remote mode the
        GPU picker's VRAM floor + region also depend on the engine (Real-ESRGAN uses a low
        floor + region-wide, #18 B), so refresh the card list too."""
        sel = self.scan_tree.selection()
        if sel and sel[0] in self._scan_rows:
            self._populate_target_combo(self._scan_rows[sel[0]])
        if self.mode_var.get() != "local":
            self._refresh_gpus()
        self._sync_prepare_btn()

    def _populate_target_combo(self, row):
        """Fill the Target combobox with the targets the source can reach for the SELECTED
        METHOD on the selected GPU (#7/#11), each shown as a concrete output resolution.

        SeedVR2: source-eligible ratios + presets, filtered by the card's VRAM feasibility.

        Real-ESRGAN is fixed-ratio: it is NOT VRAM-capped, but it is restricted to targets whose
        scale is EXACTLY a native model scale (2x / 4x), so the generated frame is never
        ffmpeg-resized up or down. A preset that coincides with a native scale (e.g. 4K IS 2x of
        1080p) is offered; a mismatched one (1440p from 1080p, 1.33x) is dropped, so that path
        stays SeedVR2-only. Stores a label->token map for Prepare."""
        import video_estimate as ve
        r = row.get("r") or {}
        w, h = r.get("width"), r.get("height")
        done_targets = self._done_targets(row["rel"])
        engine, model = self._selected_method()
        if engine == "fixed_ratio":
            import esrgan_models as em
            scales = em.tier_scales(em.spec(model).kind)

            def _ok(t):
                s = ve.fit_scale(w, h, t)
                # Exact native scale only (small tolerance for odd source dims / rounding),
                # so no resize of the generated frame is ever needed.
                return s is not None and any(abs(s / ns - 1.0) <= 0.01 for ns in scales)
            feas = [t for t in row.get("elig", []) if _ok(t)]
        else:
            max_mp = self._current_max_mp()
            feas = [t for t in row.get("elig", []) if ve.target_is_feasible(w, h, t, max_mp)]
        labels = [ve.target_label(w, h, t) for t in feas]
        self._target_label_to_token = dict(zip(labels, feas))
        self.target_combo.configure(values=labels)
        if feas:
            nxt = next((t for t in feas if t not in done_targets), feas[0])
            self.target_var.set(ve.target_label(w, h, nxt))
        else:
            self.target_var.set("")

    def _selected_target_token(self):
        """The canonical target token ('1080p' / '2X' …) for the label shown in the combobox."""
        return getattr(self, "_target_label_to_token", {}).get(self.target_var.get())

    def _sync_prepare_btn(self):
        sel = self.scan_tree.selection()
        row = self._scan_rows.get(sel[0]) if sel else None
        target = self._selected_target_token()
        ok = bool(row and target)
        if ok and target in self._done_targets(row["rel"]):
            ok = False                           # already upscaled to this target (15.1 step 5)
        self.prepare_btn.configure(state="normal" if ok else "disabled")

    def _on_scan_double(self, event):
        iid = self.scan_tree.identify_row(event.y)
        if not iid:
            return
        row = self._scan_rows.get(iid)
        if not row:
            return
        col = self.scan_tree.identify_column(event.x)
        # Double-click the "Upscaled" cell (#5) -> compare; else open the source.
        done = [o for o in row["outs"] if o[1] == "done"]
        if col == "#5" and done:
            self._open_compare(row["abs"], done[0][2])
        else:
            self._open_path(row["abs"])

    def _on_scan_right(self, event):
        iid = self.scan_tree.identify_row(event.y)
        if not iid:
            return
        self.scan_tree.selection_set(iid)
        row = self._scan_rows.get(iid)
        if not row:
            return
        m = tk.Menu(self, tearoff=0)
        m.add_command(label="Extract segment…",
                      command=lambda: self._open_segment_picker(row),
                      state="normal" if row.get("elig") else "disabled")
        m.add_separator()
        m.add_command(label="Open source video", command=lambda: self._open_path(row["abs"]))
        m.add_command(label="Open source folder",
                      command=lambda: self._open_folder(row["abs"]))
        for t, s, p in row["outs"]:
            if s == "done" and p:
                m.add_command(label=f"Compare videos ({t})",
                              command=lambda p=p: self._open_playback(row["abs"], p))
                m.add_command(label=f"Compare frames ({t})",
                              command=lambda p=p: self._open_compare(row["abs"], p))
                m.add_command(label=f"Open upscaled ({t})",
                              command=lambda p=p: self._open_path(p))
                m.add_command(label=f"Open upscaled folder ({t})",
                              command=lambda p=p: self._open_folder(p))
        # A failed / gave-up (skipped) output leaves the run queue, so the scan list is
        # where the user retries or inspects it (item 4).
        for t, s, _p in row["outs"]:
            if s in ("failed", "skipped"):
                verb = "gave up" if s == "skipped" else "failed"
                m.add_separator()
                m.add_command(label=f"Retry ({t}, {verb})",
                              command=lambda t=t: self._retry_job(row["rel"], t))
                m.add_command(label=f"Show reason ({t})",
                              command=lambda t=t: self._show_job_reason(row["rel"], t))
        try:
            m.tk_popup(event.x_root, event.y_root)
        finally:
            m.grab_release()

    # ── segment extractor (section 16.4/16.5) ─────────────────────────────────

    def _open_segment_picker(self, row):
        """Open the picker for one scanned video; its Queue commits virtual clip
        jobs via _queue_clips."""
        if not row.get("elig"):
            messagebox.showinfo(APP_TITLE, "This video is already at/above the "
                                           "largest target — nothing to upscale.")
            return
        from gui.video_segment_picker import VideoSegmentPicker
        VideoSegmentPicker(self.app, self.app, row["abs"], row["rel"],
                           row["elig"], on_queue=self._queue_clips)

    def _queue_clips(self, rel, clips):
        """Enqueue the picker's pending clips as virtual jobs (off the UI thread)."""
        if self._root_id is None or not clips:
            return
        # Same queue-add guard as Prepare, per distinct target (all clips share the
        # source, so one verdict per target covers its clips). Drop downscale targets
        # and confirm marginal (< 50 %) ones ONCE, before committing anything.
        import video_estimate as ve
        srow = next((v for v in self._scan_rows.values() if v.get("rel") == rel), None)
        r = (srow or {}).get("r") or {}
        sw, sh = r.get("width"), r.get("height")
        if sw and sh:
            verdicts = {t: ve.classify_upscale(sw, sh, t) for t in {c["target"] for c in clips}}
            blocked = {t for t, v in verdicts.items() if v == "downscale"}
            marginal = {t for t, v in verdicts.items() if v == "marginal"}
            if blocked:
                messagebox.showwarning(
                    APP_TITLE,
                    f"This video is {sw}x{sh}; target(s) {', '.join(sorted(blocked))} "
                    f"would downscale it. Clip(s) for those target(s) were skipped.",
                    parent=self)
            if marginal and not messagebox.askyesno(
                    APP_TITLE,
                    f"Target(s) {', '.join(sorted(marginal))} enlarge this {sw}x{sh} "
                    f"video by less than 50%. Add those clip(s) to the queue anyway?",
                    parent=self):
                blocked |= marginal
            clips = [c for c in clips if c["target"] not in blocked]
            if not clips:
                return

        def work():
            import batch_video_upscale as bv
            conn = self._conn()
            errs = []
            for c in clips:
                try:
                    bv.prepare_clip(conn, self._root_id, self._src_root, self._out_root,
                                    rel, c["target"], c["start"], c["end"],
                                    c["label"], self._vcfg())
                except Exception as exc:             # noqa: BLE001
                    errs.append(str(exc))
            self.after(0, lambda: self._after_queue_clips(len(clips) - len(errs), errs))

        threading.Thread(target=work, daemon=True).start()

    def _after_queue_clips(self, n, errs):
        self._load_queue()
        self._update_estimate()
        if errs:
            self.status_var.set(f"Queued {n} segment(s); {len(errs)} failed: {errs[0]}")
        else:
            self.status_var.set(f"Queued {n} segment(s) to the upscale queue.")

    def _open_segments(self):
        """Open the Segments manager over this root's virtual clip jobs."""
        if self._root_id is None:
            self._load_queue()                       # try to bind a root from the fields
        if self._root_id is None:
            messagebox.showinfo(APP_TITLE, "Scan a video folder first.")
            return
        win = getattr(self, "_segments_win", None)
        if win is not None and win.winfo_exists():
            win.refresh()
            win.deiconify()
            win.lift()
            return
        self._segments_win = SegmentsManager(self.app, self)

    def _open_path(self, p):
        try:
            os.startfile(p)
        except Exception as exc:                         # noqa: BLE001
            messagebox.showerror(APP_TITLE, f"Could not open:\n{p}\n{exc}")

    def _open_folder(self, p):
        try:
            subprocess.Popen(["explorer", "/select,", os.path.normpath(p)])
        except Exception:
            self._open_path(os.path.dirname(p))

    def _open_compare(self, src, up):
        """Open/reuse the shared FRAME comparison window (scrub + before/after wipe,
        libVLC-free)."""
        if not (up and os.path.exists(up)):
            self._open_path(src)
            return
        win = getattr(self.app, "video_comparison_window", None)
        if win is not None and win.winfo_exists():
            win.show_videos(src, up)
            win.deiconify()
            win.lift()
        else:
            self.app.video_comparison_window = VideoComparisonWindow(
                self.app, src, up, app=self.app)

    def _open_playback(self, src, up):
        """Open/reuse the shared VIDEO playback window (side-by-side / A-B flip, with
        audio, via libVLC)."""
        if not (up and os.path.exists(up)):
            self._open_path(src)
            return
        win = getattr(self.app, "video_playback_window", None)
        if win is not None and win.winfo_exists():
            win.show_videos(src, up)
            win.deiconify()
            win.lift()
        else:
            self.app.video_playback_window = VideoPlaybackWindow(
                self.app, src, up, app=self.app)

    # ── prepare / queue ──────────────────────────────────────────────────────

    def _upscale_add_ok(self, src_w, src_h, target):
        """Queue-add guard shared by the Prepare and clip paths. Blocks a DOWNSCALE
        outright and asks for confirmation on a MARGINAL (< 50 % larger) upscale.
        Returns True when the caller may proceed. See video_estimate.classify_upscale."""
        import video_estimate as ve
        verdict = ve.classify_upscale(src_w, src_h, target)
        if verdict is None:
            return True
        out = ve.output_dims(src_w, src_h, target)
        out_txt = f"{out[0]}x{out[1]}" if out else target
        if verdict == "downscale":
            messagebox.showwarning(
                APP_TITLE,
                f"This video is already {src_w}x{src_h}. Upscaling to {target} "
                f"({out_txt}) would DOWNSCALE it, not enlarge it.\n\n"
                f"It was not added to the queue. Pick a larger target.",
                parent=self)
            return False
        s = ve.fit_scale(src_w, src_h, target) or 1.0
        return messagebox.askyesno(
            APP_TITLE,
            f"This video is {src_w}x{src_h}; target {target} ({out_txt}) enlarges it "
            f"by only {round((s - 1) * 100)}% (less than 50%).\n\n"
            f"A small upscale gains little quality for the GPU time / cost. "
            f"Add it to the queue anyway?",
            parent=self)

    def _prepare(self):
        sel = self.scan_tree.selection()
        row = self._scan_rows.get(sel[0]) if sel else None
        target = self._selected_target_token()
        if not row or not target:
            return
        r = row.get("r") or {}
        if not self._upscale_add_ok(r.get("width"), r.get("height"), target):
            return
        # No status-bar chatter for queue edits: the queue list is the source of
        # truth and shows the add/remove directly. Only a failure (below) is worth
        # surfacing, since it has no other on-screen home.
        self.prepare_btn.configure(state="disabled")
        # Read the Method + GPU on the UI thread (tk vars aren't safe off it) and stamp the job.
        engine, model = self._selected_method()
        # Per-item GPU binding (18): remote runs stamp the card selected right now, so a mixed
        # queue can route each job to its own pod at Start. Local runs leave it NULL (there is
        # one local GPU; grouping is a remote concept).
        gpu_id = None
        if self.mode_var.get() == "remote":
            g = self._selected_gpu()
            gpu_id = (g.get("id") or g.get("name")) if g else None

        def work():
            import batch_video_upscale as bv
            try:
                info = bv.prepare_job(self._conn(), self._root_id, self._src_root,
                                      self._out_root, row["rel"], target, self._vcfg(),
                                      engine=engine, model=model, gpu=gpu_id)
            except Exception as exc:                     # noqa: BLE001
                self.after(0, lambda e=exc: self.status_var.set(f"Prepare failed: {e}"))
                return
            self.after(0, lambda: self._after_prepare(row["rel"], target, info))

        threading.Thread(target=work, daemon=True).start()

    def _after_prepare(self, rel, target, info):
        # The new row appearing in the queue list is the feedback; no status line.
        self._load_queue()
        self._sync_prepare_btn()
        self._update_estimate()

    def _load_queue(self):
        """Populate the queue list from the DB (survives restarts, 15.1)."""
        import batch_video_upscale as bv
        conn = self._conn()
        if self._root_id is None:
            src = self.src_var.get().strip()
            if src and os.path.isdir(src):
                self._src_root = os.path.abspath(src)
                self._out_root = os.path.abspath(self.out_var.get().strip()
                                                 or os.path.join(self._src_root, "__upscaled__"))
                self._root_id = bv.db.get_video_root_id(conn, self._src_root, self._out_root)
            else:
                return
        import video_estimate as ve
        self.queue_tree.delete(*self.queue_tree.get_children())
        self._queue_rows = {}
        self._queue_order = []
        for place, j in enumerate(bv.db.get_video_queue(conn, self._root_id), start=1):
            vf = bv.db.get_video_file(conn, self._root_id, j["rel_path"])
            clip_id = j["clip_id"] or 0
            segs = bv.db.get_video_segments(conn, self._root_id, j["rel_path"],
                                            j["target"], clip_id=clip_id)
            done = sum(1 for s in segs if s["status"] == "done")
            segtxt = f"{done}/{len(segs)}" if segs else "?"
            w = vf["width"] if vf else 0
            h = vf["height"] if vf else 0
            res = f"{w}x{h}" if w else "?"
            codec = (vf["vcodec"] if vf else None) or "?"
            fps = f"{vf['fps']:.2f}" if (vf and vf["fps"]) else "?"
            if clip_id:
                # A virtual clip: show it as "<rel> ✂ label", its own (clipped)
                # duration and its approximate frame count.
                label = j["clip_label"] or f"{_clip_tc(j['clip_start'])}-{_clip_tc(j['clip_end'])}"
                text = f"{j['rel_path']}  ✂ {label}"
                cdur = (j["clip_end"] or 0) - (j["clip_start"] or 0)
                dur = ve.fmt_duration(cdur) if cdur else "?"
                frames = (j["clip_frames"] or None) or "?"
                row_dur = cdur or 0.0
            else:
                text = j["rel_path"]
                dur = ve.fmt_duration(vf["duration"]) if (vf and vf["duration"]) else "?"
                frames = (vf["nb_frames"] if vf else None) or "?"
                row_dur = (vf["duration"] if vf else 0) or 0.0
            abs_path = os.path.join(self._src_root, j["rel_path"]) if self._src_root else j["rel_path"]
            jkeys = j.keys()
            method = _short_method(j["engine"] if "engine" in jkeys else None,
                                   j["model"] if "model" in jkeys else None)
            gpu_id = (j["gpu"] if "gpu" in jkeys else None) or None
            gpu_lbl = _short_gpu(gpu_id)
            iid = self.queue_tree.insert(
                "", "end", text=text,
                values=(place, method, gpu_lbl, j["target"], j["status"], res, dur, codec, fps,
                        frames, segtxt),
                tags=(j["rel_path"], j["target"], str(clip_id)))
            self._queue_order.append(iid)
            self._queue_rows[iid] = {
                "rel": j["rel_path"], "target": j["target"], "clip_id": clip_id,
                "abs": abs_path, "w": w or 0, "h": h or 0, "place": place,
                "status": j["status"], "skip_reason": j["skip_reason"],
                "method": method,
                "engine": (j["engine"] if "engine" in jkeys else None) or "seedvr2",
                "gpu": gpu_id,
                "duration": row_dur,
                "fps": (vf["fps"] if vf else 0) or 0.0,
                "codec": codec}
        self._populate_queue_filters()
        self._apply_queue_filters()
        self._apply_queue_feasibility()
        self._refresh_scan_outputs()
        self._update_estimate()

    def _apply_queue_feasibility(self):
        """Grey a queued job only when ITS OWN card can't reach its target (18: per-item GPU
        binding, the semantic flip). A remote row prepared under a specific GPU is feasible by
        construction (the Target combo was filtered by that card at Prepare), so it is NEVER
        re-greyed by the bottom picker's current selection: changing that picker only affects the
        next Prepare. An UNBOUND row (a local run, or a legacy pre-binding row) falls back to the
        selected card (feature #7), and Real-ESRGAN is never VRAM-capped (#11).

        `row['feasible']` is set for the Start gate; the row is tagged 'infeasible' (muted)."""
        import video_estimate as ve
        self.queue_tree.tag_configure("infeasible", foreground="#8a8f98")
        sel_max = self._current_max_mp()
        for iid, row in self._queue_rows.items():
            if row.get("engine") == "fixed_ratio" or row.get("gpu"):
                feasible = True          # Real-ESRGAN (uncapped) or bound to its own card
            else:
                feasible = ve.target_is_feasible(row.get("w"), row.get("h"),
                                                 row["target"], sel_max)
            row["feasible"] = feasible
            tags = [t for t in self.queue_tree.item(iid, "tags") if t != "infeasible"]
            if not feasible:
                tags.append("infeasible")
            self.queue_tree.item(iid, tags=tags)

    def _on_gpu_change(self):
        """The selected GPU changed. With per-item GPU binding (18) this only sets the card for
        the NEXT Prepare: it re-estimates and re-filters the Target combobox for the selected
        scan row, but already-queued rows keep their own bound card (the feasibility pass no
        longer re-greys a bound row, only unbound legacy/local ones)."""
        self._update_estimate()
        self._apply_queue_feasibility()
        sel = self.scan_tree.selection()
        if sel and sel[0] in self._scan_rows:
            self._populate_target_combo(self._scan_rows[sel[0]])
            self._sync_prepare_btn()

    def _populate_queue_filters(self):
        """Fill the queue filter combos with the DISTINCT targets/statuses now queued
        (each prefixed 'All'). Preserves the current selection if it still occurs."""
        rows = self._queue_rows.values()
        targets = sorted({r["target"] for r in rows if r.get("target")})
        statuses = sorted({r["status"] for r in rows if r.get("status")})
        for label, values, var in (("Target", targets, self.qfilter_target_var),
                                   ("Status", statuses, self.qfilter_status_var)):
            self._qfilter_combos[label].configure(values=["All"] + list(values))
            if var.get() != "All" and var.get() not in values:
                var.set("All")

    def _reset_queue_filters(self):
        self.qfilter_target_var.set("All")
        self.qfilter_status_var.set("All")
        self._apply_queue_filters()

    def _apply_queue_filters(self):
        """Show only queue rows matching the target/status filters, by detaching the
        rest and reattaching matches in true queue order (so 'Place' stays meaningful)."""
        tf = self.qfilter_target_var.get()
        sf = self.qfilter_status_var.get()
        idx = 0
        for iid in self._queue_order:
            row = self._queue_rows.get(iid)
            if not row:
                continue
            ok = ((tf in ("All", "") or row.get("target") == tf)
                  and (sf in ("All", "") or row.get("status") == sf))
            if ok:
                self.queue_tree.reattach(iid, "", idx)
                idx += 1
            else:
                self.queue_tree.detach(iid)

    def _refresh_scan_outputs(self):
        """Re-read the scan rows' upscaled counterparts so the 'Upscaled' columns
        and the green tag stay current after a run (without a full re-scan). Uses a
        SINGLE query for the whole root, not one per file — a per-row query froze
        the UI thread on a large tree (1000+ files)."""
        if self._root_id is None or not self._scan_rows:
            return
        import batch_video_upscale as bv
        by_rel = {}
        for o in bv.db.get_video_outputs_all(self._conn(), self._root_id):
            by_rel.setdefault(o["rel_path"], []).append(
                (o["target"], o["status"], o["output_path"]))
        for iid, row in self._scan_rows.items():
            outs = by_rel.get(row["rel"], [])
            if outs == row.get("outs"):
                continue                      # unchanged — skip the tree write
            row["outs"] = outs
            done = [o for o in outs if o[1] == "done"]
            up = ", ".join(t for t, _s, _p in done)
            vals = list(self.scan_tree.item(iid, "values"))
            vals[4] = up           # Upscaled
            vals[5] = up           # Up res (target == short side, shown as the target)
            self.scan_tree.item(iid, values=vals, tags=self._scan_row_tags(outs))

    def _selected_queue_job(self):
        sel = self.queue_tree.selection()
        if not sel:
            return None
        tags = self.queue_tree.item(sel[0], "tags")
        if len(tags) < 2:
            return None
        clip_id = int(tags[2]) if len(tags) >= 3 else 0
        return (tags[0], tags[1], clip_id)

    def _queue_remove(self):
        job = self._selected_queue_job()
        if not job:
            return
        import batch_video_upscale as bv
        conn = self._conn()
        # Reclaim the removed job's staging dir (its segments are gigabytes and only got
        # cleaned on success before). Look up its output_path before deleting the rows.
        row = bv.db.get_video_output(conn, self._root_id, job[0], job[1], clip_id=job[2])
        bv.db.delete_video_output(conn, self._root_id, job[0], job[1], clip_id=job[2])
        if row is not None:
            bv._remove_job_staging(row["output_path"], self._vcfg().get("work_root"))
        self._load_queue()

    def _retry_job(self, rel, target, clip_id=0):
        """Re-queue a failed / gave-up job (item 4): zero its fail_count and set it
        back to 'queued', clearing the old reason, so the next run tries it fresh."""
        if self._root_id is None:
            return
        import batch_video_upscale as bv
        conn = self._conn()
        bv.db.reset_video_fail_count(conn, self._root_id, rel, target, clip_id=clip_id)
        bv.db.upsert_video_output(conn, self._root_id, rel, target, clip_id=clip_id,
                                  status="queued", skip_reason=None)
        self._load_queue()
        self.status_var.set(f"Re-queued {rel} -> {target}.")

    def _show_job_reason(self, rel, target, clip_id=0):
        """Show why a job failed / gave up (the recorded skip_reason)."""
        if self._root_id is None:
            return
        import batch_video_upscale as bv
        job = bv.db.get_video_output(self._conn(), self._root_id, rel, target, clip_id=clip_id)
        reason = (job["skip_reason"] if job else None) or "No reason recorded."
        messagebox.showinfo(APP_TITLE, f"{rel} -> {target}\n\n{reason}", parent=self)

    def _queue_move(self, delta):
        job = self._selected_queue_job()
        if not job or self._root_id is None:
            return
        import batch_video_upscale as bv
        conn = self._conn()
        jobs = list(bv.db.get_video_queue(conn, self._root_id))
        idx = next((i for i, j in enumerate(jobs)
                    if j["rel_path"] == job[0] and j["target"] == job[1]
                    and (j["clip_id"] or 0) == job[2]), -1)
        ni = idx + delta
        if idx < 0 or ni < 0 or ni >= len(jobs):
            return
        # Swap queue_order with the neighbour, then renormalise positions.
        order = [(j["rel_path"], j["target"], j["clip_id"] or 0) for j in jobs]
        order[idx], order[ni] = order[ni], order[idx]
        for pos, (rel, tgt, cid) in enumerate(order):
            bv.db.set_queue_order(conn, self._root_id, rel, tgt, pos, clip_id=cid)
        self._load_queue()
        # Re-select the moved row.
        for iid in self.queue_tree.get_children():
            t = self.queue_tree.item(iid, "tags")
            if (len(t) >= 3 and t[0] == job[0] and t[1] == job[1]
                    and int(t[2]) == job[2]):
                self.queue_tree.selection_set(iid)
                break

    def _queue_move_to_top(self, row):
        """Send the job to the front of the queue (it runs first)."""
        if self._root_id is None:
            return
        import batch_video_upscale as bv
        conn = self._conn()
        jobs = [(j["rel_path"], j["target"], j["clip_id"] or 0)
                for j in bv.db.get_video_queue(conn, self._root_id)]
        key = (row["rel"], row["target"], row.get("clip_id", 0))
        if key not in jobs:
            return
        jobs.remove(key)
        jobs.insert(0, key)
        for pos, (rel, tgt, cid) in enumerate(jobs):
            bv.db.set_queue_order(conn, self._root_id, rel, tgt, pos, clip_id=cid)
        self._load_queue()

    def _queue_sort_key(self, col):
        """Sort key for a queue row, by the underlying property where numeric."""
        def key(iid):
            row = self._queue_rows.get(iid) or {}
            if col == "#0":
                return (row.get("rel") or "").lower()
            if col == "place":
                return row.get("place") or 0
            if col == "res":
                return (row.get("w") or 0) * (row.get("h") or 0)
            if col == "dur":
                return row.get("duration") or 0.0
            if col == "fps":
                return row.get("fps") or 0.0
            if col == "codec":
                return (row.get("codec") or "").lower()
            return (self.queue_tree.set(iid, col) or "").lower()
        return key

    def _on_queue_double(self, event):
        """Double-click a queued video to open the source file in the default player
        (same as the scan list)."""
        iid = self.queue_tree.identify_row(event.y)
        row = self._queue_rows.get(iid) if iid else None
        if row:
            self._open_path(row["abs"])

    def _on_queue_right(self, event):
        iid = self.queue_tree.identify_row(event.y)
        if not iid:
            return
        self.queue_tree.selection_set(iid)
        row = self._queue_rows.get(iid)
        if not row:
            return
        running = str(self.start_btn["state"]) == "disabled"
        m = tk.Menu(self, tearoff=0)
        m.add_command(label="Start upscaling", command=self._start,
                      state="disabled" if running else "normal")
        m.add_separator()
        m.add_command(label="Move to top", command=lambda: self._queue_move_to_top(row))
        m.add_command(label="Move up", command=lambda: self._queue_move(-1))
        m.add_command(label="Move down", command=lambda: self._queue_move(1))
        m.add_command(label="Remove from queue", command=self._queue_remove)
        if row.get("status") == "failed":
            m.add_separator()
            m.add_command(label="Retry (reset & re-queue)",
                          command=lambda: self._retry_job(row["rel"], row["target"],
                                                          row.get("clip_id", 0)))
            m.add_command(label="Show failure reason",
                          command=lambda: self._show_job_reason(row["rel"], row["target"],
                                                                row.get("clip_id", 0)))
        m.add_separator()
        m.add_command(label="Open source video", command=lambda: self._open_path(row["abs"]))
        m.add_command(label="Open source folder",
                      command=lambda: self._open_folder(row["abs"]))
        try:
            m.tk_popup(event.x_root, event.y_root)
        finally:
            m.grab_release()

    # ── GPU picker + estimate ────────────────────────────────────────────────

    def _refresh_gpus(self):
        if self.mode_var.get() == "local":
            return self._refresh_local_gpu()
        rpc = CFG.get("runpod", {})
        if not rpc.get("api_key"):
            self.gpu_var.set("(set a RunPod API key)")
            return
        self.gpu_var.set("loading GPUs …")
        self.gpu_combo.configure(values=[])

        def work():
            try:
                import batch_video_upscale as bv
                import runpod_client as rp
                import video_estimate as ve
                key = rpc["api_key"]
                # Real-ESRGAN (#18 B) deploys a VOLUME-FREE pod region-wide, so its picker
                # is NOT scoped to the SeedVR2 model volume's region and uses the LOW esrgan
                # VRAM floor (cheap cards), not SeedVR2's 32/80/90. SeedVR2 keeps the volume
                # region + its target floor.
                engine, _model = self._selected_method()
                if engine == "fixed_ratio":
                    dc, floor = None, ve.ESRGAN_VRAM_FLOOR
                else:
                    vol = (rpc.get("network_volume_id") or "").strip()
                    dc = rp.volume_region(key, vol) if vol else None
                    floor = ve.max_target_floor(self._queue_jobs()) or 24
                gpus = rp.available_gpus(key, dc, min_memory_gb=floor)
                self.after(0, lambda: self._populate_gpus(gpus))
            except Exception as exc:                     # noqa: BLE001
                self.after(0, lambda e=exc: self.gpu_var.set(f"GPU list failed: {e}"))

        threading.Thread(target=work, daemon=True).start()

    def _populate_gpus(self, gpus):
        import video_estimate as ve
        jobs = self._queue_jobs()
        ranked = ve.recommend_gpus(gpus, jobs, self._spin_up(),
                                   conn=self._conn()) if jobs else []
        self._gpu_choices = ranked or gpus
        labels = []
        for g in self._gpu_choices:
            price = f"${g['price']:.2f}/h" if g.get("price") is not None else "n/a"
            est = g.get("estimate")
            tail = f" → ${est['cost']:.2f}" if est else ""
            labels.append(f"{g['name']} — {g['memory_gb']} GB — {price} ({g['stock']}){tail}")
        self.gpu_combo.configure(values=labels)
        if labels:
            self.gpu_combo.current(0)
        else:
            self.gpu_var.set("no eligible GPU available right now")
        self._on_gpu_change()

    def _refresh_local_gpu(self):
        """Local mode (#7): the 'GPU' is simply this machine's card (no live list / price /
        stock). Detect it off-thread (nvidia-smi) and show it as the single choice so the
        rest of the flow (estimate, Start) reads it through `_selected_gpu` unchanged."""
        self.gpu_var.set("detecting local GPU …")
        self.gpu_combo.configure(values=[])

        def work():
            try:
                import system_telemetry as st
                g = st.sample_gpu()
                name = st.gpu_name() or "Local GPU"
            except Exception:                          # noqa: BLE001
                g, name = None, None
            self.after(0, lambda: self._populate_local_gpu(g, name))

        threading.Thread(target=work, daemon=True).start()

    def _populate_local_gpu(self, g, name):
        if not g:
            self._gpu_choices = []
            self.gpu_combo.configure(values=[])
            self.gpu_var.set("no NVIDIA GPU detected")
            self._update_estimate()
            return
        vram_gb = round((g.get("gpu_total_mb") or 0) / 1024.0)
        # A synthetic choice shaped like a remote GPU dict (id/name/memory_gb/price/stock)
        # so _selected_gpu / _start read it the same way; price=None marks it free/local.
        # id = the nvidia-smi card name so it matches the perf key the LOCAL runner records
        # under (batch_video_upscale uses engine.gpu_id = the same name), letting the
        # estimate read this card's own measured history back.
        self._gpu_choices = [{"id": name, "name": name, "memory_gb": vram_gb,
                              "price": None, "stock": "local"}]
        self.gpu_combo.configure(values=[f"{name}, {vram_gb} GB"])
        self.gpu_combo.current(0)
        self._on_gpu_change()

    def _selected_gpu(self):
        if not self._gpu_choices:
            return None
        i = self.gpu_combo.current()
        return self._gpu_choices[i if 0 <= i < len(self._gpu_choices) else 0]

    def _queue_jobs(self):
        """The queue as estimator job dicts [{frames, target, segments, width,
        height}]. width/height are the source size so the estimate is aspect-correct
        (cost scales with output megapixels, not frame count)."""
        import batch_video_upscale as bv
        if self._root_id is None:
            return []
        conn = self._conn()
        jobs = []
        for j in bv.db.get_video_queue(conn, self._root_id):
            vf = bv.db.get_video_file(conn, self._root_id, j["rel_path"])
            if j["clip_id"]:
                # A clip costs its OWN (clipped) frames/duration, not the source's.
                frames = (j["clip_frames"] or 0)
                dur = (j["clip_end"] or 0) - (j["clip_start"] or 0)
            else:
                frames = (vf["nb_frames"] if vf else 0) or 0
                dur = (vf["duration"] if vf else 0) or 0
            seg_secs = self._vcfg()["segment_seconds"]
            import math as _m
            segs = max(1, _m.ceil(dur / seg_secs)) if seg_secs else 1
            jkeys = j.keys()
            jobs.append({"frames": frames, "target": j["target"], "segments": segs,
                         "width": (vf["width"] if vf else None),
                         "height": (vf["height"] if vf else None),
                         "engine": (j["engine"] if "engine" in jkeys else None) or "seedvr2",
                         # model drives the Real-ESRGAN rate lookup (rate is per model tier).
                         "model": (j["model"] if "model" in jkeys else None),
                         # Per-item GPU + identity (18): the grouped Start reorders/persists by
                         # (engine, gpu) and estimates each group on its own card.
                         "gpu": (j["gpu"] if "gpu" in jkeys else None) or "",
                         "rel": j["rel_path"], "clip_id": j["clip_id"] or 0})
        return jobs

    def _spin_up(self):
        return float(CFG.get("video", {}).get("spin_up_seconds", 360))

    def _update_estimate(self):
        import video_estimate as ve
        jobs = self._queue_jobs()
        if not jobs:
            self.estimate_var.set("Add videos to the queue for an estimate.")
            return
        if self.mode_var.get() == "local":
            return self._update_local_estimate(jobs)
        g = self._selected_gpu()
        if not g:
            self.estimate_var.set(f"{len(jobs)} job(s) queued — pick a GPU (↻).")
            return
        est = ve.estimate_queue(jobs, g.get("id") or g.get("name"), g.get("price"),
                                self._spin_up(), conn=self._conn())
        if not est:
            self.estimate_var.set(f"{g['name']} can't serve every queued target.")
            return
        self.estimate_var.set(
            f"{len(jobs)} job(s) · {ve.fmt_duration(est['duration_seconds'])} · "
            f"${est['cost']:.2f} · {est['segments']} segments · "
            f"${est['cost_per_segment']:.3f}/segment")

    def _update_local_estimate(self, jobs):
        """Local mode (#7): no cost (the GPU is free). The TIME is HONEST and history-driven
        -- once this card+target has measured history (db.gpu_perf, filled per segment by a
        local run) the estimate shows a real time; before that it shows the work SIZE and
        says the first segment calibrates it (rather than quoting a fabricated number). A
        seeded (benchmark-suite) rate is flagged '(rough)'."""
        import video_estimate as ve
        g = self._selected_gpu()
        est = (ve.estimate_queue_local(jobs, g.get("id") or g.get("name"), self._conn())
               if g else None)
        segs = sum(j.get("segments") or 1 for j in jobs)
        if est:
            qual = "" if est["calibrated"] else " (rough)"
            self.estimate_var.set(
                f"{len(jobs)} job(s) · ~{ve.fmt_duration(est['duration_seconds'])}{qual} · "
                f"{segs} segments · runs on your GPU (no cost).")
            return
        frames = sum(j.get("frames") or 0 for j in jobs)
        self.estimate_var.set(
            f"{len(jobs)} job(s) · {frames:,} frames · {segs} segments · runs on your GPU "
            f"(no cost). Time depends on the target; the first segment calibrates it.")

    # ── start / stop / run ───────────────────────────────────────────────────

    def _start(self):
        if self.proc is not None:
            return
        if not getattr(self, "_ready", False):
            messagebox.showwarning(APP_TITLE, self.ready_var.get())
            return
        jobs = self._queue_jobs()
        if not jobs:
            messagebox.showinfo(APP_TITLE, "The queue is empty. Prepare a video first.")
            return
        if self.mode_var.get() == "local":
            return self._start_local(jobs)
        # Grouped multi-pod Start (18): the queue mixes (engine, gpu) groups -> reorder it
        # visibly and run one pod per group. Any fixed_ratio (Real-ESRGAN, #18 B) group ALSO
        # takes the grouped path even alone, because it needs the volume-free esrgan pod, not
        # the classic SeedVR2 single-pod path below (which a lone fixed_ratio group would
        # otherwise wrongly deploy as a SeedVR2 video pod). A single SeedVR2 group is unchanged.
        import batch_video_upscale as bv
        group_keys = bv.distinct_group_keys(jobs)
        if len(group_keys) > 1 or any(k[0] == "fixed_ratio" for k in group_keys):
            return self._start_grouped(jobs)
        g = self._selected_gpu()
        if not g:
            messagebox.showwarning(APP_TITLE, "Pick a GPU (press ↻ to load the list).")
            return
        import video_estimate as ve
        # Feasibility guard (#7): a low-VRAM pod can't reach every target. Refuse if NOTHING
        # in the queue fits it; otherwise run only the feasible jobs (the rest stay pending
        # for a bigger card) and estimate on those.
        max_mp, feasible, infeasible_n = self._queue_feasibility(jobs, g)
        if not feasible:
            messagebox.showwarning(
                APP_TITLE,
                f"None of the {len(jobs)} queued video(s) fit {g['name']} "
                f"({g.get('memory_gb', '?')} GB): every target exceeds its VRAM. Pick a "
                "larger GPU, or lower the targets (grayed rows can't run on this card).")
            return
        est = ve.estimate_queue(feasible, g.get("id") or g.get("name"), g.get("price"),
                                self._spin_up(), conn=self._conn())
        if CFG.get("video", {}).get("confirm_before_rent", True):
            cost = f"${est['cost']:.2f}" if est else "?"
            dur = ve.fmt_duration(est["duration_seconds"]) if est else "?"
            skip = (f"\n\n{infeasible_n} video(s) exceed this GPU and will be skipped "
                    "(left in the queue for a larger card)." if infeasible_n else "")
            if not messagebox.askyesno(
                    APP_TITLE,
                    f"Rent {g['name']} (${g.get('price', 0):.2f}/h) and upscale "
                    f"{len(feasible)} job(s)?\n\nEstimated: {dur}, {cost}.{skip}\n\n"
                    "A billed pod is created and torn down when done."):
                return
        # Pass ONLY the selected GPU — never silently fall back to a different GPU
        # TYPE. If it can't be deployed (sold out / unavailable by start time), the
        # run fails with a clear message and the user refreshes (↻) and picks
        # another card themselves.
        env = {}
        if g.get("id"):
            env["IMGTBX_GPU_OVERRIDE"] = g["id"]
        # Hand the queue's cost estimate to the funds safety-net so it can refuse a
        # run that would drop the balance below the floor before renting a pod (#1).
        if est and est.get("cost"):
            env["IMGTBX_RUN_ESTIMATE"] = f"{est['cost']:.4f}"
        # Defer (not fail) any target this card can't reach (#7).
        if max_mp:
            env["IMGTBX_MAX_OUTPUT_MP"] = f"{max_mp:.4f}"
        # Self-healing (#6): arm the auto-resume supervisor for this run only.
        if self.auto_resume_var.get():
            env["IMGTBX_AUTO_RESUME"] = "1"
        self._run_gpu = g.get("id") or g.get("name")     # for the time-based estimate
        self._begin_run(sum(j["frames"] for j in feasible))
        self._launch("batch_video_upscale.py", [self._src_root, self._out_root], env)

    def _start_grouped(self, jobs):
        """Grouped multi-pod Start (18): the queue mixes (engine, gpu) groups. Reorder the
        queue so each group is contiguous, PERSIST + show that order (the user sees the run
        order), estimate/confirm across all groups, then launch. The runner reads the per-item
        gpu from the DB and deploys one pod per group, one at a time (the pendulum picks the
        next by live stock). Each job was prepared under its own card, so no feasibility
        deferral is needed here."""
        import batch_video_upscale as bv
        import video_estimate as ve
        conn = self._conn()
        # Rank groups cheapest-first using the live prices from the GPU picker list; a card with
        # no known price sorts last so a real quote always wins.
        price = {c.get("id"): c.get("price") for c in self._gpu_choices if c.get("id")}

        def rank(key):
            p = price.get(key[1])
            return (p if isinstance(p, (int, float)) else 9e9, key[1] or "")

        ordered = bv.group_queue_order(jobs, group_rank=rank)
        # Persist the grouped order so the runner walks it AND the queue tree shows it.
        for pos, jb in enumerate(ordered):
            bv.db.set_queue_order(conn, self._root_id, jb["rel"], jb["target"], pos,
                                  clip_id=jb.get("clip_id", 0))
        self._load_queue()                     # renumbers + regroups the queue tree (visible)

        # Per-group estimate: each group's jobs on its own card's price; sum for the total.
        groups = bv.distinct_group_keys(ordered)
        total_cost = 0.0
        total_secs = 0.0
        cost_known = True
        lines = []
        for key in groups:
            gjobs = [jb for jb in ordered if bv.job_group_key(jb) == key]
            gid = key[1]
            gprice = price.get(gid)
            est = ve.estimate_queue(gjobs, gid, gprice, self._spin_up(), conn=conn)
            cost = est.get("cost") if est else None
            if cost is None:
                cost_known = False
            else:
                total_cost += cost
            if est and est.get("duration_seconds"):
                total_secs += est["duration_seconds"]
            eng_lbl = "Real-ESRGAN" if key[0] == "fixed_ratio" else "SeedVR2"
            gname = _short_gpu(gid) or "the selected GPU"
            pstr = f"${gprice:.2f}/h" if isinstance(gprice, (int, float)) else "price ?"
            lines.append(f"  {eng_lbl} on {gname} ({pstr}): {len(gjobs)} job(s)")

        if CFG.get("video", {}).get("confirm_before_rent", True):
            cost_s = f"${total_cost:.2f}" if cost_known else "?"
            dur_s = ve.fmt_duration(total_secs) if total_secs else "?"
            body = ("Grouped run: one rented pod per GPU group, one at a time (cheapest "
                    "first). Only one pod is ever billed at once.\n\n"
                    + "\n".join(lines)
                    + f"\n\nEstimated total: {dur_s}, {cost_s}.\n\n"
                    "Each group's pod is created and torn down when its jobs finish.")
            if not messagebox.askyesno(APP_TITLE, body):
                return

        env = {}
        if cost_known and total_cost:
            env["IMGTBX_RUN_ESTIMATE"] = f"{total_cost:.4f}"   # funds floor spans the whole run
        if self.auto_resume_var.get():
            env["IMGTBX_AUTO_RESUME"] = "1"
        # A mixed-GPU run has no single card; the per-segment time estimate falls back to a
        # generic rate (the runner emits the live pod + $/h per group as each one deploys).
        self._run_gpu = None
        self._begin_run(sum(j["frames"] for j in jobs))
        self._launch("batch_video_upscale.py", [self._src_root, self._out_root], env)

    def _start_local(self, jobs):
        """Start a LOCAL run (#7): the SeedVR2 work runs on this machine's GPU, no pod, no
        cost, no GPU-override/auto-resume/funds plumbing. The runner picks the batch with
        the predictive VRAM sizer and guards a degrading GPU with the thrash watchdog; if
        the card can't do a target it OOM-recovers to a smaller window or stops loudly."""
        g = self._selected_gpu()
        if not g:
            messagebox.showwarning(
                APP_TITLE, "No local NVIDIA GPU detected (press ↻ to re-check).")
            return
        # Feasibility guard (#7): the target combobox already filters to this card, but the
        # queue can hold a job prepared under a different card / before a benchmark. Refuse
        # if nothing fits; otherwise run only the feasible jobs.
        max_mp, feasible, infeasible_n = self._queue_feasibility(jobs, g)
        if not feasible:
            messagebox.showwarning(
                APP_TITLE,
                f"None of the {len(jobs)} queued video(s) fit {g['name']} "
                f"({g.get('memory_gb', '?')} GB): every target exceeds what it can upscale "
                "to. Lower the targets (grayed rows can't run on this card).")
            return
        if CFG.get("video", {}).get("confirm_before_local", True):
            skip = (f"\n\n{infeasible_n} video(s) exceed this GPU and will be skipped."
                    if infeasible_n else "")
            if not messagebox.askyesno(
                    APP_TITLE,
                    f"Upscale {len(feasible)} job(s) on {g['name']} ({g.get('memory_gb', '?')} GB)?{skip}\n\n"
                    "For best results, close all non-essential applications and reduce active "
                    "machine usage to a minimum (other apps holding VRAM can slow the run or "
                    "force a smaller batch).\n\n"
                    "This runs on your own GPU (no cost). It can be slow, and a long GPU "
                    "session may degrade (the run stops loudly if it does; reboot and re-run "
                    "to continue)."):
                return
        env = {}
        if max_mp:                                       # defer (not fail) unreachable targets
            env["IMGTBX_MAX_OUTPUT_MP"] = f"{max_mp:.4f}"
        self._run_gpu = g.get("id") or g.get("name")
        self._begin_run(sum(j.get("frames") or 0 for j in feasible),
                        starting_msg="Starting local upscale …")
        self._launch("batch_video_upscale.py",
                     [self._src_root, self._out_root, "--local"], env)
        if self.proc is not None:            # launch succeeded (else _end_run already ran)
            self._start_local_telemetry()

    def _begin_run(self, total_frames, starting_msg="Starting pod …"):
        self._run_total = total_frames
        self._run_done = 0
        self._cur_seg_frames = self._cur_seg_done = 0
        self._run_start = time.time()
        self._rate = None
        # Time-based fallback state: the worker often can't report within-segment
        # frame progress (SeedVR2's bar counts batches, not frames), so we estimate
        # the running segment's progress from elapsed-vs-expected time until (and
        # unless) real frame counts arrive. See _run_tick.
        self._seg_start = None        # when the current segment began processing
        self._seg_expected = None     # estimated seconds for the current segment
        self._seg_has_frames = False  # worker reported real frames_processed for it
        self._cur_status = ""         # base status line the tick appends ETA to
        self._live_spf = None         # live seconds/frame the pod measures per DiT batch
        self._seg_total_chunks = 1    # streaming chunks in the segment (caps the pre-report bar)
        self._last_fp_time = None     # wall-clock of the last real frames_processed report
        self._bar_frac = 0.0          # last painted bar fraction (kept MONOTONIC)
        self._eta_finish = None       # projected finish time; refreshed only when progress advances
        self._eta_done = 0            # done_now at the last ETA refresh (so a stall can't inflate it)
        self.progress.set(0)
        self.start_btn.configure(state="disabled")
        self.auto_resume_chk.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.app.refresh_benchmark_lock()             # lock the GPU benchmark for this run
        self.status_var.set(starting_msg)
        self.console.clear()
        self.app.taskbar_state("indeterminate")
        if self._run_tick_job is None:
            self._run_tick_job = self.after(1000, self._run_tick)

    def _launch(self, script, args, extra_env=None):
        cmd = [PYTHON_EXE, "-u", os.path.join(SCRIPT_DIR, script)] + list(args)
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"
        if extra_env:
            env.update(extra_env)
        try:
            self.proc = subprocess.Popen(
                cmd, cwd=APP_ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, creationflags=CREATE_NO_WINDOW, env=env)
        except Exception as exc:                         # noqa: BLE001
            messagebox.showerror(APP_TITLE, f"Could not start the runner:\n{exc}")
            self._end_run()
            return
        self._hold = ""
        self._marker_buf = None
        # Exclusivity: lock the other tabs now that proc is live (a local run owns the
        # GPU; _begin_run ran before proc existed, so it can't do this itself).
        self.app.refresh_tab_exclusivity()
        threading.Thread(target=self._pump, daemon=True).start()
        self.after(50, self._poll)

    @property
    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def send(self, line):
        """Send a control line to the runner's stdin (App._on_close uses this)."""
        if self.proc and self.proc.stdin:
            try:
                self.proc.stdin.write((line + "\n").encode("utf-8"))
                self.proc.stdin.flush()
            except Exception:
                pass

    def terminate(self):
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.kill()
            except Exception:
                pass

    def _stop(self):
        if self.running:
            self.send("q")
            self.status_var.set("Stopping (aborting the segment, tearing down the pod) …")
        self.stop_btn.configure(state="disabled")

    def _pump(self):
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        stream = self.proc.stdout
        while True:
            chunk = stream.read1(4096)
            if not chunk:
                break
            self._queue_io.put(decoder.decode(chunk))
        self._queue_io.put(None)

    def _poll(self):
        finished = False
        while True:
            try:
                item = self._queue_io.get_nowait()
            except queue.Empty:
                break
            if item is None:
                finished = True
                break
            self.console.feed(self._filter_markers(item))
        if finished:
            code = self.proc.wait() if self.proc else 0
            self.proc = None
            self.on_exit(code)
        elif self.proc is not None:
            self.after(50, self._poll)

    def _filter_markers(self, text):
        """Strip @@TBX@@KIND|payload lines and dispatch them; same parser shape as
        ToolTab (markers can arrive mid-line from a background thread)."""
        out = []
        data = self._hold + text
        self._hold = ""
        pos, n = 0, len(data)
        while pos < n:
            if self._marker_buf is not None:
                nl = data.find("\n", pos)
                if nl < 0:
                    self._marker_buf += data[pos:]
                    pos = n
                else:
                    self._marker_buf += data[pos:nl]
                    self._on_marker(self._marker_buf)
                    self._marker_buf = None
                    pos = nl + 1
                continue
            idx = data.find(GUI_MARKER, pos)
            if idx < 0:
                hold = ""
                for k in range(len(GUI_MARKER) - 1, 0, -1):
                    if data.endswith(GUI_MARKER[:k]):
                        hold = GUI_MARKER[:k]
                        break
                out.append(data[pos:n - len(hold)] if hold else data[pos:])
                self._hold = hold
                pos = n
            else:
                out.append(data[pos:idx])
                self._marker_buf = ""
                pos = idx + len(GUI_MARKER)
        return "".join(out)

    def _on_marker(self, content):
        kind, _, payload = content.strip().partition("|")
        try:
            data = json.loads(payload) if payload else None
        except ValueError:
            data = None
        self._handle_event(kind, data, payload)

    def _handle_event(self, kind, data, payload):
        if kind == "POD":
            # batch_video_upscale's gui_event JSON-encodes its payload, so the
            # pod id arrives as `data` (the decoded string), not the raw payload.
            self.active_pod_id = data or None
            self.app.notify_active_pods_changed()
        elif kind == "RCOST" and data is not None:
            # The deployed pod's real billed rate ($/h): arms the live accrued-cost
            # readout in _run_tick so a paid, multi-hour run isn't flying blind.
            try:
                self._remote_rate = float(data)
            except (TypeError, ValueError):
                self._remote_rate = None
        elif kind == "VIDEO" and data:
            self._cur_rel = data.get("rel")
            self._cur_target = data.get("target")
            self._cur_w = data.get("width")          # source size, for the
            self._cur_h = data.get("height")         # aspect-correct time estimate
            self._cur_seg_frames = self._cur_seg_done = 0
            self._cur_status = (f"Upscaling {os.path.basename(data.get('rel',''))} "
                                f"→ {data.get('target')} "
                                f"[{data.get('index')}/{data.get('total')}]")
            self.status_var.set(self._cur_status)
        elif kind == "SEGMENT" and data:
            seg_frames = data.get("seg_frames") or 0
            fp = data.get("frames_processed")
            state = data.get("state")
            self._cur_seg_frames = seg_frames
            self._live_spf = data.get("live_spf")   # pod's measured s/frame (DiT phase)
            if data.get("total_chunks"):
                self._seg_total_chunks = int(data["total_chunks"])
            if state == "running":
                # "running" arrives on EVERY status poll (~5 s), not just once. Anchor
                # the time-based estimate the FIRST time only (_seg_start is None),
                # else each poll would reset the clock -> the bar sticks near 0 % and
                # the elapsed counter loops back to 0 every poll.
                if self._seg_start is None:
                    self._seg_start = time.time()
                    self._seg_has_frames = False
                    self._cur_seg_done = 0
                    import video_estimate as ve
                    self._seg_expected = ve.estimate_job(
                        seg_frames, self._cur_target, getattr(self, "_run_gpu", None),
                        conn=self._conn(), src_w=getattr(self, "_cur_w", None),
                        src_h=getattr(self, "_cur_h", None))
                if fp is not None:               # real within-segment frames, if any
                    nd = min(fp, seg_frames)
                    if nd != self._cur_seg_done:  # only restart the interp clock on a CHANGE
                        self._last_fp_time = time.time()  # (polls repeat the same fp otherwise)
                    self._cur_seg_done = nd
                    self._seg_has_frames = True  # real data overrides the clock
            elif state == "done":
                self._run_done += seg_frames
                self._cur_seg_done = 0
                self._seg_start = None
                self._seg_has_frames = False
                self._live_spf = None
                self._last_fp_time = None
            elif fp is not None:
                nd = min(fp, seg_frames)
                if nd != self._cur_seg_done:      # only restart the interp clock on a CHANGE
                    self._last_fp_time = time.time()
                self._cur_seg_done = nd
                self._seg_has_frames = True      # real data: it overrides the clock
            # _run_tick (1 s) owns the bar via _paint_bar; repaint here too on a real
            # frame count / 'done' so the bar reacts immediately to new data.
            self._paint_bar("seg")
        elif kind == "VTOTAL" and data is not None:
            # The runner re-reads the live queue each job and sends the current total
            # frame count, so the progress denominator tracks mid-run queue edits.
            self._run_total = int(data)
        elif kind == "VRESULT" and data:
            self._load_queue()                           # done/failed leaves the queue
        elif kind == "RTELEM" and data:
            self.app.apply_remote_telemetry(self, data)

    def _paint_bar(self, src="tick"):
        """Single owner of the progress bar. Computes a frames-done estimate (real
        within-segment frames anchored + live-s/frame smoothing, or a capped time
        estimate before the first report) and paints it MONOTONICALLY, so the bar never
        jumps backward (the old rush-to-100 %-then-snap) and never freezes mid-chunk."""
        if self._run_total <= 0:
            return
        done = _video_bar_done(
            self._run_done, self._cur_seg_frames, self._cur_seg_done,
            self._last_fp_time, time.time(), self._live_spf,
            self._seg_start, self._seg_expected, self._seg_total_chunks,
            self._seg_has_frames)
        # Cap BELOW 100 % while the run is live: the frame count can saturate (or the
        # denominator can undershoot) well before the pod finishes decoding/encoding the
        # last segment and the local reassemble/mux/drift tail runs — a 19-min stretch
        # on a big segment. Pinning at 100 % then reads as "done but frozen" (a
        # non-technical user may think it locked and Stop it). Reserve the top ~1 % for
        # that tail; on_exit(0) snaps it to a true 100 % only when the run actually ends.
        raw = min(0.99, done / self._run_total)
        prev = getattr(self, "_bar_frac", 0.0)
        self._bar_frac = max(prev, raw)
        self.progress.set(100.0 * self._bar_frac)
        self.app.taskbar_progress(int(self._bar_frac * self._run_total),
                                  max(1, self._run_total))
        if self._bar_frac != prev:
            self._dbg_progress(src, raw, done)

    def _dbg_progress(self, src, raw, done):
        """TEMPORARY: log each bar value change with the inputs behind it, so the bar
        can be validated over a long run. Routed by _PROGRESS_DEBUG_MODE (window/file/
        off). `src` = who painted ('tick' = 1 s heartbeat, 'seg' = a SEGMENT event);
        `raw` = the pre-monotonic fraction (a raw < the painted bar means a value was
        clamped, i.e. it wanted to go backward)."""
        mode = _PROGRESS_DEBUG_MODE
        if not mode:
            return
        try:
            age = (f"{time.time() - self._last_fp_time:.1f}"
                   if self._last_fp_time else "-")
            line = (f"[progress {_log_hms()}] bar={100 * self._bar_frac:6.2f}% "
                    f"(raw {100 * raw:6.2f}%) src={src} done={done:.1f} "
                    f"run_done={self._run_done} "
                    f"seg_done={self._cur_seg_done}/{self._cur_seg_frames} "
                    f"total={self._run_total} has_frames={self._seg_has_frames} "
                    f"live_spf={self._live_spf} fp_age={age}s "
                    f"chunks={self._seg_total_chunks}\n")
            if mode == "file":
                with open(os.path.join(APP_ROOT, "logs", "video_progress_debug.log"),
                          "a", encoding="utf-8") as f:
                    f.write(line)
            else:
                self.console.feed(line)
        except Exception:
            pass

    def _run_tick(self):
        """1 s heartbeat during a run. Repaints the bar every tick via _paint_bar (the
        single bar owner: real frames when the pod reports them, else a capped time
        estimate) and refreshes the ETA + the pod's live s/frame. With no benchmark for
        the card we can't estimate, so we show an elapsed counter to prove it is alive."""
        if self.proc is None:
            self._run_tick_job = None
            return
        import video_estimate as ve
        now = time.time()
        elapsed = now - (self._run_start or now)
        done_now = self._run_done + self._cur_seg_done
        # Project the finish time, refreshed ONLY when progress actually advances. Between
        # advances done_now is frozen while elapsed climbs, so recomputing the rate every
        # tick would inflate the ETA (the old sawtooth: creep up 1-2 s/tick, drop on report).
        # Instead we hold the projection and let the display count DOWN to it.
        if done_now > self._eta_done and self._run_total > 0 and elapsed > 0:
            self._rate = elapsed / done_now           # running-average seconds/frame
            self._eta_finish = (self._run_start or now) + self._rate * self._run_total
            self._eta_done = done_now
        self._paint_bar("tick")
        base = getattr(self, "_cur_status", "") or ""
        tail = ""
        if self._eta_finish is not None and done_now > 0 and self._eta_finish - now > 1:
            tail = f" · ETA {ve.fmt_duration(self._eta_finish - now)}"
        elif self._seg_start is not None and done_now > 0:
            # ETA elapsed but the segment is still running: the frame count has
            # saturated while the pod decodes/encodes the tail (and the local
            # reassemble/mux/drift step follows). Show a LIVE "finishing up" counter
            # instead of a frozen "ETA 0s" so the run never looks stuck.
            tail = f" · finishing up ({ve.fmt_duration(now - self._seg_start)} on this segment)"
        elif self._seg_start is not None and self._seg_expected and self._run_total > 0:
            # No real frames yet: a rough estimate from the benchmark, counted off the bar.
            per_frame = self._seg_expected / self._cur_seg_frames if self._cur_seg_frames else 0
            if per_frame:
                remaining = max(0.0, self._run_total * (1.0 - self._bar_frac))
                tail = f" · ETA {ve.fmt_duration(remaining * per_frame)}"
        elif self._seg_start is not None:
            # No benchmark for this card: prove liveness with an elapsed counter.
            tail = f" · running {ve.fmt_duration(now - self._seg_start)}"
        # The pod measures real seconds/frame per DiT batch (first batch is high while
        # torch.compile warms up, then it drops): show it live so a benchmarking run
        # isn't flying blind on an estimate. See worker _HeartbeatTee / SEGMENT events.
        if self._live_spf:
            tail += f" · {self._live_spf:.1f} s/frame (live)"
        # Live accrued cost, once the pod's real billed rate is known (remote run):
        # a paid, multi-hour video run shouldn't fly blind on the up-front estimate.
        if self._remote_rate and self._run_start is not None:
            spent = self._remote_rate * (now - self._run_start) / 3600.0
            tail += f" · ${spent:.2f} so far"
        if base:
            self.status_var.set(base + tail)
        # NOTE: the per-minute "Processing" heartbeat now lives in the RUNNER
        # (batch_video_upscale._progress), so it reaches BOTH the console and the
        # on-disk log file. It used to be fed here (console only), which left a long
        # segment with no on-disk trace. See that runner code.
        self._run_tick_job = self.after(1000, self._run_tick)

    # ── local-run telemetry (#7 local GPU; usage graphs #9) ──────────────────
    LOCAL_TELEM_MS = 5000     # sampling cadence while a LOCAL run works the GPU

    def _start_local_telemetry(self):
        """Sample this machine while a LOCAL run works the GPU, so the Local Unit
        row stays live and the run feeds a per-run usage graph (#9). A remote run
        uses the pod's own remote row/history instead, and the app's 60 s idle
        sampler pauses while any run is active, so a local run must drive its own
        (denser) sampling here."""
        self.app.telemetry_history_start("local",
                                         title=f"Local system - {self.tool_name}")
        self._local_telem_tick()

    def _local_telem_tick(self):
        if self.proc is None:                 # run ended: stop sampling
            self._local_telem_job = None
            return
        self.app.sample_telemetry()
        self._local_telem_job = self.after(self.LOCAL_TELEM_MS,
                                           self._local_telem_tick)

    def _stop_local_telemetry(self):
        """Cancel the local sampler and seal the run's history (a safe no-op after
        a remote run, which never started a 'local' history)."""
        if self._local_telem_job is not None:
            try:
                self.after_cancel(self._local_telem_job)
            except Exception:
                pass
            self._local_telem_job = None
        self.app.telemetry_history_seal("local")

    def on_exit(self, code):
        self._end_run()
        self.app.taskbar_clear()
        self.app.flash_attention()
        self._load_queue()
        if code == 0:
            # The bar is held at <=99 % during the run (see _paint_bar); a clean finish
            # is the only place it reaches a true 100 %.
            self._bar_frac = 1.0
            self.progress.set(100.0)
            self.status_var.set("Run finished.")
        else:
            # A common cause is the picked GPU selling out between picker-refresh
            # and Start (we never substitute a different card). Point the user at
            # the manual re-pick rather than guessing for them.
            self.status_var.set("Run failed (see View log). If the GPU is no longer "
                                "available, press ↻ to refresh the list and pick another.")

    def _end_run(self):
        self.proc = None
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        # Auto-resume is pod-only: keep it greyed in Local mode, re-enable it in Remote.
        self._apply_mode_ui()
        self.app.refresh_benchmark_lock()             # run over: re-enable the benchmark (if idle)
        self.app.refresh_tab_exclusivity()            # run over: re-enable the other tabs
        # The run is over: its pod (if any) is no longer protected from terminate.
        # Cleared GUI-side (not via a runner event) so a hard-killed runner still
        # releases the protection. Mirrors ToolTab.on_exit.
        if self.active_pod_id is not None:
            self.active_pod_id = None
            self.app.notify_active_pods_changed()
        self._remote_rate = None
        if self._run_tick_job is not None:
            self.after_cancel(self._run_tick_job)
            self._run_tick_job = None
        # Stop the local-run telemetry sampler and freeze its usage graph (#9). Safe
        # for a remote run too (it never started a 'local' history).
        self._stop_local_telemetry()
        # The remote-pod telemetry row only makes sense during a remote run; hide it and
        # zero the MQTT system/remote/* topics so a terminated pod leaves no stale values.
        self.app.clear_remote_telemetry(self)

    def _view_log(self):
        self.app.show_log(self.console, f"{APP_TITLE} — Video Upscaler output")

    def on_exit_app(self):
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
            except Exception:
                pass
