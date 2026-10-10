# Isolated software FFmpeg on the Orin Nano

The recovered R39.2 board's NVIDIA FFmpeg 8.0.1 package omitted `libx264`.
Godlydonuts's Codex agent already repaired that board and passed a real synthetic
640×480 software encode, as recorded in `jetson-runtime-results.md`. The helper
below makes that **opt-in** repair repeatable. It does not replace NVIDIA's
installed libraries or establish camera/Quest playback or loaded throughput.

From the actual Jetson, inspect the plan first:

```sh
bash jetson/install_software_ffmpeg.sh --dry-run --install-service-path
bash jetson/install_software_ffmpeg.sh --install-service-path
```

Defaults match the verified deployment: private extraction at
`~/farsight-ffmpeg`, wrapper at `~/.local/bin/ffmpeg`, and Ubuntu FFmpeg
`7:6.1.1-3ubuntu5`. The helper downloads FFmpeg, its versioned libav60-era
libraries, and `libx264-164` using `apt-get download`, checks the `.deb`
architectures and expected package set, and extracts into a temporary private
tree with `dpkg-deb -x`. It never runs `dpkg -i`, downgrades a system package,
or changes system library directories/ldconfig.

The candidate must list `libx264` and successfully encode three synthetic red
frames at 640×480/10 FPS before being published. The final installed private
path is tested again. Encoder listing is bounded to 15 seconds and the smoke
encode to 20 seconds, with forced cleanup after a short grace period. A good existing private tree is reused after the same
checks, without redownloading. A failed candidate preserves the old prefix and
wrapper; a failed final check restores the previous private prefix.

The wrapper sets `LD_LIBRARY_PATH` **for its FFmpeg process** to the extracted
ARM64 library directory and executes that private FFmpeg with unchanged
arguments. This does not alter the Python/CUDA process's library path. The
optional `--install-service-path` writes only the Farsight service's PATH
drop-in at `/etc/systemd/system/farsight.service.d/software-ffmpeg.conf` and
reloads systemd. It never restarts the running service automatically.

When ready to switch the service deliberately:

```sh
sudo systemctl restart farsight.service
systemctl show farsight.service -p Environment
~/.local/bin/ffmpeg -hide_banner -encoders | rg libx264
```

The generated PATH starts with the wrapper directory and preserves the usual
system and CUDA executable locations. The Orin Nano needs this CPU encoding
path; do not assume an `h264_nvenc` option supplies a working Nano encoder.

## Setup integration and offline packages

Normal `setup.sh` behavior remains unchanged except for an encoder-list check.
If the system build omits `libx264`, it warns and does not claim H.264 readiness.
Explicitly select the repair during core setup:

```sh
bash jetson/setup.sh --core-only --with-software-ffmpeg
```

The helper's default package version is the version used on the recovered
board. If an APT repository no longer offers it, choose an available compatible
Ubuntu **6.1** version with `--ffmpeg-version`; this helper intentionally uses
the libavcodec60 ABI rather than mixing NVIDIA 8.x libraries with an older
binary. A download or missing linked-library error is a failure, not readiness.
Inspect the error and package sources/proxy instead of replacing NVIDIA libs.

For a trusted pre-downloaded package set, use `--package-dir /path/to/debs`.
Every expected package must be present exactly once; the FFmpeg version must
match `--ffmpeg-version`, and each package must be arm64 or architecture-independent.
`--prefix` and `--wrapper` support explicit private locations, including spaces.
System paths, symlink wrappers/prefixes, and an unrelated existing prefix are
refused. The helper does not configure an internet proxy or fetch from arbitrary
URLs; use the target's existing configured APT sources.

The focused tests run small mocked ARM/apt/encoder fixtures and prove command
selection, isolation, encoder gating, rollback, quoting, rerun behavior, and
opt-in service integration:

```sh
python -m unittest tools.tests.test_software_ffmpeg -v
```

Those mocks do not prove target encoding. The real-board encoder result belongs
to the recorded manual repair; deploying this helper still requires its own
on-device verification. Actual phone capture, camera-to-display age, Quest
rendering, control leases, power, and thermals remain separate acceptance gates.
