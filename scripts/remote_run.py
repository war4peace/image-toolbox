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
import hashlib
import tempfile
import subprocess

import runpod_client as rp
import ssh_setup
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
                 attach=None, mode="upscale"):
        """`attach` = (pod_id, host, ssh_port) to reuse a running pod instead of
        creating one (dev/validation). `on_event(msg)` is for progress lines.

        `mode`: "upscale" (full worker: SeedVR2 + /upscale + /orient) or "tag"
        (remote Tag & Rename — the worker loads only /orient so the VRAM is free
        for Ollama, which is also started on the pod and reached via a second
        tunnel exposed as `self.ollama_url`)."""
        self.cfg = runpod_cfg
        self.upscale_cfg = upscale_cfg
        self.app_root = app_root
        self.mode = mode
        self.worker_mode = "tag" if mode == "tag" else "full"
        self.ollama_url = None        # set in tag mode (local end of the tunnel)
        self._ollama_tunnel = None
        self.api_key = runpod_cfg.get("api_key", "")
        # Empty/unset ssh_key_path → the app's managed default key, so a user who
        # never touched config.json still has a usable key (zero-config, #1).
        self.key_path = (os.path.expandvars(runpod_cfg.get("ssh_key_path", ""))
                         or ssh_setup.default_key_path())
        # Public half of the app's key — handed to the pod via PUBLIC_KEY so SSH
        # works without the user registering it on the RunPod website (zero-config
        # onboarding, #1). None falls back to account-level keys (back-compat).
        self.public_key = ssh_setup.read_public_key(self.key_path) if self.key_path else None
        self.worker_port = int(runpod_cfg.get("worker_port", 8200))
        self.terminate_when_done = bool(runpod_cfg.get("terminate_when_done", True))
        self.on_event = on_event or (lambda *a: None)
        self.known_hosts = os.path.join(app_root, "logs", "runpod_known_hosts")
        self._attach = attach
        self.pod_id = attach[0] if attach else None
        self.host = attach[1] if attach else None
        self.ssh_port = attach[2] if attach else None
        self.engine = None
        self.worker_version = self._worker_version()

    def _worker_version(self):
        """Short hash of the worker-side code (worker.py + the modules it loads).
        The worker reports it via /health; a reused pod whose worker reports a
        different version is restarted, so it never keeps serving stale code
        after an app update."""
        h = hashlib.blake2b(digest_size=8)
        for rel in (("pod", "worker.py"),
                    ("scripts", "upscale_engine.py"),
                    ("scripts", "orientation.py")):
            try:
                with open(os.path.join(self.app_root, *rel), "rb") as f:
                    h.update(f.read())
            except OSError:
                pass
        return h.hexdigest()

    # ── ssh / scp ──────────────────────────────────────────────────────────────

    # stdin=DEVNULL is essential: under the GUI, batch_upscale's stdin is a pipe
    # (the GUI sends pause/quit through it). ssh/scp inherit that pipe and BLOCK
    # reading it — the run hangs on the first ssh/scp. Detaching stdin fixes it.
    def _ssh(self, command, check=True, timeout=None):
        args = ["ssh", *_ssh_base(self.key_path, self.ssh_port, self.known_hosts),
                f"root@{self.host}", command]
        return subprocess.run(args, check=check, timeout=timeout,
                              stdin=subprocess.DEVNULL, capture_output=True, text=True)

    def _scp(self, local, remote):
        args = ["scp", "-i", self.key_path, "-P", str(self.ssh_port),
                "-o", "StrictHostKeyChecking=no",
                "-o", f"UserKnownHostsFile={self.known_hosts}",
                local, f"root@{self.host}:{remote}"]
        subprocess.run(args, check=True, stdin=subprocess.DEVNULL,
                       capture_output=True, text=True)

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
        if self.mode == "tag":
            self._start_ollama()
        self._arm_deadman()
        self._emit("Connecting to the worker …")
        self.engine = RemoteUpscaleEngine(
            self.host, self.ssh_port, self.key_path,
            worker_port=self.worker_port, known_hosts=self.known_hosts)
        if self.mode == "tag":
            self._open_ollama_tunnel()
            self._emit(f"Remote tagging ready (Ollama at {self.ollama_url}).")
        else:
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
        # GPU choice: upscaling needs the heavy card; tagging needs only ~6.6 GB,
        # so it uses a cheap card with an ORDERED FALLBACK CHAIN (the configured
        # tag_gpu_type_id first, then the rest of the curated low-tier list) so a
        # tag run still starts when the preferred card is sold out.
        if self.mode == "tag":
            primary = self.cfg.get("tag_gpu_type_id") or rp.TAG_GPU_TYPES[0][1]
            gpu_ids = [primary] + [gid for _l, gid in rp.TAG_GPU_TYPES if gid != primary]
        else:
            gpu_ids = [self.cfg.get("gpu_type_id", "NVIDIA GeForce RTX 5090")]
        spec = {
            "name": "image-toolbox-remote",
            "imageName": self.cfg.get("image_name") or DEFAULT_IMAGE,
            "gpuTypeIds": gpu_ids,
            "gpuCount": 1, "cloudType": "SECURE",
            "dataCenterIds": [region], "networkVolumeId": vol_id,
            "containerDiskInGb": int(self.cfg.get("container_disk_gb", 30)),
            "ports": ["22/tcp"],
        }
        # Inject the app's public key so the pod trusts it at boot (RunPod base
        # images append $PUBLIC_KEY to authorized_keys) — no account-level
        # registration needed. Additive: account keys still work too.
        if self.public_key:
            spec["env"] = {"PUBLIC_KEY": self.public_key}
        chain_note = "" if len(gpu_ids) == 1 else " (+ fallbacks)"
        self._emit(f"Creating pod ({gpu_ids[0]}{chain_note} in {region}) …")

        def ev(kind, attempt, pod_id, info):
            if kind == "created":
                self.pod_id = pod_id
                self._emit(f"Pod {pod_id} created on {info} (attempt {attempt}); waiting for deploy …")
            elif kind == "bad":
                self._emit(f"Pod start failed ({info}); trying the next option …")
            elif kind == "giveup":
                self._emit(f"Gave up creating a pod: {info}")

        pod = rp.create_pod_resilient(self.api_key, spec, attempts=3,
                                      deploy_timeout=240, poll=8, on_event=ev)
        self.pod_id = pod.get("id")
        self.host, self.ssh_port = rp.ssh_endpoint(pod)

    def _push_files(self):
        self._emit("Uploading worker + dead-man's switch to the pod …")
        # orientation.py rides along so the worker can run the auto-straighten CNN
        # on the pod (remote #1 option B) — the local side stays torch-free.
        for name in ("upscale_engine.py", "orientation.py"):
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
        wp = self.worker_port
        # Reuse a worker only if it is healthy AND running the CURRENT code
        # version — otherwise a reused pod would keep serving a stale worker
        # after an app update (e.g. the telemetry / straighten code changed).
        # A matching version skips the needless ~97 s model reload.
        health = self._ssh(f"curl -sf localhost:{wp}/health || true",
                           check=False, timeout=30)
        if health.returncode == 0 and health.stdout.strip():
            try:
                info = json.loads(health.stdout)
                running = info.get("version")
                running_mode = info.get("mode", "full")
            except ValueError:
                running, running_mode = None, None
            if running and running == self.worker_version and running_mode == self.worker_mode:
                self._emit("Reusing the healthy worker already on the pod "
                           "(matching version + mode).")
                return
            if running_mode != self.worker_mode:
                self._emit(f"Worker on the pod is in '{running_mode}' mode — "
                           f"reloading it in '{self.worker_mode}' mode.")
            else:
                self._emit("Worker on the pod is a different version — reloading it.")
        self._emit("Starting the resident worker (first model load is slow) …")
        # The launch must be `setsid sh -c '…' </dev/null >log 2>&1 &` with the
        # redirect on the backgrounded command — a `cd && nohup … &` wrapper
        # leaves the ssh channel open and the call hangs (worker loads, ssh never
        # returns). TORCH_HOME points at the volume so the auto-straighten CNN
        # weights (cached by provision.sh) are found without re-downloading.
        inner = (
            "echo $$ > /root/worker.pid; "
            "exec env TORCH_HOME=/workspace/models/torch "
            "/workspace/venv/bin/python /root/worker.py "
            "--repo-dir /workspace/seedvr2 --model-dir /workspace/models/seedvr2 "
            f"--settings /root/worker_settings.json --port {wp} --heartbeat {HEARTBEAT} "
            f"--worker-version {self.worker_version} --mode {self.worker_mode}")
        launch = (
            "([ -f /root/worker.pid ] && kill \"$(cat /root/worker.pid)\" 2>/dev/null); sleep 1; "
            f"setsid sh -c '{inner}' < /dev/null > /root/worker.log 2>&1 & "
            f"for i in $(seq 1 120); do curl -sf localhost:{wp}/health >/dev/null 2>&1 && break; sleep 5; done; "
            f"curl -sf localhost:{wp}/health >/dev/null 2>&1 || "
            "{ echo WORKER_FAILED; tail -n 20 /root/worker.log; exit 1; }")
        res = self._ssh(launch, check=False, timeout=900)
        if "WORKER_FAILED" in (res.stdout + res.stderr) or res.returncode != 0:
            raise rp.RunPodError(
                "Worker failed to become ready:\n" + (res.stdout + res.stderr)[-800:])

    def _start_ollama(self):
        """Start `ollama serve` on the pod (remote Tag & Rename), serving the
        vision model from the network volume. The models live on the volume
        (provision.sh) but the ollama BINARY normally does not, so resolve it from
        PATH → a volume cache → an on-the-fly install. Skips if already up (reused
        pod). setsid + redirect-on-the-backgrounded-command so the ssh call
        returns (same rule as the worker launch)."""
        self._emit("Starting Ollama on the pod (vision model from the volume) …")
        launch = (
            "if curl -sf localhost:11434/api/version >/dev/null 2>&1; then echo OLLAMA_UP; exit 0; fi; "
            "OLLAMA_BIN=$(command -v ollama || true); "
            "{ [ -z \"$OLLAMA_BIN\" ] && [ -x /workspace/ollama/bin/ollama ]; } && OLLAMA_BIN=/workspace/ollama/bin/ollama; "
            "if [ -z \"$OLLAMA_BIN\" ]; then curl -fsSL https://ollama.com/install.sh | sh >/root/ollama_install.log 2>&1; "
            "OLLAMA_BIN=$(command -v ollama || echo /usr/local/bin/ollama); fi; "
            "export OLLAMA_MODELS=/workspace/models/ollama OLLAMA_HOST=127.0.0.1:11434; "
            "setsid \"$OLLAMA_BIN\" serve </dev/null >/root/ollama.log 2>&1 & "
            "for i in $(seq 1 90); do curl -sf localhost:11434/api/version >/dev/null 2>&1 && break; sleep 2; done; "
            "curl -sf localhost:11434/api/version >/dev/null 2>&1 || { echo OLLAMA_FAILED; tail -n 20 /root/ollama.log; exit 1; }")
        res = self._ssh(launch, check=False, timeout=600)
        if "OLLAMA_FAILED" in (res.stdout + res.stderr) or res.returncode != 0:
            raise rp.RunPodError(
                "Ollama failed to start on the pod:\n" + (res.stdout + res.stderr)[-800:])

    def _open_ollama_tunnel(self):
        """Open an ssh -L tunnel to the pod's Ollama (11434) and set
        self.ollama_url to the local end, so tag_and_rename can point its Ollama
        URL at localhost."""
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        local = s.getsockname()[1]
        s.close()
        opts = ["-i", self.key_path, "-p", str(self.ssh_port),
                "-o", "StrictHostKeyChecking=no",
                "-o", f"UserKnownHostsFile={self.known_hosts}",
                "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=30",
                "-N", "-L", f"{local}:127.0.0.1:11434"]
        args = ["ssh", *opts, f"root@{self.host}"]
        self._ollama_tunnel = subprocess.Popen(
            args, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.time() + 60
        while time.time() < deadline:
            if self._ollama_tunnel.poll() is not None:
                raise rp.RunPodError("Ollama SSH tunnel exited before it was reachable.")
            try:
                with socket.create_connection(("127.0.0.1", local), timeout=3):
                    self.ollama_url = f"http://127.0.0.1:{local}"
                    return
            except OSError:
                time.sleep(2)
        raise rp.RunPodError("Ollama tunnel was not reachable within 60s.")

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

    def close(self, stop_pod=None):
        """Disconnect and tear the pod down. The on-pod deadman is the backup if
        this fails (e.g. the client crashed before reaching here).

        `stop_pod` overrides the default teardown decision (the GUI's Stop modal
        uses it): None = default (stop only a pod we created, never a reused one);
        True = stop the pod now regardless (even a reused one); False = leave the
        pod running (the dead-man's switch stops it on the idle timeout)."""
        if self.engine:
            try:
                self.engine.close()
            except Exception:
                pass
            self.engine = None
        if self._ollama_tunnel and self._ollama_tunnel.poll() is None:
            try:
                self._ollama_tunnel.terminate()
                self._ollama_tunnel.wait(timeout=5)
            except Exception:
                pass
            self._ollama_tunnel = None
        if not self.pod_id:
            return
        do_stop = (not self._attach) if stop_pod is None else stop_pod
        if do_stop:
            ok, msg = rp.ensure_stopped(self.api_key, self.pod_id,
                                        terminate=self.terminate_when_done)
            self._emit(msg)
        else:
            self._emit("Leaving the remote pod running — the dead-man's switch "
                       "will stop it after the idle timeout.")
