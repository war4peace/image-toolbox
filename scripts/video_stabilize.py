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

GUI control lines (stdin):  q   (stop; the partial output is discarded)
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
    """Writes to both the terminal and logs/stab_<hash of source path>.log."""

    def __init__(self, source):
        digest  = hashlib.sha256(source.encode("utf-8")).hexdigest()[:12]
        log_dir = os.path.join(APP_ROOT, "logs")
        os.makedirs(log_dir, exist_ok=True)
        self.path = os.path.join(log_dir, f"stab_{digest}.log")
        self._fh  = open(self.path, "a", encoding="utf-8", buffering=1)
        ts = datetime.datetime.now().strftime("%Y-%m-%d | %H:%M:%S")
        self._fh.write(f"\n{'=' * 64}\nStabilization session: {ts}\n"
                       f"Source: {source}\n{'=' * 64}\n")

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


def completion_notice(ok, stopped, reason=""):
    """(title, color) for the end-of-run notification. Pure, so it is unit-tested.
    Colours are notifications.COLOR_* constants."""
    if stopped:
        return "Video Stabilization -- Stopped by User", notifications.COLOR_YELLOW
    if not ok:
        return "Video Stabilization -- Failed", notifications.COLOR_RED
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


def suggest_output_path(src, suffix="_stabilized", ext=".mp4"):
    """Default output beside the source: `<stem>_stabilized.mp4`. Never returns the
    source path itself, and never an existing file - a stabilise that silently ate
    its own input, or someone else's output, is not recoverable."""
    src = os.path.abspath(src)
    folder = os.path.dirname(src)
    stem = os.path.splitext(os.path.basename(src))[0]
    candidate = os.path.join(folder, f"{stem}{suffix}{ext}")
    n = 2
    while (os.path.exists(candidate)
           or os.path.normcase(candidate) == os.path.normcase(src)):
        candidate = os.path.join(folder, f"{stem}{suffix}_{n}{ext}")
        n += 1
    return candidate


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

def stabilize(src, dest, log, smoothing=DEFAULT_SMOOTHING,
              shakiness=DEFAULT_SHAKINESS, accuracy=DEFAULT_ACCURACY,
              optzoom=DEFAULT_OPTZOOM, crop=DEFAULT_CROP, skip_health=False):
    """Stabilise `src` into `dest`. Returns a summary dict. Raises StabilizeError on a
    refusal or an ffmpeg failure, KeyboardInterrupt when the user stopped it."""
    ffmpeg, _ffprobe = vp.find_ffmpeg()
    src = os.path.abspath(src)
    dest = os.path.abspath(dest)
    if os.path.normcase(src) == os.path.normcase(dest):
        raise StabilizeError("The output file must be different from the source.")
    if not os.path.isfile(src):
        raise StabilizeError(f"Source video not found: {src}")

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
            on_progress=lambda f, t: _report(1, f, t, started),
            stall_timeout=vp._ENCODE_STALL_S)
        # Pass 1 has now counted the frames exactly; the header count that seeded the
        # first bar can be an estimate, so pass 2 gets the measured number.
        if counted > 0:
            total = counted
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
            on_progress=lambda f, t: _report(2, f, t, started),
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


_last_report = [0.0]


def _report(which, frame, total, started):
    """Progress + ETA for one pass. Throttled: ffmpeg emits a -progress block per
    frame, and forwarding thousands of marker lines would flood the GUI's pipe.

    Both events are reported in WHOLE-JOB units (frames across both passes), not
    per-pass ones: two passes of comparable length, so pass 1 fills the first half
    and pass 2 the second. A per-pass ETA would count down to zero twice, and a bar
    that reaches 100% halfway through is worse than no bar. Pass 2 is the slower of
    the two (measured 2.4x realtime against 3.8x), so the estimate runs a little
    optimistic during pass 1 and corrects itself as pass 2 gets going."""
    now = time.time()
    if now - _last_report[0] < 0.25 and frame != total:
        return
    _last_report[0] = now
    if not total or total <= 0:
        return
    whole = total * 2
    done = min(frame + (total if which == 2 else 0), whole)
    _gui_event("PROG", f"{done}|{whole}")
    elapsed = now - started
    _gui_event("ETA", f"{elapsed:.1f}|{done}|{done}|{whole}")


# ─────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────

def build_parser():
    p = argparse.ArgumentParser(
        description="Stabilise one shaky video into a new file (future-features #20).")
    p.add_argument("source", help="the video to stabilise (never modified)")
    p.add_argument("output", nargs="?", default=None,
                   help="output file (default: <stem>_stabilized.mp4 beside the source)")
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

    src = os.path.abspath(args.source)
    dest = os.path.abspath(args.output) if args.output else suggest_output_path(src)

    log = Logger(src)
    _gui_event("LOG", log.path)
    log.tee(f"Stabilising: {src}")
    log.tee(f"  Output:  {dest}")
    log.tee(f"  Settings: smoothing={args.smoothing}, optzoom={args.optzoom}, "
            f"crop={args.crop}, shakiness={args.shakiness}, accuracy={args.accuracy}")
    if args.optzoom:
        log.tee("  ! optzoom=1 zooms in to hide the borders: this LOSES picture at "
                "every edge.")

    if GUI_MODE:
        threading.Thread(target=_watch_stdin, daemon=True).start()

    stopped = False
    result = None
    error = ""
    try:
        result = stabilize(src, dest, log,
                           smoothing=args.smoothing, shakiness=args.shakiness,
                           accuracy=args.accuracy, optzoom=args.optzoom,
                           crop=args.crop, skip_health=args.skip_health)
    except KeyboardInterrupt:
        stopped = True
        log.tee("  Stopped by user - nothing was written.")
        _gui_event("STATUS", "Stopped - nothing was written.")
    except StabilizeError as exc:
        error = str(exc)
        log.tee(f"  Refused: {error}")
        _gui_event("STATUS", error.splitlines()[0] if error else "Failed.")
        _gui_event("REFUSED", json.dumps({"reason": error}))
    except Exception as exc:                         # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
        log.tee(f"  Failed: {error}")
        _gui_event("STATUS", f"Failed - {error}")

    ok = result is not None
    title, color = completion_notice(ok, stopped, error)
    if ok:
        _gui_event("DONE", json.dumps(result))
        _gui_event("STATUS", f"Done - {os.path.basename(dest)}")
        send_notification(title, f"Stabilised **{os.path.basename(src)}**", color,
                          fields=[{"name": "Output", "value": os.path.basename(dest)},
                                  {"name": "Frames", "value": str(result.get("frames", "?"))},
                                  {"name": "Time",
                                   "value": fmt_duration(result.get("elapsed_seconds", 0))}])
    elif stopped:
        _gui_event("DONE", json.dumps({"tool": "stabilize", "source": src,
                                       "processed": 0, "failed": 0,
                                       "stopped_by_user": True,
                                       "elapsed_seconds": 0}))
        send_notification(title, f"Stopped while stabilising "
                                 f"**{os.path.basename(src)}**", color)
    else:
        _gui_event("DONE", json.dumps({"tool": "stabilize", "source": src,
                                       "processed": 0, "failed": 1,
                                       "stop_reason": error.splitlines()[0] if error else "",
                                       "elapsed_seconds": 0}))
        send_notification(title, f"Could not stabilise **{os.path.basename(src)}**",
                          color, fields=[{"name": "Reason",
                                          "value": (error.splitlines() or [""])[0]}])
    log.close()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
