"""Measure a simulated cellular budget through the production operator relay.

Real CPU pose and FFmpeg; no physical devices. TCP retry delay models loss
recovery without corrupting the reliable byte stream or changing OS networking.
"""

import argparse
import asyncio
from collections import defaultdict
import hashlib
import io
from importlib.metadata import version
import json
import math
from pathlib import Path
import platform
import time
import wave

import aiohttp

from tools.qualify_link import percentile
from tools.tests.test_demo_qualification import Controller, Rig, VideoClient, fixture_app, wait_until


class Counter:
    def __init__(self):
        self.values = defaultdict(lambda: dict(bytes=0, messages=0))

    def add(self, path, direction, data):
        row = self.values[path + ':' + direction]
        row['bytes'] += len(data.encode() if isinstance(data, str) else data)
        row['messages'] += 1

    def report(self, seconds):
        return {key: dict(row, kbps=round(row['bytes'] * 8 / seconds / 1000, 3),
                          messages_hz=round(row['messages'] / seconds, 3))
                for key, row in sorted(self.values.items())}


def budget_app(args):
    from fastapi import Request
    from fastapi.responses import Response
    app = fixture_app(args.serve_fixture, scene_path=args.scene,
                      server_options=dict(detector='pose', detector_model=args.model))
    meter = Counter()

    class Meter:
        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            path = scope.get('path', '')
            enabled = not path.startswith('/__') and path not in ('/control/status',)
            async def measured_receive():
                message = await receive()
                if enabled and message['type'] in ('websocket.receive', 'http.request'):
                    meter.add(path, 'up', message.get('text') or message.get('bytes') or message.get('body', b''))
                return message
            async def measured_send(message):
                await send(message)
                if enabled and message['type'] in ('websocket.send', 'http.response.body'):
                    meter.add(path, 'down', message.get('text') or message.get('bytes') or message.get('body', b''))
            await self.app(scope, measured_receive, measured_send)
    app.add_middleware(Meter)

    @app.api_route('/__budget', methods=['GET', 'DELETE'])
    async def budget(request: Request):
        if request.method == 'DELETE':
            meter.values.clear()
        return dict(streams=dict(meter.values), perception=app.state.perception.health(),
                    mapping_frames=app.state.mapping.frames)

    # Explicit synthetic sizing fixture: two seconds of silent 16 kHz PCM.
    audio = io.BytesIO()
    with wave.open(audio, 'wb') as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(bytes(64000))

    @app.get('/tts/reply-budget.wav')
    async def tts_size_fixture():
        return Response(audio.getvalue(), media_type='audio/wav')

    app.router.routes.sort(key=lambda route: 0 if getattr(route, 'path', '').startswith(
        ('/__', '/tts/reply-budget.wav')) else 1)
    return app


async def trial(args, mbps, repeat, scenario):
    extra = ['--model', str(args.model)]
    if args.scene:
        extra += ['--scene', str(args.scene)]
    async with Rig(args.source, bandwidth_mbps=mbps, fixture_module='tools.budget_link',
                   fixture_args=extra, proxy_options=dict(loss_rate=args.loss_rate,
                   retransmit_ms=args.retransmit_ms, seed=62 + repeat)) as rig:
        video = VideoClient(rig.url)
        controller = Controller(rig, video)
        delivered, errors, samples, box_counts = Counter(), [], [], []
        encodings = defaultdict(set)
        original_receive = aiohttp.ClientWebSocketResponse.receive
        original_send = aiohttp.ClientWebSocketResponse.send_str
        def client_socket(ws):
            return str(ws._response.url.origin()) == rig.url
        async def receive(ws, *pos, **kw):
            msg = await original_receive(ws, *pos, **kw)
            if client_socket(ws) and msg.type in (aiohttp.WSMsgType.TEXT, aiohttp.WSMsgType.BINARY):
                delivered.add(ws._response.url.path, 'down', msg.data)
            return msg
        async def send(ws, data, *pos, **kw):
            await original_send(ws, data, *pos, **kw)
            if client_socket(ws):
                delivered.add(ws._response.url.path, 'up', data)
        aiohttp.ClientWebSocketResponse.receive = receive
        aiohttp.ClientWebSocketResponse.send_str = send
        tasks = []
        started = None
        async def http(path, method='GET', **kw):
            async with rig.client.request(method, rig.url + path, auto_decompress=False, **kw) as response:
                data = await response.read()
                encodings[path].add(response.headers.get('Content-Encoding', 'identity'))
                delivered.add(path, 'down', data)
                response.raise_for_status()
                return data
        async def detections():
            async with rig.client.ws_connect(rig.url + '/ws/detections') as ws:
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        box_counts.append(len(msg.json()['boxes']))
                        del box_counts[:-1000]
        async def assistant():
            while True:
                await http('/ask', method='POST', json={'text': 'Summarize the scene'})
                await http('/tts/reply-budget.wav')
                await asyncio.sleep(10)
        async def preview():
            while True:
                if not args.optimized or not controller.held:
                    paths = ['/depth.jpg'] if args.optimized else ['/camera.jpg', '/depth.jpg']
                    await asyncio.gather(*(http(path) for path in paths))
                await asyncio.sleep(.25)
        async def maps():
            if args.optimized and controller.held:
                return
            path = '/ws/map' + ('?points=http' if args.optimized else '')
            pending, versions = {}, {}
            last_kind, last_start = 'mesh', -float('inf')
            async def pull(kind, revision):
                await http('/map/' + kind + '.bin', headers={'Accept-Encoding': 'br, gzip'})
                versions[kind] = revision
            try:
                async with rig.client.ws_connect(rig.url + path, max_msg_size=16 * 1024 * 1024) as ws:
                    async for msg in ws:
                        if msg.type != aiohttp.WSMsgType.TEXT:
                            continue
                        status = msg.json()
                        if args.optimized and controller.held:
                            await ws.close()
                            return
                        if args.optimized and status.get('points_transport') != 'http':
                            raise ValueError('Status-only map subscription was not acknowledged')
                        choices = [('mesh', status['mesh_revision'])]
                        if args.optimized:
                            if any(not task.done() for task in pending.values()) or time.monotonic() - last_start < 2:
                                continue
                            choices += [('points', status['revision'])]
                            if last_kind == 'mesh':
                                choices.reverse()
                        for kind, revision in choices:
                            if (kind in pending and not pending[kind].done()
                                    or versions.get(kind) == revision):
                                continue
                            last_start, last_kind = time.monotonic(), kind
                            pending[kind] = asyncio.create_task(pull(kind, revision))
                            pending[kind].add_done_callback(lambda task: errors.append(
                                type(task.exception()).__name__ + ': ' + str(task.exception()))
                                if not task.cancelled() and task.exception() else None)
                            if args.optimized:
                                break
            finally:
                for task in pending.values():
                    task.cancel()
                await asyncio.gather(*pending.values(), return_exceptions=True)
        async def guarded(fn):
            try:
                await fn()
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
                errors.append(type(exc).__name__ + ': ' + str(exc))
        try:
            await video.start()
            await controller.start()
            await wait_until(lambda: len(video.bounds) >= 10 and video.current_age() <= 400)
            await controller.release()
            await wait_until(lambda: not controller.status.get('requires_release') and video.current_age() <= 400)
            if scenario not in ('stopped-map', 'preview-only') and not controller.press():
                raise RuntimeError('Fresh view/release required before simulated motion')
            async with rig.client.delete(rig.direct + '/__budget') as response:
                response.raise_for_status()
            delivered.values.clear()
            video.bounds.clear()
            started = time.monotonic()
            proxy_start = rig.proxy.stats['bytes']
            tasks = [asyncio.create_task(guarded(detections)), asyncio.create_task(guarded(assistant))]
            if scenario in ('map', 'preview', 'stopped-map'):
                tasks.append(asyncio.create_task(guarded(maps)))
            if scenario in ('preview', 'preview-only'):
                tasks.append(asyncio.create_task(guarded(preview)))
            while time.monotonic() - started < args.duration:
                status = await rig.status()
                samples.append(dict(at=time.monotonic() - started,
                    moving=bool(status['left_pwm'] or status['right_pwm']), age_ms=video.current_age()))
                await asyncio.sleep(.04)
            elapsed = time.monotonic() - started
            async with rig.client.get(rig.direct + '/__budget') as response:
                server = await response.json()
            backend = await rig.fixture_status()
            failure = backend['first_failure']
            failure_at = failure['at'] if failure else -float('inf')
            failure_context = {key: [event for event in backend[key] if abs(event['at'] - failure_at) <= .25]
                for key in ('command_events', 'status_events', 'ws_send_events', 'loop_lags')}
            offered = Counter()
            offered.values.update(server.pop('streams'))
            stable = [sample for sample in samples if sample['at'] >= 1]
            return dict(mbps=mbps, repeat=repeat, scenario=scenario, seconds=elapsed,
                workload_valid=(not server['perception']['detector_error']
                    and server['perception']['detector_fps'] > 0
                    and (args.source != 'map5' or server['mapping_frames'] > 0)),
                settled_moving_fraction=sum(s['moving'] for s in stable) / max(1, len(stable)),
                capture_receipt_p95_ms=percentile(video.bounds, .95),
                held_view_peak_ms=max((s['age_ms'] for s in samples if math.isfinite(s['age_ms'])), default=None),
                disconnected_view_samples=sum(not math.isfinite(s['age_ms']) for s in samples),
                delivered_payload=delivered.report(elapsed), server_payload=offered.report(elapsed),
                content_encodings={path: sorted(values) for path, values in encodings.items()},
                proxy_kbps=(rig.proxy.stats['bytes'] - proxy_start) * 8 / elapsed / 1000,
                proxy_stats=rig.proxy.stats.copy(), detections_max_boxes=max(box_counts, default=0),
                workload=server, errors=errors + video.errors + controller.errors,
                control_ack_p95_ms=percentile(controller.acks, .95),
                command_gap_max_ms=max(controller.cadences, default=0),
                first_backend_failure=failure, failure_context=failure_context,
                server_loop_lag_max_ms=max((event['lag_ms'] for event in backend['loop_lags']), default=0),
                client_loop_lag_max_ms=max((event['lag_ms'] for event in controller.loop_lags), default=0),
                invalidations=controller.invalidations)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await controller.close()
            await video.close()
            aiohttp.ClientWebSocketResponse.receive = original_receive
            aiohttp.ClientWebSocketResponse.send_str = original_send


def finite_report(value):
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: finite_report(item) for key, item in value.items()}
    if isinstance(value, list):
        return [finite_report(item) for item in value]
    return value


async def run(args):
    report = dict(host=platform.platform(), python=platform.python_version(), simulated=True,
        opencv=version('opencv-python-headless'), onnxruntime=version('onnxruntime'),
        model_sha256=hashlib.sha256(args.model.read_bytes()).hexdigest(),
        scene_sha256=hashlib.sha256(args.scene.read_bytes()).hexdigest() if args.scene else None,
        settings={key: str(value) if isinstance(value, Path) else value for key, value in vars(args).items()},
        map_policy=('one map download at a time, at least 2 seconds between starts, paused while held'
                    if args.optimized else 'legacy raw WS points plus asynchronous mesh pulls'),
        limits=['Payload bytes exclude TCP/TLS/Tailscale framing; proxy bytes include HTTP/WS framing',
                'Proxy delivered bytes exclude modeled retransmissions, counted separately in proxy_stats',
                'Server payload is submitted to ASGI, delivered payload crossed the relay',
                'Loss is seeded 4 KiB TCP retry-delay emulation, not real packet loss or congestion control',
                'The seed does not pin a retry to the same route under different traffic/scheduling',
                'TTS is a synthetic two-second PCM sizing fixture; assistant uses local summary only',
                'No physical camera/Quest/phone/Jetson/motors or decoded display qualification'], trials=[])
    for mbps in args.mbps:
        for repeat in range(1, args.repeats + 1):
            for scenario in args.scenarios:
                result = await trial(args, mbps, repeat, scenario)
                report['trials'].append(result)
                args.report.write_text(json.dumps(finite_report(report), indent=2, allow_nan=False) + '\n')
                if not result['workload_valid']:
                    raise RuntimeError('Pose/fusion workload invalid; report retained')
                print(f"{mbps} Mbps {scenario} repeat {repeat}: motion={result['settled_moving_fraction']:.1%} "
                      f"view p95={result['capture_receipt_p95_ms']:.1f} ms proxy={result['proxy_kbps']:.1f} kbps", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--serve-fixture', choices=('rgb15', 'map5'))
    parser.add_argument('--port', type=int)
    parser.add_argument('--source', choices=('rgb15', 'map5'), default='map5')
    parser.add_argument('--model', type=Path, required=True)
    parser.add_argument('--scene', type=Path)
    parser.add_argument('--duration', type=float, default=20)
    parser.add_argument('--repeats', type=int, default=2)
    parser.add_argument('--mbps', type=float, nargs='+', default=[2, 5])
    parser.add_argument('--scenarios', nargs='+', choices=('camera', 'map', 'preview', 'stopped-map', 'preview-only'), default=['camera', 'map'])
    parser.add_argument('--loss-rate', type=float, default=.01)
    parser.add_argument('--retransmit-ms', type=float, default=80)
    parser.add_argument('--optimized', action='store_true')
    parser.add_argument('--report', type=Path, default=Path('cellular-budget.json'))
    args = parser.parse_args()
    if not 2 <= args.duration <= 300 or not 1 <= args.repeats <= 10:
        parser.error('Use duration 2..300 and repeats 1..10')
    if args.serve_fixture:
        import uvicorn
        uvicorn.run(budget_app(args), host='127.0.0.1', port=args.port,
                    log_level='warning', access_log=False, timeout_graceful_shutdown=2)
    else:
        asyncio.run(run(args))


if __name__ == '__main__':
    main()
