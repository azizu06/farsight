"""Mac visual-offload message shapes. No browser, no ffmpeg, no motion."""

import json
import shutil
import struct
import subprocess
import unittest
from pathlib import Path

from tools.mac_render.encode import encoder_args
from tools.mac_render.mp4 import Mp4Reader, codec_from_init, fragment_timing
from tools.mac_render.protocol import (
    frame_age_ms, oldest_of, parse_pose, percentile, pose_is_stale,
    pose_to_frame_ms, segment_sidecar,
)
from tools.mac_render.server import ViewerQueue, _is_keyframe
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

    def test_stale_uses_the_mac_clock_not_the_viewer_clock(self):
        held = {"received_mono_ms": 0}
        self.assertTrue(pose_is_stale(held, 251))
        self.assertFalse(pose_is_stale(held, 250))
        # A viewer's own timestamp can be hours away from ours. That is not stale.
        self.assertFalse(pose_is_stale({"received_mono_ms": 10_000}, 10_100))

    def test_oldest_frame_in_a_fragment_is_the_age(self):
        samples = [{"frame_id": 1}, {"frame_id": 2}, {"frame_id": 3}]
        self.assertEqual(oldest_of(samples, 3)["frame_id"], 1)
        self.assertEqual(oldest_of(samples, 1)["frame_id"], 3)

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
        self.assertEqual(args[args.index("-g") + 1], "10", "a keyframe every 1/3 s at 30 fps")
        self.assertEqual(args[args.index("-frag_duration") + 1], "33333", "one frame per fragment")
        fallback, fallback_name = encoder_args(1920, 1080, 30, 4_000_000, hardware=False)
        self.assertEqual(fallback_name, "libx264")
        self.assertIn("bframes=0", " ".join(fallback))


QUEST = Path(__file__).resolve().parents[2] / "quest"
PRODUCER = """
import { MapWalk } from "./src/map-walk.js";
import { mapPoseMessage } from "./src/mac-auto-pose.js";
const walk = new MapWalk(); walk.setMode("walk"); walk.setFloor(-0.3); walk.observeHead(0, 0, 0.4); walk.moveViewer(1, 0.5, 0, 0.6);
const projection = [1.2, 0, 0, 0, 0, 1.1917535925942102, 0, 0, 0, 0, -1.002, -1, 0, 0, -0.2002, 0];
const message = mapPoseMessage({ id: 41, mono: 1234.5, wall: 1.7e12, walk,
  head: { position: [0.1, 1.6, -0.2], quaternion: [0, 0.2, 0, 0.98] }, ray: null,
  eyes: [0, 1].map(i => ({ position: [i ? 0.032 : -0.032, 1.6, 0], quaternion: [0, 0, 0, 1], projection })),
  session: "6f1c2a9e-3b4d-4c5e-8f60-718293a4b5c6", mapRevision: 29 });
process.stdout.write(JSON.stringify(message));
"""


@unittest.skipUnless(shutil.which("node") and (QUEST / "node_modules" / "three").is_dir(), "node + quest deps")
class QuestPoseContractTest(unittest.TestCase):
    """The real #144 Quest producer, through this parser, back out as the frame echo."""

    def produce(self):
        result = subprocess.run(["node", "--input-type=module", "-e", PRODUCER], cwd=QUEST,
                                capture_output=True, text=True, timeout=30, check=True)
        return json.loads(result.stdout)

    def test_auto_pose_parses_and_echoes_identity_and_frustum(self):
        message = self.produce()
        pose = parse_pose(message, now_mono_ms=5000)
        self.assertEqual(pose["pose_id"], 41)
        self.assertEqual(pose["session_id"], message["session_id"])
        self.assertEqual(pose["space"], "ar_map")
        self.assertEqual(pose["map_revision"], 29)
        self.assertAlmostEqual(pose["fov_y_deg"], message["fov_y_deg"])
        self.assertEqual(len(pose["eyes"]), 2)
        # Map-frame head, not the raw headset pose: virtual locomotion survives the trip.
        self.assertEqual(pose["position"], message["head"]["position"])
        self.assertNotEqual(pose["position"], [0.1, 1.6, -0.2])
        drawn = dict(pose, fov_y_deg=min(pose["fov_y_deg"], 90.0))  # what render.mjs echoes
        side = segment_sidecar(9, drawn, 10, 1000, 0.0, 0.033, 3, "SIMULATED", 1, 2, 1)
        self.assertEqual(side["pose"]["pose_id"], 41)
        self.assertEqual(side["pose"]["sent_mono_ms"], 1234.5)
        self.assertEqual(side["pose"]["session_id"], message["session_id"])
        self.assertAlmostEqual(side["fov_y_deg"], drawn["fov_y_deg"])

    def test_frame_id_alias_and_rejections(self):
        message = self.produce()
        del message["pose_id"]
        self.assertEqual(parse_pose(message, 0)["pose_id"], 41)
        for bad in ({"pose_id": -1}, {"pose_id": 1.5}, {"session_id": ""}, {"session_id": "x" * 65},
                    {"space": "world"}, {"fov_y_deg": 170}, {"aspect": 0}, {"eyes": [{}]}):
            with self.assertRaises(ValueError, msg=bad):
                parse_pose(dict(self.produce(), **bad), 0)


class KeyframeTest(unittest.TestCase):
    def test_idr_is_found_in_avcc_mdat_not_by_tfhd_flags(self):
        moof = box(b"moof", box(b"tfhd", struct.pack(">II", 0x020000, 1)))
        nal = lambda kind: struct.pack(">I", 3) + bytes([kind, 0, 0])
        self.assertTrue(_is_keyframe(moof + box(b"mdat", nal(0x06) + nal(0x65))), "SEI then IDR")
        self.assertFalse(_is_keyframe(moof + box(b"mdat", nal(0x41))), "default-base-is-moof alone is not sync")
        self.assertFalse(_is_keyframe(box(b"moof")))


class ViewerQueueTest(unittest.TestCase):
    def test_late_join_waits_for_a_keyframe_and_backlog_skips_forward(self):
        queue = ViewerQueue(limit=12)
        item = lambda n, key=False: {"n": n, "keyframe": key}
        queue.offer(item(1)); queue.offer(item(2))
        self.assertEqual(queue.items, [], "no fragment before a keyframe")
        for n, key in ((3, True), (4, False), (5, False), (6, True), (7, False)):
            queue.offer(item(n, key))
        self.assertEqual(queue.take()["n"], 6, "a backlog jumps to its newest keyframe")
        self.assertEqual(queue.take()["n"], 7)

    def test_full_queue_resyncs_on_the_next_keyframe(self):
        queue = ViewerQueue(limit=3)
        queue.offer({"n": 0, "keyframe": True})
        for n in range(1, 4):
            queue.offer({"n": n, "keyframe": False})
        self.assertEqual(queue.items, [], "overflow drops the backlog")
        queue.offer({"n": 4, "keyframe": False})
        self.assertEqual(queue.items, [])
        queue.offer({"n": 5, "keyframe": True})
        self.assertEqual(queue.take()["n"], 5)


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
