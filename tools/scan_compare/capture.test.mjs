import test from 'node:test';
import assert from 'node:assert/strict';
import {prepareComparisonCapture,applyComparisonMode} from './capture.mjs';
const fixture=()=>({
 points:5,positions:new Float32Array([0,0,-2,.02,0,-2,0,.02,-2,.02,.02,-2,1,0,-2]),
 colors:new Float32Array([1,0,0,0,1,0,0,0,1,1,1,1,1,1,0]),
 patch:{positions:new Float32Array([0,0,-2,.02,0,-2,0,.02,-2,.02,.02,-2,1,0,-2,0,0,0]),
 uv:new Float32Array([.1,.9,.3,.9,.1,.7,.3,.7,.5,.5,0,0]),indices:new Uint32Array([0,2,1,1,2,3,0,1,4])}
});
test('small native faces reuse positions and colors without connecting a long edge',()=>{
 const data=fixture(),before=data.patch.indices.slice(),out=prepareComparisonCapture(data,.08);
 assert.deepEqual([...out.indices],[0,2,1,1,2,3]);
 assert.equal(out.positions,data.patch.positions);
 assert.deepEqual([...out.colors.slice(6,9)],[0,0,1]);
 assert.deepEqual([...out.colors.slice(12,15)],[1,1,0]);
 assert.deepEqual(data.patch.indices,before,'input topology stays untouched');
});
test('samples left uncovered by the chosen small triangles remain in the point subset',()=>{
 const out=prepareComparisonCapture(fixture(),.08);
 assert.deepEqual([...out.fallbackIndices],[4]);
 assert.equal(out.colors.length,18);
 assert.deepEqual([...out.colors.slice(15)],[0,0,0],'missing native slot has no invented color');
});
test('A/B/C switch only visibility, materials and point draw range',()=>{
 const position={id:'same-captured-position'},index={id:'uncovered-index'};
 const geometry={position,setIndex(v){this.index=v},setDrawRange(s,n){this.range=[s,n]}};
 const color={},texture={},mesh={geometry:{position},userData:{comparisonMaterials:{color,texture}}};
 const raw={count:5,fallbackCount:1,comparisonPointIndex:index,points:{geometry},patches:[mesh],setLayers(v){this.layers=v}};
 applyComparisonMode(raw,'points');assert.equal(geometry.index,null);assert.deepEqual(geometry.range,[0,5]);assert.equal(raw.layers.surfaces,false);
 applyComparisonMode(raw,'balanced');assert.equal(geometry.index,index);assert.equal(mesh.material,color);assert.deepEqual(geometry.range,[0,1]);
 applyComparisonMode(raw,'texture');assert.equal(mesh.material,texture);assert.equal(mesh.geometry.position,position);assert.equal(raw.layers.surfaces,true);
});
