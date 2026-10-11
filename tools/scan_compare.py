"""Serve the isolated A/B/C comparison against the existing local operator relay."""
import argparse
import asyncio
from pathlib import Path
import aiohttp
from aiohttp import web
DIST=Path(__file__).resolve().parents[1]/'.cache/scan-compare-dist'

def create_app(upstream='http://127.0.0.1:8080',dist=DIST):
    app=web.Application()
    async def lifecycle(app):
        # Separate raw reads from local point bootstrap; browser compression is
        # negotiated by aiohttp and decoded before the same-origin response.
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as dense, \
                aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as raw:
            app['dense_session'],app['raw_session']=dense,raw
            yield
    app.cleanup_ctx.append(lifecycle)
    async def proxy(request):
        capture=request.path=='/capture/frame.bin'
        path='/phone/frame.bin' if capture else request.path
        session=app['raw_session'] if capture else app['dense_session']
        try:
            async with session.get(upstream.rstrip('/')+path,params=request.query,allow_redirects=False) as reply:
                body=await reply.read()
                headers={k:v for k,v in reply.headers.items() if k.lower() in ('x-capture-key','x-capture-age-ms')}
                return web.Response(body=body,status=reply.status,content_type=reply.content_type,headers=headers)
        except (aiohttp.ClientError,asyncio.TimeoutError) as error:
            raise web.HTTPBadGateway(text=f'Camera relay unavailable: {type(error).__name__}')
    async def index(request):
        return web.FileResponse(Path(dist)/'index.html')
    app.router.add_get('/',index)
    for path in ('/dense/delta.bin','/dense/status','/capture/frame.bin'):
        app.router.add_get(path,proxy)
    app.router.add_static('/assets/',Path(dist)/'assets')
    return app

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port',type=int,default=8097)
    parser.add_argument('--upstream',default='http://127.0.0.1:8080')
    args=parser.parse_args()
    web.run_app(create_app(args.upstream),host='127.0.0.1',port=args.port)
if __name__=='__main__':main()
