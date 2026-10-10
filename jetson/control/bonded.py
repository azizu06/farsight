"""Opt-in logical controller shared by two already-attached upstream paths.

All methods run on the owning asyncio loop. ControlState remains the authority
for motor, camera, view, range, release, and receiver-issued lease checks.
"""

import asyncio
from collections import OrderedDict
from dataclasses import dataclass
import hashlib
import hmac
import json
import secrets

from jetson.nav import NavLane


@dataclass
class Attachment:
    path_id: str
    session_id: str
    secret: str
    queue: asyncio.Queue


class BondedControlHub:
    STATUS_INTERVAL = .025
    RECORD_INTERVAL = .050
    CACHE_LIMIT = 256
    MAX_PATHS = 2

    def __init__(self, state, on_status=None):
        self.state, self.on_status = state, on_status
        self.paths = {}
        self.session_id = self.secret = None
        self._cache = OrderedDict()
        self._status_seq = 0
        self._publisher = self._recorder = None
        self._recorded_at = -float("inf")
        self._closed = False
        self.lane = None  # nav revision lane, once the controller opts in

    async def attach(self, secret=None):
        if self._closed:
            raise ValueError("Bonded controller is closed")
        if self.session_id is None:
            if secret is not None:
                raise ValueError("Attachment session is no longer active")
            session = self.state.begin()
            if session is None:
                raise ValueError("Another controller owns the rover")
            self.session_id, self.secret = session, secrets.token_hex(32)
            self.lane = None
            self._cache.clear()
            self._recorded_at = -float("inf")
        elif not isinstance(secret, str) or not hmac.compare_digest(
                secret.encode("utf-8", "surrogatepass"), self.secret.encode("ascii")):
            raise ValueError("Invalid attachment secret")
        if len(self.paths) >= self.MAX_PATHS:
            raise ValueError("Two bonded paths are already attached")
        path = Attachment(secrets.token_hex(16), self.session_id, self.secret, asyncio.Queue(maxsize=1))
        self.paths[path.path_id] = path
        if self.lane is not None:
            self.lane = NavLane()  # a new path gets the nav arrays in its first status
        if self._publisher is None:
            self._publisher = asyncio.create_task(self._publish())
        return path

    def _detach(self, path_id):
        path = self.paths.pop(path_id, None)
        if path is None:
            return None
        # A full status queue is a path fault, not an invitation to hide stale
        # consumption by continually replacing statuses. Discard only during
        # termination and install a sentinel so a waiting handler can exit.
        if path.queue.full():
            path.queue.get_nowait()
        path.queue.put_nowait(None)
        if not self.paths:
            self.state.end(self.session_id)
            self.session_id = self.secret = None
            self._cache.clear()
            tasks = [task for task in (self._publisher, self._recorder) if task is not None]
            self._publisher = self._recorder = None
            for task in tasks:
                if task is not asyncio.current_task():
                    task.cancel()
            return tasks
        return []

    async def detach(self, path_id):
        tasks = self._detach(path_id) or []
        await asyncio.gather(*(task for task in tasks if task is not asyncio.current_task()),
                             return_exceptions=True)

    def accept(self, message, path_id):
        if path_id not in self.paths or self.session_id is None:
            return False  # unauthenticated/detached paths cannot affect an owner
        self.state.output()  # duplicates never postpone an existing safety expiry
        if not isinstance(message, dict):
            self.state.stop("binary_control_rejected" if isinstance(message, bytes) else "invalid_json")
            return False
        if message.get("session_id") != self.session_id:
            self.state.stop("wrong_session")
            return False
        if self.lane is None and message.get("nav_lane") == "revision":
            self.lane = NavLane()
        seq = message.get("seq")
        if type(seq) is not int or not 0 <= seq <= 2**53 - 1:
            self.state.stop("invalid_sequence")
            return False
        try:
            encoded = json.dumps(message, sort_keys=True, separators=(",", ":"),
                                 ensure_ascii=True, allow_nan=False).encode("utf-8")
        except (TypeError, ValueError, RecursionError):
            self.state.stop("invalid_json")
            return False
        if len(encoded) > 2048:
            self.state.stop("oversized_command")
            return False
        digest = hashlib.sha256(encoded).digest()
        previous = self._cache.get(seq)
        if previous is not None:
            if not hmac.compare_digest(previous, digest):
                self.state.stop("conflicting_duplicate")
            return False
        with self.state.lock:
            if seq <= self.state.seq:
                return False  # obsolete copy, including an already-applied release
            self._cache[seq] = digest
            while len(self._cache) > self.CACHE_LIMIT:
                self._cache.popitem(last=False)
            if message.get("deadman") is True:
                permit = message.get("permit")
                if not isinstance(permit, str) or not permit:
                    self.state.stop("expired_permit")
                    return False
                expiry = self.state.permits.get(permit, 0)
                if expiry <= self.state.clock():
                    return False  # a delayed copy supplies no new lease or motion
            return self.state.accept(message, self.session_id)

    async def _record(self, status):
        try:
            await self.on_status(status)
        except Exception:
            pass  # evidence recording cannot change motion authority

    async def _publish(self):
        try:
            while self.paths:
                started = asyncio.get_running_loop().time()
                status = dict(self.state.status(issue_permit=True))
                if status.get("session_id") != self.session_id:
                    for path_id in tuple(self.paths):
                        self._detach(path_id)
                    return
                self._status_seq += 1
                status["status_seq"] = self._status_seq
                # Attachment credentials are never status or recorded data.
                for name in ("secret", "attach_secret"):
                    status.pop(name, None)
                # One lane decision per status_seq, so both paths carry identical copies.
                sent = self.lane.apply(status) if self.lane is not None else status
                for path in tuple(self.paths.values()):
                    if path.queue.full():
                        self._detach(path.path_id)
                    else:
                        path.queue.put_nowait(sent)
                if (self.paths and self.on_status is not None
                        and started - self._recorded_at >= self.RECORD_INTERVAL
                        and (self._recorder is None or self._recorder.done())):
                    self._recorded_at = started
                    self._recorder = asyncio.create_task(self._record(status))
                if self.paths:
                    await asyncio.sleep(max(0, self.STATUS_INTERVAL -
                                            (asyncio.get_running_loop().time() - started)))
        except asyncio.CancelledError:
            raise
        except Exception:
            # A publisher fault cannot leave a controller alive without status.
            for path_id in tuple(self.paths):
                self._detach(path_id)

    async def close(self):
        self._closed = True
        tasks = [task for task in (self._publisher, self._recorder) if task is not None]
        for path_id in tuple(self.paths):
            self._detach(path_id)
        for task in tasks:
            if task is not asyncio.current_task():
                task.cancel()
        await asyncio.gather(*(task for task in tasks if task is not asyncio.current_task()),
                             return_exceptions=True)
        self._recorder = None
