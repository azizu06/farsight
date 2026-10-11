#!/usr/bin/env bash
# Install the Farsight systemd units on the Jetson so the server starts on every boot. Safe to re-run;
# an existing /etc/farsight.env is never overwritten (edit it, or pass --reset-env to back it up and re-render).
# Usage: bash jetson/install_service.sh [--dry-run] [--no-tls] [--no-assistant] [--reset-env] [--camera-source phone|usb] [--serial-port PATH]
# Model overrides: --llm-model PATH --llm-projector PATH --whisper-model PATH --piper-model PATH
# sudo asks for your password (typed by you). Nothing secret is written: TLS keys are generated locally by make_cert.sh.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
SRC="${FARSIGHT_SRC:-$HOME/src}"
RUN_USER="${SUDO_USER:-$USER}"
ENV_FILE="${FARSIGHT_ENV_FILE:-/etc/farsight.env}"
TLS_DIR=/etc/farsight/tls
DRY=0 TLS=1 ASSISTANT=1 RESET=0
CAMERA_SOURCE=usb
CAMERA_EXPLICIT=0
MODEL_ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY=1 ;;
    --no-tls) TLS=0 ;;
    --no-assistant) ASSISTANT=0 ;;
    --reset-env) RESET=1 ;;
    --camera-source)
      [ $# -ge 2 ] || { echo "--camera-source needs phone or usb" >&2; exit 2; }
      CAMERA_SOURCE="$2"; CAMERA_EXPLICIT=1; shift
      case "$CAMERA_SOURCE" in phone|usb) ;; *) echo "--camera-source needs phone or usb" >&2; exit 2 ;; esac ;;
    --llm-model|--llm-projector|--whisper-model|--piper-model|--serial-port)
      [ $# -ge 2 ] || { echo "$1 needs a model path" >&2; exit 2; }
      MODEL_ARGS+=("$1" "$2"); shift ;;
    -h|--help) sed -n '2,6p' "$0"; exit 0 ;;
    *) echo "unknown option: $1 (see --help)" >&2; exit 2 ;;
  esac
  shift
done

step() { printf '\n== %s\n' "$*"; }
run() { if [ "$DRY" = 1 ]; then printf '  [dry-run] %s\n' "$*"; else "$@"; fi; }
[ "$DRY" = 1 ] || [ -x "$REPO/jetson/.venv/bin/python" ] || { echo "Run jetson/setup.sh first: no venv at $REPO/jetson/.venv"; exit 1; }

# Select models using the same helper that renders the config and unit defaults.
# Existing explicit model paths override automatic selection; the env is read as data, never sourced/evaluated.
select=(python3 -m jetson.envgen --repo "$REPO" --tls-dir "$TLS_DIR" --camera-source "$CAMERA_SOURCE" --env-file "$ENV_FILE")
if [ "${#MODEL_ARGS[@]}" -gt 0 ]; then select+=("${MODEL_ARGS[@]}"); fi
selection="$(cd "$REPO" && "${select[@]}" --selection-json)"
selected() { printf '%s\n' "$selection" | python3 -c 'import json,sys; value=json.load(sys.stdin).get(sys.argv[1], ""); print("1" if value is True else "0" if value is False else value)' "$1"; }
# An explicit safety/profile request must be the configuration actually started.
# Refuse mismatches before certificate generation, env/unit writes, or systemctl.
if [ -f "$ENV_FILE" ] && [ "$RESET" = 0 ]; then
  mismatch=0
  if [ "$CAMERA_EXPLICIT" = 1 ] && [ "$(selected existing_camera_source)" != "$CAMERA_SOURCE" ]; then
    echo "ERROR: existing camera source is $(selected existing_camera_source), but --camera-source $CAMERA_SOURCE was explicitly requested." >&2
    mismatch=1
  fi
  if [ "$(selected serial_override_requested)" = 1 ] && [ "$(selected existing_serial_port)" != "$(selected requested_serial_port)" ]; then
    echo "ERROR: existing serial port differs from the explicit --serial-port request; the requested motor profile was not applied." >&2
    mismatch=1
  fi
  if [ "$mismatch" = 1 ]; then
    echo "Preserved $ENV_FILE and refused to start or modify services. Re-run the same profile with --reset-env to back it up and replace it, or edit the existing config first." >&2
    exit 1
  fi
fi
step "HTTPS certificate"
if [ "$TLS" = 1 ]; then
  if [ "$DRY" = 1 ]; then bash "$REPO/jetson/make_cert.sh" --dry-run --dir "$TLS_DIR"; else bash "$REPO/jetson/make_cert.sh" --dir "$TLS_DIR"; fi
else
  echo "skipped (--no-tls): the server speaks plain HTTP on :8443 (point tools.demo at an explicit http:// upstream)"
fi
printf '%s\n' "$selection" | python3 -c 'import json,sys; [print("WARN: " + warning) for warning in json.load(sys.stdin)["warnings"]]'
LLM_MODEL="$(selected FARSIGHT_LLM_MODEL)"
LLM_MMPROJ="$(selected FARSIGHT_LLM_MMPROJ)"
WHISPER_MODEL="$(selected FARSIGHT_WHISPER_SERVER_MODEL)"
LLM=0 WHISPER=0
if [ "$ASSISTANT" = 1 ]; then
  [ "$(selected llm_ready)" = 1 ] && [ -x "$SRC/llama.cpp/build/bin/llama-server" ] \
    && [ -s "$LLM_MODEL" ] && [ -s "$LLM_MMPROJ" ] && LLM=1
  [ "$(selected whisper_ready)" = 1 ] && [ -x "$SRC/whisper.cpp/build/bin/whisper-server" ] && [ -s "$WHISPER_MODEL" ] && WHISPER=1
fi
for tool in llama.cpp whisper.cpp; do
  [ ! -d "$SRC/$tool/build/bin" ] || compgen -G "$SRC/$tool/build/bin/libggml-cuda.so*" >/dev/null \
    || echo "WARN: $SRC/$tool is a CPU-only build (no libggml-cuda); the assistant will run far slower than on the GPU. Rebuild: jetson/setup.sh or docs/assistant-setup.md"
done
echo "Vision model: ${LLM_MODEL:-not selected}; projector: ${LLM_MMPROJ:-not selected}"
echo "Whisper model: ${WHISPER_MODEL:-not selected}"

step "$ENV_FILE"
if [ -f "$ENV_FILE" ] && [ "$RESET" = 0 ]; then
  echo "kept existing $ENV_FILE. Compare with the template: diff $ENV_FILE $REPO/jetson/farsight.env.example"
else
  render=("${select[@]}")
  [ "$LLM" = 0 ] || render+=(--assistant)
  [ "$WHISPER" = 0 ] || render+=(--whisper)
  if [ "$DRY" = 1 ]; then
    echo "  [dry-run] would write $ENV_FILE (backing up any existing one) from:"
    (cd "$REPO" && "${render[@]}") | grep -v '^#' | grep -v '^$' | sed 's/^/    /'
  else
    [ ! -f "$ENV_FILE" ] || sudo cp "$ENV_FILE" "$ENV_FILE.bak.$(date +%s)"
    (cd "$REPO" && "${render[@]}") | sudo tee "$ENV_FILE" >/dev/null
    sudo chmod 644 "$ENV_FILE"
    echo "wrote $ENV_FILE (camera, serial, detector, certs, voices chosen from what exists). Edit it, then: sudo systemctl restart farsight"
  fi
fi
run sudo mkdir -p /var/lib/farsight/missions /var/lib/farsight/tts
run sudo chown -R "$RUN_USER" /var/lib/farsight

install_unit() {  # install_unit <template> <unit name>
  local template="$REPO/jetson/$1" unit="$2"
  if [ "$DRY" = 1 ]; then echo "  [dry-run] install $unit from $template"; return; fi
  (cd "$REPO" && "${select[@]}" --unit-template "$template" --src "$SRC" --user "$RUN_USER") \
    | sudo tee "/etc/systemd/system/$unit" >/dev/null
  if command -v systemd-analyze >/dev/null; then sudo systemd-analyze verify "/etc/systemd/system/$unit" || echo "WARN: systemd-analyze reported issues for $unit (read them above)"; fi
}
step "systemd units"
[ "$LLM" = 0 ] || install_unit farsight-llm.service farsight-llm.service
[ "$WHISPER" = 0 ] || install_unit farsight-whisper.service farsight-whisper.service
[ "$LLM$WHISPER" != 00 ] || echo "assistant units skipped (need target-native llama/whisper builds and a complete vision pair or Whisper model; re-run after optional assistant setup)"
install_unit farsight.service farsight.service
install_unit farsight-warmup.service farsight-warmup.service
install_unit farsight-warmup.timer farsight-warmup.timer

step "Preflight of $ENV_FILE (missing models, certs or devices would start the server degraded)"
if [ "$DRY" = 1 ]; then echo "  [dry-run] $REPO/jetson/.venv/bin/python -m jetson.launch --check --env-file $ENV_FILE"
else (cd "$REPO" && jetson/.venv/bin/python -m jetson.launch --check --env-file "$ENV_FILE") || echo "WARN: fix the lines above in $ENV_FILE, then: sudo systemctl restart farsight"; fi

step "Start"
# With NetworkManager managing every link, systemd-networkd-wait-online runs to its 2-minute timeout
# on each boot and holds network-online.target (docker, nvargus, ...). Disable it only when networkd
# manages nothing.
if systemctl is-enabled --quiet systemd-networkd-wait-online.service 2>/dev/null \
    && ! networkctl list --no-legend 2>/dev/null | awk '$5 != "unmanaged" {found = 1} END {exit !found}'; then
  run sudo systemctl disable systemd-networkd-wait-online.service
fi
run sudo systemctl daemon-reload
[ "$LLM" = 0 ] || run sudo systemctl enable --now farsight-llm.service
[ "$WHISPER" = 0 ] || run sudo systemctl enable --now farsight-whisper.service
run sudo systemctl enable farsight.service
run sudo systemctl enable farsight-warmup.timer  # 90 s after each boot: re-pin clocks, warm the voice assistant
run sudo systemctl restart farsight.service

echo
echo "Enabled. Logs: journalctl -u farsight -f"
if [ -s "$TLS_DIR/cert.pem" ] && grep -q '^FARSIGHT_SSL_CERTFILE=.' "$ENV_FILE" 2>/dev/null; then
  echo "Probe:  $REPO/jetson/.venv/bin/python -m tools.ready --url https://farsight.local:8443 --ca-file $TLS_DIR/ca.pem"
else
  echo "Probe:  $REPO/jetson/.venv/bin/python -m tools.ready --url http://127.0.0.1:8443"
fi
