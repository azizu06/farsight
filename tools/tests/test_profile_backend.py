"""Profiling must retain bounded evidence and refuse physical motor configuration."""
import asyncio
import unittest
from unittest.mock import patch

from tools.profile_backend import Timings
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
            for options in ({"serial_port": "/dev/test"}, {"simulate_rover": False}):
                with self.assertRaisesRegex(ValueError, "simulated motors"):
                    fixture_app("rgb15", server_options=options)
            create.assert_not_called()
