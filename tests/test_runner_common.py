"""
The shared runner scaffolding (recommendations item 5): duration formatting, the
@@TBX@@ event emitter, OOM classification, remote-pod-stopped, and the
header-based image-size reader (the superset that replaced the two drifted
copies). The image tests generate real files with Pillow where available and
fall back to hand-written headers so the fast path is covered even torch/Pillow-
free. Every runner now re-exports these under its old names, so the same
functions back all four runners.
"""

import io
import os
import struct

import pytest

import runner_common as rc


# ── duration formatting ──────────────────────────────────────────────────────

@pytest.mark.parametrize("secs,expected", [
    (0, "0s"), (5, "5s"), (65, "1m 05s"), (600, "10m 00s"),
    (3725, "1h 02m 05s"), (86461, "24h 01m 01s"),
])
def test_fmt_duration(secs, expected):
    assert rc.fmt_duration(secs) == expected


@pytest.mark.parametrize("secs,expected", [(0, "00:00"), (75, "01:15"), (3599, "59:59")])
def test_fmt_mmss(secs, expected):
    assert rc.fmt_mmss(secs) == expected


@pytest.mark.parametrize("secs,expected", [(0, "00:00:00"), (3725, "01:02:05"), (90061, "25:01:01")])
def test_fmt_hhmmss(secs, expected):
    assert rc.fmt_hhmmss(secs) == expected


def test_fmt_truncates_fractional_seconds():
    assert rc.fmt_mmss(75.9) == "01:15"
    assert rc.fmt_duration(65.9) == "1m 05s"


# ── the same functions back every runner (re-export check) ───────────────────

def test_runners_reexport_the_shared_helpers():
    import batch_upscale, tag_and_rename, conciliate
    assert batch_upscale.fmt_duration is rc.fmt_duration
    assert batch_upscale.get_image_dimensions is rc.get_image_dimensions
    assert batch_upscale._is_oom_error is rc.is_oom_error
    assert batch_upscale._gui_event is rc.gui_event
    assert tag_and_rename.get_image_dimensions is rc.get_image_dimensions
    assert tag_and_rename.fmt_mmss is rc.fmt_mmss
    assert conciliate._gui_event is rc.gui_event
    assert conciliate.GUI_MARKER == rc.GUI_MARKER


# ── OOM classification ───────────────────────────────────────────────────────

@pytest.mark.parametrize("msg", [
    "CUDA out of memory. Tried to allocate 2 GB",
    "torch.OutOfMemoryError: ...",
    "CUDA_ERROR_OUT_OF_MEMORY",
    "CUDA error: an illegal memory access",
])
def test_is_oom_error_true(msg):
    assert rc.is_oom_error(RuntimeError(msg)) is True


@pytest.mark.parametrize("msg", [
    "Connection refused", "HTTP 400 Bad Request", "file not found", "",
])
def test_is_oom_error_false(msg):
    assert rc.is_oom_error(RuntimeError(msg)) is False


def test_is_oom_error_uses_type_name():
    # The classifier folds in the exception type name, so OutOfMemoryError as a
    # bare type (empty message) is still caught.
    class OutOfMemoryError(Exception):
        pass
    assert rc.is_oom_error(OutOfMemoryError()) is True


# ── remote_pod_stopped ───────────────────────────────────────────────────────

def test_remote_pod_stopped_none_session():
    assert rc.remote_pod_stopped(None) is False


class _FakeSession:
    api_key = "k"
    pod_id = "p"


def test_remote_pod_stopped_reads_status(monkeypatch):
    import runpod_client as rp
    # A pod that is gone/exited/terminated -> "stopped"; a running one -> False.
    monkeypatch.setattr(rp, "pod_status", lambda k, p: rp.STATUS_EXITED)
    assert rc.remote_pod_stopped(_FakeSession()) is True
    monkeypatch.setattr(rp, "pod_status", lambda k, p: None)
    assert rc.remote_pod_stopped(_FakeSession()) is True
    monkeypatch.setattr(rp, "pod_status", lambda k, p: "RUNNING")
    assert rc.remote_pod_stopped(_FakeSession()) is False


def test_remote_pod_stopped_is_fail_safe(monkeypatch):
    import runpod_client as rp
    def boom(k, p):
        raise RuntimeError("network down")
    monkeypatch.setattr(rp, "pod_status", boom)
    # An error reading status must never look like a stop (transient blip path).
    assert rc.remote_pod_stopped(_FakeSession()) is False


# ── gui_event wire format ────────────────────────────────────────────────────

class _CapturingStdout:
    def __init__(self):
        self.buf = io.StringIO()
    def write(self, s):
        self.buf.write(s)
    def flush(self):
        pass


def test_gui_event_writes_marker_line(monkeypatch):
    import sys
    cap = _CapturingStdout()
    monkeypatch.setattr(sys, "stdout", cap)
    monkeypatch.setattr(rc, "GUI_MODE", True)
    rc.gui_event("RESULT", '["a.jpg","ok"]')
    assert cap.buf.getvalue() == '@@TBX@@RESULT|["a.jpg","ok"]\n'


def test_gui_event_targets_raw_stream_when_wrapped(monkeypatch):
    # tag_and_rename wraps stdout in a tee exposing .raw; the marker must go to
    # .raw so it bypasses the on-disk session log.
    import sys
    raw = _CapturingStdout()
    class _Tee:
        def __init__(self, raw):
            self.raw = raw
        def write(self, s):
            raise AssertionError("marker must not hit the tee's write()")
    monkeypatch.setattr(sys, "stdout", _Tee(raw))
    monkeypatch.setattr(rc, "GUI_MODE", True)
    rc.gui_event("IMG", "x.png")
    assert raw.buf.getvalue() == "@@TBX@@IMG|x.png\n"


def test_gui_event_noop_when_not_gui(monkeypatch):
    import sys
    cap = _CapturingStdout()
    monkeypatch.setattr(sys, "stdout", cap)
    monkeypatch.setattr(rc, "GUI_MODE", False)
    rc.gui_event("IMG", "x.png")
    assert cap.buf.getvalue() == ""


# ── image dimension reader ───────────────────────────────────────────────────

def _hand_png(w, h):
    return (b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR"
            + struct.pack(">I", w) + struct.pack(">I", h) + b"\x08\x02\x00\x00\x00")


def _hand_bmp(w, h):
    # 14-byte file header + BITMAPINFOHEADER with width/height at offset 18/22.
    return (b"BM" + b"\x00" * 16 + struct.pack("<I", w) + struct.pack("<I", h)
            + b"\x00" * 8)


def test_reads_png_header(tmp_path):
    p = tmp_path / "a.png"
    p.write_bytes(_hand_png(1920, 1080))
    assert rc.get_image_dimensions(str(p)) == (1920, 1080)


def test_reads_bmp_header(tmp_path):
    p = tmp_path / "a.bmp"
    p.write_bytes(_hand_bmp(640, 480))
    assert rc.get_image_dimensions(str(p)) == (640, 480)


def test_unreadable_returns_zero(tmp_path):
    p = tmp_path / "broken.png"
    p.write_bytes(b"not a real png")
    assert rc.get_image_dimensions(str(p)) == (0, 0)


def test_missing_file_returns_zero(tmp_path):
    assert rc.get_image_dimensions(str(tmp_path / "nope.jpg")) == (0, 0)


def test_reads_real_images_across_formats(tmp_path):
    Image = pytest.importorskip("PIL.Image")
    cases = {"c.jpg": (321, 123), "c.png": (200, 100),
             "c.bmp": (64, 48), "c.webp": (128, 96), "c.tif": (77, 55)}
    for name, (w, h) in cases.items():
        p = tmp_path / name
        Image.new("RGB", (w, h), (10, 20, 30)).save(str(p))
        assert rc.get_image_dimensions(str(p)) == (w, h), name


def test_pillow_fallback_for_progressive_jpeg(tmp_path):
    Image = pytest.importorskip("PIL.Image")
    p = tmp_path / "prog.jpg"
    Image.new("RGB", (300, 200), (5, 5, 5)).save(str(p), progressive=True)
    # Progressive JPEGs have no baseline SOF the fast path finds; the Pillow
    # fallback must still return the true size.
    assert rc.get_image_dimensions(str(p)) == (300, 200)
