"""
Telemetry sampling: the widened GPU query + per-field parse (0.5.3, telemetry
graphs #9 foundation). Verifies that

  * sample_gpu returns the full dict of GPU fields,
  * a card that reports "[N/A]"/blank for a field yields None for JUST that field
    (not a dropped sample),
  * the local sampler (system_telemetry) and the pod sampler (pod.worker) query
    the SAME fields in the SAME order (they feed one GUI plot code path), and
  * the new MQTT topic constants exist and are named as the HA sample sensors
    expect.

Pure/offline: nvidia-smi is monkeypatched, no GPU needed.
"""

import types

import system_telemetry as st
import mqtt_publisher as mp


GPU_KEYS = ["gpu_used_mb", "gpu_total_mb", "gpu_temp_c", "gpu_util_pct",
            "gpu_power_w", "gpu_power_limit_w", "gpu_clock_mhz"]


# ── per-field parse ──────────────────────────────────────────────────────────

def test_parse_gpu_field_numbers_and_na():
    assert st._parse_gpu_field("83") == 83
    assert st._parse_gpu_field("245.7") == 245          # truncates to int
    assert st._parse_gpu_field(" 350.00 ") == 350       # whitespace tolerated
    for bad in ("[N/A]", "[Not Supported]", "", "  ", "N/A", "na", None, "abc"):
        assert st._parse_gpu_field(bad) is None


def _fake_run(stdout, returncode=0):
    def run(*_a, **_k):
        return types.SimpleNamespace(returncode=returncode, stdout=stdout)
    return run


def test_sample_gpu_full_row(monkeypatch):
    monkeypatch.setattr(st.subprocess, "run",
                        _fake_run("4096, 24564, 61, 83, 245.1, 350.00, 1830\n"))
    g = st.sample_gpu()
    assert g == {"gpu_used_mb": 4096, "gpu_total_mb": 24564, "gpu_temp_c": 61,
                 "gpu_util_pct": 83, "gpu_power_w": 245, "gpu_power_limit_w": 350,
                 "gpu_clock_mhz": 1830}


def test_sample_gpu_partial_na_is_per_field(monkeypatch):
    # A card with no power.limit / clocks reporting: those fields go None, the
    # rest survive.
    monkeypatch.setattr(st.subprocess, "run",
                        _fake_run("4096, 24564, 61, [N/A], 245.1, [N/A], \n"))
    g = st.sample_gpu()
    assert set(g) == set(GPU_KEYS)                       # every key still present
    assert g["gpu_used_mb"] == 4096 and g["gpu_temp_c"] == 61
    assert g["gpu_util_pct"] is None
    assert g["gpu_power_limit_w"] is None
    assert g["gpu_clock_mhz"] is None


def test_sample_gpu_failure_returns_none(monkeypatch):
    monkeypatch.setattr(st.subprocess, "run", _fake_run("", returncode=9))
    assert st.sample_gpu() is None

    def boom(*_a, **_k):
        raise OSError("nvidia-smi not found")
    monkeypatch.setattr(st.subprocess, "run", boom)
    assert st.sample_gpu() is None


# ── local vs pod parity ──────────────────────────────────────────────────────

def test_local_and_pod_query_identical_fields():
    """The GUI plots local and pod telemetry through one code path, so both
    samplers must emit the same keys in the same order."""
    import pod.worker as w                               # stdlib-only at import
    assert st._GPU_QUERY_FIELDS == w._GPU_FIELDS


def test_pod_sample_telemetry_shape(monkeypatch):
    import pod.worker as w
    monkeypatch.setattr(w, "_sample_cpu", lambda: 12)
    monkeypatch.setattr(w, "_sample_ram", lambda: (8000, 32000))
    monkeypatch.setattr(w, "subprocess", types.SimpleNamespace(
        check_output=lambda *a, **k: "4096, 24564, 61, 83, 245, 350, 1830\n"))
    s = w._sample_telemetry()
    for k in ("cpu", "ram_used_mb", "ram_total_mb", *GPU_KEYS):
        assert k in s
    assert s["gpu_util_pct"] == 83 and s["gpu_power_limit_w"] == 350


# ── MQTT topic constants ─────────────────────────────────────────────────────

def test_new_mqtt_topics_present_and_named():
    assert mp.SYS_GPU_UTIL_TOPIC == "image-toolbox/system/gpu_util"
    assert mp.SYS_GPU_POWER_TOPIC == "image-toolbox/system/gpu_power"
    assert mp.SYS_GPU_POWER_LIMIT_TOPIC == "image-toolbox/system/gpu_power_limit"
    assert mp.SYS_GPU_CLOCK_TOPIC == "image-toolbox/system/gpu_clock"
    assert mp.SYS_REMOTE_GPU_UTIL_TOPIC == "image-toolbox/system/remote/gpu_util"
    assert mp.SYS_REMOTE_GPU_POWER_TOPIC == "image-toolbox/system/remote/gpu_power"
    assert mp.SYS_REMOTE_GPU_POWER_LIMIT_TOPIC == "image-toolbox/system/remote/gpu_power_limit"
    assert mp.SYS_REMOTE_GPU_CLOCK_TOPIC == "image-toolbox/system/remote/gpu_clock"
