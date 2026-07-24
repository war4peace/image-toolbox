"""
Offline tests for the grouped multi-pod orchestrator (docs/video-upscaler.md section 18):
`batch_video_upscale.run_grouped` runs one pod per (engine, gpu) group, sequentially, choosing
the next group by live stock (the pendulum) and never substituting a card. Every RunPod-touching
seam is injected, so the loop is exercised with no pod, mirroring test_video_autoresume.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "scripts"))
import batch_video_upscale as bv


def _summary(done=1, failed=0, stopped=None):
    return {"done": done, "failed": failed, "stopped": stopped, "files": []}


def _harness(groups, *, stock=None, run_returns=None, stop_seq=None, wait_returns="first"):
    """Build the injected seams and record the order groups actually ran in.

    stock: dict key -> bool (default: everything in stock).
    run_returns: dict key -> summary (default: 1 done each).
    stop_seq: list of is_stopped() return values, consumed in order (default: always False).
    wait_returns: 'first' returns the first still-pending key; or a fixed key; or None.
    """
    ran = []
    stock = stock or {}
    run_returns = run_returns or {}
    calls = {"wait": 0}
    stops = list(stop_seq or [])

    def gpu_in_stock(key):
        return stock.get(key, True)

    def run_group(key):
        ran.append(key)
        return run_returns.get(key, _summary())

    def wait_for_stock(pending):
        calls["wait"] += 1
        if wait_returns is None:
            return None
        if wait_returns == "first":
            return pending[0]
        return wait_returns

    def is_stopped():
        return stops.pop(0) if stops else False

    events = []
    result = bv.run_grouped(groups, run_group, gpu_in_stock=gpu_in_stock,
                            wait_for_stock=wait_for_stock, is_stopped=is_stopped,
                            on_event=events.append)
    return result, ran, calls


A = ("seedvr2", "PRO6000")
B = ("fixed_ratio", "RTX2000")
C = ("seedvr2", "PRO4000")


def test_all_in_stock_run_in_order():
    result, ran, _ = _harness([B, A, C])
    assert ran == [B, A, C]
    assert result["done"] == 3 and result["failed"] == 0
    assert result["stopped"] is None


def test_soldout_group_is_deferred_then_waited_for():
    # A is sold out; B and C run first (in stock), then only A remains -> pendulum waits.
    result, ran, calls = _harness([A, B, C], stock={A: False, B: True, C: True})
    assert ran == [B, C, A]           # A deferred to last, run after wait_for_stock
    assert calls["wait"] == 1
    assert result["done"] == 3


def test_all_soldout_waits_then_runs():
    result, ran, calls = _harness([A, B], stock={A: False, B: False})
    assert calls["wait"] >= 1
    assert set(ran) == {A, B}         # both eventually run after waits
    assert result["done"] == 2


def test_stop_during_wait_ends_run():
    # All sold out and the wait returns None (user stopped while waiting).
    result, ran, _ = _harness([A, B], stock={A: False, B: False}, wait_returns=None)
    assert ran == []
    assert result["stopped"] == "stopped by user"


def test_stop_inside_group_ends_whole_run():
    # The first group reports a user Stop: remaining groups are left for a later Start.
    result, ran, _ = _harness([A, B, C],
                              run_returns={A: _summary(done=2, stopped="stopped by user")})
    assert ran == [A]                 # B and C never ran
    assert result["done"] == 2
    assert result["stopped"] == "stopped by user"


def test_funds_trip_inside_group_ends_whole_run():
    result, ran, _ = _harness([A, B],
                              run_returns={A: _summary(done=1, stopped="funds safety-net")})
    assert ran == [A]
    assert result["stopped"] == "funds safety-net"


def test_already_stopped_runs_nothing():
    result, ran, _ = _harness([A, B], stop_seq=[True])
    assert ran == []
    assert result["done"] == 0 and result["stopped"] is None


def test_failures_accumulate():
    result, _, _ = _harness([A, B], run_returns={A: _summary(done=0, failed=1),
                                                 B: _summary(done=2, failed=1)})
    assert result["done"] == 2 and result["failed"] == 2 and result["total"] == 4


# ── distinct_group_keys ────────────────────────────────────────────────────────

def _row(engine=None, gpu=None):
    return {"rel_path": "x", "engine": engine, "gpu": gpu, "target": "4K", "clip_id": 0}


def test_distinct_group_keys_first_appearance():
    jobs = [_row("seedvr2", "PRO6000"), _row("fixed_ratio", "RTX2000"),
            _row("seedvr2", "PRO6000")]
    assert bv.distinct_group_keys(jobs) == [("seedvr2", "PRO6000"), ("fixed_ratio", "RTX2000")]


def test_distinct_group_keys_legacy_single():
    # A legacy all-NULL queue is ONE group, so the remote run stays single-pod.
    jobs = [_row(), _row(), _row()]
    assert bv.distinct_group_keys(jobs) == [("seedvr2", "")]


# ── _wait_for_any_gpu_stock (the pendulum wait) ────────────────────────────────

def _stock_waiter(stock_sequence, *, stop_after=None):
    """Drive _wait_for_any_gpu_stock with a scripted sequence of available_gpus() results
    (each a list of {'id':...}); `stop_after` stops the loop after N polls."""
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
    # Poll 1: nothing; poll 2: RTX2000 in stock -> return that group.
    seq = [[], [{"id": "RTX2000"}]]
    key, polls = _stock_waiter(seq)
    assert key == ("fixed_ratio", "RTX2000")
    assert polls >= 1


def test_wait_returns_immediately_if_in_stock():
    key, polls = _stock_waiter([[{"id": "PRO6000"}]])
    assert key == ("seedvr2", "PRO6000")
    assert polls == 0            # no sleep needed


def test_wait_returns_none_on_stop():
    key, _ = _stock_waiter([[]], stop_after=2)   # never in stock; stops after 2 polls
    assert key is None
