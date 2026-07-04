"""
gui/widgets.py
--------------
Generic, reusable tkinter widgets and small text/format helpers used across the
tabs and the app shell: Tooltip, sanitize() / _fmt_eta() / _log_hms(),
ProgressBar, TelemetryRow (system-telemetry row with load-based colour bands),
LogPane, ConsoleBuffer, and the floating LogViewer window.

Depends only on gui.common (window-geometry helpers) and tkinter, so it sits
below the tabs in the import order.
"""

import re
import datetime

import tkinter as tk
from tkinter import ttk

from gui.common import _geometry_on_screen, save_settings, PROGRESS_RE


# ─────────────────────────────────────────────
#  TOOLTIP
# ─────────────────────────────────────────────

class Tooltip:
    """Small hover tooltip attached to a widget."""

    # Wrap long tooltips onto several short lines instead of one very wide line.
    # A pixel width (Tk's wraplength) reads better than hand-inserted breaks and
    # applies to every tooltip in the app at once.
    WRAP_PX = 360

    def __init__(self, widget, text, delay=500, wraplength=WRAP_PX):
        self.widget = widget
        self.text   = text
        self.delay  = delay
        self.wraplength = wraplength
        self._after = None
        self._tip   = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _event=None):
        self._cancel()
        self._after = self.widget.after(self.delay, self._show)

    def _show(self):
        if self._tip is not None:
            return
        x = self.widget.winfo_pointerx() + 14
        y = self.widget.winfo_pointery() + 18
        self._tip = tk.Toplevel(self.widget)
        self._tip.wm_overrideredirect(True)
        self._tip.wm_geometry(f"+{x}+{y}")
        tk.Label(self._tip, text=self.text, background="#ffffe0",
                 relief="solid", borderwidth=1, font=("Segoe UI", 9),
                 wraplength=self.wraplength, justify="left",
                 padx=6, pady=2).pack()

    def _cancel(self):
        if self._after is not None:
            self.widget.after_cancel(self._after)
            self._after = None

    def _hide(self, _event=None):
        self._cancel()
        if self._tip is not None:
            self._tip.destroy()
            self._tip = None


# ─────────────────────────────────────────────
#  TERMINAL-OUTPUT SANITISING
# ─────────────────────────────────────────────

# OSC 8 hyperlinks: ESC ] 8 ; ; URI (ESC \ | BEL) — keep the visible text only
_OSC8_RE = re.compile("\x1b\\]8;;[^\x07\x1b]*(?:\x1b\\\\|\x07)")
# Any CSI colour/cursor sequence
_CSI_RE  = re.compile("\x1b\\[[0-9;?]*[A-Za-z]")


def sanitize(text):
    text = _OSC8_RE.sub("", text)
    text = _CSI_RE.sub("", text)
    # Tk's Text widget cannot reliably render characters outside the BMP
    # (emoji such as the folder icon) — drop them rather than show boxes.
    return "".join(ch for ch in text if ord(ch) <= 0xFFFF)


# ─────────────────────────────────────────────
#  PROGRESS BAR  (canvas — shows a percentage on the bar)
# ─────────────────────────────────────────────

def _fmt_eta(seconds):
    """Format a duration, dropping leading zero day/hour fields.
    e.g. '04d, 11h, 23m, 35s'; '11h, 36m, 39s'; '39m, 18s'; '18s'."""
    s = int(max(0, round(seconds)))
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    parts = [(d, "d"), (h, "h"), (m, "m"), (s, "s")]
    # Trim leading all-zero units, but always keep at least the seconds field.
    while len(parts) > 1 and parts[0][0] == 0:
        parts.pop(0)
    return ", ".join(f"{v:02d}{suffix}" for v, suffix in parts)


# Tooltips for the remote-pod "$ / 100 images" readout (0.3.9): the estimate is
# author-benchmark-derived before/through image 99, then recomputed from the live
# run once 100 images have been measured.
COST100_TIP_BENCH = ("Estimated cost per 100 images, derived from the author's "
                     "benchmarks for the selected GPU.")
COST100_TIP_USER  = ("Estimated cost per 100 images, derived from your own "
                     "previous runs on this GPU.")
COST100_TIP_RUN   = ("Cost per 100 images, calculated from the current run "
                     "(time over the last 100 images x this pod's hourly rate).")


class ProgressBar(tk.Canvas):
    """A determinate progress bar that draws its own percentage on the fill."""

    TROUGH = "#d6d9de"
    FILL   = "#4f9cff"
    TEXT   = "#13233a"

    def __init__(self, master, height=20, **kw):
        super().__init__(master, height=height, highlightthickness=1,
                         highlightbackground="#aeb4bd", bg=self.TROUGH, **kw)
        self._pct = 0.0
        self.bind("<Configure>", lambda _e: self._draw())

    def set(self, pct):
        self._pct = max(0.0, min(100.0, float(pct)))
        self._draw()

    def _draw(self):
        self.delete("all")
        w = max(self.winfo_width(), 1)
        h = max(self.winfo_height(), 1)
        fw = int(w * self._pct / 100.0)
        if fw > 0:
            self.create_rectangle(0, 0, fw, h, fill=self.FILL, width=0)
        self.create_text(w / 2, h / 2, text=f"{int(round(self._pct))}%",
                         fill=self.TEXT, font=("Segoe UI", 9, "bold"))


# ─────────────────────────────────────────────
#  SYSTEM TELEMETRY ROW  (Feature #3a)
# ─────────────────────────────────────────────

class TelemetryRow(ttk.Frame):
    """
    Compact, single-line readout of live system telemetry — CPU usage, RAM, GPU
    VRAM and GPU temperature — shown below the image carousel. Pushed in by
    App.sample_telemetry frequently while a task runs (task-driven cadence) and
    every 60 s while the app is idle (so the user can watch VRAM free up before
    starting a run).

    Each segment is its own label so the percentage readouts (CPU / RAM / VRAM)
    can be colour-coded by load band; static fields (GPU temp, separators) stay
    neutral grey.
    """

    IDLE  = "System: sampling…"
    GREY  = "#7f8a99"
    SEP   = " · "
    FONT  = ("Consolas", 9)

    # Percentage → colour band: blue 0–25, green 26–65, dark yellow 66–85,
    # red 86–100 (compared against the rounded value that is displayed).
    @classmethod
    def _band(cls, pct):
        if pct <= 25:
            return "#3a86ff"   # blue
        if pct <= 65:
            return "#1a9e4b"   # green
        if pct <= 85:
            return "#b58900"   # dark yellow
        return "#d11a2a"       # red

    def __init__(self, master, prefix=""):
        super().__init__(master)
        self._prefix = prefix      # optional leading label, e.g. "Remote pod"
        self._labels = []
        self._set([(self.IDLE, self.GREY)])

    def _set(self, segments):
        """Replace the row with [(text, colour), …], joined by grey separators."""
        for w in self._labels:
            w.destroy()
        self._labels = []
        for i, (text, color) in enumerate(segments):
            if i:
                sep = tk.Label(self, text=self.SEP, font=self.FONT, fg=self.GREY)
                sep.pack(side="left")
                self._labels.append(sep)
            lbl = tk.Label(self, text=text, font=self.FONT, fg=color)
            lbl.pack(side="left")
            self._labels.append(lbl)

    @staticmethod
    def _gb(used_mb, total_mb):
        pct = round(used_mb * 100.0 / total_mb)
        return f"{used_mb/1024:.1f}/{total_mb/1024:.1f} GB ({pct}%)", pct

    def show(self, sample):
        """Render a telemetry sample dict (any field may be None). Prefixed rows
        (the Upscale tab's local + remote pair) pad each field to a fixed width —
        in the monospace font this lines their columns up; unprefixed single
        rows stay compact."""
        pad = bool(self._prefix)

        def fld(text, width):
            return text.ljust(width) if pad else text

        segs = []
        if self._prefix:
            segs.append((self._prefix.ljust(10), self.GREY))
        cpu = sample.get("cpu")
        if cpu is not None:
            c = round(cpu)
            segs.append((fld(f"CPU {c}%", 8), self._band(c)))
        else:
            segs.append((fld("CPU —", 8), self.GREY))

        ru, rt = sample.get("ram_used_mb"), sample.get("ram_total_mb")
        if ru is not None and rt:
            text, pct = self._gb(ru, rt)
            segs.append((fld(f"RAM {text}", 24), self._band(pct)))

        vu, vt = sample.get("gpu_used_mb"), sample.get("gpu_total_mb")
        temp   = sample.get("gpu_temp_c")
        if vu is not None and vt:
            text, pct = self._gb(vu, vt)
            segs.append((fld(f"VRAM {text}", 24), self._band(pct)))
        if temp is not None:
            segs.append((f"GPU {temp}°C", self.GREY))
        if vu is None and temp is None:
            segs.append(("GPU: n/a", self.GREY))

        self._set(segs)


# ─────────────────────────────────────────────
#  LOG PANE
# ─────────────────────────────────────────────

def _log_hms():
    """Wall-clock time prefix for a log line in the GUI window (HH:MM:SS). The
    on-disk log uses a fuller date+time (runs can span days); a live session
    window only needs the time of day."""
    return datetime.datetime.now().strftime("%H:%M:%S")


class LogPane(ttk.Frame):
    """
    Read-only text console. Understands carriage returns the way a terminal
    does (an in-place progress update erases the current line), so the
    scripts' live counters render correctly instead of flooding the log.

    Each line is prefixed with a wall-clock '[HH:MM:SS]' timestamp (0.3.9), to
    help reconstruct total run time and correlate the log with external events.
    Live chunks are stamped here at render time (≈ arrival); backlog text is
    fed pre-stamped (stamp=False) from ConsoleBuffer's recorded per-line times.
    """

    MAX_LINES = 4000

    def __init__(self, master):
        super().__init__(master)
        # wrap="word" so long lines fold instead of growing a horizontal
        # scrollbar; wrapping follows the widget width, so resizing the
        # window reflows the text automatically. width/height are requested
        # MINIMUMS in characters, kept small so the log shrinks gracefully
        # instead of squeezing fixed-size neighbours (e.g. the image queue)
        # out of the window.
        self.text = tk.Text(
            self, wrap="word", state="disabled", font=("Consolas", 9),
            width=40, height=10,
            background="#15181d", foreground="#d7dde4",
            insertbackground="#d7dde4", relief="flat", padx=8, pady=6,
        )
        ys = ttk.Scrollbar(self, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=ys.set)
        self.text.grid(row=0, column=0, sticky="nsew")
        ys.grid(row=0, column=1, sticky="ns")
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        # A carriage return only repositions the cursor — the line is erased
        # when the overwriting text arrives, possibly in a later chunk.
        self._pending_cr = False
        # True when the next real token starts a fresh line (so a timestamp is
        # due). Tracked across chunks; a \r-overwrite restarts the line too.
        self._at_line_start = True

    def feed(self, data, stamp=True):
        """Render a chunk. `stamp=True` prefixes a '[HH:MM:SS]' to each new line
        as it is drawn (live output); `stamp=False` renders the text verbatim
        (pre-stamped backlog) while still tracking line-start state so the next
        live chunk continues correctly."""
        data = sanitize(data.replace("\r\n", "\n"))
        if not data:
            return
        t = self.text
        t.configure(state="normal")
        follow = t.yview()[1] >= 0.999   # auto-scroll only if already at the bottom
        for token in re.split("([\r\n])", data):
            if token == "\n":
                self._pending_cr = False
                self._at_line_start = True
                t.insert("end", "\n")
            elif token == "\r":
                self._pending_cr = True
            elif token:
                if self._pending_cr:
                    self._pending_cr = False
                    t.delete("end-1c linestart", "end-1c")
                    self._at_line_start = True
                if stamp and self._at_line_start:
                    t.insert("end", f"[{_log_hms()}] ")
                self._at_line_start = False
                t.insert("end", token)
        lines = int(t.index("end-1c").split(".")[0])
        if lines > self.MAX_LINES:
            t.delete("1.0", f"{lines - self.MAX_LINES}.0")
        if follow:
            t.see("end")
        t.configure(state="disabled")

    def clear(self):
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")
        self._pending_cr    = False
        self._at_line_start = True


# ─────────────────────────────────────────────
#  CONSOLE BUFFER  (headless terminal model)
# ─────────────────────────────────────────────

class ConsoleBuffer:
    """
    Headless model of the program's clean output stream. It applies the same
    terminal carriage-return/line-feed semantics as LogPane (an in-place \\r
    update overwrites the current line) and keeps the last MAX_LINES lines so
    a freshly opened log window can show the backlog. Observers are notified
    with each new chunk (for live viewers) and with None on clear.

    This holds only the GUI-filtered output (markers already removed, engine
    diagnostics never included) — the noisy SeedVR text that used to fill the
    on-disk log never reaches it.
    """

    MAX_LINES = 4000

    def __init__(self):
        self._lines       = [""]      # current line is _lines[-1]
        self._times       = [None]    # parallel: wall-clock HH:MM:SS per line
        self._pending_cr  = False
        self._observers   = []

    def add_observer(self, cb):
        self._observers.append(cb)

    def remove_observer(self, cb):
        if cb in self._observers:
            self._observers.remove(cb)

    def feed(self, data):
        data = sanitize(data.replace("\r\n", "\n"))
        if not data:
            return
        for token in re.split("([\r\n])", data):
            if token == "\n":
                self._pending_cr = False
                self._lines.append("")
                self._times.append(None)
            elif token == "\r":
                self._pending_cr = True
            elif token:
                if self._pending_cr:
                    self._pending_cr = False
                    self._lines[-1] = ""
                    self._times[-1] = None
                # Stamp the line the moment its first character arrives, so the
                # backlog shows when each line actually happened (not when a log
                # window was later opened).
                if self._times[-1] is None:
                    self._times[-1] = _log_hms()
                self._lines[-1] += token
        if len(self._lines) > self.MAX_LINES:
            cut = len(self._lines) - self.MAX_LINES
            del self._lines[:cut]
            del self._times[:cut]
        for cb in list(self._observers):
            cb(data)

    def clear(self):
        self._lines      = [""]
        self._times      = [None]
        self._pending_cr = False
        for cb in list(self._observers):
            cb(None)

    def text(self):
        return "\n".join(self._lines)

    def text_timestamped(self):
        """Backlog text with each recorded line prefixed by its '[HH:MM:SS]'.
        Used when a log window opens, so old lines keep their real times; the
        raw `_lines` stay un-prefixed so the regex helpers below are unaffected."""
        out = []
        for line, ts in zip(self._lines, self._times):
            out.append(f"[{ts}] {line}" if ts else line)
        return "\n".join(out)

    def last_image_lines(self, n):
        """The last n lines that look like per-image processing lines
        (those carrying a "[idx/total]" counter — prep phases use "(x/y)")."""
        out = []
        for line in reversed(self._lines):
            if PROGRESS_RE.search(line):
                out.append(line.rstrip())
                if len(out) >= n:
                    break
        out.reverse()
        return out

    def find_last(self, pattern):
        """The last buffered line matching a regex (stripped), or ''."""
        rx = re.compile(pattern)
        for line in reversed(self._lines):
            if rx.search(line):
                return line.strip()
        return ""


# ─────────────────────────────────────────────
#  LOG VIEWER  (floating window mirroring the clean output)
# ─────────────────────────────────────────────

class LogViewer(tk.Toplevel):
    """
    Floating window mirroring a tab's clean output stream live. It renders the
    ConsoleBuffer's backlog on open, then receives each new chunk as an
    observer. Auto-scroll can be toggled; text is selectable/copyable. The
    noisy engine diagnostics are never shown here — only what the program
    itself prints.
    """

    def __init__(self, master, console, title, app=None):
        super().__init__(master)
        self.title(title)
        self._app = app
        geo = app.settings.get("log_geometry") if app is not None else None
        self.geometry(geo if (geo and _geometry_on_screen(self, geo)) else "860x520")
        self.minsize(480, 280)
        self._console = console

        top = ttk.Frame(self, padding=(8, 6))
        top.pack(fill="x")
        self.autoscroll = tk.BooleanVar(value=True)
        ttk.Checkbutton(top, text="Auto-scroll to newest entries",
                        variable=self.autoscroll).pack(side="left")
        ttk.Label(top, text="Program output (no engine diagnostics)",
                  foreground="#666").pack(side="right")

        body = ttk.Frame(self)
        body.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.pane = LogPane(body)
        self.pane.pack(fill="both", expand=True)

        self.pane.feed(console.text_timestamped(), stamp=False)
        self.pane.text.see("end")
        console.add_observer(self._on_console)
        self.protocol("WM_DELETE_WINDOW", self._close)

    def bind_console(self, console, title=None):
        """Point this window at a different tab's output stream, redrawing its
        backlog. Lets one shared log window follow whichever tool is active."""
        if title:
            self.title(title)
        if console is self._console:
            return
        self._console.remove_observer(self._on_console)
        self._console = console
        self.pane.clear()
        self.pane.feed(console.text_timestamped(), stamp=False)
        self.pane.text.see("end")
        console.add_observer(self._on_console)

    def _on_console(self, chunk):
        if not self.winfo_exists():
            return
        if chunk is None:
            self.pane.clear()
        else:
            self.pane.feed(chunk)
            if self.autoscroll.get():
                self.pane.text.see("end")

    def save_geometry(self):
        if self._app is not None and self.winfo_exists():
            self._app.settings["log_geometry"] = self.geometry()
            save_settings(self._app.settings)

    def _close(self):
        self.save_geometry()
        self._console.remove_observer(self._on_console)
        self.destroy()
