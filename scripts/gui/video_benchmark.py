"""
gui/video_benchmark.py
----------------------
The per-card VRAM benchmark modal (feature #7, docs/local-video-upscaler.md 16/20). A
one-click "Benchmark this GPU" window for LOCAL video: it detects the card, loads any
prior results, shows a rough runtime estimate, and (on approval) drives
scripts/video_benchmark.py to sweep each selected target upward to its ceiling. Results
persist per probe, so Stop is graceful and re-opening RESUMES from the nearest untried
batch. The findings feed the sizer (AUTO batch) and the local time estimate automatically.
"""

import os
import json
import time
import queue
import codecs
import threading
import subprocess
import tkinter as tk
from tkinter import ttk, messagebox

import db
import video_benchmark as vb
import video_estimate as ve
import video_vram_sizer as sizer
from gui.common import (APP_ROOT, APP_TITLE, CFG, GUI_MARKER, PYTHON_EXE, SCRIPT_DIR,
                        CREATE_NO_WINDOW, _geometry_on_screen, save_settings,
                        fmt_funds, funds_color, config_funds_floor, _FUNDS_GREY)
from gui.widgets import ConsoleBuffer, LogPane, TelemetryRow, Tooltip


def _fmt_hms(seconds):
    s = max(0, int(round(seconds or 0)))
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


class BenchmarkWindow(tk.Toplevel):
    """Modal benchmark window. Standalone (not a ToolTab): it owns a small subprocess pump
    for the video_benchmark runner's @@TBX@@ events (BSTART / BCELL / BPROBE / BCEILING /
    BDONE) and renders a per-target results table live."""

    # Driven off the runner's table so the two never drift: the 4:3 ladder + 16:9 presets.
    ALL_TARGETS = list(vb.TARGETS.keys())
    # Above this much VRAM already occupied (beyond normal desktop overhead), other apps are
    # holding VRAM and a benchmark would under-report the ceiling: warn (but don't block).
    VRAM_BUSY_WARN_GB = 2.5

    def __init__(self, master, tab, remote=False, gpu=None):
        super().__init__(master)
        # Build hidden, reveal once fully laid out: a Toplevel is mapped at a default size
        # first, which flashed a tiny square before our geometry + widgets applied.
        self.withdraw()
        self.tab = tab
        self.app = getattr(tab, "app", None)
        # Remote mode (docs section 22): benchmark a rented pod GPU instead of the local
        # card. `gpu` is the picker's selection ({id, name, memory_gb, price, stock}); its
        # id IS the RunPod id the run reads its learned batch under, so no local detect.
        self.remote = bool(remote)
        self.remote_gpu = gpu or {}
        self._remote_rate = None            # pod's billed $/h (RCOST); live accrued cost
        self._run_start = None              # when the pod went live (billing clock)
        self._funds_job = None              # after() id for the remote balance poller
        self.title(f"{APP_TITLE} — Benchmark GPU" + (" (Remote)" if self.remote else ""))
        try:
            self.iconbitmap(os.path.join(APP_ROOT, "app.ico"))
        except Exception:
            pass
        geo = self.app.settings.get("benchmark_geometry") if self.app is not None else None
        self.geometry(geo if (geo and _geometry_on_screen(self, geo)) else "900x560")
        self.minsize(900, 480)                             # match the main window's min width
        # NOT transient(master): while the benchmark is up we MINIMIZE the main window (see
        # _hide_master / _restore_master) so a non-technical user can't reach the tabs behind
        # it and start a conflicting GPU job. A transient child is auto-hidden when its master
        # is iconified, which would hide the benchmark itself, so it must be independent.
        self._master_win = master

        self.proc = None
        self.console = ConsoleBuffer()
        self._queue_io = queue.Queue()
        self._hold = ""
        self._marker_buf = None
        self.gpu_id = None
        # The regime-tagged video_bench key the RUNNER will write under, resolved by the runner's
        # own helper so the two can never disagree. Read _resolve_bench_key before changing this.
        self.model_tag = self._resolve_bench_key()
        self.total_vram_gb = 0
        self.target_vars = {}
        self._rows = {}                     # target -> tree iid

        self._build()
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.bind("<Configure>", self._track_geometry, add="+")
        self.deiconify()                    # reveal now that it's fully built (no flash)
        # No modal grab_set(): an application grab makes Windows swallow the title-bar MINIMIZE
        # and MAXIMIZE system commands (SC_MINIMIZE/SC_MAXIMIZE), so a long remote sweep (hours)
        # could not be minimized out of the way -- only edge-drag resizing still worked. The grab
        # was only a belt-and-suspenders guard against a second benchmark window (already blocked
        # by the tab's _benchmark_win reuse guard) and a conflicting local GPU job (already
        # blocked by _hide_master iconifying the main window + the Benchmark button's
        # local_gpu_job_running() backstop), so dropping it costs no real protection.
        self.after(80, self._detect_gpu)
        self._hide_master()

    def _hide_master(self):
        """Minimize the main app window for the benchmark's lifetime (restored in _close)."""
        try:
            if self._master_win is not None and self._master_win.winfo_exists():
                self._master_win.iconify()
        except Exception:                              # noqa: BLE001
            pass

    def _restore_master(self):
        try:
            if self._master_win is not None and self._master_win.winfo_exists():
                self._master_win.deiconify()
                self._master_win.lift()
        except Exception:                              # noqa: BLE001
            pass

    # ── layout ───────────────────────────────────────────────────────────────

    def _resolve_bench_key(self):
        """The video_bench key this window must READ, which is whatever the runner will WRITE:
        the model tag plus a tag per setting that moves the numbers (VAE tiling, torch.compile).

        A bare model tag was wrong whenever either feature was on. With compile enabled the
        runner wrote "7b|c" while this window read "7b", so the results table showed the STALE
        UNCOMPILED cell (ceiling 125, 0.54 s/f) for the entire compiled sweep, and the resume
        estimate counted those rows as work already done.

        Resolved ONCE at open, not per refresh: the local branch runs the compile gate, which
        verifies the toolchain by actually compiling. That is cached per process but costs a
        second on first call, and _refresh_estimate fires on every checkbox tick. Fail-safe: any
        problem falls back to the bare display tag (a wrong table beats no window)."""
        try:
            return vb.resolve_bench_key(remote=self.remote, log_fn=lambda *_a, **_k: None)
        except Exception:
            return sizer.model_tag(CFG.get("video", {}).get(
                "dit_model", "seedvr2_ema_7b_fp16.safetensors"))

    @staticmethod
    def _target_label(t):
        """Checkbox text: dims-named ladder cells show the dimensions (with ×) + aspect, preset
        names keep their name; both carry the output megapixels so the VRAM cost is visible at a
        glance (the ceiling scales with output MP)."""
        w, h = vb.TARGETS[t][:2]
        mp = w * h / 1_000_000.0
        if "x" in t:
            return f"{w}×{h} {'3:4' if h > w else '4:3'} · {mp:.1f} MP"
        return f"{t} · {mp:.1f} MP"

    def _is_ladder(self, t):
        """True for a dims-named 4:3 ladder cell (vs a 16:9 preset like 1080p/1440p/4K)."""
        return "x" in t

    def _build(self):
        self.columnconfigure(0, weight=1)
        pad = dict(padx=10)

        self.header_var = tk.StringVar(value="Detecting GPU …")
        tk.Label(self, textvariable=self.header_var, anchor="w",
                 font=("Segoe UI", 10, "bold")).grid(row=0, column=0, sticky="ew", pady=(10, 2), **pad)

        if self.remote:
            info = ("Deploys a pod for the selected GPU and upscales a short generated clip at "
                    "rising batch sizes until it runs out of VRAM, to find the largest safe "
                    "window per target on THAT card. Results are saved and used automatically "
                    "for remote runs on this GPU (batch sizing). The pod is billed while it "
                    "runs; stopping is safe (it tears the pod down and resumes where it left "
                    "off next time).")
        else:
            info = ("Upscales a short generated clip at rising batch sizes until it runs out of "
                    "VRAM, to find the largest safe window per target on THIS card. Results are "
                    "saved and used automatically for local runs (batch sizing + time estimate). "
                    "Stopping is safe: it resumes where it left off.")
        tk.Message(self, text=info, width=680, anchor="w", justify="left",
                   fg="#7f8a99").grid(row=1, column=0, sticky="ew", **pad)

        # Target selection. Too many for one row now (4:3 ladder + presets), so wrap into a grid;
        # the default check-state is set later in _fill_gpu, once the card's VRAM is known.
        tf = ttk.LabelFrame(self, text=" Targets to benchmark ", padding=8)
        tf.grid(row=2, column=0, sticky="ew", pady=(8, 0), **pad)
        self._toggles = []
        self._target_cb = {}
        cols = 4
        for i, t in enumerate(self.ALL_TARGETS):
            v = tk.BooleanVar(value=False)
            v.trace_add("write", lambda *_a: self._refresh_estimate())
            self.target_vars[t] = v
            cb = ttk.Checkbutton(tf, text=self._target_label(t), variable=v)
            cb.grid(row=i // cols, column=i % cols, padx=(0, 16), pady=(0, 4), sticky="w")
            self._toggles.append(cb)
            self._target_cb[t] = cb
        self.restart_var = tk.BooleanVar(value=False)
        rb = ttk.Checkbutton(tf, text="Restart (discard saved results for the ticked targets)",
                             variable=self.restart_var, command=self._refresh_estimate)
        rb.grid(row=(len(self.ALL_TARGETS) + cols - 1) // cols, column=0, columnspan=cols,
                sticky="w", pady=(6, 0))
        self._toggles.append(rb)
        Tooltip(rb, "Off (default): a new run RESUMES, keeping finished probes.\n"
                    "On: clears the saved probes for the TICKED targets only and measures\n"
                    "them from scratch. Targets you did not tick keep their results.")

        # Results table (fixed 5 rows) + the program-output log below it, filling the rest.
        rf = ttk.LabelFrame(self, text=" Results & log ", padding=4)
        rf.grid(row=3, column=0, sticky="nsew", pady=(8, 0), **pad)
        self.rowconfigure(3, weight=1)
        rf.rowconfigure(1, weight=1)          # the log grows; the 5-row table stays put
        rf.columnconfigure(0, weight=1)
        cols = ("ceiling", "saved", "overlap", "spf", "peak", "status", "runtime")
        self.tree = ttk.Treeview(rf, columns=cols, show="tree headings", height=5)
        self.tree.column("#0", width=110, stretch=False)
        self.tree.heading("#0", text="Target")
        # "Max batch" = how far the card pushed (the raw ceiling); "Used" = the batch AUTO
        # actually runs (the fastest window, which can be lower: the ceiling rides VRAM spill).
        # "Runtime" = the GPU time this target's probes took (summed from the saved probes, so it
        # persists and re-accumulates correctly on resume).
        for c, txt, w in (("ceiling", "Max batch", 84), ("saved", "Used", 60),
                          ("overlap", "Overlap", 66), ("spf", "s/frame", 74),
                          ("peak", "Peak VRAM", 104), ("status", "Status", 180),
                          ("runtime", "Runtime", 78)):
            self.tree.heading(c, text=txt)
            self.tree.column(c, width=w, stretch=(c == "status"),
                             anchor=("e" if c == "runtime" else "w"))
        self.tree.grid(row=0, column=0, sticky="ew")
        sb = ttk.Scrollbar(rf, orient="vertical", command=self.tree.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=sb.set)

        logf = ttk.Frame(rf)
        logf.grid(row=1, column=0, columnspan=2, sticky="nsew", pady=(8, 0))
        logf.rowconfigure(1, weight=1)
        logf.columnconfigure(0, weight=1)
        ttk.Label(logf, text="Program output (no engine diagnostics)",
                  foreground="#666").grid(row=0, column=0, sticky="w")
        # Auto scroll: on (default) follows the newest line; off freezes the view so the user
        # can read back through a running sweep without being yanked to the bottom. Mirrors the
        # floating LogViewer's toggle.
        self.autoscroll = tk.BooleanVar(value=True)
        ttk.Checkbutton(logf, text="Auto scroll", variable=self.autoscroll).grid(
            row=0, column=1, sticky="e")
        self.log_pane = LogPane(logf)
        self.log_pane.grid(row=1, column=0, columnspan=2, sticky="nsew")
        # Mirror the clean output stream into the embedded pane (same observer pattern the
        # old floating log window used).
        self.console.add_observer(self._on_console)

        # VRAM-contention warning (shown only when other apps are holding VRAM).
        self.vram_warn_var = tk.StringVar(value="")
        self.vram_warn_lbl = tk.Label(self, textvariable=self.vram_warn_var, anchor="w",
                                      fg="#b23b3b", wraplength=680, justify="left")
        self.vram_warn_lbl.grid(row=4, column=0, sticky="ew", pady=(6, 0), **pad)
        self.vram_warn_lbl.grid_remove()

        # Estimate / live pod-cost (left) + the RunPod account balance (right), so the
        # money picture is visible in one glance during a paid sweep, mirroring the
        # bottom-bar "Funds" readout the remote-capable tabs use. Funds is REMOTE-only
        # (a local sweep bills nothing) and colour-banded against the funds floor.
        cost_row = ttk.Frame(self)
        cost_row.grid(row=5, column=0, sticky="ew", pady=(6, 0), **pad)
        cost_row.columnconfigure(0, weight=1)
        self.estimate_var = tk.StringVar(value="")
        tk.Label(cost_row, textvariable=self.estimate_var, anchor="w",
                 fg="#2f6f3f").grid(row=0, column=0, sticky="ew")
        if self.remote:
            self.funds_var = tk.StringVar(value="Funds: …")
            self.funds_lbl = tk.Label(cost_row, textvariable=self.funds_var, anchor="e",
                                      fg=_FUNDS_GREY, font=("Consolas", 9))
            self.funds_lbl.grid(row=0, column=1, sticky="e", padx=(12, 0))
            Tooltip(self.funds_lbl,
                    "Your RunPod account balance, coloured by how far it sits above the "
                    "configured funds floor. The funds guard auto-stops the sweep if it "
                    "nears the floor. Unreadable balance never blocks a run (fail-safe).")
            self.after(300, self._tick_funds)           # live from window-open, not just mid-run
        self.status_var = tk.StringVar(value="")
        tk.Label(self, textvariable=self.status_var, anchor="w", fg="#7f8a99",
                 font=("Consolas", 9)).grid(row=6, column=0, sticky="ew", **pad)

        # Live telemetry row, matching the Upscaler/Video tabs (Feature #3a/#4) and
        # published to MQTT. LOCAL: this machine's CPU/RAM/GPU (App feeds it + system/*).
        # REMOTE: the POD's readout, fed by the runner's RTELEM stream via
        # App.apply_remote_telemetry (+ system/remote/*). Only the mode's row is created.
        if self.remote:
            self.remote_telemetry_row = TelemetryRow(self, prefix="Remote pod")
            self.remote_telemetry_row.grid(row=7, column=0, sticky="ew", pady=(4, 0), **pad)
            self.remote_telemetry_row.grid_remove()     # revealed on the first sample
        else:
            self.telemetry_row = TelemetryRow(self)
            self.telemetry_row.grid(row=7, column=0, sticky="ew", pady=(4, 0), **pad)
            # Register with the App so its sampler (idle + our faster in-run tick) feeds this
            # row AND publishes system/*; unregistered on close so the destroyed widget leaks
            # nothing. Kick one sample now so it is live immediately, not "sampling…".
            if self.app is not None and self.telemetry_row not in self.app.telemetry_rows:
                self.app.telemetry_rows.append(self.telemetry_row)
                self.after(200, self.app.sample_telemetry)

        # Buttons.
        bf = ttk.Frame(self)
        bf.grid(row=8, column=0, sticky="ew", pady=(8, 10), **pad)
        bf.columnconfigure(2, weight=1)
        self.start_btn = ttk.Button(bf, text="Start", command=self._start, state="disabled")
        self.start_btn.grid(row=0, column=0)
        self.stop_btn = ttk.Button(bf, text="Stop", command=self._stop, state="disabled")
        self.stop_btn.grid(row=0, column=1, padx=(6, 0))
        self.close_btn = ttk.Button(bf, text="Close", command=self._close)
        self.close_btn.grid(row=0, column=3)

    def _track_geometry(self, _e=None):
        if self.app is not None and self.state() == "normal":
            self._geo = self.geometry()

    # ── GPU detection + prior results ────────────────────────────────────────

    def _detect_gpu(self):
        # Remote: the card is whatever the user picked in the GPU list, not a local probe.
        # Shape it like sample_gpu's (used_mb, total_mb, temp) so _fill_gpu is unchanged;
        # there is no live "used VRAM" for a pod that isn't deployed yet, so used=0.
        if self.remote:
            vram_gb = self.remote_gpu.get("memory_gb") or 0
            g = (0, int(vram_gb) * 1024, None)
            name = self.remote_gpu.get("id") or self.remote_gpu.get("name") or "remote GPU"
            self.after(0, lambda: self._fill_gpu(g, name))
            return

        def work():
            try:
                import system_telemetry as st
                g = st.sample_gpu()
                name = st.gpu_name()
            except Exception:                          # noqa: BLE001
                g, name = None, None
            self.after(0, lambda: self._fill_gpu(g, name))
        threading.Thread(target=work, daemon=True).start()

    def _fill_gpu(self, g, name):
        if not g:
            self.header_var.set("No NVIDIA GPU detected — local benchmark unavailable.")
            self.estimate_var.set("Local upscaling needs a CUDA GPU.")
            return
        self.gpu_id = name or "local"
        self.total_vram_gb = round((g[1] or 0) / 1024.0)
        self.header_var.set(f"{self.gpu_id} — {self.total_vram_gb} GB VRAM — model {self.model_tag}")
        # Default-check the targets this card can plausibly reach (all stay toggleable). Pre-check
        # every feasible LANDSCAPE 4:3 ladder cell (the common old-camera case); PORTRAIT cells
        # stay available but unchecked (you only need them if you shot portrait, and they'd double
        # the default run time). Add the 16:9 presets only on cards big enough for them.
        max_mp = ve.max_output_mp(self.total_vram_gb, self.gpu_id, self.conn)
        for t in self.ALL_TARGETS:
            if self._is_ladder(t):
                w, h = vb.TARGETS[t][:2]
                if max_mp and w >= h and (w * h / 1_000_000.0) <= max_mp + 1e-6:
                    self.target_vars[t].set(True)
        if self.total_vram_gb >= 40:
            self.target_vars["1440p"].set(True)
        if self.total_vram_gb >= 80:
            self.target_vars["4K"].set(True)
        self._load_prior()
        self._apply_feasibility()
        self.start_btn.configure(state="normal")
        self._refresh_estimate()
        self._refresh_vram_warning(g)

    def _apply_feasibility(self):
        """Disable targets this card can't reach: a 24 GB card OOMs on 1440p/4K SeedVR2, so
        benchmarking them is a guaranteed failure. Gate on the same max-output-MP model the
        Target picker uses (seed by VRAM, raised by whatever this card has already proven), so
        anything measured feasible stays selectable."""
        max_mp = ve.max_output_mp(self.total_vram_gb, self.gpu_id, self.conn)
        if not max_mp:
            return
        for t in self.ALL_TARGETS:
            w, h = vb.TARGETS[t][:2]
            box_mp = w * h / 1_000_000.0
            cb = self._target_cb.get(t)
            if box_mp > max_mp + 1e-6:
                self.target_vars[t].set(False)
                if cb is not None:
                    cb.configure(state="disabled")
                    if not getattr(cb, "_infeasible_tip", False):
                        Tooltip(cb, f"Needs more VRAM than this card has: {t} ({box_mp:.1f} MP) "
                                   f"exceeds the ~{max_mp:.1f} MP ceiling of a "
                                   f"{self.total_vram_gb} GB GPU for SeedVR2.")
                        cb._infeasible_tip = True
                self.tree.set(self._ensure_row(t), "status", "not supported on this card (VRAM)")
            elif cb is not None:
                cb.configure(state="normal")

    def _sample_used_vram(self):
        """(used_gb, total_gb) for GPU 0 via nvidia-smi, or (None, None). Best-effort."""
        try:
            import system_telemetry as st
            g = st.sample_gpu()                        # (used_mb, total_mb, temp) or None
            if g:
                return g[0] / 1024.0, g[1] / 1024.0
        except Exception:                              # noqa: BLE001
            pass
        return None, None

    def _refresh_vram_warning(self, g=None):
        """Show the contention warning iff other apps are already holding VRAM (> the
        threshold). Purely advisory: benchmarking with busy VRAM under-reports the ceiling."""
        used = (g[0] / 1024.0) if g else self._sample_used_vram()[0]
        if used is not None and used > self.VRAM_BUSY_WARN_GB:
            self.vram_warn_var.set(
                f"⚠ {used:.1f} GB of VRAM is already in use. Close all non-essential "
                "applications for best benchmarking results — otherwise the measured ceiling "
                "will be lower than this card can really do.")
            self.vram_warn_lbl.grid()
        else:
            self.vram_warn_var.set("")
            self.vram_warn_lbl.grid_remove()

    def _load_prior(self):
        """Pre-fill the table from saved probes (a resumable prior run)."""
        for t in self.ALL_TARGETS:
            self._ensure_row(t)
            box = vb.TARGETS[t]
            probes = vb.drop_collapsed(db.get_bench_probes(
                self.conn, self.gpu_id, self.model_tag, box[0], box[1]))
            if not probes:
                self.tree.set(self._rows[t], "status", "not benchmarked")
                self._set_runtime(t)
                continue
            ceil = vb.cell_ceiling(probes)
            saved = vb.throughput_optimal_batch(probes)
            done = vb.cell_done(probes)
            self._set_ceiling(t, ceil, "saved" if done else "partial (resumable)", saved=saved)

    @property
    def conn(self):
        return db.get_conn()

    def _ensure_row(self, target):
        if target not in self._rows:
            self._rows[target] = self.tree.insert("", "end", text=target)
        return self._rows[target]

    def _set_ceiling(self, target, ceil, status, saved=None):
        iid = self._ensure_row(target)
        self.tree.set(iid, "ceiling", str(ceil) if ceil else "—")
        self.tree.set(iid, "saved", str(saved) if saved else "—")
        # Overlap reflects the batch AUTO will actually run: the saved (fastest) one, else the
        # ceiling while a sweep is still in flight and the optimum isn't known yet.
        ov_b = saved or ceil
        self.tree.set(iid, "overlap", str(sizer.auto_overlap(ov_b)) if ov_b else "—")
        self.tree.set(iid, "status", status)
        self._set_runtime(target)

    def _cell_runtime_s(self, target):
        """Total GPU time this target's probes took = the sum of the saved probes' seconds. Read
        from the DB (the runner records each probe before signalling the GUI, and upserts a
        re-probe), so it is correct live, persists, and re-accumulates on resume."""
        if not self.gpu_id:
            return 0.0
        w, h = vb.TARGETS[target][:2]
        probes = db.get_bench_probes(self.conn, self.gpu_id, self.model_tag, w, h)
        return sum((p["seconds"] or 0) for p in probes)

    def _set_runtime(self, target):
        s = self._cell_runtime_s(target)
        self.tree.set(self._ensure_row(target), "runtime", _fmt_hms(s) if s else "—")

    def _selected_targets(self):
        return [t for t in self.ALL_TARGETS if self.target_vars[t].get()]

    def _refresh_estimate(self):
        if not self.gpu_id:
            return
        targets = self._selected_targets()
        if not targets:
            self.estimate_var.set("Select at least one target.")
            self.start_btn.configure(state="disabled")
            return
        self.start_btn.configure(state="normal" if self.proc is None else "disabled")
        plan = vb.build_plan(targets)
        done = 0
        if not self.restart_var.get():
            for c in plan:
                done += len(db.get_bench_probes(self.conn, self.gpu_id, self.model_tag,
                                                c["out_w"], c["out_h"]))
        est = vb.estimate_runtime(plan, self.gpu_id, self.conn, done=done)
        verb = "Restart" if self.restart_var.get() else ("Resume" if done else "Run")
        self.start_btn.configure(text=verb if self.proc is None else verb)
        self.estimate_var.set(f"{verb}: {len(targets)} target(s) · estimated ~{_fmt_hms(est)} "
                              f"(rough; the card's real speed is unknown until measured).")

    # ── run / stop ───────────────────────────────────────────────────────────

    def _start(self):
        if self.proc is not None or not self.gpu_id:
            return
        targets = self._selected_targets()
        if not targets:
            return
        # LOCAL only: re-check VRAM occupancy at the last moment and warn (don't block) if
        # other apps are holding VRAM, since a contended sweep records an artificially low
        # ceiling. A pod is dedicated, so this never applies remotely.
        if not self.remote:
            used, _tot = self._sample_used_vram()
            self._refresh_vram_warning()
            if used is not None and used > self.VRAM_BUSY_WARN_GB:
                if not messagebox.askyesno(
                        APP_TITLE,
                        f"{used:.1f} GB of VRAM is already in use by other applications.\n\n"
                        "Close all non-essential applications for best benchmarking results. "
                        "Running now will measure a lower ceiling than this card can really do.\n\n"
                        "Benchmark anyway?", parent=self):
                    return
        if self.restart_var.get():
            # Name the targets. The old wording ("this card's saved results") described a
            # card-wide wipe and would have been an accurate warning for the old behaviour;
            # spelling out the list is what makes the narrowed scope checkable by the user.
            names = "\n".join(f"  - {self._target_label(t)}" for t in targets)
            if not messagebox.askyesno(
                    APP_TITLE,
                    f"Discard the saved benchmark results for these {len(targets)} target(s) "
                    f"and measure them from scratch?\n\n{names}\n\n"
                    "Any other target measured on this card keeps its results.",
                    parent=self):
                return
        cmd = [PYTHON_EXE, "-u", os.path.join(SCRIPT_DIR, "video_benchmark.py"),
               "--targets", ",".join(targets)]
        if self.remote:
            cmd.append("--remote")
        if self.restart_var.get():
            cmd.append("--restart")
            for t in targets:                          # clear the table rows we're redoing
                self._set_ceiling(t, None, "queued")
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"
        if self.remote:
            # The runner keys the learned batch under this id (what a remote run reads),
            # and RemoteSession deploys exactly this card (no substitution, 0.4.0).
            env["IMGTBX_GPU_OVERRIDE"] = self.remote_gpu.get("id", "")
            # The picked card's VRAM seeds the runner's VRAM-aware batch floor (skip the low
            # rungs a big card obviously clears). Blank if the picker didn't carry it.
            env["IMGTBX_GPU_VRAM_GB"] = str(self.remote_gpu.get("memory_gb") or "")
            self._run_start = time.time()              # billing clock (refined by the POD event)
        try:
            self.proc = subprocess.Popen(
                cmd, cwd=APP_ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, creationflags=CREATE_NO_WINDOW, env=env)
        except Exception as exc:                       # noqa: BLE001
            messagebox.showerror(APP_TITLE, f"Could not start the benchmark:\n{exc}",
                                 parent=self)
            self.proc = None
            return
        self._hold = ""
        self._marker_buf = None
        self.console.clear()
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.close_btn.configure(state="disabled")
        for w in self._toggles:                        # the plan is fixed for the run
            w.configure(state="disabled")
        # The "X GB already in use" warning is a pre-start hint; once a sweep is running that
        # figure is stale (the benchmark itself now holds VRAM), so hide it.
        self.vram_warn_var.set("")
        self.vram_warn_lbl.grid_remove()
        self.status_var.set("Deploying pod …" if self.remote else "Starting benchmark …")
        threading.Thread(target=self._pump, daemon=True).start()
        self.after(50, self._poll)
        if self.remote:
            self.after(1000, self._tick_cost)
        else:
            self.after(1000, self._telemetry_tick)

    def _telemetry_tick(self):
        """Faster LOCAL-GPU sampling while a local sweep runs (the App idle sampler is a
        slow 60 s), so VRAM climbing with the batch is visible live. Self-cancels when the
        run ends; the App's idle sampler keeps the row live afterwards. Remote pods report
        through RTELEM instead, so this is local-only."""
        if self.proc is None or self.remote or self.app is None:
            return
        try:
            self.app.sample_telemetry()
        except Exception:                              # noqa: BLE001
            pass
        self.after(5000, self._telemetry_tick)

    def _tick_cost(self):
        """Live accrued-cost readout for a remote sweep (no cap; the funds guard is the
        safety net). Self-cancels when the run ends; the last value stays on screen."""
        if self.proc is None or not self.remote:
            return
        if self._remote_rate and self._run_start:
            spent = self._remote_rate * (time.time() - self._run_start) / 3600.0
            self.estimate_var.set(f"Pod cost so far: ${spent:.2f}  (${self._remote_rate:.2f}/h)")
        self.after(1000, self._tick_cost)

    def _tick_funds(self):
        """Live RunPod balance readout for a remote sweep, mirroring the bottom-bar 'Funds'.
        Reads the App's SHARED balance cache (one fetch source for the whole app) and nudges a
        refresh; the App's fetch is 30 s-gated + off-thread, so this never hammers the API.
        Runs from window-open (the balance matters before you start a paid sweep) for the
        window's lifetime; self-cancels once the window is gone. Fail-safe throughout."""
        if not self.remote:
            return
        try:
            if not self.winfo_exists():
                return
        except Exception:
            return
        try:
            if self.app is not None:
                self.app._fetch_funds_async()          # 30 s-gated, off-thread, fail-safe
            text, bal = fmt_funds(getattr(self.app, "_funds_cache", None))
            self.funds_var.set(f"Funds: {text}")
            self.funds_lbl.configure(fg=funds_color(bal, config_funds_floor()))
        except Exception:                              # noqa: BLE001
            pass
        self._funds_job = self.after(5000, self._tick_funds)

    def _stop(self):
        if self.proc and self.proc.poll() is None:
            self.status_var.set("Stopping after the current probe …")
            try:
                self.proc.stdin.write(b"q\n")
                self.proc.stdin.flush()
            except Exception:                          # noqa: BLE001
                pass
        self.stop_btn.configure(state="disabled")

    # ── subprocess pump (compact @@TBX@@ parser, mirrors tab_video) ───────────

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
            if self.proc:
                self.proc.wait()
            self.proc = None
            self._on_finished()
        else:
            self.after(50, self._poll)

    def _filter_markers(self, text):
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
        self._handle_event(kind, data)

    def _handle_event(self, kind, data):
        if kind == "RTELEM" and data and self.app is not None:
            # Pod telemetry (remote sweep): render in the 'Remote pod' row + MQTT
            # system/remote/*, exactly like a real remote run.
            try:
                self.app.apply_remote_telemetry(self, data)
            except Exception:                          # noqa: BLE001
                pass
            return
        if kind == "POD":
            # Remote: pod id (non-empty) => live and billing; empty => torn down.
            if data:
                self._run_start = self._run_start or time.time()
                self.status_var.set(f"Pod live ({data}); benchmarking …")
            return
        if kind == "RCOST":
            try:
                self._remote_rate = float(data)
            except (TypeError, ValueError):
                self._remote_rate = None
            return
        if kind == "BSTART" and data:
            n = len(data.get("plan") or [])
            where = "a pod" if data.get("remote") else data.get("gpu")
            self.status_var.set(f"Benchmarking {n} target(s) on {where} …")
        elif kind == "BCELL" and data:
            self._set_ceiling(data["name"], None, "benchmarking …")
        elif kind == "BPROBE" and data:
            t = data.get("name")
            if data.get("state") == "running":
                self.tree.set(self._ensure_row(t), "status", f"probing batch {data.get('batch')} …")
            elif data.get("state") == "done":
                iid = self._ensure_row(t)
                oc = data.get("outcome")
                if oc == "ok":
                    self.tree.set(iid, "ceiling", str(data.get("batch")))
                    self.tree.set(iid, "overlap", str(sizer.auto_overlap(data.get("batch") or 0)))
                    if data.get("spf") is not None:
                        self.tree.set(iid, "spf", f"{data['spf']:.2f}")
                    pa, pr = data.get("peak_alloc"), data.get("peak_reserved")
                    if pa or pr:
                        self.tree.set(iid, "peak", f"{pa or '?'}/{pr or '?'} GB")
                    self.tree.set(iid, "status", f"batch {data.get('batch')} ok")
                else:
                    self.tree.set(iid, "status", f"batch {data.get('batch')} {oc}")
                self._set_runtime(t)                   # probe recorded -> refresh the target's total
        elif kind == "BCEILING" and data:
            ceil = data.get("ceiling")
            saved = data.get("saved")
            status = (f"done — uses {saved} (max fit {ceil})" if saved and ceil and saved != ceil
                      else f"done — batch {saved}" if saved else "can't do this target")
            self._set_ceiling(data["name"], ceil, status, saved=saved)
        elif kind == "BDONE" and data:
            self.status_var.set("Benchmark stopped." if data.get("stopped") else "Benchmark complete.")

    def _on_finished(self):
        self.stop_btn.configure(state="disabled")
        self.close_btn.configure(state="normal")
        self.start_btn.configure(state="normal")
        for w in self._toggles:
            w.configure(state="normal")
        # The pod is torn down when a remote sweep ends: hide its telemetry row and zero the
        # retained system/remote/* topics so a gone pod leaves no stale readings in HA. A
        # restart deploys a new pod and RTELEM reveals the row again.
        if self.remote and self.app is not None:
            try:
                self.app.clear_remote_telemetry(self)
            except Exception:                          # noqa: BLE001
                pass
        self._apply_feasibility()                      # keep infeasible targets gated
        for t in self.ALL_TARGETS:                     # reflect the persisted final state
            box = vb.TARGETS[t]
            probes = vb.drop_collapsed(db.get_bench_probes(
                self.conn, self.gpu_id, self.model_tag, box[0], box[1]))
            if probes:
                ceil = vb.cell_ceiling(probes)
                saved = vb.throughput_optimal_batch(probes)
                done = vb.cell_done(probes)
                cur = self.tree.set(self._rows.get(t, self._ensure_row(t)), "status")
                if not cur.startswith("done") and not cur.startswith("can't"):
                    self._set_ceiling(t, ceil, "saved" if done else "partial (resumable)",
                                      saved=saved)
        self.restart_var.set(False)
        self._refresh_estimate()
        # Nudge the tab to re-read the (now calibrated) estimate on its next view.
        try:
            if hasattr(self.tab, "_update_estimate"):
                self.tab._update_estimate()
        except Exception:                              # noqa: BLE001
            pass

    def _on_console(self, chunk):
        """Feed the embedded log pane from the console's observer stream (None = cleared)."""
        if not self.winfo_exists():
            return
        if chunk is None:
            self.log_pane.clear()
        else:
            self.log_pane.feed(chunk)
            if self.autoscroll.get():
                self.log_pane.text.see("end")

    def _close(self):
        if self.proc is not None and self.proc.poll() is None:
            if not messagebox.askyesno(
                    APP_TITLE, "A benchmark is running. Stop it and close?\n\n"
                    "(Finished probes are saved and will resume next time.)", parent=self):
                return
            self._stop()
            try:
                self.proc.wait(timeout=10)
            except Exception:                          # noqa: BLE001
                try:
                    self.proc.kill()
                except Exception:                      # noqa: BLE001
                    pass
        if self.app is not None:
            try:
                self.app.settings["benchmark_geometry"] = getattr(self, "_geo", self.geometry())
                save_settings(self.app.settings)
            except Exception:                          # noqa: BLE001
                pass
        try:
            self.console.remove_observer(self._on_console)
        except Exception:                              # noqa: BLE001
            pass
        if self._funds_job is not None:                # stop the balance poller
            try:
                self.after_cancel(self._funds_job)
            except Exception:                          # noqa: BLE001
                pass
            self._funds_job = None
        # Detach telemetry so the destroyed widget leaks nothing and no stale pod readings
        # linger: unregister the local row from the App's sampler; hide + zero the remote row.
        if self.app is not None:
            try:
                row = getattr(self, "telemetry_row", None)
                if row is not None and row in self.app.telemetry_rows:
                    self.app.telemetry_rows.remove(row)
            except Exception:                          # noqa: BLE001
                pass
            if self.remote:
                try:
                    self.app.clear_remote_telemetry(self)
                except Exception:                      # noqa: BLE001
                    pass
        self._restore_master()
        self.destroy()
