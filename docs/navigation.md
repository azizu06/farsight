# Navigation

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
