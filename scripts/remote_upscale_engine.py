"""
remote_upscale_engine.py
------------------------
Client-side counterpart of pod/worker.py for remote upscaling (#1, Phase 3).

`RemoteUpscaleEngine` mirrors `UpscaleEngine`'s interface (`upscale(src, dest,
resolution)`, `close()`, `device_name`), but instead of running SeedVR2 locally it
streams **one image at a time** to a resident worker on a RunPod pod:

    local image bytes --(ssh -L tunnel)--> POST /upscale --> upscaled bytes --> dest

so batch_upscale.py can pick local vs remote by config with no other change. The
queue, resume-cache, skip logic and the performance watchdog all stay local; only
the per-image GPU work moves to the pod. "Never touch source" is trivially kept —
only a copy is uploaded.

Connectivity is an `ssh -L` tunnel to the pod's worker port (the worker binds
localhost, never exposed publicly). Pure standard library (subprocess + urllib).
"""
import os
import time
import socket
import subprocess
import urllib.request
import urllib.error


class RemoteUpscaleError(Exception):
    pass


def _free_local_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


class RemoteUpscaleEngine:
    """Stream images to a pod worker over an ssh -L tunnel. Same surface as
    UpscaleEngine so it is a drop-in for batch_upscale."""

    def __init__(self, host, ssh_port, ssh_key, worker_port=8200, local_port=None,
                 known_hosts=None, ready_timeout=420, request_timeout=600):
        self.host = host
        self.ssh_port = int(ssh_port)
        self.ssh_key = ssh_key
        self.worker_port = int(worker_port)
        self.local_port = int(local_port or _free_local_port())
        self.request_timeout = request_timeout
        self._tunnel = self._open_tunnel(known_hosts)
        info = self._wait_health(ready_timeout)
        self.device_name = info.get("device", "remote")
        # Whether the pod's engine kept DiT + VAE resident on the GPU (big-VRAM
        # cards). Surfaced so the local log can confirm it — the engine's own
        # "Big-VRAM GPU detected" line is printed on the pod, not here.
        self.resident = bool(info.get("resident", False))

    # ── tunnel ────────────────────────────────────────────────────────────────

    def _open_tunnel(self, known_hosts):
        opts = [
            "-i", self.ssh_key, "-p", str(self.ssh_port),
            "-o", "StrictHostKeyChecking=no",
            "-o", "ExitOnForwardFailure=yes",
            "-o", "ServerAliveInterval=30",
            "-N", "-L", f"{self.local_port}:127.0.0.1:{self.worker_port}",
        ]
        if known_hosts:
            opts += ["-o", f"UserKnownHostsFile={known_hosts}"]
        args = ["ssh", *opts, f"root@{self.host}"]
        try:
            # stdin=DEVNULL: under the GUI the parent's stdin is a pipe; without
            # this the tunnel ssh can block on it.
            return subprocess.Popen(args, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except FileNotFoundError as exc:
            raise RemoteUpscaleError("ssh not found on PATH (OpenSSH required).") from exc

    def _health_url(self):
        return f"http://127.0.0.1:{self.local_port}/health"

    def _wait_health(self, timeout):
        """Poll the worker's /health through the tunnel until ready. The worker's
        first model load is slow (weights from the network volume), so the
        default timeout is generous."""
        deadline = time.time() + timeout
        last = ""
        while time.time() < deadline:
            if self._tunnel.poll() is not None:
                raise RemoteUpscaleError("SSH tunnel exited before the worker was reachable.")
            try:
                with urllib.request.urlopen(self._health_url(), timeout=10) as resp:
                    import json
                    return json.loads(resp.read().decode("utf-8", "replace"))
            except Exception as exc:                  # noqa: BLE001 (expected while loading)
                last = str(exc)
                time.sleep(3)
        raise RemoteUpscaleError(f"Worker not ready within {timeout}s ({last}).")

    # ── same interface as UpscaleEngine ────────────────────────────────────────

    def upscale(self, src_path, dest_path, resolution):
        """Upscale one image on the pod and write it to dest_path atomically."""
        with open(src_path, "rb") as f:
            data = f.read()
        ext = os.path.splitext(dest_path)[1] or ".jpg"
        url = (f"http://127.0.0.1:{self.local_port}/upscale"
               f"?resolution={int(resolution)}&ext={ext}")
        req = urllib.request.Request(url, data=data, method="POST",
                                     headers={"Content-Type": "application/octet-stream"})
        try:
            with urllib.request.urlopen(req, timeout=self.request_timeout) as resp:
                out = resp.read()
                proc = resp.headers.get("X-Process-Time")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise RemoteUpscaleError(f"Worker error HTTP {exc.code}: {detail}") from exc
        except Exception as exc:                       # noqa: BLE001
            raise RemoteUpscaleError(f"Remote upscale failed: {exc}") from exc

        tmp = dest_path + ".tmp" + ext
        try:
            with open(tmp, "wb") as f:
                f.write(out)
            os.replace(tmp, dest_path)
        except Exception:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
            raise
        self.last_process_time = float(proc) if proc else None

    def analyse(self, src_path):
        """Detect orientation on the pod (auto-straighten #1, option B). The CNN
        runs on the pod, so the local side needs no torch; we send only a small
        thumbnail (the model sees 224px anyway) to keep per-image bandwidth tiny.
        Returns (degrees, confidence) like orientation.analyse. Fail-safe: any
        problem yields (0, 0.0) so the caller leaves the image un-rotated.

        The thumbnail is built WITHOUT applying EXIF orientation, matching the
        local orientation.analyse (which reads the stored pixels) so the detected
        class means the same thing on both paths."""
        from io import BytesIO
        from PIL import Image
        try:
            im = Image.open(src_path).convert("RGB")
            im.thumbnail((512, 512))                 # longest side <= 512px
            buf = BytesIO()
            im.save(buf, format="JPEG", quality=85)
            data = buf.getvalue()
        except Exception:
            return 0, 0.0
        url = f"http://127.0.0.1:{self.local_port}/orient"
        req = urllib.request.Request(url, data=data, method="POST",
                                     headers={"Content-Type": "application/octet-stream"})
        try:
            with urllib.request.urlopen(req, timeout=self.request_timeout) as resp:
                import json
                info = json.loads(resp.read().decode("utf-8", "replace"))
            return int(info.get("degrees", 0)), float(info.get("confidence", 0.0))
        except Exception:
            return 0, 0.0

    def telemetry(self):
        """Fetch the pod's live system telemetry (remote #1, Feature #4) through
        the tunnel. Returns the same sample dict the GUI's TelemetryRow renders
        (cpu, ram_used_mb, ram_total_mb, gpu_used_mb, gpu_total_mb, gpu_temp_c),
        or None on any error. The worker answers this lock-free, so it works even
        while an upscale is in flight."""
        url = f"http://127.0.0.1:{self.local_port}/telemetry"
        try:
            with urllib.request.urlopen(url, timeout=15) as resp:
                import json
                return json.loads(resp.read().decode("utf-8", "replace"))
        except Exception:
            return None

    def close(self):
        if self._tunnel and self._tunnel.poll() is None:
            self._tunnel.terminate()
            try:
                self._tunnel.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._tunnel.kill()
