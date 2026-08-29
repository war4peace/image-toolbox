#!/usr/bin/env bash
# provision.sh — setup of the model network volume for Image Toolbox remote
# upscaling. Runs ON the pod (RunPod pytorch image: torch 2.8 / CUDA 12.8 already
# present). Writes everything to the network volume at /workspace so disposable pods
# just mount it and start fast.
#
# INCREMENTAL & safe to re-run: each artifact is kept if already valid and only
# (re)fetched when it changed — the venv is skipped via a stamp, the cached ollama
# runtime is reused, SeedVR2 weights skip valid files. Re-provisioning also PRUNES
# obsolete models (Ollama models outside the desired set, and stale SeedVR2 weights)
# so switching a model reclaims storage instead of piling up. So a model change or a
# minor update is a cheap re-provision, not a fresh volume + full re-download. See
# the "Incremental provisioning controls" flags below to force a rebuild or keep
# obsolete models.
#
# A fresh provision caches the COMMON model set (~40 GB, fits the 50 GB volume): the
# three SeedVR2 DiT tiers (3B Q8 / 7B FP8-mixed / 7B FP16) + the three vision tiers
# (qwen3-vl 2B/4B/8B-instruct), so a user can switch to a lighter model in Settings
# and it already lives on the volume (no start-of-run download).
#
# Layout on the volume:
#   /workspace/seedvr2          the SeedVR2 engine (code)
#   /workspace/pydeps           light python deps (torch/torchvision come from
#                               the image; everything else --target'd here)
#   /workspace/models/seedvr2   SeedVR2 weights (~26 GB: 3B + 7B tiers + shared VAE)
#   /workspace/models/ollama    Ollama vision models (~11 GB: all three tiers)
set -euo pipefail

VOL=/workspace
SEEDVR2_DIR="$VOL/seedvr2"
VENV="$VOL/venv"
MODELS="$VOL/models"
SEEDVR2_MODELS="$MODELS/seedvr2"
OLLAMA_MODELS_DIR="$MODELS/ollama"
DIT_MODEL="${DIT_MODEL:-seedvr2_ema_7b_fp16.safetensors}"
# SeedVR2 DiT weights to cache on the volume. Like the vision models, we cache the
# whole common set (the three wizard tiers: 3B Q8, 7B FP8-mixed, 7B FP16) so a user
# can pick a smaller/lighter model in Settings and it is already on the volume — no
# download at the start of a run. The 50 GB volume fits this set (~26 GB) alongside
# the three vision tiers (~11 GB). DIT_MODEL_LIST is a space-separated list; the
# single configured DIT_MODEL is always included too (de-duplicated), so a custom or
# off-tier choice (e.g. a sharp variant) is guaranteed present. The shared VAE is
# downloaded once.
#
# THIS LIST IS DELIBERATELY SHORTER THAN WHAT THE GUI OFFERS, and it must stay that
# way. Since 0.6.3 (#26 Part A) all TEN hash-pinned DiT variants are selectable in
# Settings; pre-caching all ten would need 70.1 GiB of weights (measured; see the
# sizes in scripts/seedvr2_models.py), against 26.6 GiB for these three. That does
# not fit a 50 GB volume next to the three vision tiers (~11 GB) and the venv.
#
# Listing a model in the GUI costs this volume NOTHING, because availability and
# pre-caching are separate: the configured DIT_MODEL is appended above and always
# fetched, and a model switched to WITHOUT a re-provision is downloaded to the volume
# on first use (remote_run's health wait budgets 15 min for exactly that). So the
# only cost of an off-list pick is a slow first run -- and volume free space, which
# is what the volume-size prompt in gui/tab_runpod.py now sizes for.
#
# Add a weight here only to make it instant on a fresh volume, and only after
# checking the budget: 7B FP16 / Sharp FP16 are 15.35 GiB each, the FP8-mixed pair
# 7.88, the 7B Q4 pair 4.43, 3B FP16 6.32, 3B Q8 3.41, 3B FP8 3.16, 3B Q4 1.86.
DIT_MODEL_LIST="${DIT_MODEL_LIST:-seedvr2_ema_7b_fp16.safetensors seedvr2_ema_7b_fp8_e4m3fn_mixed_block35_fp16.safetensors seedvr2_ema_3b-Q8_0.gguf}"
# Vision models to cache on the volume. Provisioning is meant to be a one-time job,
# so we pull the whole common set (all three wizard tiers) by default — a re-provision
# is a billed pod, and the extra ~5 GB of volume storage is cheap vs. re-running it
# every time the user switches the configured model. OLLAMA_MODEL_LIST is a space-
# separated list; OLLAMA_MODEL (single) is still honoured as a fallback/override and
# is always included so a custom-configured model is never missed. NOTE: not named
# OLLAMA_MODELS — that is Ollama's own reserved env var for the models directory
# (set to $OLLAMA_MODELS_DIR below).
OLLAMA_MODEL="${OLLAMA_MODEL:-qwen3-vl:8b-instruct}"
OLLAMA_MODEL_LIST="${OLLAMA_MODEL_LIST:-qwen3-vl:8b-instruct qwen3-vl:4b-instruct qwen3-vl:2b-instruct}"
SEEDVR2_ZIP="https://github.com/numz/ComfyUI-SeedVR2_VideoUpscaler/archive/refs/heads/main.zip"

# ── Incremental provisioning controls ────────────────────────────────────────
# Re-provisioning a volume keeps whatever is already valid and fetches only what
# changed, so switching a model (or a minor update) no longer means a fresh volume
# and a full ~27 GB re-download. These env knobs tune it; the defaults do the right
# thing (keep valid artifacts, prune obsolete models, download only what is new):
#   FORCE_ENGINE=1   re-download the SeedVR2 engine code even if it is present
#   FORCE_VENV=1     rebuild the python venv even if its stamp is unchanged
#   OLLAMA_PRUNE=0   keep Ollama models that are NOT in the desired set
#   SEEDVR2_PRUNE=0  keep SeedVR2 weight files other than the current DiT + VAE
FORCE_ENGINE="${FORCE_ENGINE:-0}"
FORCE_VENV="${FORCE_VENV:-0}"
OLLAMA_PRUNE="${OLLAMA_PRUNE:-1}"
SEEDVR2_PRUNE="${SEEDVR2_PRUNE:-1}"

echo "================ provisioning to $VOL ================"
nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader || true
python --version
mkdir -p "$SEEDVR2_MODELS" "$OLLAMA_MODELS_DIR"
rm -rf "$VOL/pydeps"   # remove the abandoned --target dir from earlier attempts

# 1. SeedVR2 engine code -------------------------------------------------------
if [ "$FORCE_ENGINE" = "1" ] || [ ! -f "$SEEDVR2_DIR/inference_cli.py" ]; then
  echo "---- downloading SeedVR2 engine ----"
  cd /tmp
  curl -fSL -o seedvr2.zip "$SEEDVR2_ZIP"
  command -v unzip >/dev/null 2>&1 || { apt-get update -qq && apt-get install -y -qq unzip; }
  rm -rf /tmp/seedvr2_extract
  unzip -q -o seedvr2.zip -d /tmp/seedvr2_extract
  rm -rf "$SEEDVR2_DIR"
  mv /tmp/seedvr2_extract/ComfyUI-SeedVR2_VideoUpscaler-main "$SEEDVR2_DIR"
  rm -rf seedvr2.zip /tmp/seedvr2_extract
else
  echo "---- SeedVR2 engine already present ----"
fi

# 2. venv on the volume, INHERITING the image's torch via --system-site-packages
#    (so pip sees torch is already satisfied and does NOT pull a second,
#    conflicting torch/CUDA — the failure mode of an earlier --target attempt).
#    The light deps install into the venv and persist on the volume.
#
#    Incremental: rebuilding the venv (a full pip install of ~30 packages) is the
#    single most wasteful part of a re-provision, so we SKIP it when nothing that
#    affects it changed. A stamp file records a hash of the resolved requirements +
#    constraints + explicit packages + the IMAGE's torch version; if the stamp still
#    matches AND the venv imports cleanly, we reuse it untouched. A changed image
#    torch (its ABI would mismatch the pinned torchvision) or FORCE_VENV=1 forces a
#    rebuild.
# The image ships torch 2.9.1+cu128 and torchaudio 2.9.1 but NO torchvision. Pin
# torch so nothing (timm/torchvision) drags in a newer torch that would mismatch the
# system torchaudio; install the matching torchvision from the cu128 index so its ABI
# matches the image torch.
grep -ivE '^torch($|[<>=~ ])|^torchvision' "$SEEDVR2_DIR/requirements.txt" > /tmp/reqs.txt
echo "torch==2.9.1" > /tmp/constraints.txt
EXPLICIT_PKGS="pillow piexif timm huggingface_hub torchvision==0.24.1"
SYS_TORCH="$(python -c 'import torch,sys;sys.stdout.write(torch.__version__)' 2>/dev/null || echo none)"
NEW_STAMP="$( { cat /tmp/reqs.txt /tmp/constraints.txt; printf '%s\n%s\n' "$EXPLICIT_PKGS" "$SYS_TORCH"; } | sha256sum | cut -d' ' -f1)"
STAMP_FILE="$VENV/.imgtbx_provision_stamp"

if [ "$FORCE_VENV" != "1" ] && [ -x "$VENV/bin/python" ] && [ -f "$STAMP_FILE" ] \
   && [ "$(cat "$STAMP_FILE" 2>/dev/null)" = "$NEW_STAMP" ] \
   && "$VENV/bin/python" -c "import torch,torchvision,torchaudio,PIL,piexif,timm" 2>/dev/null; then
  echo "---- venv already up to date at $VENV (stamp matches) — reusing ----"
else
  echo "---- (re)building venv at $VENV (reusing the image's torch ${SYS_TORCH}) ----"
  # Wipe the existing venv before rebuilding. On the network volume, a file held
  # open by ANOTHER pod (e.g. a still-running worker's /workspace/venv/bin/python,
  # with numpy/PIL/torchvision .so mmap'd) makes `rm -rf` NFS-silly-rename it and
  # report "Directory not empty" — fatal under `set -e`, aborting the whole
  # provision at step 2. So: try a plain wipe, and if the dir survives (busy),
  # rename it aside (succeeds even with open handles) so venv creation always gets
  # a clean path. The leftover is best-effort deleted. The right fix operationally
  # is to not provision while a pod still mounts the volume, but this keeps a 20-min
  # provision from dying on one stray handle.
  if [ -e "$VENV" ]; then
    rm -rf "$VENV" 2>/dev/null || true
    if [ -e "$VENV" ]; then
      echo "  venv busy (open handle on the volume?) — moving it aside"
      mv "$VENV" "${VENV}.old.$$" 2>/dev/null || true
      rm -rf "${VENV}.old.$$" 2>/dev/null || true
    fi
  fi
  python -m venv --system-site-packages "$VENV"
  "$VENV/bin/pip" install --no-cache-dir -c /tmp/constraints.txt \
      --extra-index-url https://download.pytorch.org/whl/cu128 \
      -r /tmp/reqs.txt $EXPLICIT_PKGS
  echo "$NEW_STAMP" > "$STAMP_FILE"
fi
echo "---- versions in venv (torch must stay 2.9.1+cu128) ----"
"$VENV/bin/python" -c "import torch,torchvision,torchaudio;print('torch',torch.__version__,'tv',torchvision.__version__,'ta',torchaudio.__version__,'cuda',torch.cuda.is_available())"

# 2b. Auto-straighten orientation CNN weights (ternaus/check_orientation, ~82 MB)
#     cached onto the volume's torch hub dir so disposable pods don't re-download
#     and the worker's first /orient is instant. The worker runs with the same
#     TORCH_HOME so it finds them. (timm is already installed above.)
echo "---- caching the orientation CNN weights to $MODELS/torch ----"
export TORCH_HOME="$MODELS/torch"
mkdir -p "$TORCH_HOME"
"$VENV/bin/python" - <<'PY'
import torch
url = ("https://github.com/ternaus/check_orientation/releases/download/"
       "v0.0.3/2020-11-16_resnext50_32x4d.zip")
torch.hub.load_state_dict_from_url(url, progress=False, map_location="cpu")
print("orientation weights cached")
PY

# 3. SeedVR2 weights to the volume (download_weight skips files already valid) --
# Download the whole cached DiT set (DIT_MODEL_LIST + the configured DIT_MODEL,
# de-duplicated) plus the shared VAE. The CONFIGURED DiT is required (a failure
# there fails the provision); the extra tier DiTs are best-effort (logged, skipped)
# so one bad convenience download can't abort the job. Prune (default on): after the
# downloads, remove any weight file NOT in the intended set — e.g. a DiT from a
# previous provision that is no longer offered — but only when the configured DiT
# succeeded, so we never delete a working fallback after a failed run.
echo "---- downloading SeedVR2 weights to $SEEDVR2_MODELS ----"
PYTHONPATH="$SEEDVR2_DIR" "$VENV/bin/python" - "$SEEDVR2_DIR" "$SEEDVR2_MODELS" "$SEEDVR2_PRUNE" $DIT_MODEL_LIST "$DIT_MODEL" <<'PY'
import os, sys
seedvr2_dir, model_dir, prune = sys.argv[1], sys.argv[2], sys.argv[3]
dits = sys.argv[4:]                      # tier list, then the configured DiT last
required = dits[-1] if dits else None    # the configured DiT: must succeed
sys.path.insert(0, seedvr2_dir)
from src.utils.downloads import download_weight
from src.utils.model_registry import DEFAULT_VAE

seen = []                                # de-dup, keep order
for d in dits:
    if d and d not in seen:
        seen.append(d)

failed = []
for dit in seen:
    ok = download_weight(dit_model=dit, vae_model=DEFAULT_VAE, model_dir=model_dir)
    tag = "" if dit != required else " (configured)"
    print("download_weight", dit + tag, "ok:", ok)
    if not ok:
        failed.append(dit)

# Prune only when the configured DiT is safely in place; keep the whole intended set
# (a failed extra isn't fully on disk, and we never risk deleting a good fallback).
if prune == "1" and required not in failed:
    keep = set(seen) | {DEFAULT_VAE}
    for name in sorted(os.listdir(model_dir)):
        if name in keep or not name.endswith((".safetensors", ".gguf")):
            continue
        path = os.path.join(model_dir, name)
        if os.path.isfile(path):
            try:
                os.remove(path)
                print("pruned obsolete SeedVR2 weight:", name)
            except OSError as exc:
                print("could not prune", name, ":", exc)

if failed:
    print("WARNING: DiT downloads failed:", " ".join(failed))
# Fail the provision only if the CONFIGURED DiT could not be fetched.
sys.exit(1 if required in failed else 0)
PY

# 4. Ollama + vision models to the volume --------------------------------------
# Prefer the ollama runtime already cached on the volume, so a re-provision skips
# re-installing it; else a system ollama; else a fresh install. Modern Ollama is
# TWO parts: the `ollama` binary AND a lib/ollama/ dir holding the separate
# `llama-server` + GPU-runner libs — the cached binary resolves them at
# <bindir>/../lib/ollama, i.e. /workspace/ollama/lib/ollama, so both must be cached.
CACHED_OLLAMA="$VOL/ollama/bin/ollama"
OLLAMA_FRESH=0
if command -v ollama >/dev/null 2>&1; then
  echo "---- using system ollama ----"
elif [ -x "$CACHED_OLLAMA" ] && [ -d "$VOL/ollama/lib/ollama" ]; then
  echo "---- reusing cached ollama from the volume ----"
  export PATH="$VOL/ollama/bin:$PATH"
else
  echo "---- installing ollama ----"
  curl -fsSL https://ollama.com/install.sh | sh
  OLLAMA_FRESH=1
fi
# Cache the ollama RUNTIME on the volume when freshly installed (a reused cache is
# already the source, so re-copying it would be pointless). The remote_run tag
# session prefers /workspace/ollama/bin/ollama, falling back to a fresh install if
# it (or its lib) is missing (an older volume). Caching only the binary leaves the
# reused pod unable to start the model server ("llama-server binary not found"),
# 500-ing every inference — so cache the lib dir too.
if [ "$OLLAMA_FRESH" = "1" ]; then
  OLLAMA_SRC="$(command -v ollama || true)"
  if [ -n "$OLLAMA_SRC" ]; then
    mkdir -p "$VOL/ollama/bin" "$VOL/ollama/lib/ollama"
    cp -f "$OLLAMA_SRC" "$VOL/ollama/bin/ollama" 2>/dev/null || true
    for libdir in /usr/local/lib/ollama /usr/lib/ollama "$(dirname "$OLLAMA_SRC")/../lib/ollama"; do
      [ -d "$libdir" ] || continue
      cp -rf "$libdir/." "$VOL/ollama/lib/ollama/" 2>/dev/null && break
    done
  fi
fi
export OLLAMA_MODELS="$OLLAMA_MODELS_DIR"
pkill -f "ollama serve" 2>/dev/null || true
nohup ollama serve >/tmp/ollama.log 2>&1 &
for i in $(seq 1 30); do
  curl -s http://127.0.0.1:11434/api/version >/dev/null 2>&1 && break
  sleep 1
done
# Pull every model in the list plus the single configured one, de-duplicated so a
# model already named in the list isn't pulled twice. `ollama pull` is a no-op (a
# quick manifest check) for a model already on the volume, so re-provisioning is
# cheap. A single failed pull must not abort the whole provision (set -e is on), so
# each is guarded — a bad/renamed tag is logged and skipped, the rest still cache.
_pulled=""
for _m in $OLLAMA_MODEL_LIST $OLLAMA_MODEL; do
  case " $_pulled " in *" $_m "*) continue ;; esac   # already pulled this one
  _pulled="$_pulled $_m"
  echo "---- pulling $_m to $OLLAMA_MODELS_DIR ----"
  ollama pull "$_m" || echo "  WARNING: pull of '$_m' failed; skipping it."
done

# Prune Ollama models no longer wanted (default on). The desired set is exactly
# what we just (re)pulled ($_pulled); anything else on the volume — e.g. a
# qwen2.5vl:7b from an older provision — is obsolete, and removing it reclaims
# volume storage. The ':latest' suffix is matched loosely so a model pulled under a
# bare name (stored as name:latest) is never mistaken for obsolete. OLLAMA_PRUNE=0
# keeps every installed model.
if [ "$OLLAMA_PRUNE" = "1" ]; then
  installed="$(ollama list 2>/dev/null | awk 'NR>1{print $1}' || true)"
  for _m in $installed; do
    case " $_pulled " in *" $_m "*) continue ;; esac         # wanted, exact tag
    _base="${_m%:latest}"
    case " $_pulled " in *" $_base "*|*" $_base:latest "*) continue ;; esac
    echo "---- pruning obsolete Ollama model: $_m ----"
    ollama rm "$_m" || echo "  WARNING: could not remove '$_m'."
  done
fi

# 5. ffmpeg (static) to the volume ---------------------------------------------
# The Video Upscaler's default H.265 10-bit writer shells out to `ffmpeg` on the
# pod. Cache a static build (with libx264/libx265) on the volume so every disposable
# pod has it without apt. remote_run also resolves it at launch (system ffmpeg wins;
# else this cache; else a one-off fetch), so this just makes the first run fast.
FFMPEG_DIR="$VOL/ffmpeg"
if [ -x "$FFMPEG_DIR/ffmpeg" ]; then
  echo "---- ffmpeg already cached ----"
else
  echo "---- caching static ffmpeg to $FFMPEG_DIR ----"
  mkdir -p "$FFMPEG_DIR"
  cd /tmp
  curl -fSL -o ffmpeg.txz \
    "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz"
  rm -rf /tmp/ffmpeg_extract && mkdir -p /tmp/ffmpeg_extract
  tar -xJf ffmpeg.txz -C /tmp/ffmpeg_extract
  cp /tmp/ffmpeg_extract/ffmpeg-*-amd64-static/ffmpeg \
     /tmp/ffmpeg_extract/ffmpeg-*-amd64-static/ffprobe "$FFMPEG_DIR/"
  chmod +x "$FFMPEG_DIR/ffmpeg" "$FFMPEG_DIR/ffprobe"
  rm -rf ffmpeg.txz /tmp/ffmpeg_extract
  "$FFMPEG_DIR/ffmpeg" -hide_banner -version | head -1 || true
fi

echo "================ provisioning complete ================"
echo "Volume contents:"
du -sh "$SEEDVR2_DIR" "$VENV" "$SEEDVR2_MODELS" "$OLLAMA_MODELS_DIR" "$FFMPEG_DIR" 2>/dev/null || true
