# Image Toolbox

### AI-leveraged image/video toolbox for Windows: **upscale** low-resolution photos and videos, and **describe & rename** pictures using local or remote upscaling and vision models. Built to revive personal photo/video collections and old footage and pictures taken with early digital cameras.
### ========
> ## ⚠️ The application is offered as-is.
> ### ⚠️ The author can't be held responsible for data loss.
> Always test on a small, disposable sample first. I am not responsible for data loss. Use this tool at your own risk.
> *(That said: the application never modifies your original images or videos, unless you specifically tell it to. Guardrails are put in place so that it is clear when (and how) your original data is modified.)*

Batch Upscaler runs the [SeedVR2](https://github.com/ByteDance-Seed/SeedVR) upscaling pipeline **directly in-process**. Image tagging uses [Ollama](https://ollama.com) vision models.
* Local work: *Everything runs on your machine.*
* Remote pods: *Images are sent via SSH to the remote pod **only**.*
* ***No data is sent to third parties without your knowledge.***
* ***All your data is under your direct control.***


---

## Install (Windows installer — recommended)

No Git, no Python knowledge required:

1. **Download** `ImageToolboxSetup.exe` from the [latest release](https://github.com/war4peace/image-toolbox/releases/latest).
2. **Run it** and click through the installer (no administrator rights needed).
3. **Double-click** the *Image Toolbox* shortcut.

The installer offers options to use it locally (taking advantage of your local, powerful GPU) or on a remote machine using RunPod.io infrastructure. ***Note:** I have no business relationship with runpod.io. The only (mutual) "advantage" (in a manner of speaking) is: when you create an account on RunPod from the application, my referral link is used. This gives both me and you an extra credit of 5 USD when you add at least 10 USD to your runpod account.*

The first launch opens a setup window that downloads the required components: Python, PyTorch with CUDA, the SeedVR2 engine (about 150 MB for remote-only, or about 3 GB, if you also picked the option to locally upscale images or videos), a GPL ffmpeg build (~160 MB, used by the Video Upscaler), a bundled libVLC (~40 MB, for in-app video playback with sound) and then starts the app. It also offers to install [Ollama](https://ollama.com) and the vision model used by **Tag & Rename** (~6 GB; optional. Local upscaling works without it, and you can decline). The first upscale process you run additionally downloads the AI upscaling model weights (~16 GB) automatically. Everything the setup prints is saved to `bootstrap.log` in the application folder (useful for troubleshooting).

> **Windows SmartScreen note:** because the installer is a new, unsigned download, Windows may show *"Windows protected your PC — Unknown publisher"*. Click **More info → Run anyway**. The installer is built automatically from the public source in this repository by GitHub Actions; you can verify the build on the repository's **Actions** tab.

**Requirements:**

***Local* Upscaling / Tagging:**
* Windows 10/11 (64-bit)
* An NVIDIA GPU with current drivers (16 GB VRAM minimum)
* An internet connection
* ~25 GB of free disk space (plus ~6 GB if you install the tagging model). (PyTorch ships its own CUDA runtime, so a separate CUDA Toolkit install is **not** required).

***Remote* Upscaling / Tagging:**
* Windows 10/11 (64-bit)
* An internet connection
* ~3 GB of free disk space (Python infrastructure for application functionality)
* A runpod.io account (which you can create via a link from the installer, or separately)

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
.venv\Scripts\python.exe -m pip install -r seedvr2\requirements.txt pillow piexif timm paho-mqtt python-vlc

# 3. Launch the GUI (model weights ~16 GB download automatically on first use).
#    (In-app video playback also needs a libVLC 3.0.x build in a "vlc\" folder next
#     to the app; the GUI offers a one-click "Install libVLC now" if it's missing.)
.venv\Scripts\pythonw.exe scripts\toolbox_gui.py
```

Or just double-click **`Image Toolbox.cmd`** after cloning — it bootstraps the environment and launches the app for you.

You can also run the tools headless from PowerShell:

```powershell
.venv\Scripts\python.exe scripts\batch_upscale.py "X:\Your\Photos"               # upscale
.venv\Scripts\python.exe scripts\batch_upscale.py "X:\Your\Photos" "Z:\Output"   # custom output
.venv\Scripts\python.exe scripts\tag_and_rename.py "X:\Your\Photos"              # tag & rename
.venv\Scripts\python.exe scripts\conciliate.py "X:\Your\Photos" "Z:\Output"     # conciliate (archive)
.venv\Scripts\python.exe scripts\batch_video_upscale.py "X:\Your\Videos" --target 1080p         # video upscale (remote RunPod pod)
.venv\Scripts\python.exe scripts\batch_video_upscale.py "X:\Your\Videos" --local --target 4K    # video upscale (this machine's GPU)
```

The Video Upscaler runs on a rented RunPod GPU by default; add `--local` to upscale on your own GPU instead. `--target` (`1080p` / `1440p` / `4K`) scans the folder and enqueues every eligible video before running; omit it to process a queue you already prepared in the GUI. Like the image tools, it mirrors the source tree into an output subfolder and never touches your originals.

The GUI and the scripts share the same logs and cache database (`db/cache.db`), so you can mix and match freely.

---

## The app

Windows GUI (mostly Python standard-library tkinter) with six tabs.

### Common features

- **First-start wizard:** on the first launch it detects your GPU and recommends the upscaling (SeedVR2) and tagging (Ollama) models that suit your VRAM, and offers to download the vision model in one click. Every model stays selectable (a smaller card can still run a bigger model, just slower). If your card can benefit, it also offers an **optional speed-up for local video** (`torch.compile`): it explains the download size and the trade-offs, installs the small Triton piece for you, and points you to Microsoft's page for the C++ build tools. It's optional and affects speed only, never quality. Re-run the wizard any time from **Settings → Re-run first-start wizard**.
- **Update checker** makes sure you don't miss updates. Update straight from the app.
- **Telemetry rows** (for local and/or remote machine): CPU / RAM / VRAM / GPU.
- **Live feedback:** two-row status (current + previous file), a progress bar, and an estimated time remaining that refreshes after each image.
- **Live preview**: Batches of 100 images are loaded into a "preview" pane, allowing you to open images, perform a live comparison (upscaled images), context menu (right-click images) with common actions.
- **Resizable thumbnails** in the film strip area.
- **Notification support**: Currently supports *Discord*, *Telegram* and *ntfy.sh*.
- **MQTT integration** (e.g. for Home Assistant, see **Home Assistant (MQTT)** section below).

### Batch Upscaler

- **High-quality image upscaling** with SeedVR2, prioritising quality over speed. The target is capped at 3840 × 2160 by default (a **Resolution Target** of 4K, 2K or 1080p is selectable in Settings), so results display at native resolution on common screens.
- **Your originals are never touched.** Upscaled images are written to a separate output folder that mirrors the source folder tree and filenames.
- **Auto-straighten** (on by default): a small CNN detects sideways photos and rotates them upright *before* upscaling, so the result fits the Resolution Target on the correct axis (a sideways photo upscaled on the wrong axis stops fitting once it's turned upright). It works on a temp copy, so the source is never touched; only confident calls act, and ambiguous ones are left alone. Toggle it and tune the threshold in **Settings → Upscaling**.
- **Skip-cutoff:** images already close to the target are skipped (default 66% of the target on either axis — i.e. anything that would gain less than ~1.5×). Set it to 0 in Settings to upscale everything eligible.
- **Resilient long runs:** a cache (in the local SQLite database `db/cache.db`) lets a stopped batch resume where it left off; corrupt and missing files are detected, logged and skipped (corrupt files are listed at the end so you can review them); a **second pass** re-scans the source when the batch finishes and processes anything new that appeared while it ran.
- **Pause / resume / stop** buttons; a stop finishes the current image first so a file is never left half-written.
- **Log window**: Displays more detailed information in a separate window.
- **Works with mapped network drives.**

### Tag & Rename

Analyses each image with a local or remote Ollama vision model, writes a description into EXIF, and renames the file to `OriginalName_Condensed_Description.ext`.
- **Auto-straighten** (on by default): a small local CNN detects photos shot with the camera held sideways and rotates them upright *before* tagging, which also improves the descriptions. Only confident calls are acted on; upside-down and ambiguous images are left alone and logged, so a photo is never wrongly rotated. Toggle it and tune the confidence threshold in **Settings**; rotations are reverted by **Undo** like everything else.
- **Selectable description language**, plus force-tag / force-rename options.
- **One-click Undo** restores file names, EXIF descriptions, or both. Every change is recorded to an undo cache before anything is modified.
- Already-tagged files are detected and skipped on re-runs (unless forced, optional).
- **The vision model is your choice** (set it in **Settings**). The default is [`qwen2.5vl:7b`](https://ollama.com/library/qwen2.5vl) — the most accurate of the models tried, reading faint on-screen text and inferring fine detail; it needs ~16 GB VRAM (a 16 GB+ GPU). If you have less VRAM, switch to [`minicpm-v`](https://ollama.com/library/minicpm-v) — fast and light (~7.6 GB VRAM, runs on an 8 GB GPU), which uses terser descriptions. In testing, `llava:34b` was the slowest, heaviest *and* least accurate of the models tried. It's the least recommended option, but still available if preferred.

### Conciliation

Once you're happy with the upscaled (and optionally tagged & renamed) results, **Conciliation** moves them back into your original folder tree so the originals are replaced by their high-quality versions. No manual shuffling required. It handles **photos and videos**.

- Pick an **Original Files** folder and a **Processed Files** folder, then **Scan / Preview**. Nothing is touched until you click **Run**: the preview shows a per-folder summary (how many will be *replaced*, how many files have *no match*, and how many non-media files are *kept*).
- Each original is matched to its processed counterpart using the cache database, falling back to filename matching when there's no cache. It works for upscaled-only, upscaled-then-tagged/renamed, and upscaled videos.
- Two operations: **Archive originals** (default; moves each original into an `__Archive__` subfolder) or **Delete originals** (permanent, with an extra confirmation). The processed file then takes the original's place, keeping its own name (a photo's descriptive name if it was tagged, or a video's `name_4K.mp4` output name).
- **Videos too:** a video original (e.g. `clip.avi`) is matched to its upscaled output (e.g. `clip_4K.mp4`) by content-hash lineage, so it still matches after folders are moved or renamed. A video is only ever acted on when its output is actually present in the Processed folder you chose, so pointing Conciliation at a photo-only output folder never touches your videos (and vice versa) and the preview always shows exactly what will happen first.
- **Safety first:** an original with no processed counterpart is never touched, and non-media files are never touched. After a run, emptied processed folders (e.g. a leftover `__upscaled__`) are cleaned up. The tab is locked while the Batch Upscaler or Tag & Rename is running, since they may share the same folders.

### Video Upscaler

Upscales a folder of videos with the same SeedVR2 engine the Batch Upscaler uses, either on a **rented RunPod GPU** (fast, paid) or on **your own local GPU** (free, slower). Video means a diffusion pass per output frame, so remote could offer access to powerful GPUs which greatly reduce processing time; local is there so anyone with a capable GPU can do it at no cost, except power usage. Your source videos are never modified.

- **Run locally or remotely:** a **Run on** switch picks Remote (a rented RunPod card, billed by the second) or Local (this machine's NVIDIA GPU). The same walk, split, resume, mux and drift-check pipeline runs either way; only where the GPU work happens changes. The switch follows your install type (a Remote-only install can't run locally, and vice versa).
- **Scan, queue, go:** scan a folder, filter the list (by path, resolution, duration, FPS or a combined set), pick a target per video, build a queue, pick a GPU, press Start. Targets are offered as concrete output resolutions and filtered to what the source can be upscaled to **and** what the chosen GPU can actually reach: low-resolution sources get **2× / 4×** options (e.g. 320×240 → 640×480, 1280×960), and a target a small card would run out of memory on is not offered (locally) or greys out in the queue (remotely, with Start refused if nothing fits the chosen pod).
- **Extract a scene:** for a long source you don't want to upscale in full, mark an in/out range on a live preview and queue just that clip. The source is never touched (the clip is cut to a temp file, upscaled, then reassembled), and each clip resumes independently like a whole video.
- **Cost estimate + confirm-before-rent:** the estimated duration and cost for the whole queue is shown *before* any pod is rented, and the estimator improves itself from your own finished runs. ***Note:*** *Estimates start rough and sharpen as you run; benchmarking your GPU (below) makes them accurate.*
- **Benchmark your GPU (local or remote):** a one-click **Benchmark GPU** window measures, for a specific card, the largest batch size that fits at each target and the card's real per-frame speed, by upscaling a short clip at rising batch sizes until it runs out of memory. Those measurements replace the built-in guesses: the automatic batch sizing, which targets are offered, and the duration/cost estimate are all calibrated to your actual card. It benchmarks **your own GPU** or a **rented pod** (deployed just for the benchmark, billed while it runs, torn down afterwards), and it is resumable: every measured step is saved, so you can stop and continue, and each card is measured only once.
- **Pay in installments:** each video is split into ~1-minute segments and every finished segment is progress saved. Stop any time; the next Start resumes at the first unfinished segment. Optional per-run minute / dollar caps keep a long job on budget.
- **The original audio is kept** (muxed back into the upscaled result), and a drift check warns if the output timing ever diverges from the source.
- **High-quality deliverable:** H.265 10-bit by default (selectable in Settings), written to a mirrored `__upscaled__` output tree.
- **Compare, two ways:** a **Compare frames** window (before/after wipe with shared zoom/pan, timestamp-aligned scrubbing and frame stepping) for pixel-peeping, and a **Play videos** window that plays the original and the upscaled result **side by side, in real time, with sound** (via a bundled libVLC), so you can judge motion and temporal quality, not just single frames.
- **Auto-tuned:** you pick the target and the GPU; batch size, temporal overlap and the remaining SeedVR2 knobs are resolved for the card's actual VRAM, with automatic out-of-memory recovery. A choice of SeedVR2 models (7B / 3B variants) is available in Settings.
- **Local runs are guarded, not gated:** on your own GPU the batch is sized predictively from your card's VRAM (so it fits on the first try), targets that would run out of memory aren't offered, a watchdog stops a run that starts thrashing a long GPU session (the Benchmark GPU tool above measures its real safe batch and speed). For best results, close other GPU-heavy apps before a local run.

Video upscaling is compute-heavy: as a rough guide, one hour of footage upscaled to 1080p costs about $28-46 of GPU time on a rented card (the in-app estimator shows the real numbers for your queue before you commit), or nothing but time and power usage on your own GPU. Remote runs require a RunPod account, like the other remote features; local runs need an NVIDIA GPU with a CUDA build of PyTorch.

### Settings

Most of what is used to require hand-editing `config.json` is here:

- **Ollama URL** with a reachability check, and a **model** picklist populated from the models installed on your machine.
- **Auto-straighten** toggle and confidence threshold for Tag & Rename.
- **Resolution Target** (4K / 2K / 1080p) and the **skip-cutoff** percentage.
- **SeedVR settings** (attention mode, VAE tiling, outage threshold).
- **Discord webhook**, **Telegram bot** and **ntfy** notifications, each with a **Test** button (Telegram also has a **Detect** button to find your chat ID).
- **Default folders** for each tool, also settable from each tab's *Save as Default* button.
- **MQTT** settings (host, port, credentials, test button, manual publish button)
- **Update checker** settings.
- **Video Upscaler** settings: deliverable codec/quality, SeedVR2 model, and the advanced tuning knobs (best left on Auto).

Remote (RunPod) settings - the API key, region/data center, model volume, GPU preferences and pod management - live on the dedicated **RunPod** tab.

> **Secrets never touch `config.json`.** The API key, MQTT password and notification tokens/webhook URLs are stored in an untracked `config.local.json` overlay; `config.json` keeps only blank placeholders, so it is safe to share or commit without scrubbing. (Personal **Default folders** and non-secret fields like your MQTT host still live in `config.json` — clear those by hand if you want a pristine template to share.)

### Notifications

Get a message when a queue finishes (for both the Upscaler and Tag & Rename) and on errors (repeated failures, an engine that fails to start, Ollama going unreachable). Three backends, configured in Settings → Notifications, all optional and independent:

- **Discord** — paste a channel **webhook** URL.
- **Telegram** — create a bot with **@BotFather**, paste its **bot token**, open the bot and press **Start**, then click **Detect** to fill in your chat ID.
- **ntfy** — make up a **topic** name, subscribe to it in the [ntfy](https://ntfy.sh) app, and enter it here (the **server** defaults to the public `https://ntfy.sh`; point it at your own server if you self-host). On the public server anyone who knows the topic can read it, so pick an unguessable name.

Each has a **Test** button. Whatever you configure (any combination, or none) receives the same alerts.

### Home Assistant (MQTT)

Optional. Set an MQTT broker **host** in Settings → MQTT and the app publishes its state to your broker: version and update status, availability, live task state (what it's doing, progress, ETA, per-item timings), the last-run summary, and system telemetry (CPU / RAM / VRAM / GPU temperature) for this machine and, during a remote run, the rented pod. Clear the host to disable it.

A ready-made sensor set is included: [`docs/ha-mqtt-sample-sensors.yaml`](/docs/ha-mqtt-sample-sensors.yaml) defines every published topic as a Home Assistant MQTT sensor (local and remote-pod), so you can add them to your Home Assistant configuration instead of hand-writing each topic.

---

## Configuration

Settings live in `config.json` next to the app, with these sections:

| Section    | What it holds                                                        |
|------------|----------------------------------------------------------------------|
| `seedvr2`  | Paths to the engine repo, model weights, and the venv Python.        |
| `ollama`   | Ollama server URL and the vision model for tagging.                  |
| `upscale`  | Resolution target, skip-cutoff, SeedVR pipeline options.            |
| `tagging`  | Resolution threshold, timeouts, camera-filename patterns, etc.       |
| `defaults` | Pinned default folders for each tool.                                |
| `mqtt`     | Home Assistant / MQTT broker host, port, credentials, client id.     |
| `updates`  | In-app update-checker preferences.                                   |
| `runpod`   | Remote-pod settings: GPU/region/volume prefs and pod-management limits. |
| `video`    | Video Upscaler deliverable codec/quality and SeedVR tuning knobs.    |
| `notifications` | Discord webhook, Telegram bot token/chat ID, ntfy server/topic. |

You normally never edit this file by hand — use the **Settings** tab. The installer never overwrites your `config.json` on upgrade, and removes it only on a full uninstall.

**Secrets are kept out of `config.json`.** The RunPod API key, MQTT password and notification tokens/webhook URLs live in an untracked `config.local.json` next to it (created automatically the first time you save settings); `config.json` itself only ever holds blank placeholders for those fields, so it is safe to share.

---

## Remote GPU cost (RunPod)

If your PC has no capable NVIDIA GPU, the toolbox can run a batch on a rented [RunPod](https://runpod.io) GPU instead (tick *Run on remote pod* on the tab). It rents a pod, streams one image at a time, fetches the results back, and tears the pod down upon completion. Your source files are always copied, not moved, to the remote pod. The tables below estimate what a run costs.

The figures below come from benchmarking a 100-image sample of typical digital-camera photos through the in-app remote-pod runner, on RunPod secure cloud (EU region, June 2026), across roughly fifteen GPUs. `~sec/img` and `$/100` are whole-run averages over the 100 images (they include the one-time model load on the first image, so larger runs cost a little less per image). Where a card was benchmarked more than once, the figures average its valid runs (any run flagged degraded by the performance watchdog is left out). Upscaling figures use the resident-VRAM offload added in 0.3.5, where the SeedVR2 models stay in GPU memory for the whole run, and reflect the most demanding case: upscaling to the 4K Resolution Target with the 7B FP16 model (a lower target of 2K / 1080p, or a lighter 3B model, is faster and cheaper, so treat these as an upper bound).

### Remote Upscaling (Batch Upscaler)

| GPU | $/h | ~sec/img | $/100 images |
|-----|----:|---------:|-------------:|
| **NVIDIA A40** *(best value)* | 0.44 | 15.7 | **$0.19** |
| NVIDIA RTX A6000 | 0.49 | 14.3 | $0.19 |
| RTX 5090 † | 0.99 | 12.9 | $0.36 |
| **RTX PRO 6000 Blackwell** *(fast pick)* | 2.09 | 7.5 | $0.44 |
| A100 80GB PCIe | 1.39 | 13.8 | $0.53 |
| A100 80GB SXM4 | 1.49 | 13.7 | $0.56 |
| H100 80GB HBM3 | 3.29 | 8.8 | $0.81 |
| **NVIDIA B200** *(fastest)* | 5.89 | 5.9 | $0.96 |
| NVIDIA H200 | 4.39 | 7.9 | $0.96 |

† The RTX 5090 (32 GB) cannot hold both models resident, so it runs in the slower CPU-offload mode, yet it stays the best value among sub-40 GB cards.

**Findings:** The mid-VRAM Ampere cards (A40, A6000 at about $0.19 per 100 images) are the value winners: slower per image than a 5090 but far cheaper per hour, and the resident-VRAM offload is what makes them viable. For raw speed the B200 leads (5.9 sec/img) but at five times the cost, while the **RTX PRO 6000** is the sane fast pick, beating both Hopper cards (H100, H200) at a fraction of their price. The two A100 80GB variants land mid-pack at ~13.8 sec/img (the PCIe a touch cheaper per 100 than the SXM4): capable but unremarkable for upscaling, since the cheaper Ampere cards are far better value and the newer cards are far faster. SeedVR2 upscaling rewards newer architectures: Blackwell over Hopper over Ampere on raw speed.

### Remote Tag & Rename

| GPU | $/h | ~sec/img | $/100 images |
|-----|----:|---------:|-------------:|
| **NVIDIA RTX A4500** *(best value)* | 0.25 | 3.5 | **$0.025** |
| NVIDIA RTX 2000 Ada | 0.24 | 5.8 | $0.039 |
| RTX PRO 4000 Blackwell | 0.57 | 6.0 | $0.10 |
| RTX PRO 4500 Blackwell | 0.74 | 4.7 | $0.10 |
| RTX PRO 6000 Blackwell | 2.09 | 2.7 | $0.16 |
| A100 80GB PCIe | 1.39 | 4.6 | $0.18 |
| **H100 80GB HBM3** *(fastest)* | 3.29 | 2.4 | $0.22 |
| NVIDIA H200 | 4.39 | 2.6 | $0.32 |
| NVIDIA B200 | 5.89 | 3.2 | $0.53 |

**Findings.** Tagging is light: the vision model needs only ~6.6 GB, so the big datacenter cards are wildly overprovisioned and barely faster. The fastest card (H100, 2.4 sec/img) is only about 13% quicker than the RTX A4500 yet costs 13 times as much per hour. Pick a cheap card: an **RTX A4500** or **RTX 2000 Ada** tags 10,000 photos for around $2.50. (Tag & Rename also runs on a local GPU at no GPU cost: a local RTX 3090 tagged 100 photos in under six minutes.)

> **Caveats:** prices are point-in-time and vary by availability and region (the in-app GPU picker shows live prices). The estimates exclude the billed pod boot/teardown (~2 to 3 minutes) and the image upload/download time, so real bills run a little higher, most noticeably on very small runs. Source data: [`docs/image-benchmarks.csv`](/docs/image-benchmarks.csv).

---

## Samples

The [samples](/samples/) folder contains [original images](/samples/original/) and their [upscaled](/samples/upscaled/) versions, so you can compare pairs side-by-side and judge the upscaler's strengths and weaknesses.

---

## Notes

- **About the code:** these tools were written with the help of Claude (Anthropic) by someone who is not a trained developer — "vibecoding", as some call it. This is a personal project, shared for anyone to use at no cost.
- **Maintainers:** the installer is built by the `build-installer` GitHub Actions workflow from `installer/ImageToolbox.iss` whenever a `v*` tag is pushed.
