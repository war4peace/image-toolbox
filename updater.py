"""
updater.py
----------
In-app update check & self-update for Image Toolbox.

Queries the GitHub Releases API for the latest published release, compares its
tag to the running APP_VERSION, and (on request) downloads the installer and
launches it so Inno Setup can overwrite the app in place.

Design notes:
  * Pure standard library (urllib) — keeps the dependency-light promise.
  * All network calls block and are meant to be run from a background thread;
    the GUI (toolbox_gui.py) owns the dialogs and threading.
  * The installer is the same ImageToolboxSetup.exe that CI attaches to every
    v* release. It installs to %LOCALAPPDATA%\\Programs\\Image Toolbox with
    PrivilegesRequired=lowest (no UAC) and removes the .setup_complete marker,
    so the next launch re-runs the idempotent bootstrap. The accepted Windows
    self-update pattern therefore applies: launch the installer, then quit the
    app immediately so the running scripts can be replaced.
"""

import os
import re
import json
import tempfile
import subprocess
import urllib.request
import urllib.error

GITHUB_REPO        = "war4peace/image-toolbox"
LATEST_RELEASE_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
RELEASES_PAGE      = f"https://github.com/{GITHUB_REPO}/releases/latest"
INSTALLER_ASSET    = "ImageToolboxSetup.exe"
_USER_AGENT        = "ImageToolbox-Updater"


class UpdateInfo:
    """A newer release than the one running."""

    def __init__(self, version, tag, notes, asset_url, asset_size):
        self.version    = version      # e.g. "0.2.3" (tag without the leading v)
        self.tag        = tag          # e.g. "v0.2.3"
        self.notes      = notes        # release body (patch notes), may be ""
        self.asset_url  = asset_url    # direct download URL, or None if missing
        self.asset_size = asset_size   # bytes, or 0 if unknown


def parse_version(s):
    """
    'v0.2.10' / '0.2.10' / '0.2.10-experimental' -> (0, 2, 10).
    Non-numeric suffixes are ignored; missing parts compare as if absent.
    """
    parts = re.findall(r"\d+", (s or ""))
    return tuple(int(p) for p in parts) if parts else (0,)


def is_newer(latest, current):
    """True if version string `latest` is strictly newer than `current`."""
    return parse_version(latest) > parse_version(current)


def check_for_update(current_version, timeout=10):
    """
    Look up the latest published release and compare it to current_version.

    Returns (status, payload):
      ("update",  UpdateInfo)   a newer release is available
      ("current", "0.2.2")      already on the latest version (string is latest)
      ("error",   "message")    could not determine (network/API/parse error)
    """
    try:
        req = urllib.request.Request(LATEST_RELEASE_API, headers={
            "User-Agent": _USER_AGENT,
            "Accept":     "application/vnd.github+json",
        })
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return "error", "No published releases were found for this project."
        return "error", f"GitHub returned HTTP {exc.code} {exc.reason}."
    except Exception as exc:
        return "error", f"Could not reach GitHub: {exc}"

    tag    = (data.get("tag_name") or "").strip()
    latest = tag.lstrip("vV")
    if not latest:
        return "error", "The latest release has no version tag."

    if not is_newer(latest, current_version):
        return "current", latest

    asset_url, asset_size = None, 0
    for asset in data.get("assets", []):
        if (asset.get("name") or "").lower() == INSTALLER_ASSET.lower():
            asset_url  = asset.get("browser_download_url")
            asset_size = int(asset.get("size") or 0)
            break

    notes = (data.get("body") or "").strip()
    return "update", UpdateInfo(latest, tag, notes, asset_url, asset_size)


def download_installer(url, expected_size=0, dest_dir=None, progress_cb=None, timeout=30):
    """
    Download the installer to a temp file (written to a .part file, then renamed
    so a partial download can't be mistaken for a complete one).

    progress_cb(downloaded_bytes, total_bytes) is called as data arrives; total
    is 0 when the server doesn't send Content-Length. Returns the final path.
    Raises on any network/IO error, or if the finished size doesn't match the
    release's recorded asset size (a corruption / truncation guard).
    """
    if not url:
        raise ValueError("No installer asset was attached to the release.")

    dest_dir = dest_dir or tempfile.gettempdir()
    dest     = os.path.join(dest_dir, INSTALLER_ASSET)
    part     = dest + ".part"

    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        total = int(resp.headers.get("Content-Length") or expected_size or 0)
        done  = 0
        with open(part, "wb") as f:
            while True:
                chunk = resp.read(1 << 16)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if progress_cb:
                    progress_cb(done, total)

    if expected_size and done != expected_size:
        try:
            os.remove(part)
        except OSError:
            pass
        raise IOError(
            f"Download size mismatch (got {done} bytes, expected {expected_size}). "
            f"The file may be corrupted; please try again.")

    os.replace(part, dest)
    return dest


def launch_installer(path):
    """
    Launch the downloaded installer and return. The caller MUST then quit the
    app so Inno Setup can replace the running scripts. The installer needs no
    elevation (PrivilegesRequired=lowest); SmartScreen may prompt because the
    build is unsigned — the user clicks "More info" -> "Run anyway".
    """
    if not os.path.isfile(path):
        raise FileNotFoundError(path)
    subprocess.Popen([path], close_fds=True)
