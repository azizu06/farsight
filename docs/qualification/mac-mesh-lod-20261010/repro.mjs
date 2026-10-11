import {readFile,writeFile,mkdir} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
import {dirname,resolve} from 'node:path';
import {createRequire} from 'node:module';
import {createHash} from 'node:crypto';
import vm from 'node:vm';
import assert from 'node:assert/strict';
const here=dirname(fileURLToPath(import.meta.url));
const args=process.argv.slice(2);
const expectFixed=args.includes('--expect-fixed');
function option(name,fallback){const index=args.indexOf(name);if(index<0)return fallback;assert.ok(args[index+1]&&!args[index+1].startsWith('--'),'missing '+name+' value');return args[index+1];}
for(let i=0;i<args.length;i++){assert.ok(['--expect-fixed','--source','--out','--repo'].includes(args[i]),'unknown argument '+args[i]);if(args[i]!=='--expect-fixed')i++;}
const root=resolve(option('--repo',resolve(here,'../../..')));
const sourceName=option('--source',expectFixed?'source-fixed.mjs':'source-before.mjs');
const path=resolve(here,sourceName),source=await readFile(path,'utf8');
const outputPath=resolve(option('--out',resolve(here,expectFixed?'fixed.json':'baseline-replayed.json')));
const out=dirname(outputPath);await mkdir(out,{recursive:true});
const require=createRequire(root+'/quest/package.json');const THREE=require('three');
const sourceSha256=createHash('sha256').update(source).digest('hex');
const manifest=JSON.parse(await readFile(resolve(here,'source-manifest.json'),'utf8'));
const pinned=Object.values(manifest).find(entry=>entry.sha256===sourceSha256);
assert.ok(pinned,'source differs from both pinned snapshots');
assert.equal(pinned.commit,expectFixed?'7a98b132473c8ac3b967afb5423cbc22282bbfa1':'85c7253f6ea098eca8114130f33b58d08a10559e','expectation does not match pinned source');
await writeFile(out+'/render-source.mjs',source);
const slice=(begin,end)=>{const start=source.indexOf(begin),finish=source.indexOf(end,start);assert.ok(start>=0&&finish>start,begin);return source.slice(start,finish);};
const extracted=[slice('function pooledAttribute(','// Nested LOD prefixes:'),slice('function scatterOrder(','function uploadCloud('),slice('function uploadMesh(','function clearGeometry('),slice('const LADDER = [','const LOD_CLIMB_MS'),slice('function meshDrawCount()','let frameDirty = true;')].join('\n');
await writeFile(out+'/extracted-production.js',extracted);
const surfaceGeometry=new THREE.BufferGeometry(),cloudGeometry=new THREE.BufferGeometry();
const context={THREE,surfaceGeometry,cloudGeometry,style:'lit'};vm.createContext(context);
vm.runInContext(extracted+'\nlet lodIndex=1;let frameDirty=false;',context);
const nx=8,ny=8,positions=[],colors=[],indices=[];
for(let row=0;row<=ny;row++)for(let col=0;col<=nx;col++){positions.push(col,0,row);colors.push(.5,.5,.5);}
for(let row=0;row<ny;row++)for(let col=0;col<nx;col++){const a=row*(nx+1)+col,b=a+1,c=a+nx+1,d=c+1;indices.push(a,b,d,a,d,c);}
const input={count:positions.length/3,positions:new Float32Array(positions),colors:new Float32Array(colors),indices:new Uint32Array(indices)};
context.input=input;vm.runInContext('uploadMesh(input);',context);
await writeFile(out+'/grid-input.json',JSON.stringify({columns:nx,rows:ny,...Object.fromEntries(Object.entries(input).map(([key,value])=>[key,typeof value==='number'?value:Array.from(value)]))},null,2));
const canonical=triangle=>[...triangle].sort((a,b)=>a-b).join(',');
const inputIds=new Map();for(let t=0;t<indices.length/3;t++)inputIds.set(canonical(indices.slice(t*3,t*3+3)),t);
function components(items,neighbors){let groups=0;const seen=new Set();for(const item of items){if(seen.has(item))continue;groups++;const pending=[item];seen.add(item);while(pending.length){const current=pending.pop();for(const neighbor of neighbors(current)){if(!seen.has(neighbor)){seen.add(neighbor);pending.push(neighbor);}}}}return groups;}
function measure(){
 const {start,count}=surfaceGeometry.drawRange,stored=surfaceGeometry.index.array;
 assert.equal(start,0);assert.equal(count%3,0);
 const triangles=Array.from({length:count/3},(_,i)=>Array.from(stored.slice(i*3,i*3+3)));
 const edgeFaces=new Map(),vertices=new Set(),vertexNeighbors=new Map(),faceNeighbors=triangles.map(()=>new Set());
 let area=0,degenerate=0;
 const p=surfaceGeometry.getAttribute('position').array;
 for(let i=0;i<triangles.length;i++){
  const tri=triangles[i],a=new THREE.Vector3().fromArray(p,tri[0]*3),b=new THREE.Vector3().fromArray(p,tri[1]*3),c=new THREE.Vector3().fromArray(p,tri[2]*3);
  const faceArea=new THREE.Vector3().subVectors(b,a).cross(new THREE.Vector3().subVectors(c,a)).length()/2;area+=faceArea;if(faceArea===0)degenerate++;
  for(const v of tri){vertices.add(v);if(!vertexNeighbors.has(v))vertexNeighbors.set(v,new Set());}
  for(let k=0;k<3;k++){const a=tri[k],b=tri[(k+1)%3],key=[a,b].sort((x,y)=>x-y).join(',');const adjacent=edgeFaces.get(key)??[];adjacent.push(i);edgeFaces.set(key,adjacent);vertexNeighbors.get(a).add(b);vertexNeighbors.get(b).add(a);}
 }
 for(const faces of edgeFaces.values())for(const a of faces)for(const b of faces)if(a!==b)faceNeighbors[a].add(b);
 const ids=triangles.map(tri=>inputIds.get(canonical(tri)));assert.ok(ids.every(id=>id!==undefined));
 const drawnIndices=Array.from(stored.slice(start,start+count));
 const exactInputIndexOrder=drawnIndices.length===indices.length&&drawnIndices.every((value,i)=>value===indices[i]);
 const noSourceFaceOmitted=ids.length===indices.length/3&&new Set(ids).size===indices.length/3;
 return {exactInputIndexOrder,noSourceFaceOmitted,drawRange:{start,count},retainedTriangles:triangles.length,retainedTriangleFraction:triangles.length/(indices.length/3),retainedAreaSquareMetres:area,retainedAreaFraction:area/(nx*ny),deletedInputTriangles:indices.length/3-triangles.length,edgeConnectedFaceComponents:components(triangles.map((_,i)=>i),i=>faceNeighbors[i]),vertexConnectedComponents:components(vertices,v=>vertexNeighbors.get(v)),usedVertices:vertices.size,uniqueEdges:edgeFaces.size,boundaryEdges:[...edgeFaces.values()].filter(faces=>faces.length===1).length,sharedEdges:[...edgeFaces.values()].filter(faces=>faces.length===2).length,degenerateTriangles:degenerate,retainedInputTriangleIds:ids};
}
const samples=[];
for(const lodIndex of [1,2,4]){
 context.targetLod=lodIndex;vm.runInContext('lodIndex=targetLod;surfaceGeometry.setDrawRange(0,meshDrawCount());',context);
 const fraction=vm.runInContext('LADDER[lodIndex].fraction',context);
 samples.push({lodIndex,fraction,...measure()});
}
if(expectFixed){
 assert.deepEqual(samples.map(s=>s.retainedTriangles),[128,128,128]);
 assert.deepEqual(samples.map(s=>s.retainedAreaSquareMetres),[64,64,64]);
 assert.deepEqual(samples.map(s=>s.edgeConnectedFaceComponents),[1,1,1]);
 assert.deepEqual(samples.map(s=>s.vertexConnectedComponents),[1,1,1]);
 for(const sample of samples){assert.equal(sample.exactInputIndexOrder,true);assert.equal(sample.noSourceFaceOmitted,true);assert.equal(sample.deletedInputTriangles,0);assert.equal(sample.degenerateTriangles,0);assert.deepEqual(sample.retainedInputTriangleIds,Array.from({length:128},(_,i)=>i));}
}else{
 assert.deepEqual(samples.map(s=>s.retainedTriangles),[128,64,32]);
 assert.deepEqual(samples.map(s=>s.retainedAreaSquareMetres),[64,32,16]);
 assert.deepEqual(samples.map(s=>s.edgeConnectedFaceComponents),[1,64,32]);
 assert.deepEqual(samples.map(s=>s.vertexConnectedComponents),[1,1,4]);
 assert.ok(samples[1].retainedInputTriangleIds.every(i=>i%2===0));assert.ok(samples[2].retainedInputTriangleIds.every(i=>i%4===0));
}
assert.equal(surfaceGeometry.index.array.length>=input.indices.length,true);
assert.equal(vm.runInContext('meshTris',context),128);
assert.equal(vm.runInContext('indexUsed',context),384);
const lineOf=needle=>source.slice(0,source.indexOf(needle)).split('\n').length;
const report={passed:true,scope:'CPU witness executing exact extracted production uploadMesh/scatterOrder/meshDrawCount and pooled index/attribute helpers with real Three.js BufferGeometry; synthetic connected grid, no renderer/GPU/pixels/headset.',expectFixed,executedAtUtc:new Date().toISOString(),runtime:{node:process.version,threeRevision:THREE.REVISION},source:{path,commit:pinned.commit,sourceSha256,origin:pinned.origin,lines:{scatterOrder:lineOf('function scatterOrder('),uploadMesh:lineOf('function uploadMesh('),meshDrawCount:lineOf('function meshDrawCount()'),applyLodDrawRange:lineOf('  surfaceGeometry.setDrawRange(0, meshDrawCount());\n  applyFxTier')}},input:{columns:nx,rows:ny,vertices:input.count,triangles:indices.length/3,areaSquareMetres:nx*ny,triangulation:'two consistently wound triangles per 1m square, row-major'},stored:{triangles:128,indexUsed:384,indexCapacity:surfaceGeometry.index.array.length},samples,findings:expectFixed?['All three LOD fractions preserve every source face in exact original index order, 64 square metres and one edge-connected component.','This verifies the pinned production topology fix on the synthetic connected-grid CPU fixture; it does not prove renderer, hardware, frame budget, headset fidelity or simulator PNG causality.']:['Half and quarter LODs retain every second/fourth source face via reordered prefix; removed faces are not replaced by coarser connected faces.','On this connected grid, area falls to50%/25% and edge-connected surface fractures into64/32 components.','Simulator PNG causality remains unproven without its actual capture LOD/input/conditions.']};
await writeFile(outputPath,JSON.stringify(report,null,2)+'\n');
console.log(JSON.stringify({passed:report.passed,source:report.source,input:report.input,stored:report.stored,samples:samples.map(({retainedInputTriangleIds,...sample})=>sample),findings:report.findings},null,2));
