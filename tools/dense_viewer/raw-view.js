import * as THREE from 'three';
export class RawView {
 constructor(root,onBounds,{endpoint='/capture/frame.bin',onChange=()=>{},history=true,surfaceHistory=history,pollInterval=500}={}){
  this.endpoint=endpoint;this.onChange=onChange;
  this.history=Boolean(history);this.surfaceHistory=Boolean(surfaceHistory);this.pollInterval=Math.min(4000,Math.max(125,Number(pollInterval)||500));this.pointTransfer=0;
  this.showSurfaces=true;this.showPoints=true;
  this.group=new THREE.Group();root.add(this.group);this.onBounds=onBounds;
  this.points=new THREE.Points(new THREE.BufferGeometry(),new THREE.PointsMaterial({size:.012,vertexColors:true,toneMapped:false}));
  this.points.material.onBeforeCompile=shader=>{
   shader.vertexShader=shader.vertexShader.replace('#include <logdepthbuf_vertex>','gl_PointSize = clamp(gl_PointSize, 1.25, 64.0);\n#include <logdepthbuf_vertex>');
   shader.fragmentShader=shader.fragmentShader.replace('#include <clipping_planes_fragment>','#include <clipping_planes_fragment>\nif(distance(gl_PointCoord,vec2(0.5))>0.5)discard;');
  };
  // Draw measured surfaces first; their depth offsets suppress coplanar dots.
  // Points remain retained and reappear wherever no recent surface covers them.
  this.points.renderOrder=2;this.points.material.depthFunc=THREE.LessDepth;
  this.group.add(this.points);this.patches=[];this.identity='';this.count=0;this.error=null;this.lastGood=0;this.bytes=0;this.received=0;this.busy=false;this.active=false;this.cursor='';this.ttl=this.history?30:2;this.surfaceTtl=this.surfaceHistory?30:2;this.controller=null;
  this.worker=new Worker(new URL('./raw-fusion.worker.js',import.meta.url),{type:'module'});
  this.worker.onmessage=({data})=>{
   this.busy=false;
   if(data.error){this.error=data.error;return;}
   if(!this.active){data.patch.image.close();return;}
   if(data.identity!==this.identity){this.clear();this.identity=data.identity;}
   this.error=null;this.source=data.source;this.count=data.points;this.lastGood=Date.now();this.age=this.pendingAge+Date.now()-this.pendingAt;this.frame=data.frame;this.processing=data.processing_ms;this.pointTransfer=data.point_transfer_bytes;
   let geometry=this.points.geometry;
   if(geometry.getAttribute('position')?.count<data.max_points){geometry.dispose();geometry=new THREE.BufferGeometry();this.points.geometry=geometry;}
   for(const [name,values]of [['position',data.positions],['color',data.colors]]){
    let a=geometry.getAttribute(name);
    if(!a||a.count<data.max_points){a=new THREE.BufferAttribute(new Float32Array(data.max_points*3),3).setUsage(THREE.DynamicDrawUsage);geometry.setAttribute(name,a);}
    a.array.set(values);a.clearUpdateRanges();a.addUpdateRange(0,values.length);a.needsUpdate=true;
   }
   geometry.setDrawRange(0,data.points);geometry.boundingBox=new THREE.Box3().setFromArray(data.positions);geometry.boundingSphere=geometry.boundingBox.getBoundingSphere(new THREE.Sphere());
   const patch=data.patch,g=new THREE.BufferGeometry();g.setAttribute('position',new THREE.BufferAttribute(patch.positions,3));g.setAttribute('uv',new THREE.BufferAttribute(patch.uv,2));g.setIndex(new THREE.BufferAttribute(patch.indices,1));
   const texture=new THREE.Texture(patch.image);texture.colorSpace=THREE.SRGBColorSpace;texture.flipY=false;texture.anisotropy=4;texture.needsUpdate=true;
   // ImageBitmap ignores texture.flipY; UV v is 1-y while bitmap is top-down,
   // so use a worker-flipped bitmap (see worker) for calibrated texture alignment.
   const mesh=new THREE.Mesh(g,new THREE.MeshBasicMaterial({map:texture,side:THREE.DoubleSide,toneMapped:false,polygonOffset:true,polygonOffsetFactor:-1,polygonOffsetUnits:-1}));
   if(!this.surfaceHistory){for(const old of this.patches)this.remove(old);this.patches=[];}
   const last=this.patches.at(-1),m=patch.transform;
   if(last){const old=last.userData.transform;const distance=Math.hypot(m[12]-old[12],m[13]-old[13],m[14]-old[14]);const cosine=m[8]*old[8]+m[9]*old[9]+m[10]*old[10];if(distance<.1&&cosine>Math.cos(8*Math.PI/180)){this.patches.pop();this.remove(last);}}
   mesh.renderOrder=1;mesh.visible=this.showSurfaces;
   mesh.userData={transform:m,at:patch.at,triangles:patch.indices.length/3,vertices:patch.positions.length/3};this.group.add(mesh);this.patches.push(mesh);this.expire();
   this.onBounds?.(geometry.boundingBox);this.onChange();
  };
  this.worker.onerror=e=>{this.busy=false;this.error=e.message;};
 }
 setLayers({surfaces=this.showSurfaces,points=this.showPoints}={}){this.showSurfaces=Boolean(surfaces);this.showPoints=Boolean(points);this.points.visible=this.showPoints;for(const mesh of this.patches)mesh.visible=this.showSurfaces;}
 clearPoints(){this.count=0;this.points.geometry.setDrawRange(0,0);this.onChange();}
 clear(){for(const mesh of this.patches)this.remove(mesh);this.patches=[];this.clearPoints();}
 remove(mesh){this.group.remove(mesh);mesh.geometry.dispose();mesh.material.map.image.close();mesh.material.map.dispose();mesh.material.dispose();this.onChange();}
 expire(){let triangles=this.patches.reduce((n,p)=>n+p.userData.triangles,0),vertices=this.patches.reduce((n,p)=>n+p.userData.vertices,0);
  while(this.patches.length&&(Date.now()-this.patches[0].userData.at>this.surfaceTtl*1000||triangles>1_000_000||vertices>500_000||this.patches.length>12)){
   const p=this.patches.shift();triangles-=p.userData.triangles;vertices-=p.userData.vertices;this.remove(p);
  }
 }
 async update(enabled,frozen){
  this.group.visible=enabled;this.active=enabled;
  if(!enabled){this.controller?.abort();return;}if(frozen)return;
  this.expire();
  // Texture history and point history are independent. The Mac already has
  // its dense point receiver; keep only a native grid here while recent RGB-D
  // surfaces remain available after the camera turns away.
  if(this.lastGood&&Date.now()-this.lastGood+(this.age||0)>this.ttl*1000){this.clearPoints();this.lastGood=0;}
  if(this.busy||Date.now()-this.received<this.pollInterval)return;
  this.busy=true;this.received=Date.now();
  this.controller=new AbortController();
  try{const reply=await fetch(`${this.endpoint}?after=${encodeURIComponent(this.cursor)}`,{signal:AbortSignal.any([this.controller.signal,AbortSignal.timeout(4000)])});
   if(reply.status===204){this.busy=false;return;}
   if(!reply.ok)throw Error(`Raw RGB-D ${reply.status} (service-owner endpoint mount required)`);
   const b=await reply.arrayBuffer();if(b.byteLength>8*1024*1024)throw Error('RGB-D exceeds 8MiB');
   this.cursor=reply.headers.get('X-Capture-Key')??'';this.bytes+=b.byteLength;const sourceAge=reply.headers.get('X-Capture-Age-Ms');
   const age=Number(sourceAge)+Date.now()-this.received;
   if(sourceAge===null||Number(sourceAge)<0||!Number.isFinite(age)||age>2000)throw Error('Raw RGB-D age missing, invalid or expired during transfer');
   this.pendingAge=age;this.pendingAt=Date.now();
   this.worker.postMessage({buffer:b,ttl:this.ttl,history:this.history,deadline:Date.now()+2000-age},[b]);
  }catch(e){this.error=String(e);this.busy=false;}
 }
 stats(){return {raw_mode:this.history?'history':'latest',raw_surface_history:this.surfaceHistory,raw_surface_ttl_s:this.surfaceTtl,raw_oldest_surface_age_ms:this.patches.length?Date.now()-this.patches[0].userData.at:null,raw_poll_interval_ms:this.pollInterval,raw_point_capacity:this.points.geometry.getAttribute('position')?.count??0,raw_point_transfer_bytes:this.pointTransfer,raw_patches:this.patches.length,raw_splat_m:this.points.material.size,raw_pixel_floor:1.25,raw_surfaces:this.showSurfaces,raw_point_overlay:this.showPoints,raw_source:this.source??null,raw_points:this.count,raw_triangles:this.patches.reduce((n,p)=>n+p.userData.triangles,0),raw_frame:this.frame??null,raw_identity:this.identity,raw_age_ms:this.lastGood?Date.now()-this.lastGood+(this.age||0):null,raw_bytes:this.bytes,raw_worker_ms:this.processing??null,raw_error:this.error,raw_ttl_s:this.ttl};}
 dispose(){this.controller?.abort();this.worker.terminate();this.clear();this.points.geometry.dispose();this.points.material.dispose();}
}
