#!/usr/bin/env bash
# One-time Jetson setup after the JetPack 7.2 install. Safe to re-run: every step skips finished work.
# Usage (on the Jetson, in tmux so SSH drops do not kill it):
#   bash ~/farsight/jetson/setup.sh [--dry-run] [--skip-builds] [--skip-engines] [--with-vlm] [--power-mode 15W|none]
# sudo asks for your password once at the start (type it yourself; nothing is stored).
# --dry-run prints every state-changing command instead of running it.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
SRC="${FARSIGHT_SRC:-$HOME/src}"
MODELS="$REPO/models"
VENV="${FARSIGHT_VENV:-$REPO/jetson/.venv}"
DRY=0 BUILDS=1 ENGINES=1 VLM=0 POWER=15W
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY=1 ;;
    --skip-builds) BUILDS=0 ;;
    --skip-engines) ENGINES=0 ;;
    --with-vlm) VLM=1 ;;
    --power-mode) POWER="${2:?--power-mode needs a name such as 15W, or none}"; shift ;;
    -h|--help) sed -n '2,7p' "$0"; exit 0 ;;
    *) echo "unknown option: $1 (see --help)" >&2; exit 2 ;;
  esac
  shift
done
export PATH="/usr/local/cuda/bin:$PATH"

WARNINGS=""
step() { printf '\n== %s\n' "$*"; }
warn() { printf 'WARN: %s\n' "$*"; WARNINGS="$WARNINGS- $*"$'\n'; }
run() { if [ "$DRY" = 1 ]; then printf '  [dry-run] %s\n' "$*"; else "$@"; fi; }
have_candidate() { apt-cache policy "$1" 2>/dev/null | grep -q 'Candidate: [^(]'; }

if [ "$DRY" = 0 ]; then
  [ "$(uname -m)" = aarch64 ] || { echo "Not an aarch64 machine: run this on the Jetson (or add --dry-run)"; exit 1; }
  sudo -v
  ( while sleep 50; do sudo -n true 2>/dev/null || exit; done ) &  # keep the sudo ticket alive through the long builds
  trap 'kill $! 2>/dev/null || true' EXIT
fi

step "Preflight"
if [ -f /etc/nv_tegra_release ]; then head -n 1 /etc/nv_tegra_release; else warn "no /etc/nv_tegra_release: is this a JetPack install?"; fi
free_gb=$(df -Pk "$HOME" | awk 'NR==2 {print int($4/1024/1024)}')
[ "${free_gb:-0}" -ge 15 ] || warn "only ${free_gb:-?} GB free in $HOME; the CUDA builds, venv and models need about 15 GB"
mem_gb=$(awk '/MemTotal/ {print int($2/1024/1024)}' /proc/meminfo 2>/dev/null || echo 8)
echo "RAM ${mem_gb} GB, free disk ${free_gb:-?} GB"

step "System packages"
run sudo apt-get update
run sudo apt-get install -y openssh-server avahi-daemon tmux v4l-utils git cmake build-essential \
  python3-venv python3-pip libcurl4-openssl-dev libssl-dev ffmpeg alsa-utils pulseaudio-utils openssl curl rsync
# JetPack runtime (CUDA, TensorRT, trtexec). A missing NVIDIA apt source must not abort the rest of the setup.
for pkg in nvidia-jetpack python3-libnvinfer; do
  if [ "$DRY" = 1 ] || have_candidate "$pkg"; then
    run sudo apt-get install -y "$pkg" || warn "apt could not install $pkg"
  else
    warn "no apt candidate for $pkg: check /etc/apt/sources.list.d for the NVIDIA Jetson repo (docs/jetson-bringup.md)"
  fi
done
run sudo systemctl enable --now ssh avahi-daemon || warn "could not enable ssh/avahi"

step "Hostname farsight (reach it as farsight.local) and device groups"
[ "$(hostname)" = farsight ] || run sudo hostnamectl set-hostname farsight
for group in dialout video; do
  id -nG "$USER" | tr ' ' '\n' | grep -qx "$group" || run sudo usermod -aG "$group" "$USER"
done

step "Power mode $POWER (the 30 W power bank cannot sustain MAXN SUPER)"
if [ "$POWER" = none ]; then
  echo "skipped (--power-mode none)"
else
  mode_id="$(sed -n "s/.*POWER_MODEL[[:space:]]*ID=\([0-9][0-9]*\)[[:space:]]*NAME=${POWER}[[:space:]>].*/\1/p" /etc/nvpmodel.conf 2>/dev/null | head -n 1)" || mode_id=""
  if [ -n "$mode_id" ]; then
    if [ "$DRY" = 0 ] && sudo nvpmodel -q 2>/dev/null | grep -qx "NV Power Mode: $POWER"; then
      echo "already in $POWER"
    else
      run sudo nvpmodel -m "$mode_id"
    fi
  else
    warn "no power mode named $POWER in /etc/nvpmodel.conf; list them with: grep POWER_MODEL /etc/nvpmodel.conf"
  fi
fi

step "Python env (system site packages so TensorRT's python bindings are visible)"
[ -x "$VENV/bin/python" ] || run python3 -m venv --system-site-packages "$VENV"
if [ -f "$VENV/pyvenv.cfg" ] && grep -q 'include-system-site-packages = false' "$VENV/pyvenv.cfg"; then
  echo "venv was created without system site packages; fixing so TensorRT is visible"
  run sed -i 's/include-system-site-packages = false/include-system-site-packages = true/' "$VENV/pyvenv.cfg"
fi
run "$VENV/bin/pip" install -q -e "$REPO/jetson[dev,onnx]"
run "$VENV/bin/pip" install -q piper-tts || warn "piper-tts failed to install: the headset falls back to browser speech"
if [ "$DRY" = 0 ]; then
  "$VENV/bin/python" -c "import tensorrt as t; print('TensorRT', t.__version__)" \
    || warn "tensorrt python bindings not importable (try: sudo apt install python3-libnvinfer; dpkg -l | grep nvinfer)"
  "$VENV/bin/python" -c "import cv2, onnxruntime, serial; print('cv2', cv2.__version__, '| onnxruntime', onnxruntime.__version__)" \
    || warn "cv2/onnxruntime/pyserial import failed in $VENV"
  command -v trtexec >/dev/null || [ -x /usr/src/tensorrt/bin/trtexec ] \
    || warn "trtexec not found (find / -name trtexec 2>/dev/null); jetson.build_engine needs it"
fi

step "Models that can be downloaded (Piper English + Spanish voices, face model, whisper base.en)"
groups=piper,face,whisper
[ "$VLM" = 0 ] || groups="$groups,vlm"
fetch_flags=(); [ "$DRY" = 0 ] || fetch_flags=(--dry-run)
(cd "$REPO" && python3 -m tools.fetch_models --groups "$groups" ${fetch_flags[@]+"${fetch_flags[@]}"}) \
  || warn "some downloads failed (offline?). Push them from a laptop instead: tools/push_models.sh --fetch $USER@farsight.local"

build() {  # build <name> <git url> <cmake target...>: skips when the first target's binary exists
  local name="$1" url="$2" dir="$SRC/$1"; shift 2
  local binary="$dir/build/bin/$1"
  if [ -x "$binary" ]; then echo "$name: $1 already built"; return 0; fi
  if [ ! -d "$dir/.git" ]; then
    run rm -rf "$dir.partial"
    run git clone --depth 1 "$url" "$dir.partial" && run mv "$dir.partial" "$dir"
  fi
  # Each CUDA translation unit can take 1-2 GB of RAM: 8 GB boards need fewer jobs than cores.
  local cpus; cpus="$(nproc 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null || echo 4)"
  local jobs=$(( mem_gb / 2 )); [ "$jobs" -ge 1 ] || jobs=1; [ "$jobs" -le "$cpus" ] || jobs="$cpus"
  local cuda_args=(-DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=87 -DCMAKE_BUILD_TYPE=Release)
  [ ! -x /usr/local/cuda/bin/nvcc ] || cuda_args+=(-DCMAKE_CUDA_COMPILER=/usr/local/cuda/bin/nvcc)
  run cmake -S "$dir" -B "$dir/build" "${cuda_args[@]}" -DLLAMA_CURL=OFF \
    || { warn "$name: cmake configure failed (nvcc on PATH? CUDA_ARCHITECTURES=87 is the Orin)"; return 0; }
  run cmake --build "$dir/build" -j"$jobs" --target "$@" \
    || warn "$name: build failed. Out of memory? Re-run with fewer jobs: cmake --build $dir/build -j2 --target $*"
}
if [ "$BUILDS" = 1 ]; then
  run mkdir -p "$SRC"
  step "llama.cpp with CUDA sm_87 (vision-language model server; 20-40 min on first run)"
  build llama.cpp https://github.com/ggml-org/llama.cpp llama-server
  step "whisper.cpp with CUDA sm_87 (speech to text)"
  build whisper.cpp https://github.com/ggml-org/whisper.cpp whisper-server whisper-cli
else
  step "llama.cpp / whisper.cpp builds skipped (--skip-builds)"
fi

step "TensorRT FP16 engines from the detector ONNX files (cached by ONNX hash; reruns are instant)"
if [ "$ENGINES" = 0 ]; then
  echo "skipped (--skip-engines)"
elif compgen -G "$MODELS/yolo11n*.onnx" >/dev/null; then
  (cd "$REPO" && run "$VENV/bin/python" -m jetson.build_engine "$MODELS"/yolo11n*.onnx) \
    || warn "an engine build failed (see above); the server still runs the .onnx on CPU until it is fixed"
else
  warn "no models/yolo11n*.onnx yet: run tools/push_models.sh from the laptop, then re-run this script"
fi

step "Hardware checks"
if [ "$DRY" = 1 ]; then
  echo "skipped in --dry-run"
else
  head -n 1 /etc/nv_tegra_release 2>/dev/null || true
  nvcc --version 2>/dev/null | tail -n 1 || warn "nvcc not on PATH (/usr/local/cuda/bin)"
  sudo nvpmodel -q 2>/dev/null || true
  v4l2-ctl --list-devices 2>/dev/null || warn "v4l2-ctl found nothing: is the camera plugged in?"
  ls /dev/ttyACM* /dev/ttyUSB* 2>/dev/null || warn "Uno not found: unplug the ESP camera module, check the USB cable"
fi

echo
if [ -n "$WARNINGS" ]; then printf 'Finished with warnings:\n%s' "$WARNINGS"; else echo "Finished with no warnings."; fi
echo "Next: bash $REPO/jetson/install_service.sh   (start the server on every boot)"
echo "Log out and back in once (or reboot) so the dialout/video groups apply to interactive shells."
