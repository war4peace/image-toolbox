# Future Features

Candidate features for the toolbox, **sorted by implementation difficulty
(easiest first)**, with a feasibility assessment for each. See the bottom for
the cross-feature dependencies that should drive sequencing.

All six are feasible. They split into two tiers: contained, low-risk additions
(1–3) and larger milestones that introduce new process models, networking, or
packaging (4–6).

---

## 1. Image tree conciliation ("destructive" functionality) — Easy–Medium
When the user is satisfied with the upscaling results, the application moves
upscaled images into the source folder tree and either places the originals in
an archive folder of choice, or removes them.

- **Why it's the easiest:** the output already mirrors the source tree via
  `os.path.relpath` (`batch_upscale.py`, the `os.walk` / `output_dir` logic), so
  conciliation is just: walk the output tree, map each file back to its source
  location, move it in, then archive-or-delete the original.
- **Reuse:** the relpath mapping in `batch_upscale.py`; the undo-cache pattern
  from `tag_and_rename.py`; `shutil` from the standard library.
- **Work needed:** a new module + a small GUI panel (or a button on the Upscale
  tab); archive-folder picker; collision handling; an undo log.
- **Risks:** it is *destructive* — the app's whole pitch is "your originals are
  never touched" — so it needs strong confirmation, a dry-run preview, and undo.
  Edge case: the upscaler can change the extension (PNG→JPG in
  `upscale_engine._save_image`), so name-matching must be tolerant.

## 2. In-app auto-update functionality — Medium
The application checks for updates, displays patch notes, prompts to update, and
self-updates.

> **Status: implemented (0.2.3).** See `updater.py` and `UpdateDialog` /
> the Settings "Updates" section in `toolbox_gui.py`. Checks the GitHub Releases
> API on startup (opt-out) and on demand; downloads `ImageToolboxSetup.exe`,
> launches it and quits so Inno Setup replaces the app in place. "Skip this
> version" is remembered in `config.json` (`updates` section).

- **What's in place:** `APP_VERSION` in `toolbox_gui.py`; distribution is the
  GitHub-Releases installer built by CI on `v*` tags; `urllib` is already used.
- **Work needed:** query the GitHub Releases API for the latest tag, compare to
  `APP_VERSION`, show the release body as patch notes (a tkinter dialog),
  download `ImageToolboxSetup.exe`, launch it and exit.
- **Why not "easy":** Windows self-update means the installer replaces files of a
  running app — solved by the standard pattern (launch installer, quit
  immediately; Inno overwrites the scripts, user relaunches). Must also handle
  the unsigned-installer SmartScreen flow, network/checksum failures, and
  "skip this version."
- **Risks:** low technical risk; mostly UX polish. No GPU/Linux concerns.

## 3. Home Assistant integration — Medium
Statistics, cache file state, application status.

- **What helps:** the data is all readable — app status (is a subprocess
  running), cache/scan state (`scans/`), processed counts.
- **The hard part is transport:** there is no long-running server today.
  Easiest path is MQTT discovery (publish sensors HA auto-discovers) or pushing
  to HA's REST API, driven by a small background publisher thread in the GUI.
- **Work needed:** an MQTT dependency (the app is currently proudly
  dependency-light) or hand-rolled REST pushes; a config section; a publisher
  that samples state.
- **Coupling:** if the HTTP interface (#5) is built first, HA can simply scrape
  it and this collapses to near-trivial.
- **Risks:** introduces a networking dependency and a persistent background loop
  into a currently simple app.

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

- **#1 and #2 are independent quick wins** — they can ship anytime, need no new
  dependencies, and don't change the process model.
- **#6 depends on #5** (headless needs a web UI), and **#3 is dramatically
  cheaper if #5 lands first.** If the web direction is on the roadmap, sequence
  **#5 → #3 → #6** to avoid duplicated work.
- **Architectural watch-item:** the app is currently dependency-light and
  Windows-only. #3, #5, and #6 each push it toward extra packages, a
  long-running server, and cross-platform support — adopt those deliberately.
