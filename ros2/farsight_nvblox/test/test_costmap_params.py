import copy
import pathlib

import pytest
import yaml

from farsight_nvblox.costmap_params import (LAYER_NAME, LAYER_PLUGIN, SLICE_TOPIC, apply_nvblox_layers, main,
                                            nvblox_nav2_params)

BASE = yaml.safe_load("""
local_costmap:
  local_costmap:
    ros__parameters:
      global_frame: odom
      rolling_window: true
      resolution: 0.1
      plugins: [voxel_layer, inflation_layer]
      voxel_layer: {plugin: nav2_costmap_2d::VoxelLayer, enabled: true, observation_sources: pointcloud}
      inflation_layer: {plugin: nav2_costmap_2d::InflationLayer, inflation_radius: 0.5}
global_costmap:
  global_costmap:
    ros__parameters:
      global_frame: map
      plugins: [static_layer, obstacle_layer, inflation_layer]
      static_layer: {plugin: nav2_costmap_2d::StaticLayer}
      obstacle_layer: {plugin: nav2_costmap_2d::ObstacleLayer, observation_sources: pointcloud}
      inflation_layer: {plugin: nav2_costmap_2d::InflationLayer}
controller_server: {ros__parameters: {controller_frequency: 20.0}}
""")


def block(params, name):
    return params[name][name]["ros__parameters"]


def test_obstacle_layers_become_nvblox_in_both_costmaps_and_others_keep_order():
    out = apply_nvblox_layers(BASE)
    assert block(out, "local_costmap")["plugins"] == [LAYER_NAME, "inflation_layer"]
    assert block(out, "global_costmap")["plugins"] == ["static_layer", LAYER_NAME, "inflation_layer"]
    for name, frame in (("local_costmap", "odom"), ("global_costmap", "map")):
        layer = block(out, name)[LAYER_NAME]
        assert layer == dict(plugin=LAYER_PLUGIN, enabled=True, nav2_costmap_global_frame=frame,
                             nvblox_map_slice_topic=SLICE_TOPIC, convert_to_binary_costmap=True)
    assert "voxel_layer" not in block(out, "local_costmap") and "obstacle_layer" not in block(out, "global_costmap")
    assert out["controller_server"] == BASE["controller_server"]


def test_input_is_not_mutated_and_the_result_is_stable_when_applied_twice():
    snapshot = copy.deepcopy(BASE)
    once = apply_nvblox_layers(BASE)
    assert BASE == snapshot
    assert apply_nvblox_layers(once) == once


def test_resolution_follows_the_voxel_size_only_when_given():
    assert block(apply_nvblox_layers(BASE), "local_costmap")["resolution"] == 0.1
    assert block(apply_nvblox_layers(BASE, resolution=0.05), "global_costmap")["resolution"] == 0.05


def test_without_an_obstacle_layer_nvblox_goes_before_inflation():
    base = copy.deepcopy(BASE)
    local = block(base, "local_costmap")
    local["plugins"] = ["inflation_layer"]
    assert block(apply_nvblox_layers(base), "local_costmap")["plugins"] == [LAYER_NAME, "inflation_layer"]


def test_missing_costmap_or_global_frame_is_an_error():
    with pytest.raises(ValueError, match="global_costmap"):
        apply_nvblox_layers({"local_costmap": BASE["local_costmap"]})
    base = copy.deepcopy(BASE)
    del block(base, "local_costmap")["global_frame"]
    with pytest.raises(ValueError, match="global_frame"):
        apply_nvblox_layers(base)


def test_file_round_trip_and_cli(tmp_path, capsys):
    source = tmp_path / "nav2.yaml"
    source.write_text(yaml.safe_dump(BASE))
    out = pathlib.Path(nvblox_nav2_params(str(source)))
    try:
        assert yaml.safe_load(out.read_text()) == apply_nvblox_layers(BASE)
    finally:
        out.unlink()
    target = tmp_path / "out.yaml"
    assert main([str(source), str(target), "--resolution", "0.08"]) == 0
    assert block(yaml.safe_load(target.read_text()), "local_costmap")["resolution"] == 0.08
    assert str(target) in capsys.readouterr().out


def test_real_nav2_default_params_convert_when_nav2_is_installed():
    candidates = [pathlib.Path("/opt/ros/jazzy/share/nav2_bringup/params/nav2_params.yaml")]
    path = next((c for c in candidates if c.exists()), None)
    if path is None:
        pytest.skip("nav2_bringup not installed")
    out = apply_nvblox_layers(yaml.safe_load(path.read_text()))
    for name in ("local_costmap", "global_costmap"):
        plugins = block(out, name)["plugins"]
        assert LAYER_NAME in plugins and plugins[-1] == "inflation_layer"
