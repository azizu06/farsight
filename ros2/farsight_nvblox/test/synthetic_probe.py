"""Run against a live `synthetic_depth` node: depth/camera_info rate, image content and the odom -> camera_optical TF."""

import sys
import time

import numpy as np
import rclpy
from rclpy.duration import Duration
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from rclpy.time import Time
from sensor_msgs.msg import CameraInfo, Image
from tf2_ros import Buffer, TransformListener


def main():
    rclpy.init()
    node = Node("synthetic_probe")
    images, infos = [], []
    node.create_subscription(Image, "depth/image", images.append, qos_profile_sensor_data)
    node.create_subscription(CameraInfo, "depth/camera_info", infos.append, qos_profile_sensor_data)
    buffer = Buffer()
    TransformListener(buffer, node)
    end = time.time() + 6
    while time.time() < end:
        rclpy.spin_once(node, timeout_sec=0.1)
    problems = []
    rate = len(images) / 6.0
    if rate < 10:
        problems.append(f"depth/image rate {rate:.1f} Hz, expected about 15")
    if not infos or infos[-1].header.frame_id != "camera_optical" or list(infos[-1].k)[0] <= 0:
        problems.append("depth/camera_info missing or malformed")
    if images:
        image = images[-1]
        depth = np.frombuffer(bytes(image.data), "<f4").reshape(image.height, image.width)
        if image.encoding != "32FC1" or image.header.frame_id != "camera_optical" or not (depth > 0).any():
            problems.append("depth/image is not a populated 32FC1 camera_optical image")
        try:
            tf = buffer.lookup_transform("odom", "camera_optical", Time.from_msg(image.header.stamp),
                                         Duration(seconds=1.0))
            if abs(tf.transform.translation.z - 0.15) > 1e-3:
                problems.append(f"camera height {tf.transform.translation.z:.3f} m, expected 0.15")
        except Exception as error:  # noqa: BLE001 - report any lookup failure as a probe failure
            problems.append(f"no odom -> camera_optical TF at the image stamp: {error}")
    print(f"depth/image {rate:.1f} Hz, camera_info {len(infos)} msgs, problems: {problems or 'none'}")
    rclpy.shutdown()
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
