"""Bounded Mac-render stream check; never opens a rover control connection.

Default is receive-only. --probe-pose changes the render camera and belongs on
an isolated desk run, not while someone is using the headset.
"""
import argparse
import asyncio
from contextlib import suppress
import json
import math
from pathlib import Path
import struct
import time

import aiohttp

from tools.geometry_relay import geometry_origin

MAX_MESSAGE = 4_000_000


def number(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f'{name} must be finite')
    return value


def integer(value, name):
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < 2**53:
        raise ValueError(f'{name} must be an unsigned safe integer')
    return value


def boxes(body):
    """Validate complete top-level ISO BMFF box boundaries, not codec decoding."""
    result, offset = [], 0
    while offset < len(body):
        if len(body) - offset < 8:
            raise ValueError('Truncated MP4 box header')
        size, kind = struct.unpack_from('>I4s', body, offset)
        header = 8
        if size == 1:
            if len(body) - offset < 16:
                raise ValueError('Truncated extended MP4 box header')
            size = struct.unpack_from('>Q', body, offset + 8)[0]
            header = 16
        elif size == 0:
            size = len(body) - offset
        if size < header or size > len(body) - offset:
            raise ValueError('Invalid MP4 box size')
        result.append(kind)
        offset += size
    return result


def summary(values):
    if not values:
        return None
    ordered = sorted(values)
    def percentile(p):
        index = (len(ordered) - 1) * p
        lo, hi = math.floor(index), math.ceil(index)
        return round(ordered[lo] + (ordered[hi] - ordered[lo]) * (index - lo), 3)
    return dict(p50=percentile(.5), p95=percentile(.95), maximum=round(ordered[-1], 3))


async def no_redirect(_session, _context, _params):
    # aiohttp's WS handshake follows HTTP redirects unless this trace rejects
    # them. A checker must not be redirected into /ws/control or another host.
    raise ValueError('WebSocket redirects are not allowed')


async def probe(client, origin, count, send_pose, report):
    stream = pose_socket = None
    evidence = report['evidence']
    try:
        if send_pose:
            pose_socket = await client.ws_connect(origin.with_path('/ws/macview-pose'),
                                                  max_msg_size=65536)
            pose = dict(type='pose', version=1, sent_mono_ms=time.monotonic() * 1000,
                        sent_wall_ms=time.time() * 1000,
                        head=dict(position=[0, 1.6, 0], quaternion=[0, 0, 0, 1]), ray=None)
            evidence['probe_pose_sent_mono_ms'] = pose['sent_mono_ms']
            await pose_socket.send_json(pose)
        stream = await client.ws_connect(origin.with_path('/ws/macview'), max_msg_size=MAX_MESSAGE)
        init = pending = None
        initialized = False
        outstanding, samples = {}, []
        next_id = 0
        first_received = None

        async def replenish():
            nonlocal next_id
            while len(outstanding) < 2 and len(samples) + len(outstanding) < count:
                next_id += 1
                outstanding[next_id] = time.monotonic()
                await stream.send_json(dict(type='next', request_id=next_id))

        async for message in stream:
            if message.type == aiohttp.WSMsgType.TEXT:
                if len(message.data.encode()) > 65536:
                    raise ValueError('Metadata exceeds byte limit')
                packet = json.loads(message.data)
                if not isinstance(packet, dict):
                    raise ValueError('Metadata must be an object')
                kind = packet.get('type')
                if kind in ('video_error', 'macview_error'):
                    raise ValueError(f'Renderer reported {kind}: {str(packet.get("reason", "unavailable"))[:200]}')
                if kind == 'video_init':
                    if init is not None or type(packet.get('version')) is not int or packet['version'] != 1:
                        raise ValueError('Duplicate or unsupported video initialization')
                    mime = packet.get('mime')
                    if not isinstance(mime, str) or not mime.startswith('video/mp4') or 'avc' not in mime:
                        raise ValueError('Initialization does not announce H.264 MP4')
                    if not isinstance(packet.get('stereo'), bool):
                        raise ValueError('Initialization must declare stereo explicitly')
                    for key in ('width', 'height', 'fps'):
                        if number(packet.get(key), key) <= 0:
                            raise ValueError(f'{key} must be positive')
                    init = packet
                    evidence['init'] = {key: packet.get(key) for key in
                                        ('mime', 'width', 'height', 'fps', 'stereo', 'source')}
                elif kind == 'video_segment':
                    if not initialized or pending is not None:
                        raise ValueError('Segment metadata outside the init/binary sequence')
                    if type(packet.get('version')) is not int or packet['version'] != 1:
                        raise ValueError('Unsupported fragment metadata version')
                    request_id = integer(packet.get('request_id'), 'request_id')
                    if request_id not in outstanding:
                        raise ValueError('Unknown or reused fragment request ID')
                    start = number(packet.get('media_start_s'), 'media_start_s')
                    end = number(packet.get('media_end_s'), 'media_end_s')
                    if start < 0 or end <= start or (samples and start < samples[-1]['media_end_s'] - .0001):
                        raise ValueError('Fragment media intervals overlap or go backwards')
                    if number(packet.get('age_ms'), 'age_ms') < 0:
                        raise ValueError('Negative render age')
                    if packet.get('stereo') is not init['stereo']:
                        raise ValueError('Stereo layout changed without initialization')
                    frame_id = integer(packet.get('frame_id'), 'frame_id')
                    if samples and frame_id <= samples[-1]['frame_id']:
                        raise ValueError('Render frame IDs are not advancing')
                    if not isinstance(packet.get('source'), str) or not packet['source']:
                        raise ValueError('Fragment source label missing')
                    pending = packet
                else:
                    raise ValueError('Unexpected stream metadata type')
            elif message.type == aiohttp.WSMsgType.BINARY:
                kinds = boxes(message.data)
                if not initialized:
                    if init is None or b'ftyp' not in kinds or b'moov' not in kinds:
                        raise ValueError('Missing MP4 initialization boxes')
                    initialized = True
                    evidence['init_bytes'] = len(message.data)
                    await replenish()
                    continue
                if pending is None or b'moof' not in kinds or b'mdat' not in kinds:
                    raise ValueError('Fragment missing metadata or moof/mdat boxes')
                received = time.monotonic()
                request_ms = (received - outstanding.pop(pending['request_id'])) * 1000
                first_received = first_received or received
                item = dict(request_id=pending['request_id'], frame_id=pending['frame_id'],
                            media_start_s=pending['media_start_s'], media_end_s=pending['media_end_s'],
                            bytes=len(message.data), source_claim=pending['source'],
                            request_ms=request_ms, reported_render_age_ms=pending['age_ms'],
                            receipt_age_bound_ms=pending['age_ms'] + request_ms)
                if isinstance(pending.get('pose'), dict):
                    item['echoed_pose_sent_mono_ms'] = pending['pose'].get('sent_mono_ms')
                samples.append(item)
                evidence['samples'] = samples
                pending = None
                if len(samples) == count:
                    elapsed = received - first_received
                    evidence['request_ms'] = summary([x['request_ms'] for x in samples])
                    evidence['receipt_age_bound_ms'] = summary([x['receipt_age_bound_ms'] for x in samples])
                    # Exclude the first fragment: its interval began before this
                    # measurement. Include full startup/request cost above.
                    evidence['bytes_per_second_after_first_fragment'] = (
                        round(sum(x['bytes'] for x in samples[1:]) / elapsed, 1) if elapsed > 0 else None)
                    evidence['sources_claimed'] = sorted({x['source_claim'] for x in samples})
                    if send_pose and not any(x.get('echoed_pose_sent_mono_ms') == pose['sent_mono_ms'] for x in samples):
                        raise ValueError('Stationary test pose was not echoed in a rendered fragment')
                    report['qualified'] = True
                    return
                await replenish()
            elif message.type == aiohttp.WSMsgType.ERROR:
                raise ValueError('Stream transport error')
        raise ValueError('Stream closed before requested fragments arrived')
    finally:
        for socket in (stream, pose_socket):
            if socket is not None:
                with suppress(Exception):
                    await asyncio.wait_for(socket.close(), .3)


async def qualify(origin='http://127.0.0.1:8090', segments=12, timeout=10, probe_pose=False):
    report = dict(qualified=False, evidence={}, errors=[], limitations=[
        'Transport/MP4-envelope evidence only: no codec decode, headset FPS, stereo quality or pose-to-photon proof.',
        'Receipt age uses server-reported render age plus local request time; it is not physical sensor/camera age.',
        'Source labels and encoder performance are not independently verified by this checker.',
        'Pose probe is opt-in and changes the render camera; use it only on an isolated desk test.',
    ])
    started = time.monotonic()
    try:
        origin = geometry_origin(origin)
        if not isinstance(segments, int) or isinstance(segments, bool) or not 2 <= segments <= 200:
            raise ValueError('segments must be between 2 and 200')
        if number(timeout, 'timeout') <= 0 or timeout > 120:
            raise ValueError('timeout must be between 0 and 120 seconds')
        trace = aiohttp.TraceConfig()
        trace.on_request_redirect.append(no_redirect)
        async with aiohttp.ClientSession(trace_configs=[trace], trust_env=False,
                                       timeout=aiohttp.ClientTimeout(total=None, sock_connect=min(timeout, 3))) as client:
            await asyncio.wait_for(probe(client, origin, segments, probe_pose, report), timeout)
    except asyncio.TimeoutError:
        report['errors'].append('Overall stream qualification deadline expired')
    except (ValueError, aiohttp.ClientError, OSError) as exc:
        report['errors'].append(str(exc))
    report['elapsed_seconds'] = round(time.monotonic() - started, 3)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--origin', default='http://127.0.0.1:8090')
    parser.add_argument('--segments', type=int, default=12)
    parser.add_argument('--timeout', type=float, default=10)
    parser.add_argument('--probe-pose', action='store_true', help='Stationary render-camera probe; isolated desk runs only')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args(argv)
    report = asyncio.run(qualify(args.origin, args.segments, args.timeout, args.probe_pose))
    text = json.dumps(report, indent=2, allow_nan=False) + '\n'
    if args.output:
        args.output.write_text(text)
    print(text, end='')
    return 0 if report['qualified'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
