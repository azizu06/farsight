import concurrent.futures
import asyncio
import json
import threading
import time
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from jetson.mapping.server import create_app
from jetson.mapping.state import MapState
from jetson.mapping.tests.test_mapping import bundle, flat_frame
from jetson.nav import NavService


def frame(number):
    value = flat_frame()
    value.header.update(frame_id=number, t_capture=float(number),
                        _received_monotonic=time.monotonic())
    return value


class DensePublicationTests(unittest.TestCase):
    def app(self):
        return create_app(simulate_rover=True, detector='none', fusion_bypass=True)

    def test_blocked_visual_detail_does_not_block_camera_pose_or_map_status(self):
        app = self.app()
        state = app.state.mapping
        state.start_session(('test', 1), 'Fixture')
        entered, release = threading.Event(), threading.Event()
        original = state.dense.integrate

        def slow(*args, **kwargs):
            entered.set()
            if not release.wait(3):
                raise AssertionError('Test did not release dense inference')
            return original(*args, **kwargs)

        with TestClient(app), mock.patch.object(state.dense, 'integrate', slow), \
                concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(state.process, frame(1))
            try:
                self.assertTrue(entered.wait(1))
                first.result(timeout=.5)
                status = pool.submit(state.status)
                self.assertTrue(status.result(timeout=.5)['connected'])
                newer = frame(2)
                pool.submit(state.process, newer).result(timeout=.5)
                self.assertEqual(state.camera_snapshot()[2], 2)
                self.assertEqual(state.frame_id, 2)
            finally:
                release.set()
                first.result(timeout=2)

    def test_backlog_retains_only_newest_frame_and_still_offers_every_nav_capture(self):
        app = self.app()
        state = app.state.mapping
        state.start_session(('test', 1), 'Fixture')
        seen = []
        state.frame_observers.append(lambda value: seen.append(value.header['frame_id']))
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        original = state.dense.integrate
        calls = []

        def slow(*args, **kwargs):
            calls.append(args[0].copy())
            if len(calls) == 1:
                entered.set()
                release.wait(3)
            result = original(*args, **kwargs)
            if len(calls) == 2:
                finished.set()
            return result

        with TestClient(app), mock.patch.object(state.dense, 'integrate', slow), \
                concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(state.process, frame(1))
            try:
                self.assertTrue(entered.wait(1))
                first.result(timeout=.5)
                for number in range(2, 22):
                    newest = frame(number)
                    newest.transform[0, 3] = number
                    newest.header['transform'] = newest.transform.flatten(order='F').tolist()
                    state.process(newest)
                self.assertEqual(seen, list(range(1, 22)))
                self.assertEqual(state.camera_snapshot()[2], 21)
                report = state.dense_worker.status()
                self.assertEqual(report['queued'], 1)
                self.assertEqual(report['dropped'], 19)
                release.set()
                self.assertTrue(finished.wait(2))
                self.assertEqual(len(calls), 2)
                self.assertGreaterEqual(float(calls[-1][:, 0].min()), 20)
            finally:
                release.set()
                first.result(timeout=2)

    def test_reset_during_detail_preparation_cannot_resurrect_old_cells(self):
        app = self.app()
        state = app.state.mapping
        state.start_session(('test', 1), 'Fixture')
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        original = state.dense.integrate

        def slow(*args, **kwargs):
            entered.set()
            release.wait(3)
            try:
                return original(*args, **kwargs)
            finally:
                finished.set()

        with TestClient(app), mock.patch.object(state.dense, 'integrate', slow), \
                concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(state.process, frame(1))
            try:
                self.assertTrue(entered.wait(1))
                first.result(timeout=.5)
                old_epoch = state.dense.epoch
                pool.submit(state.clear).result(timeout=.5)
                self.assertNotEqual(state.dense.epoch, old_epoch)
                release.set()
                self.assertTrue(finished.wait(1))
                self.assertEqual(len(state.dense.cells), 0)
            finally:
                release.set()
                first.result(timeout=2)

    def test_late_frame_observer_cannot_replace_reconnected_socket_depth(self):
        state = MapState(fusion_bypass=True)
        self.addCleanup(state.dense_worker.close)
        socket_generation = state.start_session(('test', 1), 'Fixture')
        nav = NavService(state)
        entered, release = threading.Event(), threading.Event()

        def delay(value):
            if value.header['frame_id'] == 1:
                entered.set()
                release.wait(2)

        state.frame_observers.extend((delay, nav.offer_frame))
        old = frame(1)
        # A producer cannot predict a future socket's trusted observer tag.
        old.header['_mapping_socket_generation'] = socket_generation + 1
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(state.process, old, socket_generation=socket_generation)
            try:
                self.assertTrue(entered.wait(1))
                current_socket = state.start_session(('test', 1), 'Reconnected fixture')
                newest = frame(2)
                state.process(newest, socket_generation=current_socket)
                self.assertIs(nav.frame, newest)
                release.set()
                pending.result(timeout=1)
                self.assertIs(nav.frame, newest, 'Finishing old observer replaced current-socket depth')
                self.assertIsNone(nav.sensor_fault())
                # Clearing only visual geometry preserves the current socket.
                state.clear()
                after_reset = frame(3)
                state.process(after_reset, socket_generation=current_socket)
                self.assertIs(nav.frame, after_reset)
                self.assertIsNone(nav.sensor_fault())
            finally:
                release.set()
                pending.result(timeout=1)

    def test_accepted_frame_overwrites_producer_supplied_observer_generation(self):
        state = MapState(fusion_bypass=True)
        self.addCleanup(state.dense_worker.close)
        generation = state.start_session(('test', 1), 'Fixture')
        value = frame(1)
        value.header['_mapping_socket_generation'] = generation + 100
        observed = []
        state.frame_observers.append(lambda capture: observed.append(capture.header['_mapping_socket_generation']))
        state.process(value, socket_generation=generation)
        self.assertEqual(observed, [generation])


class PoseReceiptTests(unittest.TestCase):
    def test_delayed_rgbd_processing_does_not_renew_pose_receipt(self):
        state = MapState(fusion_bypass=True)
        self.addCleanup(state.dense_worker.close)
        state.start_session(('test', 1), 'Fixture')
        value = frame(1)
        receipt = time.monotonic() - .75
        value.header['_received_monotonic'] = receipt
        state.process(value)
        self.assertAlmostEqual(state.last_received, receipt, places=6)
        self.assertGreaterEqual(state.status()['age_ms'], 750)

    def test_disconnected_pose_cannot_renew_tracking_or_receipt(self):
        state = MapState(fusion_bypass=True)
        self.addCleanup(state.dense_worker.close)
        state.start_session(('test', 1), 'Fixture')
        state.connected = False
        value = frame(1)
        state.observe_pose(value.header, value.transform, time.monotonic())
        self.assertEqual(state.tracking, 'not_available')
        self.assertEqual(state.last_received, 0.)


class PhoneOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_disconnect_drains_cleanup_and_releases_owner(self):
        app = create_app(detector='none', fusion_bypass=True)
        state = app.state.mapping
        endpoint = next(route.endpoint for route in app.routes if getattr(route, 'path', '') == '/phone')

        class Socket:
            def __init__(self, session):
                self.session, self.code = session, None
                self.messages = asyncio.Queue()

            async def accept(self):
                pass

            async def close(self, code=1000, reason=''):
                self.code = code

            async def receive_text(self):
                return json.dumps(dict(version=1, type='hello', session_id=self.session, map_epoch=1,
                                       supports_scene_depth=True))

            async def receive(self):
                return await self.messages.get()

        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        original = asyncio.to_thread

        async def delayed(function, *args, **kwargs):
            if function.__name__ == 'disconnect_sensor' and not entered.is_set():
                def blocked():
                    entered.set()
                    release.wait(2)
                    try:
                        return function(*args, **kwargs)
                    finally:
                        finished.set()
                return await original(blocked)
            return await original(function, *args, **kwargs)

        initial = replacement = None
        with mock.patch('jetson.mapping.server.asyncio.to_thread', delayed):
            try:
                initial = asyncio.create_task(endpoint(Socket('old')))
                await self.wait(lambda: state.connected and state.identity == ('old', 1))
                initial.cancel()
                await self.wait(entered.is_set)
                self.assertFalse(state.connected)
                initial.cancel()
                await asyncio.sleep(0)
                competing = Socket('competing')
                await endpoint(competing)
                self.assertEqual(competing.code, 1008, 'Cleanup must retain ownership while the worker is live')
                self.assertIsNotNone(app.state.phone_owner)
                initial.cancel()
                await asyncio.sleep(0)
                release.set()
                await self.wait(finished.is_set)
                with self.assertRaises(asyncio.CancelledError):
                    await initial
                self.assertIsNone(app.state.phone_owner, 'Cancelled cleanup left phone ownership permanently held')
                second = Socket('new')
                replacement = asyncio.create_task(endpoint(second))
                await self.wait(lambda: state.connected and state.identity == ('new', 1))
                self.assertIsNone(second.code)
            finally:
                release.set()
                for task in (initial, replacement):
                    if task is not None and not task.done():
                        task.cancel()
                        try:
                            await task
                        except asyncio.CancelledError:
                            pass
                await self.wait(finished.is_set)
                await original(state.dense_worker.close)

    async def test_cancelled_hello_drains_session_initialization_before_releasing_owner(self):
        app = create_app(detector='none', fusion_bypass=True)
        state = app.state.mapping
        endpoint = next(route.endpoint for route in app.routes if getattr(route, 'path', '') == '/phone')

        class Socket:
            def __init__(self, session):
                self.session, self.code = session, None
                self.messages = asyncio.Queue()

            async def accept(self):
                pass

            async def close(self, code=1000, reason=''):
                self.code = code

            async def receive_text(self):
                return json.dumps(dict(version=1, type='hello', session_id=self.session, map_epoch=1,
                                       supports_scene_depth=True))

            async def receive(self):
                return await self.messages.get()

        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        original = asyncio.to_thread

        async def delayed(function, *args, **kwargs):
            if function.__name__ == 'start_session' and args[0] == ('old', 1):
                def blocked():
                    entered.set()
                    release.wait(2)
                    try:
                        return function(*args, **kwargs)
                    finally:
                        finished.set()
                return await original(blocked)
            if function.__name__ == 'disconnect_sensor':
                # Keep cleanup deterministic: one event-loop turn completes the old
                # unprotected path, while protected initialization remains blocked.
                return function(*args, **kwargs)
            return await original(function, *args, **kwargs)

        initial = replacement = None
        with mock.patch('jetson.mapping.server.asyncio.to_thread', delayed):
            try:
                initial = asyncio.create_task(endpoint(Socket('old')))
                await self.wait(entered.is_set)
                initial.cancel()
                await asyncio.sleep(0)
                competing = Socket('competing')
                competing_task = asyncio.create_task(endpoint(competing))
                await self.wait(lambda: competing.code is not None or state.identity == ('competing', 1))
                self.assertEqual(competing.code, 1008, 'Cancelled initialization released ownership before draining')
                await competing_task
                self.assertFalse(initial.done(), 'Endpoint completed while its initialization worker was live')
                initial.cancel()  # Repeated shutdown cancellation must not abandon the drain.
                await asyncio.sleep(0)
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await initial
                self.assertTrue(finished.is_set())
                self.assertIsNone(app.state.phone_owner)
                self.assertFalse(state.connected)
                replacement = asyncio.create_task(endpoint(Socket('new')))
                await self.wait(lambda: state.connected and state.identity == ('new', 1))
                self.assertEqual(state.identity, ('new', 1), 'Late old initialization replaced the new session')
            finally:
                release.set()
                for task in (initial, locals().get('competing_task'), replacement):
                    if task is not None and not task.done():
                        task.cancel()
                        try:
                            await task
                        except asyncio.CancelledError:
                            pass
                await self.wait(finished.is_set)
                await original(state.dense_worker.close)

    async def live_socket_after_reset(self, lane):
        app = create_app(detector='none', fusion_bypass=True)
        state = app.state.mapping
        endpoint = next(route.endpoint for route in app.routes if getattr(route, 'path', '') == '/phone')
        reset = next(route.endpoint for route in app.routes if getattr(route, 'path', '') == '/map/reset')

        class Socket:
            def __init__(self):
                self.messages = asyncio.Queue()

            async def accept(self):
                pass

            async def close(self, **kwargs):
                pass

            async def receive_text(self):
                return json.dumps(dict(version=1, type='hello', session_id='test', map_epoch=1,
                                       supports_scene_depth=True))

            async def receive(self):
                return await self.messages.get()

        completed = asyncio.Event()
        original = asyncio.to_thread
        job = 'observe_pose' if lane == 'pose' else 'process'

        async def observed(function, *args, **kwargs):
            result = await original(function, *args, **kwargs)
            if function.__name__ == job:
                completed.set()
            return result

        phone = Socket()
        task = None
        with mock.patch('jetson.mapping.server.asyncio.to_thread', observed):
            try:
                task = asyncio.create_task(endpoint(phone))
                await self.wait(lambda: state.connected and state.identity == ('test', 1))
                await reset()
                value = frame(4)
                value.transform[0, 3] = 4
                value.header['transform'] = value.transform.flatten(order='F').tolist()
                packet = (dict(type='websocket.receive', text=json.dumps(dict(value.header, type='pose')))
                          if lane == 'pose' else dict(type='websocket.receive', bytes=bundle(value)))
                await phone.messages.put(packet)
                await asyncio.wait_for(completed.wait(), 1)
                self.assertEqual(state.tracking, 'normal', 'Map reset invalidated the connected phone socket')
                self.assertEqual(float(state.pose[0, 3]), 4.)
                self.assertEqual(state.last_pose_capture, 4.)
                if lane == 'rgbd':
                    self.assertEqual(state.camera_snapshot()[2], 4)
            finally:
                if task is not None and not task.done():
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                await original(state.dense_worker.close)

    async def test_connected_socket_pose_still_publishes_after_map_reset(self):
        await self.live_socket_after_reset('pose')

    async def test_connected_socket_rgbd_still_publishes_after_map_reset(self):
        await self.live_socket_after_reset('rgbd')

    async def test_old_phone_cleanup_finishes_before_new_phone_can_start(self):
        app = create_app(simulate_rover=True, detector='none', fusion_bypass=True)
        endpoint = next(route.endpoint for route in app.routes if getattr(route, 'path', '') == '/phone')

        class Socket:
            def __init__(self, session):
                self.session, self.code = session, None
                self.messages = asyncio.Queue()

            async def accept(self):
                pass

            async def close(self, code=1000, reason=''):
                self.code = code

            async def receive_text(self):
                return json.dumps(dict(version=1, type='hello', session_id=self.session,
                                       map_epoch=1, supports_scene_depth=True))

            async def receive(self):
                return await self.messages.get()

        entered, release = asyncio.Event(), asyncio.Event()
        original = asyncio.to_thread

        async def delayed(function, *args, **kwargs):
            if function.__name__ == 'disconnect_sensor':
                entered.set()
                await release.wait()
            return await original(function, *args, **kwargs)

        first = Socket('first')
        second = Socket('second')
        with mock.patch('jetson.mapping.server.asyncio.to_thread', delayed):
            initial = asyncio.create_task(endpoint(first))
            try:
                for _ in range(100):
                    if app.state.mapping.identity == ('first', 1):
                        break
                    await asyncio.sleep(.005)
                await first.messages.put(dict(type='websocket.disconnect'))
                await asyncio.wait_for(entered.wait(), 1)
                self.assertFalse(app.state.mapping.connected,
                                 'Disconnect must withdraw live authority before waiting for cleanup')
                competing = asyncio.create_task(endpoint(second))
                await asyncio.sleep(.05)
                self.assertEqual(second.code, 1008,
                                 'A delayed old cleanup must still own the stream')
                await competing
                release.set()
                await initial
                third = Socket('third')
                replacement = asyncio.create_task(endpoint(third))
                for _ in range(100):
                    if app.state.mapping.identity == ('third', 1):
                        break
                    await asyncio.sleep(.005)
                self.assertTrue(app.state.mapping.connected)
                await third.messages.put(dict(type='websocket.disconnect'))
                await replacement
            finally:
                release.set()
                for task in (initial, locals().get('competing'), locals().get('replacement')):
                    if task is not None and not task.done():
                        task.cancel()
                        try:
                            await task
                        except asyncio.CancelledError:
                            pass

    async def wait(self, predicate):
        async with asyncio.timeout(1):
            while not predicate():
                await asyncio.sleep(.001)

    async def late_job(self, lane, next_session):
        app = create_app(detector='none', fusion_bypass=True)
        state = app.state.mapping
        endpoint = next(route.endpoint for route in app.routes if getattr(route, 'path', '') == '/phone')

        class Socket:
            def __init__(self, session, permits=False):
                self.session, self.permits = session, permits
                self.messages, self.failed = asyncio.Queue(), asyncio.Event()

            async def accept(self):
                pass

            async def close(self, **kwargs):
                pass

            async def receive_text(self):
                return json.dumps(dict(version=1, type='hello', session_id=self.session, map_epoch=1,
                                       supports_scene_depth=True, supports_capture_permits=self.permits))

            async def receive(self):
                return await self.messages.get()

            async def send_json(self, message):
                await self.failed.wait()
                raise RuntimeError('Fixture permit sender disconnected')

        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        original = asyncio.to_thread
        job = 'observe_pose' if lane == 'pose' else 'process'

        async def delayed(function, *args, **kwargs):
            if function.__name__ == job:
                def blocked():
                    entered.set()
                    release.wait(2)
                    try:
                        return function(*args, **kwargs)
                    finally:
                        finished.set()
                return await original(blocked)
            return await original(function, *args, **kwargs)

        first, second = Socket('first', True), Socket(next_session)
        initial = replacement = None
        with mock.patch('jetson.mapping.server.asyncio.to_thread', delayed):
            try:
                initial = asyncio.create_task(endpoint(first))
                await self.wait(lambda: state.connected and state.identity == ('first', 1))
                old_generation = state.socket_generation
                value = frame(1)
                value.header.update(session_id='first', t_capture=100.)
                value.transform[0, 3] = 99
                value.header['transform'] = value.transform.flatten(order='F').tolist()
                packet = (dict(type='websocket.receive', text=json.dumps(dict(value.header, type='pose')))
                          if lane == 'pose' else dict(type='websocket.receive', bytes=bundle(value)))
                await first.messages.put(packet)
                await self.wait(entered.is_set)
                first.failed.set()
                await initial
                replacement = asyncio.create_task(endpoint(second))
                await self.wait(lambda: state.connected and state.socket_generation > old_generation
                                and state.identity == (next_session, 1))
                release.set()
                await self.wait(finished.is_set)
                self.assertEqual(state.tracking, 'not_available', 'Old worker renewed the new socket tracking')
                self.assertEqual(float(state.pose[0, 3]), 0., 'Old worker replaced the new socket pose')
                self.assertEqual(state.frame_id, -1, 'Old decoded frame became the new socket camera')
                self.assertIsNone(state.jpeg)
            finally:
                release.set()
                for task in (initial, replacement):
                    if task is not None and not task.done():
                        task.cancel()
                        try:
                            await task
                        except asyncio.CancelledError:
                            pass
                await original(state.dense_worker.close)

    async def test_cancelled_raw_pose_cannot_publish_after_sensor_identity_changes(self):
        await self.late_job('pose', 'second')

    async def test_cancelled_raw_pose_cannot_publish_after_same_identity_reconnect(self):
        await self.late_job('pose', 'first')

    async def test_cancelled_decoded_frame_cannot_publish_after_sensor_identity_changes(self):
        await self.late_job('rgbd', 'second')

    async def test_cancelled_decoded_frame_cannot_publish_after_same_identity_reconnect(self):
        await self.late_job('rgbd', 'first')


if __name__ == '__main__':
    unittest.main()
