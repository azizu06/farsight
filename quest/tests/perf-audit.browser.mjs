// Canonical built app, isolated SIM relay only. No physical target or motor commands.
// PERF_ORIGIN=http://127.0.0.1:<relay> PERF_GL=metal|swiftshader node tests/perf-audit.browser.mjs
// CPU throttling and IWER are comparative desktop diagnostics, not Quest qualification.
import { readFileSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import { chromium } from "@playwright/test";

const origin = process.env.PERF_ORIGIN;
if (!origin) throw new Error("PERF_ORIGIN must name an isolated SIM relay");
const health = await fetch(`${origin}/health`).then(r => r.json());
if (!health.control?.simulated) throw new Error("Refusing a non-simulated backend");
const seconds = Number(process.env.PERF_SECONDS ?? 8);
const throttle = Number(process.env.PERF_CPU_THROTTLE ?? 4);
const gpu = process.env.PERF_GL ?? "metal";
const gpuTimers = process.env.PERF_GPU_TIMERS !== "0";
const output = process.env.PERF_OUTPUT ?? `/tmp/farsight-vr-audit-${gpu}.json`;
const iwer = readFileSync(createRequire(import.meta.url).resolve("iwer/build/iwer.min.js"), "utf8");
const probe = `${iwer}
window.__device=new IWER.XRDevice(IWER.metaQuest2); window.__device.installRuntime({forceInstall:true});
(() => {
 const p=window.__perf={phase:'desktop',frames:[],gaps:[],uploads:0,bytes:0,uploadMs:0,draws:0,bySize:{},byKind:{},gpu:[],queryQueue:[],gpuStatus:'not available'};
 const frame=(cb,kind)=>(...args)=>{
  if(p.phase!==kind)return cb(...args);
  const now=performance.now(); if(p.lastFrame!==undefined)p.gaps.push(now-p.lastFrame);p.lastFrame=now;
  const gl=p.gl,ext=p.ext; let query;
  if(${gpuTimers}&&gl&&ext&&!gl.getParameter(ext.GPU_DISJOINT_EXT)){
   while(p.queryQueue.length&&gl.getQueryParameter(p.queryQueue[0],gl.QUERY_RESULT_AVAILABLE)){
    const q=p.queryQueue.shift();p.gpu.push(gl.getQueryParameter(q,gl.QUERY_RESULT)/1e6);gl.deleteQuery(q);
   }
   if(p.queryQueue.length<8){query=gl.createQuery();gl.beginQuery(ext.TIME_ELAPSED_EXT,query);}
  }
  p.kinds=[];const start=performance.now();
  try{return cb(...args);}finally{
   const ms=performance.now()-start;p.frames.push(ms);
   const key=p.kinds.sort().join('+')||'none';(p.byKind[key]??=[]).push(ms);
   if(query){gl.endQuery(ext.TIME_ELAPSED_EXT);p.queryQueue.push(query);}
  }
 };
 const raf=window.requestAnimationFrame.bind(window);window.requestAnimationFrame=cb=>raf(frame(cb,'desktop'));
 const xr=XRSession.prototype.requestAnimationFrame;XRSession.prototype.requestAnimationFrame=function(cb){return xr.call(this,frame(cb,'xr'));};
 const get=HTMLCanvasElement.prototype.getContext;HTMLCanvasElement.prototype.getContext=function(...args){
  const gl=get.apply(this,args);if(args[0]==='webgl2'&&!p.gl&&gl){p.gl=gl;p.ext=gl.getExtension('EXT_disjoint_timer_query_webgl2');p.gpuStatus=p.ext?'timer query':'timer query unavailable';}return gl;
 };
 for(const C of [WebGLRenderingContext,WebGL2RenderingContext]){
  for(const name of ['drawArrays','drawElements','drawArraysInstanced','drawElementsInstanced']){
   const fn=C.prototype[name];if(fn)C.prototype[name]=function(...args){p.draws++;return fn.apply(this,args);};
  }
  for(const name of ['texImage2D','texSubImage2D']){
   const fn=C.prototype[name];C.prototype[name]=function(...args){
    const src=args.find(a=>a&&typeof a==='object'&&('width'in a||'videoWidth'in a));
    const w=src?(src.videoWidth||src.width):(name==='texSubImage2D'?args[4]:args[3]);
    const h=src?(src.videoHeight||src.height):(name==='texSubImage2D'?args[5]:args[4]);
    const start=performance.now();try{return fn.apply(this,args);}finally{if(Number(w)*Number(h)>0){
     p.uploads++;p.bytes+=w*h*4;p.uploadMs+=performance.now()-start;const key=w+'x'+h;p.kinds?.push(key);p.bySize[key]=(p.bySize[key]??0)+1;
    }};
   };
  }
 }
})();`;
const stats = values => {
  const a = [...values].sort((x,y)=>x-y), q = n => a[Math.min(a.length-1,Math.floor(n*a.length))] ?? 0;
  return {n:a.length,p50:q(.5),p95:q(.95),p99:q(.99),max:a.at(-1)??0,mean:a.reduce((x,y)=>x+y,0)/(a.length||1)};
};
let browser;
try {
  browser=await chromium.launch({channel:"chrome",headless:true,args:gpu==="swiftshader"
    ? ["--use-gl=angle","--use-angle=swiftshader","--enable-unsafe-swiftshader"]
    : ["--use-angle=metal","--enable-gpu","--ignore-gpu-blocklist","--autoplay-policy=no-user-gesture-required"]});
  const page=await browser.newPage({viewport:{width:1280,height:720}}), errors=[];
  page.on("pageerror",e=>errors.push(e.message));await page.addInitScript(probe);
  const cdp=await page.context().newCDPSession(page);await cdp.send("Performance.enable");
  if(throttle>1)await cdp.send("Emulation.setCPUThrottlingRate",{rate:throttle});
  await page.goto(origin);await page.waitForSelector("#rover-message");
  const startup=await page.evaluate(()=>({navigation:performance.getEntriesByType('navigation')[0].toJSON(),firstCallbacks:window.__perf.frames.slice(0,12),firstGaps:window.__perf.gaps.slice(0,12)}));
  await page.evaluate(()=>{const box=[...document.querySelectorAll('#settings label.check input')].find(i=>i.parentElement.textContent.includes('H.264'));box.click();});
  await page.waitForTimeout(3500);
  const metric=async()=>Object.fromEntries((await cdp.send('Performance.getMetrics')).metrics.map(m=>[m.name,m.value]));
  const measurements=[];
  const measure=async label=>{
    await page.evaluate(()=>Object.assign(window.__perf,{frames:[],gaps:[],lastFrame:undefined,uploads:0,bytes:0,uploadMs:0,draws:0,bySize:{},byKind:{},gpu:[]}));
    const before=await metric();await cdp.send('Profiler.enable');await cdp.send('Profiler.setSamplingInterval',{interval:500});await cdp.send('Profiler.start');
    const start=Date.now();while(Date.now()-start<seconds*1000){
      await page.evaluate(t=>{const d=window.__device,a=Math.sin(t/900)*.35;d.quaternion.set(0,Math.sin(a/2),0,Math.cos(a/2));},Date.now()-start);
      await new Promise(r=>setTimeout(r,50));
    }
    const {profile}=await cdp.send('Profiler.stop'),after=await metric();
    const raw=await page.evaluate(()=>{
      const p=window.__perf,gl=p.gl,e=gl.getExtension('WEBGL_debug_renderer_info');
      return {frames:p.frames,gaps:p.gaps,uploads:p.uploads,bytes:p.bytes,uploadMs:p.uploadMs,draws:p.draws,bySize:p.bySize,byKind:p.byKind,gpu:p.gpu,gpuStatus:p.gpuStatus,renderer:e?gl.getParameter(e.UNMASKED_RENDERER_WEBGL):'unknown',drawingBuffer:[gl.drawingBufferWidth,gl.drawingBufferHeight],details:document.querySelector('#rover-details').textContent,message:document.querySelector('#rover-message').textContent};
    });
    const nodes=new Map(profile.nodes.map(n=>[n.id,n])),parents=new Map(),self=new Map(),inclusive=new Map();
    profile.nodes.forEach(n=>n.children?.forEach(id=>parents.set(id,n.id)));
    const stages=/^(update|render|drawCamera|drawHUD|drawSource|drawStop|drawVoice|drawDetections|uploadTexture|texSubImage2D|texImage2D|drawImage|fillText|measureText|pump|send|acceptStatus|updateFeeds|syncMapConnection)$/;
    profile.samples.forEach((id,i)=>{
      const weight=(profile.timeDeltas[i]??0)/1000,c=nodes.get(id).callFrame,key=`${c.functionName||'(anon)'} ${c.url.split('/').pop()}:${c.lineNumber+1}`;
      self.set(key,(self.get(key)??0)+weight);const seen=new Set();
      for(let n=id;n!==undefined;n=parents.get(n)){const name=nodes.get(n).callFrame.functionName;if(stages.test(name)&&!seen.has(name)){seen.add(name);inclusive.set(name,(inclusive.get(name)??0)+weight);}}
    });
    const elapsed=after.Timestamp-before.Timestamp;
    const result={label,elapsed,callback:stats(raw.frames),gap:stats(raw.gaps),gpu:stats(raw.gpu),gpuStatus:gpuTimers?raw.gpuStatus:'disabled',renderer:raw.renderer,drawingBuffer:raw.drawingBuffer,callbacksPerSecond:raw.frames.length/elapsed,drawsPerCallback:raw.draws/(raw.frames.length||1),uploadsPerSecond:raw.uploads/elapsed,estimatedRGBAUploadMBPerSecond:raw.bytes/elapsed/1e6,synchronousUploadMsPerSecond:raw.uploadMs/elapsed,uploadSizesPerSecond:Object.fromEntries(Object.entries(raw.bySize).map(([k,v])=>[k,v/elapsed])),callbackByUpload:Object.fromEntries(Object.entries(raw.byKind).map(([k,v])=>[k,stats(v)])),busy:Object.fromEntries(['TaskDuration','ScriptDuration','LayoutDuration','RecalcStyleDuration'].map(k=>[k,(after[k]-before[k])/elapsed*100])),heapMB:after.JSHeapUsedSize/1e6,inclusiveMsPerSecond:Object.fromEntries([...inclusive].sort((a,b)=>b[1]-a[1]).map(([k,v])=>[k,v/elapsed])),topSelfMsPerSecond:[...self].sort((a,b)=>b[1]-a[1]).filter(([k])=>!k.startsWith('(idle)')).slice(0,25).map(([k,v])=>[k,v/elapsed]),message:raw.message,details:raw.details};
    measurements.push(result);console.log(JSON.stringify(result,null,2));
  };
  await measure('desktop-camera');
  await page.evaluate(()=>{Object.assign(window.__perf,{phase:'xr',frames:[],gaps:[],lastFrame:undefined});document.querySelector('#xr-slot button').click();});await page.waitForTimeout(1500);
  const xrStartup=await page.evaluate(()=>({frames:window.__perf.frames.slice(-90),gaps:window.__perf.gaps.slice(-90)}));
  await page.evaluate(async()=>{const d=window.__device;for(const hand of ['left','right'])Object.defineProperty(Object.getPrototypeOf(d.controllers[hand].inputSource.gamepad),'connected',{configurable:true,get:()=>true});d.controllers.right.updateButtonValue('trigger',0);});
  await page.waitForTimeout(600);await measure('IWER-Quest2-camera');
  await page.screenshot({path:output.replace(/\.json$/, '-camera.png')});
  await page.evaluate(()=>window.__device.controllers.right.updateButtonValue('a-button',1));await page.waitForTimeout(200);
  await page.evaluate(()=>window.__device.controllers.right.updateButtonValue('a-button',0));await page.waitForTimeout(1200);
  await measure('IWER-Quest2-map');await page.screenshot({path:output.replace(/\.json$/, '-map.png')});
  await measure('IWER-Quest2-map-warmed');
  if (process.env.PERF_RETURN) {
    await page.evaluate(()=>window.__device.controllers.right.updateButtonValue('a-button',1));await page.waitForTimeout(200);
    await page.evaluate(()=>window.__device.controllers.right.updateButtonValue('a-button',0));await page.waitForTimeout(2500);
    await measure('IWER-Quest2-live-return');
  }
  writeFileSync(output,JSON.stringify({scope:'Canonical SIM backend/relay; IWER Quest2; headless desktop GPU, no physical Quest; profiler instrumentation enabled; RGBA upload bytes estimated; callbacks are not compositor presentation',origin,throttle,gpu,gpuTimers,startup,xrStartup,measurements,errors},null,2));
  console.log(`Saved ${output}`);
} finally { await browser?.close(); }
