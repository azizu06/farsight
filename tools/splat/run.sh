#!/usr/bin/env bash
# Train a Gaussian splat on this Mac (Metal via Brush/wgpu) and export a Quest-sized .ply.
#
#   tools/splat/run.sh <capture-dir> <out-dir>
#
# <capture-dir> holds posed images Brush can read: a nerfstudio transforms.json (ARKit poses,
# file_path relative to the dir) or a COLMAP sparse/ model. An init.ply there (e.g. LiDAR points)
# seeds the splats. Outputs in <out-dir>: splat.ply (pruned for Quest Browser), raw.ply (last
# Brush export), brush.log and timings.json.
#
# Environment (all optional):
#   SPLAT_BUDGET_S=840     wall-clock training budget; the newest export inside it is used
#   SPLAT_STEPS=30000      Brush --total-steps (training stops early at the budget)
#   SPLAT_EXPORT_EVERY=500 steps between exports
#   SPLAT_MAX_SPLATS=300000  Brush growth cap and final prune cap
#   SPLAT_SH_DEGREE=1      SH degree kept in splat.ply (0 smallest; 3 = full)
#   SPLAT_MAX_RES=1600     Brush --max-resolution (longest image side)
#   BRUSH=/path/brush_app  use this binary instead of the pinned download
set -euo pipefail

if [ $# -ne 2 ]; then
  sed -n 2,20p "$0" | sed 's/^# \{0,1\}//'
  exit 2
fi
CAPTURE=$(cd "$1" && pwd)
mkdir -p "$2"
OUT=$(cd "$2" && pwd)
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
PYTHON=${PYTHON:-python3}

BUDGET=${SPLAT_BUDGET_S:-840}
STEPS=${SPLAT_STEPS:-30000}
EVERY=${SPLAT_EXPORT_EVERY:-500}
MAX_SPLATS=${SPLAT_MAX_SPLATS:-300000}
SH=${SPLAT_SH_DEGREE:-1}
MAX_RES=${SPLAT_MAX_RES:-1600}

if [ ! -f "$CAPTURE/transforms.json" ] && [ ! -d "$CAPTURE/sparse" ]; then
  echo "No transforms.json or COLMAP sparse/ in $CAPTURE" >&2
  exit 1
fi

# Pinned Brush release (Metal through wgpu); downloaded once, verified by SHA-256.
CACHE=${XDG_CACHE_HOME:-$HOME/.cache}/farsight-splat
VERSION=v0.3.0
ARCHIVE=brush-app-aarch64-apple-darwin.tar.xz
SHA256=65b2631398c839be3c1d4d7160fe2326389dec87830aac0710985e6690a1048c
if [ -z "${BRUSH:-}" ]; then
  BRUSH=$CACHE/brush-$VERSION/brush-app-aarch64-apple-darwin/brush_app
  if [ ! -x "$BRUSH" ]; then
    [ "$(uname -sm)" = "Darwin arm64" ] || { echo "Pinned Brush is macOS arm64 only; set BRUSH=" >&2; exit 1; }
    mkdir -p "$CACHE/brush-$VERSION"
    curl -fsSL -o "$CACHE/brush-$VERSION/$ARCHIVE" \
      "https://github.com/ArthurBrussee/brush/releases/download/$VERSION/$ARCHIVE"
    echo "$SHA256  $CACHE/brush-$VERSION/$ARCHIVE" | shasum -a 256 -c - >/dev/null
    tar -xJf "$CACHE/brush-$VERSION/$ARCHIVE" -C "$CACHE/brush-$VERSION"
    xattr -d com.apple.quarantine "$BRUSH" 2>/dev/null || true
  fi
fi

# Brush writes its kernel autotune cache to ./target; keep it shared across runs.
WORK=$CACHE/work
mkdir -p "$WORK" "$OUT/exports"
rm -f "$OUT"/exports/*.ply

started=$(date +%s)
(cd "$WORK" && exec "$BRUSH" "$CAPTURE" --total-steps "$STEPS" --export-every "$EVERY" \
  --export-path "$OUT/exports" --max-splats "$MAX_SPLATS" --max-resolution "$MAX_RES") \
  >"$OUT/brush.log" 2>&1 &
pid=$!
stopped=finished
while kill -0 "$pid" 2>/dev/null; do
  if [ $(( $(date +%s) - started )) -ge "$BUDGET" ]; then
    stopped=budget
    kill "$pid" 2>/dev/null || true
    break
  fi
  sleep 2
done
wait "$pid" 2>/dev/null || true
trained=$(( $(date +%s) - started ))

latest=$(ls "$OUT"/exports/*.ply 2>/dev/null | sed -E 's/.*_([0-9]+)\.ply$/\1 &/' | sort -n | tail -1 | cut -d' ' -f2-)
if [ -z "$latest" ]; then
  echo "Brush produced no export within ${trained}s; see $OUT/brush.log" >&2
  exit 1
fi
cp "$latest" "$OUT/raw.ply"
step=$((10#$(basename "$latest" | sed -E 's/.*_([0-9]+)\.ply$/\1/')))  # Brush zero-pads step numbers

prune_started=$(date +%s)
summary=$(cd "$ROOT" && "$PYTHON" -m tools.splat.prune "$OUT/raw.ply" "$OUT/splat.ply" \
  --max-splats "$MAX_SPLATS" --sh-degree "$SH")
pruned=$(( $(date +%s) - prune_started ))

cat >"$OUT/timings.json" <<EOF
{"capture": "$CAPTURE", "brush": "$VERSION", "stopped": "$stopped", "train_s": $trained,
 "steps_exported": $step, "prune_s": $pruned, "budget_s": $BUDGET, "max_resolution": $MAX_RES,
 "prune": $summary, "splat_ply_bytes": $(stat -f %z "$OUT/splat.ply")}
EOF
cat "$OUT/timings.json"
