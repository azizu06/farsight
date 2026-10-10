import os
import time
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from jetson.mapping.server import create_app


class ServerIdentityTests(unittest.TestCase):
    def camera_ready(self, client):
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            health = client.get('/health').json()
            if health['camera']:
                return health
            time.sleep(.01)
        self.fail('Simulated camera failed to start')

    def test_optional_identity_routes_and_existing_face_contract(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            app = create_app(demo=True, simulate_rover=True, detector='none')
        with TestClient(app) as client:
            status = client.get('/phone/identity/status')
            self.assertEqual(status.status_code, 200)
            self.assertFalse(status.json()['enabled'])
            self.assertFalse(status.json()['ready'])
            self.assertEqual(client.get('/phone/identity/enroll').status_code, 200)
            health = self.camera_ready(client)
            self.assertFalse(health['face_detector'])
            self.assertEqual(health['camera_source'], 'SIMULATED')
            self.assertTrue(client.get('/control/status').json()['simulated'])
        self.assertTrue(app.state.identity.closed)

    def test_bad_identity_models_leave_camera_control_and_drill_available(self):
        with mock.patch.dict(os.environ, {'FARSIGHT_IDENTITY_YUNET': '/missing/yunet.onnx',
                                         'FARSIGHT_IDENTITY_SFACE': '/missing/sface.onnx'}):
            app = create_app(demo=True, simulate_rover=True, detector='none')
        with TestClient(app) as client:
            status = client.get('/phone/identity/status').json()
            self.assertTrue(status['enabled'])
            self.assertFalse(status['ready'])
            self.assertTrue(status['error'])
            self.camera_ready(client)
            self.assertEqual(client.get('/camera.jpg').status_code, 200)
            self.assertEqual(client.get('/control/status').status_code, 200)
            self.assertFalse(client.get('/mission/drill').json()['face_detector'])


if __name__ == '__main__':
    unittest.main()
