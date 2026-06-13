"""
toolbox_gui.py
--------------
Windows GUI for the Image Toolbox — Phase 2 of the standalone-app refactor.

Two tabs:
  * Batch Upscaler – drives batch_upscale.py
  * Tag & Rename   – drives tag_and_rename.py

The tools run as subprocesses of the toolbox venv's Python. The main window
shows a two-row status (previous + current file) and a responsive thumbnail
wall; the full clean program output is available on demand in a floating log
window (View log). Control (pause / resume / stop) is sent over the child's
stdin as one command per line — see PauseController._watch_stdin in
batch_upscale.py and RemoteControl in tag_and_rename.py.

Launch (no console window):
    .venv\\Scripts\\pythonw.exe toolbox_gui.py
or double-click "Image Toolbox.cmd".

Requires only the Python standard library (tkinter).
"""

import os
import re
import sys
import json
import queue
import codecs
import threading
import subprocess
import urllib.request

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
APP_TITLE  = "Image Toolbox"
# Shown in the main window title bar. On a release, set this to the tag (e.g.
# "0.1.3") and drop the "-experimental" suffix.
APP_VERSION = "0.1.3-experimental"

CREATE_NO_WINDOW = 0x08000000

# Matches the per-image counters both scripts print, e.g. "[37/59]"
PROGRESS_RE = re.compile(r"\[(\d+)/(\d+)\]")


# ─────────────────────────────────────────────
#  CONFIG / INTERPRETER
# ─────────────────────────────────────────────

def _load_config():
    path = os.path.join(SCRIPT_DIR, "config.json")
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return {}

CFG = _load_config()


def _resolve_python():
    """Interpreter used to run the tools — the toolbox venv's python."""
    venv_py = os.path.expandvars(CFG.get("seedvr2", {}).get("venv_python", ""))
    if venv_py:
        p = venv_py if os.path.isabs(venv_py) else os.path.join(SCRIPT_DIR, venv_py)
        if os.path.exists(p):
            return p
    return sys.executable

PYTHON_EXE = _resolve_python()


def _ollama_release_vram():
    """
    Best-effort backup: ask Ollama to unload the tagging model so VRAM is
    freed even if tag_and_rename.py was killed before its own unload ran.
    Checks /api/ps first so it never triggers a load of an unloaded model.
    Runs in a background thread; all failures are silently ignored.
    """
    try:
        o     = CFG.get("ollama", {})
        url   = o.get("url", "http://127.0.0.1:11434")
        model = o.get("model", "llava:34b")
        with urllib.request.urlopen(f"{url}/api/ps", timeout=5) as resp:
            loaded = [m.get("name", "") for m in json.loads(resp.read()).get("models", [])]
        if not any(model.split(":")[0] in name for name in loaded):
            return
        payload = json.dumps({"model": model, "keep_alive": 0}).encode("utf-8")
        req = urllib.request.Request(f"{url}/api/generate", data=payload,
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=30)
    except Exception:
        pass


# ─────────────────────────────────────────────
#  GUI SETTINGS  (user preferences, e.g. default folders)
# ─────────────────────────────────────────────

SETTINGS_PATH = os.path.join(SCRIPT_DIR, "gui_settings.json")


def load_settings():
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_settings(settings):
    try:
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2)
    except OSError:
        pass   # preferences are best-effort


_GEOMETRY_RE = re.compile(r"^(\d+)x(\d+)([+-]\d+)([+-]\d+)$")


def _geometry_on_screen(win, geo):
    """True if a saved 'WxH+X+Y' string is sane and at least partly visible.
    Guards against restoring a window onto a monitor that is no longer there."""
    m = _GEOMETRY_RE.match(geo or "")
    if not m:
        return False
    w, h, x, y = (int(g) for g in m.groups())
    if not (300 <= w <= 10000 and 200 <= h <= 10000):
        return False
    sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
    # Require a ~100px sliver to remain reachable (negative x/y is valid on a
    # secondary monitor placed to the left of / above the primary one).
    if x > sw - 100 or y > sh - 100 or x + w < 100 or y + h < 100:
        return False
    return True


# ─────────────────────────────────────────────
#  TOOLTIP
# ─────────────────────────────────────────────

class Tooltip:
    """Small hover tooltip attached to a widget."""

    def __init__(self, widget, text, delay=500):
        self.widget = widget
        self.text   = text
        self.delay  = delay
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
    """Format a duration as 'dd, hh, mm, ss' — e.g. '04d, 11h, 23m, 35s'."""
    s = int(max(0, round(seconds)))
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return f"{d:02d}d, {h:02d}h, {m:02d}m, {s:02d}s"


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
#  LOG PANE
# ─────────────────────────────────────────────

class LogPane(ttk.Frame):
    """
    Read-only text console. Understands carriage returns the way a terminal
    does (an in-place progress update erases the current line), so the
    scripts' live counters render correctly instead of flooding the log.
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

    def feed(self, data):
        data = sanitize(data.replace("\r\n", "\n"))
        if not data:
            return
        t = self.text
        t.configure(state="normal")
        follow = t.yview()[1] >= 0.999   # auto-scroll only if already at the bottom
        for token in re.split("([\r\n])", data):
            if token == "\n":
                self._pending_cr = False
                t.insert("end", "\n")
            elif token == "\r":
                self._pending_cr = True
            elif token:
                if self._pending_cr:
                    self._pending_cr = False
                    t.delete("end-1c linestart", "end-1c")
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
            elif token == "\r":
                self._pending_cr = True
            elif token:
                if self._pending_cr:
                    self._pending_cr = False
                    self._lines[-1] = ""
                self._lines[-1] += token
        if len(self._lines) > self.MAX_LINES:
            del self._lines[:len(self._lines) - self.MAX_LINES]
        for cb in list(self._observers):
            cb(data)

    def clear(self):
        self._lines      = [""]
        self._pending_cr = False
        for cb in list(self._observers):
            cb(None)

    def text(self):
        return "\n".join(self._lines)

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

        self.pane.feed(console.text())
        self.pane.text.see("end")
        console.add_observer(self._on_console)
        self.protocol("WM_DELETE_WINDOW", self._close)

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
        self._viewer        = None    # open LogViewer window, if any
        self.console        = ConsoleBuffer()   # clean output stream (backlog + live)
        self._phase_text    = "Ready."          # activity/phase line (prep + final)
        self._final_top     = ""                # summary line shown above the final message
        self._finished      = True              # True when no run is in progress

    # ── UI helpers ──────────────────────────────────────────────────────────

    def _build_output_area(self, row):
        """Progress bar + two-row status + full-width image preview wall."""
        pf = ttk.Frame(self)
        pf.grid(row=row, column=0, columnspan=4, sticky="ew", pady=(10, 2))
        ttk.Label(pf, text="Est. Time Remaining:").pack(side="left")
        self.eta_var = tk.StringVar(value="—")
        ttk.Label(pf, textvariable=self.eta_var, width=20,
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
        ttk.Label(zb, text="Thumbnail size:").pack(side="left", padx=(0, 4))
        ttk.Button(zb, text="–", width=3,
                   command=lambda: self.strip.zoom_out()).pack(side="left")
        ttk.Button(zb, text="+", width=3,
                   command=lambda: self.strip.zoom_in()).pack(side="left", padx=(2, 0))

        cell = int(self.app.settings.get("thumb_cell", CELL_DEFAULT))
        self.strip = FilmStrip(body, cell=cell, on_zoom=self._save_cell)
        self.strip.grid(row=1, column=0, sticky="nsew", pady=(6, 0))
        Tooltip(self.strip.canvas,
                "Double-click an image to open it • use +/– to resize")

    def _save_cell(self, cell):
        self.app.settings["thumb_cell"] = cell
        save_settings(self.app.settings)

    # ── Two-row status ───────────────────────────────────────────────────────

    def _on_phase_change(self):
        self._phase_text = self.status_var.get()
        self._refresh_status()

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

    # ── Process control ─────────────────────────────────────────────────────

    @property
    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def launch(self, script, args):
        cmd = [PYTHON_EXE, "-u", os.path.join(SCRIPT_DIR, script)] + args
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"
        try:
            self.proc = subprocess.Popen(
                cmd, cwd=SCRIPT_DIR,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                creationflags=CREATE_NO_WINDOW, env=env,
            )
        except Exception as exc:
            messagebox.showerror(APP_TITLE, f"Could not start {script}:\n{exc}")
            return False
        self._finished = False
        if hasattr(self, "viewlog_btn"):
            self.viewlog_btn.configure(state="normal")
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
        dispatch them to _handle_event. Markers always start at the beginning
        of a line but may be split across read chunks, so the parser keeps
        state between calls (including holding back an ambiguous prefix like
        "@@TB" that arrives at the very end of a chunk).
        """
        out  = []
        data = self._hold + text
        self._hold = ""
        pos, n = 0, len(data)
        while pos < n:
            if self._marker_buf is not None:        # inside a marker line
                nl = data.find("\n", pos)
                if nl < 0:
                    self._marker_buf += data[pos:]
                    pos = n
                else:
                    self._marker_buf += data[pos:nl]
                    self._on_marker(self._marker_buf)
                    self._marker_buf    = None
                    self._at_line_start = True
                    pos = nl + 1
                continue
            if self._at_line_start:
                head = data[pos:pos + len(GUI_MARKER)]
                if head == GUI_MARKER:
                    self._marker_buf = ""
                    pos += len(GUI_MARKER)
                    continue
                if pos + len(head) == n and GUI_MARKER.startswith(head):
                    self._hold = head               # might be a marker — wait for more
                    break
                self._at_line_start = False
            # Both \n and \r end a "line" for marker detection: the scripts'
            # in-place progress updates end with \r, and a marker printed
            # right after one starts at a \r boundary, not a \n boundary.
            cands = [i for i in (data.find("\n", pos), data.find("\r", pos)) if i >= 0]
            if not cands:
                out.append(data[pos:])
                pos = n
            else:
                nxt = min(cands)
                out.append(data[pos:nxt + 1])
                self._at_line_start = True
                pos = nxt + 1
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
        elif kind == "RENAME" and payload:
            try:
                old, new = json.loads(payload)
            except (ValueError, TypeError):
                return
            self.strip.rename(old, new)
            if self.preview_name.get() == os.path.basename(old):
                self.preview_name.set(os.path.basename(new))
        elif kind == "ETA" and payload:
            self._handle_eta(payload)
        elif kind == "LOG" and payload:
            self._log_path = payload
            self.viewlog_btn.configure(state="normal")

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

    def _tick(self):
        """Every poll cycle: display freshly decoded strip thumbnails."""
        self.strip.drain()

    def _view_log(self):
        if self._viewer is not None and self._viewer.winfo_exists():
            self._viewer.lift()
            self._viewer.focus_set()
            return
        self._viewer = LogViewer(self, self.console,
                                 f"{APP_TITLE} — program output", app=self.app)

    def _scan_progress(self, text):
        matches = PROGRESS_RE.findall(text)
        if not matches:
            return
        cur, tot = (int(x) for x in matches[-1])
        if tot <= 0:
            return
        self.progress.set(cur * 100 / tot)
        self.status_var.set(f"Processing image {cur} of {tot} …")

    def _handle_eta(self, payload):
        """ETA event from the running tool: 'elapsed|processed|idx|total'.
        Estimate = (elapsed / images actually processed this session) ×
        images still to go. Using the processed count — not the position
        counter, which also advances on skipped files — keeps the average
        per-image time honest. The elapsed value is pause-excluded."""
        try:
            elapsed, processed, idx, total = payload.split("|")
            elapsed   = float(elapsed)
            processed = int(processed)
            idx       = int(idx)
            total     = int(total)
        except ValueError:
            return
        if total > 0:
            self.progress.set(idx * 100 / total)
        if processed > 0 and total > 0:
            avg       = elapsed / processed
            remaining = max(0, total - idx)
            self.eta_var.set(_fmt_eta(avg * remaining))

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

    def on_exit(self, code):
        """Override in subclasses."""


# ─────────────────────────────────────────────
#  TAB 1 — BATCH UPSCALER
# ─────────────────────────────────────────────

# Marker prefix batch_upscale.py uses for GUI event lines ("KIND|payload") —
# intercepted here, never shown in the log.
GUI_MARKER   = "@@TBX@@"
THUMB_MASTER = 512           # px — bounding box the master thumbnail is decoded to
CELL_DEFAULT = 150           # px — default square cell edge for one thumbnail
CELL_MIN     = 90            # px — smallest cell (zoom out)
CELL_HARD_MAX = 1000         # px — absolute ceiling for a cell
CELL_STEP    = 30            # px — change per +/- click
GRID_GAP     = 10            # px — gap between cells
BATCH_SIZE   = 100           # thumbnails decoded per batch


class FilmStrip(ttk.Frame):
    """
    Responsive thumbnail wall of the queued images.

    The full ordered queue arrives via a QUEUE event. Thumbnails flow left to
    right and wrap into as many columns as the width allows, re-flowing on
    resize; each image keeps its aspect ratio inside a square cell. The image
    currently being processed is highlighted and scrolled into view, and any
    thumbnail can be double-clicked to open it. The +/- buttons (zoom_in /
    zoom_out) change the cell size.

    Thumbnails are decoded in a background thread, one batch of BATCH_SIZE
    around the current image at a time (when the current image crosses into
    the next batch, that batch is loaded), so very large queues stay snappy.
    """

    BG          = "#202329"
    HILITE      = "#4f9cff"
    OUTLINE     = "#3a3f48"

    def __init__(self, master, cell=CELL_DEFAULT, on_zoom=None):
        super().__init__(master)
        self._on_zoom = on_zoom
        self.canvas = tk.Canvas(self, highlightthickness=0, bg=self.BG)
        ys = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=ys.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        ys.grid(row=0, column=1, sticky="ns")
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        self.canvas.bind("<MouseWheel>", self._wheel)
        self.canvas.bind("<Double-Button-1>", self._on_double)
        self.canvas.bind("<Configure>", self._on_resize)

        self._paths   = []        # full queue
        self._index   = {}        # path -> position in queue
        self._renamed = {}        # old path -> new path (renamed mid-run)
        self._batch   = None      # batch number currently displayed
        self._order   = []        # paths in the displayed batch, in order
        self._pos     = {}        # path -> position within the batch
        self._master  = {}        # path -> PIL master thumbnail (<= THUMB_MASTER)
        self._photo   = {}        # path -> PhotoImage at the current cell size
        self._img_id  = {}        # path -> canvas image item id
        self._current = None
        self._cell    = max(CELL_MIN, min(CELL_HARD_MAX, int(cell)))
        self._cols    = 1
        self._hl_id   = None      # highlight rectangle item id
        self._gen     = 0         # invalidates stale loader threads
        self._q       = queue.Queue()
        self._resize_after = None

    def _wheel(self, event):
        self.canvas.yview_scroll(-(event.delta // 120) * 2, "units")

    # ── Zoom ─────────────────────────────────────────────────────────────────

    def zoom_in(self):
        self._set_cell(self._cell + CELL_STEP)

    def zoom_out(self):
        self._set_cell(self._cell - CELL_STEP)

    def _max_cell(self):
        """A cell may grow until it fills the smaller of the viewport's width
        or height (one image fills the preview), bounded by a hard ceiling."""
        w = max(self.canvas.winfo_width(), 1)
        h = max(self.canvas.winfo_height(), 1)
        return max(CELL_MIN, min(CELL_HARD_MAX, min(w, h) - 2 * GRID_GAP))

    def _set_cell(self, cell):
        cell = max(CELL_MIN, min(self._max_cell(), cell))
        if cell == self._cell:
            return
        self._cell = cell
        for p in list(self._master):     # regenerate photos at the new size
            self._make_photo(p)
        self._relayout(force=True)
        if self._on_zoom:
            self._on_zoom(self._cell)

    # ── Queue management ─────────────────────────────────────────────────────

    def set_queue(self, paths):
        self._paths = [p.strip() for p in paths if p.strip()]
        self._index = {p: i for i, p in enumerate(self._paths)}
        self._renamed = {}
        self._batch = None              # force a rebuild on the next IMG event

    def clear(self):
        self._gen += 1
        self._paths, self._index, self._renamed = [], {}, {}
        self._batch, self._current = None, None
        self._build_cells([])

    def rename(self, old, new):
        """
        A queued file changed name on disk (tag rename or undo revert).
        Remap every reference so highlighting, double-click-to-open and
        late-arriving thumbnails all follow the file to its new path.
        """
        if old == new or old not in self._index:
            return
        i = self._index.pop(old)
        self._paths[i] = new
        self._index[new] = i
        self._renamed[old] = new
        if self._current == old:
            self._current = new
        if old in self._pos:
            self._pos[new] = self._pos.pop(old)
            self._order[self._pos[new]] = new
        for d in (self._master, self._photo, self._img_id):
            if old in d:
                d[new] = d.pop(old)

    def set_current(self, path):
        if path not in self._index:     # rescan oddity — still show the image
            self._index[path] = len(self._paths)
            self._paths.append(path)
        idx   = self._index[path]
        batch = idx // BATCH_SIZE
        if batch != self._batch:
            self._batch = batch
            sl = self._paths[batch * BATCH_SIZE:(batch + 1) * BATCH_SIZE]
            self._build_cells(sl, first=path)
            self.canvas.yview_moveto(0)     # new batch — start at the top
        # The current image is only marked (highlighted); the view is NOT
        # moved, so scrolling/browsing is never interrupted mid-run.
        self._current = path
        self._draw_highlight()

    # ── Layout ───────────────────────────────────────────────────────────────

    def _build_cells(self, order, first=None):
        self._order   = list(order)
        self._pos     = {p: i for i, p in enumerate(order)}
        self._master  = {}
        self._photo   = {}
        self._img_id  = {}
        self._hl_id   = None
        self._relayout(force=True)
        self._start_loader(order, first=first)

    def _grid_cols(self):
        w = max(self.canvas.winfo_width(), 1)
        return max(1, (w - GRID_GAP) // (self._cell + GRID_GAP))

    def _cell_origin(self, i):
        col = i % self._cols
        row = i // self._cols
        x0  = GRID_GAP + col * (self._cell + GRID_GAP)
        y0  = GRID_GAP + row * (self._cell + GRID_GAP)
        return x0, y0

    def _relayout(self, force=False):
        cols = self._grid_cols()
        if not force and cols == self._cols:
            return
        self._cols = cols
        self.canvas.delete("all")
        self._img_id = {}
        self._hl_id  = None
        cell = self._cell
        for i, p in enumerate(self._order):
            x0, y0 = self._cell_origin(i)
            self.canvas.create_rectangle(x0, y0, x0 + cell, y0 + cell,
                                         outline=self.OUTLINE, width=1)
            photo = self._photo.get(p)
            if photo is not None:
                self._img_id[p] = self.canvas.create_image(
                    x0 + cell / 2, y0 + cell / 2, image=photo)
        self._update_scrollregion()
        self._draw_highlight()

    def _update_scrollregion(self):
        cols = max(self._cols, 1)
        rows = (len(self._order) + cols - 1) // cols
        h    = GRID_GAP + rows * (self._cell + GRID_GAP)
        w    = max(self.canvas.winfo_width(), 1)
        self.canvas.configure(
            scrollregion=(0, 0, w, max(h, self.canvas.winfo_height())))

    def _place(self, p):
        """Draw or refresh one thumbnail at its grid position."""
        i = self._pos.get(p)
        if i is None:
            return
        photo = self._photo.get(p)
        if photo is None:
            return
        x0, y0 = self._cell_origin(i)
        cx, cy = x0 + self._cell / 2, y0 + self._cell / 2
        if p in self._img_id:
            self.canvas.itemconfigure(self._img_id[p], image=photo)
            self.canvas.coords(self._img_id[p], cx, cy)
        else:
            self._img_id[p] = self.canvas.create_image(cx, cy, image=photo)

    def _draw_highlight(self):
        if self._hl_id is not None:
            self.canvas.delete(self._hl_id)
            self._hl_id = None
        i = self._pos.get(self._current) if self._current else None
        if i is None:
            return
        x0, y0 = self._cell_origin(i)
        cell = self._cell
        self._hl_id = self.canvas.create_rectangle(
            x0 - 1, y0 - 1, x0 + cell + 1, y0 + cell + 1,
            outline=self.HILITE, width=3)

    def _on_resize(self, _event):
        if self._resize_after is not None:
            self.after_cancel(self._resize_after)
        self._resize_after = self.after(150, self._do_resize)

    def _do_resize(self):
        self._resize_after = None
        # A shrunk viewport may no longer fit the current cell — pull it in.
        max_cell = self._max_cell()
        if self._cell > max_cell:
            self._cell = max_cell
            for p in list(self._master):
                self._make_photo(p)
            self._relayout(force=True)
            if self._on_zoom:
                self._on_zoom(self._cell)
        elif self._grid_cols() != self._cols:
            self._relayout(force=True)
        else:
            self._update_scrollregion()

    def _on_double(self, event):
        x = self.canvas.canvasx(event.x)
        y = self.canvas.canvasy(event.y)
        col = int((x - GRID_GAP) // (self._cell + GRID_GAP))
        row = int((y - GRID_GAP) // (self._cell + GRID_GAP))
        if col < 0 or col >= self._cols:
            return
        idx = row * self._cols + col
        if 0 <= idx < len(self._order):
            self._open(self._order[idx])

    @staticmethod
    def _open(path):
        if os.path.exists(path):
            try:
                os.startfile(path)
            except OSError:
                pass

    # ── Background thumbnail loading ─────────────────────────────────────────

    def _start_loader(self, paths, first=None):
        self._gen += 1
        if first in paths:              # decode the current image first
            i = paths.index(first)
            paths = paths[i:] + paths[:i]
        threading.Thread(target=self._load_batch,
                         args=(list(paths), self._gen), daemon=True).start()

    def _load_batch(self, paths, gen):
        from PIL import Image, ImageOps
        for p in paths:
            if gen != self._gen:
                return                  # batch changed — abandon
            img = None
            try:
                with Image.open(p) as f:
                    f.draft("RGB", (THUMB_MASTER, THUMB_MASTER))
                    f = ImageOps.exif_transpose(f)
                    f.thumbnail((THUMB_MASTER, THUMB_MASTER), Image.LANCZOS)
                    img = f.convert("RGB")
            except Exception:
                img = None
            self._q.put((gen, p, img))

    def _make_photo(self, p):
        from PIL import Image, ImageTk
        m = self._master.get(p)
        if m is None:
            return
        # Fit the image to the square cell, preserving aspect. Cells larger
        # than the decoded master upscale it (mildly soft at extreme zoom).
        scale = min(self._cell / m.width, self._cell / m.height)
        w = max(1, int(round(m.width * scale)))
        h = max(1, int(round(m.height * scale)))
        img = m if (w == m.width and h == m.height) else m.resize((w, h), Image.LANCZOS)
        self._photo[p] = ImageTk.PhotoImage(img)

    def drain(self):
        """Main thread (via _tick): turn decoded masters into placed thumbnails."""
        changed = False
        while True:
            try:
                gen, p, img = self._q.get_nowait()
            except queue.Empty:
                break
            # The file may have been renamed while its thumbnail was decoding
            for _ in range(8):
                if p in self._renamed:
                    p = self._renamed[p]
                else:
                    break
            if gen != self._gen or p not in self._pos or img is None:
                continue
            self._master[p] = img
            self._make_photo(p)
            self._place(p)
            changed = True
        if changed:
            self._draw_highlight()


class UpscaleTab(ToolTab):

    def __init__(self, notebook, app):
        super().__init__(notebook, app)
        u = CFG.get("upscale", {})
        self.src_var      = tk.StringVar()
        self.out_var      = tk.StringVar()
        self.cutoff_var   = tk.IntVar(value=int(u.get("upscale_cutoff_pct", 66)))
        self.save_src_var = tk.BooleanVar(value=False)
        self.save_out_var = tk.BooleanVar(value=False)
        self._paused      = False
        self._processing  = False    # True once the per-image phase started
        self._cancelled   = False    # user cancelled a preparation phase
        self._phase       = ""       # current phase text (from STATUS events)
        self._build()

        # Restore saved default folders, then keep the checkboxes in sync
        s = app.settings
        src_default = s.get("default_source", "")
        if src_default and os.path.isdir(src_default):
            self.src_var.set(src_default)
            self.save_src_var.set(True)
            if not self.out_var.get().strip():
                self.out_var.set(os.path.join(src_default, "__upscaled__"))
        out_default = s.get("default_output", "")
        if out_default:
            self.out_var.set(out_default)
            self.save_out_var.set(True)
        self.src_var.trace_add("write", lambda *_: self._sync_default("src"))
        self.out_var.trace_add("write", lambda *_: self._sync_default("out"))
        self._sync_default("src")
        self._sync_default("out")

    def _build(self):
        ttk.Label(self, text="Photo folder:").grid(row=0, column=0, sticky="w", pady=3)
        ttk.Entry(self, textvariable=self.src_var).grid(row=0, column=1, sticky="ew", padx=6, pady=3)
        ttk.Button(self, text="Browse…", command=self._pick_src).grid(row=0, column=2, pady=3)
        self.save_src_chk = ttk.Checkbutton(
            self, text="Save as Default", variable=self.save_src_var,
            command=lambda: self._on_save_toggle("src"), state="disabled")
        self.save_src_chk.grid(row=0, column=3, sticky="w", padx=(8, 0), pady=3)

        ttk.Label(self, text="Save upscaled to:").grid(row=1, column=0, sticky="w", pady=3)
        ttk.Entry(self, textvariable=self.out_var).grid(row=1, column=1, sticky="ew", padx=6, pady=3)
        ttk.Button(self, text="Browse…", command=self._pick_out).grid(row=1, column=2, pady=3)
        self.save_out_chk = ttk.Checkbutton(
            self, text="Save as Default", variable=self.save_out_var,
            command=lambda: self._on_save_toggle("out"), state="disabled")
        self.save_out_chk.grid(row=1, column=3, sticky="w", padx=(8, 0), pady=3)

        opts = ttk.Frame(self)
        opts.grid(row=2, column=0, columnspan=4, sticky="w", pady=(6, 0))
        ttk.Label(opts, text="Skip images already at").pack(side="left")
        ttk.Spinbox(opts, from_=0, to=99, width=4, textvariable=self.cutoff_var).pack(side="left", padx=4)
        ttk.Label(opts, text="% of the target size or larger   (0 = upscale everything eligible)").pack(side="left")

        btns = ttk.Frame(self)
        btns.grid(row=3, column=0, columnspan=4, sticky="w", pady=(10, 0))
        self.start_btn = ttk.Button(btns, text="Start upscaling", command=self._start)
        self.pause_btn = ttk.Button(btns, text="Pause", command=self._pause_or_cancel, state="disabled")
        self.stop_btn  = ttk.Button(btns, text="Stop after current image", command=self._stop, state="disabled")
        self.open_btn  = ttk.Button(btns, text="Open output folder", command=self._open_out)
        self.viewlog_btn = ttk.Button(btns, text="View log", command=self._view_log, state="disabled")
        for b in (self.start_btn, self.pause_btn, self.stop_btn, self.open_btn, self.viewlog_btn):
            b.pack(side="left", padx=(0, 6))

        self._build_output_area(row=4)

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
        elif kind == "PROG":
            cur, _, tot = payload.partition("|")
            try:
                cur, tot = int(cur), int(tot)
            except ValueError:
                return
            if tot > 0:
                self.progress.set(cur * 100 / tot)
                if not self._cancelled:
                    self.status_var.set(f"{self._phase}  ({cur:,} / {tot:,})")
        else:
            super()._handle_event(kind, payload)

    # ── Default-folder preferences ───────────────────────────────────────────

    def _sync_default(self, which):
        """
        Keep a Save-as-Default checkbox consistent with its path field:
        enabled only for a valid path, and while checked the latest valid
        path is persisted.
        """
        if which == "src":
            chk, var, key = self.save_src_chk, self.save_src_var, "default_source"
            path  = self.src_var.get().strip()
            valid = bool(path) and os.path.isdir(path)
        else:
            chk, var, key = self.save_out_chk, self.save_out_var, "default_output"
            path  = self.out_var.get().strip()
            # The output folder is created on demand — accept it if it exists
            # or can be created inside an existing folder.
            valid = bool(path) and (os.path.isdir(path) or
                                    os.path.isdir(os.path.dirname(path)))
        chk.configure(state="normal" if valid else "disabled")
        if valid and var.get() and self.app.settings.get(key) != path:
            self.app.settings[key] = path
            save_settings(self.app.settings)

    def _on_save_toggle(self, which):
        key = "default_source" if which == "src" else "default_output"
        var = self.save_src_var if which == "src" else self.save_out_var
        if var.get():
            self._sync_default(which)
        elif key in self.app.settings:
            del self.app.settings[key]
            save_settings(self.app.settings)

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
        try:
            cutoff = max(0, min(99, int(self.cutoff_var.get())))
        except (ValueError, tk.TclError):
            cutoff = int(CFG.get("upscale", {}).get("upscale_cutoff_pct", 66))
            self.cutoff_var.set(cutoff)
        if not self.confirm_gpu_overlap():
            return

        self.progress.set(0)
        self._reset_stream_state()
        self.status_var.set("Starting — loading the AI engine (the first run can take a few minutes) …")
        if self.launch("batch_upscale.py", [src, out, str(cutoff)]):
            self._paused     = False
            self._processing = False
            self._cancelled  = False
            self._phase      = ""
            self._set_running(True)
            # Until per-image processing starts, this button cancels the run
            self.pause_btn.configure(text="Cancel")

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
        if self._paused:
            self.status_var.set("Pausing — finishes the current image first …")

    def _stop(self):
        self.send("q")
        self.stop_btn.configure(state="disabled")
        self.status_var.set("Stopping — finishes the current image first …")

    def _set_running(self, running):
        self.start_btn.configure(state="disabled" if running else "normal")
        self.pause_btn.configure(state="normal"   if running else "disabled")
        self.stop_btn.configure(state="normal"    if running else "disabled")
        # VRAM is fully committed during an upscale run — lock out the other tool
        self.app.set_tag_tab_enabled(not running)

    def on_exit(self, code):
        self._set_running(False)
        self._paused = False
        self.pause_btn.configure(text="Pause")
        self.eta_var.set("—")
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
            self.status_var.set(f"Stopped with an error (code {code}) — see the messages above.")


# ─────────────────────────────────────────────
#  TAB 2 — TAG & RENAME
# ─────────────────────────────────────────────

LANGUAGES = [
    "English", "Romanian", "French", "German", "Spanish", "Italian",
    "Portuguese", "Dutch", "Polish", "Hungarian", "Czech", "Greek",
    "Russian", "Ukrainian", "Turkish", "Swedish", "Norwegian", "Danish",
    "Finnish",
]


class TagTab(ToolTab):

    def __init__(self, notebook, app):
        super().__init__(notebook, app)
        self.dir_var    = tk.StringVar()
        self.lang_var   = tk.StringVar(value="English")
        self.ftag_var   = tk.BooleanVar(value=False)
        self.fren_var   = tk.BooleanVar(value=False)
        self.scope_var  = tk.StringVar(value="all")
        self._mode      = "tag"          # "tag" | "undo" — for the exit message
        self._build()

    def _build(self):
        ttk.Label(self, text="Photo folder:").grid(row=0, column=0, sticky="w", pady=3)
        ttk.Entry(self, textvariable=self.dir_var).grid(row=0, column=1, sticky="ew", padx=6, pady=3)
        ttk.Button(self, text="Browse…", command=self._pick_dir).grid(row=0, column=2, pady=3)

        opts = ttk.Frame(self)
        opts.grid(row=1, column=0, columnspan=3, sticky="w", pady=(6, 0))
        ttk.Label(opts, text="Description language:").pack(side="left")
        ttk.Combobox(opts, textvariable=self.lang_var, values=LANGUAGES,
                     width=14).pack(side="left", padx=(4, 16))
        ttk.Checkbutton(opts, text="Tag all images (ignore size and previous tags)",
                        variable=self.ftag_var).pack(side="left", padx=(0, 12))
        ttk.Checkbutton(opts, text="Rename all images (not just camera names)",
                        variable=self.fren_var).pack(side="left")

        btns = ttk.Frame(self)
        btns.grid(row=2, column=0, columnspan=3, sticky="w", pady=(10, 0))
        self.start_btn  = ttk.Button(btns, text="Start tagging", command=self._start)
        self.resume_btn = ttk.Button(btns, text="Resume after error", command=self._resume, state="disabled")
        self.stop_btn   = ttk.Button(btns, text="Stop after current image", command=self._stop, state="disabled")
        self.open_btn    = ttk.Button(btns, text="Open photo folder", command=self._open_dir)
        self.viewlog_btn = ttk.Button(btns, text="View log", command=self._view_log, state="disabled")
        for b in (self.start_btn, self.resume_btn, self.stop_btn, self.open_btn, self.viewlog_btn):
            b.pack(side="left", padx=(0, 6))

        undo = ttk.LabelFrame(self, text=" Undo previous runs ", padding=(8, 4))
        undo.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(12, 0))
        ttk.Radiobutton(undo, text="Undo everything", value="all",
                        variable=self.scope_var).pack(side="left", padx=(0, 10))
        ttk.Radiobutton(undo, text="File names only", value="names",
                        variable=self.scope_var).pack(side="left", padx=(0, 10))
        ttk.Radiobutton(undo, text="Descriptions only", value="exif",
                        variable=self.scope_var).pack(side="left", padx=(0, 16))
        self.undo_btn = ttk.Button(undo, text="Undo this folder…", command=self._undo)
        self.undo_btn.pack(side="left")

        self._build_output_area(row=4)

    # ── Actions ──────────────────────────────────────────────────────────────

    def _pick_dir(self):
        folder = filedialog.askdirectory(title="Choose the folder with photos to tag")
        if folder:
            self.dir_var.set(os.path.normpath(folder))

    def _open_dir(self):
        folder = self.dir_var.get().strip()
        if folder and os.path.isdir(folder):
            os.startfile(folder)
        else:
            messagebox.showinfo(APP_TITLE, "Please choose a valid photo folder first.")

    def _valid_dir(self):
        folder = self.dir_var.get().strip()
        if not folder or not os.path.isdir(folder):
            messagebox.showwarning(APP_TITLE, "Please choose a valid photo folder first.")
            return None
        return folder

    def _start(self):
        folder = self._valid_dir()
        if not folder:
            return
        if not self.confirm_gpu_overlap():
            return
        args = [folder, "--no-prompt"]
        if self.ftag_var.get():
            args.append("-ftag")
        if self.fren_var.get():
            args.append("-frename")
        lang = self.lang_var.get().strip()
        if lang and lang.lower() != "english":
            args.append(f"--language:{lang}")

        self._mode = "tag"
        self.progress.set(0)
        self._reset_stream_state()
        self.eta_var.set("calculating…")
        self.status_var.set("Starting — checking Ollama and scanning the folder …")
        if self.launch("tag_and_rename.py", args):
            self._set_running(True)

    def _undo(self):
        folder = self._valid_dir()
        if not folder:
            return
        scope = self.scope_var.get()
        scope_text = {
            "all":   "file names AND embedded descriptions",
            "names": "file names only",
            "exif":  "embedded descriptions only",
        }[scope]
        if not messagebox.askyesno(
                APP_TITLE,
                f"Undo the changes made by previous runs in:\n{folder}\n\n"
                f"This restores: {scope_text}.\n\nContinue?"):
            return
        args = [folder, "--undo-all"]
        if scope == "names":
            args.append("--names-only")
        elif scope == "exif":
            args.append("--exif-only")

        self._mode = "undo"
        self.progress.set(0)
        self._reset_stream_state()
        self.status_var.set("Undoing previous changes …")
        if self.launch("tag_and_rename.py", args):
            self._set_running(True)

    def _resume(self):
        self.send("r")
        self.status_var.set("Resuming …")

    def _stop(self):
        self.send("q")
        self.stop_btn.configure(state="disabled")
        self.status_var.set("Stopping — finishes the current image first …")

    def _set_running(self, running):
        self.start_btn.configure(state="disabled"  if running else "normal")
        self.undo_btn.configure(state="disabled"   if running else "normal")
        self.resume_btn.configure(state="normal"   if running else "disabled")
        self.stop_btn.configure(state="normal"     if running else "disabled")

    def on_exit(self, code):
        self._set_running(False)
        self.eta_var.set("—")
        # Backup VRAM release — the script unloads the model itself on a
        # graceful exit, but not if it was killed (e.g. app closed mid-image).
        threading.Thread(target=_ollama_release_vram, daemon=True).start()
        # Drain any strip thumbnail decodes still in flight after exit
        for delay in (250, 1000, 3000):
            self.after(delay, self._tick)
        if code == 0:
            self.progress.set(100)
            if self._mode == "undo":
                self.status_var.set("Undo finished — see the summary above.")
            else:
                self.status_var.set("Done. Descriptions written and files renamed where applicable.")
        else:
            self.status_var.set(f"Stopped with an error (code {code}) — see the messages above.")


# ─────────────────────────────────────────────
#  APP WINDOW
# ─────────────────────────────────────────────

class App(tk.Tk):

    def __init__(self):
        super().__init__()
        self.title(f"{APP_TITLE} {APP_VERSION}")
        self.minsize(780, 560)
        try:
            ttk.Style(self).theme_use("vista")
        except tk.TclError:
            pass

        self.settings = load_settings()
        self._last_normal_geo = None
        self._restore_geometry()

        self.nb = ttk.Notebook(self)
        self.upscale_tab = UpscaleTab(self.nb, self)
        self.tag_tab     = TagTab(self.nb, self)
        self.nb.add(self.upscale_tab, text="  Batch Upscaler  ")
        self.nb.add(self.tag_tab,     text="  Tag & Rename  ")
        self.nb.pack(fill="both", expand=True, padx=8, pady=8)

        self.bind("<Configure>", self._track_geometry)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── Window geometry persistence ──────────────────────────────────────────

    def _restore_geometry(self):
        geo = self.settings.get("main_geometry")
        self.geometry(geo if (geo and _geometry_on_screen(self, geo)) else "980x720")
        if self.settings.get("main_zoomed"):
            try:
                self.state("zoomed")
            except tk.TclError:
                pass

    def _track_geometry(self, event):
        # Remember the last *restored* (non-maximised) geometry so closing while
        # maximised still records a sensible size to come back to.
        if event.widget is self and self.state() == "normal":
            self._last_normal_geo = self.geometry()

    def _save_geometry(self):
        try:
            zoomed = (self.state() == "zoomed")
        except tk.TclError:
            zoomed = False
        geo = self._last_normal_geo or self.geometry()
        self.settings["main_geometry"] = geo
        self.settings["main_zoomed"]   = zoomed
        save_settings(self.settings)

    def other_tab(self, tab):
        return self.tag_tab if tab is self.upscale_tab else self.upscale_tab

    def set_tag_tab_enabled(self, enabled):
        """Grey out the Tag & Rename tab while an upscale run owns the GPU."""
        self.nb.tab(self.tag_tab, state="normal" if enabled else "disabled")

    def _on_close(self):
        busy = [t for t in (self.upscale_tab, self.tag_tab) if t.running]
        if busy:
            if not messagebox.askyesno(
                    APP_TITLE, "A task is still running.\nStop it and close the app?"):
                return
            for t in busy:
                t.send("q")
            # Brief grace period — a kill mid-EXIF-write could damage a photo.
            for t in busy:
                if t.proc is not None:
                    try:
                        t.proc.wait(timeout=5)
                    except Exception:
                        t.terminate()
        # Persist the main window layout and any open log window's layout.
        self._save_geometry()
        for t in (self.upscale_tab, self.tag_tab):
            v = getattr(t, "_viewer", None)
            if v is not None and v.winfo_exists():
                v.save_geometry()
        self.destroy()


def main():
    # Crisp text on high-DPI displays
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    App().mainloop()


if __name__ == "__main__":
    main()
