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


def test_build_cell_uses_native_aspect_matched_source():
    c = vb.build_cell("1080p", *vb.TARGETS["1080p"])
    assert (c["out_w"], c["out_h"]) == (1920, 1080)
    assert c["source"] == "w360"
    assert (c["src_w"], c["src_h"]) == (640, 360)   # native 16:9 source, even dims (engine upscales)
    assert c["resolution"] == 1080                  # output short side the source is scaled up to
    assert c["mp"] == pytest.approx(2.0736)
    # A 3:4 cell draws from the native 240x320 portrait source (real upscale, not a synthetic half).
    p = vb.build_cell("810x1080", *vb.TARGETS["810x1080"])
    assert p["source"] == "p3x4" and (p["src_w"], p["src_h"]) == (240, 320)


def test_build_plan_skips_unknown_targets():
    plan = vb.build_plan(["1080p", "bogus", "4K"])
    assert [c["name"] for c in plan] == ["1080p", "4K"]


def test_gui_target_labels_build_for_every_target():
    """Guard the 3-tuple TARGETS regression: the Benchmark window builds a checkbox label for
    EVERY target on open (BenchmarkWindow._target_label unpacks TARGETS[t]). When TARGETS grew a
    source key, a `w, h = TARGETS[t]` there crashed window open ('too many values to unpack').
    The staticmethod needs no Tk root, so exercise it for every target so that can't regress."""
    pytest.importorskip("tkinter")
    from gui.video_benchmark import BenchmarkWindow
    for t in vb.TARGETS:
        label = BenchmarkWindow._target_label(t)
        assert str(vb.TARGETS[t][0]) in label or t in label


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
        assert c["src_w"] % 2 == 0 and c["src_h"] % 2 == 0       # even native source (yuv420p)
        keys.append(sizer.mp_bucket(c["mp"]))
    assert len(set(keys)) == len(keys)                           # distinct key per real size
    # A portrait box-fit output is smaller-area than its landscape counterpart at the same box,
    # so it is a genuinely distinct point (810x1080 = 0.87 MP vs 1440x1080 = 1.56 MP).
    assert sizer.mp_bucket(0.87) != sizer.mp_bucket(1.56)
    # 1280x960 is the 2x-of-640x480 / 4x-of-320x240 output — must be present and feasible.
    assert vb.TARGETS["1280x960"][:2] == (1280, 960)


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


def test_next_batch_geometric_climb_then_bracket():
    # Phase 1: start at the floor, then DOUBLE (snapped to 4n+1) until a failure.
    assert vb.next_batch([], 5, 33) == 5                                    # fresh -> floor
    assert vb.next_batch([{"batch": 5, "outcome": "ok"}], 5, 33) == 9       # 5*2 -> 9
    assert vb.next_batch([{"batch": 5, "outcome": "ok"},
                          {"batch": 9, "outcome": "ok"}], 5, 33) == 17      # 9*2 -> 17
    # Phase 2: a failure brackets the ceiling; binary-refine between the last ok and it.
    assert vb.next_batch([{"batch": 5, "outcome": "ok"},
                          {"batch": 9, "outcome": "ok"},
                          {"batch": 17, "outcome": "oom"}], 5, 33) == 13    # mid(9,17)
    # Adjacent ok/oom rungs => ceiling pinned, cell done.
    assert vb.next_batch([{"batch": 9, "outcome": "ok"},
                          {"batch": 13, "outcome": "oom"}], 5, 33) is None
    # The floor itself failing (nothing smaller tried) => card can't do this cell.
    assert vb.next_batch([{"batch": 5, "outcome": "thrash"}], 5, 33) is None


def test_next_batch_climbs_from_a_high_vram_floor_to_a_high_ceiling():
    # The "go stupid" path: a big card starts high (337) and doubles toward a 3000 cap, so it
    # reaches a ~1500 wall in a handful of probes instead of ~370 linear rungs.
    seq, probes = [], []
    outcome = lambda b: "ok" if b <= 1400 else "oom"
    for _ in range(60):
        b = vb.next_batch(probes, 337, 3000)
        if b is None:
            break
        seq.append(b)
        probes.append({"batch": b, "outcome": outcome(b)})
    assert seq[0] == 337                                    # opened at the VRAM floor, not 5
    assert seq[1] == 673 and seq[2] == 1345                 # geometric doubling
    assert len(seq) < 20                                    # ~a dozen (climb + binary), not ~370
    assert vb.cell_ceiling(probes) == 1397                  # binary refine pins the EXACT wall
    assert max(seq) > 1400                                  # it overshot the wall then refined down


def test_next_batch_floor_too_high_searches_below():
    # A predicted floor that OOMs first must fall back below it (confirm a low anchor, then
    # bisect), never leaving the cell unbenchmarked.
    assert vb.next_batch([{"batch": 337, "outcome": "oom"}], 337, 3000) == 5   # anchor low
    nxt = vb.next_batch([{"batch": 337, "outcome": "oom"},
                         {"batch": 5, "outcome": "ok"}], 337, 3000)
    assert 5 < nxt < 337                                    # bisecting up toward the wall


def test_next_batch_stale_contended_failure_is_retried():
    # A batch-9 oom recorded when only 15 GB was free; now 22 GB is free (7 GB more headroom):
    # the failure was contention, not the ceiling -> re-probe 9, and it does NOT cap the sweep.
    probes = [{"batch": 5, "outcome": "ok", "free_vram": 15.0},
              {"batch": 9, "outcome": "oom", "free_vram": 15.0}]
    assert vb.next_batch(probes, 5, 33, free_now=22.0) == 9       # stale fail -> retried
    # With the SAME headroom as when it failed, the failure is trusted (terminal, ceiling=5).
    assert vb.next_batch(probes, 5, 33, free_now=15.0) is None
    # No free_now (can't tell) -> trust the recorded failure, as before.
    assert vb.next_batch(probes, 5, 33) is None


def test_cell_done_is_floor_independent():
    # cell_done answers 'ceiling pinned?' from the probes alone (GUI has no VRAM), matching
    # next_batch's terminal condition regardless of the floor used to start.
    assert vb.cell_done([{"batch": 9, "outcome": "ok"},
                         {"batch": 13, "outcome": "oom"}]) is True
    assert vb.cell_done([{"batch": 5, "outcome": "ok"}]) is False        # can still climb


def test_vram_floor_batch_scales_with_vram_and_is_safe_when_unknown():
    # Unknown VRAM or MP -> the base floor (climb from 5), never a crash.
    assert sizer.vram_floor_batch(None, 0.389) == sizer.BATCH_FLOOR
    assert sizer.vram_floor_batch(96, 0) == sizer.BATCH_FLOOR
    # A small card at a modest output has no headroom above the fixed working set -> base floor.
    assert sizer.vram_floor_batch(24, 0.389) == sizer.BATCH_FLOOR
    # A big card at a small output starts HIGH (skips the pointless low rungs), a valid 4n+1.
    big = sizer.vram_floor_batch(96, 0.389)
    assert big > 200 and (big - 1) % 4 == 0
    # Monotone: more VRAM -> higher floor; a bigger output (more VRAM/frame) -> lower floor.
    assert sizer.vram_floor_batch(96, 0.389) > sizer.vram_floor_batch(48, 0.389)
    assert sizer.vram_floor_batch(96, 8.29) < sizer.vram_floor_batch(96, 0.389)


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
    cell = vb.build_cell("1080p", *vb.TARGETS["1080p"])
    for b in (5, 9, 13, 17):
        db.record_bench_probe(db_conn, gpu, model, 1920, 1080, b, "ok",
                              frames=37, seconds=185.0, peak_alloc=20.9, peak_reserved=23.0)
    db.record_bench_probe(db_conn, gpu, model, 1920, 1080, 21, "oom")

    ceil, saved = vb._record_cell_result(db_conn, gpu, model, cell, f"{gpu}|{model}")
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
    cell = vb.build_cell("4K", *vb.TARGETS["4K"])
    db.record_bench_probe(db_conn, gpu, model, 3840, 2160, 5, "oom")   # can't even do the floor
    assert vb._record_cell_result(db_conn, gpu, model, cell, f"{gpu}|{model}") == (None, None)
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
    cell = vb.build_cell("540x720", *vb.TARGETS["540x720"])
    for b in (5, 33):
        db.record_bench_probe(db_conn, gpu, model, 540, 720, b, "ok",
                              frames=max(b, 37), seconds=100.0, peak_alloc=10.0, peak_reserved=12.0)
    for b in (41, 65):                                        # collapsed phantoms (frames < batch)
        db.record_bench_probe(db_conn, gpu, model, 540, 720, b, "ok", frames=37, seconds=100.0)
    assert vb._record_cell_result(db_conn, gpu, model, cell, f"{gpu}|{model}") == (33, 33)


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
    cell = vb.build_cell("960x720", *vb.TARGETS["960x720"])
    for b, spf in ((5, 4.25), (61, 0.93), (69, 1.22)):
        db.record_bench_probe(db_conn, gpu, model, 960, 720, b, "ok",
                              frames=max(b, 37), seconds=spf * max(b, 37),
                              peak_alloc=20.0, peak_reserved=22.0)
    db.record_bench_probe(db_conn, gpu, model, 960, 720, 73, "oom")
    ceil, saved = vb._record_cell_result(db_conn, gpu, model, cell, f"{gpu}|{model}")
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

    def probe_batch(self, src, dest, *, resolution, batch, overlap=None, frames=None,
                    should_stop=None):
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
    # No GPU in the test: force the VRAM read to unknown so the climb starts at the base floor
    # (5) deterministically, independent of whatever card the test machine actually has.
    monkeypatch.setattr(sizer, "free_vram_gb", lambda *a, **k: (None, None))
    monkeypatch.setattr(vb.bclip, "ensure_source_clip", lambda *a, **k: str(tmp_path / "dummy.mp4"))
    monkeypatch.setattr(vb.bv, "_load_config", lambda: {
        "video": {"work_root": str(tmp_path / "work"),
                  "dit_model": "seedvr2_ema_7b_fp16.safetensors"},
        "upscale": {}, "seedvr2": {}})
    return engine


def test_run_benchmark_sweeps_records_and_learns(fake_run, db_conn):
    vb.run_benchmark(["1080p"], frames=37, resume=False, batch_cap=33)
    # Geometric climb 5->9->17(oom), then binary-refine mid(9,17)=13; ceiling 13 pinned.
    assert fake_run.asked == [5, 9, 17, 13]                 # climb 5,9,17(oom); refine mid->13
    rows = db.get_bench_probes(db_conn, "FakeGPU", "7b", 1920, 1080)   # returned sorted by batch
    assert [(r["batch"], r["outcome"]) for r in rows] == [(5, "ok"), (9, "ok"), (13, "ok"), (17, "oom")]
    # ceiling 13 landed in the sizer's learned store
    assert db.get_learned_batch(db_conn, "FakeGPU|7b", sizer.mp_bucket(2.0736)) == 13


def test_run_benchmark_resumes_from_saved(fake_run, db_conn):
    # Pre-seed a partial sweep (5, 9 already clean); a resume must continue the climb, not redo.
    for b in (5, 9):
        db.record_bench_probe(db_conn, "FakeGPU", "7b", 1920, 1080, b, "ok",
                              frames=37, seconds=100.0)
    vb.run_benchmark(["1080p"], frames=37, resume=True, batch_cap=33)
    assert fake_run.asked == [17, 13]                       # skipped the saved 5, 9; climb+refine


def test_probe_clip_is_sized_to_the_batch_no_collapse(db_conn, tmp_path, monkeypatch):
    """Regression: a fixed 37-frame clip made every 'batch 41+' probe SECRETLY run batch 37
    (a temporal window can't exceed the clip's frame count). Each probe's clip must have
    >= its batch's frames, and the sweep must be able to climb PAST 37 to the real ceiling."""
    requested = []

    def fake_ensure(work, source, frames, **k):
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
    monkeypatch.setattr(sizer, "free_vram_gb", lambda *a, **k: (None, None))   # floor -> 5
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


# ── remote sweep (fake pod: no RunPod, no GPU) ───────────────────────────────

class _FakeSession:
    """Stand-in for remote_run.RemoteSession: records that the pod was torn down."""
    def __init__(self):
        self.pod_id = "pod-abc"
        self.cost_per_hr = 0.69
        self._funds_tripped = False
        self.closed = False

    def close(self, *a, **k):
        self.closed = True


def _remote_cfg(tmp_path):
    return {"video": {"work_root": str(tmp_path / "work"),
                      "dit_model": "seedvr2_ema_7b_fp16.safetensors"},
            "upscale": {}, "seedvr2": {}, "runpod": {}}


def test_run_benchmark_remote_keys_plain_id_and_tears_down(db_conn, tmp_path, monkeypatch):
    """A REMOTE sweep keys the learned batch under the PLAIN RunPod id (what process_job's
    auto-tuner reads), NOT the model-qualified local key, and tears the pod down at the end
    (docs section 22.3)."""
    engine = _FakeEngine()
    session = _FakeSession()
    monkeypatch.setattr(vb, "_deploy_remote_engine", lambda cfg, vcfg: (engine, session))
    monkeypatch.setattr(vb.bclip, "ensure_source_clip", lambda *a, **k: str(tmp_path / "d.mp4"))
    monkeypatch.setattr(vb.bv, "_load_config", lambda: _remote_cfg(tmp_path))
    gpu = "NVIDIA GeForce RTX 5090"
    monkeypatch.setenv("IMGTBX_GPU_OVERRIDE", gpu)

    summary = vb.run_benchmark(["1080p"], frames=37, resume=False, batch_cap=33, remote=True)
    assert summary["stopped"] is None
    # A remote sweep warms the pod first (WARMUP_PROBES bs9 throwaways), then the measured
    # climb 5,9,17(oom) + binary-refine mid->13.
    assert engine.asked[:vb.WARMUP_PROBES] == [vb.WARMUP_BATCH] * vb.WARMUP_PROBES
    assert engine.asked[vb.WARMUP_PROBES:] == [5, 9, 17, 13]
    # probes stored under the RunPod id (returned sorted by batch; 17 is the recorded oom)
    rows = db.get_bench_probes(db_conn, gpu, "7b", 1920, 1080)
    assert (17, "oom") in [(r["batch"], r["outcome"]) for r in rows]
    # learned batch stored under the PLAIN id (the remote run's read key), NOT gpu|model
    assert db.get_learned_batch(db_conn, gpu, sizer.mp_bucket(2.0736)) == 13
    assert db.get_learned_batch(db_conn, f"{gpu}|7b", sizer.mp_bucket(2.0736)) is None
    # the remote RUN's auto-tuner seed (get_learned_batch_ge on the plain id) now finds it
    assert db.get_learned_batch_ge(db_conn, gpu, sizer.mp_bucket(2.0736)) == 13
    assert session.closed is True                            # pod torn down (billing stops)


def test_remote_warmup_runs_but_is_not_recorded(db_conn, tmp_path, monkeypatch):
    """The pod's cold first-forward cost is absorbed by throwaway bs9 warmups (a slow first
    batch the user flagged). They run before the measured sweep but are NOT persisted, so they
    can't pollute the ceiling or the throughput timing. Remote only."""
    engine = _FakeEngine()
    session = _FakeSession()
    monkeypatch.setattr(vb, "_deploy_remote_engine", lambda cfg, vcfg: (engine, session))
    monkeypatch.setattr(vb.bclip, "ensure_source_clip", lambda *a, **k: str(tmp_path / "d.mp4"))
    monkeypatch.setattr(vb.bv, "_load_config", lambda: _remote_cfg(tmp_path))
    monkeypatch.setenv("IMGTBX_GPU_OVERRIDE", "GPU-warm")

    vb.run_benchmark(["1080p"], frames=37, resume=False, batch_cap=33, remote=True)
    rows = db.get_bench_probes(db_conn, "GPU-warm", "7b", 1920, 1080)
    # Every asked batch = WARMUP_PROBES warmups + the measured probes; only the measured ones
    # are in the DB, so the warmups added exactly WARMUP_PROBES un-recorded passes.
    assert len(engine.asked) == vb.WARMUP_PROBES + len(rows)
    assert engine.asked[:vb.WARMUP_PROBES] == [vb.WARMUP_BATCH] * vb.WARMUP_PROBES


def test_local_sweep_does_not_warm_up(fake_run, db_conn):
    """The LOCAL path reloads the model per probe (fresh subprocess), so there is no warm state
    to build -- warmup must be skipped, and the measured sweep starts immediately."""
    vb.run_benchmark(["1080p"], frames=37, resume=False, batch_cap=33)
    assert fake_run.asked == [5, 9, 17, 13]                  # measured sweep, no warmup prefix


def test_run_benchmark_remote_refuses_without_selected_gpu(tmp_path, monkeypatch):
    monkeypatch.setattr(vb.bv, "_load_config", lambda: _remote_cfg(tmp_path))
    monkeypatch.delenv("IMGTBX_GPU_OVERRIDE", raising=False)
    deployed = []
    monkeypatch.setattr(vb, "_deploy_remote_engine",
                        lambda *a: (deployed.append(1), (None, None))[1])
    summary = vb.run_benchmark(["1080p"], frames=37, resume=False, remote=True)
    assert summary["stopped"] == "no GPU selected"
    assert deployed == []                                    # never spun a pod up


def test_run_benchmark_remote_stops_on_funds_guard(db_conn, tmp_path, monkeypatch):
    """The funds guard tripping ends the sweep cleanly (live accrual + guard is the only
    money control; no separate cost cap)."""
    engine = _FakeEngine()
    session = _FakeSession()
    session._funds_tripped = True                            # tripped before the first probe
    monkeypatch.setattr(vb, "_deploy_remote_engine", lambda cfg, vcfg: (engine, session))
    monkeypatch.setattr(vb.bclip, "ensure_source_clip", lambda *a, **k: str(tmp_path / "d.mp4"))
    monkeypatch.setattr(vb.bv, "_load_config", lambda: _remote_cfg(tmp_path))
    monkeypatch.setenv("IMGTBX_GPU_OVERRIDE", "NVIDIA GeForce RTX 5090")
    summary = vb.run_benchmark(["1080p"], frames=37, resume=False, remote=True)
    assert summary["stopped"] == "funds guard"
    assert engine.asked == []                                # nothing probed after the trip
    assert session.closed is True


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
