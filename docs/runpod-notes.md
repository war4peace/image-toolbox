# RunPod remote-pod notes (for a future feature)

Reference notes distilled from the old `remote-image-upscale.ps1` /
`remote-tag-and-rename.ps1` scripts (removed — see git history before commit
`baf6f8b` for the full originals). Those scripts targeted the **pre-0.1.0
ComfyUI architecture** and do not run against the current app; this file keeps
the parts that are still worth reusing when remote-pod support is rebuilt.

## Architecture: what changed, and what still maps

The old model: run the tool **locally**, SSH-tunnel into a service on the pod,
let the pod's GPU do the work over the tunnel.

- **Upscaling — must be redesigned.** The old script ran `batch_upscale.py`
  locally against **ComfyUI on the pod** (HTTP, port 8188). The current upscaler
  loads SeedVR2 **in-process**, so the GPU work happens wherever the script runs.
- **Tagging — pattern still works.** Tagging talks to Ollama over a URL. The old
  script tunnelled to **Ollama on the pod** (port 11434) and ran
  `tag_and_rename.py` locally against it. The same thing works today with **no
  code change**: start Ollama on the pod, open `ssh -L 11434:localhost:11434`,
  and set **Settings → Ollama URL** to `http://127.0.0.1:11434`.

Prefer building any new version **inside the app** (GUI/Settings, Python,
cross-platform) rather than as standalone PowerShell.

## Decided design (0.3.1-experimental)

Settled when groundwork started — drives every phase below:

- **Create-on-demand, disposable pods.** The app creates a fresh pod via
  `POST /pods`, runs, then **terminates** it. No manually-managed long-lived pod.
  This is what makes the watchdog teardown (below) cheap: a degraded pod is
  thrown away, not nursed.
- **Streaming, one image at a time — *not* a bulk transfer.** Production sets are
  tens of thousands of files / many GB, so neither "upload everything first" nor
  a network volume fits. Instead the **queue, resume-cache, skip logic,
  film-strip and watchdog all stay local**; for each image the local orchestrator
  uploads one source copy → the pod upscales it → downloads the one result →
  next. A dropped connection loses one in-flight image, not a multi-GB transfer,
  and results land locally immediately.
- **The pod is a resident *worker*, not a batch runner.** SeedVR2's ~16 GB model
  load can't be per-image, so the pod runs a small long-lived process that loads
  DiT/VAE **once** and serves single images (over an `ssh -L` tunnel). This is a
  thin, single-purpose service — *not* the full HTTP UI mirror of roadmap #2.
- **`RemoteUpscaleEngine`, same interface as `UpscaleEngine`.** Remote vs local
  is a config switch in `batch_upscale.py`; the batch loop barely changes. The
  watchdog keeps working unchanged — it now times the full remote round-trip per
  output-MP, which also catches network stalls. **"Never touch source" is trivially
  kept** (only a copy is ever uploaded).
- **Watchdog = pod health signal.** On `DEGRADED`, tear the pod down and
  re-provision a fresh one; the local resume-cache continues the queue. (Locally
  the same signal just auto-stops for a reboot.)
- **Dead-man's switch is the organising constraint.** A billed pod must never be
  left running. The app's after-run auto-stop is the *fast* path; a self-stop
  deadline **enforced on the pod** (max-runtime + idle-timeout, via the
  pre-installed `runpodctl`) is the *guaranteed* path that survives a dropped
  connection or a crashed controller. Build the stop path first.

## Heavyweight models live on a persistent network volume

Disposable pods must **not** re-download the models on every launch — that burns
GPU-hour time on a pure download. The fix (decided 2026-06-20):

- **One persistent RunPod network volume holds the heavyweight data** — SeedVR2
  weights (~16 GB) and the Ollama vision model (~6 GB) — written **once** and
  mounted on every disposable pod. The on-pod worker loads SeedVR2 from the
  mounted path; Ollama points its model dir there too.
- This is **not** a reversal of the "no network volume for images" decision: that
  was about the *image data* (tens of thousands of files, streamed one-by-one).
  This volume is write-once / read-every-pod — the opposite access pattern.
- **Cost is trivial:** RunPod charges **$0.07/GB/mo** (under 1 TB), so ~22–25 GB
  ≈ **~$1.6/month** — negligible against even one hour of GPU time, and it removes
  the ~22 GB cold-download from every pod start.
- **Caveat — network volumes are region-locked.** The volume is pinned to one
  data center, so every pod that mounts it **must** be created in that same data
  center. `dataCenterIds` on `create_pod` is therefore *derived from the volume's
  region*, not a free choice. `runpod.network_volume_id` is in `config.json`
  (Phase 2 wires create_pod + a one-time "provision the model volume" flow:
  create volume → spin a pod with it mounted → download models → done).

## Install modes & a first-run wizard (bootstrap rework — late phase)

Once remote upscaling works end-to-end, the **bootstrap/installer should branch
on how the user intends to run**, so someone without a capable GPU isn't forced
through the ~20 GB local download. A first-run wizard records the mode in config:

- **Local** (current default): full `bootstrap.ps1` — Python, **PyTorch CUDA
  (~3 GB)**, **SeedVR2 engine + weights (~16 GB)**, Pillow, paho-mqtt. Needs an
  NVIDIA GPU (8 GB+).
- **Remote:** lightweight — Python, Pillow, paho-mqtt, the RunPod control plane;
  OpenSSH ships with Windows. **Skips torch CUDA and SeedVR2 weights** (GPU work
  is on the pod). A ~20 GB install becomes tiny; no GPU required.
- **Both:** full local bootstrap + the remote control plane available.

Wrinkles to honour when this is built:

- **Auto-straighten needs torch.** `orientation.py`'s CNN runs *locally* today,
  before upscaling. Remote-only has no local torch, so the straighten step must
  **move onto the pod worker** (straighten → upscale in one round-trip), for both
  Batch Upscale and Tag & Rename. So "remote-only" is only viable once the worker
  straightens — which is why the wizard lands *after* Phases 2–3, not now.
- **`upscale_engine` import must stay lazy/conditional.** `batch_upscale.py` must
  not import the SeedVR2 engine (torch) when running in remote mode, or a
  torch-less install can't launch. Select Remote vs Local engine before importing.
- **Tagging in remote mode** reuses the existing pattern: tunnel to **Ollama on
  the pod** (`ssh -L 11434:localhost:11434`, set Ollama URL to localhost) — no
  code change, and bootstrap still never installs Ollama itself.
- **Pillow and the comparison view stay in every mode** — the GUI needs Pillow,
  and the before/after wipe compares the local original against the locally
  downloaded result, so it works unchanged for remote runs.

### Status

- **Phase 0 (done):** control plane + config + Settings. `scripts/runpod_client.py`
  wraps the REST calls (stdlib `urllib`); `config.json` has a `runpod` section;
  Settings → *Remote upscaling (RunPod)* holds the API key (with a **Test**
  button), hourly rate, and the dead-man's-switch limits. Nothing is provisioned
  yet — Test only lists pods (free).
- **Phase 1 (in progress):** the on-pod dead-man's switch — `pod/deadman.py` is
  done (self-stops on max-runtime/idle via `runpodctl stop pod $RUNPOD_POD_ID`,
  pure tested `evaluate()`; the user's API key never lives on the pod). Remaining:
  the local after-run auto-stop with a cancel countdown (wired in with the run
  flow, Phase 3). The on-pod half is fully exercisable only once Phase 2
  provisions a real pod, but its decision logic is verified off-pod (`--selftest`).
- **Phase 2a (done):** region-aware provisioning groundwork. `runpod_client` has
  network-volume CRUD (`/networkvolumes`, verified live) + curated `GPU_TYPES` and
  EU-only `EU_DATACENTERS` enums (the REST API has no list endpoint). Settings now
  has a GPU-type picklist, an **EU data-center picklist (defaults to EU-RO-1)**, and
  a model-volume row (Refresh lists, Create makes one in the chosen DC with a cost
  confirmation). No pod spun up.
- **Phase 2b:** `create_pod` → wait RUNNING → SSH probe → bring up the worker, with
  the model volume mounted (data center derived from the volume's region).
- **Phase 3:** `RemoteUpscaleEngine` streaming; `DEGRADED` teardown/re-provision;
  cost embed.

## RunPod REST API

- Base URL: `https://rest.runpod.io/v1`  ·  OpenAPI: `…/v1/openapi.json`
- Auth header: `Authorization: Bearer <api_key>`
- Endpoints **verified against the live OpenAPI spec (2026-06)** and wrapped in
  `scripts/runpod_client.py`:

  | Method | Path | Use |
  |---|---|---|
  | `GET`    | `/pods`             | list (filter by `desiredStatus`, etc.) |
  | `GET`    | `/pods/{id}`        | status — `desiredStatus` ∈ `RUNNING`/`EXITED`/`TERMINATED` |
  | `POST`   | `/pods`             | create: `gpuTypeIds`, `imageName`/`templateId`, `networkVolumeId`, `containerDiskInGb`, `ports`, `env`, `dataCenterIds` |
  | `POST`   | `/pods/{id}/start`  | start / resume |
  | `POST`   | `/pods/{id}/stop`   | stop (storage still billed while stopped) |
  | `DELETE` | `/pods/{id}`        | terminate (frees all billing) |

- The exact `gpuTypeIds` string (e.g. for a 5090) should be read from the GPU
  types endpoint at provision time rather than hard-coded — the config default is
  a best guess to be confirmed in Phase 2.

## `runpod` config section (shipped in 0.3.1)

The block now in `config.json` (`api_key` blank in the tracked template — a
credential, same rule as the `mqtt` block):

```jsonc
"runpod": {
    "api_key": "",                  // RunPod REST key; local only, never committed
    "gpu_type_id": "NVIDIA GeForce RTX 5090",
    "image_name": "",               // or template_id; set in Phase 2
    "template_id": "",
    "data_center_ids": [],
    "container_disk_gb": 30,
    "ssh_key_path": "%USERPROFILE%\\.ssh\\id_ed25519_runpod",
    "worker_port": 8200,            // on-pod upscale worker, tunnelled via ssh -L
    "hourly_rate": 0.90,            // USD/h, for cost estimates
    "stop_pod_when_done": true,
    "terminate_when_done": false,   // true = disposable: delete, don't just stop
    "max_runtime_minutes": 720,     // dead-man's switch, enforced on the pod
    "idle_timeout_minutes": 15
}
```

## SSH connectivity + tunnel

```powershell
# Connectivity check (also confirms the GPU)
ssh -i <key> -p <port> -o StrictHostKeyChecking=no root@<host> `
    "echo connected && nvidia-smi --query-gpu=name,memory.total --format=csv,noheader"

# Tunnel a pod service to localhost (Ollama shown; ComfyUI was 8188)
ssh -i <key> -p <port> -o StrictHostKeyChecking=no -o ServerAliveInterval=30 `
    -L 11434:localhost:11434 -N root@<host>
```

## Cost tracking + auto-stop safety

The most valuable bit: never leave a billed pod idle.

- Track `sessionElapsed = end - start`; `cost = round(hours * hourly_rate, 2)`.
- After the run, **stop the pod automatically** with a short cancel countdown
  (old default: 60 s, cancel on Escape). Always close the SSH tunnel regardless.
- If the API stop call fails, tell the user to stop it manually and show the
  pod ID — don't fail silently.

## Discord completion embed

Send on completion (green `0x2ecc71` / `3066993`), with fields:
duration, estimated cost, hourly rate, processed count, average time per image,
pod ID, completed-at timestamp. The app's Python `send_discord_notification`
already covers the general case; remote runs would just add the cost/pod fields.
