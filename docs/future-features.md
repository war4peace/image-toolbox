# Future Features

Candidate features for the toolbox, **sorted by implementation difficulty
(easiest first)**, with a feasibility assessment for each. See the bottom for
the cross-feature dependencies that should drive sequencing.

All six are feasible. They split into two tiers: contained, lower-risk additions
(1–3) and larger milestones that introduce new process models, networking, or
packaging (4–6). Within tier 1 the numbering reflects grouping rather than strict
difficulty (#3 is the smallest).

---

## 1. Move the Python scripts into a `scripts/` subfolder — Easy–Medium
The repository root currently holds ~11 top-level `.py` files mixed in with
config, docs and the vendored engine. Move them all into `scripts/` and update
every reference so the root is just entry points, data and `seedvr2/`.

- **Why do it:** a cleaner, self-explanatory root (the app's own code vs.
  config/state/engine), and one obvious home for the modules.
- **Reuse:** the modules already import each other as flat siblings, so once they
  all live together in `scripts/` those imports keep working unchanged — the
  entry script's own directory is on `sys.path`.
- **Work needed:** the real effort is *path anchoring*. Many modules compute
  locations from their own `__file__` (e.g. `SCRIPT_DIR` in `batch_upscale.py` /
  `toolbox_gui.py`), and `config.json`, `gui_settings.json`, `logs/`, `scans/`,
  `trcache/`, `db/`, `seedvr2/` and the model weights all live at the *app root*,
  not in `scripts/`. Introduce a single `APP_ROOT` (the parent of `scripts/`) and
  route every data/config/engine path through it. Then update the launcher
  (`Image Toolbox.cmd` → `scripts\toolbox_gui.py`), `bootstrap.ps1`, the GUI's
  subprocess launches (`ToolTab.launch`, which builds the child script paths),
  and the installer (`installer/ImageToolbox.iss`: `..\*.py` → `..\scripts\*.py`
  with the matching `DestDir`, plus the shortcut target).
- **Risks:** path regressions are easy to miss because each module resolves paths
  independently — the 0.2.5 breakage was exactly a packaging/path mismatch.
  In-place upgrades also need an `[InstallDelete]` rule to remove the now-stale
  root-level `.py` files, or old and new copies coexist. Test a clean install
  *and* an upgrade-over-0.2.x.

## 2. Comparison tab (original vs. upscaled) — Medium
A new tab that shows a source image beside its upscaled result so the user can
judge the quality gain — ideally with synchronized zoom/pan and a before/after
wipe slider.

- **Why it's feasible:** the source→upscaled pairing already exists. The
  `lineage` table (`db.py`, content-hash links) maps an original to its upscaled
  output independently of path, with the mirrored-tree `relpath` mapping as a
  fallback — so "find the counterpart" is a lookup, not new bookkeeping.
- **Reuse:** the tab / `FilmStrip` patterns in `toolbox_gui.py`; the lineage
  lookup; `Pillow` (already in the venv) for loading and resizing.
- **Work needed:** a `ComparisonTab` with a pair picker (choose an original;
  auto-resolve its upscaled counterpart via lineage), an image viewer with synced
  zoom/pan, and a split before/after slider. Handle the resolution mismatch by
  rendering both at the same on-screen size (upscaled at native detail, original
  scaled up by the viewer) so the difference is visible.
- **Risks:** displaying 4K images in tkinter needs care — downscale to the
  viewport and re-render on zoom to stay responsive and bounded in memory. It
  also brings `Pillow`/`ImageTk` into the GUI layer (which has stayed stdlib-only
  so far, though Pillow is already installed for the engine) — a small, deliberate
  dependency call. Synced pan/zoom on a tkinter `Canvas` is fiddly but
  well-trodden.

## 3. "Report an issue" feedback link — Easy
A Feedback button/link in the lower-right of the main window (to the right of the
telemetry row) that opens
`https://github.com/war4peace/image-toolbox/issues/new` in the browser.

- **Why it's easy:** `webbrowser.open(...)` is already used (Discord / releases
  links), so this is one small widget plus a URL.
- **Make it useful, cheaply:** pre-fill the issue via query params
  (`?title=…&body=…`) with `APP_VERSION` and basic environment (OS, GPU name from
  `system_telemetry.sample_gpu`), and prompt the user to attach the newest
  `logs/crash_*.log` — turning the crash logging added in 0.2.6 into actionable
  reports. A repo issue template (`?template=`) would standardize this further.
- **Reuse:** `webbrowser`, `APP_VERSION`, `system_telemetry`, and the crash-log
  convention from `crash_logger.py`.
- **Work needed:** place the link in a small bottom-right status area beside the
  telemetry row; build the pre-filled URL (keep the body short — URLs have length
  limits, and `body` must be URL-encoded); open it.
- **Risks:** negligible. Only watch the pre-filled body length and encoding.

## 4. Remote upscaling (RunPod) — Hard
Spin up a runpod.io pod, point the application to the pod, install requirements
on the remote pod, use it to upscale images, and shut it down when finished.
See `docs/runpod-notes.md` for distilled notes from the old scripts.

- **Why it's hard:** the upscaler now loads SeedVR2 **in-process**, so the GPU
  work happens wherever the script runs. The old "tunnel to a service on the
  pod" model no longer applies — the flow must be **inverted**: ship the work to
  the pod and fetch results back.
- **Work needed:** RunPod REST calls to **create/start** a pod (the notes only
  cover *stop*); SSH connectivity + key management on Windows; transfer of many
  GB of images up (SCP/rsync, or attach a network volume); remote provisioning
  (install torch ~3 GB + weights ~16 GB on the pod); run `batch_upscale.py`
  remotely; stream progress back over SSH; fetch results; **cost tracking and
  guaranteed auto-stop**.
- **Risks:** the most failure-prone — network drops mid-transfer, partial
  uploads, billed pods left running if auto-stop fails, SSH on Windows, remote
  bootstrap drift. Should be its own milestone.

## 5. HTTP interface — Hard
Spin up a small HTTP server with a UI that mirrors the application UI.

- **What "mirror" implies:** rebuilding the thumbnail wall, two-row live status,
  progress/ETA, pause/resume/stop, and Settings as a web app — plus a backend
  and live updates (WebSocket/SSE).
- **Reuse:** the subprocess + stdin/stdout protocol is a clean backend seam; a
  server can drive the same scripts the GUI does.
- **Work needed:** an HTTP server (stdlib `http.server` is too thin for this —
  realistically a small framework), a streaming channel for live
  progress/thumbnails, and a full second UI to maintain alongside the tkinter
  one.
- **Risks:** large, ongoing surface area (two UIs to keep in sync); auth/binding
  concerns if exposed beyond localhost.
- **Scope note:** a minimal "status + start/stop" web panel is far cheaper than
  a true mirror and worth considering first.

## 6. Unraid Community Apps integration — Hardest
The user installs and runs the application on their Unraid server.

> **Status: deferred.** The app stays Windows-only for now — there is no Linux
> GPU server to target. Revisit only if that changes.

- **Why it's the hardest:** this is a distribution/packaging effort on top of a
  Linux port — not a discrete code feature. The app is Windows-bound (tkinter
  GUI, PowerShell `bootstrap.ps1`, `%USERPROFILE%`/`.venv\Scripts` paths,
  `CREATE_NO_WINDOW`). Unraid runs headless Docker on Linux.
- **Requires:** the HTTP interface (#5) for any UI; a Linux build of the
  pipeline; the NVIDIA Container Toolkit for GPU passthrough; a Dockerfile
  replacing `bootstrap.ps1`; and a Community Apps template XML.
- **What helps:** the heavy lifting (PyTorch/CUDA, SeedVR2, Ollama-over-URL) is
  already cross-platform; only the shell/GUI/packaging layers are Windows-only.

---

## Sequencing & dependencies

- **Already shipped (0.2.0–0.2.7):** image-tree conciliation, in-app auto-update,
  Home Assistant (MQTT), the system-telemetry sampler, crash logging, and
  auto-straighten-before-upscaling. Those former roadmap items have been removed
  from the list above.
- **Tier 1 (#1–#3) are independent**, but do **#1 (scripts → `scripts/`) first**:
  it re-anchors paths and touches the installer, so landing it before the
  comparison tab (#2) means new files arrive in the final structure and aren't
  moved twice. #3 (feedback link) is the smallest and can slot in any time.
- **#6 depends on #5** (headless Unraid needs a web UI). With Home Assistant
  already done over MQTT, the old **#5 → telemetry** coupling no longer drives
  sequencing; #4, #5 and #6 remain the large, mostly independent milestones.
- **Architectural watch-item:** the app is dependency-light and Windows-only. #2
  leans on `Pillow` in the GUI layer; #4, #5 and #6 each push toward extra
  packages, a long-running server, and cross-platform support — adopt those
  deliberately.
