"""ffmpeg args for the Mac visual stream. VideoToolbox first, libx264 fallback.

Knobs for live tuning without touching the server (all optional):

MAC_RENDER_BITRATE     bitrate floor bits/s; default 8000000. The server's
                       --bitrate can still go higher; set this lower on a slow link.
MAC_RENDER_GOP_MS      keyframe interval ms; default 333 (~3 restarts/s)
MAC_RENDER_FRAG_MS     muxer fragment duration ms; default 0 = one frame
MAC_RENDER_BUF_MS      VBV rate-control window ms; default 500
MAC_RENDER_PROFILE     baseline|main|high; default high
MAC_RENDER_X264_PRESET libx264 preset for the fallback path; default faster
MAC_RENDER_VT_QUALITY  VideoToolbox -q:v 1..100 (bounded by -maxrate); unset = CBR
MAC_RENDER_VT_EXTRA    extra h264_videotoolbox args, shlex-split
MAC_RENDER_X264_EXTRA  extra libx264 args, shlex-split (after our params)
"""

import os
import shlex
import shutil
import subprocess

# A keyframe every third of a second lets a lagging viewer skip forward to the
# newest decodable picture; one frame per fragment keeps fragmenting from adding delay.
GOP_MS = 333

X264_PRESETS = ("ultrafast", "superfast", "veryfast", "faster", "fast", "medium",
                "slow", "slower", "veryslow")


def _env_int(name, default, low, high):
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name} must be an integer, got {raw!r}")
    if not low <= value <= high:
        raise ValueError(f"{name} must be {low}..{high}, got {value}")
    return value


def _env_choice(name, default, choices):
    value = os.environ.get(name, default)
    if value not in choices:
        raise ValueError(f"{name} must be one of {choices}, got {value!r}")
    return value


def videobox_available():
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return False
    try:
        result = subprocess.run(
            [ffmpeg, "-hide_banner", "-encoders"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return "h264_videotoolbox" in result.stdout


def encoder_args(width, height, fps, bitrate, hardware=None, fragment_ms=None):
    """Low-latency, no B-frames, short GOP. Matches jetson/video.py fragment flags."""
    if hardware is None:
        hardware = videobox_available()
    if any(not isinstance(size, int) or size % 2 or not 16 <= size <= 1920 for size in (width, height)):
        raise ValueError("even dimensions 16..1920")
    if not isinstance(fps, int) or not 1 <= fps <= 60:
        raise ValueError("fps 1..60")
    bitrate = max(bitrate, _env_int("MAC_RENDER_BITRATE", 8_000_000, 100_000, 80_000_000))
    gop_ms = _env_int("MAC_RENDER_GOP_MS", GOP_MS, 50, 10_000)
    buf_ms = _env_int("MAC_RENDER_BUF_MS", 500, 50, 5_000)
    frag_ms = fragment_ms if fragment_ms is not None else _env_int("MAC_RENDER_FRAG_MS", 0, 0, 5_000)
    profile = _env_choice("MAC_RENDER_PROFILE", "high", ("baseline", "main", "high"))
    vt_quality = _env_int("MAC_RENDER_VT_QUALITY", 0, 0, 100)
    gop = max(1, round(fps * gop_ms / 1000))
    # Zero or unset frag duration cuts one fragment per encoded frame.
    fragment_us = max(1, round(1_000_000 * (frag_ms / 1000 if frag_ms else 1 / fps)))
    # A 500 ms VBV window lets keyframes stay sharp on the LAN; tighten with
    # MAC_RENDER_BUF_MS if the link queues bursts.
    bufsize = str(max(64_000, bitrate * buf_ms // 1000))
    rate = (["-q:v", str(vt_quality), "-maxrate", str(bitrate), "-bufsize", bufsize]
            if hardware and vt_quality
            else ["-b:v", str(bitrate), "-maxrate", str(bitrate), "-bufsize", bufsize])
    common = [
        "-an", "-r", str(fps), "-g", str(gop), "-bf", "0", "-profile:v", profile,
        "-pix_fmt", "yuv420p", *rate,
        "-movflags", "frag_keyframe+empty_moov+default_base_moof",
        "-video_track_timescale", "1000000",
        "-frag_duration", str(fragment_us), "-flush_packets", "1",
        "-f", "mp4", "pipe:1",
    ]
    if hardware:
        # -realtime 1 keeps VideoToolbox from buffering for quality.
        return ["-c:v", "h264_videotoolbox", "-realtime", "1", "-allow_sw", "1",
                *shlex.split(os.environ.get("MAC_RENDER_VT_EXTRA", "")), *common], "h264_videotoolbox"
    return [
        "-c:v", "libx264",
        "-preset", _env_choice("MAC_RENDER_X264_PRESET", "faster", X264_PRESETS),
        "-tune", "zerolatency",
        "-x264-params", f"bframes=0:keyint={gop}:min-keyint={gop}:scenecut=0",
        *shlex.split(os.environ.get("MAC_RENDER_X264_EXTRA", "")),
        *common,
    ], "libx264"


def screencast_to_ffmpeg(width, height, fps, bitrate, hardware=None, fragment_ms=None):
    """CDP screencast already emits JPEG. Decode it, then hardware-encode."""
    encode, name = encoder_args(width, height, fps, bitrate, hardware, fragment_ms)
    args = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        # Decode each JPEG as it arrives; no probing and no decoder delay.
        # (-fflags nobuffer was measured to drop the first decoded frame on
        # ffmpeg 4.4, shifting the stream one input ordinal early — leave it off.)
        "-flags", "low_delay",
        "-probesize", "32", "-analyzeduration", "0", "-threads", "1", "-filter_threads", "1",
        "-f", "image2pipe", "-vcodec", "mjpeg", "-framerate", str(fps),
        "-i", "pipe:0",
        *encode,
    ]
    return args, name
