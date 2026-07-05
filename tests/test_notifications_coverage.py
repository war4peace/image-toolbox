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
