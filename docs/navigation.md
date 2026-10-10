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
    [controller:=rpp|mppi] [slam:=false|true] [costmap_source:=pointcloud|nvblox] [explore:=true|false]
    [bridge_executor:=events|multithreaded]
```

- **Modes:** with Nav2 up, all three autonomy modes plan with Nav2; `nav.backend`
  is `nav2` while Nav2 drives and `null` when nothing can.
  - **Go-to-point:** `POST /nav/goal` arms a voxel-map `x, z`; the operator's held
    `goto` heartbeats drive it. A goal posted while stopped (deadman not held) is
    staged and waits for a new press.
  - **Explore:** the bridge finds frontiers (free cells next to unknown space,
    8-connected clusters of at least 30 cm) on the global costmap every 0.5 s and
    scores observed wide approaches, visible unknown area, frontier size,
    heading, thin clutter and decaying path revisits. Goals stay in known free
    space, one footprint short of the unseen boundary. A candidate must support
    a 0.36 m wide goal (any gap the ~0.35 m body fits through) and final 1.2 m
    approach; if none does, a relaxed pass accepts a 0.36 m corridor with a
    0.6 m approach. If both reject every frontier (common in a real room, where
    the approach is still unknown), Nav2 gets the nearest broad frontier anyway
    and plans through unknown; that goal is kept while it lasts. On the
    2026-10-10 floor run without this fallback, 73% of a 3-minute Explore had
    22-25 frontiers but "no reachable frontier", and the rover spent most of it
    scanning in place. Nav2 plans the full route. The
    current broad direction is held for at least 2 s and changes only for a
    score improvement of 0.8, unless its approach becomes unsafe or disappears.
    Within 0.6 m it selects the next patch before the current goal completes.
    A frontier is skipped for 30 s, and the next one picked at once, when Nav2
    aborts or rejects it, starts a recovery on it, gets no 0.3 m closer for 3 s,
    or has not reached it in 15 s. When the rover stays within 0.4 m for 8 s
    (spinning, dithering or pushing), the current frontier is skipped and the
    next goal must lie at least 1.5 m from that spot for 6 s.
    Explore never ends on its own (captain's order, 2026-10-10). Planning gaps,
    and stretches with no frontier at all, creep along the most open
    measured arc; with no open arc it turns in place to look, restarting after
    each full turn, and re-scores frontiers every 0.5 s. A detected
    person never switches Explore to Seek: people are moving obstacles in the
    LiDAR costmaps. Seek runs only when the operator selects it.
    The 20 Hz motion arbiter forwards Nav2 whenever its command is moving.
    Otherwise (planning gaps, goal changes, a controller zero, a recovery
    hand-off) it drives itself, using the current validated depth and pose
    (each under 0.5 s) and a costmap under 2.5 s. It projects the sampled swept
    corridor at rover body height into the depth camera: every ray needs a real
    return beyond the corridor. Unknown/missing forward rays are blocked for that
    arc (it picks another arc or turns), never stopping Explore.
    The current 0.2 m body disc and actual sampled pose path fill only unknown
    near-field cells; measured costs remain intact. Something measured inside
    the body disc makes it back straight off (at most 0.35 m, only over floor
    the costmap has seen or the rover has driven), then turn away from it.
    The fallback selects the widest supported gentle arc at 0.25 m/s, or turns
    in place at 1.8 rad/s (about PWM 95 per side; at 0.82 rad/s the skid-steer
    sat at L75/R60, just above the floors, and stalled under payload); it never
    stands still waiting for Nav2 (captain,
    2026-10-10). Only a stale pose, depth or costmap stops it. Server deadman, B/STOP, the lease
    (link-loss stop) and the Nav2 sensor gate still apply; the
    `/ws/nav` and viewer status contracts are unchanged.
  - **Seek:** the server projects the tracked person's box through the LiDAR depth
    (median of the torso region) to a floor point, and Nav2 drives to a point
    0.8 m short of it on the line from the rover. The goal is re-sent whenever the
    person moves more than 0.3 m, so the rover follows a walking person. Within
    0.95 m the rover stops (`arrived`); with the person unseen for 3 s, Seek stops
    with `failed` (`Target lost`).
- **No blind fallback:** if the bridge is down, silent for 1.5 s, or Nav2's lifecycle is
  not active, `nav.backend` is `null` and `nav.state` is `unavailable`, go-to goals are
  refused with `409`, and Explore/Seek hold still (zero output) instead of running the
  reactive policy, which cannot see obstacles without the ultrasonic. Holding the deadman
  resumes once the bridge is back. Without `--nav2` the reactive policy still runs.
- **Sensor gate:** Nav2 drives only with a normal-tracking ARKit pose (at most 0.5 s
  old) and LiDAR depth (at most 1 s old) from the current phone session. There is no
  dead reckoning. Losing either stops the rover and needs a new deadman press after
  recovery; a new phone session or map epoch disarms the goal and resets Nav2.
- **Gating:** the bridge is not a controller. Nav2's `cmd_vel` only moves the rover
  while the operator holds the deadman in an autonomy mode with a live 250 ms lease.
  `POST /nav/goal` only arms a goal (staged without a lease); motion starts with the
  operator's own `goto` heartbeats on a new press. A command older than 250 ms, or computed for an earlier goal or
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
| server -> bridge | `hello {version, speed_mps, turn_radps, max_v, max_w, camera_height, frame, camera_yaw_offset_rad, drive_profile, generation, command_generation_required:true}` |
| server -> bridge | `reset {generation, reason}` when the phone session or map epoch changes: the bridge cancels its goal, clears both costmaps and its odometry history |
| server -> bridge | `pose {transform (16, column-major ARKit camera-to-world), tracking, source, generation, age_ms, floor_y}` every 40 ms, only with fresh normal tracking; `floor_y` is `-nav_camera_height` |
| server -> bridge | binary depth: `uint32` header length, JSON header (`frame_id, width, height, fx, fy, cx, cy, transform, floor_y, source, age_ms, generation, tracking`; intrinsics scaled to the subsampled size), normal-tracking frames only, `uint16` millimetres (0 = no return), at most 160 px wide |
| server -> bridge | `range {cm, age_ms}` at 10 Hz; `intent {generation, active, mode, goal {x, y, yaw} in odom, goal_id, target_id}` on change and every 200 ms |
| bridge -> server | `cmd_vel {generation, linear, angular, goal_id}` (m/s, rad/s counter-clockwise) at the controller rate; Goto/Seek commands for another `goal_id` are dropped |
| bridge -> server | `status {generation, state, goal_id, path, frontiers, message, controller_hz, recoveries, nav2}` at 4 Hz; `path` and `frontiers` in odom |

Deploy the Python server and rebuilt `farsight_nav` ROS overlay from the same
revision. `cmd_vel` and `status` must echo the current integer generation from
hello/reset; missing, boolean or older generations fail closed. Goto and Seek
also require the exact current goal ID. Replacing a Seek goal withdraws its
previous command immediately. The bridge blocks Twist forwarding during reset,
pending goal acceptance and rejection, and captures authority before enqueueing.
An older bridge cannot drive the updated server. With the rover stopped, rebuild
the overlay with `colcon build --packages-select farsight_nav` from `ros2/`, source
its `install/setup.bash` in the bridge launch environment, then restart the server
and bridge together using the rig's existing service manager. These are deployment
steps, not commands for an unattended test.

Pose/depth publication rechecks the generation after CPU work and announces reset
before new-world data. Depth age includes encoding and worker queueing; samples
that expire before sending are dropped. Raw ROS `Twist` has no goal ID, so a delayed
old Twist arriving after a new goal is accepted cannot be conclusively attributed.
Supervised goal-change and reconnect checks on the deployed ROS stack remain
necessary; callback tests do not establish physical stopping behavior.

The bridge drops any pose or depth sample whose `tracking` is not `normal` or whose
`generation` is not the current one, so limited tracking or an old phone session
never renews TF, odometry or costmaps. It publishes `/odom`, TF, `/farsight/depth/points` (PointCloud2),
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
  unexplored space stay distinct for frontiers and replanning. Updated at 10 Hz:
  `IsPathValid` checks it, so a person stepping onto the path invalidates it within 0.1 s.
- **Inflation:** `robot_radius` 0.17 m (the ~0.35 m wide body, no extra margin),
  inflation 0.22 m (footprint + 5 cm), cost scaling 8, so the rover threads gaps
  just over its width (captain's call, 2026-10-10; earlier 0.16 / 0.28 m). With 0.35 m inflation the rover dithered at such gaps;
  with a 0.13 m radius RPP clipped a wall end.
- **Planner:** `SmacPlanner2D`, `allow_unknown` (goals are often in unseen space),
  0.25 m tolerance.
- **Controller:** Regulated Pure Pursuit by default (20 Hz, 0.70 m/s, 0.2-0.5 m
  speed-scaled lookahead, rotate in place only beyond 1.0 rad, otherwise arc), or MPPI
  with `controller:=mppi` (1000 samples, 2 s horizon, `vx_max` 0.70 m/s, `wz_max`
  2.5 rad/s). These suit `FARSIGHT_AUTONOMY_MAX_PWM=200` on the 2026-10-10 drive
  profile (0.74 m/s, 3.8 rad/s); no cost-regulated slow zone near obstacles and
  only 0.2 m of goal approach slowing. The earlier simulated results used 0.28-0.30 m/s. Both are loaded; the bridge picks one through the latched
  `controller_selector` topic. RPP is the default because it was faster in every
  simulated world and lighter (see the results below); MPPI hesitated in 0.65 m
  gaps. RPP does not dodge obstacles by itself: the 0.25 s replanning does, which is
  how it steers around a pop-up obstacle without stopping.
- **Odometry twist:** the bridge estimates `/odom` velocity over a 150 ms window.
  Differencing single 30 Hz poses with jittery stamps produced rate spikes that
  made MPPI believe the rover was still spinning, and it spun in place.
- **Behavior tree** (`behavior_trees/navigate_farsight.xml`): ticks at 20 Hz and
  replans at least every 0.25 s, within 50 ms when the path becomes invalid (the
  global costmap updates at 15 Hz), or when the goal moves. It recovers in turn by
  clearing the local costmap and the global costmap within 1.5 m of the rover (the
  explored map stays), backing up 0.25 m at 0.3 m/s and spinning 90 degrees at up
  to 2.2 rad/s; it never waits. Goal checks are position only (0.20 m); final
  heading does not matter.

## Real-chassis tuning

Do these in order on the rig; record each result with conditions.

1. **Drive profile first.** Run `tools/calibrate_drive.py` and start with
   `--drive-profile`. Without it, measure top speed and spin rate at `--max-pwm`
   and pass them as `--nav-speed-mps` / `--nav-turn-radps`.
2. **Footprint.** Measure the chassis and set `robot_radius` (both costmaps) to
   half the widest dimension plus about 4 cm; keep `inflation_radius` about
   `robot_radius + 0.12`. A gap wider than `2 * inflation_radius` keeps a zero-cost
   centre line; narrower gaps are passable but slow.
3. **Obstacle band.** Check `min_obstacle_height` against the floor estimate: if the
   floor shows up as obstacles, raise it to 0.06; if low clutter is missed, lower it.
4. **Speed limits.** Set RPP `desired_linear_vel` (or MPPI `vx_max` / `wz_max`) at or
   under the profile `limits`, then raise only after a clean run.
5. **Controller rate.** Watch `controller_hz` in `/nav/status` `detail.remote`
   while driving. RPP should hold 20 Hz easily; with `controller:=mppi`, lower
   `batch_size` (1000 to 600) if it cannot hold 15 Hz on the Jetson.
6. **Progress checker.** If recoveries trigger while the rover is slowly turning,
   raise `movement_time_allowance` (6 s) rather than lowering speeds.

## Closed-loop simulator

`python -m jetson.server --sim-world NAME` (implies `--demo` and `--simulate-rover`)
drives a ray-cast rover through a test floor plan from `jetson/sim/worlds.py`:
`popup`, `doorway`, `deadend`, `floorplan`, `seek`. Applied motor commands move the
simulated rover; its camera renders 160 x 120 depth and RGB that go through the
same `MapState` path as a LiDAR iPhone (pose at 30 Hz, RGB-D at 10 Hz), so the
bridge, voxel map, camera freshness and sensor gate all run unchanged. A
simulated ultrasonic (~206 cm echo limit) feeds `/farsight/range` only; the rig
as built has none (#91), Nav2's costmaps use depth alone, and the scenarios run
with `--proximity-stop off`. Seek gets ground-truth person boxes. Everything is labelled `SIMULATED`.
`GET /sim/state` (only with `--sim-world`) returns ground truth: pose, collisions,
minimum clearance, distance driven, pop-up events, people and floor coverage.

`ros2/tests/scenarios.py` runs each world against Nav2 with a real held-deadman
`/ws/control` operator, with `controller:=mppi` and `controller:=rpp`; reports go
to `ros2/tests/out/scenarios.json` and per-run timelines to `*-timeline.json`.

## Tests

| Check | Command | Result |
|---|---|---|
| Server seam, gating, contract, Seek | `.venv/bin/python -m pytest -q jetson/tests/test_nav.py` | 22 pass |
| Simulator worlds and sensors | `.venv/bin/python -m pytest -q jetson/tests/test_sim.py` | 5 pass |
| Frame math, frontiers | `.venv/bin/python -m pytest -q ros2/farsight_nav/test` | 12 pass (host and container) |
| Bringup smoke | `bash ros2/docker/run-tests.sh smoke` | PASS in 17-19 s: Nav2 lifecycle active, topics and `map -> camera_optical` TF present |
| Closed-loop scenarios | `bash ros2/docker/run-tests.sh scenarios` | see below |

The Docker image is `ros2/docker/Dockerfile` (ROS 2 Jazzy ros-base + Nav2 on
Apple silicon arm64). Nothing here has run on the Jetson or with real phone depth.

Without Docker (for example on the Jetson, from a separate checkout), the same
scripts take their paths from the environment. The live server already holds 8443,
and the harness refuses a port that is in use, because its test operator holds a
real deadman lease:

```
source /opt/ros/jazzy/setup.bash && source ros2/install/setup.bash
export ROS_DOMAIN_ID=42 ROS_AUTOMATIC_DISCOVERY_RANGE=LOCALHOST
export FARSIGHT_REPO=$PWD FARSIGHT_SERVER_PYTHON=~/farsight/jetson/.venv/bin/python
export FARSIGHT_OUT=/tmp/farsight-nav-out FARSIGHT_TEST_PORT=8601
python3 ros2/tests/smoke.py
```

### Scenario results (simulation only)

Source: one `run-tests.sh all` run on the operator Mac (Docker Desktop, 15 cores,
7.7 GB VM), one scenario at a time, with `bridge_executor:=events` (the default).
Raw reports and verdicts: `docs/qualification/nav-scenarios-sim.json`. RPP is the
default and gates the suite; MPPI runs are informational.

| World | What it checks | RPP (default) | MPPI |
|---|---|---|---|
| `popup` | a 0.5 m box appears in the corridor when the rover is 1.75 m from it; no standstill over 0.5 s | arrived in 26.4 s, longest standstill 0.41 s, clearance 0.24 m | stood still 1.22 s (fails the gate) |
| `doorway` | the only way to the far room is a 0.8 m door off to one side | arrived in 26.0 s, 0 recoveries | 26.8 s |
| `deadend` | a 0.7 m corridor at the goal closes once the rover is inside; back out and take the side gap | arrived in 73.4 s, 4 recoveries | 72.9 s |
| `floorplan` Explore | three rooms; time to see 90 % of reachable floor | 91 % in 25.0 s, 0 collisions | 90 % in 33.4 s |
| `seek` | follow a 0.25 m/s walker, 0.8 m standoff | 5.21 m to a 1.18 m median gap (max 2.04 m) | 1.24 m median gap |

All runs: 0 collisions, controller 20 Hz median. CPU in % of one core, mean over a
run: bridge 4-6 %, `jetson.server` 29-38 %, `controller_server` about 5 % with RPP
and 7 % with MPPI, `bt_navigator` 2-5 %, `planner_server` about 2 %.

The bridge used to take 80-107 % of a core. py-spy showed 78 % of its samples
inside rclpy's Python `MultiThreadedExecutor` rebuilding its wait set on every TF,
costmap and action-feedback wakeup, not in bridge code. The bridge now spins
rclpy's C++ `EventsExecutor` (`rclpy.experimental`, present in Jazzy rclpy 7.1.12
on the Mac image and the Jetson). Its callbacks never block, so one thread is
enough. `bridge_executor:=multithreaded` restores the old executor. In the earlier
run the doorway and dead end took longer (up to 61 s and 121 s with more
recoveries), so treat single times as examples, not bounds.

## Jetson install

`bash jetson/ros2-install.sh --check` prints the OS, disk, RAM and exact apt list
and changes nothing. A full run (needs internet for the ROS apt source and
packages, about 630 MB download, 3.2 GB installed) installs ROS 2 Jazzy ros-base
and Nav2, builds `ros2/` with colcon and installs the `farsight-nav` unit,
disabled unless `--enable`. `--with-slam` adds SLAM Toolbox. It was verified in a
fresh Ubuntu 24.04 arm64 container, not on the Jetson.

On the rig (2026-10-10) the unit is enabled and Nav2 activates on the Jetson. The unit
waits for `/nav/status` to report a live phone pose before launching: Nav2's lifecycle
bringup aborts if `base_link` has no TF for about 60 s, which stranded the first start
until a manual `systemctl restart farsight-nav`.

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

### Open-space Explore tuning and evidence

The `farsight_bridge.ros__parameters` section of
`ros2/farsight_nav/config/nav2_params.yaml` contains all Explore weights and
limits. Scoring runs in one bounded background worker so the 20 Hz command
arbiter and sensor callbacks keep running during a large-map score pass; an
operator/session generation check discards late results after release/reset.
`params_file:=/path/to/params.yaml` applies the same file to the bridge
and Nav2. The `ExploreConfig` defaults in `frontiers.py` define the score.
Clearance uses a capped Euclidean distance transform of lethal + inscribed
cells (99/100), with a second transform requiring observed traversable support
throughout the footprint. Actual occupied/swept body discs fill the near-field
unknown blind ring without erasing measured obstacles. A cluster supplies up to
32 boundary samples so curved camera frontiers are not reduced to a close
centroid. Distant all-unknown map padding is cropped with a full sensor-range
rim to keep the 2 Hz NumPy work small. Unknown area beyond the body trail is
prospective information gain only;
known obstacles occlude it. Small lethal blobs near the approach add clutter
cost. The 25 cm world-aligned visit grid decays over 60 s and survives rolling
costmap origins. Goal failure/timeout blacklists and commitment reset on a new
Explore press; visits persist until the sensor session resets.

| Parameter suffix (`explore_…`) | Default | Effect |
| --- | --- | --- |
| `min_corridor_width`, `approach_length` | 0.7 m, 1.2 m | Reject narrow goals and short approaches |
| `sensor_range` | 4.0 m | Visible unknown disc; configurable within 0.3–4 m |
| `clearance_weight`, `information_weight`, `size_weight` | 2.5, 3.0, 1.5 | Prefer spacious unseen patches |
| `distance_weight`, `turn_weight`, `clutter_weight`, `revisit_weight` | 0.35, 0.8, 3.0, 3.0 | Penalize long routes, turns, thin clutter, revisits |
| `commitment_s`, `switch_margin` | 4 s, 0.8 | Resist oscillation; unsafe targets bypass commitment |
| `transition_distance`, `revisit_decay_s` | 0.6 m, 60 s | Preselect next patch; revisit decay |
| `creep_mps`, `scan_radps`, `gap_budget_s` | 0.2 m/s, 0.82 rad/s, 4 s | Fresh-depth planning fallback |
| `body_radius`, `scan_budget_s` | 0.35 m, 10 s | Actual swept-body support; scan stops after one measured full turn or the time ceiling |

Synthetic scenario (`test_frontiers.py`, 10 cm cells): the prior chooser selected
near table-leg clutter at `(4.45, 2.05)`. The new chooser selects the broad
hallway frontier `(7.45, 5.05)`, with its observed-free setback goal at
`(7.008, 4.968)`. Tests also cover narrow-gap rejection, unknown approaches,
visibility occlusion, hysteresis, revisit decay and immediate unsafe-goal release.
`test_local_motion.py` exercises 80 consecutive 50 ms steps through valid-sensor
Nav2/empty-plan transitions and noisy depth, with no idle command gaps, plus
a controller zero that keeps moving, blocked corridors, sensor loss, blind-ring scans and measured
full-turn exhaustion.
These are software fixtures, not real-floor evidence.

Read-only calibration check on 2026-10-10 against the rig's live drive profile:
0.2 m/s maps to left/right PWM 78/69 with the autonomy cap of 100; gentle
±0.2 rad/s arcs map to 76/74 and 79/64. In-place ±0.82 rad/s maps to ±75/∓60.
All exceed the reported L70/R40 moving floors. The profile/driver still owns
trimming and saturation. No Jetson environment, service or restart is changed
by this implementation. Deployment and real-floor verification belong to the
Nav2 bring-up owner: compare poles/table legs against the broad hallway, record
selected targets, actual wheel movement, release/disconnect/capture-loss stops,
and time spent idle. A green software test does not qualify that floor behavior.

Open-space closed-loop checks (2026-10-10, Docker Jazzy, RPP, ray-cast
`floorplan`): retaining and revalidating the exact committed waypoint through
boundary resampling passed the existing 80% scenario gate with **81.6% coverage
in 33.4 s, 1.856 m traveled, zero collisions, 0.305 m minimum physical clearance
and a 0.61 s longest standstill**. Controller output was 20 Hz median (10.2 Hz
minimum); bridge CPU averaged 8.6% of one core in the container. Two preceding
background-scoring runs ended at 76.6% and 75.1%, both with zero collisions and
0.61 s longest standstills; this remains a single successful final simulation,
not a claim of repeatable floor performance. The initial centroid-only/strict
blind-ring candidate never moved (6.5% coverage). Actual rig behavior remains
unverified, and these numbers do not replace a real-floor receipt.
