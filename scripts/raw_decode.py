"""
raw_decode.py
-------------
RAW / DNG input for the Batch Upscaler (roadmap #19).

**The app renders RAW, it does not develop it.** A RAW is sensor data plus
instructions, so turning it into a picture means choosing a demosaic, white
balance, tone curve and colour space. Those choices ARE the photo, and this app
has never had a rendering opinion. There is no exposure/WB/curve UI here and
there is not going to be one; what this module does is produce the picture the
camera itself would have produced, and nothing more.

Two ways to get there, in this order:

  * **S2, the preview path** - lift the camera's own embedded full-size JPEG
    preview. That IS the manufacturer's rendering, i.e. exactly what the camera
    would have written in JPEG mode, so no opinion is required from us, and it
    costs no demosaic (measured: ~0.00 s vs 0.27-0.46 s).
  * **S1, the demosaic path** - LibRaw's own render with fixed defaults, for the
    files that have no full-size preview.

S2 is used only when the preview is essentially the whole image
(PREVIEW_TOLERANCE), which makes the two paths dimension-identical BY
CONSTRUCTION - that is what lets the eligibility scan read sizes from the header
and stay correct whichever path the run later takes.

Measured on 24 CC0 camera files, 2004-2020, 9 formats (docs/raw-preview-survey.csv):
S2 applies to 42%. So both paths are main paths; neither is an edge case.

**Metadata comes from BOTH files, merged.** The plan for #19 assumed the preview
"carries the camera's EXIF intact", so S2 would make #13 free. It does not: a
typical preview block is 12 top-level + 4 Exif tags, with DateTime and the
exposure triple but **no DateTimeOriginal and no GPS**. The RAW's own block is
much richer (24+31+GPS) but Pillow can only open the TIFF-derived containers - it
cannot open CR3, ORF, RW2, RAF, ARW or SRW at all (9 of the 24). Neither source
covers the set alone; the union covers all of it, and for the RAF/RW2 pair the
preview is actually the RICHER of the two. So the rule is exif_copy's own
"copy what is missing, keep what is present", applied RAW-block-first.

Everything here is read-only: the RAW is opened, never written. rawpy/LibRaw is
imported lazily so a machine without it still loads this module (and every
entry point then reports "no" rather than raising).
"""

import io
import os

try:
    from debug_log import debug_log
except Exception:                                  # noqa: BLE001 (old install)
    def debug_log(*_a, **_k):
        pass


# The common set (#19 decision 6). LibRaw covers all of them, so the marginal
# cost of each is one entry in this tuple. Lowercase, matched against
# os.path.splitext()[1].lower().
RAW_EXTS = (".dng", ".cr2", ".cr3", ".nef", ".arw", ".orf",
            ".rw2", ".raf", ".pef", ".srw")

# How close to the full image an embedded preview must be before it is used as
# the picture (#19 decision 2). 2% covers the few-pixel border crop every camera
# in the survey applies to its preview (a 3900x2611 NEF previews at 3872x2592)
# without ever admitting a genuinely reduced one - the next size down in the
# survey is a 1024x683 preview of a 3900x2611 file, which is 74% off.
PREVIEW_TOLERANCE = 0.02

# LibRaw flip values that TRANSPOSE the image. dcraw's convention: 0 = none,
# 3 = 180 degrees (dimensions unchanged), 5 and 6 = the two 90-degree rotations.
# Verified against rawpy: postprocess(user_flip=5|6) swaps the output shape
# relative to sizes.iwidth/iheight, and 0|3 leave it alone - i.e. iwidth/iheight
# are PRE-flip and this swap is not optional.
_FLIP_TRANSPOSES = (5, 6)

_EXIF_IFD = 0x8769
_GPS_IFD  = 0x8825


def is_raw(path):
    """True when `path`'s extension is one this module handles."""
    return os.path.splitext(path)[1].lower() in RAW_EXTS


def available():
    """True when rawpy (LibRaw) can actually be imported. Callers use this to
    report a clear 'RAW support is not installed' instead of failing per file."""
    try:
        import rawpy                               # noqa: F401
        return True
    except Exception:                              # noqa: BLE001
        return False


# ─────────────────────────────────────────────
#  PURE HELPERS  (no rawpy, no Pillow - unit-tested without either)
# ─────────────────────────────────────────────

def upright_size(iwidth, iheight, flip):
    """The dimensions the rendered picture will actually have.

    LibRaw reports `sizes.iwidth`/`iheight` BEFORE `sizes.flip` is applied, so a
    portrait shot off a landscape sensor reports landscape numbers. Every skip and
    target calculation downstream is about the picture a user will see, so the
    swap belongs here, once, rather than at each of them.
    """
    if flip in _FLIP_TRANSPOSES:
        return int(iheight), int(iwidth)
    return int(iwidth), int(iheight)


def preview_is_full_size(pw, ph, tw, th, tolerance=PREVIEW_TOLERANCE):
    """True when a (pw x ph) preview is essentially the whole (tw x th) image.

    Orientation-agnostic on purpose: a preview may be stored pre- or post-
    rotation depending on the manufacturer, and a rotated full-size preview is
    still a full-size preview.
    """
    if not (pw and ph and tw and th):
        return False

    def close(a, b, c, d):
        return abs(a - c) <= tolerance * c and abs(b - d) <= tolerance * d

    return close(pw, ph, tw, th) or close(pw, ph, th, tw)


def render_name(src_name):
    """The output filename for a RAW source: `<stem>_raw.jpg`.

    Output is `.jpg` (#19 decision 3): the app exists to make old photos display
    natively on monitors and TVs, not to produce a mastering format.

    The `_raw` part is NOT decoration, and it is why this is a function rather
    than an f-string at the call site. Every other format keeps its stem, but
    shooting RAW+JPEG is ordinary, and `IMG_1234.CR2` and `IMG_1234.JPG` in one
    folder both want to become `IMG_1234.jpg` in the mirrored output tree. Two
    sources mapping to one output is not a crash: the first one processed wins
    and the second is silently counted as "already upscaled", with the film strip
    and the lineage row pointing at a file that came from the other source. A
    suffix that is always present costs nothing and cannot collide.
    """
    return os.path.splitext(os.path.basename(src_name))[0] + "_raw.jpg"


def is_render_name(name):
    """True when `name` looks like a file this module produced. Used by the tools
    that have to recognise our own output without a database."""
    return os.path.basename(name).lower().endswith("_raw.jpg")


# ─────────────────────────────────────────────
#  HEADER READ  (no demosaic, no thumb unpack)
# ─────────────────────────────────────────────

def raw_dimensions(path):
    """
    (width, height) of the picture `path` represents, upright, or (0, 0).

    Header-only: LibRaw parses the metadata on open, so this costs no demosaic
    and no thumbnail unpack, which is what lets the expensive decode sit AFTER
    the eligibility check.

    (0, 0) on any failure is deliberate and is the opposite of this module's
    usual instinct. `runner_common.get_image_dimensions` falls back to Pillow for
    anything it cannot parse, and for a RAW that fallback answers CONFIDENTLY AND
    WRONGLY - Pillow sniffs a CR2/NEF/DNG as a TIFF and hands back the size of
    the small preview in IFD 0. Measured on the survey set: 15 of 24 files report
    a wrong size that way, and not always an obviously wrong one (the 20D reads
    as 1536x1024, the 5D as 2496x1664 - plausible photo sizes that would upscale
    a thumbnail into a 4K file and never look wrong in a log). So an unreadable
    RAW must return (0, 0) and be reported unreadable, exactly like a corrupt
    JPEG, and must never reach Pillow.
    """
    try:
        import rawpy
        with rawpy.imread(path) as raw:
            s = raw.sizes
            return upright_size(s.iwidth, s.iheight, s.flip)
    except Exception as exc:                       # noqa: BLE001 (unreadable/absent)
        debug_log(f"raw_decode.raw_dimensions: {path}", exc=exc)
        return (0, 0)


# ─────────────────────────────────────────────
#  METADATA
# ─────────────────────────────────────────────

# Tags that tell a raw DEVELOPER how to turn sensor data into a picture: the
# colour matrices, the black/white levels, the linearisation table, the lens
# correction opcodes, the manufacturer's private blob. All of it is meaningless
# once the file IS a picture, and it is enormous - a DNG's block measures 79 KB
# (Pentax K10D), 138 KB (Nikon D80) and 317 KB (Adobe DNG Converter) against a
# JPEG APP1 ceiling of 64 KB. Before this strip, exif_copy correctly refused to
# write an oversized block and those files came out with NO metadata at all: not
# a truncated date, no date.
#
# The DNG spec owns everything from 50706 up, and none of it describes the
# photograph, so the whole range goes rather than a hand-kept list that a newer
# spec revision would silently outgrow. The handful below 50706 are the TIFF/EP
# raw tags with the same character.
_DNG_TAG_FLOOR = 50706
_RAW_DEVELOPMENT_TAGS = frozenset({
    37393,   # ImageNumber (sequence, not the photo)
    50341,   # PrintImageMatching
    50706, 50707, 50708, 50709, 50710, 50711,     # DNGVersion .. CFALayout
    50712, 50713, 50714, 50715, 50716, 50717, 50718,   # LinearizationTable .. levels
})


def _drop_development_tags(exif):
    """Remove the raw-development tags from an Exif object, in place."""
    try:
        for tag in list(dict(exif)):
            if tag >= _DNG_TAG_FLOOR or tag in _RAW_DEVELOPMENT_TAGS:
                del exif[tag]
    except Exception as exc:                       # noqa: BLE001
        debug_log("raw_decode._drop_development_tags", exc=exc)
    return exif


def _exif_from_bytes(blob):
    """A Pillow Exif object parsed from a raw APP1 block, or None."""
    if not blob:
        return None
    try:
        from PIL import Image
        exif = Image.Exif()
        exif.load(blob)                            # tolerates the "Exif\0\0" prefix
        if not dict(exif):
            return None
        exif.get_ifd(_EXIF_IFD)
        exif.get_ifd(_GPS_IFD)
        return exif
    except Exception as exc:                       # noqa: BLE001
        debug_log("raw_decode._exif_from_bytes", exc=exc)
        return None


def _exif_from_container(path):
    """The RAW container's OWN metadata block as BYTES, or None.

    Works for the TIFF/EP derivatives (DNG, CR2, NEF, PEF as far as Pillow's
    TIFF plugin goes) and simply fails for the rest; the caller merges whatever
    it gets. Read-only and header-only.

    Bytes, not an Exif object, and serialised INSIDE the `with`. Pillow's Exif
    reads large values (a MakerNote, a sub-IFD) lazily from the still-open file
    handle, so an object returned past the close is only partly alive and blows
    up later with "seek of closed file" - observed on a CR2, and the kind of
    failure that would show up as silently missing metadata on some cameras and
    not others.
    """
    try:
        from PIL import Image
        with Image.open(path) as img:
            exif = img.getexif()
            if not dict(exif):
                return None
            exif.get_ifd(_EXIF_IFD)
            exif.get_ifd(_GPS_IFD)
            _drop_development_tags(exif)
            return exif.tobytes()
    except Exception:                              # noqa: BLE001 (CR3/ORF/RW2/RAF/ARW/SRW)
        return None


def _merge_exif(primary, secondary):
    """`primary` filled in with every field it is MISSING from `secondary`.

    The same one rule exif_copy's backfill uses - "copy what is missing, keep
    what is present" - so neither module needs a per-field table and neither can
    undo what the other decided. Returns an Exif object, or None when both are
    empty.

    One Pillow trap worth knowing before "fixing" this: after `primary[ptr] =
    merged`, calling `primary.get_ifd(ptr)` still returns the PRE-merge dict.
    Pillow caches parsed sub-IFDs in `Exif._ifds` and does not invalidate that
    cache on assignment. The merge is nonetheless correct - `tobytes()` reads
    `_data`, which holds the merged dict - so verify this function by serialising
    it, never by reading `get_ifd` back.
    """
    if primary is None:
        return secondary
    if secondary is None:
        return primary
    try:
        have = dict(primary)
        for tag, value in dict(secondary).items():
            if tag not in have:
                primary[tag] = value
        for ptr in (_EXIF_IFD, _GPS_IFD):
            extra = dict(secondary.get_ifd(ptr))
            if not extra:
                continue
            merged = dict(primary.get_ifd(ptr))
            for tag, value in extra.items():
                if tag not in merged:
                    merged[tag] = value
            if merged:
                primary[ptr] = merged
    except Exception as exc:                       # noqa: BLE001
        debug_log("raw_decode._merge_exif", exc=exc)
    return primary


def _serialise(exif):
    """Exif object -> raw bytes, or None. The block is handed on to exif_copy,
    which does the sanitising (Orientation, pixel dimensions, structural tags),
    so nothing is corrected here."""
    if exif is None:
        return None
    try:
        return exif.tobytes()
    except Exception as exc:                       # noqa: BLE001
        debug_log("raw_decode._serialise", exc=exc)
        return None


# ─────────────────────────────────────────────
#  RENDER
# ─────────────────────────────────────────────

class RawRender:
    """What `render()` produces.

    image  - a PIL RGB Image, upright (LibRaw's flip already applied).
    exif   - the merged source metadata as raw bytes, or None. NOT sanitised:
             the caller passes it through exif_copy, which owns the Orientation
             and pixel-dimension corrections for every format.
    how    - "preview" (S2) or "demosaic" (S1), for the log. Which path a file
             took is the single most useful thing to have recorded when someone
             later asks why one photo looks different from another.
    """

    __slots__ = ("image", "exif", "how")

    def __init__(self, image, exif, how):
        self.image = image
        self.exif  = exif
        self.how   = how

    @property
    def size(self):
        return self.image.size


def _preview(raw):
    """(PIL image, exif bytes, (w, h)) for the embedded preview, or (None, None,
    (0, 0)). A bitmap preview yields pixels but no metadata; a missing one raises
    inside LibRaw, which is why this is wrapped."""
    try:
        import rawpy
        thumb = raw.extract_thumb()
    except Exception:                              # noqa: BLE001 (no preview at all)
        return None, None, (0, 0)
    try:
        from PIL import Image
        if thumb.format == rawpy.ThumbFormat.JPEG:
            img = Image.open(io.BytesIO(thumb.data))
            img.load()                             # detach from the BytesIO
            blob = img.info.get("exif")
            return img.convert("RGB"), blob, img.size
        # ThumbFormat.BITMAP: an ndarray (h, w, c) backed by LibRaw's own memory,
        # and no metadata with it. The .copy() is NOT decoration: Pillow's
        # convert("RGB") returns `self` when the mode already matches, so without
        # it the image would still be aliasing a buffer that dies when the caller's
        # `with rawpy.imread(...)` block exits.
        arr = thumb.data
        return (Image.fromarray(arr).convert("RGB").copy(), None,
                (int(arr.shape[1]), int(arr.shape[0])))
    except Exception as exc:                       # noqa: BLE001
        debug_log("raw_decode._preview", exc=exc)
        return None, None, (0, 0)


def render(path, prefer_preview=True):
    """
    Render `path` to a `RawRender`. Raises on a RAW that cannot be read at all -
    the caller treats that exactly like a corrupt JPEG.

    `prefer_preview=False` forces the demosaic path; it exists for testing and
    for a user who ever needs to bypass a camera's own bad preview, not as a
    setting.
    """
    import rawpy
    from PIL import Image

    with rawpy.imread(path) as raw:
        s = raw.sizes
        tw, th = int(s.iwidth), int(s.iheight)
        flip   = int(s.flip)

        pv_img, pv_exif, (pw, ph) = _preview(raw)

        use_preview = (prefer_preview and pv_img is not None
                       and preview_is_full_size(pw, ph, tw, th))
        if use_preview:
            # The preview is stored in the camera's own output orientation, so
            # unlike the demosaic path there is no flip left to apply.
            image, how = pv_img, "preview"
        else:
            # LibRaw's defaults, stated rather than tuned: camera white balance
            # (what the camera would have used), sRGB, 8 bits, and the flip
            # applied - i.e. the camera's rendering intent, not ours.
            arr = raw.postprocess(use_camera_wb=True, no_auto_bright=True,
                                  output_bps=8)
            image, how = Image.fromarray(arr).convert("RGB"), "demosaic"

        container_exif = _exif_from_bytes(_exif_from_container(path))
        preview_exif   = _drop_development_tags(_exif_from_bytes(pv_exif)) \
            if pv_exif else None
        # RAW block first: where both exist it is the richer one (DateTimeOriginal
        # and GPS live there and are absent from every full-size preview in the
        # survey). Where it does not exist - CR3, ORF, RW2, RAF, ARW, SRW - the
        # preview's thinner block is all there is, and it still carries the date,
        # the exposure triple and the ISO.
        exif = _serialise(_merge_exif(container_exif, preview_exif))

    # Both paths come out upright already - postprocess() applies `flip` itself,
    # and a camera's preview is written in the camera's output orientation - so
    # `flip` is read here only for upright_size()'s benefit, never applied twice.
    return RawRender(image, exif, how)


def thumbnail(path, box=512):
    """A small PIL RGB image for `path`, or None. For the GUI's thumbnail wall.

    Uses the embedded preview at WHATEVER size it happens to be - the full-size
    test that governs `render()` is irrelevant here, because a 160x120 preview is
    still a perfectly good 150 px cell. Never demosaics: a wall of 200 CR2s would
    otherwise cost a minute of CPU to draw. Without this the strip falls into its
    generic decode failure, which retries four times with sleeps and then leaves
    the cell blank, so a folder of negatives drew as an empty grid.
    """
    try:
        import rawpy
        from PIL import Image
        with rawpy.imread(path) as raw:
            img, _exif, _size = _preview(raw)
        if img is None:
            return None
        img.thumbnail((box, box), Image.LANCZOS)
        return img
    except Exception as exc:                       # noqa: BLE001
        debug_log(f"raw_decode.thumbnail: {path}", exc=exc)
        return None


def describe(render_result, upright):
    """One log-friendly phrase for how a RAW became a picture."""
    if render_result.how == "preview":
        return f"RAW: camera preview {upright[0]}x{upright[1]}px"
    return f"RAW: demosaic {upright[0]}x{upright[1]}px"
