"""
gui/tab_conciliate.py
---------------------
The Conciliation tab.
"""

import os
import json
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import mqtt_publisher
import taskbar_progress
from gui.common import APP_TITLE, get_default_folder, set_default_folder
from gui.widgets import Tooltip, ProgressBar, TelemetryRow
from gui.tooltab import ToolTab


CONCILIATE_MODES = [
    ("Archive originals", "archive"),
    ("Delete originals",  "delete"),
]


class ConciliateTab(ToolTab):
    """
    Replace original photos and videos with their processed (upscaled, optionally
    tagged & renamed) counterparts. Two phases: Scan/Preview builds a per-folder
    plan and touches nothing; Run performs the chosen archive/delete operation.
    Drives conciliate.py as a subprocess, reusing ToolTab's stdin/marker plumbing.
    """

    def __init__(self, notebook, app):
        super().__init__(notebook, app)
        self.tool_name      = "Conciliation"
        self.mqtt_task_name = "conciliating"
        self.telemetry_interval_ms = 30000      # sample every 30 s while running
        self.orig_var  = tk.StringVar()
        self.proc_var  = tk.StringVar()
        self.mode_var  = tk.StringVar(value=CONCILIATE_MODES[0][0])
        self._phase    = "idle"     # idle | scanning | preview | running
        self._plan_replaced = 0
        self._result   = None       # last DONE summary dict
        self._build()

        # Restore pinned default folders from config.json
        self.restore_defaults_if_empty()
        self.orig_var.trace_add("write", lambda *_: self._refresh_buttons())
        self.proc_var.trace_add("write", lambda *_: self._refresh_buttons())
        self._refresh_buttons()

    def restore_defaults_if_empty(self):
        if not self.orig_var.get().strip():
            orig_default = get_default_folder("conciliate_original")
            if orig_default:
                self.orig_var.set(orig_default)
        if not self.proc_var.get().strip():
            proc_default = get_default_folder("conciliate_processed")
            if proc_default:
                self.proc_var.set(proc_default)

    # ── construction ──────────────────────────────────────────────────────────

    def _build(self):
        self.columnconfigure(1, weight=1)

        ttk.Label(self, text="Original Files:").grid(row=0, column=0, sticky="w", pady=3)
        ttk.Entry(self, textvariable=self.orig_var).grid(row=0, column=1, sticky="ew", padx=6, pady=3)
        ttk.Button(self, text="Browse…", command=self._pick_orig).grid(row=0, column=2, pady=3)
        self.save_orig_btn = ttk.Button(
            self, text="Save as Default", command=lambda: self._save_default("orig"))
        self.save_orig_btn.grid(row=0, column=3, sticky="ew", padx=(8, 0), pady=3)

        ttk.Label(self, text="Processed Files:").grid(row=1, column=0, sticky="w", pady=3)
        ttk.Entry(self, textvariable=self.proc_var).grid(row=1, column=1, sticky="ew", padx=6, pady=3)
        ttk.Button(self, text="Browse…", command=self._pick_proc).grid(row=1, column=2, pady=3)
        self.save_proc_btn = ttk.Button(
            self, text="Save as Default", command=lambda: self._save_default("proc"))
        self.save_proc_btn.grid(row=1, column=3, sticky="ew", padx=(8, 0), pady=3)

        # Operation picklist
        opf = ttk.Frame(self)
        opf.grid(row=2, column=0, columnspan=4, sticky="w", pady=(8, 0))
        ttk.Label(opf, text="When I run this:").pack(side="left", padx=(0, 6))
        self.mode_cmb = ttk.Combobox(opf, textvariable=self.mode_var, state="readonly",
                                     values=[m[0] for m in CONCILIATE_MODES], width=20)
        self.mode_cmb.pack(side="left")
        Tooltip(self.mode_cmb,
                "Archive: move each matched original into __Archive__, then move its\n"
                "processed version into the original folder.\n"
                "Delete: permanently remove each matched original instead of archiving.")
        self.mode_hint = ttk.Label(opf, text="", foreground="#666")
        self.mode_hint.pack(side="left", padx=(12, 0))
        self.mode_var.trace_add("write", lambda *_: self._update_mode_hint())
        self._update_mode_hint()

        # Action buttons
        btns = ttk.Frame(self)
        btns.grid(row=3, column=0, columnspan=4, sticky="w", pady=(10, 0))
        self.scan_btn = ttk.Button(btns, text="Scan / Preview", command=self._scan)
        self.run_btn  = ttk.Button(btns, text="Run", command=self._run, state="disabled")
        self.stop_btn = ttk.Button(btns, text="Stop", command=self._stop, state="disabled")
        self.open_btn = ttk.Button(btns, text="Open original folder", command=self._open_orig)
        self.viewlog_btn = ttk.Button(btns, text="View log", command=self._view_log, state="disabled")
        for b in (self.scan_btn, self.run_btn, self.stop_btn, self.open_btn, self.viewlog_btn):
            b.pack(side="left", padx=(0, 6))

        # Status + progress
        sf = ttk.Frame(self)
        sf.grid(row=4, column=0, columnspan=4, sticky="ew", pady=(10, 2))
        sf.columnconfigure(0, weight=1)
        self.status_lbl = ttk.Label(sf, text="Choose both folders, then Scan / Preview.",
                                    anchor="w")
        self.status_lbl.grid(row=0, column=0, sticky="ew")
        self.progress = ProgressBar(sf, width=200)
        self.progress.grid(row=0, column=1, sticky="e", padx=(8, 0))
        self.progress.grid_remove()

        # Per-folder preview table
        body = ttk.LabelFrame(self, text=" Preview ", padding=6)
        body.grid(row=5, column=0, columnspan=4, sticky="nsew", pady=(6, 0))
        self.rowconfigure(5, weight=1)
        body.rowconfigure(0, weight=1)
        body.columnconfigure(0, weight=1)

        cols = ("replaced", "skipped", "kept")
        self.tree = ttk.Treeview(body, columns=cols, show="tree headings", height=8)
        self.tree.heading("#0", text="Folder")
        self.tree.heading("replaced", text="Replaced")
        self.tree.heading("skipped", text="No match (kept)")
        self.tree.heading("kept", text="Non-media (kept)")
        self.tree.column("#0", width=320, anchor="w", stretch=True)
        for c in cols:
            self.tree.column(c, width=120, anchor="center", stretch=False)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb = ttk.Scrollbar(body, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        vsb.grid(row=0, column=1, sticky="ns")

        # Compact system-telemetry row, below the preview table (Feature #3a).
        self.telemetry_row = TelemetryRow(self)
        self.telemetry_row.grid(row=6, column=0, columnspan=4,
                                sticky="ew", pady=(4, 0))

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

    # ── GUI events from conciliate.py ─────────────────────────────────────────

    def _handle_event(self, kind, payload):
        if kind == "STATUS":
            self.status_lbl.configure(text=payload)
            if self.running:
                self.app.mqtt_publish({mqtt_publisher.TASK_DETAILS_TOPIC: payload})
        elif kind == "FOLDER":
            try:
                d = json.loads(payload)
            except ValueError:
                return
            label = d.get("dir") or "."
            if label == ".":
                label = "(root)"
            self.tree.insert("", "end", text=label,
                             values=(d.get("replaced", 0), d.get("skipped", 0), d.get("kept", 0)))
        elif kind == "PLAN":
            try:
                d = json.loads(payload)
            except ValueError:
                return
            self._plan_replaced = int(d.get("replaced", 0))
            self._phase = "preview"
            if self._plan_replaced > 0:
                self.run_btn.configure(state="normal")
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
        elif kind == "DONE":
            self._last_done = payload     # for MQTT last_run (published on exit)
            try:
                self._result = json.loads(payload)
            except ValueError:
                self._result = None
            self.app.flash_attention()    # catch the eye when the run finishes
        else:
            super()._handle_event(kind, payload)

    # ── default-folder buttons ────────────────────────────────────────────────

    def _refresh_buttons(self):
        ready = bool(self.orig_var.get().strip()) and bool(self.proc_var.get().strip())
        if not self.running:
            self.scan_btn.configure(state="normal" if ready else "disabled")
        self.save_orig_btn.configure(
            state="normal" if os.path.isdir(self.orig_var.get().strip() or "") else "disabled")
        self.save_proc_btn.configure(
            state="normal" if os.path.isdir(self.proc_var.get().strip() or "") else "disabled")
        self.open_btn.configure(
            state="normal" if self.orig_var.get().strip() else "disabled")

    def _save_default(self, which):
        if which == "orig":
            if not os.path.isdir(self.orig_var.get().strip() or ""):
                return
            set_default_folder("conciliate_original", self.orig_var.get().strip())
            self._flash_saved(self.save_orig_btn)
        else:
            if not os.path.isdir(self.proc_var.get().strip() or ""):
                return
            set_default_folder("conciliate_processed", self.proc_var.get().strip())
            self._flash_saved(self.save_proc_btn)
        self.app.sync_settings_defaults()

    def _flash_saved(self, btn):
        btn.configure(text="Saved ✓")
        self.after(1200, lambda: btn.configure(text="Save as Default"))

    def _update_mode_hint(self):
        if self._mode_code() == "delete":
            self.mode_hint.configure(
                text="Originals are permanently deleted (extra confirmation required).",
                foreground="#b3261e")
        else:
            self.mode_hint.configure(
                text="Originals are moved to an __Archive__ subfolder.", foreground="#666")

    def _mode_code(self):
        for label, code in CONCILIATE_MODES:
            if label == self.mode_var.get():
                return code
        return "archive"

    # ── actions ────────────────────────────────────────────────────────────────

    def _pick_orig(self):
        folder = filedialog.askdirectory(title="Choose the folder with the ORIGINAL files")
        if folder:
            self.orig_var.set(os.path.normpath(folder))

    def _pick_proc(self):
        folder = filedialog.askdirectory(title="Choose the folder with the PROCESSED files")
        if folder:
            self.proc_var.set(os.path.normpath(folder))

    def _open_orig(self):
        p = self.orig_var.get().strip()
        if p and os.path.isdir(p):
            os.startfile(p)
        else:
            messagebox.showinfo(APP_TITLE, "The original folder does not exist.")

    def _scan(self):
        if self.app.upscale_tab.running or self.app.tag_tab.running:
            messagebox.showinfo(
                APP_TITLE,
                "Please wait for the Batch Upscaler or Tag & Rename to finish "
                "before running a conciliation — they may be using the same folders.")
            return
        orig = self.orig_var.get().strip()
        proc = self.proc_var.get().strip()
        if not os.path.isdir(orig):
            messagebox.showwarning(APP_TITLE, "Please choose a valid Original Files folder.")
            return
        if not os.path.isdir(proc):
            messagebox.showwarning(APP_TITLE, "Please choose a valid Processed Files folder.")
            return
        if os.path.normcase(os.path.abspath(orig)) == os.path.normcase(os.path.abspath(proc)):
            messagebox.showwarning(APP_TITLE,
                                   "The Original and Processed folders must be different.")
            return

        self.tree.delete(*self.tree.get_children())
        self.progress.set(0)
        self.progress.grid_remove()
        self._plan_replaced = 0
        self._result = None
        self._reset_stream_state()
        self.status_lbl.configure(text="Scanning …")
        if self.launch("conciliate.py", [orig, proc, self._mode_code()]):
            self._phase = "scanning"
            self._set_running(True)

    def _run(self):
        n = self._plan_replaced
        if n <= 0:
            return
        mode = self._mode_code()
        if mode == "delete":
            if not messagebox.askyesno(
                    APP_TITLE,
                    f"DELETE {n} original file(s) and replace them with the processed "
                    f"versions?\n\nThe originals will NOT be archived."):
                return
            if not messagebox.askyesno(
                    APP_TITLE,
                    "Are you absolutely sure?\n\nDeleted originals cannot be recovered."):
                return
        else:
            if not messagebox.askyesno(
                    APP_TITLE,
                    f"Archive {n} original file(s) into '__Archive__' and move the "
                    f"processed versions into the original folder?"):
                return
        self.run_btn.configure(state="disabled")
        self._phase = "running"
        self.status_lbl.configure(text="Running …")
        self.send("run")

    def _stop(self):
        self.send("q")
        self.stop_btn.configure(state="disabled")
        self.run_btn.configure(state="disabled")
        self.status_lbl.configure(text="Stopping …")

    def _set_running(self, running):
        self.scan_btn.configure(state="disabled" if running else "normal")
        self.stop_btn.configure(state="normal" if running else "disabled")
        self.mode_cmb.configure(state="disabled" if running else "readonly")
        if not running:
            self.run_btn.configure(state="disabled")
        self._refresh_buttons()
        self.app.refresh_tab_exclusivity()

    def on_exit(self, code):
        self._set_running(False)
        self._phase = "idle"
        for delay in (250, 1500):
            self.after(delay, self._tick)
        if self._result is not None:
            r = self._result
            self.progress.set(100)
            removed = r.get("removed_dirs", 0)
            extra = f", {removed} empty folder(s) removed" if removed else ""
            self.status_lbl.configure(
                text=f"Done — {r.get('done', 0)} replaced, "
                     f"{r.get('conflicts', 0)} skipped (conflict), "
                     f"{r.get('errors', 0)} error(s){extra}.")
        elif self._plan_replaced and self._phase != "running" and code == 0:
            # Process exited after a preview without running (Stop during preview).
            self.status_lbl.configure(text="Stopped — nothing was changed.")
        elif code == 0:
            self.progress.grid_remove()
            self.status_lbl.configure(text="Preview complete. Nothing to conciliate.")
        else:
            self.status_lbl.configure(
                text=f"Stopped with an error (code {code}) — see the log.")
        # Conciliate uses its own status label (not status_var), so publish the
        # final details line explicitly before going idle.
        self.app.mqtt_publish({mqtt_publisher.TASK_DETAILS_TOPIC: self.status_lbl.cget("text")})
        super().on_exit(code)


# ─────────────────────────────────────────────
#  UPDATE DIALOG
# ─────────────────────────────────────────────
