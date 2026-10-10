import unittest

from jetson.tracker import Tracker


def person(x, y=.2, w=.15, h=.5, cls="person"):
    return dict(cls=cls, x=x, y=y, w=w, h=h)


class TrackerTests(unittest.TestCase):
    def test_moving_box_keeps_id(self):
        tracker = Tracker()
        first = tracker.update([person(.1)], 0)[0]["id"]
        for i in range(1, 10):
            self.assertEqual(tracker.update([person(.1 + .03 * i)], i * .1)[0]["id"], first)

    def test_fast_box_with_no_overlap_matches_by_centroid(self):
        tracker = Tracker()
        first = tracker.update([person(.1)], 0)[0]["id"]
        self.assertEqual(tracker.update([person(.3)], .1)[0]["id"], first)

    def test_crossing_people_keep_distinct_ids(self):
        tracker = Tracker()
        a, b = tracker.update([person(.05), person(.75)], 0)
        ids = {"left": a["id"], "right": b["id"]}
        self.assertNotEqual(ids["left"], ids["right"])
        for i in range(1, 11):
            boxes = tracker.update([person(.05 + .07 * i), person(.75 - .07 * i)], i * .1)
            by_x = {box["id"]: box["x"] for box in boxes}
            self.assertEqual(set(by_x), set(ids.values()))
        self.assertGreater(by_x[ids["left"]], by_x[ids["right"]])

    def test_box_missing_for_two_frames_keeps_id(self):
        tracker = Tracker()
        first = tracker.update([person(.4)], 0)[0]["id"]
        tracker.update([], .1)
        tracker.update([], .2)
        self.assertEqual(tracker.update([person(.41)], .3)[0]["id"], first)

    def test_new_person_gets_new_id(self):
        tracker = Tracker()
        first = tracker.update([person(.1)], 0)[0]["id"]
        boxes = tracker.update([person(.1), person(.7)], .1)
        self.assertEqual(boxes[0]["id"], first)
        self.assertNotIn(boxes[1]["id"], (first, None))

    def test_track_expires_after_max_age(self):
        tracker = Tracker(max_age=3)
        first = tracker.update([person(.4)], 0)[0]["id"]
        for i in range(1, 6):
            tracker.update([], i * .1)
        self.assertNotEqual(tracker.update([person(.4)], .6)[0]["id"], first)

    def test_track_expires_by_wall_time(self):
        tracker = Tracker()
        first = tracker.update([person(.4)], 0)[0]["id"]
        self.assertNotEqual(tracker.update([person(.4)], 10)[0]["id"], first)

    def test_ids_never_reused_and_classes_do_not_mix(self):
        tracker = Tracker()
        first = tracker.update([person(.4)], 0)[0]["id"]
        other = tracker.update([person(.4, cls="chair")], .1)[0]["id"]
        self.assertNotEqual(first, other)


if __name__ == "__main__":
    unittest.main()
