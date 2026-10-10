"""nvblox as the optional GPU costmap source; the core bringup includes this when costmap_source:=nvblox.

One Isaac ROS NvbloxNode in a composable container, fed the bridge's depth Image + CameraInfo and the
odom -> camera_optical TF. It publishes the 2D ESDF slice that nvblox_nav2's costmap layer reads
(/nvblox_node/static_map_slice). Nav2 wiring: farsight_nvblox.costmap_params.nvblox_nav2_params().

Arguments:
  depth_image_topic  sensor_msgs/Image 32FC1 metres from the bridge (default depth/image)
  depth_info_topic   matching CameraInfo (default depth/camera_info)
  params_file        nvblox parameter file (default config/nvblox.yaml)
  voxel_size         metres; 0.05 default, 0.08 if RAM or GPU time is tight
  slice_min_height   2D ESDF slice floor in metres above odom Z = 0 (keep above floor noise)
  slice_max_height   2D ESDF slice ceiling (rover body top)
  mesh_rate_hz       0 disables the mesh (default); only the smoke test and viewers want it
  synthetic          true also starts farsight_nvblox synthetic_depth (never with the real bridge)
  use_sim_time       true when replaying a bag with --clock
"""

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.conditions import IfCondition
from launch.substitutions import LaunchConfiguration, PathJoinSubstitution
from launch_ros.actions import ComposableNodeContainer, Node
from launch_ros.descriptions import ComposableNode
from launch_ros.parameter_descriptions import ParameterValue
from launch_ros.substitutions import FindPackageShare


def generate_launch_description():
    share = FindPackageShare("farsight_nvblox")
    cfg = LaunchConfiguration

    def number(name):
        return ParameterValue(cfg(name), value_type=float)

    overrides = {
        "use_sim_time": ParameterValue(cfg("use_sim_time"), value_type=bool),
        "voxel_size": number("voxel_size"),
        "update_mesh_rate_hz": number("mesh_rate_hz"),
        "static_mapper.esdf_slice_min_height": number("slice_min_height"),
        "static_mapper.esdf_slice_max_height": number("slice_max_height"),
    }
    args = [
        DeclareLaunchArgument("depth_image_topic", default_value="depth/image"),
        DeclareLaunchArgument("depth_info_topic", default_value="depth/camera_info"),
        DeclareLaunchArgument("params_file", default_value=PathJoinSubstitution([share, "config", "nvblox.yaml"])),
        DeclareLaunchArgument("voxel_size", default_value="0.05"),
        DeclareLaunchArgument("slice_min_height", default_value="0.06"),
        DeclareLaunchArgument("slice_max_height", default_value="0.25"),
        DeclareLaunchArgument("mesh_rate_hz", default_value="0.0"),
        DeclareLaunchArgument("synthetic", default_value="false"),
        DeclareLaunchArgument("use_sim_time", default_value="false"),
        DeclareLaunchArgument("log_level", default_value="info"),
    ]
    nvblox = ComposableNodeContainer(
        name="nvblox_container", namespace="", package="rclcpp_components", executable="component_container_mt",
        arguments=["--ros-args", "--log-level", cfg("log_level")],
        composable_node_descriptions=[ComposableNode(
            name="nvblox_node", package="nvblox_ros", plugin="nvblox::NvbloxNode",
            parameters=[cfg("params_file"), overrides],
            remappings=[("camera_0/depth/image", cfg("depth_image_topic")),
                        ("camera_0/depth/camera_info", cfg("depth_info_topic"))])],
        output="screen")
    synthetic = Node(package="farsight_nvblox", executable="synthetic_depth", name="synthetic_depth",
                     parameters=[{"use_sim_time": ParameterValue(cfg("use_sim_time"), value_type=bool)}],
                     condition=IfCondition(cfg("synthetic")), output="screen")
    return LaunchDescription([*args, nvblox, synthetic])
