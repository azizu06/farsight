#!/usr/bin/env python3
"""Isolated paced synthetic JPEG -> matching fMP4 ordinal diagnostic (Mac only).
No servers, ports, phones, poses or repo writes. Run ONLY after QA owner says GO.
"""
import argparse, asyncio, json, os, re, statistics, struct, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
import cv2
import numpy as np
from tools.mac_render.mp4 import Mp4Reader, fragment_timing

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--frames',type=int,default=90)
parser.add_argument('--output',default='/tmp/farsight-vt-latency.json')
parser.add_argument('--cases',nargs='+',choices=['baseline','output-low-delay','x264-control'],default=['baseline','output-low-delay'])
args=parser.parse_args()
if not 90 <= args.frames <= 180:
    parser.error('--frames must be 90..180 to include the steady measurement window')
FPS=30; WIDTH=1280; HEIGHT=720
base=cv2.imread(str(Path(__file__).with_name('synthetic-room.png')))
if base is None:
    y,x=np.mgrid[:HEIGHT,:WIDTH]
    base=np.stack([(x//7+y//4)%256,(x//4)%256,(y//3)%256],axis=2).astype('uint8')
base=cv2.resize(base,(WIDTH,HEIGHT))
images=[]
for index in range(args.frames):
    image=base.copy()
    for bit in range(8):
        value=255 if index&(1<<bit) else 0
        cv2.rectangle(image,(20+48*bit,20),(50+48*bit,50),(value,value,value),-1)
    ok,jpeg=cv2.imencode('.jpg',image,[cv2.IMWRITE_JPEG_QUALITY,80]); assert ok
    images.append(jpeg.tobytes())

async def run(case):
    for key in list(os.environ):
        if key.startswith('MAC_RENDER_'): os.environ.pop(key)
    recorded = json.loads(Path(__file__).with_name('evidence.json').read_text())
    reference = next(item for item in recorded if item['case'] == case)
    command, name = list(reference['command']), reference['encoder']
    # Diagnostic log only; continuously drained to avoid stderr backpressure.
    # Replay the exact recorded arguments, independently of later product tuning.
    process=await asyncio.create_subprocess_exec(*command,stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
    inputs={}; events=[]; logs=[]; fragments=[]; raw_output=[]; drain_ms=[]
    started=time.monotonic()
    async def sender():
        for index,jpeg in enumerate(images):
            delay=started+index/FPS-time.monotonic()
            if delay>0: await asyncio.sleep(delay)
            before=time.monotonic();inputs[index]=dict(begin=before-started)
            process.stdin.write(jpeg);await process.stdin.drain()
            after=time.monotonic();inputs[index]['drained']=after-started
            drain_ms.append((after-before)*1000)
        process.stdin.close();await process.stdin.wait_closed()
    async def output():
        reader=Mp4Reader()
        while True:
            chunk=await process.stdout.read(65536)
            if not chunk: break
            arrival=time.monotonic()-started;raw_output.append(chunk)
            for kind,data in reader.feed(chunk):
                if kind=='fragment':
                    first,count,_,_=fragment_timing(data,FPS)
                    fragments.append(dict(first=first,count=count,arrival=arrival,bytes=len(data)))
    async def stderr():
        while True:
            line=await process.stderr.readline()
            if not line:break
            decoded=line.decode(errors='replace').strip()
            logs.append(decoded)
            if '->' in decoded or '<-' in decoded:events.append(dict(arrival=time.monotonic()-started,line=decoded))
    try:
        await asyncio.wait_for(asyncio.gather(sender(),output(),stderr()),15)
        code=await asyncio.wait_for(process.wait(),3)
        assert code==0,'\n'.join(logs[-20:])
    finally:
        if process.returncode is None:
            process.kill();await process.wait()
    output_path=Path('/tmp/farsight-vt-'+case+'.mp4');output_path.write_bytes(b''.join(raw_output))
    Path('/tmp/farsight-vt-'+case+'.stderr').write_text('\n'.join(logs)+'\n')
    # Decode synthetic bit markers to prove actual picture ordinal, not only PTS.
    decoded=await asyncio.create_subprocess_exec('ffmpeg','-hide_banner','-loglevel','error','-threads','1',
        '-i',str(output_path),'-vf','crop=384:40:16:16,format=gray','-f','rawvideo','pipe:1',
        stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
    pixels,errors=await asyncio.wait_for(decoded.communicate(),10)
    assert decoded.returncode==0,errors.decode()
    frame_bytes=384*40; assert len(pixels)==args.frames*frame_bytes,(len(pixels),args.frames)
    picture_ordinals=[]
    for offset in range(0,len(pixels),frame_bytes):
        picture_ordinals.append(sum((1<<bit) for bit in range(8) if pixels[offset+19*384+19+48*bit]>127))
    assert picture_ordinals==list(range(args.frames)),picture_ordinals
    represented=[n for fragment in fragments for n in range(fragment['first'],fragment['first']+fragment['count'])]
    assert represented==list(range(args.frames)),represented
    live=[f for f in fragments if 15<=f['first']<args.frames-15]
    latency=[(f['arrival']-inputs[f['first']]['begin'])*1000 for f in live]
    all_latency=[(f['arrival']-inputs[f['first']]['begin'])*1000 for f in fragments]
    return dict(case=case,encoder=name,command=command,scope='Mac synthetic rendered-room JPEG paced30fps only; markers prove picture ordinals',
        frames=args.frames,jpeg_bytes_median=statistics.median(map(len,images)),one_frame_fragments=all(f['count']==1 for f in fragments),
        picture_ordinals_verified=True,drain_ms_median=statistics.median(drain_ms),steady_input_to_fragment_ms_median=statistics.median(latency),
        steady_input_to_fragment_ms_p95=sorted(latency)[int(.95*(len(latency)-1))],first_input_to_fragment_ms=all_latency[0],
        inputs=inputs,fragments=fragments,debug_events=events)

async def main():
    results=[]
    for case in args.cases:
        result=await run(case);results.append(result)
        print(json.dumps({k:v for k,v in result.items() if k not in ('inputs','fragments','debug_events','command')}),flush=True)
    Path(args.output).write_text(json.dumps(results,indent=2)+'\n')
asyncio.run(main())
