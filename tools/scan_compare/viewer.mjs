import * as THREE from 'three';
import {OrbitControls} from '../../quest/node_modules/three/examples/jsm/controls/OrbitControls.js';
import {DenseCloudState,DenseCloudReceiver,DenseGpuChunks} from '../mac_render/dense-cloud.mjs';
import {RawView} from '../dense_viewer/raw-view.js';
import {prepareComparisonCapture,applyComparisonMode} from './capture.mjs';
const renderer=new THREE.WebGLRenderer({antialias:true,preserveDrawingBuffer:true});
renderer.setPixelRatio(1);renderer.setSize(innerWidth,innerHeight);renderer.outputColorSpace=THREE.SRGBColorSpace;
renderer.setClearColor(0x1b2229);document.querySelector('#stage').append(renderer.domElement);
const scene=new THREE.Scene(),root=new THREE.Group();scene.add(root);
const camera=new THREE.PerspectiveCamera(48,innerWidth/innerHeight,.01,200);
camera.position.set(7,4,7);camera.lookAt(0,.8,0);
const controls=new OrbitControls(camera,renderer.domElement);controls.target.set(0,.8,0);controls.enableDamping=false;
let dirty=true,mode='points',frozen=false,streaming=false,freezeAfterCapture=false,inputKind='none',requestedKind='none',dispatchInput=null,inFlightInput=null,shownError=null,recordingName='',recordedWall=null,accepted=0,framed=false,disposed=false;
const change=()=>{dirty=true;};controls.addEventListener('change',change);
const cloudMaterial=new THREE.PointsMaterial({size:.035,vertexColors:true,toneMapped:false,depthFunc:THREE.LessDepth});
cloudMaterial.onBeforeCompile=shader=>{
 shader.vertexShader=shader.vertexShader.replace('#include <logdepthbuf_vertex>','gl_PointSize=clamp(gl_PointSize*projectionMatrix[1][1],1.25,64.0);\n#include <logdepthbuf_vertex>');
 shader.fragmentShader=shader.fragmentShader.replace('#include <clipping_planes_fragment>','#include <clipping_planes_fragment>\nif(distance(gl_PointCoord,vec2(.5))>.5)discard;');
};
const state=new DenseCloudState({capacity:5_000_000});
const receiver=new DenseCloudReceiver({state,endpoint:'/dense/delta.bin'});
const gpu=new DenseGpuChunks({state,THREE,material:cloudMaterial,parent:root});gpu.group.renderOrder=2;
const raw=new RawView(root,()=>{if(!framed){frameScan();framed=true;}change();},{endpoint:'/capture/frame.bin',history:false,surfaceHistory:true,fallbackOnly:true,pollInterval:1000,onChange:change});
raw.group.visible=false;
const remove=raw.remove.bind(raw);
raw.remove=mesh=>{const materials=mesh.userData.comparisonMaterials;if(materials){mesh.material=materials.texture;materials.color.dispose();}remove(mesh);};
const post=raw.worker.postMessage.bind(raw.worker);
raw.worker.postMessage=(message,transfer)=>{
 inFlightInput=dispatchInput??{kind:'live',oneShot:freezeAfterCapture};
 return post(message,transfer);
};
function cancelPendingLive(){
 if(freezeAfterCapture||inFlightInput?.kind==='recorded')return;
 if(inFlightInput)inFlightInput.cancelled=true;
 raw.controller?.abort();
}
const receive=raw.worker.onmessage;
raw.worker.onmessage=event=>{
 const data=event.data,input=inFlightInput;inFlightInput=null;
 if(input?.cancelled){data.patch?.image.close();raw.busy=false;updateLabels();change();return;}
 if(!data.error&&raw.active){
  gpu.group.visible=input?.kind!=='recorded';
  if(input?.kind==='recorded'||inputKind==='recorded'){raw.clear();framed=false;}
 }
 if(!data.error){const prepared=prepareComparisonCapture(data);data.patch.indices=prepared.indices;data.fallback_indices=prepared.fallbackIndices;data.patch.comparisonColors=prepared.colors;}
 receive(event);
 if(!data.error&&raw.active&&raw.frame===data.frame){
  const mesh=raw.patches.at(-1);
  mesh.geometry.setAttribute('color',new THREE.BufferAttribute(data.patch.comparisonColors,3));
  const texture=mesh.material,color=new THREE.MeshBasicMaterial({vertexColors:true,side:THREE.DoubleSide,toneMapped:false,polygonOffset:true,polygonOffsetFactor:-1,polygonOffsetUnits:-1});
  mesh.userData.comparisonMaterials={color,texture};raw.comparisonPointIndex=raw.points.geometry.getIndex();accepted++;inputKind=input?.kind??'live';
  recordingName=inputKind==='recorded'?input.name:'';recordedWall=inputKind==='recorded'?input.wall:null;
  if(inputKind==='recorded'){frozen=true;streaming=false;}
  applyComparisonMode(raw,mode);
 }
 if(freezeAfterCapture){freezeAfterCapture=false;frozen=true;streaming=false;}
 updateLabels();change();
};
const workerError=raw.worker.onerror;
raw.worker.onerror=event=>{inFlightInput=null;workerError(event);freezeAfterCapture=false;streaming=false;frozen=true;updateLabels();change();};
const titles={points:['A · Points only','Measured points, without connecting faces.'],balanced:['B · Small triangles + points','Same native depth faces, with camera colors sampled at their vertices.'],texture:['C · Camera color + points','Same native depth faces, with the full camera image across each face.']};
function setMode(value){mode=value;applyComparisonMode(raw,mode);cloudMaterial.size=mode==='points'?.035:.018;updateLabels();change();return stats();}
function bounds(){const box=new THREE.Box3();if(gpu.group.visible)for(const slot of gpu.slots.values())if(slot.points.geometry.boundingBox)box.union(slot.points.geometry.boundingBox);if(raw.count&&raw.points.geometry.boundingBox)box.union(raw.points.geometry.boundingBox);return box;}
function setPose(value='front'){
 const box=bounds();if(box.isEmpty())return;const center=box.getCenter(new THREE.Vector3()),radius=Math.max(.3,box.getSize(new THREE.Vector3()).length()/2);
 const vectors={front:new THREE.Vector3(.45,.3,1),side:new THREE.Vector3(1,.25,.25),above:new THREE.Vector3(.1,1,.2)};
 const distance=radius/Math.sin(THREE.MathUtils.degToRad(camera.fov/2))*1.06;
 camera.position.copy(center).addScaledVector(vectors[value].normalize(),distance);camera.up.set(0,1,0);controls.target.copy(center);controls.update();change();
}
function frameScan(){setPose('front');}
function stats(){
 const native=raw.stats(),triangles=mode==='points'?0:native.raw_triangles;
 return {mode,input:inputKind,requested_input:requestedKind,capture_pending:raw.busy,frozen,streaming,accepted_captures:accepted,recording:recordingName||null,recorded_wall_ms:recordedWall,source:inputKind==='recorded'?`RECORDED ${native.raw_source??'UNVERIFIED RGB-D'}`:inputKind==='live'?(native.raw_source??'WAITING FOR NATIVE RGB-D'):receiver.sourceLabel,retained_points:gpu.group.visible?state.size:0,stored_retained_points:state.size,native_points:raw.count,triangles,native_faces:native.raw_triangles,native_patches:native.raw_patches,fallback_points:raw.fallbackCount,native_frame:native.raw_frame,native_identity:native.raw_identity,native_age_ms:inputKind==='recorded'?null:native.raw_age_ms,recorded_decode_age_ms:inputKind==='recorded'?native.raw_age_ms:null,native_bytes:native.raw_bytes,error:native.raw_error,dense_error:receiver.error,bootstrap_incomplete:receiver.cursor.snapshot==='true',dense_epoch:receiver.epoch,camera:camera.position.toArray(),target:controls.target.toArray(),draw_calls:renderer.info.render.calls,scope:'Read-only desktop comparison; no rover control or headset performance claim'};
}
function updateLabels(){
 const s=stats();document.querySelector('#title').textContent=titles[mode][0];document.querySelector('#subtitle').textContent=titles[mode][1];
 document.querySelectorAll('[data-mode]').forEach(button=>button.classList.toggle('active',button.dataset.mode===mode));
 const source=inputKind==='recorded'?`${s.source} · ${recordingName} · ${recordedWall?new Date(recordedWall).toLocaleString():'original capture'}`:inputKind==='live'?`${s.source} · ${frozen?'CAPTURED SAMPLE':streaming?'LIVE CAPTURE REQUESTED':'ONE-SHOT CAPTURE'}`:`POINT HISTORY · native capture idle`;
 document.querySelector('#source').textContent=source;
 document.querySelector('#stats').textContent=`${s.retained_points.toLocaleString()} retained points · ${s.native_points.toLocaleString()} native samples · ${s.triangles.toLocaleString()} triangles · ${s.fallback_points.toLocaleString()} uncovered samples${s.native_frame!==null?' · frame '+s.native_frame:''}${s.bootstrap_incomplete?' · point bootstrap incomplete':''}`;
 document.querySelector('#error').textContent=raw.error??(!s.native_faces&&mode!=='points'?'No native triangles accepted yet. Capture a sample or load a recorded RGB-D packet.':receiver.error??'');
 shownError=raw.error;document.querySelector('#capture').disabled=raw.busy;document.querySelector('#recording').disabled=raw.busy;document.querySelector('#live').textContent=streaming?'Stop live capture':'Start live capture';document.querySelector('#freeze').disabled=inputKind==='recorded';document.querySelector('#freeze').textContent=inputKind==='recorded'?'Recorded sample frozen':frozen?'Resume point history':'Freeze comparison';
}
async function captureOnce(){
 if(raw.busy)return;
 requestedKind='live';streaming=false;frozen=true;freezeAfterCapture=true;receiver.request?.abort();
 await raw.update(true,false,true,true);
 if(!raw.busy){freezeAfterCapture=false;frozen=true;}
 updateLabels();change();
}
function setFrozen(value){if(inputKind==='recorded'&&!value)return;frozen=value;if(value){streaming=false;receiver.request?.abort();cancelPendingLive();}updateLabels();change();}
async function loadRecording(buffer,name='recorded-frame.bin'){
 if(raw.busy)return false;
 streaming=false;frozen=true;freezeAfterCapture=false;requestedKind='recorded';receiver.request?.abort();
 const headerSize=new DataView(buffer).getUint32(0,true),header=JSON.parse(new TextDecoder().decode(new Uint8Array(buffer,4,headerSize)));dispatchInput={kind:'recorded',name,wall:header.t_wall_ms};
 // A recorded input is intentionally not a live-age admission or qualification.
 // Decode the original capture identity unchanged, with its recorded label.
 raw.active=true;raw.group.visible=true;raw.busy=true;raw.pendingAge=0;raw.pendingAt=Date.now();raw.bytes+=buffer.byteLength;
 try{raw.worker.postMessage({buffer,history:false,ttl:raw.ttl},[buffer]);}finally{dispatchInput=null;}
 updateLabels();change();return true;
}
document.querySelectorAll('[data-mode]').forEach(button=>button.onclick=()=>setMode(button.dataset.mode));
document.querySelectorAll('[data-pose]').forEach(button=>button.onclick=()=>setPose(button.dataset.pose));
document.querySelector('#capture').onclick=()=>captureOnce();document.querySelector('#frame').onclick=()=>frameScan();
document.querySelector('#freeze').onclick=()=>setFrozen(!frozen);
document.querySelector('#live').onclick=()=>{if(streaming){streaming=false;frozen=true;cancelPendingLive();}else{requestedKind='live';raw.active=true;raw.group.visible=true;streaming=true;frozen=false;}updateLabels();change();};
document.querySelector('#recording').onchange=async event=>{const file=event.target.files[0];if(file)await loadRecording(await file.arrayBuffer(),file.name);};
let seenPages=-1,seenReset=-1;
renderer.setAnimationLoop(()=>{
 if(disposed)return;
 if(!frozen)receiver.update();else receiver.request?.abort();
 if(state.dirty.size||receiver.pages!==seenPages||state.resetVersion!==seenReset){gpu.flush();seenPages=receiver.pages;seenReset=state.resetVersion;change();updateLabels();if(!framed&&state.size&&!receiver.more){frameScan();framed=true;}}
 void raw.update(raw.active,frozen,streaming).then(()=>{if(raw.error!==shownError){updateLabels();change();}});
 if(dirty){gpu.beginFrame();renderer.render(scene,camera);gpu.endFrame();dirty=false;}
});
addEventListener('resize',()=>{renderer.setSize(innerWidth,innerHeight);camera.aspect=innerWidth/innerHeight;camera.updateProjectionMatrix();change();});
function dispose(){disposed=true;renderer.setAnimationLoop(null);receiver.dispose();gpu.dispose();raw.dispose();cloudMaterial.dispose();controls.dispose();renderer.dispose();}
addEventListener('pagehide',dispose);
window.scanCompare={ready:true,setMode,setPose,setFrozen,captureOnce,loadRecording,stats,frameScan,dispose,render:()=>renderer.render(scene,camera)};
setMode(new URLSearchParams(location.search).get('mode')??'points');
