"""Logical path faults and command idempotence; no serial pump or hardware."""

import asyncio
from collections import OrderedDict
import json
import time
import unittest
from unittest.mock import patch

from jetson.control.bonded import BondedControlHub
from jetson.control.state import ControlState


class Clock:
    now = 100.0
    def __call__(self): return self.now


async def drain(path, seen):
    while True:
        status = await path.queue.get()
        if status is None:
            return
        seen.append(status)


async def until(predicate):
    while not predicate():
        await asyncio.sleep(.001)


class BondedPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.clock, self.camera = Clock(), [True]
        self.state = ControlState(clock=self.clock, camera_ready=lambda: self.camera[0])
        self.state.set_connected(True)
        self.state.update_range(100)
        self.hub = BondedControlHub(self.state)
        self.a = await self.hub.attach()
        self.b = await self.hub.attach(self.a.secret)
        self.seen = [[], []]
        self.readers = [asyncio.create_task(drain(path, seen)) for path, seen in zip((self.a, self.b), self.seen)]
        self.seq = 0
        self.release = self.message(deadman=False)
        self.assertTrue(self.hub.accept(self.release, self.a.path_id))

    async def asyncTearDown(self):
        await self.hub.close()
        await asyncio.gather(*self.readers, return_exceptions=True)

    def message(self, **changes):
        self.seq += 1
        permit = self.state.status(issue_permit=True)["permit"]
        message = dict(seq=self.seq, session_id=self.a.session_id, permit=permit,
                       deadman=True, v=.5, w=0, pan=0, mode="teleop",
                       video_fresh=True, video_age_ms=0)
        message.update(changes)
        return message

    def drive(self):
        command = self.message()
        self.assertTrue(self.hub.accept(command, self.a.path_id))
        self.assertEqual(self.state.output()[:2], (50, 50))
        return command

    async def test_private_auth_two_path_limit_and_retired_secret(self):
        self.assertEqual(len(self.a.secret), 64)
        self.assertEqual(len(bytes.fromhex(self.a.secret)), 32)
        self.assertNotEqual(self.a.path_id, self.b.path_id)
        with self.assertRaises(ValueError): await self.hub.attach(self.a.secret)
        with patch("jetson.control.bonded.hmac.compare_digest", wraps=__import__("hmac").compare_digest) as compare:
            with self.assertRaises(ValueError): await self.hub.attach("0" * 64)
            compare.assert_called_once()
        old_secret = self.a.secret
        await self.hub.detach(self.a.path_id)
        await self.hub.detach(self.b.path_id)
        with self.assertRaises(ValueError): await self.hub.attach(old_secret)
        new = await self.hub.attach()
        self.assertNotEqual(new.session_id, self.a.session_id)
        self.assertNotEqual(new.secret, old_secret)
        self.assertTrue(self.state.requires_release)
        await self.hub.detach(new.path_id)

    async def test_one_detach_keeps_motion_and_last_detach_ends_owner_immediately(self):
        self.drive()
        await self.hub.detach(self.a.path_id)
        self.assertEqual(self.state.session_id, self.b.session_id)
        self.assertEqual(self.state.output()[:2], (50, 50))
        self.assertFalse(self.state.requires_release)
        await self.hub.detach(self.b.path_id)
        self.assertIsNone(self.state.session_id)
        self.assertEqual(self.state.output()[:2], (0, 0))
        self.assertTrue(self.state.requires_release)

    async def test_canonical_duplicate_is_applied_once_and_never_renews_deadline(self):
        with patch.object(self.state, "accept", wraps=self.state.accept) as accepted:
            command = self.drive()
            deadline = self.state.deadline
            self.clock.now += .1
            reversed_keys = dict(reversed(list(command.items())))
            self.assertFalse(self.hub.accept(reversed_keys, self.b.path_id))
            self.assertEqual(accepted.call_count, 1)
            self.assertEqual(self.state.deadline, deadline)
            self.assertEqual(self.state.output()[:2], (50, 50))

    async def test_old_release_copy_after_repress_does_not_stop_or_clear_any_latch(self):
        self.drive()
        released = self.message(deadman=False)
        self.assertTrue(self.hub.accept(released, self.a.path_id))
        self.drive()
        self.assertFalse(self.hub.accept(released, self.b.path_id))
        self.assertEqual(self.state.output()[:2], (50, 50))
        self.state.stop("camera_stale")
        self.assertFalse(self.hub.accept(released, self.b.path_id))
        self.assertTrue(self.state.requires_release)

    async def test_obsolete_unseen_command_is_ignored_without_stopping_new_motion(self):
        old = self.message(v=-.5)
        self.drive()
        self.assertFalse(self.hub.accept(old, self.b.path_id))
        self.assertEqual(self.state.output()[:2], (50, 50))

    async def test_conflicting_duplicate_fails_closed_and_requires_real_new_release(self):
        command = self.drive()
        self.assertFalse(self.hub.accept(dict(command, v=-.5), self.b.path_id))
        self.assertEqual(self.state.output()[:2], (0, 0))
        self.assertEqual(self.state.reason, "conflicting_duplicate")
        self.assertTrue(self.state.requires_release)
        self.assertFalse(self.hub.accept(command, self.a.path_id))
        self.assertFalse(self.hub.accept(self.message(), self.b.path_id))
        self.assertTrue(self.hub.accept(self.message(deadman=False), self.a.path_id))
        self.drive()

    async def test_expired_motion_copy_drops_but_actual_lease_expiry_still_stops_both_paths(self):
        self.drive()
        original = self.state.deadline
        late = self.message()
        self.state.permits[late["permit"]] = 100.01
        self.clock.now = 100.02
        with patch.object(self.state, "accept", wraps=self.state.accept) as accepted:
            self.assertFalse(self.hub.accept(late, self.b.path_id))
            accepted.assert_not_called()
        self.assertEqual(self.state.deadline, original)
        self.assertEqual(self.state.output()[:2], (50, 50))
        self.clock.now = 100.26
        self.assertFalse(self.hub.accept(late, self.a.path_id))
        self.assertEqual(self.state.output()[:2], (0, 0))
        self.assertTrue(self.state.requires_release)
        self.assertFalse(self.hub.accept(self.message(), self.b.path_id))

    async def test_camera_view_range_and_serial_guards_remain_authoritative(self):
        command = self.drive()
        self.camera[0] = False
        self.hub.accept(command, self.b.path_id)
        self.assertEqual(self.state.output()[:2], (0, 0))
        self.assertTrue(self.state.requires_release)
        self.camera[0] = True
        self.hub.accept(self.message(deadman=False), self.a.path_id)
        self.assertFalse(self.hub.accept(self.message(video_age_ms=501), self.b.path_id))
        self.assertEqual(self.state.reason, "video_stale")
        self.hub.accept(self.message(deadman=False), self.a.path_id)
        self.state.update_range(10)
        self.assertTrue(self.hub.accept(self.message(), self.a.path_id))
        self.assertEqual(self.state.output()[:2], (0, 0))
        self.state.set_connected(False)
        self.assertFalse(self.hub.accept(self.message(), self.b.path_id))
        self.assertTrue(self.state.requires_release)

    async def test_bad_or_unattached_messages_cannot_bypass_validation(self):
        for changes in (dict(seq=True), dict(seq=-1), dict(seq=2**53), dict(session_id="wrong"),
                        dict(v=float("nan")), dict(permit=None), dict(extra="x" * 3000)):
            self.hub.accept(self.message(deadman=False), self.a.path_id)
            self.assertFalse(self.hub.accept(self.message(**changes), self.a.path_id))
            self.assertTrue(self.state.requires_release)
        self.hub.accept(self.message(deadman=False), self.a.path_id)
        self.drive()
        self.assertFalse(self.hub.accept(b"invalid", "not-attached"))
        self.assertEqual(self.state.output()[:2], (50, 50))
        self.assertFalse(self.hub.accept(b"invalid", self.b.path_id))
        self.assertEqual(self.state.reason, "binary_control_rejected")

    async def test_cache_is_bounded_and_evicted_obsolete_release_stays_idempotent(self):
        for _ in range(300):
            self.hub.accept(self.message(deadman=False), self.a.path_id)
        self.assertEqual(len(self.hub._cache), 256)
        self.assertIsInstance(self.hub._cache, OrderedDict)
        self.assertNotIn(self.release["seq"], self.hub._cache)
        self.drive()
        self.assertFalse(self.hub.accept(self.release, self.b.path_id))
        self.assertEqual(self.state.output()[:2], (50, 50))


class BondedPublicationTests(unittest.IsolatedAsyncioTestCase):
    def state(self):
        state = ControlState()
        state.set_connected(True)
        state.update_range(100)
        return state

    async def test_shared_publisher_fans_identical_status_once_and_records_one_logical_stream(self):
        records, recorded_times = [], []
        async def record(status):
            records.append(status)
            recorded_times.append(time.monotonic())
        state = self.state()
        hub = BondedControlHub(state, on_status=record)
        with patch.object(state, "begin", wraps=state.begin) as begin, \
                patch.object(state, "status", wraps=state.status) as status:
            a = await hub.attach()
            b = await hub.attach(a.secret)
            seen = [[], []]
            readers = [asyncio.create_task(drain(path, values)) for path, values in zip((a, b), seen)]
            try:
                await asyncio.wait_for(until(lambda: len(seen[0]) >= 6 and len(records) >= 3), .3)
                self.assertEqual(seen[0], seen[1])
                self.assertTrue(all(x is y for x, y in zip(*seen)))
                self.assertEqual(status.call_count, len(seen[0]))
                begin.assert_called_once()
                self.assertEqual([value["status_seq"] for value in seen[0]], list(range(1, len(seen[0]) + 1)))
                self.assertLess(len(records), len(seen[0]))
                self.assertTrue(all(b - a >= .045 for a, b in zip(recorded_times, recorded_times[1:])))
                self.assertNotIn(a.secret, json.dumps(seen + records))
                self.assertTrue(all("secret" not in packet for packet in records))
            finally:
                await hub.close()
                await asyncio.gather(*readers)

    async def test_nav_lane_is_decided_once_per_status_for_both_paths(self):
        from jetson.mapping.state import MapState
        from jetson.nav import NavService
        nav = NavService(MapState())
        state = ControlState(navigator=nav)
        nav.attach(state)
        state.set_connected(True)
        hub = BondedControlHub(state)
        a = await hub.attach()
        b = await hub.attach(a.secret)
        seen = [[], []]
        readers = [asyncio.create_task(drain(path, values)) for path, values in zip((a, b), seen)]
        try:
            await asyncio.wait_for(until(lambda: len(seen[0]) >= 2), .3)
            self.assertIn("path", seen[0][-1]["nav"])  # legacy until a command opts in
            self.assertTrue(hub.accept(dict(session_id=a.session_id, seq=0, deadman=False, nav_lane="revision"),
                                       a.path_id))
            start = len(seen[0])
            await asyncio.wait_for(until(lambda: len(seen[0]) >= start + 6 and len(seen[1]) >= start + 6), .5)
            self.assertTrue(all(x is y for x, y in zip(seen[0], seen[1])))  # identical copies per status_seq
            self.assertTrue(all("path" not in status["nav"] for status in seen[0][start + 1:start + 6]))
        finally:
            await hub.close()
            await asyncio.gather(*readers)

    async def test_full_queue_faults_only_stalled_path_and_terminates_it(self):
        state = self.state()
        hub = BondedControlHub(state)
        a = await hub.attach()
        b = await hub.attach(a.secret)
        seen = []
        reader = asyncio.create_task(drain(b, seen))
        try:
            await asyncio.wait_for(until(lambda: a.path_id not in hub.paths and len(seen) >= 3), .2)
            self.assertEqual(await a.queue.get(), None)
            self.assertEqual(state.session_id, b.session_id)
            self.assertEqual(set(hub.paths), {b.path_id})
        finally:
            await hub.close()
            await reader

    async def test_both_stalled_paths_end_owner_and_release_all_queues(self):
        state = self.state()
        hub = BondedControlHub(state)
        a = await hub.attach()
        b = await hub.attach(a.secret)
        try:
            await asyncio.wait_for(until(lambda: not hub.paths), .2)
            self.assertIsNone(state.session_id)
            self.assertTrue(state.requires_release)
            self.assertIsNone(await a.queue.get())
            self.assertIsNone(await b.queue.get())
            self.assertIsNone(hub._publisher)
        finally:
            await hub.close()

    async def test_blocked_recording_is_bounded_and_closed_without_blocking_status(self):
        started, finished = asyncio.Event(), asyncio.Event()
        async def record(status):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                finished.set()
        hub = BondedControlHub(self.state(), on_status=record)
        a = await hub.attach()
        seen = []
        reader = asyncio.create_task(drain(a, seen))
        await started.wait()
        await asyncio.wait_for(until(lambda: len(seen) >= 4), .2)
        recorder = hub._recorder
        self.assertFalse(recorder.done())
        await hub.detach(a.path_id)
        self.assertTrue(finished.is_set())
        self.assertTrue(recorder.done())
        await reader
        await hub.close()

    async def test_existing_legacy_owner_is_not_replaced_and_closed_hub_cannot_restart(self):
        state = self.state()
        legacy = state.begin()
        hub = BondedControlHub(state)
        with self.assertRaises(ValueError): await hub.attach()
        self.assertEqual(state.session_id, legacy)
        await hub.close()
        self.assertEqual(state.session_id, legacy)
        with self.assertRaises(ValueError): await hub.attach()

    async def test_publisher_fault_revokes_owner_and_terminates_paths(self):
        state = self.state()
        hub = BondedControlHub(state)
        a = await hub.attach()
        b = await hub.attach(a.secret)
        with patch.object(state, "status", side_effect=RuntimeError("injected publisher fault")):
            await asyncio.wait_for(until(lambda: not hub.paths), .2)
        self.assertIsNone(state.session_id)
        self.assertTrue(state.requires_release)
        self.assertIsNone(await a.queue.get())
        self.assertIsNone(await b.queue.get())
        await hub.close()


if __name__ == "__main__":
    unittest.main()
