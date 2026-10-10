"""Smoke: jetson.server --demo --nav2 and nav.launch.py come up together in the container.

Proves the bridge connects, Nav2's lifecycle reaches active, ARKit-style pose becomes /odom and TF,
depth becomes PointCloud2/Image/CameraInfo, and the local costmap is published. Software only.
"""

import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(__file__))
from harness import Stack, main  # noqa: E402


def topic_once(topic, timeout=20):
    result = subprocess.run(["timeout", str(timeout), "ros2", "topic", "echo", "--once", "--no-arr", topic],
                            capture_output=True, text=True)
    if result.returncode != 0:
        raise AssertionError(f"no message on {topic}: {result.stderr.strip()[-200:]}")
    return result.stdout


def bringup():
    with Stack("smoke", ["--demo", "--simulate-rover", "--proximity-stop", "off"]) as stack:
        stack.nav_ready()
        expected = {"/odom", "/farsight/depth/points", "/farsight/depth/image", "/farsight/depth/camera_info",
                    "/farsight/range", "/local_costmap/costmap", "/global_costmap/costmap", "/cmd_vel"}
        listed = set()

        def all_listed():
            # A fresh ros2 daemon can list before discovery has finished: poll, do not sample once.
            listed.update(subprocess.run(["ros2", "topic", "list"], capture_output=True, text=True).stdout.split())
            return expected <= listed

        try:
            stack.wait(all_listed, 20, "topics never appeared")
        except AssertionError as exc:
            raise AssertionError(f"{exc}; missing {sorted(expected - listed)}") from None
        topic_once("/odom")
        topic_once("/local_costmap/costmap")
        info = topic_once("/farsight/depth/camera_info")
        if "camera_optical" not in info:
            raise AssertionError("camera_info is not in camera_optical")
        tf = subprocess.run(["timeout", "10", "ros2", "run", "tf2_ros", "tf2_echo", "map", "camera_optical"],
                            capture_output=True, text=True).stdout
        if "Translation" not in tf:
            raise AssertionError("TF map -> camera_optical unavailable")
        status = stack.get("/nav/status")
        if status["backend"] != "nav2":
            raise AssertionError(f"backend {status['backend']}")
        return f"Nav2 active; bridge {status['detail']['bridge_hello']}; topics and TF chain present"


if __name__ == "__main__":
    main([("bringup", bringup)])
