// Pip Scout's essential capture path: native same-frame RGB-D, calibrated world
// projection, measured 1cm association, observed grid topology, recent texture.
// This bounded first port uses a rolling lifetime for unsegmented moving bodies;
// it does not claim Pip Scout's planar compression or two-view retirement algorithm.
const cells=new Map();let identity='',lastFrame=-1;
const capacity=2_000_000;
self.onmessage=async({data})=>{
 try{
  const {buffer,ttl=30,history=true}=data,v=new DataView(buffer),n=v.getUint32(0,true);
  if(n<1||n>65536||n+4>buffer.byteLength)throw Error('Invalid RGB-D header');
  if(Date.now()>(data.deadline??Infinity))throw Error('RGB-D expired before worker decode');
  const h=JSON.parse(new TextDecoder().decode(new Uint8Array(buffer,4,n)));
  const id=JSON.stringify([h.session_id,h.map_epoch,h.map_generation]);
  if(h.tracking!=='normal')throw Error('Tracking is not normal');
  if(id!==identity){identity=id;cells.clear();lastFrame=-1;}
  if(h.frame_id<=lastFrame)throw Error('Duplicate/out-of-order capture');
  const w=h.depth.width,ht=h.depth.height,count=w*ht,start=4+n,jpegLength=h.image.jpeg_len;
  if(!Number.isInteger(count)||w<2||ht<2||w>640||ht>640||start+jpegLength+count*5!==buffer.byteLength)throw Error('Invalid native raster sizes');
  const read=i=>v.getFloat32(start+jpegLength+i*4,true);
  const confidence=new Uint8Array(buffer,start+jpegLength+count*4,count);
  const jpeg=buffer.slice(start,start+jpegLength);
  const bitmap=await createImageBitmap(new Blob([jpeg],{type:'image/jpeg'}));
  if(bitmap.width!==h.image.width||bitmap.height!==h.image.height)throw Error('RGB dimensions mismatch');
  if(Date.now()>(data.deadline??Infinity)){bitmap.close();throw Error('RGB-D expired during image decode');}
  const canvas=new OffscreenCanvas(w,ht),ctx=canvas.getContext('2d',{willReadFrequently:true});
  ctx.drawImage(bitmap,0,0,w,ht);const rgb=ctx.getImageData(0,0,w,ht).data;
  const k=h.image.intrinsics,m=h.transform;
  if(k?.length!==9||m?.length!==16||![...k,...m].every(Number.isFinite)||k[0]<=0||k[4]<=0)throw Error('Invalid calibration');
  const fx=k[0]*w/bitmap.width,fy=k[4]*ht/bitmap.height,cx=k[6]*w/bitmap.width,cy=k[7]*ht/bitmap.height;
  const positions=new Float32Array(count*3),uv=new Float32Array(count*2),valid=new Uint8Array(count),depths=new Float32Array(count),now=Date.now();
  const latestPositions=history?null:new Float32Array(count*3),latestColors=history?null:new Float32Array(count*3);let latestCount=0;
  if(!history)cells.clear();
  const linear=x=>x<=.04045?x/12.92:((x+.055)/1.055)**2.4;
  for(let i=0;i<count;i++){
   const z=depths[i]=read(i),x=i%w,y=Math.floor(i/w);
   if(confidence[i]<1||confidence[i]>2||!Number.isFinite(z)||z<.15||z>8)continue;
   const a=(x-cx)*z/fx,b=-(y-cy)*z/fy,c=-z;
   const p=[m[0]*a+m[4]*b+m[8]*c+m[12],m[1]*a+m[5]*b+m[9]*c+m[13],m[2]*a+m[6]*b+m[10]*c+m[14]];
   positions.set(p,i*3);uv.set([(x+.5)/w,1-(y+.5)/ht],i*2);valid[i]=1;
   const color=[linear(rgb[i*4]/255),linear(rgb[i*4+1]/255),linear(rgb[i*4+2]/255)];
   if(history){const key=p.map(x=>Math.floor(x/.01)).join(',');cells.delete(key);cells.set(key,{p,color,at:now});}
   else{latestPositions.set(p,latestCount*3);latestColors.set(color,latestCount*3);latestCount++;}
  }
  // Only triangles supported by neighboring measured depth pixels; refuse depth
  // discontinuities. No unorganized-cell triangulation or hole filling.
  const indices=[],covered=history?null:new Uint8Array(count);
  const triangle=(a,b,c)=>{
   if(!valid[a]||!valid[b]||!valid[c])return;
   const lo=Math.min(depths[a],depths[b],depths[c]),hi=Math.max(depths[a],depths[b],depths[c]);
   if(hi-lo>Math.max(.025,lo*.04))return;
   indices.push(a,b,c);
   if(covered)covered[a]=covered[b]=covered[c]=1;
  };
  for(let y=0;y<ht-1;y++)for(let x=0;x<w-1;x++){const a=y*w+x,b=a+1,c=a+w,d=c+1;triangle(a,c,b);triangle(b,c,d);}
  // Preserve every valid latest-grid measurement; draw only the ones that
  // belong to none of the already accepted native-depth triangles.
  const fallback=[];let pointIndex=0;
  if(!history)for(let i=0;i<count;i++)if(valid[i]){if(!covered[i])fallback.push(pointIndex);pointIndex++;}
  const fallbackIndices=history?null:new Uint32Array(fallback);
  if(history)for(const [key,value]of cells){if(now-value.at>ttl*1000||cells.size>capacity)cells.delete(key);else break;}
  const cloudPositions=history?new Float32Array(cells.size*3):latestPositions.subarray(0,latestCount*3);
  const colors=history?new Float32Array(cells.size*3):latestColors.subarray(0,latestCount*3);let cursor=0;
  if(history)for(const value of cells.values()){cloudPositions.set(value.p,cursor);colors.set(value.color,cursor);cursor+=3;}
  const textureCanvas=new OffscreenCanvas(bitmap.width,bitmap.height),textureContext=textureCanvas.getContext('2d');
  textureContext.translate(0,bitmap.height);textureContext.scale(1,-1);textureContext.drawImage(bitmap,0,0);bitmap.close();const textureImage=textureCanvas.transferToImageBitmap();
  if(Date.now()>(data.deadline??Infinity)){textureImage.close();throw Error('RGB-D expired during projection');}
  lastFrame=h.frame_id;
  const faces=new Uint32Array(indices);
  self.postMessage({identity,frame:h.frame_id,mode:history?'history':'latest',max_points:history?capacity:count,point_transfer_bytes:cloudPositions.buffer.byteLength+colors.buffer.byteLength,points:cloudPositions.length/3,positions:cloudPositions,colors,fallback_indices:fallbackIndices,
   patch:{positions,uv,indices:faces,image:textureImage,at:now,transform:m},ttl,source:h.capture_source??'UNVERIFIED RAW CAPTURE',processing_ms:Date.now()-now},
   [cloudPositions.buffer,colors.buffer,positions.buffer,uv.buffer,faces.buffer,textureImage,...(fallbackIndices?[fallbackIndices.buffer]:[])]);
 }catch(e){self.postMessage({error:String(e)});}
};
