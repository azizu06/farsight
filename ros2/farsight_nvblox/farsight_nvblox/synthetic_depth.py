"""Synthetic ARKit-like depth + TF for the nvblox smoke test when no phone or bag is available.

A rover at the origin sways left and right in a 3 m x 3 m room (floor, four walls) with a 0.3 m box ahead, the
phone 15 cm up. Publishes depth/image (32FC1) and depth/camera_info at the packet geometry the real bridge uses
(256x192-ish), plus odom -> base_link and the static base_link -> camera_link -> camera_optical chain, all
stamped from one clock reading. Never run it next to the real bridge: both would publish the same TF.

    ros2 run farsight_nvblox synthetic_depth --ros-args -p rate_hz:=15.0
"""

import math

import numpy as np

# camera_link -> camera_optical (x right, y down, z forward), quaternion x, y, z, w (REP 103).
OPTICAL_QUATERNION = (-0.5, 0.5, -0.5, 0.5)
ROOM = dict(x_max=3.0, y_half=1.5, box=((1.8, 2.1), (-0.2, 0.2), (0.0, 0.3)))


def _hit_planes(origin, direction, room):
    """Smallest positive ray parameter over floor, walls and the box; inf where the ray escapes."""
    inf = np.full(direction.shape[:2], np.inf)
    with np.errstate(divide="ignore", invalid="ignore"):
        best = inf.copy()
        for axis, value in ((2, 0.0), (0, room["x_max"]), (1, room["y_half"]), (1, -room["y_half"])):
            t = (value - origin[axis]) / direction[..., axis]
            best = np.minimum(best, np.where(t > 1e-6, t, np.inf))
        lo = np.array([room["box"][i][0] for i in range(3)])
        hi = np.array([room["box"][i][1] for i in range(3)])
        t1, t2 = (lo - origin) / direction, (hi - origin) / direction
        near = np.minimum(t1, t2).max(axis=-1)
        far = np.maximum(t1, t2).min(axis=-1)
        box = np.where((near <= far) & (far > 1e-6), np.maximum(near, 1e-6), np.inf)
    return np.minimum(best, box)


def render_depth(yaw, *, height=0.15, width=256, rows=192, fx=210.0, fy=210.0, max_range=5.0, room=ROOM):
    """Depth in metres along the optical axis (0 = no return) for a camera at `height` facing `yaw`."""
    cx, cy = width / 2, rows / 2
    u, v = np.meshgrid(np.arange(width) + 0.5, np.arange(rows) + 0.5)
    link = np.stack([np.ones_like(u), -(u - cx) / fx, -(v - cy) / fy], axis=-1)  # x fwd, y left, z up
    c, s = math.cos(yaw), math.sin(yaw)
    rotation = np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]])
    direction = link @ rotation.T
    t = _hit_planes(np.array([0.0, 0.0, height]), direction, room)
    return np.where(np.isfinite(t) & (t >= 0.15) & (t <= max_range), t, 0.0).astype(np.float32)


def main(args=None):
    import rclpy
    from geometry_msgs.msg import TransformStamped
    from rclpy.node import Node
    from tf2_ros import StaticTransformBroadcaster, TransformBroadcaster

    from farsight_nvblox.depth_msgs import DepthPublisher

    class SyntheticDepth(Node):
        def __init__(self):
            super().__init__("synthetic_depth")
            self.rate = self.declare_parameter("rate_hz", 15.0).value
            self.height = self.declare_parameter("camera_height", 0.15).value
            self.sway = self.declare_parameter("sway_rad", 0.5).value
            self.width, self.rows, self.focal = 256, 192, 210.0
            self.depth = DepthPublisher(self)
            self.tf = TransformBroadcaster(self)
            self.static = StaticTransformBroadcaster(self)
            self.start = self.get_clock().now()
            self.static.sendTransform([
                self._transform("base_link", "camera_link", (0.0, 0.0, self.height), (0.0, 0.0, 0.0, 1.0)),
                self._transform("camera_link", "camera_optical", (0.0, 0.0, 0.0), OPTICAL_QUATERNION)])
            self.create_timer(1.0 / self.rate, self.tick)

        def _transform(self, parent, child, translation, quaternion, stamp=None):
            msg = TransformStamped()
            msg.header.stamp = stamp or self.get_clock().now().to_msg()
            msg.header.frame_id, msg.child_frame_id = parent, child
            msg.transform.translation.x, msg.transform.translation.y, msg.transform.translation.z = translation
            (msg.transform.rotation.x, msg.transform.rotation.y,
             msg.transform.rotation.z, msg.transform.rotation.w) = quaternion
            return msg

        def tick(self):
            now = self.get_clock().now()
            seconds = (now - self.start).nanoseconds / 1e9
            yaw = self.sway * math.sin(0.4 * seconds)
            stamp = now.to_msg()
            self.tf.sendTransform(self._transform("odom", "base_link", (0.0, 0.0, 0.0),
                                                  (0.0, 0.0, math.sin(yaw / 2), math.cos(yaw / 2)), stamp))
            header = dict(fx=self.focal, fy=self.focal, cx=self.width / 2, cy=self.rows / 2)
            self.depth.publish(header, render_depth(yaw, height=self.height, width=self.width, rows=self.rows,
                                                    fx=self.focal, fy=self.focal), stamp)

    rclpy.init(args=args)
    node = SyntheticDepth()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == "__main__":
    main()
