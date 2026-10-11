import assert from 'node:assert/strict';
import vm from 'node:vm';
import { readFileSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
const filename=new URL('./continuous-sbs.mjs',import.meta.url);
const source=readFileSync(filename,'utf8');
const init=source.match(/await page\.addInitScript\(origin=>\{([\s\S]*?)\n  \},origin\);/)[1];
class FakeWebSocket extends EventTarget {
  constructor(url){super();this.url=url;}
  send(){}
}
const setup=()=>{
  const window={WebSocket:FakeWebSocket};
  const context={window,location:{href:"http://127.0.0.1:18873/"},document:{createElement:name=>({name})},CanvasRenderingContext2D:{prototype:{fillText(){}}},URL,DOMException,performance:{now:()=>Date.now()}};
  vm.runInNewContext('(origin=>{'+init+'})("http://127.0.0.1:18873")',context);
  return window;
};
let window=setup();
let first=new window.WebSocket('ws://127.0.0.1:18873/ws/control');
first.dispatchEvent(new Event('open'));
first.dispatchEvent(new Event('close'));
const second=new window.WebSocket('ws://127.0.0.1:18873/ws/control');
second.dispatchEvent(new Event('open'));
let state=window.__continuousSbs.controlState;
assert.deepEqual([state.constructed,state.opened,state.closed,state.live,state.maxOpen],[2,2,1,1,1]);
assert.equal(state.events.filter(e=>e.type==='open').length,2);
assert.equal(state.events.filter(e=>e.type==='close').length,1);
assert.equal(state.records[0].closedAt!==null,true);
console.log('PASS reconnect distinguishes cumulative 2 from max OPEN 1');
window=setup();first=new window.WebSocket('ws://127.0.0.1:18873/ws/control');first.dispatchEvent(new Event('open'));
new window.WebSocket('ws://127.0.0.1:18873/ws/control').dispatchEvent(new Event('open'));
state=window.__continuousSbs.controlState;assert.equal(state.maxOpen,2);assert.equal(state.live,2);
console.log('PASS simultaneous owners produce max OPEN 2');
window=setup();first=new window.WebSocket('ws://127.0.0.1:18873/ws/control');first.dispatchEvent(new Event('error'));first.dispatchEvent(new Event('close'));
state=window.__continuousSbs.controlState;assert.deepEqual([state.constructed,state.errors,state.closed,state.live,state.maxOpen],[1,1,1,0,0]);
assert.throws(()=>new window.WebSocket('ws://127.0.0.1:18870/ws/control'),/Only the agreed SIM relay/);
assert.throws(()=>new window.WebSocket('ws://127.0.0.1:18873/ws/unapproved'),/Only the agreed SIM relay/);
console.log('PASS failed connection stays nonnegative; foreign origin/path blocked before native constructor');
const classifier=source.match(/const mapDownload = ([^\n]+);/)[1];
const mapDownload=vm.runInNewContext(classifier,{URL});
for(const path of ['/map/points.bin','/map/mesh.bin','/dense/manifest?foo=1','/dense/chunk.bin?lod=1'])assert.equal(mapDownload('http://127.0.0.1:18873'+path),true,path);
for(const path of ['/dense/metrics','/ws/map','/assets/main.js','/map/state'])assert.equal(mapDownload('http://127.0.0.1:18873'+path),false,path);
console.log('PASS map geometry HTTP classifier excludes metrics and metadata');
const env={...process.env};delete env.FARSIGHT_SBS_GO;delete env.FARSIGHT_PRODUCER_SHA;
const result=spawnSync(process.execPath,[filename.pathname],{env,encoding:'utf8'});
assert.notEqual(result.status,0);assert.match(result.stderr,/Require a NEW owner GPU GO/);
console.log('PASS absent new GO refuses execution before any browser launch');
