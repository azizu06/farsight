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

from . import frames, frontiers, local_motion, protocol

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
        self.explore_stall_s = p("explore_stall_s", 3.0).value          # no 0.3 m of progress: frontier blocked
        self.explore_blacklist_s = p("explore_blacklist_s", 30.0).value  # a skipped frontier may come back after this
        # Low net progress over a bounded window forces a frontier away from where the rover is stuck.
        self.progress = frontiers.ProgressWindow(window_s=p("explore_progress_window_s", 8.0).value,
                                                 min_m=p("explore_progress_min_m", 0.4).value)
        self.explore_escape_m = p("explore_escape_m", 1.5).value
        # A server stop that re-arms Explore within this long (demo-mode video/camera blips)
        # resumes the same frontier, blacklist and progress window instead of starting over.
        self.explore_resume_s = p("explore_resume_s", 2.0).value
        self.explore_paused_at = None
        self.explore_config = frontiers.ExploreConfig(**{
            name: p("explore_" + name, field.default).value
            for name, field in frontiers.ExploreConfig.__dataclass_fields__.items()})
        self.local_motion = local_motion.LocalMotion(
            min_width=self.explore_config.min_corridor_width,
            creep_mps=p("explore_creep_mps", 0.2).value,
            turn_radps=p("explore_scan_radps", 0.82).value,
            budget_s=p("explore_gap_budget_s", 4.0).value,
            scan_budget_s=p("explore_scan_budget_s", 10.0).value)
        self.swept = frontiers.SweptFootprint(radius=p("explore_body_radius", 0.35).value)
        # "events": rclpy's C++ EventsExecutor. The Python MultiThreadedExecutor
        # rebuilt its wait set on every TF/costmap/feedback wakeup and spent
        # most of a core doing it. Callbacks here never block, so one thread is enough.
        self.executor_kind = p("executor", "events").value

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
        self.intent_active = False
        self.intent_goal_id = None
        self.goal_generation = None
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
        self.explorer = frontiers.Explorer(self.explore_config)
        self.global_grid = None
        self.global_grid_at = -math.inf
        self.local_support = None
        self.depth_snapshot = None
        self.depth_at = self.pose_at = -math.inf
        self.nav_command = None
        self.nav_command_at = -math.inf
        self.no_frontier_since = None
        self.explore_finished = False
        self.frontiers = []          # Frontier list in the map frame, newest search
        self.frontiers_odom = []     # [(x, y)] goal points in odom, for the viewer
        self.explore_target = None   # map (x, y) of the current frontier goal
        self.explore_since = 0.0
        self.explore_seq = 0
        self.blacklist = []          # map (x, y, monotonic time) of frontier goals Nav2 failed to reach
        self.explore_blocked = False  # Nav2 started a recovery on the current frontier goal
        self.explore_fallback = False  # current goal came from the unscored fallback chooser
        self.escape_from, self.escape_until = None, 0.0  # (x, y) map point to leave after low progress
        self.progress_best, self.progress_at = math.inf, 0.0  # closest distance_remaining and when it improved
        self.create_timer(0.25, self.send_status)
        self.explore_busy = False
        self.explore_again = False   # a re-pick was requested while scoring ran
        self.explore_session = 0
        self.create_timer(p("explore_tick_s", 0.5).value, self.request_explore_tick)
        self.create_timer(0.05, self.motion_tick)

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
            self.on_reset(dict(generation=message.get("generation"), reason="navigation server connected"))
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
            self.intent_active, self.intent_goal_id, self.exploring = False, None, False
            self.explore_paused_at = None  # a new session or server link never resumes the old Explore
            self.nav_command, self.nav_command_at = None, -math.inf
            self.frontiers, self.frontiers_odom = [], []
            self.velocity = frames.VelocityEstimator()
            self.robot = None
            self.global_grid, self.local_support, self.depth_snapshot = None, None, None
            self.depth_at = self.pose_at = self.global_grid_at = -math.inf
            self.explorer = frontiers.Explorer(self.explore_config)
            self.swept = frontiers.SweptFootprint(self.swept.radius)
            self.explore_session += 1
            self.local_motion.reset()
            self.explore_target, self.no_frontier_since = None, None
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
            self.swept.record(base_pos[:2])
            age = max(0, (self.get_clock().now().nanoseconds - stamp_ns) / 1e9)
            self.pose_at = time.monotonic() - age
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
        with self.lock:
            self.depth_snapshot = (depth, header)
            self.depth_at = time.monotonic() - max(0, float(header.get("age_ms") or 0) / 1000)
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
            if not protocol.accept_generation(message, self.generation):
                return  # a queued old intent cannot resume navigation after reset
            self.intent_active = bool(active and (exploring or (mode in ("goto", "seek") and goal is not None)))
            self.intent_goal_id = goal_id if self.intent_active and not exploring else None
            if exploring and not self.exploring:
                self.explore_session += 1  # discard scoring that started before the pause
                paused = self.explore_paused_at
                if paused is None or time.monotonic() - paused > self.explore_resume_s:
                    self.explore_target, self.blacklist = None, []  # a new Explore press starts fresh
                    self.progress.reset()
                    self.escape_from = None
                    self.no_frontier_since, self.explore_finished = None, False
                    # Keep decaying visits, start a separate commitment object so an
                    # older scoring result cannot arm this new operator press.
                    previous = self.explorer
                    self.explorer = frontiers.Explorer(self.explore_config)
                    self.explorer.visits = dict(previous.visits)
                    self.explorer.last_robot = previous.last_robot
                # else: a short server stop (video/camera blip) resumes the same frontier
                # without re-picking a goal and turning to face it.
                self.explore_paused_at = None
                self.local_motion.reset()
                self.nav_command, self.nav_command_at = None, -math.inf
            if self.exploring and not exploring:
                self.explore_paused_at = time.monotonic()
                self.local_motion.reset()
                self.nav_command, self.nav_command_at = None, -math.inf
            self.exploring = exploring
            if exploring:
                self.inactive_since = None
                return  # explore_tick sends frontier goals
            if not active or mode not in ("goto", "seek") or goal is None:
                self.nav_command, self.nav_command_at = None, -math.inf
                if self.goal_pending:
                    self.cancel_goal("operator released or changed mode before goal acceptance")
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
            self.global_grid_at = time.monotonic()

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

    def request_explore_tick(self):
        # The EventsExecutor has one callback thread. Scoring must not starve
        # 20 Hz command output or sensor callbacks on a large costmap.
        with self.lock:
            if self.explore_busy:
                self.explore_again = True  # an abort or recovery arrived mid-score: score again at once
                return
            self.explore_busy, self.explore_again = True, False

        def update():
            while True:
                try:
                    self.explore_tick()
                except Exception as exc:
                    self.get_logger().error(f"Explore scoring: {exc}")
                with self.lock:
                    if not self.explore_again:
                        self.explore_busy = False
                        return
                    self.explore_again = False
        threading.Thread(target=update, name="explore-score", daemon=True).start()

    def skip_frontier(self, point=None):
        """Caller holds the lock. Blacklist a frontier goal (default: the current one) for explore_blacklist_s."""
        if point is None:
            point = self.explore_target if self.explore_fallback else (self.explorer.committed or self.explore_target)
        if point is not None:
            self.blacklist.append((float(point[0]), float(point[1]), time.monotonic()))

    def explore_tick(self):
        with self.lock:
            exploring, grid, robot = self.exploring, self.global_grid, self.robot
            session, swept = self.explore_session, self.swept
        if grid is None:
            return
        info = grid.info
        raw_cells = np.asarray(grid.data, np.int16).reshape(info.height, info.width)
        origin = (info.origin.position.x, info.origin.position.y)
        to_map = self.planar(self.map_frame, self.odom_frame)
        with self.lock:
            # Snapshot path samples; NumPy rasterization runs outside the lock.
            trail = frontiers.SweptFootprint(swept.radius)
            trail.poses = dict(swept.poses)
        cells = trail.apply(raw_cells, info.resolution, origin, to_map)
        cells, origin = frontiers.observed_window(cells, info.resolution, origin,
                                                  margin=self.explore_config.sensor_range)
        found = frontiers.find(cells, info.resolution, origin)
        to_odom = self.planar(self.odom_frame, self.map_frame)
        with self.lock:
            if session != self.explore_session:
                return
            self.frontiers = found
            self.frontiers_odom = [to_odom(*f.goal)[:2] for f in sorted(found, key=lambda f: -f.size_m)[:50]]
        if not exploring or robot is None or not self.ready():
            return
        robot_map = to_map(*robot)
        now = time.monotonic()
        support = frontiers.clearance_map(~((cells >= 0) & (cells <= frontiers.FREE_MAX)), info.resolution)
        # Inflation encodes robot padding, not an observed object inside the
        # current body disc. Only lethal measured cells veto the physical sweep;
        # raw live depth below independently vetoes newly arrived objects.
        obstacles = frontiers.clearance_map(cells >= 100, info.resolution)
        with self.lock:
            if session != self.explore_session or not self.exploring:
                return
            self.local_support = (support, info.resolution, origin, obstacles)
            if now - self.pose_at >= 0.5 or now - self.depth_at >= 0.5 or now - self.global_grid_at >= 2.5:
                return  # server capture-loss interlock owns stopping and explicit rearming
            target = self.explore_target
            # Low net progress (spinning, dithering, pushing) over the window: leave this spot.
            stuck = self.progress.update(now, robot_map[:2])
            if stuck:
                self.progress.reset()
                self.escape_from, self.escape_until = tuple(robot_map[:2]), now + 6.0
            # Re-pick the moment the current frontier is blocked: Nav2 began a
            # recovery, or got no 0.3 m closer for explore_stall_s (15 s backstop).
            timed_out = target is not None and (
                stuck or self.explore_blocked or now - self.progress_at > self.explore_stall_s
                or now - self.explore_since > self.explore_goal_timeout_s)
            if timed_out:
                self.skip_frontier()
                self.explorer.release()
            self.blacklist = [bad for bad in self.blacklist if now - bad[2] < self.explore_blacklist_s]
            explorer, blacklist = self.explorer, [bad[:2] for bad in self.blacklist]
            escape = self.escape_from if now < self.escape_until else None
        candidates = found
        if escape is not None:  # a genuinely different goal: nothing near the stuck spot
            candidates = [f for f in found if math.dist(f.goal, escape) >= self.explore_escape_m] or found
        choice = explorer.choose(candidates, robot_map, cells, info.resolution, origin,
                                 now=now, blacklist=blacklist)
        if choice is None:  # never stop while a rover-width route to any frontier exists
            choice = explorer.choose(candidates, robot_map, cells, info.resolution, origin,
                                     now=now, blacklist=blacklist, relaxed=True)
        fallback = False
        if choice is None:
            # Real rooms: the approach to most frontiers is still unknown, so the measured
            # corridor checks reject them all. Hand Nav2 the nearest broad one anyway; it plans
            # through unknown, and a failure or stall re-picks at once. Scan only with none.
            keep = None
            if target is not None and not timed_out and self.explore_fallback:
                keep = next((f for f in candidates if math.dist(f.goal, target) < 0.5), None)
            choice = keep or frontiers.choose(candidates, robot_map, blacklist=blacklist)
            fallback = choice is not None
        with self.lock:
            if (session != self.explore_session or not self.exploring
                    or time.monotonic() - self.pose_at >= 0.5 or time.monotonic() - self.depth_at >= 0.5):
                return
            if choice is None:
                # Explore never ends on its own (captain's order): local motion wanders toward
                # the most open measured arc, scanning when none is open, until a frontier appears.
                if self.no_frontier_since is None:
                    self.no_frontier_since = now
                    self.cancel_goal("No reachable frontier")
                self.explore_target = None
                self.goal_state, self.goal_message = "planning", "No reachable frontier: wandering toward open space"
                return
            self.no_frontier_since = None
            running = self.goal_handle is not None or (
                self.goal_pending and now - self.goal_sent_at < 5.0)
            # A kept fallback frontier drifts as cells clear; re-sending it would restart Nav2.
            radius = 0.5 if fallback and self.explore_fallback else max(0.1, 2 * info.resolution)
            same = target is not None and math.dist(choice.goal, target) < radius
            if same and running and not timed_out:
                return
            # Preselect the next broad patch within the goal tolerance, before
            # Nav2 reports completion. The local policy covers planning gaps.
            self.explore_target, self.explore_since, self.explore_fallback = choice.goal, now, fallback
            self.explore_seq += 1
            goal_id = f"explore-{self.explore_seq}"
        gx, gy, _ = to_odom(*choice.goal)
        yaw = math.atan2(choice.centroid[1] - robot_map[1], choice.centroid[0] - robot_map[0])
        _, _, yaw_odom = to_odom(0, 0, yaw)
        sent = self.send_goal(goal_id, dict(x=gx, y=gy, yaw=yaw_odom), explore_session=session)
        with self.lock:
            if sent and session == self.explore_session:
                self.goal_message = (f"Exploring frontier {choice.size_m:.1f} m (route still unseen)" if fallback
                                     else f"Exploring broad frontier {choice.size_m:.1f} m")

    def motion_tick(self):
        """One Explore command writer: current Nav2 or fresh local planning fallback."""
        with self.lock:
            if not self.exploring or not self.intent_active or type(self.generation) is not int:
                return
            now = time.monotonic()
            if self.explore_finished or not self.ready():
                self.send(protocol.cmd_vel(0, 0, self.goal_id, self.generation))
                return
            support, resolution, origin, obstacles = self.local_support or (None, 1, (0, 0), None)
            depth, header = self.depth_snapshot or (None, {})
            robot = self.robot
            # Support is map-frame; measured depth projection is odom-frame.
            # Query the support in map using each odom sample point.
            # Map/odom may rotate under SLAM, so pass that transform explicitly.
            to_map = self.planar(self.map_frame, self.odom_frame)
            body_clear = False
            if robot is not None and obstacles is not None:
                rx, ry, _ = to_map(*robot)
                col, row = math.floor((rx - origin[0]) / resolution), math.floor((ry - origin[1]) / resolution)
                body_clear = (0 <= row < obstacles.shape[0] and 0 <= col < obstacles.shape[1]
                              and obstacles[row, col] >= self.swept.radius)
            command = self.local_motion.command(
                now=now, active=True, nav_command=self.nav_command, nav_at=self.nav_command_at,
                robot=robot, pose_at=self.pose_at, depth=depth, header=header, depth_at=self.depth_at,
                support=support, resolution=resolution, origin=origin, map_at=self.global_grid_at,
                to_map=to_map, body_clear=body_clear)
            if command is not None:
                self.send(protocol.cmd_vel(*command, self.goal_id, self.generation))

    # -- Nav2 ----------------------------------------------------------------------------

    def send_goal(self, goal_id, goal, explore_session=None):
        if not self.ready():
            with self.lock:
                self.goal_state, self.goal_message = "planning", "Waiting for Nav2 navigate_to_pose"
            return
        x, y = float(goal["x"]), float(goal["y"])
        with self.lock:
            robot, generation = self.robot, self.generation
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
            if generation != self.generation or not self.intent_active:
                return False  # sensor reset or release while the TF lookup ran
            if explore_session is not None and (explore_session != self.explore_session or not self.exploring):
                return False  # operator released/changed mode while scoring or TF lookup ran
            if explore_session is None and (self.exploring or goal_id != self.intent_goal_id):
                return False
            self.goal_id, self.goal_state, self.goal_message = goal_id, "planning", ""
            self.goal_generation = generation
            self.path, self.recoveries = [], 0
            self.nav_command, self.nav_command_at = None, -math.inf
            self.explore_blocked = False
            self.progress_best, self.progress_at = math.inf, time.monotonic()
            self.goal_seq += 1
            self.goal_pending, self.goal_sent_at, seq = True, time.monotonic(), self.goal_seq
        future = self.navigate.send_goal_async(
            request, feedback_callback=lambda feedback, sent=seq: self.on_feedback(sent, feedback))
        future.add_done_callback(lambda done, sent=seq: self.on_accepted(sent, done))
        return True

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
        repick = False
        with self.lock:
            if seq != self.goal_seq:  # superseded by a newer send (or a cancel)
                if handle.accepted:
                    handle.cancel_goal_async()
                return
            self.goal_pending = False
            self.nav_command, self.nav_command_at = None, -math.inf
            if handle.accepted:
                self.goal_handle = handle
            else:
                self.goal_handle, self.goal_generation = None, None
                self.goal_state, self.goal_message = "failed", "Nav2 rejected the goal"
                if self.exploring and self.explore_target is not None:
                    self.explore_session += 1
                    self.skip_frontier()
                    self.explore_target = None
                    self.explorer.release()
                    repick = True
        if not handle.accepted:
            if repick:
                self.request_explore_tick()
            return
        handle.get_result_async().add_done_callback(lambda done, sent=seq: self.on_result(sent, done))

    def on_feedback(self, seq, feedback):
        data = feedback.feedback
        now = time.monotonic()
        repick = False
        with self.lock:
            if seq != self.goal_seq:
                return  # a preempted earlier goal's recoveries say nothing about this one
            if 0 < data.distance_remaining < self.progress_best - 0.3:
                self.progress_best, self.progress_at = data.distance_remaining, now
            elif not math.isfinite(self.progress_best) and data.distance_remaining > 0:
                self.progress_best = data.distance_remaining
            if data.number_of_recoveries > self.recoveries:
                self.recoveries = data.number_of_recoveries
                self.recovering_until = now + 3.0
                if self.exploring and str(self.goal_id).startswith("explore-") and not self.explore_blocked:
                    self.explore_blocked = repick = True  # pick another frontier now; Nav2 keeps recovering meanwhile
            if self.goal_state in ("planning", "driving", "recovering"):
                self.goal_state = "recovering" if time.monotonic() < self.recovering_until else "driving"
                self.goal_message = f"{data.distance_remaining:.1f} m to go"
        if repick:
            self.request_explore_tick()

    def on_result(self, seq, future):
        result = future.result()
        repick = False
        with self.lock:
            if seq != self.goal_seq:
                return  # a preempted or cancelled earlier send must not end the current goal
            self.goal_handle = None
            if self.exploring and str(self.goal_id).startswith("explore-") and result.status != GoalStatus.STATUS_CANCELED:
                # One frontier done (or unreachable): exploration goes on, so never report arrived here.
                self.explore_session += 1  # discard a scorer snapshot from before this result
                if result.status != GoalStatus.STATUS_SUCCEEDED and self.explore_target is not None:
                    self.skip_frontier()
                self.goal_state = "driving"
                self.goal_message = ("Frontier reached; choosing the next" if result.status == GoalStatus.STATUS_SUCCEEDED
                                     else "Frontier unreachable; trying another")
                self.explore_target, self.path = None, []
                self.explorer.release()
                self.nav_command, self.nav_command_at = None, -math.inf
                repick = True
            elif result.status == GoalStatus.STATUS_SUCCEEDED:
                self.goal_state, self.goal_message = "arrived", "Goal reached"
            elif result.status == GoalStatus.STATUS_CANCELED:
                self.goal_state, self.goal_message = "idle", "Navigation cancelled"
                self.goal_id = None  # a later active intent with the same id re-sends it
            else:
                code = getattr(result.result, "error_code", 0)
                self.goal_state, self.goal_message = "failed", f"Nav2 aborted (error {code})"
            self.path = []
        if repick:
            self.request_explore_tick()  # next frontier now, not at the next 0.5 s tick

    def cancel_goal(self, reason):
        with self.lock:
            handle, self.goal_handle = self.goal_handle, None
            self.goal_seq += 1  # any in-flight acceptance for the cancelled send is dropped
            self.goal_pending = False
            self.goal_generation = None
            self.nav_command, self.nav_command_at = None, -math.inf
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
            if (not self.intent_active or self.goal_pending or self.goal_handle is None
                    or type(self.generation) is not int or self.goal_generation != self.generation):
                return
            goal_id = self.goal_id
            if not self.exploring and goal_id != self.intent_goal_id:
                return
            if self.exploring:
                self.nav_command = (float(message.linear.x), float(message.angular.z))
                self.nav_command_at = now
                return  # motion_tick arbitrates; never race two command writers
            # Twist carries no goal id: this gates reset/pending callbacks, but
            # cannot identify a delayed old Twist after a new goal is accepted.
            outgoing = protocol.cmd_vel(message.linear.x, message.angular.z, goal_id, self.generation)
        self.send(outgoing)

    def send_status(self):
        with self.lock:
            hz = (len(self.cmd_times) - 1) / (self.cmd_times[-1] - self.cmd_times[0]) if len(self.cmd_times) > 2 else 0.0
            message = protocol.status(self.goal_state, self.goal_id, path=self.path[::2] if len(self.path) > 400 else self.path,
                                      frontiers=self.frontiers_odom, exploring=self.exploring,
                                      message=self.goal_message, controller_hz=round(hz, 1),
                                      recoveries=self.recoveries, nav2=self.ready(), generation=self.generation)
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
                with self.lock:
                    self.intent_active, self.intent_goal_id, self.exploring = False, None, False
                    self.explore_paused_at = None  # a new session or server link never resumes the old Explore
                self.cancel_goal("server link lost")
            await asyncio.sleep(1.0)

    def run_websocket(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self.connect_forever())


def make_executor(node):
    if node.executor_kind == "events":
        try:
            from rclpy.experimental import EventsExecutor
            return EventsExecutor()
        except ImportError:
            node.get_logger().warning("rclpy has no EventsExecutor; using the multi-threaded executor")
    elif node.executor_kind != "multithreaded":
        raise ValueError("executor must be events or multithreaded")
    return MultiThreadedExecutor(num_threads=3)


def main():
    rclpy.init()
    node = FarsightBridge()
    if websockets is None:
        node.get_logger().fatal("python3-websockets is not installed")
        return
    thread = threading.Thread(target=node.run_websocket, name="farsight-ws", daemon=True)
    thread.start()
    executor = make_executor(node)
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
