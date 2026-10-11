"""ROS-only crowd layer/path regression; synthetic floor, no server or motors.

After colcon build/source, in an isolated ROS domain:
ROS_DOMAIN_ID=91 python3 ros2/tests/crowds.py --out /out/crowds
Uses the checked-in costmap people/inflation settings, in both real Nav2 maps.
Only the measured input layer is replaced by a small synthetic floor map.
"""

import argparse
import array
import copy
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time

import numpy as np
import rclpy
from rclpy.action import ActionClient
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
import yaml

from geometry_msgs.msg import TransformStamped
from lifecycle_msgs.msg import Transition
from lifecycle_msgs.srv import ChangeState
from nav2_msgs.action import ComputePathToPose
from nav2_msgs.msg import CostmapFilterInfo
from nav_msgs.msg import OccupancyGrid
from tf2_ros import StaticTransformBroadcaster

from farsight_nav import crowds


class Probe(Node):
    def __init__(self):
        super().__init__("crowd_layer_probe")
        qos = QoSProfile(depth=1, reliability=ReliabilityPolicy.RELIABLE,
                         durability=DurabilityPolicy.TRANSIENT_LOCAL)
        self.floor_pub = self.create_publisher(OccupancyGrid, "/crowd_test/floor", qos)
        self.people_pub = self.create_publisher(OccupancyGrid, "/farsight/people/map", qos)
        self.info_pub = self.create_publisher(CostmapFilterInfo, "/farsight/people/filter_info", qos)
        info = CostmapFilterInfo()
        info.header.frame_id, info.header.stamp = "map", self.get_clock().now().to_msg()
        info.type, info.base, info.multiplier = 0, 0., 1.
        info.filter_mask_topic = "/farsight/people/map"
        self.info_pub.publish(info)
        self.maps = {}
        for name in ("local_costmap", "global_costmap"):
            self.create_subscription(OccupancyGrid, f"/{name}/costmap",
                                     lambda message, name=name: self.maps.__setitem__(name, message), qos)
        self.tf = StaticTransformBroadcaster(self)
        transforms = []
        for parent, child, x in (("map", "odom", 0.), ("odom", "base_link", -2.)):
            t = TransformStamped()
            t.header.stamp, t.header.frame_id, t.child_frame_id = self.get_clock().now().to_msg(), parent, child
            t.transform.translation.x, t.transform.rotation.w = x, 1.
            transforms.append(t)
        self.tf.sendTransform(transforms)
        self.planner = ActionClient(self, ComputePathToPose, "/compute_path_to_pose")

    def wait(self, condition, timeout=15):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            rclpy.spin_once(self, timeout_sec=.05)
            if condition():
                return
        raise AssertionError("crowd probe timed out")

    def grid(self, data):
        message = OccupancyGrid()
        message.header.stamp, message.header.frame_id = self.get_clock().now().to_msg(), "map"
        message.info.width = message.info.height = 120
        message.info.resolution = .05
        message.info.origin.position.x = message.info.origin.position.y = -3.
        message.info.origin.orientation.w = 1.
        message.data = array.array("b", np.asarray(data, np.int8).tobytes())
        return message

    def people(self, points):
        data = np.full((120, 120), -1, np.int8)
        data[crowds.rasterize(points, data.shape, .05, (-3., -3.))] = 100
        self.people_pub.publish(self.grid(data))

    def at(self, name, x, y):
        message = self.maps.get(name)
        if message is None:
            return None
        info = message.info
        col = int(np.floor((x - info.origin.position.x) / info.resolution))
        row = int(np.floor((y - info.origin.position.y) / info.resolution))
        if not (0 <= row < info.height and 0 <= col < info.width):
            return None
        return message.data[row * info.width + col]

    def activate(self, name):
        client = self.create_client(ChangeState, f"/{name}/change_state")
        self.wait(lambda: client.service_is_ready())
        for transition in (Transition.TRANSITION_CONFIGURE, Transition.TRANSITION_ACTIVATE):
            request = ChangeState.Request()
            request.transition.id = transition
            future = client.call_async(request)
            self.wait(future.done)
            assert future.result().success, f"{name} lifecycle {transition} failed"

    def path(self):
        self.wait(self.planner.server_is_ready)
        goal = ComputePathToPose.Goal()
        for pose, x in ((goal.start, -2.), (goal.goal, 2.)):
            pose.header.frame_id, pose.header.stamp = "map", self.get_clock().now().to_msg()
            pose.pose.position.x, pose.pose.orientation.w = x, 1.
        goal.use_start = True
        future = self.planner.send_goal_async(goal)
        self.wait(future.done)
        handle = future.result()
        assert handle.accepted
        future = handle.get_result_async()
        self.wait(future.done)
        result = future.result().result
        assert result.error_code == 0, str(result)
        return [(p.pose.position.x, p.pose.position.y) for p in result.path.poses]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=Path("/tmp/farsight-crowds"))
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    source = Path(__file__).resolve().parents[1] / "farsight_nav/config/nav2_params.yaml"
    params = copy.deepcopy(yaml.safe_load(source.read_text()))
    for name in ("local_costmap", "global_costmap"):
        costmap = params[name][name]["ros__parameters"]
        assert costmap["people_layer"]["plugin"] == "nav2_costmap_2d::KeepoutFilter"
        # Only the synthetic floor uses StaticLayer/max; production measured
        # layers retain their own geometry and original parent settings.
        costmap["use_maximum"] = True
        costmap["plugins"] = ["test_floor", "people_layer", "inflation_layer"]
        costmap["test_floor"] = dict(plugin="nav2_costmap_2d::StaticLayer", map_topic="/crowd_test/floor",
                                      map_subscribe_transient_local=True)
    params["global_costmap"]["global_costmap"]["ros__parameters"].update(
        width=6, height=6, origin_x=-3., origin_y=-3.)
    processes, logs = [], []
    rclpy.init()
    node = Probe()
    try:
        floor = np.zeros((120, 120), np.int8)
        floor[5, 5], floor[110, 110] = -1, 100  # unknown corner and measured obstacle
        centres = [(-.4, -.4), (.4, -.4), (.4, .4), (-.4, .4)]
        for x, y in centres:
            for dx in (-.1, .1):  # sparse feet leave a centre corridor in the measured input
                floor[int((y + 3) / .05), int((x + dx + 3) / .05)] = 100
        node.floor_pub.publish(node.grid(floor))
        node.people([])  # latched bootstrap BEFORE lifecycle activation
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml") as file:
            yaml.safe_dump(params, file)
            file.flush()
            for package, executable in (("nav2_planner", "planner_server"), ("nav2_controller", "controller_server")):
                log = (args.out / f"{executable}.log").open("w")
                logs.append(log)
                processes.append(subprocess.Popen(["ros2", "run", package, executable, "--ros-args",
                                                    "--params-file", file.name], stdout=log, stderr=log,
                                                   start_new_session=True))
            node.activate("planner_server")
            node.activate("controller_server")
            node.wait(lambda: node.at("global_costmap", 0, 0) == 0 and node.at("local_costmap", 0, 0) == 0)
            baseline = node.path()
            assert max(abs(y) for x, y in baseline) < .25
            node.people(centres)
            node.wait(lambda: all(node.at(name, 0, 0) == 100 for name in ("global_costmap", "local_costmap")))
            detour = node.path()
            assert max(abs(y) for x, y in detour) > .7
            mask = crowds.rasterize(centres, floor.shape, .05, (-3., -3.))
            assert all(not mask[int((y + 3) / .05), int((x + 3) / .05)] for x, y in detour)
            assert node.at("global_costmap", -2.725, -2.725) == -1
            assert node.at("global_costmap", 2.525, 2.525) == 100
            node.people([(x, y + 2) for x, y in centres])
            node.wait(lambda: node.at("global_costmap", 0, 2) == 100 and
                      all(node.at(name, 0, 0) == 0 for name in ("global_costmap", "local_costmap")))
            vacated = node.path()
            assert max(abs(y) for x, y in vacated) < .25
            node.people([(-.4, -1.5), (.4, -1.5), (-.4, 1.5), (.4, 1.5)])
            node.wait(lambda: all(node.at("global_costmap", 0, y) == 100 for y in (-1.5, 1.5)) and
                      node.at("global_costmap", 0, 0) == 0)
            aisle = node.path()
            assert max(abs(y) for x, y in aisle) < .25
            node.people([])
            node.wait(lambda: all(node.at("global_costmap", 0, y) == 0 for y in (-1.5, 1.5)))
            report = dict(source="synthetic ROS/Nav2 fixture, no motor commands", result="PASS",
                          paths=dict(feet_only=baseline, crowd_detour=detour, vacated=vacated, wide_aisle=aisle),
                          local_and_global_crowd=True, unknown_preserved=True, measured_obstacle_preserved=True)
            (args.out / "report.json").write_text(json.dumps(report, indent=2))
            print(json.dumps({k: v for k, v in report.items() if k != "paths"}))
    finally:
        for process in processes:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGINT)
        for process in processes:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
        for log in logs:
            log.close()
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
