# Native Mac benchmark — 2026-10-11

Host: Apple M5 Pro, 48 GB, Metal, processes niced to 19. Saved run `run-20261011T094259`; no remote pull. Shared background workloads were uncontrolled. Source revision `0b82491125396a9e405cb116b404f864b819736b` includes rasterizer reland `511f55d`. Compilation is outside the per-run timing.

The exact requested `splat/bench_mac.sh <saved-run>` command built successfully but found no frames: the archive root has a non-camera `manifest.json`, while the camera file is under `photos/transforms.json`. The unmodified script was rerun on a private flat dataset: the same 715 camera records and photo symlink, with a separate copy of `init.ply` and an explicit `ply_file_path`. The original seed remains unchanged.

The first stock 30-second arm failed with `calibration ran past the budget`; its bench aborts on that first failure, so it produced no stock 60/120/300 receipts. The script still exited zero because it suppresses arm errors. This is not a complete successful stock benchmark.

| Arm | Budget s | Prep s | Train s | Export s | Score s | Total s | Steps | Splats | Views | PSNR dB |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| turbo | 30 | 26.48 | 9.98 | 0.15 | 0.64 | 37.25 | 0 | 235307 | 40 | 12.00 |
| turbo | 60 | 35.30 | 23.35 | 0.13 | 0.56 | 59.33 | 476 | 237958 | 40 | 15.07 |
| turbo | 120 | 36.60 | 81.70 | 0.13 | 0.59 | 119.04 | 2126 | 242468 | 40 | 17.36 |
| turbo | 300 | 52.22 | 247.59 | 0.23 | 1.82 | 301.86 | 5383 | 250606 | 40 | 18.28 |

Prep is load + cull + prepared-data write + seed + calibration. Train includes dataset loading, kernel warmup and the training stream. Total includes export/scoring overhead. The 30-second turbo arm exhausted its budget before a training step was recorded; its score is an initialization result.

Culling kept 320/715 photos and changed the evaluation split to 40 views. These scores do not establish the requested 20.40 dB on the original 90 held-out views. Re-evaluating a culled model on the original 90 cameras would include some training views and would not fix that comparison.

The stock raw seeder rewrote its private source copy through the prepared seed hard link: 1,454,055 to 1,395,570 points. The subsequent turbo arms consumed that filtered cloud. Original and post-stock SHA256 values are in [bench.json](bench.json). The original archive seed is intact; this observation does not modify Sai's sources.

[turbo-bench.json](turbo-bench.json) retains the native per-run reports and per-view scores. [bench.json](bench.json) adds provenance and the stage table. Absolute local paths are replaced with placeholders; numbers are unchanged.

[kernels.json](kernels.json) is a separate untimed 200-step `--seed none` stock profile, 320 photos and 1,395,570 initial splats. Resolved GPU timestamps serialize launches, so its wall time is not a turbo benchmark. `ProjectBackwardsKernel` and `RasterizeBackwardsKernel` each completed 200 device-timed launches without the reported dominance panic. Project backward averaged 6.62 ms; rasterize backward averaged 1.59 ms.

The newer-head `f53fbfa99a8cbe3e24faede31190745bf39f8de7` stock 60-second confirmation was not run: its final link was preempted at 14:37:18Z when the live capture appeared, before Firstmate reiterated the stop at 14:37:45Z. The original-split full/prefix comparisons were prepared but never started. Therefore the profile proves the `511f55d` backward kernels ran on Metal at `0b82491`; it does not claim a newer-head stock 60-second pass. The demo watcher remains on the qualified stable Brush 5k/300k/SH3 plus CPU faces.
