import math
import time
import unittest

import numpy as np

from jetson.mapping.heading import measured_heading
from jetson.mapping.state import MapState
from jetson.control.state import ControlState


class HeadingTests(unittest.TestCase):
    def setUp(self):
        self.mapping = MapState()
        self.mapping.identity = ('phone', 1)
        self.mapping.connected = True
        self.mapping.tracking = 'normal'
        self.mapping.last_received = time.monotonic()

    def test_actual_camera_forward_yaw_and_existing_mount_offset(self):
        angle = math.radians(30)
        c, s = math.cos(angle), math.sin(angle)
        self.mapping.pose = np.array([[c, 0, s, 0], [0, 1, 0, 0], [-s, 0, c, 0], [0, 0, 0, 1]])
        value = measured_heading(self.mapping)
        self.assertAlmostEqual(value['yaw_rad'], angle)
        self.assertEqual((value['session_id'], value['map_epoch'], value['source']), ('phone', 1, 'arkit'))
        self.assertAlmostEqual(measured_heading(self.mapping, yaw_offset=.1)['yaw_rad'], angle+.1)
        # Phone roll changes image orientation, not its optical forward direction.
        roll = np.array([[0, -1, 0, 0], [1, 0, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]])
        self.mapping.pose = self.mapping.pose @ roll
        self.assertAlmostEqual(measured_heading(self.mapping)['yaw_rad'], angle)

    def test_no_command_history_or_old_scan_is_used_as_heading(self):
        self.mapping.connected = False
        self.assertIsNone(measured_heading(self.mapping))
        self.mapping.connected = True
        self.mapping.tracking = 'limited'
        self.assertIsNone(measured_heading(self.mapping))
        self.mapping.tracking = 'normal'
        self.mapping.last_received -= 2
        self.assertIsNone(measured_heading(self.mapping))

    def test_optional_heading_reaches_regular_and_bonded_state_status_without_nav2(self):
        state = ControlState(simulated=True, heading=lambda: measured_heading(self.mapping))
        value = state.status()['heading']
        self.assertAlmostEqual(value['yaw_rad'], 0)
        self.assertEqual(value['socket_generation'], self.mapping.socket_generation)
        self.assertNotIn('heading', ControlState(simulated=True).status())

    def test_assembled_server_publishes_heading_on_both_control_transports(self):
        from fastapi.testclient import TestClient
        from jetson.mapping.server import create_app
        for path in ('/ws/control', '/ws/control/bonded'):
            with self.subTest(path=path):
                app = create_app(simulate_rover=True, detector='none', bonded_control=True, pan_mount='none')
                with TestClient(app) as client:
                    mapping = app.state.mapping
                    mapping.identity, mapping.connected, mapping.tracking = ('fixture-phone', 1), True, 'normal'
                    mapping.last_received = time.monotonic()
                    with client.websocket_connect(path) as ws:
                        if path.endswith('/bonded'):
                            ws.send_json(dict(type='attach', version=1))
                            self.assertEqual(ws.receive_json()['type'], 'attached')
                        status = ws.receive_json()
                        self.assertEqual(status['heading']['session_id'], 'fixture-phone')
                        self.assertAlmostEqual(status['heading']['yaw_rad'], 0)
                        angle = math.radians(-40)
                        c, s = math.cos(angle), math.sin(angle)
                        mapping.pose = np.array([[c, 0, s, 0], [0, 1, 0, 0], [-s, 0, c, 0], [0, 0, 0, 1]])
                        mapping.last_received = time.monotonic()
                        for _ in range(20):
                            status = ws.receive_json()
                            if abs(status['heading']['yaw_rad'] - angle) < 1e-6:
                                break
                        self.assertAlmostEqual(status['heading']['yaw_rad'], angle)
                        self.assertEqual((status['left_pwm'], status['right_pwm']), (0, 0))

    def test_servo_camera_pose_is_not_published_as_chassis_heading(self):
        from jetson.mapping.server import create_app
        app = create_app(simulate_rover=True, detector='none', pan_mount='servo')
        mapping = app.state.mapping
        mapping.identity, mapping.connected, mapping.tracking = ('fixture-phone',1), True,'normal'
        mapping.last_received = time.monotonic()
        self.assertIsNotNone(measured_heading(mapping))
        self.assertIsNone(app.state.control.state.status()['heading'])


if __name__ == '__main__':
    unittest.main()
