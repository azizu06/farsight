#!/usr/bin/env bash
# Copy models/ (not in git) from a laptop to the Jetson. Safe to re-run: rsync only sends what changed.
# Usage: tools/push_models.sh [--fetch] [--groups piper,face,whisper,vlm] [--dry-run] [user@host]
#   default target: $USER@farsight.local     (USB-C to the Mac: user@192.168.55.1)
#   --fetch  first download what is missing on this laptop (Piper English + Spanish voices, face model,
#            whisper base.en; add --groups vlm for the 2.8 GB Qwen2.5-VL). Without it, only a report.
#   --dry-run shows what would be downloaded and sent; nothing changes.
# Engines (*.engine) are never pushed: they only run on the TensorRT build that made them (jetson/setup.sh builds them).
set -euo pipefail
cd "$(dirname "$0")/.."

FETCH=0 DRY=0 GROUPS_ARG="" TARGET=""
while [ $# -gt 0 ]; do
  case "$1" in
    --fetch) FETCH=1 ;;
    --dry-run) DRY=1 ;;
    --groups) GROUPS_ARG="${2:?--groups needs a comma list}"; shift ;;
    -h|--help) sed -n '2,9p' "$0"; exit 0 ;;
    -*) echo "unknown option: $1 (see --help)" >&2; exit 2 ;;
    *) TARGET="$1" ;;
  esac
  shift
done
TARGET="${TARGET:-$USER@farsight.local}"
REMOTE_DIR="${FARSIGHT_REMOTE_DIR:-farsight}"
PYTHON="${PYTHON:-python3}"

groups=(--groups "${GROUPS_ARG:-piper,face,whisper}")
echo "== Models on this laptop"
if [ "$FETCH" = 1 ]; then
  fetch_flags=(); [ "$DRY" = 0 ] || fetch_flags=(--dry-run)
  "$PYTHON" -m tools.fetch_models "${groups[@]}" ${fetch_flags[@]+"${fetch_flags[@]}"} || echo "WARN: some downloads failed; pushing what exists"
else
  "$PYTHON" -m tools.fetch_models "${groups[@]}" --check || echo "(run again with --fetch to download the missing ones)"
fi
"$PYTHON" -m tools.fetch_models --groups export --check || echo "WARN: export the YOLO ONNX files on this laptop before the Jetson can build engines"

echo "== Push to $TARGET:~/$REMOTE_DIR/models/"
rsync_flags=(-ah --partial --exclude='*.engine' --exclude='*.engine.json' --exclude='.DS_Store' --exclude='*.part')
if rsync --info=help >/dev/null 2>&1; then rsync_flags+=(--info=progress2); else rsync_flags+=(--progress); fi  # macOS openrsync lacks --info
[ "$DRY" = 0 ] || rsync_flags+=(--dry-run)
if [ "$DRY" = 0 ]; then
  ssh -o ConnectTimeout=8 "$TARGET" "mkdir -p ~/$REMOTE_DIR/models"
fi
rsync "${rsync_flags[@]}" models/ "$TARGET:~/$REMOTE_DIR/models/"
echo "Done. On the Jetson: bash ~/$REMOTE_DIR/jetson/setup.sh builds the engines from these ONNX files."
