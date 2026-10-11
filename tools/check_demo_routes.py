"""GET-only checks of the selected operator routes, not physical qualification.

python -m tools.check_demo_routes --operator http://127.0.0.1:8080 --require-dense --require-render
Use --camera-only when no map has been captured; a connected rover and a fresh,
drive-ready camera are still required. No control/pose sockets, POSTs,
redirects, route changes, or whole-map downloads are made.
"""
import argparse
import asyncio
import json
import math
from pathlib import Path
import struct
import time
import uuid

import aiohttp

from tools.geometry_relay import geometry_origin
from tools.operator_relay import upstream_origin


def count(value):
    return type(value) is int and value >= 0


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def json_float(value):
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError('nonfinite JSON number')
    return parsed


async def read(client, url, max_bytes, *, mesh=None):
    """Bound JSON bodies, or read only the map header and first vertex."""
    result = dict(url=url, http_status=None, error=None, body=None)
    try:
        async with client.get(url, allow_redirects=False,
                headers={'Accept-Encoding': 'identity'}) as response:
            result['http_status'] = response.status
            if response.status != 200:
                raise ValueError(f'HTTP {response.status}; no fallback or redirect was attempted')
            if response.headers.get('Content-Encoding', 'identity').lower() != 'identity':
                raise ValueError('encoded response rejected; identity was requested')
            if mesh is not None:
                header = await response.content.readexactly(8 if mesh else 4)
                vertices = struct.unpack_from('<I', header)[0]
                indices = struct.unpack_from('<I', header, 4)[0] if mesh else None
                if vertices > 600000 or (mesh and (indices > 480000 or indices % 3)):
                    raise ValueError('map header exceeds the viewer limits')
                expected_bytes = len(header) + vertices * 16 + (indices * 4 if mesh else 0)
                if response.content_length is not None and response.content_length != expected_bytes:
                    raise ValueError('map Content-Length disagrees with the declared vertex/index counts')
                if vertices:
                    vertex = await response.content.readexactly(16)
                    if not all(math.isfinite(v) for v in struct.unpack_from('<fff', vertex)):
                        raise ValueError('first map vertex has nonfinite coordinates')
                result['body'] = dict(vertices=vertices, indices=indices,
                                      prefix_bytes=len(header) + (16 if vertices else 0))
                return result
            if response.content_length is not None and response.content_length > max_bytes:
                raise ValueError(f'response exceeds {max_bytes} byte limit')
            body = bytearray()
            async for chunk in response.content.iter_chunked(min(max_bytes + 1, 16384)):
                body.extend(chunk)
                if len(body) > max_bytes:
                    raise ValueError(f'response exceeds {max_bytes} byte limit')
            packet = json.loads(body, parse_float=json_float,
                parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite JSON number')))
            if not isinstance(packet, dict):
                raise ValueError('status must be a JSON object')
            result['body'] = packet
    except (aiohttp.ClientError, asyncio.TimeoutError, asyncio.IncompleteReadError,
            UnicodeError, ValueError, RecursionError) as exc:
        result['error'] = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
    return result


async def qualify(operator='http://127.0.0.1:8080', *, camera_only=False,
                  require_dense=False, require_render=False, renderer=None,
                  expected_upstream=None, expected_geometry=None, timeout=5, max_bytes=65536):
    operator = str(upstream_origin(operator))
    renderer = str(geometry_origin(renderer)) if renderer is not None else None
    expected_upstream = str(upstream_origin(expected_upstream)) if expected_upstream is not None else None
    expected_geometry = str(geometry_origin(expected_geometry)) if expected_geometry is not None else None
    if not finite(timeout) or not 0 < timeout <= 30 or type(max_bytes) is not int or not 1 <= max_bytes <= 1048576:
        raise ValueError('timeout must be 0..30 seconds; max-bytes must be 1..1048576')
    if camera_only and (require_dense or require_render):
        raise ValueError('--camera-only cannot require a dense map or renderer')
    started = time.monotonic()
    report = dict(qualified=False, routes=dict(backend=None, geometry=None, render=None), checks=[],
                  scope='Selected route readiness from bounded GET evidence; not hardware or visual qualification',
                  limitations=['Map payload check reads only its header and first vertex, not the complete snapshot.',
                               'HTTP snapshots are independent; this does not establish headset display or stopping behavior.'])
    def record(name, response, passed, state, detail, action, facts=None):
        report['checks'].append(dict(name=name, url=response['url'], http_status=response['http_status'],
            passed=bool(passed and not response['error']), state='request_failed' if response['error'] else state,
            detail=response['error'] or detail, action=action, facts=facts or {}))
    async def fetch(client, path, **kwargs):
        remaining = max(.001, timeout - (time.monotonic() - started))
        try:
            return await asyncio.wait_for(read(client, operator + path, max_bytes, **kwargs), remaining)
        except asyncio.TimeoutError:
            return dict(url=operator + path, http_status=None, error='Overall deadline expired', body=None)

    async with aiohttp.ClientSession(auto_decompress=False, trust_env=False,
            timeout=aiohttp.ClientTimeout(total=timeout), connector=aiohttp.TCPConnector(limit=4)) as client:
        paths = ['/relay/health', '/health'] + ([] if camera_only else ['/map/status'])
        if require_dense:
            paths.append('/dense/status')
        replies = dict(zip(paths, await asyncio.gather(*(fetch(client, path) for path in paths))))
        relay_response = replies['/relay/health']; relay = relay_response['body'] or {}
        selected = relay.get('upstream')
        try:
            report['routes']['backend'] = str(upstream_origin(selected))
        except (ValueError, TypeError):
            pass
        geometry = relay.get('geometry') if isinstance(relay.get('geometry'), dict) else None
        render = relay.get('render') if isinstance(relay.get('render'), dict) else {}
        for name, packet in (('geometry', geometry or {}), ('render', render)):
            try:
                report['routes'][name] = str(geometry_origin(packet.get('upstream')))
            except (ValueError, TypeError):
                pass
        selections_ok = relay.get('relay') is True and report['routes']['backend'] is not None
        selections_ok &= expected_upstream is None or report['routes']['backend'] == expected_upstream
        selections_ok &= expected_geometry is None or report['routes']['geometry'] == expected_geometry
        record('relay', relay_response, selections_ok, 'selected_routes' if selections_ok else 'route_mismatch',
            'Selected origins reported; null means unavailable selection evidence',
            'Check the running relay --upstream/--geometry-upstream flags against the intended route.',
            dict(quest_built=relay.get('quest_built') is True, geometry_configured=geometry is not None,
                 render_configured=render.get('configured') is True))
        response = replies['/health']; health = response['body'] or {}
        camera_age = health.get('camera_age_ms')
        backend_ok = health.get('type') == 'status' and health.get('rover') is True and health.get('camera') is True
        backend_ok &= health.get('camera_drive_ready') is True and finite(camera_age) and 0 <= camera_age < 500
        backend_ok &= isinstance(health.get('camera_source'), str) and health['camera_source'] not in ('', 'WAITING')
        record('backend', response, backend_ok, 'backend_ready' if backend_ok else 'backend_not_ready',
            'Selected backend must report a connected rover and fresh drive-ready camera.',
            f"Check the selected Jetson route {report['routes']['backend'] or 'unknown'} and its camera/rover status; local relay health does not verify it.",
            {key: health.get(key) for key in ('type', 'rover', 'camera', 'camera_drive_ready', 'camera_age_ms', 'camera_source')})
        if not camera_only:
            response = replies['/map/status']; mapping = response['body'] or {}
            geo = mapping.get('geometry') if isinstance(mapping.get('geometry'), dict) else {}
            configured_geometry = geometry is not None or bool(mapping.get('geometry_source'))
            identity_ok = False
            try:
                uuid.UUID(mapping.get('session_id'))
                identity_ok = count(mapping.get('map_epoch'))
            except (ValueError, TypeError, AttributeError):
                pass
            points, triangles = mapping.get('points'), mapping.get('triangles')
            nonempty = count(points) and points > 0
            geometry_ok = not configured_geometry or all(geo.get(key) is True for key in ('matched', 'connected', 'snapshot_complete'))
            map_ok = mapping.get('type') == 'status' and mapping.get('fresh') is True and mapping.get('connected') is True
            map_ok &= identity_ok and nonempty and count(triangles) and geometry_ok
            state = 'map_ready' if map_ok else 'geometry_unavailable' if configured_geometry and not geometry_ok else 'awaiting_scan' if points == 0 else 'map_not_ready'
            record('map', response, map_ok, state, 'Map needs fresh capture identity and usable geometry.',
                'Check --geometry-upstream and the matching complete phone reconstruction; the healthy camera is a separate lane.' if configured_geometry else
                'Capture a fresh scan on the selected source; use --camera-only when intentionally checking only live camera/control.',
                dict(source=mapping.get('source'), geometry_source=mapping.get('geometry_source'), points=points,
                     triangles=triangles, fresh=mapping.get('fresh'), connected=mapping.get('connected'),
                     matched=geo.get('matched'), snapshot_complete=geo.get('snapshot_complete')))
            if map_ok:
                mesh = triangles > 0
                response = await fetch(client, '/map/mesh.bin' if mesh else '/map/points.bin', mesh=mesh)
                payload = response['body'] or {}
                usable = count(payload.get('vertices')) and payload['vertices'] > 0
                usable &= not mesh or count(payload.get('indices')) and payload['indices'] > 0
                record('map_payload', response, usable, 'map_payload_ready' if usable else 'empty_map_payload',
                    'Map header and first vertex are nonempty.',
                    'Check the selected map route; a positive status count must have a usable points/mesh response.', payload)
        if require_dense:
            response = replies['/dense/status']; dense = response['body'] or {}
            age = dense.get('age_s')
            ready = count(dense.get('mac_points')) and dense['mac_points'] > 0 and dense.get('error') is None
            ready &= finite(age) and 0 <= age < 30
            record('dense', response, ready, 'dense_ready' if ready else 'dense_not_ready',
                'Local dense endpoint must contain retained RGB-D samples with a recent successful pull.',
                'Start the owner-selected relay with tools.demo --dense-map or tools.dense_map; verify /map/dense.bin on its selected Jetson.',
                {key: dense.get(key) for key in ('mac_points', 'jetson_points', 'age_s', 'error', 'geometry_mac_points')})
        if require_render:
            selected_renderer = report['routes']['render']
            route_ok = render.get('configured') is True and selected_renderer is not None
            route_ok &= renderer is None or renderer == selected_renderer
            response = dict(url=operator + '/relay/health', http_status=relay_response['http_status'], error=None, body=None)
            record('render_route', response, route_ok, 'render_selected' if route_ok else 'render_route_unknown_or_mismatch',
                'Render socket destination must be configured and explicitly reported by this relay.',
                'Check --render-upstream and deploy relay origin diagnostics; --renderer must match that exact selected origin.')
            if route_ok:
                remaining = max(.001, timeout - (time.monotonic() - started))
                try:
                    response = await asyncio.wait_for(read(client, selected_renderer + '/health', max_bytes), remaining)
                except asyncio.TimeoutError:
                    response = dict(url=selected_renderer + '/health', http_status=None, error='Overall deadline expired', body=None)
                body = response['body'] or {}
                points, triangles = body.get('drawn_points'), body.get('drawn_triangles')
                dense = body.get('dense') if isinstance(body.get('dense'), dict) else {}
                dense_drawn, dense_uploaded = dense.get('gpu_drawn_cells'), dense.get('gpu_uploaded_cells')
                dense_drawing = count(dense_drawn) and dense_drawn > 0
                dense_ready = dense_drawing and count(dense_uploaded) and dense_uploaded > 0 and dense.get('error') is None
                drawing = (count(points) and points > 0) or (count(triangles) and triangles > 0) or dense_drawing
                ready = body.get('service') == 'mac-render' and body.get('ready') is True and drawing
                ready &= not require_dense or dense_ready
                state = 'renderer_ready' if ready else 'renderer_dense_not_ready' if require_dense and not dense_ready else 'renderer_empty' if not drawing else 'renderer_not_ready'
                record('renderer', response, ready, state,
                    'Encoder readiness must accompany positive drawn geometry.',
                    'Check the renderer upstream map/dense source and its running revision; encoding an empty scene is not map readiness.',
                    dict(service=body.get('service'), ready=body.get('ready'), drawn_points=points, drawn_triangles=triangles,
                         dense_drawn_cells=dense_drawn, dense_uploaded_cells=dense_uploaded, dense_error=dense.get('error')))
    report['qualified'] = all(check['passed'] for check in report['checks'])
    report['elapsed_ms'] = round((time.monotonic() - started) * 1000, 1)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--operator', default='http://127.0.0.1:8080')
    parser.add_argument('--camera-only', action='store_true', help='Skip map checks; rover and drive-ready live camera are still required')
    parser.add_argument('--require-dense', action='store_true')
    parser.add_argument('--require-render', action='store_true')
    parser.add_argument('--renderer', help='Optional expected loopback renderer origin; must match relay selection')
    parser.add_argument('--expected-upstream', help='Expected selected Jetson origin')
    parser.add_argument('--expected-geometry', help='Expected selected loopback geometry assembler origin')
    parser.add_argument('--timeout', type=float, default=5)
    parser.add_argument('--max-bytes', type=int, default=65536)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args(argv)
    try:
        report = asyncio.run(qualify(args.operator, camera_only=args.camera_only, require_dense=args.require_dense,
            require_render=args.require_render, renderer=args.renderer, expected_upstream=args.expected_upstream,
            expected_geometry=args.expected_geometry, timeout=args.timeout, max_bytes=args.max_bytes))
    except (ValueError, TypeError) as exc:
        parser.error(str(exc))
    encoded = json.dumps(report, indent=2, allow_nan=False) + '\n'
    if args.output:
        args.output.write_text(encoded)
    print(encoded, end='')
    return 0 if report['qualified'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
