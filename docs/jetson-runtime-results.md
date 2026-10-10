# Physical Jetson application setup — October 10, 2026

Godlydonuts’s Codex agent provisioned the recovered Orin Nano over USB SSH.
OS and USB recovery evidence is in `jetson-recovery-results.md`.

## Verified on the board

- Source deployed to `/home/ubuntu/farsight`; Python 3.12 virtual environment
  includes system packages so NVIDIA TensorRT bindings remain accessible.
- ARM64 runtime wheels installed from `/home/ubuntu/farsight-wheelhouse` with
  `--no-index`. Imports passed for OpenCV 4.14, FastAPI 0.143, ONNX Runtime
  1.31, NumPy 2.5.3 and aiohttp. Models copied separately: detection, pose,
  UltraFace, SmolVLM plus projector, Whisper tiny.en and Piper low voice.
- `farsight.service` is enabled and serves HTTP on USB address
  `http://192.168.55.1:8443`. Configuration is `/etc/farsight.env`, with phone
  input, physical serial unset, pose ONNX and H.264 enabled. TLS is omitted
  for this USB bring-up; operator relay provides the Quest-facing origin.
- `/health` loaded pose ONNX without detector error and reported camera
  `WAITING`, rover disabled, estop true and release required. No camera,
  `/dev/ttyACM*` or `/dev/ttyUSB*` device was present during this check.
- Added `ubuntu` to `dialout` for subsequent Arduino attachment; systemd
  already includes the group explicitly.
- A static public photo sent over USB through `/phone/rgb` produced pose
  inference and H.264 initialization plus a requested media segment (10,818
  binary bytes). This exercised the real board's phone-protocol/encoder path,
  not actual iPhone capture. After disconnect, camera freshness expired,
  rover remained disabled and estop remained true.
- Native ARM64 Whisper CPU binaries were compiled on the board. Piper
  generated a 1.952-second, 16 kHz mono WAV; Whisper tiny.en transcribed
  “foresight is ready for the rover test.” from the generated speech.
  This is a local synthesis/transcription check, not microphone qualification.
- CUDA 13.2 runtime initialized device 0; TensorRT 10.16.2.10 imported.
  NVCC 13.2 and cuBLAS development libraries are installed for native GPU
  voice builds. Engine builds and those builds are still separate checks.
- Tailscale 1.104.1 and `iproxy` are installed. Tailscale used the official
  [Ubuntu Noble signed package source](https://pkgs.tailscale.com/stable/#ubuntu-noble).
  Tailnet authentication and phone cellular transport remain pending.

## Software H.264 repair

The NVIDIA `ffmpeg` 8.0.1 package disables GPL encoders, including `libx264`.
The Orin Nano demo needs software encoding. Ubuntu FFmpeg 6.1.1 and its
versioned libav libraries were downloaded as packages and extracted into
`/home/ubuntu/farsight-ffmpeg`, preserving NVIDIA's installed libraries.

`/home/ubuntu/.local/bin/ffmpeg` sets `LD_LIBRARY_PATH` to that extracted
library directory and executes its FFmpeg binary. The systemd drop-in
`/etc/systemd/system/farsight.service.d/ffmpeg.conf` puts that wrapper first
on the service's PATH. A real 640×480, 10 FPS red-frame libx264 encode
produced `/tmp/farsight-encoder-smoke.mp4` successfully. This qualifies the
encoder, not phone capture or Quest playback.

## Installation transport and remaining qualification

An SSH remote SOCKS proxy bound only to Jetson loopback port 18083 routes
installation downloads through the Mac's internet connection. APT calls
explicitly select that proxy; the application does not depend on it.
Ubuntu's empty sources file was restored from its installer backup and the
stale installation-media source disabled. No distribution upgrade or
autoremove was run.

TensorRT/CUDA installation and target-built engine execution are separate
checks. Phone signing/trust/capture, cellular routing, Quest USB/browser,
actual motor direction/PWM, watchdog disconnects and power under load must
still be verified on the complete rig. System-site `pip check` also exposes
the preinstalled SciPy's NumPy constraint and missing cffi for PyNaCl;
neither package is used by the verified service startup.
