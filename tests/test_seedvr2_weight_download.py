"""
Robust SeedVR2 weight pre-download (upscale_engine.ensure_seedvr2_weights + _download_verified):
the fix for the local-video "soft-hang" where a MISSING SeedVR2 DiT/VAE was fetched by seedvr2's
own downloader -- silent (stdout captured to a file sink) and with no firing read timeout (a
0-byte .download parked on a stalled HuggingFace socket for 10+ minutes). We now pre-fetch the
file ourselves: visible, resumable, timing-out, hash-verified, into the same model_dir seedvr2
falls back to. All offline (a fake urlopen); the live HF round-trip is validated separately.
"""
import hashlib
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import upscale_engine as ue        # torch-free at import (torch loads only inside the engine)


# ── a fake HTTP response so the downloader is exercised without network ───────

class _FakeResp:
    """Minimal stand-in for http.client.HTTPResponse: streams `data` in small chunks."""
    def __init__(self, data, status=200):
        self._chunks = [data[i:i + 7] for i in range(0, len(data), 7)]
        self.status = status
        self.headers = {"Content-Length": str(len(data))}

    def read(self, _n=-1):
        return self._chunks.pop(0) if self._chunks else b""

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


def _serve(monkeypatch, payload, status=200):
    """Point the downloader's urlopen at a fake response serving `payload`."""
    def _fake_urlopen(req, context=None, timeout=None):   # noqa: ARG001
        return _FakeResp(payload, status=status)
    monkeypatch.setattr(ue.urllib.request, "urlopen", _fake_urlopen)


# ── _download_verified ───────────────────────────────────────────────────────

def test_download_verified_writes_and_verifies(tmp_path, monkeypatch):
    payload = b"seedvr2-weight-bytes" * 100
    sha = hashlib.sha256(payload).hexdigest()
    _serve(monkeypatch, payload)
    dest = str(tmp_path / "model.safetensors")
    ue._download_verified("https://example/model", dest, sha)
    assert os.path.exists(dest)
    assert open(dest, "rb").read() == payload
    assert not os.path.exists(dest + ".part")          # temp cleaned up on success


def test_download_verified_sha_mismatch_raises_and_cleans(tmp_path, monkeypatch):
    payload = b"corrupt-bytes"
    _serve(monkeypatch, payload)
    dest = str(tmp_path / "model.safetensors")
    with pytest.raises(RuntimeError, match="could not download"):
        # Expected sha is for DIFFERENT bytes, so every attempt mismatches.
        ue._download_verified("https://example/model", dest,
                              hashlib.sha256(b"other").hexdigest(), retries=2)
    assert not os.path.exists(dest)                    # never leaves an unverified file
    assert not os.path.exists(dest + ".part")          # nor a partial


# ── ensure_seedvr2_weights ───────────────────────────────────────────────────

def test_ensure_skips_present_and_unknown(tmp_path, monkeypatch):
    calls = []

    def _fake_dl(url, dest, sha, **_k):
        calls.append(url)
        with open(dest, "wb") as f:                     # write it so a later call sees it present
            f.write(b"downloaded")
    monkeypatch.setattr(ue, "_download_verified", _fake_dl)
    monkeypatch.setattr(ue, "_seed_validation_cache", lambda *a, **k: None)

    # The DiT is a KNOWN filename but already on disk -> skipped; the VAE (known, absent) is the
    # only thing fetched.
    dit = "seedvr2_ema_3b-Q8_0.gguf"
    (tmp_path / dit).write_bytes(b"already here")
    n = ue.ensure_seedvr2_weights(str(tmp_path), dit, log=None)
    assert n == 1                                      # only the missing VAE
    assert len(calls) == 1 and "ema_vae_fp16" in calls[0]

    # Now both are present (DiT on disk, VAE just fetched): an UNKNOWN DiT touches nothing.
    calls.clear()
    n = ue.ensure_seedvr2_weights(str(tmp_path), "not-a-real-model.bin", log=None)
    assert n == 0 and calls == []


def test_ensure_downloads_missing_and_seeds_cache(tmp_path, monkeypatch):
    dit = "seedvr2_ema_3b-Q8_0.gguf"
    vae = "ema_vae_fp16.safetensors"

    def _fake_dl(url, dest, sha, **_k):
        with open(dest, "wb") as f:
            f.write(b"x" * 2048)
    monkeypatch.setattr(ue, "_download_verified", _fake_dl)
    n = ue.ensure_seedvr2_weights(str(tmp_path), dit, log=None)
    assert n == 2                                      # DiT + VAE both missing -> both fetched
    assert (tmp_path / dit).exists() and (tmp_path / vae).exists()
    # Cache seeded so the engine won't re-hash the multi-GB file on first load.
    cache = json.loads((tmp_path / ".validation_cache.json").read_text())
    for name in (dit, vae):
        assert cache[name]["size"] == 2048
        assert cache[name]["hash"] == ue._SEEDVR2_WEIGHTS[name][1]
        assert "mtime" in cache[name]


def test_ensure_is_noop_without_model_dir_or_model():
    assert ue.ensure_seedvr2_weights("", "seedvr2_ema_3b-Q8_0.gguf") == 0
    assert ue.ensure_seedvr2_weights("/some/dir", "") == 0


def test_seed_validation_cache_format(tmp_path):
    f = tmp_path / "w.safetensors"
    f.write_bytes(b"abc")
    ue._seed_validation_cache(str(tmp_path), "w.safetensors", "deadbeef")
    cache = json.loads((tmp_path / ".validation_cache.json").read_text())
    assert cache["w.safetensors"] == {
        "size": 3, "mtime": pytest.approx(os.path.getmtime(f)), "hash": "deadbeef"}


# ── drift guard: our torch-free mirror must match seedvr2's real registry ─────

def test_registry_mirror_matches_seedvr2():
    """The hardcoded _SEEDVR2_WEIGHTS mirrors seedvr2's MODEL_REGISTRY (repo + sha256). If seedvr2
    is re-vendored with new/changed weights this catches the drift. Skipped where the (torch-heavy)
    seedvr2 package can't be imported. Restores sys.path + sys.modules so importing the real `src.*`
    package here can't leak into other tests (a global `src` on the path breaks tests that rely on
    it being absent, e.g. the compat fail-safe test)."""
    repo_dir = os.path.join(ROOT, "seedvr2")
    added = repo_dir not in sys.path
    if added:
        sys.path.insert(0, repo_dir)
    before = set(sys.modules)
    try:
        reg = pytest.importorskip("src.utils.model_registry")
        default_repo = reg.ModelInfo().repo
        for name, info in reg.MODEL_REGISTRY.items():
            assert name in ue._SEEDVR2_WEIGHTS, f"{name} missing from the mirror"
            mrepo, msha = ue._SEEDVR2_WEIGHTS[name]
            assert mrepo == (info.repo or default_repo), f"{name} repo drift"
            assert msha == info.sha256, f"{name} sha drift"
        assert ue._SEEDVR2_DEFAULT_VAE == reg.DEFAULT_VAE
    finally:
        for name in set(sys.modules) - before:
            if name == "src" or name.startswith("src."):
                del sys.modules[name]
        if added and repo_dir in sys.path:
            sys.path.remove(repo_dir)
