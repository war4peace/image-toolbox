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


# ── compile cache location: a SPACE in the path breaks every inductor build ──
# torch/_inductor/cpp_builder.py assembles the compile command as a STRING and re-splits it
# with shlex.split (~line 614). Include dirs are quoted for that trip (~2023) but the SOURCE
# path is not (`" ".join(sources)`, ~1998). The default cache dir is
# %TEMP%/torchinductor_<username>, so a Windows account name with a space in it -- e.g.
# "Eduard Baniceru" -- splits the .cpp argument in half and every build dies with
# "C1083: Cannot open source file: 'Baniceru/...'". Windows already short-names the TEMP root;
# torch re-introduces the space by appending the full username to it.

def test_compile_cache_root_prefers_a_space_free_app_root(monkeypatch):
    # NOTE: these use literal paths with makedirs stubbed, NOT tmp_path. pytest's own tmp dir
    # on the machine this was written for is "pytest-of-Eduard Baniceru" -- it contains the
    # very space under test, so a tmp_path fixture cannot express "space-free root".
    monkeypatch.setattr(ts.os, "makedirs", lambda *a, **k: None)
    assert ts.compile_cache_root(r"D:\Work\image-toolbox-dev") ==         os.path.join(r"D:\Work\image-toolbox-dev", "cache")


def test_compile_cache_root_never_returns_a_path_with_a_space(monkeypatch):
    """The whole contract. If neither candidate can be made space-free it must answer None so
    the caller disables compile -- NOT hand back a path that fails at build time."""
    monkeypatch.setattr(ts.os, "makedirs", lambda *a, **k: None)
    monkeypatch.setenv("ProgramData", r"C:\Program Data")
    monkeypatch.setattr(ts, "_short_path", lambda p: p)     # 8.3 disabled on this volume
    assert ts.compile_cache_root(r"C:\App Root With Spaces") is None


def test_compile_cache_root_falls_back_to_the_8dot3_short_name(monkeypatch):
    monkeypatch.setattr(ts.os, "makedirs", lambda *a, **k: None)
    monkeypatch.setattr(ts, "_short_path", lambda p: r"C:\APPROO~1\cache")
    assert ts.compile_cache_root(r"C:\App Root") == r"C:\APPROO~1\cache"


def test_compile_cache_root_falls_back_to_programdata(monkeypatch):
    """An app installed under a spacey path still gets a cache, via ProgramData."""
    monkeypatch.setattr(ts.os, "makedirs", lambda *a, **k: None)
    monkeypatch.setenv("ProgramData", r"C:\ProgramData")
    monkeypatch.setattr(ts, "_short_path", lambda p: p)     # no 8.3 rescue
    assert ts.compile_cache_root(r"C:\App Root") ==         os.path.join(r"C:\ProgramData", "ImageToolbox", "cache")


def test_ensure_cache_env_points_inductor_and_triton_at_it(tmp_path, monkeypatch):
    monkeypatch.delenv("TORCHINDUCTOR_CACHE_DIR", raising=False)
    monkeypatch.delenv("TRITON_CACHE_DIR", raising=False)
    ok, detail = ts.ensure_cache_env(str(tmp_path))
    assert ok
    for var in ("TORCHINDUCTOR_CACHE_DIR", "TRITON_CACHE_DIR"):
        val = os.environ[var]
        assert " " not in val and os.path.isdir(val), var


def test_ensure_cache_env_respects_a_usable_existing_setting(tmp_path, monkeypatch):
    monkeypatch.setenv("TORCHINDUCTOR_CACHE_DIR", r"D:\mycache")
    ok, detail = ts.ensure_cache_env(str(tmp_path))
    assert ok and os.environ["TORCHINDUCTOR_CACHE_DIR"] == r"D:\mycache"


def test_ensure_cache_env_overrides_an_existing_setting_with_a_space(tmp_path, monkeypatch):
    """A user-set path with a space cannot work, so deferring to it would just fail later."""
    monkeypatch.setenv("TORCHINDUCTOR_CACHE_DIR", r"C:\my cache\inductor")
    ok, _ = ts.ensure_cache_env(str(tmp_path))
    assert ok and " " not in os.environ["TORCHINDUCTOR_CACHE_DIR"]


def test_ensure_cache_env_reports_failure_instead_of_raising(monkeypatch):
    monkeypatch.delenv("TORCHINDUCTOR_CACHE_DIR", raising=False)
    monkeypatch.setattr(ts, "compile_cache_root", lambda a=None: None)
    ok, detail = ts.ensure_cache_env("whatever")
    assert ok is False and "space-free" in detail


def test_short_path_is_fail_safe_on_a_missing_path():
    """GetShortPathNameW needs the path to exist; a miss must return the input, not raise."""
    p = os.path.join(tempfile.gettempdir(), "definitely-not-here-12345")
    assert ts._short_path(p) == p


def test_gate_disables_compile_when_no_space_free_cache_dir(monkeypatch):
    """End to end: a verified compiler is NOT enough. Without a space-free cache dir every
    build fails, so compile must be off rather than break mid-run."""
    import importlib.util as ilu
    import msvc_setup
    import batch_video_upscale as bv
    real = ilu.find_spec
    monkeypatch.setattr(ilu, "find_spec",
                        lambda n, *a, **k: object() if n == "triton" else real(n, *a, **k))
    monkeypatch.setattr(msvc_setup, "verify_toolchain", lambda *a, **k: (True, "ok"))
    monkeypatch.setattr(ts, "ensure_cache_env",
                        lambda root=None, log=None: (False, "no space-free cache dir"))
    s = {"compile_dit": True, "compile_vae": True}
    disabled, why = bv.gate_local_compile(s, None)
    assert disabled is True and "space-free" in why
    assert s["compile_dit"] is False and s["compile_vae"] is False
