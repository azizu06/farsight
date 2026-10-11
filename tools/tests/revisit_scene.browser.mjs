// Actual worker, texture retirement and GPU pixels; synthetic native frames only.
import assert from 'node:assert/strict';
import {createServer} from 'node:http';
import {readFile} from 'node:fs/promises';
import {chromium} from '../../quest/node_modules/playwright/index.mjs';
const directory=process.argv[2];let phase=0;
const html=`<script type="module">
import * as THREE from '/quest/node_modules/three/build/three.module.js';
import {rawTextureLayer} from '/layer/raw-texture-layer.mjs';
const root=new THREE.Group(),scene=new THREE.Scene(),camera=new THREE.PerspectiveCamera(60,1,.01,30),renderer=new THREE.WebGLRenderer();
scene.add(root);renderer.setSize(128,128);document.body.append(renderer.domElement);camera.lookAt(0,0,-1);
const layer=rawTextureLayer();layer.init({root,requestFrame:()=>{}});
renderer.setAnimationLoop(()=>renderer.render(scene,camera));
window.sceneTest={capture:()=>layer.captureOnce(),stats:()=>{
 renderer.render(scene,camera);const gl=renderer.getContext(),pixel=new Uint8Array(4);gl.readPixels(64,64,1,1,gl.RGBA,gl.UNSIGNED_BYTE,pixel);
 const meshes=[];root.traverse(o=>{if(o.isMesh){const positions=o.geometry.getAttribute('position').array,indices=o.geometry.index.array;
  const used=indices.subarray(0,o.geometry.drawRange.count===Infinity?indices.length:o.geometry.drawRange.count);
  meshes.push({id:o.userData.id,z:[...new Set([...used].map(i=>positions[i*3+2]))],faces:o.userData.triangles});}});
 return {...layer.stats(),meshes,pixel:[...pixel],drawn:renderer.info.render.triangles};
},dispose:()=>{layer.dispose();renderer.setAnimationLoop(null);renderer.dispose();}};
</script>`;
const server=createServer(async(req,res)=>{
 try{
  const url=new URL(req.url,'http://localhost'),path=url.pathname;
  if(path==='/'){res.setHeader('Content-Type','text/html');return res.end(html);}
  if(path.startsWith('/phase/')){phase=Number(path.split('/').at(-1));return res.end('ok');}
  if(path==='/phone/frame.bin'){
   if(url.searchParams.get('after')===String(phase)){res.writeHead(204);return res.end();}
   res.setHeader('X-Capture-Key',String(phase));res.setHeader('X-Capture-Age-Ms','0');return res.end(await readFile(directory+'/frame'+phase+'.bin'));
  }
  const permitted=/^\/layer\/(raw-texture-layer\.mjs|assets\/raw-fusion\.worker-[\w-]+\.js)$/.test(path)||/^\/quest\/node_modules\/three\/build\/three\.(module|core)\.js$/.test(path);
  if(!permitted){res.writeHead(404);return res.end();}
  res.setHeader('Content-Type','text/javascript');res.end(await readFile(path.startsWith('/layer/')?'.cache/dense-layer-dist/'+path.slice(7):'.'+path));
 }catch(e){res.writeHead(500);res.end(String(e));}
});await new Promise(r=>server.listen(0,'127.0.0.1',r));
const origin='http://127.0.0.1:'+server.address().port,browser=await chromium.launch({args:['--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader']});
try{
 const page=await browser.newPage(),errors=[];page.on('pageerror',error=>errors.push(String(error)));
 await page.goto(origin);await page.waitForFunction(()=>window.sceneTest);
 const capture=async phase=>{
  await fetch(origin+'/phase/'+phase);await page.evaluate(()=>window.sceneTest.capture());
  await page.waitForFunction(phase=>window.sceneTest.stats().raw_frame===phase,phase,{timeout:10000});
  return page.evaluate(()=>window.sceneTest.stats());
 };
 const foreground=await capture(0);assert.equal(foreground.raw_patches,1);assert.ok(foreground.pixel[0]>foreground.pixel[1]+100);
 const cleared=await capture(1);assert.equal(cleared.raw_patches,1);assert.deepEqual(cleared.meshes[0].z,[-2]);assert.ok(cleared.pixel[1]>cleared.pixel[0]+100);assert.equal(cleared.drawn,98);
 const other=await capture(2);assert.equal(other.raw_patches,2);
 const occluded=await capture(3);assert.equal(occluded.raw_patches,3);assert.ok(occluded.meshes.some(mesh=>mesh.z.includes(-2)));assert.ok(occluded.pixel[0]>occluded.pixel[1]+100);
 const revisited=await capture(4);assert.equal(revisited.raw_patches,2);assert.ok(revisited.meshes.some(mesh=>mesh.z.includes(-3)));assert.ok(revisited.meshes.every(mesh=>!mesh.z.includes(-1)));assert.ok(revisited.pixel[1]>revisited.pixel[0]+100);
 assert.deepEqual(errors,[]);await page.evaluate(()=>window.sceneTest.dispose());
 console.log(JSON.stringify({passed:true,scope:'synthetic native frames, actual worker/Three.js/Chromium GPU; no hardware',foreground_pixel:foreground.pixel,replaced_pixel:cleared.pixel,revisited_pixel:revisited.pixel,removed_foreground:true,other_scene_preserved:true,occluded_background_preserved:true}));
}finally{await browser.close();await new Promise(r=>server.close(r));}
