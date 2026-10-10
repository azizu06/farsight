"""Optional SLAM for Farsight navigation; the core bringup includes this with slam:=true.

LiDAR PointCloud2 -> pointcloud_to_laserscan -> /scan -> SLAM Toolbox, which
publishes map -> odom (correcting ARKit drift) and a persistent /map.

Arguments:
  cloud_topic      PointCloud2 from the bridge (default /farsight/lidar/points)
  scan_topic       flattened scan (default /scan)
  scan_from_cloud  run pointcloud_to_laserscan here; false if the caller already publishes scan_topic
  slam_mode        mapping (online async) or localization (needs map_file)
  map_file         serialized pose graph to continue from (no extension); '' starts a new map
  slam_params      SLAM Toolbox parameter file
  use_sim_time     true under a simulator publishing /clock
  autostart        configure and activate SLAM Toolbox (a lifecycle node in Jazzy) from this launch
  use_lifecycle_manager  true when a Nav2 lifecycle_manager owns slam_toolbox instead; autostart is then ignored
"""

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, EmitEvent, RegisterEventHandler
from launch.conditions import IfCondition
from launch.events import matches_action
from launch.substitutions import (AndSubstitution, EqualsSubstitution, LaunchConfiguration,
                                  NotSubstitution, PathJoinSubstitution)
from launch_ros.actions import LifecycleNode, Node
from launch_ros.event_handlers import OnStateTransition
from launch_ros.events.lifecycle import ChangeState
from launch_ros.substitutions import FindPackageShare
from lifecycle_msgs.msg import Transition


def lifecycle_autostart(node, condition):
    """Configure, then activate, `node` once launched (what slam_toolbox's own launch does)."""
    def change(transition, **kwargs):
        return EmitEvent(event=ChangeState(lifecycle_node_matcher=matches_action(node),
                                           transition_id=transition), **kwargs)
    configure = change(Transition.TRANSITION_CONFIGURE, condition=condition)
    activate = RegisterEventHandler(
        OnStateTransition(target_lifecycle_node=node, start_state="configuring",
                          goal_state="inactive",
                          entities=[change(Transition.TRANSITION_ACTIVATE)]),
        condition=condition)
    return [configure, activate]


def generate_launch_description():
    share = FindPackageShare("farsight_slam")
    cfg = LaunchConfiguration
    use_sim_time = {"use_sim_time": cfg("use_sim_time")}
    slam_overrides = {
        "use_sim_time": cfg("use_sim_time"),
        "scan_topic": cfg("scan_topic"),
        "base_frame": cfg("base_frame"),
        "odom_frame": cfg("odom_frame"),
        "map_frame": cfg("map_frame"),
        "use_lifecycle_manager": cfg("use_lifecycle_manager"),
    }

    args = [
        DeclareLaunchArgument("cloud_topic", default_value="/farsight/lidar/points"),
        DeclareLaunchArgument("scan_topic", default_value="/scan"),
        DeclareLaunchArgument("scan_from_cloud", default_value="true"),
        DeclareLaunchArgument("slam_mode", default_value="mapping",
                              choices=["mapping", "localization"]),
        DeclareLaunchArgument("map_file", default_value=""),
        DeclareLaunchArgument("base_frame", default_value="base_link"),
        DeclareLaunchArgument("odom_frame", default_value="odom"),
        DeclareLaunchArgument("map_frame", default_value="map"),
        DeclareLaunchArgument("use_sim_time", default_value="false"),
        DeclareLaunchArgument("autostart", default_value="true"),
        DeclareLaunchArgument("use_lifecycle_manager", default_value="false"),
        DeclareLaunchArgument(
            "slam_params",
            default_value=PathJoinSubstitution([share, "config", "slam_toolbox_indoor.yaml"])),
        DeclareLaunchArgument(
            "scan_params",
            default_value=PathJoinSubstitution([share, "config", "pointcloud_to_laserscan.yaml"])),
    ]

    to_scan = Node(
        package="pointcloud_to_laserscan",
        executable="pointcloud_to_laserscan_node",
        name="pointcloud_to_laserscan",
        parameters=[cfg("scan_params"), use_sim_time, {"target_frame": cfg("base_frame")}],
        remappings=[("cloud_in", cfg("cloud_topic")), ("scan", cfg("scan_topic"))],
        condition=IfCondition(cfg("scan_from_cloud")),
        output="screen",
    )

    is_mapping = EqualsSubstitution(cfg("slam_mode"), "mapping")
    is_localization = EqualsSubstitution(cfg("slam_mode"), "localization")
    mapping = LifecycleNode(
        package="slam_toolbox",
        executable="async_slam_toolbox_node",
        name="slam_toolbox",
        namespace="",
        parameters=[cfg("slam_params"), slam_overrides, {"map_file_name": cfg("map_file")}],
        condition=IfCondition(is_mapping),
        output="screen",
    )
    localization = LifecycleNode(
        package="slam_toolbox",
        executable="localization_slam_toolbox_node",
        name="slam_toolbox",
        namespace="",
        parameters=[cfg("slam_params"), slam_overrides,
                    {"mode": "localization", "map_file_name": cfg("map_file"),
                     "map_start_at_dock": True}],
        condition=IfCondition(is_localization),
        output="screen",
    )
    autostart = NotSubstitution(cfg("use_lifecycle_manager"))
    autostart = AndSubstitution(cfg("autostart"), autostart)

    actions = args + [to_scan, mapping, localization]
    actions += lifecycle_autostart(mapping, IfCondition(AndSubstitution(autostart, is_mapping)))
    actions += lifecycle_autostart(
        localization, IfCondition(AndSubstitution(autostart, is_localization)))
    return LaunchDescription(actions)
