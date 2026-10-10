"""Native Mac loopback joined-pipeline test; no phone/VPN/hardware qualification."""
from pathlib import Path
import json
import platform
import shutil
import struct
import subprocess
import tempfile
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless(platform.system() == 'Darwin' and shutil.which('swiftc'), 'Apple Network/Swift required')
class PhoneGeometryChainTests(unittest.TestCase):
    def test_native_feed_listener_operator_bridge_and_recovery(self):
        import brotli
        with tempfile.TemporaryDirectory(prefix='farsight-geometry-chain-') as work:
            path=Path(work); binary=path/'chain'
            production=['GeometryWire.swift','PhoneGeometryFeed.swift','PhoneGeometryServer.swift','PhoneBridge.swift']
            subprocess.run(['swiftc',*[str(ROOT/'ios/FarsightCapture'/x) for x in production],
                            str(ROOT/'ios/tests/PhoneBridgeTestSupport.swift'),
                            str(ROOT/'ios/tests/PhoneGeometryChainFixture.swift'),'-o',str(binary)],
                           check=True,capture_output=True,text=True,timeout=60)
            result=subprocess.run([str(binary),work],capture_output=True,text=True,timeout=35)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            self.assertIn('PASS joined native geometry chain',result.stdout)
            previous=0
            for name in ('first','second','before-background','recovered'):
                packets=[(path/f'{name}-{i}.bin').read_bytes() for i in range(3)]
                headers=[struct.unpack('<BBHQQ16s16s16fII',packet[:124]) for packet in packets]
                self.assertEqual([h[1] for h in headers],[2,0,3])
                self.assertGreater(headers[0][3],previous); previous=headers[-1][3]
                for h in headers:
                    self.assertEqual(h[4],7)
                    self.assertEqual(h[5],uuid.UUID('123e4567-e89b-12d3-a456-426614174000').bytes)
                self.assertEqual(len(packets[0]),124); self.assertEqual(len(packets[2]),124)
                replacement=headers[1]
                self.assertEqual(replacement[2],2)
                self.assertEqual(replacement[6],uuid.UUID('223e4567-e89b-12d3-a456-426614174000').bytes)
                self.assertEqual(replacement[19:22],(1,2,3))
                self.assertEqual(replacement[-2:],(3,3))
                self.assertEqual(struct.unpack('<9f3I',brotli.decompress(packets[1][124:])),
                                 (0,0,0,1,0,0,0,1,0,0,1,2))
            status=json.loads((path/'status.json').read_text())
            self.assertTrue(status['ready']); self.assertEqual(status['map_epoch'],7)
            self.assertEqual(status['source'],'IPHONE ARKIT MESH')
            self.assertEqual((status['anchors'],status['vertices'],status['triangles']),(1,3,1))
