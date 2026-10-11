// Real Three.js buffers, controlled completed worker messages; no GPU/browser.
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import * as THREE from '../../quest/node_modules/three/build/three.module.js';

// Preserve the production class while resolving its browser dependency and
// import.meta URL for Node. Only the worker and HTTP transport are controlled.
const sourceUrl=new URL('../dense_viewer/raw-view.js',import.meta.url);
const threeUrl=new URL('../../quest/node_modules/three/build/three.module.js',import.meta.url);
const source=(await readFile(sourceUrl,'utf8'))
 .replace("from 'three'",`from ${JSON.stringify(threeUrl.href)}`)
 .replaceAll('import.meta.url',JSON.stringify(sourceUrl.href));
const {RawView}=await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);
class ControlledWorker {
 postMessage(data){this.pending=data;}
 complete(data){this.onmessage({data});}
 terminate(){}
}
function capture(frame,{positions=[-.25,.25,-1,.25,.25,-1,-.25,-.25,-1,.25,-.25,-1,-.75,-.75,-2,.75,-.75,-2],fallback=[4,5],removed=[],faces=[0,2,1,1,2,3]}={}){
 const p=new Float32Array(positions),image={closed:0,close(){this.closed++;}};
 return {identity:'test-session',frame,mode:'latest',max_points:6,points:p.length/3,
  positions:p,colors:new Float32Array(p.length).fill(.5),fallback_indices:new Uint32Array(fallback),
  point_transfer_bytes:p.byteLength*2,processing_ms:1,source:'CONTROLLED TEST CAPTURE',
  patch:{id:frame,positions:p.slice(),uv:new Float32Array(p.length/3*2),indices:new Uint32Array(faces),image,at:Date.now(),transform:new THREE.Matrix4().toArray()},
  patch_updates:[],patch_removed:removed};
}
function setup(t){
 const originalWorker=globalThis.Worker,originalFetch=globalThis.fetch;
 globalThis.Worker=ControlledWorker;
 const calls=[];
 globalThis.fetch=async url=>{
  calls.push(url);
  return calls.length<=2?{status:200,ok:true,headers:{get:name=>name==='X-Capture-Key'?`capture-${calls.length}`:'0'},arrayBuffer:async()=>new ArrayBuffer(8)}:{status:204};
 };
 const view=new RawView(new THREE.Group(),undefined,{history:false,surfaceHistory:true,fallbackOnly:true});
 t.after(()=>{view.dispose();globalThis.Worker=originalWorker;globalThis.fetch=originalFetch;});
 return {view,calls};
}

test('an accepted revisit updates hidden dot buffers before a later 204 response',async t=>{
 const {view,calls}=setup(t);
 await view.update(true,false,true,true);const first=capture(1);view.worker.complete(first);
 const positions=view.points.geometry.getAttribute('position'),index=view.points.geometry.getIndex();
 assert.equal(positions.array[2],-1);assert.equal(view.points.geometry.drawRange.count,2);

 await view.update(true,false,true,true);
 assert.ok(view.busy,'second capture is processing in the worker');
 await view.update(false,false);
 const revisit=capture(2,{positions:[-.25,.25,-4,.25,.25,-4,-.25,-.25,-4,.25,-.25,-4,0,0,-4],fallback:[4],removed:[1]});
 view.worker.complete(revisit);
 assert.equal(view.group.visible,false,'completion does not reveal a disabled layer');
 assert.equal(view.active,false);assert.equal(view.frame,2);assert.equal(view.count,5);
 assert.equal(view.points.geometry.getAttribute('position'),positions,'reuse the allocated point buffer');
 assert.equal(view.points.geometry.getIndex(),index,'reuse the allocated fallback index buffer');
 assert.equal(positions.array[2],-4);assert.equal(index.array[0],4);
 assert.equal(view.points.geometry.drawRange.count,1,'old fallback indices are outside the new draw range');
 assert.equal(view.stats().raw_point_draw_count,1);
 assert.deepEqual(view.patches.map(p=>p.userData.id),[2]);
 assert.equal(first.patch.image.closed,1);assert.equal(revisit.patch.image.closed,0);

 await view.update(true,false,true,true);
 assert.equal(calls.length,3);assert.match(calls[2],/after=capture-2/);
 assert.equal(view.group.visible,true);assert.equal(view.frame,2);
 assert.equal(positions.array[2],-4,'no newer capture is needed to display the accepted replacement');
});

test('an accepted empty native frame clears hidden dot draw counts and retired images',async t=>{
 const {view}=setup(t);
 await view.update(true,false,true,true);const first=capture(1);view.worker.complete(first);
 await view.update(true,false,true,true);await view.update(false,false);
 const empty=capture(2,{positions:[],fallback:[],removed:[1],faces:[]});view.worker.complete(empty);
 assert.equal(view.group.visible,false);assert.equal(view.frame,2);
 assert.equal(view.count,0);assert.equal(view.fallbackCount,0);
 assert.equal(view.points.geometry.drawRange.count,0);assert.equal(view.stats().raw_point_draw_count,0);
 assert.equal(view.patches.length,0);assert.equal(first.patch.image.closed,1);assert.equal(empty.patch.image.closed,1);
});
