import json
import struct
import time
import unittest

import cv2
import numpy as np
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from jetson.mapping.demo import simulated_frame
from jetson.mapping.geometry import Calibration, POINT_DTYPE
from jetson.mapping.phone import RGBDFrame, decode_frame, reconstruct, rigid_transform
from jetson.mapping.server import create_app
from jetson.mapping.state import MapState
from jetson.mapping.surfaces import SurfaceMap
from jetson.mapping.voxel import VoxelMap


def bundle(frame, **changes):
    h, w = frame.depth.shape
    image = cv2.imdecode(np.frombuffer(frame.jpeg, np.uint8), cv2.IMREAD_COLOR)
    ih, iw = image.shape[:2]
    k = frame.calibration
    header = dict(frame.header, image=dict(width=iw, height=ih, jpeg_len=len(frame.jpeg),
                  intrinsics=[k.fx * iw / w, 0, 0, 0, k.fy * ih / h, 0,
                              k.cx * iw / w, k.cy * ih / h, 1], orientation="landscape_right"),
                  depth=dict(width=w, height=h, format="float32_m", len=w * h * 4),
                  confidence=dict(width=w, height=h, format="uint8_0_2", len=w * h))
    header.update(changes)
    metadata = json.dumps(header).encode()
    return struct.pack('<I', len(metadata)) + metadata + frame.jpeg + frame.depth.astype('<f4').tobytes() + frame.confidence.tobytes()


def flat_frame():
    width, height = 8, 6
    rgb = np.full((height, width, 3), 127, np.uint8)
    depth = np.full((height, width), 2, np.float32)
    header = dict(version=1, type='frame', session_id='test', map_epoch=1, frame_id=0,
                  t_capture=0.0, tracking='normal', transform=np.eye(4).flatten(order='F').tolist())
    _, jpeg = cv2.imencode('.jpg', rgb)
    return RGBDFrame(header, rgb, depth, np.full_like(depth, 2, dtype=np.uint8),
                     Calibration(width, height, 8, 8, 4, 3), np.eye(4), jpeg.tobytes())


class GeometryTests(unittest.TestCase):
    def test_arkit_camera_axis_and_column_major_pose(self):
        frame = flat_frame()
        frame.transform[:3, 3] = [1, 2, 3]
        points, _, _ = reconstruct(frame, stride=1)
        # ARKit camera looks down -Z. Top-left pixels are left and up.
        np.testing.assert_allclose(points[0], [0, 2.75, 1])
        np.testing.assert_allclose(points[3 * 8 + 4], [1, 2, 1])

    def test_scaled_intrinsics_and_binary_roundtrip(self):
        frame = simulated_frame(1.0, 4)
        decoded = decode_frame(bundle(frame), ('simulated-room', 1))
        np.testing.assert_allclose(decoded.depth, frame.depth)
        np.testing.assert_allclose(decoded.transform, frame.transform)
        self.assertEqual(decoded.calibration, frame.calibration)

    def test_low_confidence_and_invalid_depth_are_excluded(self):
        frame = flat_frame()
        frame.confidence[:] = 0
        points, _, mesh = reconstruct(frame)
        self.assertEqual(len(points), 0)
        self.assertEqual(len(mesh[2]), 0)
        frame.confidence[:] = 2
        frame.depth[:] = np.nan
        self.assertEqual(len(reconstruct(frame)[0]), 0)

    def test_surface_does_not_bridge_depth_edges(self):
        frame = flat_frame()
        frame.depth[:, 4:] = 5
        vertices, _, indices = reconstruct(frame, stride=1)[2]
        for triangle in indices:
            self.assertLess(np.ptp(vertices[triangle, 2]), 0.01)

    def test_transform_scale_and_reflection_are_rejected(self):
        for scale in (2, -1):
            transform = np.eye(4)
            transform[0, 0] = scale
            with self.assertRaises(ValueError):
                rigid_transform(transform.flatten(order='F'))

    def test_protocol_rejects_truncation_identity_and_malformed_intrinsics(self):
        frame = flat_frame()
        packet = bundle(frame)
        for bad in (packet[:-1], packet + b'\0', b'bad'):
            with self.assertRaises(ValueError):
                decode_frame(bad, ('test', 1))
        with self.assertRaises(ValueError):
            decode_frame(packet, ('test', 2))
        with self.assertRaises(ValueError):
            decode_frame(bundle(frame, transform=[0] * 16), ('test', 1))

    def test_voxel_average_and_capacity(self):
        voxels = VoxelMap(size=1, capacity=2)
        voxels.integrate(np.array([[.1, .1, .1], [.3, .1, .1], [1.1, 0, 0], [2.1, 0, 0]]),
                         np.full((4, 3), 200, np.uint8))
        self.assertEqual(len(voxels.cells), 2)
        points, colors = voxels.snapshot()
        self.assertEqual(points.shape, (2, 3))
        self.assertTrue((colors == 200).all())

    def test_surface_keyframe_retention_and_budget(self):
        frame = flat_frame()
        mesh = reconstruct(frame, stride=1)[2]
        surfaces = SurfaceMap(triangle_budget=len(mesh[2]))
        surfaces.integrate(mesh, np.eye(4))
        surfaces.integrate(mesh, np.eye(4))
        self.assertEqual(len(surfaces.keyframes), 1)
        pose = np.eye(4); pose[0, 3] = 0.3
        surfaces.integrate(mesh, pose)
        self.assertEqual(len(surfaces.keyframes), 1)
        encoded = surfaces.encode()
        vertex_count, index_count = struct.unpack_from('<II', encoded)
        self.assertEqual(len(encoded), 8 + 16 * vertex_count + 4 * index_count)


class StateTests(unittest.TestCase):
    def setUp(self):
        self.state = MapState()
        self.state.start_session(('test', 1), 'Test iPhone')

    def test_loss_pause_and_duplicate_frames_do_not_fuse(self):
        frame = flat_frame()
        self.state.process(frame)
        self.assertEqual(self.state.frames, 1)
        self.state.process(frame)
        self.assertEqual(self.state.frames, 1)
        self.assertEqual(self.state.rejected, 1)
        frame.header['t_capture'] = 1
        frame.header['tracking'] = 'limited'
        self.state.process(frame)
        self.assertEqual(self.state.frames, 1)
        frame.header['t_capture'] = 2
        frame.header['tracking'] = 'normal'
        self.state.paused = True
        self.state.process(frame)
        self.assertEqual(self.state.frames, 1)

    def test_reconnect_retains_map_new_epoch_clears(self):
        self.state.process(flat_frame())
        count = len(self.state.cloud.cells)
        self.state.start_session(('test', 1), 'Test iPhone')
        self.assertEqual(len(self.state.cloud.cells), count)
        self.state.start_session(('test', 2), 'Test iPhone')
        self.assertEqual(len(self.state.cloud.cells), 0)
        self.assertEqual(self.state.surfaces.count, 0)
        self.assertIsNone(self.state.jpeg)

    def test_a_newer_limited_pose_freezes_older_frame_fusion(self):
        frame = flat_frame()
        pose = dict(frame.header, t_capture=2, tracking='limited')
        self.state.observe_pose(pose, np.eye(4))
        self.state.process(frame)
        self.assertEqual(self.state.frames, 0)

    def test_pose_and_rgbd_for_same_capture_must_agree(self):
        frame = flat_frame()
        self.state.observe_pose(frame.header, frame.transform)
        other = frame.transform.copy()
        other[0, 3] = 1
        with self.assertRaises(ValueError):
            self.state.observe_pose(frame.header, other)


class APITests(unittest.TestCase):
    def test_phone_to_map_export_pause_reset_and_reconnect_snapshot(self):
        app = create_app()
        with TestClient(app) as client:
            with client.websocket_connect('/phone') as phone:
                phone.send_json(dict(version=1, type='hello', session_id='test', map_epoch=1,
                                     device='Test', supports_scene_depth=True))
                phone.send_bytes(bundle(flat_frame()))
                deadline = time.monotonic() + 3
                while app.state.mapping.frames == 0 and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertEqual(app.state.mapping.frames, 1)
                with client.websocket_connect('/ws/map') as viewer:
                    status = viewer.receive_json()
                    packet = viewer.receive_bytes()
                    count = struct.unpack_from('<I', packet)[0]
                    self.assertEqual(count, status['points'])
                    self.assertEqual(len(packet), 4 + count * POINT_DTYPE.itemsize)
                self.assertEqual(client.get('/camera.jpg').status_code, 200)
                self.assertEqual(client.get('/depth.jpg').status_code, 200)
                self.assertIn(b'format binary_little_endian', client.get('/map/export.ply').content)
                self.assertTrue(client.post('/map/pause', json={'paused': True}).json()['paused'])
            self.assertFalse(client.get('/health').json()['connected'])
            # Disconnect keeps the map and the viewer's map generation.
            self.assertGreater(client.get('/health').json()['points'], 0)
            before = client.get('/health').json()['generation']
            after = client.post('/map/reset').json()
            self.assertEqual(after['generation'], before + 1)
            self.assertEqual(after['points'], 0)
            self.assertEqual(struct.unpack('<II', client.get('/map/mesh.bin').content), (0, 0))

    def test_second_phone_is_rejected(self):
        app = create_app()
        with TestClient(app) as client:
            with client.websocket_connect('/phone') as first:
                first.send_json(dict(version=1, type='hello', session_id='test', map_epoch=1,
                                     supports_scene_depth=True))
                with client.websocket_connect('/phone') as second:
                    with self.assertRaises(WebSocketDisconnect) as failure:
                        second.receive_json()
                    self.assertEqual(failure.exception.code, 1008)


if __name__ == '__main__':
    unittest.main()
