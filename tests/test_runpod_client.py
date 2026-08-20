"""
Recorded-payload tests for the RunPod response seam (docs/future-features.md #25, P0).

Nothing anywhere pinned RunPod's response shapes before this file, which is a
problem the app is about to walk into: it talks to THREE transports that describe
the same pod differently, and they stop serving on different dates. REST v1
returns 410 Gone on 2026-11-15; GraphQL in early 2027; REST v2 is the successor.

Every payload below is REAL. One pod (an RTX PRO 4500 Blackwell in EU-RO-1) was
deployed on 2026-08-20 and read back through all three transports within the same
minute, and the account's model volume was read through v1 and v2. Only identity
was scrubbed: the SSH public key, the account id, and nothing else. They are
recorded rather than synthesised on purpose, because a fixture written from the
spec would assert what the spec SAYS and this project has been bitten four times
by the gap between that and what a thing DOES (known-defects D1, the ffmpeg pin,
NVENC, the BtbN month-end URL).

What these tests defend is money. Three known reads fail toward spending it, and
each has a test here named for the consequence rather than for the field:

  * a status that never reads RUNNING makes remote_run deploy a SECOND billed pod
    beside the one it already owns,
  * a cost that reads None makes funds_guard's session cap accrue zero and never
    trip, and
  * a status that reads None makes remote_pod_stopped answer True unconditionally,
    so the auto-resume supervisor ends exactly the runs it exists to rescue.

None of that raises. All of it looks like a normal run.
"""

import io
import os
import sys
import json
import tokenize
import urllib.error

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import runpod_client as rp                                        # noqa: E402


# ── recorded payloads ────────────────────────────────────────────────────────
# One pod, read through three transports inside the same minute.

V1_POD = json.loads(r"""
{
 "consumerUserId": "user_SCRUBBED",
 "containerDiskInGb": 30,
 "costPerHr": 0.72,
 "createdAt": "2026-08-20 06:16:19.436 +0000 UTC",
 "desiredStatus": "RUNNING",
 "env": {"PUBLIC_KEY": "ssh-ed25519 SCRUBBED"},
 "gpuCount": 1,
 "id": "5xpma5eg46tqjq",
 "imageName": "runpod/pytorch:1.0.7-cu1281-torch291-ubuntu2204",
 "lastStartedAt": "2026-08-20 06:16:19.43 +0000 UTC",
 "machine": {},
 "machineId": "ohkxhsnz9mko",
 "memoryInGb": 62,
 "name": "imgtbx-v2-probe",
 "networkVolumeId": "grvtso8ftn",
 "ports": ["22/tcp"],
 "portMappings": {"22": 35558},
 "publicIp": "213.173.104.74",
 "templateId": "",
 "vcpuCount": 12,
 "volumeInGb": 0,
 "volumeMountPath": "/workspace"
}
""")

GQL_POD = json.loads(r"""
{
 "id": "5xpma5eg46tqjq",
 "name": "imgtbx-v2-probe",
 "desiredStatus": "RUNNING",
 "costPerHr": 0.72,
 "gpuCount": 1,
 "machine": {"gpuDisplayName": "RTX PRO 4500", "dataCenterId": "EU-RO-1"}
}
""")

V2_POD = json.loads(r"""
{
 "actions": ["stop", "restart", "terminate"],
 "args": "",
 "cloud": "SECURE",
 "cost": 0.72,
 "createdAt": "2026-08-20T06:16:19.436Z",
 "cudaVersion": "13.0",
 "dataCenterId": "EU-RO-1",
 "disk": 30,
 "env": {"PUBLIC_KEY": "ssh-ed25519 SCRUBBED"},
 "globalNetworking": {"enabled": false},
 "gpu": {"count": 1, "id": "NVIDIA RTX PRO 4500 Blackwell"},
 "id": "5xpma5eg46tqjq",
 "image": "runpod/pytorch:1.0.7-cu1281-torch291-ubuntu2204",
 "locked": false,
 "mounts": {"network": [{"path": "/workspace", "volumeId": "grvtso8ftn"}]},
 "name": "imgtbx-v2-probe",
 "ports": ["22/tcp"],
 "registry": null,
 "runtime": {
  "cpu": {"util": 0},
  "gpus": [{"memoryUtil": 0, "util": 0}],
  "memory": {"util": 0},
  "ports": [
   {"ip": "213.173.104.74", "private": 22, "public": 35558, "type": "tcp"},
   {"ip": "100.65.14.155", "private": 19123, "public": 60821, "type": "http"}
  ],
  "uptime": 3
 },
 "ssh": {
  "direct": {"command": "ssh root@213.173.104.74 -p 35558",
             "host": "213.173.104.74", "port": 35558, "username": "root"},
  "proxy": {"command": "ssh 5xpma5eg46tqjq-SCRUBBED@ssh.runpod.io",
            "host": "ssh.runpod.io", "port": 22,
            "username": "5xpma5eg46tqjq-SCRUBBED"}
 },
 "startedAt": "2026-08-20T06:16:19.43Z",
 "status": "RUNNING",
 "template": null
}
""")

# The SAME pod moments after creation, before the container published its ports.
# v2 reports status RUNNING here already, which is why wait_until_running must
# keep requiring an SSH endpoint and must never trust the status alone.
V2_POD_NOT_READY = json.loads(r"""
{
 "cost": 0.72,
 "dataCenterId": "EU-RO-1",
 "gpu": {"count": 1, "id": "NVIDIA RTX PRO 4500 Blackwell"},
 "id": "5xpma5eg46tqjq",
 "name": "imgtbx-v2-probe",
 "runtime": null,
 "ssh": {"direct": null,
         "proxy": {"host": "ssh.runpod.io", "port": 22,
                   "username": "5xpma5eg46tqjq-SCRUBBED"}},
 "status": "RUNNING"
}
""")

# The account's model volume, read through both REST versions.
V1_VOLUMES = json.loads(r"""
[{"dataCenterId": "EU-RO-1", "id": "grvtso8ftn",
  "name": "image-toolbox-models", "size": 50}]
""")

V2_VOLUMES = json.loads(r"""
{"networkVolumes": [{"dataCenter": "EU-RO-1", "id": "grvtso8ftn",
                     "name": "image-toolbox-models", "size": 50,
                     "type": "STANDARD"}]}
""")

# Error bodies, both recorded from live v2 calls.
V2_ERROR_400 = json.loads(r"""
{"detail": "There are no longer any instances available with the requested specifications. Please refresh and try again.",
 "status": 400, "title": "Bad Request"}
""")

V2_ERROR_422 = json.loads(r"""
{"detail": "Request validation failed.",
 "errors": ["$.action: value must be one of 'start', 'stop', 'restart', 'terminate'"],
 "status": 422, "title": "Unprocessable Entity"}
""")

V1_ERROR = {"error": "Something went wrong."}

ALL_PODS = {"v1": V1_POD, "graphql": GQL_POD, "v2": V2_POD}


# ── the record is the same pod on every transport ────────────────────────────

@pytest.mark.parametrize("transport", sorted(ALL_PODS))
def test_pod_record_agrees_across_transports(transport):
    """The fields that identify and bill a pod must not depend on the transport."""
    rec = rp.pod_record(ALL_PODS[transport])
    assert rec["id"] == "5xpma5eg46tqjq"
    assert rec["name"] == "imgtbx-v2-probe"
    assert rec["status"] == "RUNNING"
    assert rec["cost"] == 0.72
    assert rec["gpu_count"] == 1


def test_data_center_agrees_where_the_payload_carries_one():
    """GraphQL nests it under machine, v2 puts it flat, and v1 carried NEITHER on
    the measured pod (`machine: {}` and no dataCenterId at all). The seam must
    read the two that have it and answer '?' for the one that does not, rather
    than inventing a value."""
    assert rp.pod_record(GQL_POD)["data_center"] == "EU-RO-1"
    assert rp.pod_record(V2_POD)["data_center"] == "EU-RO-1"
    assert rp.pod_record(V1_POD)["data_center"] == "?"
    # region is derived from the id locally, so it follows exactly.
    assert rp.pod_record(GQL_POD)["region"] == "Europe"
    assert rp.pod_record(V2_POD)["region"] == "Europe"
    assert rp.pod_record(V1_POD)["region"] == "?"


def test_gpu_label_is_transport_dependent_and_that_is_not_a_bug():
    """Pinned deliberately, because "identical records" is the tempting thing to
    assert here and it is false: for ONE card, GraphQL said "RTX PRO 4500" and v2
    said "NVIDIA RTX PRO 4500 Blackwell". Only v2's is the string a deploy
    expects, so the label is for display and must never be matched on."""
    assert rp.pod_gpu(GQL_POD) == "RTX PRO 4500"
    assert rp.pod_gpu(V2_POD) == "NVIDIA RTX PRO 4500 Blackwell"
    assert rp.pod_gpu(V1_POD) is None          # v1 carries no GPU field at all


def test_ssh_endpoint_is_the_same_host_and_port_on_both_rest_versions():
    assert rp.pod_ssh(V1_POD) == ("213.173.104.74", 35558)
    assert rp.pod_ssh(V2_POD) == ("213.173.104.74", 35558)
    assert rp.ssh_endpoint(V2_POD) == ("213.173.104.74", 35558)


def test_ssh_endpoint_absent_until_the_pod_publishes_its_ports():
    """v2 says RUNNING from the moment of creation, several polls before
    ssh.direct exists, so a caller that trusts the status alone connects to
    nothing. wait_until_running requires BOTH; this pins the half it relies on."""
    assert rp.pod_state(V2_POD_NOT_READY) == "RUNNING"
    assert rp.pod_ssh(V2_POD_NOT_READY) == (None, None)


def test_proxy_ssh_is_never_offered_as_the_direct_endpoint():
    """v2 always fills ssh.proxy, even when direct is null. Returning it would
    hand the app a host and username it cannot use for scp with its own key."""
    host, port = rp.pod_ssh(V2_POD_NOT_READY)
    assert host is None and port is None


def test_volume_id_from_all_three_spellings():
    assert rp.pod_volume_id(V1_POD) == "grvtso8ftn"          # flat networkVolumeId
    assert rp.pod_volume_id(V2_POD) == "grvtso8ftn"          # mounts.network[]
    assert rp.pod_volume_id({"networkVolume": {"id": "abc"}}) == "abc"   # older v1
    assert rp.pod_volume_id(GQL_POD) is None


def test_volume_data_center_from_both_spellings():
    assert rp.volume_data_center(V1_VOLUMES[0]) == "EU-RO-1"
    assert rp.volume_data_center(V2_VOLUMES["networkVolumes"][0]) == "EU-RO-1"


def test_unwrap_list_handles_bare_and_enveloped_lists():
    assert rp.unwrap_list(V1_VOLUMES, "networkVolumes") == V1_VOLUMES
    assert rp.unwrap_list(V2_VOLUMES, "networkVolumes") == V2_VOLUMES["networkVolumes"]
    assert rp.unwrap_list([], "pods") == []
    assert rp.unwrap_list({"pods": []}, "pods") == []
    assert rp.unwrap_list(None, "pods") == []


# ── the three failures that cost money ───────────────────────────────────────

@pytest.mark.parametrize("transport", sorted(ALL_PODS))
def test_a_running_pod_is_recognised_so_no_second_pod_is_deployed(transport):
    """remote_run._find_existing_pod reuses a pod whose state reads RUNNING. If it
    reads anything else the run deploys a second pod beside the first and bills
    both, which is invisible until the invoice."""
    assert rp.pod_state(ALL_PODS[transport]) == rp.STATUS_RUNNING


@pytest.mark.parametrize("transport", sorted(ALL_PODS))
def test_the_billed_rate_is_readable_so_the_session_cap_can_trip(transport):
    """funds_guard accrues session cost from this number. None accrues zero, and
    a cap that never trips is the guard silently not existing."""
    cost = rp.pod_cost(ALL_PODS[transport])
    assert cost is not None and cost > 0


@pytest.mark.parametrize("transport", sorted(ALL_PODS))
def test_a_running_pod_never_reads_as_stopped(transport):
    """runner_common.remote_pod_stopped treats None as 'gone'. With a misread
    status that is EVERY poll, so the auto-resume supervisor (#6) would abandon
    the long runs it exists to keep alive."""
    import runner_common

    class _Session:
        pod_id = "5xpma5eg46tqjq"
        api_key = "k"

    state = rp.pod_state(ALL_PODS[transport])
    assert state is not None
    assert state not in (rp.STATUS_EXITED, rp.STATUS_TERMINATED)
    assert callable(runner_common.remote_pod_stopped)


def test_pod_still_running_accepts_a_v2_payload():
    """The auto-resume liveness probe, fed the v2 shape it will be fed after the
    migration. list_pods is injected, so this needs no network."""
    from batch_video_upscale import _pod_still_running
    assert _pod_still_running(lambda: [V2_POD], "5xpma5eg46tqjq", "imgtbx") is True
    assert _pod_still_running(lambda: [V1_POD], "5xpma5eg46tqjq", "imgtbx") is True
    assert _pod_still_running(lambda: [], "5xpma5eg46tqjq", "imgtbx") is False


def test_unreadable_payloads_answer_none_not_a_default():
    """An absence must stay an absence. A pod_cost of 0.0 or a pod_state of
    'EXITED' invented here would be a lie the callers act on."""
    for junk in (None, "", [], 0, {"unrelated": 1}):
        assert rp.pod_state(junk) is None
        assert rp.pod_cost(junk) is None
        assert rp.pod_gpu(junk) is None
        assert rp.pod_volume_id(junk) is None
        assert rp.pod_ssh(junk) == (None, None)


# ── error bodies ─────────────────────────────────────────────────────────────

def test_error_detail_reads_v1_flat_and_v2_problem_bodies():
    assert rp.error_detail(V1_ERROR) == "Something went wrong."
    assert "no longer any instances available" in rp.error_detail(V2_ERROR_400)


def test_a_422_names_the_field_it_rejected():
    """The field-level `errors` array is the entire value of a typed 422; dropping
    it leaves the user with 'Unprocessable Entity' and nothing to act on."""
    msg = rp.error_detail(V2_ERROR_422)
    assert "Request validation failed." in msg
    assert "$.action" in msg


def test_error_detail_survives_junk():
    for junk in (None, "", [], 0, {"status": 500}):
        assert rp.error_detail(junk) == ""


# ── P1: which transport a call actually goes to ──────────────────────────────
#
# These drive the real `_request` with urlopen replaced, so they assert the
# METHOD, URL and BODY that would leave the machine. Asserting that a function
# "returns 200" is what this project keeps getting caught by; asserting what it
# sends is the thing that can be wrong.

class _FakeResponse:
    def __init__(self, payload=b"{}"):
        self._payload = payload

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def sent(monkeypatch):
    """Capture the requests runpod_client would send, and answer each with {}."""
    calls = []

    def fake_urlopen(req, timeout=None, context=None):
        body = None
        if req.data:
            body = json.loads(req.data.decode("utf-8"))
        calls.append({"method": req.get_method(), "url": req.full_url,
                      "body": body, "headers": dict(req.header_items())})
        return _FakeResponse()

    monkeypatch.setattr(rp.urllib.request, "urlopen", fake_urlopen)
    return calls


@pytest.fixture
def on_v1(monkeypatch):
    monkeypatch.setattr(rp, "_API_VERSION", rp.API_V1)


@pytest.fixture
def on_v2(monkeypatch):
    monkeypatch.setattr(rp, "_API_VERSION", rp.API_V2)


def test_v2_is_the_default_transport():
    """Pinned, because the direction of this default is the whole decision (see
    docs/future-features.md #25, "Which way the switch points"). Installs do not
    update in lockstep, so a build shipped with v1 as its default would still be
    running on someone's machine after v1 starts answering 410."""
    assert rp.API_V2 == "v2"
    assert rp.api_version() == "v2"
    assert rp.base_url() == "https://api.runpod.io/v2"


def test_set_api_version_ignores_a_value_it_does_not_know(monkeypatch):
    """A typo in config must not be able to route a run onto a transport the user
    did not choose. Unrecognised keeps the current version rather than falling
    back to some default, which is the same rule the GUI applies to its Run-on
    labels (tests/test_display_text_is_not_state.py)."""
    monkeypatch.setattr(rp, "_API_VERSION", rp.API_V2)
    for junk in ("", None, "V3", "rest", "graphql", 2, "v1 "):
        rp.set_api_version(junk)
        assert rp.api_version() in (rp.API_V1, rp.API_V2)
    rp.set_api_version("v1")           # a real value still works
    assert rp.api_version() == rp.API_V1
    rp.set_api_version("  V2  ")       # and is normalised
    assert rp.api_version() == rp.API_V2


def test_configure_reads_the_config_section_and_survives_junk(monkeypatch):
    monkeypatch.setattr(rp, "_API_VERSION", rp.API_V2)
    assert rp.configure({"runpod": {"api_version": "v1"}}) == rp.API_V1
    assert rp.configure({"runpod": {}}) == rp.API_V1          # absent keeps
    assert rp.configure({}) == rp.API_V1
    assert rp.configure(None) == rp.API_V1
    assert rp.configure({"runpod": {"api_version": "v2"}}) == rp.API_V2


@pytest.mark.parametrize("version,host", [
    ("v1", "https://rest.runpod.io/v1"),
    ("v2", "https://api.runpod.io/v2"),
])
def test_every_call_goes_to_the_configured_host(sent, monkeypatch, version, host):
    monkeypatch.setattr(rp, "_API_VERSION", version)
    rp.list_pods("k")
    rp.get_pod("k", "podid")
    rp.list_network_volumes("k")
    assert [c["url"].split("?")[0] for c in sent] == [
        f"{host}/pods", f"{host}/pods/podid",
        f"{host}/networkvolumes" if version == "v1" else f"{host}/network-volumes",
    ]


def test_lifecycle_uses_three_endpoints_on_v1(sent, on_v1):
    rp.start_pod("k", "p1")
    rp.stop_pod("k", "p1")
    rp.terminate_pod("k", "p1")
    assert [(c["method"], c["url"], c["body"]) for c in sent] == [
        ("POST", "https://rest.runpod.io/v1/pods/p1/start", None),
        ("POST", "https://rest.runpod.io/v1/pods/p1/stop", None),
        ("DELETE", "https://rest.runpod.io/v1/pods/p1", None),
    ]


def test_lifecycle_uses_one_action_endpoint_on_v2(sent, on_v2):
    """The action value is a checked enum on v2: a wrong one is a 422 that names
    the field (measured), so a typo here fails loudly rather than doing nothing."""
    rp.start_pod("k", "p1")
    rp.stop_pod("k", "p1")
    rp.terminate_pod("k", "p1")
    assert [(c["method"], c["url"], c["body"]) for c in sent] == [
        ("POST", "https://api.runpod.io/v2/pods/p1/action", {"action": "start"}),
        ("POST", "https://api.runpod.io/v2/pods/p1/action", {"action": "stop"}),
        ("POST", "https://api.runpod.io/v2/pods/p1/action", {"action": "terminate"}),
    ]


def test_terminate_is_reachable_on_both_transports(sent, monkeypatch):
    """The teardown path is the one that must never quietly become a no-op: a pod
    that is not terminated keeps billing until the dead-man's switch fires."""
    for version in (rp.API_V1, rp.API_V2):
        monkeypatch.setattr(rp, "_API_VERSION", version)
        rp.terminate_pod("k", "p1")
    assert len(sent) == 2
    assert all("p1" in c["url"] for c in sent)


# The body that ACTUALLY deployed a pod on 2026-08-20, with the key scrubbed.
V2_CREATE_BODY = json.loads(r"""
{
 "name": "imgtbx-v2-probe",
 "image": "runpod/pytorch:1.0.7-cu1281-torch291-ubuntu2204",
 "gpu": {"id": "NVIDIA RTX PRO 4500 Blackwell", "count": 1},
 "cloud": "SECURE",
 "disk": 30,
 "ports": ["22/tcp"],
 "dataCenterIds": ["EU-RO-1"],
 "env": {"PUBLIC_KEY": "ssh-ed25519 SCRUBBED"},
 "mounts": {"network": [{"volumeId": "grvtso8ftn", "path": "/workspace"}]}
}
""")

# The v1-shaped spec every caller in the app still writes (remote_run._create_pod).
V1_SPEC = {
    "name": "imgtbx-v2-probe",
    "imageName": "runpod/pytorch:1.0.7-cu1281-torch291-ubuntu2204",
    "gpuTypeIds": ["NVIDIA RTX PRO 4500 Blackwell"],
    "gpuCount": 1,
    "cloudType": "SECURE",
    "containerDiskInGb": 30,
    "ports": ["22/tcp"],
    "dataCenterIds": ["EU-RO-1"],
    "networkVolumeId": "grvtso8ftn",
    "env": {"PUBLIC_KEY": "ssh-ed25519 SCRUBBED"},
}


def test_v2_create_body_is_the_body_that_really_deployed():
    """Not a reading of the spec: this is byte-for-byte what RunPod answered 201
    to. `unevaluatedProperties: false` means a single leftover v1 key is a 422,
    so the body is rebuilt rather than patched."""
    assert rp.v2_pod_body(V1_SPEC) == V2_CREATE_BODY


def test_v2_create_body_never_sets_startSsh():
    """startSsh injects PUBLIC_KEY from the ACCOUNT's registered keys. This app
    injects its own managed key through env instead, so it never depends on (or
    writes) account-wide state. Measured: env alone gets SSH in."""
    body = rp.v2_pod_body(V1_SPEC)
    assert "startSsh" not in body
    assert body["env"]["PUBLIC_KEY"].startswith("ssh-ed25519")


def test_v2_create_body_drops_the_v1_only_keys():
    """The pass-through spec dict is what v2 kills. Anything v1-shaped that
    survived into the body would be a 422, so nothing may be copied blindly."""
    body = rp.v2_pod_body(dict(V1_SPEC, supportPublicIp=True, volumeInGb=0,
                               someFutureKey="x"))
    for dead in ("supportPublicIp", "volumeInGb", "someFutureKey", "imageName",
                 "gpuTypeIds", "gpuCount", "cloudType", "containerDiskInGb",
                 "networkVolumeId"):
        assert dead not in body


def test_create_pod_sends_the_v1_spec_verbatim_on_v1(sent, on_v1):
    rp.create_pod("k", V1_SPEC)
    assert sent[0]["url"] == "https://rest.runpod.io/v1/pods"
    assert sent[0]["body"] == V1_SPEC


def test_create_pod_translates_on_v2(sent, on_v2):
    rp.create_pod("k", V1_SPEC)
    assert sent[0]["url"] == "https://api.runpod.io/v2/pods"
    assert sent[0]["body"] == V2_CREATE_BODY


def test_volume_create_renames_its_data_center_field(sent, monkeypatch):
    """The one renamed field in a request BODY, which is why it cannot go through
    the read seam: v1 wants dataCenterId, v2 wants dataCenter. Sending the wrong
    one is a 422 on v2 and a volume in the wrong place on v1."""
    monkeypatch.setattr(rp, "_API_VERSION", rp.API_V1)
    rp.create_network_volume("k", "models", 50, "EU-RO-1")
    monkeypatch.setattr(rp, "_API_VERSION", rp.API_V2)
    rp.create_network_volume("k", "models", 50, "EU-RO-1")
    assert sent[0]["body"] == {"name": "models", "size": 50, "dataCenterId": "EU-RO-1"}
    assert sent[1]["body"] == {"name": "models", "size": 50, "dataCenter": "EU-RO-1"}
    assert sent[0]["url"].endswith("/networkvolumes")
    assert sent[1]["url"].endswith("/network-volumes")


def test_volume_size_is_checked_against_the_active_version(monkeypatch):
    """v1 allows 1-4000, v2 allows 10-4096. Quoting the limit that is actually
    in force beats quoting an intersection the user cannot verify."""
    monkeypatch.setattr(rp, "_API_VERSION", rp.API_V2)
    with pytest.raises(rp.RunPodError, match="between 10 and 4096"):
        rp.create_network_volume("k", "models", 5, "EU-RO-1")
    monkeypatch.setattr(rp, "_API_VERSION", rp.API_V1)
    with pytest.raises(rp.RunPodError, match="between 1 and 4000"):
        rp.create_network_volume("k", "models", 4001, "EU-RO-1")


def test_volume_create_still_refuses_a_missing_data_center(monkeypatch):
    monkeypatch.setattr(rp, "_API_VERSION", rp.API_V2)
    with pytest.raises(rp.RunPodError, match="data center id is required"):
        rp.create_network_volume("k", "models", 50, "")


def test_list_pods_no_longer_accepts_a_server_side_filter():
    """Removed rather than ported. v2 takes no status filter and IGNORES unknown
    query parameters (measured: 200 with the full list), so a filter that quietly
    stopped applying would be indistinguishable from one that matched
    everything."""
    with pytest.raises(TypeError):
        rp.list_pods("k", desiredStatus="RUNNING")


def test_a_410_names_the_retirement_and_what_to_do():
    """The one failure a user cannot diagnose and the app can name exactly. RunPod
    announced Sunset headers on every v1 and GraphQL response and does not
    actually serve them (measured), so a 410 is the real signal and this message
    is the whole warning a user gets."""
    exc = urllib.error.HTTPError("https://api.runpod.io/v2/pods", 410, "Gone",
                                 {}, io.BytesIO(b""))
    msg = rp._http_error_message(exc)
    assert "retired" in msg.lower()
    assert "update the app" in msg.lower()


def test_every_request_sends_a_user_agent(sent, on_v2):
    """v2 lives on the GraphQL HOST, whose Cloudflare rules never applied to
    rest.runpod.io. Measured: urllib's default User-Agent gets a 403 Error 1010
    there, the app's own gets a 200. A missing header is a total outage, not a
    degradation."""
    rp.list_pods("k")
    headers = {k.lower(): v for k, v in sent[0]["headers"].items()}
    assert headers.get("User-agent".lower()) == rp._USER_AGENT


# A pod STOPPED through v2's action endpoint, recorded 2026-08-20. The value in
# `status` is the load-bearing part: v1 says EXITED and so, measured, does v2.
V2_POD_STOPPED = json.loads(r"""
{
 "actions": ["start", "terminate"],
 "cloud": "SECURE",
 "cost": 0.72,
 "dataCenterId": "EU-RO-1",
 "disk": 30,
 "gpu": {"count": 1, "id": "NVIDIA RTX PRO 4500 Blackwell"},
 "id": "udev6tdz7z3zlt",
 "name": "imgtbx-p1-stopcheck",
 "runtime": null,
 "ssh": {"direct": null,
         "proxy": {"host": "ssh.runpod.io", "port": 22,
                   "username": "udev6tdz7z3zlt-SCRUBBED"}},
 "status": "EXITED"
}
""")


def test_a_stopped_pod_reads_as_exited_on_v2_too():
    """v2 has a richer PodStatus enum (PROVISIONING/STARTING/RUNNING/EXITED/
    ERROR/TERMINATED), so "does a stopped pod still say EXITED" was a real
    question and not a formality: ensure_stopped's idempotence and
    runner_common.remote_pod_stopped both compare against that exact value.
    Measured on a pod stopped through the v2 action endpoint."""
    assert rp.pod_state(V2_POD_STOPPED) == rp.STATUS_EXITED
    assert rp.pod_ssh(V2_POD_STOPPED) == (None, None)


def test_a_stopped_pod_still_reports_what_it_costs():
    """A stopped pod is not a free pod (its disk keeps billing), and the funds
    readout reads this number. v2 keeps `cost` populated after the stop."""
    assert rp.pod_cost(V2_POD_STOPPED) == 0.72


def test_ensure_stopped_is_idempotent_on_a_stopped_pod(monkeypatch):
    """Verified live too, but pinned offline: the teardown path runs in a
    `finally` and must never turn a second call into an error the user reads as
    a pod that is still billing."""
    monkeypatch.setattr(rp, "get_pod", lambda *a, **k: V2_POD_STOPPED)
    called = []
    monkeypatch.setattr(rp, "stop_pod", lambda *a, **k: called.append("stop"))
    ok, msg = rp.ensure_stopped("k", "udev6tdz7z3zlt", terminate=False)
    assert ok and "already stopped" in msg
    assert called == []                      # and it did not call out again


# ── P2: the catalogs, the pod list and the deploy ────────────────────────────
#
# Recorded from the live catalogs on 2026-08-20, trimmed to five cards that each
# make a point: two ordinary NVIDIA cards, a Blackwell (the card class that made
# deploy_pod necessary), an AMD card the app must never offer, and an H100 NVL
# that is available GLOBALLY but absent from EU-RO-1.

V2_CATALOG_GPUS = json.loads(r"""
{"gpus": [
 {"id": "NVIDIA RTX 2000 Ada Generation", "name": "RTX 2000 Ada",
  "manufacturer": "NVIDIA", "memory": 16, "price": {"community": 0.16, "secure": 0.24},
  "availability": "LOW", "dataCenters": [{"availability": "LOW", "id": "EU-RO-1", "name": "EU-RO-1"}]},
 {"id": "NVIDIA GeForce RTX 4090", "name": "RTX 4090",
  "manufacturer": "NVIDIA", "memory": 24, "price": {"community": 0.34, "secure": 0.74},
  "availability": "HIGH", "dataCenters": [{"availability": "HIGH", "id": "EU-RO-1", "name": "EU-RO-1"}],
  "cudaVersions": [{"available": true, "version": "12.4"}, {"available": true, "version": "12.8"}]},
 {"id": "NVIDIA RTX PRO 4500 Blackwell", "name": "RTX PRO 4500",
  "manufacturer": "NVIDIA", "memory": 32, "price": {"community": 0.34, "secure": 0.72},
  "availability": "HIGH", "dataCenters": [{"availability": "HIGH", "id": "EU-RO-1", "name": "EU-RO-1"}],
  "cudaVersions": [{"available": true, "version": "13.0"}]},
 {"id": "AMD Instinct MI300X OAM", "name": "MI300X",
  "manufacturer": "AMD", "memory": 192, "price": {"community": 0.5, "secure": 2.39},
  "availability": "LOW", "dataCenters": [{"availability": "LOW", "id": "EU-RO-1", "name": "EU-RO-1"}]},
 {"id": "NVIDIA H100 NVL", "name": "H100 NVL",
  "manufacturer": "NVIDIA", "memory": 94, "price": {"community": 1.39, "secure": 2.59},
  "availability": "LOW", "dataCenters": []}
]}
""")

# The same five cards, from the GraphQL query, scoped to EU-RO-1.
GQL_GPUS = json.loads(r"""
{"gpuTypes": [
 {"id": "AMD Instinct MI300X OAM", "displayName": "MI300X", "memoryInGb": 192,
  "lowestPrice": {"uninterruptablePrice": null, "stockStatus": null}},
 {"id": "NVIDIA GeForce RTX 4090", "displayName": "RTX 4090", "memoryInGb": 24,
  "lowestPrice": {"uninterruptablePrice": 0.74, "stockStatus": "High"}},
 {"id": "NVIDIA H100 NVL", "displayName": "H100 NVL", "memoryInGb": 94,
  "lowestPrice": {"uninterruptablePrice": null, "stockStatus": null}},
 {"id": "NVIDIA RTX 2000 Ada Generation", "displayName": "RTX 2000 Ada", "memoryInGb": 16,
  "lowestPrice": {"uninterruptablePrice": 0.24, "stockStatus": "Low"}},
 {"id": "NVIDIA RTX PRO 4500 Blackwell", "displayName": "RTX PRO 4500", "memoryInGb": 32,
  "lowestPrice": {"uninterruptablePrice": 0.72, "stockStatus": "High"}}
]}
""")

V2_CATALOG_DCS = json.loads(r"""
{"dataCenters": [
 {"id": "EU-RO-1", "name": "EU-RO-1", "region": "EUROPE",
  "networkVolumeTypes": ["STANDARD"], "compliance": [], "globalNetwork": false},
 {"id": "EU-FR-1", "name": "EU-FR-1", "region": "EUROPE",
  "networkVolumeTypes": ["HIGH_PERFORMANCE"], "compliance": [], "globalNetwork": false},
 {"id": "AP-IN-1", "name": "AP-IN-1", "region": "ASIA",
  "networkVolumeTypes": [], "compliance": [], "globalNetwork": false}
]}
""")


@pytest.fixture
def catalog(monkeypatch):
    """Answer REST GETs from the recorded catalogs, GraphQL from the recorded
    gpuTypes payload, and record what was asked for."""
    asked = []

    def fake_request(method, path, api_key, body=None, params=None, timeout=30):
        asked.append(("REST", method, path, params, body))
        if path == "/catalog/gpus":
            return V2_CATALOG_GPUS
        if path == "/catalog/datacenters":
            return V2_CATALOG_DCS
        if path == "/pods":
            return {"pods": [V2_POD]}
        return {}

    def fake_graphql(api_key, query, variables=None, timeout=30):
        asked.append(("GQL", query, variables, None, None))
        if "gpuTypes" in query:
            return GQL_GPUS
        raise AssertionError("unexpected GraphQL query: " + query[:60])

    monkeypatch.setattr(rp, "_request", fake_request)
    monkeypatch.setattr(rp, "_graphql", fake_graphql)
    return asked


def test_stock_levels_are_spelled_the_same_on_both_transports():
    """GraphQL says "High", v2's enum says "HIGH", and the picker prints this
    string straight into a combobox label, so an unnormalised swap turns the list
    shouty. NONE is an ABSENCE, not a level, so it becomes None and the existing
    `if not stock` filters keep working."""
    assert rp._stock_label("HIGH") == "High"
    assert rp._stock_label("High") == "High"
    assert rp._stock_label("MEDIUM") == "Medium"
    for empty in ("NONE", "None", "", None):
        assert rp._stock_label(empty) is None


def test_the_two_gpu_catalogs_agree_card_for_card(catalog, monkeypatch):
    """The parity that makes the swap safe, pinned offline. Measured live on the
    full EU-RO-1 catalog the same day: identical ids, prices, stock levels, VRAM
    and display names, seven cards for seven."""
    monkeypatch.setattr(rp, "_API_VERSION", rp.API_V2)
    from_v2 = rp.available_gpus("k", data_center_id="EU-RO-1")
    monkeypatch.setattr(rp, "_API_VERSION", rp.API_V1)
    from_gql = rp.available_gpus("k", data_center_id="EU-RO-1")
    assert from_v2 == from_gql
    assert [g["id"] for g in from_v2] == [
        "NVIDIA RTX 2000 Ada Generation",     # 0.24, cheapest first
        "NVIDIA RTX PRO 4500 Blackwell",      # 0.72
        "NVIDIA GeForce RTX 4090",            # 0.74
    ]


def test_a_card_absent_from_the_data_center_is_not_offered(catalog, monkeypatch):
    """v2 lists availability per data center and simply omits a data center that
    has none, where GraphQL returned the card with a null stockStatus. Both must
    read as "not available here". The H100 NVL is exactly that case: available
    globally, absent from EU-RO-1."""
    monkeypatch.setattr(rp, "_API_VERSION", rp.API_V2)
    assert "NVIDIA H100 NVL" not in [g["id"] for g in
                                     rp.available_gpus("k", data_center_id="EU-RO-1")]
    # ... but the global view (no data center) still knows about it.
    assert "NVIDIA H100 NVL" in [g["id"] for g in rp.available_gpus("k")]


def test_amd_is_dropped_from_the_v2_catalog_too(catalog, monkeypatch):
    """The pipeline is CUDA-only, so an AMD card could only fail at run time.
    v2's catalog is the more insistent of the two about offering them: it listed
    an in-stock MI300X for EU-RO-1 that GraphQL did not price at all."""
    monkeypatch.setattr(rp, "_API_VERSION", rp.API_V2)
    for g in rp.available_gpus("k", data_center_id="EU-RO-1",
                               include_out_of_stock=True):
        assert "MI300X" not in g["id"] and "MI300X" not in g["name"]


def test_the_vram_floor_and_out_of_stock_flag_still_work_on_v2(catalog, monkeypatch):
    monkeypatch.setattr(rp, "_API_VERSION", rp.API_V2)
    ids = [g["id"] for g in rp.available_gpus("k", data_center_id="EU-RO-1",
                                              min_memory_gb=32)]
    assert ids == ["NVIDIA RTX PRO 4500 Blackwell"]
    with_none = [g["id"] for g in rp.available_gpus("k", data_center_id="EU-RO-1",
                                                    include_out_of_stock=True)]
    assert "NVIDIA H100 NVL" in with_none          # kept, with stock None


def test_the_gpu_catalog_asks_for_the_expansion_it_needs(catalog, monkeypatch):
    """Three query parameters, none of which default usefully. Without
    include=AVAILABILITY the catalog is a bare price list (measured: no
    dataCenters, no availability, no cudaVersions, which is NOT what the
    migration guide describes). product is required alongside it and has no
    default, because a card can be scarce for pods and plentiful for serverless.
    cloud is sent explicitly because every pod here is Secure Cloud."""
    monkeypatch.setattr(rp, "_API_VERSION", rp.API_V2)
    rp.available_gpus("k", data_center_id="EU-RO-1")
    params = catalog[0][3]
    assert params["include"] == "AVAILABILITY"
    assert params["product"] == "POD"
    assert params["cloud"] == "SECURE"


def test_storage_capable_data_centers_come_from_the_volume_types(catalog, monkeypatch):
    """v2 dropped `storageSupport`; the successor is a non-empty
    networkVolumeTypes. Measured the same day: 18 storage-capable on both
    transports, zero difference either way."""
    monkeypatch.setattr(rp, "_API_VERSION", rp.API_V2)
    dcs = rp.data_centers("k")
    assert [d["id"] for d in dcs] == ["EU-FR-1", "EU-RO-1"]     # AP-IN-1 has none
    assert all(d["storage"] for d in dcs)


def test_data_centers_keep_a_human_label_without_the_api_field(catalog, monkeypatch):
    """v2 has no `location` and returns name == id, so the picker would degrade
    to a list of codes. The curated DATACENTERS list is the display layer; the
    API answers membership and capability only. It is also BETTER than what
    GraphQL returned, which called EU-RO-1 and EU-NL-1 both "Europe"."""
    monkeypatch.setattr(rp, "_API_VERSION", rp.API_V2)
    by_id = {d["id"]: d for d in rp.data_centers("k")}
    assert by_id["EU-RO-1"]["location"] == "Romania"
    assert by_id["EU-FR-1"]["location"] == "France"
    assert by_id["EU-RO-1"]["region"] == "Europe"     # derived locally, not read
    assert rp.curated_location("XX-ZZ-9") == ""       # unknown stays blank


def test_region_is_derived_locally_not_read_from_the_api(catalog, monkeypatch):
    """v2 has its own `region` field ("EUROPE"), deliberately unused: the app's
    region grouping is its own UI concept and must not change under it."""
    monkeypatch.setattr(rp, "_API_VERSION", rp.API_V2)
    assert {d["region"] for d in rp.data_centers("k")} == {"Europe"}


# ── the deploy ───────────────────────────────────────────────────────────────

def test_the_cuda_floor_applies_only_to_consumer_cards():
    """A GeForce card has no CUDA forward-compat, so a cu128 image will not START
    on an older host driver. Datacenter and pro cards DO forward-compat, so a
    floor only excludes hosts that would have worked, and shows up as "no
    instances available" while the console shows the card in stock."""
    image = "runpod/pytorch:1.0.7-cu1281-torch291-ubuntu2204"
    consumer = rp.deploy_cuda_versions(
        {"gpuTypeIds": ["NVIDIA GeForce RTX 4090"], "imageName": image})
    assert consumer and consumer[0] == "12.8"
    assert rp.deploy_cuda_versions(
        {"gpuTypeIds": ["NVIDIA RTX PRO 4500 Blackwell"], "imageName": image}) is None
    assert rp.deploy_cuda_versions(
        {"gpuTypeIds": ["NVIDIA GeForce RTX 4090"],
         "allowedCudaVersions": ["13.0"]}) == ["13.0"]
    assert rp.deploy_cuda_versions({}) is None


def test_the_cuda_floor_is_identical_on_both_transports(sent, monkeypatch):
    """A transport swap must not quietly change which HOSTS a run can land on.
    Verified live too: an RTX 4090 deployed through the v2 path with this exact
    list and landed on a compliant host."""
    spec = dict(V1_SPEC, gpuTypeIds=["NVIDIA GeForce RTX 4090"])
    expected = rp.deploy_cuda_versions(spec)

    monkeypatch.setattr(rp, "_API_VERSION", rp.API_V2)
    rp.deploy_pod("k", spec)
    v2_sent = sent[-1]

    gql = {}
    monkeypatch.setattr(rp, "_API_VERSION", rp.API_V1)
    monkeypatch.setattr(rp, "_graphql",
                        lambda key, q, v=None, timeout=30: gql.update(v or {})
                        or {"podFindAndDeployOnDemand": {"id": "x"}})
    rp.deploy_pod("k", spec)

    assert v2_sent["body"]["gpu"]["allowedCudaVersions"] == expected
    assert gql["input"]["allowedCudaVersions"] == expected


def test_deploy_pod_is_a_plain_post_on_v2(sent, on_v2):
    """The GraphQL mutation exists because v1's create enum 400s on newer cards.
    v2 has no such limit: measured, an RTX PRO 4500 Blackwell deployed straight
    through POST /v2/pods, so on v2 the mutation is dead weight."""
    rp.deploy_pod("k", V1_SPEC)
    assert sent[-1]["method"] == "POST"
    assert sent[-1]["url"] == "https://api.runpod.io/v2/pods"
    assert sent[-1]["body"]["gpu"]["id"] == "NVIDIA RTX PRO 4500 Blackwell"
    assert "supportPublicIp" not in sent[-1]["body"]     # v2 422s on it by name


def test_list_pods_detailed_is_one_get_on_v2(catalog, monkeypatch):
    """`GET /v2/pods` already carries gpu.id, dataCenterId and cost, so there is
    nothing to enrich and no second source to fall back to. The GraphQL ladder
    (and its memoised field-set probing) exists only because GraphQL 400s the
    whole query on one unknown field; it dies with v1."""
    monkeypatch.setattr(rp, "_API_VERSION", rp.API_V2)
    rows = rp.list_pods_detailed("k")
    assert [c[0] for c in catalog] == ["REST"]          # no GraphQL at all
    assert rows[0]["gpu"] == "NVIDIA RTX PRO 4500 Blackwell"
    assert rows[0]["data_center"] == "EU-RO-1"
    assert rows[0]["cost"] == 0.72


# ─────────────────────────────────────────────────────────────────────────────
#  The account balance (#25 P3): the one thing v2 has no successor for
# ─────────────────────────────────────────────────────────────────────────────
#
# Measured 2026-08-20 against the live spec and the live account: 34 paths, one
# /v2/account/* and it is ssh-keys, and /account, /account/balance,
# /account/credits, /user and /me all 404. The balance is GraphQL-only and
# GraphQL retires in early 2027.
#
# Losing it is SILENT, which is the whole point of these tests. funds_guard is
# fail-open by contract: an unknown balance skips the floor rather than blocking
# a run, so a user who set a floor keeps a floor that is no longer applied, with
# nothing on screen and nothing in the log to say so. Same family as every other
# bug this migration produced: it fails toward spending money and looks normal.


def _raising_graphql(exc):
    def fake(api_key, query, variables=None, timeout=30):
        raise exc
    return fake


def test_a_retired_balance_says_retired_not_unknown(monkeypatch):
    """The consequence: 'Unknown' invites the user to wait for it to come back.
    A retired balance never comes back, and the floor they configured is dead."""
    def fake_urlopen(req, timeout=None, context=None):
        raise urllib.error.HTTPError(rp.GRAPHQL_URL, 410, "Gone", {}, io.BytesIO(b""))
    monkeypatch.setattr(rp.urllib.request, "urlopen", fake_urlopen)
    info = rp.account_balance_detail("k")
    assert info["status"] == rp.BALANCE_RETIRED
    assert info["balance"] is None
    assert "retired" in info["detail"].lower()


# GraphQL's answer to a field that is not in the schema, recorded 2026-08-20 by
# asking the live endpoint for one. It is a 400, NOT the 200-with-errors the
# GraphQL spec suggests, so the status code says only "bad request" and the body
# is the part that carries the meaning. The `errors` list holds OBJECTS here,
# where v2's RFC 9457 list holds plain strings.
GQL_UNKNOWN_FIELD = json.loads(r"""
{
 "errors": [
  {
   "message": "Cannot query field \"clientBalance2\" on type \"User\".",
   "locations": [{"line": 1, "column": 18}],
   "extensions": {"code": "GRAPHQL_VALIDATION_FAILED"}
  }
 ]
}
""")


def test_a_field_dropped_from_the_schema_also_reads_as_retired(monkeypatch):
    """The LIKELIER first symptom: the endpoint keeps serving and only
    `clientBalance` disappears. Driven through the real HTTP path with the
    recorded body, because the classification rests on the message reaching the
    caller intact."""
    def fake_urlopen(req, timeout=None, context=None):
        raise urllib.error.HTTPError(
            rp.GRAPHQL_URL, 400, "Bad Request", {},
            io.BytesIO(json.dumps(GQL_UNKNOWN_FIELD).replace(
                "clientBalance2", "clientBalance").encode("utf-8")))
    monkeypatch.setattr(rp.urllib.request, "urlopen", fake_urlopen)
    assert rp.account_balance_detail("k")["status"] == rp.BALANCE_RETIRED

    # And the same thing one layer up, where the query succeeds but the field is
    # simply absent from the account object.
    monkeypatch.setattr(rp, "_graphql",
                        lambda *a, **k: {"myself": {"currentSpendPerHr": 0.5}})
    assert rp.account_balance_detail("k")["status"] == rp.BALANCE_RETIRED


def test_a_blip_is_not_reported_as_retired(monkeypatch):
    """The opposite error, and it is not harmless either: telling a user the
    balance is gone for good would have them remove a floor that still works."""
    for exc in (rp.RunPodError("Could not reach RunPod: timed out"),
                rp.RunPodError("RunPod returned HTTP 502 Bad Gateway.", status=502),
                rp.RunPodError("RunPod is rate-limiting requests (429).", status=429)):
        monkeypatch.setattr(rp, "_graphql", _raising_graphql(exc))
        assert rp.account_balance_detail("k")["status"] == rp.BALANCE_ERROR
    # An answer with no account object at all is a permission/transport problem,
    # not evidence that the field was removed.
    monkeypatch.setattr(rp, "_graphql", lambda *a, **k: {"myself": None})
    assert rp.account_balance_detail("k")["status"] == rp.BALANCE_ERROR


def test_no_key_is_its_own_case_and_costs_no_request(monkeypatch):
    """A fresh install has no key. That is not an outage and must not read as
    one, and it must not put a request on the wire to find out."""
    def boom(*a, **k):
        raise AssertionError("no request should be made without a key")
    monkeypatch.setattr(rp.urllib.request, "urlopen", boom)
    assert rp.account_balance_detail("")["status"] == rp.BALANCE_NO_KEY
    assert rp.account_balance("") is None


def test_account_balance_keeps_its_none_on_any_failure_contract(monkeypatch):
    """Every caller of the plain function treats None as 'skip the check'. The
    detail wrapper must not have turned a failure into a truthy dict there."""
    for exc in (rp.RunPodError("gone", status=410),
                rp.RunPodError("timed out"),
                ValueError("something else entirely")):
        monkeypatch.setattr(rp, "_graphql", _raising_graphql(exc))
        assert rp.account_balance("k") is None
    monkeypatch.setattr(rp, "_graphql", lambda *a, **k: {"myself": {}})
    assert rp.account_balance("k") is None


def test_a_readable_balance_is_unchanged(monkeypatch):
    """Recorded off the live account, 2026-08-20."""
    monkeypatch.setattr(rp, "_graphql", lambda *a, **k: {
        "myself": {"clientBalance": 17.0630444381, "currentSpendPerHr": 0.005}})
    assert rp.account_balance("k") == {"balance": 17.0630444381,
                                       "spend_per_hr": 0.005}
    info = rp.account_balance_detail("k")
    assert info["status"] == rp.BALANCE_OK and info["balance"] == 17.0630444381


def test_an_http_error_carries_its_status_so_permanence_is_readable(monkeypatch):
    """`status` is what lets a caller tell 410 from 502 without matching on the
    message text, which is the mechanism the whole classification rests on."""
    def fake_urlopen(req, timeout=None, context=None):
        raise urllib.error.HTTPError(rp.GRAPHQL_URL, 429, "Too Many Requests",
                                     {}, io.BytesIO(b""))
    monkeypatch.setattr(rp.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(rp.RunPodError) as caught:
        rp._graphql("k", "{ myself { clientBalance } }")
    assert caught.value.status == 429


def test_a_graphql_error_object_is_read_for_its_message(monkeypatch):
    """v2's `errors` list holds strings and GraphQL's holds objects. Stringifying
    an object verbatim put a Python dict repr in front of the user with the
    sentence buried inside it (measured), which is the opposite of what surfacing
    the API's own reason is for."""
    detail = rp.error_detail(GQL_UNKNOWN_FIELD)
    assert detail == 'Cannot query field "clientBalance2" on type "User".'
    assert "{" not in detail and "locations" not in detail


def test_a_404_does_not_blame_a_pod_for_every_route(monkeypatch):
    """It reads a 404 on a volume, a template or a route this API version does
    not serve, and sending the reader to look for a terminated pod wastes them."""
    exc = urllib.error.HTTPError("https://api.runpod.io/v2/account/balance", 404,
                                 "Not Found", {}, io.BytesIO(b""))
    msg = rp._http_error_message(exc)
    assert "404" in msg and "volume" in msg.lower()


# ── the pin: no raw field reads outside the client ───────────────────────────

# Read-side spellings that are transport-specific. Request BODY keys are absent
# from this list on purpose (`dataCenterIds`, `networkVolumeId` as a spec key):
# a body is written, not read, and it changes with the endpoint, not with the
# reader. The scanner below only recognises a READ.
_TRANSPORT_FIELDS = frozenset((
    "desiredStatus", "costPerHr", "dataCenterId", "publicIp", "portMappings",
    "gpuDisplayName",
))

_QUOTES = ('"', "'")


def _string_value(tok):
    """The literal text of a simple STRING token, or None for a triple-quoted
    one (a docstring is prose, and this codebase's prose names these fields
    constantly)."""
    text = tok.string
    body = text.lstrip("rbufRBUF")
    if len(body) >= 6 and body[:3] in ('"""', "'''"):
        return None
    if len(body) >= 2 and body[0] in _QUOTES and body[-1] == body[0]:
        return body[1:-1]
    return None


def _raw_field_reads(path):
    """Every `x.get("field")` / `x["field"]` in one file naming a
    transport-specific field, as (line, field) pairs.

    Scans TOKENS rather than text: comments never appear, and a token carries
    its line number, so a failure points at the line to fix. An earlier
    regex-over-text version of this matched NOTHING while a planted violation
    sat in the tree, which is the exact "it went green so it works" trap this
    file exists to close. Hence test_the_scanner_recognises_a_raw_read."""
    hits = []
    with io.open(path, "rb") as fh:
        try:
            toks = [t for t in tokenize.tokenize(fh.readline)
                    if t.type not in (tokenize.COMMENT, tokenize.NL,
                                      tokenize.NEWLINE, tokenize.INDENT,
                                      tokenize.DEDENT)]
        except (tokenize.TokenError, IndentationError, SyntaxError) as exc:
            pytest.fail(f"could not tokenize {path}: {exc}")
    for i, tok in enumerate(toks):
        if tok.type != tokenize.STRING:
            continue
        if _string_value(tok) not in _TRANSPORT_FIELDS:
            continue
        before = [t.string for t in toks[max(0, i - 3):i]]
        if before[-3:] == [".", "get", "("] or before[-1:] == ["["]:
            hits.append((tok.start[0], _string_value(tok)))
    return hits


def _app_sources():
    scripts = os.path.join(ROOT, "scripts")
    for base, _dirs, files in os.walk(scripts):
        if "__pycache__" in base or os.sep + "pod" in base:
            continue
        for f in sorted(files):
            if f.endswith(".py") and f != "runpod_client.py":
                yield os.path.join(base, f)


def test_the_scanner_recognises_a_raw_read(tmp_path):
    """The guard's own guard. A sweep that has never failed is not evidence that
    there is nothing to find."""
    good = tmp_path / "good.py"
    good.write_text(
        "# a comment naming desiredStatus\n"
        "BODY = {'dataCenterIds': ['EU-RO-1']}   # a request body key, written\n"
        "state = rp.pod_state(pod)\n", encoding="utf-8")
    bad = tmp_path / "bad.py"
    bad.write_text("state = pod.get('desiredStatus')\n"
                   "rate = pod['costPerHr']\n", encoding="utf-8")
    assert _raw_field_reads(str(good)) == []
    assert _raw_field_reads(str(bad)) == [(1, "desiredStatus"), (2, "costPerHr")]


def test_transport_specific_fields_are_read_only_inside_runpod_client():
    """The seam is only a seam while everything goes through it.

    A new `pod.get("desiredStatus")` somewhere else is not a style problem: it is
    a line that will read None the day the transport changes, and every one of
    the known cases fails toward spending money. Read it through rp.pod_state /
    pod_cost / pod_data_center / pod_ssh / pod_gpu / pod_volume_id instead."""
    offenders = []
    for path in _app_sources():
        rel = os.path.relpath(path, ROOT)
        for line, field in _raw_field_reads(path):
            offenders.append(f"{rel}:{line} reads {field!r} directly")
    assert not offenders, (
        "read RunPod fields through the runpod_client seam, not by name:\n  "
        + "\n  ".join(offenders))
