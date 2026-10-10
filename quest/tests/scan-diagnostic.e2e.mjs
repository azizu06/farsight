// Software-only bounded scan diagnostic. Own simulated backend/relay and browser.
// node quest/tests/scan-diagnostic.e2e.mjs --out /tmp/scan-diagnostic.json
// Every failed/aborted profile is retained in the report and exits nonzero; no retry hides stalls.
import { spawn } from 'node:child_process';
import { existsSync, writeFileSync } from 'node:fs';
import net from 'node:net';
import { fileURLToPath } from 'node:url';
import { chromium } from '@playwright/test';
const root = fileURLToPath(new URL('../..', import.meta.url));
const python = existsSync(`${root}.venv/bin/python`) ? `${root}.venv/bin/python` : 'python3';
const sleep = ms => new Promise(r => setTimeout(r, ms));
const outputIndex = process.argv.indexOf('--out');
const output = outputIndex >= 0 ? process.argv[outputIndex + 1] : '/tmp/scan-diagnostic.json';
if (!output) throw Error('--out requires a path');
const children = [];
async function port() { return new Promise(resolve => { const s=net.createServer();s.listen(0,'127.0.0.1',()=>{ const p=s.address().port;s.close(()=>resolve(p)); }); }); }
function start(args) {const p=spawn(python,args,{cwd:root,detached:true,stdio:['ignore','pipe','pipe']});let log=''; for(const stream of [p.stdout,p.stderr])stream.on('data',d=>{log=(log+d).slice(-3000);});children.push(p); return {p,log:()=>log};}
async function json(url) { const r=await fetch(url,{signal:AbortSignal.timeout(2000)});if(!r.ok)throw Error(`HTTP${r.status}`);return r.json(); }
async function until(check,ms=10000) { const end=Date.now()+ms;let last;while(Date.now()<end) {try {last=await check();if(last)return last;}catch{}await sleep(30);}throw Error(`Wait deadline; last=${last}`); }
let browser;
const results=[];
try {
 const [backendPort,relayPort]=await Promise.all([port(),port()]);
 const backend=start(['-m','jetson.server','--demo','--simulate-rover','--host','127.0.0.1','--port',String(backendPort)]);
 const direct=`http://127.0.0.1:${backendPort}`;
 await until(async()=>{if(backend.p.exitCode!==null)throw Error(backend.log());return (await json(direct+'/health')).camera;});
 const relay=start(['-m','tools.operator_relay','--upstream',direct,'--port',String(relayPort)]);
 const origin=`http://127.0.0.1:${relayPort}`;await until(()=>json(origin+'/relay/health'));
 browser=await chromium.launch({args:['--no-sandbox','--disable-dev-shm-usage','--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader']});
 for(const profile of [{name:'baseline',postDelayMs:0,cpuRate:1},{name:'delayed-response',postDelayMs:300,cpuRate:1},{name:'cpu-four',postDelayMs:0,cpuRate:4}]) {
  const page=await browser.newPage({viewport:{width:960,height:600}});
  await page.addInitScript(({postDelayMs})=>{
   const trace=[];let lastCameraDraw=-Infinity;globalThis.__scanTrace=trace;const record=(kind,fields={})=>{trace.push({wall_ms:Date.now(),at:performance.now(),kind,...fields});if(trace.length>2000)trace.shift();};
   const Native=window.WebSocket;window.WebSocket=class extends Native {
    constructor(url,...args){super(url,...args);if(String(url).includes('/ws/control'))this.addEventListener('message',e=>{try{const p=JSON.parse(e.data);record('status',{seq:p.seq,mode:p.mode,requires_release:p.requires_release,reason:p.reason,left_pwm:p.left_pwm,right_pwm:p.right_pwm});}catch{}});}
    send(message){if(this.url.includes('/ws/control')){try{const p=JSON.parse(message);record('command',{seq:p.seq,deadman:p.deadman,mode:p.mode,video_age_ms:p.video_age_ms,buffered_before:this.bufferedAmount,display_age_ms:performance.now()-lastCameraDraw});}catch{}}const result=super.send(message);if(this.url.includes('/ws/control'))record('send-buffer',{buffered_after:this.bufferedAmount});return result;}
    close(...args){if(this.url.includes('/ws/control'))record('client-close',{buffered:this.bufferedAmount,stack:new Error().stack});return super.close(...args);}
   };
   const nativeFetch=window.fetch.bind(window);window.fetch=async (...args)=>{
    const url=String(args[0]),scan=url.endsWith('/scan')&&args[1]?.method==='POST';if(scan)record('scan-post-start');
    const response=await nativeFetch(...args);
    if(scan){record('scan-post-response',{status:response.status});if(postDelayMs)await new Promise(r=>setTimeout(r,postDelayMs));record('scan-post-delivered');}
    if(url.includes('/camera.jpg'))record('jpeg-received',{frame:response.headers.get('X-Frame-Id'),age_ms:Number(response.headers.get('X-Frame-Age-Ms'))});
    return response;
   };
   const draw=CanvasRenderingContext2D.prototype.drawImage;CanvasRenderingContext2D.prototype.drawImage=function(...args){if(this.canvas.id==='rover-camera'){lastCameraDraw=performance.now();record('camera-draw');}return draw.apply(this,args);};
  },profile);
  const cdp=await page.context().newCDPSession(page);await cdp.send('Emulation.setCPUThrottlingRate',{rate:profile.cpuRate});
  await page.goto(origin);await page.locator('#rover-message').waitFor();
  const server=[];
  let polling=true;
  const pollingTask=(async()=>{while(polling){try{const h=await json(direct+'/health');server.push({wall_ms:Date.now(),camera_age_ms:h.camera_age_ms,camera_frame:h.camera_frame,detector_ms:h.detector_inference_ms,processing_ms:h.processing_ms,control:{seq:h.control.seq,reason:h.control.reason,requires_release:h.control.requires_release,mode:h.control.mode},scan:h.scan});if(server.length>400)server.shift();}catch{}await sleep(40);}})();
  let outcome;
  try {
   await until(async()=>/Ready: hold deadman/.test(await page.locator('#rover-message').textContent())&&!(await json(direct+'/control/status')).requires_release,12000);
   await page.keyboard.down('Space');await until(async()=>(await json(direct+'/control/status')).reason==='active',3000);
   await page.locator('#rover-scan').click();
   outcome=await until(async()=>{const s=await json(direct+'/scan');return ['complete','aborted'].includes(s.state)&&s;},12000);
   await sleep(350);
  } catch(e){outcome={state:'diagnostic-error',error:String(e)};}
  await page.keyboard.up('Space');polling=false;await pollingTask;
  const trace=await page.evaluate(()=>globalThis.__scanTrace);
  const details=await page.locator('#rover-details').textContent();
  const post=trace.find(e=>e.kind==='scan-post-start');const delivered=trace.find(e=>e.kind==='scan-post-delivered');
  const commands=trace.filter(e=>e.kind==='command'&&e.deadman);
  const pendingCommands=commands.filter(e=>post&&e.at>=post.at&&(!delivered||e.at<=delivered.at));
  const gaps=commands.slice(1).map((e,i)=>e.at-commands[i].at);
  const result={profile,outcome,details,server,trace,pending_held_commands:pendingCommands.length,max_held_command_gap_ms:Math.max(0,...gaps),max_camera_age_ms:Math.max(0,...server.map(e=>e.camera_age_ms||0))};results.push(result);
  console.log(JSON.stringify({profile,outcome,details,pending_held_commands:pendingCommands.length,max_held_command_gap_ms:result.max_held_command_gap_ms,max_camera_age_ms:result.max_camera_age_ms}));
  await page.close();await sleep(250);
 }
 if(results.some(result=>result.outcome.state!=='complete'))process.exitCode=1;
} finally {
 if(browser)await browser.close();
 for(const p of children)if(p.exitCode===null)try{process.kill(-p.pid,'SIGTERM');}catch{}
 await sleep(1000);for(const p of children)if(p.exitCode===null)try{process.kill(-p.pid,'SIGKILL');}catch{}
 writeFileSync(output,JSON.stringify({scope:'Software-only SIMULATED backend and SwiftShader browser. No target, phones or physical motors. Delay explicitly holds POST response delivery; CPU throttle uses Chromium CDP.',results},null,2));
}
