"""
The remote GPU picker must not offer a card that can't run the selected SeedVR2 job.

A 16 GB card (e.g. RTX 2000 Ada) is physically incapable of a 4K SeedVR2 upscale, yet the
picker used to list it (the list was gated by the QUEUE's floor, not by the target being
added, and the Target combo never re-filtered the card list). VideoTab._seedvr2_gpu_ok is the
rule that fixes it: a card qualifies only when it clears the target's VRAM floor (>= 32 GB, and
the higher per-target floor for 1440p/4K) OR has been successfully benchmarked / already run to
at least that target's output size. Real-ESRGAN is not gated (it tiles on OOM).

These drive the REAL predicate on a minimal fake tab (no tkinter window), pinning the fix so a
future refactor can't silently re-open the "impossible card offered" bug.
"""

import pytest

pytest.importorskip("tkinter")

import db                                   # noqa: E402
import video_estimate as ve                 # noqa: E402
from gui.tab_video import VideoTab           # noqa: E402


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


class FakeVideoTab:
    """Carries just what _seedvr2_gpu_ok touches: a db connection accessor."""
    _seedvr2_gpu_ok = VideoTab._seedvr2_gpu_ok

    def __init__(self, conn):
        self._conn_obj = conn

    def _conn(self):
        return self._conn_obj


def _gpu(name, gb):
    return {"id": name, "name": name, "memory_gb": gb}


# A landscape 1080p source: output at 4K = 3840x2160 (8.29 MP), at 1080p = 1920x1080 (2.07 MP).
SRC_W, SRC_H = 1920, 1080


def test_small_card_rejected_for_4k(db_conn):
    tab = FakeVideoTab(db_conn)
    assert tab._seedvr2_gpu_ok(_gpu("RTX 2000 Ada", 16), "4K", SRC_W, SRC_H) is False
    assert tab._seedvr2_gpu_ok(_gpu("RTX 2000 Ada", 16), "1080p", SRC_W, SRC_H) is False


def test_32gb_card_passes_1080p_but_not_4k(db_conn):
    tab = FakeVideoTab(db_conn)
    # 1080p floor is 32; a 32 GB card clears it. 4K floor is 90; a 32 GB card does not.
    assert tab._seedvr2_gpu_ok(_gpu("card-32", 32), "1080p", SRC_W, SRC_H) is True
    assert tab._seedvr2_gpu_ok(_gpu("card-32", 32), "4K", SRC_W, SRC_H) is False
    assert tab._seedvr2_gpu_ok(_gpu("card-32", 32), "1440p", SRC_W, SRC_H) is False


def test_big_card_passes_every_target(db_conn):
    tab = FakeVideoTab(db_conn)
    for t in ("1080p", "1440p", "4K"):
        assert tab._seedvr2_gpu_ok(_gpu("H200", 141), t, SRC_W, SRC_H) is True


def test_benchmarked_small_card_is_offered(db_conn):
    """A 24 GB card proven (benchmarked / already run) to a target is offered below the floor."""
    tab = FakeVideoTab(db_conn)
    # No proof yet: a 24 GB card is below the 1080p floor of 32, so it is rejected.
    assert tab._seedvr2_gpu_ok(_gpu("RTX 3090", 24), "1080p", SRC_W, SRC_H) is False
    # Record a successful 1080p SeedVR2 probe (2.07 MP output) for this card.
    db.record_bench_probe(db_conn, "RTX 3090", "7b", out_w=1920, out_h=1080, batch=1,
                          outcome="ok", frames=8, seconds=8.0)
    assert tab._seedvr2_gpu_ok(_gpu("RTX 3090", 24), "1080p", SRC_W, SRC_H) is True
    # But it is still NOT offered for 4K, which it has not proven.
    assert tab._seedvr2_gpu_ok(_gpu("RTX 3090", 24), "4K", SRC_W, SRC_H) is False


def test_esrgan_4k_probe_does_not_qualify_a_small_card_for_seedvr2(db_conn):
    """The real leak: a 16 GB card benchmarked for Real-ESRGAN at a 4K-sized output (a GAN
    tiles on OOM, so it reaches 4K on a small card) must NOT be read as feasible for a 4K
    SeedVR2 job. SeedVR2's ceiling excludes ESRGAN ('esrgan-*' bench model) probes."""
    tab = FakeVideoTab(db_conn)
    # The ESRGAN benchmark's e1080 2X cell: 1920x1080 -> 3840x2160 (8.29 MP), outcome ok.
    db.record_bench_probe(db_conn, "RTX 2000 Ada", "esrgan-quality", out_w=3840, out_h=2160,
                          batch=1, outcome="ok", frames=8, seconds=2.0)
    # That success is an ESRGAN success, not a SeedVR2 one: the card stays rejected for 4K
    # SeedVR2 (and every SeedVR2 target, being only 16 GB).
    assert db.max_feasible_output_mp(db_conn, "RTX 2000 Ada") is None
    assert tab._seedvr2_gpu_ok(_gpu("RTX 2000 Ada", 16), "4K", SRC_W, SRC_H) is False
    assert tab._seedvr2_gpu_ok(_gpu("RTX 2000 Ada", 16), "1080p", SRC_W, SRC_H) is False


# ── the regime the proof was measured under (0.6.3) ──────────────────────────

def test_a_compiled_run_is_not_offered_a_card_only_proven_uncompiled(db_conn):
    """This project's own 3090, with its own numbers: `7b` reaches 1920x1080, `7b|c` OOMs
    there. Unscoped, the guard took the max over both and offered a COMPILED run a card
    whose compiled measurements say it cannot. This is the half that already bites, and it
    predates #26 Part A: compile has been in the bench key for a long time, it just never
    reached this function.

    Reaches for the effect through the real `_seedvr2_gpu_ok`, not through the db helper,
    because the guard has to actually pass the regime down for any of this to matter."""
    tab = FakeVideoTab(db_conn)
    db.record_bench_probe(db_conn, "RTX 3090", "7b", out_w=1920, out_h=1080, batch=5,
                          outcome="ok", frames=8, seconds=8.0)
    db.record_bench_probe(db_conn, "RTX 3090", "7b|c", out_w=1280, out_h=960, batch=5,
                          outcome="ok", frames=8, seconds=8.0)

    assert tab._seedvr2_gpu_ok(_gpu("RTX 3090", 24), "1080p", SRC_W, SRC_H,
                               "7b") is True
    assert tab._seedvr2_gpu_ok(_gpu("RTX 3090", 24), "1080p", SRC_W, SRC_H,
                               "7b|c") is False
    # Omitted, it behaves exactly as it did before this shipped.
    assert tab._seedvr2_gpu_ok(_gpu("RTX 3090", 24), "1080p", SRC_W, SRC_H) is True


def test_a_light_models_proof_does_not_open_a_card_for_a_heavy_job(db_conn):
    """#26 Part A made ten weights selectable, so a 4K probe under the 1.86 GiB 3B-Q4 could
    qualify a card for the 15.35 GiB 7B FP16 at 4K. Not yet realised in any corpus, but
    reachable from the moment the picklists grew."""
    tab = FakeVideoTab(db_conn)
    db.record_bench_probe(db_conn, "RTX 3090", "3b_q4", out_w=3840, out_h=2160, batch=5,
                          outcome="ok", frames=8, seconds=8.0)
    assert tab._seedvr2_gpu_ok(_gpu("RTX 3090", 24), "4K", SRC_W, SRC_H, "3b_q4") is True
    assert tab._seedvr2_gpu_ok(_gpu("RTX 3090", 24), "4K", SRC_W, SRC_H, "7b") is False


def test_the_tab_resolves_a_regime_at_all():
    """_bench_regime feeds every call site and is wrapped in a bare except returning None,
    which would silently restore the old behaviour everywhere. So this asserts it produces
    a REAL key: the same failure shape as the resolve_bench_key/resolve_bench_keys mismatch
    that made a whole remote benchmark window collapse to one row.

    Runs the real method against a minimal fake rather than a live VideoTab, the way
    FakeVideoTab above does. Building a whole tab starts background threads that outlive
    root.destroy() and starved another test's scan wait, which is a poor trade for exercising
    two attribute reads."""
    class _FakeMode:
        def get(self):
            return "local"

    class _FakeTab:
        _bench_regime = VideoTab._bench_regime
        mode_var = _FakeMode()

        def _selected_method(self):
            return ("seedvr2", None)                   # None = whatever config selects

    regime = _FakeTab()._bench_regime()
    assert regime, "no regime resolved: every call site silently stops filtering"
    assert regime.split("|")[0] in ("7b", "7b_fp8", "7b_q4",
                                    "3b", "3b_fp16", "3b_fp8", "3b_q4")


def test_the_regime_follows_the_picked_model():
    """The model half has to come from the METHOD picker, not from config, or a queue item
    picked with a light model would be gated as if it were the configured heavy one."""
    class _FakeMode:
        def get(self):
            return "local"

    def _regime_for(model):
        class _FakeTab:
            _bench_regime = VideoTab._bench_regime
            mode_var = _FakeMode()

            def _selected_method(self):
                return ("seedvr2", model)

        return _FakeTab()._bench_regime()

    assert _regime_for("seedvr2_ema_3b-Q4_K_M.gguf").split("|")[0] == "3b_q4"
    assert _regime_for("seedvr2_ema_7b_fp16.safetensors").split("|")[0] == "7b"
