"""
Self-healing remote runs (future-features #6, "Auto-resume"): a long video run survives
LOSING ITS POD mid-run without babysitting. A connectivity blip reconnects to the surviving
pod; a real pod loss waits (no time cap, $0 billed) for the IDENTICAL card to return and
redeploys it; either way the run continues from the first unfinished segment. The only
automatic stops are a completed queue, a user Stop, or the funds guard tripping.

These tests exercise the whole feature WITHOUT a pod or network by injecting the RunPod
seams (session factory, pod-alive, wait-for-stock, funds/stop predicates), matching how
funds_guard / video_estimate are unit-tested:

  - the pod-vs-source failure classifier (_is_pod_failure);
  - run_queue's auto_resume propagation (a pod death re-raises PodLost WITHOUT blaming the
    source's fail_count; a bad-source worker error still counts; unarmed = unchanged);
  - the unbounded wait-for-stock poll (backoff, stop-aware, exact-card match);
  - blip-vs-loss detection (_pod_still_running);
  - the redeploy target resolution (_healer_gpu_target);
  - the supervisor loop end to end (loss -> wait -> redeploy -> resume, blip -> reconnect,
    funds trip / user Stop -> stop without redeploy, redeploy-failure -> wait again).
"""

import os

import pytest

import db
import batch_video_upscale as bv
import remote_video_engine as rve


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


# ── classifier (pure) ────────────────────────────────────────────────────────

def test_pod_failure_classifier():
    # A transport/liveness failure IS a pod failure (heal it).
    assert bv._is_pod_failure(rve.RemoteVideoError("lost contact with the worker"))
    # A worker error on the segment is a BAD SOURCE, not a pod loss (never heal).
    assert not bv._is_pod_failure(rve.RemoteVideoWorkerError("worker error on segment"))
    # RemoteVideoStopped subclasses RemoteVideoError but is a user Stop, handled elsewhere;
    # it never reaches the classifier in practice. Anything unrelated is not a pod failure.
    assert not bv._is_pod_failure(ValueError("bad arg"))
    assert not bv._is_pod_failure(RuntimeError("boom"))


# ── run_queue auto_resume propagation ────────────────────────────────────────

def _enqueue(conn, tmp_path, rel="v.avi"):
    src_root = str(tmp_path / "src")
    out_root = str(tmp_path / "out")
    os.makedirs(src_root, exist_ok=True)
    root = db.get_video_root_id(conn, src_root, out_root)
    db.upsert_video_output(conn, root, rel, "1080p", status="queued", queue_order=0)
    return root, src_root


def _vcfg(tmp_path):
    vcfg = bv.resolve_video_cfg({})
    vcfg["work_root"] = str(tmp_path / "work")         # sibling of src: no conflict
    return vcfg


def test_armed_pod_failure_propagates_without_blaming_source(db_conn, tmp_path, monkeypatch):
    root, src_root = _enqueue(db_conn, tmp_path)
    monkeypatch.setattr(bv, "process_job", lambda *a, **k: (_ for _ in ()).throw(
        rve.RemoteVideoError("lost contact with the worker")))

    with pytest.raises(bv.PodLost) as ei:
        bv.run_queue(None, db_conn, root, src_root, _vcfg(tmp_path),
                     bv.RunBudget(0, 0.0), auto_resume=True)

    # The pod died: the source is NOT blamed (fail_count untouched, still queued), so a
    # redeployed pod resumes it instead of counting toward give-up.
    job = db.get_video_output(db_conn, root, "v.avi", "1080p")
    assert job["fail_count"] == 0
    assert len(db.get_video_queue(db_conn, root)) == 1
    assert ei.value.done == 0 and ei.value.failed == 0


def test_armed_worker_error_still_counts_as_source_failure(db_conn, tmp_path, monkeypatch):
    root, src_root = _enqueue(db_conn, tmp_path)
    monkeypatch.setattr(bv, "process_job", lambda *a, **k: (_ for _ in ()).throw(
        rve.RemoteVideoWorkerError("worker error on segment: bad codec")))

    # A bad source is NOT a pod loss: even armed, run_queue counts it (item 4 give-up),
    # never raises PodLost.
    summary = bv.run_queue(None, db_conn, root, src_root, _vcfg(tmp_path),
                           bv.RunBudget(0, 0.0), auto_resume=True)
    assert summary["failed"] == 1
    assert db.get_video_output(db_conn, root, "v.avi", "1080p")["fail_count"] == 1


def test_unarmed_pod_failure_is_unchanged(db_conn, tmp_path, monkeypatch):
    root, src_root = _enqueue(db_conn, tmp_path)
    monkeypatch.setattr(bv, "process_job", lambda *a, **k: (_ for _ in ()).throw(
        rve.RemoteVideoError("lost contact with the worker")))

    # With auto_resume OFF, behaviour is exactly as before: mark the job failed, keep going,
    # never raise PodLost.
    summary = bv.run_queue(None, db_conn, root, src_root, _vcfg(tmp_path),
                           bv.RunBudget(0, 0.0), auto_resume=False)
    assert summary["failed"] == 1
    assert db.get_video_output(db_conn, root, "v.avi", "1080p")["fail_count"] == 1


# ── wait-for-stock ───────────────────────────────────────────────────────────

def test_wait_for_stock_returns_when_card_returns():
    calls = {"n": 0}

    def list_gpus():
        calls["n"] += 1
        return [] if calls["n"] < 3 else [{"id": "RTX6000"}, {"id": "OTHER"}]

    ok = bv._wait_for_gpu_stock(list_gpus, "RTX6000", "RTX 6000",
                                on_event=lambda m: None, stop=lambda: False,
                                sleep=lambda s, st: None, first_interval=1, max_interval=4)
    assert ok is True and calls["n"] == 3


def test_wait_for_stock_stops_on_user_stop():
    ok = bv._wait_for_gpu_stock(lambda: [], "X", "X", on_event=lambda m: None,
                                stop=lambda: True, sleep=lambda s, st: None)
    assert ok is False


def test_wait_for_stock_survives_list_error_then_recovers():
    seq = [RuntimeError("api down"), [], [{"id": "A"}]]

    def list_gpus():
        v = seq.pop(0)
        if isinstance(v, Exception):
            raise v
        return v

    ok = bv._wait_for_gpu_stock(list_gpus, "A", "A", on_event=lambda m: None,
                                stop=lambda: False, sleep=lambda s, st: None,
                                first_interval=1, max_interval=2)
    assert ok is True and seq == []


def test_gpu_in_stock_exact_match():
    assert bv._gpu_in_stock([{"id": "A"}, {"id": "B"}], "B")
    assert not bv._gpu_in_stock([{"id": "A"}], "B")
    assert not bv._gpu_in_stock([], "B")
    assert not bv._gpu_in_stock(None, "B")


# ── blip vs loss ─────────────────────────────────────────────────────────────

def test_pod_still_running_true_only_when_running_under_prefix():
    running = [{"id": "p1", "desiredStatus": "RUNNING", "name": "video-toolbox-remote"}]
    assert bv._pod_still_running(lambda: running, "p1", "video-toolbox")
    # Exited pod, wrong id, and a control-plane error all read as gone (redeploy).
    exited = [{"id": "p1", "desiredStatus": "EXITED", "name": "video-toolbox-remote"}]
    assert not bv._pod_still_running(lambda: exited, "p1", "video-toolbox")
    assert not bv._pod_still_running(lambda: running, "other", "video-toolbox")
    assert not bv._pod_still_running(
        lambda: (_ for _ in ()).throw(RuntimeError("api")), "p1", "video-toolbox")
    assert not bv._pod_still_running(lambda: running, "", "video-toolbox")


# ── redeploy target ──────────────────────────────────────────────────────────

def test_healer_gpu_target_prefers_override(monkeypatch):
    monkeypatch.setenv("IMGTBX_GPU_OVERRIDE", "RTX6000ADA,FALLBACK")
    gid, label, region = bv._healer_gpu_target({"runpod": {}})
    assert gid == "RTX6000ADA" and label == "RTX6000ADA" and region is None


def test_healer_gpu_target_falls_back_to_configured(monkeypatch):
    monkeypatch.delenv("IMGTBX_GPU_OVERRIDE", raising=False)
    gid, _label, _region = bv._healer_gpu_target(
        {"runpod": {"gpu_type_id": "NVIDIA GeForce RTX 5090"}})
    assert gid == "NVIDIA GeForce RTX 5090"


# ── supervisor loop (fake sessions) ──────────────────────────────────────────

class _FakeSession:
    def __init__(self, i, funds_tripped=False):
        self.pod_id = "pod%d" % i
        self.pod_name_prefix = "video-toolbox"
        self._funds_tripped = funds_tripped
        self.closed_with = None

    def close(self, stop_pod=None):
        self.closed_with = stop_pod


def _supervise(run_plan, *, alive_map=None, funds_map=None, stopped=False,
               wait_ok=True, factory_fail_first_redeploy=False):
    """Drive _run_supervised with fake sessions. `run_plan` maps a pass index to an
    outcome: PodLost(reason, done, failed) or a summary dict returned cleanly."""
    made = []                                          # successful sessions, in order
    attempts = {"n": 0}
    events = []
    waits = {"n": 0}

    def factory(first):
        attempts["n"] += 1
        # Fail the FIRST redeploy attempt (a capacity race after stock appeared) so the
        # supervisor must wait for stock again; a placeholder never enters `made`, so the
        # run_plan index stays the successful-session ordinal.
        if factory_fail_first_redeploy and attempts["n"] == 2:
            raise RuntimeError("deploy race: sold out")
        i = len(made)
        s = _FakeSession(i, funds_tripped=(funds_map or {}).get(i, False))
        made.append(s)
        return s, ("engine", i)

    def run_pass(engine):
        _, i = engine
        outcome = run_plan[i]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def wait_for_stock():
        waits["n"] += 1
        return wait_ok

    summary = bv._run_supervised(
        factory, run_pass,
        pod_alive=lambda s: (alive_map or {}).get(int(s.pod_id[3:]), False),
        wait_for_stock=wait_for_stock,
        is_stopped=lambda: stopped,
        funds_tripped=lambda s: s._funds_tripped,
        close_session=lambda s, sp: s.close(sp),
        on_event=events.append, notify_settings=None, source_root="X")
    return summary, made, waits, events


def test_supervisor_heals_loss_then_blip_then_completes():
    plan = {
        0: bv.PodLost("host reclaimed", done=1, failed=0),   # loss -> wait -> redeploy
        1: bv.PodLost("tunnel dropped", done=1, failed=0),   # blip -> reconnect (same pod)
        2: {"done": 1, "failed": 0, "stopped": None, "total": 1},
    }
    summary, sessions, waits, _ev = _supervise(plan, alive_map={0: False, 1: True})
    assert summary == {"done": 3, "failed": 0, "stopped": None, "total": 3, "files": [],
                       "attempted": set()}
    assert waits["n"] == 1                             # only the loss waited for stock
    real = [s for s in sessions if s is not None]
    assert real[0].closed_with is True                # loss: terminate the remnant
    assert real[1].closed_with is False               # blip: KEEP the pod (reconnect)
    assert real[2].closed_with is None                # final: default teardown


def test_supervisor_funds_trip_stops_without_redeploy():
    plan = {0: bv.PodLost("lost contact", done=0, failed=0)}
    summary, sessions, waits, _ev = _supervise(plan, funds_map={0: True})
    assert summary["stopped"] == "funds safety-net"
    assert waits["n"] == 0                             # never waited, never redeployed
    assert len([s for s in sessions if s]) == 1


def test_supervisor_user_stop_stops_without_redeploy():
    plan = {0: bv.PodLost("lost contact", done=0, failed=0)}
    summary, sessions, waits, _ev = _supervise(plan, stopped=True)
    assert summary["stopped"] == "stopped by user"
    assert waits["n"] == 0
    assert len([s for s in sessions if s]) == 1


def test_supervisor_stops_when_wait_is_interrupted():
    plan = {0: bv.PodLost("host reclaimed", done=2, failed=1)}
    summary, _sessions, waits, _ev = _supervise(plan, alive_map={0: False}, wait_ok=False)
    # Wait-for-stock returned False (user Stop during the wait): end, keeping the counts
    # accrued before the loss.
    assert summary == {"done": 2, "failed": 1, "stopped": "stopped by user",
                       "total": 3, "files": [], "attempted": set()}
    assert waits["n"] == 1


def test_supervisor_redeploy_failure_waits_again():
    plan = {
        0: bv.PodLost("host reclaimed", done=0, failed=0),   # loss -> wait -> redeploy (fails)
        1: {"done": 1, "failed": 0, "stopped": None, "total": 1},  # after 2nd wait -> ok
    }
    summary, sessions, waits, _ev = _supervise(
        plan, alive_map={0: False}, factory_fail_first_redeploy=True)
    assert summary == {"done": 1, "failed": 0, "stopped": None, "total": 1, "files": [],
                       "attempted": set()}
    assert waits["n"] == 2                             # loss wait + post-redeploy-failure wait


def test_supervisor_first_start_failure_propagates():
    # A failure on the VERY FIRST deploy is a clean fail-to-start (main() reports it), not
    # something to heal -> it propagates out of the supervisor.
    def factory(first):
        raise RuntimeError("no api key")

    with pytest.raises(RuntimeError, match="no api key"):
        bv._run_supervised(
            factory, lambda e: {"done": 0, "failed": 0, "stopped": None, "total": 0},
            pod_alive=lambda s: False, wait_for_stock=lambda: True,
            is_stopped=lambda: False, funds_tripped=lambda s: False,
            close_session=lambda s, sp: None, on_event=lambda m: None,
            notify_settings=None, source_root="X")
