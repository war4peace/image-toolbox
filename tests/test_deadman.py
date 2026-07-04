"""
pod/deadman.evaluate — the pure stop-decision at the heart of the never-leave-a-
billed-pod-running promise. It already shipped with a `--selftest`; this gives
that logic a real home in CI (item 2).
"""

from pod import deadman


def test_both_limits_disabled_never_stops():
    stop, reason = deadman.evaluate(1000, 0, 1000, 0, 0)
    assert stop is False
    assert reason is None


def test_max_runtime_trips():
    stop, reason = deadman.evaluate(1000, 0, 1000, max_runtime_s=600, idle_s=0)
    assert stop is True
    assert "max runtime" in reason


def test_under_max_runtime_does_not_trip():
    stop, _ = deadman.evaluate(500, 0, 500, max_runtime_s=600, idle_s=0)
    assert stop is False


def test_idle_timeout_trips():
    # last activity at t=100, now=1000 -> 900s idle > 300s limit.
    stop, reason = deadman.evaluate(1000, 0, 100, max_runtime_s=0, idle_s=300)
    assert stop is True
    assert "idle" in reason


def test_under_idle_limit_does_not_trip():
    stop, _ = deadman.evaluate(1000, 0, 900, max_runtime_s=0, idle_s=300)
    assert stop is False


def test_max_runtime_takes_precedence_when_both_would_trip():
    # Both limits exceeded; max-runtime is checked first, so its reason wins.
    stop, reason = deadman.evaluate(1000, 0, 900, max_runtime_s=600, idle_s=300)
    assert stop is True
    assert "max runtime" in reason


def test_negative_limit_is_treated_as_disabled():
    stop, reason = deadman.evaluate(10_000, 0, 0, max_runtime_s=-1, idle_s=-1)
    assert stop is False
    assert reason is None


def test_selftest_passes():
    # The module's own bundled cases must still agree with evaluate().
    assert deadman._selftest() == 0
