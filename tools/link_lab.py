"""Repeatable cellular link lab for the real operator control + H.264 path.

python -m tools.link_lab --profiles direct derp_typical derp_bad booth --seconds 60 --report /tmp/link-lab.json
python -m tools.link_lab --calibrate --profiles derp_typical      # stop-only RTT vs the field numbers
python -m tools.link_lab --sweep --seconds 30                     # where held drive starts to stutter
python -m tools.link_lab --upstream http://192.168.55.1:8443 --calibrate --profiles derp_typical

Topology, each piece in its own process like the real rig:

    lab clients (Quest-equivalent control + H.264 window 2)
      -> tools.operator_relay (production relay)
      -> emulated cellular leg (this file, localhost user-space TCP proxy)
      -> jetson.mapping.server (local fixture: real FFmpeg H.264, simulated motors)
         or a real server given by --upstream (stop-only probes, never motion)

The emulated leg is one shared bottleneck per direction, like a Tailscale
tunnel: every TCP connection shares its serializer and delay process. Each
packet gets base delay + exponential jitter + any active stall; a lost packet
is delivered after a retransmission timeout, and TCP order is kept, so later
bytes on that connection wait behind it (head-of-line). It never touches OS
networking. Results are software evidence only, not a hardware result.
"""

import argparse
import asyncio
from contextlib import suppress
from dataclasses import asdict, dataclass, replace
import json
import math
import os
from pathlib import Path
import platform
import random
import signal
import socket
import statistics
import subprocess
import sys
import tempfile
import time

import aiohttp

ROOT = Path(__file__).resolve().parents[1]
MTU = 1400
CHUNK = 65536
MAX_CONNECTIONS = 32


@dataclass(frozen=True)
class Profile:
    name: str
    one_way_ms: float            # base delay each direction
    jitter_ms: float = 0         # mean exponential extra delay per packet, each direction
    stall_hz: float = 0          # Poisson stall onsets per second, each direction
    stall_ms: tuple = (0, 0)     # stall length range; packets sent inside wait for its end
    loss: float = 0              # per-packet loss, recovered after rto_ms (TCP order kept)
    rto_ms: float = 250
    down_mbps: float = 0         # rover -> operator (video, permits); 0 = unlimited
    up_mbps: float = 0           # operator -> rover (commands, video requests)
    queue_ms: float = 400        # bottleneck buffer before senders are back-pressured
    note: str = ""


# Calibrated with --calibrate so the stop-only probe (tools.qualify_link method:
# 10 Hz deadman=false, ack = first status echoing the seq) reproduces the field
# numbers. That probe includes the 0-50 ms wait for the next 20 Hz status.
PROFILES = {
    "none": Profile("none", 0, note="Pass-through: measure a real path (--upstream) without emulation"),
    "direct": Profile("direct", 22, jitter_ms=3, down_mbps=8, up_mbps=4,
                      note="Tailscale direct UDP, cellular both ends: ~50 ms +/-10 RTT"),
    "derp_typical": Profile("derp_typical", 26, jitter_ms=20, stall_hz=0.5, stall_ms=(80, 200),
                            down_mbps=6, up_mbps=3,
                            note="Field 2026-10-10 over DERP Miami: probe p50 118 / p95 217 ms"),
    "derp_bad": Profile("derp_bad", 32, jitter_ms=26, stall_hz=0.4, stall_ms=(200, 380),
                        loss=0.02, rto_ms=250, down_mbps=4, up_mbps=2,
                        note="DERP with 350-500 ms spikes and 2% loss"),
    "booth": Profile("booth", 40, jitter_ms=30, stall_hz=0.2, stall_ms=(150, 300),
                     loss=0.01, rto_ms=300, down_mbps=0.9, up_mbps=0.5, queue_ms=600,
                     note="Crowded booth cell: <1 Mbps rover uplink, bufferbloat, 1% loss"),
}


def percentile(values, fraction):
    values = sorted(values)
    return values[max(0, math.ceil(len(values) * fraction) - 1)] if values else None


def summary(values, digits=1):
    if not values:
        return dict(n=0, p50=None, p95=None, max=None)
    return dict(n=len(values), p50=round(percentile(values, .5), digits),
                p95=round(percentile(values, .95), digits), max=round(max(values), digits))


class LinkPath:
    """One direction of the shared bottleneck; all connections go through it."""

    def __init__(self, profile, mbps, rng):
        self.profile, self.rng = profile, rng
        self.rate = mbps * 1e6 / 8 if mbps else 0
        self.free_at = 0.0
        self.stall_until = -math.inf
        self.next_stall = None
        self.stats = dict(packets=0, bytes=0, lost=0, stalls=0, backlog_peak_ms=0.0)

    def stall_wait(self, t):
        if not self.profile.stall_hz:
            return 0.0
        if self.next_stall is None:
            self.next_stall = t + self.rng.expovariate(self.profile.stall_hz)
        while self.next_stall <= t:
            length = self.rng.uniform(*self.profile.stall_ms) / 1000
            self.stall_until = max(self.stall_until, self.next_stall + length)
            self.next_stall += self.rng.expovariate(self.profile.stall_hz)
            self.stats["stalls"] += 1
        return max(0.0, self.stall_until - t)

    def schedule(self, size, now):
        depart = now
        if self.rate:
            depart = max(now, self.free_at) + size / self.rate
            self.free_at = depart
            self.stats["backlog_peak_ms"] = max(self.stats["backlog_peak_ms"], (depart - now) * 1000)
        delay = self.profile.one_way_ms / 1000 + self.stall_wait(depart)
        if self.profile.jitter_ms:
            delay += self.rng.expovariate(1000 / self.profile.jitter_ms)
        if self.profile.loss and self.rng.random() < self.profile.loss:
            delay += self.profile.rto_ms / 1000
            self.stats["lost"] += 1
        self.stats["packets"] += 1
        self.stats["bytes"] += size
        return depart + delay

    def backlog(self, now):
        return max(0.0, self.free_at - now)


class LinkProxy:
    """Localhost TCP proxy that applies a Profile; never changes system networking."""

    def __init__(self, profile, upstream_host, upstream_port, *, port=0, seed=1010):
        self.profile = profile
        self.upstream = (upstream_host, upstream_port)
        self.port = port
        rng = random.Random(seed)
        self.down = LinkPath(profile, profile.down_mbps, random.Random(rng.random()))
        self.up = LinkPath(profile, profile.up_mbps, random.Random(rng.random()))
        self.server = None
        self.tasks = set()
        self.connections = 0

    async def start(self):
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", self.port, backlog=MAX_CONNECTIONS)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def pipe(self, reader, writer, path):
        loop = asyncio.get_running_loop()
        queue = asyncio.Queue()
        last = 0.0

        async def read():
            nonlocal last
            while True:
                data = await reader.read(CHUNK)
                now = loop.time()
                if not data:
                    queue.put_nowait((last, b""))
                    return
                for offset in range(0, len(data), MTU):
                    packet = data[offset:offset + MTU]
                    # TCP delivers in order: a late or retransmitted packet holds back the rest.
                    last = max(last, path.schedule(len(packet), now))
                    queue.put_nowait((last, packet))
                backlog = path.backlog(loop.time()) - self.profile.queue_ms / 1000
                if backlog > 0:
                    await asyncio.sleep(backlog)  # full bottleneck buffer: sender waits

        async def write():
            while True:
                ready, packet = await queue.get()
                wait = ready - loop.time()
                if wait > 0:
                    await asyncio.sleep(wait)
                if not packet:
                    if writer.can_write_eof():
                        writer.write_eof()
                    return
                parts = [packet]
                while not queue.empty() and queue._queue[0][0] <= loop.time() and queue._queue[0][1]:
                    parts.append(queue.get_nowait()[1])
                writer.write(b"".join(parts))
                await writer.drain()

        tasks = [asyncio.create_task(read()), asyncio.create_task(write())]
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def handle(self, reader, writer):
        if len(self.tasks) >= MAX_CONNECTIONS:
            writer.transport.abort()
            return
        task = asyncio.current_task()
        self.tasks.add(task)
        self.connections += 1
        upstream = None
        try:
            remote_reader, upstream = await asyncio.wait_for(asyncio.open_connection(*self.upstream), 3)
            for connection in (writer, upstream):
                # macOS accepted sockets skip asyncio's automatic TCP_NODELAY.
                connection.get_extra_info("socket").setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            await asyncio.gather(self.pipe(reader, upstream, self.up), self.pipe(remote_reader, writer, self.down))
        except (OSError, asyncio.TimeoutError):
            pass
        finally:
            for connection in (writer, upstream):
                if connection is not None:
                    connection.transport.abort()
            self.tasks.discard(task)

    def stats(self):
        return dict(profile=self.profile.name, connections=self.connections,
                    down=self.down.stats, up=self.up.stats)

    async def close(self):
        if self.server:
            self.server.close()
        for task in tuple(self.tasks):
            task.cancel()
        await asyncio.gather(*tuple(self.tasks), return_exceptions=True)


def resolve_profile(name, overrides=None):
    if name not in PROFILES:
        raise ValueError(f"Unknown profile {name}; choose from {', '.join(PROFILES)}")
    return replace(PROFILES[name], **(overrides or {}))


async def serve_proxy(args):
    profile = resolve_profile(args.serve_proxy, json.loads(args.overrides or "{}"))
    proxy = await LinkProxy(profile, args.upstream_host, args.upstream_port, port=args.port,
                            seed=args.seed).start()
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stopped.set)
    print(f"LINK LAB EMULATION {profile.name}: localhost:{proxy.port} -> "
          f"{args.upstream_host}:{args.upstream_port}", flush=True)
    await stopped.wait()
    await proxy.close()
    if args.stats:
        Path(args.stats).write_text(json.dumps(proxy.stats()))


def serve_fixture(args):
    """Production server with synthetic RGB and simulated motors (no hardware)."""
    import uvicorn
    from tools.tests.test_demo_qualification import fixture_app
    accept = socket.socket.accept

    def accept_nodelay(sock):
        # Linux (the Jetson) gets TCP_NODELAY on uvicorn's accepted sockets; macOS
        # does not, and Nagle plus delayed ACK would add lab-only status delay.
        connection, address = accept(sock)
        with suppress(OSError):
            connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        return connection, address
    socket.socket.accept = accept_nodelay
    from jetson.control.service import ControlService
    options = json.loads(args.server_options or "{}")
    if args.permit_hz:
        ControlService.STATUS_INTERVAL = 1 / args.permit_hz
    uvicorn.run(fixture_app("rgb15", server_options=options), host="127.0.0.1", port=args.port,
                access_log=False, log_level="warning", timeout_graceful_shutdown=2)


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Lab:
    """Fixture (or real upstream) + emulated leg + production relay, one process each."""

    def __init__(self, profile, *, upstream=None, overrides=None, server_options=None, seed=1010, permit_hz=20):
        self.profile, self.upstream, self.permit_hz = profile, upstream, permit_hz
        self.overrides, self.server_options, self.seed = overrides or {}, server_options or {}, seed
        self.processes = []
        self.temp = tempfile.TemporaryDirectory()
        self.client = None
        self.direct = self.url = None

    def spawn(self, *command):
        log = open(Path(self.temp.name) / f"{len(self.processes)}.log", "w+")
        process = subprocess.Popen([sys.executable, *command], cwd=ROOT, stdout=log, stderr=log,
                                   start_new_session=True)
        process.log = log
        self.processes.append(process)
        return process

    async def wait_ready(self, process, url, check=lambda body: True, timeout=30):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            if process.poll() is not None:
                process.log.seek(0)
                raise RuntimeError(f"{url} process exited: " + process.log.read()[-2000:])
            with suppress(aiohttp.ClientError, ValueError, asyncio.TimeoutError):
                async with self.client.get(url) as response:
                    if response.status == 200 and check(await response.json()):
                        return
            await asyncio.sleep(.1)
        raise RuntimeError(f"Not ready: {url}")

    async def __aenter__(self):
        self.client = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5))
        try:
            if self.upstream:
                from yarl import URL
                origin = URL(self.upstream)
                host, port = origin.host, origin.port
                self.direct = None
            else:
                host, port = "127.0.0.1", free_port()
                self.direct = f"http://{host}:{port}"
                fixture = self.spawn("-m", "tools.link_lab", "--serve-fixture", "--port", str(port),
                                     "--server-options", json.dumps(self.server_options),
                                     "--permit-hz", str(self.permit_hz))
                def ready(health):
                    if not health["control"]["simulated"]:
                        raise RuntimeError("Link lab motion must never use physical motors")
                    return health["camera"] and health["video"]["ready"]
                await self.wait_ready(fixture, self.direct + "/health", ready)
            proxy_port, relay_port = free_port(), free_port()
            self.stats_file = Path(self.temp.name) / "proxy.json"
            proxy = self.spawn("-m", "tools.link_lab", "--serve-proxy", self.profile, "--port", str(proxy_port),
                               "--upstream-host", host, "--upstream-port", str(port), "--seed", str(self.seed),
                               "--overrides", json.dumps(self.overrides), "--stats", str(self.stats_file))
            scheme = "http"
            relay = self.spawn("-m", "tools.operator_relay", "--upstream", f"{scheme}://127.0.0.1:{proxy_port}",
                               "--port", str(relay_port))
            self.url = f"http://127.0.0.1:{relay_port}"
            await self.wait_ready(relay, self.url + "/relay/health")
            if proxy.poll() is not None:
                raise RuntimeError("Link emulator exited")
            return self
        except BaseException:
            await self.__aexit__(None, None, None)
            raise

    async def status(self):
        async with self.client.get(self.direct + "/control/status") as response:
            return await response.json()

    async def fixture_status(self):
        async with self.client.get(self.direct + "/__qualification/status") as response:
            return await response.json()

    async def __aexit__(self, *args):
        if self.client:
            await self.client.close()
        for process in reversed(self.processes):
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
        for process in self.processes:
            try:
                await asyncio.to_thread(process.wait, 5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                await asyncio.to_thread(process.wait)
            process.log.close()
        self.proxy_stats = None
        with suppress(OSError, ValueError, AttributeError):
            self.proxy_stats = json.loads(self.stats_file.read_text())
        self.temp.cleanup()


async def probe(lab, seconds):
    """The stop-only field probe (tools.qualify_link) through the emulated leg."""
    from tools.operator_relay import upstream_origin
    from tools.qualify_link import measure
    report = await measure(upstream_origin(lab.url), seconds=seconds, video=True, max_rtt_ms=10000)
    return dict(probe_rtt_p50_ms=report["control_rtt_median_ms"], probe_rtt_p95_ms=report["control_rtt_p95_ms"],
                probe_rtt_max_ms=report["control_rtt_max_ms"], probe_samples=report["control_samples"],
                video_mode=report["video_mode"], video_mbps=report["observed_video_mbps"],
                errors=report["errors"])


POLICIES = {
    # rover.ts before this change: 20 Hz timer, latest permit.
    "periodic20": dict(command_hz=20, send_on_status=False),
    # rover.ts after: same timer plus one command as soon as a new permit arrives.
    "on_permit": dict(command_hz=20, send_on_status=True),
}


def quest_video(url):
    """VideoClient with h264.ts recovery: a request pending 1 s, or a closed
    socket, resets the stream; it reconnects 1 s later. Never-reconnecting
    clients would turn one bad second into a permanently stale view."""
    from tools.qualify_link import capture_receipt_bound
    from tools.tests.test_demo_qualification import VideoClient

    class QuestVideo(VideoClient):
        resets = 0

        async def consume(self):
            while not self.stopping:
                try:
                    await self.session()
                except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, TypeError) as exc:
                    if not self.stopping:
                        self.errors.append(type(exc).__name__)
                        del self.errors[:-20]
                self.connected = False
                if not self.stopping:
                    self.resets += 1
                    await asyncio.sleep(1)

        async def session(self):
            async with self.client.ws_connect(self.url + "/ws/video",
                                              timeout=aiohttp.ClientWSTimeout(ws_close=.25)) as ws:
                self.ws = ws
                if (await asyncio.wait_for(ws.receive_json(), 3)).get("type") != "video_init":
                    raise ValueError("Video initialization missing")
                self.bytes += len(await asyncio.wait_for(ws.receive_bytes(), 3))
                requested, request_id = {}, 0
                while not self.stopping:
                    while len(requested) < self.window:
                        request_id += 1
                        requested[request_id] = time.monotonic()
                        await ws.send_json(dict(type="next", request_id=request_id))
                    timeout = 1 - (time.monotonic() - min(requested.values()))
                    metadata = await asyncio.wait_for(ws.receive_json(), max(.01, timeout))
                    data = await asyncio.wait_for(ws.receive_bytes(), 1)
                    received = time.monotonic()
                    identifier = metadata.get("request_id")
                    if identifier not in requested:
                        raise ValueError("Unsolicited fragment")
                    elapsed = (received - requested.pop(identifier)) * 1000
                    self.age = capture_receipt_bound(metadata.get("age_ms"), elapsed, metadata.get("server_wait_ms"))
                    self.at, self.connected = received, True
                    self.bounds.append(self.age)
                    self.sample_counts.append(len(metadata.get("samples", [])))
                    self.bytes += len(data)

    return QuestVideo(url, window=2)


def quest_controller(lab, video, policy):
    """The test fixture controller plus rover.ts's own permit gate (control-safety.js).

    Quest refuses to drive on a permit it received >= 200 ms ago and latches a
    release, exactly like a server lease stop, so the lab counts both.
    """
    from tools.tests.test_demo_qualification import Controller

    class QuestController(Controller):
        async def send_command(self, trigger):
            if self.held and self.armed and self.status_at is not None:
                waited = (time.monotonic() - self.status_at) * 1000
                if waited >= 200:
                    self.invalidations.append(dict(kind="permit_wait", waited_ms=round(waited)))
                    self.armed = False
            await super().send_command(trigger)

    return QuestController(lab, video, **POLICIES[policy])


async def drive(lab, seconds, policy):
    """Hold the deadman for `seconds`; re-press after every forced release like an operator."""
    from tools.tests.test_demo_qualification import wait_until
    if lab.direct is None:
        raise RuntimeError("Held-drive trials run only against the simulated fixture")
    video = quest_video(lab.url)
    controller = quest_controller(lab, video, policy)
    samples, presses, events = [], 0, {}
    try:
        await video.start()
        await controller.start()
        await wait_until(lambda: len(video.bounds) >= 5 and video.current_age() <= 450, timeout=20)
        await controller.release()
        await wait_until(lambda: not controller.status.get("requires_release"), timeout=5)
        start = time.monotonic()
        end = start + seconds
        frames0, bounds0, acks0 = sum(video.sample_counts), len(video.bounds), len(controller.acks)
        resets0 = video.resets
        cad0, inv0 = len(controller.status_cadences), len(controller.invalidations)

        async def sample():
            while time.monotonic() < end:
                status = await lab.status()
                samples.append((time.monotonic() - start, bool(status["left_pwm"] or status["right_pwm"])))
                await asyncio.sleep(.02)

        async def collect():
            # The fixture keeps the last 500 commands; poll so a long trial keeps them all.
            while True:
                for entry in (await lab.fixture_status())["command_events"]:
                    if entry["at"] >= start:
                        events[(entry["at"], entry["seq"])] = entry
                if time.monotonic() >= end:
                    return
                await asyncio.sleep(min(5, max(.05, end - time.monotonic())))

        sampler, collector = asyncio.create_task(sample()), asyncio.create_task(collect())
        while time.monotonic() < end:
            if not controller.armed:
                await controller.release()  # operator lets go >= 300 ms, then presses again
                await wait_until(lambda: controller.press() or time.monotonic() >= end, timeout=seconds + 5)
                if controller.armed:
                    presses += 1
            await asyncio.sleep(.02)
        await sampler
        await collector
        settled = [moving for at, moving in samples if at >= 1]
        gaps, gap_start = [], None
        for at, moving in samples:
            if not moving and gap_start is None:
                gap_start = at
            elif moving and gap_start is not None:
                gaps.append((at - gap_start) * 1000)
                gap_start = None
        invalidations = controller.invalidations[inv0:]
        reasons = {}
        for item in invalidations:
            key = item.get("reason") or item["kind"]
            reasons[key] = reasons.get(key, 0) + 1
        accepted = [entry["permit_age_ms"] for entry in events.values()
                    if entry["accepted"] and entry["permit_age_ms"] is not None]
        rejected = [entry for entry in events.values() if not entry["accepted"]]
        # Lease left on the rover when each renewal landed; at or below zero it stopped.
        headroom = sorted((entry["previous_deadline_at"] - entry["at"]) * 1000 for entry in events.values()
                          if entry["accepted"] and entry["previous_deadline_at"])
        frames = sum(video.sample_counts) - frames0
        return dict(policy=policy, seconds=seconds,
                    interruptions=len(invalidations),
                    interruptions_per_min=round(len(invalidations) * 60 / seconds, 2),
                    interruption_causes=reasons, represses=presses,
                    moving_fraction=round(sum(settled) / max(1, len(settled)), 3),
                    motion_gap_ms=summary(gaps, 0),
                    control_ack_ms=summary(controller.acks[acks0:]),
                    accepted_permit_age_ms=summary(accepted),
                    rejected_commands=len(rejected),
                    lease_headroom_ms=dict(min=round(headroom[0], 1) if headroom else None,
                                           p5=round(percentile(headroom, .05), 1) if headroom else None,
                                           p50=round(percentile(headroom, .5), 1) if headroom else None),
                    status_gap_ms=summary(controller.status_cadences[cad0:]),
                    video_fps=round(frames / seconds, 2),
                    video_age_bound_ms=summary(video.bounds[bounds0:], 0),
                    video_resets=video.resets - resets0, video_errors=video.errors.copy(), control_errors=controller.errors.copy(),
                    client_loop_lag_max_ms=round(max((entry["lag_ms"] for entry in controller.loop_lags),
                                                     default=0), 1))
    finally:
        await controller.close()
        await video.close()


async def run_profile(name, args, overrides=None, label=None):
    result = dict(profile=label or name, emulation=asdict(resolve_profile(name, overrides)), trials=[])
    async with Lab(name, upstream=args.upstream, overrides=overrides, seed=args.seed, permit_hz=args.permit_hz,
                   server_options=json.loads(args.server_options or "{}")) as lab:
        result.update(await probe(lab, args.probe_seconds))
        print(f"[{result['profile']}] probe p50 {result['probe_rtt_p50_ms']} / p95 {result['probe_rtt_p95_ms']} "
              f"/ max {result['probe_rtt_max_ms']} ms", file=sys.stderr, flush=True)
        if not args.calibrate and not args.upstream:
            for policy in args.policies:
                await asyncio.sleep(.5)
                trial = await drive(lab, args.seconds, policy)
                result["trials"].append(trial)
                print(f"[{result['profile']}] {policy}: {trial['interruptions_per_min']}/min interruptions, "
                      f"moving {trial['moving_fraction']:.1%}, permit age p95 {trial['accepted_permit_age_ms']['p95']} ms, "
                      f"video {trial['video_fps']} fps", file=sys.stderr, flush=True)
    result["emulator"] = lab.proxy_stats
    return result


def sweep_points(values):
    # Fixed 50 ms base RTT; mean jitter per direction grows. No stalls or loss,
    # so the RTT distribution alone decides when the 250 ms permit runs out.
    return [(f"jitter{value:g}", dict(one_way_ms=25, jitter_ms=value, stall_hz=0, loss=0,
                                      down_mbps=8, up_mbps=4)) for value in values]


def production_permit_hz():
    from jetson.control.service import ControlService
    return round(1 / ControlService.STATUS_INTERVAL, 1)


async def run(args):
    report = dict(software_evidence_only=True, hardware_verified=False,
                  host=platform.platform(), python=platform.python_version(),
                  started=time.strftime("%Y-%m-%dT%H:%M:%S%z"), upstream=args.upstream or "local fixture",
                  lease_ms=250, permit_cadence_hz=args.permit_hz or production_permit_hz(), command_cadence_hz=20, video_request_window=2,
                  results=[])

    def save():
        if args.report:
            args.report.write_text(json.dumps(report, indent=2) + "\n")

    if args.sweep:
        for label, overrides in sweep_points(args.sweep_jitter):
            report["results"].append(await run_profile("direct", args, overrides, label))
            save()
    else:
        for name in args.profiles:
            report["results"].append(await run_profile(name, args))
            save()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--profiles", nargs="+", default=list(PROFILES), choices=list(PROFILES))
    parser.add_argument("--policies", nargs="+", default=list(POLICIES), choices=list(POLICIES))
    parser.add_argument("--seconds", type=float, default=60, help="Held drive per policy")
    parser.add_argument("--probe-seconds", type=float, default=20)
    parser.add_argument("--calibrate", action="store_true", help="Stop-only probe only")
    parser.add_argument("--sweep", action="store_true", help="Jitter sweep to find the stutter threshold")
    parser.add_argument("--sweep-jitter", type=float, nargs="+", default=[10, 20, 30, 40, 50, 60],
                        help="Mean exponential jitter per direction (ms) on a 50 ms base RTT")
    parser.add_argument("--upstream", help="Real server origin (stop-only), e.g. http://192.168.55.1:8443")
    parser.add_argument("--seed", type=int, default=1010)
    parser.add_argument("--server-options", help="JSON create_app options for the local fixture")
    parser.add_argument("--permit-hz", type=float, default=0,
                        help="Override the fixture permit/status cadence; 0 keeps the production cadence")
    parser.add_argument("--report", type=Path)
    # Internal process entry points.
    parser.add_argument("--serve-proxy", help=argparse.SUPPRESS)
    parser.add_argument("--serve-fixture", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--port", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--upstream-host", help=argparse.SUPPRESS)
    parser.add_argument("--upstream-port", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--overrides", help=argparse.SUPPRESS)
    parser.add_argument("--stats", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.serve_proxy:
        return asyncio.run(serve_proxy(args))
    if args.serve_fixture:
        return serve_fixture(args)
    if not 2 <= args.seconds <= 600 or not 2 <= args.probe_seconds <= 600:
        parser.error("Choose 2..600 second trials")
    if args.upstream:
        from tools.operator_relay import upstream_origin
        try:
            origin = upstream_origin(args.upstream)
        except ValueError as exc:
            parser.error(str(exc))
        if origin.scheme != "http":
            parser.error("--upstream emulation needs the plain-HTTP USB/LAN origin (TLS hostnames break behind the proxy)")
        args.calibrate = True  # never request motion from a real server
    def interrupt(signum, frame):
        raise KeyboardInterrupt  # unwind so every lab process is stopped
    signal.signal(signal.SIGTERM, interrupt)
    try:
        report = asyncio.run(run(args))
    except KeyboardInterrupt:
        parser.exit(130, "Interrupted; partial results are in --report\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
