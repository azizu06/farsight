/** Screen-space diagnostics, not geometry completeness. Background is RGB 8/12/18.
 * Holes are enclosed background components >=4 px inside the foreground bbox;
 * open gaps to the background are excluded, so coverage must be read alongside it.
 */
export function coverageMetrics({pixels,width,height}) {
  const mask=new Uint8Array(width*height);
  let filled=0,minX=width,minY=height,maxX=-1,maxY=-1;
  for(let i=0;i<mask.length;i++) {
    const p=i*4;
    if(Math.abs(pixels[p]-8)+Math.abs(pixels[p+1]-12)+Math.abs(pixels[p+2]-18)>24){
      mask[i]=1;filled++;const x=i%width,y=Math.floor(i/width);
      minX=Math.min(minX,x);minY=Math.min(minY,y);maxX=Math.max(maxX,x);maxY=Math.max(maxY,y);
    }
  }
  let holes=0,holePixels=0;
  const visited=new Uint8Array(mask.length),queue=new Int32Array(mask.length);
  for(let y=minY;y<=maxY;y++)for(let x=minX;x<=maxX;x++){
    const start=y*width+x;if(mask[start]||visited[start])continue;
    let head=0,tail=1,border=false;queue[0]=start;visited[start]=1;
    while(head<tail){const i=queue[head++],px=i%width,py=Math.floor(i/width);
      if(px===minX||px===maxX||py===minY||py===maxY)border=true;
      for(const [nx,ny] of [[px-1,py],[px+1,py],[px,py-1],[px,py+1]]){
        if(nx<minX||nx>maxX||ny<minY||ny>maxY)continue;
        const n=ny*width+nx;if(!mask[n]&&!visited[n]){visited[n]=1;queue[tail++]=n;}
      }
    }
    if(!border&&tail>=4){holes++;holePixels+=tail;}
  }
  const area=maxX<minX?0:(maxX-minX+1)*(maxY-minY+1);
  return {coverage:filled/(width*height),object_coverage:area?filled/area:0,hole_count:holes,hole_pixels:holePixels,filled_pixels:filled,width,height};
}
