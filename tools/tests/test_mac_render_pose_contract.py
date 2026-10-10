"""Execute the server capture expression and renderer pose code, without a GPU."""

import json
from pathlib import Path
import shutil
import subprocess
import unittest
from unittest.mock import patch

from tools.mac_render.server import RenderEncoder, _UNSET


ROOT = Path(__file__).resolve().parents[2]

# Real Three.js camera/ray objects and the production setter/applyPose functions.
# Only the pixel capture is replaced: it calls applyPose in the same JS turn.
POSE_VM = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const THREE = require('./quest/node_modules/three');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const source = fs.readFileSync('tools/mac_render/render.mjs', 'utf8');
const start = source.indexOf('function clearPose()');
const end = source.indexOf('\nwindow.__mac = { ready: true', start);
if (start < 0 || end < 0) throw new Error('renderer pose functions not found');
const camera = new THREE.PerspectiveCamera(50, 4 / 3, .01, 2000);
camera.position.set(9, 6, 9);
const rayPositions = new Float32Array(6);
const geometry = new THREE.BufferGeometry();
geometry.setAttribute('position', new THREE.BufferAttribute(rayPositions, 3));
const rayLine = new THREE.Line(geometry, new THREE.LineBasicMaterial());
let clock = 100;
const context = vm.createContext({
  window: { __mac: { poseEcho: null, poseValid: false } },
  performance: { now: () => clock }, camera, rayPositions, rayLine,
  tmp: new THREE.Quaternion(),
});
vm.runInContext('let incomingPose = null; let pose = null; let frameDirty = false;', context);
vm.runInContext(source.slice(start, end), context);
vm.runInContext(`window.__macCapture = () => {
  applyPose();
  return { ...window.__mac, rayVisible: rayLine.visible,
    viewPosition: camera.position.toArray(), viewQuaternion: camera.quaternion.toArray(),
    fovY: camera.fov, aspect: camera.aspect };
};`, context);
const results = input.map(call => {
  clock += call.elapsed;
  const capture = vm.runInContext(`(${call.script})`, context);
  return capture(call.arg);
});
process.stdout.write(JSON.stringify(results));
"""


@unittest.skipUnless(shutil.which('node'), 'Node.js required for the actual JS contract')
class MacRenderPoseContractTests(unittest.TestCase):
    def test_validated_pose_moves_capture_camera_and_expires_or_clears(self):
        encoder = RenderEncoder(None, 320, 240, 30, 100000, False)
        message = dict(type='pose', version=1, session_id='quest-fixture', frame_id=7,
                       sent_mono_ms=999, sent_wall_ms=1710000000000,
                       head=dict(position=[1, 2, 3], quaternion=[0, 0, 1, 1]),
                       ray=dict(origin=[1, 2, 3], direction=[0, 0, -2]),
                       fov_y_deg=70, aspect=1.5, space='xr_local_floor', map_revision=9)
        calls = []

        def capture(elapsed=0):
            with patch('tools.mac_render.server.time.monotonic', return_value=1):
                script, arg = encoder._capture_call()
            calls.append(dict(script=script, arg=None if arg is _UNSET else arg, elapsed=elapsed))

        pose = encoder.apply_pose(message, now_mono_ms=1000)
        encoder.echo_pose(pose)
        capture()
        # No new pose: the page's existing object must age on its own clock.
        capture()
        capture(elapsed=251)
        encoder.echo_pose(encoder.apply_pose(dict(message, frame_id=8), 1000))
        capture()
        untracked = encoder.apply_pose(dict(type='pose_untracked', version=1,
                                            session_id='quest-fixture', frame_id=9), 1000)
        encoder.echo_pose(untracked if untracked['tracked'] else None)
        capture()

        result = subprocess.run(['node', '-e', POSE_VM], cwd=ROOT,
                                input=json.dumps(calls), text=True, capture_output=True,
                                check=True, timeout=10)
        fresh, repeated, expired, recovered, cleared = json.loads(result.stdout)
        self.assertTrue(fresh['poseValid'], 'validated server pose was rejected by actual applyPose')
        self.assertTrue(fresh['rayVisible'])
        self.assertEqual(fresh['viewPosition'], [1, 2, 3])
        self.assertAlmostEqual(fresh['viewQuaternion'][2], 2 ** -.5)
        self.assertAlmostEqual(fresh['viewQuaternion'][3], 2 ** -.5)
        self.assertEqual((fresh['fovY'], fresh['aspect']), (70, 1.5))
        self.assertEqual(fresh['poseEcho']['received_mono_ms'], 1000)
        self.assertEqual(fresh['poseEcho']['frame_id'], 7)
        self.assertTrue(repeated['poseValid'])
        self.assertTrue(recovered['poseValid'])
        self.assertEqual(recovered['poseEcho']['frame_id'], 8)
        for stopped in (expired, cleared):
            self.assertFalse(stopped['poseValid'])
            self.assertFalse(stopped['rayVisible'])
            self.assertIsNone(stopped['poseEcho'])
            self.assertEqual(stopped['viewPosition'], [1, 2, 3])


if __name__ == '__main__':
    unittest.main()
