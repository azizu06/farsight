"""Link lab emulator tests: real localhost TCP, no OS traffic shaping."""

import asyncio
import random
import time
import unittest

from tools.link_lab import LinkPath, LinkProxy, PROFILES, Profile, resolve_profile


class LinkPathTests(unittest.TestCase):
    def test_base_delay_without_jitter(self):
        path = LinkPath(Profile("t", 40), 0, random.Random(1))
        self.assertAlmostEqual(path.schedule(100, 10.0), 10.04)

    def test_shared_serializer_queues_packets_across_connections(self):
        path = LinkPath(Profile("t", 0), 1, random.Random(1))  # 1 Mbps
        first = path.schedule(1250, 0.0)
        second = path.schedule(1250, 0.0)  # e.g. a permit behind a video packet
        self.assertAlmostEqual(first, 0.01)
        self.assertAlmostEqual(second, 0.02)
        self.assertAlmostEqual(path.backlog(0.0), 0.02)

    def test_loss_costs_a_retransmission_timeout(self):
        path = LinkPath(Profile("t", 10, loss=1, rto_ms=250), 0, random.Random(1))
        self.assertAlmostEqual(path.schedule(100, 0.0), 0.26)
        self.assertEqual(path.stats["lost"], 1)

    def test_stall_holds_packets_until_it_ends(self):
        path = LinkPath(Profile("t", 0, stall_hz=1000, stall_ms=(300, 300)), 0, random.Random(1))
        path.schedule(100, 1.0)  # starts the stall process
        self.assertGreater(path.schedule(100, 1.1), 1.2)
        self.assertGreater(path.stats["stalls"], 0)

    def test_profiles_are_named_and_resolvable(self):
        for name, profile in PROFILES.items():
            self.assertEqual(profile.name, name)
        self.assertEqual(resolve_profile("derp_typical", dict(loss=0.5)).loss, 0.5)
        with self.assertRaises(ValueError):
            resolve_profile("missing")


class LinkProxyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        async def echo(reader, writer):
            while data := await reader.read(65536):
                writer.write(data)
                await writer.drain()
            writer.close()
        self.server = await asyncio.start_server(echo, "127.0.0.1", 0)
        self.upstream_port = self.server.sockets[0].getsockname()[1]

    async def asyncTearDown(self):
        self.server.close()
        await self.server.wait_closed()

    async def roundtrip(self, profile, payload):
        proxy = await LinkProxy(profile, "127.0.0.1", self.upstream_port).start()
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
            started = time.monotonic()
            writer.write(payload)
            await writer.drain()
            received = await asyncio.wait_for(reader.readexactly(len(payload)), 10)
            elapsed = time.monotonic() - started
            writer.close()
            return received, elapsed, proxy.stats()
        finally:
            await proxy.close()

    async def test_round_trip_delay_is_both_directions(self):
        received, elapsed, _ = await self.roundtrip(Profile("t", 40), b"permit")
        self.assertEqual(received, b"permit")
        self.assertGreaterEqual(elapsed, 0.08)
        self.assertLess(elapsed, 0.2)

    async def test_jitter_and_loss_keep_tcp_bytes_in_order(self):
        payload = bytes(range(256)) * 400
        profile = Profile("t", 2, jitter_ms=5, loss=0.05, rto_ms=20)
        received, _, stats = await self.roundtrip(profile, payload)
        self.assertEqual(received, payload)
        self.assertGreater(stats["up"]["lost"] + stats["down"]["lost"], 0)


if __name__ == "__main__":
    unittest.main()
