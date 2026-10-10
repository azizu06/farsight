#!/usr/bin/env bash
# One-time Jetson setup after the JetPack 7.2 install. Safe to re-run.
# Usage (on the Jetson): bash ~/farsight/jetson/setup.sh
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$HOME/src"
MODELS="$REPO/models"
export PATH="/usr/local/cuda/bin:$PATH"

step() { printf '\n== %s\n' "$*"; }

step "System packages"
sudo apt-get update
sudo apt-get install -y nvidia-jetpack openssh-server avahi-daemon tmux v4l-utils git cmake \
  build-essential python3-venv python3-pip libcurl4-openssl-dev ffmpeg

step "Hostname farsight (reach it as farsight.local), serial access"
[ "$(hostname)" = farsight ] || sudo hostnamectl set-hostname farsight
groups | grep -q dialout || sudo usermod -aG dialout "$USER"

step "Python env (system site packages so TensorRT's python bindings are visible)"
[ -d "$REPO/jetson/.venv" ] || python3 -m venv --system-site-packages "$REPO/jetson/.venv"
"$REPO/jetson/.venv/bin/pip" install -q -e "$REPO/jetson[dev]" piper-tts
"$REPO/jetson/.venv/bin/python" -c "import tensorrt as t; print('TensorRT', t.__version__)" \
  || echo "WARN: tensorrt python bindings not found (check: dpkg -l | grep libnvinfer)"

build() {  # build <name> <git url>
  local dir="$SRC/$1"
  [ -d "$dir" ] || git clone --depth 1 "$2" "$dir"
  if [ ! -d "$dir/build/bin" ]; then
    cmake -S "$dir" -B "$dir/build" -DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=87 -DCMAKE_BUILD_TYPE=Release
    cmake --build "$dir/build" -j"$(nproc)"
  fi
}
mkdir -p "$SRC"
step "llama.cpp with CUDA (vision-language model server)"
build llama.cpp https://github.com/ggml-org/llama.cpp
step "whisper.cpp with CUDA (speech to text)"
build whisper.cpp https://github.com/ggml-org/whisper.cpp

step "Piper voices (English answers, English + Spanish survivor phrases) in $MODELS/piper"
mkdir -p "$MODELS/piper"
for voice in en/en_US/lessac/medium/en_US-lessac-medium es/es_MX/claude/high/es_MX-claude-high; do
  for ext in onnx onnx.json; do
    file="$MODELS/piper/$(basename "$voice").$ext"
    [ -s "$file" ] || curl -fsSL -o "$file" "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/$voice.$ext" \
      || echo "WARN: could not download $(basename "$file")"
  done
done

step "TensorRT FP16 engines from ONNX in $MODELS (cached by ONNX hash; reruns are instant)"
if compgen -G "$MODELS/*.onnx" >/dev/null; then
  (cd "$REPO" && jetson/.venv/bin/python -m jetson.build_engine "$MODELS"/*.onnx) \
    || echo "WARN: an engine build failed (see above); pass the .onnx to --detector-model meanwhile"
else
  echo "No ONNX models yet: run tools/push_models.sh from a laptop"
fi

step "Checks"
cat /etc/nv_tegra_release || true
nvcc --version | tail -n 1 || true
sudo nvpmodel -q || true
v4l2-ctl --list-devices || true
ls /dev/ttyACM* /dev/ttyUSB* 2>/dev/null || echo "Uno not found (unplug the ESP camera module, check the USB cable)"

echo
echo "Next: bash $REPO/jetson/install_service.sh  (start the server on every boot)"
echo "Done. Log out and back in once for serial access. Power: use the 15 W mode on the 30 W bank."
