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
import tempfile
import subprocess

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import runpod_client as rp
import ssh_setup

APP_ROOT   = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG     = os.path.join(APP_ROOT, "config.json")
STATE      = os.path.join(APP_ROOT, "logs", "runpod_dev_state.json")
KNOWN_HOSTS = os.path.join(APP_ROOT, "logs", "runpod_known_hosts")
PROVISION_SH = os.path.join(APP_ROOT, "pod", "provision.sh")
DEADMAN_PY   = os.path.join(APP_ROOT, "pod", "deadman.py")

DEFAULT_IMAGE = "runpod/pytorch:1.0.7-cu1281-torch291-ubuntu2204"

# Provisioning runs NO GPU compute — it is a pure download + pip + `ollama pull`
# job (provision.sh) — so it does not need the expensive upscale card the run
# uses. Pick the cheapest in-stock GPU in the volume's region and pass the rest
# as an ordered fallback chain, so a single sold-out card can't fail provisioning
# (the real win is availability, not just cost). A small VRAM floor is a sanity
# guard only; nothing here needs the memory.
PROVISION_MIN_VRAM_GB = 8     # sanity floor; provisioning uses no VRAM
PROVISION_MAX_CHAIN   = 6     # bound how many cards we try before giving up


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
    print(f"  $ ssh root@{host} -p {port}  [{command[:80]}{'...' if len(command) > 80 else ''}]")
    # stdin=DEVNULL: when this runs as a GUI subprocess its stdin is a pipe;
    # ssh would inherit and block on it. The remote commands never read it.
    return subprocess.run(args, check=check, stdin=subprocess.DEVNULL)


def _scp(host, port, key, local, remote):
    # scp uses -P (capital) for the port.
    args = ["scp", "-i", key, "-P", str(port),
            "-o", "StrictHostKeyChecking=no",
            "-o", f"UserKnownHostsFile={KNOWN_HOSTS}",
            local, f"root@{host}:{remote}"]
    print(f"  $ scp {os.path.basename(local)} -> root@{host}:{remote}")
    return subprocess.run(args, check=True, stdin=subprocess.DEVNULL)


def _wait_for_ssh(host, port, key, timeout=180, poll=5):
    """Wait until the pod's sshd actually ACCEPTS a connection.

    RunPod publishes the port-22 mapping (so `ssh_endpoint` returns host:port, and
    `wait_until_running` returns) a little BEFORE sshd is listening — so an
    immediate scp/ssh can hit 'Connection refused' (exit 255) and crash the run.
    This happened on a slower data center (EUR-IS-1) where EU-SE-1 had got lucky.
    Probe with a trivial `ssh … true` until it succeeds or `timeout` elapses;
    connection noise is swallowed so only the outcome is logged. Returns True once
    SSH answers, False on timeout (the caller may still try and surface the real
    error)."""
    deadline = time.time() + timeout
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        try:
            res = subprocess.run(
                ["ssh", *_ssh_opts(key, port), f"root@{host}", "true"],
                stdin=subprocess.DEVNULL, capture_output=True, timeout=30)
            if res.returncode == 0:
                print(f"  SSH ready (after {attempt} probe(s)).", flush=True)
                return True
        except Exception:                                # noqa: BLE001 (best-effort probe)
            pass
        time.sleep(poll)
    return False


def _provision_gpu_chain(key, rpc, region):
    """Build the ordered GPU fallback chain for a provisioning pod: the cheapest
    in-stock cards in `region` first (provisioning needs no GPU power), with the
    configured `gpu_type_id` appended as a last-ditch fallback. Falls back to just
    the configured card if the live availability query fails, so provisioning
    still works offline-of-GraphQL."""
    configured = rpc.get("gpu_type_id", "NVIDIA GeForce RTX 5090")
    try:
        avail = rp.available_gpus(key, data_center_id=region,
                                  min_memory_gb=PROVISION_MIN_VRAM_GB)
    except rp.RunPodError as exc:
        print(f"  (live GPU availability lookup failed: {exc};", flush=True)
        print(f"   using the configured GPU {configured})", flush=True)
        return [configured]
    chain = [g["id"] for g in avail[:PROVISION_MAX_CHAIN]]
    if configured not in chain:
        chain.append(configured)          # last-ditch fallback to the saved pick
    if not chain:                          # region returned nothing in stock
        return [configured]
    if avail:
        c = avail[0]
        print(f"  cheapest available in {region}: {c['name']} @ ${c['price']}/h "
              f"({c['memory_gb']} GB VRAM); {len(chain)}-card fallback chain", flush=True)
    return chain


def _arm_provision_deadman(host, port, ssh_key, api_key, pod_id, ceiling_min):
    """Arm the on-pod dead-man's switch on the provisioning pod as a backstop to
    this process's finally-teardown.

    `cmd_setup_volume` terminates the pod in a `finally`, which covers errors
    INSIDE this process — but NOT this process being killed (e.g. the GUI is
    force-closed because the provisioning window looked hung during a long
    download). This daemon self-terminates the pod after `ceiling_min` even then,
    so a stuck provisioning can never leave a pod billing.

    Idle timeout is DISABLED here: no worker writes a heartbeat during
    provisioning, so the deadman's idle check would (wrongly) fire mid-download.
    The hard max-runtime ceiling is the only guard. Best-effort: any failure to
    arm is logged and ignored (the finally is still the primary teardown)."""
    if not os.path.exists(DEADMAN_PY):
        print(f"  (deadman.py missing at {DEADMAN_PY}; skipping the provisioning "
              f"dead-man's switch)", flush=True)
        return False
    try:
        _scp(host, port, ssh_key, DEADMAN_PY, "/root/deadman.py")
        # The pod self-terminates via the REST API (runpodctl isn't reliably
        # pre-authed on a deploy-API pod), so it needs the key in a 0600 file.
        kf = tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8")
        kf.write(api_key)
        kf.close()
        try:
            _scp(host, port, ssh_key, kf.name, "/root/.rp_key")
            _ssh(host, port, ssh_key, "chmod 600 /root/.rp_key", check=False)
        finally:
            os.remove(kf.name)
        # The volume venv does not exist yet (provision.sh builds it), so run the
        # stdlib-only deadman with the image's system python. setsid detaches it so
        # this ssh call returns immediately (a plain `& ` would keep ssh hanging).
        inner = (
            "echo $$ > /root/deadman.pid; "
            "exec env RUNPOD_API_KEY=\"$(cat /root/.rp_key)\" python "
            f"/root/deadman.py --pod-id {pod_id} --terminate "
            f"--max-runtime-min {int(ceiling_min)} --idle-timeout-min 0")
        launch = (
            "([ -f /root/deadman.pid ] && kill \"$(cat /root/deadman.pid)\" 2>/dev/null); sleep 1; "
            f"setsid sh -c '{inner}' < /dev/null > /root/deadman.log 2>&1 & echo armed")
        _ssh(host, port, ssh_key, launch, check=False)
        return True
    except Exception as exc:                              # noqa: BLE001 (fail-safe)
        print(f"  (could not arm the provisioning dead-man's switch: {exc};", flush=True)
        print("   this process still terminates the pod on exit)", flush=True)
        return False


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
    # Hand the app's public key to the pod (RunPod appends $PUBLIC_KEY to
    # authorized_keys at boot) so SSH works without registering it on the account.
    pub = ssh_setup.read_public_key(_ssh_key_path(rpc))
    if pub:
        spec["env"] = {"PUBLIC_KEY": pub}
    print(f"Creating pod: {gpu} in {region}, volume {vol_id}, image {image}")
    print("  *** this STARTS BILLING - remember to 'terminate' when done ***")

    def on_event(kind, attempt, pod_id, info):
        if kind == "created":
            # Record the in-flight pod immediately so a hang is always cleanable.
            _save_state({"pod_id": pod_id, "created_at": time.time(),
                         "region": region, "gpu": gpu})
            print(f"  [attempt {attempt}] created pod {pod_id}; waiting for RUNNING + SSH …")
        elif kind == "status":
            s, h, p = info
            print(f"    status={s} ssh={h}:{p}")
        elif kind == "bad":
            print(f"  [attempt {attempt}] pod {pod_id} did not deploy ({info}) "
                  f"-> retiring it and trying a fresh pod")
        elif kind == "giveup":
            print(f"  gave up after {attempt} attempt(s): {info}")

    try:
        pod = rp.create_pod_resilient(key, spec, attempts=3, deploy_timeout=240,
                                      poll=8, on_event=on_event)
    except rp.RunPodError as exc:
        _clear_state()
        sys.exit(f"Could not get a healthy pod: {exc}")
    pod_id = pod.get("id")
    host, port = rp.ssh_endpoint(pod)
    print(f"\nReady (pod {pod_id}). SSH: ssh -i <key> -p {port} root@{host}")
    print("Next: python scripts/runpod_provision.py worker   (then 'bench')")


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
        print(f"elapsed  : {hrs:.2f} h   est. cost ~ ${hrs * rate:.2f} (@ ${rate}/h)")


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


def cmd_smoke(rpc, args):
    """End-to-end proof: push the engine + a sample image, upscale on the pod
    (models from the volume), pull the result back. Usage:
        smoke [local_image] [resolution]"""
    _, host, port = _endpoint_or_die(rpc)
    key = _ssh_key_path(rpc)
    local_img = args[0] if args else os.path.join(APP_ROOT, "samples", "original", "pisic7.jpg")
    resolution = args[1] if len(args) > 1 else "1080"
    if not os.path.exists(local_img):
        sys.exit(f"Sample image not found: {local_img}")

    # Engine settings come from config.json's upscale section.
    with open(CONFIG, encoding="utf-8") as f:
        upscale_cfg = json.load(f).get("upscale", {})
    settings_path = os.path.join(APP_ROOT, "logs", "_smoke_settings.json")
    os.makedirs(os.path.dirname(settings_path), exist_ok=True)
    with open(settings_path, "w", encoding="utf-8") as f:
        json.dump(upscale_cfg, f)

    engine_py = os.path.join(APP_ROOT, "scripts", "upscale_engine.py")
    one_py    = os.path.join(APP_ROOT, "pod", "upscale_one.py")
    _scp(host, port, key, engine_py, "/root/upscale_engine.py")
    _scp(host, port, key, one_py, "/root/upscale_one.py")
    _scp(host, port, key, settings_path, "/root/smoke_settings.json")
    _scp(host, port, key, local_img, "/root/smoke_in.jpg")

    print("Upscaling one image on the pod (engine loads from the volume)…")
    cmd = ("/workspace/venv/bin/python /root/upscale_one.py "
           "/workspace/seedvr2 /workspace/models/seedvr2 /root/smoke_settings.json "
           f"/root/smoke_in.jpg /root/smoke_out.jpg {resolution}")
    _ssh(host, port, key, cmd)

    out_local = os.path.join(APP_ROOT, "test_output", "runpod_smoke.jpg")
    os.makedirs(os.path.dirname(out_local), exist_ok=True)
    args_scp = ["scp", "-i", key, "-P", str(port),
                "-o", "StrictHostKeyChecking=no",
                "-o", f"UserKnownHostsFile={KNOWN_HOSTS}",
                f"root@{host}:/root/smoke_out.jpg", out_local]
    print(f"  $ scp root@{host}:/root/smoke_out.jpg -> {out_local}")
    subprocess.run(args_scp, check=True)
    print(f"Done. Result saved to {out_local}")


def cmd_worker(rpc, args):
    """Start the resident upscale worker on the pod and wait until it is ready
    (the first model load is slow — weights from the network volume).
    Optional arg: an attention_mode override (e.g. sageattn_2) for experiments."""
    _, host, port = _endpoint_or_die(rpc)
    key = _ssh_key_path(rpc)
    wport = int(rpc.get("worker_port", 8200))
    with open(CONFIG, encoding="utf-8") as f:
        upscale_cfg = json.load(f).get("upscale", {})
    if args:
        upscale_cfg["attention_mode"] = args[0]
        print(f"  attention_mode override: {args[0]}")
    settings_path = os.path.join(APP_ROOT, "logs", "_worker_settings.json")
    os.makedirs(os.path.dirname(settings_path), exist_ok=True)
    with open(settings_path, "w", encoding="utf-8") as f:
        json.dump(upscale_cfg, f)

    _scp(host, port, key, os.path.join(APP_ROOT, "scripts", "upscale_engine.py"),
         "/root/upscale_engine.py")
    _scp(host, port, key, os.path.join(APP_ROOT, "pod", "worker.py"), "/root/worker.py")
    _scp(host, port, key, settings_path, "/root/worker_settings.json")

    # NB: kill any previous worker via a PID file — NOT `pkill -f worker.py`,
    # which would also match this very ssh shell (its command line contains
    # "worker.py") and kill it before nohup runs.
    launch = (
        "([ -f /root/worker.pid ] && kill \"$(cat /root/worker.pid)\" 2>/dev/null); sleep 1; "
        "cd /root && nohup /workspace/venv/bin/python /root/worker.py "
        "--repo-dir /workspace/seedvr2 --model-dir /workspace/models/seedvr2 "
        f"--settings /root/worker_settings.json --port {wport} "
        "> /root/worker.log 2>&1 & "
        "echo $! > /root/worker.pid; echo \"started pid $(cat /root/worker.pid)\"; "
        "for i in $(seq 1 100); do "
        f"  if curl -sf localhost:{wport}/health >/dev/null 2>&1; then "
        f"    echo READY; curl -s localhost:{wport}/health; echo; break; fi; "
        "  sleep 5; done; "
        "echo '--- worker.log (tail) ---'; tail -n 8 /root/worker.log")
    print("Starting worker on the pod (waits for the slow first model load)…")
    _ssh(host, port, key, launch, check=False)


def cmd_bench(rpc, args):
    """Stream the same image through the worker N times to separate cold-start
    warmup (image #1) from steady-state throughput (#2..N). Usage:
        bench [image] [count] [resolution]"""
    _, host, port = _endpoint_or_die(rpc)
    key = _ssh_key_path(rpc)
    wport = int(rpc.get("worker_port", 8200))
    img = args[0] if args else os.path.join(APP_ROOT, "samples", "original", "pisic7.jpg")
    count = int(args[1]) if len(args) > 1 else 4
    resolution = int(args[2]) if len(args) > 2 else 1080
    if not os.path.exists(img):
        sys.exit(f"Image not found: {img}")

    from remote_upscale_engine import RemoteUpscaleEngine, RemoteUpscaleError
    print(f"Connecting to worker on {host} (worker port {wport})…")
    try:
        engine = RemoteUpscaleEngine(host, port, key, worker_port=wport,
                                     known_hosts=KNOWN_HOSTS)
    except RemoteUpscaleError as exc:
        sys.exit(f"Could not reach the worker: {exc}  (run 'worker' first?)")
    print(f"Connected — device: {engine.device_name}")
    out_dir = os.path.join(APP_ROOT, "test_output")
    os.makedirs(out_dir, exist_ok=True)
    try:
        times = []
        for i in range(1, count + 1):
            dst = os.path.join(out_dir, f"runpod_bench_{i}.jpg")
            t0 = time.time()
            engine.upscale(img, dst, resolution)
            wall = time.time() - t0
            times.append(wall)
            tag = "cold" if i == 1 else "warm"
            wt = engine.last_process_time
            print(f"  image {i}/{count} [{tag}]: {wall:.1f}s wall"
                  + (f" (worker {wt:.1f}s)" if wt else ""))
        warm = times[1:] or times
        print(f"\nwarm avg: {sum(warm)/len(warm):.1f}s/image over {len(warm)} image(s)")
    finally:
        engine.close()


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


def cmd_setup_volume(rpc, args):
    """One-shot model-volume provisioning for the GUI 'Provision models' button:
    create a fresh pod, run pod/provision.sh to fill the network volume (~22 GB:
    SeedVR2 + Ollama + venv), then ALWAYS terminate the pod — even on error or
    Ctrl-C — so a provisioning pod is never left billing. Streams progress to
    stdout (the GUI shows it live). The on-pod dead-man's switch is not used here
    because this process owns the teardown in a finally."""
    key = rpc["api_key"]
    vol_id = rpc.get("network_volume_id", "").strip()
    if not vol_id:
        sys.exit("No model volume selected (Settings → Remote upscaling → Create…).")
    region = rp.volume_region(key, vol_id)
    if not region:
        sys.exit(f"Could not read the data center of volume {vol_id}.")
    if not os.path.exists(PROVISION_SH):
        sys.exit(f"Missing {PROVISION_SH}")

    # Pre-flight: refuse to provision a volume a RUNNING pod still mounts —
    # rebuilding the venv under a live worker corrupts it and NFS-blocks the wipe
    # ("Directory not empty" on held-open .so files). Best-effort; an unreadable
    # pod list never blocks provisioning.
    busy = [p for p in rp.pods_using_volume(key, vol_id)
            if p.get("status") == rp.STATUS_RUNNING]
    if busy:
        names = ", ".join(p["id"] + (f" ({p['name']})" if p.get("name") else "")
                          for p in busy)
        sys.exit(
            f"Volume {vol_id} is still attached to a running pod: {names}.\n"
            "Provisioning rebuilds the venv on the volume, which corrupts that "
            "pod's live worker and blocks the rebuild. Terminate the pod first "
            "(the run's Stop, or it self-stops on its idle timeout), then retry.")

    # Ensure the app's SSH key exists and grab its public half for PUBLIC_KEY.
    try:
        kp, pub, _created = ssh_setup.ensure_keypair(
            os.path.expandvars(rpc.get("ssh_key_path", "")) or None)
    except ssh_setup.SshSetupError as exc:
        sys.exit(f"SSH setup failed: {exc}")

    image = rpc.get("image_name") or DEFAULT_IMAGE
    gpu_chain = _provision_gpu_chain(key, rpc, region)
    spec = {
        "name":              "image-toolbox-provision",
        "imageName":         image,
        "gpuTypeIds":        gpu_chain,
        "gpuCount":          1,
        "cloudType":         "SECURE",
        "dataCenterIds":     [region],
        "networkVolumeId":   vol_id,
        "containerDiskInGb": int(rpc.get("container_disk_gb", 30)),
        "ports":             ["22/tcp"],
    }
    if pub:
        spec["env"] = {"PUBLIC_KEY": pub}

    print(f"Provisioning the model volume {vol_id} in {region}.", flush=True)
    print(f"  GPU {gpu_chain[0]}, image {image}. This downloads ~22 GB to the volume and", flush=True)
    print("  can take 10-20 minutes. The pod is terminated automatically when done.", flush=True)
    print("  *** a pod is now BILLING until provisioning finishes ***", flush=True)

    def on_event(kind, attempt, pod_id, info):
        if kind == "created":
            _save_state({"pod_id": pod_id, "created_at": time.time(),
                         "region": region, "gpu": info})
            print(f"  [attempt {attempt}] created pod {pod_id} on {info}; "
                  f"waiting for RUNNING + SSH …", flush=True)
        elif kind == "status":
            s, h, p = info
            print(f"    status={s} ssh={h}:{p}", flush=True)
        elif kind == "bad":
            print(f"  [attempt {attempt}] pod {pod_id} did not deploy ({info}) — retrying", flush=True)
        elif kind == "giveup":
            print(f"  gave up after {attempt} attempt(s): {info}", flush=True)

    pod_id = None
    try:
        pod = rp.create_pod_resilient(key, spec, attempts=3, deploy_timeout=240,
                                      poll=8, on_event=on_event)
        pod_id = pod.get("id")
        host, port = rp.ssh_endpoint(pod)
        if not host:
            sys.exit("Pod became ready but published no SSH endpoint.")
        # The endpoint is published before sshd is actually listening, so wait for
        # SSH to answer before any scp — otherwise the first transfer hits
        # 'Connection refused' and crashes the run (seen on a slow DC).
        print("  Waiting for the pod's SSH service to accept connections …", flush=True)
        if not _wait_for_ssh(host, port, kp):
            print("  (SSH still refusing after the wait; attempting the transfers "
                  "anyway) …", flush=True)
        # Backstop the finally-teardown: if THIS process is killed before it can
        # terminate the pod (e.g. the GUI is force-closed mid-download), the on-pod
        # dead-man's switch still self-terminates after the configured ceiling.
        ceiling = int(rpc.get("provision_max_runtime_minutes", 60))
        print(f"  Arming the provisioning dead-man's switch "
              f"(self-terminate after {ceiling} min) …", flush=True)
        _arm_provision_deadman(host, port, kp, key, pod_id, ceiling)
        _scp(host, port, kp, PROVISION_SH, "/root/provision.sh")
        dit = rpc.get("dit_model") or "seedvr2_ema_7b_fp16.safetensors"
        env = (f"DIT_MODEL='{os.environ.get('DIT_MODEL', dit)}' "
               f"OLLAMA_MODEL='{os.environ.get('OLLAMA_MODEL', 'qwen2.5vl:7b')}'")
        print("Running provision.sh on the pod …", flush=True)
        res = _ssh(host, port, kp, f"{env} bash /root/provision.sh", check=False)
        if res.returncode != 0:
            sys.exit(f"provision.sh failed (exit {res.returncode}). The pod will still be terminated.")
        print("Provisioning complete — the model volume is ready.", flush=True)
    finally:
        if pod_id:
            print("Terminating the provisioning pod …", flush=True)
            ok, msg = rp.ensure_stopped(key, pod_id, terminate=True)
            print("  " + msg, flush=True)
            if ok:
                _clear_state()


COMMANDS = {
    "create": cmd_create, "status": cmd_status, "probe": cmd_probe,
    "provision": cmd_provision, "smoke": cmd_smoke, "setup-volume": cmd_setup_volume,
    "worker": cmd_worker, "bench": cmd_bench, "ssh": cmd_ssh,
    "terminate": cmd_terminate, "stop": cmd_terminate,
}


def main(argv):
    if not argv or argv[0] not in COMMANDS:
        sys.exit("Commands: " + ", ".join(sorted(set(COMMANDS))))
    rpc = _load_config()
    COMMANDS[argv[0]](rpc, argv[1:])


if __name__ == "__main__":
    main(sys.argv[1:])
