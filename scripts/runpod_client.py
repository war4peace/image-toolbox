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
import time
import urllib.request
import urllib.parse
import urllib.error

BASE_URL    = "https://rest.runpod.io/v1"
_USER_AGENT = "ImageToolbox-RunPod"

# The REST control plane can't list GPU types / prices / availability, but the
# GraphQL endpoint can (see available_gpus). Cloudflare in front of it rejects a
# non-browser User-Agent, so GraphQL calls send a browser one.
GRAPHQL_URL = "https://api.runpod.io/graphql"
_BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

# Where users create an API key (console) and read how (docs) — surfaced as a
# link in Settings so a user can get a key without hunting for it.
CONSOLE_API_KEYS_URL = "https://www.runpod.io/console/user/settings"
DOCS_API_KEYS_URL    = "https://docs.runpod.io/get-started/api-keys"

# `desiredStatus` values returned by the API.
STATUS_RUNNING    = "RUNNING"
STATUS_EXITED     = "EXITED"
STATUS_TERMINATED = "TERMINATED"

# The REST API exposes NO endpoint to list GPU types or data centers (they are
# fixed enums) — so these are curated picklists for the Settings UI. Edit
# config.json directly for an id not listed here.
#
# Common GPU types (the value goes into create_pod's `gpuTypeIds`):
GPU_TYPES = [
    "NVIDIA GeForce RTX 5090",
    "NVIDIA GeForce RTX 4090",
    "NVIDIA RTX A5000",
    "NVIDIA RTX A6000",
    "NVIDIA A40",
    "NVIDIA L40S",
    "NVIDIA L40",
    "NVIDIA H100 80GB HBM3",
]

# Curated low-tier GPUs for remote Tag & Rename. The vision model (qwen2.5vl:7b
# via Ollama) needs only ~6.6 GB VRAM (benchmarked) and tagging isn't
# throughput-critical (warm ~2.6 s/image on an RTX 2000 Ada), so a cheap 16-20 GB
# card is ideal and far cheaper than the upscale GPU. Used as an ORDERED FALLBACK
# CHAIN: the configured `tag_gpu_type_id` is tried first, then the rest in turn,
# so a tag run still starts when the preferred card is unavailable. All four are
# secure-cloud and available in the EU. Prices are point-in-time (EU, 2026-06),
# informational only. (label, gpuTypeId)
TAG_GPU_TYPES = [
    ("RTX 2000 Ada — 16 GB (~$0.24/h)", "NVIDIA RTX 2000 Ada Generation"),
    ("RTX A4000 — 16 GB (~$0.25/h)",     "NVIDIA RTX A4000"),
    ("RTX A4500 — 20 GB (~$0.25/h)",     "NVIDIA RTX A4500"),
    ("RTX 4000 Ada — 20 GB (~$0.26/h)",  "NVIDIA RTX 4000 Ada Generation"),
]

# European data centers only (user is in Romania; network volumes are
# region-locked and throughput is region-dependent). EU-RO-1 is closest.
# (label, dataCenterId)
EU_DATACENTERS = [
    ("Romania (EU-RO-1)",        "EU-RO-1"),
    ("Netherlands (EU-NL-1)",    "EU-NL-1"),
    ("Sweden (EU-SE-1)",         "EU-SE-1"),
    ("Czech Republic (EU-CZ-1)", "EU-CZ-1"),
    ("Iceland (EUR-IS-1)",       "EUR-IS-1"),
    ("Iceland (EUR-IS-2)",       "EUR-IS-2"),
]


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


# ── provisioning helpers (poll for ready, read SSH endpoint) ─────────────────

def ssh_endpoint(pod):
    """Return (host, port) for direct-TCP SSH into a pod, or (None, None) if the
    pod hasn't published its port-22 mapping yet. Requires the pod to have been
    created with "22/tcp" in its ports."""
    if not isinstance(pod, dict):
        return None, None
    ip = pod.get("publicIp")
    mappings = pod.get("portMappings") or {}
    port = mappings.get("22") or mappings.get(22)
    return (ip, port) if (ip and port) else (None, None)


def wait_until_running(api_key, pod_id, timeout=600, poll=5, on_status=None):
    """Poll a pod until it is RUNNING with a reachable SSH endpoint, and return
    the pod dict. Raises RunPodError on timeout or if the pod ends up
    EXITED/TERMINATED first. `on_status(status, host, port)` is called on each
    poll for UI feedback."""
    deadline = time.time() + timeout
    while True:
        pod = get_pod(api_key, pod_id)
        status = pod.get("desiredStatus") if isinstance(pod, dict) else None
        host, port = ssh_endpoint(pod)
        if on_status:
            try:
                on_status(status, host, port)
            except Exception:                            # noqa: BLE001 (UI cb is best-effort)
                pass
        if status in (STATUS_EXITED, STATUS_TERMINATED):
            raise RunPodError(f"Pod entered {status} before it became reachable.")
        if status == STATUS_RUNNING and host and port:
            return pod
        if time.time() >= deadline:
            raise RunPodError(
                f"Timed out after {timeout}s waiting for the pod to become reachable "
                f"(last status: {status}).")
        time.sleep(poll)


def create_pod_resilient(api_key, spec, attempts=3, deploy_timeout=240, poll=8,
                         on_event=None):
    """Create a pod and wait until it is reachable, retrying with a FRESH pod if
    it doesn't deploy in time or EXITs early — and falling through a GPU chain.

    RunPod occasionally hands out a pod that never finishes deploying (a
    misconfigured/unhealthy host) — it sits without an SSH endpoint, or flips to
    EXITED. Rather than wait forever, give each pod a `deploy_timeout`; on failure
    terminate it and try another, up to `attempts` times.

    `spec["gpuTypeIds"]` may list **several GPU types**: they are an ORDERED
    FALLBACK CHAIN. Each type is tried in turn — a create/capacity error moves to
    the next type immediately, a deploy failure retries the SAME type up to
    `attempts` times — so a run still starts when the preferred GPU is sold out.
    A single-element list behaves exactly as before. Returns the running pod dict,
    or raises RunPodError if no type yields a reachable pod.

    `on_event(kind, attempt, pod_id, info)` is called for UI/logging with kind in
    {"created","status","bad","giveup"}; for "created"/"bad" `info` carries the
    GPU type being tried.
    """
    gpu_chain = list(spec.get("gpuTypeIds") or [None])
    last_err = None
    for gpu in gpu_chain:
        gspec = spec if gpu is None else {**spec, "gpuTypeIds": [gpu]}
        for attempt in range(1, attempts + 1):
            try:
                pod = create_pod(api_key, gspec)
            except RunPodError as exc:
                # Capacity / unavailable at create time → try the next GPU now
                # rather than burning this type's remaining attempts.
                last_err = f"{gpu or '?'}: {exc}"
                if on_event:
                    on_event("bad", attempt, None, last_err)
                break
            pod_id = (pod or {}).get("id")
            if not pod_id:
                last_err = f"{gpu or '?'}: create returned no pod id: {pod}"
                if on_event:
                    on_event("bad", attempt, None, last_err)
                continue
            if on_event:
                on_event("created", attempt, pod_id, gpu)
            try:
                return wait_until_running(
                    api_key, pod_id, timeout=deploy_timeout, poll=poll,
                    on_status=(lambda s, h, p, a=attempt, pid=pod_id:
                               on_event("status", a, pid, (s, h, p)) if on_event else None))
            except RunPodError as exc:
                last_err = f"{gpu or '?'}: {exc}"
                if on_event:
                    on_event("bad", attempt, pod_id, last_err)
                # Tear the bad pod down before trying again (don't leave it billing).
                ensure_stopped(api_key, pod_id, terminate=True)
    if on_event:
        on_event("giveup", attempts, None, last_err)
    raise RunPodError(
        f"Pod failed to deploy on any of {gpu_chain} after {attempts} attempt(s) "
        f"each. Last error: {last_err}")


def volume_region(api_key, vol_id, timeout=30):
    """Return a network volume's dataCenterId (its region), or None. Pods that
    mount the volume MUST be created in this same data center."""
    try:
        vol = get_network_volume(api_key, vol_id, timeout=timeout)
    except RunPodError:
        return None
    return vol.get("dataCenterId") if isinstance(vol, dict) else None


# ── network volumes (the persistent model store) ─────────────────────────────

def list_network_volumes(api_key, timeout=30):
    """Return the account's network volumes (each has id, name, size, dataCenterId)."""
    result = _request("GET", "/networkvolumes", api_key, timeout=timeout)
    if isinstance(result, dict):
        return result.get("networkVolumes") or result.get("data") or []
    return result or []


def get_network_volume(api_key, vol_id, timeout=30):
    """Return one network volume's details."""
    return _request("GET", f"/networkvolumes/{vol_id}", api_key, timeout=timeout)


def create_network_volume(api_key, name, size_gb, data_center_id, timeout=60):
    """Create a network volume (size in GB, 1–4000) in a specific data center.

    The data center is fixed at creation and locks every pod that mounts the
    volume to that region — pass an EU id for a Europe-based user."""
    size = int(size_gb)
    if not 1 <= size <= 4000:
        raise RunPodError("Network volume size must be between 1 and 4000 GB.")
    if not data_center_id:
        raise RunPodError("A data center id is required to create a network volume.")
    body = {"name": name, "size": size, "dataCenterId": data_center_id}
    return _request("POST", "/networkvolumes", api_key, body=body, timeout=timeout)


def delete_network_volume(api_key, vol_id, timeout=60):
    """Delete a network volume (frees its monthly storage charge)."""
    return _request("DELETE", f"/networkvolumes/{vol_id}", api_key, timeout=timeout)


# ── GPU availability + pricing (GraphQL) ─────────────────────────────────────

def _graphql(api_key, query, variables=None, timeout=30):
    """Issue one GraphQL POST and return the `data` object. Raises RunPodError on
    transport/HTTP errors or any GraphQL `errors`. Sends a browser User-Agent —
    Cloudflare in front of api.runpod.io rejects an unknown one with a 403."""
    if not api_key:
        raise RunPodError("No RunPod API key is set (Settings → Remote upscaling).")
    body = json.dumps({"query": query, "variables": variables or {}}).encode("utf-8")
    req = urllib.request.Request(
        GRAPHQL_URL, data=body, method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type":  "application/json",
            "Accept":        "application/json",
            "User-Agent":    _BROWSER_UA,
        })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        raise RunPodError(_http_error_message(exc)) from exc
    except urllib.error.URLError as exc:
        raise RunPodError(f"Could not reach RunPod: {exc.reason}") from exc
    except Exception as exc:                              # noqa: BLE001 (fail-safe)
        raise RunPodError(f"RunPod GraphQL request failed: {exc}") from exc
    try:
        parsed = json.loads(raw.decode("utf-8", "replace"))
    except ValueError as exc:
        raise RunPodError("RunPod returned a response that could not be parsed.") from exc
    if parsed.get("errors"):
        msg = parsed["errors"][0].get("message", "unknown error")
        raise RunPodError(f"RunPod GraphQL error: {msg}")
    return parsed.get("data") or {}


_GPU_AVAIL_QUERY = """
query GpuTypes($dc: String) {
  gpuTypes {
    id
    displayName
    memoryInGb
    lowestPrice(input: {gpuCount: 1, secureCloud: true, dataCenterId: $dc}) {
      uninterruptablePrice
      stockStatus
    }
  }
}
"""


def available_gpus(api_key, data_center_id=None, min_memory_gb=0, timeout=30):
    """Return the secure-cloud GPUs that are DEPLOYABLE RIGHT NOW, newest data.

    Queries GraphQL for every GPU type's live price + stock in `data_center_id`
    (None = RunPod's global view), keeps only those with stock (`stockStatus`
    set) and at least `min_memory_gb` of VRAM, and returns them sorted by hourly
    price ascending. Each item is a dict:
        {"id", "name", "memory_gb", "price", "stock"}
    where `id` is the value create_pod's `gpuTypeIds` expects. Raises RunPodError
    on failure (callers run this off a background thread and show the message).

    Unlike the curated GPU_TYPES / TAG_GPU_TYPES picklists, this reflects reality
    — a card the account can't actually rent in this region never appears, so the
    UI can't offer a GPU that will only fail at create time.
    """
    data = _graphql(api_key, _GPU_AVAIL_QUERY, {"dc": data_center_id or None},
                    timeout=timeout)
    out = []
    for g in data.get("gpuTypes") or []:
        lp = g.get("lowestPrice") or {}
        stock = lp.get("stockStatus")
        price = lp.get("uninterruptablePrice")
        mem = g.get("memoryInGb") or 0
        if not stock:                       # out of stock in this region → skip
            continue
        if mem < min_memory_gb:
            continue
        out.append({
            "id":        g.get("id"),
            "name":      g.get("displayName") or g.get("id"),
            "memory_gb": mem,
            "price":     price,
            "stock":     stock,
        })
    # Cheapest first; a missing price sorts last so a real quote always wins.
    out.sort(key=lambda r: (r["price"] is None, r["price"] or 0.0))
    return out


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
    if status is None or status == STATUS_TERMINATED:
        return True, f"Pod {pod_id} is already gone."
    # An EXITED pod is "stopped" already — but if we mean to TERMINATE, it still
    # exists (and may bill storage), so fall through and delete it.
    if status == STATUS_EXITED and not terminate:
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
