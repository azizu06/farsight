"""Mac visual-offload message shapes. No browser, no ffmpeg, no motion."""

import struct
import unittest

from tools.mac_render.encode import encoder_args
from tools.mac_render.mp4 import Mp4Reader, codec_from_init, fragment_timing
from tools.mac_render.protocol import (
    frame_age_ms, parse_pose, percentile, pose_to_frame_ms, segment_sidecar,
)
from tools.mac_render.stats import summarize


def box(kind, payload=b""):
    return struct.pack(">I4s", 8 + len(payload), kind) + payload


class ProtocolTest(unittest.TestCase):
    def test_pose_and_ray(self):
        pose = parse_pose({
            "type": "pose", "version": 1, "sent_mono_ms": 10, "sent_wall_ms": 20,
            "head": {"position": [0, 1.6, 0], "quaternion": [0, 0, 0, 2]},
            "ray": {"origin": [0, 1, 0], "direction": [0, 0, -2]},
        }, now_mono_ms=12)
        self.assertEqual(pose["quaternion"], [0, 0, 0, 1])
        self.assertEqual(pose["received_mono_ms"], 12)

    def test_rejects_motion_shaped_garbage(self):
        with self.assertRaises(ValueError):
            parse_pose({"type": "control", "v": 1, "w": 1}, 0)
        with self.assertRaises(ValueError):
            parse_pose({"type": "pose", "version": 1, "sent_mono_ms": 0, "sent_wall_ms": 0,
                        "head": {"position": [1], "quaternion": [0, 0, 0, 1]}}, 0)

    def test_frame_age_and_pose_to_frame(self):
        self.assertEqual(frame_age_ms(1000, 1040), 40)
        self.assertEqual(pose_to_frame_ms({"received_mono_ms": 10}, 25), 15)
        self.assertIsNone(pose_to_frame_ms(None, 25))
        self.assertIsNone(pose_to_frame_ms({"desktop_preview": True}, 25))
        with self.assertRaises(ValueError):
            frame_age_ms(float("nan"), 1)

    def test_sidecar_keeps_the_player_fields(self):
        side = segment_sidecar(3, {"sent_mono_ms": 1, "sent_wall_ms": 2, "received_mono_ms": 3},
                               10, 1000, 0.0, 0.1, 7, "SIMULATED", 2, 4, 1)
        self.assertEqual(side["type"], "video_segment")
        self.assertEqual(side["macview"], "macview_segment")
        self.assertEqual(side["request_id"], 7)
        self.assertGreaterEqual(side["age_ms"], 0)
        self.assertFalse(side["stereo"])

    def test_desktop_preview_identity_is_explicit_without_fake_input_latency(self):
        pose = {"session_id": "desktop-preview-fixture", "frame_id": 3, "desktop_preview": True}
        side = segment_sidecar(4, pose, 10, 1000, 0, .1, 7, "SIMULATED", 2, 4, 1)
        self.assertTrue(side["pose"]["desktop_preview"])
        self.assertEqual(side["pose"]["session_id"], pose["session_id"])
        self.assertIsNone(pose_to_frame_ms(pose, 10))

    def test_percentile(self):
        self.assertEqual(percentile([1, 2, 3, 4], 0.5), 2.5)
        self.assertIsNone(percentile([], 0.5))


class Mp4Test(unittest.TestCase):
    def test_splits_init_and_fragment(self):
        avcc = b"avcC" + bytes([1, 0x64, 0x00, 0x1F])
        reader = Mp4Reader()
        self.assertEqual(reader.feed(box(b"ftyp", b"isom")), [])
        pieces = reader.feed(box(b"moov", avcc))
        self.assertEqual(pieces[0][0], "init")
        self.assertEqual(codec_from_init(pieces[0][1]), "avc1.64001F")
        sample = box(b"moof", b"tfdt" + bytes([1, 0, 0, 0]) + struct.pack(">Q", 100000) + b"trun" + bytes(4) + struct.pack(">I", 3))
        media = box(b"mdat", b"xx")
        kinds = [item[0] for item in reader.feed(sample + media)]
        self.assertEqual(kinds, ["fragment"])
        first, count, start, end = fragment_timing(sample + media, 30)
        self.assertEqual((first, count, start), (3, 3, 0.1))
        self.assertAlmostEqual(end, 0.2)


class EncodeTest(unittest.TestCase):
    def test_no_bframes_and_short_gop(self):
        args, name = encoder_args(1280, 720, 30, 4_000_000, hardware=True)
        self.assertEqual(name, "h264_videotoolbox")
        self.assertIn("-bf", args)
        self.assertEqual(args[args.index("-bf") + 1], "0")
        self.assertIn("-g", args)
        fallback, fallback_name = encoder_args(1920, 1080, 30, 4_000_000, hardware=False)
        self.assertEqual(fallback_name, "libx264")
        self.assertIn("bframes=0", " ".join(fallback))


class StatsTest(unittest.TestCase):
    def test_report_is_labelled_desk(self):
        report = summarize(
            [{"render_ms": 5, "capture_ms": 1, "encode_ms": 3, "bytes": 1000,
              "frame_age_ms": 40, "pose_to_frame_ms": 20}],
            seconds=1, width=1280, height=720, fps=30, encoder="h264_videotoolbox",
            label="Mac desk, no headset")
        self.assertEqual(report["label"], "Mac desk, no headset")
        self.assertEqual(report["frame_age_ms_p50"], 40)
        self.assertEqual(report["bytes_per_s"], 1000)


if __name__ == "__main__":
    unittest.main()
