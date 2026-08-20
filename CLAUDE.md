# CLAUDE.md

Guidance for working in this repository.

## Conventions

- **Avoid em-dashes (—) where possible; prefer an alternative** (a colon, comma,
  parentheses, or "|" in compact UI labels). Applies to UI strings, code comments,
  and docs.

## What this is

**Image Toolbox** — an AI-leveraged image toolbox for Windows that **upscales**
low-resolution photos and **describes & renames** them using a local vision
model, and now also **upscales videos** (SeedVR2, on a rented RunPod GPU). Built
to revive personal photo collections and old digital-camera pictures.

Two design promises shape everything:

- **The upscaler never modifies source files** — it writes new images to a
  separate output folder that mirrors the source tree.
- **Dependency-light & local** — the GUI is pure standard-library tkinter;
  everything runs on the user's machine (no ComfyUI, no server). The upscaling
  pipeline runs **in-process**; tagging talks to a local Ollama server over HTTP.

**Requirements:** Windows 10/11 (64-bit), an NVIDIA GPU (8 GB VRAM min) with a
CUDA build of PyTorch. The app is currently **Windows-only** (PowerShell
bootstrap, tkinter GUI, Windows paths, `CREATE_NO_WINDOW`).

## Current features

**Batch Upscaler** — high-quality SeedVR2 upscaling capped to a selectable
Resolution Target (4K/2K/1080p). Skips images already near the target
(skip-cutoff) and images the engine cannot round-trip (0.5.9, transparency /
several pages / 16-bit: see "Image variants left as-is"). **Carries the original's
metadata onto the output** (0.5.9, capture date / camera / lens / GPS / copyright,
with Orientation normalised and the stale thumbnail dropped: see "Metadata carried
across"). Resilient long runs: a
file cache in `scans/` resumes a stopped
batch; corrupt/missing files are detected, logged and skipped; a second pass
re-scans for files that appeared mid-run. Live thumbnail wall (the current image
has a blue frame; each finished thumbnail is framed **green** when an upscaled
counterpart exists — i.e. it can be compared — or **red** on failure, via `RESULT`
events, 0.2.9), two-row status, progress bar, ETA. Pause/resume/stop (stop
finishes the current image first). **Pause hands the GPU back** (0.5.2): it
unloads DiT + VAE (and the auto-straighten CNN) after the current image and
`torch.cuda.empty_cache()`s, so the card is free for other work, then reloads
lazily on Resume with the queue intact. Clearing `UpscaleEngine._runner_cache`
alone frees nothing: seedvr2 keeps a SECOND, module-level `GlobalModelCache`
(keyed `cli_dit`/`cli_vae`) that owns the real references, so the unload goes
through its `remove_dit`/`remove_vae` (`UpscaleEngine.release`). Hooks fire from
`PauseController.check()` on the MAIN thread around the blocking wait, never from
the stdin watcher thread (which fires mid-upscale). Local runs only: a remote
run's models live on the pod. Measured: 16.6 GB returned on a 3090.
Works with mapped network drives. **Auto-straighten before upscaling** (0.2.7, on
by default) rotates sideways photos upright *first*, so the result respects the
4K-fit target (3840 wide OR 2160 tall) in its final orientation — without it, a
sideways photo is upscaled on the wrong axis and stops fitting once Tag & Rename
straightens it. The source is never touched: a temp copy is rotated, upscaled,
then deleted. Uses the same CNN/threshold as Tag & Rename (`orientation.py`); the
eligibility/skip check is orientation-aware. Toggle + threshold in Settings →
Upscaling.

**RAW and DNG input** (0.6.0, future-features #19) — the Batch Upscaler accepts
`.dng .cr2 .cr3 .nef .arw .orf .rw2 .raf .pef .srw` and renders each to a viewable
JPEG. **The app renders RAW, it does not develop it**: a raw developing UI
(exposure/WB/curve) is permanently out of scope, so the render is either the
camera's own embedded full-size JPEG preview (S2) or a LibRaw demosaic with fixed
defaults (S1), never our opinion. Three measured findings shaped it, from 24 CC0
camera files 2004-2020 (`docs/raw-preview-survey.csv`). **(1) A RAW is never
eligible for upscaling**: at the shipped 4K target + 66% cutoff, ZERO of the 24
would ever be upscaled (1 with the cutoff off), because RAW is by nature a
high-resolution format and this app targets low-resolution photos. So a RAW is
**exempt from the size skip** and the size check only chooses **render-only** vs
**render-then-upscale** -- without that the feature produces nothing at all, for
anyone. Renders are counted and reported separately (`N RAW rendered (already at
target)`) and are excluded from the ETA average AND the watchdog, since a render is
~1000x faster than an upscale and would otherwise hand the watchdog a "healthy
rate" no real upscale could match. **(2) The IFD-0 trap is real** (15/24): a
TIFF/EP raw puts a small preview in IFD 0 by spec and Pillow sniffs content, not
extension, so the dimension reader's Pillow fallback does not fail, it answers
confidently and WRONGLY -- and not always obviously (a 20D CR2 reads as 1536x1024,
a 5D as 2496x1664). So a RAW extension **never reaches Pillow**:
`get_image_dimensions` branches to LibRaw first and an unreadable RAW returns
(0,0) and is reported unreadable, exactly like a corrupt JPEG. `sizes.iwidth/iheight`
are **pre-flip** (verified: `postprocess(user_flip=5|6)` swaps the shape, `0|3` do
not), so `upright_size()` applies the swap in one place; the scan's header read and
the render can differ by the 2% preview tolerance, so `run_pass` re-derives the size
from the rendered pixels and decides there. **(3) The preview does NOT carry the
camera's EXIF intact** (the plan assumed it did): a preview block is typically 12+4
tags with the exposure triple but **no DateTimeOriginal and no GPS**, while the
container's own block is rich but unreadable by Pillow for CR3/ORF/RW2/RAF/ARW/SRW
(9 of 24). Neither covers the set, so metadata is **merged from both**,
container-first, using exif_copy's own "copy what is missing, keep what is present"
rule: 24/24 outputs carry a capture date, against 15/24 or 23/24 from either source
alone. Raw-**development** tags are stripped first (the DNG range from 50706 up): a
DNG block measures 79-317 KB against a JPEG APP1 ceiling of 64 KB, so before that
strip exif_copy correctly refused the whole block and those files came out with NO
metadata. The chain is **decode -> auto-straighten -> upscale, passing arrays**, with
**exactly one** temp written and only when the file is actually upscaled; that temp
is **lossless PNG, never JPEG** (the easy accident, since `.jpg` is what the output
is) and carries sanitised EXIF with Orientation forced to 1 -- load-bearing, because
`upscale_engine._load_image` runs `exif_transpose` on whatever it is given. Carrying
the block is also what lets the **remote** path work unchanged: the decode happens
LOCALLY even for a pod run, so the pod receives an ordinary PNG and never learns RAW
exists. Output is **`<stem>_raw.jpg`**, not `<stem>.jpg`: shooting RAW+JPEG is
ordinary and `IMG_1234.CR2` + `IMG_1234.JPG` would otherwise both map to one output,
where the first processed wins and the second is silently counted "already
upscaled". **Tag & Rename ignores RAW** (writing `ImageDescription` into a
proprietary container is the source mutation the app forbids) and **Conciliation
never archives or deletes a RAW original** -- an explicit, tested guard, not a side
effect of an extension list, because a rendered JPEG is not a superset of a negative
but one 8-bit interpretation of it with the sensor data gone. A RAW never offers
**Compare** (the window draws the ORIGINAL as its "before" half, which Pillow cannot
open), and the film strip draws RAW cells from the embedded preview (~45 ms) instead
of leaving them blank. `rawpy` ships in all three install modes; its wheel carries
the LibRaw LGPL licence itself, so nothing needs copying. Conciliation's
fold-the-render-in-alongside (the other half of #19's decision 8) is **dropped**, not
pending: it buys a filing convenience with a new action type inside the app's only
destructive tool, and since a RAW is never upscaled the render belongs in the output
folder like everything else the app produces. **So what shipped is in practice a RAW
RENDERER, and the upscale half is scaffolding**: it starts working the day a target
exists that makes a RAW small, which is why the revisit trigger recorded in
`docs/dropped-ideas.md` is an 8K resolution target. See `raw_decode.py` and
`tests/test_raw_input.py`.

**Tag & Rename** — analyses each image with a local Ollama vision model, writes
a description into EXIF, and renames to `OriginalName_Condensed_Description.ext`.
**Auto-straighten** (on by default) uses a small CNN to rotate sideways photos
upright before tagging; only confident calls act, ambiguous/upside-down images
are left alone and logged. Selectable description language, force-tag /
force-rename, and **one-click Undo** (every change is recorded before anything is
modified). Already-tagged files are skipped on re-runs. **Point it at the UPSCALED
folder**, not the source: it is step 2 of upscale -> tag -> conciliate, and
Conciliation looks for the tag/rename cache under the *processed* root
(`find_tr_cache(processed_root)`). Before 0.5.9 pointing it at the source happened to
work, because the low-res originals fell below `MIN_WIDTH`/`MIN_HEIGHT` (3840/2160)
while the 4K files inside `__upscaled__` passed; #16's derived-directory pruning ended
that accident. So the tab **pre-fills "Photo folder"** from the first rule that has a
value (`TagTab.restore_defaults_if_empty`): the field's own value, then the pinned
`tag_folder` default (which must EXIST, else fall through: a vanished photo *source* is
meaningless), then the **Batch Upscaler tab's live "Save upscaled to"**, then the pinned
`upscale_output` default (those two taken as-is, since the upscaler CREATES its output
folder on the first run, so the useful suggestion is normally a folder that is not there
yet), else empty. See `docs/tag-and-rename.md` ("Which folder to point it at"). **Pause / Resume**
(0.5.2) shares ONE dual-purpose button with the old "Resume after error": the
runner's `RemoteControl` is Pause during normal work, Resume while paused, and
Resume-after-error while an outage holds the run (those states are mutually
exclusive in its loop). The GUI always sends a bare `p` and the RUNNER assigns
the meaning from `_waiting_resume` (`_dual_button`), because the button's label
is a replica that lags the run by one pipe hop and a click can land exactly as an
outage begins; an outage also clears a pending pause, so resolving it can't drop
the run straight back to sleep. Pausing unloads the Ollama vision model
(`keep_alive=0`) AND the straighten CNN, local runs only. The tab relabels from
the runner's `PSTATE` event; Undo disables the button (no models, no pause
check). The image sent to the
model is **downscaled to a max longest edge** (0.3.3, `tagging.max_image_px`,
default 1280 px, Settings → Tag & Rename; source files are never touched) — a
full-res photo emits so many vision tokens that it OOMs a small-VRAM GPU into an
HTTP 400 (every ≤24 GB remote card crashed on the first 2272×1704 image until
this), and downscaling also speeds up tagging everywhere with no loss for
describe-and-title use. EXIF orientation is applied to the in-memory copy so the
model sees the photo upright. See `_encode_image_for_model` in `tag_and_rename.py`.
The Ollama context is also **capped** (`tagging.ollama_num_ctx`, default 1280-safe
8192, sent as `options.num_ctx`): newer vision models declare a huge native context
(qwen3-vl = 256K) and Ollama sizes its KV cache off that, so uncapped it grabs
almost the whole card and thrashes — a measured `qwen3-vl:8b-instruct` ran 9:11/100
at 98% VRAM uncapped vs **2:37 at 43%** capped, no quality change. The **shipped
default vision model is `qwen3-vl:8b-instruct`** (0.5.5): a 100-image benchmark had
the qwen3-vl family beat qwen2.5vl:7b / minicpm-v / gemma3:4b at every tier (use the
`instruct` variants, never `thinking` — the tool feeds raw output into EXIF/filenames,
so a reasoning chain leaks in). See `docs/tag-and-rename.md` +
`docs/tag-rename-benchmarks.csv`.

**Comparison** (0.2.9) — a floating, resizable **original-vs-upscaled** window
(like the log window: one shared instance, geometry persisted as
`compare_geometry`). On the Batch Upscaler tab, **double-clicking a green
(comparable) thumbnail** opens it. Both images are drawn aligned on one canvas
split by a vertical **before/after wipe** (left = original, right = upscaled);
zoom (mouse wheel, centred on the pointer, fit … 400% of the upscaled native
pixels) and pan (drag, clamped to keep the view filled) are **shared**, so the
two halves always show the same region — making the quality gain directly
visible. Drag the divider handle to slide the wipe. Only the visible slice of
each side is decoded (Pillow `resize` with a float `box`); gestures render with a
fast filter and a crisp LANCZOS pass follows when they settle. **Esc backs out
exactly ONE level per press** (0.6.0), innermost first: **pinned lens -> lens mode
-> the window**. So a pinned lens takes three presses to leave, an unpinned lens
two, the plain wipe one. Each press undoes the most recent thing the user turned
on, which is the only ordering that never surprises them (the lens hint promises
"click or Esc to release", so a pin must win while it exists, and a lens the user
is looking through must not take the window with it). Leaving lens mode goes
through `lens_var` + `_on_lens_toggle`, exactly as the L shortcut and the checkbox
do, so Esc-off and L-off are one action and the remembered preference cannot
drift. Before 0.6.0 Esc released the pin and did nothing else, on the reasoning
that a window holding a large decoded pair should not vanish on a stray keypress;
re-opening is one double-click and the re-decode is well under a second, so that
cost was overstated. Pairing is
**current-run**: the upscaler's `RESULT` event carries the output path, which the
strip remembers (`FilmStrip._compare`). Double-clicking a red/unframed thumbnail
(or any Tag & Rename thumbnail) just opens the file. A **Lens** toggle (0.6.0,
future-features #14) adds the one comparison the wipe cannot make: it magnifies the
patch under the pointer as original AND upscaled **side by side**, so the eye
compares two things next to each other instead of remembering what the divider was
showing a moment ago. Four decisions define it. The **magnification is the real
upscale ratio**, not a hard-coded 4x, which puts the right-hand panel at exactly
1:1 with the file that was produced (a `crop`, not a resample, so the "1:1" label is
literally true); a same-size pair has no ratio to use and falls back to a fixed 4x,
labelled as such. **The wheel zooms the lens** through `LENS_ZOOMS` (1/2/4/8) on top
of that ratio, with Ctrl+wheel left as the escape hatch to the picture behind. That
was NOT in the first cut and the gap it left was severe: a fixed 180 px panel at a
fixed 1:1 is useless on a small pair, because a 320x240 -> 640x480 video in a
maximised window is ALREADY drawn at ~2.7x on the canvas, so the "magnifier" showed
the patch **smaller** than it appeared behind it, in a stamp you had to squint at. So
three things scale now: the base panel follows the window (`LENS_PANEL_FRAC` of its
width, not a constant), a zoom step grows the panel further until `lens_panel_size`'s
`LENS_FIT_W/H` cap says the panels would swallow the picture they magnify out of
(past that the zoom keeps rising by narrowing the patch instead), and
`lens_zoom_floor` starts the lens at least as strong as the view it sits on, applied
once per pair and once per switch into lens mode, deferred to the first draw that has
BOTH a real image and a realised canvas (the video window decodes its frames on a
thread, and the first render runs inside `__init__` at 1x1) and cancelled the moment
the user touches the wheel. Both panels are LANCZOS at every zoom, deliberately: the
same filter on both sides is what keeps the comparison fair, and nearest-neighbour on
the upscaled half would show it blocky against a smoothly interpolated original. It
is a **mode, not an overlay**: while it is on the divider is put
away and the canvas shows the upscaled image full-frame as the context to magnify out
of, because two pointer gestures on one canvas fight (the wipe wants the drag, the
lens wants the hover). It is **transient and pinnable**: it follows the pointer and
goes away with it, and a click (a press-release that did not move, `CLICK_TOL`) pins
it for inspecting one spot with the hand off the mouse, or for a screenshot; Esc
releases. And the **video window inherits it whole** (a decoded frame pair is just
another (old, new) pair to the renderer), without fighting the frame-stepping
transport. The sample rect is computed ONCE in normalized coordinates
(`_lens_sample`) and mapped onto each image: deriving a box per image would let the
panels drift apart where the clamp bites at an edge, and a lens showing two different
places reads as a difference between the IMAGES, which is the one failure it must not
have. The arithmetic is five pure module-level functions (`lens_magnification` /
`lens_span` / `lens_placement` / `lens_panel_size` / `lens_zoom_floor`) so it is
tested without a display. A pointer move OR a wheel notch redraws only the
`"lens"`-tagged items, never the base blit underneath (that is the expensive part).
The toggle and the zoom are remembered (`compare_lens` / `compare_lens_zoom`), the
pinned spot is not (it means nothing on the next image). See `ComparisonWindow` and
`tests/test_lens_view.py`.

**Film-strip context menu** (0.3.0) — right-clicking a thumbnail
(`FilmStrip._on_right_click`, hit-tested via the shared `_path_at`) opens an
outcome-aware menu: a **processed** (green) image offers *Open original image /
folder*, *Open upscaled image / folder* and *Compare images* (same action as
double-click); a **failed** (red) image offers *Open failed image folder*; an
**unprocessed/processing** image (and a tag-only "ok" with no upscaled
counterpart) offers *Open image / Open image folder*. Folder entries select the
file in Explorer (`_open_folder` → `explorer /select,`), falling back to opening
the folder; image entries reuse `_open` (`os.startfile`). Every state also offers
*Copy path* / *Copy filename* (`_to_clipboard`). The blue "currently processing"
frame is cleared when a run ends (`FilmStrip.clear_current`, called from
`ToolTab.on_exit`) so the last image shows its own green/red outcome instead of
staying highlighted.

**Browse upscaled** (0.6.0, future-features #22) — a **Browse upscaled…** window on the
Batch Upscaler tab: folders-only tree left, paged thumbnail wall right, double-click
opens the comparison window (wipe + lens) on any pair. It closes a real gap: comparison
pairing was **in-memory and run-scoped** (`FilmStrip._compare` is filled from `RESULT`
events and wiped by both `set_queue()` and `clear()`), so the app's most directly
persuasive view was unreachable the moment a run left the screen, though both files were
still on disk. The Video Upscaler already had the equivalent, driven from the DB; the
older and more-used Batch Upscaler had nothing. **Pairing walks the OUTPUT tree and
derives the source back**, not from the DB: the upscaler mirrors via `os.path.relpath`
and keeps the source filename with a lowercased extension, so the inverse is one cached
directory lookup, needs no hashing, and works on a tree produced by another install or
after `db/cache.db` was deleted (`lineage`'s stored paths are marked "informational only"
in the schema). It is `resolve_file`, NOT `os.path.isfile`, and the difference is
load-bearing: Windows stats case-insensitively, so a probe for `a.jpg` finds `a.JPG` and
then hands back a path that is not the file's own name -- everything still opens, but the
browser would show, sort and copy a filename that disagrees with Explorer. One cached
`scandir` per directory answers with the real spelling and is cheaper than the several
stats it replaced. Resolution order is **inverted tag cache, then mirrored name, then
nothing**: `conciliate.find_tr_cache` maps original->current, and browsing starts from the
renamed file on disk, so the useful direction is the reverse (tagging is step 2 of the
documented workflow, not an edge case). **Content-hash matching is an opt-in checkbox**
applied to the remainder, and its cheap end goes first: each unpaired output is hashed and
looked up in `lineage`, and if not one has a recorded source hash the source tree is never
walked at all. A **pair-less output costs nothing** because the entry is keyed by its
OUTPUT path (a paired one is keyed by its SOURCE, like the run strip): with no `_compare`
entry, double-click falls through to `os.startfile`, no status means no green frame, and
the context menu drops into its plain "Open image" branch, all pointed at the file that
exists. `FilmStrip` gained an additive **`show_page`/`page_count`** (the visible batch used
to be a side effect of `set_current`, so paging had to be driven by faking a "current"
image) and a per-widget **`page_size`**, and is built here with **`on_zoom=None`**:
`_do_resize` clamps the cell to the viewport and writes the clamped value BACK, so a
maximised browser near `CELL_HARD_MAX` would have the tabs' few-hundred-px strip re-save
the smaller value on every close. A browser page holds **200** (`BROWSE_PAGE_SIZE`),
double the tabs' `BATCH_SIZE`, because a maximised window at 4K fits a little over 200
default-size cells and 100 left half the wall empty. Measured before changing it (220 real
4K JPEGs): decode is linear and off-thread, so time to the FIRST thumbnail is unchanged
(~25-50 ms) and the wall fills progressively; the price is memory (+99 -> +192 MB at cell
150, +140 -> +276 MB at cell 300, nearly all of it the <=512 px PIL masters, released on
the next page) plus a slower zoom click, which regenerates every PhotoImage (215 -> 432 ms
at cell 300). Cheap for a window that is modal and for an app that loads a 16 GB model; the
tabs keep 100, where a bigger batch would only decode images nobody can see, mid-run. The
zoom and paging controls sit in a toolbar **above the wall and inside the thumbnail pane**,
not spanning the window: they act on the thumbnails, and the split is draggable, so a
window-wide bar would drift away from what it controls. Within that bar the paging group
(count between the arrows that change it) is **centred on the wall** via a two-column
`uniform` group, so it is centred on the pane rather than on whatever slack the left-hand
zoom buttons happened to leave. Each control sits over what it changes: **"Match by content"
is above the TREE** (it re-pairs images, which moves them between folders and changes the
counts on those rows; over the wall it read as a filter on the thumbnails on screen, which
it is not). The **status line is a bottom bar**, with Close on it -- moving status out of
the top would otherwise have left a header row holding nothing but that button. It is
written in ONE place (`_set_status_summary`, since the same summary is re-shown after
content matching and after un-ticking it) and counts **images AND folders** ("263 upscaled
image(s) in 4 folder(s)"), because the image count alone says nothing about how far the
tree they are spread over. The folder tree opens with only the ROOT expanded. First-ever
open uses `gui.common.DEFAULT_WINDOW_GEOMETRY`, the SAME constant as the main window's
first-run size (this window replaces it on screen, so a different size reads as the app
having jumped somewhere else); after that its own `browse_geometry` wins.
**Modality copies `gui/video_benchmark.py` verbatim** -- no `grab_set()` (beyond the
Windows title-bar problem, a local grab routes events to the grabber AND ITS DESCENDANTS,
and the shared `ComparisonWindow` is a child of `App`: a grab would make it open and then
ignore every click), no `transient` (auto-hidden with its master), `withdraw()` not
`iconify()`, and restore-the-main-window bound to `<Destroy>` as well as
`WM_DELETE_WINDOW`. That last one is the riskiest line in the feature: everything else
fails visibly, but it fails by leaving the app running with no visible window at all, and
`single_instance.py` then blocks a relaunch. The walk uses `DerivedPruner` (a nested
`__Archive__` is not "upscaled images"), the scan runs off the UI thread, and the roots
come from the tab as-is -- nothing about them is remembered. **Conciliated trees are
deferred** (the output folder is empty after a run, so the empty state names conciliation,
or the deferral reads as a bug). Pairing and paging are pure module-level functions, tested
without a display. See `gui/browse_upscaled.py` and `tests/test_browse_upscaled.py`.

**Conciliation** (experimental, 0.2.1; videos 0.5.1, future-features #5) — replaces
original photos **and videos** with their processed (upscaled, optionally tagged &
renamed) counterparts. Two phases: **Scan/Preview** builds a per-folder plan
(replaced / no-match / non-media-kept counts, and lists the kept non-media files by
path) and touches nothing; **Run** then either archives originals into an
`__Archive__` subfolder or deletes them (delete needs an extra confirmation), moving
the processed files into the original tree. Matching prefers the **content-hash
lineage** (0.2.1; recorded for whole-video outputs too since 0.4.9 item 10), so a
source still matches its processed counterpart after folders are moved or renamed.
**Images** additionally fall back to mirrored-name matching when no lineage exists;
**videos are matched by lineage ONLY** (0.5.1) — deliberately no `<stem>_<target>`
name fallback, because a *clip* extract records no lineage yet shares the source's
`<base>_` prefix, so a name guess could replace a whole source with a short clip (a
loss the counts-only preview would not reveal). An un-lineaged video (a clip, or one
upscaled with `record_lineage` off) simply has no match and is left untouched.
**Video conciliation** reuses the exact same scan/run pipeline: `MEDIA_EXTS =
IMAGE_EXTS | VIDEO_EXTS` widens the walk and the processed hash-index; a video is
only ever acted on when its output is actually present in the chosen processed tree
(so pointing it at an image-only processed folder never touches a video, and the
non-destructive preview shows every action first). Originals without a processed
counterpart, and non-media files, are never touched. Since 0.5.9 an **image the
upscaler cannot round-trip** (transparency, several pages, 16-bit) is refused too,
checked on the ORIGINAL before either matching path so a tree upscaled BEFORE that
fix is protected as well: see "Image variants left as-is" (#17). Also since 0.5.9,
each image pair gets a **metadata backfill** immediately before the original is
archived or deleted (#13b, the last moment both files exist); Scan/Preview only
counts it: see "Metadata carried across". And since 0.5.9 a Run is **journalled**
so it can be reversed: see "Conciliation Undo" (#18).

**Conciliation Undo** (0.5.9, future-features #18) — Conciliation was the app's
**only destructive tool and the only one with no undo record**: the `__Archive__`
folder was the sole evidence a run had happened, and a Delete run left not even
that (Tag & Rename has recorded every change before making it since early on). A
Run now journals one row per file action to `db.conc_runs` / `conc_actions`
(`UndoRecorder`) **before** performing it, and an **Undo last run…** button on the
tab reverses an archive run: each processed file goes back to the processed tree,
each original comes back out of `__Archive__`. Four decisions carry it. **Undo
reads the DISK, not the row's status** (`undo_one`): a run interrupted between its
two moves leaves a `pending` row whose real state is only knowable by looking, so
undo re-derives it, which also makes a repeat undo a no-op and lets a partial undo
be retried (an undone row is skipped by `get_conc_actions`' default status filter).
**It never overwrites**: both halves are checked before either moves, so a refusal
leaves the pair exactly as it was, and a file changed since the run or a name
something else now occupies is a reported conflict, not a guess. The sharp case is
the usual one, where the processed file took the original's own name: a file *does*
sit at the original's path in both the done and the already-undone state, so
"already undone" is detected FIRST (original back + processed back + archive gone),
and a missing archive entry can never be read as "the original is already back".
**A delete run is refused, not attempted** — the bytes are gone and no journal
changes that, so the button stays disabled and its tooltip says why, while the
journal is spent on the question a user actually asks after a bad delete run:
`--undo` prints exactly which originals were removed. **Recording is free and
fail-safe**: the fingerprint is (size, mtime) plus the content hash *only when one
is already memoised* (`db.cached_hash`, which never reads a file — the original was
just hashed by the lineage matching, a processed file the #13b backfill rewrote was
not), because recording runs on every conciliation while verifying is the rare
recovery path and can afford the read (`_still_recorded_file`, 2 s mtime tolerance
for SMB/FAT rounding). It also costs exactly ONE commit per pair: the row's
creation must survive a crash, but its `done` stamp rides along on the next
record's commit, because a row left `pending` is precisely what undo re-derives
(measured 7.5 s per 5000 pairs, against 14.6 s when both committed, on a run whose
own file operations are often just renames). A journal failure disables it, reports itself
once on the log, and lets the conciliation finish, because refusing to run would
cost the user the thing they came for. `--undo` takes ONE folder (every path is in
the journal, and the processed tree may have been removed as empty by the run that
emptied it), emits its own `UNDONE` event rather than `DONE` (an undo's counts are
not a conciliation's, and it must not overwrite the MQTT `last_run`), and the
button re-reads the journal on every refresh so it offers a run from an earlier
session and stops offering one undone elsewhere. Runs are pruned to the newest 10
per folder. See `tests/test_conciliate_undo.py`.

**Derived-directory pruning** (0.5.9, future-features #16) — the app writes its
outputs **inside** the tree it scans (the Batch Upscaler's default output is
`<source>/__upscaled__`; Conciliation archives originals into
`<source>/__Archive__`), and before this only Conciliation pruned its own archive.
So after an archive-mode run the Batch Upscaler found every archived original,
Tag & Rename tagged and renamed them, and the Video Upscaler queued the archived
videos. **The archive is precisely the case the resolution threshold does not
cover**: the *replaced* files in the main tree are above the target and correctly
skipped, while the archive holds the only copies still **below** it, so they all
look eligible. Not data loss (the source promise holds, the archive is intact) but
wasted hours and, on a rented pod, **billed GPU time re-processing files the user
already conciliated**. Two adjacent cases the same fix covers: Tag & Rename used to
descend into `__upscaled__` and tag the originals *and* their upscaled copies, and a
second upscale run with a *different* output folder walked into the first run's
`__upscaled__`. The fix is a **NAME rule, in one place**:
`runner_common.DERIVED_DIRNAMES` (`__upscaled__`, `__Archive__`, `.imgtbx_video`,
case-insensitive, plus a changed `video.output_subdir` via `derived_dirnames(cfg)`)
and a `DerivedPruner` called from all four walkers (`batch_upscale` /
`tag_and_rename.collect_work_items`, `batch_video_upscale.iter_videos`,
`conciliate.build_plan`) plus `conciliate.build_processed_hash_index` (whose
unpruned walk was content-hashing every multi-GB segment in `.imgtbx_video`). A DB
lookup was rejected: Conciliation records nothing about its own actions, the
`upscale_files` cache is keyed by **relative path** and archiving *changes* the
path, and `db/cache.db` is per-install and regenerable, so a DB rule would fail on
a second install, a deleted cache and every pre-existing archive. The name rule is
stateless, free per file and **retroactive**. Two invariants: it prunes matching
**subdirectories**, never the chosen root (pointing a tool AT an `__upscaled__` /
`__Archive__` folder still scans it, which is now the supported way to tag an
upscaled tree - and the way Conciliation already expected it, since
`find_tr_cache(processed_root)` looks for the tag cache under the *processed* root);
and **nesting itself is correct and must not be "fixed"** (a shared sibling output
root would collide the moment a second source tree is processed:
`D:\Pics` and `D:\Pics2` would both want `D:\__upscaled__`). Each scan prints ONE
summary line (`DerivedPruner.summary()`) so a smaller-than-expected count is
explained; the Video tab feeds it to the scan console via a `pruned` queue message.
New output folders go in `DERIVED_DIRNAMES`, nowhere else. See
`tests/test_derived_dirs.py`.

**Image variants left as-is** (0.5.9, future-features #17) — the upscale engine is
**RGB-only end to end**: `upscale_engine._load_image` does `img.convert("RGB")` and
`_save_image` writes `arr[..., :3]` as `mode="RGB"`, frame 0 only. So a transparent
PNG/WebP, a multi-page TIFF and a 16-bit TIFF came out flattened / page-0-only /
8-bit **under the SAME name and extension**, which is the sharp part: Conciliation's
mirrored-name fallback then matched them with full confidence and reported an
ordinary "replaced", archiving (or **deleting**) the only copy that still had the
alpha, the other pages or the bit depth. The decision was to **skip** these, not to
support them: handling alpha, multi-page and high-bit-depth output properly is a set
of format design questions this project has not needed to answer, and guessing is
how quiet data loss happens (the other half of the decision, with its revisit
trigger, is in `docs/dropped-ideas.md`). Detection is
`runner_common.image_variant_reason(path)`: header-only (Pillow's lazy `Image.open`,
no decode), **not attempted at all for JPEG** (which cannot carry any of the three
and is the bulk of a photo tree, so the normal case pays nothing), and it reads the
format's own header for depth (TIFF `BitsPerSample`, PNG IHDR) because Pillow
presents a 16-bit-per-channel file as plain `"RGB"` after truncating it. It returns
a plain-English reason that always starts with `VARIANT_PREFIX` ("would lose
transparency", "would lose 3 of 4 pages", "would lose 16-bit depth"), so
`is_variant_reason()` can classify a **cached** `skip_reason` later without
re-opening the file (the eligibility cache stores only the string). Reported, never
just counted: the Batch Upscaler has its own `skipped_variant` counter and prints
each path + reason in the eligibility scan (where the cached ones surface) and again
in the end-of-run summary; Conciliation lists them under "Left untouched". An
unreadable file returns None on purpose so the existing "corrupted / unreadable"
classification keeps it. Two ordering rules matter: the upscaler checks the variant
**before** the already-done branch (whether a pre-0.5.9 run left a flattened output
next to it does not change what the file is, and this is how the user finds those
outputs), and Conciliation checks the **original** before BOTH matching paths (a
lineage row proves the two files are a pair, not that the pair is lossless), which
is what makes the protection retroactive for a tree upscaled before the fix. CMYK
is deliberately not a variant (a colour-space argument, and testing for it would
force a Pillow open on every JPEG). See `tests/test_image_variants.py`.

**Metadata carried across** (0.5.9, future-features #13) — `upscale_engine._save_image`
saved the result tensor with a bare `img.save(...)`, so **every upscaled image lost
all metadata**: capture date, camera, lens, exposure, GPS, copyright. For a tool
whose purpose is reviving a personal photo collection, DateTimeOriginal is the
painful one (the copy sorts by file date), and once Conciliation replaces the
original it is gone for good. Both halves shipped together, sharing one module
(`exif_copy.py`) and ONE setting (`upscale.copy_metadata`, default on, Settings →
Batch Upscaler): a user who wants scrubbed output wants it from both, and two
switches for one intention is one more thing to get wrong. **13a** reads the
source's block and writes it onto the output; it lives in the engine, so it happens
**wherever the file is written** (`exif_copy.py` is pushed to the pod alongside
`upscale_engine.py`, and `worker_settings.json` already carries the flag, so a
remote run needs no separate path). **13b** repairs the already-upscaled backlog
inside Conciliation, which is the one moment the app holds **both** files, already
matched, and in Delete mode the last moment the original exists; Scan/Preview only
COUNTS it (`count_pending_metadata`) and Run writes. **Pillow does the reading and
the writing for every format** - `getexif()` reads a block out of JPEG/WebP/PNG
(eXIf chunk)/TIFF alike and `Exif.tobytes()` writes one back with the Exif and GPS
sub-IFDs intact, so there is one code path instead of a per-format table and no
piexif on the read side at all; piexif appears only in 13b's JPEG branch, because
`insert` patches the APP1 segment and leaves the compressed scan data
**byte-for-byte identical** (verified), which is mandatory for a file about to
become the only copy. **Three corrections are not optional.** Orientation is forced
to 1: the pipeline has ALREADY applied it (`_load_image` runs `exif_transpose`, and
auto-straighten may have rotated a temp copy on top), so copying the source's value
verbatim makes every viewer rotate an upright photo a second time. The stale
embedded thumbnail is dropped (it shows the old image at the old size; `tobytes()`
does not serialise IFD1, which is the mechanism, and the JPEGInterchangeFormat tags
are stripped so a dangling offset cannot sneak through the 0th IFD). And a **TIFF
source's structural tags** (StripOffsets, RowsPerStrip, BitsPerSample …, all of
which a TIFF really does hand back) are stripped, or they would describe a strip
layout that does not exist inside a JPEG. Pixel dimensions are **corrected** to the
written array's size rather than stripped, since the size is right there. Two
fail-safes: an EXIF block the encoder rejects (a JPEG's APP1 caps at 64 KB and
Pillow raises rather than truncating) drops the MakerNote first and then falls back
to writing the image with no metadata, so metadata never costs the image; and 13b's
pass is wrapped so it can never abort the archive/delete it precedes. 13b's policy
is deliberately ONE rule, "copy what is missing, keep what is present" (any field
the processed file already has was set on purpose - Tag & Rename's description, the
normalised Orientation), so it can never undo the previous tool's work and needs no
maintenance when a later feature starts writing some new tag; Orientation and the
pixel dimensions are excluded outright, because on a metadata-free upscale both are
"missing" and both would then be wrong. Backfill is limited to **JPEG and PNG**, the
two that can be repaired losslessly; WebP (lossy q95) and TIFF (Pillow can change
its compression) are refused with a named reason rather than silently skipped, and
videos never reach it (`_is_image_pair`; container metadata is an ffmpeg job).
`orientation._rotate_file` gained the same treatment on its non-JPEG branch, where a
bare Pillow re-save had been stripping EXIF from every PNG/WebP/TIFF it straightened.
See `exif_copy.py` and `tests/test_exif_copy.py`.

**Video Stabilization** (0.6.0, future-features #20 + #23) — a tab (after Conciliation)
that steadies shaky videos into new files. Architecturally a sibling of
**Conciliation**, not of the Video Upscaler: local ffmpeg work, **no GPU, no pod,
no remote mode**, so no VRAM sizing, batch tuning, benchmark corpus, funds guard or
degraded-GPU watchdog, and no "Run on" row. It
**composes by file, not by pipeline**: stabilise, then feed the result to the Video
Upscaler (that ordering matters, since the crop happens at source resolution and the
box-fit target still fills the frame). The reason it is a separate feature rather
than a Video Upscaler option is that `vidstab` is a **two-pass GLOBAL** algorithm:
pass 1 measures camera motion across the WHOLE file, pass 2 smooths that trajectory.
The Video Upscaler splits into ~60 s segments processed independently, so running it
per segment puts a **visible jolt at every boundary**, each stretch having been
smoothed toward its own mean. **#23 added a QUEUE, and it does NOT reverse that
finding**: "not a batch tool" was always about the ALGORITHM being whole-file, and N
independent whole-file jobs preserve it exactly — nobody should later "simplify" the
queue into segmenting one video. **Coverage over steadiness** is the shipped default and
it is the opposite of what every ffmpeg tutorial does: `optzoom=1` picks ONE static
zoom that must cover the worst frame in the clip, which measured **~17-21% of the
picture discarded** on real camcorder footage, set by a single jolt in a ten-minute
video. So the default is `optzoom=0` + `crop=keep` (whole frame kept, borders filled
from previous frames); the zoom is a tick-box that leads with what it costs.
`smoothing` is the real steadiness/coverage lever (measured 9.64% of the frame lost
at 30 against 4.34% at 10) and is the one exposed knob. Stabilisation is never
auto-detected: an UNMODIFIED camcorder clip already scores a 9.64% correction, so a
detector would fire on nearly everything, and shakiness is not a defect the way
interlacing is. An **interlaced** source is deinterlaced (`bwdif`) in BOTH passes,
because pass 1 would otherwise measure the motion of two instants woven into one
frame. Output is a deliverable, not the split pipeline's intermediate, so it takes
`vp.delivery_pix_fmt` (10-bit where the codec allows) rather than `pick_encoder`'s
8-bit default; audio is carried in the same pass-2 command. It is also the first caller to
run where there may be **no NVIDIA card at all**, which is what exposed `pick_encoder`
trusting `ffmpeg -encoders` (hevc_nvenc is compiled into every GPL build, present or not):
`video_pipeline.nvenc_usable` now probes with a real one-frame encode, cached per process,
and its probe frame must stay at least 256x256 or NVENC refuses it and the probe reports no
NVENC on a working card.

**The #23 workflow** (0.6.0, all six items) is a **source folder + an output folder +
a list**. Scan folder walks the tree (`DerivedPruner`, off the UI thread) and each row
says what will happen to it. Six decisions carry it. **"Already stabilised" IS the
resume state**, and it is the reason the queue needs no schema: #20 has no resume by
design (a file finishes in well under its own duration, so Stop discards the `.part`),
but a queue of fifty does, and the cheapest correct form is the Batch Upscaler's -
re-scan and skip what is already there. It needs no table, it is retroactive, and it
works on a tree produced by another install. **`DerivedPruner` (#16) is NOT sufficient
on its own** and that is the sharp part: a result defaults to sitting BESIDE its source
as `<stem>_stabilized.mp4`, which is not a derived directory, so the pruner cannot stop
a second scan re-offering every result as fresh input (and a third scanning the results
of the results). The **NAME rule** (`is_stabilized_name`) is what does, which is also
why a separate output folder is the recommended setup and why the tooltips say so.
**Output names are claimed, not just checked**: two sources with the same stem in
different folders both want one name in a shared output folder, and neither output
exists yet when the queue is planned, so `suggest_output_path` takes a `taken` set -
existence on disk alone would let the second job overwrite the first one's result the
moment it finished. **Progress is one bar for the whole run, not a file counter**
(`QueueProgress`, counted in pass-frames): `main` probes every source up front, which
also catches an unreadable file NOW rather than after the forty-nine good ones. The
`[i/N]` index rides on the FILE event instead, and the TAB owns the status line while a
video runs (composing it from FILE + PASS), because the runner's own per-pass line
cannot know it is one of fifty. **One preflight per run**: a broken vidstab is a
property of the build, so it refuses the whole queue before any file is touched instead
of failing fifty files with the same paragraph. And **a single file's failure does not
end the queue** - it is logged, reported as a RESULT, and the next file starts; a Stop
does end it, and the video it interrupted goes back to Queued (it genuinely is: the
`.part` was discarded). A partly-failed run notifies ORANGE, not RED. **A failed row stays
runnable** (`ST_RUNNABLE` = Queued + Failed): a stabilise fails for environmental reasons the
user can go and fix, and with Failed excluded a one-video failure left BOTH buttons greyed
(Stop because the run was over, Start because nothing was runnable) beside a progress bar
frozen at 50%, which read as a hung app rather than a finished one. The bar is now hidden on
any ending that is not a clean success, and `_report_completion` writes one `Run ended:` line
to the log before the GUI event and before any notification, because nothing else there wrote
to the log at all: a run that finished and a run that died ended their log file identically,
which is precisely the wrong property for the file you read afterwards.
**Comparison is playback-first** (#23 item 4): at the shipped defaults the result has
the SAME dimensions and frame count as the source, so the pair is 1:1 and
timestamp-aligned - the easiest pair the comparison code has ever been handed.
Steadiness is a temporal artifact, so only MOTION answers "is it steadier"; a paused
pair mostly shows that the frame has *moved*, which reads as a difference rather than
an improvement. The still wipe stays on the context menu as the secondary view, where
the **lens (#14)** is the useful part for inspecting what `crop=keep` did at the
borders. Both Settings defaults (`stabilize_source` / `stabilize_output`) now pre-fill
their fields and have **Save as Default** buttons, following the same rules as the
other tabs: a source folder must EXIST, an output folder is taken as-is (the tool
creates it). A **hand-off** from the Video Upscaler's scan list (right-click →
*Stabilize…*) offers the SOURCE video only, never an upscaled output — the documented
ordering is stabilise BEFORE upscaling, and reversing it looks fine until someone
compares framing — and it is disabled while any run is active, because
`refresh_tab_exclusivity` has already greyed the target tab out (`App.active_tool_tab`
is the one place that answers "is anything running", shared by both).

**The pair record is deliberately NOT a lineage row** (#23 item 5), and this is the
data-loss guard of the milestone. `db.lineage` is not a provenance log, it is what
**Conciliation matches on**, and video conciliation is **lineage-ONLY** (#5 deliberately
gave it no name fallback). A stabilised output recorded there would make the app's one
destructive tool offer to **archive or DELETE** the original and move the stabilised
copy into its place — across a whole collection, for a transformation that is opt-in per
video precisely because its failure mode is silent and permanent, and which unlike an
upscale is not even arguably a strict improvement. So the pair lives in its own table
(`db.stab_pairs`, keyed by source path, newest wins) that no conciliation query reads. A
discriminator column was refused: it works only for as long as every future query
remembers to check it, while a table conciliation does not read cannot be forgotten. The
record is what makes Compare work in a later session (the same gap #22 closed for
images) and is checked BEFORE the name rule, since it knows what was actually produced;
it returns a path only if the file is still there, so a deleted result reads as "not
stabilised" and the scan queues the work again.

**The ffmpeg constraint is the headline, and it moved the app's pin.** Every ffmpeg
**8.1.x** release CORRUPTS MEMORY in `vidstabtransform`: libvidstab's
`vsTransformPrepare()` keeps a stale shallow copy of the source frame when it
alternates between its in-place and separate-buffer paths, and FFmpeg 8.1's scheduler
change is what started making frames arrive non-writable and alternating them (fixed
upstream by **`316531e61cf`**, 2026-04-01, FFmpeg #22595 — on **master**, NOT on
`release/8.1`, and 8.2 is unreleased). Measured on a 300-frame 720p clip, 12 identical
runs: n8.1.2 produced **12 different outputs**, a master build produced 12 identical
ones. **The crash is the lucky symptom** (intermittent, ~10-40% on some clips, 0/12 on
others) — the constant one is silently wrong pixels in a run that reports success. So
`bootstrap.ps1` now pins a **hash-verified master autobuild** instead of the n8.1
branch (an immutable dated BtbN release, which is actually a stronger pin than the
rolling `latest` URL it replaced), and writes a **build stamp** (`ffmpeg/build.txt`)
because the old "is ffmpeg.exe there?" check would otherwise keep a broken build
forever on every existing install. Belt and braces, since the offline fallback is
still a release-branch build and a user's own ffmpeg can be anything: the runner runs
a **behavioural health check** before every stabilise (`vidstab_health`) — stabilise a
synthetic clip twice, compare framemd5, and refuse if they differ. Non-determinism is
the signal rather than the crash **because it is far more sensitive**, and a
version-string table would need editing every time either side changes. Measured
10/10 correct in both directions at ~0.5 s.

**The pin move had a price, and it is the one to expect again**: master REMOVED
`-vsync` (deprecated in favour of `-fps_mode` since 5.1), so the Video Upscaler's
CFR-normalising split died with `Unrecognized option 'vsync'` before writing a
frame. Fixed by `-fps_mode cfr`, which BOTH branches understand, so it is not a
master-only spelling and must not be "simplified" back
(`tests/test_video_pipeline.py`). A sweep of all 44 ffmpeg options this codebase
passes found no other removal, but **that sweep is the thing to repeat when the pin
moves again** — a pre-release branch drops deprecated options, and only the split
happened to have a test that caught this one. See `video_stabilize.py` and
`tests/test_video_stabilize.py`.

**Video Upscaler** (experimental, future-features #2) — upscales a folder of
videos with the same SeedVR2 engine the Batch Upscaler uses for stills, to a
selectable **target** (1080p / 1440p / 4K) via **box-fit** (first reachable edge
wins, per-video from the frame dimensions). A **Method** switch (0.5.6, feature
#11) also offers **Real-ESRGAN** (fixed-ratio 2X/4X GAN, Compact/Quality tiers) as
a fast, VRAM-light alternative to SeedVR2's per-frame diffusion, local or on a
volume-free esrgan pod; each queued job carries its engine + picked GPU so a mixed
queue is grouped and run one pod per (engine, GPU). See the Real-ESRGAN engine
cluster below. **The remote GPU picker is job-aware** (0.5.7): it lists only the
cards that can actually run the video being added and never hides a valid card once
the queue is non-empty. For SeedVR2 it gates by the selected target's VRAM floor (a
16 GB card is not offered for a 4K SeedVR2 job, but a card proven by its OWN SeedVR2
benchmark is; a Real-ESRGAN benchmark of the same card does NOT qualify it, since a
GAN tiles on OOM and reaches sizes SeedVR2 can't). Prepare is disabled with no GPU
selected, the **Run on** switch locks while the queue is non-empty (a queue is one
mode until the mixed local+remote milestone #12), and the segment extractor inherits
the picked GPU + Method and offers only the targets that card can reach. Runs on a
**rented RunPod pod** OR on
**your own local GPU** (local path #7, 0.5.0): a **Run on** switch picks Local or
Remote (following the install mode), and the same walk, split, reassemble, mux,
drift check, resume and notification pipeline runs locally either way; only where
the GPU work happens changes. The source is never touched. Per video the flow
is **probe → split into segments (local ffmpeg) → stream each segment to the pod
worker (submit/poll/fetch) → reassemble (concat + audio-mux) → duration-drift
check**. Streaming one segment at a time bounds pod RAM (SeedVR2's load-all path
holds every output frame uncompressed; the worker uses the streaming
`chunk_size>0` path). Resume is at **two granularities** (db.py `video_*` tables):
a stopped run resumes at the first unfinished **segment**, not the first
unfinished video, and a per-run minute/cost cap ends a run cleanly after the
current segment (the rest stay `pending` for the next run, so a big job is paid in
affordable installments). The scan is **destination-reconciled** (0.5.1): besides
dropping DB rows for outputs deleted off disk (`reconcile_video_outputs`), it
**adopts** outputs that exist in the destination but are absent from THIS install's
`db/cache.db` (`reconcile_outputs_from_disk`, checking the exact `_output_path` of
each of the five canonical whole-video targets), so the "already upscaled" state
comes from the shared **output folder**, not just the local cache. This fixes a
cross-install gap: a second install (e.g. a laptop upscaling via a remote pod while
the desktop GPU is busy) that shares the same source + destination no longer offers
to redo videos another install already produced. A **cost/duration estimator**
(`video_estimate.py`) picks the cheapest live-available card that clears the
target's VRAM floor, seeded from the benchmark and refined by the user's own
`db.gpu_perf` history.
**Local runs** upscale each segment through the in-process `LocalVideoEngine`
instead of streaming to a pod, sizing the batch predictively from the card's VRAM
(`video_vram_sizer.py`) with OOM back-off and the degraded-GPU watchdog. A
one-click **Benchmark GPU** window (`gui/video_benchmark.py` driving
`video_benchmark.py`) sweeps each target to its measured VRAM ceiling on the actual
card (local OR a rented pod), persists per-probe rows to db.py `video_bench`, and
calibrates the AUTO batch, the offered targets and the time/cost estimate; it is
resumable. It can benchmark **both torch.compile modes in one run** (0.5.1): a
**"Torch Compile" column** (ON/OFF) fronts the results table with a row per
(target, mode), and an **"Also use Torch Compile"** checkbox (default ticked when
the local toolchain verifies via `gate_local_compile`, disabled + a "why" tooltip
otherwise, and for remote) sweeps the compiled AND uncompiled regimes back to back.
The two are stored under separate regime-tagged keys (`bench_key`: model + tile +
compile), so they never overwrite each other and AUTO reads whichever matches the
real run's compile state. This is **independent of the Settings compile checkbox**:
the window always benchmarks the no-compile baseline and *adds* the compiled sweep
when ticked. `run_benchmark(compile_modes=[...])` loops the modes (each a full
`_sweep_one_mode`); `resolve_bench_keys()` gives the GUI both keys + availability;
`--compile-modes off,on` drives it headless. Remote stays single-mode (the pod owns
its compile). The results table has the **same filter + column-sort UX as the Video
Upscaler lists** (0.5.1): a Filter bar (Torch Compile ON/OFF + Target) detaches/reattaches
rows view-only, and clicking any header view-sorts by that column's underlying value
(compile mode, output pixels, parsed batch/spf/VRAM numbers, runtime seconds), never
touching the saved probes.

**Benchmark sharing** (experimental, 0.5.1, future-features #8) — turns the per-card
video benchmark into a **crowdsourced dataset** so a card someone else already measured
is not re-swept locally (a sweep is slow and, on a rented pod, billed). Zero
infrastructure: one curated CSV (`docs/video-benchmarks.csv`) lives in the GitHub repo,
the app pulls it anonymously (the `net_ssl` GitHub path `updater.py` uses), and
contribution is **browser-delegated** to the user's existing GitHub account (no upload
endpoint, no token, no backend). **Download is automatic**: at each launch
`App._startup_bench_sync` runs `video_benchmark.auto_update` on a background thread
(community pull, shipped-CSV fallback when offline), silently and fail-safe, so the local
set stays current with no button and no prompt. **Contribute my results…** (Benchmark GPU
window) opens a **pre-filled GitHub issue** (`gui.common.contribute_benchmark`, inline CSV
or an attach hint when it exceeds the URL cap) via a **multi-select card picker**, so
several GPUs go into ONE submission. Two filters keep submissions clean: only cells the
user actually MEASURED are offered (`build_share_rows` filters `video_bench` to
`source='local'`, so imported community data is never re-contributed), and only rows not
already in the live corpus are submitted (`bench_share.new_rows`, keyed on the measurement
columns, ignoring volatile date/price). Import has **local precedence** (the one
correctness rule): `db.import_bench_rows` writes downloaded rows to `video_bench` (a
`source` column, migrated default `'local'`) + `video_batch_learn` as `'imported'`, and
NEVER overwrites a cell the user measured locally; imports stay advisory (the sizer's OOM
back-off self-corrects a slightly-wrong ceiling). Remote contributions drop compile-OFF
regimes (a pod always runs `torch.compile` on). Curation is a **maintainer tool**, not a
hand-edit: `bench_share.py --merge <csvs>` ingests submissions into a private (gitignored)
`benchmarks.db`, dedupes newest-wins, runs a physical-plausibility **sanity gate** (reject
an impossible ceiling/spf/VRAM), and re-exports the diffable master. Headless flags mirror
the GUI (`--export-csv`, `--import-csv PATH|community`). The `gpu_perf` estimate rate is
deliberately NOT seeded from imports (an accumulating store; the estimator falls back to
the author `RATES` table). See `docs/benchmark-sharing.md` and `scripts/bench_share.py`.

Two SeedVR2 limits are inherent (architectural, not tunable), documented in
`docs/video-upscaler.md`: **temporal jitter** of fine detail on slow pans/slow-mo
(the 4x causal temporal VAE) and **text/plate/logo distortion** (generative SR,
no OCR). An opt-in **"Auto-resume"** checkbox (0.5.0, next to Start, per-run,
default off, future-features #6) makes a long run survive **losing its pod
mid-run**: a supervisor (`_run_supervised`) distinguishes a bad-source
`RemoteVideoWorkerError` from a liveness `RemoteVideoError`, re-raises the latter
as `PodLost` out of `run_queue` (without blaming the source's `fail_count`), then
either **reconnects** to a surviving pod (a connectivity blip, `_pod_still_running`)
or **waits unbounded** for the IDENTICAL card to return to stock
(`_wait_for_gpu_stock`, backoff, `$0` billed while waiting, no time cap) and
**redeploys** it, continuing from the first unfinished segment. Hard stops that
never redeploy: the funds guard tripping, a user Stop, or a completed queue (no GPU
substitution, ever, 0.4.0). Video only for now. See the Video Upscaler module
cluster below and `docs/video-upscaler.md` section 17.

**Video segment extractor & in-app playback** (experimental, 0.4.7) — two children
of the Video Upscaler that share one capability the app never had: **playing video
with audio inside the GUI**, via a bundled **libVLC** (downloaded at first launch by
`bootstrap.ps1` / re-enabled on an older install by `vlc_setup.py`, driven through
`python-vlc`). (1) The **segment extractor** upscales one scene out of a long source
instead of the whole file: mark an in/out range on a live preview
(`gui/video_segment_picker.py`) and it is queued as a virtual **clip** (a `clip_id`
discriminator in `db.py`) that runs through the exact same estimate / GPU-pick /
stream / resume path as a whole video (`video_pipeline.extract_clip` cuts a temp clip
so the source is never touched; `batch_video_upscale.prepare_clip` + the `process_job`
clip branch). (2) The comparison gains a real **motion** view: `VideoPlaybackWindow`
plays original vs upscaled side by side **with sound** (audio routed to the upscaled
player as the single sync reference), while the still-frame `VideoComparisonWindow`
keeps the before/after wipe for pixel-peeping. libVLC runs a pure-software `wingdi`
vout because a GPU vout crashes across the embedded-HWND resize/pause lifecycle
(uncatchable from Python); a RivaTuner-style overlay injector also crashes it, so
`gui.video_player.warn_overlay_once` detects the hook and warns once. Playback is
fail-safe: if libVLC is absent the tool falls back to a silent frame-scrub. See
section 16 of `docs/video-upscaler.md`.

**Tooltips everywhere** (0.5.2) — every interactive control on all seven tabs has
hover help (~160 tooltips; buttons, checkboxes, picklists, spinboxes and the
scan/queue/preview lists). Written for a non-technical user: plain language, and
the money- or data-affecting controls lead with the consequence (Conciliation's
Delete is not undoable, a RunPod volume bills monthly even when idle, the Video
Upscaler's Stop abandons the segment in progress). Settings' numeric knobs quote
their **recommended value**, pinned to the runner's coded default by
`tests/test_settings_recommended_values.py` so the hint can't drift from the
fallback it describes. Texts wrap at `Tooltip.WRAP_NARROW` (250 px) so a short
hint is a compact block, not one wide strip.

**One "Run on" row everywhere** (0.5.8) — the three GPU tabs (Batch Upscaler, Tag
& Rename, Video Upscaler) present the same control pair on one row: a **"Run on:"
combobox** ("Local GPU" / "Remote: RunPod", the shared `gui.common.RUN_ON_LOCAL` /
`RUN_ON_REMOTE` labels) followed by a **GPU picker + ↻** that lists whatever that
choice can actually use. Remote lists the live RunPod catalog as before; **Local
lists this machine's NVIDIA cards** (`system_telemetry.list_gpus`, the enumerating
sibling of the first-card-only `sample_gpu`), and on a **multi-GPU** machine the
picked card is passed to the runner as `CUDA_VISIBLE_DEVICES` (`ToolTab._local_gpu_env`;
deliberately NOT sent with one card, where there is nothing to choose). This
replaced the image tabs' "Run on remote pod (RunPod)" checkbox and the Video tab's
Local/Remote radio pair. `remote_var` (Upscaler/Tag) and `mode_var` (Video) survive
unchanged as the "is this run remote" source of truth that the funds readout,
telemetry and every `_start` read, so the combobox only drives them. **The displayed
label is never the state**: each tab maps label -> mode through a table
(`ToolTab._run_on_modes`, `VideoTab._mode_tokens`) whose fallback is the CURRENT
mode, because comparing the display string to a constant (`run_on_var.get() ==
RUN_ON_REMOTE`, pre-0.5.8) reads an unrecognised label as "not remote" and would
silently run a pod-bound job on the local GPU. Same rule for the Upscaler's
phase-changing pause button: `PAUSE_PHASES` + `_set_pause_phase` emit the label and
its tooltip together instead of looking the hint up by the button's own text
(`tests/test_display_text_is_not_state.py`). A
**single-mode install** (Local-only / Remote-only) pins the selector to the one
value it can run and greys it out, with a tooltip saying why, while the GPU picker
beside it stays live: there is still a card to pick. Each mode's list is cached, so
flipping back and forth costs no extra RunPod call or `nvidia-smi` spawn. On the
Video Upscaler the row sits under the folder fields (above "Eligible videos") and
also carries the **readiness line** and, right-aligned, **Benchmark GPU…** (which
acts on the card picked on that row); the readiness line no longer repeats the
card name + VRAM the picker already shows. **Tab order** is Batch Upscaler · Tag &
Rename · Video Upscaler · Conciliation · Video Stabilization · Settings · RunPod:
the three GPU tools sit together, with the two that use no GPU (Conciliation, the
post-processing step, then Video Stabilization) after them. Video Stabilization
does NOT get a "Run on" row at all: it has no local/remote choice to make.

**Settings** — Ollama URL (with reachability check) and model picklist;
auto-straighten toggle/threshold; Resolution Target and skip-cutoff; SeedVR
pipeline options; Discord webhook (with Test); default folders per tool. The
**Default folders** rows are grouped per tool in **tab order** (Batch Upscaler ·
Tag & Rename · Video Upscaler · Conciliation · Video Stabilization); that had
drifted as tools were added, and nine near-identical folder rows are only scannable
in a predictable order, so `tests/test_settings_default_folders.py` pins both the row
order and the keys `_collect` writes. Those two can drift apart independently (one is
widget layout, the other a dict literal a few hundred lines away), and the second is
the dangerous one: `_collect` rebuilds the whole `defaults` dict from the form, so a
key missing there is silently DROPPED from `config.json` on the next Save - which is
why a tab cannot just invent a default of its own. Settings
take effect only on **Save**; an **unsaved-changes guard** compares the form
against `config.json` and shows a Save / Don't save / Cancel prompt when leaving
the Settings tab or closing the app with pending edits (`SettingsTab.is_dirty` /
`_collect` / `revert`). A **live "Not saved" indicator** (0.3.4,
`_refresh_save_indicator`, a light `after` poll of `is_dirty`) shows red **"Not
saved"** the moment any field differs from the saved state and green **"Saved."**
right after a save — reusing the save-bar status label. Picklists ignore the mouse wheel (0.2.8): `App`
rebinds the `TCombobox`/`TSpinbox` `<MouseWheel>` class bindings
(`_install_picklist_wheel_guard` / `_picklist_wheel`) so scrolling over a
combobox/spinbox no longer silently cycles its value (which used to flip a
setting unnoticed and trip the guard); the wheel is forwarded to the nearest
scrollable canvas so the page still scrolls.

**First-start Wizard** (0.4.6) — a one-time onboarding dialog shown on the first
launch (guarded by `wizard_done` in `gui_settings.json`; re-runnable from Settings →
"Re-run first-start wizard"). It detects the local GPU (`system_telemetry.sample_gpu`
/ `gpu_name`) and **recommends the SeedVR2 upscale model + Ollama vision model that
fit the card's VRAM**, so a non-technical user gets a fast, sane config without
knowing what "7B fp16" means. Calibrated tiers (pure `gui/wizard_recommend.py`,
unit-tested): 8-12 GB → 3B Q8 + `qwen3-vl:2b-instruct`; 16 GB → 7B FP8-mixed +
`qwen3-vl:4b-instruct`; 24 GB+ → 7B FP16 + `qwen3-vl:8b-instruct` (the vision tiers
rechosen 0.5.5 from the Tag & Rename benchmark, `docs/tag-and-rename.md`). The
recommendation is a **suggestion, not a gate**
(SeedVR2 offloads, so any card can run any model, just slower): every option stays
selectable. The chosen Ollama model is **checked and offered for one-click pull**
(`common.ollama_pull`, streamed `/api/pull` with progress); SeedVR weights download
lazily on first upscale, so no pull is needed there. **Remote-only** installs skip
the GPU step and route to the RunPod tab (SSH key + volume); **both** installs get
that as an optional final step, and their model config is left at the shipped
defaults. As a safety net, a **local Tag & Rename run re-checks the model on Start**
and offers to pull it if missing (`TagTab._ensure_ollama_model` + `OllamaPullDialog`),
since Ollama never auto-pulls. The GPU-blind `ollama pull qwen2.5vl:7b` was removed
from `bootstrap.ps1` (Ollama is still installed; the wizard now owns model choice).
An **optional compile-speedup step** (0.5.0, local/both, feature #7) helps set up the
local video engine's `torch.compile`: shown only when a pinned `triton-windows` wheel
exists for this Python/torch (`triton_setup.is_supported()`), it probes both halves
off the UI thread (`_detect_compile`; `msvc_setup.verify_toolchain()` actually
compiles, `triton_setup` imports torch), states the size / speed / VRAM trade, gives a
card-sized verdict (`wizard_recommend.recommend_compile`: recommended ≥32 GB, optional
≥16 GB, else not; the ≥32 GB line reflects a measured 24 GB 3090 sweep where compile
was a net loss at every real 1080p+ target because it halves the batch, see
`docs/local-video-upscaler.md` 14.4), then **installs the Triton half in-app** (verified ~50 MB wheel via
`prompt_install_triton`) and **links to Microsoft's page for the C++ Build Tools half**
(~2-3 GB, minimal `MSVC v143` + `Windows 11 SDK` components spelled out). It writes no
config: compile stays gated at runtime by `batch_video_upscale.gate_local_compile`.
See `gui/wizard.py`, `gui/wizard_recommend.py`, and `docs/first-start-wizard.md`.

**Performance watchdog** (0.3.0, experimental) — guards long upscale runs against
the **degraded-GPU** failure mode: as a run accumulates GPU/driver state the
pipeline can silently slow from seconds/image to minutes (the GPU thrashing VRAM
into system RAM), or fail with a hard OOM; only a reboot cures it and it
reproduces outside the app (ComfyUI hits it too), so it is **below** the app, not
a code bug. `run_pass` compares each image's **seconds per output megapixel**
against the run's **running minimum** (the GPU's healthy throughput): anchoring to
the minimum — not a rolling average — means a slow *creeping* ramp can't drift the
baseline up and evade detection, and the per-MP normalisation keeps it valid
across mixed resolutions. **Either** a sustained slow streak (≥`watchdog_factor`×,
default 3×, the healthy rate for `watchdog_consecutive` images) **or** a hard OOM
(`_is_oom_error`) is one *degradation episode*: it emits a `DEGRADED` event, sends
a Discord alert, and **auto-stops after the current image** (the resume cache
continues the queue after a reboot; the rescan pass is skipped via the `degraded`
stat). Edge-triggered (one notification per episode). Inherent limit: a run that
is *already* degraded at image #1 has no healthy sample to anchor to (reboot
first). Toggle + factor/consecutive in Settings → Batch Upscaler; `watchdog_min_samples`
is config-only. Built as a reusable health signal for remote-pod upscaling
(future #1). See `WATCHDOG_*` and `_trigger_degradation` in `batch_upscale.py`.

**Notifications** — alerts on queue completion and on errors, fanned out to every
configured backend (0.3.8): a **Discord** webhook, a **Telegram** bot, **ntfy**
(public ntfy.sh or a self-hosted server), and a **Home Assistant webhook** (0.5.8).
All backends live in one stdlib module,
`notifications.py` (`notify()` + `send_discord`/`send_telegram`/`send_ntfy`/`send_ha_webhook`
+ the GUI's Test/Detect helpers); the runners call the unified `send_notification(...)`,
so there is a single source of truth instead of the old per-runner copies. Settings
(Settings → Notifications) hold the Discord webhook; a Telegram **bot token** +
**chat ID** (a **Detect** button reads the bot's `getUpdates` to fill the chat ID
automatically: the user just creates a bot via @BotFather and presses Start); and
an **ntfy server** (default `https://ntfy.sh`) + **topic**; and a **Home Assistant
URL** + **webhook ID** (0.5.8, the no-broker HA route: the app POSTs the alert as
JSON to an automation's webhook trigger. Setup ORDER is the point: the automation
must exist first, because HA answers 200 to an unknown webhook id, so a Test before
it exists looks like success and proves nothing. `HA_TEST_OK` says so verbatim and
nothing "verified" is ever stored; the payload's `message` is pre-rendered so the HA
action is one line, and `level` = `level_for(color)` is what an automation branches
on. Prefer the MQTT integration when a broker exists: it is a superset, and the only
route that can report a CRASH, via the LWT). Each has a **Test**
button. Config lives in a dedicated `notifications` section (`discord_webhook_url`,
`telegram_bot_token`, `telegram_chat_id`, `ntfy_server`, `ntfy_topic`, `ha_url`,
`ha_webhook_id`, plus a config-only `ntfy_token` for self-hosted auth); `resolve_settings()` reads the
legacy `upscale.discord_webhook_url` as a fallback and the next Settings save
migrates it. Each backend is independent and fail-safe (one being misconfigured
never blocks another, and none ever raise into a run). **Severity** is one table
(0.5.8, `_SEVERITY` + the `COLOR_GREEN/ORANGE/YELLOW/RED` constants + `level_for`):
it carries as a Discord embed colour, a leading emoji on Telegram, and an emoji tag
+ priority on ntfy (errors at 5 so they buzz louder). **Runners must pass a
`notifications.COLOR_*` constant, never a raw int** — they each wrote their own
literal until 0.5.8 and the Video Upscaler's palette matched no entry, so a FAILED
video run went out at ntfy's default priority with no tag and no Telegram emoji (an
unknown colour degrades quietly by design, so nothing in production could report
it); `tests/test_notification_severity.py` now fails on any raw colour literal in a
runner. **The backends, the setup steps, the severity contract and what was
rejected are in `docs/notifications.md`.** A
**taskbar flash** (0.3.0, `App.flash_attention` via ctypes `FlashWindowEx`,
Windows-only, fail-safe) fires on run completion (every tool) and on a watchdog
degradation episode, so an unattended run catches the eye while the user is in
another app. A **taskbar progress bar** (0.3.0, `taskbar_progress.py` →
`ITaskbarList3` driven straight from ctypes COM, no new dependency) paints run
progress onto the app's taskbar button — marquee during the initial scan, a green
done/total fill while processing, **red** on a watchdog degradation, cleared when
the run ends. Driven from `App.taskbar_progress` / `taskbar_state` / `taskbar_clear`
off the same progress/DEGRADED/exit hooks as the in-app bar.

**Home Assistant (MQTT)** (0.2.4) — optional, opt-in integration that publishes
app state to an MQTT broker for Home Assistant / MQTT Explorer. No separate
enable toggle: MQTT activates whenever a broker **host** is configured in
Settings (clear the host to disable). A persistent client keeps the connection
up for the app's lifetime, verifies connectivity on startup, auto-reconnects,
and sets an availability **LWT** (`image-toolbox/availability` → online/offline)
so HA always knows if the app is alive. Retained **state** topics: `version`,
`update`, `latest_version`, `last_run` (JSON summary), `last_used`, `system/*`
telemetry, plus live `task/*` state (`name` =
idle/upscaling/tagging/conciliating/video upscaling, `details`, `runtime`,
`progress` = X/Y, `eta`, `average_processing_time`, `last_processing_time`) —
**all four tools report** since 0.5.8, the Video Upscaler included (its
`progress` counts frames, not files). Plus two **non-retained one-shot events**
(`event/run_started` / `event/run_finished`): retained state is re-sent to every
new subscriber, so it re-fires an automation on every HA restart, and the events
are what a trigger should use. The publishing lives in the widget-free
**`MqttTaskState`** mixin (`gui/tooltab.py`), shared by `ToolTab` and `VideoTab`;
every published timestamp goes through **`gui.common.now_stamp()`**
(offset-aware, so HA's freshness checks work from any timezone). Settings has
host/port/username/password/client-id fields, a "Test" button, and a "Publish
now" button. Depends on `paho-mqtt` (installed by `bootstrap.ps1`); the import is
lazy so older venvs still launch. Ready-made HA automations ship in
`samples/home-assistant/`. **The full contract, the payload keys per tool, the
design decisions and what was rejected are in `docs/mqtt-integration.md`** — read
it before touching a topic, a payload key or a retain flag.

**System telemetry** (Feature #3a) — a compact, read-only status row below the
image carousel on each tool tab showing **CPU usage, RAM, GPU VRAM, GPU
temperature**, and (0.5.3) **GPU core utilization**, also published to MQTT as
retained `image-toolbox/system/*` topics (`cpu`, `ram`, `ram_total`, `gpu_vram`,
`gpu_vram_total`, `gpu_temp`, plus 0.5.3's `gpu_util`, `gpu_power`,
`gpu_power_limit`, `gpu_clock` — power draw + limit and core clock ride the same
one `nvidia-smi` call, per-field parsed so a card that reports `[N/A]` for a field
drops only that field). The percentage readouts are colour-banded by load (blue
≤25 % · green ≤65 % · dark yellow ≤85 % · red >85 %). Dependency-light: CPU via
Windows `GetSystemTimes` and RAM via `GlobalMemoryStatusEx` (both `ctypes`, no
psutil), GPU from `nvidia-smi` (no pynvml). Cadence: during upscaling, 5 s after
each image starts (past the load/ramp, avoiding the dip between images); every
30 s during Tag & Rename and Conciliation; and every 60 s while **idle** (so the
user can watch VRAM free up before starting a run). The idle sampler steps aside
whenever a task is running. Samples run off the UI thread (a lock prevents
overlapping `nvidia-smi` calls). See `system_telemetry.py`.

**Telemetry usage graphs** (0.5.3, feature #9) — clicking a telemetry row opens a
floating, per-run **usage-graph window** (`gui/telemetry_graph.py`, one shared
instance per source: the local machine, or a tab's remote pod). Four vertically
stacked **matplotlib** charts (embedded via `FigureCanvasTkAgg`) plot the run's
GPU/CPU load, memory (% of capacity), power and temperature over time, with a
blitted crosshair readout that stays smooth on hover. The axes are **pinned to
hardware capacity** (RAM/VRAM total, power limit; temp 0-100 °C), never autoscaled,
so a run at the wall reads as *at the top of the chart*. The graph is **per-run**:
its buffer (`system_telemetry.TelemetryHistory`) records only between a run's start
and end (the continuous idle sampler feeds only the row, never a graph), the
timeline anchors to the **first processed image/video** (not the pre-scan phase, so
there is no leading dead space), and on run end it **freezes but stays browsable**
(resets on the next run, dies with the app). A per-window-**global** range bar
(1h/3h/6h/12h/24h) enables each button only once the run's runtime passes it, and
toggles the whole window between the last-N view and the whole run. matplotlib is a
deliberate, **lazy + fail-safe** GUI dependency (already present on Local/Both via
seedvr2; added to the Remote-only bootstrap): absent, only the graph is
unavailable, never the row or MQTT. Ready-made **Home Assistant dashboards** (#10)
that render the same MQTT telemetry live in `samples/home-assistant/` (a no-HACS
core dashboard + a Mushroom/ApexCharts one, plus the MQTT + derived-% sensor YAML).
See `docs/telemetry-design.md`.

**Updates** (0.2.3) — in-app update check against the GitHub Releases API.
Checks on startup (opt-out) and on demand from Settings; when a newer release
exists it shows the patch notes and can download `ImageToolboxSetup.exe`, launch
it and quit so Inno Setup replaces the app in place. "Skip this version" is
remembered. Pure stdlib (`urllib`); see `updater.py`.

**Crash logging** (0.2.5) — the GUI runs under `pythonw.exe` (no console), so an
unhandled exception used to kill the app with only a split-second flash and no
trail. `crash_logger.install()` is armed at the very top of `toolbox_gui.py`
(before the feature imports, so even an import-time failure — a module the
installer forgot to ship — is caught) and hooks `sys.excepthook`,
`threading.excepthook`, and `tkinter.Tk.report_callback_exception`. On a crash it
writes `logs/crash_<timestamp>.log` (app/Python/platform header + full traceback)
and shows a native ctypes message box pointing at the file, so the crash is
visible even when tkinter itself broke. The four subprocess runners
(`batch_upscale.py`, `tag_and_rename.py`, `conciliate.py`, `video_stabilize.py`) also arm it with
`install(notify=False)` — they write the same crash log but skip the dialog,
since their traceback already reaches the GUI log pane via stderr. Stdlib only.
(An Event Viewer entry was considered but dropped — writing the Application log
needs an elevated, registered source and the app runs non-elevated.) See
`crash_logger.py`.

**Remote upscaling (RunPod)** (0.3.1, experimental; onboarding 0.3.2) — runs the
batch on a **rented RunPod GPU** for users without a strong local GPU (or whose
GPU hit the degradation bug). Set "Run on" to "Remote: RunPod" on the Batch Upscaler tab:
the app creates a disposable pod, streams **one image at a time** to a resident
on-pod worker that loads SeedVR2 once, fetches each result back, and tears the pod
down — the queue, resume-cache, film-strip and watchdog all stay local; the source
is never touched (only a copy is uploaded). A **dead-man's switch** on the pod
(max-runtime + idle-timeout) guarantees a billed pod can't be left running even if
the connection drops. The max-runtime hard ceiling **defaults to 0 (no limit)**
(0.3.4) so a long batch of many images is never cut off mid-run — the **idle
timeout** (default 15 min) is the real switch that ends a billed pod on a dropped
connection. Models live on a **region-locked network volume** (written
once, mounted on every pod). Settings has a **world-wide Region + Data center
picker** (0.3.4, `runpod_client.data_centers` GraphQL live list grouped into
Europe / North America / Asia / Oceania via `region_of`) that offers
**storage-capable data centers only** — so a user anywhere picks the right region
and can't provision a model volume in a DC that can't host one; the volume buttons
act in the chosen DC (with a clear target readout) and selecting an existing volume
syncs the picker to its region. Auto-straighten runs **on the pod** (worker `/orient`)
so the local side needs no torch; a second **telemetry row** shows the pod's
CPU/RAM/VRAM/temp during a run. **0.3.2 made it usable by a non-technical user:**
**zero-config SSH** (the app owns an ed25519 key and injects its public half via
`PUBLIC_KEY` — no key on the RunPod website, no `config.json` editing; Settings →
"Set up SSH key"), a **Local / Remote / Both install-mode wizard** (Remote-only
installs skip the ~3 GB local GPU stack), and **one-click model-volume
provisioning** (Settings → "Provision models…"). **Remote Tag & Rename** (0.3.2)
works the same way — set "Run on" to "Remote: RunPod" on the Tag & Rename tab: the pod
runs Ollama (vision model from the volume) **plus** the orientation CNN in a
lightweight worker "tag mode" (no SeedVR2, so the VRAM is free for Ollama);
`tag_and_rename.py` still runs locally (reads/writes the files, does EXIF/rename)
but its Ollama URL is repointed at an ssh tunnel and auto-straighten detection
runs on the pod. `provision.sh` caches the full **Ollama runtime** on the volume
(the `ollama` binary **and** its `lib/ollama/` dir, which holds the separate
`llama-server` + GPU runners — caching only the binary 500s every inference with
"llama-server binary not found"); `remote_run._start_ollama` trusts the cached
runtime only when `llama-server` is present, else it falls through to a fresh
install (so an older binary-only volume self-heals). **Provisioning is incremental
& self-pruning** (0.5.5): a re-provision keeps every still-valid artifact and
(re)fetches only what changed (the venv is skipped via a requirements+torch stamp,
the cached ollama runtime is reused instead of re-installed, SeedVR2 weights skip
valid files), AND prunes obsolete models (Ollama models outside the desired set via
`ollama rm`, stale SeedVR2 DiT weights once the new one is confirmed) so switching a
model reclaims storage instead of piling up. So a model change is a cheap
re-provision, not a fresh volume + full ~40 GB re-download. `provision.sh` also
caches the **common model set** by default so the remote model can be switched with
no re-provision at all: **all three vision tiers** (`OLLAMA_MODEL_LIST` = qwen3-vl
2B/4B/8B-instruct) AND **all three SeedVR2 DiT tiers** (`DIT_MODEL_LIST` = 3B Q8 /
7B FP8-mixed / 7B FP16, ~26 GB, fits the 50 GB volume), each plus the configured
model (de-duped). The configured DiT is required (its download failing fails the
provision); the extra tier DiTs are best-effort. Both `OLLAMA_MODEL`/`_ollama_model`
and `DIT_MODEL`/`_dit_model` follow config (`ollama.model` / `upscale.dit_model`) via
`runpod_provision._load_config`. Flags: `FORCE_VENV` / `FORCE_ENGINE` /
`OLLAMA_PRUNE=0` / `SEEDVR2_PRUNE=0`. Tagging uses a **cheap GPU tier**
(`runpod.tag_gpu_type_id` →
an ordered fallback chain of 16–20 GB cards in `TAG_GPU_TYPES`; the vision model
needs only ~6.6 GB), not the upscale GPU. **0.3.3 added a live GPU picker** next
to each tab's "Run on" selector: it queries RunPod's **GraphQL** endpoint
(`runpod_client.available_gpus`) for the cards **actually deployable right now** in
the volume's region — with live price and stock — filtered to a VRAM floor (≥32 GB
upscale, ≥16 GB tag) and **sorted cheapest-first**, so a user can no longer pick a
GPU that only fails at create time (the curated `GPU_TYPES`/`TAG_GPU_TYPES`
picklists could, and EU-RO-1 routinely has all four tag cards out of stock). The
selection overrides the configured default for that run and seeds a price-ordered
fallback chain (passed to `RemoteSession` via `IMGTBX_GPU_OVERRIDE`); the Settings
comboboxes remain the persisted *preference* (pre-selected when in stock). Two
things learned from live testing: (1) **pods are created via the GraphQL deploy
path** (`runpod_client.deploy_pod` → `podFindAndDeployOnDemand`, the same call the
RunPod console uses), **not** REST `create_pod` — the REST create enum 400s on
newer cards (Blackwell PRO 4000/4500) the GraphQL catalog lists with live stock,
so deploying via GraphQL lets the picker offer the **full** catalog (incl. the
cheap RTX 2000 Ada at ~$0.24) and matches the website 1:1. The one gotcha GraphQL
needs spelled out: a mounted network volume requires an explicit `volumeMountPath`
(REST defaults it) or the container fails with "field Target must not be empty"
and never gets a public IP. The deploy also passes **`allowedCudaVersions`**
(`runpod_client.allowed_cuda_versions`, derived from the image's `cuXYZ` tag) so a
pod only lands on a host whose driver can run the image — a CUDA-12.7 machine
can't start the cu128 image, and that used to burn the whole GPU fallback chain.
**This floor is applied to consumer GeForce cards only** (`is_consumer_gpu`): they
have no CUDA forward-compat, so the image won't start on an older driver.
Datacenter/pro cards (A100, H100, B200, A40/A6000, L4/L40, RTX PRO/RTX A…) DO
forward-compat and run the image on older drivers, so a floor only *excludes*
otherwise-deployable in-stock hosts (an A100 PCIe @ 12.4–12.7 in EU-RO-1 runs
cu128 fine, yet the floor refused it with "no instances available" while the
console showed the card available) — so the deploy omits the floor for them,
matching the website deploy that works;
(2) **no GPU-type substitution** (0.4.0): a run deploys **only the card the user
picked**, never a cheaper/pricier substitute. The old automatic fallback chain (and
its per-task price ceilings `runpod.max_price_per_hour_upscale` /
`max_price_per_hour_tag`, plus `_fallback_ceiling` and the Settings → Remote
spinners) are **removed** because silent type-switching surprised the user during
benchmarking. If the picked card is sold out at deploy time the run fails cleanly
and the status line points the user to press the picker's ↻ to refresh stock and
re-pick. `_selected_gpu_chain` now returns just `[picked_id]`; the three deprecated
price keys are dropped from `config.json` on the next Settings save. If nothing meets the VRAM floor the
run is refused up-front with a clear message instead of spinning a doomed pod, and
a failed run now surfaces the real cause on the status line (pointing at "View
log") rather than the old "see the messages above" (there were none — the clean
output is in the log window, not the tab). See
the remote-pod module cluster below and `docs/runpod-notes.md` /
`docs/future-features.md` #1.

## Codebase structure

The app's Python modules live in **`scripts/`** (0.2.8 — previously the repo
root). Data, config, the `.venv` and the vendored `seedvr2/` engine stay at the
**app root**; each module resolves root-relative resources through an `APP_ROOT`
= parent-of-`scripts/` (paths anchored off `__file__`, never the cwd). Line
counts give a sense of weight:

| File (`scripts/`) | Role |
|------|------|
| `toolbox_gui.py` (~65 lines) | GUI **entry point / thin shim** (0.4.3). Arms crash logging **before** importing the `gui/` package (so an import-time failure still logs + shows a dialog), then imports `App`/`main` from `gui.app` and re-exports the public API (`App`, `main`, `APP_VERSION`, `GUI_MARKER`, `ToolTab`, `funds_color`, `fmt_funds`, ...) so `import toolbox_gui` callers and the tests are unchanged. `Image Toolbox.cmd` / bootstrap / installer all run this file. |
| **`gui/` package** (~19k lines across 21 modules, 0.4.3; +video playback 0.4.7, +the upscaled-image browser and the Stabilization queue 0.6.0) | The tkinter GUI, split out of the former single `toolbox_gui.py`. Built bottom-up so imports never cycle: **`gui/common.py`** (foundation: paths/version, config.json + gui_settings.json helpers incl. the `wizard_done` flag, funds/mqtt/ollama probes incl. `ollama_model_present` + streaming `ollama_pull`, `GUI_MARKER`, `CFG`); **`gui/widgets.py`** (Tooltip, ProgressBar, TelemetryRow, LogPane, ConsoleBuffer, LogViewer, `_ScrollFrame`, sanitize/_fmt_eta/_log_hms; the LogViewer has a **"Collapse repeating progress lines"** toggle, 0.4.8, `gui_settings.log_collapse_processing`, default on: a run of the per-minute video "Processing:" heartbeat collapses to just the latest line via `LogPane.set_collapse`/`COLLAPSE_PROCESSING_RE`, display-only so the on-disk log keeps every line; 0.5.2 adds `Tooltip.set_text()`,
which RETARGETS a hint whose button changes label (a second `Tooltip` on one
widget does NOT override the first: it binds `<Enter>` with `add="+"`, so both
pop up stacked), and `use_window_button_style()`, the bold label for a button
that opens a window which is exclusive/modal/persistent AND wants prolonged focus
(Segments…, Benchmark GPU…, Provision…, the first-start wizard; log windows stay
plain by design, the rule is recorded above the style constant)); **`gui/comparison.py`** (ComparisonWindow + VideoComparisonWindow, the floating before/after wipe views, 0.2.9, + the **lens view** on both of them, 0.6.0/#14, whose geometry is three pure display-free functions at module level, + `VideoPlaybackWindow`, the libVLC real-time side-by-side player with audio, 0.4.7); **`gui/filmstrip.py`** (FilmStrip thumbnail wall, green/red outcome frames, + the additive `show_page`/`page_count` #22 needed to drive pages without a "current" image); **`gui/browse_upscaled.py`** (0.6.0/#22, `BrowseUpscaledWindow`: browse an already-upscaled tree and compare any pair long after the run ended — folder tree + paged thumbnail wall, pairing by inverting the upscaler's own mirror, opt-in content matching; its pairing/paging arithmetic is pure module-level functions); **`gui/wizard_recommend.py`** (0.4.6, pure/tkinter-free: the GPU-VRAM → model tier logic, unit-tested); **`gui/wizard.py`** (0.4.6, `FirstStartWizard`: first-launch GPU-aware model onboarding); **`gui/tooltab.py`** (`ToolTab` base: subprocess plumbing, `@@TBX@@` marker parsing, preview strip, MQTT/taskbar task-state publishing; plus **`MqttTaskState`**, 0.5.8, the widget-free `task/*` + `last_run` publishing on its own so the non-ToolTab Video Upscaler mixes it in instead of going silent); one module per tab (**`tab_upscale`/`tab_tag`/`tab_settings`/`tab_runpod`/`tab_conciliate`/`tab_video`/`tab_stabilize`**, the last of which grew #23's folder loader + queue list and, unusually for this app, OWNS its status line while a video runs — the runner's per-pass message cannot know it is one of fifty); the Video Upscaler's two 0.4.7 helpers **`gui/video_player.py`** (the shared libVLC player: bootstrap-downloaded libVLC, software `wingdi` vout for crash-safety, fail-safe if libVLC is absent) and **`gui/video_segment_picker.py`** (the scene extractor's in/out range picker on a live preview); **`gui/telemetry_graph.py`** (0.5.3, `TelemetryGraphWindow`: the per-run telemetry usage-graph window, feature #9 — embedded matplotlib, capacity-pinned stacked charts, a dynamic/global range-toggle bar, a blitted crosshair; imported lazily + fail-safe, opened by clicking a telemetry row, reads a `system_telemetry.TelemetryHistory`); + **`gui/dialogs.py`** (UpdateDialog + `OllamaPullDialog`, the modal one-model pull); and **`gui/app.py`** (`App` window hosting the seven tabs + `main()`; shows the wizard on first launch). Tabs talk to `App` only via `self.app` at runtime, so no tab imports `gui.app`. The installer ships `..\scripts\gui\*.py` (its own `[Files]` entry — the top-level glob is non-recursive) and the import smoke test sweeps every `gui.*` module. |
| `batch_upscale.py` (~1.5k lines) | Upscale batch runner (CLI + GUI-driven). Walks the source tree, mirrors it to the output root via `os.path.relpath`, drives `UpscaleEngine`, manages the resume cache in `scans/`, and sends Discord notifications. Auto-straightens (0.2.7) before upscaling: `detect_rotation` runs the `orientation.py` CNN, `_make_straightened_copy` rotates a temp copy upright (source untouched), and the skip/target math uses the upright dimensions (`_skip_for_dims`; `should_skip_resolution` is conservative — only skips when both orientations would). |
| `upscale_engine.py` (~250 lines) | `UpscaleEngine` — wraps the in-process SeedVR2 pipeline (`seedvr2/inference_cli.py`). Loads DiT/VAE once and caches them; loads images with EXIF orientation; writes output atomically (temp + rename), format per extension. **RGB-only end to end** (`convert("RGB")` in, `arr[..., :3]` out, frame 0 only), which is why alpha / multi-page / >8-bit images are detected and skipped upstream rather than silently flattened here (#17). `_save_image` also carries the SOURCE's metadata onto the output via `exif_copy` (#13a) with a retry-without-metadata fallback, so an odd EXIF block can never cost the image. **GPU work happens wherever this runs** -- which is why the metadata copy lives here and not in `batch_upscale`: on a remote run the pod writes the file. |
| `tag_and_rename.py` (~1.7k lines) | Tag & Rename runner. Calls Ollama, writes EXIF, renames, records an undo cache; integrates auto-straighten. Has its own Discord + cache-schema versioning. |
| `conciliate.py` (~750 lines) | Conciliation runner (CLI + GUI-driven), images **and videos** (#5, 0.5.1). Two phases over stdin (`run`/`q`): scan builds the original→processed plan over `MEDIA_EXTS` (matching by content-hash lineage first — path-independent, survives folder moves — then a mirrored-name fallback for images only; videos are lineage-only so a partial clip can't be mistaken for a whole-video match), then run archives/deletes originals and moves processed files into the original tree, backfilling each image pair's missing metadata first (#13b, `exif_copy` behind a guarded import). Every file action is journalled to the DB before it happens (`UndoRecorder`), and a third mode, `--undo`, reverses an archive run from that journal (#18, `undo_one`/`run_undo`). No GPU/heavy imports — pure file I/O plus that one optional Pillow call. |
| `video_stabilize.py` (~1.2k lines) | Video Stabilization runner (CLI + GUI-driven, #20 + #23, 0.6.0), stdlib + the bundled ffmpeg, torch-free, no GPU. Two-pass `vidstab` per file: `vidstabdetect` measures the whole clip's camera motion, `vidstabtransform` smooths it and writes the deliverable (audio carried in the same command, 10-bit via `vp.delivery_pix_fmt`, `.part` + rename so a killed run leaves no truncated file). Two things here are load-bearing rather than routine. **`vidstab_health`** proves the filter is DETERMINISTIC before touching a real video, because every ffmpeg 8.1.x silently corrupts its output (see "Video Stabilization" above); it is behavioural, not a version check. And the **transform-file path never enters the filter string**: `result=`/`input=` take a path inside a filter argument, where `:` separates options, so an absolute Windows path fails with a bare "Invalid argument" naming neither the filter nor the path — and the obvious one-backslash escape STILL fails (measured; only a double-escaped colon works). The module sidesteps it entirely by writing the `.trf` into a private temp dir and passing a BARE filename with the child's `cwd` set there, which is immune to spaces/quotes/brackets in the user's paths too, not just the drive colon. `vp._run` gained a `cwd` parameter for it. #23 added the **queue** on top, in pure module-level functions so it is tested without ffmpeg: `iter_videos` (the pruned walk that also refuses its own `_stabilized` outputs, the case `DerivedPruner` structurally cannot catch), `plan_jobs` (skip-what-is-already-there + a claimed-names set), `QueueProgress` (ONE bar and ONE ETA across the run, in pass-frames), `preflight` (hoisted out of `stabilize` so the health check runs once per run, not once per file) and `run_queue` (one file's failure is reported and skipped, a Stop ends the run). Three CLI shapes: `<file> [output]` unchanged from #20, `<folder> --outdir` for headless batches, and `--queue <json>` for the GUI (a file, not a command line, because a few hundred videos would blow past Windows' argument limit and because the GUI is the authority on each output name). |
| **Video Upscaler cluster** (`batch_video_upscale.py`, `video_pipeline.py`, `video_estimate.py`, `remote_video_engine.py`, `local_video_engine.py`, `local_video_worker.py`, `video_benchmark.py`, `video_vram_sizer.py`, `gui/video_benchmark.py`; plus `pod/bench_video.py`, `pod/ram_probe.py`) | The Video Upscaler (future-features #2, remote; local GPU path #7 added 0.5.0). `batch_video_upscale.py` (~1.4k lines) is the orchestrator (CLI + GUI-driven): walk → split → stream each segment to the pod → reassemble → drift check, with resume/installments from the db.py `video_*` tables and an injected engine (`--passthrough` runs the whole pipeline with a local stream-copy no-pod engine for testing). When the pod **OOM-recovers** a segment's batch (e.g. 33→9), that corrected batch is **carried forward** to the same video's later segments (`updated_learned_batch`, 0.4.8) so they start at the safe size instead of re-discovering it (a failed forward pass + VRAM churn) on every segment; an explicit config batch stays the ceiling. **Self-healing (0.5.0, future-features #6, video only):** with the GUI's opt-in `IMGTBX_AUTO_RESUME`, `_run_supervised` wraps deploy + `run_queue` in a heal loop, `run_queue(auto_resume=True)` re-raises a pod-liveness failure as `PodLost` (not a source `fail_count` bump), and the loop reconnects a surviving pod (`_pod_still_running`) or waits unbounded for the identical card (`_wait_for_gpu_stock`) and redeploys it; the funds guard / user Stop / completed queue are the only non-redeploy stops. `video_pipeline.py` (~700 lines) is ALL the local ffmpeg container work (probe / plan_split / split / CFR-normalize / forced-keyframe re-encode / **deinterlace** / concat / audio-mux / duration-drift), stdlib + bundled ffmpeg, torch-free, never touches the source. An **interlaced source** (`detect_interlaced`: idet when `field_order` is unknown, e.g. a MiniDV 576i WMV) forces a `bwdif=mode=0` deinterlacing re-encode: interlaced fields upscale combed AND NVENC has no interlaced-HEVC path, which had produced an all-black deliverable (0.4.8 fix). A **black-output guard** (`mean_luma_head` / `is_black_reencode`) aborts a video whose first segment is black while the source isn't, *before* it is streamed to the pod. `video_estimate.py` (~200 lines) is the GUI cost/duration estimator (`recommend_gpus` intersects the live GPU list with a per-(target,GPU) rate table, drops cards below the target's VRAM floor, sorts by cheapest total queue cost). `remote_video_engine.py` (~200 lines, `RemoteVideoEngine`) subclasses `RemoteUpscaleEngine` to reuse its ssh-tunnel/health/telemetry/close machinery but streams a segment **async** (submit/poll/fetch) since a segment takes minutes to hours. On the pod, `pod/bench_video.py` (Phase-1 per-frame + max-batch/VRAM benchmark) and `pod/ram_probe.py` (validates streaming bounds RAM vs. load-all) answer the GPU questions in `docs/video-upscaler.md`. `local_video_engine.py` / `local_video_worker.py` run SeedVR2 **in-process on the local GPU** (feature #7, 0.5.0): `video_vram_sizer.py` sizes the batch predictively from the card's VRAM (OOM back-off + the degraded-GPU watchdog), and the default in-process path avoids a per-segment subprocess. `video_benchmark.py` + `gui/video_benchmark.py` are the one-click **Benchmark GPU** sweep (VRAM-aware geometric-climb + binary-refine, warm-up) that measures each target's real ceiling and s/frame on the actual card (LOCAL or a rented pod), persisting per-probe rows to db.py `video_bench` and calibrating the AUTO batch, the offered targets and the estimate; resumable. Runs through the same `@@TBX@@`/runner_common seam and `pod/worker.py --mode video`. |
| **Real-ESRGAN engine cluster** (`esrgan_models.py`, `fixed_ratio_engine.py`; plus `pod/worker.py --mode esrgan`) | The second video upscaling engine (feature #11, 0.5.6): fixed-ratio **Real-ESRGAN** (a GAN) as a fast, VRAM-light alternative to SeedVR2's per-frame diffusion. `esrgan_models.py` (~220 lines) is the torch-free model catalog: two tiers (`compact` = `realesr-general-x4v3`, native x4 only; `quality` = `RealESRGAN_x4plus` x4 + `RealESRGAN_x2plus` x2), `tier_scales`/`resolve_for_ratio` (native-scale weight per requested ratio, no fake x4-then-downscale), and the pinned weight URLs + SHAs (self-download). `fixed_ratio_engine.py` (~430 lines, `FixedRatioVideoEngine`) is the drop-in `process_segment` engine that decodes → tiled spandrel upscale → encodes, batch=1 optimal, reporting peak VRAM; its segments ARE the deliverable, so it encodes **10-bit where the codec allows** (`_delivery_pix_fmt`: `hevc_nvenc`→`p010le`, `libx265`→`yuv420p10le`, else 8-bit `yuv420p`) instead of inheriting `pick_encoder()`'s 8-bit default, which was chosen for the split's throwaway intermediate and made this engine quietly worse than SeedVR2's 10-bit output. The two 8-bit fallbacks are deliberate: `h264_nvenc` has NO 10-bit path (measured: `p010le` → "No capable devices found"), and libx264's 10-bit is H.264 High10, which most TVs can't hardware-decode; runs **local** in-process or **remote** via the volume-free esrgan pod (`RemoteSession(mode="esrgan")`, own pod name `esrgan-toolbox-remote`, RunPod cuda12.4.1 image; the worker builds the engine per job and swaps the model per `&model=` query). Wired into the Video Upscaler via a **Method** switch: each job carries its `engine` + picked GPU (a per-item GPU column on `video_outputs`), the queue groups by (engine, GPU) and Start runs each group on its own pod (grouped multi-pod, dynamic re-grouping mid-run). The estimator and Benchmark GPU window treat ESRGAN as a distinct method (single s/frame + peak-VRAM probe per cell, no batch sweep, no compile; a separate `esrgan-mp-<tier>` rate namespace so a GAN's rate never proxies SeedVR2's ~100x-costlier diffusion). See `docs/local-video-upscaler.md` §23, `docs/video-upscaler.md` §18. |
| **torch.compile enablement** (`triton_setup.py`, `msvc_setup.py`) | Local video `torch.compile` needs BOTH halves (feature #7, 0.5.0): Triton AND a C compiler. `triton_setup.py` installs the pinned, SHA-verified `triton-windows` wheel on demand and redirects the inductor/Triton caches to a space-free dir (PyTorch doesn't quote the compile source path, so the default `%TEMP%/torchinductor_<user>` breaks any account name with a space). `msvc_setup.py` finds and ACTIVATES MSVC (runs vcvarsall/VsDevCmd, or builds the INCLUDE/LIB/PATH env straight from disk) and **verifies it by compiling a hello-world** (Visual Studio never puts cl.exe on PATH, and a stub cl.exe with no SDK passes every cheap check). `batch_video_upscale.gate_local_compile` probes both halves and runs uncompiled if either is missing, preventing the piped-stdio first-segment hang; the first-start wizard offers the Triton install + a link to Microsoft's C++ Build Tools page. Stdlib + pip; fail-safe. |
| `funds_guard.py` (~150 lines) | Money safety-net for remote runs (roadmap #1, item 3). Two independent, OFF-by-default, fail-safe protections: a **start floor** (refuse to start a run if finishing the estimate would drop the account balance below a configured floor) and a **session cap** (auto-stop the pod once this run's accrued cost crosses a cap, or the live balance falls below the floor). Balance comes from `runpod_client.account_balance` (GraphQL `myself{clientBalance}`, not in REST); this module keeps only the pure, unit-tested decision logic plus a small background poller (the fetch is injected so it stays offline-testable). Complements the on-pod dead-man's switch (that guards a *forgotten* pod; this guards a *working* one draining the account). Stdlib only. |
| `benchmarks.py` (~120 lines) | Author-benchmark lookup for the remote-pod "$ / 100 images" cost readout (0.3.9). Reads the human-maintained `docs/image-benchmarks.csv` (shipped to `{app}/docs` by the installer) and answers "what did 100 images cost on this GPU in the author's runs" for a task+card+live price; the user's own `db.gpu_perf` history supersedes it once they've run a card enough. Pure stdlib, fail-safe (any miss → None → GUI shows "N/A"). |
| `bench_share.py` (~330 lines) | Benchmark sharing (0.5.1, future-features #8), torch-free/stdlib, fail-safe. The CSV serializer for the shared per-card VIDEO benchmark summary (`write_csv`/`read_csv`/`to_text`, a `# imgtbx-bench v1` sentinel, a malformed row is skipped not raised); `fetch_community` (anonymous GitHub GET of the curated `docs/video-benchmarks.csv` via `net_ssl`, offline-cacheable); `new_rows` (dedup a contribution against the published corpus by measurement identity, ignoring volatile date/price); and the **maintainer** `--merge` tool (ingest submissions into a private gitignored `benchmarks.db`, newest-wins dedupe, a physical-plausibility **sanity gate**, re-export the diffable master). Consumed by `video_benchmark` (`build_share_rows`/`import_rows`/`auto_update`) and `gui.common.contribute_benchmark`. See docs/benchmark-sharing.md. |
| `exif_copy.py` (~330 lines) | Metadata transfer (0.5.9, future-features #13), Pillow-only and fail-safe. `exif_for_upscaled()` reads the source's block, normalises Orientation to 1 (the pipeline already applied it), corrects the pixel dimensions to the output size, and strips a TIFF source's structural tags before they can describe a strip layout inside a JPEG; `save_kwargs()` splats it into `Image.save` for the formats that can carry one (BMP is excluded: Pillow accepts `exif=` and writes nothing). `pending_backfill()` / `backfill()` are Conciliation's retroactive half: "copy what is missing, keep what is present", JPEG via `piexif.insert` (byte-identical scan data) and PNG via an atomic lossless re-save, everything else refused with a named reason. Every entry point swallows its own errors and returns "nothing to do" -- 13a runs inside an upscale and 13b runs one statement before an archive or a delete. Pushed to the pod with `upscale_engine.py`. |
| `raw_decode.py` (~380 lines) | RAW / DNG input (0.6.0, future-features #19). Owns `RAW_EXTS`, the header-only `raw_dimensions()` (LibRaw, flip-corrected, and the reason a RAW never reaches Pillow), `render()` (the preview-vs-demosaic choice plus the container+preview metadata merge), `thumbnail()` for the film strip, and `render_name()` (the `_raw.jpg` rule that stops a RAW and its sibling camera JPEG mapping to one output). The geometry is pure module-level functions (`upright_size` / `preview_is_full_size`), tested without rawpy. rawpy and Pillow are lazy inside it, so importing it stays instant and torch-free -- which is why `runner_common` can import it at module level for the one shared extension list. |
| `runner_common.py` (~300 lines) | Shared runner scaffolding (0.4.3), stdlib-only/torch-free. The single source of the pieces the four runners used to each copy: `load_config()` + `APP_ROOT`, `harden_stdout()` (UTF-8 stdout, now applied by every runner, not just video), the `@@TBX@@` event protocol (`GUI_MARKER`/`stdin_is_piped()`/`GUI_MODE`/`gui_event()`), the `fmt_duration`/`fmt_mmss`/`fmt_hhmmss` helpers, `get_image_dimensions()` + the 5 Pillow-free header parsers (superset with a Pillow fallback; fixed a latent lossy-WebP mis-parse), `is_oom_error()`, `remote_pod_stopped(session)`, and (0.5.9) **`DERIVED_DIRNAMES` / `derived_dirnames(cfg)` / `DerivedPruner`** — the one list of folder names the app itself creates, pruned from every input walk (see "Derived-directory pruning" above) — plus **`image_variant_reason()` / `is_variant_reason()`** (#17), the header-only "the engine cannot round-trip this image" detector shared by the Batch Upscaler and Conciliation. Each runner re-exports these under its old local names, so nothing else changed. Loggers, the pause/stdin controllers, and the `send_notification` wrappers stay per-runner (divergent by design). |
| `db.py` (~400 lines) | Shared SQLite cache layer (`db/cache.db`, WAL). Tables: `upscale_roots`/`upscale_files` (eligibility cache); `tag_roots`/`tag_files` (tag & rename cache, full entry as JSON plus indexed columns); `lineage` (content-hash links source→upscaled→tagged, so conciliation can re-match files after a folder move/rename; the Video Upscaler records a whole-video source→output row here too, 0.4.9 item 10); `file_hashes` (memoised blake2b hashes by path+mtime+size, shared by all tools;
`cached_hash` is its read-only half, which returns a hash only if one is already
stored and never opens the file); `conc_runs`/`conc_actions` (0.5.9, #18: the
Conciliation undo journal, one row per run and per file action, written before the
operation it describes, pruned to the newest 10 runs per folder); `stab_pairs` (0.6.0,
#23 item 5: source→stabilised links, **in their own table precisely so no conciliation
query can reach them** — see the schema comment and the Video Stabilization section
above; keyed by source path, newest wins, and `stabilized_output` returns a path only
if the file is still there); the Video Upscaler's `video_roots`/`video_files`/`video_outputs`/`video_segments` tables (a `fail_count` column on `video_outputs` drives the 0.4.9 give-up-after-N triage) plus `video_batch_learn` (0.4.9 item 9: adaptive batch keyed by GPU id + output-MP bucket, 90-day staleness, newest-wins). `get_conn()` opens once per process (a single shared connection, `check_same_thread=False`); on first creation it imports the legacy `scans/*.json` and `trcache/*.cache` files whose source folder still exists (stale ones skipped). **Thread safety** (0.4.3, item 8): the GUI touches that one connection from short-lived Video-tab worker threads, so every helper that uses it is wrapped `@_locked` (a module-level reentrant `_LOCK`) to keep each read-modify-write atomic instead of interleaving statements on the shared connection; `get_conn` double-checks under the lock (`_open_conn` builds it). `hash_file_cached`/`content_hash` are intentionally left unlocked (they read whole files, incl. multi-GB videos, and holding the DB lock across that would stall everything; their only race writes an identical memoised digest). Logs are deliberately NOT in the DB. Benchmark sharing (0.5.1, #8) adds a `source` column to `video_bench` (migrated default `'local'`; `record_bench_probe` stamps `'local'`) and `import_bench_rows` (writes downloaded rows as `'imported'` to `video_bench` + `video_batch_learn` with **local precedence**: a cell the user measured locally is never overwritten). |
| `orientation.py` (~180 lines) | Auto-straighten: a small pretrained CNN (`ternaus/check_orientation`) detects sideways photos and losslessly rotates them upright; fails safe (leaves ambiguous/upside-down alone). `_rotate_file` preserves the file's metadata on BOTH branches (0.5.9, #13): the JPEG branch always did via piexif, but the non-JPEG one was a bare Pillow re-save, so straightening a PNG/WebP/TIFF silently stripped its capture date; both now clear only the Orientation tag they just invalidated. Heavy imports are lazy. `unload()` (0.5.2) drops the cached CNN and frees its VRAM (~90 MB), reloading lazily on the next `analyse()`; it reads torch from `sys.modules` rather than importing it, so a Remote-only install (no torch) is unaffected. Both pause paths call it: **a pause frees EVERY resident model, with no size-based exceptions** (an exception is one more rule to remember). |
| `updater.py` (~170 lines) | In-app updater. Queries the GitHub Releases API for the latest tag, compares it to `APP_VERSION`, and downloads/launches `ImageToolboxSetup.exe`. Pure stdlib (`urllib`), network calls meant for a background thread; the GUI (`UpdateDialog`, Settings "Updates" section) owns the UI. |
| `system_telemetry.py` (~180 lines) | System telemetry sampler (Feature #3a). Stdlib-only, read-only, best-effort: `CpuSampler` reads CPU usage from Windows `GetSystemTimes` (`ctypes`) as a delta between calls; `sample_ram()` reads physical RAM via `GlobalMemoryStatusEx`; `sample_gpu()` shells out to `nvidia-smi` (one call) for VRAM used/total, temperature, **core utilization %, power draw + limit, and core clock** (0.5.3, telemetry graphs #9), returning a **dict** with a per-field safe parse so a card that reports `[N/A]` for a field yields `None` for just that field. `pod/worker._sample_gpu` mirrors the identical query/order. All fail safe to `None`. The GPU query blocks (spawns a process), so the GUI samples from a background thread. Also holds **`TelemetryHistory`** (0.5.3, #9): a GUI-free, per-run in-memory buffer (records only between `start()`/`seal()`, so idle sampling never enters a graph; gap-breaks the line across sample stalls) that feeds `gui/telemetry_graph.py`. |
| `mqtt_publisher.py` (~300 lines) | Optional Home Assistant (MQTT) integration. One-shot helpers (`test_connection`, `publish_state`, `publish_version`) for the Settings "Test"/"Publish now" buttons and the startup snapshot, plus a persistent `MqttClient` that holds the connection for the app's lifetime, sets the availability LWT, replays retained topics on reconnect, and publishes live `task/*` state. Topics split in two (0.5.8): retained **state** (everything historic) and the non-retained one-shot **`EVENT_*`** pair (`event/run_started` / `event/run_finished`), which is deliberately NOT remembered for the reconnect replay — retained state re-fires an automation on every subscribe, events can't. Lazy `paho-mqtt` import; network calls run on background threads (the GUI owns the UI/config). |
| `notifications.py` (~330 lines) | Shared notification layer (0.3.8). The single source of truth for the queue-complete / error alerts, fanning out to **Discord** (webhook embed), **Telegram** (Bot API HTML message), **ntfy** (HTTP publish to a topic; public ntfy.sh or self-hosted) and a **Home Assistant webhook** (0.5.8, JSON POST to an automation's webhook trigger, for an HA user with no MQTT broker). `resolve_settings(cfg)` pulls the `notifications` config section (legacy `upscale.discord_webhook_url` fallback, ntfy server default `https://ntfy.sh`); `notify(settings, title, desc, color, fields)` sends to every configured backend, fail-safe; `send_discord`/`send_telegram`/`send_ntfy`/`send_ha_webhook` are the per-backend senders. GUI helpers: `test_discord`, `test_telegram`, `detect_telegram_chat` (reads the bot's `getUpdates` for the chat ID), `test_ntfy`, `test_ha_webhook`. The HA webhook is **write-only by design**: HA answers 200 both to a webhook id it never heard of and to a request `local_only` refused, so `HA_TEST_OK` says a success proves only that HA answered, no "verified" state is stored, and the user confirms from HA's own Traces; `split_ha_webhook` normalises a full endpoint pasted into either Settings field, and `ha_payload`'s key names are a contract (users' Jinja reads them). **Severity is one table** (0.5.8): `COLOR_GREEN/ORANGE/YELLOW/RED` + `_SEVERITY` (level, Telegram emoji, ntfy tag, ntfy priority) + `level_for()`, with `_COLOR_ALIASES` resolving the pre-0.5.8 flat-UI palette. Runners pass the CONSTANTS, never a raw int (see `docs/notifications.md`). Stdlib only (`urllib`, `html`); the runners replaced their duplicated `send_discord_notification` with `send_notification` → `notify`. |
| `net_ssl.py` (~50 lines) | Shared HTTPS trust context (0.5.0). `ssl_context()` hands urllib an explicit CA bundle from **certifi** (bundled in every install mode), cached per-process, with a fail-safe fallback to the stdlib default context if certifi is absent. Fixes a Remote-only-install blocker: a fresh Windows VM's OS root store often can't verify RunPod's cert, and Python's OpenSSL (unlike PowerShell/SChannel, which auto-fetches roots) fails every HTTPS call with "unable to get local issuer certificate" (the RunPod API-key test was the first casualty). Passed as `context=` to the public-TLS `urlopen` calls in `runpod_client` (REST + GraphQL), `updater`, `notifications` and `vlc_setup`; ignored for the ssh-tunnelled localhost calls, so it's safe to pass unconditionally. Stdlib + optional certifi. |
| `crash_logger.py` (~180 lines) | Last-resort crash diagnostics (0.2.5). `install()` (armed at the top of `toolbox_gui.py`, before the feature imports) hooks `sys.excepthook`, `threading.excepthook` and `tkinter.Tk.report_callback_exception`; on an unhandled crash it writes `logs/crash_<timestamp>.log` (header + full traceback) and pops a native ctypes message box so the crash is visible under `pythonw`. Stdlib only, fail-safe, re-entrancy-guarded. |
| `config_store.py` (~150 lines) | Two-file settings split that keeps secrets out of the tracked config (0.4.3, item 9). `load(app_root)` deep-merges the untracked `config.local.json` overlay over the tracked `config.json`; `save(cfg, app_root)` does the reverse, writing the secret fields (`SECRET_FIELDS`: `runpod.api_key`, `mqtt.password`, `notifications.{discord_webhook_url,telegram_bot_token,ntfy_token,ha_webhook_id}` (a webhook id is the endpoint's only credential), legacy `upscale.discord_webhook_url`) to the overlay and a secret-free copy to `config.json` (base written first, so a failed overlay write can never leak). `base_has_secrets()` drives the one-time GUI migration (`App._migrate_secrets_to_overlay`). Used by all three load sites (`gui/common`, `runner_common.load_config`, `runpod_provision`). Stdlib only, fail-safe. |
| `debug_log.py` (~110 lines) | Fail-safe diagnostic trail (0.4.3, recommendations item 7). `debug_log(msg, exc=None, tb=False)` appends one timestamped, source-tagged line to `logs/debug.log` so the app's many `except Exception: pass` handlers stop being *silent* (a cache that never persists, a dead MQTT publish, a lineage row never recorded, a pod that failed to tear down) without changing the never-crash behaviour. Itself fail-safe (any internal error swallowed) and size-capped (rolls to `debug.log.1` past 2 MB). Imported guarded (`try: from debug_log import debug_log / except: no-op`) by `db.py`, `batch_upscale.py`, `tag_and_rename.py`, `remote_run.py`, `mqtt_publisher.py` so an old install missing it can't break them; the routed handlers are the persistence + money-adjacent ones (cache saves, lineage, DB migrations, pod/tunnel teardown, the live MQTT publish rate-limited to one line per broken streak). Stdlib only. |
| `single_instance.py` (~80 lines) | Windows single-instance guard (0.3.3). `acquire()` (called first in `main()`) takes a per-session named mutex (`CreateMutexW`); a second launch detects `ERROR_ALREADY_EXISTS`, foregrounds the existing window and shows a native message box, then exits — so two copies can't share the SQLite cache / resume caches and corrupt them. Kernel-released on process death (no stale lock, unlike a PID file). ctypes only, fail-safe (any error / non-Windows = allow launch). |
| `taskbar_progress.py` (~170 lines) | Windows taskbar progress bar (0.3.0). `TaskbarProgress` wraps the shell `ITaskbarList3` COM interface **driven straight from ctypes** (manual GUID + vtable calls — no comtypes/pywin32, no new dependency), painting run progress onto the app's taskbar button: `set_progress(done, total)` (green fill), `set_state("indeterminate"/"error"/"none")`, `clear()`. Windows-only, fail-safe (disables itself on any COM failure); all calls come from the GUI/UI thread. Driven via `App.taskbar_*`. |
| `vlc_setup.py` (~180 lines) | In-app libVLC installer (0.4.7, section 16.2). Mirrors `bootstrap.ps1`'s `Install-LibVlc` so an install bootstrapped BEFORE the segment-extractor feature can enable in-app video playback WITHOUT a full reinstall (Settings "Install libVLC now" prompt): downloads the pinned VLC 3.0.21 win64 zip, extracts only `libvlc.dll` + `libvlccore.dll` + `plugins/` into `{app}/vlc` (where `gui.video_player` looks), and pip-installs `python-vlc`. Stdlib + pip only, fail-safe (every entry point returns a status, never raises into the GUI). Uses urllib's DEFAULT UA on purpose (a browser UA makes get.videolan.org serve an HTML mirror-chooser instead of the file). |
| **Remote-pod (#1) cluster** (`runpod_client.py`, `remote_run.py`, `remote_upscale_engine.py`, `runpod_provision.py`, `ssh_setup.py`; plus `pod/worker.py`, `pod/deadman.py`, `pod/provision.sh`) | Remote upscaling on a rented RunPod GPU (shipped 0.3.1; onboarding in 0.3.2). `runpod_client` = stdlib REST control plane (create/start/stop/terminate/inspect pods + network volumes) **plus a GraphQL helper** (`available_gpus`/`_graphql`) — the REST API can't list GPU types/prices/stock, but `api.runpod.io/graphql` can (browser User-Agent to pass Cloudflare), so the GUI's live GPU picker (0.3.3) shows only what's deployable now in the volume's region, cheapest-first. Pods are then **created via GraphQL too** (`deploy_pod` → `podFindAndDeployOnDemand`) — the REST `create_pod` enum 400s on newer cards (Blackwell PRO 4000/4500) the GraphQL catalog can deploy, so using the GraphQL deploy path (as the console does) gives the full catalog. `CREATABLE_GPU_IDS` is kept only as reference/documentation of the old REST limitation. **All response READS go through a normalisation seam** (0.6.1, future-features #25 P0): the app talks to three transports that spell the same pod differently and they stop serving on different dates (REST v1 410s on **2026-11-15**, GraphQL in **early 2027**), so `pod_state` / `pod_cost` / `pod_data_center` / `pod_gpu` / `pod_ssh` / `pod_volume_id` / `pod_record` / `volume_data_center` / `unwrap_list` / `error_detail` accept **every shape at once** and nothing outside the module reads a field by name. That is not tidiness: this module's callers treat None as a decision rather than as an absence, and all three known cases fail toward SPENDING MONEY. A `desiredStatus` that reads None under v2 (where it is `status`) makes `_find_existing_pod` fail to recognise the app's own running pod and deploy a **second billed one** beside it; it makes `runner_common.remote_pod_stopped` return True unconditionally, so the auto-resume supervisor (#6) ends exactly the runs it exists to rescue; and a `costPerHr` that reads None under v2 (where it is `cost`) makes `funds_guard`'s session cap accrue zero and **never trip**. None of that raises and all of it looks like a normal run, which is why `tests/test_runpod_client.py` pins it with payloads **recorded off one real pod read through all three transports**, not synthesised from the spec, and names each money test after the consequence rather than the field. It also sweeps every module for a raw read and fails with `file:line` (that sweep was itself broken and green on the first attempt: see the entry). Measured while building it, and each one changes something: v2 reports `status: RUNNING` **at creation**, ~50 s before `ssh.direct` exists, so `wait_until_running` must keep requiring an SSH endpoint and must never trust the status alone; a v1 pod object carried `machine: {}` and **no GPU field at all**; the GPU label differs per transport (`RTX PRO 4500` on GraphQL vs `NVIDIA RTX PRO 4500 Blackwell` on v2, only the latter deployable) so it is display-only and must never be matched on; and v2 answers errors as RFC 9457 (`title`/`status`/`detail`/`errors[]`), where the `errors[]` array is the half that names the rejected field. A v2 deploy was proven end to end on 2026-08-20 (Blackwell card, `env.PUBLIC_KEY` still gets SSH in with `startSsh` omitted, `ports: ["22/tcp"]` alone still publishes direct TCP, `mounts.network` mounts the volume). **The REST transport is switchable and defaults to v2** (0.6.1, #25 P1): `runpod.api_version` picks `v2` (default) or `v1`, applied once per process by the two config loaders every path already goes through (`runner_common.load_config` for the runners, `gui.common` for the GUI) rather than at each call site, since a call site that forgot would talk to the wrong transport and the symptom would be a silent None. It is **config-only, no Settings control**, like `ntfy_token`: it exists for the day beta churn bites, and `probe_api_version` names the exact key and value to set when the configured version stops answering. **The direction is the decision**: v2 default with v1 as a manual escape hatch, NOT the reverse, because installs do not update in lockstep (a build shipped with v1 as its default would still be running after the 410) and a fallback nobody exercises is a hope, not a fallback. The switch is never automatic per call: retrying a failed pod CREATE on the other transport can leave **two billed pods** when the first call really succeeded and only its reply was lost. What P1 moved: base URL, `/networkvolumes` to `/network-volumes`, the volume create body's `dataCenterId` to `dataCenter` (**the only renamed field in a REQUEST**, which is why it cannot go through the read seam), size bounds checked against the live version (v1 1-4000, v2 10-4096), the three lifecycle verbs collapsing into `POST /pods/{id}/action`, and `create_pod` gaining `v2_pod_body`, which **rebuilds** the body key by key because `unevaluatedProperties: false` turns one leftover v1 key into a 422. `list_pods`' `**filters` was **deleted, not ported**: v2 takes no status filter and IGNORES unknown query params (measured, 200 with the full list), so a filter that quietly stopped applying looks exactly like one that matched everything. A 410 now says "update the app" rather than reporting a status code, which is the only warning anyone gets since the promised `Sunset` headers are still not served. Verified live on a second pod: created through `create_pod` on v2, **stopped via the action endpoint**, and a stopped pod reports **`EXITED`** exactly as on v1 (worth measuring, since v2's `PodStatus` enum is richer and both `ensure_stopped`'s idempotence and `remote_pod_stopped` compare against that value) while keeping a populated `cost`, which is right because a stopped pod still bills its disk. `start_pod` is the one call P1 could NOT prove (RunPod answered 400 "Failed to resume pod", a semantic refusal rather than the 422 a malformed request gets); it is recorded rather than chased because the app never calls it, deploying fresh disposable pods instead. **P2 then moved the GraphQL half too** (0.6.1): `runpod.api_version` now selects a whole STACK, v2 REST + v2 catalog by default or v1 REST + GraphQL as the escape hatch. `deploy_pod` on v2 is a wrapper over `create_pod` (the mutation existed only because v1's create enum 400s on newer cards, and v2 has no such limit), returning the whole pod object rather than the mutation's three fields, so `create_pod_resilient` is untouched; v2 also needs neither `volumeMountPath` (it is `mounts.network[].path`) nor `supportPublicIp` (a 422 by name). The CUDA floor was hoisted into `deploy_cuda_versions` so BOTH paths apply the identical policy: a transport swap must not quietly change which HOSTS a run can land on. v2's `gpu.minCudaVersion` is a real numeric floor and exactly what `KNOWN_CUDA_VERSIONS` enumerates around, and it was **deliberately not adopted here**, because changing transport and host-selection policy together would make a bad landing impossible to attribute (that is #25 P4). `available_gpus` and `data_centers` moved onto `/v2/catalog/*` and were validated by running both implementations against the live account: the GPU lists came back **byte-identical** (same 7 cards for EU-RO-1, same prices, stock, VRAM, names) and the DC lists identical too (18 storage-capable on both). Three things to know about that catalog. **`include=AVAILABILITY` is what makes it a catalog at all**: a plain `GET /v2/catalog/gpus` is a bare price list with no `availability`, `dataCenters` or `cudaVersions`, and the per-GPU endpoint returns the same (the migration guide implies otherwise); it additionally REQUIRES `product=POD`, which has no default because a card can be scarce for pods and plentiful for serverless. **`_stock_label` normalises the level** (`HIGH` vs `High`), since the picker prints that string straight into a combobox label, and maps `NONE` to None so the existing `if not stock` filters are unchanged. And **v2 has no `location`** (`name == id`), so `curated_location` makes the curated `DATACENTERS` labels the display layer with the API answering membership and capability only, which is an improvement rather than a workaround: GraphQL called EU-RO-1, EU-NL-1 and EUR-IS-1 all "Europe". `list_pods_detailed` is ONE plain GET on v2 (`/v2/pods` already carries gpu.id, dataCenterId and cost), with the GraphQL ladder and its memoised `_PODS_MACHINE_SELECTIONS` surviving only on v1. Verified live on the production path with a **consumer** card on purpose, since `is_consumer_gpu` gates the CUDA floor and the shipped default `gpu_type_id` is a GeForce: an RTX 4090 through `create_pod_resilient` to `POST /v2/pods` with `gpu.allowedCudaVersions`, landed, SSH endpoint after ~84 s of RUNNING, correct through `pod_record` / `list_pods_detailed` / `pods_using_volume`, then terminated. **The deletions this earns are deliberately not taken**: `_DEPLOY_MUTATION`, `_GPU_AVAIL_QUERY`, `_DC_QUERY`, `_PODS_MACHINE_SELECTIONS` and `CREATABLE_GPU_IDS` stay reachable on the v1 branch, because an escape hatch missing half its code is not one. They go on their own deadlines. **P3 then settled the one call with no v2 successor at all, the account balance** (0.6.1): re-verified on 2026-08-20 against the live spec and the live account, it is **absent, not undocumented** (34 paths, one `/v2/account/*` and it is ssh-keys; `/account`, `/account/balance`, `/account/credits`, `/user`, `/me` all 404; zero "credit"/"funds"/"wallet" in the spec and all 8 "balance" hits are "load balancer"), and `runpod/docs#807` is unanswered. So the GraphQL island (`_BALANCE_QUERY` + `_graphql`) **stays until it 410s**, and what P3 actually built is the removal of its SILENCE. `funds_guard` is fail-OPEN by contract, so an unreadable balance skips the start floor and the in-run balance floor rather than blocking a run: correct, and invisible, which means a user who configured a floor keeps one that is no longer applied. `account_balance_detail()` classifies a lookup (`BALANCE_OK` / `NO_KEY` / `RETIRED` / `ERROR`, never raises) while `account_balance()` stays byte-for-byte the old plain-pair-or-None wrapper; `RunPodError.status` makes permanence readable without matching on message text; the pure `funds_guard.floor_unenforced()` is the one place that words it, and words RETIRED and ERROR differently (a blip needs no action, a retirement means moving to the per-run cap); the guard's `on_warn` hook is **finally fired**, having been accepted, stored and called by nothing since it was written; and the readout says `Funds: Not published` against `Funds: Unknown`, with the preflight and the "Funds guard armed" line saying it too. Three measurements shaped it. **`/v2/billing` is spend, not balance** (7 days = $0.7632, of which $0.7389 is the standing volume and $0.0243 the ENTIRE live-verification bill for P0 to P2), and a zero-spend bucket is omitted rather than padded, so `lastN=N` does not mean N records. **`spend_per_hr` has no naive successor either**: with ZERO pods running it reported $0.005/h, the volume's standing charge, so summing `pod_cost()` over running pods would report $0.00/h for an account that is genuinely draining. And **GraphQL answers a field that left the schema with HTTP 400**, not the 200-with-errors the plan assumed, carrying `Cannot query field "..." on type "User".` in an `errors` list of OBJECTS where v2's RFC 9457 list holds strings, which is both why the classification reads the message rather than the code and how `error_detail` was caught printing a Python dict repr at the user. Probing trap: Cloudflare answers a bare `urllib` User-Agent with 403 "error code: 1010" on the v2 REST paths too, so a probe script reads it as a rejected key. **Never call `PUT /v2/account/ssh-keys`**: it REPLACES the account's registered keys. `remote_run.RemoteSession` orchestrates a run (create→push→start worker→arm dead-man's switch→stream→teardown); `remote_upscale_engine.RemoteUpscaleEngine` is a drop-in for `UpscaleEngine` that streams one image per HTTP request over `ssh -L`. `_push_files` uploads `upscale_engine.py` + `orientation.py` + `exif_copy.py` each run (so an engine-side change like #13a's metadata copy needs no re-provision) plus the whole `upscale` config section as `worker_settings.json`. **The pod name is mode-aware** (0.4.3): Image Upscaler + Tag & Rename share `image-toolbox-remote` (both image-side, safe to reuse each other's pod), the Video Upscaler gets its own `video-toolbox-remote` | and `_find_existing_pod` matches on the same per-mode prefix, so starting an image run and a video run at the same time never makes them reuse (and fight over) one pod. `pod/worker.py` is the resident on-pod worker (serves `/upscale`, `/orient`, `/telemetry`, `/health`); its `--mode` is `full` (loads SeedVR2) or `tag` (skips SeedVR2 and serves `/orient` only — remote Tag & Rename, with `remote_run` also starting `ollama serve` and tunnelling 11434). Before loading the engine it **seeds the seedvr2 validation cache** from the DiT+VAE size+mtime (`_seed_validation_cache`, item 11) so a cache miss never triggers a full 16 GB re-hash on the trusted volume (the ~354 s cold-start worst case); copy-to-local NVMe was rejected (a resident worker loads once per pod, so the extra copy isn't amortised). **`ssh_setup.py` (0.3.2)** = zero-config SSH: locates OpenSSH, generates the app's ed25519 key, reads its public half — handed to each pod via the `PUBLIC_KEY` env so SSH needs no key registered on the RunPod website. `runpod_provision.py` is the dev driver + the GUI's `setup-volume` one-shot (create→provision the model volume→auto-terminate). `pod/upscale_one.py` is a minimal single-image on-pod upscale used to validate the remote stack (the original seed of `pod/worker.py`; reuses the same `UpscaleEngine`). All stdlib + the Windows OpenSSH client; the GUI launches these as subprocesses / background threads. |

Configuration & state:

- `config.json` — persistent settings: `seedvr2`, `ollama`, `upscale`,
  `tagging`, `defaults`, `mqtt`, `updates`, `runpod`, `notifications`, `video`
  sections. (`notifications` = Discord webhook + Telegram bot token/chat ID + ntfy
  server/topic + HA URL/webhook ID (0.5.8), 0.3.8; the Discord URL migrated out of
  `upscale` on first save.)
  (`video` = the Video Upscaler's target/segmenting/SeedVR2 knobs, incl. 0.4.9's
  `auto_tune_batch` (adaptive per-card batch tuning, default on) and `record_lineage`
  (content-hash source-output linking on completion, default on); its
  `watchdog_enabled`/`watchdog_factor` fall back to the `upscale.*` values.)
  (`upscale.copy_metadata` (0.5.9, default true) is read by THREE places, which is
  deliberate: `upscale_engine` (which may be running on a pod), `batch_upscale` for
  the run banner, and `conciliate` for the #13b backfill. One switch, one intention.)
  Edited via the Settings tab; preserved across installer upgrades. **Don't
  hand-edit in normal flow.** **Secrets never live here** (0.4.3, item 9): the
  RunPod API key, MQTT password and notification tokens/webhook URLs are split out
  to an untracked `config.local.json` overlay by `config_store` (see below), so the
  tracked `config.json` is genuinely a credential-free template (secret keys kept
  but blank). `runpod.ssh_key_path` may be left blank — the app falls back to its
  managed key (`ssh_setup.default_key_path`). `runpod.api_version` (0.6.1, #25) picks
  the REST transport, `v2` (default) or `v1`; it is config-only by design and absent
  from the tracked template, so the default lives in `runpod_client`, not in the file.
- `config.local.json` — untracked (`.gitignore`'d) secrets overlay written by
  `config_store.save`: holds ONLY the secret fields, deep-merged over `config.json`
  at load. Absent on a fresh machine (all secrets blank until the user enters them).
- `install_mode.txt` — written by the installer (Local / Remote / Both). Read once
  by `bootstrap.ps1` to decide what to download; a Remote-only install skips the
  local GPU stack (torch CUDA + SeedVR2 + Ollama). Missing = "both" (upgrade-safe).
- `gui_settings.json` — GUI-only state (window geometry, thumbnail size).
- `db/cache.db` — the single SQLite cache (eligibility + tag/rename), see
  `db.py`. **This replaces the old per-folder JSON caches.** The legacy `scans/`
  (upscale) and `trcache/` (tag/rename) folders are now read only once, on first
  DB creation, for the one-time import; they are otherwise vestigial.
- `logs/` — run logs (kept as text files, not in the DB).
  `test_output/`, `samples/` — sample images.

Engine, packaging & CI:

- `seedvr2/` — the SeedVR2 upscaling engine, cloned from
  `numz/ComfyUI-SeedVR2_VideoUpscaler` and used **directly in-process** (ComfyUI
  itself is not needed). Treat as vendored/third-party.
- `.venv/` — the Python 3.12 environment (PyTorch CUDA + seedvr2 requirements).
- `bootstrap.ps1` — first-launch bootstrapper: downloads Python, PyTorch CUDA,
  the SeedVR2 engine, a static ffmpeg build (Video Upscaler + Video Stabilization),
  a bundled libVLC
  (`Install-LibVlc`, in-app video playback, both install modes), and pip-installs
  `paho-mqtt` + `python-vlc` (and `matplotlib` for the telemetry graphs #9, added
  explicitly on Remote-only since it has no seedvr2 stack to pull it in).
  Idempotent. **The ffmpeg pin is a MASTER build, not a release branch** (0.6.0):
  every 8.1.x corrupts memory in `vidstabtransform`, which #20 is built on, and the
  fix (`316531e61cf`) is on master only. It is an immutable dated BtbN autobuild, so
  unlike the old rolling `latest` URL it is **SHA-256 pinned**, and it must be a
  **month-end** one: BtbN keeps roughly the last 14 dailies and then only the last-day-of-month
  snapshot, so the first pin (dated one day before a month end) 404'd two weeks after it was
  written and every install silently took the release-branch gyan.dev fallback, where the
  Stabilization tab then refuses to run. The month-end snapshots go back two years; a `ffmpeg/build.txt`
  stamp is what makes an EXISTING install replace its older bundled ffmpeg, since the
  previous "is ffmpeg.exe there?" check would have kept a broken one forever. Move
  back to a release branch once one ships containing that commit (8.2, or a
  backported 8.1.3). `Image Toolbox.cmd` launches
  it + the app (it launches `scripts\toolbox_gui.py`). The final "starting"
  window auto-closes on a 10-second countdown (press any key to close early).
  The launcher **runs the venv interpreter before starting the GUI** (36 ms) and falls
  back to the bootstrapper when that fails, and bootstrap's venv step asks
  `Test-VenvWorks` rather than `Test-Path`, repairing `pyvenv.cfg`'s `home`/`executable`
  when a compatible Python moved and rebuilding only if that fails: a venv is NOT
  self-contained, so uninstalling the base Python left the app starting into nothing with
  no window, no error and no crash log (`known-defects.md` D1). **The three-for-three
  lesson of 0.6.0 is that present is not working**: the same shape bit the ffmpeg pin (an
  existence check would have kept a memory-corrupting build) and NVENC (`-encoders` lists
  what the hardware cannot run).
- `installer/ImageToolbox.iss` — Inno Setup script; ships only the scripts +
  bootstrap (heavy components download on first launch). It packages every app
  module via a `..\scripts\*.py` glob into `{app}\scripts` (not a hand-maintained
  list — a missing entry broke 0.2.5). 0.2.8's `[InstallDelete]` removes the
  stale root-level `.py` from pre-0.2.8 installs so old/new copies can't coexist. Built by `.github/workflows/build-installer.yml` on `v*`
  tags → GitHub Releases. **Release notes are the annotated tag message:** write
  clean, user-facing notes in `git tag -a vX -m "…"`; CI strips trailers/PGP and
  publishes them as the release body (no auto-generated compare link). The in-app
  update dialog shows that body, further cleaned by `updater.clean_notes()`. So
  when cutting a release, the tag `-m` message IS what users read.
- **Release checklist** (keeps the docs from drifting again — the reason item 10
  existed): before tagging `vX.Y.Z`, (1) set `APP_VERSION` in `scripts/gui/common.py`
  to the tag and drop any `-experimental` suffix; (2) update this file's module
  table + feature list for any new module/tab, and `README.md` (tab count, the
  config-section table); (3) if a new config section or secret field landed, update
  `config_store.SECRET_FIELDS` and the config docs; (4) write the user-facing notes
  in the annotated tag `-m` message. See [release-workflow] in memory for the
  branch/fold mechanics.
- `docs/` — `known-defects.md` (confirmed shipped bugs not yet fixed, with root cause and
  what was done about it; **nothing open as of 0.6.0** - D1 to D4 were all found while testing
  it and all fixed before it shipped. Kept because code comments cite them by id and because
  three of the four are one mistake in different clothes: **present is not working**), `future-features.md` (roadmap: open milestones #24, #21,
  #12/#15, #3/#4; shipped #1/#2/#5/#6/#7/#8/#9/#10/#11/#13/#14/#16/#17/#18/#19/#20/#22
  kept only as a numbering legend), `dropped-ideas.md` (ideas
  investigated and decided against + the standing constraints: AMD/ROCm, vast.ai;
  incl. folding a RAW render back into the source tree, whose revisit trigger is an
  8K target),
  `runpod-notes.md` (remote-pod upscaling notes), `video-upscaler.md` /
  `local-video-upscaler.md` (design + as-built notes for the Video Upscaler: remote #2
  and the local GPU path #7), `benchmark-sharing.md` (as-built notes for the
  crowdsourced benchmark corpus, #8), `tag-and-rename.md` (vision-model design +
  as-built notes for Tag & Rename: the model tiers + the `ollama_num_ctx` cap, with
  `tag-rename-benchmarks.csv` the raw 100-image measurements),
  `raw-preview-survey.csv` (the 24-file RAW measurement behind #19: preview hit
  rate, the IFD-0 trap per camera, and the skip verdict that made "render always"
  the only workable shape), `notifications.md` (the Discord/Telegram/ntfy backends: setup, the severity contract, what was rejected), `mqtt-integration.md`
  (the MQTT/Home Assistant contract: every topic, the retained-state vs one-shot-event
  split, the per-tool `last_run` keys, and what was rejected — the single source for
  anything MQTT), `telemetry-design.md` (the `system/*` topics + the in-app usage
  graphs, #9/#10), `CHANGELOG.md` (working
  draft of per-version release notes), `image-benchmarks.csv` (author benchmark data,
  read by `benchmarks.py`).

## Architecture notes for changes

- **The subprocess + stdin/stdout seam** between the GUI and the batch scripts is
  the clean integration point — new front-ends (web, Home Assistant) should
  reuse the scripts rather than re-implement the pipeline.
- **Keep it dependency-light.** The GUI is overwhelmingly standard-library
  tkinter. The few non-stdlib packages are each a deliberate architectural choice,
  not a default: `paho-mqtt` (Home Assistant); `python-vlc` + a bundled libVLC
  (in-app video playback, 0.4.7, opt-in and fail-safe: absent libVLC degrades to a
  silent frame-scrub); and `matplotlib` (0.5.3, the telemetry usage-graph window
  #9, imported lazily + fail-safe: absent matplotlib disables only the graph, not
  the readout row or MQTT). matplotlib is already present on Local/Both via
  seedvr2; the Remote-only bootstrap installs it too (the pod's live graph). A
  hand-rolled tkinter-Canvas chart was prototyped and rejected: the win is
  axes/scaling/legends/resize for free and a blitted crosshair, not toolkit speed.
  And `rawpy` (0.6.0, RAW/DNG input #19, imported lazily + fail-safe: absent rawpy
  means RAW files are simply not offered, never a broken run). It is a 0.9 MB wheel
  whose only dependency is numpy, which every install already has, and it earns its
  place because LibRaw is the whole feature -- there is no stdlib route to a
  demosaic. It ships in **all three** install modes, Remote-only included, because
  the decode happens LOCALLY even for a pod run.
  Adding anything else (e.g. a web framework) needs the same deliberation.
- **Stay Windows-aware.** Paths, the PowerShell bootstrap, and
  `CREATE_NO_WINDOW` are Windows-specific; cross-platform work (Linux/Unraid)
  requires porting those layers, though the PyTorch/SeedVR2/Ollama core is
  already cross-platform.
- **Run from source:** `.venv\Scripts\pythonw.exe scripts\toolbox_gui.py`, or
  double-click `Image Toolbox.cmd`. Headless: `python scripts\batch_upscale.py
  <src> [out]` and `python scripts\tag_and_rename.py <folder>`.

## Context

Written largely with AI assistance ("vibecoding") by a non-professional
developer; a personal project shared at no cost. Match the existing code's style,
comment density, and the "fail safe / never touch originals" philosophy when
making changes.
