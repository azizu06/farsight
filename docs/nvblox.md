# nvblox GPU costmap (optional)

Isaac ROS nvblox as an alternative obstacle source for Nav2, behind the core seam `costmap_source:=nvblox`.
`costmap_source:=pointcloud` stays the default and the fallback until the go/no-go below passes **on the Jetson**.
Nothing here has run on the Orin Nano yet: it was written and checked on a Mac (config, launch, scripts, fakes),
because nvblox needs an NVIDIA GPU. Navigation itself is in `docs/navigation.md` (core nav, `ros2/farsight_nav`).

Package: `ros2/farsight_nvblox`. Scripts: `jetson/nvblox-install.sh`, `jetson/nvblox-smoke.sh`.
Background: the scout report on Isaac ROS 4.6 (Jazzy, JetPack 7.2, R39.2) found it installable and supported for one
camera with static mapping on "Orin Nano 8GB", but unproven on this board and with unmeasured RAM.

## When to use which

| | `pointcloud` (default) | `nvblox` |
| --- | --- | --- |
| Obstacle input | ARKit depth points through Nav2's obstacle layer | Depth integrated into a TSDF on the GPU, 2D ESDF slice |
| Memory of the room | Short: the layer clears and re-marks | Persistent map within `map_clearing_radius_m` (6 m) |
| Gaps and noise | Single-frame speckle shows as obstacles | Fused over frames, fewer phantom obstacles |
| Cost | Cheap, CPU | GPU plus RAM on a board already running YOLO, depth, Whisper and a VLM |
| Risk | Known quantity | Unproven on the Nano; one-hour test decides |

Use nvblox only if the smoke test passes, the live gates pass, and Nav2 does better on the same route than the
pointcloud costmap. Otherwise ship `pointcloud`; do not spend hackathon hours tuning around an unsupported-board failure.

## How it plugs in

```text
core bridge (farsight_nav)                       nvblox container (farsight_nvblox/nvblox.launch.py)
  depth packet -> depth/image  (32FC1, metres)  ->  NvbloxNode  -> /nvblox_node/static_map_slice
               -> depth/camera_info                                  |
  pose -> TF odom->base_link->camera_link->camera_optical            v
                                                    Nav2 nvblox_layer (local + global costmap)
```

- **Launch.** The core launch includes `farsight_nvblox/launch/nvblox.launch.py` when `costmap_source:=nvblox`.
  Arguments are listed at the top of that file (`voxel_size`, `slice_min_height`, `slice_max_height`, `mesh_rate_hz`).
- **Nav2 layers.** `farsight_nvblox.costmap_params.nvblox_nav2_params(base_yaml)` returns a copy of the core's Nav2
  params with `nvblox::nav2::NvbloxCostmapLayer` replacing the obstacle/voxel layer in both costmaps (static and
  inflation layers keep their place; both read `/nvblox_node/static_map_slice`; the Nano has no people layer).
  Pass `resolution=<voxel_size>` to match the costmap to the voxel grid. Also a CLI:
  `python3 -m farsight_nvblox.costmap_params nav2.yaml nav2_nvblox.yaml --resolution 0.05`.
  The core launch picks that file instead of the base one only for `nvblox`; `pointcloud` never imports this module.
- **Depth topics (needs the core bridge).** `jetson/nav.py` serves exactly one `/ws/nav` client, so nvblox cannot
  open its own depth stream. The bridge must publish, from each depth packet it already decodes, `depth/image`
  (`sensor_msgs/Image` 32FC1, metres, 0 = no return) and `depth/camera_info` (header `fx fy cx cy`, frame
  `camera_optical`), stamped like its TF. `farsight_nvblox.depth_msgs.DepthPublisher(node).publish(header, depth)`
  does exactly that and is safe to call from the bridge. Low-confidence samples are already zeroed by the server.
- **Frames.** `odom` is gravity-aligned, Z up, Z = 0 on the floor (`ros2/farsight_nav/farsight_nav/frames.py`). The
  ESDF slice heights are metres above that floor, so a wrong floor offset or a pose in the wrong frame shows up as the
  floor becoming an obstacle or nothing being seen.
- **Config.** `ros2/farsight_nvblox/config/nvblox.yaml`: one depth camera, static TSDF, no colour, no mesh,
  `voxel_size 0.05`, depth integration at 15 Hz, ESDF at 10 Hz, slice 0.06 to 0.25 m (phone at about 0.15 m; tune to
  the real rover body). The smoke test fails if the node does not declare a parameter from this file, so a name
  that this nvblox release does not know cannot be silently ignored.

## Install (Jetson, needs internet, once)

```sh
bash jetson/nvblox-install.sh --check     # R39 + CUDA 13 + ROS + disk, apt simulation, writes nothing
bash jetson/nvblox-install.sh             # adds the release-4.6 apt source, installs only 3 packages
```

It installs `ros-jazzy-nvblox-ros`, `ros-jazzy-nvblox-nav2`, `ros-jazzy-nvblox-msgs` (about 346 MB with the Isaac
dependencies), never `ros-jazzy-isaac-ros-nvblox` (3.2 GB: Triton, cuVSLAM, UNet). cuVSLAM is out of scope: ARKit
pose is better than any single-phone visual odometry we could feed it. The apt source is pinned to `release-4.6`
(5.0 is ROS 2 Lyrical only) at priority 1, so it cannot replace JetPack's OpenCV, TensorRT or CUDA. The script
simulates first and **stops** if apt would remove anything, upgrade a JetPack-owned library, or install CUDA/cuDNN/
TensorRT packages (override only after reading the list: `--allow-cuda-packages`).

Bare-metal apt is the path the spec asked for. NVIDIA recommends Docker and documents removing JetPack's OpenCV for
bare metal; if the simulation shows that conflict, stop and use NVIDIA's Docker mode instead (same package names,
host networking, same `ROS_DOMAIN_ID` and `RMW_IMPLEMENTATION` as the bridge). Download before the venue: the board
has no internet of its own.

Rollback: `sudo apt-get remove ros-jazzy-nvblox-ros ros-jazzy-nvblox-nav2 ros-jazzy-nvblox-msgs`, then delete
`/etc/apt/sources.list.d/nvidia-isaac-ros.list`, `/usr/share/keyrings/nvidia-isaac-ros.gpg` and
`/etc/apt/preferences.d/nvidia-isaac-ros`.

## One-hour go/no-go

```sh
bash jetson/nvblox-smoke.sh --check                       # packages present, tegrastats available
bash jetson/nvblox-smoke.sh --source synthetic            # 10 min on a built-in room; needs no phone
bash jetson/nvblox-smoke.sh --source bag --bag ~/bags/arkit-hall   # recorded depth + camera_info + TF
bash jetson/nvblox-smoke.sh --source live --nav2          # attach to a running costmap_source:=nvblox stack
bash jetson/nvblox-smoke.sh --source synthetic --mesh-hz 2   # costs the mesh too, if the viewer wants it
```

Run it with the rover server and YOLO up (as in the demo) so the memory and GPU numbers are honest, at the 15 W
power mode the rover uses; do not raise it without the rig power test. `synthetic` publishes its own TF, so never
run it next to the real bridge. The report (default `~/nvblox-smoke-<time>.md`) records the source, load notes,
power mode, versions and a PASS/FAIL per gate; exit code 0 = PASS, 1 = FAIL.

| Gate | Pass when | If it fails |
| --- | --- | --- |
| startup | `/nvblox_node` appears within 90 s | Stop: likely an R39.2.1 vs 39.2.0 or `sm_87` library mismatch |
| cuda errors | no CUDA/VPI/out-of-memory line in the launch log | Stop |
| parameters | every key of `nvblox.yaml` is declared by the node | Fix the name in `nvblox.yaml` |
| esdf slice rate | median >= 5 Hz on `/nvblox_node/static_map_slice` (configured 10 Hz) | Raise `voxel_size` to 0.08, lower `integrate_depth_rate_hz` |
| slice latency | mean depth-stamp to slice delay <= 300 ms | Same levers; check TF extrapolation warnings |
| memory | lowest `MemAvailable` over the run >= 1000 MB | Stop, or free RAM (VLM off during Explore) |
| gpu | mean `GR3D_FREQ` <= 90% | Lower rates; GPU saturation starves YOLO |
| mesh rate | only with `--mesh-hz`; >= half the configured rate | Leave the mesh off |
| costmap update rate | only with `--nav2`; >= 2 Hz on `/local_costmap/costmap` | Check the layer plugin and slice topic |

Swap use and missing latency data are warnings, not verdicts. The script measures only; it changes no service,
power mode or package.

**Go** means the script reports PASS and then, by hand on the real rig:

1. Live phone stream: slice appears within about 300 ms of the depth stamp, no TF extrapolation warnings, walls where
   the room has walls and the floor clear (view the slice in RViz or the Quest nav view).
2. Nav2 drives around a chair in a hallway with the nvblox costmap, compared against `costmap_source:=pointcloud`
   on the same route. Record the source, load and conditions of every latency or throughput number.

**No-go** is any failed gate 1 to 3 (startup, CUDA, memory): ship `pointcloud`, drop nvblox. A failed rate or latency
gate may be tuned once (voxel 0.08, lower rates); if it still fails, same decision.

## Fallback and recovery

- `costmap_source:=pointcloud` is always the default; nvblox is opt-in per launch.
- If nvblox dies mid-run the slice stops updating and the nvblox layer keeps the last map. Treat a stale
  `/nvblox_node/static_map_slice` (no message for about 1 s) as a failure and relaunch with `pointcloud`; Nav2's
  stop-on-stale-costmap behaviour and the server's stop leases apply unchanged.
- RAM levers, in order: `use_color` is already off; `voxel_size` 0.08; `map_clearing_radius_m` 4; lower
  `integrate_depth_rate_hz`; stop the VLM while the rover explores.

## Tests

```sh
python3 -m pytest jetson/tests/test_nvblox_smoke.py jetson/tests/test_nvblox_scripts.py   # verdict logic + scripts against fakes
ros2/farsight_nvblox/test/run_docker.sh                                                  # package tests, launch --show-args, synthetic depth
```

These prove the verdict thresholds, the scripts' branching, the config/launch syntax and the synthetic source. They do
not prove nvblox runs on the Nano: only `jetson/nvblox-smoke.sh` on the board does.
