"""Rolling timings for the bench report. No sockets, no ffmpeg."""


def take(samples, key):
    return [item[key] for item in samples if key in item and item[key] is not None]


def summarize(samples, seconds, width, height, fps, encoder, label):
    from .protocol import percentile

    ages = take(samples, "frame_age_ms")
    poses = take(samples, "pose_to_frame_ms")
    bytes_out = sum(take(samples, "bytes"))
    return {
        "label": label,
        "resolution": f"{width}x{height}",
        "fps": fps,
        "encoder": encoder,
        "seconds": round(seconds, 3),
        "fragments": len(samples),
        "render_ms_p50": percentile(take(samples, "render_ms"), 0.50),
        "capture_ms_p50": percentile(take(samples, "capture_ms"), 0.50),
        "encode_ms_p50": percentile(take(samples, "encode_ms"), 0.50),
        "bytes_per_s": round(bytes_out / seconds, 1) if seconds else None,
        "frame_age_ms_p50": percentile(ages, 0.50),
        "frame_age_ms_p95": percentile(ages, 0.95),
        "pose_to_frame_ms_p50": percentile(poses, 0.50),
        "pose_to_frame_ms_p95": percentile(poses, 0.95),
    }
