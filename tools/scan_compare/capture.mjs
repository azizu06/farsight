/** Display-only comparison of the native worker's already-supported faces.
 * No point positions, source packets or production admission rules are changed.
 */
export function prepareComparisonCapture(data,maxEdge=.08){
 const p=data.patch.positions,faces=[],covered=new Uint8Array(p.length/3),limit=maxEdge*maxEdge;
 for(let i=0;i<data.patch.indices.length;i+=3){
  const ids=[data.patch.indices[i],data.patch.indices[i+1],data.patch.indices[i+2]];
  const short=[[0,1],[1,2],[2,0]].every(([a,b])=>{const s=ids[a]*3,t=ids[b]*3;return (p[s]-p[t])**2+(p[s+1]-p[t+1])**2+(p[s+2]-p[t+2])**2<=limit;});
  if(short){faces.push(...ids);for(const id of ids)covered[id]=1;}
 }
 // In latest mode the worker packs every valid pixel in raster order. Missing
 // slots retain UV(0,0); every valid pixel has its calibrated half-pixel UV.
 const colors=new Float32Array(p.length),fallback=[];let point=0;
 for(let i=0;i<covered.length;i++)if(data.patch.uv[i*2]>0){
  colors.set(data.colors.subarray(point*3,point*3+3),i*3);
  if(!covered[i])fallback.push(point);point++;
 }
 return {positions:p,indices:new Uint32Array(faces),colors,fallbackIndices:new Uint32Array(fallback)};
}
export function applyComparisonMode(raw,mode){
 const all=mode==='points';
 raw.setLayers({surfaces:!all,points:true});
 raw.points.geometry.setIndex(all?null:raw.comparisonPointIndex??null);
 raw.points.geometry.setDrawRange(0,all?raw.count:raw.fallbackCount);
 for(const mesh of raw.patches){
  const materials=mesh.userData.comparisonMaterials;
  if(materials)mesh.material=mode==='balanced'?materials.color:materials.texture;
 }
}
