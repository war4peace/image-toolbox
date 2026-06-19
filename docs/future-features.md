# Future Features

Candidate features for the toolbox, **sorted by implementation difficulty
(easiest first)**, with a feasibility assessment for each. See the bottom for
the cross-feature dependencies that should drive sequencing.

What remains are three larger milestones that each introduce new process models,
networking, or packaging. The earlier, lower-risk additions have all shipped (the
`scripts/` reorganisation, the "Report an issue" link, and the original-vs-
upscaled comparison view) and have been removed from the list.

---

## 1. Remote upscaling (RunPod) — Hard
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

## 2. HTTP interface — Hard
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

## 3. Unraid Community Apps integration — Hardest
The user installs and runs the application on their Unraid server.

> **Status: deferred.** The app stays Windows-only for now — there is no Linux
> GPU server to target. Revisit only if that changes.

- **Why it's the hardest:** this is a distribution/packaging effort on top of a
  Linux port — not a discrete code feature. The app is Windows-bound (tkinter
  GUI, PowerShell `bootstrap.ps1`, `%USERPROFILE%`/`.venv\Scripts` paths,
  `CREATE_NO_WINDOW`). Unraid runs headless Docker on Linux.
- **Requires:** the HTTP interface (#2) for any UI; a Linux build of the
  pipeline; the NVIDIA Container Toolkit for GPU passthrough; a Dockerfile
  replacing `bootstrap.ps1`; and a Community Apps template XML.
- **What helps:** the heavy lifting (PyTorch/CUDA, SeedVR2, Ollama-over-URL) is
  already cross-platform; only the shell/GUI/packaging layers are Windows-only.

---

## Sequencing & dependencies

- **Already shipped (0.2.0–0.2.9):** image-tree conciliation, in-app auto-update,
  Home Assistant (MQTT), the system-telemetry sampler, crash logging,
  auto-straighten-before-upscaling, the `scripts/` reorganisation, the "Report an
  issue" link, and the original-vs-upscaled comparison view (a floating
  before/after wipe window with shared zoom/pan, plus green/red outcome frames in
  the film-strip). Those former roadmap items have been removed from the list.
- **#1, #2 and #3 are large, mostly independent milestones.** With Home Assistant
  already done over MQTT, the old telemetry coupling no longer drives sequencing.
- **#3 depends on #2** (headless Unraid needs a web UI).
- **Architectural watch-item:** the app is dependency-light and Windows-only. #1,
  #2 and #3 each push toward extra packages, a long-running server, and
  cross-platform support — adopt those deliberately.
