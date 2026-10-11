# Post-run phone capture

The RIIS-style path captures a run, reconstructs a Gaussian splat afterwards,
then walks the result in Quest. The capture fallback lives in
`jetson/mapping/run_capture.py`; reconstruction and Quest splat loading are
separate steps. It does not require the live dense renderer or raw-point capture.

## Record on the Jetson

`FARSIGHT_RUN_CAPTURE=1` enables the archive. When unset/blank, physical
`--demo-mode` enables it by default; `FARSIGHT_RUN_CAPTURE=0` disables it.
Synthetic `--demo`, simulator and replay sources are excluded.
`FARSIGHT_RUN_CAPTURE_DIR` overrides the default `~/captures` directory (the
server service user's home).

Stream the iPhone's RGB-D mode (`/phone`). The server samples accepted frames at
up to 2.5 Hz and copies the original transmitted JPEG plus its matching ARKit
pose and intrinsics. The current RGB-only `/phone/rgb` protocol lacks pose and
intrinsics and cannot produce this dataset. The current streamed RGB-D JPEG is
resized on iOS (normally 640 pixels wide); full-resolution RGB recorded locally
on the phone with synchronized pose/intrinsics is the preferred training input.
This fallback does not restore missing image detail.

Each connection/session/epoch gets a new UTC-named run:

```text
~/captures/<run-id>/
  images/000000.jpg
  images/000001.jpg
  transforms.json
```

`transforms.json` follows the [nerfstudio camera format](https://docs.nerf.studio/quickstart/data_conventions.html): `camera_model=OPENCV`,
zero distortion coefficients, and a `frames` array with relative `file_path`,
`fl_x`, `fl_y`, `cx`, `cy`, `w`, `h` per image. Its `transform_matrix` is a
row-major nested 4×4 camera-to-world matrix in metres. ARKit already uses
OpenGL camera axes (+X right, +Y up, -Z forward), so no camera-axis or viewer
rotation is applied. Intrinsics describe the native saved JPEG, not the smaller
depth map. `frame_id`, `t_capture` (ARKit time), and `tracking` remain attached
for downstream selection; frames with limited tracking can be filtered during
reconstruction. The dataset retains ARKit's Y-up world coordinates.

The observer runs outside `state.lock`. An eight-item nonblocking queue holds
JPEG bytes and small metadata, never depth/RGB arrays. A dedicated thread writes
images and atomically replaces the JSON after each image. Slow/full disks drop
samples rather than waiting in ingest, health or control. `/health.run_capture`
reports `written`, `queued`, `dropped`, `failed`, `error`, and the current `run`.
Images are published before their manifest entries. An interrupted write can
leave an unreferenced image or `.tmp` file; the last published JSON references
complete images. Atomic replacement protects against interrupted writes, but
is not a power-loss durability guarantee (there is no per-frame fsync).

## Pull to the Mac after the run

Disconnect/stop the phone stream before pulling, so the dataset is no longer
changing. Leave the rover services running. On the Mac:

```sh
python3 tools/pull_capture.py --host ubuntu@100.103.169.40
# Optional local destination:
python3 tools/pull_capture.py --host ubuntu@100.103.169.40 --destination ~/farsight-captures
```

The script uses SSH to select the newest run with a published `transforms.json`,
then `scp -r` to copy it to `~/farsight-captures/<run-id>/`. It needs the existing
SSH access and performs no service lifecycle actions. Copying while the phone is
still streaming is not a consistent snapshot. Hand the copied directory to the
post-run reconstruction owner. Capture tests and simulator checks do not prove
physical image quality, successful splat training or Quest playback.

```sh
.venv/bin/python -m unittest jetson.mapping.tests.test_run_capture jetson.mapping.tests.test_pull_capture
```
