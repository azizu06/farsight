"""Read-only live tailnet load window using cached measured dense pages.

No control writes or raw camera pulls. Repeated pages are traffic only and are
never accumulated as new geometry. This measures the selected network path;
AUTO stability is only observed if the human actually runs AUTO in the window.
"""

import argparse
import asyncio
import json
import gzip
import struct
import time
from pathlib import Path

import aiohttp

from jetson.mapping.compress import brotli
from tools.mac_render.protocol import percentile


async def measure(options):
    reports = []
    async with aiohttp.ClientSession(auto_decompress=False, timeout=aiohttp.ClientTimeout(total=5)) as client:
        async def page(params):
            started = time.monotonic()
            async with client.get(options.origin.rstrip('/') + options.path, params=params,
                                  headers={'Accept-Encoding':'br' if brotli else 'gzip'}) as response:
                response.raise_for_status(); body = await response.read()
                coding = response.headers.get('Content-Encoding')
                decoded = brotli.decompress(body) if coding == 'br' else gzip.decompress(body) if coding == 'gzip' else body
                if options.path == '/map/dense.bin':
                    header_size = struct.unpack_from('<I', decoded)[0]
                    meta = json.loads(decoded[4:4+header_size])
                else:
                    meta = {'reset':False}
                return meta, len(body), (time.monotonic()-started)*1000, coding
        for rate in options.mbps:
            if options.path == '/map/dense.bin':
                metadata, _, _, _ = await page({'after':0, 'limit':8192})
                params = {'epoch':metadata['epoch'], 'after':0, 'snapshot':'true',
                          'anchor':metadata['anchor'], 'limit':8192}
            else:
                params = {}
            health, pulls, errors = [], [], []
            started = time.monotonic(); deadline = started + options.seconds
            async def watch():
                while time.monotonic() < deadline:
                    try:
                        async with client.get(options.health.rstrip('/') + '/health') as response:
                            status = await response.json()
                            health.append({'at':time.monotonic(), 'camera_ms':status.get('camera_age_ms'),
                                'video_frames':status.get('video',{}).get('encoded_frames'),
                                'camera_ready':status.get('camera_drive_ready'),
                                'control':{k:status.get('control',{}).get(k) for k in
                                           ('mode','autonomy_state','reason','left_pwm','right_pwm')}})
                    except (aiohttp.ClientError, asyncio.TimeoutError) as error:
                        errors.append(str(error))
                    await asyncio.sleep(.25)
            watcher = asyncio.create_task(watch())
            transferred = 0
            while time.monotonic() < deadline:
                if rate == 0:
                    await asyncio.sleep(.1); continue
                try:
                    meta, size, latency, coding = await page(params)
                    transferred += size
                    pulls.append({'bytes':size,'latency_ms':latency,'coding':coding,'reset':meta['reset']})
                    if meta['reset']:
                        params.update(epoch=meta['epoch'],anchor=meta['anchor'])
                    await asyncio.sleep(max(0, started + transferred*8/(rate*1_000_000) - time.monotonic()))
                except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as error:
                    errors.append(str(error)); break
            await watcher
            elapsed = time.monotonic() - started
            first, last = (health[0], health[-1]) if health else ({},{})
            video_start, video_end = first.get('video_frames'),last.get('video_frames')
            result = dict(target_mbps=rate, seconds=elapsed, path=options.path, extra_payload_mbps=transferred*8/elapsed/1e6,
                requests=len(pulls), encodings=sorted({x['coding'] or 'identity' for x in pulls}),
                request_ms_p95=percentile([x['latency_ms'] for x in pulls],.95),
                camera_ms_p95=percentile([x['camera_ms'] for x in health if x['camera_ms'] is not None],.95),
                camera_ms_max=max([x['camera_ms'] for x in health if x['camera_ms'] is not None], default=None),
                camera_not_ready=sum(x['camera_ready'] is False for x in health),
                jetson_video_encoded_fps=(video_end-video_start)/(last['at']-first['at'])
                    if video_start is not None and video_end is not None and last['at']>first['at'] else None,
                observed_modes=sorted({x['control']['mode'] for x in health if x['control']['mode']}),
                observed_reasons=sorted({x['control']['reason'] for x in health if x['control']['reason']}),
                raw_pulls=0, errors=errors, health=health, pulls=pulls)
            reports.append(result); Path(options.output).write_text(json.dumps(reports,indent=2))
            print(json.dumps({k:v for k,v in result.items() if k not in ('health','pulls')}),flush=True)


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--origin',required=True)
    parser.add_argument('--health',default='http://127.0.0.1:8080')
    parser.add_argument('--path',default='/map/dense.bin', help='Measured dense page or existing static asset for unpaced path measurement')
    parser.add_argument('--mbps',nargs='+',type=float,default=[0,.5,1,2,3.5,5])
    parser.add_argument('--seconds',type=float,default=10)
    parser.add_argument('--output',required=True)
    asyncio.run(measure(parser.parse_args()))
