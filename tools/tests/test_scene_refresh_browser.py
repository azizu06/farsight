"""A removed red foreground becomes a green wall in the actual native adapter."""
import json
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless(shutil.which('node') and (ROOT / 'quest/node_modules/playwright').is_dir(), 'Node/Chromium required')
class SceneRefreshBrowserTests(unittest.TestCase):
    def test_region_replacement_pixels_and_occluded_outside_surfaces(self):
        subprocess.run(['node', str(ROOT / 'quest/node_modules/vite/bin/vite.js'), 'build', '--config',
                        'tools/dense_viewer/vite.layer.config.mjs'], cwd=ROOT, check=True,
                       capture_output=True, text=True, timeout=30)
        with tempfile.TemporaryDirectory(prefix='farsight-scene-browser-') as directory:
            for i, (depth, x, bgr) in enumerate([(1, 0, (0, 0, 220)), (2, 0, (0, 220, 0)),
                                                (3, 5, (220, 0, 0)), (1, 0, (0, 0, 220)), (2, 0, (0, 220, 0))]):
                _, jpeg = cv2.imencode('.jpg', np.full((8, 8, 3), bgr, np.uint8))
                pose = np.eye(4);pose[0, 3] = x
                header = dict(session_id='test', map_epoch=1, map_generation=1, tracking='normal', frame_id=i,
                              capture_source='SIMULATED SCENE REFRESH', transform=pose.flatten(order='F').tolist(),
                              image=dict(width=8, height=8, jpeg_len=len(jpeg), intrinsics=[4, 0, 0, 0, 4, 0, 4, 4, 1]),
                              depth=dict(width=8, height=8))
                text = json.dumps(header).encode()
                body = (struct.pack('<I', len(text)) + text + jpeg.tobytes()
                        + np.full((8, 8), depth, '<f4').tobytes() + bytes([2]) * 64)
                (Path(directory) / f'frame{i}.bin').write_bytes(body)
            result = subprocess.run(['node', str(ROOT / 'tools/tests/revisit_scene.browser.mjs'), directory],
                                    cwd=ROOT, capture_output=True, text=True, timeout=45)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            report = json.loads(result.stdout)
            self.assertTrue(report['passed'])
            self.assertTrue(report['other_scene_preserved'])
            self.assertTrue(report['occluded_background_preserved'])
