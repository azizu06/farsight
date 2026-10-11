import {readFile} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
import {createRequire} from 'node:module';
import vm from 'node:vm';
import assert from 'node:assert/strict';
const root=fileURLToPath(new URL('..',import.meta.url));
const require=createRequire(root+'/package.json');
const ts=require('typescript'),THREE=require('three');
const protocol=await import('file://'+root+'/src/protocol.js');
const {floorHeight}=await import('file://'+root+'/src/map-walk.js');
const dir=root+'/src';
function deferred(){let resolve;const promise=new Promise(r=>resolve=r);return{promise,resolve};}
async function until(predicate){for(let i=0;i<100;i++){if(predicate())return;await Promise.resolve();}assert.fail('fixture did not settle');}
function denseBody(){const h=new TextEncoder().encode(JSON.stringify({format:'xyz_mm_rgb8_op',points:1,epoch:'audit',revision:1,chunk:[0,0,0],lod:0}));const b=new ArrayBuffer(4+h.length+16),v=new DataView(b);v.setUint32(0,h.length,true);new Uint8Array(b,4,h.length).set(h);v.setUint8(4+h.length+15,1);return b;}
function pointsBody(){const b=new ArrayBuffer(20);new DataView(b).setUint32(0,1,true);return b;}
function meshBody(){const b=new ArrayBuffer(68),v=new DataView(b);v.setUint32(0,3,true);v.setUint32(4,3,true);for(let i=0;i<3;i++)v.setUint32(56+i*4,i,true);return b;}
let denseSource=await readFile(dir+'/dense-map.ts','utf8');
denseSource=denseSource.replace('from "three"','from '+JSON.stringify('file://'+root+'/node_modules/three/build/three.module.js'));
denseSource=denseSource.replace('import { decodeDenseChunk, DENSE_CHUNK_POINTS, DENSE_VIEW_CHUNKS } from "./dense-protocol.js";',
 'import { decodeDenseChunk as realDecodeDenseChunk, DENSE_CHUNK_POINTS, DENSE_VIEW_CHUNKS } from '+JSON.stringify('file://'+root+'/src/dense-protocol.js')+';\nconst decodeDenseChunk=buffer=>{globalThis.auditDecodeCalls++;return realDecodeDenseChunk(buffer);};');
denseSource=denseSource.replace('"./map-walk.js"',JSON.stringify('file://'+root+'/src/map-walk.js'));
const {DenseMapView}=await import('data:text/javascript;base64,'+Buffer.from(ts.transpileModule(denseSource,{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.ESNext}}).outputText).toString('base64'));
globalThis.window={setTimeout,clearTimeout};let now=1000;globalThis.performance={now:()=>now};
const camera=new THREE.PerspectiveCamera();
function denseFixture(){globalThis.auditDecodeCalls=0;let bounds=0;const v=new DenseMapView(new THREE.Group(),{info:{render:{points:0}},xr:{isPresenting:false}},()=>bounds++);return{v,bounds:()=>bounds};}
const results=[];
{
 now=1000;const f=denseFixture(),ready=deferred(),body=deferred();let chunkCalls=0;
 globalThis.fetch=async url=>url.startsWith('/dense/manifest')?{ok:true,json:async()=>({epoch:'audit',chunks:[{id:'0_0_0',revision:1,lod:0}]})}:url.startsWith('/dense/chunk.bin')?(chunkCalls++,{ok:true,arrayBuffer:()=>{ready.resolve();return body.promise;}}):{ok:true};
 const p=f.v.pull(now);await ready.promise;f.v.update(++now,camera,false);body.resolve(denseBody());await p;
 assert.equal(globalThis.auditDecodeCalls,0);assert.equal(f.bounds(),0);assert.equal(f.v.points,0);assert.equal(f.v.busy,false);
 globalThis.fetch=async url=>url.startsWith('/dense/manifest')?{ok:true,json:async()=>({epoch:'audit',chunks:[{id:'0_0_0',revision:1,lod:0}]})}:url.startsWith('/dense/chunk.bin')?(chunkCalls++,{ok:true,arrayBuffer:async()=>denseBody()}):{ok:true};
 now=2000;f.v.update(now,camera,true);await until(()=>!!f.v.resumeUpload);assert.equal(globalThis.auditDecodeCalls,1);assert.equal(f.v.points,1);assert.equal(f.bounds(),1);
 f.v.update(++now,camera,true);await until(()=>!f.v.busy);assert.equal(f.v.group.visible,true);assert.equal(chunkCalls,2);f.v.dispose();
 results.push({case:'dense-late-body-cancel-zero-decode-and-actual-reenable-one-decode',passed:true});
}
{
 now=1000;const f=denseFixture();let chunks=0;
 globalThis.fetch=async url=>url.startsWith('/dense/manifest')?{ok:true,json:async()=>({epoch:'audit',chunks:[{id:'0_0_0',revision:1,lod:0},{id:'1_0_0',revision:1,lod:0}]})}:url.startsWith('/dense/chunk.bin')?(chunks++,{ok:true,arrayBuffer:async()=>denseBody()}):{ok:true};
 const p=f.v.pull(now);await until(()=>!!f.v.resumeUpload);assert.equal(globalThis.auditDecodeCalls,1);assert.equal(f.v.points,1);
 f.v.update(++now,camera,false);await p;assert.equal(chunks,1);assert.equal(globalThis.auditDecodeCalls,1);assert.equal(f.v.points,1);assert.equal(f.bounds(),1);assert.equal(f.v.group.visible,false);assert.equal(f.v.busy,false);f.v.dispose();
 results.push({case:'dense-yield-cancel-no-next-fetch-decode-or-mutation-retains-fallback',passed:true});
}
{
 now=1000;const f=denseFixture(),ready=deferred(),manifest=deferred();f.v.epoch='cached';
 const geometry=new THREE.BufferGeometry(),points=new THREE.Points(geometry);f.v.group.add(points);f.v.slots.set('cached',{points,key:'old',count:1,floor:0});
 globalThis.fetch=async url=>url.startsWith('/dense/manifest')?{ok:true,json:()=>{ready.resolve();return manifest.promise;}}:{ok:true};
 const p=f.v.pull(now);await ready.promise;f.v.update(++now,camera,false);manifest.resolve({epoch:'changed',chunks:[]});await p;
 assert.equal(f.v.epoch,'cached');assert.equal(f.v.points,1);assert.equal(globalThis.auditDecodeCalls,0);f.v.dispose();
 results.push({case:'dense-late-manifest-cancel-preserves-cache-and-epoch',passed:true});
}
{
 now=1000;const f=denseFixture();let atResume=null;f.v.request=new AbortController();
 f.v.resumeUpload=()=>{atResume=f.v.request.signal.aborted;f.v.resumeUpload=null;};f.v.request.signal.addEventListener('abort',()=>f.v.resumeUpload?.());
 f.v.update(++now,camera,false);assert.equal(atResume,true);f.v.dispose();
 results.push({case:'disabled-update-aborts-before-pending-upload-release',passed:true});
}
const mainSource=await readFile(dir+'/main.ts','utf8');
const tree=ts.createSourceFile('main.ts',mainSource,ts.ScriptTarget.Latest,true);
const texts=new Map(tree.statements.filter(n=>ts.isFunctionDeclaration(n)&&n.name).map(n=>[n.name.text,n.getText(tree)]));
const mainFunctions=['loadMesh','loadPoints'].map(n=>texts.get(n)).join('\n')+'\n'+texts.get('replaceGeometry').replace('function replaceGeometry(','function actualReplaceGeometry(');
const mainJS=ts.transpileModule(mainFunctions,{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.ESNext}}).outputText;
function mainFixture(kind){
 let decodes=0,mutations=0,calls=0;const body=deferred(),ready=deferred();const object=()=>{const g=new THREE.BufferGeometry();g.setAttribute('position',new THREE.BufferAttribute(new Float32Array([9,9,9]),3));return new THREE.Points(g);};
 const ctx={THREE,AbortController,performance:{now:()=>now},generation:1,connectionVersion:1,needed:true,mapNeeded(){return ctx.needed;},pendingMesh:false,meshRequest:null,pointsRequest:null,lastMapAt:-Infinity,lastMapKind:null,appliedMesh:-1,appliedPoints:-1,MAP_TRANSFER_INTERVAL_MS:0,api:x=>x,mapResponseRevision:(_headers,next)=>next.revision,cappedMesh:()=>true,notify(){},renderer:{xr:{isPresenting:true}},cloud:object(),surfaces:object(),vrMapCenter:new THREE.Vector3(),vrMapSize:new THREE.Vector3(),vrMapSpan:0,mapWalk:{setFloor(){}},floorHeight,frameMap(){},
 fetch:async()=>{calls++;return{ok:true,headers:{},arrayBuffer:()=>{ready.resolve();return calls===1?body.promise:Promise.resolve(kind==='mesh'?meshBody():pointsBody());}};},
 decodeMesh:buffer=>{decodes++;return protocol.decodeMesh(buffer);},decodePoints:buffer=>{decodes++;return protocol.decodePoints(buffer);}};
 vm.createContext(ctx);vm.runInContext(mainJS,ctx);ctx.replaceGeometry=(...args)=>{mutations++;ctx.actualReplaceGeometry(...args);};
 return{ctx,body,ready,decodes:()=>decodes,mutations:()=>mutations,run:()=>ctx[kind==='mesh'?'loadMesh':'loadPoints']({generation:1,revision:5,mesh_revision:3,points_transport:'http'})};
}
for(const kind of ['mesh','points']){
 for(const reason of ['disabled-aborted','not-needed','generation-changed','connection-changed']){
  now=1000;const f=mainFixture(kind),p=f.run();await f.ready.promise;
  if(reason==='disabled-aborted'){f.ctx.needed=false;(kind==='mesh'?f.ctx.meshRequest:f.ctx.pointsRequest).abort();}
  if(reason==='not-needed')f.ctx.needed=false;
  if(reason==='generation-changed')f.ctx.generation=2;
  if(reason==='connection-changed')f.ctx.connectionVersion=2;
  f.body.resolve(kind==='mesh'?meshBody():pointsBody());await p;assert.equal(f.decodes(),0);assert.equal(f.mutations(),0);assert.equal(f.ctx[kind==='mesh'?'meshRequest':'pointsRequest'],null);
  results.push({case:kind+'-'+reason+'-zero-decoder-and-geometry-mutations',passed:true});
 }
 now=1000;const f=mainFixture(kind),p=f.run();await f.ready.promise;f.ctx.needed=false;(kind==='mesh'?f.ctx.meshRequest:f.ctx.pointsRequest).abort();f.body.resolve(kind==='mesh'?meshBody():pointsBody());await p;
 f.ctx.needed=true;now=2000;await f.run();assert.equal(f.decodes(),1);assert.equal(f.mutations(),1);assert.equal(f.ctx[kind==='mesh'?'surfaces':'cloud'].geometry.getAttribute('position').count,kind==='mesh'?3:1);
 results.push({case:kind+'-positive-after-cancellation-recovery-real-decode-real-geometry',passed:true});
}
const report={passed:true,checks:results.length,scope:'Actual production methods, real binary decoders and Three.js CPU geometry; injected completed-body/cancellation fixtures. No native-fetch/GPU/browser/deployment/headset evidence.',results};
console.log(JSON.stringify(report,null,2));
