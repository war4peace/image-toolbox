#!/usr/bin/env bash
# provision.sh — one-time setup of the model network volume for Image Toolbox
# remote upscaling. Runs ON the pod (RunPod pytorch image: torch 2.8 / CUDA 12.8
# already present). Writes everything to the network volume at /workspace so
# disposable pods just mount it and start fast. Safe to re-run (idempotent-ish:
# existing engine/weights are reused).
#
# Layout on the volume:
#   /workspace/seedvr2          the SeedVR2 engine (code)
#   /workspace/pydeps           light python deps (torch/torchvision come from
#                               the image; everything else --target'd here)
#   /workspace/models/seedvr2   SeedVR2 weights (~16 GB)
#   /workspace/models/ollama    Ollama vision model (~6 GB)
set -euo pipefail

VOL=/workspace
SEEDVR2_DIR="$VOL/seedvr2"
VENV="$VOL/venv"
MODELS="$VOL/models"
SEEDVR2_MODELS="$MODELS/seedvr2"
OLLAMA_MODELS_DIR="$MODELS/ollama"
DIT_MODEL="${DIT_MODEL:-seedvr2_ema_7b_fp16.safetensors}"
OLLAMA_MODEL="${OLLAMA_MODEL:-qwen2.5vl:7b}"
SEEDVR2_ZIP="https://github.com/numz/ComfyUI-SeedVR2_VideoUpscaler/archive/refs/heads/main.zip"

echo "================ provisioning to $VOL ================"
nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader || true
python --version
mkdir -p "$SEEDVR2_MODELS" "$OLLAMA_MODELS_DIR"
rm -rf "$VOL/pydeps"   # remove the abandoned --target dir from earlier attempts

# 1. SeedVR2 engine code -------------------------------------------------------
if [ ! -f "$SEEDVR2_DIR/inference_cli.py" ]; then
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
echo "---- creating venv at $VENV (reusing the image's torch 2.9.1+cu128) ----"
rm -rf "$VENV"
python -m venv --system-site-packages "$VENV"
grep -ivE '^torch($|[<>=~ ])|^torchvision' "$SEEDVR2_DIR/requirements.txt" > /tmp/reqs.txt
# The image ships torch 2.9.1+cu128 and torchaudio 2.9.1 but NO torchvision.
# Pin torch so nothing (timm/torchvision) drags in a newer torch that would
# mismatch the system torchaudio; install the matching torchvision from the
# cu128 index so its ABI matches the image torch.
echo "torch==2.9.1" > /tmp/constraints.txt
"$VENV/bin/pip" install --no-cache-dir -c /tmp/constraints.txt \
    --extra-index-url https://download.pytorch.org/whl/cu128 \
    -r /tmp/reqs.txt pillow piexif timm huggingface_hub torchvision==0.24.1
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
echo "---- downloading SeedVR2 weights to $SEEDVR2_MODELS ----"
PYTHONPATH="$SEEDVR2_DIR" "$VENV/bin/python" - "$SEEDVR2_DIR" "$SEEDVR2_MODELS" "$DIT_MODEL" <<'PY'
import sys
seedvr2_dir, model_dir, dit_model = sys.argv[1], sys.argv[2], sys.argv[3]
sys.path.insert(0, seedvr2_dir)
from src.utils.downloads import download_weight
from src.utils.model_registry import DEFAULT_VAE
ok = download_weight(dit_model=dit_model, vae_model=DEFAULT_VAE, model_dir=model_dir)
print("download_weight ok:", ok)
sys.exit(0 if ok else 1)
PY

# 4. Ollama + vision model to the volume ---------------------------------------
echo "---- installing ollama ----"
command -v ollama >/dev/null 2>&1 || curl -fsSL https://ollama.com/install.sh | sh
# Cache the ollama BINARY on the volume too (not just the models): a disposable
# run-pod for remote Tag & Rename then reuses it without re-downloading. The
# remote_run tag session prefers /workspace/ollama/bin/ollama, falling back to a
# fresh install only if this is missing (an older volume).
OLLAMA_SRC="$(command -v ollama || true)"
if [ -n "$OLLAMA_SRC" ]; then
  mkdir -p "$VOL/ollama/bin"
  cp -f "$OLLAMA_SRC" "$VOL/ollama/bin/ollama" 2>/dev/null || true
fi
export OLLAMA_MODELS="$OLLAMA_MODELS_DIR"
pkill -f "ollama serve" 2>/dev/null || true
nohup ollama serve >/tmp/ollama.log 2>&1 &
for i in $(seq 1 30); do
  curl -s http://127.0.0.1:11434/api/version >/dev/null 2>&1 && break
  sleep 1
done
echo "---- pulling $OLLAMA_MODEL to $OLLAMA_MODELS_DIR ----"
ollama pull "$OLLAMA_MODEL"

echo "================ provisioning complete ================"
echo "Volume contents:"
du -sh "$SEEDVR2_DIR" "$VENV" "$SEEDVR2_MODELS" "$OLLAMA_MODELS_DIR" 2>/dev/null || true
