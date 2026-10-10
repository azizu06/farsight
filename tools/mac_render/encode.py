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


# A keyframe every third of a second lets a lagging viewer skip forward to the
# newest decodable picture; one frame per fragment keeps fragmenting from adding delay.
GOP_SECONDS = 1 / 3


def encoder_args(width, height, fps, bitrate, hardware=None):
    """Low-latency, no B-frames, short GOP. Matches jetson/video.py fragment flags."""
    if hardware is None:
        hardware = videobox_available()
    if any(not isinstance(size, int) or size % 2 or not 16 <= size <= 1920 for size in (width, height)):
        raise ValueError("even dimensions 16..1920")
    if not isinstance(fps, int) or not 1 <= fps <= 60:
        raise ValueError("fps 1..60")
    gop = max(1, round(fps * GOP_SECONDS))
    common = [
        "-an", "-r", str(fps), "-g", str(gop), "-bf", "0", "-profile:v", "main",
        "-pix_fmt", "yuv420p", "-b:v", str(bitrate), "-maxrate", str(bitrate),
        "-bufsize", str(max(64_000, bitrate // 5)),
        "-movflags", "frag_keyframe+empty_moov+default_base_moof",
        "-video_track_timescale", "1000000",
        "-frag_duration", str(round(1_000_000 / fps)), "-flush_packets", "1", "-f", "mp4", "pipe:1",
    ]
    if hardware:
        # -realtime 1 keeps VideoToolbox from buffering for quality.
        return ["-c:v", "h264_videotoolbox", "-realtime", "1", "-allow_sw", "1", *common], "h264_videotoolbox"
    return [
        "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
        "-x264-params", f"bframes=0:keyint={gop}:min-keyint={gop}:scenecut=0",
        *common,
    ], "libx264"


def screencast_to_ffmpeg(width, height, fps, bitrate, hardware=None):
    """CDP screencast already emits JPEG. Decode it, then hardware-encode."""
    encode, name = encoder_args(width, height, fps, bitrate, hardware)
    args = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-fflags", "nobuffer", "-flags", "low_delay", "-probesize", "32", "-analyzeduration", "0", "-threads", "1", "-filter_threads", "1",
        "-f", "image2pipe", "-vcodec", "mjpeg", "-framerate", str(fps),
        "-i", "pipe:0",
        *encode,
    ]
    return args, name
