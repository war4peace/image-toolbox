"""
runpod_client.py
----------------
Thin client for the RunPod REST control plane — the pod *control* layer for the
future remote-upscaling feature (docs/future-features.md #1). This module only
creates / starts / stops / terminates / inspects pods; it does not move images or
run the upscaler (that is the on-pod worker + the streaming engine, later phases).

Design notes:
  * Pure standard library (urllib) — keeps the dependency-light promise, same as
    updater.py. No SDK, no requests.
  * All calls block and are meant to be run from a background thread; the GUI
    (toolbox_gui.py) owns the dialogs and threading.
  * Fail-safe and explicit: every call either returns parsed JSON or raises
    RunPodError with a human-readable message. The UI-facing helpers
    (test_connection / ensure_stopped) return (ok, message) tuples instead, so
    they mirror mqtt_publisher.test_connection and never throw into the UI.
  * The API key is a credential. It lives in config.json's `runpod` section
    (blank in the tracked template, exactly like the mqtt block) and is passed in
    by the caller — this module never reads config or persists anything.

API surface (verified against rest.runpod.io/v1 OpenAPI, 2026-06):
    GET    /pods                 list_pods
    GET    /pods/{id}            get_pod          (status in `desiredStatus`)
    POST   /pods                 create_pod
    POST   /pods/{id}/start      start_pod
    POST   /pods/{id}/stop       stop_pod
    DELETE /pods/{id}            terminate_pod
"""

import json
import urllib.request
import urllib.parse
import urllib.error

BASE_URL    = "https://rest.runpod.io/v1"
_USER_AGENT = "ImageToolbox-RunPod"

# `desiredStatus` values returned by the API.
STATUS_RUNNING    = "RUNNING"
STATUS_EXITED     = "EXITED"
STATUS_TERMINATED = "TERMINATED"


class RunPodError(Exception):
    """A RunPod REST call failed (network, auth, HTTP, or parse error)."""


def _request(method, path, api_key, body=None, params=None, timeout=30):
    """Issue one REST call and return the parsed JSON (or None for empty 2xx).

    Raises RunPodError with a friendly message on any failure. `path` is relative
    to BASE_URL and must start with '/'.
    """
    if not api_key:
        raise RunPodError("No RunPod API key is set (Settings → Remote upscaling).")

    url = BASE_URL + path
    if params:
        # Drop None/empty params so callers can pass them unconditionally.
        clean = {k: v for k, v in params.items() if v not in (None, "")}
        if clean:
            url += "?" + urllib.parse.urlencode(clean, doseq=True)

    data = None
    headers = {
        "Authorization": f"Bearer {api_key}",
        "User-Agent":    _USER_AGENT,
        "Accept":        "application/json",
    }
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        raise RunPodError(_http_error_message(exc)) from exc
    except urllib.error.URLError as exc:
        raise RunPodError(f"Could not reach RunPod: {exc.reason}") from exc
    except Exception as exc:                              # noqa: BLE001 (fail-safe)
        raise RunPodError(f"RunPod request failed: {exc}") from exc

    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except ValueError as exc:
        raise RunPodError("RunPod returned a response that could not be parsed.") from exc


def _http_error_message(exc):
    """Turn an HTTPError into a message that explains the common cases."""
    if exc.code == 401:
        return "RunPod rejected the API key (401 Unauthorized) — check it in Settings."
    if exc.code == 403:
        return "RunPod denied access (403 Forbidden) — the key may lack permission."
    if exc.code == 404:
        return "Not found (404) — the pod may have been terminated."
    if exc.code == 429:
        return "RunPod is rate-limiting requests (429) — try again shortly."
    # Try to surface the API's own error text, if any.
    detail = ""
    try:
        body = json.loads(exc.read().decode("utf-8", "replace"))
        detail = body.get("error") or body.get("message") or ""
    except Exception:                                    # noqa: BLE001
        pass
    detail = f" — {detail}" if detail else ""
    return f"RunPod returned HTTP {exc.code} {exc.reason}{detail}."


# ── pod lifecycle ────────────────────────────────────────────────────────────

def list_pods(api_key, timeout=30, **filters):
    """Return the list of pods (optionally filtered, e.g. desiredStatus=RUNNING).

    The API has returned both a bare list and a {"pods": [...]}-style envelope
    across versions; normalise to a list either way.
    """
    result = _request("GET", "/pods", api_key, params=filters or None, timeout=timeout)
    if isinstance(result, dict):
        return result.get("pods") or result.get("data") or []
    return result or []


def get_pod(api_key, pod_id, timeout=30):
    """Return one pod's details (its state is in `desiredStatus`)."""
    return _request("GET", f"/pods/{pod_id}", api_key, timeout=timeout)


def create_pod(api_key, spec, timeout=60):
    """Create a pod from `spec` (gpuTypeIds, imageName/templateId, ports, …) and
    return the created pod (including its new `id`). Creating a pod starts the
    billing clock — callers must own a guaranteed stop path."""
    return _request("POST", "/pods", api_key, body=spec, timeout=timeout)


def start_pod(api_key, pod_id, timeout=60):
    """Start or resume a stopped pod."""
    return _request("POST", f"/pods/{pod_id}/start", api_key, timeout=timeout)


def stop_pod(api_key, pod_id, timeout=60):
    """Stop a running pod (it can later be started again; storage is still billed
    while stopped). For the dead-man's-switch teardown use terminate_pod."""
    return _request("POST", f"/pods/{pod_id}/stop", api_key, timeout=timeout)


def terminate_pod(api_key, pod_id, timeout=60):
    """Terminate (delete) a pod — frees all billing. The disposable-pod teardown."""
    return _request("DELETE", f"/pods/{pod_id}", api_key, timeout=timeout)


def pod_status(api_key, pod_id, timeout=30):
    """Return a pod's `desiredStatus` (RUNNING/EXITED/TERMINATED), or None if the
    pod is gone / unreadable. Never raises — for safe polling."""
    try:
        pod = get_pod(api_key, pod_id, timeout=timeout)
    except RunPodError:
        return None
    if not isinstance(pod, dict):
        return None
    return pod.get("desiredStatus")


# ── UI-facing helpers (return (ok, message), never raise) ────────────────────

def test_connection(api_key, timeout=15):
    """Verify the API key by listing pods. Returns (ok, message) for the
    Settings 'Test' button — mirrors mqtt_publisher.test_connection."""
    if not api_key:
        return False, "Enter a RunPod API key first."
    try:
        pods = list_pods(api_key, timeout=timeout)
    except RunPodError as exc:
        return False, str(exc)
    running = sum(1 for p in pods if isinstance(p, dict)
                  and p.get("desiredStatus") == STATUS_RUNNING)
    n = len(pods)
    if n == 0:
        return True, "Connected — API key is valid (no pods currently on the account)."
    return True, f"Connected — {n} pod(s) on the account, {running} running."


def ensure_stopped(api_key, pod_id, terminate=False, timeout=60):
    """Best-effort guaranteed stop for the auto-stop / dead-man's-switch path.

    Idempotent: a pod already EXITED/TERMINATED (or already gone) counts as
    success. Returns (ok, message); never raises, so a failing teardown can be
    surfaced to the user (with the pod id) rather than crashing a run.
    """
    if not pod_id:
        return True, "No pod to stop."
    status = pod_status(api_key, pod_id, timeout=timeout)
    if status in (STATUS_EXITED, STATUS_TERMINATED, None):
        return True, f"Pod {pod_id} is already stopped."
    try:
        if terminate:
            terminate_pod(api_key, pod_id, timeout=timeout)
            return True, f"Pod {pod_id} terminated."
        stop_pod(api_key, pod_id, timeout=timeout)
        return True, f"Pod {pod_id} stopped."
    except RunPodError as exc:
        verb = "terminate" if terminate else "stop"
        return False, (f"Could not {verb} pod {pod_id}: {exc}  "
                       f"Stop it manually in the RunPod dashboard to avoid charges.")
