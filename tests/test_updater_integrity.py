"""
Supply-chain integrity for the in-app updater (recommendations item 4).

Covers the SHA256SUMS parsing, the streaming file hash, and download_installer's
integrity gate: a matching hash passes, a mismatch aborts and cleans up, a
missing SHA256SUMS falls back to the size check (older releases), and the size
guard still fires. Pure stdlib, torch-free; urlopen is faked so nothing hits the
network.
"""

import hashlib
import os

import pytest

import updater  # noqa: E402

ASSET = "ImageToolboxSetup.exe"


def _read_bytes(path):
    with open(path, "rb") as f:
        return f.read()


# ── parse_sha256sums ─────────────────────────────────────────────────────────

def test_parse_two_space_format():
    d = "a" * 64
    assert updater.parse_sha256sums(f"{d}  {ASSET}\n") == d


def test_parse_binary_star_marker():
    d = "b" * 64
    assert updater.parse_sha256sums(f"{d} *{ASSET}") == d


def test_parse_matches_by_basename_and_is_case_insensitive():
    d = "C" * 64
    text = f"{d}  ./dist/{ASSET.upper()}"
    assert updater.parse_sha256sums(text) == d.lower()


def test_parse_picks_the_right_line_among_many():
    other = "1" * 64
    want = "2" * 64
    text = f"{other}  something-else.zip\n{want}  {ASSET}\n"
    assert updater.parse_sha256sums(text) == want


def test_parse_returns_none_when_absent_or_garbage():
    assert updater.parse_sha256sums(f"{'a'*64}  other.exe") is None
    assert updater.parse_sha256sums("") is None
    assert updater.parse_sha256sums(None) is None
    assert updater.parse_sha256sums("not-a-hash  " + ASSET) is None
    assert updater.parse_sha256sums("deadbeef  " + ASSET) is None  # too short


# ── sha256_of_file ───────────────────────────────────────────────────────────

def test_sha256_of_file_matches_hashlib(tmp_path):
    data = b"the quick brown fox" * 5000
    p = tmp_path / "blob.bin"
    p.write_bytes(data)
    assert updater.sha256_of_file(str(p)) == hashlib.sha256(data).hexdigest()


# ── download_installer integrity gate ────────────────────────────────────────

class _FakeResp:
    def __init__(self, data, headers=None):
        self._data = data
        self.headers = headers or {}
        self._pos = 0

    def read(self, n=-1):
        if n is None or n < 0:
            chunk = self._data[self._pos:]
        else:
            chunk = self._data[self._pos:self._pos + n]
        self._pos += len(chunk)
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _install_fake_urlopen(monkeypatch, installer_bytes, sums_text):
    def fake_urlopen(req, timeout=None, context=None):
        url = getattr(req, "full_url", req)
        if url.endswith(updater.SHA256SUMS_ASSET):
            return _FakeResp((sums_text or "").encode("utf-8"))
        return _FakeResp(installer_bytes,
                         {"Content-Length": str(len(installer_bytes))})
    monkeypatch.setattr(updater.urllib.request, "urlopen", fake_urlopen)


def test_download_passes_matching_hash(tmp_path, monkeypatch):
    data = b"INSTALLER" * 1000
    sums = f"{hashlib.sha256(data).hexdigest()}  {ASSET}\n"
    _install_fake_urlopen(monkeypatch, data, sums)
    path = updater.download_installer(
        "http://x/" + ASSET, expected_size=len(data),
        sha256_url="http://x/" + updater.SHA256SUMS_ASSET, dest_dir=str(tmp_path))
    assert _read_bytes(path) == data
    assert not os.path.exists(path + ".part")


def test_download_aborts_on_hash_mismatch(tmp_path, monkeypatch):
    data = b"INSTALLER" * 1000
    sums = f"{'0' * 64}  {ASSET}\n"            # wrong digest
    _install_fake_urlopen(monkeypatch, data, sums)
    with pytest.raises(IOError, match="integrity check"):
        updater.download_installer(
            "http://x/" + ASSET, expected_size=len(data),
            sha256_url="http://x/" + updater.SHA256SUMS_ASSET, dest_dir=str(tmp_path))
    # Neither the partial nor the final file survives a failed check.
    assert not os.path.exists(os.path.join(str(tmp_path), ASSET))
    assert not os.path.exists(os.path.join(str(tmp_path), ASSET + ".part"))


def test_download_falls_back_when_no_sha256_url(tmp_path, monkeypatch):
    # An older release with no SHA256SUMS asset: the size check is the only guard.
    data = b"INSTALLER" * 1000
    _install_fake_urlopen(monkeypatch, data, "")
    path = updater.download_installer(
        "http://x/" + ASSET, expected_size=len(data),
        sha256_url=None, dest_dir=str(tmp_path))
    assert _read_bytes(path) == data


def test_download_skips_check_when_asset_missing_the_entry(tmp_path, monkeypatch):
    # SHA256SUMS exists but lists other files only -> skip, don't hard-fail.
    data = b"INSTALLER" * 1000
    sums = f"{'a' * 64}  some-other-file.zip\n"
    _install_fake_urlopen(monkeypatch, data, sums)
    path = updater.download_installer(
        "http://x/" + ASSET, expected_size=len(data),
        sha256_url="http://x/" + updater.SHA256SUMS_ASSET, dest_dir=str(tmp_path))
    assert _read_bytes(path) == data


def test_download_size_mismatch_still_raises(tmp_path, monkeypatch):
    data = b"INSTALLER" * 1000
    _install_fake_urlopen(monkeypatch, data, "")
    with pytest.raises(IOError, match="size mismatch"):
        updater.download_installer(
            "http://x/" + ASSET, expected_size=len(data) + 1,
            sha256_url=None, dest_dir=str(tmp_path))
