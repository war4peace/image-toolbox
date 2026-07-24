"""
Offline tests for the grouped multi-pod orchestrator (docs/video-upscaler.md section 18):
`batch_video_upscale.run_grouped` runs one pod per (engine, gpu) group, sequentially, choosing
the next group by live stock (the pendulum), never substituting a card, and RE-DERIVING the
groups from the live queue each pass so a mid-run add re-opens a finished group. Every
RunPod-touching seam is injected, so the loop is exercised with no pod.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "scripts"))
import batch_video_upscale as bv

A = ("seedvr2", "PRO6000")
B = ("fixed_ratio", "RTX2000")
C = ("seedvr2", "PRO4000")
D = ("seedvr2", "A100")


def _job(rel, key, outcome="done"):
    """A queue row in group `key`. outcome 'done' leaves the queue when run; 'fail' lingers
    (as a real failed job does until GIVE_UP_AFTER); 'stop' makes its group report a Stop."""
    return {"rel_path": rel, "target": "4K", "clip_id": 0,
            "engine": key[0], "gpu": key[1], "_outcome": outcome}


def _run(queue, *, stock=None, stop_seq=None, wait_returns="first", inject=None):
    """Drive run_grouped over a MUTABLE queue that behaves like the real one: a 'done' job
    leaves the queue, a 'fail' job stays (but is now attempted). `inject` = {after_Nth_group:
    [jobs]} simulates the user adding files mid-run. Returns (result, ran, calls, leftover_q)."""
    stock = stock or {}
    stops = list(stop_seq or [])
    inject = inject or {}
    q = [dict(j) for j in queue]
    ran = []
    calls = {"wait": 0}

    def live_jobs():
        return [dict(j) for j in q]

    def gpu_in_stock(key):
        return stock.get(key, True)

    def run_group(key, skip):
        ran.append(key)
        done = failed = 0
        attempted = set()
        stopped = None
        for job in list(q):
            jid = (job["rel_path"], job["target"], job["clip_id"])
            if bv.job_group_key(job) != key or jid in skip:
                continue
            attempted.add(jid)
            if job["_outcome"] == "stop":
                stopped = "stopped by user"
                failed += 1
            elif job["_outcome"] == "done":
                q.remove(job)
                done += 1
            else:                                  # 'fail' -> stays queued, now attempted
                failed += 1
        if len(ran) in inject:                     # user adds files after this group's pod
            q.extend(dict(j) for j in inject[len(ran)])
        return {"done": done, "failed": failed, "stopped": stopped,
                "files": [], "attempted": attempted}

    def wait_for_stock(pending):
        calls["wait"] += 1
        if wait_returns is None:
            return None
        return pending[0] if wait_returns == "first" else wait_returns

    def is_stopped():
        return stops.pop(0) if stops else False

    result = bv.run_grouped(live_jobs, run_group, gpu_in_stock=gpu_in_stock,
                            wait_for_stock=wait_for_stock, is_stopped=is_stopped,
                            on_event=lambda _m: None)
    return result, ran, calls, q


def test_all_in_stock_run_in_order():
    result, ran, _, left = _run([_job("a", A), _job("b", B), _job("c", C)])
    assert ran == [A, B, C]
    assert result["done"] == 3 and result["failed"] == 0 and not left


def test_failed_job_does_not_reopen_its_group():
    # The one that would loop without session-attended tracking: a failing job lingers in the
    # queue, but its group must run exactly ONCE (no repeated pod deploys).
    result, ran, _, left = _run([_job("a", A, "fail")])
    assert ran == [A]                     # A ran once, not 3x (GIVE_UP_AFTER) or forever
    assert result["failed"] == 1
    assert len(left) == 1                 # the failed job is still queued (for a later Start)


def test_midrun_add_reopens_finished_group():
    # A finishes; the user then adds another A-job (+ a B-job). A must RE-OPEN with a new pod.
    result, ran, _, left = _run(
        [_job("a1", A)],
        inject={1: [_job("b1", B), _job("a2", A)]})
    assert ran == [A, B, A]               # A ran, then B, then A re-opened for a2
    assert result["done"] == 3 and not left


def test_midrun_add_brand_new_gpu_group():
    # A brand-new GPU group (never in the initial queue) added mid-run is picked up.
    result, ran, _, _ = _run([_job("a", A)], inject={1: [_job("d", D)]})
    assert ran == [A, D]


def test_soldout_group_deferred_then_waited_for():
    result, ran, calls, _ = _run([_job("a", A), _job("b", B), _job("c", C)],
                                 stock={A: False, B: True, C: True})
    assert ran == [B, C, A]               # A deferred, run last after wait
    assert calls["wait"] == 1
    assert result["done"] == 3


def test_all_soldout_waits_then_runs():
    result, ran, calls, _ = _run([_job("a", A), _job("b", B)], stock={A: False, B: False})
    assert calls["wait"] >= 1
    assert set(ran) == {A, B}
    assert result["done"] == 2


def test_stop_during_wait_ends_run():
    result, ran, _, _ = _run([_job("a", A), _job("b", B)],
                             stock={A: False, B: False}, wait_returns=None)
    assert ran == []
    assert result["stopped"] == "stopped by user"


def test_stop_inside_group_ends_whole_run():
    result, ran, _, _ = _run([_job("a", A, "stop"), _job("b", B), _job("c", C)])
    assert ran == [A]                     # B and C never ran
    assert result["stopped"] == "stopped by user"


def test_already_stopped_runs_nothing():
    result, ran, _, _ = _run([_job("a", A), _job("b", B)], stop_seq=[True])
    assert ran == []
    assert result["done"] == 0 and result["stopped"] is None


def test_empty_queue_runs_nothing():
    result, ran, _, _ = _run([])
    assert ran == [] and result["done"] == 0


# ── distinct_group_keys ────────────────────────────────────────────────────────

def _row(engine=None, gpu=None):
    return {"rel_path": "x", "engine": engine, "gpu": gpu, "target": "4K", "clip_id": 0}


def test_distinct_group_keys_first_appearance():
    jobs = [_row("seedvr2", "PRO6000"), _row("fixed_ratio", "RTX2000"),
            _row("seedvr2", "PRO6000")]
    assert bv.distinct_group_keys(jobs) == [("seedvr2", "PRO6000"), ("fixed_ratio", "RTX2000")]


def test_distinct_group_keys_legacy_single():
    jobs = [_row(), _row(), _row()]
    assert bv.distinct_group_keys(jobs) == [("seedvr2", "")]


# ── _wait_for_any_gpu_stock (the pendulum wait) ────────────────────────────────

def _stock_waiter(stock_sequence, *, stop_after=None):
    calls = {"n": 0}

    def list_gpus():
        i = min(calls["n"], len(stock_sequence) - 1)
        return stock_sequence[i]

    def sleep(_interval, _stop):
        calls["n"] += 1

    def stop():
        return stop_after is not None and calls["n"] >= stop_after

    key = bv._wait_for_any_gpu_stock(
        list_gpus, [("seedvr2", "PRO6000"), ("fixed_ratio", "RTX2000")],
        on_event=lambda _m: None, stop=stop, sleep=sleep)
    return key, calls["n"]


def test_wait_returns_key_when_card_appears():
    seq = [[], [{"id": "RTX2000"}]]
    key, polls = _stock_waiter(seq)
    assert key == ("fixed_ratio", "RTX2000")
    assert polls >= 1


def test_wait_returns_immediately_if_in_stock():
    key, polls = _stock_waiter([[{"id": "PRO6000"}]])
    assert key == ("seedvr2", "PRO6000")
    assert polls == 0


def test_wait_returns_none_on_stop():
    key, _ = _stock_waiter([[]], stop_after=2)
    assert key is None
