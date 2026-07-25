"""
Feature #7 (local video upscaling) runner wiring.

Covers the pieces batch_video_upscale gained for the LOCAL path, none of which need a
GPU / ffmpeg / pod:

  * resolve_video_cfg exposes the local knobs (thrash_stall_seconds, local_use_subprocess).
  * _local_seedvr2_paths resolves the vendored seedvr2 repo + SEEDVR2 weights off the app
    root (same keys the Batch Upscaler reads).
  * run_queue treats a ThrashDetected (the local thrash watchdog) as a DEGRADATION episode:
    it STOPS the run (does not roll on to the next job), leaves the job `partial` (so its
    finished segments resume next run), and does NOT bump the source's fail_count.
  * _stop_notice maps a "gpu thrash" stop to a red, resume-hinted notice.
"""

import os

import pytest

import db
import batch_video_upscale as bv
import video_estimate as ve


@pytest.fixture
def db_conn(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_DIR", str(tmp_path / "db"))
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "db" / "cache.db"))
    monkeypatch.setattr(db, "_conn", None)
    monkeypatch.setattr(db, "import_legacy_json", lambda conn: None)
    conn = db.get_conn()
    yield conn
    conn.close()
    monkeypatch.setattr(db, "_conn", None)


# ── config resolution ────────────────────────────────────────────────────────

def test_resolve_video_cfg_local_defaults():
    v = bv.resolve_video_cfg({})
    assert v["thrash_stall_seconds"] == 300
    # Default is IN-PROCESS (like the image Batch Upscaler); the nested-worker subprocess
    # path is opt-in (it was the sole source of the local stall-at-first-segment hangs).
    assert v["local_use_subprocess"] is False


def test_resolve_video_cfg_local_overrides():
    v = bv.resolve_video_cfg({"video": {"thrash_stall_seconds": 120,
                                        "local_use_subprocess": True}})
    assert v["thrash_stall_seconds"] == 120
    assert v["local_use_subprocess"] is True


def test_video_resident_threshold_default_and_overrides():
    # Separate from the image knob (upscale.vram_resident_threshold_gb, default 40): video
    # defaults higher (90) because a temporal decode is far heavier than a single image.
    assert bv.resolve_video_cfg({})["vram_resident_threshold_gb"] == 90.0
    assert bv.resolve_video_cfg(
        {"video": {"vram_resident_threshold_gb": 120}})["vram_resident_threshold_gb"] == 120.0
    # 0 is meaningful (always phase) and must NOT be coerced to the default; null falls back.
    assert bv.resolve_video_cfg(
        {"video": {"vram_resident_threshold_gb": 0}})["vram_resident_threshold_gb"] == 0.0
    assert bv.resolve_video_cfg(
        {"video": {"vram_resident_threshold_gb": None}})["vram_resident_threshold_gb"] == 90.0


def test_initial_load_restores_defaults_before_loading_queue():
    # Regression: when the Video Upscaler is the restored startup tab, the durable queue showed
    # blank because the startup _load_queue ran before the folder fields were filled from the
    # pinned defaults (so it early-returned on an empty source, leaving previously-cut segments
    # invisible until the user acted). _initial_load must restore defaults FIRST, then load.
    import types
    pytest.importorskip("tkinter")
    from gui.tab_video import VideoTab
    calls = []
    fake = types.SimpleNamespace(
        restore_defaults_if_empty=lambda: calls.append("restore"),
        _load_queue=lambda: calls.append("load"),
    )
    VideoTab._initial_load(fake)
    assert calls == ["restore", "load"]


def test_video_resident_threshold_is_independent_of_image():
    # The video threshold does not disturb the image one, and the video worker settings inject
    # the video value over the image default so the pod engine reads the right number.
    cfg = {"upscale": {"vram_resident_threshold_gb": 40}, "video": {}}
    vcfg = bv.resolve_video_cfg(cfg)
    assert cfg["upscale"]["vram_resident_threshold_gb"] == 40      # image untouched
    assert vcfg["vram_resident_threshold_gb"] == 90.0             # video default


# ── shared compile-capability gate (runner AND benchmark) ────────────────────

# The gate's capability seam is msvc_setup.verify_toolchain, NOT shutil.which("cl").
# `which` was the old contract and it is provably wrong in BOTH directions:
#   * false NEGATIVE: Visual Studio never puts cl.exe on PATH (it lives in a Developer
#     Command Prompt), so a perfect toolchain looked absent to the Explorer-launched GUI.
#     msvc_setup activates it via vcvarsall instead of giving up.
#   * false POSITIVE: a real machine had cl.exe on disk, launching, reporting 19.41.34120,
#     with no CRT headers and no Windows SDK -- `which` said yes and the compile would have
#     hung. Only compiling a hello world can tell the difference.
# See tests/test_msvc_setup.py for the discovery/activation/verification logic itself.

def test_gate_local_compile_disables_without_compiler(monkeypatch):
    # Triton present but no USABLE compiler = the exact "stuck at first segment/probe" hang
    # the gate prevents (inductor shells out to cl.exe under piped stdio). Both the local
    # runner and the benchmark route their engine settings through here, so the benchmark
    # can no longer trip the hang the runner already guarded against.
    import importlib.util as ilu
    import msvc_setup
    monkeypatch.setattr(ilu, "find_spec", lambda name: object() if name == "triton" else None)
    monkeypatch.setattr(msvc_setup, "verify_toolchain",
                        lambda *a, **k: (False, "no C compiler (MSVC) is usable"))
    s = {"compile_dit": True, "compile_vae": True}
    disabled, why = bv.gate_local_compile(s)
    assert disabled is True and "compiler" in why.lower()
    assert s["compile_dit"] is False and s["compile_vae"] is False


def test_gate_local_compile_keeps_compile_when_capable(monkeypatch):
    import importlib.util as ilu
    import msvc_setup
    monkeypatch.setattr(ilu, "find_spec", lambda name: object())  # triton present
    monkeypatch.setattr(msvc_setup, "verify_toolchain",
                        lambda *a, **k: (True, "cl.exe compiled a test file"))
    s = {"compile_dit": True, "compile_vae": True}
    disabled, why = bv.gate_local_compile(s)
    assert disabled is False and why is None
    assert s["compile_dit"] is True and s["compile_vae"] is True


def test_gate_local_compile_noop_when_compile_off(monkeypatch):
    # Nothing to gate (and no capability probe / log) when compile was never requested.
    called = {"probe": False}

    def _boom(*a, **k):
        called["probe"] = True
        return None
    monkeypatch.setattr("shutil.which", _boom)
    s = {"compile_dit": False, "compile_vae": False}
    assert bv.gate_local_compile(s) == (False, None)
    assert called["probe"] is False


# ── inductor's CPU vector-ISA probe deadlocks an in-process local run ─────────

def _capable(monkeypatch):
    """Gate sees Triton + a verified compiler, i.e. it is about to enable compile."""
    import importlib.util as ilu
    import msvc_setup
    monkeypatch.setattr(ilu, "find_spec", lambda name: object())
    monkeypatch.setattr(msvc_setup, "verify_toolchain",
                        lambda *a, **k: (True, "cl.exe compiled a test file"))


def test_enabling_compile_disarms_the_vec_isa_probe(monkeypatch):
    """THE hang. inductor verifies its AVX probe by spawning a subprocess with stdout
    INHERITED; under an in-process --local run that child deadlocks at interpreter startup
    and check_call blocks forever (observed: 16m20s on "Encoding batch 1/3", only released
    by killing the probe). Setting the env var skips the subprocess."""
    monkeypatch.delenv("TORCHINDUCTOR_VEC_ISA_OK", raising=False)
    _capable(monkeypatch)
    s = {"compile_dit": True, "compile_vae": True}
    assert bv.gate_local_compile(s) == (False, None)
    assert os.environ.get("TORCHINDUCTOR_VEC_ISA_OK") == "1"


def test_an_explicit_vec_isa_value_is_never_overridden(monkeypatch):
    """setdefault, not assignment: a user/dev who set the var deliberately (including to 0 to
    re-enable the probe while debugging it) must win over our default."""
    monkeypatch.setenv("TORCHINDUCTOR_VEC_ISA_OK", "0")
    _capable(monkeypatch)
    bv.gate_local_compile({"compile_dit": True, "compile_vae": True})
    assert os.environ.get("TORCHINDUCTOR_VEC_ISA_OK") == "0"


def test_vec_isa_probe_is_left_alone_when_compile_is_disabled(monkeypatch):
    """No compile means no inductor, so there is no probe to disarm and no reason to touch
    the environment of a run that will never call it."""
    monkeypatch.delenv("TORCHINDUCTOR_VEC_ISA_OK", raising=False)
    import importlib.util as ilu
    import msvc_setup
    monkeypatch.setattr(ilu, "find_spec", lambda name: object())
    monkeypatch.setattr(msvc_setup, "verify_toolchain", lambda *a, **k: (False, "no compiler"))
    s = {"compile_dit": True, "compile_vae": True}
    bv.gate_local_compile(s)
    assert s["compile_dit"] is False
    assert "TORCHINDUCTOR_VEC_ISA_OK" not in os.environ


# ── mixed-queue regression: the vec_isa flag must be armed BEFORE torch import ──

def test_arm_vec_isa_ok_early_sets_when_compile_configured(monkeypatch):
    """A mixed local queue imports torch (via a Real-ESRGAN job) before the SeedVR2 gate runs, so
    the env var must be armed up front from the run's compile setting -- otherwise inductor.config
    freezes vec_isa_ok=None and the probe subprocess recurses. Off when compile is not configured."""
    monkeypatch.delenv("TORCHINDUCTOR_VEC_ISA_OK", raising=False)
    bv._arm_vec_isa_ok_early({"compile": True})
    assert os.environ.get("TORCHINDUCTOR_VEC_ISA_OK") == "1"

    monkeypatch.delenv("TORCHINDUCTOR_VEC_ISA_OK", raising=False)
    bv._arm_vec_isa_ok_early({"compile": False})
    assert "TORCHINDUCTOR_VEC_ISA_OK" not in os.environ


def test_arm_vec_isa_ok_early_respects_explicit_value(monkeypatch):
    monkeypatch.setenv("TORCHINDUCTOR_VEC_ISA_OK", "0")     # a dev re-enabling the probe wins
    bv._arm_vec_isa_ok_early({"compile": True})
    assert os.environ.get("TORCHINDUCTOR_VEC_ISA_OK") == "0"


def test_disarm_overrides_a_frozen_none_config(monkeypatch):
    """If torch._inductor.config is ALREADY imported (earlier torch use) its vec_isa_ok is frozen
    from the env at import time. When that froze to None the env var alone is too late, so the
    disarm sets the resolved config value directly."""
    import sys
    import types
    fake = types.ModuleType("torch._inductor.config")
    fake.cpp = types.SimpleNamespace(vec_isa_ok=None)
    monkeypatch.setitem(sys.modules, "torch._inductor.config", fake)
    monkeypatch.delenv("TORCHINDUCTOR_VEC_ISA_OK", raising=False)
    bv._disarm_vec_isa_probe()
    assert os.environ.get("TORCHINDUCTOR_VEC_ISA_OK") == "1"
    assert fake.cpp.vec_isa_ok is True                      # frozen None overridden directly


def test_disarm_leaves_an_already_resolved_config_alone(monkeypatch):
    """A config that already resolved (True/False, e.g. the env was set at its import) is a real
    decision -- never clobber it."""
    import sys
    import types
    for val in (True, False):
        fake = types.ModuleType("torch._inductor.config")
        fake.cpp = types.SimpleNamespace(vec_isa_ok=val)
        monkeypatch.setitem(sys.modules, "torch._inductor.config", fake)
        bv._disarm_vec_isa_probe()
        assert fake.cpp.vec_isa_ok is val


# ── seedvr2 path resolution ──────────────────────────────────────────────────

def test_local_seedvr2_paths_defaults_to_app_root():
    repo, model = bv._local_seedvr2_paths({})
    assert repo == os.path.join(bv.APP_ROOT, "seedvr2")
    assert model == os.path.join(bv.APP_ROOT, "models", "SEEDVR2")


def test_local_seedvr2_paths_honours_config_and_expands_env(monkeypatch):
    monkeypatch.setenv("MYWEIGHTS", "W")
    repo, model = bv._local_seedvr2_paths(
        {"seedvr2": {"repo_dir": r"C:\seed\repo",
                     "model_dir": r"%MYWEIGHTS%\SEEDVR2"}})
    assert repo == r"C:\seed\repo"                       # absolute: used as-is
    # relative (post-expansion) is anchored at the app root
    assert model == os.path.normpath(os.path.join(bv.APP_ROOT, "W", "SEEDVR2"))


# ── _stop_notice: the local GPU-thrash stop ──────────────────────────────────

def test_stop_notice_gpu_thrash_is_red_and_resumable():
    title, color, resume = bv._stop_notice("gpu thrash")
    assert "thrash" in title.lower()
    assert color == 0xE74C3C                             # red (a degradation, not a plain pause)
    assert resume is True                                # the queue resumes after a reboot


# ── run_queue: ThrashDetected stops the run without blaming the source ────────

def test_thrash_stops_run_leaves_partial_no_failcount(db_conn, tmp_path, monkeypatch):
    src_root = str(tmp_path / "src")
    out_root = str(tmp_path / "out")
    os.makedirs(src_root, exist_ok=True)
    root = db.get_video_root_id(db_conn, src_root, out_root)
    # Two queued jobs: the FIRST thrashes; the second must NOT be attempted (a degraded
    # GPU would just thrash again, so the run stops after the episode).
    db.upsert_video_output(db_conn, root, "a.avi", "1080p", status="queued", queue_order=0)
    db.upsert_video_output(db_conn, root, "b.avi", "1080p", status="queued", queue_order=1)

    seen = []

    def fake_process_job(engine, conn, root_id, source_root, job, *a, **k):
        seen.append(job["rel_path"])
        raise bv._ThrashDetected("no GPU progress for 300s at batch 17")

    monkeypatch.setattr(bv, "process_job", fake_process_job)

    vcfg = bv.resolve_video_cfg({})
    vcfg["work_root"] = str(tmp_path / "work")

    summary = bv.run_queue(None, db_conn, root, src_root, vcfg, bv.RunBudget(0, 0.0))

    assert seen == ["a.avi"]                              # stopped after the thrash, b untouched
    assert summary["stopped"] == "gpu thrash"
    assert summary["failed"] == 0                         # NOT a source failure
    a = db.get_video_output(db_conn, root, "a.avi", "1080p")
    assert a["status"] == "partial"                      # resumes next run
    assert a["fail_count"] == 0                           # source never blamed
    b = db.get_video_output(db_conn, root, "b.avi", "1080p")
    assert b["status"] == "queued"                       # never attempted


# ── local TIME estimate (video_estimate) ─────────────────────────────────────

def _job(frames=100, target="1080p", w=1920, h=1080, segs=1):
    return {"frames": frames, "target": target, "segments": segs, "width": w, "height": h}


def test_local_estimate_none_without_history_or_seed():
    # An unseeded card with no history: honestly decline to invent a time.
    assert ve.LOCAL_RATES == {}                          # ships with no fabricated seeds
    assert ve.estimate_queue_local([_job()], "NVIDIA GeForce RTX 4070", conn=None) is None


def test_local_estimate_uses_measured_history_calibrated(db_conn):
    gpu = "NVIDIA GeForce RTX 3090"
    # 1000 output-MP over 1600 s -> 1.6 s/MP (>= the 300-MP trust floor).
    db.record_gpu_perf(db_conn, "video-mp-1080p", gpu, 1000, 1600.0, min_images=300)
    est = ve.estimate_queue_local([_job(frames=100, w=1920, h=1080)], gpu, conn=db_conn)
    assert est is not None and est["calibrated"] is True
    # 1920x1080 fits the 1080p box 1:1 -> 2.0736 MP/frame; 100 * 2.0736 * 1.6 s.
    assert est["duration_seconds"] == pytest.approx(100 * 2.0736 * 1.6, rel=1e-3)
    assert est["segments"] == 1 and est["total_frames"] == 100


def test_local_estimate_seed_is_flagged_rough(monkeypatch):
    monkeypatch.setattr(ve, "LOCAL_RATES", {"1080p": {"TESTCARD": 2.0}})
    monkeypatch.setattr(ve, "_LOCAL_MODEL_TOKENS", [("TESTCARD", "TESTCARD")])
    est = ve.estimate_queue_local([_job(frames=10, w=1920, h=1080)], "MyTestCard 24GB", conn=None)
    assert est is not None and est["calibrated"] is False   # seeded, not measured -> "(rough)"
    assert est["duration_seconds"] == pytest.approx(10 * 2.0736 * 2.0, rel=1e-3)


def test_local_estimate_falls_back_across_targets_when_one_unrated(db_conn):
    gpu = "NVIDIA GeForce RTX 3090"
    db.record_gpu_perf(db_conn, "video-mp-1080p", gpu, 1000, 1600.0, min_images=300)
    # 1080p is measured; 4K has no rate of its OWN, but the card's pooled video-MP history
    # covers it (rough, cross-target). So the queue estimate is NO LONGER None -- it keeps the
    # progress bar / estimate alive on the first run of a new target -- but is flagged rough.
    jobs = [_job(target="1080p"), _job(target="4K", w=3840, h=2160)]
    est = ve.estimate_queue_local(jobs, gpu, conn=db_conn)
    assert est is not None
    assert est["calibrated"] is False            # 4K used the fallback -> not fully measured


def test_local_estimate_still_none_with_no_history_for_any_target(db_conn):
    gpu = "NVIDIA GeForce RTX 3090"
    # No video-MP history at all for this card -> nothing to fall back to -> honest None.
    jobs = [_job(target="4K", w=3840, h=2160)]
    assert ve.estimate_queue_local(jobs, gpu, conn=db_conn) is None


def test_seconds_per_mp_cross_target_fallback_keeps_progress_bar_alive(db_conn):
    # The Video tab's live progress bar gets its per-segment time budget from
    # estimate_job -> seconds_per_mp. Without a cross-target fallback, the FIRST run of a new
    # target (e.g. 4x after the card has only run 2x) has no rate -> the bar sits dead at 0 %
    # the whole run (the reported bug). The pooled video-MP history must cover it.
    gpu = "NVIDIA GeForce RTX 3090"
    db.record_gpu_perf(db_conn, "video-mp-2X", gpu, 1000, 1600.0, min_images=300)
    assert ve.seconds_per_mp(gpu, "2X", conn=db_conn) == pytest.approx(1.6, rel=1e-3)  # exact
    # 4X has no rate of its own -> pooled fallback (1.6 s/MP) so estimate_job is non-None.
    assert ve.seconds_per_mp(gpu, "4X", conn=db_conn) == pytest.approx(1.6, rel=1e-3)
    secs = ve.estimate_job(100, "4X", gpu, conn=db_conn, src_w=640, src_h=480)
    assert secs is not None and secs > 0
    # A card with no video history at all still declines (no fabrication).
    assert ve.seconds_per_mp("Totally Unknown GPU", "4X", conn=db_conn) is None
