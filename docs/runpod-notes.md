# RunPod remote-pod notes

Reference notes for the **shipped** remote-pod feature (remote Image Upscaler,
Tag & Rename, and Video Upscaler: future-features #1/#2, live since 0.3.1). This
is the design-of-record and the hard-won RunPod API / provisioning knowledge that
backs the code; `CLAUDE.md` has the user-facing feature summary. **The control
plane moved to RunPod's REST API v2 in 0.6.1** (roadmap #25); the last section of
this file is that migration's record, including the two deletions still on a date. (Originally
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
- [Live GPU availability + pricing](#live-gpu-availability--pricing)
- [`runpod` config section](#runpod-config-section)
- [SSH connectivity + tunnel](#ssh-connectivity--tunnel)
- [Running multiple runs in parallel](#running-multiple-runs-in-parallel-verified-2026-07-07)
- [Cost tracking + auto-stop safety](#cost-tracking--auto-stop-safety)
- [Completion notification](#completion-notification)
- [The API v2 migration (roadmap #25)](#the-api-v2-migration-061-roadmap-25)

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

**The app talks to `https://api.runpod.io/v2` by default since 0.6.1**, with REST v1 +
GraphQL retained as a manual escape hatch (`runpod.api_version`) until their shutdown dates.
Everything below is the as-built surface; the migration itself, what each transport spells
differently, and the two dated deletions are in
[The API v2 migration](#the-api-v2-migration-061-roadmap-25) at the end of this file.

- Base URL: `https://api.runpod.io/v2` (v1: `https://rest.runpod.io/v1`)
- OpenAPI: `https://api.runpod.io/v2/openapi.json` (34 paths as of 2026-08-20)
- Auth header: `Authorization: Bearer <api_key>`
- **Always send a `User-Agent`.** `api.runpod.io` sits behind Cloudflare, which answers
  urllib's default `Python-urllib/3.12` with **403 "error code: 1010"** and the app's own
  `ImageToolbox-RunPod` with 200. This bites the v2 REST paths, not just GraphQL, because
  they are the same host, which `rest.runpod.io` was not. A probe script written with bare
  urllib reads that 403 as a rejected API key and sends you chasing the wrong thing.

Wrapped in `scripts/runpod_client.py`, which is the only module that knows either spelling:

| Use | v2 (default) | v1 (escape hatch) |
|---|---|---|
| list pods | `GET /pods` | `GET /pods` |
| one pod | `GET /pods/{id}` | `GET /pods/{id}` |
| create | `POST /pods` (new body shape) | `POST /pods`, but see `deploy_pod` |
| start / stop / terminate | `POST /pods/{id}/action` | `POST /pods/{id}/start`, `/stop`, `DELETE /pods/{id}` |
| network volumes | `/network-volumes` | `/networkvolumes` |
| GPU + data-center catalog | `GET /catalog/gpus`, `/catalog/datacenters` | GraphQL |
| pod logs | `GET /pods/{id}/logs` (SSE) | *(none)* |
| recent spend | `GET /billing` | *(none)* |
| account balance | *(none: GraphQL only)* | GraphQL |

**A pod's state is `status` on v2 and `desiredStatus` on v1**, and that one rename is the
reason the whole read seam exists: see the migration section. Nothing outside
`runpod_client` reads a RunPod field by name, and
`tests/test_runpod_client.py::test_transport_specific_fields_are_read_only_inside_runpod_client`
sweeps every module to keep it that way.

- The exact `gpuTypeIds` string (e.g. `"NVIDIA GeForce RTX 5090"`) comes from the live GPU
  catalog, not a hard-coded default: see the next section.
- v2 validates the GPU id against the catalog itself, so the v1 create enum problem that
  `deploy_pod` exists to work around is gone on the default path (`deploy_pod` on v2 is a
  thin wrapper over `create_pod`).

<div align="right"><a href="#runpod-remote-pod-notes">↑ Back to top</a></div>

## Live GPU availability + pricing

The GPU list, its prices and its live stock come from the **v2 catalog** by default
(`GET /catalog/gpus`) and from **GraphQL** on the v1 escape hatch. `runpod_client`'s
`available_gpus(api_key, data_center_id, min_memory_gb)` is the one entry point either way:
in-stock GPUs at or above the VRAM floor, **sorted by price ascending**. The two were run
side by side against the live account on 2026-08-20 and agreed exactly (same cards, prices,
stock levels, VRAM and display names), so nothing above this call changed when the transport
did.

**Why a live catalog at all:** the REST control plane exposes no GPU-types/pricing/stock
endpoint, which is why the picklists (`GPU_TYPES`, `TAG_GPU_TYPES`) were hand-curated, and
why they went stale: on 2026-06-22 **all four** curated tag cards (RTX 2000 Ada / A4000 /
A4500 / 4000 Ada) were out of stock in EU-RO-1, so a non-technical user couldn't tag at all
and the app gave no hint why.

**The v2 catalog (default path).**

- `GET /v2/catalog/gpus?include=AVAILABILITY&product=POD&cloud=SECURE`.
- **All three query parameters are load-bearing and none default usefully.**
  `include=AVAILABILITY` is what makes it a catalog rather than a price list: without it
  there is no `availability`, no `dataCenters` and no `cudaVersions` (measured: the
  migration guide implies otherwise, and `GET /catalog/gpus/{id}` returns the same bare
  fields). `product=POD` is then **required**, deliberately, because a card can be scarce for
  pods and plentiful for serverless. `cloud` defaults to SECURE upstream but is sent
  explicitly, since every pod this app deploys is Secure Cloud and a silently changed default
  would quote community prices for capacity the app never uses.
- Per GPU: `id` (exactly the string a deploy wants), `name` (short display name), `memory`,
  `price.secure`, `availability`, a **`dataCenters[]`** array of per-DC availability, and
  **`cudaVersions[]`**, each version flagged `available` when at least one machine on it has
  free capacity.
- Stock levels are `NONE/LOW/MEDIUM/HIGH` against GraphQL's `Low/Medium/High`;
  `_stock_label` normalises the spelling in one place (the picker prints it straight into a
  combobox label) and maps `NONE` to None so the existing `if not stock` filters are
  unchanged.
- **A card with no stock anywhere carries an EMPTY `dataCenters` list** (measured), so an
  empty map means "nowhere", not "not asked".

**GraphQL (v1 escape hatch only).**

- `POST https://api.runpod.io/graphql` · `Authorization: Bearer <key>` · **must send a
  browser `User-Agent`** (Cloudflare 403s an unknown one).
- Query `gpuTypes { id displayName memoryInGb
  lowestPrice(input: {gpuCount: 1, secureCloud: true, dataCenterId: $dc}) {
  uninterruptablePrice stockStatus } }`.
- `lowestPrice.uninterruptablePrice` = on-demand $/h in that DC; `stockStatus` ∈
  High/Medium/Low when deployable, **null = out of stock there**. It answers for **one data
  center at a time**, which is why the per-DC features below are v2-only.

**Catalog prices are LIST prices.** The spec states negotiated account discounts are not
reflected, and GraphQL's `lowestPrice` behaves the same way. The pod's own `cost` is the
authoritative billed rate, so prefer it wherever a real number matters (estimates, the funds
cap) and treat the catalog as the shopping view.

### The v1 deploy path and why it exists (escape hatch only)

**GraphQL catalog ≠ REST v1 create enum → v1 deploys via GraphQL.** The GraphQL `gpuTypes`
list is a *superset* of the GPU ids the **v1** `POST /pods` endpoint accepts. Newer cards
(seen 2026-06-22: `NVIDIA RTX PRO 4500 Blackwell`, `NVIDIA RTX PRO 4000 Blackwell`) appear
with live stock and price, but v1 `create_pod` **rejects them with HTTP 400** ("value must be
one of …"). The first attempt was to intersect availability with the REST enum
(`CREATABLE_GPU_IDS`, dumped via `POST /pods` with `gpuTypeIds=["__invalid__"]`), but that
*hides* cheap, available cards the website happily deploys, so pod creation moved to the
GraphQL deploy path (`deploy_pod` → `podFindAndDeployOnDemand`, the same mutation the RunPod
console uses). `CREATABLE_GPU_IDS` stays only as documentation of the v1 limitation.
**v2 has no such limit** (measured: a Blackwell card deployed straight through `POST
/v2/pods`), so on the default path `deploy_pod` is a thin wrapper over `create_pod` and all
of this is dead weight waiting for its deletion date.

**Two GraphQL-deploy gotchas that cost a debugging round (both verified live):**

1. **`volumeMountPath` is mandatory with a network volume.** REST defaults it to
   `/workspace`; GraphQL does not, and omitting it makes the container fail at create with
   `invalid mount config for type "bind": field Target must not be empty`. The pod shows
   `desiredStatus: RUNNING` but the container never starts, so it **never gets a public IP /
   port-22 mapping** and SSH never comes up. Pass `volumeMountPath: "/workspace"`,
   `volumeInGb: 0`, `networkVolumeId`. (v2 carries this as `mounts.network[].path`, still
   required.)
2. **`supportPublicIp: true` + `ports: "22/tcp"`** (a *string*, comma-separated, not the REST
   array) are needed for the direct-TCP SSH endpoint the app relies on (`publicIp` +
   `portMappings["22"]`, which appear ~90 s after RUNNING). **v2 needs neither**: `ports:
   ["22/tcp"]` alone publishes the endpoint, and sending `supportPublicIp` is a 422 by name.

A failed-mount deploy can also leave an **auto-replacement pod** whose id the mutation never
returned: diag/teardown must sweep by **name**, not just the returned id, or a replacement
lingers and bills.

### The CUDA host constraint

**The problem, verified live:** the pod image (`runpod/pytorch:…cu1281…`) needs host driver
CUDA **≥ 12.8**. RunPod machines vary: benchmarking landed a CUDA-**12.7** RTX 4090 whose
container never started (`nvidia-container-cli: requirement error: unsatisfied condition:
cuda>=12.8`), and the doomed pod burned the whole fallback chain (2×240 s waits per GPU). So
a deploy constrains which hosts it will land on.

**The policy is the same on both transports and is applied in one place**
(`deploy_cuda_versions` / `deploy_cuda_floor`, both gated by `_needs_cuda_floor`): the
constraint is added for **consumer GeForce cards only**. A GeForce card has no CUDA forward
compatibility, so a newer-CUDA image will not start on an older driver; datacenter and pro
cards (A100, H100, H200, B200, A40, A6000, L4/L40, RTX PRO / RTX A…) **do** forward-compat and
run the same image on older drivers, so a floor there only excludes in-stock hosts that would
have worked, and surfaces as "no instances available" while the console shows the card
available. That is a property of the hardware, not of the API, and it did not change when the
spelling did.

**How it is spelled differs, and this is where v2 is genuinely better:**

- **v2 sends `gpu.minCudaVersion`**, a real numeric floor compared numerically, so 13.2 counts
  as above 12.8.
- **v1 sends `allowedCudaVersions`**, which is **exact-match set membership, not a range**, so
  meaning "≥ 12.8" requires enumerating every version at or above it
  (`allowed_cuda_versions` derives the list from the image's `cuXYZ` tag against
  `KNOWN_CUDA_VERSIONS`, deliberately listing a couple of not-yet-existing future versions so
  a newer host is not excluded).
- **That enumeration had already gone stale by the time v2 replaced it.** Measured 2026-08-20:
  the catalog offers an **RTX 4090 on CUDA 13.2**, past the end of the hand-written table, so
  v1 deploys silently exclude every host on the newest driver, which is the exact failure the
  constraint exists to prevent, in the other direction. A numeric floor cannot go stale that
  way, which is the real argument for it. **If anything ever forces a return to the v1 branch,
  extend that table first.**
- **The two fields are mutually exclusive**: a non-empty `allowedCudaVersions` sent together
  with `minCudaVersion` is a documented 400, so `v2_pod_body` picks one and nothing upstream
  has to remember.

**The catalog's `cudaVersions` then turns a burned deploy into a pre-flight** (v2 only,
`cuda_capacity_problem`). "The card is in stock, but every machine on a new enough driver is
full" answers a create with *no instances available*, the **same sentence** a sold-out card
produces, so the user refreshes the picker, still sees the card listed with stock, and tries
again. One catalog GET before a create that takes minutes settles it and skips that card in
the chain with a real reason. It is advisory in the safe direction: `cuda_floor_reachable`
returns **None rather than False** for "unknown" (no data, an unreachable catalog, a card the
catalog has never heard of), because the only thing worse than a wasted deploy attempt is
refusing one that would have worked.

**Limit:** this makes the cu128 image *reliable*; it does not let an older-driver-only machine
run it. To actually use cards whose only hosts are on <12.8 drivers you would need a
**lower-CUDA image**, which for tagging is viable (only Ollama + the orientation CNN, and
orientation falls back to CPU via `torch.cuda.is_available()`), but the on-pod venv is built
`--system-site-packages` against the image's exact torch (2.9.1+cu128, see provision.sh), so a
different image needs the **volume re-provisioned** with a matching torch. Deferred: the
constraint already makes the current image reliable.

### Where else a card is in stock

v2's per-GPU `dataCenters[]` answers a question an empty picker used to raise and could not
settle: **is RunPod out, or is this data center out?** Those are fixed completely differently
(the first is waited out, the second means putting the model volume somewhere else), and "no
GPU available in EU-RO-1 right now" reads as the first even when it is the second.
`stock_elsewhere` is asked **only when the list comes back empty**, so the normal path still
costs one call; the count goes in the combobox and the detail in the log, with the note that
pods run where the volume lives. It is empty on v1, where GraphQL asks one data center at a
time: saying nothing is correct there, and a hint that quietly stops appearing when the
transport is flipped would be worse than one that never appeared. Deliberately **not** wired
into the Video Upscaler's picker, whose list can also be empty because of the SeedVR2
feasibility gate, where "in stock elsewhere" would be answering a question nobody asked.

### No GPU-type substitution (0.4.0)

A run deploys **only the card the user picked** in the live picker. Earlier versions seeded a
price-ordered *automatic fallback chain* from the selection (capped by a per-task price
ceiling so a sold-out cheap card couldn't silently escalate to, say, an A100-SXM4-80GB at
$1.49/h). That whole mechanism is **removed**: silent type-switching surprised the user during
benchmarking (a run quietly landed on a card they hadn't chosen). Now if the picked card is
sold out at deploy time the run **fails cleanly**, and the status line tells the user to press
the picker's ↻ to refresh stock and re-pick. `_selected_gpu_chain` returns just `[picked_id]`;
the deprecated `runpod.max_price_per_hour` / `max_price_per_hour_upscale` /
`max_price_per_hour_tag` keys (and the Settings → Remote ceiling spinners + `_fallback_ceiling`)
are gone and dropped from config on the next Settings save.

The GUI's tab pickers call `available_gpus` off-thread for the volume's region (≥32 GB
upscale, ≥16 GB tag), default to the persisted preference when in stock else the cheapest, and
pass the selection to `RemoteSession` via the `IMGTBX_GPU_OVERRIDE` env. Availability
fluctuates minute-to-minute, hence the ↻ refresh button, and fast enough to **fake a
discrepancy** when comparing two transports: a side-by-side run once returned 7 cards from one
and 6 from the other purely because a card sold out in the seconds between two sequential HTTP
calls, and over about twenty minutes the EU-RO-1 list turned over almost completely. Compare
within seconds, expect churn, and treat the recorded-payload tests rather than a live run as
the thing that actually pins parity. Surprise from the first run: RTX PRO 4500 (32 GB, $0.74)
was cheaper than the RTX 5090 ($0.99) for upscaling, exactly the "is a pricier pod more
cost-efficient per image?" question worth benchmarking.

### World-wide region + data-center picker (0.3.4)

The early Settings DC picker was an **EU-only** curated enum (`EU_DATACENTERS`, defaulting to
EU-RO-1), fine for the original user (Romania) but wrong for anyone else. The catalog also
exposes the full data-center list (`runpod_client.data_centers`: `GET /catalog/datacenters` on
v2, `{ dataCenters { id name location storageSupport listed } }` on GraphQL). Crucially it
reports whether a data center supports **network storage**: a model volume can only live where
storage exists, so the picker offers **storage-capable DCs only**, preventing a user from
provisioning a volume somewhere no pod can then attach it. (v2 spells this as
`networkVolumeTypes` being non-empty, where GraphQL had a `storageSupport` boolean. The two
were compared live: **18 storage-capable data centers on both**, identical sets.)

Settings has a **Region** combo (Europe / North America / Asia / Oceania, derived from the id
prefix via `region_of`) feeding a **Data center** combo, with a Refresh that pulls the live
list and a one-line "Volume actions act in: <region> · <dc>" target readout. A curated
`DATACENTERS` fallback makes it work offline. Selecting an existing model volume **syncs the
picker to that volume's region** (it's region-locked, so that's where Create/Provision and pods
run).

**Display names come from the curated list, not the API** (`curated_location`). v2 has no
`location` field at all and returns `name == id`, so without this the picker would degrade from
"Romania (EU-RO-1)" to a bare list of codes. This turned out to be an **improvement** rather
than a workaround: GraphQL called EU-RO-1, EU-NL-1 and EUR-IS-1 all "Europe", where the curated
list says Romania, Netherlands and Iceland. The API answers membership and capability; the
curated list answers display. A data center RunPod adds later is simply shown by its id.

**Storage support is the hard filter**: a region or data center with no network-volume storage
is **never populated**, so Oceania (whose only DC, OC-AU-1, is compute-only: it shows GPUs on
the RunPod website but can't host a network volume) just doesn't show up. Simple, no confusing
"exists but unusable" message. **Volumes are scoped to the selected data center**: the
Region/DC Refresh also re-lists model volumes, showing only the one(s) in that DC or a
**`None | <data center>`** placeholder when there isn't one, so the volume shown always matches
where a pod would run. The volume combo is read-only (pick, don't type); the chosen volume is
persisted with its **full display label** (`network_volume_label`, alongside the bare
`network_volume_id` the run code uses) so it reads in full on the next launch instead of
showing only the raw id. The Refresh also fills the Upscale/Tag GPU preference combos with the
GPUs the selected DC offers plus live price (`available_gpus(..., include_out_of_stock=True)`,
a preference list so a momentarily sold-out card still shows), partitioned by the VRAM floor,
and fills the **recent-spend readout** beside the money limits. The Upscale GPU, Tag GPU and
Model volume comboboxes share one aligned grid column, stacked below the Region/Data center row
with the volume action buttons beneath them.

<div align="right"><a href="#runpod-remote-pod-notes">↑ Back to top</a></div>

## `runpod` config section

The `runpod` block in `config.json` (the `api_key` is a secret and lives in the
untracked `config.local.json` overlay, item 9; the tracked template keeps it
blank):

```jsonc
"runpod": {
    "api_key": "",                  // secret; stored in config.local.json
    "api_version": "v2",            // CONFIG-ONLY escape hatch: "v2" (default) or "v1".
                                    // Absent from the tracked template on purpose - the
                                    // default lives in runpod_client, not in the file.
                                    // See "The API v2 migration" below.
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
    "idle_timeout_minutes": 15,     // the real switch: stops a billed pod on a drop
    "session_cost_cap_usd": 0,      // funds_guard: auto-stop at this run cost; 0 = off
    "balance_floor_usd": 0          // funds_guard: keep at least this in the account;
                                    // 0 = off, and it retires with the GraphQL balance
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

## The API v2 migration (0.6.1, roadmap #25)

RunPod dated **both** transports this app was using for shutdown, five months apart, so 0.6.1
moved the whole integration onto `api.runpod.io/v2`. This section is the record of what was
decided, what was measured, and what is still on a clock. The roadmap entry
(`docs/future-features.md` #25) is now only the two **dated deletions** below; everything else
about the migration lives here. Code comments cite the phases as `#25 P0` … `#25 P4`; what each
one moved is [below](#what-each-phase-moved).

| Transport | What the app used it for | Dies |
|---|---|---|
| REST v1 (`rest.runpod.io/v1`) | `list_pods`, `get_pod`, `create_pod`, the lifecycle verbs, `pods_using_volume`, all four network-volume calls | **2026-11-15** |
| GraphQL (`api.runpod.io/graphql`) | `deploy_pod` (every pod the app creates), `available_gpus` (all three live pickers), `data_centers`, `list_pods_detailed`, `account_balance` | **early 2027** |

Losing v1 breaks teardown, volume management and status polling. Losing GraphQL breaks **pod
creation**, which is the remote feature entirely. Neither half was optional, and
**`Sunset` headers are announced but not served** (measured on both hosts: 200 with no
`Sunset`, `Deprecation` or `Link`), so the dates are hard-coded and a **410 is the only signal
anyone gets**. `_http_error_message` turns it into the one sentence a user can act on: *"RunPod
has retired the API this version of Image Toolbox uses. Update the app."*

### The failure mode this migration actually had

Not the loud one. A wrong **request body** is a hard 422 that names the field, and unknown body
properties are rejected outright (`unevaluatedProperties: false`), so the create path fails
noisily and legibly. Two other things fail silently, both **toward spending money**:

1. **A renamed response field reads as None**, and this codebase's checks treat None as a
   decision rather than as an absence. Three worked examples, all real:
   - `remote_run._find_existing_pod` compares `desiredStatus` to `RUNNING`. Under v2 that is
     always None, so the app **never recognises its own running pod** and deploys a second one
     beside it. Two pods, both billing.
   - `runner_common.remote_pod_stopped` returns True for `status in (None, EXITED, TERMINATED)`.
     Under v2 it returns **True unconditionally**, so every transient blip reads as "the pod is
     gone" and the auto-resume supervisor (#6) ends exactly the runs it exists to rescue.
   - `remote_run` reads `costPerHr` into `funds_guard`'s `cost_per_hr`. Under v2 that is None,
     so accrued session cost computes as zero and **the session cap never trips**, the one
     guard that was going to survive losing the balance.
2. **An unknown query parameter is ignored, not rejected.** Measured:
   `GET /v2/pods?desiredStatus=RUNNING` answers **200 with the full list**. A filter that
   stopped being applied looks exactly like one that matched everything, which is why
   `list_pods`' `**filters` was **deleted rather than ported**.

None of that raises and all of it looks like a normal run. It is the same shape the project has
hit repeatedly (`known-defects.md` D1, the ffmpeg pin, NVENC, the BtbN month-end URL):
**present is not working, and accepted is not applied.** The countermeasure is to assert on
**behaviour**, not on a call returning 200.

### The read seam

Every response read goes through `runpod_client`: `pod_state`, `pod_cost`, `pod_data_center`,
`pod_gpu`, `pod_gpu_count`, `pod_ssh`, `pod_volume_id`, `pod_record`, `pod_runtime_sample`,
`volume_data_center`, `unwrap_list`, `error_detail`. Each accepts **every transport's shape at
once**, so nothing outside the module reads a RunPod field by name and a transport swap cannot
turn a rename into a silent None.

The tests pin it with payloads **recorded off one real pod read through all three transports**
within the same minute, not synthesised from the spec, and each money test is named for the
**consequence** rather than the field (`test_a_second_pod_is_not_deployed_beside_the_first`,
not `test_pod_state_reads_status`). A separate test tokenises every module in `scripts/` and
fails with `file:line` on any raw read of a transport-specific field. **That sweep was itself
broken and green on the first attempt** (a regex over text matched nothing while a planted
violation sat in the tree), so it now scans tokens and has its own test
(`test_the_scanner_recognises_a_raw_read`) proving it can still fail.

### The switch, and which way it points

`runpod.api_version` selects a whole **stack**: `v2` (v2 REST + v2 catalog, the default) or `v1`
(v1 REST + GraphQL). It is **config-only, with no Settings control**, like `ntfy_token`: it
exists for the day beta churn bites, and `probe_api_version` names the exact key and value to
set when the configured version stops answering. It is applied **once per process** by the two
config loaders every path already goes through (`runner_common.load_config` for the runners,
`gui.common` for the GUI) rather than at each call site, because a call site that forgot would
talk to the wrong transport and the symptom would be a silent None.

**v2 is the default and v1 is the escape hatch, not the reverse.** The tempting shape is "keep
running v1, fall back to v2 when v1 stops answering", and it is wrong for four reasons, the
first decisive on its own.

- **Installs do not update in lockstep.** A version shipped with v1 as its default is still
  running on someone's machine on 2026-11-15, and that is the day it breaks, with nobody at the
  keyboard. Ship v2 as the default and those same installs pass the date untouched. The in-app
  updater helps but does not settle it: "Skip this version" exists.
- **The fallback path is the untested path.** Whichever transport is the default is the one
  every real run exercises; the other runs for the first time on the day it is needed.
  v1-default means v2's first real execution is in November, unattended, on every install at
  once, spending money. Code that has never run is not a fallback, it is a hope.
- **A fallback should point at the more durable transport, and v1 dies first.** v1 also cannot
  stand alone: `deploy_pod` lives on GraphQL precisely because v1's create enum rejects the
  Blackwell cards the picker offers. "Default to v1" really means keeping v1 **and** GraphQL.
- **Automatic cross-transport retry is unsafe exactly where the money is.** "v1 failed, try v2"
  around pod creation can deploy **two billed pods** when the first call succeeded and only its
  response was lost. Deploy is not idempotent, so it must never auto-fall-back. (The same
  argument runs the *other* way for teardown, where failing to stop a pod bills until the
  dead-man's switch fires. One rule for both would be wrong, so the switch is never automatic
  per call.)

### What each phase moved

- **P0, the seam and its tests**, above. Nothing about the app's behaviour changed; this was
  the change that made the rest safe.
- **P1, REST v1 to v2.** Base URL; `/networkvolumes` to `/network-volumes`; the volume create
  body's `dataCenterId` to `dataCenter` (**the only renamed field in a REQUEST**, which is why
  it cannot go through the read seam); size bounds checked against the live version (v1 1-4000,
  v2 10-4096) so a refusal quotes a limit the user can act on; the three lifecycle verbs
  collapsing into `POST /pods/{id}/action`; and `create_pod` gaining `v2_pod_body`, which
  **rebuilds** the body key by key rather than patching a copy, because `unevaluatedProperties:
  false` turns one leftover v1 key into a 422.
- **P2, GraphQL to v2.** `deploy_pod` became a wrapper over `create_pod`; `available_gpus` and
  `data_centers` moved onto `/catalog/*`; `list_pods_detailed` collapsed to a single
  `GET /pods` (which already carries `gpu.id`, `dataCenterId` and `cost`, so there is nothing
  to enrich and no second source to fall back to: the GraphQL ladder with its memoised
  `_PODS_MACHINE_SELECTIONS` probing survives only on v1). The CUDA policy was hoisted so both
  paths apply it identically: a transport swap must not quietly change which **hosts** a run can
  land on.
- **P3, the account balance.** It has no v2 successor at all; see below.
- **P4, the opportunities v2 opened.** `gpu.minCudaVersion` and the `cudaVersions` pre-flight,
  per-DC stock, `ERROR` in `TERMINAL_STATUSES`, `pod_logs`, `pod_runtime_sample` and
  `account_spend`. All six are **advisory by construction**: none may turn a working deploy, run
  or readout into a failed one, so each answers "I do not know" as loudly as the useful case.
  The first three are described in the GPU sections above; the last three are below.

### The account balance has no v2 successor

**Re-verified against the live spec and the live account on 2026-08-20**, not read off the
migration guide: the OpenAPI document has 34 paths and exactly one `/v2/account/*`, which is
`ssh-keys`. `/v2/account`, `/v2/account/balance`, `/v2/account/credits`, `/v2/user` and `/v2/me`
all **404**. The words "credit", "funds" and "wallet" appear nowhere in the spec, and all eight
matches for "balance" are "load balancer". [runpod/docs#807](https://github.com/runpod/docs/issues/807),
which put the question to RunPod directly, is still open with zero comments, and an unanswered
issue is itself an answer by the time GraphQL stops serving.

So the balance lives solely in GraphQL's `myself { clientBalance currentSpendPerHr }`, and that
**island stays until it 410s**. It is one query and one function, it is already fail-safe, and
it costs nothing to leave working.

**What that leaves standing.** The **session cap survives intact**: `session_cost(cost_per_hr,
elapsed)` needs no balance, and v2 reports `cost` on the pod itself, which is the *real* billed
rate rather than the picker's list price. The **start floor and the balance floor die with the
island**, and they die quietly and correctly, because `funds_guard` is **fail-open by
contract**: an unreadable balance skips those checks rather than blocking a run.

**Correct, and invisible, which is what P3 actually fixed.** A user who configured a floor
would keep a floor that is no longer applied, with nothing on screen and nothing in the log to
say so: the same family as every other bug this migration is about. Now
`account_balance_detail()` classifies a lookup (`BALANCE_OK` / `NO_KEY` / `RETIRED` / `ERROR`,
never raising) while `account_balance()` stays byte-for-byte the old plain-pair-or-None wrapper
so no existing caller changes; `RunPodError.status` makes permanence readable without matching
on message text; the pure `funds_guard.floor_unenforced()` is the one place that words it, and
**words RETIRED and ERROR differently** (a blip needs no action; a retirement means moving to
the per-run cap); the guard's `on_warn` hook is finally fired, having been accepted, stored and
called by nothing since it was written; and the readout says **`Funds: Not published`** against
**`Funds: Unknown`**, with the preflight and the "Funds guard armed" line saying it too.

**`spend_per_hr` has no naive successor either**, and this is the measurement that kills the
obvious replacement. Summing `pod_cost()` over the running pods looks equivalent. Measured with
**zero** pods running, GraphQL reported `currentSpendPerHr: 0.005`, the network volume's
standing storage charge, which no pod query can see. The pod sum would have reported $0.00/h for
an account that is genuinely being drained.

**GraphQL answers a field that has left the schema with HTTP 400**, not the 200-plus-`errors`
that seemed likely, carrying `Cannot query field "…" on type "User".` in an `errors` list of
**objects** where v2's RFC 9457 list holds plain **strings**. Both matter: the classification
reads the message rather than the status, and `error_detail` had been stringifying those objects
verbatim, putting a Python dict repr in front of the user with the actual sentence buried inside
it.

### Diagnostics that do not need the tunnel

Everything the app knows about a running pod normally arrives through its own `ssh -L` tunnel
and the on-pod worker, which is precisely what is missing when something has gone wrong. Two of
P4's items exist for that moment.

**`pod_logs(api_key, pod_id, tail=100)`** reads the pod's own container + system log from the
control plane. `create_pod_resilient` calls it in the failure path **before** the terminate that
destroys the only copy, and `remote_run` prints each line prefixed `pod log |` so it is
obviously the container talking. It is **SSE, not JSON**: the endpoint backfills `tail` lines and
then holds the connection open forever, so a read timeout is the **normal exit**, not an error,
and the call always costs its full timeout. Measured live: 40 lines in 8.8 s, `[system]` for the
image pull and `[container]` for the start script, ending on "Pod is ready to use". Two details
are load-bearing: **`readline`, not `read(n)`** (a fixed-size read discards everything it had
buffered when the socket times out, which on a short backfill is the entire log), and stopping
early once `tail` lines have arrived was **rejected**: it is correct only if the server honoured
`tail`, and if it ever replayed the whole log instead, an early stop would hand back the OLDEST
lines while looking identical.

**`pod_runtime_sample(pod)`** turns a pod's `runtime` block into the telemetry sample the GUI
already renders, and `RemoteSession` passes it to the engine as `telemetry_fallback`. The remote
row going blank was the only sign that the tunnel had stopped answering, and it looks exactly
like a card that has gone quiet. It is **not a drop-in equal and must not pretend to be**:
`runtime` publishes utilisation **percentages with no capacities**, so the sample fills
`ram_pct`/`gpu_mem_pct`, leaves the `*_used_mb` pairs alone, and `TelemetryRow` shows the bare
percentage. Inventing a total to keep the familiar "12.3/24.0 GB" shape would put a fabricated
number on screen beside real ones. It marks itself `via="api"` so the row can say "via API
(tunnel down)", or the drop in detail reads as the pod having gone quiet. Measured on a live
pod: **an idle pod reports 0 on all four figures** while `uptime` counts up, so "nothing to
report" is tested per field with `is None`: a truth test would make an idle pod
indistinguishable from an unreachable one. A v1 pod has no `runtime` at all, which is why the
field joined the seam's forbidden-read sweep.

### Recent spend

`GET /v2/billing` cannot replace the balance, but it is what remains when the balance retires,
and it answers something the balance never could: **where the money went**.
`account_spend(api_key, days=30)` is one call, since `metadata.totals` already sums the window, so the
per-bucket records are not walked and **must not be counted on**, since a bucket with no spend is
omitted rather than zero-padded (measured: `lastN=3&bucketSize=hour` returned 2 records). The
RunPod tab shows it beside the funds floor and the per-run cap, filled by Refresh:

> spent in the last 30 days: $4.26 ($0.79 pods, $3.48 storage)

**That split is the point.** A network volume bills around the clock whether or not anything is
running, which no other readout in the app reveals. On this account a 7-day window read $0.7632,
of which $0.7389 was the standing 50 GB model volume and **$0.0243 was the entire live
verification bill for P0 through P2**, four real pods for under three cents. None on any
failure and on v1, shown as an **empty label** rather than a "$0.00" that would read as "nothing
was charged".

### Verified on real hardware, 2026-08-20

Almost everything was verified read-only or with deliberately invalid writes. What needed real
pods got them; five pods across the whole migration, for well under ten cents.

**Pod 1 (RTX PRO 4500 Blackwell, EU-RO-1)**: created through `POST /v2/pods`, read back through
v1, GraphQL and v2 inside the same minute, terminated immediately. It settled four questions:
`env.PUBLIC_KEY` **still gets SSH in** with `startSsh` omitted (so the app's zero-config SSH
survives and nothing needs registering on the account); `ports: ["22/tcp"]` **alone publishes a
direct-TCP endpoint**, with `ssh.direct` matching v1's `publicIp` + `portMappings["22"]` byte for
byte; **a Blackwell card deploys through v2's POST**, which is the question `deploy_pod` existed
to answer; and `mounts.network` **mounts the model volume** at the given `path`, with the
provisioned tree present.

**Pod 2**: created on v2, **stopped through the action endpoint**, read back, terminated. A
stopped pod reports **`EXITED`**, the same value v1 uses, so `ensure_stopped`'s idempotence and
`remote_pod_stopped` are both correct on v2 (worth measuring, since v2's `PodStatus` enum is
richer), and it keeps a populated `cost` while stopped, which is right: a stopped pod still bills
its disk.

**Pod 3 (RTX 4090)**: the P2 production path, on a **consumer** card on purpose, since
`is_consumer_gpu` gates the CUDA policy and the shipped default `gpu_type_id` is a GeForce.
Through `create_pod_resilient` → `deploy_pod` → `create_pod` → `POST /v2/pods` carrying the CUDA
constraint, landed, published its SSH endpoint after ~84 s of reporting RUNNING, and answered
correctly through `pod_record`, `list_pods_detailed` and `pods_using_volume`, the last of which
reads v2's `mounts.network[].volumeId` and had until then only been unit-tested.

**Pod 4 (RTX 4090)**: P4, again consumer on purpose, with the request bodies recorded off the
wire. The create carried `{"id": "NVIDIA GeForce RTX 4090", "count": 1, "minCudaVersion":
"12.8"}` and **no** `allowedCudaVersions`; RUNNING with an SSH endpoint in 72 s; landed on a host
reporting **CUDA 13.0**; read back through `pod_record`, three `runtime` samples and a 40-line
log fetch; terminated, with nothing left on the account.

**Four findings from those hours changed decisions:**

- **v2 reports `status: "RUNNING"` at creation**, in the create response itself, about 50 s
  before `ssh.direct` was anything but null. The richer enum does **not** mean the status can be
  trusted alone: `wait_until_running` must keep requiring an SSH endpoint, or a run is handed a
  host of `None` and fails somewhere far less obvious.
- **The v1 pod object is worse than assumed**: on the measured pod `machine` came back `{}` with
  **no GPU field at all**, so v1 alone cannot even name the card it is billing for.
- **The GPU label is transport-dependent.** The same card was `"RTX PRO 4500"` on GraphQL and
  `"NVIDIA RTX PRO 4500 Blackwell"` on v2, and only the latter is the string a deploy accepts. So
  the label is **display-only and must never be matched on**; the seam's tests pin that rather
  than pretending the records are identical.
- **RFC 9457 is live and its `errors[]` array is the valuable half.** Verbatim: a sold-out card is
  `{"title": "Bad Request", "status": 400, "detail": "There are no longer any instances available
  with the requested specifications. Please refresh and try again."}` and a bad enum value is
  `{"title": "Unprocessable Entity", "status": 422, "detail": "Request validation failed.",
  "errors": ["$.action: value must be one of 'start', 'stop', 'restart', 'terminate'"]}`. Reading
  only `error`/`message`, as the client did, would have thrown both away and shown a bare "HTTP
  400 Bad Request" for a card that is simply sold out.

**`start_pod` is the one call that could not be proven.** RunPod answered `400 Bad Request:
Failed to resume pod.` The request SHAPE was accepted (a malformed one is a 422 naming the field,
measured), so this is RunPod declining to resume that particular stopped pod, which it does when
the machine no longer has room, not a defect in the call. Recorded rather than chased because
**the app never calls it**: runs deploy fresh disposable pods and only ever stop or terminate
them. Worth knowing before anyone builds a feature on resume.

### Traps

- **Never call `PUT /v2/account/ssh-keys`.** It **replaces** the account's registered public
  keys. The app owns a managed key and injects it per pod precisely so it never touches
  account-wide state; writing there would silently clobber the user's own keys and lock them out
  of every pod they own, including ones this app knows nothing about. It is the most destructive
  call in the new API and it is one line away from looking like the tidy solution to the
  zero-config-SSH question.
- **Cloudflare 403s a bare urllib User-Agent** on the v2 REST paths, not just GraphQL: see the
  REST API section. A probe script reads it as a rejected key.
- **The pass-through spec dict is gone.** `deploy_pod` takes a v1-REST-shaped dict and translates
  it, so callers never had to change. v2 rejects unknown properties outright, so that dict must be
  built for v2 or explicitly translated: there is no "extra keys are harmless" any more.
- **`reset` has no v2 equivalent.** The app does not use it. Recorded so nobody goes looking.
- **Live stock moves fast enough to fake a transport discrepancy** when comparing two
  implementations side by side: see the GPU availability section.

### Still on a clock

Two deletions remain, and they are the only reason roadmap #25 is still an open entry.

1. **After 2026-11-15, delete the v1 half.** `_V1_LIFECYCLE`, the v1 branches throughout
   `runpod_client`, `_DEPLOY_MUTATION`, `_GPU_AVAIL_QUERY`, `_DC_QUERY`,
   `_PODS_MACHINE_SELECTIONS`, `CREATABLE_GPU_IDS`, `KNOWN_CUDA_VERSIONS` +
   `allowed_cuda_versions` + `deploy_cuda_versions`, the `list_pods_detailed` fallback ladder,
   and the `api_version` switch itself.
2. **When the balance query 410s (early 2027), delete the GraphQL island.** `_graphql`, its
   browser User-Agent workaround and `_BALANCE_QUERY`. On that day the floor retires by itself:
   `floor_unenforced` starts saying the retired wording, the readout starts saying "Not
   published", and nothing breaks. The one thing left to do is **remove the floor field from
   Settings** so it stops being offered.

**Until then the v1 branch stays COMPLETE.** An escape hatch missing half its code is not one,
which is why the module got *longer* rather than shorter across this migration. The net line
count comes down on those two dates, not before.

<div align="right"><a href="#runpod-remote-pod-notes">↑ Back to top</a></div>
