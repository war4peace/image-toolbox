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


def _bench_key(remote=False, cfg=None):
    """The video_bench model key run_benchmark will use, derived the way it derives it.

    Not a hardcoded "7b": the key carries the tiling AND compile tags, and the compile tag
    comes from the settings the engine ACTUALLY runs under -- which on the LOCAL path means
    AFTER gate_local_compile, so it depends on whether the machine running these tests has a
    C compiler. Recomputing it here keeps the tests honest on any machine instead of asserting
    a literal that is right on one laptop.
    """
    import batch_video_upscale as bv
    cfg = cfg or {"upscale": {}, "video": {}}
    vcfg = bv.resolve_video_cfg(cfg)
    ws = vb.effective_settings(cfg, vcfg, remote=remote, log_fn=lambda *_a, **_k: None)
    return vb.bench_key(vcfg, ws)


def _learn_key(gpu, remote=False, cfg=None):
    """The video_batch_learn key run_benchmark writes, derived the way it derives it.

    Same reasoning as [_bench_key], and the same trap: this key carries the VRAM regime too
    (see video_vram_sizer.learn_tag), so a hardcoded "FakeGPU|7b" silently stops describing
    what the runner writes the moment a regime tag applies. Remote keys the plain card id;
    local qualifies it with the model family.
    """
    import batch_video_upscale as bv
    import video_vram_sizer as sizer
    cfg = cfg or {"upscale": {}, "video": {}}
    vcfg = bv.resolve_video_cfg(cfg)
    ws = vb.effective_settings(cfg, vcfg, remote=remote, log_fn=lambda *_a, **_k: None)
    base = gpu if remote else f"{gpu}|{sizer.model_tag(vcfg['dit_model'])}"
    return base + sizer.learn_tag(ws)


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


def test_saved_metrics_reconstructs_spf_and_peak_from_persisted_probes():
    # The GUI bug: s/frame + Peak VRAM read blank on reopen because they were only ever set
    # from live events. saved_metrics rebuilds them from the persisted probes, at the SAVED
    # (throughput-optimal) batch. These rows are the user's real RTX 3090 540x720 compile-ON
    # ('7b|c') sweep: fastest is batch 73 @ 0.394 s/frame (~the 0.39 they remembered).
    probes = [
        {"batch": 5,  "outcome": "ok",  "frames": 37, "seconds": 80.58,  "peak_alloc": 16.6, "peak_reserved": 16.6},
        {"batch": 9,  "outcome": "ok",  "frames": 37, "seconds": 71.776, "peak_alloc": 22.8, "peak_reserved": 22.9},
        {"batch": 17, "outcome": "ok",  "frames": 37, "seconds": 22.834, "peak_alloc": 17.9, "peak_reserved": 18.1},
        {"batch": 33, "outcome": "ok",  "frames": 37, "seconds": 26.652, "peak_alloc": 19.1, "peak_reserved": 19.2},
        {"batch": 65, "outcome": "ok",  "frames": 65, "seconds": 26.031, "peak_alloc": 22.2, "peak_reserved": 22.3},
        {"batch": 73, "outcome": "ok",  "frames": 73, "seconds": 28.737, "peak_alloc": 23.1, "peak_reserved": 23.1},
        {"batch": 77, "outcome": "oom", "frames": None, "seconds": 74.98, "peak_alloc": None, "peak_reserved": None},
    ]
    saved = vb.throughput_optimal_batch(probes)
    assert saved == 73
    m = vb.saved_metrics(probes)
    assert round(m["spf"], 3) == 0.394
    assert (m["peak_alloc"], m["peak_reserved"]) == (23.1, 23.1)


def test_saved_metrics_none_without_timed_probe():
    # Only failures / untimed probes -> nothing to show (columns stay blank, fail-safe).
    assert vb.saved_metrics([{"batch": 5, "outcome": "oom", "frames": None, "seconds": 3.0}]) is None
    assert vb.saved_metrics([]) is None


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
                    should_stop=None, on_progress=None, warmup_src=None):
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
    rows = db.get_bench_probes(db_conn, "FakeGPU", _bench_key(), 1920, 1080)  # sorted by batch
    assert [(r["batch"], r["outcome"]) for r in rows] == [(5, "ok"), (9, "ok"), (13, "ok"), (17, "oom")]
    # ceiling 13 landed in the sizer's learned store
    assert db.get_learned_batch(db_conn, _learn_key("FakeGPU"), sizer.mp_bucket(2.0736)) == 13


# ── dual torch.compile-mode benchmarking (Also use Torch Compile) ────────────

def test_resolve_modes_dedup_and_default():
    assert vb._resolve_modes({"compile": False}, None) == [False]     # config-derived (CLI default)
    assert vb._resolve_modes({"compile": True}, None) == [True]
    assert vb._resolve_modes({}, [False, True]) == [False, True]      # explicit both
    assert vb._resolve_modes({}, [False, False]) == [False]           # de-duplicated
    assert vb._resolve_modes({}, []) == [False]                       # empty -> off baseline


def test_resolve_bench_keys_available(monkeypatch):
    # Gate keeps compile on -> ON is offered, and its key is the OFF key + the compile suffix.
    monkeypatch.setattr(vb.bv, "_load_config", lambda: {"upscale": {}, "video": {}})
    monkeypatch.setattr(vb.bv, "gate_local_compile", lambda s, log=None: (False, None))
    keys = vb.resolve_bench_keys(remote=False)
    assert keys["compile_available"] is True
    assert keys["on"] == keys["off"] + "|c"
    assert keys["compile_why"] is None


def test_resolve_bench_keys_unavailable(monkeypatch):
    # Gate disables compile -> ON is not offered, and the reason is surfaced for the tooltip.
    monkeypatch.setattr(vb.bv, "_load_config", lambda: {"upscale": {}, "video": {}})

    def _gate(s, log=None):
        s["compile_dit"] = False
        s["compile_vae"] = False
        return True, "Triton is not installed"
    monkeypatch.setattr(vb.bv, "gate_local_compile", _gate)
    keys = vb.resolve_bench_keys(remote=False)
    assert keys["compile_available"] is False
    assert keys["on"] is None
    assert "Triton" in (keys["compile_why"] or "")


def test_run_benchmark_both_modes_write_separate_keys(fake_run, db_conn, monkeypatch):
    """The 'Also use Torch Compile' path: one run sweeps BOTH compile modes, each persisted under
    its own regime-tagged key, so the with- and without-compile results never overwrite each other."""
    # Force the toolchain "available" so the compile-ON sweep actually runs (CI has no compiler).
    monkeypatch.setattr(vb.bv, "gate_local_compile", lambda s, log=None: (False, None))
    summary = vb.run_benchmark(["1080p"], frames=37, resume=False, batch_cap=33,
                               compile_modes=[False, True])
    assert summary["modes"] == 2
    off_cfg = {"upscale": {}, "video": {"compile": False}}
    on_cfg = {"upscale": {}, "video": {"compile": True}}
    off_key = _bench_key(cfg=off_cfg)                       # compile OFF baseline
    on_key = _bench_key(cfg=on_cfg)                         # compile ON
    assert off_key != on_key and on_key == off_key + "|c"
    for key in (off_key, on_key):
        rows = db.get_bench_probes(db_conn, "FakeGPU", key, 1920, 1080)
        assert [(r["batch"], r["outcome"]) for r in rows] == \
            [(5, "ok"), (9, "ok"), (13, "ok"), (17, "oom")], f"key {key} not swept"
    # Each mode wrote its OWN learned batch under its own regime key (they do not collide).
    bucket = sizer.mp_bucket(2.0736)
    assert db.get_learned_batch(db_conn, _learn_key("FakeGPU", cfg=off_cfg), bucket) == 13
    assert db.get_learned_batch(db_conn, _learn_key("FakeGPU", cfg=on_cfg), bucket) == 13


def test_run_benchmark_skips_on_mode_when_compile_unavailable(fake_run, db_conn, monkeypatch):
    # Asked for both modes, but the toolchain can't compile -> only the OFF baseline is swept.
    def _gate(s, log=None):
        s["compile_dit"] = False
        s["compile_vae"] = False
        return True, "no compiler"
    monkeypatch.setattr(vb.bv, "gate_local_compile", _gate)
    summary = vb.run_benchmark(["1080p"], frames=37, resume=False, batch_cap=33,
                               compile_modes=[False, True])
    assert summary["modes"] == 1                            # ON dropped
    off_key = _bench_key(cfg={"upscale": {}, "video": {"compile": False}})
    assert db.get_bench_probes(db_conn, "FakeGPU", off_key, 1920, 1080)          # OFF still swept
    assert db.get_bench_probes(db_conn, "FakeGPU", off_key + "|c", 1920, 1080) == []  # no ON rows


def test_restart_run_only_wipes_the_targets_it_was_asked_for(fake_run, db_conn):
    """End-to-end through run_benchmark, not just the db helper: a resume=False sweep of one
    target must leave every other measured target on the card intact. This is the path the
    GUI's 'Restart' tick drives, and the one that used to wipe the whole card."""
    key = _bench_key()
    # A finished 4K cell the user is NOT re-running, plus a 1080p cell they are.
    db.record_bench_probe(db_conn, "FakeGPU", key, 3840, 2160, 5, "ok", frames=37, seconds=900.0)
    db.record_bench_probe(db_conn, "FakeGPU", key, 1920, 1080, 5, "ok", frames=37, seconds=111.0)

    vb.run_benchmark(["1080p"], frames=37, resume=False, batch_cap=33)

    assert len(db.get_bench_probes(db_conn, "FakeGPU", key, 3840, 2160)) == 1, \
        "the 4K cell was never ticked and must survive the restart"
    # The 1080p cell was re-measured from scratch: the stale 111.0s probe is gone, replaced by
    # the fresh sweep (the fake engine OOMs at 17, so the climb lands 5, 9, 13, 17).
    rows = db.get_bench_probes(db_conn, "FakeGPU", key, 1920, 1080)
    assert [(r["batch"], r["outcome"]) for r in rows] == \
        [(5, "ok"), (9, "ok"), (13, "ok"), (17, "oom")]
    assert rows[0]["seconds"] != 111.0, "the ticked cell really was re-measured, not resumed"


def test_run_benchmark_resumes_from_saved(fake_run, db_conn):
    # Pre-seed a partial sweep (5, 9 already clean); a resume must continue the climb, not redo.
    for b in (5, 9):
        db.record_bench_probe(db_conn, "FakeGPU", _bench_key(), 1920, 1080, b, "ok",
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

        def probe_batch(self, src, dest, *, resolution, batch, frames=None,
                        should_stop=None, on_progress=None, warmup_src=None):
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


def test_the_sweep_warms_each_probe_on_a_batch_plus_one_clip(db_conn, tmp_path, monkeypatch):
    """The short-warmup plumbing end to end: for each probed batch b the runner cuts a
    (b+1)-frame clip and passes it as warmup_src, distinct from the full probe clip. The full
    clip is what gets timed (frames=probe_frames); the short one only absorbs the compile."""
    cuts = []

    def fake_ensure(work, source, frames, **k):
        cuts.append(int(frames))
        return str(tmp_path / f"c_{int(frames)}.mp4")
    monkeypatch.setattr(vb.bclip, "ensure_source_clip", fake_ensure)

    seen = []

    class _Eng:
        def __init__(self, *a, **k):
            self.asked = []

        def probe_batch(self, src, dest, *, resolution, batch, frames=None,
                        should_stop=None, on_progress=None, warmup_src=None):
            self.asked.append(batch)
            seen.append((batch, src, warmup_src))
            return {"outcome": "ok" if batch <= 13 else "oom", "batch": batch, "overlap": 6,
                    "frames": frames, "seconds": 10.0, "peak_alloc_gb": 1.0,
                    "peak_reserved_gb": 1.0}

        def close(self):
            pass

    eng = _Eng()
    import local_video_engine as lve
    monkeypatch.setattr(lve, "LocalVideoEngine", lambda *a, **k: eng)
    monkeypatch.setattr(lve, "_query_gpu_name", lambda: "FakeGPU")
    monkeypatch.setattr(sizer, "free_vram_gb", lambda *a, **k: (None, None))
    monkeypatch.setattr(vb.bv, "_load_config", lambda: {
        "video": {"work_root": str(tmp_path / "work"),
                  "dit_model": "seedvr2_ema_7b_fp16.safetensors"},
        "upscale": {}, "seedvr2": {}})

    vb.run_benchmark(["540x720"], frames=37, resume=False, batch_cap=25)

    # Every probe's warmup clip was the timed clip's SHORTER sibling (b+1 frames), not itself.
    for batch, src, warmup_src in seen:
        assert warmup_src == str(tmp_path / f"c_{batch + 1}.mp4")
        assert src == str(tmp_path / "c_37.mp4")
        assert warmup_src != src
    # And the runner actually asked ffmpeg for those short clips.
    assert all((b + 1) in cuts for b, _, _ in seen)


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
    # The measured climb 5,9,17(oom) + binary-refine mid->13, and nothing else: warming is the
    # ENGINE's job now (inside the process that times the probe), so the sweep asks only for
    # the rungs it records.
    assert engine.asked == [5, 9, 17, 13]
    # probes stored under the RunPod id (returned sorted by batch; 17 is the recorded oom)
    rows = db.get_bench_probes(db_conn, gpu, _bench_key(remote=True), 1920, 1080)
    assert (17, "oom") in [(r["batch"], r["outcome"]) for r in rows]
    # learned batch stored under the PLAIN id (the remote run's read key), NOT gpu|model.
    # The VRAM-regime tag still applies (a pod compiles), so the key is the id + that tag.
    learn = _learn_key(gpu, remote=True)
    assert db.get_learned_batch(db_conn, learn, sizer.mp_bucket(2.0736)) == 13
    assert db.get_learned_batch(db_conn, f"{gpu}|7b", sizer.mp_bucket(2.0736)) is None
    # the remote RUN's auto-tuner seed (get_learned_batch_ge on the same key) now finds it
    assert db.get_learned_batch_ge(db_conn, learn, sizer.mp_bucket(2.0736)) == 13
    assert session.closed is True                            # pod torn down (billing stops)


def test_the_sweep_itself_never_warms_up(db_conn, tmp_path, monkeypatch):
    """Warming moved INTO the engines (each probe warms its own shape in the process that
    times it), so the sweep asks for exactly the rungs it measures -- no warmup prefix, on
    either path. The cell-level warmup that used to sit here could only ever warm ONE batch,
    while static compile makes every rung a distinct shape; and being a separate call, it
    could not warm a LOCAL probe at all (fresh subprocess: the model load and compile's
    dynamo work are per-process). See video_vram_sizer/local_video_worker + the dedicated
    tests in test_video_probe_warmup.py."""
    engine = _FakeEngine()
    session = _FakeSession()
    monkeypatch.setattr(vb, "_deploy_remote_engine", lambda cfg, vcfg: (engine, session))
    monkeypatch.setattr(vb.bclip, "ensure_source_clip", lambda *a, **k: str(tmp_path / "d.mp4"))
    monkeypatch.setattr(vb.bv, "_load_config", lambda: _remote_cfg(tmp_path))
    monkeypatch.setenv("IMGTBX_GPU_OVERRIDE", "GPU-warm")

    vb.run_benchmark(["1080p"], frames=37, resume=False, batch_cap=33, remote=True)

    rows = db.get_bench_probes(db_conn, "GPU-warm", _bench_key(remote=True), 1920, 1080)
    assert len(engine.asked) == len(rows), "every asked probe is a measured, recorded one"
    assert engine.asked == [5, 9, 17, 13]


def test_local_sweep_asks_only_for_the_rungs_it_measures(fake_run, db_conn):
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


# ── terminal summary table ───────────────────────────────────────────────────

def test_log_summary_table(db_conn, monkeypatch):
    gpu, model = "RTX 3090", "7b"
    # 540x720 cell: climb to a ceiling (max fit) of 41, but batch 33 is the FASTEST (2.48 s/f vs
    # 41's 5.37), so the saved batch differs from the ceiling. Fail at 45 pins the ceiling.
    peaks = {5: (20.1, 22.4), 9: (20.1, 22.4), 17: (20.1, 22.4),
             33: (18.5, 21.0), 41: (23.0, 24.0)}
    for b, secs in [(5, 100.0), (9, 90.0), (17, 74.0), (33, 82.0), (41, 220.0)]:
        db.record_bench_probe(db_conn, gpu, model, 540, 720, b, "ok",
                              frames=b, seconds=secs,
                              peak_alloc=peaks[b][0], peak_reserved=peaks[b][1])
    db.record_bench_probe(db_conn, gpu, model, 540, 720, 45, "oom")
    lines = []
    monkeypatch.setattr(vb, "log", lines.append)
    vb._log_summary_table(db_conn, gpu, model, vb.build_plan(["540x720"]))
    text = "\n".join(lines)
    assert "Summary:" in text
    assert "Target" in text and "Batch" in text and "Runtime" in text
    row = next(l for l in lines if l.startswith("540x720"))
    # Fit = ceiling 41; Batch = throughput-optimal 33; peak is the SAVED (33) probe's, not the
    # last probe's; runtime = sum of every ok probe's seconds.
    assert " 41 " in f" {row} "
    assert " 33 " in row
    assert "18.5/21.0" in row
    # 100+90+74+82+220 = 566s -> 00:09:26
    assert "00:09:26" in row


def test_log_summary_table_marks_unbenchmarked(db_conn, monkeypatch):
    lines = []
    monkeypatch.setattr(vb, "log", lines.append)
    vb._log_summary_table(db_conn, "RTX 3090", "7b", vb.build_plan(["4K"]))
    assert any("not benchmarked" in l for l in lines)


# ── completion notification ──────────────────────────────────────────────────

def _capture_notify(monkeypatch):
    """Patch notifications.notify and return the list it records calls into."""
    calls = []
    monkeypatch.setattr(vb.notifications, "notify",
                        lambda settings, title, desc, color, fields=None: calls.append(
                            {"title": title, "desc": desc, "color": color, "fields": fields}))
    return calls


def test_notify_benchmark_complete(monkeypatch):
    calls = _capture_notify(monkeypatch)
    results = [("1080p", 33, 29), ("4K", 9, 9)]
    vb._notify_benchmark({"discord_webhook_url": "x"}, "RTX 3090", "3B", False,
                         results, stopped=None, elapsed=125.0)
    assert len(calls) == 1
    c = calls[0]
    assert c["title"] == "Video benchmark complete"
    assert c["color"] == 0x2ECC71
    # a max-fit above the saved batch is annotated; an equal one is not
    fields = {f["name"]: f["value"] for f in c["fields"]}
    assert fields["1080p"] == "batch 29 (max fit 33)"
    assert fields["4K"] == "batch 9"


def test_notify_benchmark_stopped_is_orange(monkeypatch):
    calls = _capture_notify(monkeypatch)
    vb._notify_benchmark({"ntfy_topic": "t"}, "RTX 3090", "3B", True,
                         [("1080p", None, None)], stopped="stopped by user", elapsed=10.0)
    c = calls[0]
    assert c["title"] == "Video benchmark stopped"
    assert c["color"] == 0xE67E22
    assert "re-run to continue" in c["desc"]
    assert c["fields"][0]["value"] == "no batch fit"


def test_notify_benchmark_noop_without_backend(monkeypatch):
    calls = _capture_notify(monkeypatch)
    vb._notify_benchmark({}, "RTX 3090", "3B", False, [], stopped=None, elapsed=1.0)
    assert calls == []


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


# ── VAE tiling: learned-batch key isolation ──────────────────────────────────
#
# A batch ceiling measured WITH VAE tiling is a different measurement from one measured
# without: tiling bounds the VAE peak that the ceiling is largely made of. If both land on
# one key, a tiled benchmark's (much larger) ceiling gets handed to an untiled run and OOMs
# it -- and via get_learned_batch_ge, which treats a bigger output as a safe bound for a
# smaller one, a single tiled 4K cell poisons every smaller untiled output too.

def test_tile_tag_untiled_is_the_historical_key():
    """Untiled MUST tag to "" so pre-tiling learned rows keep their meaning (they were
    measured untiled) instead of being orphaned by a key change."""
    assert sizer.tile_tag({}) == ""
    assert sizer.tile_tag(None) == ""
    assert sizer.tile_tag({"encode_tiled": False, "decode_tiled": False}) == ""


def test_tile_tag_distinguishes_phase_and_size():
    assert sizer.tile_tag({"decode_tiled": True, "decode_tile_size": 1024}) == "|td1024"
    # tile SIZE changes the peak, so 512 and 1024 are not interchangeable measurements
    assert sizer.tile_tag({"decode_tiled": True, "decode_tile_size": 512}) == "|td512"
    assert sizer.tile_tag({"encode_tiled": True, "encode_tile_size": 1024}) == "|te1024"
    both = sizer.tile_tag({"encode_tiled": True, "decode_tiled": True})
    assert both == "|te1024_d1024"


def test_tiled_ceiling_never_seeds_an_untiled_run(db_conn):
    """The poisoning guard: a tiled run's learned batch is invisible to an untiled lookup."""
    gpu = "NVIDIA RTX PRO 6000"
    tiled = gpu + sizer.tile_tag({"decode_tiled": True, "decode_tile_size": 1024})
    mp_key = sizer.mp_bucket(8.29)                     # 4K landscape output

    db.put_learned_batch(db_conn, tiled, mp_key, 33)   # tiling let a big window fit
    # the untiled run must NOT see it (it would OOM on 33)
    assert db.get_learned_batch(db_conn, gpu, mp_key) is None
    assert db.get_learned_batch_ge(db_conn, gpu, mp_key) is None
    # ... while the tiled run still gets its own value back
    assert db.get_learned_batch(db_conn, tiled, mp_key) == 33


def test_tiled_4k_does_not_poison_smaller_untiled_outputs(db_conn):
    """get_learned_batch_ge treats a LARGER output as a safe bound, so an unkeyed tiled 4K
    row would leak down into 1440p/1080p untiled runs. It must not."""
    gpu = "NVIDIA RTX PRO 6000"
    tiled = gpu + sizer.tile_tag({"decode_tiled": True, "decode_tile_size": 1024})
    db.put_learned_batch(db_conn, tiled, sizer.mp_bucket(8.29), 33)      # tiled 4K
    # an untiled 1440p run looks up a SMALLER key; the tiled 4K row is >= it and would match
    assert db.get_learned_batch_ge(db_conn, gpu, sizer.mp_bucket(3.69)) is None
    # both regimes coexist independently at the same output size
    db.put_learned_batch(db_conn, gpu, sizer.mp_bucket(3.69), 5)         # untiled 1440p
    assert db.get_learned_batch_ge(db_conn, gpu, sizer.mp_bucket(3.69)) == 5
    assert db.get_learned_batch_ge(db_conn, tiled, sizer.mp_bucket(3.69)) == 33


def test_sizer_pick_honours_the_tiling_namespace(db_conn):
    """pick() reads the learned value only under the matching tiling state. Uses a 1440p
    output so the live free-VRAM step-down can't trim the learned value and confound the
    comparison (at 4K with 90 GB free it would step 33 -> 29)."""
    gpu, model = "NVIDIA RTX PRO 6000", "seedvr2_ema_7b_fp16.safetensors"
    tags = sizer.learn_tag({"decode_tiled": True, "decode_tile_size": 1024})
    sizer.record_result(db_conn, gpu, model, 2560, 1440, 33, ok=True, tags=tags)

    tiled_b, _ = sizer.pick(model, 2560, 1440, conn=db_conn, gpu_id=gpu,
                            free_gb=90.0, total_gb=96.0, tags=tags)
    plain_b, _ = sizer.pick(model, 2560, 1440, conn=db_conn, gpu_id=gpu,
                            free_gb=90.0, total_gb=96.0)
    assert tiled_b == 33            # the tiled run gets its own learned ceiling
    assert plain_b == 17            # the untiled run falls back to the conservative seed


def test_sizer_pick_honours_the_compile_namespace(db_conn):
    """The same guard for torch.compile, which is NOT a small effect: on a 3090 at 540x720
    the 7B ceiling is 125 uncompiled and 53 compiled. Sharing one row let a compiled sweep
    overwrite this card's uncompiled learned batch (113 -> 49), silently pinning every later
    uncompiled run ~2x below its real ceiling. Mirrors the tiling test's 1440p setup so the
    free-VRAM step-down can't confound it."""
    gpu, model = "NVIDIA RTX PRO 6000", "seedvr2_ema_7b_fp16.safetensors"
    tags = sizer.learn_tag({"compile_dit": True, "compile_vae": True})
    assert tags == "|c"
    sizer.record_result(db_conn, gpu, model, 2560, 1440, 33, ok=True, tags=tags)

    compiled_b, _ = sizer.pick(model, 2560, 1440, conn=db_conn, gpu_id=gpu,
                               free_gb=90.0, total_gb=96.0, tags=tags)
    plain_b, _ = sizer.pick(model, 2560, 1440, conn=db_conn, gpu_id=gpu,
                            free_gb=90.0, total_gb=96.0)
    assert compiled_b == 33         # the compiled run gets its own learned ceiling
    assert plain_b == 17            # the uncompiled run must NOT inherit it


def test_learn_tag_composes_tiling_and_compile_independently():
    """Both regimes move the ceiling, so a row measured under one must never serve the other,
    and a row measured under BOTH must serve only that pair. All-off stays "" so every row
    written before either tag existed keeps its meaning."""
    assert sizer.learn_tag({}) == ""
    assert sizer.learn_tag({"compile_dit": True}) == "|c"
    assert sizer.learn_tag({"compile_dit": True, "compile_dynamic": True}) == "|cd"
    assert sizer.learn_tag({"decode_tiled": True, "decode_tile_size": 1024}) == "|td1024"
    both = sizer.learn_tag({"decode_tiled": True, "decode_tile_size": 1024,
                            "compile_dit": True})
    assert both == "|td1024|c"
    assert len({sizer.learn_tag({}), sizer.learn_tag({"compile_dit": True}),
                sizer.learn_tag({"decode_tiled": True, "decode_tile_size": 1024}),
                both}) == 4, "each regime combination needs its own row"


def test_tiled_probes_do_not_collide_with_untiled_history(db_conn):
    """The probe store (video_bench) must namespace on tiling too, or the sweep breaks BOTH
    ways: `resume` reads the previous regime's OOMs and declares the cell infeasible without
    probing anything, and `resume=False` clears the other regime's history to make room.

    Regression for the real case: a PRO 6000 with untiled 4K OOMs at batch 5 and 13 in the DB
    would have refused to run a single TILED 4K probe and reported '4K infeasible' -- after
    deploying and billing the pod."""
    gpu, untiled = "NVIDIA RTX PRO 6000", "7b"
    tiled = untiled + sizer.tile_tag({"decode_tiled": True, "decode_tile_size": 1024})
    for b in (5, 13):
        db.record_bench_probe(db_conn, gpu, untiled, 3840, 2160, b, "oom")

    floor = sizer.vram_floor_batch(96.0, 3840 * 2160 / 1_000_000.0)
    # untiled: history says the floor already failed -> nothing left to try
    assert vb.next_batch(vb.drop_collapsed(
        db.get_bench_probes(db_conn, gpu, untiled, 3840, 2160)), floor, 3000) is None
    # tiled: a clean slate, so the sweep opens at the floor and actually measures
    assert vb.next_batch(vb.drop_collapsed(
        db.get_bench_probes(db_conn, gpu, tiled, 3840, 2160)), floor, 3000) == floor

    # a tiled probe must not overwrite the untiled row at the same (card, size, batch)
    db.record_bench_probe(db_conn, gpu, tiled, 3840, 2160, 13, "ok", frames=9, seconds=100.0)
    untiled_rows = {p["batch"]: p["outcome"]
                    for p in db.get_bench_probes(db_conn, gpu, untiled, 3840, 2160)}
    assert untiled_rows == {5: "oom", 13: "oom"}      # control arm intact
    tiled_rows = {p["batch"]: p["outcome"]
                  for p in db.get_bench_probes(db_conn, gpu, tiled, 3840, 2160)}
    assert tiled_rows == {13: "ok"}


def test_clearing_a_tiled_benchmark_keeps_untiled_history(db_conn):
    """resume=False clears only the regime being re-run, so re-benchmarking tiled can't
    destroy the untiled baseline it is meant to be compared against."""
    gpu, untiled = "NVIDIA RTX PRO 6000", "7b"
    tiled = untiled + sizer.tile_tag({"decode_tiled": True, "decode_tile_size": 1024})
    db.record_bench_probe(db_conn, gpu, untiled, 2560, 1440, 45, "ok", frames=9, seconds=50.0)
    db.record_bench_probe(db_conn, gpu, tiled, 2560, 1440, 45, "ok", frames=9, seconds=60.0)

    db.clear_bench(db_conn, gpu, tiled)
    assert db.get_bench_probes(db_conn, gpu, tiled, 2560, 1440) == []
    assert len(db.get_bench_probes(db_conn, gpu, untiled, 2560, 1440)) == 1


# ── warmup shape + benchmark compile mode ────────────────────────────────────

def test_benchmark_does_not_force_compile_dynamic():
    """REVERTED. compile_dynamic (one graph for every batch) was set by the benchmark and cost a
    rented PRO 6000 a >32-min unfinished single-threaded cold compile, against a ~9-min static
    baseline. Its benefit is comparable rates across rungs that SUCCEED, and a 4K cell has
    exactly one -- so it solved a problem that cell does not have, at an unbounded price.
    Nothing may turn it on by default again without a measurement."""
    import batch_video_upscale as bv
    cfg = {"upscale": {}, "video": {}}
    ws = vb._worker_settings(cfg, bv.resolve_video_cfg(cfg))
    assert ws["compile_dit"] and ws["compile_vae"], "compile itself stays on"
    assert not ws.get("compile_dynamic"), "the benchmark must not force dynamic compile"


def test_compile_dynamic_stays_reachable_from_config():
    """Reverted, not removed: it must stay measurable without a code edit, or the next person
    re-litigates it from opinion instead of a number."""
    import batch_video_upscale as bv
    cfg = {"upscale": {"compile_dynamic": True}, "video": {}}
    ws = vb._worker_settings(cfg, bv.resolve_video_cfg(cfg))
    assert ws.get("compile_dynamic") is True, "an explicit config opt-in must survive"


def test_the_benchmark_no_longer_owns_a_warmup():
    """The cell-level warmup is GONE, and its three lessons are now structural or live with
    the mechanism that replaced it:

      * "warm the probes' CODE PATH at a batch that survives" -- a probe now warms itself, on
        its own clip at its own batch, so it cannot warm a graph no probe runs (the old
        "bs9 warmup fine" above "batch 9: oom") nor pick a rung that OOMs.
      * "a warmup OOM must not read as success" -- an OOM while warming now IS the probe's
        outcome: the batch does not fit, which is the answer the sweep wanted anyway. Pinned
        in test_video_probe_warmup.py for both engines.
      * "one warmup per cell is enough" -- it never was. Static compile makes every rung a
        distinct shape, so one warmed batch left every other rung paying ~30s of compile
        inside its own measurement.

    This test exists so the removal is deliberate: re-adding a sweep-level warmup would put
    back a mechanism that cannot warm a local probe (fresh subprocess per probe) and only
    ever warmed one shape of many.
    """
    assert not hasattr(vb, "_warmup_cell")
    assert not hasattr(vb, "WARMUP_PROBES")


# ── compile namespacing (video_bench) ────────────────────────────────────────

def test_compile_tag_separates_uncompiled_static_and_dynamic():
    """torch.compile moves the RATE, and video_bench rows carry rates. "" MUST mean
    uncompiled: every local row on disk was measured with compile gated off (no C compiler),
    so the reverse convention would make a compiled sweep RESUME those rungs and publish
    their seconds as its own."""
    assert sizer.compile_tag({}) == ""
    assert sizer.compile_tag({"compile_dit": False, "compile_vae": False}) == ""
    assert sizer.compile_tag({"compile_dit": True}) == "|c"
    assert sizer.compile_tag({"compile_vae": True}) == "|c"
    # static and dynamic are different measurements; telling them apart is the point.
    assert sizer.compile_tag({"compile_dit": True, "compile_dynamic": True}) == "|cd"
    # dynamic is meaningless with compile off and must not invent a namespace
    assert sizer.compile_tag({"compile_dynamic": True}) == ""


def test_bench_key_carries_both_tiling_and_compile():
    import batch_video_upscale as bv
    cfg = {"upscale": {"decode_tiled": True, "decode_tile_size": 1024}, "video": {}}
    ws = vb._worker_settings(cfg, bv.resolve_video_cfg(cfg))
    key = sizer.model_tag(ws["dit_model"]) + sizer.tile_tag(ws) + sizer.compile_tag(ws)
    assert key == "7b|td1024|c"


def test_a_compiled_sweep_cannot_resume_uncompiled_rows(db_conn):
    """THE regression, with the real numbers. The 3090's 540x720 cell is finished and
    UNCOMPILED (10:21 of probes, ceiling 125). Under one shared key a compiled re-run would
    skip the cell as done (resume) or clear it to re-measure. Separate keys give the compiled
    run a clean namespace and leave the baseline. (The other half of that protection, scoping
    a restart to the ticked targets, is covered by the clear_bench cell tests below.)"""
    gpu = "NVIDIA GeForce RTX 3090"
    plain = sizer.model_tag("seedvr2_ema_7b_fp16.safetensors")          # uncompiled: "7b"
    comp = plain + sizer.compile_tag({"compile_dit": True})             # compiled:   "7b|c"
    for b, oc, secs in ((5, "ok", 113.0), (125, "ok", 93.9), (129, "oom", 27.5)):
        db.record_bench_probe(db_conn, gpu, plain, 540, 720, b, oc, frames=37, seconds=secs)

    assert db.get_bench_probes(db_conn, gpu, comp, 540, 720) == [], "compiled starts empty"
    assert len(db.get_bench_probes(db_conn, gpu, plain, 540, 720)) == 3

    # Forcing a re-measure of the compiled regime must not touch the uncompiled baseline.
    db.clear_bench(db_conn, gpu, comp)
    assert len(db.get_bench_probes(db_conn, gpu, plain, 540, 720)) == 3, \
        "the uncompiled 10:21 baseline must survive a compiled resume=False run"


# ── the GUI must read the key the runner writes ───────────────────────────────

def _quiet(*_a, **_k):
    pass


def test_resolve_bench_key_finds_the_rows_the_runner_wrote(fake_run, db_conn):
    """THE contract. The benchmark window reads its results table with resolve_bench_key(); the
    runner writes with bench_key(). Diverge and the window shows another regime's rows, which is
    exactly what a bare model tag did: it read "7b" while a compiled sweep wrote "7b|c", so the
    table showed the stale uncompiled baseline for the whole run."""
    vb.run_benchmark(["1080p"], frames=37, resume=False, batch_cap=33)

    key = vb.resolve_bench_key(remote=False, log_fn=_quiet)
    assert db.get_bench_probes(db_conn, "FakeGPU", key, 1920, 1080), \
        "the GUI's key must find the rows the runner just wrote"


def test_resolve_bench_key_follows_the_gate_not_the_config(monkeypatch, tmp_path):
    """The compile tag must describe the RUN, not the wish. config compile=True on a machine the
    gate disables means an UNCOMPILED run, and tagging it "|c" would file uncompiled seconds
    under the compiled key: the precise lie the tag exists to prevent."""
    monkeypatch.setattr(vb.bv, "_load_config", lambda: {
        "video": {"work_root": str(tmp_path), "compile": True,
                  "dit_model": "seedvr2_ema_7b_fp16.safetensors"},
        "upscale": {}, "seedvr2": {}})

    monkeypatch.setattr(vb.bv, "gate_local_compile", lambda s, log=None: (False, None))
    assert vb.resolve_bench_key(log_fn=_quiet) == "7b|c", "gate allows compile -> tagged"

    def _deny(settings, log=None):
        settings["compile_dit"] = settings["compile_vae"] = False
        return True, "no C compiler"
    monkeypatch.setattr(vb.bv, "gate_local_compile", _deny)
    assert vb.resolve_bench_key(log_fn=_quiet) == "7b", "gate denies compile -> untagged"


def test_resolve_bench_key_remote_ignores_the_local_compiler_gate(monkeypatch, tmp_path):
    """Remote must never apply THIS machine's compiler gate: the pod has its own compiler and
    runs its own _gate_compile. A dev box with no MSVC would otherwise label a compiled POD
    sweep "7b" and read the wrong rows for a run it is paying for."""
    monkeypatch.setattr(vb.bv, "_load_config", lambda: {
        "video": {"work_root": str(tmp_path), "compile": True,
                  "dit_model": "seedvr2_ema_7b_fp16.safetensors"},
        "upscale": {}, "seedvr2": {}})

    def _boom(settings, log=None):
        raise AssertionError("the local gate must not run for a remote benchmark")
    monkeypatch.setattr(vb.bv, "gate_local_compile", _boom)

    assert vb.resolve_bench_key(remote=True, log_fn=_quiet) == "7b|c"


def test_bench_key_is_bare_when_both_features_are_off():
    """The historical key. Every existing probe was measured untiled + uncompiled, so both tags
    must stay "" or 40 rows across 7 targets silently orphan."""
    vcfg = {"dit_model": "seedvr2_ema_7b_fp16.safetensors"}
    assert vb.bench_key(vcfg, {"compile_dit": False, "decode_tiled": False}) == "7b"


# ── restart scoping: a wipe must reach only the ticked targets ────────────────

def _seed_seven_targets(conn, gpu, model="7b"):
    """The 3090's real shape: several finished cells on one card+model."""
    cells = [(540, 720), (720, 540), (1920, 1080), (1440, 1080),
             (2560, 1440), (1080, 1440), (3840, 2160)]
    for w, h in cells:
        db.record_bench_probe(conn, gpu, model, w, h, 5, "ok", frames=37, seconds=113.0)
    return cells


def test_restart_of_one_target_keeps_every_other_cell(db_conn):
    """THE bug this fix exists for. Re-measuring 540x720 used to discard all 7 targets on
    the card: hours of GPU time, silently, with nothing to resume next sweep."""
    gpu = "NVIDIA GeForce RTX 3090"
    cells = _seed_seven_targets(db_conn, gpu)
    assert len(cells) == 7

    db.clear_bench(db_conn, gpu, "7b", cells=[(540, 720)])

    assert db.get_bench_probes(db_conn, gpu, "7b", 540, 720) == [], "the ticked cell is cleared"
    for w, h in cells[1:]:
        assert len(db.get_bench_probes(db_conn, gpu, "7b", w, h)) == 1, \
            f"{w}x{h} was not ticked and must survive"


def test_restart_clears_exactly_the_ticked_targets(db_conn):
    """A multi-target restart scopes to the whole selection, not just its first entry."""
    gpu = "NVIDIA GeForce RTX 3090"
    cells = _seed_seven_targets(db_conn, gpu)
    picked = [(540, 720), (2560, 1440)]

    db.clear_bench(db_conn, gpu, "7b", cells=picked)

    for w, h in picked:
        assert db.get_bench_probes(db_conn, gpu, "7b", w, h) == []
    for w, h in [c for c in cells if c not in picked]:
        assert len(db.get_bench_probes(db_conn, gpu, "7b", w, h)) == 1


def test_restart_scope_respects_the_model_key_too(db_conn):
    """Cell scoping stacks with model scoping: re-measuring compiled 540x720 leaves both the
    uncompiled 540x720 baseline AND the compiled other targets alone."""
    gpu = "NVIDIA GeForce RTX 3090"
    db.record_bench_probe(db_conn, gpu, "7b", 540, 720, 125, "ok", frames=125, seconds=93.9)
    db.record_bench_probe(db_conn, gpu, "7b|c", 540, 720, 73, "ok", frames=73, seconds=113.1)
    db.record_bench_probe(db_conn, gpu, "7b|c", 1920, 1080, 9, "ok", frames=37, seconds=60.0)

    db.clear_bench(db_conn, gpu, "7b|c", cells=[(540, 720)])

    assert db.get_bench_probes(db_conn, gpu, "7b|c", 540, 720) == []
    assert len(db.get_bench_probes(db_conn, gpu, "7b", 540, 720)) == 1, "uncompiled baseline"
    assert len(db.get_bench_probes(db_conn, gpu, "7b|c", 1920, 1080)) == 1, "other target"


def test_empty_cell_scope_clears_nothing(db_conn):
    """cells=[] is an explicitly empty selection. It must NOT fall through to the card-wide
    delete, or a caller computing a target list would wipe the card on an empty one."""
    gpu = "NVIDIA GeForce RTX 3090"
    _seed_seven_targets(db_conn, gpu)

    db.clear_bench(db_conn, gpu, "7b", cells=[])

    assert len(db.get_bench_probes(db_conn, gpu, "7b")) == 7


def test_unscoped_clear_still_wipes_the_card(db_conn):
    """cells=None keeps the old card-wide behaviour for callers that really mean it."""
    gpu = "NVIDIA GeForce RTX 3090"
    _seed_seven_targets(db_conn, gpu)

    db.clear_bench(db_conn, gpu, "7b")

    assert db.get_bench_probes(db_conn, gpu, "7b") == []
