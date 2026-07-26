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
import re
import json
import time
import queue
import codecs
import threading
import subprocess
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

import db
import video_benchmark as vb
import video_estimate as ve
import video_vram_sizer as sizer
from gui.common import (APP_ROOT, APP_TITLE, CFG, GUI_MARKER, PYTHON_EXE, SCRIPT_DIR,
                        CREATE_NO_WINDOW, _geometry_on_screen, save_settings,
                        fmt_funds, funds_color, config_funds_floor, _FUNDS_GREY,
                        report_issue, contribute_benchmark)
from gui.widgets import ConsoleBuffer, LogPane, TelemetryRow, Tooltip


def _fmt_hms(seconds):
    s = max(0, int(round(seconds or 0)))
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _benchmark_methods():
    """(label, engine, tier) rows for the benchmark Method combobox: SeedVR2 (batch sweep) then a
    single unified 'Real-ESRGAN' entry (fixed-ratio, #18 B) that sweeps ALL tiers in one run/pod
    (tier=None). Fail-safe: SeedVR2 alone if the esrgan catalog can't be imported."""
    opts = [("SeedVR2", "seedvr2", None)]
    try:
        import esrgan_models        # noqa: F401 (presence check only)
        opts.append(("Real-ESRGAN", "fixed_ratio", None))
    except Exception:               # noqa: BLE001
        pass
    return opts


class BenchmarkWindow(tk.Toplevel):
    """Modal benchmark window. Standalone (not a ToolTab): it owns a small subprocess pump
    for the video_benchmark runner's @@TBX@@ events (BSTART / BCELL / BPROBE / BCEILING /
    BDONE) and renders a per-target results table live."""

    # Driven off the runner's table so the two never drift: the 4:3 ladder + 16:9 presets.
    ALL_TARGETS = list(vb.TARGETS.keys())
    # Above this much VRAM already occupied (beyond normal desktop overhead), other apps are
    # holding VRAM and a benchmark would under-report the ceiling: warn (but don't block).
    VRAM_BUSY_WARN_GB = 2.5

    def __init__(self, master, tab, remote=False, gpu=None, method=None):
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
        # First-ever open is 980x720; after that the remembered geometry wins. Its own
        # numbers, sized by the results table (the main window's minimum is set by the
        # video queue's column count and is deliberately unrelated).
        self.geometry(geo if (geo and _geometry_on_screen(self, geo)) else "980x720")
        self.minsize(900, 480)
        # NOT transient(master): while the benchmark is up we fully HIDE the main window (see
        # _hide_master / _restore_master) so a non-technical user can't reach the tabs behind
        # it and start a conflicting GPU job. A transient child is auto-hidden when its master
        # is hidden, which would hide the benchmark itself, so it must be independent. The window
        # keeps its OWN taskbar button (it is non-transient + never iconified at open), so the app
        # still shows in the taskbar while the main window is withdrawn.
        self._master_win = master
        self._closing = False               # set once the master is deliberately restored

        self.proc = None
        self.console = ConsoleBuffer()
        self._queue_io = queue.Queue()
        self._hold = ""
        self._marker_buf = None
        self.gpu_id = None
        # Which upscaling METHOD this window benchmarks: SeedVR2 (batch sweep) or Real-ESRGAN
        # (fixed-ratio, #18 B). Switchable live via the Method combobox. `method` (engine, tier)
        # from the caller pre-selects it; else the configured video.engine wins.
        self._methods = _benchmark_methods()
        self._method_label_to_val = {lbl: (e, t) for lbl, e, t in self._methods}
        self._engine, self._tier = method or self._default_method()
        self._configure_method()            # sets self._cells + self._ratio_tokens
        # Per-compile-mode video_bench keys + whether compile-ON is benchmarkable on THIS machine.
        # Resolved via the runner's own helpers so the table reads exactly the keys the runner writes.
        # Sets self._bench_keys / self._display_modes / self._compile_available / self._compile_why /
        # self.model_tag. Read _resolve_keys before changing this.
        self._resolve_keys()
        self.total_vram_gb = 0
        self.target_vars = {}
        self._rows = {}                     # (target, "off"/"on") -> tree iid
        self._row_meta = {}                 # iid -> (target, mode), for sort/filter keys
        self._row_order = []                # iids in creation order (filtered reattach base)
        self._bench_sort = {}              # view-sort direction state for the results headers

        self._build()
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.bind("<Configure>", self._track_geometry, add="+")
        # Safety net: restore the main window no matter HOW this one goes away (a normal close
        # runs _close first; this catches any other teardown so the app can't be left running
        # with its only window hidden). Guarded to this widget (Destroy bubbles from children).
        self.bind("<Destroy>", self._on_destroy, add="+")
        self.deiconify()                    # reveal now that it's fully built (no flash)
        # No modal grab_set(): an application grab makes Windows swallow the title-bar MINIMIZE
        # and MAXIMIZE system commands (SC_MINIMIZE/SC_MAXIMIZE), so a long remote sweep (hours)
        # could not be minimized out of the way -- only edge-drag resizing still worked. The grab
        # was only a belt-and-suspenders guard against a second benchmark window (already blocked
        # by the tab's _benchmark_win reuse guard) and a conflicting local GPU job (already
        # blocked by _hide_master hiding the main window + the Benchmark button's
        # local_gpu_job_running() backstop), so dropping it costs no real protection.
        self.after(80, self._detect_gpu)
        self._hide_master()

    def _hide_master(self):
        """Fully hide (withdraw) the main app window for the benchmark's lifetime, restored in
        _restore_master. This window is not a real modal (grab was dropped in 0.5.0 so a long
        sweep can be minimized, see __init__); its 'modal' guarantee is that the main window is
        out of reach so no conflicting GPU job can be started behind it. Withdraw, not iconify:
        a merely-minimized root gets RESTORED by Windows to service a mid-session dialog (a
        messagebox, a filedialog, the card picker's grab) and then sits there reachable once the
        dialog closes; a withdrawn window is not remapped for a dialog parented elsewhere, so the
        whole problem goes away."""
        try:
            if self._master_win is not None and self._master_win.winfo_exists():
                self._master_win.withdraw()
        except Exception:                              # noqa: BLE001
            pass

    def _restore_master(self):
        if self._closing:
            return                                     # idempotent: _close + <Destroy> both call
        self._closing = True
        try:
            if self._master_win is not None and self._master_win.winfo_exists():
                self._master_win.deiconify()
                self._master_win.lift()
        except Exception:                              # noqa: BLE001
            pass

    def _on_destroy(self, event):
        # Any teardown path (not just the normal _close) must bring the main window back, or the
        # app is left running with no visible window. Guard to THIS widget: <Destroy> bubbles up
        # from every child widget as they are torn down.
        if event.widget is self:
            self._restore_master()

    # ── layout ───────────────────────────────────────────────────────────────

    # ── method (SeedVR2 vs Real-ESRGAN, #18 B) ────────────────────────────────

    def _default_method(self):
        """(engine, tier) pre-selected from the configured video.engine. Real-ESRGAN is the single
        unified entry (tier=None -> all tiers), so only the engine matters here."""
        eng = (CFG.get("video", {}).get("engine") or "seedvr2").lower()
        return ("fixed_ratio", None) if eng == "fixed_ratio" else ("seedvr2", None)

    def _method_label(self, engine, tier):
        for lbl, e, t in self._methods:
            if e == engine and t == tier:
                return lbl
        return self._methods[0][0]

    def _configure_method(self):
        """Recompute the per-method row/checkbox sets. SeedVR2: checkboxes AND rows are the
        vb.TARGETS tokens (1:1). Real-ESRGAN (unified, #18 B): checkboxes are the RATIO tokens
        (2X/4X, the union across tiers) while rows are the (cell, TIER) pairs -- one run sweeps
        BOTH tiers, so a ratio-source cell can appear once per tier that supports it. `self._cells`
        is keyed by cell name for tier-independent geometry (dims/label); the tier is the row mode."""
        if self.is_esrgan:
            self._esrgan_tiers = vb.esrgan_all_tiers()
            # The ratio checkmarks are the union of what any tier supports natively (quality gives
            # both 2X + 4X; compact only 4X), in the canonical ESRGAN_RATIOS order (2X, 4X).
            self._ratio_tokens = [
                r for r in vb.ESRGAN_RATIOS
                if any(r in vb.esrgan_targets(t) for t in self._esrgan_tiers)]
            self._cells = {c["name"]: c for c in self._esrgan_all_cells(self._ratio_tokens)}
        else:
            self._esrgan_tiers = []
            self._ratio_tokens = []
            self._cells = {}

    def _esrgan_all_cells(self, ratios):
        """Every Real-ESRGAN cell across this window's tier(s) for `ratios` (each cell carries its
        own tier + resolved model). This is the row set: a ratio-source cell appears once per tier
        that supports it natively (so a 4X cell has a compact AND a quality row, a 2X cell only a
        quality row)."""
        return vb.build_esrgan_multi_plan(ratios, self._esrgan_tiers)

    def _row_pairs(self):
        """The (token, mode) pairs that get a results-table row. SeedVR2: the full grid of targets
        x compile modes. Real-ESRGAN: exactly the valid (cell name, tier) pairs -- NOT a full grid,
        since a 2X cell has no compact row."""
        if self.is_esrgan:
            return [(c["name"], c["tier"]) for c in self._esrgan_all_cells(self._ratio_tokens)]
        return [(t, m) for t in self.ALL_TARGETS for m in self._display_modes]

    @property
    def is_esrgan(self):
        return self._engine == "fixed_ratio"

    def _row_tokens(self):
        """Tokens that get a results-table ROW (SeedVR2 targets, or ESRGAN cell names)."""
        return list(self._cells) if self.is_esrgan else list(self.ALL_TARGETS)

    def _checkbox_tokens(self):
        """Tokens that get a benchmark CHECKBOX (SeedVR2 targets, or ESRGAN ratio tokens)."""
        return list(self._ratio_tokens) if self.is_esrgan else list(self.ALL_TARGETS)

    def _dims(self, token):
        """Output (w, h) for a row/target token, method-aware (ESRGAN cell or SeedVR2 preset)."""
        if token in self._cells:
            c = self._cells[token]
            return c["out_w"], c["out_h"]
        if token in vb.TARGETS:
            return vb.TARGETS[token][:2]
        return (0, 0)

    def _mp(self, token):
        w, h = self._dims(token)
        return w * h / 1_000_000.0

    def _resolve_keys(self):
        """Resolve the per-torch.compile-mode video_bench keys the runner will WRITE, plus whether
        compile-ON can actually run here. Sets:
          self.model_tag        - base model DISPLAY name (no compile suffix), for the header.
          self._bench_keys      - {"off": key, "on": key} (LOCAL) or {mode: key} (REMOTE, single).
          self._display_modes   - the modes that get a table row, in display order.
          self._compile_available / self._compile_why - drives the "Also use Torch Compile" toggle.

        LOCAL shows the no-compile baseline always, and the compiled mode too when the toolchain
        verifies (gate_local_compile). REMOTE is single-mode (the config's compile setting): running
        both compile modes is a local-GPU feature; a pod owns its own compile. Resolved ONCE at open
        (the local gate verifies by compiling, ~1 s, cached per process). Fail-safe: any problem
        degrades to a single OFF row keyed by the bare model tag, so the window still opens."""
        self._compile_available = False
        self._compile_why = None
        if self.is_esrgan:
            # Real-ESRGAN (unified): no torch.compile, no batch sweep. Each TIER is a regime, keyed
            # by its esrgan bench model; `mode` == the tier, so the shared (token, mode) row
            # machinery applies (the #0 column shows the tier, e.g. COMPACT / QUALITY). One run
            # sweeps every tier so a remote pod is shared across them (#18 B).
            tiers = getattr(self, "_esrgan_tiers", None) or vb.esrgan_all_tiers()
            self.model_tag = "Real-ESRGAN"
            self._bench_keys = {t: vb.esrgan_bench_model(t) for t in tiers}
            self._display_modes = list(tiers)
            return
        self.model_tag = sizer.model_tag(CFG.get("video", {}).get(
            "dit_model", "seedvr2_ema_7b_fp16.safetensors"))
        try:
            if self.remote:
                key = vb.resolve_bench_key(remote=True, log_fn=lambda *_a, **_k: None)
                mode = "on" if (key.endswith("|c") or key.endswith("|cd")) else "off"
                self._bench_keys = {mode: key}
                self._display_modes = [mode]
            else:
                keys = vb.resolve_bench_keys(remote=False, log_fn=lambda *_a, **_k: None)
                self._bench_keys = {"off": keys["off"]}
                self._display_modes = ["off"]
                self._compile_available = bool(keys.get("compile_available"))
                self._compile_why = keys.get("compile_why")
                if self._compile_available and keys.get("on"):
                    self._bench_keys["on"] = keys["on"]
                    self._display_modes.append("on")
        except Exception:                              # noqa: BLE001 (a working window beats a right key)
            self._bench_keys = {"off": self.model_tag}
            self._display_modes = ["off"]

    def _bench_key(self, mode):
        """The video_bench key for a compile mode ("off"/"on"), or None if this window has no
        such mode (e.g. compile unavailable, or a remote single-mode window)."""
        return self._bench_keys.get(mode)

    def _run_modes(self):
        """The compile modes the NEXT run will benchmark. LOCAL: the no-compile baseline always,
        plus compile-ON when 'Also use Torch Compile' is ticked (and available). REMOTE: the single
        config-derived mode (the runner reads config; no --compile-modes is passed)."""
        if self.is_esrgan:
            return list(self._display_modes)            # a single mode: the tier (no compile)
        if self.remote:
            return list(self._display_modes)
        modes = ["off"]
        if self._compile_available and self.also_compile_var.get():
            modes.append("on")
        return modes

    def _also_compile_tip_text(self):
        """Dynamic tooltip for the 'Also use Torch Compile' checkbox: what it does, or why it is
        disabled (remote, or no compile toolchain on this machine)."""
        if self.remote:
            return ("Benchmarking both torch.compile modes at once is a local-GPU feature. A remote "
                    "pod benchmark uses the compile setting from Settings.")
        if not self._compile_available:
            why = self._compile_why or ("the torch.compile toolchain (Triton + a C compiler) is "
                                        "not set up")
            return ("Disabled: torch.compile can't run on this machine, so only the no-compile "
                    f"baseline is benchmarked.\nReason: {why}.\n"
                    "Set it up from the first-start wizard (or Settings) to benchmark compiled too.")
        return ("On: benchmark BOTH with and without torch.compile in one run. Each is saved under "
                "its own key, so AUTO and the time estimate use the right numbers whichever mode you "
                "later upscale with.\nOff: benchmark the no-compile baseline only.\n"
                "This is independent of the Torch Compile checkbox in Settings; it only affects "
                "this benchmark.")

    def _target_label(self, t):
        """Checkbox text. SeedVR2: dims-named ladder cells show dimensions + aspect, presets keep
        their name, both carry output MP. Real-ESRGAN: a ratio token (2X/4X) shows the native
        upscale + the cell output(s) it covers (e.g. '2X · 720p→1440p, 1080p→4K')."""
        if self.is_esrgan:
            cells = [c for c in self._cells.values() if c["ratio"] == t]
            parts = ", ".join(f"{c['src_h']}p→{min(c['out_w'], c['out_h'])}p"
                              for c in cells) or "native"
            return f"{t} · {parts}"
        w, h = vb.TARGETS[t][:2]
        mp = w * h / 1_000_000.0
        if "x" in t:
            return f"{w}×{h} {'3:4' if h > w else '4:3'} · {mp:.1f} MP"
        return f"{t} · {mp:.1f} MP"

    def _is_ladder(self, t):
        """True for a dims-named 4:3 ladder cell (vs a 16:9 preset like 1080p/1440p/4K)."""
        return "x" in t and t in vb.TARGETS

    def _row_label(self, t):
        """The Target-column text for a row token: the bare token for SeedVR2, a friendly
        'ratio · short-side' for a Real-ESRGAN cell (e.g. '2X · 1440p')."""
        if t in self._cells:
            c = self._cells[t]
            return f"{c['ratio']} · {min(c['out_w'], c['out_h'])}p"
        return t

    def _rebuild_targets(self):
        """(Re)build the checkbox area for the current method. SeedVR2: the 4:3 ladder + presets
        + 'Also use Torch Compile' + Restart. Real-ESRGAN: the 2X/4X ratio checkmarks + Restart
        (no compile). Rebuilt on a method switch; the persistent Method combobox is untouched."""
        for w in self._targets_holder.winfo_children():
            w.destroy()
        self._toggles = []
        self._target_cb = {}
        self.target_vars = {}
        holder = self._targets_holder
        tokens = self._checkbox_tokens()
        cols = 1 if self.is_esrgan else 4         # ESRGAN ratios stack; SeedVR2 grid wraps
        for i, t in enumerate(tokens):
            v = tk.BooleanVar(value=False)
            v.trace_add("write", lambda *_a: self._refresh_estimate())
            self.target_vars[t] = v
            cb = ttk.Checkbutton(holder, text=self._target_label(t), variable=v)
            cb.grid(row=i // cols, column=i % cols, padx=(0, 16), pady=(0, 4), sticky="w")
            self._toggles.append(cb)
            self._target_cb[t] = cb
        base_row = (len(tokens) + cols - 1) // cols
        span = max(cols, 1)
        if not self.is_esrgan:
            # "Also use Torch Compile": benchmark both compile modes at once (SeedVR2 only).
            self.also_compile_var.set(bool(self._compile_available and not self.remote))
            self.also_compile_cb = ttk.Checkbutton(
                holder, text="Also use Torch Compile (benchmark with AND without compile)",
                variable=self.also_compile_var, command=self._refresh_estimate)
            self.also_compile_cb.grid(row=base_row, column=0, columnspan=span,
                                      sticky="w", pady=(6, 0))
            if not (self._compile_available and not self.remote):
                self.also_compile_cb.configure(state="disabled")
            else:
                self._toggles.append(self.also_compile_cb)   # locked while a run is in flight
            self.also_compile_tip = Tooltip(self.also_compile_cb, self._also_compile_tip_text())
            base_row += 1
        self.restart_var.set(False)
        rb = ttk.Checkbutton(holder, text="Restart (discard saved results for the ticked targets)",
                             variable=self.restart_var, command=self._refresh_estimate)
        rb.grid(row=base_row, column=0, columnspan=span, sticky="w", pady=(2, 0))
        self._toggles.append(rb)
        Tooltip(rb, "Off (default): a new run RESUMES, keeping finished probes.\n"
                    "On: clears the saved probes for the TICKED targets only and measures\n"
                    "them from scratch. Targets you did not tick keep their results.")

    def _on_method_change(self):
        """Method combobox changed: switch engine/tier, then rebuild the checkbox area and the
        results table for the new method. No-op mid-run (the combobox is read-only but a switch
        would strand a running sweep), and no-op when the selection did not actually change."""
        if self.proc is not None:
            self.method_var.set(self._method_label(self._engine, self._tier))
            return
        eng, tier = self._method_label_to_val.get(self.method_var.get(),
                                                  (self._engine, self._tier))
        if (eng, tier) == (self._engine, self._tier):
            return
        self._engine, self._tier = eng, tier
        self._configure_method()
        self._resolve_keys()
        self._rebuild_targets()
        self._rebuild_rows()
        if self.gpu_id:
            self.header_var.set(
                f"{self.gpu_id} — {self.total_vram_gb} GB VRAM — model {self.model_tag}")
            self._default_check_targets()
        self._refresh_estimate()

    def _rebuild_rows(self):
        """Clear + repopulate the results table for the current method (used on a method switch).
        Relabels the #0 column to name the regime: compile mode (SeedVR2) or model tier (ESRGAN)."""
        for iid in self.tree.get_children(""):
            self.tree.delete(iid)
        self._rows.clear()
        self._row_meta.clear()
        self._row_order.clear()
        h0 = "Model" if self.is_esrgan else "Torch Compile"
        self.tree.heading("#0", text=h0, command=lambda: self._sort_tree("#0"))
        if "_titles" in self._bench_sort:
            self._bench_sort["_titles"]["#0"] = h0
        if self.gpu_id:
            self._load_prior()
            self._populate_bench_filters()
            self._apply_feasibility()

    def _default_check_targets(self):
        """Pre-tick sensible defaults for the current method. SeedVR2: the feasible landscape
        ladder cells + presets the card's VRAM can reach. Real-ESRGAN: every native ratio (it is
        light, so a default run covers them all)."""
        if self.is_esrgan:
            for t in self._checkbox_tokens():
                self.target_vars[t].set(True)
            return
        max_mp = ve.max_output_mp(self.total_vram_gb, self.gpu_id, self.conn)
        for t in self.ALL_TARGETS:
            if self._is_ladder(t):
                w, h = vb.TARGETS[t][:2]
                if max_mp and w >= h and (w * h / 1_000_000.0) <= max_mp + 1e-6:
                    self.target_vars[t].set(True)
        if self.total_vram_gb >= 40 and "1440p" in self.target_vars:
            self.target_vars["1440p"].set(True)
        if self.total_vram_gb >= 80 and "4K" in self.target_vars:
            self.target_vars["4K"].set(True)

    def _set_esrgan_row(self, t, m, probes):
        """Render a Real-ESRGAN cell row from its probes: the batch columns are '—' (a GAN has no
        batch), the s/frame + Peak VRAM come from the single probe, status = saved."""
        iid = self._ensure_row(t, m)
        for col in ("ceiling", "saved", "overlap"):
            self.tree.set(iid, col, "—")
        self.tree.set(iid, "status", "saved" if probes else "not benchmarked")
        self._set_saved_metrics(t, m, probes)
        self._set_runtime(t, m)

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

        # Method + target selection. The Method combobox (SeedVR2 vs a Real-ESRGAN tier, #18 B)
        # is persistent; the checkbox area below it is rebuilt when the method changes (the
        # targets differ: SeedVR2 has its 4:3 ladder + presets, Real-ESRGAN has 2X/4X ratios).
        tf = ttk.LabelFrame(self, text=" Method & targets ", padding=8)
        tf.grid(row=2, column=0, sticky="ew", pady=(8, 0), **pad)
        tf.columnconfigure(0, weight=1)
        mrow = ttk.Frame(tf)
        mrow.grid(row=0, column=0, sticky="w", pady=(0, 6))
        ttk.Label(mrow, text="Method:").pack(side="left")
        self.method_var = tk.StringVar(value=self._method_label(self._engine, self._tier))
        self.method_combo = ttk.Combobox(
            mrow, textvariable=self.method_var, state="readonly", width=26,
            values=[lbl for lbl, _e, _t in self._methods])
        self.method_combo.pack(side="left", padx=(4, 0))
        self.method_combo.bind("<<ComboboxSelected>>", lambda _e: self._on_method_change())
        Tooltip(self.method_combo,
                "What to benchmark. SeedVR2 sweeps the batch size upward to each target's VRAM "
                "ceiling. Real-ESRGAN is a light fixed-ratio (2x/4x) upscaler: it measures one "
                "speed + VRAM point per ratio (no batch sweep), on almost any GPU.")
        # Rebuildable checkbox holder (its contents depend on the method).
        self._targets_holder = ttk.Frame(tf)
        self._targets_holder.grid(row=1, column=0, sticky="ew")
        self.restart_var = tk.BooleanVar(value=False)
        self.also_compile_var = tk.BooleanVar(value=False)
        self._rebuild_targets()

        # Results table (fixed 5 rows) + the program-output log below it, filling the rest.
        rf = ttk.LabelFrame(self, text=" Results & log ", padding=4)
        rf.grid(row=3, column=0, sticky="nsew", pady=(8, 0), **pad)
        self.rowconfigure(3, weight=1)
        rf.rowconfigure(2, weight=1)          # the log grows; the filter bar + table stay put
        rf.columnconfigure(0, weight=1)

        # Filter bar (mirrors the Video Upscaler's lists): narrow the results by compile mode
        # or target, populated once the rows exist (_populate_bench_filters). Detach/reattach,
        # so filtering is view-only and never touches the saved probes.
        filt = ttk.Frame(rf)
        filt.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 4))
        ttk.Label(filt, text="Filter:").pack(side="left")
        self.filter_mode_var = tk.StringVar(value="All")
        self.filter_target_var = tk.StringVar(value="All")
        self._filter_combos = {}
        for label, var, width in (("Torch Compile", self.filter_mode_var, 8),
                                  ("Target", self.filter_target_var, 12)):
            ttk.Label(filt, text=f"  {label}:").pack(side="left")
            cb = ttk.Combobox(filt, textvariable=var, state="readonly",
                              width=width, values=["All"])
            cb.pack(side="left", padx=(2, 0))
            cb.bind("<<ComboboxSelected>>", lambda _e: self._apply_bench_filters())
            self._filter_combos[label] = cb
        ttk.Button(filt, text="Reset", command=self._reset_bench_filters).pack(
            side="left", padx=(10, 0))

        # Column one is "Torch Compile" (ON/OFF): each target gets a row per benchmarked compile
        # mode. The tree column (#0) carries it; "Target" is the first data column.
        cols = ("target", "ceiling", "saved", "overlap", "spf", "peak", "status", "runtime")
        self.tree = ttk.Treeview(rf, columns=cols, show="tree headings", height=6)
        self.tree.column("#0", width=100, minwidth=90, stretch=False, anchor="center")
        # Click any header (incl. Torch Compile/#0) to view-sort by that column (toggles
        # asc/desc); the values sort by their UNDERLYING number, not the display text.
        _titles = {"#0": "Torch Compile"}
        self.tree.heading("#0", text="Torch Compile",
                          command=lambda: self._sort_tree("#0"))
        # "Max batch" = how far the card pushed (the raw ceiling); "Used" = the batch AUTO
        # actually runs (the fastest window, which can be lower: the ceiling rides VRAM spill).
        # "Runtime" = the GPU time this cell's probes took (summed from the saved probes, so it
        # persists and re-accumulates correctly on resume).
        for c, txt, w in (("target", "Target", 104), ("ceiling", "Max batch", 84),
                          ("saved", "Used", 60), ("overlap", "Overlap", 66),
                          ("spf", "s/frame", 74), ("peak", "Peak VRAM", 104),
                          ("status", "Status", 170), ("runtime", "Runtime", 78)):
            _titles[c] = txt
            self.tree.heading(c, text=txt, command=lambda c=c: self._sort_tree(c))
            self.tree.column(c, width=w, stretch=(c == "status"),
                             anchor=("e" if c == "runtime" else "w"))
        self._bench_sort["_titles"] = _titles
        self.tree.grid(row=1, column=0, sticky="ew")
        sb = ttk.Scrollbar(rf, orient="vertical", command=self.tree.yview)
        sb.grid(row=1, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=sb.set)

        logf = ttk.Frame(rf)
        logf.grid(row=2, column=0, columnspan=2, sticky="nsew", pady=(8, 0))
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

        # Buttons: run controls, share actions, the issue link and Close all on ONE row.
        # Start/Stop drive the sweep; Export/Contribute give your measured results back (the
        # community corpus is pulled automatically at startup, App._startup_bench_sync, so there
        # is no Download/Import button); Export/Contribute enable once a card is detected and let
        # you pick SEVERAL cards for one submission. The issue link's column takes the slack so
        # Close sits at the far right.
        bf = ttk.Frame(self)
        bf.grid(row=8, column=0, sticky="ew", pady=(8, 10), **pad)
        bf.columnconfigure(4, weight=1)
        self.start_btn = ttk.Button(bf, text="Start", command=self._start, state="disabled")
        self.start_btn.grid(row=0, column=0)
        self.stop_btn = ttk.Button(bf, text="Stop", command=self._stop, state="disabled")
        self.stop_btn.grid(row=0, column=1, padx=(6, 0))
        self.export_btn = ttk.Button(bf, text="Export…", command=self._export,
                                     state="disabled")
        self.export_btn.grid(row=0, column=2, padx=(16, 0))
        self.contribute_btn = ttk.Button(bf, text="Contribute my results…",
                                        command=self._contribute, state="disabled")
        self.contribute_btn.grid(row=0, column=3, padx=(6, 0))
        Tooltip(self.contribute_btn,
                "Share your benchmark results with other users: opens a pre-filled GitHub "
                "issue (uses your browser's GitHub login, no setup). You can select several "
                "GPUs to submit at once. Local results always win over the community data.")
        # "Report an issue" link (mirrors the main window's bottom-bar link), but it
        # points reporters at logs/video_benchmark.log -- the benchmark's own diagnostics,
        # far more useful here than the newest crash log the main-window link suggests.
        issue = tk.Label(bf, text="Report an issue", fg="#3a86ff",
                         cursor="hand2", font=("Segoe UI", 9, "underline"))
        issue.grid(row=0, column=4, sticky="w", padx=(16, 0))
        issue.bind("<Button-1>", lambda _e: report_issue(
            os.path.join(APP_ROOT, "logs", "video_benchmark.log"), "Benchmark log"))
        issue.bind("<Enter>", lambda _e: issue.configure(fg="#1a5fd0"))
        issue.bind("<Leave>", lambda _e: issue.configure(fg="#3a86ff"))
        self.close_btn = ttk.Button(bf, text="Close", command=self._close)
        self.close_btn.grid(row=0, column=5)

    def _track_geometry(self, _e=None):
        if self.app is not None and self.state() == "normal":
            self._geo = self.geometry()

    # ── GPU detection + prior results ────────────────────────────────────────

    def _detect_gpu(self):
        # Remote: the card is whatever the user picked in the GPU list, not a local probe.
        # Shape it like sample_gpu's dict so _fill_gpu is unchanged; there is no live
        # "used VRAM" for a pod that isn't deployed yet, so used=0.
        if self.remote:
            vram_gb = self.remote_gpu.get("memory_gb") or 0
            g = {"gpu_used_mb": 0, "gpu_total_mb": int(vram_gb) * 1024,
                 "gpu_temp_c": None}
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
        self.total_vram_gb = round((g.get("gpu_total_mb") or 0) / 1024.0)
        self.header_var.set(f"{self.gpu_id} — {self.total_vram_gb} GB VRAM — model {self.model_tag}")
        # Default-check the targets this card can plausibly reach (all stay toggleable). SeedVR2:
        # every feasible LANDSCAPE 4:3 ladder cell + the presets a big card can do. Real-ESRGAN:
        # every native ratio (it is light). See _default_check_targets.
        self._default_check_targets()
        self._load_prior()
        self._populate_bench_filters()      # rows now exist: fill the filter combos
        self._apply_feasibility()
        self.start_btn.configure(state="normal")
        self.export_btn.configure(state="normal")
        self.contribute_btn.configure(state="normal")
        self._refresh_estimate()
        self._refresh_vram_warning(g)

    def _apply_feasibility(self):
        """Disable targets this card can't reach: a 24 GB card OOMs on 1440p/4K SeedVR2, so
        benchmarking them is a guaranteed failure. Gate on the same max-output-MP model the
        Target picker uses (seed by VRAM, raised by whatever this card has already proven), so
        anything measured feasible stays selectable."""
        if self.is_esrgan:
            # Real-ESRGAN is a light fixed-ratio GAN that tiles on OOM, so no cell is VRAM-gated;
            # keep every ratio checkbox enabled.
            for t in self._checkbox_tokens():
                cb = self._target_cb.get(t)
                if cb is not None:
                    cb.configure(state="normal")
            return
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
                for m in self._display_modes:
                    self.tree.set(self._ensure_row(t, m), "status",
                                  "not supported on this card (VRAM)")
            elif cb is not None:
                cb.configure(state="normal")

    def _sample_used_vram(self):
        """(used_gb, total_gb) for GPU 0 via nvidia-smi, or (None, None). Best-effort."""
        try:
            import system_telemetry as st
            g = st.sample_gpu()                        # dict of GPU fields, or None
            if g and g.get("gpu_used_mb") is not None and g.get("gpu_total_mb"):
                return g["gpu_used_mb"] / 1024.0, g["gpu_total_mb"] / 1024.0
        except Exception:                              # noqa: BLE001
            pass
        return None, None

    def _refresh_vram_warning(self, g=None):
        """Show the contention warning iff other apps are already holding VRAM (> the
        threshold). Purely advisory: benchmarking with busy VRAM under-reports the ceiling."""
        used = ((g["gpu_used_mb"] / 1024.0)
                if (g and g.get("gpu_used_mb") is not None)
                else self._sample_used_vram()[0])
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
        """Pre-fill the table from saved probes (a resumable prior run), one row per (target,
        compile-mode). Each mode reads its OWN key, so the with- and without-compile results show
        side by side."""
        for t, m in self._row_pairs():
            w, h = self._dims(t)
            self._ensure_row(t, m)
            probes = vb.drop_collapsed(db.get_bench_probes(
                self.conn, self.gpu_id, self._bench_key(m), w, h))
            if not probes:
                self.tree.set(self._rows[(t, m)], "status", "not benchmarked")
                self._set_runtime(t, m)
                continue
            if self.is_esrgan:
                self._set_esrgan_row(t, m, probes)
                continue
            ceil = vb.cell_ceiling(probes)
            saved = vb.throughput_optimal_batch(probes)
            done = vb.cell_done(probes)
            self._set_ceiling(t, m, ceil, "saved" if done else "partial (resumable)",
                              saved=saved)
            self._set_saved_metrics(t, m, probes)   # spf + Peak VRAM (persisted, not live)

    @property
    def conn(self):
        return db.get_conn()

    def _ensure_row(self, target, mode):
        key = (target, mode)
        if key not in self._rows:
            iid = self.tree.insert(
                "", "end", text=mode.upper(),          # "OFF"/"ON" (compile) or the tier (ESRGAN)
                values=(self._row_label(target), "", "", "", "", "", "", ""))
            self._rows[key] = iid
            self._row_meta[iid] = (target, mode)
            self._row_order.append(iid)
        return self._rows[key]

    # ── results filtering + column sorting (mirrors the Video Upscaler lists) ──

    def _populate_bench_filters(self):
        """Fill the Torch Compile / Target filter combos with the DISTINCT values now present
        in the table (plus 'All'), in row-creation order. Called once the rows exist."""
        modes, targets = [], []
        for iid in self._row_order:
            t, m = self._row_meta[iid]
            if m.upper() not in modes:
                modes.append(m.upper())
            if t not in targets:
                targets.append(t)
        self._filter_combos["Torch Compile"].configure(values=["All"] + modes)
        self._filter_combos["Target"].configure(values=["All"] + targets)

    def _reset_bench_filters(self):
        self.filter_mode_var.set("All")
        self.filter_target_var.set("All")
        self._apply_bench_filters()

    def _apply_bench_filters(self):
        """Show only rows matching every active filter, by detaching the rest and reattaching
        matches in creation order. View-only: the saved probes are untouched. Setting cell
        values on a detached row is fine, so a running sweep still updates hidden rows."""
        mf = self.filter_mode_var.get()
        tf = self.filter_target_var.get()
        idx = 0
        for iid in self._row_order:
            t, m = self._row_meta.get(iid, (None, None))
            if t is None:
                continue
            ok = ((mf in ("", "All") or m.upper() == mf) and
                  (tf in ("", "All") or t == tf))
            if ok:
                self.tree.reattach(iid, "", idx)
                idx += 1
            else:
                self.tree.detach(iid)

    @staticmethod
    def _num(s):
        """Leading number in a display cell (e.g. '5', '0.39', '23.1/23.1 GB'), or -1.0 when
        there is none ('—', ''), so blank/absent cells sort to the bottom of an ascending sort."""
        mm = re.search(r"[-+]?\d*\.?\d+", s or "")
        return float(mm.group()) if mm else -1.0

    @staticmethod
    def _hms_to_s(s):
        """Seconds from a 'H:MM:SS' / 'M:SS' runtime cell, or -1.0 for '—'/blank."""
        s = (s or "").strip()
        if not s or s == "—":
            return -1.0
        try:
            sec = 0
            for part in s.split(":"):
                sec = sec * 60 + int(part)
            return float(sec)
        except ValueError:
            return -1.0

    def _bench_sort_key(self, col):
        """Sort key for a results row, by the UNDERLYING value (not the display text): the
        compile mode orders OFF<ON, Target by its output pixels, the numeric columns by their
        parsed number, runtime by seconds, and Status alphabetically."""
        def key(iid):
            t, m = self._row_meta.get(iid, ("", "off"))
            if col == "#0":
                return 0 if m == "off" else 1
            if col == "target":
                w, h = self._dims(t)
                return w * h
            if col in ("ceiling", "saved", "overlap", "spf", "peak"):
                return self._num(self.tree.set(iid, col))
            if col == "runtime":
                return self._hms_to_s(self.tree.set(iid, "runtime"))
            return (self.tree.set(iid, col) or "").lower()      # status
        return key

    def _sort_tree(self, col):
        """View-sort the results table by `col` (toggling asc/desc), only over the currently
        VISIBLE (unfiltered) rows. Header-only: it reorders the display, not the saved probes."""
        state = self._bench_sort
        reverse = not state.get(col, True)          # first click = ascending
        keyfunc = self._bench_sort_key(col)
        rows = [(keyfunc(iid), iid) for iid in self.tree.get_children("")]
        try:
            rows.sort(key=lambda t: t[0], reverse=reverse)
        except TypeError:                                # mixed types -> compare as str
            rows.sort(key=lambda t: str(t[0]), reverse=reverse)
        for i, (_k, iid) in enumerate(rows):
            self.tree.move(iid, "", i)
        titles = state.get("_titles", {})
        for k in list(state.keys()):                     # reset directions, keep titles
            if k != "_titles":
                del state[k]
        state[col] = reverse
        for c, base in titles.items():
            arrow = (" ▲" if not reverse else " ▼") if c == col else ""
            self.tree.heading(c, text=base + arrow)

    def _set_ceiling(self, target, mode, ceil, status, saved=None):
        iid = self._ensure_row(target, mode)
        if self.is_esrgan:
            # A fixed-ratio GAN has no batch/overlap: those columns stay '—'; s/frame + Peak
            # VRAM (from _set_saved_metrics / live BPROBE) carry the result.
            for col in ("ceiling", "saved", "overlap"):
                self.tree.set(iid, col, "—")
            self.tree.set(iid, "status", status)
            self._set_runtime(target, mode)
            return
        self.tree.set(iid, "ceiling", str(ceil) if ceil else "—")
        self.tree.set(iid, "saved", str(saved) if saved else "—")
        # Overlap reflects the batch AUTO will actually run: the saved (fastest) one, else the
        # ceiling while a sweep is still in flight and the optimum isn't known yet.
        ov_b = saved or ceil
        self.tree.set(iid, "overlap", str(sizer.auto_overlap(ov_b)) if ov_b else "—")
        self.tree.set(iid, "status", status)
        self._set_runtime(target, mode)

    def _set_saved_metrics(self, target, mode, probes):
        """Fill the s/frame + Peak VRAM columns from the SAVED (throughput-optimal) probe.

        Those two columns are otherwise written ONLY by live BPROBE events, so a REOPENED
        window (and the reflected final state after a run) showed them blank even though the
        per-probe timing and VRAM peaks are persisted in db.video_bench — the values were
        saved, just never read back. Shows the batch AUTO will actually use (matching the
        runner's recorded rate and summary table), so a resumed/closed sweep reads the same
        s/frame a live probe did. Fail-safe: a cell with no timed 'ok' probe leaves the two
        columns untouched."""
        m = vb.saved_metrics(probes)
        if not m:
            return
        iid = self._ensure_row(target, mode)
        self.tree.set(iid, "spf", f"{m['spf']:.2f}")
        pa, pr = m["peak_alloc"], m["peak_reserved"]
        if pa or pr:                                   # mirror the live BPROBE formatting
            self.tree.set(iid, "peak", f"{pa or '?'}/{pr or '?'} GB")

    def _cell_runtime_s(self, target, mode):
        """Total GPU time this (target, mode) cell's probes took = the sum of the saved probes'
        seconds under that mode's key. Read from the DB (the runner records each probe before
        signalling the GUI, and upserts a re-probe), so it is correct live, persists, and
        re-accumulates on resume."""
        if not self.gpu_id:
            return 0.0
        w, h = self._dims(target)
        probes = db.get_bench_probes(self.conn, self.gpu_id, self._bench_key(mode), w, h)
        return sum((p["seconds"] or 0) for p in probes)

    def _set_runtime(self, target, mode):
        s = self._cell_runtime_s(target, mode)
        self.tree.set(self._ensure_row(target, mode), "runtime", _fmt_hms(s) if s else "—")

    def _selected_targets(self):
        return [t for t in self._checkbox_tokens()
                if self.target_vars.get(t) and self.target_vars[t].get()]

    def _selected_cells(self, ratios):
        """The Real-ESRGAN cells (across ALL tiers) covered by the ticked ratio tokens -- the actual
        per-tier run/row set, not the tier-deduped geometry map."""
        return [c for c in self._esrgan_all_cells(list(ratios))]

    def _refresh_estimate(self):
        if not self.gpu_id:
            return
        targets = self._selected_targets()
        if not targets:
            self.estimate_var.set("Select at least one target.")
            self.start_btn.configure(state="disabled")
            return
        self.start_btn.configure(state="normal" if self.proc is None else "disabled")
        if self.is_esrgan:
            return self._refresh_esrgan_estimate(targets)
        plan = vb.build_plan(targets)
        run_modes = self._run_modes()
        # Estimate + done count sum over every compile mode the run will sweep (each is a full pass).
        est = 0.0
        done = 0
        for m in run_modes:
            key = self._bench_key(m)
            dm = 0
            if not self.restart_var.get():
                dm = sum(len(db.get_bench_probes(self.conn, self.gpu_id, key,
                                                 c["out_w"], c["out_h"])) for c in plan)
            done += dm
            est += vb.estimate_runtime(plan, self.gpu_id, self.conn, done=dm)
        verb = "Restart" if self.restart_var.get() else ("Resume" if done else "Run")
        self.start_btn.configure(text=verb if self.proc is None else verb)
        modes_note = " · with + without compile" if len(run_modes) > 1 else ""
        self.estimate_var.set(f"{verb}: {len(targets)} target(s){modes_note} · estimated "
                              f"~{_fmt_hms(est)} (rough; the card's real speed is unknown until "
                              f"measured).")

    def _refresh_esrgan_estimate(self, ratios):
        """Estimate line for a unified Real-ESRGAN run: one probe per (cell, tier), no sweep. Runtime
        sums each cell's frames x MP x its TIER's measured rate (compact and quality differ ~16x);
        an unmeasured tier shows nothing for its cells (the run measures it)."""
        cells = self._selected_cells(ratios)
        done = sum(1 for c in cells if db.get_bench_probes(
            self.conn, self.gpu_id, self._bench_key(c["tier"]), c["out_w"], c["out_h"]))
        verb = "Restart" if self.restart_var.get() else ("Resume" if done else "Run")
        self.start_btn.configure(text=verb)
        rates = {t: ve.esrgan_seconds_per_mp(self.gpu_id, t, self.conn) for t in self._display_modes}
        est = sum(c["frames"] * c["mp"] * rates[c["tier"]] for c in cells if rates.get(c["tier"]))
        tail = f" · ~{_fmt_hms(est)}" if est else " · measured on this run (fast)"
        self.estimate_var.set(
            f"{verb}: {len(cells)} cell(s){tail} · Real-ESRGAN {'/'.join(self._display_modes)} "
            f"(fixed {'/'.join(ratios)}).")

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
        run_modes = self._run_modes()
        script = os.path.join(SCRIPT_DIR, "video_benchmark.py")
        if self.is_esrgan:
            # Real-ESRGAN (#18 B): pass the ratio tokens + ALL tiers; the runner sweeps every tier
            # in one run/pod (no batch sweep, no compile modes).
            cmd = [PYTHON_EXE, "-u", script, "--engine", "esrgan",
                   "--ratios", ",".join(targets),
                   "--esrgan-tiers", ",".join(self._display_modes)]
            if self.remote:
                cmd.append("--remote")
            if self.restart_var.get():
                cmd.append("--restart")
                for c in self._selected_cells(targets):     # clear the (cell, tier) rows we're redoing
                    self._set_ceiling(c["name"], c["tier"], None, "queued")
        else:
            cmd = [PYTHON_EXE, "-u", script, "--targets", ",".join(targets)]
            if self.remote:
                cmd.append("--remote")                 # remote uses the config compile setting
            else:
                cmd += ["--compile-modes", ",".join(run_modes)]
            if self.restart_var.get():
                cmd.append("--restart")
                for t in targets:                      # clear the table rows we're redoing
                    for m in run_modes:
                        self._set_ceiling(t, m, None, "queued")
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

    # ── benchmark sharing (feature #8) ───────────────────────────────────────

    def _local_name(self):
        """The machine's detected card name, queried once and cached (used to infer
        local/remote for a contributed card that isn't the window's)."""
        if not hasattr(self, "_local_gpu_cache"):
            try:
                from local_video_engine import _query_gpu_name
                self._local_gpu_cache = _query_gpu_name()
            except Exception:                              # noqa: BLE001
                self._local_gpu_cache = None
        return self._local_gpu_cache

    def _run_on_for(self, gpu_id):
        # The window's own card: its mode is authoritative. Any other card on disk: infer.
        if gpu_id == self.gpu_id:
            return "remote" if self.remote else "local"
        return vb.infer_run_on(gpu_id, local_name=self._local_name())

    def _rows_for(self, gpu_id):
        """Contributable summary rows for a card. Price only applies to the window's own
        remote card (a historical card carries no live price). Fail-safe: [] on any error."""
        price = self.remote_gpu.get("price") if (self.remote and gpu_id == self.gpu_id) else None
        try:
            return vb.build_share_rows(self.conn, gpu_id,
                                       run_on=self._run_on_for(gpu_id), price_usd_hr=price)
        except Exception:                                  # noqa: BLE001 (never break the window)
            return []

    def _gpu_picker(self, cards, counts):
        """Modal MULTI-select card chooser: every card with contributable rows on disk (out-of-
        stock remote cards included, so their past results can still be shared), each showing its
        contributable row count. Pre-selects ALL so submitting several GPUs at once is one click;
        narrow the selection with click / Ctrl+click / Shift+click. Returns the chosen list of
        gpu_ids (empty if cancelled)."""
        win = tk.Toplevel(self)
        win.title("Choose cards to share")
        win.transient(self)
        win.grab_set()
        try:
            win.iconbitmap(os.path.join(APP_ROOT, "app.ico"))
        except Exception:
            pass
        ttk.Label(win, text="Contribute benchmark results for (select one or more):").pack(
            anchor="w", padx=12, pady=(12, 4))
        lb = tk.Listbox(win, selectmode="extended", width=54,
                        height=min(12, len(cards)), activestyle="none", exportselection=False)
        lb.pack(fill="both", expand=True, padx=12)
        for c in cards:
            n = counts[c]
            lb.insert("end", f"{c}   ({n} row{'' if n == 1 else 's'})")
        lb.selection_set(0, "end")                     # default: all cards, so multi-submit is 1 click
        lb.see(0)
        chosen = {"gpus": []}

        def ok(_e=None):
            chosen["gpus"] = [cards[i] for i in lb.curselection()]
            win.destroy()

        lb.bind("<Double-Button-1>", ok)
        lb.bind("<Return>", ok)
        win.bind("<Escape>", lambda _e: win.destroy())
        bf = ttk.Frame(win)
        bf.pack(fill="x", padx=12, pady=12)
        ttk.Button(bf, text="Cancel", command=win.destroy).pack(side="right")
        ttk.Button(bf, text="OK", command=ok).pack(side="right", padx=(0, 6))
        ttk.Button(bf, text="Select all",
                   command=lambda: lb.selection_set(0, "end")).pack(side="left")
        ttk.Button(bf, text="Clear",
                   command=lambda: lb.selection_clear(0, "end")).pack(side="left", padx=(6, 0))
        win.update_idletasks()
        self.wait_window(win)
        return chosen["gpus"]

    def _pick_and_rows(self):
        """Choose which card(s) to share and build their COMBINED rows. The multi-select picker
        is shown when >1 card has contributable rows (a single card skips it); cards with zero
        contributable rows are hidden. Returns (gpu_ids, rows) or None (cancelled / nothing)."""
        if not db.bench_gpu_ids(self.conn):
            messagebox.showinfo(
                APP_TITLE, "No benchmark results to share yet. Run a benchmark first, then "
                "Export or Contribute.", parent=self)
            return None
        # Build each card's rows once; keep only cards that actually have something to share
        # (a remote card with only Torch Compile OFF results contributes nothing, so it is
        # hidden rather than offered as an empty pick).
        by_card = {c: self._rows_for(c) for c in db.bench_gpu_ids(self.conn)}
        cards = [c for c, r in by_card.items() if r]
        if not cards:
            messagebox.showinfo(
                APP_TITLE, "No contributable results yet.\n\nRemote pods run with Torch "
                "Compile ON, so Torch Compile OFF results from a rented pod are not shared.",
                parent=self)
            return None
        if len(cards) == 1:
            chosen = [cards[0]]
        else:
            counts = {c: len(by_card[c]) for c in cards}
            chosen = self._gpu_picker(cards, counts)
            if not chosen:
                return None
        rows = [r for c in chosen for r in by_card[c]]
        return chosen, rows

    def _default_csv_name(self, gpu_ids):
        """CSV filename for one or several cards: the card name for a single GPU, else a generic
        multi-GPU name (the CSV itself carries each card's gpu_id)."""
        if isinstance(gpu_ids, str):
            gpu_ids = [gpu_ids]
        if len(gpu_ids) == 1:
            safe = re.sub(r"[^A-Za-z0-9._-]+", "_", gpu_ids[0] or "gpu").strip("_") or "gpu"
            return f"benchmark_{safe}.csv"
        return f"benchmark_{len(gpu_ids)}-gpus.csv"

    @staticmethod
    def _share_label(gpu_ids):
        """Human label for a set of shared cards (issue title / status line)."""
        return gpu_ids[0] if len(gpu_ids) == 1 else f"{len(gpu_ids)} GPUs"

    @staticmethod
    def _cards_in_rows(rows):
        """The distinct gpu_ids present in a set of share rows, in first-appearance order. The
        title/filename derive from THIS (what is actually submitted), not the selection, so the
        community dedup dropping a card entirely can't leave a '3 GPUs' title over a 1-card body."""
        return list(dict.fromkeys(r.get("gpu_id") for r in rows if r.get("gpu_id")))

    def _export(self):
        picked = self._pick_and_rows()
        if picked is None:
            return
        gpu_ids, rows = picked
        path = filedialog.asksaveasfilename(
            parent=self, title="Export benchmark results", defaultextension=".csv",
            initialfile=self._default_csv_name(gpu_ids), filetypes=[("CSV files", "*.csv")])
        if not path:
            return
        import bench_share
        try:
            n = bench_share.write_csv(path, rows)
        except OSError as exc:
            messagebox.showerror(APP_TITLE, f"Could not write the CSV:\n{exc}", parent=self)
            return
        self.status_var.set(f"Exported {n} row(s) for {self._share_label(gpu_ids)} to {path}")
        messagebox.showinfo(
            APP_TITLE, f"Exported {n} benchmark row(s) for {self._share_label(gpu_ids)} to:\n{path}",
            parent=self)

    def _contribute(self):
        picked = self._pick_and_rows()
        if picked is None:
            return
        gpu_ids, rows = picked
        # Diff against the LIVE community corpus so a user who benchmarks a bit more each day
        # only submits genuinely new/changed rows, instead of re-posting the whole set every
        # time (submission happens in the browser and can't be confirmed by the app). Fetch off
        # the UI thread; on any failure we fall back to submitting everything (the maintainer
        # merge dedups anyway, so a missed diff is noise, never lost data).
        self.contribute_btn.configure(state="disabled")
        self.status_var.set("Checking the community data set for new results …")

        def work():
            import bench_share
            try:
                existing = bench_share.fetch_community()
                to_send = bench_share.new_rows(rows, existing) if existing else list(rows)
                checked = bool(existing)
                err = None
            except Exception as exc:                    # noqa: BLE001 (fail-safe -> send all)
                to_send, checked, err = list(rows), False, exc
            self.after(0, lambda: self._contribute_ready(gpu_ids, to_send, len(rows), checked, err))

        threading.Thread(target=work, daemon=True).start()

    def _contribute_ready(self, gpu_ids, to_send, total, checked, err):
        self.contribute_btn.configure(state="normal")
        dropped = total - len(to_send)
        if not to_send:
            self.status_var.set("Nothing new to contribute.")
            messagebox.showinfo(
                APP_TITLE, f"All of {self._share_label(gpu_ids)}'s results are already in the "
                "community data set. Nothing new to submit.", parent=self)
            return
        # After the community dedup, to_send can cover FEWER cards than were selected (a card whose
        # every row is already published drops out entirely). Title/filename/label MUST come from
        # the cards actually IN to_send, or a "3 GPUs" title sits over a one-card body (the bug).
        sent_ids = self._cards_in_rows(to_send)
        label = self._share_label(sent_ids)
        import bench_share
        path = os.path.join(APP_ROOT, "logs", self._default_csv_name(sent_ids))
        try:
            bench_share.write_csv(path, to_send)        # copy on disk for the attach fallback
            csv_text = bench_share.to_text(to_send)
        except OSError as exc:
            messagebox.showerror(APP_TITLE, f"Could not write the CSV:\n{exc}", parent=self)
            return
        # How the dedup went, appended to every outcome message.
        note = (f" ({dropped} already in the community set were skipped)" if dropped else "")
        # Name any SELECTED cards that contributed nothing new, so submitting 1 of 3 isn't a surprise.
        fully_done = [g for g in gpu_ids if g not in sent_ids]
        if fully_done:
            note += (f"; already up to date: {', '.join(fully_done)}")
        if not checked and err is not None:
            note = " (could not check the community set, so all rows are included)"
        mode = contribute_benchmark(label, csv_text, path)
        if mode == "inline":
            self.status_var.set(f"Opened a pre-filled issue for {label} "
                                f"({len(to_send)} new row(s)){note}. Review and Submit in your browser.")
        elif mode == "attach":
            self.status_var.set(f"Results saved to {path}.")
            messagebox.showinfo(
                APP_TITLE, "Your results are too large to embed in the issue directly.\n\n"
                f"They were saved to:\n{path}\n\nDrag that file into the GitHub issue that "
                "just opened, then Submit.", parent=self)
        else:
            messagebox.showwarning(
                APP_TITLE, "Could not open your browser. Your results were saved to:\n"
                f"{path}\n\nYou can attach that file to a GitHub issue manually.",
                parent=self)

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
            modes = data.get("modes") or []
            # ESRGAN's "modes" are model tiers, not compile regimes; only SeedVR2 has a compile note.
            if self.is_esrgan:
                mnote = ""
            else:
                mnote = " (with + without compile)" if len(modes) > 1 else ""
            self.status_var.set(f"Benchmarking {n} target(s){mnote} on {where} …")
        elif kind == "BCELL" and data:
            self._set_ceiling(data["name"], data.get("compile", "off"), None, "benchmarking …")
        elif kind == "BPROBE" and data:
            t = data.get("name")
            mode = data.get("compile", "off")
            if data.get("state") == "running":
                self.tree.set(self._ensure_row(t, mode),
                              "status", f"probing batch {data.get('batch')} …")
            elif data.get("state") == "done":
                iid = self._ensure_row(t, mode)
                oc = data.get("outcome")
                if self.is_esrgan:
                    # No batch: just the speed + VRAM point (batch columns stay '—').
                    if oc == "ok":
                        if data.get("spf") is not None:
                            self.tree.set(iid, "spf", f"{data['spf']:.2f}")
                        pa, pr = data.get("peak_alloc"), data.get("peak_reserved")
                        if pa or pr:
                            self.tree.set(iid, "peak", f"{pa or '?'}/{pr or '?'} GB")
                        self.tree.set(iid, "status", "measured")
                    else:
                        self.tree.set(iid, "status", oc or "error")
                    self._set_runtime(t, mode)
                elif oc == "ok":
                    self.tree.set(iid, "ceiling", str(data.get("batch")))
                    self.tree.set(iid, "overlap", str(sizer.auto_overlap(data.get("batch") or 0)))
                    if data.get("spf") is not None:
                        self.tree.set(iid, "spf", f"{data['spf']:.2f}")
                    pa, pr = data.get("peak_alloc"), data.get("peak_reserved")
                    if pa or pr:
                        self.tree.set(iid, "peak", f"{pa or '?'}/{pr or '?'} GB")
                    self.tree.set(iid, "status", f"batch {data.get('batch')} ok")
                    self._set_runtime(t, mode)
                else:
                    self.tree.set(iid, "status", f"batch {data.get('batch')} {oc}")
                    self._set_runtime(t, mode)
        elif kind == "BCEILING" and data:
            if self.is_esrgan:
                self._set_ceiling(data["name"], data.get("compile", "off"), None, "saved")
            else:
                ceil = data.get("ceiling")
                saved = data.get("saved")
                status = (f"done — uses {saved} (max fit {ceil})"
                          if saved and ceil and saved != ceil
                          else f"done — batch {saved}" if saved else "can't do this target")
                self._set_ceiling(data["name"], data.get("compile", "off"), ceil, status,
                                  saved=saved)
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
        for t, m in self._row_pairs():                 # reflect the persisted final state per row
            w, h = self._dims(t)
            probes = vb.drop_collapsed(db.get_bench_probes(
                self.conn, self.gpu_id, self._bench_key(m), w, h))
            if not probes:
                continue
            if self.is_esrgan:
                self._set_esrgan_row(t, m, probes)
                continue
            ceil = vb.cell_ceiling(probes)
            saved = vb.throughput_optimal_batch(probes)
            done = vb.cell_done(probes)
            cur = self.tree.set(self._ensure_row(t, m), "status")
            if not cur.startswith("done") and not cur.startswith("can't"):
                self._set_ceiling(t, m, ceil, "saved" if done else "partial (resumable)",
                                  saved=saved)
            # Reflect the SAVED batch's s/frame + Peak VRAM (persisted). Live BPROBE left
            # whatever the last probe showed, which mid-refine isn't the optimal one.
            self._set_saved_metrics(t, m, probes)
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
