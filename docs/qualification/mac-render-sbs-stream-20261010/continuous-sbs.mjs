import { chromium } from '/Users/sairamen/projects/farsight/quest/node_modules/playwright/index.mjs';
import { createRequire } from 'node:module';
import { readFile, mkdir, writeFile } from 'node:fs/promises';
const require = createRequire('/Users/sairamen/projects/farsight/quest/package.json');
const iwer = await readFile(require.resolve('iwer/build/iwer.min.js'), 'utf8');
const origin = 'http://127.0.0.1:18873', out = '/tmp/farsight-iwer-continuous-sbs-evidence';
await mkdir(out, { recursive: true });
const report = { class: 'Actual canonical MacAutoView/H264Viewer + IWER software-only continuous SBS',
  clientRevision: '97d9063', producerRevision: '1af4771', origin,
  scope: 'SIMULATED source and emulated Quest2 XR; no physical headset or motion acceptance.',
  errors: [], conditions: { simulatedControllerConnectedOverride: true, diagnosticSeekIntervention: false, seconds: 8 } };
let browser;
const stats = values => { const sorted = [...values].sort((a,b)=>a-b); const at=q=>sorted.length?sorted[Math.min(sorted.length-1,Math.floor(q*sorted.length))]:null;return {count:sorted.length,p50:at(.5),p95:at(.95),maximum:sorted.at(-1)??null}; };
try {
  browser = await chromium.launch({headless:true,args:['--use-angle=metal','--enable-gpu','--ignore-gpu-blocklist','--autoplay-policy=no-user-gesture-required']});
  const context = await browser.newContext({viewport:{width:1280,height:720},serviceWorkers:'block'});
  await context.grantPermissions(['local-network-access'],{origin});
  const page = await context.newPage();page.setDefaultTimeout(10000);
  page.on('pageerror',error=>report.errors.push(error.message));
  await page.addInitScript(iwer+'\nwindow.__device=new IWER.XRDevice(IWER.metaQuest2);window.__device.installRuntime({forceInstall:true});');
  await page.addInitScript(origin=>{
    const probe=window.__continuousSbs={videos:[],labels:[],sockets:[],segments:[],init:null,poseSent:0,matchedEchoes:0,lastPose:null,poses:new Map(),blocked:[],controlSockets:0};
    const create=document.createElement.bind(document);
    document.createElement=function(name,...args){const element=create(name,...args);if(String(name).toLowerCase()==='video'){
      const entry={video:element,samples:[],seeks:0,seeked:0};probe.videos.push(entry);
      element.addEventListener('seeking',()=>entry.seeks++);element.addEventListener('seeked',()=>entry.seeked++);
      const frame=(now,meta)=>{if(entry.samples.length<10000)entry.samples.push({now,received:performance.now(),mediaTime:meta.mediaTime,presentedFrames:meta.presentedFrames,expectedDisplayTime:meta.expectedDisplayTime});element.requestVideoFrameCallback(frame);};element.requestVideoFrameCallback?.(frame);
    }return element;};
    const text=CanvasRenderingContext2D.prototype.fillText;
    CanvasRenderingContext2D.prototype.fillText=function(value,...args){if(/MAC RENDER|MAC VIEW LOST|AUTO/.test(String(value))&&probe.labels.length<1000)probe.labels.push(String(value));return text.call(this,value,...args);};
    const Native=window.WebSocket;
    window.WebSocket=class extends Native{
      constructor(value,protocols){const url=new URL(String(value),location.href);const allowed=['/ws/control','/ws/video','/ws/detections','/ws/map','/ws/macview','/ws/macview-pose'];
        if(url.origin!==origin.replace(/^http/,'ws')||!allowed.includes(url.pathname)){probe.blocked.push(url.href);throw new DOMException('Only the agreed SIM relay origin is permitted','SecurityError');}
        super(value,protocols);this.__fixturePath=url.pathname;probe.sockets.push(url.href);if(url.pathname==='/ws/control')probe.controlSockets++;
        if(url.pathname==='/ws/macview')this.addEventListener('message',event=>{if(typeof event.data!=='string'||event.data.length>65536)return;try{
          const packet=JSON.parse(event.data);if(packet.type==='video_init')probe.init=packet;
          if(packet.type==='video_segment'){
            if(probe.segments.length<10000)probe.segments.push({received:performance.now(),...packet});
            const echo=packet.pose;const sent=echo&&probe.poses.get(`${echo.session_id}:${echo.frame_id??echo.pose_id}`);
            if(sent&&sent.eyes?.length===2&&echo.eyes?.length===2&&echo.eyes[0].eye==='left'&&echo.eyes[1].eye==='right')probe.matchedEchoes++;
          }
        }catch{}});
      }
      send(value){if(this.__fixturePath==='/ws/macview-pose'&&typeof value==='string')try{const pose=JSON.parse(value);if(pose.type==='pose'){
        probe.poseSent++;probe.lastPose=pose;probe.poses.set(`${pose.session_id}:${pose.frame_id??pose.pose_id}`,pose);if(probe.poses.size>512)probe.poses.delete(probe.poses.keys().next().value);
      }}catch{}return super.send(value);}
    };
  },origin);
  await page.goto(origin+'/?macview=1&stereo=1&diag=1',{waitUntil:'domcontentloaded'});
  await page.waitForTimeout(1800);await page.locator('#VRButton').click();await page.waitForTimeout(800);
  await page.evaluate(()=>{const device=window.__device;for(const hand of ['left','right'])Object.defineProperty(Object.getPrototypeOf(device.controllers[hand].inputSource.gamepad),'connected',{configurable:true,get:()=>true});device.controllers.right.updateButtonValue('trigger',0);});
  await page.waitForTimeout(700);await page.evaluate(()=>window.__device.controllers.right.updateButtonValue('a-button',1));
  await page.waitForTimeout(100);await page.evaluate(()=>window.__device.controllers.right.updateButtonValue('a-button',0));
  await page.waitForFunction(()=>window.__continuousSbs.videos.some(entry=>entry.video.videoWidth===1280&&entry.video.videoHeight===720&&entry.samples.length>0)&&window.__continuousSbs.matchedEchoes>0,undefined,{timeout:15000});
  const before=await page.evaluate(()=>{
    const p=window.__continuousSbs,e=p.videos.find(e=>e.video.videoWidth===1280&&e.samples.length>0);p.mac=e;e.samples=[];e.seeks=0;e.seeked=0;p.segments=[];p.matchedEchoes=0;p.poseSent=0;
    const q=e.video.getVideoPlaybackQuality();return{now:performance.now(),quality:{total:q.totalVideoFrames,dropped:q.droppedVideoFrames}};
  });
  await page.waitForTimeout(8000);
  const after=await page.evaluate(()=>{
    const p=window.__continuousSbs,e=p.mac,v=e.video,q=v.getVideoPlaybackQuality();return{now:performance.now(),samples:e.samples,seeks:e.seeks,seeked:e.seeked,width:v.videoWidth,height:v.videoHeight,
      quality:{total:q.totalVideoFrames,dropped:q.droppedVideoFrames},segments:p.segments,init:p.init,poseSent:p.poseSent,matchedEchoes:p.matchedEchoes,lastPose:p.lastPose,labels:p.labels,
      sockets:p.sockets,controlSockets:p.controlSockets,blocked:p.blocked,ready:v.readyState,paused:v.paused,message:document.querySelector('#rover-message')?.textContent,
      details:document.querySelector('#rover-details')?.textContent,xrButton:document.querySelector('#VRButton')?.textContent};
  });
  const elapsed=(after.now-before.now)/1000;
  report.measurement={seconds:elapsed,callbacks:after.samples.length,uniqueMediaTimes:new Set(after.samples.map(s=>s.mediaTime)).size,uniquePresentedFrames:new Set(after.samples.map(s=>s.presentedFrames)).size,
    uniqueMediaHz:new Set(after.samples.map(s=>s.mediaTime)).size/elapsed,gapMs:stats(after.samples.slice(1).map((s,i)=>s.now-after.samples[i].now)),seeks:after.seeks,seeked:after.seeked,
    width:after.width,height:after.height,rawQualityDelta:{total:after.quality.total-before.quality.total,dropped:after.quality.dropped-before.quality.dropped},qualityScope:'Raw browser counters, not missed XR frames'};
  const stages=['age_ms','encode_pipeline_ms','capture_eval_ms','server_wait_ms','pose_to_frame_ms'];
  report.sidecarTimingMs=Object.fromEntries(stages.map(key=>[key,stats(after.segments.map(s=>s[key]).filter(Number.isFinite))]));
  report.stream={init:after.init,segments:after.segments.length,matchingPairEchoes:after.matchedEchoes,poseSent:after.poseSent,lastPose:after.lastPose,lastSegment:after.segments.at(-1),sockets:after.sockets};
  report.app={labels:after.labels,message:after.message,details:after.details,xrButton:after.xrButton,controlSockets:after.controlSockets};
  report.frameTail=after.samples.slice(-10);report.blocked=after.blocked;
  report.videoPixels=await page.evaluate(()=>{
    const v=window.__continuousSbs.mac.video,started=performance.now(),canvas=document.createElement('canvas');canvas.width=128;canvas.height=72;
    try{const c=canvas.getContext('2d',{willReadFrequently:true});c.drawImage(v,0,0,128,72);const data=c.getImageData(0,0,128,72).data;let nonblack=0,difference=0;
      for(let y=0;y<72;y++)for(let x=0;x<128;x++){const i=(y*128+x)*4;if(Math.max(data[i],data[i+1],data[i+2])>3)nonblack++;if(x<64){const j=(y*128+x+64)*4;difference+=Math.abs(data[i]-data[j])+Math.abs(data[i+1]-data[j+1])+Math.abs(data[i+2]-data[j+2]);}}
      return{state:'readable',width:128,height:72,nonblackPixels:nonblack,totalPixels:128*72,meanHalfAbsoluteRgbDifference:difference/(64*72*3),readElapsedMs:performance.now()-started,scope:'One post-sample decoded-video read; may stall final snapshot; scene is SIMULATED'};
    }catch(error){return{state:'unknown',reason:error.name+': '+error.message};}
  });
  await page.screenshot({path:out+'/screenshot.png',timeout:5000});
  report.checks={xrEmulationPresenting:after.xrButton==='EXIT VR',coded1280x720:after.width===1280&&after.height===720,declaredPackedLayout:after.init?.stereo===true&&after.init?.layout==='sbs-left-right',
    advancingAtLeast20UniqueHz:report.measurement.uniqueMediaHz>=20,matchingPosePairs:after.matchedEchoes>0,sourceSimulated:after.segments.some(s=>s.source==='SIMULATED'),
    stereoLabelPainted:after.labels.some(label=>/MAC RENDER.*STEREO/.test(label)),videoNonblack:report.videoPixels.state==='readable'&&report.videoPixels.nonblackPixels>0,
    halvesDistinct:report.videoPixels.state==='readable'&&report.videoPixels.meanHalfAbsoluteRgbDifference>.1,singleSimControlSocket:after.controlSockets===1,noForeignSocket:after.blocked.length===0};
  report.passed=Object.values(report.checks).every(Boolean)&&report.errors.length===0;
}catch(error){report.errors.push(error.stack||error.message);report.passed=false;}finally{await browser?.close();}
await writeFile(out+'/evidence.json',JSON.stringify(report,null,2)+'\n');
console.log(JSON.stringify({passed:report.passed,checks:report.checks,measurement:report.measurement,pixels:report.videoPixels,errors:report.errors,evidence:out},null,2));
