"""
video_pipeline.py
-----------------
Local ffmpeg container operations for the Video Upscaler (future-features #2),
phase 2. ALL container-level work runs **locally** (probe / split /
CFR-normalize / forced-keyframe re-encode / reassemble / audio-mux /
duration-drift check); the SeedVR2 upscale itself happens elsewhere (on a RunPod
pod, phase 3+). This module never touches the GPU and imports no torch.

Design references: docs/video-upscaler.md
  6.1  keyframes: round-up is fine; re-encode only the pathological cases
  6.2  segment edges: fixed seed per video (not this module's concern)
  6.3  duration drift: detection + warnings are MANDATORY in v1
  6.4  ffmpeg: bundled, all container work stays local

The pipeline, end to end:

    probe(src) -> plan_split(...) -> split(...) -> [upscale each segment] ->
    concat_segments(...) -> mux_audio(...) -> check_drift(...)

Phase 2 proves everything EXCEPT the upscale by using the split segments
themselves as the "upscaled" segments (a lossless `-c copy` round trip): if the
round trip reproduces the source frame-for-frame, the container plumbing is
proven and phase 3 just swaps the identity step for the real per-segment
upscale. Run it from the CLI:

    python scripts/video_pipeline.py "benchmark-videos/Pisici.AVI"

Dependency-light: standard library + a bundled ffmpeg/ffprobe (located via
find_ffmpeg). Fail-safe, never touches the source, Windows-aware
(CREATE_NO_WINDOW so no console flashes under pythonw).
"""

import os
import sys
import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Optional

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
# App root = parent of scripts/, matching the rest of the app (config.json,
# seedvr2/, the bundled ffmpeg/ and the .venv all live there, never beside this
# module). Resources are anchored off __file__, never the cwd.
APP_ROOT = os.path.dirname(_SCRIPT_DIR)

# No console window for the child ffmpeg processes under pythonw (Windows only).
_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# Intermediate segment container. Matroska is the most permissive for a `-c copy`
# stream copy (it holds mjpeg, h264, hevc, … without re-muxing pain) and the pod
# re-decodes frames anyway, so the on-disk container of a segment is immaterial to
# the upscale. Override per call if a specific codec needs its native container.
SEGMENT_EXT = ".mkv"

# Audio codecs an MP4/MOV container can hold with `-c copy`. Old-camera sources
# often carry codecs outside this set (e.g. AVI's PCM mu-law), which mp4 refuses
# to mux losslessly — so the audio (only) is re-encoded to AAC then. The video
# stays `-c copy` regardless (that is 6.4's actual concern); audio is tiny and a
# telephone-quality mu-law source re-encoded to AAC is imperceptible.
_MP4_AUDIO_OK = {"aac", "mp3", "ac3", "eac3", "alac", "mov_text"}
_MP4_EXTS = {".mp4", ".m4v", ".mov", ".m4a"}

# Duration-drift tolerance (6.3): a real divergence is anything past ~one frame or
# ~50 ms; below that is rounding noise from container timebases.
DRIFT_FRAME_TOL = 1
DRIFT_SECONDS_TOL = 0.05


class FFmpegError(RuntimeError):
    """An ffmpeg/ffprobe invocation exited non-zero. Carries the stderr tail."""


# ─────────────────────────────────────────────
#  Locating the bundled ffmpeg / ffprobe
# ─────────────────────────────────────────────

_FFMPEG_CACHE: dict = {}


def find_ffmpeg():
    """
    Locate (ffmpeg, ffprobe), in priority order:
      1. $IMGTBX_FFMPEG_DIR            (explicit override, used by tests/CI)
      2. <APP_ROOT>/ffmpeg/bin         (where bootstrap.ps1 unpacks the pinned
                                        gyan.dev build, per 6.4 — both install modes)
      3. the system PATH               (a dev machine with ffmpeg installed)

    Returns absolute paths. Raises FFmpegError with a clear message if neither
    binary is found, so a missing bundle fails loudly rather than mid-run.
    """
    if _FFMPEG_CACHE:
        return _FFMPEG_CACHE["ffmpeg"], _FFMPEG_CACHE["ffprobe"]

    exe = ".exe" if os.name == "nt" else ""
    candidates = []
    env_dir = os.environ.get("IMGTBX_FFMPEG_DIR")
    if env_dir:
        candidates.append(env_dir)
    candidates.append(os.path.join(APP_ROOT, "ffmpeg", "bin"))

    ffmpeg = ffprobe = None
    for d in candidates:
        fm = os.path.join(d, "ffmpeg" + exe)
        fp = os.path.join(d, "ffprobe" + exe)
        if os.path.isfile(fm) and os.path.isfile(fp):
            ffmpeg, ffprobe = fm, fp
            break

    if not ffmpeg:
        ffmpeg = shutil.which("ffmpeg")
        ffprobe = shutil.which("ffprobe")

    if not ffmpeg or not ffprobe:
        raise FFmpegError(
            "ffmpeg/ffprobe not found. Expected the bundled build under "
            f"{os.path.join(APP_ROOT, 'ffmpeg', 'bin')} (downloaded by "
            "bootstrap.ps1), $IMGTBX_FFMPEG_DIR, or on PATH."
        )
    _FFMPEG_CACHE["ffmpeg"], _FFMPEG_CACHE["ffprobe"] = ffmpeg, ffprobe
    return ffmpeg, ffprobe


def _run(args, capture=True, check=True):
    """Run an ffmpeg/ffprobe command, no console window, return CompletedProcess."""
    cp = subprocess.run(
        args,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        creationflags=_CREATE_NO_WINDOW,
        text=True,
    )
    if check and cp.returncode != 0:
        tail = (cp.stderr or "").strip().splitlines()[-12:]
        raise FFmpegError(
            f"command failed ({cp.returncode}): {' '.join(os.path.basename(a) for a in args[:1])} …\n"
            + "\n".join(tail)
        )
    return cp


# ─────────────────────────────────────────────
#  Probe
# ─────────────────────────────────────────────

@dataclass
class VideoInfo:
    path: str
    width: int
    height: int
    r_fps: Fraction          # the container's nominal (CFR) frame rate
    avg_fps: Fraction        # frames / duration — diverges from r_fps when VFR
    nb_frames: int           # best available frame count (may be estimated)
    duration: float          # seconds
    vcodec: str
    pix_fmt: str
    has_audio: bool
    acodec: Optional[str]
    is_vfr: bool
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def fps(self) -> Fraction:
        """The nominal CFR rate to drive segment maths and CFR normalization."""
        return self.r_fps if self.r_fps and self.r_fps > 0 else self.avg_fps


def _parse_fraction(value) -> Fraction:
    """Parse ffprobe's 'num/den' rate strings; 0/0 and junk -> Fraction(0)."""
    try:
        if value in (None, "", "0/0"):
            return Fraction(0)
        if "/" in str(value):
            num, den = str(value).split("/", 1)
            den = int(den)
            return Fraction(int(num), den) if den else Fraction(0)
        return Fraction(value)
    except Exception:
        return Fraction(0)


def probe(path, count=False) -> VideoInfo:
    """
    ffprobe a video into a VideoInfo. Reads the first video + first audio stream
    and the format duration. VFR is flagged by an r_frame_rate vs avg_frame_rate
    divergence > 0.5% (a cheap heuristic; the real safety net is the per-segment
    frame-count assertion in check_drift plus the CFR normalize on re-encode).

    `count=True` replaces nb_frames with an exact packet count (one demux pass).
    The stream's nb_frames HEADER is unreliable — measured: an AVI reported 4923
    in the header but holds 4835 actual frames. Drift detection (6.3) must compare
    COUNTED frames, never the header, or it raises false alarms; metadata-only
    callers keep the instant header read.
    """
    ffmpeg, ffprobe = find_ffmpeg()
    cp = _run([
        ffprobe, "-v", "error", "-print_format", "json",
        "-show_streams", "-show_format", path,
    ])
    data = json.loads(cp.stdout or "{}")
    streams = data.get("streams", [])
    fmt = data.get("format", {})

    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if v is None:
        raise FFmpegError(f"no video stream in {path!r}")

    r_fps = _parse_fraction(v.get("r_frame_rate"))
    avg_fps = _parse_fraction(v.get("avg_frame_rate"))
    duration = float(v.get("duration") or fmt.get("duration") or 0.0) or 0.0

    if count:
        nb_frames = count_frames(path)
    else:
        nb = v.get("nb_frames")
        nb_frames = int(nb) if nb and str(nb).isdigit() else 0
        if not nb_frames and duration and avg_fps:
            nb_frames = round(duration * float(avg_fps))

    is_vfr = False
    if r_fps > 0 and avg_fps > 0:
        is_vfr = abs(float(r_fps) - float(avg_fps)) / float(r_fps) > 0.005

    return VideoInfo(
        path=path,
        width=int(v.get("width") or 0),
        height=int(v.get("height") or 0),
        r_fps=r_fps,
        avg_fps=avg_fps,
        nb_frames=nb_frames,
        duration=duration,
        vcodec=v.get("codec_name") or "?",
        pix_fmt=v.get("pix_fmt") or "?",
        has_audio=a is not None,
        acodec=(a or {}).get("codec_name"),
        is_vfr=is_vfr,
        raw=data,
    )


def count_frames(path) -> int:
    """
    Exact video frame count via packet count (`-count_packets`). Robust where
    nb_frames is absent or estimated (e.g. a freshly split mkv segment). One
    packet == one coded frame for counting purposes (B-frame reordering doesn't
    change the count). Returns 0 on failure.
    """
    _, ffprobe = find_ffmpeg()
    try:
        cp = _run([
            ffprobe, "-v", "error", "-select_streams", "v:0",
            "-count_packets", "-show_entries", "stream=nb_read_packets",
            "-of", "csv=p=0", path,
        ])
        return int((cp.stdout or "0").strip() or 0)
    except Exception:
        return 0


def max_keyframe_gap(path) -> float:
    """
    Largest gap (seconds) between consecutive keyframes — the minimum segment
    length a `-c copy` split can achieve (6.1). Decodes keyframe timestamps only
    (`-skip_frame nokey`), so it is cheap even on long files. Returns 0.0 if a
    single keyframe / no timestamps (caller treats 0 as "no constraint").
    """
    _, ffprobe = find_ffmpeg()
    cp = _run([
        ffprobe, "-v", "error", "-select_streams", "v:0",
        "-skip_frame", "nokey", "-show_entries", "frame=pts_time",
        "-of", "csv=p=0", path,
    ])
    times = []
    for line in (cp.stdout or "").splitlines():
        line = line.strip().rstrip(",")
        if not line:
            continue
        try:
            times.append(float(line))
        except ValueError:
            continue
    if len(times) < 2:
        return 0.0
    times.sort()
    return max(b - a for a, b in zip(times, times[1:]))


# ─────────────────────────────────────────────
#  Plan + split
# ─────────────────────────────────────────────

@dataclass
class SplitPlan:
    mode: str                # "copy" | "reencode"
    reason: str              # human-readable why
    segment_seconds: float
    fps: Fraction            # the CFR rate the segments will carry


def pick_encoder(prefer_hw=True):
    """
    Choose the local re-encode video codec by capability (6.4): NVENC (hevc, else
    h264) when the bundled ffmpeg exposes it and a CUDA GPU is present; otherwise
    a CPU libx265/libx264 fallback. Quality barely matters here (SeedVR2
    hallucinates detail downstream), so the settings favour a fast, near-lossless
    intermediate. Returns (codec, [extra ffmpeg args], is_hardware).
    """
    ffmpeg, _ = find_ffmpeg()
    encoders = ""
    try:
        encoders = _run([ffmpeg, "-hide_banner", "-encoders"], check=False).stdout or ""
    except Exception:
        encoders = ""
    if prefer_hw:
        if "hevc_nvenc" in encoders:
            return "hevc_nvenc", ["-preset", "p5", "-rc", "vbr", "-cq", "19"], True
        if "h264_nvenc" in encoders:
            return "h264_nvenc", ["-preset", "p5", "-rc", "vbr", "-cq", "19"], True
    if "libx265" in encoders:
        return "libx265", ["-preset", "medium", "-crf", "18"], False
    return "libx264", ["-preset", "medium", "-crf", "18"], False


def plan_split(info: VideoInfo, segment_seconds=60.0, max_segment_seconds=120.0,
               force_reencode=False) -> SplitPlan:
    """
    Decide how to split (6.1 / 6.3 / 6.4). Default is a free, lossless `-c copy`
    stream split. Re-encode (CFR normalize + forced keyframes) is triggered only
    by a real reason:
      * VFR source            -> -vsync cfr kills progressive lip-sync drift (6.3)
      * sparse GOP            -> keyframes so far apart that a `-c copy` segment
                                 would exceed max_segment_seconds (6.1)
      * force_reencode        -> caller override (e.g. an undecodable codec)
    """
    if force_reencode:
        return SplitPlan("reencode", "caller forced re-encode",
                         segment_seconds, info.fps)
    if info.is_vfr:
        return SplitPlan("reencode",
                         f"VFR source (r={float(info.r_fps):.3f} != avg="
                         f"{float(info.avg_fps):.3f} fps): CFR-normalize",
                         segment_seconds, info.fps)
    # Mistagged frame rate: a source can TAG a clean CFR (r == avg) yet hold a
    # frame count that doesn't match `tagged_fps x duration` — an old AVI tags
    # 30fps but holds 4835 frames over 164.1s = a REAL 29.46fps. Upscaling re-times
    # it to the tagged fps (SeedVR2's writer trusts CAP_PROP_FPS), shortening the
    # video and desyncing the muxed audio (measured on Pisici.AVI: a 2.9s
    # progressive drift, the dangerous lip-sync case in 6.3). CFR-normalize pads
    # the frames to a true CFR stream matching the duration, keeping A/V in sync.
    # Needs COUNTED frames (probe(count=True)); the header count would hide it.
    if info.duration and info.nb_frames and info.r_fps > 0:
        eff = info.nb_frames / info.duration
        if abs(eff - float(info.r_fps)) / float(info.r_fps) > 0.005:
            return SplitPlan("reencode",
                             f"tagged {float(info.r_fps):.3f}fps but real "
                             f"{eff:.3f}fps (counted/duration): CFR-normalize",
                             segment_seconds, info.fps)
    gap = max_keyframe_gap(info.path)
    if gap > max_segment_seconds:
        return SplitPlan("reencode",
                         f"sparse GOP (keyframe gap {gap:.1f}s > "
                         f"{max_segment_seconds:.0f}s cap): forced keyframes",
                         segment_seconds, info.fps)
    return SplitPlan("copy",
                     f"clean CFR, keyframe gap {gap:.2f}s — lossless stream copy",
                     segment_seconds, info.fps)


@dataclass
class Segment:
    index: int
    path: str
    frame_count: int


def split(info: VideoInfo, plan: SplitPlan, out_dir, segment_ext=SEGMENT_EXT):
    """
    Split `info.path` into video-only segments in `out_dir` per `plan`. Audio is
    dropped here (`-an`) and muxed back from the original at reassembly (6.4), so
    a segment is a pure video unit ready to upscale. Returns [Segment].

    copy mode:     instant, lossless, no GPU. Cuts snap to keyframes >= the
                   requested time (frame-accurate on all-intra sources like mjpeg).
    reencode mode: one ffmpeg pass folding -vsync cfr + forced keyframes + the
                   segmenter, so VFR/sparse-GOP files become cleanly splittable.
    """
    ffmpeg, _ = find_ffmpeg()
    os.makedirs(out_dir, exist_ok=True)
    pattern = os.path.join(out_dir, "seg_%05d" + segment_ext)
    seg = float(plan.segment_seconds)

    args = [ffmpeg, "-hide_banner", "-y", "-i", info.path, "-map", "0:v:0", "-an"]
    if plan.mode == "reencode":
        codec, enc_args, _hw = pick_encoder()
        fps = plan.fps if plan.fps and plan.fps > 0 else Fraction(30)
        # Normalize to yuv420p: NVENC rejects 4:2:2 (measured: a yuvj422p mjpeg
        # source 400s hevc_nvenc with "YUV422P not supported"), and yuv420p is the
        # universally-accepted delivery format for x264/x265/nvenc. Acceptable
        # because this re-encode is only an intermediate (its output is upscaled
        # downstream by a detail-hallucinating model), never the deliverable.
        args += ["-vsync", "cfr", "-r", f"{fps.numerator}/{fps.denominator}",
                 "-c:v", codec, *enc_args, "-pix_fmt", "yuv420p",
                 "-force_key_frames", f"expr:gte(t,n_forced*{seg})"]
    else:
        args += ["-c", "copy"]
    args += ["-f", "segment", "-segment_time", f"{seg}",
             "-reset_timestamps", "1", "-segment_format",
             segment_ext.lstrip("."), pattern]

    _run(args, capture=True)

    segments = []
    for i, name in enumerate(sorted(os.listdir(out_dir))):
        if not name.startswith("seg_") or not name.endswith(segment_ext):
            continue
        p = os.path.join(out_dir, name)
        segments.append(Segment(index=i, path=p, frame_count=count_frames(p)))
    return segments


# ─────────────────────────────────────────────
#  Reassemble (concat + audio mux) — both -c copy (6.4)
# ─────────────────────────────────────────────

def concat_segments(segment_paths, out_path):
    """
    Lossless `-c copy` concat of the (upscaled) segments via the concat demuxer.
    All segments share identical codec params (same worker settings), so the
    concat never re-encodes. Returns out_path.
    """
    ffmpeg, _ = find_ffmpeg()
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    # The concat demuxer needs a list file; quote paths and escape backslashes so
    # Windows paths survive its parser.
    fd, list_path = tempfile.mkstemp(suffix=".txt", prefix="concat_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for p in segment_paths:
                safe = os.path.abspath(p).replace("\\", "/").replace("'", "'\\''")
                f.write(f"file '{safe}'\n")
        _run([ffmpeg, "-hide_banner", "-y", "-f", "concat", "-safe", "0",
              "-i", list_path, "-map", "0", "-c", "copy",
              "-fflags", "+genpts", out_path])
    finally:
        try:
            os.remove(list_path)
        except OSError:
            pass
    return out_path


def _audio_copy_args(source: VideoInfo, out_path, log=None):
    """
    Pick the audio mux args (6.4). Video is always `-c copy`; audio is `-c:a copy`
    too UNLESS the source codec can't live in the target container (e.g. an AVI's
    PCM mu-law in mp4), in which case the audio alone is re-encoded to AAC. Returns
    [] if the source has no audio.
    """
    if not source.has_audio:
        return []
    ext = os.path.splitext(out_path)[1].lower()
    needs_reencode = ext in _MP4_EXTS and (source.acodec or "").lower() not in _MP4_AUDIO_OK
    if needs_reencode:
        if log:
            log(f"         audio: {source.acodec} can't `-c copy` into {ext}; "
                f"re-encoding to AAC")
        return ["-c:a", "aac", "-b:a", "192k"]
    return ["-c:a", "copy"]


def mux_audio(video_path, source_path, out_path, log=None):
    """
    Mux the original source's audio onto the reassembled (upscaled) video. The
    video is always `-c copy` (no re-encode of the upscaled result, 6.4); audio is
    `-c copy` when the container accepts it, else re-encoded to AAC (see
    _audio_copy_args). If the source has no audio the video is copied through
    unchanged. Returns out_path.
    """
    ffmpeg, _ = find_ffmpeg()
    info = probe(source_path)
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    audio_args = _audio_copy_args(info, out_path, log=log)
    if not audio_args:
        # Nothing to mux; still normalize to the output container with a copy.
        _run([ffmpeg, "-hide_banner", "-y", "-i", video_path,
              "-map", "0:v:0", "-c", "copy", out_path])
        return out_path
    _run([ffmpeg, "-hide_banner", "-y", "-i", video_path, "-i", source_path,
          "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", *audio_args, out_path])
    return out_path


# ─────────────────────────────────────────────
#  Duration-drift detection (6.3) — mandatory in v1
# ─────────────────────────────────────────────

@dataclass
class DriftReport:
    ok: bool
    warnings: list = field(default_factory=list)
    details: dict = field(default_factory=dict)


def check_drift(source: VideoInfo, output: VideoInfo, segment_frame_counts,
                reference_frames=None):
    """
    Detect (never silently fix, 6.3) divergence between source and output. Three
    independent checks, each a warning if it trips:
      1. sum(segment frame counts) == reference frame count
      2. output frame count == reference frame count
      3. |output duration - source duration| within tolerance

    `reference_frames` is the frame count the output is EXPECTED to have: the
    source frame count for a copy split, or the post-CFR-normalize count for a
    re-encode (which legitimately differs from a VFR source). Defaults to the
    source's nb_frames. The output is flagged "review recommended" on any warning;
    the caller keeps the upscaled result either way.
    """
    ref = reference_frames if reference_frames is not None else source.nb_frames
    warnings = []
    seg_sum = sum(segment_frame_counts)

    if ref and seg_sum != ref:
        warnings.append(
            f"segment frames sum to {seg_sum}, expected {ref} "
            f"(delta {seg_sum - ref:+d})")
    if ref and output.nb_frames and output.nb_frames != ref:
        warnings.append(
            f"output has {output.nb_frames} frames, expected {ref} "
            f"(delta {output.nb_frames - ref:+d})")

    fps = float(source.fps) or 30.0
    dur_tol = max(DRIFT_SECONDS_TOL, DRIFT_FRAME_TOL / fps)
    dur_delta = (output.duration or 0.0) - (source.duration or 0.0)
    if abs(dur_delta) > dur_tol:
        warnings.append(
            f"output runs {output.duration:.3f}s vs source "
            f"{source.duration:.3f}s (delta {dur_delta:+.3f}s > {dur_tol:.3f}s)")

    return DriftReport(
        ok=not warnings,
        warnings=warnings,
        details={
            "source_frames": source.nb_frames,
            "output_frames": output.nb_frames,
            "segment_frames_sum": seg_sum,
            "reference_frames": ref,
            "source_duration": source.duration,
            "output_duration": output.duration,
            "duration_delta": dur_delta,
        },
    )


# ─────────────────────────────────────────────
#  Phase-2 CLI: passthrough round-trip proof
# ─────────────────────────────────────────────

def passthrough_roundtrip(src, out_path, segment_seconds=60.0,
                          max_segment_seconds=120.0, work_dir=None,
                          force_reencode=False, keep=False, log=print):
    """
    Prove the container pipeline with NO pod (phase 2): split the source, then
    reassemble those same segments (identity = "upscaled") and mux the original
    audio. A lossless `-c copy` round trip must reproduce the source frame-for-
    frame; check_drift verifies it. Returns (DriftReport, output_path).
    """
    src = os.path.abspath(src)
    info = probe(src, count=True)   # exact frame count for a trustworthy drift reference
    log(f"Source : {os.path.basename(src)}")
    log(f"         {info.width}x{info.height}  {float(info.fps):.3f} fps  "
        f"{info.nb_frames} frames  {info.duration:.2f}s  "
        f"{info.vcodec}/{info.pix_fmt}  audio={info.acodec or 'none'}  "
        f"vfr={info.is_vfr}")

    plan = plan_split(info, segment_seconds, max_segment_seconds, force_reencode)
    log(f"Plan   : {plan.mode}  ({plan.reason})")

    temp_root = work_dir or tempfile.mkdtemp(prefix="video_pipeline_")
    seg_dir = os.path.join(temp_root, "segments")
    try:
        segments = split(info, plan, seg_dir)
        counts = [s.frame_count for s in segments]
        log(f"Split  : {len(segments)} segment(s), frames per segment = {counts}")
        if not segments:
            raise FFmpegError("split produced no segments")

        # Reference = source frames for a copy split; for a re-encode the CFR
        # normalize sets a new ground truth, so trust the segment sum.
        reference = info.nb_frames if plan.mode == "copy" else sum(counts)

        concat_path = os.path.join(temp_root, "concat" + SEGMENT_EXT)
        concat_segments([s.path for s in segments], concat_path)
        out_path = mux_audio(concat_path, src, out_path, log=log)
        out_info = probe(out_path, count=True)
        log(f"Output : {os.path.basename(out_path)}  {out_info.nb_frames} frames  "
            f"{out_info.duration:.2f}s  audio={out_info.acodec or 'none'}")

        report = check_drift(info, out_info, counts, reference_frames=reference)
        if report.ok:
            log("Drift  : OK — frame count and duration preserved.")
        else:
            log("Drift  : REVIEW RECOMMENDED")
            for w in report.warnings:
                log(f"         ! {w}")
        return report, out_path
    finally:
        if not keep:
            shutil.rmtree(temp_root, ignore_errors=True)


def main(argv=None):
    import argparse
    p = argparse.ArgumentParser(
        description="Video Upscaler phase-2 container pipeline: prove "
                    "split -> concat -> audio-mux -> drift on passthrough segments.")
    p.add_argument("source", help="source video")
    p.add_argument("output", nargs="?", help="output path (default: <name>_roundtrip.mp4 beside source)")
    p.add_argument("--segment-seconds", type=float, default=60.0)
    p.add_argument("--max-segment-seconds", type=float, default=120.0)
    p.add_argument("--work-dir", help="keep intermediates here (implies --keep)")
    p.add_argument("--force-reencode", action="store_true",
                   help="exercise the CFR-normalize / forced-keyframe path")
    p.add_argument("--keep", action="store_true", help="keep intermediate segments")
    args = p.parse_args(argv)

    src = os.path.abspath(args.source)
    if not os.path.isfile(src):
        print(f"ERROR: source not found: {src}")
        return 2
    out = args.output
    if not out:
        base, _ = os.path.splitext(src)
        out = base + "_roundtrip.mp4"

    try:
        report, out_path = passthrough_roundtrip(
            src, out,
            segment_seconds=args.segment_seconds,
            max_segment_seconds=args.max_segment_seconds,
            work_dir=args.work_dir,
            force_reencode=args.force_reencode,
            keep=args.keep or bool(args.work_dir),
        )
    except FFmpegError as e:
        print(f"\nFFMPEG ERROR:\n{e}")
        return 1
    # Drift is a warning, not a hard failure (6.3): the result is kept and the exit
    # code stays 0 either way. The warnings are already on stdout for a human/log.
    print(f"\nWrote {out_path}{'' if report.ok else '  (review recommended — see drift warnings)'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
