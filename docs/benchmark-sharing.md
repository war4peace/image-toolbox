# Benchmark sharing (feature #8)

As-built notes for the crowdsourced video-benchmark dataset (shipped 0.5.1). Turns the
per-card Video Upscaler benchmark into a **shared corpus**: the app **downloads** a
curated community set so a card someone else already measured is not re-swept locally,
and lets a user **contribute** their own measured cards back. The goal is to stop
duplicating slow (and, on a rented pod, billed) sweeps across machines.

Scope: `db.video_bench` only (the per-probe video ceilings). The image-task
`docs/Benchmarks.csv` stays a separate, author-maintained file.

## Distribution model (zero-infrastructure)

One curated CSV (`docs/video-benchmarks.csv`) lives in the GitHub repo. The app pulls it
anonymously (the same GitHub / `net_ssl` path `updater.py` uses), and contribution is
**browser-delegated** to the user's existing GitHub account. No upload endpoint, no OAuth
app, no stored token, no backend.

Considered and rejected: a stored Personal Access Token is a credential burden and a
security liability for the wizard's non-technical audience; an OAuth app / GitHub App
needs a client secret and a backend, i.e. exactly the infrastructure this avoids. Reading
public data needs no account; writing always needs auth as *some* account, so the write
path is delegated to the browser where the user is already signed in, instead of teaching
the app to authenticate.

## The shared shape

The exported rows are the **summary table the Benchmark GPU window already shows**: one
row per (target x `torch.compile` mode x tiling regime), not the raw per-batch probes.
That is what a human reads and curates, and it carries everything the machine consumers
need (the ceiling batch, the chosen batch, s/frame and peak VRAM). `build_share_rows`
reuses the same `throughput_optimal_batch` / `saved_metrics` the window renders, so shared
numbers match the UI.

## Download (automatic, no button)

Refresh is automatic and silent. At every launch, `App._startup_bench_sync` runs
`video_benchmark.auto_update` on a background thread:

1. anonymous GET of the curated `docs/video-benchmarks.csv` from
   `raw.githubusercontent.com/<repo>/main/...` (via `net_ssl.ssl_context()`), parsed and
   imported;
2. if the network yields nothing (offline / a failed fetch), fall back to the shipped
   `{app}/docs/video-benchmarks.csv` (a `[Files]` line ships it next to `Benchmarks.csv`,
   so a fresh install has the corpus offline).

It is fail-safe (never raises into the GUI) and shows no "data updated" prompt. Because
the refresh is automatic there is **no Download / Import button** (an early design had
them; auto-refresh made them redundant). The `--import-csv PATH|community` headless flag
remains for manual or scripted import.

### Import target and local precedence

Import seeds `video_bench` (so the Benchmark window shows the card as characterised and
`max_feasible_output_mp` benefits) AND `video_batch_learn` (so a real AUTO run uses the
shared ceiling and self-corrects via the OOM back-off). Each imported cell gets a synthetic
ceiling `ok` probe plus the learned/used batch; the sub-ceiling and OOM-boundary probes
stay local resume state, not worth sharing.

**Local precedence is the one real correctness rule:** an import must NEVER clobber a probe
the user measured locally. So:

- `video_bench` has a `source` column (`'local'` default via the `_ensure_video_columns`
  migration, so pre-existing rows are correctly ground truth; live sweeps write `'local'`
  via `record_bench_probe`).
- `db.import_bench_rows` upserts with local precedence: it skips any cell where a
  `source='local'` row already exists (imported-over-imported is fine), and forces
  `source='imported'` on ingest so an exported "local" cannot masquerade as local truth on
  another machine.
- Imported rows stay **advisory** regardless: the VRAM sizer's OOM back-off and the
  degraded-GPU watchdog are the safety net, so a slightly wrong import self-corrects on the
  first run rather than failing hard.

`gpu_perf` (the time/cost estimate rate) is deliberately NOT seeded from imports: it is an
accumulating store, so injecting an imported rate risks polluting the user's own measured
average, and the estimator already falls back to the author `RATES` table for an unmeasured
card. Seeding it from imports (with its own precedence) is a possible follow-on.

## Contribute

**Contribute my results…** (Benchmark GPU window) opens a **pre-filled GitHub new-issue**
form (`.../issues/new?labels=benchmark&title=...&body=...`) via
`gui.common.contribute_benchmark`. The user reviews and submits with their own account. If
the CSV fits the URL cap (~8 KB) it is inlined in the issue body as a fenced block (near
one click); otherwise the body points at the file the app wrote to `logs/` and asks the
user to drag it in (a URL cannot pre-attach a file). **Export…** is the offline equivalent:
it saves the same CSV to a chosen path.

**Multi-GPU:** a **multi-select** card picker (`db.bench_gpu_ids`) lists every card with
contributable data on disk, not just the one the window opened on, so an out-of-stock
remote card's past results are still contributable. It is skipped when only one card has
contributable rows; otherwise it pre-selects ALL cards (with Select all / Clear helpers) so
several GPUs go into ONE submission (each CSV row is keyed by `gpu_id`), removing the old
one-issue-per-GPU friction.

**Two filters keep a contribution honest** (submission happens in the browser, so the app
can't confirm an issue was actually created, and a user benchmarks a bit more each day):

- *Only cells you measured.* `build_share_rows` filters `video_bench` probes to
  `source='local'` (a local GPU run OR the user's own rented-pod sweep both record
  `'local'`), so community data pulled in by the startup auto-refresh (`'imported'`) is
  never contributed back as if it were the user's. A card with only imported cells offers
  nothing and is hidden from the picker.
- *Only rows not already published.* Contribute fetches the live community master and drops
  every candidate row whose measurement identity is already there (`bench_share.new_rows`,
  keyed on the measurement columns, ignoring the volatile date / price / free_vram). So
  partial-benchmark, submit, benchmark the rest, submit again sends only the second batch. A
  re-measured cell with a DIFFERENT ceiling/spf counts as new and is kept. If nothing is new
  the user is told so and no issue opens. A residual duplicate window remains (manual
  curation lags between submit and merge), which the maintainer `--merge` dedups; the fetch
  is fail-safe (offline sends everything, and the merge still dedups).

**Remote is compile-ON only.** `build_share_rows` drops compile-OFF regimes for a
`run_on=remote` card: a rented pod always runs `torch.compile` on, so its OFF rows are not
representative (and old remote `7b` rows are mislabelled compile state anyway, see
`sizer.compile_tag`'s KNOWN WART). Local keeps both modes. For a card that is NOT the
window's own (picked from the chooser), `run_on` is inferred: it matches the machine's
detected card name means `local`, else `remote` (`video_benchmark.infer_run_on`).

## CSV format

A `# imgtbx-bench v1` sentinel is the first line (it guards against a future column add
silently misaligning an old file on import). Then a header row, then the data. Columns:

- `gpu_id`: the card key (nvidia-smi name for a local card, the RunPod id for a pod).
- `run_on`: `local` | `remote`, where the GPU ran. A remote pod is a clean dedicated card;
  a local card may share VRAM with the desktop, so this is real context (and a results
  filter). Distinct from `source`; named to match the app's "Run on" switch.
- `model`: model family / regime tag (`7b`, `3b`, `3b_fp16`, ...).
- `compile`: `torch.compile` `ON` | `OFF`. Its own column (matching the window's Torch
  Compile column) even though internally it folds into `bench_key`.
- `tile`: VAE-tiling regime (`off` | `d1024` | `e1024_d512` ...): a tiled ceiling is not
  interchangeable with an untiled one, so it must disambiguate a row.
- `target`: the human label (`1080p` / `1440p` / `4K` / `1280x960`); `out_w`/`out_h` are
  authoritative.
- `out_w`, `out_h`: the real output dimensions (the box-fit key the sizer/estimator match
  on).
- `max_batch`: the measured ceiling (largest `ok` batch on this cell).
- `used_batch`: the AUTO/throughput-optimal batch actually chosen (at or below the ceiling).
- `overlap`: the segment overlap for that batch (derived via `sizer.auto_overlap`).
- `spf`: seconds per frame at the used batch.
- `peak_vram`: peak allocated VRAM (GB) at the used batch.
- `free_vram`: FREE VRAM (GB) at probe time (a contended card measures a lower ceiling, so
  this keeps an import from being mistaken for a clean-card measurement).
- `price_usd_hr`: the RunPod $/h stamped at export/contribute time from the picker's live
  price, blank for `run_on=local`. Stored as the raw hourly rate, NOT a derived cost (the
  useful metric, cost per frame = `spf x price/h / 3600`, is computed from this plus `spf`).
  A **snapshot** only (RunPod pricing moves with time, region/DC and stock): a future
  fastest-OR-cheapest recommender must rank on the LIVE price at decision time and treat
  this as a historical anchor. Pair with `date` for staleness.
- `source`: `local` | `imported`, data provenance, drives the local-precedence rule (forced
  to `imported` on ingest). Do not confuse with `run_on` (who measured it vs where the GPU
  ran).
- `date`: the benchmark date (`updated_at`).

The reader (`bench_share.read_csv`) is tolerant: it skips the sentinel and any `#` comment
lines, ignores unknown columns, defaults missing ones blank, and skips a row missing a
required field (`gpu_id, model, out_w, out_h, max_batch`). It never raises: a bad download
or attachment returns `[]`.

## Maintainer merge tool (`bench_share.py --merge`)

Curation is a maintainer-only step, NOT shipped to users and NOT a hand-edit of the CSV. It
ingests accepted submissions into a private, gitignored working SQLite DB (`benchmarks.db`),
dedupes them, runs a physical-plausibility gate, and re-exports the curated master
`docs/video-benchmarks.csv`. Only that CSV is committed, so every merge is reviewable as a
git text diff.

**Workflow, end to end:**

1. A user contributes via **Contribute my results…**, which opens a pre-filled GitHub issue
   (label `benchmark`) with the CSV inlined in a fenced block, or (if too large) attached.
2. Review the issue. Save the CSV locally (copy the fenced block into a `.csv`, or download
   the attachment). Keep the incoming files in one folder, e.g. `submissions/`.
3. From the repo root, merge them:

   ```
   python scripts/bench_share.py --merge submissions/*.csv
   ```

   This upserts every accepted row into `benchmarks.db` (created on first run) and rewrites
   `docs/video-benchmarks.csv` from the whole DB. The tool expands the glob itself, so it
   also works in PowerShell (which does not expand `*.csv` for a native command).
4. Read the printed report (per file: accepted / rejected counts, and the reason for each
   rejection). Investigate anything rejected before accepting the issue.
5. `git diff docs/video-benchmarks.csv` to review the added/changed rows, then commit and
   push. The app serves this file from `raw.githubusercontent.com/.../main/...`.

**Flags:**

- `--merge CSV [CSV ...]` (required): one or more submitted CSVs (globs accepted).
- `--db PATH` (default `benchmarks.db`): the private working accumulator. It PERSISTS across
  merge sessions, so you never re-feed old submissions; each run only adds the new ones. It
  is gitignored; back it up separately if you value the history.
- `--master PATH` (default `docs/video-benchmarks.csv`): the curated CSV to regenerate. The
  whole DB is exported every run (not appended), deterministically sorted (gpu_id, run_on,
  model, compile, tile, out_w, out_h) so diffs stay minimal.

**Dedupe:** a cell is identified by `(gpu_id, run_on, model, compile, tile, out_w, out_h)`.
When two submissions describe the same cell the NEWEST `date` wins (ties: the later-merged
one), so a re-benchmark on newer drivers supersedes the old figure.

**Sanity gate (`sanity_check`):** the moderation step that keeps one over-optimistic row
from poisoning every user's estimator. A row is rejected (with a reason) when it is
physically impossible: an out-of-range or non-integer `max_batch` (past the ~3000 sweep
cap), a `used_batch` above its own ceiling, an implausible `spf` (below 1 ms or above 1 h
per frame), an output edge past 8K, a negative overlap, or a `peak_vram` above the card's
VRAM. Card capacity comes from a substring table (`_CARD_VRAM`, e.g. A100-80 = 80 GB,
3090 = 24 GB) with a 5% slack; an unrecognised card falls back to a generous 400 GB absolute
ceiling. To teach it a new card, add a `(substring, gb)` entry to `_CARD_VRAM`. The gate is
deliberately lenient (it only catches the clearly impossible) because you still eyeball the
git diff. Note: structurally broken rows are dropped by `read_csv` before the gate, so
"parsed" in the report can be lower than the file's line count.

## Why

A per-card sweep is slow and, on a pod, costs money; a shared corpus means a card someone
has already characterised is not re-measured on every machine. It also seeds the estimator
(`video_estimate.recommend_gpus`) for cards the user has never run, and (with `spf` +
`price_usd_hr` per card) is the data a future **fastest-OR-cheapest GPU recommender** would
rank on: throughput from the corpus, cost from the LIVE price at decision time.

## Code map

- `scripts/bench_share.py`: CSV serializer (`write_csv` / `read_csv` / `to_text`),
  `fetch_community`, `new_rows` (contribution dedup), and the maintainer `--merge` tool
  (sanity gate + `benchmarks.db` accumulator). Torch-free, stdlib, fail-safe.
- `scripts/video_benchmark.py`: `build_share_rows`, `export_share_csv`, `import_rows` /
  `import_share_csv` / `import_community`, `auto_update`, `infer_run_on`, and the
  `_compose_bench_model` / `_decompose_bench_model` key helpers; `--export-csv` /
  `--import-csv` flags.
- `scripts/db.py`: the `source` column migration, `import_bench_rows` (local precedence),
  `bench_gpu_ids` / `bench_models`, and `get_bench_probes` returning `source`.
- `scripts/gui/common.py`: `contribute_benchmark` (browser-delegated issue).
- `scripts/gui/video_benchmark.py`: the Export / Contribute buttons and multi-select picker.
- `scripts/gui/app.py`: `_startup_bench_sync` (background auto-refresh at launch).
- `docs/video-benchmarks.csv`: the curated master (committed; shipped to `{app}/docs`).
