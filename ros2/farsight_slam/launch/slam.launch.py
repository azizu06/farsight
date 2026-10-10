"""Optional SLAM for Farsight navigation; the core bringup includes this with slam:=true.

LiDAR PointCloud2 -> pointcloud_to_laserscan -> /scan -> SLAM Toolbox, which
publishes map -> odom (correcting ARKit drift) and a persistent /map.

Arguments:
  cloud_topic      PointCloud2 from farsight_nav's bridge (default /farsight/depth/points)
  scan_topic       flattened scan (default /scan)
  scan_from_cloud  run pointcloud_to_laserscan here; false if the caller already publishes scan_topic
  slam_mode        mapping (online async) or localization (needs map_file)
  map_file         serialized pose graph (path without extension); mapping continues it,
                   localization localizes in it without adding to it
  start_pose       "x,y,yaw" of base_link in the saved map's frame when map_file is set;
                   empty waits for /initialpose (RViz "2D Pose Estimate")
  slam_params      SLAM Toolbox parameter file
  use_sim_time     true under a simulator publishing /clock
  autostart        configure and activate SLAM Toolbox (a lifecycle node in Jazzy) from this launch
  use_lifecycle_manager  true when a Nav2 lifecycle_manager owns slam_toolbox; autostart is then ignored
"""

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, EmitEvent, OpaqueFunction, RegisterEventHandler
from launch.events import matches_action
from launch.substitutions import LaunchConfiguration, PathJoinSubstitution
from launch_ros.actions import LifecycleNode, Node
from launch_ros.event_handlers import OnStateTransition
from launch_ros.events.lifecycle import ChangeState
from launch_ros.substitutions import FindPackageShare
from lifecycle_msgs.msg import Transition

EXECUTABLE = {"mapping": "async_slam_toolbox_node", "localization": "localization_slam_toolbox_node"}


def truthy(value):
    return value.strip().lower() in ("1", "true", "yes", "on")


def lifecycle_autostart(node):
    """Configure, then activate, `node` once launched (what slam_toolbox's own launch does)."""
    def change(transition):
        return EmitEvent(event=ChangeState(lifecycle_node_matcher=matches_action(node),
                                           transition_id=transition))
    activate = RegisterEventHandler(OnStateTransition(
        target_lifecycle_node=node, start_state="configuring", goal_state="inactive",
        entities=[change(Transition.TRANSITION_ACTIVATE)]))
    return [change(Transition.TRANSITION_CONFIGURE), activate]


def slam_nodes(context):
    def arg(name):
        return LaunchConfiguration(name).perform(context)

    mode = arg("slam_mode")
    use_sim_time = truthy(arg("use_sim_time"))
    managed = truthy(arg("use_lifecycle_manager"))
    actions = []

    if truthy(arg("scan_from_cloud")):
        actions.append(Node(
            package="pointcloud_to_laserscan",
            executable="pointcloud_to_laserscan_node",
            name="pointcloud_to_laserscan",
            parameters=[arg("scan_params"),
                        {"use_sim_time": use_sim_time, "target_frame": arg("base_frame")}],
            remappings=[("cloud_in", arg("cloud_topic")), ("scan", arg("scan_topic"))],
            output="screen",
        ))

    overrides = {
        "use_sim_time": use_sim_time,
        "use_lifecycle_manager": managed,
        "mode": mode,
        "scan_topic": arg("scan_topic"),
        "base_frame": arg("base_frame"),
        "odom_frame": arg("odom_frame"),
        "map_frame": arg("map_frame"),
    }
    if arg("map_file"):
        overrides["map_file_name"] = arg("map_file")
        if arg("start_pose"):
            overrides["map_start_pose"] = [float(v) for v in arg("start_pose").split(",")]
        elif mode == "mapping":
            overrides["map_start_at_dock"] = True
    elif mode == "localization":
        raise RuntimeError("slam_mode:=localization needs map_file:=<serialized pose graph>")

    slam = LifecycleNode(
        package="slam_toolbox",
        executable=EXECUTABLE[mode],
        name="slam_toolbox",
        namespace="",
        parameters=[arg("slam_params"), overrides],
        output="screen",
    )
    actions.append(slam)
    if truthy(arg("autostart")) and not managed:
        actions += lifecycle_autostart(slam)
    return actions


def generate_launch_description():
    share = FindPackageShare("farsight_slam")
    return LaunchDescription([
        DeclareLaunchArgument("cloud_topic", default_value="/farsight/depth/points"),
        DeclareLaunchArgument("scan_topic", default_value="/scan"),
        DeclareLaunchArgument("scan_from_cloud", default_value="true"),
        DeclareLaunchArgument("slam_mode", default_value="mapping", choices=list(EXECUTABLE)),
        DeclareLaunchArgument("map_file", default_value=""),
        DeclareLaunchArgument("start_pose", default_value=""),
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
        OpaqueFunction(function=slam_nodes),
    ])
