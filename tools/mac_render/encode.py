"""ffmpeg args for the Mac visual stream. VideoToolbox first, libx264 fallback."""

import shutil
import subprocess


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


def encoder_args(width, height, fps, bitrate, hardware=None):
    """Low-latency, no B-frames, short GOP. Matches jetson/video.py fragment flags."""
    if hardware is None:
        hardware = videobox_available()
    if any(not isinstance(size, int) or size % 2 or not 16 <= size <= 1920 for size in (width, height)):
        raise ValueError("even dimensions 16..1920")
    if not isinstance(fps, int) or not 1 <= fps <= 60:
        raise ValueError("fps 1..60")
    common = [
        "-an", "-r", str(fps), "-g", "30", "-bf", "0", "-profile:v", "main",
        "-pix_fmt", "yuv420p", "-b:v", str(bitrate), "-maxrate", str(bitrate),
        "-bufsize", str(max(64_000, bitrate // 5)),
        "-movflags", "frag_keyframe+empty_moov+default_base_moof",
        "-video_track_timescale", "1000000",
        "-frag_duration", "100000", "-f", "mp4", "pipe:1",
    ]
    if hardware:
        # -realtime 1 keeps VideoToolbox from buffering for quality.
        return ["-c:v", "h264_videotoolbox", "-realtime", "1", "-allow_sw", "1", *common], "h264_videotoolbox"
    return [
        "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
        "-x264-params", "bframes=0:keyint=30:min-keyint=30:scenecut=0",
        *common,
    ], "libx264"


def screencast_to_ffmpeg(width, height, fps, bitrate, hardware=None):
    """CDP screencast already emits JPEG. Decode it, then hardware-encode."""
    encode, name = encoder_args(width, height, fps, bitrate, hardware)
    args = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-f", "image2pipe", "-vcodec", "mjpeg", "-framerate", str(fps),
        "-i", "pipe:0",
        *encode,
    ]
    return args, name
