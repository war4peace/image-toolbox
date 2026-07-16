"""
MSVC discovery + activation for local torch.compile (feature #7).

Every branch here is a state a REAL machine was actually found in, which is the only reason
the module is this paranoid:

  1. Visual Studio never puts cl.exe on PATH -- it lives only inside a Developer Command
     Prompt. The GUI launches from Explorer, so a perfect toolchain looks absent.
  2. cl.exe on disk proves NOTHING: one install had it launching and reporting 19.41.34120
     with no CRT headers and no SDK. Only compiling caught it.
  3. VS 2026 (v18) ships NO vcvarsall.bat -- only Common7\\Tools\\VsDevCmd.bat.
  4. A machine had a COMPLETE, verified-working MSVC + SDK while vswhere reported no C++
     components at all and VsDevCmd set INCLUDE/LIB from the SDK alone, silently omitting
     MSVC. Every official activation path refused a compiler that compiles, links and runs.

So: discovery is permissive (it only decides what to TRY) and verification compiles.
"""
import os
import subprocess

import pytest

import msvc_setup as ms


@pytest.fixture(autouse=True)
def _clear_caches(monkeypatch):
    monkeypatch.setattr(ms, "_ENV_CACHE", None)
    monkeypatch.setattr(ms, "_ENV_PROBED", False)
    monkeypatch.setattr(ms, "_ENV_SOURCE", None)
    monkeypatch.setattr(ms, "_VERIFY_CACHE", None)


class _CP:
    def __init__(self, stdout="", returncode=0):
        self.stdout, self.stderr, self.returncode = stdout, "", returncode


def _fake_cl_dir(tmp_path, name="bin"):
    """A directory containing a cl.exe, which is what _env_has_msvc looks for."""
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "cl.exe").write_text("")
    return str(d)


# ── find_msvc: permissive on purpose ─────────────────────────────────────────

def test_find_msvc_fast_path_uses_the_component(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(ms, "vswhere_path", lambda: "vswhere.exe")

    def fake_run(cmd, **kw):
        seen.setdefault("first", cmd)
        return _CP(str(tmp_path))
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert ms.find_msvc() == str(tmp_path)
    assert "-requires" in seen["first"] and ms.VC_TOOLS_COMPONENT in seen["first"]
    # Build Tools is a separate SKU the default product filter hides, and it is exactly what
    # a non-developer installs for this.
    assert "-products" in seen["first"] and "*" in seen["first"]


def test_find_msvc_falls_back_to_a_compiler_on_disk(monkeypatch, tmp_path):
    """THE VS-2026 case. vswhere reported NO MATCH for VC.Tools.x86.x64, VC.CoreIde AND
    VC.Redist.14.Latest at once, on an install whose MSVC compiles, links and runs. A
    component ID we cannot keep current across VS versions must not veto a working compiler."""
    inst = tmp_path / "vs18"
    (inst / "VC" / "Tools" / "MSVC" / "14.44.35207" / "bin" / "Hostx64" / "x64").mkdir(parents=True)
    (inst / "VC" / "Tools" / "MSVC" / "14.44.35207" / "bin" / "Hostx64" / "x64" / "cl.exe").write_text("")
    monkeypatch.setattr(ms, "vswhere_path", lambda: "vswhere.exe")
    monkeypatch.setattr(subprocess, "run",
                        lambda cmd, **k: _CP("" if "-requires" in cmd else str(inst)))
    assert ms.find_msvc() == str(inst)


def test_find_msvc_fallback_is_not_a_rubber_stamp(monkeypatch, tmp_path):
    """A VS with no C++ at all (this machine's VS 2022) carries no cl.exe -> still None."""
    inst = tmp_path / "vs_no_cpp"
    inst.mkdir()
    monkeypatch.setattr(ms, "vswhere_path", lambda: "vswhere.exe")
    monkeypatch.setattr(subprocess, "run",
                        lambda cmd, **k: _CP("" if "-requires" in cmd else str(inst)))
    assert ms.find_msvc() is None


def test_find_msvc_never_raises(monkeypatch):
    monkeypatch.setattr(ms, "vswhere_path", lambda: "vswhere.exe")
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("boom")))
    assert ms.find_msvc() is None


def test_no_vswhere_means_no_msvc(monkeypatch):
    monkeypatch.setattr(ms, "vswhere_path", lambda: None)
    assert ms.find_msvc() is None


# ── activation_scripts: both entry points, because Microsoft changed them ────

def test_activation_scripts_prefers_vcvarsall_then_vsdevcmd(monkeypatch, tmp_path):
    inst = tmp_path / "vs"
    va = inst / "VC" / "Auxiliary" / "Build" / "vcvarsall.bat"
    vs = inst / "Common7" / "Tools" / "VsDevCmd.bat"
    for p in (va, vs):
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("@echo off")
    monkeypatch.setattr(ms, "find_msvc", lambda: str(inst))
    got = ms.activation_scripts()
    assert [os.path.basename(s) for s, _ in got] == ["vcvarsall.bat", "VsDevCmd.bat"]
    assert got[0][1] == "x64" and "-arch=x64" in got[1][1]


def test_activation_scripts_finds_vsdevcmd_when_vcvarsall_is_absent(monkeypatch, tmp_path):
    """VS 2026 ships no vcvarsall.bat at all. Requiring it locked out a whole VS generation."""
    inst = tmp_path / "vs18"
    vs = inst / "Common7" / "Tools" / "VsDevCmd.bat"
    vs.parent.mkdir(parents=True)
    vs.write_text("@echo off")
    monkeypatch.setattr(ms, "find_msvc", lambda: str(inst))
    got = ms.activation_scripts()
    assert len(got) == 1 and os.path.basename(got[0][0]) == "VsDevCmd.bat"


# ── toolset / SDK discovery on disk ──────────────────────────────────────────

def test_msvc_toolset_skips_a_newer_but_incomplete_version(monkeypatch, tmp_path):
    """A real machine had TWO toolset dirs: 14.51 a half-populated shell, 14.44 the working
    compiler. Picking by version alone would have chosen the broken one."""
    inst = tmp_path / "vs"
    good = inst / "VC" / "Tools" / "MSVC" / "14.44.35207"
    (good / "bin" / "Hostx64" / "x64").mkdir(parents=True)
    (good / "bin" / "Hostx64" / "x64" / "cl.exe").write_text("")
    (good / "include").mkdir()
    (good / "lib" / "x64").mkdir(parents=True)
    (inst / "VC" / "Tools" / "MSVC" / "14.51.36231").mkdir(parents=True)   # shell only
    monkeypatch.setattr(ms, "_installs", lambda: [str(inst)])
    binp, inc, lib = ms.msvc_toolset()
    assert "14.44.35207" in binp and "14.44.35207" in inc and "14.44.35207" in lib


def test_ver_key_orders_numerically_not_lexically():
    assert ms._ver_key("14.44.35207") > ms._ver_key("14.9.12345")   # string sort says otherwise


def test_windows_sdk_requires_the_ucrt_headers(monkeypatch, tmp_path):
    """stdio.h has been in the SDK's UCRT (not MSVC) since VS2015; without it cl.exe cannot
    compile the simplest C file. An SDK dir that exists but lacks it is not an SDK."""
    kits = tmp_path / "Windows Kits" / "10"
    for s in ("ucrt", "um", "shared"):
        (kits / "Include" / "10.0.28000.0" / s).mkdir(parents=True)
    for s in ("ucrt", "um"):
        (kits / "Lib" / "10.0.28000.0" / s / "x64").mkdir(parents=True)
    monkeypatch.setenv("ProgramFiles(x86)", str(tmp_path))
    assert ms.windows_sdk() is None, "no stdio.h -> not usable"
    (kits / "Include" / "10.0.28000.0" / "ucrt" / "stdio.h").write_text("")
    incs, libs = ms.windows_sdk()
    assert any("ucrt" in p for p in incs) and any("ucrt" in p for p in libs)


def test_manual_env_puts_msvc_ahead_of_the_sdk(monkeypatch, tmp_path):
    binp = _fake_cl_dir(tmp_path, "msvc/bin")
    monkeypatch.setattr(ms, "msvc_toolset", lambda: (binp, r"C:\msvc\inc", r"C:\msvc\lib"))
    monkeypatch.setattr(ms, "windows_sdk", lambda: ([r"C:\sdk\ucrt"], [r"C:\sdk\lib\ucrt"]))
    env = ms.manual_env()
    assert env["INCLUDE"].split(";")[0] == r"C:\msvc\inc"
    assert env["LIB"].split(";")[0] == r"C:\msvc\lib"
    assert env["PATH"].startswith(binp)


def test_manual_env_none_without_an_sdk(monkeypatch):
    monkeypatch.setattr(ms, "msvc_toolset", lambda: ("b", "i", "l"))
    monkeypatch.setattr(ms, "windows_sdk", lambda: None)
    assert ms.manual_env() is None


# ── vcvars_env: the tier chain ───────────────────────────────────────────────

def test_vcvars_env_returns_only_the_delta(monkeypatch, tmp_path):
    """Apply what MSVC needs, not a wholesale replacement of our own environment."""
    binp = _fake_cl_dir(tmp_path)
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(ms, "activation_scripts", lambda p=None: [("vcvarsall.bat", "x64")])
    monkeypatch.setattr(ms, "_run_activation_script", lambda s, a: {
        "PATH": binp + ";C:\\already", "INCLUDE": "C:\\msvc\\inc", "UNCHANGED": "same"})
    monkeypatch.setenv("UNCHANGED", "same")
    monkeypatch.setenv("PATH", "C:\\already")
    env = ms.vcvars_env()
    assert env["INCLUDE"] == "C:\\msvc\\inc"
    assert "UNCHANGED" not in env, "an unchanged var is not part of the delta"
    assert ms.activation_source() == "vcvarsall.bat"


def test_vcvars_env_falls_through_a_script_that_omits_msvc(monkeypatch, tmp_path):
    """THE VS-2026 case, exactly. VsDevCmd exits 0 and sets INCLUDE/LIB from the Windows SDK
    while never adding MSVC, so a non-empty env proves nothing. Insisting on a reachable
    cl.exe is what makes us fall through to building the env from disk."""
    binp = _fake_cl_dir(tmp_path)
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(ms, "activation_scripts", lambda p=None: [("VsDevCmd.bat", "-arch=x64")])
    monkeypatch.setattr(ms, "_run_activation_script", lambda s, a: {
        "INCLUDE": r"C:\sdk\ucrt", "LIB": r"C:\sdk\lib", "PATH": r"C:\windows"})  # no cl.exe
    monkeypatch.setattr(ms, "manual_env", lambda: {"PATH": binp, "INCLUDE": "i", "LIB": "l"})
    env = ms.vcvars_env()
    assert env["PATH"] == binp
    assert "disk" in ms.activation_source()


def test_vcvars_env_none_when_nothing_yields_a_compiler(monkeypatch):
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(ms, "activation_scripts", lambda p=None: [])
    monkeypatch.setattr(ms, "manual_env", lambda: None)
    assert ms.vcvars_env() is None
    assert ms.activation_source() is None


def test_vcvars_env_is_cached(monkeypatch, tmp_path):
    binp = _fake_cl_dir(tmp_path)
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(ms, "activation_scripts", lambda p=None: [("vcvarsall.bat", "x64")])
    calls = []
    monkeypatch.setattr(ms, "_run_activation_script",
                        lambda s, a: (calls.append(1), {"PATH": binp, "INCLUDE": "i"})[1])
    ms.vcvars_env()
    ms.vcvars_env()
    assert len(calls) == 1, "activation costs seconds and cannot change mid-run"


def test_run_activation_script_passes_a_string_not_a_list(monkeypatch):
    """REGRESSION, and one no mock could have caught before it bit us. Passing
    ["cmd.exe","/s","/c",'"<path>" && set'] makes Python's list2cmdline backslash-escape the
    inner quotes; cmd then reports 'is not recognized as an internal or external command' and
    a perfectly good vcvarsall looks missing. It must go as a STRING with shell=True."""
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"], seen["shell"] = cmd, kw.get("shell")
        return _CP("INCLUDE=x\n")
    monkeypatch.setattr(subprocess, "run", fake_run)
    ms._run_activation_script(r"C:\Program Files\vs\vcvarsall.bat", "x64")
    assert isinstance(seen["cmd"], str), "a list gets its quotes mangled by list2cmdline"
    assert seen["shell"] is True
    assert seen["cmd"].startswith('"C:\\Program Files\\vs\\vcvarsall.bat" x64')
    assert "&& set" in seen["cmd"]


def test_run_activation_script_none_on_failure(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _CP("", returncode=1))
    assert ms._run_activation_script("x.bat", "x64") is None


def test_run_activation_script_skips_malformed_lines(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _CP(
        "[VsDevCmd.bat] Environment initialized for: 'x64'\n"
        "INCLUDE=C:\\i\nProgramFiles(x86)=C:\\pf86\n"))
    env = ms._run_activation_script("x.bat", "x64")
    assert env["ProgramFiles(x86)"] == "C:\\pf86"        # parens are legal in names
    assert not any("Environment initialized" in k for k in env)


# ── ensure_msvc_env ──────────────────────────────────────────────────────────

def test_ensure_msvc_env_activates_and_reports_the_real_method(monkeypatch, tmp_path):
    """The core feature: no Developer Command Prompt required. And it must not LIE about how
    it got there -- the first cut hardcoded 'activated via vcvarsall' and said so on a
    machine with no vcvarsall anywhere."""
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(ms, "vcvars_env", lambda *a, **k: {"INCLUDE": "C:\\msvc\\inc"})
    monkeypatch.setattr(ms, "activation_source", lambda: "built from disk")
    seq = iter([None, "C:\\msvc\\bin\\cl.exe"])          # absent, then present after activation
    monkeypatch.setattr(ms.shutil, "which", lambda n: next(seq))
    ok, detail = ms.ensure_msvc_env()
    assert ok and detail == "activated: built from disk"
    assert os.environ["INCLUDE"] == "C:\\msvc\\inc"


def test_ensure_msvc_env_reports_the_install_hint_when_unavailable(monkeypatch):
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(ms.shutil, "which", lambda n: None)
    monkeypatch.setattr(ms, "vcvars_env", lambda *a, **k: None)
    monkeypatch.setattr(ms, "vswhere_path", lambda: "vswhere.exe")
    monkeypatch.setattr(ms, "find_msvc", lambda: None)
    ok, detail = ms.ensure_msvc_env()
    assert ok is False
    assert "Individual components" in detail, "tell a VS owner what to ADD, not to reinstall"


def test_ensure_msvc_env_is_a_noop_when_cl_is_already_there(monkeypatch):
    monkeypatch.setattr(ms.shutil, "which", lambda n: "C:\\cl.exe")
    monkeypatch.setattr(ms, "vcvars_env",
                        lambda *a, **k: pytest.fail("must not re-activate needlessly"))
    assert ms.ensure_msvc_env() == (True, "cl.exe already on PATH")


# ── verify_toolchain: the check that cannot be fooled ────────────────────────

def test_verify_toolchain_catches_a_compiler_that_cannot_compile(monkeypatch):
    """THE regression. cl.exe present, on PATH, launching, reporting its version -- and
    unable to find stdio.h because the SDK was never installed."""
    monkeypatch.setattr(ms, "ensure_msvc_env", lambda log=None: (True, "on PATH"))
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _CP(
        "probe.c\nprobe.c(1): fatal error C1034: stdio.h: no include path set", returncode=2))
    ok, detail = ms.verify_toolchain()
    assert ok is False and "C1034" in detail


def test_verify_toolchain_passes_on_a_real_compile(monkeypatch):
    monkeypatch.setattr(ms, "ensure_msvc_env", lambda log=None: (True, "on PATH"))
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _CP("", returncode=0))
    assert ms.verify_toolchain()[0] is True


def test_verify_toolchain_short_circuits_when_activation_failed(monkeypatch):
    monkeypatch.setattr(ms, "ensure_msvc_env", lambda log=None: (False, "no MSVC"))
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: pytest.fail("must not probe without a compiler"))
    assert ms.verify_toolchain() == (False, "no MSVC")


def test_verify_toolchain_never_raises(monkeypatch):
    monkeypatch.setattr(ms, "ensure_msvc_env", lambda log=None: (True, "on PATH"))
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("no cl")))
    ok, detail = ms.verify_toolchain()
    assert ok is False and "probe failed" in detail


def test_verify_toolchain_is_cached(monkeypatch):
    monkeypatch.setattr(ms, "ensure_msvc_env", lambda log=None: (True, "on PATH"))
    calls = []
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: (calls.append(1), _CP("", returncode=0))[1])
    ms.verify_toolchain()
    ms.verify_toolchain()
    assert len(calls) == 1


# ── the gate uses it ─────────────────────────────────────────────────────────

@pytest.fixture
def triton_present(monkeypatch):
    """Pin Triton as installed. Without this the gate's own triton check would decide these
    tests, so they'd pass or fail on whether the DEV's machine happens to have the wheel."""
    import importlib.util as ilu
    real = ilu.find_spec
    monkeypatch.setattr(ilu, "find_spec",
                        lambda n, *a, **k: object() if n == "triton" else real(n, *a, **k))


def test_gate_local_compile_disables_on_a_stub_toolchain(monkeypatch, triton_present):
    """End to end: Triton plus a stub cl.exe must run UNCOMPILED, with advice. Left on,
    inductor would hang the first segment under the GUI's piped stdio."""
    import batch_video_upscale as bv
    monkeypatch.setattr(ms, "verify_toolchain",
                        lambda *a, **k: (False, "cl.exe cannot compile: C1034"))
    monkeypatch.setattr(ms, "install_hint", lambda: "add MSVC v143 + Windows 11 SDK")
    logged = []
    s = {"compile_dit": True, "compile_vae": True}
    disabled, why = bv.gate_local_compile(s, logged.append)
    assert disabled is True and "C1034" in why
    assert s["compile_dit"] is False and s["compile_vae"] is False
    assert any("add MSVC v143" in m for m in logged), "must say how to fix it"


def test_gate_local_compile_without_triton_never_probes_the_compiler(monkeypatch):
    """No Triton means no compile regardless of MSVC, so don't pay for a compiler probe."""
    import importlib.util as ilu
    import batch_video_upscale as bv
    real = ilu.find_spec
    monkeypatch.setattr(ilu, "find_spec",
                        lambda n, *a, **k: None if n == "triton" else real(n, *a, **k))
    monkeypatch.setattr(ms, "verify_toolchain",
                        lambda *a, **k: pytest.fail("must not probe cl.exe without Triton"))
    s = {"compile_dit": True, "compile_vae": True}
    disabled, why = bv.gate_local_compile(s, None)
    assert disabled is True and why == "Triton is not installed"


def test_gate_local_compile_survives_an_old_install_with_no_msvc_setup(monkeypatch,
                                                                      triton_present):
    """Guarded import: an install predating msvc_setup.py must fall back, not crash a run."""
    import builtins
    import batch_video_upscale as bv
    real_import = builtins.__import__

    def fake(name, *a, **k):
        if name == "msvc_setup":
            raise ImportError("no such module")
        return real_import(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", fake)
    s = {"compile_dit": True, "compile_vae": True}
    disabled, why = bv.gate_local_compile(s, None)      # must not raise
    assert isinstance(disabled, bool)
