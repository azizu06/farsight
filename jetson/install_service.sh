#!/usr/bin/env bash
# Install the Farsight systemd units on the Jetson so the server starts on every boot. Safe to re-run;
# an existing /etc/farsight.env is never overwritten (edit it, or pass --reset-env to back it up and re-render).
# Usage (on the Jetson, after jetson/setup.sh): bash jetson/install_service.sh [--dry-run] [--no-tls] [--no-assistant] [--reset-env]
# sudo asks for your password (typed by you). Nothing secret is written: TLS keys are generated locally by make_cert.sh.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
SRC="${FARSIGHT_SRC:-$HOME/src}"
RUN_USER="${SUDO_USER:-$USER}"
ENV_FILE=/etc/farsight.env
TLS_DIR=/etc/farsight/tls
DRY=0 TLS=1 ASSISTANT=1 RESET=0
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY=1 ;;
    --no-tls) TLS=0 ;;
    --no-assistant) ASSISTANT=0 ;;
    --reset-env) RESET=1 ;;
    -h|--help) sed -n '2,5p' "$0"; exit 0 ;;
    *) echo "unknown option: $1 (see --help)" >&2; exit 2 ;;
  esac
  shift
done

step() { printf '\n== %s\n' "$*"; }
run() { if [ "$DRY" = 1 ]; then printf '  [dry-run] %s\n' "$*"; else "$@"; fi; }
[ "$DRY" = 1 ] || [ -x "$REPO/jetson/.venv/bin/python" ] || { echo "Run jetson/setup.sh first: no venv at $REPO/jetson/.venv"; exit 1; }

step "HTTPS certificate"
if [ "$TLS" = 1 ]; then
  if [ "$DRY" = 1 ]; then bash "$REPO/jetson/make_cert.sh" --dry-run --dir "$TLS_DIR"; else bash "$REPO/jetson/make_cert.sh" --dir "$TLS_DIR"; fi
else
  echo "skipped (--no-tls): the server speaks plain HTTP on :8443 (point tools.demo at an explicit http:// upstream)"
fi

# Which optional units can run? Their binaries and models must exist (the unit files also check, via ConditionPathExists).
LLM=0 WHISPER=0
if [ "$ASSISTANT" = 1 ]; then
  [ -x "$SRC/llama.cpp/build/bin/llama-server" ] && [ -s "$REPO/models/Qwen2.5-VL-3B-Instruct-Q4_K_M.gguf" ] \
    && [ -s "$REPO/models/mmproj-Qwen2.5-VL-3B-Instruct-Q8_0.gguf" ] && LLM=1
  [ -x "$SRC/whisper.cpp/build/bin/whisper-server" ] && [ -s "$REPO/models/ggml-base.en.bin" ] && WHISPER=1
fi

step "$ENV_FILE"
if [ -f "$ENV_FILE" ] && [ "$RESET" = 0 ]; then
  echo "kept existing $ENV_FILE. Compare with the template: diff $ENV_FILE $REPO/jetson/farsight.env.example"
else
  render=(python3 -m jetson.envgen --repo "$REPO" --tls-dir "$TLS_DIR")
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
  sed -e "s#@USER@#$RUN_USER#g" -e "s#@REPO@#$REPO#g" -e "s#@SRC@#$SRC#g" "$template" | sudo tee "/etc/systemd/system/$unit" >/dev/null
  if command -v systemd-analyze >/dev/null; then sudo systemd-analyze verify "/etc/systemd/system/$unit" || echo "WARN: systemd-analyze reported issues for $unit (read them above)"; fi
}
step "systemd units"
[ "$LLM" = 0 ] || install_unit farsight-llm.service farsight-llm.service
[ "$WHISPER" = 0 ] || install_unit farsight-whisper.service farsight-whisper.service
[ "$LLM$WHISPER" != 00 ] || echo "assistant units skipped (need llama.cpp/whisper.cpp builds and models/Qwen*, ggml-base.en.bin; re-run after setup.sh --with-vlm)"
install_unit farsight.service farsight.service

step "Preflight of $ENV_FILE (missing models, certs or devices would start the server degraded)"
if [ "$DRY" = 1 ]; then echo "  [dry-run] $REPO/jetson/.venv/bin/python -m jetson.launch --check --env-file $ENV_FILE"
else (cd "$REPO" && jetson/.venv/bin/python -m jetson.launch --check --env-file "$ENV_FILE") || echo "WARN: fix the lines above in $ENV_FILE, then: sudo systemctl restart farsight"; fi

step "Start"
run sudo systemctl daemon-reload
[ "$LLM" = 0 ] || run sudo systemctl enable --now farsight-llm.service
[ "$WHISPER" = 0 ] || run sudo systemctl enable --now farsight-whisper.service
run sudo systemctl enable farsight.service
run sudo systemctl restart farsight.service

echo
echo "Enabled. Logs: journalctl -u farsight -f"
if [ -s "$TLS_DIR/cert.pem" ] && grep -q '^FARSIGHT_SSL_CERTFILE=.' "$ENV_FILE" 2>/dev/null; then
  echo "Probe:  $REPO/jetson/.venv/bin/python -m tools.ready --url https://farsight.local:8443 --ca-file $TLS_DIR/ca.pem"
else
  echo "Probe:  $REPO/jetson/.venv/bin/python -m tools.ready --url http://127.0.0.1:8443"
fi
