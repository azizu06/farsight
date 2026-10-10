"""Shared harness for the containerised navigation tests: jetson.server + nav.launch.py + an operator.

Runs inside farsight-nav:jazzy (ros2/docker/run-tests.sh). The operator is a real /ws/control client:
it releases, then holds the deadman at 20 Hz with server-issued permits, exactly like the Quest.
"""

import asyncio
import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

import websockets

REPO = os.environ.get("FARSIGHT_REPO", "/farsight")
VENV_PY = os.environ.get("FARSIGHT_SERVER_PYTHON", "/opt/farsight-venv/bin/python")
OUT = os.environ.get("FARSIGHT_OUT", "/out")


class Stack:
    """Starts the rover server and the ROS navigation launch; stops both (process groups) on exit."""

    def __init__(self, name, server_args=(), launch_args=(), port=8443):
        self.name, self.port = name, port
        self.server_args, self.launch_args = list(server_args), list(launch_args)
        self.url = f"http://127.0.0.1:{port}"
        self.processes = []

    def __enter__(self):
        os.makedirs(OUT, exist_ok=True)
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        server = [VENV_PY, "-m", "jetson.server", "--host", "127.0.0.1", "--port", str(self.port),
                  "--detector", "none", "--nav2", *self.server_args]
        self.processes.append(self._start(server, f"{self.name}-server.log", env, cwd=REPO))
        self.wait(lambda: self.get("/health") is not None, 30, "server never answered /health")
        launch = ["ros2", "launch", "farsight_nav", "nav.launch.py", f"server:={self.url}", *self.launch_args]
        self.processes.append(self._start(launch, f"{self.name}-nav.log", env))
        return self

    def _start(self, command, log, env, cwd=None):
        handle = open(os.path.join(OUT, log), "w")
        return subprocess.Popen(command, stdout=handle, stderr=subprocess.STDOUT, env=env, cwd=cwd,
                                start_new_session=True)

    def __exit__(self, *exc):
        for process in reversed(self.processes):
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGINT)
        for process in reversed(self.processes):
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
        return False

    def request(self, path, body=None, method=None):
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(self.url + path, data=data, method=method or ("POST" if data else "GET"),
                                         headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                return response.status, json.loads(response.read() or b"null")
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read() or b"null")

    def get(self, path):
        try:
            status, body = self.request(path)
            return body if status == 200 else None
        except OSError:
            return None

    def wait(self, predicate, timeout, message, period=0.2):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for process in self.processes:
                if process.poll() is not None:
                    raise AssertionError(f"{message}: a stack process exited ({process.args[:3]})")
            try:
                value = predicate()
            except Exception:
                value = None
            if value:
                return value
            time.sleep(period)
        raise AssertionError(message)

    def nav_ready(self, timeout=90):
        return self.wait(lambda: (self.get("/nav/status") or {}).get("detail", {}).get("remote", {}).get("nav2"),
                         timeout, "Nav2 never became ready through the bridge")


class Operator:
    """Held-deadman /ws/control client on its own thread. mode/v/w can change while it runs."""

    def __init__(self, url, mode="teleop"):
        self.url = url.replace("http://", "ws://") + "/ws/control"
        self.mode, self.v, self.w = mode, 0.0, 0.0
        self.holding = True
        self.status = {}
        self.samples = []      # (monotonic, status) for every control_status received
        self.stop = threading.Event()
        self.thread = threading.Thread(target=lambda: asyncio.run(self.run()), daemon=True)

    def __enter__(self):
        self.thread.start()
        deadline = time.monotonic() + 10
        while not self.status.get("session_id") and time.monotonic() < deadline:
            time.sleep(0.05)
        return self

    def __exit__(self, *exc):
        self.stop.set()
        self.thread.join(timeout=5)
        return False

    async def run(self):
        async with websockets.connect(self.url) as ws:
            status = json.loads(await ws.recv())
            self.status = status
            seq = 0
            await ws.send(json.dumps(dict(session_id=status["session_id"], seq=seq, deadman=False)))
            released = False
            while not self.stop.is_set():
                status = json.loads(await ws.recv())
                self.status = status
                self.samples.append((time.monotonic(), status))
                seq += 1
                if not self.holding or (status.get("requires_release") and not released):
                    await ws.send(json.dumps(dict(session_id=status["session_id"], seq=seq, deadman=False)))
                    released = True
                    continue
                released = False
                await ws.send(json.dumps(dict(
                    session_id=status["session_id"], permit=status["permit"], seq=seq, deadman=True,
                    v=self.v, w=self.w, pan=0, mode=self.mode, video_fresh=True, video_age_ms=0)))


def report(name, ok, detail):
    line = f"{'PASS' if ok else 'FAIL'} {name}: {detail}"
    print(line, flush=True)
    with open(os.path.join(OUT, "results.txt"), "a") as handle:
        handle.write(line + "\n")
    return ok


def main(checks):
    """Run named check functions; exit non-zero if any fails."""
    failures = 0
    only = set(sys.argv[1:])
    for name, check in checks:
        if only and name not in only:
            continue
        started = time.monotonic()
        try:
            detail = check()
            report(name, True, f"{detail} ({time.monotonic() - started:.0f} s)")
        except Exception as exc:
            failures += 1
            report(name, False, f"{exc} ({time.monotonic() - started:.0f} s)")
    sys.exit(1 if failures else 0)
