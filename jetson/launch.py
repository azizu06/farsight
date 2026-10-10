"""Start the rover server from FARSIGHT_* environment variables (see jetson/farsight.env.example).

Used by jetson/farsight.service. Waits briefly for the camera and Uno device nodes,
then execs `python -m jetson.mapping.server`. A device that never appears is logged and
the server still starts, so /health reports the fault instead of the unit crash-looping.
"""

import os
import sys
import time

VALUE_FLAGS = (
    ("FARSIGHT_CAMERA", "--camera"), ("FARSIGHT_SERIAL_PORT", "--serial-port"),
    ("FARSIGHT_DETECTOR", "--detector"), ("FARSIGHT_DETECTOR_MODEL", "--detector-model"),
    ("FARSIGHT_MISSION_DIR", "--mission-dir"), ("FARSIGHT_SSL_CERTFILE", "--ssl-certfile"),
    ("FARSIGHT_SSL_KEYFILE", "--ssl-keyfile"), ("FARSIGHT_MAX_PWM", "--max-pwm"),
    ("FARSIGHT_MIN_PWM", "--min-pwm"),
    ("FARSIGHT_ASSISTANT_URL", "--assistant-url"), ("FARSIGHT_WHISPER_URL", "--whisper-url"),
    ("FARSIGHT_WHISPER_BIN", "--whisper-bin"), ("FARSIGHT_WHISPER_MODEL", "--whisper-model"),
    ("FARSIGHT_PIPER_BIN", "--piper-bin"), ("FARSIGHT_PIPER_MODEL", "--piper-model"),
    ("FARSIGHT_TTS_DIR", "--tts-dir"), ("FARSIGHT_PIPER_MODEL_ES", "--piper-model-es"),
    ("FARSIGHT_SPEAKER_CMD", "--speaker-cmd"),
)
INT_VARS = ("FARSIGHT_PORT", "FARSIGHT_MAX_PWM", "FARSIGHT_MIN_PWM", "FARSIGHT_WAIT_SECONDS")


def _get(env, name):
    return (env.get(name) or "").strip()


def build_args(env):
    for name in INT_VARS:
        if _get(env, name):
            try:
                int(_get(env, name))
            except ValueError:
                raise ValueError(f"{name} must be an integer, got {_get(env, name)!r}")
    if bool(_get(env, "FARSIGHT_SSL_CERTFILE")) != bool(_get(env, "FARSIGHT_SSL_KEYFILE")):
        raise ValueError("FARSIGHT_SSL_CERTFILE and FARSIGHT_SSL_KEYFILE must be set together")
    args = ["--host", _get(env, "FARSIGHT_HOST") or "0.0.0.0", "--port", _get(env, "FARSIGHT_PORT") or "8443"]
    for name, flag in VALUE_FLAGS:
        if _get(env, name):
            args += [flag, _get(env, name)]
    if _get(env, "FARSIGHT_H264").lower() in ("1", "true", "yes", "on"):
        args.append("--h264")
    return args


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


def main(env=None):
    env = os.environ if env is None else env
    try:
        args = build_args(env)
    except ValueError as exc:
        sys.exit(f"farsight launch: {exc}")
    timeout = int(_get(env, "FARSIGHT_WAIT_SECONDS") or 30)
    missing = wait_for_devices(device_paths(env), timeout)
    if missing:
        print(f"farsight launch: still missing after {timeout}s: {', '.join(missing)}; starting degraded", flush=True)
    print("farsight launch: server " + " ".join(args), flush=True)
    os.execv(sys.executable, [sys.executable, "-m", "jetson.mapping.server", *args])


if __name__ == "__main__":
    main()
