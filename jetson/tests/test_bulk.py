"""Independent byte/time and scheduling observations using controlled clocks."""

import asyncio
from contextlib import aclosing
import math
import unittest

from jetson.bulk import BulkPacer


class AutoClock:
    def __init__(self):
        self.now = 0.0
    def __call__(self):
        return self.now
    async def sleep(self, seconds):
        self.now += seconds
        await asyncio.sleep(0)


class ManualClock:
    def __init__(self):
        self.now = 0.0
        self.waiters = []
    def __call__(self):
        return self.now
    async def sleep(self, seconds):
        future = asyncio.get_running_loop().create_future()
        self.waiters.append((self.now + seconds, future))
        try:
            await future
        finally:
            self.waiters = [(due, item) for due, item in self.waiters if item is not future]
    def advance(self, seconds):
        self.now += seconds
        for due, future in self.waiters:
            if due <= self.now + 1e-12 and not future.done():
                future.set_result(None)


class BulkTests(unittest.IsolatedAsyncioTestCase):
    def pacer(self, rate=.08, clock=None, **options):
        clock = clock or AutoClock()
        return BulkPacer(rate, clock=clock, sleep=clock.sleep, **options), clock

    async def collect(self, pacer, data, clock, events, label):
        async with aclosing(pacer.chunks(data)) as chunks:
            async for part in chunks:
                events.append((clock.now, len(part), label, part))
                await asyncio.sleep(0)

    def assert_envelope(self, events, bytes_per_second, burst_seconds=.025):
        # Check all observed windows, not the pacer's internal state or sleep
        # constants. A 25 ms bucket plus elapsed capacity must cover each window.
        for left in range(len(events)):
            total = 0
            for right in range(left, len(events)):
                total += events[right][1]
                elapsed = events[right][0] - events[left][0]
                self.assertLessEqual(total, bytes_per_second * (elapsed + burst_seconds) + 1e-7)

    async def test_http_sustained_rate_and_each_window_remain_inside_shared_budget(self):
        pacer, clock = self.pacer()
        events = []
        original = bytes(range(256)) * 40
        await self.collect(pacer, original, clock, events, "http")
        self.assertEqual(b"".join(event[3] for event in events), original)
        self.assertGreaterEqual(clock.now, (len(original) - 250) / 10000 - 1e-10)
        self.assertLess(clock.now, len(original) / 10000 + .001)
        self.assert_envelope(events, 10000)

    async def test_competing_http_streams_share_one_rate_and_make_fair_progress(self):
        pacer, clock = self.pacer()
        events = []
        await asyncio.gather(self.collect(pacer, b"a" * 4000, clock, events, "a"),
                             self.collect(pacer, b"b" * 4000, clock, events, "b"))
        self.assertEqual({event[2] for event in events[:3]}, {"a", "b"})
        self.assert_envelope(events, 10000)
        self.assertGreaterEqual(clock.now, .775 - 1e-10)
        self.assertLess(clock.now, .801)
        self.assertEqual(sum(event[1] for event in events if event[2] == "a"), 4000)
        self.assertEqual(sum(event[1] for event in events if event[2] == "b"), 4000)

    async def test_sleep_is_not_credited_twice_and_idle_credit_is_bounded(self):
        pacer, clock = self.pacer()
        events = []
        await self.collect(pacer, b"x" * 2000, clock, events, "before")
        first_time = clock.now
        await self.collect(pacer, b"y" * 2000, clock, events, "after")
        self.assertGreaterEqual(clock.now - first_time, .2 - 1e-10)
        clock.now += 30
        idle_start = clock.now
        idle_events = []
        await self.collect(pacer, b"z" * 2000, clock, idle_events, "idle")
        self.assertGreaterEqual(clock.now - idle_start, .175 - 1e-9)
        self.assert_envelope(idle_events, 10000)

    async def test_atomic_packet_stays_one_packet_and_uses_same_accounting_as_http(self):
        pacer, clock = self.pacer(max_atomic_bytes=4000)
        sent = []
        async def send(data):
            sent.append((clock.now, len(data), "ws", data))
            return "sent"
        packet = b"whole packet" * 100
        self.assertEqual(await pacer.send_packet(packet, send), "sent")
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][3], packet)
        await self.collect(pacer, b"h" * 3000, clock, sent, "http")
        total = sum(event[1] for event in sent)
        self.assertLessEqual(total, 10000 * (clock.now + .025) + 1e-7)
        with self.assertRaises(ValueError):
            await pacer.send_packet(b"x" * 4001, send)
        # Atomic messages larger than the budget are intentionally not claimed
        # to obey an instantaneous 25 ms output-window bound.
        self.assertGreater(sent[0][1], 250)

    async def test_cancelled_wait_releases_queue_without_future_debt(self):
        clock = ManualClock()
        pacer, _ = self.pacer(clock=clock)
        sent = []
        async def send(data):
            sent.append((clock.now, data))
        blocked = asyncio.create_task(pacer.send_packet(b"x" * 1000, send))
        await asyncio.sleep(0)
        self.assertTrue(clock.waiters)
        following = asyncio.create_task(pacer.send_packet(b"y" * 250, send))
        await asyncio.sleep(0)
        blocked.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await blocked
        await following
        self.assertEqual(sent, [(0, b"y" * 250)])
        self.assertEqual(clock.waiters, [])
        self.assertEqual(pacer.pending_requests, 0)

    async def test_cancelling_middle_waiter_keeps_following_stream_live(self):
        clock = ManualClock()
        pacer, _ = self.pacer(clock=clock)
        sent = []
        async def send(data):
            sent.append(data)
        first = asyncio.create_task(pacer.send_packet(b"a" * 1000, send))
        await asyncio.sleep(0)
        middle = asyncio.create_task(pacer.send_packet(b"b" * 250, send))
        last = asyncio.create_task(pacer.send_packet(b"c" * 250, send))
        await asyncio.sleep(0)
        middle.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await middle
        clock.advance(.08)
        await first
        await asyncio.sleep(0)
        clock.advance(.03)
        await last
        self.assertEqual(sent, [b"a" * 1000, b"c" * 250])
        self.assertEqual(pacer.pending_requests, 0)
        self.assertEqual(clock.waiters, [])

    async def test_closing_paused_http_iterator_and_cancelling_sender_free_the_slot(self):
        pacer, clock = self.pacer()
        chunks = pacer.chunks(b"x" * 1000)
        self.assertEqual(len(await anext(chunks)), 250)
        called = asyncio.Event()
        async def send(data):
            called.set()
            await asyncio.Event().wait()
        task = asyncio.create_task(pacer.send_packet(b"y" * 100, send))
        await asyncio.sleep(0)
        self.assertFalse(called.is_set())
        await chunks.aclose()
        await called.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(pacer.pending_requests, 0)
        observed = []
        await self.collect(pacer, b"z" * 500, clock, observed, "new")
        self.assertEqual(sum(event[1] for event in observed), 500)

    async def test_disabled_mode_preserves_blocks_and_packet_without_clock_or_waits(self):
        def unused():
            raise AssertionError("disabled pacing must not read clock")
        async def unused_sleep(_):
            raise AssertionError("disabled pacing must not sleep")
        pacer = BulkPacer(0, clock=unused, sleep=unused_sleep, max_atomic_bytes=1)
        blocks = [b"abc", b"defgh"]
        self.assertEqual([part async for part in pacer.chunks(blocks)], blocks)
        sent = []
        async def send(data):
            sent.append(data)
        await pacer.send_packet(b"large allowed original packet", send)
        self.assertEqual(sent, [b"large allowed original packet"])

    async def test_unrelated_control_work_runs_while_bulk_waits(self):
        clock = ManualClock()
        pacer, _ = self.pacer(clock=clock)
        async def send(_):
            return None
        bulk = asyncio.create_task(pacer.send_packet(b"x" * 1000, send))
        await asyncio.sleep(0)
        control = asyncio.create_task(asyncio.sleep(0, result="permit remained unpaced"))
        self.assertEqual(await control, "permit remained unpaced")
        self.assertFalse(bulk.done())
        bulk.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await bulk

    def test_rate_and_limits_are_finite_nonnegative_and_typed(self):
        for rate in (-1, math.nan, math.inf, -math.inf, True, "1", 1e308):
            with self.subTest(rate=rate), self.assertRaises(ValueError):
                BulkPacer(rate)
        for options in ({"max_http_chunk": 0}, {"max_http_chunk": True}, {"max_atomic_bytes": 0}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                BulkPacer(1, **options)


if __name__ == "__main__":
    unittest.main()
