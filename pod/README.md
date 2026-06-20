# `pod/` — code that runs ON the RunPod pod

These modules run on the **rented pod's Linux**, not on the user's Windows
machine (which is what `scripts/` is). They are uploaded to the pod during
provisioning (roadmap #1 — see `docs/runpod-notes.md`). Pure Python 3 stdlib so
they run on a bare pod with no extra install.

| File | Role |
|------|------|
| `deadman.py` | The **dead-man's switch**: a daemon that self-stops the pod on a max-runtime or idle-timeout deadline, even if the controlling app drops off. Stops via the pre-installed, pre-authed `runpodctl stop pod $RUNPOD_POD_ID` (or `remove pod` to terminate) — so the user's API key never lives on the pod. Pure `evaluate()` decision logic; run `python deadman.py --selftest` to verify it off a pod. |

Planned (later phases):

- `worker.py` — the resident upscale worker: loads SeedVR2 **once** (from the
  models network volume) and serves **one image at a time** over an `ssh -L`
  tunnel, touching the heartbeat file `deadman.py` watches. The local queue,
  resume-cache and performance watchdog stay on the Windows side.
