"""Pure geometry checks for the loop test world (no ROS needed)."""

import math

import numpy as np

from farsight_slam import world as w


def test_route_is_closed_loop_in_free_space():
    world, route = w.loop_world(), w.loop_route()
    assert 15 < w.route_length(route) < 25
    for x, y in route:
        assert world.free(x, y, margin=0.3), (x, y)


def test_raycast_hits_wall_and_floor():
    world = w.World(8.0, 6.0, [])
    # Level ray east from the centre hits the x = 4 wall.
    r = world.raycast(0.0, 0.0, 0.2, [0.0], [0.0])
    assert math.isclose(r[0], 4.0, abs_tol=1e-6)
    # Downward ray hits the floor at 0.2 / tan(10 deg) horizontally.
    el = -math.radians(10)
    r = world.raycast(0.0, 0.0, 0.2, [0.0], [el])
    assert math.isclose(r[0], (0.2 / math.tan(-el)) / math.cos(el), rel_tol=1e-6)


def test_raycast_passes_over_low_box_and_drops_far_returns():
    low = w.Box(1.0, -0.5, 1.5, 0.5, height=0.12)
    world = w.World(20.0, 6.0, [low])
    # Level ray at 0.2 m clears a 0.12 m crate and the far wall is past max range.
    r = world.raycast(0.0, 0.0, 0.2, [0.0], [0.0])
    assert np.isinf(r[0])
    # Slightly downward ray lands on the crate face.
    r = world.raycast(0.0, 0.0, 0.2, [0.0], [-math.atan(0.1 / 1.0)])
    assert 1.0 < r[0] < 1.1


def test_drifting_odometry_matches_truth_without_drift():
    odom = w.DriftingOdometry()
    for _ in range(100):
        odom.step(0.01, 0.0)
    assert math.isclose(odom.x, 1.0) and odom.y == 0.0


def test_pure_pursuit_completes_a_lap():
    route = w.loop_route()
    pilot = w.PurePursuit(route, 0.35)
    x, y = route[-1]
    yaw = math.atan2(route[0][1] - y, route[0][0] - x)
    dt, travelled = 0.02, 0.0
    world = w.loop_world()
    while travelled < w.route_length(route):
        v, omega = pilot.command(x, y, yaw)
        yaw += omega * dt
        x += v * dt * math.cos(yaw)
        y += v * dt * math.sin(yaw)
        travelled += v * dt
        assert world.free(x, y, margin=0.15)
    assert math.dist((x, y), route[-1]) < 0.8
