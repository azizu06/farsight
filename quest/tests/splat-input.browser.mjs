import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { chromium } from '@playwright/test';

// Production walkthrough with real Three/OrbitControls and synthetic controller/pose input.
// Software evidence only: no physical headset or rover.
const origin = 'http://127.0.0.1:5261';
const server = spawn(process.execPath, ['--input-type=module', '-e',
  "import {createServer} from 'vite';const s=await createServer({server:{host:'127.0.0.1',port:5261,strictPort:true,watch:null}});await s.listen();"],
  {cwd:new URL('..',import.meta.url),stdio:'pipe'});
let log='', browser;
server.stdout.on('data',v=>log+=v); server.stderr.on('data',v=>log+=v);
try {
  let ready=false;
  for(let i=0;i<100;i++){if(server.exitCode!==null)throw Error(log);try{ready=(await fetch(origin)).ok}catch{}if(ready)break;await new Promise(r=>setTimeout(r,100));}
  assert.ok(ready,log);
  browser=await chromium.launch({args:['--no-sandbox','--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader']});
  const page=await browser.newPage();
  await page.addInitScript(()=>{
    globalThis.flatPad={id:'test standard pad',mapping:'standard',connected:true,axes:[0,0,0,0],buttons:Array.from({length:4},()=>({pressed:false,value:0}))};
    Object.defineProperty(navigator,'getGamepads',{value:()=>[globalThis.flatPad]});
  });
  const splat=Buffer.alloc(32*4);
  [[-2,0,-4],[2,3,0],[-2,3,0],[2,0,-4]].forEach((p,i)=>{
    p.forEach((v,j)=>splat.writeFloatLE(v,i*32+j*4));
    for(let j=0;j<3;j++)splat.writeFloatLE(.01,i*32+12+j*4);
    splat.fill(255,i*32+24,i*32+28);splat.set([255,128,128,128],i*32+28);
  });
  await page.route(url=>url.pathname==='/fixtures/room.splat',r=>r.fulfill({body:splat}));
  await page.route(url=>url.pathname==='/fixtures/capture.json',r=>r.fulfill({json:{frames:[
    {transform_matrix:[[1,0,0,2],[0,1,0,1.2],[0,0,1,-3],[0,0,0,1]]},
    {transform_matrix:[[1,0,0,2.1],[0,1,0,1.6],[0,0,1,-3],[0,0,0,1]]},
  ]}}));
  await page.goto(`${origin}/splat.html?scene=/fixtures/room.splat&transforms=/fixtures/capture.json`);
  await page.waitForFunction(()=>globalThis.__splatStats?.splats===4);
  assert.equal(await page.evaluate(()=>globalThis.__splatStats.start.source),'capture');
  assert.deepEqual(await page.evaluate(()=>globalThis.__splatStats.start.eye),[2,1.4,-3]);
  await page.evaluate(()=>globalThis.flatPad.axes[1]=-1);
  await page.waitForFunction(()=>globalThis.__splatStats.eye[2]<-3.2);
  await page.evaluate(()=>{globalThis.flatPad.axes[1]=0;globalThis.flatPad.buttons[0].pressed=true;});
  await page.waitForFunction(()=>Math.abs(globalThis.__splatStats.eye[2]+3)<1e-6);
  await page.evaluate(()=>globalThis.flatPad.buttons[0].pressed=false);
  const result=await page.evaluate(async()=>{
    const THREE=await import('/node_modules/three/build/three.module.js');
    const {OrbitControls}=await import('/node_modules/three/examples/jsm/controls/OrbitControls.js');
    const {SplatNavigation}=await import('/src/splat-navigation.ts');
    const renderer=new THREE.WebGLRenderer();
    const rig=new THREE.Group(), camera=new THREE.PerspectiveCamera();rig.add(camera);
    const controls=new OrbitControls(camera,renderer.domElement);controls.enableDamping=true;
    const nav=new SplatNavigation(renderer,rig,camera,controls);
    nav.setStart({eye:[2,1.4,-3],yaw:0});nav.reset();
    const left={id:'Oculus Touch (Left)',mapping:'xr-standard',connected:true,axes:[0,0,0,0],buttons:Array.from({length:6},()=>({pressed:false,value:0}))};
    const right={...left,id:'Oculus Touch (Right)',axes:[0,0,0,0],buttons:Array.from({length:6},()=>({pressed:false,value:0}))};
    const position=()=>{rig.updateMatrixWorld(true);return camera.getWorldPosition(new THREE.Vector3()).toArray();};
    const initial=position();
    left.axes[3]=-1;for(let i=0;i<10;i++){nav.update(.1,undefined,[left,right]);controls.update();}
    const flatWalk=position();left.axes[3]=0;
    right.axes[2]=1;nav.update(.1,undefined,[left,right]);const flatYaw=camera.rotation.y,flatPivot=position();
    for(let i=0;i<10;i++)nav.update(.1,undefined,[left,right]);const heldYaw=camera.rotation.y;
    right.axes[2]=0;right.buttons[4].pressed=true;nav.update(.1,undefined,[left,right]);const reset=position();
    right.buttons[4].pressed=false;nav.update(.1,undefined,[left,right]);
    const session={inputSources:[{handedness:'left',gamepad:left},{handedness:'right',gamepad:right}]};
    renderer.xr.isPresenting=true;renderer.xr.getSession=()=>session;renderer.xr.getReferenceSpace=()=>({});
    // Room-scale offset and seated/standing height are anchored at entry; camera deliberately stale.
    const pose={position:{x:.3,y:1.8,z:.2},orientation:{x:0,y:0,z:0,w:1}};
    const frame={getViewerPose:()=>({transform:pose})};
    nav.reset();nav.update(.1,frame,[]);const xrStart=rig.position.toArray();
    // Turn the physical head to +X after entry: movement must use this frame, not the camera.
    pose.orientation={x:0,y:-Math.SQRT1_2,z:0,w:Math.SQRT1_2};
    left.axes[3]=-1;for(let i=0;i<10;i++)nav.update(.1,frame,[]);
    const xrWalk=rig.position.toArray();left.axes[3]=0;
    const head=()=>rig.localToWorld(new THREE.Vector3(pose.position.x,pose.position.y,pose.position.z)).toArray();
    const beforeTurn=head();right.axes[2]=1;nav.update(.1,frame,[]);const afterTurn=head();const xrYaw=rig.rotation.y;
    right.axes[2]=0;nav.update(.1,frame,[]);left.buttons[4].pressed=true;nav.update(.1,frame,[]);
    const xrReset=head(),resetYaw=rig.rotation.y-Math.PI/2;
    session.inputSources=[{handedness:'right',gamepad:right}];nav.update(0,frame,[]);
    const rightOnlyStart=head();right.axes[3]=-1;
    for(let i=0;i<10;i++)nav.update(.1,frame,[]);
    const rightOnlyWalk=head(),beforeFallbackTurn=rig.rotation.y;
    right.axes[3]=0;right.axes[2]=1;nav.update(.1,frame,[]);
    const rightOnlyTurn=rig.rotation.y-beforeFallbackTurn;
    right.buttons[4].pressed=true;nav.update(.1,frame,[]);const rightOnlyReset=head();
    renderer.xr.isPresenting=false;nav.reset();const exit=position();
    controls.dispose();renderer.dispose();
    return {initial,flatWalk,flatYaw,flatPivot,heldYaw,reset,xrStart,xrWalk,beforeTurn,afterTurn,xrYaw,xrReset,resetYaw,exit,
      rightOnlyStart,rightOnlyWalk,rightOnlyTurn,rightOnlyReset};
  });
  const close=(a,b)=>assert.ok(Math.abs(a-b)<1e-6,`${a} vs ${b}`);
  result.initial.forEach((v,i)=>close(v,[2,1.4,-3][i]));
  close(result.flatWalk[2],-4.5);close(result.flatWalk[0],2);
  close(result.flatYaw,-Math.PI/6);close(result.heldYaw,result.flatYaw);
  result.flatPivot.forEach((v,i)=>close(v,result.flatWalk[i]));
  result.reset.forEach((v,i)=>close(v,result.initial[i]));
  close(result.xrWalk[0]-result.xrStart[0],1.5);close(result.xrWalk[2],result.xrStart[2]);
  result.afterTurn.forEach((v,i)=>close(v,result.beforeTurn[i]));
  close(result.xrYaw,-Math.PI/6);
  result.xrReset.forEach((v,i)=>close(v,result.initial[i]));
  close(result.resetYaw,0);
  close(result.rightOnlyWalk[0],result.rightOnlyStart[0]);close(result.rightOnlyWalk[2]-result.rightOnlyStart[2],-1.5);
  close(result.rightOnlyTurn,-Math.PI/6);
  result.rightOnlyReset.forEach((v,i)=>close(v,result.initial[i]));
  result.exit.forEach((v,i)=>close(v,result.initial[i]));
  console.log('PASS splat flat/XR walk, fresh head yaw, 30-degree snap pivot, hold, A/X reset and session exit; SOFTWARE ONLY');
}finally{
  await browser?.close();server.kill('SIGTERM');await new Promise(r=>server.once('exit',r));
}
