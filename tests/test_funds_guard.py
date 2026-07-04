"""
funds_guard — the remote-run money safety net (recommendations item 3 / roadmap
#1 remainder). The decision logic is pure and fail-safe by contract: a disabled
(0) limit never trips, and an unknown (None) balance never blocks. These pin
that contract down, plus the session-cost math and a single poller iteration.
"""

import funds_guard as fg


# ── session_cost ────────────────────────────────────────────────────────────

def test_session_cost_scales_with_time_and_rate():
    assert fg.session_cost(3.60, 3600) == 3.60          # one hour at $3.60/h
    assert fg.session_cost(3.60, 1800) == 1.80          # half an hour


def test_session_cost_zero_when_rate_unknown():
    assert fg.session_cost(None, 3600) == 0.0
    assert fg.session_cost(0, 3600) == 0.0
    assert fg.session_cost(3.60, 0) == 0.0


# ── hours_until_depleted ────────────────────────────────────────────────────

def test_hours_until_depleted():
    assert fg.hours_until_depleted(10.0, 2.0) == 5.0


def test_hours_until_depleted_none_when_underivable():
    assert fg.hours_until_depleted(None, 2.0) is None
    assert fg.hours_until_depleted(10.0, 0) is None


# ── start_blocked (the pre-start refuse) ────────────────────────────────────

def test_start_blocked_when_estimate_breaches_floor():
    blocked, reason = fg.start_blocked(balance=20.0, estimated_cost=18.0, floor=5.0)
    assert blocked is True          # 20 - 18 = 2, below the 5 floor
    assert "floor" in reason


def test_start_allowed_when_estimate_leaves_enough():
    blocked, reason = fg.start_blocked(balance=50.0, estimated_cost=18.0, floor=5.0)
    assert blocked is False
    assert reason is None


def test_start_floor_disabled_never_blocks():
    assert fg.start_blocked(1.0, 999.0, floor=0)[0] is False
    assert fg.start_blocked(1.0, 999.0, floor=None)[0] is False


def test_start_unknown_balance_never_blocks():
    assert fg.start_blocked(None, 100.0, floor=5.0)[0] is False


def test_start_blocked_with_no_estimate_reduces_to_balance_below_floor():
    assert fg.start_blocked(3.0, None, floor=5.0)[0] is True     # already under floor
    assert fg.start_blocked(9.0, None, floor=5.0)[0] is False


# ── evaluate (the in-run auto-stop) ─────────────────────────────────────────

def test_evaluate_trips_on_cap():
    stop, reason = fg.evaluate(balance=100.0, floor=0, run_cost=12.0, cap=10.0)
    assert stop is True
    assert "cap" in reason


def test_evaluate_trips_on_balance_floor():
    stop, reason = fg.evaluate(balance=4.0, floor=5.0, run_cost=1.0, cap=0)
    assert stop is True
    assert "floor" in reason


def test_evaluate_cap_takes_precedence_when_both_trip():
    stop, reason = fg.evaluate(balance=4.0, floor=5.0, run_cost=99.0, cap=10.0)
    assert stop is True
    assert "cap" in reason           # cap is checked first


def test_evaluate_no_trip_when_within_limits():
    stop, reason = fg.evaluate(balance=100.0, floor=5.0, run_cost=1.0, cap=10.0)
    assert stop is False
    assert reason is None


def test_evaluate_disabled_limits_never_trip():
    assert fg.evaluate(0.01, 0, 10_000.0, 0)[0] is False


def test_evaluate_unknown_balance_skips_only_the_floor_half():
    # No balance: the floor half can't fire, but the cap half (time-derived) still can.
    assert fg.evaluate(None, 5.0, 1.0, 0)[0] is False
    assert fg.evaluate(None, 5.0, 12.0, 10.0)[0] is True


# ── FundsGuard poller ───────────────────────────────────────────────────────

def test_guard_is_inert_without_limits():
    g = fg.FundsGuard(fetch_balance=lambda: None, cost_per_hr=3.6)
    assert g.active is False
    g.start()                        # no-op; must not spawn a thread
    assert g._thread is None


def test_guard_check_once_uses_a_fake_clock_for_cost():
    t = {"now": 1000.0}
    g = fg.FundsGuard(fetch_balance=lambda: None, cost_per_hr=3.60, cap=1.0,
                      started_at=1000.0, clock=lambda: t["now"])
    assert g.active is True
    assert g.check_once()[0] is False           # 0s elapsed, no cost yet
    t["now"] = 1000.0 + 3600                     # one hour later -> $3.60 >= $1 cap
    stop, reason = g.check_once()
    assert stop is True
    assert "cap" in reason


def test_guard_check_once_floor_uses_passed_balance():
    g = fg.FundsGuard(fetch_balance=lambda: {"balance": 2.0}, cost_per_hr=0.0,
                      floor=5.0, clock=lambda: 0.0, started_at=0.0)
    assert g.check_once(balance=2.0)[0] is True
    assert g.check_once(balance=9.0)[0] is False
