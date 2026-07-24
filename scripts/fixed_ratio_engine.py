"""
fixed_ratio_engine.py
---------------------
FixedRatioVideoEngine: the NON-SeedVR, fixed-ratio (2x/4x) local video engine
(docs/local-video-upscaler.md section 11, feature #11). A drop-in for
LocalVideoEngine / RemoteVideoEngine / PassthroughVideoEngine in
batch_video_upscale.run_queue: it implements the SAME injected-engine contract
(`process_segment(...)`, `last_segment_seconds`, `last_phase`, `device_name`,
`resident`, `telemetry()`, `close()`), so the whole orchestration around it
(split, reassemble, mux, resume, watchdog, notifications) is unchanged.

Unlike SeedVR2 (a diffusion pass per frame, generative, slow, VRAM-heavy), this
runs a small **fixed-ratio GAN** (Real-ESRGAN and friends) loaded through
**spandrel** (chaiNNer's MIT model loader, NOT basicsr, which is the unmaintained
install-breaker). It is fast, low-VRAM, deterministic and per-frame:

  * No temporal VAE, so it structurally avoids SeedVR2's slow-pan temporal jitter.
  * Generative prior far milder than diffusion SR, so text/plates/logos survive far
    better.
  * Trade-off it CANNOT avoid: each frame is upscaled independently, so it has no
    temporal-consistency mechanism and can inter-frame flicker on noisy sources.

SPIKE SCOPE (0.5.6). Goal: prove the in-process fixed-ratio path end to end on one
short clip, behind the existing engine seam. Deliberate simplifications, to be
replaced when this graduates from spike to feature:

  * **Model path is passed in / from settings, not yet lazy-downloaded + hash-pinned**
    (that is the packaging step: mirror the SeedVR2 lazy-weights pattern + the
    verify-third-party-downloads rule).
  * **No GUI: no engine-in-combobox target token, no engine-aware feasibility** (the
    two GUI requirements). This file only needs to upscale a segment file correctly.
  * **VRAM sizing is a simple batch + OOM back-off** (shrink frame-batch, then tile),
    not the predictive per-card sizer SeedVR2 uses. A fixed-ratio GAN is low-VRAM, so
    the batch/tile back-off is enough for the spike.

The frame IO is stdlib + the bundled ffmpeg (video_pipeline): decode the segment to a
raw rgb24 stream, upscale batches on the GPU (tiled when a frame is large), encode the
result to a concat-compatible mp4. The source segment is read-only.

Must run inside the toolbox venv (PyTorch CUDA + spandrel + torchvision).
"""

import os
import sys
import time
import subprocess

import video_pipeline as vp

try:
    from runner_common import is_oom_error as _is_oom_error
except Exception:                                    # noqa: BLE001
    def _is_oom_error(exc):
        s = str(exc).lower()
        return "out of memory" in s or "cuda oom" in s or ("cublas" in s and "alloc" in s)

_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# Frame-batch OOM back-off floor: below this a smaller batch buys nothing and the real
# lever is spatial tiling (which the retry drops to once batch hits the floor).
_BATCH_FLOOR = 1
# Default GPU tile edge (px, in OUTPUT space is what costs VRAM, but we tile INPUT for
# simplicity). A frame whose short side <= this is upscaled whole; larger frames tile
# with an overlap to hide seams. 512 in @ x4 -> 2048 out is comfortable on ~2 GB.
_DEFAULT_TILE = 512
_TILE_OVERLAP = 32
_TILE_FLOOR = 128                                    # smallest tile the OOM retry will drop to


def _terminate(proc):
    """Best-effort kill of an ffmpeg subprocess on the error/Stop path. Close its pipes
    first (so a process blocked on a pipe read/write unblocks), then terminate/kill."""
    for stream in (getattr(proc, "stdin", None), getattr(proc, "stdout", None)):
        try:
            if stream:
                stream.close()
        except Exception:                            # noqa: BLE001
            pass
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:                                # noqa: BLE001
        try:
            proc.kill()
        except Exception:                            # noqa: BLE001
            pass


def _query_gpu_name():
    """GPU 0's name via nvidia-smi. None on any failure (kept identical to
    LocalVideoEngine so the two engines label the card the same way)."""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=10, creationflags=_CREATE_NO_WINDOW)
        name = out.stdout.strip().splitlines()[0].strip()
        return name or None
    except Exception:                                # noqa: BLE001
        return None


class FixedRatioVideoEngine:
    """In-process fixed-ratio (2x/4x) video engine. Loads ONE spandrel model and reuses
    it across every segment, so a multi-segment video pays the model load once."""

    def __init__(self, model_path, settings=None, *, device="cuda",
                 tile=_DEFAULT_TILE, use_fp16=True, diag_sink=None):
        """`model_path` is a spandrel-loadable weight file (Real-ESRGAN .pth /
        .safetensors, etc.); its scale (2/4) is read from the model, not configured.
        `tile` caps the GPU tile edge (0 = never tile). `diag_sink` (optional
        callable(str)) receives progress/diagnostic lines for the runner's file log."""
        self._model_path = os.path.abspath(model_path)
        self._settings = dict(settings or {})
        self._device = device
        self._tile = int(tile or 0)
        self._use_fp16 = bool(use_fp16)
        self._diag_sink = diag_sink

        self._torch = None
        self._model = None              # spandrel ImageModelDescriptor (lazy-loaded)
        self.scale = None               # the model's fixed ratio (2 or 4), set on load
        self.model = os.path.basename(self._model_path)
        self.device_name = _query_gpu_name() or "local"
        self.resident = True            # weights stay resident between segments (tiny)

        self.last_segment_seconds = None
        self.last_phase = {}
        self.last_resolved_batch = None
        self.last_overlap = None
        self._real_stdout = None

    # ── model + logging ──────────────────────────────────────────────────────

    def _log(self, msg):
        """A progress line to the runner's real stdout (GUI terminal) and the file log."""
        out = self._real_stdout or sys.stdout
        try:
            out.write(msg + "\n")
            out.flush()
        except Exception:                            # noqa: BLE001
            pass
        if self._diag_sink is not None:
            try:
                self._diag_sink(msg)
            except Exception:                        # noqa: BLE001
                pass

    def _ensure_model(self):
        """Load the spandrel model once, move it to the device in the right dtype."""
        if self._model is not None:
            return self._model
        import torch
        from spandrel import ModelLoader, ImageModelDescriptor
        self._torch = torch
        desc = ModelLoader().load_from_file(self._model_path)
        if not isinstance(desc, ImageModelDescriptor):
            raise RuntimeError(
                f"{self.model}: not an image super-resolution model "
                f"(spandrel loaded {type(desc).__name__})")
        self.scale = int(desc.scale)
        self._fp16 = self._use_fp16 and bool(desc.supports_half) and self._device == "cuda"
        desc.to(self._device)
        if self._fp16:
            desc.model.half()
        desc.eval()
        self._model = desc
        self._log(f"    fixed-ratio engine: {self.model} loaded "
                  f"(x{self.scale}, {'fp16' if self._fp16 else 'fp32'}, "
                  f"{self.device_name})")
        return desc

    # ── GPU upscale (batched, tiled on OOM / large frames) ───────────────────

    def _forward(self, batch):
        """Run the model on an NCHW float tensor (0..1) already on-device, no grad."""
        torch = self._torch
        with torch.no_grad():
            if self._fp16:
                batch = batch.half()
            out = self._model(batch)
        return out.float().clamp_(0, 1)

    def _upscale_tiled(self, batch, tile):
        """Upscale an NCHW batch by tiling each frame into <= `tile`-edge input tiles with
        `_TILE_OVERLAP` px of overlap, writing each tile's non-overlap core into the output
        canvas so seams don't show. `tile<=0` or a small frame -> one whole-frame forward."""
        torch = self._torch
        n, c, h, w = batch.shape
        s = self.scale
        if not tile or (h <= tile and w <= tile):
            return self._forward(batch)
        ov = _TILE_OVERLAP
        out = torch.empty((n, c, h * s, w * s), dtype=torch.float32, device=batch.device)
        step = max(1, tile - ov)
        ys = list(range(0, h, step))
        xs = list(range(0, w, step))
        for y in ys:
            for x in xs:
                y0, x0 = y, x
                y1, x1 = min(y + tile, h), min(x + tile, w)
                # Grow the low edge so an edge tile is still a full `tile` where possible
                # (keeps the model out of tiny-input regimes at the borders).
                y0 = max(0, y1 - tile)
                x0 = max(0, x1 - tile)
                up = self._forward(batch[:, :, y0:y1, x0:x1])
                # The region we KEEP (drop the overlap margin except at true image edges).
                ky0 = y0 + (ov if y0 > 0 else 0)
                kx0 = x0 + (ov if x0 > 0 else 0)
                ky1, kx1 = y1, x1
                sy0, sx0 = (ky0 - y0) * s, (kx0 - x0) * s
                sy1, sx1 = (ky1 - y0) * s, (kx1 - x0) * s
                out[:, :, ky0 * s:ky1 * s, kx0 * s:kx1 * s] = up[:, :, sy0:sy1, sx0:sx1]
        return out

    def _upscale_batch(self, frames_u8, tile):
        """Upscale a list of HxWx3 uint8 numpy frames -> list of HxWx3 uint8 (x scale),
        with OOM back-off: shrink the frame-batch, then the tile edge, before giving up."""
        torch = self._torch
        import numpy as np
        arr = np.stack(frames_u8).astype(np.float32) / 255.0     # N,H,W,C
        batch = torch.from_numpy(arr).permute(0, 3, 1, 2).contiguous().to(self._device)
        out = self._upscale_tiled(batch, tile)
        out = (out.permute(0, 2, 3, 1).clamp_(0, 1) * 255.0 + 0.5).to(torch.uint8)
        res = out.cpu().numpy()
        del batch, out
        return [res[i] for i in range(res.shape[0])]

    # ── the engine contract ──────────────────────────────────────────────────

    def process_segment(self, src_path, dest_path, *, resolution, batch_size,
                        chunk_size=0, temporal_overlap=0, seed=None,
                        video_backend="opencv", use_10bit=False,
                        poll_interval=0, on_progress=None, should_stop=None,
                        seg_index=None, seg_total=None):
        """Upscale one segment file to dest_path on the local GPU and return frames written.

        `resolution` is the box-fit output SHORT side the runner computed for the target.
        A fixed-ratio model produces src*scale; if that does not match `resolution` the
        result is resized to fit (so a preset box or a ratio != model-scale still lands on
        the requested size). `batch_size` is FRAMES per GPU forward (auto -> a small
        default). `chunk_size` / `temporal_overlap` / `seed` are accepted for interface
        parity and ignored (per-frame, deterministic)."""
        import numpy as np
        self.last_segment_seconds = None
        self.last_phase = {}
        self._real_stdout = sys.stdout
        self._ensure_model()
        torch = self._torch

        info = vp.probe(src_path, count=True)
        sw, sh = int(info.width), int(info.height)
        frames_total = int(info.nb_frames or 0)
        fps = info.fps or 30
        # Target output dims: box-fit the source to `resolution` (short side). For a ratio
        # target this equals src*scale, so the resize below is a no-op; for a preset box it
        # trims the x4 result to fit.
        if resolution and min(sw, sh):
            fscale = float(resolution) / float(min(sw, sh))
        else:
            fscale = self.scale
        out_w, out_h = max(1, round(sw * fscale)), max(1, round(sh * fscale))
        # Even dims for yuv420p.
        out_w -= out_w % 2
        out_h -= out_h % 2
        # The model always emits src * scale; the encoder is fed THAT size, then the
        # scale filter trims it to the target box (a no-op when the target IS src*scale,
        # i.e. a ratio target equal to the model's ratio).
        mw, mh = sw * self.scale, sh * self.scale

        # `batch_size` is frames per GPU forward. AUTO (<=0) -> 1: batching does NOT speed a
        # fixed-ratio GAN up (it is compute-bound, measured ~linear on a 3090) yet it multiplies
        # the activation VRAM, so 1 is the low-VRAM sweet spot. An explicit value is honoured
        # for experimentation.
        req_batch = int(batch_size)
        batch = req_batch if req_batch > 0 else 1
        tile = self._tile
        if on_progress:
            on_progress({"state": "running", "resolved_batch": batch,
                         "resolved_overlap": 0, "total_frames": frames_total,
                         "compile_dit": False, "uniform_batch": False, "input_noise": 0})
        seg_lbl = (f"segment {seg_index + 1}/{seg_total}, "
                   if (seg_index is not None and seg_total) else "")

        ffmpeg, _ = vp.find_ffmpeg()
        codec, enc_args, _hw = vp.pick_encoder(prefer_hw=True)
        os.makedirs(os.path.dirname(os.path.abspath(dest_path)), exist_ok=True)

        dec_cmd = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error",
                   "-i", src_path, "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
        enc_cmd = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
                   "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{mw}x{mh}",
                   "-r", str(fps), "-i", "-"]
        if (out_w, out_h) != (mw, mh):
            enc_cmd += ["-vf", f"scale={out_w}:{out_h}:flags=lanczos"]
        enc_cmd += ["-c:v", codec, *enc_args, "-pix_fmt", "yuv420p", dest_path]

        frame_bytes = sw * sh * 3
        t0 = time.time()
        torch.cuda.reset_peak_memory_stats() if self._device == "cuda" else None
        dec = subprocess.Popen(dec_cmd, stdout=subprocess.PIPE,
                               creationflags=_CREATE_NO_WINDOW)
        enc = subprocess.Popen(enc_cmd, stdin=subprocess.PIPE,
                               creationflags=_CREATE_NO_WINDOW)
        written = 0
        last_hb = t0
        ok = False
        try:
            buf = []
            while True:
                if should_stop and should_stop():
                    from remote_video_engine import RemoteVideoStopped
                    raise RemoteVideoStopped("stopped by user")
                raw = dec.stdout.read(frame_bytes)
                if len(raw) < frame_bytes:
                    break
                buf.append(np.frombuffer(raw, dtype=np.uint8).reshape(sh, sw, 3))
                if len(buf) >= batch:
                    batch, tile = self._flush(buf, enc, batch, tile, on_progress)
                    written += len(buf)
                    buf = []
                    now = time.time()
                    if now - last_hb >= 1.0 and on_progress:
                        last_hb = now
                        on_progress({"state": "running", "frames_processed": written,
                                     "total_frames": frames_total})
            if buf:
                batch, tile = self._flush(buf, enc, batch, tile, on_progress)
                written += len(buf)
            enc.stdin.close()
            ok = True
        finally:
            if ok:
                # Clean path: the encoder drains its stdin and exits; wait for both.
                dec_rc = dec.wait()
                enc_rc = enc.wait()
            else:
                # Error / Stop path: the encoder is still reading a now-abandoned stdin, so
                # a plain wait() would hang forever. Kill both procs (and the decoder, which
                # may still be feeding frames) so the exception propagates promptly.
                _terminate(enc)
                _terminate(dec)
                enc_rc = enc.poll()
        if ok and enc_rc not in (0, None):
            raise vp.FFmpegError(f"fixed-ratio encode failed (exit {enc_rc}) for {dest_path}")

        dt = time.time() - t0
        self.last_segment_seconds = dt
        self.last_resolved_batch = batch
        self.last_overlap = 0
        peak_alloc = peak_reserved = None
        if self._device == "cuda":
            try:
                gb = 1024 ** 3
                peak_alloc = round(torch.cuda.max_memory_allocated() / gb, 1)
                peak_reserved = round(torch.cuda.max_memory_reserved() / gb, 1)
            except Exception:                        # noqa: BLE001
                pass
        try:
            out_bytes = os.path.getsize(dest_path)
        except OSError:
            out_bytes = 0
        self.last_phase = {"submit": 0.0, "wait": dt, "fetch": 0.0,
                           "finalize": 0.0, "bytes": out_bytes}
        self._log(f"    {seg_lbl}done: {written} frames -> {out_w}x{out_h} "
                  f"in {dt:.1f}s ({(dt / written):.3f}s/frame)" if written else
                  f"    {seg_lbl}done: 0 frames")
        if on_progress:
            on_progress({"state": "done", "frames_written": written,
                         "frames_processed": frames_total or written,
                         "total_frames": frames_total, "seconds": dt,
                         "resolved_batch": batch, "first_batch": req_batch if req_batch > 0 else batch,
                         "resolved_overlap": 0, "peak_alloc_gb": peak_alloc,
                         "peak_reserved_gb": peak_reserved, "output_bytes": out_bytes})
        return int(written)

    def _flush(self, frames, enc, batch, tile, on_progress):
        """Upscale one buffered group and write it to the encoder, with OOM back-off:
        halve the frame-batch, then halve the tile, until it fits or the floor is hit.
        Returns the (possibly reduced) (batch, tile) so the rest of the segment reuses the
        safe window instead of re-hitting the same OOM every group."""
        torch = self._torch
        while True:
            try:
                ups = self._upscale_batch(frames, tile)
                for f in ups:
                    enc.stdin.write(f.tobytes())
                return batch, tile
            except Exception as exc:                 # noqa: BLE001
                if not _is_oom_error(exc):
                    raise
                if self._device == "cuda":
                    try:
                        torch.cuda.empty_cache()
                    except Exception:                # noqa: BLE001
                        pass
                if len(frames) > _BATCH_FLOOR:
                    # Split the group in half and process each half (also lowers the
                    # steady batch for the remaining groups).
                    half = max(_BATCH_FLOOR, len(frames) // 2)
                    batch = min(batch, half)
                    if on_progress:
                        on_progress({"state": "running", "resolved_batch": batch,
                                     "oom_backoff": True})
                    mid = len(frames) // 2
                    self._flush(frames[:mid], enc, batch, tile, on_progress)
                    self._flush(frames[mid:], enc, batch, tile, on_progress)
                    return batch, tile
                new_tile = (tile // 2) if tile else max(_TILE_FLOOR, 256)
                if new_tile < _TILE_FLOOR:
                    raise
                tile = max(_TILE_FLOOR, new_tile)
                if on_progress:
                    on_progress({"state": "running", "resolved_batch": batch,
                                 "resolved_tile": tile, "oom_backoff": True})

    def telemetry(self):
        """Interface parity: local GPU telemetry is sampled by the GUI (the local row),
        so the engine does not duplicate it (matches Local/PassthroughVideoEngine)."""
        return None

    def close(self):
        try:
            if self._model is not None and self._torch is not None:
                del self._model
                self._model = None
                import gc
                gc.collect()
                self._torch.cuda.empty_cache()
        except Exception:                            # noqa: BLE001
            pass
