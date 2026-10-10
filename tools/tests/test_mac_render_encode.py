"""Encoder argv, env tuning knobs, and fMP4/AVCC parsing. Real ffmpeg where available."""

import os
import shutil
import struct
import subprocess
import unittest
from unittest.mock import patch

from tools.mac_render.encode import encoder_args, screencast_to_ffmpeg
from tools.mac_render.mp4 import Mp4Reader, codec_from_init, fragment_timing, is_keyframe


def box(kind, payload=b""):
    return struct.pack(">I4s", 8 + len(payload), kind) + payload


class EncoderArgTests(unittest.TestCase):
    def args(self, **overrides):
        env = {key: overrides[key] for key in overrides if key.startswith("MAC_RENDER_")}
        call = {key: overrides[key] for key in overrides if not key.startswith("MAC_RENDER_")}
        with patch.dict(os.environ, env, clear=False):
            return encoder_args(call.get("width", 1280), call.get("height", 720),
                                call.get("fps", 30), call.get("bitrate", 4_000_000),
                                call.get("hardware", False))

    def test_short_gop_one_frame_fragments_and_low_delay(self):
        args, name = self.args()
        self.assertEqual(name, "libx264")
        self.assertEqual(args[args.index("-g") + 1], "10", "a keyframe every 1/3 s at 30 fps")
        self.assertEqual(args[args.index("-frag_duration") + 1], "33333", "one frame per fragment")
        joined = " ".join(args)
        self.assertIn("bframes=0:keyint=10:min-keyint=10", joined)
        self.assertNotIn("nobuffer", joined, "ffmpeg 4.4 nobuffer drops the first decoded frame")

    def test_scales_with_fps(self):
        args, _ = self.args(fps=60)
        self.assertEqual(args[args.index("-g") + 1], "20")
        self.assertEqual(args[args.index("-frag_duration") + 1], "16667")

    def test_defaults_bias_quality(self):
        args, _ = self.args()
        self.assertEqual(args[args.index("-profile:v") + 1], "high")
        self.assertEqual(args[args.index("-b:v") + 1], "8000000", "8 Mbps floor beats --bitrate 4M")
        self.assertEqual(args[args.index("-bufsize") + 1], "4000000", "500 ms VBV window")
        self.assertEqual(args[args.index("-preset") + 1], "faster")
        self.assertEqual(args[args.index("-bf") + 1], "0")
        self.assertIn("-flush_packets", args)

    def test_bitrate_env_is_a_floor_not_a_ceiling(self):
        args, _ = self.args(bitrate=20_000_000)
        self.assertEqual(args[args.index("-b:v") + 1], "20000000", "--bitrate still wins above the floor")
        args, _ = self.args(MAC_RENDER_BITRATE="2000000")
        self.assertEqual(args[args.index("-b:v") + 1], "4000000")

    def test_env_tuning(self):
        args, _ = self.args(MAC_RENDER_GOP_MS="500", MAC_RENDER_FRAG_MS="100",
                            MAC_RENDER_BUF_MS="200", MAC_RENDER_PROFILE="main",
                            MAC_RENDER_X264_PRESET="fast", MAC_RENDER_X264_EXTRA="-nr 100")
        self.assertEqual(args[args.index("-g") + 1], "15")
        self.assertEqual(args[args.index("-frag_duration") + 1], "100000")
        self.assertEqual(args[args.index("-bufsize") + 1], "1600000")
        self.assertEqual(args[args.index("-profile:v") + 1], "main")
        self.assertEqual(args[args.index("-preset") + 1], "fast")
        self.assertIn("-nr", args)

    def test_videotoolbox_path_and_quality_mode(self):
        args, name = self.args(hardware=True)
        self.assertEqual(name, "h264_videotoolbox")
        self.assertEqual(args[args.index("-realtime") + 1], "1")
        args, _ = self.args(hardware=True, MAC_RENDER_VT_QUALITY="65")
        self.assertIn("-q:v", args)
        self.assertEqual(args[args.index("-q:v") + 1], "65")
        self.assertNotIn("-b:v", args, "quality mode keeps only the -maxrate cap")
        self.assertIn("-maxrate", args)
        args, _ = self.args(hardware=False, MAC_RENDER_VT_QUALITY="65")
        self.assertNotIn("-q:v", args, "quality mode is VideoToolbox only")

    def test_invalid_env_is_loud_not_silent(self):
        with patch.dict(os.environ, {"MAC_RENDER_GOP_MS": "abc"}):
            with self.assertRaises(ValueError):
                encoder_args(1280, 720, 30, 4_000_000, hardware=False)
        with patch.dict(os.environ, {"MAC_RENDER_BUF_MS": "1"}):
            with self.assertRaises(ValueError):
                encoder_args(1280, 720, 30, 4_000_000, hardware=False)
        with patch.dict(os.environ, {"MAC_RENDER_PROFILE": "high444"}):
            with self.assertRaises(ValueError):
                encoder_args(1280, 720, 30, 4_000_000, hardware=False)

    def test_full_command_shape(self):
        args, _ = screencast_to_ffmpeg(1280, 720, 30, 4_000_000, hardware=False)
        self.assertEqual(args[0], "ffmpeg")
        self.assertIn("low_delay", args)
        self.assertIn("image2pipe", args)
        self.assertEqual(args[-1], "pipe:1")


class Mp4ReaderTests(unittest.TestCase):
    def test_boxes_split_across_feeds(self):
        avcc = b"avcC" + bytes([1, 0x64, 0x00, 0x1F])
        stream = box(b"ftyp", b"isom") + box(b"moov", avcc) + box(b"moof", b"x") + box(b"mdat", b"yy")
        reader = Mp4Reader()
        out = []
        for byte in stream:  # one byte at a time
            out.extend(reader.feed(bytes([byte])))
        self.assertEqual([kind for kind, _ in out], ["init", "fragment"])
        self.assertEqual(codec_from_init(out[0][1]), "avc1.64001F")

    def test_largesize_box(self):
        payload = b"avcC" + bytes([1, 0x64, 0x00, 0x1F]) + b"pad"
        big = struct.pack(">I4sQ", 1, b"moov", 16 + len(payload)) + payload
        reader = Mp4Reader()
        out = reader.feed(big)
        self.assertEqual(out[0][0], "init")

    def test_bad_size_rejected(self):
        reader = Mp4Reader()
        with self.assertRaises(ValueError):
            reader.feed(struct.pack(">I4s", 999 * 1024 * 1024, b"mdat") + b"x" * 8)

    def test_keyframe_detection_scans_avcc_nals(self):
        moof = box(b"moof", box(b"tfhd", struct.pack(">II", 0x020000, 1)))
        nal = lambda kind: struct.pack(">I", 3) + bytes([kind, 0, 0])
        self.assertTrue(is_keyframe(moof + box(b"mdat", nal(0x06) + nal(0x65))), "SEI then IDR")
        self.assertFalse(is_keyframe(moof + box(b"mdat", nal(0x41))), "not every fragment is sync")
        self.assertFalse(is_keyframe(box(b"moof")))
        self.assertFalse(is_keyframe(moof + box(b"mdat", nal(0x00))), "zero-length NAL stops the scan")


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg required")
class RealEncoderTests(unittest.TestCase):
    def encode(self, frames=45, fps=30):
        from tools.mac_render.bench import generate_jpegs
        args, _ = screencast_to_ffmpeg(640, 360, fps, 4_000_000, hardware=False)
        data = b"".join(generate_jpegs(640, 360, fps, frames))
        result = subprocess.run(args[:-1] + ["pipe:1"], input=data,
                                capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr[-300:])
        return result.stdout

    def test_real_fragments_have_exact_idr_boundaries(self):
        reader = Mp4Reader()
        seen = []
        for kind, data in reader.feed(self.encode()):
            seen.append((kind, data))
        kinds = [kind for kind, _ in seen]
        self.assertEqual(kinds[0], "init")
        self.assertIn("avc1.64", codec_from_init(seen[0][1]), "high profile in the init segment")
        fragments = [data for kind, data in seen if kind == "fragment"]
        self.assertGreater(len(fragments), 30)
        keyframes = [index for index, data in enumerate(fragments) if is_keyframe(data)]
        self.assertEqual(keyframes[0], 0, "first fragment must be decodable")
        self.assertEqual(keyframes[:4], [0, 10, 20, 30], "IDR every 10 frames at fps=30")
        first, count, start, end = fragment_timing(fragments[0], 30)
        self.assertEqual((first, count), (0, 1), "one encoded frame per fragment")

    def test_synthetic_bench_reports(self):
        from tools.mac_render.bench import measure_synthetic
        report = measure_synthetic(320, 180, 30, 1_000_000, 2, hardware=False)
        self.assertEqual(report["encoder"], "libx264")
        self.assertGreater(report["frames_encoded"], 30)
        self.assertIsNotNone(report["first_fragment_ms"])
        self.assertGreater(report["fps_sustained"], 20)


if __name__ == "__main__":
    unittest.main()
