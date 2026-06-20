"""
runpod_provision.py — standalone DEV driver for remote-pod provisioning (#1).

NOT wired into the GUI. A command-line tool used during development to create a
RunPod pod, SSH in, build the model network volume, and tear the pod down — so
the whole flow can be validated live before any of it is wired into the app.

Reads config.json (api_key, the `runpod` section, ssh_key_path) from the app
root. Uses runpod_client for the REST control plane and the Windows OpenSSH
client (ssh.exe/scp.exe) for the pod side.

    python scripts/runpod_provision.py create       # create a pod (STARTS BILLING)
    python scripts/runpod_provision.py status        # status + SSH endpoint + cost
    python scripts/runpod_provision.py probe         # nvidia-smi over SSH
    python scripts/runpod_provision.py provision     # run pod/provision.sh on the pod
    python scripts/runpod_provision.py ssh "<cmd>"  # run an arbitrary command
    python scripts/runpod_provision.py terminate     # delete the pod (frees billing)

The pod id is remembered in logs/runpod_dev_state.json between calls.
"""

import os
import sys
import json
import time
import subprocess

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import runpod_client as rp

APP_ROOT   = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG     = os.path.join(APP_ROOT, "config.json")
STATE      = os.path.join(APP_ROOT, "logs", "runpod_dev_state.json")
KNOWN_HOSTS = os.path.join(APP_ROOT, "logs", "runpod_known_hosts")
PROVISION_SH = os.path.join(APP_ROOT, "pod", "provision.sh")

DEFAULT_IMAGE = "runpod/pytorch:2.8.0-py3.11-cuda12.8.1-cudnn-devel-ubuntu"


def _load_config():
    with open(CONFIG, encoding="utf-8") as f:
        cfg = json.load(f)
    rpc = cfg.get("runpod", {})
    if not rpc.get("api_key"):
        sys.exit("No runpod.api_key in config.json (Settings → Remote upscaling).")
    return rpc


def _ssh_key_path(rpc):
    path = os.path.expandvars(rpc.get("ssh_key_path", ""))
    if not path or not os.path.exists(path):
        sys.exit(f"SSH private key not found: {path or '(unset)'}")
    return path


def _save_state(d):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2)


def _load_state():
    try:
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _clear_state():
    try:
        os.remove(STATE)
    except OSError:
        pass


# ── ssh / scp helpers ────────────────────────────────────────────────────────

def _ssh_opts(key, port):
    return [
        "-i", key, "-p", str(port),
        "-o", "StrictHostKeyChecking=no",
        "-o", f"UserKnownHostsFile={KNOWN_HOSTS}",
        "-o", "ConnectTimeout=20",
    ]


def _ssh(host, port, key, command, check=True):
    args = ["ssh", *_ssh_opts(key, port), f"root@{host}", command]
    print(f"  $ ssh root@{host} -p {port}  «{command[:80]}{'…' if len(command) > 80 else ''}»")
    return subprocess.run(args, check=check)


def _scp(host, port, key, local, remote):
    # scp uses -P (capital) for the port.
    args = ["scp", "-i", key, "-P", str(port),
            "-o", "StrictHostKeyChecking=no",
            "-o", f"UserKnownHostsFile={KNOWN_HOSTS}",
            local, f"root@{host}:{remote}"]
    print(f"  $ scp {os.path.basename(local)} -> root@{host}:{remote}")
    return subprocess.run(args, check=True)


def _endpoint_or_die(rpc):
    st = _load_state()
    pod_id = st.get("pod_id")
    if not pod_id:
        sys.exit("No pod recorded — run 'create' first.")
    pod = rp.get_pod(rpc["api_key"], pod_id)
    host, port = rp.ssh_endpoint(pod)
    if not host:
        sys.exit(f"Pod {pod_id} has no SSH endpoint yet (status: "
                 f"{pod.get('desiredStatus') if isinstance(pod, dict) else '?'}).")
    return pod_id, host, port


# ── commands ─────────────────────────────────────────────────────────────────

def cmd_create(rpc, args):
    key = rpc["api_key"]
    vol_id = rpc.get("network_volume_id", "").strip()
    if not vol_id:
        sys.exit("No runpod.network_volume_id set — create the model volume first "
                 "(Settings → Remote upscaling → Create…).")
    region = rp.volume_region(key, vol_id)
    if not region:
        sys.exit(f"Could not read the data center of volume {vol_id}.")
    image = rpc.get("image_name") or DEFAULT_IMAGE
    gpu   = rpc.get("gpu_type_id", "NVIDIA GeForce RTX 5090")
    spec = {
        "name":              "image-toolbox-dev",
        "imageName":         image,
        "gpuTypeIds":        [gpu],
        "gpuCount":          1,
        "cloudType":         "SECURE",
        "dataCenterIds":     [region],
        "networkVolumeId":   vol_id,
        "containerDiskInGb": int(rpc.get("container_disk_gb", 30)),
        "ports":             ["22/tcp"],
    }
    print(f"Creating pod: {gpu} in {region}, volume {vol_id}, image {image}")
    print("  *** this STARTS BILLING — remember to 'terminate' when done ***")
    pod = rp.create_pod(key, spec)
    pod_id = (pod or {}).get("id")
    if not pod_id:
        sys.exit(f"Create did not return a pod id. Response: {pod}")
    _save_state({"pod_id": pod_id, "created_at": time.time(),
                 "region": region, "gpu": gpu})
    print(f"  created pod {pod_id}; waiting for RUNNING + SSH …")

    def on_status(status, host, port):
        print(f"    status={status} ssh={host}:{port}")
    pod = rp.wait_until_running(key, pod_id, timeout=900, poll=8, on_status=on_status)
    host, port = rp.ssh_endpoint(pod)
    print(f"\nReady. SSH: ssh -i <key> -p {port} root@{host}")
    print("Next: python scripts/runpod_provision.py probe   (then 'provision')")


def cmd_status(rpc, args):
    st = _load_state()
    pod_id = st.get("pod_id")
    if not pod_id:
        print("No pod recorded.")
        return
    pod = rp.get_pod(rpc["api_key"], pod_id)
    status = pod.get("desiredStatus") if isinstance(pod, dict) else "?"
    host, port = rp.ssh_endpoint(pod)
    print(f"pod_id   : {pod_id}")
    print(f"status   : {status}")
    print(f"ssh      : {host}:{port}" if host else "ssh      : (not ready)")
    created = st.get("created_at")
    if created:
        hrs = (time.time() - created) / 3600.0
        rate = float(rpc.get("hourly_rate", 0.0))
        print(f"elapsed  : {hrs:.2f} h   est. cost ≈ ${hrs * rate:.2f} (@ ${rate}/h)")


def cmd_probe(rpc, args):
    _, host, port = _endpoint_or_die(rpc)
    key = _ssh_key_path(rpc)
    _ssh(host, port, key,
         "echo connected && nvidia-smi --query-gpu=name,memory.total,driver_version "
         "--format=csv,noheader && python --version && ls -la /workspace")


def cmd_provision(rpc, args):
    _, host, port = _endpoint_or_die(rpc)
    key = _ssh_key_path(rpc)
    if not os.path.exists(PROVISION_SH):
        sys.exit(f"Missing {PROVISION_SH}")
    _scp(host, port, key, PROVISION_SH, "/root/provision.sh")
    dit = rpc.get("dit_model") or "seedvr2_ema_7b_fp16.safetensors"
    # DIT model name actually lives in the upscale section; allow override via env.
    env = (f"DIT_MODEL='{os.environ.get('DIT_MODEL', dit)}' "
           f"OLLAMA_MODEL='{os.environ.get('OLLAMA_MODEL', 'qwen2.5vl:7b')}'")
    print("Running provision.sh on the pod (this downloads ~22 GB to the volume)…")
    _ssh(host, port, key, f"{env} bash /root/provision.sh")


def cmd_ssh(rpc, args):
    if not args:
        sys.exit('Usage: runpod_provision.py ssh "<command>"')
    _, host, port = _endpoint_or_die(rpc)
    key = _ssh_key_path(rpc)
    _ssh(host, port, key, " ".join(args), check=False)


def cmd_terminate(rpc, args):
    st = _load_state()
    pod_id = st.get("pod_id")
    if not pod_id:
        print("No pod recorded.")
        return
    ok, msg = rp.ensure_stopped(rpc["api_key"], pod_id, terminate=True)
    print(msg)
    if ok:
        _clear_state()


COMMANDS = {
    "create": cmd_create, "status": cmd_status, "probe": cmd_probe,
    "provision": cmd_provision, "ssh": cmd_ssh,
    "terminate": cmd_terminate, "stop": cmd_terminate,
}


def main(argv):
    if not argv or argv[0] not in COMMANDS:
        sys.exit("Commands: " + ", ".join(sorted(set(COMMANDS))))
    rpc = _load_config()
    COMMANDS[argv[0]](rpc, argv[1:])


if __name__ == "__main__":
    main(sys.argv[1:])
