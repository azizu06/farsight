// Revisit only the region observed by the current native RGB-D frame. Camera
// matrices are column-major ARKit/GL camera-to-world transforms, looking -Z.
export class DepthObservation {
 constructor({width,height,fx,fy,cx,cy,transform,depths,valid,indices}){
  this.width=width;this.height=height;this.fx=fx;this.fy=fy;this.cx=cx;this.cy=cy;this.transform=transform;
  this.stride=Math.max(1,Math.ceil(width/128),Math.ceil(height/96));
  this.pooledWidth=Math.ceil(width/this.stride);
  this.depth=new Float32Array(this.pooledWidth*Math.ceil(height/this.stride)).fill(Infinity);
  // An unknown native sample invalidates its block. Taking the minimum also
  // keeps narrow foreground objects from being erased by background samples.
  for(let y=0;y<height;y++)for(let x=0;x<width;x++){
   const i=y*width+x,j=Math.floor(y/this.stride)*this.pooledWidth+Math.floor(x/this.stride);
   this.depth[j]=Math.min(this.depth[j],valid[i]?depths[i]:0);
  }
  // Each native quad has two independently accepted measured triangles.
  this.mesh=new Uint8Array((width-1)*(height-1));
  for(let i=0;i<indices.length;i+=3){
   const a=indices[i],b=indices[i+1],c=indices[i+2];
   const x=Math.min(a%width,b%width,c%width),y=Math.floor(Math.min(a,b,c)/width),corner=y*width+x;
   this.mesh[y*(width-1)+x]|=(a===corner||b===corner||c===corner)?1:2;
  }
 }
 covers(u,v){
  // World vertices are transferred as Float32; allow their subpixel roundoff
  // at native image borders without expanding the refreshed field of view.
  const epsilon=1e-4;
  if(u<-epsilon||v<-epsilon||u>this.width-1+epsilon||v>this.height-1+epsilon)return false;
  u=Math.max(0,Math.min(u,this.width-1));v=Math.max(0,Math.min(v,this.height-1));
  const x=Math.min(Math.floor(u),this.width-2),y=Math.min(Math.floor(v),this.height-2);
  const triangles=this.mesh[y*(this.width-1)+x],diagonal=u-x+v-y;
  return Boolean((diagonal<=1+1e-6&&(triangles&1))||(diagonal>=1-1e-6&&(triangles&2)));
 }
 classifyPoint(worldX,worldY,worldZ,replacement=true){
   // Bit 1: contradicted old foreground. Bit 2: currently observed and covered
   // by a replacement triangle. Occluded, unknown and out-of-view stay zero.
   const m=this.transform,dx=worldX-m[12],dy=worldY-m[13],dz=worldZ-m[14];
   const x=m[0]*dx+m[1]*dy+m[2]*dz,y=m[4]*dx+m[5]*dy+m[6]*dz,z=-(m[8]*dx+m[9]*dy+m[10]*dz);
   if(!(z>0))return 0;
   const u=this.fx*x/z+this.cx,v=this.cy-this.fy*y/z;
   if(!(u>=-.5&&u<this.width-.5&&v>=-.5&&v<this.height-.5))return 0;
   const nativeX=Math.floor(u+.5),nativeY=Math.floor(v+.5);
   const observed=this.depth[Math.floor(nativeY/this.stride)*this.pooledWidth+Math.floor(nativeX/this.stride)];
   if(!(observed>0))return 0;
   // Depth correspondence is two existing 1cm dense cells plus the compact
   // codec's 1mm allowance; the mesh's broader triangle-join rule is separate.
   const tolerance=.021;let selected=0;
   if(z<observed-tolerance)selected|=1;
   if(replacement&&z<=observed+tolerance&&this.covers(u,v))selected|=2;
   return selected;
 }
 classify(positions){
  const selected=new Uint8Array(positions.length/3);
  for(let i=0;i<selected.length;i++){
   selected[i]=this.classifyPoint(positions[i*3],positions[i*3+1],positions[i*3+2]);
  }
  return selected;
 }
}

// Stage voxel retirement and capture updates without cloning a potentially
// two-million-cell Map. Renderer arrays represent the prospective state;
// neither the retained cells nor their order change until commit.
export function preparePointHistory(cells,incoming,observation,{reset=false,now,ttl=30,capacity=2_000_000}={}){
 const removed=new Set();
 if(!reset)for(const [key,value]of cells){
  if(incoming.has(key)||now-value.at>ttl*1000||(observation.classifyPoint(value.p[0],value.p[1],value.p[2],false)&1))removed.add(key);
 }
 let retained=reset?0:cells.size-removed.size,excess=retained+incoming.size-capacity;
 if(excess>0&&!reset)for(const key of cells.keys()){
  if(removed.has(key))continue;
  removed.add(key);retained--;if(--excess<=0)break;
 }
 const positions=new Float32Array((retained+incoming.size)*3),colors=new Float32Array(positions.length);let cursor=0;
 const append=value=>{positions.set(value.p,cursor);colors.set(value.color,cursor);cursor+=3;};
 if(!reset)for(const [key,value]of cells)if(!removed.has(key))append(value);
 for(const value of incoming.values())append(value);
 return {removed,incoming,reset,positions,colors};
}
export function commitPointHistory(cells,prepared){
 if(prepared.reset)cells.clear();else for(const key of prepared.removed)cells.delete(key);
 for(const [key,value]of prepared.incoming)cells.set(key,value);
}

export class RevisitSurfaces {
 constructor({maxPatches=12,maxVertices=500_000,maxTriangles=1_000_000}={}){
  this.maxPatches=maxPatches;this.maxVertices=maxVertices;this.maxTriangles=maxTriangles;
  this.identity='';this.patches=[];
 }
 // Prepare is deliberately side-effect free. The worker commits only after
 // its final freshness deadline, so an expired decode cannot trim old meshes.
 prepare({identity,id,positions,indices,at,observation,history=true,ttl=30,now=at}){
  const previous=identity===this.identity?this.patches:[],updates=[],next=[];
  if(history)for(const old of previous){
   if(now-old.at>ttl*1000)continue;
   const selected=observation.classify(old.positions);let retained=null,cursor=0;
   for(let i=0;i<old.indices.length;i+=3){
    const a=selected[old.indices[i]],b=selected[old.indices[i+1]],c=selected[old.indices[i+2]];
    if((a|b|c)&1||((a&b&c)&2)){
     if(!retained){retained=new Uint32Array(old.indices.length);retained.set(old.indices.subarray(0,i));cursor=i;}
    }else if(retained){retained[cursor++]=old.indices[i];retained[cursor++]=old.indices[i+1];retained[cursor++]=old.indices[i+2];}
   }
   if(!retained)next.push(old);
   else if(!cursor)continue;
   else{
    const refreshed=retained.slice(0,cursor);
    // Returned buffers are transferred to the renderer. Keep a separate copy.
    next.push({...old,indices:refreshed.slice()});updates.push({id:old.id,indices:refreshed});
   }
  }
  if(indices.length)next.push({id,positions:positions.slice(),indices:indices.slice(),at});
  let vertices=next.reduce((n,p)=>n+p.positions.length/3,0),triangles=next.reduce((n,p)=>n+p.indices.length/3,0);
  while(next.length&&(vertices>this.maxVertices||triangles>this.maxTriangles||next.length>this.maxPatches)){
   const old=next.shift();vertices-=old.positions.length/3;triangles-=old.indices.length/3;
  }
  const kept=new Set(next.map(p=>p.id));
  return {identity,next,updates:updates.filter(p=>kept.has(p.id)),removed:previous.filter(p=>!kept.has(p.id)).map(p=>p.id)};
 }
 commit(prepared){this.identity=prepared.identity;this.patches=prepared.next;}
}
