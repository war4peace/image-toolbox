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
    `configure(cfg)` is not an exception: it takes an already-loaded dict from
    whoever owns the file, and only picks the REST version out of it.

API surface, both REST versions (v1 verified 2026-06, v2 verified 2026-08 against
a real pod). The v2 column is what runs by default; see "which REST API to talk
to" below and docs/future-features.md #25.

                        v1                        v2
    list_pods           GET    /pods              GET    /pods
    get_pod             GET    /pods/{id}         GET    /pods/{id}
    create_pod          POST   /pods              POST   /pods        (new body)
    start_pod           POST   /pods/{id}/start   POST   /pods/{id}/action
    stop_pod            POST   /pods/{id}/stop    POST   /pods/{id}/action
    terminate_pod       DELETE /pods/{id}         POST   /pods/{id}/action
    network volumes            /networkvolumes           /network-volumes

The pod's state is in `desiredStatus` on v1 and `status` on v2; nothing outside
this module knows that, because every read goes through the normalisation seam.
"""

import re
import json
import time
import urllib.request
import urllib.parse
import urllib.error

from net_ssl import ssl_context

_USER_AGENT = "ImageToolbox-RunPod"

# ── which REST API to talk to ────────────────────────────────────────────────
#
# RunPod runs two REST APIs, and v1 stops serving on 2026-11-15 (410 Gone).
# **v2 is the default and v1 is the escape hatch, not the reverse**: installs do
# not update in lockstep, so a version shipped with v1 as its default would still
# be running on someone's machine after the shutoff, and a fallback path that
# nobody exercises is not a fallback but a hope. The reasoning in full is in
# docs/future-features.md #25 under "Which way the switch points".
#
# The switch is config-only (`runpod.api_version`, "v2" or "v1"), on purpose: it
# exists for the day beta churn bites, not as a thing to browse past in Settings.
# It is deliberately NOT automatic per call. Retrying a failed pod CREATE on the
# other transport can leave two billed pods behind when the first call actually
# succeeded and only its response was lost, so the app tells the user to flip the
# switch (probe_api_version) rather than flipping it silently.
#
# Everything a RESPONSE carries is read through the normalisation seam further
# down and needs no switch at all; this only decides which URL to call and which
# shape to SEND.

API_V1 = "v1"
API_V2 = "v2"

BASE_URLS = {
    API_V1: "https://rest.runpod.io/v1",
    API_V2: "https://api.runpod.io/v2",
}

# Kept as the name older code imported; it is v1's base and no longer the one in
# use by default. Prefer base_url().
BASE_URL = BASE_URLS[API_V1]

_API_VERSION = API_V2


def api_version():
    """The REST API version this process talks to ("v2" by default)."""
    return _API_VERSION


def set_api_version(value):
    """Point the client at "v1" or "v2". Anything unrecognised is IGNORED and the
    current version kept, never silently downgraded: a typo in config must not be
    able to route a run onto a transport the user did not choose (the same rule
    the GUI applies to its Run-on labels, tests/test_display_text_is_not_state)."""
    global _API_VERSION
    name = str(value or "").strip().lower()
    if name in BASE_URLS:
        _API_VERSION = name
    return _API_VERSION


def configure(cfg):
    """Apply `runpod.api_version` out of a loaded config dict. Called once per
    process by the two config loaders (runner_common.load_config for the runners,
    gui.common for the GUI) so no individual caller has to remember; this module
    still reads no file of its own."""
    if isinstance(cfg, dict):
        section = cfg.get("runpod")
        if isinstance(section, dict):
            return set_api_version(section.get("api_version"))
    return _API_VERSION


def base_url():
    return BASE_URLS[_API_VERSION]


# Endpoint paths that differ between the two. Everything else is spelled the same
# on both, so the table holds only what actually moved.
_PATHS = {
    "volumes": {API_V1: "/networkvolumes", API_V2: "/network-volumes"},
}


def _path(name, suffix=""):
    table = _PATHS.get(name)
    base = table[_API_VERSION] if table else name
    return base + suffix

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

    url = base_url() + path
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
    if exc.code == 410:
        # The retirement itself. It is the only failure here a user cannot
        # diagnose and the app can name exactly, so say the actual thing to do
        # instead of reporting a status code. RunPod announced Sunset headers but
        # does not serve them (measured), so a 410 is the real signal.
        return (f"RunPod has retired the API this version of Image Toolbox uses "
                f"(410 Gone on {base_url()}). Update the app.")
    # Surface the API's own error text. Two body shapes, read tolerantly for the
    # same reason as every other field (see the normalisation seam below): v1
    # answers {"error"|"message": "..."} while v2 answers an RFC 9457 problem
    # object, {"title","status","detail","errors":[...]}. Measured v2 bodies:
    #   400 {"title":"Bad Request","status":400,
    #        "detail":"There are no longer any instances available ..."}
    #   422 {"title":"Unprocessable Entity","status":422,
    #        "detail":"Request validation failed.",
    #        "errors":["$.action: value must be one of 'start', 'stop', ..."]}
    # Reading only `error`/`message` against v2 throws the reason away and leaves
    # the user with a bare "HTTP 400 Bad Request", which is precisely the message
    # that tells them nothing about a sold-out card.
    detail = _error_detail(exc)
    detail = f": {detail}" if detail else ""
    return f"RunPod returned HTTP {exc.code} {exc.reason}{detail}."


def _error_detail(exc):
    """The human-readable reason out of an error body, or "" if there is none."""
    try:
        body = json.loads(exc.read().decode("utf-8", "replace"))
    except Exception:                                    # noqa: BLE001
        return ""
    return error_detail(body)


def error_detail(body):
    """The reason out of a parsed error body (v1 flat, or an RFC 9457 problem
    object). The field-level `errors` list is appended when present: it is the
    part that names WHICH field v2 rejected, which is the whole point of a 422."""
    if not isinstance(body, dict):
        return ""
    text = body.get("error") or body.get("message") or body.get("detail") or ""
    if not isinstance(text, str):
        text = str(text)
    fields = body.get("errors")
    if isinstance(fields, list) and fields:
        joined = "; ".join(str(f) for f in fields if f)
        if joined:
            text = f"{text} ({joined})" if text else joined
    return text.strip()


# ── response normalisation: the one place that knows a field's spelling ──────
#
# The app talks to three transports that describe the SAME pod in three ways, and
# they stop serving on different dates (docs/future-features.md #25): REST v1
# (410 Gone on 2026-11-15), GraphQL (410 Gone in early 2027) and REST v2. The
# table below was measured by reading ONE real pod through all three, not read off
# a spec (tests/test_runpod_client.py holds those recorded payloads):
#
#     what           v1                     GraphQL                 v2
#     status         desiredStatus          desiredStatus           status
#     cost / hour    costPerHr              costPerHr               cost
#     data center    dataCenterId*          machine.dataCenterId    dataCenterId
#     ssh endpoint   publicIp+portMappings  (absent)                ssh.direct{host,port}
#     volume id      networkVolumeId        (absent)                mounts.network[].volumeId
#     gpu label      (absent)*              machine.gpuDisplayName  gpu.id
#     volume's DC    dataCenterId           n/a                     dataCenter
#     list envelope  bare list              data.myself.pods        {"pods": [...]}
#
#   * v1 returned `machine: {}` and no GPU field at all on the measured pod, which
#     is exactly why list_pods_detailed prefers GraphQL today.
#
# EVERY read goes through these accessors. A rename is then one line here instead
# of a silent None somewhere, and silent is the whole danger: this module's
# callers treat None as a decision rather than as an absence, and all three known
# cases fail toward SPENDING MONEY. A status that never reads RUNNING makes
# remote_run deploy a SECOND billed pod beside the one it already owns; a cost
# that reads None makes funds_guard's session cap accrue zero and never trip; a
# status that reads None makes runner_common.remote_pod_stopped return True
# unconditionally, so the auto-resume supervisor ends exactly the runs it exists
# to rescue.
#
# They accept every shape AT ONCE instead of switching on a configured API
# version, deliberately: a tolerant reader needs no switch to get right, so the
# version switch (when it lands) only has to decide which URL to call. The cost
# is that a genuinely absent field and a renamed one look alike here, which is
# what the recorded-payload tests exist to catch.

def unwrap_list(result, *keys):
    """Return a list from a response that may be bare or enveloped.

    v1 answers `GET /pods` with a bare JSON list; v2 answers `{"pods": [...]}`
    and `{"networkVolumes": [...]}`. Both are handled so a caller never cares."""
    if isinstance(result, dict):
        for k in keys:
            v = result.get(k)
            if isinstance(v, list):
                return v
        v = result.get("data")
        return v if isinstance(v, list) else []
    return result if isinstance(result, list) else []


def pod_state(pod):
    """A pod's lifecycle state (RUNNING / EXITED / TERMINATED / ...), or None.

    None means "could not be read", never "stopped" | callers that conflate the
    two are the auto-resume bug described above."""
    if not isinstance(pod, dict):
        return None
    return pod.get("desiredStatus") or pod.get("status")


def pod_cost(pod):
    """A pod's real billed $/hour, or None if the payload does not carry it.

    This is what funds_guard's session cap accrues against, so a None here is a
    cap that never trips: prefer reporting the absence to defaulting to 0."""
    if not isinstance(pod, dict):
        return None
    cost = pod.get("costPerHr")
    return pod.get("cost") if cost is None else cost


def pod_data_center(pod):
    """The data center id a pod runs in, or None."""
    if not isinstance(pod, dict):
        return None
    machine = pod.get("machine") if isinstance(pod.get("machine"), dict) else {}
    return (pod.get("dataCenterId") or machine.get("dataCenterId")
            or machine.get("location") or None)


def pod_gpu(pod):
    """A human-readable GPU label for a pod, or None.

    The label is TRANSPORT-DEPENDENT and the tests assert that rather than
    pretending otherwise: for one measured pod, GraphQL said "RTX PRO 4500" while
    v2 said "NVIDIA RTX PRO 4500 Blackwell". Both name the same card, neither is
    an id to match on, and only v2's happens to be the value a deploy expects."""
    if not isinstance(pod, dict):
        return None
    gpu = pod.get("gpu")
    if isinstance(gpu, dict) and gpu.get("id"):
        return gpu.get("id")
    if isinstance(gpu, str) and gpu:
        return gpu
    machine = pod.get("machine") if isinstance(pod.get("machine"), dict) else {}
    gpu_obj = gpu if isinstance(gpu, dict) else {}
    return (pod.get("gpuTypeId") or machine.get("gpuDisplayName")
            or gpu_obj.get("displayName") or None)


def pod_gpu_count(pod):
    """How many GPUs a pod has, or None."""
    if not isinstance(pod, dict):
        return None
    gpu = pod.get("gpu")
    if isinstance(gpu, dict) and gpu.get("count") is not None:
        return gpu.get("count")
    return pod.get("gpuCount")


def pod_ssh(pod):
    """Return (host, port) for DIRECT-TCP SSH into a pod, or (None, None).

    Direct TCP only, on purpose: the app pushes files and tunnels ports with its
    own key, and v2's other option (`ssh.proxy`, via ssh.runpod.io) is a different
    host and username with its own routing. A pod that has not published its
    port-22 mapping yet answers (None, None) and the caller keeps polling."""
    if not isinstance(pod, dict):
        return None, None
    ssh = pod.get("ssh")
    if isinstance(ssh, dict):
        direct = ssh.get("direct")
        if isinstance(direct, dict):
            host, port = direct.get("host"), direct.get("port")
            if host and port:
                return host, port
    ip = pod.get("publicIp")
    mappings = pod.get("portMappings") or {}
    port = mappings.get("22") or mappings.get(22)
    return (ip, port) if (ip and port) else (None, None)


def pod_volume_id(pod):
    """The network volume id a pod has attached, or None.

    Three spellings so far: flat `networkVolumeId` (v1), nested
    `networkVolume.id` (an older v1) and `mounts.network[].volumeId` (v2)."""
    if not isinstance(pod, dict):
        return None
    v = pod.get("networkVolumeId")
    if v:
        return v
    nv = pod.get("networkVolume")
    if isinstance(nv, dict) and nv.get("id"):
        return nv.get("id")
    mounts = pod.get("mounts")
    if isinstance(mounts, dict):
        for m in mounts.get("network") or []:
            if isinstance(m, dict) and m.get("volumeId"):
                return m.get("volumeId")
    return None


def pod_record(pod):
    """One pod as the transport-independent record the GUI and the tests use:
        {id, name, status, gpu, gpu_count, data_center, region, cost,
         ssh_host, ssh_port}
    `region` is derived locally from the data center id, never from the API."""
    if not isinstance(pod, dict):
        pod = {}
    dc = pod_data_center(pod)
    host, port = pod_ssh(pod)
    return {
        "id":          pod.get("id", ""),
        "name":        pod.get("name") or pod.get("id", ""),
        "status":      pod_state(pod) or "?",
        "gpu":         pod_gpu(pod) or "?",
        "gpu_count":   pod_gpu_count(pod),
        "data_center": dc or "?",
        "region":      region_of(dc or "") or "?",
        "cost":        pod_cost(pod),
        "ssh_host":    host,
        "ssh_port":    port,
    }


def volume_data_center(vol):
    """A network volume's data center id, or None. v1 spells it `dataCenterId`,
    v2 spells it `dataCenter` (measured on the same volume through both)."""
    if not isinstance(vol, dict):
        return None
    return vol.get("dataCenterId") or vol.get("dataCenter")


# ── pod lifecycle ────────────────────────────────────────────────────────────

def list_pods(api_key, timeout=30):
    """Return the account's pods as a list.

    Both a bare list (v1) and a {"pods": [...]} envelope (v2) come back;
    unwrap_list handles either.

    Server-side FILTERING was removed rather than ported, and that is the safer
    direction. v2 takes no status filter, and an unknown query parameter there is
    IGNORED rather than rejected (measured: `?desiredStatus=RUNNING` answers 200
    with the FULL list), so a filter that quietly stopped applying is
    indistinguishable from one that matched everything. No caller ever passed one;
    filter the returned list with pod_state instead."""
    result = _request("GET", "/pods", api_key, timeout=timeout)
    return unwrap_list(result, "pods")


def get_pod(api_key, pod_id, timeout=30):
    """Return one pod's details (its state is in `desiredStatus`)."""
    return _request("GET", f"/pods/{pod_id}", api_key, timeout=timeout)


def v2_pod_body(spec):
    """Translate the v1-REST-shaped `spec` every caller writes into a v2 create
    body, and return it.

    Rebuilt key by key rather than copied and patched, because v2 sets
    `unevaluatedProperties: false` and rejects an unknown property with a 422:
    there is no "leftover keys are harmless" any more, which is exactly what the
    old pass-through dict relied on. Verified by deploying a real pod with this
    body on 2026-08-20.

    `startSsh` is deliberately NOT set. It injects PUBLIC_KEY from the ACCOUNT's
    registered keys, and this app injects its own managed key through `env`
    instead, precisely so it never depends on (or touches) account-wide state.
    Measured: `env.PUBLIC_KEY` with startSsh omitted logs in fine."""
    spec = spec or {}
    gpu_ids = spec.get("gpuTypeIds") or []
    body = {
        "name":  spec.get("name", "image-toolbox"),
        "cloud": spec.get("cloudType", "SECURE"),
        "disk":  int(spec.get("containerDiskInGb", 30)),
        "ports": list(spec.get("ports") or ["22/tcp"]),
    }
    if spec.get("imageName"):
        body["image"] = spec["imageName"]
    if spec.get("templateId"):
        body["templateId"] = spec["templateId"]
    if gpu_ids:
        body["gpu"] = {"id": gpu_ids[0], "count": int(spec.get("gpuCount", 1))}
    if spec.get("dataCenterIds"):
        body["dataCenterIds"] = list(spec["dataCenterIds"])
    if spec.get("env"):
        body["env"] = dict(spec["env"])
    if spec.get("networkVolumeId"):
        # `path` is required, the same trap GraphQL had under the name
        # volumeMountPath: without it the container never starts.
        body["mounts"] = {"network": [{
            "volumeId": spec["networkVolumeId"],
            "path":     spec.get("volumeMountPath", "/workspace"),
        }]}
    cuda = spec.get("allowedCudaVersions")
    if cuda:
        body["gpu"] = dict(body.get("gpu") or {}, allowedCudaVersions=list(cuda))
    return body


def create_pod(api_key, spec, timeout=60):
    """Create a pod from `spec` (gpuTypeIds, imageName/templateId, ports, …) and
    return the created pod (including its new `id`). Creating a pod starts the
    billing clock — callers must own a guaranteed stop path.

    `spec` stays v1-shaped whichever transport is active; v2_pod_body translates
    it. The v1 create endpoint accepts only a CURATED GPU enum (CREATABLE_GPU_IDS)
    and 400s on newer cards (Blackwell PRO 4000/4500), which is the whole reason
    deploy_pod exists. **v2 has no such limitation** (measured: an RTX PRO 4500
    Blackwell deployed straight through POST /v2/pods), so once GraphQL goes,
    deploy_pod folds into this."""
    body = v2_pod_body(spec) if _API_VERSION == API_V2 else spec
    return _request("POST", "/pods", api_key, body=body, timeout=timeout)


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
    """Parse the CUDA version a pod image needs from its tag, or None if not
    encoded. Handles both the RunPod short form (…cu128… / …cu1281… → "12.8")
    and the official-PyTorch long form (…cuda12.4… → "12.4")."""
    m = re.search(r"cu(\d{3,4})", image or "")
    if m:
        d = m.group(1)
        return f"{d[:2]}.{d[2]}"        # '128'/'1281' -> '12.8'
    m = re.search(r"cuda(\d+)\.(\d+)", image or "")
    if m:
        return f"{m.group(1)}.{m.group(2)}"
    return None


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


def deploy_cuda_versions(spec):
    """The CUDA driver floor for a deploy, or None for no constraint.

    Applied ONLY to consumer GeForce cards. A GeForce card has no CUDA
    forward-compat, so a cu128 image won't START on a host driver below 12.8 (an
    RTX 4090 @ 12.7 failed exactly this, which is why the floor exists).
    Datacenter/pro cards DO forward-compat and run the image on older drivers, so
    a floor only hurts them: it excludes every in-stock host whose driver is older
    than the image (e.g. A100 PCIe @ 12.4-12.7 in EU-RO-1, which run cu128 fine)
    and surfaces as "no instances available" even when the console shows the card
    available. Omit it for them, matching the website deploy that works. An
    explicit spec override always wins.

    Hoisted out of deploy_pod so BOTH create paths apply the same policy: a
    transport swap must not quietly change which hosts a run can land on. v2
    offers a `gpu.minCudaVersion` that expresses ">= X" directly, which is what
    KNOWN_CUDA_VERSIONS is enumerating around, but this keeps the enumeration on
    both: identical semantics is the conservative choice for a transport change,
    and swapping the policy at the same time would make a bad landing impossible
    to attribute. That swap is #25 P4.
    """
    spec = spec or {}
    explicit = spec.get("allowedCudaVersions")
    if explicit:
        return list(explicit)
    gpu_ids = spec.get("gpuTypeIds") or []
    if is_consumer_gpu(gpu_ids[0] if gpu_ids else ""):
        return allowed_cuda_versions(spec.get("imageName", ""))
    return None


_DEPLOY_MUTATION = """
mutation Deploy($input: PodFindAndDeployOnDemandInput!) {
  podFindAndDeployOnDemand(input: $input) { id machineId costPerHr }
}
"""


def deploy_pod(api_key, spec, timeout=90):
    """Create a pod and return a dict carrying at least its `id`.

    **On v2 this is just POST /v2/pods**, which accepts the full GPU catalog.
    Measured 2026-08-20: an RTX PRO 4500 Blackwell, one of the exact cards that
    made this function necessary, deployed straight through it. So on v2 the
    GraphQL mutation below is dead weight and this is a thin wrapper over
    create_pod, returning the whole pod object rather than the mutation's three
    fields (a superset, so callers reading `id` are unaffected).

    On v1 it uses the GraphQL deploy path (`podFindAndDeployOnDemand`), the SAME
    path the RunPod console uses, because the v1 REST create enum 400s on newer
    cards (Blackwell PRO 4000/4500) that the GraphQL catalog can deploy. Two
    things v1 REST does for free that GraphQL needs spelled out: a mounted network
    volume needs an explicit `volumeMountPath` (without it the container fails
    with "field Target must not be empty" and never starts, hence no public IP),
    and `supportPublicIp` must be set so the pod gets the direct-TCP SSH endpoint
    the app relies on. v2 needs neither: `mounts.network[].path` carries the first
    and `ports: ["22/tcp"]` alone publishes the endpoint (both measured), and
    sending `supportPublicIp` there is a 422.

    `spec` stays the v1-REST-shaped dict every caller writes, on both paths.

    Like create_pod, this STARTS BILLING — callers must own a guaranteed stop
    path. The pod's SSH endpoint appears a bit later (poll via wait_until_running).
    Raises RunPodError on capacity / validation / transport failure.
    """
    if _API_VERSION == API_V2:
        cuda = deploy_cuda_versions(spec)
        if cuda:
            spec = {**spec, "allowedCudaVersions": cuda}
        return create_pod(api_key, spec, timeout=timeout)
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
    cuda = deploy_cuda_versions(spec)
    if cuda:
        inp["allowedCudaVersions"] = cuda
    data = _graphql(api_key, _DEPLOY_MUTATION, {"input": inp}, timeout=timeout)
    pod = data.get("podFindAndDeployOnDemand")
    if not isinstance(pod, dict) or not pod.get("id"):
        raise RunPodError(f"Deploy returned no pod id: {data}")
    return pod


# v1 spells each transition as its own endpoint; v2 collapses all of them into
# one action endpoint whose value is a checked enum (a wrong one is a 422 naming
# the field, measured). v2 also still has DELETE /pods/{id}, but routing every
# transition through one call keeps this to a single code path.
_V1_LIFECYCLE = {
    "start":     ("POST",   "/pods/{id}/start"),
    "stop":      ("POST",   "/pods/{id}/stop"),
    "terminate": ("DELETE", "/pods/{id}"),
}


def _pod_action(api_key, pod_id, action, timeout=60):
    """Perform one lifecycle transition on a pod, on whichever transport is
    active. Raises RunPodError on failure, like every other call here: teardown
    must be able to REPORT that it failed (ensure_stopped turns that into a
    message telling the user to stop the pod by hand), never swallow it."""
    if _API_VERSION == API_V2:
        return _request("POST", f"/pods/{pod_id}/action", api_key,
                        body={"action": action}, timeout=timeout)
    method, path = _V1_LIFECYCLE[action]
    return _request(method, path.format(id=pod_id), api_key, timeout=timeout)


def start_pod(api_key, pod_id, timeout=60):
    """Start or resume a stopped pod."""
    return _pod_action(api_key, pod_id, "start", timeout=timeout)


def stop_pod(api_key, pod_id, timeout=60):
    """Stop a running pod (it can later be started again; storage is still billed
    while stopped). For the dead-man's-switch teardown use terminate_pod."""
    return _pod_action(api_key, pod_id, "stop", timeout=timeout)


def terminate_pod(api_key, pod_id, timeout=60):
    """Terminate (delete) a pod — frees all billing. The disposable-pod teardown."""
    return _pod_action(api_key, pod_id, "terminate", timeout=timeout)


def pod_status(api_key, pod_id, timeout=30):
    """Return a pod's lifecycle state (RUNNING/EXITED/TERMINATED), or None if the
    pod is gone / unreadable. Never raises, for safe polling. Reads through
    pod_state, so it is spelled correctly on every transport."""
    try:
        pod = get_pod(api_key, pod_id, timeout=timeout)
    except RunPodError:
        return None
    return pod_state(pod)


# ── provisioning helpers (poll for ready, read SSH endpoint) ─────────────────

def ssh_endpoint(pod):
    """Return (host, port) for direct-TCP SSH into a pod, or (None, None) if the
    pod hasn't published its port-22 mapping yet. Requires the pod to have been
    created with "22/tcp" in its ports. Kept as the name the callers use; the
    field-spelling lives in pod_ssh."""
    return pod_ssh(pod)


def wait_until_running(api_key, pod_id, timeout=600, poll=5, on_status=None):
    """Poll a pod until it is RUNNING with a reachable SSH endpoint, and return
    the pod dict. Raises RunPodError on timeout or if the pod ends up
    EXITED/TERMINATED first. `on_status(status, host, port)` is called on each
    poll for UI feedback.

    Requiring BOTH is load-bearing, not belt and braces: v2 reports
    `status: "RUNNING"` in the CREATE response itself, measured ~50 s before
    `ssh.direct` was anything but null. A caller that trusted the status alone
    would be handed a host of None and fail somewhere far less obvious."""
    deadline = time.time() + timeout
    while True:
        pod = get_pod(api_key, pod_id)
        status = pod_state(pod)
        host, port = pod_ssh(pod)
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
    return volume_data_center(vol)


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
        if pod_volume_id(p) == vol_id:
            out.append({"id": p.get("id"), "name": p.get("name"),
                        "status": pod_state(p)})
    return out


# ── network volumes (the persistent model store) ─────────────────────────────

def list_network_volumes(api_key, timeout=30):
    """Return the account's network volumes (id, name, size, and a data center
    read via volume_data_center: v1 spells it dataCenterId, v2 dataCenter)."""
    result = _request("GET", _path("volumes"), api_key, timeout=timeout)
    return unwrap_list(result, "networkVolumes")


def get_network_volume(api_key, vol_id, timeout=30):
    """Return one network volume's details."""
    return _request("GET", _path("volumes", f"/{vol_id}"), api_key, timeout=timeout)


def create_network_volume(api_key, name, size_gb, data_center_id, timeout=60):
    """Create a network volume (size in GB, 1–4000) in a specific data center.

    The data center is fixed at creation and locks every pod that mounts the
    volume to that region — pass an EU id for a Europe-based user."""
    size = int(size_gb)
    # The two versions disagree on the bounds (v1 1-4000, v2 10-4096), so check
    # against the one actually being called rather than an intersection: a
    # refusal a user cannot act on is worse than one that quotes the real limit.
    low, high = (10, 4096) if _API_VERSION == API_V2 else (1, 4000)
    if not low <= size <= high:
        raise RunPodError(
            f"Network volume size must be between {low} and {high} GB.")
    if not data_center_id:
        raise RunPodError("A data center id is required to create a network volume.")
    body = {"name": name, "size": size}
    # The one renamed field in a request BODY, which is why it cannot go through
    # the read seam: v1 wants dataCenterId, v2 wants dataCenter.
    body["dataCenter" if _API_VERSION == API_V2 else "dataCenterId"] = data_center_id
    return _request("POST", _path("volumes"), api_key, body=body, timeout=timeout)


def delete_network_volume(api_key, vol_id, timeout=60):
    """Delete a network volume (frees its monthly storage charge)."""
    return _request("DELETE", _path("volumes", f"/{vol_id}"), api_key, timeout=timeout)


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


def _stock_label(value):
    """One spelling for a stock level whichever transport reported it.

    GraphQL says "Low"/"Medium"/"High"; v2's AvailabilityLevel enum says
    "LOW"/"MEDIUM"/"HIGH"/"NONE". The picker prints this string straight into a
    combobox label, so without normalising, switching transport turns the list
    shouty. NONE is an ABSENCE of stock, not a level, and becomes None so the
    existing `if not stock` filters keep working unchanged."""
    text = str(value or "").strip()
    if not text or text.upper() == "NONE":
        return None
    return text.capitalize()


def _gpus_via_graphql(api_key, data_center_id, timeout):
    """The v1-era GPU catalog: one GraphQL query with a per-DC lowestPrice."""
    data = _graphql(api_key, _GPU_AVAIL_QUERY, {"dc": data_center_id or None},
                    timeout=timeout)
    out = []
    for g in data.get("gpuTypes") or []:
        lp = g.get("lowestPrice") or {}
        out.append({
            "id":        g.get("id") or "",
            "name":      g.get("displayName") or g.get("id") or "",
            "memory_gb": g.get("memoryInGb") or 0,
            "price":     lp.get("uninterruptablePrice"),
            "stock":     _stock_label(lp.get("stockStatus")),
        })
    return out


def _gpus_via_catalog(api_key, data_center_id, timeout):
    """The v2 catalog: GET /catalog/gpus with the AVAILABILITY expansion.

    Three query parameters are load-bearing and none of them default usefully.
    `include=AVAILABILITY` is what adds the availability fields at all (without
    it the catalog is a price list: measured, no `dataCenters`, no `availability`,
    no `cudaVersions`, which is NOT what the migration guide describes).
    `product=POD` is REQUIRED alongside it and has no default, deliberately, since
    the same card can be scarce for pods and plentiful for serverless. `cloud`
    defaults to SECURE upstream but is sent explicitly, because every pod this app
    deploys is Secure Cloud and a silently changed default would quote community
    prices for capacity the app never uses.

    Availability is per data center. A card with no entry for `data_center_id`
    simply is not offered there, which reads as no stock, exactly as GraphQL's
    empty stockStatus did."""
    items = unwrap_list(_request("GET", "/catalog/gpus", api_key, params={
        "include": "AVAILABILITY", "product": "POD", "cloud": "SECURE",
    }, timeout=timeout), "gpus")
    out = []
    for g in items:
        if not isinstance(g, dict):
            continue
        if data_center_id:
            here = next((d for d in g.get("dataCenters") or []
                         if isinstance(d, dict) and d.get("id") == data_center_id), None)
            stock = here.get("availability") if here else None
        else:
            stock = g.get("availability")
        out.append({
            "id":        g.get("id") or "",
            "name":      g.get("name") or g.get("id") or "",
            "memory_gb": g.get("memory") or 0,
            "price":     (g.get("price") or {}).get("secure"),
            "stock":     _stock_label(stock),
        })
    return out


def available_gpus(api_key, data_center_id=None, min_memory_gb=0, timeout=30,
                   include_out_of_stock=False):
    """Return the secure-cloud GPUs that are DEPLOYABLE RIGHT NOW, newest data.

    Asks whichever catalog the active transport owns (v2's `/catalog/gpus` with
    the AVAILABILITY expansion, or GraphQL's `gpuTypes` on v1) for every GPU
    type's live price and stock in `data_center_id` (None = RunPod's global
    view), keeps only those with stock and at least `min_memory_gb` of VRAM, and
    returns them sorted by hourly price ascending. Each item is a dict:
        {"id", "name", "memory_gb", "price", "stock"}
    where `id` is the value a deploy's GPU field expects. Raises RunPodError on
    failure (callers run this off a background thread and show the message).

    The two sources were compared side by side on EU-RO-1 (2026-08-20) and agreed
    exactly: the same 7 NVIDIA cards, the same prices, the same stock levels and
    the same display names. The only difference was the spelling of the level,
    which `_stock_label` settles.

    Unlike the curated GPU_TYPES / TAG_GPU_TYPES picklists, this reflects reality
    — a card the account can't actually rent in this region never appears, so the
    UI can't offer a GPU that will only fail at create time.

    AMD GPUs (Instinct MI-series, Radeon) are dropped here regardless of stock or
    price: the pipeline is CUDA-only (see `is_amd_gpu`), so an AMD card could only
    fail at run time. They're cheap enough to be tempting in some data centers, so
    filtering them at the source keeps them out of every picker. (v2's catalog is
    the more insistent of the two about offering them: it listed an MI300X for
    EU-RO-1 that GraphQL did not.)

    `include_out_of_stock=True` keeps cards with no current stock (their `stock`
    is None) — for the Settings GPU *preference* lists, which should still offer a
    card that's only momentarily sold out. The live per-run pickers leave it False.
    """
    if _API_VERSION == API_V2:
        rows = _gpus_via_catalog(api_key, data_center_id, timeout)
    else:
        rows = _gpus_via_graphql(api_key, data_center_id, timeout)
    out = []
    for r in rows:
        if is_amd_gpu(r["id"]) or is_amd_gpu(r["name"]):   # CUDA-only app
            continue
        if not r["stock"] and not include_out_of_stock:    # out of stock here
            continue
        if (r["memory_gb"] or 0) < min_memory_gb:
            continue
        out.append(r)
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


def curated_location(dc_id):
    """The human place-name for a data center id out of the curated DATACENTERS
    labels ("Romania (EU-RO-1)" -> "Romania"), or "" for one not in the list.

    v2's catalog dropped the `location` field and returns `name == id`, so
    without this the Settings picker degrades from "Romania (EU-RO-1)" to a bare
    list of codes. The API is used for MEMBERSHIP and CAPABILITY, the curated
    list for DISPLAY. A data center RunPod adds later is simply shown by its id
    until the list is regenerated, which is what an unknown one already did."""
    for label, did in DATACENTERS:
        if did == dc_id:
            return label.split(" (")[0].strip() if " (" in label else label
    return ""


def _dcs_via_graphql(api_key, storage_only, listed_only, timeout):
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
            "location": d.get("location") or curated_location(did),
            "region":   region_of(did),
            "storage":  bool(d.get("storageSupport")),
            "listed":   bool(d.get("listed")),
        })
    return out


def _dcs_via_catalog(api_key, storage_only, timeout):
    """The v2 catalog. Two fields the GraphQL version had are gone, and only one
    of them mattered.

    `storageSupport` became `networkVolumeTypes`, a list of the tiers offered, so
    "supports network volumes" is "that list is non-empty".

    `listed` has NO equivalent, and measuring settled what to do about it: v2
    returns 32 data centers where GraphQL returns 50, and **not one** of
    GraphQL's 18 unlisted data centers appears in v2 as storage-capable. The two
    storage-capable sets are identical, 18 for 18, so v2's catalog is already
    effectively the listed view and there is nothing to re-filter. `listed` is
    reported True rather than dropped, so the record shape stays the same for
    both transports."""
    items = unwrap_list(_request("GET", "/catalog/datacenters", api_key,
                                 timeout=timeout), "dataCenters")
    out = []
    for d in items:
        if not isinstance(d, dict):
            continue
        storage = bool(d.get("networkVolumeTypes"))
        if storage_only and not storage:
            continue
        did = d.get("id") or ""
        out.append({
            "id":       did,
            "location": curated_location(did),
            "region":   region_of(did),
            "storage":  storage,
            "listed":   True,
        })
    return out


def data_centers(api_key, storage_only=True, listed_only=True, timeout=30):
    """Live list of RunPod data centers. Each item:
        {"id", "location", "region", "storage", "listed"}
    With `storage_only` (default) only data centers that support network volumes
    are returned — a model volume can only live where storage exists, so this is
    exactly what the Settings picker should offer. Sorted by region (UI order)
    then id. Raises RunPodError on failure (callers run it off a thread and fall
    back to the curated DATACENTERS list).

    `region` is derived LOCALLY from the id in both cases (region_of), never read
    from the API, so the grouping cannot change under the app. `listed_only` only
    does anything on v1: see _dcs_via_catalog for why v2 needs no equivalent."""
    if _API_VERSION == API_V2:
        out = _dcs_via_catalog(api_key, storage_only, timeout)
    else:
        out = _dcs_via_graphql(api_key, storage_only, listed_only, timeout)
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


def list_pods_detailed(api_key, timeout=30):
    """Return the account's pods as pod_record dicts.

    On **v2 this is one plain GET**: `/v2/pods` already carries gpu.id,
    dataCenterId and cost, so there is nothing to enrich and no second source to
    fall back to.

    On v1 it prefers GraphQL (`myself.pods`), because the v1 pod object omits the
    GPU type and data center entirely (a measured v1 pod carried `machine: {}`
    and no GPU field at all), and falls back to REST /pods when every GraphQL
    attempt fails, so the list still works with '?' for the missing fields. Both
    of those go through the same pod_record as the v2 path, which is what the
    recorded-payload tests pin.

    The ladder below exists only because GraphQL 400s the WHOLE query on one
    unknown field. It dies with v1."""
    if _API_VERSION == API_V2:
        return [pod_record(p) for p in list_pods(api_key, timeout=timeout)
                if isinstance(p, dict)]
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
        return [pod_record(p) for p in pods if isinstance(p, dict)]
    # Every GraphQL attempt failed → REST fallback (id/name/status/cost at least).
    try:
        return [pod_record(p) for p in list_pods(api_key, timeout=timeout)
                if isinstance(p, dict)]
    except RunPodError:
        raise last_err or RunPodError("Could not list pods.")


# ── UI-facing helpers (return (ok, message), never raise) ────────────────────

def probe_api_version(api_key, timeout=15):
    """Ask both REST versions whether they answer, and return (ok, message).

    This is the escape hatch's discovery half. The app does NOT switch versions
    on its own: an automatic cross-transport retry around pod creation can leave
    two billed pods behind when the first call really succeeded and only its
    reply was lost. So when the configured version is unreachable and the other
    one answers, this says so and names the setting to change, and a human makes
    the call. Read-only (one GET each), so running it costs nothing."""
    if not api_key:
        return False, "Enter a RunPod API key first."
    current = api_version()
    results = {}
    for name in (API_V1, API_V2):
        before = set_api_version(name)
        try:
            list_pods(api_key, timeout=timeout)
            results[name] = None
        except RunPodError as exc:
            results[name] = str(exc)
        finally:
            set_api_version(before if before != name else current)
    set_api_version(current)
    other = API_V1 if current == API_V2 else API_V2
    if results[current] is None:
        extra = "" if results[other] is None else f" ({other} is not answering.)"
        return True, f"Connected on REST {current}.{extra}"
    if results[other] is None:
        return False, (
            f"REST {current} is not answering: {results[current]} "
            f"REST {other} does answer. Set \"api_version\": \"{other}\" in the "
            f"runpod section of config.json to use it.")
    return False, f"Neither REST version answered. {current}: {results[current]}"


def test_connection(api_key, timeout=15):
    """Verify the API key by listing pods. Returns (ok, message) for the
    Settings 'Test' button — mirrors mqtt_publisher.test_connection.

    The message names the REST version it used, so a user reporting a problem
    says which transport they were on without being asked (and so a run on an
    unexpected version is visible before it costs anything)."""
    if not api_key:
        return False, "Enter a RunPod API key first."
    try:
        pods = list_pods(api_key, timeout=timeout)
    except RunPodError as exc:
        return False, str(exc)
    running = sum(1 for p in pods if pod_state(p) == STATUS_RUNNING)
    n = len(pods)
    where = f"REST {api_version()}"
    if n == 0:
        return True, (f"Connected on {where}: API key is valid "
                      f"(no pods currently on the account).")
    return True, f"Connected on {where}: {n} pod(s) on the account, {running} running."


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
