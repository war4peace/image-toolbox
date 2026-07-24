"""
Item 9 (runner side): adaptive batch tuning. On a multi-segment AUTO-batch video the
runner seeds the batch from the DB (keyed by card + output-MP bucket), lets the first
segment's measured suggestion refine + freeze it, and persists it for next time. Pure
helpers + DB round-trip always run; the end-to-end convergence needs ffmpeg (real split +
reassemble) so it skips cleanly where ffmpeg is absent.
"""

import os
import subprocess

import pytest

import db
import video_pipeline as vp
import batch_video_upscale as bv


@pytest.fixture
def db_conn(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_DIR", str(tmp_path / "db"))
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "db" / "cache.db"))
    monkeypatch.setattr(db, "_conn", None)
    monkeypatch.setattr(db, "import_legacy_json", lambda conn: None)
    conn = db.get_conn()
    yield conn
    conn.close()
    monkeypatch.setattr(db, "_conn", None)


def _have_ffmpeg():
    try:
        vp.find_ffmpeg()
        return True
    except Exception:
        return False


needs_ffmpeg = pytest.mark.skipif(not _have_ffmpeg(), reason="ffmpeg not available")


def _make_source(path, seconds=24, fps=30, size="320x240"):
    ffmpeg, _ = vp.find_ffmpeg()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    subprocess.run(
        [ffmpeg, "-hide_banner", "-y",
         "-f", "lavfi", "-i", f"testsrc=size={size}:rate={fps}:duration={seconds}",
         "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
         "-c:v", "mjpeg", "-q:v", "5", "-c:a", "pcm_s16le", "-shortest", path],
        check=True, capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0)


# ── pure helpers ─────────────────────────────────────────────────────────────

def test_request_batch_precedence():
    # explicit config batch wins (tuning off), then tuned/seed, then OOM-carry, then auto.
    assert bv._request_batch(13, 17, 9) == 13     # explicit override wins
    assert bv._request_batch(0, 17, 9) == 17      # tuned/seed beats the carry
    assert bv._request_batch(0, None, 9) == 9     # OOM-carry when no tuned value
    assert bv._request_batch(0, None, None) == 0  # auto (pod sizes it)


def test_mp_bucket_is_fine_grained_and_separates_real_sizes():
    # FINE grid (0.05 MP): distinct real output sizes get distinct keys, so a portrait box-fit
    # output (810x1080 = 0.87 MP) no longer collides with a landscape one (1280x960 = 1.23 MP)
    # as it did under the old 0.5-MP bucket. Very-near sizes still merge (history reused).
    assert bv._mp_bucket(0.87) != bv._mp_bucket(1.23)       # portrait vs landscape (was one bucket)
    assert bv._mp_bucket(0.39) != bv._mp_bucket(0.69)       # two small distinct points
    assert bv._mp_bucket(8.30) == bv._mp_bucket(8.31)       # near-identical sizes still merge
    assert bv._mp_bucket(0) == 0
    assert isinstance(bv._mp_bucket(3.7), int)


# ── DB round-trip + staleness ────────────────────────────────────────────────

def test_learned_batch_roundtrip(db_conn):
    assert db.get_learned_batch(db_conn, "GPU-X", 17) is None
    db.put_learned_batch(db_conn, "GPU-X", 17, 13)
    assert db.get_learned_batch(db_conn, "GPU-X", 17) == 13
    db.put_learned_batch(db_conn, "GPU-X", 17, 21)         # newest wins (self-correct)
    assert db.get_learned_batch(db_conn, "GPU-X", 17) == 21
    # a different card / bucket is independent
    assert db.get_learned_batch(db_conn, "GPU-Y", 17) is None
    assert db.get_learned_batch(db_conn, "GPU-X", 8) is None


def test_learned_batch_ge_nearest_at_or_above(db_conn):
    # The sizer's read: the nearest recorded key >= the run's key (a ceiling measured at a LARGER
    # output is a safe bound for a smaller run). Keys are in 0.05-MP units here.
    db.put_learned_batch(db_conn, "GPU-X", 14, 25)         # 0.69 MP -> batch 25
    db.put_learned_batch(db_conn, "GPU-X", 25, 17)         # 1.23 MP -> batch 17
    assert db.get_learned_batch_ge(db_conn, "GPU-X", 14) == 25   # exact
    assert db.get_learned_batch_ge(db_conn, "GPU-X", 20) == 17   # between -> next above (1.23)
    assert db.get_learned_batch_ge(db_conn, "GPU-X", 25) == 17   # exact upper
    assert db.get_learned_batch_ge(db_conn, "GPU-X", 26) is None  # above everything -> use seed
    assert db.get_learned_batch_ge(db_conn, "GPU-Y", 14) is None  # other card independent


def test_learned_batch_ge_skips_stale_to_next(db_conn):
    db.put_learned_batch(db_conn, "GPU-X", 14, 25)
    db.put_learned_batch(db_conn, "GPU-X", 25, 17)
    db_conn.execute("UPDATE video_batch_learn SET updated_at='2000-01-01T00:00:00' WHERE mp_key=14")
    db_conn.commit()
    # The nearest (14) is stale -> skip it and return the fresh 25's batch (still a safe >= bound).
    assert db.get_learned_batch_ge(db_conn, "GPU-X", 14) == 17


def test_learned_batch_staleness(db_conn):
    db.put_learned_batch(db_conn, "GPU-X", 17, 13)
    # backdate the row well past the window
    db_conn.execute("UPDATE video_batch_learn SET updated_at = '2000-01-01T00:00:00'")
    db_conn.commit()
    assert db.get_learned_batch(db_conn, "GPU-X", 17, max_age_days=90) is None
    assert db.get_learned_batch(db_conn, "GPU-X", 17, max_age_days=0) == 13   # 0 = ignore age


def test_put_ignores_junk(db_conn):
    db.put_learned_batch(db_conn, "", 17, 13)              # no gpu
    db.put_learned_batch(db_conn, "GPU-X", 17, 0)         # no batch
    assert db.get_learned_batch(db_conn, "GPU-X", 17) is None


# ── end-to-end convergence (ffmpeg) ──────────────────────────────────────────

class _FakeTuningEngine(bv.PassthroughVideoEngine):
    """PassthroughVideoEngine that also reports pod-style status (a resolved + a
    measured-anchored suggested batch), and records the batch requested per segment."""

    def __init__(self, suggested):
        super().__init__()
        self.suggested = suggested
        self.requested = []

    def process_segment(self, src_path, dest_path, *, resolution, batch_size,
                        chunk_size, temporal_overlap=0, seed=None,
                        video_backend="opencv", use_10bit=False,
                        poll_interval=0, on_progress=None, should_stop=None,
                        seg_index=None, seg_total=None, model=None):
        self.requested.append(batch_size)
        n = super().process_segment(src_path, dest_path, resolution=resolution,
                                    batch_size=batch_size, chunk_size=chunk_size,
                                    temporal_overlap=temporal_overlap)
        if on_progress:
            on_progress({"state": "running", "resolved_batch": batch_size or 13,
                         "resolved_overlap": 6, "peak_alloc_gb": 20.0,
                         "suggested_batch": self.suggested,
                         "frames_written": n, "seconds": self.last_segment_seconds})
        return n


def _bucket_for(src_root, rel, target):
    import video_estimate as ve
    info = vp.probe(os.path.join(src_root, rel))
    return bv._mp_bucket(ve.output_megapixels(info.width, info.height, target))


def _learn_key(vcfg, gpu="GPU-TEST"):
    """The video_batch_learn key the runner will read for this config, derived the way
    process_job derives it rather than spelled out here. A literal "GPU-TEST" used to work
    only because the key happened to be the bare card id; it stopped the moment the VRAM
    regime joined the key, and a test that hardcodes a key cannot notice that it moved.
    """
    import video_vram_sizer as sizer
    return gpu + sizer.learn_tag(bv._engine_flags(vcfg))


@needs_ffmpeg
def test_autotune_converges_freezes_and_persists(db_conn, tmp_path, monkeypatch):
    monkeypatch.setenv("IMGTBX_GPU_OVERRIDE", "GPU-TEST")
    src_root = str(tmp_path / "src")
    out_root = str(tmp_path / "out")
    _make_source(os.path.join(src_root, "long.avi"), seconds=24)
    root = db.get_video_root_id(db_conn, src_root, out_root)
    vcfg = bv.resolve_video_cfg({})
    vcfg["work_root"] = str(tmp_path / "work")
    vcfg["segment_seconds"] = 8            # force multiple segments from 24s
    vcfg["max_segment_seconds"] = 10
    assert vcfg["auto_tune_batch"] is True

    bv.prepare_job(db_conn, root, src_root, out_root, "long.avi", "1080p", vcfg)
    eng = _FakeTuningEngine(suggested=17)
    summary = bv.run_queue(eng, db_conn, root, src_root, vcfg, bv.RunBudget(0, 0.0))
    assert summary["done"] == 1 and summary["failed"] == 0
    assert len(eng.requested) >= 2, "needed a multi-segment video to test tuning"

    # First segment ran AUTO (no seed); the converged value froze the rest at 17.
    assert eng.requested[0] == 0
    assert all(b == 17 for b in eng.requested[1:])

    # ...and it was persisted for next time, keyed by card + VRAM regime + output-MP bucket.
    bucket = _bucket_for(src_root, "long.avi", "1080p")
    assert db.get_learned_batch(db_conn, _learn_key(vcfg), bucket) == 17


@needs_ffmpeg
def test_autotune_seeds_first_segment_from_db(db_conn, tmp_path, monkeypatch):
    monkeypatch.setenv("IMGTBX_GPU_OVERRIDE", "GPU-TEST")
    src_root = str(tmp_path / "src")
    out_root = str(tmp_path / "out")
    _make_source(os.path.join(src_root, "long.avi"), seconds=24)
    root = db.get_video_root_id(db_conn, src_root, out_root)
    vcfg = bv.resolve_video_cfg({})
    vcfg["work_root"] = str(tmp_path / "work")
    vcfg["segment_seconds"] = 8
    vcfg["max_segment_seconds"] = 10

    bucket = _bucket_for(src_root, "long.avi", "1080p")
    db.put_learned_batch(db_conn, _learn_key(vcfg), bucket, 9)   # a prior learned value

    bv.prepare_job(db_conn, root, src_root, out_root, "long.avi", "1080p", vcfg)
    eng = _FakeTuningEngine(suggested=9)                   # measurement confirms 9
    bv.run_queue(eng, db_conn, root, src_root, vcfg, bv.RunBudget(0, 0.0))
    assert eng.requested[0] == 9                           # seeded from the DB, not auto


@needs_ffmpeg
def test_autotune_disabled_writes_nothing(db_conn, tmp_path, monkeypatch):
    monkeypatch.setenv("IMGTBX_GPU_OVERRIDE", "GPU-TEST")
    src_root = str(tmp_path / "src")
    out_root = str(tmp_path / "out")
    _make_source(os.path.join(src_root, "long.avi"), seconds=24)
    root = db.get_video_root_id(db_conn, src_root, out_root)
    vcfg = bv.resolve_video_cfg({})
    vcfg["work_root"] = str(tmp_path / "work")
    vcfg["segment_seconds"] = 8
    vcfg["max_segment_seconds"] = 10
    vcfg["auto_tune_batch"] = False                        # off

    bv.prepare_job(db_conn, root, src_root, out_root, "long.avi", "1080p", vcfg)
    eng = _FakeTuningEngine(suggested=17)
    bv.run_queue(eng, db_conn, root, src_root, vcfg, bv.RunBudget(0, 0.0))
    bucket = _bucket_for(src_root, "long.avi", "1080p")
    assert db.get_learned_batch(db_conn, "GPU-TEST", bucket) is None
    assert all(b == 0 for b in eng.requested)             # stayed auto throughout
