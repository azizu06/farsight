"""Swap the pointcloud obstacle layer of a Nav2 params file for nvblox's costmap layer.

The core bringup calls `nvblox_nav2_params(base_yaml)` when costmap_source:=nvblox and hands the returned
file to Nav2; costmap_source:=pointcloud never touches this module. Only the local and global costmaps change:
obstacle/voxel layers are replaced by `nvblox_layer`, other layers (static, inflation) keep their place.

    python3 -m farsight_nvblox.costmap_params base.yaml out.yaml [--resolution 0.05]
"""

import argparse
import copy
import os
import sys
import tempfile

import yaml

COSTMAPS = ("local_costmap", "global_costmap")
OBSTACLE_PLUGINS = {"nav2_costmap_2d::ObstacleLayer", "nav2_costmap_2d::VoxelLayer",
                    "nav2_costmap_2d::RangeSensorLayer"}
LAYER_NAME = "nvblox_layer"
LAYER_PLUGIN = "nvblox::nav2::NvbloxCostmapLayer"
# NVIDIA's Carter example uses combined_map_slice (static + people) on the global costmap; the Nano has no
# people layer, so both costmaps read the static slice.
SLICE_TOPIC = "/nvblox_node/static_map_slice"


def _costmap_block(params, name):
    try:
        block = params[name][name]["ros__parameters"]
    except (KeyError, TypeError):
        raise ValueError(f"Nav2 params have no {name}.{name}.ros__parameters") from None
    if not isinstance(block, dict) or not isinstance(block.get("plugins"), list):
        raise ValueError(f"{name} needs a plugins list")
    return block


def apply_nvblox_layers(params, *, slice_topic=SLICE_TOPIC, resolution=None):
    """Return a copy of Nav2 `params` with `nvblox_layer` in place of the obstacle layers."""
    result = copy.deepcopy(params)
    for name in COSTMAPS:
        block = _costmap_block(result, name)
        plugins, replaced = [], False
        for plugin_name in block["plugins"]:
            config = block.get(plugin_name)
            if isinstance(config, dict) and config.get("plugin") in OBSTACLE_PLUGINS:
                del block[plugin_name]
                if not replaced:
                    plugins.append(LAYER_NAME)
                    replaced = True
            elif plugin_name != LAYER_NAME:
                plugins.append(plugin_name)
        if not replaced:  # no obstacle layer to swap: sit just before inflation, which must come last
            inflation = next((i for i, n in enumerate(plugins) if n.startswith("inflation")), len(plugins))
            plugins.insert(inflation, LAYER_NAME)
        block["plugins"] = plugins
        global_frame = block.get("global_frame")
        if not global_frame:
            raise ValueError(f"{name} needs global_frame (nvblox must publish into the same frame)")
        block[LAYER_NAME] = dict(plugin=LAYER_PLUGIN, enabled=True, nav2_costmap_global_frame=global_frame,
                                 nvblox_map_slice_topic=slice_topic, convert_to_binary_costmap=True)
        if resolution is not None:
            block["resolution"] = float(resolution)
    return result


def nvblox_nav2_params(base_path, out_path=None, **kwargs):
    """Write the nvblox variant of the Nav2 params file at `base_path`; returns its path."""
    with open(base_path) as handle:
        params = yaml.safe_load(handle)
    result = apply_nvblox_layers(params, **kwargs)
    if out_path is None:
        descriptor, out_path = tempfile.mkstemp(prefix="nav2_nvblox_", suffix=".yaml")
        os.close(descriptor)
    with open(out_path, "w") as handle:
        yaml.safe_dump(result, handle, sort_keys=False)
    return out_path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("base")
    parser.add_argument("out")
    parser.add_argument("--resolution", type=float, help="costmap resolution in metres (match nvblox voxel_size)")
    parser.add_argument("--slice-topic", default=SLICE_TOPIC)
    args = parser.parse_args(argv)
    print(nvblox_nav2_params(args.base, args.out, slice_topic=args.slice_topic, resolution=args.resolution))
    return 0


if __name__ == "__main__":
    sys.exit(main())
