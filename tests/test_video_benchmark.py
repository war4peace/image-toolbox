"""
Per-card VRAM benchmark suite (feature #7, docs 16/20). GPU-free coverage of the pure
sweep logic, the DB probe store + resume state, the clip-download integrity check, and the
end-to-end "ceiling -> learned batch + rate" recording that feeds the sizer and the estimate.
"""

import hashlib
import pathlib

import pytest

import db
import video_benchmark as vb
import video_vram_sizer as sizer
import video_estimate as ve
import benchmark_clip as bclip


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


# ── pure plan / sweep logic ──────────────────────────────────────────────────

def test_batch_series_is_ascending_4n1():
    s = vb.batch_series(cap=33)
    assert s == [5, 9, 13, 17, 21, 25, 29, 33]
    assert vb.batch_series(cap=65)[-1] == 65        # the suite probes past the AUTO cap


def test_build_cell_source_is_even_half():
    c = vb.build_cell("1080p", 1920, 1080)
    assert (c["out_w"], c["out_h"]) == (1920, 1080)
    assert (c["src_w"], c["src_h"]) == (960, 540)   # 2x source, even dims
    assert c["resolution"] == 1080                  # output short side
    assert c["mp"] == pytest.approx(2.0736)


def test_build_plan_skips_unknown_targets():
    plan = vb.build_plan(["1080p", "bogus", "4K"])
    assert [c["name"] for c in plan] == ["1080p", "4K"]


def test_ladder_targets_are_benchmarkable_on_24gb():
    """The 4:3 + 3:4 ladder gives a 24 GB card real targets BELOW the 2.1-MP 1080p edge. Each
    cell must fit under the 24 GB cap and get a DISTINCT fine sizer key (its own learned-batch
    row, no collision), and the user's actual workload size (1280x960) is included."""
    ladder = ["540x720", "960x720", "810x1080", "1280x960", "1440x1080", "1600x1200"]
    cap = ve.max_output_mp(24)                                   # 2.1 MP on a 24 GB card
    keys = []
    for name in ladder:
        assert name in vb.TARGETS
        c = vb.build_cell(name, *vb.TARGETS[name])
        assert c["mp"] <= cap + 1e-6                             # benchmarkable on a 24 GB card
        assert c["src_w"] % 2 == 0 and c["src_h"] % 2 == 0       # even 2x source (yuv420p)
        keys.append(sizer.mp_bucket(c["mp"]))
    assert len(set(keys)) == len(keys)                           # distinct key per real size
    # A portrait box-fit output is smaller-area than its landscape counterpart at the same box,
    # so it is a genuinely distinct point (810x1080 = 0.87 MP vs 1440x1080 = 1.56 MP).
    assert sizer.mp_bucket(0.87) != sizer.mp_bucket(1.56)
    # 1280x960 is the 2x-of-640x480 / 4x-of-320x240 output — must be present and feasible.
    assert vb.TARGETS["1280x960"] == (1280, 960)


def test_sizer_uses_nearest_benchmark_at_or_above(db_conn):
    """The safety property behind the fine key grid: a benchmark at a LARGER output serves a
    smaller run (conservative, safe), but a benchmark only at a SMALLER output is NOT applied to
    a larger run (its ceiling is too high) -- that run uses the conservative seed instead."""
    gpu, model = "NVIDIA GeForce RTX 3090", "seedvr2_ema_7b_fp16.safetensors"
    tag = sizer.model_tag(model)
    db.put_learned_batch(db_conn, f"{gpu}|{tag}", sizer.mp_bucket(1.23), 21)   # 1280x960 ceiling
    # Smaller run (810x1080 = 0.87 MP) may safely reuse the 1.23-MP ceiling.
    b, _ = sizer.pick(model, 810, 1080, conn=db_conn, gpu_id=gpu, free_gb=200, total_gb=24)
    assert b == 21
    # Larger run (1440x1080 = 1.56 MP) has no benchmark at-or-above -> never the too-high 21.
    b2, _ = sizer.pick(model, 1440, 1080, conn=db_conn, gpu_id=gpu, free_gb=200, total_gb=24)
    assert b2 != 21


def test_next_batch_fresh_resume_and_done():
    series = vb.batch_series(cap=33)
    assert vb.next_batch([], series) == 5                                   # fresh
    assert vb.next_batch([{"batch": 5, "outcome": "ok"}], series) == 9      # continue
    # a gap (5 ok, 13 ok, 9 missing) resumes at the lowest untried
    assert vb.next_batch([{"batch": 5, "outcome": "ok"},
                          {"batch": 13, "outcome": "ok"}], series) == 9
    # a failure ends the cell
    assert vb.next_batch([{"batch": 5, "outcome": "ok"},
                          {"batch": 9, "outcome": "oom"}], series) is None
    assert vb.next_batch([{"batch": 5, "outcome": "thrash"}], series) is None


def test_next_batch_stale_contended_failure_is_retried():
    series = vb.batch_series(cap=33)
    # A batch-9 oom recorded when only 15 GB was free; now 22 GB is free (7 GB more headroom):
    # the failure was contention, not the ceiling -> re-probe 9, and it does NOT cap the sweep.
    probes = [{"batch": 5, "outcome": "ok", "free_vram": 15.0},
              {"batch": 9, "outcome": "oom", "free_vram": 15.0}]
    assert vb.next_batch(probes, series, free_now=22.0) == 9      # stale fail -> retried
    # With the SAME headroom as when it failed, the failure is trusted (terminal).
    assert vb.next_batch(probes, series, free_now=15.0) is None
    # No free_now (can't tell) -> trust the recorded failure, as before.
    assert vb.next_batch(probes, series) is None


def test_next_batch_stale_failure_below_trustworthy_one():
    series = vb.batch_series(cap=33)
    # 9 failed under contention (stale), 21 failed clean (trustworthy). We should re-probe the
    # gap below 21 (9, 13, 17) but never reach 21+.
    probes = [{"batch": 5, "outcome": "ok", "free_vram": 22.0},
              {"batch": 9, "outcome": "oom", "free_vram": 15.0},
              {"batch": 21, "outcome": "oom", "free_vram": 22.0}]
    assert vb.next_batch(probes, series, free_now=22.0) == 9


def test_cell_ceiling():
    assert vb.cell_ceiling([{"batch": 5, "outcome": "ok"},
                            {"batch": 9, "outcome": "ok"},
                            {"batch": 13, "outcome": "oom"}]) == 9
    assert vb.cell_ceiling([{"batch": 5, "outcome": "oom"}]) is None


def test_estimate_runtime_positive_and_drops_with_done():
    plan = vb.build_plan(["1080p", "4K"])
    full = vb.estimate_runtime(plan, "RTX 3090", conn=None, done=0)
    part = vb.estimate_runtime(plan, "RTX 3090", conn=None, done=4)
    assert full > 0 and 0 <= part < full


# ── DB probe store + resume ──────────────────────────────────────────────────

def test_bench_probe_roundtrip_and_clear(db_conn):
    db.record_bench_probe(db_conn, "RTX 3090", "7b", 1920, 1080, 5, "ok",
                          frames=37, seconds=180.0, peak_alloc=17.1, peak_reserved=20.8)
    db.record_bench_probe(db_conn, "RTX 3090", "7b", 1920, 1080, 9, "oom")
    rows = db.get_bench_probes(db_conn, "RTX 3090", "7b")
    assert [(r["batch"], r["outcome"]) for r in rows] == [(5, "ok"), (9, "oom")]
    assert db.get_bench_probes(db_conn, "RTX 3090", "7b", 1920, 1080)[0]["seconds"] == 180.0
    db.record_bench_probe(db_conn, "RTX 3090", "7b", 1920, 1080, 5, "thrash")  # upsert overwrites
    assert db.get_bench_probes(db_conn, "RTX 3090", "7b", 1920, 1080)[0]["outcome"] == "thrash"
    db.clear_bench(db_conn, "RTX 3090")
    assert db.get_bench_probes(db_conn, "RTX 3090", "7b") == []


def test_bench_probe_free_vram_tag_roundtrips(db_conn):
    db.record_bench_probe(db_conn, "RTX 3090", "7b", 1920, 1080, 9, "oom", free_vram=15.3)
    row = db.get_bench_probes(db_conn, "RTX 3090", "7b", 1920, 1080)[0]
    assert row["free_vram"] == pytest.approx(15.3)


# ── ceiling -> learned batch + rate (the payoff) ─────────────────────────────

def test_record_cell_result_feeds_sizer_and_estimate(db_conn):
    gpu, model = "NVIDIA GeForce RTX 3090", "7b"
    cell = vb.build_cell("1080p", 1920, 1080)
    for b in (5, 9, 13, 17):
        db.record_bench_probe(db_conn, gpu, model, 1920, 1080, b, "ok",
                              frames=37, seconds=185.0, peak_alloc=20.9, peak_reserved=23.0)
    db.record_bench_probe(db_conn, gpu, model, 1920, 1080, 21, "oom")

    ceil, saved = vb._record_cell_result(db_conn, gpu, model, cell)
    assert ceil == 17 and saved == 17          # equal-speed probes -> saved == max fit

    # Sizer now starts AUTO at the saved (fastest) batch for this card+output.
    b, ov = sizer.pick("seedvr2_ema_7b_fp16.safetensors", 1920, 1080,
                       conn=db_conn, gpu_id=gpu, free_gb=200, total_gb=24)
    assert b == 17

    # Estimate now has a measured, calibrated s/MP for 1080p on this card.
    spm, calibrated = ve.local_seconds_per_mp(gpu, "1080p", conn=db_conn)
    assert calibrated is True
    # gpu_perf stores cumulative MP as an int (record_gpu_perf casts), so the rate is
    # seconds / int(frames * MP); ~1% below the float, which is fine for an estimate.
    assert spm == pytest.approx(185.0 / int(37 * cell["mp"]), rel=1e-3)


def test_record_cell_result_none_when_floor_fails(db_conn):
    gpu, model = "SmallCard", "7b"
    cell = vb.build_cell("4K", 3840, 2160)
    db.record_bench_probe(db_conn, gpu, model, 3840, 2160, 5, "oom")   # can't even do the floor
    assert vb._record_cell_result(db_conn, gpu, model, cell) == (None, None)
    # nothing learned for that bucket
    assert db.get_learned_batch(db_conn, f"{gpu}|{model}", sizer.mp_bucket(cell["mp"])) is None


# ── clip download integrity ──────────────────────────────────────────────────

def test_drop_collapsed_discards_phantom_ok_rows():
    rows = [{"batch": 5, "outcome": "ok", "frames": 37},      # frames >= batch: real
            {"batch": 37, "outcome": "ok", "frames": 37},     # frames == batch: real
            {"batch": 41, "outcome": "ok", "frames": 37},     # frames < batch: phantom (collapsed)
            {"batch": 65, "outcome": "ok", "frames": 37},     # phantom
            {"batch": 9, "outcome": "oom", "frames": None}]   # failures kept regardless
    kept = vb.drop_collapsed(rows)
    assert [(p["batch"], p["outcome"]) for p in kept] == [(5, "ok"), (37, "ok"), (9, "oom")]
    # A phantom high row must NOT be taken as the ceiling.
    assert vb.cell_ceiling(kept) == 37
    assert vb.cell_ceiling(rows) == 65                        # (unfiltered would be poisoned)


def test_record_cell_result_ignores_collapsed_rows(db_conn):
    # Real ceiling is 33 (frames-correct); phantom 'ok' at 41/65 (only 37 frames) must not win.
    gpu, model = "NVIDIA GeForce RTX 3090", "7b"
    cell = vb.build_cell("540x720", 540, 720)
    for b in (5, 33):
        db.record_bench_probe(db_conn, gpu, model, 540, 720, b, "ok",
                              frames=max(b, 37), seconds=100.0, peak_alloc=10.0, peak_reserved=12.0)
    for b in (41, 65):                                        # collapsed phantoms (frames < batch)
        db.record_bench_probe(db_conn, gpu, model, 540, 720, b, "ok", frames=37, seconds=100.0)
    assert vb._record_cell_result(db_conn, gpu, model, cell) == (33, 33)


def test_throughput_optimal_batch_picks_knee_not_ceiling():
    # s/frame improves to bs61 then WORSENS at bs69 (the ceiling rides sysmem spill).
    probes = [{"batch": 5, "outcome": "ok", "frames": 37, "seconds": 37 * 4.0},
              {"batch": 61, "outcome": "ok", "frames": 61, "seconds": 61 * 0.93},
              {"batch": 69, "outcome": "ok", "frames": 69, "seconds": 69 * 1.22},
              {"batch": 73, "outcome": "oom", "frames": None, "seconds": 30.0}]
    assert vb.throughput_optimal_batch(probes) == 61          # the fastest, not the max fit (69)
    # equal-speed probes -> the LARGER window wins (more continuity for the same speed)
    tie = [{"batch": 9, "outcome": "ok", "frames": 37, "seconds": 37.0},
           {"batch": 13, "outcome": "ok", "frames": 37, "seconds": 37.0}]
    assert vb.throughput_optimal_batch(tie) == 13
    # no timing anywhere -> None (caller falls back to the ceiling)
    assert vb.throughput_optimal_batch(
        [{"batch": 5, "outcome": "ok", "frames": None, "seconds": None}]) is None


def test_record_cell_result_saves_fastest_not_ceiling(db_conn):
    """The user's directive: save + use the MOST EFFICIENT batch, not the raw ceiling (which
    can be slower, riding VRAM spill). Mirrors the real 960x720 sweep: bs61 fastest, bs69 the
    ceiling but slower, bs73 oom."""
    gpu, model = "NVIDIA GeForce RTX 3090", "7b"
    cell = vb.build_cell("960x720", 960, 720)
    for b, spf in ((5, 4.25), (61, 0.93), (69, 1.22)):
        db.record_bench_probe(db_conn, gpu, model, 960, 720, b, "ok",
                              frames=max(b, 37), seconds=spf * max(b, 37),
                              peak_alloc=20.0, peak_reserved=22.0)
    db.record_bench_probe(db_conn, gpu, model, 960, 720, 73, "oom")
    ceil, saved = vb._record_cell_result(db_conn, gpu, model, cell)
    assert ceil == 69 and saved == 61                         # max fit 69, but USES the fast 61
    # AUTO now starts at the efficient batch, not the spill-edge ceiling.
    b, _ = sizer.pick("seedvr2_ema_7b_fp16.safetensors", 960, 720,
                      conn=db_conn, gpu_id=gpu, free_gb=200, total_gb=24)
    assert b == 61


def test_download_refuses_unpinned():
    with pytest.raises(ValueError):
        bclip.download_base_clip("http://x/y.mp4", "", "/tmp/z.mp4")
    with pytest.raises(ValueError):
        bclip.download_base_clip("", "abc", "/tmp/z.mp4")


# ── end-to-end sweep (fake engine, no GPU/ffmpeg) ────────────────────────────

class _FakeEngine:
    """Records the batches it was asked to probe; 'ok' up to `ceil`, then 'oom'."""
    def __init__(self, *a, **k):
        self.asked = []

    def probe_batch(self, src, dest, *, resolution, batch, frames=None, should_stop=None):
        self.asked.append(batch)
        if batch <= 13:
            return {"outcome": "ok", "batch": batch, "overlap": 6, "frames": frames,
                    "seconds": 100.0, "peak_alloc_gb": 20.0, "peak_reserved_gb": 22.0}
        return {"outcome": "oom", "batch": batch, "overlap": 6, "seconds": 30.0}

    def close(self):
        pass


@pytest.fixture
def fake_run(db_conn, tmp_path, monkeypatch):
    """Patch the GPU/ffmpeg dependencies so run_benchmark exercises its control flow only."""
    engine = _FakeEngine()
    import local_video_engine as lve
    monkeypatch.setattr(lve, "LocalVideoEngine", lambda *a, **k: engine)
    monkeypatch.setattr(lve, "_query_gpu_name", lambda: "FakeGPU")
    monkeypatch.setattr(vb.bclip, "ensure_source_clip", lambda *a, **k: str(tmp_path / "dummy.mp4"))
    monkeypatch.setattr(vb.bv, "_load_config", lambda: {
        "video": {"work_root": str(tmp_path / "work"),
                  "dit_model": "seedvr2_ema_7b_fp16.safetensors"},
        "upscale": {}, "seedvr2": {}})
    return engine


def test_run_benchmark_sweeps_records_and_learns(fake_run, db_conn):
    vb.run_benchmark(["1080p"], frames=37, resume=False, batch_cap=33)
    assert fake_run.asked == [5, 9, 13, 17]                 # stopped at the first oom
    rows = db.get_bench_probes(db_conn, "FakeGPU", "7b", 1920, 1080)
    assert [(r["batch"], r["outcome"]) for r in rows] == [(5, "ok"), (9, "ok"), (13, "ok"), (17, "oom")]
    # ceiling 13 landed in the sizer's learned store
    assert db.get_learned_batch(db_conn, "FakeGPU|7b", sizer.mp_bucket(2.0736)) == 13


def test_run_benchmark_resumes_from_saved(fake_run, db_conn):
    # Pre-seed a partial sweep (5, 9 already clean); a resume must continue at 13.
    for b in (5, 9):
        db.record_bench_probe(db_conn, "FakeGPU", "7b", 1920, 1080, b, "ok",
                              frames=37, seconds=100.0)
    vb.run_benchmark(["1080p"], frames=37, resume=True, batch_cap=33)
    assert fake_run.asked == [13, 17]                       # skipped the saved 5, 9


def test_probe_clip_is_sized_to_the_batch_no_collapse(db_conn, tmp_path, monkeypatch):
    """Regression: a fixed 37-frame clip made every 'batch 41+' probe SECRETLY run batch 37
    (a temporal window can't exceed the clip's frame count). Each probe's clip must have
    >= its batch's frames, and the sweep must be able to climb PAST 37 to the real ceiling."""
    requested = []

    def fake_ensure(work, w, h, frames, **k):
        requested.append(int(frames))
        return str(tmp_path / f"c_{int(frames)}.mp4")
    monkeypatch.setattr(vb.bclip, "ensure_source_clip", fake_ensure)

    class _Eng:
        def __init__(self, *a, **k):
            self.asked = []

        def probe_batch(self, src, dest, *, resolution, batch, frames=None, should_stop=None):
            self.asked.append((batch, frames))
            oc = "ok" if batch <= 45 else "oom"             # real ceiling ABOVE the old 37/65 fog
            return {"outcome": oc, "batch": batch, "overlap": 6, "frames": frames,
                    "seconds": 10.0, "peak_alloc_gb": 1.0, "peak_reserved_gb": 1.0}

        def close(self):
            pass

    eng = _Eng()
    import local_video_engine as lve
    monkeypatch.setattr(lve, "LocalVideoEngine", lambda *a, **k: eng)
    monkeypatch.setattr(lve, "_query_gpu_name", lambda: "FakeGPU")
    monkeypatch.setattr(vb.bv, "_load_config", lambda: {
        "video": {"work_root": str(tmp_path / "work"),
                  "dit_model": "seedvr2_ema_7b_fp16.safetensors"},
        "upscale": {}, "seedvr2": {}})

    vb.run_benchmark(["540x720"], frames=37, resume=False, batch_cap=65)
    # Every probe's clip had at least as many frames as the batch (no silent collapse).
    for batch, fr in eng.asked:
        assert fr is not None and fr >= batch
    # The sweep climbed past the old fixed-37 wall to the true ceiling (oom at 49).
    assert max(b for b, _ in eng.asked) > 37
    assert vb.cell_ceiling([{"batch": b, "outcome": ("ok" if b <= 45 else "oom")}
                            for b, _ in eng.asked]) == 45


def test_download_verifies_hash(tmp_path):
    src = tmp_path / "clip.bin"
    src.write_bytes(b"benchmark-bytes")
    good = hashlib.sha256(src.read_bytes()).hexdigest()
    url = pathlib.Path(src).as_uri()
    dest = tmp_path / "out.bin"
    assert bclip.download_base_clip(url, good, str(dest)) == str(dest)
    assert dest.read_bytes() == b"benchmark-bytes"
    # a wrong pin refuses AND removes the partial
    bad_dest = tmp_path / "bad.bin"
    with pytest.raises(ValueError):
        bclip.download_base_clip(url, "0" * 64, str(bad_dest))
    assert not bad_dest.exists()
