// Synthetic packet exercises real decoder worker, calibrated mesh and texture.
// No physical phone, headset or motor claim.
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {chromium} from '../../quest/node_modules/playwright/index.mjs';
const browser=await chromium.launch({channel:'chrome',headless:true,args:['--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader']});
try{
 const page=await browser.newPage({viewport:{width:1000,height:700}}),requests=[],errors=[];
 page.on('request',r=>requests.push(r.url()));page.on('websocket',s=>{throw Error(`Unexpected socket ${s.url()}`);});page.on('pageerror',e=>errors.push(e.message));
 const packet=await readFile(process.argv[3]??'.cache/raw-fixture.bin');
 let read=false, shifted=false;
 await page.route('**/capture/frame.bin?*',r=>{if(shifted)return r.fulfill({status:204});if(read){shifted=true;const n=packet.readUInt32LE(0),h=JSON.parse(packet.subarray(4,4+n));h.frame_id=1;h.map_epoch=2;h.transform[12]=5;const text=Buffer.from(JSON.stringify(h)),prefix=Buffer.alloc(4);prefix.writeUInt32LE(text.length);return r.fulfill({body:Buffer.concat([prefix,text,packet.subarray(4+n)]),headers:{'X-Capture-Key':'fixture:2','X-Capture-Age-Ms':'0'}});}read=true;return r.fulfill({body:packet,contentType:'application/octet-stream',headers:{'X-Capture-Key':'fixture:1','X-Capture-Age-Ms':'0'}});});
 await page.goto(process.argv[2]??'http://127.0.0.1:8086/');
 await page.evaluate(()=>window.denseLab.setMode('raw'));
 await page.waitForFunction(()=>window.denseLab.stats().raw_points===48,{timeout:10000});
 await page.waitForFunction(()=>window.denseLab.stats().raw_frame===1);
 const followed=await page.evaluate(()=>window.denseLab.stats());assert.ok(followed.target[0]>4,'live view follows a moved/reset measured scan');
 await page.evaluate(()=>{window.denseLab.setFrozen(true);window.denseLab.pose(0);window.denseLab.setSize(.1);});
 await page.waitForTimeout(1200);
 const stats=await page.evaluate(()=>window.denseLab.stats()),coverage=await page.evaluate(()=>window.denseLab.coverage());
 // With a supported measured plane, coplanar points must not paint over its
 // interior texture; hiding the plane must restore the retained samples.
 const interior=()=>{window.denseLab.render();const c=window.denseLab.canvas,d=document.createElement('canvas');d.width=c.width;d.height=c.height;const x=d.getContext('2d');x.drawImage(c,0,0);return [...x.getImageData(Math.floor(c.width*.4),Math.floor(c.height*.4),Math.floor(c.width*.2),Math.floor(c.height*.2)).data];};
 const combined=await page.evaluate(interior);
 await page.evaluate(()=>window.denseLab.setLayers({points:false}));
 const meshOnly=await page.evaluate(interior);assert.deepEqual(combined,meshOnly,'mesh depth suppresses coplanar point overlay');
 await page.evaluate(()=>window.denseLab.setLayers({points:true,surfaces:false}));
 const pointsOnly=await page.evaluate(interior);assert.notDeepEqual(meshOnly,pointsOnly,'retained dots remain available without a surface');
 await page.evaluate(()=>window.denseLab.setLayers({surfaces:true}));
 assert.equal(stats.raw_triangles,70);assert.ok(coverage.coverage>.1,JSON.stringify(coverage));
 assert.equal(await page.locator('video').count(),0,'no video overlay can obscure local canvas');
 assert.equal(requests.some(u=>u.includes('/ws/control')||u.includes('/ws/macview-pose')),false);
 assert.deepEqual(errors,[]);
 await page.locator('#viewport').screenshot({path:'.cache/raw-fixture.png'});
 console.log(JSON.stringify({scope:'SYNTHETIC RGB-D desktop worker',stats,coverage,control_connections:0}));
}finally{await browser.close();}
