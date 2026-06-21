"""
ssh_setup.py
------------
Zero-config SSH onboarding for remote-pod upscaling (#1). Makes the remote path
reachable by a **non-technical** user: the app owns a dedicated SSH keypair and
hands its public half to RunPod at pod-creation time (the ``PUBLIC_KEY`` env var
that RunPod's base images append to ``authorized_keys``), so the user never has
to run ``ssh-keygen``, paste a key into the RunPod website, or hand-edit
``config.json``. The only thing they supply is the API key.

What this module does, all best-effort and Windows-first:

  * Locate the OpenSSH client (``ssh`` / ``ssh-keygen``). Windows 10/11 ship it
    as an optional feature under ``%WINDIR%\\System32\\OpenSSH``, but it can be
    absent/disabled — detect that and say so, rather than failing cryptically.
  * Generate a dedicated ed25519 keypair (``-N ""``, no passphrase, so the app
    can use it unattended) if one isn't already there. The key lives under the
    user's ``~/.ssh`` so its directory ACL is already owner-only — OpenSSH
    refuses a world-readable private key ("UNPROTECTED PRIVATE KEY FILE"), so we
    also lock the file down with ``icacls`` as a belt-and-braces step.
  * Read the public half (``<key>.pub``) to feed into the pod's ``PUBLIC_KEY``.

Stdlib only (subprocess + ctypes for the no-console flag). Nothing here ever
touches the network or config — the GUI owns those.
"""

import os
import sys
import subprocess

CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

# The dedicated key's basename. Kept distinct from a user's personal keys so we
# never touch (or lock down) those, and a RunPod-only key is easy to recognise.
_KEY_NAME = "id_ed25519_runpod"


class SshSetupError(Exception):
    """SSH onboarding failed (no OpenSSH, keygen error, unreadable key)."""


def _no_window():
    """subprocess kwargs that suppress a console flash under pythonw (Windows)."""
    return {"creationflags": CREATE_NO_WINDOW} if sys.platform == "win32" else {}


def _candidate_dirs():
    """Standard locations to look for the OpenSSH client beyond PATH."""
    dirs = []
    windir = os.environ.get("WINDIR") or r"C:\Windows"
    dirs.append(os.path.join(windir, "System32", "OpenSSH"))
    # Some installs ship it under Program Files (Git for Windows, OpenSSH MSI).
    for pf in (os.environ.get("ProgramFiles"), os.environ.get("ProgramW6432")):
        if pf:
            dirs.append(os.path.join(pf, "OpenSSH"))
            dirs.append(os.path.join(pf, "Git", "usr", "bin"))
    return dirs


def find_executable(name):
    """Return the full path to ``name`` (``ssh`` / ``ssh-keygen``) or None.

    Checks PATH first (``shutil.which``), then the standard Windows OpenSSH and a
    couple of common bundled locations — so we find it even when the optional
    feature dir isn't on PATH.
    """
    import shutil
    found = shutil.which(name)
    if found:
        return found
    exe = name + (".exe" if sys.platform == "win32" else "")
    for d in _candidate_dirs():
        cand = os.path.join(d, exe)
        if os.path.isfile(cand):
            return cand
    return None


def ssh_available():
    """Return (ok, ssh_path, keygen_path, message).

    ok is True only when BOTH ``ssh`` and ``ssh-keygen`` are found — the remote
    flow needs ssh/scp to talk to the pod and ssh-keygen to mint the key.
    """
    ssh = find_executable("ssh")
    keygen = find_executable("ssh-keygen")
    if ssh and keygen:
        return True, ssh, keygen, "OpenSSH is available."
    missing = []
    if not ssh:
        missing.append("ssh")
    if not keygen:
        missing.append("ssh-keygen")
    msg = (
        "OpenSSH is not available (" + ", ".join(missing) + " not found). On "
        "Windows 10/11 enable it under Settings → Apps → Optional features → "
        "'OpenSSH Client', then try again."
    )
    return False, ssh, keygen, msg


def default_key_path():
    """The app's dedicated RunPod private-key path under the user's ~/.ssh.

    ~/.ssh is already owner-only on Windows (it lives under the user profile),
    which keeps OpenSSH from rejecting the key on permissions."""
    return os.path.join(os.path.expanduser("~"), ".ssh", _KEY_NAME)


def public_key_path(key_path):
    return key_path + ".pub"


def read_public_key(key_path):
    """Return the public key text (single line), or None if it isn't there."""
    pub = public_key_path(key_path)
    try:
        with open(pub, "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return None


def _lock_down(key_path):
    """Best-effort: make the private key owner-only so OpenSSH accepts it.

    OpenSSH on Windows refuses a private key that other accounts can read
    ("UNPROTECTED PRIVATE KEY FILE"). Keys under ~/.ssh are usually already fine,
    but a freshly created file can inherit a looser ACL — reset inheritance and
    grant only the current user. Never fatal: a perms warning still lets the key
    work in most cases, and we don't want onboarding to die on icacls.
    """
    if sys.platform != "win32":
        try:
            os.chmod(key_path, 0o600)
        except OSError:
            pass
        return
    user = os.environ.get("USERNAME") or os.environ.get("USER")
    if not user:
        return
    try:
        subprocess.run(["icacls", key_path, "/inheritance:r"],
                       capture_output=True, text=True, **_no_window())
        subprocess.run(["icacls", key_path, "/grant:r", f"{user}:F"],
                       capture_output=True, text=True, **_no_window())
    except Exception:                                    # noqa: BLE001 (best-effort)
        pass


def ensure_keypair(key_path=None):
    """Ensure a usable ed25519 keypair exists at ``key_path``; create it if not.

    Returns (key_path, public_key, created). Raises SshSetupError if OpenSSH is
    missing or ssh-keygen fails. A passphrase-less key (``-N ""``) is deliberate —
    the app drives ssh/scp unattended during a run.
    """
    key_path = key_path or default_key_path()
    ok, _ssh, keygen, msg = ssh_available()
    if not keygen:
        raise SshSetupError(msg)

    existing_pub = read_public_key(key_path)
    if os.path.exists(key_path) and existing_pub:
        # Already set up — just make sure permissions are sane and report it.
        _lock_down(key_path)
        return key_path, existing_pub, False

    os.makedirs(os.path.dirname(key_path), exist_ok=True)
    # If a stale private key exists without a .pub (or vice-versa), regenerate
    # cleanly so the pair is consistent.
    for p in (key_path, public_key_path(key_path)):
        try:
            if os.path.exists(p):
                os.remove(p)
        except OSError:
            pass

    cmd = [keygen, "-t", "ed25519", "-f", key_path, "-N", "",
           "-C", "image-toolbox-runpod", "-q"]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=60,
                             **_no_window())
    except Exception as exc:                             # noqa: BLE001
        raise SshSetupError(f"ssh-keygen could not run: {exc}") from exc
    if res.returncode != 0:
        raise SshSetupError(
            "ssh-keygen failed: " + (res.stderr or res.stdout or "unknown error").strip())

    _lock_down(key_path)
    pub = read_public_key(key_path)
    if not pub:
        raise SshSetupError("Key was generated but its public half could not be read.")
    return key_path, pub, True


def fingerprint(key_path):
    """Return the key's SHA256 fingerprint string, or None (display only)."""
    keygen = find_executable("ssh-keygen")
    if not keygen:
        return None
    try:
        res = subprocess.run([keygen, "-lf", public_key_path(key_path)],
                             capture_output=True, text=True, timeout=15,
                             **_no_window())
        if res.returncode == 0:
            return res.stdout.strip()
    except Exception:                                    # noqa: BLE001
        pass
    return None


def setup(key_path=None):
    """UI-facing one-shot: ensure OpenSSH + a keypair, never raise.

    Returns (ok, info) where info is a dict with key_path / public_key /
    created / fingerprint / message. Mirrors the (ok, …) convention of
    runpod_client.test_connection so the Settings button can call it on a thread
    and render the result without try/except.
    """
    ok, _ssh, _keygen, msg = ssh_available()
    if not ok:
        return False, {"message": msg, "key_path": key_path or default_key_path()}
    try:
        key_path, pub, created = ensure_keypair(key_path)
    except SshSetupError as exc:
        return False, {"message": str(exc), "key_path": key_path or default_key_path()}
    verb = "Generated a new" if created else "Found the existing"
    return True, {
        "message": f"{verb} SSH key — remote pods will trust it automatically.",
        "key_path": key_path,
        "public_key": pub,
        "created": created,
        "fingerprint": fingerprint(key_path),
    }


if __name__ == "__main__":
    # Manual self-test: `python ssh_setup.py` reports OpenSSH + ensures the key.
    ok, info = setup()
    print("OK" if ok else "FAILED", "-", info.get("message"))
    if ok:
        print("  key   :", info["key_path"])
        print("  pub   :", (info.get("public_key") or "")[:60], "…")
        print("  finger:", info.get("fingerprint"))
