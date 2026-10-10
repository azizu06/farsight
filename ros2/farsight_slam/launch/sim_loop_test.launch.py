"""Loop-closure test: loop sim + slam.launch.py + evaluator; exits when the report is written.

  ros2 launch farsight_slam sim_loop_test.launch.py drift:=heavy report_path:=/tmp/r.json

drift: none | arkit | heavy (see DRIFT below). Every slam.launch.py argument passes
through, e.g. slam_mode:=localization map_file:=/tmp/loop to test relocalization.
"""

from launch import LaunchDescription
from launch.actions import (DeclareLaunchArgument, EmitEvent, IncludeLaunchDescription,
                            OpaqueFunction, RegisterEventHandler)
from launch.event_handlers import OnProcessExit
from launch.events import Shutdown
from launch.substitutions import LaunchConfiguration, PathJoinSubstitution
from launch_ros.actions import Node
from launch_ros.substitutions import FindPackageShare

# Odometry error models. arkit is roughly what ARKit VIO does on a featureless
# indoor loop (~1-2 % of distance); heavy is a bad-tracking case that must be corrected.
DRIFT = {
    "none": {"scale_error": 0.0, "yaw_drift_per_m": 0.0, "yaw_noise": 0.0},
    "arkit": {"scale_error": 0.01, "yaw_drift_per_m": 0.004, "yaw_noise": 0.0005},
    "heavy": {"scale_error": 0.03, "yaw_drift_per_m": 0.015, "yaw_noise": 0.001},
}


def nodes(context):
    drift = DRIFT[LaunchConfiguration("drift").perform(context)]
    laps = float(LaunchConfiguration("laps").perform(context))
    odom_at_origin = LaunchConfiguration("odom_at_origin").perform(context).lower() == "true"
    sim = Node(package="farsight_slam", executable="loop_sim", name="farsight_loop_sim",
               parameters=[drift, {"laps": laps, "odom_at_origin": odom_at_origin}],
               output="screen")
    evaluator = Node(
        package="farsight_slam", executable="slam_eval", name="farsight_slam_eval",
        parameters=[{"report_path": LaunchConfiguration("report_path"),
                     "serialize_to": LaunchConfiguration("serialize_to"),
                     "label": LaunchConfiguration("drift")}],
        output="screen")
    stop = RegisterEventHandler(OnProcessExit(target_action=evaluator,
                                              on_exit=[EmitEvent(event=Shutdown())]))
    return [sim, evaluator, stop]


def generate_launch_description():
    slam = IncludeLaunchDescription(
        PathJoinSubstitution([FindPackageShare("farsight_slam"), "launch", "slam.launch.py"]))
    return LaunchDescription([
        DeclareLaunchArgument("drift", default_value="heavy", choices=list(DRIFT)),
        DeclareLaunchArgument("laps", default_value="2.0"),
        # true starts odometry at (0, 0, 0) like a new ARKit session; false starts it at the truth pose.
        DeclareLaunchArgument("odom_at_origin", default_value="false"),
        DeclareLaunchArgument("report_path", default_value="/tmp/slam_report.json"),
        # Pose graph to save after the run (no extension); reload with slam_mode:=localization map_file:=...
        DeclareLaunchArgument("serialize_to", default_value=""),
        slam,
        OpaqueFunction(function=nodes),
    ])
