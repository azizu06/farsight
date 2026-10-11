import tempfile
import unittest
from pathlib import Path

import numpy as np

from tools.splat.prune import main, read_ply, sh_degree, write_ply

# Brush writes f_rest in lexicographic property order (f_rest_0, f_rest_1, f_rest_10, ...).
REST = sorted((f"f_rest_{i}" for i in range(45)), key=str)
NAMES = ["f_dc_0", "f_dc_1", "f_dc_2", *REST, "opacity", "rot_0", "rot_1", "rot_2", "rot_3",
         "scale_0", "scale_1", "scale_2", "x", "y", "z"]


def splats(count):
    rows = np.zeros((count, len(NAMES)), np.float32)
    column = {name: i for i, name in enumerate(NAMES)}
    rows[:, column["x"]] = np.arange(count)
    rows[:, column["opacity"]] = np.linspace(-4, 4, count)   # later splats are more opaque
    for i in range(45):
        rows[:, column[f"f_rest_{i}"]] = i                   # value = original SH index
    return rows


class PruneTests(unittest.TestCase):
    def roundtrip(self, rows, *args):
        with tempfile.TemporaryDirectory() as tmp:
            src, dst = Path(tmp) / "in.ply", Path(tmp) / "out.ply"
            write_ply(src, ["comment Exported from Brush"], NAMES, rows, 3)
            main([str(src), str(dst), *args])
            return read_ply(dst)

    def test_keeps_the_most_opaque_splats_in_canonical_order(self):
        comments, names, rows = self.roundtrip(splats(100), "--max-splats", "10", "--sh-degree", "3")
        self.assertEqual(len(rows), 10)
        self.assertEqual(names[:6], ["x", "y", "z", "f_dc_0", "f_dc_1", "f_dc_2"])
        self.assertEqual(names[6:51], [f"f_rest_{i}" for i in range(45)])
        self.assertEqual(names[51:], ["opacity", "scale_0", "scale_1", "scale_2", "rot_0", "rot_1", "rot_2", "rot_3"])
        np.testing.assert_array_equal(rows[:, 0], np.arange(90, 100))   # equal scales: opacity decides
        np.testing.assert_array_equal(rows[0, 6:51], np.arange(45))       # SH values follow their names
        self.assertIn("comment SH degree: 3", comments)

    def test_sh_trim_keeps_the_low_order_coefficients_per_channel(self):
        _, names, rows = self.roundtrip(splats(5), "--sh-degree", "1")
        self.assertEqual(sh_degree(names), 1)
        rest = rows[0, 6:15]
        # Degree 1 keeps 3 coefficients from each 15-wide channel block (R 0-2, G 15-17, B 30-32).
        np.testing.assert_array_equal(rest, [0, 1, 2, 15, 16, 17, 30, 31, 32])

    def test_small_input_is_kept_whole(self):
        _, _, rows = self.roundtrip(splats(7), "--max-splats", "300000", "--sh-degree", "-1")
        self.assertEqual(rows.shape, (7, len(NAMES)))


if __name__ == "__main__":
    unittest.main()
