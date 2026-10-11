import assert from 'node:assert/strict';
import {mkdir,writeFile,readFile} from 'node:fs/promises';
import {resolve} from 'node:path';
import {createHash} from 'node:crypto';
import {chromium} from '../../quest/node_modules/playwright/index.mjs';
const args=process.argv.slice(2),option=(key,fallback)=>{const at=args.indexOf(key);return at<0?fallback:args[at+1];};
const url=option('--url','http://127.0.0.1:8097'),recording=resolve(option('--recording','.cache/recorded-frame.bin')),out=resolve(option('--out','.cache/scan-compare-review'));
await mkdir(out,{recursive:true});
const browser=await chromium.launch({headless:true,...(process.env.PLAYWRIGHT_EXECUTABLE_PATH?{executablePath:process.env.PLAYWRIGHT_EXECUTABLE_PATH}:{}),args:['--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader']});
const page=await browser.newPage({viewport:{width:1440,height:960}}),errors=[],requests=[];
page.on('pageerror',error=>errors.push(error.message));page.on('request',r=>requests.push({method:r.method(),path:new URL(r.url()).pathname}));
try{
 await page.goto(url);await page.waitForFunction(()=>window.scanCompare?.ready);
 if(args.includes('--fixture-live')){
  await page.request.get('http://127.0.0.1:18762/fresh');
  await page.locator('#live').click();
  await page.waitForFunction(()=>scanCompare.stats().accepted_captures>0,null,{timeout:6000});
  assert.equal((await page.evaluate(()=>scanCompare.stats())).input,'live','fresh-page Live button starts native capture');
  await page.locator('#live').click();
  await page.request.get('http://127.0.0.1:18762/delay?ms=800');
  await page.locator('#live').click();
  await page.waitForFunction(()=>scanCompare.stats().capture_pending);
  const beforeFreeze=await page.evaluate(()=>scanCompare.stats().accepted_captures);
  await page.locator('#freeze').click();await page.waitForTimeout(1200);
  assert.equal((await page.evaluate(()=>scanCompare.stats())).accepted_captures,beforeFreeze,'Freeze keeps the accepted native sample unchanged');
  await page.request.get('http://127.0.0.1:18762/fresh');
  await page.reload();await page.waitForFunction(()=>window.scanCompare?.ready);
  const encoded=(await readFile(recording)).toString('base64');
  await page.evaluate(encoded=>{
   const bytes=Uint8Array.from(atob(encoded),c=>c.charCodeAt(0));
   scanCompare.loadRecording(bytes.buffer,'race-recording.bin');
   document.querySelector('#live').onclick();
  },encoded);
  await page.waitForFunction(()=>scanCompare.stats().accepted_captures===1);
  const recordedRace=await page.evaluate(()=>scanCompare.stats());
  assert.equal(recordedRace.input,'recorded','accepted provenance follows dispatched packet, not latest button intent');
  assert.equal(recordedRace.frozen,true);assert.equal(recordedRace.streaming,false);assert.equal(recordedRace.native_age_ms,null);
  await page.reload();await page.waitForFunction(()=>window.scanCompare?.ready);
  await page.evaluate(encoded=>{
   const bytes=Uint8Array.from(atob(encoded),c=>c.charCodeAt(0)),size=new DataView(bytes.buffer).getUint32(0,true);
   const header=JSON.parse(new TextDecoder().decode(bytes.subarray(4,4+size)));header.capture_source='SIMULATED RECORDED FIXTURE';
   const json=new TextEncoder().encode(JSON.stringify(header)),packet=new Uint8Array(4+json.length+bytes.length-4-size);
   new DataView(packet.buffer).setUint32(0,json.length,true);packet.set(json,4);packet.set(bytes.subarray(4+size),4+json.length);
   scanCompare.loadRecording(packet.buffer,'simulated-recorded.bin');
  },encoded);
  await page.waitForFunction(()=>scanCompare.stats().accepted_captures===1);
  assert.equal((await page.evaluate(()=>scanCompare.stats())).source,'RECORDED SIMULATED RECORDED FIXTURE','recorded label preserves original source');
  await page.reload();await page.waitForFunction(()=>window.scanCompare?.ready);requests.length=0;
 }
 await page.waitForTimeout(300);assert.equal(requests.filter(r=>r.path==='/capture/frame.bin').length,0,'idle dashboard does not pull raw');
 await page.locator('#recording').setInputFiles(recording);
 await page.waitForFunction(()=>window.scanCompare.stats().accepted_captures===1,null,{timeout:15000});
 const original=await page.evaluate(()=>scanCompare.stats());assert.equal(original.input,'recorded');assert.ok(original.native_points>0);assert.ok(original.native_faces>0);
 assert.match(await page.locator('#source').innerText(),/RECORDED IPHONE LIDAR/);assert.equal(original.native_age_ms,null);
 const fingerprint=s=>JSON.stringify([s.native_frame,s.native_identity,s.native_points,s.native_faces,s.native_bytes,s.camera,s.target]);
 const results=[],hashes=[];
 for(const mode of ['points','balanced','texture']){
  await page.evaluate(mode=>{scanCompare.setMode(mode);scanCompare.render();},mode);
  const s=await page.evaluate(()=>scanCompare.stats());assert.equal(fingerprint(s),fingerprint(original),'same packet and camera after mode switch');
  const png=await page.screenshot({path:out+'/'+mode+'-recorded.png'});hashes.push(createHash('sha256').update(png).digest('hex'));results.push(s);
 }
 assert.equal(new Set(hashes).size,3,'all three modes produce different rendered pixels');
 assert.equal(requests.filter(r=>r.path==='/capture/frame.bin').length,0,'recorded A/B/C comparison uses no raw HTTP');
 if(args.includes('--fixture-live')){
  await page.locator('#capture').click();await page.waitForFunction(()=>scanCompare.stats().accepted_captures===2,null,{timeout:15000});
  const live=await page.evaluate(()=>scanCompare.stats());assert.equal(live.input,'live');assert.equal(live.frozen,true);assert.equal(live.streaming,false);assert.match(live.source,/SIMULATED/);
  await page.waitForTimeout(1200);assert.equal(requests.filter(r=>r.path==='/capture/frame.bin').length,1,'one Capture action makes one raw read');
  await page.request.get('http://127.0.0.1:18762/stale');await page.locator('#capture').click();
  await page.waitForFunction(()=>scanCompare.stats().error?.includes('expired'),null,{timeout:10000});
  assert.equal((await page.evaluate(()=>scanCompare.stats())).accepted_captures,2,'stale transport data is not accepted');
 }
 assert.deepEqual(errors,[]);
 const report={scope:'Recorded RGB-D offline comparison; optional live-transport checks use a local fixture, not the rig',results,errors,requests};
 await writeFile(out+'/report.json',JSON.stringify(report,null,2));console.log(JSON.stringify({output:out,results,errors,raw_reads:requests.filter(r=>r.path==='/capture/frame.bin').length}));
}finally{await browser.close();}
