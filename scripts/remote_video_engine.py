"""
remote_video_engine.py
----------------------
Client-side counterpart of pod/worker.py's `--mode video` (Video Upscaler #2,
phase 4). Streams ONE segment at a time to the resident worker on a RunPod pod
and writes the upscaled segment back:

    local segment bytes --(ssh -L)--> POST /video/submit --> {id}
        --> poll GET /video/status?id  (until done) --> GET /video/fetch?id
        --> upscaled mp4 bytes --> dest

A segment upscale takes minutes to hours, so unlike the image engine's single
synchronous request this is async (submit / poll / fetch) — the long wait is a
sequence of cheap polls, survives nothing-to-read stretches, and exposes live
progress to the caller via `on_progress`.

`RemoteVideoEngine` subclasses `RemoteUpscaleEngine` purely to reuse its proven
ssh-tunnel + /health + /telemetry + close() machinery; it does NOT use the
inherited image `upscale()` / `analyse()`. Pure standard library.
"""
import os
import time
import json
import urllib.request
import urllib.error

from remote_upscale_engine import RemoteUpscaleEngine, RemoteUpscaleError


class RemoteVideoError(Exception):
    pass


class RemoteVideoEngine(RemoteUpscaleEngine):
    """Stream video segments to a pod worker in `video` mode over an ssh -L
    tunnel. Reuses RemoteUpscaleEngine for the tunnel/health/telemetry/close."""

    # Per-call HTTP timeouts. submit uploads the whole segment (bandwidth-bound),
    # fetch downloads the upscaled segment; both get a generous ceiling. status is
    # a tiny JSON poll. The OVERALL segment wait has no cap here — a segment can
    # legitimately run for hours; liveness is the worker heartbeat + the pod's
    # dead-man's switch, not a client timeout.
    SUBMIT_TIMEOUT = 1200
    FETCH_TIMEOUT = 1800
    STATUS_TIMEOUT = 30

    def _url(self, path):
        return f"http://127.0.0.1:{self.local_port}{path}"

    def process_segment(self, src_path, dest_path, *, resolution, batch_size,
                        chunk_size, temporal_overlap=0, seed=None,
                        video_backend="opencv", use_10bit=False,
                        poll_interval=5, on_progress=None):
        """Upscale one segment file on the pod and write the result to dest_path
        atomically. Returns the number of frames the worker wrote. Raises
        RemoteVideoError on a worker error, a lost job, or a dropped tunnel.

        `on_progress(status_dict)` (optional) is called after each status poll so
        the caller can surface live segment progress."""
        with open(src_path, "rb") as f:
            data = f.read()
        ext = os.path.splitext(src_path)[1] or ".mkv"

        self.last_segment_seconds = None
        job_id = self._submit(data, ext, resolution, batch_size, chunk_size,
                              temporal_overlap, seed, video_backend, use_10bit)
        frames = self._await(job_id, poll_interval, on_progress)
        out = self._fetch(job_id)

        ext_out = ".mp4"
        tmp = dest_path + ".tmp" + ext_out
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
        return frames

    # ── protocol steps ──────────────────────────────────────────────────────

    def _submit(self, data, ext, resolution, batch_size, chunk_size,
                temporal_overlap, seed, video_backend, use_10bit):
        q = (f"?resolution={int(resolution)}&batch_size={int(batch_size)}"
             f"&chunk_size={int(chunk_size)}&temporal_overlap={int(temporal_overlap)}"
             f"&video_backend={video_backend}&use_10bit={'1' if use_10bit else '0'}"
             f"&ext={ext}")
        if seed is not None:
            q += f"&seed={int(seed)}"
        req = urllib.request.Request(self._url("/video/submit" + q), data=data,
                                     method="POST",
                                     headers={"Content-Type": "application/octet-stream"})
        try:
            with urllib.request.urlopen(req, timeout=self.SUBMIT_TIMEOUT) as resp:
                info = json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise RemoteVideoError(f"submit failed HTTP {exc.code}: {detail}") from exc
        except Exception as exc:                       # noqa: BLE001
            raise RemoteVideoError(f"submit failed: {exc}") from exc
        job_id = info.get("id")
        if not job_id:
            raise RemoteVideoError(f"worker returned no job id: {info}")
        return job_id

    def _await(self, job_id, poll_interval, on_progress):
        """Poll /video/status until the job leaves the running state. A handful of
        consecutive poll failures (a brief tunnel hiccup) are tolerated; a sustained
        outage or a job the worker no longer knows about raises."""
        url = self._url(f"/video/status?id={job_id}")
        misses = 0
        while True:
            try:
                with urllib.request.urlopen(url, timeout=self.STATUS_TIMEOUT) as resp:
                    st = json.loads(resp.read().decode("utf-8", "replace"))
                misses = 0
            except Exception as exc:                   # noqa: BLE001
                misses += 1
                if misses >= 6:
                    raise RemoteVideoError(
                        f"lost contact with the worker while polling segment: {exc}")
                time.sleep(poll_interval)
                continue
            if on_progress:
                try:
                    on_progress(st)
                except Exception:
                    pass
            state = st.get("state")
            if state == "done":
                self.last_segment_seconds = st.get("seconds")
                return int(st.get("frames_written") or 0)
            if state == "error":
                raise RemoteVideoError(f"worker error on segment: {st.get('error')}")
            if state == "unknown":
                raise RemoteVideoError("worker lost the job (it may have restarted).")
            time.sleep(poll_interval)

    def _fetch(self, job_id):
        url = self._url(f"/video/fetch?id={job_id}")
        try:
            with urllib.request.urlopen(url, timeout=self.FETCH_TIMEOUT) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise RemoteVideoError(f"fetch failed HTTP {exc.code}: {detail}") from exc
        except Exception as exc:                       # noqa: BLE001
            raise RemoteVideoError(f"fetch failed: {exc}") from exc
