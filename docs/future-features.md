# Future Features

Candidate features that are **not yet implemented**, sorted by difficulty
(easiest first), with a feasibility assessment for each. See "Sequencing &
dependencies" for the threads that drive ordering, and "Decided against /
constraints" at the bottom for ideas investigated and dropped.

The remaining open milestones are both lower priority: two that each introduce a new
process model, networking, or packaging (HTTP interface #3, Unraid #4).

**Shipped milestones (kept only as a numbering legend).** Roadmap **#1, #2, #5, #6
and #7** are done and live; they are no longer described here (their design of record
lives in `CLAUDE.md`, `docs/runpod-notes.md`, `docs/video-upscaler.md` and
`docs/local-video-upscaler.md`). The numbers survive only because code and other
docs cite the roadmap by them (`remote #1`, `Video Upscaler #2`, `local #7`):

- **#1 — Remote upscaling (RunPod).** Shipped 0.3.1–0.4.2. See `CLAUDE.md` +
  `docs/runpod-notes.md`.
- **#2 — Video upscaling (RunPod-only, experimental).** Shipped. See
  `docs/video-upscaler.md`.
- **#5 — Video conciliation.** Shipped 0.5.1-experimental: Conciliation now
  matches and replaces VIDEO originals with their upscaled outputs, alongside
  images, in one scan. Videos match by the content-hash `lineage` the Video
  Upscaler records on completion (item 10) ONLY — no name fallback, so a partial
  clip (which records no lineage) can never be mistaken for a whole-video match;
  a video is acted on only when its output is present in the chosen processed
  tree. See `CLAUDE.md` (Conciliation) and `conciliate.py`.
- **#6 — Self-healing remote runs (auto-recover a lost pod).** Shipped 0.5.0
  (video only): an opt-in "Auto-resume" supervisor reconnects a blipped pod, or
  waits unbounded for the identical card and redeploys it, continuing from the
  first unfinished segment. Funds guard / user Stop / completed queue are the only
  non-redeploy stops. See `docs/video-upscaler.md` section 17.
- **#7 — Local video upscaling (free-and-slow alternative to remote).** Shipped
  0.5.0: the same SeedVR2 video work runs in-process on the user's own GPU via
  `LocalVideoEngine`, with a predictive VRAM sizer, a one-click per-card benchmark,
  and optional `torch.compile`. See `docs/local-video-upscaler.md`.

---

## 8. Benchmark sharing (community download / contribute) — Easy
Turn the per-card video benchmark into a **crowdsourced dataset**: users
**download** a curated community corpus so a card someone else already measured
does not have to be re-swept locally, and **contribute** their own measured card
back. The goal is to stop duplicating slow (and, on a rented pod, billed) sweeps
across machines. Covers `db.video_bench` only (the per-probe video ceilings); the
image-task `docs/Benchmarks.csv` stays a separate, author-maintained file.

**Status (0.5.1-experimental): BOTH halves are built.** The contribute half is
proven (real submissions sent + curated). Shipped:
- The serializer `scripts/bench_share.py` (`write_csv` / `read_csv` / `to_text`),
  torch-free and fail-safe, with the `# imgtbx-bench v1` sentinel.
- `video_benchmark.build_share_rows` (summary rows from real probes, reusing the
  same `throughput_optimal_batch` / `saved_metrics` the window shows so shared
  numbers match the UI), `export_share_csv`, `infer_run_on`, and the
  `--export-csv` headless flag; the remote-compile-OFF exclusion.
- The Benchmark GPU window's **Export…** and **Contribute my results…** buttons,
  the card picker over `db.bench_gpu_ids` (hides cards with zero contributable
  rows, shows each card's row count), and `gui.common.contribute_benchmark` (the
  browser-delegated pre-filled GitHub issue, inline-CSV or attach fallback).
- The **maintainer merge tool** `bench_share.py --merge` (dedupe + sanity gate +
  curated-master export). See "Maintainer merge tool" at the end of this section.
- **Download half:** the `source` column migration on `video_bench`
  (`_ensure_video_columns`, existing rows default `'local'`; `record_bench_probe`
  now stamps `'local'`), `db.import_bench_rows` (local-precedence, writes synthetic
  `ok` probes + the sizer's learned batch, tagged `'imported'`),
  `bench_share.fetch_community` (anonymous GitHub GET via `net_ssl`),
  `video_benchmark.import_rows` / `import_share_csv` / `import_community` (reconstruct
  the regime-tagged `model` + learned key from the CSV's model+compile+tile), the
  `--import-csv PATH|community` headless flag, the Benchmark window's **Download
  community…** and **Import file…** buttons, the seeded `docs/video-benchmarks.csv`
  + its installer `[Files]` line.

**Import scope (deliberate):** import seeds `video_bench` (so the window shows the
card as characterised and `max_feasible_output_mp` benefits) AND `video_batch_learn`
(so a real AUTO run uses the shared ceiling and self-corrects via the OOM back-off).
It does NOT seed `gpu_perf` (the time/cost estimate rate): that is an ACCUMULATING
store, so injecting an imported rate risks polluting the user's own measured average,
and the estimator already falls back to the author `RATES` table for an unmeasured
card. Seeding `gpu_perf` from imports (with its own precedence) is a possible
follow-on if imported time estimates prove worth it.

The exported shape is the **summary table the Benchmark GPU window already shows**:
one row per (target x `torch.compile` mode), not the raw per-batch probe rows. That
is what a human reads and curates, and it still carries everything the machine
consumers need (the ceiling batch, the chosen batch, s/frame and peak VRAM). Import
seeds `video_bench` with a synthetic ceiling `ok` probe and the learned/used batch
per cell; the sub-ceiling and OOM-boundary probes stay local resume state, not
worth sharing.

The distribution model is deliberately **zero-infrastructure**: one curated CSV
lives in the GitHub repo, the app pulls it anonymously (the same GitHub/`net_ssl`
path `updater.py` already uses), and contribution is **browser-delegated** to an
existing GitHub account. No upload endpoint, no OAuth app, no stored token, no
backend. (Considered and rejected: a stored Personal Access Token is a credential
burden and a security liability for the wizard's non-technical audience; an OAuth
app / GitHub App needs a client secret and a backend, i.e. exactly the
infrastructure this avoids. Reading public data needs no account; writing always
needs auth as *some* account, so the write path is delegated to the browser where
the user is already signed in, instead of teaching the app to authenticate.)

- **What it is (four entry points on `gui/video_benchmark.py`):**
  - **Download community benchmarks** — anonymous GET of a curated
    `docs/video-benchmarks.csv` from `raw.githubusercontent.com/<repo>/main/...`
    (via `net_ssl.ssl_context()`), parsed and imported. The file also ships in the
    installer to `{app}/docs` (a `[Files]` line next to the existing
    `Benchmarks.csv` one), so a fresh install has the corpus offline and the
    download just refreshes it.
  - **Contribute my results… / Export…** — a **card picker** (`db.bench_gpu_ids`)
    lists EVERY card with benchmark data on disk, not just the one the window opened
    on, so an out-of-stock remote card's past results are still contributable (the
    window can only *open* on an in-stock pick; the data is keyed by card in
    `video_bench` regardless). The picker is skipped when only one card exists, and
    defaults to the window's card; it shows each card's contributable row count.
    Contribute exports the chosen card's rows to a CSV on disk, then opens the
    browser at a **pre-filled GitHub new-issue** form
    (`.../issues/new?labels=benchmark&title=...&body=...`). The user reviews and
    submits with their own account. If the CSV is small enough for the URL cap
    (~8 KB), it is inlined in the issue body as a fenced block (near one click);
    otherwise the body says "benchmark written to `<path>`, drag it into this issue"
    (a URL cannot pre-attach a file).
  - **Remote is compile-ON only.** `build_share_rows` DROPS compile-OFF regimes for a
    `run_on=remote` card: a rented pod always runs `torch.compile` on, so its OFF
    rows are not representative (and old remote `7b` rows are mislabelled compile
    state anyway, see `sizer.compile_tag`'s KNOWN WART). Local keeps both modes. For
    a card that is NOT the window's own (picked from the chooser), `run_on` is
    inferred: it matches the machine's detected card name -> `local`, else `remote`
    (`video_benchmark.infer_run_on`).
  - Curation is a **maintainer
    tool**, not a hand-edit: `bench_share.py --merge <submitted-csvs...>` ingests
    accepted submissions into the maintainer's private working `benchmarks.db`,
    dedupes, runs **sanity checks** (reject a physically impossible ceiling, an spf
    that cannot be right, a peak VRAM above the card's capacity), then exports the
    curated master back to `docs/video-benchmarks.csv`. The working DB stays private
    (db ergonomics + outlier-catching for the maintainer); only the diffable CSV is
    committed, so a submission's added rows are reviewable as a git text diff. This
    sanity-check step is the **moderation gate** that keeps one over-optimistic
    ceiling from poisoning every user's estimator.
  - **Export… / Import file…** — the local-file path as a secondary offline option
    (save a CSV, load a CSV), reusing the same serializer/importer.
- **Why:** a per-card sweep is slow and, on a pod, costs money; a shared corpus
  means a card someone has already characterised is not re-measured on every
  machine. It also seeds the estimator (`video_estimate.recommend_gpus`) for cards
  the user has never run, and (with `spf` + `price_usd_hr` per card) is the data a
  future **fastest-OR-cheapest GPU recommender** would rank on: throughput from the
  corpus, cost from the LIVE price at decision time.
- **Local precedence (the one real correctness rule):** an import must **never**
  clobber a probe the user measured locally. The current `record_bench_probe`
  upsert is newest-wins (`ON CONFLICT ... DO UPDATE`), which would let a downloaded
  row overwrite local ground truth (and an over-optimistic imported ceiling OOMs
  the first real run). So:
  - add a `source` column to `video_bench` (`'local'` default via the
    `_ensure_video_columns` migration, so existing rows are correctly ground
    truth; live sweeps write `'local'`);
  - a new `db.import_bench_rows` upserts with **local precedence**: skip any cell
    (keyed `gpu_id, model, compile, out_w, out_h`) where a `source='local'` row
    already exists; imported-over-imported is fine. Ingest forces
    `source='imported'` so an exported "local" cannot masquerade as local truth on
    another machine.
  - Imported rows stay **advisory** regardless: the VRAM sizer's OOM back-off and
    the degraded-GPU watchdog remain the safety net, so a slightly wrong import
    self-corrects on the first run rather than failing hard.
- **CSV columns** (after a `# imgtbx-bench v1` sentinel first line, which guards
  against a future column add silently misaligning an old file on import):
  - `gpu_id` — the card key (nvidia-smi name for a local card, the RunPod id for a
    pod).
  - `run_on` — `local` | `remote`: where the GPU ran. A remote pod is a clean,
    dedicated card; a local card may share VRAM with the desktop, so this is real
    context for a reader (and a natural results filter). Distinct from `source`
    below; named to match the app's "Run on" switch.
  - `model` — model family / regime tag (`7b`, `3b`, `3b_fp16`, ...).
  - `compile` — `torch.compile` `ON` | `OFF`. Surfaced as its own column (matching
    the window's Torch Compile column) even though internally it is folded into
    `bench_key`. Local-only signal: a remote row carries the pod's mode (or `n/a`).
  - `target` — the human label (`1080p` / `1440p` / `4K`), a convenience over ...
  - `out_w`, `out_h` — the real output dimensions (the per-video box-fit key the
    sizer/estimator actually match on; `target` is just the readable name).
  - `max_batch` — the measured ceiling (largest `ok` batch on this cell).
  - `used_batch` — the AUTO/throughput-optimal batch actually chosen (at or below
    the ceiling, with the safety margin).
  - `overlap` — the segment overlap for that batch (derived via
    `sizer.auto_overlap`).
  - `spf` — seconds per frame at the used batch.
  - `peak_vram` — peak VRAM (GB) at the used batch.
  - `free_vram` — FREE VRAM (GB) at probe time: a contended card measures a lower
    ceiling, so this keeps an import from being mistaken for a clean-card
    measurement.
  - `price_usd_hr` — the RunPod $/h stamped at export/contribute time from the
    benchmark window's picker (its live `available_gpus` price), blank for
    `run_on=local`. (Stamped at export, not recorded per-probe: for the common fresh
    benchmark-then-contribute flow these coincide; a re-export of an old card carries
    the current price, which is fine since this is advisory anyway.) Stored as the raw
    hourly rate, NOT
    a derived cost: the useful metric (cost per frame / per output-minute =
    `spf x price/h / 3600`) is computed from this plus `spf`. This is a **snapshot**
    only (RunPod pricing moves with time, region/DC and stock): a future
    fastest-OR-cheapest recommender MUST rank on the LIVE price at decision time and
    treat this column as a historical anchor / fallback for a card no longer
    live-listed. Pair it with `date` to know how stale it is; note it is also
    region-specific (all pods run on Secure Cloud, but DC prices differ), so a
    region column is a natural follow-on if cross-region cost comparison ever
    matters.
  - `source` — `local` | `imported`: data **provenance**, drives the
    local-precedence rule below (forced to `imported` on ingest). Do not confuse
    with `run_on` (the two "local"s mean different things: who measured it vs where
    the GPU ran).
  - `date` — the benchmark date (`updated_at`).
- **Reuse:** the data is already flat/tabular (a summary row keyed
  `gpu_id, model, compile, out_w, out_h`); `benchmarks.py`'s stdlib `csv` reader and
  the `updater.py` GitHub-fetch pattern are both established. Serializer/fetcher
  live in a new torch-free, fail-safe `scripts/bench_share.py` (`write_csv` /
  `read_csv` / `fetch_community`, plus the maintainer-side `--merge` that dedupes +
  sanity-checks submissions into the curated master CSV).
- **Work needed:** DONE (feature complete, experimental): the serializer,
  `build_share_rows` + `--export-csv`, the Export/Contribute buttons + card picker,
  `contribute_benchmark`, the maintainer `--merge`, and the whole download half
  (`fetch_community`, `import_bench_rows` + the `source` migration, the
  Download/Import buttons, `--import-csv`, the seeded master + its installer line).
  Possible follow-on: seed `gpu_perf` from imports so shared TIME estimates apply
  to unmeasured cards (see "Import scope" above).
- **Risks:** low. Fail-safe on a malformed CSV or a failed download (skip bad
  rows, return None, never raise into the GUI); local precedence protects measured
  data; it only writes cache rows the user can re-measure.

### Maintainer merge tool (`bench_share.py --merge`)

Curation is a maintainer-only step, NOT shipped to users and NOT a hand-edit of the
CSV. It ingests accepted submissions into a private, gitignored working SQLite DB
(`benchmarks.db`), dedupes them, runs a physical-plausibility gate, and re-exports
the curated master `docs/video-benchmarks.csv`. Only that CSV is committed, so every
merge is reviewable as a git text diff.

**Workflow, end to end:**
1. A user contributes via the Benchmark GPU window's **Contribute my results…**
   button, which opens a pre-filled GitHub issue (label `benchmark`) with the CSV
   inlined in a fenced block, or (if too large for the URL) attached as a file.
2. Review the issue. Save the CSV locally (copy the fenced block into a `.csv`, or
   download the attachment). Keep the incoming files in one folder, e.g.
   `submissions/`.
3. From the repo root, merge them:

   ```
   python scripts/bench_share.py --merge submissions/*.csv
   ```

   This upserts every accepted row into `benchmarks.db` (created on first run) and
   rewrites `docs/video-benchmarks.csv` from the whole DB.
4. Read the printed report (per file: accepted / rejected counts, and the reason for
   each rejection). Investigate anything rejected before accepting the issue.
5. `git diff docs/video-benchmarks.csv` to review the added/changed rows, then commit
   and push. The Download side (once built) serves this file from `raw.githubusercontent.com`.

**Flags:**
- `--merge CSV [CSV ...]` (required): one or more submitted CSVs. Globs are expanded
  by the shell (`submissions/*.csv`).
- `--db PATH` (default `benchmarks.db`): the private working accumulator. It PERSISTS
  across merge sessions, so you never re-feed old submissions; each run only adds the
  new ones. It is gitignored; back it up separately if you value the history.
- `--master PATH` (default `docs/video-benchmarks.csv`): the curated CSV to
  regenerate. The whole DB is exported every run (not appended), deterministically
  sorted (gpu_id, run_on, model, compile, tile, out_w, out_h) so diffs stay minimal.

**Dedupe:** a cell is identified by `(gpu_id, run_on, model, compile, tile, out_w,
out_h)`. When two submissions describe the same cell the NEWEST `date` wins (ties: the
later-merged one), so a re-benchmark on newer drivers supersedes the old figure.

**Sanity gate (`sanity_check`):** the moderation step that keeps one over-optimistic
row from poisoning every user's estimator. A row is rejected (with a reason) when it
is physically impossible: an out-of-range or non-integer `max_batch` (past the ~3000
sweep cap), a `used_batch` above its own ceiling, an implausible `spf` (below 1 ms or
above 1 h per frame), an output edge past 8K, a negative overlap, or a `peak_vram`
above the card's VRAM. Card capacity comes from a substring table in `bench_share.py`
(`_CARD_VRAM`, e.g. A100-80 = 80 GB, 3090 = 24 GB) with a 5% slack; an unrecognised
card falls back to a generous 400 GB absolute ceiling. To teach it a new card, add a
`(substring, gb)` entry to `_CARD_VRAM`. The gate is deliberately lenient (it only
catches the clearly impossible) because you still eyeball the git diff.

**Note:** structurally broken rows (missing a required field, unparseable) are dropped
silently by `read_csv` before the gate sees them, so "parsed" in the report can be
lower than the file's line count; the gate's accepted/rejected counts are over the
parsed rows.

## 3. HTTP interface — Hard (low priority)
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

## 4. Unraid Community Apps integration — Hardest (low priority)
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

- **#1, #2, #5, #6 and #7 are complete** (remote upscaling + funds-floor; RunPod
  video; video conciliation; self-healing remote runs; local video), so the
  remaining sequencing is only among the low-priority open milestones below.
- **#3 and #4 are much lower priority** — large, mostly independent milestones.
  With Home Assistant already done over MQTT, the old telemetry coupling no longer
  drives sequencing.
- **#4 depends on #3** (headless Unraid needs a web UI).
- **Follow-ons from the shipped #6/#7 (not yet scheduled):** generalise the
  Auto-resume supervisor from video to the image runners (batch upscale / tag); and
  #7's deferred Phase 2 — a non-SeedVR fixed-ratio 2x/4x engine (Real-ESRGAN-class:
  fast, low-VRAM, deterministic) dropping into the same engine seam.
- **Architectural watch-item:** the app is dependency-light and Windows-only. #3
  and #4 each push toward extra packages, a long-running server, and
  cross-platform support, so adopt those deliberately.

---

## Decided against / constraints

- **Region pre-seed at first-run bootstrap — dropped.** The idea was to ask the
  user's region during install and pre-seed `data_center_ids`. After repeatedly
  checking the live list, there are so few regions/data centers that auto-detecting
  one adds little: the Settings Region/DC picker already lets the user pick
  directly, which is clearer than guessing for them.
- **AMD GPUs (ROCm) — not supported, filtered out.** The pipeline is CUDA-only
  (PyTorch CUDA build, SeedVR2, the orientation CNN, `nvidia-smi` telemetry), so an
  AMD card can't run any task. RunPod occasionally lists AMD Instinct cards (e.g.
  the MI300X in EU-RO-1, sometimes *cheaper* than comparable NVIDIA), so
  `available_gpus` drops them at the source via `is_amd_gpu` (0.4.0) rather than
  letting a user pick one that fails at run time. A ROCm port would be a separate,
  large effort and is not planned.
- **vast.ai as a second provider — investigated 2026-06-23, not pursued.** The
  goal was provider choice (price/availability/region) behind a thin interface.
  Two billing dimensions RunPod doesn't charge make vast.ai a poor fit for this
  app's stream-one-image-at-a-time, disposable-pod design: **storage** is
  ~$0.33–0.40/GB/mo (RunPod $0.07), and **bandwidth is metered both ways** at
  ~$40/TB (RunPod free) — directly taxing the upload-every-image / download-every-
  result flow. It also has **no region-wide network volume** (host-local only),
  which defeats the availability gain that motivated the look. Reusable finding:
  the worker, streaming engine, dead-man's switch, and local queue/watchdog are
  provider-agnostic; a port would be a provider seam (`RunPodProvider` +
  `VastProvider`) plus a GUI selector, the GUI being the largest lift. Vet any
  future provider against this checklist before writing code: (a) free/cheap
  ingress+egress, (b) cheap region-wide persistent storage that mounts on
  disposable instances, (c) reliable SSH with key injection.
