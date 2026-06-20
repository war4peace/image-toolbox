#!/usr/bin/env python3
"""
worker.py — the resident upscale worker on the pod (remote upscaling #1, Phase 3).

Loads the SAME UpscaleEngine the local app uses **once** (models from the network
volume), then serves **one image per HTTP request** over localhost. The local
side reaches it through an `ssh -L` tunnel, so the worker is never exposed
publicly. Each request touches a heartbeat file so pod/deadman.py can tell the
pod is still doing work.

Why HTTP + single-threaded: the request/response shape makes streaming one image
up and one result down trivial (raw bytes in the body), and a single-threaded
server serialises GPU work (one image at a time) for free.

    python worker.py --repo-dir /workspace/seedvr2 \
                     --model-dir /workspace/models/seedvr2 \
                     --settings /root/worker_settings.json \
                     --port 8200 --heartbeat /tmp/upscale_heartbeat

Endpoints:
    GET  /health                      -> {"status":"ok","device":"...","count":N}
    POST /upscale?resolution=R&ext=.jpg   body = source bytes
                                      -> upscaled bytes (X-Process-Time header, s)
"""
import os
import sys
import json
import time
import argparse
import tempfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

_ENGINE = None
_HEARTBEAT = None
_COUNT = 0


def _touch(path):
    if not path:
        return
    try:
        with open(path, "a"):
            os.utime(path, None)
    except OSError:
        pass


def _log(msg):
    print(f"[worker] {msg}", flush=True)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):          # quiet the default per-request logging
        pass

    def _send(self, code, body=b"", ctype="application/octet-stream", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):
        if urlparse(self.path).path == "/health":
            body = json.dumps({"status": "ok",
                               "device": getattr(_ENGINE, "device_name", "?"),
                               "count": _COUNT}).encode()
            self._send(200, body, "application/json")
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        global _COUNT
        parsed = urlparse(self.path)
        if parsed.path != "/upscale":
            self._send(404, b"not found", "text/plain")
            return
        q = parse_qs(parsed.query)
        try:
            resolution = int(q.get("resolution", ["1080"])[0])
        except ValueError:
            self._send(400, b"bad resolution", "text/plain")
            return
        ext = q.get("ext", [".jpg"])[0]
        if not ext.startswith("."):
            ext = "." + ext

        length = int(self.headers.get("Content-Length", 0))
        if length <= 0:
            self._send(400, b"empty body", "text/plain")
            return
        data = self.rfile.read(length)

        _touch(_HEARTBEAT)                       # work arriving = still alive
        tmpdir = tempfile.mkdtemp(prefix="wrk_")
        src = os.path.join(tmpdir, "in" + ext)
        dst = os.path.join(tmpdir, "out" + ext)
        try:
            with open(src, "wb") as f:
                f.write(data)
            t0 = time.time()
            _ENGINE.upscale(src, dst, resolution)
            dt = time.time() - t0
            with open(dst, "rb") as f:
                out = f.read()
            _COUNT += 1
            _touch(_HEARTBEAT)
            _log(f"#{_COUNT} upscaled {len(data)}B -> {len(out)}B in {dt:.1f}s "
                 f"(res={resolution})")
            self._send(200, out, _ctype_for(ext), {"X-Process-Time": f"{dt:.3f}"})
        except Exception as exc:                 # noqa: BLE001 — report, keep serving
            _log(f"ERROR upscaling: {exc}")
            self._send(500, str(exc).encode(), "text/plain")
        finally:
            for p in (src, dst):
                try:
                    os.remove(p)
                except OSError:
                    pass
            try:
                os.rmdir(tmpdir)
            except OSError:
                pass


def _ctype_for(ext):
    return {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".png": "image/png", ".webp": "image/webp"}.get(ext.lower(),
                                                             "application/octet-stream")


def main(argv=None):
    global _ENGINE, _HEARTBEAT
    p = argparse.ArgumentParser(description="Resident upscale worker for a RunPod pod.")
    p.add_argument("--repo-dir", required=True)
    p.add_argument("--model-dir", required=True)
    p.add_argument("--settings", required=True)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8200)
    p.add_argument("--heartbeat", default="/tmp/upscale_heartbeat")
    args = p.parse_args(argv)

    _HEARTBEAT = args.heartbeat
    sys.path.insert(0, args.repo_dir)
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from upscale_engine import UpscaleEngine

    with open(args.settings, encoding="utf-8") as f:
        settings = json.load(f)

    _log(f"loading engine (repo={args.repo_dir} models={args.model_dir}) …")
    t0 = time.time()
    _ENGINE = UpscaleEngine(args.repo_dir, args.model_dir, settings)
    _log(f"engine ready in {time.time() - t0:.1f}s on {_ENGINE.device_name}")
    _touch(_HEARTBEAT)                            # ready = first heartbeat

    httpd = HTTPServer((args.host, args.port), Handler)
    _log(f"serving on {args.host}:{args.port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        _ENGINE.close()


if __name__ == "__main__":
    main()
