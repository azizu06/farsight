// Software-only visual audit: actual production scene with an IWER Quest 2 session.
// node quest/tests/vr-visual-audit.mjs (writes /tmp/farsight-vr-audit-*.png/json)
import { spawn } from 'node:child_process';
import { existsSync, readFileSync, writeFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import net from 'node:net';
import { chromium } from '@playwright/test';
const root = fileURLToPath(new URL('../..', import.meta.url));
const python = existsSync(`${root}.venv/bin/python`) ? `${root}.venv/bin/python` : 'python3';
const iwer = readFileSync(createRequire(import.meta.url).resolve('iwer/build/iwer.min.js'), 'utf8');
const children = [], findings = [];
const after = process.argv.includes('--after');
const prefix = process.argv.includes('--polish') ? '/tmp/farsight-vr-final' : after ? '/tmp/farsight-vr-after' : '/tmp/farsight-vr-audit';
let browser;
const pause = ms => new Promise(r => setTimeout(r, ms));
async function port() { return new Promise(resolve => { const server = net.createServer(); server.listen(0,'127.0.0.1',()=>{const p=server.address().port; server.close(()=>resolve(p));}); }); }
function start(args) { const child=spawn(python,args,{cwd:root,detached:true,stdio:['ignore','pipe','pipe']});child.stdout.on('data',()=>{});child.stderr.on('data',()=>{});children.push(child);return child; }
async function ready(url,child) {const end=Date.now()+15000;while(Date.now()<end){if(child.exitCode!==null)throw Error('Owned service exited');if(await fetch(url,{signal:AbortSignal.timeout(500)}).then(r=>r.ok,()=>false))return;await pause(50);}throw Error('Readiness deadline');}
try {
 const backendPort=await port(), relayPort=await port();
 const backend=start(['-m','jetson.server','--demo','--simulate-rover','--detector','none','--host','127.0.0.1','--port',String(backendPort)]);
 await ready(`http://127.0.0.1:${backendPort}/health`,backend);
 const relay=start(['-m','tools.operator_relay','--upstream',`http://127.0.0.1:${backendPort}`,'--port',String(relayPort)]);
 const origin=`http://127.0.0.1:${relayPort}`;await ready(`${origin}/relay/health`,relay);
 browser=await chromium.launch({args:['--no-sandbox','--disable-dev-shm-usage','--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader']});
 const page=await browser.newPage({viewport:{width:1920,height:1080}});
 await page.addInitScript(`${iwer}\nwindow.__device = new IWER.XRDevice(IWER.metaQuest2, {stereoEnabled:true, visibilityMaskEnabled:true}); window.__device.installRuntime({forceInstall:true});\n`);
 await page.addInitScript(() => {
  const rows = new WeakMap(); window.__menuAudit = { paints: 0, hoveredRow: null };
  const roundRect = CanvasRenderingContext2D.prototype.roundRect;
  CanvasRenderingContext2D.prototype.roundRect = function(...args) {
   if (this.canvas.width === 768 && this.canvas.height === 900) rows.set(this, args[1]);
   return roundRect.apply(this, args);
  };
  const stroke = CanvasRenderingContext2D.prototype.stroke;
  CanvasRenderingContext2D.prototype.stroke = function(...args) {
   if (this.canvas.width === 768 && this.canvas.height === 900 && this.strokeStyle === '#70f2ff') {
    window.__menuAudit.paints++; window.__menuAudit.hoveredRow = rows.get(this);
   }
   return stroke.apply(this, args);
  };
 });
 await page.goto(origin+'/?h264=0');await page.locator('#rover-message').waitFor();await pause(2200);
 await page.screenshot({path:`${prefix}-desktop.png`});
 await page.evaluate(()=>document.querySelector('#xr-slot button').click());await pause(1200);
 await page.evaluate(()=>{const d=window.__device;for(const hand of ['left','right'])Object.defineProperty(Object.getPrototypeOf(d.controllers[hand].inputSource.gamepad),'connected',{configurable:true,get:()=>true});d.controllers.right.updateButtonValue('trigger',0);});
 const save = async name => {
  await pause(600);await page.screenshot({path:`${prefix}-${name}.png`});
  findings.push(await page.evaluate(name=>({name,source:document.querySelector('.source-banner')?.dataset.kind,
   headline:document.querySelector('#rover-message')?.textContent,details:document.querySelector('#rover-details')?.textContent,
   xrButton:document.querySelector('#xr-slot button')?.textContent,
   runtime:!!document.querySelector('[data-webxr_runtime]'),
   viewport:{width:innerWidth,height:innerHeight}, stereo:window.__device.stereoEnabled, menuHover:window.__menuAudit,
   headset:{x:window.__device.position.x,y:window.__device.position.y,z:window.__device.position.z},
  }),name));
 };
 await save('camera-forward-stereo');
 await page.evaluate(()=>{const a=Math.PI/6;window.__device.quaternion.set(0,Math.sin(a/2),0,Math.cos(a/2));});
 await save('camera-yaw30-stereo');
 await page.evaluate(()=>{const a=-Math.PI/7;window.__device.quaternion.set(Math.sin(a/2),0,0,Math.cos(a/2));});
 await save('camera-lookdown-stereo');
 if (after) {
  await page.evaluate(()=>{const d=window.__device;d.quaternion.set(0,0,0,1);d.controllers.left.updateButtonValue('y-button',1);});
  await pause(120);await page.evaluate(()=>window.__device.controllers.left.updateButtonValue('y-button',0));
  await page.evaluate(()=>{const c=window.__device.controllers.right; c.position.set(0,1.4,-.3);const angle=Math.atan2(.551,1.9);c.quaternion.set(Math.sin(angle/2),0,0,Math.cos(angle/2));});
  await save('menu-hover-stereo');
  await page.evaluate(()=>window.__device.controllers.left.updateButtonValue('y-button',1));
  await pause(120);await page.evaluate(()=>window.__device.controllers.left.updateButtonValue('y-button',0));
 }
 await page.evaluate(()=>{window.__device.quaternion.set(0,0,0,1);window.__device.controllers.right.updateButtonValue('a-button',1);});
 await pause(80);await page.evaluate(()=>window.__device.controllers.right.updateButtonValue('a-button',0));
 await pause(1600);await save('map-forward-stereo');
 await page.evaluate(()=>{const a=Math.PI/6;window.__device.quaternion.set(0,Math.sin(a/2),0,Math.cos(a/2));});
 await save('map-yaw30-stereo');
 console.log(JSON.stringify(findings,null,2));
} finally {
 await browser?.close();
 for(const child of children)if(child.exitCode===null&&child.signalCode===null)try{process.kill(-child.pid,'SIGTERM');}catch{}
 await pause(700);
 for(const child of children)if(child.exitCode===null&&child.signalCode===null)try{process.kill(-child.pid,'SIGKILL');}catch{}
 writeFileSync(`${prefix}.json`,JSON.stringify({scope:'Actual IWER Quest 2 stereo session in Chromium/SwiftShader; isolated SIMULATED source, never physical Quest evidence.',findings},null,2));
}
