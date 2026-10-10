"""USB-only Mac loopback forward to the native operator phone bridge.

python -m tools.phone_usb --check
python -m tools.phone_usb
Requires installed modern iproxy (libusbmuxd) and idevice_id (libimobiledevice).
Never installs tools, changes network/device settings, or sends control commands.

Flag semantics verified against the upstream tools/iproxy.c and
tools/idevice_id.c in github.com/libimobiledevice/libusbmuxd and libimobiledevice.
Legacy iproxy without explicit USB-only/source-address support is refused.
"""

import argparse
import http.client
import json
import math
import os
import platform
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time


class BridgeError(RuntimeError):
    """Safe actionable text; raw tool output and identifiers never escape."""


def tool_output(argv):
    try:
        result = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, timeout=3, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise BridgeError("USB tool unavailable or timed out; check the installed tools and local usbmuxd") from None
    if result.returncode != 0 or len(result.stdout) + len(result.stderr) > 65536:
        raise BridgeError("USB tool query failed; check installed tool versions and the local USB connection")
    return result.stdout + (result.stderr if "--help" in argv else "")


def require_modern_iproxy(help_text, dual_route=False):
    lines = help_text.splitlines()
    if ("LOCAL_PORT:DEVICE_PORT" not in help_text
            or not any("--local" in line and "USB" in line for line in lines)
            or not any("--source" in line and "address" in line.lower() for line in lines)
            or not any("--udid" in line for line in lines)):
        raise BridgeError("Unsupported legacy iproxy: install a modern libusbmuxd with USB-only --local, "
                          "loopback --source, --udid, and LOCAL_PORT:DEVICE_PORT support; no network fallback is allowed")
    if dual_route and "LOCAL_PORT2:DEVICE_PORT2" not in help_text:
        raise BridgeError("iproxy does not document multiple port pairs; install a modern multiport libusbmuxd before --dual-route")


def select_usb_device(text, requested=None):
    devices = [line.strip() for line in text.splitlines() if line.strip()]
    pattern = r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{8}-[0-9a-fA-F]{16})"
    if any(not re.fullmatch(pattern, value) for value in devices) or len(set(devices)) != len(devices):
        raise BridgeError("USB enumeration returned ambiguous or unsupported device evidence")
    if not devices:
        raise BridgeError("No USB iOS device found; connect and unlock the operator phone with a data cable")
    if requested is not None:
        if requested not in devices:
            raise BridgeError("Selected device is not present in USB-only enumeration; no network-device fallback is allowed")
        return requested
    if len(devices) != 1:
        raise BridgeError("Multiple USB iOS devices found; select the operator phone using --device (identifier is not logged)")
    return devices[0]


def preflight_port(port):
    if type(port) is not int or not 1024 <= port <= 65535:
        raise BridgeError("Local port must be an integer in 1024..65535")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            raise BridgeError("Local loopback port is already in use or cannot be bound; stop that listener or choose --local-port") from None


def health_summary(packet):
    if (not isinstance(packet, dict) or type(packet.get("camera")) is not bool
            or type(packet.get("rover")) is not bool or not isinstance(packet.get("camera_source"), str)
            or not isinstance(packet.get("detector"), str) or not isinstance(packet.get("video"), dict)):
        raise BridgeError("Forwarded /health response lacks the expected Farsight backend fields")
    source = packet["camera_source"]
    source = source if source in ("PHONE RGB", "IPHONE LIDAR", "USB CAMERA", "SIMULATED", "WAITING") else "REPLAY" if source.startswith("REPLAY") else "OTHER"
    backend = packet.get("detector_backend", packet["detector"])
    backend = backend if backend in ("none", "hog", "pose", "pose-tensorrt", "pose-onnx", "pose-onnx-cv2dnn") else "OTHER"
    def boolean(value): return value if type(value) is bool else None
    control = packet.get("control")
    return dict(camera=packet["camera"], camera_source=source,
                camera_drive_ready=boolean(packet.get("camera_drive_ready")),
                camera_capture_verified=boolean(packet.get("camera_capture_verified")),
                detector_backend=backend, rover=packet["rover"],
                video_ready=boolean(packet["video"].get("ready")),
                simulated=boolean(control.get("simulated")) if isinstance(control, dict) else None,
                replay=boolean(packet.get("replay")))


class PhoneUSB:
    PHONE_PORT = 8081
    SECONDARY_PHONE_PORT = 8082

    def __init__(self, local_port=18443, device=None, timeout=5, iproxy=None, idevice_id=None,
                 secondary_local_port=None):
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not .1 <= timeout <= 30:
            raise BridgeError("Timeout must be finite seconds in 0.1..30")
        self.local_port, self.device, self.timeout = local_port, device, float(timeout)
        self.secondary_local_port = secondary_local_port
        self.iproxy, self.idevice_id = iproxy, idevice_id
        self.process = None

    def start(self):
        if self.process is not None:
            raise BridgeError("USB forward is already started")
        # Upstream libusbmuxd supports remote daemon overrides; refuse them so
        # --local cannot accidentally refer to USB devices on another host.
        if "USBMUXD_SOCKET_ADDRESS" in os.environ:
            raise BridgeError("Unset USBMUXD_SOCKET_ADDRESS to require this Mac's local USB daemon")
        if self.secondary_local_port is not None and self.secondary_local_port == self.local_port:
            raise BridgeError("Primary and secondary loopback ports must be distinct")
        mappings = [(self.local_port, self.PHONE_PORT)]
        if self.secondary_local_port is not None:
            mappings.append((self.secondary_local_port, self.SECONDARY_PHONE_PORT))
        for local, _ in mappings:
            preflight_port(local)
        iproxy = self.iproxy or shutil.which("iproxy")
        listing = self.idevice_id or shutil.which("idevice_id")
        if not iproxy or not listing:
            raise BridgeError("Install modern libusbmuxd (iproxy) and libimobiledevice (idevice_id) before running this helper; no automatic installs")
        require_modern_iproxy(tool_output([iproxy, "--help"]), self.secondary_local_port is not None)
        listing_help = tool_output([listing, "--help"])
        if not any("--list" in line and "USB" in line for line in listing_help.splitlines()):
            raise BridgeError("idevice_id must document USB-only --list; unsupported enumeration is refused")
        selected = select_usb_device(tool_output([listing, "--list"]), self.device)
        argv = [iproxy, "--local", "--source", "127.0.0.1", "--udid", selected]
        argv.extend(f"{local}:{phone}" for local, phone in mappings)
        try:
            # iproxy prints device identifiers for individual connections even
            # without debug mode. Suppress its output rather than forwarding it.
            self.process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                            stderr=subprocess.DEVNULL, start_new_session=True)
            deadline = time.monotonic() + self.timeout
            pending = {local for local, _ in mappings}
            while time.monotonic() < deadline:
                self.ensure_running()
                for local in tuple(pending):
                    try:
                        with socket.create_connection(("127.0.0.1", local), timeout=.1):
                            pass
                        pending.remove(local)
                    except OSError:
                        pass
                if not pending:
                    self.ensure_running()
                    return self
                time.sleep(.025)
            raise BridgeError("iproxy did not open all configured loopback listeners before the timeout")
        except BaseException:
            self.close()
            raise

    def ensure_running(self):
        if self.process is None or self.process.poll() is not None:
            raise BridgeError("iproxy exited; check the cable, modern tool support, and operator bridge app (raw device output suppressed)")

    def check_health(self, local_port=None):
        local_port = self.local_port if local_port is None else local_port
        if type(local_port) is not int or local_port not in (self.local_port, self.secondary_local_port):
            raise BridgeError("Health probe port must match a configured loopback mapping")
        self.ensure_running()
        deadline = time.monotonic() + self.timeout
        sock = None
        response = None
        timer = None
        try:
            sock = socket.create_connection(("127.0.0.1", local_port), timeout=self.timeout)
            def expire():
                # Bound the complete probe, including trickled HTTP headers.
                # Retain the original socket through response parsing so a
                # Connection:close response cannot evade this shutdown.
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            timer = threading.Timer(max(0, deadline - time.monotonic()), expire)
            timer.daemon = True
            timer.start()
            sock.sendall(f"GET /health HTTP/1.1\r\nHost: 127.0.0.1:{local_port}\r\nConnection: close\r\n\r\n".encode("ascii"))
            response = http.client.HTTPResponse(sock)
            response.begin()
            # Reject redirects instead of following an arbitrary remote URL.
            if response.status != 200:
                raise BridgeError("Local forwarded /health did not return HTTP200; check both native bridge roles and the rover USB backend")
            data = bytearray()
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise BridgeError("Local forwarded health check timed out")
                sock.settimeout(remaining)
                chunk = response.read1(min(4096, 65537 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > 65536:
                    raise BridgeError("Local forwarded health response exceeds the bounded size")
            packet = json.loads(data, parse_constant=lambda value: (_ for _ in ()).throw(ValueError()))
            self.ensure_running()
            return health_summary(packet)
        except (OSError, ValueError, http.client.HTTPException):
            raise BridgeError("Local forwarded health check failed; keep both bridge apps foreground and verify the configured phone/Jetson endpoints") from None
        finally:
            if timer is not None:
                timer.cancel()
            if response is not None:
                response.close()
            if sock is not None:
                sock.close()

    def close(self):
        process, self.process = self.process, None
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", help="Explicit operator phone USB identifier; never printed")
    parser.add_argument("--local-port", type=int, default=18443)
    parser.add_argument("--dual-route", action="store_true", help="Opt in to a second USB mapping to operator phone port8082")
    parser.add_argument("--secondary-local-port", type=int, help="Secondary loopback port with --dual-route (default18444)")
    parser.add_argument("--timeout", type=float, default=5, help="Bound startup and optional health probe separately, max30s")
    parser.add_argument("--check", action="store_true", help="Probe only local forwarded /health then stop iproxy; sends no controls")
    args = parser.parse_args(argv)
    if args.secondary_local_port is not None and not args.dual_route:
        parser.error("--secondary-local-port requires --dual-route")
    secondary = (args.secondary_local_port if args.secondary_local_port is not None else 18444) if args.dual_route else None
    proxy = None
    previous = None
    def interrupted(signum, frame): raise KeyboardInterrupt
    try:
        if platform.system() != "Darwin":
            raise BridgeError("This operator helper supports macOS only")
        previous = signal.signal(signal.SIGTERM, interrupted)
        proxy = PhoneUSB(args.local_port, args.device, args.timeout, secondary_local_port=secondary).start()
        routes = [dict(local_port=args.local_port, phone_port=proxy.PHONE_PORT, health=None)]
        if secondary is not None:
            routes.append(dict(local_port=secondary, phone_port=proxy.SECONDARY_PHONE_PORT, health=None))
        if args.check:
            for route in routes:
                try:
                    route["health"] = proxy.check_health(route["local_port"])
                except BridgeError as error:
                    route["error"] = str(error)
        health = routes[0]["health"]
        checked = args.check and all(route["health"] is not None for route in routes)
        next_step = f"python -m tools.demo --upstream http://127.0.0.1:{args.local_port}"
        if secondary is not None:
            next_step += f" --bonded-upstream http://127.0.0.1:{secondary}"
        next_step += " --quest"
        report = dict(usb_only_requested=True, loopback="127.0.0.1", local_port=args.local_port,
                      phone_port=proxy.PHONE_PORT, device_selection="explicit" if args.device else "sole_usb_device",
                      health=health, health_forwarding_observed=checked, dual_route=args.dual_route,
                      secondary_local_port=secondary, routes=routes,
                      native_bridge_hops_independently_verified=False, check_stops_proxy=args.check,
                      instruction=("Restart without --check using the same device selection and keep this helper running before the next step"
                                   if args.check else "Keep this helper running while the operator relay is used"),
                      limitations="Does not qualify cellular/carrier path, iOS listener accessibility beyond this probe, throughput, headset latency, or physical driving",
                      next_step=next_step)
        print(json.dumps(report, indent=2), flush=True)
        if args.check:
            return 0 if checked else 1
        while True:
            proxy.ensure_running()
            time.sleep(.2)
    except KeyboardInterrupt:
        return 0
    except (BridgeError, OSError) as error:
        # Do not allow raw subprocess argv (which includes the identifier) or
        # errors from native tools to enter terminal output.
        print(str(error) if isinstance(error, BridgeError) else "USB proxy could not start or stop (system error)", file=sys.stderr)
        return 1
    finally:
        if proxy is not None:
            proxy.close()
        if previous is not None:
            signal.signal(signal.SIGTERM, previous)


if __name__ == "__main__":
    raise SystemExit(main())
