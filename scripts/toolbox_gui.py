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
import taskbar_progress
import runpod_client
import notifications
import ssh_setup
# Single-instance guard is optional — a packaging miss must not brick startup.
try:
    import single_instance
except Exception:
    single_instance = None

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog

# SCRIPT_DIR is where this module (and its sibling child scripts) live: the
# scripts/ folder. APP_ROOT is its parent — where config.json, gui_settings.json,
# the .venv, logs/, db/ and the seedvr2/ engine all live.
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
APP_ROOT   = os.path.dirname(SCRIPT_DIR)
APP_TITLE  = "Image Toolbox"
# Shown in the main window title bar. On a release, set this to the tag (e.g.
# "0.1.3") and drop the "-experimental" suffix.
APP_VERSION = "0.3.9"

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


def get_install_mode():
    """Installation mode chosen at install time: 'local' | 'remote' | 'both'.

    Read from install_mode.txt (written by the installer; see bootstrap.ps1). A
    Remote-only install has NO local upscaling engine (torch + SeedVR2 are
    skipped), so the GUI defaults the 'Run on remote pod' toggle on and refuses a
    local run. Missing/unknown marker → 'both' (a from-source run or a pre-0.3.2
    install supports everything)."""
    try:
        with open(os.path.join(APP_ROOT, "install_mode.txt"), encoding="utf-8") as f:
            mode = f.read().strip().lower()
        if mode in ("local", "remote", "both"):
            return mode
    except OSError:
        pass
    return "both"


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


# Discord/Telegram probes live in notifications.py (shared with the runners).
# test_discord_webhook stays as a thin alias so existing callers keep working.
test_discord_webhook = notifications.test_discord


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


# ─────────────────────────────────────────────
#  COMPARISON WINDOW  (original vs. upscaled)
# ─────────────────────────────────────────────

class ComparisonWindow(tk.Toplevel):
    """
    Floating, resizable original-vs-upscaled comparison (Future Feature #1).

    Both images are drawn aligned on a single canvas, split by a vertical
    **before/after wipe**: left of the divider shows the original, right shows the
    upscaled result. Zoom and pan are *shared*, so the two halves always show the
    same region — the quality gain is directly visible (the lower-resolution
    original, magnified to the same on-screen scale, reads softer).

    Interaction:
      * mouse wheel  — zoom in/out, centred on the pointer (fit … 400% of the
        upscaled image's native pixels)
      * drag image   — pan, clamped so the view stays filled
      * drag divider — slide the wipe (grab the handle / the divider line)

    Only the visible region of each side is decoded (Pillow ``resize`` with a
    float ``box``), so even 4K stays responsive; an interactive gesture renders
    with a fast filter and a crisp pass follows when it settles. Like the log
    window there is one shared instance, re-targeted on each double-click.
    """

    ZOOM_STEP = 1.25         # per wheel notch
    ABS_MAX   = 4.0          # max screen px per upscaled-image px (≈400%)
    HANDLE_TOL = 9           # px around the divider that grabs the wipe
    DIVIDER   = "#e8edf3"
    HANDLE_FILL = "#e8edf3"
    HANDLE_EDGE = "#202329"

    def __init__(self, master, source, output, app=None):
        super().__init__(master)
        self._app = app
        geo = app.settings.get("compare_geometry") if app is not None else None
        self.geometry(geo if (geo and _geometry_on_screen(self, geo)) else "1100x640")
        self.minsize(560, 360)
        self._last_normal_geo = None
        if app is not None and app.settings.get("compare_zoomed"):
            try:
                self.state("zoomed")
            except tk.TclError:
                pass

        bar = ttk.Frame(self, padding=(8, 4))
        bar.pack(fill="x")
        self._old_lbl = ttk.Label(bar, foreground="#aab2bf")
        self._old_lbl.pack(side="left")
        self._new_lbl = ttk.Label(bar, foreground="#aab2bf")
        self._new_lbl.pack(side="right")

        self.canvas = tk.Canvas(self, highlightthickness=0, bg="#15181d")
        self.canvas.pack(fill="both", expand=True)

        # View state (shared by both images)
        self._old = None          # PIL image, native resolution
        self._new = None          # PIL image, native resolution (the reference)
        self._zoom = 1.0          # 1.0 == fit-to-window
        self._ox = self._oy = 0.0 # canvas px of the display rect's top-left
        self._wipe_frac = 0.5     # divider position as a fraction of width
        self._inited = False      # offsets centred once images/size are known
        self._last_size = None    # (cw, ch) for centre-preserving resize
        self._drag = None         # None | ("pan", x, y) | ("wipe",)
        self._photos = []         # keep PhotoImage refs alive
        self._crisp_after = None

        self.canvas.bind("<Configure>", self._on_configure)
        self.canvas.bind("<MouseWheel>", self._on_wheel)
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Motion>", self._on_motion)
        # Track the last non-maximised geometry on the Toplevel itself, so a
        # close-while-maximised still records a sensible size + the zoomed flag.
        self.bind("<Configure>", self._track_geometry, add="+")

        self.protocol("WM_DELETE_WINDOW", self._close)
        self.show(source, output)

    # ── Public API (shared single instance) ──────────────────────────────────

    def show(self, source, output):
        """Point the window at a new pair and reset the view to fit."""
        from PIL import Image, ImageOps

        def _load(path):
            try:
                im = Image.open(path)
                return ImageOps.exif_transpose(im).convert("RGB")
            except Exception:
                return None

        self._old = _load(source)
        self._new = _load(output)
        self._old_lbl.configure(text="Original:  " + self._caption(source, self._old))
        self._new_lbl.configure(text="Upscaled:  " + self._caption(output, self._new))
        self.title(f"{APP_TITLE} — Compare — {os.path.basename(source)}")
        self._zoom = 1.0
        self._wipe_frac = 0.5
        self._inited = False
        self.lift()
        self.focus_set()
        self._render()

    def _track_geometry(self, event):
        if event.widget is self and self.state() == "normal":
            self._last_normal_geo = self.geometry()

    def save_geometry(self):
        if self._app is not None and self.winfo_exists():
            try:
                zoomed = (self.state() == "zoomed")
            except tk.TclError:
                zoomed = False
            self._app.settings["compare_geometry"] = self._last_normal_geo or self.geometry()
            self._app.settings["compare_zoomed"] = zoomed
            save_settings(self._app.settings)

    # ── Geometry helpers ─────────────────────────────────────────────────────

    @staticmethod
    def _caption(path, img):
        if img is None:
            return f"{os.path.basename(path)}  ·  (cannot open)"
        return f"{os.path.basename(path)}  ·  {img.width}×{img.height}"

    def _size(self):
        return max(self.canvas.winfo_width(), 1), max(self.canvas.winfo_height(), 1)

    def _fit(self, cw, ch):
        """Absolute scale (screen px per reference px) at zoom == 1.0."""
        return min(cw / self._new.width, ch / self._new.height)

    def _zoom_max(self, fit):
        return max(1.0, self.ABS_MAX / fit)

    @staticmethod
    def _clamp_axis(o, disp, view):
        """Keep the view filled: centre when the image is smaller than the view,
        otherwise stop the image edges from leaving a gap."""
        if disp <= view:
            return (view - disp) / 2.0
        return min(0.0, max(view - disp, o))

    # ── Interaction ──────────────────────────────────────────────────────────

    def _on_configure(self, _event):
        if self._new is None:
            return
        cw, ch = self._size()
        if self._inited and self._last_size and self._last_size != (cw, ch):
            # Preserve the image point at the view centre across a resize.
            ocw, och = self._last_size
            os_ = self._fit(ocw, och) * self._zoom
            odw, odh = self._new.width * os_, self._new.height * os_
            cu = (ocw / 2 - self._ox) / odw if odw else 0.5
            cv = (och / 2 - self._oy) / odh if odh else 0.5
            ns = self._fit(cw, ch) * self._zoom
            self._ox = cw / 2 - cu * self._new.width * ns
            self._oy = ch / 2 - cv * self._new.height * ns
        self._render()

    def _on_wheel(self, event):
        if self._new is None:
            return
        cw, ch = self._size()
        fit = self._fit(cw, ch)
        s = fit * self._zoom
        dw, dh = self._new.width * s, self._new.height * s
        # Image-space fraction under the pointer, kept fixed across the zoom.
        u = (event.x - self._ox) / dw if dw else 0.5
        v = (event.y - self._oy) / dh if dh else 0.5
        factor = self.ZOOM_STEP if event.delta > 0 else 1.0 / self.ZOOM_STEP
        nz = min(max(self._zoom * factor, 1.0), self._zoom_max(fit))
        if nz == self._zoom:
            return
        s2 = fit * nz
        self._ox = event.x - u * self._new.width * s2
        self._oy = event.y - v * self._new.height * s2
        self._zoom = nz
        self._render(fast=True)
        self._schedule_crisp()

    def _on_press(self, event):
        if self._new is None:
            return
        cw, _ = self._size()
        wipe_x = self._wipe_frac * cw
        if abs(event.x - wipe_x) <= self.HANDLE_TOL:
            self._drag = ("wipe",)
        else:
            self._drag = ("pan", event.x, event.y)

    def _on_drag(self, event):
        if self._drag is None or self._new is None:
            return
        cw, ch = self._size()
        if self._drag[0] == "wipe":
            self._wipe_frac = min(1.0, max(0.0, event.x / cw))
        else:
            _, lx, ly = self._drag
            self._ox += event.x - lx
            self._oy += event.y - ly
            self._drag = ("pan", event.x, event.y)
        self._render(fast=True)

    def _on_release(self, _event):
        if self._drag is not None:
            self._drag = None
            self._render(fast=False)

    def _on_motion(self, event):
        if self._drag is not None or self._new is None:
            return
        cw, _ = self._size()
        near = abs(event.x - self._wipe_frac * cw) <= self.HANDLE_TOL
        self.canvas.configure(cursor="sb_h_double_arrow" if near else "fleur")

    def _schedule_crisp(self):
        if self._crisp_after is not None:
            self.after_cancel(self._crisp_after)
        self._crisp_after = self.after(140, lambda: self._render(fast=False))

    # ── Rendering ────────────────────────────────────────────────────────────

    def _render(self, fast=False):
        from PIL import Image

        self._crisp_after = None
        self.canvas.delete("all")
        self._photos = []
        cw, ch = self._size()
        if self._new is None:
            self.canvas.create_text(cw / 2, ch / 2, fill="#aab2bf",
                                    text="Cannot open the upscaled image.")
            return

        s = self._fit(cw, ch) * self._zoom
        disp_w = self._new.width * s
        disp_h = self._new.height * s
        if not self._inited:
            self._ox = (cw - disp_w) / 2.0
            self._oy = (ch - disp_h) / 2.0
            self._inited = True
        self._ox = self._clamp_axis(self._ox, disp_w, cw)
        self._oy = self._clamp_axis(self._oy, disp_h, ch)
        self._last_size = (cw, ch)

        wipe_x = min(float(cw), max(0.0, self._wipe_frac * cw))
        resample = Image.BILINEAR if fast else Image.LANCZOS
        # Left of the divider = original; right = upscaled. Same display rect, so
        # the two halves are pixel-aligned at every zoom/pan.
        if self._old is not None:
            self._blit(self._old, 0.0, wipe_x, disp_w, disp_h, cw, ch, resample)
        self._blit(self._new, wipe_x, float(cw), disp_w, disp_h, cw, ch, resample)
        self._draw_divider(wipe_x, ch)

    def _blit(self, img, x0, x1, disp_w, disp_h, cw, ch, resample):
        """Draw the slice of `img` visible in canvas x-range [x0, x1]."""
        from PIL import ImageTk

        cx0 = max(x0, self._ox, 0.0)
        cx1 = min(x1, self._ox + disp_w, float(cw))
        cy0 = max(0.0, self._oy)
        cy1 = min(float(ch), self._oy + disp_h)
        if cx1 - cx0 < 1 or cy1 - cy0 < 1:
            return
        iw, ih = img.size
        sx0 = (cx0 - self._ox) / disp_w * iw
        sx1 = (cx1 - self._ox) / disp_w * iw
        sy0 = (cy0 - self._oy) / disp_h * ih
        sy1 = (cy1 - self._oy) / disp_h * ih
        tw = max(1, int(round(cx1 - cx0)))
        th = max(1, int(round(cy1 - cy0)))
        tile = img.resize((tw, th), resample, box=(sx0, sy0, sx1, sy1))
        photo = ImageTk.PhotoImage(tile)
        self._photos.append(photo)
        self.canvas.create_image(int(round(cx0)), int(round(cy0)),
                                 anchor="nw", image=photo)

    def _draw_divider(self, wipe_x, ch):
        self.canvas.create_line(wipe_x, 0, wipe_x, ch, fill=self.DIVIDER, width=2)
        cy = ch / 2
        self.canvas.create_rectangle(wipe_x - 7, cy - 24, wipe_x + 7, cy + 24,
                                     fill=self.HANDLE_FILL, outline=self.HANDLE_EDGE)
        # Two little arrows hinting the handle slides horizontally.
        for dx, tip in ((-3, -7), (3, 7)):
            self.canvas.create_line(wipe_x + dx, cy - 5, wipe_x + tip, cy,
                                    wipe_x + dx, cy + 5, fill=self.HANDLE_EDGE)

    def _close(self):
        self.save_geometry()
        self.destroy()


class VideoComparisonWindow(ComparisonWindow):
    """Original-vs-upscaled **video** comparison (Video Upscaler #2, phase 5). The
    video analogue of ComparisonWindow: it reuses the parent's shared zoom / pan /
    before-after-wipe rendering verbatim, and only swaps the image source — each
    seek decodes a frame PAIR from the two videos through the bundled ffmpeg
    (`-ss <t> -i <file> -frames:v 1` to a PNG pipe -> Pillow) and feeds them into
    the same renderer.

    v1 is **scrub + frame-step**, aligned by TIMESTAMP not frame index (the
    upscaled frame count can differ after a CFR-normalize, section 14), so seeking
    both sides to the same time keeps the same content under the wipe. Zoom/pan are
    preserved across seeks; only opening a new pair resets the view. Decoding runs
    off the UI thread. See docs/video-upscaler.md sections 11 / 15."""

    def __init__(self, master, source, upscaled, app=None):
        tk.Toplevel.__init__(self, master)
        self._app = app
        geo = app.settings.get("video_compare_geometry") if app is not None else None
        self.geometry(geo if (geo and _geometry_on_screen(self, geo)) else "1100x680")
        self.minsize(560, 380)
        self._last_normal_geo = None
        if app is not None and app.settings.get("video_compare_zoomed"):
            try:
                self.state("zoomed")
            except tk.TclError:
                pass

        bar = ttk.Frame(self, padding=(8, 4))
        bar.pack(fill="x")
        self._old_lbl = ttk.Label(bar, foreground="#aab2bf")
        self._old_lbl.pack(side="left")
        self._new_lbl = ttk.Label(bar, foreground="#aab2bf")
        self._new_lbl.pack(side="right")

        # Bottom transport: frame-step, a scrubber, and a time read-out.
        tl = ttk.Frame(self, padding=(8, 4))
        tl.pack(side="bottom", fill="x")
        ttk.Button(tl, text="◀ frame", width=8,
                   command=lambda: self._step(-1)).pack(side="left")
        ttk.Button(tl, text="frame ▶", width=8,
                   command=lambda: self._step(1)).pack(side="left", padx=(4, 8))
        self.time_var = tk.StringVar(value="0:00.000 / 0:00.000")
        ttk.Label(tl, textvariable=self.time_var, width=20,
                  font=("Consolas", 9)).pack(side="left")
        self.timeline = ttk.Scale(tl, from_=0.0, to=1.0, orient="horizontal",
                                  command=self._on_scrub)
        self.timeline.pack(side="left", fill="x", expand=True, padx=(8, 0))

        self.canvas = tk.Canvas(self, highlightthickness=0, bg="#15181d")
        self.canvas.pack(fill="both", expand=True)

        # Shared view state (consumed by the inherited renderer).
        self._old = self._new = None
        self._zoom = 1.0
        self._ox = self._oy = 0.0
        self._wipe_frac = 0.5
        self._inited = False
        self._last_size = None
        self._drag = None
        self._photos = []
        self._crisp_after = None
        # Video state.
        self._src_path = self._up_path = None
        self._duration = 0.0
        self._fps = 30.0
        self._cur_t = 0.0
        self._scrub_after = None
        self._seek_seq = 0

        self.canvas.bind("<Configure>", self._on_configure)
        self.canvas.bind("<MouseWheel>", self._on_wheel)
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Motion>", self._on_motion)
        self.bind("<Configure>", self._track_geometry, add="+")
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.show_videos(source, upscaled)

    def save_geometry(self):
        if self._app is not None and self.winfo_exists():
            try:
                zoomed = (self.state() == "zoomed")
            except tk.TclError:
                zoomed = False
            self._app.settings["video_compare_geometry"] = self._last_normal_geo or self.geometry()
            self._app.settings["video_compare_zoomed"] = zoomed
            save_settings(self._app.settings)

    # ── public API (shared single instance) ──────────────────────────────────

    def show_videos(self, source, upscaled):
        """Point the window at a new (source, upscaled) pair and reset the view."""
        import video_pipeline as vp
        self._src_path, self._up_path = source, upscaled
        try:
            si, ui = vp.probe(source), vp.probe(upscaled)
        except Exception:
            si = ui = None
        sdim = f"{si.width}×{si.height}" if si else "?"
        udim = f"{ui.width}×{ui.height}" if ui else "?"
        self._duration = max((si.duration if si else 0) or 0,
                             (ui.duration if ui else 0) or 0) or 0.01
        self._fps = float((ui.fps if ui else None) or (si.fps if si else None) or 30)
        self._old_lbl.configure(text=f"Original:  {os.path.basename(source)}  ·  {sdim}")
        self._new_lbl.configure(text=f"Upscaled:  {os.path.basename(upscaled)}  ·  {udim}")
        self.title(f"{APP_TITLE} — Compare video — {os.path.basename(source)}")
        self._zoom = 1.0
        self._wipe_frac = 0.5
        self._inited = False
        self.timeline.configure(to=max(0.01, self._duration))
        self.timeline.set(0.0)
        self.lift()
        self.focus_set()
        self._seek(0.0)

    # ── transport ─────────────────────────────────────────────────────────────

    @staticmethod
    def _fmt_t(t):
        t = max(0.0, t)
        m, s = divmod(t, 60)
        return f"{int(m)}:{s:06.3f}"

    def _update_time_label(self, t):
        self.time_var.set(f"{self._fmt_t(t)} / {self._fmt_t(self._duration)}")

    def _on_scrub(self, val):
        try:
            t = float(val)
        except (TypeError, ValueError):
            return
        self._update_time_label(t)
        # Debounce: decode only after the scrub settles (decoding is ~hundreds of
        # ms per frame and we decode two), so dragging stays smooth.
        if self._scrub_after is not None:
            self.after_cancel(self._scrub_after)
        self._scrub_after = self.after(160, lambda: self._seek(t))

    def _step(self, frames):
        t = max(0.0, min(self._duration, self._cur_t + frames / max(1e-6, self._fps)))
        self.timeline.set(t)
        self._seek(t)

    def _seek(self, t):
        """Decode the frame pair at time `t` off the UI thread, then render."""
        self._cur_t = t
        self._update_time_label(t)
        src, up = self._src_path, self._up_path
        if not src or not up:
            return
        self._seek_seq += 1
        seq = self._seek_seq

        def work():
            o = self._decode_frame(src, t)
            n = self._decode_frame(up, t)
            self.after(0, lambda: self._apply_frames(seq, o, n))

        threading.Thread(target=work, daemon=True).start()

    def _apply_frames(self, seq, old_img, new_img):
        if seq != self._seek_seq:        # a newer seek superseded this one
            return
        if new_img is not None:
            self._new = new_img
        if old_img is not None:
            self._old = old_img
        self._render()

    def _decode_frame(self, path, t):
        """One frame at time `t` via the bundled ffmpeg -> PIL (no new dependency).
        Returns None on any failure (the renderer shows the placeholder)."""
        import io
        import video_pipeline as vp
        from PIL import Image
        try:
            ffmpeg, _ = vp.find_ffmpeg()
        except Exception:
            return None
        args = [ffmpeg, "-hide_banner", "-loglevel", "error",
                "-ss", f"{max(0.0, t):.3f}", "-i", path, "-frames:v", "1",
                "-f", "image2pipe", "-vcodec", "png", "-"]
        try:
            cp = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                creationflags=CREATE_NO_WINDOW)
            if cp.returncode != 0 or not cp.stdout:
                return None
            return Image.open(io.BytesIO(cp.stdout)).convert("RGB")
        except Exception:
            return None


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
        Tooltip(self.remote_chk,
                "Run on a rented RunPod GPU instead of this PC (roadmap #1, "
                "experimental). Creates a billed pod and terminates it when done. "
                "Needs a RunPod API key + model volume in Settings.")
        ttk.Label(rr, text="GPU:").pack(side="left", padx=(16, 4))
        self.gpu_pick_var = tk.StringVar(value="")
        self.gpu_combo = ttk.Combobox(
            rr, textvariable=self.gpu_pick_var, state="disabled", width=44)
        self.gpu_combo.pack(side="left")
        Tooltip(self.gpu_combo,
                "GPUs that can be rented in your volume's region right now, "
                "cheapest first. Live availability and price from RunPod. If your "
                "pick is unavailable, only cheaper in-stock cards under the price "
                "ceiling (RunPod tab) are tried automatically.")
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
        if self.console.find_last(
                r"no instances currently available|Pod failed to deploy on any of"
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
            max_run = int(rp.get("max_runtime_minutes", 720))
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
        self.canvas.bind("<Button-3>", self._on_right_click)
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

    def clear_current(self):
        """Drop the blue 'currently processing' highlight (called when a run
        ends). Without this the last image keeps its blue frame even though it is
        finished — it should show its own green/red outcome frame instead."""
        if self._current is None:
            return
        last = self._current
        self._current = None
        self._draw_highlight()          # _current is None now → removes the rect
        if last in self._pos:           # reveal the finished image's outcome frame
            self._draw_frame(last)

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

    def _path_at(self, event):
        """Map a canvas click to the queued image path under it, or None for
        empty space. Shared by double-click-to-open and the right-click menu."""
        x = self.canvas.canvasx(event.x)
        y = self.canvas.canvasy(event.y)
        col = int((x - GRID_GAP) // (self._cell + GRID_GAP))
        row = int((y - GRID_GAP) // (self._cell + GRID_GAP))
        if col < 0 or col >= self._cols:
            return None
        idx = row * self._cols + col
        if not (0 <= idx < len(self._order)):
            return None
        return self._order[idx]

    def _on_double(self, event):
        path = self._path_at(event)
        if not path:
            return
        # A comparable (green) thumbnail with a known upscaled output opens the
        # comparison window; anything else just opens the file.
        out = self._compare.get(path)
        if self.on_compare and out and os.path.exists(out) and os.path.exists(path):
            self.on_compare(path, out)
        else:
            self._open(path)

    def _on_right_click(self, event):
        """Context menu over a thumbnail. Entries depend on the image's outcome:
        a failed image offers only its folder; a processed (green) image offers
        the original, the upscaled counterpart, their folders and Compare; an
        unprocessed/processing image offers just open-image / open-folder."""
        path = self._path_at(event)
        if not path:
            return
        status   = self._status.get(path)
        upscaled = self._compare.get(path)
        menu = tk.Menu(self, tearoff=0)
        if status == "fail":
            menu.add_command(label="Open failed image folder",
                             command=lambda p=path: self._open_folder(p))
        elif status == "ok" and upscaled:
            menu.add_command(label="Open original image",
                             command=lambda p=path: self._open(p))
            menu.add_command(label="Open original image folder",
                             command=lambda p=path: self._open_folder(p))
            menu.add_command(label="Open upscaled image",
                             command=lambda p=upscaled: self._open(p))
            menu.add_command(label="Open upscaled image folder",
                             command=lambda p=upscaled: self._open_folder(p))
            menu.add_separator()
            can_compare = bool(self.on_compare and os.path.exists(upscaled)
                               and os.path.exists(path))
            menu.add_command(
                label="Compare images",
                command=lambda p=path, o=upscaled: self.on_compare(p, o),
                state=("normal" if can_compare else "disabled"))
        else:
            # Unprocessed or currently processing — no outcome recorded yet (and
            # tag-only "ok" with no upscaled counterpart falls here too).
            menu.add_command(label="Open image",
                             command=lambda p=path: self._open(p))
            menu.add_command(label="Open image folder",
                             command=lambda p=path: self._open_folder(p))
        # Clipboard helpers, useful in every state (the thumbnail's source path).
        menu.add_separator()
        menu.add_command(label="Copy path",
                         command=lambda p=path: self._to_clipboard(p))
        menu.add_command(label="Copy filename",
                         command=lambda p=path: self._to_clipboard(os.path.basename(p)))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _to_clipboard(self, text):
        """Put `text` on the clipboard (so it survives after the app closes,
        Tk owns the selection while running). Fail-safe."""
        try:
            self.clipboard_clear()
            self.clipboard_append(text)
        except tk.TclError:
            pass

    @staticmethod
    def _open(path):
        if os.path.exists(path):
            try:
                os.startfile(path)
            except OSError:
                pass

    @staticmethod
    def _open_folder(path):
        """Open the folder holding `path` in Explorer with the file selected;
        fall back to just opening the folder. Fail-safe (best effort)."""
        if not path:
            return
        norm = os.path.normpath(path)
        if os.path.exists(norm):
            try:
                subprocess.Popen(["explorer", f"/select,{norm}"],
                                 creationflags=CREATE_NO_WINDOW)
                return
            except Exception:
                pass
        folder = os.path.dirname(norm)
        if os.path.isdir(folder):
            try:
                os.startfile(folder)
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

        self._build_remote_row(row=3)
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

    def on_exit(self, code):
        self._set_running(False)
        self._paused = False
        self.pause_btn.configure(text="Pause")
        self.eta_var.set("—")
        # The remote-pod telemetry row only makes sense during a remote run.
        if self.remote_telemetry_row.winfo_manager():
            self.remote_telemetry_row.grid_remove()
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
        # Remote toggle defaults ON for a Remote-only install (the only way to tag
        # there — no local Ollama/torch); OFF for Local/Both.
        self.remote_var = tk.BooleanVar(value=(get_install_mode() == "remote"))
        # Live GPU picker: the vision model needs only ~6.6 GB, so a 16 GB floor
        # is plenty; the persisted preference is runpod.tag_gpu_type_id.
        self._gpu_min_vram = 16
        self._gpu_pref_key = "tag_gpu_type_id"
        self._bench_task   = "tag"       # $/100 readout: tag benchmark rows
        self.scope_var  = tk.StringVar(value=UNDO_SCOPES[0][0])
        self._mode      = "tag"          # "tag" | "undo" — for the exit message
        self._build()

        # Restore the pinned default folder from config.json
        self.restore_defaults_if_empty()
        self.dir_var.trace_add("write", lambda *_: self._refresh_dir_buttons())
        self._refresh_dir_buttons()

    def restore_defaults_if_empty(self):
        if not self.dir_var.get().strip():
            tag_default = get_default_folder("tag_folder")
            if tag_default and os.path.isdir(tag_default):
                self.dir_var.set(tag_default)

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

        # "Run on remote pod" + the live GPU picker share a dedicated row. The
        # checkbox tooltip is set here (overriding the generic one) because remote
        # tagging is the unusual case: tagging runs locally, only the model calls
        # go over the tunnel.
        self._build_remote_row(row=3)
        Tooltip(self.remote_chk,
                "Run the vision model (and the auto-straighten CNN) on a rented "
                "RunPod GPU instead of this PC (roadmap #1, experimental). Tagging "
                "itself runs locally; only the model calls go over an SSH tunnel. "
                "Creates a billed pod and terminates it when done. Needs a RunPod "
                "API key + model volume in the RunPod tab.")

        undo = ttk.LabelFrame(self, text=" Undo previous runs ", padding=(8, 4))
        undo.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(12, 0))
        ttk.Label(undo, text="Scope:").pack(side="left", padx=(0, 4))
        ttk.Combobox(undo, textvariable=self.scope_var, state="readonly", width=18,
                     values=[label for label, _ in UNDO_SCOPES]).pack(side="left", padx=(0, 16))
        self.undo_btn = ttk.Button(undo, text="Undo this folder…", command=self._undo)
        self.undo_btn.pack(side="left")

        self._build_output_area(row=5)

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
        remote = bool(self.remote_var.get())
        extra_env = None
        if remote:
            rp = CFG.get("runpod", {})
            if not rp.get("api_key") or not rp.get("network_volume_id"):
                messagebox.showwarning(
                    APP_TITLE,
                    "Remote tagging needs a RunPod API key and a model volume.\n"
                    "Set them in the RunPod tab.")
                return
            ok_ssh, ssh_info = ssh_setup.setup(
                os.path.expandvars(rp.get("ssh_key_path", "")) or None)
            if not ok_ssh:
                messagebox.showwarning(APP_TITLE, ssh_info.get("message", "SSH setup failed."))
                return
            gpu_chain = self._selected_gpu_chain()
            if gpu_chain == []:
                messagebox.showwarning(
                    APP_TITLE,
                    "No RunPod GPU with at least 16 GB of VRAM is available in your "
                    "volume's region right now.\n\nTry again in a while, or choose a "
                    "different region for your model volume in Settings. (Press ↻ to "
                    "re-check availability.)")
                return
            if not self.confirm_deadman_safety():
                return
            gpu_note = self._gpu_confirm_note(gpu_chain)
            if not messagebox.askyesno(
                    APP_TITLE,
                    "Run Tag & Rename on a rented RunPod GPU?\n\n"
                    "This creates a BILLED pod running the vision model, tags each "
                    "image against it (the files are still read/written here), and "
                    "terminates the pod when the run ends. The pod also "
                    f"self-terminates on an idle / max-runtime deadline.{gpu_note}\n\n"
                    "Proceed?"):
                return
            extra_env = {"IMGTBX_TAG_REMOTE": "1"}
            if gpu_chain:
                extra_env["IMGTBX_GPU_OVERRIDE"] = ",".join(gpu_chain)
        else:
            # A Remote-only install has no local Ollama or torch — refuse a local
            # tag run with a clear message instead of crashing on a missing model.
            if get_install_mode() == "remote":
                messagebox.showwarning(
                    APP_TITLE,
                    "This is a Remote-only install — the local vision model "
                    "(Ollama) and PyTorch aren't installed.\n\n"
                    "Tick 'Run on remote pod (RunPod)' to tag on a rented GPU, or "
                    "reinstall and choose Local or Both to tag on this PC.")
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
        self._snapshot_cost_run(remote)
        self.eta_var.set("calculating…")
        self.status_var.set(
            "Starting the remote pod (first run takes a few minutes) …" if remote
            else "Starting — checking Ollama and scanning the folder …")
        if self.launch("tag_and_rename.py", args, extra_env=extra_env):
            self._remote_run = remote
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
        self._remote_run = False    # undo is always local file I/O
        self.progress.set(0)
        self._reset_stream_state()
        self.status_var.set("Undoing previous changes …")
        if self.launch("tag_and_rename.py", args):
            self._set_running(True)

    def _resume(self):
        self.send("r")
        self.status_var.set("Resuming …")

    def _stop(self):
        if getattr(self, "_remote_run", False):
            idle = int(CFG.get("runpod", {}).get("idle_timeout_minutes", 15))
            # Yes → stop the pod now · No → leave it running · Cancel → keep going.
            ans = messagebox.askyesnocancel(
                APP_TITLE,
                "The tagging run will stop after the current image.\n\n"
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
            self.status_var.set(self._failure_status(code))
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
                   "auto_straighten", "straighten_min_confidence",
                   "watchdog_enabled", "watchdog_factor", "watchdog_consecutive",
                   "watchdog_min_samples",
                   # These are intentionally hidden from the UI: the defaults are
                   # the right ones and a non-technical user can't meaningfully
                   # change them (DiT/VAE model = the 7B FP16 + FP16 VAE combo;
                   # color_correction = LAB; blocks_to_swap is a VRAM/speed lever
                   # that only matters on a VRAM-starved card). The values stay in
                   # config.json (merged on save) for advanced hand-edits.
                   "dit_model", "vae_model",
                   "color_correction", "blocks_to_swap",
                   # Resident-offload threshold: a numeric tuning knob (default
                   # 40 GB, see upscale_engine) that no end user should touch;
                   # config.json only.
                   "vram_resident_threshold_gb"}

# Friendly labels for the generic SeedVR fields.
_SEEDVR_LABELS = {
    "attention_mode":   "Attention mode",
    "encode_tiled":     "VAE encode tiled",
    "decode_tiled":     "VAE decode tiled",
    "encode_tile_size": "Encode tile size",
    "decode_tile_size": "Decode tile size",
    "outage_threshold": "Outage threshold",
}

# Suggested values for the free-text enum fields (editable — type anything).
_SEEDVR_CHOICES = {
    "attention_mode":   ["sdpa", "flash_attn", "sage"],
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


# Video deliverable codec choices (label -> video_backend, use_10bit). opencv is
# SeedVR2's default writer (MPEG-4 / mp4v, most compatible); the ffmpeg backend
# enables H.265 10-bit (docs/video-upscaler.md 6.4 / 14).
_VIDEO_CODEC_OPTIONS = [
    ("Standard — MPEG-4 (most compatible)", "opencv", False),
    ("High quality — H.265 10-bit (ffmpeg)", "ffmpeg", True),
]


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
        self.default_vsrc_var = tk.StringVar(value=defs.get("video_source", ""))
        self.default_vout_var = tk.StringVar(value=defs.get("video_output", ""))
        for r, (text, var) in enumerate((
                ("Batch Upscaler — Photo folder:",  self.default_src_var),
                ("Batch Upscaler — Output folder:", self.default_out_var),
                ("Tag & Rename — Photo folder:",    self.default_tag_var),
                ("Conciliation — Original folder:",  self.default_corig_var),
                ("Conciliation — Processed folder:", self.default_cproc_var),
                ("Video Upscaler — Video folder:",   self.default_vsrc_var),
                ("Video Upscaler — Output folder:",  self.default_vout_var))):
            ttk.Label(sec, text=text).grid(row=r, column=0, sticky="w", pady=3)
            ttk.Entry(sec, textvariable=var).grid(row=r, column=1, sticky="ew", padx=6, pady=3)
            ttk.Button(sec, text="Browse…",
                       command=lambda v=var: self._pick_folder(v)).grid(row=r, column=2, pady=3)

        # ── Ollama ────────────────────────────────────────────────────────────
        sec = self._section(body, "Ollama")
        sec.columnconfigure(1, weight=1)

        # URL + model picker + a single Check on one row. "Check" both verifies
        # the URL is reachable AND refreshes the model list (the two used to be
        # separate buttons). The model combobox is read-only (pick from what's
        # actually installed) and ~130 px wide.
        ttk.Label(sec, text="Ollama URL:").grid(row=0, column=0, sticky="w", pady=3)
        self.ollama_url_var = tk.StringVar(value=ollama.get("url", "http://127.0.0.1:11434"))
        ttk.Entry(sec, textvariable=self.ollama_url_var).grid(row=0, column=1, sticky="ew", padx=6, pady=3)
        ttk.Label(sec, text="Model:").grid(row=0, column=2, sticky="e", padx=(6, 4), pady=3)
        self.ollama_model_var = tk.StringVar(value=ollama.get("model", "qwen2.5vl:7b"))
        self.ollama_model_cmb = ttk.Combobox(sec, textvariable=self.ollama_model_var,
                                             state="readonly", width=16)
        self.ollama_model_cmb.grid(row=0, column=3, sticky="w", pady=3)
        Tooltip(self.ollama_model_cmb,
                "Vision model used for tagging. Pick from the models installed on "
                "the Ollama server; press Check to (re)load the list.")
        ttk.Button(sec, text="Check", command=self._check_ollama).grid(
            row=0, column=4, padx=(8, 0), pady=3)

        self.ollama_status = ttk.Label(sec, text="", foreground="#666")
        self.ollama_status.grid(row=1, column=0, columnspan=5, sticky="w", padx=6, pady=(4, 0))

        # ── Tag & Rename ───────────────────────────────────────────────────────
        sec = self._section(body, "Tag & Rename")

        # All Tag & Rename settings on one row.
        strip = ttk.Frame(sec)
        strip.grid(row=0, column=0, sticky="w", pady=3)

        self.straighten_var = tk.BooleanVar(value=bool(tag.get("auto_straighten", True)))
        chk = ttk.Checkbutton(strip, text="Auto-straighten rotated photos",
                              variable=self.straighten_var)
        chk.pack(side="left")
        Tooltip(chk, "Detects sideways photos and rotates them upright before tagging. "
                     "Only confident calls are acted on; ambiguous ones are left alone.")

        ttk.Label(strip, text="Confidence threshold:").pack(side="left", padx=(18, 4))
        self.straighten_conf_var = tk.DoubleVar(
            value=float(tag.get("straighten_min_confidence", 0.9)))
        spin = ttk.Spinbox(strip, from_=0.50, to=1.00, increment=0.05, width=6, format="%.2f",
                           textvariable=self.straighten_conf_var)
        spin.pack(side="left")
        Tooltip(spin, "0.50–1.00   (higher = fewer, safer rotations)")

        ttk.Label(strip, text="Max image size sent to model:").pack(side="left", padx=(18, 4))
        self.tag_maxpx_var = tk.IntVar(value=int(tag.get("max_image_px", 1280)))
        maxpx_spin = ttk.Spinbox(strip, from_=0, to=4096, increment=128, width=6,
                                 textvariable=self.tag_maxpx_var)
        maxpx_spin.pack(side="left")
        ttk.Label(strip, text="px").pack(side="left", padx=(4, 0))
        Tooltip(maxpx_spin,
                "The image is downscaled to this longest edge (in pixels) before "
                "going to the vision model (your source files are never changed). "
                "Large photos otherwise OOM small-VRAM GPUs into an HTTP 400 — "
                "1280 px is plenty for describing and titling. Higher = more detail "
                "+ more VRAM. 0 = full resolution (not recommended).")

        # ── Upscaling targets ──────────────────────────────────────────────────
        sec = self._section(body, "Upscaling")

        # Two aligned columns on a shared grid: column 0 holds the Resolution
        # Target and the two checkboxes; column 1 (Skip images over / Confidence
        # threshold / Slowdown factor) lines up vertically because column 0 sizes
        # to its widest item (the watchdog checkbox), so column 1's left edge is
        # the same on every row.
        c0 = ttk.Frame(sec)
        c0.grid(row=0, column=0, sticky="w", pady=3)
        ttk.Label(c0, text="Resolution Target:").pack(side="left", padx=(0, 4))
        self.restarget_var = tk.StringVar(value=self._current_preset_label(ups))
        restarget_cmb = ttk.Combobox(c0, textvariable=self.restarget_var, state="readonly",
                                     values=[p[0] for p in RESOLUTION_PRESETS], width=16)
        restarget_cmb.pack(side="left")
        Tooltip(restarget_cmb, "longer edge / shorter edge, in pixels")

        skip = ttk.Frame(sec)
        skip.grid(row=0, column=1, sticky="w", padx=(18, 0), pady=3)
        ttk.Label(skip, text="Skip images over:").pack(side="left", padx=(0, 4))
        self.cutoff_var = tk.IntVar(value=int(ups.get("upscale_cutoff_pct", 66)))
        cut_spin = ttk.Spinbox(skip, from_=0, to=99, width=4, textvariable=self.cutoff_var)
        cut_spin.pack(side="left")
        ttk.Label(skip, text="% of target resolution").pack(side="left", padx=(4, 0))
        Tooltip(cut_spin, "Percentage of the target resolution.   (0 = upscale everything eligible)")

        self.up_straighten_var = tk.BooleanVar(value=bool(ups.get("auto_straighten", True)))
        up_chk = ttk.Checkbutton(sec, text="Auto-straighten photos",
                                 variable=self.up_straighten_var)
        up_chk.grid(row=1, column=0, sticky="w", pady=3)
        Tooltip(up_chk, "Rotates a sideways photo upright BEFORE upscaling so the result still "
                        "fits a 4K screen. Without this, the upscaler targets the wrong axis and "
                        "the image no longer fits once Tag & Rename straightens it. The source is "
                        "never modified (a temp copy is rotated and upscaled).")
        conf = ttk.Frame(sec)
        conf.grid(row=1, column=1, sticky="w", padx=(18, 0), pady=3)
        ttk.Label(conf, text="Confidence threshold:").pack(side="left", padx=(0, 4))
        self.up_straighten_conf_var = tk.DoubleVar(
            value=float(ups.get("straighten_min_confidence", 0.9)))
        up_spin = ttk.Spinbox(conf, from_=0.50, to=1.00, increment=0.05, width=6, format="%.2f",
                              textvariable=self.up_straighten_conf_var)
        up_spin.pack(side="left")
        Tooltip(up_spin, "0.50–1.00   (higher = fewer, safer rotations)")

        self.watchdog_var = tk.BooleanVar(value=bool(ups.get("watchdog_enabled", True)))
        wd_chk = ttk.Checkbutton(
            sec, text="Performance watchdog (auto-stop)",
            variable=self.watchdog_var)
        wd_chk.grid(row=2, column=0, sticky="w", pady=3)
        Tooltip(wd_chk,
                "Watches per-image upscale time. If it degrades to a sustained multiple of the "
                "normal speed (the GPU thrashing VRAM into system RAM) OR hits a hard out-of-memory "
                "error, the run auto-stops after the current image and you're notified (log, Discord, "
                "taskbar flash). The resume cache continues the queue after you reboot — the known cure.")
        wd = ttk.Frame(sec)
        wd.grid(row=2, column=1, sticky="w", padx=(18, 0), pady=3)
        ttk.Label(wd, text="Slowdown factor:").pack(side="left", padx=(0, 4))
        self.watchdog_factor_var = tk.DoubleVar(value=float(ups.get("watchdog_factor", 3.0)))
        wf_spin = ttk.Spinbox(wd, from_=1.5, to=10.0, increment=0.5, width=6, format="%.1f",
                              textvariable=self.watchdog_factor_var)
        wf_spin.pack(side="left")
        Tooltip(wf_spin, "How many times slower than the run's healthy rate (per megapixel) "
                         "counts as 'slow' (e.g. 3×).")
        ttk.Label(wd, text="for").pack(side="left", padx=(12, 4))
        self.watchdog_consec_var = tk.IntVar(value=int(ups.get("watchdog_consecutive", 2)))
        wc_spin = ttk.Spinbox(wd, from_=1, to=10, width=4, textvariable=self.watchdog_consec_var)
        wc_spin.pack(side="left")
        ttk.Label(wd, text="images in a row").pack(side="left", padx=(4, 0))
        Tooltip(wc_spin, "Consecutive slow images before stopping (filters out a single odd image).")

        # ── SeedVR Settings (everything else in the upscale block) ──────────────
        sec = self._section(body, "SeedVR Settings")
        present = {k: v for k, v in ups.items() if k not in _SEEDVR_EXCLUDE}

        def _lbl(key):
            return _SEEDVR_LABELS.get(key, key.replace("_", " ").capitalize())

        placed = set()

        # Goal: fit every SeedVR control on ONE row even at the app's minimum
        # width. So they live in a single tight, left-packed strip with short
        # group labels and narrow controls, rather than the old two-row grid.
        strip = ttk.Frame(sec)
        strip.grid(row=0, column=0, sticky="w", pady=3)

        def _grp(text, pad_left):
            ttk.Label(strip, text=text).pack(side="left", padx=(pad_left, 4))

        if "attention_mode" in present:
            _grp("Attention mode:", 0)
            self._make_seedvr_control(strip, "attention_mode", present["attention_mode"],
                                      width=9, readonly=True).pack(side="left")
            placed.add("attention_mode")

        if "outage_threshold" in present:
            _grp("Outage threshold:", 14)
            self._make_seedvr_control(strip, "outage_threshold", present["outage_threshold"],
                                      width=3).pack(side="left")
            placed.add("outage_threshold")

        # VAE Tiled: two checkboxes whose own labels read Encode / Decode.
        if "encode_tiled" in present or "decode_tiled" in present:
            _grp("VAE Tiled:", 14)
            if "encode_tiled" in present:
                self._make_seedvr_control(strip, "encode_tiled", present["encode_tiled"],
                                          text="Encode").pack(side="left")
                placed.add("encode_tiled")
            if "decode_tiled" in present:
                self._make_seedvr_control(strip, "decode_tiled", present["decode_tiled"],
                                          text="Decode").pack(side="left", padx=(6, 0))
                placed.add("decode_tiled")

        # Tile Size: two narrow spinboxes labelled Encode / Decode.
        if "encode_tile_size" in present or "decode_tile_size" in present:
            _grp("Tile Size:", 14)
            if "encode_tile_size" in present:
                _grp("Encode", 0)
                self._make_seedvr_control(strip, "encode_tile_size",
                                          present["encode_tile_size"], width=6).pack(side="left")
                placed.add("encode_tile_size")
            if "decode_tile_size" in present:
                _grp("Decode", 8)
                self._make_seedvr_control(strip, "decode_tile_size",
                                          present["decode_tile_size"], width=6).pack(side="left")
                placed.add("decode_tile_size")

        # Any unrecognised keys fall back to one-per-row generic controls.
        row = 1
        for key, value in present.items():
            if key in placed:
                continue
            ttk.Label(sec, text=f"{_lbl(key)}:").grid(row=row, column=0, sticky="w", pady=3, padx=(0, 4))
            self._make_seedvr_control(sec, key, value).grid(row=row, column=1, sticky="w", pady=3)
            row += 1

        # ── Video Upscaler (#2) ───────────────────────────────────────────────────
        vid = CFG.get("video", {})
        sec = self._section(body, "Video Upscaler")
        sec.columnconfigure(1, weight=1)
        ttk.Label(sec, text="Default target:").grid(row=0, column=0, sticky="w", pady=3)
        self.video_target_var = tk.StringVar(value=vid.get("target", "1080p"))
        ttk.Combobox(sec, textvariable=self.video_target_var, state="readonly",
                     width=10, values=["1080p", "1440p", "4K"]).grid(
            row=0, column=1, sticky="w", padx=6, pady=3)
        ttk.Label(sec, text="Output subfolder:").grid(row=1, column=0, sticky="w", pady=3)
        self.video_outsub_var = tk.StringVar(value=vid.get("output_subdir", "__upscaled__"))
        ttk.Entry(sec, textvariable=self.video_outsub_var, width=20).grid(
            row=1, column=1, sticky="w", padx=6, pady=3)
        ttk.Label(sec, text="Output quality:").grid(row=2, column=0, sticky="w", pady=3)
        self.video_codec_var = tk.StringVar(value=self._video_codec_label(vid))
        ttk.Combobox(sec, textvariable=self.video_codec_var, state="readonly",
                     width=34, values=[lbl for lbl, _b, _t in _VIDEO_CODEC_OPTIONS]).grid(
            row=2, column=1, sticky="w", padx=6, pady=3)
        self.video_confirm_var = tk.BooleanVar(value=bool(vid.get("confirm_before_rent", True)))
        ttk.Checkbutton(sec, text="Confirm (show the cost estimate) before renting a pod",
                        variable=self.video_confirm_var).grid(
            row=3, column=0, columnspan=2, sticky="w", pady=(4, 0))
        Tooltip(sec, "The Video Upscaler runs only on a rented RunPod GPU. The "
                     "output subfolder mirrors the source tree (like Batch Upscaler).")

        # ── Notifications ───────────────────────────────────────────────────────
        # Settings live in the "notifications" config section; resolve_settings()
        # also reads the legacy upscale.discord_webhook_url so existing installs
        # keep their webhook until the next Save migrates it.
        notif = notifications.resolve_settings(CFG)
        sec = self._section(body, "Notifications")
        sec.columnconfigure(1, weight=1)

        ttk.Label(sec, text="Discord webhook:").grid(row=0, column=0, sticky="w", pady=3)
        self.webhook_var = tk.StringVar(value=notif.get("discord_webhook_url", ""))
        webhook_entry = ttk.Entry(sec, textvariable=self.webhook_var)
        webhook_entry.grid(row=0, column=1, sticky="ew", padx=6, pady=3)
        Tooltip(webhook_entry,
                "Optional. Notifies when a queue finishes or on errors. Leave empty to disable.")
        ttk.Button(sec, text="Test", command=self._test_webhook).grid(row=0, column=2, pady=3)
        self.webhook_status = ttk.Label(sec, text="", foreground="#666")
        self.webhook_status.grid(row=1, column=1, columnspan=2, sticky="w", padx=6)

        ttk.Label(sec, text="Telegram bot token:").grid(row=2, column=0, sticky="w", pady=3)
        self.tg_token_var = tk.StringVar(value=notif.get("telegram_bot_token", ""))
        tg_token_entry = ttk.Entry(sec, textvariable=self.tg_token_var)
        tg_token_entry.grid(row=2, column=1, sticky="ew", padx=6, pady=3)
        Tooltip(tg_token_entry,
                "Optional. In Telegram, create a bot with @BotFather and paste the "
                "token it gives you here. Leave empty to disable Telegram alerts.")
        ttk.Button(sec, text="Test", command=self._test_telegram).grid(row=2, column=2, pady=3)

        ttk.Label(sec, text="Telegram chat ID:").grid(row=3, column=0, sticky="w", pady=3)
        self.tg_chat_var = tk.StringVar(value=notif.get("telegram_chat_id", ""))
        tg_chat_entry = ttk.Entry(sec, textvariable=self.tg_chat_var)
        tg_chat_entry.grid(row=3, column=1, sticky="ew", padx=6, pady=3)
        Tooltip(tg_chat_entry,
                "Where to send alerts. Open your bot in Telegram and press Start "
                "(or send it any message), then click Detect to fill this in.")
        ttk.Button(sec, text="Detect", command=self._detect_telegram).grid(row=3, column=2, pady=3)
        self.tg_status = ttk.Label(sec, text="", foreground="#666")
        self.tg_status.grid(row=4, column=1, columnspan=2, sticky="w", padx=6)

        ttk.Label(sec, text="ntfy server:").grid(row=5, column=0, sticky="w", pady=3)
        self.ntfy_server_var = tk.StringVar(value=notif.get("ntfy_server", "https://ntfy.sh"))
        ntfy_server_entry = ttk.Entry(sec, textvariable=self.ntfy_server_var)
        ntfy_server_entry.grid(row=5, column=1, sticky="ew", padx=6, pady=3)
        Tooltip(ntfy_server_entry,
                "The ntfy server to publish to. Leave as https://ntfy.sh for the "
                "free public server, or point it at your own self-hosted ntfy.")

        ttk.Label(sec, text="ntfy topic:").grid(row=6, column=0, sticky="w", pady=3)
        self.ntfy_topic_var = tk.StringVar(value=notif.get("ntfy_topic", ""))
        ntfy_topic_entry = ttk.Entry(sec, textvariable=self.ntfy_topic_var)
        ntfy_topic_entry.grid(row=6, column=1, sticky="ew", padx=6, pady=3)
        Tooltip(ntfy_topic_entry,
                "A topic name you make up, then subscribe to in the ntfy app. On the "
                "public server anyone who knows the topic can read it, so pick an "
                "unguessable name. Leave empty to disable ntfy alerts.")
        ttk.Button(sec, text="Test", command=self._test_ntfy).grid(row=6, column=2, pady=3)
        self.ntfy_status = ttk.Label(sec, text="", foreground="#666")
        self.ntfy_status.grid(row=7, column=1, columnspan=2, sticky="w", padx=6)

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

        # Broker host with a fixed "mqtt://" hint so it's clear only the hostname
        # or IP goes in the field (the scheme/port aren't typed here).
        ttk.Label(sec, text="Broker host:").grid(row=1, column=0, sticky="w", pady=3)
        hostrow = ttk.Frame(sec)
        hostrow.grid(row=1, column=1, sticky="ew", padx=6, pady=3)
        ttk.Label(hostrow, text="mqtt://").pack(side="left")
        ttk.Entry(hostrow, textvariable=self.mqtt_host_var).pack(side="left", fill="x", expand=True)
        ttk.Label(sec, text="Port:").grid(row=1, column=2, sticky="e", pady=3)
        ttk.Spinbox(sec, from_=1, to=65535, width=7,
                    textvariable=self.mqtt_port_var).grid(row=1, column=3, sticky="w", padx=6, pady=3)

        # Username, Password, Test and Publish now share one row. Client ID has no
        # control (mqtt_cid_var still carries it through to config.json) — it's an
        # advanced field a non-technical user never needs; edit config.json to change.
        urow = ttk.Frame(sec)
        urow.grid(row=2, column=0, columnspan=4, sticky="w", pady=3)
        ttk.Label(urow, text="Username:").pack(side="left")
        ttk.Entry(urow, textvariable=self.mqtt_user_var, width=16).pack(side="left", padx=(4, 0))
        ttk.Label(urow, text="Password:").pack(side="left", padx=(12, 0))
        ttk.Entry(urow, textvariable=self.mqtt_pass_var, show="•", width=16).pack(side="left", padx=(4, 0))
        self.mqtt_test_btn = ttk.Button(urow, text="Test", command=self._test_mqtt)
        self.mqtt_test_btn.pack(side="left", padx=(12, 0))
        ttk.Button(urow, text="Publish now", command=self._publish_mqtt).pack(side="left", padx=(6, 0))

        self.mqtt_status = ttk.Label(sec, text="", foreground="#666")
        self.mqtt_status.grid(row=3, column=0, columnspan=4, sticky="w", padx=6, pady=(4, 0))

        # ── Updates (kept last: rarely changed, low priority) ────────────────────
        sec = self._section(body, "Updates")
        row = ttk.Frame(sec)
        row.grid(row=0, column=0, sticky="w", pady=3)
        ttk.Label(row, text=f"Current version: {APP_VERSION}").pack(side="left")
        self.auto_update_var = tk.BooleanVar(value=update_auto_check_enabled())
        ttk.Checkbutton(row, text="Check for updates on startup",
                        variable=self.auto_update_var).pack(side="left", padx=(18, 0))
        self.check_update_btn = ttk.Button(
            row, text="Check for updates now", command=self._check_updates)
        self.check_update_btn.pack(side="left", padx=(18, 0))
        self.update_status = ttk.Label(sec, text="", foreground="#666")
        self.update_status.grid(row=1, column=0, sticky="w", padx=6, pady=(4, 0))

        # ── Save bar ────────────────────────────────────────────────────────────
        bar = ttk.Frame(body, padding=(8, 12))
        bar.pack(fill="x")
        ttk.Button(bar, text="Save settings", command=self._save).pack(side="left")
        self.save_status = ttk.Label(bar, text="", foreground="#666")
        self.save_status.pack(side="left", padx=12)

        # Baseline for unsaved-changes detection: a snapshot of the form as it
        # mirrors config.json right now. Re-taken on every successful save/revert.
        self._baseline = self._snapshot()

        # Live unsaved-changes indicator. Rather than wire a trace onto dozens of
        # heterogeneous vars (entries, spinboxes, comboboxes), poll is_dirty() on a
        # light timer: "Not saved" (red) the moment any field differs from the saved
        # state, "Saved." (green) right after a successful save, blank when clean and
        # unsaved-this-session. `_save_status_base` is what to show when clean;
        # `_save_status_hold` lets a transient message (e.g. a write error) linger.
        self._save_status_base = ""
        self._save_status_hold = 0.0
        self._refresh_save_indicator()

        # Probe the saved Ollama URL in the background so the status text already
        # reflects reachability by the time the user opens the Settings tab.
        self._check_ollama()

    def _make_seedvr_control(self, parent, key, value, width=None, readonly=False, text=None):
        """Build the editable control for a SeedVR field and register its var.
        Returns the widget (caller positions it). `width` overrides the default
        char-width; `readonly` makes a combobox pick-only; `text` labels a
        checkbutton (so the field's own word sits beside the box)."""
        if isinstance(value, bool):
            var = tk.BooleanVar(value=value)
            widget = ttk.Checkbutton(parent, variable=var, text=text or "")
            self._seedvr_vars[key] = (var, bool)
        elif isinstance(value, int):
            var = tk.StringVar(value=str(value))
            widget = ttk.Spinbox(parent, from_=0, to=100000,
                                 width=width if width is not None else 8, textvariable=var)
            self._seedvr_vars[key] = (var, int)
        else:
            var = tk.StringVar(value=str(value))
            if key in _SEEDVR_CHOICES:
                widget = ttk.Combobox(parent, textvariable=var,
                                      values=_SEEDVR_CHOICES[key],
                                      state="readonly" if readonly else "normal",
                                      width=width if width is not None else 16)
            else:
                widget = ttk.Entry(parent, textvariable=var,
                                  width=width if width is not None else 22)
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

    def _test_webhook(self):
        ok, msg = test_discord_webhook(self.webhook_var.get())
        self.webhook_status.configure(text=msg, foreground="#1a7f37" if ok else "#b3261e")

    def _detect_telegram(self):
        """Read the bot's recent updates and fill in the chat ID. The user must
        have pressed Start (or messaged the bot) first."""
        self.tg_status.configure(text="Detecting chat…", foreground="#666")
        self.tg_status.update_idletasks()
        chat_id, msg = notifications.detect_telegram_chat(self.tg_token_var.get())
        if chat_id:
            self.tg_chat_var.set(chat_id)
        self.tg_status.configure(text=msg, foreground="#1a7f37" if chat_id else "#b3261e")

    def _test_telegram(self):
        """Verify the token and send a test message to the configured chat."""
        self.tg_status.configure(text="Testing…", foreground="#666")
        self.tg_status.update_idletasks()
        ok, msg = notifications.test_telegram(self.tg_token_var.get(), self.tg_chat_var.get())
        self.tg_status.configure(text=msg, foreground="#1a7f37" if ok else "#b3261e")

    def _test_ntfy(self):
        """Publish a test message to the configured ntfy topic. The auth token (if
        any, for a self-hosted server) is config-only — read it from CFG."""
        self.ntfy_status.configure(text="Testing…", foreground="#666")
        self.ntfy_status.update_idletasks()
        token = CFG.get("notifications", {}).get("ntfy_token", "")
        ok, msg = notifications.test_ntfy(
            self.ntfy_server_var.get(), self.ntfy_topic_var.get(), token)
        self.ntfy_status.configure(text=msg, foreground="#1a7f37" if ok else "#b3261e")

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
        self.default_vsrc_var.set(defs.get("video_source", ""))
        self.default_vout_var.set(defs.get("video_output", ""))

    def _video_codec_label(self, vid):
        """The codec combobox label matching the saved video_backend/use_10bit."""
        backend = vid.get("video_backend", "opencv")
        ten = bool(vid.get("use_10bit", False))
        for lbl, b, t in _VIDEO_CODEC_OPTIONS:
            if b == backend and t == ten:
                return lbl
        return _VIDEO_CODEC_OPTIONS[0][0]

    def _video_section(self):
        """The proposed `video` config block from the form. Only the exposed keys
        are set; config-only keys (segment_seconds, spin_up_seconds, …) are left
        untouched by _save's per-section update()."""
        backend, ten = _VIDEO_CODEC_OPTIONS[0][1], _VIDEO_CODEC_OPTIONS[0][2]
        for lbl, b, t in _VIDEO_CODEC_OPTIONS:
            if lbl == self.video_codec_var.get():
                backend, ten = b, t
                break
        return {
            "target":              self.video_target_var.get(),
            "output_subdir":       self.video_outsub_var.get().strip() or "__upscaled__",
            "video_backend":       backend,
            "use_10bit":           ten,
            "confirm_before_rent": bool(self.video_confirm_var.get()),
        }

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
        ups["auto_straighten"] = bool(self.up_straighten_var.get())
        try:
            up_conf = round(float(self.up_straighten_conf_var.get()), 2)
        except (ValueError, tk.TclError):
            up_conf = 0.9
        ups["straighten_min_confidence"] = min(1.0, max(0.5, up_conf))
        ups["watchdog_enabled"] = bool(self.watchdog_var.get())
        try:
            wd_factor = round(float(self.watchdog_factor_var.get()), 1)
        except (ValueError, tk.TclError):
            wd_factor = 3.0
        ups["watchdog_factor"] = min(10.0, max(1.5, wd_factor))
        try:
            ups["watchdog_consecutive"] = max(1, min(10, int(self.watchdog_consec_var.get())))
        except (ValueError, tk.TclError):
            ups["watchdog_consecutive"] = 2
        ups.update(seedvr_out)

        try:
            conf = round(float(self.straighten_conf_var.get()), 2)
        except (ValueError, tk.TclError):
            conf = 0.9
        try:
            max_px = max(0, int(self.tag_maxpx_var.get()))
        except (ValueError, tk.TclError):
            max_px = 1280

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
                "video_source":  self.default_vsrc_var.get().strip(),
                "video_output":  self.default_vout_var.get().strip(),
            },
            "tagging": {
                "auto_straighten": bool(self.straighten_var.get()),
                "straighten_min_confidence": min(1.0, max(0.5, conf)),
                "max_image_px": max_px,
            },
            "video": self._video_section(),
            "updates": {"auto_check": bool(self.auto_update_var.get())},
            "mqtt": self._mqtt_fields(),
            "notifications": {
                "discord_webhook_url": self.webhook_var.get().strip(),
                "telegram_bot_token":  self.tg_token_var.get().strip(),
                "telegram_chat_id":    self.tg_chat_var.get().strip(),
                "ntfy_server":         self.ntfy_server_var.get().strip(),
                "ntfy_topic":          self.ntfy_topic_var.get().strip(),
            },
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
            if name == "runpod":
                target.pop("max_price_per_hour", None)          # 0.3.4: split per task
                target.pop("max_price_per_hour_upscale", None)  # 0.4.0: no auto-fallback
                target.pop("max_price_per_hour_tag", None)      # GPU is never substituted
            if name == "upscale":
                target.pop("discord_webhook_url", None)  # 0.3.8: moved to notifications
            target.update(values)

        if save_config():
            self._baseline = self._snapshot()    # the form is now the saved state
            # Apply MQTT changes immediately (connect/disconnect/reconfigure).
            self.app.restart_mqtt()
            self._save_status_base = "Saved."    # the live indicator renders it green
            self.save_status.configure(text="Saved.", foreground="#1a7f37")
            return True
        # Write failed — hold the error on screen so the live indicator doesn't
        # immediately overwrite it with "Not saved".
        self._save_status_hold = time.time() + 6
        self.save_status.configure(
            text="Could not write config.json (check file permissions).",
            foreground="#b3261e")
        return False

    def _refresh_save_indicator(self):
        """Light timer that keeps `save_status` reflecting the unsaved-changes
        state live: 'Not saved' (red) whenever the form differs from the saved
        state, otherwise the clean-state base text ('Saved.' green after a save,
        blank before). A transient message (e.g. a write error) is left alone until
        `_save_status_hold` expires. Polling avoids tracing dozens of mixed vars."""
        try:
            if not self.save_status.winfo_exists():
                return
            if time.time() >= self._save_status_hold:
                if self.is_dirty():
                    self.save_status.configure(text="Not saved", foreground="#b3261e")
                else:
                    base = self._save_status_base
                    self.save_status.configure(
                        text=base, foreground="#1a7f37" if base == "Saved." else "#666")
        except Exception:                       # noqa: BLE001 (never let the timer die loudly)
            pass
        self.after(400, self._refresh_save_indicator)

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
        self.default_vsrc_var.set(defs.get("video_source", ""))
        self.default_vout_var.set(defs.get("video_output", ""))
        self.ollama_url_var.set(ollama.get("url", "http://127.0.0.1:11434"))
        self.ollama_model_var.set(ollama.get("model", "qwen2.5vl:7b"))
        self.straighten_var.set(bool(tag.get("auto_straighten", True)))
        self.tag_maxpx_var.set(int(tag.get("max_image_px", 1280)))
        self.straighten_conf_var.set(float(tag.get("straighten_min_confidence", 0.9)))
        self.restarget_var.set(self._current_preset_label(ups))
        self.cutoff_var.set(int(ups.get("upscale_cutoff_pct", 66)))
        self.up_straighten_var.set(bool(ups.get("auto_straighten", True)))
        self.up_straighten_conf_var.set(float(ups.get("straighten_min_confidence", 0.9)))
        self.watchdog_var.set(bool(ups.get("watchdog_enabled", True)))
        self.watchdog_factor_var.set(float(ups.get("watchdog_factor", 3.0)))
        self.watchdog_consec_var.set(int(ups.get("watchdog_consecutive", 2)))
        notif = notifications.resolve_settings(CFG)
        self.webhook_var.set(notif.get("discord_webhook_url", ""))
        self.tg_token_var.set(notif.get("telegram_bot_token", ""))
        self.tg_chat_var.set(notif.get("telegram_chat_id", ""))
        self.ntfy_server_var.set(notif.get("ntfy_server", "https://ntfy.sh"))
        self.ntfy_topic_var.set(notif.get("ntfy_topic", ""))
        vid = CFG.get("video", {})
        self.video_target_var.set(vid.get("target", "1080p"))
        self.video_outsub_var.set(vid.get("output_subdir", "__upscaled__"))
        self.video_codec_var.set(self._video_codec_label(vid))
        self.video_confirm_var.set(bool(vid.get("confirm_before_rent", True)))
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
        self._save_status_base = ""          # discard the "Saved." indicator too


# ─────────────────────────────────────────────
#  TAB 5 — RUNPOD (remote pod settings)
# ─────────────────────────────────────────────

class RunPodTab(ttk.Frame):
    """Remote-pod (RunPod) settings, split out of SettingsTab (0.3.7) into its own
    tab because the section grew large and complex. Self-contained: it owns its
    save bar, unsaved-changes detection and revert, all scoped to the `runpod`
    block of config.json only (SettingsTab writes every other section)."""

    def __init__(self, notebook, app):
        super().__init__(notebook)
        self.app = app
        self._all_volumes = None
        self._pods_data = []      # last-fetched pod dicts (for the pods list)
        self._pod_rows = {}       # tree row id -> {"id", "active"}
        self._build()

    def _section(self, parent, title):
        lf = ttk.LabelFrame(parent, text=f"  {title}  ", padding=(10, 8))
        lf.pack(fill="x", padx=10, pady=(10, 0))
        return lf

    def _build(self):
        sf = _ScrollFrame(self)
        sf.pack(fill="both", expand=True)
        body = sf.body

        # ── Remote upscaling (RunPod) ───────────────────────────────────────────
        # No LabelFrame: this is now its own tab, so the content sits directly on
        # the page (a plain padded frame, packed so it coexists with the save bar).
        rp = CFG.get("runpod", {})
        sec = ttk.Frame(body, padding=(10, 8))
        sec.pack(fill="x", padx=10, pady=(10, 0))

        desc = ttk.Frame(sec)
        desc.grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 4))
        ttk.Label(desc, wraplength=560, foreground="#666",
                  text=("Process images on a rented remote pod (RunPod.io). Tick "
                        "'Run on remote pod (RunPod)' in the appropriate tab. The API "
                        "key authenticates the pod control plane; the auto-stop / "
                        "runtime limits below are the safety net that keeps a "
                        "billed pod from being left running.")
                  ).pack(anchor="w")
        key_link = tk.Label(desc, text="Get a RunPod API key →", fg="#3a86ff",
                            cursor="hand2", font=("Segoe UI", 9, "underline"))
        key_link.pack(anchor="w", pady=(2, 0))
        key_link.bind("<Button-1>",
                      lambda _e: webbrowser.open(runpod_client.CONSOLE_API_KEYS_URL))
        key_link.bind("<Enter>", lambda _e: key_link.configure(fg="#1a5fd0"))
        key_link.bind("<Leave>", lambda _e: key_link.configure(fg="#3a86ff"))
        Tooltip(key_link,
                "Opens the RunPod console (Settings → API Keys → Create API Key). "
                f"Docs: {runpod_client.DOCS_API_KEYS_URL}")

        # First row, all inline: API key + Test + Set up SSH key + SSH-ready status.
        keyrow = ttk.Frame(sec)
        keyrow.grid(row=1, column=0, columnspan=4, sticky="w", pady=3)
        ttk.Label(keyrow, text="API key:").pack(side="left")
        self.runpod_key_var = tk.StringVar(value=rp.get("api_key", ""))
        key_entry = ttk.Entry(keyrow, textvariable=self.runpod_key_var, show="•", width=44)
        key_entry.pack(side="left", padx=(4, 0))
        Tooltip(key_entry, "RunPod API key (rest.runpod.io). Stored locally in "
                           "config.json; never committed.")
        self.runpod_test_btn = ttk.Button(keyrow, text="Test", command=self._test_runpod)
        self.runpod_test_btn.pack(side="left", padx=(6, 0))
        # Zero-config SSH: the app owns a dedicated key and hands its public half to
        # every pod via PUBLIC_KEY, so the user never runs ssh-keygen or pastes a key
        # into the RunPod website. A run also auto-ensures it; this button is a
        # convenience, not a prerequisite.
        self.runpod_ssh_btn = ttk.Button(keyrow, text="Set up SSH key",
                                         command=self._setup_ssh)
        self.runpod_ssh_btn.pack(side="left", padx=(6, 0))
        Tooltip(self.runpod_ssh_btn,
                "Generates the dedicated SSH key the app uses to reach rented "
                "pods (one-time). Its public half is sent to each pod "
                "automatically — you never paste a key into the RunPod website.")
        self.runpod_ssh_status = ttk.Label(keyrow, text="", foreground="#666")
        self.runpod_ssh_status.pack(side="left", padx=(8, 0))
        self._refresh_ssh_status()

        # Region + data center (FIRST, so the Refresh next to it clearly drives the
        # GPU lists and volume below). A model volume is region-locked and can only
        # live where network storage is supported, so the picker is grouped by region
        # and offers storage-capable data centers only. Pods follow the volume's region.
        dcsel = ttk.Frame(sec)
        dcsel.grid(row=3, column=0, columnspan=4, sticky="w", pady=3)
        ttk.Label(dcsel, text="Region:").grid(row=0, column=0, sticky="w", padx=(0, 4))
        self.runpod_region_var = tk.StringVar()
        self.runpod_region_cmb = ttk.Combobox(dcsel, textvariable=self.runpod_region_var,
                                              state="readonly", values=runpod_client.REGIONS,
                                              width=16)
        self.runpod_region_cmb.grid(row=0, column=1, sticky="w")
        self.runpod_region_cmb.bind("<<ComboboxSelected>>", self._on_region_change)
        Tooltip(self.runpod_region_cmb,
                "Pick the part of the world to host your model volume and run pods. "
                "Only regions with a storage-capable data center are listed. Choose "
                "the one nearest you for the best throughput (volumes are region-locked).")
        ttk.Label(dcsel, text="Data center:").grid(row=0, column=2, sticky="w", padx=(18, 4))
        self.runpod_dc_var = tk.StringVar()
        self.runpod_dc_cmb = ttk.Combobox(dcsel, textvariable=self.runpod_dc_var,
                                          state="readonly", values=[], width=26)
        self.runpod_dc_cmb.grid(row=0, column=3, sticky="w")
        self.runpod_dc_cmb.bind("<<ComboboxSelected>>", self._on_dc_change)
        Tooltip(self.runpod_dc_cmb,
                "Only data centers that support network volumes are listed. The "
                "GPU lists and model volume below apply to this data center. Refresh "
                "to pull the live list (data centers, GPUs and volumes) from RunPod.")
        ttk.Button(dcsel, text="Refresh", command=self._refresh_datacenters).grid(
            row=0, column=4, sticky="w", padx=(8, 0))

        # Upscale GPU, Tag GPU and Model volume share ONE grid (column 0 = labels,
        # column 1 = comboboxes) so the three comboboxes line up under each other,
        # directly below the Region/Data center row whose Refresh drives them. The GPU
        # combos start as the curated name lists; Refresh repopulates them with the
        # GPUs offered in the selected DC plus live price (see _populate_settings_gpus).
        # `_gpu_id_by_label` maps the shown label back to the gpuTypeId (identity for
        # the curated names, label->id for live entries) so resolution works either way.
        gv = ttk.Frame(sec)
        gv.grid(row=4, column=0, columnspan=4, sticky="w", pady=3)
        _CMB_W = 50      # shared width so all three comboboxes align

        ttk.Label(gv, text="Upscale GPU:").grid(row=0, column=0, sticky="w", padx=(0, 6), pady=(0, 2))
        self.runpod_gpu_var = tk.StringVar(
            value=rp.get("gpu_type_id", runpod_client.GPU_TYPES[0]))
        self._gpu_id_by_label = {name: name for name in runpod_client.GPU_TYPES}
        self.runpod_gpu_cmb = ttk.Combobox(gv, textvariable=self.runpod_gpu_var, state="readonly",
                                           values=runpod_client.GPU_TYPES, width=_CMB_W)
        self.runpod_gpu_cmb.grid(row=0, column=1, sticky="w", pady=(0, 2))
        Tooltip(self.runpod_gpu_cmb,
                "RunPod GPU for upscaling (the heavy SeedVR2 work). The persisted "
                "preference; the Refresh above fills this with the GPUs offered in the "
                "selected data center and their live price. Each tab's live picker "
                "still overrides it per run.")

        # Tag & Rename GPU. The vision model needs only ~6.6 GB, so a cheap 16-20 GB
        # card is ideal; the chosen card is tried first, then the rest as a fallback.
        ttk.Label(gv, text="Tag GPU:").grid(row=1, column=0, sticky="w", padx=(0, 6), pady=(0, 2))
        self._tag_gpu_label_by_id = {gid: lbl for lbl, gid in runpod_client.TAG_GPU_TYPES}
        self._tag_gpu_id_by_label = {lbl: gid for lbl, gid in runpod_client.TAG_GPU_TYPES}
        cur_tg = rp.get("tag_gpu_type_id", runpod_client.TAG_GPU_TYPES[0][1])
        self.runpod_tag_gpu_var = tk.StringVar(
            value=self._tag_gpu_label_by_id.get(cur_tg, runpod_client.TAG_GPU_TYPES[0][0]))
        self.runpod_tag_gpu_cmb = ttk.Combobox(gv, textvariable=self.runpod_tag_gpu_var,
                                               state="readonly",
                                               values=[lbl for lbl, _ in runpod_client.TAG_GPU_TYPES],
                                               width=_CMB_W)
        self.runpod_tag_gpu_cmb.grid(row=1, column=1, sticky="w", pady=(0, 2))
        Tooltip(self.runpod_tag_gpu_cmb,
                "GPU for remote Tag & Rename. The vision model needs only ~6.6 GB, so "
                "a cheap card is plenty. The Refresh above fills this with the GPUs "
                "offered in the selected data center and their live price.")

        # Model volume (the persistent model store). Saved WITH its full display label
        # (network_volume_label) so it reads in full on restart, not just the bare id;
        # the bare id (network_volume_id) is what the run/provision code consumes.
        ttk.Label(gv, text="Model volume:").grid(row=2, column=0, sticky="w", padx=(0, 6))
        saved_vid = rp.get("network_volume_id", "")
        saved_vlabel = rp.get("network_volume_label", "")
        vol_initial = (saved_vlabel
                       if saved_vlabel and saved_vlabel.split("|", 1)[0].strip() == saved_vid
                       else saved_vid)
        self.runpod_vol_var = tk.StringVar(value=vol_initial)
        self.runpod_vol_cmb = ttk.Combobox(gv, textvariable=self.runpod_vol_var,
                                           state="readonly", width=_CMB_W)
        self.runpod_vol_cmb.grid(row=2, column=1, sticky="w")
        self.runpod_vol_cmb.bind("<<ComboboxSelected>>", self._on_volume_selected)
        Tooltip(self.runpod_vol_cmb,
                "Persistent RunPod network volume that holds the models (SeedVR2 + "
                "Ollama) so disposable pods don't re-download them. Format: "
                "'id | name | size | dc'. The list shows ALL volumes on your "
                "account; the one in the selected data center (or 'None | <data "
                "center>') is pre-selected. Picking a volume from another region "
                "switches the Region / Data center / GPU pickers to match it. "
                "Refresh lists them; Create makes one.")
        # The four volume action buttons on their own row, aligned under the combo.
        volbtns = ttk.Frame(gv)
        volbtns.grid(row=3, column=1, sticky="w", pady=(4, 0))
        ttk.Button(volbtns, text="Refresh", command=self._refresh_volumes).pack(side="left")
        ttk.Button(volbtns, text="Create…", command=self._create_volume).pack(side="left", padx=(6, 0))
        del_btn = tk.Button(volbtns, text="Delete…", fg="#b3261e", activeforeground="#b3261e",
                            cursor="hand2", command=self._delete_volume)
        del_btn.pack(side="left", padx=(6, 0))
        Tooltip(del_btn, "Permanently delete the selected network volume AND all "
                         "models stored on it. Asks for confirmation first.")
        prov_btn = ttk.Button(volbtns, text="Provision…", command=self._provision_models)
        prov_btn.pack(side="left", padx=(6, 0))
        Tooltip(prov_btn, "One-time: fill the selected volume with the models "
                          "(SeedVR2 + Ollama) by briefly renting a pod. ~10-20 min; "
                          "the pod is terminated automatically when finished.")

        # Where the volume actions act (rendered by the seed below via _update_dc_target).
        self.runpod_dc_target = ttk.Label(sec, text="", foreground="#444")
        self.runpod_dc_target.grid(row=5, column=0, columnspan=4, sticky="w", padx=2, pady=(2, 0))

        # Picker state: the last-fetched volumes (None until a Refresh, so the
        # filter leaves the saved id alone on first open). Regions/DCs without
        # network-volume storage are simply never populated, so there's no
        # special-case to carry (a compute-only DC like OC-AU-1 just doesn't appear).
        self._all_volumes = None

        # Seed the picker from the curated list, then point it at the saved DC.
        dc_ids = rp.get("data_center_ids") or []
        cur_dc = dc_ids[0] if dc_ids else "EU-RO-1"
        self._set_dc_entries(
            [{"id": dcid, "label": lbl, "region": runpod_client.region_of(dcid)}
             for lbl, dcid in runpod_client.DATACENTERS],
            preserve_id=cur_dc)

        safety = ttk.Frame(sec)
        safety.grid(row=7, column=0, columnspan=4, sticky="w", pady=3)
        ttk.Label(safety, text="Auto-stop after:").pack(side="left", padx=(0, 4))
        self.runpod_maxrun_var = tk.StringVar(value=str(rp.get("max_runtime_minutes", 0)))
        maxrun_spin = ttk.Spinbox(safety, from_=0, to=10080, increment=30, width=7,
                                  textvariable=self.runpod_maxrun_var)
        maxrun_spin.pack(side="left")
        ttk.Label(safety, text="min max runtime,").pack(side="left", padx=(4, 12))
        Tooltip(maxrun_spin, "Hard ceiling enforced on the pod itself: it stops "
                             "after this long no matter what. Defaults to 0 (no "
                             "limit) so a long batch of many images is never cut off "
                             "mid-run — the idle timeout below is the dead-man's "
                             "switch that still ends a billed pod if the connection "
                             "drops. Set a value only if you want a hard cap.")
        self.runpod_idle_var = tk.StringVar(value=str(rp.get("idle_timeout_minutes", 15)))
        idle_spin = ttk.Spinbox(safety, from_=0, to=1440, increment=5, width=6,
                                textvariable=self.runpod_idle_var)
        idle_spin.pack(side="left")
        ttk.Label(safety, text="min idle timeout").pack(side="left", padx=(4, 12))
        Tooltip(idle_spin, "Stop the pod after this many minutes with no work "
                           "(0 = no idle limit).")

        ttk.Label(safety, text="·  Provision:").pack(side="left", padx=(0, 4))
        self.runpod_provrun_var = tk.StringVar(value=str(rp.get("provision_max_runtime_minutes", 60)))
        provrun_spin = ttk.Spinbox(safety, from_=30, to=240, increment=15, width=5,
                                   textvariable=self.runpod_provrun_var)
        provrun_spin.pack(side="left")
        ttk.Label(safety, text="min ceiling").pack(side="left", padx=(4, 0))
        Tooltip(provrun_spin, "Hard ceiling for the temporary model-provisioning "
                              "pod's dead-man's switch: it self-terminates after "
                              "this long even if the app is closed or the "
                              "provisioning window is force-killed, so a stuck "
                              "download can't leave a pod billing. The download "
                              "normally takes 10-20 min; 60 leaves headroom. (Idle "
                              "timeout doesn't apply here — no heartbeat is written "
                              "during a download.)")

        self.runpod_terminate_var = tk.BooleanVar(value=bool(rp.get("terminate_when_done", True)))
        term_chk = ttk.Checkbutton(
            sec, text="Terminate (delete) the pod when done, not just stop it",
            variable=self.runpod_terminate_var)
        term_chk.grid(row=8, column=0, columnspan=4, sticky="w", pady=3)
        Tooltip(term_chk, "ON (recommended): the disposable pod is deleted when a "
                          "run ends, freeing ALL billing. This NEVER deletes your "
                          "model network volume — that's a separate resource. OFF "
                          "only stops the pod (it lingers as EXITED and keeps "
                          "billing for its disk); the app never reuses a stopped "
                          "pod, so OFF just leaves billing cruft.")

        self.runpod_status = ttk.Label(sec, text="", foreground="#666")
        self.runpod_status.grid(row=9, column=0, columnspan=4, sticky="w", padx=6, pady=(4, 0))

        # ── Your pods ───────────────────────────────────────────────────────────
        # List every pod on the account (running or exited) with a Terminate
        # control, so the user can clean up billing without visiting the RunPod
        # website. A pod a live remote run depends on is marked '(in use)' and
        # can't be terminated here.
        pods = ttk.Frame(body, padding=(10, 8))
        pods.pack(fill="x", padx=10, pady=(10, 0))
        hdr = ttk.Frame(pods)
        hdr.pack(fill="x")
        ttk.Label(hdr, text="Your pods", font=("Segoe UI", 9, "bold")).pack(side="left")
        ttk.Button(hdr, text="Refresh", command=self._refresh_pods).pack(side="left", padx=(10, 0))
        self.runpod_pods_term_btn = tk.Button(
            hdr, text="Terminate selected…", fg="#b3261e", activeforeground="#b3261e",
            cursor="hand2", state="disabled", command=self._terminate_pod)
        self.runpod_pods_term_btn.pack(side="left", padx=(6, 0))
        Tooltip(self.runpod_pods_term_btn,
                "Permanently delete the selected pod, freeing all its billing. This "
                "never touches your model network volume. A pod a remote run is "
                "using right now is marked '(in use)' and can't be terminated here "
                "— stop that run first.")

        tree = ttk.Treeview(pods,
                            columns=("name", "status", "gpu", "region", "dc", "cost"),
                            show="headings", height=5, selectmode="browse")
        for col, txt, w, anchor in (("name", "Name / id", 170, "w"),
                                    ("status", "Status", 85, "w"),
                                    ("gpu", "GPU", 150, "w"),
                                    ("region", "Region", 105, "w"),
                                    ("dc", "Data center", 95, "w"),
                                    ("cost", "$/hr", 55, "e")):
            tree.heading(col, text=txt)
            tree.column(col, width=w, anchor=anchor, stretch=(col == "gpu"))
        tree.pack(fill="x", pady=(6, 0))
        tree.bind("<<TreeviewSelect>>", lambda _e: self._refresh_terminate_state())
        self.runpod_pods_tree = tree
        self.runpod_pods_status = ttk.Label(pods, text="", foreground="#666")
        self.runpod_pods_status.pack(anchor="w", pady=(4, 0))

        # ── Save bar ────────────────────────────────────────────
        bar = ttk.Frame(body, padding=(8, 12))
        bar.pack(fill="x")
        ttk.Button(bar, text="Save settings", command=self._save).pack(side="left")
        self.save_status = ttk.Label(bar, text="", foreground="#666")
        self.save_status.pack(side="left", padx=12)

        # Baseline + live "Not saved" indicator, same pattern as SettingsTab.
        self._baseline = self._snapshot()
        self._save_status_base = ""
        self._save_status_hold = 0.0
        self._refresh_save_indicator()

    # ── save / unsaved-changes machinery (runpod-scoped) ───────────────

    def _collect(self):
        """The runpod section the form currently describes, as ({"runpod": {...}},
        errors). Kept in SettingsTab's (sections, errors) shape so the save/dirty
        helpers below mirror it exactly."""
        return {"runpod": self._runpod_fields()}, []

    def _snapshot(self):
        sections, errors = self._collect()
        return json.dumps(sections, sort_keys=True), bool(errors)

    def is_dirty(self):
        try:
            return self._snapshot() != self._baseline
        except Exception:
            return False

    def _save(self):
        sections, errors = self._collect()
        if errors:
            messagebox.showwarning(
                APP_TITLE, "These fields need a whole number:\n  • " + "\n  • ".join(errors))
            return False
        for name, values in sections.items():
            target = CFG.setdefault(name, {})
            if name == "runpod":
                target.pop("max_price_per_hour", None)          # 0.3.4: split per task
                target.pop("max_price_per_hour_upscale", None)  # 0.4.0: no auto-fallback
                target.pop("max_price_per_hour_tag", None)      # GPU is never substituted
            target.update(values)
        if save_config():
            self._baseline = self._snapshot()
            self._save_status_base = "Saved."
            self.save_status.configure(text="Saved.", foreground="#1a7f37")
            return True
        self._save_status_hold = time.time() + 6
        self.save_status.configure(
            text="Could not write config.json (check file permissions).",
            foreground="#b3261e")
        return False

    def _refresh_save_indicator(self):
        """Light timer mirroring SettingsTab's: 'Not saved' (red) when the form
        differs from the saved state, 'Saved.' (green) right after a save."""
        try:
            if not self.save_status.winfo_exists():
                return
            if time.time() >= self._save_status_hold:
                if self.is_dirty():
                    self.save_status.configure(text="Not saved", foreground="#b3261e")
                else:
                    base = self._save_status_base
                    self.save_status.configure(
                        text=base, foreground="#1a7f37" if base == "Saved." else "#666")
        except Exception:                       # noqa: BLE001
            pass
        self.after(400, self._refresh_save_indicator)

    def revert(self):
        """Discard unsaved edits: reset every runpod field to the values in CFG."""
        rp = CFG.get("runpod", {})
        self.runpod_key_var.set(rp.get("api_key", ""))
        self.runpod_maxrun_var.set(str(rp.get("max_runtime_minutes", 0)))
        self.runpod_idle_var.set(str(rp.get("idle_timeout_minutes", 15)))
        self.runpod_provrun_var.set(str(rp.get("provision_max_runtime_minutes", 60)))
        self.runpod_terminate_var.set(bool(rp.get("terminate_when_done", True)))
        # Reset the GPU combos to the curated lists (discard any live-refresh state).
        self.runpod_gpu_cmb.configure(values=runpod_client.GPU_TYPES)
        self._gpu_id_by_label = {name: name for name in runpod_client.GPU_TYPES}
        self.runpod_gpu_var.set(rp.get("gpu_type_id", runpod_client.GPU_TYPES[0]))
        self._tag_gpu_label_by_id = {gid: lbl for lbl, gid in runpod_client.TAG_GPU_TYPES}
        self._tag_gpu_id_by_label = {lbl: gid for lbl, gid in runpod_client.TAG_GPU_TYPES}
        self.runpod_tag_gpu_cmb.configure(values=[lbl for lbl, _ in runpod_client.TAG_GPU_TYPES])
        self.runpod_tag_gpu_var.set(self._tag_gpu_label_by_id.get(
            rp.get("tag_gpu_type_id", runpod_client.TAG_GPU_TYPES[0][1]),
            runpod_client.TAG_GPU_TYPES[0][0]))
        dc_ids = rp.get("data_center_ids") or []
        cur_dc = dc_ids[0] if dc_ids else "EU-RO-1"
        self._sync_region_dc_to(cur_dc)
        saved_vid = rp.get("network_volume_id", "")
        saved_vlabel = rp.get("network_volume_label", "")
        self.runpod_vol_var.set(
            saved_vlabel if saved_vlabel and saved_vlabel.split("|", 1)[0].strip() == saved_vid
            else saved_vid)
        self._baseline = self._snapshot()
        self._save_status_base = ""

    def _runpod_fields(self):
        """The RunPod settings currently in the form (so Test works pre-Save).
        Numeric fields fall back to their defaults rather than erroring — the
        save path re-validates and reports."""
        def _num(var, default, cast):
            try:
                return cast(str(var.get()).strip())
            except (ValueError, tk.TclError):
                return default
        rp = CFG.get("runpod", {})
        dc_id = self._selected_dc_id()
        return {
            "api_key":              self.runpod_key_var.get().strip(),
            "max_runtime_minutes":  _num(self.runpod_maxrun_var, 0, int),
            "idle_timeout_minutes": _num(self.runpod_idle_var, 15, int),
            "provision_max_runtime_minutes": _num(self.runpod_provrun_var, 60, int),
            "terminate_when_done":  bool(self.runpod_terminate_var.get()),
            "gpu_type_id":      self._gpu_id_by_label.get(
                self.runpod_gpu_var.get(),
                self.runpod_gpu_var.get().strip() or runpod_client.GPU_TYPES[0]),
            "tag_gpu_type_id":  self._tag_gpu_id_by_label.get(
                self.runpod_tag_gpu_var.get(), runpod_client.TAG_GPU_TYPES[0][1]),
            "data_center_ids":  [dc_id] if dc_id else [],
            "network_volume_id": self._selected_volume_id(),
            # The full combobox label ('id | name | size | dc') so it reloads in full
            # next launch instead of just the bare id; blank when no real volume.
            "network_volume_label": (self.runpod_vol_var.get()
                                     if self._selected_volume_id() else ""),
            # Carried through unchanged (no UI) so a save never drops them.
            # hourly_rate has no UI control (live GPU prices + the per-task ceilings
            # replaced it); only the `status` dev CLI still reads it for a cost estimate.
            "hourly_rate":      rp.get("hourly_rate", 0.90),
            "image_name":       rp.get("image_name", ""),
            "template_id":      rp.get("template_id", ""),
            "container_disk_gb": rp.get("container_disk_gb", 30),
            "ssh_key_path":     rp.get("ssh_key_path", ""),
            "worker_port":      rp.get("worker_port", 8200),
            "stop_pod_when_done": rp.get("stop_pod_when_done", True),
        }

    def _selected_volume_id(self):
        """Resolve the network-volume field to a bare id ('' if none). The combobox
        shows 'id | name | size | dc', or a 'None | <data center>' placeholder when
        the selected DC has no volume — both resolve to no id."""
        tok = (self.runpod_vol_var.get().split("|", 1)[0]).strip()
        return "" if tok == "None" else tok

    # ── Region / data-center picker ──────────────────────────────────────────
    def _set_dc_entries(self, entries, preserve_id=None):
        """Adopt a list of data-center entries ({id,label,region}) as the picker's
        source, rebuild the lookup maps, and point the Region/Data center combos at
        `preserve_id` (kept available even if absent from the list)."""
        entries = list(entries)
        if preserve_id and preserve_id not in {e["id"] for e in entries}:
            entries.append({
                "id": preserve_id, "label": preserve_id,
                "region": runpod_client.region_of(preserve_id) or runpod_client.REGIONS[0]})
        self._dc_entries     = entries
        self._dc_label_by_id = {e["id"]: e["label"] for e in entries}
        self._dc_id_by_label = {e["label"]: e["id"] for e in entries}
        # Only offer regions that actually have a storage-capable DC (so e.g.
        # Oceania, with only the compute-only OC-AU-1, simply doesn't appear).
        avail_regions = [r for r in runpod_client.REGIONS
                         if any(e["region"] == r for e in entries)]
        self.runpod_region_cmb.configure(values=avail_regions)
        target = preserve_id or self._selected_dc_id()
        region = runpod_client.region_of(target)
        if region not in avail_regions:
            region = self.runpod_region_var.get() if self.runpod_region_var.get() in avail_regions \
                else (avail_regions[0] if avail_regions else "")
        self.runpod_region_var.set(region)
        self._populate_dc_for_region(select_id=target)

    def _populate_dc_for_region(self, select_id=None):
        """Fill the Data center combo with the DCs in the chosen region, selecting
        `select_id` if it lives there else the first one."""
        region = self.runpod_region_var.get()
        labels = [e["label"] for e in self._dc_entries if e["region"] == region]
        self.runpod_dc_cmb.configure(values=labels)
        if labels:
            lab = self._dc_label_by_id.get(select_id) if select_id else None
            self.runpod_dc_var.set(lab if lab in labels else labels[0])
        else:
            self.runpod_dc_var.set("")
        self._update_dc_target()
        self._apply_volume_filter()

    def _on_region_change(self, *_):
        self._populate_dc_for_region()

    def _on_dc_change(self, *_):
        self._update_dc_target()
        self._apply_volume_filter()

    def _selected_dc_id(self):
        """The data-center id currently chosen in the picker ('' if none)."""
        return self._dc_id_by_label.get(self.runpod_dc_var.get(), "")

    def _update_dc_target(self):
        """Spell out, in plain language, where the volume buttons will act. Regions
        without a storage-capable DC are never offered, so a missing selection just
        means 'nothing picked yet'."""
        dc = self._selected_dc_id()
        region = self.runpod_region_var.get()
        if dc:
            self.runpod_dc_target.configure(
                text=f"Volume actions (Create / Provision) act in:  {region}  ·  {dc}",
                foreground="#444")
        else:
            self.runpod_dc_target.configure(
                text="Pick a region and data center for the model volume.",
                foreground="#666")

    def _sync_region_dc_to(self, dc_id):
        """Point the Region/Data center pickers at `dc_id` (e.g. a selected
        volume's region) so the displayed target matches where actions run. Adds an
        ad-hoc entry if the id isn't already in the list."""
        if not dc_id:
            return
        if dc_id not in self._dc_label_by_id:
            self._dc_entries.append({
                "id": dc_id, "label": dc_id,
                "region": runpod_client.region_of(dc_id) or runpod_client.REGIONS[0]})
            self._dc_label_by_id[dc_id] = dc_id
            self._dc_id_by_label[dc_id] = dc_id
        self.runpod_region_var.set(
            runpod_client.region_of(dc_id) or self.runpod_region_var.get())
        self._populate_dc_for_region(select_id=dc_id)

    def _on_volume_selected(self, *_):
        """When the user picks an existing volume, follow it: a volume is
        region-locked, so the Region/Data center should reflect where it lives, and
        the Upscale/Tag GPU lists should refresh for that data center. Picking a
        volume from another region is thus the reverse of changing the DC by hand."""
        parts = self.runpod_vol_var.get().split("|")
        if len(parts) >= 4:
            dc = parts[-1].strip()
            if dc and dc != "?":
                self._sync_region_dc_to(dc)
                self._refresh_settings_gpus()

    def _refresh_datacenters(self):
        """Pull the live storage-capable data-center list from RunPod (GraphQL) and
        populate the Region/Data center pickers. Also refreshes the model volumes
        and the Upscale/Tag GPU lists for the selected data center."""
        key = self.runpod_key_var.get().strip()
        if not key:
            self.runpod_status.configure(text="Enter a RunPod API key first.",
                                         foreground="#b3261e")
            return
        self.runpod_status.configure(text="Listing data centers…", foreground="#666")
        keep = self._selected_dc_id()

        def work():
            try:
                dcs = runpod_client.data_centers(key)      # storage-capable only
                err = None
            except runpod_client.RunPodError as exc:
                dcs, err = [], str(exc)

            def apply():
                if err:
                    self.runpod_status.configure(text=err, foreground="#b3261e")
                    return
                entries = [{
                    "id": d["id"],
                    "label": (f'{d["location"]} ({d["id"]})' if d["location"] else d["id"]),
                    "region": d["region"] or runpod_client.region_of(d["id"]),
                } for d in dcs]
                self._set_dc_entries(entries, preserve_id=keep)
                self.runpod_status.configure(
                    text=f"{len(entries)} storage-capable data center(s) across "
                         f"{len({e['region'] for e in entries})} region(s).",
                    foreground="#1a7f37")
                # Also refresh volumes + GPU lists for the (now-selected) DC.
                self._refresh_volumes()
                self._refresh_settings_gpus()
            self.after(0, apply)

        threading.Thread(target=work, daemon=True).start()

    def _fmt_gpu(self, g):
        """Label for a Settings GPU combo entry: name, VRAM, live price, stock."""
        price = f"${g['price']:.2f}/h" if g.get("price") is not None else "n/a"
        tail = "" if g.get("stock") else " | no stock"
        return f"{g.get('name', g.get('id'))} | {g.get('memory_gb', 0)} GB | {price}{tail}"

    def _refresh_settings_gpus(self):
        """Populate the Upscale/Tag GPU comboboxes with the GPUs the selected data
        center offers, each with its live price. Out-of-stock cards are included
        (these are a stored PREFERENCE, not a now-deployable pick) so the defaults
        (RTX 5090 / RTX 2000 Ada) are offered even when momentarily sold out."""
        key = self.runpod_key_var.get().strip()
        if not key:
            return
        dc = self._selected_dc_id() or None

        def work():
            try:
                gpus = runpod_client.available_gpus(key, dc, min_memory_gb=0,
                                                    include_out_of_stock=True)
                err = None
            except runpod_client.RunPodError as exc:
                gpus, err = [], str(exc)

            def apply():
                if err or not gpus:
                    return                  # keep the curated lists on failure
                self._populate_settings_gpus(gpus)
            self.after(0, apply)

        threading.Thread(target=work, daemon=True).start()

    def _populate_settings_gpus(self, gpus):
        """Fill both GPU combos from a live availability list, partitioned by the
        VRAM floor (≥32 GB upscale, ≥16 GB tag), keeping the current pick if it is
        still offered else defaulting to RTX 5090 / RTX 2000 Ada else cheapest."""
        ups = [g for g in gpus if (g.get("memory_gb") or 0) >= 32]
        tag = [g for g in gpus if (g.get("memory_gb") or 0) >= 16]
        self._fill_gpu_combo(self.runpod_gpu_cmb, self.runpod_gpu_var, ups,
                             "_gpu_id_by_label", "NVIDIA GeForce RTX 5090")
        self._fill_gpu_combo(self.runpod_tag_gpu_cmb, self.runpod_tag_gpu_var, tag,
                             "_tag_gpu_id_by_label", "NVIDIA RTX 2000 Ada Generation")

    def _fill_gpu_combo(self, cmb, var, gpus, id_map_attr, default_id):
        """Set a GPU combo's values to live entries and select the current pick (by
        resolved id) if still present, else default_id, else the first (cheapest)."""
        if not gpus:
            return
        labels  = [self._fmt_gpu(g) for g in gpus]
        id_by_label = {lbl: g["id"] for lbl, g in zip(labels, gpus)}
        cur_id = getattr(self, id_map_attr, {}).get(var.get())
        setattr(self, id_map_attr, id_by_label)
        cmb.configure(values=labels)
        want = cur_id if any(g["id"] == cur_id for g in gpus) else default_id
        sel = next((lbl for lbl, g in zip(labels, gpus) if g["id"] == want), labels[0])
        var.set(sel)

    def _refresh_volumes(self, select_id=None):
        """Fetch the account's network volumes (free call), cache them, and show the
        ones in the currently-selected data center. `select_id` pre-selects a volume
        (e.g. one just created)."""
        key = self.runpod_key_var.get().strip()
        if not key:
            self.runpod_status.configure(text="Enter a RunPod API key first.",
                                         foreground="#b3261e")
            return
        self.runpod_status.configure(text="Listing network volumes…", foreground="#666")

        def work():
            try:
                vols = runpod_client.list_network_volumes(key)
                err = None
            except runpod_client.RunPodError as exc:
                vols, err = [], str(exc)

            def apply():
                if err:
                    self.runpod_status.configure(text=err, foreground="#b3261e")
                    return
                self._all_volumes = [v for v in vols if isinstance(v, dict)]
                # select_id forces a pick (e.g. a just-created volume); a plain
                # Refresh (None) lets the filter follow the selected data center.
                total, n = self._apply_volume_filter(select_id=select_id)
                dc = self._selected_dc_id() or "the selected data center"
                if total:
                    self.runpod_status.configure(
                        text=(f"{total} volume(s) on your account · {n} in {dc}."
                              if n else
                              f"{total} volume(s) on your account · none in {dc} "
                              "(use Create…)."),
                        foreground="#1a7f37")
                else:
                    self.runpod_status.configure(
                        text="No network volumes on your account yet — use Create…",
                        foreground="#666")
            self.after(0, apply)

        threading.Thread(target=work, daemon=True).start()

    def _volume_label(self, v):
        return (f"{v.get('id','')} | {v.get('name','?')} | "
                f"{v.get('size','?')} GB | {v.get('dataCenterId','?')}")

    def _apply_volume_filter(self, select_id=None):
        """Populate the Model volume combobox with ALL of the account's volumes (so
        the user can see and pick any volume without hunting region-by-region), and
        select the one in the currently-selected data center — or a 'None | <dc>'
        placeholder when that DC has none. Returns (total, in_dc) counts; (0, 0)
        before the first fetch, so the saved id stays visible on first open."""
        if self._all_volumes is None:
            return (0, 0)
        dc = self._selected_dc_id()
        all_labels = [self._volume_label(v) for v in self._all_volumes]
        in_dc = [v for v in self._all_volumes if v.get("dataCenterId") == dc] if dc else []
        # A DC with no volume still needs a readable selection: prepend a
        # 'None | <dc>' placeholder (kept first so it's easy to spot in the list).
        values = list(all_labels)
        placeholder = None
        if not in_dc:
            dc_label = self._dc_label_by_id.get(dc, dc) if dc else "(no data center)"
            placeholder = f"None | {dc_label}"
            values = [placeholder] + values
        self.runpod_vol_cmb.configure(values=values)
        # Selection: an explicit target wins; else keep the current pick when it
        # belongs to this DC; else the DC's own volume; else the placeholder. So a
        # DC change follows the DC, while a Refresh keeps a still-valid selection.
        cur = self._selected_volume_id()
        want = select_id or (cur if any(v.get("id") == cur and v.get("dataCenterId") == dc
                                        for v in self._all_volumes) else None)
        sel = (next((l for l in all_labels if l.split("|", 1)[0].strip() == want), None)
               if want else None)
        if sel is None and in_dc:
            sel = self._volume_label(in_dc[0])
        if sel is None:
            sel = placeholder or (values[0] if values else "")
        self.runpod_vol_var.set(sel)
        return (len(self._all_volumes), len(in_dc))

    def _create_volume(self):
        """Create a network volume in the selected region's data center (this
        starts a small monthly storage charge — confirmed first)."""
        key = self.runpod_key_var.get().strip()
        if not key:
            self.runpod_status.configure(text="Enter a RunPod API key first.",
                                         foreground="#b3261e")
            return
        dc_id = self._selected_dc_id()
        if not dc_id:
            self.runpod_status.configure(
                text=f"No storage data center selected for {self.runpod_region_var.get()} "
                     "— pick another region first.", foreground="#b3261e")
            return
        name = simpledialog.askstring(
            "Create network volume",
            "Name for the model volume:", parent=self, initialvalue="image-toolbox-models")
        if not name:
            return
        size = simpledialog.askinteger(
            "Create network volume",
            "Size in GB (SeedVR2 ~16 GB + Ollama model ~6 GB; 40 leaves headroom):",
            parent=self, initialvalue=40, minvalue=1, maxvalue=4000)
        if not size:
            return
        est = size * 0.07
        if not messagebox.askyesno(
                APP_TITLE,
                f"Create a {size} GB network volume '{name}' in {dc_id}?\n\n"
                f"This starts a storage charge of about ${est:.2f}/month "
                f"(at $0.07/GB/mo) until you delete it."):
            return
        self.runpod_status.configure(text="Creating network volume…", foreground="#666")

        def work():
            try:
                vol = runpod_client.create_network_volume(key, name, size, dc_id)
                err = None
            except runpod_client.RunPodError as exc:
                vol, err = None, str(exc)

            def apply():
                if err:
                    self.runpod_status.configure(text=err, foreground="#b3261e")
                    return
                vid = (vol or {}).get("id", "")
                self.runpod_status.configure(
                    text=f"Created volume {vid} in {dc_id}.", foreground="#1a7f37")
                self._refresh_volumes(select_id=vid)
            self.after(0, apply)

        threading.Thread(target=work, daemon=True).start()

    def _delete_volume(self):
        """Permanently delete the selected network volume (and the models on it),
        behind an explicit warning confirmation."""
        key = self.runpod_key_var.get().strip()
        if not key:
            self.runpod_status.configure(text="Enter a RunPod API key first.",
                                         foreground="#b3261e")
            return
        vid = self._selected_volume_id()
        if not vid:
            self.runpod_status.configure(
                text="Select a volume to delete first (Refresh, then pick one).",
                foreground="#b3261e")
            return
        label = self.runpod_vol_var.get().strip() or vid
        if not messagebox.askyesno(
                APP_TITLE,
                f"Delete this network volume?\n\n  {label}\n\n"
                "This PERMANENTLY destroys the volume and ALL MODELS stored on it "
                "(SeedVR2, Ollama). Any disposable pod will have to re-download "
                "~22 GB the next time you run. This cannot be undone.",
                icon="warning", default="no"):
            return
        self.runpod_status.configure(text="Deleting network volume…", foreground="#666")

        def work():
            try:
                runpod_client.delete_network_volume(key, vid)
                err = None
            except runpod_client.RunPodError as exc:
                err = str(exc)

            def apply():
                if err:
                    self.runpod_status.configure(text=err, foreground="#b3261e")
                    return
                # Clear the selection if it was the deleted volume, then re-list.
                if self._selected_volume_id() == vid:
                    self.runpod_vol_var.set("")
                self.runpod_status.configure(
                    text=f"Deleted volume {vid}.", foreground="#1a7f37")
                self._refresh_volumes()
            self.after(0, apply)

        threading.Thread(target=work, daemon=True).start()

    # ── Your pods (list + terminate) ──────────────────────────────────────────
    def _active_pod_ids(self):
        app = getattr(self, "app", None)
        return app.active_remote_pod_ids() if app is not None else set()

    def _refresh_pods(self):
        """Fetch every pod on the account (running or exited) and show it."""
        key = self.runpod_key_var.get().strip()
        if not key:
            self.runpod_pods_status.configure(text="Enter a RunPod API key first.",
                                              foreground="#b3261e")
            return
        self.runpod_pods_status.configure(text="Listing pods…", foreground="#666")

        def work():
            try:
                pods = runpod_client.list_pods_detailed(key)
                err = None
            except runpod_client.RunPodError as exc:
                pods, err = [], str(exc)

            def apply():
                if err:
                    self.runpod_pods_status.configure(text=err, foreground="#b3261e")
                    return
                self._pods_data = [p for p in pods if isinstance(p, dict)]
                self._render_pods()
                n = len(self._pods_data)
                self.runpod_pods_status.configure(
                    text=(f"{n} pod(s) on your account." if n
                          else "No pods on your account."),
                    foreground="#1a7f37" if n else "#666")
            self.after(0, apply)

        threading.Thread(target=work, daemon=True).start()

    def _pod_fields(self, p):
        """Display tuple for one pod from a normalized runpod_client record
        ({id, name, status, gpu, gpu_count, region, data_center, cost})."""
        pid = p.get("id", "")
        name = p.get("name") or pid
        status = p.get("status") or "?"
        gpu = p.get("gpu") or "?"
        cnt = p.get("gpu_count")
        if cnt and gpu != "?":
            gpu = f"{cnt}× {gpu}"
        region = p.get("region") or "?"
        dc = p.get("data_center") or "?"
        cost = p.get("cost")
        cost = f"${cost:.2f}" if isinstance(cost, (int, float)) else "?"
        return pid, name, status, gpu, region, dc, cost

    def _render_pods(self):
        """Rebuild the pods tree from the cached data, marking the live pod(s)."""
        tree = self.runpod_pods_tree
        if not tree.winfo_exists():
            return
        tree.delete(*tree.get_children())
        self._pod_rows = {}
        active = self._active_pod_ids()
        for p in self._pods_data:
            pid, name, status, gpu, region, dc, cost = self._pod_fields(p)
            is_active = pid in active
            shown = f"{status} · in use" if is_active else status
            row = tree.insert("", "end", values=(name, shown, gpu, region, dc, cost))
            self._pod_rows[row] = {"id": pid, "active": is_active}
        self._refresh_terminate_state()

    def _refresh_terminate_state(self):
        """Enable Terminate only when a selected pod is not in use by a live run."""
        if not self.runpod_pods_term_btn.winfo_exists():
            return
        sel = self.runpod_pods_tree.selection()
        info = self._pod_rows.get(sel[0]) if sel else None
        ok = bool(info) and not info["active"] and info["id"] not in self._active_pod_ids()
        self.runpod_pods_term_btn.configure(state="normal" if ok else "disabled")

    def on_active_pods_changed(self):
        """A remote run started/ended: re-mark the list and the Terminate button."""
        if self._pods_data:
            self._render_pods()
        else:
            self._refresh_terminate_state()

    def _terminate_pod(self):
        sel = self.runpod_pods_tree.selection()
        info = self._pod_rows.get(sel[0]) if sel else None
        if not info:
            return
        pid = info["id"]
        # Re-check liveness at click time (a run may have started since render).
        if info["active"] or pid in self._active_pod_ids():
            self.runpod_pods_status.configure(
                text="That pod is in use by a running remote task — stop it first.",
                foreground="#b3261e")
            self._refresh_terminate_state()
            return
        key = self.runpod_key_var.get().strip()
        if not key:
            self.runpod_pods_status.configure(text="Enter a RunPod API key first.",
                                              foreground="#b3261e")
            return
        vals = self.runpod_pods_tree.item(sel[0], "values")
        label = f"{vals[0]} ({pid})" if vals else pid
        if not messagebox.askyesno(
                APP_TITLE,
                f"Terminate this pod?\n\n  {label}\n\n"
                "This permanently deletes the pod and frees its billing. It does "
                "NOT touch your model network volume. This cannot be undone.",
                icon="warning", default="no"):
            return
        self.runpod_pods_status.configure(text="Terminating pod…", foreground="#666")

        def work():
            try:
                runpod_client.terminate_pod(key, pid)
                err = None
            except runpod_client.RunPodError as exc:
                err = str(exc)

            def apply():
                if err:
                    self.runpod_pods_status.configure(text=err, foreground="#b3261e")
                    return
                self.runpod_pods_status.configure(
                    text=f"Terminated pod {pid}.", foreground="#1a7f37")
                self._refresh_pods()
            self.after(0, apply)

        threading.Thread(target=work, daemon=True).start()

    def _test_runpod(self):
        key = self.runpod_key_var.get().strip()
        if not key:
            self.runpod_status.configure(text="Enter a RunPod API key first.",
                                         foreground="#b3261e")
            return
        self.runpod_test_btn.configure(state="disabled")
        self.runpod_status.configure(text="Testing connection…", foreground="#666")

        def work():
            ok, msg = runpod_client.test_connection(key)
            def apply():
                self.runpod_test_btn.configure(state="normal")
                self.runpod_status.configure(
                    text=msg, foreground="#1a7f37" if ok else "#b3261e")
            self.after(0, apply)

        threading.Thread(target=work, daemon=True).start()

    def _effective_ssh_key(self):
        """The key path a run will use: the configured one, else the app default."""
        return (os.path.expandvars(CFG.get("runpod", {}).get("ssh_key_path", ""))
                or ssh_setup.default_key_path())

    def _refresh_ssh_status(self):
        """Reflect the current SSH-key state without generating anything."""
        if ssh_setup.read_public_key(self._effective_ssh_key()):
            self.runpod_ssh_status.configure(text="SSH key ready ✓", foreground="#1a7f37")
            return
        ok, _ssh, _kg, _msg = ssh_setup.ssh_available()
        if ok:
            self.runpod_ssh_status.configure(text="No key yet — click to set up.",
                                             foreground="#666")
        else:
            self.runpod_ssh_status.configure(
                text="OpenSSH not found — enable it in Windows Optional features.",
                foreground="#b3261e")

    def _setup_ssh(self):
        """Generate (or locate) the app's dedicated SSH key off the UI thread."""
        self.runpod_ssh_btn.configure(state="disabled")
        self.runpod_ssh_status.configure(text="Setting up SSH…", foreground="#666")
        # Use the configured path if any; ensure_keypair falls back to the default.
        key_path = os.path.expandvars(CFG.get("runpod", {}).get("ssh_key_path", "")) or None

        def work():
            ok, info = ssh_setup.setup(key_path)
            def apply():
                self.runpod_ssh_btn.configure(state="normal")
                self.runpod_ssh_status.configure(
                    text=info.get("message", ""),
                    foreground="#1a7f37" if ok else "#b3261e")
            self.after(0, apply)

        threading.Thread(target=work, daemon=True).start()

    def _provision_models(self):
        """One-time model-volume provisioning: launch runpod_provision.py
        setup-volume (create pod → fill the volume → auto-terminate) and stream
        its progress in a window. Reads config.json, so settings must be saved."""
        key = self.runpod_key_var.get().strip()
        if not key:
            self.runpod_status.configure(text="Enter a RunPod API key first.",
                                         foreground="#b3261e")
            return
        if not self._selected_volume_id():
            self.runpod_status.configure(text="Select or create a model volume first.",
                                         foreground="#b3261e")
            return
        # setup-volume reads config.json, not the live form — persist edits first.
        if self.is_dirty():
            if not messagebox.askokcancel(
                    APP_TITLE, "Provisioning reads your saved settings. Save the "
                               "current changes now and continue?"):
                return
            if not self._save():
                return
        if not messagebox.askyesno(
                APP_TITLE,
                "Provision the model volume now?\n\n"
                "This briefly rents a BILLED pod, downloads ~22 GB of models "
                "(SeedVR2 + Ollama) onto the selected volume, and terminates the "
                "pod automatically when done — usually 10-20 minutes. You only "
                "need to do this once per volume.\n\nProceed?"):
            return
        self._stream_provision()

    def _stream_provision(self):
        """Run the setup-volume subprocess and stream its output into a window."""
        win = tk.Toplevel(self)
        win.title("Provisioning the model volume")
        win.geometry("780x460")
        # Match the Batch Upscaler / Tag & Rename log console palette (LogPane):
        # dark background, light text, flat relief.
        win.configure(bg="#15181d")
        body = tk.Frame(win, bg="#15181d")
        body.pack(fill="both", expand=True, padx=8, pady=8)
        txt = tk.Text(body, wrap="word", font=("Consolas", 9), state="disabled",
                      background="#15181d", foreground="#d7dde4",
                      insertbackground="#d7dde4", relief="flat", padx=8, pady=6)
        sb = ttk.Scrollbar(body, command=txt.yview)
        txt.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        txt.pack(side="left", fill="both", expand=True)

        # A model download streams a tqdm/curl progress bar: thousands of updates
        # for a 15 GB file. Render it like a terminal — a carriage return rewrites
        # the current line instead of appending a fresh one — so the Text widget
        # stays small. Without this it grew unbounded until each insert/see got so
        # slow the Tk main loop starved and Windows flagged the window "Not
        # Responding". A hard line cap is a defensive backstop for tools that emit
        # a newline per update.
        MAX_LINES = 600
        # A "still working" heartbeat for the silent stretches: pip building/
        # installing ~30 packages, a big weight download, `ollama pull` — all emit
        # nothing for minutes, which looks identical to a hang. When the pod has
        # been quiet this long, show a single self-updating line so the user knows
        # it's alive (and isn't tempted to force-kill, which would orphan/strand
        # the provisioning pod).
        HB_AFTER = 10.0          # seconds of silence before the heartbeat appears
        HB_EVERY = 5.0           # refresh the heartbeat (its counter) this often
        last_activity = time.monotonic()
        last_hb = 0.0
        hb_active = False        # a heartbeat line is currently the last line

        def _clear_heartbeat():
            nonlocal hb_active
            if hb_active:
                txt.delete("end-1c linestart", "end-1c")
                hb_active = False

        def append(s):
            nonlocal last_activity
            txt.configure(state="normal")
            _clear_heartbeat()   # real output supersedes the transient heartbeat
            for token in re.split(r"(\r\n|\r|\n)", s):
                if not token:
                    continue
                if token in ("\n", "\r\n"):
                    txt.insert("end", "\n")
                elif token == "\r":
                    # Carriage return: drop the current (in-progress) line so the
                    # next text overwrites it — collapses a progress bar to one line.
                    txt.delete("end-1c linestart", "end-1c")
                else:
                    txt.insert("end", token)
            excess = int(txt.index("end-1c").split(".")[0]) - MAX_LINES
            if excess > 0:
                txt.delete("1.0", f"{excess + 1}.0")
            txt.see("end")
            txt.configure(state="disabled")
            last_activity = time.monotonic()

        def heartbeat():
            nonlocal hb_active, last_hb
            txt.configure(state="normal")
            if hb_active:
                txt.delete("end-1c linestart", "end-1c")   # overwrite the previous
            elif txt.get("end-1c linestart", "end-1c"):     # last line has content
                txt.insert("end", "\n")                     # give the heartbeat its own line
            secs = int(time.monotonic() - last_activity)
            txt.insert("end", f"  … still working on the pod — no new output for "
                              f"{secs}s. Large downloads and installs run silent; "
                              f"please wait …")
            hb_active = True
            last_hb = time.monotonic()
            txt.see("end")
            txt.configure(state="disabled")

        cmd = [PYTHON_EXE, "-u", os.path.join(SCRIPT_DIR, "runpod_provision.py"),
               "setup-volume"]
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"
        try:
            proc = subprocess.Popen(
                cmd, cwd=APP_ROOT, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                creationflags=CREATE_NO_WINDOW, env=env)
        except Exception as exc:
            append(f"Could not start provisioning: {exc}\n")
            return

        q = queue.Queue()

        def reader():
            dec = codecs.getincrementaldecoder("utf-8")("replace")
            for chunk in iter(lambda: proc.stdout.read1(4096), b""):
                q.put(dec.decode(chunk))
            q.put(None)

        def pump():
            done = False
            while True:
                try:
                    item = q.get_nowait()
                except queue.Empty:
                    break
                if item is None:
                    done = True
                    break
                append(item)
            if done:
                code = proc.wait()
                append(f"\n--- finished (exit {code}) ---\n")
                if code == 0:
                    self.runpod_status.configure(
                        text="Model volume provisioned — remote upscaling is ready.",
                        foreground="#1a7f37")
                else:
                    self.runpod_status.configure(
                        text="Provisioning failed — see the window for details.",
                        foreground="#b3261e")
            else:
                # No output for a while but the process is alive → reassure the user.
                now = time.monotonic()
                if (now - last_activity) >= HB_AFTER and (now - last_hb) >= HB_EVERY:
                    heartbeat()
                win.after(80, pump)

        threading.Thread(target=reader, daemon=True).start()
        win.after(80, pump)

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
        busy = [t for t in (self.app.upscale_tab, self.app.video_tab, self.app.tag_tab,
                            self.app.conciliate_tab) if t.running]
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

class VideoTab(ttk.Frame):
    """The Video Upscaler tab (#2, phase 5). RunPod-only. A two-list setup flow
    (scan list -> Prepare -> a durable queue), a cheapest-first GPU picker with a
    live cost estimate, and a frames-based running view. Standalone (not a ToolTab)
    because its shape is very different from the image tabs; it reuses the runner's
    GPU-free helpers (batch_video_upscale) and the cost estimator (video_estimate)
    in-process, and drives the queue via the batch_video_upscale subprocess.
    See docs/video-upscaler.md section 15."""

    def __init__(self, notebook, app):
        super().__init__(notebook, padding=(12, 10))
        self.app = app
        self.proc = None
        self._queue_io = queue.Queue()
        self._hold = ""
        self._marker_buf = None
        self.console = ConsoleBuffer()
        self.tool_name = "Video Upscaler"
        self.active_pod_id = None
        self._gpu_choices = []
        self._scan_rows = {}        # tree iid -> dict(rel, abs, width, height, ...)
        self._root_id = None
        self._src_root = None
        self._out_root = None
        # Scan progress (incremental, off the UI thread).
        self._scan_seq = 0
        self._scanning = False
        self._scan_q = None
        self._scan_total = 0
        self._scan_done = 0
        self._scan_start = None
        # Run progress state (frames-based, 15.8).
        self._run_total = 0
        self._run_done = 0
        self._cur_seg_frames = 0
        self._cur_seg_done = 0
        self._run_start = None
        self._rate = None           # observed s/frame, refined from done segments
        self._run_tick_job = None
        self._build()
        self.after(200, self._check_readiness)
        self.after(300, self._load_queue)

    # ── config / db ──────────────────────────────────────────────────────────

    def _conn(self):
        import db
        return db.get_conn()

    def _vcfg(self):
        import batch_video_upscale as bv
        return bv.resolve_video_cfg(CFG)

    # ── layout ───────────────────────────────────────────────────────────────

    def _build(self):
        self.columnconfigure(0, weight=1)

        # 1) Remote-readiness strip.
        self.ready_var = tk.StringVar(value="Checking remote readiness …")
        self.ready_lbl = tk.Label(self, textvariable=self.ready_var, anchor="w",
                                  fg="#7f8a99", font=("Segoe UI", 9))
        self.ready_lbl.grid(row=0, column=0, sticky="ew", pady=(0, 6))

        # 2) Source / output folders.
        ff = ttk.Frame(self)
        ff.grid(row=1, column=0, sticky="ew")
        ff.columnconfigure(1, weight=1)
        ttk.Label(ff, text="Video folder:").grid(row=0, column=0, sticky="w")
        self.src_var = tk.StringVar()
        ttk.Entry(ff, textvariable=self.src_var).grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Button(ff, text="Browse…", command=self._browse_source).grid(row=0, column=2)
        self.scan_btn = ttk.Button(ff, text="Scan", command=self._scan)
        self.scan_btn.grid(row=0, column=3, padx=(6, 0))
        ttk.Label(ff, text="Save upscaled to:").grid(row=1, column=0, sticky="w", pady=(4, 0))
        self.out_var = tk.StringVar()
        ttk.Entry(ff, textvariable=self.out_var).grid(row=1, column=1, columnspan=2,
                                                      sticky="ew", padx=6, pady=(4, 0))

        # 3) Scan list.
        sf = ttk.LabelFrame(self, text=" Videos in this folder ", padding=4)
        sf.grid(row=2, column=0, sticky="nsew", pady=(8, 0))
        self.rowconfigure(2, weight=3)
        sf.rowconfigure(0, weight=1)
        sf.columnconfigure(0, weight=1)
        cols = ("res", "dur", "codec", "fps", "up", "upres", "status")
        self.scan_tree = ttk.Treeview(sf, columns=cols, show="tree headings", height=7)
        self.scan_tree.heading("#0", text="File")
        self.scan_tree.column("#0", width=240, stretch=True)
        for c, txt, w in (("res", "Resolution", 90), ("dur", "Duration", 70),
                          ("codec", "Codec", 70), ("fps", "FPS", 55),
                          ("up", "Upscaled", 150), ("upres", "Up res", 80),
                          ("status", "Status", 80)):
            self.scan_tree.heading(c, text=txt)
            self.scan_tree.column(c, width=w, stretch=False, anchor="w")
        self.scan_tree.tag_configure("haveup", foreground="#2f6f3f")
        self.scan_tree.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(sf, orient="vertical", command=self.scan_tree.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.scan_tree.configure(yscrollcommand=sb.set)
        self.scan_tree.bind("<<TreeviewSelect>>", self._on_scan_select)
        self.scan_tree.bind("<Double-1>", self._on_scan_double)
        self.scan_tree.bind("<Button-3>", self._on_scan_right)

        # 4) Source file + target + Prepare.
        pf = ttk.Frame(self)
        pf.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        pf.columnconfigure(1, weight=1)
        ttk.Label(pf, text="Source file:").grid(row=0, column=0, sticky="w")
        self.srcfile_var = tk.StringVar()
        ttk.Entry(pf, textvariable=self.srcfile_var, state="readonly").grid(
            row=0, column=1, sticky="ew", padx=6)
        ttk.Label(pf, text="Target:").grid(row=0, column=2)
        self.target_var = tk.StringVar()
        self.target_combo = ttk.Combobox(pf, textvariable=self.target_var,
                                         state="readonly", width=8, values=[])
        self.target_combo.grid(row=0, column=3, padx=4)
        self.target_combo.bind("<<ComboboxSelected>>", lambda _e: self._sync_prepare_btn())
        self.prepare_btn = ttk.Button(pf, text="Prepare ▾ add to queue",
                                     command=self._prepare, state="disabled")
        self.prepare_btn.grid(row=0, column=4, padx=(6, 0))

        # 5) Queue list.
        qf = ttk.LabelFrame(self, text=" Upscale queue ", padding=4)
        qf.grid(row=4, column=0, sticky="nsew", pady=(8, 0))
        self.rowconfigure(4, weight=2)
        qf.rowconfigure(0, weight=1)
        qf.columnconfigure(0, weight=1)
        qcols = ("target", "status", "frames", "segs")
        self.queue_tree = ttk.Treeview(qf, columns=qcols, show="tree headings", height=5)
        self.queue_tree.heading("#0", text="File")
        self.queue_tree.column("#0", width=240, stretch=True)
        for c, txt, w in (("target", "Target", 70), ("status", "Status", 90),
                          ("frames", "Frames", 80), ("segs", "Segments", 80)):
            self.queue_tree.heading(c, text=txt)
            self.queue_tree.column(c, width=w, stretch=False, anchor="w")
        self.queue_tree.grid(row=0, column=0, rowspan=4, sticky="nsew")
        qsb = ttk.Scrollbar(qf, orient="vertical", command=self.queue_tree.yview)
        qsb.grid(row=0, column=1, rowspan=4, sticky="ns")
        self.queue_tree.configure(yscrollcommand=qsb.set)
        ttk.Button(qf, text="↑", width=3, command=lambda: self._queue_move(-1)).grid(row=0, column=2, padx=(4, 0))
        ttk.Button(qf, text="↓", width=3, command=lambda: self._queue_move(1)).grid(row=1, column=2, padx=(4, 0))
        ttk.Button(qf, text="Remove", command=self._queue_remove).grid(row=2, column=2, padx=(4, 0), pady=(4, 0))

        # 6) GPU picker + estimate.
        gf = ttk.Frame(self)
        gf.grid(row=5, column=0, sticky="ew", pady=(8, 0))
        gf.columnconfigure(5, weight=1)
        ttk.Label(gf, text="GPU:").grid(row=0, column=0, sticky="w")
        self.gpu_var = tk.StringVar()
        self.gpu_combo = ttk.Combobox(gf, textvariable=self.gpu_var, state="readonly",
                                     width=40, values=[])
        self.gpu_combo.grid(row=0, column=1, padx=4)
        self.gpu_combo.bind("<<ComboboxSelected>>", lambda _e: self._update_estimate())
        ttk.Button(gf, text="↻", width=3, command=self._refresh_gpus).grid(row=0, column=2)
        self.estimate_var = tk.StringVar(value="Add videos to the queue for an estimate.")
        ttk.Label(gf, textvariable=self.estimate_var, anchor="w",
                  foreground="#2f6f3f").grid(row=0, column=5, sticky="ew", padx=(12, 0))

        # 7) Start / Stop + progress + status.
        af = ttk.Frame(self)
        af.grid(row=6, column=0, sticky="ew", pady=(8, 0))
        af.columnconfigure(2, weight=1)
        self.start_btn = ttk.Button(af, text="Start Upscaling", command=self._start)
        self.start_btn.grid(row=0, column=0)
        self.stop_btn = ttk.Button(af, text="Stop", command=self._stop, state="disabled")
        self.stop_btn.grid(row=0, column=1, padx=(6, 0))
        self.progress = ProgressBar(af, width=200)
        self.progress.grid(row=0, column=2, sticky="ew", padx=12)
        ttk.Button(af, text="View log", command=self._view_log).grid(row=0, column=3)
        self.status_var = tk.StringVar(value="Ready.")
        tk.Label(self, textvariable=self.status_var, anchor="w", fg="#7f8a99",
                 font=("Consolas", 9)).grid(row=7, column=0, sticky="ew", pady=(4, 0))

        # 8) Remote-pod telemetry (CPU/RAM/GPU). Created hidden; App.apply_remote_
        # telemetry reveals it on the first RTELEM sample of a run and _end_run
        # hides it again, so it only shows while a pod is actually streaming.
        self.remote_telemetry_row = TelemetryRow(self, prefix="Remote pod")
        self.remote_telemetry_row.grid(row=8, column=0, sticky="ew", pady=(2, 0))
        self.remote_telemetry_row.grid_remove()

    # ── readiness ────────────────────────────────────────────────────────────

    def restore_defaults_if_empty(self):
        if not self.src_var.get():
            d = get_default_folder("video_source")
            if d:
                self.src_var.set(os.path.normpath(d))
        if not self.out_var.get():
            d = get_default_folder("video_output")
            if d:
                self.out_var.set(os.path.normpath(d))

    def on_enter(self):
        """Called when the tab is entered (not only at startup): re-check remote
        readiness so a RunPod API key / SSH key / volume set after launch is seen,
        and refresh the durable queue."""
        self.restore_defaults_if_empty()
        self._check_readiness()
        self._load_queue()

    def _check_readiness(self):
        rpc = CFG.get("runpod", {})

        def work():
            msg, ok = self._readiness_text(rpc)
            self.after(0, lambda: self._set_ready(msg, ok))

        threading.Thread(target=work, daemon=True).start()

    def _readiness_text(self, rpc):
        if not rpc.get("api_key"):
            return "Not ready: set a RunPod API key (RunPod tab).", False
        try:
            import ssh_setup
            key = os.path.expandvars(rpc.get("ssh_key_path", "")) or ssh_setup.default_key_path()
        except Exception:
            key = os.path.expandvars(rpc.get("ssh_key_path", ""))
        if not (key and os.path.exists(key)):
            return "Not ready: no SSH key — use 'Set up SSH key' (Settings).", False
        vol = (rpc.get("network_volume_id") or "").strip()
        if not vol:
            return "Not ready: no model network volume configured (Settings).", False
        try:
            import runpod_client as rp
            region = rp.volume_region(rpc["api_key"], vol)
        except Exception as exc:
            return f"Not ready: could not reach RunPod ({exc}).", False
        if not region:
            return "Not ready: configured network volume not found.", False
        return f"Remote ready — models in {region}.", True

    def _set_ready(self, msg, ok):
        self.ready_var.set(msg)
        self.ready_lbl.configure(fg="#2f6f3f" if ok else "#b23b3b")
        self._ready = ok

    # ── browse / scan ────────────────────────────────────────────────────────

    def _browse_source(self):
        folder = filedialog.askdirectory(title="Choose the folder with videos to upscale")
        if folder:
            # askdirectory returns forward slashes on Windows; normpath gives the
            # native backslash form so both fields read consistently (no X:/a\b mix).
            folder = os.path.normpath(folder)
            self.src_var.set(folder)
            out = self.out_var.get().strip()
            if not out or os.path.basename(out) == "__upscaled__":
                self.out_var.set(os.path.join(folder, "__upscaled__"))

    def _scan(self):
        src = self.src_var.get().strip()
        if not src or not os.path.isdir(src):
            messagebox.showwarning(APP_TITLE, "Choose a valid video folder first.")
            return
        if self._scanning:
            return
        if not self.out_var.get().strip():
            self.out_var.set(os.path.join(src, "__upscaled__"))
        self._src_root = os.path.abspath(src)
        self._out_root = os.path.abspath(self.out_var.get().strip())
        # Reflect the canonical (backslash) form back into the fields, and remember
        # BOTH folders so a relaunch restores them together (output used to be lost).
        self.src_var.set(self._src_root)
        self.out_var.set(self._out_root)
        set_default_folder("video_source", self._src_root)
        set_default_folder("video_output", self._out_root)
        self.scan_tree.delete(*self.scan_tree.get_children())
        self._scan_rows.clear()
        self.progress.set(0)
        self._scanning = True
        self._scan_total = self._scan_done = 0
        self._scan_skipped = []          # rels ffprobe could not read
        self._scan_res = {}              # "WxH" -> count, for the summary breakdown
        self._scan_listing = 0           # files found so far during the tree walk
        self._scan_seq += 1
        seq = self._scan_seq
        self._scan_q = queue.Queue()
        self.scan_btn.configure(state="disabled")
        self.status_var.set("Listing files …")
        self.console.feed(f"Scanning {self._src_root} …\n")
        threading.Thread(target=lambda: self._scan_worker(seq), daemon=True).start()
        self.after(120, lambda: self._scan_poll(seq))

    def _scan_worker(self, seq):
        """Walk + probe each video off the UI thread, streaming results to the
        poller via a queue so the list/progress fill in live. ffprobe is cached by
        (mtime, size), so a re-scan of an unchanged tree races through.

        Walking a large tree on a network drive can take many seconds, so we
        iterate the walk lazily and stream a running 'found N' count while it runs
        (otherwise the first feedback is the long-delayed total)."""
        import batch_video_upscale as bv
        try:
            conn = self._conn()
            self._root_id = bv.db.get_video_root_id(conn, self._src_root, self._out_root)
            cutoff = self._vcfg()["skip_cutoff_pct"]
            files = []
            for abs_path, rel in bv.iter_videos(self._src_root):
                if seq != self._scan_seq:
                    return
                files.append((abs_path, rel))
                if len(files) % 25 == 0:
                    self._scan_q.put(("listing", len(files)))
            files.sort(key=lambda t: t[1].lower())
            self._scan_q.put(("total", len(files)))
            for abs_path, rel in files:
                if seq != self._scan_seq:
                    return                       # a newer scan superseded this one
                r = bv.scan_file(conn, self._root_id, abs_path, rel)
                if r is None:
                    self._scan_q.put(("skip", rel))
                    continue
                elig = bv.eligible_targets(r["width"], r["height"], cutoff)
                outs = [(o["target"], o["status"], o["output_path"])
                        for o in bv.db.get_video_outputs_for(conn, self._root_id, rel)]
                self._scan_q.put(("row", (rel, abs_path, dict(r), elig, outs)))
            self._scan_q.put(("done", None))
        except Exception as exc:                 # noqa: BLE001 (UI thread surfaces it)
            self._scan_q.put(("error", str(exc)))

    def _scan_poll(self, seq):
        if seq != self._scan_seq:
            return
        import video_estimate as ve
        drained = 0
        while drained < 400:                     # cap per cycle: stay responsive on
            try:                                 # a fast cached re-scan of a huge tree
                kind, data = self._scan_q.get_nowait()
            except queue.Empty:
                break
            drained += 1
            if kind == "listing":
                self._scan_listing = data
                self.status_var.set(f"Listing files … {data} found")
            elif kind == "total":
                self._scan_total = data
                self._scan_start = time.time()
                self.console.feed(f"Found {data} video file(s); reading properties …\n")
                if data == 0:
                    self.status_var.set("No videos found in this folder.")
            elif kind == "row":
                self._insert_scan_row(*data)
                self._scan_done += 1
                w, h = data[2]["width"], data[2]["height"]
                key = f"{w}×{h}" if w and h else "unknown"
                self._scan_res[key] = self._scan_res.get(key, 0) + 1
                self.console.feed(f"  {data[0]}  ({w}×{h}, "
                                  f"{data[2]['vcodec']})\n")
            elif kind == "skip":
                self._scan_done += 1
                self._scan_skipped.append(data)
                self.console.feed(f"  {data}  (unreadable, skipped)\n")
            elif kind == "error":
                self.status_var.set(f"Scan failed: {data}")
                self._finish_scan()
                return
            elif kind == "done":
                self._finish_scan()
                return
        if self._scan_total > 0:
            self.progress.set(100.0 * self._scan_done / self._scan_total)
            el = time.time() - (self._scan_start or time.time())
            eta = (self._scan_total - self._scan_done) * (el / self._scan_done) \
                if self._scan_done > 0 else None
            tail = f" · ETA {ve.fmt_duration(eta)}" if eta else ""
            self.status_var.set(f"Scanning {self._scan_done}/{self._scan_total}{tail}")
        # If the cap was hit there is likely more waiting — poll again promptly.
        self.after(1 if drained >= 400 else 120, lambda: self._scan_poll(seq))

    def _insert_scan_row(self, rel, abs_path, r, elig, outs):
        import video_estimate as ve
        res = f"{r['width']}x{r['height']}" if r["width"] else "?"
        dur = ve.fmt_duration(r["duration"]) if r["duration"] else "?"
        fps = f"{r['fps']:.2f}" if r["fps"] else "?"
        done = [o for o in outs if o[1] == "done"]
        up = ", ".join(t for t, _s, _p in done)
        iid = self.scan_tree.insert(
            "", "end", text=rel,
            values=(res, dur, r["vcodec"] or "?", fps, up, up, ""),
            tags=("haveup",) if done else ())
        self._scan_rows[iid] = {"rel": rel, "abs": abs_path, "elig": elig, "outs": outs}

    def _finish_scan(self):
        self._scanning = False
        self.scan_btn.configure(state="normal")
        n = len(self._scan_rows)
        skipped = getattr(self, "_scan_skipped", [])
        res = getattr(self, "_scan_res", {})
        self.progress.set(100 if self._scan_total else 0)
        tail = f", {len(skipped)} unreadable (skipped)" if skipped else ""
        self.status_var.set(f"Found {n} video(s){tail}." if n
                            else "No videos found in this folder.")
        # A clearly delimited summary block (find-able after the long per-file run):
        # totals, then a count grouped by resolution, then the skipped files.
        bar = "=" * 56
        self.console.feed(f"\n{bar}\n")
        self.console.feed("Scan summary\n")
        self.console.feed(f"  Videos ready to queue: {n}\n")
        if skipped:
            self.console.feed(f"  Unreadable (skipped):  {len(skipped)}\n")
        if res:
            self.console.feed("  By resolution:\n")
            for key, cnt in sorted(res.items(), key=lambda kv: (-kv[1], kv[0])):
                self.console.feed(f"     {key:<13}{cnt}\n")
        if skipped:
            self.console.feed("  Unreadable files:\n")
            for rel in skipped:
                self.console.feed(f"     {rel}\n")
        self.console.feed(f"{bar}\n")

    def _done_targets(self, rel):
        """Targets already upscaled for `rel`, read LIVE from the DB (not the scan
        cache) so a job that finished after the scan still disables re-Prepare."""
        if self._root_id is None:
            return set()
        import batch_video_upscale as bv
        return {o["target"] for o in bv.db.get_video_outputs_for(self._conn(), self._root_id, rel)
                if o["status"] == "done"}

    def _on_scan_select(self, _e=None):
        sel = self.scan_tree.selection()
        if not sel:
            return
        row = self._scan_rows.get(sel[0])
        if not row:
            return
        self.srcfile_var.set(row["abs"])
        done_targets = self._done_targets(row["rel"])
        self.target_combo.configure(values=row["elig"])
        if row["elig"]:
            # default to the first eligible target not already done
            nxt = next((t for t in row["elig"] if t not in done_targets), row["elig"][0])
            self.target_var.set(nxt)
        else:
            self.target_var.set("")
        self._sync_prepare_btn()

    def _sync_prepare_btn(self):
        sel = self.scan_tree.selection()
        row = self._scan_rows.get(sel[0]) if sel else None
        target = self.target_var.get()
        ok = bool(row and target)
        if ok and target in self._done_targets(row["rel"]):
            ok = False                           # already upscaled to this target (15.1 step 5)
        self.prepare_btn.configure(state="normal" if ok else "disabled")

    def _on_scan_double(self, event):
        iid = self.scan_tree.identify_row(event.y)
        if not iid:
            return
        row = self._scan_rows.get(iid)
        if not row:
            return
        col = self.scan_tree.identify_column(event.x)
        # Double-click the "Upscaled" cell (#5) -> compare; else open the source.
        done = [o for o in row["outs"] if o[1] == "done"]
        if col == "#5" and done:
            self._open_compare(row["abs"], done[0][2])
        else:
            self._open_path(row["abs"])

    def _on_scan_right(self, event):
        iid = self.scan_tree.identify_row(event.y)
        if not iid:
            return
        self.scan_tree.selection_set(iid)
        row = self._scan_rows.get(iid)
        if not row:
            return
        m = tk.Menu(self, tearoff=0)
        m.add_command(label="Open source video", command=lambda: self._open_path(row["abs"]))
        m.add_command(label="Open source folder",
                      command=lambda: self._open_folder(row["abs"]))
        for t, s, p in row["outs"]:
            if s == "done" and p:
                m.add_command(label=f"Compare ({t})",
                              command=lambda p=p: self._open_compare(row["abs"], p))
                m.add_command(label=f"Open upscaled ({t})",
                              command=lambda p=p: self._open_path(p))
        try:
            m.tk_popup(event.x_root, event.y_root)
        finally:
            m.grab_release()

    def _open_path(self, p):
        try:
            os.startfile(p)
        except Exception as exc:                         # noqa: BLE001
            messagebox.showerror(APP_TITLE, f"Could not open:\n{p}\n{exc}")

    def _open_folder(self, p):
        try:
            subprocess.Popen(["explorer", "/select,", os.path.normpath(p)])
        except Exception:
            self._open_path(os.path.dirname(p))

    def _open_compare(self, src, up):
        """Open/reuse the shared original-vs-upscaled video comparison window."""
        if not (up and os.path.exists(up)):
            self._open_path(src)
            return
        win = getattr(self.app, "video_comparison_window", None)
        if win is not None and win.winfo_exists():
            win.show_videos(src, up)
            win.deiconify()
            win.lift()
        else:
            self.app.video_comparison_window = VideoComparisonWindow(
                self.app, src, up, app=self.app)

    # ── prepare / queue ──────────────────────────────────────────────────────

    def _prepare(self):
        sel = self.scan_tree.selection()
        row = self._scan_rows.get(sel[0]) if sel else None
        target = self.target_var.get()
        if not row or not target:
            return
        self.prepare_btn.configure(state="disabled")
        self.status_var.set(f"Preparing {os.path.basename(row['abs'])} → {target} …")

        def work():
            import batch_video_upscale as bv
            try:
                info = bv.prepare_job(self._conn(), self._root_id, self._src_root,
                                      self._out_root, row["rel"], target, self._vcfg())
            except Exception as exc:                     # noqa: BLE001
                self.after(0, lambda e=exc: self.status_var.set(f"Prepare failed: {e}"))
                return
            self.after(0, lambda: self._after_prepare(row["rel"], target, info))

        threading.Thread(target=work, daemon=True).start()

    def _after_prepare(self, rel, target, info):
        self.status_var.set(f"Queued {os.path.basename(rel)} → {target} "
                            f"({info['nb_frames']} frames, ~{info['segments']} segments).")
        self._load_queue()
        self._sync_prepare_btn()
        self._update_estimate()

    def _load_queue(self):
        """Populate the queue list from the DB (survives restarts, 15.1)."""
        import batch_video_upscale as bv
        conn = self._conn()
        if self._root_id is None:
            src = self.src_var.get().strip()
            if src and os.path.isdir(src):
                self._src_root = os.path.abspath(src)
                self._out_root = os.path.abspath(self.out_var.get().strip()
                                                 or os.path.join(self._src_root, "__upscaled__"))
                self._root_id = bv.db.get_video_root_id(conn, self._src_root, self._out_root)
            else:
                return
        self.queue_tree.delete(*self.queue_tree.get_children())
        for j in bv.db.get_video_queue(conn, self._root_id):
            vf = bv.db.get_video_file(conn, self._root_id, j["rel_path"])
            frames = (vf["nb_frames"] if vf else None) or "?"
            segs = bv.db.get_video_segments(conn, self._root_id, j["rel_path"], j["target"])
            done = sum(1 for s in segs if s["status"] == "done")
            segtxt = f"{done}/{len(segs)}" if segs else "?"
            self.queue_tree.insert("", "end",
                                   text=j["rel_path"],
                                   values=(j["target"], j["status"], frames, segtxt),
                                   tags=(j["rel_path"], j["target"]))
        self._refresh_scan_outputs()
        self._update_estimate()

    def _refresh_scan_outputs(self):
        """Re-read the scan rows' upscaled counterparts so the 'Upscaled' columns
        and the green tag stay current after a run (without a full re-scan). Uses a
        SINGLE query for the whole root, not one per file — a per-row query froze
        the UI thread on a large tree (1000+ files)."""
        if self._root_id is None or not self._scan_rows:
            return
        import batch_video_upscale as bv
        by_rel = {}
        for o in bv.db.get_video_outputs_all(self._conn(), self._root_id):
            by_rel.setdefault(o["rel_path"], []).append(
                (o["target"], o["status"], o["output_path"]))
        for iid, row in self._scan_rows.items():
            outs = by_rel.get(row["rel"], [])
            if outs == row.get("outs"):
                continue                      # unchanged — skip the tree write
            row["outs"] = outs
            done = [o for o in outs if o[1] == "done"]
            up = ", ".join(t for t, _s, _p in done)
            vals = list(self.scan_tree.item(iid, "values"))
            vals[4] = up           # Upscaled
            vals[5] = up           # Up res (target == short side, shown as the target)
            self.scan_tree.item(iid, values=vals, tags=("haveup",) if done else ())

    def _selected_queue_job(self):
        sel = self.queue_tree.selection()
        if not sel:
            return None
        tags = self.queue_tree.item(sel[0], "tags")
        return (tags[0], tags[1]) if len(tags) >= 2 else None

    def _queue_remove(self):
        job = self._selected_queue_job()
        if not job:
            return
        import batch_video_upscale as bv
        bv.db.delete_video_output(self._conn(), self._root_id, job[0], job[1])
        self._load_queue()

    def _queue_move(self, delta):
        job = self._selected_queue_job()
        if not job or self._root_id is None:
            return
        import batch_video_upscale as bv
        conn = self._conn()
        jobs = list(bv.db.get_video_queue(conn, self._root_id))
        idx = next((i for i, j in enumerate(jobs)
                    if j["rel_path"] == job[0] and j["target"] == job[1]), -1)
        ni = idx + delta
        if idx < 0 or ni < 0 or ni >= len(jobs):
            return
        # Swap queue_order with the neighbour, then renormalise positions.
        order = [(j["rel_path"], j["target"]) for j in jobs]
        order[idx], order[ni] = order[ni], order[idx]
        for pos, (rel, tgt) in enumerate(order):
            bv.db.set_queue_order(conn, self._root_id, rel, tgt, pos)
        self._load_queue()
        # Re-select the moved row.
        for iid in self.queue_tree.get_children():
            t = self.queue_tree.item(iid, "tags")
            if len(t) >= 2 and t[0] == job[0] and t[1] == job[1]:
                self.queue_tree.selection_set(iid)
                break

    # ── GPU picker + estimate ────────────────────────────────────────────────

    def _refresh_gpus(self):
        rpc = CFG.get("runpod", {})
        if not rpc.get("api_key"):
            self.gpu_var.set("(set a RunPod API key)")
            return
        self.gpu_var.set("loading GPUs …")
        self.gpu_combo.configure(values=[])

        def work():
            try:
                import batch_video_upscale as bv
                import runpod_client as rp
                import video_estimate as ve
                key = rpc["api_key"]
                vol = (rpc.get("network_volume_id") or "").strip()
                dc = rp.volume_region(key, vol) if vol else None
                floor = ve.max_target_floor(self._queue_jobs()) or 24
                gpus = rp.available_gpus(key, dc, min_memory_gb=floor)
                self.after(0, lambda: self._populate_gpus(gpus))
            except Exception as exc:                     # noqa: BLE001
                self.after(0, lambda e=exc: self.gpu_var.set(f"GPU list failed: {e}"))

        threading.Thread(target=work, daemon=True).start()

    def _populate_gpus(self, gpus):
        import video_estimate as ve
        jobs = self._queue_jobs()
        ranked = ve.recommend_gpus(gpus, jobs, self._spin_up(),
                                   conn=self._conn()) if jobs else []
        self._gpu_choices = ranked or gpus
        labels = []
        for g in self._gpu_choices:
            price = f"${g['price']:.2f}/h" if g.get("price") is not None else "n/a"
            est = g.get("estimate")
            tail = f" → ${est['cost']:.2f}" if est else ""
            labels.append(f"{g['name']} — {g['memory_gb']} GB — {price} ({g['stock']}){tail}")
        self.gpu_combo.configure(values=labels)
        if labels:
            self.gpu_combo.current(0)
        else:
            self.gpu_var.set("no eligible GPU available right now")
        self._update_estimate()

    def _selected_gpu(self):
        if not self._gpu_choices:
            return None
        i = self.gpu_combo.current()
        return self._gpu_choices[i if 0 <= i < len(self._gpu_choices) else 0]

    def _queue_jobs(self):
        """The queue as estimator job dicts [{frames, target, segments}]."""
        import batch_video_upscale as bv
        if self._root_id is None:
            return []
        conn = self._conn()
        jobs = []
        for j in bv.db.get_video_queue(conn, self._root_id):
            vf = bv.db.get_video_file(conn, self._root_id, j["rel_path"])
            frames = (vf["nb_frames"] if vf else 0) or 0
            dur = (vf["duration"] if vf else 0) or 0
            seg_secs = self._vcfg()["segment_seconds"]
            import math as _m
            segs = max(1, _m.ceil(dur / seg_secs)) if seg_secs else 1
            jobs.append({"frames": frames, "target": j["target"], "segments": segs})
        return jobs

    def _spin_up(self):
        return float(CFG.get("video", {}).get("spin_up_seconds", 360))

    def _update_estimate(self):
        import video_estimate as ve
        jobs = self._queue_jobs()
        if not jobs:
            self.estimate_var.set("Add videos to the queue for an estimate.")
            return
        g = self._selected_gpu()
        if not g:
            self.estimate_var.set(f"{len(jobs)} job(s) queued — pick a GPU (↻).")
            return
        est = ve.estimate_queue(jobs, g.get("id") or g.get("name"), g.get("price"),
                                self._spin_up(), conn=self._conn())
        if not est:
            self.estimate_var.set(f"{g['name']} can't serve every queued target.")
            return
        self.estimate_var.set(
            f"{len(jobs)} job(s) · {ve.fmt_duration(est['duration_seconds'])} · "
            f"${est['cost']:.2f} · {est['segments']} segments · "
            f"${est['cost_per_segment']:.3f}/segment")

    # ── start / stop / run ───────────────────────────────────────────────────

    def _start(self):
        if self.proc is not None:
            return
        if not getattr(self, "_ready", False):
            messagebox.showwarning(APP_TITLE, self.ready_var.get())
            return
        jobs = self._queue_jobs()
        if not jobs:
            messagebox.showinfo(APP_TITLE, "The queue is empty. Prepare a video first.")
            return
        g = self._selected_gpu()
        if not g:
            messagebox.showwarning(APP_TITLE, "Pick a GPU (press ↻ to load the list).")
            return
        import video_estimate as ve
        est = ve.estimate_queue(jobs, g.get("id") or g.get("name"), g.get("price"),
                                self._spin_up(), conn=self._conn())
        if CFG.get("video", {}).get("confirm_before_rent", True):
            cost = f"${est['cost']:.2f}" if est else "?"
            dur = ve.fmt_duration(est["duration_seconds"]) if est else "?"
            if not messagebox.askyesno(
                    APP_TITLE,
                    f"Rent {g['name']} (${g.get('price', 0):.2f}/h) and upscale "
                    f"{len(jobs)} job(s)?\n\nEstimated: {dur}, {cost}.\n\n"
                    "A billed pod is created and torn down when done."):
                return
        # Pass ONLY the selected GPU — never silently fall back to a different GPU
        # TYPE. If it can't be deployed (sold out / unavailable by start time), the
        # run fails with a clear message and the user refreshes (↻) and picks
        # another card themselves.
        env = {"IMGTBX_GPU_OVERRIDE": g["id"]} if g.get("id") else None
        self._begin_run(sum(j["frames"] for j in jobs))
        self._launch("batch_video_upscale.py", [self._src_root, self._out_root], env)

    def _begin_run(self, total_frames):
        self._run_total = total_frames
        self._run_done = 0
        self._cur_seg_frames = self._cur_seg_done = 0
        self._run_start = time.time()
        self._rate = None
        self.progress.set(0)
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.status_var.set("Starting pod …")
        self.console.clear()
        self.app.taskbar_state("indeterminate")
        if self._run_tick_job is None:
            self._run_tick_job = self.after(1000, self._run_tick)

    def _launch(self, script, args, extra_env=None):
        cmd = [PYTHON_EXE, "-u", os.path.join(SCRIPT_DIR, script)] + list(args)
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"
        if extra_env:
            env.update(extra_env)
        try:
            self.proc = subprocess.Popen(
                cmd, cwd=APP_ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, creationflags=CREATE_NO_WINDOW, env=env)
        except Exception as exc:                         # noqa: BLE001
            messagebox.showerror(APP_TITLE, f"Could not start the runner:\n{exc}")
            self._end_run()
            return
        self._hold = ""
        self._marker_buf = None
        threading.Thread(target=self._pump, daemon=True).start()
        self.after(50, self._poll)

    @property
    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def send(self, line):
        """Send a control line to the runner's stdin (App._on_close uses this)."""
        if self.proc and self.proc.stdin:
            try:
                self.proc.stdin.write((line + "\n").encode("utf-8"))
                self.proc.stdin.flush()
            except Exception:
                pass

    def terminate(self):
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.kill()
            except Exception:
                pass

    def _stop(self):
        if self.running:
            self.send("q")
            self.status_var.set("Stopping after the current segment …")
        self.stop_btn.configure(state="disabled")

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
            code = self.proc.wait() if self.proc else 0
            self.proc = None
            self.on_exit(code)
        elif self.proc is not None:
            self.after(50, self._poll)

    def _filter_markers(self, text):
        """Strip @@TBX@@KIND|payload lines and dispatch them; same parser shape as
        ToolTab (markers can arrive mid-line from a background thread)."""
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
        self._handle_event(kind, data, payload)

    def _handle_event(self, kind, data, payload):
        if kind == "POD":
            self.active_pod_id = payload or None
            self.app.notify_active_pods_changed()
        elif kind == "VIDEO" and data:
            self._cur_rel = data.get("rel")
            self._cur_target = data.get("target")
            self._cur_seg_frames = self._cur_seg_done = 0
            self.status_var.set(f"Upscaling {os.path.basename(data.get('rel',''))} "
                                f"→ {data.get('target')} "
                                f"[{data.get('index')}/{data.get('total')}]")
        elif kind == "SEGMENT" and data:
            seg_frames = data.get("seg_frames") or 0
            fp = data.get("frames_processed")
            self._cur_seg_frames = seg_frames
            if data.get("state") == "done":
                self._run_done += seg_frames
                self._cur_seg_done = 0
            elif fp is not None:
                self._cur_seg_done = min(fp, seg_frames)
            self._update_progress()
        elif kind == "VRESULT" and data:
            self._load_queue()                           # done/failed leaves the queue
        elif kind == "RTELEM" and data:
            self.app.apply_remote_telemetry(self, data)

    def _update_progress(self):
        if self._run_total <= 0:
            return
        done = self._run_done + self._cur_seg_done
        self.progress.set(100.0 * done / self._run_total)
        self.app.taskbar_progress(done, self._run_total)

    def _run_tick(self):
        """1 s heartbeat during a run: refine the rate, smooth a time-based bar when
        the worker isn't reporting frames_processed, and show ETA."""
        if self.proc is None:
            self._run_tick_job = None
            return
        elapsed = time.time() - (self._run_start or time.time())
        done = self._run_done + self._cur_seg_done
        if done > 0 and elapsed > 0:
            self._rate = elapsed / done
        if self._run_total > 0:
            remaining = max(0, self._run_total - done)
            eta = remaining * self._rate if self._rate else None
            import video_estimate as ve
            tail = f" · ETA {ve.fmt_duration(eta)}" if eta else ""
            self.status_var.set(
                getattr(self, "_cur_status", self.status_var.get()).split(" · ETA")[0] + tail)
        self._run_tick_job = self.after(1000, self._run_tick)

    def on_exit(self, code):
        self._end_run()
        self.app.taskbar_clear()
        self.app.flash_attention()
        self._load_queue()
        if code == 0:
            self.status_var.set("Run finished.")
        else:
            # A common cause is the picked GPU selling out between picker-refresh
            # and Start (we never substitute a different card). Point the user at
            # the manual re-pick rather than guessing for them.
            self.status_var.set("Run failed (see View log). If the GPU is no longer "
                                "available, press ↻ to refresh the list and pick another.")

    def _end_run(self):
        self.proc = None
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        if self._run_tick_job is not None:
            self.after_cancel(self._run_tick_job)
            self._run_tick_job = None
        # The remote-pod telemetry row only makes sense during a remote run.
        if self.remote_telemetry_row.winfo_manager():
            self.remote_telemetry_row.grid_remove()

    def _view_log(self):
        self.app.show_log(self.console, f"{APP_TITLE} — Video Upscaler output")

    def on_exit_app(self):
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
            except Exception:
                pass


class App(tk.Tk):

    def __init__(self):
        super().__init__()
        self.title(f"{APP_TITLE} {APP_VERSION}")
        self.minsize(900, 560)
        try:
            ttk.Style(self).theme_use("vista")
        except tk.TclError:
            pass

        self.settings = load_settings()
        self._last_normal_geo = None
        self.log_window = None          # single shared LogViewer for both tools
        self.comparison_window = None   # single shared ComparisonWindow (images)
        self.video_comparison_window = None  # single shared VideoComparisonWindow
        self._migrate_default_folders()
        self._restore_geometry()
        self._install_picklist_wheel_guard()

        self.nb = ttk.Notebook(self)
        self.upscale_tab    = UpscaleTab(self.nb, self)
        self.video_tab      = VideoTab(self.nb, self)
        self.tag_tab        = TagTab(self.nb, self)
        self.conciliate_tab = ConciliateTab(self.nb, self)
        self.settings_tab   = SettingsTab(self.nb, self)
        self.runpod_tab     = RunPodTab(self.nb, self)
        self.nb.add(self.upscale_tab,    text="  Batch Upscaler  ")
        self.nb.add(self.tag_tab,        text="  Tag & Rename  ")
        self.nb.add(self.conciliate_tab, text="  Conciliation  ")
        self.nb.add(self.video_tab,      text="  Video Upscaler  ")
        self.nb.add(self.settings_tab,   text="  Settings  ")
        self.nb.add(self.runpod_tab,     text="  RunPod  ")
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

    def _config_tab_name(self, widget):
        """Name of a settings-style (save-bar) tab, or None for any other tab."""
        if widget is self.settings_tab:
            return "Settings"
        if widget is self.runpod_tab:
            return "RunPod"
        return None

    def _on_tab_changed(self, event=None):
        """When leaving a settings-style tab (Settings or RunPod) with unsaved
        edits, prompt to save."""
        if self._suppress_tab_event:
            return
        try:
            new_widget = self.nb.nametowidget(self.nb.select())
        except Exception:
            return
        prev = self._prev_tab_widget
        prev_name = self._config_tab_name(prev)
        if prev_name and new_widget is not prev and prev.is_dirty():
            if self._confirm_unsaved("leaving", prev, prev_name) == "cancel":
                # Bounce back to the tab we tried to leave, without re-triggering.
                self._suppress_tab_event = True
                try:
                    self.nb.select(prev)
                finally:
                    self._suppress_tab_event = False
                return   # _prev_tab_widget stays on the settings tab
        self._prev_tab_widget = new_widget
        # Entering a tool tab: fill any empty folder field from the pinned
        # default, so a default set in Settings after startup takes effect.
        if isinstance(new_widget, ToolTab):
            new_widget.restore_defaults_if_empty()
        elif new_widget is self.video_tab:
            # Re-check remote readiness on entry, so an API key / SSH key / volume
            # configured in the RunPod tab AFTER startup is picked up (not only the
            # one-time check at launch), and refresh the durable queue.
            self.video_tab.on_enter()

    def _confirm_unsaved(self, context, tab=None, name="Settings"):
        """Modal Save / Don't save / Cancel prompt for unsaved edits in a
        settings-style tab. Returns 'ok' (handled — safe to proceed) or 'cancel'
        (stay put). `context` is 'leaving' or 'closing' for the wording."""
        tab = tab or self.settings_tab
        verb = "closing" if context == "closing" else f"leaving the {name} tab"
        ans = messagebox.askyesnocancel(
            APP_TITLE,
            f"You have unsaved changes in {name}.\n\nSave them before {verb}?")
        if ans is None:
            return "cancel"
        if ans:
            return "ok" if tab._save() else "cancel"
        # "Don't save" — discard the edits so the form matches config.json again.
        tab.revert()
        return "ok"

    def active_remote_pod_ids(self):
        """Pod ids currently in use by a running remote task, so the RunPod tab
        won't offer to terminate a pod a live run depends on."""
        ids = set()
        for t in (self.upscale_tab, self.tag_tab):
            pid = getattr(t, "active_pod_id", None)
            if pid and t.running:
                ids.add(pid)
        return ids

    def notify_active_pods_changed(self):
        """A remote run started/ended: let the RunPod tab re-mark its pod list."""
        rt = getattr(self, "runpod_tab", None)
        if rt is not None:
            rt.on_active_pods_changed()

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

    def flash_attention(self):
        """Flash the taskbar button until the window is brought to the foreground,
        so a notification catches the eye while the user is working in another app
        (e.g. an overnight upscale finishing or degrading). Windows-only, ctypes
        only (matches the dependency-light telemetry/crash-logger pattern) and
        fail-safe — never let a missing API break the GUI. No-op if already focused
        (FLASHW_TIMERNOFG) or not on Windows."""
        if os.name != "nt":
            return
        try:
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            # winfo_id() is Tk's client HWND; its parent is the top-level window
            # that owns the taskbar button.
            hwnd = user32.GetParent(self.winfo_id()) or self.winfo_id()

            class FLASHWINFO(ctypes.Structure):
                _fields_ = [("cbSize",    ctypes.c_uint),
                            ("hwnd",      wintypes.HWND),
                            ("dwFlags",   ctypes.c_uint),
                            ("uCount",    ctypes.c_uint),
                            ("dwTimeout", ctypes.c_uint)]

            FLASHW_TRAY      = 0x00000002   # flash the taskbar button
            FLASHW_TIMERNOFG = 0x0000000C   # keep flashing until the window is focused
            info = FLASHWINFO(ctypes.sizeof(FLASHWINFO), hwnd,
                              FLASHW_TRAY | FLASHW_TIMERNOFG, 0, 0)
            user32.FlashWindowEx(ctypes.byref(info))
        except Exception:
            pass

    def _taskbar(self):
        """Lazily build the taskbar progress controller for this window (the
        button only exists once the window is shown). Fail-safe → None."""
        tb = getattr(self, "_taskbar_obj", "unset")
        if tb == "unset":
            try:
                import ctypes
                hwnd = ctypes.windll.user32.GetParent(self.winfo_id()) or self.winfo_id()
                self._taskbar_obj = taskbar_progress.TaskbarProgress(hwnd)
            except Exception:
                self._taskbar_obj = None
        return self._taskbar_obj

    def taskbar_progress(self, done, total):
        """Paint a green done/total fill on the taskbar button (UI thread)."""
        tb = self._taskbar()
        if tb is not None:
            tb.set_progress(done, total)

    def taskbar_state(self, state):
        """Set the taskbar bar state ('indeterminate'/'error'/'none'/…)."""
        tb = self._taskbar()
        if tb is not None:
            tb.set_state(state)

    def taskbar_clear(self):
        """Remove the taskbar progress bar (run finished or idle)."""
        tb = self._taskbar()
        if tb is not None:
            tb.clear()

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

    def apply_remote_telemetry(self, tab, sample):
        """Render a remote-pod telemetry sample (remote #1, Feature #4) in the
        tab's dedicated remote row — revealing it on the first sample — and
        mirror it to the MQTT system/remote/* topics."""
        row = getattr(tab, "remote_telemetry_row", None)
        if row is not None:
            try:
                if not row.winfo_manager():     # hidden via grid_remove → reveal
                    row.grid()
                row.show(sample)
            except Exception:
                pass
        values = {}
        if sample.get("cpu") is not None:
            values[mqtt_publisher.SYS_REMOTE_CPU_TOPIC] = f"{sample['cpu']:.0f}"
        if sample.get("ram_used_mb") is not None:
            values[mqtt_publisher.SYS_REMOTE_RAM_TOPIC]       = str(sample["ram_used_mb"])
            values[mqtt_publisher.SYS_REMOTE_RAM_TOTAL_TOPIC] = str(sample["ram_total_mb"])
        if sample.get("gpu_used_mb") is not None:
            values[mqtt_publisher.SYS_REMOTE_GPU_VRAM_TOPIC]       = str(sample["gpu_used_mb"])
            values[mqtt_publisher.SYS_REMOTE_GPU_VRAM_TOTAL_TOPIC] = str(sample["gpu_total_mb"])
        if sample.get("gpu_temp_c") is not None:
            values[mqtt_publisher.SYS_REMOTE_GPU_TEMP_TOPIC] = str(sample["gpu_temp_c"])
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
        # Don't let unsaved Settings / RunPod edits vanish on exit.
        for tab, name in ((self.settings_tab, "Settings"), (self.runpod_tab, "RunPod")):
            if tab.is_dirty() and self._confirm_unsaved("closing", tab, name) == "cancel":
                return
        # Time each teardown step. If the close is slow (it once flagged "Not
        # Responding"), the per-step breakdown is written to logs/close_timing.log
        # so the real culprit is recorded instead of inferred.
        t0 = time.perf_counter()
        marks = []

        def mark(label):
            marks.append((label, time.perf_counter() - t0))

        busy = [t for t in (self.upscale_tab, self.video_tab, self.tag_tab,
                            self.conciliate_tab) if t.running]
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
            mark("stop-tasks")
        # Persist the main window layout and the shared log window's layout.
        self._save_geometry()
        if self.log_window is not None and self.log_window.winfo_exists():
            self.log_window.save_geometry()
        if self.comparison_window is not None and self.comparison_window.winfo_exists():
            self.comparison_window.save_geometry()
        if self.video_comparison_window is not None and self.video_comparison_window.winfo_exists():
            self.video_comparison_window.save_geometry()
        mark("save-geometry")
        # Record last-used time and announce going offline before we exit.
        self.stop_mqtt(last_used=datetime.datetime.now().isoformat(timespec="seconds"))
        mark("stop-mqtt")
        self._log_close_timing(marks)
        self.destroy()

    def _log_close_timing(self, marks, slow_threshold=1.5):
        """Append the close-path step timings to logs/close_timing.log, but only
        when the close was slow — so a clean exit stays silent and a stall is
        captured with its real breakdown. Best-effort, never raises."""
        try:
            total = marks[-1][1] if marks else 0.0
            if total < slow_threshold:
                return
            log_dir = os.path.join(APP_ROOT, "logs")
            os.makedirs(log_dir, exist_ok=True)
            stamp = datetime.datetime.now().isoformat(timespec="seconds")
            steps = "  ".join(f"{label}={secs:.2f}s" for label, secs in marks)
            with open(os.path.join(log_dir, "close_timing.log"), "a", encoding="utf-8") as f:
                f.write(f"{stamp}  total={total:.2f}s  {steps}\n")
        except Exception:                       # noqa: BLE001 (diagnostics must never block exit)
            pass


def main():
    # Refuse to start a second copy — two instances share the SQLite cache and the
    # resume/log folders (double file access, DB contention). Done first, before
    # anything touches config/DB. The existing window is brought to the front.
    if single_instance and not single_instance.acquire(
            window_title=f"{APP_TITLE} {APP_VERSION}", app_title=APP_TITLE):
        sys.exit(0)
    # Crisp text on high-DPI displays
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    App().mainloop()


if __name__ == "__main__":
    main()
