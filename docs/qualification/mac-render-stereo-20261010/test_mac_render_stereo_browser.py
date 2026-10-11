"""Actual-renderer eye-pair pixels; simulated fixtures, no hardware.

Private preparation stays skipped until FARSIGHT_STEREO_GPU_GO=1.
FARSIGHT_ROOT selects the immutable owner implementation checkout.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT = Path(os.environ.get('FARSIGHT_ROOT', Path(__file__).resolve().parents[3]))
SCRIPT = Path(__file__).with_name('mac_render_stereo.browser.mjs')


@unittest.skipUnless(os.environ.get('FARSIGHT_STEREO_GPU_GO') == '1', 'Owner implementation SHA and explicit GPU GO required')
@unittest.skipUnless(shutil.which('node') and (ROOT/'quest/node_modules/playwright').is_dir(), 'Node/Chromium required')
class MacRenderStereoBrowserTests(unittest.TestCase):
    def test_real_eye_views_projection_parallax_pair_identity_rejection_and_state_restore(self):
        env = {**os.environ, 'FARSIGHT_ROOT': str(ROOT)}
        result = subprocess.run(['node', str(SCRIPT), '--run'], cwd=ROOT, env=env,
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads(result.stdout)
        self.assertTrue(report['passed']); self.assertFalse(report['hardwareQualified'])
        self.assertFalse(report['realSensorGeometry'])
        self.assertGreater(report['nearDisparityPixels'], report['farDisparityPixels'] + 10)
        self.assertIn('disconnect', report['rejectedPairs'])


if __name__ == '__main__':
    unittest.main()
