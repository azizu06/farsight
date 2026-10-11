# Post-run 3D room: Dechante's pipeline (2026-10-11)

How the walkable 3D rooms were made from a rover run. Training ran on an RTX 4070 PC with
[Brush](https://github.com/ArthurBrussee/brush) v0.3.0 (same binary as `tools/splat/run.sh`); everything
else ran on a Mac. Nothing here runs on the Jetson or changes the rover.

## Steps

1. **Pull the run** from the Jetson capture archive (#192): `~/captures/<run-id>/` = `images/`, `transforms.json`
   (+ `init.ply` once the run saver drops it there).
2. **Pick one continuous drive**, not the whole capture (parked stretches add duplicate views and person ghosts):
   `python find_drives.py RUN_DIR` then `python find_drives.py RUN_DIR --cut A B OUT_DIR`.
   Run `094259` was one continuous 6 min / 55.8 m drive, so it was used whole.
3. **Seed with LiDAR dots** (`init.ply` + `"ply_file_path": "init.ply"` in `transforms.json`). Either the run
   saver's `init.ply`, or `python seed_from_dots.py OUT_DIR points.bin [dense.xyzrgb]` from the Jetson map.
4. **Drop frames Brush rejects** (`IO error ... early eof` on a JPEG OpenCV still opens; run `094259` frame 608):
   bisect by trimming `frames`. Re-encoding alone did not fix that frame.
5. **Train** on the PC: `train_job.ps1 <dataset> <tag> "<extra Brush args>"` (30k steps, export every 5k as
   checkpoints, `--eval-split-every 8 --eval-save-to-disk`). Launch it with `Invoke-WmiMethod Win32_Process Create`
   so it survives the SSH session closing.
6. **Score** against held-out photos: `python score_heldout.py IMAGES_DIR out-*/eval_30000`.
7. **Walk it**: serve a folder with `walk.html`, the `.ply` and `transforms.json`, open
   `walk.html?scene=room.ply&transforms=transforms.json` (WASD, drag, Q/E, ride-along, minimap).
   For the Quest headset use `/splat.html?scene=...` (stick walking); prune first with
   `python -m tools.splat.prune in.ply out.ply --max-splats 300000 --sh-degree 1`.

## Measured results

| Room | Input | Seed | Train time (4070) | Held-out PSNR |
|---|---|---|---|---|
| 072139 drive | 159 frames, 76 s, 26 m | none | 643 s | 17.9 dB (20 views) |
| 072139 drive | same | 59,496 dots | 655 s | **19.3 dB**, better on 15/20 |
| 094259 run | 714 frames, 6 min, 55.8 m | 1,454,055 dots (run saver) | 1,325 s | — |
| 072139 full capture | 1,904 frames | none | 328 s | (no held-out split) |

Same 1,904-frame run on an M2 8 GB Mac: ~37 min. Aziz's M5 Pro: ~40–45 steps/s (live refresh 800 steps in 18–33 s).

## Other tools tried

- `dots_viewer.html` + `poisson.py` + `surface.mjs`: dots → solid surface (Open3D Poisson, or the team's #201
  measured-surface builder). Real shape from any angle, but lumpy; not photo-real.
- `render_from_camera.html`: draw dots/surfaces from a photo's exact camera to compare with the real photo.
- `ply2splat.py`: Brush `.ply` → antimatter15 `.splat` (32 B/splat) for web viewers.
- `clean_splat.py`: deleting floaters/needles after training. **Made it worse** (the chairs are built from those).
- `cull.py`: keep the sharpest frame per parked stretch. Superseded by one continuous drive.
- `refine.py`: AI image cleanup (Nano Banana / gpt-image) of renders. **Not for judge-facing output**: gpt-image-2
  invented a room where the splat had no data.
- 400k extra "simulated" dots sampled on the Poisson surface: ~3× slower and near 11.4/12.3 GB VRAM; stopped.

## Fast mode: train fewer steps (measured 2026-10-11, run 094259)

Same room, same 1,454,055-dot seed, same 90 held-out photos, separate full runs at each step budget
(not checkpoints of the 30k run, so each one finishes its own learning-rate schedule). RTX 4070.

| Steps | Train time | Held-out PSNR | vs 30k |
|---|---|---|---|
| 2,000 | **81 s** | 19.0 dB | 16× faster, −2.9 dB |
| 5,000 | **174 s** | **20.4 dB** | 7.6× faster, −1.5 dB |
| 10,000 | pending | pending | |
| 30,000 | 1,325 s | 21.9 dB | baseline |

Sai's `farsight-compiler` (dots + painted photos, no training) on the same run and photos: **12.3 dB**, 61 s on an
M2 8 GB CPU. So even a 2k-step seeded splat beats the untrained textured mesh by ~6.7 dB.

Run a fast room: `train_job.ps1 <dataset> fast "--total-steps 5000"` (later args override the 30k default).
