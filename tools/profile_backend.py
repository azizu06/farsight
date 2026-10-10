"""Isolated software profiling with real FFmpeg, detector and delayed relay.

psutil supplies required CPU/thread samples. No camera or physical serial opens.
Scan bursts exercise production scan kernels, not a physical/control sweep.
"""

import argparse
import asyncio
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from functools import wraps
import hashlib
import json
from pathlib import Path
import platform
import sys
import threading
import time

from tools.qualify_link import percentile
from tools.tests.test_demo_qualification import Rig, fixture_app, motion_trial


class Timings:
    """Bounded tail samples plus lifetime counts, safe for executor workers."""
    def __init__(self, capacity=4096):
        self.capacity = capacity
        self.values = defaultdict(lambda: deque(maxlen=capacity))
        self.counts = defaultdict(int)
        self.lock = threading.Lock()

    def add(self, name, value):
        with self.lock:
            self.values[name].append(value)
            self.counts[name] += 1

    def summary(self):
        with self.lock:
            return {name: dict(count=self.counts[name], retained=len(values),
                               p50=percentile(list(values), .5), p95=percentile(list(values), .95),
                               maximum=max(values)) for name, values in self.values.items() if values}


class TimedLock:
    """Fixture-only outer hold/wait measurements; preserve recursive locking."""
    def __init__(self, lock, name, timings):
        self.lock, self.name, self.timings = lock, name, timings
        self.local = threading.local()

    def __enter__(self):
        started = time.monotonic()
        self.lock.acquire()
        depth = getattr(self.local, "depth", 0)
        if depth == 0:
            self.timings.add(self.name + ".wait_ms", (time.monotonic() - started) * 1000)
            self.local.started = time.monotonic()
        self.local.depth = depth + 1
        return self

    def __exit__(self, *exc):
        self.local.depth -= 1
        elapsed = time.monotonic() - self.local.started if self.local.depth == 0 else None
        self.lock.release()
        if elapsed is not None:
            self.timings.add(self.name + ".hold_ms", elapsed * 1000)


class ThreadCpuProfile:
    """Thread-local Python/native CPU accounting, excluding callback overhead.

    Python CPU is a GIL-demand proxy, not GIL ownership or wait time. In
    particular, a native call may hold or release the GIL. Use only in separate
    intrusive diagnostic runs, never the sustained-motion comparison.
    """
    def __init__(self):
        self.cpu = dict(python=0.0, native=0.0)
        self.mode, self.stack = "python", []
        self.last = time.thread_time()

    def __call__(self, frame, event, arg):
        now = time.thread_time()
        self.cpu[self.mode] += now - self.last
        if event in ("call", "c_call"):
            self.stack.append(self.mode)
            self.mode = "native" if event == "c_call" else "python"
        elif event in ("return", "c_return", "c_exception"):
            self.mode = self.stack.pop() if self.stack else "python"
        self.last = time.thread_time()


def profile_app(args):
    import cv2
    from fastapi import HTTPException, Request
    from jetson.depth import back_project
    from jetson.mapping.demo import simulated_frame
    from jetson.mapping.voxel import VoxelMap

    timings = Timings()
    if args.cv_threads is not None:
        cv2.setNumThreads(args.cv_threads)
    try:
        import onnxruntime as ort
    except ImportError:
        ort = None
        if args.ort_threads is not None:
            raise ValueError("--ort-threads needs onnxruntime")
    original_session = ort.InferenceSession if ort else None
    class Session:
        def __init__(self, *positional, **kwargs):
            if args.ort_threads is not None:
                # Experiment-only override; omission measures production defaults.
                options = kwargs.get("sess_options") or ort.SessionOptions()
                options.intra_op_num_threads = args.ort_threads
                kwargs["sess_options"] = options
            self.session = original_session(*positional, **kwargs)
        def __getattr__(self, name):
            return getattr(self.session, name)
        def run(self, *positional, **kwargs):
            start = time.monotonic()
            try:
                return self.session.run(*positional, **kwargs)
            finally:
                timings.add("onnx.run_ms", (time.monotonic() - start) * 1000)
    app = fixture_app(args.serve_fixture, scene_path=args.scene,
                      server_options=dict(detector=args.detector, detector_model=args.model))
    state, camera = app.state.mapping, app.state.camera
    if args.pause_fusion:
        state.paused = True
    for owner, name in ((state, "MapState.lock"), (camera, "CameraService.lock"),
                        (app.state.control.state, "ControlState.lock")):
        owner.lock = TimedLock(owner.lock, name, timings)

    def measure_sync(owner, method):
        original = getattr(owner, method)
        name = type(owner).__name__ + "." + method
        @wraps(original)
        def measured(*positional, **kwargs):
            wall, cpu = time.monotonic(), time.thread_time()
            profiler = ThreadCpuProfile() if method == "process" and args.fusion_python_profile else None
            if profiler:
                previous_profiler = sys.getprofile()
                sys.setprofile(profiler)
            try:
                return original(*positional, **kwargs)
            finally:
                if profiler:
                    sys.setprofile(previous_profiler)
                    for mode, seconds in profiler.cpu.items():
                        timings.add(name + "." + mode + "_cpu_ms", seconds * 1000)
                timings.add(name + ".wall_ms", (time.monotonic() - wall) * 1000)
                timings.add(name + ".cpu_ms", (time.thread_time() - cpu) * 1000)
        setattr(owner, method, measured)
    for owner, method in ((state, "process"), (state, "camera_snapshot"), (camera, "snapshot")):
        measure_sync(owner, method)

    video = app.state.video
    original_publish = video.publish
    from collections import OrderedDict
    submitted = OrderedDict()
    class Samples(OrderedDict):
        previous = None
        def __setitem__(self, key, value):
            submitted[key] = time.monotonic()
            timings.add("camera.age_at_encoder_ms", (time.monotonic() - value.received) * 1000)
            identity = (value.source, value.frame_id, value.received)
            if identity != self.previous:
                if self.previous:
                    timings.add("camera.capture_interval_ms", (value.received - self.previous[2]) * 1000)
                self.previous = identity
            while len(submitted) > 600:
                submitted.popitem(last=False)
            super().__setitem__(key, value)
    video.samples = Samples()
    def publish(fragment):
        if fragment:
            first = round(fragment["media_start_s"] * video.fps)
            if first in submitted:
                timings.add("h264.input_to_fragment_ms", (time.monotonic() - submitted.pop(first)) * 1000)
        return original_publish(fragment)
    video.publish = publish
    original_thread = asyncio.to_thread
    async def measured_thread(func, /, *positional, **kwargs):
        queued = time.monotonic()
        name = getattr(func, "__qualname__", type(func).__name__)
        def work():
            started = time.monotonic()
            timings.add(name + ".queue_ms", (started - queued) * 1000)
            try:
                return func(*positional, **kwargs)
            finally:
                timings.add(name + ".work_ms", (time.monotonic() - started) * 1000)
        return await original_thread(work)

    async def resources():
        import psutil
        process = psutil.Process()
        previous = {}
        while True:
            now = time.monotonic()
            for item in [process, *process.children()]:
                try:
                    label = "server" if item.pid == process.pid else item.name()
                    cpu = item.cpu_times()
                    total = cpu.user + cpu.system
                    old = previous.get(item.pid)
                    if old:
                        timings.add(label + ".cpu_percent", 100 * (total - old[1]) / (now - old[0]))
                    previous[item.pid] = (now, total)
                    timings.add(label + ".threads", item.num_threads())
                except psutil.Error:
                    pass
            await asyncio.sleep(.1)

    original_lifespan = app.router.lifespan_context
    @asynccontextmanager
    async def lifespan(app):
        if ort:
            ort.InferenceSession = Session
        asyncio.to_thread = measured_thread
        monitor = asyncio.create_task(resources())
        try:
            async with original_lifespan(app):
                yield
        finally:
            monitor.cancel()
            await asyncio.gather(monitor, return_exceptions=True)
            asyncio.to_thread = original_thread
            if ort:
                ort.InferenceSession = original_session
    app.router.lifespan_context = lifespan

    def guard(request):
        if request.client.host not in ("127.0.0.1", "::1") or not app.state.control.state.simulated:
            raise HTTPException(403, "Profiling requires localhost and simulated motors")

    @app.get("/__profile")
    async def profile(request: Request):
        guard(request)
        mapping = await asyncio.to_thread(state.status)
        return dict(timings=timings.summary(), mapping=mapping, opencv_threads=cv2.getNumThreads(),
                    perception=app.state.perception.health())

    # One bounded scan-kernel job; HTTP callers cannot queue overlapping bursts.
    burst_lock = asyncio.Lock()
    @app.post("/__profile/scan-kernels")
    async def scan_kernels(request: Request):
        guard(request)
        if burst_lock.locked():
            raise HTTPException(503, "Scan kernels busy")
        async with burst_lock:
            cloud = VoxelMap(size=.025, capacity=100_000)
            for index, yaw in enumerate((-80, -40, 0, 40, 80)):
                frame = await asyncio.to_thread(simulated_frame, 0, index, scan_yaw=yaw)
                k = frame.calibration
                xyz, colors = await asyncio.to_thread(back_project, frame.depth, frame.rgb,
                    (k.fx, k.fy, k.cx, k.cy), yaw)
                await asyncio.to_thread(cloud.integrate, xyz, colors)
            return dict(points=len(cloud.cells), simulated=True, scan_kernels_only=True)

    app.router.routes.sort(key=lambda route: 0 if getattr(route, "path", "").startswith("/__") else 1)
    return app


async def bursts(rig, events):
    async def request(path, body=None):
        start = time.monotonic()
        async with rig.client.post(rig.direct + path, json=body) as response:
            await response.read()
            events.append(dict(path=path, status=response.status,
                               elapsed_ms=(time.monotonic() - start) * 1000))
    while True:
        await asyncio.gather(request("/__profile/scan-kernels"),
                             request("/ask", {"text": "What do you see?"}))
        await asyncio.sleep(2)


async def run(args):
    import cv2
    from importlib.metadata import PackageNotFoundError, version
    try:
        ort_version = version("onnxruntime")
    except PackageNotFoundError:
        ort_version = None
    report = dict(host=platform.platform(), python=platform.python_version(),
        opencv_version=cv2.__version__, onnxruntime_version=ort_version, hardware_verified=False,
        settings=dict(detector=args.detector, model_sha256=hashlib.sha256(Path(args.model).read_bytes()).hexdigest()
                      if args.model else None,
                      scene_sha256=hashlib.sha256(args.scene.read_bytes()).hexdigest()
                          if args.scene and args.source == "rgb15" else None,
                      cv_threads=args.cv_threads, ort_threads=args.ort_threads, source=args.source,
                      fusion_python_profile=args.fusion_python_profile,
                      pause_fusion=args.pause_fusion,
                      delay_each_direction_ms=50, aggregate_mbps=2, encoder_fps=10,
                      encoder_bitrate=500000, request_window=2, permit_ms=250, command_hz=20, view_guard_ms=500),
        limitations=["Software capture receipt, not sensor/display latency", "No Quest, GPU or physical motors",
                     "Assistant uses local observation summary; no VLM/STT/TTS model",
                     "Scan bursts use production synthetic geometry kernels; no control sweep/depth model"], trials=[])
    for repeat in range(args.repeats):
        for load in args.loads:
            extra = ["--detector", args.detector]
            if args.fusion_python_profile:
                extra.append("--fusion-python-profile")
            if args.pause_fusion:
                extra.append("--pause-fusion")
            for flag, value in (("--model", args.model), ("--scene", args.scene),
                                ("--cv-threads", args.cv_threads), ("--ort-threads", args.ort_threads)):
                if value is not None:
                    extra.extend((flag, str(value)))
            async with Rig(args.source, fixture_module="tools.profile_backend", fixture_args=extra) as rig:
                events = []
                burst = asyncio.create_task(bursts(rig, events)) if load == "combined" else None
                try:
                    result = await motion_trial(rig, duration=args.duration)
                finally:
                    burst_error = None
                    if burst:
                        if burst.done() and not burst.cancelled():
                            error = burst.exception()
                            burst_error = str(error) if error else "Burst task ended unexpectedly"
                        burst.cancel()
                        await asyncio.gather(burst, return_exceptions=True)
                async with rig.client.get(rig.direct + "/__profile") as response:
                    response.raise_for_status()
                    profile = await response.json()
                valid = (args.detector == "none" or (not profile["perception"]["detector_error"]
                         and profile["perception"]["detector_fps"] > 0))
                if args.source == "map5" and not args.pause_fusion:
                    valid = valid and profile["mapping"]["frames"] > 0
                if load == "combined":
                    completed = {event["path"] for event in events if event["status"] == 200}
                    valid = valid and not burst_error and completed == {"/__profile/scan-kernels", "/ask"}
                report["trials"].append(dict(repeat=repeat + 1, load=load, workload_valid=valid, motion=result,
                                             profile=profile, bursts=events, burst_error=burst_error))
                args.report.write_text(json.dumps(report, indent=2) + "\n")
                if not valid:
                    raise RuntimeError("Detector/burst workload failed; invalid trial retained in report")
                print(f"{load} repeat {repeat + 1}: moving={result['stable_moving_fraction']:.3f}, "
                      f"lag={result['server_event_loop_lag_max_ms']:.1f} ms", flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serve-fixture", choices=("rgb15", "map5"))
    parser.add_argument("--source", choices=("rgb15", "map5"), default="rgb15")
    parser.add_argument("--port", type=int)
    parser.add_argument("--detector", choices=("none", "hog", "pose"), default="hog")
    parser.add_argument("--model")
    parser.add_argument("--scene", type=Path)
    parser.add_argument("--cv-threads", type=int)
    parser.add_argument("--ort-threads", type=int, help="Experiment override; 0 reproduces the unrestricted ONNX default")
    parser.add_argument("--fusion-python-profile", action="store_true",
                        help="Intrusive per-fusion thread-CPU profile; Python CPU is only a GIL-demand proxy")
    parser.add_argument("--pause-fusion", action="store_true", help="Diagnosis-only: freeze fusion, retain camera production")
    parser.add_argument("--duration", type=float, default=20)
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--loads", nargs="+", choices=("quiet", "combined"), default=["quiet", "combined"])
    parser.add_argument("--report", type=Path, default=Path("backend-profile.json"))
    args = parser.parse_args()
    if (not 2 <= args.duration <= 300 or not 1 <= args.repeats <= 10
            or (args.cv_threads is not None and not 1 <= args.cv_threads <= 32)
            or (args.ort_threads is not None and not 0 <= args.ort_threads <= 32)):
        parser.error("Use duration 2..300, repeats 1..10, OpenCV threads 1..32, ONNX threads 0..32")
    try:
        import psutil  # noqa: F401: required evidence, never silently omitted
    except ImportError:
        parser.error("Install the profiling dependency: python -m pip install psutil")
    if args.serve_fixture:
        import uvicorn
        uvicorn.run(profile_app(args), host="127.0.0.1", port=args.port,
                    access_log=False, log_level="warning", timeout_graceful_shutdown=2)
    else:
        asyncio.run(run(args))


if __name__ == "__main__":
    main()
