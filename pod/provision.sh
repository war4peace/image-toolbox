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
PYDEPS="$VOL/pydeps"
MODELS="$VOL/models"
SEEDVR2_MODELS="$MODELS/seedvr2"
OLLAMA_MODELS_DIR="$MODELS/ollama"
DIT_MODEL="${DIT_MODEL:-seedvr2_ema_7b_fp16.safetensors}"
OLLAMA_MODEL="${OLLAMA_MODEL:-qwen2.5vl:7b}"
SEEDVR2_ZIP="https://github.com/numz/ComfyUI-SeedVR2_VideoUpscaler/archive/refs/heads/main.zip"

echo "================ provisioning to $VOL ================"
nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader || true
python --version
mkdir -p "$SEEDVR2_MODELS" "$OLLAMA_MODELS_DIR" "$PYDEPS"

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

# 2. Light python deps to the volume (torch/torchvision come from the image) ---
echo "---- installing python deps to $PYDEPS ----"
grep -ivE '^torch($|[<>=~ ])|^torchvision' "$SEEDVR2_DIR/requirements.txt" > /tmp/reqs.txt
pip install --no-cache-dir --target="$PYDEPS" -r /tmp/reqs.txt pillow piexif timm huggingface_hub

# 3. SeedVR2 weights to the volume (download_weight skips files already valid) --
echo "---- downloading SeedVR2 weights to $SEEDVR2_MODELS ----"
PYTHONPATH="$PYDEPS:$SEEDVR2_DIR" python - "$SEEDVR2_DIR" "$SEEDVR2_MODELS" "$DIT_MODEL" <<'PY'
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
du -sh "$SEEDVR2_DIR" "$PYDEPS" "$SEEDVR2_MODELS" "$OLLAMA_MODELS_DIR" 2>/dev/null || true
