# Navigation

Nav2 path planning is **opt-in**. Without `--nav2` (`FARSIGHT_NAV2=1`) the server
runs exactly the reactive policy in `jetson/autonomy.py`, and `/nav/goal` answers
`409` (contract: [interfaces](interfaces.md#optional-navigation-status-and-go-to-goals-nav2)).
Everything below is simulation and Docker evidence until it runs on the rig.

```
iPhone (ARKit pose + LiDAR depth) --/phone--> jetson.server --/ws/nav (loopback)--> farsight_bridge (rclpy)
                                                   ^                                   | /odom, TF, PointCloud2,
operator held deadman --/ws/control--> ControlState |                                   | depth image + camera_info, Range
                                                   |   cmd_vel (m/s, rad/s)            v
                                      wheel PWM <--+<---------------------------- Nav2 (planner, MPPI/RPP, BT, behaviors)
```

## Running it

```
python -m jetson.server ... --nav2                     # server side; add --drive-profile for calibrated m/s
ros2 launch farsight_nav nav.launch.py                 # or the farsight-nav systemd unit
    [controller:=mppi|rpp] [slam:=false|true] [costmap_source:=pointcloud|nvblox]
```

- **Go-to-point** (`POST /nav/goal`) is the Nav2-backed mode today. Explore and
  Seek keep the reactive policy until their Nav2 versions land; the `nav.backend`
  field says which one is driving (`nav2` or `fallback`).
- **Fallback:** if the bridge is down, silent for 1.5 s, or Nav2's lifecycle is not
  active, `nav.backend` is `fallback` (or `null`), go-to goals are refused with `409`,
  and Explore/Seek drive the reactive policy. Nothing restarts or waits on ROS.
- **Gating:** the bridge is not a controller. Nav2's `cmd_vel` only moves the rover
  while the operator holds the deadman in an autonomy mode with a live 250 ms lease.
  `POST /nav/goal` only arms a goal; motion starts with the operator's own `goto`
  heartbeats. A command older than 250 ms, or computed for an earlier goal or
  before the current deadman press, stops; the forward range stop still applies
  (unless `--proximity-stop off`); stick input, STOP, cancel, a controller change
  or a serial disconnect disarm the goal.
- **Velocity mapping:** with `--drive-profile`, `cmd_vel` is clamped to the
  profile's top linear and angular speed at `--max-pwm`, then goes through
  `DriveProfile.wheel_pwm` like `velocity` heartbeats
  ([drive calibration](drive-calibration.md)). Without it, `linear / --nav-speed-mps`
  and `angular / --nav-turn-radps` become normalized `v, w`, capped by
  `--nav-max-v` / `--nav-max-w`. Keep Nav2's `vx_max` / `wz_max` at or under the
  profile's `limits`, or the profile saturates (curvature is kept).

## Frames

```
map -> odom -> base_link -> camera_link -> camera_optical
```

- **odom is the ARKit world.** ARKit is metres, Y up, the rover starting toward -Z.
  ROS odom is X forward, Y left, Z up: `X = -z`, `Y = -x`, `Z = y - floor_y`.
  Voxel-map `x, z` (what the viewer sees) is therefore ROS `-Y, -X`; every path,
  goal and frontier the server sends is in voxel-map `x, z`.
- **map -> odom** is identity, published by the bridge, unless `slam:=true`.
  The global costmap and goals live in `map`, so SLAM corrections move them with
  the world, not with ARKit drift.
- **base_link** is on the floor under the phone, yaw only. Its heading is the camera
  forward axis plus the drive profile's `camera_yaw_offset_rad` (travel heading minus
  camera heading, counter-clockwise); with the phone pointing nearly straight down,
  the camera's left axis is used instead.
- **camera_link** carries the phone height and pitch/roll relative to `base_link`
  (dynamic, from each pose). **camera_optical** is the static REP 103 optical rotation
  (z forward, x right, y down) that the point cloud and depth image use.

## Server and bridge protocol (`/ws/nav`, version 1)

Loopback only; one bridge at a time; other hosts get close code 1008.

| Direction | Message |
|---|---|
| bridge -> server | `hello {version, node, nav2, controller, slam, costmap_source}` |
| server -> bridge | `hello {version, speed_mps, turn_radps, max_v, max_w, camera_height, frame, camera_yaw_offset_rad, drive_profile}` |
| server -> bridge | `pose {transform (16, column-major ARKit camera-to-world), tracking, source, age_ms, floor_y}` every 40 ms; `floor_y` is `-nav_camera_height` |
| server -> bridge | binary depth: `uint32` header length, JSON header (`frame_id, width, height, fx, fy, cx, cy, transform, floor_y, source, age_ms`; intrinsics scaled to the subsampled size), `uint16` millimetres (0 = no return), at most 160 px wide |
| server -> bridge | `range {cm, age_ms}` at 10 Hz; `intent {active, mode, goal {x, y, yaw} in odom, goal_id, target_id}` on change and every 200 ms |
| bridge -> server | `cmd_vel {linear, angular, goal_id}` (m/s, rad/s counter-clockwise) at the controller rate; go-to commands for another `goal_id` are dropped |
| bridge -> server | `status {state, goal_id, path, frontiers, message, controller_hz, recoveries, nav2}` at 4 Hz; `path` and `frontiers` in odom |

The bridge publishes `/odom`, TF, `/farsight/depth/points` (PointCloud2),
`/farsight/depth/image` (32FC1 metres) with `/farsight/depth/camera_info` for nvblox,
and `/farsight/range` (Range). It sends `NavigateToPose` for an active intent and
cancels it 0.3 s after the intent goes inactive, so a lease hiccup does not drop
the goal.

## Nav2 configuration (`ros2/farsight_nav/config/nav2_params.yaml`)

- **No static map, no AMCL, no collision monitor.** ARKit is the odometry; the
  costmaps and the controller do obstacle avoidance, so a box that appears in front
  of the rover is steered around instead of freezing it.
- **Local costmap:** 5 x 5 m rolling window, 5 cm, `VoxelLayer` from the depth cloud
  (4-45 cm obstacles, raytrace clearing to 4.5 m), updated at 15 Hz so a pop-up
  obstacle lands on the next cycle. A second `depth_floor` source (-20..4 cm,
  clearing only) raytraces the floor returns that the height filter would
  otherwise drop, so free floor clears stale obstacles.
- **Global costmap:** 40 x 40 m, not rolling, `track_unknown_space` so explored and
  unexplored space stay distinct for frontiers and replanning.
- **Inflation:** `robot_radius` 0.13 m, inflation 0.35 m, cost scaling 6.
- **Planner:** `SmacPlanner2D`, `allow_unknown` (goals are often in unseen space),
  0.25 m tolerance.
- **Controller:** MPPI at 20 Hz (1000 samples, 2 s horizon, `vx_max` 0.30 m/s,
  `wz_max` 1.6 rad/s) or RPP (`controller:=rpp`, 0.28 m/s) if MPPI is too heavy on
  the Orin Nano. Both are loaded; the bridge picks one through the latched
  `controller_selector` topic.
- **Behavior tree** (`behavior_trees/navigate_farsight.xml`): replans at least every
  0.4 s, immediately when the path becomes invalid or the goal moves, and recovers
  in turn by clearing costmaps, backing up 0.25 m, spinning 90 degrees and waiting
  1 s. Goal checks are position only (0.20 m); final heading does not matter.

## Real-chassis tuning

Do these in order on the rig; record each result with conditions.

1. **Drive profile first.** Run `tools/calibrate_drive.py` and start with
   `--drive-profile`. Without it, measure top speed and spin rate at `--max-pwm`
   and pass them as `--nav-speed-mps` / `--nav-turn-radps`.
2. **Footprint.** Measure the chassis and set `robot_radius` (both costmaps) to
   half the widest dimension; set `inflation_radius` to at least
   `robot_radius + 0.15`. Narrow doorways need `2 * inflation_radius` under the gap.
3. **Obstacle band.** Check `min_obstacle_height` against the floor estimate: if the
   floor shows up as obstacles, raise it to 0.06; if low clutter is missed, lower it.
4. **Speed limits.** Set MPPI `vx_max` / `wz_max` (or RPP `desired_linear_vel`) at or
   under the profile `limits`, then raise only after a clean run.
5. **Controller rate.** Watch `controller_hz` in `/nav/status` `detail.remote`
   while driving. If MPPI cannot hold 15 Hz on the Jetson, lower `batch_size`
   (1000 to 600) or switch to `controller:=rpp`.
6. **Progress checker.** If recoveries trigger while the rover is slowly turning,
   raise `movement_time_allowance` (6 s) rather than lowering speeds.

## Tests

| Check | Command | Result |
|---|---|---|
| Server seam, gating, contract | `.venv/bin/python -m pytest -q jetson/tests/test_nav.py` | 14 pass |
| Frame math | `.venv/bin/python -m pytest -q ros2/farsight_nav/test` | 7 pass (host and container) |
| Bringup smoke | `bash ros2/docker/run-tests.sh smoke` | PASS in 17-19 s: Nav2 lifecycle active, topics and `map -> camera_optical` TF present |

The Docker image is `ros2/docker/Dockerfile` (ROS 2 Jazzy ros-base + Nav2 on
Apple silicon arm64). Nothing here has run on the Jetson or with real phone depth.

## Jetson install

`bash jetson/ros2-install.sh --check` prints the OS, disk, RAM and exact apt list
and changes nothing. A full run (needs internet for the ROS apt source and
packages, about 630 MB download, 3.2 GB installed) installs ROS 2 Jazzy ros-base
and Nav2, builds `ros2/` with colcon and installs the `farsight-nav` unit,
disabled unless `--enable`. `--with-slam` adds SLAM Toolbox. It was verified in a
fresh Ubuntu 24.04 arm64 container, not on the Jetson.

## SLAM (optional, `ros2/farsight_slam`)

`farsight_nav`'s bringup includes `ros2/farsight_slam/launch/slam.launch.py` with `slam:=true`.
Without it, the bridge publishes `map -> odom` as identity and ARKit is the only localization.

```
/farsight/depth/points (PointCloud2, camera_optical)
  -> pointcloud_to_laserscan (base_link, 0.06-1.2 m band, 62 deg cone, 0.15-5 m)
  -> /scan -> SLAM Toolbox (online async) -> map -> odom TF, /map
```

- **Frames:** `map -> odom -> base_link -> camera_link -> camera_optical`. ARKit world is
  `odom`, so SLAM only adds the drift correction `map -> odom`. Voxel-map `x, z` is
  ROS `x, y` in `odom`. Anything sent to the viewer in voxel-map coordinates (paths,
  goals, frontiers) must be transformed from `map` to `odom` first, because the
  correction changes when SLAM closes a loop.
- **Persistent map:** in mapping mode, `/map` and the `map` frame persist for the
  session, so go-to-point goals stay put while ARKit drifts. To keep a map across
  sessions, call `/slam_toolbox/serialize_map` with a filename, then relaunch with
  `slam_mode:=localization map_file:=<file>` and `start_pose:=x,y,yaw`, or send
  `/initialpose`. A new ARKit session has its own origin, so localization needs
  that start pose. It can be rough: in single simulated runs, start poses off by
  0.29 m and 10°, and by 0.5 m and 20°, both converged to 0.06 m within one lap.
- **Lifecycle:** SLAM Toolbox is a lifecycle node in Jazzy. The launch configures and
  activates it itself. If a Nav2 `lifecycle_manager` should own it instead, pass
  `use_lifecycle_manager:=true` and add `slam_toolbox` to that manager's node list.
- **Tuning:** `config/slam_toolbox_indoor.yaml` (each change from the Jazzy defaults is
  commented) and `config/pointcloud_to_laserscan.yaml`. The main changes:
  - SLAM trusts ARKit between scans: variance penalties go from 0.5/1.0 to 0.03, and
    the correlation window from 0.5 m to 0.3 m.
  - It adds scans every 0.25 m or 0.25 rad, with 0.15-5 m range and a 4 m loop search.
  - The scan's angle span is set so its reading count matches what Karto expects.
    Otherwise SLAM Toolbox silently drops every scan.

### Results (simulation only)

Source: `ros2/farsight_slam/test/run_docker.sh` (`test_loop_closure.py`, 12 tests pass).
The run uses ROS 2 Jazzy in Docker on the operator Mac (Apple silicon, 15 cores), with
three runs in parallel. Raw reports: `docs/qualification/slam-loop-sim.json`.

The simulated room is 8 x 6 m. The rover drives two laps of a 16.7 m loop around an
island at 0.35 m/s. The LiDAR is a 62 x 48 degree cone (160 x 12 rays, 10 Hz, 5 m),
with 1 cm + 1 % range noise. Position error is measured against ground truth.

| Odometry model | Odometry error mean / final | SLAM error mean / final | Final heading error, odometry → SLAM |
|---|---|---|---|
| Perfect | 0.00 / 0.00 m | 0.05 / 0.03 m | 0.0° → 0.2° |
| ARKit-like (1 % scale, 0.23°/m) | 0.13 / 0.34 m | 0.05 / 0.06 m | 4.4° → 0.5° |
| Heavy (3 % scale, 0.86°/m) | 0.50 / 1.36 m | 0.07 / 0.03 m | 22.2° → 0.4° |
| Relocalize in saved heavy map, new odometry origin, 1 lap | 0.21 / 0.61 m | 0.04 / 0.06 m | 9.7° → 0.2° |

- **Loop closure:** after the first lap, the pose graph has edges joining scans more
  than 30 scans apart: 29 for ARKit-like and 57 for heavy. These are revisit
  constraints that only appear on a closed loop.
- **Map:** the map stays square under heavy drift (`docs/assets/slam-loop-map.png`).
  Two 4 cm floor items stay out of it, because the scan band starts at 6 cm.
- **Resources:** `async_slam_toolbox_node` used a mean of 2-3 % of one core (max 5 %)
  and 54 MB RSS. `pointcloud_to_laserscan` used about 1.5 % and 32 MB. Localization
  mode used 3.8 % mean (max 8 %) and 56 MB. This was measured on the Mac, not the
  Jetson. Orin Nano A78AE cores are slower, so expect a few times more CPU. That
  should still fit easily, but it is unmeasured.
- **Tuning trade-off:** with the Jazzy default penalties, SLAM made perfect odometry
  worse: 0.27 m mean, 0.63 m max, from a narrow scan sliding along walls. The
  ARKit-trusting penalties fixed that.

What this does not show:
- Real iPhone LiDAR on the rig.
- ARKit relocalization jumps.
- People or moving obstacles in the scan.
- Glass, mirrors, or long featureless corridors.
- Timing on the Jetson.

The simulator is 2D-extruded, with perfect sensor timing. With good ARKit tracking on a
short demo, SLAM mainly helps on revisits and long missions. Leave `slam:=false` unless
the run loops back or lasts long enough for ARKit drift to show.

### robot_localization EKF: skipped

An EKF adds nothing with Farsight's sensors, so `farsight_slam` does not run one.
- **No second motion source:** ARKit is the only one. It already fuses the phone's
  camera and IMU at 60 Hz. Feeding the same IMU again would double-count it. There are
  no wheel encoders, and the HC-SR04 gives a range, not motion.
- **One input:** an EKF with one input only re-publishes it, with added lag and made-up
  covariances. ARKit gives no covariance. It cannot detect or bridge ARKit tracking loss
  without a second source.
- **Drift:** the EKF fixes local noise, not global drift. Global drift is what SLAM's
  `map -> odom` corrects.
- **Planar projection:** the bridge already flattens the ARKit pose to a planar
  `base_link`, so an EKF's `two_d_mode` adds nothing there.

Add an EKF when there is a second motion source: wheel encoders, a separate IMU, or
cmd_vel-based dead reckoning to coast through ARKit tracking loss. So
`ros-jazzy-robot-localization` is not needed.

### Jetson packages

```
sudo apt install ros-jazzy-slam-toolbox ros-jazzy-pointcloud-to-laserscan
```

These are on top of the Nav2 install, and SLAM Toolbox pulls in Ceres.
`python3-psutil` is only needed for the simulator tests. Never run the simulator tests
on the Jetson; they are for the Mac/Docker image (`ros2/farsight_slam/docker/Dockerfile`).
