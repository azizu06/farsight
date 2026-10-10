import json

from jetson.camera import CameraFrame
from jetson.mission import Mission
from jetson.replay import ReplayRun


def test_mission_recording_round_trip(tmp_path):
    mission = Mission(tmp_path, record=True)
    mission.record("frame", CameraFrame(b"jpeg", 10, 7, "USB CAMERA"), elapsed=0)
    mission.record("detections", {"frame": 7, "boxes": []}, elapsed=.2)
    mission.record("control", {"range_cm": 64, "permit": "secret"}, elapsed=.3)
    mission.finish_recording(elapsed=.5)
    run = ReplayRun(mission.directory)
    assert run.duration == .5
    assert run.events[0]["path"].read_bytes() == b"jpeg"
    assert run.events[1]["packet"] == {"frame": 7, "boxes": []}
    assert run.events[2]["packet"] == {"range_cm": 64}
    assert json.loads((mission.directory / "replay.json").read_text())["version"] == 1


def test_replay_health_camera_and_read_only_control(tmp_path):
    import cv2
    import numpy as np
    import time
    from fastapi.testclient import TestClient
    from jetson.mapping.server import create_app

    mission = Mission(tmp_path, record=True)
    jpeg = cv2.imencode('.jpg', np.zeros((48, 64, 3), dtype=np.uint8))[1].tobytes()
    mission.record('frame', CameraFrame(jpeg, 0, 7, 'SIMULATED'), elapsed=0)
    mission.record('detections', dict(frame=7, boxes=[], fps=10, source='SIMULATED'), elapsed=0)
    mission.record('control', dict(range_cm=64, left_pwm=100, right_pwm=100), elapsed=0)
    mission.finish_recording(elapsed=.5)
    with TestClient(create_app(replay=mission.directory)) as client:
        time.sleep(.05)
        health = client.get('/health').json()
        assert health['replay'] is True
        assert health['camera_source'] == 'REPLAY: SIMULATED'
        assert client.get('/camera.jpg').status_code == 200
        assert client.get('/control/status').json()['range_cm'] == 64
        with client.websocket_connect('/ws/control') as ws:
            ws.send_json(dict(deadman=True, v=1, w=1))
            status = ws.receive_json()
            assert status['replay'] is True
            assert status['left_pwm'] == status['right_pwm'] == 0
            assert status['rover'] is False
            assert status['permit'] == ''
        with client.websocket_connect('/ws/detections') as ws:
            assert ws.receive_json()['replay'] is True


def test_replay_refuses_physical_or_other_camera_sources(tmp_path):
    import pytest
    from jetson.mapping.server import create_app
    for options in [dict(serial_port='/dev/ttyACM0'), dict(camera_device=0), dict(demo=True),
                    dict(simulate_rover=True), dict(record=True)]:
        with pytest.raises(ValueError, match='Replay'):
            create_app(replay=tmp_path, **options)


def test_replay_preserves_timing_and_loops(tmp_path):
    import asyncio
    mission = Mission(tmp_path, record=True)
    mission.record('frame', CameraFrame(b'jpeg', 0, 1, 'USB CAMERA'), elapsed=.1)
    mission.record('control', {'range_cm': 42}, elapsed=.3)
    mission.finish_recording(elapsed=.6)
    run = ReplayRun(mission.directory)
    now = [0.0]
    seen = []

    async def sleep(delay):
        now[0] += delay

    def publish(event):
        if event:
            seen.append((event['kind'], round(now[0], 3)))
            if len(seen) == 4:
                raise StopAsyncIteration

    async def exercise():
        try:
            await run.play(publish, sleep=sleep, clock=lambda: now[0])
        except StopAsyncIteration:
            pass

    asyncio.run(exercise())
    assert seen == [('frame', .1), ('control', .3), ('frame', .7), ('control', .9)]


def test_replay_loop_has_new_frame_identity_with_matching_detections(tmp_path):
    from jetson.camera import CameraService
    from jetson.mapping.state import MapState
    from jetson.replay import ReplaySession, ReplayControl, ReplayPerception
    mission = Mission(tmp_path, record=True)
    mission.record('frame', CameraFrame(b'jpeg', 0, 7, 'USB CAMERA'), elapsed=0)
    mission.record('detections', dict(frame=7, boxes=[], source='USB CAMERA'), elapsed=0)
    camera = CameraService(MapState(False))
    perception = ReplayPerception(camera)
    session = ReplaySession(mission.directory, camera, perception, ReplayControl())
    identities = []
    for _ in range(2):
        session.publish(None)
        for event in session.run.events:
            session.publish(event)
        identities.append(camera.snapshot().frame_id)
        assert perception.snapshot()['frame'] == camera.snapshot().frame_id
    assert identities[1] > identities[0]


def test_demo_records_frames_detections_and_control_replies_in_mission(tmp_path):
    import time
    from fastapi.testclient import TestClient
    from jetson.mapping.server import create_app
    app = create_app(demo=True, simulate_rover=True, mission_dir=tmp_path, record=True)
    with TestClient(app) as client:
        with client.websocket_connect('/ws/control') as ws:
            reply = ws.receive_json()
            assert reply['session_id']
            time.sleep(.4)
        assert client.get('/health').json()['recording'] is True
    run = ReplayRun(app.state.mission.directory)
    assert {event['kind'] for event in run.events} == {'frame', 'detections', 'control'}
    assert run.duration >= .4
    assert all('permit' not in event.get('packet', {}) and 'session_id' not in event.get('packet', {})
               for event in run.events)
    assert all(event['path'].read_bytes().startswith(b'\xff\xd8')
               for event in run.events if event['kind'] == 'frame')


def test_replay_rejects_incomplete_frame_copy_before_startup(tmp_path):
    import pytest
    mission = Mission(tmp_path, record=True)
    mission.record('frame', CameraFrame(b'jpeg', 0, 1, 'USB CAMERA'), elapsed=0)
    (mission.directory / 'frames/00000000.jpg').unlink()
    with pytest.raises(ValueError, match='Missing replay frame'):
        ReplayRun(mission.directory)
