# CLAUDE.md

Guidance for working in this repository.

## What this is

**Image Toolbox** — an AI-leveraged image toolbox for Windows that **upscales**
low-resolution photos and **describes & renames** them using a local vision
model. Built to revive personal photo collections and old digital-camera
pictures.

Two design promises shape everything:

- **The upscaler never modifies source files** — it writes new images to a
  separate output folder that mirrors the source tree.
- **Dependency-light & local** — the GUI is pure standard-library tkinter;
  everything runs on the user's machine (no ComfyUI, no server). The upscaling
  pipeline runs **in-process**; tagging talks to a local Ollama server over HTTP.

**Requirements:** Windows 10/11 (64-bit), an NVIDIA GPU (8 GB VRAM min) with a
CUDA build of PyTorch. The app is currently **Windows-only** (PowerShell
bootstrap, tkinter GUI, Windows paths, `CREATE_NO_WINDOW`).

## Current features

**Batch Upscaler** — high-quality SeedVR2 upscaling capped to a selectable
Resolution Target (4K/2K/1080p). Skips images already near the target
(skip-cutoff). Resilient long runs: a file cache in `scans/` resumes a stopped
batch; corrupt/missing files are detected, logged and skipped; a second pass
re-scans for files that appeared mid-run. Live thumbnail wall, two-row status,
progress bar, ETA. Pause/resume/stop (stop finishes the current image first).
Works with mapped network drives.

**Tag & Rename** — analyses each image with a local Ollama vision model, writes
a description into EXIF, and renames to `OriginalName_Condensed_Description.ext`.
**Auto-straighten** (on by default) uses a small CNN to rotate sideways photos
upright before tagging; only confident calls act, ambiguous/upside-down images
are left alone and logged. Selectable description language, force-tag /
force-rename, and **one-click Undo** (every change is recorded before anything is
modified). Already-tagged files are skipped on re-runs.

**Conciliation** (experimental, 0.2.1) — replaces original photos with their
processed (upscaled, optionally tagged & renamed) counterparts. Two phases:
**Scan/Preview** builds a per-folder plan (replaced / no-match / non-image-kept
counts, and lists the kept non-image files by path) and touches nothing; **Run**
then either archives originals into an `__Archive__` subfolder or deletes them
(delete needs an extra confirmation), moving the processed files into the
original tree. Matching prefers the **content-hash lineage** (0.2.1) recorded as
files are produced, so a source still matches its processed counterpart after
folders are moved or renamed; it falls back to mirrored-name matching for files
with no recorded lineage. Originals without a processed counterpart, and
non-image files, are never touched.

**Settings** — Ollama URL (with reachability check) and model picklist;
auto-straighten toggle/threshold; Resolution Target and skip-cutoff; SeedVR
pipeline options; Discord webhook (with Test); default folders per tool.

**Notifications** — Discord webhook on queue completion and on errors.

## Codebase structure

Top-level Python (the actual app — note line counts give a sense of weight):

| File | Role |
|------|------|
| `toolbox_gui.py` (~2.5k lines) | The tkinter GUI. `App` (window) hosts four tabs: `UpscaleTab`, `TagTab`, `ConciliateTab`, `SettingsTab`. Launches the tools as **subprocesses** and talks to them over stdin/stdout (`ToolTab.launch`). Also: `LogPane`/`LogViewer`, `FilmStrip` (thumbnail wall), Discord webhook test. `APP_VERSION` lives here. |
| `batch_upscale.py` (~1.5k lines) | Upscale batch runner (CLI + GUI-driven). Walks the source tree, mirrors it to the output root via `os.path.relpath`, drives `UpscaleEngine`, manages the resume cache in `scans/`, and sends Discord notifications. |
| `upscale_engine.py` (~250 lines) | `UpscaleEngine` — wraps the in-process SeedVR2 pipeline (`seedvr2/inference_cli.py`). Loads DiT/VAE once and caches them; loads images with EXIF orientation; writes output atomically (temp + rename), format per extension. **GPU work happens wherever this runs.** |
| `tag_and_rename.py` (~1.7k lines) | Tag & Rename runner. Calls Ollama, writes EXIF, renames, records an undo cache; integrates auto-straighten. Has its own Discord + cache-schema versioning. |
| `conciliate.py` (~430 lines) | Conciliation runner (CLI + GUI-driven). Two phases over stdin (`run`/`q`): scan builds the original→processed plan (matching by content-hash lineage first — path-independent, survives folder moves — then falling back to mirrored-name matching), then run archives/deletes originals and moves processed files into the original tree. No GPU/heavy imports — pure file I/O. |
| `db.py` (~400 lines) | Shared SQLite cache layer (`db/cache.db`, WAL). Tables: `upscale_roots`/`upscale_files` (eligibility cache); `tag_roots`/`tag_files` (tag & rename cache, full entry as JSON plus indexed columns); `lineage` (content-hash links source→upscaled→tagged, so conciliation can re-match files after a folder move/rename — see `docs/content-hash-lineage.md`); `file_hashes` (memoised blake2b hashes by path+mtime+size, shared by all tools). `get_conn()` opens once per process; on first creation it imports the legacy `scans/*.json` and `trcache/*.cache` files whose source folder still exists (stale ones skipped). Logs are deliberately NOT in the DB. |
| `orientation.py` (~170 lines) | Auto-straighten: a small pretrained CNN (`ternaus/check_orientation`) detects sideways photos and losslessly rotates them upright; fails safe (leaves ambiguous/upside-down alone). Heavy imports are lazy. |

Configuration & state:

- `config.json` — persistent settings: `seedvr2`, `ollama`, `upscale`,
  `tagging`, `defaults` sections. Edited via the Settings tab; preserved across
  installer upgrades. **Don't hand-edit in normal flow.**
- `gui_settings.json` — GUI-only state (window geometry, thumbnail size).
- `db/cache.db` — the single SQLite cache (eligibility + tag/rename), see
  `db.py`. **This replaces the old per-folder JSON caches.** The legacy `scans/`
  (upscale) and `trcache/` (tag/rename) folders are now read only once, on first
  DB creation, for the one-time import; they are otherwise vestigial.
- `logs/` — run logs (kept as text files, not in the DB).
  `test_output/`, `samples/` — sample images.

Engine, packaging & CI:

- `seedvr2/` — the SeedVR2 upscaling engine, cloned from
  `numz/ComfyUI-SeedVR2_VideoUpscaler` and used **directly in-process** (ComfyUI
  itself is not needed). Treat as vendored/third-party.
- `.venv/` — the Python 3.12 environment (PyTorch CUDA + seedvr2 requirements).
- `bootstrap.ps1` — first-launch bootstrapper: downloads Python, PyTorch CUDA,
  the SeedVR2 engine. Idempotent. `Image Toolbox.cmd` launches it + the app.
- `installer/ImageToolbox.iss` — Inno Setup script; ships only the scripts +
  bootstrap (heavy components download on first launch). Built by
  `.github/workflows/build-installer.yml` on `v*` tags → GitHub Releases.
- `tools/git-clean-webhook.py` — repo maintenance helper.
- `docs/` — `future-features.md` (roadmap + feasibility), `runpod-notes.md`
  (notes for future remote-pod upscaling).

## Architecture notes for changes

- **The subprocess + stdin/stdout seam** between the GUI and the batch scripts is
  the clean integration point — new front-ends (web, Home Assistant) should
  reuse the scripts rather than re-implement the pipeline.
- **Keep it dependency-light.** The GUI deliberately uses only the standard
  library. Adding packages (e.g. a web framework, MQTT) is a deliberate
  architectural choice, not a default.
- **Stay Windows-aware.** Paths, the PowerShell bootstrap, and
  `CREATE_NO_WINDOW` are Windows-specific; cross-platform work (Linux/Unraid)
  requires porting those layers, though the PyTorch/SeedVR2/Ollama core is
  already cross-platform.
- **Run from source:** `.venv\Scripts\pythonw.exe toolbox_gui.py`, or
  double-click `Image Toolbox.cmd`. Headless: `python batch_upscale.py <src>
  [out]` and `python tag_and_rename.py <folder>`.

## Context

Written largely with AI assistance ("vibecoding") by a non-professional
developer; a personal project shared at no cost. Match the existing code's style,
comment density, and the "fail safe / never touch originals" philosophy when
making changes.
