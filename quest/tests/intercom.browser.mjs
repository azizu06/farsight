import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {mkdtemp,writeFile,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {chromium} from '@playwright/test';

// Actual Chromium audio processing; generated microphone and simulated phone transport.
// No hardware, rover endpoint, motor commands or production service.
const origin='http://127.0.0.1:5259';
const temporary=await mkdtemp(join(tmpdir(),'farsight-intercom-browser-'));
const wav=Buffer.alloc(44+48_000*2*4);
wav.write('RIFF');wav.writeUInt32LE(wav.length-8,4);wav.write('WAVEfmt ',8);
wav.writeUInt32LE(16,16);wav.writeUInt16LE(1,20);wav.writeUInt16LE(1,22);
wav.writeUInt32LE(48_000,24);wav.writeUInt32LE(96_000,28);wav.writeUInt16LE(2,32);
wav.writeUInt16LE(16,34);wav.write('data',36);wav.writeUInt32LE(wav.length-44,40);
for(let i=0;i<48_000*4;i++)wav.writeInt16LE(Math.round(8192*Math.sin(2*Math.PI*440*i/48_000)),44+i*2);
const audioPath=join(temporary,'microphone.wav');await writeFile(audioPath,wav);
const server=spawn(process.execPath,['--input-type=module','-e',
  "import {createServer} from 'vite';const s=await createServer({server:{host:'127.0.0.1',port:5259,strictPort:true,watch:null}});await s.listen();"],
  {cwd:new URL('..',import.meta.url),stdio:'pipe'});
let log='',browser;
server.stdout.on('data',v=>log+=v);server.stderr.on('data',v=>log+=v);
try{
  let ready=false;
  for(let i=0;i<100;i++){
    if(server.exitCode!==null)throw Error(log);
    try{ready=(await fetch(origin)).ok;}catch{}
    if(ready)break;await new Promise(r=>setTimeout(r,100));
  }
  assert.ok(ready,log);
  browser=await chromium.launch({args:['--no-sandbox','--use-fake-ui-for-media-stream',
    '--use-fake-device-for-media-stream',`--use-file-for-fake-audio-capture=${audioPath}`,
    '--autoplay-policy=no-user-gesture-required']});
  const page=await browser.newPage();
  await page.route('**/audio-fixture',r=>r.fulfill({contentType:'text/html',body:'<!doctype html><title>Audio fixture</title>'}));
  await page.goto(`${origin}/audio-fixture`);
  const result=await page.evaluate(async()=>{
    const {QuestIntercom,INTERCOM_FRAME_BYTES}=await import('/src/intercom.ts');
    const packet=new ArrayBuffer(INTERCOM_FRAME_BYTES),pcm=new DataView(packet);
    for(let i=0;i<320;i++)pcm.setInt16(i*2,8192,true);
    const render=async count=>{
      const context=new OfflineAudioContext(1,48_000/2,48_000);
      const output=new QuestIntercom(()=>{});
      output.context=context;output.requested=true;output.serverActive=true;
      for(let i=0;i<count;i++)output.play(packet);
      const rendered=await context.startRendering();
      let peak=0,loudSamples=0,windowSum=0,windowRMS=0;
      const values=rendered.getChannelData(0);
      for(let i=0;i<values.length;i++){
        const value=Math.abs(values[i]);peak=Math.max(peak,value);
        if(value>.26)loudSamples++;
        windowSum+=value*value;
        if(i>=240)windowSum-=values[i-240]*values[i-240];
        if(i>=239)windowRMS=Math.max(windowRMS,Math.sqrt(Math.max(0,windowSum)/240));
      }
      return {peak,loudSamples,windowRMS};
    };
    const single=await render(1),batch=await render(11),singlePeak=single.peak,peak=batch.peak;

    // Real getUserMedia, AudioContext and processor/encoder; only the peer is simulated.
    const originalSocket=window.WebSocket;
    const frames=[];
    class PhonePeer{
      readyState=1;bufferedAmount=0;
      constructor(){queueMicrotask(()=>{this.onopen?.();this.onmessage?.({data:JSON.stringify({type:'peer',connected:true})});});}
      send(value){if(typeof value==='string')queueMicrotask(()=>this.onmessage?.({data:value}));else frames.push(value);}
      close(){this.readyState=3;}
    }
    window.WebSocket=PhonePeer;
    const live=new QuestIntercom(()=>{});
    try{
      live.connectIfNeeded();await new Promise(r=>setTimeout(r,0));
      await live.toggle();
      const deadline=performance.now()+5000;
      while(frames.length<3&&performance.now()<deadline){
        live.socket.onmessage({data:packet});
        await new Promise(r=>setTimeout(r,20));
      }
      let microphonePeak=0;
      for(const frame of frames){const view=new DataView(frame);for(let i=0;i<320;i++)microphonePeak=Math.max(microphonePeak,Math.abs(view.getInt16(i*2,true)));}
      const before=frames.length;
      live.disable(true);await new Promise(r=>setTimeout(r,60));
      return {peak,singlePeak,single,batch,frames:before,frameSizes:frames.map(f=>f.byteLength),microphonePeak,framesAfterOff:frames.length,
        enabledAfterOff:live.enabled};
    }finally{live.dispose();window.WebSocket=originalSocket;}
  });
  assert.ok(result.singlePeak>=.24&&result.peak>=.24,'the received phone audio must actually render');
  // Native 16→48 kHz rendering duplicates occasional one-sample buffer endpoints.
  // A 5 ms energy window detects overlapping speech frames without treating those
  // isolated sample boundaries as a second speaker.
  assert.ok(result.batch.windowRMS<=result.single.windowRMS*1.02,
    `a packet batch must not amplify one voice; single RMS=${result.single.windowRMS}, batch RMS=${result.batch.windowRMS}`);
  assert.ok(result.frames>=3,'microphone frames must flow during incoming playback');
  assert.ok(result.microphonePeak>100,'actual generated microphone signal is captured and encoded');
  assert.ok(result.frameSizes.every(size=>size===640));
  assert.equal(result.framesAfterOff,result.frames);assert.equal(result.enabledAfterOff,false);
  console.log(`PASS rendered audio RMS ${result.batch.windowRMS.toFixed(3)} vs single ${result.single.windowRMS.toFixed(3)}, simultaneous real browser capture ${result.frames} PCM frames, toggle off; SOFTWARE ONLY`);
}finally{
  await browser?.close();
  if(server.exitCode===null){server.kill('SIGTERM');await new Promise(r=>server.once('exit',r));}
  await rm(temporary,{recursive:true,force:true});
}
