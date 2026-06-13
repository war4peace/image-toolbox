# RunPod remote-pod notes (for a future feature)

Reference notes distilled from the old `remote-image-upscale.ps1` /
`remote-tag-and-rename.ps1` scripts (removed — see git history before commit
`baf6f8b` for the full originals). Those scripts targeted the **pre-0.1.0
ComfyUI architecture** and do not run against the current app; this file keeps
the parts that are still worth reusing when remote-pod support is rebuilt.

## Architecture: what changed, and what still maps

The old model: run the tool **locally**, SSH-tunnel into a service on the pod,
let the pod's GPU do the work over the tunnel.

- **Upscaling — must be redesigned.** The old script ran `batch_upscale.py`
  locally against **ComfyUI on the pod** (HTTP, port 8188). The current upscaler
  loads SeedVR2 **in-process**, so the GPU work happens wherever the script runs.
  A remote-pod upscale now has to invert the flow: provision the pod, get images
  and code onto it (upload, or attach a RunPod network volume), run
  `batch_upscale.py` **on the pod**, then fetch the results back.
- **Tagging — pattern still works.** Tagging talks to Ollama over a URL. The old
  script tunnelled to **Ollama on the pod** (port 11434) and ran
  `tag_and_rename.py` locally against it. The same thing works today with **no
  code change**: start Ollama on the pod, open `ssh -L 11434:localhost:11434`,
  and set **Settings → Ollama URL** to `http://127.0.0.1:11434`.

Prefer building any new version **inside the app** (GUI/Settings, Python,
cross-platform) rather than as standalone PowerShell.

## RunPod REST API

- Base URL: `https://rest.runpod.io/v1`
- Auth header: `Authorization: Bearer <api_key>`
- Stop a pod (used for auto-stop-when-done):
  `POST /pods/{pod_id}/stop`
- The old scripts assumed the pod was **already running** (started manually from
  the dashboard) — they only ever *stopped* it. If a new feature should also
  start pods, check the current RunPod REST docs for the start/create endpoints
  rather than assuming.

## Suggested `runpod` config section

```jsonc
"runpod": {
    "pod_id": "",
    "api_key": "",
    "ssh_host": "",
    "ssh_port": 22,
    "ssh_key_path": "%USERPROFILE%\\.ssh\\id_ed25519_runpod",
    "hourly_rate": 0.90,            // USD/h, for cost estimates
    "stop_pod_when_done": true
}
```

## SSH connectivity + tunnel

```powershell
# Connectivity check (also confirms the GPU)
ssh -i <key> -p <port> -o StrictHostKeyChecking=no root@<host> `
    "echo connected && nvidia-smi --query-gpu=name,memory.total --format=csv,noheader"

# Tunnel a pod service to localhost (Ollama shown; ComfyUI was 8188)
ssh -i <key> -p <port> -o StrictHostKeyChecking=no -o ServerAliveInterval=30 `
    -L 11434:localhost:11434 -N root@<host>
```

## Cost tracking + auto-stop safety

The most valuable bit: never leave a billed pod idle.

- Track `sessionElapsed = end - start`; `cost = round(hours * hourly_rate, 2)`.
- After the run, **stop the pod automatically** with a short cancel countdown
  (old default: 60 s, cancel on Escape). Always close the SSH tunnel regardless.
- If the API stop call fails, tell the user to stop it manually and show the
  pod ID — don't fail silently.

## Discord completion embed

Send on completion (green `0x2ecc71` / `3066993`), with fields:
duration, estimated cost, hourly rate, processed count, average time per image,
pod ID, completed-at timestamp. The app's Python `send_discord_notification`
already covers the general case; remote runs would just add the cost/pod fields.
