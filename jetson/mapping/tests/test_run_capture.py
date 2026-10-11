import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np
from fastapi.testclient import TestClient

from jetson.mapping.phone import decode_frame
from jetson.mapping.run_capture import PhoneRunCapture
from jetson.mapping.server import create_app
from jetson.mapping.tests.test_mapping import bundle, flat_frame


def capture_frame(frame_id=0, epoch=1):
    frame = flat_frame()
    # Native JPEG is twice the depth dimensions. Do not save depth-scaled K.
    _, jpeg = cv2.imencode('.jpg', np.full((12, 16, 3), 100, np.uint8))
    frame.jpeg = jpeg.tobytes()
    transform = np.array([[0, 0, 1, 4], [0, 1, 0, 5], [-1, 0, 0, 6], [0, 0, 0, 1.]])
    return decode_frame(bundle(frame, frame_id=frame_id, t_capture=frame_id / 3,
                              map_epoch=epoch, transform=transform.flatten(order='F').tolist()),
                        ('test', epoch))


class RunCaptureTests(unittest.TestCase):
    def test_native_jpeg_intrinsics_opengl_pose_sampling_and_run_reset(self):
        with tempfile.TemporaryDirectory() as root:
            archive = PhoneRunCapture(root)
            archive.start()
            frame = capture_frame()
            archive.offer_frame(frame, now=0)
            archive.offer_frame(capture_frame(1), now=.2)  # skipped at 2.5 Hz
            archive.offer_frame(capture_frame(2), now=.4)
            archive.offer_frame(capture_frame(3, epoch=2), now=.5)
            reconnect = capture_frame(4, epoch=2)
            reconnect.header['_mapping_socket_generation'] = 2
            archive.offer_frame(reconnect, now=.6)
            archive.close()
            runs = sorted(Path(root).glob('*/transforms.json'))
            self.assertEqual(len(runs), 3)
            documents = [json.loads(p.read_text()) for p in runs]
            first = next(d for d in documents if d['map_epoch'] == 1)
            self.assertEqual([f['frame_id'] for f in first['frames']], [0, 2])
            saved = first['frames'][0]
            self.assertEqual({k: saved[k] for k in ('fl_x', 'fl_y', 'cx', 'cy', 'w', 'h')},
                             dict(fl_x=16, fl_y=16, cx=8, cy=6, w=16, h=12))
            np.testing.assert_equal(saved['transform_matrix'], frame.transform)
            run = next(p.parent for p, d in zip(runs, documents) if d['map_epoch'] == 1)
            self.assertEqual((run / saved['file_path']).read_bytes(), frame.jpeg)
            self.assertEqual(archive.status()['written'], 4)
            self.assertEqual(sorted(d['socket_generation'] for d in documents), [0, 0, 2])

    def test_blocked_disk_drops_bounded_queue_without_stalling_health_or_control(self):
        with tempfile.TemporaryDirectory() as root, patch.dict('os.environ', {
                'FARSIGHT_RUN_CAPTURE': '1', 'FARSIGHT_RUN_CAPTURE_DIR': root}):
            entered, release = threading.Event(), threading.Event()
            original = Path.write_bytes

            def blocked_write(path, data):
                entered.set()
                release.wait(10)
                return original(path, data)

            with patch.object(Path, 'write_bytes', blocked_write):
                app = create_app(detector='none', simulate_rover=True)
                with TestClient(app) as client:
                    archive = app.state.run_capture
                    try:
                        state = app.state.mapping
                        state.start_session(('test', 1), 'test phone')
                        state.process(capture_frame(), socket_generation=state.socket_generation)
                        self.assertTrue(entered.wait(2))
                        started = time.monotonic()
                        for i in range(1, 30):
                            archive.offer_frame(capture_frame(i), now=1000 + i)
                        self.assertEqual(archive.status()['queued'], 8)
                        self.assertGreater(archive.status()['dropped'], 0)
                        self.assertTrue(state.lock.acquire(blocking=False))
                        state.lock.release()
                        self.assertEqual(client.get('/health').status_code, 200)
                        self.assertEqual(client.get('/control/status').status_code, 200)
                        self.assertLess(time.monotonic() - started, 1)
                        self.assertFalse(release.is_set())
                    finally:
                        release.set()

    def test_demo_default_and_explicit_disable_without_synthetic_archive(self):
        with patch.dict('os.environ', {}, clear=True):
            self.assertIsNone(create_app(detector='none').state.run_capture)
            self.assertIsNotNone(create_app(detector='none', demo_mode=True).state.run_capture)
            self.assertIsNone(create_app(detector='none', demo=True, demo_mode=True).state.run_capture)
        with patch.dict('os.environ', {'FARSIGHT_RUN_CAPTURE': '0'}):
            self.assertIsNone(create_app(detector='none', demo_mode=True).state.run_capture)


if __name__ == '__main__':
    unittest.main()
