# Improvement recommendations (0.4.9-experimental review)

Reviewed: 2026-07-09, branch `0.4.9-experimental` at `426d34c` (the 0.4.8 release
commit; no new work on the branch yet). Test suite: 398 passed.

This document is a work order: each item is written so Claude (Opus 4.8) can
implement it directly. Items are ordered by urgency and importance: correctness
first, then money-safety and robustness, then security, efficiency, and QoL.
Context: the image-side runners (Batch Upscaler, Tag & Rename, Conciliation) went
through the 12-item 0.4.5 review and are mature; the findings below concentrate in
the newer Video Upscaler / remote path (0.4.4-0.4.8) where the code is younger.

House rules that apply to every item (from CLAUDE.md):
- Never touch source files. Fail safe: a helper failing must not kill a run.
- Dependency-light: stdlib only unless there is a deliberate reason.
- Avoid em-dashes in strings/comments/docs; match existing comment density.
- After each item: run `.venv\Scripts\python.exe -m pytest tests -q` and keep it
  green; add tests where an item says so.

---

## P1: Correctness / data-integrity bugs

### 1. [DONE] A reused (resumed) split is trusted blindly, so an interrupted split resumes incomplete

**Done (0.4.9-experimental).** `ensure_split` now writes a `split.done` marker
(segment count + frame sum + mode) after a completed split and, on reuse, validates
via the new pure `split_is_complete`: marker present, gapless `seg_NNNNN` indices,
count and counted-frame-sum still matching. Any mismatch logs why, clears the stale
segment files (`_clear_split_dir`), and re-splits. `tests/test_video_split_resume.py`
(7 tests). Suite 405 green.



**Where:** `scripts/batch_video_upscale.py`: `ensure_split` (~line 456) and
`_enumerate_segments` (~line 443); `scripts/video_pipeline.py`: `split` (~line 635).

**Problem.** `split()` lets ffmpeg's segment muxer write `seg_*.mkv` files directly
into `in_dir`. If the run dies mid-split (app closed, crash, power loss, the stall
watchdog killing ffmpeg), the dir holds a partial set of segments and the last file
is typically truncated. On the next run, `ensure_split` sees `_enumerate_segments`
return a non-empty list and reuses it as-is ("reused"), so the video is upscaled
from an incomplete/truncated segment set. The only backstop is the duration-drift
check at the very end, which merely marks the output "DONE (review)": the user pays
real pod money for a truncated deliverable flagged with a soft warning. Note that
`extract_clip` (video_pipeline.py ~line 717) already solved exactly this problem
class for clips with a `.part` + `os.replace` pattern and a comment explaining why;
the split path never got the same care.

**Fix.**
- In `ensure_split`, validate a reused split before returning it:
  (a) the filenames' numeric suffixes are gapless and match the enumeration index
  (`seg_00000`, `seg_00001`, ...), and
  (b) `sum(seg.frame_count) == info.nb_frames` within the same tolerance thinking
  `check_drift` uses (for a re-encode plan the count can legitimately differ from a
  VFR source's, so compare against what the plan implies; simplest robust rule:
  require the sum to be within `DRIFT_FRAME_TOL`-style tolerance of `info.nb_frames`
  for copy splits, and for re-encode splits require it within tolerance of
  `fps x duration`).
  On any mismatch: log why, delete the stale segment files, and fall through to a
  fresh `plan_split` + `split` (also `clear_video_segments` happens downstream
  already via the `len(recorded) != len(segs)` check, but clear it here explicitly).
- Belt-and-suspenders: have `split()` write a small `split.done` marker file (e.g.
  JSON with segment count + frame sum) into `in_dir` after a successful split, and
  have `ensure_split` refuse to reuse a dir without the marker. This catches the
  killed-mid-split case even when the frame math happens to pass.

**Tests.** Unit-test `ensure_split` reuse-validation with synthetic dirs (missing
marker, gap in indices, short frame sum -> re-split path taken; valid dir ->
reused). The frame-count helper can be monkeypatched; no ffmpeg needed.

### 2. [DONE] Every early-stop notification is labeled "paused (per-run cap)"

**Done (0.4.9-experimental).** New pure `_stop_notice(stopped)` next to `_failure_notice`
branches the title/color/resume-hint on the reason string: per-run cap (and cost cap)
keep "paused (per-run cap)", a user Stop is "Video upscale stopped", the work-root
refusal is "Video upscale did not start" (red, no resume hint), and any unforeseen
reason gets "stopped early" instead of being mislabeled a cap. `_notify_summary` uses
it (errors still outrank an early stop for the title) and the em-dash in the resume
tail was replaced with a comma. 8 tests in `tests/test_notifications_coverage.py`.
Suite 412 green.

**Where:** `scripts/batch_video_upscale.py`: `_notify_summary` (~lines 1224-1241).

**Problem.** `summary["stopped"]` can be a per-run cap message, "stopped by user",
or the work-root-conflict refusal from `run_queue`, but the notification title is
hard-coded `"Video upscale paused (per-run cap)"` for all of them. A user who
pressed Stop (or mis-configured the staging folder) gets a push notification
claiming a cost cap fired.

**Fix.** Branch the title/description on the stop reason: user stop ("Video
upscale stopped"), cap ("paused (per-run cap), re-run to continue"), and startup
refusal ("did not start"). Keep the reason text in the body as today. Extract the
title/color choice into a small pure helper next to `_failure_notice` and unit-test
it (mirror `test_video_pod_events` style).

### 3. [DONE] `RemoteSession.close()` can raise out of the runner's `finally`

**Done (0.4.9-experimental).** The `rp.ensure_stopped(...)` call in `close()` is now
wrapped in try/except like every other teardown step: on error it `_emit`s "Could not
stop the pod (<err>); the dead-man's switch will stop it on the idle timeout." and
`debug_log`s it (same pattern `_on_funds_trip` uses), so a teardown failure can no
longer replace the run's real outcome with a secondary traceback or skip `_close_log`.
New `tests/test_remote_close_guard.py` (3 tests: swallows a stop error, still emits the
normal message on success, leaves a reused pod running). Suite 415 green.

**Where:** `scripts/remote_run.py`: `close` (~lines 674-680), the
`rp.ensure_stopped(...)` call.

**Problem.** Every other step in `close()` is guarded, but the teardown call itself
is not: an API/network error in `ensure_stopped` propagates out of `close()`, which
every runner calls from a `finally` block (`batch_video_upscale.main` ~line 1346).
A raise there replaces the run's real outcome/return code, skips `_close_log()`,
and surfaces a confusing secondary traceback after a run that may have succeeded.
The on-pod dead-man's switch already guarantees the pod stops eventually, so a
failed teardown call is a log-worthy event, not a crash-worthy one.

**Fix.** Wrap the `ensure_stopped` call in try/except: `_emit` a clear line
("could not stop the pod (<err>); the dead-man's switch will stop it on the idle
timeout") and `debug_log` it. Same pattern `_on_funds_trip` already uses two
methods up.

---

## P2: Money-safety and robustness (remote video runs)

### 4. [DONE] Deterministically-failing jobs are re-attempted on every run, forever

**Done (0.4.9-experimental).** `video_outputs` gained a `fail_count` column (SCHEMA +
guarded `_ensure_video_columns` migration). Each failure calls the new atomic
`db.bump_video_fail_count`; the pure `_failure_disposition(fail_count, reason)` in the
runner marks the job `skipped` (leaves `get_video_queue`) with the reason prefixed
"gave up after N attempts:" once it reaches `GIVE_UP_AFTER` (3), else keeps it `failed`
for a normal retry. The runner logs the give-up clearly. GUI triage (Video tab): the
queue right-click gains "Retry (reset & re-queue)" + "Show failure reason" for a failed
job, and the scan-list right-click gains per-target "Retry" / "Show reason" for
failed/gave-up outputs (the place a skipped job stays visible), backed by
`db.reset_video_fail_count`; a scan row with a failed/gave-up output is flagged with a
muted-red "failedup" tag. Tests: `tests/test_video_failcount.py` (7: pure disposition,
DB bump/reset, queue exclusion, and the 3-run give-up path with `process_job`
monkeypatched). Suite 422 green.

**Where:** `scripts/db.py`: `get_video_queue` (~line 625) includes `'failed'`
status; `scripts/batch_video_upscale.py`: `run_queue` (~lines 1168-1202) only
dedupes attempts within one run (`attempted` set).

**Problem.** A job that fails for a permanent reason (black-output guard, corrupt
source, the downscale ValueError, an unreadable codec) stays `failed` in the queue
and is retried at the top of every subsequent run: re-probe, re-split (potentially
a full re-encode of a long video), and in some cases pod time, burned every run of
an installment-paid batch. `skip_reason` is recorded but the queue UI gives the
user no triage.

**Fix.**
- Add a `fail_count` column to `video_outputs` (extend `_ensure_video_columns`),
  incremented on each failure. After N consecutive failures (suggest N=3, constant
  in the runner), set status `'skipped'` with the last `skip_reason` prefixed
  "gave up after 3 attempts:" so `get_video_queue` stops returning it. Log this
  clearly so the user knows why it left the queue.
- In the Video tab's queue/scan list, surface `skip_reason` (tooltip or a details
  line) and offer explicit "Retry" (reset status to `queued`, zero `fail_count`)
  and "Remove" actions for failed/skipped jobs, if not already present.

**Tests.** DB-level test for the fail_count escalation; pure-function test for the
give-up threshold decision.

### 5. [DONE] Staging work dirs leak on the app drive

**Done (0.4.9-experimental).** Two reclamation paths, both fail-safe: (1) immediate
`_remove_job_staging(out_video, work_base)` when a job leaves the queue for good, wired
into the runner's give-up branch and the GUI removes (queue Remove + the Segments
manager's clip Delete); (2) a run-start `_sweep_orphan_staging(conn, work_base)` in
`run_queue` that deletes every staging dir under the base that no active job owns,
logging "Cleaned N orphaned staging folder(s), X GB freed". The sweep's keep-set is
every non-terminal job's output_path across ALL roots (new
`db.get_active_video_output_paths`), because the base is shared, so it never deletes
another root's in-progress dirs. Both guard with `_path_within(root, work_base)` and
leave stray files alone. `debug_log` was added (guarded) to this runner for the
swallowed-error trail. Tests: `tests/test_video_staging_sweep.py` (6: remove happy/no-op,
sweep keep-vs-orphan, stray-file/missing-base, cross-root safety, and give-up removal via
run_queue). Suite 428 green.

**Where:** `scripts/batch_video_upscale.py`: `_work_dirs` (~line 513) creates
`<app>/.imgtbx_video/<name>_<hash>`; cleanup only happens on job success
(`process_job` ~line 1108). `scripts/db.py`: `delete_video_output` (~line 653)
removes the DB rows but never the staging dir.

**Problem.** The work dir is intentionally kept for `failed`/`partial` jobs (resume
needs it), but it is orphaned forever when: the user removes a job from the queue
in the GUI, item 4's give-up marks a job skipped, or the output path changes
(target renamed, output root moved) so the hash-keyed dir is never revisited.
Split segments of a long 4K video are gigabytes; the default staging base is on
the app drive, so this silently eats the system disk.

**Fix.**
- When a job is removed from the queue (GUI remove path, and item 4's give-up),
  compute its `_work_dirs` root from the job's `output_path` and the configured
  `work_root`, and `shutil.rmtree(..., ignore_errors=True)` it. Guard: only delete
  paths that are inside the resolved staging base (`_path_within` already exists).
- Add a sweep at run start (in `run_queue`, before the queue loop): list the
  staging base's subdirs, keep those whose hash matches a pending/partial job's
  output path, delete the rest. Log a one-line summary ("cleaned N orphaned
  staging folder(s), X GB"). Fail-safe: any error skips the sweep.

**Tests.** Pure helper that maps queue rows -> expected staging dir names, tested
against `_work_dirs`; sweep tested with tmp dirs.

### 6. [DONE] The video path has no slow-segment health signal (watchdog gap)

**Done (0.4.9-experimental).** New pure `VideoSlowWatch` (unit-tested like funds_guard):
anchors to the run's minimum seconds-per-output-megapixel across completed segments and,
edge-triggered, warns when a segment sustains >= `watchdog_factor` x that baseline. It is
NOTIFY-ONLY (a log `WATCHDOG:` line + a yellow `notifications.notify` alert, "likely pod
host contention; cost is accruing; the run continues"), never auto-stop, per
[[remote-pods-secure-cloud]]: contention usually clears and killing a half-done segment
wastes its cost. Wired run-wide in `run_queue` (one baseline across every segment of
every job, since the pod is shared) and fed from `process_job` after each segment using
the same `ve.output_megapixels` the estimate uses; fail-safe. Config reuses the image
watchdog's `upscale.watchdog_enabled`/`watchdog_factor` (overridable under `video.*`);
echoed in `log_video_settings`. CLAUDE.md config documentation is folded into item 12.
Tests: `tests/test_video_slow_watch.py` (8: baseline/edge-trigger/re-arm, anchor-to-min,
per-MP fairness, ignored timing-less input, config resolution). Suite 436 green.

**Where:** `scripts/batch_video_upscale.py` (no counterpart to
`batch_upscale.py`'s `WATCHDOG_*` / `_trigger_degradation`).

**Problem.** The image watchdog exists because a degraded GPU silently multiplies
cost; it was explicitly "built as a reusable health signal for remote-pod
upscaling". The video runner, whose runs are the longest and most expensive, has
none: a segment running far slower than the run's healthy rate (observed in the
field as rare shared-infra contention on Secure Cloud, invisible from the guest)
just keeps billing. The per-segment `live_spf` and `video_estimate.record_run`
rates already exist, so the signal is nearly free.

**Fix (notify-only, do NOT auto-stop).** Track the run's best observed
seconds-per-output-megapixel across completed segments (same
anchor-to-the-minimum reasoning as the image watchdog). When a segment's live rate
sustains >= factor x the baseline (reuse `watchdog_factor`, default 3), emit one
edge-triggered warning per episode: a log line + a `send_notification` alert
("segment N running ~3x slower than this run's baseline; likely host contention;
cost is accruing"). Auto-stop is deliberately out of scope: contention often
clears, and killing a half-done segment wastes its cost; the user decides.
Config: reuse the existing `upscale.watchdog_enabled`/`watchdog_factor` keys or
mirror them under `video.` (pick one, document it in CLAUDE.md's config list).

**Tests.** Pure decision function (baseline update + trip/edge logic), unit-tested
like `funds_guard`.

---

## P3: Supply-chain hardening (small, contained)

### 7. [DONE] Two pinned downloads are not integrity-checked

**Done (0.4.9-experimental).** (a) The libVLC zip is now hash-pinned: `VLC_SHA256`
(`a0b7ec0...14843`, VideoLAN's published sum, verified against the real 77.6 MB download)
is baked into both `scripts/vlc_setup.py` and `bootstrap.ps1` (`$LIBVLC_SHA256`), checked
after download; a mismatch deletes the temp file and fails the (optional) install step.
(b) The on-pod static ffmpeg now passes a functional `ffmpeg -version` gate on the
extracted binary BEFORE it is cached to the persistent volume, so a truncated/corrupt
download can't be cached once and break every future run (johnvansickle has no strong
hash; kept best-effort with the worker still failing loudly if ffmpeg is truly absent).
A standing rule was recorded in memory ([[verify-third-party-downloads]]). Tests:
`tests/test_vlc_hash_pin.py` (3: mismatch rejected, matching hash proceeds, both
installers pin the same artifact). Suite 439 green.

**Where:**
- `scripts/vlc_setup.py` (~line 30) and bootstrap's `Install-LibVlc`: the pinned
  `vlc-3.0.21-win64.zip` is downloaded with no hash check, while bootstrap already
  hash-pins Python and verifies gyan.dev ffmpeg against its sidecar.
- `scripts/remote_run.py` `_start_worker` (~line 437): the on-pod static ffmpeg is
  curl'd from `johnvansickle.com` straight onto the model volume with no
  verification and no fallback mirror.

**Fix.**
- The VLC zip is a fixed, immutable artifact: hard-code its SHA-256 in both
  `vlc_setup.py` and `bootstrap.ps1`, verify after download, delete + fail the
  install (with the existing status-return pattern) on mismatch.
- For the pod ffmpeg: johnvansickle publishes an `.md5` sidecar (weak) and the
  build moves; at minimum add `ffmpeg -version` as a post-extract sanity gate
  before caching it to the volume (matching bootstrap's "functional version check"
  approach), and prefer switching the URL to a BtbN-pinned release tarball for
  linux64 (same pin rationale bootstrap documents at lines 62-66). Keep it
  best-effort: the worker already fails loudly if ffmpeg is truly absent.

---

## P4: Efficiency

### 8. [DONE] `RemoteVideoEngine` buffers whole segments in RAM in both directions

**Done (0.4.9-experimental).** Both directions stream now. `_submit` takes the source
path, sets an explicit `Content-Length` (`os.path.getsize`) and passes the open file
object as `data`, so http.client sends it in blocks with a normal Content-Length body
(no chunked encoding, so the worker is unaffected) instead of `f.read()` into RAM.
`_fetch` takes the temp path and `shutil.copyfileobj(resp, f, 1<<20)` straight to disk,
returning the byte count; `process_segment` then does the atomic `os.replace` and reads
`last_phase["bytes"]` from that count. Peak local RAM per segment drops from input+output
whole to one block. Phase timing keeps its meaning (fetch now includes the disk write,
finalize is just the rename). Tests: `tests/test_video_stream_io.py` (4: fetch streams to
disk, submit sets Content-Length + streams a file object, end-to-end round trip, byte
count recorded), faked urlopen so no pod. Suite 443 green.

**Where:** `scripts/remote_video_engine.py`: `process_segment` (~line 70,
`f.read()` of the whole input) and `_fetch` (~line 178, `resp.read()` of the whole
result); the result then sits in `out` while being rewritten to disk.

**Problem.** A 4K upscaled segment is commonly hundreds of MB; peak local RAM per
segment is input + output simultaneously. Not a crash today, but needless pressure
on long unattended runs (and the doc's own streaming philosophy on the pod side).

**Fix.** `urllib.request.Request(url, data=<file object>)` streams an upload when a
`Content-Length` header is set (`os.path.getsize`); download side: open the
`.tmp` file and `shutil.copyfileobj(resp, f, length=1<<20)`. Keep the atomic
`os.replace`. `last_phase["bytes"]` becomes the byte count from copyfileobj/stat.

### 9. [DONE] Persist the OOM-learned batch across videos and runs

**Done (0.4.9-experimental), redesigned as adaptive tuning.** Persisting the OOM-learned
ceiling as-written was unsafe (it only ratchets DOWN and, keyed by target, would cap a
portrait 4K clip at a landscape 4K video's batch). Built instead as a measured, self-
correcting tuner:
- **Worker** returns a MEASURED-ANCHORED `suggested_batch` in `/video/status`: it shifts
  the calibrated VRAM curve by the real working set just measured at the run batch, then
  returns the largest 4n+1 that fits the same budget. So it can go UP (recover a stale-low
  seed) or DOWN (a too-high one), anchored to reality not the cold prediction. Also returns
  `vram_total_gb`. Factored `_vram_budget_gb`; added `_suggested_batch`.
- **Worker overlap fix:** OOM recovery now re-derives `_auto_overlap(smaller)` (for an auto
  overlap) instead of inheriting the larger batch's overlap, so a hard drop can't collapse
  the stride (batch 9 + inherited overlap 8 = stride 1 -> re-derived floor 6 = stride 3).
  Note: the floor is `_MIN_OVERLAP=6` for seam invisibility, so a small batch legitimately
  runs overlap 6 (stride 3); that is a quality floor, not a bug, and the real lever is
  recovering to a BIGGER batch when headroom exists (bigger stride, less redundancy).
- **DB:** `video_batch_learn(gpu_id, mp_bucket, batch, updated_at)`, keyed by **output-MP
  bucket** (0.5 MP grid), not target, so aspect/target tiers don't collide; 90-day
  staleness; newest write wins (self-heals after a driver/code change).
  `get_learned_batch`/`put_learned_batch`.
- **Runner controller** (gated by `video.auto_tune_batch`, AUTO batch only, >= 2 segments):
  seed the first segment from the DB (or auto), adopt the pod's `suggested_batch` after
  segment 1, FREEZE it for the rest of the video (one torch.compile shape) and persist it.
  `_request_batch` precedence (config > tuned > OOM-carry > auto), `_mp_bucket`.
- **Config + GUI:** `video.auto_tune_batch` (default on) + a Settings -> Video checkbox.

Tests: `tests/test_worker_batch_suggest.py` (7: anchored math, monotonicity, bounds, the
overlap floor) and `tests/test_video_autotune.py` (8: precedence, bucketing, DB round-trip
+ staleness, and ffmpeg-gated converge/seed/disabled end-to-end). Suite 458 green.

**Where:** `scripts/batch_video_upscale.py`: `updated_learned_batch` /
`_learned_batch` (0.4.8) resets per video.

**Problem.** The 0.4.8 carry-forward stops re-discovery within one video, but the
next video at the same output resolution on the same card re-pays the failed
forward pass + VRAM churn, on every video of a big batch and again next run.

**Fix.** Store the learned safe batch in the DB keyed by (gpu_id from
`IMGTBX_GPU_OVERRIDE`, output short-side resolution): a small
`video_batch_learn(gpu_id, resolution, batch, updated_at)` table (or reuse the
`gpu_perf` pattern in `db.py`). Seed `req_batch` from it at segment-loop start
(explicit config batch remains the ceiling, exactly like the in-video carry), and
update it whenever `updated_learned_batch` lowers the value. Include the
resolution in the key: the same card OOMs at different batches for 1080p vs 4K.
Log when a stored value is applied ("starting at batch 9, learned on this card
for 2160px on 2026-07-01"). Consider a staleness rule (ignore entries older than
~90 days; drivers and worker code change).

**Tests.** DB round-trip + the seeding precedence (config > learned > default).

---

## P5: Roadmap enablers and housekeeping

### 10. [DONE] Record video lineage at job completion (enabler for roadmap #5, video conciliation)

**Done (0.4.9-experimental).** After a WHOLE-video job's DONE upsert, the runner links
its source to its output in the shared `lineage` table via the new fail-safe
`_record_video_lineage` (reuses `db.hash_file_cached` + `db.record_upscale_lineage`, the
exact image-runner path; the video has no tag stage so `tagged_hash` stays NULL). Gated
by a new `video.record_lineage` config key (default on) since hashing a multi-GB source
over a network share is a full read; echoed in `log_video_settings`. Clips are deliberately
NOT linked (the "source" is `src_abs`, the untouched whole file, never the temp clip; a
src->clip link would be wrong for conciliation), and a source path that no longer exists is
skipped. Tests: `tests/test_video_lineage.py` (5: pure helper round-trip, missing-source
skip, bad-input fail-safe, and ffmpeg-gated whole-video-records / disabled-writes-nothing).
Suite 463 green.

**Where:** `scripts/batch_video_upscale.py` `process_job` (the `status="done"`
block ~line 1106); `scripts/db.py` lineage helpers (~line 750).

`docs/future-features.md` #5 needs source->output lineage recorded as files are
produced. The image runners already do this; the video runner records nothing, so
every video upscaled before #5 lands will be un-matchable by content hash.
Recording now is cheap to write but hashing multi-GB sources is not free: use
`hash_file_cached` (memoised by path+mtime+size, and the source was just read for
the split so it is warm in the OS cache), do it after the DONE line inside a
fail-safe try/except, and skip when the source is on a path that no longer exists.
If measured hashing time on network sources proves annoying, gate it behind a
`video.record_lineage` config default-on. No GUI work; this is data collection
only.

### 11. [DOC] Self-healing remote runs (#6) is the next feature milestone

Not a defect, listed for planning honesty. No code lands here (it is a milestone, not a
review fix), but the one insight this item carries that `docs/future-features.md` #6 did
not yet state was folded in there so it survives this doc's deletion: items 1/4/5 (0.4.9)
are now **load-bearing prerequisites** for #6, because the healer resumes unattended and
*repeatedly*, amplifying exactly the split-reuse (item 1), failed-job-forever (item 4) and
staging-leak (item 5) failure modes those items fixed. #6's "Reuse" section and the
Sequencing bullet were updated accordingly; the rest of #6's decided design (opt-in
Auto-resume checkbox, identical-card-only redeploy, unbounded money-gated wait, double-bill
safety) was already complete there. The build itself remains a separate milestone.

### 12. [DONE] Release housekeeping for 0.4.9

**Done (0.4.9-experimental).**
- `APP_VERSION` in `scripts/gui/common.py` bumped `"0.4.8"` -> `"0.4.9"` (no
  `-experimental` suffix, per the checklist). The git tag + fold-to-main is the
  author's action (see [release-workflow] in memory), not done here.
- CLAUDE.md updated: the config-section list now includes `video` (with the new
  `auto_tune_batch` + `record_lineage` keys and the `watchdog_*` fallback noted), and
  the `db.py` module row now names the `video_*` tables, the `fail_count` column
  (item 4) and the `video_batch_learn` table (item 9). `config_store.SECRET_FIELDS`
  is untouched (none of the new keys are secrets). README's config table already
  listed `video`; its description covers the tuning knobs, so no change needed.
- Both DB changes go through the guarded pattern so an existing `cache.db` upgrades
  in place: `fail_count` via SCHEMA + the `_ensure_video_columns` ALTER, and
  `video_batch_learn` via `CREATE TABLE IF NOT EXISTS` in the schema run on `get_conn`.
- Suite 463 green.

---

## Suggested implementation order

1 -> 2 -> 3 (correctness, small diffs, each independently shippable), then
4 -> 5 (queue/staging hygiene, share the "job leaves the queue" plumbing), then
6, 7, 8, 9 (independent of each other), then 10, and 12 at release time.
Item 11 is its own milestone and starts only after 1-6 are green.
