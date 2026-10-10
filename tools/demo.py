"""Launch the operator path; no package installs or privileged changes.

python -m tools.demo --simulate --build
python -m tools.demo --upstream https://jetson.tailnet.ts.net:8443 --quest
"""

import argparse
import importlib.util
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def run(command):
    subprocess.run(command, cwd=ROOT, check=True)


def free_port(port):
    with socket.socket() as sock:
        # Match aiohttp/uvicorn restart behavior; a closed socket in TIME_WAIT
        # must not make the next demo launch look like an active listener.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            raise RuntimeError(f"Port {port} is already in use; stop that server or choose another port")


def wait_http(url, process):
    for _ in range(100):
        if process.poll() is not None:
            raise RuntimeError(f"Server exited with code {process.returncode}")
        try:
            with urllib.request.urlopen(url, timeout=0.25) as response:
                if response.status == 200:
                    return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"Server did not become ready: {url}")


def quest_reverse(port, serial):
    if not shutil.which("adb"):
        raise RuntimeError("Android Platform Tools missing: install adb before the Quest hardware check")
    prefix = ["adb"] + (["-s", serial] if serial else [])
    # adb get-state fails helpfully for unauthorized/offline/multiple devices.
    run(prefix + ["get-state"])
    run(prefix + ["reverse", f"tcp:{port}", f"tcp:{port}"])
    run(prefix + ["reverse", "--list"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--simulate", action="store_true", help="Run local mapped-room and stopped virtual rover")
    target.add_argument("--upstream", help="Jetson origin; HTTPS verifies certificates by default")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--jetson-port", type=int, default=8443)
    parser.add_argument("--build", action="store_true", help="Build bundled Quest assets with installed npm dependencies")
    parser.add_argument("--h264", action="store_true", help="Enable CPU H.264 video in simulation (requires ffmpeg)")
    parser.add_argument("--quest", action="store_true", help="Configure the connected Quest ADB reverse tunnel")
    parser.add_argument("--adb-serial", help="Select the Quest when multiple ADB devices are connected")
    parser.add_argument("--ca-file", help="CA PEM for private Jetson TLS")
    parser.add_argument("--bonded-upstream", help="Opt-in second route to the same Jetson; requires server --bonded-control, JPEG only")
    parser.add_argument("--check", action="store_true", help="Check prerequisites and exit without starting servers")
    args = parser.parse_args()
    processes = []
    def stop_on_signal(signum, frame):
        raise KeyboardInterrupt
    previous_term_handler = signal.signal(signal.SIGTERM, stop_on_signal)
    try:
        packages = ["aiohttp"] + (["numpy", "cv2", "fastapi", "uvicorn"] if args.simulate else [])
        missing = [name for name in packages if importlib.util.find_spec(name) is None]
        if missing:
            raise RuntimeError("Missing Python packages: " + ", ".join(missing)
                + "\nInstall into your environment: python -m pip install -r tools/requirements.txt"
                + (" -r jetson/mapping/requirements.txt" if args.simulate else ""))
        if args.upstream:
            from .operator_relay import upstream_origin
            try:
                upstream_origin(args.upstream)
            except ValueError as exc:
                raise RuntimeError(str(exc))
        if args.bonded_upstream:
            if args.simulate or not args.upstream:
                raise RuntimeError("--bonded-upstream needs two explicit routes; configure fault-path simulation separately")
            from .operator_relay import upstream_origin
            try:
                second = upstream_origin(args.bonded_upstream)
            except ValueError as exc:
                raise RuntimeError(str(exc)) from None
            if second == upstream_origin(args.upstream):
                raise RuntimeError("Bonded routes must be distinct")
        if args.build:
            if not shutil.which("npm") or not (ROOT / "quest/node_modules").is_dir():
                raise RuntimeError("Build prerequisites missing; run npm --prefix quest ci before going offline")
            if not args.check:
                run(["npm", "--prefix", "quest", "run", "build"])
        elif not (ROOT / "quest/dist/index.html").is_file():
            raise RuntimeError("Quest build missing; use --build after npm --prefix quest ci")
        free_port(args.port)
        if args.simulate:
            free_port(args.jetson_port)
        if args.h264:
            if not args.simulate:
                raise RuntimeError("--h264 configures the local simulation; configure video on the remote Jetson separately")
            if not shutil.which("ffmpeg"):
                raise RuntimeError("ffmpeg missing; install before enabling --h264")
        if args.quest and not shutil.which("adb"):
            raise RuntimeError("Android Platform Tools (adb) missing")
        if args.check:
            print("Prerequisites ready. USB, cellular routing, TLS, and headset WebXR still require hardware checks.")
            return

        def start(command):
            process = subprocess.Popen(command, cwd=ROOT, start_new_session=True)
            processes.append(process)
            return process

        upstream = args.upstream
        if args.simulate:
            upstream = f"http://127.0.0.1:{args.jetson_port}"
            server_command = [sys.executable, "-m", "jetson.server", "--demo",
                              "--simulate-rover", "--host", "127.0.0.1", "--port", str(args.jetson_port)]
            if args.h264:
                server_command.append("--h264")
            server = start(server_command)
            wait_http(upstream + "/health", server)
        command = [sys.executable, "-m", "tools.operator_relay", "--upstream", upstream,
                   "--port", str(args.port)]
        if args.ca_file:
            command += ["--ca-file", args.ca_file]
        if args.bonded_upstream:
            command += ["--bonded-upstream", args.bonded_upstream]
        relay = start(command)
        wait_http(f"http://127.0.0.1:{args.port}/relay/health", relay)
        if args.quest:
            quest_reverse(args.port, args.adb_serial)
        print(f"Open http://localhost:{args.port} in Quest Browser" if args.quest
              else f"Open http://localhost:{args.port}; add --quest when the headset is connected", flush=True)
        print("Simulation: no physical motors" if args.simulate else f"Rover upstream: {upstream}", flush=True)
        print("Ctrl-C stops both local processes. Reapply ADB reverse after reconnecting USB.", flush=True)
        while all(process.poll() is None for process in processes):
            time.sleep(0.2)
        raise RuntimeError("A demo process ended; stopped the remaining local processes")
    except KeyboardInterrupt:
        pass
    except (RuntimeError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"{exc}\n")
    finally:
        signal.signal(signal.SIGTERM, previous_term_handler)
        for process in processes:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
        for process in processes:
            try:
                process.wait(timeout=4)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()


if __name__ == "__main__":
    main()
