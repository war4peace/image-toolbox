# Changelog

User-facing release notes ship as the **annotated git tag message** (CI publishes
them as the GitHub Release body, and the in-app updater shows them). This file is the
working draft those notes are distilled from, and it records **experimental**,
in-development versions before they are tagged. For released versions, see the GitHub
Releases page.

## Contents

- [0.5.5](#055)
- [0.5.4](#054)
- [0.5.3](#053)
- [0.5.2](#052)
- [0.5.1](#051)

---

## 0.5.5

Better Tag & Rename descriptions, and much smarter remote provisioning.

### New default vision model: qwen3-vl:8b-instruct
A 100-image benchmark (RTX 3090) had the **qwen3-vl** family beat the old
`qwen2.5vl:7b` / `minicpm-v` / `gemma3:4b` picks at every size, with clearer, more
detailed descriptions. The new defaults, chosen by the first-start wizard to fit
your card:

- **24 GB+**: `qwen3-vl:8b-instruct` (the clearest; the new overall default)
- **16 GB**: `qwen3-vl:4b-instruct`
- **8-12 GB**: `qwen3-vl:2b-instruct`

Every model stays selectable in Settings. See the benchmark table in the README and
`docs/tag-and-rename.md`.

### Faster tagging via an Ollama context cap
Newer vision models declare a huge native context (qwen3-vl = 256K), and Ollama
sizes its VRAM off that, so uncapped they grab almost the whole card and thrash
(the 8B model ran 9:11 per 100 images at 98% VRAM). Tag & Rename now caps the
context (`tagging.ollama_num_ctx`, default 8192): the same run drops to **2:37 at
43% VRAM** with no quality change. Applied automatically, local and remote.

### Smarter remote provisioning
Provisioning the RunPod model volume is no longer a wasteful all-or-nothing job:

- **Caches the common model set** so you can switch models with no re-provision:
  all three vision tiers **and** all three SeedVR2 upscale tiers (3B Q8 / 7B
  FP8-mixed / 7B FP16) now fit the 50 GB volume.
- **Follows your configured models** (`ollama.model` and `upscale.dit_model`) so the
  model you actually picked is guaranteed on the volume (previously it silently
  provisioned a fixed default).
- **Incremental & self-pruning re-provision:** keeps whatever is already valid and
  fetches only what changed (the python venv is skipped via a stamp, the cached
  Ollama runtime is reused, weights skip valid files), and prunes obsolete models to
  reclaim storage. So a model change or a minor update is a cheap re-provision, not a
  fresh volume and a full re-download.

### Note on the Remote Tag & Rename cost table
The README's Remote Tag & Rename cost figures were measured with the old
`qwen2.5vl:7b` and are now marked obsolete pending fresh remote benchmarks with
`qwen3-vl:8b-instruct` (the new model ran at essentially the same speed locally, so
costs should be close).

---

## 0.5.4

A small cleanup and fix release.

### Fix: the Video Upscaler now shows telemetry on a local run
Running the Video Upscaler on your **own GPU** (not a rented pod) showed no
telemetry at all: no CPU / RAM / VRAM / temperature row under the carousel, and
nothing to click for the usage graph. The row is now there and updates live while
a local run works, and clicking it opens the same per-run usage graph (0.5.3) the
other tools have. A remote-pod run is unchanged: it still shows the pod's own row.

### Telemetry graph window polish
- The graph window now opens at the **same size as the main window** and won't let
  you shrink it below that, so the four charts always have room.
- It **remembers its size and position** between openings and across restarts, like
  the log and comparison windows.
- The range bar is cleaner: the active range button is now shown in **bold** instead
  of a separate "Showing: last Xh" caption.

### Documentation
- The README and every document under `docs/` gained a **Contents** list at the top
  and "Back to top" links, so the longer ones are easier to navigate on GitHub.

<div align="right"><a href="#changelog">↑ Back to top</a></div>

## 0.5.3

### See what a run is doing to your machine: live telemetry graphs
The little telemetry row under the image carousel (CPU / RAM / VRAM / GPU) has
grown up. **Click any telemetry row** and a graph window opens, plotting the whole
run over time.

- **Four graphs:** GPU and CPU load, memory (VRAM and RAM against how much your
  card/PC actually has), GPU power, and GPU temperature. Move the mouse across them
  for an exact readout at that moment.
- **Honest scale:** the memory and power graphs are pinned to your hardware's real
  limits, so a run that fills the card sits right at the top of the chart, not
  rescaled to look half-empty.
- **Starts when work starts:** the graph begins at the first image or video
  actually processed, so the long "scanning your folders" phase at the start does
  not show as empty space.
- **Range buttons** (1h / 3h / 6h / …) let you zoom into the most recent stretch of
  a long run; they light up as the run gets long enough. When a run ends the graph
  freezes so you can still review it.
- Works for both a **local** run and a **remote pod** run (click the matching row).

### More detail in the telemetry
The row and the graphs now also show **GPU utilization** (how hard the card is
actually working, not just how full its memory is), plus **power draw** and **core
clock**. These also go out over MQTT for Home Assistant.

### Ready-made Home Assistant dashboards
If you use Home Assistant, there are now **paste-in dashboards** under
`samples/home-assistant/`: a simple one that works on any install with no add-ons,
and a fancier one using popular HACS cards. Each pastes into a single dashboard
card (no risky whole-dashboard editing). Screenshots and a step-by-step README are
included.

<div align="right"><a href="#changelog">↑ Back to top</a></div>

## 0.5.2

### Pause now frees your graphics card
Pause used to stop the queue but keep the AI models loaded, so the card stayed
occupied and you could not go and use it for anything else. It now unloads
everything and hands the memory back, then reloads when you press Resume. The
queue is kept, so nothing is re-scanned and no work is repeated.

- **Batch Upscaler:** pausing releases the upscaling models (measured: 16.6 GB
  returned on an RTX 3090). The first image after Resume takes a little longer
  while they reload.
- **Tag & Rename now has a Pause at all**, which it never did. It shares one
  button with the old "Resume after error": it reads **Pause** while tagging,
  **Resume** while paused, and **Resume after error** when a run is held because
  the vision model kept failing. Pausing unloads the vision model.
- A pause frees **every** loaded model, including the small auto-straighten one.
  No exceptions to remember.
- Remote runs are unchanged: those models live on the rented pod, so unloading
  them would free nothing on your PC.

### Hover help on every control
Every button, checkbox, picklist and list on all six tabs now explains itself on
hover. The wording avoids jargon, and anything that costs money or changes files
says so plainly: Conciliation's Delete cannot be undone, a RunPod volume keeps
billing monthly even when idle, the Video Upscaler's Stop abandons the segment in
progress. Settings' numeric boxes also state their recommended value.

Buttons that open a window you settle into and work in (Segments…, Benchmark
GPU…, Provision…, the setup wizard) are drawn in bold, to set them apart from
buttons that act where you are.

### Fixes
- The Tag & Rename remote checkbox showed two tooltips stacked on top of each
  other.
- Settings claimed the Video Upscaler runs only on a rented pod. That stopped
  being true in 0.5.0, when local video upscaling arrived, and the claim sat
  directly above the Local/Remote switch.
- Video Upscaler: the progress bar now advances within a long segment on a local
  run, instead of appearing stuck until the segment finished.

### Also in this release
- **Run exclusivity:** while any run is active, the other tabs are locked, so two
  runs can no longer fight over the same GPU or the same folders.
- **Video notifications** carry a per-file summary of what finished, rather than
  a bare "done".

<div align="right"><a href="#changelog">↑ Back to top</a></div>

## 0.5.1

### Benchmark sharing (feature #8, NEW)
Turns the per-card video benchmark into a **crowdsourced dataset**, so a GPU someone
else already measured is not re-swept locally (a sweep is slow and, on a rented pod,
billed). Zero infrastructure: a curated `docs/video-benchmarks.csv` lives in the repo,
the app pulls it anonymously from GitHub, and contributions are delegated to the user's
own GitHub account in the browser (no upload endpoint, no token, no backend).

- **Automatic updates.** The community dataset is pulled and merged in the background
  at every launch (silent, fail-safe, offline falls back to the shipped copy). No
  button, no prompt. Your own measured results always take precedence over downloaded
  ones, which stay advisory (the batch sizer self-corrects a slightly-wrong ceiling).
- **Contribute my results…** (Benchmark GPU window) opens a pre-filled GitHub issue
  with your data. A **multi-select** card picker lets you submit **several GPUs at
  once** in one issue. Two filters keep it clean: only cells you actually measured are
  offered (never other people's downloaded data), and only rows not already in the
  published set are submitted (so benchmarking a little more each day sends only the
  new rows).
- **Export…** saves your results to a CSV file.
- **Maintainer tool** `bench_share.py --merge` curates submissions (dedupe + a
  physical-plausibility sanity gate) into the committed master CSV.

### Video conciliation (feature #5)
Conciliation now handles **videos** as well as images: it matches upscaled video
outputs back into the source tree and archives or replaces the originals, exactly as
it does for photos. Videos are matched by content-hash lineage ONLY (a partial clip
can never be mistaken for a whole-video match); the "never touch originals /
archive-first" guarantees carry over unchanged.

### Benchmark GPU window
- **Benchmark both torch.compile modes in one run:** a "Torch Compile" ON/OFF column
  fronts the results table, and an "Also use Torch Compile" checkbox sweeps the
  compiled and uncompiled regimes back to back (stored under separate keys, so AUTO
  reads whichever matches the real run).
- **Filter + column-sort** for the results table (same UX as the Video Upscaler
  lists): a Torch Compile / Target filter bar, and click any header to sort.
- The main window is now **fully hidden** while the Benchmark window is open (was
  minimized, which a dialog could restore, leaving it reachable behind the benchmark).
- All controls (Start, Stop, Export, Contribute, Report an issue, Close) now share a
  **single button row**.
- The window's **first-ever default size matches the main window** (980x720); the
  remembered size/position still wins after that.

### Video Upscaler
- **Cross-install "already upscaled" detection:** the scan is destination-reconciled,
  adopting outputs that exist in the shared destination folder but are absent from
  this install's local cache. A second machine sharing the same source + destination
  no longer offers to redo videos another install already produced.

### Installer / packaging
- **One application icon everywhere** (`app.ico`): the setup executable, the
  uninstall entry, the Start-menu and desktop shortcuts, and the running window all
  use the same icon (previously the shortcuts used a different icon).
- Ships the seeded `docs/video-benchmarks.csv` so a fresh install has the community
  benchmark dataset offline.

<div align="right"><a href="#changelog">↑ Back to top</a></div>
