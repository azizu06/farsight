#!/usr/bin/env bash
# Install the opt-in tailnet forwarder unit (docs/phone-relay.md, "Direct operator path").
# Never touches farsight.service, the firewall or Tailscale settings. Safe to re-run.
# Usage: bash jetson/install_tailnet_forward.sh [--dry-run] [--uninstall]
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
RUN_USER="${SUDO_USER:-$USER}"
UNIT=farsight-tailnet-forward.service
DRY=0 REMOVE=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY=1 ;;
    --uninstall) REMOVE=1 ;;
    -h|--help) sed -n '2,4p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done
run() { if [ "$DRY" = 1 ]; then printf '  [dry-run] %s\n' "$*"; else "$@"; fi; }
if [ "$REMOVE" = 1 ]; then
  run sudo systemctl disable --now "$UNIT"
  run sudo rm -f "/etc/systemd/system/$UNIT"
  run sudo systemctl daemon-reload
  exit 0
fi
rendered="$(sed -e "s|@USER@|$RUN_USER|g" -e "s|@REPO@|$REPO|g" "$REPO/jetson/$UNIT")"
if [ "$DRY" = 1 ]; then printf '%s\n' "$rendered"; else printf '%s\n' "$rendered" | sudo tee "/etc/systemd/system/$UNIT" >/dev/null; fi
if [ "$DRY" = 0 ] && command -v systemd-analyze >/dev/null; then sudo systemd-analyze verify "/etc/systemd/system/$UNIT" || echo "WARN: systemd-analyze reported issues"; fi
run sudo systemctl daemon-reload
run sudo systemctl enable "$UNIT"
run sudo systemctl restart "$UNIT"
echo "Enabled $UNIT. Logs: journalctl -u farsight-tailnet-forward -f"
