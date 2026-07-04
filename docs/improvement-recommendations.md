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

> **Status: DONE (2026-07-03; shipped in 0.4.0, download revised on
> 0.4.1-experimental).** `bootstrap.ps1` installs ffmpeg into `ffmpeg\bin` in
> BOTH install modes (`Install-Ffmpeg`), and `VideoTab._readiness_text` checks
> ffmpeg first and shows a "Not ready" message with the fix. `ffmpeg\` was added
> to `.gitignore` and to the installer's `[UninstallDelete]`; upgraders are
> covered because the installer deletes `.setup_complete` on every (re)install
> so the idempotent bootstrap re-runs.
>
> The 0.4.0 build downloaded from gyan.dev with the progress bar silenced (its
> rendering cripples `Invoke-WebRequest` throughput on PS 5.1), which left a
> non-technical user staring at a frozen window for ~10 min on gyan's slow
> (~275 KB/s) host. **Revised on 0.4.1-experimental** (user feedback): the
> download now uses **`curl.exe`** (ships in Windows 10/11; a real progress bar
> and much faster than IWR) and pulls from **BtbN's GitHub build** (github.com
> CDN, durable URL pinned to the ffmpeg 8.1 branch), with gyan.dev's
> release-essentials as a fallback. BtbN publishes no `.sha256` sidecar and
> rebuilds in place, so the BtbN path can't be hash-pinned; integrity there is
> HTTPS-to-github plus a functional post-extract check (`ffprobe -version` must
> run and report the expected version). The gyan fallback keeps its real
> SHA-256 sidecar check. Verified end to end on both source paths (curl
> download, extraction, functional check, idempotent second run) and both
> readiness branches (missing -> guidance; present -> proceeds to the API-key
> check; an install-after-launch is picked up on tab re-entry because only
> successful lookups are cached).

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

> **Status: DONE (2026-07-04, 0.4.2-experimental).** Added a `tests/` folder
> (pytest) and a `Tests` GitHub Actions workflow (`.github/workflows/test.yml`,
> windows-latest, system Python 3.12, `pip install pytest` only, no GPU stack).
> 109 tests, ~0.4 s, covering the pure logic called out below: `batch_upscale`
> fit math (`_skip_for_dims`, `compute_seedvr2_resolution`), `video_pipeline`
> (`plan_split` re-encode triggers + `check_drift`, the ffmpeg-free branches),
> `video_estimate` (box-fit + the aspect-ratio megapixel regression + queue/
> recommend), `pod/deadman.evaluate`, `notifications.resolve_settings` (legacy
> migration), and `db.py` (the video-table migration + the lineage round trip
> against a temp cache.db). Plus the two guards this item asked for: an **import
> smoke test** over every `scripts/*.py` that also asserts torch/timm/cv2 stay
> lazy (Remote-only-install guard + 0.2.5 packaging guard), and a **golden test
> for the `@@TBX@@` protocol** driving the real `ToolTab._filter_markers` parser
> through mid-line and chunk-split marker cases. Verified the whole suite passes
> in a fresh torch-free venv (the exact CI condition), not just the app venv.
> The workflow ignores `v*` tag pushes (those build the installer instead).

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

> **Status: DONE (2026-07-04, 0.4.2-experimental).** New isolated, fail-safe
> `scripts/funds_guard.py` (pure decision logic + a background `FundsGuard`
> poller) plus `runpod_client.account_balance()` (the legacy GraphQL
> `myself { clientBalance currentSpendPerHr }` query, browser User-Agent, returns
> None on any failure). Two protections, both OFF by default (0 = disabled), both
> fail-safe (unreadable balance never blocks a run):
> - **Start floor** — `RemoteSession._preflight_funds()` refuses to start (before
>   any pod is created) when finishing the run would drop the balance below the
>   floor. The Video tab passes its real queue estimate via `IMGTBX_RUN_ESTIMATE`;
>   other paths reduce to "is the balance already below the floor".
> - **Session cap + balance floor auto-stop** — `RemoteSession._arm_funds_guard()`
>   starts a poller on run start (the single chokepoint for every remote tool) that
>   stops the pod once this run's accrued cost crosses the cap OR the live balance
>   hits the floor. The run then follows the existing "pod stopped mid-run" path
>   (resume cache saved, continue later), the same graceful teardown the on-pod
>   dead-man's switch uses. Edge-triggered; `close()` stops the poller.
>
> Config (runpod section): `session_cost_cap_usd`, `balance_floor_usd` (both 0 by
> default), plus config-only `funds_poll_seconds` (default 60). Settings → Remote
> has a "Money safety" row (cap + floor spinboxes, tooltips). A **live Funds
> readout** on the far left of the shared bottom status bar shows the account
> balance, coloured by margin above the floor (the telemetry bands: blue >= +$10,
> green +$5-10, dark yellow +$1-5, red at/near the floor, grey = Unknown). Live on
> the Video Upscaler and RunPod tabs always; on Batch Upscaler / Tag & Rename only
> when "Run on remote pod" is on (grey "n/a" otherwise); hidden entirely on
> Conciliation and Settings. The RunPod tab's Region Refresh also updates it.
> 18 unit tests cover
> the pure logic (`session_cost`, `hours_until_depleted`, `start_blocked`,
> `evaluate`, and the poller's `check_once`). **Not yet exercised on a live billed
> pod** — the decision logic and wiring are tested/imported, but the actual
> stop-mid-run teardown interaction wants one live-pod confirmation.
>
> The within-segment progress-honesty concern (below) is left on the list — treat
> it separately from the funds work.

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

> **Status: DONE (steps 1-2; 2026-07-04, 0.4.2-experimental).** Everything the
> app downloads or ships is now pinned and verified where a trustworthy digest
> exists; step 3 (code signing) stays a deliberate "when it matters" item.
>
> - **`bootstrap.ps1` Python** now downloads via the robust `Get-Download`
>   helper (curl `--fail --retry`, real progress) and verifies the installer
>   against a **repo-pinned SHA-256** (`$PYTHON_SHA256`). Pinning the hash *in the
>   file* (not fetching a sidecar from python.org) is the actual tamper guard: an
>   attacker who swapped the server file can't change this git-tracked value.
>   python.org publishes only an MD5 for the installer, so the pinned SHA-256 was
>   computed locally from the MD5-confirmed download.
> - **SeedVR2 engine** is pinned to a **specific commit**
>   (`$SEEDVR2_COMMIT`, v2.5.24) instead of the moving `main` branch, so a fresh
>   install always gets the known-good snapshot this version was validated
>   against. GitHub regenerates archive zips (compression can change) so the bytes
>   can't be hash-pinned; the commit pin is the reproducibility guarantee. It too
>   now downloads via `Get-Download`.
> - **ffmpeg** already verifies (gyan.dev `.sha256` sidecar; BtbN has no sidecar
>   so it rests on HTTPS-to-github + a functional `ffprobe -version` check) - done
>   in 0.4.1.
> - **Updater**: CI (`build-installer.yml`) now emits a **`SHA256SUMS`** asset
>   next to `ImageToolboxSetup.exe`, and `updater.download_installer` fetches it
>   and **verifies the installer's SHA-256 before launching** (on top of the
>   existing size check). A mismatch aborts and deletes the file; a release with
>   no `SHA256SUMS` (older builds) cleanly falls back to the size check.
>   `updater.parse_sha256sums` / `sha256_of_file` are pure and unit-tested
>   (`tests/test_updater_integrity.py`, 11 tests: parsing formats, streaming hash,
>   and the download gate's pass / mismatch-abort / no-sums-fallback / size paths).
> - **Ollama** (`OllamaSetup.exe`) is left unpinned: it is optional, its download
>   URL is a moving "latest", and it's out of this item's scope.
> - **Code signing (step 3)** remains unaddressed by design (cost vs. a free
>   personal project); noted below as a future "when it matters" item.

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

> **Status: DONE (2026-07-04, 0.4.3-experimental).** New `scripts/runner_common.py`
> (stdlib-only, torch-free) now holds the genuinely-shared runner scaffolding, and
> all four runners plus were migrated to it:
> - `load_config()` + `APP_ROOT`, `harden_stdout()`, the `@@TBX@@` protocol
>   (`GUI_MARKER` / `stdin_is_piped()` / `GUI_MODE` / `gui_event()`), `fmt_duration`
>   / `fmt_mmss` / `fmt_hhmmss`, `get_image_dimensions()` + the 5 header parsers,
>   `is_oom_error()`, and `remote_pod_stopped(session)`.
> - Each runner re-exports these under its old local names (`_gui_event`,
>   `get_image_dimensions`, `_is_oom_error`, ...), so call sites and any external
>   attribute access are unchanged - a pure move.
> - **The drift the item called out is fixed**: `harden_stdout()` (the UTF-8
>   stdout reconfigure) is now applied by **every** runner, not just the video one.
> - **Consolidation surfaced and fixed a latent bug**: both `get_image_dimensions`
>   copies mis-parsed lossy-WebP (VP8) headers (wrong byte skip + a spurious +1),
>   returning wrong non-zero dimensions; because they returned without raising,
>   tag_and_rename's Pillow fallback never corrected it. The unified reader is a
>   superset (all 5 header formats + a Pillow fallback) and is tested against real
>   Pillow-written images across formats.
> - **Deliberately left per-runner** (divergent by design, NOT pure moves): the
>   session loggers (`batch_upscale.Logger` / `tag_and_rename._TeeOutput` /
>   `conciliate.Logger` - different tee strategies and user-visible `log_`/`tag_`/
>   `conc_`/`video_` filenames); the stdin control loops (`PauseController` vs
>   `RemoteControl` - different command sets and pause semantics); the one-line
>   `send_notification` wrappers (differ only by username); `runpod_provision`'s
>   config loader (a specialised variant that returns the validated `runpod`
>   section, not the whole config); and `video_estimate.fmt_duration` (a different
>   colon-separated format).
> - 35 tests (`tests/test_runner_common.py`): the fmt helpers, OOM classifier,
>   `remote_pod_stopped` (incl. fail-safe), the `gui_event` wire format (plain +
>   raw-stream-when-wrapped + no-op), the image reader across formats + fallback,
>   and a re-export check that the same functions back every runner. Full suite 192
>   passing.
>
> `toolbox_gui.py`'s own copies (item 6) are a separate, larger job (a GUI package
> split) and are not part of this move.

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

> **Status: DONE (2026-07-04, 0.4.3-experimental).** `toolbox_gui.py` went from
> ~8,400 lines to a **65-line entry-point shim**; the GUI now lives in a
> `scripts/gui/` package of 14 focused modules, built bottom-up so imports never
> cycle:
> - `gui/common.py` (foundation: paths/version, config.json + gui_settings.json,
>   the funds/mqtt/ollama helpers, `GUI_MARKER`),
> - `gui/widgets.py` (Tooltip, ProgressBar, TelemetryRow, LogPane, ConsoleBuffer,
>   LogViewer, `_ScrollFrame`, sanitize/_fmt_eta/_log_hms),
> - `gui/comparison.py` (ComparisonWindow + VideoComparisonWindow),
> - `gui/filmstrip.py` (FilmStrip), `gui/tooltab.py` (ToolTab base),
> - one module per tab (`tab_upscale`, `tab_tag`, `tab_settings`, `tab_runpod`,
>   `tab_conciliate`, `tab_video`) + `gui/dialogs.py` (UpdateDialog),
> - `gui/app.py` (the App window + `main()`).
>
> `toolbox_gui.py` stays the launch path (`Image Toolbox.cmd` / bootstrap /
> installer run it): it arms crash logging **before** importing the package (so an
> import-time failure still logs + shows a dialog) and re-exports the public API
> (App, main, APP_VERSION, GUI_MARKER, ToolTab, funds_color, fmt_funds, ...) so
> `import toolbox_gui` callers and the tests are unchanged. The move was pure (no
> behaviour change): tabs talk to App only via `self.app` at runtime, so no tab
> imports the app module.
>
> **The packaging trap was handled up front:** `installer/ImageToolbox.iss` now
> ships `..\scripts\gui\*.py` (the top-level `scripts\*.py` glob is non-recursive),
> and the import smoke test (item 2) sweeps every `gui.*` module, so a subpackage
> module left out of the installer fails CI. Verification at every stage: a
> package-wide `pyflakes` undefined-name sweep (catches a moved-global-not-imported
> that tests would miss since Python resolves function-body globals lazily), the
> full suite (now 205, sweeping the new modules), and a headless `App()` that
> builds/selects/renders all six tabs and tears down cleanly.
>
> **Deferred (separate, behaviour-changing follow-ups, NOT part of the pure
> move):** making `VideoTab` subclass `ToolTab` (it still has its own copy of the
> subprocess plumbing / `@@TBX@@` parser), and factoring the shared
> `is_dirty`/`_collect`/`revert` dirty-tracking out of `SettingsTab`/`RunPodTab`
> into a common base. Both are noted below and want their own change.

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

> **Status: DONE (2026-07-04, 0.4.3-experimental).** New `scripts/debug_log.py`:
> `debug_log(msg, exc=None, tb=False)` appends one timestamped, source-tagged line
> to `logs/debug.log`. It is itself fail-safe (any internal error swallowed, so it
> can never reintroduce a crash into a fail-safe handler) and size-capped (rolls to
> `debug.log.1` past 2 MB so a long-lived install can't grow it without bound). The
> source tag (entry-point script name) distinguishes the GUI from each subprocess
> runner, all of which append to the same file. Stdlib only.
>
> The **priority handlers named in this item** were routed through it (not all 66:
> the interesting persistence + money-adjacent ones): `EligibilityCache.save` and
> `.record_lineage` (batch_upscale), `save_cache` and the tag-lineage recording
> (tag_and_rename), the two video-table **DB migrations** (`db._migrate_video_tables`
> / `_ensure_video_columns`), the pod/tunnel/funds-guard **teardown** in
> `RemoteSession.close`, and the live **MQTT publish** (`MqttClient.publish`,
> rate-limited to one line per broken streak so a down broker can't flood the log).
> Each importer pulls `debug_log` **guarded** (`try: from debug_log import debug_log
> / except: no-op`) so an old install missing the module can't break the cache /
> runner / MQTT layers. Handlers that already print to the live log pane
> (`save_cache`) now also persist the line, since the pane is ephemeral across
> sessions. 9 tests in `test_debug_log.py` (append/format/exc/tb/rollover/never-raises).
> The GUI's own `except: pass` blocks (geometry/thumbnail persistence, lower
> stakes) were left untouched: the helper is in place for them if a field report
> ever points there.

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

> **Status: DONE (2026-07-04, 0.4.3-experimental).** `db.py` grew a module-level
> reentrant lock (`_LOCK = threading.RLock()`) and a `@_locked` decorator, applied
> to **every** helper that touches the shared connection (the video cache/queue,
> roots, gpu_perf, lineage, legacy import) so each helper's whole read-modify-write
> runs atomically instead of interleaving with another thread's statements on the
> single connection. `get_conn()` now double-checks under the lock and defers to a
> new `_open_conn()` so two threads can't both build the connection. Chose the lock
> over per-thread connections deliberately: callers (e.g. `EligibilityCache`) cache
> the connection object across threads, which a `threading.local` connection would
> defeat.
>
> Two helpers are **intentionally not locked**: `content_hash` (pure file I/O, no
> conn) and `hash_file_cached` (it reads the whole file, a multi-GB video in the
> Video path, and holding the global DB lock across that read would stall every
> other DB op; its only race is two threads writing the *identical* memoised digest,
> which is harmless, and SQLite's own mutex keeps each statement safe). Reentrant so
> the nested helper calls (`upsert_video_* -> _upsert`) don't deadlock. 3 tests in
> `test_db.py` (concurrent distinct-row writers all land; concurrent same-row
> upserts don't raise a duplicate-PK IntegrityError, which they *do* without the
> lock, verified; reentrancy doesn't deadlock).

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

> **Status: DONE (2026-07-04, 0.4.3-experimental).** New `scripts/config_store.py`
> splits the settings across two files at the app root: the tracked `config.json`
> (which now NEVER holds a secret value: every secret key is present but blank) and
> an untracked `config.local.json` overlay (added to `.gitignore`) that holds ONLY
> the secret fields. `config_store.load()` deep-merges the overlay over the base so
> the rest of the app still sees one merged dict; `config_store.save()` does the
> reverse (secret fields to the overlay, a secret-free copy to `config.json`, base
> written first so a failed overlay write can never leak). `SECRET_FIELDS` =
> `runpod.api_key`, `mqtt.password`, `notifications.{discord_webhook_url,
> telegram_bot_token, ntfy_token}`, and the legacy `upscale.discord_webhook_url`.
>
> Wired through the three load sites (`gui/common` load+`save_config`,
> `runner_common.load_config`, `runpod_provision`) so both the GUI and every
> subprocess runner read the merged view. A one-time GUI migration
> (`App._migrate_secrets_to_overlay`, guarded by `config_store.base_has_secrets`)
> moves an existing install's secrets out of `config.json` into the overlay on the
> next launch and blanks them in the tracked file; idempotent afterwards. The
> `skip-worktree` bit + the webhook git clean filter are now redundant belt-and-
> suspenders (left in place, harmless). 12 tests in `test_config_store.py` incl. the
> leak invariant (no secret literal appears in the written `config.json`) and the
> full migration flow.
>
> **Deferred (cosmetic, needs the skip-worktree dance on a secret-laden working
> copy, so out of scope for this automated change):** physically dropping the
> now-blank legacy `upscale.discord_webhook_url` and the dead
> `runpod.max_price_per_hour_*` keys from the *tracked* template. `split_secrets`
> already blanks the webhook on every save, so it never carries a value; the key
> removal is purely tidiness.

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
