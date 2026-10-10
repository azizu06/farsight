"""Farsight navigation: bridge to jetson.server + Nav2 (no map server, no AMCL, no collision monitor).

ros2 launch farsight_nav nav.launch.py [server:=http://127.0.0.1:8443] [controller:=mppi|rpp]
    [slam:=false|true] [costmap_source:=pointcloud|nvblox] [explore:=true|false] [params_file:=...]

slam:=true includes farsight_slam's launch (SLAM Toolbox publishes map->odom); otherwise the
bridge publishes map->odom as identity and ARKit odometry is the only localization.
costmap_source:=nvblox overlays config/nvblox_overlay.yaml (an nvblox costmap layer instead of
the point-cloud voxel/obstacle layers); the nvblox node itself is launched by its own package.
"""

import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, GroupAction, IncludeLaunchDescription, OpaqueFunction
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node, SetParameter
from launch_ros.descriptions import ParameterFile
from nav2_common.launch import RewrittenYaml

NAV2_NODES = ("controller_server", "planner_server", "behavior_server", "bt_navigator")


def nav_nodes(context):
    share = get_package_share_directory("farsight_nav")
    params = LaunchConfiguration("params_file").perform(context) or os.path.join(share, "config", "nav2_params.yaml")
    bt_xml = os.path.join(share, "behavior_trees", "navigate_farsight.xml")
    configured = ParameterFile(RewrittenYaml(source_file=params, root_key="",
                                             param_rewrites={"default_nav_to_pose_bt_xml": bt_xml},
                                             convert_types=True), allow_substs=True)
    files = [configured]
    if LaunchConfiguration("costmap_source").perform(context) == "nvblox":
        files.append(os.path.join(share, "config", "nvblox_overlay.yaml"))
    elif LaunchConfiguration("costmap_source").perform(context) != "pointcloud":
        raise RuntimeError("costmap_source must be pointcloud or nvblox")
    controller = LaunchConfiguration("controller").perform(context).lower()
    if controller not in ("mppi", "rpp"):
        raise RuntimeError("controller must be mppi or rpp")
    slam = LaunchConfiguration("slam").perform(context).lower() in ("1", "true", "yes")
    remappings = [("/tf", "tf"), ("/tf_static", "tf_static")]
    log = LaunchConfiguration("log_level")
    nodes = [
        Node(package="nav2_controller", executable="controller_server", name="controller_server", output="screen",
             parameters=files, remappings=remappings, arguments=["--ros-args", "--log-level", log]),
        Node(package="nav2_planner", executable="planner_server", name="planner_server", output="screen",
             parameters=files, remappings=remappings, arguments=["--ros-args", "--log-level", log]),
        Node(package="nav2_behaviors", executable="behavior_server", name="behavior_server", output="screen",
             parameters=files, remappings=remappings, arguments=["--ros-args", "--log-level", log]),
        Node(package="nav2_bt_navigator", executable="bt_navigator", name="bt_navigator", output="screen",
             parameters=files, remappings=remappings, arguments=["--ros-args", "--log-level", log]),
        Node(package="nav2_lifecycle_manager", executable="lifecycle_manager", name="lifecycle_manager_navigation",
             output="screen", parameters=[{"autostart": True, "bond_timeout": 4.0, "node_names": list(NAV2_NODES)}]),
        Node(package="farsight_nav", executable="bridge", name="farsight_bridge", output="screen",
             parameters=[{"server": LaunchConfiguration("server"), "publish_map_odom": not slam,
                          "controller": controller.upper()}],
             remappings=remappings),
    ]
    if slam:
        try:
            slam_share = get_package_share_directory("farsight_slam")
        except Exception as exc:
            raise RuntimeError("slam:=true needs the farsight_slam package built in this workspace") from exc
        nodes.append(IncludeLaunchDescription(PythonLaunchDescriptionSource(
            os.path.join(slam_share, "launch", "slam.launch.py"))))
    return nodes


def generate_launch_description():
    return LaunchDescription([
        DeclareLaunchArgument("server", default_value=os.environ.get("FARSIGHT_NAV_SERVER", "http://127.0.0.1:8443"),
                              description="jetson.server origin (loopback)"),
        DeclareLaunchArgument("controller", default_value="mppi", description="mppi or rpp"),
        DeclareLaunchArgument("slam", default_value="false", description="Include farsight_slam (map->odom)"),
        DeclareLaunchArgument("costmap_source", default_value="pointcloud", description="pointcloud or nvblox"),
        DeclareLaunchArgument("explore", default_value="true", description="Run the frontier explorer"),
        DeclareLaunchArgument("params_file", default_value="", description="Override config/nav2_params.yaml"),
        DeclareLaunchArgument("use_sim_time", default_value="false"),
        DeclareLaunchArgument("log_level", default_value="info"),
        SetParameter("use_sim_time", LaunchConfiguration("use_sim_time")),
        GroupAction([OpaqueFunction(function=nav_nodes)]),
    ])
