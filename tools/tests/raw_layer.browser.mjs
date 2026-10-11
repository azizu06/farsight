// Built Mac layer contract test; synthetic native packet, no rig or controls.
import assert from 'node:assert/strict';
import {createServer} from 'node:http';
import {readFile} from 'node:fs/promises';
import {chromium} from '../../quest/node_modules/playwright/index.mjs';
const packet=await readFile('.cache/raw-fixture.bin');let read=false;
const html=`<script type="module">
import * as THREE from '/quest/node_modules/three/build/three.module.js';
import {rawTextureLayer} from '/layer/raw-texture-layer.mjs';
const root=new THREE.Group(),scene=new THREE.Scene(),camera=new THREE.PerspectiveCamera(60,1,.01,20),renderer=new THREE.WebGLRenderer();scene.add(root);document.body.append(renderer.domElement);camera.position.set(0,0,2);
let dirtied=0;const layer=rawTextureLayer();layer.init({root,requestFrame:()=>dirtied++});
let disposed=false;renderer.setAnimationLoop(()=>{if(!disposed){layer.poll();renderer.render(scene,camera);}});
window.layerTest={stats:()=>({...layer.stats(),dirtied,children:root.children.length}),dispose:()=>{disposed=true;layer.dispose();renderer.setAnimationLoop(null);renderer.dispose();return root.children.length;}};
</script>`;
const server=createServer(async(req,res)=>{
 try{
  const path=new URL(req.url,'http://localhost').pathname;
  if(path==='/'){res.setHeader('Content-Type','text/html');return res.end(html);}
  if(path==='/phone/frame.bin'){if(read){res.writeHead(204);return res.end();}read=true;res.setHeader('X-Capture-Key','synthetic:1');res.setHeader('X-Capture-Age-Ms','0');return res.end(packet);}
  const allowed=/^\/layer\/(raw-texture-layer\.mjs|assets\/raw-fusion\.worker-[\w-]+\.js)$/.test(path)||/^\/quest\/node_modules\/three\/build\/three\.(module|core)\.js$/.test(path);
  if(!allowed){res.writeHead(404);return res.end();}
  res.setHeader('Content-Type','text/javascript');res.end(await readFile(path.startsWith('/layer/')?'.cache/dense-layer-dist/'+path.slice(7):'.'+path));
 }catch(e){res.writeHead(500);res.end(String(e));}
});await new Promise(r=>server.listen(0,'127.0.0.1',r));
const browser=await chromium.launch({channel:'chrome',headless:true,args:['--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader']});
try{
 const page=await browser.newPage(),errors=[];page.on('pageerror',e=>errors.push(String(e)));page.on('websocket',s=>{throw Error(s.url());});
 await page.goto(`http://127.0.0.1:${server.address().port}/`);await page.waitForFunction(()=>window.layerTest?.stats().raw_points===48,{timeout:10000});
 const stats=await page.evaluate(()=>window.layerTest.stats());assert.equal(stats.raw_triangles,70);assert.ok(stats.dirtied>0);assert.equal(stats.children,1);
 assert.equal(await page.evaluate(()=>window.layerTest.dispose()),0);assert.deepEqual(errors,[]);console.log(JSON.stringify({scope:'synthetic Mac layer adapter',stats,disposed:true}));
}finally{await browser.close();await new Promise(r=>server.close(r));}
