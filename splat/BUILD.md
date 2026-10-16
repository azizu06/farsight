# splat/brush — build & headless train

Vendored Brush fork (Apache-2.0, upstream `1388f74`, see `UPSTREAM_COMMIT`), own cargo
workspace on burn/cubecl git main. NOT part of `rust/` — `cd splat/brush` first.

## Toolchain

- rustc/cargo 1.97.1+ (edition 2024). No rust-toolchain file; system toolchain is fine.
- Linux system deps for the headless trainer:
  `sudo apt-get install -y mesa-vulkan-drivers libvulkan1 vulkan-tools pkg-config libudev-dev`
  (`mesa-vulkan-drivers` = lavapipe, the CPU Vulkan ICD wgpu uses when no GPU).
- macOS: no system deps; wgpu picks Metal automatically. On the M5 Pro just build release.

## Build (headless trainer)

```
cd splat/brush
cargo build --release -p brush-cli        # ~6 min cold on a 16-core x86 VM
# binary: target/release/brush-cli
```

The GUI app is `-p brush-app` (default member) — skip it for headless work.

## Train headless

```
brush-cli <capture_dir> \
  --total-train-iters 5000 --export-every 5000 \
  --eval-every 5000 --eval-split-every 8 --eval-save-to-disk \
  --export-path <out_dir>/
```

- `<capture_dir>` = nerfstudio layout: `transforms.json` + `images/` (+ `init.ply` seed when
  `transforms.json` has `"ply_file_path": "init.ply"`). COLMAP dirs also work.
- CPU-only box: llvmpipe is found automatically; if wgpu grabs a wrong adapter run with
  `env -u DISPLAY` (vulkaninfo needs `env -u DISPLAY` too — it X_CreateWindow's otherwise).
  `error: XDG_RUNTIME_DIR not set` lines are harmless.
- `--eval-split-every 8` holds out every 8th frame for eval (Dechante's protocol);
  `--eval-save-to-disk` writes eval renders under `<export-path>` for PSNR scoring.

Verified on this VM (lavapipe, Mesa 23.2.1): 24-frame 160x120 synth capture + 3k-dot
`init.ply`, 200 steps -> eval ran, splats exported. First ~10-20 s of any run is cubecl
autotune (`Tuning ...` lines) — a fixed per-process cost that mostly reuses the on-disk
tune cache on reruns.

## farsight-splat (the pipeline CLI)

`crates/farsight-splat` builds `target/release/farsight-splat`:

```
farsight-splat synth <out_dir>                      # synthetic test capture
farsight-splat run   <capture_dir> --out <dir> --budget-s 60
farsight-splat bench <capture_dir> --budgets 30,60,120,300
```

One-command M5 Pro bench from repo root: `splat/bench_mac.sh <capture_dir>` — builds
release and runs the same budgets; no Python deps.
