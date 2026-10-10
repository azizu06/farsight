"""Bounded localhost TCP fault injector; never changes system networking.

python -m tools.fault_proxy --port 18843 --upstream-port 18443 \
    --delay-ms 50 --bandwidth-mbps 2 --drop-after 20

Delay is per direction. Bandwidth is shared across all connections/directions.
The optional outage begins after proxy startup and rejects subsequent connects.
Use only software simulation; this tool can deliberately invalidate controls.
"""

import argparse
import asyncio
from contextlib import suppress
import json
import math
import signal
import socket
import time

CHUNK = 16384
WIRE_CHUNK = 4096
QUEUE_CHUNKS = 4
MAX_CONNECTIONS = 32


class Bandwidth:
    """One aggregate token bucket; callers wait instead of collecting payloads."""

    def __init__(self, megabits=0, burst=4096):
        self.rate = megabits * 1000000 / 8
        self.burst = burst
        self.tokens = float(burst)
        self.last = time.monotonic()
        self.lock = asyncio.Lock()

    async def take(self, size):
        if not self.rate:
            return
        async with self.lock:
            now = time.monotonic()
            tokens = min(self.burst, self.tokens + (now - self.last) * self.rate)
            if size > tokens:
                await asyncio.sleep((size - tokens) / self.rate)
                self.tokens = 0
            else:
                self.tokens = tokens - size
            self.last = time.monotonic()


class FaultProxy:
    def __init__(self, upstream_port, *, port=18843, upstream_host="127.0.0.1",
                 delay_ms=0, bandwidth_mbps=0, drop_after=0):
        if upstream_host not in ("127.0.0.1", "::1", "localhost"):
            raise ValueError("Fault injection upstream must be localhost simulation")
        if (type(upstream_port) is not int or not 1 <= upstream_port <= 65535
                or type(port) is not int or not 0 <= port <= 65535 or port == upstream_port):
            raise ValueError("Choose distinct valid upstream/listen ports; zero listen port is allowed for tests")
        for value in (delay_ms, bandwidth_mbps, drop_after):
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ValueError("Delay, bandwidth, and drop time must be finite nonnegative numbers")
        if delay_ms > 5000 or bandwidth_mbps > 10000 or drop_after > 3600:
            raise ValueError("Limits: delay <=5000ms, bandwidth <=10000Mbps, drop time <=3600s")
        self.upstream_port, self.upstream_host, self.port = upstream_port, upstream_host, port
        self.delay = delay_ms / 1000
        self.bandwidth = Bandwidth(bandwidth_mbps)
        self.drop_after = drop_after
        self.server = self.timer = None
        self.tasks, self.writers = set(), set()
        self.failed = False
        self.stats = dict(connections=0, rejected=0, bytes=0, queue_peak_chunks=0,
                          connection_peak=0, dropped=False)

    async def start(self):
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", self.port,
                                                limit=CHUNK * 2, backlog=MAX_CONNECTIONS)
        self.port = self.server.sockets[0].getsockname()[1]
        if self.drop_after:
            async def outage():
                await asyncio.sleep(self.drop_after)
                self.drop()
            self.timer = asyncio.create_task(outage())
        return self

    def drop(self):
        self.failed = True
        self.stats["dropped"] = True
        # Abort rather than graceful close: this is an outage, not a reconnect.
        for writer in tuple(self.writers):
            writer.transport.abort()
        for task in tuple(self.tasks):
            if not task.cancelling():
                task.cancel()

    async def pipe(self, reader, writer):
        queue = asyncio.Queue(maxsize=QUEUE_CHUNKS)

        async def read():
            while True:
                payload = await reader.read(CHUNK)
                await queue.put((time.monotonic() + self.delay, payload))
                self.stats["queue_peak_chunks"] = max(self.stats["queue_peak_chunks"], queue.qsize())
                if not payload:
                    return

        async def write():
            while True:
                ready, payload = await queue.get()
                await asyncio.sleep(max(0, ready - time.monotonic()))
                if not payload:
                    if writer.can_write_eof():
                        writer.write_eof()
                        await writer.drain()
                    return
                # Share the wire in small scheduling units so a 16KiB video
                # read cannot hold the aggregate limiter ahead of control for
                # 65ms at 2Mbps. Reader queues remain separately bounded.
                for offset in range(0, len(payload), WIRE_CHUNK):
                    packet = payload[offset:offset + WIRE_CHUNK]
                    await self.bandwidth.take(len(packet))
                    writer.write(packet)
                    await writer.drain()
                    self.stats["bytes"] += len(packet)

        tasks = [asyncio.create_task(read()), asyncio.create_task(write())]
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def handle(self, reader, writer):
        if self.failed or len(self.tasks) >= MAX_CONNECTIONS:
            self.stats["rejected"] += 1
            writer.transport.abort()
            return
        task = asyncio.current_task()
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        self.writers.add(writer)
        self.stats["connections"] += 1
        self.stats["connection_peak"] = max(self.stats["connection_peak"], len(self.tasks))
        upstream = None
        pipes = []
        try:
            remote_reader, upstream = await asyncio.wait_for(
                asyncio.open_connection(self.upstream_host, self.upstream_port, limit=CHUNK * 2), 3)
            self.writers.add(upstream)
            for connection in (writer, upstream):
                # asyncio skips its automatic TCP_NODELAY setup on accepted
                # sockets whose proto is 0 (observed on macOS). Disable Nagle
                # explicitly so delayed ACKs do not add unrequested 200ms
                # batching to this latency injector.
                connection.get_extra_info("socket").setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                connection.transport.set_write_buffer_limits(high=CHUNK * 2, low=CHUNK // 2)
            pipes = [asyncio.create_task(self.pipe(reader, upstream)),
                     asyncio.create_task(self.pipe(remote_reader, writer))]
            await asyncio.gather(*pipes)
        except (OSError, asyncio.TimeoutError):
            pass
        finally:
            connections = [connection for connection in (writer, upstream) if connection is not None]
            # Release tracking and begin closing before the first cleanup await;
            # cancellation during EOF cleanup must not leave a tracked socket.
            for connection in connections:
                self.writers.discard(connection)
                connection.close()
            for pipe in pipes:
                pipe.cancel()
            try:
                await asyncio.gather(*pipes, return_exceptions=True)
                for connection in connections:
                    try:
                        await asyncio.wait_for(connection.wait_closed(), 0.25)
                    except (OSError, asyncio.TimeoutError):
                        connection.transport.abort()
            except asyncio.CancelledError:
                for connection in connections:
                    connection.transport.abort()
                raise
            self.tasks.discard(task)

    async def close(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        if self.timer:
            self.timer.cancel()
            with suppress(asyncio.CancelledError):
                await self.timer
        for writer in tuple(self.writers):
            writer.transport.abort()
        for task in tuple(self.tasks):
            if not task.cancelling():
                task.cancel()
        await asyncio.gather(*tuple(self.tasks), return_exceptions=True)


async def run(args):
    proxy = FaultProxy(args.upstream_port, port=args.port, delay_ms=args.delay_ms,
                       bandwidth_mbps=args.bandwidth_mbps, drop_after=args.drop_after)
    await proxy.start()
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stopped.set)
    print(f"FAULT INJECTION: localhost:{proxy.port} -> localhost:{proxy.upstream_port}; "
          f"{args.delay_ms:g}ms each direction, aggregate {args.bandwidth_mbps:g}Mbps "
          f"(0 = unlimited), outage after {args.drop_after:g}s (0 = disabled).", flush=True)
    try:
        await stopped.wait()
    finally:
        await proxy.close()
        print(json.dumps(proxy.stats), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, required=True, help="Explicit localhost listen port")
    parser.add_argument("--upstream-port", type=int, required=True, help="Simulated server localhost port")
    parser.add_argument("--delay-ms", type=float, default=0, help="Delay per direction")
    parser.add_argument("--bandwidth-mbps", type=float, default=0, help="Aggregate both directions; zero disables cap")
    parser.add_argument("--drop-after", type=float, default=0, help="Outage N seconds after startup; zero disables")
    args = parser.parse_args()
    try:
        asyncio.run(run(args))
    except (ValueError, OSError) as exc:
        parser.exit(1, str(exc) + "\n")


if __name__ == "__main__":
    main()
