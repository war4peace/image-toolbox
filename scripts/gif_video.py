"""
gif_video.py
------------
Animated GIF -> upscaled video (roadmap #27 phase 2; the design record, including the
measurements behind every choice below, is `docs/video-upscaler.md` section 20).

An animated GIF is upscaled by the VIDEO Upscaler, not the Batch Upscaler, and the
reason is not architectural taste. `upscale_engine.upscale()` draws a FRESH RANDOM SEED
for every image, with no setting to pin it; the video path fixes one stable seed per
source (`batch_video_upscale.per_video_seed`). On a photo that is invisible. On ten
consecutive frames of one scene it is generative FLICKER, and no encoder fixes it. The
video path also brings temporal batching, which is the thing that makes an animation
look coherent in the first place.

The shape is PREP -> the existing pipeline, unchanged -> RETIME:

  * `prepare()` turns the GIF into an ordinary constant-rate video with EXACTLY one
    frame per source frame, in the work area. Everything downstream (plan_split, split,
    the engine, concat, the drift check) then treats it as any other short video.
  * `retime()` puts the original per-frame timing back afterwards, duplicating frames
    at ENCODE time.

That ordering is the whole feature, and it is measured. Feeding a GIF straight to the
splitter works -- `probe()` reads GIF and `plan_split` correctly plans a CFR normalise
-- but it normalises to the container's nominal rate, and GIF delays are
centisecond-quantised and routinely non-uniform, so the frame count INFLATES before the
model ever runs. Measured on 10-frame GIFs through this app's own `probe()`:

    uniform 100ms delays          10 -> 10 frames   (1.0x, no penalty)
    messy real-world delays       10 -> 38 frames   (3.8x)

Every one of those duplicates is a full diffusion pass, and the cost is invisible in the
UI. Prep-then-retime pays 10 either way, because the duplication happens at encode time
where it is free. The rule generalises past GIF: DUPLICATE AFTER UPSCALING, NEVER
BEFORE.

Torch-free. Stdlib + Pillow (frame delays, which ffmpeg does not expose per frame) +
the bundled ffmpeg through `video_pipeline`. Pillow is imported lazily so importing this
module stays instant, the same rule `raw_decode` follows.
"""

import math
import os
import shutil

import video_pipeline as vp

GIF_EXT = ".gif"

# Output frame rate bounds for the delivered file. Below 10 the result stutters on
# players that resample, and above 100 is meaningless for source material quantised to
# centiseconds in the first place.
MIN_FPS = 10
MAX_FPS = 100

# The rate of the intermediate handed to the pipeline. Arbitrary and invisible: it
# exists only so the file is a clean CFR video, since the real timing is re-applied by
# retime() afterwards. 10 keeps the intermediate's declared duration close to the GIF's
# own, which makes the pipeline's own progress readouts less confusing to read in a log.
NOMINAL_FPS = 10

# What a browser substitutes for a 0 ms delay, which some encoders emit meaning "as fast
# as possible". Without this the frame would be dropped entirely.
ZERO_DELAY_MS = 100

DEFAULT_MATTE = "black"


def is_animated(path):
    """True when `path` is a GIF with more than one frame. False for a static GIF, for
    anything that is not a GIF, and for an unreadable file (which the callers' existing
    "corrupted / unreadable" paths report properly, exactly as
    `runner_common.image_variant_reason` does)."""
    if os.path.splitext(path)[1].lower() != GIF_EXT:
        return False
    try:
        from PIL import Image
        with Image.open(path) as im:
            return int(getattr(im, "n_frames", 1) or 1) > 1
    except Exception:                                    # noqa: BLE001
        return False


def frame_delays(path):
    """Per-frame delays in milliseconds, or [] if unreadable.

    Read with Pillow rather than ffmpeg because ffmpeg exposes a nominal rate and
    per-packet timestamps, not the GIF's own per-frame delay values, and those values
    are what retime() has to reproduce exactly.
    """
    try:
        from PIL import Image
        out = []
        with Image.open(path) as im:
            for i in range(int(getattr(im, "n_frames", 1) or 1)):
                im.seek(i)
                out.append(int(im.info.get("duration") or 0))
        return out
    except Exception:                                    # noqa: BLE001
        return []


def plan_timing(delays_ms):
    """(fps, repeats_per_frame) reproducing `delays_ms` exactly at a constant rate.

    Pure arithmetic, so the part that decides the output's length is unit-tested without
    ffmpeg. GIF delays are centisecond-quantised, so a tick dividing all of them always
    exists: take the GCD, and each frame is then shown delay/tick times. Total frames is
    arithmetic rather than a decision ffmpeg makes for us, which is the point.

    That matters because the obvious form is WRONG, and measurably. Writing concat
    `duration` directives plus the repeated last frame the demuxer needs (or it drops
    it) ran +60 ms long on a 760 ms clip; adding `-t` to trim overcorrected to -40 ms.
    Both leave the trailing frame's length to ffmpeg. This form measured ZERO drift on
    all four timing shapes tested, including one 10 ms tick among 100 ms frames.

    Returns (0, []) for an empty input.
    """
    d = [int(x) if x and int(x) > 0 else ZERO_DELAY_MS for x in (delays_ms or [])]
    if not d:
        return 0, []

    tick = 0
    for x in d:
        tick = math.gcd(tick, x)
    tick = tick or ZERO_DELAY_MS
    fps = int(round(1000.0 / tick))

    if fps < MIN_FPS or fps > MAX_FPS:
        # Clamp and re-derive: the exact tick is unusable, so frames land on the nearest
        # tick of a sane rate instead. Duration then moves by at most half a tick per
        # frame, which no viewer can see and which the drift check tolerates.
        fps = max(MIN_FPS, min(MAX_FPS, fps))
        tick = 1000.0 / fps

    return fps, [max(1, int(round(x / tick))) for x in d]


# The marker that goes into an output name. Lives here as a constant because TWO
# places build the name: this module states the rule, and
# `batch_video_upscale._output_path` applies it inside the naming it already owns
# (target, clip range, engine tag). A test pins that the two agree.
OUTPUT_MARKER = "_gif"


def output_name(src_name, target):
    """`<base>_gif_<target>.mp4`.

    The `_gif` marker is this codebase's third encounter with one collision: the video
    upscaler names its outputs `<base>_<target>.mp4`, so `logo.gif` and `logo.mp4` in
    one folder would both claim `logo_4K.mp4`. That is not a crash, which is what makes
    it dangerous -- one silently becomes "already upscaled" and its lineage row points
    at a file made from the other source. #19 met it as RAW+JPEG, #27 phase 1 met it as
    GIF+PNG, and the answer is the same each time: an UNCONDITIONAL marker, never "only
    when it would collide", because a name that depends on what else is in the folder
    changes when a sibling is added later.
    """
    base = os.path.splitext(os.path.basename(src_name))[0]
    return f"{base}{OUTPUT_MARKER}_{target}.mp4"


def is_output_name(name):
    """True when `name` looks like a video this module produced from a GIF."""
    stem = os.path.splitext(os.path.basename(name or ""))[0].lower()
    return (OUTPUT_MARKER + "_") in stem


def _explode(src, dest_dir, ffmpeg, keep_alpha=False):
    """Every frame of `src` as a PNG, one file per frame, numbered from 1.

    `-fps_mode passthrough` is what makes it frame-exact: no rate conversion, so no
    frame is invented or dropped. Verified 10 -> 10 on uniform, non-uniform and
    single-fast-tick GIFs. Nothing else in the filter chain may set a rate, which is
    why the matte is composited afterwards rather than here (see prepare).

    `keep_alpha` forces rgba out, so a transparent GIF's alpha survives to the matte
    step instead of being flattened by the PNG encoder's own format choice.
    """
    if os.path.isdir(dest_dir):
        shutil.rmtree(dest_dir, ignore_errors=True)
    os.makedirs(dest_dir, exist_ok=True)
    args = [ffmpeg, "-y", "-v", "error", "-i", src, "-fps_mode", "passthrough"]
    if keep_alpha:
        args += ["-pix_fmt", "rgba"]
    vp._run(args + [os.path.join(dest_dir, "f_%05d.png")], hard_timeout=600)
    return sorted(f for f in os.listdir(dest_dir) if f.endswith(".png"))


def _matte_rgb(colour):
    """`colour` as an (r, g, b) tuple. Accepts anything PIL's ImageColor does: a name
    ("black", "white", "cornflowerblue") or "#RRGGBB". Falls back to the default rather
    than raising, because a typo in a config file must not fail a run."""
    from PIL import ImageColor
    try:
        rgb = ImageColor.getrgb(str(colour or DEFAULT_MATTE))
    except (ValueError, AttributeError):
        rgb = ImageColor.getrgb(DEFAULT_MATTE)
    return rgb[:3]


def prepare(src, dest, work_dir, matte=DEFAULT_MATTE, log=None):
    """Turn the animated GIF `src` into a clean CFR video at `dest`, one frame per
    source frame. Returns the list of per-frame delays, which `retime` needs.

    The work is SPLIT between ffmpeg and Pillow deliberately, and the split is not
    stylistic. ffmpeg explodes the GIF, because GIF frame disposal and coalescing are
    real semantics its decoder implements and a naive per-frame read gets wrong. Pillow
    then composites the matte, because doing that in the filtergraph requires a second
    input (a `color` source), and an overlay takes its output RATE from that background
    input: the first version of this function generated `color=c=black` with no rate,
    and a 10-frame GIF came out as 17 PNGs. Resampling before the model runs is the one
    thing this whole feature exists to avoid, so the matte does not go near the rate.

    The matte is applied DELIBERATELY. A transparent GIF decodes as bgra and a default
    conversion composites it to black, which is also the default matte, so a matte left
    to fall out of the conversion would appear to work while doing nothing.

    Raises FFmpegError on failure, like the rest of the pipeline.
    """
    ffmpeg, _ = vp.find_ffmpeg()
    delays = frame_delays(src)
    if len(delays) < 2:
        raise vp.FFmpegError(f"{os.path.basename(src)} is not an animated GIF")

    os.makedirs(work_dir, exist_ok=True)
    seq_dir = os.path.join(work_dir, "gif_frames")
    names = _explode(src, seq_dir, ffmpeg, keep_alpha=True)
    if len(names) != len(delays):
        raise vp.FFmpegError(
            f"GIF prep is not frame-exact: {len(delays)} source frames, "
            f"{len(names)} extracted. Refusing rather than upscaling the wrong count.")

    from PIL import Image
    rgb = _matte_rgb(matte)
    for name in names:
        path = os.path.join(seq_dir, name)
        with Image.open(path) as im:
            frame = im.convert("RGBA")
        flat = Image.new("RGB", frame.size, rgb)
        flat.paste(frame, mask=frame.split()[3])      # the alpha IS the mask
        flat.save(path)

    os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)
    vp._run([ffmpeg, "-y", "-v", "error",
             "-framerate", str(NOMINAL_FPS),
             "-i", os.path.join(seq_dir, "f_%05d.png"),
             "-vf", "format=yuv420p", "-c:v", "ffv1", dest], hard_timeout=1800)
    shutil.rmtree(seq_dir, ignore_errors=True)

    if log:
        log(f"  GIF prep: {len(delays)} frame(s), matte={matte or DEFAULT_MATTE}")
    return delays


def retime(src, delays, dest, work_dir, log=None):
    """Re-apply the GIF's original per-frame timing to the upscaled video `src`.

    `src` must hold exactly len(delays) frames (the pipeline preserves the count; its
    own drift check is what proves that). Frames are duplicated at ENCODE time, which is
    free, rather than before the model ran, which is not.

    Raises FFmpegError if the frame count does not match, because a silent mismatch
    would re-time the wrong frames onto the wrong delays.
    """
    ffmpeg, _ = vp.find_ffmpeg()
    fps, reps = plan_timing(delays)
    if not reps:
        raise vp.FFmpegError("no frame timing to apply")

    os.makedirs(work_dir, exist_ok=True)
    frames_dir = os.path.join(work_dir, "gif_out")
    names = _explode(src, frames_dir, ffmpeg)
    if len(names) != len(reps):
        raise vp.FFmpegError(
            f"upscaled video has {len(names)} frames but the GIF had {len(reps)}; "
            f"refusing to re-time onto a different frame count")

    # The listing lives BESIDE the frames, and that is load-bearing rather than tidy:
    # the concat demuxer resolves a relative entry against the LISTING's directory, not
    # against the process's cwd. Putting it anywhere else means writing absolute paths
    # into a file whose entries have their own quoting rules, and a Windows user's
    # folder can contain spaces, quotes and brackets. Bare filenames sidestep all of it,
    # the same reasoning as video_stabilize's .trf file.
    listing = os.path.join(frames_dir, "concat.txt")
    with open(listing, "w", encoding="utf-8") as fh:
        for name, count in zip(names, reps):
            for _ in range(count):
                fh.write(f"file '{name}'\n")

    os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)
    codec, extra, _hw = vp.pick_encoder()
    # Encode to a STAGING file, then move. Two reasons, and the first is not optional:
    # the caller re-times IN PLACE (src and dest are the same upscaled output), and
    # ffmpeg cannot open one path for reading and writing at once. The second is the
    # habit the rest of this pipeline already follows: finalising an mp4 seeks back to
    # write the moov atom, which an SMB share can leave unwritten, so the bytes are
    # built locally and moved across in one plain sequential copy.
    staged = os.path.join(work_dir, "gif_retimed.mp4")
    vp._run([ffmpeg, "-y", "-v", "error",
             "-f", "concat", "-safe", "0", "-r", str(fps),
             "-i", listing,
             "-fps_mode", "passthrough", "-c:v", codec] + list(extra or []) +
            ["-pix_fmt", vp.delivery_pix_fmt(codec), "-movflags", "+faststart", staged],
            cwd=frames_dir, stall_timeout=600)
    if not vp.probe(staged, count=True).nb_frames:      # never ship a dud
        raise vp.FFmpegError("re-timed output has no decodable frames")
    shutil.move(staged, dest)
    shutil.rmtree(frames_dir, ignore_errors=True)   # takes the listing with it

    if log:
        log(f"  GIF retime: {len(reps)} frame(s) -> {sum(reps)} at {fps} fps "
            f"({sum(delays) / 1000.0:.3f}s)")
    return dest
