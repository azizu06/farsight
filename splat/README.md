# farsight-splat (splat-turbo)

Goal: Dechante's photo-real room (`tools/postprocess`, Brush Gaussian-splat training seeded with rover dots) as fast and as
high quality as possible on an M5 Pro (48 GB). Baseline (RTX 4070, Brush v0.3.0, run 094259): 2k steps 81 s / 19.0 dB,
5k steps 174 s / 20.4 dB, 30k steps 1,325 s / 21.9 dB held-out PSNR.

`splat/brush/` is a vendored fork of [Brush](https://github.com/ArthurBrussee/brush) (Apache-2.0) at upstream commit
1388f74. All optimisation work lands here; the end-to-end Rust CLI is `farsight-splat` (see docs as they land).
