#!/usr/bin/env bash
# Sweep Brush v0.3.0 training configs and report steps/s, wall time and held-out PSNR.
#
#   tools/compiler/brush_sweep.sh <capture-dir> [config ...]
#
# <capture-dir> must be a nerfstudio capture (transforms.json + images/) with
# an init.ply referenced by transforms.json "ply_file_path" (our compiled cloud).
# Config names come from refine_presets.rs (fast | balanced | ultra) or "name=val"
# extra args may be appended after a literal "--" e.g.
#   tools/compiler/brush_sweep.sh cap fast balanced -- --sh-degree 0
#
# Env: BRUSH=/path/brush_app (required), OUT=/tmp/brush_sweep, KILL_S=0 to also
# stop each run after N seconds. Prints a table; per-run logs land in $OUT/<cfg>.log
set -euo pipefail
[ $# -ge 2 ] || { sed -n 2,14p "$0" | sed 's/^# \{0,1\}//'; exit 2; }
CAP=$(cd "$1" && pwd); shift
BRUSH=${BRUSH:?set BRUSH=/path/to/brush_app}
OUT=$(cd "$(dirname "${OUT:-/tmp/brush_sweep}")" && pwd -P || mkdir -p "${OUT:-/tmp/brush_sweep}" && cd "${OUT:-/tmp/brush_sweep}" && pwd)
mkdir -p "$OUT"
EXTRA=()
if [ "${1:-}" = "--" ]; then shift; EXTRA=("$@"); fi
declare -A ARGS=(
  [fast]="--total-steps 1000 --max-resolution 640 --sh-degree 1 --refine-every 200 --growth-stop-iter 400 --growth-select-fraction 0.05 --max-splats 300000 --subsample-points 2 --subsample-frames 1 --eval-split-every 8 --eval-every 250 --export-every 500 --lr-mean 2e-6 --lr-mean-end 5e-7 --lr-coeffs-dc 3e-3 --lr-opac 1e-2 --lr-scale 1e-2 --lr-scale-end 6e-3 --lr-rotation 1e-3 --ssim-weight 0.2 --aux-loss-time 0.8"
  [balanced]="--total-steps 3000 --max-resolution 800 --sh-degree 1 --refine-every 150 --growth-stop-iter 1200 --growth-select-fraction 0.1 --max-splats 350000 --subsample-points 1 --subsample-frames 1 --eval-split-every 8 --eval-every 500 --export-every 1000 --lr-mean 5e-6 --lr-mean-end 1e-6 --lr-coeffs-dc 2e-3 --lr-opac 1e-2 --lr-scale 1e-2 --lr-scale-end 6e-3 --lr-rotation 1e-3 --ssim-weight 0.2 --aux-loss-time 0.85"
  [ultra]="--total-steps 6000 --max-resolution 1280 --sh-degree 2 --refine-every 200 --growth-stop-iter 3000 --growth-select-fraction 0.1 --max-splats 500000 --subsample-points 1 --subsample-frames 1 --eval-split-every 8 --eval-every 1000 --export-every 1000 --lr-mean 1e-5 --lr-mean-end 1e-6 --lr-coeffs-dc 2e-3 --lr-opac 1e-2 --lr-scale 1e-2 --lr-scale-end 6e-3 --lr-rotation 1e-3 --ssim-weight 0.2 --aux-loss-time 0.9"
)
printf "%-10s %8s %8s %9s %10s\n" config wall_s steps steps_s psnr
for cfg in "$@"; do
  [ -n "${ARGS[$cfg]:-}" ] || { echo "unknown config $cfg"; continue; }
  log="$OUT/$cfg.log"; mkdir -p "$OUT/$cfg"
  t0=$(date +%s%N)
  timeout "${KILL_S:-0}" "$BRUSH" "$CAP" ${ARGS[$cfg]} "${EXTRA[@]}" --export-path "$OUT/$cfg" >"$log" 2>&1 || true
  wall=$(( ($(date +%s%N) - t0) / 1000000000 ))
  steps=$(grep -oE "Eval iter [0-9]+" "$log" | tail -1 | grep -oE "[0-9]+" || echo 0)
  steps=${steps:-0}; [ "$steps" = 0 ] && steps=$(grep -oE "total-steps [0-9]+" <<<"${ARGS[$cfg]}" | grep -oE "[0-9]+$")
  psnr=$(grep -oE "PSNR [0-9.]+" "$log" | tail -1 | grep -oE "[0-9.]+" || echo -)
  sps=$([ "$wall" -gt 0 ] && echo "$steps $wall" | awk '{printf "%.1f", $1/$2}' || echo -)
  printf "%-10s %8s %8s %9s %10s\n" "$cfg" "$wall" "$steps" "$sps" "${psnr:--}"
done
