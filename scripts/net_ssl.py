"""
net_ssl.py
----------
Shared HTTPS trust context (0.5.0).

A fresh Windows machine (very common for a **Remote-only** install, e.g. a clean
VM) has a nearly-empty OS root-certificate store, and Windows' automatic root
update may not have run yet. PowerShell / .NET (SChannel) papers over this by
auto-fetching missing roots on demand, so `bootstrap.ps1`'s downloads succeed,
but Python's `ssl` (OpenSSL) does NOT auto-fetch. The result: every HTTPS call
urllib makes fails with "certificate verify failed: unable to get local issuer
certificate" even though the same box can download files fine. The first casualty
is the RunPod API-key test on the Remote tab.

The fix is to hand urllib an explicit, self-contained CA bundle from `certifi`
(installed in every install mode by `bootstrap.ps1`) instead of relying on the OS
store. If `certifi` is somehow absent (an older venv), we fall back to the default
context so nothing regresses on a machine whose OS store already works.

Stdlib + optional `certifi`; fail-safe; the context is built once and cached.
"""

import ssl

_CTX = None


def ssl_context():
    """Return a shared SSL context that trusts a real CA bundle.

    Prefers `certifi`'s bundle (independent of the OS store); falls back to the
    stdlib default context if certifi can't be imported. Safe to pass to
    `urllib.request.urlopen(..., context=...)`; it is ignored for plain-HTTP URLs
    (e.g. the ssh-tunnelled localhost calls), so callers can pass it
    unconditionally.
    """
    global _CTX
    if _CTX is not None:
        return _CTX
    try:
        import certifi
        _CTX = ssl.create_default_context(cafile=certifi.where())
    except Exception:                                    # noqa: BLE001 (fail-safe)
        _CTX = ssl.create_default_context()
    return _CTX
