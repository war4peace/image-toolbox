# image-toolbox

AI-leveraged image toolbox for Windows: **upscale** low-resolution photos, and
**describe & rename** them using a local vision model. Built to revive personal
photo collections and old pictures taken with early digital cameras.

> ## ⚠️ Work in progress — do NOT use it on important data.
> Always test on a small, disposable sample first. I am not responsible for data
> loss. Use this tool at your own risk. *(That said: the upscaler never modifies
> your source files — it only writes new images to a separate output folder.)*

The toolbox runs the [SeedVR2](https://github.com/ByteDance-Seed/SeedVR) upscaling
pipeline **directly in-process** — no ComfyUI, no server to start. Tagging uses a
local [Ollama](https://ollama.com) vision model. Everything runs on your machine.

---

## Install (Windows installer — recommended)

No Git, no Python knowledge required:

1. **Download** `ImageToolboxSetup.exe` from the [latest release](https://github.com/war4peace/image-toolbox/releases/latest).
2. **Run it** and click through the installer (no administrator rights needed).
3. **Double-click** the *Image Toolbox* shortcut.

The first launch opens a setup window that downloads the required components
(Python, PyTorch with CUDA, the SeedVR2 engine — about 3 GB) and then starts the
app. It also offers to install [Ollama](https://ollama.com) and the vision model
used by **Tag & Rename** (~6 GB; optional — upscaling works without it, and you
can decline). The first upscale you run additionally downloads the AI upscaling
model weights (~16 GB) automatically. Everything the setup prints is saved to
`bootstrap.log` next to the app for later review.

> **Windows SmartScreen note:** because the installer is a new, unsigned
> download, Windows may show *"Windows protected your PC — Unknown publisher"*.
> Click **More info → Run anyway**. The installer is built automatically from the
> public source in this repository by GitHub Actions; you can verify the build on
> the repository's **Actions** tab.

**Requirements:** Windows 10/11 (64-bit), an NVIDIA GPU with current drivers
(8 GB VRAM minimum), an internet connection, and ~25 GB of free disk space
(plus ~6 GB if you install the tagging model). PyTorch ships its own CUDA
runtime, so a separate CUDA Toolkit install is **not** required.

---

## Run from source

If you'd rather run from the repository instead of the installer:

```powershell
# 1. Clone this repo and the SeedVR2 engine into it.
#    (The engine lives in a repo named "ComfyUI-SeedVR2..." but is used
#     directly in-process here — the ComfyUI application itself is NOT needed.)
git clone https://github.com/war4peace/image-toolbox
cd image-toolbox
git clone https://github.com/numz/ComfyUI-SeedVR2_VideoUpscaler seedvr2

# 2. Create the Python environment (Python 3.12, NVIDIA GPU required).
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install torch torchvision --index-url https://download.pytorch.org/whl/cu128
.venv\Scripts\python.exe -m pip install -r seedvr2\requirements.txt pillow piexif timm

# 3. Launch the GUI (model weights ~16 GB download automatically on first use).
.venv\Scripts\pythonw.exe toolbox_gui.py
```

Or just double-click **`Image Toolbox.cmd`** after cloning — it bootstraps the
environment and launches the app for you.

You can also run the tools headless from PowerShell:

```powershell
.venv\Scripts\python.exe batch_upscale.py "X:\Your\Photos"               # upscale
.venv\Scripts\python.exe batch_upscale.py "X:\Your\Photos" "Z:\Output"   # custom output
.venv\Scripts\python.exe tag_and_rename.py "X:\Your\Photos"              # tag & rename
.venv\Scripts\python.exe conciliate.py "X:\Your\Photos" "Z:\Output"     # conciliate (archive)
```

The GUI and the scripts share the same logs and cache database (`db/cache.db`),
so you can mix and match freely.

---

## The app

`toolbox_gui.py` is a Windows GUI (pure Python standard-library tkinter — no extra
packages) with four tabs.

### Batch Upscaler

- **High-quality upscaling** with SeedVR2, prioritising quality over speed. The
  target is capped at 3840 × 2160 by default (a **Resolution Target** of 4K, 2K
  or 1080p is selectable in Settings), so results display at native resolution on
  common screens.
- **Your originals are never touched.** Upscaled images are written to a separate
  output folder that mirrors the source folder tree and filenames.
- **Skip-cutoff:** images already close to the target are skipped (default 66% of
  the target on either axis — i.e. anything that would gain less than ~1.5×).
  Set it to 0 in Settings to upscale everything eligible.
- **Resilient long runs:** a cache (in the local SQLite database `db/cache.db`)
  lets a stopped batch resume where it left off; corrupt and missing files are
  detected, logged and skipped
  (corrupt files are listed at the end so you can review them); a **second pass**
  re-scans the source when the batch finishes and processes anything new that
  appeared while it ran.
- **Live feedback:** a thumbnail wall, two-row status (current + previous file),
  a progress bar, and an estimated time remaining that refreshes after each image.
- **Pause / resume / stop** are buttons; a stop finishes the current image first
  so a file is never left half-written.
- **Works with mapped network drives.**

### Tag & Rename

- Analyses each image with a local Ollama vision model, writes a description into
  EXIF, and renames the file to `OriginalName_Condensed_Description.ext`.
- **Auto-straighten** (on by default): a small local CNN detects photos shot with
  the camera held sideways and rotates them upright *before* tagging — which also
  improves the descriptions. Only confident calls are acted on; upside-down and
  ambiguous images are left alone and logged, so a photo is never wrongly rotated.
  Toggle it and tune the confidence threshold in **Settings**; rotations are
  reverted by **Undo** like everything else.
- **Selectable description language**, plus force-tag / force-rename options.
- **One-click Undo** restores file names, EXIF descriptions, or both — every
  change is recorded to an undo cache before anything is modified.
- Already-tagged files are detected and skipped on re-runs.
- **The vision model is your choice** (set it in **Settings**). The default is
  [`qwen2.5vl:7b`](https://ollama.com/library/qwen2.5vl) — the most accurate of
  the models tried, reading faint on-screen text and inferring fine detail; it
  needs ~16 GB VRAM (a 16 GB+ GPU). If you have less VRAM, switch to
  [`minicpm-v`](https://ollama.com/library/minicpm-v) — fast and light (~7.6 GB
  VRAM, runs on an 8 GB GPU), with a welcome habit of describing only what it can
  clearly see instead of guessing. In testing, `llava:34b` was the slowest,
  heaviest *and* least accurate of the models tried — it's not recommended.

### Conciliation

Once you're happy with the upscaled (and optionally tagged & renamed) results,
**Conciliation** moves them back into your original folder tree so the originals
are replaced by their high-quality versions — no manual shuffling.

- Pick an **Original Photos** folder and a **Processed Photos** folder, then
  **Scan / Preview**. Nothing is touched until you click **Run**: the preview
  shows a per-folder summary (how many will be *replaced*, how many images have
  *no match*, and how many non-image files are *kept*).
- Each original is matched to its processed counterpart using the cache
  database, falling back to filename matching when there's no cache — so it works
  for both upscaled-only and upscaled-then-tagged/renamed files.
- Two operations: **Archive originals** (default — moves each original into an
  `__Archive__` subfolder) or **Delete originals** (permanent, with an extra
  confirmation). The processed file then takes the original's place, keeping its
  descriptive name if it was tagged.
- **Safety first:** an original with no processed counterpart is never touched,
  and non-image files are never touched. After a run, emptied processed folders
  (e.g. a leftover `__upscaled__`) are cleaned up. The tab is locked while the
  Upscaler or Tag & Rename is running, since they may share the same folders.

### Settings

Everything that used to require hand-editing `config.json` is here:

- **Ollama URL** with a reachability check, and a **model** picklist populated
  from the models installed on your machine.
- **Auto-straighten** toggle and confidence threshold for Tag & Rename.
- **Resolution Target** (4K / 2K / 1080p) and the **skip-cutoff** percentage.
- **SeedVR settings** (attention mode, color correction, models, tiling, etc.).
- **Discord webhook** with a **Test** button.
- **Default folders** for each tool, also settable from each tab's
  *Save as Default* button.

### Notifications

Set a **Discord webhook** (in Settings) to get a message when a queue finishes —
for both the Upscaler and Tag & Rename — and on errors (repeated failures, an
engine that fails to start, Ollama going unreachable).

---

## Configuration

Settings live in `config.json` next to the app, with these sections:

| Section    | What it holds                                                        |
|------------|----------------------------------------------------------------------|
| `seedvr2`  | Paths to the engine repo, model weights, and the venv Python.        |
| `ollama`   | Ollama server URL and the vision model for tagging.                  |
| `upscale`  | Resolution target, skip-cutoff, SeedVR pipeline options, webhook.    |
| `tagging`  | Resolution threshold, timeouts, camera-filename patterns, etc.       |
| `defaults` | Pinned default folders for each tool.                                |

You normally never edit this file by hand — use the **Settings** tab. The
installer never overwrites your `config.json` on upgrade, and removes it only on
a full uninstall.

---

## Samples

The [samples](/samples/) folder contains [original images](/samples/original/)
and their [upscaled](/samples/upscaled/) versions, so you can compare pairs
side-by-side and judge the upscaler's strengths and weaknesses.

---

## Notes

- **About the code:** these tools were written with the help of Claude (Anthropic)
  by someone who is not a trained developer — "vibecoding", as some call it. This
  is a personal project, shared for anyone to use at no cost.
- **Maintainers:** the installer is built by the `build-installer` GitHub Actions
  workflow from `installer/ImageToolbox.iss` whenever a `v*` tag is pushed.
