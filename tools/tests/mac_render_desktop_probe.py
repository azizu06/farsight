import asyncio,json,math,time
from pathlib import Path
import aiohttp
import argparse
args=argparse.ArgumentParser(description='Isolated SIMULATED Mac preview regression; visual poses only')
args.add_argument('--origin',required=True)
args.add_argument('--output',default='/tmp/farsight-mac-desktop-proof.json')
options=args.parse_args()
BASE=options.origin.rstrip('/')
async def run():
    async with aiohttp.ClientSession() as client:
        for _ in range(40):
            health=await (await client.get(BASE+'/health')).json()
            if health.get('ready'): break
            await asyncio.sleep(.1)
        assert health.get('ready'),health
        assert health.get('map',{}).get('source')=='SIMULATED','Refusing visual-camera changes outside the SIMULATED fixture'
        video=await client.ws_connect(BASE+'/ws/macview?layout=sbs-left-right')
        init=await video.receive_json(timeout=5); assert init['stereo'] is True,init
        await video.receive(timeout=5)
        request=0
        async def frame():
            nonlocal request
            request+=1; await video.send_json({'type':'next','request_id':request})
            message=await video.receive_json(timeout=5)
            assert message['type']=='video_segment',message
            binary=await video.receive(timeout=5); assert binary.type==aiohttp.WSMsgType.BINARY
            return message
        idle=await frame()
        assert idle['pose']['session_id'].startswith('desktop-preview-'),idle
        pose=await client.ws_connect(BASE+'/ws/macview-pose')
        near,far=.01,2000; f=1/math.tan(math.radians(25))
        projection=[f/(480/540),0,0,0,0,f,0,0,0,0,-(far+near)/(far-near),-1,0,0,-2*far*near/(far-near),0]
        active=True
        async def sender():
            seq=0
            while active:
                seq+=1
                await pose.send_json({'type':'pose','version':1,'tracked':True,'space':'ar_map','session_id':'held-headset-test','frame_id':seq,'client_nonce':'actual-repro',
                    'sent_mono_ms':time.monotonic()*1000,'sent_wall_ms':time.time()*1000,'head':{'position':[0,3,9],'quaternion':[0,0,0,1]},'stereo':True,
                    'eyes':[{'eye':name,'position':[x,3,9],'quaternion':[0,0,0,1],'projection':projection} for name,x in [('left',-.032),('right',.032)]]})
                await asyncio.sleep(1/30)
        sending=asyncio.create_task(sender())
        for _ in range(40):
            live=await frame()
            if live['pose']['session_id']=='held-headset-test': break
        assert live['pose']['session_id']=='held-headset-test',live
        active=False; await sending
        await asyncio.sleep(.8)
        for _ in range(40):
            expired=await frame()
            if expired['pose']['session_id'].startswith('desktop-preview-'): break
        assert not pose.closed
        assert expired['pose']['session_id'].startswith('desktop-preview-'),expired
        after=await (await client.get(BASE+'/health')).json()
        proof={'scope':'Production renderer with actual encoder; SIMULATED geometry, illustrative eyes, no XR hardware/control commands',
               'ready_without_pose':health['ready'],'init':init,'no_pose':idle,'active_pose':live,'expired_but_connected':expired,
               'held_socket_still_connected':not pose.closed,'ready_after_expiry':after['ready'],'stereo_after_expiry':after['stereo']}
        Path(options.output).write_text(json.dumps(proof,indent=2))
        print(json.dumps({'passed':True,'ready_without_pose':health['ready'],'active_session':live['pose']['session_id'],
               'fallback_session':expired['pose']['session_id'],'held_socket_still_connected':not pose.closed,'ready_after_expiry':after['ready']}))
        await pose.close(); await video.close()
asyncio.run(run())
