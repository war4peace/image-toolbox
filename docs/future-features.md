# Future Features

Candidate features for the toolbox, **sorted by implementation difficulty
(easiest first)**, with a feasibility assessment for each. See the bottom for
the cross-feature dependencies that should drive sequencing.

The remaining candidates split into two tiers: one contained, lower-risk
addition (#1) and three larger milestones that introduce new process models,
networking, or packaging (#2–#4). Two earlier tier-1 items shipped in 0.2.8 —
the `scripts/` move and the "Report an issue" link — and have been removed from
the list.

---

## 1. Comparison tab (original vs. upscaled) — Medium
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

## 2. Remote upscaling (RunPod) — Hard
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

## 3. HTTP interface — Hard
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

## 4. Unraid Community Apps integration — Hardest
The user installs and runs the application on their Unraid server.

> **Status: deferred.** The app stays Windows-only for now — there is no Linux
> GPU server to target. Revisit only if that changes.

- **Why it's the hardest:** this is a distribution/packaging effort on top of a
  Linux port — not a discrete code feature. The app is Windows-bound (tkinter
  GUI, PowerShell `bootstrap.ps1`, `%USERPROFILE%`/`.venv\Scripts` paths,
  `CREATE_NO_WINDOW`). Unraid runs headless Docker on Linux.
- **Requires:** the HTTP interface (#3) for any UI; a Linux build of the
  pipeline; the NVIDIA Container Toolkit for GPU passthrough; a Dockerfile
  replacing `bootstrap.ps1`; and a Community Apps template XML.
- **What helps:** the heavy lifting (PyTorch/CUDA, SeedVR2, Ollama-over-URL) is
  already cross-platform; only the shell/GUI/packaging layers are Windows-only.

---

## Sequencing & dependencies

- **Already shipped (0.2.0–0.2.8):** image-tree conciliation, in-app auto-update,
  Home Assistant (MQTT), the system-telemetry sampler, crash logging,
  auto-straighten-before-upscaling, the `scripts/` reorganisation, and the
  "Report an issue" feedback link. Those former roadmap items have been removed
  from the list above.
- **#1 (comparison tab) is independent** and the only remaining tier-1 item. With
  the `scripts/` move already landed, new files (e.g. a `ComparisonTab`) arrive in
  the final structure and aren't moved twice.
- **#4 depends on #3** (headless Unraid needs a web UI). With Home Assistant
  already done over MQTT, the old telemetry coupling no longer drives sequencing;
  #2, #3 and #4 remain the large, mostly independent milestones.
- **Architectural watch-item:** the app is dependency-light and Windows-only. #1
  leans on `Pillow` in the GUI layer; #2, #3 and #4 each push toward extra
  packages, a long-running server, and cross-platform support — adopt those
  deliberately.
