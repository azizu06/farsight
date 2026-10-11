import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from jetson.intercom import IntercomHub, intercom_router
from jetson.mapping.server import create_app


FRAME = bytes(range(256)) * 2 + bytes(range(128))  # one 640-byte, 20 ms PCM frame


class IntercomRouteTests(unittest.TestCase):
    def setUp(self):
        self.hub = IntercomHub()
        app = FastAPI()
        app.include_router(intercom_router(self.hub))
        self.client = TestClient(app)

    def test_canonical_jetson_server_registers_the_audio_route(self):
        app = create_app(simulate_rover=True, detector="none")
        self.assertIsInstance(app.state.intercom, IntercomHub)
        with TestClient(app) as client:
            with client.websocket_connect("/ws/intercom?role=phone") as phone:
                self.assertEqual(phone.receive_json(), {"type": "peer", "connected": False})
                self.assertEqual(phone.receive_json(), {"type": "talk", "active": False})

    def test_quest_toggle_enables_simultaneous_audio_in_both_directions(self):
        with self.client.websocket_connect("/ws/intercom?role=phone") as phone:
            self.assertEqual(phone.receive_json(), {"type": "peer", "connected": False})
            self.assertEqual(phone.receive_json(), {"type": "talk", "active": False})
            with self.client.websocket_connect("/ws/intercom?role=quest") as quest:
                self.assertEqual(quest.receive_json(), {"type": "peer", "connected": True})
                self.assertEqual(quest.receive_json(), {"type": "talk", "active": False})
                self.assertEqual(phone.receive_json(), {"type": "peer", "connected": True})

                quest.send_json({"type": "talk", "active": True})
                self.assertEqual(quest.receive_json(), {"type": "talk", "active": True})
                self.assertEqual(phone.receive_json(), {"type": "talk", "active": True})

                quest.send_bytes(FRAME)
                self.assertEqual(phone.receive_bytes(), FRAME)
                phone.send_bytes(FRAME)
                self.assertEqual(quest.receive_bytes(), FRAME)

                quest.send_json({"type": "talk", "active": False})
                self.assertEqual(quest.receive_json(), {"type": "talk", "active": False})
                self.assertEqual(phone.receive_json(), {"type": "talk", "active": False})

    def test_audio_is_not_replayed_after_talk_is_disabled(self):
        with self.client.websocket_connect("/ws/intercom?role=phone") as phone:
            phone.receive_json(); phone.receive_json()
            with self.client.websocket_connect("/ws/intercom?role=quest") as quest:
                quest.receive_json(); quest.receive_json(); phone.receive_json()
                quest.send_json({"type": "talk", "active": True})
                quest.receive_json(); phone.receive_json()
                quest.send_json({"type": "talk", "active": False})
                quest.receive_json(); phone.receive_json()
                quest.send_bytes(FRAME)
                quest.send_json({"type": "talk", "active": True})
                self.assertEqual(quest.receive_json(), {"type": "talk", "active": True})
                self.assertEqual(phone.receive_json(), {"type": "talk", "active": True})
                quest.send_bytes(FRAME)
                self.assertEqual(phone.receive_bytes(), FRAME)

    def test_only_one_socket_per_role_can_connect(self):
        with self.client.websocket_connect("/ws/intercom?role=phone") as phone:
            phone.receive_json(); phone.receive_json()
            with self.assertRaises(WebSocketDisconnect):
                with self.client.websocket_connect("/ws/intercom?role=phone") as duplicate:
                    duplicate.receive_text()

    def test_rejects_non_pcm_frame_size(self):
        with self.client.websocket_connect("/ws/intercom?role=phone") as phone:
            phone.receive_json(); phone.receive_json()
            with self.client.websocket_connect("/ws/intercom?role=quest") as quest:
                quest.receive_json(); quest.receive_json(); phone.receive_json()
                quest.send_json({"type": "talk", "active": True})
                quest.receive_json(); phone.receive_json()
                quest.send_bytes(b"short")
                with self.assertRaises(WebSocketDisconnect):
                    quest.receive_text()


if __name__ == "__main__":
    unittest.main()
