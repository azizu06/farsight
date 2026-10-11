"""Exercise bridge callbacks without starting ROS, a WebSocket, or any hardware."""

import importlib
import math
from pathlib import Path
import sys
import threading
import time
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from farsight_nav import crowds, frontiers, local_motion  # noqa: E402


def bridge_module():
    # ROS is external to the host test environment. Keep the real callbacks and
    # protocol; replace only imports needed to define the Node subclass.
    names = {
        "rclpy": (), "rclpy.action": ("ActionClient",), "rclpy.duration": ("Duration",),
        "rclpy.executors": ("MultiThreadedExecutor",), "rclpy.node": ("Node",),
        "rclpy.qos": ("DurabilityPolicy", "QoSProfile", "ReliabilityPolicy", "qos_profile_sensor_data"),
        "rclpy.time": ("Time",), "action_msgs": (), "action_msgs.msg": ("GoalStatus",),
        "geometry_msgs": (), "geometry_msgs.msg": ("PoseStamped", "TransformStamped", "Twist"),
        "nav2_msgs": (), "nav2_msgs.action": ("NavigateToPose",),
        "nav2_msgs.msg": ("CostmapFilterInfo",),
        "nav2_msgs.srv": ("ClearEntireCostmap",), "nav_msgs": (),
        "nav_msgs.msg": ("OccupancyGrid", "Odometry", "Path"), "sensor_msgs": (),
        "sensor_msgs.msg": ("CameraInfo", "Image", "PointCloud2", "PointField", "Range"),
        "std_msgs": (), "std_msgs.msg": ("String",), "std_srvs": (),
        "std_srvs.srv": ("Trigger",),
        "tf2_ros": ("Buffer", "StaticTransformBroadcaster", "TransformBroadcaster", "TransformListener"),
    }
    modules = {}
    for name, attributes in names.items():
        module = ModuleType(name)
        module.__path__ = []
        for attribute in attributes:
            setattr(module, attribute, object)
        modules[name] = module
    with patch.dict(sys.modules, modules):
        return importlib.import_module("farsight_nav.bridge_node")


BRIDGE_MODULE = bridge_module()
Bridge = BRIDGE_MODULE.FarsightBridge


class Handle:
    accepted = True

    def __init__(self):
        self.cancelled = False

    def cancel_goal_async(self):
        self.cancelled = True

    def get_result_async(self):
        return SimpleNamespace(add_done_callback=lambda callback: None)


class PeopleBridgeTests(unittest.TestCase):
    def setUp(self):
        node = self.node = Bridge.__new__(Bridge)
        node.lock = threading.RLock()
        node.map_frame, node.odom_frame = "map", "odom"
        node.people = crowds.People()
        node.people.bind(["phone", 2, 7, 41, 99.])
        node.people_signature, node.people_options = None, {}
        node.people_geometry = (80, 60, .1, (-4., -3.))
        node.planar = lambda target, source: lambda x, y, yaw=0.: (x, y, yaw)
        node.get_clock = lambda: SimpleNamespace(now=lambda: SimpleNamespace(to_msg=lambda: "stamp"))
        self.published = []
        node.people_pub = SimpleNamespace(publish=self.published.append, topic_name="/farsight/people/map")
        self.info = []
        node.people_info_pub = SimpleNamespace(publish=self.info.append)

    def grid(self):
        return SimpleNamespace(header=SimpleNamespace(), info=SimpleNamespace(
            origin=SimpleNamespace(position=SimpleNamespace(), orientation=SimpleNamespace())))

    def data(self):
        message = self.published[-1]
        return np.asarray(message.data).reshape(message.info.height, message.info.width)

    def test_bootstrap_is_empty_unknown_and_does_not_publish_free_room_geometry(self):
        with patch.object(BRIDGE_MODULE, "OccupancyGrid", self.grid):
            self.node.publish_people()
            self.node.publish_people()
        message = self.published[-1]
        self.assertEqual((message.info.width, message.info.height, message.info.resolution), (80, 60, .1))
        self.assertEqual(message.header.frame_id, "map")
        self.assertEqual(len(self.published), 1)
        self.assertTrue((self.data() == -1).all())

    def test_filter_info_binds_the_existing_mask_with_native_occupancy_values(self):
        message = SimpleNamespace(header=SimpleNamespace())
        with patch.object(BRIDGE_MODULE, "CostmapFilterInfo", return_value=message):
            self.node.publish_people_info()
        self.assertEqual(self.info, [message])
        self.assertEqual((message.type, message.base, message.multiplier), (0, 0., 1.))
        self.assertEqual(message.filter_mask_topic, "/farsight/people/map")

    def test_actual_detection_callback_replaces_body_and_publishes_expired_clear(self):
        packet = dict(sensor_key=["phone", 2, 7, 42, 100.], fresh=True, age_ms=0,
                      boxes=[dict(cls="person", world=[0., 1.1, 0.])])
        clock = [10.]
        with patch.object(BRIDGE_MODULE, "OccupancyGrid", self.grid), patch.object(crowds.time, "monotonic", lambda: clock[0]):
            self.node.on_people(packet)
            self.assertEqual(self.data()[30, 40], 100)
            clock[0] += .1
            packet["boxes"][0]["world"] = [0., 1.1, -1.]
            self.node.on_people(packet)
            self.assertEqual(self.data()[30, 40], -1)
            self.assertEqual(self.data()[30, 50], 100)
            clock[0] += 1.
            self.node.publish_people()
            self.assertTrue((self.data() == -1).all())

    def test_slam_transform_moves_semantics_from_odom_into_map(self):
        self.node.planar = lambda target, source: lambda x, y, yaw=0.: (x + 1., y + .5, yaw)
        with patch.object(BRIDGE_MODULE, "OccupancyGrid", self.grid):
            self.node.on_people(dict(sensor_key=["phone", 2, 7, 42, 100.], fresh=True, age_ms=0,
                                     boxes=[dict(cls="person", world=[0., 1.1, 0.])]))
        self.assertEqual(self.data()[35, 50], 100)
        self.assertEqual(self.data()[30, 40], -1)

    def test_native_depth_identity_rebind_clears_the_previous_people_world(self):
        node = self.node
        node.generation, node.max_cloud_points, node.optical_frame = 2, 20000, "camera_optical"
        node.on_pose = lambda *a, **k: None
        node.cloud_pub = SimpleNamespace(publish=lambda message: None)
        node.image_pub = node.info_pub = SimpleNamespace(get_subscription_count=lambda: 0)
        node.people.update(dict(sensor_key=["phone", 2, 7, 42, 100.], fresh=True, age_ms=0,
                                boxes=[dict(cls="person", world=[0., 1., 0.])]))
        header = dict(generation=2, sensor_key=["phone", 3, 8, 1, 101.],
                      transform=np.eye(4).flatten().tolist(), fx=1., fy=1., cx=0., cy=0.)
        with patch.object(BRIDGE_MODULE.protocol, "decode_depth", return_value=(header, np.ones((1, 1)))), \
             patch.object(BRIDGE_MODULE, "stamp_from", return_value="stamp"), \
             patch.object(BRIDGE_MODULE, "cloud_msg", return_value=None):
            node.handle_depth(b"fixture")
        self.assertEqual(node.people.identity, ("phone", 3, 8))
        self.assertEqual(node.people.points(), ())


class BridgeAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.node = Bridge.__new__(Bridge)
        node = self.node
        node.lock = threading.RLock()
        node.generation = node.goal_generation = 2
        node.intent_active = True
        node.intent_goal_id = node.goal_id = "seek-2"
        node.goal_seq, node.goal_pending = 3, False
        node.goal_handle = Handle()
        node.goal_state, node.goal_message, node.path = "driving", "", []
        node.goal_sent_at, node.inactive_since, node.inactive_cancel_s = 0., None, .3
        node.cmd_times, node.exploring, node.explore = [], False, True
        node.nav_command, node.nav_command_at = (.2, 0.), 10.
        node.explore_config = frontiers.ExploreConfig()
        node.explorer = frontiers.Explorer(node.explore_config)
        node.swept = frontiers.SweptFootprint(.35)
        node.people, node.people_options = crowds.People(), {}
        node.people_signature = None
        node.publish_people = lambda: None
        node.local_motion = local_motion.LocalMotion()
        node.progress = frontiers.ProgressWindow()
        node.explore_session, node.explore_target = 1, None
        node.robot, node.global_grid, node.local_support, node.depth_snapshot = None, None, None, None
        node.pose_at = node.depth_at = node.global_grid_at = -math.inf
        node.frontiers, node.frontiers_odom = [], []
        node.clear_costmaps = []
        node.get_logger = lambda: SimpleNamespace(warning=lambda message: None)
        self.sent, self.goals = [], []
        node.send = self.sent.append
        node.send_goal = lambda goal_id, goal: self.goals.append((goal_id, goal))
        self.twist = SimpleNamespace(linear=SimpleNamespace(x=.2), angular=SimpleNamespace(z=0.))

    def explore_intent(self, active):
        self.node.on_intent(dict(generation=2, active=active, mode="explore" if active else "teleop", goal_id=None))

    def test_short_server_stop_resumes_the_same_explore_frontier(self):
        node = self.node
        node.goal_handle, node.intent_goal_id = None, None
        node.explore_resume_s, node.explore_paused_at = 2.0, None
        node.blacklist, node.escape_from, node.no_frontier_since, node.explore_finished = [], None, None, False
        clock = [100.0]
        with patch.object(BRIDGE_MODULE.time, "monotonic", lambda: clock[0]):
            self.explore_intent(True)
            node.explore_target, node.blacklist = (1.0, 2.0), [(3.0, 3.0, 99.0)]
            explorer = node.explorer
            self.explore_intent(False)  # video_stale: the server drops Explore for a moment
            clock[0] += 1.2
            self.explore_intent(True)
            self.assertEqual(node.explore_target, (1.0, 2.0))
            self.assertIs(node.explorer, explorer)
            self.assertEqual(len(node.blacklist), 1)
            self.explore_intent(False)  # a real release: back after more than 2 s starts fresh
            clock[0] += 2.5
            self.explore_intent(True)
            self.assertIsNone(node.explore_target)
            self.assertIsNot(node.explorer, explorer)
            self.assertEqual(node.blacklist, [])

    def test_sensor_reset_never_resumes_the_old_explore(self):
        node = self.node
        node.explore_resume_s, node.explore_paused_at = 2.0, None
        node.blacklist, node.escape_from, node.no_frontier_since, node.explore_finished = [], None, None, False
        node.exploring, node.explore_target = True, (1.0, 2.0)
        node.explore_paused_at = 0.0
        node.on_reset(dict(generation=2))
        self.explore_intent(True)
        self.assertIsNone(node.explore_target)

    def test_forwarded_velocity_retains_current_sensor_generation(self):
        self.node.on_cmd_vel(self.twist)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0].get("generation"), 2)
        self.assertEqual(self.sent[0]["goal_id"], "seek-2")

    def test_pending_replacement_does_not_relabel_the_previous_goals_twist(self):
        self.node.goal_pending = True  # old handle exists until the new goal is accepted
        self.node.on_cmd_vel(self.twist)
        self.assertEqual(self.sent, [])
        self.node.on_accepted(3, SimpleNamespace(result=lambda: Handle()))
        self.node.on_cmd_vel(self.twist)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0]["goal_id"], "seek-2")

    def test_rejected_replacement_cannot_forward_the_old_handles_twist(self):
        self.node.goal_pending = True
        rejected = Handle()
        rejected.accepted = False
        self.node.on_accepted(3, SimpleNamespace(result=lambda: rejected))
        self.node.on_cmd_vel(self.twist)
        self.assertEqual(self.sent, [])

    def test_reset_discards_active_intent_and_cached_nav_command(self):
        self.node.exploring = True
        self.node.people.bind(["phone", 2, 7, 41, 99.])
        self.node.people.update(dict(sensor_key=["phone", 2, 7, 42, 100.], fresh=True, age_ms=0,
                                    boxes=[dict(cls="person", world=[0., 1., 0.])]))
        self.node.on_reset(dict(generation=3))
        self.assertFalse(self.node.intent_active)
        self.assertFalse(self.node.exploring)
        self.assertIsNone(self.node.nav_command)
        self.assertEqual(self.node.people.points(), ())
        self.assertIsNone(self.node.people.identity)
        self.node.on_cmd_vel(self.twist)
        self.assertEqual(self.sent, [])

    def test_old_or_missing_generation_intent_cannot_start_a_goal(self):
        self.node.intent_active = False
        for fields in ({"generation": 1}, {}):
            self.node.on_intent(dict(active=True, mode="seek", goal_id="seek-1", goal=dict(x=2., y=0.), **fields))
        self.assertEqual(self.goals, [])
        self.assertFalse(self.node.intent_active)

    def test_release_cancels_a_goal_that_has_not_been_accepted(self):
        self.node.goal_pending, self.node.goal_handle = True, None
        self.node.on_intent(dict(generation=2, active=False, mode="seek", goal_id="seek-2", goal=dict(x=2., y=0.)))
        self.assertFalse(self.node.intent_active)
        self.assertFalse(self.node.goal_pending)
        late_handle = Handle()
        self.node.on_accepted(3, SimpleNamespace(result=lambda: late_handle))
        self.assertTrue(late_handle.cancelled)
        self.assertIsNone(self.node.goal_handle)

    def test_velocity_from_a_previous_goal_generation_is_not_forwarded(self):
        self.node.goal_generation = 1
        self.node.on_cmd_vel(self.twist)
        self.assertEqual(self.sent, [])

    def test_status_echoes_generation_before_it_is_enqueued(self):
        self.node.ready = lambda: True
        self.node.recoveries = 0
        self.node.send_status()
        self.node.generation = 3
        self.assertEqual(self.sent[0].get("generation"), 2)

    def test_reset_during_goal_transform_cannot_send_the_old_goal(self):
        module = BRIDGE_MODULE
        pose = lambda: SimpleNamespace(header=SimpleNamespace(frame_id=None, stamp=None),
                                       pose=SimpleNamespace(position=SimpleNamespace(x=0., y=0.),
                                                            orientation=SimpleNamespace(x=0., y=0., z=0., w=1.)))
        self.node.ready = lambda: True
        self.node.odom_frame, self.node.map_frame = "odom", "map"
        sends = []
        self.node.navigate = SimpleNamespace(send_goal_async=lambda *args, **kwargs: sends.append(args) or
                                            SimpleNamespace(add_done_callback=lambda callback: None))

        def reset_in_transform(value, frame):
            self.node.on_reset(dict(generation=3))
            return value

        self.node.to_frame = reset_in_transform
        with patch.object(module, "PoseStamped", pose), \
                patch.object(module, "Time", lambda: SimpleNamespace(to_msg=lambda: None)), \
                patch.object(module, "NavigateToPose", SimpleNamespace(Goal=lambda: SimpleNamespace(pose=None))):
            Bridge.send_goal(self.node, "seek-2", dict(x=2., y=0.))
        self.assertEqual(sends, [])


class ExploreStallTests(unittest.TestCase):
    """Explore stall/skip rules on a bare node: a goal mid-turn is not dropped,
    a shrinking heading counts as progress, and a fresh skip favours forward."""

    def make_node(self):
        node = Bridge.__new__(Bridge)
        node.lock = threading.RLock()
        node.generation = node.goal_generation = 2
        node.intent_active, node.intent_goal_id = True, None
        node.exploring, node.explore = True, True
        node.explore_session, node.explore_seq = 1, 0
        node.explore_target, node.explore_since = None, time.monotonic()
        node.explore_fallback, node.explore_blocked, node.explore_finished = False, False, False
        node.explore_stall_s, node.explore_goal_timeout_s = 3.0, 25.0
        node.explore_blacklist_s, node.explore_escape_m = 30.0, 1.5
        node.explore_stall_heading_rad, node.explore_skip_forward_rad = 1.0, math.radians(60)
        node.explore_config = frontiers.ExploreConfig()
        # The measured-approach scorer rejects everything here so ticks land on
        # the geometric fallback chooser, where the stall/skip rules are visible.
        node.explorer = SimpleNamespace(choose=lambda *a, **k: None, release=lambda: None,
                                      committed=None)
        node.swept = frontiers.SweptFootprint(0.35)
        node.people, node.people_options = crowds.People(), {}
        node.progress = frontiers.ProgressWindow()
        node.progress_best, node.progress_at = math.inf, time.monotonic()
        node.heading_best = math.inf
        node.blacklist = []
        node.escape_from, node.escape_until = None, 0.0
        node.no_frontier_since = None
        node.frontiers, node.frontiers_odom = [], []
        node.local_support, node.depth_snapshot = None, None
        node.robot = (2.5, 2.5, 0.0)
        now = time.monotonic()
        node.pose_at = node.depth_at = node.global_grid_at = now
        node.goal_handle, node.goal_pending, node.goal_sent_at = Handle(), False, now
        node.goal_seq, node.goal_id = 1, "explore-1"
        node.goal_state, node.goal_message, node.path = "driving", "", []
        node.nav_command, node.nav_command_at = None, -math.inf
        node.inactive_since, node.inactive_cancel_s = None, 0.3
        node.map_frame, node.odom_frame = "map", "odom"
        node.ready = lambda: True
        node.get_logger = lambda: SimpleNamespace(warning=lambda *a, **k: None,
                                                  error=lambda *a, **k: None)
        node.send = lambda message: None
        node.goals = []
        node.send_goal = lambda goal_id, goal, explore_session=None: node.goals.append((goal_id, goal)) or True
        node.global_grid = SimpleNamespace(
            info=SimpleNamespace(resolution=0.05, height=100, width=100,
                                 origin=SimpleNamespace(position=SimpleNamespace(x=0.0, y=0.0))),
            data=[0] * (100 * 100))
        return node

    def tick(self, node, found):
        with patch.object(frontiers, "find", return_value=found):
            node.explore_tick()

    def test_people_override_previously_driven_floor_in_actual_explore_callback(self):
        node = self.make_node()
        node.explorer = frontiers.Explorer(node.explore_config)
        node.people.bind(["phone", 2, 7, 41, 99.])
        node.people.update(dict(sensor_key=["phone", 2, 7, 42, 100.], fresh=True, age_ms=0,
                                boxes=[dict(cls="person", world=[-2.5, 1., -3.])]))
        node.swept.record((3., 2.5, 0.))
        self.tick(node, [])
        cells, resolution, origin = node.lethal
        self.assertTrue(cells[int((2.5 - origin[1]) / resolution), int((3. - origin[0]) / resolution)])

    def test_goal_behind_the_rover_is_not_blacklisted_mid_turn(self):
        node = self.make_node()
        node.explore_target = (0.5, 2.5)                       # dead astern: heading error pi
        node.progress_at = time.monotonic() - 10.0           # 10 s without 0.3 m of progress
        ahead = frontiers.Frontier((4.5, 2.5), (4.5, 2.5), 1.0)
        self.tick(node, [ahead])
        self.assertEqual(node.blacklist, [])                 # mid-turn is not a stall
        self.assertEqual(node.goals[-1][1]["x"], 4.5)        # and a forward frontier was sent

        node2 = self.make_node()
        node2.explore_target = (0.5, 2.5)
        node2.robot = (2.5, 2.5, math.pi)                    # now facing the goal
        node2.progress_at = time.monotonic() - 10.0
        self.tick(node2, [ahead])
        self.assertEqual(len(node2.blacklist), 1)            # facing it with no progress: stalled
        self.assertAlmostEqual(node2.blacklist[0][0], 0.5)
        self.assertAlmostEqual(node2.blacklist[0][1], 2.5)

    def test_closing_heading_counts_as_progress(self):
        node = self.make_node()
        node.robot = (2.5, 2.5, math.pi - 2.0)               # 2.0 rad off the astern target
        node.explore_target = (0.5, 2.5)
        node.heading_best = 2.9                              # the turn is closing in
        node.progress_at = time.monotonic() - 10.0
        self.tick(node, [frontiers.Frontier((4.5, 2.5), (4.5, 2.5), 1.0)])
        self.assertGreater(node.progress_at, time.monotonic() - 1.0)
        self.assertEqual(node.blacklist, [])

    def test_a_turn_that_stops_closing_is_still_a_stall(self):
        node = self.make_node()
        node.explore_target = (0.5, 2.5)
        node.heading_best = math.pi
        node.progress_at = time.monotonic() - 4.0
        self.tick(node, [frontiers.Frontier((4.5, 2.5), (4.5, 2.5), 1.0)])
        self.assertEqual(len(node.blacklist), 1)
        self.assertAlmostEqual(node.blacklist[0][0], 0.5)

    def test_oscillating_heading_does_not_renew_progress(self):
        node = self.make_node()
        node.explore_target = (0.5, 2.5)
        node.robot = (2.5, 2.5, math.pi - 2.0)
        node.heading_best = 1.9
        node.progress_at = time.monotonic() - 4.0
        self.tick(node, [frontiers.Frontier((4.5, 2.5), (4.5, 2.5), 1.0)])
        self.assertEqual(len(node.blacklist), 1)
        self.assertAlmostEqual(node.blacklist[0][0], 0.5)

    def test_after_a_skip_the_pick_prefers_the_heading_cone(self):
        east = frontiers.Frontier((4.5, 2.5), (4.5, 2.5), 0.5)                    # dead ahead
        flank = frontiers.Frontier((3.02, 4.43), (3.02, 4.43), 4.0)               # ~75 deg off, broader

        skipped = self.make_node()
        skipped.blacklist = [(9.9, 9.9, time.monotonic())]     # a goal was just dropped
        self.tick(skipped, [east, flank])
        self.assertAlmostEqual(skipped.goals[-1][1]["x"], 4.5)
        self.assertAlmostEqual(skipped.goals[-1][1]["y"], 2.5)

        fresh = self.make_node()                               # no recent skip: broad flank wins
        self.tick(fresh, [east, flank])
        self.assertAlmostEqual(fresh.goals[-1][1]["x"], 3.02, places=2)
        self.assertAlmostEqual(fresh.goals[-1][1]["y"], 4.43, places=2)

    def test_skipped_forward_goal_does_not_hide_a_usable_flank(self):
        node = self.make_node()
        node.explore_target = (4.5, 2.5)
        node.progress_at = time.monotonic() - 4.0
        east = frontiers.Frontier((4.5, 2.5), (4.5, 2.5), 1.0)
        flank = frontiers.Frontier((3.02, 4.43), (3.02, 4.43), 1.0)
        self.tick(node, [east, flank])
        self.assertEqual(len(node.blacklist), 1)
        self.assertEqual(len(node.goals), 1)
        self.assertAlmostEqual(node.goals[0][1]["x"], 3.02)
        self.assertAlmostEqual(node.goals[0][1]["y"], 4.43)

    def test_rejected_forward_view_edge_does_not_hide_a_usable_flank(self):
        node = self.make_node()
        node.blacklist = [(9.9, 9.9, time.monotonic())]
        view_edge = frontiers.Frontier((3.2, 2.5), (3.2, 2.5), 1.0)
        flank = frontiers.Frontier((3.02, 4.43), (3.02, 4.43), 1.0)
        self.tick(node, [view_edge, flank])
        self.assertEqual(len(node.goals), 1)
        self.assertAlmostEqual(node.goals[0][1]["x"], 3.02)
        self.assertAlmostEqual(node.goals[0][1]["y"], 4.43)

    def test_no_frontier_in_the_sketch_sends_an_open_space_goal_instead_of_scanning(self):
        from ros2.farsight_nav.test.test_open_space import sketch
        grid, resolution, origin, robot = sketch()
        node = self.make_node()
        node.robot, node.goal_handle = robot, None
        node.explorer = frontiers.Explorer(node.explore_config)
        node.global_grid = SimpleNamespace(
            info=SimpleNamespace(resolution=resolution, height=grid.shape[0], width=grid.shape[1],
                                 origin=SimpleNamespace(position=SimpleNamespace(x=origin[0], y=origin[1]))),
            data=grid.ravel().tolist())
        self.tick(node, [])
        self.assertEqual(len(node.goals), 1, 'A fully mapped room still gets a driving destination')
        self.assertGreater(node.goals[0][1]['x'], .8)
        self.assertIn('open space', node.goal_message)
        self.assertIsNone(node.no_frontier_since)
        # A second camera/map update keeps the chosen route instead of sending
        # another target and restarting Nav2's turn/planning phase.
        node.goal_handle = Handle()
        self.tick(node, [])
        self.assertEqual(len(node.goals), 1)


if __name__ == "__main__":
    unittest.main()
