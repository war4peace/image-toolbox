"""
tests/test_video_probe_warmup.py
--------------------------------
The benchmark's warm pass: a probe must report what a real SEGMENT costs, not what a
fresh process costs.

A probe runs in its own process (mandatory: an honest upward VRAM sweep needs a fresh CUDA
context per attempt) while a real run is ONE process for a whole video. So a probe charged
itself, per rung, two costs production pays once per run and then amortises away:

  * the 16 GB model load, which happens inside the FIRST process_video (UpscaleEngine.__init__
    only DOWNLOADS the weights, then caches the runner). Regime-independent: it inflated
    UNCOMPILED probes too, by ~2x on a short probe.
  * torch.compile's ~30s of per-process dynamo/guard/inductor-cache work, paid at every new
    shape, and every rung IS a new shape under static compile.

Measured on a 3090 at 540x720, that made a compiled sweep report ~2.5x SLOWER than
uncompiled, while the same two configs on a real 545-frame video had compile 16% FASTER.
The stored rate also feeds video_estimate's cost/duration numbers, so this was never just a
reading error.

Torch-free: the engine is faked, so the real warmup control flow is exercised without a GPU.
"""

import json
import sys
import time
import types

import pytest

import local_video_worker as lvw


@pytest.fixture
def fake_engine(monkeypatch):
    """Fake `upscale_engine.UpscaleEngine` so local_video_worker.main() runs for real.

    Each pass sleeps a per-pass duration, so the REPORTED seconds can be attributed to one
    specific pass instead of being asserted as a bare call count. `events` records the
    interleaving of passes and VRAM releases.
    """
    calls = []
    events = []
    plan = {"sleep": {}, "oom_on": None, "events": events}

    class _FakeEngine:
        def __init__(self, repo_dir, model_dir, settings, debug=False):
            plan["settings"] = settings

        def process_video(self, src, dest, **kw):
            i = len(calls)
            kw = dict(kw, _src=src)                   # capture the input path per pass
            calls.append(kw)
            events.append(f"pass{i}")
            if plan["oom_on"] == i:
                raise RuntimeError("CUDA out of memory. Tried to allocate 2.00 GiB")
            time.sleep(plan["sleep"].get(i, 0.0))
            return 37

        def close(self):
            pass

    mod = types.ModuleType("upscale_engine")
    mod.UpscaleEngine = _FakeEngine
    monkeypatch.setitem(sys.modules, "upscale_engine", mod)
    # Stand in for torch so the VRAM release + per-pass peak reads are observable with no GPU.
    # `peaks` is the (alloc, reserved) each successive read returns, so a test can make the
    # cold pass and the warm pass differ and check which number lands where.
    plan["peaks"] = [(20.0, 22.0)] * 8
    # A reset only advances to the next scripted pass if something was READ since the last
    # one: the worker resets once up-front (to start pass 0 from a clean mark) without
    # reading, and that must not consume pass 0's numbers.
    reads = {"n": 0, "dirty": False}

    def _read(idx):
        reads["dirty"] = True
        seq = plan["peaks"]
        return seq[min(reads["n"], len(seq) - 1)][idx] * 1024 ** 3

    def _reset():
        events.append("reset")
        if reads["dirty"]:
            reads["n"] += 1
            reads["dirty"] = False

    torch_mod = types.ModuleType("torch")
    torch_mod.cuda = types.SimpleNamespace(
        reset_peak_memory_stats=_reset,
        synchronize=lambda: events.append("synchronize"),
        empty_cache=lambda: events.append("empty_cache"),
        max_memory_allocated=lambda: _read(0),
        max_memory_reserved=lambda: _read(1),
    )
    monkeypatch.setitem(sys.modules, "torch", torch_mod)
    return calls, plan


def _run(tmp_path, extra=()):
    settings = tmp_path / "s.json"
    settings.write_text(json.dumps({"dit_model": "seedvr2_ema_7b_fp16.safetensors"}))
    argv = ["--repo-dir", str(tmp_path), "--model-dir", str(tmp_path),
            "--settings", str(settings), "--input", str(tmp_path / "in.mp4"),
            "--output", str(tmp_path / "out.mp4"), "--resolution", "1080",
            "--batch", "17", "--chunk", "51", "--overlap", "6"] + list(extra)
    return lvw.main(argv)


def _result_tokens(capsys):
    for line in capsys.readouterr().out.splitlines():
        if lvw.RESULT_MARK in line:
            return dict(t.split("=", 1) for t in line.split() if "=" in t)
    return None


def test_warmup_pass_runs_first_and_only_the_second_is_timed(tmp_path, fake_engine, capsys):
    """THE fix. Pass 0 absorbs the model load + compile; pass 1 is the measurement.

    The warmup is made slow and the timed pass fast, so a reported time near the SLOW pass
    would mean we are still timing the cold one.
    """
    calls, plan = fake_engine
    plan["sleep"] = {0: 0.40, 1: 0.0}

    rc = _run(tmp_path, ["--warmup-passes", "1"])

    assert rc == 0
    assert len(calls) == 2, "one throwaway pass, then the timed one"
    tok = _result_tokens(capsys)
    assert tok is not None and "seconds" in tok
    assert float(tok["seconds"]) < 0.2, \
        "reported the WARM pass, not the 0.40s cold one"


def test_no_warmup_runs_exactly_one_pass(tmp_path, fake_engine, capsys):
    """Production and any caller that does not ask: unchanged, one pass."""
    calls, plan = fake_engine

    rc = _run(tmp_path)

    assert rc == 0
    assert len(calls) == 1
    assert _result_tokens(capsys)["frames"] == "37"


def test_several_warmup_passes_are_honoured(tmp_path, fake_engine, capsys):
    calls, plan = fake_engine

    _run(tmp_path, ["--warmup-passes", "3"])

    assert len(calls) == 4, "3 discarded + 1 timed"


def test_every_pass_uses_the_same_window(tmp_path, fake_engine, capsys):
    """The warmup only warms the shape it RUNS. If it drifted off the timed pass's batch it
    would compile a graph the measurement never uses, and silently do nothing: exactly the
    'bs9 warmup ran fine' / 'batch 9: oom' trap already recorded in _warmup_cell."""
    calls, plan = fake_engine

    _run(tmp_path, ["--warmup-passes", "1"])

    assert len(calls) == 2
    keys = ("batch_size", "chunk_size", "temporal_overlap", "resolution")
    assert {k: calls[0][k] for k in keys} == {k: calls[1][k] for k in keys}
    assert calls[0]["batch_size"] == 17


def test_warmup_input_sends_the_warmup_to_the_short_clip_and_times_the_full_one(
        tmp_path, fake_engine, capsys):
    """The short-warmup optimisation: the discarded pass runs on --warmup-input while the timed
    pass runs on --input. The window (--batch) is what compiles, so the short clip warms the
    same graph; the measured pass must still be the full timed clip."""
    calls, plan = fake_engine
    warm = tmp_path / "warm.mp4"

    rc = _run(tmp_path, ["--warmup-passes", "1", "--warmup-input", str(warm)])

    assert rc == 0
    assert len(calls) == 2
    assert calls[0]["_src"] == str(warm), "warmup ran on the short clip"
    assert calls[1]["_src"] == str(tmp_path / "in.mp4"), "timed pass ran on the full clip"


def test_warmup_input_absent_warms_on_the_full_input(tmp_path, fake_engine, capsys):
    """Fallback: with no --warmup-input, both passes use --input (the pre-0.5.0 behaviour)."""
    calls, plan = fake_engine

    _run(tmp_path, ["--warmup-passes", "1"])

    assert calls[0]["_src"] == calls[1]["_src"] == str(tmp_path / "in.mp4")


def test_the_two_passes_peaks_are_reported_separately(tmp_path, fake_engine, capsys):
    """The cold pass (model load + torch.compile, whose inductor autotuning allocates at the
    compiled shape and varies run to run) and the warm pass cost very different amounts. The
    marker must carry the WORST case as the ceiling AND the steady pass on its own, or a
    compile-bound ceiling is indistinguishable from an inference-bound one."""
    calls, plan = fake_engine
    plan["peaks"] = [(21.4, 24.2),      # read #1: after the warmup pass  (cold + compile)
                     (18.4, 20.0)]      # read #2: after the timed pass   (steady state)

    _run(tmp_path, ["--warmup-passes", "1"])

    tok = _result_tokens(capsys)
    assert (tok["alloc"], tok["reserved"]) == ("21.4", "24.2"), "ceiling = the worst pass"
    assert (tok["alloc_steady"], tok["reserved_steady"]) == ("18.4", "20.0")


def test_the_ceiling_peak_is_the_worst_pass_even_if_the_warm_one_is_bigger(tmp_path,
                                                                          fake_engine, capsys):
    """Not 'the first pass': the worst of them. Order is an assumption, max() is not."""
    calls, plan = fake_engine
    plan["peaks"] = [(17.0, 18.0), (19.5, 21.0)]

    _run(tmp_path, ["--warmup-passes", "1"])

    tok = _result_tokens(capsys)
    assert (tok["alloc"], tok["reserved"]) == ("19.5", "21.0")
    assert tok["alloc_steady"] == "19.5"


def test_an_unwarmed_probe_reports_no_steady_peak(tmp_path, fake_engine, capsys):
    """One pass = one measurement. Emitting a steady peak equal to the only peak would claim
    a distinction that was never measured."""
    calls, plan = fake_engine

    _run(tmp_path)

    tok = _result_tokens(capsys)
    assert "alloc" in tok
    assert "alloc_steady" not in tok and "reserved_steady" not in tok


def test_the_warmups_vram_is_released_before_the_timed_pass(tmp_path, fake_engine, capsys):
    """A probe gets a fresh process precisely so a previous attempt's fragmented VRAM cannot
    poison it (an in-process sweep fragments and UNDER-REPORTS; Windows has no
    expandable_segments). A warmup puts TWO passes in one process and re-opens that door, so
    the warmup's pool must go back to the driver before the timed pass allocates."""
    calls, plan = fake_engine

    _run(tmp_path, ["--warmup-passes", "1"])

    assert [e for e in plan["events"] if e != "reset"] == \
        ["pass0", "synchronize", "empty_cache", "pass1"], \
        "release must sit BETWEEN the passes (and sync before freeing)"


def test_no_release_happens_without_a_warmup(tmp_path, fake_engine, capsys):
    """One pass has nothing to release, and emptying the cache would only cost time."""
    calls, plan = fake_engine

    _run(tmp_path)

    assert [e for e in plan["events"] if e != "reset"] == ["pass0"]


def test_an_oom_in_the_warmup_reports_oom_and_names_the_pass(tmp_path, fake_engine, capsys):
    """A batch that OOMs while warming does not fit, which is the same answer the sweep
    wants from a timed OOM. It must exit on the OOM path so the rung records a ceiling --
    but name the pass, since a WARMUP OOM is a real wall while a TIMED OOM (it fit cold,
    then not in the warmup's leftovers) is the warmup's own side effect."""
    calls, plan = fake_engine
    plan["oom_on"] = 0

    rc = _run(tmp_path, ["--warmup-passes", "1"])

    assert rc == lvw.OOM_EXIT
    out = capsys.readouterr().out
    assert lvw.OOM_MARK in out and "pass=warmup" in out
    assert len(calls) == 1, "stopped at the failing warmup, no timed pass attempted"


def test_a_timed_oom_is_named_as_such(tmp_path, fake_engine, capsys):
    calls, plan = fake_engine
    plan["oom_on"] = 1

    _run(tmp_path, ["--warmup-passes", "1"])

    assert "pass=timed" in capsys.readouterr().out


def test_an_oom_in_the_timed_pass_still_reports_oom(tmp_path, fake_engine, capsys):
    calls, plan = fake_engine
    plan["oom_on"] = 1

    rc = _run(tmp_path, ["--warmup-passes", "1"])

    assert rc == lvw.OOM_EXIT
    assert len(calls) == 2


# ── parent side: LocalVideoEngine.probe_batch ───────────────────────────────────

import local_video_engine as lve                    # noqa: E402


def _probe_engine(monkeypatch, attempt_result, wall=5.0):
    """A LocalVideoEngine whose worker subprocess is replaced by a canned attempt dict."""
    eng = lve.LocalVideoEngine.__new__(lve.LocalVideoEngine)
    eng.use_subprocess = True
    eng._settings = {}
    eng.model = "seedvr2_ema_7b_fp16.safetensors"
    eng._repo_dir = eng._model_dir = "."
    eng._settings_path = None
    seen = {}

    def fake_attempt(src, dest, resolution, batch, chunk, overlap, seed, backend,
                     use_10bit, should_stop, warmup_passes=0, warmup_src=None):
        seen["warmup_passes"] = warmup_passes
        seen["batch"] = batch
        seen["warmup_src"] = warmup_src
        return attempt_result
    monkeypatch.setattr(eng, "_run_subprocess_attempt", fake_attempt)
    clock = iter([0.0, wall, wall, wall])
    monkeypatch.setattr(lve.time, "time", lambda: next(clock))
    return eng, seen


def _attempt(frames=37, alloc=20.0, reserved=22.0, seconds=12.5,
             steady_alloc=None, steady_reserved=None):
    return {"frames": frames, "alloc": alloc, "reserved": reserved, "seconds": seconds,
            "steady_alloc": steady_alloc, "steady_reserved": steady_reserved}


def test_probe_asks_the_worker_to_warm_and_reports_the_worker_time(monkeypatch):
    """The whole point: the stored `seconds` is the worker's WARM pass, not the parent's
    wall clock (which also carries process spawn, the torch import and the model load)."""
    eng, seen = _probe_engine(monkeypatch, _attempt(seconds=12.5), wall=95.0)

    res = eng.probe_batch("in.mkv", "out.mp4", resolution=1080, batch=17, frames=37)

    assert seen["warmup_passes"] == lve.PROBE_WARMUP_PASSES >= 1
    assert res["outcome"] == "ok"
    assert res["seconds"] == 12.5, "the warm pass, not the 95s wall time"
    assert res["peak_alloc_gb"] == 20.0


def test_probe_falls_back_to_wall_time_when_the_worker_reports_none(monkeypatch):
    """Fail-safe for an older worker (or any path that reports no timing): a slightly wrong
    number beats no number, and this keeps the engines' probe_batch contract total."""
    eng, _ = _probe_engine(monkeypatch, _attempt(seconds=None), wall=61.0)

    res = eng.probe_batch("in.mkv", "out.mp4", resolution=1080, batch=17, frames=37)

    assert res["seconds"] == 61.0


def test_probe_carries_both_peaks_through(monkeypatch):
    """The ceiling peak and the steady peak answer different questions and must both survive
    to the caller: one is what a run's FIRST segment needs, the other what every later one
    needs. Fused, a compile-bound ceiling reads identically to an inference-bound one."""
    eng, _ = _probe_engine(monkeypatch,
                           _attempt(alloc=21.4, reserved=24.2,
                                    steady_alloc=18.4, steady_reserved=20.0))

    res = eng.probe_batch("in.mkv", "out.mp4", resolution=1080, batch=5, frames=37)

    assert (res["peak_alloc_gb"], res["peak_reserved_gb"]) == (21.4, 24.2)
    assert (res["peak_alloc_steady_gb"], res["peak_reserved_steady_gb"]) == (18.4, 20.0)


def test_an_unwarmed_probe_reports_no_steady_peak(monkeypatch):
    """One pass = one measurement; inventing a 'steady' number equal to the cold one would
    claim a distinction that was never measured."""
    eng, _ = _probe_engine(monkeypatch, _attempt())

    res = eng.probe_batch("in.mkv", "out.mp4", resolution=1080, batch=5, frames=37)

    assert res["peak_alloc_steady_gb"] is None
    assert res["peak_reserved_steady_gb"] is None


def _fake_worker(tmp_path, result_line, monkeypatch):
    """Point the engine at a throwaway script that prints `result_line`, so the REAL
    subprocess plumbing (spawn, the reader thread, the marker parser) is what runs.
    Asserting against a re-implementation of the parser here would prove nothing."""
    script = tmp_path / "fake_worker.py"
    script.write_text(
        "import sys\n"
        "print('heartbeat', flush=True)\n"
        f"print({result_line!r}, flush=True)\n"
        "sys.exit(0)\n", encoding="utf-8")
    monkeypatch.setattr(lve, "_WORKER", str(script))
    eng = lve.LocalVideoEngine.__new__(lve.LocalVideoEngine)
    eng.use_subprocess = True
    eng._repo_dir = eng._model_dir = str(tmp_path)
    eng._settings = {}
    eng.model = "seedvr2_ema_7b_fp16.safetensors"
    eng._settings_path = str(tmp_path / "s.json")
    (tmp_path / "s.json").write_text("{}", encoding="utf-8")
    eng.thrash_stall_seconds = 60
    return eng


def test_the_real_parser_reads_every_token_off_the_marker(tmp_path, monkeypatch):
    """End-to-end through the actual subprocess reader: spawn, reader thread, marker parse."""
    eng = _fake_worker(
        tmp_path, f"{lve._RESULT_MARK} frames=37 alloc=20.6 reserved=22.1 seconds=12.5 "
                  f"alloc_steady=18.4 reserved_steady=20.0", monkeypatch)

    out = eng._run_subprocess_attempt(str(tmp_path / "i.mp4"), str(tmp_path / "o.mp4"),
                                      1080, 17, 51, 6, None, "opencv", False, None,
                                      warmup_passes=1)

    assert out == {"frames": 37, "alloc": 20.6, "reserved": 22.1, "seconds": 12.5,
                   "steady_alloc": 18.4, "steady_reserved": 20.0}


def test_the_real_parser_survives_a_worker_that_reports_only_the_old_tokens(tmp_path, monkeypatch):
    """An older worker on an upgraded install: no seconds / no steady peaks. The attempt must
    still succeed with Nones so probe_batch falls back to wall time and claims no steady
    measurement it did not take."""
    eng = _fake_worker(
        tmp_path, f"{lve._RESULT_MARK} frames=37 alloc=20.6 reserved=22.1", monkeypatch)

    out = eng._run_subprocess_attempt(str(tmp_path / "i.mp4"), str(tmp_path / "o.mp4"),
                                      1080, 17, 51, 6, None, "opencv", False, None)

    assert out == {"frames": 37, "alloc": 20.6, "reserved": 22.1, "seconds": None,
                   "steady_alloc": None, "steady_reserved": None}


def test_the_oom_pass_reaches_the_error_message(tmp_path, monkeypatch):
    """The attribution has to survive the marker -> parser -> raise chain, or it never
    reaches the log where a human reads it."""
    eng = _fake_worker(tmp_path, f"{lve._OOM_MARK} batch=5 pass=timed", monkeypatch)

    with pytest.raises(RuntimeError, match=r"out of memory.*\[timed pass\]"):
        eng._run_subprocess_attempt(str(tmp_path / "i.mp4"), str(tmp_path / "o.mp4"),
                                    1080, 5, 90, 4, None, "opencv", False, None,
                                    warmup_passes=1)


def test_an_oom_without_attribution_still_raises_cleanly(tmp_path, monkeypatch):
    """Fail-safe for an older worker: no pass= token, no crash, no stray '[None pass]'."""
    eng = _fake_worker(tmp_path, f"{lve._OOM_MARK} batch=5", monkeypatch)

    with pytest.raises(RuntimeError) as e:
        eng._run_subprocess_attempt(str(tmp_path / "i.mp4"), str(tmp_path / "o.mp4"),
                                    1080, 5, 90, 4, None, "opencv", False, None)
    assert "pass]" not in str(e.value)


def test_warmup_passes_reaches_the_worker_command_line(tmp_path, monkeypatch):
    """The flag has to actually be passed: the whole fix is inert if the child never sees it."""
    seen = {}
    real_popen = lve.subprocess.Popen

    def spy(cmd, **kw):
        seen["cmd"] = cmd
        return real_popen(cmd, **kw)
    eng = _fake_worker(tmp_path, f"{lve._RESULT_MARK} frames=1 alloc=1 reserved=1 seconds=1",
                       monkeypatch)
    monkeypatch.setattr(lve.subprocess, "Popen", spy)

    eng._run_subprocess_attempt(str(tmp_path / "i.mp4"), str(tmp_path / "o.mp4"),
                                1080, 17, 51, 6, None, "opencv", False, None,
                                warmup_passes=2)
    assert "--warmup-passes" in seen["cmd"]
    assert seen["cmd"][seen["cmd"].index("--warmup-passes") + 1] == "2"

    eng._run_subprocess_attempt(str(tmp_path / "i.mp4"), str(tmp_path / "o.mp4"),
                                1080, 17, 51, 6, None, "opencv", False, None)
    assert "--warmup-passes" not in seen["cmd"], "not passed when not asked for"


def test_warmup_input_reaches_the_worker_only_when_it_differs_from_the_timed_clip(
        tmp_path, monkeypatch):
    """A distinct short clip is forwarded as --warmup-input; the SAME clip (or None) is not,
    so the worker falls back to --input and no redundant flag is emitted."""
    seen = {}
    real_popen = lve.subprocess.Popen

    def spy(cmd, **kw):
        seen["cmd"] = cmd
        return real_popen(cmd, **kw)
    eng = _fake_worker(tmp_path, f"{lve._RESULT_MARK} frames=1 alloc=1 reserved=1 seconds=1",
                       monkeypatch)
    monkeypatch.setattr(lve.subprocess, "Popen", spy)

    full = str(tmp_path / "i.mp4")
    warm = str(tmp_path / "warm.mp4")
    eng._run_subprocess_attempt(full, str(tmp_path / "o.mp4"), 1080, 17, 51, 6, None,
                                "opencv", False, None, warmup_passes=1, warmup_src=warm)
    assert "--warmup-input" in seen["cmd"]
    assert seen["cmd"][seen["cmd"].index("--warmup-input") + 1] == warm

    eng._run_subprocess_attempt(full, str(tmp_path / "o.mp4"), 1080, 17, 51, 6, None,
                                "opencv", False, None, warmup_passes=1, warmup_src=full)
    assert "--warmup-input" not in seen["cmd"], "same clip as the timed pass: no separate flag"

    eng._run_subprocess_attempt(full, str(tmp_path / "o.mp4"), 1080, 17, 51, 6, None,
                                "opencv", False, None, warmup_passes=1)
    assert "--warmup-input" not in seen["cmd"], "no warmup_src given"


def test_probe_batch_forwards_the_warmup_clip_to_the_attempt(monkeypatch):
    """probe_batch is the seam the benchmark passes the short clip through; it must reach the
    worker attempt, not be dropped on the floor."""
    eng, seen = _probe_engine(monkeypatch, _attempt())

    eng.probe_batch("full.mkv", "out.mp4", resolution=1080, batch=17, frames=37,
                    warmup_src="warm.mkv")

    assert seen["warmup_src"] == "warm.mkv"


# ── pod side: the resident worker's probe ───────────────────────────────────────

from pod import worker as podw                      # noqa: E402


@pytest.fixture
def pod_engine(monkeypatch):
    """Fake the pod's resident _ENGINE. The pod spares us the model load (it loads once at
    startup) but NOT torch.compile, which is per-shape and so per-rung."""
    calls = []
    plan = {"sleep": {}, "oom_on": None}

    class _E:
        def process_video(self, src, dest, **kw):
            i = len(calls)
            calls.append(kw)
            if plan["oom_on"] == i:
                raise RuntimeError("CUDA out of memory. Tried to allocate 2.00 GiB")
            time.sleep(plan["sleep"].get(i, 0.0))
            return 37

    monkeypatch.setattr(podw, "_ENGINE", _E())
    monkeypatch.setattr(podw, "_touch", lambda *a, **k: None)
    monkeypatch.setattr(podw, "_log", lambda *a, **k: None)
    return calls, plan


def _pod_params(**over):
    p = {"resolution": 1080, "batch_size": 17, "chunk_size": 0, "temporal_overlap": -1,
         "seed": None, "video_backend": "opencv", "use_10bit": False, "warmup": True}
    p.update(over)
    return p


def test_pod_probe_warms_then_times_only_the_second_pass(tmp_path, pod_engine):
    calls, plan = pod_engine
    plan["sleep"] = {0: 0.40, 1: 0.0}
    job = {"id": "abcdef123456", "input": str(tmp_path / "i.mkv"),
           "output": str(tmp_path / "o.mkv"), "total_frames": 37}

    podw._run_probe_job(job, _pod_params())

    assert job["outcome"] == "ok"
    assert len(calls) == 2, "one throwaway pass, then the timed one"
    assert job["seconds"] < 0.2, "reported the WARM pass, not the 0.40s cold one"


def test_pod_probe_without_warmup_runs_one_pass(tmp_path, pod_engine):
    """The switch must actually switch: a caller can still ask for the raw single pass."""
    calls, plan = pod_engine
    job = {"id": "abcdef123456", "input": str(tmp_path / "i.mkv"),
           "output": str(tmp_path / "o.mkv"), "total_frames": 37}

    podw._run_probe_job(job, _pod_params(warmup=False))

    assert len(calls) == 1


def test_pod_probe_warmup_oom_is_reported_as_the_ceiling(tmp_path, pod_engine):
    """An OOM while warming means the batch does not fit: the same answer, so it must land
    on the 'oom' outcome the sweep records as a ceiling, not an 'error'."""
    calls, plan = pod_engine
    plan["oom_on"] = 0
    job = {"id": "abcdef123456", "input": str(tmp_path / "i.mkv"),
           "output": str(tmp_path / "o.mkv"), "total_frames": 37}

    podw._run_probe_job(job, _pod_params())

    assert job["outcome"] == "oom"
    assert len(calls) == 1


def test_pod_probe_defaults_to_warming_when_the_key_is_absent(tmp_path, pod_engine):
    """An omitted key must not silently opt out of the fix. A probe is a request for a
    benchmark number and the un-warmed one is the wrong number, so BOTH the HTTP layer and
    _run_probe_job default it ON: relying on the HTTP default alone would leave any direct
    caller (and every future one) quietly measuring the cold number again."""
    calls, plan = pod_engine
    params = _pod_params()
    del params["warmup"]
    job = {"id": "abcdef123456", "input": str(tmp_path / "i.mkv"),
           "output": str(tmp_path / "o.mkv"), "total_frames": 37}

    podw._run_probe_job(job, params)

    assert len(calls) == 2
