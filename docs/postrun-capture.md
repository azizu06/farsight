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
pose, intrinsics and measured LiDAR points. The current RGB-only `/phone/rgb` protocol lacks pose and
intrinsics and cannot produce this dataset. The current streamed RGB-D JPEG is
resized on iOS (normally 640 pixels wide); full-resolution RGB recorded locally
on the phone with synchronized pose/intrinsics is the preferred training input.
This fallback does not restore missing image detail.

Each connection/session/epoch gets a new UTC-named run:

```text
~/captures/<run-id>/
  images/000000.jpg
  images/000000.points.bin
  images/000001.jpg
  images/000001.points.bin
  transforms.json
  dense.ply                 # when the segment finishes
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

Each frame's `points_path` names a compact binary file next to its JPEG, with
`points_count` records. The root declares `points_format=xyz_f32le_rgb8`: each
15-byte record is three little-endian float32 world coordinates followed by
three RGB uint8 values, without padding or a separate header. Float32 preserves
detail as the ARKit world origin gets farther away. Up to 12,288 native measured
pixels are retained per captured frame; existing confidence, depth-edge and
grazing filters apply. No interpolated or duplicated samples fill the budget.
JPEG, points and pose belong to the same accepted frame/session/epoch. Limited
tracking keeps the JPEG/pose but saves an empty point file rather than fusing
unreliable world geometry.

On a connection/session/epoch change, five seconds without a saved frame, or
graceful shutdown, the writer exports `dense.ply` in binary little-endian PLY
format. It retains the first measured xyz/RGB per 1 cm voxel, capped at 500,000
vertices. `point_cloud` in the manifest records its path, count, completion and
`truncated` flag; the per-frame files remain available even when the merged PLY
hits its cap. Long streams start another segment every 3,000 saved frames to
bound manifest memory. Resuming after an idle export also starts a new segment.
These are offline measured history, which can include moving objects; they are
not permanently fused into the live map or automatically reloaded by the relay.

The observer runs outside `state.lock`. An eight-item nonblocking queue holds
validated RGB-D frame references and small metadata. Sampling, merging and all
disk access run in the dedicated writer thread. It publishes complete JPEG/point
pairs before atomically replacing the JSON. Slow/full disks drop
samples rather than waiting in ingest, health or control. `/health.run_capture`
reports `written`, `queued`, `dropped`, `failed`, `error`, the current `run`, and
`disk_bytes`, `disk_limit_bytes`, `disk_limited`. The writer counts existing data
on startup, then budgets its writes against a 2 GiB total capture-root limit,
reserving space for the merged PLY and temporary manifest. When full, it stops
saving new pairs and preserves all earlier files; it never evicts old captures.
An interrupted write can
leave an unreferenced image or `.tmp` file; the last published JSON references
complete JPEG/point pairs. Atomic replacement protects against interrupted writes, but
is not a power-loss durability guarantee (there is no per-frame fsync).

## Pull to the Mac after the run

Disconnect/stop the phone stream and wait for `point_cloud.complete` in its
manifest (normally five seconds plus export time) before pulling. Leave the rover
services running. On the Mac:

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

An abrupt power loss may prevent the final PLY export; already published
per-frame point files stay usable alongside the photos. After pulling, rebuild
the PLY from those files without contacting the Jetson:

```sh
.venv/bin/python -m jetson.mapping.run_capture ~/farsight-captures/<run-id>
```

This cannot recover points from older photo-only datasets or uncommitted frames.

```sh
.venv/bin/python -m unittest jetson.mapping.tests.test_run_capture jetson.mapping.tests.test_pull_capture
```
