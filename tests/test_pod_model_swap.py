"""
The pod worker's SeedVR2 model swap (#26 Part A) and the client half that drives it.

Why this exists at all: the worker loads ONE DiT at startup from worker_settings.json,
which was fine while a pod served one model. Two things ended that. The multi-model
benchmark sweeps several DiTs in one run and must do it on ONE pod (a redeploy per model
pays pod creation, volume mount and cold start each time, all billed). And a mixed-model
video queue groups by (engine, gpu) with the model deliberately NOT in the key, so two
SeedVR2 jobs with different models already shared a pod and both ran on whatever was
loaded at boot: a wrong output that reported success.

These tests are about the decisions, not the plumbing:

  * an omitted model must never reload, so every pre-0.6.3 client keeps the exact boot
    behaviour and a single-model sweep pays nothing;
  * a matching model must never reload, which is what makes the fix free for real runs
    (worker_settings and the job agree there);
  * a DIFFERENT model must close the old engine BEFORE building the new one, because
    holding both would need up to 30.7 GiB of weights for a 7B-to-7B swap;
  * and a swap that cannot happen must RAISE, never silently fall back to the loaded
    model, because for a benchmark that means filing one model's numbers under another's
    key and believing them.
"""

import sys
import types

import pytest

import pod.worker as w


@pytest.fixture
def worker_engine(monkeypatch):
    """A worker with a fake resident engine, restored after each test."""
    events = []

    class _FakeEngine:
        def __init__(self, repo_dir, model_dir, settings):
            self.args = types.SimpleNamespace(dit_model=settings.get("dit_model"))
            self.device_name = "FakeGPU"
            self.closed = False
            events.append(("built", settings.get("dit_model")))

        def close(self):
            self.closed = True
            events.append(("closed", self.args.dit_model))

    fake_mod = types.ModuleType("upscale_engine")
    fake_mod.UpscaleEngine = _FakeEngine
    fake_mod.ensure_seedvr2_weights = lambda model_dir, name, **kw: events.append(
        ("fetched", name)) or 0
    monkeypatch.setitem(sys.modules, "upscale_engine", fake_mod)
    monkeypatch.setattr(w, "_seed_validation_cache", lambda *_a, **_k: None)
    monkeypatch.setattr(w, "_empty_cuda_cache", lambda *_a, **_k: None)
    monkeypatch.setattr(w, "_log", lambda *_a, **_k: None)

    boot = {"dit_model": "seedvr2_ema_7b_fp16.safetensors", "compile_dit": True}
    monkeypatch.setattr(w, "_ENGINE", _FakeEngine("/repo", "/models", boot))
    monkeypatch.setattr(w, "_ENGINE_SETTINGS", boot)
    monkeypatch.setattr(w, "_ENGINE_ARGS", ("/repo", "/models"))
    monkeypatch.setattr(w, "_ENGINE_MODEL", boot["dit_model"])
    events.clear()
    return events


def test_no_model_named_never_reloads(worker_engine):
    """Every pre-0.6.3 client sends no model. It must mean 'whatever is loaded', or the
    first such request on an upgraded pod would pay a needless multi-GiB reload."""
    before = w._ENGINE
    for absent in (None, "", "   "):
        assert w._ensure_video_model(absent) is before
    assert worker_engine == []


def test_the_same_model_never_reloads(worker_engine):
    """The case that makes this free for real runs: worker_settings and the job agree, so
    the swap path is never entered and production behaviour is byte-identical."""
    before = w._ENGINE
    assert w._ensure_video_model("seedvr2_ema_7b_fp16.safetensors") is before
    assert worker_engine == []


def test_a_different_model_closes_before_building(worker_engine):
    """Order is the whole point. A 7B-to-7B swap holding both engines would need 30.7 GiB
    of weights resident, which no card this runs on has spare."""
    eng = w._ensure_video_model("seedvr2_ema_3b-Q8_0.gguf")
    kinds = [k for k, _ in worker_engine]
    assert kinds.index("closed") < kinds.index("built"), \
        "the previous engine must be released before the new one is allocated"
    assert ("fetched", "seedvr2_ema_3b-Q8_0.gguf") in worker_engine
    assert eng.args.dit_model == "seedvr2_ema_3b-Q8_0.gguf"
    assert w._ENGINE_MODEL == "seedvr2_ema_3b-Q8_0.gguf"


def test_a_swap_keeps_every_other_setting(worker_engine):
    """Only dit_model changes. A swap that also reset compile (or tiling) would silently
    measure a different regime than the one the sweep is filing results under."""
    eng = w._ensure_video_model("seedvr2_ema_3b-Q8_0.gguf")
    assert eng.args.dit_model == "seedvr2_ema_3b-Q8_0.gguf"
    assert w._ENGINE_SETTINGS["compile_dit"] is True
    assert w._ENGINE_SETTINGS["dit_model"] == "seedvr2_ema_7b_fp16.safetensors", \
        "the boot settings are the template and must not be mutated by a swap"


def test_a_second_swap_back_reloads_again(worker_engine):
    """There is no cache of unloaded engines: swapping back is a full reload. Pinned so
    nobody later 'optimises' it into keeping two resident, which is the OOM this avoids."""
    w._ensure_video_model("seedvr2_ema_3b-Q8_0.gguf")
    worker_engine.clear()
    w._ensure_video_model("seedvr2_ema_7b_fp16.safetensors")
    assert [k for k, _ in worker_engine].count("built") == 1
    assert w._ENGINE_MODEL == "seedvr2_ema_7b_fp16.safetensors"


def test_a_swap_with_no_engine_raises_rather_than_guessing(monkeypatch):
    """Tag mode (and a failed boot) have no SeedVR2 engine. Answering a model request with
    'here is the one I have' would hand a benchmark the wrong model's numbers."""
    monkeypatch.setattr(w, "_ENGINE", None)
    monkeypatch.setattr(w, "_ENGINE_SETTINGS", None)
    monkeypatch.setattr(w, "_ENGINE_ARGS", None)
    monkeypatch.setattr(w, "_ENGINE_MODEL", None)
    with pytest.raises(RuntimeError):
        w._ensure_video_model("seedvr2_ema_3b-Q8_0.gguf")


def test_a_failed_build_leaves_no_stale_model_recorded(worker_engine, monkeypatch):
    """If the new engine cannot be built, _ENGINE_MODEL must not still name the OLD one:
    the next request for that old model would then be answered with no engine at all."""
    import upscale_engine as fake
    monkeypatch.setattr(fake, "UpscaleEngine",
                        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        w._ensure_video_model("seedvr2_ema_3b-Q8_0.gguf")
    assert w._ENGINE is None
    assert w._ENGINE_MODEL is None


# ── the client half ──────────────────────────────────────────────────────────

def test_local_probe_refuses_a_model_it_does_not_hold():
    """The local engine bakes its DiT in at construction, so a multi-model sweep builds one
    engine per model. Verifying rather than ignoring the argument is what stops a wiring
    mistake from measuring the wrong model into a regime key and being believed."""
    import local_video_engine as lve
    eng = lve.LocalVideoEngine.__new__(lve.LocalVideoEngine)
    eng.model = "seedvr2_ema_7b_fp16.safetensors"
    eng.use_subprocess = True
    with pytest.raises(RuntimeError, match="one engine per model"):
        eng.probe_batch("in.mkv", "out.mp4", resolution=1080, batch=5,
                        model="seedvr2_ema_3b-Q8_0.gguf")


def test_both_engines_accept_the_same_probe_contract():
    """video_benchmark drives a local or a remote sweep through ONE call site, so the two
    probe_batch signatures must stay compatible (that is why `warmup_src` is accepted and
    ignored remotely, and why `model` had to reach both)."""
    import inspect
    import local_video_engine as lve
    import remote_video_engine as rve
    shared = {"resolution", "batch", "overlap", "frames", "should_stop", "on_progress",
              "warmup_src", "model"}
    for cls in (lve.LocalVideoEngine, rve.RemoteVideoEngine):
        params = set(inspect.signature(cls.probe_batch).parameters)
        assert shared <= params, f"{cls.__name__}.probe_batch is missing {shared - params}"


# ── the window that drives it (D5/D6: reach for the effect, not the code) ────

def _bench_window(monkeypatch, db_conn):
    """A real BenchmarkWindow on a stubbed remote card, so no GPU or pod is needed.

    `db_conn` is REQUIRED, not decorative: the window reads saved probes through
    `db.get_conn()`, and without it the test would open the app's real cache database. A
    sibling test that forgot this overwrote four measured RTX 5090 rows with a fake engine's
    numbers, so conftest now refuses the real path outright."""
    import tkinter as tk
    from conftest import make_tk_root
    from gui.video_benchmark import BenchmarkWindow

    root = make_tk_root()

    class _FakeTab:
        app = None

    # method is pinned to SeedVR2: the default is read from config, and on a machine whose
    # config selects Real-ESRGAN the window would have no model axis at all to test.
    win = BenchmarkWindow(root, _FakeTab(), remote=True,
                          gpu={"id": "NVIDIA GeForce RTX 4090", "memory_gb": 24},
                          method=("seedvr2", None))
    root.update_idletasks()
    return root, win


def test_the_window_builds_a_row_per_model_and_names_them(monkeypatch, db_conn):
    """The whole point of a multi-model sweep is COMPARING them, so each must get its own
    row with its own Model cell. Reads the realised widget, not the module constants."""
    import seedvr2_models as sm
    root, win = _bench_window(monkeypatch, db_conn)
    try:
        win._sweep_models = ["seedvr2_ema_7b_fp16.safetensors", "seedvr2_ema_3b-Q4_K_M.gguf"]
        win._resolve_keys()
        win._rebuild_results_table()
        root.update_idletasks()
        models = {win.tree.set(iid, "model") for iid in win._row_order}
        assert models == {"7b_fp16", "3b_q4"}
        # Every regime reads its OWN saved key, or the rows would all show the same data.
        assert len(set(win._bench_keys.values())) == len(win._bench_keys)
    finally:
        root.destroy()


def test_a_single_model_window_still_names_the_model_it_swept(monkeypatch, db_conn):
    """The Model column must never be blank: a row whose weights are unidentified is exactly
    the ambiguity this feature exists to remove."""
    root, win = _bench_window(monkeypatch, db_conn)
    try:
        win._sweep_models = ["seedvr2_ema_3b-Q8_0.gguf"]
        win._resolve_keys()
        win._rebuild_results_table()
        root.update_idletasks()
        assert win._display_modes and set(win._display_modes) <= {"off", "on"}
        assert {win.tree.set(iid, "model") for iid in win._row_order} == {"3b_q8"}
    finally:
        root.destroy()


def test_the_run_modes_match_the_rows_the_table_created(monkeypatch, db_conn):
    """_run_modes drives both the estimate and Start's row-clearing, so a token it returns
    that no row uses would silently clear nothing and estimate against an empty key."""
    root, win = _bench_window(monkeypatch, db_conn)
    try:
        win._sweep_models = ["seedvr2_ema_7b_fp16.safetensors", "seedvr2_ema_3b-Q4_K_M.gguf"]
        win._resolve_keys()
        win._rebuild_results_table()
        root.update_idletasks()
        row_modes = {m for _t, m in win._row_meta.values()}
        assert set(win._run_modes()) <= row_modes
        assert set(win._run_compile_modes()) <= {"off", "on"}
    finally:
        root.destroy()
