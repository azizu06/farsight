"""People semantics: false leg gaps, filled groups, real aisles and moving people."""

from pathlib import Path
import sys
import unittest

import numpy as np
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from farsight_nav import crowds, open_space  # noqa: E402


class CrowdTests(unittest.TestCase):
    resolution, origin, shape = .05, (-3., -3.), (120, 120)

    def mask(self, points):
        return crowds.rasterize(points, self.shape, self.resolution, self.origin)

    def at(self, cells, x, y):
        return cells[int((y - self.origin[1]) / self.resolution), int((x - self.origin[0]) / self.resolution)]

    def packet(self, positions, **fields):
        # Input world Y is height, not the ROS floor's second coordinate.
        return dict(sensor_key=["phone", 2, 7, 42, 100.], fresh=True, age_ms=100,
                    boxes=[dict(cls="person", world=[-y, 1.1, -x]) for x, y in positions], **fields)

    def test_body_projection_closes_open_floor_between_disconnected_feet(self):
        floor = np.zeros(self.shape, np.int16)
        for x in (-.25, .25):
            floor[60, int((x + 3) / .05)] = 100  # depth sees two separate feet
        self.assertEqual(self.at(floor, 0, 0), 0)
        result = crowds.overlay(floor, [(0, 0)], .05, self.origin)
        self.assertEqual(self.at(result, 0, 0), 100)
        self.assertEqual(self.at(result, 0, .5), 0)
        self.assertEqual(self.at(floor, 0, 0), 0)  # measured input stays untouched

    def test_bootstrap_geometry_uses_custom_global_map_parameters(self):
        config = {"global_costmap": {"global_costmap": {"ros__parameters":
                  dict(width=16, height=8, resolution=.125, origin_x=-4., origin_y=1.)}}}
        self.assertEqual(crowds.bootstrap_parameters(config), dict(people_map_width=16., people_map_height=8.,
                         people_map_resolution=.125, people_map_origin_x=-4., people_map_origin_y=1.))

    def test_custom_file_omitted_geometry_matches_nav2_defaults(self):
        config = {"global_costmap": {"global_costmap": {"ros__parameters": {}}}}
        self.assertEqual(crowds.bootstrap_parameters(config), dict(people_map_width=5., people_map_height=5.,
                         people_map_resolution=.1, people_map_origin_x=0., people_map_origin_y=0.))

    def test_real_costmaps_compose_people_after_measured_layer_before_inflation(self):
        directory = Path(__file__).resolve().parents[1] / "config"
        params = yaml.safe_load((directory / "nav2_params.yaml").read_text())
        nvblox = yaml.safe_load((directory / "nvblox_overlay.yaml").read_text())
        for name, unknown in (("global_costmap", True), ("local_costmap", False)):
            grid = params[name][name]["ros__parameters"]
            self.assertTrue(grid["use_maximum"])
            self.assertEqual(grid["track_unknown_space"], unknown)
            self.assertEqual(grid["plugins"][-2:], ["people_layer", "inflation_layer"])
            self.assertEqual(nvblox[name][name]["ros__parameters"]["plugins"][-2:],
                             ["people_layer", "inflation_layer"])
            layer = grid["people_layer"]
            self.assertEqual(layer["plugin"], "nav2_costmap_2d::StaticLayer")
            self.assertEqual(layer["map_topic"], "/farsight/people/map")
            self.assertTrue(layer["map_subscribe_transient_local"])
            self.assertFalse(layer["footprint_clearing_enabled"])

    def test_small_crowd_has_filled_interior_instead_of_individual_leg_routes(self):
        points = [(-.4, -.4), (.4, -.4), (.4, .4), (-.4, .4)]
        mask = self.mask(points)
        self.assertTrue(self.at(mask, 0, 0))
        self.assertTrue(self.at(mask, .6, 0))  # body radius around the outer hull
        self.assertFalse(self.at(mask, .9, 0))

    def test_separate_crowds_leave_a_true_wide_aisle_open(self):
        points = [(-1.5, -.4), (-1.5, .4), (1.5, -.4), (1.5, .4)]
        mask = self.mask(points)
        self.assertFalse(mask[:, 55:65].any())
        self.assertTrue(self.at(mask, -1.5, 0))
        self.assertTrue(self.at(mask, 1.5, 0))

    def test_unknown_space_and_existing_obstacles_are_preserved_outside_people(self):
        floor = np.full(self.shape, -1, np.int16)
        floor[20, 20], floor[30, 30] = 100, 50
        result = crowds.overlay(floor, [(0, 0)], .05, self.origin)
        self.assertEqual(result[20, 20], 100)
        self.assertEqual(result[30, 30], 50)
        self.assertEqual(result[10, 10], -1)
        self.assertEqual(self.at(result, 0, 0), 100)

    def test_duplicates_collinear_people_and_off_map_people_rasterize(self):
        mask = self.mask([(0, 0), (0, 0), (0, 1), (100, 100)])
        self.assertTrue(self.at(mask, 0, .5))
        self.assertFalse(self.at(mask, 1, .5))

    def test_moving_and_empty_snapshots_clear_vacated_positions(self):
        people = crowds.People()
        people.bind(["phone", 2, 7, 41, 99.])
        people.update(self.packet([(0, 0)]), now=10)
        self.assertTrue(self.at(self.mask(people.points(10.1)), 0, 0))
        people.update(self.packet([(1, 0)]), now=10.2)
        mask = self.mask(people.points(10.3))
        self.assertFalse(self.at(mask, 0, 0))
        self.assertTrue(self.at(mask, 1, 0))
        people.update(self.packet([]), now=10.4)
        self.assertFalse(self.mask(people.points(10.5)).any())

    def test_existing_detection_freshness_expires_semantic_occupancy(self):
        people = crowds.People()
        people.bind(["phone", 2, 7, 41, 99.])
        people.update(self.packet([(0, 0)]), now=10)
        self.assertEqual(people.points(10.89), ((0., 0.),))
        self.assertEqual(people.points(10.9), ())

    def test_old_world_cannot_repopulate_after_sensor_reset(self):
        people = crowds.People()
        people.bind(["phone", 2, 7, 41, 99.])
        people.update(self.packet([(0, 0)]), now=10)
        people.bind(["phone", 3, 8, 1, 101.])
        self.assertFalse(people.update(self.packet([(0, 0)]), now=10.1))
        self.assertEqual(people.points(10.2), ())
        people.bind(None)  # an older server still supports measured navigation
        self.assertFalse(people.update(self.packet([(0, 0)]), now=10.3))

    def test_unlocated_boxes_do_not_become_guessed_world_obstacles(self):
        people = crowds.People()
        people.bind(["phone", 2, 7, 41, 99.])
        packet = self.packet([])
        packet["boxes"] = [dict(cls="person", xywh=[.1, .2, .3, .4]),
                           dict(cls="chair", world=[0, 1, 0]), dict(cls="person", world=[float("nan"), 1, 0])]
        people.update(packet, now=10)
        self.assertEqual(people.points(10.1), ())

    def test_open_space_route_goes_around_the_crowd_to_retained_destination(self):
        floor = np.zeros(self.shape, np.int16)
        points = [(-.4, -.4), (.4, -.4), (.4, .4), (-.4, .4)]
        cells = crowds.overlay(floor, points, .05, self.origin)
        choice = open_space.choose(cells, .05, self.origin, (-2., 0., 0.), current_goal=(2., 0.))
        self.assertIsNotNone(choice)
        self.assertEqual(choice.goal, (2., 0.))
        self.assertTrue(all(not self.at(self.mask(points), *point) for point in choice.route))
        self.assertGreater(max(abs(y) for x, y in choice.route), .7)

    def test_new_crowd_at_current_destination_selects_reachable_open_space(self):
        points = [(-.4, -.4), (.4, -.4), (.4, .4), (-.4, .4)]
        mask = self.mask(points)
        cells = crowds.overlay(np.zeros(self.shape, np.int16), points, .05, self.origin)
        choice = open_space.choose(cells, .05, self.origin, (-2., 0., 0.), current_goal=(0., 0.))
        self.assertIsNotNone(choice)
        self.assertNotEqual(choice.goal, (0., 0.))
        self.assertTrue(all(not self.at(mask, *point) for point in choice.route))


if __name__ == "__main__":
    unittest.main()
