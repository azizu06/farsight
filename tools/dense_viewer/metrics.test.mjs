import { test } from 'node:test';
import assert from 'node:assert/strict';
import { coverageMetrics } from './metrics.mjs';
test('coverage counts only scene pixels and enclosed 4px holes, not open gaps',()=>{
 const width=8,height=8,pixels=new Uint8Array(8*8*4);
 for(let i=0;i<64;i++)pixels.set([8,12,18,255],i*4);
 const fill=(x,y)=>pixels.set([220,80,20,255],(y*8+x)*4);
 for(let x=1;x<=6;x++)for(let y=1;y<=6;y++)if(x<=2||x>=5||y<=2||y>=5)fill(x,y);
 let m=coverageMetrics({pixels,width,height});assert.equal(m.hole_count,1);assert.equal(m.hole_pixels,4);assert.equal(m.filled_pixels,32);
 pixels.set([8,12,18,255],(3*8+1)*4);pixels.set([8,12,18,255],(3*8+2)*4);
 m=coverageMetrics({pixels,width,height});assert.equal(m.hole_count,0);
});
