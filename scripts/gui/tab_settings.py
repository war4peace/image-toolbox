"""
gui/tab_settings.py
-------------------
The Settings tab.
"""

import os
import json
import time
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import mqtt_publisher
import notifications
import updater
from gui.common import APP_TITLE, APP_VERSION, CFG, save_config, update_auto_check_enabled, ollama_installed, ollama_list_models, test_discord_webhook
from gui.widgets import Tooltip, _ScrollFrame, use_window_button_style


RESOLUTION_PRESETS = [
    ("3840 / 2160", 3840, 2160),
    ("2560 / 1440", 2560, 1440),
    ("1920 / 1080", 1920, 1080),
]

# Keys in the "upscale" block that get their own dedicated controls and so are
# NOT rendered in the generic SeedVR Settings box.
_SEEDVR_EXCLUDE = {"resolution", "max_resolution", "discord_webhook_url",
                   "upscale_cutoff_pct", "output_subdir", "debug",
                   "auto_straighten", "straighten_min_confidence",
                   "watchdog_enabled", "watchdog_factor", "watchdog_consecutive",
                   "watchdog_min_samples",
                   # These are intentionally hidden from the UI: the defaults are
                   # the right ones and a non-technical user can't meaningfully
                   # change them (DiT/VAE model = the 7B FP16 + FP16 VAE combo;
                   # color_correction = LAB; blocks_to_swap is a VRAM/speed lever
                   # that only matters on a VRAM-starved card). The values stay in
                   # config.json (merged on save) for advanced hand-edits.
                   "dit_model", "vae_model",
                   "color_correction", "blocks_to_swap",
                   # Resident-offload threshold: a numeric tuning knob (default
                   # 40 GB, see upscale_engine) that no end user should touch;
                   # config.json only.
                   "vram_resident_threshold_gb"}

# Friendly labels for the generic SeedVR fields.
_SEEDVR_LABELS = {
    "attention_mode":   "Attention mode",
    "encode_tiled":     "VAE encode tiled",
    "decode_tiled":     "VAE decode tiled",
    "encode_tile_size": "Encode tile size",
    "decode_tile_size": "Decode tile size",
    "outage_threshold": "Outage threshold",
}

# Hover help for the SeedVR controls, keyed the same way as the labels, so a
# control built by _make_seedvr_control gets its tooltip automatically (including
# the generic one-per-row fallback for an unrecognised key). A key with no entry
# here simply gets no tooltip rather than a wrong one.
_SEEDVR_TIPS = {
    "attention_mode": (
        "Which maths kernel does the heavy work. Leave on 'auto': the engine then "
        "picks the fastest one your card actually supports. Only change this if a "
        "specific backend misbehaves."),
    "outage_threshold": (
        "How many images may fail in a row before the run pauses and asks for "
        "help. Guards against grinding through a whole folder while something is "
        "broken.   Recommended: 3"),
    "encode_tiled": (
        "Process the image in tiles when READING it into the model. Cuts memory "
        "use so a big picture fits on a smaller card, at some cost in speed."),
    "decode_tiled": (
        "Process the image in tiles when WRITING the result out. This is the step "
        "that usually runs out of memory at high resolutions, so it is the one "
        "worth turning on first."),
    "encode_tile_size": (
        "How big each tile is when reading the image in. Smaller tiles use less "
        "memory and run slower. Only used when VAE Tiled Encode is ticked.   "
        "Recommended: 1024"),
    "decode_tile_size": (
        "How big each tile is when writing the result out. Smaller tiles use less "
        "memory and run slower. Only used when VAE Tiled Decode is ticked.   "
        "Recommended: 1024"),
}

# Suggested values for the free-text enum fields (editable — type anything).
# "auto" (recommended) lets the engine pick the FASTEST backend the GPU actually has
# (sageattn_3 > sageattn_2 > flash_attn_* > sdpa) so the user needn't know their card.
# The explicit names MUST match SeedVR2's argparse choices (inference_cli.py
# --attention_mode); the old short "flash_attn"/"sage" are rejected. sageattn_3 is the
# Blackwell build; sdpa is the always-available fallback.
_SEEDVR_CHOICES = {
    "attention_mode":   ["auto", "sdpa", "sageattn_3", "sageattn_2",
                         "flash_attn_3", "flash_attn_2"],
}




# Video deliverable codec choices (label -> video_backend, use_10bit). opencv is
# SeedVR2's default writer (MPEG-4 / mp4v, most compatible); the ffmpeg backend
# enables H.265 10-bit (docs/video-upscaler.md 6.4 / 14).
_VIDEO_CODEC_OPTIONS = [
    ("Standard — MPEG-4 (most compatible)", "opencv", False),
    ("High quality — H.265 10-bit (ffmpeg)", "ffmpeg", True),
]

# Video model choices (label -> dit_model filename). The pod auto-downloads the chosen
# weights to the network volume on first use (no reprovision). 7B fp16 is the quality
# default; 3B-Q8 is smaller and frees VRAM for bigger temporal windows.
_VIDEO_MODEL_OPTIONS = [
    ("7B FP16 (best detail, default)",        "seedvr2_ema_7b_fp16.safetensors"),
    ("7B FP16 Sharp (stylized/crisper)",      "seedvr2_ema_7b_sharp_fp16.safetensors"),
    ("3B Q8 (smaller, more VRAM headroom)",   "seedvr2_ema_3b-Q8_0.gguf"),
    ("3B FP16 (small, full precision)",       "seedvr2_ema_3b_fp16.safetensors"),
]

# LOCAL video engine choice (feature #11). SeedVR2 = the generative default; Real-ESRGAN
# (fixed_ratio) = a fast, low-VRAM, deterministic per-frame GAN. Only affects LOCAL runs;
# the remote path is always SeedVR2. (label -> video.engine value.)
_VIDEO_ENGINE_OPTIONS = [
    ("SeedVR2: best quality (generative, slower)",       "seedvr2"),
    ("Real-ESRGAN: fast (fixed 2x/4x, low VRAM)",        "fixed_ratio"),
]

# Fixed-ratio (Real-ESRGAN) model picklist, built from the shared catalog so Settings and
# the runner never disagree on what exists. (label -> esrgan_models key.) Guarded: an odd
# tree without esrgan_models degrades to the compact default only, never a crash.
try:
    import esrgan_models as _esrgan_models
    _ESRGAN_MODEL_OPTIONS = [(m.label, m.key) for m in _esrgan_models.catalog()]
    _ESRGAN_DEFAULT_KEY = _esrgan_models.DEFAULT_MODEL
except Exception:                                    # noqa: BLE001
    _ESRGAN_MODEL_OPTIONS = [("Compact (fast): realesr-general-x4v3", "realesr-general-x4v3")]
    _ESRGAN_DEFAULT_KEY = "realesr-general-x4v3"


class SettingsTab(ttk.Frame):
    """Edit the settings that previously lived only in config.json."""

    def __init__(self, notebook, app):
        super().__init__(notebook)
        self.app = app
        self._seedvr_vars = {}     # key -> (tk var, python type)
        self._build()

    # ── construction ─────────────────────────────────────────────────────────

    def _build(self):
        W = Tooltip.WRAP_NARROW
        sf = _ScrollFrame(self)
        sf.pack(fill="both", expand=True)
        body = sf.body

        ollama = CFG.get("ollama", {})
        ups    = CFG.get("upscale", {})
        defs   = CFG.get("defaults", {})
        tag    = CFG.get("tagging", {})

        # ── Default folders (mirrors the tabs' "Save as Default" buttons) ───────
        sec = self._section(body, "Default folders")
        sec.columnconfigure(1, weight=1)
        self.default_src_var = tk.StringVar(value=defs.get("upscale_source", ""))
        self.default_out_var = tk.StringVar(value=defs.get("upscale_output", ""))
        self.default_tag_var = tk.StringVar(value=defs.get("tag_folder", ""))
        self.default_corig_var = tk.StringVar(value=defs.get("conciliate_original", ""))
        self.default_cproc_var = tk.StringVar(value=defs.get("conciliate_processed", ""))
        self.default_vsrc_var = tk.StringVar(value=defs.get("video_source", ""))
        self.default_vout_var = tk.StringVar(value=defs.get("video_output", ""))
        for r, (text, var) in enumerate((
                ("Batch Upscaler — Photo folder:",  self.default_src_var),
                ("Batch Upscaler — Output folder:", self.default_out_var),
                ("Tag & Rename — Photo folder:",    self.default_tag_var),
                ("Conciliation — Original folder:",  self.default_corig_var),
                ("Conciliation — Processed folder:", self.default_cproc_var),
                ("Video Upscaler — Video folder:",   self.default_vsrc_var),
                ("Video Upscaler — Output folder:",  self.default_vout_var))):
            ttk.Label(sec, text=text).grid(row=r, column=0, sticky="w", pady=3)
            ttk.Entry(sec, textvariable=var).grid(row=r, column=1, sticky="ew", padx=6, pady=3)
            b = ttk.Button(sec, text="Browse…",
                           command=lambda v=var: self._pick_folder(v))
            b.grid(row=r, column=2, pady=3)
            # Same wording for all seven, naming the field it fills, so the row is
            # unambiguous when several Browse buttons sit above one another.
            Tooltip(b, f"Choose the folder that {text.rstrip(':')} starts with "
                       f"every time the app opens.", wraplength=W)

        # ── Ollama ────────────────────────────────────────────────────────────
        sec = self._section(body, "Ollama")
        sec.columnconfigure(1, weight=1)

        # URL + model picker + a single Check on one row. "Check" both verifies
        # the URL is reachable AND refreshes the model list (the two used to be
        # separate buttons). The model combobox is read-only (pick from what's
        # actually installed) and ~130 px wide.
        ttk.Label(sec, text="Ollama URL:").grid(row=0, column=0, sticky="w", pady=3)
        self.ollama_url_var = tk.StringVar(value=ollama.get("url", "http://127.0.0.1:11434"))
        ttk.Entry(sec, textvariable=self.ollama_url_var).grid(row=0, column=1, sticky="ew", padx=6, pady=3)
        ttk.Label(sec, text="Model:").grid(row=0, column=2, sticky="e", padx=(6, 4), pady=3)
        self.ollama_model_var = tk.StringVar(value=ollama.get("model", "qwen2.5vl:7b"))
        self.ollama_model_cmb = ttk.Combobox(sec, textvariable=self.ollama_model_var,
                                             state="readonly", width=16)
        self.ollama_model_cmb.grid(row=0, column=3, sticky="w", pady=3)
        Tooltip(self.ollama_model_cmb,
                "Vision model used for tagging. Pick from the models installed on "
                "the Ollama server; press Check to (re)load the list.")
        check_btn = ttk.Button(sec, text="Check", command=self._check_ollama)
        check_btn.grid(row=0, column=4, padx=(8, 0), pady=3)
        Tooltip(check_btn,
                "Contact the Ollama server at the address on the left and load the "
                "list of models it has installed. Use it after starting Ollama or "
                "pulling a new model.", wraplength=W)

        self.ollama_status = ttk.Label(sec, text="", foreground="#666")
        self.ollama_status.grid(row=1, column=0, columnspan=5, sticky="w", padx=6, pady=(4, 0))

        # ── Tag & Rename ───────────────────────────────────────────────────────
        sec = self._section(body, "Tag & Rename")

        # All Tag & Rename settings on one row.
        strip = ttk.Frame(sec)
        strip.grid(row=0, column=0, sticky="w", pady=3)

        self.straighten_var = tk.BooleanVar(value=bool(tag.get("auto_straighten", True)))
        chk = ttk.Checkbutton(strip, text="Auto-straighten rotated photos",
                              variable=self.straighten_var)
        chk.pack(side="left")
        Tooltip(chk, "Detects sideways photos and rotates them upright before tagging. "
                     "Only confident calls are acted on; ambiguous ones are left alone.")

        ttk.Label(strip, text="Confidence threshold:").pack(side="left", padx=(18, 4))
        self.straighten_conf_var = tk.DoubleVar(
            value=float(tag.get("straighten_min_confidence", 0.9)))
        spin = ttk.Spinbox(strip, from_=0.50, to=1.00, increment=0.05, width=6, format="%.2f",
                           textvariable=self.straighten_conf_var)
        spin.pack(side="left")
        Tooltip(spin, "0.50–1.00   (higher = fewer, safer rotations)   "
                      "Recommended: 0.90")

        ttk.Label(strip, text="Max image size sent to model:").pack(side="left", padx=(18, 4))
        self.tag_maxpx_var = tk.IntVar(value=int(tag.get("max_image_px", 1280)))
        maxpx_spin = ttk.Spinbox(strip, from_=0, to=4096, increment=128, width=6,
                                 textvariable=self.tag_maxpx_var)
        maxpx_spin.pack(side="left")
        ttk.Label(strip, text="px").pack(side="left", padx=(4, 0))
        Tooltip(maxpx_spin,
                "The image is downscaled to this longest edge (in pixels) before "
                "going to the vision model (your source files are never changed). "
                "Large photos otherwise OOM small-VRAM GPUs into an HTTP 400 — "
                "1280 px is plenty for describing and titling. Higher = more detail "
                "+ more VRAM. 0 = full resolution (not recommended).")

        # ── Batch Upscaler ─────────────────────────────────────────────────────
        sec = self._section(body, "Batch Upscaler")

        # THREE grid columns, not two: column 0 holds the Resolution Target and the
        # two checkboxes, column 1 the right-hand LABEL and column 2 its value box.
        # Splitting the label off is what makes the three value boxes line up: they
        # used to sit in packed sub-frames, so each started wherever its own label
        # ("Skip images over:" / "Confidence threshold:" / "Slowdown factor:")
        # happened to end. Now grid sizes column 1 to the widest label and every
        # box starts at the same x.
        c0 = ttk.Frame(sec)
        c0.grid(row=0, column=0, sticky="w", pady=3)
        ttk.Label(c0, text="Resolution Target:").pack(side="left", padx=(0, 4))
        self.restarget_var = tk.StringVar(value=self._current_preset_label(ups))
        restarget_cmb = ttk.Combobox(c0, textvariable=self.restarget_var, state="readonly",
                                     values=[p[0] for p in RESOLUTION_PRESETS], width=16)
        restarget_cmb.pack(side="left")
        Tooltip(restarget_cmb, "longer edge / shorter edge, in pixels")

        ttk.Label(sec, text="Skip images over:").grid(row=0, column=1, sticky="w",
                                                      padx=(18, 4), pady=3)
        skip = ttk.Frame(sec)
        skip.grid(row=0, column=2, sticky="w", pady=3)
        self.cutoff_var = tk.IntVar(value=int(ups.get("upscale_cutoff_pct", 66)))
        cut_spin = ttk.Spinbox(skip, from_=0, to=99, width=4, textvariable=self.cutoff_var)
        cut_spin.pack(side="left")
        ttk.Label(skip, text="% of target resolution").pack(side="left", padx=(4, 0))
        Tooltip(cut_spin, "Percentage of the target resolution.   (0 = upscale "
                          "everything eligible)   Recommended: 66")

        self.up_straighten_var = tk.BooleanVar(value=bool(ups.get("auto_straighten", True)))
        up_chk = ttk.Checkbutton(sec, text="Auto-straighten photos",
                                 variable=self.up_straighten_var)
        up_chk.grid(row=1, column=0, sticky="w", pady=3)
        Tooltip(up_chk, "Rotates a sideways photo upright BEFORE upscaling so the result still "
                        "fits a 4K screen. Without this, the upscaler targets the wrong axis and "
                        "the image no longer fits once Tag & Rename straightens it. The source is "
                        "never modified (a temp copy is rotated and upscaled).")
        ttk.Label(sec, text="Confidence threshold:").grid(row=1, column=1, sticky="w",
                                                          padx=(18, 4), pady=3)
        self.up_straighten_conf_var = tk.DoubleVar(
            value=float(ups.get("straighten_min_confidence", 0.9)))
        up_spin = ttk.Spinbox(sec, from_=0.50, to=1.00, increment=0.05, width=6, format="%.2f",
                              textvariable=self.up_straighten_conf_var)
        up_spin.grid(row=1, column=2, sticky="w", pady=3)
        Tooltip(up_spin, "0.50–1.00   (higher = fewer, safer rotations)   "
                         "Recommended: 0.90")

        self.watchdog_var = tk.BooleanVar(value=bool(ups.get("watchdog_enabled", True)))
        wd_chk = ttk.Checkbutton(
            sec, text="Performance watchdog (auto-stop)",
            variable=self.watchdog_var)
        wd_chk.grid(row=2, column=0, sticky="w", pady=3)
        Tooltip(wd_chk,
                "Watches per-image upscale time. If it degrades to a sustained multiple of the "
                "normal speed (the GPU thrashing VRAM into system RAM) OR hits a hard out-of-memory "
                "error, the run auto-stops after the current image and you're notified (log, Discord, "
                "taskbar flash). The resume cache continues the queue after you reboot — the known cure.")
        ttk.Label(sec, text="Slowdown factor:").grid(row=2, column=1, sticky="w",
                                                     padx=(18, 4), pady=3)
        wd = ttk.Frame(sec)
        wd.grid(row=2, column=2, sticky="w", pady=3)
        self.watchdog_factor_var = tk.DoubleVar(value=float(ups.get("watchdog_factor", 3.0)))
        wf_spin = ttk.Spinbox(wd, from_=1.5, to=10.0, increment=0.5, width=6, format="%.1f",
                              textvariable=self.watchdog_factor_var)
        wf_spin.pack(side="left")
        Tooltip(wf_spin, "How many times slower than the run's healthy rate (per megapixel) "
                         "counts as 'slow'.   Recommended: 3.0")
        ttk.Label(wd, text="for").pack(side="left", padx=(12, 4))
        self.watchdog_consec_var = tk.IntVar(value=int(ups.get("watchdog_consecutive", 2)))
        wc_spin = ttk.Spinbox(wd, from_=1, to=10, width=4, textvariable=self.watchdog_consec_var)
        wc_spin.pack(side="left")
        ttk.Label(wd, text="images in a row").pack(side="left", padx=(4, 0))
        Tooltip(wc_spin, "Consecutive slow images before stopping (filters out a "
                         "single odd image).   Recommended: 2")

        # ── Video Upscaler (#2) ───────────────────────────────────────────────────
        vid = CFG.get("video", {})
        sec = self._section(body, "Video Upscaler")
        # THREE label+widget column pairs (0/1, 2/3, 4/5) so the section reads as a
        # compact grid instead of one long stack: the settings rows below reuse the
        # SAME six columns, which is what keeps "Batch size / Temporal overlap / 4K
        # input noise" and the three checkboxes lined up under the row above them.
        # Only the last widget column stretches, so the groups keep even gaps.
        sec.columnconfigure(5, weight=1)
        _G2 = (24, 4)                     # left pad that opens the 2nd/3rd column pair
        ttk.Label(sec, text="Default target:").grid(row=0, column=0, sticky="w",
                                                    padx=(0, 4), pady=3)
        self.video_target_var = tk.StringVar(value=vid.get("target", "1080p"))
        vt_cb = ttk.Combobox(sec, textvariable=self.video_target_var, state="readonly",
                             width=10, values=["1080p", "1440p", "4K"])
        vt_cb.grid(row=0, column=1, sticky="w", pady=3)
        Tooltip(vt_cb,
                "The size pre-selected on the Video Upscaler tab. You can still "
                "pick a different one per video before queueing it.", wraplength=W)
        ttk.Label(sec, text="Output subfolder:").grid(row=0, column=2, sticky="w",
                                                      padx=_G2, pady=3)
        self.video_outsub_var = tk.StringVar(value=vid.get("output_subdir", "__upscaled__"))
        vo_ent = ttk.Entry(sec, textvariable=self.video_outsub_var, width=20)
        vo_ent.grid(row=0, column=3, sticky="w", pady=3)
        Tooltip(vo_ent,
                "Name of the folder created for upscaled videos when you have not "
                "chosen an output folder yourself.", wraplength=W)
        ttk.Label(sec, text="Work folder (staging):").grid(row=11, column=0, sticky="w",
                                                           padx=(0, 4), pady=3)
        self.video_workroot_var = tk.StringVar(value=vid.get("work_root", ""))
        wr = ttk.Frame(sec)
        wr.grid(row=11, column=1, columnspan=5, sticky="ew", pady=3)
        wr.columnconfigure(0, weight=1)
        wr_entry = ttk.Entry(wr, textvariable=self.video_workroot_var)
        wr_entry.grid(row=0, column=0, sticky="ew")
        wr_btn = ttk.Button(wr, text="Browse", width=8,
                            command=self._pick_video_workroot)
        wr_btn.grid(row=0, column=1, padx=(6, 0))
        Tooltip(wr_btn,
                "Choose the staging folder. Leave the box empty for the "
                "recommended default on the app drive.", wraplength=W)
        Tooltip(wr_entry,
                "Where segments are staged during a run. Leave EMPTY to use a fast "
                "local folder on the app drive (recommended). Staging locally means a "
                "network hiccup on the OUTPUT drive can't strand a run in progress: only "
                "the first read of the source and the final write of the output touch "
                "the network. If you set a custom path, keep it OUTSIDE your source "
                "folder or the scanner may re-read its own segments.")
        ttk.Label(sec, text="Output quality:").grid(row=0, column=4, sticky="w",
                                                    padx=_G2, pady=3)
        self.video_codec_var = tk.StringVar(value=self._video_codec_label(vid))
        vc_cb = ttk.Combobox(sec, textvariable=self.video_codec_var, state="readonly",
                             width=34, values=[lbl for lbl, _b, _t in _VIDEO_CODEC_OPTIONS])
        vc_cb.grid(row=0, column=5, sticky="w", pady=3)
        Tooltip(vc_cb,
                "How the finished video is compressed. Higher quality means a "
                "bigger file for the same picture; it does not change how long the "
                "upscaling itself takes.", wraplength=W)
        # The SeedVR2 weights used to have their own "Model:" row here. It is gone: the
        # single "Model:" picklist under "Default method for new queue items" now covers
        # BOTH engines, following the Method above it. The var lives on as the store for
        # the SeedVR2 half of that picklist (row 3 is deliberately left empty rather than
        # renumbering the whole grid).
        self.video_model_var = tk.StringVar(value=self._video_model_label(vid))
        # ── Advanced (leave on Auto) ──────────────────────────────────────────────
        # Batch (temporal window) and overlap default to Auto: the pod sizes them from
        # its real VRAM + the output resolution, so a user never has to learn SeedVR2's
        # knobs. These are overrides for power users; a wrong value self-corrects on the
        # pod (OOM auto-recovery). The picklists only offer valid 4n+1 / in-range values.
        ttk.Label(sec, text="Advanced (leave on Auto):", foreground="#888").grid(
            row=4, column=0, columnspan=6, sticky="w", pady=(8, 0))
        ttk.Label(sec, text="Batch size (window):").grid(row=5, column=0, sticky="w",
                                                         padx=(0, 4), pady=3)
        _bs = int(vid.get("batch_size", 0) or 0)
        self.video_batch_var = tk.StringVar(value=("Auto" if _bs <= 0 else str(_bs)))
        _batch_choices = ["Auto"] + [str(4 * n + 1) for n in range(1, 126)]   # 5,9,…,501
        bs_cb = ttk.Combobox(sec, textvariable=self.video_batch_var, state="readonly",
                             width=10, values=_batch_choices)
        bs_cb.grid(row=5, column=1, sticky="w", pady=3)
        Tooltip(bs_cb, "Frames SeedVR2 processes together (4n+1). Auto = the largest the "
                       "pod's VRAM safely allows for the target (more = smoother motion, "
                       "fewer seams). Big values past ~33 mostly cut overlap redundancy, not "
                       "boost quality, and need a big card (4K caps near 33 on 180 GB; bigger "
                       "fits only at 1440p/1080p). A too-large pick self-corrects via the pod's "
                       "OOM-recovery. Leave on Auto unless you're benchmarking.")
        ttk.Label(sec, text="Temporal overlap:").grid(row=5, column=2, sticky="w",
                                                      padx=_G2, pady=3)
        _ov = int(vid.get("temporal_overlap", -1))
        self.video_overlap_var = tk.StringVar(value=("Auto" if _ov < 0 else str(_ov)))
        ov_cb = ttk.Combobox(sec, textvariable=self.video_overlap_var, state="readonly",
                             width=10, values=["Auto"] + [str(n) for n in range(0, 13)])
        ov_cb.grid(row=5, column=3, sticky="w", pady=3)
        Tooltip(ov_cb, "Frames blended between batches to hide the seam (a quality floor, "
                       "not a cost knob: 3 left a visible break, 6 was undetectable). Auto "
                       "uses at least 6, more for large windows. 0 = hard cut. The pod caps "
                       "it below the batch.")
        self.video_compile_var = tk.BooleanVar(value=bool(vid.get("compile", True)))
        cmp_cb = ttk.Checkbutton(sec, text="Speed up with torch.compile (recommended)",
                                 variable=self.video_compile_var)
        cmp_cb.grid(row=6, column=0, columnspan=2, sticky="w", pady=(4, 0))
        Tooltip(cmp_cb, "Compiles the SeedVR2 model on the first segment of a run (that "
                        "segment is slower), then later segments run faster. Best on long "
                        "videos, where the one-time cost is spread over many segments.\n\n"
                        "It is not free: compiling raises VRAM use, so the largest batch "
                        "that still fits gets smaller (measured on a 24 GB card: about half "
                        "the frames per window). A smaller window can cost more speed than "
                        "compiling gains, so on a small card it can be a net loss. Benchmark "
                        "your card both ways if you care.\n\n"
                        "Local runs also need a C compiler and Triton; without them the run "
                        "goes ahead uncompiled and the log says so.")
        self.video_uniform_var = tk.BooleanVar(value=bool(vid.get("uniform_batch_size", True)))
        uni_cb = ttk.Checkbutton(sec, text="Uniform batch size (cleaner seams)",
                                 variable=self.video_uniform_var)
        uni_cb.grid(row=6, column=2, columnspan=2, sticky="w", padx=_G2, pady=(4, 0))
        Tooltip(uni_cb, "Pads the last (ragged) batch so it matches the rest, preventing a "
                        "flicker at the end of each chunk. Tiny extra compute, recommended on.")
        self.video_autotune_var = tk.BooleanVar(value=bool(vid.get("auto_tune_batch", True)))
        at_cb = ttk.Checkbutton(sec, text="Auto-tune batch size (long videos)",
                                variable=self.video_autotune_var)
        at_cb.grid(row=6, column=4, columnspan=2, sticky="w", padx=_G2, pady=(4, 0))
        Tooltip(at_cb, "On a multi-segment video, learns the largest batch size that fits "
                       "this card at this output size from the first segment's real VRAM use, "
                       "then reuses it (and remembers it for next time). Speeds up long runs "
                       "and avoids repeated out-of-memory back-offs. Only affects the AUTO "
                       "batch size (an explicit Advanced batch overrides it).")
        ttk.Label(sec, text="4K input noise:").grid(row=5, column=4, sticky="w",
                                                    padx=_G2, pady=3)
        _ns = float(vid.get("input_noise_scale", 0.0) or 0.0)
        self.video_noise_var = tk.StringVar(value=("Off" if _ns <= 0 else f"{_ns:g}"))
        ns_cb = ttk.Combobox(sec, textvariable=self.video_noise_var, state="readonly",
                             width=10, values=["Off", "0.02", "0.03", "0.05"])
        ns_cb.grid(row=5, column=5, sticky="w", pady=3)
        Tooltip(ns_cb, "Injects a little noise into the input to counter the soft, "
                       "over-smoothed look 4K upscales can have. Off is fine for 1080p/1440p; "
                       "try 0.02 if your 4K output looks plasticky.")
        self.video_confirm_var = tk.BooleanVar(value=bool(vid.get("confirm_before_rent", True)))
        # row 12, NOT 11: row 11 already holds the work-folder row above. Two widgets in one
        # grid cell do not push each other aside, they DRAW ON TOP of each other, so this
        # checkbox silently covered the "Work folder (staging)" label and its entry.
        confirm_chk = ttk.Checkbutton(
            sec, text="Confirm (show the cost estimate) before renting a pod",
            variable=self.video_confirm_var)
        confirm_chk.grid(row=12, column=0, columnspan=6, sticky="w", pady=(4, 0))
        Tooltip(confirm_chk,
                "Ask first, showing what the queue is expected to cost and how "
                "long it should take, before any billed machine is created. "
                "Leave this on unless the prompt is getting in your way.",
                wraplength=W)

        # ── Default method for new queue items ────────────────────────────────────
        # These are just the DEFAULTS pre-selected on the Video Upscaler tab; the actual
        # method is chosen PER VIDEO there (a queue can mix methods), so this is not a global
        # switch (rows 13+ so the existing grid is untouched).
        ttk.Label(sec, text="Default method for new queue items:", foreground="#888").grid(
            row=13, column=0, columnspan=6, sticky="w", pady=(10, 0))
        # Method + Model share one row, in their OWN frame: both comboboxes are 40 chars
        # wide, so gridding them into the six-column grid above would stretch its first
        # two columns and pull the three-across rows out of shape.
        mrow = ttk.Frame(sec)
        mrow.grid(row=14, column=0, columnspan=6, sticky="w", pady=3)
        ttk.Label(mrow, text="Method:").pack(side="left", padx=(0, 4))
        self.video_engine_var = tk.StringVar(value=self._video_engine_label(vid))
        eng_cb = ttk.Combobox(mrow, textvariable=self.video_engine_var, state="readonly",
                              width=40, values=[lbl for lbl, _v in _VIDEO_ENGINE_OPTIONS])
        eng_cb.pack(side="left")
        Tooltip(eng_cb,
                "The method PRE-SELECTED for a new video on the Video Upscaler tab. You still "
                "pick the method per video there, so one queue can mix SeedVR2 and Real-ESRGAN; "
                "this only sets the starting choice. (Real-ESRGAN is a local-GPU engine; remote "
                "pod runs are always SeedVR2.)\n\n"
                "SeedVR2 invents new detail (best quality) but is slow and VRAM-hungry on a "
                "local card. Real-ESRGAN is a fast, light, fixed 2x/4x upscaler that runs on "
                "almost any GPU and keeps text/edges cleaner, at some loss of fine invented "
                "detail. Try Real-ESRGAN if SeedVR2 is too slow on your machine.",
                wraplength=W)
        # ONE "Model:" picklist for both engines: it lists the SeedVR2 weights or the
        # Real-ESRGAN models depending on the Method above it. The two choices are stored
        # SEPARATELY (video_model_var -> dit_model, video_esrgan_model_var ->
        # fixed_ratio_model), so flipping Method back and forth never loses the other
        # engine's pick; this combobox is only the view onto whichever one is active.
        ttk.Label(mrow, text="Model:").pack(side="left", padx=(24, 4))
        self.video_esrgan_model_var = tk.StringVar(value=self._video_esrgan_model_label(vid))
        self.video_model_pick_var = tk.StringVar()
        self._model_cb = ttk.Combobox(
            mrow, textvariable=self.video_model_pick_var, state="readonly", width=40)
        self._model_cb.pack(side="left")
        self._model_tip = Tooltip(self._model_cb, self.MODEL_TIP_SEEDVR2, wraplength=W)
        self._model_cb.bind("<<ComboboxSelected>>", lambda _e: self._on_model_pick())
        # The Method drives which list this shows, so refresh it on every change.
        eng_cb.bind("<<ComboboxSelected>>", lambda _e: self._sync_model_choices())
        self._sync_model_choices()

        # ── SeedVR Settings (everything else in the upscale block) ──────────────
        # Placed AFTER the two tabs it serves (Batch Upscaler + Video Upscaler): these
        # are engine internals both of them share, so they read as a footnote to the
        # pair rather than as part of the still-image settings. Sections are packed in
        # creation order, so this position IS the on-screen order.
        sec = self._section(body, "SeedVR Settings")
        present = {k: v for k, v in ups.items() if k not in _SEEDVR_EXCLUDE}

        def _lbl(key):
            return _SEEDVR_LABELS.get(key, key.replace("_", " ").capitalize())

        placed = set()

        # Goal: fit every SeedVR control on ONE row even at the app's minimum
        # width. So they live in a single tight, left-packed strip with short
        # group labels and narrow controls, rather than the old two-row grid.
        strip = ttk.Frame(sec)
        strip.grid(row=0, column=0, sticky="w", pady=3)

        def _grp(text, pad_left):
            ttk.Label(strip, text=text).pack(side="left", padx=(pad_left, 4))

        if "attention_mode" in present:
            _grp("Attention mode:", 0)
            self._make_seedvr_control(strip, "attention_mode", present["attention_mode"],
                                      width=9, readonly=True).pack(side="left")
            placed.add("attention_mode")

        if "outage_threshold" in present:
            _grp("Outage threshold:", 14)
            self._make_seedvr_control(strip, "outage_threshold", present["outage_threshold"],
                                      width=3).pack(side="left")
            placed.add("outage_threshold")

        # VAE Tiled: two checkboxes whose own labels read Encode / Decode.
        if "encode_tiled" in present or "decode_tiled" in present:
            _grp("VAE Tiled:", 14)
            if "encode_tiled" in present:
                self._make_seedvr_control(strip, "encode_tiled", present["encode_tiled"],
                                          text="Encode").pack(side="left")
                placed.add("encode_tiled")
            if "decode_tiled" in present:
                self._make_seedvr_control(strip, "decode_tiled", present["decode_tiled"],
                                          text="Decode").pack(side="left", padx=(6, 0))
                placed.add("decode_tiled")

        # Tile Size: two narrow spinboxes labelled Encode / Decode.
        if "encode_tile_size" in present or "decode_tile_size" in present:
            _grp("Tile Size:", 14)
            if "encode_tile_size" in present:
                _grp("Encode", 0)
                self._make_seedvr_control(strip, "encode_tile_size",
                                          present["encode_tile_size"], width=6).pack(side="left")
                placed.add("encode_tile_size")
            if "decode_tile_size" in present:
                _grp("Decode", 8)
                self._make_seedvr_control(strip, "decode_tile_size",
                                          present["decode_tile_size"], width=6).pack(side="left")
                placed.add("decode_tile_size")

        # Any unrecognised keys fall back to one-per-row generic controls.
        row = 1
        for key, value in present.items():
            if key in placed:
                continue
            ttk.Label(sec, text=f"{_lbl(key)}:").grid(row=row, column=0, sticky="w", pady=3, padx=(0, 4))
            self._make_seedvr_control(sec, key, value).grid(row=row, column=1, sticky="w", pady=3)
            row += 1

        # ── Notifications ───────────────────────────────────────────────────────
        # Settings live in the "notifications" config section; resolve_settings()
        # also reads the legacy upscale.discord_webhook_url so existing installs
        # keep their webhook until the next Save migrates it.
        notif = notifications.resolve_settings(CFG)
        sec = self._section(body, "Notifications")
        sec.columnconfigure(1, weight=1)

        ttk.Label(sec, text="Discord webhook:").grid(row=0, column=0, sticky="w", pady=3)
        self.webhook_var = tk.StringVar(value=notif.get("discord_webhook_url", ""))
        webhook_entry = ttk.Entry(sec, textvariable=self.webhook_var)
        webhook_entry.grid(row=0, column=1, sticky="ew", padx=6, pady=3)
        Tooltip(webhook_entry,
                "Optional. Notifies when a queue finishes or on errors. Leave empty to disable.")
        wh_test = ttk.Button(sec, text="Test", command=self._test_webhook)
        wh_test.grid(row=0, column=2, pady=3)
        Tooltip(wh_test, "Send a sample alert to this webhook now, so you can see "
                         "whether it arrives in Discord.", wraplength=W)
        self.webhook_status = ttk.Label(sec, text="", foreground="#666")
        self.webhook_status.grid(row=1, column=1, columnspan=2, sticky="w", padx=6)

        ttk.Label(sec, text="Telegram bot token:").grid(row=2, column=0, sticky="w", pady=3)
        self.tg_token_var = tk.StringVar(value=notif.get("telegram_bot_token", ""))
        tg_token_entry = ttk.Entry(sec, textvariable=self.tg_token_var)
        tg_token_entry.grid(row=2, column=1, sticky="ew", padx=6, pady=3)
        Tooltip(tg_token_entry,
                "Optional. In Telegram, create a bot with @BotFather and paste the "
                "token it gives you here. Leave empty to disable Telegram alerts.")
        tg_test = ttk.Button(sec, text="Test", command=self._test_telegram)
        tg_test.grid(row=2, column=2, pady=3)
        Tooltip(tg_test, "Send a sample alert to the chat ID below, so you can see "
                         "whether it arrives in Telegram.", wraplength=W)

        ttk.Label(sec, text="Telegram chat ID:").grid(row=3, column=0, sticky="w", pady=3)
        self.tg_chat_var = tk.StringVar(value=notif.get("telegram_chat_id", ""))
        tg_chat_entry = ttk.Entry(sec, textvariable=self.tg_chat_var)
        tg_chat_entry.grid(row=3, column=1, sticky="ew", padx=6, pady=3)
        Tooltip(tg_chat_entry,
                "Where to send alerts. Open your bot in Telegram and press Start "
                "(or send it any message), then click Detect to fill this in.")
        tg_detect = ttk.Button(sec, text="Detect", command=self._detect_telegram)
        tg_detect.grid(row=3, column=2, pady=3)
        Tooltip(tg_detect,
                "Fill in the chat ID automatically by asking your bot who has "
                "written to it. Press Start in the Telegram chat first, otherwise "
                "there is nothing for it to find.", wraplength=W)
        self.tg_status = ttk.Label(sec, text="", foreground="#666")
        self.tg_status.grid(row=4, column=1, columnspan=2, sticky="w", padx=6)

        ttk.Label(sec, text="ntfy server:").grid(row=5, column=0, sticky="w", pady=3)
        self.ntfy_server_var = tk.StringVar(value=notif.get("ntfy_server", "https://ntfy.sh"))
        ntfy_server_entry = ttk.Entry(sec, textvariable=self.ntfy_server_var)
        ntfy_server_entry.grid(row=5, column=1, sticky="ew", padx=6, pady=3)
        Tooltip(ntfy_server_entry,
                "The ntfy server to publish to. Leave as https://ntfy.sh for the "
                "free public server, or point it at your own self-hosted ntfy.")

        ttk.Label(sec, text="ntfy topic:").grid(row=6, column=0, sticky="w", pady=3)
        self.ntfy_topic_var = tk.StringVar(value=notif.get("ntfy_topic", ""))
        ntfy_topic_entry = ttk.Entry(sec, textvariable=self.ntfy_topic_var)
        ntfy_topic_entry.grid(row=6, column=1, sticky="ew", padx=6, pady=3)
        Tooltip(ntfy_topic_entry,
                "A topic name you make up, then subscribe to in the ntfy app. On the "
                "public server anyone who knows the topic can read it, so pick an "
                "unguessable name. Leave empty to disable ntfy alerts.")
        ntfy_test = ttk.Button(sec, text="Test", command=self._test_ntfy)
        ntfy_test.grid(row=6, column=2, pady=3)
        Tooltip(ntfy_test, "Publish a sample alert to this server and topic, so you "
                           "can see whether it reaches the ntfy app.", wraplength=W)
        self.ntfy_status = ttk.Label(sec, text="", foreground="#666")
        self.ntfy_status.grid(row=7, column=1, columnspan=2, sticky="w", padx=6)

        ttk.Label(sec, text="Home Assistant URL:").grid(row=8, column=0, sticky="w", pady=3)
        self.ha_url_var = tk.StringVar(value=notif.get("ha_url", ""))
        ha_url_entry = ttk.Entry(sec, textvariable=self.ha_url_var)
        ha_url_entry.grid(row=8, column=1, sticky="ew", padx=6, pady=3)
        Tooltip(ha_url_entry,
                "The address you open Home Assistant at on your own network, for "
                "example http://homeassistant.local:8123. Leave empty to disable "
                "Home Assistant alerts. (Only needed if you do NOT use the MQTT "
                "integration below, which reports far more.)")

        ttk.Label(sec, text="HA webhook ID:").grid(row=9, column=0, sticky="w", pady=3)
        self.ha_webhook_var = tk.StringVar(value=notif.get("ha_webhook_id", ""))
        ha_webhook_entry = ttk.Entry(sec, textvariable=self.ha_webhook_var)
        ha_webhook_entry.grid(row=9, column=1, sticky="ew", padx=6, pady=3)
        Tooltip(ha_webhook_entry,
                "An ID you make up, and give to a Home Assistant automation that "
                "starts with a Webhook trigger. Build that automation FIRST; this "
                "only sends, it cannot create it. The ID is the only thing "
                "protecting it, so make it long and unguessable, and leave the "
                "automation's \"Only accessible from the local network\" on.")
        ha_test = ttk.Button(sec, text="Test", command=self._test_ha_webhook)
        ha_test.grid(row=9, column=2, pady=3)
        Tooltip(ha_test,
                "Send a sample alert to this webhook. It can only tell you Home "
                "Assistant answered: it answers the same way for an ID it has never "
                "heard of, so check the automation actually ran on the Home "
                "Assistant side.", wraplength=W)
        self.ha_status = ttk.Label(sec, text="", foreground="#666", wraplength=460, justify="left")
        self.ha_status.grid(row=10, column=1, columnspan=2, sticky="w", padx=6)

        # ── Home Assistant (MQTT) ───────────────────────────────────────────────
        mqtt = CFG.get("mqtt", {})
        sec = self._section(body, "Home Assistant (MQTT)")
        sec.columnconfigure(1, weight=1)

        ttk.Label(sec, wraplength=560, foreground="#666",
                  text=("Publishes status to an MQTT broker (e.g. Home Assistant's "
                        "Mosquitto). Enabled automatically whenever a broker host is "
                        "set below — clear the host to disable. The app verifies the "
                        "connection on startup.")
                  ).grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 4))

        self.mqtt_host_var = tk.StringVar(value=mqtt.get("host", ""))
        self.mqtt_port_var = tk.StringVar(value=str(mqtt.get("port", 1883)))
        self.mqtt_user_var = tk.StringVar(value=mqtt.get("username", ""))
        self.mqtt_pass_var = tk.StringVar(value=mqtt.get("password", ""))
        self.mqtt_cid_var  = tk.StringVar(
            value=mqtt.get("client_id", mqtt_publisher.DEFAULT_CLIENT_ID))

        # The whole broker connection on ONE row: host, port, credentials and both
        # buttons. The host entry is a fixed 22 chars (it used to stretch to fill the
        # section) — that is what makes the row fit inside the app's 1200 px minimum
        # width, and a hostname or IP needs no more. The fixed "mqtt://" hint keeps it
        # clear that only the host goes in the field, not the scheme or port. Client ID
        # has no control (mqtt_cid_var still carries it through to config.json): it's an
        # advanced field a non-technical user never needs; edit config.json to change.
        brow = ttk.Frame(sec)
        brow.grid(row=1, column=0, columnspan=4, sticky="w", pady=3)
        ttk.Label(brow, text="Broker host:").pack(side="left", padx=(0, 4))
        ttk.Label(brow, text="mqtt://").pack(side="left")
        ttk.Entry(brow, textvariable=self.mqtt_host_var, width=22).pack(side="left")
        ttk.Label(brow, text="Port:").pack(side="left", padx=(12, 4))
        port_spin = ttk.Spinbox(brow, from_=1, to=65535, width=7,
                                textvariable=self.mqtt_port_var)
        port_spin.pack(side="left")
        Tooltip(port_spin,
                "The broker's port. 1883 is the standard, and is almost always "
                "right; change it only if your broker was set up differently.",
                wraplength=W)
        ttk.Label(brow, text="Username:").pack(side="left", padx=(12, 4))
        ttk.Entry(brow, textvariable=self.mqtt_user_var, width=14).pack(side="left")
        ttk.Label(brow, text="Password:").pack(side="left", padx=(12, 4))
        ttk.Entry(brow, textvariable=self.mqtt_pass_var, show="•", width=14).pack(side="left")
        self.mqtt_test_btn = ttk.Button(brow, text="Test", command=self._test_mqtt)
        self.mqtt_test_btn.pack(side="left", padx=(12, 0))
        pub_btn = ttk.Button(brow, text="Publish now", command=self._publish_mqtt)
        pub_btn.pack(side="left", padx=(6, 0))
        Tooltip(self.mqtt_test_btn,
                "Try connecting to the broker with these details and report "
                "whether it worked. Nothing is saved by testing.", wraplength=W)
        Tooltip(pub_btn,
                "Send the current state (version, last run, idle/busy) to the "
                "broker straight away, so Home Assistant shows it without waiting "
                "for the next change.", wraplength=W)

        self.mqtt_status = ttk.Label(sec, text="", foreground="#666")
        self.mqtt_status.grid(row=3, column=0, columnspan=4, sticky="w", padx=6, pady=(4, 0))

        # ── Setup ────────────────────────────────────────────────────────────────
        sec = self._section(body, "Setup")
        row = ttk.Frame(sec)
        row.grid(row=0, column=0, sticky="w", pady=3)
        # Bold: the wizard is modal (grab_set) and a multi-step task, not a glance.
        wizard_btn = ttk.Button(row, text="Re-run first-start wizard",
                                command=self._rerun_wizard)
        wizard_btn.pack(side="left")
        use_window_button_style(wizard_btn)
        Tooltip(wizard_btn,
                "Open the setup guide again: it looks at your graphics card and "
                "suggests which models suit it, then offers to download the "
                "tagging model. Nothing changes until you finish it.", wraplength=W)
        ttk.Label(row, foreground="#666",
                  text="Re-detect your GPU and recommend upscaling / tagging models.").pack(
            side="left", padx=(12, 0))

        # ── Updates (kept last: rarely changed, low priority) ────────────────────
        sec = self._section(body, "Updates")
        row = ttk.Frame(sec)
        row.grid(row=0, column=0, sticky="w", pady=3)
        ttk.Label(row, text=f"Current version: {APP_VERSION}").pack(side="left")
        self.auto_update_var = tk.BooleanVar(value=update_auto_check_enabled())
        auto_upd_chk = ttk.Checkbutton(row, text="Check for updates on startup",
                                       variable=self.auto_update_var)
        auto_upd_chk.pack(side="left", padx=(18, 0))
        self.check_update_btn = ttk.Button(
            row, text="Check for updates now", command=self._check_updates)
        self.check_update_btn.pack(side="left", padx=(18, 0))
        Tooltip(auto_upd_chk,
                "Look for a newer version each time the app starts, and say so if "
                "one exists. Nothing is downloaded without asking you.",
                wraplength=W)
        Tooltip(self.check_update_btn,
                "Look for a newer version now and show what changed in it. A "
                "version you chose to skip is offered again.", wraplength=W)
        self.update_status = ttk.Label(sec, text="", foreground="#666")
        self.update_status.grid(row=1, column=0, sticky="w", padx=6, pady=(4, 0))

        # ── Save bar ────────────────────────────────────────────────────────────
        bar = ttk.Frame(body, padding=(8, 12))
        bar.pack(fill="x")
        save_btn = ttk.Button(bar, text="Save settings", command=self._save)
        save_btn.pack(side="left")
        Tooltip(save_btn,
                "Write every setting on this page to disk. Changes only take "
                "effect once saved: leaving the tab with unsaved edits asks what "
                "to do with them.", wraplength=W)
        self.save_status = ttk.Label(bar, text="", foreground="#666")
        self.save_status.pack(side="left", padx=12)

        # Baseline for unsaved-changes detection: a snapshot of the form as it
        # mirrors config.json right now. Re-taken on every successful save/revert.
        self._baseline = self._snapshot()

        # Live unsaved-changes indicator. Rather than wire a trace onto dozens of
        # heterogeneous vars (entries, spinboxes, comboboxes), poll is_dirty() on a
        # light timer: "Not saved" (red) the moment any field differs from the saved
        # state, "Saved." (green) right after a successful save, blank when clean and
        # unsaved-this-session. `_save_status_base` is what to show when clean;
        # `_save_status_hold` lets a transient message (e.g. a write error) linger.
        self._save_status_base = ""
        self._save_status_hold = 0.0
        self._refresh_save_indicator()

        # Probe the saved Ollama URL in the background so the status text already
        # reflects reachability by the time the user opens the Settings tab.
        self._check_ollama()

    def _make_seedvr_control(self, parent, key, value, width=None, readonly=False, text=None):
        """Build the editable control for a SeedVR field and register its var.
        Returns the widget (caller positions it). `width` overrides the default
        char-width; `readonly` makes a combobox pick-only; `text` labels a
        checkbutton (so the field's own word sits beside the box)."""
        if isinstance(value, bool):
            var = tk.BooleanVar(value=value)
            widget = ttk.Checkbutton(parent, variable=var, text=text or "")
            self._seedvr_vars[key] = (var, bool)
        elif isinstance(value, int):
            var = tk.StringVar(value=str(value))
            widget = ttk.Spinbox(parent, from_=0, to=100000,
                                 width=width if width is not None else 8, textvariable=var)
            self._seedvr_vars[key] = (var, int)
        else:
            var = tk.StringVar(value=str(value))
            if key in _SEEDVR_CHOICES:
                widget = ttk.Combobox(parent, textvariable=var,
                                      values=_SEEDVR_CHOICES[key],
                                      state="readonly" if readonly else "normal",
                                      width=width if width is not None else 16)
            else:
                widget = ttk.Entry(parent, textvariable=var,
                                  width=width if width is not None else 22)
            self._seedvr_vars[key] = (var, str)
        tip = _SEEDVR_TIPS.get(key)
        if tip:
            Tooltip(widget, tip, wraplength=Tooltip.WRAP_NARROW)
        return widget

    def _section(self, parent, title):
        lf = ttk.LabelFrame(parent, text=f"  {title}  ", padding=(10, 8))
        lf.pack(fill="x", padx=10, pady=(10, 0))
        return lf

    # ── helpers ──────────────────────────────────────────────────────────────

    def _current_preset_label(self, ups):
        mx, rs = int(ups.get("max_resolution", 3840)), int(ups.get("resolution", 2160))
        for label, pmx, prs in RESOLUTION_PRESETS:
            if pmx == mx and prs == rs:
                return label
        return RESOLUTION_PRESETS[0][0]

    def _check_ollama(self):
        """Probe the Ollama URL off the UI thread and report reachability."""
        url = self.ollama_url_var.get().strip()
        self.ollama_status.configure(text="Checking Ollama…", foreground="#666")

        def work():
            installed = ollama_installed()
            ok, value = ollama_list_models(url)
            self.after(0, lambda: self._apply_ollama_check(ok, value, installed))

        threading.Thread(target=work, daemon=True).start()

    def _apply_ollama_check(self, ok, value, installed):
        if ok:
            self.ollama_model_cmb.configure(values=value)
            inst = "installed" if installed else "not on PATH"
            self.ollama_status.configure(
                text=f"Reachable — {len(value)} model(s) available (ollama executable {inst}).",
                foreground="#1a7f37")
        else:
            inst = "Ollama executable found on PATH." if installed else \
                   "Ollama executable not found on PATH."
            self.ollama_status.configure(
                text=f"Not reachable at this URL — {value}. {inst}", foreground="#b3261e")

    def _test_webhook(self):
        ok, msg = test_discord_webhook(self.webhook_var.get())
        self.webhook_status.configure(text=msg, foreground="#1a7f37" if ok else "#b3261e")

    def _detect_telegram(self):
        """Read the bot's recent updates and fill in the chat ID. The user must
        have pressed Start (or messaged the bot) first."""
        self.tg_status.configure(text="Detecting chat…", foreground="#666")
        self.tg_status.update_idletasks()
        chat_id, msg = notifications.detect_telegram_chat(self.tg_token_var.get())
        if chat_id:
            self.tg_chat_var.set(chat_id)
        self.tg_status.configure(text=msg, foreground="#1a7f37" if chat_id else "#b3261e")

    def _test_telegram(self):
        """Verify the token and send a test message to the configured chat."""
        self.tg_status.configure(text="Testing…", foreground="#666")
        self.tg_status.update_idletasks()
        ok, msg = notifications.test_telegram(self.tg_token_var.get(), self.tg_chat_var.get())
        self.tg_status.configure(text=msg, foreground="#1a7f37" if ok else "#b3261e")

    def _test_ntfy(self):
        """Publish a test message to the configured ntfy topic. The auth token (if
        any, for a self-hosted server) is config-only — read it from CFG."""
        self.ntfy_status.configure(text="Testing…", foreground="#666")
        self.ntfy_status.update_idletasks()
        token = CFG.get("notifications", {}).get("ntfy_token", "")
        ok, msg = notifications.test_ntfy(
            self.ntfy_server_var.get(), self.ntfy_topic_var.get(), token)
        self.ntfy_status.configure(text=msg, foreground="#1a7f37" if ok else "#b3261e")

    def _test_ha_webhook(self):
        """POST a sample alert to the Home Assistant webhook. Both fields are on the
        form, so nothing is read from CFG. A success is reported in muted grey, not
        green: Home Assistant answers 200 to an unknown webhook ID too, so this
        proves only that it answered (finding F3). A failure IS meaningful and is
        shown in red."""
        self.ha_status.configure(text="Testing…", foreground="#666")
        self.ha_status.update_idletasks()
        ok, msg = notifications.test_ha_webhook(
            self.ha_url_var.get(), self.ha_webhook_var.get())
        self.ha_status.configure(text=msg, foreground="#666" if ok else "#b3261e")

    def _rerun_wizard(self):
        """Open the first-start Wizard on demand. Re-running is always allowed (the
        wizard re-detects the GPU and re-recommends models); it re-sets wizard_done
        on Finish/Skip. Lazy import + fail-safe so a wizard problem can't break the
        Settings tab."""
        try:
            from gui.wizard import FirstStartWizard
            FirstStartWizard(self.app)
        except Exception:
            messagebox.showerror(APP_TITLE, "Could not open the setup wizard.")

    def _check_updates(self):
        """Manual update check from Settings (always reports the outcome, and
        shows the prompt for an available update even if it was skipped before)."""
        self.check_update_btn.configure(state="disabled")
        self.update_status.configure(text="Checking for updates…", foreground="#666")

        def work():
            status, payload = updater.check_for_update(APP_VERSION)
            self.after(0, lambda: self._apply_update_check(status, payload))

        threading.Thread(target=work, daemon=True).start()

    def _apply_update_check(self, status, payload):
        self.check_update_btn.configure(state="normal")
        if status == "update":
            self.update_status.configure(
                text=f"Version {payload.version} is available.", foreground="#1a7f37")
            self.app.show_update_dialog(payload)
        elif status == "current":
            self.update_status.configure(
                text=f"You're on the latest version ({payload}).", foreground="#1a7f37")
        else:
            self.update_status.configure(text=payload, foreground="#b3261e")

    def _mqtt_fields(self):
        """The MQTT settings currently in the form (so Test works pre-Save)."""
        try:
            port = int(self.mqtt_port_var.get())
        except (ValueError, tk.TclError):
            port = 1883
        return {
            "host":      self.mqtt_host_var.get().strip(),
            "port":      port,
            "username":  self.mqtt_user_var.get().strip(),
            "password":  self.mqtt_pass_var.get(),
            "client_id": self.mqtt_cid_var.get().strip() or mqtt_publisher.DEFAULT_CLIENT_ID,
        }

    def _test_mqtt(self):
        cfg = self._mqtt_fields()
        if not cfg["host"]:
            self.mqtt_status.configure(text="Enter the broker host first.", foreground="#b3261e")
            return
        self.mqtt_test_btn.configure(state="disabled")
        self.mqtt_status.configure(text="Testing connection…", foreground="#666")

        def work():
            ok, msg = mqtt_publisher.test_connection(cfg)
            def apply():
                self.mqtt_test_btn.configure(state="normal")
                self.mqtt_status.configure(text=msg, foreground="#1a7f37" if ok else "#b3261e")
            self.after(0, apply)

        threading.Thread(target=work, daemon=True).start()

    def _publish_mqtt(self):
        cfg = self._mqtt_fields()
        if not cfg["host"]:
            self.mqtt_status.configure(text="Enter the broker host first.", foreground="#b3261e")
            return
        self.mqtt_status.configure(text="Publishing…", foreground="#666")

        def work():
            status, payload = updater.check_for_update(APP_VERSION)
            update_available = (status == "update")
            ok, msg = mqtt_publisher.publish_state(cfg, {
                mqtt_publisher.VERSION_TOPIC:        APP_VERSION,
                mqtt_publisher.UPDATE_TOPIC:         "yes" if update_available else "no",
                mqtt_publisher.LATEST_VERSION_TOPIC: payload.version if update_available else APP_VERSION,
            })
            self.after(0, lambda: self.mqtt_status.configure(
                text=msg, foreground="#1a7f37" if ok else "#b3261e"))

        threading.Thread(target=work, daemon=True).start()

    def _pick_folder(self, var):
        folder = filedialog.askdirectory(title="Choose a folder")
        if folder:
            var.set(os.path.normpath(folder))

    def _pick_video_workroot(self):
        """Browse for the video staging folder, warning (but not blocking) if the pick
        is a poor one. The authoritative check against the ACTUAL run source is the
        run-time guard in batch_video_upscale; this is early, best-effort feedback."""
        folder = filedialog.askdirectory(title="Choose a staging work folder")
        if not folder:
            return
        folder = os.path.normpath(folder)
        warn = self._video_workroot_warning(folder)
        if warn and not messagebox.askyesno(APP_TITLE, warn + "\n\nUse this folder anyway?"):
            return
        self.video_workroot_var.set(folder)

    def _video_workroot_warning(self, path):
        """A human warning if `path` is a poor staging choice (a network location, or
        inside the configured default video source/output folder), else None. Checked
        against the DEFAULT folders since the real run source is chosen per-run on the
        Video tab; batch_video_upscale.work_root_conflict is the hard run-time guard."""
        if not path:
            return None
        import batch_video_upscale as bv
        src = self.default_vsrc_var.get().strip()
        out = self.default_vout_var.get().strip()
        sub = self.video_outsub_var.get().strip() or "__upscaled__"
        if src and (bv._path_within(path, src) or bv._path_within(path, os.path.join(src, sub))):
            return ("That folder is inside your default video SOURCE folder. The scanner "
                    "would try to re-read the run's own segments as new videos. Pick a "
                    "folder outside the source tree.")
        if out and bv._path_within(path, out):
            return ("That folder is inside your default video OUTPUT folder. Staging there "
                    "mixes work files in with your results; a separate folder is cleaner.")
        if bv.is_network_path(path):
            return ("That folder is on a network drive. Local staging exists precisely so a "
                    "network hiccup can't stall or strand a run: staging on the network "
                    "reintroduces that risk. A folder on a local disk is strongly recommended.")
        return None

    def load_defaults(self):
        """Refresh the default-folder fields from config (called when a tab's
        Save-as-Default button updates them, so both views stay in sync)."""
        defs = CFG.get("defaults", {})
        self.default_src_var.set(defs.get("upscale_source", ""))
        self.default_out_var.set(defs.get("upscale_output", ""))
        self.default_tag_var.set(defs.get("tag_folder", ""))
        self.default_corig_var.set(defs.get("conciliate_original", ""))
        self.default_cproc_var.set(defs.get("conciliate_processed", ""))
        self.default_vsrc_var.set(defs.get("video_source", ""))
        self.default_vout_var.set(defs.get("video_output", ""))

    def _video_codec_label(self, vid):
        """The codec combobox label matching the saved video_backend/use_10bit."""
        backend = vid.get("video_backend", "ffmpeg")        # H.265 10-bit default
        ten = bool(vid.get("use_10bit", True))
        for lbl, b, t in _VIDEO_CODEC_OPTIONS:
            if b == backend and t == ten:
                return lbl
        return _VIDEO_CODEC_OPTIONS[0][0]

    def _video_model_label(self, vid):
        """The model combobox label matching the saved dit_model filename."""
        model = vid.get("dit_model", "seedvr2_ema_7b_fp16.safetensors")
        for lbl, fname in _VIDEO_MODEL_OPTIONS:
            if fname == model:
                return lbl
        return _VIDEO_MODEL_OPTIONS[0][0]

    def _video_engine_label(self, vid):
        """The local-engine combobox label matching the saved video.engine value."""
        engine = vid.get("engine", "seedvr2")
        for lbl, val in _VIDEO_ENGINE_OPTIONS:
            if val == engine:
                return lbl
        return _VIDEO_ENGINE_OPTIONS[0][0]

    def _video_esrgan_model_label(self, vid):
        """The Real-ESRGAN model combobox label matching the saved fixed_ratio_model key."""
        key = vid.get("fixed_ratio_model", _ESRGAN_DEFAULT_KEY)
        for lbl, k in _ESRGAN_MODEL_OPTIONS:
            if k == key:
                return lbl
        return _ESRGAN_MODEL_OPTIONS[0][0]

    MODEL_TIP_SEEDVR2 = (
        "The SeedVR2 weights used when the default method above is SeedVR2 (video only; "
        "the Batch Upscaler keeps its own). You still choose the method and model per "
        "video on the Video Upscaler tab.\n\n"
        "7B FP16 = best detail. 3B Q8 = smaller, frees VRAM for bigger windows. A remote "
        "pod downloads the chosen file to the volume on first use (one-time), so switching "
        "needs no reprovision; that first run is slower while it downloads.")
    MODEL_TIP_ESRGAN = (
        "The Real-ESRGAN model used when the default method above is Real-ESRGAN. You "
        "still choose the method and model per video on the Video Upscaler tab.\n\n"
        "Compact is fast and low-VRAM (the recommended default). Quality gives sharper "
        "fine detail but is much slower and needs more VRAM. The chosen model is "
        "downloaded (and checked) on first use.")

    def _model_store_var(self):
        """The var holding the model choice for the currently selected Method, paired
        with that engine's option list. SeedVR2 and Real-ESRGAN keep separate stores so
        neither is lost when the Method is flipped."""
        is_fixed = any(val == "fixed_ratio" and lbl == self.video_engine_var.get()
                       for lbl, val in _VIDEO_ENGINE_OPTIONS)
        if is_fixed:
            return self.video_esrgan_model_var, _ESRGAN_MODEL_OPTIONS, self.MODEL_TIP_ESRGAN
        return self.video_model_var, _VIDEO_MODEL_OPTIONS, self.MODEL_TIP_SEEDVR2

    def _sync_model_choices(self):
        """Point the one "Model:" picklist at the selected Method's model list and show
        that engine's stored choice. Fail-safe: never break the tab over a UI refresh."""
        try:
            store, options, tip = self._model_store_var()
            self._model_cb.configure(values=[lbl for lbl, _v in options])
            self.video_model_pick_var.set(store.get() or options[0][0])
            self._model_tip.set_text(tip)
        except Exception:                            # noqa: BLE001
            pass

    def _on_model_pick(self):
        """Write the picked label back into the ACTIVE engine's store var (the combobox
        itself is only a view; _video_section reads the two stores)."""
        try:
            store, _options, _tip = self._model_store_var()
            store.set(self.video_model_pick_var.get())
        except Exception:                            # noqa: BLE001
            pass

    def _video_section(self):
        """The proposed `video` config block from the form. Only the exposed keys
        are set; config-only keys (segment_seconds, spin_up_seconds, …) are left
        untouched by _save's per-section update()."""
        backend, ten = _VIDEO_CODEC_OPTIONS[0][1], _VIDEO_CODEC_OPTIONS[0][2]
        for lbl, b, t in _VIDEO_CODEC_OPTIONS:
            if lbl == self.video_codec_var.get():
                backend, ten = b, t
                break
        bs = self.video_batch_var.get()
        batch = 0 if bs.lower() == "auto" else int(bs)      # 0 = auto (pod sizes it)
        ov = self.video_overlap_var.get()
        overlap = -1 if ov.lower() == "auto" else int(ov)   # -1 = auto (scaled to batch)
        ns = self.video_noise_var.get()
        noise = 0.0 if ns.lower() in ("off", "") else float(ns)
        model = next((f for lbl, f in _VIDEO_MODEL_OPTIONS
                      if lbl == self.video_model_var.get()), _VIDEO_MODEL_OPTIONS[0][1])
        engine = next((v for lbl, v in _VIDEO_ENGINE_OPTIONS
                       if lbl == self.video_engine_var.get()), _VIDEO_ENGINE_OPTIONS[0][1])
        esrgan_model = next((k for lbl, k in _ESRGAN_MODEL_OPTIONS
                             if lbl == self.video_esrgan_model_var.get()), _ESRGAN_DEFAULT_KEY)
        return {
            "engine":              engine,
            "fixed_ratio_model":   esrgan_model,
            "target":              self.video_target_var.get(),
            "output_subdir":       self.video_outsub_var.get().strip() or "__upscaled__",
            "work_root":           self.video_workroot_var.get().strip(),
            "video_backend":       backend,
            "use_10bit":           ten,
            "dit_model":           model,
            "batch_size":          batch,
            "temporal_overlap":    overlap,
            "compile":             bool(self.video_compile_var.get()),
            "uniform_batch_size":  bool(self.video_uniform_var.get()),
            "auto_tune_batch":     bool(self.video_autotune_var.get()),
            "input_noise_scale":   noise,
            "confirm_before_rent": bool(self.video_confirm_var.get()),
        }

    def _collect(self):
        """Build the config sections the form currently describes, without
        touching CFG. Returns (sections, errors): `sections` maps section name →
        proposed {key: value}; `errors` names any field that failed validation.
        Shared by _save (apply) and the unsaved-changes detection (compare)."""
        errors = []
        seedvr_out = {}
        for key, (var, typ) in self._seedvr_vars.items():
            raw = var.get()
            if typ is int:
                try:
                    seedvr_out[key] = int(str(raw).strip())
                except ValueError:
                    errors.append(_SEEDVR_LABELS.get(key, key))
            elif typ is bool:
                seedvr_out[key] = bool(var.get())
            else:
                seedvr_out[key] = str(raw).strip()

        ups = {}
        try:
            ups["upscale_cutoff_pct"] = max(0, min(99, int(self.cutoff_var.get())))
        except (ValueError, tk.TclError):
            errors.append("Skip images over")
        for label, pmx, prs in RESOLUTION_PRESETS:
            if label == self.restarget_var.get():
                ups["max_resolution"], ups["resolution"] = pmx, prs
                break
        ups["auto_straighten"] = bool(self.up_straighten_var.get())
        try:
            up_conf = round(float(self.up_straighten_conf_var.get()), 2)
        except (ValueError, tk.TclError):
            up_conf = 0.9
        ups["straighten_min_confidence"] = min(1.0, max(0.5, up_conf))
        # Normalise the HA webhook pair at collect time, so a full endpoint pasted
        # into either box is stored (and shown after Save) already split.
        ha_url, ha_webhook_id = notifications.split_ha_webhook(
            self.ha_url_var.get(), self.ha_webhook_var.get())

        ups["watchdog_enabled"] = bool(self.watchdog_var.get())
        try:
            wd_factor = round(float(self.watchdog_factor_var.get()), 1)
        except (ValueError, tk.TclError):
            wd_factor = 3.0
        ups["watchdog_factor"] = min(10.0, max(1.5, wd_factor))
        try:
            ups["watchdog_consecutive"] = max(1, min(10, int(self.watchdog_consec_var.get())))
        except (ValueError, tk.TclError):
            ups["watchdog_consecutive"] = 2
        ups.update(seedvr_out)

        try:
            conf = round(float(self.straighten_conf_var.get()), 2)
        except (ValueError, tk.TclError):
            conf = 0.9
        try:
            max_px = max(0, int(self.tag_maxpx_var.get()))
        except (ValueError, tk.TclError):
            max_px = 1280

        sections = {
            "ollama": {
                "url":   self.ollama_url_var.get().strip() or "http://127.0.0.1:11434",
                "model": self.ollama_model_var.get().strip(),
            },
            "upscale": ups,
            "defaults": {
                "upscale_source": self.default_src_var.get().strip(),
                "upscale_output": self.default_out_var.get().strip(),
                "tag_folder":     self.default_tag_var.get().strip(),
                "conciliate_original":  self.default_corig_var.get().strip(),
                "conciliate_processed": self.default_cproc_var.get().strip(),
                "video_source":  self.default_vsrc_var.get().strip(),
                "video_output":  self.default_vout_var.get().strip(),
            },
            "tagging": {
                "auto_straighten": bool(self.straighten_var.get()),
                "straighten_min_confidence": min(1.0, max(0.5, conf)),
                "max_image_px": max_px,
            },
            "video": self._video_section(),
            "updates": {"auto_check": bool(self.auto_update_var.get())},
            "mqtt": self._mqtt_fields(),
            "notifications": {
                "discord_webhook_url": self.webhook_var.get().strip(),
                "telegram_bot_token":  self.tg_token_var.get().strip(),
                "telegram_chat_id":    self.tg_chat_var.get().strip(),
                "ntfy_server":         self.ntfy_server_var.get().strip(),
                "ntfy_topic":          self.ntfy_topic_var.get().strip(),
                "ha_url":              ha_url,
                "ha_webhook_id":       ha_webhook_id,
            },
        }
        return sections, errors

    def _snapshot(self):
        """A stable, comparable key for the form's current contents — the basis
        for unsaved-changes detection. Invalid input counts as 'changed'."""
        sections, errors = self._collect()
        return json.dumps(sections, sort_keys=True), bool(errors)

    def is_dirty(self):
        """True if the form differs from the last-saved (baseline) state."""
        try:
            return self._snapshot() != self._baseline
        except Exception:
            return False

    def _save(self):
        """Persist the form to config.json. Returns True on success; on a
        validation error it warns and returns False (nothing is written)."""
        sections, errors = self._collect()
        if errors:
            messagebox.showwarning(
                APP_TITLE, "These fields need a whole number:\n  • " + "\n  • ".join(errors))
            return False

        # A poor staging folder (network / inside the source tree) is allowed but warned
        # once, only when the value actually changed (so re-saving other settings doesn't
        # re-prompt). Also catches a hand-typed path the Browse dialog never validated.
        wr = self.video_workroot_var.get().strip()
        if wr != (CFG.get("video", {}) or {}).get("work_root", ""):
            warn = self._video_workroot_warning(wr)
            if warn and not messagebox.askyesno(APP_TITLE, warn + "\n\nSave this setting anyway?"):
                return False

        for name, values in sections.items():
            target = CFG.setdefault(name, {})
            if name == "mqtt":
                target.pop("enabled", None)   # MQTT is now gated by host being set
            if name == "runpod":
                target.pop("max_price_per_hour", None)          # 0.3.4: split per task
                target.pop("max_price_per_hour_upscale", None)  # 0.4.0: no auto-fallback
                target.pop("max_price_per_hour_tag", None)      # GPU is never substituted
            if name == "upscale":
                target.pop("discord_webhook_url", None)  # 0.3.8: moved to notifications
            target.update(values)

        if save_config():
            self._baseline = self._snapshot()    # the form is now the saved state
            # Apply MQTT changes immediately (connect/disconnect/reconfigure).
            self.app.restart_mqtt()
            self._save_status_base = "Saved."    # the live indicator renders it green
            self.save_status.configure(text="Saved.", foreground="#1a7f37")
            return True
        # Write failed — hold the error on screen so the live indicator doesn't
        # immediately overwrite it with "Not saved".
        self._save_status_hold = time.time() + 6
        self.save_status.configure(
            text="Could not write config.json (check file permissions).",
            foreground="#b3261e")
        return False

    def _refresh_save_indicator(self):
        """Light timer that keeps `save_status` reflecting the unsaved-changes
        state live: 'Not saved' (red) whenever the form differs from the saved
        state, otherwise the clean-state base text ('Saved.' green after a save,
        blank before). A transient message (e.g. a write error) is left alone until
        `_save_status_hold` expires. Polling avoids tracing dozens of mixed vars."""
        try:
            if not self.save_status.winfo_exists():
                return
            if time.time() >= self._save_status_hold:
                if self.is_dirty():
                    self.save_status.configure(text="Not saved", foreground="#b3261e")
                else:
                    base = self._save_status_base
                    self.save_status.configure(
                        text=base, foreground="#1a7f37" if base == "Saved." else "#666")
        except Exception:                       # noqa: BLE001 (never let the timer die loudly)
            pass
        self.after(400, self._refresh_save_indicator)

    def revert(self):
        """Discard unsaved edits: reset every field to the values in CFG."""
        ollama = CFG.get("ollama", {})
        ups    = CFG.get("upscale", {})
        defs   = CFG.get("defaults", {})
        tag    = CFG.get("tagging", {})
        mqtt   = CFG.get("mqtt", {})
        self.default_src_var.set(defs.get("upscale_source", ""))
        self.default_out_var.set(defs.get("upscale_output", ""))
        self.default_tag_var.set(defs.get("tag_folder", ""))
        self.default_corig_var.set(defs.get("conciliate_original", ""))
        self.default_cproc_var.set(defs.get("conciliate_processed", ""))
        self.default_vsrc_var.set(defs.get("video_source", ""))
        self.default_vout_var.set(defs.get("video_output", ""))
        self.ollama_url_var.set(ollama.get("url", "http://127.0.0.1:11434"))
        self.ollama_model_var.set(ollama.get("model", "qwen2.5vl:7b"))
        self.straighten_var.set(bool(tag.get("auto_straighten", True)))
        self.tag_maxpx_var.set(int(tag.get("max_image_px", 1280)))
        self.straighten_conf_var.set(float(tag.get("straighten_min_confidence", 0.9)))
        self.restarget_var.set(self._current_preset_label(ups))
        self.cutoff_var.set(int(ups.get("upscale_cutoff_pct", 66)))
        self.up_straighten_var.set(bool(ups.get("auto_straighten", True)))
        self.up_straighten_conf_var.set(float(ups.get("straighten_min_confidence", 0.9)))
        self.watchdog_var.set(bool(ups.get("watchdog_enabled", True)))
        self.watchdog_factor_var.set(float(ups.get("watchdog_factor", 3.0)))
        self.watchdog_consec_var.set(int(ups.get("watchdog_consecutive", 2)))
        notif = notifications.resolve_settings(CFG)
        self.webhook_var.set(notif.get("discord_webhook_url", ""))
        self.tg_token_var.set(notif.get("telegram_bot_token", ""))
        self.tg_chat_var.set(notif.get("telegram_chat_id", ""))
        self.ntfy_server_var.set(notif.get("ntfy_server", "https://ntfy.sh"))
        self.ntfy_topic_var.set(notif.get("ntfy_topic", ""))
        self.ha_url_var.set(notif.get("ha_url", ""))
        self.ha_webhook_var.set(notif.get("ha_webhook_id", ""))
        vid = CFG.get("video", {})
        self.video_target_var.set(vid.get("target", "1080p"))
        self.video_outsub_var.set(vid.get("output_subdir", "__upscaled__"))
        self.video_workroot_var.set(vid.get("work_root", ""))
        self.video_codec_var.set(self._video_codec_label(vid))
        self.video_model_var.set(self._video_model_label(vid))
        self.video_engine_var.set(self._video_engine_label(vid))
        self.video_esrgan_model_var.set(self._video_esrgan_model_label(vid))
        self._sync_model_choices()      # re-point the shared "Model:" picklist
        _vbs = int(vid.get("batch_size", 0) or 0)
        self.video_batch_var.set("Auto" if _vbs <= 0 else str(_vbs))
        _vov = int(vid.get("temporal_overlap", -1))
        self.video_overlap_var.set("Auto" if _vov < 0 else str(_vov))
        self.video_compile_var.set(bool(vid.get("compile", True)))
        self.video_uniform_var.set(bool(vid.get("uniform_batch_size", True)))
        self.video_autotune_var.set(bool(vid.get("auto_tune_batch", True)))
        _vns = float(vid.get("input_noise_scale", 0.0) or 0.0)
        self.video_noise_var.set("Off" if _vns <= 0 else f"{_vns:g}")
        self.video_confirm_var.set(bool(vid.get("confirm_before_rent", True)))
        self.auto_update_var.set(update_auto_check_enabled())
        self.mqtt_host_var.set(mqtt.get("host", ""))
        self.mqtt_port_var.set(str(mqtt.get("port", 1883)))
        self.mqtt_user_var.set(mqtt.get("username", ""))
        self.mqtt_pass_var.set(mqtt.get("password", ""))
        self.mqtt_cid_var.set(mqtt.get("client_id", mqtt_publisher.DEFAULT_CLIENT_ID))
        for key, (var, typ) in self._seedvr_vars.items():
            if key in ups:
                var.set(ups[key] if typ is bool else str(ups[key]))
        self._baseline = self._snapshot()
        self._save_status_base = ""          # discard the "Saved." indicator too


# ─────────────────────────────────────────────
#  TAB 5 — RUNPOD (remote pod settings)
# ─────────────────────────────────────────────
