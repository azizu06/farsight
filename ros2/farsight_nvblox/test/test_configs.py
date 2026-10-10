import importlib.util
import pathlib

import pytest
import yaml

PACKAGE = pathlib.Path(__file__).resolve().parents[1]


def flatten(prefix, node):
    for key, value in node.items():
        if isinstance(value, dict):
            yield from flatten(f"{prefix}{key}.", value)
        else:
            yield f"{prefix}{key}", value


def nvblox_params():
    params = yaml.safe_load((PACKAGE / "config" / "nvblox.yaml").read_text())
    return dict(flatten("", params["/**"]["ros__parameters"]))


def test_nvblox_params_are_the_single_camera_static_2d_geometry_only_setup():
    p = nvblox_params()
    assert (p["num_cameras"], p["use_depth"], p["use_color"], p["use_lidar"]) == (1, True, False, False)
    assert (p["mapping_type"], p["esdf_mode"]) == ("static_tsdf", "2d")
    assert p["global_frame"] == "odom" and p["use_tf_transforms"] is True and p["use_topic_transforms"] is False
    assert p["update_mesh_rate_hz"] == 0.0 and 0.03 <= p["voxel_size"] <= 0.1


def test_esdf_slice_sits_between_floor_noise_and_the_rover_top():
    p = nvblox_params()
    low, mid, high = (p[f"static_mapper.esdf_slice_{k}"] for k in ("min_height", "height", "max_height"))
    assert 0.03 <= low < mid < high <= 0.4, "a slice touching the floor turns the room into an obstacle"


def test_launch_accepts_arguments_and_builds_without_the_nvblox_packages():
    if importlib.util.find_spec("launch_ros") is None:
        pytest.skip("ROS 2 launch not installed")
    from launch import LaunchContext
    spec = importlib.util.spec_from_file_location("nvblox_launch", PACKAGE / "launch" / "nvblox.launch.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    description = module.generate_launch_description()
    names = {e.name for e in description.entities if type(e).__name__ == "DeclareLaunchArgument"}
    assert {"depth_image_topic", "depth_info_topic", "params_file", "voxel_size", "slice_min_height",
            "slice_max_height", "mesh_rate_hz", "synthetic", "use_sim_time"} <= names
    assert description.get_launch_arguments_with_include_launch_description_actions()
    assert isinstance(LaunchContext(), LaunchContext)
