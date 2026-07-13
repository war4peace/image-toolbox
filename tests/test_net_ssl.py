"""
Tests for net_ssl.ssl_context (0.5.0).

The bug: a fresh Windows VM (Remote-only install) has a nearly-empty OS root
store, so urllib's default SSL context can't verify RunPod's cert and every HTTPS
call fails with "unable to get local issuer certificate". The fix hands urllib
certifi's bundle explicitly. These tests pin the two invariants that matter:

  1. the context trusts a real CA bundle (not an empty store), and
  2. it falls back to the stdlib default context if certifi is absent (an older
     venv) rather than raising, so nothing regresses.

Pure stdlib; no network.
"""

import ssl
import sys
import importlib

sys.path.insert(0, "scripts")
import net_ssl


def _fresh():
    """Reload net_ssl so the per-process cached context is rebuilt."""
    return importlib.reload(net_ssl)


def test_context_trusts_a_real_ca_bundle():
    m = _fresh()
    ctx = m.ssl_context()
    assert isinstance(ctx, ssl.SSLContext)
    # certifi ships >100 roots; an empty store would fail exactly the way the VM did.
    assert ctx.cert_store_stats()["x509_ca"] > 50


def test_context_is_cached_per_process():
    m = _fresh()
    assert m.ssl_context() is m.ssl_context()


def test_falls_back_to_default_when_certifi_absent(monkeypatch):
    """If `import certifi` fails, we must still return a usable default context,
    never raise, so a machine with a working OS store keeps functioning."""
    m = _fresh()
    real_import = __import__

    def no_certifi(name, *args, **kwargs):
        if name == "certifi":
            raise ImportError("simulated missing certifi")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", no_certifi)
    ctx = m.ssl_context()
    assert isinstance(ctx, ssl.SSLContext)
    # Default context still verifies + checks hostname (we didn't weaken security).
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True
