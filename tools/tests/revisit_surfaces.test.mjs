import test from 'node:test';
import assert from 'node:assert/strict';
import {DepthObservation,RevisitSurfaces,preparePointHistory,commitPointHistory} from '../dense_viewer/revisit-surfaces.mjs';

const identity=[1,0,0,0,0,1,0,0,0,0,1,0,0,0,0,1];
function grid({width=4,height=4,depth=2,fx=4,fy=4,cx=(width-1)/2,cy=(height-1)/2,valid,transform=identity}={}){
 const depths=typeof depth==='number'?new Float32Array(width*height).fill(depth):depth;
 valid??=new Uint8Array(width*height).fill(1);
 const positions=new Float32Array(width*height*3),indices=[];
 for(let y=0;y<height;y++)for(let x=0;x<width;x++){
  const i=y*width+x,z=depths[i],a=(x-cx)*z/fx,b=-(y-cy)*z/fy,c=-z,m=transform;
  positions.set([m[0]*a+m[4]*b+m[8]*c+m[12],m[1]*a+m[5]*b+m[9]*c+m[13],m[2]*a+m[6]*b+m[10]*c+m[14]],i*3);
 }
 const triangle=(a,b,c)=>{
  const lo=Math.min(depths[a],depths[b],depths[c]),hi=Math.max(depths[a],depths[b],depths[c]);
  if(valid[a]&&valid[b]&&valid[c]&&hi-lo<=Math.max(.025,lo*.04))indices.push(a,b,c);
 };
 for(let y=0;y<height-1;y++)for(let x=0;x<width-1;x++){const a=y*width+x,b=a+1,c=a+width,d=c+1;triangle(a,c,b);triangle(b,c,d);}
 const faces=new Uint32Array(indices);
 return {positions,indices:faces,observation:new DepthObservation({width,height,fx,fy,cx,cy,transform,depths,valid,indices:faces})};
}
function accept(history,capture,{id=1,source='a',at=0,ttl=30,surfaceHistory=true}={}){
 const prepared=history.prepare({identity:source,id,positions:capture.positions,indices:capture.indices,at,now:at,ttl,history:surfaceHistory,observation:capture.observation});
 history.commit(prepared);return prepared;
}
function faceAt(observation,pixels,depth){
 const {fx,fy,cx,cy}=observation;
 return {positions:new Float32Array(pixels.flatMap(([u,v])=>[(u-cx)*depth/fx,-(v-cy)*depth/fy,-depth])),indices:new Uint32Array([0,1,2]),observation};
}

test('a departed foreground surface is replaced with newly measured background',()=>{
 const history=new RevisitSurfaces();accept(history,grid({depth:1}));
 const update=accept(history,grid({depth:4}),{id:2});
 assert.deepEqual(update.removed,[1]);assert.deepEqual(history.patches.map(p=>p.id),[2]);
 assert.equal(history.patches[0].positions[2],-4);
});

test('a removed thin object six centimetres in front of a wall retires',()=>{
 const history=new RevisitSurfaces();accept(history,grid({depth:1.94}));
 const update=accept(history,grid({depth:2}),{id:2});
 assert.deepEqual(update.removed,[1]);assert.deepEqual(history.patches.map(p=>p.id),[2]);
 const cells=new Map([['thin_object',cell([0,0,-1.94])],['wall',cell([0,0,-2])]]);
 const points=preparePointHistory(cells,new Map(),grid({depth:2}).observation,{now:1});
 assert.deepEqual([...points.removed],['thin_object']);
 commitPointHistory(cells,points);assert.deepEqual([...cells.keys()],['wall']);
});

test('background four centimetres behind the current surface remains occluded',()=>{
 const history=new RevisitSurfaces(),background=grid({depth:2.04});accept(history,background);
 const update=accept(history,grid({depth:2}),{id:2});
 assert.deepEqual(update.updates,[]);assert.deepEqual(update.removed,[]);
 assert.deepEqual(history.patches[0].indices,background.indices);
 assert.equal(grid({depth:2}).observation.classifyPoint(0,0,-2.04),0);
});

test('same-pose measured surfaces refresh texture without retaining duplicates',()=>{
 const history=new RevisitSurfaces();accept(history,grid());
 const update=accept(history,grid({depth:2.03}),{id:2});
 assert.deepEqual(update.removed,[1]);assert.equal(history.patches.length,1);
});

test('Float32 native world coordinates still refresh the image-border triangles',()=>{
 const history=new RevisitSurfaces(),options={width:64,height:48,fx:51.921,fy:52.345,cx:31.021,cy:23.109,depth:3.171};
 accept(history,grid(options));
 const update=accept(history,grid(options),{id:2});
 assert.deepEqual(update.removed,[1]);assert.deepEqual(history.patches.map(p=>p.id),[2]);
});

test('a partial camera revisit trims covered faces and retains out-of-view faces',()=>{
 const history=new RevisitSurfaces(),old=grid({width:7,height:7});accept(history,old);
 const update=accept(history,grid({width:3,height:3}),{id:2});
 assert.equal(update.updates.length,1);assert.equal(update.updates[0].id,1);
 assert.ok(update.updates[0].indices.length>0);
 assert.ok(update.updates[0].indices.length<old.indices.length);
 assert.equal(history.patches.length,2);
 const oldIndices=Array.from(update.updates[0].indices);
 assert.ok(oldIndices.includes(0),'outer corner remains outside the refreshed field of view');
});

test('one contradicted vertex retires its old face even without a replacement triangle',()=>{
 const history=new RevisitSurfaces(),old=grid({width:3,height:3});accept(history,old);
 const valid=new Uint8Array(9);valid[4]=1;
 const current=grid({width:3,height:3,depth:4,valid});
 assert.equal(current.indices.length,0);
 const update=accept(history,current,{id:2});
 const kept=update.updates[0].indices;
 assert.ok(kept.length<old.indices.length);assert.ok(!Array.from(kept).includes(4));
});

test('unknown depth and a nearer occluder preserve old surface geometry',()=>{
 for(const current of [grid({valid:new Uint8Array(16)}),grid({depth:1})]){
  const history=new RevisitSurfaces(),old=grid({depth:4});accept(history,old);
  const update=accept(history,current,{id:2});
  assert.deepEqual(update.updates,[]);assert.deepEqual(update.removed,[]);
  assert.deepEqual(history.patches[0].indices,old.indices);
 }
});

test('a matching depth sample without a replacement mesh retains the old texture',()=>{
 const history=new RevisitSurfaces(),old=grid();accept(history,old);
 const current=grid();current.indices=new Uint32Array();
 current.observation=new DepthObservation({width:4,height:4,fx:4,fy:4,cx:1.5,cy:1.5,transform:identity,depths:new Float32Array(16).fill(2),valid:new Uint8Array(16).fill(1),indices:current.indices});
 const update=accept(history,current,{id:2});
 assert.deepEqual(update.updates,[]);assert.deepEqual(update.removed,[]);
 assert.equal(history.patches.length,1);
});

test('minimum-pooled native blocks preserve thin occluders and unknown samples',()=>{
 for(const unknown of [false,true]){
  const width=256,height=192,depths=new Float32Array(width*height).fill(5),valid=new Uint8Array(width*height).fill(1);
  depths[10*width+10]=1;if(unknown)valid[10*width+10]=0;
  const current=grid({width,height,depth:depths,valid,fx:100,fy:100,cx:128,cy:96});
  assert.equal(current.observation.stride,2);
  const old=faceAt(current.observation,[[10.1,10.1],[10.8,10.1],[10.1,10.8]],2);
  const history=new RevisitSurfaces();accept(history,old);
  const update=accept(history,current,{id:2});
  assert.deepEqual(update.updates,[]);assert.deepEqual(update.removed,[]);
  assert.deepEqual(history.patches[0].indices,old.indices);
 }
});

test('projection uses the calibrated translated and rotated -Z camera pose',()=>{
 const pose=[0,0,-1,0,0,1,0,0,1,0,0,0,3,2,1,1],current=grid({depth:4,transform:pose});
 const selected=current.observation.classify(new Float32Array([1,2,1,7,2,1,1,2,100]));
 assert.equal(selected[0]&1,1);assert.equal(selected[1],0);assert.equal(selected[2],0);
});

test('the depth tolerance preserves occluded geometry just beyond the current surface',()=>{
 const observation=grid({depth:2}).observation,tolerance=.021;
 assert.equal(observation.classify(new Float32Array([0,0,-(2+tolerance+.001)]))[0],0);
 assert.equal(observation.classify(new Float32Array([0,0,-(2-tolerance-.001)]))[0]&1,1);
 assert.equal(observation.classify(new Float32Array([0,0,-2]))[0]&2,2);
});

test('a prepared expired update does not mutate history unless committed',()=>{
 const history=new RevisitSurfaces(),old=grid({depth:1});accept(history,old);
 const current=grid({depth:4});
 const prepared=history.prepare({identity:'a',id:2,positions:current.positions,indices:current.indices,at:1,observation:current.observation});
 assert.deepEqual(prepared.removed,[1]);
 assert.deepEqual(history.patches.map(p=>p.id),[1]);assert.deepEqual(history.patches[0].indices,old.indices);
});

test('transferable renderer buffers cannot neuter retained history',()=>{
 const history=new RevisitSurfaces(),old=grid({width:7,height:7});accept(history,old);
 structuredClone({positions:old.positions,indices:old.indices},{transfer:[old.positions.buffer,old.indices.buffer]});
 assert.ok(history.patches[0].positions.length);assert.ok(history.patches[0].indices.length);
 const update=accept(history,grid({width:3,height:3}),{id:2}),indices=update.updates[0].indices;
 structuredClone(indices,{transfer:[indices.buffer]});
 assert.equal(indices.length,0);assert.ok(history.patches[0].indices.length);
});

test('source reset, TTL and history-off remove previous patch state',()=>{
 const history=new RevisitSurfaces();accept(history,grid());
 accept(history,grid(),{id:1,source:'b'});
 assert.equal(history.identity,'b');assert.deepEqual(history.patches.map(p=>p.id),[1]);
 const aged=accept(history,grid(),{id:2,source:'b',at:1001,ttl:1});assert.deepEqual(aged.removed,[1]);
 const latest=accept(history,grid({depth:1}),{id:3,source:'b',at:1002,surfaceHistory:false});
 assert.deepEqual(latest.removed,[2]);assert.deepEqual(history.patches.map(p=>p.id),[3]);
});

test('retained patch, vertex and triangle counts stay bounded',()=>{
 for(const limits of [{maxPatches:2},{maxVertices:5},{maxTriangles:1}]){
  const history=new RevisitSurfaces(limits),observation=grid().observation;
  for(let id=1;id<=4;id++){
   const positions=new Float32Array([100,0,-2,101,0,-2,100,1,-2]);
   accept(history,{positions,indices:new Uint32Array([0,1,2]),observation},{id});
  }
  assert.ok(history.patches.length<=(limits.maxPatches??1));
  assert.ok(history.patches.reduce((n,p)=>n+p.positions.length/3,0)<=(limits.maxVertices??500_000));
  assert.ok(history.patches.reduce((n,p)=>n+p.indices.length/3,0)<=(limits.maxTriangles??1_000_000));
 }
});

test('both accepted halves of a native depth quad provide mesh coverage',()=>{
 const observation=grid({width:2,height:2}).observation;
 assert.equal(observation.covers(.1,.1),true);assert.equal(observation.covers(.9,.9),true);
 const single=new DepthObservation({width:2,height:2,fx:4,fy:4,cx:.5,cy:.5,transform:identity,depths:new Float32Array(4).fill(2),valid:new Uint8Array(4).fill(1),indices:new Uint32Array([0,2,1])});
 assert.equal(single.covers(.1,.1),true);assert.equal(single.covers(.9,.9),false);
});

const cell=(p,color=[.1,.2,.3],at=0)=>({p,color,at});
test('raw history retires departed foreground points and preserves the background',()=>{
 const cells=new Map([['person',cell([0,0,-1])],['background',cell([0,0,-4])],['occluded',cell([0,0,-6])],['outside',cell([100,0,-1])]]);
 const incoming=new Map([['new_background',cell([.1,0,-4],[.8,.9,1],1)]]);
 const prepared=preparePointHistory(cells,incoming,grid({depth:4}).observation,{now:1});
 assert.deepEqual([...prepared.removed],['person']);
 assert.equal(cells.has('person'),true,'preparing cannot change retained state');
 assert.equal(prepared.positions.length/3,4);
 commitPointHistory(cells,prepared);
 assert.deepEqual([...cells.keys()],['background','occluded','outside','new_background']);
 assert.deepEqual(cells.get('background').p,[0,0,-4]);
});

test('raw history preserves points hidden by people and unknown native pixels',()=>{
 const valid=new Uint8Array(16).fill(1);valid[0]=0;
 const cells=new Map([['behind_person',cell([0,0,-4])],['unknown',cell([-.75,.75,-2])]]);
 const prepared=preparePointHistory(cells,new Map(),grid({depth:1,valid}).observation,{now:1});
 assert.equal(prepared.removed.size,0);commitPointHistory(cells,prepared);
 assert.deepEqual([...cells.keys()],['behind_person','unknown']);
});

test('raw point updates, expiry, capacity and source resets remain atomic',()=>{
 const observation=grid({depth:4,valid:new Uint8Array(16)}).observation;
 const cells=new Map([['expired',cell([100,0,-2],undefined,0)],['oldest',cell([100,0,-2],undefined,500)],['refreshed',cell([100,0,-2],undefined,501)],['kept',cell([100,0,-2],undefined,502)]]);
 const incoming=new Map([['refreshed',cell([100,0,-3],[1,0,0],1100)],['new',cell([100,0,-3],[0,1,0],1100)]]);
 const prepared=preparePointHistory(cells,incoming,observation,{now:1100,ttl:1,capacity:3});
 assert.deepEqual([...prepared.removed],['expired','refreshed','oldest']);
 assert.deepEqual([...cells.keys()],['expired','oldest','refreshed','kept']);
 assert.deepEqual(cells.get('refreshed').p,[100,0,-2]);
 commitPointHistory(cells,prepared);
 assert.deepEqual([...cells.keys()],['kept','refreshed','new']);
 assert.deepEqual(cells.get('refreshed').color,[1,0,0]);
 const reset=preparePointHistory(cells,new Map([['reset',cell([0,0,-2])]]),observation,{reset:true,now:1101});
 assert.equal(cells.size,3);commitPointHistory(cells,reset);assert.deepEqual([...cells.keys()],['reset']);
});
