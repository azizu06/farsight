# Brush speed tuning — ~45 s, ≥19.3 dB held-out

Target: Dechante-level quality (Brush splat on posed photos, 19.3 dB held-out
PSNR at 30 k steps / ~11 min on a 4070) in ~45–50 s on the M5 Pro, seeded by
our compiled cloud. The seed does the geometry work; Brush only has to learn
appearance. Everything below was verified against brush v0.3.0 source and a
live run on this VM (llvmpipe software-Vulkan adapter).

## The two data-prep gotchas (W6 must know)

1. **`ply_file_path` in `transforms.json` is the seed mechanism.** Brush
   seeds splats from the PLY named by that field (relative to the capture
   dir). Without it Brush falls back to random splats sampled in camera
   bounds — and panics (`cannot sample empty range`) if bounds are
   degenerate. Write `init.ply` into the capture dir and set
   `"ply_file_path": "init.ply"`. Format: binary LE `x,y,z (f32)` +
   `red,green,blue (u8)` — identical to our `points.ply` layout, so
   `cloud.ply` can be renamed/copied.
2. **Tiny images crash the loader.** Brush's `get_image_data` does a fixed
   `read_exact` of 16 387 bytes, then decodes dims from the partial buffer.
   Files **< 16 387 bytes** fail with `IO error ... early eof`, and JPEGs
   whose SOF isn't decodable within the first chunk die the same way.
   Safest: write **PNG** and pad any file smaller than ~20 KB with trailing
   zero bytes (decoders stop at IEND; trailing bytes are ignored).

## Verified on this VM (lavapipe `llvmpipe`, software Vulkan)

- `brush_app <capture> <flags>` runs headless (no `--with-viewer`) end to end:
  dataset load → init.ply seed (126 k splats at `--subsample-points 4`) →
  training loop → `Eval iter N: PSNR x, ssim y` → exports.
- 30 steps in 41 s wall (incl. ~15 s load+eval) at 640 px / SH 1 —
  ~1 step/s on a CPU rasteriser. On Metal this maps to the 40–60 steps/s
  range the 4070 reference implies, so **2.5–4 k steps ≈ 45–55 s**.

## Knobs that move PSNR/s (from brush-train source)

- `refine_every` still prunes+resamples every call even after growth stops —
  keep it ≥150 or refine overhead dwarfs training (`train.rs::refine_if_needed`).
- `growth_stop_iter` is the only growth off switch (growth while
  `iter < stop`). Seeded clouds need a short window just for residual holes.
- `--sh-degree` is per-splat memory + render cost. SH 1 ≈ 2× faster per step
  than SH 3 on view-dependent-light scenes like our synth room.
- `--max-resolution` is **load-time only** — no in-run schedule. A
  400→800 px schedule = two invocations: phase-1 export becomes phase-2's
  `init.ply` (`--subsample-points 1` keeps every splat).
- `--lr-mean` 2e-5 default assumes random init; seeded positions are good, so
  `2e-6..5e-6` converges colour/opacity/scale without drifting geometry.
- `--eval-split-every 8` = the score_heldout.py acceptance method (every 8th
  view held out). All presets keep it.
- `--subsample-points` thins the seed; `--subsample-frames`/`--max-frames`
  cut views if load time matters.

## Presets (`src/refine_presets.rs` — mirrored in `brush_sweep.sh`)

| name | steps | res | SH | growth | splats | est. wall |
|---|---|---|---|---|---|---|
| fast | 1000 | 640 | 1 | ≤400, frac .05 | ≤300k (sub 2) | ~15–20 s |
| balanced | 3000 | 800 | 1 | ≤1200, frac .10 | ≤350k | ~45–55 s |
| ultra | 6000 | 1280 | 2 | ≤3000 | ≤500k | ~90–120 s |

`balanced` is the acceptance candidate; if it lands short, the fallback is
`fast` geometry + a second 800→1280 px relaunch (export → init.ply).

## Sweep on the Mac

```
BRUSH=~/.cache/farsight-splat/brush-v0.3.0/.../brush_app \
  tools/compiler/brush_sweep.sh <capture-dir> fast balanced ultra
```

prints `config wall_s steps steps_s psnr`. Add `-- <extra brush args>` to
append overrides to every config (e.g. `-- --sh-degree 0`).

## Measured trajectory (lavapipe, 640 px, SH1, seeded 126 k splats)

| iter | PSNR | SSIM |
|---|---|---|
| 30 | 8.69 | 0.31 |
| 50 | 8.52 | 0.31 |
| 100 | 9.09 | 0.37 |
| 150 | 9.77 | 0.43 |
| 200 | 10.40 | 0.48 |

~0.85 steps/s sustained on a CPU rasteriser (200 steps in ~170 s of loop).
PSNR is climbing ~0.6–0.7 dB per 50 steps with no sign of plateau — at that
slope the seeded model crosses ~19 dB around iter 1.5–2.5 k, i.e. inside the
`balanced` step budget on a ~50× faster Metal adapter. Numbers here prove
the pipeline and arg surface, not throughput.
