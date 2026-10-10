"""Read-only HTTP check of the operator relay's phone-built mesh source path.

No control requests, device access, link benchmark, deployment or network changes.
"""
import argparse
import asyncio
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile

import aiohttp

from tools.geometry_relay import MAX_JSON, MAX_MESH, geometry_origin, identity, validate_mesh

SOURCE = 'IPHONE ARKIT MESH'
MAX_ATTEMPTS = 3


class SnapshotChanged(ValueError):
    pass


def counter(value, name):
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < 2**64:
        raise ValueError(f'{name} must be an unsigned integer')
    return value


def status_evidence(packet):
    if not isinstance(packet, dict) or packet.get('type') != 'status':
        raise ValueError('Map status is not a status object')
    geometry = packet.get('geometry')
    if not isinstance(geometry, dict):
        raise ValueError('Geometry source evidence missing')
    if packet.get('geometry_source') != SOURCE or geometry.get('source') != SOURCE:
        raise ValueError('Map source is not IPHONE ARKIT MESH')
    for key, expected in (('matched', True), ('retained', False), ('connected', True), ('snapshot_complete', True)):
        if geometry.get(key) is not expected:
            raise ValueError(f'geometry.{key} must be {str(expected).lower()} (unknown is not qualified)')
    if packet.get('fresh') is not True or packet.get('connected') is not True:
        raise ValueError('Jetson pose association must report fresh=true and connected=true')
    pair = identity(packet.get('session_id'), packet.get('map_epoch'))
    if pair is None:
        raise ValueError('Map session/epoch invalid')
    result = dict(session_id=pair[0], map_epoch=pair[1], geometry_source=SOURCE,
                  fresh=True, connected=True, matched=True, retained=False, snapshot_complete=True)
    for key in ('generation', 'revision', 'mesh_revision', 'points', 'triangles'):
        result[key] = counter(packet.get(key), key)
    result['published_revision'] = counter(geometry.get('revision'), 'geometry.revision')
    result['receiver_revision'] = counter(geometry.get('receiver_revision'), 'geometry.receiver_revision')
    if not 0 < result['points'] <= 600000 or not 0 < result['triangles'] <= 160000:
        raise ValueError('A positive bounded vertex/triangle mesh is required')
    return result


def stable_key(status):
    # The receiver may advance while its last complete mesh remains published.
    return tuple(status[x] for x in ('session_id', 'map_epoch', 'generation', 'revision',
                                    'mesh_revision', 'published_revision', 'points', 'triangles'))


def header_counter(headers, name):
    value = headers.get(name, '')
    if not re.fullmatch(r'0|[1-9][0-9]*', value, flags=re.ASCII):
        raise ValueError(f'{name} header missing or invalid')
    return counter(int(value), name)


def mesh_evidence(body, headers, status):
    vertices, triangles = validate_mesh(body)  # same validator as the production relay
    if vertices != status['points'] or triangles != status['triangles']:
        raise SnapshotChanged('Mesh counts changed during the status bracket')
    if headers.get('X-Geometry-Source') != SOURCE or headers.get('X-Geometry-Retained') != 'false':
        raise ValueError('Mesh source/retained headers do not qualify a live phone mesh')
    pair = identity(headers.get('X-Map-Session'), header_counter(headers, 'X-Map-Epoch'))
    generation = header_counter(headers, 'X-Map-Generation')
    revision = header_counter(headers, 'X-Map-Revision')
    if pair is None:
        raise ValueError('Mesh session header invalid')
    if pair != (status['session_id'], status['map_epoch']) or generation != status['generation'] or revision != status['revision']:
        raise SnapshotChanged('Mesh identity/revision differs from bracketed status')
    return dict(vertices=vertices, triangles=triangles, bytes=len(body), sha256=hashlib.sha256(body).hexdigest(),
                session_id=pair[0], map_epoch=pair[1], generation=generation, revision=revision,
                source=SOURCE, retained=False)


async def read(client, origin, path, maximum):
    async with client.get(origin.with_path(path), headers={'Accept-Encoding': 'identity'},
                          allow_redirects=False) as response:
        if response.status != 200:
            raise ValueError(f'{path} returned HTTP {response.status}')
        if response.headers.get('Content-Encoding', 'identity').lower() != 'identity':
            raise ValueError(f'{path} returned unexpected content encoding')
        if response.content_length is not None and response.content_length > maximum:
            raise ValueError(f'{path} exceeds byte limit')
        chunks, size = [], 0
        async for chunk in response.content.iter_chunked(65536):
            size += len(chunk)
            if size > maximum:
                raise ValueError(f'{path} exceeds byte limit')
            chunks.append(chunk)
        return b''.join(chunks), response.headers


def parse_json(body):
    def invalid(_):
        raise ValueError('Non-finite JSON value')
    return json.loads(body, parse_constant=invalid)


async def _request_snapshot(origin, timeout, report):
    async with aiohttp.ClientSession(auto_decompress=False, trust_env=False,
            timeout=aiohttp.ClientTimeout(total=timeout), connector=aiohttp.TCPConnector(limit=1)) as client:
        for attempt in range(1, MAX_ATTEMPTS+1):
            report['attempts'] = attempt
            before_body, _ = await read(client, origin, '/map/status', MAX_JSON)
            before = status_evidence(parse_json(before_body))
            report['evidence'] = dict(status_before=before)
            body, headers = await read(client, origin, '/map/mesh.bin', MAX_MESH)
            after_body, _ = await read(client, origin, '/map/status', MAX_JSON)
            after = status_evidence(parse_json(after_body))
            report['evidence']['status_after'] = after
            try:
                if stable_key(before) != stable_key(after):
                    raise SnapshotChanged('Published mesh identity/revision changed during download')
                report['evidence']['mesh'] = mesh_evidence(body, headers, before)
            except SnapshotChanged as exc:
                report['evidence']['last_race'] = str(exc)
                if attempt == MAX_ATTEMPTS:
                    raise ValueError(f'No stable mesh bracket after {MAX_ATTEMPTS} attempts: {exc}') from exc
                continue
            report['qualified'] = True
            break


async def qualify(relay='http://127.0.0.1:8080', timeout=5):
    report = dict(qualified=False, scope='Read-only HTTP phone geometry source-path check', attempts=0,
                  limitations=['Does not qualify Quest rendering, physical motion, camera freshness guards, or cellular routing/latency.',
                               'Map fresh=true is pose association evidence; it is not the 500 ms displayed-camera guard.'],
                  evidence={}, errors=[])
    try:
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= 60:
            raise ValueError('Timeout must be finite, positive and at most 60 seconds')
        origin = geometry_origin(relay)
        await asyncio.wait_for(_request_snapshot(origin, timeout, report), timeout=timeout)
    except (ValueError, TypeError, UnicodeError) as exc:
        report['errors'].append(str(exc))
    except asyncio.TimeoutError:
        report['errors'].append('Overall timeout before a stable mesh snapshot was verified')
    except aiohttp.ClientError as exc:
        report['errors'].append(f'HTTP read failed: {type(exc).__name__}')
    return report


def save_report(path, report):
    destination = Path(path)
    with tempfile.NamedTemporaryFile(mode='w', dir=destination.parent, prefix='.geometry-check-', delete=False) as output:
        temporary = Path(output.name)
        try:
            json.dump(report, output, indent=2, allow_nan=False); output.write('\n')
            output.flush(); os.fsync(output.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--relay', default='http://127.0.0.1:8080')
    parser.add_argument('--timeout', type=float, default=5, help='Overall deadline, 0 < seconds <= 60')
    parser.add_argument('--output', help='Optional JSON evidence file (failures are also saved)')
    args = parser.parse_args()
    report = asyncio.run(qualify(args.relay, args.timeout))
    if args.output:
        try:
            save_report(args.output, report)
        except OSError:
            report['qualified'] = False
            report['errors'].append('Could not save JSON evidence file')
    print(json.dumps(report, indent=2, allow_nan=False))
    return 0 if report['qualified'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
