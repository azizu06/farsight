"""farsight_bridge: jetson.server <-> ROS 2 / Nav2 (docs/navigation.md).

From the server's loopback WebSocket /ws/nav it publishes:
  TF map->odom (identity unless SLAM owns it), odom->base_link, base_link->camera_link,
  static camera_link->camera_optical; /odom; /farsight/depth/points (PointCloud2 in
  camera_optical); /farsight/depth/image (32FC1 metres, 0 = no return) with
  /farsight/depth/camera_info, stamped like the frame's TF; /farsight/range (Range).
It forwards /cmd_vel to the server, which applies it only under a live operator
lease, and executes the server's go-to-point intent as a NavigateToPose action.
"""

import array
import asyncio
import json
import math
import ssl
import threading
import time

import numpy as np
import rclpy
from rclpy.action import ActionClient
from rclpy.duration import Duration
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy, qos_profile_sensor_data
from rclpy.time import Time

from action_msgs.msg import GoalStatus
from geometry_msgs.msg import PoseStamped, TransformStamped, Twist
from nav2_msgs.action import NavigateToPose
from nav2_msgs.srv import ClearEntireCostmap
from nav_msgs.msg import OccupancyGrid, Odometry, Path
from sensor_msgs.msg import CameraInfo, Image, PointCloud2, PointField, Range
from std_msgs.msg import String
from std_srvs.srv import Trigger
from tf2_ros import Buffer, StaticTransformBroadcaster, TransformBroadcaster, TransformListener

from . import frames, frontiers, protocol

try:
    import websockets
except ImportError:  # pragma: no cover - installed by ros2-install.sh / the Docker image
    websockets = None


def stamp_from(node, age_ms):
    now = node.get_clock().now()
    age = max(0.0, float(age_ms or 0)) / 1000.0
    return (now - Duration(seconds=age)).to_msg()


def transform_msg(stamp, parent, child, translation, rotation_quaternion):
    message = TransformStamped()
    message.header.stamp, message.header.frame_id, message.child_frame_id = stamp, parent, child
    message.transform.translation.x, message.transform.translation.y, message.transform.translation.z = (
        float(translation[0]), float(translation[1]), float(translation[2]))
    (message.transform.rotation.x, message.transform.rotation.y,
     message.transform.rotation.z, message.transform.rotation.w) = rotation_quaternion
    return message


def image_msgs(stamp, frame_id, depth, header):
    """32FC1 depth image (metres, 0 = no return) and its CameraInfo, for nvblox and other consumers."""
    image = Image()
    image.header.stamp, image.header.frame_id = stamp, frame_id
    image.height, image.width = depth.shape
    image.encoding, image.is_bigendian, image.step = "32FC1", 0, depth.shape[1] * 4
    image.data = array.array("B", np.ascontiguousarray(depth, dtype="<f4").tobytes())  # see cloud_msg
    info = CameraInfo()
    info.header.stamp, info.header.frame_id = stamp, frame_id
    info.height, info.width = depth.shape
    fx, fy, cx, cy = (float(header[key]) for key in ("fx", "fy", "cx", "cy"))
    info.distortion_model = "plumb_bob"
    info.d = [0.0] * 5
    info.k = [fx, 0.0, cx, 0.0, fy, cy, 0.0, 0.0, 1.0]
    info.r = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]
    info.p = [fx, 0.0, cx, 0.0, 0.0, fy, cy, 0.0, 0.0, 0.0, 1.0, 0.0]
    return image, info


def cloud_msg(stamp, frame_id, points):
    message = PointCloud2()
    message.header.stamp, message.header.frame_id = stamp, frame_id
    message.height, message.width = 1, len(points)
    message.fields = [PointField(name=name, offset=4 * i, datatype=PointField.FLOAT32, count=1)
                      for i, name in enumerate("xyz")]
    message.is_bigendian, message.point_step = False, 12
    message.row_step, message.is_dense = 12 * len(points), True
    # array.array skips rclpy's per-element type assertion on uint8[] (bytes cost ~1 core here).
    message.data = array.array("B", np.ascontiguousarray(points, dtype="<f4").tobytes())
    return message


class FarsightBridge(Node):
    def __init__(self):
        super().__init__("farsight_bridge")
        p = self.declare_parameter
        self.server = p("server", "http://127.0.0.1:8443").value
        self.map_frame = p("map_frame", "map").value
        self.odom_frame = p("odom_frame", "odom").value
        self.base_frame = p("base_frame", "base_link").value
        self.camera_frame = p("camera_frame", "camera_link").value
        self.optical_frame = p("optical_frame", "camera_optical").value
        self.publish_map_odom = p("publish_map_odom", True).value
        self.controller = p("controller", "RPP").value
        self.max_cloud_points = p("max_cloud_points", 20000).value
        self.inactive_cancel_s = p("inactive_cancel_s", 0.3).value
        self.explore = p("explore", True).value  # frontier exploration for the server's Explore mode
        self.explore_goal_timeout_s = p("explore_goal_timeout_s", 25.0).value

        self.tf = TransformBroadcaster(self)
        self.static_tf = StaticTransformBroadcaster(self)
        self.tf_buffer = Buffer()
        self.tf_listener = TransformListener(self.tf_buffer, self)
        self.odom_pub = self.create_publisher(Odometry, "odom", 20)
        self.cloud_pub = self.create_publisher(PointCloud2, "farsight/depth/points", qos_profile_sensor_data)
        self.range_pub = self.create_publisher(Range, "farsight/range", qos_profile_sensor_data)
        self.image_pub = self.create_publisher(Image, "farsight/depth/image", qos_profile_sensor_data)
        self.info_pub = self.create_publisher(CameraInfo, "farsight/depth/camera_info", qos_profile_sensor_data)
        latched = QoSProfile(depth=1, reliability=ReliabilityPolicy.RELIABLE,
                             durability=DurabilityPolicy.TRANSIENT_LOCAL)
        self.controller_pub = self.create_publisher(String, "controller_selector", latched)
        self.controller_pub.publish(String(data=self.controller))
        self.create_subscription(Twist, "cmd_vel", self.on_cmd_vel, 20)
        self.create_subscription(Path, "plan", self.on_plan, 5)
        self.create_subscription(OccupancyGrid, "global_costmap/costmap", self.on_global_costmap, latched)
        self.navigate = ActionClient(self, NavigateToPose, "navigate_to_pose")
        # The action server exists before Nav2 is activated; the lifecycle manager knows.
        self.lifecycle = self.create_client(Trigger, "lifecycle_manager_navigation/is_active")
        self.clear_costmaps = [self.create_client(ClearEntireCostmap, name) for name in
                               ("local_costmap/clear_entirely_local_costmap",
                                "global_costmap/clear_entirely_global_costmap")]
        self.nav2_active = False
        self.lifecycle_pending = False
        self.create_timer(1.0, self.check_lifecycle)

        static = [transform_msg(self.get_clock().now().to_msg(), self.camera_frame, self.optical_frame,
                                (0, 0, 0), frames.OPTICAL_QUATERNION)]
        if self.publish_map_odom:
            static.append(transform_msg(self.get_clock().now().to_msg(), self.map_frame, self.odom_frame,
                                        (0, 0, 0), (0.0, 0.0, 0.0, 1.0)))
        self.static_tf.sendTransform(static)

        self.lock = threading.RLock()
        self.loop = None
        self.ws = None
        self.config = {}
        self.yaw_offset = 0.0  # drive profile camera_yaw_offset_rad, from the server hello
        self.generation = None  # server sensor generation (hello/reset); older samples are dropped
        self.last_tf_stamp = None
        self.velocity = frames.VelocityEstimator()  # odometry twist over a 150 ms window
        self.robot = None           # (x, y, yaw) in odom
        self.path = []              # odom points of the newest plan
        self.goal_handle = None
        self.goal_seq = 0           # bumps on every NavigateToPose send; callbacks for older sends are ignored
        self.goal_pending = False   # sent, not yet accepted or rejected
        self.goal_sent_at = 0.0
        self.goal_id = None         # server goal id of the active/last NavigateToPose
        self.goal_state = "idle"
        self.goal_message = ""
        self.recoveries = 0
        self.recovering_until = 0.0
        self.inactive_since = None
        self.cmd_times = []
        # Explore: the bridge picks frontier goals itself (no explore_lite for Jazzy).
        self.exploring = False
        self.global_grid = None
        self.frontiers = []          # Frontier list in the map frame, newest search
        self.frontiers_odom = []     # [(x, y)] goal points in odom, for the viewer
        self.explore_target = None   # map (x, y) of the current frontier goal
        self.explore_since = 0.0
        self.explore_seq = 0
        self.blacklist = []          # map (x, y) of frontier goals Nav2 failed to reach
        self.create_timer(0.25, self.send_status)
        self.create_timer(1.0, self.explore_tick)

    def check_lifecycle(self):
        if self.lifecycle_pending or not self.lifecycle.service_is_ready():
            return
        self.lifecycle_pending = True
        future = self.lifecycle.call_async(Trigger.Request())

        def done(result):
            self.lifecycle_pending = False
            try:
                self.nav2_active = bool(result.result().success)
            except Exception:
                self.nav2_active = False
        future.add_done_callback(done)

    def ready(self):
        return self.nav2_active and self.navigate.server_is_ready()

    # -- server messages ----------------------------------------------------------------

    def handle_text(self, message):
        kind = message.get("type")
        if kind == "hello":
            self.config = message
            self.generation = message.get("generation")
            offset = message.get("camera_yaw_offset_rad")
            self.yaw_offset = float(offset) if isinstance(offset, (int, float)) and math.isfinite(offset) else 0.0
        elif kind == "reset":
            self.on_reset(message)
        elif kind == "pose":
            if protocol.accept_sample(message, self.generation):
                self.on_pose(message)
        elif kind == "range":
            self.on_range(message)
        elif kind == "intent":
            self.on_intent(message)

    def on_reset(self, message):
        """New sensor session: nothing from the old one may steer the rover or mark a costmap."""
        with self.lock:
            self.generation = message.get("generation")
            self.velocity = frames.VelocityEstimator()
            self.robot = None
        self.cancel_goal(str(message.get("reason") or "sensor session changed"))
        for client in self.clear_costmaps:
            if client.service_is_ready():
                client.call_async(ClearEntireCostmap.Request())
        self.get_logger().warning(f"Sensor reset to generation {self.generation}: goal cancelled, costmaps cleared")

    def on_pose(self, message, stamp=None):
        transform = frames.transform_from_column_major(message["transform"])
        floor_y = float(message.get("floor_y", -0.15))
        stamp = stamp or stamp_from(self, message.get("age_ms"))
        stamp_ns = Time.from_msg(stamp).nanoseconds
        with self.lock:
            if self.last_tf_stamp is not None and stamp_ns <= self.last_tf_stamp:
                return  # TF must move forward; an older pose adds nothing
            self.last_tf_stamp = stamp_ns
        cam_pos, cam_rot = frames.camera_in_odom(transform, floor_y)
        base_pos, yaw = frames.base_in_odom(cam_pos, cam_rot, self.yaw_offset)
        rel_pos, rel_rot = frames.camera_in_base(cam_pos, cam_rot, base_pos, yaw)
        self.tf.sendTransform([
            transform_msg(stamp, self.odom_frame, self.base_frame, base_pos, frames.yaw_quaternion(yaw)),
            transform_msg(stamp, self.base_frame, self.camera_frame, rel_pos, frames.quaternion(rel_rot)),
        ])
        now = stamp_ns / 1e9
        with self.lock:
            twist = self.velocity.add(now, float(base_pos[0]), float(base_pos[1]), yaw)
            self.robot = (base_pos[0], base_pos[1], yaw)
        odom = Odometry()
        odom.header.stamp, odom.header.frame_id, odom.child_frame_id = stamp, self.odom_frame, self.base_frame
        odom.pose.pose.position.x, odom.pose.pose.position.y = float(base_pos[0]), float(base_pos[1])
        (odom.pose.pose.orientation.x, odom.pose.pose.orientation.y,
         odom.pose.pose.orientation.z, odom.pose.pose.orientation.w) = frames.yaw_quaternion(yaw)
        odom.twist.twist.linear.x, odom.twist.twist.angular.z = float(twist[0]), float(twist[1])
        self.odom_pub.publish(odom)

    def handle_depth(self, packet):
        header, depth = protocol.decode_depth(packet)
        if not protocol.accept_sample(header, self.generation):
            return  # limited tracking or an older sensor session
        stamp = stamp_from(self, header.get("age_ms"))
        # The frame carries its own pose: publish TF at the cloud's stamp so the costmap's
        # raytrace origin is the camera that actually captured these points.
        self.on_pose(dict(transform=header["transform"], floor_y=header.get("floor_y", -0.15)), stamp=stamp)
        points = frames.depth_points(depth, header["fx"], header["fy"], header["cx"], header["cy"])
        if len(points) > self.max_cloud_points:
            points = points[:: math.ceil(len(points) / self.max_cloud_points)]
        self.cloud_pub.publish(cloud_msg(stamp, self.optical_frame, points))
        if self.image_pub.get_subscription_count() or self.info_pub.get_subscription_count():  # nvblox only
            image, info = image_msgs(stamp, self.optical_frame, depth, header)
            self.image_pub.publish(image)
            self.info_pub.publish(info)

    def on_range(self, message):
        cm = message.get("cm")
        reading = Range()
        reading.header.stamp = stamp_from(self, message.get("age_ms"))
        reading.header.frame_id = self.base_frame
        reading.radiation_type = Range.ULTRASOUND
        reading.field_of_view, reading.min_range, reading.max_range = 0.26, 0.02, 4.0
        reading.range = float(cm) / 100 if isinstance(cm, (int, float)) and cm > 0 else math.inf
        self.range_pub.publish(reading)

    def on_intent(self, message):
        """Server intent: goto/seek carry a goal (seek's moves with the person); explore picks frontiers."""
        active, mode, goal = message.get("active"), message.get("mode"), message.get("goal")
        goal_id = message.get("goal_id")
        exploring = bool(active and mode == "explore" and self.explore)
        with self.lock:
            if exploring and not self.exploring:
                self.explore_target, self.blacklist = None, []  # a new Explore press starts fresh
            self.exploring = exploring
            if exploring:
                self.inactive_since = None
                return  # explore_tick sends frontier goals
            if not active or mode not in ("goto", "seek") or goal is None:
                if self.goal_handle is not None:
                    now = time.monotonic()
                    self.inactive_since = self.inactive_since or now
                    if now - self.inactive_since >= self.inactive_cancel_s:
                        self.cancel_goal("operator released or changed mode")
                if mode not in ("goto", "seek") or goal is None:
                    self.goal_state = "idle"
                return
            self.inactive_since = None
            pending = self.goal_pending and time.monotonic() - self.goal_sent_at < 5.0  # resend if Nav2 never answers
            if goal_id == self.goal_id and (self.goal_handle is not None or pending
                                            or self.goal_state in ("arrived", "failed")):
                return  # sent, running, or finished and latched until the server issues a new goal id
        self.send_goal(goal_id, goal)

    # -- frontier exploration ---------------------------------------------------------------

    def on_global_costmap(self, message):
        with self.lock:
            self.global_grid = message

    def planar(self, target, source):
        """(x, y, yaw) -> (x, y, yaw) from source to target frame; identity when TF has none (map == odom)."""
        try:
            t = self.tf_buffer.lookup_transform(target, source, Time()).transform
        except Exception:
            return lambda x, y, yaw=0.0: (x, y, yaw)
        turn = math.atan2(2 * (t.rotation.w * t.rotation.z + t.rotation.x * t.rotation.y),
                          1 - 2 * (t.rotation.y ** 2 + t.rotation.z ** 2))
        c, s, tx, ty = math.cos(turn), math.sin(turn), t.translation.x, t.translation.y
        return lambda x, y, yaw=0.0: (c * x - s * y + tx, s * x + c * y + ty, yaw + turn)

    def explore_tick(self):
        with self.lock:
            exploring, grid, robot = self.exploring, self.global_grid, self.robot
        if grid is None:
            return
        info = grid.info
        cells = np.asarray(grid.data, np.int16).reshape(info.height, info.width)
        found = frontiers.find(cells, info.resolution, (info.origin.position.x, info.origin.position.y))
        to_odom = self.planar(self.odom_frame, self.map_frame)
        with self.lock:
            self.frontiers = found
            self.frontiers_odom = [to_odom(*f.goal)[:2] for f in sorted(found, key=lambda f: -f.size_m)[:50]]
        if not exploring or robot is None or not self.ready():
            return
        robot_map = self.planar(self.map_frame, self.odom_frame)(*robot)
        now = time.monotonic()
        with self.lock:
            target = self.explore_target
            stale = (target is None or (self.goal_handle is None and not self.goal_pending)
                     or not frontiers.still_frontier(found, target)
                     or now - self.explore_since > self.explore_goal_timeout_s)
            if not stale:
                return
            if target is not None and now - self.explore_since > self.explore_goal_timeout_s:
                self.blacklist.append(target)
            choice = frontiers.choose(found, robot_map, self.blacklist)
            if choice is None and found and self.blacklist:
                self.blacklist = []  # everything left was skipped once: try those again
                choice = frontiers.choose(found, robot_map)
            if choice is None:
                self.explore_target = None
                if not found and self.goal_handle is None:
                    self.goal_state, self.goal_message = "arrived", "Exploration complete: no frontier left"
                return
            self.explore_target, self.explore_since = choice.goal, now
            self.explore_seq += 1
            goal_id = f"explore-{self.explore_seq}"
        gx, gy, _ = to_odom(*frontiers.beyond(choice, robot_map))
        self.send_goal(goal_id, dict(x=gx, y=gy, yaw=None))
        with self.lock:
            self.goal_message = f"Exploring frontier {choice.size_m:.1f} m wide"

    # -- Nav2 ----------------------------------------------------------------------------

    def send_goal(self, goal_id, goal):
        if not self.ready():
            with self.lock:
                self.goal_state, self.goal_message = "planning", "Waiting for Nav2 navigate_to_pose"
            return
        x, y = float(goal["x"]), float(goal["y"])
        with self.lock:
            robot = self.robot
        yaw = goal.get("yaw")
        if yaw is None:
            yaw = math.atan2(y - robot[1], x - robot[0]) if robot else 0.0
        pose = PoseStamped()
        pose.header.frame_id = self.odom_frame
        pose.header.stamp = Time().to_msg()  # latest available transform
        pose.pose.position.x, pose.pose.position.y = x, y
        (pose.pose.orientation.x, pose.pose.orientation.y,
         pose.pose.orientation.z, pose.pose.orientation.w) = frames.yaw_quaternion(yaw)
        pose = self.to_frame(pose, self.map_frame)
        request = NavigateToPose.Goal()
        request.pose = pose
        with self.lock:
            self.goal_id, self.goal_state, self.goal_message = goal_id, "planning", ""
            self.path, self.recoveries = [], 0
            self.goal_seq += 1
            self.goal_pending, self.goal_sent_at, seq = True, time.monotonic(), self.goal_seq
        future = self.navigate.send_goal_async(request, feedback_callback=self.on_feedback)
        future.add_done_callback(lambda done, sent=seq: self.on_accepted(sent, done))

    def to_frame(self, pose, frame):
        if pose.header.frame_id == frame:
            return pose
        try:
            return self.tf_buffer.transform(pose, frame, timeout=Duration(seconds=0.2))
        except Exception:
            pose.header.frame_id = frame  # map == odom when no SLAM has published yet
            return pose

    def on_accepted(self, seq, future):
        handle = future.result()
        with self.lock:
            if seq != self.goal_seq:  # superseded by a newer send (or a cancel)
                if handle.accepted:
                    handle.cancel_goal_async()
                return
            self.goal_pending = False
            if not handle.accepted:
                self.goal_state, self.goal_message = "failed", "Nav2 rejected the goal"
                return
            self.goal_handle = handle
        handle.get_result_async().add_done_callback(lambda done, sent=seq: self.on_result(sent, done))

    def on_feedback(self, feedback):
        data = feedback.feedback
        with self.lock:
            if data.number_of_recoveries > self.recoveries:
                self.recoveries = data.number_of_recoveries
                self.recovering_until = time.monotonic() + 3.0
            if self.goal_state in ("planning", "driving", "recovering"):
                self.goal_state = "recovering" if time.monotonic() < self.recovering_until else "driving"
                self.goal_message = f"{data.distance_remaining:.1f} m to go"

    def on_result(self, seq, future):
        result = future.result()
        with self.lock:
            if seq != self.goal_seq:
                return  # a preempted or cancelled earlier send must not end the current goal
            self.goal_handle = None
            if self.exploring and str(self.goal_id).startswith("explore-") and result.status != GoalStatus.STATUS_CANCELED:
                # One frontier done (or unreachable): exploration goes on, so never report arrived here.
                if result.status != GoalStatus.STATUS_SUCCEEDED and self.explore_target is not None:
                    self.blacklist.append(self.explore_target)
                self.goal_state = "driving"
                self.goal_message = ("Frontier reached; choosing the next" if result.status == GoalStatus.STATUS_SUCCEEDED
                                     else "Frontier unreachable; trying another")
                self.explore_target, self.path = None, []
                return
            if result.status == GoalStatus.STATUS_SUCCEEDED:
                self.goal_state, self.goal_message = "arrived", "Goal reached"
            elif result.status == GoalStatus.STATUS_CANCELED:
                self.goal_state, self.goal_message = "idle", "Navigation cancelled"
                self.goal_id = None  # a later active intent with the same id re-sends it
            else:
                code = getattr(result.result, "error_code", 0)
                self.goal_state, self.goal_message = "failed", f"Nav2 aborted (error {code})"
            self.path = []

    def cancel_goal(self, reason):
        with self.lock:
            handle, self.goal_handle = self.goal_handle, None
            self.goal_seq += 1  # any in-flight acceptance for the cancelled send is dropped
            self.goal_pending = False
            self.goal_state, self.goal_message, self.path = "idle", reason, []
            self.goal_id, self.inactive_since = None, None
        if handle is not None:
            handle.cancel_goal_async()

    def on_plan(self, message):
        # One TF lookup per plan, not per pose: plans have hundreds of poses at several Hz.
        to_odom = self.planar(self.odom_frame, message.header.frame_id)
        points = [to_odom(stamped.pose.position.x, stamped.pose.position.y)[:2] for stamped in message.poses]
        with self.lock:
            if self.goal_handle is not None or self.goal_state == "planning":
                self.path = points

    def on_cmd_vel(self, message):
        now = time.monotonic()
        with self.lock:
            self.cmd_times = [t for t in self.cmd_times if now - t < 2.0] + [now]
            goal_id = self.goal_id
        self.send(protocol.cmd_vel(message.linear.x, message.angular.z, goal_id))

    def send_status(self):
        with self.lock:
            hz = (len(self.cmd_times) - 1) / (self.cmd_times[-1] - self.cmd_times[0]) if len(self.cmd_times) > 2 else 0.0
            message = protocol.status(self.goal_state, self.goal_id, path=self.path[::2] if len(self.path) > 400 else self.path,
                                      frontiers=self.frontiers_odom, exploring=self.exploring,
                                      message=self.goal_message, controller_hz=round(hz, 1),
                                      recoveries=self.recoveries, nav2=self.ready())
        self.send(message)

    # -- WebSocket -----------------------------------------------------------------------

    def send(self, message):
        loop, ws = self.loop, self.ws
        if loop is None or ws is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(ws.send(protocol.encode(message)), loop)
        except RuntimeError:
            pass

    async def connect_forever(self):
        url = self.server.rstrip("/").replace("https://", "wss://").replace("http://", "ws://") + "/ws/nav"
        context = None
        if url.startswith("wss://"):
            context = ssl.create_default_context()
            context.check_hostname, context.verify_mode = False, ssl.CERT_NONE  # loopback self-signed
        while rclpy.ok():
            try:
                async with websockets.connect(url, ssl=context, max_size=8 * 1024 * 1024, ping_interval=None) as ws:
                    await ws.send(protocol.encode(protocol.hello(controller=self.controller,
                                                                 slam=not self.publish_map_odom,
                                                                 nav2=True)))
                    self.ws = ws
                    self.get_logger().info(f"Connected to {url}")
                    async for raw in ws:
                        if isinstance(raw, bytes):
                            self.handle_depth(raw)
                        else:
                            self.handle_text(json.loads(raw))
            except Exception as exc:  # reconnect on any failure; motion is gated by the server
                self.get_logger().warning(f"Server link: {exc}", throttle_duration_sec=10.0)
            finally:
                self.ws = None
                if self.goal_handle is not None:
                    self.cancel_goal("server link lost")
            await asyncio.sleep(1.0)

    def run_websocket(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self.connect_forever())


def main():
    rclpy.init()
    node = FarsightBridge()
    if websockets is None:
        node.get_logger().fatal("python3-websockets is not installed")
        return
    thread = threading.Thread(target=node.run_websocket, name="farsight-ws", daemon=True)
    thread.start()
    executor = MultiThreadedExecutor(num_threads=3)
    executor.add_node(node)
    try:
        executor.spin()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            node.destroy_node()
            if rclpy.ok():
                rclpy.shutdown()
        except (KeyboardInterrupt, Exception):
            pass


if __name__ == "__main__":
    main()
