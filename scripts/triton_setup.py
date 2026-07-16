"""
triton_setup.py
---------------
On-demand installer for `triton-windows` (feature #7, local video torch.compile).

torch.compile's inductor backend needs Triton. Upstream Triton ships no Windows
wheels, but the community `triton-windows` fork does (Ampere/Ada/Blackwell, incl.
the RTX 3090), and with it `torch.compile` works on Windows. We do NOT bundle it in
the initial install (it is a ~50 MB wheel most users never need): the local video
engine runs fine without it (compile is gated off when Triton is absent, see
batch_video_upscale). This module installs it ON DEMAND, mirroring vlc_setup.py.

Guards (per project rule: verify every third-party download):
  * The exact wheel is HARD-PINNED per (Python ABI, torch minor) with its SHA-256
    (and MD5 as a fallback), so a compromised mirror can't swap it under us.
  * SHA-256 is preferred; MD5 is only a fallback when no SHA-256 is pinned.
  * `pip install --no-deps` the verified local wheel (the wheel has no runtime deps
    on Python >= 3.10), so pip never pulls anything unverified.

Stdlib + pip only; fail-safe (every entry point returns a status, never raises into
the GUI). Windows-oriented (win_amd64 wheels); pip runs against the app's own venv
via sys.executable.
"""

import os
import sys
import hashlib
import tempfile
import subprocess
import urllib.request

_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# Hard-pinned wheels keyed by (python ABI tag, torch minor). triton-windows tracks
# PyTorch closely (torch 2.7 -> Triton 3.3, 2.8 -> 3.4, ... 2.11 -> 3.7), so the
# right wheel depends on BOTH the interpreter ABI and the installed torch. Add a row
# when a new Python/torch combo ships; an unknown combo returns None (caller explains
# it) rather than installing a mismatched build that would make torch.compile error.
# Files + digests come from PyPI's per-file JSON (files.pythonhosted.org is immutable
# and content-addressed). Bump URL and BOTH hashes together if a pin ever changes.
_WHEELS = {
    ("cp312", "2.11"): {
        "version": "3.7.1.post27",
        "filename": "triton_windows-3.7.1.post27-cp312-cp312-win_amd64.whl",
        "url": ("https://files.pythonhosted.org/packages/76/30/"
                "325b420efd0047e119679c646a9a410db216069800ec009fae3da26c69a3/"
                "triton_windows-3.7.1.post27-cp312-cp312-win_amd64.whl"),
        "sha256": "f5406230d7dbf6965bc4051fcad27b81c39ba4a4bfde06f494dd7ff4eb325a9e",
        "md5": "73da36b1301a19043a2be5ba9ad09689",
    },
}


# ── environment probing ──────────────────────────────────────────────────────

def triton_installed():
    """True if `triton` is importable in this venv (triton-windows installs under the
    plain `triton` name, so torch.compile picks it up automatically)."""
    try:
        import importlib.util
        return importlib.util.find_spec("triton") is not None
    except Exception:                                  # noqa: BLE001
        return False


def compiler_available():
    """True if a C/C++ compiler inductor can use is on PATH. torch.compile is NOT usable
    with Triton alone: inductor shells out to a compiler (MSVC `cl.exe` on Windows) to build
    kernels. Without one, SeedVR2's first compile errors ('Compiler: cl is not found') in a
    console, or HANGS under the GUI's piped stdio. Visual Studio installed but not activated
    does NOT count: cl.exe is only on PATH inside a Developer Command Prompt (vcvarsall)."""
    import shutil
    return bool(shutil.which("cl") or shutil.which("cc") or shutil.which("gcc")
                or shutil.which("clang"))


def compile_ready():
    """True only when BOTH halves torch.compile needs are present: Triton AND a compiler.
    The local video engine gates compile on this; the GUI uses it to decide whether offering
    the Triton install would actually help."""
    return triton_installed() and compiler_available()


# ── compile cache location (a SPACE in the path breaks inductor) ─────────────

def _short_path(path):
    """The Windows 8.3 short name of an EXISTING path, or `path` unchanged.

    8.3 names never contain spaces, which is the only property we need. Not guaranteed:
    8.3 generation can be disabled per volume (`fsutil 8dot3name query`), in which case
    Windows returns the long name and the caller must fall back."""
    if os.name != "nt":
        return path
    try:
        import ctypes
        from ctypes import wintypes
        fn = ctypes.windll.kernel32.GetShortPathNameW
        fn.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
        fn.restype = wintypes.DWORD
        buf = ctypes.create_unicode_buffer(1024)
        n = fn(path, buf, len(buf))
        if 0 < n < len(buf) and buf.value:
            return buf.value
    except Exception:                                  # noqa: BLE001 (fail-safe)
        pass
    return path


def compile_cache_root(app_root=None):
    """A writable, SPACE-FREE directory to hold the inductor + Triton caches, or None.

    WHY THE SPACE MATTERS -- this is a PyTorch bug, not a preference. In
    torch/_inductor/cpp_builder.py the compile command is assembled as a STRING and then
    re-split with `shlex.split` (~line 614). Include dirs are quoted for that trip
    (`f'/I "{inc_dir}" '`, ~line 2023) but THE SOURCE PATH IS NOT (`" ".join(sources)`,
    ~line 1998). So any space in the cache path splits the .cpp argument in half and cl.exe
    dies with:

        warning D9024: unrecognized source file type '...torchinductor_Eduard'
        fatal error C1083: Cannot open source file: 'Baniceru/fh/....main.cpp'

    The default location is `%TEMP%/torchinductor_<username>`, so EVERY user whose Windows
    account name contains a space (very common) cannot torch.compile at all. Note Windows
    already short-names the TEMP root (`C:\\Users\\EDUARD~1\\...`); torch re-introduces the
    space by appending the full username to it.

    Candidates in order: the app root (no space on a normal install), then ProgramData. Each
    is 8.3-shortened if needed. None => the caller must not enable compile.
    """
    cands = []
    if app_root:
        cands.append(os.path.join(app_root, "cache"))
    cands.append(os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"),
                              "ImageToolbox", "cache"))
    for c in cands:
        try:
            os.makedirs(c, exist_ok=True)
        except OSError:
            continue
        if " " not in c:
            return c
        short = _short_path(c)                          # needs the dir to exist, hence after mkdir
        if " " not in short:
            return short
    return None


def ensure_cache_env(app_root=None, log=None):
    """Point inductor + Triton at a space-free cache dir for THIS process. (ok, detail).

    Sets TORCHINDUCTOR_CACHE_DIR / TRITON_CACHE_DIR in os.environ, which the probe/segment
    worker subprocesses inherit. Two wins from one change: it dodges the unquoted-source bug
    above, AND it moves the caches out of %TEMP% into a stable, app-owned location, so they
    survive a temp sweep and a compile is paid once rather than once per run.

    An existing space-free setting is respected (the user knows better); an existing setting
    WITH a space is overridden, because it cannot work. Fail-safe: never raises.
    """
    try:
        cur = os.environ.get("TORCHINDUCTOR_CACHE_DIR", "")
        if cur and " " not in cur:
            return True, f"using the configured cache dir {cur}"
        root = compile_cache_root(app_root)
        if not root:
            return False, ("no space-free compile-cache directory is available (PyTorch does "
                           "not quote the source path, so a space in it breaks every build)")
        ind, tri = os.path.join(root, "inductor"), os.path.join(root, "triton")
        for d in (ind, tri):
            os.makedirs(d, exist_ok=True)
        os.environ["TORCHINDUCTOR_CACHE_DIR"] = ind
        os.environ["TRITON_CACHE_DIR"] = tri
        if log:
            log(f"    compile cache: {root} (space-free; kept across runs)")
        return True, root
    except Exception as exc:                            # noqa: BLE001 (fail-safe)
        return False, f"could not set the compile cache dir: {exc}"


def python_tag():
    """The CPython ABI tag for this interpreter, e.g. 'cp312'."""
    return f"cp{sys.version_info.major}{sys.version_info.minor}"


def torch_minor():
    """The installed torch's 'MAJOR.MINOR' (e.g. '2.11'), or None if torch is absent."""
    try:
        import torch
        parts = torch.__version__.split("+", 1)[0].split(".")
        return ".".join(parts[:2]) if len(parts) >= 2 else None
    except Exception:                                  # noqa: BLE001
        return None


def wheel_for_env():
    """The pinned wheel spec for this (Python ABI, torch minor), or None if the combo
    isn't in the table (unknown/untested -> we refuse rather than guess)."""
    tm = torch_minor()
    if not tm:
        return None
    return _WHEELS.get((python_tag(), tm))


def is_supported():
    """True if a pinned wheel exists for this environment (Windows x64 assumed)."""
    return os.name == "nt" and wheel_for_env() is not None


def status_line():
    """A short human summary for the GUI (never raises)."""
    if triton_installed():
        return "Triton is installed (local torch.compile speedup available)."
    spec = wheel_for_env()
    if spec is None:
        return (f"No pinned triton-windows wheel for {python_tag()} + torch "
                f"{torch_minor() or '?'}; local torch.compile stays off.")
    return f"Triton not installed; can install triton-windows {spec['version']} on demand."


# ── install ──────────────────────────────────────────────────────────────────

def install(progress=None):
    """Download the pinned, verified triton-windows wheel and pip-install it (no deps).
    `progress(msg)` is an optional short-status callback. Returns (ok, message);
    fail-safe: any error is caught and returned as (False, reason)."""
    try:
        if triton_installed():
            return True, "Triton is already installed."
        spec = wheel_for_env()
        if spec is None:
            return False, (f"No pinned triton-windows wheel for {python_tag()} + torch "
                           f"{torch_minor() or 'unknown'}. Not installing a mismatched build.")
        tmp = tempfile.mkdtemp(prefix="imgtbx_triton_")
        try:
            dest = os.path.join(tmp, spec["filename"])
            if progress:
                progress(f"Downloading triton-windows {spec['version']}…")
            _download(spec["url"], dest, spec["version"], progress)
            ok, algo, got = verify_download(dest, spec.get("sha256"), spec.get("md5"))
            if not ok:
                exp = spec.get("sha256") or spec.get("md5") or "?"
                raise RuntimeError(
                    f"triton-windows download failed its {algo or 'hash'} check "
                    f"(got {got}, expected {exp}). The download may be corrupt or tampered.")
            if progress:
                progress("Installing Triton…")
            _pip_install_wheel(dest, progress)
        finally:
            _rmtree(tmp)
        if not triton_installed():
            return False, "Triton was installed but is not importable; a restart may be needed."
        return True, f"triton-windows {spec['version']} installed (verified by {algo})."
    except Exception as exc:                            # noqa: BLE001
        return False, str(exc)


# ── internals ────────────────────────────────────────────────────────────────

def _download(url, dest, version, progress=None):
    # files.pythonhosted.org serves the wheel directly to urllib's default UA; no
    # browser-UA trap (unlike get.videolan.org), so keep the plain default UA.
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
                progress(f"Downloading triton-windows {version}… {got * 100 // total}%")


def _hash_file(path, algo):
    h = hashlib.new(algo)
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_download(path, expected_sha256=None, expected_md5=None):
    """Verify `path` against the pinned digest. SHA-256 is preferred; MD5 is only a
    fallback when no SHA-256 is pinned. Returns (ok, algo_used, actual_digest). With
    neither pinned, refuses (ok=False) rather than trusting an unverified file."""
    if expected_sha256:
        got = _hash_file(path, "sha256")
        return (got.lower() == expected_sha256.lower(), "sha256", got)
    if expected_md5:
        got = _hash_file(path, "md5")
        return (got.lower() == expected_md5.lower(), "md5", got)
    return (False, None, None)


def _pip_install_wheel(wheel_path, progress=None):
    # --no-deps: the wheel has no runtime deps on Python >= 3.10, so pip installs ONLY
    # the verified local file and pulls nothing else. --no-index for the same reason
    # (belt-and-suspenders: never reach out to an index for a transitive dep).
    cp = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--no-deps", "--no-index", wheel_path],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        creationflags=_CREATE_NO_WINDOW, text=True)
    if cp.returncode != 0:
        tail = (cp.stdout or "").strip()[-400:]
        raise RuntimeError(f"pip install of the Triton wheel failed:\n{tail}")


def _rmtree(path):
    import shutil
    shutil.rmtree(path, ignore_errors=True)


if __name__ == "__main__":
    print(status_line())
    if "--install" in sys.argv:
        ok, msg = install(progress=lambda m: print(m))
        print("OK" if ok else "FAILED", msg)
        sys.exit(0 if ok else 1)
