# Codebase review: improvement & optimization recommendations

Review of the full repository (docs, `scripts/`, `pod/`, `tests/`, `tools/`,
root) on the `0.4.5-experimental` branch, 2026-07-05. Successor to the 0.4.0
review (12 items, all shipped in 0.4.3/0.4.4 and pruned from docs). Ordered by
importance/urgency: items 1-4 are correctness/data-integrity bugs worth fixing
before the next release; 5-6 are real performance/quality wins in Tag & Rename;
7-9 are consistency debt; 10-12 are lower-priority polish.

What is already strong, so it stays that way: the design docs are still
excellent and full of verified-live findings; the runner/GUI subprocess seam,
the `db.py` cache layer, `runner_common`, `config_store` and the notification
fan-out are clean and well-commented; the money paths (dead-man's switch,
funds guard, no-GPU-substitution) are genuinely defensive; the test suite (233
tests, ~1.2 s) is fast and green. The items below close gaps around that core;
none change the architecture.

---

## 1. The Video Upscaler's pod is not protected from "Terminate", and its cost readouts never arm

**Severity: money bug (remote runs).**

**Status: DONE (0.4.5-experimental).** `batch_video_upscale.main()` now emits
`POD` and `RCOST` after `session.start()`; the Video tab's POD handler reads the
decoded value (this runner JSON-encodes, unlike the image runners) and an RCOST
handler arms a live `$X.XX so far` readout in `_run_tick`; `active_pod_id` is
cleared GUI-side in `_end_run` so a hard-killed runner still releases the
protection. `App.active_remote_pod_ids()` and `App._any_task_running()` now
include `video_tab`. Regression test: `tests/test_video_pod_events.py` pins the
emitter to the parser end to end.

**What:** the RunPod tab refuses to terminate a pod that a live run depends on
via `App.active_remote_pod_ids()` ([gui/app.py:424-432](../scripts/gui/app.py)),
but that method iterates only `(self.upscale_tab, self.tag_tab)`. The Video tab
is never consulted, so during a multi-hour (paid) video run the user can select
`video-toolbox-remote` in the RunPod tab and terminate it mid-segment with no
warning.

**Compounding it:** the protection can't work anyway, because
`batch_video_upscale.py` never emits the `POD` event at all (the other two
remote runners do: [batch_upscale.py:1440](../scripts/batch_upscale.py),
[tag_and_rename.py:1299](../scripts/tag_and_rename.py)). The Video tab's `POD`
handler ([gui/tab_video.py:1285-1287](../scripts/gui/tab_video.py)) is dead
code: `active_pod_id` stays `None` for the whole run. The runner also never
emits `RCOST`, so whatever live-cost UI keys off the pod's real billed rate on
the other tabs has no equivalent signal here (the runner knows the rate:
`session.cost_per_hr` is read for `RunBudget` in
[batch_video_upscale.py:930](../scripts/batch_video_upscale.py)).

**Do:**
- In `batch_video_upscale.main()`, after `session.start()`, emit
  `gui_event("POD", session.pod_id or "")` and
  `gui_event("RCOST", session.cost_per_hr)` exactly like the image runners,
  and clear `POD` on exit.
- Add `self.video_tab` to `App.active_remote_pod_ids()` (it already maintains
  `active_pod_id` and has a `running` property).
- While there: `App._any_task_running()`
  ([gui/app.py:619-621](../scripts/gui/app.py)) also omits `video_tab`, so the
  60 s idle telemetry sampler keeps firing during video runs, contradicting the
  "the idle sampler steps aside whenever a task is running" contract. Include
  the video tab there too.

## 2. Tag & Rename silently converts PNG/WebP/TIFF files to JPEG bytes (or fails on them)

**Severity: data-integrity bug; conflicts with the app's core promise.**

**Status: DONE (0.4.5-experimental).** Took the minimal/honest option. EXIF is
now embedded only for JPEG (`_exif_writable()` gates on the *real* container
format read from the file, not the extension); `write_exif` /
`write_processed_marker` return a bool and no-op on non-JPEG, leaving those files
**byte-for-byte untouched** (no re-encode, no RGBA crash). `_save_with_exif` grew
a hard guard that raises rather than ever writing JPEG bytes under a non-JPEG
extension, so a future caller can't reintroduce the corruption. The skip-on-rerun
signal for non-JPEG is the cache's existing `"processed"` status
(`is_already_processed(path, cache, root)` consults it, found by `current_rel_path`
after a rename), so PNG/TIFF are no longer re-analysed every run. WebP/TIFF EXIF
support (both *can* round-trip) was deliberately left out to keep one simple rule
and stay consistent with the piexif-based undo path, which can't read PNG/TIFF EXIF
back. Regression tests: `tests/test_tag_exif_formats.py`.

**What:** every EXIF write goes through `_save_with_exif`
([tag_and_rename.py:661-673](../scripts/tag_and_rename.py)), which does
`img.save(path, "jpeg", ...)` unconditionally, while the tab's
`IMAGE_EXTS` ([tag_and_rename.py:270](../scripts/tag_and_rename.py)) admits
`.png`, `.webp`, `.tiff`, `.tif`. Consequences for any non-JPEG input:

- A `.png`/`.webp`/`.tif` file gets **JPEG bytes written under its original
  extension** (a lossy re-encode wearing the wrong container), or
- the save **raises** for RGBA PNGs ("cannot write mode RGBA as JPEG") and the
  image counts as failed.

Worse, the processed marker can't round-trip either: `is_already_processed`
reads EXIF via piexif, which doesn't parse PNG/WebP, so a mangled-but-saved
file is **re-processed (and re-encoded again) on every subsequent run**. The
upscaler legitimately produces `.png`/`.tiff` outputs (its `_save_image`
follows the source extension), so this isn't hypothetical: an upscaled-then-
tagged PNG tree hits it directly.

**Do (pick one):**
- Minimal and honest: restrict Tag & Rename's write path to JPEGs. Non-JPEG
  files can still be **renamed** (that part is format-agnostic); log
  "tagged name only, EXIF unsupported for .png" and skip the EXIF write. The
  undo cache already tolerates absent EXIF snapshots.
- Fuller: branch on format (Pillow can write PNG `iTXt`/XMP or preserve the
  original format with `img.save(path, format_of_source, exif=...)` where the
  format supports EXIF, e.g. WebP and TIFF do; PNG needs a different container).
- Either way, add a regression test: tag a small PNG and assert the file still
  begins with the PNG signature afterwards.

## 3. Non-English EXIF descriptions are destroyed by ASCII encoding

**Severity: feature-quality bug, directly hits the selectable-language feature.**

**Status: DONE (0.4.5-experimental).** `write_exif` now writes `ImageDescription`
as UTF-8 (was `encode("ascii", "replace")`) and mirrors the description into
`XPTitle` (tag 40091, UTF-16LE), the Windows-native "Title" field, so accented
text shows both in standard viewers and in Explorer. XPTitle was added to
`_TRACKED_EXIF_FIELDS` so undo restores/removes it. While here, fixed a latent
undo-snapshot bug this would have worsened: piexif returns the XP* tags as int
tuples, which `base64.b64encode` can't take, so a `_snapshot_exif` that reached an
XP field aborted mid-loop and silently dropped the fields after it; it now
normalises tuples to bytes. `--help` and the module header document the new
fields. Regression tests: `tests/test_tag_exif_encoding.py`.

**What:** `write_exif` stores the model's description as
`long_description.encode("ascii", "replace")`
([tag_and_rename.py:686-688](../scripts/tag_and_rename.py)). With
`--language:RO` (or any non-ASCII language) every diacritic becomes `?`:
"Pisică pe acoperiș" is stored as "Pisic? pe acoperi?". The GUI offers the
language picker prominently, so the flagship output of that feature is mangled
in the one place it is persisted. (Filenames are deliberately ASCII-sanitised;
that part is correct and unaffected.)

**Do:** write UTF-8 bytes into `ImageDescription` (piexif passes bytes
through; virtually every viewer including Windows Explorer and XnView reads
UTF-8 there), and/or mirror the description into `XPTitle` (tag 40091,
UTF-16LE, the Windows-native Unicode field, same encoding path the code
already uses for `XPComment`). Keep the undo snapshot logic unchanged (it
stores raw bytes, so it round-trips either way).

## 4. `merge()` in batch_upscale crashes the end-of-run summary after a second pass

**Severity: latent crash (KeyError), easy fix.**

**Status: DONE (0.4.5-experimental).** Both per-folder stat templates now come
from one factory (`_new_folder_stats`, keys in `_FOLDER_STAT_KEYS`), so `run_pass`
and the merge can't drift. `merge()` was hoisted out of `main()` to a pure,
module-level `_merge_pass_stats(s1, s2)` (the per-run flags user_quit/degraded/
remote_stopped are intentionally not merged — the caller reads those from the
individual passes). Unit test: `tests/test_merge_pass_stats.py` (the same-folder-
in-both-passes case that used to KeyError, plus disjoint folders and the
corrupt-file list).

**What:** when the rescan finds new files and a second pass runs, the summary
calls `merge(stats1, stats2)`
([batch_upscale.py:1716-1736](../scripts/batch_upscale.py)). Its per-folder
template dict is missing the `"skipped_corrupt"` key that `run_pass`'s
`folder_stats` template includes
([batch_upscale.py:989-992](../scripts/batch_upscale.py)), so
`merged[d]["skipped_corrupt"] += ...` raises `KeyError` on the first folder
merged. The whole queue has been processed by then (crash_logger catches it),
but the summary table, DONE event, MQTT last_run and the completion
notification are all lost, exactly on the runs that had a second pass.

**Do:** add `"skipped_corrupt": 0` to the template in `merge()`; better, build
the template from one shared constant so the two can't drift again. Add a
5-line unit test that merges two stats dicts (this is exactly the kind of pure
function `tests/` already covers well elsewhere).

## 5. Tag & Rename's cache persistence is O(N²): full rewrite after every image

**Severity: performance, scales badly on the target workload (large photo trees).**

**Status: DONE (0.4.5-experimental).** `save_cache` is now incremental: a `_dirty`
set on the cache tracks which entries changed, and only those are upserted
(`tag_files`' PRIMARY KEY (root_id, original_rel_path) makes INSERT OR REPLACE a
true upsert), replacing the DELETE-all + re-INSERT-every-row that ran after each
image. `_find_entry` is now O(1) via a `{normcase(current_rel_path): key}` index
(`_index`) maintained by `ensure_cache_entry` / `update_cache_entry`, with a linear
fallback for a cache built without one (tests). The four mutation sites (ensure,
update, skip, rotation) mark the entry dirty; undo keeps a single `full=True`
whole-root rewrite (it saves once, not per-image, and rewrites in place). On-disk
schema unchanged. Tests: `tests/test_tag_cache.py` (index resolution incl.
renames, and the key correctness guarantee that an incremental save does not drop
untouched rows).

**What:** `save_cache` deletes and re-inserts **every** row for the root
(`DELETE FROM tag_files WHERE root_id = ?` + executemany of all entries, each
`json.dumps`-ed) ([tag_and_rename.py:896-924](../scripts/tag_and_rename.py)),
and it is called after **every** processed image, every failure, every skip
and every rotation. For a 10,000-file folder that is ~10k JSON serialisations
plus a 10k-row transaction per image: O(N²) DB work over a run, easily minutes
of pure overhead and steady WAL churn on a long tag run. `_find_entry` is also
a linear scan over all entries
([tag_and_rename.py:946-961](../scripts/tag_and_rename.py)) and is called
several times per image (lookup, original-name, rotation, update).

**Do:** mirror what `EligibilityCache` already does on the upscale side
([batch_upscale.py:313-461](../scripts/batch_upscale.py)): track a `dirty` set
of keys and upsert only those rows on save; keep a `{normcase(current_rel_path):
key}` index dict alongside `cache["files"]` so `_find_entry` is O(1). The
on-disk schema needs no change. This is a contained refactor with an outsized
effect on big folders.

## 6. Every tagged JPEG is fully re-encoded twice (quality + time)

**Severity: image-quality + performance; violates the spirit of "touch files gently".**

**What:** a successful tag does `write_exif(path, ...)` (re-encode #1 at
quality 95) and then `write_processed_marker(new_path)` (re-encode #2 at
quality 95) ([tag_and_rename.py:1631-1648](../scripts/tag_and_rename.py)).
Two generations of JPEG loss per image, twice the disk I/O, and on
high-resolution upscaler outputs each save is not cheap. The undo path
(`_restore_exif_fields`) adds a third re-encode when used.

**Do:** merge the description, XPComment and the processed marker into **one**
EXIF dict and one `_save_with_exif` call (the marker's timestamp is known
before the save; the rename can happen after the single save exactly as now).
Longer term, consider piexif's `insert()` (rewrites only the EXIF segment,
no pixel re-encode) for JPEGs whose EXIF block fits, falling back to the
Pillow re-encode; that would make tagging effectively lossless.

**Status: DONE (0.4.5-experimental).** Both halves shipped, not just the merge:
- `write_exif` now folds the description (ImageDescription UTF-8 + XPTitle),
  the original filename (XPComment) **and** the processed marker (UserComment)
  into one EXIF dict and one save. The separate `write_processed_marker` call
  was removed from the main tag loop (the function is kept for tests / standalone
  callers). The single save happens before the rename, so the marker rides the
  same bytes whether or not the file is renamed.
- `_save_with_exif` now writes via **piexif `insert()`** — it patches only the
  APP1/EXIF segment and never re-encodes the pixels, so tagging is truly
  lossless (verified byte-identical even after repeated re-tags), with a single
  Pillow re-encode (quality 95) as the fallback for a JPEG `insert()` can't
  patch. Net effect: two lossy re-encodes per image became **zero**.
  Tests: `tests/test_tag_lossless.py` (one write marks processed + carries all
  four fields; pixels byte-identical; no degradation across 5 re-tags). Existing
  `test_tag_exif_formats` / `test_tag_exif_encoding` still pass (268 total).

## 7. `batch_video_upscale.py` re-implements the shared runner scaffolding

**Severity: consistency debt; the exact drift class `runner_common` (0.4.3 item 5) was built to end.**

**What:** the video runner imports `runner_common` but then defines its own
private `GUI_MARKER`, `_gui_mode()`, `GUI_MODE` and `gui_event()`
([batch_video_upscale.py:82-155](../scripts/batch_video_upscale.py)) instead of
re-exporting the shared ones like the other three runners do. Its `gui_event`
even differs in behaviour (it `json.dumps`-es the payload itself, and writes
via `sys.stdout` rather than the tee-aware `.raw` path). If the marker or the
atomic-write rule ever changes in `runner_common`, this copy silently drifts;
that is precisely the failure mode the 0.4.3 consolidation documented.

**Do:** re-export `runner_common.GUI_MARKER / GUI_MODE / stdin_is_piped` and
wrap `runner_common.gui_event` with the JSON convenience locally. Fold in the
`POD`/`RCOST` emission from item 1 in the same pass.

**Status: DONE (0.4.5-experimental).** The private `GUI_MARKER = "@@TBX@@"`,
`_gui_mode()`, `GUI_MODE` and the payload-writing body of `gui_event` are gone.
`batch_video_upscale` now re-exports `runner_common.GUI_MARKER / GUI_MODE /
stdin_is_piped` (exactly as `conciliate`, `batch_upscale`, `tag_and_rename` do),
and its `gui_event(kind, payload)` is a one-liner that JSON-encodes the payload
and delegates the write to `runner_common.gui_event` (the shared atomic,
tee-aware emitter). The wire output is byte-identical to before (JSON-encoded
payload), so the Video tab parser is unchanged; the marker/atomic-write rule now
has a single source of truth. The `POD`/`RCOST` emission from item 1 already
lives here and flows through the same wrapper.
Tests: `tests/test_video_pod_events.py` gained a delegation test (gui_event
hands the JSON string to `runner_common.gui_event`) and its `_emit` helper now
forces the correct flag (`runner_common.GUI_MODE`); the 5 existing emitter→parser
tests still pin the wire format end to end (269 total).

## 8. Stale text pass: tooltips, help output, pod/README, default mismatches

**Severity: doc/UI drift; individually small, together misleading.**

- **GPU picker tooltip still describes the removed auto-fallback.** The 0.4.0
  change removed GPU substitution and the price ceilings, yet the tooltip says
  "If your pick is unavailable, only cheaper in-stock cards under the price
  ceiling (RunPod tab) are tried automatically"
  ([gui/tooltab.py:231-235](../scripts/gui/tooltab.py)). A user reading it will
  expect behaviour the code explicitly refuses to do.
- **`pod/README.md` is two eras stale:** it lists only `deadman.py` and calls
  `worker.py` "planned (later phases)", while the folder ships `worker.py`,
  `bench_video.py`, `ram_probe.py`, `upscale_one.py`, `provision.sh`.
- **`tag_and_rename.py --help` still points at the retired JSON cache:** the
  usage text prints `trcache/<folderhash>.cache`
  ([tag_and_rename.py:1352-1354](../scripts/tag_and_rename.py)) and the module
  docstring says the same; the cache has lived in `db/cache.db` since 0.2.x.
  `CACHE_DIR` survives only for the one-time legacy import (in `db.py`), so the
  constant in this file is vestigial.
- **`max_runtime_minutes` fallback default disagrees with the documented
  default:** config/docs say the ceiling defaults to **0 (no limit)**, but the
  code fallbacks use **720** when the key is absent
  ([remote_run.py:545](../scripts/remote_run.py),
  [gui/tooltab.py:810](../scripts/gui/tooltab.py)). Harmless while every real
  config carries the key, but the first missing-key install will behave
  differently from the docs. Pick one default and use it everywhere.

**Status: DONE (0.4.5-experimental).** All four fixed:
- GPU-picker tooltip ([gui/tooltab.py](../scripts/gui/tooltab.py)) rewritten to
  the actual 0.4.0 behaviour: the run uses exactly the picked card, no automatic
  substitution; if it has sold out by deploy time the run stops cleanly and the
  user presses ↻ to refresh stock and re-pick.
- `pod/README.md` rewritten: `worker.py` is now the resident worker (all three
  `--mode`s), and `provision.sh` / `upscale_one.py` / `bench_video.py` /
  `ram_probe.py` are documented as shipped, not "planned".
- `tag_and_rename.py`: the `--help` "Undo support" text and the module docstring
  now name `db/cache.db` (tag_files table); the vestigial `CACHE_DIR` constant is
  removed (only `CACHE_SCHEMA_VERSION`, still used, remains, with a corrected
  comment); the stale `trcache/` comment on `APP_ROOT` is fixed.
- `max_runtime_minutes` fallback is now **0** in both outliers
  ([remote_run.py](../scripts/remote_run.py),
  [gui/tooltab.py](../scripts/gui/tooltab.py)), matching `config.json`,
  `tab_runpod.py` (3 sites) and `docs/runpod-notes.md`. No new test: this is a
  text/consistency pass, and the default now agrees across all seven sites (the
  full suite still passes, 269).

## 9. Notification coverage is uneven across the tools

**Severity: consistency; matters for the unattended-run use case the app optimises for.**

- **Conciliation sends no notifications at all** (no `notifications` import in
  [conciliate.py](../scripts/conciliate.py)): a long archive/delete run over a
  big tree finishes (or errors) silently while upscale/tag/video all notify.
- **A video run that fails to start the pod notifies nobody:** `main()` logs
  and returns 1 ([batch_video_upscale.py:936-943](../scripts/batch_video_upscale.py)),
  while the image runners send a red "Engine Failed to Start" alert. The
  in-queue failure path does notify; only the startup path is silent.

**Do:** add the standard `notifications.resolve_settings` + completion/error
sends to conciliate, and a red notification in the video runner's `except`
around session start. Both are ~10-line changes reusing `notifications.py`.

**Status: DONE (0.4.5-experimental).**
- **Conciliation** now imports `notifications` + `config_store`, resolves a
  module-level `NOTIFY` (fail-safe: `config_store.load` returns None on a
  missing/malformed config and `resolve_settings({})` makes every backend a
  no-op, so conciliate still runs config-free), and has a `send_notification`
  wrapper (username "Conciliate Bot") like the other runners. The end of a run
  fires a completion alert coloured by outcome via the pure
  `_completion_notice(done, conflicts, errors, stopped)` helper (green clean,
  yellow stopped/with-issues), with source/operation/elapsed/machine fields.
- **Video runner:** the `except` around session start + `run_queue` now sends a
  red alert. A pure `_failure_notice(started, exc)` distinguishes the
  previously-silent **startup** failure ("Video upscale failed to start" /
  "Could not start the remote pod/engine") from a mid-run crash ("Video upscale
  failed"); per-video failures remain covered by the existing summary send.
- Tests: `tests/test_notifications_coverage.py` (8) pins both pure selectors and
  the conciliate wrapper's delegation + fail-safe no-op. Full suite 277.
- Test-suite safety: the runners resolve their notification settings from the
  developer's live `config.json` at import (a real webhook), so the fail-safe
  test was firing an actual Discord message. Fixed two ways: that test now forces
  an empty settings dict, and `tests/conftest.py` gained an autouse fixture that
  stubs `notifications.send_discord/telegram/ntfy` to no-ops for EVERY test, so no
  test run can ever contact a real endpoint.

## 10. `PauseController` internals are poked from the outage path

**Severity: robustness/style.**

**What:** the upscaler's outage handler reaches into privates:
`with pause._lock: pause._paused = True; pause._pause_start = time.time()`
([batch_upscale.py:1262-1265](../scripts/batch_upscale.py)), and the stats dict
reads `pause._quit` ([batch_upscale.py:1320](../scripts/batch_upscale.py))
despite a public `quit_requested` property existing. Works today, but any
internal change to the controller breaks a code path that only fires during
GPU outages, i.e. the least-tested moment.

**Do:** add `PauseController.force_pause()` (and use `quit_requested`), three
lines each.

**Status: DONE (0.4.5-experimental).** Added `PauseController.force_pause()` (an
idempotent lock-held pause that no-ops if already paused, so it can't clobber a
running pause's `_pause_start` and corrupt `paused_seconds`). The outage handler
now calls `pause.force_pause()` instead of the three-line `with pause._lock: …`
poke, and the stats dict reads `getattr(pause, "quit_requested", False)` (the
public property) instead of `pause._quit`. No `pause._*` private access remains
in the file. Tests: `tests/test_pause_controller.py` (4) prove force_pause()
blocks `.check()` until Resume, that Stop also releases it (returning False),
idempotency of the pause clock, and that `quit_requested`/`check()` reflect a
quit. Full suite 281.

## 11. Loose substring matching in the Ollama model checks

**Severity: minor correctness.**

**What:** `check_ollama` and `unload_model` match with
`any(base in m for m in models)` where `base = OLLAMA_MODEL.split(":")[0]`
([tag_and_rename.py:454-501](../scripts/tag_and_rename.py)). A configured
`llava` matches `llava-phi3`, `qwen2.5vl` matches any future `qwen2.5vl-*`
variant, so the pre-flight can pass while the actual `/api/generate` fails
with "model not found" and burns the outage path instead of the clean startup
error. Match on the name up to the tag boundary (`m == base or
m.startswith(base + ":")`).

**Status: DONE (0.4.5-experimental).** Added `_ollama_model_available(models)`,
matching each listed name on the `base` (`OLLAMA_MODEL` up to `:`) via `m == base
or m.startswith(base + ":")`. Both call sites (`check_ollama` on `/api/tags`,
`unload_model` on `/api/ps`) now use it, replacing the two `base in m` substring
tests. Tag-agnostic base matching is deliberately kept (a configured `llava:13b`
still matches a pulled `llava:7b`); only the name boundary is tightened. Tests:
`tests/test_ollama_model_match.py` (8) reject the reported false positives
(`llava`→`llava-phi3`, `qwen2.5vl`→`qwen2.5vl-max`, `my-llava`/`llavax`) and keep
the real matches (exact, untagged, differently-tagged, among several). Full suite 289.

## 12. Test-coverage gaps around the data-touching logic

**Severity: prevention; the suite is fast and green but skips the riskiest module.**

**What:** `tests/` covers the protocol, db, video pipeline/estimate, deadman,
funds guard, notifications, runner_common, config_store and updater well, but
there is **no test at all for `tag_and_rename`**, the one tool that mutates
user files in place (rename + EXIF + rotation + undo). All of its trickiest
parts are pure or nearly pure and would test cheaply:

- `_sanitize_condensed` / `_auto_condense` / `resolve_language` /
  `has_camera_default_name` (pure string logic),
- `build_new_path` collision handling (tmp dir),
- the undo round-trip: tag a tmp JPEG, `_undo_entry`, assert name + EXIF bytes
  restored (this is the "every change is recorded" promise, currently
  verified only by manual use),
- the item-4 `merge()` regression, and the item-2 "PNG stays a PNG" check.

**Do:** add a `tests/test_tag_and_rename.py` with those; the module already
imports cleanly off-GPU (Ollama is only touched at run time).

**Status: DONE (0.4.5-experimental).** Added `tests/test_tag_and_rename.py` (18):
the pure string logic (`resolve_language` code/name/unknown, `has_camera_default_name`
suffix-stripping + start-anchoring, `_auto_condense` + `_sanitize_condensed`
capitalisation / word-cap / diacritic-fold / illegal-char strip / empty-fallback),
`build_new_path` collision handling (counter suffix, own-name-not-a-collision,
base_stem replacement), and the **undo round-trip** that backs the "every change is
recorded" promise (tag a real JPEG, `_undo_entry`, assert name restored + the added
EXIF incl. the processed marker stripped so it is re-taggable + status "undone";
plus a names-only undo that leaves EXIF intact). Together with the tests from items
2/3/6 and the filename-growth fix (`test_tag_exif_formats`, `test_tag_exif_encoding`,
`test_tag_lossless`, `test_tag_cache`, `test_tag_rename_original`, `test_ollama_model_match`),
`tag_and_rename` is now the best-covered runner rather than the only untested one.
Also fixed a stale `_restore_exif_fields` docstring (it claimed a quality-95
re-encode; item 6 made the save lossless). Full suite 307.

---

## Out-of-band fix: Tag & Rename filename growth on re-runs (user-reported)

**Severity: correctness (user-reported during the 0.4.5 work). DONE.**

**What:** re-running Tag & Rename could append each new description to the
*previous* pass's result (`001.jpg` -> `001_Child_At_Window.jpg` ->
`001_Child_At_Window_Child_Looking_Out.jpg` -> ...). The intended design already
rebuilds the name from the original stem, and a normal same-folder re-run was in
fact correct. The failure was a cache MISS (a reset DB, or the same folder reached
via a different root path such as a mapped drive vs UNC): `ensure_cache_entry`
seeded the new entry's `original_rel_path` from the CURRENT, already-renamed name,
and `get_original_name`'s cache-first lookup then short-circuited the EXIF fallback
that would have recovered the true original. Worse, the wrong name was then written
back into EXIF XPComment, poisoning the last durable record so every later pass
grew again.

**Fix:** the DB entry's `original_rel_path` is the source of truth, and it is now
seeded with the *true* original: `ensure_cache_entry` recovers it from the file's
own EXIF XPComment (decoded from the snapshot it already takes, no extra read) when
no cache entry exists, guarded so it never clobbers another file's key. So a
re-rename always rebuilds from `001.jpg`, and XPComment stays pristine. Inherent
limit: a non-JPEG (PNG/TIFF) can't hold XPComment, so after a *cache loss* its true
original is unrecoverable (same-folder re-runs are fine — the cache persists). Any
files ALREADY poisoned by the old bug can't be auto-healed (the pristine original
is gone from every record); a fresh copy or manual rename is the only recovery.
Regression tests: `tests/test_tag_rename_original.py`.

## Noted, deliberately not recommended as action items

- **`scripts/gui/tab_video.py` (1.5k lines) and `tab_runpod.py` (1.2k)** are
  the two biggest GUI modules. They are cohesive and freshly written; split
  them only when a feature forces a revisit, not preemptively.
- **`find_upscale_root`/`find_tag_root` O(n) scans and the `_upsert` SQL
  interpolation** were reviewed in 0.4.3 (item 12) and carry accurate
  in-code notes; still fine at real scale.
- **`StrictHostKeyChecking=no`** remains the right trade for RunPod's reused
  proxy endpoints; the reasoning is well documented at
  [remote_run.py:72-80](../scripts/remote_run.py).
- **Conciliation for videos** (matching `__upscaled__` video outputs back into
  the source tree) is a plausible future feature, not debt: the lineage table
  design would extend naturally if wanted.
