"""Start the rover server from FARSIGHT_* environment variables (see jetson/farsight.env.example).

Used by jetson/farsight.service. Waits briefly for the camera and Uno device nodes,
then execs `python -m jetson.server`. A device that never appears is logged and
the server still starts, so /health reports the fault instead of the unit crash-looping.

Dry run, no server started (exit 1 when a configured file or device is missing):
    jetson/.venv/bin/python -m jetson.launch --check --env-file /etc/farsight.env
"""

import argparse
import math
import os
import sys
import time

VALUE_FLAGS = (
    ("FARSIGHT_CAMERA", "--camera"), ("FARSIGHT_SERIAL_PORT", "--serial-port"),
    ("FARSIGHT_DETECTOR", "--detector"), ("FARSIGHT_DETECTOR_MODEL", "--detector-model"),
    ("FARSIGHT_MISSION_DIR", "--mission-dir"), ("FARSIGHT_SSL_CERTFILE", "--ssl-certfile"),
    ("FARSIGHT_SSL_KEYFILE", "--ssl-keyfile"), ("FARSIGHT_MAX_PWM", "--max-pwm"),
    ("FARSIGHT_MIN_PWM", "--min-pwm"), ("FARSIGHT_AUTONOMY_MAX_PWM", "--autonomy-max-pwm"),
    ("FARSIGHT_DRIVE_PROFILE", "--drive-profile"),
    ("FARSIGHT_ASSISTANT_URL", "--assistant-url"), ("FARSIGHT_WHISPER_URL", "--whisper-url"),
    ("FARSIGHT_WHISPER_BIN", "--whisper-bin"), ("FARSIGHT_WHISPER_MODEL", "--whisper-model"),
    ("FARSIGHT_PIPER_BIN", "--piper-bin"), ("FARSIGHT_PIPER_MODEL", "--piper-model"),
    ("FARSIGHT_TTS_DIR", "--tts-dir"), ("FARSIGHT_PIPER_MODEL_ES", "--piper-model-es"),
    ("FARSIGHT_SPEAKER_CMD", "--speaker-cmd"), ("FARSIGHT_FACE_MODEL", "--face-model"),
    ("FARSIGHT_VIDEO_FPS", "--video-fps"), ("FARSIGHT_VIDEO_BITRATE", "--video-bitrate"),
    ("FARSIGHT_BULK_LIMIT_MBPS", "--bulk-limit-mbps"), ("FARSIGHT_VIDEO_LIMIT_MBPS", "--video-limit-mbps"),
    ("FARSIGHT_TRIAGE_INTERVAL", "--triage-interval"), ("FARSIGHT_DEPTH_MODEL", "--depth-model"),
    ("FARSIGHT_SCAN_FOV", "--scan-fov"), ("FARSIGHT_SCAN_TURN_DPS", "--scan-turn-dps"),
    ("FARSIGHT_PROXIMITY_STOP", "--proximity-stop"), ("FARSIGHT_PAN_MOUNT", "--pan-mount"),
    ("FARSIGHT_NAV_SPEED_MPS", "--nav-speed-mps"), ("FARSIGHT_NAV_TURN_RADPS", "--nav-turn-radps"),
    ("FARSIGHT_NAV_MAX_V", "--nav-max-v"), ("FARSIGHT_NAV_MAX_W", "--nav-max-w"),
    ("FARSIGHT_NAV_CAMERA_HEIGHT", "--nav-camera-height"),
    ("FARSIGHT_MAP_POINTS_CAP", "--map-points-cap"), ("FARSIGHT_MAP_TRIANGLES_CAP", "--map-triangles-cap"),
)
BOOL_FLAGS = (("FARSIGHT_H264", "--h264"), ("FARSIGHT_FUSION_BYPASS", "--fusion-bypass"), ("FARSIGHT_SIMULATE_ROVER", "--simulate-rover"),
              ("FARSIGHT_SCAN_PAN", "--scan-pan"), ("FARSIGHT_RECORD", "--record"), ("FARSIGHT_NAV2", "--nav2"),
              ("FARSIGHT_ALLOW_CALIBRATION", "--allow-calibration"), ("FARSIGHT_DEMO_MODE", "--demo-mode"), ("FARSIGHT_LIDAR_DRIVE", "--lidar-drive"))
INT_VARS = ("FARSIGHT_PORT", "FARSIGHT_MAX_PWM", "FARSIGHT_MIN_PWM", "FARSIGHT_AUTONOMY_MAX_PWM", "FARSIGHT_WAIT_SECONDS",
            "FARSIGHT_VIDEO_FPS", "FARSIGHT_VIDEO_BITRATE", "FARSIGHT_MAP_POINTS_CAP", "FARSIGHT_MAP_TRIANGLES_CAP")
FLOAT_VARS = ("FARSIGHT_TRIAGE_INTERVAL", "FARSIGHT_SCAN_FOV", "FARSIGHT_SCAN_TURN_DPS",
              "FARSIGHT_BULK_LIMIT_MBPS", "FARSIGHT_VIDEO_LIMIT_MBPS", "FARSIGHT_NAV_SPEED_MPS", "FARSIGHT_NAV_TURN_RADPS",
              "FARSIGHT_NAV_MAX_V", "FARSIGHT_NAV_MAX_W", "FARSIGHT_NAV_CAMERA_HEIGHT")
# Variables whose value is a file that must exist for the feature to work.
FILE_VARS = ("FARSIGHT_DETECTOR_MODEL", "FARSIGHT_FACE_MODEL", "FARSIGHT_PIPER_MODEL", "FARSIGHT_PIPER_MODEL_ES",
             "FARSIGHT_WHISPER_MODEL", "FARSIGHT_DEPTH_MODEL", "FARSIGHT_SSL_CERTFILE", "FARSIGHT_SSL_KEYFILE",
             "FARSIGHT_DRIVE_PROFILE")


def _get(env, name):
    return (env.get(name) or "").strip()


def _truthy(env, name):
    return _get(env, name).lower() in ("1", "true", "yes", "on")


def read_env_file(path):
    """Parse a systemd EnvironmentFile (KEY=VALUE, optional quotes, # comments) for manual dry runs."""
    values = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip("\"'")
    return values


def build_args(env):
    for names, kind in ((INT_VARS, int), (FLOAT_VARS, float)):
        for name in names:
            if _get(env, name):
                try:
                    kind(_get(env, name))
                except ValueError:
                    raise ValueError(f"{name} must be {'an integer' if kind is int else 'a number'}, got {_get(env, name)!r}")
    if _get(env, "FARSIGHT_BULK_LIMIT_MBPS"):
        bulk_limit = float(_get(env, "FARSIGHT_BULK_LIMIT_MBPS"))
        if not math.isfinite(bulk_limit) or bulk_limit < 0:
            raise ValueError("FARSIGHT_BULK_LIMIT_MBPS must be finite and nonnegative; 0 disables pacing")
    if _truthy(env, "FARSIGHT_SIMULATE_ROVER") and _get(env, "FARSIGHT_SERIAL_PORT"):
        raise ValueError("FARSIGHT_SIMULATE_ROVER cannot be combined with FARSIGHT_SERIAL_PORT")
    if bool(_get(env, "FARSIGHT_SSL_CERTFILE")) != bool(_get(env, "FARSIGHT_SSL_KEYFILE")):
        raise ValueError("FARSIGHT_SSL_CERTFILE and FARSIGHT_SSL_KEYFILE must be set together")
    args = ["--host", _get(env, "FARSIGHT_HOST") or "0.0.0.0", "--port", _get(env, "FARSIGHT_PORT") or "8443"]
    for name, flag in VALUE_FLAGS:
        if _get(env, name):
            args += [flag, _get(env, name)]
    for name, flag in BOOL_FLAGS:
        if _truthy(env, name):
            args.append(flag)
    intrinsics = _get(env, "FARSIGHT_SCAN_INTRINSICS").replace(",", " ").split()
    if intrinsics:
        if len(intrinsics) != 4:
            raise ValueError("FARSIGHT_SCAN_INTRINSICS must be four numbers: FX FY CX CY")
        for value in intrinsics:
            try:
                float(value)
            except ValueError:
                raise ValueError(f"FARSIGHT_SCAN_INTRINSICS must be four numbers, got {value!r}")
        args += ["--scan-intrinsics", *intrinsics]
    return args


def preflight(env, exists=os.path.exists):
    """Problems that would degrade or break the server: missing model/cert files, absent device nodes."""
    problems = []
    for name in FILE_VARS:
        path = _get(env, name)
        if path and not exists(path):
            problems.append(f"{name}={path} does not exist")
    if _get(env, "FARSIGHT_DETECTOR") == "pose" and not _get(env, "FARSIGHT_DETECTOR_MODEL"):
        problems.append("FARSIGHT_DETECTOR=pose needs FARSIGHT_DETECTOR_MODEL (models/yolo11n-pose.engine)")
    for path in device_paths(env):
        if not exists(path):
            problems.append(f"{path} not present (camera unplugged, Uno on another tty, or udev not settled)")
    return problems


def device_paths(env):
    """Device nodes worth waiting for; a bare camera index like '0' has no node to poll."""
    paths = [_get(env, "FARSIGHT_CAMERA"), _get(env, "FARSIGHT_SERIAL_PORT")]
    return [p for p in paths if p.startswith("/dev/")]


def wait_for_devices(paths, timeout, exists=os.path.exists, now=time.monotonic, sleep=time.sleep):
    deadline = now() + timeout
    missing = [p for p in paths if not exists(p)]
    while missing and now() < deadline:
        sleep(0.5)
        missing = [p for p in missing if not exists(p)]
    return missing


def main(argv=None, env=None, out=sys.stdout):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="Print the server command and any missing files/devices, then exit")
    parser.add_argument("--env-file", help="Read FARSIGHT_* values from this file (the service gets them from systemd)")
    args = parser.parse_args(argv)
    env = dict(os.environ if env is None else env)
    if args.env_file:
        env = {**read_env_file(args.env_file), **{k: v for k, v in env.items() if k.startswith("FARSIGHT_") and v}}
    try:
        server_args = build_args(env)
    except ValueError as exc:
        if args.check:
            print(f"farsight launch: {exc}", file=out)
            return 1
        sys.exit(f"farsight launch: {exc}")
    if args.check:
        print("farsight launch: python -m jetson.server " + " ".join(server_args), file=out)
        problems = preflight(env)
        for problem in problems:
            print(f"  PROBLEM {problem}", file=out)
        print("  OK" if not problems else f"  {len(problems)} problem(s): the server would start degraded or fail", file=out)
        return 1 if problems else 0
    timeout = int(_get(env, "FARSIGHT_WAIT_SECONDS") or 30)
    missing = wait_for_devices(device_paths(env), timeout)
    if missing:
        print(f"farsight launch: still missing after {timeout}s: {', '.join(missing)}; starting degraded", flush=True)
    print("farsight launch: server " + " ".join(server_args), flush=True)
    os.execv(sys.executable, [sys.executable, "-m", "jetson.server", *server_args])


if __name__ == "__main__":
    sys.exit(main())
