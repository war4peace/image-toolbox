"""
gui/tab_upscale.py
------------------
The Batch Upscaler tab.
"""

import os
import json
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import ssh_setup
import taskbar_progress
from gui.common import APP_TITLE, CFG, get_default_folder, set_default_folder, get_install_mode
from gui.tooltab import ToolTab
from gui.widgets import Tooltip


class UpscaleTab(ToolTab):

    def __init__(self, notebook, app):
        super().__init__(notebook, app)
        self.tool_name      = "Batch Upscaler"
        self.mqtt_task_name = "upscaling"
        self.src_var      = tk.StringVar()
        self.out_var      = tk.StringVar()
        # Default the remote toggle ON for a Remote-only install (it's the only
        # way to upscale there); OFF for Local/Both.
        self.remote_var   = tk.BooleanVar(value=(get_install_mode() == "remote"))
        # Live GPU picker: upscaling needs a heavy card, so floor VRAM at 32 GB;
        # the persisted preference is runpod.gpu_type_id.
        self._gpu_min_vram = 32
        self._gpu_pref_key = "gpu_type_id"
        self._bench_task   = "upscale"   # $/100 readout: upscale benchmark rows
        self._paused      = False
        self._processing  = False    # True once the per-image phase started
        self._cancelled   = False    # user cancelled a preparation phase
        self._phase       = ""       # current phase text (from STATUS events)
        self._build()
        # Double-clicking a comparable (green) thumbnail opens the comparison
        # window. Only the Upscaler produces an original↔upscaled pair, so only
        # this tab wires the hook; the Tag tab's double-click just opens the file.
        self.strip.on_compare = lambda src, out: self.app.show_comparison(src, out)

        # Restore the pinned default folders from config.json
        self.restore_defaults_if_empty()
        self.src_var.trace_add("write", lambda *_: self._refresh_save_buttons())
        self.out_var.trace_add("write", lambda *_: self._refresh_save_buttons())
        self._refresh_save_buttons()

    def restore_defaults_if_empty(self):
        if not self.src_var.get().strip():
            src_default = get_default_folder("upscale_source")
            if src_default and os.path.isdir(src_default):
                self.src_var.set(src_default)
        if not self.out_var.get().strip():
            # An explicit output default wins; otherwise mirror it next to source.
            out_default = get_default_folder("upscale_output")
            if out_default:
                self.out_var.set(out_default)
            else:
                src_now = self.src_var.get().strip()
                if src_now and os.path.isdir(src_now):
                    self.out_var.set(os.path.join(src_now, "__upscaled__"))

    def _build(self):
        ttk.Label(self, text="Photo folder:").grid(row=0, column=0, sticky="w", pady=3)
        ttk.Entry(self, textvariable=self.src_var).grid(row=0, column=1, sticky="ew", padx=6, pady=3)
        self.browse_src_btn = ttk.Button(self, text="Browse…", command=self._pick_src)
        self.browse_src_btn.grid(row=0, column=2, pady=3)
        self.save_src_btn = ttk.Button(
            self, text="Save as Default", command=lambda: self._save_default("src"))
        self.save_src_btn.grid(row=0, column=3, sticky="ew", padx=(8, 0), pady=3)

        ttk.Label(self, text="Save upscaled to:").grid(row=1, column=0, sticky="w", pady=3)
        ttk.Entry(self, textvariable=self.out_var).grid(row=1, column=1, sticky="ew", padx=6, pady=3)
        self.browse_out_btn = ttk.Button(self, text="Browse…", command=self._pick_out)
        self.browse_out_btn.grid(row=1, column=2, pady=3)
        self.save_out_btn = ttk.Button(
            self, text="Save as Default", command=lambda: self._save_default("out"))
        self.save_out_btn.grid(row=1, column=3, sticky="ew", padx=(8, 0), pady=3)

        btns = ttk.Frame(self)
        btns.grid(row=2, column=0, columnspan=4, sticky="w", pady=(10, 0))
        self.start_btn = ttk.Button(btns, text="Start upscaling", command=self._start)
        self.pause_btn = ttk.Button(btns, text="Pause", command=self._pause_or_cancel, state="disabled")
        self.stop_btn  = ttk.Button(btns, text="Stop after current image", command=self._stop, state="disabled")
        self.open_btn  = ttk.Button(btns, text="Open output folder", command=self._open_out)
        self.viewlog_btn = ttk.Button(btns, text="View log", command=self._view_log, state="disabled")
        for b in (self.start_btn, self.pause_btn, self.stop_btn, self.open_btn, self.viewlog_btn):
            b.pack(side="left", padx=(0, 6))
        self._add_tooltips()

        self._build_remote_row(row=3)
        self._build_output_area(row=4)

    # ── Button hints ─────────────────────────────────────────────────────────

    # The middle button changes label with the run phase, so its hint follows
    # (see _sync_pause_tip). Keyed by the label the button is currently showing.
    PAUSE_TIPS = {
        "Cancel": ("Cancel the run before the per-image work starts. The current "
                   "step (scanning, cache check) is finished cleanly and what was "
                   "scanned so far is kept."),
        "Pause":  ("Pause after the current image and unload the AI models, "
                   "giving the graphics card back to you for other work. The "
                   "queue is kept, so Resume continues without re-scanning."),
        "Resume": ("Continue the paused run. The AI models reload first, so the "
                   "next image takes longer than the rest."),
    }

    def _add_tooltips(self):
        W = Tooltip.WRAP_NARROW
        Tooltip(self.browse_src_btn,
                "Pick the folder of photos to upscale. Sub-folders are included.",
                wraplength=W)
        Tooltip(self.browse_out_btn,
                "Pick where the upscaled copies go. The source folder's structure "
                "is mirrored there; your originals are never modified.",
                wraplength=W)
        Tooltip(self.save_src_btn,
                "Remember this photo folder as the default source, pre-filled "
                "every time the app starts.", wraplength=W)
        Tooltip(self.save_out_btn,
                "Remember this folder as the default destination for upscaled "
                "photos, pre-filled every time the app starts.", wraplength=W)
        Tooltip(self.start_btn,
                "Scan the photo folder and upscale every image that is below the "
                "Resolution Target. Your originals are never modified: the "
                "results are written to the output folder.", wraplength=W)
        self.pause_tip = Tooltip(self.pause_btn, self.PAUSE_TIPS["Pause"],
                                 wraplength=W)
        Tooltip(self.stop_btn,
                "End the run once the current image is finished. Whatever is "
                "left stays queued: starting again picks up where you stopped.",
                wraplength=W)
        Tooltip(self.open_btn,
                "Open the folder holding the upscaled copies in Windows Explorer.",
                wraplength=W)
        Tooltip(self.viewlog_btn,
                "Open the log window with this run's full output. Available once "
                "a run has started.", wraplength=W)

    def _sync_pause_tip(self):
        tip = self.PAUSE_TIPS.get(self.pause_btn.cget("text"))
        if tip:
            self.pause_tip.set_text(tip)

    # ── GUI events specific to the upscaler ─────────────────────────────────

    def _handle_event(self, kind, payload):
        if kind == "STATUS":
            self._phase = payload
            if not self._cancelled:
                self.status_var.set(payload)
            self.progress.set(0)
            processing = payload.startswith("Processing")
            if processing != self._processing:
                self._processing = processing
                if processing:                 # entering a processing pass
                    self.eta_var.set("calculating…")
                if self.running and not self._cancelled:
                    self.pause_btn.configure(
                        text="Pause" if processing else "Cancel")
                    self._sync_pause_tip()
        elif kind == "PROG":
            cur, _, tot = payload.partition("|")
            try:
                cur, tot = int(cur), int(tot)
            except ValueError:
                return
            if tot > 0:
                self.progress.set(cur * 100 / tot)
                self.app.taskbar_progress(cur, tot)
                if not self._cancelled:
                    self.status_var.set(f"{self._phase}  ({cur:,} / {tot:,})")
        else:
            super()._handle_event(kind, payload)

    def _on_image_started(self, path):
        """Sample telemetry 5 s after an image starts upscaling — past the
        load/ramp, so the reading reflects steady-state work, not the dip
        between images. A fresh image cancels any still-pending sample."""
        if self._telemetry_img_job is not None:
            try:
                self.after_cancel(self._telemetry_img_job)
            except Exception:
                pass
        self._telemetry_img_job = self.after(5000, self._sample_after_image)

    def _sample_after_image(self):
        self._telemetry_img_job = None
        self.app.sample_telemetry()

    # ── Default-folder buttons ───────────────────────────────────────────────

    def _src_valid(self):
        p = self.src_var.get().strip()
        return bool(p) and os.path.isdir(p)

    def _out_valid(self):
        p = self.out_var.get().strip()
        # The output folder is created on demand — accept it if it exists or
        # can be created inside an existing parent.
        return bool(p) and (os.path.isdir(p) or os.path.isdir(os.path.dirname(p)))

    def _refresh_save_buttons(self):
        self.save_src_btn.configure(state="normal" if self._src_valid() else "disabled")
        self.save_out_btn.configure(state="normal" if self._out_valid() else "disabled")
        # "Open output folder" is meaningless with no output path entered.
        self.open_btn.configure(
            state="normal" if self.out_var.get().strip() else "disabled")
        # No source folder, nothing to upscale — but never re-enable mid-run.
        if not self.running:
            self.start_btn.configure(
                state="normal" if self.src_var.get().strip() else "disabled")

    def _save_default(self, which):
        if which == "src":
            if not self._src_valid():
                return
            set_default_folder("upscale_source", self.src_var.get().strip())
            self._flash_saved(self.save_src_btn)
        else:
            if not self._out_valid():
                return
            set_default_folder("upscale_output", self.out_var.get().strip())
            self._flash_saved(self.save_out_btn)
        self.app.sync_settings_defaults()   # mirror into the Settings tab

    def _flash_saved(self, btn):
        btn.configure(text="Saved ✓")
        self.after(1200, lambda: btn.configure(text="Save as Default"))

    # ── Actions ──────────────────────────────────────────────────────────────

    def _pick_src(self):
        folder = filedialog.askdirectory(title="Choose the folder with photos to upscale")
        if folder:
            folder = os.path.normpath(folder)
            self.src_var.set(folder)
            out = self.out_var.get().strip()
            # Keep following the source unless the user chose a custom output
            if not out or os.path.basename(out) == "__upscaled__":
                self.out_var.set(os.path.join(folder, "__upscaled__"))

    def _pick_out(self):
        folder = filedialog.askdirectory(title="Choose where to save upscaled photos")
        if folder:
            self.out_var.set(os.path.normpath(folder))

    def _open_out(self):
        out = self.out_var.get().strip()
        if out and os.path.isdir(out):
            os.startfile(out)
        else:
            messagebox.showinfo(APP_TITLE, "The output folder does not exist yet.")

    def _start(self):
        src = self.src_var.get().strip()
        if not src or not os.path.isdir(src):
            messagebox.showwarning(APP_TITLE, "Please choose a valid photo folder first.")
            return
        out = self.out_var.get().strip() or os.path.join(src, "__upscaled__")
        self.out_var.set(out)

        remote = bool(self.remote_var.get())
        extra_env = None
        if remote:
            rp = CFG.get("runpod", {})
            if not rp.get("api_key") or not rp.get("network_volume_id"):
                messagebox.showwarning(
                    APP_TITLE,
                    "Remote upscaling needs a RunPod API key and a model volume.\n"
                    "Set them in the RunPod tab.")
                return
            # Zero-config SSH: make sure the app's key exists before we create a
            # pod (the pod trusts its public half via PUBLIC_KEY). Auto-generates
            # on first remote run so the user needn't have opened Settings.
            ok_ssh, ssh_info = ssh_setup.setup(
                os.path.expandvars(rp.get("ssh_key_path", "")) or None)
            if not ok_ssh:
                messagebox.showwarning(APP_TITLE, ssh_info.get("message", "SSH setup failed."))
                return
            gpu_chain = self._selected_gpu_chain()
            if gpu_chain == []:
                messagebox.showwarning(
                    APP_TITLE,
                    "No RunPod GPU with at least 32 GB of VRAM is available in your "
                    "volume's region right now.\n\nTry again in a while, or choose a "
                    "different region for your model volume in Settings. (Press ↻ to "
                    "re-check availability.)")
                return
            if not self.confirm_deadman_safety():
                return
            gpu_note = self._gpu_confirm_note(gpu_chain)
            if not messagebox.askyesno(
                    APP_TITLE,
                    "Run this batch on a rented RunPod GPU?\n\n"
                    "This creates a BILLED pod, streams each image to it, writes the "
                    "results here, and terminates the pod when the run ends. The pod "
                    "also self-terminates on an idle / max-runtime deadline as a "
                    f"safety net.{gpu_note}\n\nProceed?"):
                return
            extra_env = {"IMGTBX_UPSCALE_REMOTE": "1"}
            if gpu_chain:
                extra_env["IMGTBX_GPU_OVERRIDE"] = ",".join(gpu_chain)
        else:
            # Local run requested. A Remote-only install has no local engine
            # (torch + SeedVR2 weren't installed), so a local run would crash on
            # import — refuse it with a clear message instead.
            if get_install_mode() == "remote":
                messagebox.showwarning(
                    APP_TITLE,
                    "This is a Remote-only install — the local upscaling engine "
                    "(PyTorch + SeedVR2) isn't installed.\n\n"
                    "Tick 'Run on remote pod (RunPod)' to upscale on a rented GPU, "
                    "or reinstall and choose Local or Both to upscale on this PC.")
                return
            if not self.confirm_gpu_overlap():
                # Warn about local GPU contention. (No local GPU is used in remote
                # mode, so that check is skipped above.)
                return

        self.progress.set(0)
        self._reset_stream_state()
        self._snapshot_cost_run(remote)
        self.status_var.set(
            "Starting the remote pod (first run takes a few minutes) …" if remote
            else "Starting — loading the AI engine (the first run can take a few minutes) …")
        # The skip-cutoff now lives in Settings; batch_upscale reads it from config.json.
        if self.launch("batch_upscale.py", [src, out], extra_env=extra_env):
            self._remote_run = remote
            self._paused     = False
            self._processing = False
            self._cancelled  = False
            self._phase      = ""
            self._set_running(True)
            # Until per-image processing starts, this button cancels the run
            self.pause_btn.configure(text="Cancel")
            self._sync_pause_tip()

    def _pause_or_cancel(self):
        """
        Before per-image processing starts this button cancels the run
        (scanning / cache verification / eligibility end gracefully and the
        cache is saved); during processing it toggles pause as before.
        """
        if not self._processing:
            self._cancelled = True
            self.send("q")
            self.pause_btn.configure(state="disabled")
            self.stop_btn.configure(state="disabled")
            self.status_var.set("Cancelling — finishing the current step …")
            return
        self.send("p")
        self._paused = not self._paused
        self.pause_btn.configure(text="Resume" if self._paused else "Pause")
        self._sync_pause_tip()
        if self._paused:
            # A local run releases the GPU once the current image is done; a
            # remote one has nothing local to release (the models are on the pod).
            self.status_var.set(
                "Pausing — finishes the current image, then releases the GPU …"
                if not getattr(self, "_remote_run", False)
                else "Pausing — finishes the current image first …")

    def _stop(self):
        if getattr(self, "_remote_run", False):
            idle = int(CFG.get("runpod", {}).get("idle_timeout_minutes", 15))
            # Yes → stop the pod now · No → leave it running · Cancel → keep going.
            ans = messagebox.askyesnocancel(
                APP_TITLE,
                "The upscale run will stop after the current image.\n\n"
                "Also STOP the remote pod now?\n\n"
                "• Yes — stop the pod immediately (billing stops now).\n"
                f"• No — leave it running; the dead-man's switch stops it "
                f"automatically after {idle} min of inactivity.\n\n"
                "Cancel — keep the run going.")
            if ans is None:
                return                              # cancelled — keep running
            self.send("qstop" if ans else "qkeep")
        else:
            self.send("q")
        self.stop_btn.configure(state="disabled")
        self.status_var.set("Stopping — finishes the current image first …")

    def _set_running(self, running):
        has_src = bool(self.src_var.get().strip())
        self.start_btn.configure(
            state="normal" if (not running and has_src) else "disabled")
        self.pause_btn.configure(state="normal"   if running else "disabled")
        self.stop_btn.configure(state="normal"    if running else "disabled")
        # VRAM is fully committed during an upscale run — lock out the other tool
        self.app.set_tag_tab_enabled(not running)
        self.app.refresh_conciliate_lock()
        self.app.refresh_benchmark_lock()
        self.app.refresh_tab_exclusivity()

    def on_exit(self, code):
        self._set_running(False)
        self._paused = False
        self.pause_btn.configure(text="Pause")
        self._sync_pause_tip()
        self.eta_var.set("—")
        # The remote-pod telemetry row only makes sense during a remote run; hide it and
        # zero the MQTT system/remote/* topics so a terminated pod leaves no stale values.
        self.app.clear_remote_telemetry(self)
        # Surface the run's tally line (e.g. "65 processed, 850 already done,
        # 5 corrupted, …") on the top status row, above the closing message.
        self._final_top = self.console.find_last(r"\(\d+ processed")
        # The last image's preview decode may still be in flight — the poll
        # loop has stopped, so drain the preview queue a little while longer.
        for delay in (250, 1000, 3000):
            self.after(delay, self._tick)
        if self._cancelled:
            self.progress.set(0)
            self.status_var.set("Cancelled. Progress so far was saved — "
                                "the next run will pick up where this one left off.")
        elif code == 0:
            self.progress.set(100)
            self.status_var.set("Done. The upscaled photos are in the output folder.")
        else:
            self.status_var.set(self._failure_status(code))
        super().on_exit(code)


# ─────────────────────────────────────────────
#  TAB 2 — TAG & RENAME
# ─────────────────────────────────────────────
