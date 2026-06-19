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
re-scans for files that appeared mid-run. Live thumbnail wall (the current image
has a blue frame; each finished thumbnail is framed **green** when an upscaled
counterpart exists — i.e. it can be compared — or **red** on failure, via `RESULT`
events, 0.2.9), two-row status, progress bar, ETA. Pause/resume/stop (stop
finishes the current image first).
Works with mapped network drives. **Auto-straighten before upscaling** (0.2.7, on
by default) rotates sideways photos upright *first*, so the result respects the
4K-fit target (3840 wide OR 2160 tall) in its final orientation — without it, a
sideways photo is upscaled on the wrong axis and stops fitting once Tag & Rename
straightens it. The source is never touched: a temp copy is rotated, upscaled,
then deleted. Uses the same CNN/threshold as Tag & Rename (`orientation.py`); the
eligibility/skip check is orientation-aware. Toggle + threshold in Settings →
Upscaling.

**Tag & Rename** — analyses each image with a local Ollama vision model, writes
a description into EXIF, and renames to `OriginalName_Condensed_Description.ext`.
**Auto-straighten** (on by default) uses a small CNN to rotate sideways photos
upright before tagging; only confident calls act, ambiguous/upside-down images
are left alone and logged. Selectable description language, force-tag /
force-rename, and **one-click Undo** (every change is recorded before anything is
modified). Already-tagged files are skipped on re-runs.

**Comparison** (0.2.9) — a floating, resizable **original-vs-upscaled** window
(like the log window: one shared instance, geometry persisted as
`compare_geometry`). On the Batch Upscaler tab, **double-clicking a green
(comparable) thumbnail** opens it. Both images are drawn aligned on one canvas
split by a vertical **before/after wipe** (left = original, right = upscaled);
zoom (mouse wheel, centred on the pointer, fit … 400% of the upscaled native
pixels) and pan (drag, clamped to keep the view filled) are **shared**, so the
two halves always show the same region — making the quality gain directly
visible. Drag the divider handle to slide the wipe. Only the visible slice of
each side is decoded (Pillow `resize` with a float `box`); gestures render with a
fast filter and a crisp LANCZOS pass follows when they settle. Pairing is
**current-run**: the upscaler's `RESULT` event carries the output path, which the
strip remembers (`FilmStrip._compare`). Double-clicking a red/unframed thumbnail
(or any Tag & Rename thumbnail) just opens the file. See `ComparisonWindow`.

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
pipeline options; Discord webhook (with Test); default folders per tool. Settings
take effect only on **Save**; an **unsaved-changes guard** compares the form
against `config.json` and shows a Save / Don't save / Cancel prompt when leaving
the Settings tab or closing the app with pending edits (`SettingsTab.is_dirty` /
`_collect` / `revert`). Picklists ignore the mouse wheel (0.2.8): `App`
rebinds the `TCombobox`/`TSpinbox` `<MouseWheel>` class bindings
(`_install_picklist_wheel_guard` / `_picklist_wheel`) so scrolling over a
combobox/spinbox no longer silently cycles its value (which used to flip a
setting unnoticed and trip the guard); the wheel is forwarded to the nearest
scrollable canvas so the page still scrolls.

**Notifications** — Discord webhook on queue completion and on errors.

**Home Assistant (MQTT)** (0.2.4) — optional, opt-in integration that publishes
app state to an MQTT broker for Home Assistant / MQTT Explorer. No separate
enable toggle: MQTT activates whenever a broker **host** is configured in
Settings (clear the host to disable). A persistent client keeps the connection
up for the app's lifetime, verifies connectivity on startup, auto-reconnects,
and sets an availability **LWT** (`image-toolbox/availability` → online/offline)
so HA always knows if the app is alive. Retained topics: `version`, `update`,
`latest_version`, `last_run` (JSON summary), `last_used`, plus live `task/*`
state (`name` = idle/upscaling/tagging/conciliating, `details`, `runtime`,
`progress` = X/Y, `eta`, `average_processing_time`, `last_processing_time`).
Settings has host/port/username/password/client-id fields, a "Test" button, and
a "Publish now" button. Depends on `paho-mqtt` (installed by `bootstrap.ps1`);
the import is lazy so older venvs still launch. See `mqtt_publisher.py`.

**System telemetry** (Feature #3a) — a compact, read-only status row below the
image carousel on each tool tab showing **CPU usage, RAM, GPU VRAM and GPU
temperature**, also published to MQTT as retained `image-toolbox/system/*`
topics (`cpu`, `ram`, `ram_total`, `gpu_vram`, `gpu_vram_total`, `gpu_temp`). The
percentage readouts are colour-banded by load (blue ≤25 % · green ≤65 % · dark
yellow ≤85 % · red >85 %). Dependency-light: CPU via Windows `GetSystemTimes` and
RAM via `GlobalMemoryStatusEx` (both `ctypes`, no psutil), GPU from `nvidia-smi`
(no pynvml). Cadence: during upscaling, 5 s after each image starts
(past the load/ramp, avoiding the dip between images); every 30 s during Tag &
Rename and Conciliation; and every 60 s while **idle** (so the user can watch
VRAM free up before starting a run). The idle sampler steps aside whenever a task
is running. Samples run off the UI thread (a lock prevents overlapping
`nvidia-smi` calls). See `system_telemetry.py`.

**Updates** (0.2.3) — in-app update check against the GitHub Releases API.
Checks on startup (opt-out) and on demand from Settings; when a newer release
exists it shows the patch notes and can download `ImageToolboxSetup.exe`, launch
it and quit so Inno Setup replaces the app in place. "Skip this version" is
remembered. Pure stdlib (`urllib`); see `updater.py`.

**Crash logging** (0.2.5) — the GUI runs under `pythonw.exe` (no console), so an
unhandled exception used to kill the app with only a split-second flash and no
trail. `crash_logger.install()` is armed at the very top of `toolbox_gui.py`
(before the feature imports, so even an import-time failure — a module the
installer forgot to ship — is caught) and hooks `sys.excepthook`,
`threading.excepthook`, and `tkinter.Tk.report_callback_exception`. On a crash it
writes `logs/crash_<timestamp>.log` (app/Python/platform header + full traceback)
and shows a native ctypes message box pointing at the file, so the crash is
visible even when tkinter itself broke. The three subprocess runners
(`batch_upscale.py`, `tag_and_rename.py`, `conciliate.py`) also arm it with
`install(notify=False)` — they write the same crash log but skip the dialog,
since their traceback already reaches the GUI log pane via stderr. Stdlib only.
(An Event Viewer entry was considered but dropped — writing the Application log
needs an elevated, registered source and the app runs non-elevated.) See
`crash_logger.py`.

## Codebase structure

The app's Python modules live in **`scripts/`** (0.2.8 — previously the repo
root). Data, config, the `.venv` and the vendored `seedvr2/` engine stay at the
**app root**; each module resolves root-relative resources through an `APP_ROOT`
= parent-of-`scripts/` (paths anchored off `__file__`, never the cwd). Line
counts give a sense of weight:

| File (`scripts/`) | Role |
|------|------|
| `toolbox_gui.py` (~2.6k lines) | The tkinter GUI. `App` (window) hosts four tabs: `UpscaleTab`, `TagTab`, `ConciliateTab`, `SettingsTab`. Launches the tools as **subprocesses** (siblings in `scripts/`, run with cwd at the app root) and talks to them over stdin/stdout (`ToolTab.launch`). Also: `LogPane`/`LogViewer`, `FilmStrip` (thumbnail wall with green/red outcome frames), `ComparisonWindow` (floating original-vs-upscaled wipe view with shared zoom/pan, 0.2.9), Discord webhook test, the bottom-bar **"Report an issue"** link (`report_issue`/`_issue_url`, 0.2.8). `APP_VERSION` lives here. |
| `batch_upscale.py` (~1.5k lines) | Upscale batch runner (CLI + GUI-driven). Walks the source tree, mirrors it to the output root via `os.path.relpath`, drives `UpscaleEngine`, manages the resume cache in `scans/`, and sends Discord notifications. Auto-straightens (0.2.7) before upscaling: `detect_rotation` runs the `orientation.py` CNN, `_make_straightened_copy` rotates a temp copy upright (source untouched), and the skip/target math uses the upright dimensions (`_skip_for_dims`; `should_skip_resolution` is conservative — only skips when both orientations would). |
| `upscale_engine.py` (~250 lines) | `UpscaleEngine` — wraps the in-process SeedVR2 pipeline (`seedvr2/inference_cli.py`). Loads DiT/VAE once and caches them; loads images with EXIF orientation; writes output atomically (temp + rename), format per extension. **GPU work happens wherever this runs.** |
| `tag_and_rename.py` (~1.7k lines) | Tag & Rename runner. Calls Ollama, writes EXIF, renames, records an undo cache; integrates auto-straighten. Has its own Discord + cache-schema versioning. |
| `conciliate.py` (~430 lines) | Conciliation runner (CLI + GUI-driven). Two phases over stdin (`run`/`q`): scan builds the original→processed plan (matching by content-hash lineage first — path-independent, survives folder moves — then falling back to mirrored-name matching), then run archives/deletes originals and moves processed files into the original tree. No GPU/heavy imports — pure file I/O. |
| `db.py` (~400 lines) | Shared SQLite cache layer (`db/cache.db`, WAL). Tables: `upscale_roots`/`upscale_files` (eligibility cache); `tag_roots`/`tag_files` (tag & rename cache, full entry as JSON plus indexed columns); `lineage` (content-hash links source→upscaled→tagged, so conciliation can re-match files after a folder move/rename — see `docs/content-hash-lineage.md`); `file_hashes` (memoised blake2b hashes by path+mtime+size, shared by all tools). `get_conn()` opens once per process; on first creation it imports the legacy `scans/*.json` and `trcache/*.cache` files whose source folder still exists (stale ones skipped). Logs are deliberately NOT in the DB. |
| `orientation.py` (~170 lines) | Auto-straighten: a small pretrained CNN (`ternaus/check_orientation`) detects sideways photos and losslessly rotates them upright; fails safe (leaves ambiguous/upside-down alone). Heavy imports are lazy. |
| `updater.py` (~170 lines) | In-app updater. Queries the GitHub Releases API for the latest tag, compares it to `APP_VERSION`, and downloads/launches `ImageToolboxSetup.exe`. Pure stdlib (`urllib`), network calls meant for a background thread; the GUI (`UpdateDialog`, Settings "Updates" section) owns the UI. |
| `system_telemetry.py` (~180 lines) | System telemetry sampler (Feature #3a). Stdlib-only, read-only, best-effort: `CpuSampler` reads CPU usage from Windows `GetSystemTimes` (`ctypes`) as a delta between calls; `sample_ram()` reads physical RAM via `GlobalMemoryStatusEx`; `sample_gpu()` shells out to `nvidia-smi` for VRAM used/total and temperature. All fail safe to `None`. The GPU query blocks (spawns a process), so the GUI samples from a background thread. |
| `mqtt_publisher.py` (~290 lines) | Optional Home Assistant (MQTT) integration. One-shot helpers (`test_connection`, `publish_state`, `publish_version`) for the Settings "Test"/"Publish now" buttons and the startup snapshot, plus a persistent `MqttClient` that holds the connection for the app's lifetime, sets the availability LWT, replays retained topics on reconnect, and publishes live `task/*` state. Lazy `paho-mqtt` import; network calls run on background threads (the GUI owns the UI/config). |
| `crash_logger.py` (~180 lines) | Last-resort crash diagnostics (0.2.5). `install()` (armed at the top of `toolbox_gui.py`, before the feature imports) hooks `sys.excepthook`, `threading.excepthook` and `tkinter.Tk.report_callback_exception`; on an unhandled crash it writes `logs/crash_<timestamp>.log` (header + full traceback) and pops a native ctypes message box so the crash is visible under `pythonw`. Stdlib only, fail-safe, re-entrancy-guarded. |

Configuration & state:

- `config.json` — persistent settings: `seedvr2`, `ollama`, `upscale`,
  `tagging`, `defaults`, `mqtt`, `updates` sections. Edited via the Settings tab;
  preserved across installer upgrades. **Don't hand-edit in normal flow**, and
  the tracked copy is a credential-free template — never commit real `mqtt`
  broker credentials.
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
  the SeedVR2 engine, and `paho-mqtt`. Idempotent. `Image Toolbox.cmd` launches
  it + the app (it launches `scripts\toolbox_gui.py`). The final "starting"
  window auto-closes on a 10-second countdown (press any key to close early).
- `installer/ImageToolbox.iss` — Inno Setup script; ships only the scripts +
  bootstrap (heavy components download on first launch). It packages every app
  module via a `..\scripts\*.py` glob into `{app}\scripts` (not a hand-maintained
  list — a missing entry broke 0.2.5). 0.2.8's `[InstallDelete]` removes the
  stale root-level `.py` from pre-0.2.8 installs so old/new copies can't coexist. Built by `.github/workflows/build-installer.yml` on `v*`
  tags → GitHub Releases. **Release notes are the annotated tag message:** write
  clean, user-facing notes in `git tag -a vX -m "…"`; CI strips trailers/PGP and
  publishes them as the release body (no auto-generated compare link). The in-app
  update dialog shows that body, further cleaned by `updater.clean_notes()`. So
  when cutting a release, the tag `-m` message IS what users read.
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
- **Run from source:** `.venv\Scripts\pythonw.exe scripts\toolbox_gui.py`, or
  double-click `Image Toolbox.cmd`. Headless: `python scripts\batch_upscale.py
  <src> [out]` and `python scripts\tag_and_rename.py <folder>`.

## Context

Written largely with AI assistance ("vibecoding") by a non-professional
developer; a personal project shared at no cost. Match the existing code's style,
comment density, and the "fail safe / never touch originals" philosophy when
making changes.
