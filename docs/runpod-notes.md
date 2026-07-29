# RunPod remote-pod notes

Reference notes for the **shipped** remote-pod feature (remote Image Upscaler,
Tag & Rename, and Video Upscaler: future-features #1/#2, live since 0.3.1). This
is the design-of-record and the hard-won RunPod API / provisioning knowledge that
backs the code; `CLAUDE.md` has the user-facing feature summary. (Originally
distilled from the pre-0.1.0 ComfyUI-era `remote-*.ps1` scripts, long since
removed; see git history before `baf6f8b`.)

> **Engine scope:** the pod path documented in THIS file is **SeedVR2**, on the model
> volume. The 0.5.6 Real-ESRGAN fixed-ratio engine (`docs/local-video-upscaler.md` section
> 11) runs both locally and remotely, but its remote half deliberately shares almost none of
> this file's machinery: a lightweight `--mode esrgan` worker on a cheap **no-volume** pod
> that self-downloads the ~65 MB hash-pinned weight, under its own pod name
> (`esrgan-toolbox-remote`) so `_find_existing_pod` can never adopt a volume SeedVR2 pod. It
> and the queue change it rides on (**per-item GPU binding + grouped multi-pod Start**) are
> documented in `docs/video-upscaler.md` section 18.

## Contents

- [Performance findings](#performance-findings-measured-on-the-pod)
- [Architecture: what changed, and what still maps](#architecture-what-changed-and-what-still-maps)
- [Decided design (0.3.1-experimental)](#decided-design-031-experimental)
- [Heavyweight models live on a persistent network volume](#heavyweight-models-live-on-a-persistent-network-volume)
- [Install modes (as built, 0.3.2)](#install-modes-as-built-032)
  - [Provisioning (as built)](#provisioning-as-built)
- [RunPod REST API](#runpod-rest-api)
- [Live GPU availability + pricing (GraphQL, 0.3.3)](#live-gpu-availability--pricing-graphql-033)
- [`runpod` config section](#runpod-config-section)
- [SSH connectivity + tunnel](#ssh-connectivity--tunnel)
- [Running multiple runs in parallel](#running-multiple-runs-in-parallel-verified-2026-07-07)
- [Cost tracking + auto-stop safety](#cost-tracking--auto-stop-safety)
- [Completion notification](#completion-notification)

---

## Performance findings (measured on the pod)

- **Warm upscale throughput.** Measured via the resident worker (`bench`): cold
  image #1 ≈ 41 s, then **warm ≈ 7.6 s/image at 1080** (1620×1080) and **≈ 13.4
  s/image at 4K-class** (3240×2160). The pod 5090 at ~13.4 s/4K beats the local
  3090 (17–19 s) and is close to a local 5090 (~10 s). The 78 s in early smoke
  tests was one-time warmup (CUDA/cuDNN + first-run Blackwell kernel JIT), paid
  once by the resident worker and amortised over the queue.
- **Tag throughput (cheap card).** The vision model uses only ~6.6 GB VRAM, so
  tagging runs on a cheap card (RTX 2000 Ada, 16 GB, ~$0.24/h): session up ~32 s,
  cold inference 24.4 s (model load), **warm ~2.6 s/image**, ~3.5–4× cheaper/hour
  than the RTX 5090 for near-equivalent tag throughput.
- **Cold engine load ≈ 97 s** (once the volume holds the validation cache; a miss
  used to re-hash the 16 GB DiT for ~354 s; closed in 0.4.3 item 11, see
  `pod/worker.py` `_seed_validation_cache`). The remaining ~97 s is the unavoidable
  single 16 GB volume→VRAM read; copy-to-local-NVMe was investigated and rejected
  (a resident worker loads once per pod, so the extra copy isn't amortised).
- **SageAttention: not worth the pip version.** PyPI `sageattention` is the old
  v1.0.6 (INT8); with `attention_mode=sageattn_2` it gave only 12.9 s vs 13.4 s at
  4K (~4%). The real speedups need SageAttention 2 (FP8) / 3 (Blackwell FP4),
  neither on PyPI; source builds from thu-ml/SageAttention (nvcc, ~10 min, some
  risk). Deferred: the pod 5090 is already ~30% faster than the user's 3090
  without it. (v1.0.6 is in the volume venv; the worker accepts an `attention_mode`
  override.)

<div align="right"><a href="#runpod-remote-pod-notes">↑ Back to top</a></div>

## Architecture: what changed, and what still maps

The old model: run the tool **locally**, SSH-tunnel into a service on the pod,
let the pod's GPU do the work over the tunnel.

- **Upscaling: must be redesigned.** The old script ran `batch_upscale.py`
  locally against **ComfyUI on the pod** (HTTP, port 8188). The current upscaler
  loads SeedVR2 **in-process**, so the GPU work happens wherever the script runs.
- **Tagging: pattern still works.** Tagging talks to Ollama over a URL. The old
  script tunnelled to **Ollama on the pod** (port 11434) and ran
  `tag_and_rename.py` locally against it. The same thing works today with **no
  code change**: start Ollama on the pod, open `ssh -L 11434:localhost:11434`,
  and set **Settings → Ollama URL** to `http://127.0.0.1:11434`.

Prefer building any new version **inside the app** (GUI/Settings, Python,
cross-platform) rather than as standalone PowerShell.

<div align="right"><a href="#runpod-remote-pod-notes">↑ Back to top</a></div>

## Decided design (0.3.1-experimental)

Settled when groundwork started; drives every phase below:

- **Create-on-demand, disposable pods.** The app creates a fresh pod via
  `POST /pods`, runs, then **terminates** it. No manually-managed long-lived pod.
  This is what makes the watchdog teardown (below) cheap: a degraded pod is
  thrown away, not nursed.
- **Streaming, one image at a time, *not* a bulk transfer.** Production sets are
  tens of thousands of files / many GB, so neither "upload everything first" nor
  a network volume fits. Instead the **queue, resume-cache, skip logic,
  film-strip and watchdog all stay local**; for each image the local orchestrator
  uploads one source copy → the pod upscales it → downloads the one result →
  next. A dropped connection loses one in-flight image, not a multi-GB transfer,
  and results land locally immediately.
- **The pod is a resident *worker*, not a batch runner.** SeedVR2's ~16 GB model
  load can't be per-image, so the pod runs a small long-lived process that loads
  DiT/VAE **once** and serves single images (over an `ssh -L` tunnel). This is a
  thin, single-purpose service, *not* the full HTTP UI mirror of roadmap #2.
- **`RemoteUpscaleEngine`, same interface as `UpscaleEngine`.** Remote vs local
  is a config switch in `batch_upscale.py`; the batch loop barely changes. The
  watchdog keeps working unchanged: it now times the full remote round-trip per
  output-MP, which also catches network stalls. **"Never touch source" is trivially
  kept** (only a copy is ever uploaded).
- **Watchdog = pod health signal.** On `DEGRADED`, tear the pod down and
  re-provision a fresh one; the local resume-cache continues the queue. (Locally
  the same signal just auto-stops for a reboot.)
- **Dead-man's switch is the organising constraint.** A billed pod must never be
  left running. The app's after-run auto-stop is the *fast* path; a self-stop
  deadline **enforced on the pod** (max-runtime + idle-timeout) is the
  *guaranteed* path that survives a dropped connection or a crashed controller.
  Build the stop path first.
- **0 disables a limit (0.3.2).** `deadman.evaluate` treats `max_runtime=0` (and
  `idle=0`) as "no limit", so a long overnight run can set **max-runtime 0** and
  rely on the **idle timeout** as the safety net (the worker touches a heartbeat
  per image, so idle never fires during active work). The GUI warns if BOTH are 0
  (no auto-stop at all → a crash leaves the pod billing).
- **A dead-man's-switch stop ends the run gracefully (0.3.2).** When the pod stops
  mid-run, the next image's request fails with a connection error. The runners
  used to treat that as a recoverable local *outage* and pause forever (the
  GUI can't resume a dead pod). Now, on a failure, `batch_upscale` /
  `tag_and_rename` check the pod's status (`_remote_pod_stopped` → `pod_status`);
  if it is gone/EXITED/TERMINATED they **end the run cleanly** (save the resume
  cache, amber notification, skip the rescan) instead of pausing: a re-run
  continues the queue. A still-RUNNING pod keeps the normal outage path.

<div align="right"><a href="#runpod-remote-pod-notes">↑ Back to top</a></div>

## Heavyweight models live on a persistent network volume

Disposable pods must **not** re-download the models on every launch: that burns
GPU-hour time on a pure download. The fix (decided 2026-06-20):

- **One persistent RunPod network volume holds the heavyweight data**, SeedVR2
  weights (~16 GB) and the Ollama vision model (~6 GB), written **once** and
  mounted on every disposable pod. The on-pod worker loads SeedVR2 from the
  mounted path; Ollama points its model dir there too.
- This is **not** a reversal of the "no network volume for images" decision: that
  was about the *image data* (tens of thousands of files, streamed one-by-one).
  This volume is write-once / read-every-pod, the opposite access pattern.
- **Cost is trivial:** RunPod charges **$0.07/GB/mo** (under 1 TB), so ~22–25 GB
  ≈ **~$1.6/month**, negligible against even one hour of GPU time, and it removes
  the ~22 GB cold-download from every pod start.
- **Caveat: network volumes are region-locked.** The volume is pinned to one
  data center, so every pod that mounts it **must** be created in that same data
  center. `dataCenterIds` on `create_pod` is therefore *derived from the volume's
  region*, not a free choice. `runpod.network_volume_id` is in `config.json`; the
  one-time "provision the model volume" flow (Settings → Remote) creates the volume,
  spins a pod with it mounted, downloads the models, and terminates.

<div align="right"><a href="#runpod-remote-pod-notes">↑ Back to top</a></div>

## Install modes (as built, 0.3.2)

The installer branches on how the user intends to run, so someone without a
capable GPU isn't forced through the ~20 GB local download. The mode is written to
`install_mode.txt` and read by `bootstrap.ps1`:

- **Local** (default): full `bootstrap.ps1`, i.e. Python, PyTorch CUDA (~3 GB), SeedVR2
  engine + weights (~16 GB), Pillow, paho-mqtt. Needs an NVIDIA GPU (8 GB+).
- **Remote:** lightweight, i.e. Python, Pillow, paho-mqtt, the RunPod control plane;
  OpenSSH ships with Windows. **Skips torch CUDA and SeedVR2 weights** (GPU work is
  on the pod). No GPU required.
- **Both:** full local bootstrap + the remote control plane.

Design points that made Remote-only viable:

- **Auto-straighten runs on the pod for remote runs.** `orientation.py`'s CNN needs
  torch, which a Remote-only install doesn't have locally, so the worker serves
  `/orient` and straighten happens on the pod (for both Upscale and Tag & Rename).
- **The SeedVR2 engine import stays lazy/conditional.** `batch_upscale.py` selects
  the remote vs local engine *before* importing, so a torch-less install launches.
- **Remote Tag & Rename** (`IMGTBX_TAG_REMOTE=1`,
  `tag_and_rename._setup_remote_tagging`): a `RemoteSession(mode="tag")` starts the
  worker in tag mode (skips SeedVR2, serves `/orient` only, leaving the VRAM for
  Ollama), starts `ollama serve` on the pod (models + the `ollama` runtime cached on
  the volume by `provision.sh`), opens a second `ssh -L` tunnel to 11434, and
  repoints `OLLAMA_URL` at it. Bootstrap never installs Ollama locally.
- **Tagging uses a cheap GPU tier.** The vision model needs only ~6.6 GB VRAM
  (measured), so tagging runs on a cheap card (RTX 2000 Ada, ~$0.24/h, warm
  ~2.6 s/image), NOT the upscale GPU. The card is the user's live picker choice
  (`runpod.tag_gpu_type_id` is the default); note there is **no** automatic GPU
  substitution as of 0.4.0: a sold-out pick fails cleanly rather than swapping.
- **Pillow + the comparison view stay in every mode** (the GUI needs Pillow, and
  the before/after wipe compares the local original against the downloaded result).

### Provisioning (as built)

- **Everything heavy lives on the network volume** (mounted at `/workspace`): the
  Python venv (torch CUDA + seedvr2 requirements), the SeedVR2 weights, and the
  Ollama models. A one-time provisioning (`pod/provision.sh`, driven by
  `scripts/runpod_provision.py`) builds them once (≈ 24 GB: 16 GB weights + 5.6 GB
  Ollama + 2.6 GB venv + engine); every disposable pod just mounts the volume and
  starts fast: no ~20 GB reinstall/redownload per pod.
- **Models auto-populate.** `UpscaleEngine.__init__` calls
  `src.utils.downloads.download_weight(..., model_dir=...)`, which downloads the
  weights to `model_dir` if missing. Point `model_dir` at a path on the volume and
  the **first run fills the volume**; later pods find them present. Same idea for
  Ollama via `OLLAMA_MODELS` on the volume, so there is **no separate model
  uploader**.
- **The worker reuses `UpscaleEngine` unchanged.** `pod/worker.py` is a thin
  resident wrapper: load the engine once (models from the volume), serve one image
  per request over localhost (reached via `ssh -L`), touch the `deadman.py`
  heartbeat. `scripts/remote_run.RemoteSession` orchestrates create → push → start
  worker → arm dead-man's switch → stream → teardown.
- **Connectivity.** Pod is created with `ports: ["22/tcp"]`; only port 22 is
  public (`publicIp:portMappings["22"]`). The worker binds localhost and is reached
  through an `ssh -L` tunnel, never exposed publicly.
- **Region.** The pod's `dataCenterIds` is derived from the volume's region
  (`volume_region`), keeping pod and volume co-located (network volumes are
  region-locked).
- **Deploy watchdog.** `runpod_client.create_pod_resilient` gives each pod a
  deploy budget (240 s); on timeout or early EXIT it terminates the bad pod and
  tries a fresh one (RunPod sometimes hands out pods that never finish deploying),
  up to N attempts.

**Hard-won provisioning lessons (baked into `provision.sh` / the driver):**

- **Base image:** `runpod/pytorch:1.0.7-cu1281-torch291-ubuntu2204` (the plausible
  `2.8.0-...-cuda12.8.1-...` tag does NOT exist on the registry). Ships Python
  3.12, torch 2.9.1+cu128, torchaudio 2.9.1, but **no torchvision**.
- **Torch pinning is essential.** Installing the seedvr2 deps naively makes
  `timm`/`torchvision` drag in a newer torch (2.12 + CUDA 13) that mismatches the
  image's torchaudio 2.9.1 → `libtorchaudio.so: undefined symbol` at import. Fix: a
  venv with `--system-site-packages` + a constraints file pinning `torch==2.9.1` +
  matching `torchvision==0.24.1` from the cu128 index (torch/tv/ta all 2.9.1+cu128).
- **Volume mounts at `/workspace`.** Models auto-download there via the engine's
  `download_weight` (skips if present, so later pods reuse them). Cold engine load
  is ~97 s (see Performance findings; the one-time hash-validation is cached on the
  volume, item 11).

**SSH & launch gotchas (solved):**

- A pod **self-terminates via the REST API with the key ON the pod**: REST-created
  pods have NO pre-authed `runpodctl` and no `$RUNPOD_POD_ID`, so key-on-pod is
  unavoidable (written to a 0600 file; use a SCOPED key in prod).
- A backgrounded daemon must be launched as `setsid sh -c '…' </dev/null >log 2>&1
  &` with the redirect **directly on the backgrounded command**: a `cd && …`
  wrapper (or a missing redirect) keeps the ssh channel open and the call hangs.

<div align="right"><a href="#runpod-remote-pod-notes">↑ Back to top</a></div>

## RunPod REST API

- Base URL: `https://rest.runpod.io/v1`  ·  OpenAPI: `…/v1/openapi.json`
- Auth header: `Authorization: Bearer <api_key>`
- Endpoints **verified against the live OpenAPI spec (2026-06)** and wrapped in
  `scripts/runpod_client.py`:

  | Method | Path | Use |
  |---|---|---|
  | `GET`    | `/pods`             | list (filter by `desiredStatus`, etc.) |
  | `GET`    | `/pods/{id}`        | status: `desiredStatus` ∈ `RUNNING`/`EXITED`/`TERMINATED` |
  | `POST`   | `/pods`             | create: `gpuTypeIds`, `imageName`/`templateId`, `networkVolumeId`, `containerDiskInGb`, `ports`, `env`, `dataCenterIds` |
  | `POST`   | `/pods/{id}/start`  | start / resume |
  | `POST`   | `/pods/{id}/stop`   | stop (storage still billed while stopped) |
  | `DELETE` | `/pods/{id}`        | terminate (frees all billing) |

- The exact `gpuTypeIds` string (e.g. `"NVIDIA GeForce RTX 5090"`) comes from the
  live GPU list, not a hard-coded default: see the GraphQL section below (the REST
  create enum is a subset of what's actually deployable, so pods deploy via GraphQL).

<div align="right"><a href="#runpod-remote-pod-notes">↑ Back to top</a></div>

## Live GPU availability + pricing (GraphQL, 0.3.3)

The REST control plane exposes **no** GPU-types/pricing/stock endpoint: that's
why the picklists (`GPU_TYPES`, `TAG_GPU_TYPES`) were hand-curated, and why they
went stale: on 2026-06-22 **all four** curated tag cards (RTX 2000 Ada / A4000 /
A4500 / 4000 Ada) were out of stock in EU-RO-1, so a non-technical user couldn't
tag at all and the app gave no hint why.

The **GraphQL** endpoint has the data the REST one lacks:

- `POST https://api.runpod.io/graphql` · `Authorization: Bearer <key>` ·
  **must send a browser `User-Agent`** (Cloudflare 403s an unknown one).
- Query `gpuTypes { id displayName memoryInGb
  lowestPrice(input: {gpuCount: 1, secureCloud: true, dataCenterId: $dc}) {
  uninterruptablePrice stockStatus } }`.
- `id` is exactly the `gpuTypeIds` value `create_pod` wants
  (`"NVIDIA GeForce RTX 5090"`); `displayName` is the short name.
- `lowestPrice.uninterruptablePrice` = on-demand $/h in that DC;
  `stockStatus` ∈ High/Medium/Low when deployable, **null = out of stock there**.

**Gotcha: GraphQL catalog ≠ REST create enum → deploy via GraphQL.** The GraphQL
`gpuTypes` list is a *superset* of the GPU ids the REST `POST /pods` endpoint
accepts. Newer cards (seen 2026-06-22: `NVIDIA RTX PRO 4500 Blackwell`,
`NVIDIA RTX PRO 4000 Blackwell`) appear in GraphQL with live stock + price but
REST `create_pod` **rejects them with HTTP 400** ("value must be one of …").

First attempt was to intersect availability with the REST enum (`CREATABLE_GPU_IDS`,
dumped via `POST /pods` `gpuTypeIds=["__invalid__"]`). But that *hides* cheap,
available cards the website happily deploys, so instead **pod creation moved to
the GraphQL deploy path** (`runpod_client.deploy_pod` → `podFindAndDeployOnDemand`,
the same mutation the RunPod console uses). It accepts the full catalog, so the
picker shows everything in stock (incl. RTX 2000 Ada ~$0.24) and `available_gpus`
no longer filters. `CREATABLE_GPU_IDS` stays only as documentation.

**Two GraphQL-deploy gotchas that cost a debugging round (both verified live):**

1. **`volumeMountPath` is mandatory with a network volume.** REST defaults it to
   `/workspace`; GraphQL does not, and omitting it makes the container fail at
   create with `invalid mount config for type "bind": field Target must not be
   empty`. The pod shows `desiredStatus: RUNNING` but the container never starts,
   so it **never gets a public IP / port-22 mapping** and SSH never comes up. Pass
   `volumeMountPath: "/workspace"`, `volumeInGb: 0`, `networkVolumeId`.
2. **`supportPublicIp: true` + `ports: "22/tcp"`** (a *string*, comma-separated,
   not the REST array) are needed for the direct-TCP SSH endpoint the app relies
   on (`publicIp` + `portMappings["22"]`, which appear ~90 s after RUNNING).

A failed-mount deploy can also leave an **auto-replacement pod** whose id the
mutation never returned: diag/teardown must sweep by **name**, not just the
returned id, or a replacement lingers and bills.

**Gotcha: CUDA driver mismatch → `allowedCudaVersions` deploy filter.** The pod
image (`runpod/pytorch:…cu1281…`) needs host driver CUDA **≥ 12.8**. RunPod
machines vary: benchmarking landed a CUDA-**12.7** RTX 4090 whose container never
started (`nvidia-container-cli: requirement error: unsatisfied condition:
cuda>=12.8`), and the doomed pod burned the whole fallback chain (2×240 s waits ×
each GPU). `deploy_pod` now passes `allowedCudaVersions` so the pod only lands on
a machine that can run the image. Verified-live facts:

- the field is **exact-match set membership**, not a `>=` range, so to mean
  "≥ 12.8" you must enumerate every version ≥ 12.8 (`runpod_client.allowed_cuda_versions`
  derives this from the image's `cuXYZ` tag against `KNOWN_CUDA_VERSIONS`, listing
  a couple of not-yet-existing future versions so a newer host isn't excluded).
- it turns a *random* failure (you might get a 12.7 host and crash) into reliable
  success (you only ever get a ≥12.8 host) or a clean, fast supply-constraint
  fallback. It can't conjure compatible machines where none exist for that GPU.

**Limit / future work:** this makes the cu128 image *reliable*, but doesn't let an
older-driver-only machine run it. To actually use cards whose only hosts are on
<12.8 drivers you'd need a **lower-CUDA image**, which for tagging is viable
(only Ollama + the orientation CNN, and orientation falls back to CPU via
`torch.cuda.is_available()`), but the on-pod venv is built with
`--system-site-packages` against the image's exact torch (2.9.1+cu128, see
provision.sh), so a different image needs the **volume re-provisioned** with a
matching/own torch. Deferred: the filter already makes the current image
reliable.

**No GPU-type substitution (0.4.0).** A run deploys **only the card the user
picked** in the live picker. Earlier versions seeded a price-ordered *automatic
fallback chain* from the selection (capped by a per-task price ceiling so a sold-out
cheap card couldn't silently escalate to, say, an A100-SXM4-80GB at $1.49/h). That
whole mechanism is **removed**: silent type-switching surprised the user during
benchmarking (a run quietly landed on a card they hadn't chosen). Now if the picked
card is sold out at deploy time the run **fails cleanly**, and the status line tells
the user to press the picker's ↻ to refresh stock and re-pick a different card
themselves. `_selected_gpu_chain` returns just `[picked_id]`; the deprecated
`runpod.max_price_per_hour` / `max_price_per_hour_upscale` / `max_price_per_hour_tag`
keys (and the Settings → Remote ceiling spinners + `_fallback_ceiling`) are gone and
dropped from config on the next Settings save.

Wrapped as `runpod_client.available_gpus(api_key, data_center_id, min_memory_gb)`
→ in-stock GPUs ≥ the VRAM floor, **sorted by price ascending**. The GUI's tab
pickers call it (off-thread) for the volume's region (≥32 GB upscale, ≥16 GB
tag), default to the persisted preference when in stock else the cheapest, and
pass the selection + a price-ordered fallback chain to `RemoteSession` via the
`IMGTBX_GPU_OVERRIDE` env. Availability fluctuates minute-to-minute, hence the ↻
refresh button. Surprise from the first run: RTX PRO 4500 (32 GB, $0.74) was
cheaper than the RTX 5090 ($0.99) for upscaling, exactly the "is a pricier pod
more cost-efficient per image?" question worth benchmarking.

**World-wide region + data-center picker (0.3.4).** The early Settings DC picker
was an **EU-only** curated enum (`EU_DATACENTERS`, defaulting to EU-RO-1), fine
for the original user (Romania) but wrong for anyone else. GraphQL also exposes
the full data-center catalog: `{ dataCenters { id location storageSupport listed } }`
(`runpod_client.data_centers`). Crucially it reports **`storageSupport`**: a
network (model) volume can only live in a DC that supports storage, so the picker
offers **storage-capable, listed DCs only**, preventing a user from provisioning a
volume somewhere no pod can then attach it. Settings now has a **Region**
combo (Europe / North America / Asia / Oceania, derived from the id prefix via
`runpod_client.region_of`) feeding a **Data center** combo, with a Refresh that
pulls the live list and a one-line "Volume actions act in: <region> · <dc>" target
readout. A curated `DATACENTERS` fallback (storage DCs as of 2026-06) makes it work
offline. Selecting an existing model volume **syncs the picker to that volume's
region** (it's region-locked, so that's where Create/Provision and pods run).
**Storage support is the hard filter**: a region or data center with no
network-volume storage is simply **never populated**: only regions with at least
one storage-capable DC appear, and the DC combo lists only storage DCs. So
Oceania (whose only DC, OC-AU-1, reports `storageSupport:false`, a compute-only
data center that shows GPUs on the RunPod website but can't host a network volume)
just doesn't show up. Simple, no confusing "exists but unusable" message.
**Volumes are scoped to the selected data center**: the Region/DC **Refresh also
re-lists model volumes**, showing only the one(s) in that DC or a
**`None | <data center>`** placeholder when there isn't one, so the volume shown
always matches where a pod would run. The volume combo is read-only (pick, don't
type); the chosen volume is persisted with its **full display label**
(`network_volume_label`, alongside the bare `network_volume_id` the run code uses)
so it reads in full on the next launch instead of showing only the raw id.
**The Refresh also fills the Upscale/Tag GPU preference combos** with the GPUs the
selected data center offers + live price (`available_gpus(..., include_out_of_stock=True)`,
a preference list so a momentarily sold-out card still shows), partitioned by the
VRAM floor (>=32 GB upscale, >=16 GB tag) and defaulting to RTX 5090 / RTX 2000 Ada
(or the current pick if still offered, else cheapest). The Upscale GPU, Tag GPU and
Model volume comboboxes share one aligned grid column, stacked below the
Region/Data center row with the volume action buttons beneath them.

<div align="right"><a href="#runpod-remote-pod-notes">↑ Back to top</a></div>

## `runpod` config section

The `runpod` block in `config.json` (the `api_key` is a secret and lives in the
untracked `config.local.json` overlay, item 9; the tracked template keeps it
blank):

```jsonc
"runpod": {
    "api_key": "",                  // secret; stored in config.local.json
    "gpu_type_id": "NVIDIA GeForce RTX 5090",  // upscale card (live-picker default)
    "tag_gpu_type_id": "",          // cheap tag card (live-picker default)
    "image_name": "",               // or template_id (blank = built-in default image)
    "template_id": "",
    "network_volume_id": "",        // region-locked model volume
    "network_volume_label": "",     // full display label, for the Settings readout
    "data_center_ids": [],          // derived from the volume's region
    "container_disk_gb": 30,
    "ssh_key_path": "",             // blank = the app's managed ed25519 key (ssh_setup)
    "worker_port": 8200,            // on-pod worker, tunnelled via ssh -L
    "hourly_rate": 0.90,            // USD/h fallback for cost estimates
    "stop_pod_when_done": true,
    "terminate_when_done": true,    // disposable: delete (not just stop) when done
    "max_runtime_minutes": 0,       // dead-man's switch hard ceiling; 0 = no limit
    "idle_timeout_minutes": 15      // the real switch: stops a billed pod on a drop
}
```

The deprecated `max_price_per_hour*` keys were removed in 0.4.0 (no automatic GPU
substitution) and are dropped from `config.json` on the next Settings save.

<div align="right"><a href="#runpod-remote-pod-notes">↑ Back to top</a></div>

## SSH connectivity + tunnel

```powershell
# Connectivity check (also confirms the GPU)
ssh -i <key> -p <port> -o StrictHostKeyChecking=no root@<host> `
    "echo connected && nvidia-smi --query-gpu=name,memory.total --format=csv,noheader"

# Tunnel a pod service to localhost (Ollama shown; ComfyUI was 8188)
ssh -i <key> -p <port> -o StrictHostKeyChecking=no -o ServerAliveInterval=30 `
    -L 11434:localhost:11434 -N root@<host>
```

<div align="right"><a href="#runpod-remote-pod-notes">↑ Back to top</a></div>

## Running multiple runs in parallel (verified 2026-07-07)

Two runs can be in flight at once. Three combinations, all confirmed by reading
the wiring:

- **Local Batch Upscaler (stills) + remote Video Upscaler.** No collision. The
  local upscale owns the 3090 in-process; the video GPU work is on a rented pod,
  so they never contend for silicon. The GUI does not gate these two tabs against
  each other (a local upscale only greys out **Tag & Rename** and **Conciliation**
  for GPU/folder reasons, `tab_upscale.py` `set_tag_tab_enabled` /
  `refresh_conciliate_lock`, never the Video tab).
- **Remote Batch Upscaler (images) + remote Video Upscaler.** Also works: the two
  run on **separate pods**. The pod name is mode-aware (image upscale + Tag &
  Rename share `image-toolbox-remote`; video gets `video-toolbox-remote`), and
  `_find_existing_pod` matches on the per-mode prefix, so neither run adopts or
  tears down the other's pod. Each run rents its own GPU, so no GPU contention.
- The **SSH tunnels don't collide.** The local end of every `ssh -L` tunnel comes
  from `_free_local_port()` (bind to port 0, OS-assigned):
  `remote_upscale_engine.py` / `remote_video_engine.py` for the worker tunnel, and
  `remote_run._open_ollama_tunnel` (also port 0) for the Ollama tunnel. The
  pod-side worker port (8200) and Ollama (11434) live on each run's **own** pod.
- The **shared `cache.db` is safe** across the two subprocesses: WAL mode +
  `timeout=30.0` (a cross-process writer waits, doesn't error), and the tools write
  mostly disjoint tables (`upscale_*` vs `video_*`); only `lineage` / `file_hashes`
  are shared, and those are sub-second, infrequent writes.

**Two caveats the app does NOT control (both remote):**

- **Concurrent mount of the shared model volume.** Both remote runs mount the same
  region-locked model network volume at `/workspace`. RunPod network volumes are
  network-attached and generally allow multiple pods in the same data center to
  mount one volume at once (the read-mostly model-sharing pattern they exist for),
  and the app neither prevents nor guarantees it. If a DC ever refused the second
  concurrent mount, the second pod's deploy fails cleanly at mount time (no
  corruption). One real race: the volume is read-only in steady state EXCEPT when a
  run writes a not-yet-cached asset (the video run auto-downloads `dit_model` on
  first use; remote Tag & Rename can pull an Ollama model). Pre-provision /
  pre-download the models so both runs only ever read, and this is a non-issue.
- **Double billing.** Two concurrent remote runs bill **two GPUs at once**, and the
  funds guard (`funds_guard.py`) polls the account balance **per session,
  independently**, so both can pass the start-floor check individually while
  together draining faster than either expects. Watch the combined burn rate.

**Cosmetic-only overlaps** (never affect file safety, the DB, or run completion):
the single **taskbar progress bar** (`App.taskbar_progress/state/clear`) and the
single MQTT **`task/*`** state slot are both driven by whichever run updates last,
so they flip-flop between the two runs and the first to finish clears/idles them
while the other is still going. The in-app per-tab progress bars stay correct.

<div align="right"><a href="#runpod-remote-pod-notes">↑ Back to top</a></div>

## Cost tracking + auto-stop safety

The most valuable bit: never leave a billed pod idle.

- Track `sessionElapsed = end - start`; `cost = round(hours * hourly_rate, 2)`.
- After the run, **stop the pod automatically** with a short cancel countdown
  (old default: 60 s, cancel on Escape). Always close the SSH tunnel regardless.
- If the API stop call fails, tell the user to stop it manually and show the
  pod ID, and don't fail silently.

<div align="right"><a href="#runpod-remote-pod-notes">↑ Back to top</a></div>

## Completion notification

Send on completion (green `0x2ecc71` / `3066993`), with fields:
duration, estimated cost, hourly rate, processed count, average time per image,
pod ID, completed-at timestamp. The runners call `send_notification(...)` →
`notifications.notify(...)` (0.3.8), which fans out to every configured backend
(Discord webhook + Telegram bot + ntfy); it already covers the general case, so remote
runs would just add the cost/pod fields. Telegram has no embed colours, so the
green status colour shows as a leading emoji there, and on ntfy as an emoji tag
plus priority.

<div align="right"><a href="#runpod-remote-pod-notes">↑ Back to top</a></div>
