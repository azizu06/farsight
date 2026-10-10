"""Profiling must retain bounded evidence and refuse physical motor configuration."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import sys
import time
import unittest
from unittest.mock import patch

from tools.profile_backend import ThreadCpuProfile, Timings
from tools.tests.test_demo_qualification import fixture_app


class ProfileTests(unittest.TestCase):
    def test_tail_samples_stay_bounded_and_lifetime_count_survives(self):
        timings = Timings(capacity=3)
        for sample in range(20):
            timings.add("decode_ms", sample)
        self.assertEqual(timings.summary()["decode_ms"],
                         dict(count=20, retained=3, p50=18, p95=19, maximum=19))

    def test_concurrent_executor_samples_are_not_lost(self):
        async def collect():
            timings = Timings(capacity=8)
            await asyncio.gather(*(asyncio.to_thread(timings.add, "worker_ms", i) for i in range(100)))
            return timings.summary()["worker_ms"]
        summary = asyncio.run(collect())
        self.assertEqual(summary["count"], 100)
        self.assertEqual(summary["retained"], 8)

    def test_fixture_rejects_physical_motor_options_before_app_creation(self):
        with patch("jetson.mapping.server.create_app") as create:
            for options in ({"serial_port": "/dev/test"}, {"simulate_rover": False},
                            {"camera_device": 0}):
                with self.assertRaisesRegex(ValueError, "simulated"):
                    fixture_app("rgb15", server_options=options)
            create.assert_not_called()

    def test_python_cpu_probe_stays_within_each_workers_own_cpu_budget(self):
        def work():
            probe = ThreadCpuProfile()
            previous = sys.getprofile()
            started = time.thread_time()
            sys.setprofile(probe)
            try:
                for _ in range(20):
                    sum(value * value for value in range(1000))
            finally:
                sys.setprofile(previous)
            return probe.cpu, time.thread_time() - started

        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(lambda _: work(), range(2)))
        for cpu, total in results:
            self.assertGreater(cpu["python"], 0)
            self.assertGreater(cpu["native"], 0)
            self.assertLessEqual(sum(cpu.values()), total)
