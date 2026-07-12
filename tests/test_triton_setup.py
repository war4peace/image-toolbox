"""Unit tests for triton_setup (pure logic: env probing, wheel lookup, hash verify)."""
import os
import sys
import hashlib
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import triton_setup as ts


def test_python_tag_matches_interpreter():
    assert ts.python_tag() == f"cp{sys.version_info.major}{sys.version_info.minor}"


def test_wheel_table_entries_are_well_formed():
    for (py_tag, torch_min), spec in ts._WHEELS.items():
        assert py_tag.startswith("cp")
        assert torch_min.count(".") == 1
        # win_amd64 wheel matching the ABI tag, with both digests pinned.
        assert spec["filename"].endswith("-win_amd64.whl")
        assert py_tag in spec["filename"]
        assert spec["url"].startswith("https://files.pythonhosted.org/")
        assert len(spec["sha256"]) == 64
        assert len(spec["md5"]) == 32
        assert spec["version"] in spec["filename"]


def test_wheel_for_env_known_and_unknown(monkeypatch):
    # A combo present in the table resolves; an unknown torch minor returns None.
    monkeypatch.setattr(ts, "python_tag", lambda: "cp312")
    monkeypatch.setattr(ts, "torch_minor", lambda: "2.11")
    if ("cp312", "2.11") in ts._WHEELS:
        assert ts.wheel_for_env() is ts._WHEELS[("cp312", "2.11")]
    monkeypatch.setattr(ts, "torch_minor", lambda: "1.0")
    assert ts.wheel_for_env() is None
    monkeypatch.setattr(ts, "torch_minor", lambda: None)
    assert ts.wheel_for_env() is None


def test_verify_download_prefers_sha256_then_md5():
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "blob.bin")
        data = b"triton-windows test payload"
        with open(p, "wb") as f:
            f.write(data)
        good_sha = hashlib.sha256(data).hexdigest()
        good_md5 = hashlib.md5(data).hexdigest()

        ok, algo, got = ts.verify_download(p, expected_sha256=good_sha)
        assert ok and algo == "sha256" and got == good_sha
        # case-insensitive
        ok, _, _ = ts.verify_download(p, expected_sha256=good_sha.upper())
        assert ok
        # wrong sha256 fails even if it could fall through (sha256 wins when present)
        ok, algo, _ = ts.verify_download(p, expected_sha256="0" * 64, expected_md5=good_md5)
        assert not ok and algo == "sha256"
        # md5 fallback only when no sha256 pinned
        ok, algo, got = ts.verify_download(p, expected_md5=good_md5)
        assert ok and algo == "md5" and got == good_md5
        # nothing pinned -> refuse
        ok, algo, got = ts.verify_download(p)
        assert not ok and algo is None and got is None


def test_torch_minor_parses_versions(monkeypatch):
    import types
    fake = types.SimpleNamespace(__version__="2.11.0+cu128")
    monkeypatch.setitem(sys.modules, "torch", fake)
    assert ts.torch_minor() == "2.11"
    fake.__version__ = "2.8.1"
    assert ts.torch_minor() == "2.8"
