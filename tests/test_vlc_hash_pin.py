"""
Item 7: the pinned libVLC download is integrity-checked.

The libVLC zip is a fixed, immutable versioned artifact, so vlc_setup.py now verifies it
against a baked-in SHA-256 (bootstrap.ps1 does the same). A mismatch = corrupt/tampered
download -> refuse it, rather than extract an unknown DLL into the app. These tests pin
the gate (mismatch rejected, match proceeds) and that both installers share one hash.
"""

import os
import shutil
import zipfile

import pytest

import vlc_setup


def test_sha256_mismatch_is_rejected(monkeypatch):
    monkeypatch.setattr(vlc_setup, "_download",
                        lambda url, dest, progress=None: open(dest, "wb").write(b"junk"))
    with pytest.raises(RuntimeError) as ei:
        vlc_setup._install_libvlc()
    assert "SHA-256" in str(ei.value)


def test_matching_hash_passes_the_gate(tmp_path, monkeypatch):
    # Build a small zip, pin VLC_SHA256 to ITS digest, and confirm _install_libvlc gets
    # PAST the hash gate: it then fails on the missing libVLC layout, not on the hash.
    zip_src = tmp_path / "fake.zip"
    with zipfile.ZipFile(zip_src, "w") as zf:
        zf.writestr("readme.txt", "hi")
    monkeypatch.setattr(vlc_setup, "VLC_SHA256", vlc_setup._sha256(str(zip_src)))
    monkeypatch.setattr(vlc_setup, "_download",
                        lambda url, dest, progress=None: shutil.copyfile(zip_src, dest))
    with pytest.raises(RuntimeError) as ei:
        vlc_setup._install_libvlc()
    assert "SHA-256" not in str(ei.value)      # gate passed; failed on the archive layout


def test_bootstrap_and_vlc_setup_pin_the_same_artifact():
    root = os.path.dirname(os.path.dirname(os.path.abspath(vlc_setup.__file__)))
    text = open(os.path.join(root, "bootstrap.ps1"), encoding="utf-8").read()
    assert vlc_setup.VLC_SHA256 in text        # keep-in-sync guard
    assert vlc_setup.VLC_URL in text
    assert len(vlc_setup.VLC_SHA256) == 64 and vlc_setup.VLC_SHA256.islower()
