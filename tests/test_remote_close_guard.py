"""
Item 3: RemoteSession.close() must not raise out of the runners' finally block.

close() runs from every runner's `finally` (e.g. batch_video_upscale.main). Every
teardown step is guarded EXCEPT the pod-stop call itself used to be: an API/network
error in rp.ensure_stopped propagated out of close(), replacing the run's real
outcome with a confusing secondary traceback and skipping _close_log(). The on-pod
dead-man's switch stops the pod on the idle timeout regardless, so a failed stop call
is log-worthy, not crash-worthy. These tests pin the guard without a real pod.
"""

import remote_run
from remote_run import RemoteSession


def _bare_session(monkeypatch):
    """A RemoteSession with only the attributes close() touches, so no pod is needed."""
    s = object.__new__(RemoteSession)
    s._funds_guard = None
    s.engine = None
    s._ollama_tunnel = None
    s.pod_id = "pod-123"
    s._attach = False                 # created (not reused) -> default is to stop it
    s.api_key = "key"
    s.terminate_when_done = True
    s._emitted = []
    s._emit = s._emitted.append
    return s


def test_close_swallows_ensure_stopped_error(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("runpod API 500")
    monkeypatch.setattr(remote_run.rp, "ensure_stopped", boom)
    s = _bare_session(monkeypatch)
    s.close()                          # must NOT raise
    joined = "\n".join(s._emitted)
    assert "Could not stop the pod" in joined
    assert "dead-man's switch" in joined


def test_close_emits_the_normal_stop_message_on_success(monkeypatch):
    monkeypatch.setattr(remote_run.rp, "ensure_stopped",
                        lambda *a, **k: (True, "pod stopped."))
    s = _bare_session(monkeypatch)
    s.close()
    assert "pod stopped." in s._emitted


def test_close_leaves_a_reused_pod_running(monkeypatch):
    # A reused (attached) pod is never stopped by close(): no ensure_stopped call.
    called = {"n": 0}
    monkeypatch.setattr(remote_run.rp, "ensure_stopped",
                        lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    s = _bare_session(monkeypatch)
    s._attach = True
    s.close()
    assert called["n"] == 0
    assert any("Leaving the remote pod running" in m for m in s._emitted)
