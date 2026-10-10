# farsight_slam

Optional SLAM for Farsight navigation: LiDAR PointCloud2 to scan, then SLAM Toolbox
publishes `map -> odom` and a persistent `/map`. Design, results, and the EKF decision
are in `docs/navigation.md`.

```
ros2 launch farsight_slam slam.launch.py [cloud_topic:=/farsight/depth/points]
    [slam_mode:=mapping|localization] [map_file:=<pose graph>] [start_pose:=x,y,yaw]
    [scan_from_cloud:=true] [use_lifecycle_manager:=false]
```

`farsight_nav`'s `nav.launch.py slam:=true` includes it with the defaults.

## Tests

```
ros2/farsight_slam/test/run_docker.sh     # builds farsight-slam:jazzy, colcon build, pytest (~3 min)
```

- **`test_world.py`:** simulator geometry. Pure numpy; runs without ROS.
- **`test_loop_closure.py`:** runs `launch/sim_loop_test.launch.py` with perfect,
  ARKit-like and heavy odometry drift, then relocalizes in the saved map. It asserts
  drift correction, revisit (loop-closure) edges in the pose graph, map coverage, and
  CPU/RSS. Reports and map PGMs go to `test/results/`.

The simulator (`farsight_slam/loop_sim.py`) publishes what `farsight_nav`'s bridge
publishes:
- `odom -> base_link`, `/odom`
- `base_link -> camera_link -> camera_optical`
- `/farsight/depth/points` in `camera_optical`

It also publishes ground truth on `/sim/ground_truth`. It runs a single scenario:

```
ros2 launch farsight_slam sim_loop_test.launch.py drift:=arkit laps:=2 report_path:=/tmp/r.json
```
