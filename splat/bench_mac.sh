#!/usr/bin/env bash
# One-command M5 Pro bench: build brush-cli (release) and train the capture at
# fixed wall-clock budgets. Usage: splat/bench_mac.sh <capture_dir> [budgets]
# No Python deps. Runs the same flags as the RTX-4070 baseline (eval split 8,
# PSNR/SSIM on held-out views, .ply + .splat export).
set -euo pipefail

CAPTURE="${1:?usage: bench_mac.sh <capture_dir> [budget_csv]}"
BUDGETS="${2:-30,60,120,300}"
ROOT="$(cd "$(dirname "$0")" && pwd)"
BRUSH="$ROOT/brush"
OUT="${SPLAT_OUT:-$ROOT/out/mac_$(date +%s)}"
mkdir -p "$OUT"

FEATURES=()
if [[ "$(uname -m)" == "arm64" && "$(uname -s)" == "Darwin" ]]; then
    # MSL-direct cubecl kernels (Apple7+, Metal >= 3.2). Auto-off elsewhere:
    # the wgpu/WGSL fallback path is the default build.
    FEATURES=(--features brush-cli/metal-msl)
fi

cd "$BRUSH"
export CARGO_PROFILE_RELEASE_LTO=fat
cargo build --release -p brush-cli "${FEATURES[@]}"
CLI=target/release/brush-cli

# cubecl autotune + wgpu pipeline caches persist across runs (fixed dir).
export CUBECL_CACHE_DIR="${CUBECL_CACHE_DIR:-$HOME/.cache/farsight-splat/cubecl}"
export XDG_CACHE_HOME="${XDG_CACHE_HOME:-$HOME/.cache}"
mkdir -p "$CUBECL_CACHE_DIR"

IFS=',' read -ra BS <<< "$BUDGETS"
for B in "${BS[@]}"; do
    echo "=== budget ${B}s: $CAPTURE -> $OUT/b$B ==="
    "$CLI" "$CAPTURE" \
        --total-train-iters 100000 --budget-s "$B" \
        --warmup \
        --eval-every 100000 --eval-split-every 8 --eval-save-to-disk \
        --export-every 100000 \
        --export-path "$OUT/b$B/" || true
done
echo "results: $OUT"
