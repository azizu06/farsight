import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import ts from 'typescript';
import {ControlSafety} from '../src/control-safety.js';

const module = {exports:{}};
const code = ts.transpileModule(readFileSync(new URL('../src/rover.ts', import.meta.url),'utf8'), {
  compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}
}).outputText;
vm.runInNewContext(code,{exports:module.exports,require:()=>({})});

function fixture() {
  const view=Object.create(module.exports.RoverExperience.prototype);
  const gate=new ControlSafety(); gate.acceptFrame('PHONE RGB:7',100,0);
  let clears=0, draws=0;
  const context={clearRect(){clears++;}};
  Object.assign(view,{
    identityCanvas:{getContext:()=>context,hidden:false},identityTexture:{needsUpdate:false},
    identityPanel:{visible:true},identityDirty:false,lastIdentityPaint:0,identityPaintSource:'PHONE RGB',
    identityCards:{draw:(_ctx,{now,source})=>{draws++; return source==='PHONE RGB' && now<1000 ? 1:0;}},
    frameSource:'PHONE RGB',gate,renderer:{xr:{isPresenting:false}},actionMenu:{visible:false},
    h264Enabled:true,h264:{video:{readyState:2},displayedFrame:null},dirty:true,lastCanvasDraw:0,
    lastHudAt:0,bannerPending:null,drawHUD(){},
  });
  return {view,clears:()=>clears,draws:()=>draws};
}

test('identity pixels expire during frozen H264 without repainting or renewing camera evidence',()=>{
  const {view,clears,draws}=fixture();
  view.paint(1200);
  assert.equal(clears(),1);
  assert.equal(draws(),1);
  assert.equal(view.identityPanel.visible,false);
  assert.equal(view.identityCanvas.hidden,true);
  assert.equal(view.identityTexture.needsUpdate,true);
  assert.equal(view.lastCanvasDraw,0);
  assert.equal(view.gate.videoAge(1200),1300);
  assert.equal(view.dirty,true,'unverified camera still awaits an actual decoded frame');
});

test('displayed source change clears an old card immediately within the paint interval',()=>{
  const {view,clears}=fixture();
  view.frameSource='USB CAMERA';
  view.paintIdentity(10);
  assert.equal(clears(),1);
  assert.equal(view.identityPanel.visible,false);
  assert.equal(view.identityCanvas.hidden,true);
  assert.equal(view.identityPaintSource,'USB CAMERA');
});


test('minimal immersive view suppresses identity cards without renewing camera evidence',()=>{
  const {view,clears,draws}=fixture(); view.renderer.xr.isPresenting=true;
  view.paintIdentity(20);
  assert.equal(view.identityPanel.visible,false);
  assert.equal(view.identityCanvas.hidden,true);
  assert.equal(clears(),0); assert.equal(draws(),0);
  assert.equal(view.gate.videoAge(20),120);
});
