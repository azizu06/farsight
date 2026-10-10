"""Repeatable relay failover/recovery qualification. Simulation only.

python -m tools.qualify_failover --output failover.json --markdown failover.md

Topology, each piece its own process, every port chosen by bind-port-0:

    operator client
      -> tools.operator_relay (production relay; one upstream, unchanged)
      -> path A or path B (independently cut/stalled/restored)
      -> python -m jetson.server --demo --simulate-rover (no serial port)

The production relay takes exactly one upstream. This harness never changes
that. Failover is modelled here as "which upstream to try": a small pluggable
function (choose_upstream) picks path A (primary) or path B (fallback) before
the relay process is started. An already-open TCP/WebSocket cannot move to the
other path; a midstream cut is reported as a stopped interval, then a new
connection through the surviving path. Connection-time fallback and midstream
continuity are separate rows in the report.

Passing this is software evidence. It is not hardware qualification. The
optional --live-origin mode only GETs /health and reads control status. It
never opens /ws/control and never requests motion.

Safety this harness will not violate:
- never opens a serial port, never sends a physical motor command
- nonzero commands go only to a server that reported simulated:true
- never resends a held command after a required stop, and never resumes one
  on its own. A new session opens latched. A new press with no release must
  be rejected, and motion resumes only after an ordered release.
"""

import argparse
import atexit
import asyncio
from contextlib import suppress
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time

import aiohttp

from jetson.control.state import ControlState
from tools.operator_relay import upstream_origin
from tools.qualify_link import percentile

ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = ROOT / "jetson" / "control" / "state.py"
QUEST_ROVER = ROOT / "quest" / "src" / "rover.ts"
MOTION_V = 0.4
SAMPLE_S = 0.15
# How long a connection-time try will wait for a path that never answers. A
# refusal that comes back sooner is reported at the time it came back. This
# number is a cap, not a measurement, and it is labelled probe_timeout_s in
# the report. A midstream gap stops at the new session's first status.
PROBE_TIMEOUT_S = 0.4
NOT_MEASURABLE = "not measurable"
# Status frames arrive at 40 Hz (ControlService.STATUS_INTERVAL is 0.025s), and
# a send lands between frames. 120 ms can miss the echo and then the check
# fails for the wrong reason. 200 ms still ends before the 250 ms lease, so a
# wrongly accepted press cannot expire its way into a pass.
REPLY_WAIT_S = 0.20
REQUIRED_FIELDS = ("scenario", "scope", "phase", "active_path", "interruption_s",
                   "control_rtt", "video_age_ms", "last_output_pwm", "stop_reason",
                   "explicit_release", "service", "passed", "reason")
PATHS = ("A", "B")

SCENARIO_EXPECT = {
    # Connection time: the other path keeps HTTP and a fresh control session.
    "primary_unavailable_before_connect": "service",
    # Midstream: the open stream dies. The lease stops the motors. A new
    # session on the surviving path restores viewing only, never the held stick.
    "primary_loss_http": "stop_then_recover",
    "primary_loss_h264": "stop_then_recover",
    "primary_loss_control": "stop_then_recover",
    # A silent primary cannot be failed over underneath an open stream.
    "stalled_primary": "stop",
    # One dead path at connect time; the other keeps service.
    "fallback_unavailable": "service",
    # Nothing left to try. Motors must be stopped, not held.
    "both_paths_lost": "stop",
    # Primary returns. A new session may use it. The old command stays dead
    # until the operator releases and presses again.
    "recovery": "stop_then_recover",
}


def read_view_guard_ms(state_text=None, quest_text=None):
    """The 500 ms view guard, read from the source that enforces it.

    ControlState.accept compares video_age_ms to a bare number. The Quest
    client uses the same number when it decides video_fresh. There is no
    named constant to import, so this reads both sites. A moved number fails
    the harness instead of being compared to a copy of itself.
    """
    state_text = state_text if state_text is not None else STATE_PATH.read_text()
    quest_text = quest_text if quest_text is not None else QUEST_ROVER.read_text()
    state_bounds = re.findall(r"0\s*<=\s*video_age\s*<=\s*(\d+)", state_text)
    quest_bounds = re.findall(r"video_fresh:\s*age\s*<\s*(\d+)", quest_text)
    if len(state_bounds) != 1 or len(quest_bounds) != 1:
        raise RuntimeError(
            f"View guard could not be read from the code (state matches {state_bounds}, "
            f"quest matches {quest_bounds}). Refusing to guess 500.")
    if state_bounds[0] != quest_bounds[0]:
        raise RuntimeError(
            f"View guard disagrees: jetson/control/state.py says {state_bounds[0]} ms, "
            f"quest/src/rover.ts says {quest_bounds[0]} ms.")
    return int(state_bounds[0])


def guard_constants():
    """Read the live safety constants. Never hard-code a second copy."""
    lease_s = ControlState.LEASE
    return {"permit_lease_s": lease_s, "permit_lease_ms": round(lease_s * 1000),
            "view_guard_ms": read_view_guard_ms(),
            "view_guard_source": "jetson/control/state.py accept() and quest/src/rover.ts"}


def assert_guards_intact(constants=None):
    constants = constants or guard_constants()
    if constants["permit_lease_s"] != 0.250 or constants["view_guard_ms"] != 500:
        raise RuntimeError(
            f"Safety constants moved: lease {constants['permit_lease_s']}s, "
            f"view guard {constants['view_guard_ms']}ms. Expected 0.250s and 500ms. "
            "This harness will not lengthen either one.")
    return constants


def free_port():
    """A free high port from the kernel. Never 8080, 8443, or 8081."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    if port in (8080, 8443, 8081):
        return free_port()
    return port


def choose_upstream(paths, *, prefer="A", probe=None, probe_all=True):
    """Try each upstream, preferred first, and pick the first one that answers.

    `paths` is {"A": {"origin": url|None, "available": bool}, "B": {...}}.
    `probe(name, origin)` returns True when that path answered. Without a
    probe, an available origin counts as answering, so the unit test can stay
    synchronous. With one, a path marked down is still tried: the probe times
    the refusal, and that time is the connection-time gap.

    probe_all is for the report. True keeps probing the paths after the first
    one answers, so the report can show what the fallback would have done.
    False stops at the first answer. GodlyDonuts' phone bridge must pass
    probe_all=False: with True, every connect also waits out the fallback
    probe even though the primary already answered.

    Returns (name, origin, tries). tries is [{name, origin, ok, elapsed_s}, ...].
    This is the seam GodlyDonuts' phone-bridge fallback will bind to. It chooses
    before a relay starts. It does not move an established stream.
    """
    order = (prefer, *(name for name in PATHS if name != prefer))
    tries = []
    chosen = None, None
    for name in order:
        if chosen[0] is not None and not probe_all:
            break
        path = paths.get(name) or {}
        origin = path.get("origin")
        started = time.monotonic()
        if probe is None:
            ok = bool(path.get("available") and origin)
            elapsed = 0.0
        else:
            ok = bool(probe(name, origin))
            elapsed = time.monotonic() - started
        tries.append({"name": name, "origin": origin, "ok": ok,
                      "elapsed_s": round(elapsed, 6),
                      "elapsed_ms": round(elapsed * 1000, 3)})
        if ok and chosen[0] is None:
            chosen = name, origin
    return (*chosen, tries)


def refused_gap(tries):
    """Seconds spent trying paths that refused, before the one that answered.

    None when every try refused: the outage is still going, so a number would
    be the harness giving up, not the path coming back.
    """
    if not tries or not any(item["ok"] for item in tries):
        return None
    spent = 0.0
    for item in tries:
        if item["ok"]:
            break
        spent += item["elapsed_s"]
    return round(spent, 6)


def motion_message(session_id, permit, seq, *, v=MOTION_V, video_age_ms=40):
    """One simulated drive heartbeat. Callers must not send this to a live origin."""
    return {"session_id": session_id, "permit": permit, "seq": seq, "deadman": True,
            "v": v, "w": 0.0, "pan": 0, "mode": "teleop",
            "video_fresh": True, "video_age_ms": video_age_ms}


def release_message(session_id, seq):
    return {"session_id": session_id, "seq": seq, "deadman": False, "v": 0, "w": 0, "mode": "teleop"}


class OwnedProcess:
    """A process this harness started. close() is bounded and idempotent."""

    def __init__(self, command, log_path):
        self.log = open(log_path, "w+")
        self.process = subprocess.Popen(
            [sys.executable, *command], cwd=ROOT, stdout=self.log, stderr=self.log,
            start_new_session=True)
        self.command = command

    @property
    def pid(self):
        return self.process.pid

    def poll(self):
        return self.process.poll()

    def tail(self):
        self.log.seek(0)
        return self.log.read()[-2000:]

    def close(self, timeout=4):
        process = self.process
        if process.poll() is None:
            with suppress(ProcessLookupError, PermissionError):
                os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                with suppress(ProcessLookupError, PermissionError):
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=timeout)
        self.log.close()


class ProcessGroup:
    """Every process the harness owns, torn down even when a scenario raises.

    Registered with atexit and with SIGTERM/SIGINT. A kill of this process
    must not leave the simulated Jetson or the relay holding ports.
    """

    _active = []

    def __init__(self):
        self.owned = []
        self.forwarders = []
        self.listeners = []
        self.temp = tempfile.TemporaryDirectory(prefix="farsight-failover-")
        self.closed = False
        ProcessGroup._active.append(self)

    def spawn(self, *command):
        owned = OwnedProcess(command, Path(self.temp.name) / f"{len(self.owned)}.log")
        self.owned.append(owned)
        return owned

    def track(self, forwarder):
        self.forwarders.append(forwarder)
        return forwarder

    def close(self):
        if self.closed:
            return
        self.closed = True
        with suppress(ValueError):
            ProcessGroup._active.remove(self)
        for forwarder in self.forwarders:
            with suppress(Exception):
                forwarder.close()
        self.forwarders.clear()
        for listener in self.listeners:
            with suppress(Exception):
                listener.close()
        self.listeners.clear()
        # Relay first, so it drops its upstream sockets before the proxies die.
        for owned in reversed(self.owned):
            with suppress(Exception):
                owned.close()
        self.owned.clear()
        with suppress(Exception):
            self.temp.cleanup()

    @classmethod
    def close_all(cls):
        for group in list(cls._active):
            with suppress(Exception):
                group.close()


def _atexit_reap():
    """Named so a test can see the registration. atexit hides its callback list."""
    ProcessGroup.close_all()


atexit.register(_atexit_reap)


def _on_signal(signum, _frame):
    ProcessGroup.close_all()
    raise SystemExit(128 + signum)


def install_signal_handlers():
    """Replace Ctrl-C only for a real run. Importing this module must not."""
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)


class Forwarder:
    """Localhost TCP forwarder this harness owns.

    tools/fault_proxy.py does abort the client socket on drop() (it tracks the
    accepted writer and calls transport.abort). It has no stall that freezes
    bytes in place, and no restore that reopens the listen port. This forwarder
    is the same plain-TCP shape and adds those two. It never rewrites bytes
    except for an explicit stall, which holds each chunk until unstall().
    """

    def __init__(self, listen_port, upstream_port):
        self.listen_port = listen_port
        self.upstream_port = upstream_port
        self.stalled = False
        self.stall_up = False
        self.server = None
        self.task = None
        self.writers = set()

    async def start(self):
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", self.listen_port)

        async def serve():
            async with self.server:
                await self.server.serve_forever()

        self.task = asyncio.create_task(serve())
        return self

    async def _pipe(self, reader, writer, *, direction):
        try:
            while True:
                payload = await reader.read(16384)
                if not payload:
                    return
                # Freeze only the direction the scenario asked for. A stall of
                # the return path holds the Jetson's bytes until unstall(). It
                # does not sleep and then deliver them. The operator's own
                # bytes still go out, which is what a stall test has to prove.
                if self._frozen(direction):
                    payload = await self._hold(writer, reader, payload, direction)
                    if payload is None:
                        return
                writer.write(payload)
                await writer.drain()
        except (OSError, asyncio.CancelledError, ConnectionError):
            return

    def _frozen(self, direction):
        if direction == "down" and self.stalled:
            return True
        if direction == "up" and self.stall_up:
            return True
        return False

    async def _hold(self, writer, reader, payload, direction):
        """Hold this chunk. A newer one replaces it, so a slow request's reply
        is what gets delivered when the stall lifts, not a stale fast one."""
        held = payload
        while self._frozen(direction):
            if writer.is_closing():
                return None
            try:
                newer = await asyncio.wait_for(reader.read(16384), 0.02)
            except asyncio.TimeoutError:
                continue
            if not newer:
                return None
            held = newer
        return held

    async def _handle(self, reader, writer):
        self.writers.add(writer)
        upstream = None
        pipes = []
        try:
            remote_reader, upstream = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", self.upstream_port), 3)
            self.writers.add(upstream)
            writer.transport.pause_reading()
            for connection in (writer, upstream):
                sock = connection.get_extra_info("socket")
                if sock is not None:
                    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            pipes = [asyncio.create_task(self._pipe(reader, upstream, direction="up")),
                     asyncio.create_task(self._pipe(remote_reader, writer, direction="down"))]
            await asyncio.sleep(0)
            writer.transport.resume_reading()
            await asyncio.gather(*pipes)
        except (OSError, asyncio.TimeoutError, ConnectionError):
            pass
        finally:
            for task in pipes:
                task.cancel()
            for connection in (writer, upstream):
                if connection is None:
                    continue
                self.writers.discard(connection)
                connection.close()

    def stall(self):
        """Freeze bytes coming back from the Jetson. Operator bytes still go out.

        The heartbeat keeps renewing with the permit it already holds. What
        stops the motors is that no newer permit comes back, so the held one
        expires. A stall that delayed bytes and then delivered them would renew.
        """
        self.stalled = True

    def stall_upstream(self):
        """Freeze the operator's own bytes. A request can be sent, then stuck."""
        self.stall_up = True

    def unstall(self):
        self.stalled = False
        self.stall_up = False

    def reset(self):
        """Abort every accepted socket. The operator sees the path die now."""
        for writer in tuple(self.writers):
            transport = getattr(writer, "transport", None)
            if transport is not None:
                transport.abort()
        self.writers.clear()
        if self.server is not None:
            self.server.close()
        if self.task is not None:
            self.task.cancel()
            self.task = None
        self.server = None

    def close(self):
        self.reset()


class CutListener:
    """Refuses a port after its forwarder is gone, so a reconnect cannot sneak through."""

    def __init__(self, port):
        self.port = port
        self.server = None
        self.task = None

    async def start(self):
        self.server = await asyncio.start_server(self._refuse, "127.0.0.1", self.port)

        async def serve():
            async with self.server:
                await self.server.serve_forever()

        self.task = asyncio.create_task(serve())
        return self

    async def _refuse(self, reader, writer):
        writer.transport.abort()
        with suppress(Exception):
            writer.close()

    def close(self):
        if self.server is not None:
            self.server.close()
        if self.task is not None:
            self.task.cancel()


class PathProxy:
    """One upstream path. A forwarder this harness owns, so a cut aborts clients.

    The production relay still sees one plain TCP upstream. Fault injection is
    cut, stall, and restore. tools/fault_proxy.py already aborts the client on
    drop(); it has no freeze-in-place stall and no restore, which is why this
    class exists.
    """

    def __init__(self, name, jetson_port):
        self.name = name
        self.jetson_port = jetson_port
        self.port = free_port()
        self.forwarder = None
        self.available = False
        self.stalled = False
        self.probe_origin = None

    @property
    def origin(self):
        return f"http://127.0.0.1:{self.port}" if self.available else None

    async def start(self, group):
        self.forwarder = group.track(Forwarder(self.port, self.jetson_port))
        await self.forwarder.start()
        self.available = True
        self.stalled = False
        return self

    def cut(self):
        """Hard loss. Abort every client socket, then stop listening."""
        if self.forwarder is not None:
            self.forwarder.reset()
            self.forwarder = None
        self.available = False
        self.stalled = False
        self.probe_origin = None

    async def reset_clients(self):
        self.cut()

    def poll_alive(self):
        return self.forwarder is not None and self.available

    async def occupy(self, group):
        """Refuse the dead port so a reconnect finds nothing to talk to."""
        listener = CutListener(self.port)
        try:
            await asyncio.wait_for(listener.start(), 1)
        except (OSError, asyncio.TimeoutError):
            listener.close()
            return None
        group.listeners.append(listener)
        return listener


async def wait_http(session, process, url, *, timeout, check=None):
    until = time.monotonic() + timeout
    last = "no response"
    while time.monotonic() < until:
        if process is not None and process.poll() is not None:
            raise RuntimeError(f"{url} process exited: {process.tail()}")
        try:
            async with session.get(url) as response:
                if response.status == 200:
                    body = await response.json(content_type=None)
                    if check is None or check(body):
                        return body
                    last = "not ready: " + json.dumps(body)[:200]
                else:
                    last = f"HTTP {response.status}"
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, OSError) as exc:
            last = type(exc).__name__
        await asyncio.sleep(0.05)
    raise RuntimeError(f"Not ready ({url}): {last}")


async def http_ok(session, url, *, timeout=1.0):
    try:
        async with session.get(url) as response:
            return response.status == 200, response.status
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
        return False, None


class ControlSession:
    """One /ws/control session. Tracks what was actually sent.

    A held command is never forwarded on by the harness. inject_replay_attack
    is the only place those bytes are sent again, and only as an attack the
    simulated server must reject.
    """

    def __init__(self, ws, first):
        self.ws = ws
        self.session_id = first.get("session_id")
        self.seq = int(first.get("seq", -1))
        self.permit = first.get("permit")
        self.latest = first
        self.sent = []
        self.stopped = False
        self.released = False

    def note(self, message):
        if message.get("session_id") == self.session_id:
            self.latest = message
            if message.get("permit"):
                self.permit = message["permit"]

    async def pump_one(self, timeout=1.0):
        message = await asyncio.wait_for(self.ws.receive_json(), timeout)
        self.note(message)
        return message

    async def reply_for(self, seq, timeout=REPLY_WAIT_S):
        """The next status that echoes this seq. Bounded under the lease.

        Returns None if that echo does not arrive in time. A check that then
        reads /control/status is looking at a message the server never handled,
        which is exactly the hollow pass this used to report.

        A rejection can echo the seq from before the command: accept() returns
        False without storing it, and the next status still carries the old seq.
        Once a frame arrives that was sent after ours, the server has had its
        chance. That frame is the answer, even when its seq did not move.
        """
        until = time.monotonic() + timeout
        sent_at = time.monotonic()
        # The first frame can already have been in flight when we sent. One
        # frame at the old seq is not an answer. Two of them, past 50 ms, are:
        # the server has published status since the command and did not store it.
        stale_frames = 0
        while time.monotonic() < until:
            remaining = until - time.monotonic()
            if remaining <= 0:
                break
            try:
                message = await self.pump_one(timeout=remaining)
            except (asyncio.TimeoutError, aiohttp.ClientError, RuntimeError, OSError, TypeError):
                return None
            if message.get("seq") == seq:
                return message
            if message.get("seq", -1) < seq:
                stale_frames += 1
            if message.get("seq", -1) < seq and stale_frames >= 2 and time.monotonic() - sent_at > 0.05:
                message = dict(message)
                message["answered_seq"] = seq
                message["echoed_prior_seq"] = True
                return message
        return None

    async def release(self):
        self.seq += 1
        message = release_message(self.session_id, self.seq)
        await self.ws.send_json(message)
        self.sent.append({"kind": "release", "seq": self.seq, "v": 0})
        self.released = True
        self.stopped = False
        return message

    async def drive_once(self, *, v=MOTION_V, video_age_ms=40):
        """Send one new heartbeat. Refuses to send if this session already stopped."""
        if self.stopped:
            raise RuntimeError("Refusing to drive a session that already required a stop")
        if not self.permit:
            raise RuntimeError("No permit; refusing to invent one")
        self.seq += 1
        message = motion_message(self.session_id, self.permit, self.seq, v=v, video_age_ms=video_age_ms)
        await self.ws.send_json(message)
        self.sent.append({"kind": "motion", "seq": self.seq, "v": v, "permit": self.permit})
        return message


def pwm_zero(status):
    return status.get("left_pwm") == 0 and status.get("right_pwm") == 0


def latched(status):
    return pwm_zero(status) and status.get("requires_release") is True


class Harness:
    def __init__(self, *, h264=False, choose=choose_upstream):
        self.h264 = h264
        self.choose = choose
        self.group = ProcessGroup()
        self.session = None
        self.jetson = None
        self.jetson_port = None
        self.jetson_origin = None
        self.paths = {}
        self.relay = None
        self.relay_origin = None
        self.active_path = None
        self.simulated_confirmed = False
        self.last_tries = []

    async def __aenter__(self):
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, connect=2, sock_read=3))
        try:
            await self.start_jetson()
            return self
        except BaseException:
            await self.close()
            raise

    async def __aexit__(self, *exc):
        await self.close()

    async def close(self):
        if self.session is not None:
            with suppress(Exception):
                await self.session.close()
            self.session = None
        self.group.close()

    async def start_jetson(self):
        self.jetson_port = free_port()
        self.jetson_origin = f"http://127.0.0.1:{self.jetson_port}"
        command = ["-m", "jetson.server", "--demo", "--simulate-rover",
                   "--host", "127.0.0.1", "--port", str(self.jetson_port),
                   "--detector", "none", "--proximity-stop", "off"]
        if self.h264:
            command.append("--h264")
        self.jetson = self.group.spawn(*command)

        def ready(body):
            control = body.get("control") or {}
            # The harness must see the server admit it is simulated before any motion.
            if control.get("simulated") is not True:
                raise RuntimeError("Simulated Jetson did not report simulated:true; refusing to continue")
            self.simulated_confirmed = True
            return body.get("camera") is True

        await wait_http(self.session, self.jetson, self.jetson_origin + "/health", timeout=30, check=ready)
        for name in PATHS:
            proxy = PathProxy(name, self.jetson_port)
            await proxy.start(self.group)
            await wait_http(self.session, None, proxy.origin + "/health", timeout=10, check=lambda body: True)
            self.paths[name] = proxy

    def path_table(self):
        return {name: {"origin": proxy.origin or getattr(proxy, "probe_origin", None),
                       "available": proxy.available, "stalled": proxy.stalled}
                for name, proxy in self.paths.items()}

    def probe_path(self, name, origin, wait_s=None):
        """One GET /health. Returns as soon as the path answers or refuses.

        The elapsed time is measured by choose_upstream around this call. A
        cut port is occupied by a listener that aborts the socket, so a refusal
        is usually under a millisecond, and that is the number the report uses.
        wait_s is only the cap for a path that never answers. It is not added
        on after a refusal, and it is not the connection-time gap.
        """
        if not origin:
            return False
        wait_s = PROBE_TIMEOUT_S if wait_s is None else wait_s
        sock = socket.socket()
        sock.settimeout(wait_s)
        try:
            sock.connect(("127.0.0.1", int(origin.rsplit(":", 1)[-1])))
            sock.sendall(b"GET /health HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n")
            reply = sock.recv(64)
            if reply.startswith(b"HTTP/1.1 200") or reply.startswith(b"HTTP/1.0 200"):
                return True
        except (OSError, ValueError):
            return False
        finally:
            sock.close()
        return False

    def pick(self, prefer="A", *, probe=False, wait_s=None, probe_all=True):
        probe_fn = None
        if probe:
            probe_fn = lambda name, origin: self.probe_path(name, origin, wait_s)
        return self.choose(self.path_table(), prefer=prefer, probe=probe_fn, probe_all=probe_all)

    async def pick_probed(self, prefer="A", wait_s=None):
        """Same picker, but the GET /health runs off the loop.

        The refuse listener lives on this loop. A blocking recv here never
        gets its RST, and the try hangs until the timeout.
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: self.pick(prefer, probe=True, wait_s=wait_s))

    async def connect_relay(self, prefer="A", *, probe=False):
        """Start a new relay at the upstream choose_upstream picks. One upstream only."""
        if self.relay is not None:
            self.relay.close()
            self.relay = None
            self.relay_origin = None
            self.active_path = None
        if probe:
            name, origin, tries = await self.pick_probed(prefer)
        else:
            name, origin, tries = self.pick(prefer)
        self.last_tries = tries
        if name is None:
            return None
        port = free_port()
        self.relay = self.group.spawn(
            "-m", "tools.operator_relay", "--upstream", origin, "--port", str(port))
        self.relay_origin = f"http://127.0.0.1:{port}"
        await wait_http(self.session, self.relay, self.relay_origin + "/relay/health", timeout=10)
        self.active_path = name
        return name

    async def direct_status(self):
        """Read-only motor truth from the simulated Jetson. Not a control socket."""
        async with self.session.get(self.jetson_origin + "/control/status") as response:
            response.raise_for_status()
            return await response.json(content_type=None)

    async def video_age(self):
        if self.relay_origin is None:
            return None, None
        try:
            async with self.session.get(self.relay_origin + "/camera.jpg") as response:
                if response.status != 200:
                    return None, None
                await response.read()
                age = response.headers.get("X-Frame-Age-Ms")
                source = response.headers.get("X-Frame-Source")
                return (int(age) if age is not None and age.lstrip("-").isdigit() else None), source
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError):
            return None, None

    async def open_control(self, timeout=4):
        ws = await self.session.ws_connect(
            self.relay_origin + "/ws/control", timeout=aiohttp.ClientWSTimeout(ws_close=0.25))
        first = await asyncio.wait_for(ws.receive_json(), timeout)
        control = ControlSession(ws, first)
        if not control.session_id:
            await ws.close()
            raise RuntimeError("Control socket did not supply a session_id")
        return control

    async def sample_rtt(self, control, *, samples=4):
        """Time a stop-only heartbeat until status echoes its seq. Never a motion byte."""
        measured = []
        for _ in range(samples):
            sent = time.monotonic()
            control.seq += 1
            target = control.seq
            await control.ws.send_json(release_message(control.session_id, target))
            control.sent.append({"kind": "release", "seq": target, "v": 0})
            control.released = True
            reply = await control.reply_for(target, timeout=1.5)
            if reply is not None:
                measured.append((time.monotonic() - sent) * 1000)
            await asyncio.sleep(SAMPLE_S)
        return measured

    async def hold_motion(self, control, *, seconds=0.5, keep_alive=False):
        """Drive the simulated rover. Confirms simulation before the first nonzero byte.

        keep_alive stays inside the 250 ms lease by sending a fresh heartbeat
        for the rest of `seconds`. A stall test needs that, or the lease runs
        out whether or not the path is stalled.
        """
        if not self.simulated_confirmed:
            raise RuntimeError("Refusing nonzero command: simulation was not confirmed")
        status = await self.direct_status()
        if status.get("simulated") is not True:
            raise RuntimeError("Refusing nonzero command: /control/status simulated is not true")
        await control.release()
        released = await control.reply_for(control.seq, timeout=1.0)
        if released is None or released.get("requires_release") is not False:
            return False
        until = time.monotonic() + seconds
        saw_motion = False
        while time.monotonic() < until:
            await control.pump_one(timeout=1)
            if not control.permit:
                continue
            await control.drive_once()
            reply = await control.reply_for(control.seq, timeout=0.4)
            if reply is not None and (reply.get("left_pwm") or reply.get("right_pwm")):
                saw_motion = True
                if not keep_alive:
                    return True
            elif keep_alive and saw_motion:
                return False
        return saw_motion

    async def inject_replay_attack(self, control):
        """Attack, simulated server only: resend the exact held motion bytes.

        Same session, same seq, same permit, same v. Not a new command, and not
        the harness resuming motion. The server must answer that seq with a
        rejection before the lease can expire on its own. A check that only
        looks at PWM 0 after 300 ms cannot tell those apart.
        """
        last = next((item for item in reversed(control.sent) if item["kind"] == "motion"), None)
        if last is None or control.ws.closed:
            return {"rejected": False, "reason": "attack was not delivered",
                    "held_command_replayed": True}
        attack_seq = last["seq"]
        sent_at = time.monotonic()
        await control.ws.send_json(motion_message(
            control.session_id, last["permit"], attack_seq, v=last["v"]))
        # Under the lease. If the server ignored the attack, this times out and
        # the check fails. It does not fall through to a later PWM-zero poll.
        reply = await control.reply_for(attack_seq, timeout=REPLY_WAIT_S)
        elapsed_s = round(time.monotonic() - sent_at, 6)
        if reply is None:
            return {"rejected": False, "after_reason": None, "after_pwm": [None, None],
                    "replayed_seq": attack_seq, "held_command_replayed": True,
                    "elapsed_s": elapsed_s,
                    "reason": "server never answered the replayed seq"}
        rejected = (reply.get("reason") in ("stale_sequence", "expired_permit", "release_required")
                    and pwm_zero(reply) and reply.get("requires_release") is True)
        return {"rejected": rejected, "after_reason": reply.get("reason"),
                "after_pwm": [reply.get("left_pwm"), reply.get("right_pwm")],
                "replayed_seq": attack_seq, "echoed_seq": reply.get("seq"),
                "elapsed_s": elapsed_s,
                "held_command_replayed": not rejected}

    async def inject_old_command_attack(self, old):
        """Attack, simulated server only: the held command, aimed at a new session.

        A new session starts latched (reason release_required) and clears every
        old permit. The injected message must be answered with a rejection of
        that seq. Reading status before the server has handled it reports the
        session-open latch and calls that a pass.
        """
        started = time.monotonic()
        try:
            fresh = await self.open_control()
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError, RuntimeError):
            return {"connected": False, "elapsed_s": round(time.monotonic() - started, 3),
                    "held_command_replayed": True}
        last = next((item for item in reversed(old.sent) if item["kind"] == "motion"), None)
        if last is None:
            await fresh.ws.close()
            return {"connected": True, "elapsed_s": round(time.monotonic() - started, 3),
                    "held_command_replayed": True, "reason": "no held command to inject"}
        attack_seq = last["seq"]
        await fresh.ws.send_json(motion_message(
            fresh.session_id, last["permit"], attack_seq, v=last["v"]))
        reply = await fresh.reply_for(attack_seq, timeout=REPLY_WAIT_S)
        # accept() of an unknown permit stops with expired_permit and does not
        # store the attack seq, so the echo can stay at the session-open -1.
        # A frame that arrived after the send is still that answer.
        # release_required is the session-open latch and is not an answer.
        rejected = (reply is not None and reply.get("reason") == "expired_permit"
                    and pwm_zero(reply) and reply.get("requires_release") is True)
        await fresh.ws.close()
        return {"connected": True, "elapsed_s": round(time.monotonic() - started, 3),
                "pwm_stayed_zero": rejected, "reason": None if reply is None else reply.get("reason"),
                "echoed_seq": None if reply is None else reply.get("seq"),
                "answered_seq": None if reply is None else reply.get("answered_seq", reply.get("seq")),
                "requires_release": None if reply is None else reply.get("requires_release"),
                "held_command_replayed": not rejected}

    async def press_without_release_is_rejected(self, control):
        """One new press, no release first. The reply for that seq must refuse it.

        Waiting 300 ms and then reading PWM 0 is not this check: the 250 ms
        lease would expire a wrongly-accepted press before the poll.
        """
        if not control.permit:
            await control.pump_one(timeout=0.3)
        if not control.permit:
            return {"rejected": False, "reason": "no permit to attack with"}
        sent = await control.drive_once()
        reply = await control.reply_for(sent["seq"], timeout=REPLY_WAIT_S)
        if reply is None:
            return {"rejected": False, "reason": "server never answered the press",
                    "echoed_seq": None}
        # accept() stops with release_required and does not store the seq, so
        # the echo stays at the pre-press seq. A frame that arrived after the
        # send is still that answer. An "active" echo would be the latch gone.
        answered = reply.get("seq") == sent["seq"] or reply.get("echoed_prior_seq") is True
        rejected = (answered and reply.get("reason") == "release_required" and pwm_zero(reply)
                    and reply.get("requires_release") is True)
        return {"rejected": rejected, "reason": reply.get("reason"),
                "echoed_seq": reply.get("seq"), "sent_seq": sent["seq"],
                "after_pwm": [reply.get("left_pwm"), reply.get("right_pwm")]}

    async def view_guard_rejects_stale_frame(self, control):
        """A press carrying video_age_ms just past the guard must come back video_stale.

        The guard number is read from the code. 501 against a guard that had
        moved to 800 would be accepted, and this check would fail.
        """
        guard = read_view_guard_ms()
        release_seq = control.seq + 1
        await control.release()
        released = await control.reply_for(release_seq, timeout=1.0)
        if released is None or released.get("requires_release") is not False:
            return {"rejected": False, "reason": "could not release before the view-guard press",
                    "guard_ms": guard, "release_reply": None if released is None else released.get("reason")}
        # The release echo can carry the previous permit. Wait for one issued
        # after the release, or the press rides a permit accept() already dropped.
        permit_before = control.permit
        until = time.monotonic() + 0.3
        while time.monotonic() < until and control.permit == permit_before:
            await control.pump_one(timeout=0.15)
        if not control.permit or control.permit == permit_before:
            return {"rejected": False, "reason": "no fresh permit for the view-guard press",
                    "guard_ms": guard}
        sent = await control.drive_once(video_age_ms=guard + 1)
        reply = await control.reply_for(sent["seq"], timeout=REPLY_WAIT_S)
        if reply is None:
            return {"rejected": False, "reason": "server never answered the stale frame",
                    "guard_ms": guard, "sent_age_ms": guard + 1}
        # Two layers. The production relay rewrites a deadman press whose
        # video_age_ms is past 500 into a release before it forwards
        # (operator_relay.py), so the Jetson answers deadman_released and the
        # ordered release clears the latch. The Jetson, if it still saw the
        # press, would answer video_stale and hold the latch (state.py). Either
        # is a rejection: PWM stays 0. An "active" answer with nonzero PWM is
        # the guard being gone, and that is the failure this check exists for.
        rejected = reply.get("reason") in ("video_stale", "deadman_released") and pwm_zero(reply)
        return {"rejected": rejected, "reason": reply.get("reason"), "guard_ms": guard,
                "sent_age_ms": guard + 1, "sent_seq": sent["seq"], "echoed_seq": reply.get("seq"),
                "after_pwm": [reply.get("left_pwm"), reply.get("right_pwm")]}

    def snapshot(self, status, *, video_age, video_source, rtt, interruption_s, release_probe):
        return {
            "stop_reason": status.get("reason"),
            "requires_release": status.get("requires_release"),
            "last_output_pwm": {"left": status.get("left_pwm"), "right": status.get("right_pwm")},
            "video_age_ms": video_age, "video_source": video_source,
            "control_rtt": rtt_summary(rtt), "interruption_s": interruption_s,
            "explicit_release": release_probe,
        }


def rtt_summary(samples):
    if samples is None:
        return {"p50_ms": NOT_MEASURABLE, "p95_ms": NOT_MEASURABLE, "samples": 0}
    p50, p95 = percentile(samples, 0.50), percentile(samples, 0.95)
    return {"p50_ms": round(p50, 2) if p50 is not None else None,
            "p95_ms": round(p95, 2) if p95 is not None else None,
            "samples": len(samples)}


def scenario_row(**fields):
    row = {
        "scenario": None, "scope": "simulated", "phase": "connection_time",
        "active_path": None, "interruption_s": None,
        "control_rtt": {"p50_ms": None, "p95_ms": None, "samples": 0},
        "video_age_ms": None, "video_source": None,
        "last_output_pwm": {"left": None, "right": None},
        "stop_reason": None, "requires_release": None,
        "explicit_release": {"required": None, "sent": False,
                             "motion_without_release_rejected": None, "held_command_replayed": False},
        "service": None, "passed": False, "reason": "",
    }
    row.update(fields)
    return row


def mark_unmeasured(row):
    """A dead path has no RTT and no frame. Say so. A bare null looks like a miss."""
    row["control_rtt"] = rtt_summary(None)
    row["video_age_ms"] = NOT_MEASURABLE
    row["video_source"] = NOT_MEASURABLE
    return row


def judge(expect, *, service_up, motors_stopped, release_held, replayed, recovered):
    """Pass/fail for one scenario. `expect` is service | stop | stop_then_recover."""
    if replayed:
        return False, "held command was replayed after a required stop"
    if expect == "service":
        if service_up and not motors_stopped:
            return False, "service stayed up but motors were not left stopped at rest"
        if service_up:
            return True, "single-path failure; the other path kept HTTP and control at connect time"
        return False, "expected the surviving path to keep service at connect time"
    if not motors_stopped or not release_held:
        return False, "expected a required stop with requires_release after the path died"
    if expect == "stop":
        if recovered:
            return False, "expected the loss to stay stopped; a path came back"
        return True, "path loss stopped the motors and held the latch; nothing was replayed"
    if not recovered:
        return False, "motors stopped, but a new session on the surviving path did not come up"
    return True, ("motors stopped for the gap, then a new session came up on the surviving path; "
                  "the held command was not replayed")


async def run_scenarios(harness):
    rows = []
    constants = assert_guards_intact()

    # --- connection time: primary down before the relay exists ---
    # Occupy the dead port so the try is a refused connection, not a skip of
    # the available flag. The gap is how long the refusal itself took, not the
    # probe cap. A pad up to PROBE_TIMEOUT_S would be the harness picking a number.
    dead_a = harness.paths["A"].port
    harness.paths["A"].cut()
    await harness.paths["A"].occupy(harness.group)
    harness.paths["A"].probe_origin = f"http://127.0.0.1:{dead_a}"
    name = await harness.connect_relay(prefer="A", probe=True)
    health_ok, _ = await http_ok(harness.session, harness.relay_origin + "/health")
    age, source = await harness.video_age()
    control = await harness.open_control()
    rtt = await harness.sample_rtt(control)
    status = await harness.direct_status()
    await control.ws.close()
    service_up = name == "B" and health_ok and control.session_id is not None
    tries = harness.last_tries
    tried_a = any(item["name"] == "A" and item["ok"] is False and item["origin"] for item in tries)
    gap = refused_gap(tries)
    passed, reason = judge("service", service_up=service_up, motors_stopped=True,
                           release_held=True, replayed=False, recovered=False)
    if not tried_a or gap is None:
        passed, reason = False, "primary was never tried; connection-time fallback was a skip, not a measurement"
    # The gap is the refusal itself. A number that matches the probe cap was padded, not measured.
    if gap is not None and gap >= PROBE_TIMEOUT_S - 0.05:
        passed, reason = False, (f"connection-time gap {gap}s is the probe cap ({PROBE_TIMEOUT_S}s), "
                                 "not the time the dead path took to refuse")
    if status.get("left_pwm") or status.get("right_pwm"):
        passed, reason = False, "resting connect produced nonzero PWM"
    rows.append(scenario_row(
        scenario="primary_unavailable_before_connect", phase="connection_time",
        active_path=name, service="kept" if service_up else "lost",
        connect_tries=tries, probe_timeout_s=PROBE_TIMEOUT_S,
        **harness.snapshot(status, video_age=age, video_source=source, rtt=rtt, interruption_s=gap,
                           release_probe={"required": False, "sent": True,
                                          "motion_without_release_rejected": None,
                                          "held_command_replayed": False}),
        passed=passed, reason=reason))

    # --- midstream HTTP loss: a camera request is in flight when the path dies ---
    rows.append(await http_loss(harness))

    if harness.h264:
        rows.append(await h264_loss(harness))
    else:
        rows.append(scenario_row(
            scenario="primary_loss_h264", phase="midstream",
            passed=None, service="not_run",
            reason="H.264 scenario skipped: ffmpeg was not found. Not a pass, and not part of the overall result."))

    # --- control socket cut on the primary while a command is held ---
    rows.append(await control_loss(harness))

    # --- stalled primary: heartbeats continue; the frozen return path is what stops it ---
    rows.append(await stall_primary(harness))

    # --- fallback dead at connect time; primary keeps service ---
    rows.append(await fallback_down(harness))

    # --- both paths die under a held command ---
    rows.append(await both_lost(harness))

    # --- primary returns; new session may use it; old stick stays dead ---
    rows.append(await recovery(harness))

    return constants, rows


async def restore_paths(harness, names=PATHS):
    """Bring named proxies back as plain forwarders. A stall is replaced, not reused."""
    for name in names:
        proxy = harness.paths.get(name)
        healthy = proxy is not None and proxy.poll_alive() and not proxy.stalled
        if healthy:
            continue
        if proxy is not None:
            proxy.cut()
        replacement = PathProxy(name, harness.jetson_port)
        await replacement.start(harness.group)
        await wait_http(harness.session, None, replacement.origin + "/health", timeout=8)
        harness.paths[name] = replacement


async def open_h264(session, origin, *, timeout=3):
    """Open /ws/video and leave it open. The caller cuts the path underneath it."""
    ws = await session.ws_connect(origin + "/ws/video", timeout=aiohttp.ClientWSTimeout(ws_close=0.25))
    try:
        first = await asyncio.wait_for(ws.receive_json(), timeout)
        if first.get("type") != "video_init":
            await ws.close()
            return None, {"ready": False, "reason": str(first.get("reason") or first.get("type"))}
        await asyncio.wait_for(ws.receive_bytes(), timeout)
        await ws.send_json({"type": "next", "request_id": 1})
        meta = await asyncio.wait_for(ws.receive_json(), timeout)
        age = meta.get("age_ms") if meta.get("type") == "video_segment" else None
        return ws, {"ready": True, "age_ms": age, "source": meta.get("source"), "open": True}
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError, TypeError, KeyError) as exc:
        with suppress(Exception):
            await ws.close()
        return None, {"ready": False, "reason": type(exc).__name__}


async def cut_path(harness, name):
    await harness.paths[name].reset_clients()
    await harness.paths[name].occupy(harness.group)


async def control_loss(harness):
    """Cut the primary while a control session is holding simulated motion."""
    await restore_paths(harness)
    await harness.connect_relay(prefer="A")
    control = await harness.open_control()
    held = await harness.hold_motion(control, seconds=0.45)
    if not held:
        await control.ws.close()
        return scenario_row(scenario="primary_loss_control", phase="midstream",
                            active_path=harness.active_path, passed=False,
                            reason="simulated motion never applied before the cut")
    cut_at = time.monotonic()
    await cut_path(harness, "A")
    died = await stream_died(control, cut_at)
    stopped = await wait_lease_stop(harness)
    return await finish_midstream(
        harness, "primary_loss_control", control, held=held, cut_at=cut_at, died=died,
        stopped=stopped, prefer_after="B", video_before=None,
        note="connection-time fallback does not apply: the established control stream died and stayed dead")


async def http_loss(harness):
    """Cut the primary while an HTTP camera request is outstanding. Not the same as a control cut."""
    await restore_paths(harness)
    await harness.connect_relay(prefer="A")
    control = await harness.open_control()
    held = await harness.hold_motion(control, seconds=0.35)
    if not held:
        await control.ws.close()
        return scenario_row(scenario="primary_loss_http", phase="midstream",
                            active_path=harness.active_path, passed=False,
                            reason="simulated motion never applied before the HTTP cut")
    # Freeze the request on its way up, so it is in flight when the path dies.
    # A request that finishes first is not a cut during HTTP.
    harness.paths["A"].forwarder.stall_upstream()
    inflight = asyncio.create_task(harness.session.get(harness.relay_origin + "/camera.jpg"))
    await asyncio.sleep(0.15)
    cut_at = time.monotonic()
    await cut_path(harness, "A")
    http_failed = False
    http_status = None
    try:
        response = await asyncio.wait_for(inflight, 2.5)
        http_status = response.status
        await response.release()
        http_failed = response.status != 200
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError, RuntimeError):
        http_failed = True
    died = await stream_died(control, cut_at)
    stopped = await wait_lease_stop(harness)
    row = await finish_midstream(
        harness, "primary_loss_http", control, held=held, cut_at=cut_at, died=died,
        stopped=stopped, prefer_after="B", video_before={"http_in_flight": True, "http_status": http_status},
        note="an HTTP camera request was in flight when the path was cut; the open control stream died with it")
    if not http_failed:
        row["passed"] = False
        row["reason"] = "HTTP request was not in flight when the path was cut (status %s)" % http_status
    else:
        row["http_failed_in_flight"] = True
    return row


async def h264_loss(harness):
    """Cut the primary while /ws/video is open and has already delivered a fragment."""
    await restore_paths(harness)
    await harness.connect_relay(prefer="A")
    control = await harness.open_control()
    held = await harness.hold_motion(control, seconds=0.35)
    if not held:
        await control.ws.close()
        return scenario_row(scenario="primary_loss_h264", phase="midstream",
                            active_path=harness.active_path, passed=False,
                            reason="simulated motion never applied before the H.264 cut")
    video = None
    video_before = {"ready": False}
    for _ in range(20):
        video, video_before = await open_h264(harness.session, harness.relay_origin)
        if video_before.get("ready"):
            break
        await asyncio.sleep(0.25)
    if video is None:
        await control.ws.close()
        return scenario_row(scenario="primary_loss_h264", phase="midstream",
                            active_path=harness.active_path, passed=False,
                            reason="H.264 socket was not open before the cut: " + str(video_before.get("reason")))
    cut_at = time.monotonic()
    await cut_path(harness, "A")
    video_died = False
    try:
        while time.monotonic() - cut_at < 2.5:
            message = await asyncio.wait_for(video.receive(), 0.4)
            if message.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING,
                                aiohttp.WSMsgType.ERROR):
                video_died = True
                break
    except (asyncio.TimeoutError, aiohttp.ClientError, RuntimeError, OSError):
        video_died = True
    with suppress(Exception):
        await video.close()
    died = await stream_died(control, cut_at)
    stopped = await wait_lease_stop(harness)
    row = await finish_midstream(
        harness, "primary_loss_h264", control, held=held, cut_at=cut_at, died=died and video_died,
        stopped=stopped, prefer_after="B", video_before=video_before,
        note="H.264 socket was open when the path died; it cannot be moved to the other upstream")
    if not video_died:
        row["passed"] = False
        row["reason"] = "H.264 socket was already closed before the cut, so this did not test an open video stream"
    row["h264_open_at_cut"] = True
    row["h264_stream_died"] = video_died
    return row


async def stream_died(control, cut_at):
    try:
        while time.monotonic() - cut_at < 2.5:
            message = await asyncio.wait_for(control.ws.receive(), 0.4)
            if message.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.ERROR):
                return True
    except (asyncio.TimeoutError, aiohttp.ClientError, RuntimeError, OSError):
        return True
    return False


async def wait_lease_stop(harness, timeout=1.5):
    """Read-only status until the lease has actually stopped the motors."""
    until = time.monotonic() + timeout
    last = {}
    while time.monotonic() < until:
        try:
            last = await harness.direct_status()
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError):
            last = {}
        if latched(last):
            return last
        await asyncio.sleep(0.05)
    return last


async def finish_midstream(harness, scenario, control, *, held, cut_at, died, stopped,
                           prefer_after, video_before, note):
    """New relay on the surviving path. The held command is not resent."""
    motors_stopped = pwm_zero(stopped)
    release_held = stopped.get("requires_release") is True
    # The cut already closed this socket. The held command is not resent at the
    # new session. A new press with no release is the check.
    replayed = False
    with suppress(Exception):
        await control.ws.close()

    recovered = False
    recovered_path = None
    rtt = []
    age = source = None
    # The gap ends when the new session's first status arrives. The attack
    # check, the RTT samples, and the camera fetch come after that, or the
    # reported outage is mostly the harness measuring itself.
    interruption = None
    release_required = False
    denied = {"rejected": None, "reason": "new session never opened"}
    try:
        recovered_path = await harness.connect_relay(prefer=prefer_after)
        again = await harness.open_control()
        first = again.latest
        interruption = round(time.monotonic() - cut_at, 3)
        release_required = first.get("requires_release") is True and pwm_zero(first)
        # No release was sent. A new press must be refused, and the held
        # command is not resent. That is the explicit-release check.
        denied = await harness.press_without_release_is_rejected(again)
        replayed = denied.get("rejected") is not True
        recovered = bool(recovered_path) and release_required and not replayed
        rtt = await harness.sample_rtt(again, samples=3)
        await again.ws.close()
        age, source = await harness.video_age()
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError, RuntimeError):
        recovered = False
    passed, reason = judge("stop_then_recover", service_up=False, motors_stopped=motors_stopped,
                           release_held=release_held, replayed=replayed, recovered=recovered)
    if not died:
        passed, reason = False, "open stream did not end when its path was cut; midstream move is not allowed"
    if not release_required:
        passed, reason = False, "new session did not open latched; a held command could have resumed"
    if replayed:
        passed, reason = False, "a new press with no release was accepted after the required stop"
    return scenario_row(
        scenario=scenario, phase="midstream",
        active_path=recovered_path or harness.active_path,
        service="restored_after_gap" if recovered else "stopped",
        stream_died=died, motion_was_held=held, h264_before=video_before,
        continuity="none — established stream cannot move upstream",
        gap_ended="first status of the new session",
        **harness.snapshot(
            stopped, video_age=age, video_source=source, rtt=rtt, interruption_s=interruption,
            release_probe={"required": True, "sent": False,
                           "motion_without_release_rejected": denied.get("rejected"),
                           "press_without_release": denied,
                           "new_session_opened_latched": release_required,
                           "held_command_replayed": replayed,
                           "same_session_attack": "not sent: the cut already closed the socket"}),
        passed=passed, reason=reason + ". " + note)


async def stall_primary(harness):
    """Heartbeats keep coming. The frozen return path is what expires the lease.

    The same run with the stall lifted, measured first, keeps moving. A stall
    that only delays bytes by 2 s, or a hold that stops heartbeating, would
    look identical to the lease running out on its own.
    """
    await restore_paths(harness)
    primary = harness.paths["A"]
    await harness.connect_relay(prefer="A")
    control = await harness.open_control()
    # Control: keep heartbeating with the path healthy. Motion must still be there.
    held = await harness.hold_motion(control, seconds=0.4, keep_alive=True)
    still_moving = await harness.direct_status()
    unstalled_kept_moving = bool(still_moving.get("left_pwm") or still_moving.get("right_pwm"))
    stall_at = time.monotonic()
    primary.forwarder.stall()
    primary.stalled = True
    # Keep sending. The operator's bytes still go out. The permits coming back
    # are frozen, so the lease cannot be renewed. That is the stall. Read
    # /control/status on every pass and stop the clock when PWM first hits 0.
    # Waiting out a fixed 0.6 s and then polling would report the loop, not the stop.
    heartbeats = 0
    stopped_at = None
    status = {}
    renew_until = time.monotonic() + 1.0
    while time.monotonic() < renew_until and not control.ws.closed and control.permit:
        await control.drive_once()
        heartbeats += 1
        try:
            status = await harness.direct_status()
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError):
            status = {}
        if pwm_zero(status):
            stopped_at = time.monotonic()
            break
        await asyncio.sleep(0.04)
    if stopped_at is None:
        status = await wait_lease_stop(harness, timeout=0.5)
        if pwm_zero(status):
            stopped_at = time.monotonic()
    elapsed = None if stopped_at is None else round(stopped_at - stall_at, 3)
    motors_stopped = pwm_zero(status)
    # The stall must still be holding. A 2 s delay that then delivers is not a stall.
    still_frozen = primary.forwarder is not None and primary.forwarder.stalled
    stayed_on_a = harness.active_path == "A"
    with suppress(Exception):
        await control.ws.close()
    # The 250 ms lease plus one status interval and one poll. A stop that only
    # shows up after the old 0.6 s loop would fail this, which is the point.
    stopped_inside_lease = elapsed is not None and elapsed < ControlState.LEASE + 0.10
    passed = (held and unstalled_kept_moving and motors_stopped and still_frozen
              and stopped_inside_lease and status.get("requires_release") is True
              and stayed_on_a and heartbeats >= 1)
    if not held:
        reason = "simulated motion never applied before the stall"
    elif heartbeats < 1:
        reason = "no heartbeat went out during the stall, so this is the lease running out on its own"
    elif not unstalled_kept_moving:
        reason = "the unstalled control run was already stopped, so the stall was not what stopped it"
    elif not still_frozen:
        reason = "stall released the frozen bytes instead of holding them"
    elif not motors_stopped:
        reason = "stalled primary kept the motors moving"
    elif not stopped_inside_lease:
        reason = (f"motors did stop, but not inside the 250 ms lease "
                  f"(first PWM 0 at {elapsed}s after the stall)")
    else:
        reason = ("unstalled heartbeats kept moving; stalled return path froze the next permit "
                  f"and PWM hit 0 at {elapsed}s, inside the 250 ms lease; stream was not moved to B")
    return mark_unmeasured(scenario_row(
        scenario="stalled_primary", phase="midstream", active_path=harness.active_path,
        service="stopped", continuity="none — a stall is not a failover",
        stall={"heartbeats_during_stall": heartbeats, "unstalled_kept_moving": unstalled_kept_moving,
               "still_frozen": still_frozen, "pwm_zero_at_s": elapsed,
               "stopped_inside_lease": stopped_inside_lease},
        **harness.snapshot(
            status, video_age=NOT_MEASURABLE, video_source=NOT_MEASURABLE, rtt=None,
            interruption_s=elapsed,
            release_probe={"required": True, "sent": False,
                           "motion_without_release_rejected": None,
                           "held_command_replayed": False}),
        passed=passed, reason=reason))


async def fallback_down(harness):
    await restore_paths(harness, ("A",))
    dead_b = harness.paths["B"].port
    if harness.paths["B"].available:
        harness.paths["B"].cut()
    await harness.paths["B"].occupy(harness.group)
    harness.paths["B"].probe_origin = f"http://127.0.0.1:{dead_b}"
    name = await harness.connect_relay(prefer="A", probe=True)
    health_ok, _ = await http_ok(harness.session, harness.relay_origin + "/health")
    age, source = await harness.video_age()
    control = await harness.open_control()
    rtt = await harness.sample_rtt(control)
    status = await harness.direct_status()
    await control.ws.close()
    service_up = name == "A" and health_ok
    tries = harness.last_tries
    tried_b = any(item["name"] == "B" and item["ok"] is False and item["origin"] for item in tries)
    # Preferred path answered, so the refused gap before it is zero. The
    # fallback try is still in connect_tries, with its own elapsed_s.
    passed, reason = judge("service", service_up=service_up, motors_stopped=True,
                           release_held=True, replayed=False, recovered=False)
    if not tried_b:
        passed, reason = False, "fallback was never tried; it was skipped on the available flag"
    return scenario_row(
        scenario="fallback_unavailable", phase="connection_time", active_path=name,
        service="kept" if service_up else "lost", connect_tries=tries,
        probe_timeout_s=PROBE_TIMEOUT_S,
        **harness.snapshot(status, video_age=age, video_source=source, rtt=rtt,
                           interruption_s=refused_gap(tries),
                           release_probe={"required": False, "sent": True,
                                          "motion_without_release_rejected": None,
                                          "held_command_replayed": False}),
        passed=passed, reason=reason + " (fallback was tried and refused; primary answered)")


async def both_lost(harness):
    await restore_paths(harness)
    await harness.connect_relay(prefer="A")
    control = await harness.open_control()
    held = await harness.hold_motion(control, seconds=0.4)
    await harness.paths["A"].reset_clients()
    await harness.paths["B"].reset_clients()
    await harness.paths["A"].occupy(harness.group)
    await harness.paths["B"].occupy(harness.group)
    with suppress(Exception):
        await asyncio.wait_for(control.ws.receive(), 1.5)
    status = await wait_lease_stop(harness, timeout=1.5)
    with suppress(Exception):
        await control.ws.close()
    # Both ports still refuse. The picker tries them; neither answers. The
    # outage has not ended, so the gap is ongoing, not the time we gave up.
    harness.paths["A"].probe_origin = f"http://127.0.0.1:{harness.paths['A'].port}"
    harness.paths["B"].probe_origin = f"http://127.0.0.1:{harness.paths['B'].port}"
    # Short tries. The outage does not end, so waiting the full probe on each
    # path would just be the harness padding a gap it then reports as ongoing.
    picked, _origin, tries = await harness.pick_probed("A", wait_s=0.05)
    motors_stopped = pwm_zero(status)
    release_held = status.get("requires_release") is True
    passed, reason = judge("stop", service_up=False, motors_stopped=motors_stopped and held,
                           release_held=release_held, replayed=False, recovered=False)
    if picked is not None or refused_gap(tries) is not None:
        passed, reason = False, f"both paths were cut but a path still answered ({picked})"
    if not tries or any(item["ok"] for item in tries):
        passed, reason = False, "both paths were not actually tried"
    return mark_unmeasured(scenario_row(
        scenario="both_paths_lost", phase="midstream", active_path=None,
        service="stopped", continuity="none",
        connect_tries=tries, gap_ended="ongoing — neither path came back",
        **harness.snapshot(
            status, video_age=NOT_MEASURABLE, video_source=NOT_MEASURABLE, rtt=None,
            interruption_s=None,
            release_probe={"required": True, "sent": False,
                           "motion_without_release_rejected": None,
                           "held_command_replayed": False}),
        passed=passed, reason=reason + " Outage is ongoing; interruption_s is null."))


async def recovery(harness):
    """Primary comes back. A new session may use it. The held stick stays dead.

    The press-without-release is judged on the reply for that seq, inside the
    lease. A missing permit is a failed check, not a crash. The held command
    is not resent. The gap ends at the recovered session's first status.
    """
    await restore_paths(harness, ("A",))
    if harness.paths["B"].available:
        harness.paths["B"].cut()
    await harness.connect_relay(prefer="A")
    control = await harness.open_control()
    held = await harness.hold_motion(control, seconds=0.4)
    cut_at = time.monotonic()
    await cut_path(harness, "A")
    stopped = await wait_lease_stop(harness, timeout=1.5)
    with suppress(Exception):
        await control.ws.close()
    # Restore primary. Do not point the dead socket at it.
    harness.paths["A"] = PathProxy("A", harness.jetson_port)
    await harness.paths["A"].start(harness.group)
    await wait_http(harness.session, None, harness.paths["A"].origin + "/health", timeout=8)
    name = await harness.connect_relay(prefer="A")
    again = await harness.open_control()
    first = again.latest
    interruption = round(time.monotonic() - cut_at, 3)
    before = await harness.direct_status()
    denied_probe = await harness.press_without_release_is_rejected(again)
    motion_rejected = denied_probe.get("rejected") is True
    # The session opened latched. An ordered release is what clears it.
    resumed_ok = False
    resumed = {}
    view_probe = {"rejected": False, "reason": "view-guard press was not reached"}
    rtt = []
    try:
        await again.release()
        released = await again.reply_for(again.seq, timeout=1.0)
        if released is None or released.get("requires_release") is not False:
            raise RuntimeError("release was not acknowledged")
        await again.pump_one(timeout=0.3)
        if not again.permit:
            raise RuntimeError("No permit arrived after the release")
        # One press one millisecond past the view guard, read from the code.
        # This is the check the report claims. A unit test of the same helper
        # is not the qualification run.
        view_probe = await harness.view_guard_rejects_stale_frame(again)
        if view_probe.get("rejected") is not True:
            raise RuntimeError(f"view guard did not reject a stale frame: {view_probe}")
        sent_at = time.monotonic()
        await again.drive_once()
        until = time.monotonic() + 0.5
        while time.monotonic() < until:
            message = await again.pump_one(timeout=0.3)
            if message.get("seq") == again.seq and (message.get("left_pwm") or message.get("right_pwm")):
                resumed = message
                rtt = [(time.monotonic() - sent_at) * 1000]
                break
            if message.get("reason") == "expired_permit" and message.get("permit"):
                again.permit = message["permit"]
                sent_at = time.monotonic()
                await again.drive_once()
        # One drive echo is one sample. p50 and p95 of one number are the same
        # number. Take two more stop-only heartbeats so the percentiles are a
        # distribution, and label the count either way.
        if rtt:
            rtt.extend(await harness.sample_rtt(again, samples=2))
        resumed_ok = (resumed.get("left_pwm") or 0) != 0 or (resumed.get("right_pwm") or 0) != 0
    except (RuntimeError, aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
        resumed = {"reason": str(exc)}
    await again.ws.send_json(release_message(again.session_id, again.seq + 1))
    await asyncio.sleep(0.15)
    age, source = await harness.video_age()
    await again.ws.close()
    # The held command was not resent. The latch at session open, and the
    # press that was refused, are the proof it did not resume.
    opened_latched = first.get("requires_release") is True and pwm_zero(first)
    replayed = not motion_rejected
    motors_stopped = pwm_zero(stopped)
    recovered = name == "A" and opened_latched and resumed_ok and motion_rejected
    passed, reason = judge("stop_then_recover", service_up=False,
                           motors_stopped=motors_stopped and held,
                           release_held=stopped.get("requires_release") is True,
                           replayed=replayed, recovered=recovered)
    if before.get("requires_release") is not True:
        passed, reason = False, "recovered session did not start latched"
    if not opened_latched:
        passed, reason = False, "recovered session's first status was not latched at PWM 0"
    if not motion_rejected:
        passed, reason = False, ("press without a release was not answered as release_required "
                                 f"(got {denied_probe.get('reason')} seq={denied_probe.get('echoed_seq')})")
    if view_probe.get("rejected") is not True:
        passed, reason = False, ("stale-video press was not rejected "
                                 f"(got {view_probe.get('reason')})")
    if not resumed_ok:
        reason += f" (after release, PWM stayed {resumed.get('left_pwm')}/{resumed.get('right_pwm')} reason={resumed.get('reason')})"
    return scenario_row(
        scenario="recovery", phase="midstream", active_path=name,
        service="restored_after_gap" if recovered else "stopped",
        continuity="new session only — the cut stream was not resumed",
        gap_ended="first status of the recovered session",
        **harness.snapshot(
            stopped, video_age=age, video_source=source, rtt=rtt,
            interruption_s=interruption,
            release_probe={"required": True, "sent": True,
                           "motion_without_release_rejected": motion_rejected,
                           "press_without_release": denied_probe,
                           "new_session_opened_latched": opened_latched,
                           "held_command_replayed": replayed,
                           "resumed_only_after_release": resumed_ok,
                           "view_guard": view_probe}),
        passed=passed, reason=reason)


def overall_passed(rows):
    """Skipped rows (passed is None) are not a pass and not part of the result."""
    graded = [row for row in rows if row.get("passed") is not None]
    return bool(graded) and all(row["passed"] for row in graded)


def summarize(rows):
    kept, stopped, failed, skipped = [], [], [], []
    for row in rows:
        if row.get("passed") is None:
            skipped.append(row["scenario"])
        elif not row["passed"]:
            failed.append(row["scenario"])
        elif row["service"] == "kept":
            kept.append(row["scenario"])
        elif row["service"] in ("stopped", "restored_after_gap"):
            stopped.append(f"{row['scenario']} ({row['service']})")
    return {"single_path_keeps_service": kept,
            "correctly_stops": stopped,
            "failed": failed,
            "skipped": skipped,
            "connection_time_vs_midstream": (
                "Connection-time fallback can pick the other path before the relay starts. "
                "An established TCP/WebSocket cannot move. Midstream rows report the stopped "
                "interval instead of calling that continuity. A connection-time gap is the "
                "measured refusal time, not the probe cap (probe_timeout_s).")}


def _cell(value):
    return "—" if value is None else value


def _gap_cell(value):
    if value is None:
        return "ongoing"
    if value == NOT_MEASURABLE:
        return NOT_MEASURABLE
    return value


def _result_cell(passed):
    if passed is None:
        return "skipped"
    return "pass" if passed else "FAIL"


def markdown_report(report):
    guards = report["guards"]
    lines = [
        "# Failover qualification",
        "",
        f"Scope: **{report['scope']}**. This is not hardware qualification.",
        "",
        f"Permit lease: {guards['permit_lease_ms']} ms (ControlState.LEASE). "
        f"View guard: {guards['view_guard_ms']} ms, read from the code "
        f"({guards.get('view_guard_source', 'jetson/control/state.py')}). Both unchanged.",
        "",
        report["summary"]["connection_time_vs_midstream"],
        "",
        "Single-path failures that keep service at connect time: "
        + (", ".join(report["summary"]["single_path_keeps_service"]) or "none"),
        "",
        "Failures that correctly stop: " + (", ".join(report["summary"]["correctly_stops"]) or "none"),
        "",
        "| Scenario | Phase | Path | Gap (s) | RTT p50/p95 (samples) | Video age | PWM | Stop reason | Release held | Result |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for row in report["scenarios"]:
        rtt = row["control_rtt"]
        pwm = row["last_output_pwm"]
        release = row["explicit_release"]
        lines.append(
            f"| {row['scenario']} | {row['phase']} | {row['active_path'] or '—'} | "
            f"{_gap_cell(row['interruption_s'])} | "
            f"{rtt['p50_ms']}/{rtt['p95_ms']} (n={rtt.get('samples', 0)}) | {_cell(row['video_age_ms'])} | "
            f"{pwm['left']}/{pwm['right']} | {row['stop_reason'] or '—'} | "
            f"{release['required']} | {_result_cell(row['passed'])} |")
    if report["summary"]["failed"]:
        lines += ["", "Failed: " + ", ".join(report["summary"]["failed"])]
    live = report.get("live")
    if live:
        if live.get("error"):
            lines += ["", f"Live read-only probe of {live.get('origin')} failed and did not stop the simulated run: "
                      f"{live['error']}. No /ws/control was opened. No motion was requested."]
        else:
            lines += ["", f"Live read-only probe of {live.get('origin')}: "
                      f"health={'ok' if isinstance(live.get('health'), dict) and 'http_status' not in live['health'] else 'failed'}. "
                      "No /ws/control was opened. No motion was requested."]
    else:
        lines += ["", "Live mode was not claimed. No /ws/control was opened against a live origin."]
    return "\n".join(lines) + "\n"


async def live_probe(url):
    """Read-only. GET /health and GET /control/status. Never opens a socket to /ws/control.

    A failure stays inside the live block. A self-signed Jetson cert, a timeout,
    or a down origin must not abort the simulated scenarios that follow.
    """
    origin = upstream_origin(url)
    health = status = None
    error = None
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as session:
            async with session.get(str(origin) + "/health") as response:
                health = await response.json(content_type=None) if response.status == 200 else {"http_status": response.status}
            async with session.get(str(origin) + "/control/status") as response:
                status = await response.json(content_type=None) if response.status == 200 else {"http_status": response.status}
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError) as exc:
        # ClientConnectorCertificateError is a ClientError, so a self-signed
        # https:// Jetson lands here instead of killing the run.
        error = f"{type(exc).__name__}: {exc}"
        health = health if isinstance(health, dict) else {"error": error}
        status = status if isinstance(status, dict) else {"error": error}
    if isinstance(status, dict):
        # Never copy a session secret into the report, simulated or live.
        status.pop("permit", None)
        status.pop("session_id", None)
        if status.get("simulated") is not True:
            # A live rover's PWM is not ours to record. Software evidence only.
            status = {key: status.get(key) for key in ("rover", "reason", "requires_release", "simulated")}
            status["left_pwm"] = status["right_pwm"] = None
    report = {"scope": "live_read_only", "origin": str(origin), "motion_requested": False,
              "control_socket_opened": False, "health": health, "control_status": status}
    if error is not None:
        report["reachable"] = False
        report["error"] = error
    return report


def write_report(report, output, markdown):
    text = json.dumps(report, indent=2)
    if output:
        path = Path(output)
        path.write_text(text + "\n")
    if markdown:
        Path(markdown).write_text(markdown_report(report))
    return text


async def qualify(args):
    constants = assert_guards_intact()
    h264 = shutil.which("ffmpeg") is not None and not args.no_h264
    report = {"scope": "simulated", "motion_requested_live": False, "hardware_qualified": False,
              "guards": constants, "probe_timeout_s": PROBE_TIMEOUT_S,
              "probe_timeout_note": ("Cap on a path that never answers. A refusal is reported "
                                     "at the time it came back, not padded out to this cap."),
              "choose_upstream": "tools.qualify_failover.choose_upstream",
              "choose_upstream_probe_all": ("The report probes every path, including ones after "
                                            "the first answer. A phone bridge binding to "
                                            "choose_upstream must pass probe_all=False or every "
                                            "connect also waits on the fallback probe."),
              "topology": ("operator_relay -> one of two harness forwarders "
                           "(cut/stall/restore; fault_proxy has no freeze-stall or restore) "
                           "-> jetson.server --simulate-rover"),
              "replay_policy": ("The qualification run never resends a held command. After a "
                                "required stop it opens a new session, asserts the latch, and sends one "
                                "new press with no release. That press must be rejected. Motion resumes "
                                "only after an explicit release, in the recovery scenario."),
              "scenarios": [], "summary": {}, "live": None}
    if args.live_origin:
        report["live"] = await live_probe(args.live_origin)
    harness = Harness(h264=h264)
    try:
        async with harness:
            constants, rows = await run_scenarios(harness)
            report["guards"] = constants
            report["scenarios"] = rows
            report["h264"] = harness.h264
    finally:
        harness.group.close()
    report["summary"] = summarize(report["scenarios"])
    report["passed"] = overall_passed(report["scenarios"]) and not report["summary"]["failed"]
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Write the JSON report here")
    parser.add_argument("--markdown", type=Path, help="Write the plain summary here")
    parser.add_argument("--no-h264", action="store_true", help="Skip the H.264 cut even when ffmpeg is installed")
    parser.add_argument("--live-origin", help="Optional read-only GET /health and /control/status. Never opens /ws/control.")
    args = parser.parse_args(argv)
    install_signal_handlers()
    if args.live_origin:
        try:
            upstream_origin(args.live_origin)
        except ValueError as exc:
            parser.error(str(exc))
    try:
        report = asyncio.run(qualify(args))
    except (RuntimeError, OSError, ValueError) as exc:
        ProcessGroup.close_all()
        parser.exit(1, str(exc) + "\n")
    text = write_report(report, args.output, args.markdown)
    print(text)
    return 0 if report["passed"] else 2


if __name__ == "__main__":
    sys.exit(main())
