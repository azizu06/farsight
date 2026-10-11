"""Exercise bridge callbacks without starting ROS, a WebSocket, or any hardware."""

import importlib
import math
from pathlib import Path
import sys
import threading
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from farsight_nav import frontiers, local_motion  # noqa: E402


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
        self.node.on_reset(dict(generation=3))
        self.assertFalse(self.node.intent_active)
        self.assertFalse(self.node.exploring)
        self.assertIsNone(self.node.nav_command)
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


if __name__ == "__main__":
    unittest.main()
