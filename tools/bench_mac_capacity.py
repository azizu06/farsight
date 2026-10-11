"""SYNTHETIC Metal + production VideoToolbox SBS capacity measurement.

python -m tools.bench_mac_capacity --output .cache/mac-capacity.json
No rover/phone/control connection. A loopback fixture uses production dense
state/GPU helpers and the actual RenderEncoder. Only the fixture's module
capacity validation is raised to permit the requested ten-million-point probe.
"""

import argparse
import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace

import aiohttp
from aiohttp import web
from playwright.async_api import async_playwright

from tools.mac_render.server import App
from tools.mac_render.protocol import percentile

ROOT = Path(__file__).resolve().parents[1]


async def run(options):
    results = []
    for count in options.points:
        args = SimpleNamespace(width=1920, height=1080, fps=30, bitrate=4_000_000,
                               hardware='videotoolbox', stereo='sbs-left-right')
        mac = App(args)
        async def asset(request):
            path = request.path
            if path == '/map/status':
                return web.json_response(dict(source='SYNTHETIC CAPACITY BENCH', session_id='123e4567-e89b-12d3-a456-426614174000',
                    map_epoch=1, generation=1, revision=0, mesh_revision=0, points=0, triangles=0, fresh=True, age_ms=0, connected=True))
            if path.startswith(('/dense/', '/phone/', '/dense-layer/')):
                return web.Response(status=503)
            file = ROOT / ('tools/mac_render/' + path[8:] if path.startswith('/render/') else path.lstrip('/'))
            if not file.resolve().is_relative_to(ROOT) or not file.is_file():
                return web.Response(status=404)
            if path == '/render/dense-cloud.mjs':
                body = file.read_text().replace('capacity > 5_000_000', 'capacity > 10_000_000')
                return web.Response(text=body, content_type='text/javascript')
            return web.FileResponse(file)
        app = web.Application()
        app.router.add_get('/health', mac.health)
        app.router.add_get('/ws/macview', mac.macview)
        app.router.add_get('/ws/macview-pose', mac.pose)
        app.router.add_get('/{tail:.*}', asset)
        runner = web.AppRunner(app); await runner.setup()
        site = web.TCPSite(runner, '127.0.0.1', 0); await site.start()
        origin = f'http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}'
        try:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(executable_path=options.browser, headless=True,
                    args=['--use-angle=metal', '--enable-gpu', '--disable-background-timer-throttling', '--disable-renderer-backgrounding'])
                try:
                    page = await browser.new_page(viewport={'width':1920,'height':1080})
                    errors = []; page.on('pageerror', lambda error: errors.append(str(error)))
                    await page.goto(origin + '/render/render.html?upstream=' + origin + '&width=1920&height=1080&layout=sbs-left-right&lod=1')
                    await page.wait_for_function('window.__mac?.ready', timeout=20000)
                    await page.evaluate('window.__macCaptureDriven=true')
                    await page.evaluate('(n) => import("/tools/mac_capacity_layer.mjs").then(m=>m.install(n))', count)
                    mac.page = page
                    await mac.start_encoder()
                    await asyncio.sleep(options.warmup)
                    encoder = mac.encoder
                    start_frames = encoder.frame_id
                    start_time = time.monotonic()
                    samples = []
                    async with aiohttp.ClientSession() as client:
                        async with client.ws_connect(origin + '/ws/macview?layout=sbs-left-right') as socket:
                            await socket.receive_json(timeout=15); await socket.receive(timeout=15)
                            while time.monotonic() - start_time < options.seconds:
                                await socket.send_json({'type':'next','request_id':len(samples)+1})
                                sidecar = await socket.receive_json(timeout=15)
                                payload = await socket.receive(timeout=15)
                                samples.append({'age_ms':time.time()*1000-sidecar['capture_wall_ms'],
                                    'frame_id':sidecar['frame_id'], 'bytes':len(payload.data),
                                    'render_ms':sidecar.get('render_ms'), 'capture_ms':sidecar.get('capture_ms')})
                    elapsed = time.monotonic() - start_time
                    fragment_times = [x for x in encoder.fragment_times if x[0] >= start_time]
                    report = dict(requested=count, synthetic=True, geometry='unique centimetre cells plus 97410 textured triangles',
                        hardware='ANGLE Metal + h264_videotoolbox', resolution='1920x1080 SBS', lod=1,
                        background='live :8091 renderer remains running; no rover commands or cellular fixture traffic',
                        seconds=elapsed, capture_fps=(encoder.frame_id-start_frames)/elapsed,
                        encoded_fps=sum(x[1] for x in fragment_times[1:])/max(.001,fragment_times[-1][0]-fragment_times[0][0]) if len(fragment_times)>1 else None,
                        picture_age_ms_p50=percentile([s['age_ms'] for s in samples], .5),
                        picture_age_ms_p95=percentile([s['age_ms'] for s in samples], .95),
                        eval_ms_p50=percentile([s['eval_ms'] for s in encoder.samples], .5),
                        snapshot=encoder.last_snapshot, errors=errors, fragments=len(samples))
                    await encoder.close()
                    raster = await page.evaluate('window.__macBenchRaster?.(60)')
                    if raster:
                        report.update(raster_ms_p50=percentile(raster, .5), raster_ms_p95=percentile(raster, .95))
                    results.append(report)
                    Path(options.output).write_text(json.dumps(results, indent=2))
                    print(json.dumps({k:v for k,v in report.items() if k != 'snapshot'}), flush=True)
                    print(json.dumps({'benchmark':report['snapshot'].get('benchmark'), 'drawn_points':report['snapshot'].get('drawnPoints')}), flush=True)
                    await page.screenshot(path=str(Path(options.output).with_name(f'mac-capacity-{count}.png')))
                finally:
                    await browser.close()
        finally:
            if mac.encoder and mac.encoder.process and mac.encoder.process.returncode is None:
                await mac.encoder.close()
            await runner.cleanup()
    return results


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--points', nargs='+', type=int, default=[1_000_000,2_000_000,5_000_000,10_000_000])
    parser.add_argument('--seconds', type=float, default=12)
    parser.add_argument('--warmup', type=float, default=4)
    parser.add_argument('--browser', default='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
    parser.add_argument('--output', required=True)
    asyncio.run(run(parser.parse_args()))
