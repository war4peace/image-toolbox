# Codebase review: improvement & optimization recommendations

Review of the full repository (docs, `scripts/`, `pod/`, `tools/`, root) on the
`0.4.0-experimental` branch, 2026-07-03. Ordered by importance/urgency:
items 1-4 should land before (or with) the 0.4.0 release; 5-9 are structural
debt worth scheduling; 10-12 are lower-priority polish.

What is already strong, so it stays that way: the design docs
(`docs/video-upscaler.md`, `docs/runpod-notes.md`) are unusually good and full of
verified-live findings; the "never touch originals / fail safe" philosophy is
applied consistently; the GUI-to-runner subprocess seam is clean and reused by
every tool; resume caches, the dead-man's switch, and the watchdog show real
defensive design. The recommendations below are about closing the gaps around
that core, not changing its architecture.

---

## 1. Release blocker: the Video Upscaler's local ffmpeg is never installed

> **Status: DONE (2026-07-03).** `bootstrap.ps1` now installs a pinned ffmpeg
> (`Install-Ffmpeg`: gyan.dev release-essentials 8.1.2, verified against its
> published SHA-256 sidecar, with the moving latest-release build as fallback
> when the pinned package ages off gyan.dev) into `ffmpeg\bin` in BOTH install
> modes; a failure warns and continues (the readiness strip is the safety net).
> `VideoTab._readiness_text` now checks ffmpeg first and shows a "Not ready"
> message with the fix. `ffmpeg\` was added to `.gitignore` and to the
> installer's `[UninstallDelete]`. Upgraders are covered automatically: the
> installer already deletes `.setup_complete` on every (re)install, so the
> idempotent bootstrap re-runs and adds ffmpeg. Verified end to end: real
> download + hash check + extraction (ffmpeg/ffprobe run, LICENSE shipped),
> idempotent second run, and both readiness branches (missing -> guidance,
> present -> proceeds to the API-key check; an install-after-launch is picked
> up on tab re-entry because only successful lookups are cached).

**What:** `video_pipeline.find_ffmpeg()` resolves `$IMGTBX_FFMPEG_DIR` ->
`<APP_ROOT>/ffmpeg/bin` ("downloaded by bootstrap.ps1") -> PATH, and the design
doc (video-upscaler.md sections 6.4 and 13) settled the bundling plan (gyan.dev
`release-essentials` zip, pinned, `.sha256` verified, both install modes). But
**`bootstrap.ps1` contains no ffmpeg step at all** (zero matches for "ffmpeg" in
the file). It works on the dev machine because ffmpeg is on PATH; on a user
install the first video scan raises `FfmpegNotFound`.

**Compounding it:** the Video tab's readiness strip
(`toolbox_gui.VideoTab._readiness_text`) checks API key, SSH key and volume,
but not ffmpeg, so the failure surfaces as a raw error mid-scan instead of a
"Not ready" message with guidance.

**Do:**
- Add the pinned ffmpeg download + SHA-256 sidecar verification to
  `bootstrap.ps1`, unconditionally in both install modes (the doc already
  specifies the exact build, URL shape and extraction: `ffmpeg.exe` +
  `ffprobe.exe` only, ship the GPL license file).
- Add an ffmpeg check as the first line of `_readiness_text` (it is a purely
  local check, cheaper than the RunPod API call that follows).

## 2. No automated tests and no test CI, at 22k lines and growing

The repo has no `tests/` directory and the only workflow is
`build-installer.yml`. Every regression so far was caught by live runs on
billed pods or by users. The codebase is now past the size where that scales,
and it already contains well-isolated pure logic that would be trivial to test:

- `batch_upscale._skip_for_dims` / `compute_seedvr2_resolution` (the fit math,
  including the portrait/transpose edge cases the comments describe),
- `video_pipeline.plan_split` / `check_drift` (the doc records real bugs here:
  the lying `nb_frames` header, the 29.46-vs-30 fps desync),
- `video_estimate` (the aspect-ratio under-estimate was exactly a unit bug),
- `pod/deadman.evaluate` (already has `--selftest`, i.e. a test wanting a home),
- `notifications.resolve_settings` (legacy-key migration),
- `db.py` migrations (`_migrate_video_tables`, `_ensure_video_columns`) against
  temp DBs, and the lineage record/lookup round trip.

**Do:**
- Add `pytest` with a `tests/` folder and a GitHub Actions workflow that runs on
  every push/PR (Windows runner, no GPU needed for any of the above).
- Include one **import smoke test**: import every `scripts/*.py` in a torch-free
  environment. This directly guards two past incident classes: the Remote-only
  install breaking on an eager torch import, and the installer shipping a broken
  module set (the 0.2.5 packaging bug).
- Add a golden test for the `@@TBX@@` stdin/stdout event protocol (one parser,
  several emitters; see item 5), since the GUI and three runners must agree on it.

This is the highest-leverage structural investment: nearly every other item in
this list becomes safer to do once a test net exists.

## 3. Money safety: implement the funds-floor safety net (roadmap #1 remainder)

`docs/future-features.md` #1 marks this as the one open piece, API verified live
2026-06-21. The app now runs **multi-hour billed video jobs** (a 1-hour 1080p
video is ~$28-46 of GPU time), which raises the stakes well beyond the image
path that the dead-man's switch was designed for. The time/idle switch protects
against a *forgotten* pod; nothing protects against a *working* pod draining the
account (or a run started with less balance than the estimate).

**Do:** the design already written in future-features.md: poll
`myself { clientBalance currentSpendPerHr }` via the legacy GraphQL endpoint
(browser User-Agent, as `runpod_client` already does elsewhere), refuse to start
when the estimate exceeds the balance floor, and auto-stop when a session
crosses a configurable cap. One isolated fail-safe helper; no balance readable =
skip the checks. The Video tab's cost estimator gives you the "refuse to start"
number for free.

Related, verify on a live pod: the within-segment progress honesty problem
(video-upscaler.md 15.8, "shipped bar is not good enough") has had a burst of
recent commits (`1669944`, `9a00c4c`, `67dc087`); the doc still says a real fix
is required. Either confirm the new time-per-phase bar closes it and update the
doc, or keep it on the list. A user interrupting a healthy billed run because
the bar looks hung is a money bug, not a cosmetic one.

## 4. Supply-chain integrity: verify what you download and ship

Nothing the app downloads or ships is integrity-checked today:

- `bootstrap.ps1` downloads the Python runtime, the SeedVR2 engine zip, and
  `OllamaSetup.exe` with plain `Invoke-WebRequest` and no hash check.
- `updater.py` downloads `ImageToolboxSetup.exe` and verifies only the **byte
  size** against the GitHub API, then launches it.
- The installer itself is unsigned (hence the SmartScreen note in the README).

Any of these is a silent code-execution path on the user's machine if the
download is tampered with or corrupted.

**Do, in increasing order of effort:**
1. Pin exact versions and verify SHA-256 in `bootstrap.ps1` where the publisher
   provides digests (python.org does; the planned gyan.dev ffmpeg has a
   `.sha256` sidecar; for the SeedVR2 zip pin a commit hash instead of a branch).
2. Have CI emit a `SHA256SUMS` asset with each release and make `updater.py`
   verify the installer against it before launching (both come from the same
   GitHub release, so this mainly protects the download path and partial files,
   but it is nearly free).
3. Longer term, a code-signing certificate: it fixes SmartScreen *and* gives the
   updater a real signature to check. Costly for a free personal project, so
   explicitly a "when it matters" item.

## 5. Extract a shared runner-support module (the duplication is now measurable)

The three image-era runners and the video runner each carry private copies of
the same infrastructure. Confirmed by search:

| Duplicated piece | Copies |
|---|---|
| `_load_config` / APP_ROOT resolution | 5 (batch_upscale, tag_and_rename, batch_video_upscale, runpod_provision, toolbox_gui) |
| `_gui_event` + `_stdin_is_piped` + the `@@TBX@@` protocol | 3 runners (+ the GUI parser twice, see below) |
| `Logger` (timestamped tee to logs/) | batch_upscale, conciliate (tag_and_rename has a variant) |
| `fmt_duration` / `fmt_mmss` / `fmt_hhmmss` | 3 files |
| `get_image_dimensions` + the 5 binary header parsers (~90 lines) | batch_upscale, tag_and_rename |
| `send_notification` wrapper, `_remote_pod_stopped`, `_is_oom_error` | batch_upscale, tag_and_rename |
| Pause/stdin control loop | `PauseController` (batch_upscale) vs `RemoteControl` (tag_and_rename) vs the video runner's own handling |

The cost is drift, and it is already visible: the UTF-8 stdout hardening
(`sys.stdout.reconfigure(encoding="utf-8", errors="replace")`, added after a
non-ASCII glyph crashed a run) exists **only** in `batch_video_upscale.py`; the
other runners rely on the GUI setting `PYTHONIOENCODING` and stay crashable in
odd headless setups. 0.3.8 already proved the pattern by unifying notifications
into `notifications.py`.

**Do:** create `scripts/runner_common.py` (config load + `APP_ROOT`, the GUI
event emitter, `Logger`, the pause/stdin controller, the fmt helpers, OOM
detection, the remote-pod-stopped check, stdout reconfigure) and migrate one
runner at a time. Pure moves, no behavior change; the installer's
`..\scripts\*.py` glob picks the new module up automatically.

## 6. Split `toolbox_gui.py` (8,170 lines) into a package

CLAUDE.md still describes it as ~2.6k lines; it has tripled. It now holds ~30
classes: generic widgets (Tooltip, ProgressBar, TelemetryRow, LogPane,
FilmStrip, two comparison windows), six tabs, the update dialog, and the app
shell. Concrete costs beyond navigation:

- **`VideoTab` re-implements `ToolTab`'s subprocess plumbing** (`_launch`,
  `_pump`, `_poll`, `_filter_markers`, `_on_marker`, `running`, `send`,
  `terminate`) as private copies instead of subclassing. Two parsers for the
  same `@@TBX@@` protocol is exactly how the GUI and a runner drift apart.
- `SettingsTab` and `RunPodTab` each carry their own
  `_collect`/`_snapshot`/`is_dirty`/`_save`/`_refresh_save_indicator`/`revert`
  dirty-tracking implementation; a shared base would halve it.

**Do:** move to `scripts/gui/` (widgets.py, filmstrip.py, comparison.py, one
file per tab, app.py), keep `toolbox_gui.py` as a thin entry point for
compatibility with `Image Toolbox.cmd` and the installer. **Note the packaging
trap:** `installer/ImageToolbox.iss` ships `..\scripts\*.py` (non-recursive); a
subfolder needs its own `[Files]` entry or `recursesubdirs`. That glob silently
missing a module is a known past incident (0.2.5), and the import smoke test
from item 2 is the guard.

## 7. Give the fail-safe `except Exception: pass` blocks a debug trail

There are 66 silent `except Exception: pass` blocks across `scripts/` (19 in
the GUI alone, 11 in tag_and_rename). The fail-safe philosophy is right for
this app; the problem is only that a swallowed failure leaves no trail, so a
"cache never persists", "MQTT silently dead", or "lineage never recorded" class
of bug is invisible until its downstream symptom appears (e.g.
`EligibilityCache.save` swallows every DB error, and the user finds out only
when a stopped batch does not resume).

**Do:** add a tiny `debug_log(msg)` helper (append to `logs/debug.log`,
timestamped, itself fail-safe) and route the interesting fail-safe handlers
through it instead of bare `pass`. Prioritize handlers guarding persistence
(cache saves, lineage, DB migrations) and money-adjacent paths (pod teardown,
`ensure_stopped`). This preserves the never-crash behavior while making field
reports diagnosable, and it matches the user preference for richer logs.

## 8. Harden the shared SQLite connection against GUI threading

`db.get_conn()` hands one process-wide connection out with
`check_same_thread=False`, and the Video tab uses it from short-lived scan /
prepare worker threads. Safety currently rests on a convention ("the GUI runs
one background op at a time") that nothing enforces; two overlapping helpers
from different threads can interleave statements inside each other's implicit
transactions.

**Do (cheap):** put a module-level `threading.Lock` around the write helpers in
`db.py` (they are all short), or switch `get_conn` to per-thread connections
via `threading.local` (WAL already supports concurrent readers plus one
writer). Either is a small, mechanical change; do it before more GUI features
grow background threads.

## 9. Move secrets out of the tracked `config.json`

`config.json` is tracked as a credential-free template, locally
`skip-worktree`'d, and the README carries a "maintainers: remember to scrub"
note. That works only as long as one person remembers two non-obvious git
states; it is a standing leak risk (RunPod API key, MQTT password, Telegram bot
token, webhook URLs all live in that file at runtime).

**Do:** load an untracked overlay at startup, e.g. `config.local.json`
deep-merged over `config.json`, and have the Settings save path write secret
fields (api_key, mqtt password, telegram token, webhook URLs, ntfy token) only
to the overlay. Add `config.local.json` to `.gitignore`. The tracked file then
never contains a secret, the skip-worktree dance disappears, and installer
upgrades keep working. While in there: drop the legacy
`upscale.discord_webhook_url` key from the tracked template (it migrated to
`notifications` in 0.3.8).

## 10. Fix documentation drift (it directly degrades the AI-assisted workflow)

CLAUDE.md is the context every future session starts from, and it is now
materially wrong: `toolbox_gui.py` listed at ~2.6k lines (actual 8.2k); the
entire Video Upscaler cluster is absent from the module table
(`batch_video_upscale.py`, `video_pipeline.py`, `video_estimate.py`,
`remote_video_engine.py`, `benchmarks.py`, `pod/bench_video.py`,
`pod/ram_probe.py`); no mention of the `VideoTab` or the separate RunPod tab.
README says "five tabs" (there are six) and its config-section table lacks
`runpod`, `video`, `mqtt`, `updates`, `notifications`... Meanwhile
`docs/video-upscaler.md` still opens with "Status: planning only. Nothing here
is built yet" above 800 lines of BUILT/DONE/measured results.

**Do:** one drift pass now (CLAUDE.md module table + feature list, README tab
count and config table, the video doc's status header), then add a release
checklist to CLAUDE.md or the tag workflow: bump `APP_VERSION` (still "0.3.9"
on this branch), update CLAUDE.md + README for new modules/tabs, write the tag
message. Cheap discipline, big payoff for a repo maintained through AI sessions.

## 11. Pod cold-start optimization (billed dead time on every run)

Already on the TODO list in `docs/runpod-notes.md` but worth promoting now that
every video run pays it: engine load was measured at ~97-354 s per pod,
dominated by reading the 16 GB DiT from the network volume plus a safetensors
hash-validation pass that reads it a second time. At B200/H200 rates that is
$0.30-0.60 of pure loading per pod, and it also stretches the "is it hung?"
window that item 3 worries about.

**Do (in the noted order):** skip the safetensors hash validation on the
trusted volume; optionally copy the models volume -> container-local NVMe once
at pod start (overlaps with worker startup) and load from local disk. Both are
pod-side only, no local changes.

## 12. Smaller findings (grouped, low urgency)

- **`db.find_upscale_root` / `find_tag_root`** scan the whole roots table in
  Python for case-insensitive matching. Fine at current scale; if roots ever
  number in the hundreds, store `_norm(path)` in a column and index it.
- **`_upsert` in db.py** builds SQL from field-name kwargs. All call sites are
  internal today; keep it that way (a docstring warning suffices).
- **SSH uses `StrictHostKeyChecking=no`** for pods. Reasonable for ephemeral
  hosts (the key is unknowable up front), but `accept-new` gives the same UX
  with protection after first contact; RunPod also exposes the host key via the
  API for the truly paranoid path. Low risk, note-and-decide.
- **README run-from-source** pip line omits `paho-mqtt` (bootstrap installs
  it); harmless because the import is lazy, but add it for parity.
- **`video-upscaler.md` growth:** at 1,145 lines it is becoming a mix of design
  doc and lab notebook. Consider splitting measured benchmark passes into
  `docs/video-benchmarks.md` so the design contract stays readable.

---

## Assumptions made during this review

- The recent progress-bar commit burst (`74b6083` through `1669944`) was taken
  as work-in-progress on item 3's honesty problem, not as its confirmed fix.
- `config.json`'s local skip-worktree state and dev RunPod key handling were
  taken from the project notes rather than inspected.
- `seedvr2/` was treated as vendored third-party and not reviewed.
