"""Read-only local inventory for the Farsight hardware handoff.

python -m tools.preflight --role operator > /tmp/farsight-preflight.json
No installs, sudo, host scans, serial-port opens, flashing, or network changes.
Exit zero means the report was collected, never that the physical rig is qualified.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import glob
import hashlib
import json
from pathlib import Path
import platform
import re
import shutil
import socket
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
COMMAND_TIMEOUT = 1.5
MAX_MANIFEST_BYTES = 65536


def command(args, timeout=COMMAND_TIMEOUT, cwd=None):
    """Arguments stay an argv list. External commands cannot prompt indefinitely."""
    try:
        result = subprocess.run(args, cwd=cwd, stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=timeout, check=False)
        return dict(state="ok" if result.returncode == 0 else "error",
                    returncode=result.returncode, stdout=result.stdout[:65536], stderr=result.stderr[:1000])
    except subprocess.TimeoutExpired:
        return dict(state="timeout", returncode=None, stdout="", stderr="")
    except OSError as exc:
        return dict(state="unavailable", returncode=None, stdout="", stderr=type(exc).__name__)


def result(status, detail, action=None, **facts):
    report = dict(status=status, detail=detail, **facts)
    if action:
        report["action"] = action
    return report


def host_info():
    model = None
    release = None
    try:
        model = Path("/proc/device-tree/model").read_text().strip("\0\n ")
    except OSError:
        pass
    try:
        release = Path("/etc/nv_tegra_release").read_text().strip()[:1000]
    except OSError:
        pass
    return dict(system=platform.system(), machine=platform.machine(), python=sys.executable,
                python_version=platform.python_version(), jetson_detected=bool(
                    release or (model and ("jetson" in model.lower() or "tegra" in model.lower()))),
                device_model=model, l4t_release=release)


def version_in_range(version, specification):
    """Current repo requirements use numeric >=/< bounds; unknown syntax stays unknown."""
    if not isinstance(version, str) or not specification:
        return None
    def numeric(value):
        matched = re.match(r"^(\d+(?:\.\d+)*)", value)
        if not matched:
            return None
        parts = tuple(int(item) for item in matched.group(1).split("."))
        return parts + (0,) * max(0, 4 - len(parts))
    actual = numeric(version)
    if actual is None:
        return None
    for clause in specification.split(","):
        matched = re.fullmatch(r"(>=|<=|==|>|<)\s*([\d.]+)", clause.strip())
        if not matched:
            return None
        operation, expected = matched.groups()
        target = numeric(expected)
        if not {">=": actual >= target, "<=": actual <= target, "==": actual == target,
                ">": actual > target, "<": actual < target}[operation]:
            return False
    return True


def requirement_specs(root):
    specs = {}
    for relative in ("jetson/mapping/requirements.txt", "jetson/control/requirements.txt", "tools/requirements.txt"):
        try:
            text = (Path(root) / relative).read_text()
        except OSError:
            continue
        for line in text.splitlines():
            matched = re.fullmatch(r"\s*([A-Za-z0-9_.-]+)(?:\[[^\]]+\])?\s*((?:>=|<=|==|>|<).+?)\s*", line)
            if matched:
                specs[matched.group(1).lower()] = matched.group(2)
    return specs


def python_dependencies(role, root=ROOT):
    wanted = {"numpy": "numpy", "cv2": "opencv-python-headless", "fastapi": "fastapi",
              "uvicorn": "uvicorn", "httpx": "httpx", "multipart": "python-multipart"}
    if role in ("operator", "both"):
        wanted["aiohttp"] = "aiohttp"
    if role in ("rover", "both"):
        wanted["serial"] = "pyserial"
    probe = """import importlib,importlib.metadata,json
names=json.loads(__import__('sys').argv[1]); out={}
for name,distribution in names.items():
 try:
  module=importlib.import_module(name)
  version=getattr(module,'__version__',None)
  if version is None:
   try: version=importlib.metadata.version(distribution)
   except importlib.metadata.PackageNotFoundError: pass
  out[name]={'installed':True,'version':version}
 except Exception as exc:
  out[name]={'installed':False,'version':None,'error':type(exc).__name__+': '+str(exc)[:200]}
print(json.dumps(out))
"""
    ran = command([sys.executable, "-c", probe, json.dumps(wanted)], timeout=3)
    if ran["state"] != "ok":
        return result("unknown", "Python import probe did not finish", "Run in the intended venv and inspect import errors",
                      probe_state=ran["state"], modules=None)
    try:
        modules = json.loads(ran["stdout"])
    except ValueError:
        return result("unknown", "Python import probe returned invalid output", modules=None)
    missing = [wanted[name] for name, value in modules.items() if not value["installed"]]
    specs = requirement_specs(root)
    incompatible, unknown = [], []
    for name, value in modules.items():
        specification = specs.get(wanted[name].lower())
        value["required"] = specification
        value["version_in_range"] = version_in_range(value.get("version"), specification)
        if value["installed"] and value["version_in_range"] is False:
            incompatible.append(wanted[name])
        elif value["installed"] and specification and value["version_in_range"] is None:
            unknown.append(wanted[name])
    failed = missing or incompatible
    return result("fail" if failed else "unknown" if unknown else "ok",
                  "Missing or out-of-range runtime dependencies" if failed else "Dependency versions are unknown" if unknown else "Runtime imports and declared version bounds succeeded",
                  "Use a dedicated environment matching the documented requirements; this helper never alters the current environment" if failed or unknown else None,
                  modules=modules, missing_packages=missing, incompatible_packages=incompatible,
                  unknown_version_packages=unknown, interpreter=sys.executable)


def ffmpeg_inventory():
    path = shutil.which("ffmpeg")
    if not path:
        return result("fail", "FFmpeg is not installed on PATH", "Install FFmpeg before enabling --h264", installed=False,
                      libx264_available=None, version=None)
    encoders = command([path, "-hide_banner", "-encoders"])
    version = command([path, "-version"])
    available = bool(re.search(r"\blibx264\b", encoders["stdout"])) if encoders["state"] == "ok" else None
    return result("ok" if available else "fail" if available is False else "unknown",
                  "FFmpeg libx264 encoder is available" if available else "libx264 was not confirmed",
                  "Use an FFmpeg build with libx264; the rover encoder requests that encoder explicitly" if not available else None,
                  installed=True, path=path, version=version["stdout"].splitlines()[0] if version["stdout"] else None,
                  libx264_available=available, encoder_probe_state=encoders["state"],
                  stream_tested=False)


def gpu_inventory(host, role):
    if role == "operator" and not host["jetson_detected"]:
        return result("skipped", "Jetson CUDA/TensorRT checks do not apply to the operator laptop",
                      cuda_version=None, tensorrt_version=None, gpu_device_count=None)
    script = """import ctypes,ctypes.util,json
out={'version':None,'cuda_runtime_version':None}
try:
 import tensorrt
 out['version']=tensorrt.__version__
except Exception as exc: out['tensorrt_error']=type(exc).__name__
for candidate in [ctypes.util.find_library('cudart'),'libcudart.so.13','libcudart.so.12','libcudart.so','/usr/local/cuda/lib64/libcudart.so']:
 if not candidate: continue
 try:
  library=ctypes.CDLL(candidate)
  query=library.cudaRuntimeGetVersion;query.argtypes=[ctypes.POINTER(ctypes.c_int)];query.restype=ctypes.c_int
  value=ctypes.c_int()
  if query(ctypes.byref(value))==0 and value.value>=1000:
   out['cuda_runtime_version']=str(value.value//1000)+'.'+str(value.value%1000//10)
   break
 except (OSError,AttributeError): pass
print(json.dumps(out))
"""
    probe = command([sys.executable, "-c", script], timeout=2)
    version, runtime_version = None, None
    if probe["state"] == "ok":
        try:
            parsed = json.loads(probe["stdout"])
            version = parsed["version"]
            runtime_version = parsed.get("cuda_runtime_version")
        except (ValueError, KeyError, TypeError):
            pass
    nvcc = shutil.which("nvcc")
    if not nvcc and Path("/usr/local/cuda/bin/nvcc").is_file():
        nvcc = "/usr/local/cuda/bin/nvcc"
    compiler = command([nvcc, "--version"]) if nvcc else None
    cuda_version = None
    if compiler and compiler["state"] == "ok":
        match = re.search(r"release\s+([\d.]+)", compiler["stdout"])
        cuda_version = match.group(1) if match else None
    packages = None
    dpkg = shutil.which("dpkg-query")
    if dpkg:
        query = command([dpkg, "-W", "-f=${Package}\t${Version}\n", "nvidia-l4t-core", "nvidia-jetpack", "python3-libnvinfer"])
        packages = dict(line.split("\t", 1) for line in query["stdout"].splitlines() if "\t" in line)
    status = "ok" if version and (runtime_version or cuda_version) else "fail" if host["jetson_detected"] else "unknown"
    return result(status, "CUDA runtime/compiler and TensorRT import versions inventoried; GPU execution is untested",
                  "Use JetPack system Python or a --system-site-packages venv; inspect /etc/nv_tegra_release before selecting an engine" if not version else None,
                  cuda_version=runtime_version or cuda_version, cuda_runtime_version=runtime_version,
                  cuda_compiler_version=cuda_version, nvcc_path=nvcc, tensorrt_version=version,
                  tensorrt_probe_state=probe["state"], packages=packages,
                  gpu_device_count=None, engine_execution_verified=False)


def camera_inventory(host):
    nodes = sorted(glob.glob("/dev/video*"))
    v4l2 = shutil.which("v4l2-ctl")
    listing = command([v4l2, "--list-devices"]) if v4l2 else None
    return result("warn" if nodes or host["system"] == "Darwin" else "unknown",
                  "Camera device nodes enumerated; no frame capture was attempted" if nodes else
                  "No Linux camera nodes visible; a native iPhone stream is a separate source",
                  "For USB UVC, inspect v4l2-ctl --list-devices; for iPhone, install ios/FarsightCapture and verify /health camera_source",
                  device_nodes=nodes, v4l2_installed=bool(v4l2),
                  v4l2_probe_state=listing["state"] if listing else None,
                  v4l2_devices=listing["stdout"].strip()[:8000] if listing and listing["state"] == "ok" else None,
                  frames_verified=False)


def serial_inventory():
    candidates = sorted(set(path for pattern in ("/dev/ttyACM*", "/dev/ttyUSB*", "/dev/cu.usbmodem*", "/dev/cu.usbserial*")
                            for path in glob.glob(pattern)))
    return result("warn", "Serial paths are candidates only; firmware and watchdog remain unverified",
                  "Lift the wheels and perform rover/README.md handshake, direction, range, deadman, and disconnect checks",
                  candidate_ports=candidates, serial_port_opened=False,
                  firmware_verification="not_checked", auto_flash=False)


def parse_network_services(text):
    return [dict(hardware_port=port.strip(), interface=device.strip())
            for port, device in re.findall(r"\(Hardware Port:\s*([^,]+), Device:\s*([^\)]+)\)", text)]


def network_inventory(host):
    if host["system"] == "Darwin":
        setup = shutil.which("networksetup")
        ifconfig = shutil.which("ifconfig")
        order = command([setup, "-listnetworkserviceorder"]) if setup else None
        links = command([ifconfig, "-a"]) if ifconfig else None
        services = parse_network_services(order["stdout"]) if order and order["state"] == "ok" else []
        usb = [service for service in services if "iphone" in service["hardware_port"].lower()
               or "usb" in service["hardware_port"].lower()]
        active = []
        if links and links["state"] == "ok":
            current = None
            for line in links["stdout"].splitlines():
                match = re.match(r"^(\S+):", line)
                if match:
                    current = match.group(1)
                inet = re.search(r"\binet\s+([\d.]+)", line)
                if inet and current:
                    active.append(dict(interface=current, ipv4=inet.group(1)))
        return result("warn", "Local macOS network services and addresses inventoried; USB routing is not qualified",
                      "Confirm iPhone USB is connected in Network settings, disable venue Wi-Fi manually, and run the link qualifier",
                      interfaces=active, possible_usb_services=usb, internet_tested=False, routes_tested=False)
    ip = shutil.which("ip")
    if not ip:
        return result("unknown", "ip command unavailable; USB interfaces/routes are unknown",
                      "Inspect ip -br address and ip route on the Jetson", interfaces=None, routes=None)
    addresses, routes = command([ip, "-j", "address", "show"]), command([ip, "-j", "route", "show"])
    try:
        interfaces = json.loads(addresses["stdout"]) if addresses["state"] == "ok" else None
        route_data = json.loads(routes["stdout"]) if routes["state"] == "ok" else None
    except ValueError:
        interfaces = route_data = None
    usb = []
    for item in interfaces or []:
        name = item.get("ifname", "")
        driver_path = Path("/sys/class/net") / name / "device/driver"
        driver = driver_path.resolve().name if driver_path.is_symlink() else None
        if driver in ("ipheth", "rndis_host", "cdc_ether", "cdc_ncm") or name.startswith("usb"):
            usb.append(dict(interface=name, driver=driver, addresses=item.get("addr_info", []), state=item.get("operstate")))
    return result("warn" if interfaces is not None else "unknown",
                  "Local interfaces/routes inventoried; no hosts were probed",
                  "Confirm the rover iPhone USB route and use the Jetson's USB interface IP in the capture app; the phone gateway IP is different",
                  interfaces=interfaces, routes=route_data, possible_usb_interfaces=usb,
                  internet_tested=False, routes_tested=False)


def tailscale_inventory():
    path = shutil.which("tailscale")
    gui = Path("/Applications/Tailscale.app").is_dir()
    if not path:
        return result("fail", "Tailscale CLI is absent; GUI presence does not establish authentication",
                      "Install/configure the documented Tailscale client and authenticate both computers before cellular rehearsal",
                      cli_installed=False, gui_app_present=gui, authenticated=None, backend_state=None)
    ran = command([path, "status", "--json"])
    try:
        state = json.loads(ran["stdout"]) if ran["state"] == "ok" else None
    except ValueError:
        state = None
    if not isinstance(state, dict):
        return result("unknown", "Tailscale CLI exists but backend/authentication state was not obtained",
                      "Inspect tailscale status manually; this helper never runs tailscale up or logs in",
                      cli_installed=True, path=path, authenticated=None, backend_state=None, probe_state=ran["state"])
    backend = state.get("BackendState")
    authenticated = True if backend == "Running" else False if backend in ("NeedsLogin", "NeedsMachineAuth", "NoState") else None
    return result("ok" if authenticated else "fail" if authenticated is False else "unknown",
                  "Tailscale backend state reported; peer reachability and direct/relay path are untested",
                  "Authenticate or approve the client manually, then qualify the configured Jetson connection" if not authenticated else None,
                  cli_installed=True, path=path, authenticated=authenticated, backend_state=backend,
                  self_online=(state.get("Self") or {}).get("Online"), local_tailnet_ips=state.get("TailscaleIPs"),
                  peers_tested=False)


def parse_adb_devices(text):
    devices = []
    for line in text.splitlines():
        if "\t" not in line:
            continue
        serial, tail = line.split("\t", 1)
        fields = tail.split()
        if fields:
            devices.append(dict(serial=serial, state=fields[0], authorized=fields[0] == "device",
                                detail=" ".join(fields[1:])))
    return devices


def adb_devices_read_only():
    """Query an already-running local ADB server; never autostart its daemon."""
    def receive(sock, count):
        data = bytearray()
        while len(data) < count:
            chunk = sock.recv(count - len(data))
            if not chunk:
                raise OSError("ADB server closed its response")
            data.extend(chunk)
        return bytes(data)
    try:
        with socket.create_connection(("127.0.0.1", 5037), timeout=.5) as sock:
            sock.settimeout(1)
            service = b"host:devices-l"
            sock.sendall(f"{len(service):04x}".encode("ascii") + service)
            if receive(sock, 4) != b"OKAY":
                return None, "server_rejected_query"
            size = int(receive(sock, 4), 16)
            if size > 65536:
                return None, "excessive_response"
            return parse_adb_devices(receive(sock, size).decode("utf-8", errors="replace")), "ok"
    except (OSError, ValueError):
        return None, "server_not_available"


def adb_inventory():
    path = shutil.which("adb")
    if not path:
        return result("fail", "ADB CLI is absent", "Install Android Platform Tools and enable Quest Developer Mode before the rehearsal",
                      installed=False, devices=None, authorized_devices=None)
    version = command([path, "version"])
    devices, state = adb_devices_read_only()
    authorized = [device for device in devices or [] if device["authorized"]]
    return result("ok" if authorized else "warn" if devices is not None else "unknown",
                  "Authorized ADB devices enumerated; device identity and WebXR still require physical verification" if authorized else
                  "No authorized Quest connection was confirmed",
                  "Connect/unlock the Quest and approve USB debugging. If ADB server is stopped, manually run adb devices, then rerun preflight" if not authorized else None,
                  installed=True, path=path, version=version["stdout"].strip()[:500],
                  server_probe_state=state, devices=devices, authorized_devices=len(authorized) if devices is not None else None,
                  quest_identity_verified=False, reverse_configured_by_helper=False)


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def model_inventory(directory):
    directory = Path(directory)
    manifests, covered = [], set()
    for path in sorted(directory.glob("*.manifest.json")):
        entry = dict(manifest=path.name, status="fail")
        try:
            if path.stat().st_size > MAX_MANIFEST_BYTES:
                raise ValueError("Manifest exceeds 64 KiB")
            metadata = json.loads(path.read_text())
            name, expected_hash, expected_bytes = metadata["model"], metadata["sha256"], metadata["bytes"]
            if (not isinstance(name, str) or Path(name).name != name or name in (".", "..")
                    or not isinstance(expected_hash, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", expected_hash)
                    or type(expected_bytes) is not int or expected_bytes <= 0):
                raise ValueError("Manifest filename, size, or SHA-256 is invalid")
            model = directory / name
            if model.resolve().parent != directory.resolve():
                raise ValueError("Model path escapes its folder")
            covered.add(name)
            entry.update(model=name, expected_bytes=expected_bytes, expected_sha256=expected_hash.lower(),
                         license=metadata.get("license"), source=metadata.get("source"))
            if not model.is_file():
                entry.update(detail="Manifest model file is missing", bytes=None, sha256=None)
            else:
                size = model.stat().st_size
                digest = sha256_file(model)
                verified = size == expected_bytes and digest == expected_hash.lower()
                entry.update(status="ok" if verified else "fail", bytes=size, sha256=digest,
                             detail="SHA-256 and size match" if verified else "SHA-256 or size mismatch")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            entry["detail"] = str(exc)[:250]
        manifests.append(entry)
    other = [dict(file=path.name, bytes=path.stat().st_size, sha256=None, integrity_verified=None)
             for path in sorted(directory.glob("*")) if path.is_file() and path.name not in covered
             and path.suffix.lower() in (".onnx", ".gguf", ".bin", ".engine", ".plan", ".pt")]
    bad = [item for item in manifests if item["status"] != "ok"]
    return result("fail" if bad else "ok" if manifests else "unknown",
                  "Prepared model manifests checked against local files; engines are not executed",
                  "Copy matching model/manifest pairs before going offline; build and smoke-test TensorRT engines on the target Jetson" if bad or not manifests else None,
                  directory=str(directory), manifests=manifests, unmanifested_files=other,
                  engine_files=[item["file"] for item in other if item["file"].endswith((".engine", ".plan"))],
                  unmanifested_files_verified=False, gpu_models_qualified=False)


def server_cli_inventory(root):
    ran = command([sys.executable, "-m", "jetson.server", "--help"], timeout=3, cwd=root)
    flags = ("--serial-port", "--detector", "--h264", "--camera", "--min-pwm")
    valid = ran["state"] == "ok" and all(flag in ran["stdout"] for flag in flags)
    return result("ok" if valid else "fail", "Canonical rover CLI checked without starting its lifespan",
                  "Use the repository venv and resolve imports before starting python -m jetson.server" if not valid else None,
                  canonical_module="jetson.server", legacy_module="jetson.mapping.server",
                  command=[sys.executable, "-m", "jetson.server", "--help"], probe_state=ran["state"],
                  required_flags_present=valid, started_server=False)


def collect(root=ROOT, role="auto"):
    started = time.monotonic()
    host = host_info()
    if role == "auto":
        role = "rover" if host["jetson_detected"] else "operator" if host["system"] == "Darwin" else "both"
    jobs = dict(python=lambda: python_dependencies(role, root), ffmpeg=ffmpeg_inventory,
                gpu=lambda: gpu_inventory(host, role), camera=lambda: camera_inventory(host),
                serial=serial_inventory, network=lambda: network_inventory(host),
                tailscale=tailscale_inventory, adb=adb_inventory,
                models=lambda: model_inventory(Path(root) / "models"), server_cli=lambda: server_cli_inventory(root))
    checks = {}
    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        pending = {pool.submit(work): name for name, work in jobs.items()}
        for future in as_completed(pending):
            name = pending[future]
            try:
                checks[name] = future.result()
            except Exception as exc:
                checks[name] = result("unknown", f"Probe failed: {type(exc).__name__}", "Inspect this subsystem manually")
    issues = [dict(check=name, severity=check["status"], detail=check["detail"], action=check.get("action"))
              for name, check in sorted(checks.items()) if check["status"] in ("fail", "warn", "unknown")]
    return dict(generated_at_utc=datetime.now(timezone.utc).isoformat(), duration_ms=round((time.monotonic() - started) * 1000),
                role=role, host=host, checks=dict(sorted(checks.items())), issues=issues,
                physical_qualification=dict(complete=False, reason="Local inventory does not qualify the real demo",
                    pending=["Uno firmware/watchdog and motor polarity", "camera frames and sensor alignment",
                             "phone USB routes and cellular capacity", "Quest identity, ADB reverse, and immersive WebXR",
                             "displayed video age, loaded inference, and physical stopping times", "battery, power, and thermal stability"]),
                launch_examples=dict(rover="python -m jetson.server --host 0.0.0.0 --port 8443 --h264",
                    operator="python -m tools.demo --upstream http://JETSON_TAILNET_IP:8443 --quest",
                    software_rehearsal="python -m tools.demo --simulate --h264",
                    qualifier="python -m tools.qualify_link --seconds 30 --output /tmp/farsight-link.json"))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--role", choices=("auto", "operator", "rover", "both"), default="auto")
    parser.add_argument("--strict", action="store_true", help="Exit 1 for failed/unknown local checks; still never proves physical qualification")
    args = parser.parse_args()
    report = collect(role=args.role)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 1 if args.strict and any(check["status"] in ("fail", "unknown") for check in report["checks"].values()) else 0


if __name__ == "__main__":
    sys.exit(main())
