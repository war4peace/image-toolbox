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
