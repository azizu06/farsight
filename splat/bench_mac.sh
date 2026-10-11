#!/usr/bin/env bash
# One-command M5 Pro bench of the whole splat-turbo stack: builds
# farsight-splat (+ farsight-seed for the TURBO arm) and runs `bench`
# twice per budget — STOCK (Dechante's method at a time budget) vs TURBO
# (our cull/seed/schedule/refine/appearance stack). Prints one combined
# receipt table and writes $OUT/bench.json. No Python.
#
#   splat/bench_mac.sh <capture_dir> [budget_csv] [out_dir]
#
# Env knobs: SSIM_TILES (one-time tile micro-bench), NO_PROFILE=1 (skip the
# untimed kernel-profile run), SPLAT_OUT, BUDGETS via $2.
set -euo pipefail

CAPTURE="${1:?usage: bench_mac.sh <capture_dir> [budget_csv] [out_dir]}"
BUDGETS="${2:-30,60,120,300}"
ROOT="$(cd "$(dirname "$0")" && pwd)"
BRUSH="$ROOT/brush"
OUT="${3:-${SPLAT_OUT:-$ROOT/out/mac_$(date +%s)}}"
mkdir -p "$OUT"

FEATURES=()
if [[ "$(uname -m)" == "arm64" && "$(uname -s)" == "Darwin" ]]; then
    # MSL-direct cubecl kernels (Apple7+, Metal >= 3.2). The feature lives
    # on brush-process -> brush-render -> brush-cube; auto-off elsewhere
    # (wgpu/WGSL fallback is the default build).
    FEATURES=(--features brush-process/metal-msl)
fi

cd "$BRUSH"
export CARGO_PROFILE_RELEASE_LTO=fat
cargo build --release -p farsight-splat -p farsight-seed "${FEATURES[@]}"
FS="$PWD/target/release/farsight-splat"
# farsight-splat resolves the farsight-seed binary next to itself (or
# $FARSIGHT_SEED); both land in target/release from the same build.
export FARSIGHT_SEED="$PWD/target/release/farsight-seed"

# cubecl autotune + wgpu pipeline caches persist across runs (fixed dir).
export CUBECL_CACHE_DIR="${CUBECL_CACHE_DIR:-$HOME/.cache/farsight-splat/cubecl}"
export XDG_CACHE_HOME="${XDG_CACHE_HOME:-$HOME/.cache}"
mkdir -p "$CUBECL_CACHE_DIR"

# SSIM_TILES="ssim-fwd-32x8,ssim-bwd-8x4" rebuilds brush-loss with tile
# variants and runs the ssim_bench micro-timing first (one-time; pick the
# fastest variant for this frame size before the budget runs).
if [[ -n "${SSIM_TILES:-}" ]]; then
    cargo test --release -p brush-loss --features "$SSIM_TILES" ssim_bench \
        -- --ignored --nocapture | tee "$OUT/ssim_bench.log"
fi

COMMON=(--eval-split-every 8 --warmup)

run_arm() { # <label> <out_dir> <extra flags...>
    local label="$1" out="$2"; shift 2
    "$FS" bench "$CAPTURE" --out "$out" --budgets "$BUDGETS" \
        "${COMMON[@]}" "$@" 2>&1 | tee "$out/bench.log" || true
}

echo "=== arm STOCK (raw seed, stock schedule, refine brush) ==="
run_arm stock "$OUT/stock" \
    --seed raw --schedule stock --refine-mode brush --calib-steps 64

echo "=== arm TURBO (farsight seed, budget schedule, quality bundle) ==="
# T4's cap keeps the 60s step rate up; 300k matched the Mac's ~77 steps/s
# pocket on the 290k-seed stock run. Tune with TURBO_EXTRA=...
run_arm turbo "$OUT/turbo" \
    --seed farsight --schedule budget --quality \
    --refine-mode budget --growth-target-splats ${GROWTH_TARGET:-300000} \
    --appearance-lr 5e-3 --appearance-warmup-frac 0.15 --appearance-reg 1e-3 \
    ${TURBO_EXTRA:-}

# Combined receipt table: arm, budget, steps, splats, Prep/Train/Export/Score/Total, PSNR, SSIM.
printf '%-7s %7s %7s %9s %7s %7s %7s %7s %7s %8s %6s\n' \
    arm budget steps splats prep_s train_s export_s score_s total_s psnr ssim
emit() { # <arm> <run.json>
    local arm="$1" f="$2"
    [[ -d "$f" ]] || return 0
    # bench writes one run.json per budget dir under <out>/b<N>/run.json
    for j in "$f"/b*/run.json; do
        [[ -f "$j" ]] || continue
        grep -o '.*' "$j" > /dev/null 2>&1 || continue
        awk -v arm="$arm" '
            /"budget_s"/      { if (match($0,/[0-9]+(\.[0-9]+)?/)) b=substr($0,RSTART,RLENGTH) }
            /"iters_done"/    { match($0,/[0-9]+/); s=substr($0,RSTART,RLENGTH) }
            /"splats"/        { match($0,/[0-9]+/); n=substr($0,RSTART,RLENGTH) }
            /"psnr_mean"/     { match($0,/[0-9]+(\.[0-9]+)?/); p=substr($0,RSTART,RLENGTH) }
            /"ssim_mean"/     { match($0,/[0-9]+(\.[0-9]+)?/); q=substr($0,RSTART,RLENGTH) }
            /"load_ms"/       { match($0,/[0-9]+/); v=substr($0,RSTART,RLENGTH); pr+=v }
            /"cull_ms"/       { match($0,/[0-9]+/); v=substr($0,RSTART,RLENGTH); pr+=v }
            /"write_ms"/      { match($0,/[0-9]+/); v=substr($0,RSTART,RLENGTH); pr+=v }
            /"seed_ms"/       { match($0,/[0-9]+/); v=substr($0,RSTART,RLENGTH); pr+=v }
            /"calib_ms"/      { match($0,/[0-9]+/); v=substr($0,RSTART,RLENGTH); pr+=v }
            /"train_ms"/      { match($0,/[0-9]+/); tr=substr($0,RSTART,RLENGTH) }
            /"export_ms"/     { match($0,/[0-9]+/); ex=substr($0,RSTART,RLENGTH) }
            /"score_ms"/      { match($0,/[0-9]+/); sc=substr($0,RSTART,RLENGTH) }
            /"wall_s"/        { match($0,/[0-9]+(\.[0-9]+)?/); w=substr($0,RSTART,RLENGTH) }
            END { printf "%-7s %7s %7s %9s %7.1f %7.1f %7.1f %7.1f %7.1f %8s %6s\n",
                  arm, b, s, n, pr/1000, tr/1000, ex/1000, sc/1000, w, p?p:"-", q?q:"-" }
        ' "$j"
    done
}
emit stock "$OUT/stock"
emit turbo "$OUT/turbo"

# Kernel table: a short extra run with --profile dumps per-kernel device
# times to JSON (Resolved timing serializes launches — run it AFTER the
# budget runs, never timed). Skip with NO_PROFILE=1.
if [[ -z "${NO_PROFILE:-}" ]]; then
    echo "=== kernel profile (untimed): $OUT/kernels.json ==="
    "$FS" run "$CAPTURE" --out "$OUT/profile" \
        --steps 200 --seed none --eval-split-every 8 \
        --eval-every 100000 --export-every 100000 \
        --profile "$OUT/kernels.json" || true
fi
echo "results: $OUT (stock/turbo bench.json + run.json per budget)"
