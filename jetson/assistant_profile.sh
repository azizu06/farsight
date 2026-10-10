#!/usr/bin/env bash
# Switch the Jetson assistant between memory profiles with one command; only the two assistant units restart.
#   bash jetson/assistant_profile.sh status
#   bash jetson/assistant_profile.sh default    # Qwen3-VL-2B + whisper base.en (fits with the desktop running)
#   bash jetson/assistant_profile.sh headless   # Qwen3-VL-2B + whisper small.en (needs the desktop off: about +0.4 GB)
# Profiles are systemd drop-ins (/etc/systemd/system/farsight-*.service.d/profile.conf) that override the
# FARSIGHT_SELECTED_* defaults rendered by install_service.sh. farsight.service and /etc/farsight.env are untouched.
# Measurements and memory math: docs/assistant-setup.md ("Jetson memory profiles").
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
MODELS="${FARSIGHT_MODELS:-$REPO/models}"
LLM_DIR=/etc/systemd/system/farsight-llm.service.d
WHISPER_DIR=/etc/systemd/system/farsight-whisper.service.d

need() { [ -s "$MODELS/$1" ] || { echo "missing $MODELS/$1 (copy it from the Mac; never download on the rover)" >&2; exit 1; }; }
dropin() {  # dropin <dir> <Environment line>
  sudo mkdir -p "$1"
  printf '[Service]\n%s\n' "$2" | sudo tee "$1/profile.conf" >/dev/null
}
restart_units() {
  sudo systemctl daemon-reload
  sudo systemctl restart farsight-whisper.service farsight-llm.service
}

case "${1:-status}" in
  default)
    sudo rm -f "$LLM_DIR/profile.conf" "$WHISPER_DIR/profile.conf"
    restart_units ;;
  headless)
    need Qwen3VL-2B-Instruct-Q4_K_M.gguf; need mmproj-Qwen3VL-2B-Instruct-Q8_0.gguf; need ggml-small.en.bin
    if systemctl is-active --quiet graphical.target; then
      echo "refusing: the desktop is running; small.en needs the headroom the desktop uses. sudo systemctl isolate multi-user.target first." >&2
      exit 1
    fi
    sudo rm -f "$LLM_DIR/profile.conf"
    dropin "$WHISPER_DIR" "Environment=\"FARSIGHT_SELECTED_WHISPER_MODEL=$MODELS/ggml-small.en.bin\""
    restart_units ;;
  status) ;;
  *) echo "usage: $0 status|default|headless" >&2; exit 2 ;;
esac

systemctl is-active farsight-llm farsight-whisper farsight || true
for unit in farsight-llm farsight-whisper; do
  systemctl show "$unit" -p Environment --value | tr ' ' '\n' | grep SELECTED || true
done
free -m | sed -n '1,2p'
