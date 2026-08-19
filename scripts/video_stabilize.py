"""
video_stabilize.py
------------------
Video Stabilization (future-features #20) - stabilise ONE shaky video into one
new file. Driven by the GUI's Stabilization tab, or run headless from a terminal.

Architecturally this is a sibling of conciliate.py, NOT of the Video Upscaler: it
is local ffmpeg work with no GPU, no pod, no VRAM sizing, no batch tuning, no
benchmark corpus and no funds guard. It never touches the source; it writes one
new file at a path the user chose.

The shape, and why:

  * TWO PASSES over the whole file. `vidstab` is a two-pass GLOBAL algorithm:
    pass 1 (vidstabdetect) measures camera motion across the entire clip, pass 2
    (vidstabtransform) smooths that trajectory and warps each frame. That is
    exactly why this is not a stage of the Video Upscaler, which splits into ~60 s
    segments and processes them independently - run per segment, every boundary
    gets a visible jolt, because each stretch is smoothed toward its own mean.
    Outside that pipeline, whole-file is simply its natural shape.

  * COVERAGE OVER STEADINESS (decision 4). The ffmpeg default everyone copies,
    `optzoom=1`, picks ONE static zoom that must cover the worst frame in the whole
    clip, and measured on real camcorder footage that discards ~17-21% of the
    picture - a single jolt in a ten-minute video sets the crop for all ten
    minutes. Old footage is amateur footage from small cameras, and the content at
    the edge of frame is often why the clip is treasured. So the default is
    `optzoom=0` + `crop=keep`, which preserves the WHOLE frame and fills the border
    from previous frames; worst case an extreme edge looks slightly stale for a few
    frames. `smoothing` is the real steadiness/coverage lever and is exposed.

  * OFF BY DEFAULT, opted into per video. Shakiness is not a defect the way
    interlacing is - a handheld pan is how the footage was shot - and the failure
    mode is silent and permanent: content leaves the frame and nothing in the
    output says so. It is never auto-detected either: measured, an UNMODIFIED
    camcorder clip already scores a 9.64% correction, so a detector would fire on
    nearly everything.

  * It COMPOSES BY FILE, not by pipeline: stabilise, then feed the result to the
    Video Upscaler. Stabilising first is the ordering that matters (the crop
    happens at source resolution and the box-fit target still fills the frame),
    and the user's own sequencing preserves it.

  * A QUEUE OF WHOLE FILES (#23 items 2+3), which does NOT reverse the "not a
    batch tool" decision above - that decision is about the ALGORITHM being
    whole-file, and N independent whole-file jobs preserve it exactly. Nobody
    should later "simplify" the queue into segmenting one video. The preflight
    (capability + health) runs ONCE for the whole queue; a file that fails is
    logged and the queue continues, because one unreadable video in fifty must
    not cost the other forty-nine.

  * RESUME IS "THE OUTPUT ALREADY EXISTS", not a database. #20 has no resume by
    design (one file finishes in well under its own duration, so Stop discards
    the .part). A queue of fifty needs file-level resume, and the cheapest
    correct form of it is the one the Batch Upscaler already uses: re-scan, and
    skip what is already there. It needs no schema, it is retroactive, and it
    works on a tree produced by another install. `db.stab_pairs` records the pair
    on top of that for the COMPARISON (#23 item 5), never as the resume state.

THE HEALTH CHECK is not optional (see `vidstab_health`). Every ffmpeg 8.1.x
release CORRUPTS MEMORY in vidstabtransform: libvidstab's vsTransformPrepare()
keeps a stale shallow copy of the source frame when it alternates between its
in-place and separate-buffer paths, and FFmpeg 8.1's scheduler change is what
started making frames arrive non-writable and alternating them. Fixed upstream by
316531e61cf (2026-04-01, FFmpeg #22595), which is on master but NOT on
release/8.1. Measured on a 300-frame 720p clip, 12 identical runs: n8.1.2 gave 12
DIFFERENT outputs, this app's pinned master build gave 12 identical ones. The
crash (intermittent, ~10-40% on some clips) is the LUCKY symptom; the constant one
is silently wrong pixels in a run that reports success. bootstrap.ps1 now pins a
fixed build, but its offline fallback is still a release-branch ffmpeg and a
hand-installed ffmpeg on PATH can be anything, so the tool proves the filter is
sound before it will process a real video.

Usage:
    python video_stabilize.py <input> <output> [--smoothing N] [--shakiness N]
                              [--accuracy N] [--optzoom 0|1] [--crop keep|black]
    python video_stabilize.py <folder> --outdir <dir> [--no-recursive] [--redo]
    python video_stabilize.py --queue <jobs.json>        (the GUI's path)

GUI control lines (stdin):  q   (stop; the partial output is discarded, and any
                                 files not yet started stay unprocessed)
"""

import os
import re
import sys
import json
import time
import shutil
import argparse
import datetime
import hashlib
import subprocess
import tempfile
import threading

# Write a logs/crash_*.log on any unhandled crash. notify=False: this runs headless
# as a GUI subprocess, whose traceback already reaches the GUI log pane via stderr.
try:
    import crash_logger
    crash_logger.install(notify=False)
except Exception:                                    # noqa: BLE001
    pass

import runner_common
import video_pipeline as vp
import config_store
import notifications

APP_ROOT = runner_common.APP_ROOT
runner_common.harden_stdout()

GUI_MODE   = runner_common.GUI_MODE
GUI_MARKER = runner_common.GUI_MARKER
_gui_event = runner_common.gui_event
fmt_duration = runner_common.fmt_duration

_CFG   = config_store.load(APP_ROOT) or {}
NOTIFY = notifications.resolve_settings(_CFG)

# ─────────────────────────────────────────────
#  Defaults
# ─────────────────────────────────────────────

# vidstabdetect. shakiness/accuracy at the top of their useful range: pass 1 is the
# CHEAP pass (measured 3.8x realtime at 1080p against 2.4x for pass 2), so there is
# nothing to buy by measuring the motion less carefully.
DEFAULT_SHAKINESS = 8
DEFAULT_ACCURACY  = 15
# vidstabtransform. Conservative by decision 4: at optzoom=1 this is a direct
# steadiness-vs-coverage trade (measured 9.64% of the frame lost at smoothing=30
# against 4.34% at 10, same clip). At the shipped optzoom=0 + crop=keep nothing is
# cropped at all, so a high smoothing costs border FRESHNESS rather than picture -
# but it is still the knob that decides how hard the correction pulls, so it stays
# low unless the user raises it.
DEFAULT_SMOOTHING = 10
DEFAULT_OPTZOOM   = 0
DEFAULT_CROP      = "keep"

CROP_MODES = ("keep", "black")

# The suffix every result carries. It is load-bearing rather than cosmetic: an
# output defaults to sitting BESIDE its source, which is not a derived directory,
# so DerivedPruner (#16) cannot keep a second scan of the same folder from
# re-offering every result as fresh input. The suffix is what does (see
# is_stabilized_name), and it is the reason the batch setup RECOMMENDS a separate
# output folder rather than relying on the name alone.
STAB_SUFFIX = "_stabilized"
STAB_EXT    = ".mp4"

# Containers the folder walk offers. Mirrors batch_video_upscale.VIDEO_EXTS,
# defined locally for the same reason conciliate.py does: importing that module
# would drag the whole Video Upscaler orchestrator (and its torch-adjacent
# imports) into a torch-free ffmpeg tool.
VIDEO_EXTS = {".mp4", ".avi", ".mov", ".mkv", ".m4v", ".wmv", ".mpg", ".mpeg",
              ".flv", ".webm", ".3gp", ".ts", ".mts", ".m2ts", ".vob"}

# The health check's sample. Long enough that vidstabtransform alternates its
# in-place and separate-buffer paths (which is what detonates the 8.1 bug) and short
# enough to cost about a second. Validated against a known-broken n8.1.2 and the
# pinned master build; see tests/test_video_stabilize.py.
HEALTH_FRAMES = 60
HEALTH_SIZE   = "320x240"


class StabilizeError(RuntimeError):
    """A stabilisation could not be performed. Carries a user-facing message."""


# ─────────────────────────────────────────────
#  Logging
# ─────────────────────────────────────────────

class Logger:
    """Writes to both the terminal and logs/stab_<hash of the run's key>.log.

    The key is the source path for a single file and the common root for a queue,
    so re-running the same thing appends to the same log (a stabilise is judged by
    comparing settings across attempts) instead of scattering one file per run."""

    def __init__(self, key, header=None):
        digest  = hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]
        log_dir = os.path.join(APP_ROOT, "logs")
        os.makedirs(log_dir, exist_ok=True)
        self.path = os.path.join(log_dir, f"stab_{digest}.log")
        self._fh  = open(self.path, "a", encoding="utf-8", buffering=1)
        ts = datetime.datetime.now().strftime("%Y-%m-%d | %H:%M:%S")
        self._fh.write(f"\n{'=' * 64}\nStabilization session: {ts}\n"
                       f"{header or f'Source: {key}'}\n{'=' * 64}\n")

    def _ts(self):
        return datetime.datetime.now().strftime("%Y-%m-%d | %H:%M:%S")

    def tee(self, msg=""):
        print(msg)
        try:
            self._fh.write(f"{self._ts()} | {msg}\n")
        except Exception:                            # noqa: BLE001
            pass

    def log_only(self, msg):
        try:
            self._fh.write(f"{self._ts()} | {msg}\n")
        except Exception:                            # noqa: BLE001
            pass

    def close(self):
        try:
            self._fh.close()
        except Exception:                            # noqa: BLE001
            pass


def send_notification(title, description, color, fields=None):
    """Fan out an alert to every configured backend; no-op for any that isn't
    configured, and fail-safe. color: a notifications.COLOR_* constant, never a raw
    int (tests/test_notification_severity.py fails the build on a raw literal)."""
    notifications.notify(NOTIFY, title, description, color, fields,
                         username="Stabilize Bot")


def completion_notice(ok, stopped, reason="", failed=0):
    """(title, color) for the end-of-run notification. Pure, so it is unit-tested.
    Colours are notifications.COLOR_* constants.

    A queue (#23) adds one state #20 could not have: some videos done, some not.
    That is ORANGE rather than RED - the run delivered, and calling it "Failed"
    would send the user hunting for a disaster that is one unreadable file."""
    if stopped:
        return "Video Stabilization -- Stopped by User", notifications.COLOR_YELLOW
    if not ok:
        return "Video Stabilization -- Failed", notifications.COLOR_RED
    if failed:
        return ("Video Stabilization -- Finished with errors",
                notifications.COLOR_ORANGE)
    return "Video Stabilization -- Finished", notifications.COLOR_GREEN


# ─────────────────────────────────────────────
#  Filter construction
# ─────────────────────────────────────────────
#
# THE TRANSFORM-FILE PATH IS A TRAP, and the error message names neither the filter
# nor the path. `vidstabdetect=result=<path>` and `vidstabtransform=input=<path>`
# take a FILE PATH INSIDE A FILTER ARGUMENT, where `:` separates options and `\`
# escapes. An absolute Windows path fails with a bare
#     Error opening output files: Invalid argument
# Reproduced here, and the fix that "obviously" works does not: measured on ffmpeg
# 8.1, `C\:/Users/.../t.trf` (one escaped colon, forward slashes) STILL fails. What
# does work is a DOUBLE-escaped colon (`C\\:/Users/...`, i.e. the value survives both
# the filtergraph and the filter-option parser) or a bare relative filename with the
# child's cwd set.
#
# This module uses the SECOND, because it is immune to every other character a user's
# path can contain (spaces, quotes, brackets, commas - all of which are also special
# to the filtergraph parser) rather than just to the drive colon: the .trf is written
# into a private temp dir and every ffmpeg child runs with cwd set to that dir, so the
# filter only ever sees a bare filename we chose. The source and output paths are
# ordinary ffmpeg arguments, not filter arguments, so they need no escaping at all -
# they are simply made absolute, which is what makes the cwd switch safe.

TRF_NAME = "transforms.trf"


def detect_filter(shakiness=DEFAULT_SHAKINESS, accuracy=DEFAULT_ACCURACY,
                  trf_name=TRF_NAME, deinterlace=False):
    """Pass 1's -vf value. `trf_name` must be a BARE filename (see the note above)."""
    chain = []
    if deinterlace:
        chain.append("bwdif=mode=0")
    chain.append(f"vidstabdetect=shakiness={int(shakiness)}:"
                 f"accuracy={int(accuracy)}:result={trf_name}")
    return ",".join(chain)


def transform_filter(smoothing=DEFAULT_SMOOTHING, optzoom=DEFAULT_OPTZOOM,
                     crop=DEFAULT_CROP, trf_name=TRF_NAME, deinterlace=False):
    """Pass 2's -vf value. `trf_name` must be a BARE filename (see the note above)."""
    if crop not in CROP_MODES:
        raise ValueError(f"crop must be one of {CROP_MODES}, got {crop!r}")
    chain = []
    if deinterlace:
        chain.append("bwdif=mode=0")
    chain.append(f"vidstabtransform=input={trf_name}:"
                 f"smoothing={int(smoothing)}:optzoom={int(optzoom)}:crop={crop}")
    return ",".join(chain)


def detect_command(ffmpeg, src, vf):
    """Pass 1: measure motion, write the .trf, decode to nothing. `-an` because audio
    is irrelevant to motion detection and decoding it is wasted time."""
    return [ffmpeg, "-hide_banner", "-nostdin", "-y",
            "-i", os.path.abspath(src),
            "-vf", vf, "-an",
            "-progress", "pipe:1", "-nostats",
            "-f", "null", "-"]


def transform_command(ffmpeg, src, dest, vf, codec, enc_args, pix_fmt, audio_args):
    """Pass 2: warp each frame and write the deliverable, carrying the source's audio
    through in the SAME command rather than encoding then muxing separately - the
    filter only touches video, so a second pass over the file would buy nothing."""
    args = [ffmpeg, "-hide_banner", "-nostdin", "-y",
            "-i", os.path.abspath(src),
            "-vf", vf,
            "-map", "0:v:0"]
    if audio_args:
        # `0:a:0?` - optional, so a source whose audio stream ffprobe saw but that the
        # muxer then refuses cannot fail the whole run at the last step.
        args += ["-map", "0:a:0?"] + list(audio_args)
    else:
        args += ["-an"]
    args += ["-c:v", codec, *enc_args, "-pix_fmt", pix_fmt,
             "-progress", "pipe:1", "-nostats", os.path.abspath(dest)]
    return args


_PROGRESS_FRAME_RE = re.compile(r"^frame=(\d+)", re.M)


def parse_progress_frame(text):
    """Latest frame number from an `-progress pipe:1` chunk, or None. That stream is
    line-based `key=value` (unlike the \\r-updated -stats line), so this stays a plain
    regex over whatever arrived."""
    matches = _PROGRESS_FRAME_RE.findall(text or "")
    if not matches:
        return None
    try:
        return int(matches[-1])
    except ValueError:
        return None


def canonical_output_path(src, folder=None, suffix=STAB_SUFFIX, ext=STAB_EXT):
    """`<stem>_stabilized.mp4`, in `folder` (default: beside the source), with NO
    uniquifying suffix. This is the name a result is looked for under, so it must be
    derivable from the source alone - that is what makes "already stabilised"
    answerable without a database, on a tree produced by another install."""
    src = os.path.abspath(src)
    folder = os.path.abspath(folder) if folder else os.path.dirname(src)
    stem = os.path.splitext(os.path.basename(src))[0]
    return os.path.join(folder, f"{stem}{suffix}{ext}")


def suggest_output_path(src, suffix=STAB_SUFFIX, ext=STAB_EXT, folder=None,
                        taken=None):
    """A free output path for `src`: the canonical name, or `_2`, `_3` … until one is
    free. Never returns the source path itself, and never a file that already exists
    - a stabilise that silently ate its own input, or someone else's output, is not
    recoverable.

    `taken` is a set of normcase'd paths already CLAIMED by other jobs in the same
    queue. Existence on disk is not enough there: two sources with the same stem in
    different folders both want one name in a shared output folder, and neither of
    their outputs exists yet when the queue is planned, so without this the second
    would overwrite the first at the moment it finished."""
    src = os.path.abspath(src)
    claimed = taken if taken is not None else set()
    candidate = canonical_output_path(src, folder, suffix, ext)
    n = 2
    while (os.path.exists(candidate)
           or os.path.normcase(candidate) in claimed
           or os.path.normcase(candidate) == os.path.normcase(src)):
        base = canonical_output_path(src, folder, suffix, ext)
        stem, e = os.path.splitext(base)
        candidate = f"{stem}_{n}{e}"
        n += 1
    return candidate


def is_stabilized_name(path):
    """Does this filename look like one of OUR results? Used to keep a folder scan
    from offering its own previous outputs back as fresh input - which the derived-
    directory rule (#16) cannot catch, because the default output sits beside the
    source rather than in a folder of ours.

    Matches the plain suffix and the uniquified `_stabilized_2` form."""
    stem = os.path.splitext(os.path.basename(path))[0]
    if stem.lower().endswith(STAB_SUFFIX):
        return True
    head, _, tail = stem.rpartition("_")
    return bool(tail.isdigit() and head.lower().endswith(STAB_SUFFIX))


def iter_videos(root, recursive=True, cfg=None):
    """Every video under `root`, sorted, skipping the app's own derived directories
    (#16) and our own previous results. Returns (paths, pruner) so the caller can
    print the one-line prune summary the other walkers print."""
    pruner = runner_common.DerivedPruner(cfg)
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        pruner.prune(dirnames)
        if not recursive:
            dirnames[:] = []
        for name in filenames:
            if os.path.splitext(name)[1].lower() not in VIDEO_EXTS:
                continue
            if is_stabilized_name(name):
                continue
            found.append(os.path.join(dirpath, name))
    found.sort(key=lambda p: p.lower())
    return found, pruner


def plan_jobs(sources, outdir=None, redo=False):
    """Turn a list of source videos into (jobs, skipped).

    `jobs` are {"source", "output"} dicts with collision-free outputs; `skipped` are
    {"source", "output"} for sources whose canonical result is ALREADY THERE. That
    split is the whole resume story: re-running a folder does the files that are
    missing a result and reports the rest, exactly as the Batch Upscaler reports
    "already upscaled".

    `redo=True` stabilises them anyway, into `_2`, `_3` … - never over the previous
    result, since the user may still be judging it."""
    jobs, skipped, taken = [], [], set()
    for src in sources:
        src = os.path.abspath(src)
        canonical = canonical_output_path(src, outdir)
        if os.path.exists(canonical) and not redo:
            skipped.append({"source": src, "output": canonical})
            continue
        out = suggest_output_path(src, folder=outdir, taken=taken)
        taken.add(os.path.normcase(out))
        jobs.append({"source": src, "output": out})
    return jobs, skipped


# ─────────────────────────────────────────────
#  Capability + health of the local ffmpeg
# ─────────────────────────────────────────────

def vidstab_available(ffmpeg=None):
    """Does this ffmpeg expose BOTH vidstab filters? The bundled builds do, but a user
    with a hand-installed ffmpeg on PATH may not, and the failure without this check is
    a cryptic filtergraph parse error."""
    if ffmpeg is None:
        ffmpeg, _ = vp.find_ffmpeg()
    try:
        out = vp._run([ffmpeg, "-hide_banner", "-filters"],
                      check=False, hard_timeout=60).stdout or ""
    except Exception:                                # noqa: BLE001
        return False
    return "vidstabdetect" in out and "vidstabtransform" in out


def ffmpeg_version_line(ffmpeg=None):
    """First line of `ffmpeg -version`, for the log and for error messages. The point
    of naming it is that the vidstab bug is a property of the BUILD, so the user (or a
    bug report) needs to know which one produced the refusal."""
    if ffmpeg is None:
        ffmpeg, _ = vp.find_ffmpeg()
    try:
        out = vp._run([ffmpeg, "-hide_banner", "-version"],
                      check=False, hard_timeout=60).stdout or ""
        return (out.strip().splitlines() or [""])[0].strip()
    except Exception:                                # noqa: BLE001
        return ""


def vidstab_health(ffmpeg=None, frames=HEALTH_FRAMES, size=HEALTH_SIZE, log=None):
    """Prove this ffmpeg's vidstabtransform is sound before a real video is processed.

    Returns (ok, detail). The test is BEHAVIOURAL, not a version-string comparison,
    because the property that matters is "does this build corrupt memory", and that is
    a fact about the binary a user actually has - which may be a hand-installed one,
    an offline-fallback release build, or a future release that has fixed it. A
    version table would need editing every time either changes.

    How it detects the fault: the bug makes vidstabtransform read memory it does not
    own, so its OUTPUT VARIES BETWEEN IDENTICAL RUNS. Measured on the broken n8.1.2,
    11 runs of one command produced 11 different framemd5 files; a sound build is
    bit-identical every time (verified 20/20 on the pinned master build and 8/8 on an
    ffmpeg 6.x). So: synthesise a shaky clip, detect once, then transform TWICE and
    compare the frame hashes. Differing hashes - or a crash - means broken.

    Non-determinism is the signal rather than the crash because it is far more
    sensitive: on a 300-frame 720p clip the broken build crashed 0 times out of 12 yet
    still produced 12 different outputs, so a crash-only check would have passed a
    build that silently corrupts every frame it touches.
    """
    if ffmpeg is None:
        ffmpeg, _ = vp.find_ffmpeg()
    work = tempfile.mkdtemp(prefix="imgtbx_stabcheck_")
    try:
        src = os.path.join(work, "sample.mp4")
        # Synthetic, but with real motion to measure: a still clip yields near-zero
        # transforms, and the in-place/separate-buffer alternation that triggers the
        # bug needs the filter to actually be doing work.
        gen = [ffmpeg, "-hide_banner", "-nostdin", "-y", "-f", "lavfi",
               "-i", f"testsrc2=size={size}:rate=25:duration={frames / 25.0:.2f}",
               "-vf", "rotate=0.012*sin(n/4)+0.006*sin(n/11):fillcolor=black,"
                      "crop=iw-16:ih-16",
               "-c:v", "libx264", "-pix_fmt", "yuv420p", src]
        cp = vp._run(gen, check=False, hard_timeout=120)
        if cp.returncode != 0 or not os.path.exists(src):
            # Cannot build a sample: do not claim the build is broken on that basis.
            return True, "health check skipped (could not synthesise a test clip)"

        cp = vp._run([ffmpeg, "-hide_banner", "-nostdin", "-y", "-i", src,
                      "-vf", detect_filter(trf_name=TRF_NAME), "-an", "-f", "null", "-"],
                     check=False, hard_timeout=120, cwd=work)
        if cp.returncode != 0 or not os.path.exists(os.path.join(work, TRF_NAME)):
            return False, "vidstabdetect failed on a synthetic test clip"

        digests = []
        for i in range(2):
            out = os.path.join(work, f"h{i}.txt")
            cp = vp._run([ffmpeg, "-hide_banner", "-nostdin", "-y", "-i", src,
                          "-vf", transform_filter(trf_name=TRF_NAME),
                          "-an", "-f", "framemd5", out],
                         check=False, hard_timeout=120, cwd=work)
            if cp.returncode != 0 or not os.path.exists(out):
                return False, (f"vidstabtransform crashed on a synthetic test clip "
                               f"(exit {cp.returncode})")
            with open(out, "rb") as fh:
                digests.append(hashlib.sha256(fh.read()).hexdigest())
        if digests[0] != digests[1]:
            return False, ("vidstabtransform produced different output from two "
                           "identical runs (it is reading uninitialised memory)")
        return True, "vidstabtransform is deterministic"
    except Exception as exc:                         # noqa: BLE001
        if log:
            log.log_only(f"health check error: {exc}")
        return True, f"health check skipped ({exc})"
    finally:
        shutil.rmtree(work, ignore_errors=True)


BROKEN_VIDSTAB_HELP = (
    "This ffmpeg build cannot stabilise video correctly.\n\n"
    "Every ffmpeg 8.1.x release corrupts memory inside the stabilisation filter, so "
    "it silently produces a different (and wrong) result every time it runs. It was "
    "fixed upstream on 2026-04-01, in the 8.2 development line.\n\n"
    "The app normally bundles a build that has the fix. To get it, close the app and "
    "run 'Image Toolbox.cmd' again - the bootstrapper replaces an older bundled "
    "ffmpeg. If you pointed the app at your own ffmpeg, update it to one built from "
    "master (or 8.2 or newer).\n\n"
    "Nothing was written; your source file is untouched.")


# ─────────────────────────────────────────────
#  Running a pass
# ─────────────────────────────────────────────

_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

_quit_evt = threading.Event()


def _watch_stdin():
    """GUI control: 'q' stops the run after killing the current ffmpeg."""
    try:
        for line in sys.stdin:
            if line.strip().lower() == "q":
                _quit_evt.set()
                break
    except Exception:                                # noqa: BLE001
        pass


def run_pass(args, cwd, total_frames, on_progress=None, stall_timeout=None):
    """Run one ffmpeg pass, streaming `-progress pipe:1` so the caller can report
    frames done. Returns the last frame number seen.

    Killed promptly when the user stops: a stabilise is not resumable (a single file
    finishes in well under its own duration), so Stop simply discards. `stdin` is
    DEVNULL because this runner is launched by the GUI with a control stdin PIPE, and
    a child ffmpeg inherits it, reads it for interactive keys, and can swallow the
    runner's own stop byte."""
    proc = subprocess.Popen(
        args, cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        creationflags=_CREATE_NO_WINDOW)
    last_frame = [0]
    err_tail = []
    last_out = [time.monotonic()]

    def _drain_err():
        try:
            while True:
                chunk = proc.stderr.read1(65536)
                if not chunk:
                    break
                err_tail.append(chunk.decode("utf-8", "replace"))
                del err_tail[:-8]
                last_out[0] = time.monotonic()
        except Exception:                            # noqa: BLE001
            pass

    t = threading.Thread(target=_drain_err, daemon=True)
    t.start()
    stalled = False
    try:
        while True:
            chunk = proc.stdout.read1(65536)
            if not chunk:
                break
            last_out[0] = time.monotonic()
            frame = parse_progress_frame(chunk.decode("utf-8", "replace"))
            if frame is not None:
                last_frame[0] = frame
                if on_progress:
                    on_progress(frame, total_frames)
            if _quit_evt.is_set():
                break
            if stall_timeout and time.monotonic() - last_out[0] > stall_timeout:
                stalled = True
                break
    finally:
        # Kill on either exit-without-EOF path (user stop, or a wedged child that went
        # silent); otherwise the child has already finished and wait() just reaps it.
        if _quit_evt.is_set() or stalled:
            try:
                proc.kill()
            except Exception:                        # noqa: BLE001
                pass
        code = proc.wait()
        t.join(timeout=2.0)
    if _quit_evt.is_set():
        raise KeyboardInterrupt("stopped by user")
    if stalled:
        raise StabilizeError(
            f"ffmpeg stopped producing output for {stall_timeout:.0f}s and was killed; "
            "nothing was written.")
    if code != 0:
        tail = "".join(err_tail).strip().splitlines()[-8:]
        raise StabilizeError(f"ffmpeg failed (exit {code}):\n" + "\n".join(tail))
    return last_frame[0]


# ─────────────────────────────────────────────
#  The stabilisation
# ─────────────────────────────────────────────

def preflight(log, skip_health=False):
    """Prove this ffmpeg can stabilise correctly, ONCE per run. Raises StabilizeError
    with a user-facing explanation if it cannot.

    Hoisted out of `stabilize` for the queue (#23 item 3): the health check costs
    about half a second, which is nothing for one video and fifty times nothing for
    fifty. It is also the right shape for a refusal - a broken build is a property of
    the RUN, so it must stop the whole queue before any file is touched, not fail
    each of fifty files in turn with the same paragraph."""
    ffmpeg, _ffprobe = vp.find_ffmpeg()
    version = ffmpeg_version_line(ffmpeg)
    log.tee(f"  ffmpeg: {version}")
    if not vidstab_available(ffmpeg):
        raise StabilizeError(
            "This ffmpeg build has no vidstab filters, so it cannot stabilise "
            f"video.\n({version})")

    _gui_event("STATUS", "Checking the stabilisation filter …")
    if skip_health:
        log.tee("  Health check skipped (--skip-health).")
    else:
        ok, detail = vidstab_health(ffmpeg, log=log)
        log.tee(f"  Filter health: {detail}")
        if not ok:
            raise StabilizeError(BROKEN_VIDSTAB_HELP + f"\n\n({version})")
    return ffmpeg


def stabilize(src, dest, log, smoothing=DEFAULT_SMOOTHING,
              shakiness=DEFAULT_SHAKINESS, accuracy=DEFAULT_ACCURACY,
              optzoom=DEFAULT_OPTZOOM, crop=DEFAULT_CROP, skip_health=False,
              ffmpeg=None, progress=None):
    """Stabilise `src` into `dest`. Returns a summary dict. Raises StabilizeError on a
    refusal or an ffmpeg failure, KeyboardInterrupt when the user stopped it.

    `ffmpeg` skips the preflight (the caller already ran it, see `preflight`);
    `progress` is the queue-wide QueueProgress that turns this file's frame counts
    into a bar for the whole run."""
    src = os.path.abspath(src)
    dest = os.path.abspath(dest)
    if os.path.normcase(src) == os.path.normcase(dest):
        raise StabilizeError("The output file must be different from the source.")
    if not os.path.isfile(src):
        raise StabilizeError(f"Source video not found: {src}")

    if ffmpeg is None:
        ffmpeg = preflight(log, skip_health)
    if progress is None:
        progress = QueueProgress()

    info = vp.probe(src)
    total = info.nb_frames or 0
    log.tee(f"  Source: {info.width}x{info.height}, {float(info.fps):.3f} fps, "
            f"{fmt_duration(info.duration)}, {total or '?'} frames")

    # An interlaced source must be deinterlaced BEFORE the motion is measured, not
    # after: vidstabdetect would otherwise measure the motion of interleaved fields
    # (two different instants woven into one frame) and pass 2 would warp comb
    # artefacts around. Same detector and same filter the Video Upscaler already
    # applies to a MiniDV 576i source for the equivalent reason.
    deinterlace = vp.detect_interlaced(info)
    if deinterlace:
        log.tee("  Interlaced source detected - deinterlacing (bwdif) in both passes.")

    work = tempfile.mkdtemp(prefix="imgtbx_stab_")
    started = time.time()
    progress.begin_file(total)
    tmp_dest = dest + ".part" + (os.path.splitext(dest)[1] or ".mp4")
    try:
        # ── Pass 1: measure ────────────────────────────────────────────────
        _gui_event("PASS", json.dumps({"pass": 1, "of": 2, "name": "Measuring motion"}))
        _gui_event("STATUS", "Pass 1 of 2 - measuring camera motion …")
        log.tee("  Pass 1/2: measuring camera motion …")
        vf1 = detect_filter(shakiness, accuracy, TRF_NAME, deinterlace)
        log.log_only(f"    -vf {vf1}")
        counted = run_pass(
            detect_command(ffmpeg, src, vf1), work, total,
            on_progress=lambda f, _t: progress.report(1, f),
            stall_timeout=vp._ENCODE_STALL_S)
        # Pass 1 has now counted the frames exactly; the header count that seeded the
        # first bar can be an estimate, so pass 2 gets the measured number.
        if counted > 0:
            total = counted
            progress.correct_file(counted)
        trf = os.path.join(work, TRF_NAME)
        if not os.path.exists(trf) or os.path.getsize(trf) == 0:
            raise StabilizeError("Pass 1 produced no motion data; nothing was written.")
        log.tee(f"    measured {counted} frames ({os.path.getsize(trf) / 1024:.0f} KB "
                f"of motion data)")

        # ── Pass 2: warp + encode ──────────────────────────────────────────
        _gui_event("PASS", json.dumps({"pass": 2, "of": 2, "name": "Stabilising"}))
        _gui_event("STATUS", "Pass 2 of 2 - stabilising and encoding …")
        codec, enc_args, is_hw = vp.pick_encoder()
        # This output is the DELIVERABLE (it is what the user keeps, and what they may
        # feed to the Video Upscaler next), not the split pipeline's throwaway
        # intermediate, so it follows the same 10-bit-where-safe rule as the
        # Real-ESRGAN engine's segments rather than pick_encoder's 8-bit default.
        pix_fmt = vp.delivery_pix_fmt(codec)
        audio_args = vp._audio_copy_args(info, dest, log=log.tee) if info.has_audio else []
        log.tee(f"  Pass 2/2: stabilising ({codec}{' (GPU)' if is_hw else ''}, {pix_fmt})…")
        vf2 = transform_filter(smoothing, optzoom, crop, TRF_NAME, deinterlace)
        log.log_only(f"    -vf {vf2}")
        run_pass(
            transform_command(ffmpeg, src, tmp_dest, vf2, codec, enc_args,
                              pix_fmt, audio_args),
            work, total,
            on_progress=lambda f, _t: progress.report(2, f),
            stall_timeout=vp._ENCODE_STALL_S)

        # Write to a `.part` sibling and rename on success, so a killed or failed pass 2
        # never leaves a TRUNCATED file sitting at the name the user chose, looking like
        # a finished stabilise.
        os.replace(tmp_dest, dest)
        elapsed = time.time() - started

        out_info = None
        try:
            out_info = vp.probe(dest)
        except Exception:                            # noqa: BLE001
            pass
        # Duration drift is the one silent way a two-pass filter run goes wrong, and it
        # is cheap to check; report it rather than fail, since the file is valid either
        # way and the user can judge.
        if out_info is not None and info.duration and out_info.duration:
            delta = abs(out_info.duration - info.duration)
            if delta > max(vp.DRIFT_SECONDS_TOL, 1.0 / max(float(info.fps), 1.0)):
                log.tee(f"  ! Duration drift: source {info.duration:.3f}s vs "
                        f"output {out_info.duration:.3f}s")
        log.tee(f"  Done in {fmt_duration(elapsed)} -> {dest}")
        progress.end_file()
        record_pair(src, dest, smoothing, optzoom, total, log)
        return {"tool": "stabilize", "source": src, "output": dest,
                "processed": total, "failed": 0, "frames": total,
                "elapsed_seconds": round(elapsed, 1),
                "smoothing": smoothing, "optzoom": optzoom, "crop": crop,
                "deinterlaced": deinterlace,
                "size_bytes": os.path.getsize(dest) if os.path.exists(dest) else 0}
    finally:
        for junk in (tmp_dest,):
            if os.path.exists(junk):
                try:
                    os.remove(junk)
                except OSError:
                    pass
        shutil.rmtree(work, ignore_errors=True)


class QueueProgress:
    """One progress bar and one ETA for the WHOLE run, however many files it holds.

    Everything is counted in PASS-FRAMES: one frame of one pass. A file of N frames
    contributes 2N, because both passes read every frame. Two consequences, both
    deliberate:

      * Progress is reported in WHOLE-JOB units, not per-pass ones. Two passes of
        comparable length, so pass 1 fills the first half of a file's share and
        pass 2 the second. A per-pass bar would reach 100% halfway through and then
        start again, which is worse than no bar. Pass 2 is the slower of the two
        (measured 2.4x realtime against 3.8x), so the estimate runs a little
        optimistic during pass 1 and corrects itself as pass 2 gets going.

      * A QUEUE gets a real bar rather than a file counter, because the budget is
        known up front: `main` probes every source before starting (which is also
        where an unreadable file is caught, before any GPU-free hour is spent on
        the rest). The doc asked for a `[i/N]` file index alongside the bar; that
        rides on the FILE event instead, so the bar can stay proportional - fifty
        files of wildly different lengths make `[i/N]` a poor progress signal.

    A header frame count can be an estimate, so `correct_file` folds pass 1's exact
    count back into the queue budget when it differs.
    """

    def __init__(self, total_frames=0):
        self.budget   = max(0, int(total_frames)) * 2   # pass-frames for the queue
        self.done     = 0                               # completed by FINISHED files
        self.cur_pass_frames = 0                        # this file's 2N
        self.started  = time.time()
        self._last    = 0.0

    # ── queue bookkeeping ────────────────────────────────────────────────────

    def begin_file(self, frames):
        self.cur_pass_frames = max(0, int(frames)) * 2
        if not self.budget:
            # No up-front probe (a single headless file): the budget IS this file.
            self.budget = self.cur_pass_frames

    def correct_file(self, counted):
        """Pass 1 measured the frames exactly; shift the queue budget by the delta."""
        exact = max(0, int(counted)) * 2
        self.budget = max(0, self.budget - self.cur_pass_frames + exact)
        self.cur_pass_frames = exact

    def end_file(self):
        self.done += self.cur_pass_frames
        self.cur_pass_frames = 0

    # ── reporting ────────────────────────────────────────────────────────────

    def report(self, which, frame, force=False):
        """Emit PROG + ETA for the whole run. Throttled: ffmpeg emits a -progress
        block per frame, and forwarding thousands of marker lines would flood the
        GUI's pipe."""
        now = time.time()
        if not force and now - self._last < 0.25:
            return
        self._last = now
        if self.budget <= 0:
            return
        in_file = int(frame) + (self.cur_pass_frames // 2 if which == 2 else 0)
        done = min(self.done + max(0, in_file), self.budget)
        _gui_event("PROG", f"{done}|{self.budget}")
        elapsed = now - self.started
        _gui_event("ETA", f"{elapsed:.1f}|{done}|{done}|{self.budget}")


# ─────────────────────────────────────────────
#  The pair record  (#23 item 5)
# ─────────────────────────────────────────────
#
# WHAT THIS IS NOT: a lineage row. `db.lineage` is what Conciliation matches on,
# and video conciliation is lineage-ONLY (#5 deliberately gave it no name
# fallback), so recording a stabilised output there would make the app's one
# destructive tool offer to archive or DELETE the original and move the stabilised
# copy into its place. That is #20's own stated failure mode ("silent and
# permanent") applied to a whole collection, for a transformation that is opt-in
# per video precisely because it is not arguably an improvement the way an upscale
# is. So the pair goes in its own table (db.stab_pairs) that no conciliation query
# reads - see the schema comment there for why a discriminator column was refused.

def record_pair(src, dest, smoothing, optzoom, frames, log=None):
    """Remember source -> result so the pair can still be compared in a later
    session. Fail-safe and entirely optional: a run that cannot reach the cache is a
    successful run with one convenience missing, never a failed one."""
    try:
        import db
        db.record_stabilized(db.get_conn(), src, dest, smoothing=smoothing,
                             optzoom=optzoom, frames=frames)
    except Exception as exc:                         # noqa: BLE001
        if log:
            log.log_only(f"    (pair not recorded: {exc})")


# ─────────────────────────────────────────────
#  The queue  (#23 items 2+3)
# ─────────────────────────────────────────────

def probe_queue(jobs, log):
    """Measure the whole queue before starting it, returning (total_frames, bad).

    Two things this buys, and both are why it is worth an ffprobe per file up front:
    the run gets ONE proportional progress bar instead of a file counter (see
    QueueProgress), and an unreadable file is reported NOW rather than after the
    forty-nine good ones have been processed around it. A file whose header carries
    no frame count is not "bad" - it simply contributes an estimate from its
    duration, and pass 1 corrects the budget when it gets there."""
    total, bad = 0, []
    for job in jobs:
        try:
            info = vp.probe(job["source"])
        except Exception as exc:                     # noqa: BLE001
            bad.append((job, f"{type(exc).__name__}: {exc}"))
            continue
        frames = info.nb_frames or 0
        if not frames and info.duration and info.fps:
            frames = int(float(info.duration) * float(info.fps))
        job["frames"] = frames
        total += frames
    if bad:
        for job, why in bad:
            log.tee(f"  ! Cannot read {os.path.basename(job['source'])}: {why}")
    return total, bad


def run_queue(jobs, log, opts, ffmpeg):
    """Stabilise every job in order. Returns a summary dict.

    A single file's failure does NOT end the queue: it is logged, reported as a
    RESULT, and the next file starts. A user Stop does, and so does a refusal
    (which `preflight` has already ruled out before this is called). Files never
    started stay untouched, and the next run picks them up because their result is
    missing - which is the whole of the resume story (see the module docstring)."""
    total_frames, bad = probe_queue(jobs, log)
    unreadable = {id(j) for j, _why in bad}
    runnable = [j for j in jobs if id(j) not in unreadable]
    progress = QueueProgress(total_frames)
    results, failures = [], []
    stopped = False

    for job, why in bad:
        failures.append({"source": job["source"], "reason": why})
        _gui_event("RESULT", json.dumps({"source": job["source"], "ok": False,
                                         "reason": why}))

    n = len(runnable)
    for i, job in enumerate(runnable, 1):
        src, dest = job["source"], job["output"]
        _gui_event("FILE", json.dumps({"index": i, "total": n, "source": src,
                                       "output": dest,
                                       "name": os.path.basename(src)}))
        prefix = f"[{i}/{n}] " if n > 1 else ""
        log.tee(f"{prefix}{src}")
        log.tee(f"  Output:  {dest}")
        try:
            res = stabilize(src, dest, log,
                            smoothing=opts["smoothing"], shakiness=opts["shakiness"],
                            accuracy=opts["accuracy"], optzoom=opts["optzoom"],
                            crop=opts["crop"], ffmpeg=ffmpeg, progress=progress)
        except KeyboardInterrupt:
            stopped = True
            log.tee("  Stopped by user - nothing was written for this video.")
            break
        except Exception as exc:                     # noqa: BLE001
            # A StabilizeError already carries a user-facing sentence; anything else
            # is a bug or an environment failure and gets its type named.
            why = str(exc) if isinstance(exc, StabilizeError) \
                else f"{type(exc).__name__}: {exc}"
            log.tee(f"  Failed: {why.splitlines()[0]}")
            failures.append({"source": src, "reason": why})
            _gui_event("RESULT", json.dumps({"source": src, "output": dest,
                                             "ok": False,
                                             "reason": why.splitlines()[0]}))
            # The budget still has to move on, or the bar would stall at whatever
            # fraction the failed file reached and every later ETA would be wrong.
            progress.end_file()
            continue
        results.append(res)
        _gui_event("RESULT", json.dumps({"source": src, "output": dest, "ok": True,
                                         "frames": res.get("frames", 0),
                                         "size_bytes": res.get("size_bytes", 0)}))

    elapsed = time.time() - progress.started
    # A single-video run keeps every per-file key #20 published (source, output,
    # frames, smoothing, optzoom, crop, deinterlaced, size_bytes), so an existing
    # Home Assistant template still reads it. The QUEUE's counts are layered ON TOP,
    # deliberately: the shared `processed` key means "items finished", and a video's
    # own summary counts FRAMES under that name - merged the other way round, a
    # 50-frame clip reported "50 processed" for one video.
    summary = dict(results[0]) if (len(results) == 1 and len(jobs) == 1) else {}
    summary.update({"tool": "stabilize", "queued": len(jobs),
                    "processed": len(results), "failed": len(failures),
                    "stopped_by_user": stopped,
                    "results": results, "failures": failures,
                    "elapsed_seconds": round(elapsed, 1)})
    return summary


def load_queue_file(path):
    """Read the GUI's queue: a JSON list of {"source", "output"} objects (or an
    object with a "jobs" key). The GUI hands the queue over in a FILE rather than on
    the command line because a folder of a few hundred videos would blow past
    Windows' command-line length limit, and because the GUI is the authority on each
    output name (it has shown them to the user in the list already)."""
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    jobs = data.get("jobs", []) if isinstance(data, dict) else data
    out = []
    for j in jobs:
        src = os.path.abspath(j["source"])
        dest = j.get("output") or suggest_output_path(src)
        out.append({"source": src, "output": os.path.abspath(dest)})
    return out


# ─────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────

def build_parser():
    p = argparse.ArgumentParser(
        description="Stabilise shaky video into new files (future-features #20/#23).")
    p.add_argument("source", nargs="?", default=None,
                   help="the video to stabilise, or a FOLDER of videos "
                        "(never modified)")
    p.add_argument("output", nargs="?", default=None,
                   help="output file (default: <stem>_stabilized.mp4 beside the source)")
    p.add_argument("--queue", default=None,
                   help="JSON file of {source, output} jobs (the GUI's path)")
    p.add_argument("--outdir", default=None,
                   help="write every result into this folder instead of beside "
                        "each source (recommended for a folder run)")
    p.add_argument("--no-recursive", action="store_true",
                   help="with a folder source, do not descend into subfolders")
    p.add_argument("--redo", action="store_true",
                   help="stabilise videos that already have a result, into a new "
                        "file (never over the existing one)")
    p.add_argument("--smoothing", type=int, default=DEFAULT_SMOOTHING,
                   help=f"how hard to smooth the camera path (default {DEFAULT_SMOOTHING})")
    p.add_argument("--shakiness", type=int, default=DEFAULT_SHAKINESS,
                   help=f"pass-1 shakiness, 1-10 (default {DEFAULT_SHAKINESS})")
    p.add_argument("--accuracy", type=int, default=DEFAULT_ACCURACY,
                   help=f"pass-1 accuracy, 1-15 (default {DEFAULT_ACCURACY})")
    p.add_argument("--optzoom", type=int, default=DEFAULT_OPTZOOM, choices=(0, 1),
                   help="1 zooms in to hide the borders and LOSES ~10-13%% of the "
                        "picture; 0 (default) keeps the whole frame")
    p.add_argument("--crop", default=DEFAULT_CROP, choices=CROP_MODES,
                   help=f"border fill when optzoom=0 (default {DEFAULT_CROP})")
    p.add_argument("--skip-health", action="store_true",
                   help="skip the vidstab correctness self-test (not recommended)")
    p.add_argument("--check", action="store_true",
                   help="only run the ffmpeg capability + health check, then exit")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)

    if args.check:
        ffmpeg, _ = vp.find_ffmpeg()
        print(ffmpeg_version_line(ffmpeg))
        if not vidstab_available(ffmpeg):
            print("vidstab: NOT AVAILABLE in this build")
            return 2
        ok, detail = vidstab_health(ffmpeg)
        print(f"vidstab: {'OK' if ok else 'BROKEN'} - {detail}")
        return 0 if ok else 3

    jobs, skipped, key, header = _jobs_from_args(args)
    if jobs is None:                                 # nothing to do; message printed
        return 2

    log = Logger(key, header)
    _gui_event("LOG", log.path)
    log.tee(header)
    for job in skipped:
        log.tee(f"  Already stabilised, skipped: {os.path.basename(job['source'])} "
                f"-> {os.path.basename(job['output'])}")
    if skipped:
        log.tee(f"  {len(skipped)} video(s) already have a result and were skipped "
                f"(use --redo to stabilise them again).")
    if not jobs:
        _gui_event("STATUS", "Nothing to do - every video already has a result.")
        _gui_event("DONE", json.dumps({"tool": "stabilize", "queued": 0,
                                       "processed": 0, "failed": 0,
                                       "skipped": len(skipped),
                                       "elapsed_seconds": 0}))
        log.close()
        return 0
    log.tee(f"  Settings: smoothing={args.smoothing}, optzoom={args.optzoom}, "
            f"crop={args.crop}, shakiness={args.shakiness}, accuracy={args.accuracy}")
    if args.optzoom:
        log.tee("  ! optzoom=1 zooms in to hide the borders: this LOSES picture at "
                "every edge.")

    if GUI_MODE:
        threading.Thread(target=_watch_stdin, daemon=True).start()

    opts = {"smoothing": args.smoothing, "shakiness": args.shakiness,
            "accuracy": args.accuracy, "optzoom": args.optzoom, "crop": args.crop}
    summary = None
    error = ""
    try:
        # ONE preflight for the whole run: a broken vidstab is a property of the
        # build, so it must refuse before any file is touched rather than fail each
        # of fifty files with the same paragraph.
        ffmpeg = preflight(log, args.skip_health)
        summary = run_queue(jobs, log, opts, ffmpeg)
    except StabilizeError as exc:
        error = str(exc)
        log.tee(f"  Refused: {error}")
        _gui_event("STATUS", error.splitlines()[0] if error else "Failed.")
        _gui_event("REFUSED", json.dumps({"reason": error}))
    except KeyboardInterrupt:
        error = "Stopped by user."
        log.tee("  Stopped by user - nothing was written.")
        _gui_event("STATUS", "Stopped - nothing was written.")
    except Exception as exc:                         # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
        log.tee(f"  Failed: {error}")
        _gui_event("STATUS", f"Failed - {error}")

    if summary is None:                              # the run never started
        summary = {"tool": "stabilize", "queued": len(jobs), "processed": 0,
                   "failed": 0 if error.startswith("Stopped") else 1,
                   "stopped_by_user": error.startswith("Stopped"),
                   "stop_reason": error.splitlines()[0] if error else "",
                   "elapsed_seconds": 0}
    summary["skipped"] = len(skipped)

    done   = summary.get("processed", 0)
    failed = summary.get("failed", 0)
    _report_completion(summary, done, failed, error, log)
    log.close()
    return 0 if done and not failed else (0 if done else 1)


def _jobs_from_args(args):
    """(jobs, skipped, log_key, header) from the command line, or (None, …) when
    there is nothing to run. Three shapes reach here: the GUI's --queue file, a
    FOLDER source, and #20's original single file + optional output."""
    if args.queue:
        try:
            jobs = load_queue_file(args.queue)
        except Exception as exc:                     # noqa: BLE001
            print(f"Could not read the queue file: {exc}")
            return None, [], "", ""
        key = jobs[0]["source"] if jobs else args.queue
        return jobs, [], key, f"Stabilising {len(jobs)} video(s)"

    if not args.source:
        print("Nothing to stabilise: give a video, a folder, or --queue.")
        return None, [], "", ""

    src = os.path.abspath(args.source)
    if os.path.isdir(src):
        found, pruner = iter_videos(src, recursive=not args.no_recursive, cfg=_CFG)
        line = pruner.summary()
        if line:
            print(f"  {line}")
        jobs, skipped = plan_jobs(found, args.outdir, redo=args.redo)
        return (jobs, skipped, src,
                f"Stabilising {len(jobs)} video(s) under: {src}")

    # Single file - #20's shape, unchanged. An explicit output wins; otherwise the
    # canonical name in --outdir (or beside the source), uniquified rather than
    # overwritten, because a re-run of one chosen file is a deliberate second
    # attempt whose predecessor the user may still be judging.
    dest = (os.path.abspath(args.output) if args.output
            else suggest_output_path(src, folder=args.outdir))
    return ([{"source": src, "output": dest}], [], src, f"Stabilising: {src}")


def _report_completion(summary, done, failed, error, log):
    """The end-of-run GUI event + notification, for one file or fifty."""
    stopped = bool(summary.get("stopped_by_user"))
    title, color = completion_notice(bool(done), stopped, error, failed=failed)
    # ONE end-of-run line in the log, written before the GUI event and before any
    # notification is attempted. Nothing else here writes to the log, so without it a
    # run that finished and a run that died mid-loop end their log file identically -
    # which is exactly the ambiguity that made a stuck run undiagnosable on 2026-08-19
    # (docs/known-defects.md D3). Its PRESENCE now means the loop completed.
    bits = [f"{done} stabilised"]
    if failed:                       bits.append(f"{failed} failed")
    if summary.get("skipped"):       bits.append(f"{summary['skipped']} already done")
    log.tee(f"  Run ended: {', '.join(bits)} in "
            f"{fmt_duration(summary.get('elapsed_seconds', 0))}"
            + (" (stopped by user)" if stopped else ""))
    _gui_event("DONE", json.dumps(summary))

    results = summary.get("results") or []
    one = results[0] if len(results) == 1 and summary.get("queued") == 1 else None
    if one:
        _gui_event("STATUS", f"Done - {os.path.basename(one['output'])}")
        send_notification(
            title, f"Stabilised **{os.path.basename(one['source'])}**", color,
            fields=[{"name": "Output", "value": os.path.basename(one["output"])},
                    {"name": "Frames", "value": str(one.get("frames", "?"))},
                    {"name": "Time",
                     "value": fmt_duration(one.get("elapsed_seconds", 0))}])
        return

    if not done and not failed and not stopped:
        _gui_event("STATUS", error.splitlines()[0] if error else "Nothing was done.")
        send_notification(title, "Could not stabilise anything.", color,
                          fields=[{"name": "Reason",
                                   "value": (error.splitlines() or ["unknown"])[0]}])
        return

    bits = [f"{done} stabilised"]
    if failed:
        bits.append(f"{failed} failed")
    if summary.get("skipped"):
        bits.append(f"{summary['skipped']} already done")
    line = ", ".join(bits)
    _gui_event("STATUS", ("Stopped - " if stopped else "Done - ") + line)
    fields = [{"name": "Videos", "value": line},
              {"name": "Time",
               "value": fmt_duration(summary.get("elapsed_seconds", 0))}]
    for f in (summary.get("failures") or [])[:5]:
        fields.append({"name": os.path.basename(f["source"]),
                       "value": (f["reason"].splitlines() or [""])[0][:200]})
    send_notification(title, line, color, fields=fields)


if __name__ == "__main__":
    sys.exit(main())
