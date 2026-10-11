# Gaussian splat on the Mac

The capture → splat → Quest walkthrough path (the RIIS workflow) trains on the MacBook GPU. It uses [Brush](https://github.com/ArthurBrussee/brush) v0.3.0 (Rust, wgpu on Metal; no CUDA). The release binary is pinned by SHA-256 and downloaded on first use to `~/.cache/farsight-splat`.

```sh
tools/splat/run.sh <capture-dir> <out-dir>
```

Outputs in `<out-dir>`:

| File | Contents |
|---|---|
| `splat.ply` | Pruned for Quest Browser: at most 300k splats, SH degree 1 by default (92 bytes per splat, so 300k ≈ 28 MB) |
| `raw.ply` | The newest Brush export inside the time budget |
| `exports/` | Every periodic Brush export |
| `brush.log` | Brush output |
| `timings.json` | Train and prune times, the exported step, splat counts and bytes |

Knobs (environment variables):

| Variable | Default | Meaning |
|---|---|---|
| `SPLAT_BUDGET_S` | 840 | Wall-clock training budget. Brush is stopped there and the newest export is kept. |
| `SPLAT_STEPS` | 30000 | Brush `--total-steps`. Training ends at whichever comes first, this or the budget. |
| `SPLAT_EXPORT_EVERY` | 500 | Steps between exports |
| `SPLAT_MAX_SPLATS` | 300000 | Brush growth cap and final prune cap |
| `SPLAT_SH_DEGREE` | 1 | SH degree kept in `splat.ply`: 0 is smallest (68 bytes/splat), 3 is full (248 bytes/splat) |
| `SPLAT_MAX_RES` | 1600 | Brush `--max-resolution`, the longest image side used for training |
| `BRUSH` | pinned download | Path to another `brush_app` binary |

`tools/splat/prune.py` can also be run on its own:

```sh
python -m tools.splat.prune in.ply out.ply --max-splats 300000 --sh-degree 1
```

It keeps the splats with the highest opacity × volume, trims SH coefficients per channel, and writes the canonical 3DGS property order.

## Capture format

Brush reads posed images in two formats:
- **nerfstudio:** `transforms.json` with `file_path` relative to the capture dir.
- **COLMAP:** a `sparse/` model plus `images/`.

For the ARKit capture, write `transforms.json` like this:
- **Intrinsics:** `fl_x`, `fl_y`, `cx`, `cy`, `w`, `h` at the saved image resolution.
- **Poses:** one `transform_matrix` per frame, camera-to-world.
- **Axes:** ARKit's camera frame (+X right, +Y up, looking down −Z) is the nerfstudio/OpenGL convention, so `frame.camera.transform` goes in as-is (row-major 4×4).
- **Initial points (optional):** an `init.ply` of measured LiDAR points in the same world frame. Brush seeds splats from it instead of a random start, which helps short training budgets.

If the ARKit poses are bad:
- **COLMAP on the images alone:** `brew install colmap`, then `colmap automatic_reconstructor --workspace_path <dir> --image_path <dir>/images`, and point `run.sh` at that dir.
- **MASt3R:** another images-only fallback.
- **OpenSplat:** an alternative trainer with an MPS backend, if Brush itself misbehaves.

## Measured (MacBook, Apple Silicon)

See the PR description for the validation run on the instant-ngp `fox` sample: 50 real 1080×1920 photos with known poses in `transforms.json`, the same format as the ARKit capture.
