"""
exif_copy.py
------------
Carry a photo's metadata from the original into its upscaled copy (roadmap #13).

The upscale engine writes the result tensor with a bare `img.save(...)`, so until
this module existed **every upscaled image lost all metadata**: capture date,
camera make and model, lens, exposure, GPS, copyright, ratings. For a tool whose
purpose is reviving a personal photo collection, DateTimeOriginal is the painful
one - the upscaled copy sorts by file date, and once Conciliation replaces the
original the capture date is gone for good.

Two entry points, matching the two halves of #13:

  * exif_for_upscaled()  - 13a: the metadata block to write onto a NEW upscaled
                           file, read from its source and sanitised.
  * backfill()           - 13b: repair an ALREADY-upscaled file at conciliation
                           time, which is the last moment both files exist.

Pillow only, on purpose. `Image.getexif()` reads a metadata block out of JPEG,
WebP, PNG (eXIf chunk) and TIFF alike, and `Exif.tobytes()` writes one back for
all four with the sub-IFDs (Exif, GPS) intact - so there is one code path instead
of a per-format table, and no piexif dependency for the read side. piexif is used
only in backfill(), and only for JPEG, because `piexif.insert` patches the APP1
segment and leaves the compressed scan data byte-for-byte identical: Conciliation
must never re-encode a file that is about to become the only copy.

Fail-safe throughout: every public function swallows its own errors and returns
"nothing to do" rather than raising. Losing metadata is a disappointment; failing
an upscale, or aborting an archive/delete halfway, is damage.
"""

import os

try:
    from debug_log import debug_log
except Exception:                                  # noqa: BLE001 (old install)
    def debug_log(*_a, **_k):
        pass


# Destination extensions Pillow can carry a metadata block into. BMP is absent
# deliberately: Pillow accepts `exif=` for it and silently writes nothing, so
# listing it would report a copy that did not happen. (Measured, Pillow 12.)
EXIF_CAPABLE_EXTS = (".jpg", ".jpeg", ".webp", ".png", ".tif", ".tiff")

# Formats whose metadata can be repaired IN PLACE with no loss, for 13b:
#   JPEG - piexif.insert rewrites only the APP1 segment (scan data untouched).
#   PNG  - lossless by definition, so a full Pillow re-save costs nothing.
# WebP and TIFF are absent on purpose: our WebP output is lossy q95, so a re-save
# would spend a generation of quality to add a date, and re-saving a TIFF through
# Pillow can silently change its compression. Those are counted and named instead.
BACKFILL_FORMATS = ("JPEG", "MPO", "PNG")

# EXIF sub-IFD pointers. Their VALUES in the top-level dict are byte offsets that
# only mean something inside the file they came from, so they are never copied as
# numbers: Pillow re-serialises a sub-IFD when the pointer tag holds a dict.
_EXIF_IFD = 0x8769
_GPS_IFD  = 0x8825
_INTEROP  = 0xA005

# Tags that describe how the SOURCE FILE's bytes are laid out, not the photograph.
# Harmless-looking until the source is a TIFF, whose 0th IFD carries all of them:
# copied verbatim into a JPEG they describe a strip/tile layout that does not
# exist there. (Measured: a Pillow-saved TIFF hands back 256, 257, 258, 259, 262,
# 273, 277, 278, 279 and 284 alongside the real metadata.)
_STRUCTURAL = frozenset({
    256,   # ImageWidth
    257,   # ImageLength
    258,   # BitsPerSample
    259,   # Compression
    262,   # PhotometricInterpretation
    273,   # StripOffsets
    277,   # SamplesPerPixel
    278,   # RowsPerStrip
    279,   # StripByteCounts
    284,   # PlanarConfiguration
    317,   # Predictor
    320,   # ColorMap
    322, 323,        # TileWidth / TileLength
    324, 325,        # TileOffsets / TileByteCounts
    330,   # SubIFDs
    339,   # SampleFormat
    513, 514,        # JPEGInterchangeFormat(+Length): the embedded thumbnail
    530,   # YCbCrSubSampling
})

_ORIENTATION      = 274
_PIXEL_X_DIM      = 40962
_PIXEL_Y_DIM      = 40963
_MAKER_NOTE       = 37500

# Never copied at the top level: the layout tags above, the pointers (offsets),
# and Orientation (see the note in exif_for_upscaled - the pixels are already
# upright, so the source's value would rotate them a second time).
_NEVER_COPY_0TH = _STRUCTURAL | {_ORIENTATION, _EXIF_IFD, _GPS_IFD}

# Same idea inside the Exif sub-IFD. The Interop pointer is dropped rather than
# followed: it holds one tag ("R98"), and a stale offset is worse than no tag.
_NEVER_COPY_SUB = frozenset({_INTEROP})

# A JPEG's APP1 segment cannot exceed 64 KB, and Pillow raises "EXIF data is too
# long" rather than truncating (measured). A camera MakerNote is what gets a block
# there, and it is also the one part that does not survive being moved into
# another file anyway (its internal offsets are file-relative), so it is the first
# thing dropped when the block will not fit.
_JPEG_EXIF_LIMIT = 60000


def _open_exif(path):
    """Return (Exif object, True) for `path`, or (None, False). Header-only: the
    pixels are never decoded."""
    try:
        from PIL import Image
        with Image.open(path) as img:
            exif = img.getexif()
            # Force the sub-IFDs to parse while the file is still open; the Exif
            # object reads them lazily from the file handle otherwise.
            exif.get_ifd(_EXIF_IFD)
            exif.get_ifd(_GPS_IFD)
            return exif, True
    except Exception as exc:                       # noqa: BLE001 (unreadable/absent)
        debug_log(f"exif_copy._open_exif: {path}", exc=exc)
        return None, False


def _is_jpeg_ext(ext):
    return (ext or "").lower() in (".jpg", ".jpeg")


def _serialise(exif, ext):
    """Exif object -> bytes, or None when there is nothing worth writing.

    Shrinks an oversized block for a JPEG destination by dropping the MakerNote
    first (keeping the date, camera and GPS) and giving up entirely only if it
    still does not fit.
    """
    try:
        if not dict(exif):
            return None                            # an empty block is not metadata
        blob = exif.tobytes()
        if _is_jpeg_ext(ext) and len(blob) > _JPEG_EXIF_LIMIT:
            sub = dict(exif.get_ifd(_EXIF_IFD))
            if sub.pop(_MAKER_NOTE, None) is not None:
                exif[_EXIF_IFD] = sub
                blob = exif.tobytes()
            if len(blob) > _JPEG_EXIF_LIMIT:
                return None                        # cannot fit: write no metadata
        return blob
    except Exception as exc:                       # noqa: BLE001
        debug_log("exif_copy._serialise", exc=exc)
        return None


# ─────────────────────────────────────────────
#  13a: metadata for a NEWLY upscaled file
# ─────────────────────────────────────────────

def exif_for_upscaled(src_path, dest_path, size=None):
    """
    The metadata block to write onto the upscaled copy of `src_path`, or None.

    `size` is the output's (width, height) when known, so the pixel-describing
    tags can be corrected instead of left describing the small original.

    Two corrections are not optional:

    * **Orientation is forced to 1.** The pipeline has ALREADY applied it -
      `_load_image` runs `ImageOps.exif_transpose`, and auto-straighten may have
      rotated a temp copy on top of that - so the output pixels are upright.
      Copying the source's Orientation verbatim would make every viewer rotate an
      already-upright photo a second time.
    * **The embedded thumbnail is dropped.** It is stale (it shows the old image
      at the old size). Pillow's `Exif.tobytes()` does not serialise IFD1 at all,
      so this happens for free; the JPEGInterchangeFormat tags are in
      `_STRUCTURAL` so a source that kept them in the 0th IFD cannot smuggle a
      dangling offset through either.
    """
    exif, ok = _open_exif(src_path)
    if not ok or exif is None:
        return None
    try:
        for tag in list(dict(exif)):
            if tag in _STRUCTURAL:
                del exif[tag]

        # A source with nothing to say must produce nothing. Checked AFTER the
        # structural strip (a plain TIFF's whole 0th IFD is layout tags) and
        # BEFORE anything is added below, so an upscaled copy of a metadata-free
        # photo does not gain a synthetic three-tag block that says only
        # "upright, 3840x2160" - which reads as metadata but is not.
        if not dict(exif) and not exif.get_ifd(_EXIF_IFD) and not exif.get_ifd(_GPS_IFD):
            return None
        exif[_ORIENTATION] = 1

        sub = dict(exif.get_ifd(_EXIF_IFD))
        for tag in _NEVER_COPY_SUB:
            sub.pop(tag, None)
        if size:
            sub[_PIXEL_X_DIM], sub[_PIXEL_Y_DIM] = int(size[0]), int(size[1])
        else:
            # Better to say nothing than to describe the original's dimensions.
            sub.pop(_PIXEL_X_DIM, None)
            sub.pop(_PIXEL_Y_DIM, None)
        if sub:
            exif[_EXIF_IFD] = sub

        gps = dict(exif.get_ifd(_GPS_IFD))
        if gps:
            exif[_GPS_IFD] = gps
    except Exception as exc:                       # noqa: BLE001
        debug_log("exif_copy.exif_for_upscaled", exc=exc)
        return None
    return _serialise(exif, os.path.splitext(dest_path)[1])


def save_kwargs(dest_path, blob):
    """`{"exif": blob}` when `dest_path`'s format can carry one, else `{}`, so a
    caller can splat this into `Image.save` without a per-format branch."""
    if not blob:
        return {}
    if os.path.splitext(dest_path)[1].lower() not in EXIF_CAPABLE_EXTS:
        return {}
    return {"exif": blob}


# ─────────────────────────────────────────────
#  13b: repair an ALREADY-upscaled file
# ─────────────────────────────────────────────

def _merged_exif(orig_path, proc_path):
    """
    Build the processed file's metadata with every field it is MISSING filled in
    from the original. Returns (Exif object, tags added) or (None, 0).

    "Copy what is missing, keep what is present" is deliberately one rule rather
    than a per-field table. Any field the processed file already carries got there
    because something set it on purpose: the pipeline's normalised Orientation,
    the ImageDescription Tag & Rename wrote, corrected pixel dimensions. Never
    overwriting means this pass can never undo the work of the tool that ran
    before it, and the rule needs no maintenance when a later feature starts
    writing some new tag.
    """
    s_exif, s_ok = _open_exif(orig_path)
    p_exif, p_ok = _open_exif(proc_path)
    if not (s_ok and p_ok) or s_exif is None or p_exif is None:
        return None, 0
    try:
        added = 0
        for tag, value in dict(s_exif).items():
            if tag in _NEVER_COPY_0TH or tag in dict(p_exif):
                continue
            p_exif[tag] = value
            added += 1

        for ptr in (_EXIF_IFD, _GPS_IFD):
            s_sub = dict(s_exif.get_ifd(ptr))
            if not s_sub:
                continue
            p_sub = dict(p_exif.get_ifd(ptr))
            changed = False
            for tag, value in s_sub.items():
                if tag in _NEVER_COPY_SUB or tag in p_sub:
                    continue
                # The processed file's pixel dimensions describe the UPSCALED
                # image; the original's describe a smaller one. Absent is honest,
                # the source's numbers are a lie, so this pair is never copied.
                if ptr == _EXIF_IFD and tag in (_PIXEL_X_DIM, _PIXEL_Y_DIM):
                    continue
                p_sub[tag] = value
                added += 1
                changed = True
            if changed or p_sub:
                p_exif[ptr] = p_sub
        return p_exif, added
    except Exception as exc:                       # noqa: BLE001
        debug_log("exif_copy._merged_exif", exc=exc)
        return None, 0


def _proc_format(path):
    """The processed file's true container format (Pillow name), read from its
    content rather than its extension - an upscaled tree can hold a .png that
    really is a PNG and never a JPEG. None if it cannot be opened."""
    try:
        from PIL import Image
        with Image.open(path) as im:
            return (im.format or "").upper()
    except Exception:                              # noqa: BLE001
        return None


def pending_backfill(orig_path, proc_path):
    """
    How many fields `backfill` WOULD copy, without writing anything. Used by
    Conciliation's Scan/Preview, which must keep its promise of touching nothing.
    0 means "already complete, or not repairable", so the preview count and the
    run's count agree.
    """
    if _proc_format(proc_path) not in BACKFILL_FORMATS:
        return 0
    _exif, added = _merged_exif(orig_path, proc_path)
    return added


def backfill(orig_path, proc_path):
    """
    Copy the fields `proc_path` is missing from `orig_path`, in place.

    Returns (fields copied, reason skipped). A non-None reason is plain English
    and safe to show a user; it never means the file was damaged, only that
    nothing was written.

    NEVER RAISES. This runs inside Conciliation, immediately before an original
    is archived or deleted, and a bonus metadata pass must not be able to abort
    the file operation it precedes.
    """
    fmt = _proc_format(proc_path)
    if fmt is None:
        return 0, "unreadable"
    if fmt not in BACKFILL_FORMATS:
        return 0, f"{fmt} cannot be repaired without re-encoding it"

    exif, added = _merged_exif(orig_path, proc_path)
    if exif is None or added == 0:
        return 0, None                             # nothing missing: idempotent

    ext = os.path.splitext(proc_path)[1]
    blob = _serialise(exif, ".jpg" if fmt in ("JPEG", "MPO") else ext)
    if not blob:
        return 0, "metadata block too large for the format"

    try:
        if fmt in ("JPEG", "MPO"):
            # Lossless: rewrites only the APP1 segment, leaving the compressed
            # scan data byte-for-byte identical (the same call Tag & Rename uses).
            import piexif
            piexif.insert(blob, proc_path)
        else:
            # PNG is a lossless format, so a full re-save costs no quality. Atomic,
            # because the very next step archives or deletes the only other copy.
            from PIL import Image
            tmp = proc_path + ".tmp.png"
            try:
                with Image.open(proc_path) as im:
                    im.load()
                    im.save(tmp, "png", exif=blob)
                os.replace(tmp, proc_path)
            except Exception:
                if os.path.exists(tmp):
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
                raise
    except Exception as exc:                       # noqa: BLE001
        debug_log(f"exif_copy.backfill: {proc_path}", exc=exc)
        return 0, f"{type(exc).__name__}"
    return added, None
