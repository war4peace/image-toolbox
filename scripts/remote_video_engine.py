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
import shutil
import urllib.request
import urllib.error

from remote_upscale_engine import RemoteUpscaleEngine, RemoteUpscaleError


class RemoteVideoError(Exception):
    """A liveness / transport failure talking to the pod worker (a dropped tunnel,
    a lost job, a connection refused). The self-healing supervisor (#6) treats this
    as a POD failure worth reconnecting/redeploying for, NOT a bad source."""
    pass


class RemoteVideoWorkerError(RemoteVideoError):
    """The worker ran but reported an error ON THE SEGMENT (a bad/corrupt source, a
    codec the pod can't read). This is a per-job failure, NOT a pod loss: redeploying
    a fresh identical pod would hit the exact same error, so the supervisor must let
    it count against the job's fail_count (item 4 give-up) instead of healing it."""
    pass


class RemoteVideoStopped(RemoteVideoError):
    """Raised when the caller's should_stop() asks to abort while polling a segment,
    so a Stop tears the (disposable) pod down within a poll interval instead of waiting
    for a long segment to finish on its own. There is no on-pod 'abort batch' message —
    killing the pod is the abort, which the runner's teardown (session.close) does."""
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
                        poll_interval=5, on_progress=None, should_stop=None):
        """Upscale one segment file on the pod and write the result to dest_path
        atomically. Returns the number of frames the worker wrote. Raises
        RemoteVideoError on a worker error, a lost job, or a dropped tunnel.

        `on_progress(status_dict)` (optional) is called after each status poll so
        the caller can surface live segment progress. `should_stop` (optional) is a
        predicate polled each iteration; when it returns True the wait aborts with
        RemoteVideoStopped so a Stop is responsive even mid-segment.

        Both the upload and the download are STREAMED (item 8): a 4K segment is commonly
        hundreds of MB, and the old code held the whole input AND the whole output in RAM
        at once. Now the input is sent straight from the file (Content-Length set) and the
        result is written straight to disk (copyfileobj), so peak RAM is one block, not
        two whole segments."""
        ext = os.path.splitext(src_path)[1] or ".mkv"

        # Phase timing (troubleshooting): submit (upload) / wait (submit->done, incl. the
        # pod's own process_video 'seconds') / fetch (stream download to disk) / finalize
        # (the atomic rename). The runner logs these so the per-segment overhead beyond
        # the reported pod time (wait - last_segment_seconds) can be localised. See
        # process_job's segment line.
        self.last_segment_seconds = None
        self.last_phase = {}
        t0 = time.monotonic()
        job_id = self._submit(src_path, ext, resolution, batch_size, chunk_size,
                              temporal_overlap, seed, video_backend, use_10bit)
        t_submit = time.monotonic()
        frames = self._await(job_id, poll_interval, on_progress, should_stop)
        t_await = time.monotonic()

        ext_out = ".mp4"
        tmp = dest_path + ".tmp" + ext_out
        try:
            nbytes = self._fetch(job_id, tmp)         # streams the result straight to tmp
            t_fetch = time.monotonic()
            os.replace(tmp, dest_path)
        except Exception:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
            raise
        self.last_phase = {
            "submit":   t_submit - t0,
            "wait":     t_await - t_submit,
            "fetch":    t_fetch - t_await,            # download now includes the disk write
            "finalize": time.monotonic() - t_fetch,   # just the atomic rename
            "bytes":    nbytes,
        }
        return frames

    def probe_batch(self, src_path, dest_path, *, resolution, batch, overlap=None,
                    frames=None, poll_interval=5, should_stop=None):
        """Benchmark primitive (feature #7, docs/local-video-upscaler.md section 22):
        stream one short clip to the pod's /video/probe, measure ONE fixed-batch window,
        and return the outcome dict. This is the SAME contract as
        LocalVideoEngine.probe_batch, so scripts/video_benchmark.py drives a LOCAL or a
        REMOTE sweep through one code path (an engine swap).

        Returns {outcome, batch, overlap, frames, seconds, peak_alloc_gb,
        peak_reserved_gb} where outcome is 'ok' | 'oom' | 'error' | 'stopped'. Never
        raises for an OOM (an expected sweep outcome); a lost pod / dropped tunnel raises
        RemoteVideoError so the benchmark reports the pod died. `dest_path` and `frames`
        are ignored (the pod discards the upscaled output and counts frames itself; the
        client-generated clip already holds the batch)."""
        ext = os.path.splitext(src_path)[1] or ".mkv"
        b = int(batch)
        ov = -1 if overlap is None else int(overlap)         # -1 = the pod's AUTO overlap
        job_id = self._submit_probe(src_path, ext, resolution, b, ov)
        return self._await_probe(job_id, b, poll_interval, should_stop)

    # ── protocol steps ──────────────────────────────────────────────────────

    def _submit(self, src_path, ext, resolution, batch_size, chunk_size,
                temporal_overlap, seed, video_backend, use_10bit):
        q = (f"?resolution={int(resolution)}&batch_size={int(batch_size)}"
             f"&chunk_size={int(chunk_size)}&temporal_overlap={int(temporal_overlap)}"
             f"&video_backend={video_backend}&use_10bit={'1' if use_10bit else '0'}"
             f"&ext={ext}")
        if seed is not None:
            q += f"&seed={int(seed)}"
        # Stream the segment straight from the file: an explicit Content-Length lets
        # http.client send the open file object in blocks (no chunked encoding, so the
        # worker still reads a normal Content-Length body) WITHOUT us buffering it in RAM.
        size = os.path.getsize(src_path)
        try:
            with open(src_path, "rb") as body:
                req = urllib.request.Request(
                    self._url("/video/submit" + q), data=body, method="POST",
                    headers={"Content-Type": "application/octet-stream",
                             "Content-Length": str(size)})
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

    def _await(self, job_id, poll_interval, on_progress, should_stop=None):
        """Poll /video/status until the job leaves the running state. A handful of
        consecutive poll failures (a brief tunnel hiccup) are tolerated; a sustained
        outage or a job the worker no longer knows about raises. A `should_stop` that
        turns True aborts the wait with RemoteVideoStopped (the run then tears the pod
        down) instead of blocking until a long segment finishes."""
        url = self._url(f"/video/status?id={job_id}")
        misses = 0
        while True:
            if should_stop and should_stop():
                raise RemoteVideoStopped("stopped by user")
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
                # A source/content failure, not a pod loss (see RemoteVideoWorkerError):
                # the supervisor must NOT redeploy for this, or it would loop forever on
                # the same bad segment.
                raise RemoteVideoWorkerError(f"worker error on segment: {st.get('error')}")
            if state == "unknown":
                raise RemoteVideoError("worker lost the job (it may have restarted).")
            time.sleep(poll_interval)

    def _fetch(self, job_id, dest_tmp):
        """Stream the upscaled segment straight to `dest_tmp`, returning the byte count.
        copyfileobj (1 MB blocks) so a hundreds-of-MB 4K segment never sits whole in RAM;
        the caller does the atomic os.replace onto the final path."""
        url = self._url(f"/video/fetch?id={job_id}")
        try:
            with urllib.request.urlopen(url, timeout=self.FETCH_TIMEOUT) as resp, \
                    open(dest_tmp, "wb") as f:
                shutil.copyfileobj(resp, f, 1 << 20)
            return os.path.getsize(dest_tmp)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise RemoteVideoError(f"fetch failed HTTP {exc.code}: {detail}") from exc
        except Exception as exc:                       # noqa: BLE001
            raise RemoteVideoError(f"fetch failed: {exc}") from exc

    # ── probe steps (remote benchmark, section 22) ───────────────────────────

    def _submit_probe(self, src_path, ext, resolution, batch, overlap):
        """Upload the probe clip to /video/probe (streamed straight from the file, like
        _submit) and return the job id. Raises RemoteVideoError on a transport failure."""
        q = (f"?resolution={int(resolution)}&batch_size={int(batch)}"
             f"&temporal_overlap={int(overlap)}&ext={ext}")
        size = os.path.getsize(src_path)
        try:
            with open(src_path, "rb") as body:
                req = urllib.request.Request(
                    self._url("/video/probe" + q), data=body, method="POST",
                    headers={"Content-Type": "application/octet-stream",
                             "Content-Length": str(size)})
                with urllib.request.urlopen(req, timeout=self.SUBMIT_TIMEOUT) as resp:
                    info = json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise RemoteVideoError(f"probe submit failed HTTP {exc.code}: {detail}") from exc
        except Exception as exc:                       # noqa: BLE001
            raise RemoteVideoError(f"probe submit failed: {exc}") from exc
        job_id = info.get("id")
        if not job_id:
            raise RemoteVideoError(f"worker returned no probe id: {info}")
        return job_id

    def _await_probe(self, job_id, batch, poll_interval, should_stop=None):
        """Poll /video/status until the probe leaves the running state, then return the
        outcome dict (never raising for an 'oom'/'error' RESULT -- those are the sweep's
        own outcomes). A `should_stop` that turns True returns {'outcome': 'stopped'}; a
        sustained poll outage or a job the worker forgot raises RemoteVideoError (a pod
        loss the benchmark surfaces, not a probe result)."""
        url = self._url(f"/video/status?id={job_id}")
        misses = 0
        while True:
            if should_stop and should_stop():
                return {"outcome": "stopped", "batch": batch}
            try:
                with urllib.request.urlopen(url, timeout=self.STATUS_TIMEOUT) as resp:
                    st = json.loads(resp.read().decode("utf-8", "replace"))
                misses = 0
            except Exception as exc:                   # noqa: BLE001
                misses += 1
                if misses >= 6:
                    raise RemoteVideoError(
                        f"lost contact with the worker while probing batch {batch}: {exc}")
                time.sleep(poll_interval)
                continue
            state = st.get("state")
            if state in ("done", "error"):
                # The worker tags the probe result explicitly; fall back by state for an
                # older worker that predates the `outcome` field.
                outcome = st.get("outcome") or ("error" if state == "error" else "ok")
                res = {
                    "outcome":          outcome,
                    "batch":            int(st.get("resolved_batch") or batch),
                    "overlap":          st.get("resolved_overlap"),
                    "frames":           st.get("frames_written"),
                    "seconds":          st.get("seconds"),
                    "peak_alloc_gb":    st.get("peak_alloc_gb"),
                    "peak_reserved_gb": st.get("peak_reserved_gb"),
                }
                if outcome == "error":
                    res["error"] = st.get("error")
                return res
            if state == "unknown":
                raise RemoteVideoError("worker lost the probe job (it may have restarted).")
            time.sleep(poll_interval)
