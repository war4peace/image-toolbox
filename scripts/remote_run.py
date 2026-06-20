"""
remote_run.py
-------------
Orchestrates a remote-pod upscaling session (#1, Phase 3) and is shared by the
GUI-driven batch_upscale and the dev driver. One `RemoteSession`:

  create pod (deploy watchdog)  →  push engine/worker/deadman to the pod
   →  start the resident worker  →  arm the on-pod dead-man's switch
   →  hand back a connected RemoteUpscaleEngine
   →  on close(): tear the pod down (the deadman is the backup if we can't)

Security note: a REST-API-created pod has NO pre-authed runpodctl and no
$RUNPOD_POD_ID, so the dead-man's switch self-terminates via the REST API with
the **API key placed on the pod** (written to a 0600 file, not the command line).
That key-on-pod is unavoidable for a pod to stop itself after the client dies —
use a SCOPED key (pod stop/terminate only) in production.

Pure standard library (subprocess + the stdlib runpod_client / remote engine).
"""
import os
import json
import time
import tempfile
import subprocess

import runpod_client as rp
from remote_upscale_engine import RemoteUpscaleEngine

DEFAULT_IMAGE = "runpod/pytorch:1.0.7-cu1281-torch291-ubuntu2204"
HEARTBEAT = "/tmp/upscale_heartbeat"


def _ssh_base(key, port, known_hosts):
    return ["-i", key, "-p", str(port),
            "-o", "StrictHostKeyChecking=no",
            "-o", f"UserKnownHostsFile={known_hosts}",
            "-o", "ConnectTimeout=20"]


class RemoteSession:
    def __init__(self, runpod_cfg, upscale_cfg, app_root, on_event=None,
                 attach=None):
        """`attach` = (pod_id, host, ssh_port) to reuse a running pod instead of
        creating one (dev/validation). `on_event(msg)` is for progress lines."""
        self.cfg = runpod_cfg
        self.upscale_cfg = upscale_cfg
        self.app_root = app_root
        self.api_key = runpod_cfg.get("api_key", "")
        self.key_path = os.path.expandvars(runpod_cfg.get("ssh_key_path", ""))
        self.worker_port = int(runpod_cfg.get("worker_port", 8200))
        self.terminate_when_done = bool(runpod_cfg.get("terminate_when_done", True))
        self.on_event = on_event or (lambda *a: None)
        self.known_hosts = os.path.join(app_root, "logs", "runpod_known_hosts")
        self._attach = attach
        self.pod_id = attach[0] if attach else None
        self.host = attach[1] if attach else None
        self.ssh_port = attach[2] if attach else None
        self.engine = None

    # ── ssh / scp ──────────────────────────────────────────────────────────────

    def _ssh(self, command, check=True, timeout=None):
        args = ["ssh", *_ssh_base(self.key_path, self.ssh_port, self.known_hosts),
                f"root@{self.host}", command]
        return subprocess.run(args, check=check, timeout=timeout,
                              capture_output=True, text=True)

    def _scp(self, local, remote):
        args = ["scp", "-i", self.key_path, "-P", str(self.ssh_port),
                "-o", "StrictHostKeyChecking=no",
                "-o", f"UserKnownHostsFile={self.known_hosts}",
                local, f"root@{self.host}:{remote}"]
        subprocess.run(args, check=True, capture_output=True, text=True)

    # ── lifecycle ──────────────────────────────────────────────────────────────

    def start(self):
        if not self.api_key:
            raise rp.RunPodError("No RunPod API key configured.")
        if not (self.key_path and os.path.exists(self.key_path)):
            raise rp.RunPodError(f"SSH key not found: {self.key_path}")
        if not self.host:
            # Reuse a still-running app pod (faster, and avoids double-spinning);
            # mark it as attached so close() won't terminate a pod we didn't make.
            found = self._find_existing_pod()
            if found:
                self.pod_id, self.host, self.ssh_port = found
                self._attach = found
                self._emit(f"Reusing running pod {self.pod_id} ({self.host}).")
            else:
                self._create_pod()
        self._push_files()
        self._start_worker()
        self._arm_deadman()
        self._emit("Connecting to the worker …")
        self.engine = RemoteUpscaleEngine(
            self.host, self.ssh_port, self.key_path,
            worker_port=self.worker_port, known_hosts=self.known_hosts)
        self._emit(f"Remote engine ready on {self.engine.device_name}.")
        return self.engine

    def _emit(self, msg):
        try:
            self.on_event(msg)
        except Exception:
            pass

    def _find_existing_pod(self):
        """Return (id, host, ssh_port) of a RUNNING app pod that's already up and
        SSH-reachable, or None. Lets a run reuse a pod the user left running
        instead of spinning a new one."""
        try:
            pods = rp.list_pods(self.api_key)
        except rp.RunPodError:
            return None
        for p in pods:
            if not isinstance(p, dict) or p.get("desiredStatus") != rp.STATUS_RUNNING:
                continue
            if not str(p.get("name", "")).startswith("image-toolbox"):
                continue
            host, port = rp.ssh_endpoint(p)
            if host and port:
                return p.get("id"), host, port
        return None

    def _create_pod(self):
        vol_id = self.cfg.get("network_volume_id", "").strip()
        if not vol_id:
            raise rp.RunPodError("No runpod.network_volume_id configured.")
        region = rp.volume_region(self.api_key, vol_id)
        if not region:
            raise rp.RunPodError(f"Could not read region of volume {vol_id}.")
        spec = {
            "name": "image-toolbox-remote",
            "imageName": self.cfg.get("image_name") or DEFAULT_IMAGE,
            "gpuTypeIds": [self.cfg.get("gpu_type_id", "NVIDIA GeForce RTX 5090")],
            "gpuCount": 1, "cloudType": "SECURE",
            "dataCenterIds": [region], "networkVolumeId": vol_id,
            "containerDiskInGb": int(self.cfg.get("container_disk_gb", 30)),
            "ports": ["22/tcp"],
        }
        self._emit(f"Creating pod ({spec['gpuTypeIds'][0]} in {region}) …")

        def ev(kind, attempt, pod_id, info):
            if kind == "created":
                self.pod_id = pod_id
                self._emit(f"Pod {pod_id} created (attempt {attempt}); waiting for deploy …")
            elif kind == "bad":
                self._emit(f"Pod {pod_id} failed to deploy ({info}); retrying with a fresh pod …")
            elif kind == "giveup":
                self._emit(f"Gave up creating a pod: {info}")

        pod = rp.create_pod_resilient(self.api_key, spec, attempts=3,
                                      deploy_timeout=240, poll=8, on_event=ev)
        self.pod_id = pod.get("id")
        self.host, self.ssh_port = rp.ssh_endpoint(pod)

    def _push_files(self):
        self._emit("Uploading worker + dead-man's switch to the pod …")
        for name in ("upscale_engine.py",):
            self._scp(os.path.join(self.app_root, "scripts", name), f"/root/{name}")
        for name in ("worker.py", "deadman.py"):
            self._scp(os.path.join(self.app_root, "pod", name), f"/root/{name}")
        # Worker settings (the upscale config section).
        tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        json.dump(self.upscale_cfg, tmp)
        tmp.close()
        try:
            self._scp(tmp.name, "/root/worker_settings.json")
        finally:
            os.remove(tmp.name)
        # The API key for the on-pod dead-man's switch — written to a 0600 file,
        # never the command line. (Unavoidable for a pod to stop itself; use a
        # scoped key in production.)
        keyfile = tempfile.NamedTemporaryFile("w", delete=False)
        keyfile.write(self.api_key)
        keyfile.close()
        try:
            self._scp(keyfile.name, "/root/.rp_key")
            self._ssh("chmod 600 /root/.rp_key")
        finally:
            os.remove(keyfile.name)

    def _start_worker(self):
        self._emit("Starting the resident worker (first model load is slow) …")
        launch = (
            "([ -f /root/worker.pid ] && kill \"$(cat /root/worker.pid)\" 2>/dev/null); sleep 1; "
            "cd /root && nohup /workspace/venv/bin/python /root/worker.py "
            "--repo-dir /workspace/seedvr2 --model-dir /workspace/models/seedvr2 "
            f"--settings /root/worker_settings.json --port {self.worker_port} "
            f"--heartbeat {HEARTBEAT} < /dev/null > /root/worker.log 2>&1 & "
            "echo $! > /root/worker.pid; "
            f"for i in $(seq 1 120); do "
            f"  curl -sf localhost:{self.worker_port}/health >/dev/null 2>&1 && break; sleep 5; done; "
            f"curl -sf localhost:{self.worker_port}/health >/dev/null 2>&1 || "
            "{ echo WORKER_FAILED; tail -n 20 /root/worker.log; exit 1; }")
        res = self._ssh(launch, check=False, timeout=900)
        if "WORKER_FAILED" in (res.stdout + res.stderr) or res.returncode != 0:
            raise rp.RunPodError(
                "Worker failed to become ready:\n" + (res.stdout + res.stderr)[-800:])

    def _arm_deadman(self):
        max_min = int(self.cfg.get("max_runtime_minutes", 720))
        idle_min = int(self.cfg.get("idle_timeout_minutes", 15))
        action = "--terminate" if self.terminate_when_done else ""
        self._emit(f"Arming dead-man's switch (max {max_min} min, idle {idle_min} min) …")
        # setsid fully detaches the daemon into its own session so the ssh
        # channel closes immediately (a plain `nohup … &` keeps ssh hanging).
        # The inner `sh -c` records the real python pid (echo $$ then exec).
        inner = (
            "echo $$ > /root/deadman.pid; "
            "exec env RUNPOD_API_KEY=\"$(cat /root/.rp_key)\" /workspace/venv/bin/python "
            f"/root/deadman.py --pod-id {self.pod_id} {action} "
            f"--max-runtime-min {max_min} --idle-timeout-min {idle_min} "
            f"--heartbeat {HEARTBEAT}")
        # The backgrounded command must be JUST the redirected setsid (no
        # `cd && …` wrapper) — otherwise the wrapping subshell keeps the ssh
        # channel's stdout open and the ssh call never returns.
        launch = (
            "([ -f /root/deadman.pid ] && kill \"$(cat /root/deadman.pid)\" 2>/dev/null); sleep 1; "
            f"setsid sh -c '{inner}' < /dev/null > /root/deadman.log 2>&1 & echo armed")
        self._ssh(launch, check=False, timeout=60)

    def close(self):
        """Disconnect and tear the pod down. The on-pod deadman is the backup if
        this fails (e.g. the client crashed before reaching here)."""
        if self.engine:
            try:
                self.engine.close()
            except Exception:
                pass
            self.engine = None
        if self.pod_id and not self._attach:
            ok, msg = rp.ensure_stopped(self.api_key, self.pod_id,
                                        terminate=self.terminate_when_done)
            self._emit(msg)
