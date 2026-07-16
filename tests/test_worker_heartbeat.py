"""
The pod's liveness signal (the 4K dead-man's-switch bug).

A /video/status poll MUST touch the heartbeat. The worker used to touch it only per line
of SeedVR2 pipeline output, on the theory that a working segment stays alive while a hung
GPU goes stale. At 4K that theory is false: the tiled decode is one atomic, silent step of
many minutes, so a B200 probe computing flat out went >15 min without printing, the
heartbeat went stale, and the switch TERMINATED the pod mid-benchmark.

The switch's real job is reclaiming a pod whose CLIENT died, and a client poll measures
exactly that. worker.py's module imports are stdlib-only (SeedVR2 loads lazily in main),
so this tests off the pod.
"""
import os
import json
import time

from pod import deadman as d
from pod import worker as w

IDLE_S = 15 * 60          # the shipped runpod.idle_timeout_minutes default


class _Handler(w.Handler):
    """A Handler with no socket: the status path only needs _send captured."""

    def __init__(self):                       # noqa: D107 (deliberately skips BaseHTTPRequestHandler)
        self.sent = []

    def _send(self, code, body, ctype, extra=None):
        self.sent.append((code, body, ctype))


def _job(**over):
    j = {"id": "abc", "state": "running", "total_frames": 37,
         "started": time.time(), "last_output_t": time.time(),
         "output": os.path.join("nonexistent", "out.mp4")}
    j.update(over)
    return j


def _status(monkeypatch, hb_path, job):
    monkeypatch.setattr(w, "_HEARTBEAT", str(hb_path))
    monkeypatch.setattr(w, "_VIDEO_JOB", job)
    h = _Handler()
    h._handle_video_status({"id": [job["id"]] if job else ["abc"]})
    return h


def _stale(hb_path, age_s):
    hb_path.write_text("x")
    t = time.time() - age_s
    os.utime(str(hb_path), (t, t))
    return t


# ── the poll is the heartbeat ────────────────────────────────────────────────

def test_video_status_poll_touches_the_heartbeat(tmp_path, monkeypatch):
    hb = tmp_path / "hb"
    _status(monkeypatch, hb, _job())
    assert hb.exists()


def test_a_silent_running_job_is_kept_alive_by_the_poll(tmp_path, monkeypatch):
    """THE regression. The pipeline has emitted nothing for an hour (a 4K tiled decode);
    the client is still polling, so the pod must NOT go stale."""
    hb = tmp_path / "hb"
    old = _stale(hb, 3600)
    _status(monkeypatch, hb, _job(last_output_t=old))
    assert os.path.getmtime(str(hb)) > old + 60, \
        "a silent-but-working probe must still refresh the heartbeat"


def test_poll_for_an_unknown_job_still_counts_as_client_alive(tmp_path, monkeypatch):
    """The touch precedes the job lookup on purpose: ANY poll means the app is there.
    A client polling a job the worker forgot is told 'unknown' and gives up on its own,
    so this cannot keep a pod alive by itself."""
    hb = tmp_path / "hb"
    old = _stale(hb, 3600)
    monkeypatch.setattr(w, "_HEARTBEAT", str(hb))
    monkeypatch.setattr(w, "_VIDEO_JOB", None)
    h = _Handler()
    h._handle_video_status({"id": ["gone"]})
    assert h.sent[0][0] == 404
    assert os.path.getmtime(str(hb)) > old + 60


# ── what the switch then decides ─────────────────────────────────────────────

def test_deadman_keeps_a_polled_pod_and_reclaims_an_abandoned_one():
    """The two cases the one signal must separate, at the shipped 15-min idle timeout."""
    now = 10_000.0
    started = now - 7200
    # Client polled 5s ago; pipeline silent for 2h (a long 4K decode). KEEP.
    assert d.evaluate(now, started, now - 5, 0, IDLE_S)[0] is False
    # Client gone 16 min (crashed / laptop closed). RECLAIM.
    stop, why = d.evaluate(now, started, now - 16 * 60, 0, IDLE_S)
    assert stop is True and "idle" in why


def test_the_old_signal_would_have_killed_the_working_pod():
    """Documents the bug: under the pipeline-output signal, the B200 4K probe's ~19 min of
    silence read as idle and the pod was terminated while computing."""
    now = 10_000.0
    last_pipeline_output = now - 19 * 60
    assert d.evaluate(now, now - 7200, last_pipeline_output, 0, IDLE_S)[0] is True


# ── stalled_for is reported, not enforced ────────────────────────────────────

def test_status_reports_stalled_for(tmp_path, monkeypatch):
    """The silent stretch is now visible (is a mute 4K probe in the compile or a decode?)
    instead of being a guess, but it no longer gates the pod."""
    hb = tmp_path / "hb"
    h = _status(monkeypatch, hb, _job(last_output_t=time.time() - 300))
    code, body, _ = h.sent[0]
    assert code == 200
    assert 295 <= json.loads(body)["stalled_for"] <= 310


def test_stalled_for_falls_back_to_the_job_start(tmp_path, monkeypatch):
    """A job that has not printed at all yet must report its age, not crash the poll."""
    hb = tmp_path / "hb"
    job = _job(started=time.time() - 120)
    job.pop("last_output_t")
    h = _status(monkeypatch, hb, job)
    assert 115 <= json.loads(h.sent[0][1])["stalled_for"] <= 130


# ── pod-side compile gate ────────────────────────────────────────────────────
# Inductor shells out to a C compiler; with compile requested and none present the first VAE
# compile hangs with NO output. batch_video_upscale.gate_local_compile has guarded the LOCAL
# path for a while, but _worker_cfg / _worker_settings ship compile_dit + compile_vae straight
# from config and nothing ever asked the POD whether it could. On a rented pod that is a
# silent, billing, undiagnosable stall.

def test_pod_gate_disables_compile_when_no_compiler(monkeypatch):
    monkeypatch.setattr(w.shutil, "which", lambda n: None)
    s = {"compile_dit": True, "compile_vae": True}
    on, why = w._gate_compile(s)
    assert on is False and "compiler" in why
    assert s["compile_dit"] is False and s["compile_vae"] is False


def test_pod_gate_keeps_compile_when_the_toolchain_is_there(monkeypatch):
    monkeypatch.setattr(w.shutil, "which", lambda n: "/usr/bin/cc" if n == "cc" else None)
    s = {"compile_dit": True, "compile_vae": True}
    on, why = w._gate_compile(s)
    assert on is True and why == "/usr/bin/cc"
    assert s["compile_dit"] is True


def test_pod_gate_is_a_noop_when_compile_was_not_asked_for(monkeypatch):
    s = {"compile_dit": False, "compile_vae": False}
    assert w._gate_compile(s) == (False, "not requested")
    assert s == {"compile_dit": False, "compile_vae": False}


def test_pod_gate_never_raises(monkeypatch):
    """Fail-safe: a broken probe must leave the run alone, not kill the worker at startup."""
    def _boom(_n):
        raise OSError("PATH exploded")
    monkeypatch.setattr(w.shutil, "which", _boom)
    s = {"compile_dit": True, "compile_vae": True}
    on, why = w._gate_compile(s)
    assert on is True and why == "gate check failed"
    assert s["compile_dit"] is True, "an unknown answer must not silently change behaviour"
