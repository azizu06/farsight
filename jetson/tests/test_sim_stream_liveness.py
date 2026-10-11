"""CPU-only production simulator lock contention and worker lifetime checks."""
import asyncio
from contextlib import asynccontextmanager
import threading
import time
from types import SimpleNamespace
import unittest

from jetson.mapping.state import MapState
from jetson.sim.stream import SimFeed, stream_world
from jetson.sim.world import RoverSim, World


async def event_ready(event):
    if not await asyncio.wait_for(asyncio.to_thread(event.wait, 3), 4):
        raise AssertionError('Worker did not reach its controlled barrier')


class HeldFusion:
    """Hold the actual mapper fusion lock, not an invented asyncio lock."""
    def __init__(self, *, pause_before_pose=False, fail_pose=False):
        self.state = MapState(demo=True)
        self.feed = SimFeed(RoverSim(World('SYNTHETIC lock fixture', blocks=[])))
        self.camera = SimpleNamespace(phone_rgb=None)
        self.fusion_entered = threading.Event()
        self.fusion_release = threading.Event()
        self.fusion_finished = threading.Event()
        self.pose_entered = threading.Event()
        self.pose_release = threading.Event()
        self.pose_applied = threading.Event()
        self.pose_return = threading.Event()
        self.pose_finished = threading.Event()
        self.calls = []
        integrate = self.state.cloud.integrate
        observe = self.state.observe_pose

        def held_integrate(points, colors):
            self.fusion_entered.set()
            if not self.fusion_release.wait(3):
                raise RuntimeError('Fusion fixture barrier timed out')
            try:
                return integrate(points, colors)
            finally:
                self.fusion_finished.set()

        def held_pose(header, transform, received_at=None, *, socket_generation=None):
            # MapState.process also publishes the frame's pose. Only the
            # explicit socket-bound heartbeat is the worker under test.
            if socket_generation is None:
                return observe(header, transform, received_at)
            self.calls.append(dict(header=header, received_at=received_at,
                                   socket_generation=socket_generation,
                                   thread=threading.get_ident()))
            # Make the contention seam deterministic even if the executor
            # schedules the pose task before fusion reaches its lock section.
            if not self.fusion_entered.wait(3):
                raise RuntimeError('Fusion did not acquire its production lock')
            self.pose_entered.set()
            if fail_pose:
                raise RuntimeError('controlled pose fault')
            if pause_before_pose and not self.pose_release.wait(3):
                raise RuntimeError('Pose fixture input barrier timed out')
            observe(header, transform, received_at, socket_generation=socket_generation)
            self.pose_applied.set()
            if not self.pose_return.wait(3):
                raise RuntimeError('Pose fixture return barrier timed out')
            self.pose_finished.set()

        self.state.cloud.integrate = held_integrate
        self.state.observe_pose = held_pose
        self.task = None

    async def start(self):
        self.task = asyncio.create_task(stream_world(self.state, self.camera, self.feed))
        await event_ready(self.fusion_entered)
        await event_ready(self.pose_entered)

    async def close(self):
        self.fusion_release.set(); self.pose_release.set(); self.pose_return.set()
        try:
            if self.task:
                self.task.cancel()
                await asyncio.wait_for(asyncio.gather(self.task, return_exceptions=True), 4)
        finally:
            await asyncio.to_thread(self.state.dense_worker.close)


@asynccontextmanager
async def held_fusion(**kwargs):
    fixture = HeldFusion(**kwargs)
    try:
        await fixture.start()
        yield fixture
    finally:
        await fixture.close()


class SimStreamLivenessTests(unittest.IsolatedAsyncioTestCase):
    async def test_event_loop_runs_while_real_fusion_lock_holds_one_pose_worker(self):
        async with held_fusion() as fixture:
            self.assertFalse(fixture.fusion_release.is_set())
            # A timer and coroutine must run while the worker is STILL blocked
            # on the production lock; measuring a final runtime would miss it.
            pulse = asyncio.Event()
            asyncio.get_running_loop().call_later(.01, pulse.set)
            await asyncio.wait_for(pulse.wait(), 1)
            self.assertFalse(fixture.fusion_release.is_set())
            self.assertFalse(fixture.pose_applied.is_set())
            self.assertEqual(len(fixture.calls), 1, 'No queued pose backlog')
            self.assertNotEqual(fixture.calls[0]['thread'], threading.get_ident())
            self.assertIn(fixture.feed.offer_frame, fixture.state.frame_observers)

    async def test_executor_delay_keeps_original_pose_receipt_and_socket_lifetime(self):
        async with held_fusion() as fixture:
            queued = fixture.calls[0]
            self.assertAlmostEqual(queued['header']['t_capture'] + fixture.feed.started,
                                   queued['received_at'])
            self.assertEqual(queued['socket_generation'], fixture.state.socket_generation)
            # Establish a later completion boundary without changing any clock.
            pulse = asyncio.Event()
            asyncio.get_running_loop().call_later(.01, pulse.set)
            await asyncio.wait_for(pulse.wait(), 1)
            resumed_at = time.monotonic()
            fixture.fusion_release.set()
            await event_ready(fixture.pose_applied)
            self.assertLess(queued['received_at'], resumed_at)
            self.assertEqual(fixture.state.last_received, queued['received_at'])
            self.assertEqual(len(fixture.calls), 1)

    async def test_pending_pose_cannot_publish_into_reconnect_or_disconnection(self):
        for change in ('same-session-reconnect', 'new-session', 'disconnect'):
            with self.subTest(change=change):
                async with held_fusion(pause_before_pose=True) as fixture:
                    old_generation = fixture.calls[0]['socket_generation']
                    fixture.fusion_release.set()
                    await event_ready(fixture.fusion_finished)

                    def change_owner():
                        if change == 'disconnect':
                            with fixture.state.lock:
                                fixture.state.connected = False
                        else:
                            identity = fixture.state.identity if change == 'same-session-reconnect' else ('new-source', 2)
                            fixture.state.start_session(identity, 'SYNTHETIC replacement')
                    await asyncio.to_thread(change_owner)
                    before = (fixture.state.last_received, fixture.state.last_pose_capture,
                              fixture.state.pose.copy(), fixture.state.tracking)
                    if change != 'disconnect':
                        self.assertNotEqual(old_generation, fixture.state.socket_generation)
                    fixture.pose_release.set()
                    await event_ready(fixture.pose_applied)
                    self.assertEqual(fixture.state.last_received, before[0])
                    self.assertEqual(fixture.state.last_pose_capture, before[1])
                    self.assertTrue((fixture.state.pose == before[2]).all())
                    self.assertEqual(fixture.state.tracking, before[3])

    async def test_repeated_cancellation_drains_both_jobs_before_observer_removal(self):
        async with held_fusion() as fixture:
            fixture.task.cancel()
            await asyncio.sleep(0)
            fixture.task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(fixture.task.done())
            self.assertFalse(fixture.fusion_finished.is_set())
            self.assertIn(fixture.feed.offer_frame, fixture.state.frame_observers)
            fixture.fusion_release.set()
            await event_ready(fixture.pose_applied)
            self.assertTrue(fixture.fusion_finished.is_set())
            self.assertFalse(fixture.pose_finished.is_set())
            self.assertFalse(fixture.task.done())
            self.assertIn(fixture.feed.offer_frame, fixture.state.frame_observers)
            fixture.task.cancel()
            await asyncio.sleep(0)
            fixture.pose_return.set()
            result = await asyncio.wait_for(asyncio.gather(fixture.task, return_exceptions=True), 2)
            self.assertIsInstance(result[0], asyncio.CancelledError)
            self.assertTrue(fixture.pose_finished.is_set())
            self.assertNotIn(fixture.feed.offer_frame, fixture.state.frame_observers)

    async def test_pose_fault_still_drains_fusion_before_removing_observer(self):
        async with held_fusion(fail_pose=True) as fixture:
            await asyncio.sleep(0)
            self.assertFalse(fixture.task.done())
            self.assertIn(fixture.feed.offer_frame, fixture.state.frame_observers)
            fixture.fusion_release.set()
            result = await asyncio.wait_for(asyncio.gather(fixture.task, return_exceptions=True), 2)
            self.assertIsInstance(result[0], RuntimeError)
            self.assertEqual(str(result[0]), 'controlled pose fault')
            self.assertTrue(fixture.fusion_finished.is_set())
            self.assertNotIn(fixture.feed.offer_frame, fixture.state.frame_observers)
