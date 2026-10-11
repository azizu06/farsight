import math

import numpy as np

from farsight_nav import speed


def test_open_space_is_full_speed_and_tight_spots_slow_to_the_floor():
    top = 0.304  # the loaded rover's profile top
    assert speed.limit((0.95, 0.4), None, top) == (top, 0.4 * top / 0.95)  # nothing within reach
    assert speed.limit((0.95, 0.0), 1.2, top) == (top, 0.0)
    assert speed.limit((0.95, 0.0), 0.3, top) == (0.15, 0.0)
    assert speed.limit((0.95, 0.0), 0.1, top) == (0.15, 0.0)  # never under the AUTO floor
    v, w = speed.limit((0.95, 1.0), 0.55, top)
    assert math.isclose(v, 0.15 + (top - 0.15) * 0.5) and math.isclose(w / v, 1.0 / 0.95)  # same arc
    assert speed.limit((0.1, 0.3), 0.3, top) == (0.1, 0.3)  # a slower command passes unchanged
    assert speed.limit((0.0, 2.2), 0.3, top) == (0.0, 2.2)  # an in-place turn is not a translation
    assert speed.limit((-0.3, 0.0), 0.3, top) == (-0.15, 0.0)
    assert speed.limit((0.95, 0.0), 0.3, None) == (0.95, 0.0)  # no server hello yet


def test_top_speed_prefers_the_drive_profile():
    assert speed.top_speed(dict(speed_mps=0.45, max_v=0.7, drive_profile=dict(max_linear_mps=0.304))) == 0.304
    assert math.isclose(speed.top_speed(dict(speed_mps=0.45, max_v=0.7, drive_profile=None)), 0.315)
    assert speed.top_speed({}) is None and speed.top_speed(None) is None


def test_clearance_counts_measured_cells_only_never_the_map_edge():
    lethal = np.zeros((40, 40), bool)
    assert speed.clearance(lethal, 0.05, (0.0, 0.0), (0.02, 0.02), 1.0) is None  # at the map corner
    lethal[20, 30] = True  # one cell centred at x 1.525, y 1.025
    d = speed.clearance(lethal, 0.05, (0.0, 0.0), (1.025, 1.025), 1.0)
    assert math.isclose(d, 0.5 - 0.05 / math.sqrt(2))
    assert speed.clearance(lethal, 0.05, (0.0, 0.0), (0.2, 1.025), 1.0) is None  # 1.3 m away: out of reach
    assert speed.clearance(lethal, 0.05, (-1.0, -1.0), (0.525, 0.025), 1.0) < 0.01  # map origin offset
