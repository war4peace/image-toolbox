"""
vlc_setup.py
------------
In-app installer for the bundled libVLC + the python-vlc package (section 16.2),
so an existing install (bootstrapped BEFORE the segment-extractor feature, i.e.
before bootstrap.ps1 learned to fetch libVLC) can enable in-app video playback
WITHOUT a full reinstall. This mirrors bootstrap.ps1's Install-LibVlc, driven from
the GUI's "Install libVLC now" prompt.

Stdlib + pip only (`urllib`, `zipfile`, `subprocess`); fail-safe (every entry
point returns a status, never raises into the GUI). Windows-oriented like the rest
of the app (the download is a win64 build; pip runs against the app's own venv via
sys.executable).
"""

import os
import sys
import hashlib
import zipfile
import tempfile
import subprocess
import urllib.request

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
APP_ROOT = os.path.dirname(_SCRIPT_DIR)

# Same pin as bootstrap.ps1 ($LIBVLC_URL): a VLC 3.0.x that matches python-vlc.
# get.videolan.org 302-redirects a file request to a working mirror -- BUT only for a
# non-browser client. A browser-like User-Agent gets an HTML mirror-CHOOSER page
# instead of the zip, so we send urllib's default UA (do NOT spoof a browser here).
VLC_URL = "https://get.videolan.org/vlc/3.0.21/win64/vlc-3.0.21-win64.zip"
# SHA-256 of that immutable versioned artifact (VideoLAN's published sum; verified
# against the real download). Baked in so a later-compromised mirror can't swap the zip
# under us. MUST change in lockstep with VLC_URL if the pin is ever bumped. Keep this in
# sync with bootstrap.ps1's $LIBVLC_SHA256.
VLC_SHA256 = "a0b7ec02b50adf6417eed014fb8df50af39690505a4225b85b3dc2ed17d14843"
_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def vlc_dir():
    """<APP_ROOT>/vlc, where gui.video_player looks for libvlc.dll + plugins/."""
    return os.path.join(APP_ROOT, "vlc")


def libvlc_present():
    d = vlc_dir()
    return (os.path.isfile(os.path.join(d, "libvlc.dll"))
            and os.path.isdir(os.path.join(d, "plugins")))


def python_vlc_present():
    try:
        import importlib.util
        return importlib.util.find_spec("vlc") is not None
    except Exception:
        return False


def is_installed():
    """True when BOTH halves are in place (the bundled libVLC and the python-vlc
    binding), i.e. playback can work."""
    return libvlc_present() and python_vlc_present()


def install(progress=None):
    """Ensure both halves are installed. `progress(msg)` is an optional status
    callback (a short human line). Returns (ok, message); fail-safe — any error is
    caught and returned as (False, reason)."""
    try:
        if not libvlc_present():
            _install_libvlc(progress)
        if not python_vlc_present():
            _pip_install("python-vlc", progress)
        if progress:
            progress("Done.")
        return True, "libVLC installed."
    except Exception as exc:                          # noqa: BLE001
        return False, str(exc)


# ── internals ────────────────────────────────────────────────────────────────

def _download(url, dest, progress=None):
    # Default urllib UA (Python-urllib/...): a browser UA makes get.videolan.org
    # serve an HTML mirror-chooser instead of the file (see VLC_URL note).
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=120) as r, open(dest, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        got = 0
        while True:
            chunk = r.read(1 << 16)
            if not chunk:
                break
            f.write(chunk)
            got += len(chunk)
            if progress and total:
                progress(f"Downloading libVLC… {got * 100 // total}%")


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _install_libvlc(progress=None):
    """Download the pinned VLC win64 zip and copy libvlc.dll + libvlccore.dll +
    plugins/ into <APP_ROOT>/vlc (only what libVLC needs, not vlc.exe / the UI)."""
    if progress:
        progress("Downloading libVLC…")
    tmp = tempfile.mkdtemp(prefix="imgtbx_vlc_")
    try:
        zip_path = os.path.join(tmp, "vlc.zip")
        _download(VLC_URL, zip_path, progress)
        # Integrity gate: the zip is a fixed, immutable artifact, so a mismatch means a
        # corrupt or tampered download -> refuse it (the temp dir, incl. this zip, is
        # removed in `finally`). Mirrors bootstrap.ps1's Python/ffmpeg hash checks.
        actual = _sha256(zip_path)
        if actual != VLC_SHA256:
            raise RuntimeError(
                f"libVLC download failed its SHA-256 check (got {actual}, expected "
                f"{VLC_SHA256}). The download may be corrupt or tampered.")
        if progress:
            progress("Extracting libVLC…")
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(tmp)
        pkg = _find_pkg(tmp)
        if pkg is None:
            raise RuntimeError("the VLC archive had an unexpected layout")
        dest = vlc_dir()
        os.makedirs(dest, exist_ok=True)
        for dll in ("libvlc.dll", "libvlccore.dll"):
            src = os.path.join(pkg, dll)
            if not os.path.isfile(src):
                raise RuntimeError(f"the VLC archive is missing {dll}")
            _copy(src, os.path.join(dest, dll))
        plugins_src = os.path.join(pkg, "plugins")
        if not os.path.isdir(plugins_src):
            raise RuntimeError("the VLC archive is missing plugins/")
        _copy_tree(plugins_src, os.path.join(dest, "plugins"))
        # Ship VLC's license alongside (LGPL for libVLC).
        for name in os.listdir(pkg):
            if name.upper().startswith("COPYING"):
                _copy(os.path.join(pkg, name), os.path.join(dest, "COPYING.txt"))
                break
        if not libvlc_present():
            raise RuntimeError("libvlc.dll was not installed")
    finally:
        _rmtree(tmp)


def _find_pkg(root):
    """The extracted VLC folder (holds libvlc.dll). Usually <root>/vlc-3.0.21/."""
    if os.path.isfile(os.path.join(root, "libvlc.dll")):
        return root
    for name in sorted(os.listdir(root)):
        d = os.path.join(root, name)
        if os.path.isdir(d) and os.path.isfile(os.path.join(d, "libvlc.dll")):
            return d
    return None


def _pip_install(pkg, progress=None):
    if progress:
        progress(f"Installing {pkg}…")
    cp = subprocess.run(
        [sys.executable, "-m", "pip", "install", pkg],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        creationflags=_CREATE_NO_WINDOW, text=True)
    if cp.returncode != 0:
        tail = (cp.stdout or "").strip()[-400:]
        raise RuntimeError(f"pip install {pkg} failed:\n{tail}")


def _copy(src, dst):
    import shutil
    shutil.copy2(src, dst)


def _copy_tree(src, dst):
    import shutil
    if os.path.isdir(dst):
        shutil.rmtree(dst, ignore_errors=True)
    shutil.copytree(src, dst)


def _rmtree(path):
    import shutil
    shutil.rmtree(path, ignore_errors=True)


if __name__ == "__main__":
    ok, msg = install(progress=lambda m: print(m))
    print("OK" if ok else "FAILED", msg)
    sys.exit(0 if ok else 1)
