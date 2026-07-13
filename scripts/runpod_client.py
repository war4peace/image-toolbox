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

import re
import json
import time
import urllib.request
import urllib.parse
import urllib.error

from net_ssl import ssl_context

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

# Reference only (no longer used to filter the picker). The REST /pods create
# endpoint accepts ONLY this curated enum and 400s on newer cards — e.g.
# "NVIDIA RTX PRO 4500/4000 Blackwell" — that GraphQL's catalog lists with live
# stock. We deploy via the GraphQL path (deploy_pod) instead, which takes the
# FULL catalog, so this intersection is no longer needed; kept to document the
# REST limitation and why deploy_pod exists. (To regenerate: POST /pods with
# gpuTypeIds=["__invalid__"] and read the 400 body's "value must be one of …".)
CREATABLE_GPU_IDS = frozenset({
    "NVIDIA GeForce RTX 4090", "NVIDIA A40", "NVIDIA RTX A5000",
    "NVIDIA GeForce RTX 5090", "NVIDIA H100 80GB HBM3", "NVIDIA GeForce RTX 3090",
    "NVIDIA RTX A4500", "NVIDIA L40S", "NVIDIA H200", "NVIDIA L4",
    "NVIDIA RTX 6000 Ada Generation", "NVIDIA A100-SXM4-80GB",
    "NVIDIA RTX 4000 Ada Generation", "NVIDIA RTX A6000", "NVIDIA A100 80GB PCIe",
    "NVIDIA RTX 2000 Ada Generation", "NVIDIA RTX A4000",
    "NVIDIA RTX PRO 6000 Blackwell Server Edition", "NVIDIA H100 PCIe",
    "NVIDIA H100 NVL", "NVIDIA L40", "NVIDIA B200", "NVIDIA GeForce RTX 3080 Ti",
    "NVIDIA RTX PRO 6000 Blackwell Workstation Edition", "NVIDIA GeForce RTX 3080",
    "NVIDIA GeForce RTX 3070", "AMD Instinct MI300X OAM",
    "NVIDIA GeForce RTX 4080 SUPER", "Tesla V100-PCIE-16GB",
    "Tesla V100-SXM2-32GB", "NVIDIA RTX 5000 Ada Generation",
    "NVIDIA GeForce RTX 4070 Ti", "NVIDIA RTX 4000 SFF Ada Generation",
    "NVIDIA GeForce RTX 3090 Ti", "NVIDIA RTX A2000", "NVIDIA GeForce RTX 4080",
    "NVIDIA A30", "NVIDIA GeForce RTX 5080", "Tesla V100-FHHL-16GB",
    "NVIDIA H200 NVL", "Tesla V100-SXM2-16GB",
    "NVIDIA RTX PRO 6000 Blackwell Max-Q Workstation Edition", "NVIDIA A5000 Ada",
    "Tesla V100-PCIE-32GB", "Tesla T4", "NVIDIA RTX A30",
})

# ── Data centers & regions ───────────────────────────────────────────────────
# Network volumes are region-locked and a model volume can ONLY live in a data
# center that supports network storage, so the Settings picker is grouped by
# region and offers storage-capable DCs only — this stops a user provisioning a
# volume somewhere it (or a pod attaching it) can't actually run.
#
# The four UI regions and the data-center id prefixes that map to them. RunPod
# ids are prefixed by location (EU-RO-1, US-TX-3, AP-JP-1, …); derive a region
# from any id via region_of().
REGIONS = ["Europe", "North America", "Asia", "Oceania"]

_REGION_BY_PREFIX = {
    "EU": "Europe", "EUR": "Europe",
    "US": "North America", "CA": "North America",
    "AP": "Asia", "SEA": "Asia",
    "OC": "Oceania",
}


def region_of(dc_id):
    """Map a data-center id (e.g. 'EU-RO-1') to one of REGIONS, or '' if unknown."""
    if not dc_id:
        return ""
    prefix = str(dc_id).split("-", 1)[0].upper()
    return _REGION_BY_PREFIX.get(prefix, "")


# Curated fallback list of data centers that support network volumes
# (storageSupport=True, listed=True), so the picker works offline / before a live
# refresh. Generated 2026-06 from the GraphQL `dataCenters` query; data_centers()
# refreshes it live. (label, dataCenterId) — region is derived via region_of().
# (To regenerate: query `{ dataCenters { id location storageSupport listed } }`
# and keep storageSupport && listed.) Oceania has no storage-capable DC yet
# (OC-AU-1 is compute-only), so it appears empty until RunPod adds one — the live
# refresh will surface it automatically.
DATACENTERS = [
    # Europe
    ("Romania (EU-RO-1)",          "EU-RO-1"),
    ("Netherlands (EU-NL-1)",      "EU-NL-1"),
    ("Sweden (EU-SE-1)",           "EU-SE-1"),
    ("Czech Republic (EU-CZ-1)",   "EU-CZ-1"),
    ("France (EU-FR-1)",           "EU-FR-1"),
    ("Iceland (EUR-IS-1)",         "EUR-IS-1"),
    ("Iceland (EUR-IS-3)",         "EUR-IS-3"),
    ("Norway (EUR-NO-1)",          "EUR-NO-1"),
    ("Norway (EUR-NO-2)",          "EUR-NO-2"),
    # North America
    ("USA — California (US-CA-2)",   "US-CA-2"),
    ("USA — Georgia (US-GA-2)",      "US-GA-2"),
    ("USA — Illinois (US-IL-1)",     "US-IL-1"),
    ("USA — Kansas (US-KS-2)",       "US-KS-2"),
    ("USA — Missouri (US-MO-2)",     "US-MO-2"),
    ("USA — N. Carolina (US-NC-1)",  "US-NC-1"),
    ("USA — N. Carolina (US-NC-2)",  "US-NC-2"),
    ("USA — Nebraska (US-NE-1)",     "US-NE-1"),
    ("USA — Texas (US-TX-3)",        "US-TX-3"),
    ("USA — Washington (US-WA-1)",   "US-WA-1"),
    ("Canada — Montreal (CA-MTL-3)", "CA-MTL-3"),
    ("Canada — Montreal (CA-MTL-4)", "CA-MTL-4"),
    # Asia
    ("Japan (AP-JP-1)",            "AP-JP-1"),
    # Oceania — none with network storage at time of writing.
]

# Back-compat alias: some code/docs still refer to EU_DATACENTERS. The picker is
# now world-wide, but keep the Europe subset available under the old name.
EU_DATACENTERS = [(lbl, dcid) for lbl, dcid in DATACENTERS
                  if region_of(dcid) == "Europe"]


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
        with urllib.request.urlopen(req, timeout=timeout, context=ssl_context()) as resp:
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
    billing clock — callers must own a guaranteed stop path.

    NOTE: the REST create endpoint only accepts a CURATED GPU enum
    (CREATABLE_GPU_IDS) — it 400s on newer cards (Blackwell PRO 4000/4500) that
    the GraphQL deploy path (deploy_pod) handles. New code should prefer
    deploy_pod; this is kept for reference / the REST-only path."""
    return _request("POST", "/pods", api_key, body=spec, timeout=timeout)


# RunPod machines report a host driver CUDA version; the deploy filter
# `allowedCudaVersions` is EXACT-MATCH set membership (verified: values aren't
# range-compared), so to mean ">= X" we must enumerate every version >= X. A pod
# image built for cuYYZ won't START on a machine whose driver CUDA is lower
# (nvidia-container-cli: "unsatisfied condition: cuda>=12.8") — that's the failure
# that wasted the whole fallback chain on a CUDA-12.7 RTX 4090 in benchmarking.
# Listing a few not-yet-existing future versions is harmless (they just match
# nothing); the risk is the other way — omitting a real high version excludes a
# capable machine, so keep this ahead of RunPod's current max.
KNOWN_CUDA_VERSIONS = (
    "12.0", "12.1", "12.2", "12.3", "12.4", "12.5", "12.6", "12.7", "12.8",
    "12.9", "13.0", "13.1",
)


def _cuda_from_image(image):
    """Parse the CUDA version a pod image needs from its tag (…cu128… or
    …cu1281… → "12.8"), or None if not encoded."""
    m = re.search(r"cu(\d{3,4})", image or "")
    if not m:
        return None
    d = m.group(1)
    return f"{d[:2]}.{d[2]}"            # '128'/'1281' -> '12.8'


def allowed_cuda_versions(image):
    """Every known CUDA version >= what `image` requires — the value for the
    deploy filter so the pod only lands on a machine whose driver can run it.
    None when the image tag carries no cuXYZ hint (then no filter is applied)."""
    req = _cuda_from_image(image)
    if not req:
        return None
    try:
        floor = float(req)
    except ValueError:
        return None
    out = [v for v in KNOWN_CUDA_VERSIONS if float(v) >= floor]
    return out or None


def is_consumer_gpu(gpu_id):
    """True for consumer GeForce cards. They do NOT support CUDA forward
    compatibility, so a newer-CUDA image won't START on a host with an older
    driver (an RTX 4090 @ 12.7 can't launch a cu128 image). Datacenter/pro cards
    (A100, H100, H200, B200, A40, A6000, L4/L40, RTX PRO/RTX A…) DO forward-compat
    and run the same image on older drivers. Used to decide whether a deploy
    should pin a CUDA-version floor (see deploy_pod)."""
    return "geforce" in (gpu_id or "").lower()


# AMD Instinct accelerators (MI300X, MI250, MI325X…) sometimes appear in a data
# center's catalog cheaper than comparable NVIDIA cards, but the whole pipeline is
# CUDA-only: PyTorch is a CUDA build, SeedVR2 and the orientation CNN run on CUDA,
# and telemetry shells out to nvidia-smi — none of which works on AMD's ROCm/HIP
# stack. So an AMD card can never run a job; it must never reach the GPU pickers.
_AMD_GPU_RE = re.compile(r"\b(amd|instinct|radeon|mi\d{2,3}x?)\b", re.IGNORECASE)


def is_amd_gpu(gpu_id):
    """True for AMD GPUs (Instinct MI-series, Radeon). The app is CUDA-only, so
    these can't run any task — they're filtered out of the live GPU pickers
    (`available_gpus`) before a user can pick one that would only fail at run
    time."""
    return bool(_AMD_GPU_RE.search(gpu_id or ""))


_DEPLOY_MUTATION = """
mutation Deploy($input: PodFindAndDeployOnDemandInput!) {
  podFindAndDeployOnDemand(input: $input) { id machineId costPerHr }
}
"""


def deploy_pod(api_key, spec, timeout=90):
    """Create a pod via the GraphQL deploy path (`podFindAndDeployOnDemand`) and
    return {"id": …} for the new pod. This is the SAME path the RunPod console
    uses, so it accepts the FULL GPU catalog — including cards the REST
    create_pod enum rejects with HTTP 400 (e.g. Blackwell PRO 4000/4500).

    `spec` is the REST-style dict create_pod takes (so callers don't change); it
    is translated to the GraphQL input here. Two things the REST API does for free
    that GraphQL needs spelled out: a mounted network volume needs an explicit
    `volumeMountPath` (without it the container fails with "field Target must not
    be empty" and never starts — hence no public IP), and `supportPublicIp` must
    be set so the pod gets the direct-TCP SSH endpoint the app relies on.

    Like create_pod, this STARTS BILLING — callers must own a guaranteed stop
    path. The pod's SSH endpoint appears a bit later (poll via wait_until_running).
    Raises RunPodError on capacity / validation / transport failure.
    """
    gpu_ids = spec.get("gpuTypeIds") or []
    dcs = spec.get("dataCenterIds") or []
    vol = spec.get("networkVolumeId") or ""
    env = spec.get("env") or {}
    inp = {
        "cloudType":        spec.get("cloudType", "SECURE"),
        "gpuCount":         int(spec.get("gpuCount", 1)),
        "gpuTypeId":        gpu_ids[0] if gpu_ids else None,
        "name":             spec.get("name", "image-toolbox"),
        "imageName":        spec.get("imageName", ""),
        "containerDiskInGb": int(spec.get("containerDiskInGb", 30)),
        "volumeInGb":       int(spec.get("volumeInGb", 0)),
        "ports":            ",".join(spec.get("ports") or ["22/tcp"]),
        "supportPublicIp":  True,
        "env":              [{"key": k, "value": v} for k, v in env.items()],
    }
    if dcs:
        inp["dataCenterId"] = dcs[0]
    if vol:
        inp["networkVolumeId"] = vol
        # REST defaults this; GraphQL requires it or the bind-mount fails.
        inp["volumeMountPath"] = spec.get("volumeMountPath", "/workspace")
    # CUDA driver floor — applied ONLY to consumer GeForce cards. A GeForce card
    # has no CUDA forward-compat, so a cu128 image won't START on a host driver
    # below 12.8 (an RTX 4090 @ 12.7 failed exactly this, which is why the floor
    # exists). Datacenter/pro cards DO forward-compat and run the image on older
    # drivers, so a floor only hurts them: it excludes every in-stock host whose
    # driver is older than the image (e.g. A100 PCIe @ 12.4–12.7 in EU-RO-1, which
    # run cu128 fine) and surfaces as "no instances available" even when the
    # console shows the card available. Omit it for them, matching the website
    # deploy that works. An explicit spec override always wins.
    explicit = spec.get("allowedCudaVersions")
    gpu0 = gpu_ids[0] if gpu_ids else ""
    if explicit:
        cuda = explicit
    elif is_consumer_gpu(gpu0):
        cuda = allowed_cuda_versions(inp["imageName"])
    else:
        cuda = None
    if cuda:
        inp["allowedCudaVersions"] = cuda
    data = _graphql(api_key, _DEPLOY_MUTATION, {"input": inp}, timeout=timeout)
    pod = data.get("podFindAndDeployOnDemand")
    if not isinstance(pod, dict) or not pod.get("id"):
        raise RunPodError(f"Deploy returned no pod id: {data}")
    return pod


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
    attempts_made = 0                  # real count across the chain (may be < attempts:
                                       # a capacity error breaks a GPU's retries early)
    for gpu in gpu_chain:
        gspec = spec if gpu is None else {**spec, "gpuTypeIds": [gpu]}
        for attempt in range(1, attempts + 1):
            attempts_made += 1
            try:
                # GraphQL deploy (not REST create): accepts the full GPU catalog,
                # so a card from the live picker never 400s at create time.
                pod = deploy_pod(api_key, gspec)
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
        on_event("giveup", attempts_made, None, last_err)
    tries = f"{attempts_made} attempt" + ("" if attempts_made == 1 else "s")
    if len(gpu_chain) == 1:
        # No GPU-type substitution (0.4.0): a run uses only the picked card, so report
        # it plainly instead of the multi-card "on any of [...] N attempts each" wording.
        where = f"on {gpu_chain[0]} ({tries})"
    else:
        where = f"on any of {gpu_chain} ({tries} total)"
    raise RunPodError(f"Pod failed to deploy {where}. Last error: {last_err}")


def volume_region(api_key, vol_id, timeout=30):
    """Return a network volume's dataCenterId (its region), or None. Pods that
    mount the volume MUST be created in this same data center."""
    try:
        vol = get_network_volume(api_key, vol_id, timeout=timeout)
    except RunPodError:
        return None
    return vol.get("dataCenterId") if isinstance(vol, dict) else None


def _pod_volume_id(pod):
    """The network volume id a pod has attached, or None. The REST pod object has
    exposed it both flat (`networkVolumeId`) and nested (`networkVolume.id`) across
    API versions, so check both."""
    if not isinstance(pod, dict):
        return None
    v = pod.get("networkVolumeId")
    if v:
        return v
    nv = pod.get("networkVolume")
    return nv.get("id") if isinstance(nv, dict) else None


def pods_using_volume(api_key, vol_id, timeout=30):
    """Return the pods that currently have network volume `vol_id` attached, as
    a list of {"id", "name", "status"}.

    Used as a provisioning pre-flight: rebuilding the venv on a volume that a
    running pod still mounts corrupts that pod's live worker and NFS-blocks the
    `rm -rf` (the file is held open → "Directory not empty"). Best-effort —
    returns [] on any list failure or an API version that omits the volume field,
    so a transient hiccup never blocks provisioning (the move-aside in
    provision.sh is the backstop)."""
    if not vol_id:
        return []
    try:
        pods = list_pods(api_key, timeout=timeout)
    except RunPodError:
        return []
    out = []
    for p in pods:
        if _pod_volume_id(p) == vol_id:
            out.append({"id": p.get("id"), "name": p.get("name"),
                        "status": p.get("desiredStatus")})
    return out


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
        with urllib.request.urlopen(req, timeout=timeout, context=ssl_context()) as resp:
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


def available_gpus(api_key, data_center_id=None, min_memory_gb=0, timeout=30,
                   include_out_of_stock=False):
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

    AMD GPUs (Instinct MI-series, Radeon) are dropped here regardless of stock or
    price: the pipeline is CUDA-only (see `is_amd_gpu`), so an AMD card could only
    fail at run time. They're cheap enough to be tempting in some data centers, so
    filtering them at the source keeps them out of every picker.

    `include_out_of_stock=True` keeps cards with no current stock (their `stock`
    is None) — for the Settings GPU *preference* lists, which should still offer a
    card that's only momentarily sold out. The live per-run pickers leave it False.
    """
    data = _graphql(api_key, _GPU_AVAIL_QUERY, {"dc": data_center_id or None},
                    timeout=timeout)
    out = []
    for g in data.get("gpuTypes") or []:
        gid = g.get("id") or ""
        name = g.get("displayName") or gid
        lp = g.get("lowestPrice") or {}
        stock = lp.get("stockStatus")
        price = lp.get("uninterruptablePrice")
        mem = g.get("memoryInGb") or 0
        if is_amd_gpu(gid) or is_amd_gpu(name):       # CUDA-only app → never offer AMD
            continue
        if not stock and not include_out_of_stock:   # out of stock here → skip
            continue
        if mem < min_memory_gb:
            continue
        # No REST-enum filter here: pods are created via the GraphQL deploy path
        # (deploy_pod), which accepts the full catalog — so every in-stock card is
        # genuinely deployable, matching what the RunPod console offers.
        out.append({
            "id":        gid,
            "name":      name,
            "memory_gb": mem,
            "price":     price,
            "stock":     stock,
        })
    # Cheapest first; a missing price sorts last so a real quote always wins.
    out.sort(key=lambda r: (r["price"] is None, r["price"] or 0.0))
    return out


_BALANCE_QUERY = "query { myself { clientBalance currentSpendPerHr } }"


def account_balance(api_key, timeout=15):
    """Live account balance for the money safety-net (funds_guard, roadmap #1).

    Returns {"balance": float (USD), "spend_per_hr": float (USD/h)} or None on ANY
    failure — no key, unreachable, unparseable. Fail-safe by contract: the funds
    guard skips its checks when the balance is unknown and never blocks a run on
    it. The balance is not in the REST API; only the legacy GraphQL `myself` query
    exposes it (which _graphql already reaches with the browser User-Agent
    Cloudflare requires)."""
    try:
        data = _graphql(api_key, _BALANCE_QUERY, timeout=timeout)
    except Exception:                                    # noqa: BLE001 (fail-safe)
        return None
    me = (data or {}).get("myself") or {}
    bal = me.get("clientBalance")
    if bal is None:
        return None
    try:
        return {"balance": float(bal),
                "spend_per_hr": float(me.get("currentSpendPerHr") or 0.0)}
    except (TypeError, ValueError):
        return None


_DC_QUERY = "{ dataCenters { id name location storageSupport listed } }"


def data_centers(api_key, storage_only=True, listed_only=True, timeout=30):
    """Live list of RunPod data centers via GraphQL (the same source the GPU
    picker uses). Each item:
        {"id", "location", "region", "storage", "listed"}
    With `storage_only` (default) only data centers that support network volumes
    are returned — a model volume can only live where storage exists, so this is
    exactly what the Settings picker should offer. Sorted by region (UI order)
    then id. Raises RunPodError on failure (callers run it off a thread and fall
    back to the curated DATACENTERS list)."""
    data = _graphql(api_key, _DC_QUERY, {}, timeout=timeout)
    out = []
    for d in data.get("dataCenters") or []:
        if not isinstance(d, dict):
            continue
        if listed_only and not d.get("listed"):
            continue
        if storage_only and not d.get("storageSupport"):
            continue
        did = d.get("id") or ""
        out.append({
            "id":       did,
            "location": d.get("location") or "",
            "region":   region_of(did),
            "storage":  bool(d.get("storageSupport")),
            "listed":   bool(d.get("listed")),
        })
    order = {r: i for i, r in enumerate(REGIONS)}
    out.sort(key=lambda x: (order.get(x["region"], 99), x["id"]))
    return out


# The REST /pods object omits the GPU type and data center (only id/name/status/
# costPerHr come back), so the pod list is fetched via GraphQL — the same source
# the RunPod console uses — which exposes a clean machine.gpuDisplayName. The data
# center field name on the pod's machine isn't certain across schema versions, and
# GraphQL 400s the WHOLE query on one unknown field, so try a few machine
# sub-selections richest-first and remember the first the API accepts.
_PODS_MACHINE_SELECTIONS = (
    "gpuDisplayName dataCenterId",
    "gpuDisplayName location",
    "gpuDisplayName",
)
_pods_machine_sel = None        # the sub-selection known to work (memoised)


def _pods_query(machine_sel):
    return ("query Pods { myself { pods { id name desiredStatus costPerHr "
            "gpuCount machine { " + machine_sel + " } } } }")


def _normalize_gql_pod(p):
    m = p.get("machine") if isinstance(p.get("machine"), dict) else {}
    dc = m.get("dataCenterId") or m.get("location") or "?"
    return {
        "id":           p.get("id", ""),
        "name":         p.get("name") or p.get("id", ""),
        "status":       p.get("desiredStatus") or "?",
        "gpu":          m.get("gpuDisplayName") or "?",
        "gpu_count":    p.get("gpuCount"),
        "data_center":  dc,
        "region":       region_of(dc) or "?",      # derived locally from the id
        "cost":         p.get("costPerHr"),
    }


def _normalize_rest_pod(p):
    machine = p.get("machine") if isinstance(p.get("machine"), dict) else {}
    gpu_obj = p.get("gpu") if isinstance(p.get("gpu"), dict) else {}
    dc = (p.get("dataCenterId") or machine.get("dataCenterId")
          or machine.get("location") or "?")
    return {
        "id":           p.get("id", ""),
        "name":         p.get("name") or p.get("id", ""),
        "status":       p.get("desiredStatus") or p.get("status") or "?",
        "gpu":          (p.get("gpuTypeId") or machine.get("gpuDisplayName")
                         or gpu_obj.get("displayName") or gpu_obj.get("id") or "?"),
        "gpu_count":    p.get("gpuCount"),
        "data_center":  dc,
        "region":       region_of(dc) or "?",      # derived locally from the id
        "cost":         p.get("costPerHr"),
    }


def list_pods_detailed(api_key, timeout=30):
    """Return the account's pods with clean, typed fields, each:
        {id, name, status, gpu, gpu_count, data_center, cost}

    Prefers GraphQL (`myself.pods`) because the REST pod object omits the GPU type
    and data center; falls back to REST /pods if every GraphQL attempt fails (so
    the list still works, just with '?' for the missing fields)."""
    global _pods_machine_sel
    selections = ([_pods_machine_sel] if _pods_machine_sel
                  else list(_PODS_MACHINE_SELECTIONS))
    last_err = None
    for sel in selections:
        try:
            data = _graphql(api_key, _pods_query(sel), timeout=timeout)
        except RunPodError as exc:
            last_err = exc
            continue
        _pods_machine_sel = sel        # remember the field set the API accepted
        pods = ((data.get("myself") or {}).get("pods")) or []
        return [_normalize_gql_pod(p) for p in pods if isinstance(p, dict)]
    # Every GraphQL attempt failed → REST fallback (id/name/status/cost at least).
    try:
        return [_normalize_rest_pod(p) for p in list_pods(api_key, timeout=timeout)
                if isinstance(p, dict)]
    except RunPodError:
        raise last_err or RunPodError("Could not list pods.")


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
