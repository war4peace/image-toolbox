"""
Pausing a run frees EVERY model the app holds, with no size-based exceptions:
the vision model (Ollama), the upscale engine (SeedVR2) and the small
auto-straighten CNN alike. An exception would be one more rule to remember, and
the next model added would have to rediscover it.

These tests pin orientation.unload()'s contract without needing torch or a GPU
(a fake stands in for the loaded model), so they run anywhere, including a
Remote-only install where torch is not present at all.
"""

import sys

import orientation


def test_unload_with_nothing_loaded_is_a_noop(monkeypatch):
    monkeypatch.setattr(orientation, "_MODEL", None)
    assert orientation.unload() is False


def test_unload_never_imports_torch_by_itself(monkeypatch):
    """A Remote-only install has no torch at all. unload() must go through
    sys.modules rather than importing it, so it can never drag a 2 GB dependency
    in (or ImportError) just to release something. Exercised with a model
    'loaded', so the release path really runs."""
    import builtins
    real_import = builtins.__import__

    def guard(name, *a, **kw):
        assert name != "torch", "unload() must not import torch"
        return real_import(name, *a, **kw)

    monkeypatch.delitem(sys.modules, "torch", raising=False)   # never imported
    monkeypatch.setattr(builtins, "__import__", guard)
    monkeypatch.setattr(orientation, "_MODEL", (object(), "cuda"))

    assert orientation.unload() is True
    assert orientation._MODEL is None


def test_unload_releases_a_loaded_model_and_is_idempotent(monkeypatch):
    monkeypatch.setattr(orientation, "_MODEL", (object(), "cuda"))
    assert orientation.unload() is True          # released
    assert orientation._MODEL is None            # and the handle is dropped
    assert orientation.unload() is False         # second call: nothing to do


def test_unload_survives_a_broken_torch(monkeypatch):
    """Fail-safe: a problem freeing memory must not raise into the run. Worst
    case the memory stays held, which is the old behaviour."""
    class Boom:
        class cuda:
            @staticmethod
            def is_available():
                raise RuntimeError("driver gone")

    monkeypatch.setitem(sys.modules, "torch", Boom)
    monkeypatch.setattr(orientation, "_MODEL", (object(), "cuda"))
    assert orientation.unload() is True
    assert orientation._MODEL is None
