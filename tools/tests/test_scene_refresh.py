"""Native view -> retained Mac region refresh, including an expired source cache."""

import base64
from collections import deque
import unittest
from unittest import mock

import numpy as np

from jetson.mapping.dense import DenseMap, unpack
from jetson.mapping.geometry import Calibration
from jetson.mapping.phone import RGBDFrame
from jetson.mapping.revisit import DepthView
from tools.dense_map import DenseAccumulator

SESSION = '123e4567-e89b-12d3-a456-426614174000'
FOREGROUND = (0, 0, -1000, 255, 0, 0, 1)
BACKGROUND = (0, 0, -2000, 0, 255, 0, 1)
OCCLUDED = (0, 0, -3000, 0, 0, 255, 1)
OUTSIDE = (10000, 0, -1000, 255, 255, 0, 1)


def view(depth=2, frame_id=2):
    frame = RGBDFrame(dict(_mapping_sensor_key=(SESSION, 1, 0, frame_id, float(frame_id))),
                      np.zeros((8, 8, 3), np.uint8), np.full((8, 8), depth, np.float32),
                      np.full((8, 8), 2, np.uint8), Calibration(8, 8, 4, 4, 4, 4), np.eye(4), b'')
    return DepthView.from_frame(frame)


def key(record):
    return tuple(value // 10 for value in record[:3])


class SceneRefreshTests(unittest.TestCase):
    def setUp(self):
        self.now = 0.
        self.source = DenseMap(ttl=1, clock=lambda: self.now, cache_only=True)
        self.source.source = [SESSION, 1]
        self.mac = DenseAccumulator('https://jetson.example:8443')

    def packet(self, **changes):
        cursor = dict(self.mac.cursor, **changes)
        if isinstance(cursor.get('snapshot'), str):
            cursor['snapshot'] = cursor['snapshot'] == 'true'
        body = self.source.delta(**cursor)
        return unpack(body)

    def pull(self, **changes):
        metadata, records = self.packet(**changes)
        self.mac.accept(metadata, records)
        return metadata, records

    def test_revisit_clears_old_foreground_after_jetson_cache_expired_but_keeps_other_scene(self):
        self.source.apply([FOREGROUND, BACKGROUND, OCCLUDED, OUTSIDE])
        self.pull()
        self.now = 2
        self.pull()  # Ordinary cache TTL removes no retained Mac room history.
        self.assertFalse(self.source.cells)
        self.assertEqual(len(self.mac.cloud.cells), 4)
        self.source.publish_view(view())
        metadata, _ = self.pull()
        self.assertNotIn(key(FOREGROUND), self.mac.cloud.cells)
        for record in (BACKGROUND, OCCLUDED, OUTSIDE):
            self.assertIn(key(record), self.mac.cloud.cells)
        self.assertEqual(self.mac.cloud.revisited_cells, 1)
        self.assertEqual(self.mac.cursor['view_after'], metadata['view']['key'])
        following, _ = self.packet()
        self.assertNotIn('view', following)
        _, updates = unpack(self.mac.cloud.delta(self.mac.cloud.epoch, after=4))
        self.assertTrue(any(record[-1] == 0 and key(record) == key(FOREGROUND) for record in updates))

    def test_view_metadata_fits_old_page_header_and_repeated_view_is_omitted(self):
        self.source.publish_view(view())
        metadata, _ = self.pull()
        self.assertLess(len(str(metadata)), 65536)
        metadata, _ = self.pull()
        self.assertNotIn('view', metadata)

    def test_authoritative_delete_passes_through_while_cache_delete_does_not(self):
        self.source.apply([FOREGROUND, OUTSIDE])
        self.pull()
        self.source.reconcile(view())
        metadata, records = self.pull()
        mask = base64.b64decode(metadata['authoritative_deletes'])
        self.assertTrue(mask[0] & 1)
        self.assertEqual(records[0][-1], 0)
        self.assertNotIn(key(FOREGROUND), self.mac.cloud.cells)
        self.assertIn(key(OUTSIDE), self.mac.cloud.cells)

    def test_a_new_positive_observation_is_not_cleared_by_an_older_depth_view(self):
        self.source.apply([BACKGROUND])
        self.source.publish_view(view())
        self.source.apply([FOREGROUND])  # Source observation newer than view.
        metadata, _ = self.pull()
        self.assertIn('newer_than_view', metadata)
        self.assertIn(key(FOREGROUND), self.mac.cloud.cells)
        self.source.publish_view(view(frame_id=3))  # A fresh revisit sees through it.
        self.pull()
        self.assertNotIn(key(FOREGROUND), self.mac.cloud.cells)

    def test_same_epoch_snapshot_cannot_reintroduce_old_cleared_foreground(self):
        self.source.apply([FOREGROUND, BACKGROUND])
        self.source.publish_view(view())
        self.pull()
        self.assertNotIn(key(FOREGROUND), self.mac.cloud.cells)
        self.pull(epoch=None, after=0)  # Bootstrap cached records in the same world again.
        self.assertNotIn(key(FOREGROUND), self.mac.cloud.cells)
        self.assertIn(key(BACKGROUND), self.mac.cloud.cells)

    def test_identical_returning_object_is_sent_immediately_after_revisit(self):
        self.source.apply([FOREGROUND, BACKGROUND])
        self.pull()
        self.now = .1
        self.source.publish_view(view())
        self.pull()
        self.assertNotIn(key(FOREGROUND), self.mac.cloud.cells)
        self.now = .2
        self.source.apply([FOREGROUND])  # Exactly the same coordinates and RGB.
        metadata, rows = self.pull()
        self.assertIn(FOREGROUND, rows)
        self.assertIn('newer_than_view', metadata)
        self.assertIn(key(FOREGROUND), self.mac.cloud.cells)
        self.source.publish_view(view(depth=1, frame_id=3))
        self.pull()
        self.assertIn(key(FOREGROUND), self.mac.cloud.cells)

    def test_expired_view_and_same_epoch_resync_do_not_resurrect_removed_dots(self):
        self.source.ttl = 30
        self.source.apply([FOREGROUND, BACKGROUND, OCCLUDED, OUTSIDE])
        self.pull()
        self.now = .1
        self.source.publish_view(view())
        self.pull()
        self.now = 2.5  # Native evidence expires before the cache does.
        metadata, rows = self.pull(epoch=None, after=0)
        self.assertGreater(metadata['view']['age_ms'], 2000)
        self.assertNotIn(FOREGROUND, rows)
        self.assertNotIn(key(FOREGROUND), self.mac.cloud.cells)
        self.assertEqual(set(self.mac.cloud.cells), {key(r) for r in (BACKGROUND, OCCLUDED, OUTSIDE)})

    def test_source_revisit_index_is_regional_and_tracks_cache_eviction_and_clear(self):
        self.source.capacity = 3
        self.source.apply([FOREGROUND, BACKGROUND, OCCLUDED, OUTSIDE])
        self.assertNotIn(key(FOREGROUND), set().union(*self.source.spatial_chunks.values()))
        self.source.apply([FOREGROUND])
        original = DepthView.contradicted
        with mock.patch.object(DepthView, 'contradicted', autospec=True) as project:
            project.side_effect = original
            self.source.publish_view(view())
            self.assertEqual(len(project.call_args.args[1]), 1)  # Distant and occluded chunks were not projected.
        indexed = set().union(*self.source.spatial_chunks.values())
        self.assertEqual(indexed, set(self.source.cells))
        self.source.clear([SESSION, 1])
        self.assertFalse(self.source.spatial_chunks)

    def test_saved_snapshot_cannot_restore_a_delete_that_rolled_out_of_journal(self):
        self.source.ttl = 30
        self.source.journal = deque(maxlen=2)
        self.source.apply([BACKGROUND, FOREGROUND, OUTSIDE])
        first, rows = self.pull(limit=1)
        self.assertTrue(first['snapshot'])
        anchor = first['anchor']
        self.now = .1
        self.source.publish_view(view())
        for green in (10, 11, 12):
            self.source.apply([(*OUTSIDE[:3], 255, green, 0, 1)])
        self.assertFalse(self.source.authoritative_deletes)  # The tiny journal has wrapped.
        self.now = 3  # Compact view is now too old for retained-history projection.
        while self.mac.cursor['snapshot'] == 'true':
            metadata, rows = self.pull(limit=1)
            self.assertFalse(metadata['reset'])  # Bootstrap continues instead of starving.
            self.assertEqual(metadata['anchor'], anchor)
            self.assertNotIn(FOREGROUND, rows)
        self.pull()  # Journal gap recovers from the producer's current cache.
        self.assertNotIn(key(FOREGROUND), self.mac.cloud.cells)
        self.assertIn(key(BACKGROUND), self.mac.cloud.cells)
        self.assertIn(key(OUTSIDE), self.mac.cloud.cells)
        self.source.clear([SESSION, 1])
        self.assertFalse(self.source.snapshot_keys)

    def test_unknown_pixels_foreign_capture_and_expired_view_preserve_scene(self):
        # Exercise retained history beyond the producer cache, where only the
        # compact view can retire a point (not an authoritative source delete).
        self.source.capacity = 1
        self.source.apply([FOREGROUND])
        self.pull()
        self.source.apply([OUTSIDE])
        self.pull()
        unknown = view()
        unknown.depth_mm.setflags(write=True)
        unknown.depth_mm[4, 4] = 0
        self.source.publish_view(unknown)
        self.pull()
        self.assertIn(key(FOREGROUND), self.mac.cloud.cells)
        foreign = view(frame_id=4).to_metadata()
        foreign['sensor_key'][0] = 'other-world'
        self.source.publish_view(DepthView.from_metadata(foreign))
        self.pull()
        self.assertIn(key(FOREGROUND), self.mac.cloud.cells)
        self.source.publish_view(view(frame_id=5))
        self.now = 3
        self.pull()
        self.assertIn(key(FOREGROUND), self.mac.cloud.cells)

    def test_invalid_new_view_does_not_apply_older_evidence_to_new_foreground(self):
        self.source.apply([BACKGROUND])
        self.source.publish_view(view())
        self.pull()
        self.source.apply([FOREGROUND])
        self.source.publish_view(view(depth=1, frame_id=3))
        metadata, records = self.packet()
        metadata['view']['version'] = 999
        self.mac.accept(metadata, records)
        self.assertIn(key(FOREGROUND), self.mac.cloud.cells)
        self.assertIsNotNone(self.mac.revisit_error)
        self.pull()
        self.assertIn(key(FOREGROUND), self.mac.cloud.cells)

    def test_view_transfer_age_and_epoch_reset_with_same_capture_key(self):
        self.source.capacity = 1
        self.source.apply([FOREGROUND])
        self.pull()
        self.source.apply([OUTSIDE])
        self.pull()
        current = view()
        self.source.publish_view(current)
        metadata, records = self.packet()
        self.mac.accept(metadata, records, transfer_seconds=2.5)
        self.assertIn(key(FOREGROUND), self.mac.cloud.cells)
        self.source.clear([SESSION, 1])
        self.source.apply([FOREGROUND])
        self.source.publish_view(current)
        self.mac.cursor['view_after'] = metadata['view']['key']
        reset, _ = self.pull()
        self.assertIn('view', reset)
        self.assertNotIn(key(FOREGROUND), self.mac.cloud.cells)


if __name__ == '__main__':
    unittest.main()
