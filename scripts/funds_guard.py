"""
funds_guard.py
--------------
Money safety-net for remote (RunPod) runs — the last open piece of roadmap #1
(docs/future-features.md). The on-pod dead-man's switch (deadman.py) guards
against a *forgotten* pod (time/idle); this guards against a *working* pod
draining the account, and against a run started with less balance than it needs.

Two independent protections, both OFF by default (a 0/None limit disables that
check), both fail-safe (balance unreadable → the check is skipped, never blocks):

  * start floor  — refuse to START a run when finishing the estimate would drop
                   the account balance below a configured floor.
  * session cap  — while a run is billing, AUTO-STOP the pod once this run's
                   accumulated cost crosses a configured cap, OR the live balance
                   falls below the floor.

Account balance is not in the REST API; it comes from the legacy GraphQL
`myself { clientBalance currentSpendPerHr }` query (see
runpod_client.account_balance). This module keeps only the DECISION logic (pure,
unit-tested) plus a small background poller; the network fetch is injected so it
stays testable off-line and never imports the control plane itself.

Design: docs/future-features.md #1 ("Funds-floor safety-net + auto-stop").
Stdlib only, fail-safe, isolated.
"""

import time
import threading


# ─────────────────────────────────────────────
#  Pure decisions (unit-tested; no I/O)
# ─────────────────────────────────────────────

def session_cost(cost_per_hr, elapsed_seconds):
    """This run's accumulated cost so far: the pod's real billed $/h times the
    hours it has been running. 0 when the rate is unknown."""
    if not cost_per_hr or cost_per_hr <= 0 or not elapsed_seconds or elapsed_seconds <= 0:
        return 0.0
    return cost_per_hr * (elapsed_seconds / 3600.0)


def hours_until_depleted(balance, spend_per_hr):
    """Hours of runway left at the current spend rate, or None when it can't be
    derived (no balance, or nothing is spending)."""
    if balance is None or not spend_per_hr or spend_per_hr <= 0:
        return None
    return max(0.0, balance) / spend_per_hr


def start_blocked(balance, estimated_cost, floor):
    """Should a run be REFUSED before it starts? True when finishing the estimate
    would leave the balance below `floor`. Pure, fail-open:
      * floor <= 0            → disabled, never blocks.
      * balance is None       → unknown, never blocks (checks are best-effort).
    `estimated_cost` may be 0/None when the caller has no estimate; then the test
    reduces to "is the balance already below the floor".
    Returns (blocked, reason)."""
    if not floor or floor <= 0:
        return False, None
    if balance is None:
        return False, None
    projected = balance - (estimated_cost or 0.0)
    if projected < floor:
        est = estimated_cost or 0.0
        return True, (
            f"balance ${balance:.2f} minus the ${est:.2f} estimate would leave "
            f"${projected:.2f}, below your ${floor:.2f} floor")
    return False, None


def evaluate(balance, floor, run_cost, cap):
    """The in-run auto-stop decision. Pure. Returns (should_stop, reason).
      * cap  > 0 and run_cost >= cap        → stop (this run cost too much).
      * floor > 0 and balance <= floor      → stop (balance hit the floor).
    A 0/None limit disables that half; an unknown (None) balance skips only the
    floor half. The cap half needs no network (it is derived from elapsed time)."""
    if cap and cap > 0 and run_cost is not None and run_cost >= cap:
        return True, (f"this run's cost ${run_cost:.2f} reached your "
                      f"${cap:.2f} cap")
    if floor and floor > 0 and balance is not None and balance <= floor:
        return True, (f"account balance ${balance:.2f} reached your "
                      f"${floor:.2f} floor")
    return False, None


# ─────────────────────────────────────────────
#  Background poller (wraps evaluate on a cadence)
# ─────────────────────────────────────────────

class FundsGuard:
    """Polls the balance while a remote run bills and trips `on_trip(reason)` once
    the session cap or the balance floor is crossed. Edge-triggered: it fires at
    most once, then stops polling (the caller tears the pod down). Fail-safe — any
    error in a poll is swallowed so the guard can never crash or stop a run
    spuriously.

    `fetch_balance()` returns {"balance", "spend_per_hr"} or None (injected so this
    is testable and free of a control-plane import). `cost_per_hr` is the pod's
    real billed rate; `started_at` defaults to now. A guard with neither a cap nor
    a floor set is inert (start() is a no-op), so wiring it in costs nothing when
    the user hasn't opted in."""

    def __init__(self, fetch_balance, cost_per_hr, floor=0.0, cap=0.0,
                 poll_seconds=60, on_trip=None, on_warn=None,
                 started_at=None, clock=time.time):
        self.fetch_balance = fetch_balance
        self.cost_per_hr = cost_per_hr or 0.0
        self.floor = floor or 0.0
        self.cap = cap or 0.0
        self.poll_seconds = max(15, int(poll_seconds or 60))
        self.on_trip = on_trip or (lambda reason: None)
        self.on_warn = on_warn or (lambda msg: None)
        self.started_at = started_at if started_at is not None else clock()
        self._clock = clock
        self._stop = threading.Event()
        self._thread = None
        self._tripped = False

    @property
    def active(self):
        """A guard only does anything when at least one limit is set."""
        return bool((self.cap and self.cap > 0) or (self.floor and self.floor > 0))

    def check_once(self, balance=None):
        """Run the decision a single time and return (should_stop, reason). Used by
        the poll loop and directly by tests. `balance` may be passed in; otherwise
        the cap (network-free) half still evaluates and the floor half is skipped."""
        run_cost = session_cost(self.cost_per_hr, self._clock() - self.started_at)
        return evaluate(balance, self.floor, run_cost, self.cap)

    def start(self):
        """Begin polling on a background daemon thread (no-op if inert)."""
        if not self.active or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="FundsGuard",
                                        daemon=True)
        self._thread.start()

    def stop(self):
        """Stop polling; safe to call more than once."""
        self._stop.set()

    def _run(self):
        while not self._stop.is_set():
            # Sleep first: at t=0 no cost has accrued and the balance hasn't moved.
            if self._stop.wait(self.poll_seconds):
                break
            try:
                info = self.fetch_balance() or {}
                balance = info.get("balance")
                should, reason = self.check_once(balance)
                if should and not self._tripped:
                    self._tripped = True
                    self.on_trip(reason)
                    return
            except Exception:                    # noqa: BLE001 — never crash a run
                # A failed balance read is expected sometimes (transient network);
                # the cap half still works next tick since it needs no balance.
                pass
