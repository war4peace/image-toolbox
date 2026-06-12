"""
upscale_engine.py
-----------------
In-process SeedVR2 upscale engine — replaces the ComfyUI server dependency.

Wraps the standalone SeedVR2 pipeline (seedvr2/inference_cli.py, the same code
that powers the ComfyUI node) so batch_upscale.py can upscale images directly:

  - No ComfyUI install, no HTTP upload/submit/poll/download round-trip.
  - DiT and VAE models are loaded ONCE and kept cached (offloaded to system
    RAM between images), instead of being re-prepared for every image.
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

    def _build_args(self, settings):
        """
        Build the full argument namespace through the CLI's own parser so we
        inherit every default and stay compatible with upstream changes.
        """
        argv = [
            os.path.join(self.repo_dir, "inference_cli.py"),
            "__engine__",                                    # dummy input, never used
            "--dit_model",         settings.get("dit_model", "seedvr2_ema_7b_fp16.safetensors"),
            "--model_dir",         self.model_dir,
            "--resolution",        "1080",                   # overridden per image
            "--max_resolution",    str(settings.get("max_resolution", 3840)),
            "--batch_size",        "1",                      # single image per call
            "--color_correction",  settings.get("color_correction", "lab"),
            "--attention_mode",    settings.get("attention_mode", "sdpa"),
            # Keep models in memory across images, parked in system RAM
            "--cache_dit", "--cache_vae",
            "--dit_offload_device", "cpu",
            "--vae_offload_device", "cpu",
        ]
        blocks_to_swap = int(settings.get("blocks_to_swap", 0))
        if blocks_to_swap > 0:
            argv += ["--blocks_to_swap", str(blocks_to_swap)]
        if settings.get("encode_tiled", False):
            argv += ["--vae_encode_tiled",
                     "--vae_encode_tile_size", str(settings.get("encode_tile_size", 1024))]
        if settings.get("decode_tiled", False):
            argv += ["--vae_decode_tiled",
                     "--vae_decode_tile_size", str(settings.get("decode_tile_size", 1024))]

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
        self.args.seed       = random.randint(0, 2**32 - 1)

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
