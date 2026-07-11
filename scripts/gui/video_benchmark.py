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
                        CREATE_NO_WINDOW, _geometry_on_screen, save_settings)
from gui.widgets import ConsoleBuffer, LogPane, Tooltip


def _fmt_hms(seconds):
    s = max(0, int(round(seconds or 0)))
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


class BenchmarkWindow(tk.Toplevel):
    """Modal benchmark window. Standalone (not a ToolTab): it owns a small subprocess pump
    for the video_benchmark runner's @@TBX@@ events (BSTART / BCELL / BPROBE / BCEILING /
    BDONE) and renders a per-target results table live."""

    ALL_TARGETS = ["1080p", "1440p", "4K"]
    # Above this much VRAM already occupied (beyond normal desktop overhead), other apps are
    # holding VRAM and a benchmark would under-report the ceiling: warn (but don't block).
    VRAM_BUSY_WARN_GB = 2.5

    def __init__(self, master, tab):
        super().__init__(master)
        # Build hidden, reveal once fully laid out: a Toplevel is mapped at a default size
        # first, which flashed a tiny square before our geometry + widgets applied.
        self.withdraw()
        self.tab = tab
        self.app = getattr(tab, "app", None)
        self.title(f"{APP_TITLE} — Benchmark GPU")
        try:
            self.iconbitmap(os.path.join(APP_ROOT, "app.ico"))
        except Exception:
            pass
        geo = self.app.settings.get("benchmark_geometry") if self.app is not None else None
        self.geometry(geo if (geo and _geometry_on_screen(self, geo)) else "720x560")
        self.minsize(640, 480)
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
        self.model_tag = sizer.model_tag(CFG.get("video", {}).get(
            "dit_model", "seedvr2_ema_7b_fp16.safetensors"))
        self.total_vram_gb = 0
        self.target_vars = {}
        self._rows = {}                     # target -> tree iid

        self._build()
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.bind("<Configure>", self._track_geometry, add="+")
        self.deiconify()                    # reveal now that it's fully built (no flash)
        # Modal: the log now lives INSIDE this window, so a local grab no longer freezes a
        # needed helper. Modality also stops a second benchmark window being opened from the
        # main app while this one is up. Deferred so the window is mapped before we grab.
        self.after(60, self._grab)
        self.after(80, self._detect_gpu)
        self._hide_master()

    def _grab(self):
        try:
            self.grab_set()
        except tk.TclError:
            pass

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

    def _build(self):
        self.columnconfigure(0, weight=1)
        pad = dict(padx=10)

        self.header_var = tk.StringVar(value="Detecting GPU …")
        tk.Label(self, textvariable=self.header_var, anchor="w",
                 font=("Segoe UI", 10, "bold")).grid(row=0, column=0, sticky="ew", pady=(10, 2), **pad)

        info = ("Upscales a short generated clip at rising batch sizes until it runs out of "
                "VRAM, to find the largest safe window per target on THIS card. Results are "
                "saved and used automatically for local runs (batch sizing + time estimate). "
                "Stopping is safe: it resumes where it left off.")
        tk.Message(self, text=info, width=680, anchor="w", justify="left",
                   fg="#7f8a99").grid(row=1, column=0, sticky="ew", **pad)

        # Target selection.
        tf = ttk.LabelFrame(self, text=" Targets to benchmark ", padding=8)
        tf.grid(row=2, column=0, sticky="ew", pady=(8, 0), **pad)
        self._toggles = []
        self._target_cb = {}
        for i, t in enumerate(self.ALL_TARGETS):
            v = tk.BooleanVar(value=(t == "1080p"))
            v.trace_add("write", lambda *_a: self._refresh_estimate())
            self.target_vars[t] = v
            cb = ttk.Checkbutton(tf, text=t, variable=v)
            cb.grid(row=0, column=i, padx=(0, 16), sticky="w")
            self._toggles.append(cb)
            self._target_cb[t] = cb
        self.restart_var = tk.BooleanVar(value=False)
        rb = ttk.Checkbutton(tf, text="Restart (discard saved results for this card)",
                             variable=self.restart_var, command=self._refresh_estimate)
        rb.grid(row=1, column=0, columnspan=3, sticky="w", pady=(6, 0))
        self._toggles.append(rb)
        Tooltip(rb, "Off (default): a new run RESUMES, keeping finished probes.\n"
                    "On: clears this card+model's saved probes and benchmarks from scratch.")

        # Results table (fixed 5 rows) + the program-output log below it, filling the rest.
        rf = ttk.LabelFrame(self, text=" Results & log ", padding=4)
        rf.grid(row=3, column=0, sticky="nsew", pady=(8, 0), **pad)
        self.rowconfigure(3, weight=1)
        rf.rowconfigure(1, weight=1)          # the log grows; the 5-row table stays put
        rf.columnconfigure(0, weight=1)
        cols = ("ceiling", "overlap", "spf", "peak", "status")
        self.tree = ttk.Treeview(rf, columns=cols, show="tree headings", height=5)
        self.tree.column("#0", width=110, stretch=False)
        self.tree.heading("#0", text="Target")
        for c, txt, w in (("ceiling", "Max batch", 90), ("overlap", "Overlap", 70),
                          ("spf", "s/frame", 80), ("peak", "Peak VRAM", 110),
                          ("status", "Status", 200)):
            self.tree.heading(c, text=txt)
            self.tree.column(c, width=w, stretch=(c == "status"), anchor="w")
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
        self.log_pane = LogPane(logf)
        self.log_pane.grid(row=1, column=0, sticky="nsew")
        # Mirror the clean output stream into the embedded pane (same observer pattern the
        # old floating log window used).
        self.console.add_observer(self._on_console)

        # VRAM-contention warning (shown only when other apps are holding VRAM).
        self.vram_warn_var = tk.StringVar(value="")
        self.vram_warn_lbl = tk.Label(self, textvariable=self.vram_warn_var, anchor="w",
                                      fg="#b23b3b", wraplength=680, justify="left")
        self.vram_warn_lbl.grid(row=4, column=0, sticky="ew", pady=(6, 0), **pad)
        self.vram_warn_lbl.grid_remove()

        # Estimate + status.
        self.estimate_var = tk.StringVar(value="")
        tk.Label(self, textvariable=self.estimate_var, anchor="w",
                 fg="#2f6f3f").grid(row=5, column=0, sticky="ew", pady=(6, 0), **pad)
        self.status_var = tk.StringVar(value="")
        tk.Label(self, textvariable=self.status_var, anchor="w", fg="#7f8a99",
                 font=("Consolas", 9)).grid(row=6, column=0, sticky="ew", **pad)

        # Buttons.
        bf = ttk.Frame(self)
        bf.grid(row=7, column=0, sticky="ew", pady=(8, 10), **pad)
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
        # Default-check the targets this card can plausibly reach (all stay toggleable).
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
            w, h = vb.TARGETS[t]
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
            probes = db.get_bench_probes(self.conn, self.gpu_id, self.model_tag, box[0], box[1])
            if not probes:
                self.tree.set(self._rows[t], "status", "not benchmarked")
                continue
            ceil = vb.cell_ceiling(probes)
            done = vb.next_batch(probes, vb.batch_series()) is None
            self._set_ceiling(t, ceil, "saved" if done else "partial (resumable)")

    @property
    def conn(self):
        return db.get_conn()

    def _ensure_row(self, target):
        if target not in self._rows:
            self._rows[target] = self.tree.insert("", "end", text=target)
        return self._rows[target]

    def _set_ceiling(self, target, ceil, status):
        iid = self._ensure_row(target)
        if ceil:
            self.tree.set(iid, "ceiling", str(ceil))
            self.tree.set(iid, "overlap", str(sizer.auto_overlap(ceil)))
        else:
            self.tree.set(iid, "ceiling", "—")
            self.tree.set(iid, "overlap", "—")
        self.tree.set(iid, "status", status)

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
        # Re-check VRAM occupancy at the last moment: warn (don't block) if other apps are
        # holding VRAM, since a contended sweep records an artificially low ceiling.
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
            if not messagebox.askyesno(
                    APP_TITLE, "Discard this card's saved benchmark results and start over?",
                    parent=self):
                return
        cmd = [PYTHON_EXE, "-u", os.path.join(SCRIPT_DIR, "video_benchmark.py"),
               "--targets", ",".join(targets)]
        if self.restart_var.get():
            cmd.append("--restart")
            for t in targets:                          # clear the table rows we're redoing
                self._set_ceiling(t, None, "queued")
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"
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
        self.status_var.set("Starting benchmark …")
        threading.Thread(target=self._pump, daemon=True).start()
        self.after(50, self._poll)

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
        if kind == "BSTART" and data:
            n = len(data.get("plan") or [])
            self.status_var.set(f"Benchmarking {n} target(s) on {data.get('gpu')} …")
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
        elif kind == "BCEILING" and data:
            ceil = data.get("ceiling")
            self._set_ceiling(data["name"], ceil,
                              f"done — max batch {ceil}" if ceil else "can't do this target")
        elif kind == "BDONE" and data:
            self.status_var.set("Benchmark stopped." if data.get("stopped") else "Benchmark complete.")

    def _on_finished(self):
        self.stop_btn.configure(state="disabled")
        self.close_btn.configure(state="normal")
        self.start_btn.configure(state="normal")
        for w in self._toggles:
            w.configure(state="normal")
        self._apply_feasibility()                      # keep infeasible targets gated
        for t in self.ALL_TARGETS:                     # reflect the persisted final state
            box = vb.TARGETS[t]
            probes = db.get_bench_probes(self.conn, self.gpu_id, self.model_tag, box[0], box[1])
            if probes:
                ceil = vb.cell_ceiling(probes)
                done = vb.next_batch(probes, vb.batch_series()) is None
                cur = self.tree.set(self._rows.get(t, self._ensure_row(t)), "status")
                if not cur.startswith("done") and not cur.startswith("can't"):
                    self._set_ceiling(t, ceil, "saved" if done else "partial (resumable)")
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
        self._restore_master()
        self.destroy()
