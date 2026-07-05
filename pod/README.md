# `pod/` — code that runs ON the RunPod pod

These modules run on the **rented pod's Linux**, not on the user's Windows
machine (which is what `scripts/` is). They are uploaded to the pod during
provisioning (roadmap #1, remote upscaling: see `docs/runpod-notes.md`; and
roadmap #2, the Video Upscaler: see `docs/video-upscaler.md`). Pure Python 3
stdlib (plus, for the workers, the SeedVR2 engine already on the network volume)
so they run on a bare pod with no extra install.

| File | Role |
|------|------|
| `worker.py` | The **resident worker**: loads the SAME `UpscaleEngine` the local app uses **once** (models from the network volume) and serves work over localhost, reached from the Windows side through an `ssh -L` tunnel (never exposed publicly). Endpoints `/upscale`, `/orient`, `/telemetry`, `/health`; each request touches the heartbeat file `deadman.py` watches. `--mode full` loads SeedVR2 (image upscaling); `--mode tag` skips SeedVR2 and serves `/orient` only (remote Tag & Rename, freeing VRAM for Ollama, which `remote_run` also starts + tunnels); `--mode video` streams a segment at a time for the Video Upscaler. Seeds the seedvr2 validation cache from the DiT+VAE size/mtime so a cold start never re-hashes 16 GB. |
| `deadman.py` | The **dead-man's switch**: a daemon that self-stops the pod on a max-runtime or idle-timeout deadline, even if the controlling app drops off. Stops via the pre-installed, pre-authed `runpodctl stop pod $RUNPOD_POD_ID` (or `remove pod` to terminate), so the user's API key never lives on the pod. Pure `evaluate()` decision logic; run `python deadman.py --selftest` to verify it off a pod. |
| `provision.sh` | **One-time volume setup** (idempotent-ish): populates the model network volume at `/workspace` (the SeedVR2 engine + weights (~16 GB), light python deps, and the full Ollama runtime, both the `ollama` binary **and** its `lib/ollama/` runners), so disposable pods just mount it and start fast. |
| `upscale_one.py` | A **minimal single-image** on-pod upscale (the original seed of `worker.py`), reusing the same `UpscaleEngine`. Used to validate the remote stack end to end. |
| `bench_video.py` | Video Upscaler **Phase-1 benchmark**: per-frame throughput for the temporal-batch path at each target, plus the max-viable `batch_size` per (target × card) and its VRAM curve. Answers the GPU-only questions in `docs/video-upscaler.md`. |
| `ram_probe.py` | Video Upscaler **RAM validation**: confirms the streaming path (`chunk_size > 0`) bounds process RAM versus SeedVR2's load-all path (`chunk_size = 0`, which holds every output frame uncompressed). |
