"""
Local Video Upscaler terminal-log behaviour:

1. The SeedVR2 engine's own stdout (setup banners like 'SeedVR2 optimizations check…',
   'Creating DiT model…', tqdm tails, the engine's own timestamped spacer lines) goes to the
   run-log FILE only, never the GUI terminal -- it clutters the terminal and doubles the
   timestamp (the engine stamps its own, then the GUI adds one more).
2. A long multi-chunk segment reports LIVE: each 'Chunk X/N' marker the engine prints becomes
   one clean per-chunk progress line on the terminal, so the run isn't silent for 20 minutes.

LocalVideoEngine captures the engine's stdout through `_LiveEngineOutput` (line-streamed, so it
lands in the file as it happens, not buffered to segment end) and re-emits the chunk markers via
`_maybe_emit_chunk` to the SAVED real stdout (bypassing the capture). batch_video_upscale passes
`diag_sink=log_file_only` and `seg_index`/`seg_total`. No GPU needed: UpscaleEngine is faked.
"""

import sys
import types

import pytest

import local_video_engine as l


# ── _LiveEngineOutput (pure) ─────────────────────────────────────────────────

def test_live_output_emits_each_completed_line():
    got = []
    w = l._LiveEngineOutput(got.append)
    w.write("Creating DiT model\nVAE model cached\n")
    assert got == ["Creating DiT model", "VAE model cached"]


def test_live_output_buffers_until_newline():
    got = []
    w = l._LiveEngineOutput(got.append)
    w.write("partial ")
    assert got == []                                  # no newline yet -> nothing emitted
    w.write("line done\n")
    assert got == ["partial line done"]


def test_live_output_collapses_carriage_return_redraws():
    got = []
    w = l._LiveEngineOutput(got.append)
    w.write("bar 10%\rbar 50%\rbar 100%\n")           # tqdm-style in-place redraws
    assert got == ["bar 100%"]


def test_live_output_drops_blank_lines():
    got = []
    w = l._LiveEngineOutput(got.append)
    w.write("keep\n   \n\nkeep2\n")
    assert got == ["keep", "keep2"]


def test_live_output_close_flushes_trailing_partial():
    got = []
    w = l._LiveEngineOutput(got.append)
    w.write("no newline yet")
    w.close()
    assert got == ["no newline yet"]


def test_live_output_is_fail_safe():
    l._LiveEngineOutput(lambda _s: (_ for _ in ()).throw(RuntimeError("boom"))).write("x\n")


# ── in-process capture + live per-chunk progress ─────────────────────────────

class _FakeEngine:
    """Stands in for UpscaleEngine. When process_video runs it PRINTS engine-style output --
    setup banners and the per-chunk 'Chunk X/N' markers -- to stdout, exactly as the real one
    does; the redirect installed by LocalVideoEngine decides where those land."""

    def __init__(self, *a, **k):
        self.args = types.SimpleNamespace(dit_model="m", attention_mode="auto")
        self.device_name = "TestGPU"
        self.resident = False
        self.captured = []

    def process_video(self, *a, capture=False, **k):
        self.captured.append(capture)
        print("[22:07:50.727]  Requested output format: mp4")
        print("Chunk 1/3: 98 new + 0 context frames")
        print("Chunk 2/3: 98 new + 8 context frames")
        print("Chunk 3/3: 98 new + 8 context frames")
        return 294


@pytest.fixture
def fake_engine(monkeypatch):
    import upscale_engine
    monkeypatch.setattr(upscale_engine, "UpscaleEngine", _FakeEngine)


def test_engine_stdout_to_file_and_chunks_to_terminal(fake_engine, capsys):
    file_lines = []
    eng = l.LocalVideoEngine("repo", "model", {"dit_model": "m"}, diag_sink=file_lines.append)
    eng._seg_ctx = (0, 1, 294)                          # segment 1/1, 294 frames
    eng._chunk_state = {"last": 0}
    eng._real_stdout = sys.stdout                        # saved before the redirect (as process_segment does)

    n, _a, _r = eng._run_attempt("s", "d", 1080, 97, 98, 8, None, "opencv", False, None, None)
    assert n == 294
    assert eng._engine.captured == [False]              # our own redirect, not the engine buffer

    # The raw engine stdout (banner + the raw Chunk lines) went to the FILE sink.
    assert "[22:07:50.727]  Requested output format: mp4" in file_lines
    assert any(x.startswith("Chunk 1/3") for x in file_lines)

    # The TERMINAL got one clean per-chunk line each, with segment context + frame count.
    out = capsys.readouterr().out
    assert "    segment 1/1, chunk 1/3 of 294 frames …" in out
    assert "    segment 1/1, chunk 2/3 of 294 frames …" in out
    assert "    segment 1/1, chunk 3/3 of 294 frames …" in out
    # The raw engine banner must NOT be on the terminal.
    assert "Requested output format" not in out


def test_no_diag_sink_keeps_live_stdout(fake_engine, capsys):
    # Without a sink (e.g. the benchmark), behaviour is unchanged: the engine prints live to
    # stdout and no per-chunk re-emission happens.
    eng = l.LocalVideoEngine("repo", "model", {"dit_model": "m"})
    eng._run_attempt("s", "d", 1080, 97, 98, 8, None, "opencv", False, None, None)
    assert eng._engine.captured == [False]
    out = capsys.readouterr().out
    assert "Requested output format" in out             # raw engine output reaches the terminal
    assert "segment 1/1, chunk" not in out              # no clean re-emission


# ── construction-time banner capture ─────────────────────────────────────────

class _BannerEngine(_FakeEngine):
    """Prints seedvr2-style import banners during __init__ (as compatibility.py does on first
    import), to prove construction is captured, not just the per-segment call."""

    def __init__(self, *a, **k):
        print("SeedVR2 optimizations check: Triton OK")
        print("   ")                                     # blank spacer -> dropped
        print("Conv3d workaround active")
        super().__init__(*a, **k)


def test_construction_banners_go_to_file_not_terminal(monkeypatch, capsys):
    import upscale_engine
    monkeypatch.setattr(upscale_engine, "UpscaleEngine", _BannerEngine)
    lines = []
    l.LocalVideoEngine("repo", "model", {"dit_model": "m"}, diag_sink=lines.append)
    assert capsys.readouterr().out == ""                 # nothing leaked to the terminal
    assert lines == ["SeedVR2 optimizations check: Triton OK", "Conv3d workaround active"]


def test_construction_uncaptured_without_sink(monkeypatch, capsys):
    import upscale_engine
    monkeypatch.setattr(upscale_engine, "UpscaleEngine", _BannerEngine)
    l.LocalVideoEngine("repo", "model", {"dit_model": "m"})   # no sink
    assert "optimizations check" in capsys.readouterr().out  # printed live, as before
