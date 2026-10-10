#!/usr/bin/env bash
# Install the Farsight systemd unit on the Jetson. Safe to re-run; never overwrites /etc/farsight.env.
# Usage (on the Jetson, after jetson/setup.sh): bash jetson/install_service.sh
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
RUN_USER="${SUDO_USER:-$USER}"
[ -x "$REPO/jetson/.venv/bin/python" ] || { echo "Run jetson/setup.sh first: no venv at $REPO/jetson/.venv"; exit 1; }

if [ ! -f /etc/farsight.env ]; then
  sudo install -m 644 "$REPO/jetson/farsight.env.example" /etc/farsight.env
  sudo sed -i "s#/home/farsight/farsight#$REPO#" /etc/farsight.env
  echo "Wrote /etc/farsight.env: edit camera, serial and TLS paths, then: sudo systemctl restart farsight"
fi
sudo mkdir -p /var/lib/farsight/missions && sudo chown -R "$RUN_USER" /var/lib/farsight

sed -e "s#@USER@#$RUN_USER#" -e "s#@REPO@#$REPO#g" "$REPO/jetson/farsight.service" \
  | sudo tee /etc/systemd/system/farsight.service >/dev/null
command -v systemd-analyze >/dev/null && sudo systemd-analyze verify /etc/systemd/system/farsight.service
sudo systemctl daemon-reload
sudo systemctl enable --now farsight
echo "Enabled. Logs: journalctl -u farsight -f   Probe: python -m tools.ready --url https://farsight.local:8443 --insecure"
