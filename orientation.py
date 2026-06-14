"""
Image orientation detection and correction.

Uses a small pretrained CNN (ternaus/check_orientation, a resnext50_32x4d with
a 4-class head over [0, 90, 180, 270] degrees) to decide whether a photo was
taken with the camera held sideways, and if so, losslessly* rotates it upright.

* Rotation is a pure 90-degree pixel transpose (no resampling); the only loss is
  the JPEG re-encode, done at quality=95 to match tag_and_rename's _save_with_exif.

The model, timm and torch are imported lazily so that importing this module (and
running Tag & Rename with straightening disabled) stays cheap. The ~82 MB weights
download once into the torch hub cache on first use.

Design notes (validated on the 87-image sample, see project history):
  * Only 90 and 270 predictions trigger a rotation. 180 (upside-down) is the
    hardest, least-common case and the main source of false positives, so it is
    treated as "leave alone + log".
  * A confidence floor (default 0.9) filters the remaining weak calls. Anything
    below it is left untouched and logged for manual review — the feature fails
    SAFE: an ambiguous photo is never rotated.

Class -> correction mapping (verified by calibration, see _CORRECT):
  90  -> the photo is rotated left; rotate it 90 deg CLOCKWISE  to fix.
  270 -> the photo is rotated right; rotate it 90 deg COUNTER-CW to fix.
"""

_WEIGHTS_URL = (
    "https://github.com/ternaus/check_orientation/releases/download/"
    "v0.0.3/2020-11-16_resnext50_32x4d.zip"
)
_CLASSES = [0, 90, 180, 270]
_MEAN = (0.485, 0.456, 0.406)
_STD = (0.229, 0.224, 0.225)

# CNN class -> (PIL transpose to make upright, clockwise degrees that represents).
# Resolved lazily once PIL is imported (Image.Transpose constants).
_CORRECT = None      # {90: (transpose_const, +90), 270: (transpose_const, -90)}
_MODEL = None        # (model, device) once loaded


def _ensure_correct_map():
    global _CORRECT
    if _CORRECT is None:
        from PIL import Image
        T = Image.Transpose
        # ROTATE_n in PIL is COUNTER-clockwise. To rotate a class-90 image 90 deg
        # clockwise we apply ROTATE_270 (= 270 CCW = 90 CW). Class-270 needs the
        # opposite, ROTATE_90.  (+90 = clockwise, -90 = counter-clockwise.)
        _CORRECT = {
            90:  (T.ROTATE_270, 90),
            270: (T.ROTATE_90, -90),
        }
    return _CORRECT


def is_available():
    """True if torch + timm are importable (i.e. straightening can run)."""
    import importlib.util as u
    return all(u.find_spec(m) for m in ("torch", "timm"))


def _get_model():
    global _MODEL
    if _MODEL is not None:
        return _MODEL
    import torch
    import torch.nn as nn
    from timm import create_model
    m = create_model("resnext50_32x4d", pretrained=False, num_classes=4)
    sd = torch.hub.load_state_dict_from_url(
        _WEIGHTS_URL, progress=False, map_location="cpu"
    )
    sd = sd.get("state_dict", sd)
    sd = {k.replace("model.", "", 1): v for k, v in sd.items()}
    m.load_state_dict(sd)
    m.eval()
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    _MODEL = (nn.Sequential(m, nn.Softmax(dim=1)).to(dev), dev)
    return _MODEL


def _preprocess_pil(pil_img):
    """Resize longest side to 224, pad to 224x224 (black), ImageNet-normalize.

    Matches the validated test preprocessing exactly so accuracy is preserved.
    """
    import numpy as np
    import torch
    img = np.asarray(pil_img.convert("RGB"))
    h, w = img.shape[:2]
    s = 224 / max(h, w)
    nh, nw = max(1, round(h * s)), max(1, round(w * s))
    # PIL for the resize keeps this dependency-light (no cv2 needed at call time).
    small = pil_img.convert("RGB").resize((nw, nh))
    small = np.asarray(small)
    canvas = np.zeros((224, 224, 3), dtype=np.uint8)
    top, left = (224 - nh) // 2, (224 - nw) // 2
    canvas[top:top + nh, left:left + nw] = small
    x = canvas.astype(np.float32) / 255.0
    x = (x - np.array(_MEAN, dtype=np.float32)) / np.array(_STD, dtype=np.float32)
    return torch.from_numpy(x.transpose(2, 0, 1)).unsqueeze(0)


def analyse(path):
    """Return (degrees, confidence): degrees in {0,90,180,270}, confidence 0..1."""
    import torch
    from PIL import Image
    model, dev = _get_model()
    x = _preprocess_pil(Image.open(path)).to(dev)
    with torch.no_grad():
        p = model(x)[0].cpu().numpy()
    cls = int(p.argmax())
    return _CLASSES[cls], float(p[cls])


def should_rotate(degrees, confidence, min_confidence):
    """Policy: rotate only confident 90/270 calls; skip 0, 180 and weak calls."""
    return degrees in (90, 270) and confidence >= min_confidence


def _rotate_file(path, transpose_const):
    """Apply a 90-degree transpose to the file in place, re-encoding JPEG at q95
    and clearing the EXIF orientation flag so viewers don't double-rotate."""
    from PIL import Image
    img = Image.open(path)
    fmt = (img.format or "").upper()
    rotated = img.transpose(transpose_const)
    ext = path.lower().rsplit(".", 1)[-1]
    if ext in ("jpg", "jpeg") or fmt in ("JPEG", "MPO"):
        # Drop any orientation tag; we've physically corrected the pixels.
        try:
            import piexif
            exif = piexif.load(path)
            exif.get("0th", {}).pop(piexif.ImageIFD.Orientation, None)
            exif_bytes = piexif.dump(exif)
            rotated.save(path, "jpeg", exif=exif_bytes, quality=95, subsampling=0)
        except Exception:
            rotated.save(path, "jpeg", quality=95, subsampling=0)
    else:
        rotated.save(path)


def straighten(path, degrees):
    """Rotate `path` upright for a given CNN class. Returns clockwise degrees
    applied (+90 or -90), to be stored for undo. Caller must have checked policy.
    """
    transpose_const, cw = _ensure_correct_map()[degrees]
    _rotate_file(path, transpose_const)
    return cw


def unrotate(path, net_cw_degrees):
    """Undo a previous straighten() by applying the inverse transpose.

    net_cw_degrees is the total clockwise rotation that was applied (any
    multiple of 90, e.g. +90, -90, 270). The inverse is applied to restore the
    original pixels. A net of 0 is a no-op.
    """
    net = net_cw_degrees % 360
    if net == 0:
        return
    from PIL import Image
    T = Image.Transpose
    # Inverse of a net CW rotation: ROTATE_n is n degrees COUNTER-clockwise.
    inverse = {90: T.ROTATE_90, 180: T.ROTATE_180, 270: T.ROTATE_270}[net]
    _rotate_file(path, inverse)
