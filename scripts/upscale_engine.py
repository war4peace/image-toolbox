"""
upscale_engine.py
-----------------
In-process SeedVR2 upscale engine — replaces the ComfyUI server dependency.

Wraps the standalone SeedVR2 pipeline (seedvr2/inference_cli.py, the same code
that powers the ComfyUI node) so batch_upscale.py can upscale images directly:

  - No ComfyUI install, no HTTP upload/submit/poll/download round-trip.
  - DiT and VAE models are loaded ONCE and kept cached, instead of being
    re-prepared for every image. On a small GPU they are parked in system RAM
    between images; on a big-VRAM card (>= vram_resident_threshold_gb, default
    40 GB) they stay resident on the GPU, skipping the per-image CPU round trip.
  - Images are loaded with PIL and EXIF orientation is applied, matching the
    behaviour of ComfyUI's LoadImage node (the standalone CLI's own loader
    ignores orientation tags, which old camera JPEGs rely on).
  - Output is written in the real format matching the destination extension
    (the old flow saved PNG bytes into .jpg-named files). JPEG is saved at
    quality 95 with chroma subsampling disabled; writes are atomic
    (temp file + rename) so an interrupted run never leaves a partial file
    that would be skipped as "already done" on resume.

Must run inside the toolbox venv (PyTorch + seedvr2/requirements.txt).

Usage:
    from upscale_engine import UpscaleEngine
    engine = UpscaleEngine(repo_dir, model_dir, upscale_cfg)
    engine.upscale(src_path, dest_path, resolution=1622)
"""

import io
import os
import sys
import random
import contextlib

try:
    from debug_log import debug_log
except Exception:                                  # noqa: BLE001 (old install)
    def debug_log(*_a, **_k):
        pass

_TRUTHINESS_FIXED = False


def _fix_compiled_dit_truthiness():
    """Give seedvr2's CompatibleDiT a __len__ so a torch.compile'd DiT survives a truthiness
    test. Without this, a COMPILED local video run dies on its SECOND chunk with
    "CompatibleDiT does not support len()".

    The chain: with compile_dit, runner.dit is a torch._dynamo OptimizedModule wrapping
    CompatibleDiT. seedvr2's cache-reuse path (model_configuration._initialize_cache_context)
    asks `if cached_model:`. Python resolves truthiness as __bool__, then __len__, then
    "always true". A bare nn.Module has neither dunder, so it is simply truthy and the check
    means "is it there". But OptimizedModule DEFINES __len__ (proxying to the wrapped module),
    so the check now calls it, and OptimizedModule.__len__ raises for any wrapped module that
    is not Sized. torch.compile does not break the DiT: it gives it a __len__ it cannot honour,
    and a plain `if x:` silently changes meaning from "exists" to "is non-empty".

    Fixed HERE, not in seedvr2/, because seedvr2/ is .gitignore'd and bootstrap-downloaded: an
    edit there works on one machine, reaches no user, and dies at the next bootstrap. Upstream's
    real fix is `if cached_model is not None:`.

    __len__ = 1 restores EXACTLY the pre-compile behaviour (a bare CompatibleDiT was
    unconditionally truthy) and is what OptimizedModule.__len__ proxies to. Nothing calls len()
    on a DiT for a count. Chosen over patching torch's OptimizedModule.__bool__ (which would fix
    the whole class of bug, but reaches into a dependency we do not control on a hot upgrade
    path, to repair a defect that is seedvr2's missing dunder). Idempotent, fail-safe, and never
    overrides a __len__ upstream may add.

    The Sized.register call is NOT redundant. OptimizedModule.__len__ gates on
    `isinstance(self._orig_mod, Sized)`, and Sized answers via __subclasshook__, whose result
    ABCMeta CACHES per class. A negative cached before we attach __len__ is never rechecked, so
    the patch would silently do nothing depending on import order. register() bumps abc's
    invalidation counter, which clears those caches, and makes the isinstance true outright
    instead of relying on a hook re-run.
    """
    global _TRUTHINESS_FIXED
    if _TRUTHINESS_FIXED:
        return
    _TRUTHINESS_FIXED = True
    try:
        from collections.abc import Sized
        from src.optimization.compatibility import CompatibleDiT
        if getattr(CompatibleDiT, "__len__", None) is None:
            CompatibleDiT.__len__ = lambda _self: 1
        Sized.register(CompatibleDiT)
    except Exception as exc:                       # noqa: BLE001 (upstream moved/renamed it)
        debug_log("upscale_engine._fix_compiled_dit_truthiness", exc=exc)


class UpscaleEngine:
    """
    Owns the SeedVR2 pipeline for the lifetime of a batch run.

    Heavy imports (torch, the seedvr2 package) happen in __init__, so callers
    can still print usage/help without paying the import cost.
    """

    def __init__(self, repo_dir, model_dir, settings, debug=False):
        """
        repo_dir  – path to the cloned seedvr2 repository
        model_dir – directory holding the .safetensors weights (downloaded
                    automatically if missing)
        settings  – the "upscale" section of config.json
        """
        self.repo_dir  = os.path.abspath(repo_dir)
        self.model_dir = os.path.abspath(model_dir)
        cli_path = os.path.join(self.repo_dir, "inference_cli.py")
        if not os.path.exists(cli_path):
            raise FileNotFoundError(
                f"SeedVR2 repository not found at: {self.repo_dir}\n"
                f"  Expected: {cli_path}\n"
                f"  Clone it with:\n"
                f"  git clone https://github.com/numz/ComfyUI-SeedVR2_VideoUpscaler \"{self.repo_dir}\""
            )

        if self.repo_dir not in sys.path:
            sys.path.insert(0, self.repo_dir)

        import inference_cli as _cli            # heavy: imports torch, cv2
        self._cli = _cli
        _fix_compiled_dit_truthiness()
        _cli.debug.enabled = debug
        self.debug    = debug
        self.log_sink = None   # optional file object; receives captured pipeline output

        self.args = self._build_args(settings)
        self._runner_cache = {}                  # persists DiT/VAE across images

        from src.utils.downloads import download_weight
        from src.utils.model_registry import DEFAULT_VAE
        self._default_vae = DEFAULT_VAE
        if not download_weight(dit_model=self.args.dit_model, vae_model=DEFAULT_VAE,
                               model_dir=self.model_dir, debug=_cli.debug):
            raise RuntimeError(
                f"SeedVR2 model weights unavailable in {self.model_dir} "
                f"and automatic download failed."
            )

        import torch
        if not torch.cuda.is_available():
            raise RuntimeError(
                "CUDA is not available in this Python environment. "
                "An NVIDIA GPU and a CUDA build of PyTorch are required."
            )
        self.device_name = torch.cuda.get_device_name(0)

    def _resident_offload_device(self, settings):
        """
        Pick where the cached DiT/VAE are parked *between images*.

        Default behaviour keeps them in system RAM ("cpu") — the right call on a
        small local GPU that can't spare the VRAM. But on a big-VRAM card the
        per-image CPU↔GPU round trip (~14 GB DiT + the VAE, every image, over
        PCIe) is pure waste: the card can hold both models resident, so we park
        the offload on the GPU itself instead. This is what made powerful remote
        pods (A100/H100/H200 40-80 GB, RTX Pro 6000 96 GB) benchmark so
        inefficiently — a big card was behaving as if it had 8.

        Returns (offload_device, resident_bool). Fails safe to ("cpu", False) on
        any error, so a detection hiccup can never make a run worse.
        """
        # The 40 GB default is set from real testing: a resident run peaked at
        # 36.6 GB VRAM on a 4K batch, so any 40+ GB card holds both models with
        # room to spare. Going resident pins the ~14 GB DiT in VRAM during the
        # VAE decode too (today's CPU offload *phases* them — DiT off to RAM,
        # then VAE decodes with the card to itself), so it shrinks the decode
        # headroom by the DiT's footprint; at 40 GB the measured peak still fits.
        # If a future card sits *below* 40 GB and you want it resident anyway,
        # don't just drop the threshold — pair it with decode_tiled, which bounds
        # the VAE-decode spike so ballooning can't OOM.
        threshold = float(settings.get("vram_resident_threshold_gb", 40))
        if threshold <= 0:
            return "cpu", False
        try:
            import torch
            if not torch.cuda.is_available():
                return "cpu", False
            total_gb = torch.cuda.get_device_properties(0).total_memory / (1024 ** 3)
            if total_gb >= threshold:
                return "cuda:0", True
        except Exception:
            pass
        return "cpu", False

    def _resolve_attention(self, mode):
        """Resolve attention_mode 'auto' to the FASTEST backend actually installed on
        this GPU (SeedVR2's compatibility flags), so the user never has to know which
        kernel their card supports: sageattn_3 (Blackwell) > sageattn_2 > flash_attn_3
        > flash_attn_2 > sdpa. A non-'auto' value is honoured as-is. Fail-safe: any
        import problem falls back to the always-available sdpa."""
        if (mode or "auto").lower() != "auto":
            return mode
        try:
            from src.optimization import compatibility as compat
            if getattr(compat, "SAGE_ATTN_3_AVAILABLE", False):
                pick = "sageattn_3"
            elif getattr(compat, "SAGE_ATTN_2_AVAILABLE", False):
                pick = "sageattn_2"
            elif getattr(compat, "FLASH_ATTN_3_AVAILABLE", False):
                pick = "flash_attn_3"
            elif getattr(compat, "FLASH_ATTN_2_AVAILABLE", False):
                pick = "flash_attn_2"
            else:
                pick = "sdpa"
        except Exception:
            pick = "sdpa"
        print(f"🧠 Attention mode auto -> {pick}", flush=True)
        return pick

    def _build_args(self, settings):
        """
        Build the full argument namespace through the CLI's own parser so we
        inherit every default and stay compatible with upstream changes.
        """
        offload_device, resident = self._resident_offload_device(settings)
        self.resident = resident                 # exposed for callers/tests

        argv = [
            os.path.join(self.repo_dir, "inference_cli.py"),
            "__engine__",                                    # dummy input, never used
            "--dit_model",         settings.get("dit_model", "seedvr2_ema_7b_fp16.safetensors"),
            "--model_dir",         self.model_dir,
            "--resolution",        "1080",                   # overridden per image
            "--max_resolution",    str(settings.get("max_resolution", 3840)),
            "--batch_size",        "1",                      # single image per call
            "--color_correction",  settings.get("color_correction", "lab"),
            "--attention_mode",    self._resolve_attention(settings.get("attention_mode", "auto")),
            # Keep models in memory across images. On a small GPU they park in
            # system RAM; on a big-VRAM card they stay resident on the GPU.
            "--cache_dit", "--cache_vae",
            "--dit_offload_device", offload_device,
            "--vae_offload_device", offload_device,
        ]
        if resident:
            print(f"📌 Big-VRAM GPU detected — keeping DiT + VAE resident on "
                  f"{offload_device} (no per-image CPU offload).", flush=True)
        # Block-swap trades speed for VRAM; it directly contradicts keeping the
        # model resident, so a resident run never swaps regardless of config.
        blocks_to_swap = 0 if resident else int(settings.get("blocks_to_swap", 0))
        if blocks_to_swap > 0:
            argv += ["--blocks_to_swap", str(blocks_to_swap)]
        # VAE tiling. The tile OVERLAP is passed too (it used to fall back to the CLI's own
        # 128 default): it is the width of the cosine cross-fade that hides each tile seam,
        # so it is the quality knob of tiling and has to be tunable and recorded, not
        # implicit. The engine itself skips tiling per frame when the frame already fits one
        # tile, so a tile size >= the output is a no-op rather than a cost.
        if settings.get("encode_tiled", False):
            argv += ["--vae_encode_tiled",
                     "--vae_encode_tile_size", str(settings.get("encode_tile_size", 1024)),
                     "--vae_encode_tile_overlap", str(settings.get("encode_tile_overlap", 128))]
        if settings.get("decode_tiled", False):
            argv += ["--vae_decode_tiled",
                     "--vae_decode_tile_size", str(settings.get("decode_tile_size", 1024)),
                     "--vae_decode_tile_overlap", str(settings.get("decode_tile_overlap", 128))]
        # Video-path quality/speed knobs (set only on the Video Upscaler's pod, so
        # the image path is unaffected). torch.compile: a one-time compile cost on
        # the first segment, then 20-40% faster DiT every segment after — worth it
        # because our segments share one fixed shape (uniform_batch_size keeps it
        # constant, so it never recompiles). uniform_batch_size pads the ragged
        # final batch so it can't flicker. input_noise_scale (>0) counters 4K
        # over-smoothing. All default off on the image path (keys absent there).
        if settings.get("compile_dit"):
            argv += ["--compile_dit"]
        if settings.get("compile_vae"):
            argv += ["--compile_vae"]
        # compile_dynamic: compile ONE graph that serves every batch size, instead of
        # specialising per shape. Off in production ON PURPOSE (a run has ONE fixed batch
        # forever, so the static graph is both free and faster); ON for the BENCHMARK, whose
        # whole job is sweeping many batches -- there, static specialisation recompiles at
        # EVERY rung and the compile can never amortise, which inflates s/frame worst at
        # small batches (fewest frames to spread it over) and so corrupts the batch ranking
        # itself. A dynamic graph is slightly slower than a static one, but that bias is
        # uniform across rungs, which a per-rung compile is not.
        if settings.get("compile_dynamic"):
            argv += ["--compile_dynamic"]
        if settings.get("uniform_batch_size"):
            argv += ["--uniform_batch_size"]
        noise = float(settings.get("input_noise_scale", 0.0) or 0.0)
        if noise > 0:
            argv += ["--input_noise_scale", str(noise)]

        saved_argv = sys.argv
        sys.argv = argv
        try:
            return self._cli.parse_arguments()
        finally:
            sys.argv = saved_argv

    # ── Image I/O ────────────────────────────────────────────────────────────

    @staticmethod
    def _load_image(path):
        """
        Load an image as a [1, H, W, 3] float16 RGB tensor in [0, 1], applying
        EXIF orientation (parity with ComfyUI's LoadImage). Alpha is flattened
        by RGB conversion, also matching LoadImage.
        """
        import numpy as np
        import torch
        from PIL import Image, ImageOps

        with Image.open(path) as img:
            img = ImageOps.exif_transpose(img)
            img = img.convert("RGB")
            arr = np.asarray(img, dtype=np.float32) / 255.0
        return torch.from_numpy(arr[None, ...]).to(torch.float16)

    @staticmethod
    def _save_image(tensor, dest_path):
        """
        Save frame 0 of a [T, H, W, C] float tensor (RGB, range [0, 1]) to
        dest_path. Format follows the extension. Atomic: writes to a temp
        file in the destination folder, then renames over the target.
        """
        import numpy as np
        from PIL import Image

        frame = tensor[0]
        arr = (frame.clamp(0, 1).numpy() * 255.0).round().astype(np.uint8)
        img = Image.fromarray(arr[..., :3], mode="RGB")

        ext = os.path.splitext(dest_path)[1].lower()
        tmp_path = dest_path + ".tmp" + ext      # keep extension so PIL picks the format
        try:
            if ext in (".jpg", ".jpeg"):
                img.save(tmp_path, "jpeg", quality=95, subsampling=0, optimize=True)
            elif ext == ".webp":
                img.save(tmp_path, "webp", quality=95)
            else:
                # .png and everything else (.bmp/.tiff sources are saved as PNG
                # content only if the extension says so; PIL infers from ext)
                img.save(tmp_path)
            os.replace(tmp_path, dest_path)
        except Exception:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            raise

    # ── Output capture ───────────────────────────────────────────────────────

    def _drain_capture(self, buf):
        """
        Write captured pipeline output to log_sink (if set), collapsing
        progress-bar redraws (carriage returns) to their final state and
        dropping blank lines.
        """
        text = buf.getvalue()
        if not text or self.log_sink is None:
            return
        lines = []
        for raw in text.replace("\r\n", "\n").split("\n"):
            line = raw.rsplit("\r", 1)[-1]
            if line.strip():
                lines.append(line)
        if lines:
            try:
                self.log_sink.write("\n".join(lines) + "\n")
            except Exception:
                pass   # logging is best-effort — never fail an upscale over it

    # ── Main entry point ─────────────────────────────────────────────────────

    def upscale(self, src_path, dest_path, resolution):
        """
        Upscale one image. `resolution` is the SeedVR2 short-side target
        (see compute_seedvr2_resolution in batch_upscale.py).
        Raises on any failure; never leaves a partial destination file.

        Pipeline output (phase banners, tqdm bars) is hidden from the terminal
        and appended to log_sink instead, unless debug=True.
        """
        self.args.resolution = int(resolution)
        # Cap the seed at 2**31-1, not 2**32-1: the SeedVR2 engine internally
        # derives the VAE seed as `seed + 1000000` (generation_phases.py) and
        # feeds it to numpy's np.random.seed(), which rejects anything above
        # 2**32-1. A draw in the top 1,000,000 of the full 32-bit range used to
        # overflow that and fail the image ("Seed must be between 0 and
        # 2**32 - 1"). 2**31-1 still gives 2.1 billion distinct seeds with
        # billions of headroom for any internal offset.
        self.args.seed       = random.randint(0, 2**31 - 1)

        if self.debug:
            frames = self._load_image(src_path)
            result = self._run_core(frames)
        else:
            buf = io.StringIO()
            try:
                with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                    frames = self._load_image(src_path)
                    result = self._run_core(frames)
            finally:
                self._drain_capture(buf)
        self._save_image(result, dest_path)

    def _run_core(self, frames):
        return self._cli._process_frames_core(
            frames_tensor=frames,
            args=self.args,
            device_id="0",
            debug=self._cli.debug,
            runner_cache=self._runner_cache,
        )

    # ── Video entry point (Video Upscaler #2, phase 3) ───────────────────────

    def process_video(self, src_path, dest_path, *, resolution, batch_size=13,
                      chunk_size=0, temporal_overlap=0, seed=None,
                      video_backend="opencv", use_10bit=False,
                      load_cap=0, skip_first_frames=0, capture=False):
        """
        Upscale a whole video SEGMENT file to dest_path and return the number of
        frames written. Drives the SAME SeedVR2 streaming path the benchmark and
        RAM harnesses validated (`inference_cli.process_single_file`), reusing the
        cached DiT/VAE so successive segments skip the model load.

        The video-only knobs (no equivalent on the image path) come from the tuned
        per-(target x card) defaults the runner picks; `chunk_size > 0` MUST be set
        by the caller for any non-trivial segment so output frames stream out
        instead of accumulating in RAM (docs/video-upscaler.md section 8). The
        source segment is read-only; output is written to dest_path.

        Unlike upscale(), this does NOT redirect stdout by default (`capture=False`)
        so the pipeline's progress reaches the pod log live during a long segment;
        the worker monitors the growing output file for liveness/heartbeat.
        """
        self.args.resolution        = int(resolution)
        self.args.batch_size        = int(batch_size)
        self.args.chunk_size        = int(chunk_size)
        self.args.temporal_overlap  = int(temporal_overlap)
        self.args.load_cap          = int(load_cap)
        self.args.skip_first_frames = int(skip_first_frames)
        self.args.output_format     = "mp4"
        self.args.video_backend     = video_backend
        self.args.use_10bit         = bool(use_10bit)
        # One fixed seed per source video (6.2) when the caller supplies it; else a
        # fresh draw. Capped at 2**31-1 for the same VAE-seed reason as upscale().
        self.args.seed = int(seed) if seed is not None else random.randint(0, 2**31 - 1)

        def _go():
            return self._cli.process_single_file(
                src_path, self.args, ["0"],
                output_path=dest_path, runner_cache=self._runner_cache)

        if not capture:
            return _go()
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                return _go()
        finally:
            self._drain_capture(buf)

    def close(self):
        """Release cached models and free VRAM/RAM."""
        self._runner_cache.clear()
        try:
            import torch
            import gc
            gc.collect()
            torch.cuda.empty_cache()
        except Exception:
            pass
