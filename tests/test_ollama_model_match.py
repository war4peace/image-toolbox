"""
Item 11: the Ollama pre-flight ('is the configured model pulled/loaded?') matched
with a loose substring, `base in m` where base = OLLAMA_MODEL.split(':')[0]. So a
configured 'llava' matched 'llava-phi3' and 'qwen2.5vl' would match any future
'qwen2.5vl-max' — the check passed while /api/generate then failed with 'model not
found', burning the outage path instead of the clean startup error.

`_ollama_model_available` now matches on the name up to the ':tag' boundary. These
tests pin the false-positives it must reject and the real matches it must keep
(including tag-agnostic base matching, which is deliberately preserved). Pure /
stdlib, no network (the helper takes the already-fetched name list).
"""

import pytest

import tag_and_rename as tr


def _avail(monkeypatch, configured, names):
    monkeypatch.setattr(tr, "OLLAMA_MODEL", configured)
    return tr._ollama_model_available(names)


# ── the reported false positives, now rejected ──────────────────────────────

def test_llava_does_not_match_llava_phi3(monkeypatch):
    assert _avail(monkeypatch, "llava", ["llava-phi3:latest"]) is False


def test_qwen_does_not_match_qwen_max_variant(monkeypatch):
    assert _avail(monkeypatch, "qwen2.5vl", ["qwen2.5vl-max:latest"]) is False


def test_no_partial_prefix_match(monkeypatch):
    # 'llava' must not match a model that merely CONTAINS it either.
    assert _avail(monkeypatch, "llava", ["my-llava:latest", "llavax:latest"]) is False


# ── the real matches, still accepted ─────────────────────────────────────────

def test_matches_exact_tagged_name(monkeypatch):
    assert _avail(monkeypatch, "llava", ["llava:latest"]) is True


def test_matches_untagged_name(monkeypatch):
    assert _avail(monkeypatch, "llava", ["llava"]) is True


def test_tag_agnostic_base_match_is_preserved(monkeypatch):
    # A configured tag ('llava:13b') still matches any pulled 'llava:*' — the fix
    # only tightens the NAME boundary, not the tag, so a differently-tagged pull
    # doesn't spuriously fail the pre-flight.
    assert _avail(monkeypatch, "llava:13b", ["llava:7b"]) is True


def test_matches_among_several(monkeypatch):
    assert _avail(monkeypatch, "qwen2.5vl",
                  ["llava:latest", "qwen2.5vl:7b", "bakllava:latest"]) is True


def test_empty_list_is_not_available(monkeypatch):
    assert _avail(monkeypatch, "llava", []) is False
