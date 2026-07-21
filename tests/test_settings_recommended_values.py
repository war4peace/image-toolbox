"""
Settings tooltips quote a "Recommended:" value for the numeric knobs. That number
must be the value the code actually falls back to when the key is absent from
config.json, or the hint teaches the user something false. Config can hold any
value, so the tooltip is checked against the CODE default, not the live config.

Pinned here because the two live far apart: the hint sits in gui/tab_settings.py
while the default sits in batch_upscale.py / upscale_engine.py, so changing one
silently and leaving the other is the easy mistake.
"""

import re

import pytest

import batch_upscale
import tag_and_rename
import upscale_engine   # noqa: F401  (imported for the tile-size default's home)
from gui import tab_settings


def _code_default(module_src, key, cast):
    """The literal default in a `.get("<key>", <default>)` call."""
    m = re.search(rf'get\(\s*"{key}"\s*,\s*([0-9.]+)\s*\)', module_src)
    assert m, f"no coded default found for {key}"
    return cast(m.group(1))


@pytest.fixture(scope="module")
def sources():
    import inspect
    return {
        "upscale": inspect.getsource(batch_upscale),
        "tag": inspect.getsource(tag_and_rename),
        "engine": inspect.getsource(upscale_engine),
    }


@pytest.mark.parametrize("key, expected", [
    ("outage_threshold", "3"),
    ("encode_tile_size", "1024"),
    ("decode_tile_size", "1024"),
])
def test_seedvr_tips_quote_the_coded_default(key, expected, sources):
    tip = tab_settings._SEEDVR_TIPS[key]
    assert f"Recommended: {expected}" in tip, tip
    src = sources["engine"] if "tile_size" in key else sources["upscale"]
    assert str(_code_default(src, key, float)).rstrip("0").rstrip(".") == expected


@pytest.fixture(scope="module")
def settings_src():
    import inspect
    return inspect.getsource(tab_settings)


def test_upscale_cutoff_default_matches(sources, settings_src):
    assert _code_default(sources["upscale"], "upscale_cutoff_pct", int) == 66
    assert "Recommended: 66" in settings_src


def test_straighten_confidence_default_matches(sources, settings_src):
    """Both auto-straighten thresholds (Upscaling and Tag & Rename) default to 0.9
    and both tooltips must say so. Counted, not just searched: with two identical
    hint strings, an `in` check would still pass if one of them lost its label."""
    assert _code_default(sources["upscale"], "straighten_min_confidence", float) == 0.9
    assert _code_default(sources["tag"], "straighten_min_confidence", float) == 0.9
    assert settings_src.count("Recommended: 0.90") == 2


def test_watchdog_defaults_match(sources, settings_src):
    assert _code_default(sources["upscale"], "watchdog_factor", float) == 3.0
    assert _code_default(sources["upscale"], "watchdog_consecutive", int) == 2
    assert "Recommended: 3.0" in settings_src
    assert "Recommended: 2" in settings_src
