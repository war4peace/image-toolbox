"""
gui/tooltab.py
--------------
ToolTab: the shared base for the Batch Upscaler, Tag & Rename and Conciliation
tabs. Owns the subprocess plumbing (launch/pump/poll of the runner scripts), the
@@TBX@@ event-marker parsing, the image-queue preview strip (FilmStrip), the log
viewer, per-tool telemetry, and the MQTT/taskbar task-state publishing.

Sits above gui.common/widgets/comparison/filmstrip in the import order; it talks
to the App only at runtime via self.app, so it does not import the app module.
"""

import os
import json
import codecs
import datetime
import queue
import threading
import subprocess

import tkinter as tk
from tkinter import ttk, messagebox

import mqtt_publisher
import runpod_client
import taskbar_progress

from gui.common import (
    SCRIPT_DIR, APP_ROOT, APP_TITLE, GUI_MARKER, PROGRESS_RE, PYTHON_EXE,
    CREATE_NO_WINDOW, CFG, save_config, load_settings, save_settings,
    get_default_folder, set_default_folder, get_install_mode, mqtt_enabled,
    funds_color, config_funds_floor, fmt_funds, _FUNDS_GREY,
)
from gui.widgets import (
    sanitize, _fmt_eta, ProgressBar, TelemetryRow, LogPane, LogViewer,
    ConsoleBuffer, Tooltip, COST100_TIP_BENCH, COST100_TIP_USER, COST100_TIP_RUN,
)
from gui.comparison import ComparisonWindow, VideoComparisonWindow
from gui.filmstrip import FilmStrip, CELL_DEFAULT


# ─────────────────────────────────────────────
#  TOOL TAB BASE  (subprocess plumbing)
# ─────────────────────────────────────────────

class ToolTab(ttk.Frame):
    """
    Shared plumbing for both tool tabs: subprocess management, GUI-event
    marker parsing, the image-queue preview strip and the log viewer.
    """

    def __init__(self, notebook, app):
        super().__init__(notebook, padding=(12, 10))
        self.app    = app
        self.proc   = None
        self._queue = queue.Queue()
        # Marker-stream parser state (see _filter_markers)
        self._at_line_start = True
        self._marker_buf    = None    # not None → inside a marker line
        self._hold          = ""      # ambiguous marker prefix held back
        self._log_path      = None    # current log file (from LOG events)
        self.tool_name      = "program"         # overridden by each tool tab
        self.active_pod_id  = None              # remote pod this run uses (POD events)
        self.console        = ConsoleBuffer()   # clean output stream (backlog + live)
        self._phase_text    = "Ready."          # activity/phase line (prep + final)
        self._final_top     = ""                # summary line shown above the final message
        self._finished      = True              # True when no run is in progress
        self.mqtt_task_name = "task"            # MQTT task/name label (overridden)
        self._last_done     = None              # last DONE payload (for MQTT last_run)
        self._mqtt_prev_elapsed   = 0.0         # for last-image processing time
        self._mqtt_prev_processed = 0
        # System telemetry (Feature #3a). Periodic tabs set an interval; the
        # upscaler leaves it None and samples per image instead (see launch /
        # _on_image_started). The row widget is created by whichever build path
        # the tab uses.
        self.telemetry_row          = None
        self.telemetry_interval_ms  = None      # set by periodic tabs (Tag/Conciliate)
        self._telemetry_job         = None      # periodic sampler `after` id
        self._telemetry_img_job     = None      # upscaler per-image sample `after` id
        # Remote-pod live cost tracking (0.3.9). `_remote_rate` = the deployed
        # pod's real $/h (from the RCOST event; None on local runs). The combined
        # "Est. Time Remaining / Cost" field and the per-100-images readout derive
        # from it. `_proc_history` is a per-image (processed, elapsed) trail used
        # for the sliding last-100-images cost once 100+ images are done.
        self._remote_rate   = None
        self._proc_history  = []
        self._cost100_real  = False             # True once 100+ images measured
        self._bench_task    = None              # "tag"/"upscale", set by each tab
        self.cost100_var    = None              # per-100 readout (built per tab)
        self.cost100_tip    = None              # its (dynamic) tooltip
        # GPU id + benchmark task this run uses, snapshotted at launch so the
        # run's measured timing can be recorded on completion (db.gpu_perf) to
        # warm-start a future run's estimate. None on local runs (nothing to bill).
        self._run_gpu_id    = None
        self._run_task      = None

    # ── UI helpers ──────────────────────────────────────────────────────────

    def _build_output_area(self, row):
        """Progress bar + two-row status + full-width image preview wall."""
        pf = ttk.Frame(self)
        pf.grid(row=row, column=0, columnspan=4, sticky="ew", pady=(10, 2))
        # Label flips to ".../ Cost" on a remote run (when a $/h rate is known);
        # the value then carries "<time> / $<remaining cost>" (0.3.9).
        self.eta_label_var = tk.StringVar(value="Est. Time Remaining:")
        ttk.Label(pf, textvariable=self.eta_label_var).pack(side="left")
        self.eta_var = tk.StringVar(value="—")
        ttk.Label(pf, textvariable=self.eta_var, width=34, anchor="e",
                  font=("Consolas", 9)).pack(side="left", padx=(4, 12))
        self.progress = ProgressBar(pf, width=260)
        self.progress.pack(side="left", fill="x", expand=True)

        # Two-row status: the previously completed file and the current one.
        # Monospaced so the columns line up; width=1 + sticky lets the labels
        # stretch with the window and clip overflow instead of forcing it wider.
        sf = ttk.Frame(self)
        sf.grid(row=row + 1, column=0, columnspan=4, sticky="ew")
        sf.columnconfigure(0, weight=1)
        self.status_top = tk.Label(sf, font=("Consolas", 9), fg="#7f8a99",
                                   anchor="w", width=1)
        self.status_bot = tk.Label(sf, font=("Consolas", 9), anchor="w", width=1)
        self.status_top.grid(row=0, column=0, sticky="ew")
        self.status_bot.grid(row=1, column=0, sticky="ew")
        self.status_var = tk.StringVar(value="Ready.")
        self.status_var.trace_add("write", lambda *_: self._on_phase_change())
        self._on_phase_change()

        body = ttk.LabelFrame(self, text=" Image preview ", padding=6)
        body.grid(row=row + 2, column=0, columnspan=4, sticky="nsew", pady=(6, 0))
        self.rowconfigure(row + 2, weight=1)
        self.columnconfigure(1, weight=1)
        body.rowconfigure(1, weight=1)
        body.columnconfigure(0, weight=1)

        header = ttk.Frame(body)
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)
        self.preview_name = tk.StringVar(value="")
        ttk.Label(header, textvariable=self.preview_name, anchor="w").grid(
            row=0, column=0, sticky="w")
        zb = ttk.Frame(header)
        zb.grid(row=0, column=1, sticky="e")
        size_lbl = ttk.Label(zb, text="Thumbnail size:")
        size_lbl.pack(side="left", padx=(0, 4))
        smaller_btn = ttk.Button(zb, text="–", width=3,
                                 command=lambda: self.strip.zoom_out())
        smaller_btn.pack(side="left")
        larger_btn = ttk.Button(zb, text="+", width=3,
                                command=lambda: self.strip.zoom_in())
        larger_btn.pack(side="left", padx=(2, 0))
        Tooltip(size_lbl,
                "How big each picture is drawn in the preview strip below. The "
                "size you pick is remembered between runs.",
                wraplength=Tooltip.WRAP_NARROW)
        Tooltip(smaller_btn,
                "Smaller thumbnails, so more of the run's images fit on screen "
                "at once.", wraplength=Tooltip.WRAP_NARROW)
        Tooltip(larger_btn,
                "Larger thumbnails, so more detail is visible in each image.",
                wraplength=Tooltip.WRAP_NARROW)

        cell = int(self.app.settings.get("thumb_cell", CELL_DEFAULT))
        self.strip = FilmStrip(body, cell=cell, on_zoom=self._save_cell)
        self.strip.grid(row=1, column=0, sticky="nsew", pady=(6, 0))
        Tooltip(self.strip.canvas,
                "Double-click an image to open it • use +/– to resize")

        # Compact system-telemetry row, just below the carousel (Feature #3a).
        # The "Local Unit" prefix is the same width as the remote row's
        # "Remote pod" so the two rows' columns line up when both are shown.
        self.telemetry_row = TelemetryRow(self, prefix="Local Unit")
        self.telemetry_row.grid(row=row + 3, column=0, columnspan=4,
                                sticky="ew", pady=(4, 0))
        # Second row for the remote pod (remote #1, Feature #4) — created hidden,
        # shown only while a remote-pod run is streaming RTELEM telemetry.
        self.remote_telemetry_row = TelemetryRow(self, prefix="Remote pod")
        self.remote_telemetry_row.grid(row=row + 4, column=0, columnspan=4,
                                       sticky="ew", pady=(1, 0))
        self.remote_telemetry_row.grid_remove()

    def _save_cell(self, cell):
        self.app.settings["thumb_cell"] = cell
        save_settings(self.app.settings)

    # ── Two-row status ───────────────────────────────────────────────────────

    def _on_phase_change(self):
        self._phase_text = self.status_var.get()
        self._refresh_status()
        # Mirror the phase line to MQTT as the task's detailed status. (No-op
        # until the MQTT client exists, so the construction-time "Ready." is
        # never published.)
        self.app.mqtt_publish({mqtt_publisher.TASK_DETAILS_TOPIC: self._phase_text})

    def _refresh_status(self):
        if not hasattr(self, "status_bot"):
            return
        img = self.console.last_image_lines(2)
        if not self._finished and img:
            top = img[-2] if len(img) >= 2 else ""
            bot = img[-1]
        else:
            # After a run, show the summary line (if any) above the final message.
            top, bot = self._final_top, self._phase_text
        self.status_top.configure(text=top)
        self.status_bot.configure(text=bot)

    def restore_defaults_if_empty(self):
        """Re-apply the pinned default folder(s) to any field still empty.

        Called when this tab is entered (see App._on_tab_changed), not only at
        construction — so a default set in Settings *after* startup shows up when
        the user switches to the tab. Idempotent: only fills empty fields, never
        overwrites a folder the user is working with. Overridden per tool."""

    # ── Remote-pod row: "Run on remote pod" + live GPU picker ────────────────
    #
    # The picker queries RunPod for what is ACTUALLY deployable right now (GPU,
    # live price, stock) in the volume's region, filtered to a usable VRAM floor
    # and sorted cheapest-first — so the user can't pick a card that only fails at
    # create time (the static Settings picklists could). The selection overrides
    # the configured default for this run and seeds a price-ordered fallback
    # chain. Tabs set self._gpu_min_vram and self._gpu_pref_key before building.

    def _build_remote_row(self, row):
        """A dedicated full-width row so the long checkbox label and the GPU
        picker both fit (fixes the 'Run on rer' clipping when they shared the
        button row)."""
        rr = ttk.Frame(self)
        rr.grid(row=row, column=0, columnspan=4, sticky="ew", pady=(10, 0))
        self.remote_chk = ttk.Checkbutton(
            rr, text="Run on remote pod (RunPod)", variable=self.remote_var,
            command=self._on_remote_toggle)
        self.remote_chk.pack(side="left")
        # Kept as an attribute so a tab with a different story (Tag & Rename runs
        # locally and only tunnels the model calls) can RETARGET it via set_text.
        # Adding a second Tooltip would not override this one: Tooltip binds with
        # add="+", so both would pop up, stacked.
        self.remote_tip = Tooltip(
            self.remote_chk,
            "Run on a rented RunPod GPU instead of this PC (roadmap #1, "
            "experimental). Creates a billed pod and terminates it when done. "
            "Needs a RunPod API key + model volume in Settings.",
            wraplength=Tooltip.WRAP_NARROW)
        ttk.Label(rr, text="GPU:").pack(side="left", padx=(16, 4))
        self.gpu_pick_var = tk.StringVar(value="")
        self.gpu_combo = ttk.Combobox(
            rr, textvariable=self.gpu_pick_var, state="disabled", width=44)
        self.gpu_combo.pack(side="left")
        Tooltip(self.gpu_combo,
                "GPUs that can be rented in your volume's region right now, "
                "cheapest first. Live availability and price from RunPod. The run "
                "uses exactly the card you pick, with no automatic substitution: if "
                "it has sold out by the time the pod deploys, the run stops cleanly "
                "and you press ↻ to refresh stock and pick another.")
        self.gpu_refresh_btn = ttk.Button(
            rr, text="↻", width=3, state="disabled", command=self._refresh_gpus)
        self.gpu_refresh_btn.pack(side="left", padx=(4, 0))
        Tooltip(self.gpu_refresh_btn, "Re-check availability and pricing.")
        # Estimated $ / 100 images (0.3.9). Benchmark-derived before a run; switches
        # to a live figure once 100 images are processed. Hidden on local runs.
        self.cost100_var = tk.StringVar(value="")
        self.cost100_lbl = ttk.Label(rr, textvariable=self.cost100_var,
                                     anchor="w", foreground="#2f6f3f")
        self.cost100_lbl.pack(side="left", fill="x", expand=True, padx=(12, 0))
        self.cost100_tip = Tooltip(self.cost100_lbl, COST100_TIP_BENCH)
        # The GPU selection drives the benchmark estimate.
        self.gpu_combo.bind("<<ComboboxSelected>>",
                            lambda _e: self._update_cost100_estimate(), add="+")
        self._gpu_choices = []      # parallel to the combobox values (dicts)
        self._gpu_loaded  = False   # True after a successful fetch
        # A Remote-only install defaults the toggle on — load the list up front.
        if self.remote_var.get():
            self.after(300, self._on_remote_toggle)

    def _on_remote_toggle(self):
        on = bool(self.remote_var.get())
        self.gpu_combo.configure(state="readonly" if on else "disabled")
        self.gpu_refresh_btn.configure(state="normal" if on else "disabled")
        # The $/100 readout is meaningful only for a billed remote run.
        if on:
            self._update_cost100_estimate()
        elif self.cost100_var is not None:
            self.cost100_var.set("")
        if on and not self._gpu_loaded:
            self._refresh_gpus()
        # Enable/disable the bottom-bar funds readout to match the remote toggle.
        self.app.refresh_funds_readout()

    def _refresh_gpus(self):
        rp_cfg = CFG.get("runpod", {})
        if not rp_cfg.get("api_key"):
            self.gpu_pick_var.set("(set a RunPod API key in Settings)")
            return
        self.gpu_refresh_btn.configure(state="disabled")
        self.gpu_pick_var.set("loading available GPUs …")
        self.gpu_combo.configure(values=[])
        min_vram = getattr(self, "_gpu_min_vram", 0)

        def work():
            try:
                import runpod_client as rp
                key   = rp_cfg.get("api_key", "")
                vol   = rp_cfg.get("network_volume_id", "").strip()
                # The pod is created in the VOLUME's region, so price/stock must be
                # read there — fall back to the configured data center, then global.
                cfg_dcs = rp_cfg.get("data_center_ids") or []
                dc = (rp.volume_region(key, vol) if vol else None) \
                    or (cfg_dcs[0] if cfg_dcs else None)
                gpus = rp.available_gpus(key, dc, min_memory_gb=min_vram)
                self.after(0, lambda: self._populate_gpus(gpus, dc))
            except Exception as exc:                     # noqa: BLE001 (UI thread)
                self.after(0, lambda e=exc: self._gpu_error(e))

        threading.Thread(target=work, daemon=True).start()

    def _gpu_label(self, g):
        price = f"${g['price']:.2f}/h" if g.get("price") is not None else "price n/a"
        return f"{g['name']} — {g['memory_gb']} GB — {price} ({g['stock']})"

    def _populate_gpus(self, gpus, dc):
        self.gpu_refresh_btn.configure(state="normal")
        self._gpu_choices = gpus
        self._gpu_loaded  = True
        if not gpus:
            region = dc or "this region"
            self.gpu_combo.configure(values=[])
            self.gpu_pick_var.set(f"no GPU available in {region} right now")
            return
        labels = [self._gpu_label(g) for g in gpus]
        self.gpu_combo.configure(values=labels)
        # Pre-select the persisted preference if it is in stock; else cheapest.
        pref = CFG.get("runpod", {}).get(getattr(self, "_gpu_pref_key", ""), "")
        idx = next((i for i, g in enumerate(gpus) if g["id"] == pref), 0)
        self.gpu_combo.current(idx)
        self._update_cost100_estimate()

    def _gpu_error(self, exc):
        self.gpu_refresh_btn.configure(state="normal")
        self._gpu_choices = []
        self._gpu_loaded  = False
        self.gpu_pick_var.set("couldn't load list — will use the Settings default")
        self.console.feed(f"[remote] GPU availability check failed: {exc}\n")

    # ── $ / 100 images readout (0.3.9) ───────────────────────────────────────

    def _selected_gpu(self):
        """The GPU dict currently picked in the combobox, or None."""
        if not self._gpu_choices:
            return None
        idx = self.gpu_combo.current()
        if idx < 0 or idx >= len(self._gpu_choices):
            idx = 0
        return self._gpu_choices[idx]

    def _bench_task_for(self, gpu):
        """The Benchmarks.csv 'Task' that applies to this tab + GPU. Tagging is
        one task; upscaling splits by VRAM (resident vs pod-RAM offload), the same
        40 GB threshold the engine uses."""
        import benchmarks
        if self._bench_task == "tag":
            return benchmarks.TASK_TAG
        thr = CFG.get("upscale", {}).get("vram_resident_threshold_gb", 40)
        return benchmarks.upscale_task(gpu.get("memory_gb"), thr)

    def _update_cost100_estimate(self):
        """Refresh the per-100-images readout from the author's benchmarks for the
        selected GPU (called pre-run and on GPU change). A live run overrides this
        once it has measured 100 images (_update_cost100_live)."""
        if self.cost100_var is None or self._cost100_real:
            return
        if not self.remote_var.get():
            self.cost100_var.set("")
            return
        g = self._selected_gpu()
        if not g:
            self.cost100_var.set("")
            return
        import benchmarks
        # Precedence: the user's own history on this exact GPU (once they have
        # enough) -> the author benchmark -> N/A. The tooltip names the source.
        secs, source = benchmarks.estimate_seconds_per_100(
            self._bench_task_for(g), g.get("id"), g.get("name"))
        price = g.get("price")
        if self.cost100_tip is not None:
            self.cost100_tip.text = COST100_TIP_USER if source == "user" else COST100_TIP_BENCH
        if secs is None or price is None:
            self.cost100_var.set("~ N/A / 100 images")
        else:
            cost = secs / 3600.0 * float(price)
            self.cost100_var.set(f"~ ${cost:,.2f} / 100 images")

    def _update_cost100_live(self, processed, elapsed):
        """Recompute the per-100-images cost from the current run once 100 images
        have been processed: time over the last 100 images x the pod's $/h. Before
        100 the benchmark estimate stands."""
        if (self.cost100_var is None or self._remote_rate is None
                or processed < 100 or len(self._proc_history) < 101):
            return
        p0, e0 = self._proc_history[-101]           # exactly 100 images back
        dp, de = processed - p0, elapsed - e0
        if dp <= 0 or de <= 0:
            return
        cost = (de / dp) * 100 / 3600.0 * self._remote_rate
        self.cost100_var.set(f"${cost:,.2f} / 100 images")
        if not self._cost100_real:
            self._cost100_real = True
            if self.cost100_tip is not None:
                self.cost100_tip.text = COST100_TIP_RUN

    def _snapshot_cost_run(self, remote):
        """Remember the GPU id + benchmark task this run uses, so its measured
        timing can be recorded on completion (db.gpu_perf) and warm-start future
        $/100 estimates. Local runs record nothing (no GPU bill)."""
        g = self._selected_gpu() if remote else None
        self._run_gpu_id = g.get("id") if g else None
        self._run_task   = self._bench_task_for(g) if g else None

    def _record_gpu_perf(self, payload):
        """On a finished remote run, accumulate this GPU's measured timing
        (db.gpu_perf) so a later run on the same card warm-starts its $/100
        estimate from real data. Time only; keyed by GPU id + benchmark task.
        Best-effort: any DB error is swallowed."""
        if self._remote_rate is None or not self._run_gpu_id or not self._run_task:
            return
        try:
            data    = json.loads(payload) if payload else {}
            images  = int(data.get("processed") or 0)
            seconds = float(data.get("elapsed_seconds") or 0.0)
        except (ValueError, TypeError):
            return
        try:
            import db
            db.record_gpu_perf(db.get_conn(), self._run_task, self._run_gpu_id,
                               images, seconds)
        except Exception:                       # noqa: BLE001 (best-effort)
            pass

    def _selected_gpu_chain(self):
        """The picked GPU id as a single-element list. We NEVER substitute a
        different GPU TYPE without the user's consent (their preference): if the
        pick is unavailable at start, the run stops and the user refreshes (↻) and
        re-picks. None when nothing was loaded (the run then uses the configured
        default). Empty list = loaded but none available, a hard stop for the
        caller. (Kept the "_chain" name + return shape so the call sites and the
        IMGTBX_GPU_OVERRIDE join are unchanged.)"""
        if not self._gpu_loaded:
            return None
        if not self._gpu_choices:
            return []
        idx = self.gpu_combo.current()
        if idx < 0 or idx >= len(self._gpu_choices):
            idx = 0
        return [self._gpu_choices[idx]["id"]]

    def _gpu_confirm_note(self, gpu_chain):
        """A short line for the 'run on a billed pod?' confirm: the chosen GPU and
        price. No fallback line — only the selected card is used."""
        if not gpu_chain:
            return ""
        g = self._gpu_choices[max(0, self.gpu_combo.current())]
        price = g.get("price")
        price_str = f" (~${price:.2f}/h)" if price is not None else ""
        return f"\n\nGPU: {g['name']}, {g['memory_gb']} GB{price_str}."

    def _failure_status(self, code):
        """A meaningful one-line status for a non-zero exit. The clean output goes
        to the (separate) log window, not the tab, so the old 'see the messages
        above' pointed at nothing — this surfaces the actual cause instead and
        points at 'View log'."""
        # The common remote case — no rentable GPU — gets a plain-language line.
        # "Pod failed to deploy on" matches both the single-card wording (0.4.0: "on
        # <gpu> (N attempts)") and the multi-card "on any of [...]".
        if self.console.find_last(
                r"no instances currently available|Pod failed to deploy on"
                r"|Gave up creating a pod"):
            return ("Couldn't start a remote pod: no matching GPU was available "
                    "just now. Re-check the GPU picker (↻), try another region, or "
                    "open 'View log' for details.")
        # Otherwise echo the script's own ERROR / '-> detail' line if there is one.
        err = self.console.find_last(r"^\s*(ERROR:|->)")
        if err:
            err = err.lstrip("-> ").strip()
            if len(err) > 140:
                err = err[:139] + "…"
            return f"Stopped with an error (code {code}): {err}  (See 'View log'.)"
        return f"Stopped with an error (code {code}) — open 'View log' for details."

    # ── Process control ─────────────────────────────────────────────────────

    @property
    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def launch(self, script, args, extra_env=None):
        # The child scripts are this module's siblings in scripts/; run them with
        # the working directory at the app root (where config/state/engine live).
        cmd = [PYTHON_EXE, "-u", os.path.join(SCRIPT_DIR, script)] + args
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"
        if extra_env:
            env.update(extra_env)
        try:
            self.proc = subprocess.Popen(
                cmd, cwd=APP_ROOT,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                creationflags=CREATE_NO_WINDOW, env=env,
            )
        except Exception as exc:
            messagebox.showerror(APP_TITLE, f"Could not start {script}:\n{exc}")
            return False
        self._finished = False
        # Taskbar button: marquee until the first real progress tick arrives
        # (covers the initial scan), then it switches to a determinate fill.
        self.app.taskbar_state("indeterminate")
        if hasattr(self, "viewlog_btn"):
            self.viewlog_btn.configure(state="normal")
        # If the shared log window is open, make it follow this run's output.
        self.app.rebind_log_if_open(self.console, self._log_title())
        # MQTT: a task is now active — reset timing and announce it.
        self._last_done = None
        self._mqtt_prev_elapsed   = 0.0
        self._mqtt_prev_processed = 0
        self.app.mqtt_publish({
            mqtt_publisher.TASK_NAME_TOPIC:     self.mqtt_task_name,
            mqtt_publisher.TASK_DETAILS_TOPIC:  "starting",
            mqtt_publisher.TASK_PROGRESS_TOPIC: "",
            mqtt_publisher.TASK_ETA_TOPIC:      "",
            mqtt_publisher.TASK_RUNTIME_TOPIC:  "0",
        })
        self._start_telemetry()
        threading.Thread(target=self._pump, daemon=True).start()
        self.after(50, self._poll)
        return True

    def _pump(self):
        """Reader thread: stream child stdout into the queue as it arrives."""
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        stream  = self.proc.stdout
        while True:
            chunk = stream.read1(4096)
            if not chunk:
                break
            self._queue.put(decoder.decode(chunk))
        self._queue.put(None)   # sentinel: child closed its output

    def _poll(self):
        finished = False
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            if item is None:
                finished = True
                break
            self._process_chunk(item)
        self._tick()
        if finished:
            code = self.proc.wait()
            self.proc = None
            self._finished = True
            self.on_exit(code)
        elif self.proc is not None:
            self.after(50, self._poll)

    def _process_chunk(self, text):
        """Handle one chunk of child output (GUI markers filtered out)."""
        text = self._filter_markers(text)
        if text:
            self.console.feed(text)      # backlog + any open log window
            self._scan_progress(text)    # progress bar + phase text
            self._refresh_status()       # two-row file status

    def _filter_markers(self, text):
        """
        Strip "@@TBX@@KIND|payload\\n" event lines from the output stream and
        dispatch them to _handle_event. Events are normally on their own line,
        but a background thread (the remote-pod telemetry sampler) can interleave
        one MID-LINE, so the marker is detected ANYWHERE in the stream — not only
        at a line start. Consuming the event together with its trailing newline
        reconstructs the human line it split. State is kept between calls: a
        marker split across read chunks, and an ambiguous trailing prefix like
        "@@TB" held back until the next chunk completes it.
        """
        out  = []
        data = self._hold + text
        self._hold = ""
        pos, n = 0, len(data)
        while pos < n:
            if self._marker_buf is not None:        # inside a marker line → to \n
                nl = data.find("\n", pos)
                if nl < 0:
                    self._marker_buf += data[pos:]
                    pos = n
                else:
                    self._marker_buf += data[pos:nl]
                    self._on_marker(self._marker_buf)
                    self._marker_buf = None
                    pos = nl + 1                    # consume the event's newline
                continue
            idx = data.find(GUI_MARKER, pos)
            if idx < 0:
                # No complete marker ahead. Hold back a trailing partial marker
                # prefix ("@@TB") so it isn't shown as content and can complete
                # on the next chunk.
                hold = ""
                for k in range(len(GUI_MARKER) - 1, 0, -1):
                    if n - pos >= k and data.endswith(GUI_MARKER[:k]):
                        hold = GUI_MARKER[:k]
                        break
                out.append(data[pos:n - len(hold)] if hold else data[pos:])
                self._hold = hold
                pos = n
            else:
                out.append(data[pos:idx])           # human text before the marker
                self._marker_buf = ""
                pos = idx + len(GUI_MARKER)
        return "".join(out)

    def _on_marker(self, content):
        # The pipe delivers Windows line endings — the split on "\n" leaves
        # a trailing "\r", which would break paths and JSON parsing.
        content = content.strip()
        kind, _, payload = content.partition("|")
        self._handle_event(kind, payload)

    def _handle_event(self, kind, payload):
        """Events common to both tools; subclasses extend for their own."""
        if kind == "QUEUE":
            try:
                self.strip.set_queue(json.loads(payload))
            except ValueError:
                pass
        elif kind == "IMG" and payload:
            self.preview_name.set(os.path.basename(payload))
            self.strip.set_current(payload)
            self._on_image_started(payload)
        elif kind == "RENAME" and payload:
            try:
                old, new = json.loads(payload)
            except (ValueError, TypeError):
                return
            self.strip.rename(old, new)
            if self.preview_name.get() == os.path.basename(old):
                self.preview_name.set(os.path.basename(new))
        elif kind == "RESULT" and payload:
            try:
                data = json.loads(payload)
                path, status = data[0], data[1]
                compare_to = data[2] if len(data) > 2 else None
            except (ValueError, TypeError, IndexError):
                return
            self.strip.set_status(path, status, compare_to)
        elif kind == "REFRESH" and payload:
            self.strip.refresh(payload)
        elif kind == "ETA" and payload:
            self._handle_eta(payload)
        elif kind == "LOG" and payload:
            self._log_path = payload
            self.viewlog_btn.configure(state="normal")
        elif kind == "POD":
            # Remote run reports the pod it's using (payload empty = none). The
            # RunPod tab uses this to protect the live pod from being terminated.
            self.active_pod_id = payload or None
            self.app.notify_active_pods_changed()
        elif kind == "RCOST" and payload:
            # The deployed pod's real billed rate ($/h). Arms the live cost
            # readouts: flip the ETA label to include cost and recompute the
            # $/100 estimate against the actual rate.
            try:
                self._remote_rate = float(payload)
            except ValueError:
                self._remote_rate = None
            if self._remote_rate is not None:
                self.eta_label_var.set("Est. Time Remaining / Cost:")
        elif kind == "DEGRADED" and payload:
            # Performance watchdog tripped in the runner (sustained slow streak or
            # a hard OOM): it is auto-stopping. Surface it and flash for attention —
            # the user is typically away during a long run. The full reason (carried
            # in the JSON payload) is already in the log stream; keep this short.
            self.status_var.set("⚠ GPU performance degraded — run auto-stopped "
                                "(reboot, then resume).")
            self.app.taskbar_state("error")    # red taskbar bar
            self.app.flash_attention()
        elif kind == "RTELEM" and payload:
            # Remote-pod telemetry (#4): reveal/update the tab's remote row.
            try:
                sample = json.loads(payload)
            except ValueError:
                return
            self.app.apply_remote_telemetry(self, sample)
        elif kind == "DONE":
            self._last_done = payload   # JSON summary; published to MQTT on exit
            self.app.flash_attention()  # catch the eye when a long run finishes
            self._record_gpu_perf(payload)   # learn this GPU's timing for next time

    def _reset_stream_state(self):
        """Reset the marker parser, console and preview strip before a new run."""
        self._at_line_start = True
        self._marker_buf    = None
        self._hold          = ""
        self.console.clear()
        self.strip.clear()
        self.preview_name.set("")
        self._final_top = ""
        self.eta_var.set("—")
        # Reset remote-pod cost tracking; RCOST (re)arms it for a remote run, and
        # the $/100 readout falls back to the benchmark estimate.
        self._remote_rate  = None
        self._proc_history = []
        self._cost100_real = False
        self.eta_label_var.set("Est. Time Remaining:")
        self._update_cost100_estimate()

    def _tick(self):
        """Every poll cycle: display freshly decoded strip thumbnails."""
        self.strip.drain()

    def _log_title(self):
        return f"{APP_TITLE} — {self.tool_name} output"

    def _view_log(self):
        # One shared log window for the whole app; it switches to show the
        # output of whichever tab asked for it (or whichever tool is running).
        self.app.show_log(self.console, self._log_title())

    def _scan_progress(self, text):
        matches = PROGRESS_RE.findall(text)
        if not matches:
            return
        cur, tot = (int(x) for x in matches[-1])
        if tot <= 0:
            return
        self.progress.set(cur * 100 / tot)
        self.status_var.set(f"Processing image {cur} of {tot} …")
        self.app.taskbar_progress(cur, tot)
        self.app.mqtt_publish({mqtt_publisher.TASK_PROGRESS_TOPIC: f"{cur}/{tot}"})

    def _handle_eta(self, payload):
        """ETA event from the running tool: 'elapsed|processed|idx|total[|P]'.
        Estimate = (elapsed / images actually processed this session) ×
        images still to go. Using the processed count — not the position
        counter, which also advances on skipped files — keeps the average
        per-image time honest. The elapsed value is pause-excluded. A trailing
        'P' marks the image-processing phase, which arms the remote-pod cost
        readouts (scan/verify/eligibility ETAs omit it)."""
        parts = payload.split("|")
        try:
            elapsed   = float(parts[0])
            processed = int(parts[1])
            idx       = int(parts[2])
            total     = int(parts[3])
        except (ValueError, IndexError):
            return
        # A trailing 'P' marks the real image-processing phase (scan/verify/
        # eligibility ETAs omit it): only then do we track remote-pod cost.
        processing = len(parts) > 4 and parts[4] == "P"
        if total > 0:
            self.progress.set(idx * 100 / total)
            self.app.taskbar_progress(idx, total)
        mqtt_values = {
            mqtt_publisher.TASK_RUNTIME_TOPIC:  str(int(elapsed)),
            mqtt_publisher.TASK_PROGRESS_TOPIC: f"{idx}/{total}",
        }
        if processed > 0 and total > 0:
            avg       = elapsed / processed
            remaining = max(0, total - idx)
            eta_txt   = _fmt_eta(avg * remaining)
            # On a billed remote run, pair the remaining time with the remaining
            # cost (cost-to-go = images left x avg per-image time x the pod's $/h).
            if processing and self._remote_rate is not None:
                rem_cost = remaining * avg / 3600.0 * self._remote_rate
                self.eta_var.set(f"{eta_txt} / ${rem_cost:,.2f}")
                # Seed a (0 images, ~0 s) boundary so the 100th image yields an
                # exact last-100 window (otherwise the live figure lags by one).
                if not self._proc_history:
                    self._proc_history.append((0, 0.0))
                self._proc_history.append((processed, elapsed))
                if len(self._proc_history) > 150:
                    self._proc_history.pop(0)
                self._update_cost100_live(processed, elapsed)
            else:
                self.eta_var.set(eta_txt)
            mqtt_values[mqtt_publisher.TASK_ETA_TOPIC] = eta_txt
            mqtt_values[mqtt_publisher.TASK_AVG_TOPIC] = f"{avg:.1f}"
            # Last-image time = work done since the previous ETA tick.
            d_proc = processed - self._mqtt_prev_processed
            if d_proc > 0:
                last = (elapsed - self._mqtt_prev_elapsed) / d_proc
                mqtt_values[mqtt_publisher.TASK_LAST_TOPIC] = f"{max(0.0, last):.1f}"
        self._mqtt_prev_elapsed   = elapsed
        self._mqtt_prev_processed = processed
        self.app.mqtt_publish(mqtt_values)

    def send(self, line):
        """Send one control line to the child's stdin."""
        if self.proc and self.proc.stdin:
            try:
                self.proc.stdin.write((line + "\n").encode("utf-8"))
                self.proc.stdin.flush()
            except OSError:
                pass

    def terminate(self):
        if self.running:
            try:
                self.proc.kill()
            except Exception:
                pass

    def confirm_gpu_overlap(self):
        """Warn when the other tab is busy — both tools compete for the GPU."""
        other = self.app.other_tab(self)
        if other is not None and other.running:
            return messagebox.askyesno(
                APP_TITLE,
                "The other tool is still running.\n"
                "Running both at the same time will be very slow and may run "
                "out of video memory.\n\nStart anyway?")
        return True

    def confirm_deadman_safety(self):
        """For a remote run: warn if BOTH dead-man's-switch limits are 0, which
        leaves no auto-stop safety net (a crash/dropped connection would leave the
        pod billing). max-runtime 0 alone is fine — the idle timeout still guards.
        Returns True to proceed."""
        rp = CFG.get("runpod", {})
        try:
            max_run = int(rp.get("max_runtime_minutes", 0))   # 0 = no limit (item 8)
            idle    = int(rp.get("idle_timeout_minutes", 15))
        except (TypeError, ValueError):
            return True
        if max_run <= 0 and idle <= 0:
            return messagebox.askyesno(
                APP_TITLE,
                "Both dead-man's-switch limits are 0 — max runtime AND idle "
                "timeout.\n\nThe pod will NOT stop itself, so if the app crashes "
                "or loses connection it keeps billing until you stop it manually "
                "in the RunPod dashboard.\n\nSet an idle timeout in the RunPod "
                "tab for a safety net.\n\nStart anyway?")
        return True

    def on_exit(self, code):
        """Subclasses override for their own UI, then call super().on_exit(code)
        so the shared MQTT 'task finished' state is published once."""
        # Drop the blue 'currently processing' frame so the final image shows its
        # own green/red outcome instead of staying highlighted after the run ends.
        strip = getattr(self, "strip", None)
        if strip is not None:
            strip.clear_current()
        # The run is over: its pod (if any) is no longer protected from terminate.
        if self.active_pod_id is not None:
            self.active_pod_id = None
            self.app.notify_active_pods_changed()
        self.app.taskbar_clear()        # remove the taskbar progress/error bar
        self._stop_telemetry()
        # One immediate idle sample so the row reflects VRAM freeing up right
        # after the run, rather than waiting for the next idle tick. The proc is
        # already cleared by this point, so this counts as an idle reading.
        self.app.sample_telemetry()
        self._publish_task_idle()

    def _publish_task_idle(self):
        """MQTT: the task ended — go idle and publish a last_run summary."""
        if self.app.mqtt is None:
            return
        values = {
            mqtt_publisher.TASK_NAME_TOPIC:     "idle",
            mqtt_publisher.TASK_PROGRESS_TOPIC: "",
            mqtt_publisher.TASK_ETA_TOPIC:      "",
        }
        if self._last_done:
            try:
                summary = json.loads(self._last_done)
            except (ValueError, TypeError):
                summary = {"raw": self._last_done}
            summary.setdefault("tool", self.mqtt_task_name)
            summary["finished_at"] = datetime.datetime.now().isoformat(timespec="seconds")
            values[mqtt_publisher.LAST_RUN_TOPIC] = json.dumps(summary)
        self.app.mqtt_publish(values)

    # ── System telemetry (Feature #3a) ───────────────────────────────────────

    def _on_image_started(self, path):
        """Hook: an image just began processing. Overridden by the upscaler to
        sample telemetry a few seconds in (past the load/ramp); no-op here."""

    def _start_telemetry(self):
        """Begin sampling for this run. Periodic tabs poll on a fixed interval;
        the upscaler is event-driven (per image), so it has no interval set."""
        self._stop_telemetry()
        if self.telemetry_interval_ms:
            self._telemetry_tick()

    def _telemetry_tick(self):
        self.app.sample_telemetry()
        self._telemetry_job = self.after(self.telemetry_interval_ms,
                                         self._telemetry_tick)

    def _stop_telemetry(self):
        """Cancel this run's sampler timers. The app's idle sampler keeps the
        row updated between runs, so it is not blanked here."""
        for attr in ("_telemetry_job", "_telemetry_img_job"):
            job = getattr(self, attr)
            if job is not None:
                try:
                    self.after_cancel(job)
                except Exception:
                    pass
                setattr(self, attr, None)


# ─────────────────────────────────────────────
#  TAB 1 — BATCH UPSCALER
# ─────────────────────────────────────────────

# Marker prefix batch_upscale.py uses for GUI event lines ("KIND|payload") —
# intercepted here, never shown in the log.
# The FilmStrip thumbnail wall (+ its cell-size constants) now lives in
# gui/filmstrip.py. CELL_DEFAULT is re-imported because ToolTab reads it.
