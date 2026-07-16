"""
tests/test_upscale_engine_compat.py
-----------------------------------
The runtime patches upscale_engine applies to the DOWNLOADED seedvr2 engine.

seedvr2/ is .gitignore'd and fetched by bootstrap.ps1, so a bug in it cannot be fixed by
editing it: the edit works on one machine, ships to nobody, and is erased by the next
bootstrap. Anything we must correct there is corrected at import time from our side, and
pinned here.

Torch-free by design (the whole suite is): the seedvr2 module these patches reach into is
faked, so the real function under test runs without a GPU, a 2 GB import, or the engine
being downloaded at all.
"""

import sys
import types
from collections.abc import Sized

import pytest

import upscale_engine as ue


@pytest.fixture
def fake_seedvr2(monkeypatch):
    """Inject a fake `src.optimization.compatibility` exposing CompatibleDiT, and reset the
    one-shot guard so each test exercises the real patch.

    The class is minted FRESH per test: the patch mutates it (and registers it with an ABC),
    and a module-level class would carry those mutations into the next test. It did, and the
    resulting cross-test failure is why this is built here.
    """
    compat = types.ModuleType("src.optimization.compatibility")
    compat.CompatibleDiT = type("_FakeCompatibleDiT", (), {})
    monkeypatch.setitem(sys.modules, "src", types.ModuleType("src"))
    monkeypatch.setitem(sys.modules, "src.optimization", types.ModuleType("src.optimization"))
    monkeypatch.setitem(sys.modules, "src.optimization.compatibility", compat)
    monkeypatch.setattr(ue, "_TRUTHINESS_FIXED", False)
    return compat


def test_patch_makes_a_dit_satisfy_len_and_sized(fake_seedvr2):
    """THE bug: torch.compile wraps the DiT in an OptimizedModule, whose __len__ proxies to
    the wrapped module and RAISES when it is not Sized. seedvr2's cache-reuse path asks
    `if cached_model:`, Python resolves that through __len__, and a compiled local video run
    died on its SECOND chunk with "CompatibleDiT does not support len()".
    """
    Dit = fake_seedvr2.CompatibleDiT
    assert getattr(Dit, "__len__", None) is None, "starts without __len__"

    ue._fix_compiled_dit_truthiness()

    dit = Dit()
    assert len(dit) == 1
    assert bool(dit) is True, "a DiT must stay truthy, as a bare nn.Module always was"
    # What OptimizedModule.__len__ actually gates on before proxying the call.
    assert isinstance(dit, Sized)


def test_patch_survives_a_poisoned_abc_negative_cache(fake_seedvr2):
    """Sized answers isinstance via __subclasshook__, and ABCMeta CACHES the answer per class.
    An isinstance check that runs BEFORE the patch caches a NEGATIVE that is never rechecked,
    so attaching __len__ alone leaves OptimizedModule.__len__ still raising. Reproduced for
    real against torch: the fix only held once Sized.register() bumped abc's invalidation
    counter. Without that call this test fails, which is the point of it.
    """
    dit = fake_seedvr2.CompatibleDiT()
    assert not isinstance(dit, Sized), "poisons the negative cache, as the real run did"

    ue._fix_compiled_dit_truthiness()

    assert isinstance(dit, Sized), "the cached negative must not outlive the patch"
    assert len(dit) == 1


def test_patch_never_overrides_an_upstream_len(fake_seedvr2):
    """If upstream fixes this by giving CompatibleDiT a real __len__, theirs wins. We are
    repairing a missing dunder, not asserting what it should return."""
    fake_seedvr2.CompatibleDiT.__len__ = lambda _self: 42

    ue._fix_compiled_dit_truthiness()

    assert len(fake_seedvr2.CompatibleDiT()) == 42


def test_patch_is_idempotent_and_runs_once(fake_seedvr2):
    """Called from UpscaleEngine.__init__, so it runs once per engine, many times per process."""
    ue._fix_compiled_dit_truthiness()
    ue._fix_compiled_dit_truthiness()
    ue._fix_compiled_dit_truthiness()

    assert len(fake_seedvr2.CompatibleDiT()) == 1
    assert ue._TRUTHINESS_FIXED is True


def test_patch_is_fail_safe_when_upstream_moves(monkeypatch):
    """seedvr2 is third-party and downloaded: a rename must degrade to an uncompiled-shaped
    failure we can read in debug.log, never take the engine down at import."""
    monkeypatch.setattr(ue, "_TRUTHINESS_FIXED", False)
    for name in ("src", "src.optimization", "src.optimization.compatibility"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    logged = []
    monkeypatch.setattr(ue, "debug_log", lambda msg, **kw: logged.append(msg))

    ue._fix_compiled_dit_truthiness()          # must not raise

    assert logged and "truthiness" in logged[0]
