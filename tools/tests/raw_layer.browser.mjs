// Built Mac layer contract test; synthetic native packet, no rig or controls.
import assert from 'node:assert/strict';
import {createServer} from 'node:http';
import {readFile} from 'node:fs/promises';
import {chromium} from '../../quest/node_modules/playwright/index.mjs';
const packet=await readFile('.cache/raw-fixture.bin');let reads=0,allowed=1,calls=0;
function frame(index){
 const n=packet.readUInt32LE(0),header=JSON.parse(packet.subarray(4,4+n));
 header.frame_id=index===2?0:index;header.transform[12]=index*5;if(index===2)header.map_epoch=2;
 const text=Buffer.from(JSON.stringify(header)),prefix=Buffer.alloc(4);prefix.writeUInt32LE(text.length);
 const result=Buffer.concat([prefix,text,packet.subarray(4+n)]);
 if(index===1){const start=4+text.length+header.image.jpeg_len,count=header.depth.width*header.depth.height;
  result[start+count*4]=0; // Confidence-zero sample is omitted.
  result.writeFloatLE(5,start+Math.floor(count/2)*4); // Valid depth step must not be bridged.
 }
 return result;
}
const html=`<script type="module">
import * as THREE from '/quest/node_modules/three/build/three.module.js';
import {rawTextureLayer} from '/layer/raw-texture-layer.mjs';
const root=new THREE.Group(),scene=new THREE.Scene(),camera=new THREE.PerspectiveCamera(60,1,.01,20),renderer=new THREE.WebGLRenderer();scene.add(root);document.body.append(renderer.domElement);camera.position.set(0,0,2);
let dirtied=0;const layer=rawTextureLayer();layer.init({root,requestFrame:()=>dirtied++});
let disposed=false;renderer.setAnimationLoop(()=>{if(!disposed){layer.poll();renderer.render(scene,camera);}});
window.layerTest={enable:()=>layer.setEnabled(true),disable:()=>layer.setEnabled(false),stats:()=>({...layer.stats(),dirtied,children:root.children.length}),dispose:()=>{disposed=true;layer.dispose();renderer.setAnimationLoop(null);renderer.dispose();return root.children.length;}};
</script>`;
const server=createServer(async(req,res)=>{
 try{
  const path=new URL(req.url,'http://localhost').pathname;
  if(path==='/'){res.setHeader('Content-Type','text/html');return res.end(html);}
  if(path==='/phone/frame.bin'){calls++;if(reads>=allowed){res.writeHead(204);return res.end();}const index=reads++;res.setHeader('X-Capture-Key',`synthetic:${index}`);res.setHeader('X-Capture-Age-Ms','0');return res.end(frame(index));}
  const permitted=/^\/layer\/(raw-texture-layer\.mjs|assets\/raw-fusion\.worker-[\w-]+\.js)$/.test(path)||/^\/quest\/node_modules\/three\/build\/three\.(module|core)\.js$/.test(path);
  if(!permitted){res.writeHead(404);return res.end();}
  res.setHeader('Content-Type','text/javascript');res.end(await readFile(path.startsWith('/layer/')?'.cache/dense-layer-dist/'+path.slice(7):'.'+path));
 }catch(e){res.writeHead(500);res.end(String(e));}
});await new Promise(r=>server.listen(0,'127.0.0.1',r));
const browser=await chromium.launch({channel:'chrome',headless:true,args:['--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader']});
try{
 const page=await browser.newPage(),errors=[];page.on('pageerror',e=>errors.push(String(e)));page.on('websocket',s=>{throw Error(s.url());});
 await page.goto(`http://127.0.0.1:${server.address().port}/`);await page.waitForFunction(()=>window.layerTest);
 await page.waitForTimeout(600);assert.equal(calls,0,'default-off layer makes no raw request');
 assert.equal(await page.evaluate(()=>window.layerTest.stats().raw_enabled),false);
 await page.evaluate(()=>window.layerTest.enable());
 await page.waitForFunction(()=>window.layerTest?.stats().raw_points===48,null,{timeout:10000}).catch(async error=>{
  console.error(JSON.stringify({errors,calls,reads,stats:await page.evaluate(()=>window.layerTest.stats())}));throw error;
 });
 const stats=await page.evaluate(()=>window.layerTest.stats());assert.equal(stats.raw_triangles,70);assert.ok(stats.dirtied>0);assert.equal(stats.children,1);
 assert.equal(stats.raw_mode,'latest');assert.equal(stats.raw_point_capacity,48);assert.equal(stats.raw_point_transfer_bytes,48*24);assert.equal(stats.raw_poll_interval_ms,125);
 allowed=2;await page.waitForFunction(()=>window.layerTest.stats().raw_frame===1);
 const moved=await page.evaluate(()=>window.layerTest.stats());assert.equal(moved.raw_points,47,'latest valid grid replaces prior viewpoint instead of accumulating it');
 assert.ok(moved.raw_triangles<70,'confidence/depth discontinuities remove unsupported faces');assert.equal(moved.raw_patches,1);assert.equal(moved.raw_point_capacity,48);
 allowed=3;await page.waitForFunction(()=>window.layerTest.stats().raw_identity==='["test",2,1]');
 const reset=await page.evaluate(()=>window.layerTest.stats());assert.equal(reset.raw_points,48);assert.equal(reset.raw_frame,0);assert.equal(reset.raw_patches,1);
 await page.evaluate(()=>window.layerTest.disable());const stoppedCalls=calls;await page.waitForTimeout(300);assert.equal(calls,stoppedCalls,'disabling stops new capture requests');
 assert.equal(await page.evaluate(()=>window.layerTest.dispose()),0);assert.deepEqual(errors,[]);console.log(JSON.stringify({scope:'synthetic latest-grid Mac layer adapter; not live link or headset FPS',stats,moved,reset,default_off:true,disposed:true}));
}finally{await browser.close();await new Promise(r=>server.close(r));}
