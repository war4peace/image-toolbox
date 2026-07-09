"""
Item 9: notification coverage is now even across the runners.

Two gaps existed: conciliate sent NO notifications (a long archive/delete finished
or errored silently), and the video runner's pod-STARTUP failure was silent (only
the in-queue failure path notified). Both are closed. These tests pin the pure
outcome->(title,color) selection helpers and the conciliate send wrapper's
delegation; they are stdlib-only (no torch/PIL/tkinter), so they run everywhere.
"""

import conciliate
import batch_video_upscale as bv


# ── conciliate: completion notice colour/title by outcome ────────────────────

def test_conciliate_notice_clean_is_green():
    title, color = conciliate._completion_notice(done=10, conflicts=0, errors=0,
                                                  stopped=False)
    assert color == 3066993                     # green
    assert "Finished" in title and "Issues" not in title


def test_conciliate_notice_conflicts_is_yellow():
    title, color = conciliate._completion_notice(5, conflicts=2, errors=0,
                                                  stopped=False)
    assert color == 16776960                    # yellow
    assert "Issues" in title


def test_conciliate_notice_errors_is_yellow():
    title, color = conciliate._completion_notice(5, conflicts=0, errors=1,
                                                  stopped=False)
    assert color == 16776960
    assert "Issues" in title


def test_conciliate_notice_stopped_wins_over_clean():
    # A user stop is reported as such even when nothing errored.
    title, color = conciliate._completion_notice(3, conflicts=0, errors=0,
                                                  stopped=True)
    assert color == 16776960
    assert "Stopped by User" in title


# ── conciliate: the send wrapper delegates to notifications.notify ───────────

def test_conciliate_send_notification_delegates(monkeypatch):
    seen = {}
    monkeypatch.setattr(conciliate.notifications, "notify",
                        lambda settings, title, desc, color, fields=None, username="x":
                        seen.update(title=title, color=color, username=username,
                                    settings=settings))
    conciliate.send_notification("T", "D", 3066993, fields=[{"name": "a", "value": "b"}])
    assert seen["title"] == "T"
    assert seen["color"] == 3066993
    assert seen["username"] == "Conciliate Bot"
    assert seen["settings"] is conciliate.NOTIFY     # uses the module-level settings


def test_conciliate_send_notification_is_failsafe_when_unconfigured(monkeypatch):
    # With NOTHING configured, a real send must be a harmless no-op (never raise
    # into a run). Force an empty settings dict so this exercises the fail-safe
    # path WITHOUT contacting a backend even on a machine whose config.json has a
    # real Discord webhook (the module-level NOTIFY is resolved from live config).
    monkeypatch.setattr(conciliate, "NOTIFY",
                        conciliate.notifications.resolve_settings({}))
    conciliate.send_notification("T", "D", 3066993)   # must not raise


# ── video runner: startup-vs-midrun failure notice ───────────────────────────

def test_video_failure_notice_startup_is_distinct():
    title, desc = bv._failure_notice(started=False, exc=RuntimeError("no stock"))
    assert title == "Video upscale failed to start"
    assert "Could not start" in desc
    assert "no stock" in desc


def test_video_failure_notice_midrun_is_distinct():
    title, desc = bv._failure_notice(started=True, exc=RuntimeError("boom"))
    assert title == "Video upscale failed"
    assert "stopped on an error" in desc
    assert "boom" in desc


# ── video runner: early-stop label branches on the reason (item 2) ───────────

def test_stop_notice_per_run_cap():
    title, color, resume = bv._stop_notice("per-run cap of 30 min reached")
    assert title == "Video upscale paused (per-run cap)"
    assert color == 0xF1C40F and resume is True


def test_stop_notice_per_run_cost_cap():
    # the cost-cap message also starts with "per-run " -> same cap label
    title, color, resume = bv._stop_notice("per-run cost cap of $5 reached (~$4.80)")
    assert title == "Video upscale paused (per-run cap)"
    assert resume is True


def test_stop_notice_user_stop_is_not_a_cap():
    # the bug: a user Stop used to be labeled "paused (per-run cap)"
    title, color, resume = bv._stop_notice("stopped by user")
    assert title == "Video upscale stopped"
    assert "cap" not in title.lower()
    assert resume is True


def test_stop_notice_work_root_refusal_did_not_start():
    reason = ("the staging work folder is inside the source folder being scanned "
              "(X:\\stage): the scanner would re-read its own segments")
    title, color, resume = bv._stop_notice(reason)
    assert title == "Video upscale did not start"
    assert color == 0xE74C3C and resume is False


def test_stop_notice_unknown_reason_is_not_mislabeled_a_cap():
    title, color, resume = bv._stop_notice("some new reason we didn't foresee")
    assert "cap" not in title.lower()
    assert title == "Video upscale stopped early"


def test_notify_summary_user_stop_does_not_say_cap(monkeypatch):
    sent = {}
    monkeypatch.setattr(bv.notifications, "notify",
                        lambda settings, title, desc, color, fields=None:
                        sent.update(title=title, desc=desc, color=color))
    bv._notify_summary({"any": "settings"},
                       {"done": 2, "failed": 0, "stopped": "stopped by user", "total": 2},
                       "X:\\src")
    assert sent["title"] == "Video upscale stopped"
    assert "cap" not in sent["title"].lower()
    assert "stopped by user" in sent["desc"]
    assert "re-run to continue" in sent["desc"]


def test_notify_summary_startup_refusal_has_no_resume_hint(monkeypatch):
    sent = {}
    monkeypatch.setattr(bv.notifications, "notify",
                        lambda settings, title, desc, color, fields=None:
                        sent.update(title=title, desc=desc))
    reason = "the staging work folder is inside the source folder being scanned (X:\\s)"
    bv._notify_summary({"any": "settings"},
                       {"done": 0, "failed": 0, "stopped": reason, "total": 0},
                       "X:\\src")
    assert sent["title"] == "Video upscale did not start"
    assert "re-run to continue" not in sent["desc"]
