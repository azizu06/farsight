import os
import unittest
import threading
import time
from unittest.mock import patch

from jetson.mapping.dense_worker import DenseWorker
from jetson.mapping.tests.test_mapping import flat_frame


class DenseBudgetTests(unittest.TestCase):
    def worker(self, env):
        with patch.dict(os.environ, env, clear=True):
            worker = DenseWorker(lambda *_: True)
        self.addCleanup(worker.close)
        return worker

    def test_fast_turn_defaults_and_bounded_work_do_not_start_a_thread(self):
        worker = self.worker({})
        self.assertEqual((worker.hz, worker.frame_points), (8, 12_288))
        self.assertIsNone(worker.thread)
        self.assertEqual(worker.interval(.01), .125)
        self.assertEqual(worker.status()['sampled_points'], 0)

    def test_processing_cost_reduces_admission_instead_of_building_a_backlog(self):
        worker = self.worker({})
        self.assertAlmostEqual(worker.interval(.1), 2 / 3)
        self.assertAlmostEqual(worker.interval(.25), 5 / 3)
        self.assertGreaterEqual(worker.interval(.25) - .25, 1.4)

    def test_operator_can_select_the_previous_cellular_budget(self):
        worker = self.worker({'FARSIGHT_DENSE_HZ': '2', 'FARSIGHT_DENSE_FRAME_POINTS': '4096',
                              'FARSIGHT_DENSE_CPU_BUDGET': '.2'})
        self.assertEqual((worker.hz, worker.frame_points), (2, 4096))
        self.assertEqual(worker.interval(.02), .5)

    def test_invalid_or_unbounded_values_cannot_remove_the_budget(self):
        for value in ('0', '-1', 'NaN', 'infinity', '1000000', 'bad'):
            worker = self.worker({'FARSIGHT_DENSE_HZ': value, 'FARSIGHT_DENSE_FRAME_POINTS': value,
                                  'FARSIGHT_DENSE_CPU_BUDGET': value})
            self.assertEqual((worker.hz, worker.frame_points, worker.cpu_budget), (8, 12_288, .15))

    def test_a_blocked_visual_lock_does_not_add_a_long_recovery_delay(self):
        worker = self.worker({})
        entered, release, next_frame = threading.Event(), threading.Event(), threading.Event()
        class Cloud:
            calls = 0
            def integrate(self, *_):
                self.calls += 1
                if self.calls == 1:
                    entered.set()
                    release.wait(2)
                else:
                    next_frame.set()
        cloud = Cloud()
        try:
            worker.offer(flat_frame(), cloud, 1)
            self.assertTrue(entered.wait(1))
            worker.offer(flat_frame(), cloud, 1)
            # Sleeping/lock waiting consumes no dense-worker CPU budget.
            time.sleep(.25)
            release.set()
            self.assertTrue(next_frame.wait(.4))
        finally:
            release.set()
