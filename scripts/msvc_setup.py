"""
msvc_setup.py
-------------
Find and ACTIVATE the MSVC toolchain for local torch.compile (feature #7), the missing
half of triton_setup.py.

torch.compile needs BOTH halves: Triton (triton_setup.py installs it) AND a C compiler,
because inductor shells out to `cl.exe` to build kernels. Triton alone is not enough.

Why this module has to exist at all: **Visual Studio never puts cl.exe on PATH.** It lives
only inside a "Developer Command Prompt", which is just cmd.exe after `vcvarsall.bat` has
set PATH/INCLUDE/LIB. The GUI launches from Explorer, so it inherits none of that, and
`shutil.which("cl")` fails on a machine with a perfectly good compiler installed. This
module runs vcvarsall itself, captures the environment it produces, and merges it into the
current process -- so a user gets the compile speedup without knowing what vcvarsall is.

Why the detection is paranoid: `which("cl")` is a LIAR. A real machine here had cl.exe
present and launching (19.41.34120) with NO CRT headers, NO libs, NO Windows SDK and no
vcvarsall.bat -- a stub dragged in by some other VS component. Trusting it would have
turned a clean "compile disabled" message into a C1034 or, under the GUI's piped stdio,
the silent first-segment HANG that gate_local_compile exists to prevent. So:

  * find_msvc() asks **vswhere** for an install that actually HAS the C++ toolset
    component, rather than trusting a binary on disk.
  * verify_toolchain() **compiles a hello world**. That is the only claim that matters,
    and it is the one check the stub above could not have passed.

Stdlib only; fail-safe (every entry point returns a status, never raises into the GUI or a
runner). Windows-only by nature: every non-Windows path reports "unavailable" cleanly.
"""

import os
import re
import shutil
import tempfile
import subprocess

_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# The C++ toolset component. An install WITHOUT this can still contain a cl.exe (see the
# module docstring), which is exactly why we require the component and not the binary.
VC_TOOLS_COMPONENT = "Microsoft.VisualStudio.Component.VC.Tools.x86.x64"

# vswhere ships with the VS Installer at a FIXED, documented location, so it is findable
# without already knowing where VS is (that is its whole purpose).
_VSWHERE = os.path.join(
    os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
    "Microsoft Visual Studio", "Installer", "vswhere.exe")

# `set` output: NAME=VALUE. Windows names may contain parentheses (ProgramFiles(x86)) but
# never '='. Anything that doesn't match is skipped rather than guessed at.
_SET_RE = re.compile(r"^([^=\s][^=]*)=(.*)$")

_ENV_CACHE = None            # activation env delta (dict) | None; None = not probed yet
_ENV_PROBED = False
_ENV_SOURCE = None           # WHICH tier activated: the script path, or "built from disk"
_VERIFY_CACHE = None         # (ok, detail) of the hello-world compile


# ── discovery ────────────────────────────────────────────────────────────────

def vswhere_path():
    """Path to vswhere.exe, or None. Present whenever any modern VS/Build Tools is."""
    return _VSWHERE if os.path.exists(_VSWHERE) else None


def _vswhere(*args):
    """Run vswhere and return its non-empty output lines ([] on any failure)."""
    vsw = vswhere_path()
    if not vsw:
        return []
    try:
        cp = subprocess.run([vsw, *args], capture_output=True, text=True, timeout=60,
                            creationflags=_CREATE_NO_WINDOW)
        return [ln.strip() for ln in (cp.stdout or "").splitlines() if ln.strip()]
    except Exception:                                   # noqa: BLE001 (fail-safe)
        return []


def _installs():
    """Every VS / Build Tools installation path, newest first ([] if none). `-products *`
    matters: it includes the standalone Build Tools SKU, which the default filter omits and
    which is what a non-developer installs for this."""
    return [p for p in _vswhere("-products", "*", "-sort",
                                "-property", "installationPath", "-format", "value")
            if os.path.isdir(p)]


def _install_has_compiler(path):
    """True if this install carries an MSVC cl.exe on disk (any toolset version)."""
    import glob as _g
    return bool(_g.glob(os.path.join(path, "VC", "Tools", "MSVC", "*",
                                     "bin", "Hostx64", "x64", "cl.exe")))


def find_msvc():
    """installationPath of an install that has an MSVC compiler, or None.

    Two passes, because every cheap signal here has now been caught lying:

      * Fast path: vswhere -requires the C++ toolset component. Correct when it works.
      * Fallback: any install carrying a cl.exe on disk, newest first. A real machine with
        VS 2022 and VS 2026 side by side reported NO MATCH for VC.Tools.x86.x64,
        VC.CoreIde AND VC.Redist.14.Latest simultaneously, while holding an MSVC that
        compiles, links and runs a hello world. The registration simply never happened. An
        ID we cannot keep current across VS versions must not be able to veto a working
        compiler.

    This is deliberately permissive, and that costs no safety: it only decides what to TRY.
    verify_toolchain() compiles afterwards, and that is the claim that cannot be faked.
    """
    hit = [p for p in _vswhere("-products", "*", "-latest", "-requires", VC_TOOLS_COMPONENT,
                               "-property", "installationPath", "-format", "value")
           if os.path.isdir(p)]
    if hit:
        return hit[0]
    for p in _installs():
        if _install_has_compiler(p):
            return p
    return None


def vcvarsall_path(install_path=None):
    """Path to vcvarsall.bat for an install, or None. Note this is NOT a verdict on the
    toolchain: VS 2026 (v18) does not ship vcvarsall.bat at all, and a real machine here had
    a fully working MSVC with no vcvarsall anywhere. See activation_scripts()."""
    install_path = install_path or find_msvc()
    if not install_path:
        return None
    p = os.path.join(install_path, "VC", "Auxiliary", "Build", "vcvarsall.bat")
    return p if os.path.exists(p) else None


def activation_scripts(install_path=None):
    """[(script, args)] to try, best first, for activating an install's MSVC.

    Two entry points exist because Microsoft changed them: `vcvarsall.bat` is the classic
    one every guide names, and VS 2026 (v18) SHIPS NO vcvarsall.bat -- only
    Common7\\Tools\\VsDevCmd.bat. Trying both keeps one VS generation from locking us out.
    """
    install_path = install_path or find_msvc()
    out = []
    if not install_path:
        return out
    va = os.path.join(install_path, "VC", "Auxiliary", "Build", "vcvarsall.bat")
    if os.path.exists(va):
        out.append((va, "x64"))
    vs = os.path.join(install_path, "Common7", "Tools", "VsDevCmd.bat")
    if os.path.exists(vs):
        out.append((vs, "-arch=x64 -host_arch=x64"))
    return out


def _ver_key(name):
    """Sort key for a dotted version dir ('14.44.35207' -> (14, 44, 35207)). String sort
    would rank 14.9 above 14.44."""
    parts = []
    for p in str(name).split("."):
        try:
            parts.append(int(p))
        except ValueError:
            parts.append(0)
    return tuple(parts)


def msvc_toolset():
    """(bin_dir, include_dir, lib_dir) of the newest COMPLETE MSVC toolset, or None.

    'Complete' is checked per candidate rather than assumed: a real machine had TWO toolset
    dirs where the newest (14.51) was a half-populated shell and the older (14.44) held the
    working compiler. Picking by version alone would have chosen the broken one.
    """
    for inst in _installs():
        root = os.path.join(inst, "VC", "Tools", "MSVC")
        if not os.path.isdir(root):
            continue
        try:
            vers = sorted(os.listdir(root), key=_ver_key, reverse=True)
        except OSError:
            continue
        for v in vers:
            b = os.path.join(root, v, "bin", "Hostx64", "x64")
            i = os.path.join(root, v, "include")
            l = os.path.join(root, v, "lib", "x64")
            if (os.path.exists(os.path.join(b, "cl.exe")) and os.path.isdir(i)
                    and os.path.isdir(l)):
                return b, i, l
    return None


def windows_sdk():
    """(include_dirs, lib_dirs) of the newest COMPLETE Windows 10/11 SDK, or None.

    The SDK is not optional trivia: stdio.h has lived in the SDK's UCRT (not in MSVC) since
    VS2015, so without it cl.exe cannot compile the simplest C file.
    """
    root = os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                        "Windows Kits", "10")
    inc_root, lib_root = os.path.join(root, "Include"), os.path.join(root, "Lib")
    if not os.path.isdir(inc_root):
        return None
    try:
        vers = sorted(os.listdir(inc_root), key=_ver_key, reverse=True)
    except OSError:
        return None
    for v in vers:
        incs = [os.path.join(inc_root, v, s) for s in ("ucrt", "um", "shared")]
        libs = [os.path.join(lib_root, v, s, "x64") for s in ("ucrt", "um")]
        if (all(os.path.isdir(p) for p in incs) and all(os.path.isdir(p) for p in libs)
                and os.path.exists(os.path.join(inc_root, v, "ucrt", "stdio.h"))):
            return incs, libs
    return None


def manual_env():
    """Build the MSVC environment straight from the files on disk, or None.

    THE LAST RESORT, and it is not hypothetical. A real machine ended up with a complete,
    verified-working MSVC 14.44.35207 and Windows SDK 10.0.28000.0 while the installer never
    wrote vcvarsall.bat nor registered the component -- so vswhere reported no C++ at all and
    VsDevCmd set INCLUDE/LIB to the SDK only, silently omitting MSVC. Every official path
    refused a compiler that compiles, links and runs a hello world.

    This reimplements only the part of vcvarsall we actually need (INCLUDE / LIB / PATH), and
    it is safe to be this aggressive for one reason: verify_toolchain() COMPILES afterwards,
    so a wrong guess here is caught and reported rather than trusted.
    """
    ms, sdk = msvc_toolset(), windows_sdk()
    if not ms or not sdk:
        return None
    binp, inc, lib = ms
    incs, libs = sdk
    return {
        "INCLUDE": ";".join([inc] + incs),
        "LIB": ";".join([lib] + libs),
        "PATH": binp + os.pathsep + os.environ.get("PATH", ""),
    }


def _env_has_msvc(env):
    """True only if this environment can actually reach cl.exe. VsDevCmd 'succeeds' and sets
    INCLUDE/LIB from the SDK while omitting MSVC entirely, so a non-empty env proves nothing."""
    for p in (env.get("PATH") or "").split(os.pathsep):
        if p and os.path.exists(os.path.join(p, "cl.exe")):
            return True
    return False


# ── activation ───────────────────────────────────────────────────────────────

def _run_activation_script(script, args):
    """Run an activation script and return the environment it produces, or None.

    QUOTING: the command MUST go to subprocess as a STRING with shell=True. Passing
    ["cmd.exe", "/s", "/c", '"<path>" ... && set'] looks equivalent and is not: Python's
    list2cmdline backslash-escapes the inner quotes, cmd receives \\"C:\\Program Files\\...\\"
    and reports 'is not recognized as an internal or external command'. That bug made a
    perfectly good vcvarsall look missing, and no amount of mocking subprocess.run could
    have caught it.

    `>nul` keeps the script's banner out of stdout so only `set` output remains, and `&&`
    means `set` runs ONLY if activation succeeded.
    """
    try:
        cp = subprocess.run(f'"{script}" {args} >nul && set',
                            shell=True, capture_output=True, text=True,
                            errors="replace", timeout=240,
                            creationflags=_CREATE_NO_WINDOW)
        if cp.returncode != 0:
            return None
        new = {}
        for line in (cp.stdout or "").splitlines():
            m = _SET_RE.match(line.rstrip("\r"))
            if m:
                new[m.group(1)] = m.group(2)
        return new or None
    except Exception:                                   # noqa: BLE001 (fail-safe)
        return None


def vcvars_env(arch="x64", force=False):
    """The environment CHANGES needed to use MSVC, as a dict, or None if unavailable.

    Three tiers, first one that actually yields a reachable cl.exe wins:
      1. vcvarsall.bat (the classic entry point)
      2. VsDevCmd.bat (VS 2026 / v18 ships no vcvarsall at all)
      3. manual_env() -- built from the files on disk, for the install that has a working
         compiler but no activation script and no component registration

    Returns a DELTA against the current environment, so the caller applies what MSVC needs
    (PATH/INCLUDE/LIB/LIBPATH and the VS locator vars) instead of wholesale-replacing its own
    environment with a subprocess's. Cached per process: activation costs seconds and its
    answer cannot change while we run.
    """
    global _ENV_CACHE, _ENV_PROBED, _ENV_SOURCE
    if _ENV_PROBED and not force:
        return _ENV_CACHE
    _ENV_PROBED = True
    _ENV_CACHE = _ENV_SOURCE = None
    if os.name != "nt":
        return None
    for script, args in activation_scripts():
        new = _run_activation_script(script, args if arch == "x64" else arch)
        # A script can "succeed" and still not give us MSVC: VsDevCmd on a machine with an
        # unregistered toolset set INCLUDE/LIB from the Windows SDK alone. Insist on cl.exe.
        if new and _env_has_msvc(new):
            _ENV_CACHE = {k: v for k, v in new.items() if os.environ.get(k) != v} or None
            _ENV_SOURCE = script
            return _ENV_CACHE
    manual = manual_env()
    if manual and _env_has_msvc(manual):
        _ENV_CACHE = manual
        _ENV_SOURCE = "built from disk (no activation script on this install)"
        return _ENV_CACHE
    return None


def activation_source():
    """WHICH tier activated MSVC, for the log. Never guess this: the first version of this
    module hardcoded 'activated via vcvarsall' and cheerfully reported it on a machine with
    no vcvarsall anywhere, which is precisely the kind of confident-and-wrong status line
    that turns a 5-minute diagnosis into an hour."""
    return _ENV_SOURCE


def ensure_msvc_env(log=None):
    """Make cl.exe usable by THIS process, activating MSVC if needed. Returns (ok, detail).

    Merges the vcvars delta into os.environ, which is what inductor's child cl.exe
    inherits, so calling this inside a runner is enough to turn compile on for the whole
    run. Idempotent and fail-safe: never raises, never removes anything.
    """
    if shutil.which("cl"):
        return True, "cl.exe already on PATH"
    if os.name != "nt":
        return False, "not Windows (no MSVC)"
    env = vcvars_env()
    if not env:
        return False, install_hint()
    os.environ.update(env)
    if not shutil.which("cl"):
        return False, "activation ran but cl.exe is still not on PATH (broken install)"
    src = activation_source() or "unknown method"
    if log:
        log(f"    MSVC activated ({src}): torch.compile can build.")
    return True, f"activated: {src}"


def verify_toolchain(force=False):
    """Actually COMPILE something. Returns (ok, detail).

    This is the only check that cannot be fooled. The machine this module was written for
    had cl.exe on disk, launching and reporting its version, yet `#include <stdio.h>` gave
    'fatal error C1034: no include path set' because the CRT headers and the Windows SDK
    were never installed. Every cheaper check passed. Cached per process (~a second).
    """
    global _VERIFY_CACHE
    if _VERIFY_CACHE is not None and not force:
        return _VERIFY_CACHE
    ok, detail = ensure_msvc_env()
    if not ok:
        _VERIFY_CACHE = (False, detail)
        return _VERIFY_CACHE
    tmp = tempfile.mkdtemp(prefix="imgtbx_cl_")
    try:
        src = os.path.join(tmp, "probe.c")
        with open(src, "w", encoding="ascii") as f:
            f.write("#include <stdio.h>\nint main(void){return 0;}\n")
        cp = subprocess.run(["cl.exe", "/nologo", "/c", src],
                            capture_output=True, text=True, errors="replace",
                            cwd=tmp, timeout=120, creationflags=_CREATE_NO_WINDOW)
        if cp.returncode == 0:
            _VERIFY_CACHE = (True, "cl.exe compiled a test file")
        else:
            out = ((cp.stdout or "") + (cp.stderr or "")).strip().replace("\n", " ")
            _VERIFY_CACHE = (False, f"cl.exe cannot compile: {out[:200]}")
    except Exception as exc:                            # noqa: BLE001 (fail-safe)
        _VERIFY_CACHE = (False, f"compiler probe failed: {exc}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return _VERIFY_CACHE


# ── reporting ────────────────────────────────────────────────────────────────

def install_hint():
    """One line telling the user exactly what to do. Each branch is a state a real machine
    was actually in, because 'install Visual Studio' is useless advice to someone who has
    it, and 'repair your install' is useless to someone whose install works fine."""
    if os.name != "nt":
        return "MSVC is Windows-only."
    if not vswhere_path():
        return ("No Visual Studio found. Install 'Build Tools for Visual Studio 2022' with "
                "the 'Desktop development with C++' workload to enable local torch.compile.")
    if not find_msvc():
        return ("Visual Studio is installed but has no C++ compiler. In the Visual Studio "
                "Installer press Modify -> Individual components and add "
                "'MSVC v143 - VS 2022 C++ x64/x86 build tools' + 'Windows 11 SDK'.")
    if not windows_sdk():
        return ("MSVC is installed but no complete Windows SDK was found (stdio.h lives in "
                "the SDK's UCRT, not in MSVC). Add 'Windows 11 SDK' in the Visual Studio "
                "Installer -> Modify -> Individual components.")
    return ("MSVC is present but could not be activated. Try repairing the C++ workload in "
            "the Visual Studio Installer.")


def status_line():
    """A short human summary for the GUI/log (never raises)."""
    if os.name != "nt":
        return "MSVC is Windows-only; local torch.compile stays off."
    if shutil.which("cl"):
        return "MSVC is on PATH (local torch.compile can build)."
    if find_msvc() and (activation_scripts() or manual_env()):
        return "MSVC is installed; the app activates it automatically."
    return install_hint()


if __name__ == "__main__":
    print("vswhere      :", vswhere_path() or "not found")
    print("MSVC install :", find_msvc() or "none with the C++ toolset")
    print("vcvarsall    :", vcvarsall_path() or "not found")
    ok, detail = ensure_msvc_env()
    print("activate     :", "OK" if ok else "FAILED", "-", detail)
    ok2, detail2 = verify_toolchain()
    print("compile probe:", "OK" if ok2 else "FAILED", "-", detail2)
    print("status       :", status_line())
