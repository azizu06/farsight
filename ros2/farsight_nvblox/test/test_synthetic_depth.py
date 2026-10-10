import numpy as np

from farsight_nvblox.synthetic_depth import render_depth


def test_straight_ahead_ray_hits_the_box_before_the_wall():
    depth = render_depth(0.0)
    centre = depth[96, 128]
    assert abs(centre - 1.8) < 0.05  # box front face at x = 1.8; optical depth = x distance when yaw = 0


def test_view_to_the_side_reaches_the_far_wall():
    depth = render_depth(0.0, fx=210.0)
    assert depth[96, 5] > 0 and depth[96, 5] < 3.5  # left edge grazes the side wall or the back wall


def test_floor_rows_are_valid_and_sky_rows_are_not_garbage():
    depth = render_depth(0.0)
    assert (depth[150:, :] > 0).all()                      # lower image sees the floor
    assert depth.min() >= 0 and depth.max() <= 5.0 and depth.dtype == np.float32


def test_floor_depth_matches_geometry():
    # Row v below the centre looks down at atan((v-cy)/fy); the floor is hit at height / tan(angle) metres out.
    depth = render_depth(0.0, height=0.15, rows=192, fy=210.0)
    v = 170
    expected = 0.15 / ((v + 0.5 - 96) / 210.0)
    assert abs(depth[v, 128] - expected) < 0.05


def test_yaw_changes_what_the_camera_sees():
    assert not np.allclose(render_depth(0.0), render_depth(0.4))
