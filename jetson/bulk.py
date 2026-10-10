"""Optional shared admission pacing for bulk payloads, never control traffic.

HTTP must consume ``chunks`` as a streaming body, not collect the iterator into
one response. WebSocket ``send_packet`` preserves a whole packet; an atomic
packet larger than 25 ms of credit necessarily exceeds that short-window burst
size. This module does not change framing or configure any server endpoint.
"""

import asyncio
from collections.abc import AsyncIterable, Awaitable, Callable, Iterable
from contextlib import asynccontextmanager
import math
import time


class BulkPacketTooLarge(ValueError):
    """A bulk packet cannot fit the enabled atomic handoff limit."""


class BulkPacer:
    BURST_SECONDS = 0.025

    def __init__(self, rate_mbps=0, *, max_http_chunk=16384, max_atomic_bytes=262144,
                 clock=time.monotonic, sleep=asyncio.sleep):
        if type(rate_mbps) not in (int, float) or not math.isfinite(rate_mbps) or rate_mbps < 0:
            raise ValueError("Bulk rate must be a finite nonnegative Mbps number")
        rate = float(rate_mbps) * 1_000_000 / 8
        if not math.isfinite(rate):
            raise ValueError("Bulk rate exceeds finite byte accounting")
        if type(max_http_chunk) is not int or max_http_chunk <= 0:
            raise ValueError("HTTP chunk limit must be a positive integer")
        if type(max_atomic_bytes) is not int or max_atomic_bytes <= 0:
            raise ValueError("Atomic packet limit must be a positive integer")
        self.rate_mbps = float(rate_mbps)
        self.rate_bytes_per_second = rate
        self.enabled = rate > 0
        self.http_chunk_bytes = (max(1, min(max_http_chunk, math.floor(rate * self.BURST_SECONDS)))
                                 if self.enabled else max_http_chunk)
        self.max_atomic_bytes = max_atomic_bytes
        self._clock, self._sleep = clock, sleep
        self._finish = None
        self._lock = asyncio.Lock()
        self._pending = 0

    @property
    def pending_requests(self):
        return self._pending

    async def _admit(self, size):
        now = self._clock()
        start = max(self._finish if self._finish is not None else now - self.BURST_SECONDS,
                    now - self.BURST_SECONDS)
        finish = start + size / self.rate_bytes_per_second
        while finish > now:
            await self._sleep(finish - now)
            now = self._clock()
        # Commit only after the wait completed. The paid sleep ends at finish;
        # the next request never adds that same sleep back as fresh tokens.
        # A cancelled wait leaves no reserved future debt for another stream.
        self._finish = finish

    @asynccontextmanager
    async def _slot(self, size):
        if not self.enabled or size == 0:
            yield
            return
        self._pending += 1
        acquired = False
        try:
            # asyncio.Lock serves already-waiting requests in FIFO order. Keep
            # ownership through the handoff so concurrent writers cannot build
            # an application send queue after independently obtaining credit.
            await self._lock.acquire()
            acquired = True
            await self._admit(size)
            yield
        finally:
            if acquired:
                self._lock.release()
            self._pending -= 1

    @staticmethod
    def _bytes(value):
        if not isinstance(value, (bytes, bytearray, memoryview)):
            raise TypeError("Bulk payloads must be bytes-like")
        return bytes(value)

    async def chunks(self, source: bytes | AsyncIterable[bytes] | Iterable[bytes]):
        """Yield paced HTTP chunks; close the iterator when its consumer stops.

        One chunk owns its slot until the consumer requests the next chunk or
        closes/cancels this iterator. That intentionally observes downstream
        backpressure. A caller must not leave a paused iterator abandoned.
        Disabled mode preserves each incoming block and performs no waits.
        """
        async def blocks():
            if isinstance(source, (bytes, bytearray, memoryview)):
                yield source
            elif isinstance(source, AsyncIterable):
                async for block in source:
                    yield block
            else:
                for block in source:
                    yield block

        async for block in blocks():
            data = self._bytes(block)
            if not self.enabled:
                yield data
                continue
            for offset in range(0, len(data), self.http_chunk_bytes):
                part = data[offset:offset + self.http_chunk_bytes]
                async with self._slot(len(part)):
                    yield part

    async def send_packet(self, payload, send: Callable[[bytes], Awaitable[object]]):
        """Admit and hand off exactly one atomic bulk WebSocket payload.

        The enabled-mode size limit refuses unexpectedly large packets instead
        of splitting a wire message. Use HTTP chunks for large map snapshots.
        Send failure/cancellation after admission stays charged conservatively;
        some bytes may already have reached the transport.
        """
        data = self._bytes(payload)
        if self.enabled and len(data) > self.max_atomic_bytes:
            raise BulkPacketTooLarge("Atomic bulk payload exceeds the configured limit; use a chunked HTTP path")
        async with self._slot(len(data)):
            return await send(data)
