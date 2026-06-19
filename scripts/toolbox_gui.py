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
import time
import json
import queue
import codecs
import shutil
import datetime
import threading
import subprocess
import platform
import webbrowser
import urllib.parse
import urllib.request
import urllib.error

# Arm crash logging before the feature imports below, so even an import-time
# failure (e.g. a module the installer forgot to ship) leaves a crash log and a
# visible dialog instead of a silent split-second window. The try/except means
# a missing crash_logger.py can't itself reintroduce a silent crash.
try:
    import crash_logger
    crash_logger.install()
except Exception:
    crash_logger = None
    import traceback as _traceback
    import datetime as _datetime

    def _emergency_excepthook(exc_type, exc, tb):
        try:
            _d = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
            os.makedirs(_d, exist_ok=True)
            _p = os.path.join(
                _d, "crash_" + _datetime.datetime.now().strftime("%Y%m%d_%H%M%S") + ".log")
            with open(_p, "w", encoding="utf-8") as _f:
                _traceback.print_exception(exc_type, exc, tb, file=_f)
        except Exception:
            pass
        sys.__excepthook__(exc_type, exc, tb)

    sys.excepthook = _emergency_excepthook

import updater
import mqtt_publisher
import system_telemetry

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

# SCRIPT_DIR is where this module (and its sibling child scripts) live: the
# scripts/ folder. APP_ROOT is its parent — where config.json, gui_settings.json,
# the .venv, logs/, db/ and the seedvr2/ engine all live.
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
APP_ROOT   = os.path.dirname(SCRIPT_DIR)
APP_TITLE  = "Image Toolbox"
# Shown in the main window title bar. On a release, set this to the tag (e.g.
# "0.1.3") and drop the "-experimental" suffix.
APP_VERSION = "0.2.9-experimental"

if crash_logger:
    crash_logger.set_version(APP_VERSION)

CREATE_NO_WINDOW = 0x08000000

# Matches the per-image counters both scripts print, e.g. "[37/59]"
PROGRESS_RE = re.compile(r"\[(\d+)/(\d+)\]")


# ─────────────────────────────────────────────
#  CONFIG / INTERPRETER
# ─────────────────────────────────────────────

def _load_config():
    path = os.path.join(APP_ROOT, "config.json")
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return {}

CFG = _load_config()


def save_config(cfg=None):
    """
    Write config.json back to disk (the Settings tab edits CFG in place, then
    calls this). Returns True on success. The backend scripts read config.json
    fresh at launch, so saved changes take effect on the next run.
    """
    if cfg is None:
        cfg = CFG
    path = os.path.join(APP_ROOT, "config.json")
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=4)
        return True
    except OSError:
        return False


# Default folders the user pins for each tool. Stored in config.json so they
# travel with the rest of the configuration and are shown in the Settings tab.
#   upscale_source / upscale_output  – Batch Upscaler
#   tag_folder                       – Tag & Rename
def get_default_folder(key):
    return CFG.get("defaults", {}).get(key, "")


def set_default_folder(key, value):
    CFG.setdefault("defaults", {})[key] = value
    save_config()


# ─────────────────────────────────────────────
#  UPDATE PREFERENCES  (config.json "updates" section)
# ─────────────────────────────────────────────
# auto_check   – check GitHub for a newer release shortly after launch
# skip_version – a version the user chose to skip; never nag about it again
def update_auto_check_enabled():
    return bool(CFG.get("updates", {}).get("auto_check", True))


def set_update_auto_check(enabled):
    CFG.setdefault("updates", {})["auto_check"] = bool(enabled)
    save_config()


def update_skipped_version():
    return CFG.get("updates", {}).get("skip_version", "") or ""


def set_update_skipped_version(version):
    CFG.setdefault("updates", {})["skip_version"] = version or ""
    save_config()


# ─────────────────────────────────────────────
#  REPORT AN ISSUE  (Future Feature #3)
# ─────────────────────────────────────────────

def _newest_crash_log():
    """Path of the most recent logs/crash_*.log, or None. Best-effort."""
    try:
        log_dir = os.path.join(APP_ROOT, "logs")
        crashes = [f for f in os.listdir(log_dir)
                   if f.startswith("crash_") and f.endswith(".log")]
        if not crashes:
            return None
        crashes.sort()
        return os.path.join(log_dir, crashes[-1])
    except Exception:
        return None


def _issue_url():
    """
    Build a GitHub "new issue" URL pre-filled with the app version and basic
    environment, so reports arrive actionable. The GPU name is best-effort (it
    shells out to nvidia-smi) and the newest crash log, if any, is pointed at so
    the user knows what to attach. All fields fail safe to "unknown".
    """
    try:
        gpu = system_telemetry.gpu_name() or "unknown"
    except Exception:
        gpu = "unknown"
    crash = _newest_crash_log()
    crash_line = (f"- Newest crash log (please attach): {crash}\n"
                  if crash else "")
    body = (
        "**What happened?**\n\n\n"
        "**Steps to reproduce:**\n\n\n"
        "---\n"
        "*Environment (auto-filled — please keep):*\n"
        f"- Image Toolbox: {APP_VERSION}\n"
        f"- OS: {platform.platform()}\n"
        f"- Python: {sys.version.split()[0]}\n"
        f"- GPU: {gpu}\n"
        f"{crash_line}"
    )
    params = urllib.parse.urlencode({"title": "", "body": body})
    return f"https://github.com/{updater.GITHUB_REPO}/issues/new?{params}"


def report_issue():
    """Open a pre-filled GitHub new-issue page in the browser. Fail-safe: on any
    error, fall back to the plain issues page."""
    try:
        webbrowser.open(_issue_url())
    except Exception:
        try:
            webbrowser.open(f"https://github.com/{updater.GITHUB_REPO}/issues/new")
        except Exception:
            pass


# ─────────────────────────────────────────────
#  MQTT / HOME ASSISTANT  (config.json "mqtt" section)
# ─────────────────────────────────────────────

def mqtt_config():
    return CFG.get("mqtt", {})


def mqtt_enabled():
    """MQTT is active whenever a broker host is configured — no separate toggle.
    Clear the host in Settings to disable publishing."""
    return bool((mqtt_config().get("host") or "").strip())


# ─────────────────────────────────────────────
#  OLLAMA / DISCORD probes (used by the Settings tab)
# ─────────────────────────────────────────────

def ollama_installed():
    """True if the ollama executable is found on PATH."""
    return shutil.which("ollama") is not None


def ollama_list_models(url, timeout=5):
    """
    Query a running Ollama server for its installed models.
    Returns (ok, value): on success value is a list of model names,
    on failure value is a short error string.
    """
    try:
        endpoint = f"{url.rstrip('/')}/api/tags"
        with urllib.request.urlopen(endpoint, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        names = [m.get("name", "") for m in data.get("models", []) if m.get("name")]
        return True, names
    except Exception as exc:
        return False, str(exc)


def test_discord_webhook(url, timeout=10):
    """
    Verify a Discord webhook by GETting its metadata (same check setup.ps1 does).
    Returns (ok, message) — message names the channel on success.
    """
    url = (url or "").strip()
    if not url:
        return False, "No webhook URL entered."
    try:
        # Discord's Cloudflare front returns 403 to the default urllib
        # User-Agent, so present a browser-like one (matches the send path).
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        })
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        channel = data.get("name") or data.get("channel_id") or "?"
        return True, f"Webhook OK — connected to channel: {channel}"
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            return False, "Invalid webhook (401 Unauthorized) — check the URL."
        if exc.code == 404:
            return False, "Webhook not found (404) — it may have been deleted."
        return False, f"HTTP {exc.code} {exc.reason}"
    except Exception as exc:
        return False, f"Could not reach webhook: {exc}"


def _resolve_python():
    """Interpreter used to run the tools — the toolbox venv's python."""
    venv_py = os.path.expandvars(CFG.get("seedvr2", {}).get("venv_python", ""))
    if venv_py:
        p = venv_py if os.path.isabs(venv_py) else os.path.join(APP_ROOT, venv_py)
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
        model = o.get("model", "qwen2.5vl:7b")
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

SETTINGS_PATH = os.path.join(APP_ROOT, "gui_settings.json")


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
    SEP   = "   ·   "
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

    def __init__(self, master):
        super().__init__(master)
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
        """Render a telemetry sample dict (any field may be None)."""
        segs = []
        cpu = sample.get("cpu")
        if cpu is not None:
            c = round(cpu)
            segs.append((f"CPU {c}%", self._band(c)))
        else:
            segs.append(("CPU —", self.GREY))

        ru, rt = sample.get("ram_used_mb"), sample.get("ram_total_mb")
        if ru is not None and rt:
            text, pct = self._gb(ru, rt)
            segs.append((f"RAM {text}", self._band(pct)))

        vu, vt = sample.get("gpu_used_mb"), sample.get("gpu_total_mb")
        temp   = sample.get("gpu_temp_c")
        if vu is not None and vt:
            text, pct = self._gb(vu, vt)
            segs.append((f"VRAM {text}", self._band(pct)))
        if temp is not None:
            segs.append((f"GPU {temp}°C", self.GREY))
        if vu is None and temp is None:
            segs.append(("GPU: n/a", self.GREY))

        self._set(segs)


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
        self.pane.feed(console.text())
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


# ─────────────────────────────────────────────
#  COMPARISON WINDOW  (original vs. upscaled)
# ─────────────────────────────────────────────

class _ComparePane(ttk.Frame):
    """One side of the comparison: a header (filename · dimensions) over a canvas
    that fits the image to the available space, re-fitting on resize. Decoding is
    lazy (Pillow) and resizes are debounced so dragging stays smooth even at 4K."""

    def __init__(self, master, title):
        super().__init__(master)
        self._title  = title
        self.header  = ttk.Label(self, anchor="center", foreground="#aab2bf")
        self.header.pack(fill="x", padx=4, pady=(4, 2))
        self.canvas  = tk.Canvas(self, highlightthickness=0, bg="#15181d")
        self.canvas.pack(fill="both", expand=True)
        self._master_img = None       # PIL image at native resolution
        self._photo      = None       # current PhotoImage (kept referenced)
        self._resize_after = None
        self.canvas.bind("<Configure>", self._on_resize)

    def set_image(self, path):
        from PIL import Image, ImageOps
        try:
            img = Image.open(path)
            img = ImageOps.exif_transpose(img).convert("RGB")
            self._master_img = img
            self.header.configure(
                text=f"{self._title}:  {os.path.basename(path)}  ·  {img.width}×{img.height}")
        except Exception:
            self._master_img = None
            self.header.configure(
                text=f"{self._title}:  {os.path.basename(path)}  ·  (cannot open)")
        self._render()

    def _on_resize(self, _event):
        if self._resize_after is not None:
            self.after_cancel(self._resize_after)
        self._resize_after = self.after(120, self._render)

    def _render(self):
        from PIL import Image, ImageTk
        self._resize_after = None
        self.canvas.delete("all")
        m = self._master_img
        if m is None:
            return
        cw = max(self.canvas.winfo_width(), 1)
        ch = max(self.canvas.winfo_height(), 1)
        scale = min(cw / m.width, ch / m.height)        # fit, preserve aspect
        w = max(1, int(round(m.width * scale)))
        h = max(1, int(round(m.height * scale)))
        img = m if (w == m.width and h == m.height) else m.resize((w, h), Image.LANCZOS)
        self._photo = ImageTk.PhotoImage(img)
        self.canvas.create_image(cw // 2, ch // 2, image=self._photo)


class ComparisonWindow(tk.Toplevel):
    """
    Floating, resizable original-vs-upscaled comparison (Future Feature #1).

    The source image and its upscaled counterpart sit side by side in a draggable
    split pane, each fit to its half so the quality gain is directly visible (the
    lower-resolution original, scaled up to the same on-screen size, reads softer
    than the upscaled result). Like the log window there is a single shared
    instance, re-targeted whenever another comparable thumbnail is double-clicked.
    """

    def __init__(self, master, source, output, app=None):
        super().__init__(master)
        self._app = app
        geo = app.settings.get("compare_geometry") if app is not None else None
        self.geometry(geo if (geo and _geometry_on_screen(self, geo)) else "1100x640")
        self.minsize(560, 360)

        panes = ttk.Panedwindow(self, orient="horizontal")
        panes.pack(fill="both", expand=True)
        self._left  = _ComparePane(panes, "Original")
        self._right = _ComparePane(panes, "Upscaled")
        panes.add(self._left,  weight=1)
        panes.add(self._right, weight=1)

        self.protocol("WM_DELETE_WINDOW", self._close)
        self.show(source, output)

    def show(self, source, output):
        """Point the window at a new pair (used for the shared single instance)."""
        self.title(f"{APP_TITLE} — Compare — {os.path.basename(source)}")
        self._left.set_image(source)
        self._right.set_image(output)
        self.lift()
        self.focus_set()

    def save_geometry(self):
        if self._app is not None and self.winfo_exists():
            self._app.settings["compare_geometry"] = self.geometry()
            save_settings(self._app.settings)

    def _close(self):
        self.save_geometry()
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
        self.tool_name      = "program"         # overridden by each tool tab
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

    # ── UI helpers ──────────────────────────────────────────────────────────

    def _build_output_area(self, row):
        """Progress bar + two-row status + full-width image preview wall."""
        pf = ttk.Frame(self)
        pf.grid(row=row, column=0, columnspan=4, sticky="ew", pady=(10, 2))
        ttk.Label(pf, text="Est. Time Remaining:").pack(side="left")
        self.eta_var = tk.StringVar(value="—")
        ttk.Label(pf, textvariable=self.eta_var, width=20, anchor="e",
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

        # Compact system-telemetry row, just below the carousel (Feature #3a).
        self.telemetry_row = TelemetryRow(self)
        self.telemetry_row.grid(row=row + 3, column=0, columnspan=4,
                                sticky="ew", pady=(4, 0))

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

    # ── Process control ─────────────────────────────────────────────────────

    @property
    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def launch(self, script, args):
        # The child scripts are this module's siblings in scripts/; run them with
        # the working directory at the app root (where config/state/engine live).
        cmd = [PYTHON_EXE, "-u", os.path.join(SCRIPT_DIR, script)] + args
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"
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
        elif kind == "DONE":
            self._last_done = payload   # JSON summary; published to MQTT on exit

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
        self.app.mqtt_publish({mqtt_publisher.TASK_PROGRESS_TOPIC: f"{cur}/{tot}"})

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
        mqtt_values = {
            mqtt_publisher.TASK_RUNTIME_TOPIC:  str(int(elapsed)),
            mqtt_publisher.TASK_PROGRESS_TOPIC: f"{idx}/{total}",
        }
        if processed > 0 and total > 0:
            avg       = elapsed / processed
            remaining = max(0, total - idx)
            self.eta_var.set(_fmt_eta(avg * remaining))
            mqtt_values[mqtt_publisher.TASK_ETA_TOPIC] = _fmt_eta(avg * remaining)
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

    def on_exit(self, code):
        """Subclasses override for their own UI, then call super().on_exit(code)
        so the shared MQTT 'task finished' state is published once."""
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
    currently being processed is highlighted (blue frame) and any thumbnail can
    be double-clicked to open it. The +/- buttons (zoom_in / zoom_out) change the
    cell size.

    Per-image outcomes arrive via RESULT events (set_status): a thin green frame
    marks a success / an image with an upscaled counterpart that exists (so it
    can be compared), a thin red frame marks a failure, and no frame means
    not-yet-processed. Outcomes are run-level (kept across batch swaps) and
    follow a file through a rename; the blue current-frame always draws on top.

    Thumbnails are decoded in a background thread, one batch of BATCH_SIZE
    around the current image at a time (when the current image crosses into
    the next batch, that batch is loaded), so very large queues stay snappy.
    """

    BG          = "#202329"
    HILITE      = "#4f9cff"
    OUTLINE     = "#3a3f48"
    # Per-image outcome frames (scaffolding for the comparison feature): green =
    # processed successfully / an upscaled counterpart exists (comparable); red =
    # processing failed. No frame = not yet processed.
    OK_FRAME    = "#3fb950"
    FAIL_FRAME  = "#f85149"

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
        self._status  = {}        # path -> "ok"/"fail" (run-level, all batches)
        self._compare = {}        # source path -> upscaled output path (this run)
        self._frame_id = {}       # path -> status-frame rect id (current batch)
        # Optional callback(source_path, output_path): set by the Upscaler tab to
        # open the comparison window when a green (comparable) thumbnail is
        # double-clicked. When unset, double-click just opens the file.
        self.on_compare = None
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
        self._status = {}               # outcomes belong to one run only
        self._compare = {}
        self._batch = None              # force a rebuild on the next IMG event

    def clear(self):
        self._gen += 1
        self._paths, self._index, self._renamed = [], {}, {}
        self._status = {}
        self._compare = {}
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
        for d in (self._master, self._photo, self._img_id, self._status,
                  self._frame_id, self._compare):
            if old in d:
                d[new] = d.pop(old)

    def refresh(self, path):
        """Re-decode one thumbnail whose file changed on disk (e.g. after an
        auto-straighten rotation), so the strip shows the corrected pixels.

        Reuses the background loader: the decode runs off-thread and the
        result is placed by the regular drain() tick. Dropping the cached
        photo first guards against a stale image lingering if the file is
        outside the currently displayed batch."""
        p = (path or "").strip()
        if not p:
            return
        self._photo.pop(p, None)
        threading.Thread(target=self._load_batch,
                         args=([p], self._gen), daemon=True).start()

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

    def set_status(self, path, status, compare_to=None):
        """Record a per-image outcome ('ok'/'fail') and, if that image is in the
        batch on screen, frame it (green/red). Run-level: kept across batches, so
        scrolling back to an earlier batch still shows its outcomes. `compare_to`
        (the upscaled output path) enables double-click-to-compare for that
        thumbnail."""
        path = (path or "").strip()
        if not path or status not in ("ok", "fail"):
            return
        self._status[path] = status
        if compare_to:
            self._compare[path] = compare_to
        if path in self._pos:
            self._draw_frame(path)
            self._draw_highlight()      # keep the blue current-frame on top

    # ── Layout ───────────────────────────────────────────────────────────────

    def _build_cells(self, order, first=None):
        self._order   = list(order)
        self._pos     = {p: i for i, p in enumerate(order)}
        self._master  = {}
        self._photo   = {}
        self._img_id  = {}
        self._frame_id = {}
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
        self._frame_id = {}
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
        self._draw_frames()
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

    def _draw_frames(self):
        """(Re)draw every status frame for the batch on screen."""
        for p in list(self._frame_id):
            self.canvas.delete(self._frame_id.pop(p))
        for p in self._order:
            if p in self._status:
                self._draw_frame(p)

    def _draw_frame(self, path):
        """Draw (or redraw) one image's green/red outcome frame."""
        old = self._frame_id.pop(path, None)
        if old is not None:
            self.canvas.delete(old)
        i = self._pos.get(path)
        status = self._status.get(path)
        if i is None or status is None:
            return
        x0, y0 = self._cell_origin(i)
        cell  = self._cell
        color = self.OK_FRAME if status == "ok" else self.FAIL_FRAME
        self._frame_id[path] = self.canvas.create_rectangle(
            x0, y0, x0 + cell, y0 + cell, outline=color, width=2)

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
        if not (0 <= idx < len(self._order)):
            return
        path = self._order[idx]
        # A comparable (green) thumbnail with a known upscaled output opens the
        # comparison window; anything else just opens the file.
        out = self._compare.get(path)
        if self.on_compare and out and os.path.exists(out) and os.path.exists(path):
            self.on_compare(path, out)
        else:
            self._open(path)

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

    def _resolve_renamed(self, p):
        """Follow any rename(s) recorded for `p` to its latest on-disk path.
        The undo pass renames each file back to its original right after we
        start decoding, so the path captured at batch start may be stale."""
        seen = 0
        while p in self._renamed and seen < 8:
            p = self._renamed[p]
            seen += 1
        return p

    def _load_batch(self, paths, gen):
        from PIL import Image, ImageOps
        for p in paths:
            if gen != self._gen:
                return                  # batch changed — abandon
            img = None
            # Decode the current on-disk path. A rename can land mid-decode
            # (file vanishes from under us), so re-resolve and retry a few
            # times before giving up — otherwise that thumbnail stays blank.
            for attempt in range(4):
                src = self._resolve_renamed(p)
                try:
                    with Image.open(src) as f:
                        f.draft("RGB", (THUMB_MASTER, THUMB_MASTER))
                        f = ImageOps.exif_transpose(f)
                        f.thumbnail((THUMB_MASTER, THUMB_MASTER), Image.LANCZOS)
                        img = f.convert("RGB")
                    break
                except Exception:
                    img = None
                    if gen != self._gen:
                        return
                    time.sleep(0.05)    # let a pending rename event land
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
            # A freshly placed thumbnail is created above any existing frame, so
            # redraw the frames (and the highlight) to keep them on top.
            self._draw_frames()
            self._draw_highlight()


class UpscaleTab(ToolTab):

    def __init__(self, notebook, app):
        super().__init__(notebook, app)
        self.tool_name      = "Batch Upscaler"
        self.mqtt_task_name = "upscaling"
        self.src_var      = tk.StringVar()
        self.out_var      = tk.StringVar()
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
        src_default = get_default_folder("upscale_source")
        if src_default and os.path.isdir(src_default):
            self.src_var.set(src_default)
            if not self.out_var.get().strip():
                self.out_var.set(os.path.join(src_default, "__upscaled__"))
        out_default = get_default_folder("upscale_output")
        if out_default:
            self.out_var.set(out_default)
        self.src_var.trace_add("write", lambda *_: self._refresh_save_buttons())
        self.out_var.trace_add("write", lambda *_: self._refresh_save_buttons())
        self._refresh_save_buttons()

    def _build(self):
        ttk.Label(self, text="Photo folder:").grid(row=0, column=0, sticky="w", pady=3)
        ttk.Entry(self, textvariable=self.src_var).grid(row=0, column=1, sticky="ew", padx=6, pady=3)
        ttk.Button(self, text="Browse…", command=self._pick_src).grid(row=0, column=2, pady=3)
        self.save_src_btn = ttk.Button(
            self, text="Save as Default", command=lambda: self._save_default("src"))
        self.save_src_btn.grid(row=0, column=3, sticky="ew", padx=(8, 0), pady=3)

        ttk.Label(self, text="Save upscaled to:").grid(row=1, column=0, sticky="w", pady=3)
        ttk.Entry(self, textvariable=self.out_var).grid(row=1, column=1, sticky="ew", padx=6, pady=3)
        ttk.Button(self, text="Browse…", command=self._pick_out).grid(row=1, column=2, pady=3)
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

        self._build_output_area(row=3)

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
        if not self.confirm_gpu_overlap():
            return

        self.progress.set(0)
        self._reset_stream_state()
        self.status_var.set("Starting — loading the AI engine (the first run can take a few minutes) …")
        # The skip-cutoff now lives in Settings; batch_upscale reads it from config.json.
        if self.launch("batch_upscale.py", [src, out]):
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
        has_src = bool(self.src_var.get().strip())
        self.start_btn.configure(
            state="normal" if (not running and has_src) else "disabled")
        self.pause_btn.configure(state="normal"   if running else "disabled")
        self.stop_btn.configure(state="normal"    if running else "disabled")
        # VRAM is fully committed during an upscale run — lock out the other tool
        self.app.set_tag_tab_enabled(not running)
        self.app.refresh_conciliate_lock()

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
        super().on_exit(code)


# ─────────────────────────────────────────────
#  TAB 2 — TAG & RENAME
# ─────────────────────────────────────────────

LANGUAGES = [
    "English", "Romanian", "French", "German", "Spanish", "Italian",
    "Portuguese", "Dutch", "Polish", "Hungarian", "Czech", "Greek",
    "Russian", "Ukrainian", "Turkish", "Swedish", "Norwegian", "Danish",
    "Finnish",
]

# Undo scopes for Tag & Rename, as (menu label, backend code) pairs.
# The first entry is the default selection.
UNDO_SCOPES = [
    ("Undo everything",   "all"),
    ("File names only",   "names"),
    ("Descriptions only", "exif"),
]


class TagTab(ToolTab):

    def __init__(self, notebook, app):
        super().__init__(notebook, app)
        self.tool_name      = "Tag & Rename"
        self.mqtt_task_name = "tagging"
        self.telemetry_interval_ms = 30000      # sample every 30 s while running
        self.dir_var    = tk.StringVar()
        self.lang_var   = tk.StringVar(value="English")
        self.ftag_var   = tk.BooleanVar(value=False)
        self.fren_var   = tk.BooleanVar(value=False)
        self.scope_var  = tk.StringVar(value=UNDO_SCOPES[0][0])
        self._mode      = "tag"          # "tag" | "undo" — for the exit message
        self._build()

        # Restore the pinned default folder from config.json
        tag_default = get_default_folder("tag_folder")
        if tag_default and os.path.isdir(tag_default):
            self.dir_var.set(tag_default)
        self.dir_var.trace_add("write", lambda *_: self._refresh_dir_buttons())
        self._refresh_dir_buttons()

    def _build(self):
        ttk.Label(self, text="Photo folder:").grid(row=0, column=0, sticky="w", pady=3)
        ttk.Entry(self, textvariable=self.dir_var).grid(row=0, column=1, sticky="ew", padx=6, pady=3)
        ttk.Button(self, text="Browse…", command=self._pick_dir).grid(row=0, column=2, pady=3)
        self.save_dir_btn = ttk.Button(self, text="Save as Default", command=self._save_default)
        self.save_dir_btn.grid(row=0, column=3, sticky="ew", padx=(8, 0), pady=3)

        opts = ttk.Frame(self)
        opts.grid(row=1, column=0, columnspan=3, sticky="w", pady=(6, 0))
        ttk.Label(opts, text="Description language:").pack(side="left")
        ttk.Combobox(opts, textvariable=self.lang_var, values=LANGUAGES,
                     width=14).pack(side="left", padx=(4, 16))
        ttk.Checkbutton(opts, text="Force Tag all images",
                        variable=self.ftag_var).pack(side="left", padx=(0, 12))
        ttk.Checkbutton(opts, text="Force Rename all images",
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
        ttk.Label(undo, text="Scope:").pack(side="left", padx=(0, 4))
        ttk.Combobox(undo, textvariable=self.scope_var, state="readonly", width=18,
                     values=[label for label, _ in UNDO_SCOPES]).pack(side="left", padx=(0, 16))
        self.undo_btn = ttk.Button(undo, text="Undo this folder…", command=self._undo)
        self.undo_btn.pack(side="left")

        self._build_output_area(row=4)

    # ── Actions ──────────────────────────────────────────────────────────────

    def _pick_dir(self):
        folder = filedialog.askdirectory(title="Choose the folder with photos to tag")
        if folder:
            self.dir_var.set(os.path.normpath(folder))

    # ── Default-folder button ────────────────────────────────────────────────

    def _dir_valid(self):
        p = self.dir_var.get().strip()
        return bool(p) and os.path.isdir(p)

    def _refresh_dir_buttons(self):
        has_dir = bool(self.dir_var.get().strip())
        self.save_dir_btn.configure(state="normal" if self._dir_valid() else "disabled")
        # "Open photo folder" and "Undo this folder…" are meaningless with no
        # folder entered. Undo additionally stays locked while a run is active.
        self.open_btn.configure(state="normal" if has_dir else "disabled")
        self.undo_btn.configure(
            state="normal" if (has_dir and not self.running) else "disabled")
        # No photo folder, nothing to tag — but never re-enable mid-run.
        if not self.running:
            self.start_btn.configure(state="normal" if has_dir else "disabled")

    def _save_default(self):
        if not self._dir_valid():
            return
        set_default_folder("tag_folder", self.dir_var.get().strip())
        self.app.sync_settings_defaults()   # mirror into the Settings tab
        self.save_dir_btn.configure(text="Saved ✓")
        self.after(1200, lambda: self.save_dir_btn.configure(text="Save as Default"))

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
        scope = dict(UNDO_SCOPES).get(self.scope_var.get(), "all")
        scope_text = {
            "all":   "file names, embedded descriptions, and auto-straighten rotations",
            "names": "file names only",
            "exif":  "embedded descriptions and auto-straighten rotations",
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
        self.resume_btn.configure(state="normal"   if running else "disabled")
        self.stop_btn.configure(state="normal"     if running else "disabled")
        # Start and Undo are locked while running; otherwise they follow the
        # folder state (no folder entered → nothing to act on).
        has_dir = bool(self.dir_var.get().strip())
        self.start_btn.configure(
            state="normal" if (not running and has_dir) else "disabled")
        self.undo_btn.configure(
            state="normal" if (not running and has_dir) else "disabled")
        self.app.refresh_conciliate_lock()

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
        super().on_exit(code)


# ─────────────────────────────────────────────
#  SETTINGS TAB
# ─────────────────────────────────────────────

# Resolution Target presets → (max_resolution, resolution)
RESOLUTION_PRESETS = [
    ("3840 / 2160", 3840, 2160),
    ("2560 / 1440", 2560, 1440),
    ("1920 / 1080", 1920, 1080),
]

# Keys in the "upscale" block that get their own dedicated controls and so are
# NOT rendered in the generic SeedVR Settings box.
_SEEDVR_EXCLUDE = {"resolution", "max_resolution", "discord_webhook_url",
                   "upscale_cutoff_pct", "output_subdir", "debug",
                   "auto_straighten", "straighten_min_confidence"}

# Friendly labels for the generic SeedVR fields.
_SEEDVR_LABELS = {
    "attention_mode":   "Attention mode",
    "color_correction": "Color correction",
    "dit_model":        "DiT model",
    "vae_model":        "VAE model",
    "blocks_to_swap":   "Blocks to swap",
    "encode_tiled":     "VAE encode tiled",
    "decode_tiled":     "VAE decode tiled",
    "encode_tile_size": "Encode tile size",
    "decode_tile_size": "Decode tile size",
    "outage_threshold": "Outage threshold",
}

# Suggested values for the free-text enum fields (editable — type anything).
_SEEDVR_CHOICES = {
    "attention_mode":   ["sdpa", "flash_attn", "sage"],
    "color_correction": ["lab", "wavelet", "none"],
}


class _ScrollFrame(ttk.Frame):
    """A vertically scrollable container. Add child widgets to `.body`."""

    def __init__(self, master):
        super().__init__(master)
        self.canvas = tk.Canvas(self, highlightthickness=0)
        vsb = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.body = ttk.Frame(self.canvas)
        self._win = self.canvas.create_window((0, 0), window=self.body, anchor="nw")
        self.body.bind("<Configure>",
                       lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>",
                         lambda e: self.canvas.itemconfigure(self._win, width=e.width))
        # Mouse wheel scrolls only while the pointer is over this canvas.
        self.canvas.bind("<Enter>", lambda e: self.canvas.bind_all("<MouseWheel>", self._wheel))
        self.canvas.bind("<Leave>", lambda e: self.canvas.unbind_all("<MouseWheel>"))

    def _wheel(self, event):
        self.canvas.yview_scroll(int(-event.delta / 120), "units")


class SettingsTab(ttk.Frame):
    """Edit the settings that previously lived only in config.json."""

    def __init__(self, notebook, app):
        super().__init__(notebook)
        self.app = app
        self._seedvr_vars = {}     # key -> (tk var, python type)
        self._build()

    # ── construction ─────────────────────────────────────────────────────────

    def _build(self):
        sf = _ScrollFrame(self)
        sf.pack(fill="both", expand=True)
        body = sf.body

        ollama = CFG.get("ollama", {})
        ups    = CFG.get("upscale", {})
        defs   = CFG.get("defaults", {})
        tag    = CFG.get("tagging", {})

        # ── Default folders (mirrors the tabs' "Save as Default" buttons) ───────
        sec = self._section(body, "Default folders")
        sec.columnconfigure(1, weight=1)
        self.default_src_var = tk.StringVar(value=defs.get("upscale_source", ""))
        self.default_out_var = tk.StringVar(value=defs.get("upscale_output", ""))
        self.default_tag_var = tk.StringVar(value=defs.get("tag_folder", ""))
        self.default_corig_var = tk.StringVar(value=defs.get("conciliate_original", ""))
        self.default_cproc_var = tk.StringVar(value=defs.get("conciliate_processed", ""))
        for r, (text, var) in enumerate((
                ("Batch Upscaler — Photo folder:",  self.default_src_var),
                ("Batch Upscaler — Output folder:", self.default_out_var),
                ("Tag & Rename — Photo folder:",    self.default_tag_var),
                ("Conciliation — Original folder:",  self.default_corig_var),
                ("Conciliation — Processed folder:", self.default_cproc_var))):
            ttk.Label(sec, text=text).grid(row=r, column=0, sticky="w", pady=3)
            ttk.Entry(sec, textvariable=var).grid(row=r, column=1, sticky="ew", padx=6, pady=3)
            ttk.Button(sec, text="Browse…",
                       command=lambda v=var: self._pick_folder(v)).grid(row=r, column=2, pady=3)

        # ── Ollama ────────────────────────────────────────────────────────────
        sec = self._section(body, "Ollama")
        sec.columnconfigure(1, weight=1)

        ttk.Label(sec, text="Ollama URL:").grid(row=0, column=0, sticky="w", pady=3)
        self.ollama_url_var = tk.StringVar(value=ollama.get("url", "http://127.0.0.1:11434"))
        ttk.Entry(sec, textvariable=self.ollama_url_var).grid(row=0, column=1, sticky="ew", padx=6, pady=3)
        ttk.Button(sec, text="Check", command=self._check_ollama).grid(row=0, column=2, pady=3)

        ttk.Label(sec, text="Ollama model:").grid(row=1, column=0, sticky="w", pady=3)
        self.ollama_model_var = tk.StringVar(value=ollama.get("model", "qwen2.5vl:7b"))
        self.ollama_model_cmb = ttk.Combobox(sec, textvariable=self.ollama_model_var)
        self.ollama_model_cmb.grid(row=1, column=1, sticky="ew", padx=6, pady=3)
        ttk.Button(sec, text="Refresh", command=self._refresh_models).grid(row=1, column=2, pady=3)

        self.ollama_status = ttk.Label(sec, text="", foreground="#666")
        self.ollama_status.grid(row=2, column=0, columnspan=3, sticky="w", padx=6, pady=(4, 0))

        # ── Tag & Rename ───────────────────────────────────────────────────────
        sec = self._section(body, "Tag & Rename")
        sec.columnconfigure(1, weight=1)

        self.straighten_var = tk.BooleanVar(value=bool(tag.get("auto_straighten", True)))
        chk = ttk.Checkbutton(sec, text="Auto-straighten rotated photos",
                              variable=self.straighten_var)
        chk.grid(row=0, column=0, sticky="w", pady=3)
        Tooltip(chk, "Detects sideways photos and rotates them upright before tagging. "
                     "Only confident calls are acted on; ambiguous ones are left alone.")

        conf = ttk.Frame(sec)
        conf.grid(row=0, column=1, sticky="w", padx=18, pady=3)
        ttk.Label(conf, text="Confidence threshold:").pack(side="left", padx=(0, 4))
        self.straighten_conf_var = tk.DoubleVar(
            value=float(tag.get("straighten_min_confidence", 0.9)))
        spin = ttk.Spinbox(conf, from_=0.50, to=1.00, increment=0.05, width=6, format="%.2f",
                           textvariable=self.straighten_conf_var)
        spin.pack(side="left")
        Tooltip(spin, "0.50–1.00   (higher = fewer, safer rotations)")

        # ── Upscaling targets ──────────────────────────────────────────────────
        sec = self._section(body, "Upscaling")
        sec.columnconfigure(1, weight=1)

        strip = ttk.Frame(sec)
        strip.grid(row=0, column=0, columnspan=2, sticky="w", pady=3)

        ttk.Label(strip, text="Resolution Target:").pack(side="left", padx=(0, 4))
        self.restarget_var = tk.StringVar(value=self._current_preset_label(ups))
        restarget_cmb = ttk.Combobox(strip, textvariable=self.restarget_var, state="readonly",
                                     values=[p[0] for p in RESOLUTION_PRESETS], width=16)
        restarget_cmb.pack(side="left")
        Tooltip(restarget_cmb, "longer edge / shorter edge, in pixels")

        ttk.Label(strip, text="Skip images over:").pack(side="left", padx=(24, 4))
        self.cutoff_var = tk.IntVar(value=int(ups.get("upscale_cutoff_pct", 66)))
        cut_spin = ttk.Spinbox(strip, from_=0, to=99, width=4, textvariable=self.cutoff_var)
        cut_spin.pack(side="left")
        ttk.Label(strip, text="% of target resolution").pack(side="left", padx=(4, 0))
        Tooltip(cut_spin, "Percentage of the target resolution.   (0 = upscale everything eligible)")

        strip2 = ttk.Frame(sec)
        strip2.grid(row=1, column=0, columnspan=2, sticky="w", pady=3)
        self.up_straighten_var = tk.BooleanVar(value=bool(ups.get("auto_straighten", True)))
        up_chk = ttk.Checkbutton(strip2, text="Auto-straighten rotated photos before upscaling",
                                 variable=self.up_straighten_var)
        up_chk.pack(side="left")
        Tooltip(up_chk, "Rotates a sideways photo upright BEFORE upscaling so the result still "
                        "fits a 4K screen. Without this, the upscaler targets the wrong axis and "
                        "the image no longer fits once Tag & Rename straightens it. The source is "
                        "never modified (a temp copy is rotated and upscaled).")
        ttk.Label(strip2, text="Confidence threshold:").pack(side="left", padx=(18, 4))
        self.up_straighten_conf_var = tk.DoubleVar(
            value=float(ups.get("straighten_min_confidence", 0.9)))
        up_spin = ttk.Spinbox(strip2, from_=0.50, to=1.00, increment=0.05, width=6, format="%.2f",
                              textvariable=self.up_straighten_conf_var)
        up_spin.pack(side="left")
        Tooltip(up_spin, "0.50–1.00   (higher = fewer, safer rotations)")

        # ── SeedVR Settings (everything else in the upscale block) ──────────────
        sec = self._section(body, "SeedVR Settings")
        present = {k: v for k, v in ups.items() if k not in _SEEDVR_EXCLUDE}

        def _lbl(key):
            return _SEEDVR_LABELS.get(key, key.replace("_", " ").capitalize())

        # Controls grouped onto shared rows for a compact layout.
        seedvr_rows = [
            ["attention_mode", "color_correction"],
            ["dit_model", "vae_model"],
            ["blocks_to_swap", "outage_threshold"],
            ["encode_tiled", "decode_tiled", "encode_tile_size", "decode_tile_size"],
        ]

        placed = set()
        row = 0
        for group in seedvr_rows:
            keys = [k for k in group if k in present]
            if not keys:
                continue
            # Rows with more than a simple pair are packed tightly to the left in
            # their own frame, so they don't inherit the wide column alignment of
            # the two-control rows above.
            if len(keys) > 2:
                strip = ttk.Frame(sec)
                strip.grid(row=row, column=0, columnspan=4, sticky="w", pady=3)
                for i, key in enumerate(keys):
                    ttk.Label(strip, text=f"{_lbl(key)}:").pack(
                        side="left", padx=(0 if i == 0 else 24, 4))
                    self._make_seedvr_control(strip, key, present[key]).pack(side="left")
                    placed.add(key)
            else:
                col = 0
                for key in keys:
                    ttk.Label(sec, text=f"{_lbl(key)}:").grid(
                        row=row, column=col, sticky="w", pady=3, padx=(0 if col == 0 else 14, 4))
                    self._make_seedvr_control(sec, key, present[key]).grid(
                        row=row, column=col + 1, sticky="w", pady=3)
                    placed.add(key)
                    col += 2
            row += 1

        # Any unrecognised keys fall back to one-per-row generic controls.
        for key, value in present.items():
            if key in placed:
                continue
            ttk.Label(sec, text=f"{_lbl(key)}:").grid(row=row, column=0, sticky="w", pady=3, padx=(0, 4))
            self._make_seedvr_control(sec, key, value).grid(row=row, column=1, sticky="w", pady=3)
            row += 1

        # ── Notifications ───────────────────────────────────────────────────────
        sec = self._section(body, "Notifications")
        sec.columnconfigure(1, weight=1)

        ttk.Label(sec, text="Discord Webhook:").grid(row=0, column=0, sticky="w", pady=3)
        self.webhook_var = tk.StringVar(value=ups.get("discord_webhook_url", ""))
        webhook_entry = ttk.Entry(sec, textvariable=self.webhook_var)
        webhook_entry.grid(row=0, column=1, sticky="ew", padx=6, pady=3)
        Tooltip(webhook_entry,
                "Optional. Notifies when a queue finishes or on errors. Leave empty to disable.")
        ttk.Button(sec, text="Test", command=self._test_webhook).grid(row=0, column=2, pady=3)
        self.webhook_status = ttk.Label(sec, text="", foreground="#666")
        self.webhook_status.grid(row=1, column=1, columnspan=2, sticky="w", padx=6)

        # ── Updates ─────────────────────────────────────────────────────────────
        sec = self._section(body, "Updates")
        sec.columnconfigure(1, weight=1)

        ttk.Label(sec, text=f"Current version: {APP_VERSION}").grid(
            row=0, column=0, sticky="w", pady=3)
        self.check_update_btn = ttk.Button(
            sec, text="Check for updates now", command=self._check_updates)
        self.check_update_btn.grid(row=0, column=2, pady=3)

        self.auto_update_var = tk.BooleanVar(value=update_auto_check_enabled())
        ttk.Checkbutton(sec, text="Check for updates on startup",
                        variable=self.auto_update_var).grid(
            row=1, column=0, sticky="w", pady=3)

        self.update_status = ttk.Label(sec, text="", foreground="#666")
        self.update_status.grid(row=2, column=0, columnspan=3, sticky="w", padx=6, pady=(4, 0))

        # ── Home Assistant (MQTT) ───────────────────────────────────────────────
        mqtt = CFG.get("mqtt", {})
        sec = self._section(body, "Home Assistant (MQTT)")
        sec.columnconfigure(1, weight=1)

        ttk.Label(sec, wraplength=560, foreground="#666",
                  text=("Publishes status to an MQTT broker (e.g. Home Assistant's "
                        "Mosquitto). Enabled automatically whenever a broker host is "
                        "set below — clear the host to disable. The app verifies the "
                        "connection on startup.")
                  ).grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 4))

        self.mqtt_host_var = tk.StringVar(value=mqtt.get("host", ""))
        self.mqtt_port_var = tk.StringVar(value=str(mqtt.get("port", 1883)))
        self.mqtt_user_var = tk.StringVar(value=mqtt.get("username", ""))
        self.mqtt_pass_var = tk.StringVar(value=mqtt.get("password", ""))
        self.mqtt_cid_var  = tk.StringVar(
            value=mqtt.get("client_id", mqtt_publisher.DEFAULT_CLIENT_ID))

        ttk.Label(sec, text="Broker host:").grid(row=1, column=0, sticky="w", pady=3)
        ttk.Entry(sec, textvariable=self.mqtt_host_var).grid(row=1, column=1, sticky="ew", padx=6, pady=3)
        ttk.Label(sec, text="Port:").grid(row=1, column=2, sticky="e", pady=3)
        ttk.Spinbox(sec, from_=1, to=65535, width=7,
                    textvariable=self.mqtt_port_var).grid(row=1, column=3, sticky="w", padx=6, pady=3)

        ttk.Label(sec, text="Username:").grid(row=2, column=0, sticky="w", pady=3)
        ttk.Entry(sec, textvariable=self.mqtt_user_var).grid(row=2, column=1, sticky="ew", padx=6, pady=3)
        ttk.Label(sec, text="Password:").grid(row=3, column=0, sticky="w", pady=3)
        ttk.Entry(sec, textvariable=self.mqtt_pass_var, show="•").grid(
            row=3, column=1, sticky="ew", padx=6, pady=3)
        self.mqtt_test_btn = ttk.Button(sec, text="Test", command=self._test_mqtt)
        self.mqtt_test_btn.grid(row=3, column=2, columnspan=2, sticky="e", padx=6, pady=3)

        ttk.Label(sec, text="Client ID:").grid(row=4, column=0, sticky="w", pady=3)
        ttk.Entry(sec, textvariable=self.mqtt_cid_var).grid(row=4, column=1, sticky="ew", padx=6, pady=3)

        ttk.Button(sec, text="Publish now", command=self._publish_mqtt).grid(
            row=4, column=2, columnspan=2, sticky="e", padx=6, pady=3)
        self.mqtt_status = ttk.Label(sec, text="", foreground="#666")
        self.mqtt_status.grid(row=5, column=0, columnspan=4, sticky="w", padx=6, pady=(4, 0))

        # ── Save bar ────────────────────────────────────────────────────────────
        bar = ttk.Frame(body, padding=(8, 12))
        bar.pack(fill="x")
        ttk.Button(bar, text="Save settings", command=self._save).pack(side="left")
        self.save_status = ttk.Label(bar, text="", foreground="#666")
        self.save_status.pack(side="left", padx=12)

        # Baseline for unsaved-changes detection: a snapshot of the form as it
        # mirrors config.json right now. Re-taken on every successful save/revert.
        self._baseline = self._snapshot()

        # Probe the saved Ollama URL in the background so the status text already
        # reflects reachability by the time the user opens the Settings tab.
        self._check_ollama()

    def _make_seedvr_control(self, parent, key, value):
        """Build the editable control for a SeedVR field and register its var.
        Returns the widget (caller positions it with .grid)."""
        if isinstance(value, bool):
            var = tk.BooleanVar(value=value)
            widget = ttk.Checkbutton(parent, variable=var)
            self._seedvr_vars[key] = (var, bool)
        elif isinstance(value, int):
            var = tk.StringVar(value=str(value))
            widget = ttk.Spinbox(parent, from_=0, to=100000, width=8, textvariable=var)
            self._seedvr_vars[key] = (var, int)
        else:
            var = tk.StringVar(value=str(value))
            if key in _SEEDVR_CHOICES:
                widget = ttk.Combobox(parent, textvariable=var,
                                      values=_SEEDVR_CHOICES[key], width=16)
            else:
                widget = ttk.Entry(parent, textvariable=var, width=22)
            self._seedvr_vars[key] = (var, str)
        return widget

    def _section(self, parent, title):
        lf = ttk.LabelFrame(parent, text=f"  {title}  ", padding=(10, 8))
        lf.pack(fill="x", padx=10, pady=(10, 0))
        return lf

    # ── helpers ──────────────────────────────────────────────────────────────

    def _current_preset_label(self, ups):
        mx, rs = int(ups.get("max_resolution", 3840)), int(ups.get("resolution", 2160))
        for label, pmx, prs in RESOLUTION_PRESETS:
            if pmx == mx and prs == rs:
                return label
        return RESOLUTION_PRESETS[0][0]

    def _check_ollama(self):
        """Probe the Ollama URL off the UI thread and report reachability."""
        url = self.ollama_url_var.get().strip()
        self.ollama_status.configure(text="Checking Ollama…", foreground="#666")

        def work():
            installed = ollama_installed()
            ok, value = ollama_list_models(url)
            self.after(0, lambda: self._apply_ollama_check(ok, value, installed))

        threading.Thread(target=work, daemon=True).start()

    def _apply_ollama_check(self, ok, value, installed):
        if ok:
            self.ollama_model_cmb.configure(values=value)
            inst = "installed" if installed else "not on PATH"
            self.ollama_status.configure(
                text=f"Reachable — {len(value)} model(s) available (ollama executable {inst}).",
                foreground="#1a7f37")
        else:
            inst = "Ollama executable found on PATH." if installed else \
                   "Ollama executable not found on PATH."
            self.ollama_status.configure(
                text=f"Not reachable at this URL — {value}. {inst}", foreground="#b3261e")

    def _refresh_models(self):
        url = self.ollama_url_var.get().strip()
        self.ollama_status.configure(text="Refreshing models…", foreground="#666")

        def work():
            ok, value = ollama_list_models(url)
            self.after(0, lambda: self._apply_refresh_models(ok, value))

        threading.Thread(target=work, daemon=True).start()

    def _apply_refresh_models(self, ok, value):
        if ok:
            self.ollama_model_cmb.configure(values=value)
            self.ollama_status.configure(
                text=f"Found {len(value)} model(s)." if value else "No models installed.",
                foreground="#1a7f37" if value else "#666")
        else:
            self.ollama_status.configure(text=f"Could not list models — {value}",
                                         foreground="#b3261e")

    def _test_webhook(self):
        ok, msg = test_discord_webhook(self.webhook_var.get())
        self.webhook_status.configure(text=msg, foreground="#1a7f37" if ok else "#b3261e")

    def _check_updates(self):
        """Manual update check from Settings (always reports the outcome, and
        shows the prompt for an available update even if it was skipped before)."""
        self.check_update_btn.configure(state="disabled")
        self.update_status.configure(text="Checking for updates…", foreground="#666")

        def work():
            status, payload = updater.check_for_update(APP_VERSION)
            self.after(0, lambda: self._apply_update_check(status, payload))

        threading.Thread(target=work, daemon=True).start()

    def _apply_update_check(self, status, payload):
        self.check_update_btn.configure(state="normal")
        if status == "update":
            self.update_status.configure(
                text=f"Version {payload.version} is available.", foreground="#1a7f37")
            self.app.show_update_dialog(payload)
        elif status == "current":
            self.update_status.configure(
                text=f"You're on the latest version ({payload}).", foreground="#1a7f37")
        else:
            self.update_status.configure(text=payload, foreground="#b3261e")

    def _mqtt_fields(self):
        """The MQTT settings currently in the form (so Test works pre-Save)."""
        try:
            port = int(self.mqtt_port_var.get())
        except (ValueError, tk.TclError):
            port = 1883
        return {
            "host":      self.mqtt_host_var.get().strip(),
            "port":      port,
            "username":  self.mqtt_user_var.get().strip(),
            "password":  self.mqtt_pass_var.get(),
            "client_id": self.mqtt_cid_var.get().strip() or mqtt_publisher.DEFAULT_CLIENT_ID,
        }

    def _test_mqtt(self):
        cfg = self._mqtt_fields()
        if not cfg["host"]:
            self.mqtt_status.configure(text="Enter the broker host first.", foreground="#b3261e")
            return
        self.mqtt_test_btn.configure(state="disabled")
        self.mqtt_status.configure(text="Testing connection…", foreground="#666")

        def work():
            ok, msg = mqtt_publisher.test_connection(cfg)
            def apply():
                self.mqtt_test_btn.configure(state="normal")
                self.mqtt_status.configure(text=msg, foreground="#1a7f37" if ok else "#b3261e")
            self.after(0, apply)

        threading.Thread(target=work, daemon=True).start()

    def _publish_mqtt(self):
        cfg = self._mqtt_fields()
        if not cfg["host"]:
            self.mqtt_status.configure(text="Enter the broker host first.", foreground="#b3261e")
            return
        self.mqtt_status.configure(text="Publishing…", foreground="#666")

        def work():
            status, payload = updater.check_for_update(APP_VERSION)
            update_available = (status == "update")
            ok, msg = mqtt_publisher.publish_state(cfg, {
                mqtt_publisher.VERSION_TOPIC:        APP_VERSION,
                mqtt_publisher.UPDATE_TOPIC:         "yes" if update_available else "no",
                mqtt_publisher.LATEST_VERSION_TOPIC: payload.version if update_available else APP_VERSION,
            })
            self.after(0, lambda: self.mqtt_status.configure(
                text=msg, foreground="#1a7f37" if ok else "#b3261e"))

        threading.Thread(target=work, daemon=True).start()

    def _pick_folder(self, var):
        folder = filedialog.askdirectory(title="Choose a folder")
        if folder:
            var.set(os.path.normpath(folder))

    def load_defaults(self):
        """Refresh the default-folder fields from config (called when a tab's
        Save-as-Default button updates them, so both views stay in sync)."""
        defs = CFG.get("defaults", {})
        self.default_src_var.set(defs.get("upscale_source", ""))
        self.default_out_var.set(defs.get("upscale_output", ""))
        self.default_tag_var.set(defs.get("tag_folder", ""))
        self.default_corig_var.set(defs.get("conciliate_original", ""))
        self.default_cproc_var.set(defs.get("conciliate_processed", ""))

    def _collect(self):
        """Build the config sections the form currently describes, without
        touching CFG. Returns (sections, errors): `sections` maps section name →
        proposed {key: value}; `errors` names any field that failed validation.
        Shared by _save (apply) and the unsaved-changes detection (compare)."""
        errors = []
        seedvr_out = {}
        for key, (var, typ) in self._seedvr_vars.items():
            raw = var.get()
            if typ is int:
                try:
                    seedvr_out[key] = int(str(raw).strip())
                except ValueError:
                    errors.append(_SEEDVR_LABELS.get(key, key))
            elif typ is bool:
                seedvr_out[key] = bool(var.get())
            else:
                seedvr_out[key] = str(raw).strip()

        ups = {}
        try:
            ups["upscale_cutoff_pct"] = max(0, min(99, int(self.cutoff_var.get())))
        except (ValueError, tk.TclError):
            errors.append("Skip images over")
        for label, pmx, prs in RESOLUTION_PRESETS:
            if label == self.restarget_var.get():
                ups["max_resolution"], ups["resolution"] = pmx, prs
                break
        ups["discord_webhook_url"] = self.webhook_var.get().strip()
        ups["auto_straighten"] = bool(self.up_straighten_var.get())
        try:
            up_conf = round(float(self.up_straighten_conf_var.get()), 2)
        except (ValueError, tk.TclError):
            up_conf = 0.9
        ups["straighten_min_confidence"] = min(1.0, max(0.5, up_conf))
        ups.update(seedvr_out)

        try:
            conf = round(float(self.straighten_conf_var.get()), 2)
        except (ValueError, tk.TclError):
            conf = 0.9

        sections = {
            "ollama": {
                "url":   self.ollama_url_var.get().strip() or "http://127.0.0.1:11434",
                "model": self.ollama_model_var.get().strip(),
            },
            "upscale": ups,
            "defaults": {
                "upscale_source": self.default_src_var.get().strip(),
                "upscale_output": self.default_out_var.get().strip(),
                "tag_folder":     self.default_tag_var.get().strip(),
                "conciliate_original":  self.default_corig_var.get().strip(),
                "conciliate_processed": self.default_cproc_var.get().strip(),
            },
            "tagging": {
                "auto_straighten": bool(self.straighten_var.get()),
                "straighten_min_confidence": min(1.0, max(0.5, conf)),
            },
            "updates": {"auto_check": bool(self.auto_update_var.get())},
            "mqtt": self._mqtt_fields(),
        }
        return sections, errors

    def _snapshot(self):
        """A stable, comparable key for the form's current contents — the basis
        for unsaved-changes detection. Invalid input counts as 'changed'."""
        sections, errors = self._collect()
        return json.dumps(sections, sort_keys=True), bool(errors)

    def is_dirty(self):
        """True if the form differs from the last-saved (baseline) state."""
        try:
            return self._snapshot() != self._baseline
        except Exception:
            return False

    def _save(self):
        """Persist the form to config.json. Returns True on success; on a
        validation error it warns and returns False (nothing is written)."""
        sections, errors = self._collect()
        if errors:
            messagebox.showwarning(
                APP_TITLE, "These fields need a whole number:\n  • " + "\n  • ".join(errors))
            return False

        for name, values in sections.items():
            target = CFG.setdefault(name, {})
            if name == "mqtt":
                target.pop("enabled", None)   # MQTT is now gated by host being set
            target.update(values)

        if save_config():
            self._baseline = self._snapshot()    # the form is now the saved state
            # Apply MQTT changes immediately (connect/disconnect/reconfigure).
            self.app.restart_mqtt()
            self.save_status.configure(text="Saved.", foreground="#1a7f37")
            return True
        self.save_status.configure(
            text="Could not write config.json (check file permissions).",
            foreground="#b3261e")
        return False

    def revert(self):
        """Discard unsaved edits: reset every field to the values in CFG."""
        ollama = CFG.get("ollama", {})
        ups    = CFG.get("upscale", {})
        defs   = CFG.get("defaults", {})
        tag    = CFG.get("tagging", {})
        mqtt   = CFG.get("mqtt", {})
        self.default_src_var.set(defs.get("upscale_source", ""))
        self.default_out_var.set(defs.get("upscale_output", ""))
        self.default_tag_var.set(defs.get("tag_folder", ""))
        self.default_corig_var.set(defs.get("conciliate_original", ""))
        self.default_cproc_var.set(defs.get("conciliate_processed", ""))
        self.ollama_url_var.set(ollama.get("url", "http://127.0.0.1:11434"))
        self.ollama_model_var.set(ollama.get("model", "qwen2.5vl:7b"))
        self.straighten_var.set(bool(tag.get("auto_straighten", True)))
        self.straighten_conf_var.set(float(tag.get("straighten_min_confidence", 0.9)))
        self.restarget_var.set(self._current_preset_label(ups))
        self.cutoff_var.set(int(ups.get("upscale_cutoff_pct", 66)))
        self.up_straighten_var.set(bool(ups.get("auto_straighten", True)))
        self.up_straighten_conf_var.set(float(ups.get("straighten_min_confidence", 0.9)))
        self.webhook_var.set(ups.get("discord_webhook_url", ""))
        self.auto_update_var.set(update_auto_check_enabled())
        self.mqtt_host_var.set(mqtt.get("host", ""))
        self.mqtt_port_var.set(str(mqtt.get("port", 1883)))
        self.mqtt_user_var.set(mqtt.get("username", ""))
        self.mqtt_pass_var.set(mqtt.get("password", ""))
        self.mqtt_cid_var.set(mqtt.get("client_id", mqtt_publisher.DEFAULT_CLIENT_ID))
        for key, (var, typ) in self._seedvr_vars.items():
            if key in ups:
                var.set(ups[key] if typ is bool else str(ups[key]))
        self._baseline = self._snapshot()


# ─────────────────────────────────────────────
#  TAB 3 — CONCILIATION
# ─────────────────────────────────────────────

# (menu label, backend mode). The first entry is the default selection.
CONCILIATE_MODES = [
    ("Archive originals", "archive"),
    ("Delete originals",  "delete"),
]


class ConciliateTab(ToolTab):
    """
    Replace original photos with their processed (upscaled, optionally tagged &
    renamed) counterparts. Two phases: Scan/Preview builds a per-folder plan and
    touches nothing; Run performs the chosen archive/delete operation. Drives
    conciliate.py as a subprocess, reusing ToolTab's stdin/marker plumbing.
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
        orig_default = get_default_folder("conciliate_original")
        if orig_default:
            self.orig_var.set(orig_default)
        proc_default = get_default_folder("conciliate_processed")
        if proc_default:
            self.proc_var.set(proc_default)
        self.orig_var.trace_add("write", lambda *_: self._refresh_buttons())
        self.proc_var.trace_add("write", lambda *_: self._refresh_buttons())
        self._refresh_buttons()

    # ── construction ──────────────────────────────────────────────────────────

    def _build(self):
        self.columnconfigure(1, weight=1)

        ttk.Label(self, text="Original Photos:").grid(row=0, column=0, sticky="w", pady=3)
        ttk.Entry(self, textvariable=self.orig_var).grid(row=0, column=1, sticky="ew", padx=6, pady=3)
        ttk.Button(self, text="Browse…", command=self._pick_orig).grid(row=0, column=2, pady=3)
        self.save_orig_btn = ttk.Button(
            self, text="Save as Default", command=lambda: self._save_default("orig"))
        self.save_orig_btn.grid(row=0, column=3, sticky="ew", padx=(8, 0), pady=3)

        ttk.Label(self, text="Processed Photos:").grid(row=1, column=0, sticky="w", pady=3)
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
        self.tree.heading("kept", text="Non-image (kept)")
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
                self.app.mqtt_publish({mqtt_publisher.TASK_PROGRESS_TOPIC: f"{cur}/{tot}"})
        elif kind == "DONE":
            self._last_done = payload     # for MQTT last_run (published on exit)
            try:
                self._result = json.loads(payload)
            except ValueError:
                self._result = None
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
        folder = filedialog.askdirectory(title="Choose the folder with the ORIGINAL photos")
        if folder:
            self.orig_var.set(os.path.normpath(folder))

    def _pick_proc(self):
        folder = filedialog.askdirectory(title="Choose the folder with the PROCESSED photos")
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
            messagebox.showwarning(APP_TITLE, "Please choose a valid Original Photos folder.")
            return
        if not os.path.isdir(proc):
            messagebox.showwarning(APP_TITLE, "Please choose a valid Processed Photos folder.")
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
                    f"DELETE {n} original photo(s) and replace them with the processed "
                    f"versions?\n\nThe originals will NOT be archived."):
                return
            if not messagebox.askyesno(
                    APP_TITLE,
                    "Are you absolutely sure?\n\nDeleted originals cannot be recovered."):
                return
        else:
            if not messagebox.askyesno(
                    APP_TITLE,
                    f"Archive {n} original photo(s) into '__Archive__' and move the "
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

class UpdateDialog(tk.Toplevel):
    """
    Shows that a newer release is available, displays its patch notes, and (on
    confirmation) downloads the installer and launches it, then quits the app so
    Inno Setup can replace the running scripts. See updater.py.
    """

    def __init__(self, app, info):
        super().__init__(app)
        self.app  = app
        self.info = info
        self._downloading = False

        self.title("Update available")
        self.transient(app)
        self.resizable(True, True)
        self.minsize(520, 420)
        self.protocol("WM_DELETE_WINDOW", self._later)
        self._build()

        # Center on the parent and grab focus.
        self.update_idletasks()
        try:
            x = app.winfo_rootx() + (app.winfo_width()  - self.winfo_width())  // 2
            y = app.winfo_rooty() + (app.winfo_height() - self.winfo_height()) // 2
            self.geometry(f"+{max(0, x)}+{max(0, y)}")
        except Exception:
            pass
        self.grab_set()
        self.focus_set()

    def _build(self):
        outer = ttk.Frame(self, padding=14)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(2, weight=1)

        ttk.Label(outer, text=f"Image Toolbox {self.info.version} is available",
                  font=("Segoe UI", 12, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Label(outer, text=f"You're running {APP_VERSION}.",
                  foreground="#666").grid(row=1, column=0, sticky="w", pady=(2, 8))

        notes_frame = ttk.LabelFrame(outer, text="  What's new  ", padding=6)
        notes_frame.grid(row=2, column=0, sticky="nsew")
        notes_frame.rowconfigure(0, weight=1)
        notes_frame.columnconfigure(0, weight=1)

        txt = tk.Text(notes_frame, wrap="word", height=12, relief="flat",
                      background=self.cget("background"))
        scroll = ttk.Scrollbar(notes_frame, orient="vertical", command=txt.yview)
        txt.configure(yscrollcommand=scroll.set)
        txt.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        txt.insert("1.0", self.info.notes or "(No release notes were provided.)")
        txt.configure(state="disabled")

        # Progress (hidden until a download starts)
        self.progress_var = tk.DoubleVar(value=0.0)
        self.progress = ttk.Progressbar(outer, mode="determinate",
                                        maximum=100, variable=self.progress_var)
        self.status_lbl = ttk.Label(outer, text="", foreground="#666")

        # Buttons
        btns = ttk.Frame(outer)
        btns.grid(row=5, column=0, sticky="ew", pady=(12, 0))
        self.update_btn = ttk.Button(btns, text="Update now", command=self._update_now)
        self.skip_btn   = ttk.Button(btns, text="Skip this version", command=self._skip)
        self.later_btn  = ttk.Button(btns, text="Remind me later", command=self._later)
        self.update_btn.pack(side="left")
        self.later_btn.pack(side="right")
        self.skip_btn.pack(side="right", padx=(0, 6))

    # ── actions ────────────────────────────────────────────────────────────────

    def _set_busy(self, busy):
        state = "disabled" if busy else "normal"
        for b in (self.update_btn, self.skip_btn, self.later_btn):
            b.configure(state=state)

    def _update_now(self):
        if self._downloading:
            return
        if not self.info.asset_url:
            if messagebox.askyesno(
                    APP_TITLE,
                    "This release has no installer attached.\n\n"
                    "Open the releases page in your browser instead?"):
                webbrowser.open(updater.RELEASES_PAGE)
            return
        # Refuse to self-update while a tool is mid-run — replacing files then
        # would be unsafe. (The tabs lock each other, so checking all is enough.)
        busy = [t for t in (self.app.upscale_tab, self.app.tag_tab, self.app.conciliate_tab)
                if t.running]
        if busy:
            messagebox.showwarning(
                APP_TITLE, "Please let the current task finish before updating.")
            return

        self._downloading = True
        self._set_busy(True)
        self.progress.grid(row=3, column=0, sticky="ew", pady=(10, 2))
        self.status_lbl.grid(row=4, column=0, sticky="w")
        self.status_lbl.configure(text="Downloading the installer …")

        threading.Thread(target=self._download_worker, daemon=True).start()

    def _download_worker(self):
        def on_progress(done, total):
            pct = (done / total * 100) if total else 0
            self.after(0, lambda: self._on_progress(done, total, pct))
        try:
            path = updater.download_installer(
                self.info.asset_url, expected_size=self.info.asset_size,
                progress_cb=on_progress)
        except Exception as exc:
            self.after(0, lambda: self._on_download_error(str(exc)))
            return
        self.after(0, lambda: self._on_download_done(path))

    def _on_progress(self, done, total, pct):
        self.progress_var.set(pct)
        if total:
            self.status_lbl.configure(
                text=f"Downloading … {done // (1024*1024)} / {total // (1024*1024)} MB")
        else:
            self.status_lbl.configure(text=f"Downloading … {done // (1024*1024)} MB")

    def _on_download_error(self, msg):
        self._downloading = False
        self._set_busy(False)
        self.progress.grid_remove()
        self.status_lbl.configure(text="Download failed.", foreground="#b3261e")
        messagebox.showerror(APP_TITLE, f"The update could not be downloaded:\n\n{msg}")

    def _on_download_done(self, path):
        self.status_lbl.configure(
            text="Starting the installer — the app will now close.", foreground="#1a7f37")
        try:
            updater.launch_installer(path)
        except Exception as exc:
            self._on_download_error(f"Could not start the installer: {exc}")
            return
        # Quit so Inno can overwrite the running scripts; the installer will
        # offer to relaunch the app when it finishes.
        self.app._save_geometry()
        if self.app.log_window is not None and self.app.log_window.winfo_exists():
            self.app.log_window.save_geometry()
        self.app.destroy()

    def _skip(self):
        set_update_skipped_version(self.info.version)
        self.destroy()

    def _later(self):
        if self._downloading:
            return
        self.destroy()


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
        self.log_window = None          # single shared LogViewer for both tools
        self.comparison_window = None   # single shared ComparisonWindow
        self._migrate_default_folders()
        self._restore_geometry()
        self._install_picklist_wheel_guard()

        self.nb = ttk.Notebook(self)
        self.upscale_tab    = UpscaleTab(self.nb, self)
        self.tag_tab        = TagTab(self.nb, self)
        self.conciliate_tab = ConciliateTab(self.nb, self)
        self.settings_tab   = SettingsTab(self.nb, self)
        self.nb.add(self.upscale_tab,    text="  Batch Upscaler  ")
        self.nb.add(self.tag_tab,        text="  Tag & Rename  ")
        self.nb.add(self.conciliate_tab, text="  Conciliation  ")
        self.nb.add(self.settings_tab,   text="  Settings  ")
        # Bottom status bar with a right-aligned "Report an issue" link (Future
        # Feature #3). Packed before the notebook so it reserves the bottom strip;
        # always visible regardless of the active tab.
        self._build_statusbar()
        self.nb.pack(fill="both", expand=True, padx=8, pady=8)

        # System telemetry (Feature #3a): one CPU sampler for the whole app, and
        # the per-tab readout rows it feeds. Samples run off the UI thread; the
        # lock keeps a slow nvidia-smi call from overlapping with the next tick.
        self._cpu_sampler   = system_telemetry.CpuSampler()
        self._telemetry_lock = threading.Lock()
        self.telemetry_rows = [t.telemetry_row for t in
                               (self.upscale_tab, self.tag_tab, self.conciliate_tab)
                               if t.telemetry_row is not None]
        # Idle sampler: keep the readout live between runs (e.g. so the user can
        # watch another app's VRAM free up before starting an upscale). Fires on
        # a slow 60 s cadence and only when no task is running — task-driven
        # sampling owns the readout during a run.
        self._idle_telemetry_job = None
        self.after(2000, self._idle_telemetry_tick)

        # Guard against leaving the Settings tab with unsaved edits.
        self._suppress_tab_event = False
        self._prev_tab_widget    = self.nb.nametowidget(self.nb.select())
        self.nb.bind("<<NotebookTabChanged>>", self._on_tab_changed)

        self.bind("<Configure>", self._track_geometry)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # Persistent MQTT client (Home Assistant integration). Starts here so the
        # availability LWT is registered before anything else happens.
        self.mqtt = None
        if mqtt_enabled():
            self.start_mqtt()

        # Shortly after the window is up, run the startup checks off the UI
        # thread: one GitHub release check feeds both the update prompt (if the
        # auto-check is on) and the MQTT version/update topics (if enabled).
        self._update_dialog = None
        if update_auto_check_enabled() or mqtt_enabled():
            self.after(1500, lambda: threading.Thread(
                target=self._startup_worker, daemon=True).start())

    def _install_picklist_wheel_guard(self):
        """Stop a mouse-wheel scroll over a ttk Combobox/Spinbox from silently
        changing its value — a Windows footgun that can flip a setting unnoticed
        (and then trip the unsaved-changes guard). Replaces the default class
        binding once, so it covers every current and future picklist; the wheel
        is redirected to the nearest scrollable canvas so the surrounding page
        still scrolls. The open dropdown list is a separate widget class, so it
        keeps its own scrolling."""
        for cls in ("TCombobox", "TSpinbox"):
            self.bind_class(cls, "<MouseWheel>", self._picklist_wheel)

    def _picklist_wheel(self, event):
        # Forward the scroll to the first scrollable Canvas ancestor (if any),
        # then return "break" so the widget's own value-changing binding — and
        # the page's bind_all handler — don't also fire.
        w = getattr(event.widget, "master", None)
        while w is not None:
            if isinstance(w, tk.Canvas):
                try:
                    w.yview_scroll(int(-event.delta / 120), "units")
                except tk.TclError:
                    pass
                break
            w = getattr(w, "master", None)
        return "break"

    def _build_statusbar(self):
        """A thin bottom strip with a right-aligned 'Report an issue' link
        (Future Feature #3). The link opens a pre-filled GitHub new-issue page."""
        bar = ttk.Frame(self)
        bar.pack(side="bottom", fill="x", padx=10, pady=(0, 4))
        link = tk.Label(bar, text="Report an issue", fg="#3a86ff",
                        cursor="hand2", font=("Segoe UI", 9, "underline"))
        link.pack(side="right")
        link.bind("<Button-1>", lambda _e: report_issue())
        # Subtle hover feedback (darker blue) so it reads as a link.
        link.bind("<Enter>", lambda _e: link.configure(fg="#1a5fd0"))
        link.bind("<Leave>", lambda _e: link.configure(fg="#3a86ff"))

    def _migrate_default_folders(self):
        """Carry default folders saved by older builds in gui_settings.json over
        to config.json, where they now live (one-time, before the tabs load)."""
        defs = CFG.setdefault("defaults", {})
        moved = False
        for old_key, new_key in (("default_source", "upscale_source"),
                                  ("default_output", "upscale_output")):
            if not defs.get(new_key) and self.settings.get(old_key):
                defs[new_key] = self.settings[old_key]
                moved = True
        if moved:
            save_config()

    def sync_settings_defaults(self):
        """Push the latest default folders into the Settings tab's fields."""
        st = getattr(self, "settings_tab", None)
        if st is not None:
            st.load_defaults()

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

    # ── Unsaved-settings guard ───────────────────────────────────────────────

    def _on_tab_changed(self, event=None):
        """When leaving the Settings tab with unsaved edits, prompt to save."""
        if self._suppress_tab_event:
            return
        try:
            new_widget = self.nb.nametowidget(self.nb.select())
        except Exception:
            return
        leaving_settings = (self._prev_tab_widget is self.settings_tab
                            and new_widget is not self.settings_tab)
        if leaving_settings and self.settings_tab.is_dirty():
            if self._confirm_unsaved("leaving") == "cancel":
                # Bounce back to Settings without re-triggering this handler.
                self._suppress_tab_event = True
                try:
                    self.nb.select(self.settings_tab)
                finally:
                    self._suppress_tab_event = False
                return   # _prev_tab_widget stays on Settings
        self._prev_tab_widget = new_widget

    def _confirm_unsaved(self, context):
        """Modal Save / Don't save / Cancel prompt for unsaved Settings edits.
        Returns 'ok' (handled — safe to proceed) or 'cancel' (stay put).
        `context` is 'leaving' or 'closing' for the wording."""
        verb = "closing" if context == "closing" else "leaving the Settings tab"
        ans = messagebox.askyesnocancel(
            APP_TITLE,
            f"You have unsaved changes in Settings.\n\nSave them before {verb}?")
        if ans is None:
            return "cancel"
        if ans:
            return "ok" if self.settings_tab._save() else "cancel"
        # "Don't save" — discard the edits so the form matches config.json again.
        self.settings_tab.revert()
        return "ok"

    def other_tab(self, tab):
        return self.tag_tab if tab is self.upscale_tab else self.upscale_tab

    def set_tag_tab_enabled(self, enabled):
        """Grey out the Tag & Rename tab while an upscale run owns the GPU."""
        self.nb.tab(self.tag_tab, state="normal" if enabled else "disabled")

    def refresh_conciliate_lock(self):
        """Grey out the Conciliation tab while the Batch Upscaler or Tag & Rename
        is running — they may be reading or writing the same folders."""
        busy = self.upscale_tab.running or self.tag_tab.running
        self.nb.tab(self.conciliate_tab, state="disabled" if busy else "normal")

    # ── Shared log window ────────────────────────────────────────────────────

    def show_log(self, console, title):
        """Open (or focus) the single shared log window, bound to `console`."""
        if self.log_window is not None and self.log_window.winfo_exists():
            self.log_window.bind_console(console, title)
            self.log_window.lift()
            self.log_window.focus_set()
        else:
            self.log_window = LogViewer(self, console, title, app=self)

    def rebind_log_if_open(self, console, title):
        """If the log window is open, switch it to follow `console`."""
        if self.log_window is not None and self.log_window.winfo_exists():
            self.log_window.bind_console(console, title)

    # ── Shared comparison window ─────────────────────────────────────────────

    def show_comparison(self, source, output):
        """Open (or re-target) the single shared comparison window for a pair."""
        if self.comparison_window is not None and self.comparison_window.winfo_exists():
            self.comparison_window.show(source, output)
        else:
            self.comparison_window = ComparisonWindow(self, source, output, app=self)

    # ── Updates ──────────────────────────────────────────────────────────────

    def _startup_worker(self):
        """
        Launch-time background task (off the UI thread): check GitHub once, then
        prompt for a not-skipped update (if the auto-check is on) and/or publish
        the version snapshot to MQTT (if enabled).
        """
        status, payload = updater.check_for_update(APP_VERSION)
        update_available = (status == "update")
        latest = payload.version if update_available else APP_VERSION

        if self.mqtt is not None:
            self.mqtt.publish_many({
                mqtt_publisher.VERSION_TOPIC:        APP_VERSION,
                mqtt_publisher.UPDATE_TOPIC:         "yes" if update_available else "no",
                mqtt_publisher.LATEST_VERSION_TOPIC: latest,
            })

        if (update_auto_check_enabled() and update_available
                and payload.version != update_skipped_version()):
            self.after(0, lambda: self.show_update_dialog(payload))

    # ── MQTT (Home Assistant) ────────────────────────────────────────────────

    def start_mqtt(self):
        """Start the persistent MQTT client from the saved config and verify the
        connection (the result is reported via _on_mqtt_state). Best-effort."""
        if not mqtt_enabled():
            return
        if not mqtt_publisher.mqtt_available():
            self._on_mqtt_state(False, "paho-mqtt is not installed.")
            return
        client = mqtt_publisher.MqttClient(mqtt_config(), status_cb=self._on_mqtt_state)
        ok, msg = client.start()
        if ok:
            self.mqtt = client
            self._set_mqtt_status("Connecting…", None)
        else:
            self.mqtt = None
            self._set_mqtt_status(msg, False)

    def _set_mqtt_status(self, text, connected):
        st = getattr(self, "settings_tab", None)
        if st is not None and hasattr(st, "mqtt_status"):
            color = "#666" if connected is None else ("#1a7f37" if connected else "#b3261e")
            st.mqtt_status.configure(text=f"MQTT: {text}", foreground=color)

    def _on_mqtt_state(self, connected, message):
        """Connection-state callback (runs on the MQTT thread) → update the UI."""
        try:
            self.after(0, lambda: self._set_mqtt_status(message, connected))
        except Exception:
            pass

    def stop_mqtt(self, last_used=None):
        if self.mqtt is not None:
            self.mqtt.stop(last_used=last_used)
            self.mqtt = None

    def restart_mqtt(self):
        """Apply changed MQTT settings: drop any existing client and re-start."""
        self.stop_mqtt()
        if mqtt_enabled():
            self.start_mqtt()

    def mqtt_publish(self, values):
        """Publish a {topic: payload} mapping if the client is running (no-op
        otherwise). Called from the tool tabs as task state changes. Uses getattr
        because the tabs are constructed before self.mqtt is assigned."""
        client = getattr(self, "mqtt", None)
        if client is not None:
            client.publish_many(values)

    # ── System telemetry (Feature #3a) ───────────────────────────────────────

    IDLE_TELEMETRY_MS = 60000   # idle sampling cadence (no task running)

    def _any_task_running(self):
        return any(t.running for t in
                   (self.upscale_tab, self.tag_tab, self.conciliate_tab))

    def _idle_telemetry_tick(self):
        """Sample while idle so the readout stays live between runs. Skips when a
        task is running — that path samples on its own (faster) cadence."""
        if not self._any_task_running():
            self.sample_telemetry()
        self._idle_telemetry_job = self.after(self.IDLE_TELEMETRY_MS,
                                              self._idle_telemetry_tick)

    def sample_telemetry(self):
        """Take one telemetry sample off the UI thread, then push the result to
        the in-app rows and MQTT. Skips if a previous sample is still in flight
        (nvidia-smi can take a moment) so overlapping ticks never pile up."""
        if not self._telemetry_lock.acquire(blocking=False):
            return
        threading.Thread(target=self._telemetry_worker, daemon=True).start()

    def _telemetry_worker(self):
        try:
            cpu = self._cpu_sampler.sample()
            ram = system_telemetry.sample_ram()
            gpu = system_telemetry.sample_gpu()
        finally:
            self._telemetry_lock.release()
        sample = {"cpu": cpu}
        if ram is not None:
            sample.update(ram_used_mb=ram[0], ram_total_mb=ram[1])
        else:
            sample.update(ram_used_mb=None, ram_total_mb=None)
        if gpu is not None:
            used, total, temp = gpu
            sample.update(gpu_used_mb=used, gpu_total_mb=total, gpu_temp_c=temp)
        else:
            sample.update(gpu_used_mb=None, gpu_total_mb=None, gpu_temp_c=None)
        try:
            self.after(0, lambda: self._apply_telemetry(sample))
        except Exception:
            pass

    def _apply_telemetry(self, sample):
        """On the UI thread: render the sample in every readout row and mirror
        it to the MQTT system topics."""
        for row in self.telemetry_rows:
            try:
                row.show(sample)
            except Exception:
                pass
        values = {}
        if sample.get("cpu") is not None:
            values[mqtt_publisher.SYS_CPU_TOPIC] = f"{sample['cpu']:.0f}"
        if sample.get("ram_used_mb") is not None:
            values[mqtt_publisher.SYS_RAM_TOPIC]       = str(sample["ram_used_mb"])
            values[mqtt_publisher.SYS_RAM_TOTAL_TOPIC] = str(sample["ram_total_mb"])
        if sample.get("gpu_used_mb") is not None:
            values[mqtt_publisher.SYS_GPU_VRAM_TOPIC]       = str(sample["gpu_used_mb"])
            values[mqtt_publisher.SYS_GPU_VRAM_TOTAL_TOPIC] = str(sample["gpu_total_mb"])
        if sample.get("gpu_temp_c") is not None:
            values[mqtt_publisher.SYS_GPU_TEMP_TOPIC] = str(sample["gpu_temp_c"])
        if values:
            self.mqtt_publish(values)

    def show_update_dialog(self, info):
        """Open (or focus) the single update dialog for the given UpdateInfo."""
        if self._update_dialog is not None and self._update_dialog.winfo_exists():
            self._update_dialog.lift()
            self._update_dialog.focus_set()
            return
        self._update_dialog = UpdateDialog(self, info)

    def _on_close(self):
        # Don't let unsaved Settings edits vanish on exit.
        if self.settings_tab.is_dirty() and self._confirm_unsaved("closing") == "cancel":
            return
        busy = [t for t in (self.upscale_tab, self.tag_tab, self.conciliate_tab) if t.running]
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
        # Persist the main window layout and the shared log window's layout.
        self._save_geometry()
        if self.log_window is not None and self.log_window.winfo_exists():
            self.log_window.save_geometry()
        if self.comparison_window is not None and self.comparison_window.winfo_exists():
            self.comparison_window.save_geometry()
        # Record last-used time and announce going offline before we exit.
        self.stop_mqtt(last_used=datetime.datetime.now().isoformat(timespec="seconds"))
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
