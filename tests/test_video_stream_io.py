"""
Item 8: RemoteVideoEngine streams a segment in both directions instead of buffering the
whole thing in RAM. A 4K segment is commonly hundreds of MB, and the old code held the
whole input AND the whole output at once. Now the upload is sent straight from the file
(Content-Length set, no chunked encoding so the worker is unaffected) and the download is
written straight to disk (copyfileobj). These tests pin that behaviour with a faked
urlopen (no pod).
"""

import io
import os

import pytest

import remote_video_engine as rve


def _engine():
    eng = object.__new__(rve.RemoteVideoEngine)   # skip the tunnel-building __init__
    eng.local_port = 1
    return eng


def test_fetch_streams_to_disk_and_returns_bytes(tmp_path, monkeypatch):
    payload = b"UPSCALED-SEGMENT" * 100_000        # ~1.6 MB: crosses the 1 MB copy block
    monkeypatch.setattr(rve.urllib.request, "urlopen",
                        lambda url, timeout=None: io.BytesIO(payload))
    tmp = str(tmp_path / "seg.tmp.mp4")
    n = rve.RemoteVideoEngine._fetch(_engine(), "job1", tmp)
    assert n == len(payload)
    with open(tmp, "rb") as f:
        assert f.read() == payload                # streamed through intact


def test_submit_sets_content_length_and_streams_the_file(tmp_path, monkeypatch):
    src = tmp_path / "seg.mkv"
    src.write_bytes(b"x" * 4096)
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["req"] = req
        return io.BytesIO(b'{"id": "job1"}')

    monkeypatch.setattr(rve.urllib.request, "urlopen", fake_urlopen)
    jid = _engine()._submit(str(src), ".mkv", 1080, 5, 0, 0, None, "ffmpeg", True)
    assert jid == "job1"
    req = captured["req"]
    # Content-Length is explicit (so http.client streams the body without chunking)...
    assert req.get_header("Content-length") == "4096"
    # ...and the body is the open FILE OBJECT, not the whole bytes read into RAM.
    assert hasattr(req.data, "read")
    assert not isinstance(req.data, (bytes, bytearray))


def test_process_segment_end_to_end_streaming(tmp_path, monkeypatch):
    src = tmp_path / "seg.mkv"
    src.write_bytes(b"IN" * 1000)
    out_payload = b"OUT" * 60_000

    def fake_urlopen(req_or_url, timeout=None):
        url = req_or_url.full_url if hasattr(req_or_url, "full_url") else req_or_url
        if "/video/submit" in url:
            return io.BytesIO(b'{"id": "j"}')
        if "/video/status" in url:
            return io.BytesIO(b'{"state": "done", "seconds": 1.5, "frames_written": 7}')
        if "/video/fetch" in url:
            return io.BytesIO(out_payload)
        raise AssertionError(f"unexpected URL {url}")

    monkeypatch.setattr(rve.urllib.request, "urlopen", fake_urlopen)
    dest = str(tmp_path / "out.mp4")
    frames = _engine().process_segment(str(src), dest, resolution=1080, batch_size=5,
                                       chunk_size=0, poll_interval=0)
    assert frames == 7
    with open(dest, "rb") as f:
        assert f.read() == out_payload
    assert not os.path.exists(dest + ".tmp.mp4")   # tmp atomically renamed away


def test_process_segment_records_streamed_byte_count(tmp_path, monkeypatch):
    src = tmp_path / "seg.mkv"
    src.write_bytes(b"IN" * 10)
    out_payload = b"Z" * 12_345

    def fake_urlopen(req_or_url, timeout=None):
        url = req_or_url.full_url if hasattr(req_or_url, "full_url") else req_or_url
        if "/video/submit" in url:
            return io.BytesIO(b'{"id": "j"}')
        if "/video/status" in url:
            return io.BytesIO(b'{"state": "done", "seconds": 2, "frames_written": 3}')
        return io.BytesIO(out_payload)

    monkeypatch.setattr(rve.urllib.request, "urlopen", fake_urlopen)
    eng = _engine()
    eng.process_segment(str(src), str(tmp_path / "o.mp4"), resolution=1080,
                        batch_size=5, chunk_size=0, poll_interval=0)
    # last_phase["bytes"] comes from the streamed count, not len(out) held in RAM.
    assert eng.last_phase["bytes"] == len(out_payload)
    assert eng.last_segment_seconds == 2


# ── remote benchmark probe (section 22): submit /video/probe, poll, map the outcome ──

def test_probe_submit_streams_the_clip_and_returns_id(tmp_path, monkeypatch):
    src = tmp_path / "clip.mkv"
    src.write_bytes(b"c" * 4096)
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["clen"] = req.get_header("Content-length")
        captured["data"] = req.data
        return io.BytesIO(b'{"id": "p1"}')

    monkeypatch.setattr(rve.urllib.request, "urlopen", fake_urlopen)
    jid = _engine()._submit_probe(str(src), ".mkv", 1080, 9, -1)
    assert jid == "p1"
    assert "/video/probe" in captured["url"]
    assert "batch_size=9" in captured["url"] and "temporal_overlap=-1" in captured["url"]
    # streamed from the open file (Content-Length set, not read whole into RAM)
    assert captured["clen"] == "4096"
    assert hasattr(captured["data"], "read")


def test_probe_batch_ok_end_to_end(tmp_path, monkeypatch):
    src = tmp_path / "clip.mkv"
    src.write_bytes(b"C" * 4096)
    seq = iter([
        b'{"id": "p1"}',                                    # submit
        b'{"state": "running"}',                            # first poll: not done yet
        b'{"state": "done", "outcome": "ok", "resolved_batch": 9, '
        b'"resolved_overlap": 6, "frames_written": 37, "seconds": 100.0, '
        b'"peak_alloc_gb": 20.0, "peak_reserved_gb": 22.0}',
    ])
    monkeypatch.setattr(rve.urllib.request, "urlopen",
                        lambda req_or_url, timeout=None: io.BytesIO(next(seq)))
    res = _engine().probe_batch(str(src), str(tmp_path / "o.mp4"),
                                resolution=1080, batch=9, frames=37, poll_interval=0)
    assert res == {"outcome": "ok", "batch": 9, "overlap": 6, "frames": 37,
                   "seconds": 100.0, "peak_alloc_gb": 20.0, "peak_reserved_gb": 22.0}


def test_probe_batch_oom_is_a_result_not_a_raise(tmp_path, monkeypatch):
    src = tmp_path / "clip.mkv"
    src.write_bytes(b"C" * 128)
    seq = iter([b'{"id": "p2"}',
                b'{"state": "done", "outcome": "oom", "resolved_batch": 73, '
                b'"resolved_overlap": 12, "seconds": 30.0}'])
    monkeypatch.setattr(rve.urllib.request, "urlopen",
                        lambda req_or_url, timeout=None: io.BytesIO(next(seq)))
    res = _engine().probe_batch(str(src), str(tmp_path / "o.mp4"),
                                resolution=1080, batch=73, poll_interval=0)
    assert res["outcome"] == "oom" and res["batch"] == 73
    assert res["frames"] is None                           # an oom probe carries no frames


def test_probe_batch_stops_responsively(tmp_path, monkeypatch):
    src = tmp_path / "clip.mkv"
    src.write_bytes(b"C" * 128)
    # submit returns an id; the first poll sees should_stop -> 'stopped' (no pod-loss raise).
    monkeypatch.setattr(rve.urllib.request, "urlopen",
                        lambda req_or_url, timeout=None: io.BytesIO(b'{"id": "p3"}'))
    res = _engine().probe_batch(str(src), str(tmp_path / "o.mp4"),
                                resolution=1080, batch=9, poll_interval=0,
                                should_stop=lambda: True)
    assert res["outcome"] == "stopped"


def test_probe_await_raises_on_lost_job(tmp_path, monkeypatch):
    # A worker that forgot the job (restarted) is a POD loss, not a probe result -> raise.
    monkeypatch.setattr(rve.urllib.request, "urlopen",
                        lambda req_or_url, timeout=None: io.BytesIO(b'{"state": "unknown"}'))
    with pytest.raises(rve.RemoteVideoError):
        _engine()._await_probe("gone", 9, poll_interval=0)
