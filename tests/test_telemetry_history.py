"""
TelemetryHistory: the per-run, in-memory buffer feeding the graph window (#9).
Pure/offline (no GUI, no matplotlib). Covers the record-only-while-live rule,
reset-on-restart, runtime/enabled-spans, gap-breaking and None handling.
"""

import math

from system_telemetry import TelemetryHistory, HISTORY_SPANS


def _sample(**kw):
    base = dict(cpu=10, ram_used_mb=8000, ram_total_mb=32000,
                gpu_used_mb=4000, gpu_total_mb=24000, gpu_temp_c=50,
                gpu_util_pct=80, gpu_power_w=200, gpu_power_limit_w=350,
                gpu_clock_mhz=1800)
    base.update(kw)
    return base


def test_records_only_while_live():
    h = TelemetryHistory()
    h.append(100, _sample())            # before start -> ignored
    assert len(h) == 0 and not h.is_live
    h.start(100)
    assert h.is_live and not h.is_sealed
    h.append(105, _sample(cpu=20))
    h.append(110, _sample(cpu=30))
    assert len(h) == 2
    h.seal()
    assert h.is_sealed and not h.is_live
    h.append(115, _sample(cpu=40))      # after seal -> ignored
    assert len(h) == 2
    assert h.latest()["cpu"] == 30


def test_start_resets_previous_run():
    h = TelemetryHistory()
    h.start(0); h.append(1, _sample()); h.append(2, _sample()); h.seal()
    assert len(h) == 2
    h.start(1000)                       # new run discards the old
    assert len(h) == 0 and h.is_live and h.start_ts == 1000
    assert h.latest() is None


def test_runtime_and_enabled_spans():
    h = TelemetryHistory()
    h.start(0)
    assert h.runtime() == 0.0 and h.enabled_spans() == []
    h.append(0, _sample())
    h.append(3600, _sample())           # exactly 1h of runtime
    spans = dict(h.enabled_spans())
    assert "1h" in spans and "3h" not in spans
    h.append(10800 + 1, _sample())      # past 3h
    spans = dict(h.enabled_spans())
    assert "1h" in spans and "3h" in spans and "6h" not in spans
    # never enables a span the run hasn't reached
    assert all(lbl in dict(HISTORY_SPANS) for lbl, _ in h.enabled_spans())


def test_series_plain_field_and_bounds():
    h = TelemetryHistory()
    h.start(0)
    for t in (0, 5, 10):
        h.append(t, _sample(cpu=t))
    times, vals = h.series("cpu")
    assert times == [0.0, 5.0, 10.0]
    assert vals == [0.0, 5.0, 10.0]
    assert h.bounds() == (0, 10)


def test_series_callable_accessor():
    h = TelemetryHistory()
    h.start(0)
    h.append(0, _sample(gpu_used_mb=6000, gpu_total_mb=24000))
    times, vals = h.series(lambda s: s["gpu_used_mb"] * 100.0 / s["gpu_total_mb"])
    assert vals == [25.0]


def test_series_none_value_becomes_nan():
    h = TelemetryHistory()
    h.start(0)
    h.append(0, _sample(gpu_power_limit_w=None))
    _times, vals = h.series("gpu_power_limit_w")
    assert len(vals) == 1 and math.isnan(vals[0])


def test_series_breaks_line_on_time_gap():
    h = TelemetryHistory()
    h.start(0)
    # steady 5 s cadence, then a big gap, then resume
    for t in (0, 5, 10, 15):
        h.append(t, _sample(cpu=1))
    h.append(215, _sample(cpu=2))       # 200 s gap >> 3x median (5 s)
    h.append(220, _sample(cpu=2))
    times, vals = h.series("cpu")
    nan_idx = [i for i, v in enumerate(vals) if math.isnan(v)]
    assert len(nan_idx) == 1                     # exactly one break inserted
    gap_t = times[nan_idx[0]]
    assert 15 < gap_t < 215                       # the break sits inside the gap
