"""Receiver-issued bounds on native sensor capture age, without clock offsets."""

import asyncio
from collections import OrderedDict
import math
import secrets
import threading
import time


class CapturePermits:
    TTL = .250
    PERIOD = .050
    MAX_RETAINED = 8

    def __init__(self, session_id, map_epoch, enabled=False, clock=time.monotonic):
        self.session_id, self.map_epoch = session_id, map_epoch
        self.enabled, self.clock = enabled, clock
        self.active = True
        self.tokens = OrderedDict()
        self.lock = threading.Lock()

    def issue(self):
        with self.lock:
            if not self.active or not self.enabled:
                return None
            now = self.clock()
            self.tokens = OrderedDict((token, issued) for token, issued in self.tokens.items()
                                      if 0 <= now - issued < self.TTL)
            token = secrets.token_urlsafe(24)
            self.tokens[token] = now
            while len(self.tokens) > self.MAX_RETAINED:
                self.tokens.popitem(last=False)
            return dict(type="capture_permit", version=1, session_id=self.session_id,
                        map_epoch=self.map_epoch, token=token, valid_for_ms=250)

    def verified_origin(self, header):
        """Return server issuance time only for a current, bound RGB exposure.

        Client sensor/receipt times are compared only to each other. Neither is
        compared to the server clock or used to fabricate a clock offset.
        """
        capture, receipt = header.get("t_capture"), header.get("capture_permit_received_t")
        token = header.get("capture_permit")
        if (header.get("capture_clock") != "avcapture_sync_to_host"
                or header.get("capture_clock_verified") is not True
                or header.get("capture_bound") is not True
                or header.get("session_id") != self.session_id
                or type(header.get("map_epoch")) is not int or header.get("map_epoch") != self.map_epoch
                or not isinstance(token, str)
                or type(capture) not in (int, float) or type(receipt) not in (int, float)
                or not math.isfinite(capture) or not math.isfinite(receipt)
                or not 0 <= receipt < capture):
            return None
        with self.lock:
            issued = self.tokens.get(token)
            if not self.active or not self.enabled or issued is None:
                return None
            return issued if 0 <= self.clock() - issued < self.TTL else None

    def close(self):
        with self.lock:
            self.active = False
            self.tokens.clear()

    async def run(self, ws, receive):
        """Run an unpaced 20 Hz sender alongside the existing phone receiver."""
        async def send():
            while self.active:
                started = asyncio.get_running_loop().time()
                await asyncio.wait_for(ws.send_json(self.issue()), .1)
                await asyncio.sleep(max(0, self.PERIOD - (asyncio.get_running_loop().time() - started)))
        tasks = []
        try:
            if not self.enabled:
                return await receive()
            tasks = [asyncio.create_task(send()), asyncio.create_task(receive())]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            self.close()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
