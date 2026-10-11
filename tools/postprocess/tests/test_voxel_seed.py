"""Measured seed reduction must retain occupied cells and mean xyz/RGB."""
from pathlib import Path
import tempfile
import unittest
import numpy as np
from tools.postprocess.voxel_seed import downsample


class VoxelSeedTests(unittest.TestCase):
    def test_merge_preserves_negative_cells_and_averages_real_samples(self):
        dtype = np.dtype([(k, '<f4') for k in ('x', 'y', 'z')] + [(k, 'u1') for k in ('red', 'green', 'blue')])
        points = np.array([(-.1, 0, 0, 10, 20, 30), (.1, 0, 0, 40, 60, 80),
                           (.3, 0, 0, 60, 80, 100), (1.1, 0, 0, 90, 100, 110)], dtype=dtype)
        with tempfile.TemporaryDirectory() as tmp:
            source, target = Path(tmp) / 'input.ply', Path(tmp) / 'output.ply'
            header = b'ply\nformat binary_little_endian 1.0\nelement vertex 4\n'
            header += b''.join(f'property float {k}\n'.encode() for k in ('x', 'y', 'z'))
            header += b''.join(f'property uchar {k}\n'.encode() for k in ('red', 'green', 'blue')) + b'end_header\n'
            source.write_bytes(header + points.tobytes())
            receipt = downsample(source, target, 1)
            self.assertEqual(receipt['output_points'], 3)
            result = np.frombuffer(target.read_bytes().split(b'end_header\n', 1)[1], dtype=dtype)
            np.testing.assert_allclose(result['x'], [-.1, .2, 1.1])
            self.assertEqual(result['red'].tolist(), [10, 50, 90])
            self.assertEqual(result['blue'].tolist(), [30, 90, 110])
