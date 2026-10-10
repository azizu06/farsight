"""Pipeline behavior for the Mac render server: per-viewer freshness queues,
keyframe-safe drops, /dense passthrough, and the capture-side pose handoff.
No browser, no ffmpeg, no motion."""

import asyncio
import base64
import struct
import unittest

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from tools.mac_render.server import (
    App, RenderEncoder, ViewerQueue, _CAPTURE_JS, _CAPTURE_POSE_JS,
    _is_keyframe, create_app, parse_args,
)


def box(kind, payload=b""):
    return struct.pack(">I4s", 8 + len(payload), kind) + payload


class FakePage:
    def __init__(self):
        self.calls = []
        self.delay = 0

    async def evaluate(self, script, arg=None):
        await asyncio.sleep(self.delay)
        self.calls.append((script, arg))


class KeyframeTest(unittest.TestCase):
    def test_idr_is_found_in_avcc_mdat_not_by_tfhd_flags(self):
        moof = box(b"moof", box(b"tfhd", struct.pack(">II", 0x020000, 1)))
        nal = lambda kind: struct.pack(">I", 3) + bytes([kind, 0, 0])
        self.assertTrue(_is_keyframe(moof + box(b"mdat", nal(0x06) + nal(0x65))), "SEI then IDR")
        self.assertFalse(_is_keyframe(moof + box(b"mdat", nal(0x41))), "default-base-is-moof alone is not sync")
        self.assertFalse(_is_keyframe(box(b"moof")))


class ViewerQueueTest(unittest.TestCase):
    """Ported from aziz/mac-dense-render (quest-hud handoff)."""

    def test_late_join_waits_for_a_keyframe_and_backlog_skips_forward(self):
        queue = ViewerQueue(limit=12)
        item = lambda n, key=False: {"n": n, "keyframe": key}
        queue.offer(item(1))
        queue.offer(item(2))
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


class FanoutTest(unittest.TestCase):
    def test_publish_is_per_viewer_and_a_stalled_viewer_resyncs_alone(self):
        encoder = RenderEncoder(None, 320, 240, 30, 100000, False)
        fast, stalled = encoder.subscribe(), encoder.subscribe()
        stalled.limit = 2
        encoder._publish({"n": 0, "keyframe": True})
        for n in range(1, 4):
            encoder._publish({"n": n, "keyframe": False})
        self.assertEqual(stalled.items, [], "the stalled viewer dropped its backlog")
        self.assertEqual([item["n"] for item in fast.items], [0, 1, 2, 3],
                         "the fast viewer kept every fragment")
        encoder._publish({"n": 4, "keyframe": True})
        encoder._publish({"n": 5, "keyframe": False})
        self.assertEqual(stalled.take()["n"], 4)
        encoder.unsubscribe(stalled)
        self.assertNotIn(stalled, encoder.viewers)


class PoseEchoTest(unittest.IsolatedAsyncioTestCase):
    """The pose rides the capture evaluate: one browser turn applies it and
    produces the frame drawn from it, so poses never queue on the page's
    main thread behind slow draws."""

    async def test_disconnect_clear_wins_over_queued_poses(self):
        # Same ordering guarantee as the pump version: when the pose socket
        # closes, the null write beats every earlier queued pose and the
        # ownership slot stays reserved until the clear is staged.
        from unittest.mock import patch

        class Socket:
            async def prepare(self, request):
                pass

            async def close(self):
                pass

        page = FakePage()
        app = App(parse_args([]), page=page)
        app.encoder = RenderEncoder(page, 320, 240, 30, 100000, False)
        with patch("tools.mac_render.server.web.WebSocketResponse", return_value=Socket()):
            async with app.owned_socket("pose_socket", None):
                app.encoder.echo_pose({"received_mono_ms": 1})
                app.encoder.echo_pose({"received_mono_ms": 2})
            self.assertIsNone(app.pose_socket)
        self.assertIsNone(app.encoder.latest_pose)
        script, arg = app.encoder._capture_call()
        self.assertIs(script, _CAPTURE_POSE_JS)
        self.assertIsNone(arg, "the disconnect clear replaced the queued poses")
        self.assertEqual(page.calls, [], "no standalone evaluates — the clear rides the next capture")

    def test_a_pose_burst_coalesces_to_the_newest_capture(self):
        encoder = RenderEncoder(None, 320, 240, 30, 100000, False)
        poses = [{"received_mono_ms": i, "position": [i, 0, 0]} for i in range(6)]
        for pose in poses:
            encoder.echo_pose(pose)
        script, arg = encoder._capture_call()
        self.assertIs(script, _CAPTURE_POSE_JS)
        self.assertIs(arg, poses[-1], "a burst collapses to the newest pose")
        self.assertGreaterEqual(arg["receipt_age_ms"], 0)
        script, arg = encoder._capture_call()
        self.assertIs(script, _CAPTURE_JS, "the next capture is plain again")

    def test_untracked_pose_is_delivered_as_null(self):
        encoder = RenderEncoder(None, 320, 240, 30, 100000, False)
        encoder.echo_pose(None)
        script, arg = encoder._capture_call()
        self.assertIs(script, _CAPTURE_POSE_JS)
        self.assertIsNone(arg)


class _FakeStdin:
    def __init__(self, drain_s):
        self.drain_s = drain_s
        self.writes = 0

    def write(self, _data):
        self.writes += 1

    async def drain(self):
        await asyncio.sleep(self.drain_s)


class _FakeProcess:
    def __init__(self, drain_s):
        self.stdin = _FakeStdin(drain_s)
        self.returncode = None


class InputPathTest(unittest.IsolatedAsyncioTestCase):
    """Input-stage attribution + backpressure-driven capture skip."""

    async def _frame(self, encoder, drain_s):
        encoder.process = _FakeProcess(drain_s)
        jpeg = base64.b64encode(b"jpeg-bytes").decode()
        await encoder._on_frame(None, {"data": jpeg, "meta": {"source": "TEST"}}, 1, 2, eval_ms=240.0)
        return encoder.samples[-1]

    async def test_stage_timings_are_recorded(self):
        encoder = RenderEncoder(None, 320, 240, 30, 100000, False)
        sample = await self._frame(encoder, 0.001)
        self.assertEqual(sample["eval_ms"], 240.0)
        self.assertEqual(sample["jpeg_bytes"], len(b"jpeg-bytes"))
        self.assertIn("decode_ms", sample)
        self.assertGreaterEqual(sample["feed_wait_ms"], 0.5)
        self.assertFalse(encoder._feed_behind)

    async def test_a_slow_drain_flags_the_next_capture_as_stale(self):
        encoder = RenderEncoder(None, 320, 240, 30, 100000, False)
        await self._frame(encoder, 0.05)  # 50ms drain > 33ms interval
        self.assertTrue(encoder._feed_behind, "encoder behind -> next capture is skipped")


class DenseProxyTest(unittest.IsolatedAsyncioTestCase):
    async def test_dense_route_passes_query_and_upstream_status(self):
        seen = []
        source = web.Application()

        async def delta(request):
            seen.append((request.path, request.query_string))
            return web.Response(body=b"delta", headers={"X-Dense-Revision": "7"})

        async def missing(request):
            return web.Response(status=404, body=b"nope")

        source.router.add_get("/dense/delta.bin", delta)
        source.router.add_get("/dense/missing.bin", missing)
        upstream = TestServer(source)
        await upstream.start_server()
        client = TestClient(TestServer(create_app(
            parse_args(["--upstream", str(upstream.make_url("/")), "--no-browser"]))))
        await client.start_server()
        try:
            response = await client.get("/dense/delta.bin?since=3&limit=50")
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers["X-Dense-Revision"], "7")
            self.assertEqual(await response.read(), b"delta")
            self.assertEqual(seen, [("/dense/delta.bin", "since=3&limit=50")])
            response = await client.get("/dense/missing.bin")
            self.assertEqual(response.status, 404, "upstream status passes through")
        finally:
            await client.close()
            await upstream.close()


if __name__ == "__main__":
    unittest.main()
