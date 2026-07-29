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
import funds_guard
from remote_upscale_engine import RemoteUpscaleEngine
from remote_video_engine import RemoteVideoEngine

# Fail-safe diagnostic trail for the swallowed-error handlers (guarded import).
try:
    from debug_log import debug_log
except Exception:
    def debug_log(*_a, **_k):
        pass

DEFAULT_IMAGE = "runpod/pytorch:1.0.7-cu1281-torch291-ubuntu2204"
# esrgan (#18 B) runs region-wide on the CHEAPEST card anywhere, so its image must
# clear the OLDEST driver in the fleet. The SeedVR2 image is cu1281 (CUDA 12.8.1) and
# torch 2.9.1 hard-refuses a host driver even one PATCH below it (a real RTX 2000 Ada
# deploy failed on a 12.8.0 driver: "driver too old, found 12080"). A CUDA 12.4 image
# runs on any 12.4+ driver (essentially the whole Ampere/Ada fleet, incl. that 12.8.0
# host), and Real-ESRGAN is a light GAN that needs nothing newer.
#
# It MUST be a runpod/* image, not a vanilla pytorch/pytorch one: only RunPod's images
# boot sshd and honour the injected PUBLIC_KEY (a stock pytorch image has no SSH server,
# so scp fails at exit 255 before anything runs). runpod/pytorch's 2.4.0-cuda12.4.1
# -devel tag is a RunPod image (SSH works) AND low-CUDA (driver-compatible) AND -devel
# (ships curl + pip, which the base-image setup needs). Blackwell (sm_120) needs cu128,
# so a Blackwell esrgan run would need a cu128 image override, deferred with the rest of
# the wide-card benchmarking. Overridable via runpod.esrgan_image_name.
ESRGAN_IMAGE = "runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04"
HEARTBEAT = "/tmp/upscale_heartbeat"


def _to_float(value, default=0.0):
    """Best-effort float (config values can be strings/blank); default on junk."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _is_transient_gpu_error(text):
    """True if a worker-startup failure is a transient RunPod GPU-allocation problem
    (the rented host handed us a GPU that is busy/unavailable), not a code or config
    fault. These clear on a retry with a fresh pod, so the caller can advise that."""
    t = (text or "").lower()
    return ("cudaerrordevicesunavailable" in t
            or "busy or unavailable" in t
            or "is/are busy" in t
            or "no cuda-capable device" in t
            or "all cuda-capable devices are busy" in t)


def _ssh_base(key, port, known_hosts):
    # ServerAlive* sends a protocol keepalive every 30s: a long, SILENT command
    # (the worker-readiness loop waits minutes producing no output; a streaming
    # segment runs minutes-to-hours) otherwise looks idle to RunPod's SSH proxy,
    # which drops the channel and blanks the result. With the server alive the
    # probes are answered so the connection persists; CountMax 40 only gives up
    # after ~20 min of a truly unresponsive host.
    #
    # StrictHostKeyChecking=no is deliberate for these disposable pods (weighed in
    # recommendations item 12). `accept-new` would add MITM protection after first
    # contact, BUT RunPod hands out pods behind a finite pool of proxy (ip, port)
    # endpoints that get REUSED across pods, each with its own fresh host key. With
    # a persisted known_hosts + accept-new, a reused endpoint serving a new pod trips
    # "REMOTE HOST IDENTIFICATION HAS CHANGED" and the connection HARD-FAILS — a
    # scary, hard-to-self-resolve error for this app's non-technical audience, and a
    # worse outcome than the low, RunPod-infra-internal MITM risk it would guard. So
    # we keep `no` (accept the key, log the host) rather than gain fragile protection.
    return ["-i", key, "-p", str(port),
            "-o", "StrictHostKeyChecking=no",
            "-o", f"UserKnownHostsFile={known_hosts}",
            "-o", "ConnectTimeout=20",
            "-o", "ServerAliveInterval=30",
            "-o", "ServerAliveCountMax=40"]


class RemoteSession:
    def __init__(self, runpod_cfg, upscale_cfg, app_root, on_event=None,
                 attach=None, mode="upscale", gpu_override=None):
        """`attach` = (pod_id, host, ssh_port) to reuse a running pod instead of
        creating one (dev/validation). `on_event(msg)` is for progress lines.

        `gpu_override` (18, grouped multi-pod Start): the GPU type id (or a comma-
        separated chain) to deploy, taking precedence over the `IMGTBX_GPU_OVERRIDE`
        env. Grouped Start passes each (engine, gpu) group's own card here so one run
        can deploy a different pod per group; None keeps the env/config behaviour.

        `mode`: "upscale" (full worker: SeedVR2 + /upscale + /orient), "tag"
        (remote Tag & Rename — the worker loads only /orient so the VRAM is free
        for Ollama, which is also started on the pod and reached via a second
        tunnel exposed as `self.ollama_url`), or "video" (Video Upscaler #2 — the
        worker loads SeedVR2 and serves /video/*, and start() hands back a
        RemoteVideoEngine that streams one segment at a time)."""
        self.cfg = runpod_cfg
        self.upscale_cfg = upscale_cfg
        self.app_root = app_root
        self.mode = mode
        self.gpu_override = (gpu_override or "").strip() or None
        self.worker_mode = {"tag": "tag", "video": "video",
                            "esrgan": "esrgan"}.get(mode, "full")
        # Pod name is mode-aware so runs of different shapes never reuse each other's
        # pod: Image Upscaler + Tag & Rename share "image-toolbox-remote" (both are
        # image-side, safe to share), the SeedVR2 Video Upscaler gets "video-toolbox-
        # remote", and remote Real-ESRGAN (#18 B) gets its OWN "esrgan-toolbox-remote".
        # esrgan is deliberately NOT folded into the video pool: an esrgan pod is
        # NO-VOLUME (base python + self-downloaded weight), structurally different from
        # a volume-mounted SeedVR2 video pod, so _find_existing_pod (which matches on
        # this prefix) must never adopt one for the other. Grouped Start runs one pod at
        # a time anyway, so separate names cost nothing and remove the mismatch hazard.
        if mode == "esrgan":
            self.pod_name, self.pod_name_prefix = "esrgan-toolbox-remote", "esrgan-toolbox"
        elif mode == "video":
            self.pod_name, self.pod_name_prefix = "video-toolbox-remote", "video-toolbox"
        else:
            self.pod_name, self.pod_name_prefix = "image-toolbox-remote", "image-toolbox"
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
        # Money safety-net (funds_guard, roadmap #1). Both default OFF (0 = disabled)
        # so wiring it in costs nothing until the user opts in. Read here (not just in
        # the GUI) so a headless run honours the same config. balance_floor stops the
        # pod when the live account balance falls to/below it; session_cost_cap stops
        # it once THIS run's accrued cost crosses it.
        self.balance_floor = _to_float(runpod_cfg.get("balance_floor_usd", 0))
        self.session_cost_cap = _to_float(runpod_cfg.get("session_cost_cap_usd", 0))
        self.funds_poll_seconds = int(runpod_cfg.get("funds_poll_seconds", 60) or 60)
        self._funds_guard = None
        self._funds_tripped = False
        self.on_event = on_event or (lambda *a: None)
        self.known_hosts = os.path.join(app_root, "logs", "runpod_known_hosts")
        self._attach = attach
        self.pod_id = attach[0] if attach else None
        self.host = attach[1] if attach else None
        self.ssh_port = attach[2] if attach else None
        self.engine = None
        # Real billed rate of the deployed pod ($/h). Filled when the pod is
        # created (or read back for a reused pod); the runner forwards it to the
        # GUI's live cost readout. None until known.
        self.cost_per_hr = None
        self.worker_version = self._worker_version()

    def _worker_version(self):
        """Short hash of the worker-side code (worker.py + the modules it loads).
        The worker reports it via /health; a reused pod whose worker reports a
        different version is restarted, so it never keeps serving stale code
        after an app update. The hashed file set is MODE-aware: esrgan mode ships the
        fixed-ratio engine stack (not SeedVR2's upscale_engine), so editing one path's
        code doesn't needlessly reload a pod running the other."""
        if self.mode == "esrgan":
            files = (("pod", "worker.py"),
                     ("scripts", "fixed_ratio_engine.py"),
                     ("scripts", "esrgan_models.py"),
                     ("scripts", "video_pipeline.py"),
                     ("scripts", "runner_common.py"),
                     ("scripts", "net_ssl.py"))
        else:
            files = (("pod", "worker.py"),
                     ("scripts", "upscale_engine.py"),
                     ("scripts", "orientation.py"))
        h = hashlib.blake2b(digest_size=8)
        for rel in files:
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

    @staticmethod
    def _ssh_output(res):
        """Combined stdout+stderr of an _ssh result, guarding against either being
        None. capture_output normally yields strings, but a connection that drops
        mid-run can leave a stream uncaptured (None); concatenating None used to crash
        the error handler with a TypeError, masking the real worker.log tail."""
        return (res.stdout or "") + (res.stderr or "")

    def _scp(self, local, remote):
        args = ["scp", "-i", self.key_path, "-P", str(self.ssh_port),
                "-o", "StrictHostKeyChecking=no",
                "-o", f"UserKnownHostsFile={self.known_hosts}",
                local, f"root@{self.host}:{remote}"]
        subprocess.run(args, check=True, stdin=subprocess.DEVNULL,
                       capture_output=True, text=True)

    def _wait_for_ssh(self, timeout=180, poll=5):
        """Wait until the pod's sshd actually ACCEPTS a connection before the first
        scp. RunPod publishes the port-22 mapping (so ssh_endpoint returns a
        host:port) a little BEFORE sshd is listening, so an immediate transfer can
        hit 'Connection refused' (exit 255) and abort the run — seen on slower data
        centers. Probe with `ssh … true` until it answers; a reused pod answers on
        the first try. Returns True once reachable, False on timeout (the caller
        proceeds and lets the real transfer surface the error)."""
        deadline = time.time() + timeout
        attempt = 0
        while time.time() < deadline:
            attempt += 1
            try:
                res = subprocess.run(
                    ["ssh", *_ssh_base(self.key_path, self.ssh_port, self.known_hosts),
                     f"root@{self.host}", "true"],
                    stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
                if res.returncode == 0:
                    if attempt > 1:
                        self._emit(f"SSH ready (after {attempt} probe(s)).")
                    return True
            except Exception:                            # noqa: BLE001 (best-effort probe)
                pass
            time.sleep(poll)
        return False

    # ── lifecycle ──────────────────────────────────────────────────────────────

    def start(self):
        if not self.api_key:
            raise rp.RunPodError("No RunPod API key configured.")
        if not (self.key_path and os.path.exists(self.key_path)):
            raise rp.RunPodError(f"SSH key not found: {self.key_path}")
        self._preflight_funds()
        if not self.host:
            # Reuse a still-running app pod (faster, and avoids double-spinning);
            # mark it as attached so close() won't terminate a pod we didn't make.
            found = self._find_existing_pod()
            if found:
                self.pod_id, self.host, self.ssh_port = found
                self._attach = found
                self.cost_per_hr = self._read_pod_cost(self.pod_id)
                self._emit(f"Reusing running pod {self.pod_id} ({self.host}).")
            else:
                self._create_pod()
        # Gate the first scp on sshd actually answering (the endpoint is published
        # before sshd listens). A reused pod clears this immediately.
        self._wait_for_ssh()
        self._push_files()
        self._start_worker()
        if self.mode == "tag":
            self._start_ollama()
        self._arm_deadman()
        self._arm_funds_guard()
        self._emit("Connecting to the worker …")
        engine_cls = (RemoteVideoEngine if self.mode in ("video", "esrgan")
                      else RemoteUpscaleEngine)
        self.engine = engine_cls(
            self.host, self.ssh_port, self.key_path,
            worker_port=self.worker_port, known_hosts=self.known_hosts)
        if self.mode == "tag":
            self._open_ollama_tunnel()
            self._emit(f"Remote tagging ready (Ollama at {self.ollama_url}).")
        elif self.mode == "esrgan":
            self._emit(f"Remote Real-ESRGAN engine ready on {self.engine.device_name} "
                       f"(fixed-ratio, per-job model, no network volume).")
        elif self.mode == "video":
            vram = (" (big-VRAM: DiT + VAE kept resident on the GPU)"
                    if getattr(self.engine, "resident", False)
                    else " (DiT/VAE CPU offload)")
            self._emit(f"Remote video engine ready on {self.engine.device_name}.{vram}")
        else:
            vram = (" (big-VRAM: DiT + VAE kept resident on the GPU)"
                    if getattr(self.engine, "resident", False)
                    else " (per-image CPU offload)")
            self._emit(f"Remote engine ready on {self.engine.device_name}.{vram}")
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
            if not str(p.get("name", "")).startswith(self.pod_name_prefix):
                continue
            host, port = rp.ssh_endpoint(p)
            if host and port:
                return p.get("id"), host, port
        return None

    def _create_pod(self):
        # esrgan (#18 B) is VOLUME-FREE: the worker self-downloads its ~65 MB verified
        # weight and pip-installs spandrel on the base image, so there is no model volume
        # to bind and no region to lock to. That is what lets it deploy the cheapest card
        # anywhere (the benchmarking goal): a volume would pin it to one datacenter.
        if self.mode == "esrgan":
            vol_id, region = "", None
        else:
            vol_id = self.cfg.get("network_volume_id", "").strip()
            if not vol_id:
                raise rp.RunPodError("No runpod.network_volume_id configured.")
            region = rp.volume_region(self.api_key, vol_id)
            if not region:
                raise rp.RunPodError(f"Could not read region of volume {vol_id}.")
        # GPU choice. The GUI's live picker (filtered by VRAM, sorted by price
        # against real availability) passes its selection + fallbacks as a
        # comma-separated IMGTBX_GPU_OVERRIDE — preferred since it reflects what is
        # actually deployable now. Without it (headless / picker failed) fall back
        # to the configured defaults: a curated cheap chain for tagging (the
        # vision model needs only ~6.6 GB), the single configured card for upscale.
        # A per-session gpu_override (grouped multi-pod Start, 18) wins over the env, which
        # in turn wins over the config defaults, so one grouped run deploys a distinct card
        # per (engine, gpu) group without disturbing the single-pod env path.
        override = self.gpu_override or os.environ.get("IMGTBX_GPU_OVERRIDE", "").strip()
        if override:
            gpu_ids = [g.strip() for g in override.split(",") if g.strip()]
        elif self.mode == "tag":
            primary = self.cfg.get("tag_gpu_type_id") or rp.TAG_GPU_TYPES[0][1]
            gpu_ids = [primary] + [gid for _l, gid in rp.TAG_GPU_TYPES if gid != primary]
        else:
            gpu_ids = [self.cfg.get("gpu_type_id", "NVIDIA GeForce RTX 5090")]
        # esrgan has its OWN low-CUDA image default (see ESRGAN_IMAGE), separate from the
        # SeedVR2 image_name config so the two pod types never share the wrong image.
        if self.mode == "esrgan":
            image = self.cfg.get("esrgan_image_name") or ESRGAN_IMAGE
        else:
            image = self.cfg.get("image_name") or DEFAULT_IMAGE
        spec = {
            "name": self.pod_name,
            "imageName": image,
            "gpuTypeIds": gpu_ids,
            "gpuCount": 1, "cloudType": "SECURE",
            "containerDiskInGb": int(self.cfg.get("container_disk_gb", 30)),
            "ports": ["22/tcp"],
        }
        # Only pin a datacenter + bind a volume when there IS one (SeedVR2 modes). A
        # volume-free esrgan pod deploys region-wide, so it omits both (passing a
        # [None] dataCenterId would fail the deploy).
        if region:
            spec["dataCenterIds"] = [region]
        if vol_id:
            spec["networkVolumeId"] = vol_id
        # Inject the app's public key so the pod trusts it at boot (RunPod base
        # images append $PUBLIC_KEY to authorized_keys) — no account-level
        # registration needed. Additive: account keys still work too.
        if self.public_key:
            spec["env"] = {"PUBLIC_KEY": self.public_key}
        chain_note = "" if len(gpu_ids) == 1 else " (+ fallbacks)"
        where = f"in {region}" if region else "region-wide (no volume)"
        self._emit(f"Creating pod ({gpu_ids[0]}{chain_note} {where}) …")

        def ev(kind, attempt, pod_id, info):
            if kind == "created":
                self.pod_id = pod_id
                self._emit(f"Pod {pod_id} created on {info} (attempt {attempt}); waiting for deploy …")
            elif kind == "bad":
                # No "trying the next option": a capacity error stops here (an
                # upscale/video run uses only the picked card, 0.4.0), and even where a
                # retry does follow (deploy timeout, or tag mode's fallback chain) the
                # next "Pod … created" line shows it — so we never promise a retry that
                # won't happen.
                self._emit(f"Pod start failed ({info}).")
            # "giveup" is intentionally not surfaced here: main() prints one clean
            # "Run failed: …" line with the same cause, so a second line is just noise.

        pod = rp.create_pod_resilient(self.api_key, spec, attempts=3,
                                      deploy_timeout=240, poll=8, on_event=ev)
        self.pod_id = pod.get("id")
        self.host, self.ssh_port = rp.ssh_endpoint(pod)
        # The deploy/running pod dict carries costPerHr — the REAL billed rate of
        # whichever GPU in the fallback chain actually deployed, so the live cost
        # readout reflects the bill, not the picked card.
        self.cost_per_hr = pod.get("costPerHr")
        if self.cost_per_hr is None:
            self.cost_per_hr = self._read_pod_cost(self.pod_id)

    def _read_pod_cost(self, pod_id):
        """Best-effort $/h for an existing pod (reused, or when the deploy
        response omitted it). Fail-safe: None on any error."""
        if not pod_id:
            return None
        try:
            return rp.get_pod(self.api_key, pod_id).get("costPerHr")
        except Exception:                                # noqa: BLE001 (best-effort)
            return None

    def _push_files(self):
        self._emit("Uploading worker + dead-man's switch to the pod …")
        if self.mode == "esrgan":
            # The fixed-ratio engine stack (#18 B): the engine, its torch-free model
            # catalog + verified downloader (net_ssl for the HTTPS trust), the ffmpeg
            # container helpers, and runner_common (is_oom_error). No SeedVR2 modules.
            scripts = ("fixed_ratio_engine.py", "esrgan_models.py", "video_pipeline.py",
                       "runner_common.py", "net_ssl.py")
        else:
            # orientation.py rides along so the worker can run the auto-straighten CNN
            # on the pod (remote #1 option B); the local side stays torch-free.
            # exif_copy.py: the pod's engine writes the output file, so the metadata
            # copy (#13a) has to happen there too - the local side only sees the
            # finished bytes. The upscale config section (with copy_metadata) is
            # already pushed as worker_settings.json below.
            scripts = ("upscale_engine.py", "orientation.py", "exif_copy.py")
        for name in scripts:
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

    def _worker_already_current(self, wp):
        """True if a healthy worker of the CURRENT version AND mode is already on the
        pod (reuse it, skip the reload). Otherwise emit why it must reload and return
        False. Shared by the SeedVR2 and esrgan launch paths."""
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
                return True
            if running_mode != self.worker_mode:
                self._emit(f"Worker on the pod is in '{running_mode}' mode — "
                           f"reloading it in '{self.worker_mode}' mode.")
            else:
                self._emit("Worker on the pod is a different version — reloading it.")
        return False

    def _verify_worker_launch(self, res):
        """Inspect the launch ssh result and raise a helpful RunPodError if the
        worker did not come up. Shared by both launch paths."""
        out = self._ssh_output(res)
        if "WORKER_FAILED" in out or res.returncode != 0:
            tail = out[-1200:]
            # The launch connection can still drop during a long first-load,
            # blanking its captured output; pull the worker log over a FRESH
            # connection (the pod is up until teardown) so a failure is never
            # diagnosed blind.
            if "Traceback" not in tail and "WORKER_" not in tail:
                try:
                    log = self._ssh("tail -n 60 /root/worker.log 2>/dev/null",
                                    check=False, timeout=60)
                    extra = self._ssh_output(log).strip()
                    if extra:
                        tail = (tail + "\n--- worker.log ---\n" + extra)[-2000:]
                except Exception:                        # noqa: BLE001
                    pass
            if _is_transient_gpu_error(tail):
                raise rp.RunPodError(
                    "The rented pod's GPU was busy or unavailable during model load "
                    "(CUDA cudaErrorDevicesUnavailable). That is a bad GPU allocation "
                    "on RunPod's side, not your run or the 4K target. The pod was "
                    "terminated. Press Start to retry: a fresh pod usually lands on a "
                    "healthy GPU (or pick a different card with the picker's refresh).\n\n"
                    + tail)
            raise rp.RunPodError("Worker failed to become ready:\n" + tail)

    def _health_wait_snippet(self, wp):
        """The shell tail that polls /health, short-circuits on a dead worker
        process, and reports WORKER_FAILED with the log tail. Shared verbatim by
        both launch commands (see _start_worker for the design rationale)."""
        return (
            f"for i in $(seq 1 180); do "
            f"curl -sf localhost:{wp}/health >/dev/null 2>&1 && break; "
            "PID=$(cat /root/worker.pid 2>/dev/null); "
            "if [ -n \"$PID\" ] && ! kill -0 \"$PID\" 2>/dev/null; then echo WORKER_DIED; break; fi; "
            "[ $((i % 12)) -eq 0 ] && echo \"worker-wait $((i * 5))s ...\"; "
            "sleep 5; done; "
            f"curl -sf localhost:{wp}/health >/dev/null 2>&1 || "
            "{ echo WORKER_FAILED; tail -n 40 /root/worker.log; exit 1; }")

    def _start_worker(self):
        if self.mode == "esrgan":
            return self._start_esrgan_worker()
        wp = self.worker_port
        # Reuse a worker only if it is healthy AND running the CURRENT code
        # version, otherwise a reused pod would keep serving a stale worker
        # after an app update (e.g. the telemetry / straighten code changed).
        # A matching version skips the needless ~97 s model reload.
        if self._worker_already_current(wp):
            return
        self._emit("Starting the resident worker (first model load is slow) …")
        # The launch must be `setsid sh -c '…' </dev/null >log 2>&1 &` with the
        # redirect on the backgrounded command — a `cd && nohup … &` wrapper
        # leaves the ssh channel open and the call hangs (worker loads, ssh never
        # returns). TORCH_HOME points at the volume so the auto-straighten CNN
        # weights (cached by provision.sh) are found without re-downloading.
        inner = (
            "echo $$ > /root/worker.pid; "
            # PATH carries the volume-cached ffmpeg so the worker's H.265 10-bit
            # writer (FFMPEGVideoWriter calls a bare `ffmpeg`) resolves it; a system
            # ffmpeg in /usr/bin still wins if the image ships one.
            "exec env TORCH_HOME=/workspace/models/torch PATH=/workspace/ffmpeg:$PATH "
            "/workspace/venv/bin/python /root/worker.py "
            "--repo-dir /workspace/seedvr2 --model-dir /workspace/models/seedvr2 "
            f"--settings /root/worker_settings.json --port {wp} --heartbeat {HEARTBEAT} "
            f"--worker-version {self.worker_version} --mode {self.worker_mode}")
        launch = (
            "([ -f /root/worker.pid ] && kill \"$(cat /root/worker.pid)\" 2>/dev/null); sleep 1; "
            # Guarantee ffmpeg for the H.265 10-bit writer: a system one is fine,
            # else the volume cache (provision.sh), else fetch a static build to the
            # volume once (best-effort; the worker fails loudly if it's truly absent).
            # The build has no strong published hash, so gate it FUNCTIONALLY: run
            # `ffmpeg -version` on the extracted binary and cache it to the PERSISTENT
            # volume only if it runs. Otherwise a truncated/corrupt download would be
            # cached once and break every future run on this volume (same rationale as
            # bootstrap's ffprobe version check).
            "if ! command -v ffmpeg >/dev/null 2>&1 && [ ! -x /workspace/ffmpeg/ffmpeg ]; then "
            "echo 'caching static ffmpeg to the volume...'; mkdir -p /workspace/ffmpeg; "
            "curl -fsSL https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz "
            "-o /tmp/ff.txz && tar -xJf /tmp/ff.txz -C /tmp && "
            "if /tmp/ffmpeg-*-amd64-static/ffmpeg -version >/dev/null 2>&1; then "
            "cp /tmp/ffmpeg-*-amd64-static/ffmpeg /tmp/ffmpeg-*-amd64-static/ffprobe /workspace/ffmpeg/ && "
            "chmod +x /workspace/ffmpeg/ffmpeg /workspace/ffmpeg/ffprobe; "
            "else echo 'ffmpeg failed its sanity check - not caching a bad build'; fi; "
            "rm -rf /tmp/ff.txz /tmp/ffmpeg-*-amd64-static; fi; "
            "rm -f /root/worker.pid; "
            f"setsid sh -c '{inner}' < /dev/null > /root/worker.log 2>&1 & "
            # Poll /health, but bail the moment the worker PROCESS dies (e.g. a CUDA
            # crash at model load) instead of polling the full 10 min on a dead pod.
            # Only trip on a written-then-dead pid (a missing pid = not booted yet).
            # 180 x 5s = 15 min: an UNCACHED model (first use of a new dit_model)
            # must DOWNLOAD then load before /health answers, which can outrun a
            # 10-min window; a dead worker still short-circuits via WORKER_DIED, so
            # the longer ceiling only costs time when it is genuinely still loading.
            # The periodic echo gives the channel real traffic (belt-and-suspenders
            # with ServerAlive) so the silent wait can't be proxy-dropped.
            + self._health_wait_snippet(wp))
        res = self._ssh(launch, check=False, timeout=1080)
        self._verify_worker_launch(res)

    def _start_esrgan_worker(self):
        """Start the fixed-ratio Real-ESRGAN worker (#18 B) on a VOLUME-FREE pod.
        Unlike the SeedVR2 path there is no provisioned venv or model volume, so this
        stands the worker up on the base image itself: find the image's torch python,
        pip-install spandrel (+ certifi for the verified weight download), fetch a
        static ffmpeg to /root/ffmpeg, then launch worker.py --mode esrgan. The ~65 MB
        model is self-downloaded per job by the worker, so nothing model-side happens
        here and /health answers as soon as the deps are in place (fast cold start)."""
        wp = self.worker_port
        if self._worker_already_current(wp):
            return
        self._emit("Setting up the Real-ESRGAN worker on the base image "
                   "(python + spandrel + ffmpeg; no volume) …")
        # The base RunPod pytorch image already ships torch; the model is self-
        # downloaded, so this launch only needs spandrel + certifi + ffmpeg. PYBIN is
        # resolved on the pod (the image's python layout varies) and EXPORTED so the
        # backgrounded setsid inherits it. --model-dir is unused in esrgan mode (the
        # weight goes to /root/models via ensure_model), passed only to satisfy argparse.
        inner = (
            "echo $$ > /root/worker.pid; "
            "exec env PATH=/root/ffmpeg:$PATH \"$PYBIN\" /root/worker.py "
            "--repo-dir /root --model-dir /root "
            f"--settings /root/worker_settings.json --port {wp} --heartbeat {HEARTBEAT} "
            f"--worker-version {self.worker_version} --mode esrgan")
        launch = (
            "([ -f /root/worker.pid ] && kill \"$(cat /root/worker.pid)\" 2>/dev/null); sleep 1; "
            # Find a python that can import torch (the base image's env layout varies:
            # a /venv, the system python3, or plain `python`). Bail loudly if none:
            # a no-torch pod could never run the engine.
            "PYBIN=''; for c in /venv/bin/python python python3 /usr/bin/python3; do "
            "\"$c\" -c 'import torch' >/dev/null 2>&1 && { PYBIN=\"$c\"; break; }; done; "
            "if [ -z \"$PYBIN\" ]; then echo WORKER_FAILED; echo 'no torch-capable python on the base image'; exit 1; fi; "
            "export PYBIN; echo \"using python: $PYBIN\"; "
            # Install spandrel (the model loader) + certifi (HTTPS trust for the weight
            # download) into THAT python, only if missing. pip logs to a file so a
            # failure is diagnosable without flooding the launch channel.
            "\"$PYBIN\" -c 'import spandrel' >/dev/null 2>&1 || { echo 'installing spandrel + certifi ...'; "
            "\"$PYBIN\" -m pip install --no-input spandrel certifi >/root/pip.log 2>&1 || "
            "{ echo WORKER_FAILED; echo '--- pip.log ---'; tail -n 30 /root/pip.log; exit 1; }; }; "
            # Fetch a static ffmpeg to /root/ffmpeg (no volume to cache it on). Same
            # FUNCTIONAL gate as the SeedVR2 path: only keep the binary if it runs, so a
            # truncated download can't poison the pod. A system ffmpeg still wins.
            "if ! command -v ffmpeg >/dev/null 2>&1 && [ ! -x /root/ffmpeg/ffmpeg ]; then "
            "echo 'fetching static ffmpeg ...'; mkdir -p /root/ffmpeg; "
            "FFURL=https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz; "
            "{ curl -fsSL \"$FFURL\" -o /tmp/ff.txz || wget -qO /tmp/ff.txz \"$FFURL\"; } && "
            "tar -xJf /tmp/ff.txz -C /tmp && "
            "if /tmp/ffmpeg-*-amd64-static/ffmpeg -version >/dev/null 2>&1; then "
            "cp /tmp/ffmpeg-*-amd64-static/ffmpeg /tmp/ffmpeg-*-amd64-static/ffprobe /root/ffmpeg/ && "
            "chmod +x /root/ffmpeg/ffmpeg /root/ffmpeg/ffprobe; "
            "else echo 'ffmpeg failed its sanity check - not keeping a bad build'; fi; "
            "rm -rf /tmp/ff.txz /tmp/ffmpeg-*-amd64-static; fi; "
            "rm -f /root/worker.pid; "
            f"setsid sh -c '{inner}' < /dev/null > /root/worker.log 2>&1 & "
            + self._health_wait_snippet(wp))
        # The pip install + ffmpeg fetch are the long pole here (no 16 GB model load),
        # so the same generous ceiling as the SeedVR2 launch is ample.
        res = self._ssh(launch, check=False, timeout=1080)
        self._verify_worker_launch(res)

    def _start_ollama(self):
        """Start `ollama serve` on the pod (remote Tag & Rename), serving the
        vision model from the network volume. The models live on the volume
        (provision.sh) but the ollama RUNTIME normally does not, so resolve it from
        PATH → a volume cache → an on-the-fly install. Skips if already up (reused
        pod). setsid + redirect-on-the-backgrounded-command so the ssh call
        returns (same rule as the worker launch).

        The cached binary is only trusted when its companion `llama-server` lib is
        also cached (/workspace/ollama/lib/ollama) — an older volume cached just
        the binary, which can't start the model server and 500s every inference.
        Requiring the lib makes such a volume self-heal: it falls through to a
        fresh install (which ships the full runtime) instead of serving a broken
        ollama."""
        self._emit("Starting Ollama on the pod (vision model from the volume) …")
        launch = (
            "if curl -sf localhost:11434/api/version >/dev/null 2>&1; then echo OLLAMA_UP; exit 0; fi; "
            "OLLAMA_BIN=$(command -v ollama || true); "
            "{ [ -z \"$OLLAMA_BIN\" ] && [ -x /workspace/ollama/bin/ollama ] && [ -e /workspace/ollama/lib/ollama/llama-server ]; } && OLLAMA_BIN=/workspace/ollama/bin/ollama; "
            "if [ -z \"$OLLAMA_BIN\" ]; then curl -fsSL https://ollama.com/install.sh | sh >/root/ollama_install.log 2>&1; "
            "OLLAMA_BIN=$(command -v ollama || echo /usr/local/bin/ollama); fi; "
            "export OLLAMA_MODELS=/workspace/models/ollama OLLAMA_HOST=127.0.0.1:11434; "
            "setsid \"$OLLAMA_BIN\" serve </dev/null >/root/ollama.log 2>&1 & "
            "for i in $(seq 1 90); do curl -sf localhost:11434/api/version >/dev/null 2>&1 && break; sleep 2; done; "
            "curl -sf localhost:11434/api/version >/dev/null 2>&1 || { echo OLLAMA_FAILED; tail -n 20 /root/ollama.log; exit 1; }")
        res = self._ssh(launch, check=False, timeout=600)
        out = self._ssh_output(res)
        if "OLLAMA_FAILED" in out or res.returncode != 0:
            raise rp.RunPodError(
                "Ollama failed to start on the pod:\n" + out[-800:])

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
        # Default 0 = no hard ceiling (the idle timeout is the real switch); this
        # matches config.json, the RunPod tab and docs. A missing key must not
        # silently impose a 720-min cap that cuts a long batch off mid-run (item 8).
        max_min = int(self.cfg.get("max_runtime_minutes", 0))
        idle_min = int(self.cfg.get("idle_timeout_minutes", 15))
        action = "--terminate" if self.terminate_when_done else ""
        self._emit(f"Arming dead-man's switch (max {max_min} min, idle {idle_min} min) …")
        # setsid fully detaches the daemon into its own session so the ssh
        # channel closes immediately (a plain `nohup … &` keeps ssh hanging).
        # The inner `sh -c` records the real python pid (echo $$ then exec).
        # deadman.py is stdlib-only, so a volume-free esrgan pod (no /workspace/venv)
        # runs it on the base image's python3; SeedVR2 modes use the provisioned venv.
        dm_python = "python3" if self.mode == "esrgan" else "/workspace/venv/bin/python"
        inner = (
            "echo $$ > /root/deadman.pid; "
            f"exec env RUNPOD_API_KEY=\"$(cat /root/.rp_key)\" {dm_python} "
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

    def _preflight_funds(self):
        """Refuse to START when finishing this run would drop the balance below the
        floor (roadmap #1). Runs before any pod is created, so a run the account
        can't afford never spins a billed pod. The GUI can set IMGTBX_RUN_ESTIMATE
        ($) so the check uses the real queue estimate; without it the test reduces
        to 'is the balance already at/below the floor'. Fail-safe: an unreadable
        balance never blocks (the in-run guard is the backstop)."""
        if not self.balance_floor or self.balance_floor <= 0:
            return
        info = rp.account_balance(self.api_key)
        bal = (info or {}).get("balance")
        est = _to_float(os.environ.get("IMGTBX_RUN_ESTIMATE"), 0.0)
        blocked, reason = funds_guard.start_blocked(bal, est, self.balance_floor)
        if blocked:
            raise rp.RunPodError(
                "Run refused by the funds safety-net: " + reason
                + ". Add funds, or lower the floor in Settings -> Remote.")

    def _arm_funds_guard(self):
        """Start the local money safety-net for this run (roadmap #1). Inert unless
        a floor or cap is configured. Polls the account balance and, once the run's
        accrued cost crosses the cap OR the balance falls to the floor, STOPS the
        pod — the run then hits the same 'pod stopped mid-run' path the on-pod
        dead-man's switch uses (resume cache saved, continue later). Fail-safe: any
        problem arming it just leaves the run unguarded, never blocks it."""
        try:
            guard = funds_guard.FundsGuard(
                fetch_balance=lambda: rp.account_balance(self.api_key),
                cost_per_hr=self.cost_per_hr,
                floor=self.balance_floor,
                cap=self.session_cost_cap,
                poll_seconds=self.funds_poll_seconds,
                on_trip=self._on_funds_trip)
            self._funds_guard = guard
            if not guard.active:
                return
            # One up-front runway line so the user sees the guard is watching.
            info = rp.account_balance(self.api_key)
            bal = (info or {}).get("balance")
            limits = []
            if self.session_cost_cap > 0:
                limits.append(f"cap ${self.session_cost_cap:.2f}/run")
            if self.balance_floor > 0:
                limits.append(f"floor ${self.balance_floor:.2f}")
            runway = ""
            if bal is not None and self.cost_per_hr:
                hrs = funds_guard.hours_until_depleted(bal, self.cost_per_hr)
                runway = (f"; balance ${bal:.2f}"
                          + (f", ~{hrs:.1f}h at ${self.cost_per_hr:.2f}/h" if hrs else ""))
            self._emit(f"Funds guard armed ({', '.join(limits)}){runway}.")
            guard.start()
        except Exception as exc:                         # noqa: BLE001 (fail-safe)
            self._emit(f"Funds guard could not start (continuing unguarded): {exc}")

    def _on_funds_trip(self, reason):
        """Balance floor / cost cap crossed while billing: stop the pod now. The
        runner detects the pod is gone and ends the run cleanly (resume cache
        saved). Guarded so it fires at most once."""
        if self._funds_tripped:
            return
        self._funds_tripped = True
        self._emit(f"FUNDS SAFETY: stopping the pod — {reason}. "
                   f"The resume cache is saved; continue later when funded.")
        try:
            _ok, msg = rp.ensure_stopped(self.api_key, self.pod_id,
                                         terminate=self.terminate_when_done)
            self._emit(msg)
        except Exception as exc:                         # noqa: BLE001 (fail-safe)
            self._emit(f"Funds safety could not stop the pod ({exc}); the on-pod "
                       f"dead-man's switch will stop it on the idle timeout.")

    def close(self, stop_pod=None):
        """Disconnect and tear the pod down. The on-pod deadman is the backup if
        this fails (e.g. the client crashed before reaching here).

        `stop_pod` overrides the default teardown decision (the GUI's Stop modal
        uses it): None = default (stop only a pod we created, never a reused one);
        True = stop the pod now regardless (even a reused one); False = leave the
        pod running (the dead-man's switch stops it on the idle timeout)."""
        if self._funds_guard:
            try:
                self._funds_guard.stop()
            except Exception as exc:
                debug_log("RemoteSession.close: funds_guard.stop", exc=exc)
            self._funds_guard = None
        if self.engine:
            try:
                self.engine.close()
            except Exception as exc:
                debug_log("RemoteSession.close: engine.close", exc=exc)
            self.engine = None
        if self._ollama_tunnel and self._ollama_tunnel.poll() is None:
            try:
                self._ollama_tunnel.terminate()
                self._ollama_tunnel.wait(timeout=5)
            except Exception as exc:
                # A stranded ssh tunnel process is worth a trail (money-adjacent
                # cleanup path).
                debug_log("RemoteSession.close: ollama tunnel terminate", exc=exc)
            self._ollama_tunnel = None
        if not self.pod_id:
            return
        do_stop = (not self._attach) if stop_pod is None else stop_pod
        if do_stop:
            # Guarded like every other step above: close() runs from the runners'
            # finally block, so an API/network error here must not replace the run's
            # real outcome with a secondary traceback (and skip _close_log). The on-pod
            # dead-man's switch stops the pod on the idle timeout regardless.
            try:
                _ok, msg = rp.ensure_stopped(self.api_key, self.pod_id,
                                             terminate=self.terminate_when_done)
                self._emit(msg)
            except Exception as exc:                     # noqa: BLE001 (fail-safe)
                self._emit(f"Could not stop the pod ({exc}); the dead-man's switch "
                           f"will stop it on the idle timeout.")
                debug_log("RemoteSession.close: ensure_stopped", exc=exc)
        else:
            self._emit("Leaving the remote pod running — the dead-man's switch "
                       "will stop it after the idle timeout.")
