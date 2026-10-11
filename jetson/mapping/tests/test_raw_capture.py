import time
import unittest
from unittest.mock import patch
import numpy as np
from fastapi import HTTPException
from fastapi.testclient import TestClient
from jetson.mapping.server import create_app
from jetson.mapping.raw_capture import mount
from jetson.mapping.phone import decode_frame
from jetson.mapping.raw_capture import RawCapture
from jetson.mapping.state import MapState
from jetson.mapping.tests.test_mapping import bundle, flat_frame


class RawCaptureTests(unittest.TestCase):
    def setUp(self):
        self.state = MapState(fusion_bypass=True)
        self.state.start_session(('test', 1), 'Fixture')
        self.capture = RawCapture(self.state)
        self.state.frame_observers.append(self.capture.observe)
        self.frame = decode_frame(bundle(flat_frame()), ('test',1))
        self.frame.header['_received_monotonic'] = time.monotonic()
        self.state.process(self.frame, socket_generation=self.state.socket_generation)

    def test_native_same_frame_roundtrip_and_cursor(self):
        body, headers = self.capture.snapshot()
        decoded = decode_frame(body, ('test',1))
        self.assertEqual(decoded.jpeg, self.frame.jpeg)
        np.testing.assert_array_equal(decoded.depth, self.frame.depth)
        np.testing.assert_array_equal(decoded.confidence, self.frame.confidence)
        np.testing.assert_array_equal(decoded.transform, self.frame.transform)
        self.assertEqual(self.capture.snapshot(headers['X-Capture-Key'])[0], None)
        self.assertIs(self.capture.snapshot()[0], body)

    def test_reset_disconnect_limited_stale_and_same_identity_reconnect_reject(self):
        for invalidate in [lambda: self.state.clear(),
                           lambda: setattr(self.state,'connected',False),
                           lambda: setattr(self.state,'tracking','limited'),
                           lambda: self.state.start_session(('test',1),'reconnected')]:
            with self.subTest(invalidate=invalidate):
                self.setUp();invalidate()
                with self.assertRaises(HTTPException) as error:self.capture.snapshot()
                self.assertEqual(error.exception.status_code,503)
        self.setUp()
        with patch('jetson.mapping.raw_capture.time.monotonic',return_value=time.monotonic()+3):
            with self.assertRaises(HTTPException):self.capture.snapshot()


class RawMountTests(unittest.TestCase):
    def test_explicit_mount_precedes_static_catchall_and_default_is_off(self):
        app=create_app(simulate_rover=True,detector='none')
        self.assertFalse(any(getattr(r,'path',None)=='/phone/frame.bin' for r in app.routes))
        mount(app)
        with TestClient(app) as client:
            self.assertEqual(client.get('/phone/frame.bin').status_code,503)

    def test_env_opt_in_mounts_endpoint(self):
        with patch.dict('os.environ',{'FARSIGHT_RAW_CAPTURE':'1'}):
            app=create_app(simulate_rover=True,detector='none')
        self.assertTrue(any(getattr(r,'path',None)=='/phone/frame.bin' for r in app.routes))
