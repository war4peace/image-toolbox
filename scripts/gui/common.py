"""
gui/common.py
-------------
Shared foundation for the GUI package: path/version constants, config.json load
+ save, the per-tool default-folder / update-preference / funds / mqtt / ollama
helpers, and the gui_settings.json (window geometry) helpers.

This module has NO dependency on any sibling gui module, so it can be imported
freely by all of them without a cycle. The heavier app modules it does use
(updater, system_telemetry, notifications) are all stdlib-light and torch-free.

CFG is loaded once here and mutated in place by the Settings tab (never
reassigned), so every module that does `from gui.common import CFG` shares the
same dict and sees saved changes.
"""

import os
import re
import sys
import json
import shutil
import platform
import webbrowser
import urllib.parse
import urllib.request
import urllib.error   # noqa: F401 (kept for parity; callers catch broad Exception)

import updater
import system_telemetry
import notifications
import config_store

# SCRIPT_DIR is where the gui package's parent (scripts/) lives; APP_ROOT is its
# parent - where config.json, gui_settings.json, the .venv, logs/, db/ and the
# seedvr2/ engine all live. Anchored two levels up from this file (scripts/gui/).
SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_ROOT   = os.path.dirname(SCRIPT_DIR)
APP_TITLE  = "Image Toolbox"
# Shown in the main window title bar. On a release, set this to the tag (e.g.
# "0.1.3") and drop the "-experimental" suffix.
APP_VERSION = "0.4.3"

CREATE_NO_WINDOW = 0x08000000

# Matches the per-image counters both scripts print, e.g. "[37/59]"
PROGRESS_RE = re.compile(r"\[(\d+)/(\d+)\]")

# Marker prefix the runners emit on stdout for machine-readable event lines
# (@@TBX@@KIND|payload); the GUI strips it before showing the log. The runner
# side has its own copy in runner_common (they must agree on the literal).
GUI_MARKER = "@@TBX@@"


# ─────────────────────────────────────────────
#  CONFIG / INTERPRETER
# ─────────────────────────────────────────────

def _load_config():
    # Merged view of the tracked config.json plus the untracked config.local.json
    # secrets overlay (config_store). The rest of the GUI reads CFG exactly as
    # before; it never needs to know secrets live in a separate file.
    return config_store.load(APP_ROOT) or {}

CFG = _load_config()


def save_config(cfg=None):
    """
    Write the settings back to disk (the Settings tab edits CFG in place, then
    calls this). Returns True on success. Secrets go to config.local.json and a
    secret-free copy to config.json (config_store.save); the tracked file never
    gains a credential. The backend scripts read both files fresh at launch, so
    saved changes take effect on the next run.
    """
    if cfg is None:
        cfg = CFG
    return config_store.save(cfg, APP_ROOT)


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
#  RUNPOD FUNDS READOUT  (Settings + tool-tab status bars)
# ─────────────────────────────────────────────
# The account balance, coloured by how far it sits above the configured funds
# floor (balance_floor_usd). The bands reuse the telemetry colours (TelemetryRow)
# so the readouts read as one system.

_FUNDS_GREY = "#7f8a99"   # unknown/disabled — matches TelemetryRow.GREY


def funds_color(funds, floor):
    """Colour a balance by its margin above the funds floor (mirrors the telemetry
    bands): blue >= +$10, green +$5-10, dark yellow +$1-5, red at/near the floor.
    Unknown balance → grey."""
    if funds is None:
        return _FUNDS_GREY
    delta = funds - (floor or 0.0)
    if delta >= 10:
        return "#3a86ff"      # blue
    if delta >= 5:
        return "#1a9e4b"      # green
    if delta > 1:
        return "#b58900"      # dark yellow
    return "#d11a2a"          # red


def config_funds_floor():
    """The saved funds floor ($) from config; 0.0 if unset/invalid."""
    try:
        return float(CFG.get("runpod", {}).get("balance_floor_usd", 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def fmt_funds(info):
    """(display text, balance-or-None) for a runpod_client.account_balance result.
    A missing/failed lookup reads as 'Unknown'."""
    bal = info.get("balance") if isinstance(info, dict) else None
    if bal is None:
        return "Unknown", None
    try:
        bal = float(bal)
    except (TypeError, ValueError):
        return "Unknown", None
    return f"${bal:.2f}", bal


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
