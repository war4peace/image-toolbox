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
import hashlib
import tempfile
import subprocess
import urllib.request
import urllib.error

GITHUB_REPO        = "war4peace/image-toolbox"
LATEST_RELEASE_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
RELEASES_PAGE      = f"https://github.com/{GITHUB_REPO}/releases/latest"
INSTALLER_ASSET    = "ImageToolboxSetup.exe"
SHA256SUMS_ASSET   = "SHA256SUMS"     # emitted alongside the installer by CI
_USER_AGENT        = "ImageToolbox-Updater"


class UpdateInfo:
    """A newer release than the one running."""

    def __init__(self, version, tag, notes, asset_url, asset_size, sha256_url=None):
        self.version    = version      # e.g. "0.2.3" (tag without the leading v)
        self.tag        = tag          # e.g. "v0.2.3"
        self.notes      = notes        # release body (patch notes), may be ""
        self.asset_url  = asset_url    # direct download URL, or None if missing
        self.asset_size = asset_size   # bytes, or 0 if unknown
        self.sha256_url = sha256_url   # SHA256SUMS asset URL, or None if absent


def clean_notes(body):
    """
    Turn a raw GitHub release body into clean, user-facing text for the update
    dialog. Releases carry curated notes (from the annotated tag message), but
    this also defensively strips the noise that auto-generated or older bodies
    contain so the dialog never shows a bare compare URL or commit trailers:

      * the auto "**Full Changelog**: .../compare/..." line and bare compare URLs
      * commit/tag trailers (Co-Authored-By:, Signed-off-by:)
      * PGP signature blocks
      * light markdown markers (** bold **, leading # headers)

    Collapses runs of blank lines and trims. Returns "" when nothing is left.
    """
    out = []
    in_sig = False
    for line in (body or "").splitlines():
        s = line.rstrip()
        if s.startswith("-----BEGIN PGP"):
            in_sig = True
            continue
        if s.startswith("-----END PGP"):
            in_sig = False
            continue
        if in_sig:
            continue
        if re.match(r"^(Co-Authored-By|Signed-off-by):", s, re.IGNORECASE):
            continue
        # Auto-generated changelog link, with or without markdown emphasis.
        if re.match(r"^\**Full Changelog\**\s*:", s, re.IGNORECASE):
            continue
        if re.match(r"^https?://\S*/compare/\S+$", s):
            continue
        s = s.replace("**", "")                       # bold markers
        s = re.sub(r"^\s*#{1,6}\s+", "", s)           # markdown headers
        out.append(s)

    # Collapse 3+ blank lines down to one and trim the ends.
    text = "\n".join(out)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


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

    asset_url, asset_size, sha256_url = None, 0, None
    for asset in data.get("assets", []):
        name = (asset.get("name") or "").lower()
        if name == INSTALLER_ASSET.lower():
            asset_url  = asset.get("browser_download_url")
            asset_size = int(asset.get("size") or 0)
        elif name == SHA256SUMS_ASSET.lower():
            sha256_url = asset.get("browser_download_url")

    notes = clean_notes(data.get("body") or "")
    return "update", UpdateInfo(latest, tag, notes, asset_url, asset_size, sha256_url)


def parse_sha256sums(text, asset_name=INSTALLER_ASSET):
    """
    Extract the SHA-256 digest recorded for asset_name from the text of a
    SHA256SUMS file, or None when it isn't present. The format is the standard
    `sha256sum` output, one entry per line:

        <64 hex chars><spaces>[*]<filename>

    Matching is by basename and case-insensitive on the name; the digest is
    returned lower-cased. Robust to the binary-mode '*' marker and to full or
    bare filenames. Pure (no I/O) so it can be unit-tested.
    """
    want = os.path.basename(asset_name).lower()
    for line in (text or "").splitlines():
        parts = line.strip().split()
        if len(parts) < 2:
            continue
        digest = parts[0]
        if not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
            continue
        name = os.path.basename(parts[-1].lstrip("*")).lower()
        if name == want:
            return digest.lower()
    return None


def fetch_expected_sha256(sums_url, asset_name=INSTALLER_ASSET, timeout=30):
    """
    Download the release's SHA256SUMS file and return the digest recorded for
    asset_name, or None when the file (or that entry) can't be read. Fail-safe:
    any network/parse error yields None so a release published without the asset
    (older releases) simply falls back to the size check rather than blocking.
    """
    if not sums_url:
        return None
    try:
        req = urllib.request.Request(sums_url, headers={"User-Agent": _USER_AGENT})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode("utf-8", "replace")
    except Exception:
        return None
    return parse_sha256sums(text, asset_name)


def sha256_of_file(path, chunk=1 << 20):
    """Streaming SHA-256 of a file, returned as lower-case hex."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def download_installer(url, expected_size=0, sha256_url=None, dest_dir=None,
                       progress_cb=None, timeout=30):
    """
    Download the installer to a temp file (written to a .part file, then renamed
    so a partial download can't be mistaken for a complete one).

    progress_cb(downloaded_bytes, total_bytes) is called as data arrives; total
    is 0 when the server doesn't send Content-Length. Returns the final path.
    Raises on any network/IO error, if the finished size doesn't match the
    release's recorded asset size (a corruption / truncation guard), or if the
    file fails the SHA-256 recorded in the release's SHA256SUMS. When no
    SHA256SUMS is available (sha256_url absent, or an older release without the
    asset) the size check remains the only guard - the hash step is skipped, not
    treated as a failure.
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

    # Integrity gate: verify against the release-published SHA-256 when present.
    expected_hash = fetch_expected_sha256(sha256_url)
    if expected_hash:
        actual = sha256_of_file(part)
        if actual != expected_hash:
            try:
                os.remove(part)
            except OSError:
                pass
            raise IOError(
                "The downloaded installer failed its SHA-256 integrity check "
                f"(got {actual}, expected {expected_hash}). The file may be "
                "corrupted or tampered with; the update was aborted.")

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
