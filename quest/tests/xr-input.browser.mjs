import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { chromium } from '@playwright/test';
// Production controller/HUD software fixture; no headset, motors or real network.
const origin = 'http://127.0.0.1:5258';
const server = spawn(process.execPath, ['--input-type=module', '-e',
  "import {createServer} from 'vite';const s=await createServer({server:{host:'127.0.0.1',port:5258,strictPort:true,watch:null}});await s.listen();"],
  {cwd:new URL('..',import.meta.url),stdio:'pipe'});
let log='', browser;
server.stdout.on('data',v=>log+=v); server.stderr.on('data',v=>log+=v);
try {
  let ready=false;
  for(let i=0;i<100;i++){if(server.exitCode!==null)throw Error(log);try{ready=(await fetch(origin)).ok}catch{}if(ready)break;await new Promise(r=>setTimeout(r,100));}
  assert.ok(ready,log);
  browser=await chromium.launch({args:['--no-sandbox','--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader']});
  const page=await browser.newPage({viewport:{width:960,height:640}});
  await page.routeWebSocket('**/ws/**',socket=>socket.close());
  await page.route('**/camera.jpg',r=>r.fulfill({status:503,body:''}));
  await page.route('**/health',r=>r.fulfill({contentType:'application/json',body:JSON.stringify({rover:true,camera:true,camera_source:'SIMULATED',control:{simulated:true}})}));
  await page.goto(`${origin}/?demo=0`);
  const result=await page.evaluate(async()=>{
    const {RoverExperience}=await import('/src/rover.ts');
    const THREE=await import('/node_modules/three/build/three.module.js');
    const {mapWalk}=await import('/src/map-walk.js');
    const renderer=new THREE.WebGLRenderer({preserveDrawingBuffer:true});renderer.setSize(960,640);
    const scene=new THREE.Scene();scene.background=new THREE.Color('#07111a');
    const camera=new THREE.PerspectiveCamera(70,1.5,.1,100);
    const subject=new RoverExperience(renderer,scene,()=>{},()=> 'camera');
    subject.pollCamera=()=>{};subject.pollHealth=()=>{};subject.h264Enabled=false;
    const right={handedness:'right',gripSpace:{},gamepad:{connected:true,axes:[0,0,0,0],buttons:Array.from({length:6},()=>({pressed:false,value:0}))}};
    const left={handedness:'left',gripSpace:{},gamepad:{connected:true,axes:[0,0,0,0],buttons:Array.from({length:6},()=>({pressed:false,value:0}))}};
    const session={visibilityState:'visible',inputSources:[right,left]},reference={};
    renderer.xr.isPresenting=true;renderer.xr.getSession=()=>session;renderer.xr.getReferenceSpace=()=>reference;renderer.xr.getCamera=()=>camera;
    let tracked=true;
    const frame={getViewerPose:()=>({transform:{orientation:{x:0,y:0,z:0,w:1},position:{x:0,y:0,z:0}}}),getPose:()=>tracked?{}:null};
    let now=10000,messages=[];const realNow=performance.now.bind(performance);performance.now=()=>now;
    const socket={readyState:WebSocket.OPEN,bufferedAmount:0,send:raw=>messages.push(JSON.parse(raw)),close(){this.readyState=WebSocket.CLOSED;}};
    subject.socket=socket;
    const imageCanvas=document.createElement('canvas');imageCanvas.width=640;imageCanvas.height=480;
    const c=imageCanvas.getContext('2d');c.fillStyle='#16475c';c.fillRect(0,0,640,480);c.fillStyle='#327c87';c.fillRect(180,70,280,330);
    const image=new Image();image.src=imageCanvas.toDataURL();await image.decode();subject.frame=image;subject.frameSource='SIMULATED';
    const tick=(dt=60,fresh=true)=>{
      now+=dt;
      if(fresh){subject.status={session_id:'fixture',permit:`p${now}`,permit_ms:250,rover:true,simulated:true,requires_release:false,seq:messages.at(-1)?.seq??-1};subject.gate.acceptStatus(subject.status,now);subject.gate.acceptFrame(`f${now}`,0,now);}
      subject.lastCanvasDraw=now;subject.dirty=true;subject.update(now,frame);renderer.render(scene,camera);subject.afterRender(now);
      return {...messages.at(-1)};
    };
    try {
      tick();tick();
      const initial={auto:subject.freeRoam.active,mode:subject.autonomy.mode};
      right.gamepad.axes[3]=-1;right.gamepad.axes[2]=.625;
      const manual=tick();
      right.gamepad.axes[2]=right.gamepad.axes[3]=0;
      const centered=tick();
      right.gamepad.buttons[4].pressed=true;tick();tick();const auto=tick();
      const autoOnPress=subject.freeRoam.active;
      // Holding A for >600ms must never open an actions menu or toggle twice.
      for(let i=0;i<25;i++)tick();
      const heldA={auto:subject.freeRoam.active,menu:subject.actionMenu.visible};
      right.gamepad.buttons[4].pressed=false;tick();
      const before={x:mapWalk.x,z:mapWalk.z,yaw:mapWalk.yaw};
      left.gamepad.axes[2]=1;left.gamepad.axes[3]=-1;right.gamepad.axes[2]=1;
      for(let i=0;i<5;i++)tick();
      const roaming={before,after:{x:mapWalk.x,z:mapWalk.z,yaw:mapWalk.yaw},commands:messages.slice(-5)};
      left.gamepad.axes[2]=left.gamepad.axes[3]=0;right.gamepad.axes[2]=0;right.gamepad.axes[3]=-1;
      right.gamepad.buttons[4].pressed=true;const transition=tick();
      const heldAfterSwitch=tick();
      right.gamepad.buttons[4].pressed=false;right.gamepad.axes[3]=0;tick();
      right.gamepad.axes[3]=-1;const rearmed=tick();
      // A lost tracked controller must never spend its previous joystick command.
      tracked=false;const lost=tick();tracked=true;right.gamepad.axes[3]=0;tick();right.gamepad.axes[3]=-1;tick();
      const stale=tick(400,false);
      right.gamepad.axes[3]=0;tick();right.gamepad.axes[3]=-1;tick();
      right.gamepad.buttons[5].pressed=true;const stopped=tick();
      const panels={children:subject.cockpit.children.length,menu:subject.actionMenu.visible,hint:subject.hintPanel.visible,stop:subject.stopPanel.visible,voice:subject.voicePanel.visible,drill:subject.drillPanel.visible,identity:subject.identityPanel.visible,pointer:subject.pointer.visible};
      right.gamepad.buttons[5].pressed=false;right.gamepad.axes[3]=0;tick();
      subject.drawHUD(now);if(subject.bannerPending)subject.drawBanner();renderer.render(scene,camera);
      const pixels=new Uint8Array(4),gl=renderer.getContext();gl.readPixels(480,320,1,1,gl.RGBA,gl.UNSIGNED_BYTE,pixels);
      const picture=renderer.domElement.toDataURL();
      subject.dispose();renderer.dispose();
      return {initial,manual,centered,auto,autoOnPress,heldA,roaming,transition,heldAfterSwitch,rearmed,lost,stale,stopped,panels,pixels:[...pixels],picture};
    }finally{performance.now=realNow;}
  });
  assert.deepEqual(result.initial,{auto:false,mode:'teleop'});
  assert.equal(result.manual.deadman,true,'joystick drives with trigger released');
  // Full forward plus .625 stick steering (32 degrees, past the straight-ahead lock) becomes
  // v=1 and w=.5^2.5: half-way past the .25 deadzone, through the steering expo curve.
  // Preserve forward authority instead of restoring the old diagonal-to-pivot mapping.
  // Forward eases in from rest (manual-drive.js ForwardRamp): the first sampled push sends no forward
  // step; steering is not eased and keeps its curve.
  assert.equal(result.manual.v,0,'forward eases in from rest instead of stepping to full');
  assert.ok(Math.abs(result.manual.w-.5**2.5)<1e-9,'steering uses its dead-zoned, expo-curved demand');
  assert.equal(result.centered.deadman,false);assert.equal(result.centered.v,0);assert.equal(result.centered.w,0);
  assert.equal(result.autoOnPress,true);assert.equal(result.auto.deadman,true);assert.equal(result.auto.mode,'explore');assert.equal(result.auto.v,0);assert.equal(result.auto.w,0);
  assert.deepEqual(result.heldA,{auto:true,menu:false});
  assert.ok(Math.hypot(result.roaming.after.x-result.roaming.before.x,result.roaming.after.z-result.roaming.before.z)>0);
  assert.notEqual(result.roaming.after.yaw,result.roaming.before.yaw);
  assert.ok(result.roaming.commands.every(m=>m.mode==='explore'&&m.v===0&&m.w===0));
  assert.equal(result.transition.deadman,false);assert.equal(result.heldAfterSwitch.deadman,false);assert.equal(result.rearmed.deadman,true);
  assert.equal(result.lost.deadman,false);assert.equal(result.stale.deadman,false);assert.equal(result.stopped.deadman,false);
  assert.deepEqual(result.panels,{children:2,menu:false,hint:false,stop:false,voice:false,drill:false,identity:false,pointer:false});
  assert.ok(result.pixels.slice(0,3).some(v=>v>25),'actual camera pixels remain visible');
  const {writeFile}=await import('node:fs/promises');
  await writeFile('/tmp/farsight-minimal-vr.png',Buffer.from(result.picture.split(',')[1],'base64'));
  delete result.picture;await writeFile('/tmp/farsight-minimal-vr.json',JSON.stringify(result,null,2));
  console.log('PASS production minimal VR modes, joystick routing, tracking/expiry, actual pixels; SOFTWARE ONLY');
}finally{
  await browser?.close();server.kill('SIGTERM');await new Promise(r=>server.once('exit',r));
}
