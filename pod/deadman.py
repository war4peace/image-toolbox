#!/usr/bin/env python3
"""
deadman.py — the dead-man's switch for a remote upscaling pod.

Runs ON the RunPod pod (Linux), NOT on the user's Windows machine. It is the
*guaranteed* half of the never-leave-a-billed-pod-running promise: the local app
issuing a stop is the fast path, but if the connection drops, the controller
crashes, or the laptop lid closes, this daemon still shuts the pod down on its
own deadline.

How it decides to stop:
  * max runtime  — a hard ceiling from the daemon's own start (≈ pod boot).
  * idle timeout — no work seen for this long. The worker (Phase 3) touches a
    heartbeat file on every image; we read that file's mtime as "last activity".
    Before the worker's first heartbeat we fall back to our start time, so a pod
    that never receives work still dies. (idle_timeout MUST exceed the model
    cold-load time, or the pod would kill itself mid-startup; the worker should
    also touch the heartbeat once it is ready.)

How it stops the pod (no user API key on the box):
  * `runpodctl` is pre-installed and pre-authed on RunPod images, and the pod's
    own id is in $RUNPOD_POD_ID — so the pod can stop *itself* with
    `runpodctl stop pod <id>` (or `runpodctl remove pod <id>` to terminate and
    free all billing). The user's REST API key never has to live on the pod.
  * Known gotcha: runpodctl from inside a pod can hit
    "x509: certificate signed by unknown authority" (runpodctl#149). We retry,
    log loudly, and — as a last resort only if RUNPOD_API_KEY is present in the
    environment — fall back to the REST API so the bill still stops.

Pure stdlib; the stop *decision* (`evaluate`) is side-effect-free so it can be
unit-tested off a pod (see `--selftest`).
"""

import os
import sys
import time
import argparse
import subprocess
import urllib.request

DEFAULT_HEARTBEAT = "/tmp/upscale_heartbeat"
REST_BASE_URL     = "https://rest.runpod.io/v1"


def evaluate(now, started_at, last_activity, max_runtime_s, idle_s):
    """Decide whether the pod should stop. Pure — no side effects.

    Returns (should_stop, reason). A limit of 0 (or less) disables that check.
    `last_activity` is the most recent of the heartbeat mtime or `started_at`.
    """
    if max_runtime_s and max_runtime_s > 0:
        ran = now - started_at
        if ran >= max_runtime_s:
            return True, (f"max runtime reached ({ran/60:.1f} min "
                          f">= {max_runtime_s/60:.0f} min limit)")
    if idle_s and idle_s > 0:
        idle = now - last_activity
        if idle >= idle_s:
            return True, (f"idle timeout reached ({idle/60:.1f} min "
                          f">= {idle_s/60:.0f} min limit)")
    return False, None


def _last_activity(heartbeat_path, started_at):
    """Most recent activity: the heartbeat file's mtime, or our start time if the
    worker hasn't written one yet (or it is unreadable)."""
    try:
        return max(os.path.getmtime(heartbeat_path), started_at)
    except OSError:
        return started_at


def _log(msg):
    print(f"[deadman] {msg}", flush=True)


def stop_pod(pod_id, terminate=False, retries=3):
    """Stop (or terminate) this pod. Tries runpodctl first, then a REST fallback
    if an API key happens to be in the environment. Returns True on success."""
    if not pod_id:
        _log("ERROR: no pod id (RUNPOD_POD_ID unset) — cannot self-stop.")
        return False

    verb = "remove" if terminate else "stop"
    cmd = ["runpodctl", verb, "pod", pod_id]
    for attempt in range(1, retries + 1):
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            if res.returncode == 0:
                _log(f"runpodctl {verb} pod succeeded.")
                return True
            _log(f"runpodctl attempt {attempt}/{retries} failed "
                 f"(rc={res.returncode}): {res.stderr.strip() or res.stdout.strip()}")
        except FileNotFoundError:
            _log("runpodctl not found on PATH.")
            break
        except Exception as exc:                          # noqa: BLE001 (fail-safe)
            _log(f"runpodctl attempt {attempt}/{retries} errored: {exc}")
        time.sleep(2 * attempt)

    # Last resort: REST, only if a key is already present on the pod.
    if _rest_stop(pod_id, terminate):
        return True
    _log("ERROR: could not stop the pod by any method — manual stop required.")
    return False


def _rest_stop(pod_id, terminate):
    """Fallback stop via the REST API, using RUNPOD_API_KEY from the environment
    if present. Returns True on success, False otherwise (incl. no key)."""
    api_key = os.environ.get("RUNPOD_API_KEY", "").strip()
    if not api_key:
        return False
    method = "DELETE" if terminate else "POST"
    path = f"/pods/{pod_id}" if terminate else f"/pods/{pod_id}/stop"
    req = urllib.request.Request(
        REST_BASE_URL + path, method=method,
        headers={"Authorization": f"Bearer {api_key}",
                 "User-Agent": "ImageToolbox-Deadman"})
    try:
        with urllib.request.urlopen(req, timeout=60):
            _log("REST fallback stop succeeded.")
            return True
    except Exception as exc:                              # noqa: BLE001
        _log(f"REST fallback stop failed: {exc}")
        return False


def run(max_runtime_min, idle_timeout_min, heartbeat_path, pod_id,
        terminate=False, poll_sec=30, dry_run=False):
    """Poll until a deadline trips, then stop the pod. Returns the stop reason."""
    started_at    = time.time()
    max_runtime_s = max_runtime_min * 60
    idle_s        = idle_timeout_min * 60
    _log(f"started - pod={pod_id or '?'} max_runtime={max_runtime_min}min "
         f"idle_timeout={idle_timeout_min}min action={'terminate' if terminate else 'stop'} "
         f"poll={poll_sec}s heartbeat={heartbeat_path}"
         + ("  [DRY RUN]" if dry_run else ""))

    while True:
        now = time.time()
        should, reason = evaluate(
            now, started_at, _last_activity(heartbeat_path, started_at),
            max_runtime_s, idle_s)
        if should:
            _log(f"TRIPPED: {reason}")
            if dry_run:
                _log("dry run — would stop the pod now.")
            else:
                stop_pod(pod_id, terminate=terminate)
            return reason
        time.sleep(poll_sec)


def _selftest():
    """Off-pod sanity check of the pure decision logic."""
    cases = [
        # now, start, last_act, max_s, idle_s, expect_stop
        (1000, 0,    1000, 0,    0,    False),   # both disabled
        (1000, 0,    1000, 600,  0,    True),    # max runtime hit
        (500,  0,    500,  600,  0,    False),   # under max runtime
        (1000, 0,    100,  0,    300,  True),    # idle hit (900s idle)
        (1000, 0,    900,  0,    300,  False),   # idle 100s, under limit
        (1000, 0,    900,  600,  300,  True),    # max runtime hit first
    ]
    ok = True
    for now, start, last, mx, idle, expect in cases:
        got, reason = evaluate(now, start, last, mx, idle)
        flag = "ok " if got == expect else "FAIL"
        if got != expect:
            ok = False
        print(f"  [{flag}] now={now} start={start} last={last} max={mx} idle={idle} "
              f"-> stop={got} ({reason})")
    print("selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv=None):
    p = argparse.ArgumentParser(description="Dead-man's switch for a RunPod upscaling pod.")
    p.add_argument("--max-runtime-min", type=float,
                   default=float(os.environ.get("DEADMAN_MAX_RUNTIME_MIN", 720)))
    p.add_argument("--idle-timeout-min", type=float,
                   default=float(os.environ.get("DEADMAN_IDLE_TIMEOUT_MIN", 15)))
    p.add_argument("--heartbeat", default=os.environ.get("DEADMAN_HEARTBEAT", DEFAULT_HEARTBEAT))
    p.add_argument("--pod-id", default=os.environ.get("RUNPOD_POD_ID", ""))
    p.add_argument("--terminate", action="store_true",
                   help="terminate (remove) the pod instead of stopping it (frees all billing)")
    p.add_argument("--poll-sec", type=float, default=30.0)
    p.add_argument("--dry-run", action="store_true",
                   help="log the decision but do not actually stop the pod")
    p.add_argument("--selftest", action="store_true", help="run the decision-logic self-test and exit")
    args = p.parse_args(argv)

    if args.selftest:
        return _selftest()

    run(args.max_runtime_min, args.idle_timeout_min, args.heartbeat, args.pod_id,
        terminate=args.terminate, poll_sec=args.poll_sec, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
