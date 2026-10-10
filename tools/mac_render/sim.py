"""Start the local simulated map. Same command tools/demo.py uses. No motors."""

import atexit
import os
import signal
import subprocess
import sys
import time
import urllib.request


def launch_sim(port):
    command = [
        sys.executable, "-m", "jetson.server", "--demo", "--simulate-rover",
        "--host", "127.0.0.1", "--port", str(port),
    ]
    process = subprocess.Popen(command, start_new_session=True)
    atexit.register(stop_sim, process)
    url = f"http://127.0.0.1:{port}/health"
    for _ in range(100):
        if process.poll() is not None:
            raise RuntimeError(f"sim server exited {process.returncode}")
        try:
            with urllib.request.urlopen(url, timeout=0.25) as response:
                if response.status == 200:
                    return process
        except OSError:
            time.sleep(0.1)
    stop_sim(process)
    raise RuntimeError(f"sim server did not answer {url}")


def stop_sim(process):
    """Stop the sim and the session it was started in. Safe to call twice."""
    if process is None or process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            process.kill()
        process.wait(timeout=3)
