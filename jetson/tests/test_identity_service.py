import asyncio
import base64
import threading
import time
import unittest
from unittest import mock

import numpy as np
from fastapi import FastAPI
from fastapi.testclient import TestClient

from jetson.camera import CameraFrame
from jetson.identity_api import identity_router
from jetson.identity_engine import Gallery, IdentityError
from jetson.identity_service import IdentityService


def feature(index=0):
    vector = np.zeros(128, dtype=np.float32)
    vector[index] = 1
    return vector


class FakeCamera:
    def __init__(self):
        self.frame = CameraFrame(b'jpeg', time.monotonic(), 1, 'PHONE RGB')

    def snapshot(self):
        return self.frame


class FakeEngine:
    def infer(self, jpeg):
        return [dict(x=.1, y=.2, w=.3, h=.4, conf=.95, embedding=feature())]

    def enroll_embedding(self, jpeg):
        return feature()


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.camera = FakeCamera()
        self.service = IdentityService(self.camera)
        self.service.engine = FakeEngine()
        self.service.gallery = Gallery()
        self.person = self.service.gallery.enroll('Mehdi', feature())

    async def asyncTearDown(self):
        await self.service.close()

    async def test_unknown_faces_and_original_frame_age(self):
        self.service.gallery.remove(self.person['id'])
        await self.service.process_once()
        packet = self.service.snapshot()
        self.assertEqual(packet['faces'][0]['name'], 'Unknown')
        self.assertEqual(packet['frame'], 1)
        self.assertEqual(packet['received'], self.camera.frame.received)
        self.camera.frame = CameraFrame(b'jpeg', time.monotonic() - 2, 1, 'PHONE RGB')
        self.assertFalse(self.service.snapshot()['fresh'])
        self.assertEqual(self.service.snapshot()['faces'], [])

    async def test_replaced_source_and_deleted_gallery_invalidate_names(self):
        await self.service.process_once()
        self.assertEqual(self.service.snapshot()['faces'][0]['name'], 'Mehdi')
        self.service.remove(self.person['id'])
        self.assertEqual(self.service.snapshot()['faces'], [])
        self.service.gallery.enroll('Mehdi', feature())
        self.camera.frame = CameraFrame(b'other', time.monotonic(), 1, 'USB CAMERA')
        self.assertEqual(self.service.snapshot()['faces'], [])

    async def test_slow_inference_does_not_renew_age_or_publish_deleted_name(self):
        entered, release = threading.Event(), threading.Event()
        def detect(jpeg):
            entered.set()
            release.wait(2)
            return FakeEngine().infer(jpeg)
        self.service.engine.infer = detect
        task = asyncio.create_task(self.service.process_once())
        await asyncio.to_thread(entered.wait, 1)
        # The event loop remains responsive while inference is in its own worker.
        await asyncio.wait_for(asyncio.sleep(.01), .1)
        self.service.remove(self.person['id'])
        release.set()
        await task
        self.assertEqual(self.service.snapshot()['faces'], [])

    async def test_deletion_after_generation_check_cannot_retag_old_name_as_current(self):
        class RacingGallery(Gallery):
            reads = 0
            delete_id = None

            @property
            def generation(self):
                value = super().generation
                self.reads += 1
                if self.reads == 2:
                    self.remove(self.delete_id)
                return value

        gallery = RacingGallery()
        person = gallery.enroll('One', feature())
        gallery.delete_id = person['id']
        self.service.gallery = gallery
        await self.service.process_once()
        self.assertEqual(gallery.people(), [])
        self.assertEqual(self.service.snapshot()['faces'], [])

    async def test_busy_enrollment_rejected_without_queue(self):
        await self.service.lock.acquire()
        try:
            with self.assertRaisesRegex(IdentityError, 'busy'):
                await self.service.enroll('Mehdi', b'image')
        finally:
            self.service.lock.release()

    async def test_one_enrollment_waits_for_next_slot_without_starvation_or_extra_queue(self):
        await self.service.lock.acquire()
        first = asyncio.create_task(self.service.enroll('One', b'image'))
        await asyncio.sleep(.01)
        try:
            with self.assertRaisesRegex(IdentityError, 'busy'):
                await self.service.enroll('Two', b'image')
        finally:
            self.service.lock.release()
        result = await first
        self.assertEqual(result['name'], 'One')

    async def test_old_inference_age_is_never_renewed(self):
        entered, release = threading.Event(), threading.Event()
        def detect(jpeg):
            entered.set()
            release.wait(2)
            return FakeEngine().infer(jpeg)
        self.service.engine.infer = detect
        task = asyncio.create_task(self.service.process_once())
        await asyncio.to_thread(entered.wait, 1)
        await asyncio.sleep(1.05)
        release.set()
        await task
        packet = self.service.snapshot()
        self.assertGreaterEqual(packet['age_ms'], 1000)
        self.assertFalse(packet['fresh'])
        self.assertEqual(packet['faces'], [])

    async def test_capture_binding_replaced_and_frame_counter_reset(self):
        await self.service.process_once()
        self.camera.frame = CameraFrame(b'jpeg', time.monotonic(), 0, 'PHONE RGB')
        self.assertEqual(self.service.snapshot()['faces'], [])
        self.camera.frame = CameraFrame(b'jpeg', time.monotonic(), 2, 'PHONE RGB',
                                        capture_binding=object())
        self.assertEqual(self.service.snapshot()['faces'], [])

    async def test_enrollment_cancellation_keeps_worker_gate_until_inference_finishes(self):
        entered, release = threading.Event(), threading.Event()
        def encode(jpeg):
            entered.set()
            release.wait(2)
            return feature()
        self.service.engine.enroll_embedding = encode
        task = asyncio.create_task(self.service.enroll('One', b'image'))
        await asyncio.to_thread(entered.wait, 1)
        task.cancel()
        await asyncio.sleep(.01)
        self.assertTrue(self.service.lock.locked())
        with self.assertRaisesRegex(IdentityError, 'busy'):
            await self.service.enroll('Two', b'image')
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(self.service.lock.locked())
        self.assertEqual(len(self.service.people()), 1)

    async def test_optional_model_startup_and_failure_are_nonfatal(self):
        service = IdentityService(self.camera)
        await service.start()
        self.assertFalse(service.status()['enabled'])
        self.assertFalse(service.status()['ready'])
        await service.close()
        service = IdentityService(self.camera, '/missing/yunet', '/missing/sface')
        await service.start()
        self.assertFalse(service.status()['ready'])
        self.assertTrue(service.status()['error'])
        await service.close()


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.service = IdentityService(FakeCamera())
        self.service.engine = FakeEngine()
        app = FastAPI()
        app.include_router(identity_router(self.service))
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        asyncio.run(self.service.close())

    def test_enroll_list_remove_and_no_embeddings_in_response(self):
        result = self.client.post('/phone/identity/people', json={
            'name': 'Mehdi', 'consent': True, 'image': base64.b64encode(b'image').decode()})
        self.assertEqual(result.status_code, 200, result.text)
        record = result.json()
        self.assertEqual(record['name'], 'Mehdi')
        self.assertNotIn('embedding', record)
        self.assertEqual(len(self.client.get('/phone/identity/people').json()['people']), 1)
        self.assertEqual(self.client.delete('/phone/identity/people/' + record['id']).status_code, 200)
        self.assertEqual(self.client.get('/phone/identity/people').json()['people'], [])

    def test_consent_and_bad_base64_and_body_limit(self):
        for payload in ({'name': 'Mehdi', 'image': 'aW1hZ2U='},
                        {'name': 'Mehdi', 'consent': 'true', 'image': 'aW1hZ2U='},
                        {'name': 'Mehdi', 'consent': True, 'image': 'not base64!'}):
            self.assertEqual(self.client.post('/phone/identity/people', json=payload).status_code, 422)
        result = self.client.post('/phone/identity/people', content=b'x' * (6 * 1024 * 1024 + 1))
        self.assertEqual(result.status_code, 413)

    def test_enrollment_page_and_disabled_status(self):
        self.assertEqual(self.client.get('/phone/identity/enroll').status_code, 200)
        self.service.engine = None
        status = self.client.get('/phone/identity/status').json()
        self.assertFalse(status['ready'])
        result = self.client.post('/phone/identity/people', json={
            'name': 'Mehdi', 'consent': True, 'image': 'aW1hZ2U='})
        self.assertEqual(result.status_code, 503)

    def test_identity_websocket_and_missing_person(self):
        self.assertEqual(self.client.delete('/phone/identity/people/missing').status_code, 404)
        with self.client.websocket_connect('/ws/identity') as ws:
            packet = ws.receive_json()
        self.assertEqual(packet['type'], 'identity')
        self.assertFalse(packet['fresh'])
        self.assertEqual(packet['faces'], [])

    def test_gallery_delete_failure_is_visible_and_retains_enrollment(self):
        person = self.service.gallery.enroll('Mehdi', feature())
        with mock.patch.object(self.service, 'remove', side_effect=IdentityError('Cannot save enrollment gallery')):
            result = self.client.delete('/phone/identity/people/' + person['id'])
        self.assertEqual(result.status_code, 503)
        self.assertEqual(len(self.service.people()), 1)


if __name__ == '__main__':
    unittest.main()
