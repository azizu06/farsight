"""Minimal ROS 2 rover simulator for the SLAM loop test.

Drives a unicycle around `world.loop_route()` and publishes what the Farsight
bridge publishes on the rover, so the SLAM launch runs unchanged:

- `odom -> base_link` TF and `/odom` from ARKit-like *drifting* odometry,
- a static `base_link -> camera_link` TF,
- `PointCloud2` in `camera_link` from a 62 x 48 degree iPhone-like LiDAR cone,
- `/sim/ground_truth` (nav_msgs/Odometry, frame `world`) for evaluation,
- `/sim/done` (std_msgs/Bool) after the requested number of laps.

It is not a physics sim: walls do not stop the rover. It exists to produce
repeatable odometry drift and depth so SLAM drift correction can be measured.
"""

import math

import numpy as np
import rclpy
from geometry_msgs.msg import TransformStamped
from nav_msgs.msg import Odometry
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile
from sensor_msgs.msg import PointCloud2, PointField
from std_msgs.msg import Bool, Header
from tf2_ros import StaticTransformBroadcaster, TransformBroadcaster

from farsight_slam import world as w

CAMERA_HEIGHT = 0.20     # camera_link above base_link (m)
CAMERA_FORWARD = 0.10    # camera_link ahead of base_link (m)


def yaw_quaternion(yaw):
    return 0.0, 0.0, math.sin(yaw / 2), math.cos(yaw / 2)


class LoopSim(Node):
    def __init__(self):
        super().__init__("farsight_loop_sim")
        p = self.declare_parameter
        self.laps = p("laps", 2.0).value
        self.speed = p("speed", 0.35).value
        self.rate = p("rate_hz", 50.0).value
        self.cloud_rate = p("cloud_rate_hz", 10.0).value
        self.cloud_topic = p("cloud_topic", "/farsight/lidar/points").value
        self.cols = p("lidar_cols", 160).value
        self.rows = p("lidar_rows", 12).value
        self.range_noise = p("range_noise", 0.01).value
        self.odom_frame = p("odom_frame", "odom").value
        self.base_frame = p("base_frame", "base_link").value
        self.camera_frame = p("camera_frame", "camera_link").value
        self.startup_delay = p("startup_delay_s", 3.0).value
        self.odom = w.DriftingOdometry(
            scale_error=p("scale_error", 0.0).value,
            yaw_drift_per_m=p("yaw_drift_per_m", 0.0).value,
            yaw_noise=p("yaw_noise", 0.0).value,
        )

        self.world = w.loop_world()
        self.route = w.loop_route()
        self.pilot = w.PurePursuit(self.route, self.speed)
        self.x, self.y = self.route[-1]
        nx, ny = self.route[0]
        self.yaw = math.atan2(ny - self.y, nx - self.x)
        self.odom.reset(self.x, self.y, self.yaw)
        self.goal_distance = self.laps * w.route_length(self.route)
        self.travelled = 0.0
        self.rng = np.random.default_rng(11)
        self.done = False

        # Fixed LiDAR ray fan in the camera frame.
        az = np.linspace(-w.LIDAR_HFOV / 2, w.LIDAR_HFOV / 2, self.cols)
        el = np.linspace(-w.LIDAR_VFOV / 2, w.LIDAR_VFOV / 2, self.rows)
        self.ray_az, self.ray_el = (a.ravel() for a in np.meshgrid(az, el))

        self.tf = TransformBroadcaster(self)
        self.static_tf = StaticTransformBroadcaster(self)
        self.odom_pub = self.create_publisher(Odometry, "/odom", 20)
        self.truth_pub = self.create_publisher(Odometry, "/sim/ground_truth", 20)
        self.cloud_pub = self.create_publisher(PointCloud2, self.cloud_topic, 5)
        latched = QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL)
        self.done_pub = self.create_publisher(Bool, "/sim/done", latched)

        cam = TransformStamped()
        cam.header.stamp = self.get_clock().now().to_msg()
        cam.header.frame_id = self.base_frame
        cam.child_frame_id = self.camera_frame
        cam.transform.translation.x = CAMERA_FORWARD
        cam.transform.translation.z = CAMERA_HEIGHT
        cam.transform.rotation.w = 1.0
        self.static_tf.sendTransform(cam)

        self.start = self.get_clock().now()
        self.ticks = 0
        self.cloud_every = max(1, round(self.rate / self.cloud_rate))
        self.create_timer(1.0 / self.rate, self.tick)
        self.get_logger().info(
            f"loop sim: {self.laps} laps of {w.route_length(self.route):.1f} m at {self.speed} m/s, "
            f"cloud on {self.cloud_topic}")

    def tick(self):
        now = self.get_clock().now()
        dt = 1.0 / self.rate
        moving = (now - self.start).nanoseconds * 1e-9 > self.startup_delay and not self.done
        if moving:
            v, omega = self.pilot.command(self.x, self.y, self.yaw)
            ds, dyaw = v * dt, omega * dt
            self.yaw = w.wrap(self.yaw + dyaw)
            self.x += ds * math.cos(self.yaw)
            self.y += ds * math.sin(self.yaw)
            self.odom.step(ds, dyaw)
            self.travelled += ds
            if self.travelled >= self.goal_distance:
                self.done = True
                self.done_pub.publish(Bool(data=True))
                self.get_logger().info(f"loop sim done after {self.travelled:.1f} m")
        else:
            v = omega = 0.0

        stamp = now.to_msg()
        self.publish_odom(stamp, v, omega)
        self.publish_truth(stamp)
        self.ticks += 1
        if self.ticks % self.cloud_every == 0:
            self.publish_cloud(stamp)

    def publish_odom(self, stamp, v, omega):
        q = yaw_quaternion(self.odom.yaw)
        t = TransformStamped()
        t.header.stamp = stamp
        t.header.frame_id = self.odom_frame
        t.child_frame_id = self.base_frame
        t.transform.translation.x = self.odom.x
        t.transform.translation.y = self.odom.y
        (t.transform.rotation.x, t.transform.rotation.y,
         t.transform.rotation.z, t.transform.rotation.w) = q
        self.tf.sendTransform(t)

        msg = Odometry()
        msg.header.stamp = stamp
        msg.header.frame_id = self.odom_frame
        msg.child_frame_id = self.base_frame
        msg.pose.pose.position.x = self.odom.x
        msg.pose.pose.position.y = self.odom.y
        (msg.pose.pose.orientation.x, msg.pose.pose.orientation.y,
         msg.pose.pose.orientation.z, msg.pose.pose.orientation.w) = q
        msg.twist.twist.linear.x = v
        msg.twist.twist.angular.z = omega
        self.odom_pub.publish(msg)

    def publish_truth(self, stamp):
        msg = Odometry()
        msg.header.stamp = stamp
        msg.header.frame_id = "world"
        msg.child_frame_id = "truth_base_link"
        msg.pose.pose.position.x = self.x
        msg.pose.pose.position.y = self.y
        (msg.pose.pose.orientation.x, msg.pose.pose.orientation.y,
         msg.pose.pose.orientation.z, msg.pose.pose.orientation.w) = yaw_quaternion(self.yaw)
        self.truth_pub.publish(msg)

    def publish_cloud(self, stamp):
        # Sensor origin and rays in the world, from the TRUE pose.
        cx = self.x + CAMERA_FORWARD * math.cos(self.yaw)
        cy = self.y + CAMERA_FORWARD * math.sin(self.yaw)
        ranges = self.world.raycast(cx, cy, CAMERA_HEIGHT, self.yaw + self.ray_az, self.ray_el)
        ok = np.isfinite(ranges)
        r = ranges[ok]
        r = r + self.rng.normal(0.0, self.range_noise + 0.01 * r)
        az, el = self.ray_az[ok], self.ray_el[ok]
        # Points in camera_link (x forward, y left, z up).
        pts = np.stack([r * np.cos(el) * np.cos(az), r * np.cos(el) * np.sin(az), r * np.sin(el)],
                       axis=1).astype(np.float32)
        msg = PointCloud2()
        msg.header = Header(stamp=stamp, frame_id=self.camera_frame)
        msg.height = 1
        msg.width = len(pts)
        msg.fields = [PointField(name=n, offset=4 * i, datatype=PointField.FLOAT32, count=1)
                      for i, n in enumerate("xyz")]
        msg.is_bigendian = False
        msg.point_step = 12
        msg.row_step = 12 * len(pts)
        msg.is_dense = True
        msg.data = pts.tobytes()
        self.cloud_pub.publish(msg)


def main():
    rclpy.init()
    node = LoopSim()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.try_shutdown()
