"""
gui/telemetry_graph.py
----------------------
The floating telemetry usage-graph window (future-feature #9). One shared
instance per source ("local" or a pod), opened by clicking a telemetry row.

Rendered with **matplotlib** embedded via FigureCanvasTkAgg (a deliberate,
lazy-loaded GUI dependency: see docs/telemetry-design.md). This module is
imported lazily by App.open_telemetry_graph inside a try/except, so if matplotlib
is missing the readout row + MQTT still work and only the graph is unavailable.

Design (docs/telemetry-design.md section 4):
  * per-RUN: reads a system_telemetry.TelemetryHistory that records only between
    a run's start and seal; a sealed run freezes but stays interactive.
  * four capacity-PINNED stacked charts (load %, memory %, power W, temp C):
    y-axes are hardware capacity, never autoscaled to the observed max.
  * a dynamic, per-window GLOBAL range bar (1h/3h/… enable as runtime grows;
    press to zoom to the last N, press again to revert to the whole run).
  * a blitted crosshair readout shared across all charts (smooth hover).
"""

import tkinter as tk
from tkinter import ttk

# matplotlib is imported LAZILY, inside __init__ (see below) — NOT at module top.
# The module must import cleanly without matplotlib so the import-smoke test (and a
# Remote-only install before its bootstrap adds matplotlib) never fails just to
# reach the App's lazy, guarded open path. Only actually opening a graph needs it.

from gui.common import _geometry_on_screen, save_settings
import system_telemetry as st

# series colours (distinct; the row's band palette is for the row, not lines)
C_CPU, C_UTIL = "#2f6fed", "#ff6b35"
C_VRAM, C_RAM = "#1a9e4b", "#8338ec"
C_POWER, C_TEMP = "#d9902b", "#e23b5a"
_MUTED, _CEIL = "#8a94a3", "#b6becb"

REFRESH_MS = 2000        # live redraw cadence while the run is active


def _pct(used, total):
    return 0.0 if not total else used * 100.0 / total


class TelemetryGraphWindow(tk.Toplevel):
    def __init__(self, master, app, source, title):
        # Import matplotlib HERE, before the Toplevel is created: if it is absent
        # this raises cleanly (App.open_telemetry_graph catches it and shows a
        # hint) with no half-built window left behind.
        import matplotlib
        matplotlib.use("TkAgg")
        from matplotlib.figure import Figure
        from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
        import matplotlib.ticker as mticker
        self._Figure = Figure
        self._FigureCanvasTkAgg = FigureCanvasTkAgg
        self._mticker = mticker

        super().__init__(master)
        self._app = app
        self._source = source
        self._active_span = None            # None = whole run
        self._bg = None
        self._axes = self._lines = self._markers = self._vlines = None
        self._job = None
        self._last_normal_geo = None

        self.title(title)
        geo = app.settings.get("telemetry_geometry") if app is not None else None
        # 980x720 as both the default AND the minimum: four stacked charts need that
        # much to stay readable. (Deliberately its own number — the main window's
        # minimum is set by the video queue's column count, not by this content.)
        # The remembered geometry wins after the first open.
        self.geometry(geo if (geo and _geometry_on_screen(self, geo)) else "980x720")
        self.minsize(980, 720)

        # ── top: title line + range bar + readout ────────────────────────────
        self._head_var = tk.StringVar(value=title)
        head = ttk.Frame(self, padding=(10, 6, 10, 2))
        head.pack(fill="x")
        ttk.Label(head, textvariable=self._head_var,
                  foreground=_MUTED).pack(side="left")

        bar = ttk.Frame(self, padding=(10, 0, 10, 4))
        bar.pack(fill="x")
        ttk.Label(bar, text="Range:").pack(side="left")
        # A pressed range button is marked by BOLDING its label (an active
        # zoom-to-recent filter), so no separate "Showing: …" caption is needed.
        import tkinter.font as tkfont
        _base = tkfont.nametofont("TkDefaultFont")
        self._btn_bold_font = tkfont.Font(family=_base.cget("family"),
                                          size=_base.cget("size"), weight="bold")
        ttk.Style(self).configure("TelSpanActive.TButton",
                                  font=self._btn_bold_font)
        self._span_btns = {}
        for label, span in st.HISTORY_SPANS:
            b = ttk.Button(bar, text=label, width=4, state="disabled",
                           command=lambda s=span: self._toggle(s))
            b.pack(side="left", padx=2)
            self._span_btns[span] = b
        # fixed-width readout so hover text never re-lays-out the bar
        self._read = tk.Label(bar, font=("Consolas", 9), width=84, anchor="e")
        self._read.pack(side="right")

        # ── figure: four stacked subplots ────────────────────────────────────
        self._fig = self._Figure(figsize=(9, 7), dpi=100)
        self._canvas = self._FigureCanvasTkAgg(self._fig, master=self)
        self._canvas.get_tk_widget().pack(fill="both", expand=True,
                                          padx=10, pady=(0, 10))
        self._build_axes()
        self._canvas.mpl_connect("draw_event", self._on_draw)
        self._canvas.mpl_connect("motion_notify_event", self._on_move)
        self._canvas.mpl_connect("axes_leave_event", self._on_leave)

        self.bind("<Configure>", self._track_geometry, add="+")
        self.protocol("WM_DELETE_WINDOW", self._close)
        self._refresh()

    # ── chart definitions ────────────────────────────────────────────────────
    def _chart_defs(self):
        """(title, unit, ymax(latest)->float, ceil_label, series[]) x4. ymax is
        pinned to hardware capacity; None-capacity falls back to autoscale."""
        return [
            ("GPU / CPU load", "%", lambda s: 100.0, "100%", [
                ("GPU util", C_UTIL, "gpu_util_pct", lambda s: f"{s.get('gpu_util_pct')}%"),
                ("CPU", C_CPU, "cpu", lambda s: f"{round(s.get('cpu') or 0)}%"),
            ]),
            ("Memory (% of capacity)", "%", lambda s: 100.0, "capacity", [
                ("VRAM", C_VRAM, lambda s: _pct(s.get("gpu_used_mb"), s.get("gpu_total_mb")),
                 lambda s: f"{_pct(s.get('gpu_used_mb'), s.get('gpu_total_mb')):.0f}%  "
                           f"{(s.get('gpu_used_mb') or 0)/1024:.1f}/"
                           f"{(s.get('gpu_total_mb') or 0)/1024:.0f} GB"),
                ("RAM", C_RAM, lambda s: _pct(s.get("ram_used_mb"), s.get("ram_total_mb")),
                 lambda s: f"{_pct(s.get('ram_used_mb'), s.get('ram_total_mb')):.0f}%  "
                           f"{(s.get('ram_used_mb') or 0)/1024:.1f}/"
                           f"{(s.get('ram_total_mb') or 0)/1024:.0f} GB"),
            ]),
            ("GPU power", "W", self._power_ceiling, self._power_ceil_label, [
                ("Power", C_POWER, "gpu_power_w", lambda s: f"{s.get('gpu_power_w')} W"),
            ]),
            ("GPU temperature", "°C", lambda s: 100.0, "100°C", [
                ("Temp", C_TEMP, "gpu_temp_c", lambda s: f"{s.get('gpu_temp_c')}°C"),
            ]),
        ]

    def _power_ceiling(self, latest):
        lim = latest.get("gpu_power_limit_w") if latest else None
        if lim:
            return float(lim)
        h = self._history()
        if h and len(h):                        # no reported limit: fit to observed
            _t, vals = h.series("gpu_power_w")
            peak = max((v for v in vals if v == v), default=0)   # skip NaN
            return max(50.0, peak * 1.1)
        return 400.0

    def _power_ceil_label(self, latest):
        lim = latest.get("gpu_power_limit_w") if latest else None
        return f"limit {lim}W" if lim else "peak"

    def _build_axes(self):
        defs = self._chart_defs()
        n = len(defs)
        self._axes, self._lines, self._markers, self._vlines = [], [], [], []
        for i, (title, unit, _ymax, _cl, series) in enumerate(defs):
            ax = self._fig.add_subplot(n, 1, i + 1)
            ax.grid(True, color="#e6e9ef")
            ax.tick_params(labelsize=8, colors=_MUTED)
            ax.set_ylabel(unit, fontsize=8, color=_MUTED)
            series_lines, series_markers = [], []
            for label, color, _acc, _fmt in series:
                (ln,) = ax.plot([], [], color=color, lw=1.6, label=label)
                (mk,) = ax.plot([], [], "o", color=color, ms=5, mec="white",
                                animated=True)
                series_lines.append(ln)
                series_markers.append(mk)
            ax.legend(loc="upper right", fontsize=8, framealpha=0.85)
            vline = ax.axvline(0, color="#9aa4b2", lw=1, ls=(0, (2, 2)),
                               animated=True, visible=False)
            if i < n - 1:
                ax.set_xticklabels([])
            self._axes.append(ax)
            self._lines.append(series_lines)
            self._markers.append(series_markers)
            self._vlines.append(vline)
        self._axes[-1].xaxis.set_major_formatter(
            self._mticker.FuncFormatter(self._fmt_x))
        self._fig.subplots_adjust(left=0.07, right=0.985, top=0.99, bottom=0.05,
                                  hspace=0.14)

    # ── data / refresh ───────────────────────────────────────────────────────
    def _history(self):
        return self._app.telemetry_history(self._source) if self._app else None

    def _fmt_x(self, x, _pos):
        h = self._history()
        base = (h.anchor_ts() if h and h.anchor_ts() is not None else 0)
        return f"{(x - base) / 60:.0f}m"

    def _refresh(self):
        h = self._history()
        defs = self._chart_defs()
        if not h or len(h) == 0:
            self._head_var.set("Waiting for the first sample…")
            self._job = self.after(REFRESH_MS, self._refresh)
            return

        latest = h.latest()
        t0, t1 = h.bounds()
        if self._active_span is not None:
            x0, x1 = t1 - self._active_span, t1
        else:
            x0, x1 = t0, t1
        if x1 <= x0:
            x1 = x0 + 1

        for (title, unit, ymax_fn, ceil_label, series), ax, lines in zip(
                defs, self._axes, self._lines):
            ymax = ymax_fn(latest) or 1.0
            ax.set_ylim(0, ymax * 1.02)
            ax.set_xlim(x0, x1)
            for (label, color, acc, _fmt), ln in zip(series, lines):
                xs, ys = h.series(acc)
                ln.set_data(xs, ys)
            # redraw the capacity ceiling line + labels fresh each time
            for art in list(getattr(ax, "_tbx_overlay", [])):
                try:
                    art.remove()
                except Exception:
                    pass
            cl = ceil_label(latest) if callable(ceil_label) else ceil_label
            overlay = [
                ax.axhline(ymax, ls="--", lw=1, color=_CEIL),
                ax.text(0.005, 0.92, title, transform=ax.transAxes, fontsize=9,
                        color="#3a4250", va="top"),
                ax.text(0.995, 0.92, cl, transform=ax.transAxes, fontsize=8,
                        color=_MUTED, ha="right", va="top"),
            ]
            ax._tbx_overlay = overlay
            # legend text: append the latest value to each label
            leg = ax.get_legend()
            if leg is not None:
                for txt, (label, color, acc, fmt) in zip(leg.get_texts(), series):
                    try:
                        txt.set_text(f"{label}  {fmt(latest)}")
                    except Exception:
                        txt.set_text(label)

        self._update_range_bar(h)
        state = "live" if h.is_live else "sealed"
        started = self._fmt_clock(h.anchor_ts())    # when the first sample landed
        self._head_var.set(f"{self.title()}  ·  {state}  ·  started {started}")
        self._canvas.draw()

        if h.is_live:
            self._job = self.after(REFRESH_MS, self._refresh)
        else:
            self._job = None            # sealed: static, but stays interactive

    @staticmethod
    def _fmt_clock(ts):
        import time
        try:
            return time.strftime("%H:%M", time.localtime(ts)) if ts else "?"
        except Exception:
            return "?"

    # ── range bar (dynamic enable + global toggle) ───────────────────────────
    def _update_range_bar(self, h):
        enabled = {s for _lbl, s in h.enabled_spans()}
        if self._active_span is not None and self._active_span not in enabled:
            self._active_span = None    # run got reset shorter than the zoom
        for span, btn in self._span_btns.items():
            # Bold the active span's button so the current zoom is visible without
            # a separate caption; the rest render in the default (plain) style.
            active = span == self._active_span
            btn.configure(state=("normal" if span in enabled else "disabled"),
                          style="TelSpanActive.TButton" if active else "TButton")

    def _toggle(self, span):
        self._active_span = None if self._active_span == span else span
        self._refresh()

    # ── blitted crosshair ────────────────────────────────────────────────────
    def _on_draw(self, _evt):
        try:
            self._bg = self._canvas.copy_from_bbox(self._fig.bbox)
        except Exception:
            self._bg = None

    def _on_move(self, evt):
        h = self._history()
        if evt.inaxes is None or evt.xdata is None or self._bg is None or not h or not len(h):
            return
        t = float(evt.xdata)
        s, tt = self._sample_at(h, t)       # nearest recorded sample by time
        if s is None:
            return
        self._canvas.restore_region(self._bg)
        defs = self._chart_defs()
        for (_title, _u, _ym, _cl, series), ax, vline, markers in zip(
                defs, self._axes, self._vlines, self._markers):
            vline.set_xdata([tt, tt])
            vline.set_visible(True)
            ax.draw_artist(vline)
            for (label, color, acc, _fmt), mk in zip(series, markers):
                v = acc(s) if callable(acc) else s.get(acc)
                if v is None or v != v:
                    mk.set_data([], [])
                else:
                    mk.set_data([tt], [float(v)])
                ax.draw_artist(mk)
        self._canvas.blit(self._fig.bbox)
        self._read.config(text=self._readout(s, tt, h))

    def _sample_at(self, h, t):
        import bisect
        raw = h._samples                    # (ts, sample) list
        if not raw:
            return None, None
        ts_list = [r[0] for r in raw]
        i = min(max(0, bisect.bisect_left(ts_list, t)), len(raw) - 1)
        return raw[i][1], raw[i][0]

    def _readout(self, s, tt, h):
        base = h.anchor_ts() if h.anchor_ts() is not None else tt
        def g(k):
            v = s.get(k)
            return "—" if v is None else v
        return (f"t={ (tt-base)/60:5.1f}m  util {g('gpu_util_pct')}%  "
                f"cpu {round(s.get('cpu') or 0)}%  "
                f"vram {(s.get('gpu_used_mb') or 0)/1024:.1f}G  "
                f"pwr {g('gpu_power_w')}W  {g('gpu_temp_c')}C  "
                f"clk {g('gpu_clock_mhz')}MHz")

    def _on_leave(self, _evt):
        if self._bg is None:
            return
        self._canvas.restore_region(self._bg)
        self._canvas.blit(self._fig.bbox)
        self._read.config(text="")

    # ── geometry + lifecycle ─────────────────────────────────────────────────
    def _track_geometry(self, event):
        if event.widget is self:
            try:
                if self.state() == "normal":
                    self._last_normal_geo = self.geometry()
            except Exception:
                pass

    def save_geometry(self):
        """Persist size/position without destroying (used on app close, so the
        window is remembered even when the app quits with it open)."""
        if self._app is not None and self.winfo_exists():
            try:
                self._app.settings["telemetry_geometry"] = \
                    self._last_normal_geo or self.geometry()
                save_settings(self._app.settings)
            except Exception:
                pass

    def _close(self):
        if self._job is not None:
            try:
                self.after_cancel(self._job)
            except Exception:
                pass
            self._job = None
        self.save_geometry()
        if self._app is not None:
            self._app.forget_telemetry_graph(self._source)
        self.destroy()
