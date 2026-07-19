# Changelog

User-facing release notes ship as the **annotated git tag message** (CI publishes
them as the GitHub Release body, and the in-app updater shows them). This file is the
working draft those notes are distilled from, and it records **experimental**,
in-development versions before they are tagged. For released versions, see the GitHub
Releases page.

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
