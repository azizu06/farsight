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
from jetson.mapping.run_capture import PhoneRunCapture, merge_run_points
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


def save_frames(root, *frames, **settings):
    archive = PhoneRunCapture(root, **settings)
    archive.start()
    try:
        for i, frame in enumerate(frames):
            archive.offer_frame(frame, now=i)
    finally:
        archive.close()
    return archive, sorted(Path(root).glob('*/transforms.json'))


class RunCaptureTests(unittest.TestCase):
    def test_saved_frame_points_recover_a_ply_when_finalization_is_interrupted(self):
        with tempfile.TemporaryDirectory() as root:
            original = Path.replace

            def interrupted(path, target):
                if path.name == 'dense.ply.tmp':
                    raise OSError('interrupted final export')
                return original(path, target)

            with patch.object(Path, 'replace', interrupted):
                archive, (manifest,) = save_frames(root, capture_frame())
            document = json.loads(manifest.read_text())
            self.assertNotIn('point_cloud', document)
            self.assertEqual(document['frames'][0]['points_count'], 48)
            self.assertEqual(archive.status()['failed'], 1)
            recovered = merge_run_points(manifest.parent)
            self.assertEqual(recovered['points'], 48)
            self.assertTrue(json.loads(manifest.read_text())['point_cloud']['complete'])

    def test_long_stream_rotates_segments_to_bound_manifest_memory(self):
        with tempfile.TemporaryDirectory() as root:
            _, manifests = save_frames(root, *(capture_frame(i) for i in range(3)), max_frames=2)
            documents = [json.loads(p.read_text()) for p in manifests]
            self.assertEqual(sorted(len(d['frames']) for d in documents), [1, 2])
            self.assertEqual(sorted(f['frame_id'] for d in documents for f in d['frames']), [0, 1, 2])
            self.assertTrue(all(d['point_cloud']['complete'] for d in documents))

    def test_large_frames_are_bounded_and_ply_cap_keeps_frame_points_for_recovery(self):
        with tempfile.TemporaryDirectory() as root:
            frame = capture_frame()
            frame.depth = np.full((120, 160), 2, np.float32)
            frame.confidence = np.full((120, 160), 2, np.uint8)
            frame.rgb = np.full((120, 160, 3), 100, np.uint8)
            frame.calibration = type(frame.calibration)(160, 120, 160, 160, 80, 60)
            _, (manifest,) = save_frames(root, frame, max_merge_points=2)
            document = json.loads(manifest.read_text())
            saved = document['frames'][0]
            self.assertEqual(saved['points_count'], 12288)
            self.assertEqual((manifest.parent / saved['points_path']).stat().st_size, 12288 * 15)
            self.assertEqual(document['point_cloud']['points'], 2)
            self.assertTrue(document['point_cloud']['truncated'])

    def test_limited_tracking_keeps_the_photo_without_fusing_unreliable_world_points(self):
        with tempfile.TemporaryDirectory() as root:
            frame = capture_frame()
            frame.header['tracking'] = 'limited'
            _, (manifest,) = save_frames(root, frame)
            document = json.loads(manifest.read_text())
            saved = document['frames'][0]
            self.assertEqual(saved['points_count'], 0)
            self.assertEqual((manifest.parent / saved['points_path']).read_bytes(), b'')
            self.assertEqual((manifest.parent / saved['file_path']).read_bytes(), frame.jpeg)
            self.assertEqual(document['point_cloud']['points'], 0)

    def test_stopping_the_stream_finalizes_without_stopping_the_service(self):
        with tempfile.TemporaryDirectory() as root:
            archive = PhoneRunCapture(root, idle_seconds=.1)
            archive.start()
            try:
                archive.offer_frame(capture_frame(), now=0)
                deadline = time.monotonic() + 2
                while not list(Path(root).glob('*/dense.ply')) and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertEqual(len(list(Path(root).glob('*/dense.ply'))), 1)
                archive.offer_frame(capture_frame(1), now=1)
            finally:
                archive.close()
            self.assertEqual(len(list(Path(root).glob('*/dense.ply'))), 2)

    def test_archive_budget_preserves_old_files_and_drops_without_writing_pairs(self):
        with tempfile.TemporaryDirectory() as root:
            old = Path(root) / 'old-photo.jpg'
            old.write_bytes(b'x' * 2000)
            archive, _ = save_frames(root, capture_frame(), max_bytes=2000, max_merge_points=2)
            self.assertEqual(old.read_bytes(), b'x' * 2000)
            self.assertFalse(list(Path(root).glob('*/images/*')))
            self.assertEqual(archive.status()['written'], 0)
            self.assertEqual(archive.status()['dropped'], 1)
            self.assertTrue(archive.status()['disk_limited'])

    def test_close_and_session_change_publish_separate_deduplicated_run_ply(self):
        with tempfile.TemporaryDirectory() as root:
            other = capture_frame(2, epoch=2)
            other.transform[0, 3] += 10
            _, manifests = save_frames(root, capture_frame(), capture_frame(1), other)
            for manifest in manifests:
                document = json.loads(manifest.read_text())
                cloud = document['point_cloud']
                self.assertTrue(cloud['complete'])
                self.assertEqual(cloud['points'], 48)  # repeated observations are not duplicated
                header, records = (manifest.parent / cloud['file_path']).read_bytes().split(b'end_header\n', 1)
                self.assertIn(b'format binary_little_endian 1.0', header)
                self.assertIn(b'element vertex 48', header)
                xyz = np.frombuffer(records, dtype=np.dtype([
                    ('xyz', '<f4', (3,)), ('rgb', 'u1', (3,))]))['xyz']
                expected = [2, 5, 6] if document['map_epoch'] == 1 else [12, 5, 6]
                self.assertTrue(np.any(np.all(xyz == expected, axis=1)))

    def test_measured_world_points_and_rgb_survive_with_the_matching_jpeg(self):
        with tempfile.TemporaryDirectory() as root:
            frame = capture_frame()
            frame.confidence[-1, -1] = 0
            _, (manifest,) = save_frames(root, frame)
            saved = json.loads(manifest.read_text())['frames'][0]
            points_path = manifest.parent / saved['points_path']
            data = np.frombuffer(points_path.read_bytes(), dtype=np.dtype([
                ('xyz', '<f4', (3,)), ('rgb', 'u1', (3,))]))
            self.assertEqual(saved['points_count'], 47)
            self.assertEqual(len(data), 47)
            # Independent ARKit example: depth 2m at the optical centre,
            # transformed by the JPEG's pose, is world (2,5,6).
            self.assertTrue(np.any(np.all(data['xyz'] == [2, 5, 6], axis=1)))
            np.testing.assert_array_equal(data['rgb'], np.full((47, 3), 100))
            np.testing.assert_array_equal(saved['transform_matrix'], frame.transform)
            self.assertEqual(points_path.parent, (manifest.parent / saved['file_path']).parent)
            self.assertEqual((manifest.parent / saved['file_path']).read_bytes(), frame.jpeg)

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
