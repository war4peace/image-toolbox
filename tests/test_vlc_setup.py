"""
vlc_setup — the in-app libVLC installer (section 16.2). We test the presence
checks and that install() is fail-safe (a bad URL yields (False, msg), never an
exception into the GUI). No real download is performed.
"""

import os

import vlc_setup


def test_libvlc_present_reflects_disk(monkeypatch, tmp_path):
    d = tmp_path / "vlc"
    monkeypatch.setattr(vlc_setup, "vlc_dir", lambda: str(d))
    assert vlc_setup.libvlc_present() is False
    os.makedirs(d / "plugins")
    (d / "libvlc.dll").write_bytes(b"x")
    assert vlc_setup.libvlc_present() is True


def test_python_vlc_present_is_safe():
    # Whatever the env, it must return a bool and never raise.
    assert vlc_setup.python_vlc_present() in (True, False)


def test_install_is_fail_safe_on_bad_url(monkeypatch, tmp_path):
    monkeypatch.setattr(vlc_setup, "vlc_dir", lambda: str(tmp_path / "vlc"))
    # A refused local port fails fast; install() must catch and report, not raise.
    monkeypatch.setattr(vlc_setup, "VLC_URL", "http://127.0.0.1:9/nope.zip")
    ok, msg = vlc_setup.install()
    assert ok is False and isinstance(msg, str) and msg
