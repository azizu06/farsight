import * as THREE from 'three';
import { coverageMetrics } from './metrics.mjs';
import { RawView } from './raw-view.js';
import { OrbitControls } from '../../quest/node_modules/three/examples/jsm/controls/OrbitControls.js';
import { DenseMapView } from '../../quest/src/dense-map.ts';

// Keep the canonical Quest draw/upload/reset path, but discard its Quest telemetry
// locally: a diagnostic viewer must never overwrite headset per-hop metrics.
const networkFetch = window.fetch.bind(window);
const cache = new Map();
let frozen = false, capturedAt = null, captureIdentity = null, capturedDataAge = null, manifestEpoch = null, reframe = false;
window.fetch = async (input, init) => {
  const url = new URL(typeof input === 'string' ? input : input.url, location.href);
  if (url.pathname === '/dense/metrics') return new Response('{}', {status:200});
  const dense = ['/dense/manifest','/dense/chunk.bin'].includes(url.pathname);
  // Manifest position affects LOD. A frozen comparison uses exactly the same
  // captured chunks/LOD at all three fixed poses, and is labelled accordingly.
  const key = url.pathname === '/dense/manifest' ? url.pathname : url.pathname + url.search;
  if (dense && frozen) {
    const item = cache.get(key);
    if (!item) return new Response('Not in captured view', {status:404});
    return new Response(item.body.slice(0), {headers:item.headers});
  }
  const response = await networkFetch(input, init);
  if (dense && response.ok) {
    if(url.pathname==='/dense/manifest'){const m=await response.clone().json();captureIdentity=m.source;if(manifestEpoch!==m.epoch){manifestEpoch=m.epoch;reframe=true;}}
    const copy = response.clone();
    cache.delete(key);cache.set(key, {body:await copy.arrayBuffer(), headers: [...copy.headers]});
    if(cache.size>160)cache.delete(cache.keys().next().value);
  }
  return response;
};
const host = document.querySelector('#viewport');
const renderer = new THREE.WebGLRenderer({antialias:true,preserveDrawingBuffer:true});
renderer.setPixelRatio(1); renderer.setClearColor(0x080c12); renderer.outputColorSpace = THREE.SRGBColorSpace;
host.append(renderer.domElement);
const scene = new THREE.Scene(), root = new THREE.Group(); scene.add(root);
const camera = new THREE.PerspectiveCamera(60,16/9,.01,100);
camera.position.set(0,1.3,3);
const orbit = new OrbitControls(camera,renderer.domElement); orbit.target.set(0,.3,0);
let bounds = new THREE.Box3(), framed = false, fps = 0, frameCount = 0, sampledAt = performance.now();
let size = .012, mode = 'local', status = {}, statusTime = 0, statusError = null;
let follow = true;
function acceptBounds(value) {
  bounds.copy(value);
  if (bounds.isEmpty()) return;
  if (!framed || reframe) {framed=true;reframe=false;pose(0);return;}
  // Rolling scans and capture resets can move all geometry outside the first
  // camera's view. Follow the measured bounds without changing orbit or zoom.
  if (follow && !frozen) {
    const delta=bounds.getCenter(new THREE.Vector3()).sub(orbit.target);
    if(delta.length()>.25){camera.position.add(delta);orbit.target.add(delta);orbit.update();}
  }
}
const dense = new DenseMapView(root,renderer,acceptBounds);
const raw=new RawView(root,acceptBounds);raw.group.visible=false;
function setFollow(value){follow=Boolean(value);document.querySelector('#follow').checked=follow;if(follow&&!bounds.isEmpty())acceptBounds(bounds);}
orbit.addEventListener('start',()=>setFollow(false));
document.querySelector('#follow').onchange=e=>setFollow(e.target.checked);
const canonicalCompile = dense.material.onBeforeCompile.bind(dense.material);
const splat = {value:size};
dense.material.onBeforeCompile = (shader, renderer) => {
  canonicalCompile(shader,renderer);
  shader.uniforms.labSplatDiameter = splat;
  shader.vertexShader = 'uniform float labSplatDiameter;\n' + shader.vertexShader.replace('0.012 * length(modelMatrix', 'labSplatDiameter * length(modelMatrix').replace('1.25, 16.0','1.25, 64.0');
};
dense.material.customProgramCacheKey = () => 'dense-lab-splat-v1';
function pose(index) {
  if (bounds.isEmpty()) return;
  const center = bounds.getCenter(new THREE.Vector3());
  const radius = Math.max(.4,bounds.getSize(new THREE.Vector3()).length()/2);
  const directions = [new THREE.Vector3(.15,.25,1),new THREE.Vector3(1,.2,.15),new THREE.Vector3(.1,1,.15)];
  const distance = radius / Math.sin(THREE.MathUtils.degToRad(camera.fov/2))*1.12;
  camera.position.copy(center).addScaledVector(directions[index].normalize(),distance);
  camera.up.set(0,1,0); orbit.target.copy(center); camera.lookAt(center); orbit.update();
}
function setSize(value) {
  size=Math.min(.1,Math.max(.012,Number(value))); splat.value=size;raw.points.material.size=size;
  document.querySelector('#size').value=String(size);
  document.querySelector('#size-value').textContent=`${Math.round(size*1000)} mm`;
}
async function setFrozen(value) {
  if(value){
    const previous=frozen;frozen=true; // Stop new pulls before draining the current one.
    const lane=mode==='raw'?raw:dense;const deadline=performance.now()+6000;
    while(lane.busy&&performance.now()<deadline)await new Promise(r=>setTimeout(r,25));
    if(lane.busy){frozen=previous;throw Error('Capture pull did not finish before freeze');}
  }
  frozen=Boolean(value); capturedAt=frozen ? Date.now() : null;
  capturedDataAge=frozen?(mode==='raw'?raw.stats().raw_age_ms:(status.age_s??0)*1000+Date.now()-statusTime):null;
  document.querySelector('#freeze').textContent=frozen?'Resume live data':'Freeze measured sample';
}
function setMode(value) {
  const next=value==='raw'?'raw':'local';
  if(next!==mode){framed=false;bounds.makeEmpty();}
  mode=next;document.querySelector('#mode').value=mode;
}
const keys=new Set();
addEventListener('keydown',e=>{if(!['INPUT','SELECT'].includes(e.target.tagName))keys.add(e.code);});
addEventListener('keyup',e=>keys.delete(e.code)); addEventListener('blur',()=>keys.clear());
function setLayers(value){raw.setLayers(value);document.querySelector('#surfaces').checked=raw.showSurfaces;document.querySelector('#points').checked=raw.showPoints;}
document.querySelector('#surfaces').onchange=e=>setLayers({surfaces:e.target.checked});
document.querySelector('#points').onchange=e=>setLayers({points:e.target.checked});
document.querySelector('#size').oninput=e=>setSize(e.target.value);
document.querySelector('#freeze').onclick=()=>setFrozen(!frozen);
document.querySelector('#frame').onclick=()=>pose(0);
document.querySelector('#mode').onchange=e=>setMode(e.target.value);
document.querySelectorAll('[data-pose]').forEach(b=>b.onclick=()=>pose(Number(b.dataset.pose)));
async function pollStatus() {
  try {const r=await networkFetch('/dense/status');if(!r.ok)throw Error(`Status ${r.status}`);status=await r.json();statusTime=Date.now();statusError=null;}
  catch(e){statusError=String(e);}
}
void pollStatus();const poll=setInterval(pollStatus,1000);
const stats = () => ({mode,source:frozen?'CAPTURED MEASURED SAMPLE':'LIVE MEASURED RGB-D',frozen,follow,render_frames:totalFrames,captured_at:capturedAt,
  capture_identity:mode==='raw'?raw.identity:captureIdentity,uploaded_points:mode==='raw'?raw.count:dense.points,points_drawn:renderer.info.render.points,fps,...raw.stats(),
  jetson_points:status.jetson_points??null,mac_points:status.mac_points??null,
  data_age_ms:frozen?(capturedDataAge??0)+Date.now()-capturedAt:(mode==='raw'?raw.stats().raw_age_ms:typeof status.age_s==='number'?status.age_s*1000+Date.now()-statusTime:null),
  upstream_age_ms:typeof status.age_s==='number'?status.age_s*1000+Date.now()-statusTime:null,
  capture_age_ms:capturedAt===null?null:Date.now()-capturedAt,
  splat_m:size,error:statusError??dense.error,camera:camera.position.toArray(),target:orbit.target.toArray(),bounds:{min:bounds.min.toArray(),max:bounds.max.toArray()}});
let last = performance.now(), totalFrames = 0;
renderer.setAnimationLoop(now=>{
  const dt=Math.min(.05,(now-last)/1000);last=now;
  {
    const speed=(keys.has('ShiftLeft')?2:.6)*dt;
    const move=new THREE.Vector3(Number(keys.has('KeyD'))-Number(keys.has('KeyA')),Number(keys.has('KeyE'))-Number(keys.has('KeyQ')),Number(keys.has('KeyS'))-Number(keys.has('KeyW'))).applyQuaternion(camera.quaternion).multiplyScalar(speed);
    if(move.lengthSq())setFollow(false);camera.position.add(move);orbit.target.add(move);orbit.update();
    dense.update(now,camera,mode==='local');void raw.update(mode==='raw',frozen);renderer.render(scene,camera);
  }
  totalFrames++;frameCount++;if(now-sampledAt>=1000){fps=frameCount*1000/(now-sampledAt);frameCount=0;sampledAt=now;document.querySelector('#stats').textContent=JSON.stringify(stats(),null,2);document.querySelector('#source-overlay').textContent=`${mode==='raw'?(raw.source??'WAITING FOR RAW RGB-D'):'MEASURED RGB-D / QUEST DENSE PATH'} · ${frozen?'CAPTURED SAMPLE':'LIVE'} · ${mode==='raw'?raw.count:dense.points} measured points · ${Math.round(size*1000)}mm splats`; }
});
setMode(new URLSearchParams(location.search).get('mode'));
window.denseLab={stats,pose,setSize,setFrozen,setMode,setFollow,setLayers,canvas:renderer.domElement,render:()=>renderer.render(scene,camera),
  coverage:()=>{renderer.render(scene,camera);const gl=renderer.getContext(),p=new Uint8Array(gl.drawingBufferWidth*gl.drawingBufferHeight*4);gl.readPixels(0,0,gl.drawingBufferWidth,gl.drawingBufferHeight,gl.RGBA,gl.UNSIGNED_BYTE,p);return coverageMetrics({width:gl.drawingBufferWidth,height:gl.drawingBufferHeight,pixels:p});}};
addEventListener('resize',()=>{const w=host.clientWidth,h=host.clientHeight;renderer.setSize(w,h,false);camera.aspect=w/h;camera.updateProjectionMatrix();});
dispatchEvent(new Event('resize'));
addEventListener('pagehide',()=>{clearInterval(poll);dense.dispose();raw.dispose();renderer.dispose();});
